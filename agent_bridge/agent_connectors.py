"""Invitation, connector enrollment, rotation, and component presence state."""

from __future__ import annotations


from .agent_enrollment_store import AgentEnrollmentMixin
from .agent_invitation_store import AgentInvitationMixin


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
    native_delivery_mode TEXT NOT NULL DEFAULT 'legacy_shadow'
        CHECK (native_delivery_mode IN ('legacy_shadow', 'native_preferred')),
    native_lease_id TEXT,
    native_process_epoch TEXT,
    native_lease_expires_at REAL,
    native_binding_source TEXT,
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


class AgentConnectorMixin(AgentInvitationMixin, AgentEnrollmentMixin):
    """Compose invitation governance and connector enrollment storage."""
