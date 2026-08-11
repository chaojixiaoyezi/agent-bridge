from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

from .store import ROOM_ABANDON_AFTER_SECONDS
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
        }

    def sessions(self, *, limit: int = 200) -> list[dict[str, Any]]:
        normalized_limit = max(1, min(int(limit), 500))
        now = time.time()
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT session.*, participant.client_type,
                       participant.session_alias
                FROM agent_sessions AS session
                JOIN participants AS participant
                  ON participant.participant_id = session.participant_id
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
                    "conversation_id": str(row["registered_conversation_id"]),
                    "transport": str(row["transport"]),
                    "status": status,
                    "created_at": float(row["created_at"]),
                    "expires_at": float(row["expires_at"]),
                    "last_seen": float(row["last_seen"]),
                    "revoked_at": (
                        float(row["revoked_at"])
                        if row["revoked_at"] is not None
                        else None
                    ),
                    "revoked_reason": str(row["revoked_reason"] or ""),
                }
            )
        return result

    def rooms(self, *, limit: int = 200) -> list[dict[str, Any]]:
        normalized_limit = max(1, min(int(limit), 500))
        now = time.time()
        online_after = now - 90.0
        with self._connection() as connection:
            rows = connection.execute(
                """
                WITH membership_stats AS (
                    SELECT
                        m.conversation_id,
                        COUNT(*) AS participant_count,
                        SUM(CASE WHEN m.active = 1 THEN 1 ELSE 0 END)
                            AS active_participant_count,
                        SUM(
                            CASE
                                WHEN m.active = 1
                                 AND p.status = 'online'
                                 AND p.last_seen >= ? THEN 1
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
                        MAX(sequence) AS last_sequence
                    FROM messages
                    GROUP BY conversation_id
                )
                SELECT
                    room.*,
                    COALESCE(ms.participant_count, 0) AS participant_count,
                    COALESCE(ms.active_participant_count, 0)
                        AS active_participant_count,
                    CASE
                        WHEN room.status = 'active' THEN COALESCE(ms.online_count, 0)
                        ELSE 0
                    END AS online_count,
                    COALESCE(msgs.message_count, 0) AS message_count,
                    msgs.last_sequence,
                    latest.body AS latest_body,
                    latest.created_at AS latest_created_at,
                    sender.session_alias AS latest_sender_alias,
                    sender.client_type AS latest_sender_client_type,
                    creator.client_type AS creator_client_type,
                    creator.session_alias AS creator_session_alias
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
                ORDER BY
                    CASE WHEN room.status = 'active' THEN 0 ELSE 1 END,
                    room.last_activity_at DESC,
                    room.conversation_id
                LIMIT ?
                """,
                (online_after, normalized_limit),
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
                "participant_count": int(row["participant_count"] or 0),
                "active_participant_count": int(
                    row["active_participant_count"] or 0
                ),
                "online_count": int(row["online_count"] or 0),
                "message_count": int(row["message_count"] or 0),
                "last_sequence": (
                    int(row["last_sequence"])
                    if row["last_sequence"] is not None
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
    ) -> list[dict[str, Any]]:
        conversation = validate_conversation_id(conversation_id)
        normalized_limit = max(1, min(int(limit), 500))
        parameters: list[Any] = [conversation]
        before_clause = ""
        if before_sequence is not None:
            before_clause = "AND m.sequence < ?"
            parameters.append(int(before_sequence))
        parameters.append(normalized_limit)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    m.*,
                    sender.session_alias AS sender_alias,
                    sender.client_type AS sender_client_type,
                    claimant.session_alias AS claimant_alias,
                    (
                        SELECT COUNT(*) FROM receipts AS r
                        WHERE r.message_id = m.message_id AND r.state = 'acked'
                    ) AS ack_count,
                    (
                        SELECT COUNT(*) FROM receipts AS r
                        WHERE r.message_id = m.message_id
                    ) AS receipt_count
                FROM messages AS m
                JOIN participants AS sender
                  ON sender.participant_id = m.sender_participant_id
                LEFT JOIN participants AS claimant
                  ON claimant.participant_id = m.claimed_by
                WHERE m.conversation_id = ? {before_clause}
                ORDER BY m.sequence DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        result = [self._message_payload(row) for row in reversed(rows)]
        return result

    def participants(self, conversation_id: str) -> list[dict[str, Any]]:
        conversation = validate_conversation_id(conversation_id)
        now = time.time()
        online_after = now - 90.0
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    p.*,
                    m.roles_json,
                    m.joined_at,
                    m.active AS membership_active,
                    room.status AS room_status,
                    (
                        SELECT COUNT(*) FROM agent_sessions AS session
                        WHERE session.participant_id = p.participant_id
                          AND session.revoked_at IS NULL
                          AND session.expires_at > ?
                    ) AS active_session_count
                FROM memberships AS m
                JOIN participants AS p
                  ON p.participant_id = m.participant_id
                JOIN rooms AS room
                  ON room.conversation_id = m.conversation_id
                WHERE m.conversation_id = ?
                ORDER BY
                    CASE
                        WHEN room.status = 'active'
                         AND m.active = 1
                         AND p.status = 'online'
                         AND p.last_seen >= ? THEN 0
                        ELSE 1
                    END,
                    p.session_alias,
                    p.participant_id
                """,
                (now, conversation, online_after),
            ).fetchall()
        return [
            {
                "participant_id": str(row["participant_id"]),
                "client_type": str(row["client_type"]),
                "session_alias": str(row["session_alias"]),
                "roles": json.loads(str(row["roles_json"])),
                "capabilities": json.loads(str(row["capabilities_json"])),
                "status": (
                    "online"
                    if str(row["room_status"]) == "active"
                    and int(row["membership_active"]) == 1
                    and str(row["status"]) == "online"
                    and float(row["last_seen"]) >= online_after
                    else "offline"
                ),
                "membership_active": bool(row["membership_active"]),
                "room_status": str(row["room_status"]),
                "last_seen": float(row["last_seen"]),
                "joined_at": float(row["joined_at"]),
                "active_session_count": int(row["active_session_count"] or 0),
            }
            for row in rows
        ]

    @staticmethod
    def _message_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "sequence": int(row["sequence"]),
            "message_id": str(row["message_id"]),
            "conversation_id": str(row["conversation_id"]),
            "sender_participant_id": str(row["sender_participant_id"]),
            "sender_alias": str(row["sender_alias"]),
            "sender_client_type": str(row["sender_client_type"]),
            "audience_kind": str(row["audience_kind"]),
            "audience_value": str(row["audience_value"]),
            "body": str(row["body"]),
            "refs": json.loads(str(row["refs_json"])),
            "reply_to": str(row["reply_to"]) if row["reply_to"] else None,
            "status": str(row["status"]),
            "claimed_by": str(row["claimed_by"]) if row["claimed_by"] else None,
            "claimant_alias": str(row["claimant_alias"] or ""),
            "claim_until": (
                float(row["claim_until"]) if row["claim_until"] else None
            ),
            "ack_count": int(row["ack_count"] or 0),
            "receipt_count": int(row["receipt_count"] or 0),
            "created_at": float(row["created_at"]),
        }
