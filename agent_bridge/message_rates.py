"""Message frequency policy and per-participant overrides."""

from __future__ import annotations

import math
import sqlite3
import time
from typing import Any

from .store_constants import (
    MAX_MESSAGE_COOLDOWN_SECONDS,
    MESSAGE_COOLDOWN_SECONDS,
    OWNER_PARTICIPANT_ID,
    RATE_LIMIT_ACTOR_KINDS,
    WEB_USER_MESSAGE_COOLDOWN_SECONDS,
)
from .store_errors import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    RateLimitError,
)
from .validation import ValidationError, opaque_id


RATE_LIMIT_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS message_rate_defaults (
    actor_kind TEXT PRIMARY KEY CHECK (actor_kind IN ('agent', 'web_user')),
    cooldown_seconds REAL NOT NULL
        CHECK (cooldown_seconds >= 0 AND cooldown_seconds <= {MAX_MESSAGE_COOLDOWN_SECONDS}),
    updated_at REAL NOT NULL,
    updated_by_web_user_id TEXT,
    FOREIGN KEY (updated_by_web_user_id) REFERENCES web_users(user_id)
);

CREATE TABLE IF NOT EXISTS message_rate_overrides (
    participant_id TEXT PRIMARY KEY,
    cooldown_seconds REAL NOT NULL
        CHECK (cooldown_seconds >= 0 AND cooldown_seconds <= {MAX_MESSAGE_COOLDOWN_SECONDS}),
    updated_at REAL NOT NULL,
    updated_by_web_user_id TEXT NOT NULL,
    FOREIGN KEY (participant_id) REFERENCES participants(participant_id),
    FOREIGN KEY (updated_by_web_user_id) REFERENCES web_users(user_id)
);

CREATE TABLE IF NOT EXISTS message_rate_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    revision INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);

INSERT OR IGNORE INTO message_rate_state (singleton, revision, updated_at)
VALUES (1, 0, CAST(strftime('%s', 'now') AS REAL));

DROP TRIGGER IF EXISTS trg_message_rate_defaults_revision_insert;
DROP TRIGGER IF EXISTS trg_message_rate_defaults_revision_update;
DROP TRIGGER IF EXISTS trg_message_rate_defaults_revision_delete;
DROP TRIGGER IF EXISTS trg_message_rate_overrides_revision_insert;
DROP TRIGGER IF EXISTS trg_message_rate_overrides_revision_update;
DROP TRIGGER IF EXISTS trg_message_rate_overrides_revision_delete;

CREATE TRIGGER trg_message_rate_defaults_revision_insert
AFTER INSERT ON message_rate_defaults
BEGIN
    UPDATE message_rate_state
    SET revision = revision + 1,
        updated_at = CAST(strftime('%s', 'now') AS REAL)
    WHERE singleton = 1;
END;

CREATE TRIGGER trg_message_rate_defaults_revision_update
AFTER UPDATE ON message_rate_defaults
BEGIN
    UPDATE message_rate_state
    SET revision = revision + 1,
        updated_at = CAST(strftime('%s', 'now') AS REAL)
    WHERE singleton = 1;
END;

CREATE TRIGGER trg_message_rate_defaults_revision_delete
AFTER DELETE ON message_rate_defaults
BEGIN
    UPDATE message_rate_state
    SET revision = revision + 1,
        updated_at = CAST(strftime('%s', 'now') AS REAL)
    WHERE singleton = 1;
END;

CREATE TRIGGER trg_message_rate_overrides_revision_insert
AFTER INSERT ON message_rate_overrides
BEGIN
    UPDATE message_rate_state
    SET revision = revision + 1,
        updated_at = CAST(strftime('%s', 'now') AS REAL)
    WHERE singleton = 1;
END;

CREATE TRIGGER trg_message_rate_overrides_revision_update
AFTER UPDATE ON message_rate_overrides
BEGIN
    UPDATE message_rate_state
    SET revision = revision + 1,
        updated_at = CAST(strftime('%s', 'now') AS REAL)
    WHERE singleton = 1;
END;

CREATE TRIGGER trg_message_rate_overrides_revision_delete
AFTER DELETE ON message_rate_overrides
BEGIN
    UPDATE message_rate_state
    SET revision = revision + 1,
        updated_at = CAST(strftime('%s', 'now') AS REAL)
    WHERE singleton = 1;
END;

INSERT OR IGNORE INTO message_rate_defaults
    (actor_kind, cooldown_seconds, updated_at, updated_by_web_user_id)
VALUES
    ('agent', {MESSAGE_COOLDOWN_SECONDS}, CAST(strftime('%s', 'now') AS REAL), NULL),
    ('web_user', {WEB_USER_MESSAGE_COOLDOWN_SECONDS}, CAST(strftime('%s', 'now') AS REAL), NULL);
"""


class MessageRateMixin:
    @staticmethod
    def _assert_speaking_cooldown(
        conn: sqlite3.Connection,
        *,
        participant_id: str,
        conversation_id: str,
        now: float,
        cooldown_seconds: float = MESSAGE_COOLDOWN_SECONDS,
    ) -> None:
        cooldown = max(0.0, float(cooldown_seconds))
        if cooldown == 0:
            return
        row = conn.execute(
            """
            SELECT created_at
            FROM messages
            WHERE conversation_id = ? AND sender_participant_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (conversation_id, participant_id),
        ).fetchone()
        if row is None:
            return
        retry_after = (
            float(row["created_at"]) + cooldown - float(now)
        )
        if retry_after > 0:
            raise RateLimitError(
                retry_after_seconds=retry_after,
                conversation_id=conversation_id,
            )

    @staticmethod
    def _normalize_rate_actor_kind(value: str, *, allow_all: bool = False) -> str:
        actor_kind = str(value or "").strip().lower()
        allowed = RATE_LIMIT_ACTOR_KINDS | ({"all"} if allow_all else set())
        if actor_kind not in allowed:
            choices = "agent, web_user" + (", or all" if allow_all else "")
            raise ValidationError(f"actor_kind must be {choices}")
        return actor_kind

    @staticmethod
    def _normalize_message_cooldown(value: object) -> float:
        if isinstance(value, bool):
            raise ValidationError("cooldown_seconds must be a number")
        try:
            cooldown = float(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError("cooldown_seconds must be a number") from exc
        if not math.isfinite(cooldown):
            raise ValidationError("cooldown_seconds must be finite")
        if not 0 <= cooldown <= MAX_MESSAGE_COOLDOWN_SECONDS:
            raise ValidationError(
                "cooldown_seconds must be between 0 and "
                f"{MAX_MESSAGE_COOLDOWN_SECONDS}"
            )
        return round(cooldown, 3)

    @staticmethod
    def _message_rate_defaults_locked(
        conn: sqlite3.Connection,
    ) -> dict[str, float]:
        rows = conn.execute(
            "SELECT actor_kind, cooldown_seconds FROM message_rate_defaults"
        ).fetchall()
        values = {str(row["actor_kind"]): float(row["cooldown_seconds"]) for row in rows}
        values.setdefault("agent", MESSAGE_COOLDOWN_SECONDS)
        values.setdefault("web_user", WEB_USER_MESSAGE_COOLDOWN_SECONDS)
        return values

    @classmethod
    def _effective_message_cooldown_locked(
        cls,
        conn: sqlite3.Connection,
        *,
        participant_id: str,
        actor_kind: str,
    ) -> float:
        kind = cls._normalize_rate_actor_kind(actor_kind)
        defaults = cls._message_rate_defaults_locked(conn)
        override = conn.execute(
            "SELECT cooldown_seconds FROM message_rate_overrides "
            "WHERE participant_id = ?",
            (participant_id,),
        ).fetchone()
        if override is None:
            return defaults[kind]
        return min(defaults[kind], float(override["cooldown_seconds"]))

    @staticmethod
    def _require_active_rate_admin_locked(
        conn: sqlite3.Connection,
        web_user_id: str,
    ) -> sqlite3.Row:
        administrator = conn.execute(
            "SELECT * FROM web_users WHERE user_id = ? "
            "AND role = 'admin' AND active = 1",
            (web_user_id,),
        ).fetchone()
        if administrator is None:
            raise AuthenticationError(
                "an active administrator is required to manage message rates"
            )
        return administrator

    @staticmethod
    def _participant_rate_identity_locked(
        conn: sqlite3.Connection,
        participant_id: str,
    ) -> tuple[sqlite3.Row, str]:
        row = conn.execute(
            """
            SELECT participant.*, web_user.user_id AS web_user_id,
                   web_user.username AS web_username,
                   web_user.role AS web_role,
                   web_user.active AS web_active,
                   override.cooldown_seconds AS individual_cooldown_seconds,
                   override.updated_at AS rate_updated_at,
                   override.updated_by_web_user_id AS rate_updated_by_web_user_id
            FROM participants AS participant
            LEFT JOIN web_users AS web_user
              ON web_user.participant_id = participant.participant_id
            LEFT JOIN message_rate_overrides AS override
              ON override.participant_id = participant.participant_id
            WHERE participant.participant_id = ?
            """,
            (participant_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"unknown participant: {participant_id}")
        if str(row["participant_id"]) == OWNER_PARTICIPANT_ID:
            raise ConflictError("legacy owner message rate is not configurable")
        if row["web_user_id"] is None:
            return row, "agent"
        if str(row["web_role"]) == "admin":
            raise ConflictError("administrator message rate is always unlimited")
        if not bool(row["web_active"]):
            raise ConflictError("inactive web users cannot receive a message rate override")
        return row, "web_user"

    @staticmethod
    def _participant_rate_payload(
        row: sqlite3.Row,
        *,
        actor_kind: str,
        global_cooldown_seconds: float,
    ) -> dict[str, Any]:
        individual = (
            float(row["individual_cooldown_seconds"])
            if row["individual_cooldown_seconds"] is not None
            else None
        )
        effective = (
            global_cooldown_seconds
            if individual is None
            else min(global_cooldown_seconds, individual)
        )
        return {
            "participant_id": str(row["participant_id"]),
            "actor_kind": actor_kind,
            "display_name": str(row["display_name"]),
            "identity": (
                str(row["web_username"])
                if actor_kind == "web_user"
                else str(row["client_type"])
            ),
            "username": (
                str(row["web_username"])
                if actor_kind == "web_user"
                else None
            ),
            "client_type": str(row["client_type"]),
            "signature": str(row["signature"]),
            "global_cooldown_seconds": global_cooldown_seconds,
            "individual_cooldown_seconds": individual,
            "effective_cooldown_seconds": effective,
            "rate_updated_at": (
                float(row["rate_updated_at"])
                if row["rate_updated_at"] is not None
                else None
            ),
            "rate_updated_by_web_user_id": (
                str(row["rate_updated_by_web_user_id"])
                if row["rate_updated_by_web_user_id"] is not None
                else None
            ),
        }

    def message_rate_summary(
        self,
        *,
        web_participant_id: str,
        web_role: str,
    ) -> dict[str, Any]:
        participant = opaque_id(web_participant_id, field="web_participant_id")
        role = str(web_role or "").strip().lower()
        if role not in {"admin", "user"}:
            raise ValidationError("web_role must be admin or user")
        with self._connection() as conn:
            defaults = self._message_rate_defaults_locked(conn)
            effective = (
                0.0
                if role == "admin"
                else self._effective_message_cooldown_locked(
                    conn,
                    participant_id=participant,
                    actor_kind="web_user",
                )
            )
        return {
            "agent_global_cooldown_seconds": defaults["agent"],
            "web_user_global_cooldown_seconds": defaults["web_user"],
            "current_user_effective_cooldown_seconds": effective,
            "resolution": "minimum",
            "maximum_cooldown_seconds": MAX_MESSAGE_COOLDOWN_SECONDS,
        }

    def message_rate_configuration(
        self,
        *,
        requesting_web_user_id: str,
    ) -> dict[str, Any]:
        reviewer = opaque_id(
            requesting_web_user_id,
            field="requesting_web_user_id",
        )
        with self._connection() as conn:
            self._require_active_rate_admin_locked(conn, reviewer)
            rows = conn.execute(
                """
                SELECT rate.*, web_user.username AS updated_by_username
                FROM message_rate_defaults AS rate
                LEFT JOIN web_users AS web_user
                  ON web_user.user_id = rate.updated_by_web_user_id
                ORDER BY rate.actor_kind
                """
            ).fetchall()
        globals_payload = {
            str(row["actor_kind"]): {
                "actor_kind": str(row["actor_kind"]),
                "cooldown_seconds": float(row["cooldown_seconds"]),
                "updated_at": float(row["updated_at"]),
                "updated_by_web_user_id": (
                    str(row["updated_by_web_user_id"])
                    if row["updated_by_web_user_id"] is not None
                    else None
                ),
                "updated_by_username": str(row["updated_by_username"] or ""),
            }
            for row in rows
        }
        return {
            "globals": globals_payload,
            "resolution": "minimum",
            "maximum_cooldown_seconds": MAX_MESSAGE_COOLDOWN_SECONDS,
        }

    def update_global_message_rate(
        self,
        *,
        actor_kind: str,
        cooldown_seconds: object,
        updated_by_web_user_id: str,
    ) -> dict[str, Any]:
        kind = self._normalize_rate_actor_kind(actor_kind)
        cooldown = self._normalize_message_cooldown(cooldown_seconds)
        reviewer = opaque_id(
            updated_by_web_user_id,
            field="updated_by_web_user_id",
        )
        now = time.time()
        with self._transaction() as conn:
            self._require_active_rate_admin_locked(conn, reviewer)
            conn.execute(
                """
                INSERT INTO message_rate_defaults
                    (actor_kind, cooldown_seconds, updated_at,
                     updated_by_web_user_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(actor_kind) DO UPDATE SET
                    cooldown_seconds = excluded.cooldown_seconds,
                    updated_at = excluded.updated_at,
                    updated_by_web_user_id = excluded.updated_by_web_user_id
                """,
                (kind, cooldown, now, reviewer),
            )
        return {
            "actor_kind": kind,
            "cooldown_seconds": cooldown,
            "updated_at": now,
            "updated_by_web_user_id": reviewer,
        }

    def search_message_rate_participants(
        self,
        *,
        requesting_web_user_id: str,
        query: str = "",
        actor_kind: str = "all",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        reviewer = opaque_id(
            requesting_web_user_id,
            field="requesting_web_user_id",
        )
        kind = self._normalize_rate_actor_kind(actor_kind, allow_all=True)
        normalized_query = str(query or "").strip()
        if len(normalized_query) > 64 or any(
            ord(character) < 32 for character in normalized_query
        ):
            raise ValidationError("rate participant search must be at most 64 characters")
        normalized_limit = max(1, min(int(limit), 100))
        escaped = (
            normalized_query.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        pattern = f"%{escaped}%"
        kind_clause = {
            "all": "",
            "agent": "AND web_user.user_id IS NULL",
            "web_user": "AND web_user.role = 'user' AND web_user.active = 1",
        }[kind]
        with self._connection() as conn:
            self._require_active_rate_admin_locked(conn, reviewer)
            defaults = self._message_rate_defaults_locked(conn)
            rows = conn.execute(
                f"""
                SELECT participant.*, web_user.user_id AS web_user_id,
                       web_user.username AS web_username,
                       web_user.role AS web_role,
                       web_user.active AS web_active,
                       override.cooldown_seconds AS individual_cooldown_seconds,
                       override.updated_at AS rate_updated_at,
                       override.updated_by_web_user_id
                           AS rate_updated_by_web_user_id
                FROM participants AS participant
                LEFT JOIN web_users AS web_user
                  ON web_user.participant_id = participant.participant_id
                LEFT JOIN message_rate_overrides AS override
                  ON override.participant_id = participant.participant_id
                WHERE (
                    web_user.user_id IS NULL
                    OR (web_user.role = 'user' AND web_user.active = 1)
                )
                AND participant.participant_id != '{OWNER_PARTICIPANT_ID}'
                {kind_clause}
                AND (
                    ? = ''
                    OR participant.display_name LIKE ? ESCAPE '\\'
                    OR participant.client_type LIKE ? ESCAPE '\\'
                    OR participant.signature LIKE ? ESCAPE '\\'
                    OR COALESCE(web_user.username, '') LIKE ? ESCAPE '\\'
                )
                ORDER BY participant.display_name COLLATE NOCASE,
                         participant.participant_id
                LIMIT ?
                """,
                (
                    normalized_query,
                    pattern,
                    pattern,
                    pattern,
                    pattern,
                    normalized_limit,
                ),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            row_kind = "web_user" if row["web_user_id"] is not None else "agent"
            result.append(
                self._participant_rate_payload(
                    row,
                    actor_kind=row_kind,
                    global_cooldown_seconds=defaults[row_kind],
                )
            )
        return result

    def set_participant_message_rate(
        self,
        *,
        participant_id: str,
        cooldown_seconds: object,
        updated_by_web_user_id: str,
    ) -> dict[str, Any]:
        participant = opaque_id(participant_id, field="participant_id")
        cooldown = self._normalize_message_cooldown(cooldown_seconds)
        reviewer = opaque_id(
            updated_by_web_user_id,
            field="updated_by_web_user_id",
        )
        now = time.time()
        with self._transaction() as conn:
            self._require_active_rate_admin_locked(conn, reviewer)
            _, actor_kind = self._participant_rate_identity_locked(conn, participant)
            conn.execute(
                """
                INSERT INTO message_rate_overrides
                    (participant_id, cooldown_seconds, updated_at,
                     updated_by_web_user_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(participant_id) DO UPDATE SET
                    cooldown_seconds = excluded.cooldown_seconds,
                    updated_at = excluded.updated_at,
                    updated_by_web_user_id = excluded.updated_by_web_user_id
                """,
                (participant, cooldown, now, reviewer),
            )
            row, _ = self._participant_rate_identity_locked(conn, participant)
            defaults = self._message_rate_defaults_locked(conn)
        return self._participant_rate_payload(
            row,
            actor_kind=actor_kind,
            global_cooldown_seconds=defaults[actor_kind],
        )

    def clear_participant_message_rate(
        self,
        *,
        participant_id: str,
        updated_by_web_user_id: str,
    ) -> dict[str, Any]:
        participant = opaque_id(participant_id, field="participant_id")
        reviewer = opaque_id(
            updated_by_web_user_id,
            field="updated_by_web_user_id",
        )
        with self._transaction() as conn:
            self._require_active_rate_admin_locked(conn, reviewer)
            _, actor_kind = self._participant_rate_identity_locked(conn, participant)
            conn.execute(
                "DELETE FROM message_rate_overrides WHERE participant_id = ?",
                (participant,),
            )
            row, _ = self._participant_rate_identity_locked(conn, participant)
            defaults = self._message_rate_defaults_locked(conn)
        return self._participant_rate_payload(
            row,
            actor_kind=actor_kind,
            global_cooldown_seconds=defaults[actor_kind],
        )
