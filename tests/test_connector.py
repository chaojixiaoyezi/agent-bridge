from __future__ import annotations

import json
import plistlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent_bridge.claude_adapter as claude_adapter
import agent_bridge.server as bridge_server
from agent_bridge.connector import configure_resident_connector
from agent_bridge.connector import ConnectorSetupError


BRIDGE_ROOT = Path(__file__).resolve().parents[1]


def test_codex_worker_launcher_imports_package_outside_bridge_checkout(
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        [str(BRIDGE_ROOT / "bin" / "agent-bridge-codex-worker"), "--help"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "agent-bridge-codex-worker" in completed.stdout


def test_codex_connector_writes_private_launchd_services_without_secret_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "agent_bridge.connector.shutil.which",
        lambda name: "/Applications/Codex.app/Contents/Resources/codex"
        if name == "codex"
        else None,
    )
    result = configure_resident_connector(
        connector_id="connector_1234567890abcdef",
        enrollment_token="enroll_private-test-token",
        bridge_url="http://127.0.0.1:8765",
        product="codex",
        username="值守者",
        signature="只处理明确通知。",
        conversation_id="测试群",
        adapter_kind="codex",
        requested_mode="resident",
        roles=["reviewer"],
        workspace_path=str(tmp_path),
        home=tmp_path,
        system_name="Darwin",
        activate=False,
    )

    assert result.status == "configured"
    state_directory = Path(result.state_directory)
    enrollment_file = state_directory / "enrollment.token"
    manifest_file = state_directory / "connector.json"
    assert enrollment_file.read_text(encoding="utf-8").strip() == (
        "enroll_private-test-token"
    )
    assert enrollment_file.stat().st_mode & 0o777 == 0o600
    assert "enroll_private-test-token" not in manifest_file.read_text(encoding="utf-8")

    launch_agents = tmp_path / "Library" / "LaunchAgents"
    listener_path = launch_agents / f"{result.listener_service}.plist"
    worker_path = launch_agents / f"{result.worker_service}.plist"
    listener = plistlib.loads(listener_path.read_bytes())
    worker = plistlib.loads(worker_path.read_bytes())
    serialized = listener_path.read_text(encoding="utf-8") + worker_path.read_text(
        encoding="utf-8"
    )
    assert "enroll_private-test-token" not in serialized
    assert listener["EnvironmentVariables"][
        "AGENT_BRIDGE_ENROLLMENT_TOKEN_FILE"
    ] == str(enrollment_file)
    assert listener["EnvironmentVariables"]["AGENT_BRIDGE_AUTO_REGISTER"] == "1"
    assert listener["EnvironmentVariables"]["AGENT_BRIDGE_WAKE_POLICY"] == "all"
    assert worker["EnvironmentVariables"]["AGENT_BRIDGE_AUTO_REGISTER"] == "1"
    assert worker["EnvironmentVariables"]["AGENT_BRIDGE_AGENT_WAKE_POLICY"] == (
        "mention"
    )
    assert worker["EnvironmentVariables"]["AGENT_BRIDGE_CODEX_BINARY"] == (
        "/Applications/Codex.app/Contents/Resources/codex"
    )
    assert "/opt/homebrew/bin" in worker["EnvironmentVariables"]["PATH"].split(":")
    assert worker["ProgramArguments"][0].endswith("agent-bridge-codex-worker")


def test_custom_product_acceptance_stays_manual_without_installing_services(
    tmp_path: Path,
) -> None:
    result = configure_resident_connector(
        connector_id="connector_custom123456",
        enrollment_token="enroll_custom-private-token",
        bridge_url="https://bridge.example.test",
        product="custom-agent",
        username="custom",
        signature="自定义产品。",
        conversation_id="自定义群",
        adapter_kind="manual",
        requested_mode="resident",
        workspace_path=str(tmp_path),
        home=tmp_path,
        system_name="Darwin",
        activate=False,
    )

    assert result.status == "manual"
    assert result.listener_service is None
    assert not (tmp_path / "Library" / "LaunchAgents").exists()


def test_linux_connector_uses_private_systemd_units_and_escapes_specifiers(
    tmp_path: Path,
) -> None:
    result = configure_resident_connector(
        connector_id="connector_linux123456",
        enrollment_token="enroll_linux-private-token",
        bridge_url="https://bridge.example.test",
        product="claude-code",
        username="linux值守者",
        signature="100% ready",
        conversation_id="Linux群",
        adapter_kind="claude-code",
        requested_mode="resident",
        workspace_path=str(tmp_path),
        home=tmp_path,
        system_name="Linux",
        activate=False,
    )

    assert result.status == "configured"
    unit_directory = tmp_path / ".config" / "systemd" / "user"
    units = list(unit_directory.glob("*.service"))
    assert len(units) == 2
    serialized = "\n".join(path.read_text(encoding="utf-8") for path in units)
    assert "enroll_linux-private-token" not in serialized
    assert "100%% ready" in serialized
    assert "AGENT_BRIDGE_ENROLLMENT_TOKEN_FILE=" in serialized
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in units)


def test_invalid_workspace_is_rejected_before_invitation_is_consumed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bridge_server,
        "CONFIG",
        SimpleNamespace(
            server_url="http://127.0.0.1:8765",
            client_type="codex",
        ),
    )

    def must_not_connect():
        raise AssertionError("invitation was consumed before local preflight")

    monkeypatch.setattr(bridge_server, "get_client", must_not_connect)
    with pytest.raises(ConnectorSetupError, match="workspace does not exist"):
        bridge_server.agent_accept_invitation(
            username="值守者",
            signature="只处理明确通知。",
            workspace_path=str(tmp_path / "missing"),
        )


def test_claude_adapter_uses_only_bridge_tools_and_requires_reply_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp_command = tmp_path / "agent-bridge-mcp"
    enrollment_file = tmp_path / "enrollment.token"
    mcp_command.write_text("#!/bin/sh\n", encoding="utf-8")
    enrollment_file.write_text("enroll_private\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_BRIDGE_URL", "https://bridge.example.test")
    monkeypatch.setenv("AGENT_BRIDGE_PRODUCT", "claude-code")
    monkeypatch.setenv("AGENT_BRIDGE_USERNAME", "值守者")
    monkeypatch.setenv("AGENT_BRIDGE_SIGNATURE", "只处理通知。")
    monkeypatch.setenv("AGENT_BRIDGE_CONVERSATION_ID", "测试群")
    monkeypatch.setenv("AGENT_BRIDGE_MCP_COMMAND", str(mcp_command))
    monkeypatch.setenv("AGENT_BRIDGE_ENROLLMENT_TOKEN_FILE", str(enrollment_file))
    monkeypatch.setenv("AGENT_BRIDGE_CLAUDE_CWD", str(tmp_path))
    monkeypatch.setenv("AGENT_BRIDGE_ENROLLMENT_TOKEN", "must-not-leak")
    monkeypatch.setattr(claude_adapter.shutil, "which", lambda _name: "/opt/bin/claude")
    captured: dict = {}

    def successful_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        events = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "wait-1",
                            "name": "mcp__agent-bridge__agent_wait",
                            "input": {"wait_seconds": 0},
                        },
                        {
                            "type": "tool_use",
                            "id": "reply-1",
                            "name": "mcp__agent-bridge__agent_reply",
                            "input": {"message_id": "message-42"},
                        },
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "wait-1",
                            "content": json.dumps(
                                {
                                    "messages": [
                                        {
                                            "message_id": "message-42",
                                            "priority": "mention",
                                        }
                                    ]
                                }
                            ),
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "reply-1",
                            "content": "{}",
                        },
                    ]
                },
            },
        ]
        return SimpleNamespace(
            returncode=0,
            stdout="\n".join(json.dumps(item) for item in events),
            stderr="",
        )

    monkeypatch.setattr(claude_adapter.subprocess, "run", successful_run)
    batch = {
        "schema_version": 1,
        "source": "agent-bridge-supervisor",
        "event": "wake_batch",
        "event_count": 1,
        "wake_priority": "mention",
        "priority_counts": {"mention": 1},
        "last_event_id": 42,
    }
    claude_adapter.run_claude(batch)

    assert captured["shell"] is False
    assert "AGENT_BRIDGE_ENROLLMENT_TOKEN" not in captured["env"]
    command_text = " ".join(captured["command"])
    assert "must-not-leak" not in command_text
    assert "--strict-mcp-config" in captured["command"]
    assert "--tools" in captured["command"]
    assert "mcp__agent-bridge__agent_wait" in captured["command"]
    assert "mcp__agent-bridge__agent_reply" in captured["command"]
    assert "mcp__agent-bridge__agent_register" not in captured["command"]
    assert "Read" in captured["command"]
    assert "Edit" in captured["command"]
    assert "Bash" in captured["command"]
    assert captured["command"][captured["command"].index("--permission-mode") + 1] == (
        "acceptEdits"
    )
    config_index = captured["command"].index("--mcp-config") + 1
    mcp_environment = json.loads(captured["command"][config_index])["mcpServers"][
        "agent-bridge"
    ]["env"]
    assert mcp_environment == {
        "AGENT_BRIDGE_URL": "https://bridge.example.test",
        "AGENT_BRIDGE_CLIENT_TYPE": "claude-code",
        "AGENT_BRIDGE_ENROLLMENT_TOKEN_FILE": str(enrollment_file),
        "AGENT_BRIDGE_AUTO_REGISTER": "1",
        "AGENT_BRIDGE_USERNAME": "值守者",
        "AGENT_BRIDGE_SIGNATURE": "只处理通知。",
        "AGENT_BRIDGE_CONVERSATION_ID": "测试群",
        "AGENT_BRIDGE_ROLES": "",
        "AGENT_BRIDGE_CAPABILITIES": "",
    }
    prompt = captured["command"][-1]
    assert "连接器会在第一次工具调用时自动登记固定身份" in prompt
    assert "message.authorization" in prompt
    assert "最小必要" in prompt
    assert "agent_register" not in prompt

    def incomplete_run(command, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "type": "tool_use",
                    "id": "wait-without-result",
                    "name": "mcp__agent-bridge__agent_wait",
                    "input": {},
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(claude_adapter.subprocess, "run", incomplete_run)
    with pytest.raises(claude_adapter.ClaudeAdapterError, match="tool evidence"):
        claude_adapter.run_claude(batch)

    def wrong_reply_run(command, **kwargs):
        events = [
            {
                "type": "tool_use",
                "id": "wait-2",
                "name": "mcp__agent-bridge__agent_wait",
                "input": {},
            },
            {
                "type": "tool_use",
                "id": "reply-2",
                "name": "mcp__agent-bridge__agent_reply",
                "input": {"message_id": "another-message"},
            },
            {
                "type": "tool_result",
                "tool_use_id": "wait-2",
                "content": json.dumps(
                    {"messages": [{"message_id": "message-42", "priority": "mention"}]}
                ),
            },
            {
                "type": "tool_result",
                "tool_use_id": "reply-2",
                "content": "{}",
            },
        ]
        return SimpleNamespace(
            returncode=0,
            stdout="\n".join(json.dumps(item) for item in events),
            stderr="",
        )

    monkeypatch.setattr(claude_adapter.subprocess, "run", wrong_reply_run)
    with pytest.raises(claude_adapter.ClaudeAdapterError, match="message-42"):
        claude_adapter.run_claude(batch)


def test_claude_adapter_deterministically_acks_optional_inspected_messages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp_command = tmp_path / "agent-bridge-mcp"
    enrollment_file = tmp_path / "enrollment.token"
    mcp_command.write_text("#!/bin/sh\n", encoding="utf-8")
    enrollment_file.write_text("enroll_private\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_BRIDGE_URL", "https://bridge.example.test")
    monkeypatch.setenv("AGENT_BRIDGE_PRODUCT", "claude-code")
    monkeypatch.setenv("AGENT_BRIDGE_USERNAME", "值守者")
    monkeypatch.setenv("AGENT_BRIDGE_SIGNATURE", "只处理通知。")
    monkeypatch.setenv("AGENT_BRIDGE_CONVERSATION_ID", "测试群")
    monkeypatch.setenv("AGENT_BRIDGE_MCP_COMMAND", str(mcp_command))
    monkeypatch.setenv("AGENT_BRIDGE_ENROLLMENT_TOKEN_FILE", str(enrollment_file))
    monkeypatch.setenv("AGENT_BRIDGE_CLAUDE_CWD", str(tmp_path))
    monkeypatch.setattr(claude_adapter.shutil, "which", lambda _name: "/opt/bin/claude")

    wait_result = {
        "backlog": {"required_reply_count": 0},
        "messages": [
            {
                "message_id": "message-optional",
                "delivery": {
                    "priority": "mention",
                    "reasons": ["room_activity", "wake_all"],
                },
            },
            {
                "message_id": "message-ordinary",
                "delivery": {
                    "priority": "normal",
                    "reasons": ["room_activity"],
                },
            },
        ],
    }

    def completed_run(command, **kwargs):
        events = [
            {
                "type": "tool_use",
                "id": "wait-optional",
                "name": "mcp__agent-bridge__agent_wait",
                "input": {"wait_seconds": 0},
            },
            {
                "type": "tool_result",
                "tool_use_id": "wait-optional",
                "content": json.dumps(wait_result),
            },
        ]
        return SimpleNamespace(
            returncode=0,
            stdout="\n".join(json.dumps(item) for item in events),
            stderr="",
        )

    completion_client = object()
    captured: dict = {}

    def make_client(**identity):
        captured["identity"] = identity
        return completion_client

    def acknowledge(client, message_ids):
        captured["client"] = client
        captured["message_ids"] = set(message_ids)
        return frozenset(message_ids)

    monkeypatch.setattr(claude_adapter.subprocess, "run", completed_run)
    monkeypatch.setattr(claude_adapter, "resident_http_client", make_client)
    monkeypatch.setattr(claude_adapter, "acknowledge_messages", acknowledge)

    claude_adapter.run_claude(
        {
            "schema_version": 1,
            "source": "agent-bridge-supervisor",
            "event": "wake_batch",
            "event_count": 1,
            "wake_priority": "mention",
            "required_reply_count": 0,
            "priority_counts": {"mention": 1},
            "last_event_id": 52,
        }
    )

    assert captured["client"] is completion_client
    assert captured["message_ids"] == {
        "message-optional",
        "message-ordinary",
    }
    assert captured["identity"]["username"] == "值守者"

    def failed_ack(client, message_ids):
        raise RuntimeError("bridge unavailable")

    monkeypatch.setattr(claude_adapter, "acknowledge_messages", failed_ack)
    with pytest.raises(claude_adapter.ClaudeAdapterError, match="optional-message ack"):
        claude_adapter.run_claude(
            {
                "schema_version": 1,
                "source": "agent-bridge-supervisor",
                "event": "wake_batch",
                "event_count": 1,
                "wake_priority": "mention",
                "required_reply_count": 0,
                "priority_counts": {"mention": 1},
                "last_event_id": 53,
            }
        )
