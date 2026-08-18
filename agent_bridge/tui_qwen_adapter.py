"""Qwen Code daemon and dual-file native-session adapters."""

from __future__ import annotations

import concurrent.futures
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from .tui_binding import NativeTuiError
from .tui_transport import (
    _append_jsonl,
    _json_http_request,
    _jsonl_events,
    _qwen_event_prompt_id,
    _qwen_event_session_id,
    _qwen_session_update,
    _sse_json_events,
    _text_parts,
)


class QwenTuiMixin:
    """Drive one explicitly bound Qwen daemon or dual-file session."""

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
