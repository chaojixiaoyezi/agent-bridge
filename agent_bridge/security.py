from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from starlette.responses import JSONResponse

from .web_auth import WEB_SESSION_COOKIE, WEB_SESSION_TTL_SECONDS


PUBLIC_WEB_SESSION_COOKIE = "__Host-agent_bridge_web_session"
PUBLIC_WEB_SESSION_TTL_SECONDS = 30 * 60
MAX_REQUEST_BODY_BYTES = 70_000
MAX_ASGI_REQUEST_BODY_BYTES = 26 * 1024 * 1024
MIN_AGENT_REGISTRATION_SECRET_CHARS = 32
MIN_WEB_REGISTRATION_SECRET_CHARS = 20
DEFAULT_HSTS_SECONDS = 365 * 24 * 60 * 60
_HOST_PATTERN = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)


class ViewerSecurityConfigurationError(RuntimeError):
    pass


class RequestRateLimitExceeded(RuntimeError):
    def __init__(self, retry_after_seconds: float) -> None:
        self.retry_after_seconds = max(1.0, float(retry_after_seconds))
        super().__init__("请求过于频繁，请稍后重试")


@dataclass(frozen=True)
class ViewerSecurityPolicy:
    public_mode: bool = False
    allowed_hosts: tuple[str, ...] = ()
    allowed_origins: frozenset[str] = field(default_factory=frozenset)
    web_registration_mode: str = "open"
    web_registration_secret: str | None = None
    secure_cookies: bool = False
    web_session_cookie_name: str = WEB_SESSION_COOKIE
    web_session_ttl_seconds: int = WEB_SESSION_TTL_SECONDS
    forwarded_allow_ips: str = "127.0.0.1"
    tls_cert_file: Path | None = None
    tls_key_file: Path | None = None
    hsts_seconds: int = 0

    @classmethod
    def from_env(cls) -> "ViewerSecurityPolicy":
        public_mode = _truthy(os.environ.get("AGENT_BRIDGE_PUBLIC_MODE"))
        web_registration_secret = _read_secret(
            direct_name="AGENT_BRIDGE_WEB_REGISTRATION_SECRET",
            file_name="AGENT_BRIDGE_WEB_REGISTRATION_SECRET_FILE",
            label="Web registration secret",
        )
        requested_registration_mode = os.environ.get(
            "AGENT_BRIDGE_WEB_REGISTRATION_MODE",
            "",
        ).strip().casefold()
        if requested_registration_mode:
            registration_mode = requested_registration_mode
        elif web_registration_secret:
            registration_mode = "access_code"
        else:
            registration_mode = "closed" if public_mode else "open"

        tls_cert_file = _optional_path("AGENT_BRIDGE_TLS_CERT_FILE")
        tls_key_file = _optional_path("AGENT_BRIDGE_TLS_KEY_FILE")
        forwarded_env = os.environ.get("AGENT_BRIDGE_FORWARDED_ALLOW_IPS")
        forwarded_allow_ips = (
            forwarded_env.strip()
            if forwarded_env is not None
            else ("" if public_mode else "127.0.0.1")
        )
        default_ttl = (
            PUBLIC_WEB_SESSION_TTL_SECONDS
            if public_mode
            else WEB_SESSION_TTL_SECONDS
        )
        policy = cls(
            public_mode=public_mode,
            allowed_hosts=_csv_values(os.environ.get("AGENT_BRIDGE_ALLOWED_HOSTS")),
            allowed_origins=frozenset(
                _normalized_origin(item)
                for item in _csv_values(
                    os.environ.get("AGENT_BRIDGE_ALLOWED_ORIGINS")
                )
            ),
            web_registration_mode=registration_mode,
            web_registration_secret=web_registration_secret,
            secure_cookies=public_mode,
            web_session_cookie_name=(
                PUBLIC_WEB_SESSION_COOKIE if public_mode else WEB_SESSION_COOKIE
            ),
            web_session_ttl_seconds=_bounded_int(
                os.environ.get("AGENT_BRIDGE_WEB_SESSION_TTL_SECONDS"),
                default=default_ttl,
                minimum=15 * 60,
                maximum=WEB_SESSION_TTL_SECONDS,
                label="AGENT_BRIDGE_WEB_SESSION_TTL_SECONDS",
            ),
            forwarded_allow_ips=forwarded_allow_ips,
            tls_cert_file=tls_cert_file,
            tls_key_file=tls_key_file,
            hsts_seconds=DEFAULT_HSTS_SECONDS if public_mode else 0,
        )
        policy.validate_configuration()
        if public_mode and os.environ.get(
            "AGENT_BRIDGE_REGISTRATION_SECRET_FILE",
            "",
        ).strip():
            _validate_private_file(
                Path(
                    os.environ["AGENT_BRIDGE_REGISTRATION_SECRET_FILE"]
                ).expanduser(),
                label="Agent registration secret",
            )
        return policy

    @property
    def proxy_headers_enabled(self) -> bool:
        return bool(self.forwarded_allow_ips)

    def validate_configuration(self) -> None:
        if self.web_registration_mode not in {"closed", "access_code", "open"}:
            raise ViewerSecurityConfigurationError(
                "AGENT_BRIDGE_WEB_REGISTRATION_MODE must be closed, access_code, or open"
            )
        if (
            self.web_registration_mode == "access_code"
            and self.web_registration_secret is not None
            and len(self.web_registration_secret) < MIN_WEB_REGISTRATION_SECRET_CHARS
        ):
            raise ViewerSecurityConfigurationError(
                "legacy access-code Web registration secrets require at least "
                f"{MIN_WEB_REGISTRATION_SECRET_CHARS} characters"
            )
        if bool(self.tls_cert_file) != bool(self.tls_key_file):
            raise ViewerSecurityConfigurationError(
                "AGENT_BRIDGE_TLS_CERT_FILE and AGENT_BRIDGE_TLS_KEY_FILE must be set together"
            )
        if self.tls_cert_file is not None:
            _validate_readable_file(self.tls_cert_file, label="TLS certificate")
            _validate_private_file(self.tls_key_file, label="TLS private key")
        _validate_forwarded_allow_ips(self.forwarded_allow_ips)
        if not self.public_mode:
            return
        if not self.allowed_hosts:
            raise ViewerSecurityConfigurationError(
                "public mode requires AGENT_BRIDGE_ALLOWED_HOSTS"
            )
        if not self.allowed_origins:
            raise ViewerSecurityConfigurationError(
                "public mode requires AGENT_BRIDGE_ALLOWED_ORIGINS"
            )
        for host in self.allowed_hosts:
            if host == "*" or not _HOST_PATTERN.fullmatch(host):
                raise ViewerSecurityConfigurationError(
                    f"invalid or unsafe allowed host: {host}"
                )
        for origin in self.allowed_origins:
            parsed = urlsplit(origin)
            if parsed.scheme != "https" or not parsed.hostname:
                raise ViewerSecurityConfigurationError(
                    "public allowed origins must be exact https origins"
                )
            if not any(_host_matches(parsed.hostname, host) for host in self.allowed_hosts):
                raise ViewerSecurityConfigurationError(
                    f"allowed origin host is not trusted: {origin}"
                )
        if self.tls_cert_file is None and not self.forwarded_allow_ips:
            raise ViewerSecurityConfigurationError(
                "public mode requires direct TLS files or an explicit trusted proxy IP/CIDR"
            )

    def validate_runtime(
        self,
        *,
        agent_registration_secret: str | None,
        bootstrap_admin_ready: bool,
        database: str | Path,
    ) -> None:
        self.validate_configuration()
        if not self.public_mode:
            return
        if len(agent_registration_secret or "") < MIN_AGENT_REGISTRATION_SECRET_CHARS:
            raise ViewerSecurityConfigurationError(
                "public mode requires an Agent registration secret of at least "
                f"{MIN_AGENT_REGISTRATION_SECRET_CHARS} characters"
            )
        if not bootstrap_admin_ready:
            raise ViewerSecurityConfigurationError(
                "public mode refuses to start until the default admin password is changed"
            )
        _validate_private_database(Path(database).expanduser())
        database_path = Path(database).expanduser().resolve()
        attachment_root = Path(
            os.environ.get(
                "AGENT_BRIDGE_ATTACHMENT_ROOT",
                str(database_path.parent / "attachments"),
            )
        ).expanduser().resolve()
        _validate_private_directory(
            attachment_root,
            label="Agent Bridge attachment directory",
        )

    def origin_allowed(self, origin: str | None) -> bool:
        if not self.public_mode:
            return True
        try:
            normalized = _normalized_origin(origin or "")
        except ViewerSecurityConfigurationError:
            return False
        return normalized in self.allowed_origins

    def registration_code_matches(self, supplied: object) -> bool:
        if self.web_registration_mode == "open":
            return True
        if (
            self.web_registration_mode != "access_code"
            or self.web_registration_secret is None
        ):
            return False
        return secrets.compare_digest(
            str(supplied or ""),
            str(self.web_registration_secret or ""),
        )


class SlidingWindowRateLimiter:
    """Exact SQLite-backed limiter shared by every local viewer process.

    Subjects are irreversibly hashed before persistence.  A short immediate
    transaction serializes the read/check/write step, so adding another viewer
    process cannot multiply the effective authentication or search allowance.
    """

    def __init__(
        self,
        database: str | Path,
        *,
        max_buckets: int = 100_000,
        retention_seconds: float = 24 * 60 * 60,
    ) -> None:
        self.database = Path(database).expanduser()
        self._max_buckets = max(128, int(max_buckets))
        self._retention_seconds = max(60 * 60, float(retention_seconds))
        self._checks = 0

    def check(
        self,
        bucket: str,
        subject: object,
        *,
        limit: int,
        window_seconds: float,
    ) -> None:
        now = time.time()
        window = max(1.0, float(window_seconds))
        normalized_limit = max(1, int(limit))
        normalized_bucket = str(bucket)
        subject_hash = _rate_subject(subject)
        connection = sqlite3.connect(
            str(self.database),
            timeout=2.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 2000")
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT events_json FROM shared_request_rate_windows "
                "WHERE bucket = ? AND subject_hash = ?",
                (normalized_bucket, subject_hash),
            ).fetchone()
            try:
                stored_events = json.loads(str(row["events_json"])) if row else []
                events = [
                    float(event)
                    for event in stored_events
                    if float(event) > now - window
                ]
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise sqlite3.DatabaseError(
                    "invalid shared request-rate window"
                ) from exc
            if len(events) >= normalized_limit:
                connection.rollback()
                raise RequestRateLimitExceeded(events[0] + window - now)
            events.append(now)
            connection.execute(
                """
                INSERT INTO shared_request_rate_windows (
                    bucket, subject_hash, events_json, last_seen_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(bucket, subject_hash) DO UPDATE SET
                    events_json = excluded.events_json,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    normalized_bucket,
                    subject_hash,
                    json.dumps(events, separators=(",", ":")),
                    now,
                ),
            )
            self._checks += 1
            if self._checks % 128 == 0:
                connection.execute(
                    "DELETE FROM shared_request_rate_windows "
                    "WHERE last_seen_at <= ?",
                    (now - self._retention_seconds,),
                )
                row_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM shared_request_rate_windows"
                    ).fetchone()[0]
                )
                overflow = row_count - self._max_buckets
                if overflow > 0:
                    connection.execute(
                        "DELETE FROM shared_request_rate_windows WHERE rowid IN ("
                        "SELECT rowid FROM shared_request_rate_windows "
                        "ORDER BY last_seen_at ASC LIMIT ?)",
                        (overflow,),
                    )
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()


class PublicTransportMiddleware:
    def __init__(self, app, *, enabled: bool) -> None:
        self.app = app
        self.enabled = bool(enabled)

    async def __call__(self, scope, receive, send) -> None:
        if (
            self.enabled
            and scope.get("type") == "http"
            and str(scope.get("scheme") or "http").casefold() != "https"
        ):
            response = JSONResponse(
                {"error": "public Agent Bridge requires HTTPS"},
                status_code=400,
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def request_client_key(request) -> str:
    client = request.client
    return str(client.host if client is not None else "unknown")


def _rate_subject(value: object) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _csv_values(value: str | None) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            item.strip().casefold()
            for item in str(value or "").split(",")
            if item.strip()
        )
    )


def _normalized_origin(value: str) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        parsed_port = parsed.port
    except ValueError as exc:
        raise ViewerSecurityConfigurationError(
            f"invalid exact origin: {raw}"
        ) from exc
    if (
        not parsed.scheme
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ViewerSecurityConfigurationError(f"invalid exact origin: {raw}")
    scheme = parsed.scheme.casefold()
    host = parsed.hostname.casefold()
    port = f":{parsed_port}" if parsed_port is not None else ""
    return f"{scheme}://{host}{port}"


def _host_matches(hostname: str, allowed: str) -> bool:
    host = str(hostname).casefold()
    candidate = str(allowed).casefold()
    return host == candidate


def _optional_path(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser() if value else None


def _read_secret(*, direct_name: str, file_name: str, label: str) -> str | None:
    direct = os.environ.get(direct_name, "").strip()
    if direct:
        return direct
    raw_path = os.environ.get(file_name, "").strip()
    if not raw_path:
        return None
    path = Path(raw_path).expanduser()
    _validate_private_file(path, label=label)
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ViewerSecurityConfigurationError(f"cannot read {label} file") from exc
    if not value:
        raise ViewerSecurityConfigurationError(f"{label} file is empty")
    if len(value) > 4096:
        raise ViewerSecurityConfigurationError(f"{label} is unexpectedly large")
    return value


def _validate_readable_file(path: Path | None, *, label: str) -> None:
    if path is None:
        raise ViewerSecurityConfigurationError(f"{label} file is missing")
    try:
        metadata = path.stat()
    except OSError as exc:
        raise ViewerSecurityConfigurationError(f"cannot access {label} file") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ViewerSecurityConfigurationError(f"{label} must be a regular file")


def _validate_private_file(path: Path | None, *, label: str) -> None:
    _validate_readable_file(path, label=label)
    assert path is not None
    if path.stat().st_mode & 0o077:
        raise ViewerSecurityConfigurationError(
            f"{label} file must not be readable or writable by group/others"
        )


def _validate_private_database(path: Path) -> None:
    _validate_readable_file(path, label="Agent Bridge database")
    metadata = path.stat()
    if metadata.st_uid != os.getuid():
        raise ViewerSecurityConfigurationError(
            "Agent Bridge database must be owned by the service account"
        )
    if metadata.st_mode & 0o077:
        raise ViewerSecurityConfigurationError(
            "public mode requires Agent Bridge database permissions 0600"
        )
    parent_mode = path.parent.stat().st_mode
    if parent_mode & 0o022:
        raise ViewerSecurityConfigurationError(
            "Agent Bridge database directory must not be writable by group/others"
        )


def _validate_private_directory(path: Path, *, label: str) -> None:
    try:
        metadata = path.stat()
    except OSError as exc:
        raise ViewerSecurityConfigurationError(f"cannot access {label}") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise ViewerSecurityConfigurationError(f"{label} must be a directory")
    if metadata.st_uid != os.getuid():
        raise ViewerSecurityConfigurationError(
            f"{label} must be owned by the service account"
        )
    if metadata.st_mode & 0o077:
        raise ViewerSecurityConfigurationError(
            f"public mode requires {label} permissions 0700"
        )


def _validate_forwarded_allow_ips(value: str) -> None:
    raw = str(value or "").strip()
    if not raw:
        return
    if raw == "*":
        raise ViewerSecurityConfigurationError(
            "AGENT_BRIDGE_FORWARDED_ALLOW_IPS cannot trust every source"
        )
    for item in _csv_values(raw):
        try:
            ipaddress.ip_network(item, strict=False)
        except ValueError as exc:
            raise ViewerSecurityConfigurationError(
                f"invalid trusted proxy IP/CIDR: {item}"
            ) from exc


def _bounded_int(
    value: str | None,
    *,
    default: int,
    minimum: int,
    maximum: int,
    label: str,
) -> int:
    try:
        parsed = int(value) if value is not None else int(default)
    except (TypeError, ValueError) as exc:
        raise ViewerSecurityConfigurationError(f"{label} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise ViewerSecurityConfigurationError(
            f"{label} must be between {minimum} and {maximum}"
        )
    return parsed
