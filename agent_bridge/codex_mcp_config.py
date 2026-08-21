from __future__ import annotations

import os
import re
import stat
import tempfile
import tomllib
from pathlib import Path
from typing import Any


# A direct-TUI duty call is intentionally long lived. Codex otherwise applies
# its generic five-minute MCP tool timeout, which can wake the model merely to
# retry an idle subscription. Thirty days is long enough to behave as a
# resident subscription while still leaving an explicit finite safety bound.
DIRECT_TUI_TOOL_TIMEOUT_SECONDS = 30 * 24 * 60 * 60

_AGENT_BRIDGE_SECTION = re.compile(
    r"^[ \t]*\[[ \t]*mcp_servers[ \t]*\.[ \t]*"
    r"(?:agent-bridge|\"agent-bridge\"|'agent-bridge')"
    r"[ \t]*\][ \t]*(?:#.*)?$",
    re.MULTILINE,
)
_TOOL_TIMEOUT_LINE = re.compile(
    r"^(?P<prefix>[ \t]*tool_timeout_sec[ \t]*=[ \t]*)"
    r"(?P<value>[^#\r\n]*?)"
    r"(?P<suffix>[ \t]*(?:#.*)?)(?P<newline>\r?\n|$)",
    re.MULTILINE,
)


def _public_result(
    status: str,
    *,
    changed: bool = False,
    detail: str = "",
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": status,
        "tool_timeout_sec": DIRECT_TUI_TOOL_TIMEOUT_SECONDS,
        "active_client_refresh_required": changed,
    }
    if detail:
        result["detail"] = detail
    return result


def _agent_bridge_config(parsed: dict[str, Any]) -> dict[str, Any] | None:
    servers = parsed.get("mcp_servers")
    if not isinstance(servers, dict):
        return None
    server = servers.get("agent-bridge")
    return server if isinstance(server, dict) else None


def _updated_config_text(source: str) -> str | None:
    section = _AGENT_BRIDGE_SECTION.search(source)
    if section is None:
        return None
    next_section = re.search(r"^[ \t]*\[", source[section.end() :], re.MULTILINE)
    section_end = (
        section.end() + next_section.start()
        if next_section is not None
        else len(source)
    )
    timeout_line = _TOOL_TIMEOUT_LINE.search(source, section.end(), section_end)
    value = str(DIRECT_TUI_TOOL_TIMEOUT_SECONDS)
    if timeout_line is not None:
        return (
            source[: timeout_line.start()]
            + timeout_line.group("prefix")
            + value
            + timeout_line.group("suffix")
            + timeout_line.group("newline")
            + source[timeout_line.end() :]
        )
    newline = "\r\n" if "\r\n" in source else "\n"
    return (
        source[: section.end()]
        + newline
        + f"tool_timeout_sec = {value}"
        + source[section.end() :]
    )


def _atomic_write(path: Path, content: str) -> None:
    existing_mode = stat.S_IMODE(path.stat().st_mode)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), existing_mode)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def ensure_codex_agent_bridge_timeout(
    codex_home: Path | None = None,
) -> dict[str, Any]:
    """Give direct-TUI duty calls a resident-safe Codex MCP timeout.

    This deliberately edits only an already configured ``agent-bridge`` MCP
    table. Missing or invalid user configuration is reported as a non-fatal
    setup warning because an accepted invitation must remain usable.
    """

    resolved_home = codex_home
    if resolved_home is None:
        configured_home = str(os.environ.get("CODEX_HOME") or "").strip()
        resolved_home = (
            Path(configured_home).expanduser()
            if configured_home
            else Path.home() / ".codex"
        )
    config_path = resolved_home / "config.toml"
    if not config_path.is_file():
        return _public_result(
            "unavailable",
            detail="Codex MCP config is not present; keep agent_duty one-shot until it is configured.",
        )
    try:
        source = config_path.read_text(encoding="utf-8")
        parsed = tomllib.loads(source)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        return _public_result(
            "invalid_config",
            detail=f"Codex MCP config was not changed ({type(exc).__name__}).",
        )
    server = _agent_bridge_config(parsed)
    if server is None:
        return _public_result(
            "unavailable",
            detail="The existing Codex config has no agent-bridge MCP server entry.",
        )
    current = server.get("tool_timeout_sec")
    if (
        isinstance(current, (int, float))
        and not isinstance(current, bool)
        and current >= DIRECT_TUI_TOOL_TIMEOUT_SECONDS
    ):
        return _public_result("unchanged")
    updated = _updated_config_text(source)
    if updated is None:
        return _public_result(
            "invalid_config",
            detail="The agent-bridge MCP table could not be located without rewriting the config.",
        )
    try:
        reparsed = tomllib.loads(updated)
        updated_server = _agent_bridge_config(reparsed)
        if (
            updated_server is None
            or updated_server.get("tool_timeout_sec") != DIRECT_TUI_TOOL_TIMEOUT_SECONDS
        ):
            raise ValueError("updated timeout did not validate")
        _atomic_write(config_path, updated)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, ValueError) as exc:
        return _public_result(
            "failed",
            detail=f"Codex MCP timeout could not be saved ({type(exc).__name__}).",
        )
    return _public_result("configured", changed=True)
