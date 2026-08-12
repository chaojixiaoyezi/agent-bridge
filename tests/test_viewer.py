from __future__ import annotations

import concurrent.futures
import sqlite3
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from agent_bridge.store import ROOM_ABANDON_AFTER_SECONDS, BridgeStore
from agent_bridge import viewer as viewer_module
from agent_bridge.viewer import WEB_ROOT, _event_cursor, _sse_event, create_app
from agent_bridge.viewer_store import ViewerRepository


CAPTCHA_ANSWER = "ABCDE"
ADMIN_PASSWORD = "AdminSecure1!"
USER_PASSWORD = "MemberSecure1!"


def make_app(database: Path, **kwargs):
    return create_app(
        database,
        captcha_generator=lambda: CAPTCHA_ANSWER,
        **kwargs,
    )


def intent_headers(client: TestClient, intent: str) -> dict[str, str]:
    return {
        "Origin": str(client.base_url).rstrip("/"),
        "Sec-Fetch-Site": "same-origin",
        "X-Agent-Bridge-Intent": intent,
    }


def captcha(client: TestClient) -> str:
    response = client.get("/api/auth/captcha")
    assert response.status_code == 200
    challenge = response.json()["captcha"]
    assert challenge["image"].startswith("data:image/png;base64,")
    return challenge["captcha_id"]


def login_admin(client: TestClient, *, change_password: bool = True) -> dict:
    response = client.post(
        "/api/auth/login",
        headers=intent_headers(client, "login"),
        json={
            "username": "admin",
            "password": "admin",
            "captcha_id": captcha(client),
            "captcha_answer": CAPTCHA_ANSWER,
        },
    )
    assert response.status_code == 200
    user = response.json()["user"]
    assert user["is_admin"] is True
    assert user["must_change_password"] is True
    if not change_password:
        return user
    changed = client.post(
        "/api/auth/password",
        headers=intent_headers(client, "change-password"),
        json={"current_password": "admin", "new_password": ADMIN_PASSWORD},
    )
    assert changed.status_code == 200
    return changed.json()["user"]


def register_web_user(
    client: TestClient,
    *,
    username: str = "member",
    password: str = USER_PASSWORD,
) -> dict:
    response = client.post(
        "/api/auth/register",
        headers=intent_headers(client, "register"),
        json={
            "username": username,
            "password": password,
            "captcha_id": captcha(client),
            "captcha_answer": CAPTCHA_ANSWER,
        },
    )
    assert response.status_code == 201
    return response.json()["user"]


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


def test_web_login_registration_password_policy_profile_and_roles(
    tmp_path: Path,
) -> None:
    database = tmp_path / "bridge.db"
    client = TestClient(make_app(database))

    assert client.get("/api/rooms").status_code == 401
    assert client.get("/api/sessions").status_code == 401
    assert client.get("/api/events").status_code == 401
    public_health = client.get("/api/health").json()
    assert public_health["status"] == "ok"
    assert public_health["web_login_required"] is True
    assert "database" not in public_health
    assert "counts" not in public_health

    admin = login_admin(client, change_password=False)
    assert client.get("/api/rooms").status_code == 403
    weak = client.post(
        "/api/auth/password",
        headers=intent_headers(client, "change-password"),
        json={"current_password": "admin", "new_password": "stillweak"},
    )
    assert weak.status_code == 400
    changed = client.post(
        "/api/auth/password",
        headers=intent_headers(client, "change-password"),
        json={"current_password": "admin", "new_password": ADMIN_PASSWORD},
    )
    assert changed.status_code == 200
    assert changed.json()["user"]["must_change_password"] is False
    created = client.post(
        "/api/rooms",
        headers=intent_headers(client, "create-room"),
        json={"conversation_id": "无需 Agent 登录的群"},
    )
    assert created.status_code == 201
    assert admin["participant_id"] == "participant_web_owner"

    other_admin = TestClient(make_app(database))
    other_login = other_admin.post(
        "/api/auth/login",
        headers=intent_headers(other_admin, "login"),
        json={
            "username": "admin",
            "password": ADMIN_PASSWORD,
            "captcha_id": captcha(other_admin),
            "captcha_answer": CAPTCHA_ANSWER,
        },
    )
    assert other_login.status_code == 200
    assert client.post(
        "/api/auth/password",
        headers=intent_headers(client, "change-password"),
        json={
            "current_password": ADMIN_PASSWORD,
            "new_password": "AdminRotated2!",
        },
    ).status_code == 200
    assert other_admin.get("/api/auth/me").status_code == 401

    anonymous_agent = TestClient(make_app(database))
    joined = anonymous_agent.post(
        "/agent/register",
        json={
            "product": "codex",
            "username": "无需网页登录",
            "signature": "Agent 直接登记。",
            "conversation_id": "无需 Agent 登录的群",
        },
    )
    assert joined.status_code == 201
    assert joined.json()["access_token"]

    member_client = TestClient(make_app(database))
    bad_registration = member_client.post(
        "/api/auth/register",
        headers=intent_headers(member_client, "register"),
        json={
            "username": "member",
            "password": "weak",
            "captcha_id": captcha(member_client),
            "captcha_answer": CAPTCHA_ANSWER,
        },
    )
    assert bad_registration.status_code == 400
    member = register_web_user(member_client)
    assert member["is_admin"] is False
    profile = member_client.patch(
        "/api/auth/profile",
        headers=intent_headers(member_client, "update-profile"),
        json={"display_name": "普通成员", "signature": "这是我的签名。"},
    )
    assert profile.status_code == 200
    assert profile.json()["user"]["display_name"] == "普通成员"
    assert profile.json()["user"]["signature"] == "这是我的签名。"
    assert member_client.post(
        "/api/rooms",
        headers=intent_headers(member_client, "create-room"),
        json={"conversation_id": "普通用户不能建"},
    ).status_code == 403
    assert member_client.patch(
        "/api/rooms/%E6%97%A0%E9%9C%80%20Agent%20%E7%99%BB%E5%BD%95%E7%9A%84%E7%BE%A4",
        headers=intent_headers(member_client, "rename-room"),
        json={"new_conversation_id": "普通用户不能改"},
    ).status_code == 403


def test_dashboard_lists_rooms_messages_and_participants(tmp_path: Path) -> None:
    database = tmp_path / "bridge.db"
    seed(database)
    client = TestClient(make_app(database))
    login_admin(client)

    index = client.get("/")
    assert index.status_code == 200
    assert "Agent Bridge" in index.text
    assert "开启通知" in index.text
    assert "昵称审批" in index.text
    assert "清理失效" in index.text
    assert "发言频率管理" in index.text
    assert "default-src 'self'" in index.headers["content-security-policy"]
    assert "form-action 'self'" in index.headers["content-security-policy"]
    assert index.headers["cache-control"] == "no-store"

    rooms = client.get("/api/rooms").json()["rooms"]
    assert {room["conversation_id"] for room in rooms} == {"room-one", "room-two"}
    room_one = next(room for room in rooms if room["conversation_id"] == "room-one")
    assert room_one["message_count"] == 1
    assert room_one["participant_count"] == 2
    assert room_one["current_participant_count"] == 2

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
    client = TestClient(make_app(database))
    login_admin(client)

    assert client.post("/api/rooms").status_code == 403
    assert client.post(
        "/api/rooms/room-one/messages",
        json={"body": "缺少页面意图不能发言"},
    ).status_code == 403
    assert malicious not in client.get("/").text
    payload = client.get("/api/rooms/room-one/messages").json()
    assert payload["messages"][-1]["body"] == malicious
    incremental = client.get(
        f"/api/rooms/room-one/messages?after_sequence={payload['messages'][0]['sequence']}"
    ).json()
    assert [item["body"] for item in incremental["messages"]] == [malicious]
    assert incremental["has_more"] is False
    event_snapshot = ViewerRepository(database).event_snapshot(
        after_sequence=payload["messages"][0]["sequence"]
    )
    assert event_snapshot["changed_rooms"] == [
        {
            "conversation_id": "room-one",
            "message_count": 1,
            "first_sequence": payload["messages"][1]["sequence"],
            "last_sequence": payload["messages"][1]["sequence"],
        }
    ]
    assert malicious not in str(event_snapshot)
    javascript = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in javascript
    assert ".textContent" in javascript
    assert "setInterval" not in javascript
    assert "new EventSource" in javascript
    assert "captureTimelineAnchor" in javascript
    assert "after_sequence" in javascript
    assert "/api/sessions/cleanup" in javascript
    assert 'lastIndexOf("@")' in javascript
    assert "message-rates/participants/search" in javascript
    assert 'id="theme-select"' in (WEB_ROOT / "index.html").read_text(
        encoding="utf-8"
    )
    assert "requestAnimationFrame" in javascript
    assert "limit=120" in javascript
    assert "hadRenderedMessages ? isNearTimelineBottom() : true" in javascript

    stylesheet_response = client.get("/assets/app.css")
    assert stylesheet_response.headers["cache-control"] == (
        "public, max-age=31536000, immutable"
    )


def test_admin_can_repair_known_room_residents_without_changing_chat_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "bridge.db"
    _store, _sender, _receiver = seed(database)
    client = TestClient(make_app(database))
    login_admin(client)
    repaired: list[str] = []

    monkeypatch.setattr(
        viewer_module,
        "repair_known_identity_services",
        lambda client_type, **_kwargs: repaired.append(client_type)
        or {
            "resident_status": "online",
            "repaired_services": ["worker"],
        },
    )
    monkeypatch.setattr(
        viewer_module,
        "configure_existing_connector_from_disk",
        lambda _client_type: None,
    )
    with BridgeStore(database)._connection() as connection:
        before_count = int(
            connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        )
    response = client.post(
        "/api/rooms/room-one/residents/repair",
        headers=intent_headers(client, "repair-room-residents"),
        json={},
    )
    assert response.status_code == 200
    assert response.json()["online_count"] == 2
    assert sorted(repaired) == ["claude-code-小鲸鱼娘", "codex-小可爱"]
    with BridgeStore(database)._connection() as connection:
        assert int(connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]) == (
            before_count
        )

    repository = ViewerRepository(database)
    with repository._connection() as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM messages")


def test_dashboard_rejects_invalid_room_ids_and_limits(tmp_path: Path) -> None:
    database = tmp_path / "bridge.db"
    seed(database)
    client = TestClient(make_app(database))
    login_admin(client)

    assert client.get("/api/rooms/bad%5Cid/messages").status_code == 400
    assert client.get("/api/rooms?limit=not-a-number").status_code == 400
    health = client.get("/api/health").json()
    assert health["message_view_read_only"] is False
    assert health["room_creation_enabled"] is True
    assert health["owner_message_enabled"] is True
    assert health["open_registration_enabled"] is True
    assert health["counts"]["messages"] == 1


def test_dashboard_auto_clears_expired_sessions_and_keeps_manual_cleanup(
    tmp_path: Path,
) -> None:
    database = tmp_path / "bridge.db"
    store, sender, receiver = seed(database)
    with store._transaction() as connection:
        connection.execute(
            "UPDATE agent_sessions SET expires_at = ? WHERE session_id = ?",
            (time.time() - 1, sender["session_id"]),
        )
    client = TestClient(
        make_app(database),
        base_url="http://127.0.0.1:8765",
    )
    login_admin(client)

    before = client.get("/api/sessions").json()
    assert before["stats"] == {"active_count": 1, "clearable_count": 0}
    assert [item["session_id"] for item in before["sessions"]] == [
        receiver["session_id"]
    ]
    assert client.post("/api/sessions/cleanup").status_code == 403
    cleaned = client.post(
        "/api/sessions/cleanup",
        headers={
            "Origin": "http://127.0.0.1:8765",
            "X-Agent-Bridge-Intent": "clear-inactive-sessions",
        },
    )
    assert cleaned.status_code == 200
    assert cleaned.json()["cleared_count"] == 0
    after = client.get("/api/sessions").json()
    assert after["stats"] == {"active_count": 1, "clearable_count": 0}
    assert [item["session_id"] for item in after["sessions"]] == [
        receiver["session_id"]
    ]
    participants = client.get("/api/rooms/room-one/participants").json()[
        "participants"
    ]
    assert [person["participant_id"] for person in participants] == [
        receiver["participant_id"]
    ]
    room = next(
        item
        for item in client.get("/api/rooms").json()["rooms"]
        if item["conversation_id"] == "room-one"
    )
    assert room["participant_count"] == 2
    assert room["current_participant_count"] == 1
    assert client.get("/api/rooms/room-one/messages").json()["messages"][0][
        "sender_participant_id"
    ] == sender["participant_id"]


def test_sse_helpers_use_monotonic_ids_and_json_only() -> None:
    assert _event_cursor(None) == 0
    assert _event_cursor("42") == 42
    with pytest.raises(ValueError, match="non-negative"):
        _event_cursor("-1")
    encoded = _sse_event(
        "message_available",
        {"pending_count": 2, "body_included": False},
        event_id=42,
    )
    assert encoded == (
        b'id: 42\nevent: message_available\n'
        b'data: {"pending_count":2,"body_included":false}\n\n'
    )


def test_owner_event_revision_ignores_sliding_keepalives_and_clamps_cursor(
    tmp_path: Path,
) -> None:
    database = tmp_path / "bridge.db"
    store, sender, _ = seed(database)
    repository = ViewerRepository(database)
    initial = repository.event_snapshot(after_sequence=0)
    assert initial["cursor"] == 1

    # Session renewal updates last_seen/expires_at frequently. Those writes do
    # not change anything visible on the dashboard, so they must not trigger a
    # full UI refresh every listener keepalive.
    store.authenticate_session(sender["access_token"])
    after_keepalive = repository.event_snapshot(after_sequence=initial["cursor"])
    assert after_keepalive["state_revision"] == initial["state_revision"]
    assert after_keepalive["changed_rooms"] == []

    clamped = repository.event_snapshot(after_sequence=999_999_999)
    assert clamped["cursor"] == initial["cursor"]
    assert clamped["changed_rooms"] == []

    store.update_profile(
        participant_id=sender["participant_id"],
        authorized_session_id=sender["session_id"],
        signature="页面应该只在真实资料变化后刷新。",
    )
    after_profile = repository.event_snapshot(after_sequence=initial["cursor"])
    assert after_profile["state_revision"] != initial["state_revision"]


def test_same_origin_browser_user_can_create_room_without_agent_membership(
    tmp_path: Path,
) -> None:
    database = tmp_path / "bridge.db"
    client = TestClient(
        make_app(database),
        base_url="http://192.168.1.20:8765",
    )
    login_admin(client)
    headers = intent_headers(client, "create-room")
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
    assert room["participant_count"] == 1
    assert room["is_room_owner"] is True
    assert room["can_wake_all"] is True
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
            "Sec-Fetch-Site": "cross-site",
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


def test_admin_renames_room_and_generates_room_bound_agent_access(
    tmp_path: Path,
) -> None:
    database = tmp_path / "bridge.db"
    client = TestClient(make_app(database))
    login_admin(client)
    assert client.post(
        "/api/rooms",
        headers=intent_headers(client, "create-room"),
        json={"conversation_id": "old-room"},
    ).status_code == 201

    first = client.post(
        "/agent/register",
        json={
            "product": "codex",
            "username": "first-agent",
            "signature": "第一个 Agent。",
            "conversation_id": "old-room",
        },
    ).json()
    second = client.post(
        "/agent/register",
        json={
            "product": "claude-code",
            "username": "second-agent",
            "signature": "第二个 Agent。",
            "conversation_id": "old-room",
        },
    ).json()
    first_auth = {"Authorization": f"Bearer {first['access_token']}"}
    assert client.post(
        "/agent/follow",
        headers=first_auth,
        json={
            "conversation_id": "old-room",
            "followed_participant_id": second["participant_id"],
        },
    ).status_code == 200
    sent = client.post(
        "/agent/send",
        headers=first_auth,
        json={
            "conversation_id": "old-room",
            "body": "这条历史和投递必须跟随房间改名。",
            "mentions": [second["participant_id"]],
        },
    )
    assert sent.status_code == 200

    access = client.post(
        "/api/agent-access",
        headers=intent_headers(client, "generate-agent-access"),
        json={
            "conversation_id": "old-room",
            "product": "codex",
        },
    )
    assert access.status_code == 200
    generated = access.json()["access"]
    assert generated["agent_register_arguments"] == {
        "conversation_id": "old-room",
    }
    assert set(generated["agent_supplied_fields"]) == {
        "username",
        "signature",
        "roles",
        "capabilities",
        "workspace_path",
    }
    assert generated["http_registration_payload"] == {
        "product": "codex",
        "conversation_id": "old-room",
    }
    assert generated["mcp"]["env"]["AGENT_BRIDGE_CLIENT_TYPE"] == "codex"
    assert generated["mcp"]["env"]["AGENT_BRIDGE_INVITATION_TOKEN"].startswith(
        "invite_"
    )
    assert generated["requested_mode"] == "resident"
    assert generated["adapter_kind"] == "codex"
    assert generated["resident_capable"] is True
    assert generated["mcp"]["command"].endswith("/bin/agent-bridge-mcp")
    assert "Agent 无需 Web 登录" in generated["instructions"]
    assert "access_token" not in access.text
    assert "registration-authority" not in access.text
    assert "Agent 自己选择长期稳定的 username" in generated["instructions"]

    access_html = client.get("/").text
    assert '<select class="room-id-input" id="access-room" required>' in access_html
    assert 'list="agent-product-options"' in access_html
    assert '<option value="hermes"></option>' in access_html
    assert 'id="access-username"' not in access_html
    assert 'id="access-signature"' not in access_html
    assert 'id="access-roles"' not in access_html

    custom_product = client.post(
        "/api/agent-access",
        headers=intent_headers(client, "generate-agent-access"),
        json={"conversation_id": "old-room", "product": "custom-agent"},
    )
    assert custom_product.status_code == 200
    assert (
        custom_product.json()["access"]["mcp"]["env"][
            "AGENT_BRIDGE_CLIENT_TYPE"
        ]
        == "custom-agent"
    )
    assert custom_product.json()["access"]["adapter_kind"] == "manual"
    assert custom_product.json()["access"]["resident_capable"] is False

    user_identity_rejected = client.post(
        "/api/agent-access",
        headers=intent_headers(client, "generate-agent-access"),
        json={
            "conversation_id": "old-room",
            "product": "codex",
            "username": "网页不应填写",
        },
    )
    assert user_identity_rejected.status_code == 400

    renamed = client.patch(
        "/api/rooms/old-room",
        headers=intent_headers(client, "rename-room"),
        json={"new_conversation_id": "new-room"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["room"]["previous_conversation_id"] == "old-room"
    assert renamed.json()["room"]["conversation_id"] == "new-room"

    history = client.post(
        "/agent/history",
        headers=first_auth,
        json={"conversation_id": "new-room"},
    )
    assert history.status_code == 200
    assert history.json()["messages"][-1]["message_id"] == sent.json()["message_id"]
    with BridgeStore(database)._connection() as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT COUNT(*) FROM memberships WHERE conversation_id = 'old-room'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = 'new-room'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM follows WHERE conversation_id = 'new-room'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM agent_sessions "
            "WHERE registered_conversation_id = 'new-room'"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM agent_invitations "
            "WHERE conversation_id = 'new-room'"
        ).fetchone()[0] == 2


def test_dashboard_projects_and_revokes_admin_chat_authority(
    tmp_path: Path,
) -> None:
    database = tmp_path / "bridge.db"
    client = TestClient(make_app(database))
    login_admin(client)
    assert client.post(
        "/api/rooms",
        headers=intent_headers(client, "create-room"),
        json={"conversation_id": "授权聊天室"},
    ).status_code == 201
    agent = client.post(
        "/agent/register",
        json={
            "product": "codex",
            "username": "authority-target",
            "signature": "验证授权来源。",
            "conversation_id": "授权聊天室",
        },
    ).json()

    sent = client.post(
        "/api/rooms/%E6%8E%88%E6%9D%83%E8%81%8A%E5%A4%A9%E5%AE%A4/messages",
        headers=intent_headers(client, "send-message"),
        json={
            "body": "请实现这一需求并运行测试。",
            "mentions": [agent["participant_id"]],
        },
    )
    assert sent.status_code == 201
    message = sent.json()["message"]
    assert message["authorization"]["status"] == "active"

    projected = client.get(
        "/api/rooms/%E6%8E%88%E6%9D%83%E8%81%8A%E5%A4%A9%E5%AE%A4/messages"
    )
    assert projected.status_code == 200
    dashboard_message = projected.json()["messages"][-1]
    assert dashboard_message["authorization"]["issuer_username"] == "admin"

    revoked = client.post(
        f"/api/messages/{message['message_id']}/authorization/revoke",
        headers=intent_headers(client, "revoke-chat-authorization"),
        json={"reason": "方案取消"},
    )
    assert revoked.status_code == 200
    assert revoked.json()["authorization"]["status"] == "revoked"

    agent_auth = {"Authorization": f"Bearer {agent['access_token']}"}
    history = client.post(
        "/agent/history",
        headers=agent_auth,
        json={"conversation_id": "授权聊天室"},
    )
    authority = history.json()["messages"][-1]["authorization"]
    assert authority["status"] == "revoked"
    assert authority["revocation_reason"] == "方案取消"


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
    client = TestClient(make_app(database))
    login_admin(client)

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


def test_registration_secret_is_optional_and_reported_by_health(
    tmp_path: Path,
) -> None:
    database = tmp_path / "bridge.db"
    store = BridgeStore(database)
    store.create_user_room("受控登记群")
    client = TestClient(
        make_app(database, registration_secret="registration-authority")
    )
    registration = {
        "product": "codex",
        "username": "远端监听者",
        "signature": "只被事件唤醒。",
        "conversation_id": "受控登记群",
    }

    assert client.post("/agent/register", json=registration).status_code == 401
    assert (
        client.post(
            "/agent/register",
            json=registration,
            headers={"X-Agent-Bridge-Registration": "wrong"},
        ).status_code
        == 401
    )
    accepted = client.post(
        "/agent/register",
        json=registration,
        headers={"X-Agent-Bridge-Registration": "registration-authority"},
    )
    assert accepted.status_code == 201


def test_one_time_invitation_enrolls_exact_agent_and_tracks_resident_status(
    tmp_path: Path,
) -> None:
    database = tmp_path / "bridge.db"
    client = TestClient(
        make_app(database, registration_secret="global-registration-authority")
    )
    login_admin(client)
    assert client.post(
        "/api/rooms",
        headers=intent_headers(client, "create-room"),
        json={"conversation_id": "邀请值守群"},
    ).status_code == 201

    generated = client.post(
        "/api/agent-access",
        headers=intent_headers(client, "generate-agent-access"),
        json={
            "conversation_id": "邀请值守群",
            "product": "codex",
            "mode": "resident",
        },
    )
    assert generated.status_code == 200
    access = generated.json()["access"]
    invitation_token = access["mcp"]["env"]["AGENT_BRIDGE_INVITATION_TOKEN"]
    invitation_id = access["invitation"]["invitation_id"]
    assert invitation_token in access["instructions"]
    assert access["invitation"]["status"] == "active"
    assert access["invitation"]["reusable"] is False
    with BridgeStore(database)._connection() as connection:
        stored = connection.execute(
            "SELECT token_hash FROM agent_invitations WHERE invitation_id = ?",
            (invitation_id,),
        ).fetchone()
    assert stored["token_hash"] != invitation_token

    wrong_product = client.post(
        "/agent/invitations/accept",
        headers={"X-Agent-Bridge-Invitation": invitation_token},
        json={
            "product": "claude-code",
            "username": "invitee",
            "signature": "只处理明确通知。",
        },
    )
    assert wrong_product.status_code == 401

    proposed_enrollment = "enroll_" + ("a" * 48)
    accepted = client.post(
        "/agent/invitations/accept",
        headers={"X-Agent-Bridge-Invitation": invitation_token},
        json={
            "product": "codex",
            "username": "invitee",
            "signature": "只处理明确通知。",
            "roles": ["reviewer"],
            "enrollment_token": proposed_enrollment,
        },
    )
    assert accepted.status_code == 201
    registration = accepted.json()
    enrollment_token = registration["enrollment_token"]
    assert enrollment_token == proposed_enrollment
    connector_id = registration["connector_id"]
    agent_headers = {"Authorization": f"Bearer {registration['access_token']}"}
    assert registration["client_type"] == "codex-invitee"
    assert registration["setup_status"] == "awaiting_setup"
    assert client.post(
        "/agent/invitations/accept",
        headers={"X-Agent-Bridge-Invitation": invitation_token},
        json={
            "product": "codex",
            "username": "invitee",
            "signature": "重复接受应失败。",
        },
    ).status_code == 409
    retried = client.post(
        "/agent/invitations/accept",
        headers={"X-Agent-Bridge-Invitation": invitation_token},
        json={
            "product": "codex",
            "username": "invitee",
            "signature": "只处理明确通知。",
            "roles": ["reviewer"],
            "enrollment_token": proposed_enrollment,
        },
    )
    assert retried.status_code == 201
    assert retried.json()["connector_id"] == connector_id
    assert retried.json()["participant_id"] == registration["participant_id"]
    assert retried.json()["session_id"] != registration["session_id"]

    reported = client.post(
        "/agent/connector/setup",
        headers=agent_headers,
        json={
            "connector_id": connector_id,
            "setup_status": "configured",
            "detail": {
                "platform": "Darwin",
                "adapter_kind": "codex",
                "listener_service": "test-listener",
            },
        },
    )
    assert reported.status_code == 200
    BridgeStore(database).touch_agent_connector(
        participant_id=registration["participant_id"],
        authorized_session_id=registration["session_id"],
        connector_id=connector_id,
    )
    participants = client.get(
        "/api/rooms/%E9%82%80%E8%AF%B7%E5%80%BC%E5%AE%88%E7%BE%A4/participants"
    ).json()["participants"]
    invited = next(
        item
        for item in participants
        if item["participant_id"] == registration["participant_id"]
    )
    assert invited["resident_status"] == "online"
    assert invited["connector_adapter_kind"] == "codex"

    invitation_list = client.get("/api/agent-invitations").json()["invitations"]
    listed = next(item for item in invitation_list if item["invitation_id"] == invitation_id)
    assert listed["status"] == "exhausted"
    assert listed["use_count"] == 1
    assert listed["connector_count"] == 1
    assert listed["resident_status"] == "online"
    assert invitation_token not in str(listed)
    assert enrollment_token not in str(listed)

    renamed = client.patch(
        "/api/rooms/%E9%82%80%E8%AF%B7%E5%80%BC%E5%AE%88%E7%BE%A4",
        headers=intent_headers(client, "rename-room"),
        json={"new_conversation_id": "邀请值守新群"},
    )
    assert renamed.status_code == 200
    renewed = client.post(
        "/agent/register",
        headers={"X-Agent-Bridge-Enrollment": enrollment_token},
        json={
            "product": "codex",
            "username": "invitee",
            "signature": "续期仍使用同一身份。",
            "conversation_id": "邀请值守群",
        },
    )
    assert renewed.status_code == 201
    assert renewed.json()["conversation_id"] == "邀请值守新群"

    revoked = client.post(
        f"/api/agent-invitations/{invitation_id}/revoke",
        headers=intent_headers(client, "revoke-agent-invitation"),
    )
    assert revoked.status_code == 200
    assert revoked.json()["invitation"]["status"] == "revoked"
    assert client.post(
        "/agent/register",
        headers={"X-Agent-Bridge-Enrollment": enrollment_token},
        json={
            "product": "codex",
            "username": "invitee",
            "signature": "已撤销后不能续期。",
            "conversation_id": "邀请值守新群",
        },
    ).status_code == 401
    health = client.get("/api/health").json()
    assert health["open_registration_enabled"] is False
    assert health["registration_secret_required"] is True


def test_reusable_invitation_enrolls_multiple_agents_with_independent_credentials(
    tmp_path: Path,
) -> None:
    database = tmp_path / "bridge.db"
    client = TestClient(make_app(database))
    login_admin(client)
    assert client.post(
        "/api/rooms",
        headers=intent_headers(client, "create-room"),
        json={"conversation_id": "多人邀请群"},
    ).status_code == 201

    generated = client.post(
        "/api/agent-access",
        headers=intent_headers(client, "generate-agent-access"),
        json={
            "conversation_id": "多人邀请群",
            "product": "codex",
            "mode": "resident",
            "reusable": True,
        },
    )
    assert generated.status_code == 200
    access = generated.json()["access"]
    invitation = access["invitation"]
    invitation_id = invitation["invitation_id"]
    invitation_token = access["mcp"]["env"]["AGENT_BRIDGE_INVITATION_TOKEN"]
    assert access["reusable"] is True
    assert invitation["reuse_policy"] == "reusable"
    assert invitation["max_uses"] is None
    assert invitation["status"] == "active"
    assert "多个不同 Agent" in access["instructions"]

    registrations = []
    enrollment_tokens = []
    for index, username in enumerate(("multi-one", "multi-two"), start=1):
        enrollment_token = "enroll_" + (str(index) * 48)
        response = client.post(
            "/agent/invitations/accept",
            headers={"X-Agent-Bridge-Invitation": invitation_token},
            json={
                "product": "codex",
                "username": username,
                "signature": f"第 {index} 个独立接入。",
                "enrollment_token": enrollment_token,
            },
        )
        assert response.status_code == 201
        registrations.append(response.json())
        enrollment_tokens.append(enrollment_token)

    assert len({item["connector_id"] for item in registrations}) == 2
    assert len({item["participant_id"] for item in registrations}) == 2
    assert all(item["invitation_reusable"] is True for item in registrations)

    duplicate_identity = client.post(
        "/agent/invitations/accept",
        headers={"X-Agent-Bridge-Invitation": invitation_token},
        json={
            "product": "codex",
            "username": "multi-one",
            "signature": "不能领取第二份凭据。",
            "enrollment_token": "enroll_" + ("3" * 48),
        },
    )
    assert duplicate_identity.status_code == 409

    retry = client.post(
        "/agent/invitations/accept",
        headers={"X-Agent-Bridge-Invitation": invitation_token},
        json={
            "product": "codex",
            "username": "multi-one",
            "signature": "第 1 个独立接入。",
            "enrollment_token": enrollment_tokens[0],
        },
    )
    assert retry.status_code == 201
    assert retry.json()["connector_id"] == registrations[0]["connector_id"]
    assert retry.json()["session_id"] != registrations[0]["session_id"]

    first_headers = {
        "Authorization": f"Bearer {registrations[0]['access_token']}"
    }
    assert client.post(
        "/agent/connector/setup",
        headers=first_headers,
        json={
            "connector_id": registrations[0]["connector_id"],
            "setup_status": "configured",
            "detail": {"listener_service": "multi-one-listener"},
        },
    ).status_code == 200
    BridgeStore(database).touch_agent_connector(
        participant_id=registrations[0]["participant_id"],
        authorized_session_id=registrations[0]["session_id"],
        connector_id=registrations[0]["connector_id"],
    )

    listed = next(
        item
        for item in client.get("/api/agent-invitations").json()["invitations"]
        if item["invitation_id"] == invitation_id
    )
    assert listed["status"] == "active"
    assert listed["use_count"] == 2
    assert listed["connector_count"] == 2
    assert listed["active_connector_count"] == 2
    assert listed["online_connector_count"] == 1
    assert invitation_token not in str(listed)
    assert all(secret not in str(listed) for secret in enrollment_tokens)

    with BridgeStore(database)._transaction() as connection:
        connection.execute(
            "UPDATE agent_invitations SET expires_at = ? WHERE invitation_id = ?",
            (time.time() - 1, invitation_id),
        )
    expired = next(
        item
        for item in client.get("/api/agent-invitations").json()["invitations"]
        if item["invitation_id"] == invitation_id
    )
    assert expired["status"] == "expired"
    assert client.post(
        "/agent/invitations/accept",
        headers={"X-Agent-Bridge-Invitation": invitation_token},
        json={
            "product": "codex",
            "username": "multi-three",
            "signature": "到期后不能新增。",
        },
    ).status_code == 409

    renewed = client.post(
        "/agent/register",
        headers={"X-Agent-Bridge-Enrollment": enrollment_tokens[1]},
        json={
            "product": "codex",
            "username": "multi-two",
            "signature": "到期不影响已签发连接。",
            "conversation_id": "多人邀请群",
        },
    )
    assert renewed.status_code == 201
    assert renewed.json()["connector_id"] == registrations[1]["connector_id"]

    revoked = client.post(
        f"/api/agent-invitations/{invitation_id}/revoke",
        headers=intent_headers(client, "revoke-agent-invitation"),
    )
    assert revoked.status_code == 200
    assert revoked.json()["invitation"]["status"] == "revoked"
    assert revoked.json()["invitation"]["active_connector_count"] == 0
    for index, enrollment_token in enumerate(enrollment_tokens, start=1):
        assert client.post(
            "/agent/register",
            headers={"X-Agent-Bridge-Enrollment": enrollment_token},
            json={
                "product": "codex",
                "username": f"multi-{'one' if index == 1 else 'two'}",
                "signature": "撤销后不能续期。",
                "conversation_id": "多人邀请群",
            },
        ).status_code == 401
    with BridgeStore(database)._connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM agent_connectors "
            "WHERE invitation_id = ? AND revoked_at IS NOT NULL",
            (invitation_id,),
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM agent_sessions "
            "WHERE connector_id IN (SELECT connector_id FROM agent_connectors "
            "WHERE invitation_id = ?) AND revoked_at IS NULL",
            (invitation_id,),
        ).fetchone()[0] == 0


def test_open_registration_owner_chat_and_authenticated_agent_http_flow(
    tmp_path: Path,
) -> None:
    database = tmp_path / "bridge.db"
    store = BridgeStore(database)
    store.create_user_room("大家沟通群")
    client = TestClient(make_app(database))
    admin = login_admin(client)
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
    assert registration["display_name"] == "codex-小团子"
    assert registration["signature"] == "群聊气氛助手"
    access_token = registration["access_token"]
    auth = {"Authorization": f"Bearer {access_token}"}

    profile = client.post(
        "/agent/profile",
        json={"signature": "喜欢把复杂协作讲清楚。"},
        headers=auth,
    )
    assert profile.status_code == 200
    assert profile.json()["signature"] == "喜欢把复杂协作讲清楚。"
    nickname = client.post(
        "/agent/nickname/request",
        json={"display_name": "小团子"},
        headers=auth,
    )
    assert nickname.status_code == 200
    request_id = nickname.json()["request_id"]
    listed = client.get("/api/nickname-requests").json()["requests"]
    assert [item["request_id"] for item in listed] == [request_id]
    reviewed = client.post(
        f"/api/nickname-requests/{request_id}/review",
        json={"action": "approve"},
        headers=intent_headers(client, "review-nickname"),
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["request"]["current_display_name"] == "小团子"
    assert reviewed.json()["request"]["reviewed_by_web_user_id"] == admin["user_id"]
    assert client.post(
        "/agent/nickname/request",
        json={"display_name": "一天内第二次"},
        headers=auth,
    ).status_code == 429

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

    owner_headers = intent_headers(client, "send-message")
    owner_sent = client.post(
        "/api/rooms/%E5%A4%A7%E5%AE%B6%E6%B2%9F%E9%80%9A%E7%BE%A4/messages",
        json={
            "body": "你好，@小团子，我是网页用户。",
            "mentions": [registration["participant_id"]],
        },
        headers=owner_headers,
    )
    assert owner_sent.status_code == 201
    assert (
        owner_sent.json()["message"]["sender_participant_id"]
        == "participant_web_owner"
    )
    notification = client.post(
        "/agent/notifications",
        json={"after_sequence": 0},
        headers=auth,
    )
    assert notification.status_code == 200
    assert notification.json()["has_new"] is True
    assert notification.json()["backlog"]["pending_count"] == 1
    assert "你好，@小团子，我是网页用户。" not in notification.text
    assert client.get("/agent/events").status_code == 401
    assert client.get(
        "/agent/events",
        headers={**auth, "Last-Event-ID": "not-a-number"},
    ).status_code == 400
    owner_history = client.get(
        "/api/rooms/%E5%A4%A7%E5%AE%B6%E6%B2%9F%E9%80%9A%E7%BE%A4/messages"
    ).json()["messages"]
    assert owner_history[-1]["sender_client_type"] == "web-user"
    owner_second = client.post(
        "/api/rooms/%E5%A4%A7%E5%AE%B6%E6%B2%9F%E9%80%9A%E7%BE%A4/messages",
        json={"body": "管理员可以连续发言。"},
        headers=owner_headers,
    )
    assert owner_second.status_code == 201
    delivered_owner_message = client.post(
        "/agent/wait",
        json={"wait_seconds": 0},
        headers=auth,
    )
    assert delivered_owner_message.status_code == 200
    assert delivered_owner_message.json()["messages"][0]["message_id"] == owner_sent.json()[
        "message"
    ]["message_id"]
    assert delivered_owner_message.json()["messages"][0]["mentions"] == [
        registration["participant_id"]
    ]
    assert delivered_owner_message.json()["messages"][0]["delivery"][
        "priority"
    ] == "mention"
    acknowledged_owner_message = client.post(
        "/agent/action",
        json={
            "message_id": owner_sent.json()["message"]["message_id"],
            "action": "ack",
        },
        headers=auth,
    )
    assert acknowledged_owner_message.status_code == 200
    assert client.post(
        "/agent/action",
        json={
            "message_id": owner_second.json()["message"]["message_id"],
            "action": "ack",
        },
        headers=auth,
    ).status_code == 200
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
    reconnected = client.post(
        "/agent/register",
        json={
            "product": "codex",
            "username": "小团子",
            "session_alias": "另一个会话",
            "conversation_id": "大家沟通群",
        },
    )
    assert reconnected.status_code == 201
    assert reconnected.json()["participant_id"] == registration["participant_id"]
    assert reconnected.json()["display_name"] == "小团子"
    assert reconnected.json()["signature"] == "喜欢把复杂协作讲清楚。"

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
            headers=intent_headers(client, "revoke-session"),
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
        make_app(database),
        base_url="http://192.168.1.3:8765",
        client=("192.168.1.50", 50000),
    )
    web_user = register_web_user(client, username="lan-member")
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
            "Sec-Fetch-Site": "same-origin",
            "X-Agent-Bridge-Intent": "send-message",
        },
    )
    assert sent.status_code == 201
    assert sent.json()["message"]["sender_participant_id"] == web_user["participant_id"]
    assert client.get("/api/rooms/room-one/messages").json()["messages"][-1][
        "sender_client_type"
    ].startswith("web-user-")

    limited = client.post(
        "/api/rooms/room-one/messages",
        json={"body": "普通用户一分钟内不能再发。"},
        headers=intent_headers(client, "send-message"),
    )
    assert limited.status_code == 429
    assert 0 < limited.json()["retry_after_seconds"] <= 60
    assert client.get("/api/sessions").status_code == 403

    revoked = client.post(
        f"/api/sessions/{registered.json()['session_id']}/revoke",
        headers={
            "Origin": "http://192.168.1.3:8765",
            "Sec-Fetch-Site": "same-origin",
            "X-Agent-Bridge-Intent": "revoke-session",
        },
    )
    assert revoked.status_code == 403

    cross_origin = client.post(
        "/api/rooms/room-one/messages",
        json={"body": "跨站请求不能发言。"},
        headers={
            "Origin": "https://evil.example",
            "Sec-Fetch-Site": "cross-site",
            "X-Agent-Bridge-Intent": "send-message",
        },
    )
    assert cross_origin.status_code == 403


def test_admin_manages_global_and_individual_message_rates(
    tmp_path: Path,
) -> None:
    database = tmp_path / "bridge.db"
    store, agent, _ = seed(database)
    admin_client = TestClient(make_app(database))
    admin = login_admin(admin_client)
    member_client = TestClient(make_app(database))
    member = register_web_user(member_client, username="rate-member")

    defaults = admin_client.get("/api/message-rates")
    assert defaults.status_code == 200
    assert defaults.json()["resolution"] == "minimum"
    assert defaults.json()["globals"]["agent"]["cooldown_seconds"] == 15
    assert defaults.json()["globals"]["web_user"]["cooldown_seconds"] == 60
    assert member_client.get("/api/message-rates").status_code == 403
    assert member_client.patch(
        "/api/message-rates/global/agent",
        headers=intent_headers(member_client, "update-global-message-rate"),
        json={"cooldown_seconds": 1},
    ).status_code == 403

    agent_search = admin_client.get(
        "/api/message-rates/participants/search",
        params={"query": "小鲸鱼娘", "actor_kind": "agent"},
    )
    assert agent_search.status_code == 200
    agent_rate = next(
        item
        for item in agent_search.json()["participants"]
        if item["participant_id"] == agent["participant_id"]
    )
    assert agent_rate["effective_cooldown_seconds"] == 15

    member_search = admin_client.get(
        "/api/message-rates/participants/search",
        params={"query": "rate-member", "actor_kind": "web_user"},
    )
    assert member_search.status_code == 200
    assert [item["participant_id"] for item in member_search.json()["participants"]] == [
        member["participant_id"]
    ]
    assert all(
        item["participant_id"] != "participant_web_owner"
        for item in admin_client.get(
            "/api/message-rates/participants/search",
            params={"query": "", "actor_kind": "all"},
        ).json()["participants"]
    )

    revision_before = ViewerRepository(database).event_snapshot()["state_revision"][-1]
    updated_global = admin_client.patch(
        "/api/message-rates/global/agent",
        headers=intent_headers(admin_client, "update-global-message-rate"),
        json={"cooldown_seconds": 5},
    )
    assert updated_global.status_code == 200
    assert updated_global.json()["global"]["updated_by_web_user_id"] == admin["user_id"]
    longer_individual = admin_client.put(
        f"/api/message-rates/participants/{agent['participant_id']}",
        headers=intent_headers(admin_client, "set-participant-message-rate"),
        json={"cooldown_seconds": 10},
    )
    assert longer_individual.status_code == 200
    assert longer_individual.json()["participant"]["effective_cooldown_seconds"] == 5
    assert ViewerRepository(database).event_snapshot()["state_revision"][-1] > revision_before

    with store._transaction() as connection:
        connection.execute(
            "UPDATE messages SET created_at = ? "
            "WHERE sender_participant_id = ? AND conversation_id = 'room-one'",
            (time.time() - 6, agent["participant_id"]),
        )
    global_shorter = store.send(
        authorized_session_id=agent["session_id"],
        sender_participant_id=agent["participant_id"],
        conversation_id="room-one",
        body_text="整体五秒比单独十秒短，所以可以发送。",
    )

    shorter_individual = admin_client.put(
        f"/api/message-rates/participants/{agent['participant_id']}",
        headers=intent_headers(admin_client, "set-participant-message-rate"),
        json={"cooldown_seconds": 2},
    )
    assert shorter_individual.status_code == 200
    assert shorter_individual.json()["participant"]["effective_cooldown_seconds"] == 2
    with store._transaction() as connection:
        connection.execute(
            "UPDATE messages SET created_at = ? WHERE message_id = ?",
            (time.time() - 3, global_shorter["message_id"]),
        )
    individual_shorter = store.send(
        authorized_session_id=agent["session_id"],
        sender_participant_id=agent["participant_id"],
        conversation_id="room-one",
        body_text="单独两秒比整体五秒短，所以也可以发送。",
    )
    with pytest.raises(sqlite3.IntegrityError, match="MESSAGE_RATE_LIMITED"):
        with store._transaction() as connection:
            direct_created_at = individual_shorter["created_at"] + 1
            connection.execute(
                """
                INSERT INTO messages
                    (message_id, conversation_id, sender_participant_id,
                     audience_kind, audience_value, message_kind, body,
                     refs_json, status, authorized_session_id,
                     created_at, updated_at)
                VALUES (?, ?, ?, 'room', '*', 'message', ?, '[]', 'open', ?, ?, ?)
                """,
                (
                    "msg_dynamic_rate_bypass",
                    "room-one",
                    agent["participant_id"],
                    "数据库触发器仍应阻止绕过。",
                    agent["session_id"],
                    direct_created_at,
                    direct_created_at,
                ),
            )

    assert admin_client.patch(
        "/api/message-rates/global/web_user",
        headers=intent_headers(admin_client, "update-global-message-rate"),
        json={"cooldown_seconds": 20},
    ).status_code == 200
    member_override = admin_client.put(
        f"/api/message-rates/participants/{member['participant_id']}",
        headers=intent_headers(admin_client, "set-participant-message-rate"),
        json={"cooldown_seconds": 5},
    )
    assert member_override.status_code == 200
    assert member_override.json()["participant"]["effective_cooldown_seconds"] == 5
    member_health = member_client.get("/api/health").json()["message_rate_limits"]
    assert member_health["current_user_effective_cooldown_seconds"] == 5

    first_member_message = member_client.post(
        "/api/rooms/room-one/messages",
        headers=intent_headers(member_client, "send-message"),
        json={"body": "普通用户第一条。"},
    )
    assert first_member_message.status_code == 201
    assert member_client.post(
        "/api/rooms/room-one/messages",
        headers=intent_headers(member_client, "send-message"),
        json={"body": "五秒内仍会限频。"},
    ).status_code == 429
    with store._transaction() as connection:
        connection.execute(
            "UPDATE messages SET created_at = ? WHERE message_id = ?",
            (time.time() - 6, first_member_message.json()["message"]["message_id"]),
        )
    assert member_client.post(
        "/api/rooms/room-one/messages",
        headers=intent_headers(member_client, "send-message"),
        json={"body": "单独五秒后可以发送。"},
    ).status_code == 201

    cleared = admin_client.delete(
        f"/api/message-rates/participants/{member['participant_id']}",
        headers=intent_headers(admin_client, "clear-participant-message-rate"),
    )
    assert cleared.status_code == 200
    assert cleared.json()["participant"]["individual_cooldown_seconds"] is None
    assert cleared.json()["participant"]["effective_cooldown_seconds"] == 20
    assert member_client.get("/api/health").json()["message_rate_limits"][
        "current_user_effective_cooldown_seconds"
    ] == 20

    assert admin_client.patch(
        "/api/message-rates/global/agent",
        headers=intent_headers(admin_client, "update-global-message-rate"),
        json={"cooldown_seconds": 86401},
    ).status_code == 400
    assert admin_client.put(
        f"/api/message-rates/participants/{agent['participant_id']}",
        headers=intent_headers(admin_client, "set-participant-message-rate"),
        json={"cooldown_seconds": True},
    ).status_code == 400


def test_admin_agent_lifecycle_kick_migration_and_jump_button_ui(
    tmp_path: Path,
) -> None:
    database = tmp_path / "bridge.db"
    admin_client = TestClient(make_app(database))
    login_admin(admin_client)
    member_client = TestClient(make_app(database))
    register_web_user(member_client, username="member-manager-check")
    for room in ("api-source-a", "api-target-b", "api-source-c"):
        assert admin_client.post(
            "/api/rooms",
            headers=intent_headers(admin_client, "create-room"),
            json={"conversation_id": room},
        ).status_code == 201

    agents = []
    for room, username in (
        ("api-source-a", "api-a"),
        ("api-source-c", "api-c"),
        ("api-source-a", "api-kick"),
    ):
        response = admin_client.post(
            "/agent/register",
            json={
                "product": "codex",
                "username": username,
                "signature": f"{username} 签名",
                "conversation_id": room,
            },
        )
        assert response.status_code == 201
        agents.append(response.json())

    lifecycle = admin_client.get("/api/agent-lifecycle")
    assert lifecycle.status_code == 200
    assert lifecycle.json()["inactivity_days"] == 10
    assert member_client.get("/api/agent-lifecycle").status_code == 403
    assert member_client.get("/api/admin/room-members").status_code == 403
    updated = admin_client.patch(
        "/api/agent-lifecycle",
        headers=intent_headers(admin_client, "update-agent-lifecycle"),
        json={"inactivity_days": 20},
    )
    assert updated.status_code == 200
    assert updated.json()["inactivity_days"] == 20
    assert admin_client.patch(
        "/api/agent-lifecycle",
        json={"inactivity_days": 21},
    ).status_code == 403

    members = admin_client.get("/api/admin/room-members")
    assert members.status_code == 200
    by_room = {
        room["conversation_id"]: room["agents"]
        for room in members.json()["rooms"]
    }
    assert set(by_room) == {"api-source-a", "api-target-b", "api-source-c"}
    assert len(by_room["api-source-a"]) == 2
    assert by_room["api-target-b"] == []

    kick_path = (
        "/api/rooms/api-source-a/participants/"
        f"{agents[2]['participant_id']}/kick"
    )
    assert member_client.post(
        kick_path,
        headers=intent_headers(member_client, "kick-agent"),
    ).status_code == 403
    kicked = admin_client.post(
        kick_path,
        headers=intent_headers(admin_client, "kick-agent"),
    )
    assert kicked.status_code == 200
    assert kicked.json()["agent"]["history_preserved"] is True

    migration_payload = {
        "target_conversation_id": "api-target-b",
        "selections": [
            {
                "source_conversation_id": "api-source-a",
                "participant_ids": [agents[0]["participant_id"]],
            },
            {
                "source_conversation_id": "api-source-c",
                "participant_ids": [agents[1]["participant_id"]],
            },
        ],
    }
    assert member_client.post(
        "/api/room-memberships/migrate",
        headers=intent_headers(member_client, "migrate-agents"),
        json=migration_payload,
    ).status_code == 403
    migrated = admin_client.post(
        "/api/room-memberships/migrate",
        headers=intent_headers(admin_client, "migrate-agents"),
        json=migration_payload,
    )
    assert migrated.status_code == 200
    assert migrated.json()["migration"]["membership_count"] == 2
    assert migrated.json()["migration"]["source_memberships_preserved"] is True
    target_participants = admin_client.get(
        "/api/rooms/api-target-b/participants"
    ).json()["participants"]
    assert {
        participant["participant_id"]
        for participant in target_participants
        if participant["membership_active"]
    }.issuperset({agents[0]["participant_id"], agents[1]["participant_id"]})

    html = admin_client.get("/").text
    javascript = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
    stylesheet = (WEB_ROOT / "app.css").read_text(encoding="utf-8")
    assert 'id="new-message-indicator"' in html
    assert 'd="M12 4v14m-6-6 6 6 6-6"' in html
    assert 'id="member-management-dialog"' in html
    assert 'id="repair-residents"' in html
    assert "复制加入目标群" in html
    assert "state.messages.length > 0 && !isNearTimelineBottom()" in javascript
    assert 'behavior: "smooth"' in javascript
    assert "/api/room-memberships/migrate" in javascript
    assert ".new-message-indicator svg" in stylesheet
    assert ':root[data-theme="ocean"]' in stylesheet
    assert "content-visibility: auto" in stylesheet
