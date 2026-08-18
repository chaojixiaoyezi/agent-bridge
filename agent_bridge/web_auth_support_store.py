"""Internal Web-auth row, token, session, and CAPTCHA helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import random
import secrets
import sqlite3
import struct
import time
import uuid
import zlib

from .validation import ValidationError, opaque_id
from .web_auth_contracts import (
    CAPTCHA_ALPHABET,
    CAPTCHA_GLYPHS,
    EMAIL_TOKEN_AUDIT_RETENTION_SECONDS,
    WEB_SESSION_AUDIT_RETENTION_SECONDS,
    WebAuthenticationError,
    WebAuthorizationError,
    mask_email,
)


class WebAuthSupportMixin:
    """Private helpers shared by account, registration, and recovery flows."""

    @staticmethod
    def _require_admin_locked(
        connection: sqlite3.Connection,
        user_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM web_users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row is None or not bool(row["active"]):
            raise WebAuthenticationError("未知或已停用的用户")
        if str(row["role"]) != "admin":
            raise WebAuthorizationError("此操作仅限管理员")
        return row

    @classmethod
    def _require_registration_code_locked(
        cls,
        connection: sqlite3.Connection,
        *,
        registration_code: object | None,
        now: float,
    ) -> sqlite3.Row:
        supplied = str(registration_code or "").strip()
        if not supplied or len(supplied) > 256 or any(
            ord(character) < 32 or ord(character) == 127
            for character in supplied
        ):
            raise WebAuthorizationError("注册码无效、已过期或已用完")
        row = connection.execute(
            "SELECT * FROM web_registration_codes WHERE code_hash = ?",
            (cls._secret_hash(supplied),),
        ).fetchone()
        if (
            row is None
            or row["revoked_at"] is not None
            or float(row["expires_at"]) <= now
            or int(row["use_count"]) >= int(row["max_uses"])
        ):
            raise WebAuthorizationError("注册码无效、已过期或已用完")
        return row

    @staticmethod
    def _registration_code_payload(
        row: sqlite3.Row,
        *,
        now: float,
    ) -> dict[str, object]:
        keys = set(row.keys())
        if row["revoked_at"] is not None:
            status = "revoked"
        elif float(row["expires_at"]) <= now:
            status = "expired"
        elif int(row["use_count"]) >= int(row["max_uses"]):
            status = "exhausted"
        else:
            status = "active"
        return {
            "code_id": str(row["code_id"]),
            "label": str(row["label"] or ""),
            "max_uses": int(row["max_uses"]),
            "use_count": int(row["use_count"]),
            "remaining_uses": max(
                0,
                int(row["max_uses"]) - int(row["use_count"]),
            ),
            "status": status,
            "created_by_web_user_id": str(row["created_by_web_user_id"]),
            "created_by_username": (
                str(row["created_by_username"])
                if "created_by_username" in keys
                else None
            ),
            "created_at": float(row["created_at"]),
            "expires_at": float(row["expires_at"]),
            "last_used_at": (
                float(row["last_used_at"])
                if row["last_used_at"] is not None
                else None
            ),
            "revoked_at": (
                float(row["revoked_at"])
                if row["revoked_at"] is not None
                else None
            ),
            "revoked_by_web_user_id": (
                str(row["revoked_by_web_user_id"])
                if row["revoked_by_web_user_id"] is not None
                else None
            ),
            "revoked_by_username": (
                str(row["revoked_by_username"])
                if "revoked_by_username" in keys
                and row["revoked_by_username"] is not None
                else None
            ),
        }

    def _consume_captcha(self, captcha_id: str, answer: str) -> None:
        try:
            challenge = opaque_id(captcha_id, field="captcha_id")
        except ValidationError as exc:
            raise WebAuthenticationError("验证码错误或已过期") from exc
        normalized_answer = str(answer or "").strip().upper()
        now = time.time()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM web_login_captchas WHERE captcha_id = ?",
                (challenge,),
            ).fetchone()
            if (
                row is None
                or row["consumed_at"] is not None
                or float(row["expires_at"]) <= now
            ):
                raise WebAuthenticationError("验证码错误或已过期")
            connection.execute(
                "UPDATE web_login_captchas SET consumed_at = ? WHERE captcha_id = ?",
                (now, challenge),
            )
            expected = str(row["answer_hash"])
        if not hmac.compare_digest(
            self._captcha_hash(challenge, normalized_answer),
            expected,
        ):
            raise WebAuthenticationError("验证码错误或已过期")

    def _create_session_locked(
        self,
        connection: sqlite3.Connection,
        *,
        user_id: str,
        now: float,
    ) -> tuple[str, str]:
        retention_cutoff = now - WEB_SESSION_AUDIT_RETENTION_SECONDS
        connection.execute(
            "DELETE FROM web_sessions WHERE expires_at <= ? "
            "OR (revoked_at IS NOT NULL AND revoked_at <= ?)",
            (retention_cutoff, retention_cutoff),
        )
        session_id = f"websession_{uuid.uuid4().hex}"
        token = f"web_{secrets.token_urlsafe(32)}"
        connection.execute(
            """
            INSERT INTO web_sessions
                (session_id, user_id, token_hash, created_at, expires_at,
                 ttl_seconds, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                user_id,
                self._secret_hash(token),
                now,
                now + self.session_ttl_seconds,
                self.session_ttl_seconds,
                now,
            ),
        )
        return token, session_id

    @staticmethod
    def _require_live_session_locked(
        connection: sqlite3.Connection,
        *,
        session_id: str,
        user_id: str,
        now: float,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM web_sessions WHERE session_id = ? AND user_id = ? "
            "AND revoked_at IS NULL AND expires_at > ?",
            (session_id, user_id, now),
        ).fetchone()
        if row is None:
            raise WebAuthenticationError("登录已失效，请重新登录")
        return row

    @staticmethod
    def _user_row_locked(connection: sqlite3.Connection, user_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM web_users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            raise WebAuthenticationError("未知用户")
        return row

    @staticmethod
    def _user_payload(row: sqlite3.Row) -> dict[str, object]:
        keys = set(row.keys())
        email = row["email"] if "email" in keys else None
        pending_email = row["pending_email"] if "pending_email" in keys else None
        return {
            "user_id": str(row["user_id"]),
            "username": str(row["username"]),
            "role": str(row["role"]),
            "is_admin": str(row["role"]) == "admin",
            "participant_id": str(row["participant_id"]),
            "display_name": str(row["display_name"]),
            "signature": str(row["signature"]),
            "avatar_key": str(row["avatar_key"] or "auto"),
            "must_change_password": bool(row["must_change_password"]),
            "can_create_rooms": (
                True
                if str(row["role"]) == "admin"
                else bool(row["can_create_rooms"])
            ),
            "room_limit": int(row["room_limit"]),
            "created_at": float(
                row["user_created_at"]
                if "user_created_at" in keys
                else row["created_at"]
            ),
            "password_changed_at": (
                float(row["password_changed_at"])
                if row["password_changed_at"] is not None
                else None
            ),
            "last_login_at": (
                float(row["last_login_at"])
                if row["last_login_at"] is not None
                else None
            ),
            "email_masked": mask_email(email),
            "email_verified": bool(
                email
                and "email_verified_at" in keys
                and row["email_verified_at"] is not None
            ),
            "email_verified_at": (
                float(row["email_verified_at"])
                if "email_verified_at" in keys
                and row["email_verified_at"] is not None
                else None
            ),
            "pending_email_masked": mask_email(pending_email),
            "email_verification_pending": bool(pending_email),
            "email_updated_at": (
                float(row["email_updated_at"])
                if "email_updated_at" in keys
                and row["email_updated_at"] is not None
                else None
            ),
        }

    def _issue_email_token_locked(
        self,
        connection: sqlite3.Connection,
        *,
        user_id: str,
        purpose: str,
        email: str,
        ttl_seconds: float,
        now: float,
    ) -> dict[str, object]:
        connection.execute(
            "UPDATE web_email_tokens SET consumed_at = ? "
            "WHERE user_id = ? AND purpose = ? AND consumed_at IS NULL",
            (now, user_id, purpose),
        )
        connection.execute(
            "DELETE FROM web_email_tokens WHERE "
            "(consumed_at IS NOT NULL AND consumed_at < ?) "
            "OR expires_at < ?",
            (
                now - EMAIL_TOKEN_AUDIT_RETENTION_SECONDS,
                now - EMAIL_TOKEN_AUDIT_RETENTION_SECONDS,
            ),
        )
        # token_urlsafe may legally begin with '-' or '_', while opaque_id
        # deliberately requires an alphanumeric first character. Prefix every
        # newly issued email token so every value we deliver is also accepted
        # by the verification/reset boundary.
        token_value = f"email_{secrets.token_urlsafe(32)}"
        expires_at = now + float(ttl_seconds)
        connection.execute(
            "INSERT INTO web_email_tokens "
            "(token_id, user_id, purpose, email, token_hash, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                f"email_token_{uuid.uuid4().hex}",
                user_id,
                purpose,
                email,
                self._secret_hash(token_value),
                now,
                expires_at,
            ),
        )
        return {
            "email": email,
            "token": token_value,
            "purpose": purpose,
            "expires_at": expires_at,
        }

    @staticmethod
    def _normalize_email_token(value: object) -> str:
        try:
            return opaque_id(str(value or ""), field="email_token")
        except ValidationError as exc:
            raise ValidationError("链接无效或已过期") from exc

    @staticmethod
    def _secret_hash(value: str) -> str:
        return hashlib.sha256(str(value).encode("utf-8")).hexdigest()

    @staticmethod
    def _captcha_hash(captcha_id: str, answer: str) -> str:
        return hashlib.sha256(
            f"{captcha_id}:{str(answer).strip().upper()}".encode()
        ).hexdigest()

    @staticmethod
    def _random_captcha() -> str:
        return "".join(secrets.choice(CAPTCHA_ALPHABET) for _ in range(5))

    @staticmethod
    def _captcha_png_data(answer: str) -> str:
        width = 180
        height = 58
        image_random = random.Random(int.from_bytes(secrets.token_bytes(16)))
        pixels = bytearray(width * height * 3)
        for offset in range(0, len(pixels), 3):
            shade = 7 + image_random.randrange(7)
            pixels[offset : offset + 3] = bytes((shade, shade + 2, shade + 7))

        def set_pixel(x: int, y: int, color: tuple[int, int, int]) -> None:
            if not 0 <= x < width or not 0 <= y < height:
                return
            offset = (y * width + x) * 3
            pixels[offset : offset + 3] = bytes(color)

        def draw_line(
            x1: int,
            y1: int,
            x2: int,
            y2: int,
            color: tuple[int, int, int],
        ) -> None:
            delta_x = abs(x2 - x1)
            step_x = 1 if x1 < x2 else -1
            delta_y = -abs(y2 - y1)
            step_y = 1 if y1 < y2 else -1
            error = delta_x + delta_y
            while True:
                set_pixel(x1, y1, color)
                if x1 == x2 and y1 == y2:
                    break
                doubled = 2 * error
                if doubled >= delta_y:
                    error += delta_y
                    x1 += step_x
                if doubled <= delta_x:
                    error += delta_x
                    y1 += step_y

        colors = (
            (85, 214, 199),
            (155, 140, 255),
            (242, 181, 99),
            (215, 219, 227),
        )
        noise_colors = ((31, 48, 54), (48, 42, 70), (61, 47, 31))
        for _ in range(8):
            draw_line(
                image_random.randrange(width),
                image_random.randrange(height),
                image_random.randrange(width),
                image_random.randrange(height),
                image_random.choice(noise_colors),
            )
        for _ in range(180):
            set_pixel(
                image_random.randrange(width),
                image_random.randrange(height),
                image_random.choice(noise_colors),
            )

        scale = 5 if len(answer) <= 5 else 4 if len(answer) <= 7 else 3
        glyph_width = 5 * scale
        gap = max(3, scale - 1)
        total_width = len(answer) * glyph_width + (len(answer) - 1) * gap
        start_x = (width - total_width) // 2
        for index, character in enumerate(answer):
            pattern = CAPTCHA_GLYPHS[character]
            glyph_x = start_x + index * (glyph_width + gap)
            glyph_y = (height - 7 * scale) // 2 + image_random.randrange(5) - 2
            color = image_random.choice(colors)
            for row_index, row in enumerate(pattern):
                shear = (row_index - 3) * (index % 2) // 4
                for column_index, value in enumerate(row):
                    if value != "#":
                        continue
                    cell_x = glyph_x + column_index * scale + shear
                    cell_y = glyph_y + row_index * scale
                    for pixel_y in range(scale - 1):
                        for pixel_x in range(scale - 1):
                            set_pixel(cell_x + pixel_x, cell_y + pixel_y, color)

        raw = b"".join(
            b"\x00" + bytes(pixels[row * width * 3 : (row + 1) * width * 3])
            for row in range(height)
        )

        def chunk(kind: bytes, data: bytes) -> bytes:
            return (
                struct.pack(">I", len(data))
                + kind
                + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
            )

        png = (
            b"\x89PNG\r\n\x1a\n"
            + chunk(
                b"IHDR",
                struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0),
            )
            + chunk(b"IDAT", zlib.compress(raw, level=9))
            + chunk(b"IEND", b"")
        )
        encoded = base64.b64encode(png).decode("ascii")
        return f"data:image/png;base64,{encoded}"
