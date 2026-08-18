from __future__ import annotations

import secrets as secrets
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from .web_auth_account_store import WebAuthAccountMixin
from .web_auth_contracts import (
    CAPTCHA_ALPHABET as CAPTCHA_ALPHABET,
    CAPTCHA_GLYPHS as CAPTCHA_GLYPHS,
    CAPTCHA_TTL_SECONDS as CAPTCHA_TTL_SECONDS,
    DEFAULT_ADMIN_CLIENT_TYPE as DEFAULT_ADMIN_CLIENT_TYPE,
    DEFAULT_ADMIN_PARTICIPANT_ID as DEFAULT_ADMIN_PARTICIPANT_ID,
    DEFAULT_ADMIN_PASSWORD as DEFAULT_ADMIN_PASSWORD,
    DEFAULT_ADMIN_USERNAME as DEFAULT_ADMIN_USERNAME,
    DEFAULT_REGISTRATION_CODE_TTL_SECONDS as DEFAULT_REGISTRATION_CODE_TTL_SECONDS,
    DEFAULT_WEB_USER_ROOM_LIMIT as DEFAULT_WEB_USER_ROOM_LIMIT,
    EMAIL_DOMAIN_LABEL_RE as EMAIL_DOMAIN_LABEL_RE,
    EMAIL_LOCAL_RE as EMAIL_LOCAL_RE,
    EMAIL_TOKEN_AUDIT_RETENTION_SECONDS as EMAIL_TOKEN_AUDIT_RETENTION_SECONDS,
    EMAIL_VERIFICATION_TTL_SECONDS as EMAIL_VERIFICATION_TTL_SECONDS,
    MAX_REGISTRATION_CODE_TTL_SECONDS as MAX_REGISTRATION_CODE_TTL_SECONDS,
    MAX_REGISTRATION_CODE_USES as MAX_REGISTRATION_CODE_USES,
    MAX_WEB_USER_ROOM_LIMIT as MAX_WEB_USER_ROOM_LIMIT,
    PASSWORD_MAX_LENGTH as PASSWORD_MAX_LENGTH,
    PASSWORD_MIN_LENGTH as PASSWORD_MIN_LENGTH,
    PASSWORD_RESET_TTL_SECONDS as PASSWORD_RESET_TTL_SECONDS,
    REGISTRATION_CODE_PREFIX as REGISTRATION_CODE_PREFIX,
    USERNAME_RE as USERNAME_RE,
    WEB_AUTH_SCHEMA as WEB_AUTH_SCHEMA,
    WEB_SESSION_AUDIT_RETENTION_SECONDS as WEB_SESSION_AUDIT_RETENTION_SECONDS,
    WEB_SESSION_COOKIE as WEB_SESSION_COOKIE,
    WEB_SESSION_TOUCH_INTERVAL_SECONDS as WEB_SESSION_TOUCH_INTERVAL_SECONDS,
    WEB_SESSION_TTL_SECONDS as WEB_SESSION_TTL_SECONDS,
    WebAuthenticationError as WebAuthenticationError,
    WebAuthorizationError as WebAuthorizationError,
    WebAuthError as WebAuthError,
    WebConflictError as WebConflictError,
    _password_hash as _password_hash,
    _password_matches as _password_matches,
    mask_email as mask_email,
    normalize_email as normalize_email,
    normalize_username as normalize_username,
    password_policy_payload as password_policy_payload,
    validate_password as validate_password,
)
from .web_auth_recovery_store import WebAuthRecoveryMixin
from .web_auth_registration_store import WebAuthRegistrationMixin
from .web_auth_support_store import WebAuthSupportMixin


class WebAuthStore(
    WebAuthAccountMixin,
    WebAuthRegistrationMixin,
    WebAuthRecoveryMixin,
    WebAuthSupportMixin,
):
    """Compose Web-auth persistence with its domain-specific operations."""

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
