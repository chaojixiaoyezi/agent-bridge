"""Bounded local HTTP, event-stream, file relay, and endpoint lock helpers."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import socket
import time
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .tui_binding import NativeTuiBinding, NativeTuiError


MAX_HTTP_RESPONSE_BYTES = 16 * 1024 * 1024


MAX_JSONL_READ_BYTES = 16 * 1024 * 1024


MAX_SSE_LINE_BYTES = 1024 * 1024


MAX_SSE_EVENT_BYTES = 16 * 1024 * 1024


MAX_WEBSOCKET_MESSAGE_BYTES = 16 * 1024 * 1024


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _open_local(request: Request, *, timeout: float):
    # Bindings are deliberately loopback-only. Disable environment proxies and
    # redirects so a local endpoint cannot forward prompts or bearer tokens to
    # a remote host after validation has already succeeded.
    opener = build_opener(ProxyHandler({}), _RejectRedirects())
    return opener.open(request, timeout=max(1.0, timeout))


def _bounded_read(handle: Any, *, limit: int, source: str) -> bytes:
    raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise NativeTuiError(f"{source} exceeded the {limit}-byte safety limit")
    return raw


def endpoint_lock_path(binding: NativeTuiBinding, *, state_root: Path) -> Path:
    digest = hashlib.sha256(binding.endpoint_id.encode("utf-8")).hexdigest()[:32]
    directory = state_root.expanduser().resolve() / "tui-endpoints" / digest
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory / "turn.lock"


@contextlib.contextmanager
def endpoint_turn_lock(path: Path, *, blocking: bool = True) -> Iterator[bool]:
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(descriptor, flags)
            acquired = True
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _json_request(
    url: str,
    payload: dict[str, Any],
    *,
    timeout: float,
    expected_empty: bool = False,
) -> Any:
    _, value, _ = _json_http_request(
        url,
        payload,
        timeout=timeout,
        expected_statuses={200, 201, 202, 204},
        expected_empty=expected_empty,
    )
    return value


def _json_http_request(
    url: str,
    payload: dict[str, Any],
    *,
    timeout: float,
    expected_statuses: set[int],
    expected_empty: bool = False,
    headers: dict[str, str] | None = None,
) -> tuple[int, Any, dict[str, str]]:
    request_headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        **(headers or {}),
    }
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=request_headers,
        method="POST",
    )
    try:
        with _open_local(request, timeout=timeout) as response:
            status = int(response.status)
            raw = _bounded_read(
                response,
                limit=MAX_HTTP_RESPONSE_BYTES,
                source="native TUI HTTP response",
            )
            response_headers = {
                str(key).lower(): str(value) for key, value in response.headers.items()
            }
    except HTTPError as exc:
        detail = exc.read(501).decode("utf-8", errors="replace")[:500]
        raise NativeTuiError(f"native TUI HTTP {exc.code}: {detail}") from exc
    except (URLError, OSError) as exc:
        raise NativeTuiError(f"native TUI endpoint is unavailable: {exc}") from exc
    if status not in expected_statuses:
        raise NativeTuiError(f"native TUI HTTP returned unexpected status {status}")
    if not raw and expected_empty:
        return status, None, response_headers
    try:
        return status, json.loads(raw.decode("utf-8")), response_headers
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise NativeTuiError("native TUI returned invalid JSON") from exc


def _json_http_get(
    url: str,
    *,
    timeout: float,
    headers: dict[str, str] | None = None,
) -> Any:
    request = Request(
        url,
        headers={"Accept": "application/json", **(headers or {})},
        method="GET",
    )
    try:
        with _open_local(request, timeout=timeout) as response:
            status = int(response.status)
            raw = _bounded_read(
                response,
                limit=MAX_HTTP_RESPONSE_BYTES,
                source="native TUI HTTP response",
            )
    except HTTPError as exc:
        detail = exc.read(501).decode("utf-8", errors="replace")[:500]
        raise NativeTuiError(f"native TUI HTTP {exc.code}: {detail}") from exc
    except (URLError, OSError) as exc:
        raise NativeTuiError(f"native TUI endpoint is unavailable: {exc}") from exc
    if status != 200:
        raise NativeTuiError(f"native TUI HTTP returned unexpected status {status}")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise NativeTuiError("native TUI returned invalid JSON") from exc


def _text_parts(value: Any) -> str:
    texts: list[str] = []
    if isinstance(value, dict):
        if value.get("type") == "text" and isinstance(value.get("text"), str):
            texts.append(value["text"])
        elif value.get("role") == "assistant" and isinstance(value.get("content"), str):
            texts.append(value["content"])
        for nested in value.values():
            texts.append(_text_parts(nested))
    elif isinstance(value, list):
        for nested in value:
            texts.append(_text_parts(nested))
    return "".join(texts)


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    encoded = (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _jsonl_events(path: Path, *, offset: int) -> tuple[list[dict[str, Any]], int]:
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            chunk = _bounded_read(
                handle,
                limit=MAX_JSONL_READ_BYTES,
                source="native TUI JSONL event batch",
            )
            next_offset = handle.tell()
    except FileNotFoundError:
        return [], offset
    if not chunk:
        return [], next_offset
    # A producer appends one complete JSON object per line. Keep an incomplete
    # tail for the next read by advancing only through the final newline.
    complete_length = chunk.rfind(b"\n") + 1
    if complete_length == 0:
        return [], offset
    events: list[dict[str, Any]] = []
    for line in chunk[:complete_length].splitlines():
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            events.append(value)
    return events, offset + complete_length


def _sse_json_events(
    url: str,
    *,
    headers: dict[str, str],
    timeout: float,
) -> Iterator[dict[str, Any]]:
    request = Request(
        url,
        headers={"Accept": "text/event-stream", **headers},
        method="GET",
    )
    try:
        with _open_local(request, timeout=timeout) as response:
            if int(response.status) != 200:
                raise NativeTuiError(
                    f"Qwen Code SSE returned unexpected status {response.status}"
                )
            data_lines: list[str] = []
            event_bytes = 0
            while True:
                raw = response.readline(MAX_SSE_LINE_BYTES + 1)
                if not raw:
                    break
                if len(raw) > MAX_SSE_LINE_BYTES:
                    raise NativeTuiError("Qwen Code SSE line exceeded the safety limit")
                try:
                    line = raw.decode("utf-8").rstrip("\r\n")
                except UnicodeError:
                    continue
                if line:
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                        event_bytes += len(raw)
                        if event_bytes > MAX_SSE_EVENT_BYTES:
                            raise NativeTuiError(
                                "Qwen Code SSE event exceeded the safety limit"
                            )
                    continue
                if not data_lines:
                    continue
                encoded = "\n".join(data_lines)
                data_lines.clear()
                event_bytes = 0
                try:
                    event = json.loads(encoded)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    yield event
    except HTTPError as exc:
        detail = exc.read(501).decode("utf-8", errors="replace")[:500]
        raise NativeTuiError(f"Qwen Code SSE HTTP {exc.code}: {detail}") from exc
    except (TimeoutError, socket.timeout, URLError, OSError) as exc:
        raise NativeTuiError(f"Qwen Code SSE is unavailable: {exc}") from exc


def _qwen_event_session_id(event: dict[str, Any]) -> str:
    direct = str(event.get("session_id") or event.get("sessionId") or "").strip()
    if direct:
        return direct
    data = event.get("data")
    if isinstance(data, dict):
        return str(data.get("session_id") or data.get("sessionId") or "").strip()
    return ""


def _qwen_event_prompt_id(event: dict[str, Any]) -> str:
    direct = str(event.get("promptId") or event.get("prompt_id") or "").strip()
    if direct:
        return direct
    data = event.get("data")
    if not isinstance(data, dict):
        return ""
    direct = str(data.get("promptId") or data.get("prompt_id") or "").strip()
    if direct:
        return direct
    metadata = data.get("_meta")
    if isinstance(metadata, dict):
        prompt_id = str(
            metadata.get("promptId") or metadata.get("prompt_id") or ""
        ).strip()
        if prompt_id:
            return prompt_id
    update = data.get("update")
    if isinstance(update, dict):
        metadata = update.get("_meta")
        if isinstance(metadata, dict):
            return str(
                metadata.get("promptId") or metadata.get("prompt_id") or ""
            ).strip()
    return ""


def _qwen_session_update(event: dict[str, Any]) -> dict[str, Any]:
    """Return the ACP update across Qwen daemon envelope generations."""

    data = event.get("data")
    if not isinstance(data, dict):
        return {}
    # Qwen Code 0.21 nests the ACP payload below data.update so data can also
    # carry sessionId. Older daemon builds exposed the update directly as
    # data. Accept both because installed native runtimes upgrade separately
    # from Agent Bridge.
    update = data.get("update")
    return update if isinstance(update, dict) else data


def _wait_jsonl_result(
    path: Path,
    *,
    request_id: str,
    offset: int,
    timeout: float,
    poll_inputs: Callable[[], list[dict[str, Any]]] | None,
    steer: Callable[[str, str], None],
) -> tuple[str, list[str]]:
    deadline = time.monotonic() + timeout
    applied: list[str] = []
    queued: set[str] = set()
    remainder = b""
    while time.monotonic() < deadline:
        if poll_inputs is not None:
            for item in poll_inputs():
                input_id = str(item.get("input_id") or "")
                if input_id and input_id not in applied and input_id not in queued:
                    steer(
                        input_id,
                        str(item.get("body") or item.get("body_text") or ""),
                    )
                    queued.add(input_id)
        try:
            with path.open("rb") as handle:
                handle.seek(offset)
                chunk = _bounded_read(
                    handle,
                    limit=MAX_JSONL_READ_BYTES,
                    source="native TUI JSONL result batch",
                )
                offset = handle.tell()
        except FileNotFoundError:
            chunk = b""
        if chunk:
            lines = (remainder + chunk).split(b"\n")
            remainder = lines.pop()
            for line in lines:
                try:
                    event = json.loads(line.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError):
                    continue
                if str(event.get("request_id") or "") != request_id:
                    continue
                event_type = str(event.get("type") or "")
                if event_type == "steer-accepted":
                    input_id = str(event.get("input_id") or "")
                    if input_id and input_id not in applied:
                        applied.append(input_id)
                    continue
                if event_type in {"error", "failed"}:
                    raise NativeTuiError(
                        str(event.get("error") or "native TUI turn failed")
                    )
                if event_type in {"result", "complete", "agent_end"}:
                    text = str(
                        event.get("text")
                        or event.get("result")
                        or event.get("response")
                        or ""
                    ).strip()
                    return text or "任务已完成；原生 TUI 未返回额外摘要。", applied
        time.sleep(0.25)
    raise NativeTuiError("native TUI turn exceeded the execution timeout")
