from __future__ import annotations

import json
import os
import platform
import plistlib
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .connector import configure_resident_connector
from .tui_binding import NativeTuiError, load_native_tui_binding
from .validation import client_identity


_CACHE_LOCK = threading.Lock()
_CACHE_AT = 0.0
_CACHE_VALUE: dict[str, dict[str, Any]] = {}
# Page-level presence already refreshes from the durable database and SSE.
# Avoid repeating the comparatively expensive launchd/systemd filesystem scan
# on every fast room switch; the maintenance loop still forces a fresh probe.
_CACHE_SECONDS = 15.0


def split_supported_identity(value: str) -> tuple[str, str] | None:
    identity = client_identity(value)
    for product in ("claude-code", "codex"):
        prefix = f"{product}-"
        if identity.startswith(prefix) and len(identity) > len(prefix):
            return product, identity[len(prefix) :]
    return None


def _launchd_state(label: str) -> str:
    try:
        completed = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            shell=False,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if completed.returncode != 0:
        return "missing"
    return "running" if "state = running" in completed.stdout else "loaded"


def _launchd_disabled_labels() -> set[str]:
    try:
        completed = subprocess.run(
            ["launchctl", "print-disabled", f"gui/{os.getuid()}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            shell=False,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        # If launchd itself is unavailable, avoid overriding an operator's
        # possible maintenance pause until the next successful health scan.
        return set()
    if completed.returncode != 0:
        return set()
    return {
        match.group(1)
        for match in re.finditer(
            r'"([^"\n]+)"\s*=>\s*(?:disabled|true|1)',
            completed.stdout,
            flags=re.IGNORECASE,
        )
    }


def _launchd_services(home: Path) -> dict[str, dict[str, Any]]:
    launch_agents = home / "Library" / "LaunchAgents"
    services: dict[str, dict[str, Any]] = {}
    if not launch_agents.is_dir():
        return services
    disabled_labels = _launchd_disabled_labels()
    for plist_path in sorted(launch_agents.glob("*.plist")):
        try:
            payload = plistlib.loads(plist_path.read_bytes())
        except (OSError, plistlib.InvalidFileException, ValueError):
            continue
        environment = payload.get("EnvironmentVariables")
        arguments = payload.get("ProgramArguments")
        label = str(payload.get("Label") or "").strip()
        if not isinstance(environment, dict) or not isinstance(arguments, list) or not label:
            continue
        product = str(environment.get("AGENT_BRIDGE_PRODUCT") or "").strip()
        username = str(environment.get("AGENT_BRIDGE_USERNAME") or "").strip()
        if not product or not username or not environment.get("AGENT_BRIDGE_URL"):
            continue
        command = " ".join(str(item) for item in arguments)
        if "agent-bridge" not in command and "agent_bridge" not in command:
            continue
        try:
            identity = client_identity(f"{product}-{username}")
        except ValueError:
            continue
        executable = Path(str(arguments[0])).name if arguments else ""
        if executable in {"agent-bridge-listen", "agent_bridge.listener"} or (
            "agent_bridge.listener" in command
        ):
            kind = "listener"
        elif executable in {
            "agent-bridge-task-worker",
            "agent_bridge.task_worker",
        } or "agent_bridge.task_worker" in command:
            kind = "task"
        else:
            kind = "worker"
        launchd_state = _launchd_state(label)
        service = {
            "label": label,
            "path": str(plist_path),
            "kind": kind,
            "connector_id": str(
                environment.get("AGENT_BRIDGE_CONNECTOR_ID") or ""
            ).strip(),
            "conversation_id": str(
                environment.get("AGENT_BRIDGE_CONVERSATION_ID") or ""
            ).strip(),
            "component": str(
                environment.get("AGENT_BRIDGE_COMPONENT") or ""
            ).strip().lower(),
            "state": "disabled" if label in disabled_labels else launchd_state,
            "launchd_state": launchd_state,
        }
        entry = services.setdefault(
            identity,
            {
                "client_type": identity,
                "adapter_kind": product,
                "services": [],
            },
        )
        entry["services"].append(service)
    return services


def local_resident_snapshot(
    *,
    home: Path | None = None,
    system_name: str | None = None,
    force: bool = False,
) -> dict[str, dict[str, Any]]:
    global _CACHE_AT, _CACHE_VALUE
    current_home = (home or Path.home()).expanduser().resolve()
    host_system = system_name or platform.system()
    now = time.monotonic()
    cacheable = home is None and system_name is None
    with _CACHE_LOCK:
        if cacheable and not force and now - _CACHE_AT < _CACHE_SECONDS:
            return {key: dict(value) for key, value in _CACHE_VALUE.items()}
    raw = _launchd_services(current_home) if host_system == "Darwin" else {}
    snapshot: dict[str, dict[str, Any]] = {}
    for identity, detail in raw.items():
        services = list(detail["services"])
        listeners = [item for item in services if item["kind"] == "listener"]
        workers = [item for item in services if item["kind"] == "worker"]
        tasks = [item for item in services if item["kind"] == "task"]
        listener_running = any(item["state"] == "running" for item in listeners)
        worker_running = any(item["state"] == "running" for item in workers)
        task_running = any(item["state"] == "running" for item in tasks)
        task_component_ready = any(
            item.get("component") == "task" for item in tasks
        )
        snapshot[identity] = {
            **detail,
            "listener_running": listener_running,
            "worker_running": worker_running,
            "task_configured": bool(tasks),
            "task_running": task_running,
            "task_component_ready": task_component_ready,
            "resident_status": (
                "online"
                if listener_running and worker_running
                else "degraded"
                if listeners or workers
                else "none"
            ),
        }
        connectors: dict[str, dict[str, Any]] = {}
        for service in services:
            connector_id = str(service.get("connector_id") or "")
            conversation_id = str(service.get("conversation_id") or "")
            key = connector_id or f"legacy:{conversation_id or identity}"
            connector = connectors.setdefault(
                key,
                {
                    "connector_id": connector_id or None,
                    "conversation_id": conversation_id or None,
                    "services": [],
                },
            )
            connector["services"].append(service)
        for connector in connectors.values():
            connector_listeners = [
                item for item in connector["services"] if item["kind"] == "listener"
            ]
            connector_workers = [
                item for item in connector["services"] if item["kind"] == "worker"
            ]
            connector_tasks = [
                item for item in connector["services"] if item["kind"] == "task"
            ]
            connector["listener_running"] = any(
                item["state"] == "running" for item in connector_listeners
            )
            connector["worker_running"] = any(
                item["state"] == "running" for item in connector_workers
            )
            connector["task_running"] = any(
                item["state"] == "running" for item in connector_tasks
            )
            connector["task_configured"] = bool(connector_tasks)
            connector["task_component_ready"] = any(
                item.get("component") == "task" for item in connector_tasks
            )
            connector["resident_status"] = (
                "online"
                if connector["listener_running"] and connector["worker_running"]
                else "degraded"
                if connector_listeners or connector_workers
                else "none"
            )
        snapshot[identity]["connectors"] = connectors
    if cacheable:
        with _CACHE_LOCK:
            _CACHE_AT = now
            _CACHE_VALUE = snapshot
    return {key: dict(value) for key, value in snapshot.items()}


def _run_launchctl(command: list[str], *, description: str) -> None:
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"{description}失败") from exc
    if completed.returncode != 0:
        raise RuntimeError(f"{description}失败")


def repair_known_identity_services(
    client_type: str,
    *,
    connector_id: str | None = None,
    conversation_id: str | None = None,
    home: Path | None = None,
    system_name: str | None = None,
    enable_disabled: bool = False,
) -> dict[str, Any] | None:
    current_home = (home or Path.home()).expanduser().resolve()
    host_system = system_name or platform.system()
    before = local_resident_snapshot(
        home=current_home,
        system_name=host_system,
        force=True,
    ).get(client_type)
    if before is None:
        return None
    selected_services = list(before["services"])
    if connector_id:
        selected_services = [
            service
            for service in selected_services
            if str(service.get("connector_id") or "") == connector_id
        ]
    if conversation_id:
        selected_services = [
            service
            for service in selected_services
            if str(service.get("conversation_id") or "") == conversation_id
        ]
    if not selected_services:
        return None
    if host_system != "Darwin":
        return room_resident_detail(
            {client_type: before},
            client_type=client_type,
            connector_id=connector_id,
            conversation_id=conversation_id,
        ) or before
    domain = f"gui/{os.getuid()}"
    repaired: list[str] = []
    for service in selected_services:
        if service["state"] == "running":
            continue
        label = str(service["label"])
        state = str(service["state"])
        if state == "disabled":
            if not enable_disabled:
                continue
            _run_launchctl(
                ["launchctl", "enable", f"{domain}/{label}"],
                description=f"启用值守服务 {label}",
            )
            state = str(service.get("launchd_state") or "missing")
        if state == "running":
            repaired.append(label)
            continue
        if state == "missing":
            _run_launchctl(
                ["launchctl", "bootstrap", domain, str(service["path"])],
                description=f"启动值守服务 {label}",
            )
        else:
            _run_launchctl(
                ["launchctl", "kickstart", "-k", f"{domain}/{label}"],
                description=f"恢复值守服务 {label}",
            )
        repaired.append(label)
    time.sleep(0.15)
    after = local_resident_snapshot(
        home=current_home,
        system_name=host_system,
        force=True,
    ).get(client_type, before)
    if connector_id:
        selected = (after.get("connectors") or {}).get(connector_id)
        if selected is not None:
            return {**selected, "repaired_services": repaired}
    if conversation_id:
        selected = next(
            (
                detail
                for detail in (after.get("connectors") or {}).values()
                if detail.get("conversation_id") == conversation_id
            ),
            None,
        )
        if selected is not None:
            return {**selected, "repaired_services": repaired}
    if connector_id or conversation_id:
        return None
    return {**after, "repaired_services": repaired}


def room_resident_detail(
    snapshot: dict[str, dict[str, Any]],
    *,
    client_type: str,
    connector_id: str | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any] | None:
    """Project one identity snapshot onto one connector/room seat."""

    identity = snapshot.get(client_type)
    if identity is None:
        return None
    connectors = identity.get("connectors") or {}
    if connector_id:
        detail = connectors.get(connector_id)
        if detail is None:
            return None
        if conversation_id and detail.get("conversation_id") != conversation_id:
            return None
        return detail
    if conversation_id:
        for detail in connectors.values():
            if detail.get("conversation_id") == conversation_id:
                return detail
    return None


def local_connector_template(
    client_type: str,
    *,
    home: Path | None = None,
    system_name: str | None = None,
) -> dict[str, Any] | None:
    """Read non-secret local defaults for cloning one identity into another room."""

    current_home = (home or Path.home()).expanduser().resolve()
    host_system = system_name or platform.system()
    connector_root = (
        current_home / "Library" / "Application Support" / "AgentBridge" / "connectors"
        if host_system == "Darwin"
        else current_home / ".local" / "state" / "agent-bridge" / "connectors"
    )
    if not connector_root.is_dir():
        return None
    candidates: list[tuple[float, dict[str, Any]]] = []
    for manifest_path in connector_root.glob("connector_*/connector.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_identity = client_identity(
                f"{manifest['product']}-{manifest['username']}"
            )
            modified_at = manifest_path.stat().st_mtime
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if manifest_identity != client_type:
            continue
        candidates.append(
            (
                modified_at,
                {
                    "product": str(manifest["product"]),
                    "username": str(manifest["username"]),
                    "signature": str(manifest.get("signature") or ""),
                    "roles": list(manifest.get("roles") or []),
                    "capabilities": list(manifest.get("capabilities") or []),
                    "workspace_path": str(manifest.get("workspace_path") or ""),
                    "adapter_kind": str(manifest.get("adapter_kind") or ""),
                },
            )
        )
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def configure_existing_connector_from_disk(
    client_type: str,
    *,
    connector_id: str | None = None,
    conversation_id: str | None = None,
    home: Path | None = None,
    system_name: str | None = None,
    activate_task_only: bool = False,
) -> dict[str, Any] | None:
    current_home = (home or Path.home()).expanduser().resolve()
    host_system = system_name or platform.system()
    if host_system == "Darwin":
        connector_root = (
            current_home / "Library" / "Application Support" / "AgentBridge" / "connectors"
        )
    else:
        connector_root = current_home / ".local" / "state" / "agent-bridge" / "connectors"
    if not connector_root.is_dir():
        return None
    for manifest_path in sorted(connector_root.glob("connector_*/connector.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_identity = client_identity(
                f"{manifest['product']}-{manifest['username']}"
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if manifest_identity != client_type:
            continue
        if connector_id and str(manifest.get("connector_id") or "") != connector_id:
            continue
        if (
            conversation_id
            and str(manifest.get("conversation_id") or "") != conversation_id
        ):
            continue
        enrollment_file = manifest_path.parent / "enrollment.token"
        try:
            enrollment_token = enrollment_file.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        native_binding = None
        binding_file = manifest_path.parent / "tui-binding.json"
        if binding_file.exists():
            try:
                native_binding = load_native_tui_binding(binding_file)
            except NativeTuiError:
                continue
        result = configure_resident_connector(
            connector_id=str(manifest["connector_id"]),
            enrollment_token=enrollment_token,
            bridge_url=str(manifest["bridge_url"]),
            trusted_http_host=(
                str(manifest.get("trusted_http_host") or "").strip() or None
            ),
            product=str(manifest["product"]),
            username=str(manifest["username"]),
            signature=str(manifest["signature"]),
            conversation_id=str(manifest["conversation_id"]),
            adapter_kind=str(manifest["adapter_kind"]),
            requested_mode=str(manifest["requested_mode"]),
            tui_adapter_kind=(
                native_binding.adapter_kind if native_binding is not None else None
            ),
            tui_endpoint_id=(
                native_binding.endpoint_id if native_binding is not None else None
            ),
            tui_native_session_id=(
                native_binding.native_session_id if native_binding is not None else None
            ),
            tui_capabilities=(
                list(native_binding.capabilities) if native_binding is not None else None
            ),
            tui_transport=(
                native_binding.transport if native_binding is not None else None
            ),
            roles=list(manifest.get("roles") or []),
            capabilities=list(manifest.get("capabilities") or []),
            workspace_path=str(manifest.get("workspace_path") or ""),
            execution_source_thread_id=str(
                manifest.get("execution_source_thread_id") or ""
            )
            or None,
            home=current_home,
            system_name=host_system,
            activate=True,
            activate_task_only=activate_task_only,
        )
        local_resident_snapshot(force=True)
        return result.public_payload()
    return None
