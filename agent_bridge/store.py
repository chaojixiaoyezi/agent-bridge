from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import sqlite3
import time
from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .a2a_store import A2A_GATEWAY_SCHEMA, A2AStoreMixin
from .agent_connectors import INVITATION_SCHEMA, AgentConnectorMixin
from .agent_lifecycle import AGENT_LIFECYCLE_SCHEMA, AgentLifecycleMixin
from .agent_sessions import AgentSessionMixin
from .avatars import (
    AGENT_AVATAR_CHANGE_COOLDOWN_SECONDS,
    normalize_avatar_key as normalize_avatar_key,
)
from .admin_audit import ADMIN_AUDIT_SCHEMA, AdminAuditMixin
from .chat_authorization import CHAT_AUTHORIZATION_SCHEMA, ChatAuthorizationMixin
from .connector_health import ConnectorHealthMixin
from .store_constants import (
    AGENT_ACTIVE_ROOM_LIMIT as AGENT_ACTIVE_ROOM_LIMIT,
    AUDIENCE_KINDS as AUDIENCE_KINDS,
    CHAT_AUTHORIZATION_FROZEN,
    CONNECTOR_COMPONENTS as CONNECTOR_COMPONENTS,
    CONNECTOR_ONLINE_WINDOW_SECONDS as CONNECTOR_ONLINE_WINDOW_SECONDS,
    CONNECTOR_SESSION_IDLE_RETIRE_SECONDS as CONNECTOR_SESSION_IDLE_RETIRE_SECONDS,
    CONNECTOR_SESSION_MIN_RETAIN as CONNECTOR_SESSION_MIN_RETAIN,
    CONNECTOR_SETUP_STATUSES as CONNECTOR_SETUP_STATUSES,
    DEFAULT_AGENT_INACTIVITY_DAYS as DEFAULT_AGENT_INACTIVITY_DAYS,
    DEFAULT_INVITATION_TTL_SECONDS as DEFAULT_INVITATION_TTL_SECONDS,
    DEFAULT_OFFLINE_BACKLOG_KEEP_MESSAGES,
    DEFAULT_ROOM_DIGEST_AFTER_SECONDS as DEFAULT_ROOM_DIGEST_AFTER_SECONDS,
    DEFAULT_ROOM_DIGEST_MIN_MESSAGES as DEFAULT_ROOM_DIGEST_MIN_MESSAGES,
    DEFAULT_ROOM_WAKE_MODE as DEFAULT_ROOM_WAKE_MODE,
    DEFAULT_SESSION_TTL_SECONDS,
    DEFAULT_UNACTIVATED_AGENT_INACTIVITY_DAYS,
    DELIVERY_STATES as DELIVERY_STATES,
    ENROLLMENT_PREVIOUS_GRACE_SECONDS as ENROLLMENT_PREVIOUS_GRACE_SECONDS,
    INVITATION_ADAPTERS as INVITATION_ADAPTERS,
    INVITATION_MODES as INVITATION_MODES,
    INVITATION_STATUSES as INVITATION_STATUSES,
    MAX_AGENT_INACTIVITY_DAYS,
    MAX_HISTORY_SEARCH_QUERY_LENGTH,
    MAX_HISTORY_SEARCH_TERMS,
    MAX_INVITATION_TTL_SECONDS as MAX_INVITATION_TTL_SECONDS,
    MAX_MENTIONS_PER_MESSAGE as MAX_MENTIONS_PER_MESSAGE,
    MAX_MESSAGE_COOLDOWN_SECONDS as MAX_MESSAGE_COOLDOWN_SECONDS,
    MAX_OFFLINE_BACKLOG_KEEP_MESSAGES,
    MAX_TASK_TARGETS as MAX_TASK_TARGETS,
    MAX_WAIT_MESSAGES_PAGE_SIZE,
    MESSAGE_ACTIONS,
    MESSAGE_COOLDOWN_SECONDS,
    MESSAGE_NOTIFICATION_MODES as MESSAGE_NOTIFICATION_MODES,
    MESSAGE_SENDER_SEATS as MESSAGE_SENDER_SEATS,
    MIN_AGENT_INACTIVITY_DAYS,
    NATIVE_CHANNEL_MAX_MESSAGES as NATIVE_CHANNEL_MAX_MESSAGES,
    NATIVE_CHANNEL_MAX_WAIT_SECONDS as NATIVE_CHANNEL_MAX_WAIT_SECONDS,
    NATIVE_SESSION_LEASE_SECONDS as NATIVE_SESSION_LEASE_SECONDS,
    NATIVE_TUI_ADAPTERS as NATIVE_TUI_ADAPTERS,
    NICKNAME_REQUEST_COOLDOWN_SECONDS,
    OWNER_AUTHORIZATION_ID as OWNER_AUTHORIZATION_ID,
    OWNER_CLIENT_TYPE as OWNER_CLIENT_TYPE,
    OWNER_PARTICIPANT_ID,
    OWNER_SESSION_ALIAS as OWNER_SESSION_ALIAS,
    PRESENCE_STATES as PRESENCE_STATES,
    RATE_LIMIT_ACTOR_KINDS as RATE_LIMIT_ACTOR_KINDS,
    ROOM_ABANDON_AFTER_SECONDS as ROOM_ABANDON_AFTER_SECONDS,
    ROOM_MESSAGE_MARKER_KINDS as ROOM_MESSAGE_MARKER_KINDS,
    ROOM_WAKE_MODES as ROOM_WAKE_MODES,
    RUNTIME_HEARTBEAT_INTERVAL_SECONDS as RUNTIME_HEARTBEAT_INTERVAL_SECONDS,
    RUNTIME_INSTANCE_ACTIVE_SECONDS as RUNTIME_INSTANCE_ACTIVE_SECONDS,
    RUNTIME_INSTANCE_RETENTION_SECONDS as RUNTIME_INSTANCE_RETENTION_SECONDS,
    RUNTIME_LEASE_TTL_SECONDS as RUNTIME_LEASE_TTL_SECONDS,
    SESSION_COMPONENTS as SESSION_COMPONENTS,
    TASK_CLAIM_LEASE_SECONDS as TASK_CLAIM_LEASE_SECONDS,
    TASK_INPUT_REDELIVERY_SECONDS as TASK_INPUT_REDELIVERY_SECONDS,
    TASK_STATUSES as TASK_STATUSES,
    TUI_STATES as TUI_STATES,
    WEB_USER_MESSAGE_COOLDOWN_SECONDS as WEB_USER_MESSAGE_COOLDOWN_SECONDS,
    _ACKNOWLEDGEMENT_ONLY_PATTERN as _ACKNOWLEDGEMENT_ONLY_PATTERN,
    _DIRECT_AGENT_REPLY_REQUEST_PATTERNS as _DIRECT_AGENT_REPLY_REQUEST_PATTERNS,
    _DIRECT_REVIEW_REQUEST_PATTERNS as _DIRECT_REVIEW_REQUEST_PATTERNS,
)
from .store_errors import (
    AuthenticationError,
    AuthorizationError,
    AvatarRateLimitError as AvatarRateLimitError,
    BridgeError,
    ConflictError,
    NicknameRateLimitError as NicknameRateLimitError,
    NotFoundError,
    RateLimitError as RateLimitError,
)
from .history_governance import (
    HISTORY_GOVERNANCE_SCHEMA,
    HISTORY_REDACTED_MESSAGE_BODY as HISTORY_REDACTED_MESSAGE_BODY,
    HISTORY_REDACTED_TASK_BODY as HISTORY_REDACTED_TASK_BODY,
    HistoryGovernanceMixin,
)
from .message_composer import (
    ROOM_WAKE_POLICY_SCHEMA,
    MessageComposerMixin,
)
from .message_routing import (
    AUTHORIZATION_SCHEMA,
    DELIVERY_SCHEMA,
    MessageRoutingMixin,
)
from .message_rates import RATE_LIMIT_SCHEMA, MessageRateMixin
from .native_sessions import NATIVE_SESSION_SCHEMA, NativeSessionMixin
from .operational_monitoring import (
    OPERATIONAL_MONITORING_COLUMN_ADDITIONS,
    OPERATIONAL_MONITORING_SCHEMA,
    REQUIRED_REPLY_DELAY_WARNING_SECONDS as REQUIRED_REPLY_DELAY_WARNING_SECONDS,
)
from .participant_profiles import PROFILE_SCHEMA, ParticipantProfileMixin
from .room_governance import (
    ROOM_GOVERNANCE_SCHEMA,
    ROOM_KNOWLEDGE_SCHEMA,
    RoomGovernanceMixin,
)
from .room_tasks import ROOM_TASK_SCHEMA, RoomTaskMixin
from .runtime_coordination import RUNTIME_COORDINATION_SCHEMA, RuntimeCoordinationMixin
from .validation import (
    MAX_AGENT_USERNAME_CHARS as MAX_AGENT_USERNAME_CHARS,
    MAX_CLIENT_IDENTITY_CHARS as MAX_CLIENT_IDENTITY_CHARS,
    ValidationError,
    agent_username as agent_username,
    alias as alias,
    body as body,
    client_identity as client_identity,
    compact_json,
    conversation_id as validate_conversation_id,
    message_refs as message_refs,
    opaque_id,
    product_username as product_username,
    string_tokens as string_tokens,
    token as token,
)
from .web_auth import (
    DEFAULT_WEB_USER_ROOM_LIMIT,
    MAX_WEB_USER_ROOM_LIMIT,
    WEB_AUTH_SCHEMA,
)


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
    connector_id TEXT,
    component TEXT NOT NULL DEFAULT 'unknown'
        CHECK (component IN ('listener', 'chat', 'task', 'mcp', 'a2a', 'unknown')),
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
    avatar_key TEXT NOT NULL DEFAULT 'auto',
    avatar_changed_at REAL,
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
    room_sequence INTEGER,
    message_id TEXT NOT NULL UNIQUE,
    conversation_id TEXT NOT NULL,
    sender_participant_id TEXT NOT NULL,
    audience_kind TEXT NOT NULL,
    audience_value TEXT NOT NULL,
    message_kind TEXT NOT NULL,
    body TEXT NOT NULL,
    refs_json TEXT NOT NULL DEFAULT '[]',
    mentions_json TEXT NOT NULL DEFAULT '[]',
    wake_all_agents INTEGER NOT NULL DEFAULT 0
        CHECK (wake_all_agents IN (0, 1)),
    reply_to TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    claimed_by TEXT,
    claim_until REAL,
    authorized_session_id TEXT,
    forwarded_from_message_id TEXT,
    sender_seat TEXT NOT NULL DEFAULT 'unknown'
        CHECK (sender_seat IN ('main', 'shadow', 'executor', 'web', 'a2a', 'unknown')),
    notification_mode TEXT NOT NULL DEFAULT 'ordinary'
        CHECK (notification_mode IN ('ordinary', 'mention')),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES rooms(conversation_id),
    FOREIGN KEY (sender_participant_id) REFERENCES participants(participant_id),
    FOREIGN KEY (reply_to) REFERENCES messages(message_id),
    FOREIGN KEY (forwarded_from_message_id) REFERENCES messages(message_id),
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


ROOM_MESSAGE_SEQUENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS room_message_sequences (
    conversation_id TEXT PRIMARY KEY,
    last_sequence INTEGER NOT NULL DEFAULT 0 CHECK (last_sequence >= 0),
    FOREIGN KEY (conversation_id) REFERENCES rooms(conversation_id)
);

DROP TRIGGER IF EXISTS trg_messages_assign_room_sequence;
CREATE TRIGGER trg_messages_assign_room_sequence
AFTER INSERT ON messages
WHEN NEW.room_sequence IS NULL
BEGIN
    INSERT INTO room_message_sequences (conversation_id, last_sequence)
    VALUES (NEW.conversation_id, 1)
    ON CONFLICT(conversation_id) DO UPDATE
    SET last_sequence = room_message_sequences.last_sequence + 1;

    UPDATE messages
    SET room_sequence = (
        SELECT last_sequence
        FROM room_message_sequences
        WHERE conversation_id = NEW.conversation_id
    )
    WHERE sequence = NEW.sequence;
END;

DROP TRIGGER IF EXISTS trg_messages_sync_explicit_room_sequence;
CREATE TRIGGER trg_messages_sync_explicit_room_sequence
AFTER INSERT ON messages
WHEN NEW.room_sequence IS NOT NULL
BEGIN
    INSERT INTO room_message_sequences (conversation_id, last_sequence)
    VALUES (NEW.conversation_id, NEW.room_sequence)
    ON CONFLICT(conversation_id) DO UPDATE
    SET last_sequence = MAX(
        room_message_sequences.last_sequence,
        excluded.last_sequence
    );
END;

DROP TRIGGER IF EXISTS trg_messages_room_sequence_immutable;
CREATE TRIGGER trg_messages_room_sequence_immutable
BEFORE UPDATE OF room_sequence ON messages
WHEN OLD.room_sequence IS NOT NULL
 AND NEW.room_sequence IS NOT OLD.room_sequence
BEGIN
    SELECT RAISE(ABORT, 'ROOM_MESSAGE_SEQUENCE_IMMUTABLE');
END;
"""


class BridgeStore(
    AdminAuditMixin,
    HistoryGovernanceMixin,
    MessageRateMixin,
    MessageRoutingMixin,
    MessageComposerMixin,
    RuntimeCoordinationMixin,
    A2AStoreMixin,
    ParticipantProfileMixin,
    ChatAuthorizationMixin,
    AgentLifecycleMixin,
    AgentConnectorMixin,
    ConnectorHealthMixin,
    AgentSessionMixin,
    NativeSessionMixin,
    RoomGovernanceMixin,
    RoomTaskMixin,
):
    _history_conflict_error = ConflictError
    _history_not_found_error = NotFoundError
    _history_authorization_error = AuthorizationError

    def __init__(
        self,
        database: str | Path,
        *,
        poll_interval_seconds: float = 0.2,
        business_timezone: str | None = None,
    ) -> None:
        self.database = Path(database).expanduser()
        self.poll_interval_seconds = max(0.05, min(float(poll_interval_seconds), 2.0))
        (
            self.business_timezone,
            self.business_timezone_name,
        ) = self._resolve_business_timezone(business_timezone)
        self._initialize()

    @staticmethod
    def _resolve_business_timezone(value: str | None):
        configured = str(
            value
            if value is not None
            else os.environ.get("AGENT_BRIDGE_TIMEZONE", "")
        ).strip()
        if not configured:
            try:
                resolved = str(Path("/etc/localtime").resolve())
            except OSError:
                resolved = ""
            marker = "/zoneinfo/"
            if marker in resolved:
                configured = resolved.split(marker, 1)[1]
        if configured:
            try:
                return ZoneInfo(configured), configured
            except ZoneInfoNotFoundError as exc:
                raise ValueError(
                    f"unknown AGENT_BRIDGE_TIMEZONE: {configured}"
                ) from exc
        local = datetime.now().astimezone().tzinfo or timezone.utc
        return local, getattr(local, "key", None) or str(local)

    def _next_business_midnight(self, now: float) -> float:
        current = datetime.fromtimestamp(now, tz=self.business_timezone)
        next_date = current.date() + timedelta(days=1)
        return datetime.combine(
            next_date,
            datetime_time.min,
            tzinfo=self.business_timezone,
        ).timestamp()

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
            if "avatar_key" not in participant_columns:
                conn.execute(
                    "ALTER TABLE participants ADD COLUMN avatar_key TEXT "
                    "NOT NULL DEFAULT 'auto'"
                )
            if "avatar_changed_at" not in participant_columns:
                conn.execute(
                    "ALTER TABLE participants ADD COLUMN avatar_changed_at REAL"
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
            if "connector_id" not in session_columns:
                conn.execute("ALTER TABLE agent_sessions ADD COLUMN connector_id TEXT")
            if "component" not in session_columns:
                # Existing credentials predate authoritative seat tracking. Do
                # not infer their origin; only new registrations are concrete.
                conn.execute(
                    "ALTER TABLE agent_sessions ADD COLUMN component TEXT "
                    "NOT NULL DEFAULT 'unknown'"
                )
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
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_sessions_connector_activity "
                "ON agent_sessions(connector_id, cleared_at, revoked_at, last_seen DESC)"
            )
            message_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(messages)").fetchall()
            }
            if "room_sequence" not in message_columns:
                conn.execute("ALTER TABLE messages ADD COLUMN room_sequence INTEGER")
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
            if "wake_all_agents" not in message_columns:
                conn.execute(
                    "ALTER TABLE messages ADD COLUMN wake_all_agents INTEGER "
                    "NOT NULL DEFAULT 0 CHECK (wake_all_agents IN (0, 1))"
                )
            if "forwarded_from_message_id" not in message_columns:
                conn.execute(
                    "ALTER TABLE messages ADD COLUMN forwarded_from_message_id TEXT "
                    "REFERENCES messages(message_id)"
                )
            if "sender_seat" not in message_columns:
                # Historical messages stay explicit unknown. Guessing from
                # prose would recreate the ambiguity this field removes.
                conn.execute(
                    "ALTER TABLE messages ADD COLUMN sender_seat TEXT "
                    "NOT NULL DEFAULT 'unknown'"
                )
            notification_mode_added = "notification_mode" not in message_columns
            if notification_mode_added:
                conn.execute(
                    "ALTER TABLE messages ADD COLUMN notification_mode TEXT "
                    "NOT NULL DEFAULT 'ordinary' "
                    "CHECK (notification_mode IN ('ordinary', 'mention'))"
                )
                conn.execute(
                    """
                    UPDATE messages
                    SET notification_mode = CASE
                        WHEN mentions_json != '[]'
                          OR wake_all_agents = 1
                          OR reply_to IS NOT NULL
                          OR audience_kind IN ('participant', 'role')
                        THEN 'mention'
                        ELSE 'ordinary'
                    END
                    """
                )
            if schema_version < 8 or mentions_column_added:
                self._backfill_implicit_participant_mentions(conn)
            # Ensure even very old databases have room authority before the
            # per-room counter is seeded. The later call remains an idempotent
            # safety net for the rest of the legacy migration path.
            self._backfill_legacy_rooms(conn)
            self._initialize_room_message_sequences_locked(conn)
            conn.executescript(WEB_AUTH_SCHEMA)
            self._migrate_web_user_room_permissions(conn)
            conn.executescript(ROOM_GOVERNANCE_SCHEMA)
            conn.executescript(ROOM_KNOWLEDGE_SCHEMA)
            conn.executescript(ROOM_WAKE_POLICY_SCHEMA)
            conn.executescript(ROOM_TASK_SCHEMA)
            if schema_version < 30:
                self._backfill_room_web_members(conn)
            task_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(room_tasks)").fetchall()
            }
            if "lease_expires_at" not in task_columns:
                conn.execute("ALTER TABLE room_tasks ADD COLUMN lease_expires_at REAL")
            for name in (
                "source_sequence",
                "context_start_sequence",
                "context_end_sequence",
            ):
                if name not in task_columns:
                    conn.execute(
                        f"ALTER TABLE room_tasks ADD COLUMN {name} INTEGER"
                    )
            conn.executescript(RATE_LIMIT_SCHEMA)
            conn.executescript(PROFILE_SCHEMA)
            self._migrate_reusable_agent_invitations(conn)
            self._migrate_agent_connector_conversations(conn)
            conn.executescript(INVITATION_SCHEMA)
            self._migrate_connector_identity_bindings(conn)
            self._migrate_native_tui_bindings(conn)
            if schema_version < 21:
                self._repair_connector_room_bindings(conn)
            lifecycle_table_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'agent_lifecycle_policy'"
            ).fetchone() is not None
            if lifecycle_table_exists:
                existing_lifecycle_columns = {
                    str(row["name"])
                    for row in conn.execute(
                        "PRAGMA table_info(agent_lifecycle_policy)"
                    ).fetchall()
                }
                if "unactivated_inactivity_days" not in existing_lifecycle_columns:
                    # The schema script seeds this column. Existing v16-v24
                    # databases must gain it before that INSERT is parsed.
                    conn.execute(
                        "ALTER TABLE agent_lifecycle_policy ADD COLUMN "
                        "unactivated_inactivity_days INTEGER NOT NULL "
                        f"DEFAULT {DEFAULT_UNACTIVATED_AGENT_INACTIVITY_DAYS} "
                        f"CHECK (unactivated_inactivity_days BETWEEN "
                        f"{MIN_AGENT_INACTIVITY_DAYS} AND "
                        f"{MAX_AGENT_INACTIVITY_DAYS})"
                    )
            conn.executescript(AGENT_LIFECYCLE_SCHEMA)
            lifecycle_policy_columns = {
                str(row["name"])
                for row in conn.execute(
                    "PRAGMA table_info(agent_lifecycle_policy)"
                ).fetchall()
            }
            if "unactivated_inactivity_days" not in lifecycle_policy_columns:
                conn.execute(
                    "ALTER TABLE agent_lifecycle_policy ADD COLUMN "
                    "unactivated_inactivity_days INTEGER NOT NULL "
                    f"DEFAULT {DEFAULT_UNACTIVATED_AGENT_INACTIVITY_DAYS} "
                    f"CHECK (unactivated_inactivity_days BETWEEN "
                    f"{MIN_AGENT_INACTIVITY_DAYS} AND "
                    f"{MAX_AGENT_INACTIVITY_DAYS})"
                )
            self._backfill_agent_lifecycle_states(conn)
            self._restore_legacy_migrated_memberships(conn)
            nickname_columns = {
                str(row["name"])
                for row in conn.execute(
                    "PRAGMA table_info(nickname_requests)"
                ).fetchall()
            }
            if "reviewed_by_web_user_id" not in nickname_columns:
                conn.execute(
                    "ALTER TABLE nickname_requests "
                    "ADD COLUMN reviewed_by_web_user_id TEXT"
                )
            delivery_table_existed = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'message_deliveries'"
            ).fetchone() is not None
            conn.executescript(DELIVERY_SCHEMA)
            conn.executescript(OPERATIONAL_MONITORING_SCHEMA)
            monitoring_columns = {
                str(row["name"])
                for row in conn.execute(
                    "PRAGMA table_info(operational_metric_samples)"
                ).fetchall()
            }
            for name, declaration in (
                OPERATIONAL_MONITORING_COLUMN_ADDITIONS.items()
            ):
                if name not in monitoring_columns:
                    conn.execute(
                        "ALTER TABLE operational_metric_samples "
                        f"ADD COLUMN {name} {declaration}"
                    )
            conn.executescript(ADMIN_AUDIT_SCHEMA)
            conn.executescript(HISTORY_GOVERNANCE_SCHEMA)
            conn.executescript(RUNTIME_COORDINATION_SCHEMA)
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
            delivery_stage_added = "delivery_stage" not in delivery_columns
            delivery_additions = {
                "delivery_stage": (
                    "TEXT NOT NULL DEFAULT 'queued' "
                    "CHECK (delivery_stage IN ("
                    "'queued', 'legacy_delivered', 'native_injected', "
                    "'native_applied', 'replied', 'legacy_acked', 'cancelled'"
                    "))"
                ),
                "native_session_id": "TEXT",
                "native_event_id": "TEXT",
                "native_injected_at": "REAL",
                "native_applied_at": "REAL",
                "native_replied_at": "REAL",
                "shadow_seen_at": "REAL",
            }
            for name, declaration in delivery_additions.items():
                if name not in delivery_columns:
                    conn.execute(
                        f"ALTER TABLE message_deliveries "
                        f"ADD COLUMN {name} {declaration}"
                    )
            if delivery_stage_added:
                conn.execute(
                    """
                    UPDATE message_deliveries
                    SET delivery_stage = CASE state
                        WHEN 'pending' THEN 'queued'
                        WHEN 'delivered' THEN 'legacy_delivered'
                        WHEN 'acked' THEN 'legacy_acked'
                        WHEN 'cancelled' THEN 'cancelled'
                        ELSE 'queued'
                    END
                    """
                )
            conn.executescript(NATIVE_SESSION_SCHEMA)
            conn.executescript(AUTHORIZATION_SCHEMA)
            conn.executescript(CHAT_AUTHORIZATION_SCHEMA)
            self._freeze_legacy_chat_authorizations(conn)
            conn.executescript(A2A_GATEWAY_SCHEMA)
            if schema_version < 19:
                self._migrate_agent_mentions_to_optional(conn)
            if schema_version < 20:
                self._migrate_internal_participant_mentions_to_display_names(conn)
            reconcile_deliveries = (
                schema_version < 8
                or not delivery_table_existed
                or actionable_column_added
            )
        with self._transaction() as conn:
            self._backfill_legacy_rooms(conn)
            if reconcile_deliveries:
                self._backfill_message_deliveries(
                    conn,
                    # Optional wake reasons were introduced in schema 17.
                    # Rebuilding an older ledger must not turn historical
                    # replies into fresh high-priority notifications.  A
                    # schema-17 recovery, however, must faithfully recreate
                    # the current delivery semantics.
                    include_optional_wakes=schema_version >= 17,
                )
            if schema_version < 11:
                # v8-v10 stored explicit structured mentions as ``important``.
                # Keep the existing CHECK-compatible ``direct`` storage value,
                # which is projected publicly as ``mention`` and does not imply
                # hidden visibility or actionability.
                conn.execute(
                    "UPDATE message_deliveries SET priority = 'direct' "
                    "WHERE priority = 'important' "
                    "AND (instr(reasons_json, '\"mention\"') > 0 "
                    "OR instr(reasons_json, '\"agent_mention\"') > 0)"
                )
            self._archive_stale_rooms_locked(conn, now=time.time())
            conn.execute("PRAGMA user_version = 41")
            conn.execute("PRAGMA optimize")
        try:
            os.chmod(self.database, 0o600)
        except OSError:
            pass

    @staticmethod
    def _migrate_web_user_room_permissions(conn: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(web_users)").fetchall()
        }
        if "can_create_rooms" not in columns:
            conn.execute(
                "ALTER TABLE web_users ADD COLUMN can_create_rooms INTEGER "
                "NOT NULL DEFAULT 0 CHECK (can_create_rooms IN (0, 1))"
            )
        if "room_limit" not in columns:
            conn.execute(
                "ALTER TABLE web_users ADD COLUMN room_limit INTEGER "
                f"NOT NULL DEFAULT {DEFAULT_WEB_USER_ROOM_LIMIT} "
                f"CHECK (room_limit BETWEEN 1 AND {MAX_WEB_USER_ROOM_LIMIT})"
            )
        if "avatar_key" not in columns:
            conn.execute(
                "ALTER TABLE web_users ADD COLUMN avatar_key TEXT "
                "NOT NULL DEFAULT 'auto'"
            )
        for column, declaration in (
            ("email", "TEXT COLLATE NOCASE"),
            ("email_verified_at", "REAL"),
            ("pending_email", "TEXT COLLATE NOCASE"),
            ("email_updated_at", "REAL"),
        ):
            if column not in columns:
                conn.execute(
                    f"ALTER TABLE web_users ADD COLUMN {column} {declaration}"
                )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_web_users_verified_email_unique "
            "ON web_users(email COLLATE NOCASE) WHERE email IS NOT NULL"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_web_users_pending_email_unique "
            "ON web_users(pending_email COLLATE NOCASE) "
            "WHERE pending_email IS NOT NULL"
        )

    @staticmethod
    def _initialize_room_message_sequences_locked(
        conn: sqlite3.Connection,
    ) -> None:
        """Backfill stable room-local labels without changing global cursors."""

        conn.execute(
            """
            WITH ranked AS (
                SELECT message_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY conversation_id ORDER BY sequence
                       ) AS assigned_sequence
                FROM messages
            )
            UPDATE messages
            SET room_sequence = (
                SELECT ranked.assigned_sequence
                FROM ranked
                WHERE ranked.message_id = messages.message_id
            )
            WHERE room_sequence IS NULL
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS room_message_sequences (
                conversation_id TEXT PRIMARY KEY,
                last_sequence INTEGER NOT NULL DEFAULT 0
                    CHECK (last_sequence >= 0),
                FOREIGN KEY (conversation_id) REFERENCES rooms(conversation_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO room_message_sequences (conversation_id, last_sequence)
            SELECT conversation_id, MAX(room_sequence)
            FROM messages
            GROUP BY conversation_id
            ON CONFLICT(conversation_id) DO UPDATE
            SET last_sequence = MAX(
                room_message_sequences.last_sequence,
                excluded.last_sequence
            )
            """
        )
        conn.executescript(ROOM_MESSAGE_SEQUENCE_SCHEMA)
        # An old process can insert in the narrow migration window before the
        # trigger exists. Repair such rows once more, then seed counters from
        # the authoritative room order before enforcing uniqueness.
        conn.execute(
            """
            WITH ranked AS (
                SELECT message_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY conversation_id ORDER BY sequence
                       ) AS assigned_sequence
                FROM messages
            )
            UPDATE messages
            SET room_sequence = (
                SELECT ranked.assigned_sequence
                FROM ranked
                WHERE ranked.message_id = messages.message_id
            )
            WHERE room_sequence IS NULL
            """
        )
        conn.execute(
            """
            INSERT INTO room_message_sequences (conversation_id, last_sequence)
            SELECT conversation_id, MAX(room_sequence)
            FROM messages
            GROUP BY conversation_id
            ON CONFLICT(conversation_id) DO UPDATE
            SET last_sequence = MAX(
                room_message_sequences.last_sequence,
                excluded.last_sequence
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "idx_messages_conversation_room_sequence "
            "ON messages(conversation_id, room_sequence)"
        )

    @staticmethod
    def _backfill_room_web_members(conn: sqlite3.Connection) -> None:
        """Preserve explicit pre-v30 Web participation without opening rooms."""

        conn.execute(
            """
            INSERT OR IGNORE INTO room_web_members
                (conversation_id, web_user_id, access_role, active,
                 invited_by_web_user_id, created_at, updated_at)
            SELECT ownership.conversation_id, ownership.web_user_id,
                   'member', 1, ownership.web_user_id,
                   ownership.created_at, ownership.created_at
            FROM room_web_owners AS ownership
            JOIN web_users AS web_user
              ON web_user.user_id = ownership.web_user_id
             AND web_user.active = 1
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO room_web_members
                (conversation_id, web_user_id, access_role, active,
                 invited_by_web_user_id, created_at, updated_at)
            SELECT membership.conversation_id, web_user.user_id,
                   'member', 1, NULL,
                   membership.joined_at, membership.updated_at
            FROM memberships AS membership
            JOIN web_users AS web_user
              ON web_user.participant_id = membership.participant_id
             AND web_user.active = 1
             AND web_user.role = 'user'
            WHERE membership.active = 1
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO room_web_members
                (conversation_id, web_user_id, access_role, active,
                 invited_by_web_user_id, created_at, updated_at)
            SELECT grant_row.conversation_id, grant_row.web_user_id,
                   'member', 1, grant_row.granted_by_web_user_id,
                   grant_row.created_at, grant_row.updated_at
            FROM room_task_grants AS grant_row
            JOIN web_users AS web_user
              ON web_user.user_id = grant_row.web_user_id
             AND web_user.active = 1
            """
        )

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

    @staticmethod
    def _migrate_reusable_agent_invitations(conn: sqlite3.Connection) -> None:
        """Split v14's one-connector invitation row into reusable grants."""

        invitation_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'agent_invitations'"
        ).fetchone()
        if invitation_table is None:
            return
        columns = {
            str(row["name"])
            for row in conn.execute(
                "PRAGMA table_info(agent_invitations)"
            ).fetchall()
        }
        if "reuse_policy" in columns:
            connector_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'agent_connectors'"
            ).fetchone()
            if connector_table is None:
                raise BridgeError("reusable invitations require agent_connectors")
            return
        if "connector_id" not in columns:
            raise BridgeError("unsupported agent_invitations schema")

        legacy_table = "agent_invitations_v14"
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(f"DROP TABLE IF EXISTS {legacy_table}")
            conn.execute(
                f"ALTER TABLE agent_invitations RENAME TO {legacy_table}"
            )
            for index_name in (
                "idx_agent_invitations_room_created",
                "idx_agent_invitations_status_expires",
                "idx_agent_invitations_participant",
                "idx_agent_invitations_connector",
            ):
                conn.execute(f"DROP INDEX IF EXISTS {index_name}")
            for statement in INVITATION_SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute(
                f"""
                INSERT INTO agent_invitations
                    (invitation_id, token_hash, conversation_id, product,
                     requested_mode, adapter_kind, reuse_policy, max_uses,
                     use_count, status, created_by_web_user_id, created_at,
                     expires_at, first_accepted_at, last_accepted_at,
                     revoked_at, updated_at)
                SELECT invitation_id, token_hash, conversation_id, product,
                       requested_mode, adapter_kind, 'single', 1,
                       CASE WHEN accepted_at IS NOT NULL THEN 1 ELSE 0 END,
                       CASE status
                           WHEN 'pending' THEN 'active'
                           WHEN 'accepted' THEN 'exhausted'
                           ELSE status
                       END,
                       created_by_web_user_id, created_at, expires_at,
                       accepted_at, accepted_at, revoked_at, updated_at
                FROM {legacy_table}
                """
            )
            conn.execute(
                f"""
                INSERT INTO agent_connectors
                    (connector_id, invitation_id, conversation_id,
                     accepted_participant_id,
                     initial_session_id, enrollment_token_hash,
                     enrollment_last_used_at, setup_status,
                     setup_detail_json, setup_updated_at,
                     connector_last_seen_at, created_at, revoked_at, updated_at)
                SELECT connector_id, invitation_id, conversation_id,
                       accepted_participant_id,
                       accepted_session_id, enrollment_token_hash,
                       enrollment_last_used_at,
                       CASE WHEN setup_status = 'awaiting_acceptance'
                            THEN 'awaiting_setup' ELSE setup_status END,
                       setup_detail_json, setup_updated_at,
                       connector_last_seen_at,
                       COALESCE(accepted_at, updated_at),
                       CASE WHEN status = 'revoked' THEN revoked_at ELSE NULL END,
                       updated_at
                FROM {legacy_table}
                WHERE connector_id IS NOT NULL
                  AND accepted_participant_id IS NOT NULL
                  AND accepted_session_id IS NOT NULL
                  AND enrollment_token_hash IS NOT NULL
                """
            )
            conn.execute(f"DROP TABLE {legacy_table}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    @staticmethod
    def _migrate_agent_connector_conversations(conn: sqlite3.Connection) -> None:
        """Give every v15 connector its own movable room binding."""

        connector_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'agent_connectors'"
        ).fetchone()
        if connector_table is None:
            return
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(agent_connectors)").fetchall()
        }
        if "conversation_id" in columns:
            return
        conn.execute(
            "ALTER TABLE agent_connectors ADD COLUMN conversation_id TEXT "
            "REFERENCES rooms(conversation_id)"
        )
        conn.execute(
            """
            UPDATE agent_connectors
            SET conversation_id = (
                SELECT invitation.conversation_id
                FROM agent_invitations AS invitation
                WHERE invitation.invitation_id = agent_connectors.invitation_id
            )
            """
        )
        missing = int(
            conn.execute(
                "SELECT COUNT(*) FROM agent_connectors "
                "WHERE conversation_id IS NULL OR trim(conversation_id) = ''"
            ).fetchone()[0]
        )
        if missing:
            raise BridgeError(
                "agent connector room migration would leave "
                f"{missing} connector(s) without a room"
            )

    @staticmethod
    def _migrate_connector_identity_bindings(conn: sqlite3.Connection) -> None:
        """Snapshot immutable connector identity without invalidating v22 clients."""

        connector_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'agent_connectors'"
        ).fetchone()
        if connector_table is None:
            return
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(agent_connectors)").fetchall()
        }
        additions = {
            "binding_version": (
                "INTEGER NOT NULL DEFAULT 1 CHECK (binding_version IN (1, 2))"
            ),
            "requested_username": "TEXT",
            "bound_client_type": "TEXT",
            "bound_roles_json": "TEXT",
            "bound_capabilities_json": "TEXT",
            "previous_enrollment_token_hash": "TEXT",
            "previous_enrollment_valid_until": "REAL",
            "enrollment_rotated_at": "REAL",
            "enrollment_rotation_count": (
                "INTEGER NOT NULL DEFAULT 0 "
                "CHECK (enrollment_rotation_count >= 0)"
            ),
            "enrollment_credential_version": (
                "INTEGER NOT NULL DEFAULT 1 "
                "CHECK (enrollment_credential_version >= 1)"
            ),
            "enrollment_rotation_required_at": "REAL",
            "enrollment_rotation_requested_by_web_user_id": "TEXT",
            "revoked_by_web_user_id": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in columns:
                conn.execute(
                    f"ALTER TABLE agent_connectors ADD COLUMN {name} {declaration}"
                )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_connectors_rotation_required "
            "ON agent_connectors(enrollment_rotation_required_at, revoked_at)"
        )
        conn.execute(
            """
            UPDATE agent_connectors
            SET bound_client_type = (
                    SELECT participant.client_type
                    FROM participants AS participant
                    WHERE participant.participant_id =
                          agent_connectors.accepted_participant_id
                )
            WHERE bound_client_type IS NULL OR trim(bound_client_type) = ''
            """
        )
        conn.execute(
            """
            UPDATE agent_connectors
            SET requested_username = (
                    SELECT CASE
                        WHEN substr(
                                 participant.client_type,
                                 1,
                                 length(invitation.product) + 1
                             ) = invitation.product || '-'
                        THEN substr(
                                 participant.client_type,
                                 length(invitation.product) + 2
                             )
                        ELSE participant.client_type
                    END
                    FROM participants AS participant
                    JOIN agent_invitations AS invitation
                      ON invitation.invitation_id = agent_connectors.invitation_id
                    WHERE participant.participant_id =
                          agent_connectors.accepted_participant_id
                )
            WHERE requested_username IS NULL OR trim(requested_username) = ''
            """
        )
        conn.execute(
            """
            UPDATE agent_connectors
            SET bound_roles_json = COALESCE((
                    SELECT membership.roles_json
                    FROM memberships AS membership
                    WHERE membership.conversation_id =
                          agent_connectors.conversation_id
                      AND membership.participant_id =
                          agent_connectors.accepted_participant_id
                ), '[]')
            WHERE bound_roles_json IS NULL OR trim(bound_roles_json) = ''
            """
        )
        conn.execute(
            """
            UPDATE agent_connectors
            SET bound_capabilities_json = COALESCE((
                    SELECT participant.capabilities_json
                    FROM participants AS participant
                    WHERE participant.participant_id =
                          agent_connectors.accepted_participant_id
                ), '[]')
            WHERE bound_capabilities_json IS NULL
               OR trim(bound_capabilities_json) = ''
            """
        )
        incomplete = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM agent_connectors
                WHERE requested_username IS NULL OR trim(requested_username) = ''
                   OR bound_client_type IS NULL OR trim(bound_client_type) = ''
                   OR bound_roles_json IS NULL OR trim(bound_roles_json) = ''
                   OR bound_capabilities_json IS NULL
                      OR trim(bound_capabilities_json) = ''
                """
            ).fetchone()[0]
        )
        if incomplete:
            raise BridgeError(
                "connector identity migration left "
                f"{incomplete} incomplete binding(s)"
            )

    @staticmethod
    def _migrate_native_tui_bindings(conn: sqlite3.Connection) -> None:
        """Add native-TUI state without rebuilding live invitation tables."""

        invitation_columns = {
            str(row["name"])
            for row in conn.execute(
                "PRAGMA table_info(agent_invitations)"
            ).fetchall()
        }
        if "tui_adapter_kind" not in invitation_columns:
            conn.execute(
                "ALTER TABLE agent_invitations ADD COLUMN tui_adapter_kind TEXT"
            )
        # This column is reserved for the native-session bridge. Early v26
        # development builds briefly copied first-party adapter names here;
        # clear them so legacy Codex/Claude invitations keep their unchanged
        # resident path after an in-place upgrade.
        conn.execute(
            "UPDATE agent_invitations SET tui_adapter_kind = NULL "
            "WHERE tui_adapter_kind IN ('codex', 'claude-code')"
        )

        connector_columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(agent_connectors)").fetchall()
        }
        legacy_access_mode = "tui_access_mode" in connector_columns
        additions = {
            "tui_endpoint_id": "TEXT",
            "tui_native_session_id": "TEXT",
            "tui_state": "TEXT NOT NULL DEFAULT 'unbound'",
            "tui_capabilities_json": "TEXT NOT NULL DEFAULT '[]'",
            "tui_last_seen_at": "REAL",
            "tui_active_task_id": "TEXT",
            "tui_detail_json": "TEXT NOT NULL DEFAULT '{}'",
            "native_delivery_mode": (
                "TEXT NOT NULL DEFAULT 'legacy_shadow' "
                "CHECK (native_delivery_mode IN "
                "('legacy_shadow', 'native_preferred'))"
            ),
            "native_lease_id": "TEXT",
            "native_process_epoch": "TEXT",
            "native_lease_expires_at": "REAL",
            "native_binding_source": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in connector_columns:
                conn.execute(
                    f"ALTER TABLE agent_connectors ADD COLUMN {name} {declaration}"
                )
        if legacy_access_mode:
            # v35 deliberately stops persisting a guessed TUI permission mode.
            # Keep the legacy column in upgraded SQLite databases so the
            # migration remains additive, but erase its stale value. The
            # bound local runtime is the only permission authority for every
            # turn and may change independently at any time.
            conn.execute(
                "UPDATE agent_connectors SET tui_access_mode = 'unknown' "
                "WHERE tui_access_mode IS NULL OR tui_access_mode <> 'unknown'"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_connectors_tui_endpoint "
            "ON agent_connectors(tui_endpoint_id, revoked_at, tui_last_seen_at DESC)"
        )

    @staticmethod
    def _repair_connector_room_bindings(conn: sqlite3.Connection) -> None:
        """Repair pre-v21 connector/session room drift without moving memberships.

        An invitation is the immutable authority that created a connector. Room
        renames update both rows atomically, so a mismatch means an older
        migration rebound only the central connector record while its local
        resident configuration stayed in the invitation room.
        """

        conn.execute(
            """
            UPDATE agent_connectors
            SET conversation_id = (
                    SELECT invitation.conversation_id
                    FROM agent_invitations AS invitation
                    WHERE invitation.invitation_id = agent_connectors.invitation_id
                ),
                updated_at = MAX(updated_at, CAST(strftime('%s', 'now') AS REAL))
            WHERE EXISTS (
                SELECT 1 FROM agent_invitations AS invitation
                WHERE invitation.invitation_id = agent_connectors.invitation_id
                  AND invitation.conversation_id != agent_connectors.conversation_id
            )
            """
        )
        conn.execute(
            """
            UPDATE agent_sessions
            SET registered_conversation_id = (
                    SELECT connector.conversation_id
                    FROM agent_connectors AS connector
                    WHERE connector.connector_id = agent_sessions.connector_id
                )
            WHERE connector_id IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM agent_connectors AS connector
                  WHERE connector.connector_id = agent_sessions.connector_id
                    AND connector.conversation_id
                        != agent_sessions.registered_conversation_id
              )
            """
        )

    @staticmethod
    def _restore_legacy_migrated_memberships(conn: sqlite3.Connection) -> None:
        """Restore source-room membership removed by the old move semantics.

        Migration is additive, while each resident connector remains bound to one
        room. Older releases deactivated the source membership and recorded a
        ``migrated`` block. Reactivate only memberships whose source room is still
        active; abandoned-room history remains untouched.
        """

        conn.execute(
            """
            UPDATE memberships
            SET active = 1,
                updated_at = MAX(
                    updated_at,
                    COALESCE((
                        SELECT block.blocked_at
                        FROM agent_room_blocks AS block
                        WHERE block.conversation_id = memberships.conversation_id
                          AND block.participant_id = memberships.participant_id
                          AND block.reason = 'migrated'
                    ), updated_at)
                )
            WHERE active = 0
              AND EXISTS (
                  SELECT 1 FROM rooms AS room
                  WHERE room.conversation_id = memberships.conversation_id
                    AND room.status = 'active'
              )
              AND EXISTS (
                  SELECT 1 FROM agent_room_blocks AS block
                  WHERE block.conversation_id = memberships.conversation_id
                    AND block.participant_id = memberships.participant_id
                    AND block.reason = 'migrated'
              )
            """
        )
        conn.execute(
            """
            DELETE FROM agent_room_blocks
            WHERE reason = 'migrated'
              AND EXISTS (
                  SELECT 1 FROM memberships AS membership
                  WHERE membership.conversation_id = agent_room_blocks.conversation_id
                    AND membership.participant_id = agent_room_blocks.participant_id
                    AND membership.active = 1
              )
            """
        )

    @staticmethod
    def _backfill_agent_lifecycle_states(conn: sqlite3.Connection) -> None:
        """Seed inactivity anchors without changing any current membership."""

        conn.execute(
            """
            INSERT OR IGNORE INTO agent_lifecycle_states
                (participant_id, access_granted_at, last_spoke_at,
                 reinvite_required, expired_at, expired_reason, updated_at)
            SELECT participant.participant_id,
                   MAX(
                       participant.created_at,
                       COALESCE((
                           SELECT MAX(membership.joined_at)
                           FROM memberships AS membership
                           WHERE membership.participant_id = participant.participant_id
                       ), participant.created_at),
                       COALESCE((
                           SELECT MAX(session.created_at)
                           FROM agent_sessions AS session
                           WHERE session.participant_id = participant.participant_id
                       ), participant.created_at),
                       COALESCE((
                           SELECT MAX(connector.created_at)
                           FROM agent_connectors AS connector
                           WHERE connector.accepted_participant_id = participant.participant_id
                       ), participant.created_at)
                   ),
                   (
                       SELECT MAX(message.created_at)
                       FROM messages AS message
                       WHERE message.sender_participant_id = participant.participant_id
                   ),
                   0, NULL, NULL, participant.last_seen
            FROM participants AS participant
            WHERE participant.participant_id != ?
              AND NOT EXISTS (
                  SELECT 1 FROM web_users AS web_user
                  WHERE web_user.participant_id = participant.participant_id
              )
            """,
            (OWNER_PARTICIPANT_ID,),
        )

    @classmethod
    def _backfill_admin_chat_authorization_grants(
        cls,
        conn: sqlite3.Connection,
    ) -> None:
        """Snapshot historical authenticated admin messages as authority sources.

        The snapshot records who was an active administrator when the message was
        written. Later role or session changes cannot manufacture authority for
        ordinary messages, and revocation remains a separate durable state.
        """

        rows = conn.execute(
            """
            SELECT message.*, web_user.user_id AS issuer_web_user_id,
                   web_user.username AS issuer_username,
                   web_user.role AS issuer_role
            FROM messages AS message
            JOIN web_sessions AS web_session
              ON web_session.session_id = message.authorized_session_id
            JOIN web_users AS web_user
              ON web_user.user_id = web_session.user_id
             AND web_user.participant_id = message.sender_participant_id
            WHERE web_user.role = 'admin'
              AND message.message_kind = 'message'
              AND NOT EXISTS (
                  SELECT 1 FROM chat_authorization_grants AS grant_record
                  WHERE grant_record.source_message_id = message.message_id
              )
            ORDER BY message.sequence
            """
        ).fetchall()
        for row in rows:
            cls._insert_admin_chat_authorization_grant_locked(
                conn,
                message=row,
                issuer_web_user_id=str(row["issuer_web_user_id"]),
                issuer_username=str(row["issuer_username"]),
                issuer_role=str(row["issuer_role"]),
            )

    @staticmethod
    def _freeze_legacy_chat_authorizations(conn: sqlite3.Connection) -> None:
        """Keep the old ledger for audit while removing all chat authority.

        Ordinary room prose is deliberately not an execution authorization
        boundary.  Existing rows remain queryable so a rolling upgrade loses no
        history, but every row is projected as frozen and no new row is created.
        """

        if not CHAT_AUTHORIZATION_FROZEN:
            return
        now = time.time()
        conn.execute(
            """
            UPDATE chat_authorization_grants
            SET authority_kind = 'legacy_frozen',
                revoked_at = COALESCE(revoked_at, ?),
                revocation_reason = COALESCE(
                    revocation_reason,
                    'chat_authorization_feature_frozen'
                )
            WHERE authority_kind != 'legacy_frozen'
               OR revoked_at IS NULL
            """,
            (now,),
        )

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


    def compact_optional_backlog(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        keep_recent: int = DEFAULT_OFFLINE_BACKLOG_KEEP_MESSAGES,
    ) -> dict[str, Any]:
        """Cancel only old optional deliveries while preserving room history.

        This is used for an explicit reconnect backlog event. Required replies,
        actionable participant/role deliveries, and the newest optional window
        remain in the normal delivery queue. Cancelled rows keep their original
        message in history/search and gain an audit reason instead of pretending
        the Agent read or acknowledged their bodies.
        """

        participant = opaque_id(participant_id, field="participant_id")
        session = opaque_id(
            authorized_session_id,
            field="authorized_session_id",
        )
        if isinstance(keep_recent, bool):
            raise ValidationError("keep_recent must be an integer")
        try:
            keep = int(keep_recent)
        except (TypeError, ValueError) as exc:
            raise ValidationError("keep_recent must be an integer") from exc
        if not 1 <= keep <= MAX_OFFLINE_BACKLOG_KEEP_MESSAGES:
            raise ValidationError(
                "keep_recent must be between 1 and "
                f"{MAX_OFFLINE_BACKLOG_KEEP_MESSAGES}"
            )

        now = time.time()
        compacted_count = 0
        oldest_sequence: int | None = None
        newest_sequence: int | None = None
        sender_counts: Counter[tuple[str, str, str]] = Counter()
        with self._transaction() as conn:
            self._archive_stale_rooms_locked(conn, now=now)
            session_row = self._require_live_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=now,
            )
            conversation = str(session_row["registered_conversation_id"])
            self._require_membership(conn, participant, conversation)

            candidate_where = """
                delivery.participant_id = ?
                AND delivery.state IN ('pending', 'delivered')
                AND delivery.actionable = 0
                AND instr(delivery.reasons_json, '"mention"') = 0
                AND instr(delivery.reasons_json, '"agent_request"') = 0
                AND message.conversation_id = ?
            """
            optional_total = int(
                conn.execute(
                    "SELECT COUNT(*) FROM message_deliveries AS delivery "
                    "JOIN messages AS message "
                    "ON message.message_id = delivery.message_id "
                    f"WHERE {candidate_where}",
                    (participant, conversation),
                ).fetchone()[0]
            )
            protected_pending = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM message_deliveries AS delivery
                    JOIN messages AS message
                      ON message.message_id = delivery.message_id
                    WHERE delivery.participant_id = ?
                      AND delivery.state IN ('pending', 'delivered')
                      AND message.conversation_id = ?
                      AND (
                          delivery.actionable = 1
                          OR instr(delivery.reasons_json, '"mention"') > 0
                          OR instr(
                              delivery.reasons_json,
                              '"agent_request"'
                          ) > 0
                      )
                    """,
                    (participant, conversation),
                ).fetchone()[0]
            )

            while optional_total - compacted_count > keep:
                rows = conn.execute(
                    """
                    SELECT delivery.message_id, delivery.reasons_json,
                           message.sequence,
                           sender.participant_id AS sender_participant_id,
                           sender.client_type AS sender_client_type,
                           sender.display_name AS sender_display_name
                    FROM message_deliveries AS delivery
                    JOIN messages AS message
                      ON message.message_id = delivery.message_id
                    JOIN participants AS sender
                      ON sender.participant_id = message.sender_participant_id
                    WHERE """
                    + candidate_where
                    + " ORDER BY message.sequence DESC LIMIT 500 OFFSET ?",
                    (participant, conversation, keep),
                ).fetchall()
                if not rows:
                    break
                updates: list[tuple[str, str, str]] = []
                for row in rows:
                    try:
                        reasons = list(json.loads(str(row["reasons_json"] or "[]")))
                    except (TypeError, json.JSONDecodeError):
                        reasons = []
                    if "offline_compacted" not in reasons:
                        reasons.append("offline_compacted")
                    updates.append(
                        (
                            compact_json(reasons),
                            str(row["message_id"]),
                            participant,
                        )
                    )
                    sequence = int(row["sequence"])
                    oldest_sequence = (
                        sequence
                        if oldest_sequence is None
                        else min(oldest_sequence, sequence)
                    )
                    newest_sequence = (
                        sequence
                        if newest_sequence is None
                        else max(newest_sequence, sequence)
                    )
                    sender_counts[
                        (
                            str(row["sender_participant_id"]),
                            str(row["sender_client_type"]),
                            str(row["sender_display_name"]),
                        )
                    ] += 1
                conn.executemany(
                    """
                    UPDATE message_deliveries
                    SET state = 'cancelled', delivery_stage = 'cancelled',
                        reasons_json = ?, actionable = 0
                    WHERE message_id = ? AND participant_id = ?
                      AND state IN ('pending', 'delivered')
                    """,
                    updates,
                )
                compacted_count += len(rows)

        sender_summary = [
            {
                "participant_id": sender[0],
                "client_type": sender[1],
                "display_name": sender[2],
                "message_count": count,
            }
            for sender, count in sorted(
                sender_counts.items(),
                key=lambda item: (-item[1], item[0][2], item[0][0]),
            )[:10]
        ]
        return {
            "applied": compacted_count > 0,
            "conversation_id": conversation,
            "compacted_optional_count": compacted_count,
            "kept_recent_optional_count": min(optional_total, keep),
            "protected_pending_count": protected_pending,
            "oldest_compacted_sequence": oldest_sequence,
            "newest_compacted_sequence": newest_sequence,
            "sender_counts": sender_summary,
            "other_sender_message_count": max(
                0,
                compacted_count
                - sum(item["message_count"] for item in sender_summary),
            ),
            "history_preserved": True,
            "history_hint": (
                "Use agent_history with before_sequence/around_sequence or "
                "agent_search_history when older context is relevant."
            ),
        }

    def _native_delivery_handoff(
        self,
        *,
        participant_id: str,
        authorized_session_id: str | None,
    ) -> dict[str, Any] | None:
        if authorized_session_id is None:
            return None
        participant = opaque_id(participant_id, field="participant_id")
        session_id = opaque_id(
            authorized_session_id,
            field="authorized_session_id",
        )
        now = time.time()
        with self._connection() as conn:
            session = self._require_live_session(
                conn,
                session_id=session_id,
                participant_id=participant,
                now=now,
            )
            component = str(session["component"] or "unknown")
            connector_id = str(session["connector_id"] or "")
            if component not in {"listener", "chat"} or not connector_id:
                return None
            connector = conn.execute(
                "SELECT native_delivery_mode, native_lease_id, "
                "native_lease_expires_at, tui_native_session_id "
                "FROM agent_connectors WHERE connector_id = ? "
                "AND accepted_participant_id = ? AND revoked_at IS NULL",
                (connector_id, participant),
            ).fetchone()
            if (
                connector is None
                or str(connector["native_delivery_mode"] or "")
                != "native_preferred"
            ):
                return None
        return {
            "active": True,
            "connector_id": connector_id,
            "component": component,
            "lease_id": str(connector["native_lease_id"] or "") or None,
            "native_session_id": (
                str(connector["tui_native_session_id"] or "") or None
            ),
            "lease_expires_at": (
                float(connector["native_lease_expires_at"])
                if connector["native_lease_expires_at"] is not None
                else None
            ),
            "reason": "exact_native_session_owns_delivery",
        }

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
        # Keep each Agent context page small and predictable.  Callers can
        # follow ``has_more`` for up to five pages (100 messages) per model
        # turn without one request flooding the context window.
        normalized_limit = max(1, min(int(limit), MAX_WAIT_MESSAGES_PAGE_SIZE))
        deadline = time.monotonic() + wait_for
        conversation = self._authorized_session_room(
            participant_id=participant,
            authorized_session_id=authorized_session_id,
        )
        with self._connection() as conn:
            if conversation is None:
                # Internal callers may intentionally omit a session and read
                # the participant's aggregate inbox.
                self_row = conn.execute(
                    """
                    SELECT participant.participant_id, participant.client_type,
                           participant.display_name, participant.signature,
                           COALESCE((
                               SELECT membership.roles_json
                               FROM memberships AS membership
                               WHERE membership.participant_id =
                                     participant.participant_id
                                 AND membership.active = 1
                               ORDER BY membership.updated_at DESC
                               LIMIT 1
                           ), '[]') AS roles_json
                    FROM participants AS participant
                    WHERE participant.participant_id = ?
                    """,
                    (participant,),
                ).fetchone()
            else:
                self_row = conn.execute(
                    """
                    SELECT participant.participant_id, participant.client_type,
                           participant.display_name, participant.signature,
                           membership.roles_json
                    FROM participants AS participant
                    JOIN memberships AS membership
                      ON membership.participant_id = participant.participant_id
                     AND membership.conversation_id = ?
                     AND membership.active = 1
                    WHERE participant.participant_id = ?
                    """,
                    (conversation, participant),
                ).fetchone()
        if self_row is None:
            raise ConflictError("Agent is not an active member of its session room")
        self_identity = {
            "participant_id": str(self_row["participant_id"]),
            "client_type": str(self_row["client_type"]),
            "display_name": str(self_row["display_name"]),
            "signature": str(self_row["signature"]),
            "roles": json.loads(str(self_row["roles_json"] or "[]")),
            "identity_rule": (
                "display_name is your fixed public name; a shadow listener and "
                "task executor are seats of this same public identity"
            ),
        }

        native_handoff = self._native_delivery_handoff(
            participant_id=participant,
            authorized_session_id=authorized_session_id,
        )
        if native_handoff is not None:
            if wait_for > 0:
                time.sleep(wait_for)
            self.heartbeat(
                participant,
                authorized_session_id=authorized_session_id,
            )
            backlog = self._pending_manifest(
                participant,
                conversation_id=conversation,
            )
            return {
                "participant_id": participant,
                "conversation_id": conversation,
                "self_identity": self_identity,
                "messages": [],
                "count": 0,
                "timed_out": True,
                "last_sequence": None,
                "backlog": backlog,
                "pending_count": backlog["pending_count"],
                "has_more": backlog["pending_count"] > 0,
                "native_handoff": native_handoff,
            }

        while True:
            messages = self._pending_messages(
                participant,
                limit=normalized_limit,
                auto_claim_roles=bool(auto_claim_roles),
                authorized_session_id=authorized_session_id,
                conversation_id=conversation,
            )
            if messages:
                backlog = self._pending_manifest(
                    participant,
                    conversation_id=conversation,
                )
                return {
                    "participant_id": participant,
                    "conversation_id": conversation,
                    "self_identity": self_identity,
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
                backlog = self._pending_manifest(
                    participant,
                    conversation_id=conversation,
                )
                return {
                    "participant_id": participant,
                    "conversation_id": conversation,
                    "self_identity": self_identity,
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
        conversation: str | None = None
        native_handoff: dict[str, Any] | None = None
        with self._transaction() as conn:
            self._archive_stale_rooms_locked(conn, now=now)
            if authorized_session_id is not None:
                session_row = self._require_live_session(
                    conn,
                    session_id=opaque_id(
                        authorized_session_id,
                        field="authorized_session_id",
                    ),
                    participant_id=participant,
                    now=now,
                )
                conversation = str(session_row["registered_conversation_id"])
                component = str(session_row["component"] or "unknown")
                connector_id = str(session_row["connector_id"] or "")
                if component in {"listener", "chat"} and connector_id:
                    connector = conn.execute(
                        "SELECT native_delivery_mode, native_lease_id, "
                        "native_lease_expires_at, tui_native_session_id "
                        "FROM agent_connectors WHERE connector_id = ? "
                        "AND accepted_participant_id = ? AND revoked_at IS NULL",
                        (connector_id, participant),
                    ).fetchone()
                    if (
                        connector is not None
                        and str(connector["native_delivery_mode"] or "")
                        == "native_preferred"
                    ):
                        native_handoff = {
                            "active": True,
                            "connector_id": connector_id,
                            "component": component,
                            "lease_id": (
                                str(connector["native_lease_id"] or "") or None
                            ),
                            "native_session_id": (
                                str(connector["tui_native_session_id"] or "")
                                or None
                            ),
                            "lease_expires_at": (
                                float(connector["native_lease_expires_at"])
                                if connector["native_lease_expires_at"] is not None
                                else None
                            ),
                            "reason": "exact_native_session_owns_delivery",
                        }
            known = conn.execute(
                "SELECT participant_id FROM participants WHERE participant_id = ?",
                (participant,),
            ).fetchone()
            if known is None:
                raise NotFoundError(f"unknown participant: {participant}")
            if conversation is None:
                room_sequence = int(
                    conn.execute(
                        "SELECT COALESCE(MAX(sequence), 0) FROM messages"
                    ).fetchone()[0]
                )
            else:
                self._require_membership(conn, participant, conversation)
                room_sequence = int(
                    conn.execute(
                        "SELECT COALESCE(MAX(sequence), 0) FROM messages "
                        "WHERE conversation_id = ?",
                        (conversation,),
                    ).fetchone()[0]
                )
        # A corrupt or manually edited Last-Event-ID must not suppress every
        # future event forever.  Global message sequence is monotonic, so it is
        # the largest cursor the server can currently have issued.
        cursor = min(requested_cursor, room_sequence)
        backlog = self._pending_manifest(
            participant,
            conversation_id=conversation,
        )
        new_since_cursor = self._pending_manifest(
            participant,
            after_sequence=cursor,
            conversation_id=conversation,
        )
        room_activity_since_cursor = self._activity_manifest(
            participant,
            after_sequence=cursor,
            conversation_id=conversation,
        )
        result = {
            "participant_id": participant,
            "conversation_id": conversation,
            # Cursor tracks this connector room's append-only sequence, not
            # unread state. Another room cannot wake or advance this listener.
            "cursor": room_sequence,
            "has_new": new_since_cursor["pending_count"] > 0,
            "has_room_activity": room_activity_since_cursor["activity_count"] > 0,
            "backlog": backlog,
            "new_since_cursor": new_since_cursor,
            "room_activity_since_cursor": room_activity_since_cursor,
            "server_time": time.time(),
        }
        if native_handoff is not None:
            result["has_new"] = False
            result["has_room_activity"] = False
            result["native_handoff"] = native_handoff
        return result

    def wait_for_notification(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        after_sequence: int | None = None,
        wait_seconds: float = 25.0,
    ) -> dict[str, Any]:
        """Wait for room activity without repeatedly rebuilding delivery aggregates.

        The append-only global sequence is the cheap change detector.  Full
        participant-scoped manifests and sliding-session renewal run only when
        that sequence changes (plus the initial snapshot), while delivery rows
        remain the authoritative backlog and are never consumed here.
        """
        wait_for = max(0.0, min(float(wait_seconds), 60.0))
        deadline = time.monotonic() + wait_for
        snapshot = self.notification_snapshot(
            participant_id=participant_id,
            authorized_session_id=authorized_session_id,
            after_sequence=after_sequence,
        )
        if snapshot["has_room_activity"]:
            snapshot["timed_out"] = False
            return snapshot
        observed_sequence = int(snapshot["cursor"])
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                snapshot["timed_out"] = True
                return snapshot
            time.sleep(min(max(self.poll_interval_seconds, 0.5), remaining))
            latest_sequence = self._message_sequence(
                snapshot.get("conversation_id")
            )
            if latest_sequence <= observed_sequence:
                continue
            observed_sequence = latest_sequence
            snapshot = self.notification_snapshot(
                participant_id=participant_id,
                authorized_session_id=authorized_session_id,
                after_sequence=after_sequence,
            )
            if snapshot["has_room_activity"]:
                snapshot["timed_out"] = False
                return snapshot

    def _message_sequence(self, conversation_id: object = None) -> int:
        """Read a monotonic room change key without renewing a session."""

        with self._connection() as conn:
            if conversation_id is not None:
                return int(
                    conn.execute(
                        "SELECT COALESCE(MAX(sequence), 0) FROM messages "
                        "WHERE conversation_id = ?",
                        (str(conversation_id),),
                    ).fetchone()[0]
                )
            return int(
                conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) FROM messages"
                ).fetchone()[0]
            )

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
        if original is None:
            raise NotFoundError(f"unknown message: {original_id}")
        with self._connection() as conn:
            self._require_live_room_session(
                conn,
                session_id=opaque_id(
                    authorized_session_id,
                    field="authorized_session_id",
                ),
                participant_id=participant,
                conversation_id=str(original["conversation_id"]),
                now=time.time(),
            )
        self._require_eligible_participant(participant, original_id)
        continued_top_level = original["reply_to"] is not None
        continuation_target: str | None = None
        continuation_mentions = list(mentions or [])
        if continued_top_level:
            candidate = str(original["sender_participant_id"])
            with self._connection() as conn:
                active_sender = conn.execute(
                    "SELECT 1 FROM memberships "
                    "WHERE conversation_id = ? AND participant_id = ? "
                    "AND active = 1",
                    (str(original["conversation_id"]), candidate),
                ).fetchone()
            if candidate != participant and active_sender is not None:
                continuation_target = candidate
                if candidate not in continuation_mentions:
                    continuation_mentions.append(candidate)
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
                audience_kind="room",
                audience_value="*",
                reply_to=None if continued_top_level else original_id,
                refs=refs,
                mentions=continuation_mentions,
            )
        except Exception:
            if claim_acquired:
                try:
                    self._release(participant, original_id)
                except BridgeError:
                    pass
            raise
        self._ack(participant, original_id)
        replied_at = time.time()
        with self._transaction() as conn:
            conn.execute(
                """
                UPDATE message_deliveries
                SET delivery_stage = 'replied',
                    native_replied_at = COALESCE(native_replied_at, ?)
                WHERE message_id = ? AND participant_id = ?
                  AND native_applied_at IS NOT NULL
                  AND state != 'cancelled'
                """,
                (replied_at, original_id, participant),
            )
        return {
            "reply": reply_message,
            "original_message_id": original_id,
            "original_acked": True,
            "continued_top_level": continued_top_level,
            "continuation_notified_participant_id": continuation_target,
        }

    def history(
        self,
        *,
        participant_id: str,
        conversation_id: str,
        limit: int = 50,
        before_sequence: int | None = None,
        after_sequence: int | None = None,
        around_sequence: int | None = None,
        authorized_session_id: str | None = None,
    ) -> dict[str, Any]:
        participant = opaque_id(participant_id, field="participant_id")
        conversation = validate_conversation_id(conversation_id)
        normalized_limit = max(1, min(int(limit), 200))
        supplied_cursors = sum(
            value is not None
            for value in (before_sequence, after_sequence, around_sequence)
        )
        if supplied_cursors > 1:
            raise ValidationError(
                "before_sequence, after_sequence, and around_sequence cannot "
                "be used together"
            )
        with self._transaction() as conn:
            now = time.time()
            self._archive_stale_rooms_locked(conn, now=now)
            if authorized_session_id is not None:
                self._require_live_room_session(
                    conn,
                    session_id=opaque_id(
                        authorized_session_id,
                        field="authorized_session_id",
                    ),
                    participant_id=participant,
                    conversation_id=conversation,
                    now=now,
                )
            self._require_membership(conn, participant, conversation)
            if after_sequence is not None:
                rows = conn.execute(
                    "SELECT * FROM messages WHERE conversation_id = ? "
                    "AND sequence > ? ORDER BY sequence LIMIT ?",
                    (conversation, int(after_sequence), normalized_limit),
                ).fetchall()
            elif around_sequence is not None:
                center = max(0, int(around_sequence))
                rows = conn.execute(
                    "SELECT * FROM messages WHERE conversation_id = ? "
                    "ORDER BY ABS(sequence - ?), sequence LIMIT ?",
                    (conversation, center, normalized_limit),
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
        if around_sequence is not None:
            ordered_rows = sorted(rows, key=lambda row: int(row["sequence"]))
        else:
            ordered_rows = rows if after_sequence is not None else list(reversed(rows))
        with self._connection() as conn:
            messages = [
                self._message_payload(
                    row,
                    authorization=self._chat_authorization_for_message_locked(
                        conn,
                        message_id=str(row["message_id"]),
                        recipient_participant_id=participant,
                    ),
                )
                for row in ordered_rows
            ]
        first_sequence = messages[0]["sequence"] if messages else None
        last_sequence = messages[-1]["sequence"] if messages else None
        with self._connection() as conn:
            if around_sequence is not None:
                has_earlier = bool(
                    first_sequence is not None
                    and conn.execute(
                        "SELECT 1 FROM messages WHERE conversation_id = ? "
                        "AND sequence < ? LIMIT 1",
                        (conversation, first_sequence),
                    ).fetchone()
                )
                has_later = bool(
                    last_sequence is not None
                    and conn.execute(
                        "SELECT 1 FROM messages WHERE conversation_id = ? "
                        "AND sequence > ? LIMIT 1",
                        (conversation, last_sequence),
                    ).fetchone()
                )
                has_more = has_earlier or has_later
            elif after_sequence is not None:
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
            "around_sequence": (
                max(0, int(around_sequence))
                if around_sequence is not None
                else None
            ),
        }

    def search_history(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        conversation_id: str,
        query: str = "",
        message_id: str | None = None,
        sequence: int | None = None,
        sender_participant_id: str | None = None,
        created_after: float | None = None,
        created_before: float | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Search joined-room history without consuming or acknowledging delivery."""

        participant = opaque_id(participant_id, field="participant_id")
        session_id = opaque_id(
            authorized_session_id,
            field="authorized_session_id",
        )
        conversation = validate_conversation_id(conversation_id)
        normalized_query = str(query or "").strip()
        if len(normalized_query) > MAX_HISTORY_SEARCH_QUERY_LENGTH or any(
            ord(character) < 32 and character not in "\t\n\r"
            for character in normalized_query
        ):
            raise ValidationError(
                "query must contain at most "
                f"{MAX_HISTORY_SEARCH_QUERY_LENGTH} visible characters"
            )
        terms = normalized_query.split()
        if len(terms) > MAX_HISTORY_SEARCH_TERMS:
            raise ValidationError(
                f"query cannot contain more than {MAX_HISTORY_SEARCH_TERMS} terms"
            )
        normalized_message_id = (
            opaque_id(message_id, field="message_id") if message_id else None
        )
        if sequence is not None and isinstance(sequence, bool):
            raise ValidationError("sequence must be an integer")
        normalized_sequence = max(0, int(sequence)) if sequence is not None else None
        normalized_sender = (
            opaque_id(sender_participant_id, field="sender_participant_id")
            if sender_participant_id
            else None
        )
        normalized_after = self._finite_history_timestamp(
            created_after,
            field="created_after",
        )
        normalized_before = self._finite_history_timestamp(
            created_before,
            field="created_before",
        )
        if (
            normalized_after is not None
            and normalized_before is not None
            and normalized_after > normalized_before
        ):
            raise ValidationError("created_after cannot be later than created_before")
        if not any(
            (
                terms,
                normalized_message_id,
                normalized_sequence is not None,
                normalized_sender,
                normalized_after is not None,
                normalized_before is not None,
            )
        ):
            raise ValidationError("history search requires a query or exact filter")
        normalized_limit = max(1, min(int(limit), 20))

        conditions = ["message.conversation_id = ?"]
        parameters: list[Any] = [conversation]
        if normalized_message_id is not None:
            conditions.append("message.message_id = ?")
            parameters.append(normalized_message_id)
        if normalized_sequence is not None:
            conditions.append("message.sequence = ?")
            parameters.append(normalized_sequence)
        if normalized_sender is not None:
            conditions.append("message.sender_participant_id = ?")
            parameters.append(normalized_sender)
        if normalized_after is not None:
            conditions.append("message.created_at >= ?")
            parameters.append(normalized_after)
        if normalized_before is not None:
            conditions.append("message.created_at <= ?")
            parameters.append(normalized_before)
        for term in terms:
            escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            conditions.append("message.body LIKE ? ESCAPE '\\'")
            parameters.append(f"%{escaped}%")
        parameters.append(normalized_limit)

        now = time.time()
        with self._connection() as conn:
            self._require_live_room_session(
                conn,
                session_id=session_id,
                participant_id=participant,
                conversation_id=conversation,
                now=now,
            )
            self._require_membership(conn, participant, conversation)
            rows = conn.execute(
                f"""
                SELECT message.*,
                       sender.display_name AS sender_display_name,
                       sender.client_type AS sender_client_type,
                       original.sequence AS replied_sequence,
                       original.sender_participant_id AS replied_sender_participant_id,
                       original_sender.display_name AS replied_sender_display_name
                FROM messages AS message
                JOIN participants AS sender
                  ON sender.participant_id = message.sender_participant_id
                LEFT JOIN messages AS original
                  ON original.message_id = message.reply_to
                LEFT JOIN participants AS original_sender
                  ON original_sender.participant_id = original.sender_participant_id
                WHERE {' AND '.join(conditions)}
                ORDER BY message.sequence DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()

        with self._connection() as conn:
            results = [
                self._history_search_payload(
                    row,
                    terms=terms,
                    authorization=self._chat_authorization_for_message_locked(
                        conn,
                        message_id=str(row["message_id"]),
                        recipient_participant_id=participant,
                    ),
                )
                for row in rows
            ]
        return {
            "conversation_id": conversation,
            "query": normalized_query,
            "results": results,
            "count": len(results),
            "limit": normalized_limit,
            "state_changed": False,
            "context_hint": (
                "Call agent_history with around_sequence set to a result sequence "
                "to inspect nearby messages."
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
                self._require_live_room_session(
                    conn,
                    session_id=opaque_id(
                        authorized_session_id,
                        field="authorized_session_id",
                    ),
                    participant_id=caller,
                    conversation_id=conversation,
                    now=now,
                )
            self._require_membership(conn, caller, conversation)
            rows = conn.execute(
                """
                SELECT p.*, m.roles_json,
                       EXISTS (
                           SELECT 1
                           FROM agent_sessions AS session
                           WHERE session.participant_id = p.participant_id
                             AND session.registered_conversation_id = m.conversation_id
                             AND session.cleared_at IS NULL
                             AND session.revoked_at IS NULL
                             AND session.expires_at > ?
                             AND session.last_seen >= ?
                       ) AS room_agent_online,
                       EXISTS (
                           SELECT 1
                           FROM web_sessions AS web_session
                           JOIN web_users AS web_user
                             ON web_user.user_id = web_session.user_id
                           WHERE web_user.participant_id = p.participant_id
                             AND web_user.active = 1
                             AND web_session.revoked_at IS NULL
                             AND web_session.expires_at > ?
                             AND web_session.last_seen >= ?
                       ) AS room_web_online
                FROM memberships AS m
                JOIN participants AS p ON p.participant_id = m.participant_id
                WHERE m.conversation_id = ? AND m.active = 1
                ORDER BY p.display_name, p.participant_id
                """,
                (
                    now,
                    now - float(online_window_seconds),
                    now,
                    now - float(online_window_seconds),
                    conversation,
                ),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            online = bool(
                (
                    str(row["status"]) == "online"
                    and row["room_agent_online"]
                )
                or row["room_web_online"]
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
        conversation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        now = time.time()
        with self._connection() as conn:
            if authorized_session_id is not None:
                session_row = self._require_live_session(
                    conn,
                    session_id=opaque_id(
                        authorized_session_id,
                        field="authorized_session_id",
                    ),
                    participant_id=participant_id,
                    now=now,
                )
                bound_room = str(session_row["registered_conversation_id"])
                if conversation_id is not None and bound_room != conversation_id:
                    raise AuthorizationError(
                        f"Agent session is bound to conversation {bound_room}; "
                        f"use a room-specific connector for {conversation_id}"
                    )
                conversation_id = bound_room
            participant = conn.execute(
                "SELECT * FROM participants WHERE participant_id = ?",
                (participant_id,),
            ).fetchone()
            if participant is None:
                raise NotFoundError(f"unknown participant: {participant_id}")
            room_clause = (
                "AND message.conversation_id = ?" if conversation_id else ""
            )
            parameters: list[Any] = [participant_id, participant_id]
            if conversation_id:
                parameters.append(conversation_id)
            rows = conn.execute(
                f"""
                SELECT message.*,
                       delivery.state AS delivery_state,
                       delivery.reasons_json AS delivery_reasons_json,
                       delivery.priority AS delivery_priority,
                       delivery.actionable AS delivery_actionable,
                       delivery.first_delivered_at AS delivery_first_delivered_at,
                       delivery.last_delivered_at AS delivery_last_delivered_at,
                       delivery.acked_at AS delivery_acked_at,
                       delivery.delivery_stage AS delivery_stage,
                       delivery.native_session_id AS delivery_native_session_id,
                       delivery.native_event_id AS delivery_native_event_id,
                       delivery.native_injected_at AS delivery_native_injected_at,
                       delivery.native_applied_at AS delivery_native_applied_at,
                       delivery.native_replied_at AS delivery_native_replied_at,
                       delivery.shadow_seen_at AS delivery_shadow_seen_at,
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
                  {room_clause}
                ORDER BY
                    CASE
                        WHEN instr(delivery.reasons_json, '"mention"') > 0
                          OR instr(delivery.reasons_json, '"agent_request"') > 0
                        THEN 3
                        WHEN delivery.priority IN ('direct', 'mention') THEN 2
                        WHEN delivery.priority = 'important' THEN 1
                        ELSE 0
                    END DESC,
                    message.sequence
                LIMIT 500
                """,
                parameters,
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
                               delivery.delivery_stage AS delivery_stage,
                               delivery.native_session_id
                                   AS delivery_native_session_id,
                               delivery.native_event_id AS delivery_native_event_id,
                               delivery.native_injected_at
                                   AS delivery_native_injected_at,
                               delivery.native_applied_at
                                   AS delivery_native_applied_at,
                               delivery.native_replied_at
                                   AS delivery_native_replied_at,
                               delivery.shadow_seen_at AS delivery_shadow_seen_at,
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
                session_row = self._require_live_session(
                    conn,
                    session_id=opaque_id(
                        authorized_session_id,
                        field="authorized_session_id",
                    ),
                    participant_id=participant_id,
                    now=delivered_at,
                )
                if conversation_id is not None and str(
                    session_row["registered_conversation_id"]
                ) != conversation_id:
                    raise AuthorizationError(
                        "Agent session room changed while messages were delivered"
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
                        delivery_stage = CASE
                            WHEN delivery_stage IN (
                                'native_injected', 'native_applied', 'replied'
                            ) THEN delivery_stage
                            ELSE 'legacy_delivered'
                        END,
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
                           delivery.delivery_stage AS delivery_stage,
                           delivery.native_session_id AS delivery_native_session_id,
                           delivery.native_event_id AS delivery_native_event_id,
                           delivery.native_injected_at AS delivery_native_injected_at,
                           delivery.native_applied_at AS delivery_native_applied_at,
                           delivery.native_replied_at AS delivery_native_replied_at,
                           delivery.shadow_seen_at AS delivery_shadow_seen_at,
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
        with self._connection() as conn:
            return [
                self._message_payload(
                    row,
                    authorization=self._chat_authorization_for_message_locked(
                        conn,
                        message_id=str(row["message_id"]),
                        recipient_participant_id=participant_id,
                    ),
                )
                for row in delivered_rows
            ]

    def _apply_room_wake_policies(
        self,
        *,
        participant_id: str,
        conversations: list[dict[str, Any]],
        conversation_id: str | None,
        count_key: str,
        now: float,
        native_unassigned_only: bool = False,
    ) -> None:
        """Promote optional room activity to a wake without requiring reply."""

        by_room = {
            str(item["conversation_id"]): item for item in conversations
        }
        candidate_rooms = (
            [conversation_id]
            if conversation_id is not None
            else list(by_room)
        )
        if not candidate_rooms:
            return
        with self._connection() as conn:
            for room_id in candidate_rooms:
                policy_row = conn.execute(
                    "SELECT * FROM room_wake_policies WHERE conversation_id = ?",
                    (room_id,),
                ).fetchone()
                policy = self._room_wake_policy_payload(
                    policy_row,
                    conversation_id=room_id,
                )
                mode = str(policy["mode"])
                item = by_room.get(room_id)
                dnd_row = conn.execute(
                    "SELECT enabled_at, expires_at, timezone_name "
                    "FROM agent_room_dnd "
                    "WHERE participant_id = ? AND conversation_id = ?",
                    (participant_id, room_id),
                ).fetchone()
                dnd_active = bool(
                    dnd_row is not None and float(dnd_row["expires_at"]) > now
                )
                threshold_reset_at = (
                    float(dnd_row["expires_at"])
                    if dnd_row is not None and not dnd_active
                    else None
                )
                promote = bool(
                    mode == "all"
                    and item is not None
                    and int(item.get("policy_eligible_count") or 0) > 0
                )
                digest_pending_count = 0
                digest_oldest_created_at: float | None = None
                if mode == "digest" and not dnd_active:
                    metrics = conn.execute(
                        """
                        SELECT COUNT(*) AS pending_count,
                               MIN(message.sequence) AS oldest_sequence,
                               MAX(message.sequence) AS newest_sequence,
                               MIN(message.created_at) AS oldest_created_at,
                               MAX(message.created_at) AS newest_created_at
                        FROM message_deliveries AS delivery
                        JOIN messages AS message
                          ON message.message_id = delivery.message_id
                        JOIN memberships AS membership
                          ON membership.conversation_id = message.conversation_id
                         AND membership.participant_id = delivery.participant_id
                         AND membership.active = 1
                        WHERE delivery.participant_id = ?
                          AND message.conversation_id = ?
                          AND delivery.state IN ('pending', 'delivered')
                          AND message.sender_participant_id != ?
                          AND message.notification_mode = 'ordinary'
                          AND message.created_at >= ?
                          AND (? = 0 OR delivery.native_event_id IS NULL)
                          AND instr(
                              delivery.reasons_json,
                              '"echo_suppressed"'
                          ) = 0
                        """,
                        (
                            participant_id,
                            room_id,
                            participant_id,
                            threshold_reset_at or 0.0,
                            1 if native_unassigned_only else 0,
                        ),
                    ).fetchone()
                    digest_pending_count = int(metrics["pending_count"] or 0)
                    digest_oldest_created_at = (
                        float(metrics["oldest_created_at"])
                        if metrics["oldest_created_at"] is not None
                        else None
                    )
                    promote = digest_pending_count > 0 and (
                        digest_pending_count >= int(policy["digest_min_messages"])
                        or (
                            digest_oldest_created_at is not None
                            and digest_oldest_created_at
                            <= now - float(policy["digest_after_seconds"])
                        )
                    )
                    if promote and item is None:
                        item = {
                            "conversation_id": room_id,
                            count_key: digest_pending_count,
                            "oldest_sequence": int(metrics["oldest_sequence"]),
                            "newest_sequence": int(metrics["newest_sequence"]),
                            "priority_counts": {
                                "mention": 0,
                                "important": 0,
                                "normal": digest_pending_count,
                            },
                            "required_reply_count": 0,
                            "policy_eligible_count": digest_pending_count,
                        }
                        if count_key == "pending_count":
                            item["oldest_created_at"] = digest_oldest_created_at
                            item["newest_created_at"] = float(
                                metrics["newest_created_at"]
                            )
                        conversations.append(item)
                        by_room[room_id] = item
                if item is not None:
                    item["wake_policy"] = policy
                    item["policy_promoted"] = promote
                    item["digest_pending_count"] = digest_pending_count
                    item["digest_oldest_created_at"] = digest_oldest_created_at
                    item["dnd"] = {
                        "active": dnd_active,
                        "expires_at": (
                            float(dnd_row["expires_at"])
                            if dnd_row is not None
                            else None
                        ),
                        "timezone": (
                            str(dnd_row["timezone_name"])
                            if dnd_row is not None
                            else self.business_timezone_name
                        ),
                        "threshold_reset_at": threshold_reset_at,
                    }
                    if promote:
                        # Wake a mention-only worker; required replies remain
                        # unchanged so every Agent may still choose silence.
                        item["priority_counts"]["mention"] = max(
                            1,
                            int(item["priority_counts"]["mention"]),
                        )

    def _pending_manifest(
        self,
        participant_id: str,
        *,
        after_sequence: int | None = None,
        conversation_id: str | None = None,
        native_unassigned_only: bool = False,
    ) -> dict[str, Any]:
        now = time.time()
        sequence_clause = ""
        parameters: list[Any] = [participant_id, participant_id]
        if after_sequence is not None:
            sequence_clause = "AND message.sequence > ?"
            parameters.append(max(0, int(after_sequence)))
        room_clause = ""
        if conversation_id is not None:
            room_clause = "AND message.conversation_id = ?"
            parameters.append(conversation_id)
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
                       SUM(CASE WHEN instr(delivery.reasons_json, '"mention"') > 0
                                      OR instr(
                                          delivery.reasons_json,
                                          '"agent_request"'
                                      ) > 0
                                THEN 1 ELSE 0 END) AS required_reply_count,
                       SUM(CASE WHEN delivery.priority = 'important' THEN 1 ELSE 0 END)
                           AS important_count,
                       SUM(CASE WHEN delivery.priority = 'normal' THEN 1 ELSE 0 END)
                           AS normal_count,
                       SUM(CASE
                               WHEN instr(
                                   delivery.reasons_json,
                                   '"echo_suppressed"'
                               ) = 0
                               THEN 1 ELSE 0
                           END) AS policy_eligible_count
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
                  AND (? = 0 OR delivery.native_event_id IS NULL)
                  AND message.sender_participant_id != ?
                  {sequence_clause}
                  {room_clause}
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
                [
                    parameters[0],
                    1 if native_unassigned_only else 0,
                    *parameters[1:],
                ],
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
                "required_reply_count": int(row["required_reply_count"] or 0),
                "policy_eligible_count": int(
                    row["policy_eligible_count"] or 0
                ),
            }
            for row in rows
        ]
        self._apply_room_wake_policies(
            participant_id=participant_id,
            conversations=conversations,
            conversation_id=conversation_id,
            count_key="pending_count",
            now=now,
            native_unassigned_only=native_unassigned_only,
        )
        priority_counts = {
            priority: sum(
                int(item["priority_counts"][priority]) for item in conversations
            )
            for priority in ("mention", "important", "normal")
        }
        pending_count = sum(int(item["pending_count"]) for item in conversations)
        required_reply_count = sum(
            int(item["required_reply_count"]) for item in conversations
        )
        return {
            "pending_count": pending_count,
            "required_reply_count": required_reply_count,
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
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        """Summarize visible room activity independently from unread state."""
        room_clause = ""
        parameters: list[Any] = [
            participant_id,
            participant_id,
            max(0, int(after_sequence)),
        ]
        if conversation_id is not None:
            room_clause = "AND message.conversation_id = ?"
            parameters.append(conversation_id)
        now = time.time()
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT message.conversation_id,
                       COUNT(*) AS activity_count,
                       MIN(message.sequence) AS oldest_sequence,
                       MAX(message.sequence) AS newest_sequence,
                       SUM(CASE WHEN delivery.priority IN ('direct', 'mention')
                                THEN 1 ELSE 0 END) AS mention_count,
                       SUM(CASE WHEN instr(delivery.reasons_json, '"mention"') > 0
                                      OR instr(
                                          delivery.reasons_json,
                                          '"agent_request"'
                                      ) > 0
                                THEN 1 ELSE 0 END) AS required_reply_count,
                       SUM(CASE WHEN delivery.priority = 'important' THEN 1 ELSE 0 END)
                           AS important_count,
                       SUM(CASE WHEN delivery.priority = 'normal' THEN 1 ELSE 0 END)
                           AS normal_count,
                       SUM(CASE
                               WHEN instr(
                                   delivery.reasons_json,
                                   '"echo_suppressed"'
                               ) = 0
                               THEN 1 ELSE 0
                           END) AS policy_eligible_count
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
                  {room_clause}
                GROUP BY message.conversation_id
                ORDER BY oldest_sequence
                """,
                parameters,
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
                "required_reply_count": int(row["required_reply_count"] or 0),
                "policy_eligible_count": int(
                    row["policy_eligible_count"] or 0
                ),
            }
            for row in rows
        ]
        self._apply_room_wake_policies(
            participant_id=participant_id,
            conversations=conversations,
            conversation_id=conversation_id,
            count_key="activity_count",
            now=now,
        )
        priority_counts = {
            priority: sum(
                int(item["priority_counts"][priority]) for item in conversations
            )
            for priority in ("mention", "important", "normal")
        }
        return {
            "activity_count": sum(
                int(item["activity_count"]) for item in conversations
            ),
            "required_reply_count": sum(
                int(item["required_reply_count"]) for item in conversations
            ),
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
            if authorized_session_id is not None:
                live_session = self._require_live_room_session(
                    conn,
                    session_id=opaque_id(
                        authorized_session_id,
                        field="authorized_session_id",
                    ),
                    participant_id=participant,
                    conversation_id=str(row["conversation_id"]),
                    now=now,
                )
                self._require_session_write_authority_locked(
                    conn,
                    session=live_session,
                )
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
            if authorized_session_id is not None:
                live_session = self._require_live_room_session(
                    conn,
                    session_id=opaque_id(
                        authorized_session_id,
                        field="authorized_session_id",
                    ),
                    participant_id=participant,
                    conversation_id=str(row["conversation_id"]),
                    now=now,
                )
                self._require_session_write_authority_locked(
                    conn,
                    session=live_session,
                )
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
            if authorized_session_id is not None:
                live_session = self._require_live_room_session(
                    conn,
                    session_id=opaque_id(
                        authorized_session_id,
                        field="authorized_session_id",
                    ),
                    participant_id=participant,
                    conversation_id=str(row["conversation_id"]),
                    now=now,
                )
                self._require_session_write_authority_locked(
                    conn,
                    session=live_session,
                )
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
                    delivery_stage = CASE
                        WHEN native_replied_at IS NOT NULL THEN 'replied'
                        WHEN native_applied_at IS NOT NULL THEN 'native_applied'
                        ELSE 'legacy_acked'
                    END,
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
    def _require_live_room_session(
        conn: sqlite3.Connection,
        *,
        session_id: str,
        participant_id: str,
        conversation_id: str,
        now: float,
    ) -> sqlite3.Row:
        """Require a live MCP credential bound to exactly one room context."""

        row = BridgeStore._require_live_session(
            conn,
            session_id=session_id,
            participant_id=participant_id,
            now=now,
        )
        registered_room = str(row["registered_conversation_id"])
        if registered_room != conversation_id:
            raise AuthorizationError(
                f"Agent session is bound to conversation {registered_room}; "
                f"use a room-specific connector for {conversation_id}"
            )
        return row

    @staticmethod
    def _require_session_write_authority_locked(
        conn: sqlite3.Connection,
        *,
        session: sqlite3.Row,
    ) -> None:
        """Fence legacy shadow writes after an exact native TUI takes ownership."""

        component = str(session["component"] or "unknown")
        connector_id = str(session["connector_id"] or "")
        if component not in {"chat", "listener"} or not connector_id:
            return
        connector = conn.execute(
            "SELECT native_delivery_mode FROM agent_connectors "
            "WHERE connector_id = ? AND accepted_participant_id = ? "
            "AND revoked_at IS NULL",
            (connector_id, str(session["participant_id"])),
        ).fetchone()
        if (
            connector is not None
            and str(connector["native_delivery_mode"] or "")
            == "native_preferred"
        ):
            raise ConflictError(
                "native TUI owns this Agent identity; shadow chat writes are "
                "disabled until the connector explicitly returns to legacy shadow mode"
            )

    def _authorized_session_room(
        self,
        *,
        participant_id: str,
        authorized_session_id: str | None,
    ) -> str | None:
        """Resolve the room of a public MCP call; keep unauthenticated test helpers."""

        if authorized_session_id is None:
            return None
        session_id = opaque_id(
            authorized_session_id,
            field="authorized_session_id",
        )
        with self._connection() as conn:
            row = self._require_live_session(
                conn,
                session_id=session_id,
                participant_id=participant_id,
                now=time.time(),
            )
            conversation = str(row["registered_conversation_id"])
            self._require_membership(conn, participant_id, conversation)
        return conversation

    @staticmethod
    def _require_live_web_session(
        conn: sqlite3.Connection,
        *,
        session_id: str,
        participant_id: str,
        now: float,
    ) -> sqlite3.Row:
        row = conn.execute(
            """
            SELECT web_session.*, web_user.username, web_user.role,
                   web_user.participant_id, web_user.display_name,
                   web_user.signature, web_user.can_create_rooms,
                   web_user.room_limit
            FROM web_sessions AS web_session
            JOIN web_users AS web_user ON web_user.user_id = web_session.user_id
            WHERE web_session.session_id = ?
              AND web_user.participant_id = ?
              AND web_user.active = 1
              AND web_session.revoked_at IS NULL
              AND web_session.expires_at > ?
            """,
            (session_id, participant_id, float(now)),
        ).fetchone()
        if row is None:
            raise AuthenticationError(
                "a live authenticated web user session is required to chat"
            )
        return row


    @staticmethod
    def _finite_history_timestamp(
        value: float | None,
        *,
        field: str,
    ) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValidationError(f"{field} must be a finite Unix timestamp")
        normalized = float(value)
        if not math.isfinite(normalized) or normalized < 0:
            raise ValidationError(f"{field} must be a finite Unix timestamp")
        return normalized

    @staticmethod
    def _history_search_payload(
        row: sqlite3.Row,
        *,
        terms: Sequence[str],
        authorization: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body_text = str(row["body"])
        folded = body_text.casefold()
        offsets = [folded.find(term.casefold()) for term in terms if term]
        offsets = [offset for offset in offsets if offset >= 0]
        start = max(0, (min(offsets) if offsets else 0) - 70)
        end = min(len(body_text), start + 240)
        snippet = body_text[start:end]
        if start > 0:
            snippet = "…" + snippet
        if end < len(body_text):
            snippet += "…"
        payload = {
            "message_id": str(row["message_id"]),
            "sequence": int(row["sequence"]),
            "sender_participant_id": str(row["sender_participant_id"]),
            "sender_seat": (
                str(row["sender_seat"] or "unknown")
                if "sender_seat" in set(row.keys())
                else "unknown"
            ),
            "sender_display_name": str(row["sender_display_name"]),
            "sender_client_type": str(row["sender_client_type"]),
            "created_at": float(row["created_at"]),
            "snippet": snippet,
            "reply": (
                {
                    "message_id": str(row["reply_to"]),
                    "sequence": int(row["replied_sequence"]),
                    "sender_participant_id": str(
                        row["replied_sender_participant_id"]
                    ),
                    "sender_display_name": str(
                        row["replied_sender_display_name"] or ""
                    ),
                }
                if row["reply_to"] is not None
                else None
            ),
        }
        if authorization is not None:
            payload["authorization"] = authorization
        return payload

    @staticmethod
    def _secret_hash(secret: str) -> str:
        return hashlib.sha256(str(secret).encode("utf-8")).hexdigest()

    @staticmethod
    def _constant_time_eq(left: str, right: str) -> bool:
        # compare_digest only accepts ASCII str; encode so Unicode identities
        # (e.g. claude-code-小鲸鱼娘) compare safely and in constant time.
        return secrets.compare_digest(
            str(left).encode("utf-8"),
            str(right).encode("utf-8"),
        )


    @staticmethod
    def _participant_profile_payload(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            raise NotFoundError("participant row disappeared")
        avatar_changed_at = (
            float(row["avatar_changed_at"])
            if row["avatar_changed_at"] is not None
            else None
        )
        avatar_change_available_at = (
            avatar_changed_at + AGENT_AVATAR_CHANGE_COOLDOWN_SECONDS
            if avatar_changed_at is not None
            else None
        )
        return {
            "participant_id": str(row["participant_id"]),
            "client_type": str(row["client_type"]),
            "display_name": str(row["display_name"]),
            "signature": str(row["signature"]),
            "avatar_key": str(row["avatar_key"] or "auto"),
            "avatar_changed_at": avatar_changed_at,
            "avatar_change_available_at": avatar_change_available_at,
            "avatar_change_remaining_seconds": max(
                0.0,
                (avatar_change_available_at or 0.0) - time.time(),
            ),
            "avatar_change_cooldown_seconds": (
                AGENT_AVATAR_CHANGE_COOLDOWN_SECONDS
            ),
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
            "reviewed_by_web_user_id": (
                str(row["reviewed_by_web_user_id"])
                if "reviewed_by_web_user_id" in set(row.keys())
                and row["reviewed_by_web_user_id"] is not None
                else None
            ),
            "next_request_at": requested_at + NICKNAME_REQUEST_COOLDOWN_SECONDS,
        }

    @staticmethod
    def _message_payload(
        row: sqlite3.Row | None,
        *,
        authorization: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if row is None:
            raise NotFoundError("message row disappeared")
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
            "sender_seat": (
                str(row["sender_seat"] or "unknown")
                if "sender_seat" in set(row.keys())
                else "unknown"
            ),
            "notification_mode": (
                str(row["notification_mode"] or "ordinary")
                if "notification_mode" in set(row.keys())
                else (
                    "mention"
                    if row["reply_to"]
                    or bool(row["wake_all_agents"])
                    or json.loads(str(row["mentions_json"] or "[]"))
                    else "ordinary"
                )
            ),
            "audience_kind": str(row["audience_kind"]),
            "message_kind": str(row["message_kind"] or "message"),
            "audience_value": str(row["audience_value"]),
            "body": str(row["body"]),
            "refs": json.loads(str(row["refs_json"])),
            "mentions": json.loads(str(row["mentions_json"] or "[]")),
            "wake_all_agents": bool(row["wake_all_agents"]),
            "reply_to": str(row["reply_to"]) if row["reply_to"] else None,
            "status": str(row["status"]),
            "claimed_by": str(row["claimed_by"]) if row["claimed_by"] else None,
            "claim_until": float(row["claim_until"]) if row["claim_until"] else None,
            "created_at": float(row["created_at"]),
        }
        if (
            "forwarded_from_message_id" in keys
            and row["forwarded_from_message_id"] is not None
        ):
            payload["forwarded_from_message_id"] = str(
                row["forwarded_from_message_id"]
            )
        if authorization is not None:
            payload["authorization"] = authorization
        if "delivery_state" in keys:
            reasons = json.loads(str(row["delivery_reasons_json"] or "[]"))
            payload["delivery"] = {
                "state": str(row["delivery_state"]),
                "reasons": reasons,
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
