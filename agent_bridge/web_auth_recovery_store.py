"""Verified-email and password-recovery workflows for Web accounts."""

from __future__ import annotations

import sqlite3
import time

from .validation import ValidationError, opaque_id
from .web_auth_contracts import (
    EMAIL_VERIFICATION_TTL_SECONDS,
    PASSWORD_RESET_TTL_SECONDS,
    WebConflictError,
    _password_hash,
    _password_matches,
    normalize_email,
    validate_password,
)


class WebAuthRecoveryMixin:
    """Email verification and password-reset token lifecycle."""

    def request_email_verification(
        self,
        *,
        user_id: str,
        session_id: str,
        current_password: str,
        email: object,
    ) -> dict[str, object]:
        user = opaque_id(user_id, field="web_user_id")
        session = opaque_id(session_id, field="web_session_id")
        normalized_email = normalize_email(email)
        now = time.time()
        try:
            with self._transaction() as connection:
                self._require_live_session_locked(
                    connection,
                    session_id=session,
                    user_id=user,
                    now=now,
                )
                row = self._user_row_locked(connection, user)
                if not _password_matches(
                    current_password,
                    str(row["password_hash"]),
                ):
                    raise ValidationError("当前密码不正确")
                if str(row["email"] or "").casefold() == normalized_email:
                    raise WebConflictError("该邮箱已经完成验证")
                collision = connection.execute(
                    "SELECT user_id FROM web_users "
                    "WHERE (email = ? COLLATE NOCASE OR pending_email = ? COLLATE NOCASE) "
                    "AND user_id != ?",
                    (normalized_email, normalized_email, user),
                ).fetchone()
                if collision is not None:
                    raise WebConflictError("该邮箱已被其他账户使用")
                connection.execute(
                    "UPDATE web_users SET pending_email = ?, email_updated_at = ?, "
                    "updated_at = ? WHERE user_id = ?",
                    (normalized_email, now, now, user),
                )
                result = self._issue_email_token_locked(
                    connection,
                    user_id=user,
                    purpose="verify_email",
                    email=normalized_email,
                    ttl_seconds=EMAIL_VERIFICATION_TTL_SECONDS,
                    now=now,
                )
        except sqlite3.IntegrityError as exc:
            raise WebConflictError("该邮箱已被其他账户使用") from exc
        return result

    def verify_email(self, token_value: object) -> dict[str, object]:
        normalized_token = self._normalize_email_token(token_value)
        now = time.time()
        try:
            with self._transaction() as connection:
                row = connection.execute(
                    "SELECT email_token.*, user.active, user.pending_email "
                    "FROM web_email_tokens AS email_token "
                    "JOIN web_users AS user ON user.user_id = email_token.user_id "
                    "WHERE email_token.token_hash = ? "
                    "AND email_token.purpose = 'verify_email'",
                    (self._secret_hash(normalized_token),),
                ).fetchone()
                if (
                    row is None
                    or row["consumed_at"] is not None
                    or float(row["expires_at"]) <= now
                    or not bool(row["active"])
                    or str(row["pending_email"] or "").casefold()
                    != str(row["email"]).casefold()
                ):
                    raise ValidationError("邮箱验证链接无效或已过期")
                collision = connection.execute(
                    "SELECT user_id FROM web_users "
                    "WHERE email = ? COLLATE NOCASE AND user_id != ?",
                    (str(row["email"]), str(row["user_id"])),
                ).fetchone()
                if collision is not None:
                    raise ValidationError("邮箱验证链接无效或已过期")
                connection.execute(
                    "UPDATE web_users SET email = ?, email_verified_at = ?, "
                    "pending_email = NULL, email_updated_at = ?, updated_at = ? "
                    "WHERE user_id = ?",
                    (
                        str(row["email"]),
                        now,
                        now,
                        now,
                        str(row["user_id"]),
                    ),
                )
                connection.execute(
                    "UPDATE web_email_tokens SET consumed_at = ? "
                    "WHERE user_id = ? AND purpose = 'verify_email' "
                    "AND consumed_at IS NULL",
                    (now, str(row["user_id"])),
                )
                updated = self._user_row_locked(connection, str(row["user_id"]))
        except sqlite3.IntegrityError as exc:
            raise ValidationError("邮箱验证链接无效或已过期") from exc
        return self._user_payload(updated)

    def create_password_reset(
        self,
        *,
        identifier: object,
        captcha_id: str,
        captcha_answer: str,
    ) -> dict[str, object] | None:
        normalized_identifier = str(identifier or "").strip().casefold()[:254]
        self._consume_captcha(captcha_id, captcha_answer)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM web_users WHERE active = 1 AND "
                "(username = ? COLLATE NOCASE OR email = ? COLLATE NOCASE) "
                "ORDER BY CASE WHEN username = ? COLLATE NOCASE THEN 0 ELSE 1 END "
                "LIMIT 1",
                (
                    normalized_identifier,
                    normalized_identifier,
                    normalized_identifier,
                ),
            ).fetchone()
        # Keep the expensive password primitive in both account-found and
        # account-missing paths so the HTTP response does not become a cheap
        # account-enumeration oracle.
        _password_matches("reset-request-probe", self._dummy_password_hash)
        if row is None or row["email_verified_at"] is None or not row["email"]:
            return None
        now = time.time()
        with self._transaction() as connection:
            current = self._user_row_locked(connection, str(row["user_id"]))
            if not bool(current["active"]) or not current["email"]:
                return None
            return self._issue_email_token_locked(
                connection,
                user_id=str(current["user_id"]),
                purpose="reset_password",
                email=str(current["email"]),
                ttl_seconds=PASSWORD_RESET_TTL_SECONDS,
                now=now,
            )

    def reset_password(
        self,
        *,
        token_value: object,
        new_password: str,
    ) -> dict[str, object]:
        normalized_token = self._normalize_email_token(token_value)
        now = time.time()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT email_token.*, user.username, user.password_hash, user.active, "
                "user.email AS verified_email, user.email_verified_at "
                "FROM web_email_tokens AS email_token "
                "JOIN web_users AS user ON user.user_id = email_token.user_id "
                "WHERE email_token.token_hash = ? "
                "AND email_token.purpose = 'reset_password'",
                (self._secret_hash(normalized_token),),
            ).fetchone()
            if (
                row is None
                or row["consumed_at"] is not None
                or float(row["expires_at"]) <= now
                or not bool(row["active"])
                or row["email_verified_at"] is None
                or str(row["verified_email"] or "").casefold()
                != str(row["email"]).casefold()
            ):
                raise ValidationError("密码重置链接无效或已过期")
            normalized_password = validate_password(
                new_password,
                username=str(row["username"]),
            )
            if _password_matches(normalized_password, str(row["password_hash"])):
                raise ValidationError("new password must differ from the current password")
            password_hash = _password_hash(normalized_password)
            connection.execute(
                "UPDATE web_users SET password_hash = ?, must_change_password = 0, "
                "password_changed_at = ?, updated_at = ? WHERE user_id = ?",
                (password_hash, now, now, str(row["user_id"])),
            )
            connection.execute(
                "UPDATE web_sessions SET revoked_at = ?, "
                "revoked_reason = 'password_reset' WHERE user_id = ? "
                "AND revoked_at IS NULL",
                (now, str(row["user_id"])),
            )
            connection.execute(
                "UPDATE web_email_tokens SET consumed_at = ? "
                "WHERE user_id = ? AND purpose = 'reset_password' "
                "AND consumed_at IS NULL",
                (now, str(row["user_id"])),
            )
        return {"email": str(row["email"]), "password_changed_at": now}
