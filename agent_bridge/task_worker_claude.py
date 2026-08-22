"""Persistent Claude Code stream-json host for structured room tasks."""

from __future__ import annotations

import json
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .task_worker_common import (
    TASK_MCP_TOOLS,
    THREAD_ID_PATTERN,
    TaskWorkerError,
    _private_write,
    _task_developer_instructions,
    _task_input_prompt,
)


_RUNTIME_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(sk-[A-Za-z0-9_-]{10,})\b"),
    re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._~+/-]{10,}=?"),
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|authorization|password|secret)"
        r"\s*([=:]\s*|\s+)['\"]?[^\s,'\"}]{6,}"
    ),
)


def _redact_runtime_text(value: object, *, maximum: int = 2_000) -> str:
    text = str(value or "").replace("\x00", "").strip()
    for pattern in _RUNTIME_SECRET_PATTERNS:
        if pattern.pattern.startswith("(?i)\\b(sk-"):
            text = pattern.sub("<redacted-key>", text)
        elif "(Bearer)" in pattern.pattern:
            text = pattern.sub(r"\1 <redacted>", text)
        else:
            text = pattern.sub(r"\1=<redacted>", text)
    if len(text) > maximum:
        return text[: maximum - 1].rstrip() + "…"
    return text


def _claude_tool_label(name: object) -> str:
    value = str(name or "Tool").strip()
    if "__" in value:
        value = value.rsplit("__", 1)[-1]
    return value or "Tool"


def _claude_tool_input_summary(name: object, payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    label = _claude_tool_label(name).casefold()
    preferred = {
        "bash": ("description", "command"),
        "read": ("file_path", "offset", "limit"),
        "write": ("file_path",),
        "edit": ("file_path",),
        "multiedit": ("file_path",),
        "glob": ("pattern", "path"),
        "grep": ("pattern", "path", "glob", "output_mode"),
        "webfetch": ("url", "prompt"),
        "websearch": ("query",),
        "task": ("description", "subagent_type"),
    }.get(label, ("description", "file_path", "path", "pattern", "query", "url"))
    parts: list[str] = []
    for key in preferred:
        value = payload.get(key)
        if value is None or value == "":
            continue
        rendered = _redact_runtime_text(value, maximum=1_200 if key == "command" else 500)
        if rendered:
            parts.append(f"{key}: {rendered}")
    if not parts:
        visible_keys = sorted(
            str(key)
            for key in payload
            if str(key).casefold()
            not in {"content", "old_string", "new_string", "prompt", "password", "token"}
        )
        if visible_keys:
            parts.append("参数: " + "、".join(visible_keys[:12]))
    return _redact_runtime_text("\n".join(parts), maximum=2_000)


def _claude_tool_result_summary(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return _redact_runtime_text(content)
    if isinstance(content, list):
        texts = [
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return _redact_runtime_text("\n".join(texts))
    if isinstance(content, dict):
        return _redact_runtime_text(
            json.dumps(content, ensure_ascii=False, sort_keys=True),
        )
    return ""


def _claude_result_summary(event: dict[str, Any]) -> str:
    parts: list[str] = []
    duration = event.get("duration_ms")
    if isinstance(duration, (int, float)) and duration >= 0:
        parts.append(f"{duration / 1000:.1f}s")
    turns = event.get("num_turns")
    if isinstance(turns, int) and turns >= 0:
        parts.append(f"{turns} 个模型轮次")
    return " · ".join(parts)


def _runtime_failure_event_kind(detail: object) -> str:
    normalized = str(detail or "").casefold()
    return (
        "approval_required"
        if any(
            marker in normalized
            for marker in (
                "permission",
                "approval",
                "not allowed",
                "not permitted",
                "权限",
                "审批",
            )
        )
        else "runtime_error"
    )


def _claude_mcp_config(
    *,
    mcp_command: Path,
    bridge_url: str,
    product: str,
    username: str,
    signature: str,
    conversation: str,
    roles: list[str],
    capabilities: list[str],
    enrollment_file: Path,
    connector_id: str | None = None,
) -> dict[str, Any]:
    connector_environment = (
        {"AGENT_BRIDGE_CONNECTOR_ID": connector_id} if connector_id else {}
    )
    return {
        "mcpServers": {
            "agent-bridge": {
                "type": "stdio",
                "command": str(mcp_command),
                "env": {
                    "AGENT_BRIDGE_URL": bridge_url,
                    "AGENT_BRIDGE_CLIENT_TYPE": product,
                    "AGENT_BRIDGE_ENROLLMENT_TOKEN_FILE": str(enrollment_file),
                    "AGENT_BRIDGE_AUTO_REGISTER": "1",
                    "AGENT_BRIDGE_USERNAME": username,
                    "AGENT_BRIDGE_SIGNATURE": signature,
                    "AGENT_BRIDGE_CONVERSATION_ID": conversation,
                    "AGENT_BRIDGE_ROLES": ",".join(roles),
                    "AGENT_BRIDGE_CAPABILITIES": ",".join(capabilities),
                    "AGENT_BRIDGE_COMPONENT": "task",
                    **connector_environment,
                },
            }
        }
    }


def _run_claude_task(
    *,
    prompt: str,
    cwd: Path,
    state_file: Path,
    binary: str,
    mcp_config: dict[str, Any],
    environment: dict[str, str],
    poll_inputs: Callable[[], list[dict[str, Any]]] | None = None,
    on_runtime_event: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[str, str, list[str]]:
    resolved = shutil.which(binary)
    if resolved is None:
        raise TaskWorkerError("Claude Code CLI was not found")
    if state_file.exists():
        session_id = state_file.read_text(encoding="utf-8").strip().casefold()
        if not THREAD_ID_PATTERN.fullmatch(session_id):
            raise TaskWorkerError("Claude task session state is invalid")
        session_arguments = ["--resume", session_id]
    else:
        session_id = str(uuid.uuid4())
        session_arguments = ["--session-id", session_id]
    allowed_tools = [f"mcp__agent-bridge__{tool}" for tool in TASK_MCP_TOOLS]
    process = subprocess.Popen(
        [
            resolved,
            "--print",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--replay-user-messages",
            "--verbose",
            *session_arguments,
            "--mcp-config",
            json.dumps(mcp_config, ensure_ascii=False, separators=(",", ":")),
            "--allowedTools",
            *allowed_tools,
            "--append-system-prompt",
            _task_developer_instructions(),
        ],
        cwd=cwd,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        shell=False,
    )
    if process.stdin is None or process.stdout is None or process.stderr is None:
        process.terminate()
        raise TaskWorkerError("Claude task streams were not created")

    output_lines: queue.Queue[str | None] = queue.Queue()
    stderr_lines: list[str] = []

    def read_stdout() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            output_lines.put(line)
        output_lines.put(None)

    def read_stderr() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            stderr_lines.append(line.rstrip())
            if len(stderr_lines) > 200:
                del stderr_lines[:100]

    stdout_thread = threading.Thread(target=read_stdout, daemon=True)
    stderr_thread = threading.Thread(target=read_stderr, daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    def send_user_message(text: str) -> None:
        envelope = {
            "type": "user",
            "message": {"role": "user", "content": text},
            "parent_tool_use_id": None,
            "session_id": session_id,
        }
        process.stdin.write(
            json.dumps(envelope, ensure_ascii=False, separators=(",", ":")) + "\n"
        )
        process.stdin.flush()

    runtime_terminal_emitted = False

    def emit_runtime_event(event_kind: str, **values: Any) -> None:
        nonlocal runtime_terminal_emitted
        if event_kind in {"approval_required", "turn_completed", "runtime_error"}:
            runtime_terminal_emitted = True
        if on_runtime_event is None:
            return
        payload = {"event_kind": event_kind, **values}
        try:
            on_runtime_event(payload)
        except Exception:
            # Runtime projection is observational. A rolling Viewer update or
            # temporary network failure must never abort the actual local turn.
            pass

    latest_result = ""
    latest_assistant_text = ""
    injected_input_ids: set[str] = set()
    pending_inputs: dict[str, dict[str, Any]] = {}
    next_input_poll = 0.0
    result_seen_at: float | None = None
    stdin_closed = False
    tool_names: dict[str, str] = {}
    deadline = time.monotonic() + 6 * 60 * 60
    send_user_message(prompt)
    emit_runtime_event(
        "turn_started",
        native_session_id=session_id,
        summary=_redact_runtime_text(f"工作目录：{cwd}", maximum=1_000),
    )
    try:
        while time.monotonic() < deadline:
            now = time.monotonic()
            if not stdin_closed and poll_inputs is not None and now >= next_input_poll:
                next_input_poll = now + 0.5
                try:
                    for item in poll_inputs():
                        input_id = str(item.get("input_id") or "")
                        if input_id and input_id not in injected_input_ids:
                            pending_inputs[input_id] = item
                except Exception:
                    pass
            if not stdin_closed and pending_inputs:
                updates = list(pending_inputs.values())
                send_user_message(_task_input_prompt(updates))
                result_seen_at = None
                for item in updates:
                    input_id = str(item["input_id"])
                    injected_input_ids.add(input_id)
                    pending_inputs.pop(input_id, None)
            try:
                line = output_lines.get(timeout=0.1)
            except queue.Empty:
                line = ""
            if line is None:
                break
            if line:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "assistant":
                    message = event.get("message")
                    content = message.get("content") if isinstance(message, dict) else []
                    if isinstance(content, list):
                        texts = [
                            str(block.get("text") or "")
                            for block in content
                            if isinstance(block, dict) and block.get("type") == "text"
                        ]
                        if any(texts):
                            latest_assistant_text = "\n".join(texts).strip()
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            block_type = str(block.get("type") or "")
                            if block_type == "text":
                                visible_text = _redact_runtime_text(
                                    block.get("text"),
                                    maximum=12_000,
                                )
                                if visible_text:
                                    emit_runtime_event(
                                        "assistant_text",
                                        native_session_id=session_id,
                                        summary=visible_text,
                                    )
                            elif block_type == "tool_use":
                                tool_use_id = str(block.get("id") or "").strip()
                                tool_name = _claude_tool_label(block.get("name"))
                                if tool_use_id:
                                    tool_names[tool_use_id] = tool_name
                                emit_runtime_event(
                                    "tool_started",
                                    native_session_id=session_id,
                                    tool_use_id=tool_use_id or None,
                                    tool_name=tool_name,
                                    summary=_claude_tool_input_summary(
                                        block.get("name"),
                                        block.get("input"),
                                    ),
                                )
                if event.get("type") == "user":
                    message = event.get("message")
                    content = message.get("content") if isinstance(message, dict) else []
                    if isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict) or block.get("type") != "tool_result":
                                continue
                            tool_use_id = str(block.get("tool_use_id") or "").strip()
                            is_error = bool(block.get("is_error"))
                            emit_runtime_event(
                                "tool_failed" if is_error else "tool_completed",
                                native_session_id=session_id,
                                tool_use_id=tool_use_id or None,
                                tool_name=tool_names.get(tool_use_id),
                                summary=_claude_tool_result_summary(block),
                            )
                if event.get("type") == "result":
                    result = str(event.get("result") or "").strip()
                    if result:
                        latest_result = result
                    subtype = str(event.get("subtype") or "").casefold()
                    failed = bool(event.get("is_error")) or subtype not in {"", "success"}
                    error_detail = _redact_runtime_text(
                        event.get("error") or (result if failed else ""),
                        maximum=2_000,
                    )
                    emit_runtime_event(
                        (
                            _runtime_failure_event_kind(error_detail)
                            if failed
                            else "turn_completed"
                        ),
                        native_session_id=session_id,
                        summary=error_detail if failed else _claude_result_summary(event),
                    )
                    result_seen_at = time.monotonic()
            if (
                not stdin_closed
                and result_seen_at is not None
                and time.monotonic() - result_seen_at >= 0.75
            ):
                if poll_inputs is not None:
                    try:
                        for item in poll_inputs():
                            input_id = str(item.get("input_id") or "")
                            if input_id and input_id not in injected_input_ids:
                                pending_inputs[input_id] = item
                    except Exception:
                        pass
                if pending_inputs:
                    continue
                process.stdin.close()
                stdin_closed = True
            if process.poll() is not None and output_lines.empty():
                break
        else:
            emit_runtime_event(
                "runtime_error",
                native_session_id=session_id,
                summary="Claude Code 回合超过 6 小时执行上限。",
            )
            raise TaskWorkerError("Claude task exceeded the execution timeout")
    finally:
        if process.poll() is None:
            if not stdin_closed:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)
    if process.returncode != 0:
        detail = next((line for line in reversed(stderr_lines) if line), "unknown error")
        if not runtime_terminal_emitted:
            emit_runtime_event(
                _runtime_failure_event_kind(detail),
                native_session_id=session_id,
                summary=_redact_runtime_text(detail, maximum=2_000),
            )
        raise TaskWorkerError("Claude task failed: " + detail[:500])
    _private_write(state_file, session_id)
    summary = latest_result or latest_assistant_text
    return (
        summary or "任务已完成；执行席位未返回额外摘要。",
        session_id,
        sorted(injected_input_ids),
    )
