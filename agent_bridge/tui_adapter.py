from __future__ import annotations

import contextlib
import concurrent.futures
import hashlib
import json
import os
import re
import socket
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


NATIVE_TUI_ADAPTERS = {
    "deepseek-harness",
    "opencode",
    "hermes",
    "pi",
    "qwen-code",
}
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
MAX_PROMPT_CHARS = 100_000
MAX_HTTP_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_JSONL_READ_BYTES = 16 * 1024 * 1024
MAX_SSE_LINE_BYTES = 1024 * 1024
MAX_SSE_EVENT_BYTES = 16 * 1024 * 1024
MAX_WEBSOCKET_MESSAGE_BYTES = 16 * 1024 * 1024
DEFAULT_TURN_TIMEOUT_SECONDS = 3_600.0
QWEN_CLIENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class NativeTuiError(RuntimeError):
    pass


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


@dataclass(frozen=True)
class NativeTuiBinding:
    adapter_kind: str
    endpoint_id: str
    native_session_id: str
    capabilities: tuple[str, ...]
    transport: dict[str, Any]

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "adapter_kind": self.adapter_kind,
            "endpoint_id": self.endpoint_id,
            "native_session_id": self.native_session_id,
            "capabilities": list(self.capabilities),
            "transport": self.transport,
        }


def _identifier(value: object, *, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 256 or "\x00" in normalized:
        raise NativeTuiError(f"{field} must contain 1-256 safe characters")
    if any(character.isspace() for character in normalized):
        raise NativeTuiError(f"{field} cannot contain whitespace")
    return normalized


def _local_url(
    value: object,
    *,
    schemes: set[str],
    field: str,
    allow_query: bool = False,
) -> str:
    normalized = str(value or "").strip().rstrip("/")
    parsed = urlparse(normalized)
    if (
        parsed.scheme not in schemes
        or parsed.hostname not in LOOPBACK_HOSTS
        or parsed.username
        or parsed.password
        or parsed.fragment
        or (parsed.query and not allow_query)
    ):
        raise NativeTuiError(f"{field} must be a safe loopback URL")
    return normalized


def _local_file(value: object, *, field: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise NativeTuiError(f"{field} is required")
    path = Path(raw).expanduser().resolve()
    if not path.is_absolute() or not path.parent.is_dir():
        raise NativeTuiError(f"{field} parent directory does not exist")
    return str(path)


def validate_native_tui_binding(
    *,
    adapter_kind: str,
    endpoint_id: str,
    native_session_id: str,
    access_mode: object = None,
    transport: dict[str, Any] | None = None,
    capabilities: list[str] | tuple[str, ...] | None = None,
) -> NativeTuiBinding:
    adapter = str(adapter_kind or "").strip().lower()
    if adapter not in NATIVE_TUI_ADAPTERS:
        raise NativeTuiError("unsupported native TUI adapter")
    endpoint = _identifier(endpoint_id, field="tui_endpoint_id")
    native_session = _identifier(
        native_session_id,
        field="tui_native_session_id",
    )
    # Kept as an ignored compatibility argument for v1 callers. Permissions
    # belong to the live TUI runtime and can change between turns; persisting a
    # claimed mode here would be stale and could never grant real authority.
    del access_mode
    raw_transport = dict(transport or {})
    kind = str(raw_transport.get("kind") or adapter).strip().lower()
    normalized: dict[str, Any]
    if adapter == "deepseek-harness":
        if kind not in {"deepseek-harness", "deepseek-http"}:
            raise NativeTuiError("DeepSeek Harness requires deepseek-http transport")
        normalized = {
            "kind": "deepseek-http",
            "base_url": _local_url(
                raw_transport.get("base_url"),
                schemes={"http"},
                field="DeepSeek Harness base_url",
            ),
        }
    elif adapter == "opencode":
        if kind not in {"opencode", "opencode-http"}:
            raise NativeTuiError("OpenCode requires opencode-http transport")
        normalized = {
            "kind": "opencode-http",
            "base_url": _local_url(
                raw_transport.get("base_url"),
                schemes={"http"},
                field="OpenCode base_url",
            ),
        }
        directory = str(raw_transport.get("directory") or "").strip()
        if directory:
            normalized["directory"] = str(Path(directory).expanduser().resolve())
    elif adapter == "hermes":
        if kind not in {"hermes", "hermes-websocket"}:
            raise NativeTuiError("Hermes requires hermes-websocket transport")
        normalized = {
            "kind": "hermes-websocket",
            "websocket_url": _local_url(
                raw_transport.get("websocket_url"),
                schemes={"ws"},
                field="Hermes websocket_url",
                allow_query=True,
            ),
        }
    elif adapter == "pi":
        if kind not in {"pi", "pi-extension"}:
            raise NativeTuiError("Pi requires pi-extension transport")
        normalized = {
            "kind": "pi-extension",
            "command_file": _local_file(
                raw_transport.get("command_file"),
                field="Pi command_file",
            ),
            "event_file": _local_file(
                raw_transport.get("event_file"),
                field="Pi event_file",
            ),
            "session_file": _local_file(
                raw_transport.get("session_file"),
                field="Pi session_file",
            ),
        }
        relay_paths = {
            normalized["command_file"],
            normalized["event_file"],
            normalized["session_file"],
        }
        if len(relay_paths) != 3:
            raise NativeTuiError(
                "Pi command_file, event_file, and session_file must be distinct"
            )
    else:
        if kind in {"qwen-daemon", "qcode-daemon"}:
            normalized = {
                "kind": "qwen-daemon",
                "base_url": _local_url(
                    raw_transport.get("base_url"),
                    schemes={"http"},
                    field="Qwen Code daemon base_url",
                ),
            }
            token_file = str(raw_transport.get("token_file") or "").strip()
            if token_file:
                normalized["token_file"] = _local_file(
                    token_file,
                    field="Qwen Code token_file",
                )
            client_id = str(raw_transport.get("client_id") or "").strip()
            if client_id:
                if QWEN_CLIENT_ID_PATTERN.fullmatch(client_id) is None:
                    raise NativeTuiError(
                        "Qwen Code client_id must contain 1-128 URL-safe characters"
                    )
                normalized["client_id"] = client_id
        elif kind in {"qwen-code", "qwen-dual-file", "qcode"}:
            normalized = {
                "kind": "qwen-dual-file",
                "input_file": _local_file(
                    raw_transport.get("input_file"),
                    field="Qwen Code input_file",
                ),
                "event_file": _local_file(
                    raw_transport.get("event_file"),
                    field="Qwen Code event_file",
                ),
            }
            if normalized["input_file"] == normalized["event_file"]:
                raise NativeTuiError(
                    "Qwen Code input_file and event_file must be distinct"
                )
        else:
            raise NativeTuiError(
                "Qwen Code requires qwen-daemon or qwen-dual-file transport"
            )
    capability_values = tuple(
        sorted(
            {_identifier(item, field="tui_capability") for item in (capabilities or ())}
        )
    )
    return NativeTuiBinding(
        adapter_kind=adapter,
        endpoint_id=endpoint,
        native_session_id=native_session,
        capabilities=capability_values,
        transport=normalized,
    )


def load_native_tui_binding(path: Path) -> NativeTuiBinding:
    try:
        raw = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NativeTuiError("native TUI binding file is unreadable") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") not in {1, 2}:
        raise NativeTuiError("native TUI binding schema is unsupported")
    return validate_native_tui_binding(
        adapter_kind=str(raw.get("adapter_kind") or ""),
        endpoint_id=str(raw.get("endpoint_id") or ""),
        native_session_id=str(raw.get("native_session_id") or ""),
        access_mode=raw.get("access_mode"),
        capabilities=raw.get("capabilities")
        if isinstance(raw.get("capabilities"), list)
        else [],
        transport=raw.get("transport")
        if isinstance(raw.get("transport"), dict)
        else {},
    )


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
    steer: Callable[[str], None],
) -> tuple[str, list[str]]:
    deadline = time.monotonic() + timeout
    applied: list[str] = []
    remainder = b""
    while time.monotonic() < deadline:
        if poll_inputs is not None:
            for item in poll_inputs():
                input_id = str(item.get("input_id") or "")
                if input_id and input_id not in applied:
                    steer(str(item.get("body") or item.get("body_text") or ""))
                    applied.append(input_id)
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


class NativeTuiClient:
    def __init__(self, binding: NativeTuiBinding) -> None:
        self.binding = binding

    def probe(self, *, timeout: float = 5.0) -> dict[str, Any]:
        """Verify the bound endpoint and native session without injecting a turn."""

        kind = str(self.binding.transport["kind"])
        session_id = self.binding.native_session_id
        if kind == "deepseek-http":
            value = self._deepseek_rpc(
                "session.history",
                {"sessionId": session_id, "maxMessages": 1},
                timeout,
            )
            if not isinstance(value, dict) or not isinstance(value.get("events"), list):
                raise NativeTuiError("DeepSeek Harness session history is invalid")
            return {"online": True, "transport": kind}
        if kind == "opencode-http":
            session = quote(session_id, safe="")
            url = f"{self.binding.transport['base_url']}/session/{session}"
            directory = self.binding.transport.get("directory")
            if directory:
                url += "?directory=" + quote(str(directory), safe="")
            value = _json_http_get(url, timeout=timeout)
            if not isinstance(value, dict) or str(value.get("id") or "") != session_id:
                raise NativeTuiError("OpenCode returned a mismatched session")
            return {"online": True, "transport": kind}
        if kind == "hermes-websocket":
            self._probe_hermes(timeout=timeout)
            return {"online": True, "transport": kind}
        if kind == "pi-extension":
            heartbeat_file = Path(str(self.binding.transport["event_file"]) + ".heartbeat")
            try:
                latest = json.loads(heartbeat_file.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise NativeTuiError("Pi extension heartbeat is stale or missing") from exc
            if (
                not isinstance(latest, dict)
                or str(latest.get("endpoint_id") or "") != self.binding.endpoint_id
                or str(latest.get("session_id") or "") != session_id
            ):
                raise NativeTuiError("Pi extension heartbeat identity is mismatched")
            try:
                heartbeat_at = float(latest.get("at") or 0)
            except (TypeError, ValueError, OverflowError) as exc:
                raise NativeTuiError("Pi extension heartbeat timestamp is invalid") from exc
            if heartbeat_at <= 0 or time.time() - heartbeat_at > 30:
                raise NativeTuiError("Pi extension heartbeat is stale or missing")
            return {"online": True, "transport": kind, "heartbeat_at": heartbeat_at}
        if kind == "qwen-daemon":
            headers = self._qwen_headers()
            session = quote(session_id, safe="")
            value = _json_http_get(
                f"{self.binding.transport['base_url']}/session/{session}/status",
                timeout=timeout,
                headers=headers,
            )
            if (
                not isinstance(value, dict)
                or str(value.get("sessionId") or "") != session_id
            ):
                raise NativeTuiError("Qwen Code returned a mismatched session status")
            try:
                client_count = int(value.get("clientCount") or 0)
            except (TypeError, ValueError, OverflowError) as exc:
                raise NativeTuiError("Qwen Code session status is invalid") from exc
            return {
                "online": True,
                "transport": kind,
                "has_active_prompt": bool(value.get("hasActivePrompt")),
                "client_count": client_count,
            }
        if kind == "qwen-dual-file":
            return {
                "online": False,
                "transport": kind,
                "reason": "dual-file mode has no read-only liveness signal",
            }
        raise NativeTuiError("native TUI transport is unsupported")

    def run_turn(
        self,
        prompt: str,
        *,
        timeout: float = DEFAULT_TURN_TIMEOUT_SECONDS,
        poll_inputs: Callable[[], list[dict[str, Any]]] | None = None,
    ) -> tuple[str, list[str]]:
        text = str(prompt or "")
        if not text or len(text) > MAX_PROMPT_CHARS:
            raise NativeTuiError("native TUI prompt must contain 1-100000 characters")
        kind = str(self.binding.transport["kind"])
        if kind == "deepseek-http":
            return self._run_deepseek(text, timeout=timeout, poll_inputs=poll_inputs)
        if kind == "opencode-http":
            return self._run_opencode(text, timeout=timeout, poll_inputs=poll_inputs)
        if kind == "hermes-websocket":
            return self._run_hermes(text, timeout=timeout, poll_inputs=poll_inputs)
        if kind == "pi-extension":
            return self._run_file_relay(
                text,
                timeout=timeout,
                poll_inputs=poll_inputs,
                command_key="command_file",
                event_key="event_file",
            )
        if kind == "qwen-dual-file":
            return self._run_qwen_dual_file(
                text,
                timeout=timeout,
                poll_inputs=poll_inputs,
            )
        if kind == "qwen-daemon":
            return self._run_qwen_daemon(text, timeout=timeout, poll_inputs=poll_inputs)
        raise NativeTuiError("native TUI transport is unsupported")

    def _deepseek_rpc(
        self, method: str, payload: dict[str, Any], timeout: float
    ) -> Any:
        rpc_id = str(uuid.uuid4())
        response = _json_request(
            f"{self.binding.transport['base_url']}/api/{method}",
            {
                "type": "client-request",
                "rpcId": rpc_id,
                "method": method,
                "payload": payload,
            },
            timeout=timeout,
        )
        if not isinstance(response, dict) or response.get("rpcId") != rpc_id:
            raise NativeTuiError("DeepSeek Harness returned a mismatched RPC response")
        result = response.get("result")
        if not isinstance(result, dict) or not result.get("ok"):
            error = result.get("error") if isinstance(result, dict) else None
            raise NativeTuiError(f"DeepSeek Harness RPC failed: {error}")
        return result.get("value")

    @staticmethod
    def _deepseek_events(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, dict) or not isinstance(value.get("events"), list):
            return []
        events: list[dict[str, Any]] = []
        for entry in value["events"]:
            if isinstance(entry, dict) and isinstance(entry.get("event"), dict):
                events.append(entry["event"])
        return events

    def _run_deepseek(
        self,
        prompt: str,
        *,
        timeout: float,
        poll_inputs: Callable[[], list[dict[str, Any]]] | None,
    ) -> tuple[str, list[str]]:
        session = self.binding.native_session_id
        before = self._deepseek_events(
            self._deepseek_rpc(
                "session.history",
                {"sessionId": session, "maxMessages": 200},
                15,
            )
        )
        baseline = max((int(event.get("seq") or -1) for event in before), default=-1)
        self._deepseek_rpc(
            "session.prompt",
            {
                "sessionId": session,
                "mode": "queue",
                "content": [{"type": "text", "text": prompt}],
            },
            30,
        )
        deadline = time.monotonic() + timeout
        applied: list[str] = []
        pending_inputs: dict[str, str] = {}
        prompt_sequence: int | None = None
        while time.monotonic() < deadline:
            events = self._deepseek_events(
                self._deepseek_rpc(
                    "session.history",
                    {"sessionId": session, "maxMessages": 200},
                    15,
                )
            )
            fresh = sorted(
                (event for event in events if int(event.get("seq") or -1) > baseline),
                key=lambda event: int(event.get("seq") or -1),
            )
            if prompt_sequence is None:
                for event in fresh:
                    if event.get("type") != "user/message":
                        continue
                    if _text_parts(event.get("data")).strip() == prompt.strip():
                        prompt_sequence = int(event.get("seq") or -1)
                        break
            if poll_inputs is not None:
                for item in poll_inputs():
                    input_id = str(item.get("input_id") or "")
                    if (
                        input_id
                        and input_id not in applied
                        and input_id not in pending_inputs
                    ):
                        pending_inputs[input_id] = str(
                            item.get("body") or item.get("body_text") or ""
                        )
            terminal: dict[str, Any] | None = None
            if prompt_sequence is not None:
                terminal = next(
                    (
                        event
                        for event in fresh
                        if event.get("type") == "turn/end"
                        and int(event.get("seq") or -1) > prompt_sequence
                    ),
                    None,
                )
            if terminal is not None:
                reason = str(
                    ((terminal.get("data") or {}).get("reason") or {}).get("kind") or ""
                )
                if reason in {"error", "aborted", "cancelled", "interrupted"}:
                    raise NativeTuiError(f"DeepSeek Harness turn ended as {reason}")
                if pending_inputs:
                    followup = (
                        "任务执行期间收到以下补充，请继续在同一会话落实：\n"
                        + "\n".join(f"- {value}" for value in pending_inputs.values())
                    )
                    self._deepseek_rpc(
                        "session.prompt",
                        {
                            "sessionId": session,
                            "mode": "queue",
                            "content": [{"type": "text", "text": followup}],
                        },
                        30,
                    )
                    applied.extend(pending_inputs)
                    pending_inputs.clear()
                    baseline = int(terminal.get("seq") or baseline)
                    prompt = followup
                    prompt_sequence = None
                    continue
                eligible = [
                    event
                    for event in fresh
                    if int(event.get("seq") or -1) > prompt_sequence
                    and int(event.get("seq") or -1) < int(terminal.get("seq") or -1)
                ]
                for event in reversed(eligible):
                    if event.get("type") != "assistant/message":
                        continue
                    text = _text_parts((event.get("data") or {}).get("message"))
                    if text.strip():
                        return text.strip(), applied
                return "任务已完成；DeepSeek Harness 未返回额外摘要。", applied
            if prompt_sequence is not None and pending_inputs:
                for input_id, input_text in list(pending_inputs.items()):
                    self._deepseek_rpc(
                        "session.prompt",
                        {
                            "sessionId": session,
                            "mode": "steer",
                            "content": [{"type": "text", "text": input_text}],
                        },
                        30,
                    )
                    applied.append(input_id)
                    pending_inputs.pop(input_id, None)
            time.sleep(0.75)
        raise NativeTuiError("DeepSeek Harness turn exceeded the execution timeout")

    def _run_opencode(
        self,
        prompt: str,
        *,
        timeout: float,
        poll_inputs: Callable[[], list[dict[str, Any]]] | None,
    ) -> tuple[str, list[str]]:
        base = self.binding.transport["base_url"]
        session = quote(self.binding.native_session_id, safe="")
        payload: dict[str, Any] = {"parts": [{"type": "text", "text": prompt}]}
        directory = self.binding.transport.get("directory")
        url = f"{base}/session/{session}/message"
        if directory:
            url += "?directory=" + quote(str(directory), safe="")
        applied: list[str] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_json_request, url, payload, timeout=timeout)
            while True:
                try:
                    response = future.result(timeout=0.25)
                    break
                except concurrent.futures.TimeoutError:
                    pass
                if poll_inputs is None:
                    continue
                for item in poll_inputs():
                    input_id = str(item.get("input_id") or "")
                    if not input_id or input_id in applied:
                        continue
                    _json_request(
                        f"{base}/session/{session}/prompt_async"
                        + (
                            "?directory=" + quote(str(directory), safe="")
                            if directory
                            else ""
                        ),
                        {
                            "parts": [
                                {
                                    "type": "text",
                                    "text": str(
                                        item.get("body") or item.get("body_text") or ""
                                    ),
                                }
                            ]
                        },
                        timeout=min(30, max(1, timeout)),
                        expected_empty=True,
                    )
                    applied.append(input_id)
        result = _text_parts(response).strip()
        return result or "任务已完成；OpenCode 未返回额外摘要。", applied

    def _probe_hermes(self, *, timeout: float) -> None:
        try:
            from websockets.sync.client import connect
        except ImportError as exc:
            raise NativeTuiError(
                "Hermes adapter requires the websockets package"
            ) from exc
        websocket_url = str(self.binding.transport["websocket_url"])
        parsed = urlparse(websocket_url)
        origin = (
            f"http://{parsed.hostname}:{parsed.port}"
            if parsed.port
            else f"http://{parsed.hostname}"
        )
        rpc_id = f"bridge-probe-{uuid.uuid4().hex}"
        deadline = time.monotonic() + timeout
        try:
            with connect(
                websocket_url,
                open_timeout=max(1.0, timeout),
                max_size=MAX_WEBSOCKET_MESSAGE_BYTES,
                origin=origin,
                proxy=None,
            ) as websocket:
                websocket.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": rpc_id,
                            "method": "session.history",
                            "params": {"session_id": self.binding.native_session_id},
                        }
                    )
                )
                while time.monotonic() < deadline:
                    try:
                        raw = websocket.recv(
                            timeout=min(0.5, max(0.05, deadline - time.monotonic()))
                        )
                    except TimeoutError:
                        continue
                    try:
                        value = json.loads(raw)
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if not isinstance(value, dict) or value.get("id") != rpc_id:
                        continue
                    if value.get("error"):
                        raise NativeTuiError(
                            f"Hermes session.history failed: {value['error']}"
                        )
                    result = value.get("result")
                    if not isinstance(result, dict) or not isinstance(
                        result.get("messages"), list
                    ):
                        raise NativeTuiError("Hermes session history is invalid")
                    return
        except NativeTuiError:
            raise
        except Exception as exc:
            raise NativeTuiError(f"Hermes endpoint is unavailable: {exc}") from exc
        raise NativeTuiError("Hermes session probe timed out")

    def _run_hermes(
        self,
        prompt: str,
        *,
        timeout: float,
        poll_inputs: Callable[[], list[dict[str, Any]]] | None,
    ) -> tuple[str, list[str]]:
        try:
            from websockets.sync.client import connect
        except ImportError as exc:
            raise NativeTuiError(
                "Hermes adapter requires the websockets package"
            ) from exc
        websocket_url = str(self.binding.transport["websocket_url"])
        parsed = urlparse(websocket_url)
        origin = (
            f"http://{parsed.hostname}:{parsed.port}"
            if parsed.port
            else f"http://{parsed.hostname}"
        )
        applied: list[str] = []
        pending_inputs: dict[str, str] = {}
        with connect(
            websocket_url,
            open_timeout=15,
            max_size=MAX_WEBSOCKET_MESSAGE_BYTES,
            origin=origin,
            proxy=None,
        ) as websocket:
            deadline = time.monotonic() + timeout
            queued_events: list[dict[str, Any]] = []

            def rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
                rpc_id = f"bridge-{uuid.uuid4().hex}"
                websocket.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": rpc_id,
                            "method": method,
                            "params": params,
                        },
                        ensure_ascii=False,
                    )
                )
                while time.monotonic() < deadline:
                    try:
                        raw = websocket.recv(
                            timeout=min(
                                0.5,
                                max(0.05, deadline - time.monotonic()),
                            )
                        )
                    except TimeoutError:
                        continue
                    try:
                        value = json.loads(raw)
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if not isinstance(value, dict):
                        continue
                    if value.get("id") != rpc_id:
                        queued_events.append(value)
                        continue
                    if value.get("error"):
                        raise NativeTuiError(
                            f"Hermes {method} failed: {value['error']}"
                        )
                    result = value.get("result")
                    return result if isinstance(result, dict) else {}
                raise NativeTuiError(f"Hermes {method} exceeded the execution timeout")

            def history() -> list[dict[str, Any]]:
                result = rpc(
                    "session.history",
                    {"session_id": self.binding.native_session_id},
                )
                values = result.get("messages")
                if not isinstance(values, list):
                    return []
                return [item for item in values if isinstance(item, dict)]

            messages = history()
            baseline_count = len(messages)
            current_prompt = prompt
            rpc(
                "prompt.submit",
                {
                    "session_id": self.binding.native_session_id,
                    "text": current_prompt,
                    "queued": True,
                },
            )
            own_started = False
            while time.monotonic() < deadline:
                if poll_inputs is not None:
                    for item in poll_inputs():
                        input_id = str(item.get("input_id") or "")
                        if (
                            input_id
                            and input_id not in applied
                            and input_id not in pending_inputs
                        ):
                            pending_inputs[input_id] = str(
                                item.get("body") or item.get("body_text") or ""
                            )
                if own_started and pending_inputs:
                    for input_id, input_text in list(pending_inputs.items()):
                        steered = rpc(
                            "session.steer",
                            {
                                "session_id": self.binding.native_session_id,
                                "text": input_text,
                            },
                        )
                        if str(steered.get("status") or "") != "queued":
                            raise NativeTuiError("Hermes rejected a live task input")
                        applied.append(input_id)
                        pending_inputs.pop(input_id, None)
                if queued_events:
                    event = queued_events.pop(0)
                else:
                    try:
                        raw = websocket.recv(
                            timeout=min(
                                0.5,
                                max(0.05, deadline - time.monotonic()),
                            )
                        )
                    except TimeoutError:
                        continue
                    try:
                        event = json.loads(raw)
                    except (TypeError, json.JSONDecodeError):
                        continue
                if not isinstance(event, dict):
                    continue
                params = event.get("params") if isinstance(event, dict) else None
                if not isinstance(params, dict):
                    continue
                event_session = str(params.get("session_id") or "")
                if event_session and event_session != self.binding.native_session_id:
                    continue
                event_type = str(params.get("type") or "")
                if event_type == "message.start":
                    messages = history()
                    own_started = any(
                        str(item.get("role") or "") == "user"
                        and str(item.get("text") or "") == current_prompt
                        for item in messages[baseline_count:]
                    )
                elif event_type in {"error", "message.complete"}:
                    messages = history()
                    tail = messages[baseline_count:]
                    prompt_index = next(
                        (
                            index
                            for index, item in enumerate(tail)
                            if str(item.get("role") or "") == "user"
                            and str(item.get("text") or "") == current_prompt
                        ),
                        None,
                    )
                    if prompt_index is None:
                        continue
                    if event_type == "error":
                        payload = params.get("payload")
                        raise NativeTuiError(
                            str(
                                (
                                    payload.get("message")
                                    if isinstance(payload, dict)
                                    else payload
                                )
                                or "Hermes turn failed"
                            )
                        )
                    if pending_inputs:
                        followup = (
                            "任务执行期间收到以下补充，请继续在同一会话落实：\n"
                            + "\n".join(
                                f"- {value}" for value in pending_inputs.values()
                            )
                        )
                        applied.extend(pending_inputs)
                        pending_inputs.clear()
                        baseline_count = len(messages)
                        current_prompt = followup
                        own_started = False
                        rpc(
                            "prompt.submit",
                            {
                                "session_id": self.binding.native_session_id,
                                "text": current_prompt,
                                "queued": True,
                            },
                        )
                        continue
                    assistant = next(
                        (
                            item
                            for item in reversed(tail[prompt_index + 1 :])
                            if str(item.get("role") or "") == "assistant"
                        ),
                        None,
                    )
                    text = str((assistant or {}).get("text") or "").strip()
                    return text or "任务已完成；Hermes 未返回额外摘要。", applied
        raise NativeTuiError("Hermes turn exceeded the execution timeout")

    def _run_file_relay(
        self,
        prompt: str,
        *,
        timeout: float,
        poll_inputs: Callable[[], list[dict[str, Any]]] | None,
        command_key: str,
        event_key: str,
    ) -> tuple[str, list[str]]:
        command_file = Path(str(self.binding.transport[command_key]))
        event_file = Path(str(self.binding.transport[event_key]))
        offset = event_file.stat().st_size if event_file.exists() else 0
        request_id = f"bridge-{uuid.uuid4().hex}"

        def steer(text: str) -> None:
            _append_jsonl(
                command_file,
                {
                    "type": "steer",
                    "request_id": request_id,
                    "session_id": self.binding.native_session_id,
                    "text": text,
                },
            )

        _append_jsonl(
            command_file,
            {
                "type": "submit",
                "request_id": request_id,
                "session_id": self.binding.native_session_id,
                "text": prompt,
            },
        )
        return _wait_jsonl_result(
            event_file,
            request_id=request_id,
            offset=offset,
            timeout=timeout,
            poll_inputs=poll_inputs,
            steer=steer,
        )

    def _run_qwen_dual_file(
        self,
        prompt: str,
        *,
        timeout: float,
        poll_inputs: Callable[[], list[dict[str, Any]]] | None,
    ) -> tuple[str, list[str]]:
        input_file = Path(str(self.binding.transport["input_file"]))
        event_file = Path(str(self.binding.transport["event_file"]))
        offset = event_file.stat().st_size if event_file.exists() else 0
        pending: list[dict[str, str | None]] = [{"input_id": None, "text": prompt}]
        known_input_ids: set[str] = set()
        applied: list[str] = []
        active: dict[str, str | None] | None = None
        last_assistant = ""
        latest_result = ""
        deadline = time.monotonic() + timeout

        def submit(text: str) -> None:
            # This is Qwen Code's documented dual-output input schema. In
            # particular, request ids and session ids are not accepted here.
            _append_jsonl(input_file, {"type": "submit", "text": text})

        def collect_inputs() -> None:
            if poll_inputs is None:
                return
            for item in poll_inputs():
                input_id = str(item.get("input_id") or "")
                if not input_id or input_id in known_input_ids:
                    continue
                known_input_ids.add(input_id)
                text = str(item.get("body") or item.get("body_text") or "")
                pending.append({"input_id": input_id, "text": text})
                submit(text)

        submit(prompt)
        while time.monotonic() < deadline:
            collect_inputs()
            events, offset = _jsonl_events(event_file, offset=offset)
            for event in events:
                event_session = _qwen_event_session_id(event)
                if event_session and event_session != self.binding.native_session_id:
                    raise NativeTuiError(
                        "Qwen Code dual-output session changed during a Bridge turn"
                    )
                event_type = str(event.get("type") or "")
                if event_type == "system" and event.get("subtype") == "session_start":
                    continue
                if event_type == "user":
                    user_text = _text_parts(event.get("message") or event).strip()
                    if active is None and pending:
                        expected = str(pending[0].get("text") or "").strip()
                        if user_text == expected:
                            active = pending.pop(0)
                            last_assistant = ""
                    continue
                if event_type == "assistant" and active is not None:
                    assistant = _text_parts(event.get("message") or event).strip()
                    if assistant:
                        last_assistant = assistant
                    continue
                if event_type != "result" or active is None:
                    continue
                subtype = str(event.get("subtype") or "").strip().lower()
                if bool(event.get("is_error")) or (
                    subtype and subtype not in {"success", "completed"}
                ):
                    raise NativeTuiError(
                        str(event.get("error") or event.get("result") or subtype)
                    )
                result = str(event.get("result") or "").strip()
                latest_result = result or last_assistant or latest_result
                input_id = str(active.get("input_id") or "")
                if input_id:
                    applied.append(input_id)
                active = None
                collect_inputs()
                if not pending:
                    return (
                        latest_result or "任务已完成；Qwen Code 未返回额外摘要。",
                        applied,
                    )
            time.sleep(0.25)
        raise NativeTuiError(
            "Qwen Code dual-output turn exceeded the execution timeout"
        )

    def _qwen_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        token_file = str(self.binding.transport.get("token_file") or "").strip()
        if token_file:
            try:
                token = Path(token_file).read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError) as exc:
                raise NativeTuiError("Qwen Code token file is unreadable") from exc
            if not token or len(token.encode("utf-8")) > 4096:
                raise NativeTuiError("Qwen Code token file is empty or too large")
            headers["Authorization"] = f"Bearer {token}"
        client_id = str(self.binding.transport.get("client_id") or "").strip()
        if client_id:
            headers["X-Qwen-Client-Id"] = client_id
        return headers

    def _run_qwen_daemon_once(self, prompt: str, *, timeout: float) -> str:
        base = str(self.binding.transport["base_url"])
        session = quote(self.binding.native_session_id, safe="")
        headers = self._qwen_headers()
        _, admission, _ = _json_http_request(
            f"{base}/session/{session}/prompt",
            {"prompt": [{"type": "text", "text": prompt}]},
            timeout=timeout,
            expected_statuses={202},
            headers=headers,
        )
        if not isinstance(admission, dict):
            raise NativeTuiError("Qwen Code prompt admission omitted metadata")
        prompt_id = str(admission.get("promptId") or "").strip()
        last_event_id = admission.get("lastEventId")
        if not prompt_id or not isinstance(last_event_id, (int, str)):
            raise NativeTuiError("Qwen Code prompt admission omitted correlation ids")
        sse_headers = {**headers, "Last-Event-ID": str(last_event_id)}
        own_started = False
        user_candidate = ""
        assistant_chunks: list[str] = []
        for event in _sse_json_events(
            f"{base}/session/{session}/events?connectReason=prompt_restart",
            headers=sse_headers,
            timeout=timeout,
        ):
            event_session = _qwen_event_session_id(event)
            if event_session and event_session != self.binding.native_session_id:
                continue
            event_type = str(event.get("type") or "")
            event_prompt_id = _qwen_event_prompt_id(event)
            data = event.get("data")
            payload = data if isinstance(data, dict) else {}
            if event_type == "session_update":
                session_update = _qwen_session_update(event)
                update = str(session_update.get("sessionUpdate") or "")
                content = session_update.get("content")
                chunk = _text_parts(content).strip()
                if update == "user_message_chunk" and chunk:
                    if event_prompt_id == prompt_id:
                        own_started = True
                    elif prompt.startswith(user_candidate + chunk):
                        user_candidate += chunk
                        own_started = user_candidate == prompt
                    elif prompt.startswith(chunk):
                        user_candidate = chunk
                        own_started = user_candidate == prompt
                    else:
                        user_candidate = ""
                    continue
                if update == "agent_message_chunk" and chunk:
                    if event_prompt_id == prompt_id or own_started:
                        assistant_chunks.append(chunk)
                    continue
            if event_type == "permission_request" and (
                own_started or event_prompt_id == prompt_id
            ):
                raise NativeTuiError(
                    "Qwen Code requested local approval; complete it in the bound "
                    "TUI or adjust that TUI's local permissions"
                )
            if event_type == "turn_error" and event_prompt_id == prompt_id:
                raise NativeTuiError(
                    str(
                        payload.get("error")
                        or payload.get("message")
                        or payload.get("errorKind")
                        or "Qwen Code turn failed"
                    )
                )
            if event_type == "turn_complete" and event_prompt_id == prompt_id:
                stop_reason = str(payload.get("stopReason") or "")
                if stop_reason != "end_turn":
                    raise NativeTuiError(
                        "Qwen Code turn ended as " + (stop_reason or "unknown")
                    )
                result = "".join(assistant_chunks).strip()
                return result or "任务已完成；Qwen Code 未返回额外摘要。"
            if event_type in {
                "client_evicted",
                "session_closed",
                "session_died",
                "state_resync_required",
                "stream_error",
            }:
                raise NativeTuiError(f"Qwen Code SSE ended as {event_type}")
        raise NativeTuiError("Qwen Code SSE ended before the correlated turn completed")

    def _run_qwen_daemon(
        self,
        prompt: str,
        *,
        timeout: float,
        poll_inputs: Callable[[], list[dict[str, Any]]] | None,
    ) -> tuple[str, list[str]]:
        deadline = time.monotonic() + timeout
        applied: list[str] = []
        known_input_ids: set[str] = set()
        pending_inputs: dict[str, str] = {}
        current_prompt = prompt
        current_input_ids: list[str] = []

        def collect_inputs() -> None:
            if poll_inputs is None:
                return
            try:
                values = poll_inputs()
            except Exception:
                return
            for item in values:
                input_id = str(item.get("input_id") or "")
                if not input_id or input_id in known_input_ids:
                    continue
                known_input_ids.add(input_id)
                pending_inputs[input_id] = str(
                    item.get("body") or item.get("body_text") or ""
                )

        latest_result = ""
        while time.monotonic() < deadline:
            remaining = max(1.0, deadline - time.monotonic())
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    self._run_qwen_daemon_once,
                    current_prompt,
                    timeout=remaining,
                )
                while not future.done() and time.monotonic() < deadline:
                    collect_inputs()
                    time.sleep(0.25)
                if not future.done():
                    raise NativeTuiError(
                        "Qwen Code daemon turn exceeded the execution timeout"
                    )
                latest_result = future.result()
            applied.extend(current_input_ids)
            collect_inputs()
            if not pending_inputs:
                return latest_result, applied
            current_input_ids = list(pending_inputs)
            current_prompt = (
                "任务执行期间收到以下补充，请继续在同一会话落实：\n"
                + "\n".join(f"- {value}" for value in pending_inputs.values())
            )
            pending_inputs.clear()
        raise NativeTuiError("Qwen Code daemon turn exceeded the execution timeout")
