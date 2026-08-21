from __future__ import annotations

import os
import stat
import tomllib
from pathlib import Path

from agent_bridge.codex_mcp_config import (
    DIRECT_TUI_TOOL_TIMEOUT_SECONDS,
    ensure_codex_agent_bridge_timeout,
)


def _write_config(codex_home: Path, content: str) -> Path:
    codex_home.mkdir()
    config = codex_home / "config.toml"
    config.write_text(content, encoding="utf-8")
    return config


def test_inserts_timeout_without_moving_env_table(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex"
    config = _write_config(
        codex_home,
        """[mcp_servers.agent-bridge]
command = "/bridge/bin/agent-bridge-mcp"

[mcp_servers.agent-bridge.env]
AGENT_BRIDGE_CLIENT_TYPE = "codex"
""",
    )
    os.chmod(config, 0o600)

    result = ensure_codex_agent_bridge_timeout(codex_home)

    parsed = tomllib.loads(config.read_text(encoding="utf-8"))
    server = parsed["mcp_servers"]["agent-bridge"]
    assert result == {
        "status": "configured",
        "tool_timeout_sec": DIRECT_TUI_TOOL_TIMEOUT_SECONDS,
        "active_client_refresh_required": True,
    }
    assert server["tool_timeout_sec"] == DIRECT_TUI_TOOL_TIMEOUT_SECONDS
    assert server["env"]["AGENT_BRIDGE_CLIENT_TYPE"] == "codex"
    assert stat.S_IMODE(config.stat().st_mode) == 0o600


def test_replaces_only_a_short_timeout_and_preserves_comment(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex"
    config = _write_config(
        codex_home,
        """[mcp_servers.\"agent-bridge\"]
tool_timeout_sec = 300 # old generic timeout
command = "/bridge/bin/agent-bridge-mcp"
""",
    )

    result = ensure_codex_agent_bridge_timeout(codex_home)

    text = config.read_text(encoding="utf-8")
    assert result["status"] == "configured"
    assert (
        f"tool_timeout_sec = {DIRECT_TUI_TOOL_TIMEOUT_SECONDS} # old generic timeout"
        in text
    )


def test_keeps_an_existing_longer_timeout_unchanged(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex"
    original = """[mcp_servers.agent-bridge]
tool_timeout_sec = 3000000
command = "/bridge/bin/agent-bridge-mcp"
"""
    config = _write_config(codex_home, original)

    result = ensure_codex_agent_bridge_timeout(codex_home)

    assert result == {
        "status": "unchanged",
        "tool_timeout_sec": DIRECT_TUI_TOOL_TIMEOUT_SECONDS,
        "active_client_refresh_required": False,
    }
    assert config.read_text(encoding="utf-8") == original


def test_missing_server_is_nonfatal_and_unchanged(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex"
    original = '[mcp_servers.other]\ncommand = "other"\n'
    config = _write_config(codex_home, original)

    result = ensure_codex_agent_bridge_timeout(codex_home)

    assert result["status"] == "unavailable"
    assert result["active_client_refresh_required"] is False
    assert config.read_text(encoding="utf-8") == original


def test_invalid_config_is_nonfatal_and_unchanged(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex"
    original = "[mcp_servers.agent-bridge\n"
    config = _write_config(codex_home, original)

    result = ensure_codex_agent_bridge_timeout(codex_home)

    assert result["status"] == "invalid_config"
    assert result["active_client_refresh_required"] is False
    assert config.read_text(encoding="utf-8") == original
