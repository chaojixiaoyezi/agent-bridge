"""Room task policy, durable task ledger, assignment, and Agent execution state."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Sequence
from typing import Any

from .store_constants import (
    MAX_TASK_TARGETS,
    OWNER_PARTICIPANT_ID,
    TASK_CLAIM_LEASE_SECONDS,
    TASK_INPUT_REDELIVERY_SECONDS,
)
from .store_errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
)
from .validation import (
    ValidationError,
    body,
    compact_json,
    conversation_id as validate_conversation_id,
    opaque_id,
)


ROOM_TASK_SCHEMA = """
CREATE TABLE IF NOT EXISTS room_task_policies (
    conversation_id TEXT PRIMARY KEY,
    allow_global_admin INTEGER NOT NULL DEFAULT 0
        CHECK (allow_global_admin IN (0, 1)),
    updated_by_web_user_id TEXT,
    updated_at REAL NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES rooms(conversation_id),
    FOREIGN KEY (updated_by_web_user_id) REFERENCES web_users(user_id)
);

CREATE TABLE IF NOT EXISTS room_task_grants (
    conversation_id TEXT NOT NULL,
    web_user_id TEXT NOT NULL,
    can_assign_tasks INTEGER NOT NULL DEFAULT 0
        CHECK (can_assign_tasks IN (0, 1)),
    can_cancel_tasks INTEGER NOT NULL DEFAULT 0
        CHECK (can_cancel_tasks IN (0, 1)),
    granted_by_web_user_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (conversation_id, web_user_id),
    FOREIGN KEY (conversation_id) REFERENCES rooms(conversation_id),
    FOREIGN KEY (web_user_id) REFERENCES web_users(user_id),
    FOREIGN KEY (granted_by_web_user_id) REFERENCES web_users(user_id)
);

CREATE TABLE IF NOT EXISTS room_tasks (
    task_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    source_message_id TEXT UNIQUE,
    parent_task_id TEXT,
    issuer_web_user_id TEXT,
    issuer_participant_id TEXT NOT NULL,
    target_kind TEXT NOT NULL
        CHECK (target_kind IN ('participants', 'room_agents')),
    target_participant_ids_json TEXT NOT NULL DEFAULT '[]',
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN (
            'queued', 'claimed', 'running', 'needs_input',
            'completed', 'failed', 'cancelled'
        )),
    claimed_by_participant_id TEXT,
    claimed_at REAL,
    lease_expires_at REAL,
    started_at REAL,
    completed_at REAL,
    result_summary TEXT,
    execution_cwd TEXT,
    execution_thread_id TEXT,
    source_sequence INTEGER,
    context_start_sequence INTEGER,
    context_end_sequence INTEGER,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES rooms(conversation_id),
    FOREIGN KEY (source_message_id) REFERENCES messages(message_id),
    FOREIGN KEY (parent_task_id) REFERENCES room_tasks(task_id),
    FOREIGN KEY (issuer_web_user_id) REFERENCES web_users(user_id),
    FOREIGN KEY (issuer_participant_id) REFERENCES participants(participant_id),
    FOREIGN KEY (claimed_by_participant_id) REFERENCES participants(participant_id)
);

CREATE INDEX IF NOT EXISTS idx_room_tasks_room_created
    ON room_tasks(conversation_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_room_tasks_claim
    ON room_tasks(status, conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_room_task_grants_user
    ON room_task_grants(web_user_id, conversation_id);

CREATE TABLE IF NOT EXISTS room_task_inputs (
    input_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    source_message_id TEXT NOT NULL,
    source_sequence INTEGER NOT NULL,
    issuer_web_user_id TEXT NOT NULL,
    target_participant_id TEXT NOT NULL,
    body TEXT NOT NULL,
    first_delivered_at REAL,
    last_delivered_at REAL,
    delivery_count INTEGER NOT NULL DEFAULT 0
        CHECK (delivery_count >= 0),
    applied_at REAL,
    created_at REAL NOT NULL,
    UNIQUE (task_id, source_message_id),
    FOREIGN KEY (task_id) REFERENCES room_tasks(task_id),
    FOREIGN KEY (source_message_id) REFERENCES messages(message_id),
    FOREIGN KEY (issuer_web_user_id) REFERENCES web_users(user_id),
    FOREIGN KEY (target_participant_id) REFERENCES participants(participant_id)
);

CREATE INDEX IF NOT EXISTS idx_room_task_inputs_delivery
    ON room_task_inputs(task_id, applied_at, last_delivered_at, source_sequence);
CREATE INDEX IF NOT EXISTS idx_room_task_inputs_source
    ON room_task_inputs(source_message_id, task_id);
"""


class RoomTaskMixin:
    def send_web_task(
        self,
        *,
        authorized_session_id: str,
        participant_id: str,
        conversation_id: str,
        body_text: str,
        target_participant_ids: Sequence[str] | None = None,
        reply_to: str | None = None,
    ) -> dict[str, Any]:
        """Create a server-authorized task without changing ordinary chat authority."""

        normalized_body = str(body_text or "").strip()
        if normalized_body.startswith("/任务"):
            normalized_body = normalized_body[len("/任务") :].lstrip("：: \t")
        if not normalized_body:
            raise ValidationError("任务内容不能为空")
        requested_targets = self._normalize_mentions(target_participant_ids)

        # The Web composer normally sends structured IDs. Keep the typed
        # ``/任务 @昵称 ...`` shortcut equivalent by resolving exact visible
        # aliases only when the caller omitted those IDs. The stored task body
        # remains unchanged and no ordinary chat delivery is created.
        if not requested_targets:
            with self._connection() as conn:
                requested_targets = self._infer_text_mentions_locked(
                    conn,
                    conversation_id=validate_conversation_id(conversation_id),
                    sender_participant_id=opaque_id(
                        participant_id,
                        field="participant_id",
                    ),
                    body_text=normalized_body,
                )

        return self.send(
            authorized_session_id=authorized_session_id,
            sender_participant_id=participant_id,
            conversation_id=conversation_id,
            body_text=normalized_body,
            audience_kind="room",
            audience_value="*",
            mentions=[],
            reply_to=reply_to,
            _web_user=True,
            _message_kind="task",
            _suppress_chat_authorization=True,
            _suppress_mention_inference=True,
            _task_request={
                "target_participant_ids": requested_targets
            },
        )

    def convert_web_message_to_task(
        self,
        *,
        authorized_session_id: str,
        participant_id: str,
        message_id: str,
        target_participant_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Atomically turn the caller's ordinary room message into a task.

        The original message remains the immutable source.  Its nearby history
        locator is captured so the execution seat receives the full handoff
        context instead of relying on a shadow Agent's paraphrase.
        """

        session = opaque_id(authorized_session_id, field="authorized_session_id")
        participant = opaque_id(participant_id, field="participant_id")
        source_id = opaque_id(message_id, field="message_id")
        requested_targets = self._normalize_mentions(target_participant_ids)
        now = time.time()
        task_id = f"task_{uuid.uuid4().hex}"
        with self._transaction() as conn:
            identity = self._require_live_web_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=now,
            )
            source = conn.execute(
                "SELECT * FROM messages WHERE message_id = ?",
                (source_id,),
            ).fetchone()
            if source is None:
                raise NotFoundError(f"unknown message: {source_id}")
            if str(source["sender_participant_id"]) != participant:
                raise AuthorizationError("只能把自己发送的普通消息转为任务")
            if str(source["message_kind"]) != "message":
                raise ConflictError("只有普通聊天消息可以转为任务")
            conversation = str(source["conversation_id"])
            self._require_web_room_access_locked(
                conn,
                web_identity=identity,
                conversation_id=conversation,
            )
            permissions = self._room_task_permissions_locked(
                conn,
                conversation_id=conversation,
                web_identity=identity,
            )
            if not permissions["can_assign_tasks"]:
                raise AuthorizationError("你没有在这个聊天室布置任务的权限")
            if conn.execute(
                "SELECT 1 FROM room_tasks WHERE source_message_id = ?",
                (source_id,),
            ).fetchone() is not None:
                raise ConflictError("这条消息已经转为任务")
            if not requested_targets:
                mentioned = self._normalize_mentions(
                    json.loads(str(source["mentions_json"] or "[]"))
                )
                if mentioned:
                    placeholders = ",".join("?" for _ in mentioned)
                    requested_targets = [
                        str(row["participant_id"])
                        for row in conn.execute(
                            f"""
                            SELECT participant.participant_id
                            FROM participants AS participant
                            JOIN memberships AS membership
                              ON membership.participant_id = participant.participant_id
                             AND membership.conversation_id = ?
                             AND membership.active = 1
                            LEFT JOIN web_users AS web_user
                              ON web_user.participant_id = participant.participant_id
                            WHERE participant.participant_id IN ({placeholders})
                              AND web_user.user_id IS NULL
                            """,
                            (conversation, *mentioned),
                        ).fetchall()
                    ]
            restricted_recipients = {
                str(row["participant_id"])
                for row in conn.execute(
                    "SELECT participant_id FROM message_restriction_recipients "
                    "WHERE message_id = ?",
                    (source_id,),
                ).fetchall()
            }
            if restricted_recipients and not requested_targets:
                requested_targets = sorted(restricted_recipients)
            target_kind, target_ids = self._resolve_task_targets_locked(
                conn,
                conversation_id=conversation,
                requested_participant_ids=requested_targets,
            )
            unauthorized_targets = sorted(
                set(target_ids) - restricted_recipients
            ) if restricted_recipients else []
            if unauthorized_targets:
                raise AuthorizationError(
                    "定向文件或图片消息只能转交给发送时已经指定的 Agent"
                )
            source_sequence = int(source["sequence"])
            context_end = int(
                conn.execute(
                    "SELECT COALESCE(MAX(sequence), ?) FROM messages "
                    "WHERE conversation_id = ?",
                    (source_sequence, conversation),
                ).fetchone()[0]
            )
            conn.execute(
                """
                INSERT INTO room_tasks
                    (task_id, conversation_id, source_message_id,
                     parent_task_id, issuer_web_user_id,
                     issuer_participant_id, target_kind,
                     target_participant_ids_json, body, status,
                     source_sequence, context_start_sequence,
                     context_end_sequence, created_at, updated_at)
                VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    conversation,
                    source_id,
                    str(identity["user_id"]),
                    participant,
                    target_kind,
                    compact_json(target_ids),
                    str(source["body"]),
                    source_sequence,
                    max(1, source_sequence - 20),
                    context_end,
                    now,
                    now,
                ),
            )
            # The route-immutability trigger permits this one transition only
            # after the durable task row exists in the same transaction.
            conn.execute(
                "UPDATE messages SET message_kind = 'task', updated_at = ? "
                "WHERE message_id = ?",
                (now, source_id),
            )
            conn.execute(
                """
                UPDATE message_deliveries
                SET state = 'cancelled', delivery_stage = 'cancelled',
                    actionable = 0
                WHERE message_id = ?
                  AND participant_id IN (
                      SELECT participant.participant_id
                      FROM participants AS participant
                      LEFT JOIN web_users AS web_user
                        ON web_user.participant_id = participant.participant_id
                      WHERE web_user.user_id IS NULL
                  )
                  AND state IN ('pending', 'delivered')
                """,
                (source_id,),
            )
            task_row = conn.execute(
                "SELECT * FROM room_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            message_row = conn.execute(
                "SELECT * FROM messages WHERE message_id = ?",
                (source_id,),
            ).fetchone()
            result = self._message_payload(message_row, authorization=None)
            result.update(
                self._message_asset_projection_locked(conn, [source_id])[source_id]
            )
            result["task"] = self._task_payload(task_row)
        return result

    @staticmethod
    def _body_ready_targets_locked(
        conn: sqlite3.Connection,
        *,
        conversation_id: str,
        participant_ids: Sequence[str],
    ) -> set[str]:
        requested = sorted(set(participant_ids))
        if not requested:
            return set()
        placeholders = ",".join("?" for _ in requested)
        rows = conn.execute(
            f"""
            SELECT DISTINCT membership.participant_id
            FROM memberships AS membership
            JOIN agent_connectors AS connector
              ON connector.accepted_participant_id = membership.participant_id
             AND connector.conversation_id = membership.conversation_id
             AND connector.setup_status = 'configured'
             AND connector.revoked_at IS NULL
            JOIN connector_component_readiness AS readiness
              ON readiness.connector_id = connector.connector_id
             AND readiness.component = 'task'
            LEFT JOIN web_users AS web_user
              ON web_user.participant_id = membership.participant_id
             AND web_user.active = 1
            WHERE membership.conversation_id = ?
              AND membership.active = 1
              AND membership.participant_id IN ({placeholders})
              AND web_user.user_id IS NULL
              AND NOT EXISTS (
                    SELECT 1
                    FROM agent_connectors AS native_connector
                    WHERE native_connector.accepted_participant_id =
                          membership.participant_id
                      AND native_connector.conversation_id =
                          membership.conversation_id
                      AND native_connector.setup_status = 'configured'
                      AND native_connector.revoked_at IS NULL
                      AND native_connector.native_delivery_mode =
                          'native_preferred'
              )
            """,
            (conversation_id, *requested),
        ).fetchall()
        return {str(row["participant_id"]) for row in rows}

    def _route_web_message_to_body_locked(
        self,
        conn: sqlite3.Connection,
        *,
        message: sqlite3.Row,
        issuer_web_user_id: str,
        mentioned_participant_ids: Sequence[str],
        now: float,
    ) -> list[dict[str, Any]]:
        """Route an authorized personal @ to the persistent body seat.

        A running task receives an exact durable input. An idle body seat gets
        a new single-target task. Targets without a proven task component stay
        on the normal chat path so rolling upgrades never black-hole mentions.
        Targets whose connector has selected native_preferred also stay on that
        path, including while offline, so resume reaches the same real TUI
        instead of silently switching the public identity to an executor seat.
        """

        conversation = str(message["conversation_id"])
        ready_targets = self._body_ready_targets_locked(
            conn,
            conversation_id=conversation,
            participant_ids=mentioned_participant_ids,
        )
        if not ready_targets:
            return []
        source_message_id = str(message["message_id"])
        source_sequence = int(message["sequence"])
        issuer_participant_id = str(message["sender_participant_id"])
        message_body = str(message["body"])
        routes: list[dict[str, Any]] = []
        primary_task_id: str | None = None

        for target in mentioned_participant_ids:
            if target not in ready_targets:
                continue
            active = conn.execute(
                """
                SELECT * FROM room_tasks
                WHERE conversation_id = ?
                  AND claimed_by_participant_id = ?
                  AND status IN ('claimed', 'running', 'needs_input')
                ORDER BY COALESCE(started_at, claimed_at, created_at) DESC,
                         created_at DESC
                LIMIT 1
                """,
                (conversation, target),
            ).fetchone()
            if active is not None:
                task_id = str(active["task_id"])
                input_id = f"taskinput_{uuid.uuid4().hex}"
                conn.execute(
                    """
                    INSERT INTO room_task_inputs
                        (input_id, task_id, source_message_id,
                         source_sequence, issuer_web_user_id,
                         target_participant_id, body, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        input_id,
                        task_id,
                        source_message_id,
                        source_sequence,
                        issuer_web_user_id,
                        target,
                        message_body,
                        now,
                    ),
                )
                mode = "steer"
                if str(active["status"]) == "needs_input":
                    conn.execute(
                        """
                        UPDATE room_tasks
                        SET status = 'queued',
                            target_kind = 'participants',
                            target_participant_ids_json = ?,
                            claimed_by_participant_id = NULL,
                            claimed_at = NULL,
                            lease_expires_at = NULL,
                            completed_at = NULL,
                            updated_at = ?
                        WHERE task_id = ? AND status = 'needs_input'
                        """,
                        (compact_json([target]), now, task_id),
                    )
                    mode = "resume"
                routes.append(
                    {
                        "target_participant_id": target,
                        "task_id": task_id,
                        "task_input_id": input_id,
                        "mode": mode,
                    }
                )
                continue

            task_id = f"task_{uuid.uuid4().hex}"
            conn.execute(
                """
                INSERT INTO room_tasks
                    (task_id, conversation_id, source_message_id,
                     parent_task_id, issuer_web_user_id,
                     issuer_participant_id, target_kind,
                     target_participant_ids_json, body, status,
                     source_sequence, context_start_sequence,
                     context_end_sequence, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'participants', ?, ?, 'queued',
                        ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    conversation,
                    source_message_id if primary_task_id is None else None,
                    primary_task_id,
                    issuer_web_user_id,
                    issuer_participant_id,
                    compact_json([target]),
                    message_body,
                    source_sequence,
                    max(1, source_sequence - 20),
                    source_sequence,
                    now,
                    now,
                ),
            )
            if primary_task_id is None:
                primary_task_id = task_id
            routes.append(
                {
                    "target_participant_id": target,
                    "task_id": task_id,
                    "task_input_id": None,
                    "mode": "queued",
                }
            )
        return routes

    @staticmethod
    def _room_task_permissions_locked(
        conn: sqlite3.Connection,
        *,
        conversation_id: str,
        web_identity: sqlite3.Row,
    ) -> dict[str, Any]:
        ownership = conn.execute(
            "SELECT web_user_id FROM room_web_owners WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        policy = conn.execute(
            "SELECT * FROM room_task_policies WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        grant = conn.execute(
            "SELECT * FROM room_task_grants WHERE conversation_id = ? "
            "AND web_user_id = ?",
            (conversation_id, str(web_identity["user_id"])),
        ).fetchone()
        owner_user_id = (
            str(ownership["web_user_id"]) if ownership is not None else None
        )
        user_id = str(web_identity["user_id"])
        is_admin = str(web_identity["role"]) == "admin"
        # Orphaned legacy Web rooms are treated as admin-owned until the
        # WebAuthStore migration writes their explicit owner row.
        is_owner = owner_user_id == user_id or (owner_user_id is None and is_admin)
        admin_allowed = bool(policy["allow_global_admin"]) if policy else False
        delegated_assign = bool(grant and grant["can_assign_tasks"])
        delegated_cancel = bool(grant and grant["can_cancel_tasks"])
        can_admin_act = is_admin and (is_owner or admin_allowed)
        return {
            "conversation_id": conversation_id,
            "owner_web_user_id": owner_user_id,
            "is_room_owner": is_owner,
            "allow_global_admin": admin_allowed,
            "can_assign_tasks": bool(is_owner or can_admin_act or delegated_assign),
            "can_cancel_tasks": bool(is_owner or can_admin_act or delegated_cancel),
            "can_manage_task_permissions": bool(is_owner),
        }

    def _resolve_task_targets_locked(
        self,
        conn: sqlite3.Connection,
        *,
        conversation_id: str,
        requested_participant_ids: Sequence[str] | None,
    ) -> tuple[str, list[str]]:
        requested = self._normalize_mentions(requested_participant_ids)
        if len(requested) > MAX_TASK_TARGETS:
            raise ValidationError(
                f"task targets cannot contain more than {MAX_TASK_TARGETS} entries"
            )
        rows = conn.execute(
            """
            SELECT membership.participant_id
            FROM memberships AS membership
            LEFT JOIN web_users AS web_user
              ON web_user.participant_id = membership.participant_id
             AND web_user.active = 1
            WHERE membership.conversation_id = ?
              AND membership.active = 1
              AND web_user.user_id IS NULL
              AND membership.participant_id != ?
            ORDER BY membership.joined_at, membership.participant_id
            """,
            (conversation_id, OWNER_PARTICIPANT_ID),
        ).fetchall()
        available = [str(row["participant_id"]) for row in rows]
        available_set = set(available)
        if requested:
            invalid = [item for item in requested if item not in available_set]
            if invalid:
                raise ConflictError(
                    "task targets must be active Agent members of this room: "
                    + ", ".join(invalid)
                )
            return "participants", requested
        if not available:
            raise ConflictError("这个聊天室当前没有可领取任务的 Agent")
        return "room_agents", available[:MAX_TASK_TARGETS]

    def room_task_permissions(
        self,
        *,
        authorized_session_id: str,
        participant_id: str,
        conversation_id: str,
    ) -> dict[str, Any]:
        session = opaque_id(authorized_session_id, field="authorized_session_id")
        participant = opaque_id(participant_id, field="participant_id")
        conversation = validate_conversation_id(conversation_id)
        with self._transaction() as conn:
            identity = self._require_live_web_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=time.time(),
            )
            self._require_web_room_access_locked(
                conn,
                web_identity=identity,
                conversation_id=conversation,
            )
            self._require_active_room(conn, conversation)
            permissions = self._room_task_permissions_locked(
                conn,
                conversation_id=conversation,
                web_identity=identity,
            )
            members: list[dict[str, Any]] = []
            if permissions["can_manage_task_permissions"]:
                rows = conn.execute(
                    """
                    SELECT web_user.user_id, web_user.username,
                           web_user.display_name, web_user.role,
                           COALESCE(task_grant.can_assign_tasks, 0)
                               AS can_assign_tasks,
                           COALESCE(task_grant.can_cancel_tasks, 0)
                               AS can_cancel_tasks
                    FROM memberships AS membership
                    JOIN web_users AS web_user
                      ON web_user.participant_id = membership.participant_id
                     AND web_user.active = 1
                    LEFT JOIN room_task_grants AS task_grant
                      ON task_grant.conversation_id = membership.conversation_id
                     AND task_grant.web_user_id = web_user.user_id
                    WHERE membership.conversation_id = ?
                      AND membership.active = 1
                    ORDER BY web_user.display_name COLLATE NOCASE,
                             web_user.username COLLATE NOCASE
                    """,
                    (conversation,),
                ).fetchall()
                members = [
                    {
                        "user_id": str(row["user_id"]),
                        "username": str(row["username"]),
                        "display_name": str(row["display_name"]),
                        "role": str(row["role"]),
                        "is_room_owner": str(row["user_id"])
                        == permissions["owner_web_user_id"],
                        "can_assign_tasks": bool(row["can_assign_tasks"]),
                        "can_cancel_tasks": bool(row["can_cancel_tasks"]),
                    }
                    for row in rows
                ]
        return {**permissions, "members": members}

    def room_task_permissions_bulk(
        self,
        *,
        authorized_session_id: str,
        participant_id: str,
        conversation_ids: Sequence[str],
    ) -> dict[str, dict[str, Any]]:
        session = opaque_id(authorized_session_id, field="authorized_session_id")
        participant = opaque_id(participant_id, field="participant_id")
        conversations = [
            validate_conversation_id(value) for value in conversation_ids
        ]
        with self._connection() as conn:
            identity = self._require_live_web_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=time.time(),
            )
            result: dict[str, dict[str, Any]] = {}
            for conversation in conversations:
                self._require_web_room_access_locked(
                    conn,
                    web_identity=identity,
                    conversation_id=conversation,
                )
                result[conversation] = self._room_task_permissions_locked(
                    conn,
                    conversation_id=conversation,
                    web_identity=identity,
                )
            return result

    def update_room_task_policy(
        self,
        *,
        authorized_session_id: str,
        participant_id: str,
        conversation_id: str,
        allow_global_admin: bool,
    ) -> dict[str, Any]:
        if not isinstance(allow_global_admin, bool):
            raise ValidationError("allow_global_admin must be a boolean")
        session = opaque_id(authorized_session_id, field="authorized_session_id")
        participant = opaque_id(participant_id, field="participant_id")
        conversation = validate_conversation_id(conversation_id)
        now = time.time()
        with self._transaction() as conn:
            identity = self._require_live_web_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=now,
            )
            self._require_web_room_access_locked(
                conn,
                web_identity=identity,
                conversation_id=conversation,
            )
            self._require_active_room(conn, conversation)
            permissions = self._room_task_permissions_locked(
                conn,
                conversation_id=conversation,
                web_identity=identity,
            )
            if not permissions["can_manage_task_permissions"]:
                raise AuthorizationError("只有聊天室创建者可以调整任务权限")
            conn.execute(
                """
                INSERT INTO room_task_policies
                    (conversation_id, allow_global_admin,
                     updated_by_web_user_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    allow_global_admin = excluded.allow_global_admin,
                    updated_by_web_user_id = excluded.updated_by_web_user_id,
                    updated_at = excluded.updated_at
                """,
                (
                    conversation,
                    1 if allow_global_admin else 0,
                    str(identity["user_id"]),
                    now,
                ),
            )
        return self.room_task_permissions(
            authorized_session_id=session,
            participant_id=participant,
            conversation_id=conversation,
        )

    def update_room_task_grant(
        self,
        *,
        authorized_session_id: str,
        participant_id: str,
        conversation_id: str,
        target_web_user_id: str,
        can_assign_tasks: bool,
        can_cancel_tasks: bool,
    ) -> dict[str, Any]:
        if not isinstance(can_assign_tasks, bool) or not isinstance(
            can_cancel_tasks, bool
        ):
            raise ValidationError("task grant values must be booleans")
        session = opaque_id(authorized_session_id, field="authorized_session_id")
        participant = opaque_id(participant_id, field="participant_id")
        target = opaque_id(target_web_user_id, field="target_web_user_id")
        conversation = validate_conversation_id(conversation_id)
        now = time.time()
        with self._transaction() as conn:
            identity = self._require_live_web_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=now,
            )
            self._require_web_room_access_locked(
                conn,
                web_identity=identity,
                conversation_id=conversation,
            )
            permissions = self._room_task_permissions_locked(
                conn,
                conversation_id=conversation,
                web_identity=identity,
            )
            if not permissions["can_manage_task_permissions"]:
                raise AuthorizationError("只有聊天室创建者可以调整任务权限")
            target_row = conn.execute(
                """
                SELECT web_user.user_id
                FROM web_users AS web_user
                JOIN memberships AS membership
                  ON membership.participant_id = web_user.participant_id
                 AND membership.conversation_id = ?
                 AND membership.active = 1
                WHERE web_user.user_id = ? AND web_user.active = 1
                """,
                (conversation, target),
            ).fetchone()
            if target_row is None:
                raise NotFoundError("目标用户不是这个聊天室的有效成员")
            if target == permissions["owner_web_user_id"]:
                raise ConflictError("聊天室创建者始终拥有完整任务权限")
            if can_assign_tasks or can_cancel_tasks:
                conn.execute(
                    """
                    INSERT INTO room_task_grants
                        (conversation_id, web_user_id, can_assign_tasks,
                         can_cancel_tasks, granted_by_web_user_id,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(conversation_id, web_user_id) DO UPDATE SET
                        can_assign_tasks = excluded.can_assign_tasks,
                        can_cancel_tasks = excluded.can_cancel_tasks,
                        granted_by_web_user_id = excluded.granted_by_web_user_id,
                        updated_at = excluded.updated_at
                    """,
                    (
                        conversation,
                        target,
                        1 if can_assign_tasks else 0,
                        1 if can_cancel_tasks else 0,
                        str(identity["user_id"]),
                        now,
                        now,
                    ),
                )
            else:
                conn.execute(
                    "DELETE FROM room_task_grants WHERE conversation_id = ? "
                    "AND web_user_id = ?",
                    (conversation, target),
                )
        return self.room_task_permissions(
            authorized_session_id=session,
            participant_id=participant,
            conversation_id=conversation,
        )

    def cancel_web_task(
        self,
        *,
        authorized_session_id: str,
        participant_id: str,
        task_id: str,
    ) -> dict[str, Any]:
        session = opaque_id(authorized_session_id, field="authorized_session_id")
        participant = opaque_id(participant_id, field="participant_id")
        task = opaque_id(task_id, field="task_id")
        now = time.time()
        with self._transaction() as conn:
            identity = self._require_live_web_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=now,
            )
            row = conn.execute(
                "SELECT * FROM room_tasks WHERE task_id = ?", (task,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"unknown task: {task}")
            self._require_web_room_access_locked(
                conn,
                web_identity=identity,
                conversation_id=str(row["conversation_id"]),
            )
            permissions = self._room_task_permissions_locked(
                conn,
                conversation_id=str(row["conversation_id"]),
                web_identity=identity,
            )
            owns_task = str(row["issuer_web_user_id"] or "") == str(
                identity["user_id"]
            )
            if not owns_task and not permissions["can_cancel_tasks"]:
                raise AuthorizationError("你没有取消这个任务的权限")
            if str(row["status"]) in {"completed", "failed"}:
                raise ConflictError("已结束的任务不能取消")
            if str(row["status"]) != "cancelled":
                conn.execute(
                    "UPDATE room_tasks SET status = 'cancelled', "
                    "completed_at = ?, updated_at = ? WHERE task_id = ?",
                    (now, now, task),
                )
            updated = conn.execute(
                "SELECT * FROM room_tasks WHERE task_id = ?", (task,)
            ).fetchone()
        return self._task_payload(updated)

    def claim_next_task(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
    ) -> dict[str, Any] | None:
        participant = opaque_id(participant_id, field="participant_id")
        session = opaque_id(authorized_session_id, field="authorized_session_id")
        now = time.time()
        with self._transaction() as conn:
            session_row = self._require_live_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=now,
            )
            conversation = str(session_row["registered_conversation_id"])
            self._require_membership(conn, participant, conversation)
            # A task executor can disappear after an atomic claim. Requeue only
            # expired non-terminal claims; the live worker renews this lease
            # before and during a long product turn.
            conn.execute(
                "UPDATE room_tasks SET status = 'queued', "
                "claimed_by_participant_id = NULL, claimed_at = NULL, "
                "lease_expires_at = NULL, updated_at = ? "
                "WHERE conversation_id = ? "
                "AND status IN ('claimed', 'running') "
                "AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?",
                (now, conversation, now),
            )
            candidates = conn.execute(
                "SELECT * FROM room_tasks WHERE conversation_id = ? "
                "AND status = 'queued' ORDER BY created_at, task_id LIMIT 100",
                (conversation,),
            ).fetchall()
            selected = next(
                (
                    row
                    for row in candidates
                    if participant
                    in json.loads(str(row["target_participant_ids_json"] or "[]"))
                ),
                None,
            )
            if selected is None:
                return None
            changed = conn.execute(
                "UPDATE room_tasks SET status = 'claimed', "
                "claimed_by_participant_id = ?, claimed_at = ?, "
                "lease_expires_at = ?, updated_at = ? "
                "WHERE task_id = ? AND status = 'queued'",
                (
                    participant,
                    now,
                    now + TASK_CLAIM_LEASE_SECONDS,
                    now,
                    str(selected["task_id"]),
                ),
            ).rowcount
            if changed != 1:
                return None
            updated = conn.execute(
                "SELECT * FROM room_tasks WHERE task_id = ?",
                (str(selected["task_id"]),),
            ).fetchone()
        return self._task_payload(updated)

    def wait_next_task(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        wait_seconds: float = 20.0,
    ) -> dict[str, Any]:
        bounded_wait = max(0.0, min(float(wait_seconds), 30.0))
        deadline = time.monotonic() + bounded_wait
        while True:
            claimed = self.claim_next_task(
                participant_id=participant_id,
                authorized_session_id=authorized_session_id,
            )
            if claimed is not None:
                return {"task": claimed}
            if time.monotonic() >= deadline:
                return {"task": None}
            time.sleep(min(self.poll_interval_seconds, deadline - time.monotonic()))

    @staticmethod
    def _task_input_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "input_id": str(row["input_id"]),
            "task_id": str(row["task_id"]),
            "source_message_id": str(row["source_message_id"]),
            "source_sequence": int(row["source_sequence"]),
            "issuer_web_user_id": str(row["issuer_web_user_id"]),
            "issuer_username": str(row["issuer_username"] or ""),
            "issuer_display_name": str(row["issuer_display_name"] or ""),
            "issuer_role": str(row["issuer_role"] or "user"),
            "target_participant_id": str(row["target_participant_id"]),
            "body": str(row["body"]),
            "delivery_count": int(row["delivery_count"] or 0),
            "first_delivered_at": (
                float(row["first_delivered_at"])
                if row["first_delivered_at"] is not None
                else None
            ),
            "last_delivered_at": (
                float(row["last_delivered_at"])
                if row["last_delivered_at"] is not None
                else None
            ),
            "applied_at": (
                float(row["applied_at"])
                if row["applied_at"] is not None
                else None
            ),
            "created_at": float(row["created_at"]),
        }

    def poll_agent_task_inputs(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        task_id: str,
        limit: int = 50,
    ) -> dict[str, Any]:
        participant = opaque_id(participant_id, field="participant_id")
        session = opaque_id(authorized_session_id, field="authorized_session_id")
        task = opaque_id(task_id, field="task_id")
        bounded_limit = max(1, min(int(limit), 100))
        now = time.time()
        with self._transaction() as conn:
            session_row = self._require_live_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=now,
            )
            task_row = conn.execute(
                "SELECT * FROM room_tasks WHERE task_id = ?",
                (task,),
            ).fetchone()
            if task_row is None:
                raise NotFoundError(f"unknown task: {task}")
            if str(task_row["conversation_id"]) != str(
                session_row["registered_conversation_id"]
            ):
                raise AuthorizationError("task belongs to another room")
            if str(task_row["claimed_by_participant_id"] or "") != participant:
                raise AuthorizationError(
                    "only the Agent that claimed this task may read task inputs"
                )
            if str(task_row["status"]) not in {"claimed", "running", "needs_input"}:
                return {"task_id": task, "inputs": [], "count": 0}
            rows = conn.execute(
                """
                SELECT task_input.*,
                       web_user.username AS issuer_username,
                       web_user.role AS issuer_role,
                       profile.display_name AS issuer_display_name
                FROM room_task_inputs AS task_input
                JOIN web_users AS web_user
                  ON web_user.user_id = task_input.issuer_web_user_id
                JOIN participants AS profile
                  ON profile.participant_id = web_user.participant_id
                WHERE task_input.task_id = ?
                  AND task_input.applied_at IS NULL
                  AND (
                      task_input.last_delivered_at IS NULL
                      OR task_input.last_delivered_at <= ?
                  )
                ORDER BY task_input.source_sequence, task_input.created_at,
                         task_input.input_id
                LIMIT ?
                """,
                (task, now - TASK_INPUT_REDELIVERY_SECONDS, bounded_limit),
            ).fetchall()
            if rows:
                input_ids = [str(row["input_id"]) for row in rows]
                placeholders = ",".join("?" for _ in input_ids)
                conn.execute(
                    f"""
                    UPDATE room_task_inputs
                    SET first_delivered_at = COALESCE(first_delivered_at, ?),
                        last_delivered_at = ?,
                        delivery_count = delivery_count + 1
                    WHERE input_id IN ({placeholders})
                      AND applied_at IS NULL
                    """,
                    (now, now, *input_ids),
                )
                rows = conn.execute(
                    f"""
                    SELECT task_input.*,
                           web_user.username AS issuer_username,
                           web_user.role AS issuer_role,
                           profile.display_name AS issuer_display_name
                    FROM room_task_inputs AS task_input
                    JOIN web_users AS web_user
                      ON web_user.user_id = task_input.issuer_web_user_id
                    JOIN participants AS profile
                      ON profile.participant_id = web_user.participant_id
                    WHERE task_input.input_id IN ({placeholders})
                    ORDER BY task_input.source_sequence, task_input.created_at,
                             task_input.input_id
                    """,
                    input_ids,
                ).fetchall()
            projections = self._message_asset_projection_locked(
                conn,
                [str(row["source_message_id"]) for row in rows],
            )
        inputs = []
        for row in rows:
            payload = self._task_input_payload(row)
            payload.update(projections[str(row["source_message_id"])])
            inputs.append(payload)
        return {"task_id": task, "inputs": inputs, "count": len(inputs)}

    def acknowledge_agent_task_inputs(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        task_id: str,
        input_ids: Sequence[str],
    ) -> dict[str, Any]:
        participant = opaque_id(participant_id, field="participant_id")
        session = opaque_id(authorized_session_id, field="authorized_session_id")
        task = opaque_id(task_id, field="task_id")
        normalized_ids = [
            opaque_id(value, field="input_id")
            for value in dict.fromkeys(input_ids)
        ]
        if not normalized_ids:
            return {"task_id": task, "applied_input_ids": [], "count": 0}
        if len(normalized_ids) > 100:
            raise ValidationError("input_ids cannot contain more than 100 entries")
        now = time.time()
        with self._transaction() as conn:
            session_row = self._require_live_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=now,
            )
            task_row = conn.execute(
                "SELECT * FROM room_tasks WHERE task_id = ?",
                (task,),
            ).fetchone()
            if task_row is None:
                raise NotFoundError(f"unknown task: {task}")
            if str(task_row["conversation_id"]) != str(
                session_row["registered_conversation_id"]
            ):
                raise AuthorizationError("task belongs to another room")
            if str(task_row["claimed_by_participant_id"] or "") != participant:
                raise AuthorizationError(
                    "only the Agent that claimed this task may acknowledge inputs"
                )
            placeholders = ",".join("?" for _ in normalized_ids)
            matched = conn.execute(
                f"SELECT input_id FROM room_task_inputs "
                f"WHERE task_id = ? AND input_id IN ({placeholders})",
                (task, *normalized_ids),
            ).fetchall()
            matched_ids = [str(row["input_id"]) for row in matched]
            if set(matched_ids) != set(normalized_ids):
                raise ConflictError("one or more task inputs do not belong to this task")
            conn.execute(
                f"UPDATE room_task_inputs SET applied_at = COALESCE(applied_at, ?) "
                f"WHERE task_id = ? AND input_id IN ({placeholders})",
                (now, task, *normalized_ids),
            )
        return {
            "task_id": task,
            "applied_input_ids": sorted(matched_ids),
            "count": len(matched_ids),
        }

    def update_agent_task(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        task_id: str,
        status: str,
        result_summary: str | None = None,
        execution_cwd: str | None = None,
        execution_thread_id: str | None = None,
    ) -> dict[str, Any]:
        participant = opaque_id(participant_id, field="participant_id")
        session = opaque_id(authorized_session_id, field="authorized_session_id")
        task = opaque_id(task_id, field="task_id")
        normalized_status = str(status or "").strip().lower()
        if normalized_status not in {
            "running",
            "needs_input",
            "completed",
            "failed",
        }:
            raise ValidationError("unsupported Agent task status")
        summary = str(result_summary or "").strip()
        cwd = str(execution_cwd or "").strip()
        thread_id = str(execution_thread_id or "").strip()
        if len(summary) > 20_000:
            raise ValidationError("task result summary is too long")
        if len(cwd) > 2_000 or len(thread_id) > 256:
            raise ValidationError("task execution metadata is too long")
        now = time.time()
        with self._transaction() as conn:
            session_row = self._require_live_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=now,
            )
            row = conn.execute(
                "SELECT * FROM room_tasks WHERE task_id = ?", (task,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"unknown task: {task}")
            if str(row["conversation_id"]) != str(
                session_row["registered_conversation_id"]
            ):
                raise AuthorizationError("task belongs to another room")
            if str(row["claimed_by_participant_id"] or "") != participant:
                raise AuthorizationError("only the Agent that claimed this task may update it")
            if str(row["status"]) == "cancelled":
                raise ConflictError("task was cancelled")
            current_status = str(row["status"])
            if current_status in {"completed", "failed"}:
                if current_status == normalized_status:
                    return self._task_payload(row)
                raise ConflictError("task is already finished")
            if current_status == "needs_input" and normalized_status in {
                "running",
                "completed",
            }:
                # A model can deliberately pause a task for missing local
                # authority or user context. The resident lease heartbeat and
                # wrapper closeout must not silently overwrite that decision.
                return self._task_payload(row)
            completed_at = now if normalized_status in {"completed", "failed"} else None
            lease_expires_at = (
                now + TASK_CLAIM_LEASE_SECONDS
                if normalized_status in {"running", "needs_input"}
                else None
            )
            existing_summary = str(row["result_summary"] or "")
            existing_cwd = str(row["execution_cwd"] or "")
            existing_thread_id = str(row["execution_thread_id"] or "")
            visible_task_changed = (
                normalized_status != current_status
                or (normalized_status == "running" and row["started_at"] is None)
                or (bool(summary) and summary != existing_summary)
                or (bool(cwd) and cwd != existing_cwd)
                or (bool(thread_id) and thread_id != existing_thread_id)
            )
            visible_updated_at = now if visible_task_changed else float(row["updated_at"])
            conn.execute(
                """
                UPDATE room_tasks
                SET status = ?,
                    started_at = CASE
                        WHEN ? = 'running' THEN COALESCE(started_at, ?)
                        ELSE started_at
                    END,
                    completed_at = COALESCE(?, completed_at),
                    result_summary = CASE WHEN ? != '' THEN ? ELSE result_summary END,
                    execution_cwd = CASE WHEN ? != '' THEN ? ELSE execution_cwd END,
                    execution_thread_id = CASE WHEN ? != '' THEN ?
                                               ELSE execution_thread_id END,
                    lease_expires_at = ?,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (
                    normalized_status,
                    normalized_status,
                    now,
                    completed_at,
                    summary,
                    summary,
                    cwd,
                    cwd,
                    thread_id,
                    thread_id,
                    lease_expires_at,
                    visible_updated_at,
                    task,
                ),
            )
            updated = conn.execute(
                "SELECT * FROM room_tasks WHERE task_id = ?", (task,)
            ).fetchone()
        return self._task_payload(updated)

    def delegate_agent_task(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        parent_task_id: str,
        body_text: str,
        target_participant_ids: Sequence[str],
    ) -> dict[str, Any]:
        participant = opaque_id(participant_id, field="participant_id")
        session = opaque_id(authorized_session_id, field="authorized_session_id")
        parent = opaque_id(parent_task_id, field="parent_task_id")
        normalized_body = body(body_text)
        now = time.time()
        with self._transaction() as conn:
            session_row = self._require_live_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=now,
            )
            parent_row = conn.execute(
                "SELECT * FROM room_tasks WHERE task_id = ?", (parent,)
            ).fetchone()
            if parent_row is None:
                raise NotFoundError(f"unknown parent task: {parent}")
            if str(parent_row["claimed_by_participant_id"] or "") != participant:
                raise AuthorizationError("only the task coordinator may delegate it")
            conversation = str(parent_row["conversation_id"])
            if conversation != str(session_row["registered_conversation_id"]):
                raise AuthorizationError("parent task belongs to another room")
            target_kind, target_ids = self._resolve_task_targets_locked(
                conn,
                conversation_id=conversation,
                requested_participant_ids=target_participant_ids,
            )
            child_id = f"task_{uuid.uuid4().hex}"
            conn.execute(
                """
                INSERT INTO room_tasks
                    (task_id, conversation_id, source_message_id, parent_task_id,
                     issuer_web_user_id, issuer_participant_id, target_kind,
                     target_participant_ids_json, body, status,
                     created_at, updated_at)
                VALUES (?, ?, NULL, ?, NULL, ?, ?, ?, ?, 'queued', ?, ?)
                """,
                (
                    child_id,
                    conversation,
                    parent,
                    participant,
                    target_kind,
                    compact_json(target_ids),
                    normalized_body,
                    now,
                    now,
                ),
            )
            child = conn.execute(
                "SELECT * FROM room_tasks WHERE task_id = ?", (child_id,)
            ).fetchone()
        return self._task_payload(child)

    @staticmethod
    def _task_payload(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            raise NotFoundError("task row disappeared")
        return {
            "task_id": str(row["task_id"]),
            "conversation_id": str(row["conversation_id"]),
            "source_message_id": (
                str(row["source_message_id"])
                if row["source_message_id"] is not None
                else None
            ),
            "parent_task_id": (
                str(row["parent_task_id"])
                if row["parent_task_id"] is not None
                else None
            ),
            "issuer_web_user_id": (
                str(row["issuer_web_user_id"])
                if row["issuer_web_user_id"] is not None
                else None
            ),
            "issuer_participant_id": str(row["issuer_participant_id"]),
            "target_kind": str(row["target_kind"]),
            "target_participant_ids": json.loads(
                str(row["target_participant_ids_json"] or "[]")
            ),
            "body": str(row["body"]),
            "status": str(row["status"]),
            "claimed_by_participant_id": (
                str(row["claimed_by_participant_id"])
                if row["claimed_by_participant_id"] is not None
                else None
            ),
            "claimed_at": (
                float(row["claimed_at"]) if row["claimed_at"] is not None else None
            ),
            "lease_expires_at": (
                float(row["lease_expires_at"])
                if row["lease_expires_at"] is not None
                else None
            ),
            "started_at": (
                float(row["started_at"]) if row["started_at"] is not None else None
            ),
            "completed_at": (
                float(row["completed_at"])
                if row["completed_at"] is not None
                else None
            ),
            "result_summary": (
                str(row["result_summary"])
                if row["result_summary"] is not None
                else None
            ),
            "execution_cwd": (
                str(row["execution_cwd"])
                if row["execution_cwd"] is not None
                else None
            ),
            "execution_thread_id": (
                str(row["execution_thread_id"])
                if row["execution_thread_id"] is not None
                else None
            ),
            "source_sequence": (
                int(row["source_sequence"])
                if "source_sequence" in row.keys()
                and row["source_sequence"] is not None
                else None
            ),
            "context_start_sequence": (
                int(row["context_start_sequence"])
                if "context_start_sequence" in row.keys()
                and row["context_start_sequence"] is not None
                else None
            ),
            "context_end_sequence": (
                int(row["context_end_sequence"])
                if "context_end_sequence" in row.keys()
                and row["context_end_sequence"] is not None
                else None
            ),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }
