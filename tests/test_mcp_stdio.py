from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from urllib.request import urlopen

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent_bridge.store import BridgeStore
from agent_bridge.web_auth import WebAuthStore


BRIDGE_ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def bridge_server(database: Path):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    env.update(
        {
            "AGENT_BRIDGE_DB": str(database),
            "AGENT_BRIDGE_VIEWER_HOST": "127.0.0.1",
            "AGENT_BRIDGE_VIEWER_PORT": str(port),
        }
    )
    process = subprocess.Popen(
        [str(BRIDGE_ROOT / "bin" / "agent-bridge-viewer")],
        cwd=str(BRIDGE_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 8
        while True:
            if process.poll() is not None:
                raise RuntimeError(process.stderr.read())
            try:
                with urlopen(f"{url}/api/health", timeout=0.25) as response:
                    if response.status == 200:
                        break
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("Agent Bridge test server did not start")
            time.sleep(0.05)
        yield url
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@asynccontextmanager
async def mcp_client(
    server_url: str,
    client_type: str,
    *,
    extra_env: dict[str, str] | None = None,
):
    env = dict(os.environ)
    env.update(
        {
            "AGENT_BRIDGE_URL": server_url,
            "AGENT_BRIDGE_CLIENT_TYPE": client_type,
            "AGENT_BRIDGE_MAX_WAIT_SECONDS": "5",
        }
    )
    env.update(extra_env or {})
    parameters = StdioServerParameters(
        command=str(BRIDGE_ROOT / "bin" / "agent-bridge-mcp"),
        args=[],
        env=env,
        # Codex starts MCP servers from the task cwd, not the bridge checkout.
        # Keep this integration test cross-directory so the launcher must make
        # its own package import path authoritative.
        cwd=str(BRIDGE_ROOT.parent),
    )
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            yield session


def payload(result):
    assert not result.is_error
    assert isinstance(result.structured_content, dict)
    return result.structured_content


def run_cli(database: Path, *args: str) -> dict:
    process = subprocess.run(
        [
            str(BRIDGE_ROOT / "bin" / "agent-bridge"),
            "--database",
            str(database),
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
        env=dict(os.environ),
    )
    return json.loads(process.stdout)


def test_two_real_stdio_mcp_processes_use_open_registration_central_chat(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = tmp_path / "bridge.db"
        store = BridgeStore(database)
        store.create_user_room("MCP沟通群")

        with bridge_server(database) as server_url:
            async with mcp_client(server_url, "codex") as codex:
                async with mcp_client(server_url, "claude-code") as claude:
                    codex_tools = await codex.list_tools()
                    expected = {
                        "agent_register",
                        "agent_heartbeat",
                        "agent_send",
                        "agent_create_room",
                        "agent_wait",
                        "agent_notifications",
                        "agent_message_action",
                        "agent_reply",
                        "agent_history",
                        "agent_participants",
                        "agent_update_profile",
                        "agent_request_nickname",
                        "agent_set_follow",
                        "agent_following",
                        "agent_accept_invitation",
                    }
                    assert expected == {tool.name for tool in codex_tools.tools}
                    register_tool = next(
                        tool
                        for tool in codex_tools.tools
                        if tool.name == "agent_register"
                    )
                    assert set(register_tool.input_schema["properties"]) == {
                        "conversation_id",
                        "username",
                        "session_alias",
                        "signature",
                        "roles",
                    }
                    send_tool = next(
                        tool for tool in codex_tools.tools if tool.name == "agent_send"
                    )
                    assert "participant_id" not in send_tool.input_schema["properties"]
                    assert "message_kind" not in send_tool.input_schema["properties"]
                    assert "mentions" in send_tool.input_schema["properties"]

                    codex_registration = payload(
                        await codex.call_tool(
                            "agent_register",
                            {
                                "conversation_id": "MCP沟通群",
                                "username": "小可爱",
                                "session_alias": "Codex 工具审计",
                                "roles": ["reviewer"],
                            },
                        )
                    )
                    claude_registration = payload(
                        await claude.call_tool(
                            "agent_register",
                            {
                                "conversation_id": "MCP沟通群",
                                "username": "小鲸鱼娘",
                                "session_alias": "Claude 工具开发",
                                "roles": ["developer"],
                            },
                        )
                    )
                    assert codex_registration["client_type"] == "codex-小可爱"
                    assert (
                        claude_registration["client_type"]
                        == "claude-code-小鲸鱼娘"
                    )
                    assert "access_token" not in codex_registration

                    sent = payload(
                        await claude.call_tool(
                            "agent_send",
                            {
                                "conversation_id": "MCP沟通群",
                                "body": "请复核 settle 事务。",
                                "audience_kind": "participant",
                                "audience_value": codex_registration[
                                    "participant_id"
                                ],
                            },
                        )
                    )
                    notification = payload(
                        await codex.call_tool(
                            "agent_notifications",
                            {"after_sequence": 0},
                        )
                    )
                    assert notification["has_new"] is True
                    assert notification["new_since_cursor"]["priority_counts"][
                        "mention"
                    ] == 1
                    assert "请复核 settle 事务。" not in str(notification)
                    received = payload(
                        await codex.call_tool(
                            "agent_wait",
                            {"wait_seconds": 2},
                        )
                    )
                    assert received["messages"][0]["message_id"] == sent["message_id"]

                    reply = payload(
                        await codex.call_tool(
                            "agent_reply",
                            {
                                "message_id": sent["message_id"],
                                "body": "必须把 verify 和 UPDATE 收进同一事务。",
                            },
                        )
                    )
                    answer = payload(
                        await claude.call_tool(
                            "agent_wait",
                            {"wait_seconds": 2},
                        )
                    )
                    assert answer["messages"][0]["reply_to"] == sent["message_id"]
                    nested = await claude.call_tool(
                        "agent_reply",
                        {
                            "message_id": reply["reply"]["message_id"],
                            "body": "这条会形成回声链。",
                        },
                    )
                    assert nested.is_error

    asyncio.run(scenario())


def test_real_stdio_mcp_reuses_basic_invitation_and_keeps_secrets_private(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = tmp_path / "bridge.db"
        store = BridgeStore(database)
        WebAuthStore(database)
        store.create_user_room("MCP邀请群")
        with store._connection() as connection:
            admin_id = str(
                connection.execute(
                    "SELECT user_id FROM web_users WHERE username = 'admin'"
                ).fetchone()[0]
            )
        invitation = store.create_agent_invitation(
            conversation_id="MCP邀请群",
            product="future-agent",
            requested_mode="basic",
            adapter_kind="manual",
            created_by_web_user_id=admin_id,
            reusable=True,
        )
        invitation_token = str(invitation.pop("invitation_token"))
        connector_home = tmp_path / "connectors"

        with bridge_server(database) as server_url:
            async with mcp_client(
                server_url,
                "future-agent",
                extra_env={
                    "AGENT_BRIDGE_INVITATION_TOKEN": invitation_token,
                    "AGENT_BRIDGE_CONNECTOR_HOME": str(connector_home),
                },
            ) as invited:
                accepted = payload(
                    await invited.call_tool(
                        "agent_accept_invitation",
                        {
                            "username": "远端值守者",
                            "signature": "接受结构化邀请后加入。",
                            "workspace_path": str(tmp_path),
                        },
                    )
                )
            async with mcp_client(
                server_url,
                "future-agent",
                extra_env={
                    "AGENT_BRIDGE_INVITATION_TOKEN": invitation_token,
                    "AGENT_BRIDGE_CONNECTOR_HOME": str(connector_home),
                },
            ) as second_invited:
                second_accepted = payload(
                    await second_invited.call_tool(
                        "agent_accept_invitation",
                        {
                            "username": "第二位远端值守者",
                            "signature": "复用同一邀请后独立加入。",
                            "workspace_path": str(tmp_path),
                        },
                    )
                )

        assert accepted["conversation_id"] == "MCP邀请群"
        assert accepted["invitation_accepted"] is True
        assert accepted["invitation_consumed"] is False
        assert accepted["resident_setup"]["status"] == "manual"
        assert "access_token" not in str(accepted)
        assert "enroll_" not in str(accepted)
        assert second_accepted["connector_id"] != accepted["connector_id"]
        assert second_accepted["participant_id"] != accepted["participant_id"]
        assert second_accepted["invitation_consumed"] is False
        assert "access_token" not in str(second_accepted)
        assert "enroll_" not in str(second_accepted)
        state_directories = list(connector_home.glob("connector_*"))
        assert len(state_directories) == 2
        for state_directory in state_directories:
            enrollment_file = state_directory / "enrollment.token"
            assert enrollment_file.is_file()
            assert enrollment_file.stat().st_mode & 0o777 == 0o600
        listed = store.list_agent_invitations(
            requesting_web_user_id=admin_id,
        )[0]
        assert listed["status"] == "active"
        assert listed["use_count"] == 2
        assert listed["connector_count"] == 2
        assert listed["setup_status"] == "manual"

    asyncio.run(scenario())


def test_cli_admin_and_mcp_chat_share_one_sqlite_authority(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = tmp_path / "bridge.db"
        run_cli(database, "create-room", "--conversation", "shared-room")
        with bridge_server(database) as server_url:
            async with mcp_client(server_url, "future-mcp-agent") as mcp_session:
                payload(
                    await mcp_session.call_tool(
                        "agent_register",
                        {
                            "conversation_id": "shared-room",
                            "username": "未来伙伴",
                            "session_alias": "MCP Agent",
                        },
                    )
                )
                sent = payload(
                    await mcp_session.call_tool(
                        "agent_send",
                        {
                            "conversation_id": "shared-room",
                            "body": "MCP 写入，CLI 只读。",
                        },
                    )
                )
        history = run_cli(
            database,
            "history",
            "--conversation",
            "shared-room",
        )
        assert history["messages"][0]["message_id"] == sent["message_id"]

    asyncio.run(scenario())
