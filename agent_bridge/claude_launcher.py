from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any

from .claude_native import ClaudeNativeError, load_claude_connector_state
from .claude_guide import tmux_guide_from_environment


def _claude_binary(manifest: dict[str, Any]) -> str:
    configured = str(os.environ.get("AGENT_BRIDGE_CLAUDE_BINARY", "")).strip()
    if not configured:
        channel = manifest.get("claude_channel")
        if isinstance(channel, dict):
            configured = str(channel.get("claude_binary") or "").strip()
    candidate = configured or shutil.which("claude") or ""
    if not candidate:
        raise ClaudeNativeError("Claude Code CLI was not found")
    resolved = Path(candidate).expanduser().resolve()
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ClaudeNativeError("configured Claude Code CLI is not executable")
    return str(resolved)


def _is_resume(arguments: list[str]) -> bool:
    return any(
        argument in {"--resume", "-r", "--continue", "-c"}
        or argument.startswith("--resume=")
        for argument in arguments
    )


def build_claude_command(
    *,
    binary: str,
    selector: str,
    plugin_root: str,
    mcp_config_file: str,
    arguments: list[str],
) -> list[str]:
    return [
        binary,
        *arguments,
        "--disallowedTools",
        "mcp__agent-bridge__*",
        "--plugin-dir",
        plugin_root,
        "--mcp-config",
        mcp_config_file,
        "--dangerously-load-development-channels",
        selector,
    ]


def build_tmux_bootstrap_command(
    *,
    tmux_binary: str,
    state_directory: str,
    launcher_arguments: list[str],
    cwd: str,
    python_binary: str,
) -> list[str]:
    digest = hashlib.sha256(
        str(Path(state_directory).expanduser().resolve()).encode("utf-8")
    ).hexdigest()[:12]
    session_name = f"agent-bridge-claude-{digest}"
    child = [
        python_binary,
        "-m",
        "agent_bridge.claude_launcher",
        *launcher_arguments,
    ]
    return [
        tmux_binary,
        "new-session",
        "-A",
        "-s",
        session_name,
        "-c",
        cwd,
        shlex.join(child),
    ]


def _bootstrap_tmux_if_needed(
    *,
    args: argparse.Namespace,
    launcher_arguments: list[str],
) -> None:
    if (
        args.print_command
        or os.environ.get("AGENT_BRIDGE_CLAUDE_TMUX_BOOTSTRAPPED") == "1"
        or tmux_guide_from_environment() is not None
        or not sys.stdin.isatty()
        or not sys.stdout.isatty()
    ):
        return
    tmux_binary = shutil.which("tmux")
    if not tmux_binary:
        return
    environment = dict(os.environ)
    environment["AGENT_BRIDGE_CLAUDE_TMUX_BOOTSTRAPPED"] = "1"
    command = build_tmux_bootstrap_command(
        tmux_binary=tmux_binary,
        state_directory=str(args.state_directory),
        launcher_arguments=launcher_arguments,
        cwd=os.getcwd(),
        python_binary=sys.executable,
    )
    os.execvpe(command[0], command, environment)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-bridge-claude",
        description=(
            "Start or resume Claude Code with one exact Agent Bridge channel."
        ),
    )
    parser.add_argument("--state-directory", required=True)
    parser.add_argument("--print-command", action="store_true")
    parser.add_argument(
        "--replace-binding",
        action="store_true",
        help="explicitly replace a different session already bound to this connector",
    )
    parser.add_argument("claude_args", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    launcher_arguments = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(launcher_arguments)
    _bootstrap_tmux_if_needed(
        args=args,
        launcher_arguments=launcher_arguments,
    )
    claude_args = list(args.claude_args)
    if claude_args[:1] == ["--"]:
        claude_args = claude_args[1:]
    process_epoch = "epoch_" + secrets.token_urlsafe(24)
    environment = dict(os.environ)
    environment["AGENT_BRIDGE_CLAUDE_STATE_DIRECTORY"] = str(
        Path(args.state_directory).expanduser().resolve()
    )
    environment["AGENT_BRIDGE_CLAUDE_PROCESS_EPOCH"] = process_epoch
    environment["AGENT_BRIDGE_CLAUDE_ALLOW_SESSION_REPLACEMENT"] = (
        "1" if args.replace_binding or not _is_resume(claude_args) else "0"
    )
    os.environ.update(
        {
            key: value
            for key, value in environment.items()
            if key.startswith("AGENT_BRIDGE_CLAUDE_")
        }
    )
    state = load_claude_connector_state(args.state_directory)
    channel = state.manifest.get("claude_channel")
    if not isinstance(channel, dict):
        raise ClaudeNativeError("Claude channel configuration is missing")
    selector = str(channel.get("selector") or "").strip()
    if not selector:
        raise ClaudeNativeError("Claude channel selector is missing")
    plugin_root = str(channel.get("plugin_root") or "").strip()
    mcp_config_file = str(channel.get("mcp_config_file") or "").strip()
    if not plugin_root or not mcp_config_file:
        raise ClaudeNativeError("Claude channel launch files are missing")
    command = build_claude_command(
        binary=_claude_binary(state.manifest),
        selector=selector,
        plugin_root=plugin_root,
        mcp_config_file=mcp_config_file,
        arguments=claude_args,
    )
    if args.print_command:
        print(json.dumps(command, ensure_ascii=False))
        return 0
    os.execvpe(command[0], command, environment)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
