from __future__ import annotations

import concurrent.futures
import sqlite3
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from agent_bridge.store import ROOM_ABANDON_AFTER_SECONDS, BridgeStore
from agent_bridge import viewer as viewer_module
from agent_bridge.security import (
    DEFAULT_HSTS_SECONDS,
    PUBLIC_WEB_SESSION_COOKIE,
    PUBLIC_WEB_SESSION_TTL_SECONDS,
    ViewerSecurityConfigurationError,
    ViewerSecurityPolicy,
)
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


def public_security_policy(
    *,
    registration_mode: str = "closed",
    registration_secret: str | None = None,
) -> ViewerSecurityPolicy:
    return ViewerSecurityPolicy(
        public_mode=True,
        allowed_hosts=("bridge.example",),
        allowed_origins=frozenset({"https://bridge.example"}),
        web_registration_mode=registration_mode,
        web_registration_secret=registration_secret,
        secure_cookies=True,
        web_session_cookie_name=PUBLIC_WEB_SESSION_COOKIE,
        web_session_ttl_seconds=PUBLIC_WEB_SESSION_TTL_SECONDS,
        forwarded_allow_ips="127.0.0.1",
        hsts_seconds=DEFAULT_HSTS_SECONDS,
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


def grant_web_room_access(
    database: Path,
    *,
    user: dict,
    room: str,
) -> dict:
    store = BridgeStore(database)
    with store._connection() as connection:
        admin_id = str(
            connection.execute(
                "SELECT user_id FROM web_users WHERE username = 'admin'"
            ).fetchone()[0]
        )
    return store.manage_room_web_member(
        requesting_web_user_id=admin_id,
        conversation_id=room,
        target_web_user_id=str(user["user_id"]),
        active=True,
    )


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
    joined_payload = joined.json()
    assert joined_payload["access_token"]
    agent_avatars = anonymous_agent.post(
        "/agent/avatars",
        headers={"Authorization": f"Bearer {joined_payload['access_token']}"},
        json={"vendor": "gpt"},
    )
    assert agent_avatars.status_code == 200
    assert len(agent_avatars.json()["groups"][0]["avatars"]) == 8
    avatar_profile = anonymous_agent.post(
        "/agent/profile",
        headers={"Authorization": f"Bearer {joined_payload['access_token']}"},
        json={"avatar_key": "gpt-04-skeptical"},
    )
    assert avatar_profile.status_code == 200
    assert avatar_profile.json()["avatar_key"] == "gpt-04-skeptical"
    room_dnd = anonymous_agent.post(
        "/agent/room-dnd",
        headers={"Authorization": f"Bearer {joined_payload['access_token']}"},
        json={"conversation_id": "无需 Agent 登录的群", "enabled": True},
    )
    assert room_dnd.status_code == 200
    assert room_dnd.json()["active"] is True
    assert room_dnd.json()["expires_at"] > room_dnd.json()["enabled_at"]

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
        json={
            "display_name": "普通成员",
            "signature": "这是我的签名。",
            "avatar_key": "qwen-03-curious-question",
        },
    )
    assert profile.status_code == 200
    assert profile.json()["user"]["display_name"] == "普通成员"
    assert profile.json()["user"]["signature"] == "这是我的签名。"
    assert profile.json()["user"]["avatar_key"] == "qwen-03-curious-question"
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


def test_private_web_rooms_require_explicit_access_and_revocation_isolated(
    tmp_path: Path,
) -> None:
    database = tmp_path / "private-rooms.db"
    store, _sender, _receiver = seed(database)
    room_two_agent = store.register_agent_session(
        conversation_id="room-two",
        product="future-agent",
        username="未来伙伴",
        session_alias="另一个房间",
    )
    store.send(
        authorized_session_id=room_two_agent["session_id"],
        sender_participant_id=room_two_agent["participant_id"],
        conversation_id="room-two",
        body_text="这条隐藏消息不能泄露给 room-one 用户。",
    )

    admin_client = TestClient(make_app(database))
    login_admin(admin_client)
    member_client = TestClient(make_app(database))
    member = register_web_user(member_client, username="private-member")

    admin_rooms = {
        room["conversation_id"]
        for room in admin_client.get("/api/rooms").json()["rooms"]
    }
    assert {"room-one", "room-two"}.issubset(admin_rooms)
    assert member_client.get("/api/rooms").json()["rooms"] == []
    member_health = member_client.get("/api/health").json()
    assert "database" not in member_health
    assert "counts" not in member_health
    assert "security" not in member_health
    for path in (
        "/api/rooms/room-one/messages",
        "/api/rooms/room-one/search?q=事务",
        "/api/rooms/room-one/receipts",
        "/api/rooms/room-one/participants",
        "/api/rooms/room-one/wake-policy",
        "/api/rooms/room-one/task-permissions",
    ):
        assert member_client.get(path).status_code == 403
    denied_send = member_client.post(
        "/api/rooms/room-one/messages",
        headers=intent_headers(member_client, "send-message"),
        json={"body": "不能通过猜 URL 加入。"},
    )
    assert denied_send.status_code == 403

    candidates = admin_client.get(
        "/api/admin/rooms/room-one/web-users",
        params={"query": "private-member"},
    )
    assert candidates.status_code == 200
    assert [item["user_id"] for item in candidates.json()["users"]] == [
        member["user_id"]
    ]
    granted = admin_client.put(
        f"/api/admin/rooms/room-one/web-users/{member['user_id']}",
        headers=intent_headers(admin_client, "invite-room-web-user"),
    )
    assert granted.status_code == 200
    assert granted.json()["user"]["has_room_access"] is True

    visible = member_client.get("/api/rooms").json()["rooms"]
    assert [room["conversation_id"] for room in visible] == ["room-one"]
    assert member_client.get("/api/rooms/room-one/messages").status_code == 200
    assert member_client.get("/api/rooms/room-two/messages").status_code == 403
    sent = member_client.post(
        "/api/rooms/room-one/messages",
        headers=intent_headers(member_client, "send-message"),
        json={"body": "获准后可以正常聊天。"},
    )
    assert sent.status_code == 201

    with sqlite3.connect(database) as connection:
        agent_memberships_before = connection.execute(
            "SELECT COUNT(*) FROM memberships AS membership "
            "LEFT JOIN web_users AS web_user "
            "ON web_user.participant_id = membership.participant_id "
            "WHERE web_user.user_id IS NULL"
        ).fetchone()[0]
        messages_before = connection.execute(
            "SELECT COUNT(*) FROM messages"
        ).fetchone()[0]

    revoked = admin_client.delete(
        f"/api/admin/rooms/room-one/web-users/{member['user_id']}",
        headers=intent_headers(admin_client, "remove-room-web-user"),
    )
    assert revoked.status_code == 200
    assert revoked.json()["user"]["has_room_access"] is False
    assert member_client.get("/api/rooms").json()["rooms"] == []
    assert member_client.get("/api/rooms/room-one/messages").status_code == 403
    convert_after_revocation = member_client.post(
        f"/api/messages/{sent.json()['message']['message_id']}/convert-to-task",
        headers=intent_headers(member_client, "convert-message-to-task"),
        json={},
    )
    assert convert_after_revocation.status_code == 403
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM memberships AS membership "
            "LEFT JOIN web_users AS web_user "
            "ON web_user.participant_id = membership.participant_id "
            "WHERE web_user.user_id IS NULL"
        ).fetchone()[0] == agent_memberships_before
        assert connection.execute(
            "SELECT COUNT(*) FROM messages"
        ).fetchone()[0] == messages_before

    room_permission = admin_client.patch(
        f"/api/admin/web-users/{member['user_id']}/room-permission",
        headers=intent_headers(admin_client, "manage-room-permission"),
        json={"can_create_rooms": True, "room_limit": 1},
    )
    assert room_permission.status_code == 200
    owned = member_client.post(
        "/api/rooms",
        headers=intent_headers(member_client, "create-room"),
        json={"conversation_id": "private-member-owned"},
    )
    assert owned.status_code == 201
    assert [
        room["conversation_id"]
        for room in member_client.get("/api/rooms").json()["rooms"]
    ] == ["private-member-owned"]

    scoped_event = ViewerRepository(database).event_snapshot(
        after_sequence=0,
        visible_conversation_ids=["room-one"],
        include_admin_state=False,
    )
    assert [item["conversation_id"] for item in scoped_event["changed_rooms"]] == [
        "room-one"
    ]
    assert "room-two" not in str(scoped_event)
    assert "这条隐藏消息" not in str(scoped_event)


def test_public_mode_fails_closed_and_enforces_transport_host_cookie_and_body(
    tmp_path: Path,
) -> None:
    database = tmp_path / "bridge.db"
    policy = public_security_policy()
    agent_registration_secret = "agent-registration-secret-" + "x" * 32

    with pytest.raises(
        ViewerSecurityConfigurationError,
        match="default admin password",
    ):
        create_app(
            database,
            registration_secret=agent_registration_secret,
            captcha_generator=lambda: CAPTCHA_ANSWER,
            security_policy=policy,
        )

    bootstrap = TestClient(make_app(database), base_url="http://bridge.test")
    login_admin(bootstrap)
    with pytest.raises(
        ViewerSecurityConfigurationError,
        match="Agent registration secret",
    ):
        create_app(
            database,
            registration_secret="too-short",
            captcha_generator=lambda: CAPTCHA_ANSWER,
            security_policy=policy,
        )

    app = create_app(
        database,
        registration_secret=agent_registration_secret,
        captcha_generator=lambda: CAPTCHA_ANSWER,
        security_policy=policy,
    )
    client = TestClient(app, base_url="https://bridge.example")
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["public_security_mode"] is True
    assert health.json()["web_registration_mode"] == "closed"
    assert health.headers["strict-transport-security"] == (
        f"max-age={DEFAULT_HSTS_SECONDS}"
    )
    assert health.headers["permissions-policy"] == (
        "camera=(), geolocation=(), microphone=(), payment=(), usb=()"
    )
    assert health.headers["cross-origin-opener-policy"] == "same-origin"
    assert health.headers["cross-origin-resource-policy"] == "same-origin"
    assert health.headers["x-request-id"].startswith("req_")

    login = client.post(
        "/api/auth/login",
        headers=intent_headers(client, "login"),
        json={
            "username": "admin",
            "password": ADMIN_PASSWORD,
            "captcha_id": captcha(client),
            "captcha_answer": CAPTCHA_ANSWER,
        },
    )
    assert login.status_code == 200
    cookie = login.headers["set-cookie"]
    assert cookie.startswith(f"{PUBLIC_WEB_SESSION_COOKIE}=")
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=strict" in cookie
    assert f"Max-Age={PUBLIC_WEB_SESSION_TTL_SECONDS}" in cookie
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT ttl_seconds FROM web_sessions ORDER BY created_at DESC LIMIT 1"
        ).fetchone()[0] == PUBLIC_WEB_SESSION_TTL_SECONDS

    assert client.post(
        "/api/auth/logout",
        headers={
            "Origin": "https://evil.example",
            "Sec-Fetch-Site": "cross-site",
            "X-Agent-Bridge-Intent": "logout",
        },
    ).status_code == 403
    assert client.post(
        "/api/auth/logout",
        headers={
            "Origin": "https://bridge.example:invalid",
            "Sec-Fetch-Site": "same-origin",
            "X-Agent-Bridge-Intent": "logout",
        },
    ).status_code == 403
    assert client.get(
        "/api/health",
        headers={"Host": "evil.example"},
    ).status_code == 400
    insecure_client = TestClient(app, base_url="http://bridge.example")
    assert insecure_client.get("/api/health").status_code == 400
    oversized = client.post(
        "/agent/register",
        headers={
            "Content-Type": "application/json",
            "X-Agent-Bridge-Registration": agent_registration_secret,
        },
        content=b"{" + b"x" * 70_001 + b"}",
    )
    assert oversized.status_code == 413
    assert client.post(
        "/api/auth/register",
        headers=intent_headers(client, "register"),
        json={},
    ).status_code == 403
    assert client.post(
        "/agent/register",
        json={
            "product": "codex",
            "username": "public-probe",
            "conversation_id": "missing-room",
        },
    ).status_code == 401


def test_public_security_environment_requires_exact_trust_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = (
        "AGENT_BRIDGE_PUBLIC_MODE",
        "AGENT_BRIDGE_ALLOWED_HOSTS",
        "AGENT_BRIDGE_ALLOWED_ORIGINS",
        "AGENT_BRIDGE_FORWARDED_ALLOW_IPS",
        "AGENT_BRIDGE_TLS_CERT_FILE",
        "AGENT_BRIDGE_TLS_KEY_FILE",
        "AGENT_BRIDGE_WEB_REGISTRATION_MODE",
        "AGENT_BRIDGE_WEB_REGISTRATION_SECRET",
        "AGENT_BRIDGE_WEB_REGISTRATION_SECRET_FILE",
        "AGENT_BRIDGE_WEB_SESSION_TTL_SECONDS",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_BRIDGE_PUBLIC_MODE", "1")
    with pytest.raises(
        ViewerSecurityConfigurationError,
        match="ALLOWED_HOSTS",
    ):
        ViewerSecurityPolicy.from_env()

    monkeypatch.setenv("AGENT_BRIDGE_ALLOWED_HOSTS", "bridge.example")
    monkeypatch.setenv(
        "AGENT_BRIDGE_ALLOWED_ORIGINS",
        "https://bridge.example",
    )
    monkeypatch.setenv("AGENT_BRIDGE_FORWARDED_ALLOW_IPS", "127.0.0.1/32")
    policy = ViewerSecurityPolicy.from_env()
    assert policy.public_mode is True
    assert policy.web_registration_mode == "closed"
    assert policy.secure_cookies is True
    assert policy.web_session_ttl_seconds == PUBLIC_WEB_SESSION_TTL_SECONDS

    monkeypatch.setenv("AGENT_BRIDGE_FORWARDED_ALLOW_IPS", "*")
    with pytest.raises(
        ViewerSecurityConfigurationError,
        match="cannot trust every source",
    ):
        ViewerSecurityPolicy.from_env()

    monkeypatch.setenv("AGENT_BRIDGE_FORWARDED_ALLOW_IPS", "127.0.0.1/32")
    monkeypatch.setenv("AGENT_BRIDGE_ALLOWED_HOSTS", "*.example")
    with pytest.raises(
        ViewerSecurityConfigurationError,
        match="invalid or unsafe allowed host",
    ):
        ViewerSecurityPolicy.from_env()


def test_public_access_code_registration_and_auth_rate_limits(
    tmp_path: Path,
) -> None:
    database = tmp_path / "bridge.db"
    bootstrap = TestClient(make_app(database), base_url="http://bridge.test")
    login_admin(bootstrap)
    database.chmod(0o600)
    access_code = "web-registration-code-" + "y" * 24
    app = create_app(
        database,
        registration_secret="agent-registration-secret-" + "z" * 32,
        captcha_generator=lambda: CAPTCHA_ANSWER,
        security_policy=public_security_policy(
            registration_mode="access_code",
            registration_secret=access_code,
        ),
    )
    client = TestClient(app, base_url="https://bridge.example")
    captcha_id = captcha(client)
    registration = {
        "username": "secure-member",
        "password": USER_PASSWORD,
        "captcha_id": captcha_id,
        "captcha_answer": CAPTCHA_ANSWER,
    }
    denied = client.post(
        "/api/auth/register",
        headers=intent_headers(client, "register"),
        json={**registration, "registration_code": "wrong-code"},
    )
    assert denied.status_code == 403
    created = client.post(
        "/api/auth/register",
        headers=intent_headers(client, "register"),
        json={**registration, "registration_code": access_code},
    )
    assert created.status_code == 201
    assert access_code not in created.text
    assert created.json()["user"]["username"] == "secure-member"

    limiter_database = tmp_path / "limiter.db"
    limiter_client = TestClient(make_app(limiter_database))
    for _index in range(20):
        assert limiter_client.get("/api/auth/captcha").status_code == 200
    limited = limiter_client.get("/api/auth/captcha")
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) >= 1
    assert limited.json()["retry_after_seconds"] > 0


def test_admin_managed_registration_codes_are_hashed_atomic_and_revocable(
    tmp_path: Path,
) -> None:
    database = tmp_path / "bridge.db"
    app = make_app(
        database,
        security_policy=ViewerSecurityPolicy(web_registration_mode="access_code"),
    )
    stale_browser = TestClient(app)
    stale_browser.cookies.set("agent_bridge_web_session", "stale-session-token")
    stale_health = stale_browser.get("/api/health")
    assert stale_health.status_code == 200
    assert stale_health.json()["web_registration_mode"] == "access_code"

    admin = TestClient(app)
    login_admin(admin)

    created = admin.post(
        "/api/admin/web-registration-codes",
        headers=intent_headers(admin, "create-registration-code"),
        json={"label": "测试邀请", "max_uses": 1, "expires_in_hours": 24},
    )
    assert created.status_code == 201
    registration_code = created.json()["registration_code"]
    plaintext = registration_code.pop("code")
    assert plaintext.startswith("ABR-")
    assert registration_code["status"] == "active"
    assert registration_code["remaining_uses"] == 1

    listed = admin.get("/api/admin/web-registration-codes")
    assert listed.status_code == 200
    assert listed.json()["codes"][0]["label"] == "测试邀请"
    assert plaintext not in listed.text
    with sqlite3.connect(database) as connection:
        stored = connection.execute(
            "SELECT code_hash, use_count FROM web_registration_codes"
        ).fetchone()
        assert stored is not None
        assert stored[0] != plaintext
        assert len(stored[0]) == 64
        assert stored[1] == 0

    def register(index: int) -> int:
        client = TestClient(app)
        return client.post(
            "/api/auth/register",
            headers=intent_headers(client, "register"),
            json={
                "username": f"atomic-member-{index}",
                "password": USER_PASSWORD,
                "captcha_id": captcha(client),
                "captcha_answer": CAPTCHA_ANSWER,
                "registration_code": plaintext,
            },
        ).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        statuses = sorted(executor.map(register, range(2)))
    assert statuses == [201, 403]
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT use_count FROM web_registration_codes"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM web_registration_code_uses"
        ).fetchone()[0] == 1

    reusable = admin.post(
        "/api/admin/web-registration-codes",
        headers=intent_headers(admin, "create-registration-code"),
        json={"label": "撤销测试", "max_uses": 3, "expires_in_hours": 48},
    ).json()["registration_code"]
    revoked = admin.post(
        f"/api/admin/web-registration-codes/{reusable['code_id']}/revoke",
        headers=intent_headers(admin, "revoke-registration-code"),
    )
    assert revoked.status_code == 200
    assert revoked.json()["registration_code"]["status"] == "revoked"

    denied_client = TestClient(app)
    denied = denied_client.post(
        "/api/auth/register",
        headers=intent_headers(denied_client, "register"),
        json={
            "username": "revoked-member",
            "password": USER_PASSWORD,
            "captcha_id": captcha(denied_client),
            "captcha_answer": CAPTCHA_ANSWER,
            "registration_code": reusable["code"],
        },
    )
    assert denied.status_code == 403
    assert "无效" in denied.json()["error"]

    expiring = admin.post(
        "/api/admin/web-registration-codes",
        headers=intent_headers(admin, "create-registration-code"),
        json={"label": "过期测试", "max_uses": 1, "expires_in_hours": 1},
    ).json()["registration_code"]
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE web_registration_codes SET expires_at = ? WHERE code_id = ?",
            (time.time() - 1, expiring["code_id"]),
        )
    expired_codes = admin.get("/api/admin/web-registration-codes").json()["codes"]
    assert next(
        item for item in expired_codes if item["code_id"] == expiring["code_id"]
    )["status"] == "expired"
    expired_client = TestClient(app)
    expired = expired_client.post(
        "/api/auth/register",
        headers=intent_headers(expired_client, "register"),
        json={
            "username": "expired-member",
            "password": USER_PASSWORD,
            "captcha_id": captcha(expired_client),
            "captcha_answer": CAPTCHA_ANSWER,
            "registration_code": expiring["code"],
        },
    )
    assert expired.status_code == 403


def test_registration_code_admin_endpoints_reject_ordinary_users(
    tmp_path: Path,
) -> None:
    database = tmp_path / "bridge.db"
    app = make_app(database)
    user = TestClient(app)
    register_web_user(user)
    assert user.get("/api/admin/web-registration-codes").status_code == 403
    denied = user.post(
        "/api/admin/web-registration-codes",
        headers=intent_headers(user, "create-registration-code"),
        json={},
    )
    assert denied.status_code == 403


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

    avatars = client.get("/api/avatars")
    assert avatars.status_code == 200
    avatar_catalog = avatars.json()
    assert {group["key"] for group in avatar_catalog["groups"]} == {
        "neutral",
        "deepseek",
        "gpt",
        "claude",
        "grok",
        "gemini",
        "kimi",
        "minimax",
        "glm",
        "qwen",
    }
    illustrated = [
        avatar
        for avatar in avatar_catalog["avatars"]
        if avatar.get("expression")
    ]
    assert len(illustrated) == 72
    avatar_asset = client.get(illustrated[0]["image_url"])
    assert avatar_asset.status_code == 200
    assert avatar_asset.headers["content-type"] == "image/webp"
    assert avatar_asset.headers["cache-control"] == (
        "public, max-age=31536000, immutable"
    )
    assert client.get("/assets/avatars/gpt/not-an-avatar.webp").status_code == 404

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
    assert all(person["inactivity_expires_at"] for person in participants)


def test_room_message_search_is_scoped_composable_paginated_and_jumpable(
    tmp_path: Path,
) -> None:
    database = tmp_path / "bridge.db"
    _store, _sender, _receiver = seed(database)
    client = TestClient(make_app(database))
    admin = login_admin(client)
    headers = intent_headers(client, "send-message")

    room_one_messages = []
    for index in range(6):
        response = client.post(
            "/api/rooms/room-one/messages",
            headers=headers,
            json={"body": f"alpha room-one result {index}"},
        )
        assert response.status_code == 201
        room_one_messages.append(response.json()["message"])
    room_two = client.post(
        "/api/rooms/room-two/messages",
        headers=headers,
        json={"body": "alpha room-two private result"},
    )
    assert room_two.status_code == 201

    assert client.get("/api/rooms/room-one/search").status_code == 400
    assert client.get(
        "/api/rooms/room-one/search",
        params={"sender_participant_id": "bad id"},
    ).status_code == 400

    first_page = client.get(
        "/api/rooms/room-one/search",
        params={
            "q": "ALPHA",
            "sender_participant_id": admin["participant_id"],
            "limit": 2,
        },
    )
    assert first_page.status_code == 200
    first_payload = first_page.json()
    assert first_payload["conversation_id"] == "room-one"
    assert first_payload["count"] == 2
    assert first_payload["has_more"] is True
    assert first_payload["next_before_sequence"] is not None
    assert all(
        item["sender_participant_id"] == admin["participant_id"]
        and "room-one" in item["body_preview"]
        and "room-two" not in item["body_preview"]
        for item in first_payload["results"]
    )

    second_page = client.get(
        "/api/rooms/room-one/search",
        params={
            "q": "alpha",
            "sender_participant_id": admin["participant_id"],
            "limit": 2,
            "before_sequence": first_payload["next_before_sequence"],
        },
    ).json()
    assert {
        item["message_id"] for item in first_payload["results"]
    }.isdisjoint({item["message_id"] for item in second_page["results"]})
    assert all("room-two" not in item["body_preview"] for item in second_page["results"])

    sender_only = client.get(
        "/api/rooms/room-one/search",
        params={"sender_participant_id": admin["participant_id"], "limit": 20},
    ).json()
    assert sender_only["count"] == 6

    target = room_one_messages[2]
    around = client.get(
        "/api/rooms/room-one/messages",
        params={"around_sequence": target["sequence"], "limit": 3},
    )
    assert around.status_code == 200
    around_payload = around.json()
    assert target["message_id"] in {
        item["message_id"] for item in around_payload["messages"]
    }
    assert around_payload["has_earlier"] is True
    assert around_payload["has_later"] is True
    assert all(
        item["conversation_id"] == "room-one"
        and "room-two private" not in item["body"]
        for item in around_payload["messages"]
    )
    assert client.get(
        "/api/rooms/room-one/messages",
        params={"around_sequence": 1, "before_sequence": 2},
    ).status_code == 400


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
    receipts = client.get(
        "/api/rooms/room-one/receipts?after_sequence=0&limit=20"
    ).json()["receipts"]
    assert [item["message_id"] for item in receipts] == [
        item["message_id"] for item in payload["messages"]
    ]
    event_snapshot = ViewerRepository(database).event_snapshot(
        after_sequence=payload["messages"][0]["sequence"]
    )
    assert set(event_snapshot["state_revisions"]) == {
        "messages",
        "nicknames",
        "participants",
        "memberships",
        "online",
        "sessions",
        "rooms",
        "connectors",
        "permissions",
        "tasks",
        "task_permissions",
        "receipts",
        "rates",
    }
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
    index_html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    assert "app.js?v=20260814-6" in index_html
    assert "app.css?v=20260814-6" in index_html
    assert 'id="open-registration-codes"' in index_html
    assert 'id="registration-code-dialog"' in index_html
    assert "requestAnimationFrame" in javascript
    assert "const INITIAL_ROOM_MESSAGE_LIMIT = 60" in javascript
    assert "const INCREMENTAL_ROOM_MESSAGE_LIMIT = 100" in javascript
    assert "new AbortController()" in javascript
    assert "/search?${parameters.toString()}" in javascript
    assert 'id="room-message-search-form"' in index_html
    assert 'id="register-access-code"' in index_html
    assert "webRegistrationMode" in javascript
    assert "function appendMessages" in javascript
    assert "function updateReceiptLabels" in javascript
    assert "/receipts?limit=" in javascript
    assert 'mode: taskMode ? "task" : "room"' in javascript
    assert "state_revisions" in javascript
    assert "? isNearTimelineBottom()" in javascript
    assert "dormant-member-group" in javascript
    assert "inactivity_expires_at" in javascript
    assert 'id="participant-search"' in index_html
    assert ".filter((person) => !isDormantParticipant(person))" in javascript
    assert "participantMatchesQuery" in javascript
    assert "function createAvatarElement" in javascript
    assert "profileAvatarVendor" in javascript
    mention_menu_start = javascript.index("function updateMentionMenu")
    mention_candidates = javascript.index(
        "const candidates = state.participants",
        mention_menu_start,
    )
    add_mention = javascript.index("function addComposerMention")
    dormant_filter = javascript.index(
        ".filter((person) => !isDormantParticipant(person))",
        mention_candidates,
        add_mention,
    )
    assert mention_candidates < dormant_filter < add_mention
    card_mention = javascript.index(
        'mention.addEventListener("click", () => addComposerMention(person))'
    )
    participant_search = javascript.index("function participantMatchesQuery")
    assert participant_search < card_mention < mention_menu_start

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
    assert response.json()["online_count"] == 0
    assert repaired == []
    assert len(response.json()["unavailable"]) == 2
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
    assert {person["participant_id"] for person in participants} == {
        sender["participant_id"],
        receiver["participant_id"],
    }
    assert next(
        person for person in participants
        if person["participant_id"] == sender["participant_id"]
    )["status"] == "offline"
    inactive_sender = next(
        person for person in participants
        if person["participant_id"] == sender["participant_id"]
    )
    assert inactive_sender["active_session_count"] == 0
    assert inactive_sender["connector_id"] is None
    assert inactive_sender["inactivity_expires_at"] > time.time()
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
            "notification_mode": "mention",
        },
    )
    assert sent.status_code == 200
    assert sent.json()["notification_mode"] == "mention"
    conflicting_mode = client.post(
        "/agent/send",
        headers=first_auth,
        json={
            "conversation_id": "old-room",
            "body": "普通模式不能携带艾特对象。",
            "mentions": [second["participant_id"]],
            "notification_mode": "ordinary",
        },
    )
    assert conflicting_mode.status_code == 400

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
        "avatar_key",
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
    assert "实际机器 username 由 Bridge 返回并固定到该 connector" in generated[
        "instructions"
    ]
    assert generated["avatar_selection"]["recommended_vendor"] == "gpt"
    assert len(generated["avatar_selection"]["choices"]) == 8
    assert "avatar_key" in generated["agent_supplied_fields"]
    assert "agent_list_avatars" in generated["instructions"]

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

    claude_product = client.post(
        "/api/agent-access",
        headers=intent_headers(client, "generate-agent-access"),
        json={"conversation_id": "old-room", "product": "claude-code"},
    )
    assert claude_product.status_code == 200
    claude_access = claude_product.json()["access"]
    assert claude_access["quick_start"]["kind"] == "claude-code-direct-accept"
    assert claude_access["quick_start"]["requires_mcp_restart"] is False
    assert "agent-bridge-accept" in claude_access["quick_start"]["command"]
    assert "--avatar-key" in claude_access["quick_start"]["command"]
    assert "printf %s" in claude_access["quick_start"]["command"]
    assert "无需重启现有 TUI/MCP" in claude_access["instructions"]

    deepseek_product = client.post(
        "/api/agent-access",
        headers=intent_headers(client, "generate-agent-access"),
        json={"conversation_id": "old-room", "product": "deepseek-harness"},
    )
    assert deepseek_product.status_code == 200
    deepseek_access = deepseek_product.json()["access"]
    assert deepseek_access["adapter_kind"] == "manual"
    assert deepseek_access["tui_adapter_kind"] == "deepseek-harness"
    assert deepseek_access["effective_adapter_kind"] == "deepseek-harness"
    assert deepseek_access["resident_capable"] is True
    assert deepseek_access["quick_start"]["kind"] == (
        "deepseek-harness-cordis-patch"
    )
    assert deepseek_access["quick_start"]["hot_reload"] is True
    assert deepseek_access["avatar_selection"]["recommended_vendor"] == "deepseek"
    deepseek_row = deepseek_access["quick_start"]["patch"][0]["insert"][0]
    assert deepseek_row["name"] == "@deepseek-ai/dsh-mcp-client"
    assert deepseek_row["config"]["serverName"].startswith("agent-bridge-")
    assert deepseek_row["config"]["transport"] == "stdio"
    assert "cwd" not in deepseek_row["config"]
    assert deepseek_access["quick_start"]["accept_tool"].endswith(
        "__agent_accept_invitation"
    )
    stable_deepseek_row = deepseek_access["quick_start"]["stable_patch_template"][
        0
    ]["insert"][0]
    stable_env = stable_deepseek_row["config"]["env"]
    assert "AGENT_BRIDGE_INVITATION_TOKEN" not in stable_env
    assert "AGENT_BRIDGE_ENROLLMENT_TOKEN_FILE" in stable_env
    assert deepseek_access["native_tui_binding_template"] == {
        "kind": "deepseek-http",
        "base_url": "http://127.0.0.1:<Harness Web Host 端口>",
    }
    assert "confirm_tui_binding=true" in deepseek_access["instructions"]

    for product, adapter in (
        ("opencode", "opencode"),
        ("hermes", "hermes"),
        ("pi", "pi"),
        ("qwen-code", "qwen-code"),
    ):
        native_product = client.post(
            "/api/agent-access",
            headers=intent_headers(client, "generate-agent-access"),
            json={"conversation_id": "old-room", "product": product},
        )
        assert native_product.status_code == 200
        native_access = native_product.json()["access"]
        assert native_access["tui_adapter_kind"] == adapter
        assert native_access["resident_capable"] is True
        assert native_access["quick_start"]["kind"] == "native-tui-direct-accept"
        assert native_access["quick_start"]["requires_mcp_restart"] is False
        assert (
            "--confirm-tui-binding" in native_access["quick_start"]["command_template"]
        )
    assert "AGENT_BRIDGE_CONNECTOR_ID" in stable_env
    assert stable_env["AGENT_BRIDGE_AUTO_REGISTER"] == "1"
    assert "HMR 热加载" in deepseek_access["instructions"]
    assert "随后自动启用真实 TUI 常驻唤醒" in deepseek_access["instructions"]

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
        ).fetchone()[0] == 8


def test_dashboard_keeps_admin_chat_ordinary_while_authorization_is_frozen(
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
    assert "authorization" not in message

    projected = client.get(
        "/api/rooms/%E6%8E%88%E6%9D%83%E8%81%8A%E5%A4%A9%E5%AE%A4/messages"
    )
    assert projected.status_code == 200
    dashboard_message = projected.json()["messages"][-1]
    assert "authorization" not in dashboard_message

    revoked = client.post(
        f"/api/messages/{message['message_id']}/authorization/revoke",
        headers=intent_headers(client, "revoke-chat-authorization"),
        json={"reason": "方案取消"},
    )
    assert revoked.status_code == 404

    agent_auth = {"Authorization": f"Bearer {agent['access_token']}"}
    history = client.post(
        "/agent/history",
        headers=agent_auth,
        json={"conversation_id": "授权聊天室"},
    )
    assert "authorization" not in history.json()["messages"][-1]


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
            "avatar_key": "gpt-05-determined-fist",
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
    assert registration["avatar_key"] == "gpt-05-determined-fist"
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
        headers={
            "X-Agent-Bridge-Enrollment": enrollment_token,
            "X-Agent-Bridge-Connector": connector_id,
        },
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
                "connector_binding_version": 2,
            },
        )
        assert response.status_code == 201
        registrations.append(response.json())
        enrollment_tokens.append(enrollment_token)

    assert len({item["connector_id"] for item in registrations}) == 2
    assert len({item["participant_id"] for item in registrations}) == 2
    assert all(item["invitation_reusable"] is True for item in registrations)

    same_requested_name = client.post(
        "/agent/invitations/accept",
        headers={"X-Agent-Bridge-Invitation": invitation_token},
        json={
            "product": "codex",
            "username": "multi-one",
            "signature": "同名接入也必须获得独立凭据。",
            "enrollment_token": "enroll_" + ("3" * 48),
            "connector_binding_version": 2,
        },
    )
    assert same_requested_name.status_code == 201
    assert same_requested_name.json()["participant_id"] not in {
        item["participant_id"] for item in registrations
    }
    assert same_requested_name.json()["connector_id"] not in {
        item["connector_id"] for item in registrations
    }
    assert same_requested_name.json()["username"].startswith("multi-one-")

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
    assert listed["use_count"] == 3
    assert listed["connector_count"] == 3
    assert listed["active_connector_count"] == 3
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
        headers={
            "X-Agent-Bridge-Enrollment": enrollment_tokens[1],
            "X-Agent-Bridge-Connector": registrations[1]["connector_id"],
        },
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
        ).fetchone()[0] == 3
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
    assert sent.json()["message_kind"] == "message"

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
    grant_web_room_access(database, user=web_user, room="room-one")
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
    grant_web_room_access(database, user=member, room="room-one")

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

    revision_before = ViewerRepository(database).event_snapshot()[
        "state_revisions"
    ]["rates"]
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
    assert ViewerRepository(database).event_snapshot()["state_revisions"][
        "rates"
    ] > revision_before

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
    assert 'id="manage-wake-policy"' in html
    assert 'id="wake-policy-dialog"' in html
    assert 'id="unactivated-agent-inactivity-days"' in html
    assert "本体优先，影子兜底" in html
    assert "复制加入目标群" in html
    assert "state.messages.length > 0 && !isNearTimelineBottom()" in javascript
    assert 'behavior: "smooth"' in javascript
    assert "/api/room-memberships/migrate" in javascript
    assert ".new-message-indicator svg" in stylesheet
    assert ".body-delivery-label" in stylesheet
    assert ':root[data-theme="ocean"]' in stylesheet
    assert "color-scheme: dark" in stylesheet
    assert "select option {" in stylesheet
    assert ".message {\n  position: relative;" in stylesheet
    assert "contain-intrinsic-size: 120px" not in stylesheet
    assert "roomSnapshots: new Map()" in javascript
    assert "const ROOM_SNAPSHOT_LIMIT = 4" in javascript
    assert "cacheActiveRoomSnapshot();\n  state.roomRequestController?.abort();" in javascript
    assert "const restored = restoreRoomSnapshot(roomId);" in javascript
    assert "refreshActiveRoom(!restored, !restored" in javascript
    assert "timelineNodes: [...elements.timeline.childNodes]" in javascript
    assert "elements.timeline.replaceChildren(...snapshot.timelineNodes)" in javascript
    assert "本体已接收并纳入当前任务" in javascript
    assert "convert-message-to-task" in javascript
    room_render_source = javascript[
        javascript.index("function renderRooms()"):
        javascript.index("function isNearTimelineBottom()")
    ]
    assert 'state.selectedRoom || ""' not in room_render_source


def test_room_scoped_a2a_gateway_creates_standard_structured_task(
    tmp_path: Path,
) -> None:
    database = tmp_path / "bridge.db"
    client = TestClient(make_app(database), base_url="http://bridge.test")
    login_admin(client)
    assert client.post(
        "/api/rooms",
        headers=intent_headers(client, "create-room"),
        json={"conversation_id": "A2A任务群"},
    ).status_code == 201
    target = client.post(
        "/agent/register",
        json={
            "product": "codex",
            "username": "a2a-target",
            "signature": "处理 A2A 任务。",
            "conversation_id": "A2A任务群",
        },
    ).json()
    created = client.post(
        "/api/a2a/grants",
        headers=intent_headers(client, "create-a2a-grant"),
        json={
            "conversation_id": "A2A任务群",
            "label": "外部审计系统",
            "ttl_seconds": 3600,
        },
    )
    assert created.status_code == 201
    grant = created.json()["grant"]
    access_token = grant["access_token"]

    card = client.get("/.well-known/agent-card.json")
    assert card.status_code == 200
    assert card.json()["protocolVersion"] == "1.0"
    assert card.json()["supportedInterfaces"][0]["url"] == (
        "http://bridge.test/a2a"
    )

    rpc_headers = {
        "Authorization": f"Bearer {access_token}",
        "A2A-Version": "1.0",
    }
    sent = client.post(
        "/a2a",
        headers=rpc_headers,
        json={
            "jsonrpc": "2.0",
            "id": "request-1",
            "method": "SendMessage",
            "params": {
                "message": {
                    "messageId": "external-message-1",
                    "contextId": "external-context-1",
                    "role": "ROLE_USER",
                    "parts": [{"text": "核对变更并提供测试证据。"}],
                    "metadata": {
                        "targetParticipantIds": [target["participant_id"]]
                    },
                }
            },
        },
    )
    assert sent.status_code == 200
    task = sent.json()["result"]
    assert task["status"]["state"] == "TASK_STATE_SUBMITTED"
    assert task["status"]["timestamp"].endswith("Z")
    assert task["contextId"] == "external-context-1"
    assert task["metadata"]["agentBridgeConversationId"] == "A2A任务群"
    assert task["metadata"]["targetParticipantIds"] == [
        target["participant_id"]
    ]

    fetched = client.post(
        "/a2a",
        headers=rpc_headers,
        json={
            "jsonrpc": "2.0",
            "id": "request-2",
            "method": "GetTask",
            "params": {"id": task["id"]},
        },
    )
    assert fetched.status_code == 200
    assert fetched.json()["result"]["id"] == task["id"]

    projected = client.get(
        "/api/rooms/A2A%E4%BB%BB%E5%8A%A1%E7%BE%A4/messages"
    ).json()["messages"][-1]
    assert projected["sender_seat"] == "a2a"
    assert projected["message_kind"] == "task"

    revoked = client.post(
        f"/api/a2a/grants/{grant['grant_id']}/revoke",
        headers=intent_headers(client, "revoke-a2a-grant"),
    )
    assert revoked.status_code == 200
    denied = client.post(
        "/a2a",
        headers=rpc_headers,
        json={
            "jsonrpc": "2.0",
            "id": "request-3",
            "method": "GetTask",
            "params": {"id": task["id"]},
        },
    )
    assert denied.status_code == 401
