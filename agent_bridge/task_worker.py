from __future__ import annotations

import argparse
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from .codex_worker import JsonRpcProcess as JsonRpcProcess
from .executables import resolve_executable_path as resolve_executable_path
from .http_client import BridgeRemoteError
from .resident_completion import resident_http_client
from .task_worker_claude import (
    _claude_mcp_config as _claude_mcp_config,
    _run_claude_task as _run_claude_task,
)
from .task_worker_codex import CodexTaskHost as CodexTaskHost
from .task_worker_common import (
    SENSITIVE_CHILD_ENV as SENSITIVE_CHILD_ENV,
    TASK_MCP_TOOLS as TASK_MCP_TOOLS,
    THREAD_ID_PATTERN as THREAD_ID_PATTERN,
    TaskLeaseKeeper as TaskLeaseKeeper,
    TaskWorkerError as TaskWorkerError,
    _mcp_config_arguments as _mcp_config_arguments,
    _private_write as _private_write,
    _read_thread_id as _read_thread_id,
    _refresh_native_tui_state as _refresh_native_tui_state,
    _report_native_tui_state as _report_native_tui_state,
    _required_env as _required_env,
    _safe_report_native_tui_state as _safe_report_native_tui_state,
    _split_tokens as _split_tokens,
    _task_developer_instructions as _task_developer_instructions,
    _task_input_prompt as _task_input_prompt,
    _task_poll_retry_delay as _task_poll_retry_delay,
    _task_prompt as _task_prompt,
)
from .tui_adapter import (
    NATIVE_TUI_ADAPTERS,
    NativeTuiClient,
    NativeTuiError,
    endpoint_turn_lock,
    load_native_tui_binding,
)


def run_worker(args: argparse.Namespace) -> None:
    bridge_url = _required_env("AGENT_BRIDGE_URL").rstrip("/")
    product = _required_env("AGENT_BRIDGE_PRODUCT")
    username = _required_env("AGENT_BRIDGE_USERNAME")
    signature = _required_env("AGENT_BRIDGE_SIGNATURE")
    conversation = _required_env("AGENT_BRIDGE_CONVERSATION_ID")
    adapter = _required_env("AGENT_BRIDGE_TASK_ADAPTER").casefold()
    roles = _split_tokens("AGENT_BRIDGE_ROLES")
    capabilities = _split_tokens("AGENT_BRIDGE_CAPABILITIES")
    cwd = Path(_required_env("AGENT_BRIDGE_TASK_CWD")).expanduser().resolve()
    state_file = Path(_required_env("AGENT_BRIDGE_TASK_THREAD_STATE_FILE")).expanduser().resolve()
    enrollment_file = Path(
        _required_env("AGENT_BRIDGE_ENROLLMENT_TOKEN_FILE")
    ).expanduser().resolve()
    mcp_command = Path(_required_env("AGENT_BRIDGE_MCP_COMMAND")).expanduser().resolve()
    connector_id = os.environ.get("AGENT_BRIDGE_CONNECTOR_ID", "").strip() or None
    if not cwd.is_dir() or not enrollment_file.is_file() or not mcp_command.is_file():
        raise TaskWorkerError("task worker workspace or private connector files are missing")
    environment = dict(os.environ)
    for name in SENSITIVE_CHILD_ENV:
        environment.pop(name, None)
    client = resident_http_client(
        bridge_url=bridge_url,
        product=product,
        username=username,
        signature=signature,
        conversation_id=conversation,
        roles=roles,
        capabilities=capabilities,
        connector_component="task",
    )
    native_binding = None
    native_client = None
    native_lock_file = None
    if adapter in NATIVE_TUI_ADAPTERS:
        if not connector_id:
            raise TaskWorkerError(
                "native TUI task worker requires a connector identity"
            )
        try:
            native_binding = load_native_tui_binding(
                Path(_required_env("AGENT_BRIDGE_TUI_BINDING_FILE"))
            )
        except NativeTuiError as exc:
            raise TaskWorkerError(str(exc)) from exc
        if native_binding.adapter_kind != adapter:
            raise TaskWorkerError(
                "native TUI adapter binding does not match task worker"
            )
        native_client = NativeTuiClient(native_binding)
        native_lock_file = Path(_required_env("AGENT_BRIDGE_TUI_LOCK_FILE"))
        _refresh_native_tui_state(
            client,
            connector_id=connector_id,
            binding=native_binding,
            native_client=native_client,
            lock_file=native_lock_file,
        )
    codex_host: CodexTaskHost | None = None
    try:
        if adapter == "codex":
            codex_host = CodexTaskHost(
                state_file=state_file,
                source_thread_id=os.environ.get(
                    "AGENT_BRIDGE_TASK_SOURCE_THREAD_ID", ""
                ),
            )
            codex_host.start(
                binary=os.environ.get("AGENT_BRIDGE_CODEX_BINARY", "codex"),
                cwd=cwd,
                mcp_arguments=_mcp_config_arguments(
                    mcp_command=mcp_command,
                    bridge_url=bridge_url,
                    product=product,
                    username=username,
                    signature=signature,
                    conversation=conversation,
                    roles=roles,
                    capabilities=capabilities,
                    enrollment_file=enrollment_file,
                    connector_id=connector_id,
                ),
                environment=environment,
            )
        poll_failure_count = 0
        while True:
            try:
                page = client.post(
                    "/agent/tasks/next",
                    {"wait_seconds": 20},
                    timeout=30,
                )
            except BridgeRemoteError as exc:
                delay = _task_poll_retry_delay(exc, poll_failure_count)
                if delay is None or args.once:
                    raise
                poll_failure_count += 1
                time.sleep(delay)
                continue
            poll_failure_count = 0
            task = page.get("task")
            if not isinstance(task, dict):
                if (
                    native_client is not None
                    and native_binding is not None
                    and connector_id is not None
                ):
                    _refresh_native_tui_state(
                        client,
                        connector_id=connector_id,
                        binding=native_binding,
                        native_client=native_client,
                        lock_file=native_lock_file,
                    )
                if args.once:
                    return
                continue
            task_id = str(task["task_id"])
            context_messages: list[dict[str, Any]] = []
            source_sequence = task.get("source_sequence")
            if source_sequence is not None:
                try:
                    context_page = client.post(
                        "/agent/history",
                        {
                            "conversation_id": conversation,
                            "around_sequence": int(source_sequence),
                            "limit": 50,
                        },
                    )
                    raw_context = context_page.get("messages") or []
                    start_sequence = int(
                        task.get("context_start_sequence") or 0
                    )
                    end_sequence = int(
                        task.get("context_end_sequence") or source_sequence
                    )
                    context_messages = [
                        item
                        for item in raw_context
                        if isinstance(item, dict)
                        and start_sequence
                        <= int(item.get("sequence") or 0)
                        <= end_sequence
                    ]
                except (BridgeRemoteError, TypeError, ValueError):
                    # The exact task body and durable sequence locator still
                    # reach the executor; it can retry agent_history itself.
                    context_messages = []
            prompt = _task_prompt(
                task,
                conversation=conversation,
                cwd=cwd,
                context_messages=context_messages,
            )

            def poll_task_inputs() -> list[dict[str, Any]]:
                page = client.post(
                    "/agent/tasks/inputs",
                    {
                        "task_id": task_id,
                        "action": "poll",
                        "limit": 50,
                    },
                )
                values = page.get("inputs")
                if not isinstance(values, list):
                    return []
                return [item for item in values if isinstance(item, dict)]

            lease_keeper: TaskLeaseKeeper | None = None
            try:
                progress_payload = {
                    "task_id": task_id,
                    "status": "running",
                    "execution_cwd": str(cwd),
                    "execution_thread_id": (
                        codex_host.thread_id
                        if codex_host is not None
                        else (
                            native_binding.native_session_id
                            if native_binding is not None
                            else ""
                        )
                    ),
                }
                client.post("/agent/tasks/update", progress_payload)

                def renew_task_lease() -> None:
                    client.post("/agent/tasks/update", progress_payload)
                    if native_binding is not None and connector_id is not None:
                        _safe_report_native_tui_state(
                            client,
                            connector_id=connector_id,
                            binding=native_binding,
                            state="busy",
                            active_task_id=task_id,
                            detail={"reason": "structured_task"},
                        )

                lease_keeper = TaskLeaseKeeper(renew_task_lease)
                lease_keeper.start()
                if adapter == "codex":
                    if codex_host is None:
                        raise TaskWorkerError("Codex task host is missing")
                    summary, applied_input_ids = codex_host.run(
                        prompt,
                        poll_inputs=poll_task_inputs,
                    )
                    thread_id = codex_host.thread_id or ""
                elif adapter == "claude-code":
                    summary, thread_id, applied_input_ids = _run_claude_task(
                        prompt=prompt,
                        cwd=cwd,
                        state_file=state_file,
                        binary=os.environ.get("AGENT_BRIDGE_CLAUDE_BINARY", "claude"),
                        mcp_config=_claude_mcp_config(
                            mcp_command=mcp_command,
                            bridge_url=bridge_url,
                            product=product,
                            username=username,
                            signature=signature,
                            conversation=conversation,
                            roles=roles,
                            capabilities=capabilities,
                            enrollment_file=enrollment_file,
                            connector_id=connector_id,
                        ),
                        environment=environment,
                        poll_inputs=poll_task_inputs,
                    )
                elif adapter in NATIVE_TUI_ADAPTERS:
                    if (
                        native_client is None
                        or native_binding is None
                        or native_lock_file is None
                        or connector_id is None
                    ):
                        raise TaskWorkerError("native TUI task host is missing")
                    with endpoint_turn_lock(native_lock_file) as acquired:
                        if not acquired:
                            raise TaskWorkerError("native TUI endpoint lock failed")
                        _safe_report_native_tui_state(
                            client,
                            connector_id=connector_id,
                            binding=native_binding,
                            state="busy",
                            active_task_id=task_id,
                            detail={"reason": "structured_task"},
                        )
                        try:
                            summary, applied_input_ids = native_client.run_turn(
                                prompt,
                                poll_inputs=poll_task_inputs,
                            )
                        except Exception as exc:
                            error_text = str(exc)
                            waiting = any(
                                marker in error_text.casefold()
                                for marker in (
                                    "approval",
                                    "permission",
                                    "full-access",
                                    "权限",
                                    "审批",
                                )
                            )
                            _safe_report_native_tui_state(
                                client,
                                connector_id=connector_id,
                                binding=native_binding,
                                state="waiting_approval" if waiting else "error",
                                active_task_id=task_id,
                                detail={"error": error_text[:500]},
                            )
                            raise
                        else:
                            _safe_report_native_tui_state(
                                client,
                                connector_id=connector_id,
                                binding=native_binding,
                                state="online",
                            )
                    thread_id = native_binding.native_session_id
                else:
                    raise TaskWorkerError("unsupported task adapter")
                if applied_input_ids:
                    client.post(
                        "/agent/tasks/inputs",
                        {
                            "task_id": task_id,
                            "action": "ack",
                            "input_ids": applied_input_ids,
                        },
                    )
                terminal = client.post(
                    "/agent/tasks/update",
                    {
                        "task_id": task_id,
                        "status": "completed",
                        "result_summary": summary[:20_000],
                        "execution_cwd": str(cwd),
                        "execution_thread_id": thread_id,
                    },
                )
                if str((terminal.get("task") or {}).get("status")) == "completed":
                    source_message = str(task.get("source_message_id") or "")
                    try:
                        client.post(
                            "/agent/send",
                            {
                                "conversation_id": conversation,
                                "body": summary[:10_000],
                                "audience_kind": "room",
                                "audience_value": "*",
                                "reply_to": source_message or None,
                                "refs": [],
                                "mentions": [],
                            },
                        )
                    except Exception:
                        # The durable task card already contains the result. A
                        # transient chat cooldown or outage must not turn an
                        # actually completed task into a false failure.
                        pass
            except Exception as exc:
                try:
                    error_text = str(exc)
                    terminal_status = (
                        "needs_input"
                        if any(
                            marker in error_text.casefold()
                            for marker in (
                                "approval",
                                "permission",
                                "sandbox",
                                "not permitted",
                                "权限",
                                "审批",
                            )
                        )
                        else "failed"
                    )
                    client.post(
                        "/agent/tasks/update",
                        {
                            "task_id": task_id,
                            "status": terminal_status,
                            "result_summary": error_text[:2_000],
                            "execution_cwd": str(cwd),
                            "execution_thread_id": (
                                codex_host.thread_id
                                if codex_host is not None
                                else (
                                    native_binding.native_session_id
                                    if native_binding is not None
                                    else ""
                                )
                            ),
                        },
                    )
                except Exception:
                    pass
            finally:
                if lease_keeper is not None:
                    lease_keeper.close()
            if args.once:
                return
    finally:
        if codex_host is not None:
            codex_host.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Agent Bridge task executor")
    parser.add_argument("--once", action="store_true")
    return parser


def main() -> None:
    try:
        run_worker(build_parser().parse_args())
    except (TaskWorkerError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
