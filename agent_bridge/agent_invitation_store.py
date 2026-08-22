from __future__ import annotations

import json
import math
import secrets
import sqlite3
import time
import uuid
from typing import Any

from .store_constants import (
    CONNECTOR_ONLINE_WINDOW_SECONDS,
    DEFAULT_INVITATION_TTL_SECONDS,
    ENROLLMENT_PREVIOUS_GRACE_SECONDS,
    INVITATION_ADAPTERS,
    INVITATION_MODES,
    MAX_INVITATION_TTL_SECONDS,
    NATIVE_TUI_ADAPTERS,
)
from .store_errors import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    NotFoundError,
)
from .validation import (
    ValidationError,
    compact_json,
    conversation_id as validate_conversation_id,
    opaque_id,
    token,
)


class AgentInvitationMixin:
    """Invitation issuance, listing, revocation, and credential governance."""

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
        max_uses = int(row["max_uses"]) if row["max_uses"] is not None else None
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
                float(row["revoked_at"]) if row["revoked_at"] is not None else None
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
            row["revoked_at"] is not None or str(row["invitation_status"]) == "revoked"
        )
        if revoked:
            resident_status = "revoked"
        elif (
            setup_status == "configured"
            and last_seen is not None
            and (now - last_seen <= CONNECTOR_ONLINE_WINDOW_SECONDS)
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
        native_delivery_mode = str(
            row["native_delivery_mode"] or "legacy_shadow"
        )
        native_lease_expires_at = (
            float(row["native_lease_expires_at"])
            if row["native_lease_expires_at"] is not None
            else None
        )
        native_lease_active = bool(
            row["native_lease_id"] is not None
            and native_lease_expires_at is not None
            and native_lease_expires_at > now
        )
        raw_tui_state = str(row["tui_state"] or "unbound")
        effective_tui_state = (
            "offline"
            if native_delivery_mode == "native_preferred"
            and raw_tui_state in {"online", "busy"}
            and not native_lease_active
            else raw_tui_state
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
                "credential_version": int(row["enrollment_credential_version"] or 1),
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
                "state": effective_tui_state,
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
                "mode": native_delivery_mode,
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
                "lease_expires_at": native_lease_expires_at,
                "binding_source": (
                    str(row["native_binding_source"])
                    if row["native_binding_source"] is not None
                    else None
                ),
                "lease_active": native_lease_active,
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
            room = self._require_active_room(conn, conversation)
            if str(room["room_kind"]) == "integration" and reusable:
                raise ValidationError("整合聊天室只支持单次 Agent 邀请")
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
            validate_conversation_id(conversation_id) if conversation_id else None
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
                    raise AuthorizationError("聊天室管理员只能查看指定聊天室的邀请")
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
            if (
                row["revoked_at"] is not None
                or str(row["invitation_status"]) == "revoked"
            ):
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
            raise ValidationError("new_enrollment_token must be a strong enroll_ token")
        current_hash = self._secret_hash(current_token)
        next_hash = self._secret_hash(next_token)
        now = time.time()
        with self._transaction() as conn:
            row = self._agent_connector_row_locked(conn, connector)
            if (
                row["revoked_at"] is not None
                or str(row["invitation_status"]) == "revoked"
            ):
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
                previous_valid and self._constant_time_eq(current_hash, stored_previous)
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
