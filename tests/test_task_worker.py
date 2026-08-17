from __future__ import annotations

import os
from argparse import Namespace
from pathlib import Path
from typing import Any

import agent_bridge.task_worker as task_worker
from agent_bridge.http_client import BridgeRemoteError
from agent_bridge.task_worker import (
    TASK_MCP_TOOLS,
    CodexTaskHost,
    _mcp_config_arguments,
    _run_claude_task,
    _task_developer_instructions,
    _task_input_prompt,
    _task_poll_retry_delay,
    _task_prompt,
)
from agent_bridge.tui_adapter import validate_native_tui_binding


THREAD = "019f0000-0000-7000-8000-000000000001"
FORKED = "019f0000-0000-7000-8000-000000000002"


def test_codex_task_host_forks_inviting_tui_without_overriding_permissions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    requests: list[tuple[str, dict]] = []

    class FakeRpc:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def start(self) -> None:
            pass

        def request(self, method: str, params: dict, **_kwargs):
            requests.append((method, params))
            if method == "thread/fork":
                return {"thread": {"id": FORKED}}
            return {}

        def close(self) -> None:
            pass

    monkeypatch.setattr(task_worker, "JsonRpcProcess", FakeRpc)
    monkeypatch.setattr(task_worker.shutil, "which", lambda _name: "/usr/bin/true")
    state_file = tmp_path / "task-thread"
    host = CodexTaskHost(state_file=state_file, source_thread_id=THREAD)
    host.start(
        binary="codex",
        cwd=tmp_path,
        mcp_arguments=[],
        environment={},
    )

    fork = next(params for method, params in requests if method == "thread/fork")
    assert fork["threadId"] == THREAD
    assert fork["cwd"] == str(tmp_path)
    assert "sandbox" not in fork
    assert "approvalPolicy" not in fork
    assert state_file.read_text(encoding="utf-8").strip() == FORKED


def test_task_prompt_treats_cwd_as_starting_point_and_local_permissions_as_limit(
    tmp_path: Path,
) -> None:
    prompt = _task_prompt(
        {
            "task_id": "task_123",
            "body": "审计另一个目录，需要时分工。",
            "target_participant_ids": ["participant_a"],
        },
        conversation="任务群",
        cwd=tmp_path,
    )

    assert "不是普通聊天" in prompt
    assert "当前目录只是起点" in prompt
    assert "本机权限" in prompt
    assert "agent_task_delegate" in prompt
    assert "完成和失败终态" in prompt
    assert str(tmp_path) in prompt


def test_task_seat_can_create_real_nickname_approval_request(tmp_path: Path) -> None:
    arguments = _mcp_config_arguments(
        mcp_command=tmp_path / "agent-bridge-mcp",
        bridge_url="http://127.0.0.1:8765",
        product="codex-memory",
        username="memory-steward",
        signature="maintains durable memory",
        conversation="tools-room",
        roles=[],
        capabilities=[],
        enrollment_file=tmp_path / "enrollment.token",
        connector_id="connector_memory",
    )
    command_text = " ".join(arguments)

    assert "agent_request_nickname" in TASK_MCP_TOOLS
    assert "agent_request_nickname" in command_text
    instructions = _task_developer_instructions()
    assert "正式审批记录" in instructions
    assert "不得用普通群消息冒充正式申请" in instructions


def test_task_poll_retries_rolling_upgrade_without_masking_permanent_errors() -> None:
    assert _task_poll_retry_delay(
        BridgeRemoteError("old viewer", status_code=404), 0
    ) == 1.0
    assert _task_poll_retry_delay(
        BridgeRemoteError("offline", status_code=None), 8
    ) == 16.0
    assert _task_poll_retry_delay(
        BridgeRemoteError(
            "busy",
            status_code=429,
            retry_after_seconds=120,
        ),
        2,
    ) == 30.0
    assert (
        _task_poll_retry_delay(
            BridgeRemoteError("forbidden", status_code=403),
            0,
        )
        is None
    )


def test_codex_active_task_steers_exact_authorized_followup() -> None:
    requests: list[tuple[str, dict]] = []
    notifications = [
        {
            "method": "item/completed",
            "params": {
                "turnId": THREAD,
                "item": {"type": "agentMessage", "text": "已按四分钟调整。"},
            },
        },
        {
            "method": "turn/completed",
            "params": {"turn": {"id": THREAD, "status": "completed"}},
        },
    ]

    class FakeRpc:
        def request(self, method: str, params: dict, **_kwargs):
            requests.append((method, params))
            if method == "turn/start":
                return {"turn": {"id": THREAD}}
            if method == "turn/steer":
                return {"turnId": THREAD}
            return {}

        def poll_notification(self):
            return notifications.pop(0) if notifications else None

    host = CodexTaskHost(
        state_file=Path("/tmp/unused-agent-bridge-test-thread"),
        source_thread_id=None,
    )
    host.rpc = FakeRpc()
    host.thread_id = THREAD
    pages = [
        [
            {
                "input_id": "taskinput_live",
                "body": "每次 sleep 不超过 4 分钟。",
                "issuer_role": "admin",
            }
        ],
        [],
    ]

    summary, applied = host.run(
        "开始原任务。",
        poll_inputs=lambda: pages.pop(0) if pages else [],
    )

    assert summary == "已按四分钟调整。"
    assert applied == ["taskinput_live"]
    steer = next(params for method, params in requests if method == "turn/steer")
    assert "每次 sleep 不超过 4 分钟" in steer["input"][0]["text"]


def test_task_input_prompt_marks_live_message_as_body_input_not_shadow_relay() -> None:
    prompt = _task_input_prompt(
        [{"input_id": "taskinput_1", "body": "停止长时间 sleep。"}]
    )
    assert "本体执行席" in prompt
    assert "不是值守影子的转述" in prompt
    assert "不要只回复‘收到’" in prompt


def test_claude_streaming_task_accepts_live_input_in_same_session(
    tmp_path: Path,
) -> None:
    fake_claude = tmp_path / "fake-claude"
    fake_claude.write_text(
        """#!/usr/bin/env python3
import json
import sys

for line in sys.stdin:
    if not line.strip():
        continue
    event = json.loads(line)
    content = str(event["message"]["content"])
    print(json.dumps({
        "type": "assistant",
        "message": {
            "content": [{"type": "text", "text": "applied:" + content}]
        },
    }), flush=True)
    print(json.dumps({
        "type": "result",
        "subtype": "success",
        "result": "applied:" + content,
    }), flush=True)
""",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)
    poll_count = 0

    def poll_inputs() -> list[dict]:
        nonlocal poll_count
        poll_count += 1
        if poll_count == 2:
            return [
                {
                    "input_id": "taskinput_claude_live",
                    "body": "每次 sleep 不超过 4 分钟。",
                }
            ]
        return []

    summary, session_id, applied = _run_claude_task(
        prompt="开始原任务。",
        cwd=tmp_path,
        state_file=tmp_path / "claude-task-session",
        binary=str(fake_claude),
        mcp_config={},
        environment=dict(os.environ),
        poll_inputs=poll_inputs,
    )

    assert "每次 sleep 不超过 4 分钟" in summary
    assert len(session_id) == 36
    assert applied == ["taskinput_claude_live"]
    assert (tmp_path / "claude-task-session").read_text(
        encoding="utf-8"
    ).strip() == session_id


def test_native_tui_task_worker_executes_in_bound_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    enrollment = tmp_path / "enrollment.token"
    enrollment.write_text("enroll_test\n", encoding="utf-8")
    mcp = tmp_path / "agent-bridge-mcp"
    mcp.write_text("#!/bin/sh\n", encoding="utf-8")
    binding_file = tmp_path / "tui-binding.json"
    binding_value = validate_native_tui_binding(
        adapter_kind="opencode",
        endpoint_id="tui-opencode-task",
        native_session_id="native-session-task",
        transport={
            "kind": "opencode-http",
            "base_url": "http://127.0.0.1:9201",
        },
    )
    binding_file.write_text(
        __import__("json").dumps(binding_value.payload()),
        encoding="utf-8",
    )
    sent: list[tuple[str, dict]] = []

    class FakeClient:
        def post(self, path: str, payload: dict, **_kwargs):
            sent.append((path, payload))
            if path == "/agent/tasks/next":
                return {
                    "task": {
                        "task_id": "task_native",
                        "body": "检查当前工程并报告。",
                        "source_message_id": "message_native",
                        "target_participant_ids": ["participant_native"],
                    }
                }
            if path == "/agent/tasks/inputs":
                return {"inputs": []}
            if path == "/agent/tasks/update":
                return {"task": {"status": payload["status"]}}
            return {}

    prompts: list[str] = []

    class FakeNativeClient:
        def __init__(self, configured) -> None:
            assert configured.native_session_id == "native-session-task"

        def probe(self, **_kwargs):
            return {"online": True, "transport": "opencode-http"}

        def run_turn(self, prompt: str, **_kwargs):
            prompts.append(prompt)
            return "真实 TUI 已完成。", []

    class FakeLeaseKeeper:
        def __init__(self, _renew) -> None:
            pass

        def start(self) -> None:
            pass

        def close(self) -> None:
            pass

    resident_identity: dict[str, Any] = {}

    def make_client(**identity: Any) -> FakeClient:
        resident_identity.update(identity)
        return FakeClient()

    monkeypatch.setattr(task_worker, "resident_http_client", make_client)
    monkeypatch.setattr(task_worker, "NativeTuiClient", FakeNativeClient)
    monkeypatch.setattr(task_worker, "TaskLeaseKeeper", FakeLeaseKeeper)
    environment = {
        "AGENT_BRIDGE_URL": "http://127.0.0.1:8765",
        "AGENT_BRIDGE_PRODUCT": "opencode",
        "AGENT_BRIDGE_USERNAME": "native-owner",
        "AGENT_BRIDGE_SIGNATURE": "真实本体。",
        "AGENT_BRIDGE_CONVERSATION_ID": "native-room",
        "AGENT_BRIDGE_TASK_ADAPTER": "opencode",
        "AGENT_BRIDGE_TASK_CWD": str(tmp_path),
        "AGENT_BRIDGE_TASK_THREAD_STATE_FILE": str(tmp_path / "thread"),
        "AGENT_BRIDGE_ENROLLMENT_TOKEN_FILE": str(enrollment),
        "AGENT_BRIDGE_MCP_COMMAND": str(mcp),
        "AGENT_BRIDGE_CONNECTOR_ID": "connector_native_task",
        "AGENT_BRIDGE_TUI_BINDING_FILE": str(binding_file),
        "AGENT_BRIDGE_TUI_LOCK_FILE": str(tmp_path / "endpoint.lock"),
    }
    for key, value in environment.items():
        monkeypatch.setenv(key, value)

    task_worker.run_worker(Namespace(once=True))

    assert resident_identity["connector_component"] == "task"
    assert "检查当前工程并报告" in prompts[0]
    completed = [
        payload
        for path, payload in sent
        if path == "/agent/tasks/update" and payload["status"] == "completed"
    ]
    assert completed[0]["execution_thread_id"] == "native-session-task"
    assert any(
        path == "/agent/send" and payload["body"] == "真实 TUI 已完成。"
        for path, payload in sent
    )
    assert all(
        "access_mode" not in payload
        for path, payload in sent
        if path == "/agent/connector/tui-state"
    )
