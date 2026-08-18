from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import time
from collections.abc import Iterator
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
    DEFAULT_OFFLINE_BACKLOG_KEEP_MESSAGES as DEFAULT_OFFLINE_BACKLOG_KEEP_MESSAGES,
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
    MAX_HISTORY_SEARCH_QUERY_LENGTH as MAX_HISTORY_SEARCH_QUERY_LENGTH,
    MAX_HISTORY_SEARCH_TERMS as MAX_HISTORY_SEARCH_TERMS,
    MAX_INVITATION_TTL_SECONDS as MAX_INVITATION_TTL_SECONDS,
    MAX_MENTIONS_PER_MESSAGE as MAX_MENTIONS_PER_MESSAGE,
    MAX_MESSAGE_COOLDOWN_SECONDS as MAX_MESSAGE_COOLDOWN_SECONDS,
    MAX_OFFLINE_BACKLOG_KEEP_MESSAGES as MAX_OFFLINE_BACKLOG_KEEP_MESSAGES,
    MAX_TASK_TARGETS as MAX_TASK_TARGETS,
    MAX_WAIT_MESSAGES_PAGE_SIZE as MAX_WAIT_MESSAGES_PAGE_SIZE,
    MESSAGE_ACTIONS as MESSAGE_ACTIONS,
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
from .message_delivery import ROOM_MESSAGE_SEQUENCE_SCHEMA, MessageDeliveryMixin
from .message_history import MessageHistoryMixin
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
    ValidationError as ValidationError,
    agent_username as agent_username,
    alias as alias,
    body as body,
    client_identity as client_identity,
    compact_json as compact_json,
    conversation_id as validate_conversation_id,  # noqa: F401
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


class BridgeStore(
    AdminAuditMixin,
    HistoryGovernanceMixin,
    MessageRateMixin,
    MessageRoutingMixin,
    MessageComposerMixin,
    MessageDeliveryMixin,
    MessageHistoryMixin,
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
