from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .config import read_enrollment_token, read_registration_secret


LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
WAKE_PRIORITIES = {"normal": 0, "important": 1, "mention": 2}
SENSITIVE_CHILD_ENV = {
    "AGENT_BRIDGE_TOKEN",
    "AGENT_TOKEN",
    "AGENT_BRIDGE_REGISTRATION_SECRET",
    "AGENT_BRIDGE_INVITATION_TOKEN",
    "AGENT_BRIDGE_ENROLLMENT_TOKEN",
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


def _iter_sse_events(stream: BinaryIO | Iterable[bytes]) -> Iterator[dict[str, Any]]:
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
) -> str:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "agent-bridge-listener/0.3",
    }
    if enrollment_token:
        headers["X-Agent-Bridge-Enrollment"] = enrollment_token
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
    return access_token


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
    once: bool = False,
) -> None:
    cursor = _read_cursor(cursor_file)
    delay = 1.0
    current_token = str(access_token or "").strip() or None
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
                current_token = _register(
                    base_url=base_url,
                    registration=registration,
                    registration_secret=registration_secret,
                    enrollment_token=enrollment_token,
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
                for event in _iter_sse_events(response):
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
                    if once:
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
        cursor_file = Path(args.cursor_file).expanduser() if args.cursor_file else None
        wake_timeout = max(0.25, min(float(args.wake_timeout), 120.0))
        listen(
            base_url=base_url,
            access_token=access_token,
            registration=registration,
            registration_secret=registration_secret,
            enrollment_token=enrollment_token,
            webhook=webhook,
            command=command,
            wake_policy=args.wake_policy,
            wake_timeout=wake_timeout,
            cursor_file=cursor_file,
            once=bool(args.once),
        )
    except ListenerError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
