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
from agent_bridge.claude_native import ClaudeNativeError
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

        def download_attachment(self, **payload):
            calls.append(("download", payload))
            return {
                "attachment_id": payload["attachment_id"],
                "saved_path": str(payload["destination_path"]),
            }

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
            "attachment_ids": ["attachment_channel_tool"],
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
                    "visibility": {
                        "kind": "restricted",
                        "target_kind": "participants",
                        "recipients": [
                            {
                                "participant_id": "participant_receiver",
                                "display_name": "接收者",
                            }
                        ],
                    },
                    "attachments": [
                        {
                            "attachment_id": "attachment_channel_tool",
                            "kind": "image",
                            "filename": "设计图.png",
                            "media_type": "image/png",
                            "size_bytes": 128,
                            "sha256": "a" * 64,
                        }
                    ],
                    "links": [
                        {
                            "link_id": "link_channel_tool",
                            "url": "https://example.com/spec",
                            "host": "example.com",
                            "display": "example.com/spec",
                        }
                    ],
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
    notification_messages = json.loads(
        notification.params.content.split("MESSAGES_JSON:\n", 1)[1]
    )
    assert notification_messages[0]["visibility"] == {
        "kind": "restricted",
        "target_kind": "participants",
    }
    assert notification_messages[0]["attachments"][0]["attachment_id"] == (
        "attachment_channel_tool"
    )
    assert notification_messages[0]["links"][0]["url"] == (
        "https://example.com/spec"
    )
    assert "recipients" not in notification_messages[0]["visibility"]

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
    destination = tmp_path / "received" / "设计图.png"
    downloaded = asyncio.run(
        runtime.call_tool(
            "agent_bridge_download_attachment",
            {
                "event_id": "event-channel-tool",
                "attachment_id": "attachment_channel_tool",
                "destination_path": str(destination),
            },
        )
    )
    assert downloaded == {
        "attachment_id": "attachment_channel_tool",
        "saved_path": str(destination),
    }
    assert calls[2] == (
        "download",
        {
            "attachment_id": "attachment_channel_tool",
            "destination_path": str(destination),
            "overwrite": False,
        },
    )
    with pytest.raises(
        ClaudeNativeError,
        match="attachment_id is not part of this event",
    ):
        asyncio.run(
            runtime.call_tool(
                "agent_bridge_download_attachment",
                {
                    "event_id": "event-channel-tool",
                    "attachment_id": "attachment_other_event",
                    "destination_path": str(destination),
                },
            )
        )


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


def test_claude_channel_bounds_many_first_class_links_without_invalid_json(
    tmp_path: Path,
) -> None:
    class FakeState:
        state_directory = tmp_path
        process_epoch = "epoch-channel-links"
        connector_id = "connector_channel_links"

        @staticmethod
        def client():
            return object()

    runtime = ChannelRuntime(FakeState())  # type: ignore[arg-type]
    messages = [
        {
            "message_id": f"message_links_{index}",
            "sequence": index,
            "sender_participant_id": "participant_sender",
            "sender_display_name": "发送者",
            "sender_client_type": "web-user",
            "body": "",
            "reply_to": None,
            "links": [
                {
                    "link_id": f"link_{index}_{link_index}",
                    "url": (
                        f"https://example.com/{index}/{link_index}/"
                        + "x" * 1_900
                    ),
                    "host": "example.com",
                    "display": f"example.com/{index}/{link_index}",
                }
                for link_index in range(8)
            ],
        }
        for index in range(20)
    ]
    notification = runtime._notification(
        {
            "event_id": "event-channel-links",
            "conversation_id": "链接群",
            "required_message_ids": [],
            "required_reply_count": 0,
            "messages": messages,
        }
    )
    decoded = json.loads(
        notification.params.content.split("MESSAGES_JSON:\n", 1)[1]
    )
    assert len(notification.params.content) <= 96_000
    assert sum(int(item.get("links_omitted_count") or 0) for item in decoded) > 0
    assert any(item["links"] for item in decoded)


def test_claude_channel_rotates_after_injection_without_blocking_new_events(
    tmp_path: Path,
) -> None:
    request_ids: list[str] = []
    received: list[tuple[str, str]] = []
    delivered: list[str] = []

    class FakeClient:
        def wait_native_channel_event(self, **payload):
            request_id = str(payload["request_id"])
            request_ids.append(request_id)
            index = len(request_ids)
            if index > 2:
                raise asyncio.CancelledError
            suffix = "one" if index == 1 else "two"
            return {
                "event": {
                    "event_id": f"event-nonblocking-{suffix}",
                    "conversation_id": "工具修改的聊天室",
                    "state": "fetched",
                    "deliverable": True,
                    "fetched_at": 1_700_000_000,
                    "required_message_ids": [f"message-nonblocking-{suffix}"],
                    "required_reply_count": 1,
                    "message_ids": [f"message-nonblocking-{suffix}"],
                    "messages": [
                        {
                            "message_id": f"message-nonblocking-{suffix}",
                            "sequence": index,
                            "sender_participant_id": "participant_sender",
                            "sender_display_name": "发送者",
                            "sender_client_type": "web-user",
                            "body": f"请查看第 {index} 条。",
                            "reply_to": None,
                            "attachments": (
                                [
                                    {
                                        "attachment_id": "attachment-nonblocking-one",
                                        "kind": "file",
                                        "filename": "说明.txt",
                                        "media_type": "text/plain",
                                        "size_bytes": 4,
                                        "sha256": "b" * 64,
                                    }
                                ]
                                if index == 1
                                else []
                            ),
                        }
                    ],
                }
            }

        def receive_native_channel_event(self, **payload):
            received.append((str(payload["event_id"]), str(payload["stage"])))
            return {"event": {"state": "injected"}}

    client = FakeClient()

    class FakeState:
        state_directory = tmp_path
        process_epoch = "epoch-nonblocking-events"
        connector_id = "connector_nonblocking_events"

        @staticmethod
        def client():
            return client

        @staticmethod
        def read_lease():
            return {
                "connector_id": "connector_nonblocking_events",
                "process_epoch": "epoch-nonblocking-events",
                "lease_id": "lease-nonblocking-events",
                "ended": False,
            }

    class FakeGuide:
        transport_name = "claude-tmux-guide"

        @staticmethod
        def deliver(prompt: str) -> None:
            delivered.append(prompt)

    runtime = ChannelRuntime(FakeState())  # type: ignore[arg-type]
    runtime.guide = FakeGuide()  # type: ignore[assignment]
    runtime.session = object()  # type: ignore[assignment]
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runtime._poll_loop())
    assert len(delivered) == 2
    assert received == [
        ("event-nonblocking-one", "injected"),
        ("event-nonblocking-two", "injected"),
    ]
    assert len(set(request_ids)) == 3
    assert runtime.request_id == request_ids[-1]
    for index, suffix in enumerate(("one", "two")):
        route = runtime.routes[f"event-nonblocking-{suffix}"]
        assert route["request_id"] == request_ids[index]
        assert route["delivery_attempt_count"] == 1
        assert route.get("completed_at") is None
    assert runtime.routes["event-nonblocking-one"]["attachment_ids"] == [
        "attachment-nonblocking-one"
    ]
    assert runtime.routes["event-nonblocking-two"]["attachment_ids"] == []


def test_claude_channel_retries_old_route_without_rotating_current_request(
    tmp_path: Path,
    monkeypatch,
) -> None:
    waited: list[dict] = []
    delivered: list[str] = []

    class FakeClient:
        def wait_native_channel_event(self, **payload):
            waited.append(payload)
            return {
                "event": {
                    "event_id": "event-monitored-old",
                    "conversation_id": "工具修改的聊天室",
                    "state": "applied",
                    "deliverable": False,
                    "fetched_at": 1_700_000_000,
                    "required_message_ids": ["message-monitored-old"],
                    "required_reply_count": 1,
                    "message_ids": ["message-monitored-old"],
                    "messages": [
                        {
                            "message_id": "message-monitored-old",
                            "sequence": 1,
                            "sender_participant_id": "participant_sender",
                            "sender_display_name": "发送者",
                            "sender_client_type": "web-user",
                            "body": "请精确回复。",
                            "reply_to": None,
                        }
                    ],
                }
            }

    client = FakeClient()

    class FakeState:
        state_directory = tmp_path
        process_epoch = "epoch-monitor-old"
        connector_id = "connector_monitor_old"

        @staticmethod
        def client():
            return client

    class FakeGuide:
        transport_name = "claude-tmux-guide"

        @staticmethod
        def deliver(prompt: str) -> None:
            delivered.append(prompt)

    runtime = ChannelRuntime(FakeState())  # type: ignore[arg-type]
    runtime.guide = FakeGuide()  # type: ignore[assignment]
    runtime.current_lease_id = "lease-monitor-old"
    current_request = runtime.request_id
    current_token = runtime.route_token
    runtime.routes = {
        "event-monitored-old": {
            "request_id": "request_monitored_old",
            "route_token": "route_" + "m" * 48,
            "lease_id": "lease-monitor-old",
            "conversation_id": "工具修改的聊天室",
            "message_ids": ["message-monitored-old"],
            "last_event_state": "injected",
            "state_changed_at": 1.0,
            "delivery_attempt_count": 1,
            "last_delivery_at": 1.0,
        }
    }
    monkeypatch.setattr(
        "agent_bridge.claude_channel.CHANNEL_RETRY_INITIAL_SECONDS",
        0.0,
    )
    asyncio.run(
        runtime._monitor_routes_once(
            {
                "lease_id": "lease-monitor-old",
                "connector_id": "connector_monitor_old",
                "process_epoch": "epoch-monitor-old",
                "ended": False,
            }
        )
    )
    assert len(delivered) == 1
    assert waited[0]["request_id"] == "request_monitored_old"
    assert waited[0]["wait_seconds"] == 0
    assert runtime.request_id == current_request
    assert runtime.route_token == current_token
    route = runtime.routes["event-monitored-old"]
    assert route["last_event_state"] == "applied"
    assert route["delivery_attempt_count"] == 2
    assert route.get("completed_at") is None


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
                    "agent_bridge_download_attachment",
                }

    asyncio.run(scenario())
