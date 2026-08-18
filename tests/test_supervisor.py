from __future__ import annotations

import json
from pathlib import Path

import pytest

import agent_bridge.supervisor as supervisor
from agent_bridge.claude_adapter import _tool_evidence
from agent_bridge.codex_worker import (
    CodexRpcError,
    CodexThreadHost,
    TurnEvidence,
    _finish_turn,
)
from agent_bridge.supervisor import (
    SupervisorError,
    attach_adapter_run,
    claim_batch,
    enqueue_event,
    process_once,
    queue_status,
    recover_inflight,
)


BRIDGE_ROOT = Path(__file__).resolve().parents[1]


def wake_event(
    *,
    event_id: int,
    priority: str = "normal",
    required_reply_count: int = 0,
    event_name: str = "message_available",
    pending_count: int = 1,
) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "source": "agent-bridge",
            "event": event_name,
            "event_id": event_id,
            "participant_id": "participant_receiver",
            "cursor": event_id,
            "wake_priority": priority,
            "required_reply_count": required_reply_count,
            "has_new": True,
            "has_room_activity": True,
            "backlog": {
                "pending_count": pending_count,
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
    assert batch["required_reply_count"] == 0
    assert batch["contains_backlog_event"] is False
    assert batch["backlog_pending_count"] == 0
    assert queue_status(database)["counts"]["handled"] == 2


def test_supervisor_marks_initial_backlog_for_bounded_reconnect_context(
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
    enqueue_event(
        database,
        wake_event(
            event_id=54,
            priority="mention",
            event_name="backlog",
            pending_count=87,
        ),
        now=10,
    )
    assert process_once(
        database,
        adapter_command=writer,
        wake_policy="mention",
        debounce=0,
        adapter_timeout=5,
        now=20,
    ) == 1
    batch = json.loads(captured.read_text(encoding="utf-8"))
    assert batch["contains_backlog_event"] is True
    assert batch["backlog_pending_count"] == 87


def test_supervisor_uses_largest_mandatory_backlog_snapshot(
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
    enqueue_event(
        database,
        wake_event(event_id=52, priority="mention", required_reply_count=2),
        now=10,
    )
    enqueue_event(
        database,
        wake_event(event_id=53, priority="mention", required_reply_count=3),
        now=11,
    )

    assert process_once(
        database,
        adapter_command=writer,
        wake_policy="mention",
        debounce=0,
        adapter_timeout=5,
        now=20,
    ) == 2
    batch = json.loads(captured.read_text(encoding="utf-8"))
    assert batch["required_reply_count"] == 3


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
    with supervisor.closing(supervisor._connect(database)) as connection:
        retry = connection.execute(
            "SELECT available_at - 20 AS delay FROM wake_events"
        ).fetchone()
    assert retry["delay"] <= supervisor.MAX_RETRY_DELAY_SECONDS


def test_supervisor_recovers_inflight_events_after_owner_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "wake-queue.db"
    enqueue_event(database, wake_event(event_id=61, priority="mention"), now=10)
    rows = claim_batch(
        database,
        wake_policy="mention",
        debounce=0,
        claim_owner="old-owner",
        now=20,
    )
    assert len(rows) == 1
    assert queue_status(database)["counts"]["inflight"] == 1

    assert recover_inflight(database, reason="worker restarted", now=21) == 1
    status = queue_status(database)
    assert status["counts"]["pending"] == 1
    assert status["counts"]["inflight"] == 0


def test_resident_codex_worker_requires_and_observes_exact_mention_reply(
    tmp_path: Path,
) -> None:
    mcp_command = tmp_path / "agent-bridge-mcp"
    mcp_command.write_text("#!/bin/sh\n", encoding="utf-8")
    host = CodexThreadHost(
        codex_binary="true",
        cwd=tmp_path,
        thread_state_file=tmp_path / "thread-id",
        thread_name="room worker",
        bridge_mcp_command=mcp_command,
        bridge_url="http://127.0.0.1:8765",
        product="codex",
        username="reviewer",
        signature="reads the real call chain",
        conversation="tools-room",
        roles=("reviewer",),
        capabilities=("tool-review",),
    )
    assert (
        'mcp_servers.agent-bridge.default_tools_approval_mode="approve"'
        in host.rpc._command
    )
    command_text = " ".join(host.rpc._command)
    assert "agent_register" not in command_text
    assert "agent_request_nickname" in command_text
    assert "agent_update_profile" in command_text
    assert 'mcp_servers.agent-bridge.env.AGENT_BRIDGE_AUTO_REGISTER="1"' in (
        host.rpc._command
    )
    assert 'mcp_servers.agent-bridge.env.AGENT_BRIDGE_USERNAME="reviewer"' in (
        host.rpc._command
    )
    assert 'mcp_servers.agent-bridge.env.AGENT_BRIDGE_CONVERSATION_ID="tools-room"' in (
        host.rpc._command
    )
    turn_id = "019f0000-0000-7000-8000-000000000001"
    mention_id = "msg_mention"
    host.active_turn_id = turn_id
    host._turn_evidence[turn_id] = TurnEvidence()

    notifications = iter(
        [
            {
                "method": "item/completed",
                "params": {
                    "turnId": turn_id,
                    "item": {
                        "type": "mcpToolCall",
                        "server": "agent-bridge",
                        "tool": "agent_wait",
                        "status": "completed",
                        "arguments": {"wait_seconds": 0},
                        "result": {
                            "structuredContent": {
                                "messages": [
                                    {
                                        "message_id": mention_id,
                                        "delivery": {"priority": "mention"},
                                    }
                                ]
                            }
                        },
                    },
                },
            },
            {
                "method": "item/completed",
                "params": {
                    "turnId": turn_id,
                    "item": {
                        "type": "mcpToolCall",
                        "server": "agent-bridge",
                        "tool": "agent_reply",
                        "status": "completed",
                        "arguments": {"message_id": mention_id},
                        "result": {"structuredContent": {}},
                    },
                },
            },
            {
                "method": "turn/completed",
                "params": {
                    "turn": {"id": turn_id, "status": "completed", "error": None}
                },
            },
        ]
    )

    class FakeRpc:
        @staticmethod
        def poll_notification():
            return next(notifications, None)

    host.rpc = FakeRpc()
    completion = host.poll_turn_completion()
    assert completion is not None
    _, status, error, evidence = completion
    assert status == "completed"
    assert error is None
    assert evidence.completed_bridge_tools == {"agent_wait", "agent_reply"}
    assert evidence.mention_message_ids == {mention_id}
    assert evidence.replied_message_ids == {mention_id}


def test_resident_codex_worker_launches_symlinked_codex_from_real_bundle_path(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "ChatGPT.app" / "Contents" / "Resources"
    bundle.mkdir(parents=True)
    real_binary = bundle / "codex"
    real_binary.write_text("#!/bin/sh\n", encoding="utf-8")
    real_binary.chmod(0o755)
    code_mode_host = bundle / "codex-code-mode-host"
    code_mode_host.write_text("#!/bin/sh\n", encoding="utf-8")
    code_mode_host.chmod(0o755)
    alias_directory = tmp_path / "bin"
    alias_directory.mkdir()
    alias = alias_directory / "codex"
    alias.symlink_to(real_binary)
    mcp_command = tmp_path / "agent-bridge-mcp"
    mcp_command.write_text("#!/bin/sh\n", encoding="utf-8")

    host = CodexThreadHost(
        codex_binary=str(alias),
        cwd=tmp_path,
        thread_state_file=tmp_path / "thread-id",
        thread_name="room worker",
        bridge_mcp_command=mcp_command,
        bridge_url="http://127.0.0.1:8765",
        product="codex",
        username="reviewer",
        signature="uses bundled tool host",
        conversation="tools-room",
        roles=("reviewer",),
        capabilities=("tool-review",),
    )

    assert host.rpc._command[0] == str(real_binary.resolve())
    assert Path(host.rpc._command[0]).with_name("codex-code-mode-host").is_file()


def test_resident_codex_worker_uses_read_only_sandbox_and_chat_only_rules(
    tmp_path: Path,
) -> None:
    mcp_command = tmp_path / "agent-bridge-mcp"
    mcp_command.write_text("#!/bin/sh\n", encoding="utf-8")
    host = CodexThreadHost(
        codex_binary="true",
        cwd=tmp_path,
        thread_state_file=tmp_path / "thread-id",
        thread_name="room worker",
        bridge_mcp_command=mcp_command,
        bridge_url="http://127.0.0.1:8765",
        product="codex",
        username="implementer",
        signature="implements authorized work",
        conversation="tools-room",
        roles=("developer",),
        capabilities=("implementation",),
    )
    sandbox = host._workspace_sandbox()
    assert sandbox == {"type": "readOnly", "networkAccess": True}
    instructions = host._developer_instructions()
    assert "固定使用只读沙箱" in instructions
    assert "不得创建 cron、定时器、轮询脚本" in instructions
    assert "本身已是引用回复也照常调用 agent_reply" in instructions
    assert "单独的 Codex TUI 任务" in instructions
    assert "不得在常驻连接器中实施修改" in instructions


def test_resident_codex_worker_sends_protocol_read_only_mode(
    tmp_path: Path,
) -> None:
    mcp_command = tmp_path / "agent-bridge-mcp"
    mcp_command.write_text("#!/bin/sh\n", encoding="utf-8")
    thread_id = "019f0000-0000-7000-8000-000000000001"

    class FakeRpc:
        def __init__(self, *, resumed: bool) -> None:
            self.resumed = resumed
            self.requests: list[tuple[str, dict]] = []

        def start(self) -> None:
            return None

        def request(self, method: str, params: dict, **_kwargs):
            self.requests.append((method, params))
            if method == "thread/start":
                return {"thread": {"id": thread_id}}
            if method == "thread/resume":
                return {"thread": {"id": thread_id, "turns": []}}
            return {}

    def make_host(state_file: Path) -> CodexThreadHost:
        return CodexThreadHost(
            codex_binary="true",
            cwd=tmp_path,
            thread_state_file=state_file,
            thread_name="room worker",
            bridge_mcp_command=mcp_command,
            bridge_url="http://127.0.0.1:8765",
            product="codex",
            username="reviewer",
            signature="chat only",
            conversation="tools-room",
            roles=("reviewer",),
            capabilities=("tool-review",),
        )

    new_host = make_host(tmp_path / "new-thread-id")
    new_rpc = FakeRpc(resumed=False)
    new_host.rpc = new_rpc
    new_host.start()
    start_params = next(
        params for method, params in new_rpc.requests if method == "thread/start"
    )
    assert start_params["sandbox"] == "read-only"

    state_file = tmp_path / "existing-thread-id"
    state_file.write_text(f"{thread_id}\n", encoding="utf-8")
    resumed_host = make_host(state_file)
    resumed_rpc = FakeRpc(resumed=True)
    resumed_host.rpc = resumed_rpc
    resumed_host.start()
    resume_params = next(
        params for method, params in resumed_rpc.requests if method == "thread/resume"
    )
    assert resume_params["sandbox"] == "read-only"


def test_resident_codex_worker_replaces_only_legacy_incompatible_thread_pointer(
    tmp_path: Path,
) -> None:
    mcp_command = tmp_path / "agent-bridge-mcp"
    mcp_command.write_text("#!/bin/sh\n", encoding="utf-8")
    legacy_thread_id = "019f0000-0000-7000-8000-000000000001"
    replacement_thread_id = "019f0000-0000-7000-8000-000000000002"
    state_file = tmp_path / "thread-id"
    state_file.write_text(f"{legacy_thread_id}\n", encoding="utf-8")

    class FakeRpc:
        def __init__(self) -> None:
            self.requests: list[tuple[str, dict]] = []

        def start(self) -> None:
            return None

        def request(self, method: str, params: dict, **_kwargs):
            self.requests.append((method, params))
            if method == "thread/resume":
                raise CodexRpcError(
                    method,
                    {
                        "message": (
                            "Invalid request: unknown variant `workspaceWrite`, "
                            "expected one of `read-only`, `workspace-write`, "
                            "`danger-full-access`"
                        )
                    },
                )
            if method == "thread/start":
                return {"thread": {"id": replacement_thread_id}}
            return {}

    host = CodexThreadHost(
        codex_binary="true",
        cwd=tmp_path,
        thread_state_file=state_file,
        thread_name="room worker",
        bridge_mcp_command=mcp_command,
        bridge_url="http://127.0.0.1:8765",
        product="codex",
        username="reviewer",
        signature="chat only",
        conversation="tools-room",
        roles=("reviewer",),
        capabilities=("tool-review",),
    )
    fake_rpc = FakeRpc()
    host.rpc = fake_rpc

    host.start()

    assert [method for method, _ in fake_rpc.requests] == [
        "thread/resume",
        "thread/start",
        "thread/name/set",
    ]
    assert host.thread_id == replacement_thread_id
    assert state_file.read_text(encoding="utf-8") == f"{replacement_thread_id}\n"


def test_resident_codex_worker_does_not_hide_other_resume_failures(
    tmp_path: Path,
) -> None:
    mcp_command = tmp_path / "agent-bridge-mcp"
    mcp_command.write_text("#!/bin/sh\n", encoding="utf-8")
    thread_id = "019f0000-0000-7000-8000-000000000001"
    state_file = tmp_path / "thread-id"
    state_file.write_text(f"{thread_id}\n", encoding="utf-8")

    class FakeRpc:
        @staticmethod
        def start() -> None:
            return None

        @staticmethod
        def request(method: str, _params: dict, **_kwargs):
            raise CodexRpcError(method, {"message": "authentication failed"})

    host = CodexThreadHost(
        codex_binary="true",
        cwd=tmp_path,
        thread_state_file=state_file,
        thread_name="room worker",
        bridge_mcp_command=mcp_command,
        bridge_url="http://127.0.0.1:8765",
        product="codex",
        username="reviewer",
        signature="chat only",
        conversation="tools-room",
        roles=("reviewer",),
        capabilities=("tool-review",),
    )
    host.rpc = FakeRpc()

    with pytest.raises(CodexRpcError, match="authentication failed"):
        host.start()

    assert state_file.read_text(encoding="utf-8") == f"{thread_id}\n"


def test_resident_codex_worker_deterministically_acks_only_optional_messages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp_command = tmp_path / "agent-bridge-mcp"
    mcp_command.write_text("#!/bin/sh\n", encoding="utf-8")
    host = CodexThreadHost(
        codex_binary="true",
        cwd=tmp_path,
        thread_state_file=tmp_path / "thread-id",
        thread_name="room worker",
        bridge_mcp_command=mcp_command,
        bridge_url="http://127.0.0.1:8765",
        product="codex",
        username="reviewer",
        signature="reads the real call chain",
        conversation="tools-room",
        roles=("reviewer",),
        capabilities=("tool-review",),
    )
    evidence = TurnEvidence(
        inspected_message_ids={"msg-mentioned", "msg-optional", "msg-resolved"},
        resolved_message_ids={"msg-mentioned", "msg-resolved"},
        mention_message_ids={"msg-mentioned"},
        replied_message_ids={"msg-mentioned"},
    )
    completion_client = object()
    captured: dict = {}

    def make_client(**identity):
        captured["identity"] = identity
        return completion_client

    def acknowledge(client, message_ids):
        captured["client"] = client
        captured["message_ids"] = set(message_ids)
        return frozenset(message_ids)

    monkeypatch.setattr("agent_bridge.codex_worker.resident_http_client", make_client)
    monkeypatch.setattr("agent_bridge.codex_worker.acknowledge_messages", acknowledge)

    assert host.acknowledge_optional_messages(evidence) == frozenset({"msg-optional"})
    assert evidence.resolved_message_ids == {
        "msg-mentioned",
        "msg-optional",
        "msg-resolved",
    }
    assert captured["client"] is completion_client
    assert captured["message_ids"] == {"msg-optional"}
    assert captured["identity"]["conversation_id"] == "tools-room"
    assert captured["identity"]["connector_component"] == "chat"


def test_resident_codex_worker_compacts_reconnect_backlog_via_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp_command = tmp_path / "agent-bridge-mcp"
    mcp_command.write_text("#!/bin/sh\n", encoding="utf-8")
    host = CodexThreadHost(
        codex_binary="true",
        cwd=tmp_path,
        thread_state_file=tmp_path / "thread-id",
        thread_name="room worker",
        bridge_mcp_command=mcp_command,
        bridge_url="http://127.0.0.1:8765",
        product="codex",
        username="reviewer",
        signature="reads bounded reconnect context",
        conversation="tools-room",
        roles=("reviewer",),
        capabilities=("history",),
    )
    calls: list[tuple[str, dict]] = []

    class CompletionClient:
        @staticmethod
        def post(path: str, payload: dict) -> dict:
            calls.append((path, payload))
            return {
                "applied": True,
                "compacted_optional_count": 80,
                "history_preserved": True,
            }

    monkeypatch.setattr(
        "agent_bridge.codex_worker.resident_http_client",
        lambda **_identity: CompletionClient(),
    )

    result = host.compact_offline_backlog()

    assert result["compacted_optional_count"] == 80
    assert calls == [
        ("/agent/backlog/compact", {"keep_recent_optional": 20})
    ]


def test_resident_codex_worker_retries_when_deterministic_optional_ack_fails(
    tmp_path: Path,
) -> None:
    database = tmp_path / "wake-queue.db"
    enqueue_event(database, wake_event(event_id=81, priority="mention"), now=10)
    rows = claim_batch(
        database,
        wake_policy="mention",
        debounce=0,
        claim_owner="test-worker",
        now=20,
    )
    attach_adapter_run(
        database,
        idempotency_keys=[str(row["idempotency_key"]) for row in rows],
        claim_owner="test-worker",
        adapter_run_id="turn-ack-failure",
    )

    class FailingHost:
        @staticmethod
        def acknowledge_optional_messages(evidence):
            raise RuntimeError("bridge unavailable")

    successful, completion_error = _finish_turn(
        database,
        host=FailingHost(),
        run_id="turn-ack-failure",
        status="completed",
        error=None,
        evidence=TurnEvidence(
            completed_bridge_tools={"agent_wait", "agent_reply"},
            inspected_message_ids={"msg-mentioned", "msg-optional"},
            resolved_message_ids={"msg-mentioned"},
            mention_message_ids={"msg-mentioned"},
            replied_message_ids={"msg-mentioned"},
            required_reply_count_observed=1,
        ),
        batch_required_reply=True,
    )

    assert successful is False
    assert completion_error is not None
    assert "optional-message ack failed" in completion_error
    status = queue_status(database)
    assert status["counts"]["pending"] == 1
    assert status["counts"]["handled"] == 0


def test_worker_evidence_distinguishes_optional_wakes_and_tracks_ack() -> None:
    optional_id = "msg_optional_wake"
    ordinary_id = "msg_ordinary"
    events = [
        {
            "type": "tool_use",
            "id": "wait-optional",
            "name": "mcp__agent-bridge__agent_wait",
            "input": {"wait_seconds": 0},
        },
        {
            "type": "tool_result",
            "tool_use_id": "wait-optional",
            "content": json.dumps(
                {
                    "backlog": {"required_reply_count": 0},
                    "messages": [
                        {
                            "message_id": optional_id,
                            "delivery": {
                                "priority": "mention",
                                "reasons": ["room_activity", "wake_all"],
                            },
                        },
                        {
                            "message_id": ordinary_id,
                            "delivery": {
                                "priority": "normal",
                                "reasons": ["room_activity"],
                            },
                        },
                    ],
                }
            ),
        },
        {
            "type": "tool_use",
            "id": "ack-optional",
            "name": "mcp__agent-bridge__agent_message_action",
            "input": {"message_id": optional_id, "action": "ack"},
        },
        {
            "type": "tool_result",
            "tool_use_id": "ack-optional",
            "content": "{}",
        },
        {
            "type": "tool_use",
            "id": "ack-ordinary",
            "name": "mcp__agent-bridge__agent_message_action",
            "input": {"message_id": ordinary_id, "action": "ack"},
        },
        {
            "type": "tool_result",
            "tool_use_id": "ack-ordinary",
            "content": "{}",
        },
    ]
    evidence = _tool_evidence("\n".join(json.dumps(item) for item in events))
    assert evidence.awaited_mentions == frozenset()
    assert evidence.inspected_messages == {optional_id, ordinary_id}
    assert evidence.resolved_messages == {optional_id, ordinary_id}
    assert evidence.required_reply_count_observed == 0
