"""Frozen legacy chat-authorization lookup and revocation."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from .store_constants import OWNER_PARTICIPANT_ID
from .store_errors import NotFoundError
from .validation import alias, opaque_id


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


class ChatAuthorizationMixin:
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
