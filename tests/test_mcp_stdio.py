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
async def mcp_client(server_url: str, client_type: str):
    env = dict(os.environ)
    env.update(
        {
            "AGENT_BRIDGE_URL": server_url,
            "AGENT_BRIDGE_CLIENT_TYPE": client_type,
            "AGENT_BRIDGE_MAX_WAIT_SECONDS": "5",
        }
    )
    parameters = StdioServerParameters(
        command=str(BRIDGE_ROOT / "bin" / "agent-bridge-mcp"),
        args=[],
        env=env,
        cwd=str(BRIDGE_ROOT),
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
                        "agent_message_action",
                        "agent_reply",
                        "agent_history",
                        "agent_participants",
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
                        "roles",
                    }
                    send_tool = next(
                        tool for tool in codex_tools.tools if tool.name == "agent_send"
                    )
                    assert "participant_id" not in send_tool.input_schema["properties"]
                    assert "message_kind" not in send_tool.input_schema["properties"]

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
