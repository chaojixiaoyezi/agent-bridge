from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .a2a_store import A2A_GATEWAY_SCHEMA, A2AStoreMixin
from .agent_connectors import INVITATION_SCHEMA, AgentConnectorMixin
from .agent_lifecycle import AGENT_LIFECYCLE_SCHEMA, AgentLifecycleMixin
from .agent_sessions import AgentSessionMixin
from .avatars import (
    AGENT_AVATAR_CHANGE_COOLDOWN_SECONDS as AGENT_AVATAR_CHANGE_COOLDOWN_SECONDS,
    normalize_avatar_key as normalize_avatar_key,
)
from .admin_audit import ADMIN_AUDIT_SCHEMA, AdminAuditMixin
from .chat_authorization import CHAT_AUTHORIZATION_SCHEMA, ChatAuthorizationMixin
from .connector_health import ConnectorHealthMixin
from .store_constants import (
    AGENT_ACTIVE_ROOM_LIMIT as AGENT_ACTIVE_ROOM_LIMIT,
    AUDIENCE_KINDS as AUDIENCE_KINDS,
    CHAT_AUTHORIZATION_FROZEN as CHAT_AUTHORIZATION_FROZEN,
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
    MESSAGE_COOLDOWN_SECONDS as MESSAGE_COOLDOWN_SECONDS,
    MESSAGE_NOTIFICATION_MODES as MESSAGE_NOTIFICATION_MODES,
    MESSAGE_SENDER_SEATS as MESSAGE_SENDER_SEATS,
    MIN_AGENT_INACTIVITY_DAYS,
    NATIVE_CHANNEL_MAX_MESSAGES as NATIVE_CHANNEL_MAX_MESSAGES,
    NATIVE_CHANNEL_MAX_WAIT_SECONDS as NATIVE_CHANNEL_MAX_WAIT_SECONDS,
    NATIVE_SESSION_LEASE_SECONDS as NATIVE_SESSION_LEASE_SECONDS,
    NATIVE_TUI_ADAPTERS as NATIVE_TUI_ADAPTERS,
    NICKNAME_REQUEST_COOLDOWN_SECONDS as NICKNAME_REQUEST_COOLDOWN_SECONDS,
    OWNER_AUTHORIZATION_ID as OWNER_AUTHORIZATION_ID,
    OWNER_CLIENT_TYPE as OWNER_CLIENT_TYPE,
    OWNER_PARTICIPANT_ID as OWNER_PARTICIPANT_ID,
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
    AuthenticationError as AuthenticationError,
    AuthorizationError,
    AvatarRateLimitError as AvatarRateLimitError,
    BridgeError as BridgeError,
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
from .message_assets import MESSAGE_ASSET_SCHEMA, MessageAssetMixin
from .message_delivery import (
    ROOM_MESSAGE_SEQUENCE_SCHEMA as ROOM_MESSAGE_SEQUENCE_SCHEMA,
    MessageDeliveryMixin,
)
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
from .session_authority import SessionAuthorityMixin
from .store_migrations import StoreMigrationMixin
from .store_schema import SCHEMA, _agent_sessions_table_sql as _agent_sessions_table_sql
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
    opaque_id as opaque_id,
    product_username as product_username,
    string_tokens as string_tokens,
    token as token,
)
from .web_auth import (
    DEFAULT_WEB_USER_ROOM_LIMIT as DEFAULT_WEB_USER_ROOM_LIMIT,
    MAX_WEB_USER_ROOM_LIMIT as MAX_WEB_USER_ROOM_LIMIT,
    WEB_AUTH_SCHEMA,
)


class BridgeStore(
    StoreMigrationMixin,
    AdminAuditMixin,
    HistoryGovernanceMixin,
    MessageAssetMixin,
    MessageRateMixin,
    MessageRoutingMixin,
    MessageComposerMixin,
    MessageDeliveryMixin,
    MessageHistoryMixin,
    SessionAuthorityMixin,
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
        configured_attachment_root = os.environ.get(
            "AGENT_BRIDGE_ATTACHMENT_ROOT",
            "",
        ).strip()
        self.attachment_root = (
            Path(configured_attachment_root).expanduser().resolve()
            if configured_attachment_root
            else (self.database.parent / "attachments").resolve()
        )
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
            conn.executescript(MESSAGE_ASSET_SCHEMA)
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
            conn.execute("PRAGMA user_version = 42")
            conn.execute("PRAGMA optimize")
        try:
            os.chmod(self.database, 0o600)
        except OSError:
            pass
        self._initialize_message_asset_storage()


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
