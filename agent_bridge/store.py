from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import time
import uuid
from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .avatars import (
    AGENT_AVATAR_CHANGE_COOLDOWN_SECONDS,
    normalize_avatar_key,
)
from .validation import (
    MAX_AGENT_USERNAME_CHARS,
    MAX_CLIENT_IDENTITY_CHARS,
    ValidationError,
    agent_username,
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
from .web_auth import (
    DEFAULT_WEB_USER_ROOM_LIMIT,
    MAX_WEB_USER_ROOM_LIMIT,
    WEB_AUTH_SCHEMA,
)


AUDIENCE_KINDS = {"participant", "room", "role", "broadcast"}
PRESENCE_STATES = {"online", "offline"}
MESSAGE_ACTIONS = {"claim", "ack", "release"}
DELIVERY_STATES = {"pending", "delivered", "acked", "cancelled"}
MESSAGE_COOLDOWN_SECONDS = 15.0
WEB_USER_MESSAGE_COOLDOWN_SECONDS = 60.0
MAX_MESSAGE_COOLDOWN_SECONDS = 24 * 60 * 60
RATE_LIMIT_ACTOR_KINDS = {"agent", "web_user"}
AGENT_ACTIVE_ROOM_LIMIT = 2
ROOM_ABANDON_AFTER_SECONDS = 90 * 24 * 60 * 60
DEFAULT_SESSION_TTL_SECONDS = 2 * 60 * 60
CONNECTOR_SESSION_IDLE_RETIRE_SECONDS = 15 * 60
CONNECTOR_SESSION_MIN_RETAIN = 6
NICKNAME_REQUEST_COOLDOWN_SECONDS = 24 * 60 * 60
MAX_MENTIONS_PER_MESSAGE = 64
MAX_WAIT_MESSAGES_PAGE_SIZE = 20
DEFAULT_OFFLINE_BACKLOG_KEEP_MESSAGES = 20
MAX_OFFLINE_BACKLOG_KEEP_MESSAGES = 100
MAX_HISTORY_SEARCH_TERMS = 8
MAX_HISTORY_SEARCH_QUERY_LENGTH = 256
MAX_TASK_TARGETS = 64
REQUIRED_REPLY_DELAY_WARNING_SECONDS = 5 * 60
MONITORING_SAMPLE_INTERVAL_SECONDS = 60
MONITORING_RETENTION_SECONDS = 30 * 24 * 60 * 60
MONITORING_REPLY_LATENCY_WARNING_SECONDS = 10 * 60
MONITORING_TASK_NEEDS_INPUT_WARNING_SECONDS = 30 * 60
MONITORING_TASK_FAILURE_RATE_WARNING = 0.5
MONITORING_MIN_RATE_SAMPLE_COUNT = 3
TASK_CLAIM_LEASE_SECONDS = 10 * 60.0
TASK_INPUT_REDELIVERY_SECONDS = 30.0
TASK_STATUSES = {
    "queued",
    "claimed",
    "running",
    "needs_input",
    "completed",
    "failed",
    "cancelled",
}
DEFAULT_INVITATION_TTL_SECONDS = 30 * 60
MAX_INVITATION_TTL_SECONDS = 24 * 60 * 60
CONNECTOR_ONLINE_WINDOW_SECONDS = 75.0
ENROLLMENT_PREVIOUS_GRACE_SECONDS = 24 * 60 * 60
DEFAULT_AGENT_INACTIVITY_DAYS = 10
DEFAULT_UNACTIVATED_AGENT_INACTIVITY_DAYS = 3
MIN_AGENT_INACTIVITY_DAYS = 1
MAX_AGENT_INACTIVITY_DAYS = 3650
INVITATION_MODES = {"basic", "resident"}
INVITATION_ADAPTERS = {"codex", "claude-code", "manual"}
NATIVE_TUI_ADAPTERS = {
    "deepseek-harness",
    "opencode",
    "hermes",
    "pi",
    "qwen-code",
}
TUI_STATES = {
    "unbound",
    "awaiting_confirmation",
    "online",
    "busy",
    "waiting_approval",
    "offline",
    "error",
}
INVITATION_STATUSES = {"active", "exhausted", "revoked", "expired"}
CONNECTOR_SETUP_STATUSES = {
    "awaiting_setup",
    "configured",
    "manual",
    "failed",
    "revoked",
}
CONNECTOR_COMPONENTS = {"listener", "chat", "task", "mcp"}
SESSION_COMPONENTS = CONNECTOR_COMPONENTS | {"a2a", "unknown"}
MESSAGE_SENDER_SEATS = {"main", "shadow", "executor", "web", "a2a", "unknown"}
ROOM_WAKE_MODES = {"mention", "digest", "all"}
MESSAGE_NOTIFICATION_MODES = {"ordinary", "mention"}
ROOM_MESSAGE_MARKER_KINDS = {"pin", "decision"}
DEFAULT_ROOM_WAKE_MODE = "digest"
DEFAULT_ROOM_DIGEST_MIN_MESSAGES = 10
DEFAULT_ROOM_DIGEST_AFTER_SECONDS = 2 * 60 * 60
CHAT_AUTHORIZATION_FROZEN = True
OWNER_PARTICIPANT_ID = "participant_web_owner"
OWNER_AUTHORIZATION_ID = "owner_web_ui"
OWNER_CLIENT_TYPE = "web-user"
OWNER_SESSION_ALIAS = "本机用户"


_REVIEW_TERMS = (
    "确认",
    "审核",
    "审查",
    "复核",
    "验收",
    "批准",
    "审批",
    "过目",
)
_REVIEW_TERM_PATTERN = "|".join(re.escape(term) for term in _REVIEW_TERMS)
_DIRECT_REVIEW_REQUEST_PATTERNS = (
    re.compile(
        rf"(?:请|麻烦|烦请|劳烦|能否|可否)"
        rf"[^。！？\n]{{0,64}}(?:{_REVIEW_TERM_PATTERN})",
        flags=re.IGNORECASE,
    ),
    re.compile(
        rf"(?:{_REVIEW_TERM_PATTERN})[^。！？\n]{{0,12}}"
        r"(?:一下|下吧|一下吧|好吗|可以吗|行吗|\?|？)",
        flags=re.IGNORECASE,
    ),
    re.compile(
        rf"需要(?:你|您)[^。！？\n]{{0,32}}(?:{_REVIEW_TERM_PATTERN})",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"(?:please|could\s+you|can\s+you|would\s+you|need\s+you\s+to)"
        r"[^.!?\n]{0,80}"
        r"(?:review|confirm|approve|verify|sign[ -]?off)",
        flags=re.IGNORECASE,
    ),
)
_AGENT_ASSIGNMENT_ACTION_PATTERN = (
    r"(?:负责|处理|执行|完成|实现|修改|开发|核对|检查|分析|调研|测试|验证|"
    r"接手|跟进|给出|回复|答复|排查|修复|审计|评审)"
)
_DIRECT_AGENT_REPLY_REQUEST_PATTERNS = (
    re.compile(
        rf"(?:请|麻烦|烦请|劳烦|需要(?:你|您)|由(?:你|您)|交给(?:你|您)|"
        rf"安排(?:你|您))[^。！？\n]{{0,64}}{_AGENT_ASSIGNMENT_ACTION_PATTERN}",
        flags=re.IGNORECASE,
    ),
    re.compile(
        rf"@[^\s@，,。.!！?？:：;；]{{1,128}}\s*[：:,，]\s*"
        rf"(?:请|麻烦|烦请|劳烦|你来|由你|{_AGENT_ASSIGNMENT_ACTION_PATTERN})",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"@[^\s@，,。.!！?？:：;；]{1,128}[^。！？\n]{0,64}"
        r"(?:请问|能否|可否|是否|怎么看|你觉得|你认为|有没有|为什么|为何|怎么)",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"@[A-Za-z0-9._:-]{1,128}\s*[:,]?\s*"
        r"(?:please|could\s+you|can\s+you|would\s+you|own|take|handle|"
        r"implement|fix|review|verify|test|investigate|reply)",
        flags=re.IGNORECASE,
    ),
)

_ACKNOWLEDGEMENT_ONLY_PATTERN = re.compile(
    r"^(?:收到|明白|好的|好|知悉|已知悉|记下了|了解|同意|认可|"
    r"复核口径一致|口径一致|已阅|ok|okay|got\s+it|acknowledged)"
    r"(?:[，,。.!！\s]*(?:谢谢|感谢|后续按此执行|按此执行|会跟进))*[。.!！\s]*$",
    flags=re.IGNORECASE,
)


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


class AvatarRateLimitError(ConflictError):
    def __init__(self, *, retry_after_seconds: float) -> None:
        self.retry_after_seconds = max(0.0, float(retry_after_seconds))
        super().__init__(
            "Agent avatars may be changed at most once every 24 hours; "
            f"retry after {self.retry_after_seconds:.3f} seconds"
        )


class AuthenticationError(BridgeError):
    pass


class AuthorizationError(BridgeError):
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


ROOM_KNOWLEDGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS room_message_markers (
    conversation_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    marker_kind TEXT NOT NULL CHECK (marker_kind IN ('pin', 'decision')),
    note TEXT NOT NULL DEFAULT '',
    created_by_web_user_id TEXT NOT NULL,
    updated_by_web_user_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (conversation_id, message_id, marker_kind),
    FOREIGN KEY (conversation_id) REFERENCES rooms(conversation_id),
    FOREIGN KEY (message_id) REFERENCES messages(message_id),
    FOREIGN KEY (created_by_web_user_id) REFERENCES web_users(user_id),
    FOREIGN KEY (updated_by_web_user_id) REFERENCES web_users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_room_message_markers_room_kind_updated
    ON room_message_markers(
        conversation_id, marker_kind, updated_at DESC, message_id
    );
"""


ROOM_GOVERNANCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS room_web_owners (
    conversation_id TEXT PRIMARY KEY,
    web_user_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES rooms(conversation_id),
    FOREIGN KEY (web_user_id) REFERENCES web_users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_room_web_owners_user
    ON room_web_owners(web_user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS room_web_members (
    conversation_id TEXT NOT NULL,
    web_user_id TEXT NOT NULL,
    access_role TEXT NOT NULL DEFAULT 'member'
        CHECK (access_role IN ('member', 'moderator')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    invited_by_web_user_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (conversation_id, web_user_id),
    FOREIGN KEY (conversation_id) REFERENCES rooms(conversation_id),
    FOREIGN KEY (web_user_id) REFERENCES web_users(user_id),
    FOREIGN KEY (invited_by_web_user_id) REFERENCES web_users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_room_web_members_user_active
    ON room_web_members(web_user_id, active, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_room_web_members_room_active
    ON room_web_members(conversation_id, active, access_role, updated_at DESC);
"""


ROOM_WAKE_POLICY_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS room_wake_policies (
    conversation_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL DEFAULT 'mention'
        CHECK (mode IN ('mention', 'digest', 'all')),
    digest_min_messages INTEGER NOT NULL DEFAULT {DEFAULT_ROOM_DIGEST_MIN_MESSAGES}
        CHECK (digest_min_messages BETWEEN 1 AND 500),
    digest_after_seconds REAL NOT NULL DEFAULT {DEFAULT_ROOM_DIGEST_AFTER_SECONDS}
        CHECK (digest_after_seconds BETWEEN 30 AND 86400),
    updated_by_web_user_id TEXT,
    updated_at REAL NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES rooms(conversation_id),
    FOREIGN KEY (updated_by_web_user_id) REFERENCES web_users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_room_wake_policies_updated
    ON room_wake_policies(updated_at DESC);

CREATE TABLE IF NOT EXISTS agent_room_dnd (
    participant_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    enabled_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    timezone_name TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (participant_id, conversation_id),
    FOREIGN KEY (participant_id) REFERENCES participants(participant_id),
    FOREIGN KEY (conversation_id) REFERENCES rooms(conversation_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_room_dnd_expiry
    ON agent_room_dnd(expires_at, participant_id, conversation_id);
"""


ROOM_TASK_SCHEMA = """
CREATE TABLE IF NOT EXISTS room_task_policies (
    conversation_id TEXT PRIMARY KEY,
    allow_global_admin INTEGER NOT NULL DEFAULT 0
        CHECK (allow_global_admin IN (0, 1)),
    updated_by_web_user_id TEXT,
    updated_at REAL NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES rooms(conversation_id),
    FOREIGN KEY (updated_by_web_user_id) REFERENCES web_users(user_id)
);

CREATE TABLE IF NOT EXISTS room_task_grants (
    conversation_id TEXT NOT NULL,
    web_user_id TEXT NOT NULL,
    can_assign_tasks INTEGER NOT NULL DEFAULT 0
        CHECK (can_assign_tasks IN (0, 1)),
    can_cancel_tasks INTEGER NOT NULL DEFAULT 0
        CHECK (can_cancel_tasks IN (0, 1)),
    granted_by_web_user_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (conversation_id, web_user_id),
    FOREIGN KEY (conversation_id) REFERENCES rooms(conversation_id),
    FOREIGN KEY (web_user_id) REFERENCES web_users(user_id),
    FOREIGN KEY (granted_by_web_user_id) REFERENCES web_users(user_id)
);

CREATE TABLE IF NOT EXISTS room_tasks (
    task_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    source_message_id TEXT UNIQUE,
    parent_task_id TEXT,
    issuer_web_user_id TEXT,
    issuer_participant_id TEXT NOT NULL,
    target_kind TEXT NOT NULL
        CHECK (target_kind IN ('participants', 'room_agents')),
    target_participant_ids_json TEXT NOT NULL DEFAULT '[]',
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN (
            'queued', 'claimed', 'running', 'needs_input',
            'completed', 'failed', 'cancelled'
        )),
    claimed_by_participant_id TEXT,
    claimed_at REAL,
    lease_expires_at REAL,
    started_at REAL,
    completed_at REAL,
    result_summary TEXT,
    execution_cwd TEXT,
    execution_thread_id TEXT,
    source_sequence INTEGER,
    context_start_sequence INTEGER,
    context_end_sequence INTEGER,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES rooms(conversation_id),
    FOREIGN KEY (source_message_id) REFERENCES messages(message_id),
    FOREIGN KEY (parent_task_id) REFERENCES room_tasks(task_id),
    FOREIGN KEY (issuer_web_user_id) REFERENCES web_users(user_id),
    FOREIGN KEY (issuer_participant_id) REFERENCES participants(participant_id),
    FOREIGN KEY (claimed_by_participant_id) REFERENCES participants(participant_id)
);

CREATE INDEX IF NOT EXISTS idx_room_tasks_room_created
    ON room_tasks(conversation_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_room_tasks_claim
    ON room_tasks(status, conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_room_task_grants_user
    ON room_task_grants(web_user_id, conversation_id);

CREATE TABLE IF NOT EXISTS room_task_inputs (
    input_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    source_message_id TEXT NOT NULL,
    source_sequence INTEGER NOT NULL,
    issuer_web_user_id TEXT NOT NULL,
    target_participant_id TEXT NOT NULL,
    body TEXT NOT NULL,
    first_delivered_at REAL,
    last_delivered_at REAL,
    delivery_count INTEGER NOT NULL DEFAULT 0
        CHECK (delivery_count >= 0),
    applied_at REAL,
    created_at REAL NOT NULL,
    UNIQUE (task_id, source_message_id),
    FOREIGN KEY (task_id) REFERENCES room_tasks(task_id),
    FOREIGN KEY (source_message_id) REFERENCES messages(message_id),
    FOREIGN KEY (issuer_web_user_id) REFERENCES web_users(user_id),
    FOREIGN KEY (target_participant_id) REFERENCES participants(participant_id)
);

CREATE INDEX IF NOT EXISTS idx_room_task_inputs_delivery
    ON room_task_inputs(task_id, applied_at, last_delivered_at, source_sequence);
CREATE INDEX IF NOT EXISTS idx_room_task_inputs_source
    ON room_task_inputs(source_message_id, task_id);
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
    reviewed_by_web_user_id TEXT,
    FOREIGN KEY (participant_id) REFERENCES participants(participant_id),
    FOREIGN KEY (requested_session_id) REFERENCES agent_sessions(session_id),
    FOREIGN KEY (reviewed_by_web_user_id) REFERENCES web_users(user_id)
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
WHEN NEW.display_name != OLD.display_name
AND NOT EXISTS (
    SELECT 1 FROM web_users
    WHERE participant_id = OLD.participant_id AND active = 1
)
AND NOT EXISTS (
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


OPERATIONAL_MONITORING_SCHEMA = """
CREATE TABLE IF NOT EXISTS operational_metric_samples (
    sample_minute INTEGER PRIMARY KEY,
    captured_at REAL NOT NULL,
    connector_count INTEGER NOT NULL CHECK (connector_count >= 0),
    connector_online_count INTEGER NOT NULL CHECK (connector_online_count >= 0),
    connector_offline_count INTEGER NOT NULL CHECK (connector_offline_count >= 0),
    connector_failed_count INTEGER NOT NULL CHECK (connector_failed_count >= 0),
    connector_attention_count INTEGER NOT NULL CHECK (connector_attention_count >= 0),
    pending_delivery_count INTEGER NOT NULL CHECK (pending_delivery_count >= 0),
    required_pending_count INTEGER NOT NULL CHECK (required_pending_count >= 0),
    delayed_required_count INTEGER NOT NULL CHECK (delayed_required_count >= 0),
    task_backlog_count INTEGER NOT NULL CHECK (task_backlog_count >= 0),
    task_queued_count INTEGER NOT NULL CHECK (task_queued_count >= 0),
    task_running_count INTEGER NOT NULL CHECK (task_running_count >= 0),
    task_needs_input_count INTEGER NOT NULL CHECK (task_needs_input_count >= 0),
    task_needs_input_delayed_count INTEGER NOT NULL
        CHECK (task_needs_input_delayed_count >= 0),
    task_expired_lease_count INTEGER NOT NULL
        CHECK (task_expired_lease_count >= 0),
    task_terminal_count_1h INTEGER NOT NULL CHECK (task_terminal_count_1h >= 0),
    task_failed_count_1h INTEGER NOT NULL CHECK (task_failed_count_1h >= 0),
    task_failure_rate_1h REAL NOT NULL CHECK (
        task_failure_rate_1h >= 0 AND task_failure_rate_1h <= 1
    ),
    reply_sample_count_1h INTEGER NOT NULL CHECK (reply_sample_count_1h >= 0),
    reply_latency_average_seconds REAL,
    reply_latency_p95_seconds REAL
);

CREATE INDEX IF NOT EXISTS idx_operational_metric_samples_captured
    ON operational_metric_samples(captured_at DESC);

CREATE TABLE IF NOT EXISTS operational_alerts (
    alert_id TEXT PRIMARY KEY,
    alert_key TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('warning', 'critical')),
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'resolved')),
    title TEXT NOT NULL,
    detail TEXT NOT NULL,
    current_value REAL NOT NULL,
    threshold_value REAL NOT NULL,
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    resolved_at REAL,
    occurrence_count INTEGER NOT NULL DEFAULT 1
        CHECK (occurrence_count >= 1),
    last_sample_minute INTEGER NOT NULL,
    acknowledged_at REAL,
    acknowledged_by_web_user_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (acknowledged_by_web_user_id) REFERENCES web_users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_operational_alerts_status_severity_updated
    ON operational_alerts(status, severity, updated_at DESC);

CREATE TABLE IF NOT EXISTS operational_monitoring_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    revision INTEGER NOT NULL DEFAULT 0,
    last_sample_at REAL,
    updated_at REAL NOT NULL
);

INSERT OR IGNORE INTO operational_monitoring_state
    (singleton, revision, last_sample_at, updated_at)
VALUES (1, 0, NULL, CAST(strftime('%s', 'now') AS REAL));

CREATE INDEX IF NOT EXISTS idx_messages_reply_target_sender_created
    ON messages(reply_to, sender_participant_id, created_at)
    WHERE reply_to IS NOT NULL;
"""


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


INVITATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_invitations (
    invitation_id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    conversation_id TEXT NOT NULL,
    product TEXT NOT NULL,
    requested_mode TEXT NOT NULL
        CHECK (requested_mode IN ('basic', 'resident')),
    adapter_kind TEXT NOT NULL
        CHECK (adapter_kind IN ('codex', 'claude-code', 'manual')),
    tui_adapter_kind TEXT,
    reuse_policy TEXT NOT NULL DEFAULT 'single'
        CHECK (reuse_policy IN ('single', 'reusable')),
    max_uses INTEGER
        CHECK (max_uses IS NULL OR max_uses >= 1),
    use_count INTEGER NOT NULL DEFAULT 0 CHECK (use_count >= 0),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'exhausted', 'revoked', 'expired')),
    created_by_web_user_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    first_accepted_at REAL,
    last_accepted_at REAL,
    revoked_at REAL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES rooms(conversation_id),
    FOREIGN KEY (created_by_web_user_id) REFERENCES web_users(user_id)
);

CREATE TABLE IF NOT EXISTS agent_connectors (
    connector_id TEXT PRIMARY KEY,
    invitation_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    accepted_participant_id TEXT NOT NULL,
    initial_session_id TEXT NOT NULL,
    enrollment_token_hash TEXT UNIQUE,
    previous_enrollment_token_hash TEXT,
    previous_enrollment_valid_until REAL,
    enrollment_last_used_at REAL,
    enrollment_rotated_at REAL,
    enrollment_rotation_count INTEGER NOT NULL DEFAULT 0
        CHECK (enrollment_rotation_count >= 0),
    enrollment_credential_version INTEGER NOT NULL DEFAULT 1
        CHECK (enrollment_credential_version >= 1),
    enrollment_rotation_required_at REAL,
    enrollment_rotation_requested_by_web_user_id TEXT,
    setup_status TEXT NOT NULL DEFAULT 'awaiting_setup'
        CHECK (setup_status IN (
            'awaiting_setup', 'configured', 'manual', 'failed', 'revoked'
        )),
    setup_detail_json TEXT NOT NULL DEFAULT '{}',
    setup_updated_at REAL,
    connector_last_seen_at REAL,
    binding_version INTEGER NOT NULL DEFAULT 1
        CHECK (binding_version IN (1, 2)),
    requested_username TEXT,
    bound_client_type TEXT,
    bound_roles_json TEXT,
    bound_capabilities_json TEXT,
    tui_endpoint_id TEXT,
    tui_native_session_id TEXT,
    tui_state TEXT NOT NULL DEFAULT 'unbound',
    tui_capabilities_json TEXT NOT NULL DEFAULT '[]',
    tui_last_seen_at REAL,
    tui_active_task_id TEXT,
    tui_detail_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    revoked_at REAL,
    revoked_by_web_user_id TEXT,
    updated_at REAL NOT NULL,
    FOREIGN KEY (invitation_id) REFERENCES agent_invitations(invitation_id),
    FOREIGN KEY (conversation_id) REFERENCES rooms(conversation_id),
    FOREIGN KEY (accepted_participant_id) REFERENCES participants(participant_id),
    FOREIGN KEY (initial_session_id) REFERENCES agent_sessions(session_id),
    FOREIGN KEY (enrollment_rotation_requested_by_web_user_id)
        REFERENCES web_users(user_id),
    FOREIGN KEY (revoked_by_web_user_id) REFERENCES web_users(user_id),
    UNIQUE (invitation_id, accepted_participant_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_invitations_room_created
    ON agent_invitations(conversation_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_invitations_status_expires
    ON agent_invitations(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_agent_connectors_invitation_created
    ON agent_connectors(invitation_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_connectors_room_participant
    ON agent_connectors(conversation_id, accepted_participant_id, revoked_at);
CREATE INDEX IF NOT EXISTS idx_agent_connectors_participant
    ON agent_connectors(accepted_participant_id, revoked_at, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_connectors_setup_seen
    ON agent_connectors(setup_status, connector_last_seen_at);
CREATE TABLE IF NOT EXISTS connector_component_readiness (
    connector_id TEXT NOT NULL,
    component TEXT NOT NULL
        CHECK (component IN ('listener', 'chat', 'task', 'mcp')),
    protocol_version INTEGER NOT NULL DEFAULT 2
        CHECK (protocol_version >= 2),
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    PRIMARY KEY (connector_id, component),
    FOREIGN KEY (connector_id) REFERENCES agent_connectors(connector_id)
);

CREATE INDEX IF NOT EXISTS idx_connector_component_readiness_seen
    ON connector_component_readiness(last_seen_at DESC);
"""


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


AUTHORIZATION_SCHEMA = f"""
CREATE INDEX IF NOT EXISTS idx_messages_authorized_session
    ON messages(authorized_session_id, sequence);
CREATE INDEX IF NOT EXISTS idx_messages_forwarded_from
    ON messages(forwarded_from_message_id, sequence);

DROP TRIGGER IF EXISTS trg_messages_require_live_mcp_session;
DROP TRIGGER IF EXISTS trg_messages_require_authorized_sender;
DROP TRIGGER IF EXISTS trg_messages_sender_cooldown;

CREATE TRIGGER trg_messages_sender_cooldown
BEFORE INSERT ON messages
WHEN NOT EXISTS (
    SELECT 1
    FROM web_sessions AS web_session
    JOIN web_users AS web_user ON web_user.user_id = web_session.user_id
    WHERE web_session.session_id = NEW.authorized_session_id
      AND web_user.participant_id = NEW.sender_participant_id
      AND web_user.role = 'admin'
      AND web_user.active = 1
      AND web_session.revoked_at IS NULL
      AND web_session.expires_at > CAST(strftime('%s', 'now') AS REAL)
)
AND EXISTS (
    SELECT 1 FROM messages AS previous
    WHERE previous.conversation_id = NEW.conversation_id
      AND previous.sender_participant_id = NEW.sender_participant_id
      AND previous.created_at > NEW.created_at - CASE
          WHEN NEW.authorized_session_id = '{OWNER_AUTHORIZATION_ID}'
               AND NEW.sender_participant_id = '{OWNER_PARTICIPANT_ID}'
          THEN {MESSAGE_COOLDOWN_SECONDS}
          WHEN EXISTS (
              SELECT 1
              FROM web_sessions AS web_session
              JOIN web_users AS web_user
                ON web_user.user_id = web_session.user_id
              WHERE web_session.session_id = NEW.authorized_session_id
                AND web_user.participant_id = NEW.sender_participant_id
                AND web_user.role = 'user'
                AND web_user.active = 1
                AND web_session.revoked_at IS NULL
                AND web_session.expires_at > CAST(strftime('%s', 'now') AS REAL)
          ) THEN MIN(
              COALESCE(
                  (SELECT cooldown_seconds FROM message_rate_defaults
                   WHERE actor_kind = 'web_user'),
                  {WEB_USER_MESSAGE_COOLDOWN_SECONDS}
              ),
              COALESCE(
                  (SELECT cooldown_seconds FROM message_rate_overrides
                   WHERE participant_id = NEW.sender_participant_id),
                  COALESCE(
                      (SELECT cooldown_seconds FROM message_rate_defaults
                       WHERE actor_kind = 'web_user'),
                      {WEB_USER_MESSAGE_COOLDOWN_SECONDS}
                  )
              )
          )
          ELSE MIN(
              COALESCE(
                  (SELECT cooldown_seconds FROM message_rate_defaults
                   WHERE actor_kind = 'agent'),
                  {MESSAGE_COOLDOWN_SECONDS}
              ),
              COALESCE(
                  (SELECT cooldown_seconds FROM message_rate_overrides
                   WHERE participant_id = NEW.sender_participant_id),
                  COALESCE(
                      (SELECT cooldown_seconds FROM message_rate_defaults
                       WHERE actor_kind = 'agent'),
                      {MESSAGE_COOLDOWN_SECONDS}
                  )
              )
          )
      END
)
BEGIN
    SELECT RAISE(ABORT, 'MESSAGE_RATE_LIMITED');
END;

CREATE TRIGGER trg_messages_require_authorized_sender
BEFORE INSERT ON messages
WHEN NOT (
    (
        NEW.authorized_session_id = '{OWNER_AUTHORIZATION_ID}'
        AND NEW.sender_participant_id = '{OWNER_PARTICIPANT_ID}'
    )
    OR EXISTS (
        SELECT 1
        FROM web_sessions AS web_session
        JOIN web_users AS web_user ON web_user.user_id = web_session.user_id
        WHERE web_session.session_id = NEW.authorized_session_id
          AND web_user.participant_id = NEW.sender_participant_id
          AND web_user.active = 1
          AND web_session.revoked_at IS NULL
          AND web_session.expires_at > CAST(strftime('%s', 'now') AS REAL)
    )
    OR EXISTS (
        SELECT 1
        FROM agent_sessions AS session
        WHERE session.session_id = NEW.authorized_session_id
          AND session.participant_id = NEW.sender_participant_id
          AND session.registered_conversation_id = NEW.conversation_id
          AND session.transport = 'mcp'
          AND session.cleared_at IS NULL
          AND session.revoked_at IS NULL
          AND session.expires_at > CAST(strftime('%s', 'now') AS REAL)
    )
)
BEGIN
    SELECT RAISE(ABORT, 'AUTHORIZED_SENDER_REQUIRED');
END;

DROP TRIGGER IF EXISTS trg_messages_forward_requires_source;
CREATE TRIGGER trg_messages_forward_requires_source
BEFORE INSERT ON messages
WHEN (
    NEW.message_kind = 'forward'
    AND (
        NEW.forwarded_from_message_id IS NULL
        OR NOT EXISTS (
            SELECT 1 FROM messages AS source
            WHERE source.message_id = NEW.forwarded_from_message_id
              AND source.conversation_id != NEW.conversation_id
        )
    )
) OR (
    NEW.message_kind != 'forward'
    AND NEW.forwarded_from_message_id IS NOT NULL
)
BEGIN
    SELECT RAISE(ABORT, 'INVALID_CROSS_ROOM_FORWARD');
END;

DROP TRIGGER IF EXISTS trg_messages_route_immutable;
CREATE TRIGGER trg_messages_route_immutable
BEFORE UPDATE OF conversation_id, sender_participant_id,
                 authorized_session_id, message_kind,
                 forwarded_from_message_id ON messages
WHEN NEW.sender_participant_id IS NOT OLD.sender_participant_id
  OR NEW.authorized_session_id IS NOT OLD.authorized_session_id
  OR (
      NEW.message_kind IS NOT OLD.message_kind
      AND NOT (
          OLD.message_kind = 'message'
          AND NEW.message_kind = 'task'
          AND EXISTS (
              SELECT 1 FROM room_tasks AS task
              WHERE task.source_message_id = OLD.message_id
          )
      )
  )
  OR NEW.forwarded_from_message_id IS NOT OLD.forwarded_from_message_id
  OR (
      NEW.conversation_id IS NOT OLD.conversation_id
      AND NOT (
          NOT EXISTS (
              SELECT 1 FROM rooms
              WHERE conversation_id = OLD.conversation_id
          )
          AND EXISTS (
              SELECT 1 FROM rooms
              WHERE conversation_id = NEW.conversation_id
          )
      )
  )
BEGIN
    SELECT RAISE(ABORT, 'MESSAGE_ROUTE_IMMUTABLE');
END;
"""


CHAT_AUTHORIZATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_authorization_grants (
    source_message_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    issuer_web_user_id TEXT NOT NULL,
    issuer_username_snapshot TEXT NOT NULL,
    issuer_role_snapshot TEXT NOT NULL
        CHECK (issuer_role_snapshot = 'admin'),
    issuer_participant_id TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    target_kind TEXT NOT NULL
        CHECK (target_kind IN ('participants', 'room_agents', 'reply_author')),
    target_participant_ids_json TEXT NOT NULL DEFAULT '[]',
    authority_kind TEXT NOT NULL DEFAULT 'admin_chat',
    created_at REAL NOT NULL,
    revoked_at REAL,
    revoked_by_web_user_id TEXT,
    revocation_reason TEXT,
    FOREIGN KEY (source_message_id) REFERENCES messages(message_id),
    FOREIGN KEY (conversation_id) REFERENCES rooms(conversation_id),
    FOREIGN KEY (issuer_web_user_id) REFERENCES web_users(user_id),
    FOREIGN KEY (issuer_participant_id) REFERENCES participants(participant_id),
    FOREIGN KEY (revoked_by_web_user_id) REFERENCES web_users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_chat_authorization_grants_room_created
    ON chat_authorization_grants(conversation_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_chat_authorization_grants_issuer
    ON chat_authorization_grants(issuer_web_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_chat_authorization_grants_active
    ON chat_authorization_grants(revoked_at, conversation_id, created_at DESC);
"""


A2A_GATEWAY_SCHEMA = """
CREATE TABLE IF NOT EXISTS a2a_access_grants (
    grant_id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    conversation_id TEXT NOT NULL,
    participant_id TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    created_by_web_user_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    revoked_at REAL,
    revoked_by_web_user_id TEXT,
    FOREIGN KEY (conversation_id) REFERENCES rooms(conversation_id),
    FOREIGN KEY (participant_id) REFERENCES participants(participant_id),
    FOREIGN KEY (session_id) REFERENCES agent_sessions(session_id),
    FOREIGN KEY (created_by_web_user_id) REFERENCES web_users(user_id),
    FOREIGN KEY (revoked_by_web_user_id) REFERENCES web_users(user_id)
);

CREATE TABLE IF NOT EXISTS a2a_task_links (
    task_id TEXT PRIMARY KEY,
    grant_id TEXT NOT NULL,
    context_id TEXT NOT NULL,
    request_message_id TEXT,
    created_at REAL NOT NULL,
    FOREIGN KEY (task_id) REFERENCES room_tasks(task_id),
    FOREIGN KEY (grant_id) REFERENCES a2a_access_grants(grant_id),
    FOREIGN KEY (request_message_id) REFERENCES messages(message_id)
);

CREATE INDEX IF NOT EXISTS idx_a2a_grants_room_created
    ON a2a_access_grants(conversation_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_a2a_links_grant_context
    ON a2a_task_links(grant_id, context_id, created_at DESC);
"""


class BridgeStore:
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
            conn.execute("PRAGMA user_version = 36")
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
    def _infer_text_mentions_locked(
        conn: sqlite3.Connection,
        *,
        conversation_id: str,
        sender_participant_id: str,
        body_text: str,
    ) -> list[str]:
        """Normalize exact visible @aliases into structured public mentions.

        Explicit participant IDs remain authoritative. This compatibility path
        exists for Agent clients that visibly write ``@name`` but forget the
        structured ``mentions`` argument. Ambiguous aliases are ignored.
        """

        rows = conn.execute(
            """
            SELECT participant.participant_id,
                   participant.client_type,
                   participant.display_name
            FROM memberships AS membership
            JOIN participants AS participant
              ON participant.participant_id = membership.participant_id
            WHERE membership.conversation_id = ?
              AND membership.active = 1
              AND participant.participant_id != ?
            """,
            (conversation_id, sender_participant_id),
        ).fetchall()
        alias_targets: dict[str, set[str]] = {}
        alias_display: dict[str, str] = {}
        for row in rows:
            participant_id = str(row["participant_id"])
            for candidate in (row["client_type"], row["display_name"]):
                visible = str(candidate or "").strip()
                if not visible:
                    continue
                folded = visible.casefold()
                alias_targets.setdefault(folded, set()).add(participant_id)
                alias_display.setdefault(folded, visible)
        inferred: list[str] = []
        for folded, targets in alias_targets.items():
            if len(targets) != 1:
                continue
            visible = alias_display[folded]
            # ``@全员`` is a reserved UI command.  Only the separately
            # authorized wake_all_agents flag can activate it, so plain text
            # must never become a personal mention merely because one Agent
            # happens to use the display name "全员".
            if visible.casefold() == "全员".casefold():
                continue
            # A visible mention may appear at the beginning, in the middle, or
            # at the end of a sentence.  The right boundary still prevents a
            # short nickname from matching the prefix of a longer token.
            pattern = (
                rf"@{re.escape(visible)}"
                r"(?=$|[\s,，。.!！?？:：;；、)）\]】}>》])"
            )
            if re.search(pattern, body_text, flags=re.IGNORECASE):
                inferred.append(next(iter(targets)))
        return sorted(set(inferred))

    @staticmethod
    def _is_direct_review_request(body_text: str) -> bool:
        """Identify only explicit requests for review or confirmation.

        Status prose such as ``等待审批`` or ``需要审核`` is deliberately not
        enough.  The sender must use a request form, which keeps ordinary room
        discussion and progress updates on the existing interest-based path.
        """

        return any(
            pattern.search(body_text)
            for pattern in _DIRECT_REVIEW_REQUEST_PATTERNS
        )

    @classmethod
    def _is_direct_agent_reply_request(cls, body_text: str) -> bool:
        """Distinguish actionable Agent requests from courtesy mentions.

        Agent-to-Agent mentions stay optional by default.  An explicit task,
        question, review, or confirmation request requires one substantive
        response, which prevents delegated work from silently stalling while
        still avoiding acknowledgement loops.
        """

        return cls._is_direct_review_request(body_text) or any(
            pattern.search(body_text)
            for pattern in _DIRECT_AGENT_REPLY_REQUEST_PATTERNS
        )

    @staticmethod
    def _is_acknowledgement_only(body_text: str) -> bool:
        visible = re.sub(
            r"(?:^|\s)@[^s@，,。.!！?？:：;；]{1,128}",
            " ",
            str(body_text or ""),
        ).strip()
        return len(visible) <= 160 and bool(
            _ACKNOWLEDGEMENT_ONLY_PATTERN.fullmatch(visible)
        )

    @staticmethod
    def _assert_agent_identity_consistent_locked(
        conn: sqlite3.Connection,
        *,
        participant_id: str,
        body_text: str,
    ) -> None:
        """Reject only an Agent's explicit denial of its fixed public name."""

        participant = conn.execute(
            """
            SELECT participant.display_name, participant.client_type
            FROM participants AS participant
            LEFT JOIN web_users AS web_user
              ON web_user.participant_id = participant.participant_id
            WHERE participant.participant_id = ?
              AND web_user.user_id IS NULL
            """,
            (participant_id,),
        ).fetchone()
        if participant is None:
            return
        public_name = str(participant["display_name"] or "").strip()
        if not public_name:
            return
        denial = re.compile(
            rf"我(?:并|本来)?不是\s*@?{re.escape(public_name)}"
            rf"(?=$|[\s,，。.!！?？:：;；])",
            flags=re.IGNORECASE,
        )
        if denial.search(body_text):
            raise ConflictError(
                "sender_identity_contradiction: 你的固定公开昵称是 "
                f"{public_name}；@{public_name} 指向你本人。值守影子与执行席位"
                "共享这个公开身份，不能把它说成另一个人"
            )

    @staticmethod
    def _infer_named_review_targets_locked(
        conn: sqlite3.Connection,
        *,
        conversation_id: str,
        sender_participant_id: str,
        body_text: str,
    ) -> list[str]:
        """Resolve unique same-room names in an explicit review request.

        This is intentionally narrower than fuzzy name matching: only complete
        public ``display_name`` or ``client_type`` aliases are considered, and
        an alias shared by multiple active members is ignored.  Existing @
        inference remains authoritative for text that already contains @.
        """

        rows = conn.execute(
            """
            SELECT participant.participant_id,
                   participant.client_type,
                   participant.display_name
            FROM memberships AS membership
            JOIN participants AS participant
              ON participant.participant_id = membership.participant_id
            WHERE membership.conversation_id = ?
              AND membership.active = 1
              AND participant.participant_id != ?
            """,
            (conversation_id, sender_participant_id),
        ).fetchall()
        alias_targets: dict[str, set[str]] = {}
        aliases: dict[str, str] = {}
        for row in rows:
            participant_id = str(row["participant_id"])
            for candidate in (row["display_name"], row["client_type"]):
                visible = str(candidate or "").strip()
                if not visible or visible.casefold() == "全员".casefold():
                    continue
                folded = visible.casefold()
                alias_targets.setdefault(folded, set()).add(participant_id)
                aliases.setdefault(folded, visible)

        inferred: set[str] = set()
        occupied_spans: list[tuple[int, int]] = []
        for folded, targets in sorted(
            alias_targets.items(),
            key=lambda item: len(aliases[item[0]]),
            reverse=True,
        ):
            if len(targets) != 1:
                continue
            visible = aliases[folded]
            left_boundary = (
                r"(?<![A-Za-z0-9._:@-])"
                if visible[0].isascii()
                else r"(?<!@)"
            )
            right_boundary = (
                r"(?![A-Za-z0-9._:@-])" if visible[-1].isascii() else ""
            )
            pattern = rf"{left_boundary}{re.escape(visible)}{right_boundary}"
            for match in re.finditer(pattern, body_text, flags=re.IGNORECASE):
                span = match.span()
                if any(
                    span[0] < occupied_end and occupied_start < span[1]
                    for occupied_start, occupied_end in occupied_spans
                ):
                    continue
                inferred.add(next(iter(targets)))
                occupied_spans.append(span)
                break
        return sorted(inferred)

    @staticmethod
    def _reply_sender_locked(
        conn: sqlite3.Connection,
        *,
        conversation_id: str,
        sender_participant_id: str,
        reply_to: str | None,
    ) -> str | None:
        if reply_to is None:
            return None
        row = conn.execute(
            """
            SELECT original.sender_participant_id
            FROM messages AS original
            JOIN memberships AS membership
              ON membership.conversation_id = original.conversation_id
             AND membership.participant_id = original.sender_participant_id
             AND membership.active = 1
            WHERE original.message_id = ?
              AND original.conversation_id = ?
              AND original.sender_participant_id != ?
            """,
            (reply_to, conversation_id, sender_participant_id),
        ).fetchone()
        return str(row["sender_participant_id"]) if row is not None else None

    @staticmethod
    def _role_review_targets_locked(
        conn: sqlite3.Connection,
        *,
        conversation_id: str,
        sender_participant_id: str,
        role: str,
    ) -> list[str]:
        rows = conn.execute(
            """
            SELECT membership.participant_id, membership.roles_json
            FROM memberships AS membership
            WHERE membership.conversation_id = ?
              AND membership.active = 1
              AND membership.participant_id != ?
            """,
            (conversation_id, sender_participant_id),
        ).fetchall()
        return sorted(
            str(row["participant_id"])
            for row in rows
            if role in set(json.loads(str(row["roles_json"])))
        )

    @staticmethod
    def _rewrite_internal_text_mentions_locked(
        conn: sqlite3.Connection,
        *,
        conversation_id: str,
        sender_participant_id: str,
        body_text: str,
        include_inactive: bool = False,
    ) -> tuple[str, list[str]]:
        """Keep opaque participant IDs out of user-visible message bodies.

        MCP routing uses participant IDs, but visible chat text must use the
        member's current display name.  Some models copied an ID returned by
        ``agent_participants`` into ``@participant_...`` text and omitted the
        structured ``mentions`` argument.  Resolve only exact IDs belonging to
        the same room, so unrelated prose and unknown identifiers are left
        untouched.  The returned IDs let new messages retain real mention
        delivery semantics after the visible text is rewritten.
        """

        membership_filter = "" if include_inactive else "AND membership.active = 1"
        rows = conn.execute(
            """
            SELECT participant.participant_id,
                   participant.client_type,
                   participant.display_name
            FROM memberships AS membership
            JOIN participants AS participant
              ON participant.participant_id = membership.participant_id
            WHERE membership.conversation_id = ?
              AND participant.participant_id != ?
            """
            f" {membership_filter}",
            (conversation_id, sender_participant_id),
        ).fetchall()
        visible_names = {
            str(row["participant_id"]): str(
                row["display_name"] or row["client_type"]
            ).strip()
            for row in rows
        }
        if "@participant_" not in body_text:
            return body_text, []

        # Match known IDs rather than a broad participant-looking token.  The
        # negative lookahead permits Chinese text directly after an ID while
        # preventing a shorter ID from matching inside a longer opaque token.
        mentioned: list[str] = []
        rewritten = body_text
        if visible_names:
            alternatives = "|".join(
                re.escape(participant_id)
                for participant_id in sorted(visible_names, key=len, reverse=True)
            )
            pattern = re.compile(
                rf"@(?P<participant_id>{alternatives})(?![A-Za-z0-9._:-])"
            )

            def replace(match: re.Match[str]) -> str:
                participant_id = match.group("participant_id")
                if participant_id not in mentioned:
                    mentioned.append(participant_id)
                return f"@{visible_names[participant_id]}"

            rewritten = pattern.sub(replace, rewritten)

        # Never leak an unresolved opaque mention into user-visible chat.  It
        # may refer to a removed member or stale model context; keep the prose
        # readable but deliberately do not create a delivery for it.
        unresolved_pattern = re.compile(
            r"@participant_[A-Za-z0-9._:-]+(?![A-Za-z0-9._:-])"
        )
        rewritten = unresolved_pattern.sub("成员（已离开或不可用）", rewritten)
        return rewritten, mentioned

    @classmethod
    def _migrate_internal_participant_mentions_to_display_names(
        cls,
        conn: sqlite3.Connection,
    ) -> None:
        """Rewrite legacy Agent-authored opaque @ IDs without replaying them.

        This intentionally changes only body text.  Existing mention metadata,
        delivery priority, receipts, and notification cursors stay untouched,
        so opening an upgraded database cannot wake Agents for old messages.
        Web-authored messages are excluded to preserve the exact body hash of
        any historical admin authorization snapshot.
        """

        rows = conn.execute(
            """
            SELECT message.message_id,
                   message.conversation_id,
                   message.sender_participant_id,
                   message.body
            FROM messages AS message
            LEFT JOIN web_users AS web_user
              ON web_user.participant_id = message.sender_participant_id
            WHERE instr(message.body, '@participant_') > 0
              AND web_user.user_id IS NULL
              AND message.sender_participant_id != ?
            ORDER BY message.sequence
            """,
            (OWNER_PARTICIPANT_ID,),
        ).fetchall()
        for row in rows:
            rewritten, _mentioned = cls._rewrite_internal_text_mentions_locked(
                conn,
                conversation_id=str(row["conversation_id"]),
                sender_participant_id=str(row["sender_participant_id"]),
                body_text=str(row["body"]),
                include_inactive=True,
            )
            if rewritten != str(row["body"]):
                conn.execute(
                    "UPDATE messages SET body = ? WHERE message_id = ?",
                    (rewritten, str(row["message_id"])),
                )

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
        include_optional_wakes: bool = True,
    ) -> list[dict[str, Any]]:
        conversation = str(message["conversation_id"])
        sender = str(message["sender_participant_id"])
        created_at = float(message["created_at"])
        mention_ids = set(json.loads(str(message["mentions_json"] or "[]")))
        wake_all_agents = bool(message["wake_all_agents"])
        sender_is_web_user = conn.execute(
            "SELECT 1 FROM web_users WHERE participant_id = ?",
            (sender,),
        ).fetchone() is not None
        sender_is_human = sender_is_web_user or sender == OWNER_PARTICIPANT_ID
        acknowledgement_only = (
            not sender_is_human
            and cls._is_acknowledgement_only(str(message["body"]))
        )
        agent_request_requires_reply = (
            not sender_is_human
            and bool(mention_ids)
            and cls._is_direct_agent_reply_request(str(message["body"]))
        )
        reply_target = None
        if message["reply_to"] is not None:
            replied = conn.execute(
                "SELECT sender_participant_id FROM messages WHERE message_id = ?",
                (str(message["reply_to"]),),
            ).fetchone()
            if replied is not None:
                reply_target = str(replied["sender_participant_id"])
        membership_filter = "" if include_inactive_memberships else "AND active = 1"
        memberships = conn.execute(
            "SELECT membership.participant_id, membership.roles_json, "
            "membership.joined_at, web_user.user_id AS web_user_id "
            "FROM memberships AS membership "
            "LEFT JOIN web_users AS web_user "
            "ON web_user.participant_id = membership.participant_id "
            "WHERE membership.conversation_id = ? "
            f"{membership_filter.replace('active', 'membership.active')} "
            "AND membership.joined_at <= ?",
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
        quiet_participants = {
            str(row["participant_id"])
            for row in conn.execute(
                "SELECT participant_id FROM agent_room_dnd "
                "WHERE conversation_id = ? AND enabled_at <= ? AND expires_at > ?",
                (conversation, created_at, created_at),
            ).fetchall()
        }
        audience_kind = str(message["audience_kind"])
        candidates: list[dict[str, Any]] = []
        for membership in memberships:
            participant = str(membership["participant_id"])
            if participant == sender:
                continue
            is_agent = membership["web_user_id"] is None
            roles = set(json.loads(str(membership["roles_json"])))
            primary_recipient = cls._eligible(
                message,
                participant_id=participant,
                roles=roles,
            )
            reasons = ["room_activity"]
            if acknowledgement_only:
                reasons.append("echo_suppressed")
            if primary_recipient:
                reasons.append(f"audience:{audience_kind}")
            if participant in mention_ids:
                # Courtesy Agent mentions remain optional, while explicit
                # assignments/questions get one required response.  Human
                # personal mentions retain their existing required semantics.
                if participant in quiet_participants:
                    reasons.extend(("agent_mention", "quiet_optional"))
                elif sender_is_human:
                    reasons.append("mention")
                elif agent_request_requires_reply:
                    reasons.append("agent_request")
                else:
                    reasons.append("agent_mention")
            if include_optional_wakes and wake_all_agents and is_agent:
                reasons.append("wake_all")
                if participant in quiet_participants:
                    reasons.append("quiet_optional")
            if include_optional_wakes and reply_target == participant and is_agent:
                reasons.append("reply_wake")
                if participant in quiet_participants:
                    reasons.append("quiet_optional")
            if participant in followers:
                reasons.append("follow")
            if participant in mention_ids:
                priority = "direct"
            elif "wake_all" in reasons or "reply_wake" in reasons:
                priority = "direct"
            elif (
                "follow" in reasons
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

    @staticmethod
    def _migrate_agent_mentions_to_optional(conn: sqlite3.Connection) -> None:
        """Decouple historical Agent mentions from mandatory reply semantics."""
        conn.execute(
            """
            UPDATE message_deliveries
            SET reasons_json = replace(
                reasons_json,
                '"mention"',
                '"agent_mention"'
            )
            WHERE instr(reasons_json, '"mention"') > 0
              AND message_id IN (
                  SELECT message.message_id
                  FROM messages AS message
                  LEFT JOIN web_users AS web_user
                    ON web_user.participant_id = message.sender_participant_id
                  WHERE web_user.user_id IS NULL
                    AND message.sender_participant_id != ?
              )
            """,
            (OWNER_PARTICIPANT_ID,),
        )

    @classmethod
    def _create_message_deliveries_locked(
        cls,
        conn: sqlite3.Connection,
        message: sqlite3.Row,
    ) -> None:
        if str(message["message_kind"]) == "task":
            # Structured tasks have their own atomic claim ledger and resident
            # executor.  Keeping them out of the ordinary chat-delivery queue
            # prevents a later @ wake from making the read-only chat worker
            # discuss or acknowledge the task a second time.
            return
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
    def _admin_chat_authorization_targets_locked(
        cls,
        conn: sqlite3.Connection,
        message: sqlite3.Row,
    ) -> tuple[str, list[str]] | None:
        conversation_id = str(message["conversation_id"])
        mentioned = list(json.loads(str(message["mentions_json"] or "[]")))
        if bool(message["wake_all_agents"]):
            targets = cls._room_agent_ids_locked(
                conn,
                conversation_id=conversation_id,
                created_at=float(message["created_at"]),
            )
            return ("room_agents", targets) if targets else None

        agent_targets: list[str] = []
        for participant_id in mentioned:
            target = conn.execute(
                """
                SELECT participant.participant_id
                FROM memberships AS membership
                JOIN participants AS participant
                  ON participant.participant_id = membership.participant_id
                LEFT JOIN web_users AS web_user
                  ON web_user.participant_id = participant.participant_id
                WHERE membership.conversation_id = ?
                  AND membership.participant_id = ?
                  AND membership.active = 1
                  AND web_user.user_id IS NULL
                  AND participant.participant_id != ?
                """,
                (conversation_id, participant_id, OWNER_PARTICIPANT_ID),
            ).fetchone()
            if target is not None:
                agent_targets.append(str(target["participant_id"]))
        if agent_targets:
            return "participants", sorted(set(agent_targets))
        if mentioned:
            # Explicit @ targets that are only Web users must not spill authority
            # over to unrelated Agents in the same public room.
            return None

        if message["reply_to"] is not None:
            reply_author = conn.execute(
                """
                SELECT original.sender_participant_id
                FROM messages AS original
                LEFT JOIN web_users AS web_user
                  ON web_user.participant_id = original.sender_participant_id
                WHERE original.message_id = ?
                  AND original.conversation_id = ?
                  AND web_user.user_id IS NULL
                  AND original.sender_participant_id != ?
                """,
                (
                    str(message["reply_to"]),
                    conversation_id,
                    OWNER_PARTICIPANT_ID,
                ),
            ).fetchone()
            if reply_author is not None:
                return "reply_author", [str(reply_author["sender_participant_id"])]
            return None

        # An authenticated admin message without a narrower addressee applies
        # to Agents already in the room when it was sent. It does not wake them.
        targets = cls._room_agent_ids_locked(
            conn,
            conversation_id=conversation_id,
            created_at=float(message["created_at"]),
        )
        return ("room_agents", targets) if targets else None

    @staticmethod
    def _room_agent_ids_locked(
        conn: sqlite3.Connection,
        *,
        conversation_id: str,
        created_at: float,
    ) -> list[str]:
        rows = conn.execute(
            """
            SELECT membership.participant_id
            FROM memberships AS membership
            LEFT JOIN web_users AS web_user
              ON web_user.participant_id = membership.participant_id
            WHERE membership.conversation_id = ?
              AND membership.joined_at <= ?
              AND membership.active = 1
              AND web_user.user_id IS NULL
              AND membership.participant_id != ?
            ORDER BY membership.participant_id
            """,
            (conversation_id, created_at, OWNER_PARTICIPANT_ID),
        ).fetchall()
        return [str(row["participant_id"]) for row in rows]

    @classmethod
    def _insert_admin_chat_authorization_grant_locked(
        cls,
        conn: sqlite3.Connection,
        *,
        message: sqlite3.Row,
        issuer_web_user_id: str,
        issuer_username: str,
        issuer_role: str,
    ) -> None:
        if CHAT_AUTHORIZATION_FROZEN:
            return
        if issuer_role != "admin" or str(message["message_kind"]) == "forward":
            return
        target = cls._admin_chat_authorization_targets_locked(
            conn,
            message,
        )
        if target is None:
            return
        target_kind, targets = target
        conn.execute(
            """
            INSERT OR IGNORE INTO chat_authorization_grants
                (source_message_id, conversation_id, issuer_web_user_id,
                 issuer_username_snapshot, issuer_role_snapshot,
                 issuer_participant_id, body_sha256, target_kind,
                 target_participant_ids_json, authority_kind, created_at)
            VALUES (?, ?, ?, ?, 'admin', ?, ?, ?, ?, 'admin_chat', ?)
            """,
            (
                str(message["message_id"]),
                str(message["conversation_id"]),
                issuer_web_user_id,
                issuer_username,
                str(message["sender_participant_id"]),
                hashlib.sha256(str(message["body"]).encode("utf-8")).hexdigest(),
                target_kind,
                compact_json(targets),
                float(message["created_at"]),
            ),
        )

    @classmethod
    def _backfill_message_deliveries(
        cls,
        conn: sqlite3.Connection,
        *,
        include_optional_wakes: bool = True,
    ) -> None:
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
                include_optional_wakes=include_optional_wakes,
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
    def _ensure_web_membership_locked(
        conn: sqlite3.Connection,
        *,
        conversation_id: str,
        participant_id: str,
        display_name: str,
        signature: str,
        role: str,
        now: float,
    ) -> None:
        participant = conn.execute(
            "SELECT participant_id FROM participants WHERE participant_id = ?",
            (participant_id,),
        ).fetchone()
        if participant is None:
            raise AuthenticationError("web user participant identity is missing")
        conn.execute(
            "UPDATE participants SET display_name = ?, signature = ?, "
            "profile_updated_at = ?, status = 'online', last_seen = ? "
            "WHERE participant_id = ?",
            (display_name, signature, now, now, participant_id),
        )
        membership_role = "admin" if role == "admin" else "web-user"
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
                conversation_id,
                participant_id,
                compact_json([membership_role]),
                now,
                now,
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
                SET state = 'cancelled', actionable = 0
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

    @staticmethod
    def _normalize_invitation_mode(value: str) -> str:
        mode = str(value or "").strip().lower()
        if mode not in INVITATION_MODES:
            raise ValidationError("invitation mode must be basic or resident")
        return mode

    @staticmethod
    def _normalize_invitation_adapter(value: str) -> str:
        adapter = str(value or "").strip().lower()
        if adapter not in INVITATION_ADAPTERS:
            raise ValidationError(
                "invitation adapter must be codex, claude-code, or manual"
            )
        return adapter

    @staticmethod
    def _normalize_tui_adapter(value: str | None) -> str | None:
        adapter = str(value or "").strip().lower()
        if not adapter:
            return None
        if adapter not in NATIVE_TUI_ADAPTERS:
            raise ValidationError("unsupported native TUI adapter")
        return adapter

    @staticmethod
    def _expire_agent_invitations_locked(
        conn: sqlite3.Connection,
        *,
        now: float,
    ) -> int:
        cursor = conn.execute(
            """
            UPDATE agent_invitations
            SET status = 'expired', updated_at = ?
            WHERE status = 'active' AND expires_at <= ?
            """,
            (now, now),
        )
        return int(cursor.rowcount)

    @staticmethod
    def _connector_detail(value: object) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValidationError("connector detail must be an object")
        forbidden = {"token", "secret", "password", "authorization", "credential"}
        normalized: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key or "").strip()
            if not key or len(key) > 64 or key.casefold() in forbidden:
                raise ValidationError("connector detail contains an invalid field")
            if isinstance(raw_value, bool) or raw_value is None:
                normalized[key] = raw_value
            elif isinstance(raw_value, (int, float)):
                normalized[key] = raw_value
            else:
                text = str(raw_value)
                if len(text) > 512 or any(ord(character) < 32 for character in text):
                    raise ValidationError("connector detail value is invalid")
                normalized[key] = text
        encoded = compact_json(normalized)
        if len(encoded.encode("utf-8")) > 4096:
            raise ValidationError("connector detail is too large")
        return normalized

    @staticmethod
    def _agent_invitation_payload(
        row: sqlite3.Row,
        *,
        now: float,
    ) -> dict[str, Any]:
        keys = set(row.keys())

        def integer(name: str) -> int:
            return int(row[name] or 0) if name in keys else 0

        connector_count = integer("connector_count")
        active_connector_count = integer("active_connector_count")
        online_connector_count = integer("online_connector_count")
        configured_connector_count = integer("configured_connector_count")
        manual_connector_count = integer("manual_connector_count")
        failed_connector_count = integer("failed_connector_count")
        latest_seen = (
            float(row["latest_connector_last_seen_at"])
            if "latest_connector_last_seen_at" in keys
            and row["latest_connector_last_seen_at"] is not None
            else None
        )
        invitation_status = str(row["status"])
        if invitation_status == "revoked":
            resident_status = "revoked"
            setup_status = "revoked"
        elif online_connector_count > 0:
            resident_status = "online"
            setup_status = "configured"
        elif configured_connector_count > 0:
            resident_status = "offline"
            setup_status = "configured"
        elif failed_connector_count > 0:
            resident_status = "failed"
            setup_status = "failed"
        elif active_connector_count > 0 and (
            manual_connector_count == active_connector_count
        ):
            resident_status = "manual"
            setup_status = "manual"
        elif active_connector_count > 0:
            resident_status = "awaiting_setup"
            setup_status = "awaiting_setup"
        else:
            resident_status = (
                invitation_status
                if invitation_status in {"expired", "exhausted"}
                else "awaiting_acceptance"
            )
            setup_status = "awaiting_acceptance"
        max_uses = (
            int(row["max_uses"])
            if row["max_uses"] is not None
            else None
        )
        use_count = int(row["use_count"])
        tui_adapter_kind = (
            str(row["tui_adapter_kind"])
            if "tui_adapter_kind" in keys and row["tui_adapter_kind"] is not None
            else None
        )
        effective_adapter_kind = tui_adapter_kind or str(row["adapter_kind"])
        return {
            "invitation_id": str(row["invitation_id"]),
            "conversation_id": str(row["conversation_id"]),
            "product": str(row["product"]),
            "requested_mode": str(row["requested_mode"]),
            "adapter_kind": str(row["adapter_kind"]),
            "tui_adapter_kind": tui_adapter_kind,
            "effective_adapter_kind": effective_adapter_kind,
            "resident_capable": effective_adapter_kind != "manual",
            "reuse_policy": str(row["reuse_policy"]),
            "reusable": str(row["reuse_policy"]) == "reusable",
            "max_uses": max_uses,
            "use_count": use_count,
            "remaining_uses": (
                max(0, max_uses - use_count) if max_uses is not None else None
            ),
            "status": invitation_status,
            "created_by_web_user_id": str(row["created_by_web_user_id"]),
            "created_by_username": (
                str(row["created_by_username"] or "")
                if "created_by_username" in keys
                else ""
            ),
            "created_at": float(row["created_at"]),
            "expires_at": float(row["expires_at"]),
            "first_accepted_at": (
                float(row["first_accepted_at"])
                if row["first_accepted_at"] is not None
                else None
            ),
            "last_accepted_at": (
                float(row["last_accepted_at"])
                if row["last_accepted_at"] is not None
                else None
            ),
            "accepted_at": (
                float(row["first_accepted_at"])
                if row["first_accepted_at"] is not None
                else None
            ),
            "last_accepted_display_name": (
                str(row["last_accepted_display_name"] or "")
                if "last_accepted_display_name" in keys
                else ""
            ),
            "last_accepted_client_type": (
                str(row["last_accepted_client_type"] or "")
                if "last_accepted_client_type" in keys
                else ""
            ),
            "connector_count": connector_count,
            "active_connector_count": active_connector_count,
            "online_connector_count": online_connector_count,
            "configured_connector_count": configured_connector_count,
            "manual_connector_count": manual_connector_count,
            "failed_connector_count": failed_connector_count,
            "setup_status": setup_status,
            "connector_last_seen_at": latest_seen,
            "resident_status": resident_status,
            "revoked_at": (
                float(row["revoked_at"])
                if row["revoked_at"] is not None
                else None
            ),
            "updated_at": float(row["updated_at"]),
        }

    @staticmethod
    def _agent_invitation_row_locked(
        conn: sqlite3.Connection,
        *,
        invitation_id: str,
        now: float,
    ) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT invitation.*, creator.username AS created_by_username,
                   COUNT(connector.connector_id) AS connector_count,
                   SUM(CASE WHEN connector.connector_id IS NOT NULL
                                 AND connector.revoked_at IS NULL
                            THEN 1 ELSE 0 END) AS active_connector_count,
                   SUM(CASE WHEN connector.revoked_at IS NULL
                                 AND connector.setup_status = 'configured'
                                 AND connector.connector_last_seen_at >= ?
                            THEN 1 ELSE 0 END) AS online_connector_count,
                   SUM(CASE WHEN connector.revoked_at IS NULL
                                 AND connector.setup_status = 'configured'
                            THEN 1 ELSE 0 END) AS configured_connector_count,
                   SUM(CASE WHEN connector.revoked_at IS NULL
                                 AND connector.setup_status = 'manual'
                            THEN 1 ELSE 0 END) AS manual_connector_count,
                   SUM(CASE WHEN connector.revoked_at IS NULL
                                 AND connector.setup_status = 'failed'
                            THEN 1 ELSE 0 END) AS failed_connector_count,
                   MAX(connector.connector_last_seen_at)
                       AS latest_connector_last_seen_at,
                   (
                       SELECT participant.display_name
                       FROM agent_connectors AS recent
                       JOIN participants AS participant
                         ON participant.participant_id = recent.accepted_participant_id
                       WHERE recent.invitation_id = invitation.invitation_id
                       ORDER BY recent.created_at DESC
                       LIMIT 1
                   ) AS last_accepted_display_name,
                   (
                       SELECT participant.client_type
                       FROM agent_connectors AS recent
                       JOIN participants AS participant
                         ON participant.participant_id = recent.accepted_participant_id
                       WHERE recent.invitation_id = invitation.invitation_id
                       ORDER BY recent.created_at DESC
                       LIMIT 1
                   ) AS last_accepted_client_type
            FROM agent_invitations AS invitation
            JOIN web_users AS creator
              ON creator.user_id = invitation.created_by_web_user_id
            LEFT JOIN agent_connectors AS connector
              ON connector.invitation_id = invitation.invitation_id
            WHERE invitation.invitation_id = ?
            GROUP BY invitation.invitation_id
            """,
            (now - CONNECTOR_ONLINE_WINDOW_SECONDS, invitation_id),
        ).fetchone()

    @staticmethod
    def _agent_connector_payload(
        row: sqlite3.Row,
        *,
        now: float,
    ) -> dict[str, Any]:
        last_seen = (
            float(row["connector_last_seen_at"])
            if row["connector_last_seen_at"] is not None
            else None
        )
        setup_status = str(row["setup_status"])
        revoked = (
            row["revoked_at"] is not None
            or str(row["invitation_status"]) == "revoked"
        )
        if revoked:
            resident_status = "revoked"
        elif setup_status == "configured" and last_seen is not None and (
            now - last_seen <= CONNECTOR_ONLINE_WINDOW_SECONDS
        ):
            resident_status = "online"
        elif setup_status == "configured":
            resident_status = "offline"
        else:
            resident_status = setup_status
        keys = set(row.keys())
        tui_adapter_kind = (
            str(row["tui_adapter_kind"])
            if "tui_adapter_kind" in keys and row["tui_adapter_kind"] is not None
            else None
        )
        return {
            "connector_id": str(row["connector_id"]),
            "invitation_id": str(row["invitation_id"]),
            "conversation_id": str(row["conversation_id"]),
            "product": str(row["product"]),
            "adapter_kind": str(row["adapter_kind"]),
            "tui_adapter_kind": tui_adapter_kind,
            "effective_adapter_kind": tui_adapter_kind or str(row["adapter_kind"]),
            "accepted_participant_id": str(row["accepted_participant_id"]),
            "setup_status": setup_status,
            "setup_detail": json.loads(str(row["setup_detail_json"] or "{}")),
            "setup_updated_at": (
                float(row["setup_updated_at"])
                if row["setup_updated_at"] is not None
                else None
            ),
            "connector_last_seen_at": last_seen,
            "resident_status": resident_status,
            "enrollment": {
                "credential_version": int(
                    row["enrollment_credential_version"] or 1
                ),
                "rotation_count": int(row["enrollment_rotation_count"] or 0),
                "last_used_at": (
                    float(row["enrollment_last_used_at"])
                    if row["enrollment_last_used_at"] is not None
                    else None
                ),
                "rotated_at": (
                    float(row["enrollment_rotated_at"])
                    if row["enrollment_rotated_at"] is not None
                    else None
                ),
                "rotation_required": (
                    row["enrollment_rotation_required_at"] is not None
                ),
                "rotation_required_at": (
                    float(row["enrollment_rotation_required_at"])
                    if row["enrollment_rotation_required_at"] is not None
                    else None
                ),
                "previous_valid_until": (
                    float(row["previous_enrollment_valid_until"])
                    if row["previous_enrollment_valid_until"] is not None
                    else None
                ),
            },
            "tui": {
                "endpoint_id": (
                    str(row["tui_endpoint_id"])
                    if row["tui_endpoint_id"] is not None
                    else None
                ),
                "native_session_id": (
                    str(row["tui_native_session_id"])
                    if row["tui_native_session_id"] is not None
                    else None
                ),
                "state": str(row["tui_state"] or "unbound"),
                "capabilities": json.loads(str(row["tui_capabilities_json"] or "[]")),
                "last_seen_at": (
                    float(row["tui_last_seen_at"])
                    if row["tui_last_seen_at"] is not None
                    else None
                ),
                "active_task_id": (
                    str(row["tui_active_task_id"])
                    if row["tui_active_task_id"] is not None
                    else None
                ),
                "detail": json.loads(str(row["tui_detail_json"] or "{}")),
            },
            "revoked_at": (
                float(row["revoked_at"]) if row["revoked_at"] is not None else None
            ),
            "updated_at": float(row["updated_at"]),
        }

    def create_agent_invitation(
        self,
        *,
        conversation_id: str,
        product: str,
        requested_mode: str,
        adapter_kind: str,
        tui_adapter_kind: str | None = None,
        created_by_web_user_id: str,
        reusable: bool = False,
        ttl_seconds: float = DEFAULT_INVITATION_TTL_SECONDS,
    ) -> dict[str, Any]:
        conversation = validate_conversation_id(conversation_id)
        normalized_product = token(product, field="product_name")
        mode = self._normalize_invitation_mode(requested_mode)
        adapter = self._normalize_invitation_adapter(adapter_kind)
        tui_adapter = self._normalize_tui_adapter(tui_adapter_kind)
        creator = opaque_id(created_by_web_user_id, field="created_by_web_user_id")
        if not isinstance(reusable, bool):
            raise ValidationError("reusable must be a boolean")
        reuse_policy = "reusable" if reusable else "single"
        max_uses = None if reusable else 1
        if isinstance(ttl_seconds, bool):
            raise ValidationError("invitation ttl must be a number")
        try:
            ttl = float(ttl_seconds)
        except (TypeError, ValueError) as exc:
            raise ValidationError("invitation ttl must be a number") from exc
        if not math.isfinite(ttl):
            raise ValidationError("invitation ttl must be finite")
        ttl = max(60.0, min(ttl, MAX_INVITATION_TTL_SECONDS))
        now = time.time()
        invitation_id = f"invite_{uuid.uuid4().hex}"
        invitation_token = f"invite_{secrets.token_urlsafe(32)}"
        with self._transaction() as conn:
            room_permissions = self._room_web_permissions_locked(
                conn,
                web_user_id=creator,
                conversation_id=conversation,
            )
            if not room_permissions["can_invite_agents"]:
                raise AuthorizationError("你没有邀请 Agent 加入这个聊天室的权限")
            self._require_active_room(conn, conversation)
            self._expire_agent_invitations_locked(conn, now=now)
            conn.execute(
                """
                INSERT INTO agent_invitations
                    (invitation_id, token_hash, conversation_id, product,
                     requested_mode, adapter_kind, tui_adapter_kind,
                     reuse_policy, max_uses,
                     use_count, status,
                     created_by_web_user_id, created_at, expires_at,
                     updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'active', ?, ?, ?, ?)
                """,
                (
                    invitation_id,
                    self._secret_hash(invitation_token),
                    conversation,
                    normalized_product,
                    mode,
                    adapter,
                    tui_adapter,
                    reuse_policy,
                    max_uses,
                    creator,
                    now,
                    now + ttl,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM agent_invitations WHERE invitation_id = ?",
                (invitation_id,),
            ).fetchone()
        result = self._agent_invitation_payload(row, now=now)
        result["invitation_token"] = invitation_token
        return result

    def list_agent_invitations(
        self,
        *,
        requesting_web_user_id: str,
        conversation_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        requester = opaque_id(
            requesting_web_user_id,
            field="requesting_web_user_id",
        )
        conversation = (
            validate_conversation_id(conversation_id)
            if conversation_id
            else None
        )
        normalized_limit = max(1, min(int(limit), 500))
        now = time.time()
        with self._transaction() as conn:
            requester_row = conn.execute(
                "SELECT role FROM web_users WHERE user_id = ? AND active = 1",
                (requester,),
            ).fetchone()
            if requester_row is None:
                raise AuthenticationError("active Web user is required")
            if conversation is None:
                if str(requester_row["role"]) != "admin":
                    raise AuthorizationError(
                        "聊天室管理员只能查看指定聊天室的邀请"
                    )
            else:
                room_permissions = self._room_web_permissions_locked(
                    conn,
                    web_user_id=requester,
                    conversation_id=conversation,
                )
                if not room_permissions["can_invite_agents"]:
                    raise AuthorizationError("你没有查看本聊天室邀请的权限")
            self._expire_agent_invitations_locked(conn, now=now)
            rows = conn.execute(
                """
                SELECT invitation.*, creator.username AS created_by_username,
                       COUNT(connector.connector_id) AS connector_count,
                       SUM(CASE WHEN connector.connector_id IS NOT NULL
                                     AND connector.revoked_at IS NULL
                                THEN 1 ELSE 0 END) AS active_connector_count,
                       SUM(CASE WHEN connector.revoked_at IS NULL
                                     AND connector.setup_status = 'configured'
                                     AND connector.connector_last_seen_at >= ?
                                THEN 1 ELSE 0 END) AS online_connector_count,
                       SUM(CASE WHEN connector.revoked_at IS NULL
                                     AND connector.setup_status = 'configured'
                                THEN 1 ELSE 0 END) AS configured_connector_count,
                       SUM(CASE WHEN connector.revoked_at IS NULL
                                     AND connector.setup_status = 'manual'
                                THEN 1 ELSE 0 END) AS manual_connector_count,
                       SUM(CASE WHEN connector.revoked_at IS NULL
                                     AND connector.setup_status = 'failed'
                                THEN 1 ELSE 0 END) AS failed_connector_count,
                       MAX(connector.connector_last_seen_at)
                           AS latest_connector_last_seen_at,
                       (
                           SELECT participant.display_name
                           FROM agent_connectors AS recent
                           JOIN participants AS participant
                             ON participant.participant_id = recent.accepted_participant_id
                           WHERE recent.invitation_id = invitation.invitation_id
                           ORDER BY recent.created_at DESC LIMIT 1
                       ) AS last_accepted_display_name,
                       (
                           SELECT participant.client_type
                           FROM agent_connectors AS recent
                           JOIN participants AS participant
                             ON participant.participant_id = recent.accepted_participant_id
                           WHERE recent.invitation_id = invitation.invitation_id
                           ORDER BY recent.created_at DESC LIMIT 1
                       ) AS last_accepted_client_type
                FROM agent_invitations AS invitation
                JOIN web_users AS creator
                  ON creator.user_id = invitation.created_by_web_user_id
                LEFT JOIN agent_connectors AS connector
                  ON connector.invitation_id = invitation.invitation_id
                WHERE (? IS NULL OR invitation.conversation_id = ?)
                GROUP BY invitation.invitation_id
                ORDER BY invitation.created_at DESC
                LIMIT ?
                """,
                (
                    now - CONNECTOR_ONLINE_WINDOW_SECONDS,
                    conversation,
                    conversation,
                    normalized_limit,
                ),
            ).fetchall()
        return [self._agent_invitation_payload(row, now=now) for row in rows]

    def revoke_agent_invitation(
        self,
        *,
        invitation_id: str,
        revoked_by_web_user_id: str,
    ) -> dict[str, Any]:
        invitation = opaque_id(invitation_id, field="invitation_id")
        reviewer = opaque_id(
            revoked_by_web_user_id,
            field="revoked_by_web_user_id",
        )
        now = time.time()
        with self._transaction() as conn:
            self._expire_agent_invitations_locked(conn, now=now)
            row = conn.execute(
                "SELECT * FROM agent_invitations WHERE invitation_id = ?",
                (invitation,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"unknown agent invitation: {invitation}")
            room_permissions = self._room_web_permissions_locked(
                conn,
                web_user_id=reviewer,
                conversation_id=str(row["conversation_id"]),
            )
            if not room_permissions["can_invite_agents"]:
                raise AuthorizationError("你没有撤销本聊天室邀请的权限")
            if str(row["status"]) == "revoked":
                projected = self._agent_invitation_row_locked(
                    conn,
                    invitation_id=invitation,
                    now=now,
                )
                return self._agent_invitation_payload(projected, now=now)
            conn.execute(
                """
                UPDATE agent_invitations
                SET status = 'revoked', revoked_at = ?, updated_at = ?
                WHERE invitation_id = ?
                """,
                (now, now, invitation),
            )
            conn.execute(
                "UPDATE agent_connectors SET setup_status = 'revoked', "
                "revoked_at = COALESCE(revoked_at, ?), "
                "revoked_by_web_user_id = "
                "COALESCE(revoked_by_web_user_id, ?), "
                "enrollment_token_hash = NULL, "
                "previous_enrollment_token_hash = NULL, "
                "previous_enrollment_valid_until = NULL, "
                "setup_updated_at = ?, updated_at = ? "
                "WHERE invitation_id = ?",
                (now, reviewer, now, now, invitation),
            )
            conn.execute(
                "UPDATE agent_sessions SET revoked_at = ?, "
                "revoked_reason = 'connector_invitation_revoked' "
                "WHERE connector_id IN (SELECT connector_id FROM agent_connectors "
                "WHERE invitation_id = ?) AND revoked_at IS NULL",
                (now, invitation),
            )
            updated = self._agent_invitation_row_locked(
                conn,
                invitation_id=invitation,
                now=now,
            )
        return self._agent_invitation_payload(updated, now=now)

    def request_agent_connector_enrollment_rotation(
        self,
        *,
        connector_id: str,
        requested_by_web_user_id: str,
    ) -> dict[str, Any]:
        connector = opaque_id(connector_id, field="connector_id")
        requester = opaque_id(
            requested_by_web_user_id,
            field="requested_by_web_user_id",
        )
        now = time.time()
        with self._transaction() as conn:
            self._require_active_admin_locked(conn, requester)
            row = self._agent_connector_row_locked(conn, connector)
            if row["revoked_at"] is not None or str(row["invitation_status"]) == "revoked":
                raise ConflictError("Agent connector is revoked")
            conn.execute(
                "UPDATE agent_connectors SET "
                "enrollment_rotation_required_at = "
                "COALESCE(enrollment_rotation_required_at, ?), "
                "enrollment_rotation_requested_by_web_user_id = "
                "CASE WHEN enrollment_rotation_required_at IS NULL THEN ? "
                "ELSE enrollment_rotation_requested_by_web_user_id END, "
                "updated_at = ? WHERE connector_id = ?",
                (now, requester, now, connector),
            )
            updated = self._agent_connector_row_locked(conn, connector)
        return self._agent_connector_payload(updated, now=now)

    def revoke_agent_connector(
        self,
        *,
        connector_id: str,
        revoked_by_web_user_id: str,
    ) -> dict[str, Any]:
        connector = opaque_id(connector_id, field="connector_id")
        requester = opaque_id(
            revoked_by_web_user_id,
            field="revoked_by_web_user_id",
        )
        now = time.time()
        with self._transaction() as conn:
            self._require_active_admin_locked(conn, requester)
            row = self._agent_connector_row_locked(conn, connector)
            if row["revoked_at"] is None:
                conn.execute(
                    "UPDATE agent_connectors SET setup_status = 'revoked', "
                    "revoked_at = ?, revoked_by_web_user_id = ?, "
                    "enrollment_token_hash = NULL, "
                    "previous_enrollment_token_hash = NULL, "
                    "previous_enrollment_valid_until = NULL, "
                    "enrollment_rotation_required_at = NULL, "
                    "enrollment_rotation_requested_by_web_user_id = NULL, "
                    "setup_updated_at = ?, updated_at = ? "
                    "WHERE connector_id = ? AND revoked_at IS NULL",
                    (now, requester, now, now, connector),
                )
                conn.execute(
                    "UPDATE agent_sessions SET revoked_at = ?, "
                    "revoked_reason = 'connector_revoked' "
                    "WHERE connector_id = ? AND revoked_at IS NULL",
                    (now, connector),
                )
            updated = self._agent_connector_row_locked(conn, connector)
        return self._agent_connector_payload(updated, now=now)

    def rotate_agent_connector_enrollment(
        self,
        *,
        connector_id: str,
        current_enrollment_token: str,
        new_enrollment_token: str,
    ) -> dict[str, Any]:
        connector = opaque_id(connector_id, field="connector_id")
        current_token = opaque_id(
            current_enrollment_token,
            field="current_enrollment_token",
        )
        next_token = opaque_id(
            new_enrollment_token,
            field="new_enrollment_token",
        )
        if not next_token.startswith("enroll_") or len(next_token) < 40:
            raise ValidationError(
                "new_enrollment_token must be a strong enroll_ token"
            )
        current_hash = self._secret_hash(current_token)
        next_hash = self._secret_hash(next_token)
        now = time.time()
        with self._transaction() as conn:
            row = self._agent_connector_row_locked(conn, connector)
            if row["revoked_at"] is not None or str(row["invitation_status"]) == "revoked":
                raise AuthenticationError("invalid or revoked Agent enrollment")
            stored_current = str(row["enrollment_token_hash"] or "")
            stored_previous = str(row["previous_enrollment_token_hash"] or "")
            previous_valid = bool(
                stored_previous
                and row["previous_enrollment_valid_until"] is not None
                and float(row["previous_enrollment_valid_until"]) > now
            )
            matches_current = self._constant_time_eq(current_hash, stored_current)
            matches_previous = bool(
                previous_valid
                and self._constant_time_eq(current_hash, stored_previous)
            )
            if matches_previous and self._constant_time_eq(next_hash, stored_current):
                # The server committed the first attempt but its response was
                # lost. Repeating the exact client-generated successor is safe.
                updated = row
            elif matches_current:
                if self._constant_time_eq(next_hash, stored_current):
                    raise ValidationError(
                        "new enrollment credential must differ from the current one"
                    )
                if stored_previous and self._constant_time_eq(
                    next_hash,
                    stored_previous,
                ):
                    raise ValidationError(
                        "new enrollment credential must not reuse the previous one"
                    )
                collision = conn.execute(
                    "SELECT connector_id FROM agent_connectors "
                    "WHERE connector_id <> ? AND revoked_at IS NULL AND ("
                    "enrollment_token_hash = ? OR ("
                    "previous_enrollment_token_hash = ? "
                    "AND previous_enrollment_valid_until > ?)) LIMIT 1",
                    (connector, next_hash, next_hash, now),
                ).fetchone()
                if collision is not None:
                    raise ValidationError(
                        "new enrollment credential is already assigned"
                    )
                conn.execute(
                    "UPDATE agent_connectors SET "
                    "previous_enrollment_token_hash = enrollment_token_hash, "
                    "previous_enrollment_valid_until = ?, "
                    "enrollment_token_hash = ?, enrollment_rotated_at = ?, "
                    "enrollment_rotation_count = enrollment_rotation_count + 1, "
                    "enrollment_credential_version = "
                    "enrollment_credential_version + 1, "
                    "enrollment_rotation_required_at = NULL, "
                    "enrollment_rotation_requested_by_web_user_id = NULL, "
                    "updated_at = ? WHERE connector_id = ?",
                    (
                        now + ENROLLMENT_PREVIOUS_GRACE_SECONDS,
                        next_hash,
                        now,
                        now,
                        connector,
                    ),
                )
                updated = self._agent_connector_row_locked(conn, connector)
            else:
                raise AuthenticationError("invalid or revoked Agent enrollment")
        payload = self._agent_connector_payload(updated, now=now)
        payload["rotation_completed"] = True
        return payload

    @staticmethod
    def _agent_connector_row_locked(
        conn: sqlite3.Connection,
        connector_id: str,
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT connector.*, invitation.product, invitation.requested_mode, "
            "invitation.adapter_kind, invitation.tui_adapter_kind, "
            "invitation.status AS invitation_status "
            "FROM agent_connectors AS connector "
            "JOIN agent_invitations AS invitation "
            "ON invitation.invitation_id = connector.invitation_id "
            "WHERE connector.connector_id = ?",
            (connector_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"unknown Agent connector: {connector_id}")
        return row

    def accept_agent_invitation(
        self,
        *,
        invitation_token: str,
        product: str,
        username: str,
        signature: str,
        avatar_key: object = "auto",
        roles: Sequence[str] | None = None,
        capabilities: Sequence[str] | None = None,
        enrollment_token: str | None = None,
        connector_binding_version: object = 2,
        tui_endpoint_id: str | None = None,
        tui_native_session_id: str | None = None,
        tui_access_mode: str = "unknown",
        tui_confirmed: bool = False,
    ) -> dict[str, Any]:
        # Compatibility-only input from pre-v35 connectors. Bridge must never
        # store or interpret the local TUI's mutable permission mode.
        del tui_access_mode
        normalized_invitation_token = opaque_id(
            invitation_token,
            field="invitation_token",
        )
        normalized_product = token(product, field="product_name")
        requested_username = agent_username(username)
        normalized_avatar = normalize_avatar_key(avatar_key)
        normalized_tui_endpoint = (
            opaque_id(tui_endpoint_id, field="tui_endpoint_id")
            if str(tui_endpoint_id or "").strip()
            else None
        )
        normalized_tui_session = (
            opaque_id(tui_native_session_id, field="tui_native_session_id")
            if str(tui_native_session_id or "").strip()
            else None
        )
        if not isinstance(tui_confirmed, bool):
            raise ValidationError("tui_confirmed must be a boolean")
        if isinstance(connector_binding_version, bool):
            raise ValidationError("connector_binding_version must be 1 or 2")
        try:
            requested_binding_version = int(connector_binding_version)
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "connector_binding_version must be 1 or 2"
            ) from exc
        if (
            requested_binding_version not in {1, 2}
            or (
                not isinstance(connector_binding_version, int)
                and str(connector_binding_version).strip()
                != str(requested_binding_version)
            )
        ):
            raise ValidationError("connector_binding_version must be 1 or 2")
        normalized_enrollment = None
        if enrollment_token is not None:
            normalized_enrollment = opaque_id(
                enrollment_token,
                field="enrollment_token",
            )
            if not normalized_enrollment.startswith("enroll_") or len(
                normalized_enrollment
            ) < 40:
                raise ValidationError("enrollment_token must be a strong enroll_ token")
        now = time.time()
        with self._transaction() as conn:
            self._expire_agent_invitations_locked(conn, now=now)
            self._expire_inactive_agents_locked(conn, now=now)
            invitation = conn.execute(
                "SELECT * FROM agent_invitations WHERE token_hash = ?",
                (self._secret_hash(normalized_invitation_token),),
            ).fetchone()
            if invitation is None:
                raise AuthenticationError("invalid Agent invitation")
            invitation_status = str(invitation["status"])
            if invitation_status == "revoked":
                raise ConflictError("Agent invitation is revoked")
            if not secrets.compare_digest(
                normalized_product,
                str(invitation["product"]),
            ):
                raise AuthenticationError("Agent invitation product does not match")
            invitation_tui_adapter = str(invitation["tui_adapter_kind"] or "").strip()
            native_tui_invitation = invitation_tui_adapter in NATIVE_TUI_ADAPTERS
            native_tui_required = (
                str(invitation["requested_mode"]) == "resident"
                and native_tui_invitation
            )
            tui_binding_supplied = bool(
                tui_confirmed
                or normalized_tui_endpoint is not None
                or normalized_tui_session is not None
            )
            if not native_tui_invitation and tui_binding_supplied:
                raise ConflictError(
                    "this invitation does not accept a native TUI binding"
                )
            native_tui_binding_expected = native_tui_required or tui_binding_supplied
            if native_tui_invitation and native_tui_binding_expected and (
                not tui_confirmed
                or normalized_tui_endpoint is None
                or normalized_tui_session is None
            ):
                raise ConflictError(
                    "native-TUI bindings require explicit TUI "
                    "confirmation, endpoint identity, and native session identity"
                )
            endpoint_owner = None
            duplicate_session = None
            if normalized_tui_endpoint is not None:
                endpoint_owner = conn.execute(
                    """
                    SELECT connector.accepted_participant_id,
                           connector.bound_client_type,
                           invitation.product
                    FROM agent_connectors AS connector
                    JOIN agent_invitations AS invitation
                      ON invitation.invitation_id = connector.invitation_id
                    WHERE connector.tui_endpoint_id = ?
                      AND connector.revoked_at IS NULL
                      AND invitation.status != 'revoked'
                    ORDER BY connector.updated_at DESC
                    LIMIT 1
                    """,
                    (normalized_tui_endpoint,),
                ).fetchone()
                if endpoint_owner is not None and not secrets.compare_digest(
                    normalized_product,
                    str(endpoint_owner["product"]),
                ):
                    raise ConflictError(
                        "native TUI endpoint is already bound to another product identity"
                    )
                if normalized_tui_session is not None:
                    duplicate_session = conn.execute(
                        "SELECT connector_id FROM agent_connectors "
                        "WHERE tui_endpoint_id = ? AND tui_native_session_id = ? "
                        "AND revoked_at IS NULL LIMIT 1",
                        (normalized_tui_endpoint, normalized_tui_session),
                    ).fetchone()
            existing_connector = None
            if normalized_enrollment is not None:
                enrollment_hash = self._secret_hash(normalized_enrollment)
                previous_collision = conn.execute(
                    "SELECT connector_id FROM agent_connectors "
                    "WHERE previous_enrollment_token_hash = ? "
                    "AND previous_enrollment_valid_until > ? "
                    "AND revoked_at IS NULL LIMIT 1",
                    (enrollment_hash, now),
                ).fetchone()
                if previous_collision is not None:
                    raise ConflictError(
                        "enrollment token is already bound to a rotating connector"
                    )
                existing_connector = conn.execute(
                    """
                    SELECT connector.*, participant.client_type
                    FROM agent_connectors AS connector
                    JOIN participants AS participant
                      ON participant.participant_id = connector.accepted_participant_id
                    WHERE connector.enrollment_token_hash = ?
                    """,
                    (enrollment_hash,),
                ).fetchone()
            if duplicate_session is not None and (
                existing_connector is None
                or str(duplicate_session["connector_id"])
                != str(existing_connector["connector_id"])
            ):
                raise ConflictError(
                    "each room binding requires a distinct native TUI session"
                )
            if existing_connector is not None:
                if str(existing_connector["invitation_id"]) != str(
                    invitation["invitation_id"]
                ):
                    raise ConflictError(
                        "enrollment token is already bound to another invitation"
                    )
                if existing_connector["revoked_at"] is not None:
                    raise ConflictError("Agent connector is revoked")
                for field, supplied in (
                    ("tui_endpoint_id", normalized_tui_endpoint),
                    ("tui_native_session_id", normalized_tui_session),
                ):
                    bound_value = str(existing_connector[field] or "")
                    if (
                        supplied is not None
                        and bound_value
                        and not self._constant_time_eq(
                            supplied,
                            bound_value,
                        )
                    ):
                        raise AuthenticationError(
                            "Agent invitation retry native TUI binding does not match"
                        )
                bound_identity = str(
                    existing_connector["bound_client_type"]
                    or existing_connector["client_type"]
                )
                assigned_username = self._username_from_bound_identity(
                    product=normalized_product,
                    client_type=bound_identity,
                )
                original_username = str(
                    existing_connector["requested_username"]
                    or assigned_username
                )
                if not any(
                    self._constant_time_eq(requested_username, candidate)
                    for candidate in (original_username, assigned_username)
                ):
                    raise AuthenticationError(
                        "Agent invitation retry identity does not match"
                    )
                bound_roles = self._connector_bound_tokens(
                    existing_connector,
                    column="bound_roles_json",
                    field="roles",
                )
                bound_capabilities = self._connector_bound_tokens(
                    existing_connector,
                    column="bound_capabilities_json",
                    field="capabilities",
                )
                registration = self._normalized_agent_registration(
                    product=normalized_product,
                    username=assigned_username,
                    session_alias=None,
                    signature=signature,
                    conversation_id=str(existing_connector["conversation_id"]),
                    roles=bound_roles,
                    capabilities=bound_capabilities,
                    session_ttl_seconds=DEFAULT_SESSION_TTL_SECONDS,
                )
                connector_id = str(existing_connector["connector_id"])
                registered = self._register_agent_session_locked(
                    conn,
                    registration=registration,
                    connector_id=connector_id,
                    session_component="mcp",
                    invitation_grant=False,
                    now=now,
                )
                conn.execute(
                    "UPDATE agent_connectors SET enrollment_last_used_at = ?, "
                    "updated_at = ? WHERE connector_id = ?",
                    (now, now, connector_id),
                )
                setup_status = str(existing_connector["setup_status"])
                binding_version = int(existing_connector["binding_version"] or 1)
            else:
                if invitation_status != "active":
                    raise ConflictError(
                        f"Agent invitation is {invitation_status}"
                    )
                connector_id = f"connector_{uuid.uuid4().hex}"
                if endpoint_owner is not None:
                    assigned_username = self._username_from_bound_identity(
                        product=normalized_product,
                        client_type=str(endpoint_owner["bound_client_type"]),
                    )
                elif requested_binding_version >= 2:
                    assigned_username = self._allocate_connector_username_locked(
                        conn,
                        product=normalized_product,
                        requested_username=requested_username,
                        connector_id=connector_id,
                        conversation_id=str(invitation["conversation_id"]),
                    )
                else:
                    assigned_username = requested_username
                    requested_identity = product_username(
                        normalized_product,
                        assigned_username,
                    )
                    duplicate_connector = conn.execute(
                        """
                        SELECT connector.connector_id
                        FROM agent_connectors AS connector
                        JOIN participants AS participant
                          ON participant.participant_id =
                             connector.accepted_participant_id
                        WHERE participant.client_type = ?
                          AND connector.revoked_at IS NULL
                        LIMIT 1
                        """,
                        (requested_identity,),
                    ).fetchone()
                    if duplicate_connector is not None:
                        raise ConflictError(
                            "this legacy Agent client must choose a unique username "
                            "or upgrade connector support"
                        )
                registration = self._normalized_agent_registration(
                    product=normalized_product,
                    username=assigned_username,
                    session_alias=None,
                    signature=signature,
                    conversation_id=str(invitation["conversation_id"]),
                    roles=roles,
                    capabilities=capabilities,
                    session_ttl_seconds=DEFAULT_SESSION_TTL_SECONDS,
                )
                normalized_enrollment = normalized_enrollment or (
                    f"enroll_{secrets.token_urlsafe(32)}"
                )
                registered = self._register_agent_session_locked(
                    conn,
                    registration=registration,
                    connector_id=connector_id,
                    session_component="mcp",
                    invitation_grant=True,
                    now=now,
                )
                avatar_profile = conn.execute(
                    "SELECT avatar_key, avatar_changed_at FROM participants "
                    "WHERE participant_id = ?",
                    (registered["participant_id"],),
                ).fetchone()
                avatar_warning = None
                try:
                    next_avatar_changed_at = self._next_avatar_changed_at(
                        current_avatar=str(avatar_profile["avatar_key"] or "auto"),
                        current_changed_at=avatar_profile["avatar_changed_at"],
                        next_avatar=normalized_avatar,
                        now=now,
                    )
                except AvatarRateLimitError as exc:
                    # Joining another room must not fail just because this
                    # shared identity picked another avatar too recently.
                    avatar_warning = str(exc)
                else:
                    if normalized_avatar != str(
                        avatar_profile["avatar_key"] or "auto"
                    ):
                        conn.execute(
                            "UPDATE participants SET avatar_key = ?, "
                            "avatar_changed_at = ?, profile_updated_at = ? "
                            "WHERE participant_id = ?",
                            (
                                normalized_avatar,
                                next_avatar_changed_at,
                                now,
                                registered["participant_id"],
                            ),
                        )
                        registered["avatar_key"] = normalized_avatar
                if avatar_warning is not None:
                    registered["avatar_selection_warning"] = avatar_warning
                setup_status = (
                    "awaiting_setup"
                    if str(invitation["requested_mode"]) == "resident"
                    and (
                        str(invitation["adapter_kind"]) != "manual"
                        or invitation["tui_adapter_kind"] is not None
                    )
                    else "manual"
                )
                conn.execute(
                    """
                    INSERT INTO agent_connectors
                        (connector_id, invitation_id, conversation_id,
                         accepted_participant_id,
                         initial_session_id, enrollment_token_hash,
                        enrollment_last_used_at, setup_status,
                         setup_updated_at, binding_version,
                         requested_username, bound_client_type,
                         bound_roles_json, bound_capabilities_json,
                         tui_endpoint_id, tui_native_session_id,
                         tui_state, tui_last_seen_at,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?)
                    """,
                    (
                        connector_id,
                        str(invitation["invitation_id"]),
                        str(registration["conversation_id"]),
                        registered["participant_id"],
                        registered["session_id"],
                        self._secret_hash(normalized_enrollment),
                        now,
                        setup_status,
                        now,
                        requested_binding_version,
                        requested_username,
                        str(registration["identity"]),
                        compact_json(registration["roles"]),
                        compact_json(registration["capabilities"]),
                        normalized_tui_endpoint,
                        normalized_tui_session,
                        (
                            "offline"
                            if native_tui_invitation and native_tui_binding_expected
                            else (
                                "awaiting_confirmation"
                                if invitation_tui_adapter in NATIVE_TUI_ADAPTERS
                                else "unbound"
                            )
                        ),
                        None,
                        now,
                        now,
                    ),
                )
                binding_version = requested_binding_version
                conn.execute(
                    """
                    UPDATE agent_invitations
                    SET use_count = use_count + 1,
                        first_accepted_at = COALESCE(first_accepted_at, ?),
                        last_accepted_at = ?,
                        status = CASE
                            WHEN max_uses IS NOT NULL
                             AND use_count + 1 >= max_uses
                            THEN 'exhausted'
                            ELSE status
                        END,
                        updated_at = ?
                    WHERE invitation_id = ? AND status = 'active'
                    """,
                    (now, now, now, str(invitation["invitation_id"])),
                )
        registered.update(
            {
                "invitation_id": str(invitation["invitation_id"]),
                "requested_mode": str(invitation["requested_mode"]),
                "adapter_kind": str(invitation["adapter_kind"]),
                "tui_adapter_kind": (
                    str(invitation["tui_adapter_kind"])
                    if invitation["tui_adapter_kind"] is not None
                    else None
                ),
                "reuse_policy": str(invitation["reuse_policy"]),
                "invitation_reusable": (
                    str(invitation["reuse_policy"]) == "reusable"
                ),
                "enrollment_token": normalized_enrollment,
                "setup_status": setup_status,
                "identity_binding_version": binding_version,
                "enrollment_credential_version": (
                    int(existing_connector["enrollment_credential_version"] or 1)
                    if existing_connector is not None
                    else 1
                ),
                "enrollment_credential_state": "current",
                "enrollment_rotation_required": bool(
                    existing_connector is not None
                    and existing_connector["enrollment_rotation_required_at"]
                    is not None
                ),
                "enrollment_previous_valid_until": (
                    float(existing_connector["previous_enrollment_valid_until"])
                    if existing_connector is not None
                    and existing_connector["previous_enrollment_valid_until"]
                    is not None
                    else None
                ),
                "tui_endpoint_id": normalized_tui_endpoint,
                "tui_native_session_id": normalized_tui_session,
            }
        )
        return registered

    @staticmethod
    def _username_from_bound_identity(*, product: str, client_type: str) -> str:
        prefix = f"{product}-"
        if not client_type.startswith(prefix):
            raise AuthenticationError("Agent connector product binding is invalid")
        return agent_username(client_type[len(prefix) :])

    @staticmethod
    def _connector_bound_tokens(
        connector: sqlite3.Row,
        *,
        column: str,
        field: str,
    ) -> list[str]:
        try:
            raw = json.loads(str(connector[column] or "[]"))
        except (TypeError, json.JSONDecodeError) as exc:
            raise BridgeError("Agent connector authority binding is invalid") from exc
        if not isinstance(raw, list):
            raise BridgeError("Agent connector authority binding is invalid")
        return string_tokens(raw, field=field)

    @staticmethod
    def _allocate_connector_username_locked(
        conn: sqlite3.Connection,
        *,
        product: str,
        requested_username: str,
        connector_id: str,
        conversation_id: str,
    ) -> str:
        """Allocate one durable machine username without reusing an identity."""

        requested_identity = product_username(product, requested_username)
        existing = conn.execute(
            """
            SELECT participant.participant_id,
                   EXISTS (
                       SELECT 1 FROM agent_connectors AS connector
                       WHERE connector.accepted_participant_id =
                             participant.participant_id
                         AND connector.revoked_at IS NULL
                   ) AS has_active_connector,
                   COALESCE(lifecycle.reinvite_required, 0) AS reinvite_required,
                   EXISTS (
                       SELECT 1 FROM agent_room_blocks AS block
                       WHERE block.participant_id = participant.participant_id
                         AND block.conversation_id = ?
                   ) AS blocked_in_room
            FROM participants AS participant
            LEFT JOIN agent_lifecycle_states AS lifecycle
              ON lifecycle.participant_id = participant.participant_id
            WHERE participant.client_type = ?
            """,
            (conversation_id, requested_identity),
        ).fetchone()
        if existing is None:
            return requested_username
        if (
            not bool(existing["has_active_connector"])
            and (
                bool(existing["reinvite_required"])
                or bool(existing["blocked_in_room"])
            )
        ):
            return requested_username
        identifier = connector_id.removeprefix("connector_")
        maximum = min(
            MAX_AGENT_USERNAME_CHARS,
            MAX_CLIENT_IDENTITY_CHARS - len(product) - 1,
        )
        for suffix_length in (8, 12, 16, 24, 32):
            suffix = identifier[:suffix_length]
            base_length = maximum - len(suffix) - 1
            if base_length < 1:
                continue
            candidate = f"{requested_username[:base_length]}-{suffix}"
            candidate_identity = product_username(product, candidate)
            collision = conn.execute(
                "SELECT 1 FROM participants WHERE client_type = ?",
                (candidate_identity,),
            ).fetchone()
            if collision is None:
                return candidate
        raise ConflictError("could not allocate a unique Agent connector identity")

    @staticmethod
    def _connector_required_components(connector: sqlite3.Row) -> set[str]:
        if str(connector["requested_mode"]) == "resident" and (
            str(connector["adapter_kind"]) in {"codex", "claude-code"}
            or (
                "tui_adapter_kind" in connector.keys()
                and str(connector["tui_adapter_kind"] or "") in NATIVE_TUI_ADAPTERS
            )
        ):
            return {"listener", "chat", "task"}
        return {"mcp"}

    @classmethod
    def _record_connector_component_locked(
        cls,
        conn: sqlite3.Connection,
        *,
        connector: sqlite3.Row,
        component: str | None,
        protocol_version: int,
        now: float,
    ) -> tuple[int, list[str], list[str]]:
        normalized = str(component or "").strip().lower()
        if not normalized:
            current = int(connector["binding_version"] or 1)
        else:
            if normalized not in CONNECTOR_COMPONENTS:
                raise ValidationError("unsupported Agent connector component")
            if protocol_version < 2:
                raise ValidationError("connector component protocol must be at least 2")
            conn.execute(
                """
                INSERT INTO connector_component_readiness
                    (connector_id, component, protocol_version,
                     first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(connector_id, component) DO UPDATE SET
                    protocol_version = MAX(
                        connector_component_readiness.protocol_version,
                        excluded.protocol_version
                    ),
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    str(connector["connector_id"]),
                    normalized,
                    protocol_version,
                    now,
                    now,
                ),
            )
            current = int(connector["binding_version"] or 1)
        ready = {
            str(row["component"])
            for row in conn.execute(
                "SELECT component FROM connector_component_readiness "
                "WHERE connector_id = ? AND protocol_version >= 2",
                (str(connector["connector_id"]),),
            ).fetchall()
        }
        required = cls._connector_required_components(connector)
        if current < 2 and required.issubset(ready):
            conn.execute(
                "UPDATE agent_connectors SET binding_version = 2, updated_at = ? "
                "WHERE connector_id = ? AND binding_version < 2",
                (now, str(connector["connector_id"])),
            )
            current = 2
        return current, sorted(ready), sorted(required - ready)

    def register_agent_session_from_enrollment(
        self,
        *,
        enrollment_token: str,
        connector_id: str | None = None,
        connector_component: str | None = None,
        connector_protocol_version: int = 2,
        product: str,
        username: str,
        session_alias: str | None = None,
        signature: str | None = None,
        roles: Sequence[str] | None = None,
        capabilities: Sequence[str] | None = None,
        session_ttl_seconds: float = DEFAULT_SESSION_TTL_SECONDS,
    ) -> dict[str, Any]:
        normalized_enrollment = opaque_id(
            enrollment_token,
            field="enrollment_token",
        )
        normalized_product = token(product, field="product_name")
        normalized_identity = product_username(normalized_product, username)
        normalized_connector = (
            opaque_id(connector_id, field="connector_id") if connector_id else None
        )
        now = time.time()
        with self._transaction() as conn:
            self._expire_inactive_agents_locked(conn, now=now)
            invitation = conn.execute(
                """
                SELECT invitation.*, connector.connector_id,
                       connector.conversation_id AS connector_conversation_id,
                       connector.accepted_participant_id,
                       connector.revoked_at AS connector_revoked_at,
                       connector.binding_version,
                       connector.bound_client_type,
                       connector.bound_roles_json,
                       connector.bound_capabilities_json,
                       connector.enrollment_token_hash,
                       connector.previous_enrollment_token_hash,
                       connector.previous_enrollment_valid_until,
                       connector.enrollment_credential_version,
                       connector.enrollment_rotation_required_at,
                       participant.client_type
                FROM agent_connectors AS connector
                JOIN agent_invitations AS invitation
                  ON invitation.invitation_id = connector.invitation_id
                JOIN participants AS participant
                  ON participant.participant_id = connector.accepted_participant_id
                WHERE connector.enrollment_token_hash = ?
                   OR (
                       connector.previous_enrollment_token_hash = ?
                       AND connector.previous_enrollment_valid_until > ?
                   )
                """,
                (
                    self._secret_hash(normalized_enrollment),
                    self._secret_hash(normalized_enrollment),
                    now,
                ),
            ).fetchone()
            if (
                invitation is None
                or str(invitation["status"]) == "revoked"
                or invitation["connector_revoked_at"] is not None
            ):
                raise AuthenticationError("invalid or revoked Agent enrollment")
            bound_connector_id = str(invitation["connector_id"])
            enrollment_hash = self._secret_hash(normalized_enrollment)
            using_previous_enrollment = not self._constant_time_eq(
                enrollment_hash,
                str(invitation["enrollment_token_hash"] or ""),
            )
            binding_version = int(invitation["binding_version"] or 1)
            if normalized_connector is not None and not self._constant_time_eq(
                normalized_connector,
                bound_connector_id,
            ):
                raise AuthenticationError("Agent enrollment connector does not match")
            ready_components: list[str] = []
            missing_components: list[str] = []
            if normalized_connector is not None:
                (
                    binding_version,
                    ready_components,
                    missing_components,
                ) = self._record_connector_component_locked(
                    conn,
                    connector=invitation,
                    component=connector_component,
                    protocol_version=int(connector_protocol_version),
                    now=now,
                )
            if binding_version >= 2 and normalized_connector is None:
                raise AuthenticationError(
                    "Agent connector identity is required for enrollment"
                )
            bound_identity = str(
                invitation["bound_client_type"] or invitation["client_type"]
            )
            if not self._constant_time_eq(
                normalized_identity,
                bound_identity,
            ):
                raise AuthenticationError("Agent enrollment identity does not match")
            assigned_username = self._username_from_bound_identity(
                product=normalized_product,
                client_type=bound_identity,
            )
            bound_roles = self._connector_bound_tokens(
                invitation,
                column="bound_roles_json",
                field="roles",
            )
            bound_capabilities = self._connector_bound_tokens(
                invitation,
                column="bound_capabilities_json",
                field="capabilities",
            )
            registration = self._normalized_agent_registration(
                product=normalized_product,
                username=assigned_username,
                session_alias=session_alias,
                signature=signature,
                conversation_id=str(invitation["connector_conversation_id"]),
                roles=bound_roles,
                capabilities=bound_capabilities,
                session_ttl_seconds=session_ttl_seconds,
            )
            registered = self._register_agent_session_locked(
                conn,
                registration=registration,
                connector_id=bound_connector_id,
                session_component=(connector_component or "mcp"),
                invitation_grant=False,
                now=now,
            )
            conn.execute(
                "UPDATE agent_connectors SET enrollment_last_used_at = ?, "
                "updated_at = ? WHERE connector_id = ?",
                (now, now, bound_connector_id),
            )
        registered["invitation_id"] = str(invitation["invitation_id"])
        registered["adapter_kind"] = str(invitation["adapter_kind"])
        registered["tui_adapter_kind"] = (
            str(invitation["tui_adapter_kind"])
            if invitation["tui_adapter_kind"] is not None
            else None
        )
        registered["identity_binding_version"] = binding_version
        registered["ready_components"] = ready_components
        registered["missing_components"] = missing_components
        registered["enrollment_credential_version"] = int(
            invitation["enrollment_credential_version"] or 1
        )
        registered["enrollment_credential_state"] = (
            "grace" if using_previous_enrollment else "current"
        )
        registered["enrollment_rotation_required"] = bool(
            using_previous_enrollment
            or invitation["enrollment_rotation_required_at"] is not None
        )
        registered["enrollment_previous_valid_until"] = (
            float(invitation["previous_enrollment_valid_until"])
            if using_previous_enrollment
            and invitation["previous_enrollment_valid_until"] is not None
            else None
        )
        return registered

    def report_agent_connector_setup(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        connector_id: str,
        setup_status: str,
        detail: object = None,
    ) -> dict[str, Any]:
        participant = opaque_id(participant_id, field="participant_id")
        session_id = opaque_id(
            authorized_session_id,
            field="authorized_session_id",
        )
        connector = opaque_id(connector_id, field="connector_id")
        status = str(setup_status or "").strip().lower()
        if status not in {"awaiting_setup", "configured", "manual", "failed"}:
            raise ValidationError("unsupported connector setup status")
        normalized_detail = self._connector_detail(detail)
        now = time.time()
        with self._transaction() as conn:
            session = self._require_live_session(
                conn,
                session_id=session_id,
                participant_id=participant,
                now=now,
            )
            if str(session["connector_id"] or "") != connector:
                raise AuthenticationError("connector does not belong to this session")
            cursor = conn.execute(
                """
                UPDATE agent_connectors
                SET setup_status = ?, setup_detail_json = ?,
                    setup_updated_at = ?, updated_at = ?
                WHERE connector_id = ? AND accepted_participant_id = ?
                  AND revoked_at IS NULL
                  AND EXISTS (
                      SELECT 1 FROM agent_invitations AS invitation
                      WHERE invitation.invitation_id = agent_connectors.invitation_id
                        AND invitation.status != 'revoked'
                  )
                """,
                (
                    status,
                    compact_json(normalized_detail),
                    now,
                    now,
                    connector,
                    participant,
                ),
            )
            if cursor.rowcount != 1:
                raise NotFoundError("active connector invitation was not found")
            row = conn.execute(
                """
                SELECT connector.*, invitation.product, invitation.adapter_kind,
                       invitation.tui_adapter_kind,
                       invitation.status AS invitation_status
                FROM agent_connectors AS connector
                JOIN agent_invitations AS invitation
                  ON invitation.invitation_id = connector.invitation_id
                WHERE connector.connector_id = ?
                """,
                (connector,),
            ).fetchone()
        return self._agent_connector_payload(row, now=now)

    def touch_agent_connector(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        connector_id: str,
    ) -> dict[str, Any] | None:
        participant = opaque_id(participant_id, field="participant_id")
        session_id = opaque_id(
            authorized_session_id,
            field="authorized_session_id",
        )
        connector = opaque_id(connector_id, field="connector_id")
        now = time.time()
        with self._transaction() as conn:
            session = self._require_live_session(
                conn,
                session_id=session_id,
                participant_id=participant,
                now=now,
            )
            if str(session["connector_id"] or "") != connector:
                raise AuthenticationError("connector does not belong to this session")
            cursor = conn.execute(
                """
                UPDATE agent_connectors
                SET connector_last_seen_at = ?, updated_at = ?
                WHERE connector_id = ? AND accepted_participant_id = ?
                  AND revoked_at IS NULL
                  AND EXISTS (
                      SELECT 1 FROM agent_invitations AS invitation
                      WHERE invitation.invitation_id = agent_connectors.invitation_id
                        AND invitation.status != 'revoked'
                  )
                """,
                (now, now, connector, participant),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                """
                SELECT connector.*, invitation.product, invitation.adapter_kind,
                       invitation.tui_adapter_kind,
                       invitation.status AS invitation_status
                FROM agent_connectors AS connector
                JOIN agent_invitations AS invitation
                  ON invitation.invitation_id = connector.invitation_id
                WHERE connector.connector_id = ?
                """,
                (connector,),
            ).fetchone()
        return self._agent_connector_payload(row, now=now)

    def report_agent_tui_state(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        connector_id: str,
        tui_endpoint_id: str,
        tui_native_session_id: str,
        state: str,
        access_mode: object = None,
        capabilities: Sequence[str] | None = None,
        active_task_id: str | None = None,
        detail: object = None,
    ) -> dict[str, Any]:
        """Heartbeat one immutable room-to-native-session binding."""

        participant = opaque_id(participant_id, field="participant_id")
        session_id = opaque_id(
            authorized_session_id,
            field="authorized_session_id",
        )
        connector = opaque_id(connector_id, field="connector_id")
        endpoint = opaque_id(tui_endpoint_id, field="tui_endpoint_id")
        native_session = opaque_id(
            tui_native_session_id,
            field="tui_native_session_id",
        )
        normalized_state = str(state or "").strip().lower()
        if normalized_state not in TUI_STATES - {"unbound", "awaiting_confirmation"}:
            raise ValidationError("unsupported native TUI state")
        # Accepted only so already-running pre-v35 workers continue reporting
        # during a rolling upgrade. The value is intentionally discarded.
        del access_mode
        normalized_capabilities = string_tokens(
            capabilities,
            field="tui_capabilities",
        )
        task_id = (
            opaque_id(active_task_id, field="active_task_id")
            if str(active_task_id or "").strip()
            else None
        )
        normalized_detail = self._connector_detail(detail)
        now = time.time()
        with self._transaction() as conn:
            live_session = self._require_live_session(
                conn,
                session_id=session_id,
                participant_id=participant,
                now=now,
            )
            if str(live_session["connector_id"] or "") != connector:
                raise AuthenticationError("connector does not belong to this session")
            bound = conn.execute(
                "SELECT tui_endpoint_id, tui_native_session_id "
                "FROM agent_connectors WHERE connector_id = ? "
                "AND accepted_participant_id = ? AND revoked_at IS NULL",
                (connector, participant),
            ).fetchone()
            if bound is None:
                raise NotFoundError("active connector invitation was not found")
            if not self._constant_time_eq(
                endpoint,
                str(bound["tui_endpoint_id"] or ""),
            ) or not self._constant_time_eq(
                native_session,
                str(bound["tui_native_session_id"] or ""),
            ):
                raise AuthenticationError("native TUI binding does not match")
            conn.execute(
                """
                UPDATE agent_connectors
                SET tui_state = ?, tui_capabilities_json = ?, tui_last_seen_at = ?,
                    tui_active_task_id = ?, tui_detail_json = ?,
                    connector_last_seen_at = ?, updated_at = ?
                WHERE connector_id = ?
                """,
                (
                    normalized_state,
                    compact_json(normalized_capabilities),
                    now,
                    task_id,
                    compact_json(normalized_detail),
                    now,
                    now,
                    connector,
                ),
            )
            row = conn.execute(
                """
                SELECT connector.*, invitation.product, invitation.adapter_kind,
                       invitation.tui_adapter_kind,
                       invitation.status AS invitation_status
                FROM agent_connectors AS connector
                JOIN agent_invitations AS invitation
                  ON invitation.invitation_id = connector.invitation_id
                WHERE connector.connector_id = ?
                """,
                (connector,),
            ).fetchone()
            endpoint_room_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM agent_connectors "
                    "WHERE tui_endpoint_id = ? AND revoked_at IS NULL",
                    (endpoint,),
                ).fetchone()[0]
            )
        payload = self._agent_connector_payload(row, now=now)
        payload["tui"]["room_binding_count"] = endpoint_room_count
        return payload

    def admin_connector_health(
        self,
        *,
        requesting_web_user_id: str,
    ) -> dict[str, Any]:
        requester = opaque_id(
            requesting_web_user_id,
            field="requesting_web_user_id",
        )
        now = time.time()
        with self._connection() as conn:
            self._require_active_admin_locked(conn, requester)
            rows = conn.execute(
                """
                SELECT connector.*, invitation.product,
                       invitation.requested_mode, invitation.adapter_kind,
                       invitation.tui_adapter_kind,
                       invitation.status AS invitation_status,
                       participant.client_type, participant.display_name,
                       MAX(session.last_seen) AS session_last_seen_at,
                       MAX(
                           CASE WHEN session.expires_at > ?
                                THEN session.last_seen END
                       ) AS active_session_last_seen_at,
                       SUM(
                           CASE WHEN session.expires_at > ? THEN 1 ELSE 0 END
                       ) AS active_session_count,
                       (
                           SELECT MAX(message.created_at)
                           FROM messages AS message
                           WHERE message.sender_participant_id =
                                 connector.accepted_participant_id
                             AND message.conversation_id = connector.conversation_id
                       ) AS last_reply_at,
                       (
                           SELECT COUNT(*)
                           FROM message_deliveries AS delivery
                           JOIN messages AS pending_message
                             ON pending_message.message_id = delivery.message_id
                           WHERE delivery.participant_id =
                                 connector.accepted_participant_id
                             AND pending_message.conversation_id =
                                 connector.conversation_id
                             AND delivery.state IN ('pending', 'delivered')
                       ) AS pending_count,
                       (
                           SELECT MIN(pending_message.created_at)
                           FROM message_deliveries AS delivery
                           JOIN messages AS pending_message
                             ON pending_message.message_id = delivery.message_id
                           WHERE delivery.participant_id =
                                 connector.accepted_participant_id
                             AND pending_message.conversation_id =
                                 connector.conversation_id
                             AND delivery.state IN ('pending', 'delivered')
                       ) AS oldest_pending_at
                       ,(
                           SELECT COUNT(*)
                           FROM message_deliveries AS delivery
                           JOIN messages AS pending_message
                             ON pending_message.message_id = delivery.message_id
                           WHERE delivery.participant_id =
                                 connector.accepted_participant_id
                             AND pending_message.conversation_id =
                                 connector.conversation_id
                             AND delivery.state IN ('pending', 'delivered')
                             AND instr(delivery.reasons_json, '"quiet_optional"') = 0
                             AND (
                                 instr(delivery.reasons_json, '"mention"') > 0
                                 OR instr(
                                     delivery.reasons_json,
                                     '"agent_request"'
                                 ) > 0
                             )
                       ) AS required_pending_count
                       ,(
                           SELECT MIN(pending_message.created_at)
                           FROM message_deliveries AS delivery
                           JOIN messages AS pending_message
                             ON pending_message.message_id = delivery.message_id
                           WHERE delivery.participant_id =
                                 connector.accepted_participant_id
                             AND pending_message.conversation_id =
                                 connector.conversation_id
                             AND delivery.state IN ('pending', 'delivered')
                             AND instr(delivery.reasons_json, '"quiet_optional"') = 0
                             AND (
                                 instr(delivery.reasons_json, '"mention"') > 0
                                 OR instr(
                                     delivery.reasons_json,
                                     '"agent_request"'
                                 ) > 0
                             )
                       ) AS oldest_required_at
                       ,(
                           SELECT COUNT(*)
                           FROM room_tasks AS task
                           WHERE task.conversation_id = connector.conversation_id
                             AND task.claimed_by_participant_id =
                                 connector.accepted_participant_id
                             AND task.status IN (
                                 'queued', 'claimed', 'running', 'needs_input'
                             )
                       ) AS active_task_count
                       ,(
                           SELECT COUNT(*)
                           FROM room_tasks AS task
                           WHERE task.conversation_id = connector.conversation_id
                             AND task.claimed_by_participant_id =
                                 connector.accepted_participant_id
                             AND task.status IN ('claimed', 'running')
                             AND task.lease_expires_at IS NOT NULL
                             AND task.lease_expires_at <= ?
                       ) AS expired_task_lease_count
                FROM agent_connectors AS connector
                JOIN agent_invitations AS invitation
                  ON invitation.invitation_id = connector.invitation_id
                JOIN participants AS participant
                  ON participant.participant_id = connector.accepted_participant_id
                LEFT JOIN agent_sessions AS session
                  ON session.connector_id = connector.connector_id
                 AND session.revoked_at IS NULL
                 AND session.cleared_at IS NULL
                WHERE connector.revoked_at IS NULL
                GROUP BY connector.connector_id
                ORDER BY connector.conversation_id,
                         participant.display_name COLLATE NOCASE,
                         connector.created_at
                """,
                (now, now, now),
            ).fetchall()
            readiness_rows = conn.execute(
                "SELECT * FROM connector_component_readiness"
            ).fetchall()
            component_activity_rows = conn.execute(
                """
                SELECT connector_id, component, MAX(last_seen) AS last_seen_at,
                       SUM(
                           CASE WHEN expires_at > ? THEN 1 ELSE 0 END
                       ) AS active_session_count
                FROM agent_sessions
                WHERE connector_id IS NOT NULL
                  AND revoked_at IS NULL AND cleared_at IS NULL
                GROUP BY connector_id, component
                """,
                (now,),
            ).fetchall()
            backlog_row = conn.execute(
                """
                SELECT COUNT(*) AS pending_count,
                       SUM(
                           CASE WHEN
                               instr(delivery.reasons_json, '"quiet_optional"') = 0
                               AND (
                                   instr(delivery.reasons_json, '"mention"') > 0
                                   OR instr(
                                       delivery.reasons_json,
                                       '"agent_request"'
                                   ) > 0
                               )
                           THEN 1 ELSE 0 END
                       ) AS required_pending_count,
                       MIN(message.created_at) AS oldest_pending_at,
                       MIN(
                           CASE WHEN
                               instr(delivery.reasons_json, '"quiet_optional"') = 0
                               AND (
                                   instr(delivery.reasons_json, '"mention"') > 0
                                   OR instr(
                                       delivery.reasons_json,
                                       '"agent_request"'
                                   ) > 0
                               )
                           THEN message.created_at END
                       ) AS oldest_required_at
                FROM message_deliveries AS delivery
                JOIN messages AS message
                  ON message.message_id = delivery.message_id
                WHERE delivery.state IN ('pending', 'delivered')
                  AND NOT EXISTS (
                      SELECT 1 FROM web_users AS web_user
                      WHERE web_user.participant_id = delivery.participant_id
                  )
                """
            ).fetchone()
            room_backlog_rows = conn.execute(
                """
                SELECT message.conversation_id,
                       COUNT(*) AS pending_count,
                       SUM(
                           CASE WHEN
                               instr(delivery.reasons_json, '"quiet_optional"') = 0
                               AND (
                                   instr(delivery.reasons_json, '"mention"') > 0
                                   OR instr(
                                       delivery.reasons_json,
                                       '"agent_request"'
                                   ) > 0
                               )
                           THEN 1 ELSE 0 END
                       ) AS required_pending_count,
                       MIN(message.created_at) AS oldest_pending_at
                FROM message_deliveries AS delivery
                JOIN messages AS message
                  ON message.message_id = delivery.message_id
                WHERE delivery.state IN ('pending', 'delivered')
                  AND NOT EXISTS (
                      SELECT 1 FROM web_users AS web_user
                      WHERE web_user.participant_id = delivery.participant_id
                  )
                GROUP BY message.conversation_id
                """
            ).fetchall()
            task_row = conn.execute(
                """
                SELECT
                    SUM(
                        CASE WHEN status IN (
                            'queued', 'claimed', 'running', 'needs_input'
                        ) THEN 1 ELSE 0 END
                    ) AS active_count,
                    SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END)
                        AS queued_count,
                    SUM(CASE WHEN status = 'claimed' THEN 1 ELSE 0 END)
                        AS claimed_count,
                    SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END)
                        AS running_count,
                    SUM(CASE WHEN status = 'needs_input' THEN 1 ELSE 0 END)
                        AS needs_input_count,
                    SUM(
                        CASE WHEN status IN ('claimed', 'running')
                                  AND lease_expires_at IS NOT NULL
                                  AND lease_expires_at <= ?
                             THEN 1 ELSE 0 END
                    ) AS expired_lease_count,
                    MIN(
                        CASE WHEN status IN (
                            'queued', 'claimed', 'running', 'needs_input'
                        ) THEN created_at END
                    ) AS oldest_active_at
                FROM room_tasks
                """,
                (now,),
            ).fetchone()
            task_input_row = conn.execute(
                """
                SELECT
                    SUM(
                        CASE WHEN input.first_delivered_at IS NULL
                             THEN 1 ELSE 0 END
                    ) AS undelivered_count,
                    SUM(
                        CASE WHEN input.first_delivered_at IS NOT NULL
                                  AND input.applied_at IS NULL
                             THEN 1 ELSE 0 END
                    ) AS unapplied_count
                FROM room_task_inputs AS input
                JOIN room_tasks AS task ON task.task_id = input.task_id
                WHERE task.status IN ('queued', 'claimed', 'running', 'needs_input')
                """
            ).fetchone()
            room_task_rows = conn.execute(
                """
                SELECT conversation_id,
                       SUM(
                           CASE WHEN status IN (
                               'queued', 'claimed', 'running', 'needs_input'
                           ) THEN 1 ELSE 0 END
                       ) AS active_task_count,
                       SUM(CASE WHEN status = 'needs_input' THEN 1 ELSE 0 END)
                           AS needs_input_count,
                       SUM(
                           CASE WHEN status IN ('claimed', 'running')
                                      AND lease_expires_at IS NOT NULL
                                      AND lease_expires_at <= ?
                                THEN 1 ELSE 0 END
                       ) AS expired_lease_count
                FROM room_tasks
                GROUP BY conversation_id
                """,
                (now,),
            ).fetchall()
        readiness: dict[str, dict[str, dict[str, Any]]] = {}
        for component in readiness_rows:
            readiness.setdefault(str(component["connector_id"]), {})[
                str(component["component"])
            ] = {
                "protocol_version": int(component["protocol_version"]),
                "first_seen_at": float(component["first_seen_at"]),
                "last_seen_at": float(component["last_seen_at"]),
            }
        component_activity: dict[str, dict[str, dict[str, Any]]] = {}
        for component in component_activity_rows:
            component_activity.setdefault(str(component["connector_id"]), {})[
                str(component["component"])
            ] = {
                "last_seen_at": float(component["last_seen_at"]),
                "active_session_count": int(
                    component["active_session_count"] or 0
                ),
            }

        def diagnostic_detail(raw_value: object) -> str | None:
            try:
                parsed = json.loads(str(raw_value or "{}"))
            except (TypeError, json.JSONDecodeError):
                return None
            if not isinstance(parsed, dict):
                return None
            for key in ("error", "message", "reason", "detail"):
                value = parsed.get(key)
                if isinstance(value, (str, int, float)) and str(value).strip():
                    return str(value).strip()[:500]
            return None

        connectors: list[dict[str, Any]] = []
        for row in rows:
            connector_id = str(row["connector_id"])
            ready = sorted(readiness.get(connector_id, {}))
            required = sorted(self._connector_required_components(row))
            last_seen = (
                float(row["connector_last_seen_at"])
                if row["connector_last_seen_at"] is not None
                else None
            )
            setup_status = str(row["setup_status"])
            online = bool(
                setup_status == "configured"
                and last_seen is not None
                and now - last_seen <= CONNECTOR_ONLINE_WINDOW_SECONDS
            )
            missing_components = sorted(set(required) - set(ready))
            required_pending_count = int(row["required_pending_count"] or 0)
            oldest_required_at = (
                float(row["oldest_required_at"])
                if row["oldest_required_at"] is not None
                else None
            )
            required_age = (
                max(0.0, now - oldest_required_at)
                if oldest_required_at is not None
                else None
            )
            native_last_seen = (
                float(row["tui_last_seen_at"])
                if row["tui_last_seen_at"] is not None
                else None
            )
            native_state = str(row["tui_state"] or "unbound")
            effective_native_state = native_state
            if (
                native_state in {"online", "busy"}
                and native_last_seen is not None
                and now - native_last_seen > CONNECTOR_ONLINE_WINDOW_SECONDS
            ):
                effective_native_state = "offline"
            issues: list[dict[str, str]] = []

            def add_issue(code: str, severity: str, label: str) -> None:
                issues.append({"code": code, "severity": severity, "label": label})

            if setup_status == "failed":
                add_issue("setup_failed", "error", "值守配置失败")
            elif setup_status == "awaiting_setup":
                add_issue("awaiting_setup", "info", "等待完成值守配置")
            elif setup_status == "configured" and not online:
                add_issue("listener_offline", "error", "listener 超过 75 秒未探活")
            if (
                setup_status == "configured"
                and int(row["binding_version"] or 1) >= 2
                and missing_components
            ):
                add_issue(
                    "missing_components",
                    "warning",
                    "缺少组件登记：" + "、".join(missing_components),
                )
            elif setup_status == "configured" and missing_components:
                add_issue(
                    "legacy_binding",
                    "info",
                    "旧版连接会在组件自然重连后补齐登记",
                )
            if setup_status == "configured" and int(row["active_session_count"] or 0) == 0:
                add_issue("session_unavailable", "warning", "没有有效 Agent 会话")
            if row["tui_adapter_kind"] is not None:
                if effective_native_state == "error":
                    add_issue("native_tui_error", "error", "真实 TUI 报告异常")
                elif effective_native_state == "offline":
                    add_issue("native_tui_offline", "error", "真实 TUI 当前不可达")
                elif effective_native_state in {
                    "unbound",
                    "awaiting_confirmation",
                    "waiting_approval",
                }:
                    add_issue("native_tui_pending", "info", "等待真实 TUI 确认")
            if (
                required_age is not None
                and required_age >= REQUIRED_REPLY_DELAY_WARNING_SECONDS
            ):
                add_issue(
                    "required_reply_delayed",
                    "warning",
                    "必须回复已等待超过 5 分钟",
                )
            if int(row["expired_task_lease_count"] or 0) > 0:
                add_issue("task_lease_expired", "warning", "存在已过期任务租约")
            if row["enrollment_rotation_required_at"] is not None:
                add_issue(
                    "credential_rotation_required",
                    "warning",
                    "设备凭证等待自动轮换",
                )

            issue_codes = {item["code"] for item in issues}
            if setup_status == "failed" or "native_tui_error" in issue_codes:
                health_state = "failed"
            elif issue_codes & {"listener_offline", "native_tui_offline"}:
                health_state = "offline"
            elif setup_status == "manual":
                health_state = "manual"
            elif setup_status == "awaiting_setup":
                health_state = "setup"
            elif any(
                item["severity"] in {"warning", "error"} for item in issues
            ):
                health_state = "degraded"
            else:
                health_state = "healthy"
            pending_count = int(row["pending_count"] or 0)
            connectors.append(
                {
                    "connector_id": connector_id,
                    "conversation_id": str(row["conversation_id"]),
                    "participant_id": str(row["accepted_participant_id"]),
                    "client_type": str(row["client_type"]),
                    "display_name": str(row["display_name"]),
                    "product": str(row["product"]),
                    "adapter_kind": str(row["adapter_kind"]),
                    "tui_adapter_kind": (
                        str(row["tui_adapter_kind"])
                        if row["tui_adapter_kind"] is not None
                        else None
                    ),
                    "effective_adapter_kind": str(
                        row["tui_adapter_kind"] or row["adapter_kind"]
                    ),
                    "setup_status": setup_status,
                    "diagnostic_detail": (
                        diagnostic_detail(row["setup_detail_json"])
                        if setup_status == "failed"
                        else diagnostic_detail(row["tui_detail_json"])
                        if "native_tui_error" in issue_codes
                        else None
                    ),
                    "online": online,
                    "health_state": health_state,
                    "issues": issues,
                    "connector_last_seen_at": last_seen,
                    "connector_last_seen_age_seconds": (
                        max(0.0, now - last_seen) if last_seen is not None else None
                    ),
                    "session_last_seen_at": (
                        float(row["session_last_seen_at"])
                        if row["session_last_seen_at"] is not None
                        else None
                    ),
                    "active_session_last_seen_at": (
                        float(row["active_session_last_seen_at"])
                        if row["active_session_last_seen_at"] is not None
                        else None
                    ),
                    "active_session_count": int(row["active_session_count"] or 0),
                    "last_reply_at": (
                        float(row["last_reply_at"])
                        if row["last_reply_at"] is not None
                        else None
                    ),
                    "pending_count": pending_count,
                    "optional_pending_count": max(
                        0,
                        pending_count - required_pending_count,
                    ),
                    "required_pending_count": required_pending_count,
                    "oldest_pending_at": (
                        float(row["oldest_pending_at"])
                        if row["oldest_pending_at"] is not None
                        else None
                    ),
                    "oldest_required_at": oldest_required_at,
                    "oldest_required_age_seconds": required_age,
                    "active_task_count": int(row["active_task_count"] or 0),
                    "expired_task_lease_count": int(
                        row["expired_task_lease_count"] or 0
                    ),
                    "binding_version": int(row["binding_version"] or 1),
                    "enrollment": {
                        "credential_version": int(
                            row["enrollment_credential_version"] or 1
                        ),
                        "rotation_count": int(
                            row["enrollment_rotation_count"] or 0
                        ),
                        "last_used_at": (
                            float(row["enrollment_last_used_at"])
                            if row["enrollment_last_used_at"] is not None
                            else None
                        ),
                        "rotated_at": (
                            float(row["enrollment_rotated_at"])
                            if row["enrollment_rotated_at"] is not None
                            else None
                        ),
                        "credential_age_seconds": max(
                            0.0,
                            now
                            - float(
                                row["enrollment_rotated_at"]
                                or row["created_at"]
                            ),
                        ),
                        "rotation_required": (
                            row["enrollment_rotation_required_at"] is not None
                        ),
                        "rotation_required_at": (
                            float(row["enrollment_rotation_required_at"])
                            if row["enrollment_rotation_required_at"] is not None
                            else None
                        ),
                        "previous_valid_until": (
                            float(row["previous_enrollment_valid_until"])
                            if row["previous_enrollment_valid_until"] is not None
                            else None
                        ),
                    },
                    "ready_components": ready,
                    "missing_components": missing_components,
                    "component_registration": readiness.get(connector_id, {}),
                    "component_activity": component_activity.get(connector_id, {}),
                    "native_tui": {
                        "endpoint_id": (
                            str(row["tui_endpoint_id"])
                            if row["tui_endpoint_id"] is not None
                            else None
                        ),
                        "native_session_id": (
                            str(row["tui_native_session_id"])
                            if row["tui_native_session_id"] is not None
                            else None
                        ),
                        "state": native_state,
                        "effective_state": effective_native_state,
                        "last_seen_at": native_last_seen,
                        "last_seen_age_seconds": (
                            max(0.0, now - native_last_seen)
                            if native_last_seen is not None
                            else None
                        ),
                        "active_task_id": (
                            str(row["tui_active_task_id"])
                            if row["tui_active_task_id"] is not None
                            else None
                        ),
                    },
                }
            )
        status_counts = {
            status: sum(item["health_state"] == status for item in connectors)
            for status in (
                "healthy",
                "degraded",
                "offline",
                "failed",
                "setup",
                "manual",
            )
        }
        room_health: dict[str, dict[str, Any]] = {}
        for row in room_backlog_rows:
            pending = int(row["pending_count"] or 0)
            required_pending = int(row["required_pending_count"] or 0)
            room_health[str(row["conversation_id"])] = {
                "conversation_id": str(row["conversation_id"]),
                "pending_count": pending,
                "required_pending_count": required_pending,
                "optional_pending_count": max(0, pending - required_pending),
                "oldest_pending_at": (
                    float(row["oldest_pending_at"])
                    if row["oldest_pending_at"] is not None
                    else None
                ),
                "active_task_count": 0,
                "needs_input_count": 0,
                "expired_lease_count": 0,
            }
        for row in room_task_rows:
            conversation = str(row["conversation_id"])
            payload = room_health.setdefault(
                conversation,
                {
                    "conversation_id": conversation,
                    "pending_count": 0,
                    "required_pending_count": 0,
                    "optional_pending_count": 0,
                    "oldest_pending_at": None,
                    "active_task_count": 0,
                    "needs_input_count": 0,
                    "expired_lease_count": 0,
                },
            )
            payload.update(
                {
                    "active_task_count": int(row["active_task_count"] or 0),
                    "needs_input_count": int(row["needs_input_count"] or 0),
                    "expired_lease_count": int(row["expired_lease_count"] or 0),
                }
            )
        pending_total = int(backlog_row["pending_count"] or 0)
        required_total = int(backlog_row["required_pending_count"] or 0)
        return {
            "connectors": connectors,
            "count": len(connectors),
            "online_count": sum(item["online"] for item in connectors),
            "attention_count": sum(
                item["health_state"] in {"degraded", "offline", "failed", "setup"}
                for item in connectors
            ),
            "status_counts": status_counts,
            "binding_v2_count": sum(
                item["binding_version"] >= 2 for item in connectors
            ),
            "backlog": {
                "pending_count": pending_total,
                "required_pending_count": required_total,
                "optional_pending_count": max(0, pending_total - required_total),
                "oldest_pending_at": (
                    float(backlog_row["oldest_pending_at"])
                    if backlog_row["oldest_pending_at"] is not None
                    else None
                ),
                "oldest_required_at": (
                    float(backlog_row["oldest_required_at"])
                    if backlog_row["oldest_required_at"] is not None
                    else None
                ),
            },
            "tasks": {
                "active_count": int(task_row["active_count"] or 0),
                "queued_count": int(task_row["queued_count"] or 0),
                "claimed_count": int(task_row["claimed_count"] or 0),
                "running_count": int(task_row["running_count"] or 0),
                "needs_input_count": int(task_row["needs_input_count"] or 0),
                "expired_lease_count": int(task_row["expired_lease_count"] or 0),
                "oldest_active_at": (
                    float(task_row["oldest_active_at"])
                    if task_row["oldest_active_at"] is not None
                    else None
                ),
                "undelivered_input_count": int(
                    task_input_row["undelivered_count"] or 0
                ),
                "unapplied_input_count": int(
                    task_input_row["unapplied_count"] or 0
                ),
            },
            "rooms": sorted(
                room_health.values(),
                key=lambda item: (
                    -int(item["required_pending_count"]),
                    -int(item["expired_lease_count"]),
                    -int(item["active_task_count"]),
                    -int(item["pending_count"]),
                    str(item["conversation_id"]),
                ),
            ),
            "required_reply_warning_seconds": (
                REQUIRED_REPLY_DELAY_WARNING_SECONDS
            ),
            "diagnostic_scope": (
                "Central Bridge state only. Remote supervisor queues and local "
                "model process errors require that machine's supervisor status/logs."
            ),
            "server_time": now,
        }

    @staticmethod
    def _monitoring_sample_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            key: (
                int(row[key])
                if key
                in {
                    "sample_minute",
                    "connector_count",
                    "connector_online_count",
                    "connector_offline_count",
                    "connector_failed_count",
                    "connector_attention_count",
                    "pending_delivery_count",
                    "required_pending_count",
                    "delayed_required_count",
                    "task_backlog_count",
                    "task_queued_count",
                    "task_running_count",
                    "task_needs_input_count",
                    "task_needs_input_delayed_count",
                    "task_expired_lease_count",
                    "task_terminal_count_1h",
                    "task_failed_count_1h",
                    "reply_sample_count_1h",
                }
                else float(row[key]) if row[key] is not None else None
            )
            for key in row.keys()
        }

    @staticmethod
    def _monitoring_alert_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "alert_id": str(row["alert_id"]),
            "alert_key": str(row["alert_key"]),
            "category": str(row["category"]),
            "severity": str(row["severity"]),
            "status": str(row["status"]),
            "title": str(row["title"]),
            "detail": str(row["detail"]),
            "current_value": float(row["current_value"]),
            "threshold_value": float(row["threshold_value"]),
            "first_seen_at": float(row["first_seen_at"]),
            "last_seen_at": float(row["last_seen_at"]),
            "resolved_at": (
                float(row["resolved_at"])
                if row["resolved_at"] is not None
                else None
            ),
            "occurrence_count": int(row["occurrence_count"]),
            "acknowledged_at": (
                float(row["acknowledged_at"])
                if row["acknowledged_at"] is not None
                else None
            ),
            "acknowledged_by_web_user_id": (
                str(row["acknowledged_by_web_user_id"])
                if row["acknowledged_by_web_user_id"] is not None
                else None
            ),
            "acknowledged_by_username": str(
                row["acknowledged_by_username"] or ""
            ),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    def record_operational_sample(self) -> dict[str, Any]:
        """Persist one deduplicated minute of operational evidence.

        Sampling uses separate WAL readers and one short write transaction. It
        never changes messages, deliveries, tasks, sessions, or connector state.
        The unique minute bucket also makes concurrent viewer samplers harmless.
        """

        captured_at = time.time()
        sample_minute = int(captured_at // MONITORING_SAMPLE_INTERVAL_SECONDS) * int(
            MONITORING_SAMPLE_INTERVAL_SECONDS
        )
        with self._connection() as conn:
            administrator = conn.execute(
                "SELECT user_id FROM web_users "
                "WHERE role = 'admin' AND active = 1 "
                "ORDER BY created_at LIMIT 1"
            ).fetchone()
        if administrator is None:
            raise AuthenticationError("an active administrator is required")
        health = self.admin_connector_health(
            requesting_web_user_id=str(administrator["user_id"]),
        )

        window_start = captured_at - 60 * 60
        needs_input_cutoff = (
            captured_at - MONITORING_TASK_NEEDS_INPUT_WARNING_SECONDS
        )
        with self._connection() as conn:
            latency_rows = conn.execute(
                """
                SELECT MIN(MAX(0, reply.created_at - source.created_at)) AS latency
                FROM message_deliveries AS delivery
                JOIN messages AS source
                  ON source.message_id = delivery.message_id
                JOIN messages AS reply
                  ON reply.reply_to = source.message_id
                 AND reply.sender_participant_id = delivery.participant_id
                WHERE source.created_at >= ?
                  AND instr(delivery.reasons_json, '"quiet_optional"') = 0
                  AND (
                      instr(delivery.reasons_json, '"mention"') > 0
                      OR instr(delivery.reasons_json, '"agent_request"') > 0
                  )
                GROUP BY source.message_id, delivery.participant_id
                """,
                (window_start,),
            ).fetchall()
            task_rates = conn.execute(
                """
                SELECT
                    SUM(
                        CASE WHEN status IN ('completed', 'failed')
                                  AND updated_at >= ?
                             THEN 1 ELSE 0 END
                    ) AS terminal_count,
                    SUM(
                        CASE WHEN status = 'failed' AND updated_at >= ?
                             THEN 1 ELSE 0 END
                    ) AS failed_count,
                    SUM(
                        CASE WHEN status = 'needs_input' AND updated_at <= ?
                             THEN 1 ELSE 0 END
                    ) AS delayed_needs_input_count
                FROM room_tasks
                """,
                (window_start, window_start, needs_input_cutoff),
            ).fetchone()

        latencies = sorted(
            float(row["latency"])
            for row in latency_rows
            if row["latency"] is not None
        )
        latency_average = (
            sum(latencies) / len(latencies) if latencies else None
        )
        latency_p95 = (
            latencies[max(0, math.ceil(len(latencies) * 0.95) - 1)]
            if latencies
            else None
        )
        terminal_count = int(task_rates["terminal_count"] or 0)
        failed_count = int(task_rates["failed_count"] or 0)
        failure_rate = failed_count / terminal_count if terminal_count else 0.0
        status_counts = health.get("status_counts", {})
        task_health = health.get("tasks", {})
        backlog = health.get("backlog", {})
        delayed_required = sum(
            any(
                issue.get("code") == "required_reply_delayed"
                for issue in connector.get("issues", [])
            )
            for connector in health.get("connectors", [])
        )
        sample: dict[str, Any] = {
            "sample_minute": sample_minute,
            "captured_at": captured_at,
            "connector_count": int(health.get("count", 0)),
            "connector_online_count": int(health.get("online_count", 0)),
            "connector_offline_count": int(status_counts.get("offline", 0)),
            "connector_failed_count": int(status_counts.get("failed", 0)),
            "connector_attention_count": int(health.get("attention_count", 0)),
            "pending_delivery_count": int(backlog.get("pending_count", 0)),
            "required_pending_count": int(
                backlog.get("required_pending_count", 0)
            ),
            "delayed_required_count": int(delayed_required),
            "task_backlog_count": int(task_health.get("active_count", 0)),
            "task_queued_count": int(task_health.get("queued_count", 0)),
            "task_running_count": int(task_health.get("running_count", 0)),
            "task_needs_input_count": int(
                task_health.get("needs_input_count", 0)
            ),
            "task_needs_input_delayed_count": int(
                task_rates["delayed_needs_input_count"] or 0
            ),
            "task_expired_lease_count": int(
                task_health.get("expired_lease_count", 0)
            ),
            "task_terminal_count_1h": terminal_count,
            "task_failed_count_1h": failed_count,
            "task_failure_rate_1h": failure_rate,
            "reply_sample_count_1h": len(latencies),
            "reply_latency_average_seconds": latency_average,
            "reply_latency_p95_seconds": latency_p95,
        }

        unavailable_count = (
            sample["connector_offline_count"] + sample["connector_failed_count"]
        )
        alert_specs = (
            {
                "key": "connector-unavailable",
                "active": unavailable_count > 0,
                "category": "connector",
                "severity": (
                    "critical" if sample["connector_failed_count"] else "warning"
                ),
                "title": "自动值守连接不可用",
                "detail": (
                    f"{sample['connector_offline_count']} 个离线，"
                    f"{sample['connector_failed_count']} 个异常。"
                ),
                "value": float(unavailable_count),
                "threshold": 0.0,
            },
            {
                "key": "required-reply-delayed",
                "active": sample["delayed_required_count"] > 0,
                "category": "reply",
                "severity": "warning",
                "title": "必须回复等待过久",
                "detail": "存在超过 5 分钟仍未完成的个人艾特或明确请求。",
                "value": float(sample["delayed_required_count"]),
                "threshold": 0.0,
            },
            {
                "key": "task-lease-expired",
                "active": sample["task_expired_lease_count"] > 0,
                "category": "task",
                "severity": "critical",
                "title": "任务租约已经过期",
                "detail": "任务仍处于领取或运行状态，但执行租约已过期。",
                "value": float(sample["task_expired_lease_count"]),
                "threshold": 0.0,
            },
            {
                "key": "task-needs-input-delayed",
                "active": sample["task_needs_input_delayed_count"] > 0,
                "category": "task",
                "severity": "warning",
                "title": "任务等待输入超过 30 分钟",
                "detail": "需要用户或协作者补充信息的任务长时间没有继续。",
                "value": float(sample["task_needs_input_delayed_count"]),
                "threshold": 0.0,
            },
            {
                "key": "task-failure-rate-high",
                "active": (
                    terminal_count >= MONITORING_MIN_RATE_SAMPLE_COUNT
                    and failure_rate >= MONITORING_TASK_FAILURE_RATE_WARNING
                ),
                "category": "task",
                "severity": "warning",
                "title": "最近一小时任务失败率偏高",
                "detail": (
                    f"最近一小时 {terminal_count} 个终态任务中 "
                    f"{failed_count} 个失败。"
                ),
                "value": float(failure_rate),
                "threshold": MONITORING_TASK_FAILURE_RATE_WARNING,
            },
            {
                "key": "reply-latency-high",
                "active": (
                    len(latencies) >= MONITORING_MIN_RATE_SAMPLE_COUNT
                    and latency_p95 is not None
                    and latency_p95 >= MONITORING_REPLY_LATENCY_WARNING_SECONDS
                ),
                "category": "reply",
                "severity": "warning",
                "title": "最近一小时回复延迟偏高",
                "detail": (
                    f"个人艾特/明确请求的 P95 回复延迟为 "
                    f"{int(latency_p95 or 0)} 秒。"
                ),
                "value": float(latency_p95 or 0.0),
                "threshold": float(MONITORING_REPLY_LATENCY_WARNING_SECONDS),
            },
        )

        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO operational_metric_samples (
                    sample_minute, captured_at,
                    connector_count, connector_online_count,
                    connector_offline_count, connector_failed_count,
                    connector_attention_count, pending_delivery_count,
                    required_pending_count, delayed_required_count,
                    task_backlog_count, task_queued_count, task_running_count,
                    task_needs_input_count, task_needs_input_delayed_count,
                    task_expired_lease_count, task_terminal_count_1h,
                    task_failed_count_1h, task_failure_rate_1h,
                    reply_sample_count_1h, reply_latency_average_seconds,
                    reply_latency_p95_seconds
                ) VALUES (
                    :sample_minute, :captured_at,
                    :connector_count, :connector_online_count,
                    :connector_offline_count, :connector_failed_count,
                    :connector_attention_count, :pending_delivery_count,
                    :required_pending_count, :delayed_required_count,
                    :task_backlog_count, :task_queued_count, :task_running_count,
                    :task_needs_input_count, :task_needs_input_delayed_count,
                    :task_expired_lease_count, :task_terminal_count_1h,
                    :task_failed_count_1h, :task_failure_rate_1h,
                    :reply_sample_count_1h, :reply_latency_average_seconds,
                    :reply_latency_p95_seconds
                )
                ON CONFLICT(sample_minute) DO UPDATE SET
                    captured_at = excluded.captured_at,
                    connector_count = excluded.connector_count,
                    connector_online_count = excluded.connector_online_count,
                    connector_offline_count = excluded.connector_offline_count,
                    connector_failed_count = excluded.connector_failed_count,
                    connector_attention_count = excluded.connector_attention_count,
                    pending_delivery_count = excluded.pending_delivery_count,
                    required_pending_count = excluded.required_pending_count,
                    delayed_required_count = excluded.delayed_required_count,
                    task_backlog_count = excluded.task_backlog_count,
                    task_queued_count = excluded.task_queued_count,
                    task_running_count = excluded.task_running_count,
                    task_needs_input_count = excluded.task_needs_input_count,
                    task_needs_input_delayed_count =
                        excluded.task_needs_input_delayed_count,
                    task_expired_lease_count = excluded.task_expired_lease_count,
                    task_terminal_count_1h = excluded.task_terminal_count_1h,
                    task_failed_count_1h = excluded.task_failed_count_1h,
                    task_failure_rate_1h = excluded.task_failure_rate_1h,
                    reply_sample_count_1h = excluded.reply_sample_count_1h,
                    reply_latency_average_seconds =
                        excluded.reply_latency_average_seconds,
                    reply_latency_p95_seconds = excluded.reply_latency_p95_seconds
                """,
                sample,
            )
            for spec in alert_specs:
                existing = conn.execute(
                    "SELECT * FROM operational_alerts WHERE alert_key = ?",
                    (spec["key"],),
                ).fetchone()
                if spec["active"]:
                    if existing is None:
                        conn.execute(
                            """
                            INSERT INTO operational_alerts (
                                alert_id, alert_key, category, severity, status,
                                title, detail, current_value, threshold_value,
                                first_seen_at, last_seen_at, occurrence_count,
                                last_sample_minute, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                            """,
                            (
                                f"opalert_{uuid.uuid4().hex}",
                                spec["key"],
                                spec["category"],
                                spec["severity"],
                                spec["title"],
                                spec["detail"],
                                spec["value"],
                                spec["threshold"],
                                captured_at,
                                captured_at,
                                sample_minute,
                                captured_at,
                                captured_at,
                            ),
                        )
                    elif str(existing["status"]) == "resolved":
                        conn.execute(
                            """
                            UPDATE operational_alerts
                            SET category = ?, severity = ?, status = 'open',
                                title = ?, detail = ?, current_value = ?,
                                threshold_value = ?, first_seen_at = ?,
                                last_seen_at = ?, resolved_at = NULL,
                                occurrence_count = 1, last_sample_minute = ?,
                                acknowledged_at = NULL,
                                acknowledged_by_web_user_id = NULL,
                                updated_at = ?
                            WHERE alert_id = ?
                            """,
                            (
                                spec["category"],
                                spec["severity"],
                                spec["title"],
                                spec["detail"],
                                spec["value"],
                                spec["threshold"],
                                captured_at,
                                captured_at,
                                sample_minute,
                                captured_at,
                                str(existing["alert_id"]),
                            ),
                        )
                    else:
                        occurrence_increment = int(
                            int(existing["last_sample_minute"]) != sample_minute
                        )
                        conn.execute(
                            """
                            UPDATE operational_alerts
                            SET category = ?, severity = ?, title = ?, detail = ?,
                                current_value = ?, threshold_value = ?,
                                last_seen_at = ?, occurrence_count =
                                    occurrence_count + ?,
                                last_sample_minute = ?, updated_at = ?
                            WHERE alert_id = ?
                            """,
                            (
                                spec["category"],
                                spec["severity"],
                                spec["title"],
                                spec["detail"],
                                spec["value"],
                                spec["threshold"],
                                captured_at,
                                occurrence_increment,
                                sample_minute,
                                captured_at,
                                str(existing["alert_id"]),
                            ),
                        )
                elif existing is not None and str(existing["status"]) == "open":
                    conn.execute(
                        """
                        UPDATE operational_alerts
                        SET status = 'resolved', resolved_at = ?, updated_at = ?
                        WHERE alert_id = ?
                        """,
                        (captured_at, captured_at, str(existing["alert_id"])),
                    )
            conn.execute(
                "DELETE FROM operational_metric_samples WHERE captured_at < ?",
                (captured_at - MONITORING_RETENTION_SECONDS,),
            )
            conn.execute(
                """
                UPDATE operational_monitoring_state
                SET revision = revision + 1, last_sample_at = ?, updated_at = ?
                WHERE singleton = 1
                """,
                (captured_at, captured_at),
            )
            row = conn.execute(
                "SELECT * FROM operational_metric_samples WHERE sample_minute = ?",
                (sample_minute,),
            ).fetchone()
        return self._monitoring_sample_payload(row)

    def operational_monitoring_dashboard(
        self,
        *,
        requesting_web_user_id: str,
        hours: object = 24,
    ) -> dict[str, Any]:
        requester = opaque_id(
            requesting_web_user_id,
            field="requesting_web_user_id",
        )
        if isinstance(hours, bool):
            raise ValidationError("monitoring hours must be an integer")
        try:
            normalized_hours = int(hours)
        except (TypeError, ValueError) as exc:
            raise ValidationError("monitoring hours must be an integer") from exc
        if not 1 <= normalized_hours <= 30 * 24:
            raise ValidationError("monitoring hours must be between 1 and 720")
        cutoff = time.time() - normalized_hours * 60 * 60
        with self._connection() as conn:
            self._require_active_admin_locked(conn, requester)
            rows = conn.execute(
                "SELECT * FROM operational_metric_samples "
                "WHERE captured_at >= ? ORDER BY captured_at",
                (cutoff,),
            ).fetchall()
            alert_rows = conn.execute(
                """
                SELECT alert.*, acknowledger.username
                    AS acknowledged_by_username
                FROM operational_alerts AS alert
                LEFT JOIN web_users AS acknowledger
                  ON acknowledger.user_id = alert.acknowledged_by_web_user_id
                WHERE alert.status = 'open' OR alert.updated_at >= ?
                ORDER BY CASE alert.status WHEN 'open' THEN 0 ELSE 1 END,
                         CASE alert.severity WHEN 'critical' THEN 0 ELSE 1 END,
                         alert.updated_at DESC
                LIMIT 100
                """,
                (cutoff,),
            ).fetchall()
            state = conn.execute(
                "SELECT * FROM operational_monitoring_state WHERE singleton = 1"
            ).fetchone()
        samples = [self._monitoring_sample_payload(row) for row in rows]
        if len(samples) > 360:
            stride = math.ceil(len(samples) / 359)
            compacted = samples[::stride]
            if compacted[-1]["sample_minute"] != samples[-1]["sample_minute"]:
                compacted.append(samples[-1])
            samples = compacted
        alerts = [self._monitoring_alert_payload(row) for row in alert_rows]
        latest = samples[-1] if samples else None

        def maximum(key: str) -> float:
            values = [
                float(sample[key])
                for sample in samples
                if sample.get(key) is not None
            ]
            return max(values, default=0.0)

        return {
            "hours": normalized_hours,
            "samples": samples,
            "sample_count": len(rows),
            "latest": latest,
            "alerts": alerts,
            "open_alert_count": sum(
                alert["status"] == "open" for alert in alerts
            ),
            "unacknowledged_open_alert_count": sum(
                alert["status"] == "open"
                and alert["acknowledged_at"] is None
                for alert in alerts
            ),
            "summary": {
                "max_offline_connectors": max(
                    (
                        int(sample["connector_offline_count"])
                        + int(sample["connector_failed_count"])
                        for sample in samples
                    ),
                    default=0,
                ),
                "max_required_pending": int(maximum("required_pending_count")),
                "max_task_backlog": int(maximum("task_backlog_count")),
                "max_reply_latency_p95_seconds": maximum(
                    "reply_latency_p95_seconds"
                ),
                "max_task_failure_rate_1h": maximum("task_failure_rate_1h"),
            },
            "thresholds": {
                "required_reply_delay_seconds": (
                    REQUIRED_REPLY_DELAY_WARNING_SECONDS
                ),
                "reply_latency_p95_seconds": (
                    MONITORING_REPLY_LATENCY_WARNING_SECONDS
                ),
                "task_needs_input_delay_seconds": (
                    MONITORING_TASK_NEEDS_INPUT_WARNING_SECONDS
                ),
                "task_failure_rate": MONITORING_TASK_FAILURE_RATE_WARNING,
                "minimum_rate_sample_count": MONITORING_MIN_RATE_SAMPLE_COUNT,
            },
            "sample_interval_seconds": MONITORING_SAMPLE_INTERVAL_SECONDS,
            "retention_days": int(MONITORING_RETENTION_SECONDS / 86400),
            "revision": int(state["revision"] if state is not None else 0),
            "last_sample_at": (
                float(state["last_sample_at"])
                if state is not None and state["last_sample_at"] is not None
                else None
            ),
            "server_time": time.time(),
        }

    def acknowledge_operational_alert(
        self,
        *,
        alert_id: str,
        acknowledged_by_web_user_id: str,
    ) -> dict[str, Any]:
        normalized_alert_id = opaque_id(alert_id, field="alert_id")
        administrator = opaque_id(
            acknowledged_by_web_user_id,
            field="acknowledged_by_web_user_id",
        )
        now = time.time()
        with self._transaction() as conn:
            self._require_active_admin_locked(conn, administrator)
            existing = conn.execute(
                "SELECT alert_id FROM operational_alerts WHERE alert_id = ?",
                (normalized_alert_id,),
            ).fetchone()
            if existing is None:
                raise NotFoundError("operational alert was not found")
            conn.execute(
                """
                UPDATE operational_alerts
                SET acknowledged_at = COALESCE(acknowledged_at, ?),
                    acknowledged_by_web_user_id =
                        COALESCE(acknowledged_by_web_user_id, ?),
                    updated_at = ?
                WHERE alert_id = ?
                """,
                (now, administrator, now, normalized_alert_id),
            )
            conn.execute(
                "UPDATE operational_monitoring_state "
                "SET revision = revision + 1, updated_at = ? "
                "WHERE singleton = 1",
                (now,),
            )
            row = conn.execute(
                """
                SELECT alert.*, acknowledger.username
                    AS acknowledged_by_username
                FROM operational_alerts AS alert
                LEFT JOIN web_users AS acknowledger
                  ON acknowledger.user_id = alert.acknowledged_by_web_user_id
                WHERE alert.alert_id = ?
                """,
                (normalized_alert_id,),
            ).fetchone()
        return self._monitoring_alert_payload(row)

    @staticmethod
    def _a2a_grant_payload(row: sqlite3.Row, *, now: float) -> dict[str, Any]:
        revoked_at = (
            float(row["revoked_at"])
            if row["revoked_at"] is not None
            else None
        )
        expires_at = float(row["expires_at"])
        return {
            "grant_id": str(row["grant_id"]),
            "conversation_id": str(row["conversation_id"]),
            "participant_id": str(row["participant_id"]),
            "label": str(row["label"]),
            "created_by_web_user_id": str(row["created_by_web_user_id"]),
            "created_at": float(row["created_at"]),
            "expires_at": expires_at,
            "revoked_at": revoked_at,
            "status": (
                "revoked"
                if revoked_at is not None
                else ("expired" if expires_at <= now else "active")
            ),
        }

    def create_a2a_access_grant(
        self,
        *,
        conversation_id: str,
        label: str,
        created_by_web_user_id: str,
        ttl_seconds: object = 30 * 24 * 60 * 60,
    ) -> dict[str, Any]:
        conversation = validate_conversation_id(conversation_id)
        normalized_label = alias(label, field="a2a_label")
        creator = opaque_id(
            created_by_web_user_id,
            field="created_by_web_user_id",
        )
        if isinstance(ttl_seconds, bool):
            raise ValidationError("A2A grant ttl must be a number")
        try:
            ttl = float(ttl_seconds)
        except (TypeError, ValueError) as exc:
            raise ValidationError("A2A grant ttl must be a number") from exc
        if not math.isfinite(ttl) or not 300 <= ttl <= 365 * 24 * 60 * 60:
            raise ValidationError("A2A grant ttl must be 300 seconds to 365 days")
        now = time.time()
        grant_id = f"a2agrant_{uuid.uuid4().hex}"
        participant_id = f"participant_{uuid.uuid4().hex}"
        session_id = f"session_{uuid.uuid4().hex}"
        access_token = f"a2a_{secrets.token_urlsafe(32)}"
        session_secret = f"session_{secrets.token_urlsafe(32)}"
        with self._transaction() as conn:
            self._require_active_admin_locked(conn, creator)
            self._require_active_room(conn, conversation)
            display = f"A2A · {normalized_label} · {grant_id[-6:]}"
            conn.execute("PRAGMA defer_foreign_keys = ON")
            conn.execute(
                """
                INSERT INTO participants
                    (participant_id, client_type, session_alias,
                     display_name, signature, avatar_key, profile_updated_at,
                     capabilities_json, status, created_at, last_seen)
                VALUES (?, ?, ?, ?, ?, 'auto', ?, '[]', 'offline', ?, ?)
                """,
                (
                    participant_id,
                    f"a2a-client-{grant_id[-12:]}",
                    normalized_label,
                    display,
                    "标准 A2A 房间任务入口",
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
                     ttl_seconds, last_seen, component)
                VALUES (?, ?, ?, ?, 'mcp', ?, ?, ?, ?, 'a2a')
                """,
                (
                    session_id,
                    participant_id,
                    conversation,
                    self._secret_hash(session_secret),
                    now,
                    now + ttl,
                    ttl,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO a2a_access_grants
                    (grant_id, token_hash, conversation_id, participant_id,
                     session_id, label, created_by_web_user_id,
                     created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    grant_id,
                    self._secret_hash(access_token),
                    conversation,
                    participant_id,
                    session_id,
                    normalized_label,
                    creator,
                    now,
                    now + ttl,
                ),
            )
            row = conn.execute(
                "SELECT * FROM a2a_access_grants WHERE grant_id = ?",
                (grant_id,),
            ).fetchone()
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise BridgeError("A2A access grant is inconsistent")
        result = self._a2a_grant_payload(row, now=now)
        result["access_token"] = access_token
        return result

    def list_a2a_access_grants(
        self,
        *,
        requesting_web_user_id: str,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        requester = opaque_id(
            requesting_web_user_id,
            field="requesting_web_user_id",
        )
        conversation = (
            validate_conversation_id(conversation_id)
            if conversation_id
            else None
        )
        now = time.time()
        with self._connection() as conn:
            self._require_active_admin_locked(conn, requester)
            rows = conn.execute(
                "SELECT * FROM a2a_access_grants "
                "WHERE (? IS NULL OR conversation_id = ?) "
                "ORDER BY created_at DESC LIMIT 500",
                (conversation, conversation),
            ).fetchall()
        grants = [self._a2a_grant_payload(row, now=now) for row in rows]
        return {"grants": grants, "count": len(grants)}

    def revoke_a2a_access_grant(
        self,
        *,
        grant_id: str,
        revoked_by_web_user_id: str,
    ) -> dict[str, Any]:
        grant = opaque_id(grant_id, field="grant_id")
        reviewer = opaque_id(
            revoked_by_web_user_id,
            field="revoked_by_web_user_id",
        )
        now = time.time()
        with self._transaction() as conn:
            self._require_active_admin_locked(conn, reviewer)
            row = conn.execute(
                "SELECT * FROM a2a_access_grants WHERE grant_id = ?",
                (grant,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"unknown A2A grant: {grant}")
            conn.execute(
                "UPDATE a2a_access_grants SET revoked_at = COALESCE(revoked_at, ?), "
                "revoked_by_web_user_id = COALESCE(revoked_by_web_user_id, ?) "
                "WHERE grant_id = ?",
                (now, reviewer, grant),
            )
            conn.execute(
                "UPDATE agent_sessions SET revoked_at = COALESCE(revoked_at, ?), "
                "revoked_reason = COALESCE(revoked_reason, 'a2a_grant_revoked'), "
                "cleared_at = COALESCE(cleared_at, ?) WHERE session_id = ?",
                (now, now, str(row["session_id"])),
            )
            updated = conn.execute(
                "SELECT * FROM a2a_access_grants WHERE grant_id = ?",
                (grant,),
            ).fetchone()
        return self._a2a_grant_payload(updated, now=now)

    def _require_a2a_grant_locked(
        self,
        conn: sqlite3.Connection,
        *,
        access_token: str,
        now: float,
    ) -> sqlite3.Row:
        normalized = str(access_token or "").strip()
        if not normalized.startswith("a2a_") or len(normalized) < 40:
            raise AuthenticationError("invalid A2A access token")
        row = conn.execute(
            "SELECT * FROM a2a_access_grants WHERE token_hash = ?",
            (self._secret_hash(normalized),),
        ).fetchone()
        if (
            row is None
            or row["revoked_at"] is not None
            or float(row["expires_at"]) <= now
        ):
            raise AuthenticationError("invalid or expired A2A access token")
        return row

    def create_a2a_room_task(
        self,
        *,
        access_token: str,
        body_text: str,
        context_id: str | None = None,
        target_participant_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        normalized_body = body(body_text)
        normalized_context = str(context_id or "").strip()
        if normalized_context and (
            len(normalized_context) > 256
            or any(ord(character) < 32 for character in normalized_context)
        ):
            raise ValidationError("A2A contextId is invalid")
        now = time.time()
        task_id = f"task_{uuid.uuid4().hex}"
        message_id = f"msg_{uuid.uuid4().hex}"
        with self._transaction() as conn:
            grant = self._require_a2a_grant_locked(
                conn,
                access_token=access_token,
                now=now,
            )
            conversation = str(grant["conversation_id"])
            self._require_active_room(conn, conversation)
            target_kind, targets = self._resolve_task_targets_locked(
                conn,
                conversation_id=conversation,
                requested_participant_ids=target_participant_ids,
            )
            conn.execute(
                """
                INSERT INTO messages
                    (message_id, conversation_id, sender_participant_id,
                     audience_kind, audience_value, message_kind, body,
                     refs_json, mentions_json, wake_all_agents, reply_to,
                     status, authorized_session_id, sender_seat,
                     created_at, updated_at)
                VALUES (?, ?, ?, 'room', ?, 'task', ?, '[]', '[]', 0,
                        NULL, 'open', ?, 'a2a', ?, ?)
                """,
                (
                    message_id,
                    conversation,
                    str(grant["participant_id"]),
                    conversation,
                    normalized_body,
                    str(grant["session_id"]),
                    now,
                    now,
                ),
            )
            message = conn.execute(
                "SELECT * FROM messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            sequence = int(message["sequence"])
            conn.execute(
                """
                INSERT INTO room_tasks
                    (task_id, conversation_id, source_message_id,
                     parent_task_id, issuer_web_user_id,
                     issuer_participant_id, target_kind,
                     target_participant_ids_json, body, status,
                     source_sequence, context_start_sequence,
                     context_end_sequence, created_at, updated_at)
                VALUES (?, ?, ?, NULL, NULL, ?, ?, ?, ?, 'queued',
                        ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    conversation,
                    message_id,
                    str(grant["participant_id"]),
                    target_kind,
                    compact_json(targets),
                    normalized_body,
                    sequence,
                    max(1, sequence - 20),
                    sequence,
                    now,
                    now,
                ),
            )
            context = normalized_context or f"ctx_{uuid.uuid4().hex}"
            conn.execute(
                "INSERT INTO a2a_task_links "
                "(task_id, grant_id, context_id, request_message_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (task_id, str(grant["grant_id"]), context, message_id, now),
            )
            self._create_message_deliveries_locked(conn, message)
            task = conn.execute(
                "SELECT * FROM room_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        result = self._task_payload(task)
        result["context_id"] = context
        result["request_message_id"] = message_id
        return result

    def get_a2a_room_task(
        self,
        *,
        access_token: str,
        task_id: str,
    ) -> dict[str, Any]:
        task = opaque_id(task_id, field="task_id")
        now = time.time()
        with self._connection() as conn:
            grant = self._require_a2a_grant_locked(
                conn,
                access_token=access_token,
                now=now,
            )
            row = conn.execute(
                """
                SELECT room_task.*, link.context_id,
                       link.request_message_id
                FROM room_tasks AS room_task
                JOIN a2a_task_links AS link ON link.task_id = room_task.task_id
                WHERE room_task.task_id = ? AND link.grant_id = ?
                """,
                (task, str(grant["grant_id"])),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"unknown A2A task: {task}")
        result = self._task_payload(row)
        result["context_id"] = str(row["context_id"])
        result["request_message_id"] = str(row["request_message_id"])
        return result

    def cancel_a2a_room_task(
        self,
        *,
        access_token: str,
        task_id: str,
    ) -> dict[str, Any]:
        task = opaque_id(task_id, field="task_id")
        now = time.time()
        with self._transaction() as conn:
            grant = self._require_a2a_grant_locked(
                conn,
                access_token=access_token,
                now=now,
            )
            row = conn.execute(
                """
                SELECT room_task.*, link.context_id,
                       link.request_message_id
                FROM room_tasks AS room_task
                JOIN a2a_task_links AS link ON link.task_id = room_task.task_id
                WHERE room_task.task_id = ? AND link.grant_id = ?
                """,
                (task, str(grant["grant_id"])),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"unknown A2A task: {task}")
            if str(row["status"]) in {"completed", "failed"}:
                raise ConflictError("finished A2A task cannot be cancelled")
            conn.execute(
                "UPDATE room_tasks SET status = 'cancelled', completed_at = ?, "
                "lease_expires_at = NULL, updated_at = ? WHERE task_id = ?",
                (now, now, task),
            )
            updated = conn.execute(
                """
                SELECT room_task.*, link.context_id,
                       link.request_message_id
                FROM room_tasks AS room_task
                JOIN a2a_task_links AS link ON link.task_id = room_task.task_id
                WHERE room_task.task_id = ?
                """,
                (task,),
            ).fetchone()
        result = self._task_payload(updated)
        result["context_id"] = str(updated["context_id"])
        result["request_message_id"] = str(updated["request_message_id"])
        return result

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

    def create_web_user_room(
        self,
        *,
        authorized_session_id: str,
        web_user_id: str,
        participant_id: str,
        conversation_id: str,
    ) -> dict[str, Any]:
        """Create a Web-owned room under an authenticated account permission."""

        session_id = opaque_id(
            authorized_session_id,
            field="authorized_session_id",
        )
        user_id = opaque_id(web_user_id, field="web_user_id")
        participant = opaque_id(participant_id, field="participant_id")
        conversation = validate_conversation_id(conversation_id)
        now = time.time()
        with self._transaction() as conn:
            self._archive_stale_rooms_locked(conn, now=now)
            identity = self._require_live_web_session(
                conn,
                session_id=session_id,
                participant_id=participant,
                now=now,
            )
            if str(identity["user_id"]) != user_id:
                raise AuthenticationError("web user session identity does not match")
            is_admin = str(identity["role"]) == "admin"
            can_create = is_admin or bool(identity["can_create_rooms"])
            if not can_create:
                raise AuthorizationError("管理员尚未授予你创建聊天室的权限")
            existing = conn.execute(
                "SELECT status FROM rooms WHERE conversation_id = ?",
                (conversation,),
            ).fetchone()
            if existing is not None:
                raise ConflictError(
                    f"conversation {conversation} already exists with status "
                    f"{existing['status']}"
                )
            owned_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM room_web_owners AS ownership "
                    "JOIN rooms AS room "
                    "ON room.conversation_id = ownership.conversation_id "
                    "WHERE ownership.web_user_id = ? AND room.status = 'active'",
                    (user_id,),
                ).fetchone()[0]
            )
            room_limit = int(identity["room_limit"])
            if not is_admin and owned_count >= room_limit:
                raise ConflictError(
                    "this web user already owns the maximum of "
                    f"{room_limit} active rooms"
                )
            conn.execute(
                "INSERT INTO rooms "
                "(conversation_id, status, creator_kind, creator_participant_id, "
                "created_at, last_activity_at) "
                "VALUES (?, 'active', 'user', NULL, ?, ?)",
                (conversation, now, now),
            )
            conn.execute(
                "INSERT INTO room_web_owners "
                "(conversation_id, web_user_id, created_at) VALUES (?, ?, ?)",
                (conversation, user_id, now),
            )
            conn.execute(
                "INSERT INTO room_web_members "
                "(conversation_id, web_user_id, access_role, active, "
                "invited_by_web_user_id, created_at, updated_at) "
                "VALUES (?, ?, 'member', 1, ?, ?, ?)",
                (conversation, user_id, user_id, now, now),
            )
            self._ensure_web_membership_locked(
                conn,
                conversation_id=conversation,
                participant_id=participant,
                display_name=str(identity["display_name"]),
                signature=str(identity["signature"]),
                role=str(identity["role"]),
                now=now,
            )
            owned_count += 1
        return {
            "conversation_id": conversation,
            "status": "active",
            "creator_kind": "user",
            "owner_web_user_id": user_id,
            "creator_participant_id": participant,
            "created_at": now,
            "last_activity_at": now,
            "owned_active_room_count": owned_count,
            "owned_active_room_limit": None if is_admin else room_limit,
            "is_room_owner": True,
        }

    def web_room_access_scope(
        self,
        *,
        authorized_session_id: str,
        participant_id: str,
    ) -> dict[str, Any]:
        """Return the authoritative Web room scope for one live session."""

        session = opaque_id(authorized_session_id, field="authorized_session_id")
        participant = opaque_id(participant_id, field="participant_id")
        with self._connection() as conn:
            identity = self._require_live_web_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=time.time(),
            )
            is_admin = str(identity["role"]) == "admin"
            if is_admin:
                conversations = None
            else:
                rows = conn.execute(
                    """
                    SELECT conversation_id
                    FROM room_web_owners
                    WHERE web_user_id = ?
                    UNION
                    SELECT conversation_id
                    FROM room_web_members
                    WHERE web_user_id = ? AND active = 1
                    ORDER BY conversation_id
                    """,
                    (str(identity["user_id"]), str(identity["user_id"])),
                ).fetchall()
                conversations = [str(row["conversation_id"]) for row in rows]
        return {
            "web_user_id": str(identity["user_id"]),
            "is_admin": is_admin,
            "conversation_ids": conversations,
        }

    def require_web_room_access(
        self,
        *,
        authorized_session_id: str,
        participant_id: str,
        conversation_id: str,
    ) -> dict[str, Any]:
        session = opaque_id(authorized_session_id, field="authorized_session_id")
        participant = opaque_id(participant_id, field="participant_id")
        conversation = validate_conversation_id(conversation_id)
        with self._connection() as conn:
            identity = self._require_live_web_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=time.time(),
            )
            access = self._require_web_room_access_locked(
                conn,
                web_identity=identity,
                conversation_id=conversation,
            )
        return access

    def room_web_permissions_bulk(
        self,
        *,
        requesting_web_user_id: str,
        conversation_ids: Sequence[str],
    ) -> dict[str, dict[str, Any]]:
        requester = opaque_id(
            requesting_web_user_id,
            field="requesting_web_user_id",
        )
        conversations = list(
            dict.fromkeys(
                validate_conversation_id(value) for value in conversation_ids
            )
        )
        if not conversations:
            return {}
        with self._connection() as conn:
            web_user = conn.execute(
                "SELECT role FROM web_users WHERE user_id = ? AND active = 1",
                (requester,),
            ).fetchone()
            if web_user is None:
                raise AuthenticationError("active Web user is required")
            placeholders = ",".join("?" for _ in conversations)
            rows = conn.execute(
                f"""
                SELECT room.conversation_id, room.status,
                       CASE WHEN owner.web_user_id IS NOT NULL THEN 1 ELSE 0 END
                           AS is_room_owner,
                       access.access_role
                FROM rooms AS room
                LEFT JOIN room_web_owners AS owner
                  ON owner.conversation_id = room.conversation_id
                 AND owner.web_user_id = ?
                LEFT JOIN room_web_members AS access
                  ON access.conversation_id = room.conversation_id
                 AND access.web_user_id = ?
                 AND access.active = 1
                WHERE room.conversation_id IN ({placeholders})
                """,
                (requester, requester, *conversations),
            ).fetchall()
            by_conversation = {
                str(row["conversation_id"]): row for row in rows
            }
            result: dict[str, dict[str, Any]] = {}
            for conversation in conversations:
                row = by_conversation.get(conversation)
                if row is None:
                    raise NotFoundError(f"unknown conversation: {conversation}")
                result[conversation] = self._room_web_permission_payload(
                    conversation_id=conversation,
                    room_status=str(row["status"]),
                    is_global_admin=str(web_user["role"]) == "admin",
                    is_room_owner=bool(row["is_room_owner"]),
                    access_role=(
                        str(row["access_role"])
                        if row["access_role"] is not None
                        else None
                    ),
                )
            return result

    def search_room_web_users(
        self,
        *,
        requesting_web_user_id: str,
        conversation_id: str,
        query: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        requester = opaque_id(
            requesting_web_user_id,
            field="requesting_web_user_id",
        )
        conversation = validate_conversation_id(conversation_id)
        normalized_query = str(query or "").strip()
        if len(normalized_query) > 64 or any(
            ord(character) < 32 for character in normalized_query
        ):
            raise ValidationError("query must contain at most 64 visible characters")
        normalized_limit = max(1, min(int(limit), 200))
        escaped = (
            normalized_query.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        pattern = f"%{escaped}%"
        with self._connection() as conn:
            manager_permissions = self._room_web_permissions_locked(
                conn,
                web_user_id=requester,
                conversation_id=conversation,
            )
            if not manager_permissions["can_manage_web_members"]:
                raise AuthorizationError("你没有管理这个聊天室成员的权限")
            if conn.execute(
                "SELECT 1 FROM rooms WHERE conversation_id = ?",
                (conversation,),
            ).fetchone() is None:
                raise NotFoundError(f"unknown conversation: {conversation}")
            rows = conn.execute(
                """
                SELECT web_user.user_id, web_user.username,
                       web_user.display_name, web_user.signature,
                       web_user.avatar_key,
                       CASE WHEN owner.web_user_id IS NOT NULL THEN 1 ELSE 0 END
                           AS is_room_owner,
                       COALESCE(access.active, 0) AS room_access_active,
                       COALESCE(access.access_role, 'member') AS access_role,
                       access.updated_at AS access_updated_at
                FROM web_users AS web_user
                LEFT JOIN room_web_owners AS owner
                  ON owner.conversation_id = ?
                 AND owner.web_user_id = web_user.user_id
                LEFT JOIN room_web_members AS access
                  ON access.conversation_id = ?
                 AND access.web_user_id = web_user.user_id
                WHERE web_user.role = 'user' AND web_user.active = 1
                  AND (? = '' OR web_user.username LIKE ? ESCAPE '\\'
                       OR web_user.display_name LIKE ? ESCAPE '\\'
                       OR web_user.signature LIKE ? ESCAPE '\\')
                ORDER BY is_room_owner DESC, room_access_active DESC,
                         web_user.display_name COLLATE NOCASE,
                         web_user.username COLLATE NOCASE
                LIMIT ?
                """,
                (
                    conversation,
                    conversation,
                    normalized_query,
                    pattern,
                    pattern,
                    pattern,
                    normalized_limit,
                ),
            ).fetchall()
        users = [self._room_web_user_payload(row) for row in rows]
        return {
            "conversation_id": conversation,
            "users": users,
            "count": len(users),
            "query": normalized_query,
            "permissions": manager_permissions,
        }

    def manage_room_web_member(
        self,
        *,
        requesting_web_user_id: str,
        conversation_id: str,
        target_web_user_id: str,
        active: bool,
        access_role: str = "member",
    ) -> dict[str, Any]:
        requester = opaque_id(
            requesting_web_user_id,
            field="requesting_web_user_id",
        )
        target = opaque_id(target_web_user_id, field="target_web_user_id")
        conversation = validate_conversation_id(conversation_id)
        if not isinstance(active, bool):
            raise ValidationError("active must be a boolean")
        normalized_role = str(access_role or "member").strip().lower()
        if normalized_role not in {"member", "moderator"}:
            raise ValidationError("access_role must be member or moderator")
        now = time.time()
        with self._transaction() as conn:
            manager_permissions = self._room_web_permissions_locked(
                conn,
                web_user_id=requester,
                conversation_id=conversation,
            )
            if not manager_permissions["can_manage_web_members"]:
                raise AuthorizationError("你没有管理这个聊天室成员的权限")
            room = conn.execute(
                "SELECT status FROM rooms WHERE conversation_id = ?",
                (conversation,),
            ).fetchone()
            if room is None:
                raise NotFoundError(f"unknown conversation: {conversation}")
            target_row = conn.execute(
                "SELECT * FROM web_users "
                "WHERE user_id = ? AND role = 'user' AND active = 1",
                (target,),
            ).fetchone()
            if target_row is None:
                raise NotFoundError("目标普通用户不存在或已停用")
            owner = conn.execute(
                "SELECT 1 FROM room_web_owners "
                "WHERE conversation_id = ? AND web_user_id = ?",
                (conversation, target),
            ).fetchone()
            if not active and owner is not None:
                raise ConflictError("不能移除聊天室创建者")
            current_access = conn.execute(
                "SELECT access_role, active FROM room_web_members "
                "WHERE conversation_id = ? AND web_user_id = ?",
                (conversation, target),
            ).fetchone()
            target_is_moderator = bool(
                current_access is not None
                and current_access["active"]
                and str(current_access["access_role"]) == "moderator"
            )
            if (
                manager_permissions["room_role"] == "moderator"
                and target_is_moderator
            ):
                raise AuthorizationError("聊天室管理员不能修改其他管理员")
            if (
                active
                and normalized_role == "moderator"
                and not manager_permissions["can_delegate_room_moderators"]
            ):
                raise AuthorizationError("只有全局管理员或创建者可以委派管理员")
            if active:
                if str(room["status"]) != "active":
                    raise ConflictError("不能向已废弃的聊天室添加 Web 用户")
                conn.execute(
                    """
                    INSERT INTO room_web_members
                        (conversation_id, web_user_id, access_role, active,
                         invited_by_web_user_id, created_at, updated_at)
                    VALUES (?, ?, ?, 1, ?, ?, ?)
                    ON CONFLICT(conversation_id, web_user_id) DO UPDATE SET
                        access_role = excluded.access_role,
                        active = 1,
                        invited_by_web_user_id = excluded.invited_by_web_user_id,
                        updated_at = excluded.updated_at
                    """,
                    (
                        conversation,
                        target,
                        normalized_role,
                        requester,
                        now,
                        now,
                    ),
                )
                self._ensure_web_membership_locked(
                    conn,
                    conversation_id=conversation,
                    participant_id=str(target_row["participant_id"]),
                    display_name=str(target_row["display_name"]),
                    signature=str(target_row["signature"]),
                    role=str(target_row["role"]),
                    now=now,
                )
            else:
                conn.execute(
                    "UPDATE room_web_members SET active = 0, updated_at = ? "
                    "WHERE conversation_id = ? AND web_user_id = ?",
                    (now, conversation, target),
                )
                conn.execute(
                    "UPDATE memberships SET active = 0, updated_at = ? "
                    "WHERE conversation_id = ? AND participant_id = ?",
                    (now, conversation, str(target_row["participant_id"])),
                )
                conn.execute(
                    "DELETE FROM room_task_grants "
                    "WHERE conversation_id = ? AND web_user_id = ?",
                    (conversation, target),
                )
            row = conn.execute(
                """
                SELECT web_user.user_id, web_user.username,
                       web_user.display_name, web_user.signature,
                       web_user.avatar_key,
                       CASE WHEN owner.web_user_id IS NOT NULL THEN 1 ELSE 0 END
                           AS is_room_owner,
                       COALESCE(access.active, 0) AS room_access_active,
                       COALESCE(access.access_role, 'member') AS access_role,
                       access.updated_at AS access_updated_at
                FROM web_users AS web_user
                LEFT JOIN room_web_owners AS owner
                  ON owner.conversation_id = ?
                 AND owner.web_user_id = web_user.user_id
                LEFT JOIN room_web_members AS access
                  ON access.conversation_id = ?
                 AND access.web_user_id = web_user.user_id
                WHERE web_user.user_id = ?
                """,
                (conversation, conversation, target),
            ).fetchone()
        return self._room_web_user_payload(row)

    def search_web_user_room_permissions(
        self,
        *,
        requesting_web_user_id: str,
        query: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        requester = opaque_id(
            requesting_web_user_id,
            field="requesting_web_user_id",
        )
        normalized_query = str(query or "").strip()
        if len(normalized_query) > 64 or any(
            ord(character) < 32 for character in normalized_query
        ):
            raise ValidationError("query must contain at most 64 visible characters")
        normalized_limit = max(1, min(int(limit), 100))
        escaped = (
            normalized_query.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        pattern = f"%{escaped}%"
        with self._connection() as conn:
            self._require_active_rate_admin_locked(conn, requester)
            rows = conn.execute(
                """
                SELECT web_user.*,
                       COUNT(CASE WHEN room.status = 'active' THEN 1 END)
                           AS owned_active_room_count,
                       COUNT(ownership.conversation_id) AS owned_room_count
                FROM web_users AS web_user
                LEFT JOIN room_web_owners AS ownership
                  ON ownership.web_user_id = web_user.user_id
                LEFT JOIN rooms AS room
                  ON room.conversation_id = ownership.conversation_id
                WHERE web_user.role = 'user' AND web_user.active = 1
                  AND (? = '' OR web_user.username LIKE ? ESCAPE '\\'
                       OR web_user.display_name LIKE ? ESCAPE '\\'
                       OR web_user.signature LIKE ? ESCAPE '\\')
                GROUP BY web_user.user_id
                ORDER BY web_user.display_name COLLATE NOCASE, web_user.username
                LIMIT ?
                """,
                (
                    normalized_query,
                    pattern,
                    pattern,
                    pattern,
                    normalized_limit,
                ),
            ).fetchall()
        users = [self._web_user_room_permission_payload(row) for row in rows]
        return {"users": users, "count": len(users), "query": normalized_query}

    def update_web_user_room_permission(
        self,
        *,
        requesting_web_user_id: str,
        target_web_user_id: str,
        can_create_rooms: bool,
        room_limit: int,
    ) -> dict[str, Any]:
        requester = opaque_id(
            requesting_web_user_id,
            field="requesting_web_user_id",
        )
        target = opaque_id(target_web_user_id, field="target_web_user_id")
        if not isinstance(can_create_rooms, bool):
            raise ValidationError("can_create_rooms must be a boolean")
        if isinstance(room_limit, bool) or not isinstance(room_limit, int):
            raise ValidationError("room_limit must be an integer")
        normalized_limit = room_limit
        if not 1 <= normalized_limit <= MAX_WEB_USER_ROOM_LIMIT:
            raise ValidationError(
                f"room_limit must be between 1 and {MAX_WEB_USER_ROOM_LIMIT}"
            )
        now = time.time()
        with self._transaction() as conn:
            self._require_active_rate_admin_locked(conn, requester)
            row = conn.execute(
                "SELECT * FROM web_users WHERE user_id = ? AND active = 1",
                (target,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"unknown active web user: {target}")
            if str(row["role"]) != "user":
                raise ConflictError("administrator room creation is always enabled")
            conn.execute(
                "UPDATE web_users SET can_create_rooms = ?, room_limit = ?, "
                "updated_at = ? WHERE user_id = ?",
                (1 if can_create_rooms else 0, normalized_limit, now, target),
            )
            updated = conn.execute(
                """
                SELECT web_user.*,
                       COUNT(CASE WHEN room.status = 'active' THEN 1 END)
                           AS owned_active_room_count,
                       COUNT(ownership.conversation_id) AS owned_room_count
                FROM web_users AS web_user
                LEFT JOIN room_web_owners AS ownership
                  ON ownership.web_user_id = web_user.user_id
                LEFT JOIN rooms AS room
                  ON room.conversation_id = ownership.conversation_id
                WHERE web_user.user_id = ?
                GROUP BY web_user.user_id
                """,
                (target,),
            ).fetchone()
        return self._web_user_room_permission_payload(updated)

    def room(self, conversation_id: str) -> dict[str, Any]:
        """Return one room's authoritative identity and lifecycle state."""

        conversation = validate_conversation_id(conversation_id)
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM rooms WHERE conversation_id = ?",
                (conversation,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"unknown conversation: {conversation}")
        return {
            "conversation_id": str(row["conversation_id"]),
            "status": str(row["status"]),
            "creator_kind": str(row["creator_kind"]),
            "created_at": float(row["created_at"]),
            "last_activity_at": float(row["last_activity_at"]),
            "abandoned_at": (
                float(row["abandoned_at"])
                if row["abandoned_at"] is not None
                else None
            ),
        }

    @staticmethod
    def _normalize_room_marker_kind(value: str) -> str:
        normalized = str(value or "").strip().casefold()
        if normalized not in ROOM_MESSAGE_MARKER_KINDS:
            raise ValidationError("marker_kind must be pin or decision")
        return normalized

    @staticmethod
    def _normalize_room_marker_note(value: str | None) -> str:
        normalized = str(value or "").strip()
        if len(normalized) > 2_000:
            raise ValidationError("marker note must contain at most 2000 characters")
        if any(
            ord(character) < 32 and character not in "\t\n\r"
            for character in normalized
        ):
            raise ValidationError("marker note contains invalid control characters")
        return normalized

    def set_room_message_marker(
        self,
        *,
        conversation_id: str,
        message_id: str,
        marker_kind: str,
        note: str | None,
        requesting_web_user_id: str,
    ) -> dict[str, Any]:
        """Pin a room message or retain it as an explicit decision record."""

        conversation = validate_conversation_id(conversation_id)
        message = opaque_id(message_id, field="message_id")
        kind = self._normalize_room_marker_kind(marker_kind)
        normalized_note = self._normalize_room_marker_note(note)
        actor = opaque_id(
            requesting_web_user_id,
            field="requesting_web_user_id",
        )
        now = time.time()
        with self._transaction() as conn:
            permissions = self._room_web_permissions_locked(
                conn,
                web_user_id=actor,
                conversation_id=conversation,
            )
            if not permissions["can_manage_highlights"]:
                raise AuthorizationError(
                    "只有管理员、聊天室创建者或聊天室管理员可以维护房间要点"
                )
            room_message = conn.execute(
                "SELECT message_id FROM messages "
                "WHERE message_id = ? AND conversation_id = ?",
                (message, conversation),
            ).fetchone()
            if room_message is None:
                raise NotFoundError(
                    f"unknown message {message} in conversation {conversation}"
                )
            conn.execute(
                """
                INSERT INTO room_message_markers
                    (conversation_id, message_id, marker_kind, note,
                     created_by_web_user_id, updated_by_web_user_id,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id, message_id, marker_kind) DO UPDATE
                SET note = excluded.note,
                    updated_by_web_user_id = excluded.updated_by_web_user_id,
                    updated_at = excluded.updated_at
                """,
                (
                    conversation,
                    message,
                    kind,
                    normalized_note,
                    actor,
                    actor,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM room_message_markers "
                "WHERE conversation_id = ? AND message_id = ? "
                "AND marker_kind = ?",
                (conversation, message, kind),
            ).fetchone()
        return self._room_message_marker_payload(row)

    def remove_room_message_marker(
        self,
        *,
        conversation_id: str,
        message_id: str,
        marker_kind: str,
        requesting_web_user_id: str,
    ) -> dict[str, Any]:
        conversation = validate_conversation_id(conversation_id)
        message = opaque_id(message_id, field="message_id")
        kind = self._normalize_room_marker_kind(marker_kind)
        actor = opaque_id(
            requesting_web_user_id,
            field="requesting_web_user_id",
        )
        with self._transaction() as conn:
            permissions = self._room_web_permissions_locked(
                conn,
                web_user_id=actor,
                conversation_id=conversation,
            )
            if not permissions["can_manage_highlights"]:
                raise AuthorizationError(
                    "只有管理员、聊天室创建者或聊天室管理员可以维护房间要点"
                )
            row = conn.execute(
                "SELECT * FROM room_message_markers "
                "WHERE conversation_id = ? AND message_id = ? "
                "AND marker_kind = ?",
                (conversation, message, kind),
            ).fetchone()
            if row is None:
                raise NotFoundError("room message marker does not exist")
            conn.execute(
                "DELETE FROM room_message_markers "
                "WHERE conversation_id = ? AND message_id = ? "
                "AND marker_kind = ?",
                (conversation, message, kind),
            )
        payload = self._room_message_marker_payload(row)
        payload["removed"] = True
        return payload

    @staticmethod
    def _room_message_marker_payload(
        row: sqlite3.Row | None,
    ) -> dict[str, Any]:
        if row is None:
            raise NotFoundError("room message marker disappeared")
        return {
            "conversation_id": str(row["conversation_id"]),
            "message_id": str(row["message_id"]),
            "marker_kind": str(row["marker_kind"]),
            "note": str(row["note"] or ""),
            "created_by_web_user_id": str(row["created_by_web_user_id"]),
            "updated_by_web_user_id": str(row["updated_by_web_user_id"]),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    def rename_room(
        self,
        *,
        conversation_id: str,
        new_conversation_id: str,
        renamed_by_web_user_id: str,
    ) -> dict[str, Any]:
        """Rename one room while preserving every foreign-key-linked record."""

        current = validate_conversation_id(conversation_id)
        renamed = validate_conversation_id(new_conversation_id)
        actor = opaque_id(
            renamed_by_web_user_id,
            field="renamed_by_web_user_id",
        )
        now = time.time()
        with self._transaction() as conn:
            room_permissions = self._room_web_permissions_locked(
                conn,
                web_user_id=actor,
                conversation_id=current,
            )
            if not room_permissions["can_rename_room"]:
                raise AuthorizationError("只有全局管理员或聊天室创建者可以重命名")
            room = conn.execute(
                "SELECT * FROM rooms WHERE conversation_id = ?",
                (current,),
            ).fetchone()
            if room is None:
                raise NotFoundError(f"unknown conversation: {current}")
            if renamed == current:
                return {
                    "previous_conversation_id": current,
                    "conversation_id": current,
                    "status": str(room["status"]),
                    "renamed_at": now,
                }
            duplicate = conn.execute(
                "SELECT status FROM rooms WHERE conversation_id = ?",
                (renamed,),
            ).fetchone()
            if duplicate is not None:
                raise ConflictError(
                    f"conversation {renamed} already exists with status "
                    f"{duplicate['status']}"
                )
            conn.execute("PRAGMA defer_foreign_keys = ON")
            conn.execute(
                "UPDATE rooms SET conversation_id = ?, "
                "last_activity_at = MAX(last_activity_at, ?) "
                "WHERE conversation_id = ?",
                (renamed, now, current),
            )
            for table in (
                "memberships",
                "agent_sessions",
                "messages",
                "room_message_sequences",
                "room_message_markers",
                "agent_invitations",
                "agent_connectors",
                "agent_room_blocks",
                "room_web_owners",
                "room_web_members",
                "room_task_policies",
                "room_task_grants",
                "room_wake_policies",
                "room_tasks",
                "chat_authorization_grants",
                "a2a_access_grants",
            ):
                column = (
                    "registered_conversation_id"
                    if table == "agent_sessions"
                    else "conversation_id"
                )
                conn.execute(
                    f"UPDATE {table} SET {column} = ? WHERE {column} = ?",
                    (renamed, current),
                )
            conn.execute(
                "UPDATE follows SET conversation_id = ?, updated_at = ? "
                "WHERE conversation_id = ?",
                (renamed, now, current),
            )
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise BridgeError("room rename would violate database relationships")
        return {
            "previous_conversation_id": current,
            "conversation_id": renamed,
            "status": str(room["status"]),
            "renamed_at": now,
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

    @staticmethod
    def _normalized_agent_registration(
        *,
        product: str,
        username: str,
        session_alias: str | None,
        signature: str | None,
        conversation_id: str,
        roles: Sequence[str] | None,
        capabilities: Sequence[str] | None,
        session_ttl_seconds: float,
    ) -> dict[str, Any]:
        normalized_product = token(product, field="product_name")
        normalized_username = agent_username(username)
        normalized_identity = product_username(
            normalized_product,
            normalized_username,
        )
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
        return {
            "product": normalized_product,
            "username": normalized_username,
            "identity": normalized_identity,
            "alias": normalized_alias,
            "signature": normalized_signature,
            "signature_supplied": signature is not None,
            "conversation_id": conversation,
            "roles": normalized_roles,
            "capabilities": normalized_capabilities,
            "session_ttl_seconds": session_ttl,
        }

    def _register_agent_session_locked(
        self,
        conn: sqlite3.Connection,
        *,
        registration: dict[str, Any],
        connector_id: str | None,
        session_component: str,
        invitation_grant: bool,
        now: float,
    ) -> dict[str, Any]:
        normalized_identity = str(registration["identity"])
        normalized_alias = str(registration["alias"])
        normalized_signature = str(registration["signature"])
        conversation = str(registration["conversation_id"])
        normalized_roles = list(registration["roles"])
        normalized_capabilities = list(registration["capabilities"])
        session_ttl = float(registration["session_ttl_seconds"])
        normalized_component = str(session_component or "").strip().lower()
        if normalized_component not in SESSION_COMPONENTS - {"unknown"}:
            raise ValidationError("unsupported Agent session component")
        session_id = f"session_{uuid.uuid4().hex}"
        access_token = f"session_{secrets.token_urlsafe(32)}"
        self._archive_stale_rooms_locked(conn, now=now)
        self._expire_inactive_agents_locked(conn, now=now)
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
            if connector_id is None and not invitation_grant:
                self._assert_agent_registration_allowed_locked(
                    conn,
                    participant_id=participant_id,
                    conversation_id=conversation,
                    now=now,
                )
            if connector_id is not None and not invitation_grant:
                connector_binding = conn.execute(
                    """
                    SELECT accepted_participant_id
                    FROM agent_connectors
                    WHERE connector_id = ? AND revoked_at IS NULL
                    """,
                    (connector_id,),
                ).fetchone()
                if (
                    connector_binding is None
                    or str(connector_binding["accepted_participant_id"])
                    != participant_id
                ):
                    raise AuthenticationError(
                        "Agent connector is not bound to this identity"
                    )
            if connector_id is None and not invitation_grant:
                bound_connector = conn.execute(
                    """
                    SELECT connector_id
                    FROM agent_connectors
                    WHERE accepted_participant_id = ?
                    LIMIT 1
                    """,
                    (participant_id,),
                ).fetchone()
                if bound_connector is not None:
                    raise AuthenticationError(
                        "existing Agent identity requires its connector enrollment "
                        "credential"
                    )
            if invitation_grant:
                self._grant_agent_invitation_locked(
                    conn,
                    participant_id=participant_id,
                    conversation_id=conversation,
                    now=now,
                )
            # Old clients used session_alias for a per-process purpose and may
            # send a different value after reconnecting. It is no longer
            # identity authority, so accept and ignore it for stable identities.
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
                    1 if registration["signature_supplied"] else 0,
                    normalized_signature,
                    now,
                    now,
                    participant_id,
                ),
            )

        if existing is None:
            if invitation_grant:
                self._grant_agent_invitation_locked(
                    conn,
                    participant_id=participant_id,
                    conversation_id=conversation,
                    now=now,
                )
            else:
                self._ensure_agent_lifecycle_state_locked(
                    conn,
                    participant_id=participant_id,
                    now=now,
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
                 ttl_seconds, last_seen, connector_id, component)
            VALUES (?, ?, ?, ?, 'mcp', ?, ?, ?, ?, ?, ?)
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
                connector_id,
                normalized_component,
            ),
        )
        if connector_id is not None:
            self._retire_idle_connector_sessions_locked(
                conn,
                now=now,
                connector_id=connector_id,
            )
        owned_count = self._agent_active_room_count(conn, participant_id)
        profile = conn.execute(
            "SELECT session_alias, display_name, signature, avatar_key "
            "FROM participants "
            "WHERE participant_id = ?",
            (participant_id,),
        ).fetchone()

        return {
            "participant_id": participant_id,
            "username": str(registration["username"]),
            "client_type": normalized_identity,
            "session_alias": str(profile["session_alias"]),
            "display_name": str(profile["display_name"]),
            "signature": str(profile["signature"]),
            "avatar_key": str(profile["avatar_key"] or "auto"),
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
            "connector_id": connector_id,
            "session_component": normalized_component,
        }

    @staticmethod
    def _retire_idle_connector_sessions_locked(
        conn: sqlite3.Connection,
        *,
        now: float,
        connector_id: str | None = None,
    ) -> int:
        """Bound superseded credentials created by resident connector turns.

        Listener, chat-worker, task-worker, and short-lived model processes can
        each register a credential for the same durable connector.  Preserve
        the newest working set and audit rows, but logically retire older idle
        credentials so participant projections do not scan an ever-growing
        two-hour overlap window.
        """

        connector_clause = "AND connector_id = ?" if connector_id else ""
        parameters: tuple[object, ...] = (
            (now, connector_id) if connector_id else (now,)
        )
        rows = conn.execute(
            f"""
            SELECT session_id, connector_id, last_seen
            FROM agent_sessions
            WHERE connector_id IS NOT NULL
              AND cleared_at IS NULL
              AND revoked_at IS NULL
              AND expires_at > ?
              {connector_clause}
            ORDER BY connector_id, last_seen DESC, created_at DESC, session_id DESC
            """,
            parameters,
        ).fetchall()
        cutoff = now - CONNECTOR_SESSION_IDLE_RETIRE_SECONDS
        ranks: dict[str, int] = {}
        retired_session_ids: list[str] = []
        for row in rows:
            row_connector = str(row["connector_id"])
            rank = ranks.get(row_connector, 0) + 1
            ranks[row_connector] = rank
            if (
                rank > CONNECTOR_SESSION_MIN_RETAIN
                and float(row["last_seen"]) <= cutoff
            ):
                retired_session_ids.append(str(row["session_id"]))
        if not retired_session_ids:
            return 0
        conn.executemany(
            """
            UPDATE agent_sessions
            SET revoked_at = COALESCE(revoked_at, ?),
                revoked_reason = COALESCE(
                    revoked_reason,
                    'connector_session_superseded'
                ),
                cleared_at = COALESCE(cleared_at, ?)
            WHERE session_id = ?
              AND cleared_at IS NULL
              AND revoked_at IS NULL
            """,
            ((now, now, session_id) for session_id in retired_session_ids),
        )
        return len(retired_session_ids)

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
        connector_id: str | None = None,
    ) -> dict[str, Any]:
        registration = self._normalized_agent_registration(
            product=product,
            username=username,
            session_alias=session_alias,
            signature=signature,
            conversation_id=conversation_id,
            roles=roles,
            capabilities=capabilities,
            session_ttl_seconds=session_ttl_seconds,
        )
        normalized_connector = (
            opaque_id(connector_id, field="connector_id") if connector_id else None
        )
        with self._transaction() as conn:
            return self._register_agent_session_locked(
                conn,
                registration=registration,
                connector_id=normalized_connector,
                session_component="mcp",
                invitation_grant=False,
                now=time.time(),
            )

    def authenticate_session(self, access_token: str) -> dict[str, Any]:
        normalized_token = opaque_id(access_token, field="access_token")
        now = time.time()
        with self._transaction() as conn:
            self._expire_inactive_agents_locked(conn, now=now)
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
            "registered_conversation_id": str(
                row["registered_conversation_id"]
            ),
            "client_type": str(row["client_type"]),
            "session_alias": str(row["session_alias"]),
            "display_name": str(row["display_name"]),
            "signature": str(row["signature"]),
            "connector_id": (
                str(row["connector_id"])
                if row["connector_id"] is not None
                else None
            ),
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
            expired_agents = self._expire_inactive_agents_locked(
                conn,
                now=cleared_at,
            )
            retired_connector_sessions = (
                self._retire_idle_connector_sessions_locked(
                    conn,
                    now=cleared_at,
                )
            )
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
        result = {
            "cleared_count": int(cleared_count),
            "cleared_at": cleared_at,
            "mode": "logical",
            "audit_links_preserved": True,
        }
        if expired_agents:
            result["expired_agent_count"] = len(expired_agents)
            result["expired_agents"] = expired_agents
        if retired_connector_sessions:
            result["retired_connector_session_count"] = (
                retired_connector_sessions
            )
        return result

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
        signature: object | None = None,
        avatar_key: object | None = None,
    ) -> dict[str, Any]:
        participant = opaque_id(participant_id, field="participant_id")
        session = opaque_id(authorized_session_id, field="authorized_session_id")
        if signature is None and avatar_key is None:
            raise ValidationError("signature or avatar_key is required")
        normalized_signature = (
            alias(signature, field="signature")
            if signature is not None
            else None
        )
        normalized_avatar = (
            normalize_avatar_key(avatar_key)
            if avatar_key is not None
            else None
        )
        now = time.time()
        with self._transaction() as conn:
            self._require_live_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=now,
            )
            current = conn.execute(
                "SELECT * FROM participants WHERE participant_id = ?",
                (participant,),
            ).fetchone()
            if current is None:
                raise NotFoundError(f"unknown participant: {participant}")
            next_avatar_changed_at = (
                self._next_avatar_changed_at(
                    current_avatar=str(current["avatar_key"] or "auto"),
                    current_changed_at=current["avatar_changed_at"],
                    next_avatar=normalized_avatar,
                    now=now,
                )
                if normalized_avatar is not None
                else current["avatar_changed_at"]
            )
            updated = conn.execute(
                "UPDATE participants SET signature = COALESCE(?, signature), "
                "avatar_key = COALESCE(?, avatar_key), avatar_changed_at = ?, "
                "profile_updated_at = ?, "
                "last_seen = ? "
                "WHERE participant_id = ?",
                (
                    normalized_signature,
                    normalized_avatar,
                    next_avatar_changed_at,
                    now,
                    now,
                    participant,
                ),
            ).rowcount
            if not updated:
                raise NotFoundError(f"unknown participant: {participant}")
            row = conn.execute(
                "SELECT * FROM participants WHERE participant_id = ?",
                (participant,),
            ).fetchone()
        return self._participant_profile_payload(row)

    @staticmethod
    def _next_avatar_changed_at(
        *,
        current_avatar: str,
        current_changed_at: object | None,
        next_avatar: str,
        now: float,
    ) -> float | None:
        changed_at = (
            float(current_changed_at)
            if current_changed_at is not None
            else None
        )
        if next_avatar == current_avatar:
            return changed_at
        if changed_at is not None:
            retry_after = (
                changed_at + AGENT_AVATAR_CHANGE_COOLDOWN_SECONDS - now
            )
            if retry_after > 0:
                raise AvatarRateLimitError(retry_after_seconds=retry_after)
        # Picking an avatar for an identity still using ``auto`` is
        # initialization, not its first daily change. Every later different
        # selection starts a rolling 24-hour cooldown.
        if current_avatar == "auto" and changed_at is None:
            return None
        return now

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
        reviewed_by_web_user_id: str | None = None,
    ) -> dict[str, Any]:
        request = opaque_id(request_id, field="request_id")
        normalized_action = str(action or "").strip().lower()
        if normalized_action not in {"approve", "reject"}:
            raise ValidationError("action must be approve or reject")
        note = alias(review_note, field="review_note") if review_note else None
        reviewer = (
            opaque_id(reviewed_by_web_user_id, field="reviewed_by_web_user_id")
            if reviewed_by_web_user_id
            else None
        )
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
            if reviewer is not None:
                administrator = conn.execute(
                    "SELECT user_id FROM web_users WHERE user_id = ? "
                    "AND role = 'admin' AND active = 1",
                    (reviewer,),
                ).fetchone()
                if administrator is None:
                    raise AuthenticationError(
                        "an active administrator is required to review nicknames"
                    )
            conn.execute(
                "UPDATE nickname_requests SET status = ?, reviewed_at = ?, "
                "review_note = ?, reviewed_by_web_user_id = ? WHERE request_id = ?",
                (status, now, note, reviewer, request),
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
            self._require_live_room_session(
                conn,
                session_id=session,
                participant_id=follower,
                conversation_id=conversation,
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
            self._require_live_room_session(
                conn,
                session_id=session,
                participant_id=follower,
                conversation_id=conversation,
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

    def set_room_dnd(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        conversation_id: str,
        enabled: bool = True,
        _now: float | None = None,
    ) -> dict[str, Any]:
        """Suppress only digest wakes in one room until the next local midnight."""

        participant = opaque_id(participant_id, field="participant_id")
        session = opaque_id(
            authorized_session_id,
            field="authorized_session_id",
        )
        conversation = validate_conversation_id(conversation_id)
        if not isinstance(enabled, bool):
            raise ValidationError("enabled must be a boolean")
        now = float(time.time() if _now is None else _now)
        with self._transaction() as conn:
            self._require_live_room_session(
                conn,
                session_id=session,
                participant_id=participant,
                conversation_id=conversation,
                now=now,
            )
            self._require_membership(conn, participant, conversation)
            if enabled:
                expires_at = self._next_business_midnight(now)
                conn.execute(
                    """
                    INSERT INTO agent_room_dnd
                        (participant_id, conversation_id, enabled_at,
                         expires_at, timezone_name, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(participant_id, conversation_id) DO UPDATE SET
                        enabled_at = excluded.enabled_at,
                        expires_at = excluded.expires_at,
                        timezone_name = excluded.timezone_name,
                        updated_at = excluded.updated_at
                    """,
                    (
                        participant,
                        conversation,
                        now,
                        expires_at,
                        self.business_timezone_name,
                        now,
                    ),
                )
            else:
                conn.execute(
                    "DELETE FROM agent_room_dnd "
                    "WHERE participant_id = ? AND conversation_id = ?",
                    (participant, conversation),
                )
            row = conn.execute(
                "SELECT * FROM agent_room_dnd "
                "WHERE participant_id = ? AND conversation_id = ?",
                (participant, conversation),
            ).fetchone()
        active = row is not None and float(row["expires_at"]) > now
        return {
            "participant_id": participant,
            "conversation_id": conversation,
            "active": active,
            "enabled_at": float(row["enabled_at"]) if active else None,
            "expires_at": float(row["expires_at"]) if active else None,
            "timezone": (
                str(row["timezone_name"])
                if active
                else self.business_timezone_name
            ),
            "digest_wake_suppressed": active,
            "direct_notifications_optional": active,
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
        notification_mode: str | None = None,
        wake_all_agents: bool = False,
        _owner_ui: bool = False,
        _web_user: bool = False,
        _message_kind: str = "message",
        _forwarded_from_message_id: str | None = None,
        _suppress_chat_authorization: bool = False,
        _suppress_mention_inference: bool = False,
        _task_request: dict[str, Any] | None = None,
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
        requested_notification_mode = (
            str(notification_mode).strip().lower()
            if notification_mode is not None
            else None
        )
        if (
            requested_notification_mode is not None
            and requested_notification_mode not in MESSAGE_NOTIFICATION_MODES
        ):
            raise ValidationError(
                "notification_mode must be ordinary or mention"
            )
        if not isinstance(wake_all_agents, bool):
            raise ValidationError("wake_all_agents must be a boolean")
        normalized_wake_all = bool(wake_all_agents)
        normalized_reply = (
            opaque_id(reply_to, field="reply_to") if reply_to else None
        )
        normalized_message_kind = str(_message_kind or "message").strip().lower()
        normalized_forward = (
            opaque_id(
                _forwarded_from_message_id,
                field="forwarded_from_message_id",
            )
            if _forwarded_from_message_id
            else None
        )
        if normalized_message_kind not in {"message", "forward", "task"}:
            raise ValidationError("unsupported internal message kind")
        if (normalized_message_kind == "forward") != bool(normalized_forward):
            raise ValidationError("cross-room forwards require one source message")
        if (normalized_message_kind == "task") != bool(_task_request):
            raise ValidationError("structured task messages require task metadata")
        if normalized_wake_all and normalized_audience != "room":
            raise ValidationError("wake_all_agents requires a room audience")
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
        task_id = f"task_{uuid.uuid4().hex}" if _task_request else None
        task_target_kind: str | None = None
        task_target_ids: list[str] = []
        review_routing: dict[str, Any] | None = None
        sender_seat = "unknown"
        web_identity: sqlite3.Row | None = None
        body_routing: list[dict[str, Any]] = []

        with self._transaction() as conn:
            self._archive_stale_rooms_locked(conn, now=now)
            cooldown_seconds = MESSAGE_COOLDOWN_SECONDS
            if _owner_ui:
                sender_seat = "web"
                if session != OWNER_AUTHORIZATION_ID or sender != OWNER_PARTICIPANT_ID:
                    raise AuthenticationError("invalid owner UI sender binding")
                self._require_active_room(conn, conversation)
                self._ensure_owner_membership_locked(
                    conn,
                    conversation_id=conversation,
                    now=now,
                )
            elif _web_user:
                sender_seat = "web"
                web_identity = self._require_live_web_session(
                    conn,
                    session_id=session,
                    participant_id=sender,
                    now=now,
                )
                self._require_web_room_access_locked(
                    conn,
                    web_identity=web_identity,
                    conversation_id=conversation,
                )
                self._require_active_room(conn, conversation)
                self._ensure_web_membership_locked(
                    conn,
                    conversation_id=conversation,
                    participant_id=sender,
                    display_name=str(web_identity["display_name"]),
                    signature=str(web_identity["signature"]),
                    role=str(web_identity["role"]),
                    now=now,
                )
                if _task_request is not None:
                    task_permissions = self._room_task_permissions_locked(
                        conn,
                        conversation_id=conversation,
                        web_identity=web_identity,
                    )
                    if not task_permissions["can_assign_tasks"]:
                        raise AuthorizationError(
                            "你没有在这个聊天室布置结构化任务的权限"
                        )
                    task_target_kind, task_target_ids = (
                        self._resolve_task_targets_locked(
                            conn,
                            conversation_id=conversation,
                            requested_participant_ids=_task_request.get(
                                "target_participant_ids"
                            ),
                        )
                    )
                if normalized_wake_all:
                    room_permissions = self._room_web_permissions_locked(
                        conn,
                        web_user_id=str(web_identity["user_id"]),
                        conversation_id=conversation,
                    )
                    if not room_permissions["can_wake_all"]:
                        raise AuthorizationError(
                            "只有全局管理员、创建者或聊天室管理员可以使用 @全员"
                        )
                cooldown_seconds = (
                    0.0
                    if str(web_identity["role"]) == "admin"
                    else self._effective_message_cooldown_locked(
                        conn,
                        participant_id=sender,
                        actor_kind="web_user",
                    )
                )
            else:
                live_session = self._require_live_room_session(
                    conn,
                    session_id=session,
                    participant_id=sender,
                    conversation_id=conversation,
                    now=now,
                )
                sender_seat = {
                    "mcp": "main",
                    "chat": "shadow",
                    "task": "executor",
                    "a2a": "a2a",
                }.get(str(live_session["component"] or "unknown"), "unknown")
                self._require_membership(conn, sender, conversation)
                cooldown_seconds = self._effective_message_cooldown_locked(
                    conn,
                    participant_id=sender,
                    actor_kind="agent",
                )
                if normalized_wake_all:
                    raise AuthorizationError("Agent 不能发起结构化 @全员")
            internal_mentions: list[str] = []
            if not _suppress_mention_inference:
                normalized_body, internal_mentions = (
                    self._rewrite_internal_text_mentions_locked(
                        conn,
                        conversation_id=conversation,
                        sender_participant_id=sender,
                        body_text=normalized_body,
                    )
                )
            # A display name can be longer than its opaque ID, so enforce the
            # body limit again after the user-visible rewrite.
            normalized_body = body(normalized_body)
            for inferred in internal_mentions:
                if inferred not in normalized_mentions:
                    normalized_mentions.append(inferred)
            if not _suppress_mention_inference:
                for inferred in self._infer_text_mentions_locked(
                    conn,
                    conversation_id=conversation,
                    sender_participant_id=sender,
                    body_text=normalized_body,
                ):
                    if inferred not in normalized_mentions:
                        normalized_mentions.append(inferred)
            if len(normalized_mentions) > MAX_MENTIONS_PER_MESSAGE:
                raise ValidationError(
                    "mentions cannot contain more than "
                    f"{MAX_MENTIONS_PER_MESSAGE} entries"
                )
            if not _owner_ui and not _web_user:
                self._assert_agent_identity_consistent_locked(
                    conn,
                    participant_id=sender,
                    body_text=normalized_body,
                )
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
            if (
                not _owner_ui
                and not _web_user
                and normalized_message_kind == "message"
                and self._is_direct_review_request(normalized_body)
            ):
                review_targets = list(normalized_mentions)
                review_source = "structured_or_visible_mention"
                if normalized_audience in {"participant", "role"}:
                    # The audience itself is already an explicit routing
                    # decision.  Participant audiences have been folded into
                    # mentions above; role messages use their claimable role
                    # delivery without manufacturing a personal @.
                    review_source = f"audience:{normalized_audience}"
                    if normalized_audience == "role":
                        review_targets = self._role_review_targets_locked(
                            conn,
                            conversation_id=conversation,
                            sender_participant_id=sender,
                            role=normalized_target,
                        )
                elif not review_targets:
                    review_targets = self._infer_named_review_targets_locked(
                        conn,
                        conversation_id=conversation,
                        sender_participant_id=sender,
                        body_text=normalized_body,
                    )
                    review_source = "named_member"
                if not review_targets and normalized_audience == "room":
                    reply_sender = self._reply_sender_locked(
                        conn,
                        conversation_id=conversation,
                        sender_participant_id=sender,
                        reply_to=normalized_reply,
                    )
                    if reply_sender is not None:
                        review_targets = [reply_sender]
                        review_source = "reply_author"
                if (
                    normalized_audience not in {"participant", "role"}
                    and not review_targets
                ):
                    review_routing = {
                        "requested": True,
                        "notified": False,
                        "source": "unresolved",
                        "target_participant_ids": [],
                        "warning": (
                            "review_or_confirmation_target_required: no reviewer "
                            "was notified; immediately resend with a same-room "
                            "member's exact name, structured mentions, reply_to, "
                            "or a participant/role audience"
                        ),
                    }
                if normalized_audience != "role":
                    for review_target in review_targets:
                        if review_target not in normalized_mentions:
                            normalized_mentions.append(review_target)
                if len(normalized_mentions) > MAX_MENTIONS_PER_MESSAGE:
                    raise ValidationError(
                        "mentions cannot contain more than "
                        f"{MAX_MENTIONS_PER_MESSAGE} entries"
                    )
                if review_routing is None:
                    review_routing = {
                        "requested": True,
                        "notified": True,
                        "source": review_source,
                        "target_participant_ids": sorted(set(review_targets)),
                    }
            if normalized_forward:
                source = conn.execute(
                    "SELECT conversation_id, message_kind FROM messages "
                    "WHERE message_id = ?",
                    (normalized_forward,),
                ).fetchone()
                if source is None:
                    raise NotFoundError(
                        f"unknown forwarded source message: {normalized_forward}"
                    )
                if str(source["conversation_id"]) == conversation:
                    raise ConflictError("cross-room forward target must differ")
                if str(source["message_kind"]) == "forward":
                    raise ConflictError(
                        "forward chains are not allowed; forward the original message"
                    )
            has_notification_target = bool(
                normalized_mentions
                or normalized_wake_all
                or normalized_reply
                or normalized_audience in {"participant", "role"}
            )
            effective_notification_mode = (
                requested_notification_mode
                or ("mention" if has_notification_target else "ordinary")
            )
            if effective_notification_mode == "mention" and not has_notification_target:
                raise ValidationError(
                    "mention mode requires mentions, reply_to, or a participant/role "
                    "audience"
                )
            if effective_notification_mode == "ordinary" and has_notification_target:
                raise ValidationError(
                    "ordinary mode cannot include mentions, reply_to, @全员, or a "
                    "participant/role audience"
                )
            self._assert_speaking_cooldown(
                conn,
                participant_id=sender,
                conversation_id=conversation,
                now=now,
                cooldown_seconds=cooldown_seconds,
            )
            try:
                conn.execute(
                    """
                    INSERT INTO messages
                        (message_id, conversation_id, sender_participant_id,
                         audience_kind, audience_value, message_kind, body,
                         refs_json, mentions_json, wake_all_agents, reply_to, status,
                         authorized_session_id, forwarded_from_message_id,
                         sender_seat, notification_mode, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        conversation,
                        sender,
                        normalized_audience,
                        normalized_target,
                        normalized_message_kind,
                        normalized_body,
                        compact_json(normalized_refs),
                        compact_json(normalized_mentions),
                        1 if normalized_wake_all else 0,
                        normalized_reply,
                        session,
                        normalized_forward,
                        sender_seat,
                        effective_notification_mode,
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
                        cooldown_seconds=cooldown_seconds,
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
                        "an authenticated Agent session or web user session is required "
                        "to chat"
                    ) from exc
                raise
            row = conn.execute(
                "SELECT * FROM messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            task_payload = None
            if _task_request is not None:
                conn.execute(
                    """
                    INSERT INTO room_tasks
                        (task_id, conversation_id, source_message_id,
                         parent_task_id, issuer_web_user_id,
                         issuer_participant_id, target_kind,
                         target_participant_ids_json, body, status,
                         source_sequence, context_start_sequence,
                         context_end_sequence,
                         created_at, updated_at)
                    VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        conversation,
                        message_id,
                        str(web_identity["user_id"]),
                        sender,
                        task_target_kind,
                        compact_json(task_target_ids),
                        normalized_body,
                        int(row["sequence"]),
                        max(1, int(row["sequence"]) - 20),
                        int(row["sequence"]),
                        now,
                        now,
                    ),
                )
                task_row = conn.execute(
                    "SELECT * FROM room_tasks WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                task_payload = self._task_payload(task_row)
            elif (
                _web_user
                and web_identity is not None
                and normalized_message_kind == "message"
                and normalized_mentions
            ):
                task_permissions = self._room_task_permissions_locked(
                    conn,
                    conversation_id=conversation,
                    web_identity=web_identity,
                )
                if task_permissions["can_assign_tasks"]:
                    body_routing = self._route_web_message_to_body_locked(
                        conn,
                        message=row,
                        issuer_web_user_id=str(web_identity["user_id"]),
                        mentioned_participant_ids=normalized_mentions,
                        now=now,
                    )
                    created_task_ids = [
                        str(route["task_id"])
                        for route in body_routing
                        if route["mode"] == "queued"
                    ]
                    if created_task_ids:
                        task_row = conn.execute(
                            "SELECT * FROM room_tasks WHERE task_id = ?",
                            (created_task_ids[0],),
                        ).fetchone()
                        task_payload = self._task_payload(task_row)
            if (
                _web_user
                and str(web_identity["role"]) == "admin"
                and not _suppress_chat_authorization
            ):
                self._insert_admin_chat_authorization_grant_locked(
                    conn,
                    message=row,
                    issuer_web_user_id=str(web_identity["user_id"]),
                    issuer_username=str(web_identity["username"]),
                    issuer_role=str(web_identity["role"]),
                )
            self._create_message_deliveries_locked(conn, row)
            if body_routing:
                routed_targets = sorted(
                    {
                        str(route["target_participant_id"])
                        for route in body_routing
                    }
                )
                placeholders = ",".join("?" for _ in routed_targets)
                conn.execute(
                    f"""
                    UPDATE message_deliveries
                    SET state = 'cancelled', actionable = 0
                    WHERE message_id = ?
                      AND participant_id IN ({placeholders})
                      AND state IN ('pending', 'delivered')
                    """,
                    (message_id, *routed_targets),
                )
            payload = self._message_payload(
                row,
                authorization=self._chat_authorization_for_message_locked(
                    conn,
                    message_id=message_id,
                    recipient_participant_id=None,
                ),
            )
            if task_payload is not None:
                payload["task"] = task_payload
            if body_routing:
                payload["body_routing"] = body_routing
            if review_routing is not None:
                payload["review_routing"] = review_routing
        return payload

    def send_owner_message(
        self,
        *,
        conversation_id: str,
        body_text: str,
        mentions: Sequence[str] | None = None,
        wake_all_agents: bool = False,
        reply_to: str | None = None,
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
            wake_all_agents=wake_all_agents,
            reply_to=reply_to,
            _owner_ui=True,
        )

    def send_web_message(
        self,
        *,
        authorized_session_id: str,
        participant_id: str,
        conversation_id: str,
        body_text: str,
        mentions: Sequence[str] | None = None,
        wake_all_agents: bool = False,
        reply_to: str | None = None,
    ) -> dict[str, Any]:
        """Send one authenticated web user's room message under its own identity."""

        return self.send(
            authorized_session_id=authorized_session_id,
            sender_participant_id=participant_id,
            conversation_id=conversation_id,
            body_text=body_text,
            audience_kind="room",
            audience_value="*",
            mentions=mentions,
            wake_all_agents=wake_all_agents,
            reply_to=reply_to,
            _web_user=True,
        )

    def send_web_task(
        self,
        *,
        authorized_session_id: str,
        participant_id: str,
        conversation_id: str,
        body_text: str,
        target_participant_ids: Sequence[str] | None = None,
        reply_to: str | None = None,
    ) -> dict[str, Any]:
        """Create a server-authorized task without changing ordinary chat authority."""

        normalized_body = str(body_text or "").strip()
        if normalized_body.startswith("/任务"):
            normalized_body = normalized_body[len("/任务") :].lstrip("：: \t")
        if not normalized_body:
            raise ValidationError("任务内容不能为空")
        requested_targets = self._normalize_mentions(target_participant_ids)

        # The Web composer normally sends structured IDs. Keep the typed
        # ``/任务 @昵称 ...`` shortcut equivalent by resolving exact visible
        # aliases only when the caller omitted those IDs. The stored task body
        # remains unchanged and no ordinary chat delivery is created.
        if not requested_targets:
            with self._connection() as conn:
                requested_targets = self._infer_text_mentions_locked(
                    conn,
                    conversation_id=validate_conversation_id(conversation_id),
                    sender_participant_id=opaque_id(
                        participant_id,
                        field="participant_id",
                    ),
                    body_text=normalized_body,
                )

        return self.send(
            authorized_session_id=authorized_session_id,
            sender_participant_id=participant_id,
            conversation_id=conversation_id,
            body_text=normalized_body,
            audience_kind="room",
            audience_value="*",
            mentions=[],
            reply_to=reply_to,
            _web_user=True,
            _message_kind="task",
            _suppress_chat_authorization=True,
            _suppress_mention_inference=True,
            _task_request={
                "target_participant_ids": requested_targets
            },
        )

    @staticmethod
    def _room_wake_policy_payload(
        row: sqlite3.Row | None,
        *,
        conversation_id: str,
    ) -> dict[str, Any]:
        return {
            "conversation_id": conversation_id,
            "mode": str(row["mode"]) if row is not None else DEFAULT_ROOM_WAKE_MODE,
            "digest_min_messages": (
                int(row["digest_min_messages"])
                if row is not None
                else DEFAULT_ROOM_DIGEST_MIN_MESSAGES
            ),
            "digest_after_seconds": (
                float(row["digest_after_seconds"])
                if row is not None
                else float(DEFAULT_ROOM_DIGEST_AFTER_SECONDS)
            ),
            "updated_at": (
                float(row["updated_at"]) if row is not None else None
            ),
        }

    def room_wake_policies_bulk(
        self,
        *,
        conversation_ids: Sequence[str],
    ) -> dict[str, dict[str, Any]]:
        conversations = [
            validate_conversation_id(value) for value in conversation_ids
        ]
        if not conversations:
            return {}
        placeholders = ",".join("?" for _ in conversations)
        with self._connection() as conn:
            rows = {
                str(row["conversation_id"]): row
                for row in conn.execute(
                    f"SELECT * FROM room_wake_policies "
                    f"WHERE conversation_id IN ({placeholders})",
                    conversations,
                ).fetchall()
            }
        return {
            conversation: self._room_wake_policy_payload(
                rows.get(conversation),
                conversation_id=conversation,
            )
            for conversation in conversations
        }

    def room_wake_policy(
        self,
        *,
        authorized_session_id: str,
        participant_id: str,
        conversation_id: str,
    ) -> dict[str, Any]:
        session = opaque_id(authorized_session_id, field="authorized_session_id")
        participant = opaque_id(participant_id, field="participant_id")
        conversation = validate_conversation_id(conversation_id)
        with self._connection() as conn:
            identity = self._require_live_web_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=time.time(),
            )
            self._require_web_room_access_locked(
                conn,
                web_identity=identity,
                conversation_id=conversation,
            )
            row = conn.execute(
                "SELECT * FROM room_wake_policies WHERE conversation_id = ?",
                (conversation,),
            ).fetchone()
        return self._room_wake_policy_payload(row, conversation_id=conversation)

    def update_room_wake_policy(
        self,
        *,
        authorized_session_id: str,
        participant_id: str,
        conversation_id: str,
        mode: str,
        digest_min_messages: object = DEFAULT_ROOM_DIGEST_MIN_MESSAGES,
        digest_after_seconds: object = DEFAULT_ROOM_DIGEST_AFTER_SECONDS,
    ) -> dict[str, Any]:
        session = opaque_id(authorized_session_id, field="authorized_session_id")
        participant = opaque_id(participant_id, field="participant_id")
        conversation = validate_conversation_id(conversation_id)
        normalized_mode = str(mode or "").strip().lower()
        if normalized_mode not in ROOM_WAKE_MODES:
            raise ValidationError("room wake mode must be mention, digest, or all")
        if isinstance(digest_min_messages, bool):
            raise ValidationError("digest_min_messages must be an integer")
        try:
            minimum = int(digest_min_messages)
            after_seconds = float(digest_after_seconds)
        except (TypeError, ValueError) as exc:
            raise ValidationError("invalid room digest configuration") from exc
        if not 1 <= minimum <= 500:
            raise ValidationError("digest_min_messages must be between 1 and 500")
        if not math.isfinite(after_seconds) or not 30 <= after_seconds <= 86_400:
            raise ValidationError(
                "digest_after_seconds must be between 30 and 86400"
            )
        now = time.time()
        with self._transaction() as conn:
            identity = self._require_live_web_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=now,
            )
            self._require_web_room_access_locked(
                conn,
                web_identity=identity,
                conversation_id=conversation,
            )
            self._require_active_room(conn, conversation)
            room_permissions = self._room_web_permissions_locked(
                conn,
                web_user_id=str(identity["user_id"]),
                conversation_id=conversation,
            )
            if not room_permissions["can_manage_wake_policy"]:
                raise AuthorizationError(
                    "只有全局管理员、创建者或聊天室管理员可以调整唤醒策略"
                )
            conn.execute(
                """
                INSERT INTO room_wake_policies
                    (conversation_id, mode, digest_min_messages,
                     digest_after_seconds, updated_by_web_user_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    mode = excluded.mode,
                    digest_min_messages = excluded.digest_min_messages,
                    digest_after_seconds = excluded.digest_after_seconds,
                    updated_by_web_user_id = excluded.updated_by_web_user_id,
                    updated_at = excluded.updated_at
                """,
                (
                    conversation,
                    normalized_mode,
                    minimum,
                    after_seconds,
                    str(identity["user_id"]),
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM room_wake_policies WHERE conversation_id = ?",
                (conversation,),
            ).fetchone()
        return self._room_wake_policy_payload(row, conversation_id=conversation)

    def convert_web_message_to_task(
        self,
        *,
        authorized_session_id: str,
        participant_id: str,
        message_id: str,
        target_participant_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Atomically turn the caller's ordinary room message into a task.

        The original message remains the immutable source.  Its nearby history
        locator is captured so the execution seat receives the full handoff
        context instead of relying on a shadow Agent's paraphrase.
        """

        session = opaque_id(authorized_session_id, field="authorized_session_id")
        participant = opaque_id(participant_id, field="participant_id")
        source_id = opaque_id(message_id, field="message_id")
        requested_targets = self._normalize_mentions(target_participant_ids)
        now = time.time()
        task_id = f"task_{uuid.uuid4().hex}"
        with self._transaction() as conn:
            identity = self._require_live_web_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=now,
            )
            source = conn.execute(
                "SELECT * FROM messages WHERE message_id = ?",
                (source_id,),
            ).fetchone()
            if source is None:
                raise NotFoundError(f"unknown message: {source_id}")
            if str(source["sender_participant_id"]) != participant:
                raise AuthorizationError("只能把自己发送的普通消息转为任务")
            if str(source["message_kind"]) != "message":
                raise ConflictError("只有普通聊天消息可以转为任务")
            conversation = str(source["conversation_id"])
            self._require_web_room_access_locked(
                conn,
                web_identity=identity,
                conversation_id=conversation,
            )
            permissions = self._room_task_permissions_locked(
                conn,
                conversation_id=conversation,
                web_identity=identity,
            )
            if not permissions["can_assign_tasks"]:
                raise AuthorizationError("你没有在这个聊天室布置任务的权限")
            if conn.execute(
                "SELECT 1 FROM room_tasks WHERE source_message_id = ?",
                (source_id,),
            ).fetchone() is not None:
                raise ConflictError("这条消息已经转为任务")
            if not requested_targets:
                mentioned = self._normalize_mentions(
                    json.loads(str(source["mentions_json"] or "[]"))
                )
                if mentioned:
                    placeholders = ",".join("?" for _ in mentioned)
                    requested_targets = [
                        str(row["participant_id"])
                        for row in conn.execute(
                            f"""
                            SELECT participant.participant_id
                            FROM participants AS participant
                            JOIN memberships AS membership
                              ON membership.participant_id = participant.participant_id
                             AND membership.conversation_id = ?
                             AND membership.active = 1
                            LEFT JOIN web_users AS web_user
                              ON web_user.participant_id = participant.participant_id
                            WHERE participant.participant_id IN ({placeholders})
                              AND web_user.user_id IS NULL
                            """,
                            (conversation, *mentioned),
                        ).fetchall()
                    ]
            target_kind, target_ids = self._resolve_task_targets_locked(
                conn,
                conversation_id=conversation,
                requested_participant_ids=requested_targets,
            )
            source_sequence = int(source["sequence"])
            context_end = int(
                conn.execute(
                    "SELECT COALESCE(MAX(sequence), ?) FROM messages "
                    "WHERE conversation_id = ?",
                    (source_sequence, conversation),
                ).fetchone()[0]
            )
            conn.execute(
                """
                INSERT INTO room_tasks
                    (task_id, conversation_id, source_message_id,
                     parent_task_id, issuer_web_user_id,
                     issuer_participant_id, target_kind,
                     target_participant_ids_json, body, status,
                     source_sequence, context_start_sequence,
                     context_end_sequence, created_at, updated_at)
                VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    conversation,
                    source_id,
                    str(identity["user_id"]),
                    participant,
                    target_kind,
                    compact_json(target_ids),
                    str(source["body"]),
                    source_sequence,
                    max(1, source_sequence - 20),
                    context_end,
                    now,
                    now,
                ),
            )
            # The route-immutability trigger permits this one transition only
            # after the durable task row exists in the same transaction.
            conn.execute(
                "UPDATE messages SET message_kind = 'task', updated_at = ? "
                "WHERE message_id = ?",
                (now, source_id),
            )
            conn.execute(
                """
                UPDATE message_deliveries
                SET state = 'cancelled', actionable = 0
                WHERE message_id = ?
                  AND participant_id IN (
                      SELECT participant.participant_id
                      FROM participants AS participant
                      LEFT JOIN web_users AS web_user
                        ON web_user.participant_id = participant.participant_id
                      WHERE web_user.user_id IS NULL
                  )
                  AND state IN ('pending', 'delivered')
                """,
                (source_id,),
            )
            task_row = conn.execute(
                "SELECT * FROM room_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            message_row = conn.execute(
                "SELECT * FROM messages WHERE message_id = ?",
                (source_id,),
            ).fetchone()
            result = self._message_payload(message_row, authorization=None)
            result["task"] = self._task_payload(task_row)
        return result

    @staticmethod
    def _body_ready_targets_locked(
        conn: sqlite3.Connection,
        *,
        conversation_id: str,
        participant_ids: Sequence[str],
    ) -> set[str]:
        requested = sorted(set(participant_ids))
        if not requested:
            return set()
        placeholders = ",".join("?" for _ in requested)
        rows = conn.execute(
            f"""
            SELECT DISTINCT membership.participant_id
            FROM memberships AS membership
            JOIN agent_connectors AS connector
              ON connector.accepted_participant_id = membership.participant_id
             AND connector.conversation_id = membership.conversation_id
             AND connector.setup_status = 'configured'
             AND connector.revoked_at IS NULL
            JOIN connector_component_readiness AS readiness
              ON readiness.connector_id = connector.connector_id
             AND readiness.component = 'task'
            LEFT JOIN web_users AS web_user
              ON web_user.participant_id = membership.participant_id
             AND web_user.active = 1
            WHERE membership.conversation_id = ?
              AND membership.active = 1
              AND membership.participant_id IN ({placeholders})
              AND web_user.user_id IS NULL
            """,
            (conversation_id, *requested),
        ).fetchall()
        return {str(row["participant_id"]) for row in rows}

    def _route_web_message_to_body_locked(
        self,
        conn: sqlite3.Connection,
        *,
        message: sqlite3.Row,
        issuer_web_user_id: str,
        mentioned_participant_ids: Sequence[str],
        now: float,
    ) -> list[dict[str, Any]]:
        """Route an authorized personal @ to the persistent body seat.

        A running task receives an exact durable input. An idle body seat gets
        a new single-target task. Targets without a proven task component stay
        on the normal chat path so rolling upgrades never black-hole mentions.
        """

        conversation = str(message["conversation_id"])
        ready_targets = self._body_ready_targets_locked(
            conn,
            conversation_id=conversation,
            participant_ids=mentioned_participant_ids,
        )
        if not ready_targets:
            return []
        source_message_id = str(message["message_id"])
        source_sequence = int(message["sequence"])
        issuer_participant_id = str(message["sender_participant_id"])
        message_body = str(message["body"])
        routes: list[dict[str, Any]] = []
        primary_task_id: str | None = None

        for target in mentioned_participant_ids:
            if target not in ready_targets:
                continue
            active = conn.execute(
                """
                SELECT * FROM room_tasks
                WHERE conversation_id = ?
                  AND claimed_by_participant_id = ?
                  AND status IN ('claimed', 'running', 'needs_input')
                ORDER BY COALESCE(started_at, claimed_at, created_at) DESC,
                         created_at DESC
                LIMIT 1
                """,
                (conversation, target),
            ).fetchone()
            if active is not None:
                task_id = str(active["task_id"])
                input_id = f"taskinput_{uuid.uuid4().hex}"
                conn.execute(
                    """
                    INSERT INTO room_task_inputs
                        (input_id, task_id, source_message_id,
                         source_sequence, issuer_web_user_id,
                         target_participant_id, body, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        input_id,
                        task_id,
                        source_message_id,
                        source_sequence,
                        issuer_web_user_id,
                        target,
                        message_body,
                        now,
                    ),
                )
                mode = "steer"
                if str(active["status"]) == "needs_input":
                    conn.execute(
                        """
                        UPDATE room_tasks
                        SET status = 'queued',
                            target_kind = 'participants',
                            target_participant_ids_json = ?,
                            claimed_by_participant_id = NULL,
                            claimed_at = NULL,
                            lease_expires_at = NULL,
                            completed_at = NULL,
                            updated_at = ?
                        WHERE task_id = ? AND status = 'needs_input'
                        """,
                        (compact_json([target]), now, task_id),
                    )
                    mode = "resume"
                routes.append(
                    {
                        "target_participant_id": target,
                        "task_id": task_id,
                        "task_input_id": input_id,
                        "mode": mode,
                    }
                )
                continue

            task_id = f"task_{uuid.uuid4().hex}"
            conn.execute(
                """
                INSERT INTO room_tasks
                    (task_id, conversation_id, source_message_id,
                     parent_task_id, issuer_web_user_id,
                     issuer_participant_id, target_kind,
                     target_participant_ids_json, body, status,
                     source_sequence, context_start_sequence,
                     context_end_sequence, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'participants', ?, ?, 'queued',
                        ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    conversation,
                    source_message_id if primary_task_id is None else None,
                    primary_task_id,
                    issuer_web_user_id,
                    issuer_participant_id,
                    compact_json([target]),
                    message_body,
                    source_sequence,
                    max(1, source_sequence - 20),
                    source_sequence,
                    now,
                    now,
                ),
            )
            if primary_task_id is None:
                primary_task_id = task_id
            routes.append(
                {
                    "target_participant_id": target,
                    "task_id": task_id,
                    "task_input_id": None,
                    "mode": "queued",
                }
            )
        return routes

    @staticmethod
    def _room_task_permissions_locked(
        conn: sqlite3.Connection,
        *,
        conversation_id: str,
        web_identity: sqlite3.Row,
    ) -> dict[str, Any]:
        ownership = conn.execute(
            "SELECT web_user_id FROM room_web_owners WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        policy = conn.execute(
            "SELECT * FROM room_task_policies WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        grant = conn.execute(
            "SELECT * FROM room_task_grants WHERE conversation_id = ? "
            "AND web_user_id = ?",
            (conversation_id, str(web_identity["user_id"])),
        ).fetchone()
        owner_user_id = (
            str(ownership["web_user_id"]) if ownership is not None else None
        )
        user_id = str(web_identity["user_id"])
        is_admin = str(web_identity["role"]) == "admin"
        # Orphaned legacy Web rooms are treated as admin-owned until the
        # WebAuthStore migration writes their explicit owner row.
        is_owner = owner_user_id == user_id or (owner_user_id is None and is_admin)
        admin_allowed = bool(policy["allow_global_admin"]) if policy else False
        delegated_assign = bool(grant and grant["can_assign_tasks"])
        delegated_cancel = bool(grant and grant["can_cancel_tasks"])
        can_admin_act = is_admin and (is_owner or admin_allowed)
        return {
            "conversation_id": conversation_id,
            "owner_web_user_id": owner_user_id,
            "is_room_owner": is_owner,
            "allow_global_admin": admin_allowed,
            "can_assign_tasks": bool(is_owner or can_admin_act or delegated_assign),
            "can_cancel_tasks": bool(is_owner or can_admin_act or delegated_cancel),
            "can_manage_task_permissions": bool(is_owner),
        }

    def _resolve_task_targets_locked(
        self,
        conn: sqlite3.Connection,
        *,
        conversation_id: str,
        requested_participant_ids: Sequence[str] | None,
    ) -> tuple[str, list[str]]:
        requested = self._normalize_mentions(requested_participant_ids)
        if len(requested) > MAX_TASK_TARGETS:
            raise ValidationError(
                f"task targets cannot contain more than {MAX_TASK_TARGETS} entries"
            )
        rows = conn.execute(
            """
            SELECT membership.participant_id
            FROM memberships AS membership
            LEFT JOIN web_users AS web_user
              ON web_user.participant_id = membership.participant_id
             AND web_user.active = 1
            WHERE membership.conversation_id = ?
              AND membership.active = 1
              AND web_user.user_id IS NULL
              AND membership.participant_id != ?
            ORDER BY membership.joined_at, membership.participant_id
            """,
            (conversation_id, OWNER_PARTICIPANT_ID),
        ).fetchall()
        available = [str(row["participant_id"]) for row in rows]
        available_set = set(available)
        if requested:
            invalid = [item for item in requested if item not in available_set]
            if invalid:
                raise ConflictError(
                    "task targets must be active Agent members of this room: "
                    + ", ".join(invalid)
                )
            return "participants", requested
        if not available:
            raise ConflictError("这个聊天室当前没有可领取任务的 Agent")
        return "room_agents", available[:MAX_TASK_TARGETS]

    def room_task_permissions(
        self,
        *,
        authorized_session_id: str,
        participant_id: str,
        conversation_id: str,
    ) -> dict[str, Any]:
        session = opaque_id(authorized_session_id, field="authorized_session_id")
        participant = opaque_id(participant_id, field="participant_id")
        conversation = validate_conversation_id(conversation_id)
        with self._transaction() as conn:
            identity = self._require_live_web_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=time.time(),
            )
            self._require_web_room_access_locked(
                conn,
                web_identity=identity,
                conversation_id=conversation,
            )
            self._require_active_room(conn, conversation)
            permissions = self._room_task_permissions_locked(
                conn,
                conversation_id=conversation,
                web_identity=identity,
            )
            members: list[dict[str, Any]] = []
            if permissions["can_manage_task_permissions"]:
                rows = conn.execute(
                    """
                    SELECT web_user.user_id, web_user.username,
                           web_user.display_name, web_user.role,
                           COALESCE(task_grant.can_assign_tasks, 0)
                               AS can_assign_tasks,
                           COALESCE(task_grant.can_cancel_tasks, 0)
                               AS can_cancel_tasks
                    FROM memberships AS membership
                    JOIN web_users AS web_user
                      ON web_user.participant_id = membership.participant_id
                     AND web_user.active = 1
                    LEFT JOIN room_task_grants AS task_grant
                      ON task_grant.conversation_id = membership.conversation_id
                     AND task_grant.web_user_id = web_user.user_id
                    WHERE membership.conversation_id = ?
                      AND membership.active = 1
                    ORDER BY web_user.display_name COLLATE NOCASE,
                             web_user.username COLLATE NOCASE
                    """,
                    (conversation,),
                ).fetchall()
                members = [
                    {
                        "user_id": str(row["user_id"]),
                        "username": str(row["username"]),
                        "display_name": str(row["display_name"]),
                        "role": str(row["role"]),
                        "is_room_owner": str(row["user_id"])
                        == permissions["owner_web_user_id"],
                        "can_assign_tasks": bool(row["can_assign_tasks"]),
                        "can_cancel_tasks": bool(row["can_cancel_tasks"]),
                    }
                    for row in rows
                ]
        return {**permissions, "members": members}

    def room_task_permissions_bulk(
        self,
        *,
        authorized_session_id: str,
        participant_id: str,
        conversation_ids: Sequence[str],
    ) -> dict[str, dict[str, Any]]:
        session = opaque_id(authorized_session_id, field="authorized_session_id")
        participant = opaque_id(participant_id, field="participant_id")
        conversations = [
            validate_conversation_id(value) for value in conversation_ids
        ]
        with self._connection() as conn:
            identity = self._require_live_web_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=time.time(),
            )
            result: dict[str, dict[str, Any]] = {}
            for conversation in conversations:
                self._require_web_room_access_locked(
                    conn,
                    web_identity=identity,
                    conversation_id=conversation,
                )
                result[conversation] = self._room_task_permissions_locked(
                    conn,
                    conversation_id=conversation,
                    web_identity=identity,
                )
            return result

    def update_room_task_policy(
        self,
        *,
        authorized_session_id: str,
        participant_id: str,
        conversation_id: str,
        allow_global_admin: bool,
    ) -> dict[str, Any]:
        if not isinstance(allow_global_admin, bool):
            raise ValidationError("allow_global_admin must be a boolean")
        session = opaque_id(authorized_session_id, field="authorized_session_id")
        participant = opaque_id(participant_id, field="participant_id")
        conversation = validate_conversation_id(conversation_id)
        now = time.time()
        with self._transaction() as conn:
            identity = self._require_live_web_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=now,
            )
            self._require_web_room_access_locked(
                conn,
                web_identity=identity,
                conversation_id=conversation,
            )
            self._require_active_room(conn, conversation)
            permissions = self._room_task_permissions_locked(
                conn,
                conversation_id=conversation,
                web_identity=identity,
            )
            if not permissions["can_manage_task_permissions"]:
                raise AuthorizationError("只有聊天室创建者可以调整任务权限")
            conn.execute(
                """
                INSERT INTO room_task_policies
                    (conversation_id, allow_global_admin,
                     updated_by_web_user_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    allow_global_admin = excluded.allow_global_admin,
                    updated_by_web_user_id = excluded.updated_by_web_user_id,
                    updated_at = excluded.updated_at
                """,
                (
                    conversation,
                    1 if allow_global_admin else 0,
                    str(identity["user_id"]),
                    now,
                ),
            )
        return self.room_task_permissions(
            authorized_session_id=session,
            participant_id=participant,
            conversation_id=conversation,
        )

    def update_room_task_grant(
        self,
        *,
        authorized_session_id: str,
        participant_id: str,
        conversation_id: str,
        target_web_user_id: str,
        can_assign_tasks: bool,
        can_cancel_tasks: bool,
    ) -> dict[str, Any]:
        if not isinstance(can_assign_tasks, bool) or not isinstance(
            can_cancel_tasks, bool
        ):
            raise ValidationError("task grant values must be booleans")
        session = opaque_id(authorized_session_id, field="authorized_session_id")
        participant = opaque_id(participant_id, field="participant_id")
        target = opaque_id(target_web_user_id, field="target_web_user_id")
        conversation = validate_conversation_id(conversation_id)
        now = time.time()
        with self._transaction() as conn:
            identity = self._require_live_web_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=now,
            )
            self._require_web_room_access_locked(
                conn,
                web_identity=identity,
                conversation_id=conversation,
            )
            permissions = self._room_task_permissions_locked(
                conn,
                conversation_id=conversation,
                web_identity=identity,
            )
            if not permissions["can_manage_task_permissions"]:
                raise AuthorizationError("只有聊天室创建者可以调整任务权限")
            target_row = conn.execute(
                """
                SELECT web_user.user_id
                FROM web_users AS web_user
                JOIN memberships AS membership
                  ON membership.participant_id = web_user.participant_id
                 AND membership.conversation_id = ?
                 AND membership.active = 1
                WHERE web_user.user_id = ? AND web_user.active = 1
                """,
                (conversation, target),
            ).fetchone()
            if target_row is None:
                raise NotFoundError("目标用户不是这个聊天室的有效成员")
            if target == permissions["owner_web_user_id"]:
                raise ConflictError("聊天室创建者始终拥有完整任务权限")
            if can_assign_tasks or can_cancel_tasks:
                conn.execute(
                    """
                    INSERT INTO room_task_grants
                        (conversation_id, web_user_id, can_assign_tasks,
                         can_cancel_tasks, granted_by_web_user_id,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(conversation_id, web_user_id) DO UPDATE SET
                        can_assign_tasks = excluded.can_assign_tasks,
                        can_cancel_tasks = excluded.can_cancel_tasks,
                        granted_by_web_user_id = excluded.granted_by_web_user_id,
                        updated_at = excluded.updated_at
                    """,
                    (
                        conversation,
                        target,
                        1 if can_assign_tasks else 0,
                        1 if can_cancel_tasks else 0,
                        str(identity["user_id"]),
                        now,
                        now,
                    ),
                )
            else:
                conn.execute(
                    "DELETE FROM room_task_grants WHERE conversation_id = ? "
                    "AND web_user_id = ?",
                    (conversation, target),
                )
        return self.room_task_permissions(
            authorized_session_id=session,
            participant_id=participant,
            conversation_id=conversation,
        )

    def cancel_web_task(
        self,
        *,
        authorized_session_id: str,
        participant_id: str,
        task_id: str,
    ) -> dict[str, Any]:
        session = opaque_id(authorized_session_id, field="authorized_session_id")
        participant = opaque_id(participant_id, field="participant_id")
        task = opaque_id(task_id, field="task_id")
        now = time.time()
        with self._transaction() as conn:
            identity = self._require_live_web_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=now,
            )
            row = conn.execute(
                "SELECT * FROM room_tasks WHERE task_id = ?", (task,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"unknown task: {task}")
            self._require_web_room_access_locked(
                conn,
                web_identity=identity,
                conversation_id=str(row["conversation_id"]),
            )
            permissions = self._room_task_permissions_locked(
                conn,
                conversation_id=str(row["conversation_id"]),
                web_identity=identity,
            )
            owns_task = str(row["issuer_web_user_id"] or "") == str(
                identity["user_id"]
            )
            if not owns_task and not permissions["can_cancel_tasks"]:
                raise AuthorizationError("你没有取消这个任务的权限")
            if str(row["status"]) in {"completed", "failed"}:
                raise ConflictError("已结束的任务不能取消")
            if str(row["status"]) != "cancelled":
                conn.execute(
                    "UPDATE room_tasks SET status = 'cancelled', "
                    "completed_at = ?, updated_at = ? WHERE task_id = ?",
                    (now, now, task),
                )
            updated = conn.execute(
                "SELECT * FROM room_tasks WHERE task_id = ?", (task,)
            ).fetchone()
        return self._task_payload(updated)

    def claim_next_task(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
    ) -> dict[str, Any] | None:
        participant = opaque_id(participant_id, field="participant_id")
        session = opaque_id(authorized_session_id, field="authorized_session_id")
        now = time.time()
        with self._transaction() as conn:
            session_row = self._require_live_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=now,
            )
            conversation = str(session_row["registered_conversation_id"])
            self._require_membership(conn, participant, conversation)
            # A task executor can disappear after an atomic claim. Requeue only
            # expired non-terminal claims; the live worker renews this lease
            # before and during a long product turn.
            conn.execute(
                "UPDATE room_tasks SET status = 'queued', "
                "claimed_by_participant_id = NULL, claimed_at = NULL, "
                "lease_expires_at = NULL, updated_at = ? "
                "WHERE conversation_id = ? "
                "AND status IN ('claimed', 'running') "
                "AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?",
                (now, conversation, now),
            )
            candidates = conn.execute(
                "SELECT * FROM room_tasks WHERE conversation_id = ? "
                "AND status = 'queued' ORDER BY created_at, task_id LIMIT 100",
                (conversation,),
            ).fetchall()
            selected = next(
                (
                    row
                    for row in candidates
                    if participant
                    in json.loads(str(row["target_participant_ids_json"] or "[]"))
                ),
                None,
            )
            if selected is None:
                return None
            changed = conn.execute(
                "UPDATE room_tasks SET status = 'claimed', "
                "claimed_by_participant_id = ?, claimed_at = ?, "
                "lease_expires_at = ?, updated_at = ? "
                "WHERE task_id = ? AND status = 'queued'",
                (
                    participant,
                    now,
                    now + TASK_CLAIM_LEASE_SECONDS,
                    now,
                    str(selected["task_id"]),
                ),
            ).rowcount
            if changed != 1:
                return None
            updated = conn.execute(
                "SELECT * FROM room_tasks WHERE task_id = ?",
                (str(selected["task_id"]),),
            ).fetchone()
        return self._task_payload(updated)

    def wait_next_task(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        wait_seconds: float = 20.0,
    ) -> dict[str, Any]:
        bounded_wait = max(0.0, min(float(wait_seconds), 30.0))
        deadline = time.monotonic() + bounded_wait
        while True:
            claimed = self.claim_next_task(
                participant_id=participant_id,
                authorized_session_id=authorized_session_id,
            )
            if claimed is not None:
                return {"task": claimed}
            if time.monotonic() >= deadline:
                return {"task": None}
            time.sleep(min(self.poll_interval_seconds, deadline - time.monotonic()))

    @staticmethod
    def _task_input_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "input_id": str(row["input_id"]),
            "task_id": str(row["task_id"]),
            "source_message_id": str(row["source_message_id"]),
            "source_sequence": int(row["source_sequence"]),
            "issuer_web_user_id": str(row["issuer_web_user_id"]),
            "issuer_username": str(row["issuer_username"] or ""),
            "issuer_display_name": str(row["issuer_display_name"] or ""),
            "issuer_role": str(row["issuer_role"] or "user"),
            "target_participant_id": str(row["target_participant_id"]),
            "body": str(row["body"]),
            "delivery_count": int(row["delivery_count"] or 0),
            "first_delivered_at": (
                float(row["first_delivered_at"])
                if row["first_delivered_at"] is not None
                else None
            ),
            "last_delivered_at": (
                float(row["last_delivered_at"])
                if row["last_delivered_at"] is not None
                else None
            ),
            "applied_at": (
                float(row["applied_at"])
                if row["applied_at"] is not None
                else None
            ),
            "created_at": float(row["created_at"]),
        }

    def poll_agent_task_inputs(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        task_id: str,
        limit: int = 50,
    ) -> dict[str, Any]:
        participant = opaque_id(participant_id, field="participant_id")
        session = opaque_id(authorized_session_id, field="authorized_session_id")
        task = opaque_id(task_id, field="task_id")
        bounded_limit = max(1, min(int(limit), 100))
        now = time.time()
        with self._transaction() as conn:
            session_row = self._require_live_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=now,
            )
            task_row = conn.execute(
                "SELECT * FROM room_tasks WHERE task_id = ?",
                (task,),
            ).fetchone()
            if task_row is None:
                raise NotFoundError(f"unknown task: {task}")
            if str(task_row["conversation_id"]) != str(
                session_row["registered_conversation_id"]
            ):
                raise AuthorizationError("task belongs to another room")
            if str(task_row["claimed_by_participant_id"] or "") != participant:
                raise AuthorizationError(
                    "only the Agent that claimed this task may read task inputs"
                )
            if str(task_row["status"]) not in {"claimed", "running", "needs_input"}:
                return {"task_id": task, "inputs": [], "count": 0}
            rows = conn.execute(
                """
                SELECT task_input.*,
                       web_user.username AS issuer_username,
                       web_user.role AS issuer_role,
                       profile.display_name AS issuer_display_name
                FROM room_task_inputs AS task_input
                JOIN web_users AS web_user
                  ON web_user.user_id = task_input.issuer_web_user_id
                JOIN participants AS profile
                  ON profile.participant_id = web_user.participant_id
                WHERE task_input.task_id = ?
                  AND task_input.applied_at IS NULL
                  AND (
                      task_input.last_delivered_at IS NULL
                      OR task_input.last_delivered_at <= ?
                  )
                ORDER BY task_input.source_sequence, task_input.created_at,
                         task_input.input_id
                LIMIT ?
                """,
                (task, now - TASK_INPUT_REDELIVERY_SECONDS, bounded_limit),
            ).fetchall()
            if rows:
                input_ids = [str(row["input_id"]) for row in rows]
                placeholders = ",".join("?" for _ in input_ids)
                conn.execute(
                    f"""
                    UPDATE room_task_inputs
                    SET first_delivered_at = COALESCE(first_delivered_at, ?),
                        last_delivered_at = ?,
                        delivery_count = delivery_count + 1
                    WHERE input_id IN ({placeholders})
                      AND applied_at IS NULL
                    """,
                    (now, now, *input_ids),
                )
                rows = conn.execute(
                    f"""
                    SELECT task_input.*,
                           web_user.username AS issuer_username,
                           web_user.role AS issuer_role,
                           profile.display_name AS issuer_display_name
                    FROM room_task_inputs AS task_input
                    JOIN web_users AS web_user
                      ON web_user.user_id = task_input.issuer_web_user_id
                    JOIN participants AS profile
                      ON profile.participant_id = web_user.participant_id
                    WHERE task_input.input_id IN ({placeholders})
                    ORDER BY task_input.source_sequence, task_input.created_at,
                             task_input.input_id
                    """,
                    input_ids,
                ).fetchall()
        inputs = [self._task_input_payload(row) for row in rows]
        return {"task_id": task, "inputs": inputs, "count": len(inputs)}

    def acknowledge_agent_task_inputs(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        task_id: str,
        input_ids: Sequence[str],
    ) -> dict[str, Any]:
        participant = opaque_id(participant_id, field="participant_id")
        session = opaque_id(authorized_session_id, field="authorized_session_id")
        task = opaque_id(task_id, field="task_id")
        normalized_ids = [
            opaque_id(value, field="input_id")
            for value in dict.fromkeys(input_ids)
        ]
        if not normalized_ids:
            return {"task_id": task, "applied_input_ids": [], "count": 0}
        if len(normalized_ids) > 100:
            raise ValidationError("input_ids cannot contain more than 100 entries")
        now = time.time()
        with self._transaction() as conn:
            session_row = self._require_live_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=now,
            )
            task_row = conn.execute(
                "SELECT * FROM room_tasks WHERE task_id = ?",
                (task,),
            ).fetchone()
            if task_row is None:
                raise NotFoundError(f"unknown task: {task}")
            if str(task_row["conversation_id"]) != str(
                session_row["registered_conversation_id"]
            ):
                raise AuthorizationError("task belongs to another room")
            if str(task_row["claimed_by_participant_id"] or "") != participant:
                raise AuthorizationError(
                    "only the Agent that claimed this task may acknowledge inputs"
                )
            placeholders = ",".join("?" for _ in normalized_ids)
            matched = conn.execute(
                f"SELECT input_id FROM room_task_inputs "
                f"WHERE task_id = ? AND input_id IN ({placeholders})",
                (task, *normalized_ids),
            ).fetchall()
            matched_ids = [str(row["input_id"]) for row in matched]
            if set(matched_ids) != set(normalized_ids):
                raise ConflictError("one or more task inputs do not belong to this task")
            conn.execute(
                f"UPDATE room_task_inputs SET applied_at = COALESCE(applied_at, ?) "
                f"WHERE task_id = ? AND input_id IN ({placeholders})",
                (now, task, *normalized_ids),
            )
        return {
            "task_id": task,
            "applied_input_ids": sorted(matched_ids),
            "count": len(matched_ids),
        }

    def update_agent_task(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        task_id: str,
        status: str,
        result_summary: str | None = None,
        execution_cwd: str | None = None,
        execution_thread_id: str | None = None,
    ) -> dict[str, Any]:
        participant = opaque_id(participant_id, field="participant_id")
        session = opaque_id(authorized_session_id, field="authorized_session_id")
        task = opaque_id(task_id, field="task_id")
        normalized_status = str(status or "").strip().lower()
        if normalized_status not in {
            "running",
            "needs_input",
            "completed",
            "failed",
        }:
            raise ValidationError("unsupported Agent task status")
        summary = str(result_summary or "").strip()
        cwd = str(execution_cwd or "").strip()
        thread_id = str(execution_thread_id or "").strip()
        if len(summary) > 20_000:
            raise ValidationError("task result summary is too long")
        if len(cwd) > 2_000 or len(thread_id) > 256:
            raise ValidationError("task execution metadata is too long")
        now = time.time()
        with self._transaction() as conn:
            session_row = self._require_live_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=now,
            )
            row = conn.execute(
                "SELECT * FROM room_tasks WHERE task_id = ?", (task,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"unknown task: {task}")
            if str(row["conversation_id"]) != str(
                session_row["registered_conversation_id"]
            ):
                raise AuthorizationError("task belongs to another room")
            if str(row["claimed_by_participant_id"] or "") != participant:
                raise AuthorizationError("only the Agent that claimed this task may update it")
            if str(row["status"]) == "cancelled":
                raise ConflictError("task was cancelled")
            current_status = str(row["status"])
            if current_status in {"completed", "failed"}:
                if current_status == normalized_status:
                    return self._task_payload(row)
                raise ConflictError("task is already finished")
            if current_status == "needs_input" and normalized_status in {
                "running",
                "completed",
            }:
                # A model can deliberately pause a task for missing local
                # authority or user context. The resident lease heartbeat and
                # wrapper closeout must not silently overwrite that decision.
                return self._task_payload(row)
            completed_at = now if normalized_status in {"completed", "failed"} else None
            lease_expires_at = (
                now + TASK_CLAIM_LEASE_SECONDS
                if normalized_status in {"running", "needs_input"}
                else None
            )
            existing_summary = str(row["result_summary"] or "")
            existing_cwd = str(row["execution_cwd"] or "")
            existing_thread_id = str(row["execution_thread_id"] or "")
            visible_task_changed = (
                normalized_status != current_status
                or (normalized_status == "running" and row["started_at"] is None)
                or (bool(summary) and summary != existing_summary)
                or (bool(cwd) and cwd != existing_cwd)
                or (bool(thread_id) and thread_id != existing_thread_id)
            )
            visible_updated_at = now if visible_task_changed else float(row["updated_at"])
            conn.execute(
                """
                UPDATE room_tasks
                SET status = ?,
                    started_at = CASE
                        WHEN ? = 'running' THEN COALESCE(started_at, ?)
                        ELSE started_at
                    END,
                    completed_at = COALESCE(?, completed_at),
                    result_summary = CASE WHEN ? != '' THEN ? ELSE result_summary END,
                    execution_cwd = CASE WHEN ? != '' THEN ? ELSE execution_cwd END,
                    execution_thread_id = CASE WHEN ? != '' THEN ?
                                               ELSE execution_thread_id END,
                    lease_expires_at = ?,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (
                    normalized_status,
                    normalized_status,
                    now,
                    completed_at,
                    summary,
                    summary,
                    cwd,
                    cwd,
                    thread_id,
                    thread_id,
                    lease_expires_at,
                    visible_updated_at,
                    task,
                ),
            )
            updated = conn.execute(
                "SELECT * FROM room_tasks WHERE task_id = ?", (task,)
            ).fetchone()
        return self._task_payload(updated)

    def delegate_agent_task(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        parent_task_id: str,
        body_text: str,
        target_participant_ids: Sequence[str],
    ) -> dict[str, Any]:
        participant = opaque_id(participant_id, field="participant_id")
        session = opaque_id(authorized_session_id, field="authorized_session_id")
        parent = opaque_id(parent_task_id, field="parent_task_id")
        normalized_body = body(body_text)
        now = time.time()
        with self._transaction() as conn:
            session_row = self._require_live_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=now,
            )
            parent_row = conn.execute(
                "SELECT * FROM room_tasks WHERE task_id = ?", (parent,)
            ).fetchone()
            if parent_row is None:
                raise NotFoundError(f"unknown parent task: {parent}")
            if str(parent_row["claimed_by_participant_id"] or "") != participant:
                raise AuthorizationError("only the task coordinator may delegate it")
            conversation = str(parent_row["conversation_id"])
            if conversation != str(session_row["registered_conversation_id"]):
                raise AuthorizationError("parent task belongs to another room")
            target_kind, target_ids = self._resolve_task_targets_locked(
                conn,
                conversation_id=conversation,
                requested_participant_ids=target_participant_ids,
            )
            child_id = f"task_{uuid.uuid4().hex}"
            conn.execute(
                """
                INSERT INTO room_tasks
                    (task_id, conversation_id, source_message_id, parent_task_id,
                     issuer_web_user_id, issuer_participant_id, target_kind,
                     target_participant_ids_json, body, status,
                     created_at, updated_at)
                VALUES (?, ?, NULL, ?, NULL, ?, ?, ?, ?, 'queued', ?, ?)
                """,
                (
                    child_id,
                    conversation,
                    parent,
                    participant,
                    target_kind,
                    compact_json(target_ids),
                    normalized_body,
                    now,
                    now,
                ),
            )
            child = conn.execute(
                "SELECT * FROM room_tasks WHERE task_id = ?", (child_id,)
            ).fetchone()
        return self._task_payload(child)

    @staticmethod
    def _task_payload(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            raise NotFoundError("task row disappeared")
        return {
            "task_id": str(row["task_id"]),
            "conversation_id": str(row["conversation_id"]),
            "source_message_id": (
                str(row["source_message_id"])
                if row["source_message_id"] is not None
                else None
            ),
            "parent_task_id": (
                str(row["parent_task_id"])
                if row["parent_task_id"] is not None
                else None
            ),
            "issuer_web_user_id": (
                str(row["issuer_web_user_id"])
                if row["issuer_web_user_id"] is not None
                else None
            ),
            "issuer_participant_id": str(row["issuer_participant_id"]),
            "target_kind": str(row["target_kind"]),
            "target_participant_ids": json.loads(
                str(row["target_participant_ids_json"] or "[]")
            ),
            "body": str(row["body"]),
            "status": str(row["status"]),
            "claimed_by_participant_id": (
                str(row["claimed_by_participant_id"])
                if row["claimed_by_participant_id"] is not None
                else None
            ),
            "claimed_at": (
                float(row["claimed_at"]) if row["claimed_at"] is not None else None
            ),
            "lease_expires_at": (
                float(row["lease_expires_at"])
                if row["lease_expires_at"] is not None
                else None
            ),
            "started_at": (
                float(row["started_at"]) if row["started_at"] is not None else None
            ),
            "completed_at": (
                float(row["completed_at"])
                if row["completed_at"] is not None
                else None
            ),
            "result_summary": (
                str(row["result_summary"])
                if row["result_summary"] is not None
                else None
            ),
            "execution_cwd": (
                str(row["execution_cwd"])
                if row["execution_cwd"] is not None
                else None
            ),
            "execution_thread_id": (
                str(row["execution_thread_id"])
                if row["execution_thread_id"] is not None
                else None
            ),
            "source_sequence": (
                int(row["source_sequence"])
                if "source_sequence" in row.keys()
                and row["source_sequence"] is not None
                else None
            ),
            "context_start_sequence": (
                int(row["context_start_sequence"])
                if "context_start_sequence" in row.keys()
                and row["context_start_sequence"] is not None
                else None
            ),
            "context_end_sequence": (
                int(row["context_end_sequence"])
                if "context_end_sequence" in row.keys()
                and row["context_end_sequence"] is not None
                else None
            ),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    def forward_web_message(
        self,
        *,
        authorized_session_id: str,
        participant_id: str,
        source_message_id: str,
        target_conversation_id: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Explicitly copy one message into another room with durable provenance."""

        session = opaque_id(authorized_session_id, field="authorized_session_id")
        participant = opaque_id(participant_id, field="participant_id")
        source_id = opaque_id(source_message_id, field="source_message_id")
        target = validate_conversation_id(target_conversation_id)
        normalized_note = str(note or "").strip()
        if len(normalized_note) > 2_000 or any(
            ord(character) < 32 and character not in "\t\n\r"
            for character in normalized_note
        ):
            raise ValidationError("forward note must contain at most 2000 characters")
        with self._connection() as conn:
            web_identity = self._require_live_web_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=time.time(),
            )
            if str(web_identity["role"]) != "admin":
                raise AuthorizationError("只有管理员可以跨聊天室转发消息")
            source = conn.execute(
                """
                SELECT message.*, sender.display_name AS sender_display_name,
                       sender.client_type AS sender_client_type
                FROM messages AS message
                JOIN participants AS sender
                  ON sender.participant_id = message.sender_participant_id
                WHERE message.message_id = ?
                """,
                (source_id,),
            ).fetchone()
            if source is None:
                raise NotFoundError(f"unknown source message: {source_id}")
            if str(source["message_kind"]) == "forward":
                raise ConflictError(
                    "forward chains are not allowed; forward the original message"
                )
            if str(source["conversation_id"]) == target:
                raise ConflictError("cross-room forward target must differ")
            source_label = (
                str(source["sender_display_name"])
                or str(source["sender_client_type"])
            )
            header = (
                "【管理员显式转发 · 来源「"
                f"{source['conversation_id']}」#"
                f"{int(source['room_sequence'] or source['sequence'])} · "
                f"{source_label}】"
            )
            sections = [header]
            if normalized_note:
                sections.append(f"转发说明：{normalized_note}")
            sections.append(f"原文：\n{source['body']}")
            forwarded_body = "\n\n".join(sections)
            # Validate before entering send's write transaction, including the
            # small provenance header added around the immutable source body.
            forwarded_body = body(forwarded_body)
        return self.send(
            authorized_session_id=session,
            sender_participant_id=participant,
            conversation_id=target,
            body_text=forwarded_body,
            audience_kind="room",
            audience_value="*",
            _web_user=True,
            _message_kind="forward",
            _forwarded_from_message_id=source_id,
            _suppress_chat_authorization=True,
            _suppress_mention_inference=True,
        )

    @staticmethod
    def _chat_authorization_applies_locked(
        conn: sqlite3.Connection,
        grant: sqlite3.Row,
        *,
        recipient_participant_id: str | None,
    ) -> bool:
        if recipient_participant_id is None:
            return True
        recipient = str(recipient_participant_id)
        target_kind = str(grant["target_kind"])
        targets = set(
            json.loads(str(grant["target_participant_ids_json"] or "[]"))
        )
        if target_kind in {"participants", "reply_author"}:
            return recipient in targets
        if target_kind != "room_agents":
            return False
        if recipient not in targets:
            return False
        membership = conn.execute(
            """
            SELECT 1
            FROM memberships AS membership
            LEFT JOIN web_users AS web_user
              ON web_user.participant_id = membership.participant_id
            WHERE membership.conversation_id = ?
              AND membership.participant_id = ?
              AND membership.active = 1
              AND web_user.user_id IS NULL
              AND membership.participant_id != ?
            """,
            (
                str(grant["conversation_id"]),
                recipient,
                OWNER_PARTICIPANT_ID,
            ),
        ).fetchone()
        return membership is not None

    @classmethod
    def _chat_authorization_for_message_locked(
        cls,
        conn: sqlite3.Connection,
        *,
        message_id: str,
        recipient_participant_id: str | None,
    ) -> dict[str, Any] | None:
        grant = conn.execute(
            "SELECT * FROM chat_authorization_grants WHERE source_message_id = ?",
            (message_id,),
        ).fetchone()
        if grant is None or not cls._chat_authorization_applies_locked(
            conn,
            grant,
            recipient_participant_id=recipient_participant_id,
        ):
            return None
        revoked_at = (
            float(grant["revoked_at"])
            if grant["revoked_at"] is not None
            else None
        )
        return {
            "kind": str(grant["authority_kind"]),
            "source_message_id": str(grant["source_message_id"]),
            "issuer_user_id": str(grant["issuer_web_user_id"]),
            "issuer_username": str(grant["issuer_username_snapshot"]),
            "issuer_role_at_send": str(grant["issuer_role_snapshot"]),
            "issuer_participant_id": str(grant["issuer_participant_id"]),
            "body_sha256": str(grant["body_sha256"]),
            "target_kind": str(grant["target_kind"]),
            "target_participant_ids": json.loads(
                str(grant["target_participant_ids_json"] or "[]")
            ),
            "issued_at": float(grant["created_at"]),
            "applies_to_recipient": True,
            "status": (
                "legacy_frozen"
                if str(grant["authority_kind"]) == "legacy_frozen"
                else ("revoked" if revoked_at is not None else "active")
            ),
            "revoked_at": revoked_at,
            "revoked_by_web_user_id": (
                str(grant["revoked_by_web_user_id"])
                if grant["revoked_by_web_user_id"] is not None
                else None
            ),
            "revocation_reason": (
                str(grant["revocation_reason"])
                if grant["revocation_reason"] is not None
                else None
            ),
            "semantics": (
                "ordinary_chat_only"
                if str(grant["authority_kind"]) == "legacy_frozen"
                else "natural_language_minimum_necessary"
            ),
        }

    def revoke_chat_authorization(
        self,
        *,
        source_message_id: str,
        revoked_by_web_user_id: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        message_id = opaque_id(source_message_id, field="source_message_id")
        administrator = opaque_id(
            revoked_by_web_user_id,
            field="revoked_by_web_user_id",
        )
        normalized_reason = (
            alias(reason, field="revocation_reason") if reason else None
        )
        now = time.time()
        with self._transaction() as conn:
            self._require_active_admin_locked(conn, administrator)
            grant = conn.execute(
                "SELECT * FROM chat_authorization_grants WHERE source_message_id = ?",
                (message_id,),
            ).fetchone()
            if grant is None:
                raise NotFoundError(
                    f"message {message_id} is not an admin chat authority source"
                )
            if grant["revoked_at"] is None:
                conn.execute(
                    """
                    UPDATE chat_authorization_grants
                    SET revoked_at = ?, revoked_by_web_user_id = ?,
                        revocation_reason = ?
                    WHERE source_message_id = ?
                    """,
                    (now, administrator, normalized_reason, message_id),
                )
            payload = self._chat_authorization_for_message_locked(
                conn,
                message_id=message_id,
                recipient_participant_id=None,
            )
        return payload or {}

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
                    SET state = 'cancelled', reasons_json = ?, actionable = 0
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
        self.archive_stale_rooms()
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
        return {
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
                self._require_live_room_session(
                    conn,
                    session_id=opaque_id(
                        authorized_session_id,
                        field="authorized_session_id",
                    ),
                    participant_id=participant,
                    conversation_id=str(row["conversation_id"]),
                    now=now,
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
                self._require_live_room_session(
                    conn,
                    session_id=opaque_id(
                        authorized_session_id,
                        field="authorized_session_id",
                    ),
                    participant_id=participant,
                    conversation_id=str(row["conversation_id"]),
                    now=now,
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
                self._require_live_room_session(
                    conn,
                    session_id=opaque_id(
                        authorized_session_id,
                        field="authorized_session_id",
                    ),
                    participant_id=participant,
                    conversation_id=str(row["conversation_id"]),
                    now=now,
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
    def _require_web_room_access_locked(
        conn: sqlite3.Connection,
        *,
        web_identity: sqlite3.Row,
        conversation_id: str,
    ) -> dict[str, Any]:
        room = conn.execute(
            "SELECT status FROM rooms WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if room is None:
            raise NotFoundError(f"unknown conversation: {conversation_id}")
        user_id = str(web_identity["user_id"])
        is_admin = str(web_identity["role"]) == "admin"
        owner = conn.execute(
            "SELECT 1 FROM room_web_owners "
            "WHERE conversation_id = ? AND web_user_id = ?",
            (conversation_id, user_id),
        ).fetchone()
        member = conn.execute(
            "SELECT access_role FROM room_web_members "
            "WHERE conversation_id = ? AND web_user_id = ? AND active = 1",
            (conversation_id, user_id),
        ).fetchone()
        is_owner = owner is not None
        if not is_admin and not is_owner and member is None:
            raise AuthorizationError("你无权访问这个聊天室")
        return {
            "conversation_id": conversation_id,
            "room_status": str(room["status"]),
            "is_admin": is_admin,
            "is_room_owner": is_owner,
            "access_role": (
                "admin"
                if is_admin
                else "owner"
                if is_owner
                else str(member["access_role"])
            ),
        }

    @staticmethod
    def _room_web_permissions_locked(
        conn: sqlite3.Connection,
        *,
        web_user_id: str,
        conversation_id: str,
    ) -> dict[str, Any]:
        web_user = conn.execute(
            "SELECT user_id, role FROM web_users "
            "WHERE user_id = ? AND active = 1",
            (web_user_id,),
        ).fetchone()
        if web_user is None:
            raise AuthenticationError("active Web user is required")
        room = conn.execute(
            "SELECT status FROM rooms WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if room is None:
            raise NotFoundError(f"unknown conversation: {conversation_id}")
        owner = conn.execute(
            "SELECT 1 FROM room_web_owners "
            "WHERE conversation_id = ? AND web_user_id = ?",
            (conversation_id, web_user_id),
        ).fetchone()
        access = conn.execute(
            "SELECT access_role FROM room_web_members "
            "WHERE conversation_id = ? AND web_user_id = ? AND active = 1",
            (conversation_id, web_user_id),
        ).fetchone()
        return BridgeStore._room_web_permission_payload(
            conversation_id=conversation_id,
            room_status=str(room["status"]),
            is_global_admin=str(web_user["role"]) == "admin",
            is_room_owner=owner is not None,
            access_role=(
                str(access["access_role"]) if access is not None else None
            ),
        )

    @staticmethod
    def _room_web_permission_payload(
        *,
        conversation_id: str,
        room_status: str,
        is_global_admin: bool,
        is_room_owner: bool,
        access_role: str | None,
    ) -> dict[str, Any]:
        is_moderator = access_role == "moderator"
        if not is_global_admin and not is_room_owner and access_role is None:
            raise AuthorizationError("你无权访问这个聊天室")
        room_role = (
            "global_admin"
            if is_global_admin
            else "owner"
            if is_room_owner
            else "moderator"
            if is_moderator
            else "member"
        )
        can_manage = is_global_admin or is_room_owner or is_moderator
        return {
            "conversation_id": conversation_id,
            "room_status": room_status,
            "room_role": room_role,
            "is_global_admin": is_global_admin,
            "is_room_owner": is_room_owner,
            "is_room_moderator": is_moderator,
            "can_wake_all": can_manage,
            "can_manage_wake_policy": can_manage,
            "can_manage_highlights": can_manage,
            "can_manage_web_members": can_manage,
            "can_delegate_room_moderators": is_global_admin or is_room_owner,
            "can_invite_agents": can_manage,
            "can_kick_agents": can_manage,
            "can_rename_room": is_global_admin or is_room_owner,
        }

    @staticmethod
    def _web_user_room_permission_payload(
        row: sqlite3.Row | None,
    ) -> dict[str, Any]:
        if row is None:
            raise NotFoundError("web user row disappeared")
        return {
            "user_id": str(row["user_id"]),
            "username": str(row["username"]),
            "display_name": str(row["display_name"]),
            "signature": str(row["signature"]),
            "avatar_key": str(row["avatar_key"] or "auto"),
            "can_create_rooms": bool(row["can_create_rooms"]),
            "room_limit": int(row["room_limit"]),
            "owned_active_room_count": int(
                row["owned_active_room_count"] or 0
            ),
            "owned_room_count": int(row["owned_room_count"] or 0),
        }

    @staticmethod
    def _room_web_user_payload(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            raise NotFoundError("web room member row disappeared")
        return {
            "user_id": str(row["user_id"]),
            "username": str(row["username"]),
            "display_name": str(row["display_name"]),
            "signature": str(row["signature"]),
            "avatar_key": str(row["avatar_key"] or "auto"),
            "is_room_owner": bool(row["is_room_owner"]),
            "has_room_access": bool(row["room_access_active"])
            or bool(row["is_room_owner"]),
            "access_role": (
                "owner"
                if bool(row["is_room_owner"])
                else str(row["access_role"] or "member")
            ),
            "access_updated_at": (
                float(row["access_updated_at"])
                if row["access_updated_at"] is not None
                else None
            ),
        }

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
