"""OpenCode native-session adapter."""

from __future__ import annotations

import concurrent.futures
from typing import Any, Callable
from urllib.parse import quote

from .tui_transport import _json_request, _text_parts


class OpenCodeTuiMixin:
    """Drive one explicitly bound OpenCode session."""

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
