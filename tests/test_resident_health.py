from __future__ import annotations

import plistlib
from pathlib import Path

from agent_bridge import resident_health


def _service(
    path: Path,
    *,
    label: str,
    product: str,
    username: str,
    command: str,
    connector_id: str = "",
    conversation_id: str = "",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        plistlib.dumps(
            {
                "Label": label,
                "ProgramArguments": [command],
                "EnvironmentVariables": {
                    "AGENT_BRIDGE_URL": "http://127.0.0.1:8765",
                    "AGENT_BRIDGE_PRODUCT": product,
                    "AGENT_BRIDGE_USERNAME": username,
                    "AGENT_BRIDGE_CONNECTOR_ID": connector_id,
                    "AGENT_BRIDGE_CONVERSATION_ID": conversation_id,
                },
            }
        )
    )


def test_local_resident_snapshot_recognizes_legacy_and_connector_services(
    tmp_path: Path,
    monkeypatch,
) -> None:
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    _service(
        launch_agents / "listener.plist",
        label="example.listener",
        product="codex",
        username="小团子",
        command="/project/bin/agent-bridge-listen",
    )
    _service(
        launch_agents / "worker.plist",
        label="example.worker",
        product="codex",
        username="小团子",
        command="/project/bin/agent-bridge-codex-worker",
    )
    monkeypatch.setattr(resident_health, "_launchd_state", lambda _label: "running")
    monkeypatch.setattr(resident_health, "_launchd_disabled_labels", set)

    snapshot = resident_health.local_resident_snapshot(
        home=tmp_path,
        system_name="Darwin",
        force=True,
    )

    assert snapshot["codex-小团子"]["resident_status"] == "online"
    assert snapshot["codex-小团子"]["listener_running"] is True
    assert snapshot["codex-小团子"]["worker_running"] is True


def test_resident_snapshot_and_repair_are_scoped_to_one_room_connector(
    tmp_path: Path,
    monkeypatch,
) -> None:
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    for room, connector, listener_state, worker_state in (
        ("房间-A", "connector_a", "running", "running"),
        ("房间-B", "connector_b", "running", "missing"),
    ):
        _service(
            launch_agents / f"{connector}.listener.plist",
            label=f"{connector}.listener",
            product="codex",
            username="双群助手",
            command="/project/bin/agent-bridge-listen",
            connector_id=connector,
            conversation_id=room,
        )
        _service(
            launch_agents / f"{connector}.worker.plist",
            label=f"{connector}.worker",
            product="codex",
            username="双群助手",
            command="/project/bin/agent-bridge-codex-worker",
            connector_id=connector,
            conversation_id=room,
        )
    states = {
        "connector_a.listener": "running",
        "connector_a.worker": "running",
        "connector_b.listener": "running",
        "connector_b.worker": "missing",
    }
    commands: list[list[str]] = []
    monkeypatch.setattr(resident_health, "_launchd_state", states.__getitem__)
    monkeypatch.setattr(resident_health, "_launchd_disabled_labels", set)

    def run(command, *, description):
        commands.append(command)
        states["connector_b.worker"] = "running"

    monkeypatch.setattr(resident_health, "_run_launchctl", run)
    monkeypatch.setattr(resident_health.time, "sleep", lambda _seconds: None)

    snapshot = resident_health.local_resident_snapshot(
        home=tmp_path,
        system_name="Darwin",
        force=True,
    )
    assert snapshot["codex-双群助手"]["connectors"]["connector_a"][
        "resident_status"
    ] == "online"
    assert snapshot["codex-双群助手"]["connectors"]["connector_b"][
        "resident_status"
    ] == "degraded"

    repaired = resident_health.repair_known_identity_services(
        "codex-双群助手",
        connector_id="connector_b",
        conversation_id="房间-B",
        home=tmp_path,
        system_name="Darwin",
    )
    assert repaired is not None
    assert repaired["resident_status"] == "online"
    assert commands == [
        [
            "launchctl",
            "bootstrap",
            f"gui/{resident_health.os.getuid()}",
            str(launch_agents / "connector_b.worker.plist"),
        ]
    ]
    assert resident_health.repair_known_identity_services(
        "codex-双群助手",
        connector_id="connector_missing",
        conversation_id="房间-B",
        home=tmp_path,
        system_name="Darwin",
    ) is None


def test_launchd_disabled_labels_accepts_native_macos_output(monkeypatch) -> None:
    class Completed:
        returncode = 0
        stdout = '''
disabled services = {
    "example.enabled" => enabled
    "example.disabled" => disabled
    "example.legacy-boolean" => true
}
'''

    monkeypatch.setattr(
        resident_health.subprocess,
        "run",
        lambda *_args, **_kwargs: Completed(),
    )

    assert resident_health._launchd_disabled_labels() == {
        "example.disabled",
        "example.legacy-boolean",
    }


def test_repair_known_identity_bootstraps_only_missing_service(
    tmp_path: Path,
    monkeypatch,
) -> None:
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    _service(
        launch_agents / "listener.plist",
        label="example.listener",
        product="claude-code",
        username="助手",
        command="/project/bin/agent-bridge-listen",
    )
    _service(
        launch_agents / "worker.plist",
        label="example.worker",
        product="claude-code",
        username="助手",
        command="/project/bin/agent-bridge-supervisor",
    )
    states = {"example.listener": "running", "example.worker": "missing"}
    commands: list[list[str]] = []
    monkeypatch.setattr(
        resident_health,
        "_launchd_state",
        lambda label: states[label],
    )
    monkeypatch.setattr(resident_health, "_launchd_disabled_labels", set)

    def run(command, *, description):
        commands.append(command)
        states["example.worker"] = "running"

    monkeypatch.setattr(resident_health, "_run_launchctl", run)
    monkeypatch.setattr(resident_health.time, "sleep", lambda _seconds: None)

    repaired = resident_health.repair_known_identity_services(
        "claude-code-助手",
        home=tmp_path,
        system_name="Darwin",
    )

    assert repaired is not None
    assert repaired["resident_status"] == "online"
    assert repaired["repaired_services"] == ["example.worker"]
    assert commands == [
        [
            "launchctl",
            "bootstrap",
            f"gui/{resident_health.os.getuid()}",
            str(launch_agents / "worker.plist"),
        ]
    ]


def test_background_repair_respects_disabled_worker_until_explicit_repair(
    tmp_path: Path,
    monkeypatch,
) -> None:
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    _service(
        launch_agents / "listener.plist",
        label="example.listener",
        product="codex",
        username="维护中",
        command="/project/bin/agent-bridge-listen",
    )
    _service(
        launch_agents / "worker.plist",
        label="example.worker",
        product="codex",
        username="维护中",
        command="/project/bin/agent-bridge-codex-worker",
    )
    disabled = {"example.worker"}
    states = {"example.listener": "running", "example.worker": "missing"}
    commands: list[list[str]] = []
    monkeypatch.setattr(resident_health, "_launchd_state", states.__getitem__)
    monkeypatch.setattr(
        resident_health,
        "_launchd_disabled_labels",
        lambda: set(disabled),
    )

    def run(command, *, description):
        commands.append(command)
        if command[1] == "enable":
            disabled.discard("example.worker")
        elif command[1] == "bootstrap":
            states["example.worker"] = "running"

    monkeypatch.setattr(resident_health, "_run_launchctl", run)
    monkeypatch.setattr(resident_health.time, "sleep", lambda _seconds: None)

    paused = resident_health.repair_known_identity_services(
        "codex-维护中",
        home=tmp_path,
        system_name="Darwin",
    )
    assert paused is not None
    assert paused["resident_status"] == "degraded"
    assert commands == []

    repaired = resident_health.repair_known_identity_services(
        "codex-维护中",
        home=tmp_path,
        system_name="Darwin",
        enable_disabled=True,
    )
    assert repaired is not None
    assert repaired["resident_status"] == "online"
    assert commands == [
        [
            "launchctl",
            "enable",
            f"gui/{resident_health.os.getuid()}/example.worker",
        ],
        [
            "launchctl",
            "bootstrap",
            f"gui/{resident_health.os.getuid()}",
            str(launch_agents / "worker.plist"),
        ],
    ]
