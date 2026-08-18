from __future__ import annotations

import json
import os
import time
from pathlib import Path

from agent_bridge.store import MESSAGE_COOLDOWN_SECONDS, BridgeStore
from agent_bridge.viewer_store import ViewerRepository
from agent_bridge.web_auth import WebAuthStore


def _bounded_environment_int(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return max(1, min(value, maximum))


def _bounded_environment_float(name: str, default: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return max(0.0, min(value, maximum))


SOAK_RECONNECT_CYCLES = _bounded_environment_int(
    "AGENT_BRIDGE_SOAK_CYCLES",
    24,
    10_000,
)
SOAK_INTERVAL_SECONDS = _bounded_environment_float(
    "AGENT_BRIDGE_SOAK_INTERVAL_SECONDS",
    0.0,
    3_600.0,
)
SCALE_ROOM_COUNT = 100
SCALE_AGENT_COUNT = 100
SCALE_MESSAGE_COUNT = 100_000


def _admin_id(store: BridgeStore) -> str:
    WebAuthStore(store.database)
    with store._connection() as connection:
        return str(
            connection.execute(
                "SELECT user_id FROM web_users WHERE username = 'admin'"
            ).fetchone()[0]
        )


def _native_connector(
    store: BridgeStore,
    *,
    room: str,
) -> tuple[dict, str]:
    store.create_user_room(room)
    invitation = store.create_agent_invitation(
        conversation_id=room,
        product="opencode",
        requested_mode="resident",
        adapter_kind="manual",
        tui_adapter_kind="opencode",
        created_by_web_user_id=_admin_id(store),
    )
    enrollment = "enroll_" + "s" * 64
    accepted = store.accept_agent_invitation(
        invitation_token=str(invitation["invitation_token"]),
        product="opencode",
        username="soak-native",
        signature="24 轮重连长稳测试",
        enrollment_token=enrollment,
        connector_binding_version=2,
        tui_endpoint_id="endpoint-soak-native",
        tui_native_session_id="session-soak-native",
        tui_confirmed=True,
    )
    return accepted, enrollment


def _expire_agent_sender_cooldown(
    store: BridgeStore,
    *,
    participant_id: str,
    room: str,
) -> None:
    with store._transaction() as connection:
        connection.execute(
            "UPDATE messages SET created_at = created_at - ? "
            "WHERE message_id = ("
            "SELECT message_id FROM messages "
            "WHERE sender_participant_id = ? AND conversation_id = ? "
            "ORDER BY sequence DESC LIMIT 1)",
            (MESSAGE_COOLDOWN_SECONDS + 1, participant_id, room),
        )


def test_twenty_four_reconnect_cycles_keep_one_identity_and_close_every_delivery(
    tmp_path: Path,
) -> None:
    """Time-compressed 24-hour gate: one full reconnect round per virtual hour."""

    database = tmp_path / "soak.db"
    room = "24-hour-soak"
    store = BridgeStore(database)
    accepted, enrollment = _native_connector(store, room=room)
    participant_id = str(accepted["participant_id"])
    connector_id = str(accepted["connector_id"])
    username = str(accepted["username"])

    for cycle in range(SOAK_RECONNECT_CYCLES):
        # Reopening the store models a viewer/adapter reconnect without using
        # production state or relying on one in-memory object.
        store = BridgeStore(database)
        session = store.register_agent_session_from_enrollment(
            enrollment_token=enrollment,
            connector_id=connector_id,
            connector_component="mcp",
            product="opencode",
            username=username,
            signature="24 轮重连长稳测试",
        )
        assert session["participant_id"] == participant_id
        source = store.send_owner_message(
            conversation_id=room,
            body_text=f"virtual-hour-{cycle + 1}",
            mentions=[participant_id],
        )
        _expire_agent_sender_cooldown(
            store,
            participant_id="participant_web_owner",
            room=room,
        )
        page = store.wait_messages(
            participant_id=participant_id,
            authorized_session_id=str(session["session_id"]),
            wait_seconds=0,
        )
        assert [item["message_id"] for item in page["messages"]] == [
            source["message_id"]
        ]
        stage = {
            "participant_id": participant_id,
            "authorized_session_id": str(session["session_id"]),
            "connector_id": connector_id,
            "tui_endpoint_id": "endpoint-soak-native",
            "tui_native_session_id": "session-soak-native",
            "message_ids": [str(source["message_id"])],
        }
        store.report_native_tui_delivery_stage(stage="injected", **stage)
        store.report_native_tui_delivery_stage(stage="applied", **stage)
        store.reply(
            authorized_session_id=str(session["session_id"]),
            participant_id=participant_id,
            message_id=str(source["message_id"]),
            body_text=f"virtual-hour-{cycle + 1}-ack",
        )
        _expire_agent_sender_cooldown(
            store,
            participant_id=participant_id,
            room=room,
        )
        if SOAK_INTERVAL_SECONDS and cycle + 1 < SOAK_RECONNECT_CYCLES:
            time.sleep(SOAK_INTERVAL_SECONDS)

    with store._connection() as connection:
        identity_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM participants WHERE client_type = ?",
                (f"opencode-{username}",),
            ).fetchone()[0]
        )
        owner_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = ? "
                "AND sender_participant_id = 'participant_web_owner'",
                (room,),
            ).fetchone()[0]
        )
        reply_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = ? "
                "AND sender_participant_id = ?",
                (room, participant_id),
            ).fetchone()[0]
        )
        open_deliveries = int(
            connection.execute(
                "SELECT COUNT(*) FROM message_deliveries "
                "WHERE participant_id = ? AND state IN ('pending', 'delivered')",
                (participant_id,),
            ).fetchone()[0]
        )
        replied_stages = int(
            connection.execute(
                "SELECT COUNT(*) FROM message_deliveries "
                "WHERE participant_id = ? AND delivery_stage = 'replied'",
                (participant_id,),
            ).fetchone()[0]
        )
    assert identity_count == 1
    assert owner_count == reply_count == replied_stages == SOAK_RECONNECT_CYCLES
    assert open_deliveries == 0
    print(
        "soak-result="
        + json.dumps(
            {
                "cycles": SOAK_RECONNECT_CYCLES,
                "identity_count": identity_count,
                "interval_seconds": SOAK_INTERVAL_SECONDS,
                "open_deliveries": open_deliveries,
                "replied_stages": replied_stages,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def test_hundred_room_hundred_agent_hundred_thousand_message_read_gates(
    tmp_path: Path,
) -> None:
    database = tmp_path / "scale.db"
    store = BridgeStore(database)
    registrations: list[tuple[str, dict]] = []
    for index in range(SCALE_ROOM_COUNT):
        room = f"scale-room-{index:03d}"
        store.create_user_room(room)
        registrations.append(
            (
                room,
                store.register_agent_session(
                    product="codex",
                    username=f"scale-{index:03d}",
                    signature="规模门禁 Agent",
                    conversation_id=room,
                ),
            )
        )
    assert len(registrations) == SCALE_AGENT_COUNT

    per_room = SCALE_MESSAGE_COUNT // SCALE_ROOM_COUNT
    created_base = time.time() - per_room * (MESSAGE_COOLDOWN_SECONDS + 1)
    rows: list[tuple] = []
    for room_index, (room, agent) in enumerate(registrations):
        for message_index in range(per_room):
            created_at = created_base + message_index * (
                MESSAGE_COOLDOWN_SECONDS + 1
            )
            marker = " scale-global-needle" if message_index == per_room - 1 else ""
            rows.append(
                (
                    f"scale_{room_index:03d}_{message_index:04d}",
                    room,
                    agent["participant_id"],
                    "room",
                    "*",
                    "message",
                    f"scale payload {message_index}{marker}",
                    "[]",
                    "[]",
                    0,
                    None,
                    "open",
                    None,
                    None,
                    agent["session_id"],
                    None,
                    "main",
                    "ordinary",
                    created_at,
                    created_at,
                    message_index + 1,
                )
            )
    assert len(rows) == SCALE_MESSAGE_COUNT
    with store._transaction() as connection:
        connection.executemany(
            """
            INSERT INTO messages (
                message_id, conversation_id, sender_participant_id,
                audience_kind, audience_value, message_kind, body,
                refs_json, mentions_json, wake_all_agents, reply_to, status,
                claimed_by, claim_until, authorized_session_id,
                forwarded_from_message_id, sender_seat, notification_mode,
                created_at, updated_at, room_sequence
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            rows,
        )

    repository = ViewerRepository(database)
    room_started = time.perf_counter()
    room_messages = repository.messages("scale-room-099", limit=60)
    room_elapsed = time.perf_counter() - room_started
    global_started = time.perf_counter()
    global_result = repository.search_messages_globally(
        query="scale-global-needle",
        limit=100,
    )
    global_elapsed = time.perf_counter() - global_started
    room_list_started = time.perf_counter()
    rooms = repository.rooms(limit=200)
    room_list_elapsed = time.perf_counter() - room_list_started

    assert len(room_messages) == 60
    assert [item["room_sequence"] for item in room_messages] == list(
        range(per_room - 59, per_room + 1)
    )
    assert global_result["count"] == SCALE_ROOM_COUNT
    assert len(rooms) == SCALE_ROOM_COUNT
    assert all(item["message_count"] == per_room for item in rooms)
    # These are regression fences, not vanity benchmarks. They are deliberately
    # loose enough for hosted macOS/Linux CI while still catching table scans
    # or per-message round trips that make room switching visibly stall.
    assert room_elapsed < 3.0
    assert global_elapsed < 3.0
    assert room_list_elapsed < 5.0
    print(
        "scale-performance="
        + json.dumps(
            {
                "agents": SCALE_AGENT_COUNT,
                "database_bytes": database.stat().st_size,
                "global_search_ms": round(global_elapsed * 1000, 2),
                "messages": SCALE_MESSAGE_COUNT,
                "room_history_ms": round(room_elapsed * 1000, 2),
                "room_list_ms": round(room_list_elapsed * 1000, 2),
                "rooms": SCALE_ROOM_COUNT,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
