"""Administrator-issued Web registration code governance."""

from __future__ import annotations

import math
import secrets
import time
import uuid

from .validation import ValidationError, opaque_id
from .web_auth_contracts import (
    DEFAULT_REGISTRATION_CODE_TTL_SECONDS,
    MAX_REGISTRATION_CODE_TTL_SECONDS,
    MAX_REGISTRATION_CODE_USES,
    REGISTRATION_CODE_PREFIX,
    WebConflictError,
)


class WebAuthRegistrationMixin:
    """Issue, list, revoke, and consume administrator registration codes."""

    def create_registration_code(
        self,
        *,
        created_by_web_user_id: str,
        label: object = "",
        max_uses: object = 1,
        expires_in_seconds: object = DEFAULT_REGISTRATION_CODE_TTL_SECONDS,
    ) -> dict[str, object]:
        creator = opaque_id(
            created_by_web_user_id,
            field="created_by_web_user_id",
        )
        normalized_label = str(label or "").strip()
        if len(normalized_label) > 80:
            raise ValidationError("registration code label must be at most 80 characters")
        if any(ord(character) < 32 or ord(character) == 127 for character in normalized_label):
            raise ValidationError("registration code label cannot contain control characters")
        if isinstance(max_uses, bool) or (
            isinstance(max_uses, float) and not max_uses.is_integer()
        ):
            raise ValidationError("max_uses must be an integer")
        try:
            normalized_max_uses = int(max_uses)
        except (TypeError, ValueError) as exc:
            raise ValidationError("max_uses must be an integer") from exc
        if not 1 <= normalized_max_uses <= MAX_REGISTRATION_CODE_USES:
            raise ValidationError(
                f"max_uses must be between 1 and {MAX_REGISTRATION_CODE_USES}"
            )
        if isinstance(expires_in_seconds, bool):
            raise ValidationError("expires_in_seconds must be a number")
        try:
            ttl_seconds = float(expires_in_seconds)
        except (TypeError, ValueError) as exc:
            raise ValidationError("expires_in_seconds must be a number") from exc
        if not math.isfinite(ttl_seconds) or not (
            60 * 60 <= ttl_seconds <= MAX_REGISTRATION_CODE_TTL_SECONDS
        ):
            raise ValidationError("registration code lifetime must be between 1 hour and 30 days")

        now = time.time()
        code_id = f"registration_code_{uuid.uuid4().hex}"
        code = f"{REGISTRATION_CODE_PREFIX}-{secrets.token_urlsafe(24)}"
        with self._transaction() as connection:
            self._require_admin_locked(connection, creator)
            connection.execute(
                """
                INSERT INTO web_registration_codes
                    (code_id, code_hash, label, max_uses, use_count,
                     created_by_web_user_id, created_at, expires_at)
                VALUES (?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    code_id,
                    self._secret_hash(code),
                    normalized_label,
                    normalized_max_uses,
                    creator,
                    now,
                    now + ttl_seconds,
                ),
            )
            row = connection.execute(
                "SELECT * FROM web_registration_codes WHERE code_id = ?",
                (code_id,),
            ).fetchone()
        payload = self._registration_code_payload(row, now=now)
        payload["code"] = code
        return payload

    def list_registration_codes(
        self,
        *,
        requesting_web_user_id: str,
        limit: int = 100,
    ) -> dict[str, object]:
        requester = opaque_id(
            requesting_web_user_id,
            field="requesting_web_user_id",
        )
        normalized_limit = max(1, min(int(limit), 200))
        now = time.time()
        with self._connection() as connection:
            self._require_admin_locked(connection, requester)
            rows = connection.execute(
                "SELECT code.*, creator.username AS created_by_username, "
                "revoker.username AS revoked_by_username "
                "FROM web_registration_codes AS code "
                "JOIN web_users AS creator "
                "ON creator.user_id = code.created_by_web_user_id "
                "LEFT JOIN web_users AS revoker "
                "ON revoker.user_id = code.revoked_by_web_user_id "
                "ORDER BY code.created_at DESC LIMIT ?",
                (normalized_limit,),
            ).fetchall()
        return {
            "codes": [self._registration_code_payload(row, now=now) for row in rows],
            "server_time": now,
        }

    def revoke_registration_code(
        self,
        *,
        code_id: str,
        revoked_by_web_user_id: str,
    ) -> dict[str, object]:
        normalized_code_id = opaque_id(code_id, field="registration_code_id")
        revoker = opaque_id(
            revoked_by_web_user_id,
            field="revoked_by_web_user_id",
        )
        now = time.time()
        with self._transaction() as connection:
            self._require_admin_locked(connection, revoker)
            row = connection.execute(
                "SELECT * FROM web_registration_codes WHERE code_id = ?",
                (normalized_code_id,),
            ).fetchone()
            if row is None:
                raise WebConflictError("注册码不存在")
            if row["revoked_at"] is None:
                connection.execute(
                    "UPDATE web_registration_codes "
                    "SET revoked_at = ?, revoked_by_web_user_id = ? "
                    "WHERE code_id = ? AND revoked_at IS NULL",
                    (now, revoker, normalized_code_id),
                )
            row = connection.execute(
                "SELECT code.*, creator.username AS created_by_username, "
                "revoker.username AS revoked_by_username "
                "FROM web_registration_codes AS code "
                "JOIN web_users AS creator "
                "ON creator.user_id = code.created_by_web_user_id "
                "LEFT JOIN web_users AS revoker "
                "ON revoker.user_id = code.revoked_by_web_user_id "
                "WHERE code.code_id = ?",
                (normalized_code_id,),
            ).fetchone()
        return self._registration_code_payload(row, now=now)
