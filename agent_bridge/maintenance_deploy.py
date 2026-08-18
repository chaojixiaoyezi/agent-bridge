"""Viewer-only rolling deployment and production invariants."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .maintenance_common import (
    MaintenanceError,
    _assert_database_healthy,
    _counts_do_not_decrease,
    database_diagnostics,
)


DEFAULT_VIEWER_LABEL = "com.xiaoyezi.agent-bridge-viewer"


DEFAULT_HEALTH_URL = "http://127.0.0.1:8765/api/health"


DEFAULT_DEPLOY_TIMEOUT_SECONDS = 90.0


def parse_launchctl_list(output: str, *, viewer_label: str) -> dict[str, int]:
    processes: dict[str, int] = {}
    for raw_line in output.splitlines():
        parts = raw_line.split(None, 2)
        if len(parts) != 3 or not parts[0].isdigit():
            continue
        label = parts[2].strip()
        compact = label.casefold().replace("-", "")
        if label == viewer_label or "agentbridge" not in compact:
            continue
        processes[label] = int(parts[0])
    return processes


def _run_command(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        check=False,
        capture_output=True,
        text=True,
    )


def _launchd_agent_processes(
    *,
    viewer_label: str,
    command_runner: Callable[
        [Sequence[str]], subprocess.CompletedProcess[str]
    ] = _run_command,
) -> dict[str, int]:
    result = command_runner(["launchctl", "list"])
    if result.returncode != 0:
        raise MaintenanceError(
            f"cannot list launchd services: {result.stderr.strip()}"
        )
    return parse_launchctl_list(result.stdout, viewer_label=viewer_label)


def _launchd_viewer_pid(
    *,
    service_target: str,
    command_runner: Callable[
        [Sequence[str]], subprocess.CompletedProcess[str]
    ] = _run_command,
) -> int | None:
    result = command_runner(["launchctl", "print", service_target])
    if result.returncode != 0:
        return None
    match = re.search(r"^\s*pid\s*=\s*(\d+)\s*$", result.stdout, re.MULTILINE)
    return int(match.group(1)) if match else None


def _read_health(url: str) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=2.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise MaintenanceError(f"health endpoint is not ready: {url}") from exc
    if not isinstance(payload, dict):
        raise MaintenanceError("health endpoint returned a non-object payload")
    return payload


def _validate_deployed_database(
    *,
    baseline: dict[str, Any],
    current: dict[str, Any],
) -> None:
    _assert_database_healthy(current, label="deployed central database")
    if current["user_version"] < baseline["user_version"]:
        raise MaintenanceError("deployment reduced the central schema version")
    if not _counts_do_not_decrease(baseline["counts"], current["counts"]):
        raise MaintenanceError("deployment lost central database rows")


def deploy_viewer(
    *,
    database: str | Path,
    health_url: str = DEFAULT_HEALTH_URL,
    viewer_label: str = DEFAULT_VIEWER_LABEL,
    expected_registration_mode: str | None = None,
    timeout_seconds: float = DEFAULT_DEPLOY_TIMEOUT_SECONDS,
    user_id: int | None = None,
    command_runner: Callable[
        [Sequence[str]], subprocess.CompletedProcess[str]
    ] = _run_command,
    health_reader: Callable[[str], dict[str, Any]] = _read_health,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    if sys.platform != "darwin" and command_runner is _run_command:
        raise MaintenanceError("viewer rolling deployment currently requires macOS")
    baseline_database = database_diagnostics(database)
    _assert_database_healthy(baseline_database, label="central database")
    try:
        baseline_health = health_reader(health_url)
    except MaintenanceError as exc:
        raise MaintenanceError("refusing deployment because preflight health failed") from exc
    if baseline_health.get("status") != "ok":
        raise MaintenanceError("refusing deployment because preflight status is not ok")
    active_registration_mode = str(
        baseline_health.get("web_registration_mode", "")
    )
    required_registration_mode = (
        str(expected_registration_mode).strip()
        if expected_registration_mode is not None
        else active_registration_mode
    )
    if active_registration_mode != required_registration_mode:
        raise MaintenanceError(
            "preflight registration mode differs from the required mode"
        )

    effective_uid = os.getuid() if user_id is None else int(user_id)
    service_target = f"gui/{effective_uid}/{viewer_label}"
    baseline_agents = _launchd_agent_processes(
        viewer_label=viewer_label,
        command_runner=command_runner,
    )
    baseline_viewer_pid = _launchd_viewer_pid(
        service_target=service_target,
        command_runner=command_runner,
    )
    kickstart = command_runner(["launchctl", "kickstart", "-k", service_target])
    if kickstart.returncode != 0:
        raise MaintenanceError(
            f"viewer kickstart failed: {kickstart.stderr.strip()}"
        )

    deadline = time.monotonic() + max(1.0, float(timeout_seconds))
    last_error = "viewer did not become ready"
    deployed_health: dict[str, Any] | None = None
    deployed_viewer_pid: int | None = None
    while time.monotonic() < deadline:
        try:
            candidate_health = health_reader(health_url)
            candidate_pid = _launchd_viewer_pid(
                service_target=service_target,
                command_runner=command_runner,
            )
            if candidate_health.get("status") != "ok":
                last_error = "viewer health status is not ok"
            elif (
                candidate_health.get("web_registration_mode")
                != required_registration_mode
            ):
                last_error = "viewer registration mode changed"
            elif candidate_pid is None:
                last_error = "viewer has no launchd PID"
            elif baseline_viewer_pid is not None and candidate_pid == baseline_viewer_pid:
                last_error = "viewer PID did not roll"
            else:
                deployed_health = candidate_health
                deployed_viewer_pid = candidate_pid
                break
        except MaintenanceError as exc:
            last_error = str(exc)
        sleep(0.5)
    if deployed_health is None or deployed_viewer_pid is None:
        raise MaintenanceError(f"viewer failed its post-deploy gate: {last_error}")

    deployed_agents = _launchd_agent_processes(
        viewer_label=viewer_label,
        command_runner=command_runner,
    )
    if deployed_agents != baseline_agents:
        raise MaintenanceError("Agent launchd PID set changed during viewer deployment")
    current_database = database_diagnostics(database)
    _validate_deployed_database(
        baseline=baseline_database,
        current=current_database,
    )
    return {
        "status": "ok",
        "service": service_target,
        "viewer_pid_before": baseline_viewer_pid,
        "viewer_pid_after": deployed_viewer_pid,
        "agent_process_count": len(deployed_agents),
        "agent_processes_unchanged": True,
        "web_registration_mode": required_registration_mode,
        "database_schema_before": baseline_database["user_version"],
        "database_schema_after": current_database["user_version"],
        "database_counts_preserved": True,
    }
