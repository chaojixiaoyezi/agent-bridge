"""DeepSeek Harness native-session adapter."""

from __future__ import annotations

import time
import uuid
from typing import Any, Callable

from .tui_binding import NativeTuiError
from .tui_transport import _json_request, _text_parts


class DeepSeekTuiMixin:
    """Drive one explicitly bound DeepSeek Harness session."""

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
