"""Persistent Codex app-server host for structured room tasks."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from .codex_worker import JsonRpcProcess
from .executables import resolve_executable_path
from .task_worker_common import (
    THREAD_ID_PATTERN,
    TaskWorkerError,
    _private_write,
    _read_thread_id,
    _task_developer_instructions,
    _task_input_prompt,
)


class CodexTaskHost:
    def __init__(self, *, state_file: Path, source_thread_id: str | None) -> None:
        self.state_file = state_file.expanduser().resolve()
        self.source_thread_id = str(source_thread_id or "").strip().casefold() or None
        if self.source_thread_id and not THREAD_ID_PATTERN.fullmatch(
            self.source_thread_id
        ):
            self.source_thread_id = None
        self.rpc: JsonRpcProcess | None = None
        self.thread_id: str | None = None

    def start(
        self,
        *,
        binary: str,
        cwd: Path,
        mcp_arguments: list[str],
        environment: dict[str, str],
    ) -> None:
        resolved = resolve_executable_path(binary)
        if resolved is None:
            raise TaskWorkerError("Codex CLI was not found")
        self.rpc = JsonRpcProcess(
            [resolved, "app-server", "--stdio", *mcp_arguments],
            cwd=cwd,
            environment=environment,
        )
        self.rpc.start()
        existing = _read_thread_id(self.state_file)
        if existing:
            response = self.rpc.request(
                "thread/resume",
                {
                    "threadId": existing,
                    "cwd": str(cwd),
                    "developerInstructions": _task_developer_instructions(),
                    "excludeTurns": False,
                },
                timeout=60,
            )
        elif self.source_thread_id:
            try:
                response = self.rpc.request(
                    "thread/fork",
                    {
                        "threadId": self.source_thread_id,
                        "cwd": str(cwd),
                        "developerInstructions": _task_developer_instructions(),
                        "excludeTurns": False,
                    },
                    timeout=60,
                )
            except Exception:
                response = self._start_new(cwd)
        else:
            response = self._start_new(cwd)
        thread = response.get("thread")
        if not isinstance(thread, dict):
            raise TaskWorkerError("Codex task thread setup omitted metadata")
        thread_id = str(thread.get("id") or "").strip().casefold()
        if not THREAD_ID_PATTERN.fullmatch(thread_id):
            raise TaskWorkerError("Codex returned an invalid task thread id")
        self.thread_id = thread_id
        _private_write(self.state_file, thread_id)
        try:
            self.rpc.request(
                "thread/name/set",
                {"threadId": thread_id, "name": "Agent Bridge 任务执行席位"},
            )
        except Exception:
            pass

    def _start_new(self, cwd: Path) -> dict[str, Any]:
        if self.rpc is None:
            raise TaskWorkerError("Codex task RPC is not started")
        return self.rpc.request(
            "thread/start",
            {
                "cwd": str(cwd),
                "serviceName": "agent-bridge-task-executor",
                "developerInstructions": _task_developer_instructions(),
            },
            timeout=60,
        )

    def run(
        self,
        prompt: str,
        *,
        poll_inputs: Callable[[], list[dict[str, Any]]] | None = None,
    ) -> tuple[str, list[str]]:
        if self.rpc is None or self.thread_id is None:
            raise TaskWorkerError("Codex task host is not initialized")
        def start_turn(text: str) -> str:
            response = self.rpc.request(
                "turn/start",
                {
                    "threadId": self.thread_id,
                    "input": [{"type": "text", "text": text, "textElements": []}],
                },
            )
            turn = response.get("turn")
            if not isinstance(turn, dict) or not str(turn.get("id") or "").strip():
                raise TaskWorkerError("Codex task turn did not start")
            return str(turn["id"])

        turn_id = start_turn(prompt)
        final_text = ""
        deadline = time.monotonic() + 6 * 60 * 60
        next_input_poll = 0.0
        pending_inputs: dict[str, dict[str, Any]] = {}
        injected_input_ids: set[str] = set()
        while time.monotonic() < deadline:
            now = time.monotonic()
            if poll_inputs is not None and now >= next_input_poll:
                next_input_poll = now + 0.5
                try:
                    for item in poll_inputs():
                        input_id = str(item.get("input_id") or "")
                        if input_id and input_id not in injected_input_ids:
                            pending_inputs[input_id] = item
                except Exception:
                    # The durable input remains unapplied and is redelivered.
                    # A temporary Bridge outage must not abort local work.
                    pass
            if pending_inputs:
                updates = list(pending_inputs.values())
                update_prompt = _task_input_prompt(updates)
                try:
                    response = self.rpc.request(
                        "turn/steer",
                        {
                            "threadId": self.thread_id,
                            "input": [
                                {
                                    "type": "text",
                                    "text": update_prompt,
                                    "textElements": [],
                                }
                            ],
                            "expectedTurnId": turn_id,
                        },
                    )
                    steered_turn_id = str(response.get("turnId") or "").strip()
                    if steered_turn_id:
                        turn_id = steered_turn_id
                except Exception as exc:
                    if "no active turn" not in str(exc).casefold():
                        time.sleep(0.1)
                        continue
                    turn_id = start_turn(update_prompt)
                    final_text = ""
                for item in updates:
                    input_id = str(item["input_id"])
                    injected_input_ids.add(input_id)
                    pending_inputs.pop(input_id, None)
            notification = self.rpc.poll_notification()
            if notification is None:
                time.sleep(0.1)
                continue
            params = notification.get("params")
            if not isinstance(params, dict):
                continue
            if notification.get("method") == "item/completed":
                if str(params.get("turnId") or "") != turn_id:
                    continue
                item = params.get("item")
                if isinstance(item, dict) and item.get("type") == "agentMessage":
                    text_value = item.get("text")
                    if isinstance(text_value, str):
                        final_text = text_value.strip()
            if notification.get("method") != "turn/completed":
                continue
            completed = params.get("turn")
            if not isinstance(completed, dict):
                raise TaskWorkerError("Codex task completion omitted turn metadata")
            if str(completed.get("id") or "") != turn_id:
                continue
            status = str(completed.get("status") or "")
            if status != "completed":
                raise TaskWorkerError(
                    "Codex task turn ended with status " + (status or "unknown")
                )
            if poll_inputs is not None:
                try:
                    for item in poll_inputs():
                        input_id = str(item.get("input_id") or "")
                        if input_id and input_id not in injected_input_ids:
                            pending_inputs[input_id] = item
                except Exception:
                    pass
            if pending_inputs:
                updates = list(pending_inputs.values())
                turn_id = start_turn(_task_input_prompt(updates))
                final_text = ""
                for item in updates:
                    input_id = str(item["input_id"])
                    injected_input_ids.add(input_id)
                    pending_inputs.pop(input_id, None)
                continue
            return (
                final_text or "任务已完成；执行席位未返回额外摘要。",
                sorted(injected_input_ids),
            )
        raise TaskWorkerError("Codex task exceeded the execution timeout")

    def close(self) -> None:
        if self.rpc is not None:
            self.rpc.close()
