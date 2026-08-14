from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from agent_bridge.maintenance import (
    MaintenanceError,
    create_snapshot,
    database_diagnostics,
    deploy_viewer,
    parse_launchctl_list,
    rehearse_restore,
    verify_snapshot,
)
from agent_bridge.store import BridgeStore


def _completed(
    args: list[str],
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


def test_snapshot_verify_and_restore_rehearsal_are_non_destructive(
    tmp_path: Path,
) -> None:
    database = tmp_path / "live" / "bridge.db"
    store = BridgeStore(database)
    store.create_user_room("maintenance-room")
    before_bytes = database.read_bytes()
    viewer_plist = tmp_path / "viewer.plist"
    viewer_plist.write_text("viewer-service\n", encoding="utf-8")
    queue_root = tmp_path / "connectors"
    queue_database = queue_root / "connector_one" / "wake-queue.db"
    queue_database.parent.mkdir(parents=True)
    with sqlite3.connect(queue_database) as connection:
        connection.execute("CREATE TABLE wake_events (event_id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO wake_events VALUES ('event_one')")

    snapshot = create_snapshot(
        database=database,
        output_root=tmp_path / "backups",
        viewer_plist=viewer_plist,
        connector_queues_root=queue_root,
        label="v-test",
        repo_root=tmp_path,
    )
    manifest = Path(snapshot["manifest"])
    verification = verify_snapshot(manifest)
    rehearsal = rehearse_restore(manifest, work_root=tmp_path / "rehearsals")

    assert snapshot["artifact_count"] == 3
    assert verification["status"] == "ok"
    assert verification["artifacts"][0]["counts"]["rooms"] == 1
    assert rehearsal["counts_preserved"] is True
    assert rehearsal["live_database_modified"] is False
    assert database.read_bytes() == before_bytes
    assert database_diagnostics(database)["counts"]["rooms"] == 1
    assert manifest.stat().st_mode & 0o777 == 0o600


def test_snapshot_verification_rejects_tampering(tmp_path: Path) -> None:
    database = tmp_path / "bridge.db"
    BridgeStore(database)
    snapshot = create_snapshot(
        database=database,
        output_root=tmp_path / "backups",
        label="tamper-test",
        repo_root=tmp_path,
    )
    manifest_path = Path(snapshot["manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact = manifest_path.parent / manifest["artifacts"][0]["path"]
    with artifact.open("ab") as handle:
        handle.write(b"tampered")

    with pytest.raises(MaintenanceError, match="size changed"):
        verify_snapshot(manifest_path)


def test_parse_launchctl_list_excludes_viewer_and_stopped_jobs() -> None:
    output = "\n".join(
        (
            "123\t0\tcom.agentbridge.connector.one.listener",
            "456\t0\tcom.xiaoyezi.agent-bridge-supervisor",
            "789\t0\tcom.xiaoyezi.agent-bridge-viewer",
            "-\t-15\tcom.agentbridge.connector.stopped.worker",
            "222\t0\tcom.example.unrelated",
        )
    )
    assert parse_launchctl_list(
        output,
        viewer_label="com.xiaoyezi.agent-bridge-viewer",
    ) == {
        "com.agentbridge.connector.one.listener": 123,
        "com.xiaoyezi.agent-bridge-supervisor": 456,
    }


def test_deploy_viewer_requires_health_pid_and_unchanged_agents(
    tmp_path: Path,
) -> None:
    database = tmp_path / "bridge.db"
    BridgeStore(database)
    viewer_prints = iter(("pid = 10\n", "pid = 11\n"))
    calls: list[list[str]] = []

    def run(args):
        args = list(args)
        calls.append(args)
        if args == ["launchctl", "list"]:
            return _completed(
                args,
                stdout="123\t0\tcom.agentbridge.connector.one.listener\n",
            )
        if args[:2] == ["launchctl", "print"]:
            return _completed(args, stdout=next(viewer_prints))
        if args[:3] == ["launchctl", "kickstart", "-k"]:
            return _completed(args)
        raise AssertionError(args)

    result = deploy_viewer(
        database=database,
        viewer_label="com.xiaoyezi.agent-bridge-viewer",
        expected_registration_mode="access_code",
        user_id=501,
        command_runner=run,
        health_reader=lambda _: {
            "status": "ok",
            "web_registration_mode": "access_code",
        },
        sleep=lambda _: None,
    )

    assert result["viewer_pid_before"] == 10
    assert result["viewer_pid_after"] == 11
    assert result["agent_process_count"] == 1
    assert result["agent_processes_unchanged"] is True
    assert [
        "launchctl",
        "kickstart",
        "-k",
        "gui/501/com.xiaoyezi.agent-bridge-viewer",
    ] in calls


def test_deploy_viewer_rejects_agent_pid_changes(tmp_path: Path) -> None:
    database = tmp_path / "bridge.db"
    BridgeStore(database)
    viewer_prints = iter(("pid = 10\n", "pid = 11\n"))
    launch_lists = iter(
        (
            "123\t0\tcom.agentbridge.connector.one.listener\n",
            "124\t0\tcom.agentbridge.connector.one.listener\n",
        )
    )

    def run(args):
        args = list(args)
        if args == ["launchctl", "list"]:
            return _completed(args, stdout=next(launch_lists))
        if args[:2] == ["launchctl", "print"]:
            return _completed(args, stdout=next(viewer_prints))
        return _completed(args)

    with pytest.raises(MaintenanceError, match="Agent launchd PID set changed"):
        deploy_viewer(
            database=database,
            expected_registration_mode="access_code",
            user_id=501,
            command_runner=run,
            health_reader=lambda _: {
                "status": "ok",
                "web_registration_mode": "access_code",
            },
            sleep=lambda _: None,
        )
