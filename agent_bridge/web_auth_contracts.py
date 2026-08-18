"""Shared Web authentication constants, validation, and password primitives."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets

from .validation import ValidationError


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
