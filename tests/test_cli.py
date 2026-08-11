from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


BRIDGE_ROOT = Path(__file__).resolve().parents[1]


def run_cli(database: Path, *args: str) -> dict:
    process = subprocess.run(
        [
            str(BRIDGE_ROOT / "bin" / "agent-bridge"),
            "--database",
            str(database),
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
        env=dict(os.environ),
    )
    return json.loads(process.stdout)


def test_cli_is_owner_admin_and_read_only_for_chat(tmp_path: Path) -> None:
    database = tmp_path / "bridge.db"
    room = run_cli(
        database,
        "create-room",
        "--conversation",
        "cli-room",
    )
    assert room["conversation_id"] == "cli-room"

    assert run_cli(database, "rooms")["rooms"][0]["conversation_id"] == "cli-room"

    help_result = subprocess.run(
        [str(BRIDGE_ROOT / "bin" / "agent-bridge"), "--help"],
        check=True,
        capture_output=True,
        text=True,
        env=dict(os.environ),
    )
    for forbidden_chat_command in (
        " send",
        " reply",
        " wait",
        " register",
        "create-invite",
        "invites",
    ):
        assert forbidden_chat_command not in help_result.stdout
