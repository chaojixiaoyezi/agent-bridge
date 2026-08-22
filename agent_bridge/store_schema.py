"""Base SQLite schema owned by the BridgeStore composition root."""

from __future__ import annotations

from .store_constants import DEFAULT_SESSION_TTL_SECONDS, MESSAGE_COOLDOWN_SECONDS


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
    room_kind TEXT NOT NULL DEFAULT 'chat'
        CHECK (room_kind IN ('chat', 'integration')),
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
