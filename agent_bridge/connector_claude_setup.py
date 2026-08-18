"""Claude Code native channel plugin artifacts for resident connectors."""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import Any

from .connector_contracts import (
    PROJECT_ROOT,
    ConnectorSetupError,
    _atomic_private_write,
    _service_suffix,
)
from .validation import opaque_id


def _claude_channel_configuration(
    *,
    connector_id: str,
    state_directory: Path,
) -> dict[str, Any]:
    suffix = _service_suffix(connector_id)
    plugin_name = f"agent-bridge-{suffix}"
    server_name = f"agent-bridge-{suffix}"
    plugin_root = state_directory / "claude-plugin"
    mcp_config_file = state_directory / "claude-channel.mcp.json"
    launcher = PROJECT_ROOT / "bin" / "agent-bridge-claude"
    return {
        "plugin_name": plugin_name,
        "plugin_root": str(plugin_root),
        "server_name": server_name,
        "selector": f"server:{server_name}",
        "mcp_config_file": str(mcp_config_file),
        "tui_endpoint_id": f"claude-{suffix}",
        "state_directory": str(state_directory),
        "launch_command": [
            str(launcher),
            "--state-directory",
            str(state_directory),
        ],
    }


def _write_claude_channel_plugin(
    *,
    configuration: dict[str, Any],
) -> None:
    """Install connector-local hooks plus a direct, uniquely named channel."""

    plugin_root = Path(str(configuration["plugin_root"]))
    plugin_root.mkdir(parents=True, exist_ok=True)
    os.chmod(plugin_root, 0o700)
    (plugin_root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (plugin_root / "hooks").mkdir(parents=True, exist_ok=True)
    for directory in (plugin_root / ".claude-plugin", plugin_root / "hooks"):
        os.chmod(directory, 0o700)
    plugin_name = str(configuration["plugin_name"])
    server_name = str(configuration["server_name"])
    state_directory = str(configuration["state_directory"])
    channel_command = str(PROJECT_ROOT / "bin" / "agent-bridge-claude-channel")
    hook_command = str(
        PROJECT_ROOT / "bin" / "agent-bridge-claude-session-hook"
    )
    _atomic_private_write(
        plugin_root / ".claude-plugin" / "plugin.json",
        (
            json.dumps(
                {
                    "name": plugin_name,
                    "version": "0.40.5",
                    "author": {"name": "Agent Bridge"},
                    "description": (
                        "Route authenticated Agent Bridge room notifications "
                        "into the exact live Claude Code session."
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        ).encode("utf-8"),
    )
    _atomic_private_write(
        Path(str(configuration["mcp_config_file"])),
        (
            json.dumps(
                {
                    "mcpServers": {
                        server_name: {
                            "command": channel_command,
                            "args": [
                                "--state-directory",
                                state_directory,
                            ],
                        }
                    }
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        ).encode("utf-8"),
    )
    quoted_hook = " ".join(
        [
            shlex.quote(hook_command),
            "--state-directory",
            shlex.quote(state_directory),
        ]
    )
    _atomic_private_write(
        plugin_root / "hooks" / "hooks.json",
        (
            json.dumps(
                {
                    "description": (
                        "Bind and end the exact Agent Bridge Claude session lease."
                    ),
                    "hooks": {
                        "SessionStart": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": quoted_hook,
                                        "timeout": 10,
                                    }
                                ]
                            }
                        ],
                        "SessionEnd": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": quoted_hook,
                                        "timeout": 10,
                                    }
                                ]
                            }
                        ],
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        ).encode("utf-8"),
    )


def configure_claude_channel_artifacts(
    state_directory: str | Path,
    *,
    home: Path | None = None,
) -> dict[str, Any]:
    """Upgrade Claude channel files without touching any resident service."""

    state = Path(state_directory).expanduser().resolve()
    manifest_file = state / "connector.json"
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, TypeError, json.JSONDecodeError) as exc:
        raise ConnectorSetupError("Claude connector manifest is invalid") from exc
    if str(manifest.get("adapter_kind") or "") != "claude-code":
        raise ConnectorSetupError("connector is not a Claude Code connector")
    connector_id = opaque_id(
        str(manifest.get("connector_id") or ""),
        field="connector_id",
    )
    del home
    configuration = _claude_channel_configuration(
        connector_id=connector_id,
        state_directory=state,
    )
    manifest["schema_version"] = max(int(manifest.get("schema_version") or 0), 4)
    manifest["claude_channel"] = configuration
    _atomic_private_write(
        manifest_file,
        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    _write_claude_channel_plugin(configuration=configuration)
    return configuration
