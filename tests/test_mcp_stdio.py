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


ALL_MCP_TOOLS = {
    "agent_register",
    "agent_accept_invitation",
    "agent_update_profile",
    "agent_list_avatars",
    "agent_request_nickname",
    "agent_set_follow",
    "agent_following",
    "agent_set_room_dnd",
    "agent_heartbeat",
    "agent_duty",
    "agent_send",
    "agent_create_room",
    "agent_wait",
    "agent_notifications",
    "agent_message_action",
    "agent_reply",
    "agent_history",
    "agent_search_history",
    "agent_download_attachment",
    "agent_participants",
    "agent_task_next",
    "agent_task_inputs",
    "agent_task_update",
    "agent_task_delegate",
}


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


def test_unconfigured_stdio_mcp_cannot_self_register_into_a_room(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = tmp_path / "bridge.db"
        store = BridgeStore(database)
        store.create_user_room("隔离群")

        with bridge_server(database) as server_url:
            async with mcp_client(server_url, "codex") as unconfigured:
                denied = await unconfigured.call_tool(
                    "agent_register",
                    {
                        "conversation_id": "隔离群",
                        "username": "误入的新会话",
                        "signature": "普通项目任务，不是受邀 Agent。",
                    },
                )
                assert denied.is_error
                assert "Direct Agent room registration is disabled" in str(
                    denied.content
                )

        with store._connection() as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM participants "
                    "WHERE client_type = 'codex-误入的新会话'"
                ).fetchone()[0]
                == 0
            )
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM memberships WHERE conversation_id = '隔离群'"
                ).fetchone()[0]
                == 0
            )

    asyncio.run(scenario())


def test_two_real_stdio_mcp_processes_use_explicit_direct_registration_chat(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = tmp_path / "bridge.db"
        store = BridgeStore(database)
        store.create_user_room("MCP沟通群")

        with bridge_server(database) as server_url:
            direct_registration = {"AGENT_BRIDGE_ALLOW_DIRECT_REGISTRATION": "1"}
            async with mcp_client(
                server_url,
                "codex",
                extra_env=direct_registration,
            ) as codex:
                async with mcp_client(
                    server_url,
                    "claude-code",
                    extra_env=direct_registration,
                ) as claude:
                    codex_tools = await codex.list_tools()
                    assert ALL_MCP_TOOLS == {tool.name for tool in codex_tools.tools}
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
                    accept_tool = next(
                        tool
                        for tool in codex_tools.tools
                        if tool.name == "agent_accept_invitation"
                    )
                    assert "avatar_key" in accept_tool.input_schema["properties"]
                    profile_tool = next(
                        tool
                        for tool in codex_tools.tools
                        if tool.name == "agent_update_profile"
                    )
                    assert profile_tool.input_schema.get("required", []) == []
                    send_tool = next(
                        tool for tool in codex_tools.tools if tool.name == "agent_send"
                    )
                    assert "participant_id" not in send_tool.input_schema["properties"]
                    assert "message_kind" not in send_tool.input_schema["properties"]
                    assert "mentions" in send_tool.input_schema["properties"]
                    assert "notification_mode" in send_tool.input_schema["properties"]

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
                    assert claude_registration["client_type"] == "claude-code-小鲸鱼娘"
                    assert "access_token" not in codex_registration

                    sent = payload(
                        await claude.call_tool(
                            "agent_send",
                            {
                                "conversation_id": "MCP沟通群",
                                "body": "请复核 settle 事务。",
                                "audience_kind": "participant",
                                "audience_value": codex_registration["participant_id"],
                                "notification_mode": "mention",
                            },
                        )
                    )
                    dnd = payload(
                        await codex.call_tool(
                            "agent_set_room_dnd",
                            {
                                "conversation_id": "MCP沟通群",
                                "enabled": True,
                            },
                        )
                    )
                    assert dnd["active"] is True
                    assert dnd["digest_wake_suppressed"] is True
                    notification = payload(
                        await codex.call_tool(
                            "agent_notifications",
                            {"after_sequence": 0},
                        )
                    )
                    assert notification["has_new"] is True
                    assert (
                        notification["new_since_cursor"]["priority_counts"]["mention"]
                        == 1
                    )
                    assert "请复核 settle 事务。" not in str(notification)
                    received = payload(
                        await codex.call_tool(
                            "agent_wait",
                            {"wait_seconds": 2},
                        )
                    )
                    assert received["messages"][0]["message_id"] == sent["message_id"]
                    assert sent["review_routing"]["notified"] is True
                    assert sent["review_routing"]["source"] == ("audience:participant")

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


def test_real_stdio_mcp_all_tool_interfaces_and_acl_matrix(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = tmp_path / "bridge.db"
        store = BridgeStore(database)
        auth = WebAuthStore(database, captcha_generator=lambda: "ABCDE")
        captcha = auth.create_captcha()
        admin, _token = auth.login(
            username="admin",
            password="admin",
            captcha_id=str(captcha["captcha_id"]),
            captcha_answer="ABCDE",
        )
        room = "MCP全接口群"
        store.create_user_room(room)
        invitation = store.create_agent_invitation(
            conversation_id=room,
            product="future-agent",
            requested_mode="basic",
            adapter_kind="manual",
            created_by_web_user_id=str(admin["user_id"]),
        )
        invitation_token = str(invitation["invitation_token"])
        called: set[str] = set()

        async def invoke(session, name: str, arguments: dict | None = None) -> dict:
            called.add(name)
            return payload(await session.call_tool(name, arguments or {}))

        direct = {"AGENT_BRIDGE_ALLOW_DIRECT_REGISTRATION": "1"}
        with bridge_server(database) as server_url:
            async with mcp_client(
                server_url,
                "codex",
                extra_env=direct,
            ) as first:
                async with mcp_client(
                    server_url,
                    "claude-code",
                    extra_env=direct,
                ) as second:
                    async with mcp_client(
                        server_url,
                        "future-agent",
                        extra_env={
                            "AGENT_BRIDGE_INVITATION_TOKEN": invitation_token,
                            "AGENT_BRIDGE_CONNECTOR_HOME": str(tmp_path / "connectors"),
                        },
                    ) as invited:
                        listed = await first.list_tools()
                        assert {tool.name for tool in listed.tools} == ALL_MCP_TOOLS

                        called.add("agent_duty")
                        unbound_duty = await first.call_tool(
                            "agent_duty",
                            {"wait_seconds": 0, "limit": 1},
                        )
                        assert unbound_duty.is_error
                        assert "exact Codex TUI" in str(unbound_duty.content)

                        first_identity = await invoke(
                            first,
                            "agent_register",
                            {
                                "conversation_id": room,
                                "username": "全接口一号",
                                "signature": "负责接口主流程。",
                                "roles": ["reviewer"],
                            },
                        )
                        second_identity = await invoke(
                            second,
                            "agent_register",
                            {
                                "conversation_id": room,
                                "username": "全接口二号",
                                "signature": "负责隔离与委派复核。",
                                "roles": ["developer"],
                            },
                        )
                        accepted = await invoke(
                            invited,
                            "agent_accept_invitation",
                            {
                                "username": "受邀接口员",
                                "signature": "只验证邀请接入接口。",
                                "workspace_path": str(tmp_path),
                                "enable_resident": False,
                            },
                        )
                        assert accepted["invitation_accepted"] is True

                        heartbeat = await invoke(
                            first,
                            "agent_heartbeat",
                            {"status": "online"},
                        )
                        assert heartbeat["status"] == "online"
                        avatars = await invoke(
                            first,
                            "agent_list_avatars",
                            {"vendor": "gpt"},
                        )
                        assert avatars["avatars"]
                        profile = await invoke(
                            first,
                            "agent_update_profile",
                            {
                                "signature": "已完成真实 MCP 全接口复核。",
                                "avatar_key": avatars["avatars"][0]["key"],
                            },
                        )
                        assert profile["signature"].startswith("已完成真实")
                        nickname = await invoke(
                            first,
                            "agent_request_nickname",
                            {"display_name": "接口小队长"},
                        )
                        assert nickname["status"] == "pending"

                        followed = await invoke(
                            first,
                            "agent_set_follow",
                            {
                                "conversation_id": room,
                                "followed_participant_id": second_identity[
                                    "participant_id"
                                ],
                                "following": True,
                            },
                        )
                        assert followed["following"] is True
                        following = await invoke(
                            first,
                            "agent_following",
                            {"conversation_id": room},
                        )
                        assert (
                            following["following"][0]["followed_participant_id"]
                            == (second_identity["participant_id"])
                        )
                        dnd = await invoke(
                            first,
                            "agent_set_room_dnd",
                            {"conversation_id": room, "enabled": True},
                        )
                        assert dnd["active"] is True
                        created_room = await invoke(
                            first,
                            "agent_create_room",
                            {"conversation_id": "MCP接口员自建群"},
                        )
                        assert created_room["creator_kind"] == "agent"
                        participants = await invoke(
                            first,
                            "agent_participants",
                            {"conversation_id": room},
                        )
                        assert len(participants["participants"]) >= 3

                        sent = await invoke(
                            second,
                            "agent_send",
                            {
                                "conversation_id": room,
                                "body": "@codex-全接口一号 请复核接口矩阵。",
                                "audience_kind": "participant",
                                "audience_value": first_identity["participant_id"],
                                "mentions": [first_identity["participant_id"]],
                                "links": ["https://example.com/interface-matrix"],
                                "notification_mode": "mention",
                            },
                        )
                        notification = await invoke(
                            first,
                            "agent_notifications",
                            {"after_sequence": 0},
                        )
                        assert notification["has_new"] is True
                        claimed = await invoke(
                            first,
                            "agent_message_action",
                            {
                                "message_id": sent["message_id"],
                                "action": "claim",
                                "lease_seconds": 30,
                            },
                        )
                        assert claimed["action"] == "claim"
                        assert claimed["claimed_by"] == first_identity["participant_id"]
                        released = await invoke(
                            first,
                            "agent_message_action",
                            {
                                "message_id": sent["message_id"],
                                "action": "release",
                            },
                        )
                        assert released["action"] == "release"
                        assert released["released"] is True
                        waiting = await invoke(
                            first,
                            "agent_wait",
                            {"wait_seconds": 0},
                        )
                        assert (
                            waiting["messages"][0]["message_id"] == sent["message_id"]
                        )
                        reply = await invoke(
                            first,
                            "agent_reply",
                            {
                                "message_id": sent["message_id"],
                                "body": "已逐项复核接口矩阵。",
                            },
                        )
                        assert reply["reply"]["reply_to"] == sent["message_id"]
                        history = await invoke(
                            first,
                            "agent_history",
                            {"conversation_id": room, "limit": 20},
                        )
                        assert any(
                            message["message_id"] == sent["message_id"]
                            for message in history["messages"]
                        )
                        searched = await invoke(
                            first,
                            "agent_search_history",
                            {"conversation_id": room, "query": "接口矩阵"},
                        )
                        assert searched["results"]

                        attachment_content = b"agent-bridge-directed-attachment"
                        attachment_message = store.send_owner_message(
                            conversation_id=room,
                            body_text="只让一号读取这份接口证据。",
                            mentions=[first_identity["participant_id"]],
                            attachments=[
                                {
                                    "filename": "interface-evidence.txt",
                                    "media_type": "text/plain",
                                    "content": attachment_content,
                                }
                            ],
                        )
                        attachment_id = attachment_message["attachments"][0][
                            "attachment_id"
                        ]
                        destination = tmp_path / "downloaded-evidence.txt"
                        downloaded = await invoke(
                            first,
                            "agent_download_attachment",
                            {
                                "attachment_id": attachment_id,
                                "destination_path": str(destination),
                            },
                        )
                        assert (
                            downloaded["sha256"]
                            == attachment_message["attachments"][0]["sha256"]
                        )
                        assert destination.read_bytes() == attachment_content
                        denied = await second.call_tool(
                            "agent_download_attachment",
                            {
                                "attachment_id": attachment_id,
                                "destination_path": str(tmp_path / "must-not-exist"),
                            },
                        )
                        assert denied.is_error
                        assert not (tmp_path / "must-not-exist").exists()

                        task_message = store.send_web_task(
                            authorized_session_id=str(admin["session_id"]),
                            participant_id=str(admin["participant_id"]),
                            conversation_id=room,
                            body_text="完成 MCP 任务工具联调。",
                            target_participant_ids=[first_identity["participant_id"]],
                        )
                        task = await invoke(
                            first,
                            "agent_task_next",
                            {"wait_seconds": 0},
                        )
                        assert (
                            task["task"]["task_id"] == task_message["task"]["task_id"]
                        )
                        running = await invoke(
                            first,
                            "agent_task_update",
                            {
                                "task_id": task["task"]["task_id"],
                                "status": "running",
                                "execution_cwd": str(tmp_path),
                                "execution_thread_id": "mcp-matrix-primary",
                            },
                        )
                        assert running["task"]["status"] == "running"
                        task_inputs = await invoke(
                            first,
                            "agent_task_inputs",
                            {
                                "task_id": task["task"]["task_id"],
                                "action": "poll",
                                "limit": 10,
                            },
                        )
                        assert task_inputs["task_id"] == task["task"]["task_id"]
                        assert task_inputs["inputs"] == []
                        delegated = await invoke(
                            first,
                            "agent_task_delegate",
                            {
                                "parent_task_id": task["task"]["task_id"],
                                "body": "复核定向附件 ACL。",
                                "target_participant_ids": [
                                    second_identity["participant_id"]
                                ],
                            },
                        )
                        child = await invoke(
                            second,
                            "agent_task_next",
                            {"wait_seconds": 0},
                        )
                        assert (
                            child["task"]["task_id"]
                            == delegated["task"]["task_id"]
                        )
                        child_done = await invoke(
                            second,
                            "agent_task_update",
                            {
                                "task_id": child["task"]["task_id"],
                                "status": "completed",
                                "result_summary": "ACL 复核通过。",
                            },
                        )
                        assert child_done["task"]["status"] == "completed"
                        parent_done = await invoke(
                            first,
                            "agent_task_update",
                            {
                                "task_id": task["task"]["task_id"],
                                "status": "completed",
                                "result_summary": "24 个工具接口联调完成。",
                            },
                        )
                        assert parent_done["task"]["status"] == "completed"

        assert called == ALL_MCP_TOOLS

    asyncio.run(scenario())


def test_real_stdio_residents_auto_register_for_open_and_enrolled_identity(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = tmp_path / "bridge.db"
        store = BridgeStore(database)
        WebAuthStore(database)
        store.create_user_room("中央值守群")
        store.create_user_room("受邀值守群")
        with store._connection() as connection:
            admin_id = str(
                connection.execute(
                    "SELECT user_id FROM web_users WHERE username = 'admin'"
                ).fetchone()[0]
            )
        invitation = store.create_agent_invitation(
            conversation_id="受邀值守群",
            product="claude-code",
            requested_mode="resident",
            adapter_kind="claude-code",
            created_by_web_user_id=admin_id,
        )
        enrollment_token = "enroll_" + ("a" * 48)
        accepted = store.accept_agent_invitation(
            invitation_token=str(invitation["invitation_token"]),
            product="claude-code",
            username="受邀值守者",
            signature="只按固定身份处理通知。",
            roles=["reviewer"],
            capabilities=["history"],
            enrollment_token=enrollment_token,
        )
        sender = store.register_agent_session(
            product="sender",
            username="提醒者",
            signature="发送测试提醒。",
            conversation_id="受邀值守群",
        )
        sent = store.send(
            authorized_session_id=str(sender["session_id"]),
            sender_participant_id=str(sender["participant_id"]),
            conversation_id="受邀值守群",
            body_text="请检查自动登记是否绑定到正确 Agent。",
            audience_kind="participant",
            audience_value=str(accepted["participant_id"]),
        )
        enrollment_file = tmp_path / "enrollment.token"
        enrollment_file.write_text(enrollment_token + "\n", encoding="utf-8")
        enrollment_file.chmod(0o600)

        no_secret_environment = {
            "AGENT_BRIDGE_AUTO_REGISTER": "1",
            "AGENT_BRIDGE_USERNAME": "中央值守者",
            "AGENT_BRIDGE_SIGNATURE": "中央开放注册身份。",
            "AGENT_BRIDGE_CONVERSATION_ID": "中央值守群",
            "AGENT_BRIDGE_ROLES": "reviewer",
            "AGENT_BRIDGE_CAPABILITIES": "history",
            "AGENT_BRIDGE_ENROLLMENT_TOKEN": "",
            "AGENT_BRIDGE_ENROLLMENT_TOKEN_FILE": "",
            "AGENT_BRIDGE_REGISTRATION_SECRET": "",
            "AGENT_BRIDGE_REGISTRATION_SECRET_FILE": "",
        }
        with bridge_server(database) as server_url:
            async with mcp_client(
                server_url,
                "codex",
                extra_env=no_secret_environment,
            ) as central:
                central_wait = payload(
                    await central.call_tool(
                        "agent_wait",
                        {"wait_seconds": 0},
                    )
                )
                assert central_wait["messages"] == []

            async with mcp_client(
                server_url,
                "claude-code",
                extra_env={
                    **no_secret_environment,
                    "AGENT_BRIDGE_USERNAME": "受邀值守者",
                    "AGENT_BRIDGE_SIGNATURE": "只按固定身份处理通知。",
                    # Enrollment remains authoritative after a room rename, even
                    # while an older local service still carries the former name.
                    "AGENT_BRIDGE_CONVERSATION_ID": "本地旧聊天室名",
                    "AGENT_BRIDGE_ENROLLMENT_TOKEN_FILE": str(enrollment_file),
                    "AGENT_BRIDGE_CONNECTOR_ID": str(accepted["connector_id"]),
                },
            ) as resident:
                received = payload(
                    await resident.call_tool(
                        "agent_wait",
                        {"wait_seconds": 0},
                    )
                )
                assert received["messages"][0]["message_id"] == sent["message_id"]

        with store._connection() as connection:
            central_identity = connection.execute(
                "SELECT participant_id FROM participants WHERE client_type = ?",
                ("codex-中央值守者",),
            ).fetchone()
            connector_sessions = connection.execute(
                "SELECT participant_id FROM agent_sessions WHERE connector_id = ?",
                (accepted["connector_id"],),
            ).fetchall()
        assert central_identity is not None
        assert len(connector_sessions) == 2
        assert {str(row["participant_id"]) for row in connector_sessions} == {
            str(accepted["participant_id"])
        }

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
            async with mcp_client(
                server_url,
                "future-mcp-agent",
                extra_env={"AGENT_BRIDGE_ALLOW_DIRECT_REGISTRATION": "1"},
            ) as mcp_session:
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
