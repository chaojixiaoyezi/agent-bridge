"""Shared Agent/Web session validation and write-authority fencing."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time

from .store_errors import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
)
from .validation import opaque_id


class SessionAuthorityMixin:
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

        row = SessionAuthorityMixin._require_live_session(
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
