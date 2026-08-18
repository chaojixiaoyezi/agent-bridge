"""Invitation, connector enrollment, rotation, and component presence state."""

from __future__ import annotations

import json
import math
import secrets
import sqlite3
import time
import uuid
from collections.abc import Sequence
from typing import Any

from .avatars import normalize_avatar_key
from .store_constants import (
    CONNECTOR_COMPONENTS,
    CONNECTOR_ONLINE_WINDOW_SECONDS,
    DEFAULT_INVITATION_TTL_SECONDS,
    DEFAULT_SESSION_TTL_SECONDS,
    ENROLLMENT_PREVIOUS_GRACE_SECONDS,
    INVITATION_ADAPTERS,
    INVITATION_MODES,
    MAX_INVITATION_TTL_SECONDS,
    NATIVE_TUI_ADAPTERS,
)
from .store_errors import (
    AuthenticationError,
    AuthorizationError,
    AvatarRateLimitError,
    BridgeError,
    ConflictError,
    NotFoundError,
)
from .validation import (
    MAX_AGENT_USERNAME_CHARS,
    MAX_CLIENT_IDENTITY_CHARS,
    ValidationError,
    agent_username,
    compact_json,
    conversation_id as validate_conversation_id,
    opaque_id,
    product_username,
    string_tokens,
    token,
)


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


class AgentConnectorMixin:
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
            "native_delivery": {
                "mode": str(row["native_delivery_mode"] or "legacy_shadow"),
                "lease_id": (
                    str(row["native_lease_id"])
                    if row["native_lease_id"] is not None
                    else None
                ),
                "process_epoch": (
                    str(row["native_process_epoch"])
                    if row["native_process_epoch"] is not None
                    else None
                ),
                "lease_expires_at": (
                    float(row["native_lease_expires_at"])
                    if row["native_lease_expires_at"] is not None
                    else None
                ),
                "binding_source": (
                    str(row["native_binding_source"])
                    if row["native_binding_source"] is not None
                    else None
                ),
                "lease_active": bool(
                    row["native_lease_id"] is not None
                    and row["native_lease_expires_at"] is not None
                    and float(row["native_lease_expires_at"]) > now
                ),
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
