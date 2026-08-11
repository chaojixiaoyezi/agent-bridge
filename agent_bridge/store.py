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
    message_refs,
    opaque_id,
    product_username,
    string_tokens,
    token,
)


AUDIENCE_KINDS = {"participant", "room", "role", "broadcast"}
PRESENCE_STATES = {"online", "offline"}
MESSAGE_ACTIONS = {"claim", "ack", "release"}
MESSAGE_COOLDOWN_SECONDS = 15.0
AGENT_ACTIVE_ROOM_LIMIT = 2
ROOM_ABANDON_AFTER_SECONDS = 90 * 24 * 60 * 60
DEFAULT_SESSION_TTL_SECONDS = 2 * 60 * 60
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
    last_seen REAL NOT NULL,
    revoked_at REAL,
    revoked_reason TEXT,
    FOREIGN KEY (participant_id) REFERENCES participants(participant_id),
    FOREIGN KEY (registered_conversation_id) REFERENCES rooms(conversation_id)
);
"""


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS participants (
    participant_id TEXT PRIMARY KEY,
    client_type TEXT NOT NULL,
    session_alias TEXT NOT NULL,
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
            self._migrate_invited_sessions(conn)
            conn.executescript(SCHEMA)
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(messages)").fetchall()
            }
            if "authorized_session_id" not in columns:
                conn.execute(
                    "ALTER TABLE messages ADD COLUMN authorized_session_id TEXT"
                )
            conn.executescript(AUTHORIZATION_SCHEMA)
        with self._transaction() as conn:
            self._backfill_legacy_rooms(conn)
            self._archive_stale_rooms_locked(conn, now=time.time())
            conn.execute("PRAGMA user_version = 5")
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
                (participant_id, client_type, session_alias,
                 capabilities_json, status, created_at, last_seen)
            VALUES (?, ?, ?, '[]', 'online', ?, ?)
            ON CONFLICT(participant_id) DO UPDATE SET
                status = 'online',
                last_seen = excluded.last_seen
            """,
            (
                OWNER_PARTICIPANT_ID,
                OWNER_CLIENT_TYPE,
                OWNER_SESSION_ALIAS,
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
                    SET capabilities_json = ?, status = 'online', last_seen = ?
                    WHERE participant_id = ?
                    """,
                    (
                        compact_json(normalized_capabilities),
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
                         capabilities_json, status, created_at, last_seen)
                    VALUES (?, ?, ?, ?, 'online', ?, ?)
                    """,
                    (
                        participant_id,
                        normalized_client,
                        normalized_alias,
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

        return {
            "participant_id": participant_id,
            "client_type": normalized_client,
            "session_alias": normalized_alias,
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
        session_alias: str,
        conversation_id: str,
        roles: Sequence[str] | None = None,
        capabilities: Sequence[str] | None = None,
        session_ttl_seconds: float = DEFAULT_SESSION_TTL_SECONDS,
    ) -> dict[str, Any]:
        normalized_product = token(product, field="product_name")
        normalized_identity = product_username(normalized_product, username)
        if normalized_identity == OWNER_CLIENT_TYPE:
            raise ConflictError("this identity is reserved for the local web user")
        normalized_alias = alias(session_alias)
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
                         capabilities_json, status, created_at, last_seen)
                    VALUES (?, ?, ?, ?, 'online', ?, ?)
                    """,
                    (
                        participant_id,
                        normalized_identity,
                        normalized_alias,
                        compact_json(normalized_capabilities),
                        now,
                        now,
                    ),
                )
            else:
                participant_id = str(existing["participant_id"])
                if str(existing["session_alias"]) != normalized_alias:
                    raise ConflictError(
                        "session_alias is immutable and must match the original "
                        "registration"
                    )
                conn.execute(
                    """
                    UPDATE participants
                    SET capabilities_json = ?, status = 'online', last_seen = ?
                    WHERE participant_id = ?
                    """,
                    (
                        compact_json(normalized_capabilities),
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
                UPDATE agent_sessions
                SET revoked_at = ?, revoked_reason = 'superseded'
                WHERE participant_id = ? AND revoked_at IS NULL
                """,
                (now, participant_id),
            )
            conn.execute(
                """
                INSERT INTO agent_sessions
                    (session_id, participant_id, registered_conversation_id,
                     token_hash, transport, created_at, expires_at, last_seen)
                VALUES (?, ?, ?, ?, 'mcp', ?, ?, ?)
                """,
                (
                    session_id,
                    participant_id,
                    conversation,
                    self._secret_hash(access_token),
                    now,
                    now + session_ttl,
                    now,
                ),
            )
            owned_count = self._agent_active_room_count(conn, participant_id)

        return {
            "participant_id": participant_id,
            "client_type": normalized_identity,
            "session_alias": normalized_alias,
            "conversation_id": conversation,
            "roles": normalized_roles,
            "capabilities": normalized_capabilities,
            "status": "online",
            "session_id": session_id,
            "access_token": access_token,
            "session_expires_at": now + session_ttl,
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
                       participant.session_alias
                FROM agent_sessions AS session
                JOIN participants AS participant
                  ON participant.participant_id = session.participant_id
                WHERE session.token_hash = ? AND session.transport = 'mcp'
                """,
                (self._secret_hash(normalized_token),),
            ).fetchone()
            if row is None:
                raise AuthenticationError("invalid agent session token")
            if row["revoked_at"] is not None:
                raise AuthenticationError("agent session has been revoked")
            if float(row["expires_at"]) <= now:
                raise AuthenticationError("agent session has expired")
            conn.execute(
                "UPDATE agent_sessions SET last_seen = ? WHERE session_id = ?",
                (now, str(row["session_id"])),
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
            "expires_at": float(row["expires_at"]),
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
        normalized_reply = (
            opaque_id(reply_to, field="reply_to") if reply_to else None
        )
        normalized_target = self._normalize_audience_value(
            normalized_audience,
            audience_value,
            conversation,
        )
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
                         refs_json, reply_to, status, authorized_session_id,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 'message', ?, ?, ?, 'open', ?, ?, ?)
                    """,
                    (
                        message_id,
                        conversation,
                        sender,
                        normalized_audience,
                        normalized_target,
                        normalized_body,
                        compact_json(normalized_refs),
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
        return self._message_payload(row)

    def send_owner_message(
        self,
        *,
        conversation_id: str,
        body_text: str,
    ) -> dict[str, Any]:
        """Send one owner-authored room message through the local web authority."""
        return self.send(
            authorized_session_id=OWNER_AUTHORIZATION_ID,
            sender_participant_id=OWNER_PARTICIPANT_ID,
            conversation_id=conversation_id,
            body_text=body_text,
            audience_kind="room",
            audience_value="*",
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
                return {
                    "participant_id": participant,
                    "messages": messages,
                    "count": len(messages),
                    "timed_out": False,
                    "last_sequence": max(item["sequence"] for item in messages),
                }
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.heartbeat(
                    participant,
                    authorized_session_id=authorized_session_id,
                )
                return {
                    "participant_id": participant,
                    "messages": [],
                    "count": 0,
                    "timed_out": True,
                    "last_sequence": None,
                }
            time.sleep(min(self.poll_interval_seconds, remaining))

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
        if str(original["audience_kind"]) in {"participant", "role"}:
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
        authorized_session_id: str | None = None,
    ) -> dict[str, Any]:
        participant = opaque_id(participant_id, field="participant_id")
        conversation = validate_conversation_id(conversation_id)
        normalized_limit = max(1, min(int(limit), 200))
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
            if before_sequence is None:
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
        messages = [self._message_payload(row) for row in reversed(rows)]
        return {
            "conversation_id": conversation,
            "messages": messages,
            "count": len(messages),
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
                ORDER BY p.session_alias, p.participant_id
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
            memberships = conn.execute(
                "SELECT conversation_id, roles_json, joined_at FROM memberships "
                "WHERE participant_id = ? AND active = 1",
                (participant_id,),
            ).fetchall()
            roles_by_conversation = {
                str(row["conversation_id"]): set(json.loads(str(row["roles_json"])))
                for row in memberships
            }
            joined_at_by_conversation = {
                str(row["conversation_id"]): float(row["joined_at"])
                for row in memberships
            }
            if not roles_by_conversation:
                return []
            placeholders = ",".join("?" for _ in roles_by_conversation)
            rows = conn.execute(
                f"""
                SELECT m.*, r.state AS receipt_state
                FROM messages AS m
                LEFT JOIN receipts AS r
                  ON r.message_id = m.message_id AND r.participant_id = ?
                WHERE m.conversation_id IN ({placeholders})
                  AND m.sender_participant_id != ?
                  AND m.status = 'open'
                  AND (r.state IS NULL OR r.state != 'acked')
                ORDER BY m.sequence
                LIMIT 500
                """,
                (participant_id, *roles_by_conversation.keys(), participant_id),
            ).fetchall()

        selected: list[sqlite3.Row] = []
        for row in rows:
            conversation = str(row["conversation_id"])
            if float(row["created_at"]) < joined_at_by_conversation[conversation]:
                continue
            if not self._eligible(
                row,
                participant_id=participant_id,
                roles=roles_by_conversation.get(conversation, set()),
            ):
                continue
            claim_until = float(row["claim_until"] or 0.0)
            claimed_by = str(row["claimed_by"] or "")
            if claimed_by and claimed_by != participant_id and claim_until > now:
                continue
            if (
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
                        "SELECT * FROM messages WHERE message_id = ?",
                        (str(row["message_id"]),),
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
                delivered_rows.append(row)
        return [self._message_payload(row) for row in delivered_rows]

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
            self._require_eligible_row(conn, participant, row)
            if str(row["audience_kind"]) not in {"participant", "role"}:
                raise ConflictError(
                    "room and broadcast messages use per-participant receipts"
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
            self._require_eligible_row(conn, participant, row)
            claimed_by = str(row["claimed_by"] or "")
            claim_until = float(row["claim_until"] or 0.0)
            if claimed_by and claimed_by != participant and claim_until > now:
                raise ConflictError("message is currently claimed by another participant")
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
            globally_resolved = str(row["audience_kind"]) in {
                "participant",
                "role",
            }
            if globally_resolved:
                conn.execute(
                    "UPDATE messages SET status = 'acked', updated_at = ? "
                    "WHERE message_id = ?",
                    (now, message),
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
    ) -> None:
        membership = self._require_membership(
            conn,
            participant_id,
            str(row["conversation_id"]),
        )
        roles = set(json.loads(str(membership["roles_json"])))
        if not self._eligible(row, participant_id=participant_id, roles=roles):
            raise ConflictError("participant is not an eligible recipient")

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
    def _message_payload(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            raise NotFoundError("message row disappeared")
        return {
            "sequence": int(row["sequence"]),
            "message_id": str(row["message_id"]),
            "conversation_id": str(row["conversation_id"]),
            "sender_participant_id": str(row["sender_participant_id"]),
            "audience_kind": str(row["audience_kind"]),
            "audience_value": str(row["audience_value"]),
            "body": str(row["body"]),
            "refs": json.loads(str(row["refs_json"])),
            "reply_to": str(row["reply_to"]) if row["reply_to"] else None,
            "status": str(row["status"]),
            "claimed_by": str(row["claimed_by"]) if row["claimed_by"] else None,
            "claim_until": float(row["claim_until"]) if row["claim_until"] else None,
            "created_at": float(row["created_at"]),
        }
