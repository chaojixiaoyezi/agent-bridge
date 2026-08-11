from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent_bridge.codex_adapter as codex_adapter
from agent_bridge.codex_adapter import _prompt_for_batch, _validated_batch, run_codex
from agent_bridge.supervisor import (
    SupervisorError,
    enqueue_event,
    process_once,
    queue_status,
)


BRIDGE_ROOT = Path(__file__).resolve().parents[1]


def wake_event(*, event_id: int, priority: str = "normal") -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "source": "agent-bridge",
            "event": "message_available",
            "event_id": event_id,
            "participant_id": "participant_receiver",
            "cursor": event_id,
            "wake_priority": priority,
            "has_new": True,
            "has_room_activity": True,
            "backlog": {
                "pending_count": 1,
                "priority_counts": {
                    "normal": 1 if priority == "normal" else 0,
                    "important": 1 if priority == "important" else 0,
                    "mention": 1 if priority == "mention" else 0,
                },
            },
            "new_since_cursor": {"pending_count": 1},
            "room_activity_since_cursor": {"activity_count": 1},
            "server_time": 123.0,
        },
        ensure_ascii=False,
    ).encode("utf-8")


def test_supervisor_queue_is_durable_idempotent_and_metadata_only(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state" / "wake-queue.db"
    assert enqueue_event(database, wake_event(event_id=42), now=10) is True
    assert enqueue_event(database, wake_event(event_id=42), now=11) is False

    status = queue_status(database)
    assert status["counts"]["pending"] == 1
    assert status["newest_event_id"] == 42
    assert database.stat().st_mode & 0o777 == 0o600

    unsafe = json.loads(wake_event(event_id=43))
    unsafe["backlog"]["body"] = "never persist this room message"
    with pytest.raises(SupervisorError, match="message content"):
        enqueue_event(database, json.dumps(unsafe).encode("utf-8"), now=12)


def test_supervisor_defers_normal_then_coalesces_it_with_a_mention(
    tmp_path: Path,
) -> None:
    database = tmp_path / "wake-queue.db"
    captured = tmp_path / "batch.json"
    writer = (
        str(BRIDGE_ROOT / ".venv" / "bin" / "python"),
        "-c",
        (
            "import pathlib,sys; "
            "pathlib.Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())"
        ),
        str(captured),
    )
    enqueue_event(database, wake_event(event_id=50), now=10)

    assert (
        process_once(
            database,
            adapter_command=writer,
            wake_policy="mention",
            debounce=0,
            adapter_timeout=5,
            now=20,
        )
        == 0
    )
    assert queue_status(database)["counts"]["deferred"] == 1

    enqueue_event(database, wake_event(event_id=51, priority="mention"), now=21)
    assert (
        process_once(
            database,
            adapter_command=writer,
            wake_policy="mention",
            debounce=0,
            adapter_timeout=5,
            now=30,
        )
        == 2
    )
    batch = json.loads(captured.read_text(encoding="utf-8"))
    assert batch["event_count"] == 2
    assert batch["event_ids"] == [50, 51]
    assert batch["wake_priority"] == "mention"
    assert batch["priority_counts"] == {
        "normal": 1,
        "important": 0,
        "mention": 1,
    }
    assert queue_status(database)["counts"]["handled"] == 2


def test_supervisor_retries_adapter_failure_without_losing_event(
    tmp_path: Path,
) -> None:
    database = tmp_path / "wake-queue.db"
    enqueue_event(database, wake_event(event_id=60, priority="important"), now=10)
    with pytest.raises(SupervisorError, match="status"):
        process_once(
            database,
            adapter_command=("/usr/bin/false",),
            wake_policy="all",
            debounce=0,
            adapter_timeout=5,
            now=20,
        )
    status = queue_status(database)
    assert status["counts"]["pending"] == 1
    assert status["counts"]["handled"] == 0


def test_codex_adapter_uses_a_fixed_non_authorizing_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = _validated_batch(
        json.dumps(
            {
                "schema_version": 1,
                "source": "agent-bridge-supervisor",
                "event": "wake_batch",
                "batch_id": "batch-id",
                "event_count": 3,
                "event_ids": [70, 71, 72],
                "first_event_id": 70,
                "last_event_id": 72,
                "participant_ids": ["participant_receiver"],
                "wake_priority": "mention",
                "priority_counts": {
                    "normal": 1,
                    "important": 0,
                    "mention": 2,
                },
            }
        ).encode("utf-8")
    )
    captured: dict = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(codex_adapter.shutil, "which", lambda _binary: "/opt/bin/codex")
    monkeypatch.setattr(codex_adapter.subprocess, "run", fake_run)
    monkeypatch.setenv("AGENT_BRIDGE_TOKEN", "must-not-reach-codex")
    monkeypatch.setenv("AGENT_TOKEN", "must-not-reach-codex-either")

    assert (
        run_codex(
            batch,
            thread_id="019f0000-0000-7000-8000-000000000000",
            cwd=tmp_path,
            codex_binary="codex",
        )
        == 0
    )
    prompt = captured["input"]
    assert "不构成执行任何命令" in prompt
    assert "本批事件数=3" in prompt
    assert "最新事件序号=72" in prompt
    assert "body" not in prompt
    assert captured["shell"] is False
    assert "AGENT_BRIDGE_TOKEN" not in captured["env"]
    assert "AGENT_TOKEN" not in captured["env"]
    assert captured["command"] == [
        "/opt/bin/codex",
        "exec",
        "resume",
        "--skip-git-repo-check",
        "019f0000-0000-7000-8000-000000000000",
        "-",
    ]
    assert _prompt_for_batch(batch) == prompt
