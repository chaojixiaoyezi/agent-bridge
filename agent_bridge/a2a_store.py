"""Durable A2A access grants and room-task projection."""

from __future__ import annotations

import math
import secrets
import sqlite3
import time
import uuid
from collections.abc import Sequence
from typing import Any

from .store_errors import (
    AuthenticationError,
    BridgeError,
    ConflictError,
    NotFoundError,
)
from .validation import (
    ValidationError,
    alias,
    body,
    compact_json,
    conversation_id as validate_conversation_id,
    opaque_id,
)


A2A_GATEWAY_SCHEMA = """
CREATE TABLE IF NOT EXISTS a2a_access_grants (
    grant_id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    conversation_id TEXT NOT NULL,
    participant_id TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    created_by_web_user_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    revoked_at REAL,
    revoked_by_web_user_id TEXT,
    FOREIGN KEY (conversation_id) REFERENCES rooms(conversation_id),
    FOREIGN KEY (participant_id) REFERENCES participants(participant_id),
    FOREIGN KEY (session_id) REFERENCES agent_sessions(session_id),
    FOREIGN KEY (created_by_web_user_id) REFERENCES web_users(user_id),
    FOREIGN KEY (revoked_by_web_user_id) REFERENCES web_users(user_id)
);

CREATE TABLE IF NOT EXISTS a2a_task_links (
    task_id TEXT PRIMARY KEY,
    grant_id TEXT NOT NULL,
    context_id TEXT NOT NULL,
    request_message_id TEXT,
    created_at REAL NOT NULL,
    FOREIGN KEY (task_id) REFERENCES room_tasks(task_id),
    FOREIGN KEY (grant_id) REFERENCES a2a_access_grants(grant_id),
    FOREIGN KEY (request_message_id) REFERENCES messages(message_id)
);

CREATE INDEX IF NOT EXISTS idx_a2a_grants_room_created
    ON a2a_access_grants(conversation_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_a2a_links_grant_context
    ON a2a_task_links(grant_id, context_id, created_at DESC);
"""


class A2AStoreMixin:
    @staticmethod
    def _a2a_grant_payload(row: sqlite3.Row, *, now: float) -> dict[str, Any]:
        revoked_at = (
            float(row["revoked_at"])
            if row["revoked_at"] is not None
            else None
        )
        expires_at = float(row["expires_at"])
        return {
            "grant_id": str(row["grant_id"]),
            "conversation_id": str(row["conversation_id"]),
            "participant_id": str(row["participant_id"]),
            "label": str(row["label"]),
            "created_by_web_user_id": str(row["created_by_web_user_id"]),
            "created_at": float(row["created_at"]),
            "expires_at": expires_at,
            "revoked_at": revoked_at,
            "status": (
                "revoked"
                if revoked_at is not None
                else ("expired" if expires_at <= now else "active")
            ),
        }

    def create_a2a_access_grant(
        self,
        *,
        conversation_id: str,
        label: str,
        created_by_web_user_id: str,
        ttl_seconds: object = 30 * 24 * 60 * 60,
    ) -> dict[str, Any]:
        conversation = validate_conversation_id(conversation_id)
        normalized_label = alias(label, field="a2a_label")
        creator = opaque_id(
            created_by_web_user_id,
            field="created_by_web_user_id",
        )
        if isinstance(ttl_seconds, bool):
            raise ValidationError("A2A grant ttl must be a number")
        try:
            ttl = float(ttl_seconds)
        except (TypeError, ValueError) as exc:
            raise ValidationError("A2A grant ttl must be a number") from exc
        if not math.isfinite(ttl) or not 300 <= ttl <= 365 * 24 * 60 * 60:
            raise ValidationError("A2A grant ttl must be 300 seconds to 365 days")
        now = time.time()
        grant_id = f"a2agrant_{uuid.uuid4().hex}"
        participant_id = f"participant_{uuid.uuid4().hex}"
        session_id = f"session_{uuid.uuid4().hex}"
        access_token = f"a2a_{secrets.token_urlsafe(32)}"
        session_secret = f"session_{secrets.token_urlsafe(32)}"
        with self._transaction() as conn:
            self._require_active_admin_locked(conn, creator)
            self._require_active_room(conn, conversation)
            display = f"A2A · {normalized_label} · {grant_id[-6:]}"
            conn.execute("PRAGMA defer_foreign_keys = ON")
            conn.execute(
                """
                INSERT INTO participants
                    (participant_id, client_type, session_alias,
                     display_name, signature, avatar_key, profile_updated_at,
                     capabilities_json, status, created_at, last_seen)
                VALUES (?, ?, ?, ?, ?, 'auto', ?, '[]', 'offline', ?, ?)
                """,
                (
                    participant_id,
                    f"a2a-client-{grant_id[-12:]}",
                    normalized_label,
                    display,
                    "标准 A2A 房间任务入口",
                    now,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO agent_sessions
                    (session_id, participant_id, registered_conversation_id,
                     token_hash, transport, created_at, expires_at,
                     ttl_seconds, last_seen, component)
                VALUES (?, ?, ?, ?, 'mcp', ?, ?, ?, ?, 'a2a')
                """,
                (
                    session_id,
                    participant_id,
                    conversation,
                    self._secret_hash(session_secret),
                    now,
                    now + ttl,
                    ttl,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO a2a_access_grants
                    (grant_id, token_hash, conversation_id, participant_id,
                     session_id, label, created_by_web_user_id,
                     created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    grant_id,
                    self._secret_hash(access_token),
                    conversation,
                    participant_id,
                    session_id,
                    normalized_label,
                    creator,
                    now,
                    now + ttl,
                ),
            )
            row = conn.execute(
                "SELECT * FROM a2a_access_grants WHERE grant_id = ?",
                (grant_id,),
            ).fetchone()
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise BridgeError("A2A access grant is inconsistent")
        result = self._a2a_grant_payload(row, now=now)
        result["access_token"] = access_token
        return result

    def list_a2a_access_grants(
        self,
        *,
        requesting_web_user_id: str,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        requester = opaque_id(
            requesting_web_user_id,
            field="requesting_web_user_id",
        )
        conversation = (
            validate_conversation_id(conversation_id)
            if conversation_id
            else None
        )
        now = time.time()
        with self._connection() as conn:
            self._require_active_admin_locked(conn, requester)
            rows = conn.execute(
                "SELECT * FROM a2a_access_grants "
                "WHERE (? IS NULL OR conversation_id = ?) "
                "ORDER BY created_at DESC LIMIT 500",
                (conversation, conversation),
            ).fetchall()
        grants = [self._a2a_grant_payload(row, now=now) for row in rows]
        return {"grants": grants, "count": len(grants)}

    def revoke_a2a_access_grant(
        self,
        *,
        grant_id: str,
        revoked_by_web_user_id: str,
    ) -> dict[str, Any]:
        grant = opaque_id(grant_id, field="grant_id")
        reviewer = opaque_id(
            revoked_by_web_user_id,
            field="revoked_by_web_user_id",
        )
        now = time.time()
        with self._transaction() as conn:
            self._require_active_admin_locked(conn, reviewer)
            row = conn.execute(
                "SELECT * FROM a2a_access_grants WHERE grant_id = ?",
                (grant,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"unknown A2A grant: {grant}")
            conn.execute(
                "UPDATE a2a_access_grants SET revoked_at = COALESCE(revoked_at, ?), "
                "revoked_by_web_user_id = COALESCE(revoked_by_web_user_id, ?) "
                "WHERE grant_id = ?",
                (now, reviewer, grant),
            )
            conn.execute(
                "UPDATE agent_sessions SET revoked_at = COALESCE(revoked_at, ?), "
                "revoked_reason = COALESCE(revoked_reason, 'a2a_grant_revoked'), "
                "cleared_at = COALESCE(cleared_at, ?) WHERE session_id = ?",
                (now, now, str(row["session_id"])),
            )
            updated = conn.execute(
                "SELECT * FROM a2a_access_grants WHERE grant_id = ?",
                (grant,),
            ).fetchone()
        return self._a2a_grant_payload(updated, now=now)

    def _require_a2a_grant_locked(
        self,
        conn: sqlite3.Connection,
        *,
        access_token: str,
        now: float,
    ) -> sqlite3.Row:
        normalized = str(access_token or "").strip()
        if not normalized.startswith("a2a_") or len(normalized) < 40:
            raise AuthenticationError("invalid A2A access token")
        row = conn.execute(
            "SELECT * FROM a2a_access_grants WHERE token_hash = ?",
            (self._secret_hash(normalized),),
        ).fetchone()
        if (
            row is None
            or row["revoked_at"] is not None
            or float(row["expires_at"]) <= now
        ):
            raise AuthenticationError("invalid or expired A2A access token")
        return row

    def create_a2a_room_task(
        self,
        *,
        access_token: str,
        body_text: str,
        context_id: str | None = None,
        target_participant_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        normalized_body = body(body_text)
        normalized_context = str(context_id or "").strip()
        if normalized_context and (
            len(normalized_context) > 256
            or any(ord(character) < 32 for character in normalized_context)
        ):
            raise ValidationError("A2A contextId is invalid")
        now = time.time()
        task_id = f"task_{uuid.uuid4().hex}"
        message_id = f"msg_{uuid.uuid4().hex}"
        with self._transaction() as conn:
            grant = self._require_a2a_grant_locked(
                conn,
                access_token=access_token,
                now=now,
            )
            conversation = str(grant["conversation_id"])
            self._require_active_room(conn, conversation)
            target_kind, targets = self._resolve_task_targets_locked(
                conn,
                conversation_id=conversation,
                requested_participant_ids=target_participant_ids,
            )
            conn.execute(
                """
                INSERT INTO messages
                    (message_id, conversation_id, sender_participant_id,
                     audience_kind, audience_value, message_kind, body,
                     refs_json, mentions_json, wake_all_agents, reply_to,
                     status, authorized_session_id, sender_seat,
                     created_at, updated_at)
                VALUES (?, ?, ?, 'room', ?, 'task', ?, '[]', '[]', 0,
                        NULL, 'open', ?, 'a2a', ?, ?)
                """,
                (
                    message_id,
                    conversation,
                    str(grant["participant_id"]),
                    conversation,
                    normalized_body,
                    str(grant["session_id"]),
                    now,
                    now,
                ),
            )
            message = conn.execute(
                "SELECT * FROM messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            sequence = int(message["sequence"])
            conn.execute(
                """
                INSERT INTO room_tasks
                    (task_id, conversation_id, source_message_id,
                     parent_task_id, issuer_web_user_id,
                     issuer_participant_id, target_kind,
                     target_participant_ids_json, body, status,
                     source_sequence, context_start_sequence,
                     context_end_sequence, created_at, updated_at)
                VALUES (?, ?, ?, NULL, NULL, ?, ?, ?, ?, 'queued',
                        ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    conversation,
                    message_id,
                    str(grant["participant_id"]),
                    target_kind,
                    compact_json(targets),
                    normalized_body,
                    sequence,
                    max(1, sequence - 20),
                    sequence,
                    now,
                    now,
                ),
            )
            context = normalized_context or f"ctx_{uuid.uuid4().hex}"
            conn.execute(
                "INSERT INTO a2a_task_links "
                "(task_id, grant_id, context_id, request_message_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (task_id, str(grant["grant_id"]), context, message_id, now),
            )
            self._create_message_deliveries_locked(conn, message)
            task = conn.execute(
                "SELECT * FROM room_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        result = self._task_payload(task)
        result["context_id"] = context
        result["request_message_id"] = message_id
        return result

    def get_a2a_room_task(
        self,
        *,
        access_token: str,
        task_id: str,
    ) -> dict[str, Any]:
        task = opaque_id(task_id, field="task_id")
        now = time.time()
        with self._connection() as conn:
            grant = self._require_a2a_grant_locked(
                conn,
                access_token=access_token,
                now=now,
            )
            row = conn.execute(
                """
                SELECT room_task.*, link.context_id,
                       link.request_message_id
                FROM room_tasks AS room_task
                JOIN a2a_task_links AS link ON link.task_id = room_task.task_id
                WHERE room_task.task_id = ? AND link.grant_id = ?
                """,
                (task, str(grant["grant_id"])),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"unknown A2A task: {task}")
        result = self._task_payload(row)
        result["context_id"] = str(row["context_id"])
        result["request_message_id"] = str(row["request_message_id"])
        return result

    def cancel_a2a_room_task(
        self,
        *,
        access_token: str,
        task_id: str,
    ) -> dict[str, Any]:
        task = opaque_id(task_id, field="task_id")
        now = time.time()
        with self._transaction() as conn:
            grant = self._require_a2a_grant_locked(
                conn,
                access_token=access_token,
                now=now,
            )
            row = conn.execute(
                """
                SELECT room_task.*, link.context_id,
                       link.request_message_id
                FROM room_tasks AS room_task
                JOIN a2a_task_links AS link ON link.task_id = room_task.task_id
                WHERE room_task.task_id = ? AND link.grant_id = ?
                """,
                (task, str(grant["grant_id"])),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"unknown A2A task: {task}")
            if str(row["status"]) in {"completed", "failed"}:
                raise ConflictError("finished A2A task cannot be cancelled")
            conn.execute(
                "UPDATE room_tasks SET status = 'cancelled', completed_at = ?, "
                "lease_expires_at = NULL, updated_at = ? WHERE task_id = ?",
                (now, now, task),
            )
            updated = conn.execute(
                """
                SELECT room_task.*, link.context_id,
                       link.request_message_id
                FROM room_tasks AS room_task
                JOIN a2a_task_links AS link ON link.task_id = room_task.task_id
                WHERE room_task.task_id = ?
                """,
                (task,),
            ).fetchone()
        result = self._task_payload(updated)
        result["context_id"] = str(updated["context_id"])
        result["request_message_id"] = str(updated["request_message_id"])
        return result
