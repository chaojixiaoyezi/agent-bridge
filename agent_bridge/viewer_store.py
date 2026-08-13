from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
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
                       invitation.adapter_kind AS connector_adapter_kind
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

    def rooms(self, *, limit: int = 200) -> list[dict[str, Any]]:
        normalized_limit = max(1, min(int(limit), 500))
        now = time.time()
        online_after = now - 90.0
        connector_online_after = now - 75.0
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
                        MAX(sequence) AS last_sequence
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
    ) -> list[dict[str, Any]]:
        conversation = validate_conversation_id(conversation_id)
        normalized_limit = max(1, min(int(limit), 501))
        if before_sequence is not None and after_sequence is not None:
            raise ValueError(
                "before_sequence and after_sequence cannot be used together"
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
                    claimant.session_alias AS claimant_alias,
                    claimant.display_name AS claimant_display_name,
                    source.conversation_id AS forwarded_source_conversation_id,
                    source.sequence AS forwarded_source_sequence,
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
                WHERE m.conversation_id = ? {sequence_clause}
                ORDER BY m.sequence {order}
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        ordered_rows = rows if after_sequence is not None else reversed(rows)
        result = [self._message_payload(row) for row in ordered_rows]
        return result

    def event_snapshot(self, *, after_sequence: int = 0) -> dict[str, Any]:
        requested_cursor = max(0, int(after_sequence))
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
            ]
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
                    "COALESCE(CAST(connector_last_seen_at AS TEXT), '') || ':' || "
                    "COALESCE(CAST(revoked_at AS TEXT), '') AS connector_state "
                    "FROM agent_connectors ORDER BY connector_state)"
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
                    "SELECT COALESCE(MAX(updated_at), 0) FROM web_users"
                ).fetchone()[0]
            )
        return {
            "cursor": max(cursor, global_sequence),
            "changed_rooms": changed_rooms,
            "pending_nickname_requests": pending_nicknames,
            "state_revision": [
                global_sequence,
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
                rate_revision,
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
                    invitation.adapter_kind AS connector_adapter_kind,
                    connector.setup_status AS connector_setup_status,
                    connector.connector_last_seen_at,
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
                            ) + policy.inactivity_days * 86400.0
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
        payload = {
            "sequence": int(row["sequence"]),
            "message_id": str(row["message_id"]),
            "conversation_id": str(row["conversation_id"]),
            "sender_participant_id": str(row["sender_participant_id"]),
            "sender_alias": str(row["sender_alias"]),
            "sender_client_type": str(row["sender_client_type"]),
            "sender_display_name": str(row["sender_display_name"]),
            "sender_signature": str(row["sender_signature"]),
            "audience_kind": str(row["audience_kind"]),
            "audience_value": str(row["audience_value"]),
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
        }
        if row["forwarded_from_message_id"] is not None:
            payload["message_kind"] = "forward"
            payload["forwarded_from"] = {
                "message_id": str(row["forwarded_from_message_id"]),
                "conversation_id": str(row["forwarded_source_conversation_id"]),
                "sequence": int(row["forwarded_source_sequence"]),
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
                "status": "revoked" if revoked_at is not None else "active",
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
                "semantics": "natural_language_minimum_necessary",
            }
        return payload
