"""Agent registration sessions, authentication, heartbeat, and expiry."""

from __future__ import annotations

import secrets
import sqlite3
import time
import uuid
from collections.abc import Sequence
from typing import Any

from .store_constants import (
    AGENT_ACTIVE_ROOM_LIMIT,
    CONNECTOR_ONLINE_WINDOW_SECONDS,
    CONNECTOR_SESSION_IDLE_RETIRE_SECONDS,
    CONNECTOR_SESSION_MIN_RETAIN,
    DEFAULT_SESSION_TTL_SECONDS,
    OWNER_CLIENT_TYPE,
    PRESENCE_STATES,
    ROOM_ABANDON_AFTER_SECONDS,
    SESSION_COMPONENTS,
)
from .store_errors import AuthenticationError, ConflictError, NotFoundError
from .validation import (
    ValidationError,
    agent_username,
    alias,
    compact_json,
    conversation_id as validate_conversation_id,
    opaque_id,
    product_username,
    string_tokens,
    token,
)


class AgentSessionMixin:
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
                  AND NOT EXISTS (
                      SELECT 1
                      FROM web_sessions AS web_session
                      JOIN web_users AS web_user
                        ON web_user.user_id = web_session.user_id
                      WHERE web_user.participant_id = participants.participant_id
                        AND web_user.active = 1
                        AND web_session.revoked_at IS NULL
                        AND web_session.expires_at > ?
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM agent_connectors AS connector
                      WHERE connector.accepted_participant_id =
                            participants.participant_id
                        AND connector.revoked_at IS NULL
                        AND connector.setup_status = 'configured'
                        AND (
                            COALESCE(connector.connector_last_seen_at, 0) >= ?
                            OR COALESCE(connector.tui_last_seen_at, 0) >= ?
                        )
                  )
                """,
                (
                    cleared_at,
                    cleared_at,
                    cleared_at - CONNECTOR_ONLINE_WINDOW_SECONDS,
                    cleared_at - CONNECTOR_ONLINE_WINDOW_SECONDS,
                ),
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
