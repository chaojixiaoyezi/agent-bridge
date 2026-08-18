"""Agent inactivity policy, room membership lifecycle, and admin migration."""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
import uuid
from collections.abc import Sequence
from typing import Any

from .store_constants import (
    CONNECTOR_ONLINE_WINDOW_SECONDS,
    DEFAULT_AGENT_INACTIVITY_DAYS,
    DEFAULT_INVITATION_TTL_SECONDS,
    DEFAULT_SESSION_TTL_SECONDS,
    DEFAULT_UNACTIVATED_AGENT_INACTIVITY_DAYS,
    MAX_AGENT_INACTIVITY_DAYS,
    MIN_AGENT_INACTIVITY_DAYS,
    OWNER_PARTICIPANT_ID,
)
from .store_errors import (
    AuthenticationError,
    AuthorizationError,
    BridgeError,
    ConflictError,
)
from .validation import (
    ValidationError,
    compact_json,
    conversation_id as validate_conversation_id,
    opaque_id,
)


AGENT_LIFECYCLE_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS agent_lifecycle_policy (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    inactivity_days INTEGER NOT NULL
        CHECK (inactivity_days BETWEEN {MIN_AGENT_INACTIVITY_DAYS}
                                   AND {MAX_AGENT_INACTIVITY_DAYS}),
    unactivated_inactivity_days INTEGER NOT NULL
        DEFAULT {DEFAULT_UNACTIVATED_AGENT_INACTIVITY_DAYS}
        CHECK (unactivated_inactivity_days BETWEEN {MIN_AGENT_INACTIVITY_DAYS}
                                               AND {MAX_AGENT_INACTIVITY_DAYS}),
    updated_at REAL NOT NULL,
    updated_by_web_user_id TEXT,
    FOREIGN KEY (updated_by_web_user_id) REFERENCES web_users(user_id)
);

CREATE TABLE IF NOT EXISTS agent_lifecycle_states (
    participant_id TEXT PRIMARY KEY,
    access_granted_at REAL NOT NULL,
    last_spoke_at REAL,
    reinvite_required INTEGER NOT NULL DEFAULT 0
        CHECK (reinvite_required IN (0, 1)),
    expired_at REAL,
    expired_reason TEXT,
    updated_at REAL NOT NULL,
    FOREIGN KEY (participant_id) REFERENCES participants(participant_id)
);

CREATE TABLE IF NOT EXISTS agent_room_blocks (
    conversation_id TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    reason TEXT NOT NULL CHECK (reason IN ('kicked', 'migrated', 'inactive')),
    blocked_at REAL NOT NULL,
    blocked_by_web_user_id TEXT,
    PRIMARY KEY (conversation_id, participant_id),
    FOREIGN KEY (conversation_id) REFERENCES rooms(conversation_id),
    FOREIGN KEY (participant_id) REFERENCES participants(participant_id),
    FOREIGN KEY (blocked_by_web_user_id) REFERENCES web_users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_lifecycle_expiration
    ON agent_lifecycle_states(reinvite_required, last_spoke_at, access_granted_at);
CREATE INDEX IF NOT EXISTS idx_agent_room_blocks_participant
    ON agent_room_blocks(participant_id, conversation_id);

INSERT OR IGNORE INTO agent_lifecycle_policy
    (singleton, inactivity_days, unactivated_inactivity_days,
     updated_at, updated_by_web_user_id)
VALUES
    (1, {DEFAULT_AGENT_INACTIVITY_DAYS},
     {DEFAULT_UNACTIVATED_AGENT_INACTIVITY_DAYS},
     CAST(strftime('%s', 'now') AS REAL), NULL);

DROP TRIGGER IF EXISTS trg_agent_lifecycle_message_insert;
CREATE TRIGGER trg_agent_lifecycle_message_insert
AFTER INSERT ON messages
WHEN NEW.sender_participant_id != '{OWNER_PARTICIPANT_ID}'
 AND NOT EXISTS (
    SELECT 1 FROM web_users
    WHERE participant_id = NEW.sender_participant_id
)
BEGIN
    INSERT INTO agent_lifecycle_states
        (participant_id, access_granted_at, last_spoke_at,
         reinvite_required, expired_at, expired_reason, updated_at)
    VALUES
        (NEW.sender_participant_id, NEW.created_at, NEW.created_at,
         0, NULL, NULL, NEW.created_at)
    ON CONFLICT(participant_id) DO UPDATE SET
        last_spoke_at = CASE
            WHEN agent_lifecycle_states.last_spoke_at IS NULL
              OR excluded.last_spoke_at > agent_lifecycle_states.last_spoke_at
            THEN excluded.last_spoke_at
            ELSE agent_lifecycle_states.last_spoke_at
        END,
        updated_at = MAX(agent_lifecycle_states.updated_at, excluded.updated_at);
END;
"""


class AgentLifecycleMixin:
    @staticmethod
    def _agent_active_room_count(
        conn: sqlite3.Connection,
        participant_id: str,
    ) -> int:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM rooms
            WHERE creator_kind = 'agent'
              AND creator_participant_id = ?
              AND status = 'active'
            """,
            (participant_id,),
        ).fetchone()
        return int(row["count"] if row is not None else 0)

    @staticmethod
    def _require_active_admin_locked(
        conn: sqlite3.Connection,
        web_user_id: str,
    ) -> sqlite3.Row:
        administrator = conn.execute(
            "SELECT * FROM web_users WHERE user_id = ? "
            "AND role = 'admin' AND active = 1",
            (web_user_id,),
        ).fetchone()
        if administrator is None:
            raise AuthenticationError("an active administrator is required")
        return administrator

    @staticmethod
    def _normalize_agent_inactivity_days(value: object) -> int:
        if isinstance(value, bool):
            raise ValidationError("Agent inactivity days must be an integer")
        try:
            days = int(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError("Agent inactivity days must be an integer") from exc
        if str(value).strip() != str(days) and not isinstance(value, int):
            raise ValidationError("Agent inactivity days must be an integer")
        if not MIN_AGENT_INACTIVITY_DAYS <= days <= MAX_AGENT_INACTIVITY_DAYS:
            raise ValidationError(
                "Agent inactivity days must be between "
                f"{MIN_AGENT_INACTIVITY_DAYS} and {MAX_AGENT_INACTIVITY_DAYS}"
            )
        return days

    @staticmethod
    def _ensure_agent_lifecycle_state_locked(
        conn: sqlite3.Connection,
        *,
        participant_id: str,
        now: float,
    ) -> sqlite3.Row:
        conn.execute(
            """
            INSERT OR IGNORE INTO agent_lifecycle_states
                (participant_id, access_granted_at, last_spoke_at,
                 reinvite_required, expired_at, expired_reason, updated_at)
            VALUES (?, ?, NULL, 0, NULL, NULL, ?)
            """,
            (participant_id, now, now),
        )
        return conn.execute(
            "SELECT * FROM agent_lifecycle_states WHERE participant_id = ?",
            (participant_id,),
        ).fetchone()

    def _assert_agent_registration_allowed_locked(
        self,
        conn: sqlite3.Connection,
        *,
        participant_id: str,
        conversation_id: str,
        now: float,
    ) -> None:
        state = self._ensure_agent_lifecycle_state_locked(
            conn,
            participant_id=participant_id,
            now=now,
        )
        if bool(state["reinvite_required"]):
            raise ConflictError(
                "Agent access expired after inactivity; a new invitation is required"
            )
        block = conn.execute(
            "SELECT reason FROM agent_room_blocks "
            "WHERE conversation_id = ? AND participant_id = ?",
            (conversation_id, participant_id),
        ).fetchone()
        if block is not None:
            raise ConflictError(
                f"Agent was {block['reason']} from conversation {conversation_id}; "
                "a new invitation is required"
            )

    def _grant_agent_invitation_locked(
        self,
        conn: sqlite3.Connection,
        *,
        participant_id: str,
        conversation_id: str,
        now: float,
    ) -> None:
        self._ensure_agent_lifecycle_state_locked(
            conn,
            participant_id=participant_id,
            now=now,
        )
        conn.execute(
            """
            UPDATE agent_lifecycle_states
            SET access_granted_at = ?, reinvite_required = 0,
                expired_at = NULL, expired_reason = NULL, updated_at = ?
            WHERE participant_id = ?
            """,
            (now, now, participant_id),
        )
        conn.execute(
            "DELETE FROM agent_room_blocks "
            "WHERE conversation_id = ? AND participant_id = ?",
            (conversation_id, participant_id),
        )

    @staticmethod
    def _cancel_agent_room_deliveries_locked(
        conn: sqlite3.Connection,
        *,
        participant_id: str,
        conversation_id: str,
    ) -> int:
        return int(
            conn.execute(
                """
                UPDATE message_deliveries
                SET state = 'cancelled', delivery_stage = 'cancelled',
                    actionable = 0
                WHERE participant_id = ?
                  AND state IN ('pending', 'delivered')
                  AND message_id IN (
                      SELECT message_id FROM messages WHERE conversation_id = ?
                  )
                """,
                (participant_id, conversation_id),
            ).rowcount
        )

    @staticmethod
    def _require_agent_participant_locked(
        conn: sqlite3.Connection,
        participant_id: str,
    ) -> sqlite3.Row:
        participant = conn.execute(
            """
            SELECT participant.*
            FROM participants AS participant
            WHERE participant.participant_id = ?
              AND participant.participant_id != ?
              AND NOT EXISTS (
                  SELECT 1 FROM web_users AS web_user
                  WHERE web_user.participant_id = participant.participant_id
              )
            """,
            (participant_id, OWNER_PARTICIPANT_ID),
        ).fetchone()
        if participant is None:
            raise ConflictError("only Agent participants can be managed here")
        return participant

    def _expire_inactive_agents_locked(
        self,
        conn: sqlite3.Connection,
        *,
        now: float,
    ) -> list[dict[str, Any]]:
        policy = conn.execute(
            "SELECT inactivity_days, unactivated_inactivity_days "
            "FROM agent_lifecycle_policy WHERE singleton = 1"
        ).fetchone()
        inactivity_days = int(policy["inactivity_days"])
        unactivated_days = int(policy["unactivated_inactivity_days"])
        candidates = conn.execute(
            """
            SELECT state.participant_id, participant.display_name,
                   state.access_granted_at, state.last_spoke_at,
                   MAX(state.access_granted_at,
                       COALESCE(state.last_spoke_at, state.access_granted_at))
                       AS inactivity_anchor,
                   CASE
                       WHEN state.last_spoke_at IS NULL
                        AND NOT EXISTS (
                            SELECT 1 FROM agent_sessions AS live_session
                            WHERE live_session.participant_id = state.participant_id
                              AND live_session.revoked_at IS NULL
                              AND live_session.cleared_at IS NULL
                              AND live_session.expires_at > ?
                        )
                        AND NOT EXISTS (
                            SELECT 1 FROM agent_connectors AS live_connector
                            WHERE live_connector.accepted_participant_id =
                                  state.participant_id
                              AND live_connector.revoked_at IS NULL
                              AND live_connector.setup_status = 'configured'
                              AND COALESCE(
                                  live_connector.connector_last_seen_at,
                                  0
                              ) >= ?
                        )
                       THEN ?
                       ELSE ?
                   END AS effective_inactivity_days,
                   CASE
                       WHEN state.last_spoke_at IS NULL
                        AND NOT EXISTS (
                            SELECT 1 FROM agent_sessions AS live_session
                            WHERE live_session.participant_id = state.participant_id
                              AND live_session.revoked_at IS NULL
                              AND live_session.cleared_at IS NULL
                              AND live_session.expires_at > ?
                        )
                        AND NOT EXISTS (
                            SELECT 1 FROM agent_connectors AS live_connector
                            WHERE live_connector.accepted_participant_id =
                                  state.participant_id
                              AND live_connector.revoked_at IS NULL
                              AND live_connector.setup_status = 'configured'
                              AND COALESCE(
                                  live_connector.connector_last_seen_at,
                                  0
                              ) >= ?
                        )
                       THEN 'inactive_unactivated'
                       ELSE 'inactive'
                   END AS expiration_reason
            FROM agent_lifecycle_states AS state
            JOIN participants AS participant
              ON participant.participant_id = state.participant_id
            WHERE state.reinvite_required = 0
              AND MAX(
                    state.access_granted_at,
                    COALESCE(state.last_spoke_at, state.access_granted_at)
                  ) + (
                    CASE
                        WHEN state.last_spoke_at IS NULL
                         AND NOT EXISTS (
                             SELECT 1 FROM agent_sessions AS live_session
                             WHERE live_session.participant_id = state.participant_id
                               AND live_session.revoked_at IS NULL
                               AND live_session.cleared_at IS NULL
                               AND live_session.expires_at > ?
                         )
                         AND NOT EXISTS (
                             SELECT 1 FROM agent_connectors AS live_connector
                             WHERE live_connector.accepted_participant_id =
                                   state.participant_id
                               AND live_connector.revoked_at IS NULL
                               AND live_connector.setup_status = 'configured'
                               AND COALESCE(
                                   live_connector.connector_last_seen_at,
                                   0
                               ) >= ?
                         )
                        THEN ?
                        ELSE ?
                    END * 86400.0
                  ) <= ?
              AND EXISTS (
                  SELECT 1 FROM memberships AS membership
                  WHERE membership.participant_id = state.participant_id
                    AND membership.active = 1
              )
            ORDER BY inactivity_anchor, state.participant_id
            """,
            (
                now,
                now - CONNECTOR_ONLINE_WINDOW_SECONDS,
                unactivated_days,
                inactivity_days,
                now,
                now - CONNECTOR_ONLINE_WINDOW_SECONDS,
                now,
                now - CONNECTOR_ONLINE_WINDOW_SECONDS,
                unactivated_days,
                inactivity_days,
                now,
            ),
        ).fetchall()
        expired: list[dict[str, Any]] = []
        for candidate in candidates:
            participant_id = str(candidate["participant_id"])
            rooms = [
                str(row["conversation_id"])
                for row in conn.execute(
                    "SELECT conversation_id FROM memberships "
                    "WHERE participant_id = ? AND active = 1 "
                    "ORDER BY conversation_id",
                    (participant_id,),
                ).fetchall()
            ]
            for conversation_id in rooms:
                conn.execute(
                    """
                    INSERT INTO agent_room_blocks
                        (conversation_id, participant_id, reason, blocked_at,
                         blocked_by_web_user_id)
                    VALUES (?, ?, 'inactive', ?, NULL)
                    ON CONFLICT(conversation_id, participant_id) DO UPDATE SET
                        reason = 'inactive', blocked_at = excluded.blocked_at,
                        blocked_by_web_user_id = NULL
                    """,
                    (conversation_id, participant_id, now),
                )
                self._cancel_agent_room_deliveries_locked(
                    conn,
                    participant_id=participant_id,
                    conversation_id=conversation_id,
                )
            conn.execute(
                "UPDATE memberships SET active = 0, updated_at = ? "
                "WHERE participant_id = ? AND active = 1",
                (now, participant_id),
            )
            conn.execute(
                """
                UPDATE agent_lifecycle_states
                SET reinvite_required = 1, expired_at = ?,
                    expired_reason = ?, updated_at = ?
                WHERE participant_id = ?
                """,
                (
                    now,
                    str(candidate["expiration_reason"]),
                    now,
                    participant_id,
                ),
            )
            conn.execute(
                """
                UPDATE agent_sessions
                SET revoked_at = COALESCE(revoked_at, ?),
                    revoked_reason = COALESCE(revoked_reason, 'agent_inactive'),
                    cleared_at = COALESCE(cleared_at, ?)
                WHERE participant_id = ?
                """,
                (now, now, participant_id),
            )
            conn.execute(
                """
                UPDATE agent_connectors
                SET setup_status = 'revoked',
                    revoked_at = COALESCE(revoked_at, ?),
                    setup_updated_at = ?, updated_at = ?
                WHERE accepted_participant_id = ? AND revoked_at IS NULL
                """,
                (now, now, now, participant_id),
            )
            conn.execute(
                "UPDATE participants SET status = 'offline' "
                "WHERE participant_id = ?",
                (participant_id,),
            )
            expired.append(
                {
                    "participant_id": participant_id,
                    "display_name": str(candidate["display_name"]),
                    "conversation_ids": rooms,
                    "last_spoke_at": (
                        float(candidate["last_spoke_at"])
                        if candidate["last_spoke_at"] is not None
                        else None
                    ),
                    "inactivity_anchor": float(candidate["inactivity_anchor"]),
                    "effective_inactivity_days": int(
                        candidate["effective_inactivity_days"]
                    ),
                    "expired_reason": str(candidate["expiration_reason"]),
                    "expired_at": now,
                }
            )
        return expired

    def agent_lifecycle_configuration(
        self,
        *,
        requesting_web_user_id: str,
    ) -> dict[str, Any]:
        requester = opaque_id(
            requesting_web_user_id,
            field="requesting_web_user_id",
        )
        now = time.time()
        with self._transaction() as conn:
            self._require_active_admin_locked(conn, requester)
            expired = self._expire_inactive_agents_locked(conn, now=now)
            policy = conn.execute(
                "SELECT * FROM agent_lifecycle_policy WHERE singleton = 1"
            ).fetchone()
        return {
            "inactivity_days": int(policy["inactivity_days"]),
            "unactivated_inactivity_days": int(
                policy["unactivated_inactivity_days"]
            ),
            "minimum_days": MIN_AGENT_INACTIVITY_DAYS,
            "maximum_days": MAX_AGENT_INACTIVITY_DAYS,
            "updated_at": float(policy["updated_at"]),
            "expired_count": len(expired),
        }

    def update_agent_lifecycle_configuration(
        self,
        *,
        inactivity_days: object,
        unactivated_inactivity_days: object | None = None,
        updated_by_web_user_id: str,
    ) -> dict[str, Any]:
        days = self._normalize_agent_inactivity_days(inactivity_days)
        unactivated_days = (
            self._normalize_agent_inactivity_days(unactivated_inactivity_days)
            if unactivated_inactivity_days is not None
            else None
        )
        reviewer = opaque_id(
            updated_by_web_user_id,
            field="updated_by_web_user_id",
        )
        now = time.time()
        with self._transaction() as conn:
            self._require_active_admin_locked(conn, reviewer)
            conn.execute(
                """
                UPDATE agent_lifecycle_policy
                SET inactivity_days = ?,
                    unactivated_inactivity_days = COALESCE(?, unactivated_inactivity_days),
                    updated_at = ?,
                    updated_by_web_user_id = ?
                WHERE singleton = 1
                """,
                (days, unactivated_days, now, reviewer),
            )
            expired = self._expire_inactive_agents_locked(conn, now=now)
            policy = conn.execute(
                "SELECT * FROM agent_lifecycle_policy WHERE singleton = 1"
            ).fetchone()
        return {
            "inactivity_days": days,
            "unactivated_inactivity_days": int(
                policy["unactivated_inactivity_days"]
            ),
            "minimum_days": MIN_AGENT_INACTIVITY_DAYS,
            "maximum_days": MAX_AGENT_INACTIVITY_DAYS,
            "updated_at": now,
            "expired_count": len(expired),
            "expired_agents": expired,
        }

    def admin_room_agents(
        self,
        *,
        requesting_web_user_id: str,
    ) -> dict[str, Any]:
        requester = opaque_id(
            requesting_web_user_id,
            field="requesting_web_user_id",
        )
        now = time.time()
        with self._transaction() as conn:
            self._require_active_admin_locked(conn, requester)
            self._expire_inactive_agents_locked(conn, now=now)
            policy = conn.execute(
                "SELECT inactivity_days, unactivated_inactivity_days "
                "FROM agent_lifecycle_policy WHERE singleton = 1"
            ).fetchone()
            inactivity_days = int(policy["inactivity_days"])
            unactivated_days = int(policy["unactivated_inactivity_days"])
            rows = conn.execute(
                """
                SELECT room.conversation_id, participant.participant_id,
                       participant.client_type, participant.display_name,
                       participant.signature, participant.status,
                       membership.roles_json, membership.joined_at,
                       state.access_granted_at, state.last_spoke_at,
                       EXISTS (
                           SELECT 1 FROM agent_sessions AS live_session
                           WHERE live_session.participant_id =
                                 participant.participant_id
                             AND live_session.revoked_at IS NULL
                             AND live_session.cleared_at IS NULL
                             AND live_session.expires_at > ?
                       ) AS has_live_session,
                       EXISTS (
                           SELECT 1 FROM agent_connectors AS live_connector
                           WHERE live_connector.accepted_participant_id =
                                 participant.participant_id
                             AND live_connector.revoked_at IS NULL
                             AND live_connector.setup_status = 'configured'
                             AND COALESCE(
                                 live_connector.connector_last_seen_at,
                                 0
                             ) >= ?
                       ) AS has_recent_connector
                FROM rooms AS room
                JOIN memberships AS membership
                  ON membership.conversation_id = room.conversation_id
                 AND membership.active = 1
                JOIN participants AS participant
                  ON participant.participant_id = membership.participant_id
                LEFT JOIN agent_lifecycle_states AS state
                  ON state.participant_id = participant.participant_id
                WHERE room.status = 'active'
                  AND participant.participant_id != ?
                  AND NOT EXISTS (
                      SELECT 1 FROM web_users AS web_user
                      WHERE web_user.participant_id = participant.participant_id
                  )
                ORDER BY room.conversation_id,
                         participant.display_name COLLATE NOCASE,
                         participant.participant_id
                """,
                (
                    now,
                    now - CONNECTOR_ONLINE_WINDOW_SECONDS,
                    OWNER_PARTICIPANT_ID,
                ),
            ).fetchall()
            active_rooms = [
                str(row["conversation_id"])
                for row in conn.execute(
                    "SELECT conversation_id FROM rooms WHERE status = 'active' "
                    "ORDER BY conversation_id"
                ).fetchall()
            ]
        rooms: dict[str, list[dict[str, Any]]] = {
            conversation_id: [] for conversation_id in active_rooms
        }
        for row in rows:
            access_granted_at = float(row["access_granted_at"] or row["joined_at"])
            last_spoke_at = (
                float(row["last_spoke_at"])
                if row["last_spoke_at"] is not None
                else None
            )
            inactivity_anchor = max(access_granted_at, last_spoke_at or 0.0)
            unactivated = (
                last_spoke_at is None
                and not bool(row["has_live_session"])
                and not bool(row["has_recent_connector"])
            )
            effective_days = unactivated_days if unactivated else inactivity_days
            rooms.setdefault(str(row["conversation_id"]), []).append(
                {
                    "participant_id": str(row["participant_id"]),
                    "client_type": str(row["client_type"]),
                    "display_name": str(row["display_name"]),
                    "signature": str(row["signature"]),
                    "status": str(row["status"]),
                    "roles": json.loads(str(row["roles_json"] or "[]")),
                    "joined_at": float(row["joined_at"]),
                    "access_granted_at": access_granted_at,
                    "last_spoke_at": last_spoke_at,
                    "lifecycle_class": "unactivated" if unactivated else "normal",
                    "effective_inactivity_days": effective_days,
                    "inactivity_expires_at": (
                        inactivity_anchor + effective_days * 86_400.0
                    ),
                }
            )
        return {
            "rooms": [
                {"conversation_id": room, "agents": agents}
                for room, agents in rooms.items()
            ],
            "inactivity_days": inactivity_days,
            "unactivated_inactivity_days": unactivated_days,
        }

    def kick_agent_from_room(
        self,
        *,
        conversation_id: str,
        participant_id: str,
        kicked_by_web_user_id: str,
    ) -> dict[str, Any]:
        conversation = validate_conversation_id(conversation_id)
        participant = opaque_id(participant_id, field="participant_id")
        administrator = opaque_id(
            kicked_by_web_user_id,
            field="kicked_by_web_user_id",
        )
        now = time.time()
        with self._transaction() as conn:
            room_permissions = self._room_web_permissions_locked(
                conn,
                web_user_id=administrator,
                conversation_id=conversation,
            )
            if not room_permissions["can_kick_agents"]:
                raise AuthorizationError("你没有踢出本聊天室 Agent 的权限")
            self._expire_inactive_agents_locked(conn, now=now)
            self._require_active_room(conn, conversation)
            profile = self._require_agent_participant_locked(conn, participant)
            membership = conn.execute(
                "SELECT roles_json FROM memberships WHERE conversation_id = ? "
                "AND participant_id = ? AND active = 1",
                (conversation, participant),
            ).fetchone()
            if membership is None:
                raise ConflictError(
                    f"Agent {participant} is not active in conversation {conversation}"
                )
            conn.execute(
                "UPDATE memberships SET active = 0, updated_at = ? "
                "WHERE conversation_id = ? AND participant_id = ?",
                (now, conversation, participant),
            )
            conn.execute(
                """
                INSERT INTO agent_room_blocks
                    (conversation_id, participant_id, reason, blocked_at,
                     blocked_by_web_user_id)
                VALUES (?, ?, 'kicked', ?, ?)
                ON CONFLICT(conversation_id, participant_id) DO UPDATE SET
                    reason = 'kicked', blocked_at = excluded.blocked_at,
                    blocked_by_web_user_id = excluded.blocked_by_web_user_id
                """,
                (conversation, participant, now, administrator),
            )
            revoked_sessions = int(
                conn.execute(
                    """
                    UPDATE agent_sessions
                    SET revoked_at = COALESCE(revoked_at, ?),
                        revoked_reason = COALESCE(revoked_reason, 'agent_kicked'),
                        cleared_at = COALESCE(cleared_at, ?)
                    WHERE participant_id = ?
                      AND registered_conversation_id = ?
                      AND cleared_at IS NULL
                    """,
                    (now, now, participant, conversation),
                ).rowcount
            )
            revoked_connectors = int(
                conn.execute(
                    """
                    UPDATE agent_connectors
                    SET setup_status = 'revoked',
                        revoked_at = COALESCE(revoked_at, ?),
                        setup_updated_at = ?, updated_at = ?
                    WHERE accepted_participant_id = ? AND conversation_id = ?
                      AND revoked_at IS NULL
                    """,
                    (now, now, now, participant, conversation),
                ).rowcount
            )
            cancelled_deliveries = self._cancel_agent_room_deliveries_locked(
                conn,
                participant_id=participant,
                conversation_id=conversation,
            )
            conn.execute(
                """
                UPDATE participants
                SET status = 'offline'
                WHERE participant_id = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM agent_sessions AS session
                      WHERE session.participant_id = participants.participant_id
                        AND session.cleared_at IS NULL
                        AND session.revoked_at IS NULL
                        AND session.expires_at > ?
                  )
                """,
                (participant, now),
            )
        return {
            "conversation_id": conversation,
            "participant_id": participant,
            "display_name": str(profile["display_name"]),
            "kicked_at": now,
            "revoked_session_count": revoked_sessions,
            "revoked_connector_count": revoked_connectors,
            "cancelled_delivery_count": cancelled_deliveries,
            "reinvite_required_for_room": True,
            "history_preserved": True,
        }

    def migrate_agents(
        self,
        *,
        target_conversation_id: str,
        selections: object,
        migrated_by_web_user_id: str,
    ) -> dict[str, Any]:
        target = validate_conversation_id(target_conversation_id)
        administrator = opaque_id(
            migrated_by_web_user_id,
            field="migrated_by_web_user_id",
        )
        if not isinstance(selections, Sequence) or isinstance(
            selections, (str, bytes)
        ):
            raise ValidationError("selections must be a list")
        sources: dict[str, list[str]] = {}
        for selection in selections:
            if not isinstance(selection, dict):
                raise ValidationError("each migration selection must be an object")
            unexpected = set(selection) - {"source_conversation_id", "participant_ids"}
            if unexpected or "source_conversation_id" not in selection or (
                "participant_ids" not in selection
            ):
                raise ValidationError(
                    "each migration selection requires source_conversation_id "
                    "and participant_ids"
                )
            source = validate_conversation_id(selection["source_conversation_id"])
            if source == target:
                raise ConflictError("migration source and target rooms must differ")
            participant_ids = selection["participant_ids"]
            if not isinstance(participant_ids, Sequence) or isinstance(
                participant_ids, (str, bytes)
            ):
                raise ValidationError("participant_ids must be a list")
            normalized = sources.setdefault(source, [])
            for participant_id in participant_ids:
                participant = opaque_id(participant_id, field="participant_id")
                if participant not in normalized:
                    normalized.append(participant)
        sources = {source: participants for source, participants in sources.items() if participants}
        selected_count = sum(len(participants) for participants in sources.values())
        if selected_count < 1:
            raise ValidationError("select at least one Agent to migrate")
        if selected_count > 500:
            raise ValidationError("cannot migrate more than 500 Agent memberships at once")

        now = time.time()
        with self._transaction() as conn:
            self._require_active_admin_locked(conn, administrator)
            self._expire_inactive_agents_locked(conn, now=now)
            self._require_active_room(conn, target)
            participant_sources: dict[str, list[str]] = {}
            participant_roles: dict[str, set[str]] = {}
            participant_names: dict[str, str] = {}
            for source, participant_ids in sources.items():
                self._require_active_room(conn, source)
                for participant in participant_ids:
                    profile = self._require_agent_participant_locked(conn, participant)
                    membership = conn.execute(
                        "SELECT roles_json FROM memberships "
                        "WHERE conversation_id = ? AND participant_id = ? "
                        "AND active = 1",
                        (source, participant),
                    ).fetchone()
                    if membership is None:
                        raise ConflictError(
                            f"Agent {participant} is not active in conversation {source}"
                        )
                    participant_sources.setdefault(participant, []).append(source)
                    participant_roles.setdefault(participant, set()).update(
                        json.loads(str(membership["roles_json"] or "[]"))
                    )
                    participant_names[participant] = str(profile["display_name"])

            for participant, source_rooms in participant_sources.items():
                target_membership = conn.execute(
                    "SELECT roles_json FROM memberships "
                    "WHERE conversation_id = ? AND participant_id = ?",
                    (target, participant),
                ).fetchone()
                if target_membership is not None:
                    participant_roles[participant].update(
                        json.loads(str(target_membership["roles_json"] or "[]"))
                    )
                roles = sorted(participant_roles[participant])
                conn.execute(
                    """
                    INSERT INTO memberships
                        (conversation_id, participant_id, roles_json, active,
                         joined_at, updated_at)
                    VALUES (?, ?, ?, 1, ?, ?)
                    ON CONFLICT(conversation_id, participant_id) DO UPDATE SET
                        roles_json = excluded.roles_json, active = 1,
                        updated_at = excluded.updated_at
                    """,
                    (target, participant, compact_json(roles), now, now),
                )
                conn.execute(
                    "DELETE FROM agent_room_blocks "
                    "WHERE conversation_id = ? AND participant_id = ?",
                    (target, participant),
                )

            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise BridgeError("Agent migration would violate database relationships")
        return {
            "target_conversation_id": target,
            "migrated_at": now,
            "membership_count": selected_count,
            "copied_membership_count": selected_count,
            "agent_count": len(participant_sources),
            "agents": [
                {
                    "participant_id": participant,
                    "display_name": participant_names[participant],
                    "source_conversation_ids": source_rooms,
                }
                for participant, source_rooms in participant_sources.items()
            ],
            "history_preserved": True,
            "source_memberships_preserved": True,
            "sessions_rebound": False,
        }

    def provision_existing_agent_room_connector(
        self,
        *,
        conversation_id: str,
        participant_id: str,
        created_by_web_user_id: str,
    ) -> dict[str, Any]:
        """Create a separate room connector for an existing public identity.

        One connector still binds to exactly one room.  The participant identity
        is reused, so additive migration never invents a suffixed duplicate.
        """

        conversation = validate_conversation_id(conversation_id)
        participant = opaque_id(participant_id, field="participant_id")
        administrator = opaque_id(
            created_by_web_user_id,
            field="created_by_web_user_id",
        )
        now = time.time()
        connector_id = f"connector_{uuid.uuid4().hex}"
        invitation_id = f"invite_{uuid.uuid4().hex}"
        session_id = f"session_{uuid.uuid4().hex}"
        access_token = f"session_{secrets.token_urlsafe(32)}"
        enrollment_token = f"enroll_{secrets.token_urlsafe(32)}"
        with self._transaction() as conn:
            self._require_active_admin_locked(conn, administrator)
            self._require_active_room(conn, conversation)
            profile = self._require_agent_participant_locked(conn, participant)
            membership = conn.execute(
                "SELECT roles_json FROM memberships WHERE conversation_id = ? "
                "AND participant_id = ? AND active = 1",
                (conversation, participant),
            ).fetchone()
            if membership is None:
                raise ConflictError("Agent is not an active member of target room")
            existing = conn.execute(
                "SELECT connector_id FROM agent_connectors "
                "WHERE conversation_id = ? AND accepted_participant_id = ? "
                "AND revoked_at IS NULL LIMIT 1",
                (conversation, participant),
            ).fetchone()
            if existing is not None:
                raise ConflictError(
                    "Agent already has a live connector for the target room"
                )
            template = conn.execute(
                """
                SELECT connector.*, invitation.product,
                       invitation.requested_mode, invitation.adapter_kind,
                       invitation.tui_adapter_kind
                FROM agent_connectors AS connector
                JOIN agent_invitations AS invitation
                  ON invitation.invitation_id = connector.invitation_id
                WHERE connector.accepted_participant_id = ?
                  AND connector.revoked_at IS NULL
                ORDER BY connector.connector_last_seen_at DESC,
                         connector.updated_at DESC
                LIMIT 1
                """,
                (participant,),
            ).fetchone()
            if template is None:
                raise ConflictError(
                    "Agent has no existing connector authority to copy safely"
                )
            product = str(template["product"])
            username = self._username_from_bound_identity(
                product=product,
                client_type=str(profile["client_type"]),
            )
            roles = json.loads(str(membership["roles_json"] or "[]"))
            capabilities = json.loads(
                str(profile["capabilities_json"] or "[]")
            )
            conn.execute("PRAGMA defer_foreign_keys = ON")
            conn.execute(
                """
                INSERT INTO agent_invitations
                    (invitation_id, token_hash, conversation_id, product,
                     requested_mode, adapter_kind, tui_adapter_kind,
                     reuse_policy, max_uses,
                     use_count, status, created_by_web_user_id,
                     created_at, expires_at, first_accepted_at,
                     last_accepted_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'single', 1, 1, 'exhausted',
                        ?, ?, ?, ?, ?, ?)
                """,
                (
                    invitation_id,
                    self._secret_hash(f"internal_{secrets.token_urlsafe(32)}"),
                    conversation,
                    product,
                    str(template["requested_mode"]),
                    str(template["adapter_kind"]),
                    template["tui_adapter_kind"],
                    administrator,
                    now,
                    now + DEFAULT_INVITATION_TTL_SECONDS,
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
                     ttl_seconds, last_seen, connector_id, component)
                VALUES (?, ?, ?, ?, 'mcp', ?, ?, ?, ?, ?, 'mcp')
                """,
                (
                    session_id,
                    participant,
                    conversation,
                    self._secret_hash(access_token),
                    now,
                    now + DEFAULT_SESSION_TTL_SECONDS,
                    DEFAULT_SESSION_TTL_SECONDS,
                    now,
                    connector_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO agent_connectors
                    (connector_id, invitation_id, conversation_id,
                     accepted_participant_id, initial_session_id,
                     enrollment_token_hash, enrollment_last_used_at,
                     setup_status, setup_updated_at, binding_version,
                     requested_username, bound_client_type,
                     bound_roles_json, bound_capabilities_json,
                     tui_endpoint_id, tui_state,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'awaiting_setup', ?, 2,
                        ?, ?, ?, ?, ?, 'awaiting_confirmation', ?, ?)
                """,
                (
                    connector_id,
                    invitation_id,
                    conversation,
                    participant,
                    session_id,
                    self._secret_hash(enrollment_token),
                    now,
                    now,
                    username,
                    str(profile["client_type"]),
                    compact_json(roles),
                    compact_json(capabilities),
                    template["tui_endpoint_id"],
                    now,
                    now,
                ),
            )
            self._grant_agent_invitation_locked(
                conn,
                participant_id=participant,
                conversation_id=conversation,
                now=now,
            )
            conn.execute(
                "UPDATE participants SET status = 'online', last_seen = ? "
                "WHERE participant_id = ?",
                (now, participant),
            )
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise BridgeError("room connector provisioning is inconsistent")
        return {
            "participant_id": participant,
            "client_type": str(profile["client_type"]),
            "display_name": str(profile["display_name"]),
            "signature": str(profile["signature"]),
            "conversation_id": conversation,
            "product": product,
            "username": username,
            "roles": roles,
            "capabilities": capabilities,
            "connector_id": connector_id,
            "invitation_id": invitation_id,
            "session_id": session_id,
            "access_token": access_token,
            "enrollment_token": enrollment_token,
            "adapter_kind": str(template["adapter_kind"]),
            "tui_adapter_kind": (
                str(template["tui_adapter_kind"])
                if template["tui_adapter_kind"] is not None
                else None
            ),
            "requested_mode": str(template["requested_mode"]),
            "identity_binding_version": 2,
        }
