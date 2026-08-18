"""Web account lifecycle, sessions, passwords, and profile operations."""

from __future__ import annotations

import sqlite3
import time
import uuid

from .avatars import normalize_avatar_key
from .validation import (
    ValidationError,
    alias,
    display_name as validate_display_name,
    opaque_id,
)
from .web_auth_contracts import (
    CAPTCHA_ALPHABET,
    CAPTCHA_TTL_SECONDS,
    DEFAULT_ADMIN_USERNAME,
    EMAIL_VERIFICATION_TTL_SECONDS,
    WEB_SESSION_TOUCH_INTERVAL_SECONDS,
    WebAuthenticationError,
    WebAuthorizationError,
    WebConflictError,
    _password_hash,
    _password_matches,
    normalize_email,
    normalize_username,
    validate_password,
)


class WebAuthAccountMixin:
    """Account registration, login sessions, passwords, and profile state."""

    def create_captcha(self) -> dict[str, object]:
        answer = str(self._captcha_generator() or "").strip().upper()
        if not 4 <= len(answer) <= 8 or any(
            character not in CAPTCHA_ALPHABET for character in answer
        ):
            raise RuntimeError("captcha generator returned an invalid challenge")
        captcha_id = f"captcha_{uuid.uuid4().hex}"
        now = time.time()
        expires_at = now + CAPTCHA_TTL_SECONDS
        answer_hash = self._captcha_hash(captcha_id, answer)
        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM web_login_captchas WHERE expires_at <= ? OR consumed_at IS NOT NULL",
                (now,),
            )
            connection.execute(
                "INSERT INTO web_login_captchas "
                "(captcha_id, answer_hash, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (captcha_id, answer_hash, now, expires_at),
            )
        return {
            "captcha_id": captcha_id,
            "image": self._captcha_png_data(answer),
            "expires_at": expires_at,
        }

    def register(
        self,
        *,
        username: str,
        password: str,
        captcha_id: str,
        captcha_answer: str,
        registration_code: object | None = None,
        registration_code_required: bool = False,
        email: object | None = None,
    ) -> tuple[dict[str, object], str]:
        normalized_username = normalize_username(username)
        normalized_password = validate_password(
            password,
            username=normalized_username,
        )
        normalized_email = normalize_email(email) if email is not None else None
        if registration_code_required:
            with self._connection() as connection:
                self._require_registration_code_locked(
                    connection,
                    registration_code=registration_code,
                    now=time.time(),
                )
        self._consume_captcha(captcha_id, captcha_answer)
        now = time.time()
        user_id = f"webuser_{uuid.uuid4().hex}"
        participant_id = f"participant_web_{uuid.uuid4().hex}"
        client_type = f"web-user-{uuid.uuid4().hex[:16]}"
        signature = "Web 用户"
        password_hash = _password_hash(normalized_password)
        verification: dict[str, object] | None = None
        try:
            with self._transaction() as connection:
                code_row = None
                if registration_code_required:
                    code_row = self._require_registration_code_locked(
                        connection,
                        registration_code=registration_code,
                        now=now,
                    )
                connection.execute(
                    """
                    INSERT INTO web_users
                        (user_id, username, password_hash, role, participant_id,
                         display_name, signature, must_change_password, active,
                         created_at, updated_at, password_changed_at, last_login_at,
                         pending_email, email_updated_at)
                    VALUES (?, ?, ?, 'user', ?, ?, ?, 0, 1, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        normalized_username,
                        password_hash,
                        participant_id,
                        normalized_username,
                        signature,
                        now,
                        now,
                        now,
                        now,
                        normalized_email,
                        now if normalized_email is not None else None,
                    ),
                )
                self._insert_participant_locked(
                    connection,
                    participant_id=participant_id,
                    client_type=client_type,
                    username=normalized_username,
                    display_name=normalized_username,
                    signature=signature,
                    now=now,
                )
                if code_row is not None:
                    updated = connection.execute(
                        "UPDATE web_registration_codes "
                        "SET use_count = use_count + 1, last_used_at = ? "
                        "WHERE code_id = ? AND revoked_at IS NULL "
                        "AND expires_at > ? AND use_count < max_uses",
                        (now, str(code_row["code_id"]), now),
                    )
                    if updated.rowcount != 1:
                        raise WebAuthorizationError("注册码无效、已过期或已用完")
                    connection.execute(
                        "INSERT INTO web_registration_code_uses "
                        "(use_id, code_id, web_user_id, username_snapshot, used_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            f"registration_use_{uuid.uuid4().hex}",
                            str(code_row["code_id"]),
                            user_id,
                            normalized_username,
                            now,
                        ),
                    )
                if normalized_email is not None:
                    verification = self._issue_email_token_locked(
                        connection,
                        user_id=user_id,
                        purpose="verify_email",
                        email=normalized_email,
                        ttl_seconds=EMAIL_VERIFICATION_TTL_SECONDS,
                        now=now,
                    )
                token, session_id = self._create_session_locked(
                    connection,
                    user_id=user_id,
                    now=now,
                )
                row = self._user_row_locked(connection, user_id)
        except sqlite3.IntegrityError as exc:
            raise WebConflictError("用户名、昵称或邮箱已被使用") from exc
        payload = self._user_payload(row)
        payload["session_id"] = session_id
        if verification is not None:
            payload["_email_verification"] = verification
        return payload, token

    def login(
        self,
        *,
        username: str,
        password: str,
        captcha_id: str,
        captcha_answer: str,
    ) -> tuple[dict[str, object], str]:
        try:
            normalized_username = normalize_username(username)
        except ValidationError:
            normalized_username = str(username or "").strip()
        self._consume_captcha(captcha_id, captcha_answer)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM web_users WHERE username = ? COLLATE NOCASE",
                (normalized_username,),
            ).fetchone()
        encoded = (
            str(row["password_hash"]) if row is not None else self._dummy_password_hash
        )
        password_valid = _password_matches(password, encoded)
        if row is None or not password_valid or not bool(row["active"]):
            raise WebAuthenticationError("用户名或密码错误")
        now = time.time()
        with self._transaction() as connection:
            current = self._user_row_locked(connection, str(row["user_id"]))
            if not bool(current["active"]):
                raise WebAuthenticationError("账户已停用")
            token, session_id = self._create_session_locked(
                connection,
                user_id=str(current["user_id"]),
                now=now,
            )
            connection.execute(
                "UPDATE web_users SET last_login_at = ?, updated_at = ? WHERE user_id = ?",
                (now, now, str(current["user_id"])),
            )
            connection.execute(
                "UPDATE participants SET status = 'online', last_seen = ? "
                "WHERE participant_id = ?",
                (now, str(current["participant_id"])),
            )
            current = self._user_row_locked(connection, str(current["user_id"]))
        payload = self._user_payload(current)
        payload["session_id"] = session_id
        return payload, token

    def authenticate(self, session_token: str | None) -> dict[str, object]:
        token_value = str(session_token or "").strip()
        if not token_value:
            raise WebAuthenticationError("请先登录")
        try:
            normalized_token = opaque_id(token_value, field="web_session_token")
        except ValidationError as exc:
            raise WebAuthenticationError("登录已失效，请重新登录") from exc
        now = time.time()
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT session.*, user.username, user.role, user.participant_id,
                       user.display_name, user.signature, user.avatar_key,
                       user.must_change_password,
                       user.active, user.can_create_rooms, user.room_limit,
                       user.created_at AS user_created_at,
                       user.password_changed_at, user.last_login_at,
                       user.email, user.email_verified_at,
                       user.pending_email, user.email_updated_at
                FROM web_sessions AS session
                JOIN web_users AS user ON user.user_id = session.user_id
                WHERE session.token_hash = ?
                """,
                (self._secret_hash(normalized_token),),
            ).fetchone()
            if row is None or row["revoked_at"] is not None:
                raise WebAuthenticationError("登录已失效，请重新登录")
            if not bool(row["active"]):
                raise WebAuthenticationError("账户已停用")
            if float(row["expires_at"]) <= now:
                raise WebAuthenticationError("登录已过期，请重新登录")
        renewed = float(row["expires_at"])
        if now - float(row["last_seen"]) >= WEB_SESSION_TOUCH_INTERVAL_SECONDS:
            renewed = max(renewed, now + float(row["ttl_seconds"]))
            with self._transaction() as connection:
                updated = connection.execute(
                    "UPDATE web_sessions SET last_seen = ?, expires_at = ? "
                    "WHERE session_id = ? AND token_hash = ? AND revoked_at IS NULL "
                    "AND expires_at > ?",
                    (
                        now,
                        renewed,
                        str(row["session_id"]),
                        self._secret_hash(normalized_token),
                        now,
                    ),
                )
                if updated.rowcount != 1:
                    raise WebAuthenticationError("登录已失效，请重新登录")
                connection.execute(
                    "UPDATE participants SET status = 'online', last_seen = ? "
                    "WHERE participant_id = ?",
                    (now, str(row["participant_id"])),
                )
        payload = self._user_payload(row)
        payload.update(
            {
                "session_id": str(row["session_id"]),
                "session_expires_at": renewed,
            }
        )
        return payload

    def logout(self, session_token: str | None) -> None:
        value = str(session_token or "").strip()
        if not value:
            return
        now = time.time()
        with self._transaction() as connection:
            connection.execute(
                "UPDATE web_sessions SET revoked_at = ?, revoked_reason = 'logout' "
                "WHERE token_hash = ? AND revoked_at IS NULL",
                (now, self._secret_hash(value)),
            )

    def change_password(
        self,
        *,
        user_id: str,
        session_id: str,
        current_password: str,
        new_password: str,
    ) -> dict[str, object]:
        user = opaque_id(user_id, field="web_user_id")
        session = opaque_id(session_id, field="web_session_id")
        now = time.time()
        with self._transaction() as connection:
            self._require_live_session_locked(
                connection,
                session_id=session,
                user_id=user,
                now=now,
            )
            row = self._user_row_locked(connection, user)
            if not _password_matches(current_password, str(row["password_hash"])):
                raise ValidationError("当前密码不正确")
            normalized_password = validate_password(
                new_password,
                username=str(row["username"]),
            )
            if _password_matches(normalized_password, str(row["password_hash"])):
                raise ValidationError(
                    "new password must differ from the current password"
                )
            connection.execute(
                "UPDATE web_users SET password_hash = ?, must_change_password = 0, "
                "password_changed_at = ?, updated_at = ? WHERE user_id = ?",
                (_password_hash(normalized_password), now, now, user),
            )
            connection.execute(
                "UPDATE web_sessions SET revoked_at = ?, revoked_reason = 'password_changed' "
                "WHERE user_id = ? AND session_id != ? AND revoked_at IS NULL",
                (now, user, session),
            )
            connection.execute(
                "UPDATE web_email_tokens SET consumed_at = ? "
                "WHERE user_id = ? AND purpose = 'reset_password' "
                "AND consumed_at IS NULL",
                (now, user),
            )
            updated = self._user_row_locked(connection, user)
        payload = self._user_payload(updated)
        if updated["email"] and updated["email_verified_at"] is not None:
            payload["_verified_email"] = str(updated["email"])
        return payload

    def update_profile(
        self,
        *,
        user_id: str,
        session_id: str,
        display_name: str,
        signature: str,
        avatar_key: object | None = None,
    ) -> dict[str, object]:
        user = opaque_id(user_id, field="web_user_id")
        session = opaque_id(session_id, field="web_session_id")
        normalized_display = validate_display_name(display_name)
        normalized_signature = alias(signature, field="signature")
        normalized_avatar = (
            normalize_avatar_key(avatar_key)
            if avatar_key is not None
            else None
        )
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
                collision = connection.execute(
                    "SELECT participant_id FROM participants "
                    "WHERE display_name = ? COLLATE NOCASE AND participant_id != ?",
                    (normalized_display, str(row["participant_id"])),
                ).fetchone()
                if collision is not None:
                    raise WebConflictError("昵称已被使用")
                connection.execute(
                    "UPDATE web_users SET display_name = ?, signature = ?, "
                    "avatar_key = COALESCE(?, avatar_key), updated_at = ? "
                    "WHERE user_id = ?",
                    (
                        normalized_display,
                        normalized_signature,
                        normalized_avatar,
                        now,
                        user,
                    ),
                )
                connection.execute(
                    "UPDATE participants SET display_name = ?, signature = ?, "
                    "avatar_key = COALESCE(?, avatar_key), profile_updated_at = ?, "
                    "last_seen = ? WHERE participant_id = ?",
                    (
                        normalized_display,
                        normalized_signature,
                        normalized_avatar,
                        now,
                        now,
                        str(row["participant_id"]),
                    ),
                )
                updated = self._user_row_locked(connection, user)
        except sqlite3.IntegrityError as exc:
            raise WebConflictError("昵称已被使用") from exc
        return self._user_payload(updated)

    def bootstrap_admin_ready(self) -> bool:
        """Return true only after the active bootstrap admin changed its password."""

        with self._connection() as connection:
            row = connection.execute(
                "SELECT role, active, must_change_password FROM web_users "
                "WHERE username = ? COLLATE NOCASE",
                (DEFAULT_ADMIN_USERNAME,),
            ).fetchone()
        return bool(
            row is not None
            and str(row["role"]) == "admin"
            and bool(row["active"])
            and not bool(row["must_change_password"])
        )
