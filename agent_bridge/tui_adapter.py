from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote as quote

from .tui_binding import (
    DEFAULT_TURN_TIMEOUT_SECONDS as DEFAULT_TURN_TIMEOUT_SECONDS,
    LOOPBACK_HOSTS as LOOPBACK_HOSTS,
    MAX_PROMPT_CHARS as MAX_PROMPT_CHARS,
    NATIVE_TUI_ADAPTERS as NATIVE_TUI_ADAPTERS,
    QWEN_CLIENT_ID_PATTERN as QWEN_CLIENT_ID_PATTERN,
    NativeTuiBinding as NativeTuiBinding,
    NativeTuiError as NativeTuiError,
    _identifier as _identifier,
    _local_file as _local_file,
    _local_url as _local_url,
    load_native_tui_binding as load_native_tui_binding,
    validate_native_tui_binding as validate_native_tui_binding,
)
from .tui_deepseek_adapter import DeepSeekTuiMixin
from .tui_hermes_adapter import HermesTuiMixin
from .tui_opencode_adapter import OpenCodeTuiMixin
from .tui_pi_adapter import PiTuiMixin
from .tui_qwen_adapter import QwenTuiMixin
from .tui_transport import (
    MAX_HTTP_RESPONSE_BYTES as MAX_HTTP_RESPONSE_BYTES,
    MAX_JSONL_READ_BYTES as MAX_JSONL_READ_BYTES,
    MAX_SSE_EVENT_BYTES as MAX_SSE_EVENT_BYTES,
    MAX_SSE_LINE_BYTES as MAX_SSE_LINE_BYTES,
    MAX_WEBSOCKET_MESSAGE_BYTES as MAX_WEBSOCKET_MESSAGE_BYTES,
    _append_jsonl as _append_jsonl,
    _bounded_read as _bounded_read,
    _json_http_get as _json_http_get,
    _json_http_request as _json_http_request,
    _json_request as _json_request,
    _jsonl_events as _jsonl_events,
    _open_local as _open_local,
    _qwen_event_prompt_id as _qwen_event_prompt_id,
    _qwen_event_session_id as _qwen_event_session_id,
    _qwen_session_update as _qwen_session_update,
    _sse_json_events as _sse_json_events,
    _text_parts as _text_parts,
    _wait_jsonl_result as _wait_jsonl_result,
    endpoint_lock_path as endpoint_lock_path,
    endpoint_turn_lock as endpoint_turn_lock,
)


class NativeTuiClient(
    DeepSeekTuiMixin,
    OpenCodeTuiMixin,
    HermesTuiMixin,
    PiTuiMixin,
    QwenTuiMixin,
):
    """Dispatch a bound native TUI turn to its product-specific adapter."""

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
