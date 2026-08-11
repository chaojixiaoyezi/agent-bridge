from __future__ import annotations

import concurrent.futures
import sqlite3
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from agent_bridge.store import ROOM_ABANDON_AFTER_SECONDS, BridgeStore
from agent_bridge.viewer import WEB_ROOT, create_app
from agent_bridge.viewer_store import ViewerRepository


def seed(database: Path) -> tuple[BridgeStore, dict, dict]:
    store = BridgeStore(database)
    sender_base = store.register(
        client_type="claude-code-小鲸鱼娘",
        session_alias="开发会话",
        conversation_id="room-one",
        roles=["developer"],
        create_room_if_missing=True,
    )
    receiver_base = store.register(
        client_type="codex-小可爱",
        session_alias="审计会话",
        conversation_id="room-one",
        roles=["reviewer"],
    )
    sender = store.register_agent_session(
        conversation_id="room-one",
        product="claude-code",
        username="小鲸鱼娘",
        session_alias="开发会话",
        roles=["developer"],
    )
    receiver = store.register_agent_session(
        conversation_id="room-one",
        product="codex",
        username="小可爱",
        session_alias="审计会话",
        roles=["reviewer"],
    )
    assert sender["participant_id"] == sender_base["participant_id"]
    assert receiver["participant_id"] == receiver_base["participant_id"]
    store.send(
        authorized_session_id=sender["session_id"],
        sender_participant_id=sender["participant_id"],
        conversation_id="room-one",
        body_text="请看一下事务边界。",
        audience_kind="participant",
        audience_value=receiver["participant_id"],
    )
    store.register(
        client_type="future-agent-未来伙伴",
        session_alias="另一个房间",
        conversation_id="room-two",
        create_room_if_missing=True,
    )
    return store, sender, receiver


def test_dashboard_lists_rooms_messages_and_participants(tmp_path: Path) -> None:
    database = tmp_path / "bridge.db"
    seed(database)
    client = TestClient(create_app(database))

    index = client.get("/")
    assert index.status_code == 200
    assert "Agent Bridge" in index.text
    assert "default-src 'self'" in index.headers["content-security-policy"]
    assert "form-action 'self'" in index.headers["content-security-policy"]
    assert index.headers["cache-control"] == "no-store"

    rooms = client.get("/api/rooms").json()["rooms"]
    assert {room["conversation_id"] for room in rooms} == {"room-one", "room-two"}
    room_one = next(room for room in rooms if room["conversation_id"] == "room-one")
    assert room_one["message_count"] == 1
    assert room_one["participant_count"] == 2

    messages = client.get("/api/rooms/room-one/messages").json()["messages"]
    assert messages[0]["body"] == "请看一下事务边界。"
    participants = client.get("/api/rooms/room-one/participants").json()[
        "participants"
    ]
    assert {person["session_alias"] for person in participants} == {
        "开发会话",
        "审计会话",
    }


def test_dashboard_renders_messages_as_text_and_keeps_read_projection_read_only(
    tmp_path: Path,
) -> None:
    database = tmp_path / "bridge.db"
    store, sender, receiver = seed(database)
    malicious = '<img src=x onerror="touch /tmp/never">'
    store.send(
        authorized_session_id=receiver["session_id"],
        sender_participant_id=receiver["participant_id"],
        conversation_id="room-one",
        body_text=malicious,
        audience_kind="participant",
        audience_value=sender["participant_id"],
    )
    client = TestClient(create_app(database))

    assert client.post("/api/rooms").status_code == 403
    assert client.post(
        "/api/rooms/room-one/messages",
        json={"body": "缺少页面意图不能发言"},
    ).status_code == 403
    assert malicious not in client.get("/").text
    payload = client.get("/api/rooms/room-one/messages").json()
    assert payload["messages"][-1]["body"] == malicious
    javascript = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in javascript
    assert ".textContent" in javascript

    repository = ViewerRepository(database)
    with repository._connection() as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM messages")


def test_dashboard_rejects_invalid_room_ids_and_limits(tmp_path: Path) -> None:
    database = tmp_path / "bridge.db"
    seed(database)
    client = TestClient(create_app(database))

    assert client.get("/api/rooms/bad%5Cid/messages").status_code == 400
    assert client.get("/api/rooms?limit=not-a-number").status_code == 400
    health = client.get("/api/health").json()
    assert health["message_view_read_only"] is False
    assert health["room_creation_enabled"] is True
    assert health["owner_message_enabled"] is True
    assert health["open_registration_enabled"] is True
    assert health["counts"]["messages"] == 1


def test_same_origin_browser_user_can_create_room_without_agent_membership(
    tmp_path: Path,
) -> None:
    database = tmp_path / "bridge.db"
    client = TestClient(
        create_app(database),
        base_url="http://192.168.1.20:8765",
    )
    headers = {
        "Origin": "http://192.168.1.20:8765",
        "X-Agent-Bridge-Intent": "create-room",
    }
    created = client.post(
        "/api/rooms",
        json={"conversation_id": "大家沟通群"},
        headers=headers,
    )
    assert created.status_code == 201
    assert created.json()["room"]["creator_kind"] == "user"

    room = next(
        room
        for room in client.get("/api/rooms").json()["rooms"]
        if room["conversation_id"] == "大家沟通群"
    )
    assert room["status"] == "active"
    assert room["participant_count"] == 0
    assert room["message_count"] == 0

    duplicate = client.post(
        "/api/rooms",
        json={"conversation_id": "大家沟通群"},
        headers=headers,
    )
    assert duplicate.status_code == 409
    cross_origin = client.post(
        "/api/rooms",
        json={"conversation_id": "evil-room"},
        headers={
            "Origin": "https://evil.example",
            "X-Agent-Bridge-Intent": "create-room",
        },
    )
    assert cross_origin.status_code == 403
    form_post = client.post(
        "/api/rooms",
        content="conversation_id=form-room",
        headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert form_post.status_code == 415
    assert client.get(
        "/api/rooms/%E5%A4%A7%E5%AE%B6%E6%B2%9F%E9%80%9A%E7%BE%A4/messages"
    ).status_code == 200
    html = client.get("/").text
    assert "pattern=" not in html
    assert "支持中文" in html


def test_dashboard_separates_abandoned_rooms_and_retains_history(
    tmp_path: Path,
) -> None:
    database = tmp_path / "bridge.db"
    store, _, _ = seed(database)
    archive_time = time.time()
    with store._transaction() as connection:
        connection.execute(
            "UPDATE rooms SET last_activity_at = ? WHERE conversation_id = ?",
            (archive_time - ROOM_ABANDON_AFTER_SECONDS, "room-one"),
        )
    client = TestClient(create_app(database))

    room = next(
        room
        for room in client.get("/api/rooms").json()["rooms"]
        if room["conversation_id"] == "room-one"
    )
    assert room["status"] == "abandoned"
    assert room["message_count"] == 1
    assert room["active_participant_count"] == 0
    assert client.get("/api/rooms/room-one/messages").json()["messages"][0][
        "body"
    ] == "请看一下事务边界。"
    participants = client.get("/api/rooms/room-one/participants").json()[
        "participants"
    ]
    assert len(participants) == 2
    assert all(person["membership_active"] is False for person in participants)

    html = client.get("/").text
    javascript = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
    assert "创建聊天室" in html
    assert "废弃聊天室" in javascript


def test_open_registration_owner_chat_and_authenticated_agent_http_flow(
    tmp_path: Path,
) -> None:
    database = tmp_path / "bridge.db"
    store = BridgeStore(database)
    store.create_user_room("大家沟通群")
    client = TestClient(create_app(database))
    assert client.post("/api/invites", json={}).status_code == 404

    registered = client.post(
        "/agent/register",
        json={
            "product": "codex",
            "username": "小团子",
            "session_alias": "群聊气氛助手",
            "conversation_id": "大家沟通群",
            "roles": ["host"],
        },
    )
    assert registered.status_code == 201
    registration = registered.json()
    assert registration["client_type"] == "codex-小团子"
    access_token = registration["access_token"]
    auth = {"Authorization": f"Bearer {access_token}"}

    assert client.post(
        "/agent/send",
        json={"conversation_id": "大家沟通群", "body": "未认证不能说话"},
    ).status_code == 401
    sent = client.post(
        "/agent/send",
        json={"conversation_id": "大家沟通群", "body": "大家好，我是小团子。"},
        headers=auth,
    )
    assert sent.status_code == 200
    assert sent.json()["body"] == "大家好，我是小团子。"
    assert "message_kind" not in sent.json()

    owner_headers = {
        "Origin": "http://testserver",
        "X-Agent-Bridge-Intent": "send-message",
    }
    owner_sent = client.post(
        "/api/rooms/%E5%A4%A7%E5%AE%B6%E6%B2%9F%E9%80%9A%E7%BE%A4/messages",
        json={"body": "你好，我是网页用户。"},
        headers=owner_headers,
    )
    assert owner_sent.status_code == 201
    assert (
        owner_sent.json()["message"]["sender_participant_id"]
        == "participant_web_owner"
    )
    owner_history = client.get(
        "/api/rooms/%E5%A4%A7%E5%AE%B6%E6%B2%9F%E9%80%9A%E7%BE%A4/messages"
    ).json()["messages"]
    assert owner_history[-1]["sender_client_type"] == "web-user"
    owner_limited = client.post(
        "/api/rooms/%E5%A4%A7%E5%AE%B6%E6%B2%9F%E9%80%9A%E7%BE%A4/messages",
        json={"body": "网页用户说得太快。"},
        headers=owner_headers,
    )
    assert owner_limited.status_code == 429
    assert 0 < owner_limited.json()["retry_after_seconds"] <= 15
    delivered_owner_message = client.post(
        "/agent/wait",
        json={"wait_seconds": 0},
        headers=auth,
    )
    assert delivered_owner_message.status_code == 200
    assert delivered_owner_message.json()["messages"][0]["message_id"] == owner_sent.json()[
        "message"
    ]["message_id"]
    acknowledged_owner_message = client.post(
        "/agent/action",
        json={
            "message_id": owner_sent.json()["message"]["message_id"],
            "action": "ack",
        },
        headers=auth,
    )
    assert acknowledged_owner_message.status_code == 200
    first_owned = client.post(
        "/agent/rooms/create",
        json={"conversation_id": "小团子的第一个房间"},
        headers=auth,
    )
    second_owned = client.post(
        "/agent/rooms/create",
        json={"conversation_id": "小团子的第二个房间"},
        headers=auth,
    )
    third_owned = client.post(
        "/agent/rooms/create",
        json={"conversation_id": "小团子的第三个房间"},
        headers=auth,
    )
    assert first_owned.json()["owned_active_room_count"] == 1
    assert second_owned.json()["owned_active_room_count"] == 2
    assert third_owned.status_code == 409
    limited = client.post(
        "/agent/send",
        json={"conversation_id": "大家沟通群", "body": "发得太快"},
        headers=auth,
    )
    assert limited.status_code == 429
    assert 0 < limited.json()["retry_after_seconds"] <= 15

    sessions_text = client.get("/api/sessions").text
    assert registration["session_id"] in sessions_text
    assert access_token not in sessions_text
    wrong_alias = client.post(
        "/agent/register",
        json={
            "product": "codex",
            "username": "小团子",
            "session_alias": "另一个会话",
            "conversation_id": "大家沟通群",
        },
    )
    assert wrong_alias.status_code == 409

    started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pending_wait = pool.submit(
            client.post,
            "/agent/wait",
            json={"wait_seconds": 2},
            headers=auth,
        )
        time.sleep(0.15)
        revoked = client.post(
            f"/api/sessions/{registration['session_id']}/revoke",
            headers={
                "Origin": "http://testserver",
                "X-Agent-Bridge-Intent": "revoke-session",
            },
        )
        revoked_wait = pending_wait.result(timeout=2)
    assert revoked.status_code == 200
    assert revoked_wait.status_code == 401
    assert time.monotonic() - started < 1.5
    assert client.post(
        "/agent/send",
        json={"conversation_id": "大家沟通群", "body": "踢出后不能说话"},
        headers=auth,
    ).status_code == 401
    for endpoint, body in (
        ("/agent/wait", {"wait_seconds": 0}),
        ("/agent/history", {"conversation_id": "大家沟通群"}),
        ("/agent/participants", {"conversation_id": "大家沟通群"}),
        ("/agent/heartbeat", {"status": "online"}),
        ("/agent/rooms/create", {"conversation_id": "撤销后不能建房"}),
    ):
        assert client.post(endpoint, json=body, headers=auth).status_code == 401


def test_lan_same_origin_user_can_chat_but_cannot_revoke_agent_session(
    tmp_path: Path,
) -> None:
    database = tmp_path / "bridge.db"
    store = BridgeStore(database)
    store.create_user_room("room-one")
    client = TestClient(
        create_app(database),
        base_url="http://192.168.1.3:8765",
        client=("192.168.1.50", 50000),
    )
    registered = client.post(
        "/agent/register",
        json={
            "conversation_id": "room-one",
            "product": "codex",
            "username": "远程成员",
            "session_alias": "远程测试",
        },
    )
    assert registered.status_code == 201

    sent = client.post(
        "/api/rooms/room-one/messages",
        json={"body": "局域网页面用户可以发言。"},
        headers={
            "Origin": "http://192.168.1.3:8765",
            "X-Agent-Bridge-Intent": "send-message",
        },
    )
    assert sent.status_code == 201
    assert sent.json()["message"]["sender_participant_id"] == "participant_web_owner"
    assert client.get("/api/rooms/room-one/messages").json()["messages"][-1][
        "sender_client_type"
    ] == "web-user"

    revoked = client.post(
        f"/api/sessions/{registered.json()['session_id']}/revoke",
        headers={
            "Origin": "http://192.168.1.3:8765",
            "X-Agent-Bridge-Intent": "revoke-session",
        },
    )
    assert revoked.status_code == 403

    cross_origin = client.post(
        "/api/rooms/room-one/messages",
        json={"body": "跨站请求不能发言。"},
        headers={
            "Origin": "https://evil.example",
            "X-Agent-Bridge-Intent": "send-message",
        },
    )
    assert cross_origin.status_code == 403
