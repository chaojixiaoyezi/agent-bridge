from __future__ import annotations

import base64
import hashlib
import hmac
import math
import random
import re
import secrets
import sqlite3
import struct
import time
import uuid
import zlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from .avatars import normalize_avatar_key
from .validation import (
    ValidationError,
    alias,
    display_name as validate_display_name,
    opaque_id,
)


WEB_SESSION_COOKIE = "agent_bridge_web_session"
WEB_SESSION_TTL_SECONDS = 12 * 60 * 60
WEB_SESSION_TOUCH_INTERVAL_SECONDS = 30
WEB_SESSION_AUDIT_RETENTION_SECONDS = 30 * 24 * 60 * 60
CAPTCHA_TTL_SECONDS = 5 * 60
EMAIL_VERIFICATION_TTL_SECONDS = 24 * 60 * 60
PASSWORD_RESET_TTL_SECONDS = 30 * 60
EMAIL_TOKEN_AUDIT_RETENTION_SECONDS = 7 * 24 * 60 * 60
DEFAULT_REGISTRATION_CODE_TTL_SECONDS = 24 * 60 * 60
MAX_REGISTRATION_CODE_TTL_SECONDS = 30 * 24 * 60 * 60
MAX_REGISTRATION_CODE_USES = 1000
REGISTRATION_CODE_PREFIX = "ABR"
PASSWORD_MIN_LENGTH = 10
PASSWORD_MAX_LENGTH = 128
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin"
DEFAULT_ADMIN_PARTICIPANT_ID = "participant_web_owner"
DEFAULT_ADMIN_CLIENT_TYPE = "web-user"
DEFAULT_WEB_USER_ROOM_LIMIT = 2
MAX_WEB_USER_ROOM_LIMIT = 100
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$")
EMAIL_LOCAL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}$")
EMAIL_DOMAIN_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
CAPTCHA_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

CAPTCHA_GLYPHS = {
    "A": (".###.", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "B": ("####.", "#...#", "#...#", "####.", "#...#", "#...#", "####."),
    "C": (".####", "#....", "#....", "#....", "#....", "#....", ".####"),
    "D": ("####.", "#...#", "#...#", "#...#", "#...#", "#...#", "####."),
    "E": ("#####", "#....", "#....", "####.", "#....", "#....", "#####"),
    "F": ("#####", "#....", "#....", "####.", "#....", "#....", "#...."),
    "G": (".###.", "#....", "#....", "#.###", "#...#", "#...#", ".###."),
    "H": ("#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "J": ("..###", "...#.", "...#.", "...#.", "...#.", "#..#.", ".##.."),
    "K": ("#...#", "#..#.", "#.#..", "##...", "#.#..", "#..#.", "#...#"),
    "L": ("#....", "#....", "#....", "#....", "#....", "#....", "#####"),
    "M": ("#...#", "##.##", "#.#.#", "#.#.#", "#...#", "#...#", "#...#"),
    "N": ("#...#", "##..#", "##..#", "#.#.#", "#..##", "#..##", "#...#"),
    "P": ("####.", "#...#", "#...#", "####.", "#....", "#....", "#...."),
    "Q": (".###.", "#...#", "#...#", "#...#", "#.#.#", "#..#.", ".##.#"),
    "R": ("####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"),
    "S": (".####", "#....", "#....", ".###.", "....#", "....#", "####."),
    "T": ("#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."),
    "U": ("#...#", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "V": ("#...#", "#...#", "#...#", "#...#", "#...#", ".#.#.", "..#.."),
    "W": ("#...#", "#...#", "#...#", "#.#.#", "#.#.#", "##.##", "#...#"),
    "X": ("#...#", "#...#", ".#.#.", "..#..", ".#.#.", "#...#", "#...#"),
    "Y": ("#...#", "#...#", ".#.#.", "..#..", "..#..", "..#..", "..#.."),
    "Z": ("#####", "....#", "...#.", "..#..", ".#...", "#....", "#####"),
    "2": (".###.", "#...#", "....#", "...#.", "..#..", ".#...", "#####"),
    "3": ("####.", "....#", "....#", ".###.", "....#", "....#", "####."),
    "4": ("...#.", "..##.", ".#.#.", "#..#.", "#####", "...#.", "...#."),
    "5": ("#####", "#....", "#....", "####.", "....#", "....#", "####."),
    "6": (".###.", "#....", "#....", "####.", "#...#", "#...#", ".###."),
    "7": ("#####", "....#", "...#.", "..#..", ".#...", ".#...", ".#..."),
    "8": (".###.", "#...#", "#...#", ".###.", "#...#", "#...#", ".###."),
    "9": (".###.", "#...#", "#...#", ".####", "....#", "....#", ".###."),
}


WEB_AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS web_users (
    user_id TEXT PRIMARY KEY,
    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user')),
    participant_id TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL COLLATE NOCASE,
    signature TEXT NOT NULL,
    avatar_key TEXT NOT NULL DEFAULT 'auto',
    must_change_password INTEGER NOT NULL DEFAULT 0
        CHECK (must_change_password IN (0, 1)),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    can_create_rooms INTEGER NOT NULL DEFAULT 0
        CHECK (can_create_rooms IN (0, 1)),
    room_limit INTEGER NOT NULL DEFAULT 2
        CHECK (room_limit BETWEEN 1 AND 100),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    password_changed_at REAL,
    last_login_at REAL,
    email TEXT COLLATE NOCASE,
    email_verified_at REAL,
    pending_email TEXT COLLATE NOCASE,
    email_updated_at REAL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_web_users_display_name_unique
    ON web_users(display_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_web_users_role_active
    ON web_users(role, active, username);

CREATE TABLE IF NOT EXISTS web_sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    ttl_seconds REAL NOT NULL,
    last_seen REAL NOT NULL,
    revoked_at REAL,
    revoked_reason TEXT,
    FOREIGN KEY (user_id) REFERENCES web_users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_web_sessions_user_active
    ON web_sessions(user_id, revoked_at, expires_at);

CREATE TABLE IF NOT EXISTS web_login_captchas (
    captcha_id TEXT PRIMARY KEY,
    answer_hash TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    consumed_at REAL
);

CREATE INDEX IF NOT EXISTS idx_web_login_captchas_expiry
    ON web_login_captchas(expires_at, consumed_at);

CREATE TABLE IF NOT EXISTS web_registration_codes (
    code_id TEXT PRIMARY KEY,
    code_hash TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL DEFAULT '',
    max_uses INTEGER NOT NULL DEFAULT 1
        CHECK (max_uses BETWEEN 1 AND 1000),
    use_count INTEGER NOT NULL DEFAULT 0
        CHECK (use_count >= 0 AND use_count <= max_uses),
    created_by_web_user_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    last_used_at REAL,
    revoked_at REAL,
    revoked_by_web_user_id TEXT,
    FOREIGN KEY (created_by_web_user_id) REFERENCES web_users(user_id),
    FOREIGN KEY (revoked_by_web_user_id) REFERENCES web_users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_web_registration_codes_status
    ON web_registration_codes(revoked_at, expires_at, use_count, created_at DESC);

CREATE TABLE IF NOT EXISTS web_registration_code_uses (
    use_id TEXT PRIMARY KEY,
    code_id TEXT NOT NULL,
    web_user_id TEXT NOT NULL,
    username_snapshot TEXT NOT NULL,
    used_at REAL NOT NULL,
    FOREIGN KEY (code_id) REFERENCES web_registration_codes(code_id),
    FOREIGN KEY (web_user_id) REFERENCES web_users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_web_registration_code_uses_code
    ON web_registration_code_uses(code_id, used_at DESC);

CREATE TABLE IF NOT EXISTS web_email_tokens (
    token_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    purpose TEXT NOT NULL
        CHECK (purpose IN ('verify_email', 'reset_password')),
    email TEXT NOT NULL COLLATE NOCASE,
    token_hash TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    consumed_at REAL,
    FOREIGN KEY (user_id) REFERENCES web_users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_web_email_tokens_user_purpose
    ON web_email_tokens(user_id, purpose, consumed_at, expires_at);
CREATE INDEX IF NOT EXISTS idx_web_email_tokens_expiry
    ON web_email_tokens(expires_at, consumed_at);
"""


class WebAuthError(RuntimeError):
    pass


class WebAuthenticationError(WebAuthError):
    pass


class WebAuthorizationError(WebAuthError):
    pass


class WebConflictError(WebAuthError):
    pass


def normalize_username(value: str) -> str:
    username = str(value or "").strip()
    if not USERNAME_RE.fullmatch(username):
        raise ValidationError(
            "username must be 3-32 ASCII letters, numbers, dots, underscores, or hyphens"
        )
    return username


def normalize_email(value: object) -> str:
    email = str(value or "").strip().casefold()
    if not email or len(email) > 254 or not email.isascii() or email.count("@") != 1:
        raise ValidationError("email must be a valid ASCII address")
    local, domain = email.rsplit("@", 1)
    if (
        not EMAIL_LOCAL_RE.fullmatch(local)
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
    ):
        raise ValidationError("email must be a valid ASCII address")
    labels = domain.split(".")
    if len(labels) < 2 or any(not EMAIL_DOMAIN_LABEL_RE.fullmatch(label) for label in labels):
        raise ValidationError("email must be a valid ASCII address")
    return email


def mask_email(value: object | None) -> str | None:
    email = str(value or "").strip()
    if not email or "@" not in email:
        return None
    local, domain = email.rsplit("@", 1)
    if len(local) == 1:
        masked_local = "*"
    elif len(local) == 2:
        masked_local = f"{local[0]}*"
    else:
        masked_local = f"{local[0]}{'*' * min(6, len(local) - 2)}{local[-1]}"
    return f"{masked_local}@{domain}"


def validate_password(password: str, *, username: str) -> str:
    value = str(password or "")
    if not PASSWORD_MIN_LENGTH <= len(value) <= PASSWORD_MAX_LENGTH:
        raise ValidationError(
            f"password must contain {PASSWORD_MIN_LENGTH}-{PASSWORD_MAX_LENGTH} characters"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValidationError("password cannot contain control characters")
    categories = sum(
        (
            any(character.isascii() and character.islower() for character in value),
            any(character.isascii() and character.isupper() for character in value),
            any(character.isdigit() for character in value),
            any(not character.isalnum() for character in value),
        )
    )
    if categories < 3:
        raise ValidationError(
            "password must include at least three of lowercase, uppercase, numbers, and symbols"
        )
    folded = value.casefold()
    if folded == str(username or "").casefold() or folded in {
        "admin",
        "password",
        "password123",
        "1234567890",
    }:
        raise ValidationError("password is too easy to guess")
    return value


def password_policy_payload() -> dict[str, object]:
    return {
        "minimum_length": PASSWORD_MIN_LENGTH,
        "maximum_length": PASSWORD_MAX_LENGTH,
        "required_character_groups": 3,
        "character_groups": ["lowercase", "uppercase", "number", "symbol"],
        "description": (
            "密码需为 10–128 个字符，并至少包含小写字母、大写字母、数字、符号中的三类。"
        ),
    }


def _password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        str(password).encode("utf-8"),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
        dklen=32,
    )
    return "$".join(
        (
            "scrypt",
            "16384",
            "8",
            "1",
            base64.urlsafe_b64encode(salt).decode("ascii").rstrip("="),
            base64.urlsafe_b64encode(derived).decode("ascii").rstrip("="),
        )
    )


def _password_matches(password: str, encoded: str) -> bool:
    try:
        scheme, n_text, r_text, p_text, salt_text, digest_text = encoded.split("$", 5)
        if scheme != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_text + "=" * (-len(salt_text) % 4))
        expected = base64.urlsafe_b64decode(digest_text + "=" * (-len(digest_text) % 4))
        actual = hashlib.scrypt(
            str(password).encode("utf-8"),
            salt=salt,
            n=int(n_text),
            r=int(r_text),
            p=int(p_text),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


class WebAuthStore:
    def __init__(
        self,
        database: str | Path,
        *,
        captcha_generator: Callable[[], str] | None = None,
        session_ttl_seconds: int = WEB_SESSION_TTL_SECONDS,
    ) -> None:
        self.database = Path(database).expanduser()
        self._captcha_generator = captcha_generator or self._random_captcha
        self.session_ttl_seconds = max(
            5 * 60,
            min(int(session_ttl_seconds), WEB_SESSION_TTL_SECONDS),
        )
        self._dummy_password_hash = _password_hash("Dummy-login-password1!")
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.database),
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(WEB_AUTH_SCHEMA)
            self._migrate_room_permissions_locked(connection)
        now = time.time()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM web_users WHERE username = ? COLLATE NOCASE",
                (DEFAULT_ADMIN_USERNAME,),
            ).fetchone()
            if existing is None:
                participant = connection.execute(
                    "SELECT display_name, signature FROM participants "
                    "WHERE participant_id = ?",
                    (DEFAULT_ADMIN_PARTICIPANT_ID,),
                ).fetchone()
                display = (
                    str(participant["display_name"])
                    if participant is not None
                    else DEFAULT_ADMIN_USERNAME
                )
                signature = (
                    str(participant["signature"])
                    if participant is not None
                    else "Agent Bridge 管理员"
                )
                user_id = f"webuser_{uuid.uuid4().hex}"
                connection.execute(
                    """
                    INSERT INTO web_users
                        (user_id, username, password_hash, role, participant_id,
                         display_name, signature, must_change_password, active,
                         created_at, updated_at)
                    VALUES (?, ?, ?, 'admin', ?, ?, ?, 1, 1, ?, ?)
                    """,
                    (
                        user_id,
                        DEFAULT_ADMIN_USERNAME,
                        _password_hash(DEFAULT_ADMIN_PASSWORD),
                        DEFAULT_ADMIN_PARTICIPANT_ID,
                        display,
                        signature,
                        now,
                        now,
                    ),
                )
                if participant is None:
                    self._insert_participant_locked(
                        connection,
                        participant_id=DEFAULT_ADMIN_PARTICIPANT_ID,
                        client_type=DEFAULT_ADMIN_CLIENT_TYPE,
                        username=DEFAULT_ADMIN_USERNAME,
                        display_name=display,
                        signature=signature,
                        now=now,
                    )
            else:
                user_id = str(existing["user_id"])

            # Versions before structured room tasks did not persist a Web owner
            # for a few legacy user-created rooms.  Assign only those orphaned
            # rooms to the existing default admin; explicit ownership is never
            # overwritten.
            has_ownership_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'room_web_owners'"
            ).fetchone()
            if has_ownership_table is not None:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO room_web_owners
                        (conversation_id, web_user_id, created_at)
                    SELECT room.conversation_id, ?, ?
                    FROM rooms AS room
                    WHERE room.creator_kind = 'user'
                    """,
                    (user_id, now),
                )

    @staticmethod
    def _migrate_room_permissions_locked(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(web_users)").fetchall()
        }
        if "can_create_rooms" not in columns:
            connection.execute(
                "ALTER TABLE web_users ADD COLUMN can_create_rooms INTEGER "
                "NOT NULL DEFAULT 0 CHECK (can_create_rooms IN (0, 1))"
            )
        if "room_limit" not in columns:
            connection.execute(
                "ALTER TABLE web_users ADD COLUMN room_limit INTEGER "
                f"NOT NULL DEFAULT {DEFAULT_WEB_USER_ROOM_LIMIT} "
                f"CHECK (room_limit BETWEEN 1 AND {MAX_WEB_USER_ROOM_LIMIT})"
            )
        if "avatar_key" not in columns:
            connection.execute(
                "ALTER TABLE web_users ADD COLUMN avatar_key TEXT "
                "NOT NULL DEFAULT 'auto'"
            )
        for column, declaration in (
            ("email", "TEXT COLLATE NOCASE"),
            ("email_verified_at", "REAL"),
            ("pending_email", "TEXT COLLATE NOCASE"),
            ("email_updated_at", "REAL"),
        ):
            if column not in columns:
                connection.execute(
                    f"ALTER TABLE web_users ADD COLUMN {column} {declaration}"
                )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_web_users_verified_email_unique "
            "ON web_users(email COLLATE NOCASE) WHERE email IS NOT NULL"
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_web_users_pending_email_unique "
            "ON web_users(pending_email COLLATE NOCASE) "
            "WHERE pending_email IS NOT NULL"
        )

    @staticmethod
    def _insert_participant_locked(
        connection: sqlite3.Connection,
        *,
        participant_id: str,
        client_type: str,
        username: str,
        display_name: str,
        signature: str,
        now: float,
    ) -> None:
        connection.execute(
            """
            INSERT INTO participants
                (participant_id, client_type, session_alias, display_name, signature,
                 profile_updated_at, capabilities_json, status, created_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, '[]', 'online', ?, ?)
            """,
            (
                participant_id,
                client_type,
                username,
                display_name,
                signature,
                now,
                now,
                now,
            ),
        )

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

    def user_count(self) -> int:
        with self._connection() as connection:
            return int(
                connection.execute("SELECT COUNT(*) FROM web_users").fetchone()[0]
            )

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
                WebAuthStore._secret_hash(token),
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
