from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from .claude_native import (
    MAX_HOOK_INPUT_BYTES,
    ClaudeConnectorState,
    ClaudeNativeError,
    load_claude_connector_state,
)


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _read_hook_payload() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(MAX_HOOK_INPUT_BYTES + 1)
    if len(raw) > MAX_HOOK_INPUT_BYTES:
        raise ClaudeNativeError("Claude hook input exceeded the safety limit")
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClaudeNativeError("Claude hook input is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ClaudeNativeError("Claude hook input must be an object")
    return payload


def handle_hook(
    state: ClaudeConnectorState,
    payload: dict[str, Any],
) -> dict[str, Any]:
    event_name = str(payload.get("hook_event_name") or "").strip()
    native_session_id = str(payload.get("session_id") or "").strip()
    if not native_session_id:
        raise ClaudeNativeError("Claude hook did not provide a session_id")
    client = state.client()
    if event_name == "SessionStart":
        raw_source = str(payload.get("source") or "startup").strip().lower()
        binding_source = "startup" if raw_source == "startup" else "resume"
        replace = (
            _truthy(os.environ.get("AGENT_BRIDGE_CLAUDE_ALLOW_SESSION_REPLACEMENT"))
            or raw_source == "clear"
        )
        intent = {
            "schema_version": 1,
            "connector_id": state.connector_id,
            "tui_endpoint_id": state.endpoint_id,
            "native_session_id": native_session_id,
            "process_epoch": state.process_epoch,
            "binding_source": binding_source,
            "replace_existing_session": replace,
            "metadata": {
                "runtime": "claude-code",
                "source": raw_source,
                "cwd": str(payload.get("cwd") or ""),
            },
            "created_at": time.time(),
            "ended": False,
        }
        state.write_binding_intent(intent)
        result = state.bind_intent(intent, client=client)
        lease = dict(result["lease"])
        return {"event": event_name, "lease": lease}
    if event_name == "SessionEnd":
        binding_intent = state.read_binding_intent()
        if (
            binding_intent is not None
            and str(binding_intent.get("process_epoch") or "") == state.process_epoch
            and str(binding_intent.get("native_session_id") or "") == native_session_id
        ):
            binding_intent["ended"] = True
            binding_intent["ended_at"] = time.time()
            state.write_binding_intent(binding_intent)
        lease_state = state.read_lease()
        if lease_state is None:
            return {"event": event_name, "ended": False, "reason": "no_lease"}
        if (
            str(lease_state.get("process_epoch") or "") != state.process_epoch
            or str(lease_state.get("native_session_id") or "") != native_session_id
        ):
            return {
                "event": event_name,
                "ended": False,
                "reason": "stale_hook",
            }
        ended = client.end_native_session(
            connector_id=state.connector_id,
            lease_id=str(lease_state["lease_id"]),
            process_epoch=state.process_epoch,
        )
        lease_state["ended"] = True
        lease_state["ended_at"] = time.time()
        state.write_lease(lease_state)
        return {"event": event_name, "ended": True, "lease": ended["lease"]}
    return {"event": event_name, "ignored": True}


def _record_failure(state_directory: str | Path, exc: Exception) -> None:
    try:
        state = Path(state_directory).expanduser().resolve()
        log = state / "logs" / "native-hook.error.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(log.parent, 0o700)
        with log.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.time():.6f} {type(exc).__name__}: {exc}\n")
        os.chmod(log, 0o600)
    except OSError:
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-bridge-claude-session-hook")
    parser.add_argument("--state-directory", default="")
    parser.add_argument("--strict", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    state_directory = str(
        args.state_directory
        or os.environ.get("AGENT_BRIDGE_CLAUDE_STATE_DIRECTORY", "")
    ).strip()
    if not state_directory:
        return 0
    try:
        state = load_claude_connector_state(state_directory)
        handle_hook(state, _read_hook_payload())
    except Exception as exc:
        _record_failure(state_directory, exc)
        if args.strict:
            raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
