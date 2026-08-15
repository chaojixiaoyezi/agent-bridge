from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .store import CONNECTOR_ONLINE_WINDOW_SECONDS, ROOM_ABANDON_AFTER_SECONDS
from .validation import conversation_id as validate_conversation_id


class ViewerRepository:
    """Read-only administrative projections for the local owner dashboard."""

    def __init__(self, database: str | Path) -> None:
        self.database = Path(database).expanduser().resolve()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        encoded = quote(str(self.database), safe="/:")
        connection = sqlite3.connect(
            f"file:{encoded}?mode=ro",
            uri=True,
            timeout=2.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 2000")
        try:
            yield connection
        finally:
            connection.close()

    def health(self) -> dict[str, Any]:
        with self._connection() as connection:
            counts = {
                table: int(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
                for table in (
                    "rooms",
                    "participants",
                    "memberships",
                    "messages",
                    "receipts",
                    "agent_sessions",
                    "nickname_requests",
                    "follows",
                    "message_deliveries",
                    "web_users",
                    "web_sessions",
                    "agent_invitations",
                    "agent_connectors",
                    "agent_lifecycle_states",
                    "agent_room_blocks",
                    "chat_authorization_grants",
                    "room_task_policies",
                    "room_task_grants",
                    "room_tasks",
                    "room_message_markers",
                    "operational_metric_samples",
                    "operational_alerts",
                )
            }
            room_states = {
                str(row["status"]): int(row["count"])
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM rooms GROUP BY status"
                ).fetchall()
            }
        return {
            "status": "ok",
            "database": str(self.database),
            "counts": counts,
            "room_states": {
                "active": room_states.get("active", 0),
                "abandoned": room_states.get("abandoned", 0),
            },
            "server_time": time.time(),
            "message_view_read_only": False,
            "room_creation_enabled": True,
            "owner_message_enabled": True,
            "open_registration_enabled": True,
            "web_login_required": True,
        }

    def sessions(self, *, limit: int = 200) -> list[dict[str, Any]]:
        normalized_limit = max(1, min(int(limit), 500))
        now = time.time()
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT session.*, participant.client_type,
                       participant.session_alias, participant.display_name,
                       participant.signature,
                       connector.setup_status AS connector_setup_status,
                       connector.connector_last_seen_at,
                       COALESCE(
                           invitation.tui_adapter_kind,
                           invitation.adapter_kind
                       ) AS connector_adapter_kind
                FROM agent_sessions AS session
                JOIN participants AS participant
                  ON participant.participant_id = session.participant_id
                LEFT JOIN agent_connectors AS connector
                  ON connector.connector_id = session.connector_id
                LEFT JOIN agent_invitations AS invitation
                  ON invitation.invitation_id = connector.invitation_id
                WHERE session.cleared_at IS NULL
                ORDER BY session.created_at DESC
                LIMIT ?
                """,
                (normalized_limit,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            if row["revoked_at"] is not None:
                status = "revoked"
            elif float(row["expires_at"]) <= now:
                status = "expired"
            else:
                status = "active"
            result.append(
                {
                    "session_id": str(row["session_id"]),
                    "participant_id": str(row["participant_id"]),
                    "client_type": str(row["client_type"]),
                    "session_alias": str(row["session_alias"]),
                    "display_name": str(row["display_name"]),
                    "signature": str(row["signature"]),
                    "conversation_id": str(row["registered_conversation_id"]),
                    "transport": str(row["transport"]),
                    "status": status,
                    "created_at": float(row["created_at"]),
                    "expires_at": float(row["expires_at"]),
                    "ttl_seconds": float(row["ttl_seconds"]),
                    "renewal_mode": "sliding",
                    "last_seen": float(row["last_seen"]),
                    "revoked_at": (
                        float(row["revoked_at"])
                        if row["revoked_at"] is not None
                        else None
                    ),
                    "revoked_reason": str(row["revoked_reason"] or ""),
                    "cleared_at": None,
                    "connector_id": (
                        str(row["connector_id"])
                        if row["connector_id"] is not None
                        else None
                    ),
                    "connector_setup_status": str(
                        row["connector_setup_status"] or ""
                    ),
                    "connector_adapter_kind": str(
                        row["connector_adapter_kind"] or ""
                    ),
                    "connector_last_seen_at": (
                        float(row["connector_last_seen_at"])
                        if row["connector_last_seen_at"] is not None
                        else None
                    ),
                }
            )
        return result

    def session_stats(self) -> dict[str, int]:
        now = time.time()
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT
                    SUM(
                        CASE WHEN revoked_at IS NULL AND expires_at > ?
                             THEN 1 ELSE 0 END
                    ) AS active_count,
                    SUM(
                        CASE WHEN revoked_at IS NOT NULL OR expires_at <= ?
                             THEN 1 ELSE 0 END
                    ) AS clearable_count
                FROM agent_sessions
                WHERE cleared_at IS NULL
                """,
                (now, now),
            ).fetchone()
        return {
            "active_count": int(row["active_count"] or 0),
            "clearable_count": int(row["clearable_count"] or 0),
        }

    def rooms(
        self,
        *,
        limit: int = 200,
        visible_conversation_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        normalized_limit = max(1, min(int(limit), 500))
        visible = (
            None
            if visible_conversation_ids is None
            else list(
                dict.fromkeys(
                    validate_conversation_id(value)
                    for value in visible_conversation_ids
                )
            )
        )
        if visible == []:
            return []
        visibility_clause = ""
        if visible is not None:
            placeholders = ",".join("?" for _ in visible)
            visibility_clause = (
                f"WHERE room.conversation_id IN ({placeholders})"
            )
        now = time.time()
        online_after = now - 90.0
        connector_online_after = now - 75.0
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                WITH membership_stats AS (
                    SELECT
                        m.conversation_id,
                        COUNT(*) AS participant_count,
                        SUM(CASE WHEN m.active = 1 THEN 1 ELSE 0 END)
                            AS active_participant_count,
                        SUM(
                            CASE
                                WHEN m.active = 1
                                 AND (
                                    EXISTS (
                                        SELECT 1
                                        FROM agent_sessions AS session
                                        WHERE session.participant_id = p.participant_id
                                          AND session.registered_conversation_id = m.conversation_id
                                          AND session.cleared_at IS NULL
                                          AND session.revoked_at IS NULL
                                          AND session.expires_at > ?
                                    )
                                    OR EXISTS (
                                        SELECT 1
                                        FROM web_sessions AS web_session
                                        JOIN web_users AS web_user
                                          ON web_user.user_id = web_session.user_id
                                        WHERE web_user.participant_id = p.participant_id
                                          AND web_user.active = 1
                                          AND web_session.revoked_at IS NULL
                                          AND web_session.expires_at > ?
                                    )
                                 ) THEN 1
                                ELSE 0
                            END
                        ) AS current_participant_count,
                        SUM(
                            CASE
                                WHEN m.active = 1
                                 AND (
                                    EXISTS (
                                        SELECT 1
                                        FROM agent_sessions AS session
                                        WHERE session.participant_id = p.participant_id
                                          AND session.registered_conversation_id = m.conversation_id
                                          AND session.cleared_at IS NULL
                                          AND session.revoked_at IS NULL
                                          AND session.expires_at > ?
                                          AND session.last_seen >= ?
                                    )
                                    OR EXISTS (
                                        SELECT 1
                                        FROM web_sessions AS web_session
                                        JOIN web_users AS web_user
                                          ON web_user.user_id = web_session.user_id
                                        WHERE web_user.participant_id = p.participant_id
                                          AND web_user.active = 1
                                          AND web_session.revoked_at IS NULL
                                          AND web_session.expires_at > ?
                                          AND web_session.last_seen >= ?
                                    )
                                    OR EXISTS (
                                        SELECT 1
                                        FROM agent_connectors AS connector
                                        JOIN agent_invitations AS invitation
                                          ON invitation.invitation_id = connector.invitation_id
                                        WHERE connector.accepted_participant_id = p.participant_id
                                          AND connector.conversation_id = m.conversation_id
                                          AND invitation.status != 'revoked'
                                          AND connector.revoked_at IS NULL
                                          AND connector.setup_status = 'configured'
                                          AND connector.connector_last_seen_at >= ?
                                    )
                                 ) THEN 1
                                ELSE 0
                            END
                        ) AS online_count
                    FROM memberships AS m
                    JOIN participants AS p
                      ON p.participant_id = m.participant_id
                    GROUP BY m.conversation_id
                ),
                message_stats AS (
                    SELECT
                        conversation_id,
                        COUNT(*) AS message_count,
                        MAX(sequence) AS last_sequence,
                        MAX(room_sequence) AS last_room_sequence
                    FROM messages
                    GROUP BY conversation_id
                )
                SELECT
                    room.*,
                    COALESCE(ms.participant_count, 0) AS participant_count,
                    COALESCE(ms.active_participant_count, 0)
                        AS active_participant_count,
                    COALESCE(ms.current_participant_count, 0)
                        AS current_participant_count,
                    CASE
                        WHEN room.status = 'active' THEN COALESCE(ms.online_count, 0)
                        ELSE 0
                    END AS online_count,
                    COALESCE(msgs.message_count, 0) AS message_count,
                    msgs.last_sequence,
                    msgs.last_room_sequence,
                    latest.body AS latest_body,
                    latest.created_at AS latest_created_at,
                    sender.session_alias AS latest_sender_alias,
                    sender.client_type AS latest_sender_client_type,
                    sender.display_name AS latest_sender_display_name,
                    creator.client_type AS creator_client_type,
                    creator.session_alias AS creator_session_alias,
                    creator.display_name AS creator_display_name
                    , ownership.web_user_id AS owner_web_user_id
                    , owner.username AS owner_username
                    , owner.display_name AS owner_display_name
                    , COALESCE(task_policy.allow_global_admin, 0)
                        AS allow_global_admin_tasks
                FROM rooms AS room
                LEFT JOIN membership_stats AS ms
                  ON ms.conversation_id = room.conversation_id
                LEFT JOIN message_stats AS msgs
                  ON msgs.conversation_id = room.conversation_id
                LEFT JOIN messages AS latest
                  ON latest.sequence = msgs.last_sequence
                LEFT JOIN participants AS sender
                  ON sender.participant_id = latest.sender_participant_id
                LEFT JOIN participants AS creator
                  ON creator.participant_id = room.creator_participant_id
                LEFT JOIN room_web_owners AS ownership
                  ON ownership.conversation_id = room.conversation_id
                LEFT JOIN web_users AS owner
                  ON owner.user_id = ownership.web_user_id
                LEFT JOIN room_task_policies AS task_policy
                  ON task_policy.conversation_id = room.conversation_id
                {visibility_clause}
                ORDER BY
                    CASE WHEN room.status = 'active' THEN 0 ELSE 1 END,
                    room.last_activity_at DESC,
                    room.conversation_id
                LIMIT ?
                """,
                (
                    now,
                    now,
                    now,
                    online_after,
                    now,
                    online_after,
                    connector_online_after,
                    *(visible or []),
                    normalized_limit,
                ),
            ).fetchall()
        return [
            {
                "conversation_id": str(row["conversation_id"]),
                "status": str(row["status"]),
                "creator_kind": str(row["creator_kind"]),
                "creator_participant_id": (
                    str(row["creator_participant_id"])
                    if row["creator_participant_id"] is not None
                    else None
                ),
                "creator_client_type": str(row["creator_client_type"] or ""),
                "creator_session_alias": str(row["creator_session_alias"] or ""),
                "creator_display_name": str(row["creator_display_name"] or ""),
                "owner_web_user_id": (
                    str(row["owner_web_user_id"])
                    if row["owner_web_user_id"] is not None
                    else None
                ),
                "owner_username": str(row["owner_username"] or ""),
                "owner_display_name": str(row["owner_display_name"] or ""),
                "allow_global_admin_tasks": bool(
                    row["allow_global_admin_tasks"]
                ),
                "participant_count": int(row["participant_count"] or 0),
                "active_participant_count": int(
                    row["active_participant_count"] or 0
                ),
                "current_participant_count": int(
                    row["current_participant_count"] or 0
                ),
                "online_count": int(row["online_count"] or 0),
                "message_count": int(row["message_count"] or 0),
                "last_sequence": (
                    int(row["last_sequence"])
                    if row["last_sequence"] is not None
                    else None
                ),
                "last_room_sequence": (
                    int(row["last_room_sequence"])
                    if row["last_room_sequence"] is not None
                    else None
                ),
                "latest_body": str(row["latest_body"] or "")[:180],
                "latest_created_at": (
                    float(row["latest_created_at"])
                    if row["latest_created_at"] is not None
                    else None
                ),
                "latest_sender_alias": str(row["latest_sender_alias"] or ""),
                "latest_sender_client_type": str(
                    row["latest_sender_client_type"] or ""
                ),
                "latest_sender_display_name": str(
                    row["latest_sender_display_name"] or ""
                ),
                "created_at": float(row["created_at"]),
                "last_activity_at": float(row["last_activity_at"]),
                "abandoned_at": (
                    float(row["abandoned_at"])
                    if row["abandoned_at"] is not None
                    else None
                ),
                "days_since_activity": max(
                    0.0,
                    (now - float(row["last_activity_at"])) / 86_400.0,
                ),
                "abandon_at": (
                    float(row["last_activity_at"]) + ROOM_ABANDON_AFTER_SECONDS
                    if str(row["status"]) == "active"
                    else None
                ),
            }
            for row in rows
        ]

    def messages(
        self,
        conversation_id: str,
        *,
        limit: int = 300,
        before_sequence: int | None = None,
        after_sequence: int | None = None,
        around_sequence: int | None = None,
    ) -> list[dict[str, Any]]:
        conversation = validate_conversation_id(conversation_id)
        normalized_limit = max(1, min(int(limit), 501))
        supplied_cursors = sum(
            value is not None
            for value in (before_sequence, after_sequence, around_sequence)
        )
        if supplied_cursors > 1:
            raise ValueError(
                "before_sequence, after_sequence, and around_sequence cannot "
                "be used together"
            )
        parameters: list[Any] = [conversation]
        sequence_clause = ""
        order = "DESC"
        if before_sequence is not None:
            sequence_clause = "AND m.sequence < ?"
            parameters.append(int(before_sequence))
        elif after_sequence is not None:
            sequence_clause = "AND m.sequence > ?"
            parameters.append(int(after_sequence))
            order = "ASC"
        elif around_sequence is not None:
            sequence_clause = """
                AND m.message_id IN (
                    SELECT candidate.message_id
                    FROM messages AS candidate
                    WHERE candidate.conversation_id = ?
                    ORDER BY ABS(candidate.sequence - ?), candidate.sequence DESC
                    LIMIT ?
                )
            """
            parameters.extend(
                (conversation, max(0, int(around_sequence)), normalized_limit)
            )
            order = "ASC"
        parameters.append(normalized_limit)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    m.*,
                    sender.session_alias AS sender_alias,
                    sender.client_type AS sender_client_type,
                    sender.display_name AS sender_display_name,
                    sender.signature AS sender_signature,
                    sender.avatar_key AS sender_avatar_key,
                    claimant.session_alias AS claimant_alias,
                    claimant.display_name AS claimant_display_name,
                    source.conversation_id AS forwarded_source_conversation_id,
                    source.sequence AS forwarded_source_sequence,
                    source.room_sequence AS forwarded_source_room_sequence,
                    source_sender.display_name AS forwarded_source_sender_display_name,
                    source_sender.client_type AS forwarded_source_sender_client_type,
                    grant.authority_kind AS authorization_kind,
                    grant.issuer_web_user_id AS authorization_issuer_user_id,
                    grant.issuer_username_snapshot
                        AS authorization_issuer_username,
                    grant.issuer_role_snapshot AS authorization_issuer_role,
                    grant.issuer_participant_id
                        AS authorization_issuer_participant_id,
                    grant.body_sha256 AS authorization_body_sha256,
                    grant.target_kind AS authorization_target_kind,
                    grant.target_participant_ids_json
                        AS authorization_target_participant_ids_json,
                    grant.created_at AS authorization_issued_at,
                    grant.revoked_at AS authorization_revoked_at,
                    grant.revoked_by_web_user_id
                        AS authorization_revoked_by_web_user_id,
                    grant.revocation_reason AS authorization_revocation_reason,
                    task.task_id AS room_task_id,
                    task.parent_task_id AS room_task_parent_id,
                    task.issuer_web_user_id AS room_task_issuer_web_user_id,
                    task.target_kind AS room_task_target_kind,
                    task.target_participant_ids_json
                        AS room_task_target_participant_ids_json,
                    task.status AS room_task_status,
                    task.claimed_by_participant_id
                        AS room_task_claimed_by_participant_id,
                    task.claimed_at AS room_task_claimed_at,
                    task.lease_expires_at AS room_task_lease_expires_at,
                    task.started_at AS room_task_started_at,
                    task.completed_at AS room_task_completed_at,
                    task.result_summary AS room_task_result_summary,
                    task.execution_cwd AS room_task_execution_cwd,
                    task.execution_thread_id AS room_task_execution_thread_id,
                    task.created_at AS room_task_created_at,
                    task.updated_at AS room_task_updated_at,
                    (
                        SELECT COUNT(*) FROM room_task_inputs AS task_input
                        WHERE task_input.source_message_id = m.message_id
                    ) AS body_input_count,
                    (
                        SELECT COUNT(*) FROM room_task_inputs AS task_input
                        WHERE task_input.source_message_id = m.message_id
                          AND task_input.first_delivered_at IS NOT NULL
                    ) AS body_input_delivered_count,
                    (
                        SELECT COUNT(*) FROM room_task_inputs AS task_input
                        WHERE task_input.source_message_id = m.message_id
                          AND task_input.applied_at IS NOT NULL
                    ) AS body_input_applied_count,
                    (
                        SELECT MAX(task_input.last_delivered_at)
                        FROM room_task_inputs AS task_input
                        WHERE task_input.source_message_id = m.message_id
                    ) AS body_input_last_delivered_at,
                    (
                        SELECT MAX(task_input.applied_at)
                        FROM room_task_inputs AS task_input
                        WHERE task_input.source_message_id = m.message_id
                    ) AS body_input_last_applied_at,
                    (
                        SELECT COUNT(*) FROM messages AS reply
                        WHERE reply.reply_to = m.message_id
                    ) AS reply_count,
                    (
                        SELECT COUNT(*) FROM receipts AS r
                        WHERE r.message_id = m.message_id AND r.state = 'acked'
                    ) AS ack_count,
                    (
                        SELECT COUNT(*) FROM message_deliveries AS d
                        WHERE d.message_id = m.message_id
                    ) AS receipt_count
                FROM messages AS m
                JOIN participants AS sender
                  ON sender.participant_id = m.sender_participant_id
                LEFT JOIN participants AS claimant
                  ON claimant.participant_id = m.claimed_by
                LEFT JOIN messages AS source
                  ON source.message_id = m.forwarded_from_message_id
                LEFT JOIN participants AS source_sender
                  ON source_sender.participant_id = source.sender_participant_id
                LEFT JOIN chat_authorization_grants AS grant
                  ON grant.source_message_id = m.message_id
                LEFT JOIN room_tasks AS task
                  ON task.source_message_id = m.message_id
                WHERE m.conversation_id = ? {sequence_clause}
                ORDER BY m.sequence {order}
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        ordered_rows = (
            rows
            if after_sequence is not None or around_sequence is not None
            else reversed(rows)
        )
        result = [self._message_payload(row) for row in ordered_rows]
        return result

    def message_thread(
        self,
        conversation_id: str,
        message_id: str,
        *,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Return one root message plus direct replies in room order."""

        conversation = validate_conversation_id(conversation_id)
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id or len(normalized_message_id) > 200:
            raise ValueError("message_id is invalid")
        normalized_limit = max(2, min(int(limit), 500))
        with self._connection() as connection:
            selected = connection.execute(
                "SELECT message_id, reply_to FROM messages "
                "WHERE conversation_id = ? AND message_id = ?",
                (conversation, normalized_message_id),
            ).fetchone()
            if selected is None:
                raise ValueError("message does not exist in this room")
            root_message_id = str(selected["reply_to"] or selected["message_id"])
            total_reply_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM messages "
                    "WHERE conversation_id = ? AND reply_to = ?",
                    (conversation, root_message_id),
                ).fetchone()[0]
            )
            rows = connection.execute(
                """
                SELECT message.sequence, message.room_sequence,
                       message.message_id, message.reply_to,
                       message.sender_participant_id, message.sender_seat,
                       message.message_kind, message.body, message.created_at,
                       sender.client_type AS sender_client_type,
                       sender.display_name AS sender_display_name,
                       sender.signature AS sender_signature,
                       sender.avatar_key AS sender_avatar_key
                FROM messages AS message
                JOIN participants AS sender
                  ON sender.participant_id = message.sender_participant_id
                WHERE message.conversation_id = ?
                  AND (message.message_id = ? OR message.reply_to = ?)
                ORDER BY message.sequence
                LIMIT ?
                """,
                (
                    conversation,
                    root_message_id,
                    root_message_id,
                    normalized_limit + 1,
                ),
            ).fetchall()
        has_more = total_reply_count + 1 > normalized_limit
        page = rows[:normalized_limit]
        messages = [self._thread_message_payload(row) for row in page]
        return {
            "conversation_id": conversation,
            "root_message_id": root_message_id,
            "messages": messages,
            "reply_count": total_reply_count,
            "has_more": has_more,
        }

    def room_highlights(
        self,
        conversation_id: str,
        *,
        limit: int = 200,
    ) -> dict[str, Any]:
        conversation = validate_conversation_id(conversation_id)
        normalized_limit = max(1, min(int(limit), 500))
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT marker.*, message.sequence, message.room_sequence,
                       message.body, message.message_kind,
                       message.sender_participant_id,
                       message.created_at AS message_created_at,
                       sender.client_type AS sender_client_type,
                       sender.display_name AS sender_display_name,
                       sender.avatar_key AS sender_avatar_key,
                       creator.username AS created_by_username,
                       creator.display_name AS created_by_display_name,
                       updater.username AS updated_by_username,
                       updater.display_name AS updated_by_display_name
                FROM room_message_markers AS marker
                JOIN messages AS message
                  ON message.message_id = marker.message_id
                 AND message.conversation_id = marker.conversation_id
                JOIN participants AS sender
                  ON sender.participant_id = message.sender_participant_id
                JOIN web_users AS creator
                  ON creator.user_id = marker.created_by_web_user_id
                JOIN web_users AS updater
                  ON updater.user_id = marker.updated_by_web_user_id
                WHERE marker.conversation_id = ?
                ORDER BY CASE marker.marker_kind
                             WHEN 'decision' THEN 0 ELSE 1
                         END,
                         marker.updated_at DESC,
                         message.sequence DESC
                LIMIT ?
                """,
                (conversation, normalized_limit),
            ).fetchall()
        items = [self._room_highlight_payload(row) for row in rows]
        return {
            "conversation_id": conversation,
            "items": items,
            "pins": [item for item in items if item["marker_kind"] == "pin"],
            "decisions": [
                item for item in items if item["marker_kind"] == "decision"
            ],
            "count": len(items),
        }

    @staticmethod
    def _thread_message_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "sequence": int(row["sequence"]),
            "room_sequence": int(row["room_sequence"] or row["sequence"]),
            "message_id": str(row["message_id"]),
            "reply_to": str(row["reply_to"]) if row["reply_to"] else None,
            "sender_participant_id": str(row["sender_participant_id"]),
            "sender_seat": str(row["sender_seat"] or "unknown"),
            "sender_client_type": str(row["sender_client_type"]),
            "sender_display_name": str(row["sender_display_name"]),
            "sender_signature": str(row["sender_signature"]),
            "sender_avatar_key": str(row["sender_avatar_key"] or "auto"),
            "message_kind": str(row["message_kind"] or "message"),
            "body": str(row["body"]),
            "created_at": float(row["created_at"]),
        }

    @staticmethod
    def _room_highlight_payload(row: sqlite3.Row) -> dict[str, Any]:
        body = str(row["body"])
        return {
            "conversation_id": str(row["conversation_id"]),
            "message_id": str(row["message_id"]),
            "sequence": int(row["sequence"]),
            "room_sequence": int(row["room_sequence"] or row["sequence"]),
            "marker_kind": str(row["marker_kind"]),
            "note": str(row["note"] or ""),
            "message_kind": str(row["message_kind"] or "message"),
            "body_preview": body[:1_000],
            "body_truncated": len(body) > 1_000,
            "sender_participant_id": str(row["sender_participant_id"]),
            "sender_client_type": str(row["sender_client_type"]),
            "sender_display_name": str(row["sender_display_name"]),
            "sender_avatar_key": str(row["sender_avatar_key"] or "auto"),
            "message_created_at": float(row["message_created_at"]),
            "created_by_web_user_id": str(row["created_by_web_user_id"]),
            "created_by_username": str(row["created_by_username"]),
            "created_by_display_name": str(row["created_by_display_name"]),
            "updated_by_web_user_id": str(row["updated_by_web_user_id"]),
            "updated_by_username": str(row["updated_by_username"]),
            "updated_by_display_name": str(row["updated_by_display_name"]),
            "marker_created_at": float(row["created_at"]),
            "marker_updated_at": float(row["updated_at"]),
        }

    def message_window_bounds(
        self,
        conversation_id: str,
        *,
        first_sequence: int | None,
        last_sequence: int | None,
    ) -> dict[str, bool]:
        conversation = validate_conversation_id(conversation_id)
        if first_sequence is None or last_sequence is None:
            return {"has_earlier": False, "has_later": False}
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT
                    EXISTS(
                        SELECT 1 FROM messages
                        WHERE conversation_id = ? AND sequence < ?
                    ) AS has_earlier,
                    EXISTS(
                        SELECT 1 FROM messages
                        WHERE conversation_id = ? AND sequence > ?
                    ) AS has_later
                """,
                (
                    conversation,
                    int(first_sequence),
                    conversation,
                    int(last_sequence),
                ),
            ).fetchone()
        return {
            "has_earlier": bool(row["has_earlier"]),
            "has_later": bool(row["has_later"]),
        }

    def search_messages(
        self,
        conversation_id: str,
        *,
        query: str = "",
        sender_participant_id: str | None = None,
        message_kind: str | None = None,
        notification_mode: str | None = None,
        thread_scope: str | None = None,
        marker_kind: str | None = None,
        room_sequence: int | None = None,
        created_after: float | None = None,
        created_before: float | None = None,
        before_sequence: int | None = None,
        limit: int = 30,
    ) -> dict[str, Any]:
        """Search one room with composable, server-validated filters."""

        conversation = validate_conversation_id(conversation_id)
        normalized_query = str(query or "").strip()
        normalized_sender = str(sender_participant_id or "").strip() or None
        normalized_message_kind = str(message_kind or "").strip().casefold() or None
        normalized_notification_mode = (
            str(notification_mode or "").strip().casefold() or None
        )
        normalized_thread_scope = str(thread_scope or "").strip().casefold() or None
        normalized_marker_kind = str(marker_kind or "").strip().casefold() or None
        normalized_room_sequence = (
            int(room_sequence) if room_sequence is not None else None
        )
        normalized_created_after = (
            float(created_after) if created_after is not None else None
        )
        normalized_created_before = (
            float(created_before) if created_before is not None else None
        )
        if not any(
            (
                normalized_query,
                normalized_sender,
                normalized_message_kind,
                normalized_notification_mode,
                normalized_thread_scope,
                normalized_marker_kind,
                normalized_room_sequence,
                normalized_created_after is not None,
                normalized_created_before is not None,
            )
        ):
            raise ValueError("at least one room search filter is required")
        if len(normalized_query) > 200:
            raise ValueError("query must be at most 200 characters")
        if normalized_sender is not None and len(normalized_sender) > 200:
            raise ValueError("sender_participant_id is invalid")
        if normalized_message_kind not in {None, "message", "task", "forward"}:
            raise ValueError("message_kind must be message, task, or forward")
        if normalized_notification_mode not in {None, "ordinary", "mention"}:
            raise ValueError("notification_mode must be ordinary or mention")
        if normalized_thread_scope not in {None, "roots", "replies"}:
            raise ValueError("thread_scope must be roots or replies")
        if normalized_marker_kind not in {None, "pin", "decision"}:
            raise ValueError("marker_kind must be pin or decision")
        if normalized_room_sequence is not None and normalized_room_sequence < 1:
            raise ValueError("room_sequence must be a positive integer")
        maximum_timestamp = 253_402_300_799.0
        for label, timestamp in (
            ("created_after", normalized_created_after),
            ("created_before", normalized_created_before),
        ):
            if timestamp is not None and not 0 <= timestamp <= maximum_timestamp:
                raise ValueError(f"{label} is outside the supported range")
        if (
            normalized_created_after is not None
            and normalized_created_before is not None
            and normalized_created_after >= normalized_created_before
        ):
            raise ValueError("created_after must be earlier than created_before")
        normalized_limit = max(1, min(int(limit), 50))
        clauses = ["message.conversation_id = ?"]
        parameters: list[Any] = [conversation]
        if normalized_query:
            clauses.append("instr(lower(message.body), lower(?)) > 0")
            parameters.append(normalized_query)
        if normalized_sender is not None:
            clauses.append("message.sender_participant_id = ?")
            parameters.append(normalized_sender)
        if normalized_message_kind is not None:
            clauses.append("message.message_kind = ?")
            parameters.append(normalized_message_kind)
        if normalized_notification_mode is not None:
            clauses.append("message.notification_mode = ?")
            parameters.append(normalized_notification_mode)
        if normalized_thread_scope == "roots":
            clauses.append("message.reply_to IS NULL")
        elif normalized_thread_scope == "replies":
            clauses.append("message.reply_to IS NOT NULL")
        if normalized_marker_kind is not None:
            clauses.append(
                "EXISTS (SELECT 1 FROM room_message_markers AS marker_filter "
                "WHERE marker_filter.conversation_id = message.conversation_id "
                "AND marker_filter.message_id = message.message_id "
                "AND marker_filter.marker_kind = ?)"
            )
            parameters.append(normalized_marker_kind)
        if normalized_room_sequence is not None:
            clauses.append("message.room_sequence = ?")
            parameters.append(normalized_room_sequence)
        if normalized_created_after is not None:
            clauses.append("message.created_at >= ?")
            parameters.append(normalized_created_after)
        if normalized_created_before is not None:
            clauses.append("message.created_at < ?")
            parameters.append(normalized_created_before)
        if before_sequence is not None:
            clauses.append("message.sequence < ?")
            parameters.append(max(0, int(before_sequence)))
        parameters.append(normalized_limit + 1)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT message.sequence, message.room_sequence,
                       message.message_id,
                       message.sender_participant_id, message.message_kind,
                       message.notification_mode, message.reply_to,
                       message.body, message.created_at,
                       sender.client_type AS sender_client_type,
                       sender.display_name AS sender_display_name,
                       sender.avatar_key AS sender_avatar_key,
                       (
                           SELECT GROUP_CONCAT(marker.marker_kind)
                           FROM room_message_markers AS marker
                           WHERE marker.conversation_id = message.conversation_id
                             AND marker.message_id = message.message_id
                       ) AS marker_kinds
                FROM messages AS message
                JOIN participants AS sender
                  ON sender.participant_id = message.sender_participant_id
                WHERE {' AND '.join(clauses)}
                ORDER BY message.sequence DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        has_more = len(rows) > normalized_limit
        page = rows[:normalized_limit]
        results = []
        for row in page:
            body = str(row["body"])
            results.append(
                {
                    "sequence": int(row["sequence"]),
                    "room_sequence": int(
                        row["room_sequence"] or row["sequence"]
                    ),
                    "message_id": str(row["message_id"]),
                    "sender_participant_id": str(row["sender_participant_id"]),
                    "sender_client_type": str(row["sender_client_type"]),
                    "sender_display_name": str(row["sender_display_name"]),
                    "sender_avatar_key": str(row["sender_avatar_key"] or "auto"),
                    "message_kind": str(row["message_kind"] or "message"),
                    "notification_mode": str(
                        row["notification_mode"] or "ordinary"
                    ),
                    "reply_to": str(row["reply_to"]) if row["reply_to"] else None,
                    "marker_kinds": sorted(
                        value
                        for value in str(row["marker_kinds"] or "").split(",")
                        if value
                    ),
                    "body_preview": body[:500],
                    "body_truncated": len(body) > 500,
                    "created_at": float(row["created_at"]),
                }
            )
        return {
            "conversation_id": conversation,
            "query": normalized_query,
            "sender_participant_id": normalized_sender,
            "filters": {
                "message_kind": normalized_message_kind,
                "notification_mode": normalized_notification_mode,
                "thread_scope": normalized_thread_scope,
                "marker_kind": normalized_marker_kind,
                "room_sequence": normalized_room_sequence,
                "created_after": normalized_created_after,
                "created_before": normalized_created_before,
            },
            "results": results,
            "count": len(results),
            "has_more": has_more,
            "next_before_sequence": (
                int(page[-1]["sequence"]) if has_more and page else None
            ),
        }

    def message_receipts(
        self,
        conversation_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 500,
    ) -> list[dict[str, int | str]]:
        conversation = validate_conversation_id(conversation_id)
        normalized_after = max(0, int(after_sequence))
        normalized_limit = max(1, min(int(limit), 1_000))
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT m.sequence, m.message_id,
                       (
                           SELECT COUNT(*) FROM receipts AS receipt
                           WHERE receipt.message_id = m.message_id
                             AND receipt.state = 'acked'
                       ) AS ack_count,
                       (
                           SELECT COUNT(*) FROM message_deliveries AS delivery
                           WHERE delivery.message_id = m.message_id
                       ) AS receipt_count
                FROM messages AS m
                WHERE m.conversation_id = ? AND m.sequence > ?
                ORDER BY m.sequence DESC
                LIMIT ?
                """,
                (conversation, normalized_after, normalized_limit),
            ).fetchall()
        return [
            {
                "sequence": int(row["sequence"]),
                "message_id": str(row["message_id"]),
                "ack_count": int(row["ack_count"] or 0),
                "receipt_count": int(row["receipt_count"] or 0),
            }
            for row in reversed(rows)
        ]

    def pending_response_center(
        self,
        *,
        participant_id: str,
        visible_conversation_ids: Sequence[str] | None,
        managed_conversation_ids: Sequence[str] | None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Project unresolved required replies and active room tasks.

        Delivery reasons are the authority for whether a chat reply is required.
        A linked reply from the exact target also resolves the projection for Web
        users, whose browser does not consume the Agent delivery queue.
        """

        participant = str(participant_id or "").strip()
        if not participant:
            raise ValueError("participant_id is required")
        normalized_limit = max(1, min(int(limit), 200))
        visible = (
            None
            if visible_conversation_ids is None
            else list(
                dict.fromkeys(
                    validate_conversation_id(value)
                    for value in visible_conversation_ids
                )
            )
        )
        if visible == []:
            return {
                "pending_responses": [],
                "active_tasks": [],
                "counts": {
                    "pending_responses": 0,
                    "incoming": 0,
                    "outgoing": 0,
                    "oversight": 0,
                    "active_tasks": 0,
                    "needs_input_tasks": 0,
                    "total": 0,
                },
                "has_more": False,
            }
        managed = (
            None
            if managed_conversation_ids is None
            else list(
                dict.fromkeys(
                    validate_conversation_id(value)
                    for value in managed_conversation_ids
                )
            )
        )

        room_clauses: list[str] = []
        room_parameters: list[Any] = []
        if visible is not None:
            placeholders = ",".join("?" for _ in visible)
            room_clauses.append(f"message.conversation_id IN ({placeholders})")
            room_parameters.extend(visible)

        access_clauses = [
            "delivery.participant_id = ?",
            "message.sender_participant_id = ?",
        ]
        access_parameters: list[Any] = [participant, participant]
        if managed is None:
            access_clauses.append("1 = 1")
        elif managed:
            placeholders = ",".join("?" for _ in managed)
            access_clauses.append(
                f"message.conversation_id IN ({placeholders})"
            )
            access_parameters.extend(managed)

        response_where = [
            "delivery.state IN ('pending', 'delivered')",
            "(instr(delivery.reasons_json, '\"mention\"') > 0 "
            "OR instr(delivery.reasons_json, '\"agent_request\"') > 0)",
            "exact_reply.reply_to IS NULL",
            f"({' OR '.join(access_clauses)})",
            *room_clauses,
        ]
        response_parameters = [*access_parameters, *room_parameters, normalized_limit]

        task_room_clauses: list[str] = []
        task_room_parameters: list[Any] = []
        if visible is not None:
            placeholders = ",".join("?" for _ in visible)
            task_room_clauses.append(f"task.conversation_id IN ({placeholders})")
            task_room_parameters.extend(visible)
        task_access_clauses = ["task.issuer_participant_id = ?"]
        task_access_parameters: list[Any] = [participant]
        if managed is None:
            task_access_clauses.append("1 = 1")
        elif managed:
            placeholders = ",".join("?" for _ in managed)
            task_access_clauses.append(f"task.conversation_id IN ({placeholders})")
            task_access_parameters.extend(managed)
        task_where = [
            "task.status IN ('queued', 'claimed', 'running', 'needs_input')",
            f"({' OR '.join(task_access_clauses)})",
            *task_room_clauses,
        ]
        task_parameters = [
            *task_access_parameters,
            *task_room_parameters,
            normalized_limit,
        ]

        with self._connection() as connection:
            response_rows = connection.execute(
                f"""
                WITH exact_replies AS (
                    SELECT DISTINCT reply_to, sender_participant_id
                    FROM messages
                    WHERE reply_to IS NOT NULL
                )
                SELECT message.message_id, message.conversation_id,
                       message.sequence, message.room_sequence,
                       message.body, message.created_at,
                       message.sender_participant_id,
                       sender.client_type AS sender_client_type,
                       sender.display_name AS sender_display_name,
                       sender.avatar_key AS sender_avatar_key,
                       delivery.participant_id AS target_participant_id,
                       target.client_type AS target_client_type,
                       target.display_name AS target_display_name,
                       target.avatar_key AS target_avatar_key,
                       delivery.state AS delivery_state,
                       delivery.reasons_json,
                       delivery.first_delivered_at,
                       delivery.last_delivered_at,
                       COUNT(*) OVER() AS total_count
                FROM message_deliveries AS delivery
                JOIN messages AS message
                  ON message.message_id = delivery.message_id
                JOIN participants AS sender
                  ON sender.participant_id = message.sender_participant_id
                JOIN participants AS target
                  ON target.participant_id = delivery.participant_id
                LEFT JOIN exact_replies AS exact_reply
                  ON exact_reply.reply_to = message.message_id
                 AND exact_reply.sender_participant_id = delivery.participant_id
                JOIN rooms AS room
                  ON room.conversation_id = message.conversation_id
                 AND room.status = 'active'
                WHERE {' AND '.join(response_where)}
                ORDER BY message.created_at, message.sequence,
                         delivery.participant_id
                LIMIT ?
                """,
                response_parameters,
            ).fetchall()
            task_rows = connection.execute(
                f"""
                SELECT task.*, source_message.room_sequence
                                   AS source_room_sequence,
                       issuer.client_type AS issuer_client_type,
                       issuer.display_name AS issuer_display_name,
                       claimant.client_type AS claimant_client_type,
                       claimant.display_name AS claimant_display_name,
                       COUNT(*) OVER() AS total_count
                FROM room_tasks AS task
                JOIN rooms AS room
                  ON room.conversation_id = task.conversation_id
                 AND room.status = 'active'
                JOIN participants AS issuer
                  ON issuer.participant_id = task.issuer_participant_id
                LEFT JOIN participants AS claimant
                  ON claimant.participant_id = task.claimed_by_participant_id
                LEFT JOIN messages AS source_message
                  ON source_message.message_id = task.source_message_id
                WHERE {' AND '.join(task_where)}
                ORDER BY CASE task.status
                             WHEN 'needs_input' THEN 0
                             WHEN 'running' THEN 1
                             WHEN 'claimed' THEN 2
                             ELSE 3
                         END,
                         task.updated_at, task.created_at
                LIMIT ?
                """,
                task_parameters,
            ).fetchall()

        now = time.time()
        response_items: list[dict[str, Any]] = []
        direction_counts = {"incoming": 0, "outgoing": 0, "oversight": 0}
        for row in response_rows:
            sender_id = str(row["sender_participant_id"])
            target_id = str(row["target_participant_id"])
            if target_id == participant:
                direction = "incoming"
            elif sender_id == participant:
                direction = "outgoing"
            else:
                direction = "oversight"
            direction_counts[direction] += 1
            body = str(row["body"])
            response_items.append(
                {
                    "message_id": str(row["message_id"]),
                    "conversation_id": str(row["conversation_id"]),
                    "sequence": int(row["sequence"]),
                    "room_sequence": int(
                        row["room_sequence"] or row["sequence"]
                    ),
                    "body_preview": body[:500],
                    "body_truncated": len(body) > 500,
                    "created_at": float(row["created_at"]),
                    "age_seconds": max(0.0, now - float(row["created_at"])),
                    "direction": direction,
                    "sender": {
                        "participant_id": sender_id,
                        "client_type": str(row["sender_client_type"]),
                        "display_name": str(row["sender_display_name"]),
                        "avatar_key": str(row["sender_avatar_key"] or "auto"),
                    },
                    "target": {
                        "participant_id": target_id,
                        "client_type": str(row["target_client_type"]),
                        "display_name": str(row["target_display_name"]),
                        "avatar_key": str(row["target_avatar_key"] or "auto"),
                    },
                    "delivery_state": str(row["delivery_state"]),
                    "delivery_reasons": json.loads(str(row["reasons_json"] or "[]")),
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
                }
            )

        task_items: list[dict[str, Any]] = []
        for row in task_rows:
            body = str(row["body"])
            task_items.append(
                {
                    "task_id": str(row["task_id"]),
                    "conversation_id": str(row["conversation_id"]),
                    "source_message_id": (
                        str(row["source_message_id"])
                        if row["source_message_id"] is not None
                        else None
                    ),
                    "source_sequence": (
                        int(row["source_sequence"])
                        if row["source_sequence"] is not None
                        else None
                    ),
                    "source_room_sequence": (
                        int(row["source_room_sequence"])
                        if row["source_room_sequence"] is not None
                        else None
                    ),
                    "body_preview": body[:500],
                    "body_truncated": len(body) > 500,
                    "status": str(row["status"]),
                    "issuer_participant_id": str(row["issuer_participant_id"]),
                    "issuer_display_name": str(row["issuer_display_name"]),
                    "issuer_client_type": str(row["issuer_client_type"]),
                    "claimed_by_participant_id": (
                        str(row["claimed_by_participant_id"])
                        if row["claimed_by_participant_id"] is not None
                        else None
                    ),
                    "claimant_display_name": str(row["claimant_display_name"] or ""),
                    "claimant_client_type": str(row["claimant_client_type"] or ""),
                    "created_at": float(row["created_at"]),
                    "updated_at": float(row["updated_at"]),
                    "age_seconds": max(0.0, now - float(row["created_at"])),
                }
            )

        response_total = (
            int(response_rows[0]["total_count"]) if response_rows else 0
        )
        task_total = int(task_rows[0]["total_count"]) if task_rows else 0
        # Direction counts above cover the bounded page.  Count exact totals
        # when a page was truncated so the top-bar badge never understates work.
        if response_total > len(response_rows):
            with self._connection() as connection:
                grouped = connection.execute(
                    f"""
                    WITH exact_replies AS (
                        SELECT DISTINCT reply_to, sender_participant_id
                        FROM messages
                        WHERE reply_to IS NOT NULL
                    )
                    SELECT CASE
                               WHEN delivery.participant_id = ? THEN 'incoming'
                               WHEN message.sender_participant_id = ? THEN 'outgoing'
                               ELSE 'oversight'
                           END AS direction,
                           COUNT(*) AS count
                    FROM message_deliveries AS delivery
                    JOIN messages AS message
                      ON message.message_id = delivery.message_id
                    LEFT JOIN exact_replies AS exact_reply
                      ON exact_reply.reply_to = message.message_id
                     AND exact_reply.sender_participant_id = delivery.participant_id
                    JOIN rooms AS room
                      ON room.conversation_id = message.conversation_id
                     AND room.status = 'active'
                    WHERE {' AND '.join(response_where)}
                    GROUP BY direction
                    """,
                    [participant, participant, *access_parameters, *room_parameters],
                ).fetchall()
            direction_counts = {
                "incoming": 0,
                "outgoing": 0,
                "oversight": 0,
            }
            for row in grouped:
                direction_counts[str(row["direction"])] = int(row["count"])

        needs_input_tasks = sum(
            1 for item in task_items if item["status"] == "needs_input"
        )
        if task_total > len(task_rows):
            with self._connection() as connection:
                needs_input_tasks = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM room_tasks AS task "
                        f"JOIN rooms AS room ON room.conversation_id = "
                        f"task.conversation_id AND room.status = 'active' "
                        f"WHERE {' AND '.join(task_where)} "
                        "AND task.status = 'needs_input'",
                        [*task_access_parameters, *task_room_parameters],
                    ).fetchone()[0]
                )

        return {
            "pending_responses": response_items,
            "active_tasks": task_items,
            "counts": {
                "pending_responses": response_total,
                **direction_counts,
                "active_tasks": task_total,
                "needs_input_tasks": needs_input_tasks,
                "total": response_total + task_total,
            },
            "has_more": (
                response_total > len(response_items)
                or task_total > len(task_items)
            ),
        }

    def event_snapshot(
        self,
        *,
        after_sequence: int = 0,
        visible_conversation_ids: Sequence[str] | None = None,
        include_admin_state: bool = True,
    ) -> dict[str, Any]:
        requested_cursor = max(0, int(after_sequence))
        visible = (
            None
            if visible_conversation_ids is None
            else {
                validate_conversation_id(value)
                for value in visible_conversation_ids
            }
        )
        now = time.time()
        with self._connection() as connection:
            global_sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) FROM messages"
                ).fetchone()[0]
            )
            cursor = min(requested_cursor, global_sequence)
            changed_rooms = [
                {
                    "conversation_id": str(row["conversation_id"]),
                    "message_count": int(row["message_count"]),
                    "first_sequence": int(row["first_sequence"]),
                    "last_sequence": int(row["last_sequence"]),
                }
                for row in connection.execute(
                    """
                    SELECT conversation_id, COUNT(*) AS message_count,
                           MIN(sequence) AS first_sequence,
                           MAX(sequence) AS last_sequence
                    FROM messages
                    WHERE sequence > ?
                    GROUP BY conversation_id
                    ORDER BY first_sequence
                    """,
                    (cursor,),
                ).fetchall()
                if visible is None or str(row["conversation_id"]) in visible
            ]
            if visible is None:
                visible_message_revision = global_sequence
            elif not visible:
                visible_message_revision = 0
            else:
                placeholders = ",".join("?" for _ in visible)
                visible_message_revision = int(
                    connection.execute(
                        f"SELECT COALESCE(MAX(sequence), 0) FROM messages "
                        f"WHERE conversation_id IN ({placeholders})",
                        sorted(visible),
                    ).fetchone()[0]
                )
            pending_nicknames = int(
                connection.execute(
                    "SELECT COUNT(*) FROM nickname_requests WHERE status = 'pending'"
                ).fetchone()[0]
            )
            nickname_revision = float(
                connection.execute(
                    "SELECT COALESCE(MAX(MAX(requested_at, "
                    "COALESCE(reviewed_at, 0))), 0) FROM nickname_requests"
                ).fetchone()[0]
            )
            participant_revision = float(
                connection.execute(
                    "SELECT COALESCE(MAX(profile_updated_at), 0) FROM participants"
                ).fetchone()[0]
            )
            membership_revision = float(
                connection.execute(
                    "SELECT COALESCE(MAX(updated_at), 0) FROM memberships"
                ).fetchone()[0]
            )
            online_revision = str(
                connection.execute(
                    "SELECT COALESCE(GROUP_CONCAT(participant_id, '|'), '') "
                    "FROM (SELECT participant_id FROM participants "
                    "WHERE status = 'online' AND last_seen >= ? "
                    "ORDER BY participant_id)",
                    (now - 90.0,),
                ).fetchone()[0]
            )
            active_session_revision = str(
                connection.execute(
                    "SELECT COALESCE(GROUP_CONCAT(session_key, '|'), '') FROM ("
                    "SELECT 'agent:' || session_id AS session_key "
                    "FROM agent_sessions WHERE cleared_at IS NULL "
                    "AND revoked_at IS NULL AND expires_at > ? "
                    "UNION ALL "
                    "SELECT 'web:' || web_session.session_id AS session_key "
                    "FROM web_sessions AS web_session "
                    "JOIN web_users AS web_user ON web_user.user_id = web_session.user_id "
                    "WHERE web_user.active = 1 AND web_session.revoked_at IS NULL "
                    "AND web_session.expires_at > ? "
                    "ORDER BY session_key)",
                    (now, now),
                ).fetchone()[0]
            )
            connector_revision = str(
                connection.execute(
                    "SELECT COALESCE(GROUP_CONCAT(connector_state, '|'), '') FROM ("
                    "SELECT 'invite:' || invitation_id || ':' || status || ':' || "
                    "CAST(use_count AS TEXT) || ':' || CAST(updated_at AS TEXT) "
                    "AS connector_state FROM agent_invitations "
                    "UNION ALL "
                    "SELECT 'connector:' || connector_id || ':' || conversation_id || ':' || "
                    "setup_status || ':' || "
                    "CASE WHEN connector_last_seen_at >= ? THEN 'online' "
                    "ELSE 'offline' END || ':' || "
                    "COALESCE(tui_state, 'unbound') || ':' || "
                    "CASE WHEN tui_last_seen_at >= ? THEN 'fresh' "
                    "ELSE 'stale' END || ':' || "
                    "COALESCE(tui_active_task_id, '') || ':' || "
                    "COALESCE(CAST(revoked_at AS TEXT), '') AS connector_state "
                    "FROM agent_connectors ORDER BY connector_state)",
                    (
                        now - CONNECTOR_ONLINE_WINDOW_SECONDS,
                        now - CONNECTOR_ONLINE_WINDOW_SECONDS,
                    ),
                ).fetchone()[0]
            )
            session_revocation_revision = float(
                connection.execute(
                    "SELECT COALESCE(MAX(COALESCE(revoked_at, 0)), 0) "
                    "FROM agent_sessions"
                ).fetchone()[0]
            )
            session_clear_revision = float(
                connection.execute(
                    "SELECT COALESCE(MAX(COALESCE(cleared_at, 0)), 0) "
                    "FROM agent_sessions"
                ).fetchone()[0]
            )
            room_revision = float(
                connection.execute(
                    "SELECT COALESCE(MAX(MAX(last_activity_at, "
                    "COALESCE(abandoned_at, 0))), 0) FROM rooms"
                ).fetchone()[0]
            )
            rate_revision = int(
                connection.execute(
                    "SELECT revision FROM message_rate_state WHERE singleton = 1"
                ).fetchone()[0]
            )
            web_user_permission_revision = float(
                connection.execute(
                    "SELECT MAX(revision) FROM ("
                    "SELECT COALESCE(MAX(updated_at), 0) AS revision FROM web_users "
                    "UNION ALL SELECT COALESCE(MAX(updated_at), 0) "
                    "FROM room_web_members)"
                ).fetchone()[0]
                or 0
            )
            task_revision = float(
                connection.execute(
                    "SELECT COALESCE(MAX(updated_at), 0) FROM room_tasks"
                ).fetchone()[0]
            )
            task_permission_revision = float(
                connection.execute(
                    "SELECT MAX(revision) FROM ("
                    "SELECT COALESCE(MAX(updated_at), 0) AS revision "
                    "FROM room_task_policies "
                    "UNION ALL SELECT COALESCE(MAX(updated_at), 0) "
                    "FROM room_task_grants)"
                ).fetchone()[0]
                or 0
            )
            receipt_revision = float(
                connection.execute(
                    "SELECT COALESCE(MAX(acked_at), 0) FROM receipts"
                ).fetchone()[0]
            )
            if visible is None:
                highlight_revision: object = str(
                    connection.execute(
                        "SELECT CAST(COUNT(*) AS TEXT) || ':' || "
                        "CAST(COALESCE(MAX(updated_at), 0) AS TEXT) "
                        "FROM room_message_markers"
                    ).fetchone()[0]
                )
            elif not visible:
                highlight_revision = "0:0"
            else:
                placeholders = ",".join("?" for _ in visible)
                highlight_revision = str(
                    connection.execute(
                        f"SELECT CAST(COUNT(*) AS TEXT) || ':' || "
                        f"CAST(COALESCE(MAX(updated_at), 0) AS TEXT) "
                        f"FROM room_message_markers "
                        f"WHERE conversation_id IN ({placeholders})",
                        sorted(visible),
                    ).fetchone()[0]
                )
            monitoring_revision = int(
                connection.execute(
                    "SELECT revision FROM operational_monitoring_state "
                    "WHERE singleton = 1"
                ).fetchone()[0]
            )
        if not include_admin_state:
            pending_nicknames = 0
            nickname_revision = 0.0

            def private_revision(label: str, value: object) -> str:
                encoded = json.dumps(
                    [label, value],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                return hashlib.sha256(encoded).hexdigest()[:20]

            participant_revision = private_revision(
                "participants",
                participant_revision,
            )
            membership_revision = private_revision(
                "memberships",
                membership_revision,
            )
            online_revision = private_revision("online", online_revision)
            active_session_revision = private_revision(
                "sessions",
                active_session_revision,
            )
            session_revocation_revision = private_revision(
                "session-revocations",
                session_revocation_revision,
            )
            session_clear_revision = private_revision(
                "session-clears",
                session_clear_revision,
            )
            room_revision = private_revision("rooms", room_revision)
            connector_revision = private_revision(
                "connectors",
                connector_revision,
            )
            web_user_permission_revision = private_revision(
                "permissions",
                web_user_permission_revision,
            )
            task_revision = private_revision("tasks", task_revision)
            task_permission_revision = private_revision(
                "task-permissions",
                task_permission_revision,
            )
            receipt_revision = private_revision("receipts", receipt_revision)
            highlight_revision = private_revision(
                "highlights",
                highlight_revision,
            )
            monitoring_revision = 0
            combined_task_revision: object = private_revision(
                "task-state",
                [task_revision, task_permission_revision],
            )
        else:
            combined_task_revision = max(
                task_revision,
                task_permission_revision,
            )
        state_revisions = {
            "messages": visible_message_revision,
            "nicknames": nickname_revision,
            "participants": participant_revision,
            "memberships": membership_revision,
            "online": online_revision,
            "sessions": [
                active_session_revision,
                session_revocation_revision,
                session_clear_revision,
            ],
            "rooms": room_revision,
            "connectors": connector_revision,
            "permissions": web_user_permission_revision,
            "tasks": task_revision,
            "task_permissions": task_permission_revision,
            "receipts": receipt_revision,
            "highlights": highlight_revision,
            "rates": rate_revision,
            "monitoring": monitoring_revision,
        }
        return {
            "cursor": max(cursor, global_sequence),
            "changed_rooms": changed_rooms,
            "pending_nickname_requests": pending_nicknames,
            # Keep the positional revision for older Web clients while giving
            # newer clients named facets they can refresh independently.
            "state_revisions": state_revisions,
            "state_revision": [
                visible_message_revision,
                nickname_revision,
                participant_revision,
                membership_revision,
                online_revision,
                active_session_revision,
                session_revocation_revision,
                session_clear_revision,
                room_revision,
                connector_revision,
                web_user_permission_revision,
                combined_task_revision,
                rate_revision,
                receipt_revision,
                highlight_revision,
                monitoring_revision,
            ],
            "server_time": now,
        }

    def participants(self, conversation_id: str) -> list[dict[str, Any]]:
        conversation = validate_conversation_id(conversation_id)
        now = time.time()
        online_after = now - 90.0
        connector_online_after = now - 75.0
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    p.*,
                    m.roles_json,
                    m.joined_at,
                    m.active AS membership_active,
                    room.status AS room_status,
                    connector.connector_id,
                    COALESCE(
                        invitation.tui_adapter_kind,
                        invitation.adapter_kind
                    ) AS connector_adapter_kind,
                    connector.setup_status AS connector_setup_status,
                    connector.connector_last_seen_at,
                    connector.tui_endpoint_id,
                    connector.tui_native_session_id,
                    connector.tui_state,
                    connector.tui_last_seen_at,
                    connector.tui_active_task_id,
                    CASE
                        WHEN lifecycle.participant_id IS NOT NULL THEN
                            MAX(
                                COALESCE(
                                    lifecycle.access_granted_at,
                                    m.joined_at
                                ),
                                COALESCE(
                                    lifecycle.last_spoke_at,
                                    lifecycle.access_granted_at,
                                    m.joined_at
                                )
                            ) + (
                                CASE
                                    WHEN lifecycle.last_spoke_at IS NULL
                                     AND NOT EXISTS (
                                         SELECT 1
                                         FROM agent_sessions AS life_session
                                         WHERE life_session.participant_id =
                                               p.participant_id
                                           AND life_session.cleared_at IS NULL
                                           AND life_session.revoked_at IS NULL
                                           AND life_session.expires_at > ?
                                     )
                                     AND NOT EXISTS (
                                         SELECT 1
                                         FROM agent_connectors AS life_connector
                                         WHERE life_connector.accepted_participant_id =
                                               p.participant_id
                                           AND life_connector.revoked_at IS NULL
                                           AND life_connector.setup_status = 'configured'
                                           AND COALESCE(
                                               life_connector.connector_last_seen_at,
                                               0
                                           ) >= ?
                                     )
                                    THEN policy.unactivated_inactivity_days
                                    ELSE policy.inactivity_days
                                END
                            ) * 86400.0
                        ELSE NULL
                    END AS inactivity_expires_at,
                    (
                        SELECT COUNT(*) FROM agent_sessions AS session
                        WHERE session.participant_id = p.participant_id
                          AND session.registered_conversation_id = m.conversation_id
                          AND session.cleared_at IS NULL
                          AND session.revoked_at IS NULL
                          AND session.expires_at > ?
                    ) AS active_agent_session_count,
                    (
                        SELECT COUNT(*) FROM agent_sessions AS session
                        WHERE session.participant_id = p.participant_id
                          AND session.registered_conversation_id = m.conversation_id
                          AND session.cleared_at IS NULL
                          AND session.revoked_at IS NULL
                          AND session.expires_at > ?
                          AND session.last_seen >= ?
                    ) AS online_agent_session_count,
                    (
                        SELECT COUNT(*)
                        FROM web_sessions AS web_session
                        JOIN web_users AS web_user
                          ON web_user.user_id = web_session.user_id
                        WHERE web_user.participant_id = p.participant_id
                          AND web_user.active = 1
                          AND web_session.revoked_at IS NULL
                          AND web_session.expires_at > ?
                    ) AS active_web_session_count,
                    (
                        SELECT COUNT(*)
                        FROM web_sessions AS web_session
                        JOIN web_users AS web_user
                          ON web_user.user_id = web_session.user_id
                        WHERE web_user.participant_id = p.participant_id
                          AND web_user.active = 1
                          AND web_session.revoked_at IS NULL
                          AND web_session.expires_at > ?
                          AND web_session.last_seen >= ?
                    ) AS online_web_session_count
                FROM memberships AS m
                JOIN participants AS p
                  ON p.participant_id = m.participant_id
                JOIN rooms AS room
                  ON room.conversation_id = m.conversation_id
                LEFT JOIN agent_connectors AS connector
                  ON connector.connector_id = (
                    SELECT recent.connector_id
                    FROM agent_connectors AS recent
                    JOIN agent_invitations AS recent_invitation
                      ON recent_invitation.invitation_id = recent.invitation_id
                    WHERE recent.accepted_participant_id = p.participant_id
                      AND recent.conversation_id = m.conversation_id
                      AND recent_invitation.status != 'revoked'
                      AND recent.revoked_at IS NULL
                    ORDER BY recent.updated_at DESC
                    LIMIT 1
                  )
                LEFT JOIN agent_invitations AS invitation
                  ON invitation.invitation_id = connector.invitation_id
                LEFT JOIN agent_lifecycle_states AS lifecycle
                  ON lifecycle.participant_id = p.participant_id
                JOIN agent_lifecycle_policy AS policy
                  ON policy.singleton = 1
                WHERE m.conversation_id = ?
                ORDER BY
                    CASE
                        WHEN room.status = 'active'
                         AND m.active = 1
                         AND (
                            online_agent_session_count > 0
                            OR online_web_session_count > 0
                            OR (
                                connector.setup_status = 'configured'
                                AND connector.connector_last_seen_at >= ?
                            )
                         ) THEN 0
                        ELSE 1
                    END,
                    p.display_name,
                    p.participant_id
                """,
                (
                    now,
                    connector_online_after,
                    now,
                    now,
                    online_after,
                    now,
                    now,
                    online_after,
                    conversation,
                    connector_online_after,
                ),
            ).fetchall()
        return [
            {
                "participant_id": str(row["participant_id"]),
                "client_type": str(row["client_type"]),
                "session_alias": str(row["session_alias"]),
                "display_name": str(row["display_name"]),
                "signature": str(row["signature"]),
                "avatar_key": str(row["avatar_key"] or "auto"),
                "roles": json.loads(str(row["roles_json"])),
                "capabilities": json.loads(str(row["capabilities_json"])),
                "status": (
                    "online"
                    if str(row["room_status"]) == "active"
                    and int(row["membership_active"]) == 1
                    and (
                        int(row["online_agent_session_count"] or 0) > 0
                        or int(row["online_web_session_count"] or 0) > 0
                        or (
                            str(row["connector_setup_status"] or "") == "configured"
                            and row["connector_last_seen_at"] is not None
                            and float(row["connector_last_seen_at"])
                            >= connector_online_after
                        )
                    )
                    else "offline"
                ),
                "membership_active": bool(row["membership_active"]),
                "room_status": str(row["room_status"]),
                "last_seen": float(row["last_seen"]),
                "joined_at": float(row["joined_at"]),
                "inactivity_expires_at": (
                    float(row["inactivity_expires_at"])
                    if row["inactivity_expires_at"] is not None
                    else None
                ),
                "active_session_count": int(
                    row["active_agent_session_count"] or 0
                )
                + int(row["active_web_session_count"] or 0),
                "connector_id": (
                    str(row["connector_id"])
                    if row["connector_id"] is not None
                    else None
                ),
                "connector_adapter_kind": str(
                    row["connector_adapter_kind"] or ""
                ),
                "connector_setup_status": str(
                    row["connector_setup_status"] or ""
                ),
                "connector_last_seen_at": (
                    float(row["connector_last_seen_at"])
                    if row["connector_last_seen_at"] is not None
                    else None
                ),
                "native_tui": {
                    "endpoint_id": (
                        str(row["tui_endpoint_id"])
                        if row["tui_endpoint_id"] is not None
                        else None
                    ),
                    "native_session_id": (
                        str(row["tui_native_session_id"])
                        if row["tui_native_session_id"] is not None
                        else None
                    ),
                    "state": (
                        "offline"
                        if str(row["tui_state"] or "unbound") in {"online", "busy"}
                        and (
                            row["tui_last_seen_at"] is None
                            or float(row["tui_last_seen_at"]) < connector_online_after
                        )
                        else str(row["tui_state"] or "unbound")
                    ),
                    "last_seen_at": (
                        float(row["tui_last_seen_at"])
                        if row["tui_last_seen_at"] is not None
                        else None
                    ),
                    "active_task_id": (
                        str(row["tui_active_task_id"])
                        if row["tui_active_task_id"] is not None
                        else None
                    ),
                },
                "resident_status": (
                    "online"
                    if str(row["connector_setup_status"] or "") == "configured"
                    and row["connector_last_seen_at"] is not None
                    and float(row["connector_last_seen_at"])
                    >= connector_online_after
                    else (
                        "offline"
                        if str(row["connector_setup_status"] or "") == "configured"
                        else str(row["connector_setup_status"] or "none")
                    )
                ),
            }
            for row in rows
            if str(row["room_status"]) != "active"
            or int(row["membership_active"]) == 1
        ]

    @staticmethod
    def _message_payload(row: sqlite3.Row) -> dict[str, Any]:
        keys = set(row.keys())
        payload = {
            "sequence": int(row["sequence"]),
            "room_sequence": (
                int(row["room_sequence"])
                if "room_sequence" in keys and row["room_sequence"] is not None
                else int(row["sequence"])
            ),
            "message_id": str(row["message_id"]),
            "conversation_id": str(row["conversation_id"]),
            "sender_participant_id": str(row["sender_participant_id"]),
            "sender_seat": str(row["sender_seat"] or "unknown"),
            "sender_alias": str(row["sender_alias"]),
            "sender_client_type": str(row["sender_client_type"]),
            "sender_display_name": str(row["sender_display_name"]),
            "sender_signature": str(row["sender_signature"]),
            "sender_avatar_key": str(row["sender_avatar_key"] or "auto"),
            "audience_kind": str(row["audience_kind"]),
            "audience_value": str(row["audience_value"]),
            "message_kind": str(row["message_kind"] or "message"),
            "body": str(row["body"]),
            "refs": json.loads(str(row["refs_json"])),
            "mentions": json.loads(str(row["mentions_json"] or "[]")),
            "wake_all_agents": bool(row["wake_all_agents"]),
            "reply_to": str(row["reply_to"]) if row["reply_to"] else None,
            "status": str(row["status"]),
            "claimed_by": str(row["claimed_by"]) if row["claimed_by"] else None,
            "claimant_alias": str(row["claimant_alias"] or ""),
            "claimant_display_name": str(row["claimant_display_name"] or ""),
            "claim_until": (
                float(row["claim_until"]) if row["claim_until"] else None
            ),
            "ack_count": int(row["ack_count"] or 0),
            "receipt_count": int(row["receipt_count"] or 0),
            "created_at": float(row["created_at"]),
            "thread_root_message_id": str(row["reply_to"] or row["message_id"]),
            "reply_count": int(row["reply_count"] or 0),
        }
        if row["forwarded_from_message_id"] is not None:
            payload["forwarded_from"] = {
                "message_id": str(row["forwarded_from_message_id"]),
                "conversation_id": str(row["forwarded_source_conversation_id"]),
                "sequence": int(row["forwarded_source_sequence"]),
                "room_sequence": int(
                    row["forwarded_source_room_sequence"]
                    or row["forwarded_source_sequence"]
                ),
                "sender_display_name": str(
                    row["forwarded_source_sender_display_name"] or ""
                ),
                "sender_client_type": str(
                    row["forwarded_source_sender_client_type"] or ""
                ),
            }
        if row["authorization_kind"] is not None:
            revoked_at = (
                float(row["authorization_revoked_at"])
                if row["authorization_revoked_at"] is not None
                else None
            )
            payload["authorization"] = {
                "kind": str(row["authorization_kind"]),
                "source_message_id": str(row["message_id"]),
                "issuer_user_id": str(row["authorization_issuer_user_id"]),
                "issuer_username": str(row["authorization_issuer_username"]),
                "issuer_role_at_send": str(row["authorization_issuer_role"]),
                "issuer_participant_id": str(
                    row["authorization_issuer_participant_id"]
                ),
                "body_sha256": str(row["authorization_body_sha256"]),
                "target_kind": str(row["authorization_target_kind"]),
                "target_participant_ids": json.loads(
                    str(row["authorization_target_participant_ids_json"] or "[]")
                ),
                "issued_at": float(row["authorization_issued_at"]),
                "status": (
                    "legacy_frozen"
                    if str(row["authorization_kind"]) == "legacy_frozen"
                    else ("revoked" if revoked_at is not None else "active")
                ),
                "revoked_at": revoked_at,
                "revoked_by_web_user_id": (
                    str(row["authorization_revoked_by_web_user_id"])
                    if row["authorization_revoked_by_web_user_id"] is not None
                    else None
                ),
                "revocation_reason": (
                    str(row["authorization_revocation_reason"])
                    if row["authorization_revocation_reason"] is not None
                    else None
                ),
                "semantics": (
                    "ordinary_chat_only"
                    if str(row["authorization_kind"]) == "legacy_frozen"
                    else "natural_language_minimum_necessary"
                ),
            }
        if row["room_task_id"] is not None:
            payload["task"] = {
                "task_id": str(row["room_task_id"]),
                "parent_task_id": (
                    str(row["room_task_parent_id"])
                    if row["room_task_parent_id"] is not None
                    else None
                ),
                "issuer_web_user_id": str(row["room_task_issuer_web_user_id"]),
                "target_kind": str(row["room_task_target_kind"]),
                "target_participant_ids": json.loads(
                    str(row["room_task_target_participant_ids_json"] or "[]")
                ),
                "status": str(row["room_task_status"]),
                "claimed_by_participant_id": (
                    str(row["room_task_claimed_by_participant_id"])
                    if row["room_task_claimed_by_participant_id"] is not None
                    else None
                ),
                "claimed_at": (
                    float(row["room_task_claimed_at"])
                    if row["room_task_claimed_at"] is not None
                    else None
                ),
                "lease_expires_at": (
                    float(row["room_task_lease_expires_at"])
                    if row["room_task_lease_expires_at"] is not None
                    else None
                ),
                "started_at": (
                    float(row["room_task_started_at"])
                    if row["room_task_started_at"] is not None
                    else None
                ),
                "completed_at": (
                    float(row["room_task_completed_at"])
                    if row["room_task_completed_at"] is not None
                    else None
                ),
                "result_summary": (
                    str(row["room_task_result_summary"])
                    if row["room_task_result_summary"] is not None
                    else None
                ),
                "execution_cwd": (
                    str(row["room_task_execution_cwd"])
                    if row["room_task_execution_cwd"] is not None
                    else None
                ),
                "execution_thread_id": (
                    str(row["room_task_execution_thread_id"])
                    if row["room_task_execution_thread_id"] is not None
                    else None
                ),
                "created_at": float(row["room_task_created_at"]),
                "updated_at": float(row["room_task_updated_at"]),
            }
        body_input_count = int(row["body_input_count"] or 0)
        if body_input_count:
            payload["body_delivery"] = {
                "count": body_input_count,
                "delivered_count": int(row["body_input_delivered_count"] or 0),
                "applied_count": int(row["body_input_applied_count"] or 0),
                "last_delivered_at": (
                    float(row["body_input_last_delivered_at"])
                    if row["body_input_last_delivered_at"] is not None
                    else None
                ),
                "last_applied_at": (
                    float(row["body_input_last_applied_at"])
                    if row["body_input_last_applied_at"] is not None
                    else None
                ),
            }
        return payload
