from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import sqlite3
import subprocess
import sys
import threading
import time
import tomllib
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .config import (
    read_connector_id,
    read_enrollment_token,
    read_enrollment_token_file,
    read_registration_secret,
)
from .http_client import BridgeHttpClient, BridgeRemoteError
from .supervisor import queue_status


LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
WAKE_PRIORITIES = {"normal": 0, "important": 1, "mention": 2}
RUNTIME_DIAGNOSTIC_INTERVAL_SECONDS = 20.0
RUNTIME_WORKER_ONLINE_SECONDS = 75.0
SENSITIVE_CHILD_ENV = {
    "AGENT_BRIDGE_TOKEN",
    "AGENT_TOKEN",
    "AGENT_BRIDGE_REGISTRATION_SECRET",
    "AGENT_BRIDGE_INVITATION_TOKEN",
    "AGENT_BRIDGE_ENROLLMENT_TOKEN",
    "AGENT_BRIDGE_DB",
    "AGENT_BRIDGE_HOME",
}


class ListenerError(RuntimeError):
    pass


class SessionExpired(ListenerError):
    pass


@dataclass(frozen=True)
class Registration:
    product: str
    username: str
    signature: str
    conversation_id: str
    roles: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()

    def payload(self) -> dict[str, Any]:
        return {
            "product": self.product,
            "username": self.username,
            "signature": self.signature,
            "conversation_id": self.conversation_id,
            "roles": list(self.roles),
            "capabilities": list(self.capabilities),
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-bridge-listen",
        description=(
            "Keep one lightweight SSE connection open, recover a durable backlog, "
            "and forward metadata-only wake events to a local Agent supervisor."
        ),
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("AGENT_BRIDGE_URL", "http://127.0.0.1:8765"),
        help="Agent Bridge base URL (default: AGENT_BRIDGE_URL)",
    )
    parser.add_argument(
        "--webhook",
        default=os.environ.get("AGENT_BRIDGE_WAKE_WEBHOOK"),
        help="Optional loopback HTTP endpoint that durably queues a wake event",
    )
    parser.add_argument(
        "--wake-command-json",
        default=os.environ.get("AGENT_BRIDGE_WAKE_COMMAND_JSON"),
        help=(
            "Optional JSON argv array for a local supervisor command; no shell is "
            "used and notification JSON is supplied on stdin"
        ),
    )
    parser.add_argument(
        "--wake-policy",
        choices=("all", "important", "mention"),
        default=os.environ.get("AGENT_BRIDGE_WAKE_POLICY", "all"),
        help="Minimum notification priority forwarded to the supervisor",
    )
    parser.add_argument(
        "--wake-timeout",
        type=float,
        default=float(os.environ.get("AGENT_BRIDGE_WAKE_TIMEOUT", "10")),
        help="Seconds allowed for a supervisor to durably accept one event",
    )
    parser.add_argument(
        "--cursor-file",
        default=os.environ.get("AGENT_BRIDGE_CURSOR_FILE"),
        help="Optional file containing only the last SSE sequence (never a token)",
    )
    parser.add_argument(
        "--diagnostic-queue-file",
        default=os.environ.get("AGENT_BRIDGE_DIAGNOSTIC_QUEUE_FILE"),
        help=(
            "Optional local supervisor queue used for sanitized connector health "
            "reports"
        ),
    )
    parser.add_argument(
        "--diagnostic-interval",
        type=float,
        default=float(
            os.environ.get(
                "AGENT_BRIDGE_DIAGNOSTIC_INTERVAL",
                str(RUNTIME_DIAGNOSTIC_INTERVAL_SECONDS),
            )
        ),
        help="Minimum seconds between sanitized connector health reports",
    )
    parser.add_argument(
        "--product",
        default=os.environ.get("AGENT_BRIDGE_PRODUCT"),
        help="Stable Agent product used when the listener must auto-register",
    )
    parser.add_argument(
        "--username",
        default=os.environ.get("AGENT_BRIDGE_USERNAME"),
        help="Stable Agent username used when the listener must auto-register",
    )
    parser.add_argument(
        "--signature",
        default=os.environ.get("AGENT_BRIDGE_SIGNATURE"),
        help="One-line Agent signature used when the listener must auto-register",
    )
    parser.add_argument(
        "--conversation",
        default=os.environ.get("AGENT_BRIDGE_CONVERSATION_ID"),
        help="Existing room joined when the listener must auto-register",
    )
    parser.add_argument(
        "--role",
        action="append",
        default=None,
        help="Registration role; repeat for multiple roles",
    )
    parser.add_argument(
        "--capability",
        action="append",
        default=None,
        help="Registration capability; repeat for multiple capabilities",
    )
    parser.add_argument(
        "--allow-insecure-http",
        action="store_true",
        help="Allow cleartext HTTP to a non-loopback Bridge (unsafe without a tunnel)",
    )
    parser.add_argument("--once", action="store_true", help=argparse.SUPPRESS)
    return parser


def _validated_base_url(value: str, *, allow_insecure_http: bool) -> str:
    normalized = str(value or "").strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ListenerError("Bridge URL must be an http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ListenerError("Bridge URL cannot contain credentials or query data")
    if (
        parsed.scheme == "http"
        and parsed.hostname not in LOOPBACK_HOSTS
        and not allow_insecure_http
    ):
        raise ListenerError(
            "refusing to send a bearer token over non-loopback HTTP; use HTTPS "
            "or a private tunnel, or explicitly pass --allow-insecure-http"
        )
    return normalized


def _validated_webhook(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    normalized = value.strip()
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in LOOPBACK_HOSTS:
        raise ListenerError("wake webhook must be an http(s) loopback URL")
    if parsed.username or parsed.password or parsed.fragment:
        raise ListenerError("wake webhook cannot contain credentials or fragments")
    return normalized


def _parse_json_argv(value: str | None) -> tuple[str, ...] | None:
    if value is None or not value.strip():
        return None
    try:
        raw = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ListenerError("wake command must be a JSON argv array") from exc
    if (
        not isinstance(raw, list)
        or not raw
        or len(raw) > 64
        or any(
            not isinstance(item, str) or not item or "\x00" in item or len(item) > 4096
            for item in raw
        )
    ):
        raise ListenerError("wake command must contain 1-64 non-empty string arguments")
    return tuple(raw)


def _split_env_tokens(name: str) -> tuple[str, ...]:
    value = os.environ.get(name, "")
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _registration_from_args(args: argparse.Namespace) -> Registration | None:
    values = {
        "product": str(args.product or "").strip(),
        "username": str(args.username or "").strip(),
        "signature": str(args.signature or "").strip(),
        "conversation_id": str(args.conversation or "").strip(),
    }
    if not any(values.values()):
        return None
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ListenerError(
            "auto-registration requires product, username, signature, and "
            f"conversation; missing: {', '.join(missing)}"
        )
    roles = (
        tuple(args.role)
        if args.role is not None
        else _split_env_tokens("AGENT_BRIDGE_ROLES")
    )
    capabilities = (
        tuple(args.capability)
        if args.capability is not None
        else _split_env_tokens("AGENT_BRIDGE_CAPABILITIES")
    )
    return Registration(
        product=values["product"],
        username=values["username"],
        signature=values["signature"],
        conversation_id=values["conversation_id"],
        roles=roles,
        capabilities=capabilities,
    )


def _read_registration_secret() -> str | None:
    try:
        return read_registration_secret()
    except RuntimeError as exc:
        raise ListenerError(str(exc)) from exc


def _read_enrollment_token() -> str | None:
    try:
        return read_enrollment_token()
    except RuntimeError as exc:
        raise ListenerError(str(exc)) from exc


def _read_connector_id() -> str | None:
    return read_connector_id()


def _read_cursor(path: Path | None) -> int:
    if path is None or not path.exists():
        return 0
    try:
        return max(0, int(path.read_text(encoding="utf-8").strip() or "0"))
    except (OSError, ValueError):
        return 0


def _write_cursor(path: Path | None, cursor: int) -> None:
    if path is None:
        return
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(f"{max(0, int(cursor))}\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _runtime_software_version() -> str:
    project_file = Path(__file__).resolve().parents[1] / "pyproject.toml"
    try:
        with project_file.open("rb") as handle:
            value = str(tomllib.load(handle)["project"]["version"]).strip()
            if value:
                return value[:64]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
        pass
    try:
        return importlib.metadata.version("agent-bridge")[:64]
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _diagnostic_queue_from_command(
    explicit: Path | None,
    command: Sequence[str] | None,
) -> Path | None:
    if explicit is not None:
        return explicit.expanduser()
    if command is None:
        return None
    values = list(command)
    for index, value in enumerate(values[:-1]):
        if value == "--database":
            candidate = str(values[index + 1] or "").strip()
            return Path(candidate).expanduser() if candidate else None
    return None


def _runtime_diagnostic_payload(
    *,
    connector_id: str,
    queue_database: Path | None,
) -> dict[str, Any]:
    queue_payload: dict[str, Any] = {
        "state": "unavailable",
        "pending_count": 0,
        "inflight_count": 0,
        "deferred_count": 0,
        "retrying_count": 0,
        "max_attempt_count": 0,
        "oldest_pending_age_seconds": None,
        "oldest_inflight_age_seconds": None,
        "newest_event_id": None,
    }
    worker_payload: dict[str, Any] = {
        "kind": "unknown",
        "state": "unknown",
        "process_epoch": None,
        "started_age_seconds": None,
        "last_seen_age_seconds": None,
        "last_success_age_seconds": None,
        "last_failure_age_seconds": None,
        "last_error_code": "queue_unavailable",
        "active_adapter_runs": 0,
    }
    listener_state = "degraded"
    if queue_database is not None:
        try:
            local = queue_status(queue_database)
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            local = None
        if local is not None:
            counts = local.get("counts") or {}
            oldest = local.get("oldest_age_seconds") or {}
            queue_payload = {
                "state": "ready",
                "pending_count": int(counts.get("pending") or 0),
                "inflight_count": int(counts.get("inflight") or 0),
                "deferred_count": int(counts.get("deferred") or 0),
                "retrying_count": int(local.get("retrying_count") or 0),
                "max_attempt_count": int(local.get("max_attempt_count") or 0),
                "oldest_pending_age_seconds": oldest.get("pending"),
                "oldest_inflight_age_seconds": oldest.get("inflight"),
                "newest_event_id": local.get("newest_event_id"),
            }
            runtime = local.get("worker")
            if isinstance(runtime, dict):
                worker_state = str(runtime.get("state") or "unknown")
                last_seen_age = runtime.get("last_seen_age_seconds")
                if (
                    last_seen_age is not None
                    and float(last_seen_age) > RUNTIME_WORKER_ONLINE_SECONDS
                ):
                    worker_state = "offline"
                worker_payload = {
                    "kind": str(runtime.get("kind") or "unknown"),
                    "state": worker_state,
                    "process_epoch": runtime.get("process_epoch"),
                    "started_age_seconds": runtime.get("started_age_seconds"),
                    "last_seen_age_seconds": last_seen_age,
                    "last_success_age_seconds": runtime.get(
                        "last_success_age_seconds"
                    ),
                    "last_failure_age_seconds": runtime.get(
                        "last_failure_age_seconds"
                    ),
                    "last_error_code": runtime.get("last_error_code"),
                    "active_adapter_runs": int(
                        local.get("active_adapter_runs") or 0
                    ),
                }
            else:
                worker_payload["last_error_code"] = None
            listener_state = "online"
    return {
        "connector_id": connector_id,
        "protocol_version": 1,
        "software_version": _runtime_software_version(),
        "platform": platform.system()
        if platform.system() in {"Darwin", "Linux", "Windows"}
        else "Other",
        "listener_state": listener_state,
        "queue": queue_payload,
        "worker": worker_payload,
    }


def _post_runtime_diagnostics(
    *,
    base_url: str,
    access_token: str,
    payload: dict[str, Any],
    timeout: float = 3.0,
) -> None:
    request = Request(
        f"{base_url}/agent/connector/runtime-diagnostics",
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        ),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
            "User-Agent": "agent-bridge-listener/0.4",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=max(0.25, min(float(timeout), 10.0))) as response:
            response.read(65_536)
            if not 200 <= response.status < 300:
                raise ListenerError(
                    f"runtime diagnostic endpoint returned HTTP {response.status}"
                )
    except HTTPError as exc:
        raise ListenerError(
            f"runtime diagnostic endpoint returned HTTP {exc.code}"
        ) from exc
    except (URLError, OSError) as exc:
        raise ListenerError("cannot report connector runtime diagnostics") from exc


class RuntimeDiagnosticsReporter:
    """Best-effort reporter that never blocks wake-event delivery."""

    def __init__(
        self,
        *,
        base_url: str,
        connector_id: str | None,
        queue_database: Path | None,
        interval_seconds: float,
    ) -> None:
        self.base_url = base_url
        self.connector_id = str(connector_id or "").strip() or None
        self.queue_database = queue_database
        self.interval_seconds = max(5.0, min(float(interval_seconds), 300.0))
        self._lock = threading.Lock()
        self._inflight = False
        self._next_due = 0.0

    def trigger(self, access_token: str, *, force: bool = False) -> None:
        if self.connector_id is None:
            return
        now = time.monotonic()
        with self._lock:
            if self._inflight or (not force and now < self._next_due):
                return
            self._inflight = True
            self._next_due = now + self.interval_seconds
        threading.Thread(
            target=self._report,
            args=(access_token,),
            daemon=True,
            name="agent-bridge-runtime-diagnostics",
        ).start()

    def report_now(self, access_token: str) -> None:
        if self.connector_id is None:
            return
        self._report_payload(access_token)

    def _report_payload(self, access_token: str) -> None:
        if self.connector_id is None:
            return
        payload = _runtime_diagnostic_payload(
            connector_id=self.connector_id,
            queue_database=self.queue_database,
        )
        _post_runtime_diagnostics(
            base_url=self.base_url,
            access_token=access_token,
            payload=payload,
        )

    def _report(self, access_token: str) -> None:
        try:
            self._report_payload(access_token)
        except (ListenerError, OSError, RuntimeError, ValueError):
            # Diagnostics are deliberately best effort. The persistent SSE and
            # durable local queue remain the delivery authority.
            pass
        finally:
            with self._lock:
                self._inflight = False


def _iter_sse_events(
    stream: BinaryIO | Iterable[bytes],
    *,
    on_keepalive: Callable[[], None] | None = None,
) -> Iterator[dict[str, Any]]:
    event_name = "message"
    event_id: int | None = None
    data_lines: list[str] = []
    for raw_line in stream:
        line = raw_line.decode("utf-8").rstrip("\r\n")
        if not line:
            if data_lines:
                raw_data = "\n".join(data_lines)
                try:
                    data = json.loads(raw_data)
                except json.JSONDecodeError as exc:
                    raise ListenerError("Bridge emitted invalid SSE JSON") from exc
                if not isinstance(data, dict):
                    raise ListenerError("Bridge emitted a non-object SSE payload")
                yield {"event": event_name, "id": event_id, "data": data}
            event_name = "message"
            event_id = None
            data_lines = []
            continue
        if line.startswith(":"):
            if on_keepalive is not None:
                on_keepalive()
            continue
        field, separator, value = line.partition(":")
        if not separator:
            value = ""
        elif value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value
        elif field == "id":
            try:
                event_id = max(0, int(value))
            except ValueError as exc:
                raise ListenerError("Bridge emitted an invalid SSE id") from exc
        elif field == "data":
            data_lines.append(value)


def _event_manifest(event: dict[str, Any]) -> dict[str, Any] | None:
    data = event.get("data")
    if not isinstance(data, dict):
        raise ListenerError("wake event data must be an object")
    event_name = event.get("event")
    if event_name == "backlog":
        manifest = data.get("backlog")
        count_field = "pending_count"
    elif event_name == "message_available":
        manifest = data.get("room_activity_since_cursor")
        count_field = "activity_count"
    elif event_name == "session_closed":
        raise SessionExpired(str(data.get("error") or "Agent session closed"))
    else:
        return None
    if not isinstance(manifest, dict) or int(manifest.get(count_field) or 0) <= 0:
        return None
    return manifest


def _event_priority(manifest: dict[str, Any]) -> str:
    counts = manifest.get("priority_counts")
    if not isinstance(counts, dict):
        return "normal"
    if int(counts.get("mention") or 0) > 0:
        return "mention"
    if int(counts.get("important") or 0) > 0:
        return "important"
    return "normal"


def _wake_envelope(
    event: dict[str, Any],
    *,
    wake_policy: str,
) -> dict[str, Any] | None:
    manifest = _event_manifest(event)
    if manifest is None:
        return None
    priority = _event_priority(manifest)
    minimum = {"all": 0, "important": 1, "mention": 2}[wake_policy]
    if WAKE_PRIORITIES[priority] < minimum:
        return None
    data = event["data"]
    return {
        "schema_version": 1,
        "source": "agent-bridge",
        "event": event["event"],
        "event_id": event.get("id"),
        "participant_id": data.get("participant_id"),
        "cursor": data.get("cursor"),
        "wake_priority": priority,
        "required_reply_count": int(manifest.get("required_reply_count") or 0),
        "has_new": bool(data.get("has_new")),
        "has_room_activity": bool(data.get("has_room_activity")),
        "backlog": data.get("backlog"),
        "new_since_cursor": data.get("new_since_cursor"),
        "room_activity_since_cursor": data.get("room_activity_since_cursor"),
        "server_time": data.get("server_time"),
    }


def _post_webhook(webhook: str, encoded: bytes, *, timeout: float) -> None:
    request = Request(
        webhook,
        data=encoded,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "agent-bridge-listener/0.3",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            response.read(65_536)
            if not 200 <= response.status < 300:
                raise ListenerError(f"wake webhook returned HTTP {response.status}")
    except (HTTPError, URLError, OSError) as exc:
        raise ListenerError("cannot deliver wake event to local supervisor") from exc


def _run_wake_command(
    command: Sequence[str],
    encoded: bytes,
    *,
    timeout: float,
) -> None:
    child_environment = dict(os.environ)
    for name in SENSITIVE_CHILD_ENV:
        child_environment.pop(name, None)
    try:
        completed = subprocess.run(
            list(command),
            input=encoded + b"\n",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=child_environment,
            shell=False,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ListenerError(
            "local wake command did not durably accept the event"
        ) from exc
    if completed.returncode != 0:
        raise ListenerError(
            f"local wake command exited with status {completed.returncode}"
        )


def _dispatch_event(
    event: dict[str, Any],
    webhook: str | None,
    *,
    command: Sequence[str] | None = None,
    wake_policy: str = "all",
    timeout: float = 10.0,
) -> bool:
    envelope = _wake_envelope(event, wake_policy=wake_policy)
    if envelope is None:
        return False
    encoded = json.dumps(
        envelope,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if webhook is not None:
        _post_webhook(webhook, encoded, timeout=timeout)
    if command is not None:
        _run_wake_command(command, encoded, timeout=timeout)
    if webhook is None and command is None:
        sys.stdout.buffer.write(encoded + b"\n")
        sys.stdout.buffer.flush()
    return True


def _register(
    *,
    base_url: str,
    registration: Registration,
    registration_secret: str | None,
    enrollment_token: str | None = None,
    connector_id: str | None = None,
    result_out: dict[str, Any] | None = None,
) -> str:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "agent-bridge-listener/0.3",
    }
    if enrollment_token:
        headers["X-Agent-Bridge-Enrollment"] = enrollment_token
        if connector_id:
            headers["X-Agent-Bridge-Connector"] = connector_id
            headers["X-Agent-Bridge-Component"] = "listener"
            headers["X-Agent-Bridge-Protocol"] = "2"
    elif registration_secret:
        headers["X-Agent-Bridge-Registration"] = registration_secret
    request = Request(
        f"{base_url}/agent/register",
        data=json.dumps(registration.payload(), ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            raw = response.read(1_048_576)
    except HTTPError as exc:
        raise ListenerError(f"Agent registration failed with HTTP {exc.code}") from exc
    except (URLError, OSError) as exc:
        raise ListenerError("cannot reach Agent Bridge for registration") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ListenerError("Agent Bridge returned invalid registration JSON") from exc
    if not isinstance(payload, dict):
        raise ListenerError("Agent Bridge returned a non-object registration response")
    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        raise ListenerError("Agent Bridge registration omitted the access token")
    if result_out is not None:
        result_out.update(payload)
    return access_token


def _rotate_enrollment_if_requested(
    *,
    registration_result: dict[str, Any],
    base_url: str,
    enrollment_token: str | None,
    connector_id: str | None,
    enrollment_token_file: Path | None,
    enrollment_token_loader: Callable[[], str | None] | None,
) -> bool:
    if not bool(registration_result.get("enrollment_rotation_required")):
        return False
    if not enrollment_token or not connector_id or enrollment_token_file is None:
        return False
    client = BridgeHttpClient(
        base_url,
        enrollment_token=enrollment_token,
        connector_id=connector_id,
        enrollment_token_file=enrollment_token_file,
        enrollment_token_loader=enrollment_token_loader,
    )
    try:
        client.rotate_enrollment()
    except (BridgeRemoteError, OSError, RuntimeError) as exc:
        print(
            "agent-bridge-listener: device credential rotation is pending; "
            f"will retry after reconnect ({exc})",
            file=sys.stderr,
        )
        return False
    return True


def listen(
    *,
    base_url: str,
    access_token: str | None,
    registration: Registration | None,
    registration_secret: str | None,
    webhook: str | None,
    command: Sequence[str] | None,
    wake_policy: str,
    wake_timeout: float,
    cursor_file: Path | None,
    enrollment_token: str | None = None,
    connector_id: str | None = None,
    enrollment_token_loader: Callable[[], str | None] | None = None,
    enrollment_token_file: Path | None = None,
    diagnostic_queue_file: Path | None = None,
    diagnostic_interval: float = RUNTIME_DIAGNOSTIC_INTERVAL_SECONDS,
    once: bool = False,
) -> None:
    cursor = _read_cursor(cursor_file)
    delay = 1.0
    current_token = str(access_token or "").strip() or None
    reporter = RuntimeDiagnosticsReporter(
        base_url=base_url,
        connector_id=connector_id,
        queue_database=_diagnostic_queue_from_command(
            diagnostic_queue_file,
            command,
        ),
        interval_seconds=diagnostic_interval,
    )
    if current_token is None and registration is None:
        raise ListenerError(
            "AGENT_BRIDGE_TOKEN is absent and auto-registration identity is incomplete"
        )
    while True:
        try:
            if current_token is None:
                if registration is None:
                    raise ListenerError(
                        "stable registration identity disappeared before reconnect"
                    )
                active_enrollment = (
                    enrollment_token_loader()
                    if enrollment_token_loader is not None
                    else enrollment_token
                )
                registration_result: dict[str, Any] = {}
                current_token = _register(
                    base_url=base_url,
                    registration=registration,
                    registration_secret=registration_secret,
                    enrollment_token=active_enrollment,
                    connector_id=connector_id,
                    result_out=registration_result,
                )
                _rotate_enrollment_if_requested(
                    registration_result=registration_result,
                    base_url=base_url,
                    enrollment_token=active_enrollment,
                    connector_id=connector_id,
                    enrollment_token_file=enrollment_token_file,
                    enrollment_token_loader=enrollment_token_loader,
                )
            headers = {
                "Accept": "text/event-stream",
                "Authorization": f"Bearer {current_token}",
                "Cache-Control": "no-cache",
                "User-Agent": "agent-bridge-listener/0.3",
            }
            if cursor:
                headers["Last-Event-ID"] = str(cursor)
            request = Request(
                f"{base_url}/agent/events",
                headers=headers,
                method="GET",
            )
            with urlopen(request, timeout=45) as response:
                if response.headers.get_content_type() != "text/event-stream":
                    raise ListenerError("Bridge did not return an SSE stream")
                delay = 1.0
                for event in _iter_sse_events(
                    response,
                    on_keepalive=lambda: reporter.trigger(str(current_token)),
                ):
                    _dispatch_event(
                        event,
                        webhook,
                        command=command,
                        wake_policy=wake_policy,
                        timeout=wake_timeout,
                    )
                    if event["id"] is not None:
                        # Persist only after every configured supervisor sink has
                        # accepted the event. A reconnect can safely redeliver it.
                        cursor = int(event["id"])
                        _write_cursor(cursor_file, cursor)
                    reporter.trigger(str(current_token))
                    if once:
                        try:
                            reporter.report_now(str(current_token))
                        except (ListenerError, OSError, RuntimeError, ValueError):
                            pass
                        return
        except KeyboardInterrupt:
            return
        except SessionExpired:
            if registration is None:
                raise
            current_token = None
            delay = 1.0
            continue
        except HTTPError as exc:
            if exc.code in {401, 403}:
                if registration is None:
                    raise SessionExpired(
                        "Agent session is no longer authorized and no stable identity "
                        "was configured for automatic recovery"
                    ) from exc
                current_token = None
                delay = 1.0
                continue
            print(
                f"agent-bridge-listener: Bridge returned HTTP {exc.code}; reconnecting",
                file=sys.stderr,
            )
        except (URLError, OSError, ListenerError) as exc:
            print(f"agent-bridge-listener: {exc}; reconnecting", file=sys.stderr)
        time.sleep(delay)
        delay = min(delay * 2.0, 30.0)


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    access_token = os.environ.get("AGENT_BRIDGE_TOKEN", "").strip() or None
    try:
        base_url = _validated_base_url(
            args.url,
            allow_insecure_http=bool(args.allow_insecure_http),
        )
        webhook = _validated_webhook(args.webhook)
        command = _parse_json_argv(args.wake_command_json)
        registration = _registration_from_args(args)
        registration_secret = _read_registration_secret()
        enrollment_token = _read_enrollment_token()
        connector_id = _read_connector_id()
        cursor_file = Path(args.cursor_file).expanduser() if args.cursor_file else None
        diagnostic_queue_file = (
            Path(args.diagnostic_queue_file).expanduser()
            if args.diagnostic_queue_file
            else None
        )
        wake_timeout = max(0.25, min(float(args.wake_timeout), 120.0))
        listen(
            base_url=base_url,
            access_token=access_token,
            registration=registration,
            registration_secret=registration_secret,
            enrollment_token=enrollment_token,
            connector_id=connector_id,
            enrollment_token_loader=_read_enrollment_token,
            enrollment_token_file=read_enrollment_token_file(),
            webhook=webhook,
            command=command,
            wake_policy=args.wake_policy,
            wake_timeout=wake_timeout,
            cursor_file=cursor_file,
            diagnostic_queue_file=diagnostic_queue_file,
            diagnostic_interval=args.diagnostic_interval,
            once=bool(args.once),
        )
    except ListenerError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
