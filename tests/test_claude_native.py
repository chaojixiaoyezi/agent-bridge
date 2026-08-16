from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from agent_bridge.claude_channel import ChannelRuntime
from agent_bridge.claude_guide import (
    TmuxClaudeGuide,
    tmux_guide_from_environment,
)
from agent_bridge.claude_launcher import build_tmux_bootstrap_command
from agent_bridge.claude_session_hook import handle_hook
from agent_bridge.connector import configure_resident_connector


BRIDGE_ROOT = Path(__file__).resolve().parents[1]


def test_claude_launcher_builds_one_connector_scoped_tmux_session(
    tmp_path: Path,
) -> None:
    state = tmp_path / "connector state"
    state.mkdir()
    arguments = [
        "--state-directory",
        str(state),
        "--",
        "--resume",
        "5ac0d6f3-a939-47e0-8386-4ac3be33a38c",
    ]
    command = build_tmux_bootstrap_command(
        tmux_binary="/opt/homebrew/bin/tmux",
        state_directory=str(state),
        launcher_arguments=arguments,
        cwd=str(tmp_path),
        python_binary="/private/venv/bin/python",
    )
    assert command[:3] == [
        "/opt/homebrew/bin/tmux",
        "new-session",
        "-A",
    ]
    assert command[3] == "-s"
    assert command[4].startswith("agent-bridge-claude-")
    assert command[5:7] == ["-c", str(tmp_path)]
    assert shlex.split(command[-1]) == [
        "/private/venv/bin/python",
        "-m",
        "agent_bridge.claude_launcher",
        *arguments,
    ]


def test_tmux_guide_discovers_only_a_safe_inherited_pane(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent_bridge.claude_guide.shutil.which",
        lambda _name, path=None: "/opt/homebrew/bin/tmux",
    )
    guide = tmux_guide_from_environment(
        {"TMUX": "/tmp/tmux.sock,1,0", "TMUX_PANE": "%42", "PATH": "/bin"}
    )
    assert guide == TmuxClaudeGuide(
        binary="/opt/homebrew/bin/tmux",
        pane="%42",
    )
    assert (
        tmux_guide_from_environment(
            {"TMUX": "/tmp/tmux.sock,1,0", "TMUX_PANE": "other:1.2"}
        )
        is None
    )


def test_tmux_guide_uses_bracketed_paste_and_one_submit(monkeypatch) -> None:
    calls: list[tuple[list[str], dict]] = []

    def fake_run(command, **kwargs):
        calls.append((list(command), dict(kwargs)))
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr("agent_bridge.claude_guide.subprocess.run", fake_run)
    monkeypatch.setattr(
        "agent_bridge.claude_guide.secrets.token_hex",
        lambda _bytes: "a" * 16,
    )
    guide = TmuxClaudeGuide(binary="/opt/homebrew/bin/tmux", pane="%7")
    guide.deliver("第一行\n第二行")
    buffer_name = f"agent-bridge-{os.getpid()}-{'a' * 16}"
    assert calls[0][0] == [
        "/opt/homebrew/bin/tmux",
        "load-buffer",
        "-b",
        buffer_name,
        "-",
    ]
    assert calls[0][1]["input"] == "第一行\n第二行".encode()
    assert calls[1][0] == [
        "/opt/homebrew/bin/tmux",
        "paste-buffer",
        "-d",
        "-p",
        "-b",
        buffer_name,
        "-t",
        "%7",
        ";",
        "send-keys",
        "-t",
        "%7",
        "Enter",
    ]
    assert calls[2][0] == [
        "/opt/homebrew/bin/tmux",
        "delete-buffer",
        "-b",
        buffer_name,
    ]


def test_claude_launcher_preserves_resume_and_adds_one_exact_channel(
    tmp_path: Path,
) -> None:
    configured = configure_resident_connector(
        connector_id="connector_launcher123456",
        enrollment_token="enroll_launcher-private-token",
        bridge_url="http://127.0.0.1:8765",
        product="claude-code",
        username="launcher-owner",
        signature="Launcher test",
        conversation_id="launcher-room",
        adapter_kind="claude-code",
        requested_mode="resident",
        workspace_path=str(tmp_path),
        home=tmp_path,
        system_name="Darwin",
        activate=False,
    )
    environment = dict(os.environ)
    environment["AGENT_BRIDGE_CLAUDE_BINARY"] = "/bin/echo"
    completed = subprocess.run(
        [
            str(BRIDGE_ROOT / "bin" / "agent-bridge-claude"),
            "--state-directory",
            configured.state_directory,
            "--print-command",
            "--",
            "--resume",
            "5ac0d6f3-a939-47e0-8386-4ac3be33a38c",
        ],
        cwd=str(tmp_path),
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    command = json.loads(completed.stdout)
    assert command[1:3] == [
        "--resume",
        "5ac0d6f3-a939-47e0-8386-4ac3be33a38c",
    ]
    manifest = json.loads(
        (Path(configured.state_directory) / "connector.json").read_text(
            encoding="utf-8"
        )
    )
    assert command[-2:] == [
        "--dangerously-load-development-channels",
        manifest["claude_channel"]["selector"],
    ]
    assert "--plugin-dir" in command
    assert "--mcp-config" in command


def test_claude_launcher_preserves_the_tui_working_directory(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    configured = configure_resident_connector(
        connector_id="connector_launchercwd1234",
        enrollment_token="enroll_launcher-cwd-private-token",
        bridge_url="http://127.0.0.1:8765",
        product="claude-code",
        username="launcher-cwd-owner",
        signature="Launcher cwd test",
        conversation_id="launcher-cwd-room",
        adapter_kind="claude-code",
        requested_mode="resident",
        workspace_path=str(workspace),
        home=tmp_path,
        system_name="Darwin",
        activate=False,
    )
    fake_claude = tmp_path / "fake-claude"
    fake_claude.write_text("#!/bin/sh\npwd\n", encoding="utf-8")
    fake_claude.chmod(0o755)
    environment = dict(os.environ)
    environment["AGENT_BRIDGE_CLAUDE_BINARY"] = str(fake_claude)
    completed = subprocess.run(
        [
            str(BRIDGE_ROOT / "bin" / "agent-bridge-claude"),
            "--state-directory",
            configured.state_directory,
            "--",
            "--resume",
            "5ac0d6f3-a939-47e0-8386-4ac3be33a38c",
        ],
        cwd=str(workspace),
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == str(workspace)


def test_claude_session_hook_binds_exact_id_without_persisting_permission() -> None:
    calls: list[dict] = []
    written: list[dict] = []
    intents: list[dict] = []

    class FakeClient:
        def bind_native_session(self, **payload):
            calls.append(payload)
            return {
                "lease": {
                    "lease_id": "lease_hook_one",
                    "expires_at": 1234.0,
                }
            }

    class FakeState:
        connector_id = "connector_hook_one"
        endpoint_id = "claude-hook-one"
        process_epoch = "epoch-hook-one"

        @staticmethod
        def client():
            return FakeClient()

        @staticmethod
        def write_lease(payload):
            written.append(payload)

        @staticmethod
        def write_binding_intent(payload):
            intents.append(payload)

        @staticmethod
        def bind_intent(intent, *, client=None):
            result = (client or FakeClient()).bind_native_session(
                connector_id=FakeState.connector_id,
                tui_endpoint_id=FakeState.endpoint_id,
                native_session_id=intent["native_session_id"],
                process_epoch=FakeState.process_epoch,
                binding_source=intent["binding_source"],
                replace_existing_session=intent[
                    "replace_existing_session"
                ],
                metadata=intent["metadata"],
            )
            FakeState.write_lease(
                {
                    "native_session_id": intent["native_session_id"],
                    "lease_id": result["lease"]["lease_id"],
                }
            )
            return result

    previous = os.environ.get("AGENT_BRIDGE_CLAUDE_ALLOW_SESSION_REPLACEMENT")
    os.environ["AGENT_BRIDGE_CLAUDE_ALLOW_SESSION_REPLACEMENT"] = "1"
    try:
        result = handle_hook(
            FakeState(),  # type: ignore[arg-type]
            {
                "hook_event_name": "SessionStart",
                "session_id": "5ac0d6f3-a939-47e0-8386-4ac3be33a38c",
                "source": "startup",
                "cwd": "/tmp/project",
                "permission_mode": "bypassPermissions",
            },
        )
        os.environ["AGENT_BRIDGE_CLAUDE_ALLOW_SESSION_REPLACEMENT"] = "0"
        handle_hook(
            FakeState(),  # type: ignore[arg-type]
            {
                "hook_event_name": "SessionStart",
                "session_id": "632d8a23-70ca-48fb-9bef-95a3b1445f10",
                "source": "clear",
                "cwd": "/tmp/project",
            },
        )
        handle_hook(
            FakeState(),  # type: ignore[arg-type]
            {
                "hook_event_name": "SessionStart",
                "session_id": "5ac0d6f3-a939-47e0-8386-4ac3be33a38c",
                "source": "resume",
                "cwd": "/tmp/project",
            },
        )
    finally:
        if previous is None:
            os.environ.pop(
                "AGENT_BRIDGE_CLAUDE_ALLOW_SESSION_REPLACEMENT",
                None,
            )
        else:
            os.environ[
                "AGENT_BRIDGE_CLAUDE_ALLOW_SESSION_REPLACEMENT"
            ] = previous
    assert result["lease"]["lease_id"] == "lease_hook_one"
    assert calls[0]["native_session_id"] == (
        "5ac0d6f3-a939-47e0-8386-4ac3be33a38c"
    )
    assert calls[0]["replace_existing_session"] is True
    assert calls[1]["replace_existing_session"] is True
    assert calls[2]["replace_existing_session"] is False
    assert calls[0]["metadata"] == {
        "runtime": "claude-code",
        "source": "startup",
        "cwd": "/tmp/project",
    }
    assert "permission" not in json.dumps(calls[0])
    assert "permission" not in json.dumps(intents[0])
    assert written[0]["native_session_id"] == calls[0]["native_session_id"]


def test_claude_channel_recovers_a_pending_exact_binding_intent(
    tmp_path: Path,
) -> None:
    recovered: list[dict] = []
    local: dict[str, dict | None] = {"lease": None}
    client = object()

    class FakeState:
        state_directory = tmp_path
        connector_id = "connector_channel_recovery"
        process_epoch = "epoch-channel-recovery"

        @staticmethod
        def client():
            return client

        @staticmethod
        def read_lease():
            return local["lease"]

        @staticmethod
        def read_binding_intent():
            return {
                "connector_id": "connector_channel_recovery",
                "process_epoch": "epoch-channel-recovery",
                "native_session_id": "session-channel-recovery",
                "ended": False,
            }

        @staticmethod
        def bind_intent(intent, *, client):
            recovered.append({"intent": intent, "client": client})
            local["lease"] = {
                "connector_id": "connector_channel_recovery",
                "process_epoch": "epoch-channel-recovery",
                "lease_id": "lease-channel-recovery",
                "ended": False,
            }
            return {"lease": local["lease"]}

    runtime = ChannelRuntime(FakeState())  # type: ignore[arg-type]
    lease = runtime._active_lease()
    assert lease is not None
    assert lease["lease_id"] == "lease-channel-recovery"
    assert recovered[0]["client"] is client


def test_claude_channel_tools_keep_route_token_out_of_model_input(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, dict]] = []

    class FakeClient:
        def receive_native_channel_event(self, **payload):
            calls.append(("apply", payload))
            return {"event": {"state": "applied"}}

        def send_native_channel_event(self, **payload):
            calls.append(("send", payload))
            return {"message": {"message_id": "message_out"}}

        def heartbeat_native_session(self, **payload):
            calls.append(("heartbeat", payload))
            return {"lease_id": payload["lease_id"]}

    class FakeState:
        state_directory = tmp_path
        process_epoch = "epoch-channel-tool"
        connector_id = "connector_channel_tool"

        @staticmethod
        def client():
            return FakeClient()

        @staticmethod
        def read_lease():
            return {
                "connector_id": "connector_channel_tool",
                "process_epoch": "epoch-channel-tool",
                "lease_id": "lease-channel-tool",
                "ended": False,
            }

    runtime = ChannelRuntime(FakeState())  # type: ignore[arg-type]
    runtime.current_lease_id = "lease-channel-tool"
    runtime.routes = {
        "event-channel-tool": {
            "route_token": "route_" + "s" * 48,
            "lease_id": "lease-channel-tool",
            "conversation_id": "工具修改的聊天室",
            "message_ids": ["message_channel_tool"],
        }
    }
    notification = runtime._notification(
        {
            "event_id": "event-channel-tool",
            "conversation_id": "工具修改的聊天室",
            "fetched_at": 1_700_000_000,
            "required_message_ids": ["message_channel_tool"],
            "required_reply_count": 1,
            "messages": [
                {
                    "message_id": "message_channel_tool",
                    "sequence": 1,
                    "sender_participant_id": "participant_sender",
                    "sender_display_name": "发送者",
                    "sender_client_type": "codex-sender",
                    "body": "请确认。",
                    "reply_to": None,
                }
            ],
        }
    )
    serialized = notification.model_dump_json()
    assert "route_" not in serialized
    assert notification.params.meta["chat_id"] == "工具修改的聊天室"
    assert notification.params.meta["message_id"] == "event-channel-tool"
    assert notification.params.meta["user"] == "发送者"
    assert notification.params.meta["ts"] == "2023-11-14T22:13:20Z"

    delivered: list[str] = []

    class FakeGuide:
        transport_name = "claude-tmux-guide"

        @staticmethod
        def deliver(prompt: str) -> None:
            delivered.append(prompt)

    runtime.guide = FakeGuide()  # type: ignore[assignment]
    asyncio.run(runtime._deliver_notification(notification))
    assert delivered == [notification.params.content]
    assert runtime._transport_name() == "claude-tmux-guide"
    result = asyncio.run(
        runtime.call_tool(
            "agent_bridge_send",
            {
                "event_id": "event-channel-tool",
                "body": "@发送者 已确认。",
                "mentions": ["participant_sender"],
                "notification_mode": "mention",
            },
        )
    )
    assert result["message"]["message_id"] == "message_out"
    assert calls[0][0] == "send"
    assert calls[0][1]["route_token"].startswith("route_")
    assert calls[0][1]["notification_mode"] == "mention"


def test_claude_channel_keeps_large_message_batch_as_valid_json(
    tmp_path: Path,
) -> None:
    class FakeState:
        state_directory = tmp_path
        process_epoch = "epoch-channel-large"
        connector_id = "connector_channel_large"

        @staticmethod
        def client():
            return object()

    runtime = ChannelRuntime(FakeState())  # type: ignore[arg-type]
    messages = [
        {
            "message_id": f"message_large_{index}",
            "sequence": index,
            "sender_participant_id": "participant_sender",
            "sender_display_name": "发送者",
            "sender_client_type": "claude-code",
            "body": "长消息" * 4_000,
            "reply_to": None,
        }
        for index in range(20)
    ]
    notification = runtime._notification(
        {
            "event_id": "event-channel-large",
            "conversation_id": "工具修改的聊天室",
            "required_message_ids": [message["message_id"] for message in messages],
            "required_reply_count": len(messages),
            "messages": messages,
        }
    )
    content = notification.params.content
    serialized = content.split("MESSAGES_JSON:\n", 1)[1]
    decoded = json.loads(serialized)
    assert len(content) <= 96_000
    assert len(decoded) == 20
    assert all("可用历史工具读取" in message["body"] for message in decoded)


def test_claude_channel_keeps_one_event_until_the_tui_applies_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    event_state = {"value": "fetched"}
    waits = 0
    received: list[str] = []
    delivered: list[str] = []

    class FakeClient:
        def wait_native_channel_event(self, **_payload):
            nonlocal waits
            waits += 1
            if waits >= 4:
                raise asyncio.CancelledError
            state = event_state["value"]
            return {
                "event": {
                    "event_id": "event-poll-until-applied",
                    "conversation_id": "工具修改的聊天室",
                    "state": state,
                    "deliverable": state == "fetched",
                    "fetched_at": 1_700_000_000,
                    "required_message_ids": [],
                    "required_reply_count": 0,
                    "message_ids": ["message-poll-until-applied"],
                    "messages": [
                        {
                            "message_id": "message-poll-until-applied",
                            "sequence": 2,
                            "sender_participant_id": "participant_sender",
                            "sender_display_name": "发送者",
                            "sender_client_type": "web-user",
                            "body": "请查看。",
                            "reply_to": None,
                        }
                    ],
                }
            }

        def receive_native_channel_event(self, **payload):
            received.append(str(payload["stage"]))
            event_state["value"] = "injected"
            return {"event": {"state": "injected"}}

    client = FakeClient()

    class FakeState:
        state_directory = tmp_path
        process_epoch = "epoch-poll-until-applied"
        connector_id = "connector_poll_until_applied"

        @staticmethod
        def client():
            return client

        @staticmethod
        def read_lease():
            return {
                "connector_id": "connector_poll_until_applied",
                "process_epoch": "epoch-poll-until-applied",
                "lease_id": "lease-poll-until-applied",
                "ended": False,
            }

    class FakeGuide:
        transport_name = "claude-tmux-guide"

        @staticmethod
        def deliver(prompt: str) -> None:
            delivered.append(prompt)

    async def advance_without_waiting(_seconds: float) -> None:
        if event_state["value"] == "injected":
            event_state["value"] = "replied"

    runtime = ChannelRuntime(FakeState())  # type: ignore[arg-type]
    runtime.guide = FakeGuide()  # type: ignore[assignment]
    runtime.session = object()  # type: ignore[assignment]
    monkeypatch.setattr(
        "agent_bridge.claude_channel.asyncio.sleep",
        advance_without_waiting,
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runtime._poll_loop())
    assert len(delivered) == 1
    assert received == ["injected"]
    route = runtime.routes["event-poll-until-applied"]
    assert route["delivery_attempt_count"] == 1
    assert route["completed_at"] > 0
    assert waits == 4


def test_claude_channel_stdio_declares_channel_capability_and_tools(
    tmp_path: Path,
) -> None:
    configured = configure_resident_connector(
        connector_id="connector_channelstdio123",
        enrollment_token="enroll_channel-stdio-private-token",
        bridge_url="http://127.0.0.1:8765",
        product="claude-code",
        username="channel-stdio",
        signature="stdio test",
        conversation_id="channel-stdio-room",
        adapter_kind="claude-code",
        requested_mode="resident",
        workspace_path=str(tmp_path),
        home=tmp_path,
        system_name="Darwin",
        activate=False,
    )

    async def scenario() -> None:
        environment = dict(os.environ)
        environment["AGENT_BRIDGE_CLAUDE_STATE_DIRECTORY"] = (
            configured.state_directory
        )
        environment["AGENT_BRIDGE_CLAUDE_PROCESS_EPOCH"] = "epoch-stdio-one"
        parameters = StdioServerParameters(
            command=str(BRIDGE_ROOT / "bin" / "agent-bridge-claude-channel"),
            args=["--state-directory", configured.state_directory],
            env=environment,
            cwd=str(BRIDGE_ROOT),
        )
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                initialized = await session.initialize()
                assert initialized.capabilities.experimental == {
                    "claude/channel": {}
                }
                listed = await session.list_tools()
                assert {tool.name for tool in listed.tools} >= {
                    "agent_bridge_apply_event",
                    "agent_bridge_reply",
                    "agent_bridge_send",
                    "agent_bridge_participants",
                    "agent_bridge_history",
                    "agent_bridge_search_history",
                }

    asyncio.run(scenario())
