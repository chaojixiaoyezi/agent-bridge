from __future__ import annotations

import asyncio
import json
import plistlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent_bridge.claude_adapter as claude_adapter
import agent_bridge.invitation_cli as invitation_cli
import agent_bridge.server as bridge_server
from agent_bridge.connector import (
    configure_claude_channel_artifacts,
    configure_resident_connector,
)
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


def test_task_worker_launcher_imports_package_outside_bridge_checkout(
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        [str(BRIDGE_ROOT / "bin" / "agent-bridge-task-worker"), "--help"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Agent Bridge task executor" in completed.stdout


def test_direct_invitation_cli_accepts_without_mcp_and_configures_connector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accepted_calls: list[dict] = []
    reported_calls: list[tuple[str, dict]] = []

    class FakeClient:
        def __init__(
            self,
            bridge_url,
            *,
            invitation_token,
            trusted_http_host,
        ):
            assert bridge_url == "http://100.79.24.67:8765"
            assert invitation_token == "invite_private"
            assert trusted_http_host == "100.79.24.67"

        def accept_invitation(self, **payload):
            accepted_calls.append(payload)
            return {
                "_enrollment_token": "enroll_private",
                "connector_id": "connector_direct123456",
                "participant_id": "participant_direct123456",
                "conversation_id": "直连群",
                "adapter_kind": "claude-code",
                "requested_mode": "resident",
                "invitation_reusable": False,
            }

        def post(self, path, payload):
            reported_calls.append((path, payload))
            return {"connector": {"setup_status": payload["setup_status"]}}

    setup_calls: list[dict] = []

    def configure(**payload):
        setup_calls.append(payload)
        return SimpleNamespace(
            public_payload=lambda: {
                "status": "configured",
                "platform": "Darwin",
                "adapter_kind": "claude-code",
                "connector_id": "connector_direct123456",
                "state_directory": str(tmp_path / "state"),
                "listener_service": "listener",
                "worker_service": "worker",
                "task_service": "task",
                "detail": "ready",
            }
        )

    monkeypatch.setattr(invitation_cli, "BridgeHttpClient", FakeClient)
    monkeypatch.setattr(invitation_cli, "configure_resident_connector", configure)
    monkeypatch.setattr(
        invitation_cli,
        "_stdin_invitation_token",
        lambda: "invite_private",
    )
    result = invitation_cli.accept_invitation(
        SimpleNamespace(
            bridge_url="http://100.79.24.67:8765",
            product="claude-code",
            username="direct-agent",
            signature="直接接入。",
            workspace=str(tmp_path),
            role=["reviewer"],
            capability=["chat"],
            basic=False,
        )
    )

    assert accepted_calls == [
        {
            "product": "claude-code",
            "username": "direct-agent",
            "signature": "直接接入。",
            "avatar_key": "auto",
            "roles": ["reviewer"],
            "capabilities": ["chat"],
        }
    ]
    assert setup_calls[0]["workspace_path"] == str(tmp_path.resolve())
    assert setup_calls[0]["trusted_http_host"] == "100.79.24.67"
    assert setup_calls[0]["enable_resident"] is True
    assert reported_calls[0][0] == "/agent/connector/setup"
    assert result["invitation_accepted"] is True
    assert result["invitation_consumed"] is True
    assert result["resident_setup"]["status"] == "configured"


def test_direct_invitation_cli_allows_https_remote_but_rejects_remote_http() -> None:
    assert invitation_cli._supported_bridge_url("https://bridge.example.test/") == (
        "https://bridge.example.test"
    )
    with pytest.raises(invitation_cli.InvitationCliError, match="requires HTTPS"):
        invitation_cli._supported_bridge_url("http://bridge.example.test")
    assert invitation_cli._supported_bridge_url(
        "http://100.79.24.67:8765/",
        trusted_http_host="100.79.24.67",
    ) == "http://100.79.24.67:8765"


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
    task_path = launch_agents / f"{result.task_service}.plist"
    listener = plistlib.loads(listener_path.read_bytes())
    worker = plistlib.loads(worker_path.read_bytes())
    task = plistlib.loads(task_path.read_bytes())
    serialized = "".join(
        path.read_text(encoding="utf-8")
        for path in (listener_path, worker_path, task_path)
    )
    assert "enroll_private-test-token" not in serialized
    assert listener["EnvironmentVariables"][
        "AGENT_BRIDGE_ENROLLMENT_TOKEN_FILE"
    ] == str(enrollment_file)
    assert listener["EnvironmentVariables"]["AGENT_BRIDGE_AUTO_REGISTER"] == "1"
    assert listener["EnvironmentVariables"]["AGENT_BRIDGE_CONNECTOR_ID"] == (
        "connector_1234567890abcdef"
    )
    assert listener["EnvironmentVariables"]["AGENT_BRIDGE_WAKE_POLICY"] == "all"
    assert listener["EnvironmentVariables"][
        "AGENT_BRIDGE_DIAGNOSTIC_QUEUE_FILE"
    ] == str(state_directory / "wake-queue.db")
    assert worker["EnvironmentVariables"]["AGENT_BRIDGE_AUTO_REGISTER"] == "1"
    assert worker["EnvironmentVariables"]["AGENT_BRIDGE_AGENT_WAKE_POLICY"] == (
        "mention"
    )
    assert worker["EnvironmentVariables"]["AGENT_BRIDGE_CODEX_BINARY"] == (
        "/Applications/Codex.app/Contents/Resources/codex"
    )
    assert "/opt/homebrew/bin" in worker["EnvironmentVariables"]["PATH"].split(":")
    assert worker["ProgramArguments"][0].endswith("agent-bridge-codex-worker")
    assert task["ProgramArguments"][0].endswith("agent-bridge-task-worker")
    assert task["EnvironmentVariables"]["AGENT_BRIDGE_TASK_ADAPTER"] == "codex"
    assert json.loads(manifest_file.read_text(encoding="utf-8"))["schema_version"] == 3


def test_tailnet_invitation_pin_is_persisted_for_every_resident_component(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "agent_bridge.connector.shutil.which",
        lambda name: "/usr/local/bin/codex" if name == "codex" else None,
    )
    result = configure_resident_connector(
        connector_id="connector_tailnet123456",
        enrollment_token="enroll_tailnet-private-token",
        bridge_url="http://100.79.24.67:8765",
        trusted_http_host="100.79.24.67",
        product="codex",
        username="tailnet-agent",
        signature="通过邀请自动接入。",
        conversation_id="私网邀请测试群",
        adapter_kind="codex",
        requested_mode="resident",
        workspace_path=str(tmp_path),
        home=tmp_path,
        system_name="Darwin",
        activate=False,
    )

    state = Path(result.state_directory)
    manifest = json.loads((state / "connector.json").read_text(encoding="utf-8"))
    assert manifest["trusted_http_host"] == "100.79.24.67"
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    service_files = list(launch_agents.glob("*.plist"))
    assert len(service_files) == 3
    for service_file in service_files:
        service = plistlib.loads(service_file.read_bytes())
        assert service["EnvironmentVariables"][
            "AGENT_BRIDGE_TRUSTED_HTTP_HOST"
        ] == "100.79.24.67"


def test_tailnet_connector_without_an_invitation_pin_fails_before_writing_state(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConnectorSetupError, match="invitation-pinned"):
        configure_resident_connector(
            connector_id="connector_untrusted1234",
            enrollment_token="enroll_untrusted-private-token",
            bridge_url="http://100.79.24.67:8765",
            product="codex",
            username="untrusted-agent",
            signature="不应写入。",
            conversation_id="私网邀请测试群",
            adapter_kind="codex",
            requested_mode="resident",
            workspace_path=str(tmp_path),
            home=tmp_path,
            system_name="Darwin",
            activate=False,
        )
    assert not (
        tmp_path / "Library" / "Application Support" / "AgentBridge"
    ).exists()


def test_claude_connector_installs_generic_exact_session_channel(
    tmp_path: Path,
) -> None:
    result = configure_resident_connector(
        connector_id="connector_claudechannel123",
        enrollment_token="enroll_claude-channel-private-token",
        bridge_url="http://127.0.0.1:8765",
        product="claude-code",
        username="青禾",
        signature="真实会话值守。",
        conversation_id="工具修改的聊天室",
        adapter_kind="claude-code",
        requested_mode="resident",
        roles=["developer"],
        capabilities=["chat"],
        workspace_path=str(tmp_path),
        home=tmp_path,
        system_name="Darwin",
        activate=False,
    )

    state = Path(result.state_directory)
    manifest = json.loads((state / "connector.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 4
    channel = manifest["claude_channel"]
    assert channel["tui_endpoint_id"].startswith("claude-c")
    assert channel["selector"].startswith("server:agent-bridge-c")
    assert result.launch_command == tuple(channel["launch_command"])
    assert result.public_payload()["launch_command"] == channel["launch_command"]

    plugin = state / "claude-plugin"
    plugin_manifest = json.loads(
        (plugin / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    mcp = json.loads(
        (state / "claude-channel.mcp.json").read_text(encoding="utf-8")
    )
    hooks = json.loads((plugin / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    assert plugin_manifest["name"].startswith("agent-bridge-c")
    assert plugin_manifest["author"] == {"name": "Agent Bridge"}
    server = channel["server_name"]
    assert mcp["mcpServers"][server]["command"].endswith(
        "agent-bridge-claude-channel"
    )
    assert set(hooks["hooks"]) == {"SessionStart", "SessionEnd"}
    serialized_plugin = json.dumps(
        {"manifest": plugin_manifest, "mcp": mcp, "hooks": hooks},
        ensure_ascii=False,
    )
    assert "enroll_claude-channel-private-token" not in serialized_plugin

    launch_agents = tmp_path / "Library" / "LaunchAgents"
    before = {
        path.name: path.read_bytes() for path in launch_agents.glob("*.plist")
    }
    upgraded = configure_claude_channel_artifacts(state, home=tmp_path)
    after = {
        path.name: path.read_bytes() for path in launch_agents.glob("*.plist")
    }
    assert upgraded["selector"] == channel["selector"]
    assert after == before


@pytest.mark.parametrize(
    "launcher",
    [
        "agent-bridge-claude",
        "agent-bridge-claude-channel",
        "agent-bridge-claude-session-hook",
    ],
)
def test_claude_native_launchers_import_package_outside_checkout(
    tmp_path: Path,
    launcher: str,
) -> None:
    completed = subprocess.run(
        [str(BRIDGE_ROOT / "bin" / launcher), "--help"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert launcher in completed.stdout


def test_connector_refuses_to_overwrite_identity_or_enrollment(
    tmp_path: Path,
) -> None:
    kwargs = {
        "connector_id": "connector_fixed123456",
        "enrollment_token": "enroll_fixed-private-token",
        "bridge_url": "http://127.0.0.1:8765",
        "product": "claude-code",
        "username": "fixed-agent",
        "signature": "固定连接器。",
        "conversation_id": "固定群",
        "adapter_kind": "manual",
        "requested_mode": "basic",
        "workspace_path": str(tmp_path),
        "home": tmp_path,
        "system_name": "Darwin",
        "activate": False,
    }
    configure_resident_connector(**kwargs)

    with pytest.raises(ConnectorSetupError, match="credential differs"):
        configure_resident_connector(
            **{**kwargs, "enrollment_token": "enroll_other-private-token"}
        )
    with pytest.raises(ConnectorSetupError, match="username differs"):
        configure_resident_connector(**{**kwargs, "username": "other-agent"})


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


def test_opencode_native_tui_connector_installs_shared_endpoint_workers(
    tmp_path: Path,
) -> None:
    result = configure_resident_connector(
        connector_id="connector_opencode123456",
        enrollment_token="enroll_opencode-private-token",
        bridge_url="http://127.0.0.1:8765",
        product="opencode",
        username="native-owner",
        signature="真实 TUI 值守。",
        conversation_id="OpenCode群",
        adapter_kind="manual",
        tui_adapter_kind="opencode",
        tui_endpoint_id="tui-opencode-stable",
        tui_native_session_id="opencode-room-session",
        tui_access_mode="read-only-at-install-time",
        tui_capabilities=["steer", "multi-room"],
        tui_transport={
            "kind": "opencode-http",
            "base_url": "http://127.0.0.1:9201",
            "directory": str(tmp_path),
        },
        requested_mode="resident",
        workspace_path=str(tmp_path),
        home=tmp_path,
        system_name="Darwin",
        activate=False,
    )

    assert result.status == "configured"
    assert result.adapter_kind == "opencode"
    state_directory = Path(result.state_directory)
    binding = json.loads(
        (state_directory / "tui-binding.json").read_text(encoding="utf-8")
    )
    assert binding["endpoint_id"] == "tui-opencode-stable"
    assert binding["native_session_id"] == "opencode-room-session"
    assert binding["schema_version"] == 2
    assert "access_mode" not in binding
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    worker = plistlib.loads(
        (launch_agents / f"{result.worker_service}.plist").read_bytes()
    )
    task = plistlib.loads((launch_agents / f"{result.task_service}.plist").read_bytes())
    assert "agent-bridge-tui-wake" in json.dumps(worker["ProgramArguments"])
    assert task["EnvironmentVariables"]["AGENT_BRIDGE_TASK_ADAPTER"] == "opencode"
    assert task["EnvironmentVariables"]["AGENT_BRIDGE_TUI_ENDPOINT_ID"] == (
        "tui-opencode-stable"
    )
    assert "AGENT_BRIDGE_TUI_ACCESS_MODE" not in task["EnvironmentVariables"]
    assert "AGENT_BRIDGE_TUI_ACCESS_MODE" not in worker["EnvironmentVariables"]
    assert (
        task["EnvironmentVariables"]["AGENT_BRIDGE_TUI_LOCK_FILE"]
        == (worker["EnvironmentVariables"]["AGENT_BRIDGE_TUI_LOCK_FILE"])
    )


def test_basic_native_product_can_join_without_installing_a_tui_binding(
    tmp_path: Path,
) -> None:
    result = configure_resident_connector(
        connector_id="connector_basicnative123",
        enrollment_token="enroll_basic-native-private-token",
        bridge_url="http://127.0.0.1:8765",
        product="opencode",
        username="basic-native",
        signature="只做基础接入。",
        conversation_id="基础原生群",
        adapter_kind="manual",
        tui_adapter_kind="opencode",
        requested_mode="basic",
        workspace_path=str(tmp_path),
        home=tmp_path,
        system_name="Darwin",
        activate=False,
    )

    assert result.status == "manual"
    state_directory = Path(result.state_directory)
    assert not (state_directory / "tui-binding.json").exists()
    manifest = json.loads(
        (state_directory / "connector.json").read_text(encoding="utf-8")
    )
    assert manifest["tui_adapter_kind"] == "opencode"
    assert manifest["tui_endpoint_id"] is None


def test_pi_native_tui_connector_installs_private_extension(
    tmp_path: Path,
) -> None:
    relay_directory = tmp_path / "pi-relay"
    relay_directory.mkdir()
    session_file = relay_directory / "room-session.jsonl"
    session_file.touch()
    result = configure_resident_connector(
        connector_id="connector_pi123456",
        enrollment_token="enroll_pi-private-token",
        bridge_url="http://127.0.0.1:8765",
        product="pi",
        username="pi-owner",
        signature="Pi 真实本体。",
        conversation_id="Pi群",
        adapter_kind="manual",
        tui_adapter_kind="pi",
        tui_endpoint_id="tui-pi-stable",
        tui_native_session_id="pi-room-session",
        tui_access_mode="legacy-ignored-value",
        tui_transport={
            "kind": "pi-extension",
            "command_file": str(relay_directory / "commands.jsonl"),
            "event_file": str(relay_directory / "events.jsonl"),
            "session_file": str(session_file),
        },
        requested_mode="resident",
        workspace_path=str(tmp_path),
        home=tmp_path,
        system_name="Darwin",
        activate=False,
    )

    extension = tmp_path / ".pi" / "agent" / "extensions" / "agent-bridge.ts"
    assert (
        extension.read_bytes()
        == (
            Path(__file__).parents[1] / "integrations" / "pi" / "agent-bridge.ts"
        ).read_bytes()
    )
    assert extension.stat().st_mode & 0o777 == 0o600
    binding = json.loads(
        (Path(result.state_directory) / "tui-binding.json").read_text(encoding="utf-8")
    )
    assert binding["schema_version"] == 2
    assert "access_mode" not in binding
    assert binding["transport"]["session_file"] == str(session_file)


def test_task_only_upgrade_does_not_restart_existing_chat_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activated: list[list[tuple[str, Path]]] = []
    monkeypatch.setattr(
        "agent_bridge.connector._activate_launchd",
        lambda services: activated.append(services),
    )
    result = configure_resident_connector(
        connector_id="connector_upgrade123456",
        enrollment_token="enroll_upgrade-private-token",
        bridge_url="http://127.0.0.1:8765",
        product="codex",
        username="平滑升级者",
        signature="聊天室持续在线。",
        conversation_id="平滑群",
        adapter_kind="codex",
        requested_mode="resident",
        workspace_path=str(tmp_path),
        home=tmp_path,
        system_name="Darwin",
        activate=True,
        activate_task_only=True,
    )

    assert len(activated) == 1
    assert activated[0] == [
        (
            str(result.task_service),
            tmp_path / "Library" / "LaunchAgents" / f"{result.task_service}.plist",
        )
    ]


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
    assert len(units) == 3
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
            trusted_http_host=None,
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


def test_untrusted_remote_transport_is_rejected_before_invitation_is_consumed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bridge_server,
        "CONFIG",
        SimpleNamespace(
            server_url="http://100.79.24.67:8765",
            client_type="codex",
            trusted_http_host=None,
        ),
    )

    def must_not_connect():
        raise AssertionError("invitation was consumed before transport preflight")

    monkeypatch.setattr(bridge_server, "get_client", must_not_connect)
    with pytest.raises(ConnectorSetupError, match="invitation-pinned"):
        bridge_server.agent_accept_invitation(
            username="值守者",
            signature="只处理明确通知。",
            workspace_path=str(tmp_path),
        )


def test_connector_preflight_defaults_to_current_tui_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    _url, workspace = bridge_server.validate_connector_preflight(
        bridge_url="http://127.0.0.1:8765",
        workspace_path=None,
    )

    assert workspace == tmp_path.resolve()


def test_agent_wait_keeps_normal_calls_wire_compatible_and_opts_in_compaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict, float]] = []

    class CompletionClient:
        @staticmethod
        def post(path, payload, *, timeout):
            calls.append((path, payload, timeout))
            return {"messages": []}

    monkeypatch.setattr(
        bridge_server,
        "CONFIG",
        SimpleNamespace(maximum_wait_seconds=30),
    )
    monkeypatch.setattr(bridge_server, "get_client", CompletionClient)

    asyncio.run(bridge_server.agent_wait(wait_seconds=0, limit=7))
    asyncio.run(
        bridge_server.agent_wait(
            wait_seconds=0,
            limit=7,
            compact_optional_backlog=True,
            keep_recent_optional=12,
        )
    )

    assert calls == [
        (
            "/agent/wait",
            {"wait_seconds": 0.0, "limit": 7, "auto_claim_roles": True},
            10.0,
        ),
        (
            "/agent/wait",
            {
                "wait_seconds": 0.0,
                "limit": 7,
                "auto_claim_roles": True,
                "compact_optional_backlog": True,
                "keep_recent_optional": 12,
            },
            10.0,
        ),
    ]


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
    wait_result = {
        "backlog": {"required_reply_count": 1},
        "messages": [
            {
                "message_id": "message-42",
                "body_text": "正文不应出现在命令行",
                "delivery": {
                    "priority": "mention",
                    "reasons": ["mention"],
                },
            }
        ],
        "has_more": False,
    }

    class CompletionClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        def post(self, path, payload):
            self.calls.append((path, payload))
            return wait_result

    completion_client = CompletionClient()
    resident_identity: dict = {}

    def make_completion_client(**identity):
        resident_identity.update(identity)
        return completion_client

    monkeypatch.setattr(
        claude_adapter,
        "resident_http_client",
        make_completion_client,
    )

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

    assert resident_identity["connector_component"] == "chat"
    assert captured["shell"] is False
    assert "AGENT_BRIDGE_ENROLLMENT_TOKEN" not in captured["env"]
    command_text = " ".join(captured["command"])
    assert "must-not-leak" not in command_text
    assert "正文不应出现在命令行" not in command_text
    assert "正文不应出现在命令行" in captured["input"]
    assert completion_client.calls == [
        (
            "/agent/wait",
            {
                "wait_seconds": 0,
                "limit": 20,
                "auto_claim_roles": True,
            },
        )
    ]
    assert "--strict-mcp-config" in captured["command"]
    assert "--tools" in captured["command"]
    assert "mcp__agent-bridge__agent_wait" not in captured["command"]
    assert "mcp__agent-bridge__agent_reply" in captured["command"]
    assert "mcp__agent-bridge__agent_request_nickname" in captured["command"]
    assert "mcp__agent-bridge__agent_register" not in captured["command"]
    tools_index = captured["command"].index("--tools") + 1
    assert captured["command"][tools_index] == ""
    assert "Read" not in captured["command"]
    assert "Edit" not in captured["command"]
    assert "Bash" not in captured["command"]
    assert captured["command"][captured["command"].index("--permission-mode") + 1] == (
        "dontAsk"
    )
    settings_index = captured["command"].index("--settings") + 1
    permissions = json.loads(captured["command"][settings_index])["permissions"]
    assert permissions["allow"] == [
        f"mcp__agent-bridge__{tool}" for tool in claude_adapter.MODEL_BRIDGE_TOOLS
    ]
    assert permissions["deny"] == ["Read", "Glob", "Grep", "Edit", "Write", "Bash"]
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
            "AGENT_BRIDGE_COMPONENT": "chat",
        }
    prompt = captured["input"]
    assert "连接器已经从 agent_wait 确定性读取" in prompt
    assert "不要再次调用 agent_wait" in prompt
    system_prompt_index = captured["command"].index("--append-system-prompt") + 1
    system_prompt = captured["command"][system_prompt_index]
    assert "模型运行前确定性读取消息" in system_prompt
    assert "不要创建 cron、定时器、轮询脚本" in system_prompt
    assert "本身已是引用回复也照常调用 agent_reply" in system_prompt
    assert "只使用 Agent Bridge MCP" in system_prompt
    assert "结构化任务执行席位" in system_prompt
    assert "agent_register" not in prompt

    completion_client.calls.clear()
    fallback_runs: list[list[str]] = []

    def incomplete_run(command, **kwargs):
        fallback_runs.append(command)
        output_format = command[command.index("--output-format") + 1]
        prompt = str(kwargs.get("input") or "")
        return SimpleNamespace(
            returncode=0,
            stdout=(
                (
                    "收到，已申请将显示名改为「claude-code-小开发」，等待管理员审批。"
                    if "底层已成功提交昵称申请" in prompt
                    else "我在，已经看到你的消息；可以继续说明需要我协助的内容。"
                )
                if output_format == "text"
                else ""
            ),
            stderr="",
        )

    monkeypatch.setattr(claude_adapter.subprocess, "run", incomplete_run)
    claude_adapter.run_claude(batch)
    assert len(fallback_runs) == 2
    assert fallback_runs[1][fallback_runs[1].index("--output-format") + 1] == "text"
    assert "--mcp-config" not in fallback_runs[1]
    assert completion_client.calls[-2:] == [
        (
            "/agent/wait",
            {
                "wait_seconds": 0,
                "limit": 20,
                "auto_claim_roles": True,
            },
        ),
        (
            "/agent/reply",
            {
                "message_id": "message-42",
                "body": "我在，已经看到你的消息；可以继续说明需要我协助的内容。",
                "refs": [],
                "mentions": [],
            },
        ),
    ]

    completion_client.calls.clear()
    wait_result["messages"][0]["body_text"] = (
        "claude-code-小开发 @claude-code-claude-pi-agent"
    )
    claude_adapter.run_claude(batch)
    assert completion_client.calls == [
        (
            "/agent/wait",
            {
                "wait_seconds": 0,
                "limit": 20,
                "auto_claim_roles": True,
            },
        ),
        (
            "/agent/wait",
            {
                "wait_seconds": 0,
                "limit": 20,
                "auto_claim_roles": True,
            },
        ),
        (
            "/agent/nickname/request",
            {"display_name": "claude-code-小开发"},
        ),
        (
            "/agent/reply",
            {
                "message_id": "message-42",
                "body": "收到，已申请将显示名改为「claude-code-小开发」，等待管理员审批。",
                "refs": [],
                "mentions": [],
            },
        ),
    ]
    wait_result["messages"][0]["body_text"] = "正文不应出现在命令行"

    def wrong_reply_run(command, **kwargs):
        if command[command.index("--output-format") + 1] == "text":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        events = [
            {
                "type": "tool_use",
                "id": "reply-2",
                "name": "mcp__agent-bridge__agent_reply",
                "input": {"message_id": "another-message"},
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
    with pytest.raises(
        claude_adapter.ClaudeAdapterError,
        match="fallback reply generation failed",
    ):
        claude_adapter.run_claude(batch)


def test_claude_adapter_bounds_only_an_explicit_reconnect_backlog(
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
    monkeypatch.setattr(claude_adapter.shutil, "which", lambda _name: "/opt/claude")

    class CompletionClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        def post(self, path, payload):
            self.calls.append((path, payload))
            return {"messages": [], "has_more": False}

    completion_client = CompletionClient()
    monkeypatch.setattr(
        claude_adapter,
        "resident_http_client",
        lambda **_identity: completion_client,
    )

    claude_adapter.run_claude(
        {
            "schema_version": 1,
            "source": "agent-bridge-supervisor",
            "event": "wake_batch",
            "event_count": 1,
            "wake_priority": "normal",
            "contains_backlog_event": True,
        }
    )

    assert completion_client.calls == [
        (
            "/agent/wait",
            {
                "wait_seconds": 0,
                "limit": 20,
                "auto_claim_roles": True,
                "compact_optional_backlog": True,
                "keep_recent_optional": 20,
            },
        )
    ]


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
        "has_more": False,
    }

    def completed_run(command, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout="",
            stderr="",
        )

    captured: dict = {}

    class CompletionClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        def post(self, path, payload):
            self.calls.append((path, payload))
            return wait_result

    completion_client = CompletionClient()

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
    assert completion_client.calls[0][0] == "/agent/wait"

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
