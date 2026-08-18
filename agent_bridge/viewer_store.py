from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .store import ROOM_ABANDON_AFTER_SECONDS
from .validation import conversation_id as validate_conversation_id
from .viewer_activity_queries import ViewerActivityQueries
from .viewer_message_queries import ViewerMessageQueries


class ViewerRepository(ViewerMessageQueries, ViewerActivityQueries):
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
                    "admin_audit_events",
                    "history_retention_policy",
                    "history_redaction_previews",
                    "history_message_redactions",
                    "bridge_runtime_instances",
                    "bridge_runtime_leases",
                    "shared_request_rate_windows",
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
                    "connector_setup_status": str(row["connector_setup_status"] or ""),
                    "connector_adapter_kind": str(row["connector_adapter_kind"] or ""),
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
            visibility_clause = f"WHERE room.conversation_id IN ({placeholders})"
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
                "allow_global_admin_tasks": bool(row["allow_global_admin_tasks"]),
                "participant_count": int(row["participant_count"] or 0),
                "active_participant_count": int(row["active_participant_count"] or 0),
                "current_participant_count": int(row["current_participant_count"] or 0),
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
