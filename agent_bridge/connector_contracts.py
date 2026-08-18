"""Connector installation contracts, validation, paths, and private files."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .validation import token


PROJECT_ROOT = Path(__file__).resolve().parents[1]


SUPPORTED_RESIDENT_ADAPTERS = {
    "codex": "codex",
    "claude-code": "claude-code",
}


SUPPORTED_NATIVE_TUI_ADAPTERS = {
    "deepseek": "deepseek-harness",
    "deepseek-harness": "deepseek-harness",
    "dsh": "deepseek-harness",
    "opencode": "opencode",
    "hermes": "hermes",
    "hermes-agent": "hermes",
    "pi": "pi",
    "pi-agent": "pi",
    "qcode": "qwen-code",
    "qwen": "qwen-code",
    "qwen-code": "qwen-code",
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
    task_service: str | None
    detail: str
    launch_command: tuple[str, ...] | None = None

    def public_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "platform": self.platform,
            "adapter_kind": self.adapter_kind,
            "connector_id": self.connector_id,
            "state_directory": self.state_directory,
            "listener_service": self.listener_service,
            "worker_service": self.worker_service,
            "task_service": self.task_service,
            "detail": self.detail,
            "launch_command": (
                list(self.launch_command) if self.launch_command else None
            ),
        }


def adapter_kind_for_product(product: str) -> str:
    normalized = token(product, field="product_name").casefold()
    return SUPPORTED_RESIDENT_ADAPTERS.get(normalized, "manual")


def tui_adapter_kind_for_product(product: str) -> str | None:
    normalized = token(product, field="product_name").casefold()
    return SUPPORTED_NATIVE_TUI_ADAPTERS.get(normalized)


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
        else Path.cwd().resolve()
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
