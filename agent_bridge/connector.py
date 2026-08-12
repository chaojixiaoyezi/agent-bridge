from __future__ import annotations

import hashlib
import json
import os
import platform
import plistlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .validation import (
    agent_username,
    alias,
    conversation_id as validate_conversation_id,
    opaque_id,
    string_tokens,
    token,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUPPORTED_RESIDENT_ADAPTERS = {
    "codex": "codex",
    "claude-code": "claude-code",
}


class ConnectorSetupError(RuntimeError):
    pass


@dataclass(frozen=True)
class ConnectorSetupResult:
    status: str
    platform: str
    adapter_kind: str
    connector_id: str
    state_directory: str
    listener_service: str | None
    worker_service: str | None
    detail: str

    def public_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "platform": self.platform,
            "adapter_kind": self.adapter_kind,
            "connector_id": self.connector_id,
            "state_directory": self.state_directory,
            "listener_service": self.listener_service,
            "worker_service": self.worker_service,
            "detail": self.detail,
        }


def adapter_kind_for_product(product: str) -> str:
    normalized = token(product, field="product_name").casefold()
    return SUPPORTED_RESIDENT_ADAPTERS.get(normalized, "manual")


def _validated_bridge_url(value: str) -> str:
    normalized = str(value or "").strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConnectorSetupError("Bridge URL must be an http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConnectorSetupError("Bridge URL cannot contain credentials or query data")
    if parsed.scheme == "http" and parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ConnectorSetupError("remote resident connectors require HTTPS")
    return normalized


def _state_root(home: Path, system_name: str) -> Path:
    override = os.environ.get("AGENT_BRIDGE_CONNECTOR_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if system_name == "Darwin":
        return home / "Library" / "Application Support" / "AgentBridge" / "connectors"
    return home / ".local" / "state" / "agent-bridge" / "connectors"


def validate_connector_preflight(
    *,
    bridge_url: str,
    workspace_path: str | None,
) -> tuple[str, Path]:
    """Validate local inputs before this Agent accepts an invitation."""

    normalized_url = _validated_bridge_url(bridge_url)
    workspace = (
        Path(workspace_path).expanduser().resolve()
        if str(workspace_path or "").strip()
        else PROJECT_ROOT
    )
    if not workspace.is_dir():
        raise ConnectorSetupError("Agent workspace does not exist")
    return normalized_url, workspace


def _atomic_private_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_bytes(data)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _service_suffix(connector_id: str) -> str:
    digest = hashlib.sha256(connector_id.encode("utf-8")).hexdigest()[:16]
    return f"c{digest}"


def _common_environment(
    *,
    bridge_url: str,
    product: str,
    username: str,
    signature: str,
    conversation_id: str,
    roles: tuple[str, ...],
    capabilities: tuple[str, ...],
    enrollment_file: Path,
    connector_id: str,
) -> dict[str, str]:
    return {
        "PYTHONUNBUFFERED": "1",
        "AGENT_BRIDGE_AUTO_REGISTER": "1",
        "AGENT_BRIDGE_URL": bridge_url,
        "AGENT_BRIDGE_PRODUCT": product,
        "AGENT_BRIDGE_CLIENT_TYPE": product,
        "AGENT_BRIDGE_USERNAME": username,
        "AGENT_BRIDGE_SIGNATURE": signature,
        "AGENT_BRIDGE_CONVERSATION_ID": conversation_id,
        "AGENT_BRIDGE_ROLES": ",".join(roles),
        "AGENT_BRIDGE_CAPABILITIES": ",".join(capabilities),
        "AGENT_BRIDGE_ENROLLMENT_TOKEN_FILE": str(enrollment_file),
        "AGENT_BRIDGE_CONNECTOR_ID": connector_id,
    }


def _launchd_plist(
    *,
    label: str,
    program_arguments: list[str],
    environment: dict[str, str],
    stdout_path: Path,
    stderr_path: Path,
) -> bytes:
    return plistlib.dumps(
        {
            "Label": label,
            "ProgramArguments": program_arguments,
            "EnvironmentVariables": environment,
            "RunAtLoad": True,
            "KeepAlive": True,
            "ProcessType": "Background",
            "StandardOutPath": str(stdout_path),
            "StandardErrorPath": str(stderr_path),
        },
        fmt=plistlib.FMT_XML,
        sort_keys=False,
    )


def _run_checked(command: list[str], *, description: str) -> None:
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
        raise ConnectorSetupError(f"{description} failed") from exc
    if completed.returncode != 0:
        raise ConnectorSetupError(f"{description} failed")


def _activate_launchd(services: list[tuple[str, Path]]) -> None:
    domain = f"gui/{os.getuid()}"
    for label, plist_path in services:
        try:
            existing = subprocess.run(
                ["launchctl", "print", f"{domain}/{label}"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ConnectorSetupError(
                f"checking existing connector service {label} failed"
            ) from exc
        if existing.returncode == 0:
            _run_checked(
                ["launchctl", "bootout", f"{domain}/{label}"],
                description=f"stopping existing connector service {label}",
            )
        _run_checked(
            ["launchctl", "bootstrap", domain, str(plist_path)],
            description=f"starting connector service {label}",
        )


def _systemd_quote(value: str) -> str:
    escaped = value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    return '"' + escaped + '"'


def _systemd_unit(
    *,
    description: str,
    program_arguments: list[str],
    environment: dict[str, str],
) -> bytes:
    environment_lines = "\n".join(
        f"Environment={_systemd_quote(f'{key}={value}')}"
        for key, value in sorted(environment.items())
    )
    command = " ".join(_systemd_quote(argument) for argument in program_arguments)
    content = (
        "[Unit]\n"
        f"Description={description}\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"{environment_lines}\n"
        f"ExecStart={command}\n"
        "Restart=always\n"
        "RestartSec=2\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    return content.encode("utf-8")


def _activate_systemd(units: list[Path]) -> None:
    _run_checked(["systemctl", "--user", "daemon-reload"], description="systemd reload")
    _run_checked(
        ["systemctl", "--user", "enable", "--now", *[unit.name for unit in units]],
        description="starting connector services",
    )


def configure_resident_connector(
    *,
    connector_id: str,
    enrollment_token: str,
    bridge_url: str,
    product: str,
    username: str,
    signature: str,
    conversation_id: str,
    adapter_kind: str,
    requested_mode: str,
    roles: list[str] | None = None,
    capabilities: list[str] | None = None,
    workspace_path: str | None = None,
    enable_resident: bool = True,
    home: Path | None = None,
    system_name: str | None = None,
    activate: bool = True,
) -> ConnectorSetupResult:
    connector = opaque_id(connector_id, field="connector_id")
    enrollment = opaque_id(enrollment_token, field="enrollment_token")
    normalized_url, workspace = validate_connector_preflight(
        bridge_url=bridge_url,
        workspace_path=workspace_path,
    )
    normalized_product = token(product, field="product_name")
    normalized_username = agent_username(username)
    normalized_signature = alias(signature, field="signature")
    conversation = validate_conversation_id(conversation_id)
    normalized_roles = tuple(string_tokens(roles, field="roles"))
    normalized_capabilities = tuple(string_tokens(capabilities, field="capabilities"))
    adapter = str(adapter_kind or "").strip().lower()
    if adapter not in {"codex", "claude-code", "manual"}:
        raise ConnectorSetupError("unsupported resident adapter")
    mode = str(requested_mode or "").strip().lower()
    if mode not in {"basic", "resident"}:
        raise ConnectorSetupError("unsupported invitation mode")
    host_system = system_name or platform.system()
    user_home = (home or Path.home()).expanduser().resolve()
    state_directory = _state_root(user_home, host_system) / connector
    state_directory.mkdir(parents=True, exist_ok=True)
    os.chmod(state_directory, 0o700)
    logs_directory = state_directory / "logs"
    logs_directory.mkdir(parents=True, exist_ok=True)
    os.chmod(logs_directory, 0o700)
    enrollment_file = state_directory / "enrollment.token"
    _atomic_private_write(enrollment_file, f"{enrollment}\n".encode("utf-8"))
    common = _common_environment(
        bridge_url=normalized_url,
        product=normalized_product,
        username=normalized_username,
        signature=normalized_signature,
        conversation_id=conversation,
        roles=normalized_roles,
        capabilities=normalized_capabilities,
        enrollment_file=enrollment_file,
        connector_id=connector,
    )
    manifest = {
        "schema_version": 1,
        "connector_id": connector,
        "bridge_url": normalized_url,
        "product": normalized_product,
        "username": normalized_username,
        "signature": normalized_signature,
        "conversation_id": conversation,
        "requested_mode": mode,
        "adapter_kind": adapter,
        "roles": list(normalized_roles),
        "capabilities": list(normalized_capabilities),
        "workspace_path": str(workspace),
        "enrollment_token_file": str(enrollment_file),
    }
    _atomic_private_write(
        state_directory / "connector.json",
        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )

    if not enable_resident or mode != "resident" or adapter == "manual":
        return ConnectorSetupResult(
            status="manual",
            platform=host_system,
            adapter_kind=adapter,
            connector_id=connector,
            state_directory=str(state_directory),
            listener_service=None,
            worker_service=None,
            detail=("基础接入已完成；该产品需要本地启动命令或 webhook 才能自动唤醒。"),
        )

    suffix = _service_suffix(connector)
    queue_database = state_directory / "wake-queue.db"
    cursor_file = state_directory / "listener.cursor"
    thread_file = state_directory / "codex-thread"
    listener_environment = {
        **common,
        "AGENT_BRIDGE_CURSOR_FILE": str(cursor_file),
        "AGENT_BRIDGE_WAKE_POLICY": "all",
        "AGENT_BRIDGE_WAKE_COMMAND_JSON": json.dumps(
            [
                str(PROJECT_ROOT / "bin" / "agent-bridge-supervisor"),
                "enqueue",
                "--database",
                str(queue_database),
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }
    if adapter == "codex":
        worker_arguments = [
            str(PROJECT_ROOT / "bin" / "agent-bridge-codex-worker"),
            "--database",
            str(queue_database),
        ]
        # launchd 的默认 PATH 不包含 Homebrew 或用户本地 bin；优先把安装时
        # 探测到的 Codex 绝对路径交给 worker，PATH 只作为可迁移的后备入口。
        local_bin = str((user_home / ".local" / "bin").expanduser().resolve())
        merged_path = os.pathsep.join(
            [local_bin, "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
        )
        codex_binary = shutil.which("codex")
        worker_environment = {
            **common,
            "AGENT_BRIDGE_AGENT_WAKE_POLICY": "mention",
            "AGENT_BRIDGE_AGENT_WAKE_DEBOUNCE": "3",
            "AGENT_BRIDGE_CODEX_CWD": str(workspace),
            "AGENT_BRIDGE_CODEX_THREAD_STATE_FILE": str(thread_file),
            "AGENT_BRIDGE_CODEX_THREAD_NAME": f"Agent Bridge 值守：{conversation}",
            "AGENT_BRIDGE_MCP_COMMAND": str(PROJECT_ROOT / "bin" / "agent-bridge-mcp"),
            "PATH": merged_path,
        }
        if codex_binary:
            worker_environment["AGENT_BRIDGE_CODEX_BINARY"] = codex_binary
    else:
        worker_arguments = [
            str(PROJECT_ROOT / "bin" / "agent-bridge-supervisor"),
            "run",
            "--database",
            str(queue_database),
            "--adapter-command-json",
            json.dumps(
                [str(PROJECT_ROOT / "bin" / "agent-bridge-claude-wake")],
                separators=(",", ":"),
            ),
            "--wake-policy",
            "mention",
            "--debounce",
            "3",
        ]
        # launchd 默认 PATH 不含 ~/.local/bin，claude 常装在 ~/.local/bin。
        # 显式把用户本地 bin 并入 PATH，并优先注入探测到的 claude 绝对路径，
        # 避免 adapter 报 "Claude Code CLI was not found"。
        local_bin = str((user_home / ".local" / "bin").expanduser().resolve())
        merged_path = os.pathsep.join(
            [local_bin, "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
        )
        claude_binary = shutil.which("claude")
        worker_environment = {
            **common,
            "AGENT_BRIDGE_CLAUDE_CWD": str(workspace),
            "AGENT_BRIDGE_MCP_COMMAND": str(PROJECT_ROOT / "bin" / "agent-bridge-mcp"),
            "PATH": merged_path,
        }
        if claude_binary:
            worker_environment["AGENT_BRIDGE_CLAUDE_BINARY"] = claude_binary

    if host_system == "Darwin":
        launch_agents = user_home / "Library" / "LaunchAgents"
        launch_agents.mkdir(parents=True, exist_ok=True)
        listener_label = f"com.agentbridge.connector.{suffix}.listener"
        worker_label = f"com.agentbridge.connector.{suffix}.worker"
        listener_plist = launch_agents / f"{listener_label}.plist"
        worker_plist = launch_agents / f"{worker_label}.plist"
        _atomic_private_write(
            listener_plist,
            _launchd_plist(
                label=listener_label,
                program_arguments=[str(PROJECT_ROOT / "bin" / "agent-bridge-listen")],
                environment=listener_environment,
                stdout_path=logs_directory / "listener.log",
                stderr_path=logs_directory / "listener.error.log",
            ),
        )
        _atomic_private_write(
            worker_plist,
            _launchd_plist(
                label=worker_label,
                program_arguments=worker_arguments,
                environment=worker_environment,
                stdout_path=logs_directory / "worker.log",
                stderr_path=logs_directory / "worker.error.log",
            ),
        )
        if activate:
            _activate_launchd(
                [(listener_label, listener_plist), (worker_label, worker_plist)]
            )
        return ConnectorSetupResult(
            status="configured",
            platform=host_system,
            adapter_kind=adapter,
            connector_id=connector,
            state_directory=str(state_directory),
            listener_service=listener_label,
            worker_service=worker_label,
            detail="listener 和产品适配器已配置为当前用户的常驻服务。",
        )

    if host_system == "Linux":
        unit_directory = user_home / ".config" / "systemd" / "user"
        unit_directory.mkdir(parents=True, exist_ok=True)
        listener_name = f"agent-bridge-{suffix}-listener.service"
        worker_name = f"agent-bridge-{suffix}-worker.service"
        listener_unit = unit_directory / listener_name
        worker_unit = unit_directory / worker_name
        _atomic_private_write(
            listener_unit,
            _systemd_unit(
                description=f"Agent Bridge listener {connector}",
                program_arguments=[str(PROJECT_ROOT / "bin" / "agent-bridge-listen")],
                environment=listener_environment,
            ),
        )
        _atomic_private_write(
            worker_unit,
            _systemd_unit(
                description=f"Agent Bridge worker {connector}",
                program_arguments=worker_arguments,
                environment=worker_environment,
            ),
        )
        if activate:
            _activate_systemd([listener_unit, worker_unit])
        return ConnectorSetupResult(
            status="configured",
            platform=host_system,
            adapter_kind=adapter,
            connector_id=connector,
            state_directory=str(state_directory),
            listener_service=listener_name,
            worker_service=worker_name,
            detail="listener 和产品适配器已配置为当前用户的 systemd 服务。",
        )

    return ConnectorSetupResult(
        status="manual",
        platform=host_system,
        adapter_kind=adapter,
        connector_id=connector,
        state_directory=str(state_directory),
        listener_service=None,
        worker_service=None,
        detail="当前操作系统暂不支持自动安装；已生成私有连接配置。",
    )
