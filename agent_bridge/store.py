from __future__ import annotations

import json
import hashlib
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from .validation import (
    ValidationError,
    alias,
    body,
    client_identity,
    compact_json,
    conversation_id as validate_conversation_id,
    display_name as validate_display_name,
    message_refs,
    opaque_id,
    product_username,
    string_tokens,
    token,
)


AUDIENCE_KINDS = {"participant", "room", "role", "broadcast"}
PRESENCE_STATES = {"online", "offline"}
MESSAGE_ACTIONS = {"claim", "ack", "release"}
DELIVERY_STATES = {"pending", "delivered", "acked", "cancelled"}
MESSAGE_COOLDOWN_SECONDS = 15.0
AGENT_ACTIVE_ROOM_LIMIT = 2
ROOM_ABANDON_AFTER_SECONDS = 90 * 24 * 60 * 60
DEFAULT_SESSION_TTL_SECONDS = 2 * 60 * 60
NICKNAME_REQUEST_COOLDOWN_SECONDS = 24 * 60 * 60
MAX_MENTIONS_PER_MESSAGE = 64
OWNER_PARTICIPANT_ID = "participant_web_owner"
OWNER_AUTHORIZATION_ID = "owner_web_ui"
OWNER_CLIENT_TYPE = "web-user"
OWNER_SESSION_ALIAS = "本机用户"


class BridgeError(RuntimeError):
    """Base error returned to MCP/CLI callers as structured failure text."""


class NotFoundError(BridgeError):
    pass


class ConflictError(BridgeError):
    pass


class RateLimitError(ConflictError):
    def __init__(self, *, retry_after_seconds: float, conversation_id: str) -> None:
        self.retry_after_seconds = max(0.0, float(retry_after_seconds))
        self.conversation_id = conversation_id
        super().__init__(
            "message rate limit: wait "
            f"{self.retry_after_seconds:.3f} seconds before speaking again in "
            f"conversation {conversation_id}"
        )


class NicknameRateLimitError(ConflictError):
    def __init__(self, *, retry_after_seconds: float) -> None:
        self.retry_after_seconds = max(0.0, float(retry_after_seconds))
        super().__init__(
            "nickname changes may be requested at most once every 24 hours; "
            f"retry after {self.retry_after_seconds:.3f} seconds"
        )


class AuthenticationError(BridgeError):
    pass


def _agent_sessions_table_sql(
    table_name: str = "agent_sessions",
    *,
    if_not_exists: bool = True,
) -> str:
    clause = "IF NOT EXISTS " if if_not_exists else ""
    return f"""
CREATE TABLE {clause}{table_name} (
    session_id TEXT PRIMARY KEY,
    participant_id TEXT NOT NULL,
    registered_conversation_id TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    transport TEXT NOT NULL DEFAULT 'mcp'
        CHECK (transport = 'mcp'),
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    ttl_seconds REAL NOT NULL DEFAULT {DEFAULT_SESSION_TTL_SECONDS},
    last_seen REAL NOT NULL,
    revoked_at REAL,
    revoked_reason TEXT,
    cleared_at REAL,
    FOREIGN KEY (participant_id) REFERENCES participants(participant_id),
    FOREIGN KEY (registered_conversation_id) REFERENCES rooms(conversation_id)
);
"""


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS participants (
    participant_id TEXT PRIMARY KEY,
    client_type TEXT NOT NULL,
    session_alias TEXT NOT NULL,
    display_name TEXT NOT NULL,
    signature TEXT NOT NULL,
    profile_updated_at REAL NOT NULL,
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'online',
    created_at REAL NOT NULL,
    last_seen REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS rooms (
    conversation_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'abandoned')),
    creator_kind TEXT NOT NULL
        CHECK (creator_kind IN ('agent', 'user', 'legacy')),
    creator_participant_id TEXT,
    created_at REAL NOT NULL,
    last_activity_at REAL NOT NULL,
    abandoned_at REAL,
    FOREIGN KEY (creator_participant_id)
        REFERENCES participants(participant_id),
    CHECK (
        (creator_kind = 'agent' AND creator_participant_id IS NOT NULL)
        OR
        (creator_kind IN ('user', 'legacy') AND creator_participant_id IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS memberships (
    conversation_id TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    roles_json TEXT NOT NULL DEFAULT '[]',
    active INTEGER NOT NULL DEFAULT 1,
    joined_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (conversation_id, participant_id),
    FOREIGN KEY (conversation_id) REFERENCES rooms(conversation_id),
    FOREIGN KEY (participant_id) REFERENCES participants(participant_id)
);

{_agent_sessions_table_sql()}

CREATE TABLE IF NOT EXISTS messages (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL UNIQUE,
    conversation_id TEXT NOT NULL,
    sender_participant_id TEXT NOT NULL,
    audience_kind TEXT NOT NULL,
    audience_value TEXT NOT NULL,
    message_kind TEXT NOT NULL,
    body TEXT NOT NULL,
    refs_json TEXT NOT NULL DEFAULT '[]',
    mentions_json TEXT NOT NULL DEFAULT '[]',
    reply_to TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    claimed_by TEXT,
    claim_until REAL,
    authorized_session_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES rooms(conversation_id),
    FOREIGN KEY (sender_participant_id) REFERENCES participants(participant_id),
    FOREIGN KEY (reply_to) REFERENCES messages(message_id),
    FOREIGN KEY (claimed_by) REFERENCES participants(participant_id)
);

CREATE TABLE IF NOT EXISTS receipts (
    message_id TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    state TEXT NOT NULL,
    delivered_at REAL,
    acked_at REAL,
    PRIMARY KEY (message_id, participant_id),
    FOREIGN KEY (message_id) REFERENCES messages(message_id),
    FOREIGN KEY (participant_id) REFERENCES participants(participant_id)
);

CREATE INDEX IF NOT EXISTS idx_memberships_participant
    ON memberships(participant_id, active, conversation_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_participants_client_identity_unique
    ON participants(client_type);
CREATE INDEX IF NOT EXISTS idx_rooms_status_activity
    ON rooms(status, last_activity_at);
CREATE INDEX IF NOT EXISTS idx_rooms_creator_active
    ON rooms(creator_participant_id, status)
    WHERE creator_kind = 'agent';
CREATE INDEX IF NOT EXISTS idx_messages_conversation_sequence
    ON messages(conversation_id, sequence);
CREATE INDEX IF NOT EXISTS idx_messages_conversation_sender_created
    ON messages(conversation_id, sender_participant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_audience
    ON messages(audience_kind, audience_value, status, sequence);
CREATE INDEX IF NOT EXISTS idx_receipts_participant
    ON receipts(participant_id, state, message_id);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_participant_active
    ON agent_sessions(participant_id, revoked_at, expires_at);

DROP TRIGGER IF EXISTS trg_messages_reply_only_to_question;

CREATE TRIGGER IF NOT EXISTS trg_participants_identity_immutable
BEFORE UPDATE OF client_type, session_alias ON participants
WHEN NEW.client_type != OLD.client_type OR NEW.session_alias != OLD.session_alias
BEGIN
    SELECT RAISE(ABORT, 'PARTICIPANT_IDENTITY_IMMUTABLE');
END;

CREATE TRIGGER IF NOT EXISTS trg_memberships_require_active_room_insert
BEFORE INSERT ON memberships
WHEN NEW.active = 1 AND NOT EXISTS (
    SELECT 1 FROM rooms
    WHERE conversation_id = NEW.conversation_id AND status = 'active'
)
BEGIN
    SELECT RAISE(ABORT, 'ROOM_NOT_ACTIVE');
END;

CREATE TRIGGER IF NOT EXISTS trg_memberships_require_active_room_update
BEFORE UPDATE OF active, conversation_id ON memberships
WHEN NEW.active = 1 AND NOT EXISTS (
    SELECT 1 FROM rooms
    WHERE conversation_id = NEW.conversation_id AND status = 'active'
)
BEGIN
    SELECT RAISE(ABORT, 'ROOM_NOT_ACTIVE');
END;

CREATE TRIGGER IF NOT EXISTS trg_messages_require_active_room_insert
BEFORE INSERT ON messages
WHEN NOT EXISTS (
    SELECT 1 FROM rooms
    WHERE conversation_id = NEW.conversation_id AND status = 'active'
)
BEGIN
    SELECT RAISE(ABORT, 'ROOM_NOT_ACTIVE');
END;

CREATE TRIGGER IF NOT EXISTS trg_messages_reply_only_to_root
BEFORE INSERT ON messages
WHEN NEW.reply_to IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM messages
    WHERE message_id = NEW.reply_to AND reply_to IS NULL
)
BEGIN
    SELECT RAISE(ABORT, 'REPLY_CHAIN_NOT_ALLOWED');
END;

CREATE TRIGGER IF NOT EXISTS trg_messages_sender_cooldown
BEFORE INSERT ON messages
WHEN EXISTS (
    SELECT 1 FROM messages
    WHERE conversation_id = NEW.conversation_id
      AND sender_participant_id = NEW.sender_participant_id
      AND created_at > NEW.created_at - {MESSAGE_COOLDOWN_SECONDS}
)
BEGIN
    SELECT RAISE(ABORT, 'MESSAGE_RATE_LIMITED');
END;

CREATE TRIGGER IF NOT EXISTS trg_messages_touch_room_after_insert
AFTER INSERT ON messages
BEGIN
    UPDATE rooms
    SET last_activity_at = MAX(last_activity_at, NEW.created_at)
    WHERE conversation_id = NEW.conversation_id AND status = 'active';
END;
"""


PROFILE_SCHEMA = """
CREATE TABLE IF NOT EXISTS nickname_requests (
    request_id TEXT PRIMARY KEY,
    participant_id TEXT NOT NULL,
    requested_display_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected')),
    requested_at REAL NOT NULL,
    requested_session_id TEXT NOT NULL,
    reviewed_at REAL,
    review_note TEXT,
    FOREIGN KEY (participant_id) REFERENCES participants(participant_id),
    FOREIGN KEY (requested_session_id) REFERENCES agent_sessions(session_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_participants_display_name_unique
    ON participants(display_name COLLATE NOCASE);
CREATE UNIQUE INDEX IF NOT EXISTS idx_nickname_requests_one_pending
    ON nickname_requests(participant_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_nickname_requests_status_requested
    ON nickname_requests(status, requested_at DESC);
CREATE INDEX IF NOT EXISTS idx_nickname_requests_participant_requested
    ON nickname_requests(participant_id, requested_at DESC);

DROP TRIGGER IF EXISTS trg_participants_display_name_requires_pending_request;
CREATE TRIGGER trg_participants_display_name_requires_pending_request
BEFORE UPDATE OF display_name ON participants
WHEN NEW.display_name != OLD.display_name AND NOT EXISTS (
    SELECT 1 FROM nickname_requests
    WHERE participant_id = OLD.participant_id
      AND requested_display_name = NEW.display_name
      AND status = 'pending'
)
BEGIN
    SELECT RAISE(ABORT, 'NICKNAME_APPROVAL_REQUIRED');
END;
"""


DELIVERY_SCHEMA = """
CREATE TABLE IF NOT EXISTS follows (
    conversation_id TEXT NOT NULL,
    follower_participant_id TEXT NOT NULL,
    followed_participant_id TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (
        conversation_id,
        follower_participant_id,
        followed_participant_id
    ),
    CHECK (follower_participant_id != followed_participant_id),
    FOREIGN KEY (conversation_id) REFERENCES rooms(conversation_id),
    FOREIGN KEY (follower_participant_id)
        REFERENCES participants(participant_id),
    FOREIGN KEY (followed_participant_id)
        REFERENCES participants(participant_id)
);

CREATE TABLE IF NOT EXISTS message_deliveries (
    message_id TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'delivered', 'acked', 'cancelled')),
    reasons_json TEXT NOT NULL DEFAULT '[]',
    priority TEXT NOT NULL DEFAULT 'normal'
        CHECK (priority IN ('normal', 'important', 'direct')),
    actionable INTEGER NOT NULL DEFAULT 0 CHECK (actionable IN (0, 1)),
    created_at REAL NOT NULL,
    first_delivered_at REAL,
    last_delivered_at REAL,
    acked_at REAL,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    PRIMARY KEY (message_id, participant_id),
    FOREIGN KEY (message_id) REFERENCES messages(message_id),
    FOREIGN KEY (participant_id) REFERENCES participants(participant_id)
);

CREATE INDEX IF NOT EXISTS idx_follows_follower_active
    ON follows(follower_participant_id, active, conversation_id);
CREATE INDEX IF NOT EXISTS idx_follows_followed_active
    ON follows(followed_participant_id, active, conversation_id);
CREATE INDEX IF NOT EXISTS idx_message_deliveries_participant_state
    ON message_deliveries(participant_id, state, created_at, message_id);
CREATE INDEX IF NOT EXISTS idx_message_deliveries_message_state
    ON message_deliveries(message_id, state, participant_id);

DROP TRIGGER IF EXISTS trg_follows_require_memberships_insert;
CREATE TRIGGER trg_follows_require_memberships_insert
BEFORE INSERT ON follows
WHEN NEW.active = 1 AND (
    NOT EXISTS (
        SELECT 1 FROM memberships
        WHERE conversation_id = NEW.conversation_id
          AND participant_id = NEW.follower_participant_id
          AND active = 1
    )
    OR NOT EXISTS (
        SELECT 1 FROM memberships
        WHERE conversation_id = NEW.conversation_id
          AND participant_id = NEW.followed_participant_id
          AND active = 1
    )
)
BEGIN
    SELECT RAISE(ABORT, 'FOLLOW_REQUIRES_ACTIVE_MEMBERSHIPS');
END;

DROP TRIGGER IF EXISTS trg_follows_require_memberships_update;
CREATE TRIGGER trg_follows_require_memberships_update
BEFORE UPDATE OF active, conversation_id,
                 follower_participant_id, followed_participant_id ON follows
WHEN NEW.active = 1 AND (
    NOT EXISTS (
        SELECT 1 FROM memberships
        WHERE conversation_id = NEW.conversation_id
          AND participant_id = NEW.follower_participant_id
          AND active = 1
    )
    OR NOT EXISTS (
        SELECT 1 FROM memberships
        WHERE conversation_id = NEW.conversation_id
          AND participant_id = NEW.followed_participant_id
          AND active = 1
    )
)
BEGIN
    SELECT RAISE(ABORT, 'FOLLOW_REQUIRES_ACTIVE_MEMBERSHIPS');
END;

DROP TRIGGER IF EXISTS trg_memberships_disable_follows_after_leave;
CREATE TRIGGER trg_memberships_disable_follows_after_leave
AFTER UPDATE OF active ON memberships
WHEN NEW.active = 0 AND OLD.active = 1
BEGIN
    UPDATE follows
    SET active = 0, updated_at = CAST(strftime('%s', 'now') AS REAL)
    WHERE conversation_id = NEW.conversation_id
      AND (
          follower_participant_id = NEW.participant_id
          OR followed_participant_id = NEW.participant_id
      );
END;
"""


AUTHORIZATION_SCHEMA = f"""
CREATE INDEX IF NOT EXISTS idx_messages_authorized_session
    ON messages(authorized_session_id, sequence);

DROP TRIGGER IF EXISTS trg_messages_require_live_mcp_session;
DROP TRIGGER IF EXISTS trg_messages_require_authorized_sender;
CREATE TRIGGER trg_messages_require_authorized_sender
BEFORE INSERT ON messages
WHEN NOT (
    (
        NEW.authorized_session_id = '{OWNER_AUTHORIZATION_ID}'
        AND NEW.sender_participant_id = '{OWNER_PARTICIPANT_ID}'
    )
    OR EXISTS (
        SELECT 1
        FROM agent_sessions AS session
        WHERE session.session_id = NEW.authorized_session_id
          AND session.participant_id = NEW.sender_participant_id
          AND session.transport = 'mcp'
          AND session.cleared_at IS NULL
          AND session.revoked_at IS NULL
          AND session.expires_at > CAST(strftime('%s', 'now') AS REAL)
    )
)
BEGIN
    SELECT RAISE(ABORT, 'AUTHORIZED_SENDER_REQUIRED');
END;
"""


class BridgeStore:
    def __init__(
        self,
        database: str | Path,
        *,
        poll_interval_seconds: float = 0.2,
    ) -> None:
        self.database = Path(database).expanduser()
        self.poll_interval_seconds = max(0.05, min(float(poll_interval_seconds), 2.0))
        self._initialize()

    def _initialize(self) -> None:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.database.parent, 0o700)
        except OSError:
            pass
        with self._connection() as conn:
            schema_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            self._migrate_invited_sessions(conn)
            conn.executescript(SCHEMA)
            participant_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(participants)").fetchall()
            }
            if "display_name" not in participant_columns:
                conn.execute("ALTER TABLE participants ADD COLUMN display_name TEXT")
            if "signature" not in participant_columns:
                conn.execute("ALTER TABLE participants ADD COLUMN signature TEXT")
            if "profile_updated_at" not in participant_columns:
                conn.execute(
                    "ALTER TABLE participants ADD COLUMN profile_updated_at REAL"
                )
            conn.execute(
                "UPDATE participants SET display_name = client_type "
                "WHERE display_name IS NULL OR trim(display_name) = ''"
            )
            conn.execute(
                "UPDATE participants SET signature = session_alias "
                "WHERE signature IS NULL OR trim(signature) = ''"
            )
            conn.execute(
                "UPDATE participants SET profile_updated_at = created_at "
                "WHERE profile_updated_at IS NULL"
            )
            session_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(agent_sessions)").fetchall()
            }
            if "ttl_seconds" not in session_columns:
                conn.execute(
                    "ALTER TABLE agent_sessions ADD COLUMN ttl_seconds REAL "
                    f"NOT NULL DEFAULT {DEFAULT_SESSION_TTL_SECONDS}"
                )
            if "cleared_at" not in session_columns:
                conn.execute("ALTER TABLE agent_sessions ADD COLUMN cleared_at REAL")
            if schema_version < 10:
                # Before sliding renewal, a successful heartbeat updated
                # last_seen without extending expires_at.  Preserve a recently
                # active legacy session across this migration, while leaving
                # genuinely stale, revoked, and cleared sessions expired.
                conn.execute(
                    """
                    UPDATE agent_sessions
                    SET expires_at = last_seen + ttl_seconds
                    WHERE revoked_at IS NULL
                      AND cleared_at IS NULL
                      AND expires_at < last_seen + ttl_seconds
                    """
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_sessions_visible_state "
                "ON agent_sessions(cleared_at, revoked_at, expires_at)"
            )
            message_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(messages)").fetchall()
            }
            if "authorized_session_id" not in message_columns:
                conn.execute(
                    "ALTER TABLE messages ADD COLUMN authorized_session_id TEXT"
                )
            mentions_column_added = "mentions_json" not in message_columns
            if mentions_column_added:
                conn.execute(
                    "ALTER TABLE messages ADD COLUMN mentions_json TEXT "
                    "NOT NULL DEFAULT '[]'"
                )
            if schema_version < 8 or mentions_column_added:
                self._backfill_implicit_participant_mentions(conn)
            conn.executescript(PROFILE_SCHEMA)
            delivery_table_existed = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'message_deliveries'"
            ).fetchone() is not None
            conn.executescript(DELIVERY_SCHEMA)
            delivery_columns = {
                str(row["name"])
                for row in conn.execute(
                    "PRAGMA table_info(message_deliveries)"
                ).fetchall()
            }
            actionable_column_added = "actionable" not in delivery_columns
            if actionable_column_added:
                conn.execute(
                    "ALTER TABLE message_deliveries ADD COLUMN actionable INTEGER "
                    "NOT NULL DEFAULT 0 CHECK (actionable IN (0, 1))"
                )
            conn.executescript(AUTHORIZATION_SCHEMA)
            reconcile_deliveries = (
                schema_version < 8
                or not delivery_table_existed
                or actionable_column_added
            )
        with self._transaction() as conn:
            self._backfill_legacy_rooms(conn)
            if reconcile_deliveries:
                self._backfill_message_deliveries(conn)
            self._archive_stale_rooms_locked(conn, now=time.time())
            conn.execute("PRAGMA user_version = 10")
            conn.execute("PRAGMA optimize")
        try:
            os.chmod(self.database, 0o600)
        except OSError:
            pass

    @staticmethod
    def _migrate_invited_sessions(conn: sqlite3.Connection) -> None:
        """Remove invite-bound session storage while preserving live sessions.

        Version 4 stored one invite id on every MCP session.  Open registration
        keeps the room that established the session but no longer stores or
        accepts invitation codes.  The migration runs before the version 5
        schema and removes the obsolete invites table in the same transaction.
        """
        session_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'agent_sessions'"
        ).fetchone()
        if session_table is None:
            return
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(agent_sessions)").fetchall()
        }
        if "registered_conversation_id" in columns:
            return
        if "invite_id" not in columns:
            raise BridgeError("unsupported agent_sessions schema")
        invite_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'invites'"
        ).fetchone()
        if invite_table is None:
            raise BridgeError("cannot migrate invite sessions without invites table")

        conn.execute("DROP TRIGGER IF EXISTS trg_messages_require_live_mcp_session")
        conn.execute("DROP TRIGGER IF EXISTS trg_messages_require_authorized_sender")
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("BEGIN IMMEDIATE")
            source_count = int(
                conn.execute("SELECT COUNT(*) FROM agent_sessions").fetchone()[0]
            )
            conn.execute(
                _agent_sessions_table_sql(
                    "agent_sessions_open_registration",
                    if_not_exists=False,
                )
            )
            conn.execute(
                """
                INSERT INTO agent_sessions_open_registration
                    (session_id, participant_id, registered_conversation_id,
                     token_hash, transport, created_at, expires_at, last_seen,
                     revoked_at, revoked_reason)
                SELECT
                    session.session_id,
                    session.participant_id,
                    invite.conversation_id,
                    session.token_hash,
                    session.transport,
                    session.created_at,
                    session.expires_at,
                    session.last_seen,
                    session.revoked_at,
                    session.revoked_reason
                FROM agent_sessions AS session
                JOIN invites AS invite ON invite.invite_id = session.invite_id
                """
            )
            copied_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM agent_sessions_open_registration"
                ).fetchone()[0]
            )
            if copied_count != source_count:
                raise BridgeError(
                    "agent session migration would lose rows: "
                    f"source={source_count}, copied={copied_count}"
                )
            conn.execute("DROP TABLE agent_sessions")
            conn.execute(
                "ALTER TABLE agent_sessions_open_registration "
                "RENAME TO agent_sessions"
            )
            conn.execute("DROP TABLE invites")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.database),
            timeout=5.0,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _backfill_legacy_rooms(conn: sqlite3.Connection) -> None:
        """Give pre-room-table conversations a durable, non-owned room row."""
        rows = conn.execute(
            """
            WITH conversation_ids AS (
                SELECT conversation_id FROM memberships
                UNION
                SELECT conversation_id FROM messages
            )
            SELECT
                ids.conversation_id,
                (SELECT MIN(joined_at) FROM memberships
                 WHERE conversation_id = ids.conversation_id) AS first_joined_at,
                (SELECT MIN(created_at) FROM messages
                 WHERE conversation_id = ids.conversation_id) AS first_message_at,
                (SELECT MAX(created_at) FROM messages
                 WHERE conversation_id = ids.conversation_id) AS last_message_at
            FROM conversation_ids AS ids
            """
        ).fetchall()
        fallback_now = time.time()
        for row in rows:
            candidates = [
                float(value)
                for value in (row["first_joined_at"], row["first_message_at"])
                if value is not None
            ]
            created_at = min(candidates) if candidates else fallback_now
            last_activity_at = (
                float(row["last_message_at"])
                if row["last_message_at"] is not None
                else created_at
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO rooms
                    (conversation_id, status, creator_kind,
                     creator_participant_id, created_at, last_activity_at)
                VALUES (?, 'active', 'legacy', NULL, ?, ?)
                """,
                (str(row["conversation_id"]), created_at, last_activity_at),
            )

    @staticmethod
    def _normalize_mentions(values: Sequence[str] | None) -> list[str]:
        if values is None:
            return []
        if isinstance(values, (str, bytes)):
            raise ValidationError("mentions must be a list of participant_id values")
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            participant = opaque_id(value, field="mentions")
            if participant in seen:
                continue
            seen.add(participant)
            normalized.append(participant)
        if len(normalized) > MAX_MENTIONS_PER_MESSAGE:
            raise ValidationError(
                f"mentions cannot contain more than {MAX_MENTIONS_PER_MESSAGE} entries"
            )
        return normalized

    @staticmethod
    def _backfill_implicit_participant_mentions(conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT message_id, audience_value, mentions_json FROM messages "
            "WHERE audience_kind = 'participant'"
        ).fetchall()
        for row in rows:
            mentions = list(json.loads(str(row["mentions_json"] or "[]")))
            target = str(row["audience_value"])
            if target not in mentions:
                mentions.append(target)
                conn.execute(
                    "UPDATE messages SET mentions_json = ? WHERE message_id = ?",
                    (compact_json(mentions), str(row["message_id"])),
                )

    @classmethod
    def _delivery_candidates_locked(
        cls,
        conn: sqlite3.Connection,
        message: sqlite3.Row,
        *,
        include_inactive_memberships: bool = False,
    ) -> list[dict[str, Any]]:
        conversation = str(message["conversation_id"])
        sender = str(message["sender_participant_id"])
        created_at = float(message["created_at"])
        mention_ids = set(json.loads(str(message["mentions_json"] or "[]")))
        membership_filter = "" if include_inactive_memberships else "AND active = 1"
        memberships = conn.execute(
            "SELECT participant_id, roles_json, joined_at FROM memberships "
            "WHERE conversation_id = ? "
            f"{membership_filter} AND joined_at <= ?",
            (conversation, created_at),
        ).fetchall()
        followers = {
            str(row["follower_participant_id"])
            for row in conn.execute(
                "SELECT follower_participant_id FROM follows "
                "WHERE conversation_id = ? AND followed_participant_id = ? "
                "AND active = 1 AND created_at <= ?",
                (conversation, sender, created_at),
            ).fetchall()
        }
        audience_kind = str(message["audience_kind"])
        candidates: list[dict[str, Any]] = []
        for membership in memberships:
            participant = str(membership["participant_id"])
            if participant == sender:
                continue
            roles = set(json.loads(str(membership["roles_json"])))
            primary_recipient = cls._eligible(
                message,
                participant_id=participant,
                roles=roles,
            )
            reasons = ["room_activity"]
            if primary_recipient:
                reasons.append(f"audience:{audience_kind}")
            if participant in mention_ids:
                reasons.append("mention")
            if participant in followers:
                reasons.append("follow")
            if audience_kind == "participant" and primary_recipient:
                priority = "direct"
            elif (
                "mention" in reasons
                or "follow" in reasons
                or (audience_kind == "role" and primary_recipient)
            ):
                priority = "important"
            else:
                priority = "normal"
            candidates.append(
                {
                    "participant_id": participant,
                    "reasons": reasons,
                    "priority": priority,
                    "actionable": (
                        primary_recipient
                        and audience_kind in {"participant", "role"}
                    ),
                }
            )
        return candidates

    @classmethod
    def _create_message_deliveries_locked(
        cls,
        conn: sqlite3.Connection,
        message: sqlite3.Row,
    ) -> None:
        mention_ids = set(json.loads(str(message["mentions_json"] or "[]")))
        candidates = cls._delivery_candidates_locked(conn, message)
        candidate_ids = {str(item["participant_id"]) for item in candidates}
        missing_mentions = sorted(mention_ids - candidate_ids)
        if missing_mentions:
            raise ConflictError(
                "mentioned participants must be active eligible recipients in the "
                f"same room: {', '.join(missing_mentions)}"
            )
        for candidate in candidates:
            conn.execute(
                """
                INSERT INTO message_deliveries
                    (message_id, participant_id, state, reasons_json,
                     priority, actionable, created_at)
                VALUES (?, ?, 'pending', ?, ?, ?, ?)
                """,
                (
                    str(message["message_id"]),
                    str(candidate["participant_id"]),
                    compact_json(candidate["reasons"]),
                    str(candidate["priority"]),
                    1 if candidate["actionable"] else 0,
                    float(message["created_at"]),
                ),
            )

    @classmethod
    def _backfill_message_deliveries(cls, conn: sqlite3.Connection) -> None:
        """Build and reconcile the durable room-delivery ledger.

        Earlier Bridge versions treated ``audience_kind=participant`` as a
        visibility filter.  It is now a structured @ target: every member who
        was already in the room gets a delivery row, while only the target is
        actionable.  Reconciliation intentionally preserves existing delivery
        state and receipts.  It only revives the legacy ``cancelled`` rows that
        were created by the short-lived private-message migration.

        Resolved historical messages are inserted as acknowledged so an
        upgrade cannot manufacture months of unread notifications.  The room
        history remains complete, and explicit legacy receipts stay intact.
        """
        messages = conn.execute(
            """
            SELECT message.*
            FROM messages AS message
            ORDER BY message.sequence
            """
        ).fetchall()
        for message in messages:
            candidates = cls._delivery_candidates_locked(
                conn,
                message,
                include_inactive_memberships=True,
            )
            existing_deliveries = {
                str(row["participant_id"]): row
                for row in conn.execute(
                    "SELECT * FROM message_deliveries WHERE message_id = ?",
                    (str(message["message_id"]),),
                ).fetchall()
            }
            for candidate in candidates:
                participant_id = str(candidate["participant_id"])
                receipt = conn.execute(
                    "SELECT * FROM receipts WHERE message_id = ? AND participant_id = ?",
                    (
                        str(message["message_id"]),
                        participant_id,
                    ),
                ).fetchone()
                receipt_state = str(receipt["state"]) if receipt is not None else ""
                existing = existing_deliveries.get(participant_id)
                if existing is not None and str(existing["state"]) != "cancelled":
                    state = str(existing["state"])
                elif receipt_state == "acked" or str(message["status"]) != "open":
                    state = "acked"
                elif receipt_state == "delivered":
                    state = "delivered"
                else:
                    state = "pending"
                delivered_at = (
                    float(receipt["delivered_at"])
                    if receipt is not None and receipt["delivered_at"] is not None
                    else (
                        float(existing["first_delivered_at"])
                        if existing is not None
                        and existing["first_delivered_at"] is not None
                        else None
                    )
                )
                acked_at = (
                    float(receipt["acked_at"])
                    if receipt is not None and receipt["acked_at"] is not None
                    else (
                        float(existing["acked_at"])
                        if existing is not None and existing["acked_at"] is not None
                        else (
                            float(message["updated_at"])
                            if state == "acked"
                            else None
                        )
                    )
                )
                conn.execute(
                    """
                    INSERT INTO message_deliveries
                        (message_id, participant_id, state, reasons_json,
                         priority, actionable, created_at, first_delivered_at,
                         last_delivered_at, acked_at, attempt_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(message_id, participant_id) DO UPDATE SET
                        state = excluded.state,
                        reasons_json = excluded.reasons_json,
                        priority = excluded.priority,
                        actionable = excluded.actionable,
                        first_delivered_at = COALESCE(
                            message_deliveries.first_delivered_at,
                            excluded.first_delivered_at
                        ),
                        last_delivered_at = COALESCE(
                            message_deliveries.last_delivered_at,
                            excluded.last_delivered_at
                        ),
                        acked_at = COALESCE(
                            message_deliveries.acked_at,
                            excluded.acked_at
                        ),
                        attempt_count = MAX(
                            message_deliveries.attempt_count,
                            excluded.attempt_count
                        )
                    """,
                    (
                        str(message["message_id"]),
                        participant_id,
                        state,
                        compact_json(candidate["reasons"]),
                        str(candidate["priority"]),
                        1
                        if candidate["actionable"]
                        and str(message["status"]) == "open"
                        else 0,
                        float(message["created_at"]),
                        delivered_at,
                        delivered_at,
                        acked_at,
                        (
                            int(existing["attempt_count"])
                            if existing is not None
                            else (1 if delivered_at is not None else 0)
                        ),
                    ),
                )

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
    def _assert_speaking_cooldown(
        conn: sqlite3.Connection,
        *,
        participant_id: str,
        conversation_id: str,
        now: float,
    ) -> None:
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
            float(row["created_at"]) + MESSAGE_COOLDOWN_SECONDS - float(now)
        )
        if retry_after > 0:
            raise RateLimitError(
                retry_after_seconds=retry_after,
                conversation_id=conversation_id,
            )

    @staticmethod
    def _ensure_owner_membership_locked(
        conn: sqlite3.Connection,
        *,
        conversation_id: str,
        now: float,
    ) -> None:
        conn.execute(
            """
            INSERT INTO participants
                (participant_id, client_type, session_alias, display_name, signature,
                 profile_updated_at, capabilities_json, status, created_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, '[]', 'online', ?, ?)
            ON CONFLICT(participant_id) DO UPDATE SET
                status = 'online',
                last_seen = excluded.last_seen
            """,
            (
                OWNER_PARTICIPANT_ID,
                OWNER_CLIENT_TYPE,
                OWNER_SESSION_ALIAS,
                OWNER_SESSION_ALIAS,
                OWNER_SESSION_ALIAS,
                float(now),
                float(now),
                float(now),
            ),
        )
        conn.execute(
            """
            INSERT INTO memberships
                (conversation_id, participant_id, roles_json, active,
                 joined_at, updated_at)
            VALUES (?, ?, '["owner"]', 1, ?, ?)
            ON CONFLICT(conversation_id, participant_id) DO UPDATE SET
                roles_json = excluded.roles_json,
                active = 1,
                updated_at = excluded.updated_at
            """,
            (
                conversation_id,
                OWNER_PARTICIPANT_ID,
                float(now),
                float(now),
            ),
        )

    @staticmethod
    def _archive_stale_rooms_locked(
        conn: sqlite3.Connection,
        *,
        now: float,
    ) -> list[str]:
        cutoff = float(now) - ROOM_ABANDON_AFTER_SECONDS
        rows = conn.execute(
            """
            SELECT conversation_id
            FROM rooms
            WHERE status = 'active' AND last_activity_at <= ?
            ORDER BY conversation_id
            """,
            (cutoff,),
        ).fetchall()
        conversation_ids = [str(row["conversation_id"]) for row in rows]
        if not conversation_ids:
            return []
        conn.execute(
            """
            UPDATE rooms
            SET status = 'abandoned', abandoned_at = ?
            WHERE status = 'active' AND last_activity_at <= ?
            """,
            (float(now), cutoff),
        )
        conn.execute(
            """
            UPDATE memberships
            SET active = 0, updated_at = ?
            WHERE active = 1 AND conversation_id IN (
                SELECT conversation_id FROM rooms
                WHERE status = 'abandoned' AND abandoned_at = ?
            )
            """,
            (float(now), float(now)),
        )
        return conversation_ids

    def register(
        self,
        *,
        client_type: str,
        session_alias: str,
        conversation_id: str,
        roles: Sequence[str] | None = None,
        capabilities: Sequence[str] | None = None,
        resume_participant_id: str | None = None,
        create_room_if_missing: bool = False,
    ) -> dict[str, Any]:
        normalized_client = client_identity(client_type)
        normalized_alias = alias(session_alias)
        normalized_conversation = validate_conversation_id(conversation_id)
        normalized_roles = string_tokens(roles, field="roles")
        normalized_capabilities = string_tokens(capabilities, field="capabilities")
        now = time.time()

        with self._transaction() as conn:
            self._archive_stale_rooms_locked(conn, now=now)
            if resume_participant_id:
                participant_id = opaque_id(
                    resume_participant_id,
                    field="resume_participant_id",
                )
                existing = conn.execute(
                    "SELECT * FROM participants WHERE participant_id = ?",
                    (participant_id,),
                ).fetchone()
                if existing is None:
                    raise NotFoundError(f"unknown participant: {participant_id}")
                if str(existing["client_type"]) != normalized_client:
                    raise ConflictError("participant client_type does not match")
                if str(existing["session_alias"]) != normalized_alias:
                    raise ConflictError(
                        "session_alias is immutable and must match the original "
                        "registration"
                    )
                conn.execute(
                    """
                    UPDATE participants
                    SET capabilities_json = ?, profile_updated_at = ?,
                        status = 'online', last_seen = ?
                    WHERE participant_id = ?
                    """,
                    (
                        compact_json(normalized_capabilities),
                        now,
                        now,
                        participant_id,
                    ),
                )
            else:
                duplicate = conn.execute(
                    "SELECT participant_id FROM participants WHERE client_type = ?",
                    (normalized_client,),
                ).fetchone()
                if duplicate is not None:
                    raise ConflictError(
                        "client identity is already registered as "
                        f"{duplicate['participant_id']}; choose another username or "
                        "resume that participant"
                    )
                participant_id = f"participant_{uuid.uuid4().hex}"
                conn.execute(
                    """
                    INSERT INTO participants
                        (participant_id, client_type, session_alias,
                         display_name, signature, profile_updated_at,
                         capabilities_json, status, created_at, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'online', ?, ?)
                    """,
                    (
                        participant_id,
                        normalized_client,
                        normalized_alias,
                        normalized_client,
                        normalized_alias,
                        now,
                        compact_json(normalized_capabilities),
                        now,
                        now,
                    ),
                )

            room = conn.execute(
                "SELECT * FROM rooms WHERE conversation_id = ?",
                (normalized_conversation,),
            ).fetchone()
            room_created = False
            if room is None:
                if not create_room_if_missing:
                    raise NotFoundError(
                        f"unknown conversation: {normalized_conversation}; "
                        "set create_room_if_missing=true to create it"
                    )
                owned_count = self._agent_active_room_count(conn, participant_id)
                if owned_count >= AGENT_ACTIVE_ROOM_LIMIT:
                    raise ConflictError(
                        "this agent session already owns the maximum of "
                        f"{AGENT_ACTIVE_ROOM_LIMIT} active rooms"
                    )
                conn.execute(
                    """
                    INSERT INTO rooms
                        (conversation_id, status, creator_kind,
                         creator_participant_id, created_at, last_activity_at)
                    VALUES (?, 'active', 'agent', ?, ?, ?)
                    """,
                    (normalized_conversation, participant_id, now, now),
                )
                room_created = True
            elif str(room["status"]) != "active":
                raise ConflictError(
                    f"conversation {normalized_conversation} is abandoned and "
                    "cannot be joined"
                )

            conn.execute(
                """
                INSERT INTO memberships
                    (conversation_id, participant_id, roles_json, active,
                     joined_at, updated_at)
                VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(conversation_id, participant_id) DO UPDATE SET
                    roles_json = excluded.roles_json,
                    active = 1,
                    updated_at = excluded.updated_at
                """,
                (
                    normalized_conversation,
                    participant_id,
                    compact_json(normalized_roles),
                    now,
                    now,
                ),
            )
            owned_count = self._agent_active_room_count(conn, participant_id)
            profile = conn.execute(
                "SELECT display_name, signature FROM participants "
                "WHERE participant_id = ?",
                (participant_id,),
            ).fetchone()

        return {
            "participant_id": participant_id,
            "client_type": normalized_client,
            "session_alias": normalized_alias,
            "display_name": str(profile["display_name"]),
            "signature": str(profile["signature"]),
            "conversation_id": normalized_conversation,
            "roles": normalized_roles,
            "capabilities": normalized_capabilities,
            "status": "online",
            "room_created": room_created,
            "owned_active_room_count": owned_count,
            "owned_active_room_limit": AGENT_ACTIVE_ROOM_LIMIT,
        }

    def create_user_room(self, conversation_id: str) -> dict[str, Any]:
        """Create an owner-managed room without consuming an agent quota."""
        conversation = validate_conversation_id(conversation_id)
        now = time.time()
        with self._transaction() as conn:
            self._archive_stale_rooms_locked(conn, now=now)
            existing = conn.execute(
                "SELECT status FROM rooms WHERE conversation_id = ?",
                (conversation,),
            ).fetchone()
            if existing is not None:
                state = str(existing["status"])
                raise ConflictError(
                    f"conversation {conversation} already exists with status {state}"
                )
            conn.execute(
                """
                INSERT INTO rooms
                    (conversation_id, status, creator_kind,
                     creator_participant_id, created_at, last_activity_at)
                VALUES (?, 'active', 'user', NULL, ?, ?)
                """,
                (conversation, now, now),
            )
        return {
            "conversation_id": conversation,
            "status": "active",
            "creator_kind": "user",
            "created_at": now,
            "last_activity_at": now,
        }

    def create_agent_room(
        self,
        *,
        authorized_session_id: str,
        participant_id: str,
        conversation_id: str,
    ) -> dict[str, Any]:
        session = opaque_id(
            authorized_session_id,
            field="authorized_session_id",
        )
        participant = opaque_id(participant_id, field="participant_id")
        conversation = validate_conversation_id(conversation_id)
        now = time.time()
        with self._transaction() as conn:
            self._archive_stale_rooms_locked(conn, now=now)
            self._require_live_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=now,
            )
            existing = conn.execute(
                "SELECT status FROM rooms WHERE conversation_id = ?",
                (conversation,),
            ).fetchone()
            if existing is not None:
                raise ConflictError(
                    f"conversation {conversation} already exists with status "
                    f"{existing['status']}"
                )
            owned_count = self._agent_active_room_count(conn, participant)
            if owned_count >= AGENT_ACTIVE_ROOM_LIMIT:
                raise ConflictError(
                    "this agent session already owns the maximum of "
                    f"{AGENT_ACTIVE_ROOM_LIMIT} active rooms"
                )
            conn.execute(
                """
                INSERT INTO rooms
                    (conversation_id, status, creator_kind,
                     creator_participant_id, created_at, last_activity_at)
                VALUES (?, 'active', 'agent', ?, ?, ?)
                """,
                (conversation, participant, now, now),
            )
            conn.execute(
                """
                INSERT INTO memberships
                    (conversation_id, participant_id, roles_json, active,
                     joined_at, updated_at)
                VALUES (?, ?, '[]', 1, ?, ?)
                """,
                (conversation, participant, now, now),
            )
            owned_count += 1
        return {
            "conversation_id": conversation,
            "status": "active",
            "creator_kind": "agent",
            "creator_participant_id": participant,
            "created_at": now,
            "last_activity_at": now,
            "owned_active_room_count": owned_count,
            "owned_active_room_limit": AGENT_ACTIVE_ROOM_LIMIT,
        }

    def register_agent_session(
        self,
        *,
        product: str,
        username: str,
        session_alias: str | None = None,
        signature: str | None = None,
        conversation_id: str,
        roles: Sequence[str] | None = None,
        capabilities: Sequence[str] | None = None,
        session_ttl_seconds: float = DEFAULT_SESSION_TTL_SECONDS,
    ) -> dict[str, Any]:
        normalized_product = token(product, field="product_name")
        normalized_identity = product_username(normalized_product, username)
        if normalized_identity == OWNER_CLIENT_TYPE:
            raise ConflictError("this identity is reserved for the local web user")
        if not str(session_alias or "").strip() and not str(signature or "").strip():
            raise ValidationError("signature is required (session_alias remains supported)")
        normalized_alias = alias(session_alias or signature, field="session_alias")
        normalized_signature = alias(signature or session_alias, field="signature")
        conversation = validate_conversation_id(conversation_id)
        normalized_roles = string_tokens(roles, field="roles")
        normalized_capabilities = string_tokens(
            capabilities,
            field="capabilities",
        )
        session_ttl = max(300.0, min(float(session_ttl_seconds), 28_800.0))
        now = time.time()
        session_id = f"session_{uuid.uuid4().hex}"
        access_token = f"session_{secrets.token_urlsafe(32)}"

        with self._transaction() as conn:
            self._archive_stale_rooms_locked(conn, now=now)
            self._require_active_room(conn, conversation)
            existing = conn.execute(
                "SELECT * FROM participants WHERE client_type = ?",
                (normalized_identity,),
            ).fetchone()
            if existing is None:
                participant_id = f"participant_{uuid.uuid4().hex}"
                conn.execute(
                    """
                    INSERT INTO participants
                        (participant_id, client_type, session_alias,
                         display_name, signature, profile_updated_at,
                         capabilities_json, status, created_at, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'online', ?, ?)
                    """,
                    (
                        participant_id,
                        normalized_identity,
                        normalized_alias,
                        normalized_identity,
                        normalized_signature,
                        now,
                        compact_json(normalized_capabilities),
                        now,
                        now,
                    ),
                )
            else:
                participant_id = str(existing["participant_id"])
                # Old clients used session_alias for a per-process purpose and
                # may send a different value after reconnecting.  It is no
                # longer identity or profile authority, so accept and ignore
                # it for an existing stable product-username participant.
                conn.execute(
                    """
                    UPDATE participants
                    SET capabilities_json = ?,
                        signature = CASE WHEN ? = 1 THEN ? ELSE signature END,
                        profile_updated_at = ?,
                        status = 'online', last_seen = ?
                    WHERE participant_id = ?
                    """,
                    (
                        compact_json(normalized_capabilities),
                        1 if signature is not None else 0,
                        normalized_signature,
                        now,
                        now,
                        participant_id,
                    ),
                )

            conn.execute(
                """
                INSERT INTO memberships
                    (conversation_id, participant_id, roles_json, active,
                     joined_at, updated_at)
                VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(conversation_id, participant_id) DO UPDATE SET
                    roles_json = excluded.roles_json,
                    active = 1,
                    updated_at = excluded.updated_at
                """,
                (
                    conversation,
                    participant_id,
                    compact_json(normalized_roles),
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO agent_sessions
                    (session_id, participant_id, registered_conversation_id,
                     token_hash, transport, created_at, expires_at,
                     ttl_seconds, last_seen)
                VALUES (?, ?, ?, ?, 'mcp', ?, ?, ?, ?)
                """,
                (
                    session_id,
                    participant_id,
                    conversation,
                    self._secret_hash(access_token),
                    now,
                    now + session_ttl,
                    session_ttl,
                    now,
                ),
            )
            owned_count = self._agent_active_room_count(conn, participant_id)
            profile = conn.execute(
                "SELECT session_alias, display_name, signature FROM participants "
                "WHERE participant_id = ?",
                (participant_id,),
            ).fetchone()

        return {
            "participant_id": participant_id,
            "client_type": normalized_identity,
            "session_alias": str(profile["session_alias"]),
            "display_name": str(profile["display_name"]),
            "signature": str(profile["signature"]),
            "conversation_id": conversation,
            "roles": normalized_roles,
            "capabilities": normalized_capabilities,
            "status": "online",
            "session_id": session_id,
            "access_token": access_token,
            "session_expires_at": now + session_ttl,
            "session_ttl_seconds": session_ttl,
            "session_renewal_mode": "sliding",
            "owned_active_room_count": owned_count,
            "owned_active_room_limit": AGENT_ACTIVE_ROOM_LIMIT,
        }

    def authenticate_session(self, access_token: str) -> dict[str, Any]:
        normalized_token = opaque_id(access_token, field="access_token")
        now = time.time()
        with self._transaction() as conn:
            row = conn.execute(
                """
                SELECT session.*, participant.client_type,
                       participant.session_alias, participant.display_name,
                       participant.signature
                FROM agent_sessions AS session
                JOIN participants AS participant
                  ON participant.participant_id = session.participant_id
                WHERE session.token_hash = ? AND session.transport = 'mcp'
                """,
                (self._secret_hash(normalized_token),),
            ).fetchone()
            if row is None:
                raise AuthenticationError("invalid agent session token")
            if row["cleared_at"] is not None:
                raise AuthenticationError("agent session has been cleared")
            if row["revoked_at"] is not None:
                raise AuthenticationError("agent session has been revoked")
            if float(row["expires_at"]) <= now:
                raise AuthenticationError("agent session has expired")
            ttl_seconds = max(300.0, float(row["ttl_seconds"] or 0.0))
            renewed_expires_at = max(
                float(row["expires_at"]),
                now + ttl_seconds,
            )
            conn.execute(
                "UPDATE agent_sessions SET last_seen = ?, expires_at = ? "
                "WHERE session_id = ?",
                (now, renewed_expires_at, str(row["session_id"])),
            )
            conn.execute(
                "UPDATE participants SET status = 'online', last_seen = ? "
                "WHERE participant_id = ?",
                (now, str(row["participant_id"])),
            )
        return {
            "session_id": str(row["session_id"]),
            "participant_id": str(row["participant_id"]),
            "client_type": str(row["client_type"]),
            "session_alias": str(row["session_alias"]),
            "display_name": str(row["display_name"]),
            "signature": str(row["signature"]),
            "expires_at": renewed_expires_at,
            "ttl_seconds": ttl_seconds,
            "renewal_mode": "sliding",
        }

    def revoke_session(
        self,
        session_id: str,
        *,
        reason: str = "owner_revoked",
    ) -> dict[str, Any]:
        normalized_session = opaque_id(session_id, field="session_id")
        normalized_reason = alias(reason, field="reason")
        now = time.time()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT participant_id, revoked_at FROM agent_sessions "
                "WHERE session_id = ?",
                (normalized_session,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"unknown agent session: {normalized_session}")
            conn.execute(
                "UPDATE agent_sessions SET revoked_at = COALESCE(revoked_at, ?), "
                "revoked_reason = COALESCE(revoked_reason, ?) WHERE session_id = ?",
                (now, normalized_reason, normalized_session),
            )
            conn.execute(
                "UPDATE participants SET status = 'offline', last_seen = ? "
                "WHERE participant_id = ? AND NOT EXISTS ("
                "SELECT 1 FROM agent_sessions WHERE participant_id = ? "
                "AND revoked_at IS NULL AND expires_at > ?)",
                (
                    now,
                    str(row["participant_id"]),
                    str(row["participant_id"]),
                    now,
                ),
            )
        return {
            "session_id": normalized_session,
            "revoked": True,
            "revoked_at": float(row["revoked_at"] or now),
            "reason": normalized_reason,
        }

    def clear_inactive_sessions(self, *, now: float | None = None) -> dict[str, Any]:
        """Hide expired or revoked credentials without deleting audit links.

        Session ids are referenced by nickname approvals and historical message
        authorization records.  Logical clearing removes dead credentials from
        normal projections and permanently rejects their tokens while keeping
        those references intact.
        """
        cleared_at = time.time() if now is None else float(now)
        with self._transaction() as conn:
            cleared_count = conn.execute(
                """
                UPDATE agent_sessions
                SET cleared_at = ?
                WHERE cleared_at IS NULL
                  AND (revoked_at IS NOT NULL OR expires_at <= ?)
                """,
                (cleared_at, cleared_at),
            ).rowcount
            conn.execute(
                """
                UPDATE participants
                SET status = 'offline'
                WHERE status != 'offline'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM agent_sessions AS session
                      WHERE session.participant_id = participants.participant_id
                        AND session.cleared_at IS NULL
                        AND session.revoked_at IS NULL
                        AND session.expires_at > ?
                  )
                """,
                (cleared_at,),
            )
        return {
            "cleared_count": int(cleared_count),
            "cleared_at": cleared_at,
            "mode": "logical",
            "audit_links_preserved": True,
        }

    def archive_stale_rooms(self, *, now: float | None = None) -> dict[str, Any]:
        """Move rooms with no messages for 90 days into immutable abandonment."""
        archive_time = time.time() if now is None else float(now)
        cutoff = archive_time - ROOM_ABANDON_AFTER_SECONDS
        with self._connection() as conn:
            has_stale_room = conn.execute(
                "SELECT 1 FROM rooms WHERE status = 'active' "
                "AND last_activity_at <= ? LIMIT 1",
                (cutoff,),
            ).fetchone()
        if has_stale_room is None:
            archived: list[str] = []
            return {
                "archived_conversation_ids": archived,
                "count": 0,
                "abandon_after_seconds": ROOM_ABANDON_AFTER_SECONDS,
            }
        with self._transaction() as conn:
            archived = self._archive_stale_rooms_locked(conn, now=archive_time)
        return {
            "archived_conversation_ids": archived,
            "count": len(archived),
            "abandon_after_seconds": ROOM_ABANDON_AFTER_SECONDS,
        }

    def heartbeat(
        self,
        participant_id: str,
        *,
        status: str = "online",
        authorized_session_id: str | None = None,
    ) -> dict[str, Any]:
        participant = opaque_id(participant_id, field="participant_id")
        normalized_status = str(status or "").strip().lower()
        if normalized_status not in PRESENCE_STATES:
            raise ValidationError("status must be online or offline")
        now = time.time()
        with self._transaction() as conn:
            if authorized_session_id is not None:
                self._require_live_session(
                    conn,
                    session_id=opaque_id(
                        authorized_session_id,
                        field="authorized_session_id",
                    ),
                    participant_id=participant,
                    now=now,
                )
            updated = conn.execute(
                "UPDATE participants SET status = ?, last_seen = ? "
                "WHERE participant_id = ?",
                (normalized_status, now, participant),
            ).rowcount
            if not updated:
                raise NotFoundError(f"unknown participant: {participant}")
        return {
            "participant_id": participant,
            "status": normalized_status,
            "last_seen": now,
        }

    def update_profile(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        signature: str,
    ) -> dict[str, Any]:
        participant = opaque_id(participant_id, field="participant_id")
        session = opaque_id(authorized_session_id, field="authorized_session_id")
        normalized_signature = alias(signature, field="signature")
        now = time.time()
        with self._transaction() as conn:
            self._require_live_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=now,
            )
            updated = conn.execute(
                "UPDATE participants SET signature = ?, profile_updated_at = ?, "
                "last_seen = ? "
                "WHERE participant_id = ?",
                (normalized_signature, now, now, participant),
            ).rowcount
            if not updated:
                raise NotFoundError(f"unknown participant: {participant}")
            row = conn.execute(
                "SELECT * FROM participants WHERE participant_id = ?",
                (participant,),
            ).fetchone()
        return self._participant_profile_payload(row)

    def request_nickname(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        requested_display_name: str,
    ) -> dict[str, Any]:
        participant = opaque_id(participant_id, field="participant_id")
        session = opaque_id(authorized_session_id, field="authorized_session_id")
        requested = validate_display_name(requested_display_name)
        now = time.time()
        with self._transaction() as conn:
            self._require_live_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=now,
            )
            profile = conn.execute(
                "SELECT display_name FROM participants WHERE participant_id = ?",
                (participant,),
            ).fetchone()
            if profile is None:
                raise NotFoundError(f"unknown participant: {participant}")
            if str(profile["display_name"]).casefold() == requested.casefold():
                raise ConflictError("requested nickname is already active")
            collision = conn.execute(
                "SELECT participant_id FROM participants "
                "WHERE display_name = ? COLLATE NOCASE AND participant_id != ?",
                (requested, participant),
            ).fetchone()
            if collision is not None:
                raise ConflictError("requested nickname is already in use")
            pending = conn.execute(
                "SELECT request_id FROM nickname_requests "
                "WHERE participant_id = ? AND status = 'pending'",
                (participant,),
            ).fetchone()
            if pending is not None:
                raise ConflictError(
                    f"nickname request {pending['request_id']} is still pending"
                )
            latest = conn.execute(
                "SELECT requested_at FROM nickname_requests "
                "WHERE participant_id = ? ORDER BY requested_at DESC LIMIT 1",
                (participant,),
            ).fetchone()
            if latest is not None:
                retry_after = (
                    float(latest["requested_at"])
                    + NICKNAME_REQUEST_COOLDOWN_SECONDS
                    - now
                )
                if retry_after > 0:
                    raise NicknameRateLimitError(
                        retry_after_seconds=retry_after,
                    )
            request_id = f"nickname_{uuid.uuid4().hex}"
            conn.execute(
                """
                INSERT INTO nickname_requests
                    (request_id, participant_id, requested_display_name,
                     status, requested_at, requested_session_id)
                VALUES (?, ?, ?, 'pending', ?, ?)
                """,
                (request_id, participant, requested, now, session),
            )
            row = conn.execute(
                "SELECT request.*, profile.client_type, profile.display_name, "
                "profile.signature FROM nickname_requests AS request "
                "JOIN participants AS profile "
                "ON profile.participant_id = request.participant_id "
                "WHERE request.request_id = ?",
                (request_id,),
            ).fetchone()
        return self._nickname_request_payload(row)

    def list_nickname_requests(
        self,
        *,
        status: str | None = "pending",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        normalized_status = str(status or "").strip().lower()
        if normalized_status and normalized_status not in {
            "pending",
            "approved",
            "rejected",
        }:
            raise ValidationError("status must be pending, approved, or rejected")
        normalized_limit = max(1, min(int(limit), 500))
        where_clause = "WHERE request.status = ?" if normalized_status else ""
        parameters: tuple[Any, ...] = (
            (normalized_status, normalized_limit)
            if normalized_status
            else (normalized_limit,)
        )
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT request.*, profile.client_type, profile.display_name,
                       profile.signature
                FROM nickname_requests AS request
                JOIN participants AS profile
                  ON profile.participant_id = request.participant_id
                {where_clause}
                ORDER BY request.requested_at DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [self._nickname_request_payload(row) for row in rows]

    def review_nickname_request(
        self,
        *,
        request_id: str,
        action: str,
        review_note: str | None = None,
    ) -> dict[str, Any]:
        request = opaque_id(request_id, field="request_id")
        normalized_action = str(action or "").strip().lower()
        if normalized_action not in {"approve", "reject"}:
            raise ValidationError("action must be approve or reject")
        note = alias(review_note, field="review_note") if review_note else None
        now = time.time()
        status = "approved" if normalized_action == "approve" else "rejected"
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM nickname_requests WHERE request_id = ?",
                (request,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"unknown nickname request: {request}")
            if str(row["status"]) != "pending":
                raise ConflictError(
                    f"nickname request is already {row['status']}"
                )
            if status == "approved":
                requested = str(row["requested_display_name"])
                collision = conn.execute(
                    "SELECT participant_id FROM participants "
                    "WHERE display_name = ? COLLATE NOCASE AND participant_id != ?",
                    (requested, str(row["participant_id"])),
                ).fetchone()
                if collision is not None:
                    raise ConflictError("requested nickname is already in use")
                conn.execute(
                    "UPDATE participants SET display_name = ?, profile_updated_at = ? "
                    "WHERE participant_id = ?",
                    (requested, now, str(row["participant_id"])),
                )
            conn.execute(
                "UPDATE nickname_requests SET status = ?, reviewed_at = ?, "
                "review_note = ? WHERE request_id = ?",
                (status, now, note, request),
            )
            reviewed = conn.execute(
                "SELECT request.*, profile.client_type, profile.display_name, "
                "profile.signature FROM nickname_requests AS request "
                "JOIN participants AS profile "
                "ON profile.participant_id = request.participant_id "
                "WHERE request.request_id = ?",
                (request,),
            ).fetchone()
        return self._nickname_request_payload(reviewed)

    def set_follow(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        conversation_id: str,
        followed_participant_id: str,
        following: bool = True,
    ) -> dict[str, Any]:
        follower = opaque_id(participant_id, field="participant_id")
        followed = opaque_id(
            followed_participant_id,
            field="followed_participant_id",
        )
        if follower == followed:
            raise ConflictError("an Agent cannot follow itself")
        session = opaque_id(authorized_session_id, field="authorized_session_id")
        conversation = validate_conversation_id(conversation_id)
        now = time.time()
        with self._transaction() as conn:
            self._require_live_session(
                conn,
                session_id=session,
                participant_id=follower,
                now=now,
            )
            self._require_membership(conn, follower, conversation)
            self._require_membership(conn, followed, conversation)
            conn.execute(
                """
                INSERT INTO follows
                    (conversation_id, follower_participant_id,
                     followed_participant_id, active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    conversation_id,
                    follower_participant_id,
                    followed_participant_id
                ) DO UPDATE SET
                    active = excluded.active,
                    updated_at = excluded.updated_at
                """,
                (
                    conversation,
                    follower,
                    followed,
                    1 if following else 0,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT follow.*, profile.client_type, profile.display_name,
                       profile.signature
                FROM follows AS follow
                JOIN participants AS profile
                  ON profile.participant_id = follow.followed_participant_id
                WHERE follow.conversation_id = ?
                  AND follow.follower_participant_id = ?
                  AND follow.followed_participant_id = ?
                """,
                (conversation, follower, followed),
            ).fetchone()
        return self._follow_payload(row)

    def following(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        conversation_id: str,
        include_inactive: bool = False,
    ) -> dict[str, Any]:
        follower = opaque_id(participant_id, field="participant_id")
        session = opaque_id(authorized_session_id, field="authorized_session_id")
        conversation = validate_conversation_id(conversation_id)
        with self._connection() as conn:
            now = time.time()
            self._require_live_session(
                conn,
                session_id=session,
                participant_id=follower,
                now=now,
            )
            self._require_membership(conn, follower, conversation)
            active_filter = "" if include_inactive else "AND follow.active = 1"
            rows = conn.execute(
                """
                SELECT follow.*, profile.client_type, profile.display_name,
                       profile.signature
                FROM follows AS follow
                JOIN participants AS profile
                  ON profile.participant_id = follow.followed_participant_id
                WHERE follow.conversation_id = ?
                  AND follow.follower_participant_id = ?
                """
                f" {active_filter} "
                "ORDER BY profile.display_name, follow.followed_participant_id",
                (conversation, follower),
            ).fetchall()
        follows = [self._follow_payload(row) for row in rows]
        return {
            "conversation_id": conversation,
            "participant_id": follower,
            "following": follows,
            "count": len(follows),
        }

    def send(
        self,
        *,
        authorized_session_id: str,
        sender_participant_id: str,
        conversation_id: str,
        body_text: str,
        audience_kind: str = "room",
        audience_value: str = "*",
        reply_to: str | None = None,
        refs: Sequence[dict[str, Any]] | None = None,
        mentions: Sequence[str] | None = None,
        _owner_ui: bool = False,
    ) -> dict[str, Any]:
        session = opaque_id(
            authorized_session_id,
            field="authorized_session_id",
        )
        sender = opaque_id(sender_participant_id, field="sender_participant_id")
        conversation = validate_conversation_id(conversation_id)
        normalized_body = body(body_text)
        normalized_audience = str(audience_kind or "").strip().lower()
        if normalized_audience not in AUDIENCE_KINDS:
            raise ValidationError(f"unsupported audience_kind: {normalized_audience}")
        normalized_refs = message_refs(refs)
        normalized_mentions = self._normalize_mentions(mentions)
        normalized_reply = (
            opaque_id(reply_to, field="reply_to") if reply_to else None
        )
        normalized_target = self._normalize_audience_value(
            normalized_audience,
            audience_value,
            conversation,
        )
        if (
            normalized_audience == "participant"
            and normalized_target not in normalized_mentions
        ):
            normalized_mentions.append(normalized_target)
        now = time.time()
        message_id = f"msg_{uuid.uuid4().hex}"

        with self._transaction() as conn:
            self._archive_stale_rooms_locked(conn, now=now)
            if _owner_ui:
                if session != OWNER_AUTHORIZATION_ID or sender != OWNER_PARTICIPANT_ID:
                    raise AuthenticationError("invalid owner UI sender binding")
                self._require_active_room(conn, conversation)
                self._ensure_owner_membership_locked(
                    conn,
                    conversation_id=conversation,
                    now=now,
                )
            else:
                self._require_live_session(
                    conn,
                    session_id=session,
                    participant_id=sender,
                    now=now,
                )
                self._require_membership(conn, sender, conversation)
            if normalized_audience == "participant":
                self._require_membership(conn, normalized_target, conversation)
            if normalized_reply:
                original = conn.execute(
                    "SELECT conversation_id, reply_to FROM messages "
                    "WHERE message_id = ?",
                    (normalized_reply,),
                ).fetchone()
                if original is None:
                    raise NotFoundError(f"unknown reply_to message: {normalized_reply}")
                if str(original["conversation_id"]) != conversation:
                    raise ConflictError("reply_to belongs to a different conversation")
                if original["reply_to"] is not None:
                    raise ConflictError(
                        "reply chains are limited to one level; continue the "
                        "conversation with a new message"
                    )
            self._assert_speaking_cooldown(
                conn,
                participant_id=sender,
                conversation_id=conversation,
                now=now,
            )
            try:
                conn.execute(
                    """
                    INSERT INTO messages
                        (message_id, conversation_id, sender_participant_id,
                         audience_kind, audience_value, message_kind, body,
                         refs_json, mentions_json, reply_to, status,
                         authorized_session_id,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 'message', ?, ?, ?, ?, 'open', ?, ?, ?)
                    """,
                    (
                        message_id,
                        conversation,
                        sender,
                        normalized_audience,
                        normalized_target,
                        normalized_body,
                        compact_json(normalized_refs),
                        compact_json(normalized_mentions),
                        normalized_reply,
                        session,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                error_text = str(exc)
                if "MESSAGE_RATE_LIMITED" in error_text:
                    self._assert_speaking_cooldown(
                        conn,
                        participant_id=sender,
                        conversation_id=conversation,
                        now=now,
                    )
                if "REPLY_CHAIN_NOT_ALLOWED" in error_text:
                    raise ConflictError(
                        "reply chains are limited to one level; continue the "
                        "conversation with a new message"
                    ) from exc
                if (
                    "LIVE_MCP_SESSION_REQUIRED" in error_text
                    or "AUTHORIZED_SENDER_REQUIRED" in error_text
                ):
                    raise AuthenticationError(
                        "an authenticated Agent session or owner UI action is "
                        "required to chat"
                    ) from exc
                raise
            row = conn.execute(
                "SELECT * FROM messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            self._create_message_deliveries_locked(conn, row)
        return self._message_payload(row)

    def send_owner_message(
        self,
        *,
        conversation_id: str,
        body_text: str,
        mentions: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Send one owner-authored room message through the local web authority."""
        return self.send(
            authorized_session_id=OWNER_AUTHORIZATION_ID,
            sender_participant_id=OWNER_PARTICIPANT_ID,
            conversation_id=conversation_id,
            body_text=body_text,
            audience_kind="room",
            audience_value="*",
            mentions=mentions,
            _owner_ui=True,
        )

    def wait_messages(
        self,
        *,
        participant_id: str,
        authorized_session_id: str | None = None,
        wait_seconds: float = 30.0,
        limit: int = 20,
        auto_claim_roles: bool = True,
    ) -> dict[str, Any]:
        participant = opaque_id(participant_id, field="participant_id")
        wait_for = max(0.0, min(float(wait_seconds), 120.0))
        normalized_limit = max(1, min(int(limit), 100))
        deadline = time.monotonic() + wait_for
        self.archive_stale_rooms()

        while True:
            messages = self._pending_messages(
                participant,
                limit=normalized_limit,
                auto_claim_roles=bool(auto_claim_roles),
                authorized_session_id=authorized_session_id,
            )
            if messages:
                backlog = self._pending_manifest(participant)
                return {
                    "participant_id": participant,
                    "messages": messages,
                    "count": len(messages),
                    "timed_out": False,
                    "last_sequence": max(item["sequence"] for item in messages),
                    "backlog": backlog,
                    "pending_count": backlog["pending_count"],
                    "has_more": backlog["pending_count"] > len(messages),
                }
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.heartbeat(
                    participant,
                    authorized_session_id=authorized_session_id,
                )
                backlog = self._pending_manifest(participant)
                return {
                    "participant_id": participant,
                    "messages": [],
                    "count": 0,
                    "timed_out": True,
                    "last_sequence": None,
                    "backlog": backlog,
                    "pending_count": backlog["pending_count"],
                    "has_more": backlog["pending_count"] > 0,
                }
            time.sleep(min(self.poll_interval_seconds, remaining))

    def notification_snapshot(
        self,
        *,
        participant_id: str,
        authorized_session_id: str | None = None,
        after_sequence: int | None = None,
    ) -> dict[str, Any]:
        """Return wake-up metadata without reading bodies or consuming delivery state."""
        participant = opaque_id(participant_id, field="participant_id")
        requested_cursor = max(0, int(after_sequence or 0))
        now = time.time()
        with self._transaction() as conn:
            self._archive_stale_rooms_locked(conn, now=now)
            if authorized_session_id is not None:
                self._require_live_session(
                    conn,
                    session_id=opaque_id(
                        authorized_session_id,
                        field="authorized_session_id",
                    ),
                    participant_id=participant,
                    now=now,
                )
            known = conn.execute(
                "SELECT participant_id FROM participants WHERE participant_id = ?",
                (participant,),
            ).fetchone()
            if known is None:
                raise NotFoundError(f"unknown participant: {participant}")
            global_sequence = int(
                conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) FROM messages"
                ).fetchone()[0]
            )
        # A corrupt or manually edited Last-Event-ID must not suppress every
        # future event forever.  Global message sequence is monotonic, so it is
        # the largest cursor the server can currently have issued.
        cursor = min(requested_cursor, global_sequence)
        backlog = self._pending_manifest(participant)
        new_since_cursor = self._pending_manifest(
            participant,
            after_sequence=cursor,
        )
        room_activity_since_cursor = self._activity_manifest(
            participant,
            after_sequence=cursor,
        )
        return {
            "participant_id": participant,
            # Cursor tracks the global append-only sequence, not unread state.
            # This keeps "the room changed" separate from "I still owe an ack".
            "cursor": global_sequence,
            "has_new": new_since_cursor["pending_count"] > 0,
            "has_room_activity": room_activity_since_cursor["activity_count"] > 0,
            "backlog": backlog,
            "new_since_cursor": new_since_cursor,
            "room_activity_since_cursor": room_activity_since_cursor,
            "server_time": time.time(),
        }

    def wait_for_notification(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        after_sequence: int | None = None,
        wait_seconds: float = 25.0,
    ) -> dict[str, Any]:
        """Wait for a durable delivery newer than a sequence cursor.

        This is intentionally a low-cost database wait. It never marks a message
        delivered or acknowledged, so reconnecting listeners can always rebuild
        state from the authoritative delivery ledger.
        """
        wait_for = max(0.0, min(float(wait_seconds), 60.0))
        deadline = time.monotonic() + wait_for
        while True:
            snapshot = self.notification_snapshot(
                participant_id=participant_id,
                authorized_session_id=authorized_session_id,
                after_sequence=after_sequence,
            )
            if snapshot["has_room_activity"]:
                snapshot["timed_out"] = False
                return snapshot
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                snapshot["timed_out"] = True
                return snapshot
            time.sleep(min(max(self.poll_interval_seconds, 0.5), remaining))

    def message_action(
        self,
        *,
        participant_id: str,
        message_id: str,
        action: str,
        lease_seconds: float = 120.0,
        authorized_session_id: str | None = None,
    ) -> dict[str, Any]:
        participant = opaque_id(participant_id, field="participant_id")
        message = opaque_id(message_id, field="message_id")
        normalized_action = str(action or "").strip().lower()
        if normalized_action not in MESSAGE_ACTIONS:
            raise ValidationError(f"unsupported action: {normalized_action}")
        if normalized_action == "claim":
            return self._claim(
                participant,
                message,
                lease_seconds=lease_seconds,
                authorized_session_id=authorized_session_id,
            )
        if normalized_action == "release":
            return self._release(
                participant,
                message,
                authorized_session_id=authorized_session_id,
            )
        return self._ack(
            participant,
            message,
            authorized_session_id=authorized_session_id,
        )

    def reply(
        self,
        *,
        authorized_session_id: str,
        participant_id: str,
        message_id: str,
        body_text: str,
        refs: Sequence[dict[str, Any]] | None = None,
        mentions: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        participant = opaque_id(participant_id, field="participant_id")
        original_id = opaque_id(message_id, field="message_id")
        with self._connection() as conn:
            original = conn.execute(
                "SELECT * FROM messages WHERE message_id = ?",
                (original_id,),
            ).fetchone()
            self._require_live_session(
                conn,
                session_id=opaque_id(
                    authorized_session_id,
                    field="authorized_session_id",
                ),
                participant_id=participant,
                now=time.time(),
            )
        if original is None:
            raise NotFoundError(f"unknown message: {original_id}")
        if original["reply_to"] is not None:
            raise ConflictError(
                "reply chains are limited to one level; continue the "
                "conversation with a new message"
            )
        self._require_eligible_participant(participant, original_id)
        claim_acquired = False
        actionable = self._delivery_is_actionable(participant, original_id)
        if actionable and str(original["audience_kind"]) in {"participant", "role"}:
            claim_now = time.time()
            claim_acquired = not (
                str(original["claimed_by"] or "") == participant
                and float(original["claim_until"] or 0.0) > claim_now
            )
            self._claim(
                participant,
                original_id,
                lease_seconds=120.0,
                authorized_session_id=authorized_session_id,
            )
        try:
            reply_message = self.send(
                authorized_session_id=authorized_session_id,
                sender_participant_id=participant,
                conversation_id=str(original["conversation_id"]),
                body_text=body_text,
                audience_kind="participant",
                audience_value=str(original["sender_participant_id"]),
                reply_to=original_id,
                refs=refs,
                mentions=mentions,
            )
        except Exception:
            if claim_acquired:
                try:
                    self._release(participant, original_id)
                except BridgeError:
                    pass
            raise
        self._ack(participant, original_id)
        return {
            "reply": reply_message,
            "original_message_id": original_id,
            "original_acked": True,
        }

    def history(
        self,
        *,
        participant_id: str,
        conversation_id: str,
        limit: int = 50,
        before_sequence: int | None = None,
        after_sequence: int | None = None,
        authorized_session_id: str | None = None,
    ) -> dict[str, Any]:
        participant = opaque_id(participant_id, field="participant_id")
        conversation = validate_conversation_id(conversation_id)
        normalized_limit = max(1, min(int(limit), 200))
        if before_sequence is not None and after_sequence is not None:
            raise ValidationError(
                "before_sequence and after_sequence cannot be used together"
            )
        with self._transaction() as conn:
            now = time.time()
            self._archive_stale_rooms_locked(conn, now=now)
            if authorized_session_id is not None:
                self._require_live_session(
                    conn,
                    session_id=opaque_id(
                        authorized_session_id,
                        field="authorized_session_id",
                    ),
                    participant_id=participant,
                    now=now,
                )
            self._require_membership(conn, participant, conversation)
            if after_sequence is not None:
                rows = conn.execute(
                    "SELECT * FROM messages WHERE conversation_id = ? "
                    "AND sequence > ? ORDER BY sequence LIMIT ?",
                    (conversation, int(after_sequence), normalized_limit),
                ).fetchall()
            elif before_sequence is None:
                rows = conn.execute(
                    "SELECT * FROM messages WHERE conversation_id = ? "
                    "ORDER BY sequence DESC LIMIT ?",
                    (conversation, normalized_limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM messages WHERE conversation_id = ? "
                    "AND sequence < ? ORDER BY sequence DESC LIMIT ?",
                    (conversation, int(before_sequence), normalized_limit),
                ).fetchall()
        ordered_rows = rows if after_sequence is not None else list(reversed(rows))
        messages = [self._message_payload(row) for row in ordered_rows]
        first_sequence = messages[0]["sequence"] if messages else None
        last_sequence = messages[-1]["sequence"] if messages else None
        with self._connection() as conn:
            if after_sequence is not None:
                has_more = bool(
                    last_sequence is not None
                    and conn.execute(
                        "SELECT 1 FROM messages WHERE conversation_id = ? "
                        "AND sequence > ? LIMIT 1",
                        (conversation, last_sequence),
                    ).fetchone()
                )
            else:
                has_more = bool(
                    first_sequence is not None
                    and conn.execute(
                        "SELECT 1 FROM messages WHERE conversation_id = ? "
                        "AND sequence < ? LIMIT 1",
                        (conversation, first_sequence),
                    ).fetchone()
                )
        return {
            "conversation_id": conversation,
            "messages": messages,
            "count": len(messages),
            "first_sequence": first_sequence,
            "last_sequence": last_sequence,
            "has_more": has_more,
            "next_after_sequence": (
                last_sequence if after_sequence is not None and has_more else None
            ),
        }

    def participants(
        self,
        *,
        participant_id: str,
        conversation_id: str,
        include_offline: bool = True,
        online_window_seconds: float = 90.0,
        authorized_session_id: str | None = None,
    ) -> dict[str, Any]:
        caller = opaque_id(participant_id, field="participant_id")
        conversation = validate_conversation_id(conversation_id)
        now = time.time()
        with self._transaction() as conn:
            self._archive_stale_rooms_locked(conn, now=now)
            if authorized_session_id is not None:
                self._require_live_session(
                    conn,
                    session_id=opaque_id(
                        authorized_session_id,
                        field="authorized_session_id",
                    ),
                    participant_id=caller,
                    now=now,
                )
            self._require_membership(conn, caller, conversation)
            rows = conn.execute(
                """
                SELECT p.*, m.roles_json
                FROM memberships AS m
                JOIN participants AS p ON p.participant_id = m.participant_id
                WHERE m.conversation_id = ? AND m.active = 1
                ORDER BY p.display_name, p.participant_id
                """,
                (conversation,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            online = (
                str(row["status"]) == "online"
                and now - float(row["last_seen"]) <= float(online_window_seconds)
            )
            if not include_offline and not online:
                continue
            result.append(
                {
                    "participant_id": str(row["participant_id"]),
                    "client_type": str(row["client_type"]),
                    "session_alias": str(row["session_alias"]),
                    "display_name": str(row["display_name"]),
                    "signature": str(row["signature"]),
                    "roles": json.loads(str(row["roles_json"])),
                    "capabilities": json.loads(str(row["capabilities_json"])),
                    "status": "online" if online else "offline",
                    "last_seen": float(row["last_seen"]),
                }
            )
        return {
            "conversation_id": conversation,
            "participants": result,
            "count": len(result),
        }

    def _pending_messages(
        self,
        participant_id: str,
        *,
        limit: int,
        auto_claim_roles: bool,
        authorized_session_id: str | None,
    ) -> list[dict[str, Any]]:
        now = time.time()
        with self._connection() as conn:
            if authorized_session_id is not None:
                self._require_live_session(
                    conn,
                    session_id=opaque_id(
                        authorized_session_id,
                        field="authorized_session_id",
                    ),
                    participant_id=participant_id,
                    now=now,
                )
            participant = conn.execute(
                "SELECT * FROM participants WHERE participant_id = ?",
                (participant_id,),
            ).fetchone()
            if participant is None:
                raise NotFoundError(f"unknown participant: {participant_id}")
            rows = conn.execute(
                """
                SELECT message.*,
                       delivery.state AS delivery_state,
                       delivery.reasons_json AS delivery_reasons_json,
                       delivery.priority AS delivery_priority,
                       delivery.actionable AS delivery_actionable,
                       delivery.first_delivered_at AS delivery_first_delivered_at,
                       delivery.last_delivered_at AS delivery_last_delivered_at,
                       delivery.acked_at AS delivery_acked_at,
                       delivery.attempt_count AS delivery_attempt_count
                FROM message_deliveries AS delivery
                JOIN messages AS message
                  ON message.message_id = delivery.message_id
                JOIN memberships AS membership
                  ON membership.conversation_id = message.conversation_id
                 AND membership.participant_id = delivery.participant_id
                 AND membership.active = 1
                JOIN rooms AS room
                  ON room.conversation_id = message.conversation_id
                 AND room.status = 'active'
                WHERE delivery.participant_id = ?
                  AND delivery.state IN ('pending', 'delivered')
                  AND message.sender_participant_id != ?
                ORDER BY message.sequence
                LIMIT 500
                """,
                (participant_id, participant_id),
            ).fetchall()

        selected: list[sqlite3.Row] = []
        for row in rows:
            claim_until = float(row["claim_until"] or 0.0)
            claimed_by = str(row["claimed_by"] or "")
            actionable = bool(row["delivery_actionable"])
            if (
                actionable
                and claimed_by
                and claimed_by != participant_id
                and claim_until > now
            ):
                continue
            if (
                actionable
                and
                str(row["audience_kind"]) == "role"
                and auto_claim_roles
            ):
                try:
                    self._claim(
                        participant_id,
                        str(row["message_id"]),
                        lease_seconds=120.0,
                        authorized_session_id=authorized_session_id,
                    )
                except ConflictError:
                    continue
                with self._connection() as conn:
                    row = conn.execute(
                        """
                        SELECT message.*,
                               delivery.state AS delivery_state,
                               delivery.reasons_json AS delivery_reasons_json,
                               delivery.priority AS delivery_priority,
                               delivery.actionable AS delivery_actionable,
                               delivery.first_delivered_at
                                   AS delivery_first_delivered_at,
                               delivery.last_delivered_at
                                   AS delivery_last_delivered_at,
                               delivery.acked_at AS delivery_acked_at,
                               delivery.attempt_count AS delivery_attempt_count
                        FROM messages AS message
                        JOIN message_deliveries AS delivery
                          ON delivery.message_id = message.message_id
                        WHERE message.message_id = ?
                          AND delivery.participant_id = ?
                        """,
                        (str(row["message_id"]), participant_id),
                    ).fetchone()
            selected.append(row)
            if len(selected) >= limit:
                break

        if not selected:
            return []
        delivered_at = time.time()
        delivered_rows: list[sqlite3.Row] = []
        with self._transaction() as conn:
            self._archive_stale_rooms_locked(conn, now=delivered_at)
            if authorized_session_id is not None:
                self._require_live_session(
                    conn,
                    session_id=opaque_id(
                        authorized_session_id,
                        field="authorized_session_id",
                    ),
                    participant_id=participant_id,
                    now=delivered_at,
                )
            conn.execute(
                "UPDATE participants SET status = 'online', last_seen = ? "
                "WHERE participant_id = ?",
                (delivered_at, participant_id),
            )
            for row in selected:
                try:
                    self._require_membership(
                        conn,
                        participant_id,
                        str(row["conversation_id"]),
                    )
                except (ConflictError, NotFoundError):
                    continue
                updated = conn.execute(
                    """
                    UPDATE message_deliveries
                    SET state = 'delivered',
                        first_delivered_at = COALESCE(first_delivered_at, ?),
                        last_delivered_at = ?,
                        attempt_count = attempt_count + 1
                    WHERE message_id = ? AND participant_id = ?
                      AND state IN ('pending', 'delivered')
                    """,
                    (
                        delivered_at,
                        delivered_at,
                        str(row["message_id"]),
                        participant_id,
                    ),
                ).rowcount
                if not updated:
                    continue
                conn.execute(
                    """
                    INSERT INTO receipts
                        (message_id, participant_id, state, delivered_at)
                    VALUES (?, ?, 'delivered', ?)
                    ON CONFLICT(message_id, participant_id) DO UPDATE SET
                        state = CASE
                            WHEN receipts.state = 'acked' THEN 'acked'
                            ELSE 'delivered'
                        END,
                        delivered_at = COALESCE(receipts.delivered_at, excluded.delivered_at)
                    """,
                    (str(row["message_id"]), participant_id, delivered_at),
                )
                delivered = conn.execute(
                    """
                    SELECT message.*,
                           delivery.state AS delivery_state,
                           delivery.reasons_json AS delivery_reasons_json,
                           delivery.priority AS delivery_priority,
                           delivery.actionable AS delivery_actionable,
                           delivery.first_delivered_at
                               AS delivery_first_delivered_at,
                           delivery.last_delivered_at
                               AS delivery_last_delivered_at,
                           delivery.acked_at AS delivery_acked_at,
                           delivery.attempt_count AS delivery_attempt_count
                    FROM messages AS message
                    JOIN message_deliveries AS delivery
                      ON delivery.message_id = message.message_id
                    WHERE message.message_id = ?
                      AND delivery.participant_id = ?
                    """,
                    (str(row["message_id"]), participant_id),
                ).fetchone()
                if delivered is not None:
                    delivered_rows.append(delivered)
        return [self._message_payload(row) for row in delivered_rows]

    def _pending_manifest(
        self,
        participant_id: str,
        *,
        after_sequence: int | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        sequence_clause = ""
        parameters: list[Any] = [participant_id, participant_id]
        if after_sequence is not None:
            sequence_clause = "AND message.sequence > ?"
            parameters.append(max(0, int(after_sequence)))
        parameters.extend((participant_id, now))
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT message.conversation_id,
                       COUNT(*) AS pending_count,
                       MIN(message.sequence) AS oldest_sequence,
                       MAX(message.sequence) AS newest_sequence,
                       MIN(message.created_at) AS oldest_created_at,
                       MAX(message.created_at) AS newest_created_at,
                       SUM(CASE WHEN delivery.priority IN ('direct', 'mention')
                                THEN 1 ELSE 0 END) AS mention_count,
                       SUM(CASE WHEN delivery.priority = 'important' THEN 1 ELSE 0 END)
                           AS important_count,
                       SUM(CASE WHEN delivery.priority = 'normal' THEN 1 ELSE 0 END)
                           AS normal_count
                FROM message_deliveries AS delivery
                JOIN messages AS message
                  ON message.message_id = delivery.message_id
                JOIN memberships AS membership
                  ON membership.conversation_id = message.conversation_id
                 AND membership.participant_id = delivery.participant_id
                 AND membership.active = 1
                JOIN rooms AS room
                  ON room.conversation_id = message.conversation_id
                 AND room.status = 'active'
                WHERE delivery.participant_id = ?
                  AND delivery.state IN ('pending', 'delivered')
                  AND message.sender_participant_id != ?
                  {sequence_clause}
                  AND (
                      delivery.actionable = 0
                      OR message.claimed_by IS NULL
                      OR message.claimed_by = ''
                      OR message.claimed_by = ?
                      OR COALESCE(message.claim_until, 0) <= ?
                  )
                GROUP BY message.conversation_id
                ORDER BY oldest_sequence
                """,
                parameters,
            ).fetchall()
        conversations = [
            {
                "conversation_id": str(row["conversation_id"]),
                "pending_count": int(row["pending_count"]),
                "oldest_sequence": int(row["oldest_sequence"]),
                "newest_sequence": int(row["newest_sequence"]),
                "oldest_created_at": float(row["oldest_created_at"]),
                "newest_created_at": float(row["newest_created_at"]),
                "priority_counts": {
                    "mention": int(row["mention_count"] or 0),
                    "important": int(row["important_count"] or 0),
                    "normal": int(row["normal_count"] or 0),
                },
            }
            for row in rows
        ]
        priority_counts = {
            priority: sum(
                int(item["priority_counts"][priority]) for item in conversations
            )
            for priority in ("mention", "important", "normal")
        }
        pending_count = sum(int(item["pending_count"]) for item in conversations)
        return {
            "pending_count": pending_count,
            "priority_counts": priority_counts,
            "oldest_sequence": (
                min(item["oldest_sequence"] for item in conversations)
                if conversations
                else None
            ),
            "newest_sequence": (
                max(item["newest_sequence"] for item in conversations)
                if conversations
                else None
            ),
            "conversations": conversations,
        }

    def _activity_manifest(
        self,
        participant_id: str,
        *,
        after_sequence: int,
    ) -> dict[str, Any]:
        """Summarize visible room activity independently from unread state."""
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT message.conversation_id,
                       COUNT(*) AS activity_count,
                       MIN(message.sequence) AS oldest_sequence,
                       MAX(message.sequence) AS newest_sequence,
                       SUM(CASE WHEN delivery.priority IN ('direct', 'mention')
                                THEN 1 ELSE 0 END) AS mention_count,
                       SUM(CASE WHEN delivery.priority = 'important' THEN 1 ELSE 0 END)
                           AS important_count,
                       SUM(CASE WHEN delivery.priority = 'normal' THEN 1 ELSE 0 END)
                           AS normal_count
                FROM message_deliveries AS delivery
                JOIN messages AS message
                  ON message.message_id = delivery.message_id
                JOIN memberships AS membership
                  ON membership.conversation_id = message.conversation_id
                 AND membership.participant_id = delivery.participant_id
                 AND membership.active = 1
                JOIN rooms AS room
                  ON room.conversation_id = message.conversation_id
                 AND room.status = 'active'
                WHERE delivery.participant_id = ?
                  AND delivery.state != 'cancelled'
                  AND message.sender_participant_id != ?
                  AND message.sequence > ?
                GROUP BY message.conversation_id
                ORDER BY oldest_sequence
                """,
                (
                    participant_id,
                    participant_id,
                    max(0, int(after_sequence)),
                ),
            ).fetchall()
        conversations = [
            {
                "conversation_id": str(row["conversation_id"]),
                "activity_count": int(row["activity_count"]),
                "oldest_sequence": int(row["oldest_sequence"]),
                "newest_sequence": int(row["newest_sequence"]),
                "priority_counts": {
                    "mention": int(row["mention_count"] or 0),
                    "important": int(row["important_count"] or 0),
                    "normal": int(row["normal_count"] or 0),
                },
            }
            for row in rows
        ]
        return {
            "activity_count": sum(
                int(item["activity_count"]) for item in conversations
            ),
            "oldest_sequence": (
                min(item["oldest_sequence"] for item in conversations)
                if conversations
                else None
            ),
            "newest_sequence": (
                max(item["newest_sequence"] for item in conversations)
                if conversations
                else None
            ),
            "conversations": conversations,
        }

    def _claim(
        self,
        participant_id: str,
        message_id: str,
        *,
        lease_seconds: float,
        authorized_session_id: str | None = None,
    ) -> dict[str, Any]:
        participant = opaque_id(participant_id, field="participant_id")
        message = opaque_id(message_id, field="message_id")
        lease = max(5.0, min(float(lease_seconds), 3_600.0))
        now = time.time()
        claim_until = now + lease
        with self._transaction() as conn:
            self._archive_stale_rooms_locked(conn, now=now)
            if authorized_session_id is not None:
                self._require_live_session(
                    conn,
                    session_id=opaque_id(
                        authorized_session_id,
                        field="authorized_session_id",
                    ),
                    participant_id=participant,
                    now=now,
                )
            row = conn.execute(
                "SELECT * FROM messages WHERE message_id = ?",
                (message,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"unknown message: {message}")
            delivery = self._require_eligible_row(conn, participant, row)
            if str(row["audience_kind"]) not in {"participant", "role"}:
                raise ConflictError(
                    "room and broadcast messages use per-participant receipts"
                )
            if not bool(delivery["actionable"]):
                raise ConflictError(
                    "this participant may read and acknowledge the group message "
                    "but is not an actionable @ recipient"
                )
            existing_claim = str(row["claimed_by"] or "")
            existing_until = float(row["claim_until"] or 0.0)
            if existing_claim and existing_claim != participant and existing_until > now:
                raise ConflictError(
                    f"message is claimed by {existing_claim} until {existing_until}"
                )
            conn.execute(
                "UPDATE messages SET claimed_by = ?, claim_until = ?, updated_at = ? "
                "WHERE message_id = ?",
                (participant, claim_until, now, message),
            )
        return {
            "message_id": message,
            "action": "claim",
            "claimed_by": participant,
            "claim_until": claim_until,
        }

    def _release(
        self,
        participant_id: str,
        message_id: str,
        *,
        authorized_session_id: str | None = None,
    ) -> dict[str, Any]:
        participant = opaque_id(participant_id, field="participant_id")
        message = opaque_id(message_id, field="message_id")
        with self._transaction() as conn:
            now = time.time()
            self._archive_stale_rooms_locked(conn, now=now)
            if authorized_session_id is not None:
                self._require_live_session(
                    conn,
                    session_id=opaque_id(
                        authorized_session_id,
                        field="authorized_session_id",
                    ),
                    participant_id=participant,
                    now=now,
                )
            row = conn.execute(
                "SELECT * FROM messages WHERE message_id = ?",
                (message,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"unknown message: {message}")
            self._require_active_room(conn, str(row["conversation_id"]))
            if str(row["claimed_by"] or "") != participant:
                raise ConflictError("only the current claimant can release a message")
            conn.execute(
                "UPDATE messages SET claimed_by = NULL, claim_until = NULL, "
                "updated_at = ? WHERE message_id = ?",
                (time.time(), message),
            )
        return {"message_id": message, "action": "release", "released": True}

    def _ack(
        self,
        participant_id: str,
        message_id: str,
        *,
        authorized_session_id: str | None = None,
    ) -> dict[str, Any]:
        participant = opaque_id(participant_id, field="participant_id")
        message = opaque_id(message_id, field="message_id")
        now = time.time()
        with self._transaction() as conn:
            self._archive_stale_rooms_locked(conn, now=now)
            if authorized_session_id is not None:
                self._require_live_session(
                    conn,
                    session_id=opaque_id(
                        authorized_session_id,
                        field="authorized_session_id",
                    ),
                    participant_id=participant,
                    now=now,
                )
            row = conn.execute(
                "SELECT * FROM messages WHERE message_id = ?",
                (message,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"unknown message: {message}")
            delivery = self._require_eligible_row(conn, participant, row)
            actionable = bool(delivery["actionable"])
            claimed_by = str(row["claimed_by"] or "")
            claim_until = float(row["claim_until"] or 0.0)
            if (
                actionable
                and claimed_by
                and claimed_by != participant
                and claim_until > now
            ):
                raise ConflictError("message is currently claimed by another participant")
            conn.execute(
                """
                UPDATE message_deliveries
                SET state = 'acked',
                    first_delivered_at = COALESCE(first_delivered_at, ?),
                    last_delivered_at = COALESCE(last_delivered_at, ?),
                    acked_at = ?
                WHERE message_id = ? AND participant_id = ?
                  AND state != 'cancelled'
                """,
                (now, now, now, message, participant),
            )
            conn.execute(
                """
                INSERT INTO receipts
                    (message_id, participant_id, state, delivered_at, acked_at)
                VALUES (?, ?, 'acked', ?, ?)
                ON CONFLICT(message_id, participant_id) DO UPDATE SET
                    state = 'acked',
                    delivered_at = COALESCE(receipts.delivered_at, excluded.delivered_at),
                    acked_at = excluded.acked_at
                """,
                (message, participant, now, now),
            )
            globally_resolved = actionable and str(row["audience_kind"]) in {
                "participant",
                "role",
            }
            if globally_resolved:
                conn.execute(
                    "UPDATE messages SET status = 'acked', updated_at = ? "
                    "WHERE message_id = ?",
                    (now, message),
                )
                conn.execute(
                    "UPDATE message_deliveries SET actionable = 0 "
                    "WHERE message_id = ? AND actionable = 1",
                    (message,),
                )
        return {
            "message_id": message,
            "action": "ack",
            "acked_by": participant,
            "acked_at": now,
        }

    def _require_eligible_participant(self, participant_id: str, message_id: str) -> None:
        with self._transaction() as conn:
            self._archive_stale_rooms_locked(conn, now=time.time())
            row = conn.execute(
                "SELECT * FROM messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"unknown message: {message_id}")
            self._require_eligible_row(conn, participant_id, row)

    def _require_eligible_row(
        self,
        conn: sqlite3.Connection,
        participant_id: str,
        row: sqlite3.Row,
    ) -> sqlite3.Row:
        self._require_membership(
            conn,
            participant_id,
            str(row["conversation_id"]),
        )
        delivery = conn.execute(
            "SELECT state, actionable, priority FROM message_deliveries "
            "WHERE message_id = ? AND participant_id = ?",
            (str(row["message_id"]), participant_id),
        ).fetchone()
        if delivery is None or str(delivery["state"]) == "cancelled":
            raise ConflictError("participant is not an eligible recipient")
        return delivery

    def _delivery_is_actionable(
        self,
        participant_id: str,
        message_id: str,
    ) -> bool:
        with self._connection() as conn:
            delivery = conn.execute(
                "SELECT actionable FROM message_deliveries "
                "WHERE message_id = ? AND participant_id = ? "
                "AND state != 'cancelled'",
                (message_id, participant_id),
            ).fetchone()
        return bool(delivery is not None and delivery["actionable"])

    @staticmethod
    def _eligible(
        row: sqlite3.Row,
        *,
        participant_id: str,
        roles: set[str],
    ) -> bool:
        audience_kind = str(row["audience_kind"])
        audience_value = str(row["audience_value"])
        if audience_kind == "participant":
            return audience_value == participant_id
        if audience_kind in {"room", "broadcast"}:
            return audience_value in {"*", str(row["conversation_id"])}
        if audience_kind == "role":
            return audience_value in roles
        return False

    @staticmethod
    def _require_membership(
        conn: sqlite3.Connection,
        participant_id: str,
        conversation_id: str,
    ) -> sqlite3.Row:
        BridgeStore._require_active_room(conn, conversation_id)
        participant = conn.execute(
            "SELECT participant_id FROM participants WHERE participant_id = ?",
            (participant_id,),
        ).fetchone()
        if participant is None:
            raise NotFoundError(f"unknown participant: {participant_id}")
        membership = conn.execute(
            "SELECT * FROM memberships WHERE conversation_id = ? "
            "AND participant_id = ? AND active = 1",
            (conversation_id, participant_id),
        ).fetchone()
        if membership is None:
            raise ConflictError(
                f"participant {participant_id} is not in conversation {conversation_id}"
            )
        return membership

    @staticmethod
    def _require_active_room(
        conn: sqlite3.Connection,
        conversation_id: str,
    ) -> sqlite3.Row:
        room = conn.execute(
            "SELECT * FROM rooms WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if room is None:
            raise NotFoundError(f"unknown conversation: {conversation_id}")
        if str(room["status"]) != "active":
            raise ConflictError(
                f"conversation {conversation_id} is abandoned and cannot be entered"
            )
        return room

    @staticmethod
    def _require_live_session(
        conn: sqlite3.Connection,
        *,
        session_id: str,
        participant_id: str,
        now: float,
    ) -> sqlite3.Row:
        row = conn.execute(
            """
            SELECT * FROM agent_sessions
            WHERE session_id = ? AND participant_id = ?
              AND transport = 'mcp' AND revoked_at IS NULL
              AND cleared_at IS NULL
              AND expires_at > ?
            """,
            (session_id, participant_id, float(now)),
        ).fetchone()
        if row is None:
            raise AuthenticationError(
                "a live authenticated MCP session is required to chat"
            )
        return row

    @staticmethod
    def _secret_hash(secret: str) -> str:
        return hashlib.sha256(str(secret).encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_audience_value(
        audience_kind: str,
        value: str,
        conversation_id: str,
    ) -> str:
        if audience_kind == "participant":
            return opaque_id(value, field="audience_value")
        if audience_kind == "role":
            return token(value, field="audience_value")
        if audience_kind == "room":
            normalized = str(value or "*").strip()
            if normalized == "*":
                return conversation_id
            target = validate_conversation_id(normalized)
            if target != conversation_id:
                raise ValidationError("room audience must match conversation_id")
            return target
        return "*"

    @staticmethod
    def _participant_profile_payload(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            raise NotFoundError("participant row disappeared")
        return {
            "participant_id": str(row["participant_id"]),
            "client_type": str(row["client_type"]),
            "display_name": str(row["display_name"]),
            "signature": str(row["signature"]),
            "session_alias": str(row["session_alias"]),
            "status": str(row["status"]),
            "last_seen": float(row["last_seen"]),
        }

    @staticmethod
    def _follow_payload(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            raise NotFoundError("follow row disappeared")
        return {
            "conversation_id": str(row["conversation_id"]),
            "follower_participant_id": str(row["follower_participant_id"]),
            "followed_participant_id": str(row["followed_participant_id"]),
            "followed_client_type": str(row["client_type"]),
            "followed_display_name": str(row["display_name"]),
            "followed_signature": str(row["signature"]),
            "following": bool(row["active"]),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    @staticmethod
    def _nickname_request_payload(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            raise NotFoundError("nickname request row disappeared")
        requested_at = float(row["requested_at"])
        return {
            "request_id": str(row["request_id"]),
            "participant_id": str(row["participant_id"]),
            "client_type": str(row["client_type"]),
            "current_display_name": str(row["display_name"]),
            "signature": str(row["signature"]),
            "requested_display_name": str(row["requested_display_name"]),
            "status": str(row["status"]),
            "requested_at": requested_at,
            "reviewed_at": (
                float(row["reviewed_at"])
                if row["reviewed_at"] is not None
                else None
            ),
            "review_note": (
                str(row["review_note"])
                if row["review_note"] is not None
                else None
            ),
            "next_request_at": requested_at + NICKNAME_REQUEST_COOLDOWN_SECONDS,
        }

    @staticmethod
    def _message_payload(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            raise NotFoundError("message row disappeared")
        payload = {
            "sequence": int(row["sequence"]),
            "message_id": str(row["message_id"]),
            "conversation_id": str(row["conversation_id"]),
            "sender_participant_id": str(row["sender_participant_id"]),
            "audience_kind": str(row["audience_kind"]),
            "audience_value": str(row["audience_value"]),
            "body": str(row["body"]),
            "refs": json.loads(str(row["refs_json"])),
            "mentions": json.loads(str(row["mentions_json"] or "[]")),
            "reply_to": str(row["reply_to"]) if row["reply_to"] else None,
            "status": str(row["status"]),
            "claimed_by": str(row["claimed_by"]) if row["claimed_by"] else None,
            "claim_until": float(row["claim_until"]) if row["claim_until"] else None,
            "created_at": float(row["created_at"]),
        }
        keys = set(row.keys())
        if "delivery_state" in keys:
            payload["delivery"] = {
                "state": str(row["delivery_state"]),
                "reasons": json.loads(str(row["delivery_reasons_json"] or "[]")),
                "priority": (
                    "mention"
                    if str(row["delivery_priority"]) == "direct"
                    else str(row["delivery_priority"])
                ),
                "actionable": bool(row["delivery_actionable"]),
                "first_delivered_at": (
                    float(row["delivery_first_delivered_at"])
                    if row["delivery_first_delivered_at"] is not None
                    else None
                ),
                "last_delivered_at": (
                    float(row["delivery_last_delivered_at"])
                    if row["delivery_last_delivered_at"] is not None
                    else None
                ),
                "acked_at": (
                    float(row["delivery_acked_at"])
                    if row["delivery_acked_at"] is not None
                    else None
                ),
                "attempt_count": int(row["delivery_attempt_count"]),
            }
        return payload
