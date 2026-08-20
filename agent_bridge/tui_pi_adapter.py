"""Pi extension native-session file relay."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Callable

from .tui_transport import _append_jsonl, _wait_jsonl_result


class PiTuiMixin:
    """Drive one explicitly bound Pi extension session."""

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

        def steer(input_id: str, text: str) -> None:
            _append_jsonl(
                command_file,
                {
                    "type": "steer",
                    "request_id": request_id,
                    "session_id": self.binding.native_session_id,
                    "input_id": input_id,
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
