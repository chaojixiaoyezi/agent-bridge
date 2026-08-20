"""Hermes WebSocket native-session adapter."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Callable
from urllib.parse import urlparse

from .tui_binding import NativeTuiError
from .tui_transport import MAX_WEBSOCKET_MESSAGE_BYTES


class HermesTuiMixin:
    """Drive one explicitly bound Hermes WebSocket session."""

    def _resolve_hermes_session_id(
        self,
        rpc: Callable[[str, dict[str, Any]], dict[str, Any]],
    ) -> str:
        """Attach a stable Hermes session to this WebSocket when available.

        Hermes runtime session IDs are connection-local and are reclaimed after
        a WebSocket disconnect.  ``stored_session_id`` is durable; resuming it
        returns the current runtime ID and also transfers event delivery to the
        new transport.  The fallback keeps freshly-created, not-yet-persisted
        sessions usable until their first prompt is committed.
        """

        runtime_session_id = self.binding.native_session_id
        stored_session_id = str(
            self.binding.transport.get("stored_session_id") or ""
        ).strip()
        if not stored_session_id:
            return runtime_session_id
        try:
            resumed = rpc(
                "session.resume",
                {
                    "session_id": stored_session_id,
                    "source": "tool",
                },
            )
        except NativeTuiError as exc:
            if "session not found" in str(exc).lower():
                return runtime_session_id
            raise
        resolved = str(resumed.get("session_id") or "").strip()
        if (
            not resolved
            or len(resolved) > 256
            or "\x00" in resolved
            or any(character.isspace() for character in resolved)
        ):
            raise NativeTuiError("Hermes session.resume returned an invalid session")
        return resolved

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

                def rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
                    current_rpc_id = f"{rpc_id}-{uuid.uuid4().hex}"
                    websocket.send(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": current_rpc_id,
                                "method": method,
                                "params": params,
                            }
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
                        if (
                            not isinstance(value, dict)
                            or value.get("id") != current_rpc_id
                        ):
                            continue
                        if value.get("error"):
                            raise NativeTuiError(
                                f"Hermes {method} failed: {value['error']}"
                            )
                        result = value.get("result")
                        return result if isinstance(result, dict) else {}
                    raise NativeTuiError(f"Hermes {method} probe timed out")

                session_id = self._resolve_hermes_session_id(rpc)
                result = rpc("session.history", {"session_id": session_id})
                if not isinstance(result.get("messages"), list):
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
                    {"session_id": session_id},
                )
                values = result.get("messages")
                if not isinstance(values, list):
                    return []
                return [item for item in values if isinstance(item, dict)]

            session_id = self._resolve_hermes_session_id(rpc)
            messages = history()
            baseline_count = len(messages)
            current_prompt = prompt
            rpc(
                "prompt.submit",
                {
                    "session_id": session_id,
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
                                "session_id": session_id,
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
                if event_session and event_session != session_id:
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
                                "session_id": session_id,
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
