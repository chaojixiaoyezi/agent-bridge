from __future__ import annotations

import json
import os
import socket
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn

from agent_bridge import tui_wake
from agent_bridge.store import MESSAGE_COOLDOWN_SECONDS, BridgeStore
from agent_bridge.supervisor import (
    _batch_envelope,
    attach_adapter_run,
    claim_batch,
    enqueue_event,
    finish_adapter_run,
    queue_status,
    recover_inflight,
)
from agent_bridge.tui_adapter import validate_native_tui_binding
from agent_bridge.viewer import create_app
from agent_bridge.web_auth import WebAuthStore


NATIVE_PRODUCTS = (
    ("deepseek-harness", "deepseek-harness", "deepseek-http"),
    ("opencode", "opencode", "opencode-http"),
    ("hermes", "hermes", "hermes-websocket"),
    ("pi", "pi", "pi-extension"),
    ("qwen-code", "qwen-code", "qwen-dual-file"),
)


def _admin_id(store: BridgeStore) -> str:
    WebAuthStore(store.database)
    with store._connection() as connection:
        return str(
            connection.execute(
                "SELECT user_id FROM web_users WHERE username = 'admin'"
            ).fetchone()[0]
        )


def _database_rows(
    database: Path,
    query: str,
    parameters: tuple[object, ...] = (),
) -> list[dict[str, object]]:
    store = BridgeStore(database)
    with store._connection() as connection:
        return [dict(row) for row in connection.execute(query, parameters).fetchall()]


def _expire_sender_cooldown(
    store: BridgeStore,
    *,
    participant_id: str,
    conversation_id: str,
) -> None:
    with store._transaction() as connection:
        connection.execute(
            "UPDATE messages SET created_at = created_at - ? "
            "WHERE message_id = ("
            "SELECT message_id FROM messages "
            "WHERE sender_participant_id = ? AND conversation_id = ? "
            "ORDER BY sequence DESC LIMIT 1)",
            (
                MESSAGE_COOLDOWN_SECONDS + 1.0,
                participant_id,
                conversation_id,
            ),
        )


def _transport(
    kind: str,
    *,
    root: Path,
) -> dict[str, str]:
    if kind == "deepseek-http":
        return {"kind": kind, "base_url": "http://127.0.0.1:19100"}
    if kind == "opencode-http":
        return {
            "kind": kind,
            "base_url": "http://127.0.0.1:19101",
            "directory": str(root),
        }
    if kind == "hermes-websocket":
        return {
            "kind": kind,
            "websocket_url": "ws://127.0.0.1:19102/api/ws?token=test",
        }
    if kind == "pi-extension":
        return {
            "kind": kind,
            "command_file": str(root / "pi-command.jsonl"),
            "event_file": str(root / "pi-event.jsonl"),
            "session_file": str(root / "pi-session.jsonl"),
        }
    return {
        "kind": "qwen-dual-file",
        "input_file": str(root / "qwen-input.jsonl"),
        "event_file": str(root / "qwen-event.jsonl"),
    }


@contextmanager
def _running_bridge(database: Path):
    app = create_app(database, enable_resident_repair=False)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = int(listener.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            lifespan="on",
        )
    )
    import threading

    thread = threading.Thread(
        target=lambda: server.run(sockets=[listener]),
        name="agent-bridge-reliability-test-viewer",
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=2)
        listener.close()
        raise AssertionError("isolated Bridge did not start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()


@contextmanager
def _native_environment(
    *,
    base_url: str,
    product: str,
    username: str,
    signature: str,
    conversation_id: str,
    connector_id: str,
    enrollment_file: Path,
    binding_file: Path,
    lock_file: Path,
):
    updates = {
        "AGENT_BRIDGE_URL": base_url,
        "AGENT_BRIDGE_PRODUCT": product,
        "AGENT_BRIDGE_USERNAME": username,
        "AGENT_BRIDGE_SIGNATURE": signature,
        "AGENT_BRIDGE_CONVERSATION_ID": conversation_id,
        "AGENT_BRIDGE_CONNECTOR_ID": connector_id,
        "AGENT_BRIDGE_ENROLLMENT_TOKEN_FILE": str(enrollment_file),
        "AGENT_BRIDGE_COMPONENT": "chat",
        "AGENT_BRIDGE_TUI_BINDING_FILE": str(binding_file),
        "AGENT_BRIDGE_TUI_LOCK_FILE": str(lock_file),
        "AGENT_BRIDGE_ROLES": "reviewer",
        "AGENT_BRIDGE_CAPABILITIES": "history",
    }
    previous = {key: os.environ.get(key) for key in updates}
    os.environ.update(updates)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _accept_native_agent(
    store: BridgeStore,
    *,
    root: Path,
    room: str,
    product: str,
    adapter_kind: str,
    transport_kind: str,
    suffix: str,
) -> tuple[dict, str, Path, Path]:
    username = f"reliability-{suffix}"
    endpoint_id = f"endpoint-{suffix}"
    native_session_id = f"native-session-{suffix}"
    invitation = store.create_agent_invitation(
        conversation_id=room,
        product=product,
        requested_mode="resident",
        adapter_kind="manual",
        tui_adapter_kind=adapter_kind,
        created_by_web_user_id=_admin_id(store),
    )
    enrollment = "enroll_" + suffix.ljust(64, "x")
    accepted = store.accept_agent_invitation(
        invitation_token=str(invitation["invitation_token"]),
        product=product,
        username=username,
        signature=f"{product} reliability identity",
        enrollment_token=enrollment,
        connector_binding_version=2,
        tui_endpoint_id=endpoint_id,
        tui_native_session_id=native_session_id,
        tui_confirmed=True,
    )
    binding = validate_native_tui_binding(
        adapter_kind=adapter_kind,
        endpoint_id=endpoint_id,
        native_session_id=native_session_id,
        capabilities=["steer", "multi-room"],
        transport=_transport(transport_kind, root=root),
    )
    enrollment_file = root / f"{suffix}.enrollment"
    enrollment_file.write_text(enrollment + "\n", encoding="utf-8")
    enrollment_file.chmod(0o600)
    binding_file = root / f"{suffix}.binding.json"
    binding_file.write_text(
        json.dumps(binding.payload(), ensure_ascii=False),
        encoding="utf-8",
    )
    binding_file.chmod(0o600)
    return accepted, username, enrollment_file, binding_file


@pytest.mark.parametrize(
    ("product", "adapter_kind", "transport_kind"),
    NATIVE_PRODUCTS,
)
def test_native_product_invitation_reaches_exact_tui_identity_and_replies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    product: str,
    adapter_kind: str,
    transport_kind: str,
) -> None:
    database = tmp_path / "bridge.db"
    store = BridgeStore(database)
    suffix = product.replace("-", "")
    room = f"native-e2e-{suffix}"
    control_room = f"native-control-{suffix}"
    store.create_user_room(room)
    store.create_user_room(control_room)
    accepted, username, enrollment_file, binding_file = _accept_native_agent(
        store,
        root=tmp_path,
        room=room,
        product=product,
        adapter_kind=adapter_kind,
        transport_kind=transport_kind,
        suffix=suffix,
    )
    marker = f"E2E_ACK_{suffix}"
    prompts: list[str] = []

    class DeterministicNativeClient:
        def __init__(self, binding) -> None:
            assert binding.adapter_kind == adapter_kind
            assert binding.native_session_id == f"native-session-{suffix}"

        def run_turn(self, prompt: str, **_kwargs):
            prompts.append(prompt)
            return marker, []

    monkeypatch.setattr(tui_wake, "NativeTuiClient", DeterministicNativeClient)

    with _running_bridge(database) as base_url:
        message = store.send_owner_message(
            conversation_id=room,
            body_text=f"opaque-{suffix}",
            mentions=[str(accepted["participant_id"])],
        )
        with _native_environment(
            base_url=base_url,
            product=product,
            username=username,
            signature=f"{product} reliability identity",
            conversation_id=room,
            connector_id=str(accepted["connector_id"]),
            enrollment_file=enrollment_file,
            binding_file=binding_file,
            lock_file=tmp_path / f"{suffix}.lock",
        ):
            tui_wake.run_native_wake({"event_count": 1})

    assert len(prompts) == 1
    assert f"conversation_id={room}" in prompts[0]
    assert control_room not in prompts[0]
    replies = _database_rows(
        database,
        "SELECT body, reply_to, sender_participant_id FROM messages "
        "WHERE conversation_id = ? AND reply_to = ?",
        (room, str(message["message_id"])),
    )
    assert replies == [
        {
            "body": marker,
            "reply_to": str(message["message_id"]),
            "sender_participant_id": str(accepted["participant_id"]),
        }
    ]
    delivery = _database_rows(
        database,
        "SELECT state, delivery_stage FROM message_deliveries "
        "WHERE message_id = ? AND participant_id = ?",
        (str(message["message_id"]), str(accepted["participant_id"])),
    )
    assert delivery == [{"state": "acked", "delivery_stage": "legacy_acked"}]
    assert not _database_rows(
        database,
        "SELECT message_id FROM messages WHERE conversation_id = ?",
        (control_room,),
    )


def test_supervisor_crash_after_reply_replays_without_duplicate_side_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "bridge.db"
    queue_database = tmp_path / "wake-queue.db"
    store = BridgeStore(database)
    room = "crash-after-side-effect"
    store.create_user_room(room)
    accepted, username, enrollment_file, binding_file = _accept_native_agent(
        store,
        root=tmp_path,
        room=room,
        product="opencode",
        adapter_kind="opencode",
        transport_kind="opencode-http",
        suffix="crash",
    )
    turns: list[str] = []

    class OneReplyNativeClient:
        def __init__(self, _binding) -> None:
            pass

        def run_turn(self, prompt: str, **_kwargs):
            turns.append(prompt)
            return "CRASH_RECOVERY_ACK", []

    monkeypatch.setattr(tui_wake, "NativeTuiClient", OneReplyNativeClient)

    with _running_bridge(database) as base_url:
        message = store.send_owner_message(
            conversation_id=room,
            body_text="opaque-crash-window",
            mentions=[str(accepted["participant_id"])],
        )
        wake = {
            "schema_version": 1,
            "source": "agent-bridge",
            "event": "message_available",
            "event_id": int(message["sequence"]),
            "participant_id": str(accepted["participant_id"]),
            "cursor": int(message["sequence"]),
            "wake_priority": "mention",
            "required_reply_count": 1,
            "has_new": True,
            "has_room_activity": True,
        }
        assert enqueue_event(
            queue_database,
            json.dumps(wake, separators=(",", ":")).encode("utf-8"),
        )
        first_claim = claim_batch(
            queue_database,
            wake_policy="mention",
            debounce=0,
            claim_owner="first-worker",
            now=time.time() + 1,
        )
        assert len(first_claim) == 1
        attach_adapter_run(
            queue_database,
            idempotency_keys=[str(first_claim[0]["idempotency_key"])],
            claim_owner="first-worker",
            adapter_run_id="run-before-crash",
        )
        with _native_environment(
            base_url=base_url,
            product="opencode",
            username=username,
            signature="opencode reliability identity",
            conversation_id=room,
            connector_id=str(accepted["connector_id"]),
            enrollment_file=enrollment_file,
            binding_file=binding_file,
            lock_file=tmp_path / "crash.lock",
        ):
            tui_wake.run_native_wake(json.loads(_batch_envelope(first_claim)))
            assert queue_status(queue_database)["counts"]["inflight"] == 1
            assert recover_inflight(
                queue_database,
                reason="simulated worker crash after reply",
                now=time.time() + 2,
            ) == 1
            second_claim = claim_batch(
                queue_database,
                wake_policy="mention",
                debounce=0,
                claim_owner="replacement-worker",
                now=time.time() + 3,
            )
            assert len(second_claim) == 1
            attach_adapter_run(
                queue_database,
                idempotency_keys=[str(second_claim[0]["idempotency_key"])],
                claim_owner="replacement-worker",
                adapter_run_id="run-after-crash",
            )
            tui_wake.run_native_wake(json.loads(_batch_envelope(second_claim)))
            assert finish_adapter_run(
                queue_database,
                adapter_run_id="run-after-crash",
                successful=True,
                now=time.time() + 4,
            ) == 1

    assert len(turns) == 1
    replies = _database_rows(
        database,
        "SELECT message_id, body FROM messages WHERE reply_to = ?",
        (str(message["message_id"]),),
    )
    assert len(replies) == 1
    assert replies[0]["body"] == "CRASH_RECOVERY_ACK"
    assert queue_status(queue_database)["counts"] == {
        "pending": 0,
        "inflight": 0,
        "handled": 1,
        "deferred": 0,
    }


def test_six_agent_two_room_collaboration_is_structured_isolated_and_bounded(
    tmp_path: Path,
) -> None:
    store = BridgeStore(tmp_path / "bridge.db", business_timezone="UTC")
    room_a = "collaboration-a"
    room_b = "collaboration-b"
    store.create_user_room(room_a)
    store.create_user_room(room_b)

    def register(product: str, username: str, room: str) -> dict:
        return store.register_agent_session(
            product=product,
            username=username,
            signature=f"{username} reliability participant",
            conversation_id=room,
        )

    a1 = register("codex", "a-one", room_a)
    a2 = register("claude-code", "a-two", room_a)
    a3 = register("opencode", "a-three", room_a)
    b1 = register("hermes", "b-one", room_b)
    b2 = register("pi", "b-two", room_b)
    b3 = register("qwen-code", "b-three", room_b)

    owner_request = store.send_owner_message(
        conversation_id=room_a,
        body_text="opaque-owner-request",
        mentions=[str(a1["participant_id"])],
    )
    a1_wait = store.wait_messages(
        participant_id=str(a1["participant_id"]),
        authorized_session_id=str(a1["session_id"]),
        wait_seconds=0,
    )
    assert [item["message_id"] for item in a1_wait["messages"]] == [
        owner_request["message_id"]
    ]
    assert a1_wait["backlog"]["required_reply_count"] == 1
    closeout = store.reply(
        authorized_session_id=str(a1["session_id"]),
        participant_id=str(a1["participant_id"]),
        message_id=str(owner_request["message_id"]),
        body_text="opaque-owner-closeout",
    )
    assert closeout["original_acked"] is True
    assert store.notification_snapshot(
        participant_id=str(a1["participant_id"]),
        authorized_session_id=str(a1["session_id"]),
        after_sequence=0,
    )["backlog"]["required_reply_count"] == 0

    _expire_sender_cooldown(
        store,
        participant_id=str(a1["participant_id"]),
        conversation_id=room_a,
    )
    delegated = store.send(
        authorized_session_id=str(a1["session_id"]),
        sender_participant_id=str(a1["participant_id"]),
        conversation_id=room_a,
        body_text="opaque-agent-delegation",
        mentions=[str(a2["participant_id"])],
        notification_mode="mention",
    )
    a2_wait = store.wait_messages(
        participant_id=str(a2["participant_id"]),
        authorized_session_id=str(a2["session_id"]),
        wait_seconds=0,
    )
    delegated_delivery = next(
        item for item in a2_wait["messages"] if item["message_id"] == delegated["message_id"]
    )
    assert "agent_request" in delegated_delivery["delivery"]["reasons"]
    assert a2_wait["backlog"]["required_reply_count"] == 1
    a2_reply = store.reply(
        authorized_session_id=str(a2["session_id"]),
        participant_id=str(a2["participant_id"]),
        message_id=str(delegated["message_id"]),
        body_text="opaque-agent-result",
    )
    assert a2_reply["original_acked"] is True
    assert store.notification_snapshot(
        participant_id=str(a2["participant_id"]),
        authorized_session_id=str(a2["session_id"]),
        after_sequence=0,
    )["backlog"]["required_reply_count"] == 0
    a1_after_reply = store.wait_messages(
        participant_id=str(a1["participant_id"]),
        authorized_session_id=str(a1["session_id"]),
        wait_seconds=0,
    )
    reply_delivery = next(
        item
        for item in a1_after_reply["messages"]
        if item["message_id"] == a2_reply["reply"]["message_id"]
    )
    assert "reply_wake" in reply_delivery["delivery"]["reasons"]
    assert "agent_request" not in reply_delivery["delivery"]["reasons"]

    for index in range(10):
        _expire_sender_cooldown(
            store,
            participant_id=str(a1["participant_id"]),
            conversation_id=room_a,
        )
        store.send(
            authorized_session_id=str(a1["session_id"]),
            sender_participant_id=str(a1["participant_id"]),
            conversation_id=room_a,
            body_text=f"opaque-ordinary-{index}",
            notification_mode="ordinary",
        )
    digest = store.notification_snapshot(
        participant_id=str(a3["participant_id"]),
        authorized_session_id=str(a3["session_id"]),
        after_sequence=int(delegated["sequence"]),
    )["room_activity_since_cursor"]
    digest_room = next(
        item
        for item in digest["conversations"]
        if item["conversation_id"] == room_a
    )
    assert digest_room["digest_pending_count"] == 10
    assert digest["required_reply_count"] == 0
    assert digest["priority_counts"]["mention"] >= 1

    room_a_message_ids = {
        str(row["message_id"])
        for row in _database_rows(
            store.database,
            "SELECT message_id FROM messages WHERE conversation_id = ?",
            (room_a,),
        )
    }
    room_b_participants = {
        str(item["participant_id"]) for item in (b1, b2, b3)
    }
    leaked = _database_rows(
        store.database,
        "SELECT message_id, participant_id FROM message_deliveries "
        "WHERE participant_id IN (?, ?, ?)",
        tuple(sorted(room_b_participants)),
    )
    assert not any(str(row["message_id"]) in room_a_message_ids for row in leaked)
