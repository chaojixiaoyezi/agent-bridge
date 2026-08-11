from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, BinaryIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


class ListenerError(RuntimeError):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-bridge-listen",
        description=(
            "Keep one lightweight SSE connection open and forward metadata-only "
            "wake events to stdout or a loopback supervisor webhook."
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
        help="Optional loopback HTTP endpoint for a local Agent supervisor",
    )
    parser.add_argument(
        "--cursor-file",
        help="Optional file containing only the last SSE sequence (never a token)",
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


def _dispatch_event(event: dict[str, Any], webhook: str | None) -> None:
    encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    if webhook is None:
        sys.stdout.buffer.write(encoded + b"\n")
        sys.stdout.buffer.flush()
        return
    request = Request(
        webhook,
        data=encoded,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "agent-bridge-listener/0.2",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            response.read(65_536)
            if not 200 <= response.status < 300:
                raise ListenerError(f"wake webhook returned HTTP {response.status}")
    except (HTTPError, URLError, OSError) as exc:
        raise ListenerError(f"cannot deliver wake event to local supervisor: {exc}") from exc


def listen(
    *,
    base_url: str,
    access_token: str,
    webhook: str | None,
    cursor_file: Path | None,
    once: bool = False,
) -> None:
    cursor = _read_cursor(cursor_file)
    delay = 1.0
    while True:
        headers = {
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {access_token}",
            "Cache-Control": "no-cache",
            "User-Agent": "agent-bridge-listener/0.2",
        }
        if cursor:
            headers["Last-Event-ID"] = str(cursor)
        request = Request(f"{base_url}/agent/events", headers=headers, method="GET")
        try:
            with urlopen(request, timeout=45) as response:
                if response.headers.get_content_type() != "text/event-stream":
                    raise ListenerError("Bridge did not return an SSE stream")
                delay = 1.0
                for event in _iter_sse_events(response):
                    _dispatch_event(event, webhook)
                    if event["id"] is not None:
                        # The server may deliberately clamp a corrupt or stale
                        # cursor after a database restore. Persist the exact id
                        # it issued so future messages cannot stay suppressed.
                        cursor = int(event["id"])
                        _write_cursor(cursor_file, cursor)
                    if once:
                        return
        except KeyboardInterrupt:
            return
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise ListenerError(
                    "Agent session is no longer authorized; register a new session "
                    "for the same identity, then restart the listener"
                ) from exc
        except (URLError, OSError, ListenerError) as exc:
            print(f"agent-bridge-listener: {exc}; reconnecting", file=sys.stderr)
        time.sleep(delay)
        delay = min(delay * 2.0, 30.0)


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    access_token = os.environ.get("AGENT_BRIDGE_TOKEN", "").strip()
    if not access_token:
        raise SystemExit("AGENT_BRIDGE_TOKEN is required and must not be passed on argv")
    try:
        base_url = _validated_base_url(
            args.url,
            allow_insecure_http=bool(args.allow_insecure_http),
        )
        webhook = _validated_webhook(args.webhook)
        cursor_file = Path(args.cursor_file).expanduser() if args.cursor_file else None
        listen(
            base_url=base_url,
            access_token=access_token,
            webhook=webhook,
            cursor_file=cursor_file,
            once=bool(args.once),
        )
    except ListenerError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
