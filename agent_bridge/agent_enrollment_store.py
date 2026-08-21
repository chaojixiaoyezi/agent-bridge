from __future__ import annotations

import json
import secrets
import sqlite3
import time
import uuid
from collections.abc import Sequence
from typing import Any

from .avatars import normalize_avatar_key
from .store_constants import (
    CONNECTOR_COMPONENTS,
    DEFAULT_SESSION_TTL_SECONDS,
    NATIVE_TUI_ADAPTERS,
)
from .store_errors import (
    AuthenticationError,
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
    opaque_id,
    product_username,
    string_tokens,
    token,
)


class AgentEnrollmentMixin:
    """Invitation acceptance, bound identity, enrollment, and component presence."""

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
            raise ValidationError("connector_binding_version must be 1 or 2") from exc
        if requested_binding_version not in {1, 2} or (
            not isinstance(connector_binding_version, int)
            and str(connector_binding_version).strip() != str(requested_binding_version)
        ):
            raise ValidationError("connector_binding_version must be 1 or 2")
        normalized_enrollment = None
        if enrollment_token is not None:
            normalized_enrollment = opaque_id(
                enrollment_token,
                field="enrollment_token",
            )
            if (
                not normalized_enrollment.startswith("enroll_")
                or len(normalized_enrollment) < 40
            ):
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
            if (
                native_tui_invitation
                and native_tui_binding_expected
                and (
                    not tui_confirmed
                    or normalized_tui_endpoint is None
                    or normalized_tui_session is None
                )
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
                           connector.tui_native_session_id,
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
                if (
                    invitation_tui_adapter == "codex"
                    and endpoint_owner is not None
                    and normalized_tui_session is not None
                    and not self._constant_time_eq(
                        normalized_tui_session,
                        str(endpoint_owner["tui_native_session_id"] or ""),
                    )
                ):
                    raise ConflictError(
                        "native TUI endpoint is already bound to another session"
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
            shared_codex_tui = bool(
                invitation_tui_adapter == "codex"
                and endpoint_owner is not None
                and normalized_tui_session is not None
                and self._constant_time_eq(
                    normalized_tui_session,
                    str(endpoint_owner["tui_native_session_id"] or ""),
                )
            )
            if duplicate_session is not None and not shared_codex_tui and (
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
                    existing_connector["requested_username"] or assigned_username
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
                    raise ConflictError(f"Agent invitation is {invitation_status}")
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
                    if normalized_avatar != str(avatar_profile["avatar_key"] or "auto"):
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
                "invitation_reusable": (str(invitation["reuse_policy"]) == "reusable"),
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
        if not bool(existing["has_active_connector"]) and (
            bool(existing["reinvite_required"]) or bool(existing["blocked_in_room"])
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
        try:
            raw_setup_detail = (
                connector["setup_detail_json"]
                if "setup_detail_json" in connector.keys()
                else "{}"
            )
            setup_detail = json.loads(str(raw_setup_detail or "{}"))
        except (TypeError, json.JSONDecodeError):
            setup_detail = {}
        if (
            isinstance(setup_detail, dict)
            and str(setup_detail.get("duty_mode") or "") == "direct_tui"
        ):
            return {"mcp"}
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
                       connector.setup_detail_json,
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
