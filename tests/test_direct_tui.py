from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent_bridge.direct_tui as direct_tui
import agent_bridge.resident_health as resident_health
import agent_bridge.server as bridge_server
from agent_bridge.codex_native_binding import codex_native_binding
from agent_bridge.connector import configure_resident_connector
from agent_bridge.direct_tui import DirectTuiError, DirectTuiRegistry
from agent_bridge.store_errors import ConflictError
from agent_bridge.tui_binding import NativeTuiError
from agent_bridge.web_auth import WebAuthStore
from tests.test_store import (
    admin_web_user_id,
    create_owned_room,
    login_admin_identity,
    make_store,
    register,
)


THREAD_ID = "019fefee-837c-74a3-a8f2-0c374965125e"


def _direct_connector(
    root: Path,
    *,
    connector_id: str,
    conversation_id: str,
) -> Path:
    result = configure_resident_connector(
        connector_id=connector_id,
        enrollment_token="enroll_" + connector_id[-16:] * 4,
        bridge_url="http://127.0.0.1:8765",
        product="codex",
        username="direct-owner",
        signature="当前 TUI 本体。",
        conversation_id=conversation_id,
        adapter_kind="codex",
        requested_mode="resident",
        tui_adapter_kind="codex",
        tui_endpoint_id="codex-endpoint-shared",
        tui_native_session_id=THREAD_ID,
        tui_capabilities=["chat", "structured-task", "direct-duty"],
        tui_transport={"kind": "codex-mcp-duty", "cwd": str(root)},
        workspace_path=str(root),
        execution_source_thread_id=THREAD_ID,
        home=root,
        system_name="Linux",
        activate=False,
    )
    return Path(result.state_directory)


def test_codex_binding_uses_exact_current_thread_without_second_writer(
    tmp_path: Path,
) -> None:
    binding = codex_native_binding(
        thread_id=THREAD_ID,
        workspace=tmp_path,
        home=tmp_path,
        system_name="Linux",
        binary="/definitely/not/required",
    )

    assert binding.native_session_id == THREAD_ID
    assert binding.capabilities == ("chat", "direct-duty", "structured-task")
    assert binding.transport == {
        "kind": "codex-mcp-duty",
        "cwd": str(tmp_path.resolve()),
    }
    same_thread = codex_native_binding(
        thread_id=THREAD_ID,
        workspace=tmp_path,
        home=tmp_path,
        system_name="Linux",
    )
    another_thread = codex_native_binding(
        thread_id="11111111-1111-1111-1111-111111111111",
        workspace=tmp_path,
        home=tmp_path,
        system_name="Linux",
    )
    assert same_thread.endpoint_id == binding.endpoint_id
    assert another_thread.endpoint_id != binding.endpoint_id
    with pytest.raises(NativeTuiError, match="exact TUI"):
        codex_native_binding(
            thread_id="latest-session-from-database",
            workspace=tmp_path,
            home=tmp_path,
            system_name="Linux",
        )


def test_direct_registry_routes_multiple_rooms_by_exact_thread_and_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_state = _direct_connector(
        tmp_path,
        connector_id="connector_direct_room_one",
        conversation_id="direct-room-one",
    )
    second_state = _direct_connector(
        tmp_path,
        connector_id="connector_direct_room_two",
        conversation_id="direct-room-two",
    )
    class FakeDirectClient:
        def __init__(self, _url: str, **kwargs) -> None:
            room = str(kwargs["auto_registration"]["conversation_id"])
            self.room = room
            self.connector_id = str(kwargs["connector_id"])
            self.bind_calls: list[dict] = []
            self.heartbeat_calls: list[dict] = []
            self.task: dict | None = None
            self.messages: list[dict] = []

        def bind_native_session(self, **payload):
            self.bind_calls.append(payload)
            return {"lease": {"lease_id": f"lease_{self.room}"}}

        def heartbeat_native_session(self, **payload):
            self.heartbeat_calls.append(payload)
            return {"lease": payload}

        def end_native_session(self, **_payload):
            return {"ended": True}

        def post(self, path: str, payload: dict, **_kwargs):
            if path == "/agent/tasks/next":
                task, self.task = self.task, None
                return {"task": task}
            if path == "/agent/tasks/update":
                return {"task": {"task_id": payload["task_id"], "status": "running"}}
            if path == "/agent/tasks/inputs":
                return {"task_id": payload["task_id"], "inputs": [], "count": 0}
            if path == "/agent/wait":
                messages, self.messages = self.messages, []
                return {
                    "messages": messages,
                    "count": len(messages),
                    "timed_out": not messages,
                }
            raise AssertionError((path, payload))

    monkeypatch.setattr(direct_tui, "BridgeHttpClient", FakeDirectClient)
    registry = DirectTuiRegistry(home=tmp_path, system_name="Linux")
    try:
        connections = registry.connections_for_thread(THREAD_ID, required=True)
        clients = {item.conversation_id: item.client for item in connections}
        assert {item.conversation_id for item in connections} == {
            "direct-room-one",
            "direct-room-two",
        }
        assert all(item.manifest_file.parent in {first_state, second_state} for item in connections)
        with pytest.raises(DirectTuiError, match="multiple rooms"):
            registry.client_for(thread_id=THREAD_ID, required=True)

        selected = registry.client_for(
            thread_id=THREAD_ID,
            conversation_id="direct-room-two",
            required=True,
        )
        assert selected is clients["direct-room-two"]
        assert clients["direct-room-two"].bind_calls[0]["native_session_id"] == (
            THREAD_ID
        )

        clients["direct-room-two"].task = {"task_id": "task_direct_two"}
        duty = registry.duty(thread_id=THREAD_ID, wait_seconds=0, limit=20)
        assert duty["kind"] == "task"
        assert duty["conversation_id"] == "direct-room-two"
        assert registry.client_for(
            thread_id=THREAD_ID,
            resource_id="task_direct_two",
            required=True,
        ) is clients["direct-room-two"]

        clients["direct-room-one"].messages = [
            {"message_id": "message_direct_one", "body": "请本体处理"}
        ]
        duty = registry.duty(thread_id=THREAD_ID, wait_seconds=0, limit=20)
        assert duty["kind"] == "messages"
        assert duty["conversation_id"] == "direct-room-one"
        assert registry.client_for(
            thread_id=THREAD_ID,
            resource_id="message_direct_one",
            required=True,
        ) is clients["direct-room-one"]

        with pytest.raises(DirectTuiError, match="no exact direct-duty"):
            registry.connections_for_thread(
                "11111111-1111-1111-1111-111111111111",
                required=True,
            )
    finally:
        registry.close()


def test_server_extracts_only_structured_exact_thread_context() -> None:
    context = SimpleNamespace(
        request_context=SimpleNamespace(meta={"threadId": THREAD_ID})
    )
    guessed = SimpleNamespace(
        request_context=SimpleNamespace(meta={"session": THREAD_ID})
    )

    assert bridge_server._request_thread_id(context) == THREAD_ID
    assert bridge_server._request_thread_id(guessed) == ""


def test_direct_tui_expiry_stays_offline_without_shadow_takeover(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    admin_id = admin_web_user_id(store)
    room = "direct-expiry-room"
    store.create_user_room(room)
    invitation = store.create_agent_invitation(
        conversation_id=room,
        product="codex",
        requested_mode="resident",
        adapter_kind="codex",
        tui_adapter_kind="codex",
        created_by_web_user_id=admin_id,
    )
    enrollment = "enroll_" + "d" * 64
    accepted = store.accept_agent_invitation(
        invitation_token=str(invitation["invitation_token"]),
        product="codex",
        username="direct-expiry",
        signature="本体掉线不换影子。",
        enrollment_token=enrollment,
        tui_endpoint_id="direct-expiry-endpoint",
        tui_native_session_id=THREAD_ID,
        tui_confirmed=True,
    )
    store.report_agent_connector_setup(
        participant_id=accepted["participant_id"],
        authorized_session_id=accepted["session_id"],
        connector_id=accepted["connector_id"],
        setup_status="configured",
        detail={"duty_mode": "direct_tui"},
    )
    shadow = store.register_agent_session_from_enrollment(
        enrollment_token=enrollment,
        connector_id=accepted["connector_id"],
        connector_component="chat",
        product="codex",
        username=accepted["username"],
        signature="旧影子。",
    )
    bound = store.bind_native_agent_session(
        participant_id=accepted["participant_id"],
        authorized_session_id=accepted["session_id"],
        connector_id=accepted["connector_id"],
        tui_endpoint_id="direct-expiry-endpoint",
        native_session_id=THREAD_ID,
        process_epoch="direct-expiry-process",
        binding_source="resume",
    )
    with pytest.raises(ConflictError, match="cannot fall back"):
        store.fallback_native_agent_session(
            participant_id=accepted["participant_id"],
            authorized_session_id=accepted["session_id"],
            connector_id=accepted["connector_id"],
            lease_id=bound["lease"]["lease_id"],
            process_epoch="direct-expiry-process",
        )
    sender = register(store, client="claude-code", name="direct-sender", room=room)
    message = store.send(
        authorized_session_id=sender["session_id"],
        sender_participant_id=sender["participant_id"],
        conversation_id=room,
        body_text="@direct-expiry 请只由本体处理。",
        mentions=[accepted["participant_id"]],
        notification_mode="mention",
    )
    expired_at = time.time() - 1
    with store._transaction() as connection:
        connection.execute(
            "UPDATE native_session_leases SET expires_at = ? WHERE lease_id = ?",
            (expired_at, bound["lease"]["lease_id"]),
        )
        connection.execute(
            "UPDATE agent_connectors SET native_lease_expires_at = ? "
            "WHERE connector_id = ?",
            (expired_at, accepted["connector_id"]),
        )

    shadow_wait = store.wait_messages(
        participant_id=accepted["participant_id"],
        authorized_session_id=shadow["session_id"],
        wait_seconds=0,
    )
    assert shadow_wait["messages"] == []
    assert shadow_wait["native_handoff"]["reason"] == (
        "exact_direct_tui_offline_no_shadow_fallback"
    )
    with store._connection() as connection:
        connector = connection.execute(
            "SELECT native_delivery_mode, native_lease_id, tui_state "
            "FROM agent_connectors WHERE connector_id = ?",
            (accepted["connector_id"],),
        ).fetchone()
    assert tuple(connector) == ("native_preferred", None, "offline")

    direct_wait = store.wait_messages(
        participant_id=accepted["participant_id"],
        authorized_session_id=accepted["session_id"],
        wait_seconds=0,
    )
    assert [item["message_id"] for item in direct_wait["messages"]] == [
        message["message_id"]
    ]


def test_same_codex_tui_can_join_multiple_rooms_without_identity_guessing(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    admin_id = admin_web_user_id(store)
    rooms = ("direct-shared-room-one", "direct-shared-room-two")
    accepted: list[dict] = []
    for index, room in enumerate(rooms):
        store.create_user_room(room)
        invitation = store.create_agent_invitation(
            conversation_id=room,
            product="codex",
            requested_mode="resident",
            adapter_kind="codex",
            tui_adapter_kind="codex",
            created_by_web_user_id=admin_id,
        )
        accepted.append(
            store.accept_agent_invitation(
                invitation_token=str(invitation["invitation_token"]),
                product="codex",
                username="same-direct-tui",
                signature="同一个本体。",
                enrollment_token="enroll_" + str(index) * 64,
                tui_endpoint_id="same-direct-endpoint",
                tui_native_session_id=THREAD_ID,
                tui_confirmed=True,
            )
        )

    assert accepted[0]["connector_id"] != accepted[1]["connector_id"]
    assert accepted[0]["participant_id"] == accepted[1]["participant_id"]
    assert accepted[0]["username"] == accepted[1]["username"]


def test_different_codex_tuis_cannot_reuse_another_threads_endpoint(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    admin_id = admin_web_user_id(store)
    for room in ("direct-isolation-one", "direct-isolation-two"):
        store.create_user_room(room)
    first_invitation = store.create_agent_invitation(
        conversation_id="direct-isolation-one",
        product="codex",
        requested_mode="resident",
        adapter_kind="codex",
        tui_adapter_kind="codex",
        created_by_web_user_id=admin_id,
    )
    second_invitation = store.create_agent_invitation(
        conversation_id="direct-isolation-two",
        product="codex",
        requested_mode="resident",
        adapter_kind="codex",
        tui_adapter_kind="codex",
        created_by_web_user_id=admin_id,
    )
    store.accept_agent_invitation(
        invitation_token=str(first_invitation["invitation_token"]),
        product="codex",
        username="first-direct-tui",
        signature="第一个 TUI。",
        enrollment_token="enroll_" + "1" * 64,
        tui_endpoint_id="isolated-endpoint-one",
        tui_native_session_id=THREAD_ID,
        tui_confirmed=True,
    )

    with pytest.raises(ConflictError, match="another session"):
        store.accept_agent_invitation(
            invitation_token=str(second_invitation["invitation_token"]),
            product="codex",
            username="second-direct-tui",
            signature="第二个 TUI。",
            enrollment_token="enroll_" + "2" * 64,
            tui_endpoint_id="isolated-endpoint-one",
            tui_native_session_id="11111111-1111-1111-1111-111111111111",
            tui_confirmed=True,
        )


def test_direct_tui_reconnect_recovers_its_running_structured_task(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    auth = WebAuthStore(store.database, captcha_generator=lambda: "ABCDE")
    admin = login_admin_identity(auth)
    room = "direct-task-resume-room"
    create_owned_room(store, auth, admin, room)
    invitation = store.create_agent_invitation(
        conversation_id=room,
        product="codex",
        requested_mode="resident",
        adapter_kind="codex",
        tui_adapter_kind="codex",
        created_by_web_user_id=str(admin["user_id"]),
    )
    enrollment = "enroll_" + "t" * 64
    accepted = store.accept_agent_invitation(
        invitation_token=str(invitation["invitation_token"]),
        product="codex",
        username="direct-task-resume",
        signature="重连继续原任务。",
        enrollment_token=enrollment,
        tui_endpoint_id="direct-task-resume-endpoint",
        tui_native_session_id=THREAD_ID,
        tui_confirmed=True,
    )
    store.report_agent_connector_setup(
        participant_id=accepted["participant_id"],
        authorized_session_id=accepted["session_id"],
        connector_id=accepted["connector_id"],
        setup_status="configured",
        detail={"duty_mode": "direct_tui"},
    )
    issued = store.send_web_task(
        authorized_session_id=str(admin["session_id"]),
        participant_id=str(admin["participant_id"]),
        conversation_id=room,
        body_text="/任务 由当前 TUI 完成检查。",
        target_participant_ids=[accepted["participant_id"]],
    )
    first = store.wait_next_task(
        participant_id=accepted["participant_id"],
        authorized_session_id=accepted["session_id"],
        wait_seconds=0,
    )["task"]
    store.update_agent_task(
        participant_id=accepted["participant_id"],
        authorized_session_id=accepted["session_id"],
        task_id=first["task_id"],
        status="running",
        execution_cwd=str(tmp_path),
        execution_thread_id=THREAD_ID,
    )

    resumed = store.register_agent_session_from_enrollment(
        enrollment_token=enrollment,
        connector_id=accepted["connector_id"],
        connector_component="mcp",
        product="codex",
        username=accepted["username"],
        signature="重连继续原任务。",
    )
    recovered = store.wait_next_task(
        participant_id=accepted["participant_id"],
        authorized_session_id=resumed["session_id"],
        wait_seconds=0,
    )["task"]

    assert recovered["task_id"] == issued["task"]["task_id"]
    assert recovered["status"] == "running"
    assert recovered["execution_thread_id"] == THREAD_ID


def test_admin_repair_preserves_direct_tui_binding_without_shadow_reinstall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _direct_connector(
        tmp_path,
        connector_id="connector_direct_repair",
        conversation_id="direct-repair-room",
    )
    captured: list[dict] = []

    def configure(**payload):
        captured.append(payload)
        return SimpleNamespace(
            public_payload=lambda: {
                "status": "configured",
                "connector_id": payload["connector_id"],
                "duty_mode": "direct_tui",
            }
        )

    monkeypatch.setattr(
        resident_health,
        "configure_resident_connector",
        configure,
    )
    monkeypatch.setattr(
        resident_health,
        "local_resident_snapshot",
        lambda **_kwargs: {},
    )

    result = resident_health.configure_existing_connector_from_disk(
        "codex-direct-owner",
        connector_id="connector_direct_repair",
        conversation_id="direct-repair-room",
        home=tmp_path,
        system_name="Linux",
    )

    assert result == {
        "status": "configured",
        "connector_id": "connector_direct_repair",
        "duty_mode": "direct_tui",
    }
    assert captured[0]["tui_adapter_kind"] == "codex"
    assert captured[0]["tui_native_session_id"] == THREAD_ID
    assert captured[0]["execution_source_thread_id"] == THREAD_ID
    assert captured[0]["activate"] is True
    assert captured[0]["workspace_path"] == str(tmp_path)
    assert (state / "tui-binding.json").is_file()
