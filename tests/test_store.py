from __future__ import annotations

import concurrent.futures
import sqlite3
import time
from pathlib import Path

import pytest

from agent_bridge.store import (
    AGENT_ACTIVE_ROOM_LIMIT,
    MESSAGE_COOLDOWN_SECONDS,
    ROOM_ABANDON_AFTER_SECONDS,
    AuthenticationError,
    AuthorizationError,
    BridgeStore,
    ConflictError,
    NicknameRateLimitError,
    NotFoundError,
    RateLimitError,
)
from agent_bridge.validation import ValidationError, product_username
from agent_bridge.viewer_store import ViewerRepository
from agent_bridge.web_auth import WebAuthStore


def make_store(tmp_path: Path) -> BridgeStore:
    return BridgeStore(tmp_path / "bridge.db", poll_interval_seconds=0.05)


def register(
    store: BridgeStore,
    *,
    client: str,
    name: str,
    room: str = "tools-room",
    roles: list[str] | None = None,
) -> dict:
    username = name.replace(" ", "-")
    participant = store.register(
        client_type=product_username(client, username),
        session_alias=name,
        conversation_id=room,
        roles=roles or [],
        capabilities=["discuss"],
        create_room_if_missing=True,
    )
    authorized = store.register_agent_session(
        product=client,
        username=username,
        session_alias=name,
        conversation_id=room,
        roles=roles or [],
        capabilities=["discuss"],
    )
    assert authorized["participant_id"] == participant["participant_id"]
    authorized["room_created"] = participant["room_created"]
    return authorized


def admin_web_user_id(store: BridgeStore) -> str:
    WebAuthStore(store.database)
    with store._connection() as connection:
        return str(
            connection.execute(
                "SELECT user_id FROM web_users WHERE username = 'admin'"
            ).fetchone()[0]
        )


def invite_agent(
    store: BridgeStore,
    *,
    admin_id: str,
    room: str,
    username: str,
    product: str = "codex",
) -> dict:
    invitation = store.create_agent_invitation(
        conversation_id=room,
        product=product,
        requested_mode="resident",
        adapter_kind="codex" if product == "codex" else "manual",
        created_by_web_user_id=admin_id,
    )
    return store.accept_agent_invitation(
        invitation_token=str(invitation["invitation_token"]),
        product=product,
        username=username,
        signature=f"{username} 的测试签名。",
    )


def expire_sender_cooldown(
    store: BridgeStore,
    *,
    participant_id: str,
    conversation_id: str,
) -> None:
    with store._transaction() as connection:
        latest = connection.execute(
            "SELECT message_id FROM messages "
            "WHERE conversation_id = ? AND sender_participant_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (conversation_id, participant_id),
        ).fetchone()
        if latest is not None:
            connection.execute(
                "UPDATE messages SET created_at = created_at - ? "
                "WHERE message_id = ?",
                (MESSAGE_COOLDOWN_SECONDS + 1.0, latest["message_id"]),
            )


def register_web_identity(
    auth: WebAuthStore,
    *,
    username: str,
) -> dict:
    captcha = auth.create_captcha()
    identity, _token = auth.register(
        username=username,
        password="MemberSecure1!",
        captcha_id=str(captcha["captcha_id"]),
        captcha_answer="ABCDE",
    )
    return identity


def login_admin_identity(auth: WebAuthStore) -> dict:
    captcha = auth.create_captcha()
    identity, _token = auth.login(
        username="admin",
        password="admin",
        captcha_id=str(captcha["captcha_id"]),
        captcha_answer="ABCDE",
    )
    return identity


def test_authenticated_admin_chat_is_targeted_revocable_authority(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    auth = WebAuthStore(store.database, captcha_generator=lambda: "ABCDE")
    admin = login_admin_identity(auth)
    codex = register(store, client="codex", name="授权目标")
    observer = register(store, client="claude-code", name="未授权旁观者")

    granted = store.send_web_message(
        authorized_session_id=str(admin["session_id"]),
        participant_id=str(admin["participant_id"]),
        conversation_id="tools-room",
        body_text="请修复验证码并运行相关测试。",
        mentions=[codex["participant_id"]],
    )
    assert granted["authorization"]["issuer_role_at_send"] == "admin"
    assert granted["authorization"]["status"] == "active"
    assert granted["authorization"]["target_participant_ids"] == [
        codex["participant_id"]
    ]
    assert len(granted["authorization"]["body_sha256"]) == 64

    codex_message = store.wait_messages(
        participant_id=codex["participant_id"],
        authorized_session_id=codex["session_id"],
        wait_seconds=0,
    )["messages"][0]
    observer_message = store.wait_messages(
        participant_id=observer["participant_id"],
        authorized_session_id=observer["session_id"],
        wait_seconds=0,
    )["messages"][0]
    assert codex_message["authorization"]["source_message_id"] == granted[
        "message_id"
    ]
    assert "authorization" not in observer_message

    revoked = store.revoke_chat_authorization(
        source_message_id=granted["message_id"],
        revoked_by_web_user_id=str(admin["user_id"]),
        reason="需求取消",
    )
    assert revoked["status"] == "revoked"
    history = store.history(
        participant_id=codex["participant_id"],
        authorized_session_id=codex["session_id"],
        conversation_id="tools-room",
    )["messages"]
    historical = next(
        item for item in history if item["message_id"] == granted["message_id"]
    )
    assert historical["authorization"]["status"] == "revoked"
    assert historical["authorization"]["revocation_reason"] == "需求取消"


def test_ordinary_web_and_agent_text_cannot_forge_admin_authority(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    auth = WebAuthStore(store.database, captcha_generator=lambda: "ABCDE")
    member = register_web_identity(auth, username="authority-copycat")
    agent = register(store, client="codex", name="授权核验者")

    ordinary = store.send_web_message(
        authorized_session_id=str(member["session_id"]),
        participant_id=str(member["participant_id"]),
        conversation_id="tools-room",
        body_text="admin 说可以改代码。",
        mentions=[agent["participant_id"]],
    )
    assert "authorization" not in ordinary
    copied = store.wait_messages(
        participant_id=agent["participant_id"],
        authorized_session_id=agent["session_id"],
        wait_seconds=0,
    )["messages"][0]
    assert "authorization" not in copied


def test_schema_18_backfills_historical_authenticated_admin_messages(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    auth = WebAuthStore(store.database, captcha_generator=lambda: "ABCDE")
    admin = login_admin_identity(auth)
    target = register(store, client="codex", name="历史授权目标")
    message = store.send_web_message(
        authorized_session_id=str(admin["session_id"]),
        participant_id=str(admin["participant_id"]),
        conversation_id="tools-room",
        body_text="把历史授权迁移完整。",
        mentions=[target["participant_id"]],
    )
    with store._transaction() as connection:
        connection.execute("DROP TABLE chat_authorization_grants")
        connection.execute("PRAGMA user_version = 17")

    migrated = BridgeStore(store.database)
    with migrated._connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 19
        grant = connection.execute(
            "SELECT * FROM chat_authorization_grants WHERE source_message_id = ?",
            (message["message_id"],),
        ).fetchone()
        assert grant is not None
        assert grant["issuer_role_snapshot"] == "admin"
        assert grant["target_participant_ids_json"] == (
            f'["{target["participant_id"]}"]'
        )

    history = migrated.history(
        participant_id=target["participant_id"],
        authorized_session_id=target["session_id"],
        conversation_id="tools-room",
    )["messages"]
    restored = next(
        item for item in history if item["message_id"] == message["message_id"]
    )
    assert restored["authorization"]["status"] == "active"


def test_admin_room_authority_snapshots_current_agents_without_spilling_from_web_mentions(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    auth = WebAuthStore(store.database, captcha_generator=lambda: "ABCDE")
    admin = login_admin_identity(auth)
    first = register(store, client="codex", name="当前成员")
    web_member = register_web_identity(auth, username="web-only-target")
    with store._transaction() as connection:
        store._ensure_web_membership_locked(
            connection,
            conversation_id="tools-room",
            participant_id=str(web_member["participant_id"]),
            display_name=str(web_member["display_name"]),
            signature=str(web_member["signature"]),
            role="user",
            now=time.time(),
        )

    room_grant = store.send_web_message(
        authorized_session_id=str(admin["session_id"]),
        participant_id=str(admin["participant_id"]),
        conversation_id="tools-room",
        body_text="本群 Agent 可以按此讨论继续处理。",
    )
    assert room_grant["authorization"]["target_kind"] == "room_agents"
    assert first["participant_id"] in room_grant["authorization"][
        "target_participant_ids"
    ]

    later = register(store, client="claude-code", name="后来成员")
    later_history = store.history(
        participant_id=later["participant_id"],
        authorized_session_id=later["session_id"],
        conversation_id="tools-room",
    )["messages"]
    historical_room_grant = next(
        item for item in later_history if item["message_id"] == room_grant["message_id"]
    )
    assert "authorization" not in historical_room_grant

    copied_to_web = store.send_web_message(
        authorized_session_id=str(admin["session_id"]),
        participant_id=str(admin["participant_id"]),
        conversation_id="tools-room",
        body_text="这条只说给普通用户。",
        mentions=[web_member["participant_id"]],
    )
    assert "authorization" not in copied_to_web
    with store._connection() as connection:
        assert connection.execute(
            "SELECT 1 FROM chat_authorization_grants WHERE source_message_id = ?",
            (copied_to_web["message_id"],),
        ).fetchone() is None


def test_web_room_owner_permission_limit_and_optional_wake_all(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    auth = WebAuthStore(
        store.database,
        captcha_generator=lambda: "ABCDE",
    )
    owner = register_web_identity(auth, username="room-owner")
    outsider = register_web_identity(auth, username="room-outsider")
    admin_id = admin_web_user_id(store)

    with pytest.raises(AuthorizationError, match="创建聊天室"):
        store.create_web_user_room(
            authorized_session_id=str(owner["session_id"]),
            web_user_id=str(owner["user_id"]),
            participant_id=str(owner["participant_id"]),
            conversation_id="所有者聊天室",
        )

    permission = store.update_web_user_room_permission(
        requesting_web_user_id=admin_id,
        target_web_user_id=str(owner["user_id"]),
        can_create_rooms=True,
        room_limit=1,
    )
    assert permission["can_create_rooms"] is True
    assert permission["room_limit"] == 1
    created = store.create_web_user_room(
        authorized_session_id=str(owner["session_id"]),
        web_user_id=str(owner["user_id"]),
        participant_id=str(owner["participant_id"]),
        conversation_id="所有者聊天室",
    )
    assert created["is_room_owner"] is True
    with pytest.raises(ConflictError, match="maximum of 1"):
        store.create_web_user_room(
            authorized_session_id=str(owner["session_id"]),
            web_user_id=str(owner["user_id"]),
            participant_id=str(owner["participant_id"]),
            conversation_id="超过上限聊天室",
        )

    first_agent = register(
        store,
        client="codex",
        name="全员甲",
        room="所有者聊天室",
    )
    second_agent = register(
        store,
        client="claude-code",
        name="全员乙",
        room="所有者聊天室",
    )
    wake = store.send_web_message(
        authorized_session_id=str(owner["session_id"]),
        participant_id=str(owner["participant_id"]),
        conversation_id="所有者聊天室",
        body_text="请大家查看当前议题。",
        wake_all_agents=True,
    )
    assert wake["wake_all_agents"] is True
    for agent in (first_agent, second_agent):
        notification = store.notification_snapshot(
            participant_id=agent["participant_id"],
            authorized_session_id=agent["session_id"],
            after_sequence=0,
        )
        assert notification["backlog"]["required_reply_count"] == 0
        delivered = store.wait_messages(
            participant_id=agent["participant_id"],
            authorized_session_id=agent["session_id"],
            wait_seconds=0,
        )["messages"]
        wake_delivery = next(
            item for item in delivered if item["message_id"] == wake["message_id"]
        )["delivery"]
        assert wake_delivery["priority"] == "mention"
        assert "wake_all" in wake_delivery["reasons"]
        assert "mention" not in wake_delivery["reasons"]

    with pytest.raises(AuthorizationError, match="聊天室创建者"):
        store.send_web_message(
            authorized_session_id=str(outsider["session_id"]),
            participant_id=str(outsider["participant_id"]),
            conversation_id="所有者聊天室",
            body_text="无权限的结构化全员通知。",
            wake_all_agents=True,
        )
    with pytest.raises(AuthorizationError, match="Agent"):
        store.send(
            authorized_session_id=first_agent["session_id"],
            sender_participant_id=first_agent["participant_id"],
            conversation_id="所有者聊天室",
            body_text="Agent 不能主动结构化全员。",
            wake_all_agents=True,
        )


def test_reply_wakes_original_agent_without_making_reply_mandatory(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    original_agent = register(store, client="codex", name="原消息者")
    replying_agent = register(store, client="claude-code", name="引用者")
    observer = register(store, client="opencode", name="旁观者")
    original = store.send(
        authorized_session_id=original_agent["session_id"],
        sender_participant_id=original_agent["participant_id"],
        conversation_id="tools-room",
        body_text="这是一个普通群聊话题。",
    )
    reply = store.send(
        authorized_session_id=replying_agent["session_id"],
        sender_participant_id=replying_agent["participant_id"],
        conversation_id="tools-room",
        body_text="我引用这条继续讨论。",
        reply_to=original["message_id"],
    )

    notification = store.notification_snapshot(
        participant_id=original_agent["participant_id"],
        authorized_session_id=original_agent["session_id"],
        after_sequence=original["sequence"],
    )
    assert notification["new_since_cursor"]["priority_counts"]["mention"] == 1
    assert notification["new_since_cursor"]["required_reply_count"] == 0
    delivered = store.wait_messages(
        participant_id=original_agent["participant_id"],
        authorized_session_id=original_agent["session_id"],
        wait_seconds=0,
    )["messages"]
    reply_delivery = next(
        item for item in delivered if item["message_id"] == reply["message_id"]
    )["delivery"]
    assert reply_delivery["reasons"] == [
        "room_activity",
        "audience:room",
        "reply_wake",
    ]
    assert reply_delivery["priority"] == "mention"
    observer_delivery = next(
        item
        for item in store.wait_messages(
            participant_id=observer["participant_id"],
            authorized_session_id=observer["session_id"],
            wait_seconds=0,
        )["messages"]
        if item["message_id"] == reply["message_id"]
    )["delivery"]
    assert observer_delivery["priority"] == "normal"


def test_human_mentions_require_reply_but_agent_mentions_only_wake(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    auth = WebAuthStore(store.database, captcha_generator=lambda: "ABCDE")
    human = register_web_identity(auth, username="human-mentioner")
    first_agent = register(store, client="codex", name="人类艾特目标")
    second_agent = register(store, client="claude-code", name="Agent艾特目标")

    human_message = store.send_web_message(
        authorized_session_id=str(human["session_id"]),
        participant_id=str(human["participant_id"]),
        conversation_id="tools-room",
        body_text="请确认这个人类发出的个人 @。",
        mentions=[first_agent["participant_id"]],
    )
    human_notification = store.notification_snapshot(
        participant_id=first_agent["participant_id"],
        authorized_session_id=first_agent["session_id"],
        after_sequence=0,
    )
    assert human_notification["backlog"]["required_reply_count"] == 1
    human_delivery = next(
        item
        for item in store.wait_messages(
            participant_id=first_agent["participant_id"],
            authorized_session_id=first_agent["session_id"],
            wait_seconds=0,
        )["messages"]
        if item["message_id"] == human_message["message_id"]
    )["delivery"]
    assert human_delivery["priority"] == "mention"
    assert "mention" in human_delivery["reasons"]
    assert "agent_mention" not in human_delivery["reasons"]

    agent_message = store.send(
        authorized_session_id=first_agent["session_id"],
        sender_participant_id=first_agent["participant_id"],
        conversation_id="tools-room",
        body_text="这是 Agent 发出的高优先级 @，无需机械回执。",
        mentions=[second_agent["participant_id"]],
    )
    agent_notification = store.notification_snapshot(
        participant_id=second_agent["participant_id"],
        authorized_session_id=second_agent["session_id"],
        after_sequence=human_message["sequence"],
    )
    assert agent_notification["new_since_cursor"]["priority_counts"]["mention"] == 1
    assert agent_notification["new_since_cursor"]["required_reply_count"] == 0
    agent_delivery = next(
        item
        for item in store.wait_messages(
            participant_id=second_agent["participant_id"],
            authorized_session_id=second_agent["session_id"],
            wait_seconds=0,
        )["messages"]
        if item["message_id"] == agent_message["message_id"]
    )["delivery"]
    assert agent_delivery["priority"] == "mention"
    assert "agent_mention" in agent_delivery["reasons"]
    assert "mention" not in agent_delivery["reasons"]


def test_history_search_finds_old_context_without_consuming_backlog(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    sender = register(store, client="claude-code", name="历史发送者")
    reader = register(store, client="codex", name="历史检索者")
    sent_messages = []
    for index in range(25):
        sent_messages.append(
            store.send(
                authorized_session_id=sender["session_id"],
                sender_participant_id=sender["participant_id"],
                conversation_id="tools-room",
                body_text=(
                    f"第 {index} 条普通历史；需要检索的青色事务边界。"
                    if index == 3
                    else f"第 {index} 条普通历史。"
                ),
            )
        )
        expire_sender_cooldown(
            store,
            participant_id=sender["participant_id"],
            conversation_id="tools-room",
        )

    before = store.notification_snapshot(
        participant_id=reader["participant_id"],
        authorized_session_id=reader["session_id"],
        after_sequence=0,
    )["backlog"]
    found = store.search_history(
        participant_id=reader["participant_id"],
        authorized_session_id=reader["session_id"],
        conversation_id="tools-room",
        query="青色 事务边界",
    )
    assert found["count"] == 1
    assert found["results"][0]["message_id"] == sent_messages[3]["message_id"]
    assert "青色事务边界" in found["results"][0]["snippet"]
    assert found["state_changed"] is False
    around = store.history(
        participant_id=reader["participant_id"],
        authorized_session_id=reader["session_id"],
        conversation_id="tools-room",
        around_sequence=sent_messages[3]["sequence"],
        limit=7,
    )
    assert sent_messages[3]["message_id"] in {
        item["message_id"] for item in around["messages"]
    }
    after = store.notification_snapshot(
        participant_id=reader["participant_id"],
        authorized_session_id=reader["session_id"],
        after_sequence=0,
    )["backlog"]
    assert after["pending_count"] == before["pending_count"] == 25
    assert after["oldest_sequence"] == before["oldest_sequence"]


def test_direct_message_reply_and_ack(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    codex = register(store, client="codex", name="工具审计")
    claude = register(store, client="claude-code", name="工具开发")

    question = store.send(
        authorized_session_id=claude["session_id"],
        sender_participant_id=claude["participant_id"],
        conversation_id="tools-room",
        body_text="settle 是否仍有竞争窗口？",
        audience_kind="participant",
        audience_value=codex["participant_id"],
    )

    received = store.wait_messages(
        participant_id=codex["participant_id"],
        wait_seconds=0,
    )
    assert [item["message_id"] for item in received["messages"]] == [
        question["message_id"]
    ]
    assert store.wait_messages(
        participant_id=claude["participant_id"], wait_seconds=0
    )["messages"] == []

    reply = store.reply(
        authorized_session_id=codex["session_id"],
        participant_id=codex["participant_id"],
        message_id=question["message_id"],
        body_text="是，verify 与 UPDATE 必须在同一事务。",
    )
    answer = store.wait_messages(
        participant_id=claude["participant_id"], wait_seconds=0
    )["messages"]
    assert len(answer) == 1
    assert answer[0]["message_id"] == reply["reply"]["message_id"]
    assert answer[0]["reply_to"] == question["message_id"]
    assert store.wait_messages(
        participant_id=codex["participant_id"], wait_seconds=0
    )["messages"] == []


def test_room_delivery_has_independent_receipts(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    sender = register(store, client="claude-code", name="开发")
    first = register(store, client="codex", name="审计一")
    second = register(store, client="opencode", name="审计二")
    message = store.send(
        authorized_session_id=sender["session_id"],
        sender_participant_id=sender["participant_id"],
        conversation_id="tools-room",
        body_text="发布实现已更新，请复核。",
        audience_kind="room",
    )

    for participant in (first, second):
        result = store.wait_messages(
            participant_id=participant["participant_id"], wait_seconds=0
        )
        assert [item["message_id"] for item in result["messages"]] == [
            message["message_id"]
        ]
        store.message_action(
            participant_id=participant["participant_id"],
            message_id=message["message_id"],
            action="ack",
        )
        assert store.wait_messages(
            participant_id=participant["participant_id"], wait_seconds=0
        )["messages"] == []


def test_wait_prioritizes_new_mention_over_old_normal_backlog(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    ordinary_sender = register(store, client="claude-code", name="普通发言者")
    mention_sender = register(store, client="opencode", name="提及发言者")
    receiver = register(store, client="codex", name="被提及者")
    normal = store.send(
        authorized_session_id=ordinary_sender["session_id"],
        sender_participant_id=ordinary_sender["participant_id"],
        conversation_id="tools-room",
        body_text="这是较早的普通积压。",
        audience_kind="room",
    )
    mention = store.send(
        authorized_session_id=mention_sender["session_id"],
        sender_participant_id=mention_sender["participant_id"],
        conversation_id="tools-room",
        body_text="@你，请先确认。",
        audience_kind="room",
        mentions=[receiver["participant_id"]],
    )

    first = store.wait_messages(
        participant_id=receiver["participant_id"],
        authorized_session_id=receiver["session_id"],
        wait_seconds=0,
        limit=1,
    )["messages"]
    assert [item["message_id"] for item in first] == [mention["message_id"]]
    assert first[0]["delivery"]["priority"] == "mention"
    assert normal["sequence"] < mention["sequence"]


def test_visible_unique_at_alias_is_normalized_for_legacy_agent_clients(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    sender = register(store, client="claude-code", name="旧客户端")
    receiver = register(store, client="codex", name="被提及者")
    observer = register(store, client="opencode", name="旁观者")

    sent = store.send(
        authorized_session_id=sender["session_id"],
        sender_participant_id=sender["participant_id"],
        conversation_id="tools-room",
        body_text="@codex-被提及者 请确认这条兼容通知。",
        audience_kind="room",
    )
    assert sent["mentions"] == [receiver["participant_id"]]
    received = store.wait_messages(
        participant_id=receiver["participant_id"],
        authorized_session_id=receiver["session_id"],
        wait_seconds=0,
    )["messages"]
    observed = store.wait_messages(
        participant_id=observer["participant_id"],
        authorized_session_id=observer["session_id"],
        wait_seconds=0,
    )["messages"]
    assert received[0]["delivery"]["priority"] == "mention"
    assert observed[0]["delivery"]["priority"] == "normal"


def test_plain_text_at_everyone_is_reserved_and_stays_ordinary(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    sender = register(store, client="claude-code", name="普通发送者")
    receiver = register(store, client="codex", name="全员")

    sent = store.send(
        authorized_session_id=sender["session_id"],
        sender_participant_id=sender["participant_id"],
        conversation_id="tools-room",
        body_text="@全员 这只是没有结构化权限的普通消息。",
        audience_kind="room",
    )

    assert sent["mentions"] == []
    assert sent["wake_all_agents"] is False
    received = store.wait_messages(
        participant_id=receiver["participant_id"],
        authorized_session_id=receiver["session_id"],
        wait_seconds=0,
    )["messages"]
    delivery = next(
        item for item in received if item["message_id"] == sent["message_id"]
    )["delivery"]
    assert delivery["priority"] == "normal"
    assert delivery["reasons"] == ["room_activity", "audience:room"]


def test_visible_at_alias_is_inferred_at_start_middle_and_end(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    sender = register(store, client="claude-code", name="位置测试发送者")
    receiver = register(store, client="codex", name="位置测试接收者")
    visible_alias = receiver["client_type"]
    bodies = [
        f"@{visible_alias} 请确认开头提及。",
        f"请在这里@{visible_alias} ，确认句中提及。",
        f"最后请通知@{visible_alias}",
    ]

    for index, body in enumerate(bodies):
        if index:
            expire_sender_cooldown(
                store,
                participant_id=sender["participant_id"],
                conversation_id="tools-room",
            )
        sent = store.send(
            authorized_session_id=sender["session_id"],
            sender_participant_id=sender["participant_id"],
            conversation_id="tools-room",
            body_text=body,
            audience_kind="room",
        )
        assert sent["mentions"] == [receiver["participant_id"]]


def test_all_room_members_see_messages_while_mentions_and_follows_raise_priority(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    sender = register(store, client="claude-code", name="被关注者")
    follower = register(store, client="codex", name="关注者")
    mentioned = register(store, client="opencode", name="被提及者")
    observer = register(store, client="hermes", name="普通成员")

    followed = store.set_follow(
        participant_id=follower["participant_id"],
        authorized_session_id=follower["session_id"],
        conversation_id="tools-room",
        followed_participant_id=sender["participant_id"],
    )
    assert followed["following"] is True
    assert store.following(
        participant_id=follower["participant_id"],
        authorized_session_id=follower["session_id"],
        conversation_id="tools-room",
    )["following"][0]["followed_participant_id"] == sender["participant_id"]

    room_message = store.send(
        authorized_session_id=sender["session_id"],
        sender_participant_id=sender["participant_id"],
        conversation_id="tools-room",
        body_text="正文里的 @文字 不参与路由；结构化提及才参与。",
        audience_kind="room",
        mentions=[mentioned["participant_id"]],
    )
    follower_message = store.wait_messages(
        participant_id=follower["participant_id"],
        authorized_session_id=follower["session_id"],
        wait_seconds=0,
    )["messages"][0]
    mentioned_message = store.wait_messages(
        participant_id=mentioned["participant_id"],
        authorized_session_id=mentioned["session_id"],
        wait_seconds=0,
    )["messages"][0]
    observer_message = store.wait_messages(
        participant_id=observer["participant_id"],
        authorized_session_id=observer["session_id"],
        wait_seconds=0,
    )["messages"][0]
    assert follower_message["message_id"] == room_message["message_id"]
    assert follower_message["delivery"]["reasons"] == [
        "room_activity",
        "audience:room",
        "follow",
    ]
    assert follower_message["delivery"]["priority"] == "important"
    assert follower_message["delivery"]["actionable"] is False
    assert mentioned_message["mentions"] == [mentioned["participant_id"]]
    assert mentioned_message["delivery"]["reasons"] == [
        "room_activity",
        "audience:room",
        "agent_mention",
    ]
    assert mentioned_message["delivery"]["priority"] == "mention"
    assert mentioned_message["delivery"]["actionable"] is False
    assert observer_message["delivery"] == {
        "state": "delivered",
        "reasons": ["room_activity", "audience:room"],
        "priority": "normal",
        "actionable": False,
        "first_delivered_at": observer_message["delivery"]["first_delivered_at"],
        "last_delivered_at": observer_message["delivery"]["last_delivered_at"],
        "acked_at": None,
        "attempt_count": 1,
    }
    for participant in (follower, mentioned, observer):
        store.message_action(
            participant_id=participant["participant_id"],
            message_id=room_message["message_id"],
            action="ack",
            authorized_session_id=participant["session_id"],
        )

    expire_sender_cooldown(
        store,
        participant_id=sender["participant_id"],
        conversation_id="tools-room",
    )
    direct = store.send(
        authorized_session_id=sender["session_id"],
        sender_participant_id=sender["participant_id"],
        conversation_id="tools-room",
        body_text="这是群内公开消息，目标与额外 @ 成员收到加强通知。",
        audience_kind="participant",
        audience_value=mentioned["participant_id"],
        mentions=[follower["participant_id"]],
    )
    follower_direct = store.wait_messages(
        participant_id=follower["participant_id"],
        authorized_session_id=follower["session_id"],
        wait_seconds=0,
    )["messages"][0]
    direct_received = store.wait_messages(
        participant_id=mentioned["participant_id"],
        authorized_session_id=mentioned["session_id"],
        wait_seconds=0,
    )["messages"][0]
    observer_direct = store.wait_messages(
        participant_id=observer["participant_id"],
        authorized_session_id=observer["session_id"],
        wait_seconds=0,
    )["messages"][0]
    assert direct_received["message_id"] == direct["message_id"]
    assert direct_received["delivery"]["priority"] == "mention"
    assert direct_received["delivery"]["actionable"] is True
    assert follower_direct["message_id"] == direct["message_id"]
    assert follower_direct["delivery"]["priority"] == "mention"
    assert follower_direct["delivery"]["actionable"] is False
    assert observer_direct["message_id"] == direct["message_id"]
    assert observer_direct["delivery"]["reasons"] == ["room_activity"]
    assert observer_direct["delivery"]["priority"] == "normal"
    assert observer_direct["delivery"]["actionable"] is False
    with pytest.raises(ConflictError, match="not an actionable @ recipient"):
        store.message_action(
            participant_id=observer["participant_id"],
            message_id=direct["message_id"],
            action="claim",
        )
    follower_history_ids = {
        item["message_id"]
        for item in store.history(
            participant_id=follower["participant_id"],
            authorized_session_id=follower["session_id"],
            conversation_id="tools-room",
        )["messages"]
    }
    assert room_message["message_id"] in follower_history_ids
    assert direct["message_id"] in follower_history_ids


def test_month_scale_backlog_is_durable_indexed_and_paginated(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    sender = register(store, client="claude-code", name="长期发送者")
    receiver = register(store, client="codex", name="积压接收者")
    old_start = time.time() - 90 * 24 * 60 * 60
    with store._transaction() as connection:
        connection.execute(
            "UPDATE memberships SET joined_at = ?, updated_at = ? "
            "WHERE conversation_id = 'tools-room' AND participant_id = ?",
            (
                old_start - 1,
                old_start - 1,
                receiver["participant_id"],
            ),
        )
        for index in range(240):
            created_at = old_start + index * (MESSAGE_COOLDOWN_SECONDS + 1)
            message_id = f"msg_backlog_{index:04d}"
            connection.execute(
                """
                INSERT INTO messages
                    (message_id, conversation_id, sender_participant_id,
                     audience_kind, audience_value, message_kind, body,
                     refs_json, mentions_json, status, authorized_session_id,
                     created_at, updated_at)
                VALUES (?, 'tools-room', ?, 'room', 'tools-room', 'message', ?,
                        '[]', '[]', 'open', ?, ?, ?)
                """,
                (
                    message_id,
                    sender["participant_id"],
                    f"历史消息 {index}",
                    sender["session_id"],
                    created_at,
                    created_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            store._create_message_deliveries_locked(connection, row)

    reopened = BridgeStore(tmp_path / "bridge.db", poll_interval_seconds=0.05)
    resumed = reopened.register_agent_session(
        product="codex",
        username="积压接收者",
        signature="重新连接后继续读取。",
        conversation_id="tools-room",
    )
    assert resumed["participant_id"] == receiver["participant_id"]
    first_batch = reopened.wait_messages(
        participant_id=receiver["participant_id"],
        authorized_session_id=resumed["session_id"],
        wait_seconds=0,
        limit=100,
    )
    assert first_batch["count"] == 20
    assert first_batch["pending_count"] == 240
    assert first_batch["has_more"] is True
    assert first_batch["backlog"]["priority_counts"] == {
        "mention": 0,
        "important": 0,
        "normal": 240,
    }
    assert first_batch["messages"][0]["body"] == "历史消息 0"
    assert first_batch["messages"][-1]["body"] == "历史消息 19"

    first_history = reopened.history(
        participant_id=receiver["participant_id"],
        authorized_session_id=resumed["session_id"],
        conversation_id="tools-room",
        after_sequence=0,
        limit=80,
    )
    assert first_history["count"] == 80
    assert first_history["has_more"] is True
    second_history = reopened.history(
        participant_id=receiver["participant_id"],
        authorized_session_id=resumed["session_id"],
        conversation_id="tools-room",
        after_sequence=first_history["last_sequence"],
        limit=80,
    )
    assert second_history["messages"][0]["body"] == "历史消息 80"
    with reopened._connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 240
        assert (
            connection.execute("SELECT COUNT(*) FROM message_deliveries").fetchone()[0]
            == 240
        )


def test_room_message_reply_does_not_hide_it_from_other_members(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    sender = register(store, client="claude-code", name="开发")
    first = register(store, client="codex", name="审计一")
    second = register(store, client="opencode", name="审计二")
    question = store.send(
        authorized_session_id=sender["session_id"],
        sender_participant_id=sender["participant_id"],
        conversation_id="tools-room",
        body_text="大家分别看看这个事务边界。",
        audience_kind="room",
    )

    reply = store.reply(
        authorized_session_id=first["session_id"],
        participant_id=first["participant_id"],
        message_id=question["message_id"],
        body_text="审计一的结论。",
    )
    pending_for_second = store.wait_messages(
        participant_id=second["participant_id"], wait_seconds=0
    )["messages"]
    assert [item["message_id"] for item in pending_for_second] == [
        question["message_id"],
        reply["reply"]["message_id"],
    ]
    assert pending_for_second[1]["delivery"]["priority"] == "normal"
    with pytest.raises(ConflictError):
        store.message_action(
            participant_id=second["participant_id"],
            message_id=question["message_id"],
            action="claim",
        )


def test_message_text_and_refs_are_never_executed_or_read(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    sender = register(store, client="claude-code", name="开发")
    receiver = register(store, client="codex", name="审计")
    marker = tmp_path / "must-not-exist"
    missing_ref = tmp_path / "must-not-be-read"
    sent = store.send(
        authorized_session_id=sender["session_id"],
        sender_participant_id=sender["participant_id"],
        conversation_id="tools-room",
        body_text=f"touch {marker}",
        audience_kind="participant",
        audience_value=receiver["participant_id"],
        refs=[{"path": str(missing_ref), "label": "仅元数据"}],
    )

    received = store.wait_messages(
        participant_id=receiver["participant_id"], wait_seconds=0
    )["messages"]
    assert received[0]["message_id"] == sent["message_id"]
    assert received[0]["refs"][0]["path"] == str(missing_ref)
    assert not marker.exists()
    assert not missing_ref.exists()


def test_role_question_is_claimed_by_only_one_worker(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    sender = register(store, client="claude-code", name="开发")
    first = register(store, client="codex", name="审计一", roles=["reviewer"])
    second = register(store, client="opencode", name="审计二", roles=["reviewer"])
    observer = register(store, client="hermes", name="旁观者")
    question = store.send(
        authorized_session_id=sender["session_id"],
        sender_participant_id=sender["participant_id"],
        conversation_id="tools-room",
        body_text="谁来审查这个事务边界？",
        audience_kind="role",
        audience_value="reviewer",
    )

    claimed = store.wait_messages(
        participant_id=first["participant_id"], wait_seconds=0
    )["messages"]
    assert claimed[0]["message_id"] == question["message_id"]
    assert claimed[0]["claimed_by"] == first["participant_id"]
    assert store.wait_messages(
        participant_id=second["participant_id"], wait_seconds=0
    )["messages"] == []
    observed = store.wait_messages(
        participant_id=observer["participant_id"], wait_seconds=0
    )["messages"][0]
    assert observed["message_id"] == question["message_id"]
    assert observed["delivery"]["priority"] == "normal"
    assert observed["delivery"]["actionable"] is False


def test_role_claim_is_atomic_under_concurrent_waiters(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    sender = register(store, client="custom-planner", name="派发者")
    first = register(store, client="custom-worker", name="工作者一", roles=["worker"])
    second = register(store, client="custom-worker", name="工作者二", roles=["worker"])
    question = store.send(
        authorized_session_id=sender["session_id"],
        sender_participant_id=sender["participant_id"],
        conversation_id="tools-room",
        body_text="只有一个工作者应该领取",
        audience_kind="role",
        audience_value="worker",
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                store.wait_messages,
                participant_id=item["participant_id"],
                wait_seconds=0.5,
            )
            for item in (first, second)
        ]
        results = [future.result(timeout=2) for future in futures]

    deliveries = [
        message
        for result in results
        for message in result["messages"]
        if message["message_id"] == question["message_id"]
    ]
    assert len(deliveries) == 1
    assert deliveries[0]["claimed_by"] in {
        first["participant_id"],
        second["participant_id"],
    }

    responder = next(
        participant
        for participant in (first, second)
        if participant["participant_id"] == deliveries[0]["claimed_by"]
    )
    reply = store.reply(
        authorized_session_id=responder["session_id"],
        participant_id=responder["participant_id"],
        message_id=question["message_id"],
        body_text="我来审查。",
    )
    loser = next(
        participant
        for participant in (first, second)
        if participant["participant_id"] != responder["participant_id"]
    )
    loser_messages = store.wait_messages(
        participant_id=loser["participant_id"], wait_seconds=0
    )["messages"]
    assert {item["message_id"] for item in loser_messages} == {
        question["message_id"],
        reply["reply"]["message_id"],
    }
    resolved_question = next(
        item for item in loser_messages if item["message_id"] == question["message_id"]
    )
    assert resolved_question["delivery"]["priority"] == "important"
    assert resolved_question["delivery"]["actionable"] is False


def test_wait_unblocks_when_another_store_sends(tmp_path: Path) -> None:
    first_store = make_store(tmp_path)
    second_store = BridgeStore(tmp_path / "bridge.db", poll_interval_seconds=0.05)
    receiver = register(first_store, client="codex", name="等待者")
    sender = register(first_store, client="claude-code", name="发送者")

    started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            second_store.wait_messages,
            participant_id=receiver["participant_id"],
            wait_seconds=2,
        )
        time.sleep(0.15)
        first_store.send(
            authorized_session_id=sender["session_id"],
            sender_participant_id=sender["participant_id"],
            conversation_id="tools-room",
            body_text="立即送达",
            audience_kind="participant",
            audience_value=receiver["participant_id"],
        )
        result = future.result(timeout=3)

    assert result["count"] == 1
    assert time.monotonic() - started < 1.5


def test_notification_wait_is_metadata_only_and_reconnect_safe(tmp_path: Path) -> None:
    first_store = make_store(tmp_path)
    second_store = BridgeStore(tmp_path / "bridge.db", poll_interval_seconds=0.05)
    receiver = register(first_store, client="codex", name="远端监听者")
    sender = register(first_store, client="claude-code", name="通知发送者")

    initial = second_store.notification_snapshot(
        participant_id=receiver["participant_id"],
        authorized_session_id=receiver["session_id"],
        after_sequence=0,
    )
    assert initial["has_new"] is False
    assert initial["has_room_activity"] is False
    assert initial["cursor"] == 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            second_store.wait_for_notification,
            participant_id=receiver["participant_id"],
            authorized_session_id=receiver["session_id"],
            after_sequence=initial["cursor"],
            wait_seconds=2,
        )
        time.sleep(0.1)
        sent = first_store.send(
            authorized_session_id=sender["session_id"],
            sender_participant_id=sender["participant_id"],
            conversation_id="tools-room",
            body_text="正文不进入唤醒事件。",
            audience_kind="participant",
            audience_value=receiver["participant_id"],
        )
        notification = future.result(timeout=3)

    assert notification["timed_out"] is False
    assert notification["has_new"] is True
    assert notification["has_room_activity"] is True
    assert notification["cursor"] == sent["sequence"]
    assert notification["new_since_cursor"]["pending_count"] == 1
    assert notification["new_since_cursor"]["priority_counts"]["mention"] == 1
    assert notification["room_activity_since_cursor"]["priority_counts"] == {
        "mention": 1,
        "important": 0,
        "normal": 0,
    }
    assert "正文不进入唤醒事件" not in str(notification)
    with first_store._connection() as connection:
        delivery = connection.execute(
            "SELECT state, attempt_count FROM message_deliveries "
            "WHERE message_id = ? AND participant_id = ?",
            (sent["message_id"], receiver["participant_id"]),
        ).fetchone()
    assert delivery["state"] == "pending"
    assert delivery["attempt_count"] == 0

    reconnected = BridgeStore(tmp_path / "bridge.db")
    replay = reconnected.notification_snapshot(
        participant_id=receiver["participant_id"],
        authorized_session_id=receiver["session_id"],
        after_sequence=notification["cursor"],
    )
    assert replay["has_new"] is False
    assert replay["has_room_activity"] is False
    assert replay["backlog"]["pending_count"] == 1

    # A corrupt persisted cursor must be clamped to a sequence the server has
    # actually issued; otherwise one typo could suppress notifications forever.
    clamped = reconnected.notification_snapshot(
        participant_id=receiver["participant_id"],
        authorized_session_id=receiver["session_id"],
        after_sequence=999_999_999,
    )
    assert clamped["cursor"] == sent["sequence"]
    expire_sender_cooldown(
        first_store,
        participant_id=sender["participant_id"],
        conversation_id="tools-room",
    )
    later = first_store.send(
        authorized_session_id=sender["session_id"],
        sender_participant_id=sender["participant_id"],
        conversation_id="tools-room",
        body_text="损坏游标后仍能收到下一条。",
        audience_kind="room",
    )
    after_clamp = reconnected.notification_snapshot(
        participant_id=receiver["participant_id"],
        authorized_session_id=receiver["session_id"],
        after_sequence=clamped["cursor"],
    )
    assert after_clamp["has_new"] is True
    assert after_clamp["has_room_activity"] is True
    assert after_clamp["cursor"] == later["sequence"]

    first_store.message_action(
        participant_id=receiver["participant_id"],
        message_id=later["message_id"],
        action="ack",
        authorized_session_id=receiver["session_id"],
    )
    already_processed = reconnected.notification_snapshot(
        participant_id=receiver["participant_id"],
        authorized_session_id=receiver["session_id"],
        after_sequence=sent["sequence"],
    )
    assert already_processed["has_room_activity"] is True
    assert already_processed["room_activity_since_cursor"]["activity_count"] == 1
    assert already_processed["has_new"] is False
    assert already_processed["new_since_cursor"]["pending_count"] == 0


def test_version_eleven_migration_promotes_existing_explicit_mentions(
    tmp_path: Path,
) -> None:
    database = tmp_path / "bridge.db"
    store = BridgeStore(database)
    store.create_user_room("mention-migration")
    sender = store.register_agent_session(
        product="codex",
        username="迁移发送者",
        signature="发送提醒。",
        conversation_id="mention-migration",
    )
    receiver = store.register_agent_session(
        product="claude-code",
        username="迁移接收者",
        signature="接收提醒。",
        conversation_id="mention-migration",
    )
    message = store.send(
        authorized_session_id=sender["session_id"],
        sender_participant_id=sender["participant_id"],
        conversation_id="mention-migration",
        body_text="旧版本结构化 @。",
        audience_kind="room",
        mentions=[receiver["participant_id"]],
    )
    with store._transaction() as connection:
        connection.execute(
            "UPDATE message_deliveries SET priority = 'important' "
            "WHERE message_id = ? AND participant_id = ?",
            (message["message_id"], receiver["participant_id"]),
        )
        connection.execute("PRAGMA user_version = 10")

    migrated = BridgeStore(database)
    with migrated._connection() as connection:
        raw = connection.execute(
            "SELECT priority, reasons_json FROM message_deliveries "
            "WHERE message_id = ? AND participant_id = ?",
            (message["message_id"], receiver["participant_id"]),
        ).fetchone()
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    assert version == 19
    assert raw["priority"] == "direct"
    assert "agent_mention" in raw["reasons_json"]
    assert '"mention"' not in raw["reasons_json"]
    delivered = migrated.wait_messages(
        participant_id=receiver["participant_id"],
        authorized_session_id=receiver["session_id"],
        wait_seconds=0,
    )["messages"]
    assert delivered[0]["delivery"]["priority"] == "mention"


def test_data_persists_across_store_instances(tmp_path: Path) -> None:
    first_store = make_store(tmp_path)
    sender = register(first_store, client="codex", name="发送者")
    receiver = register(first_store, client="hermes", name="接收者")
    message = first_store.send(
        authorized_session_id=sender["session_id"],
        sender_participant_id=sender["participant_id"],
        conversation_id="tools-room",
        body_text="持久消息",
        audience_kind="participant",
        audience_value=receiver["participant_id"],
        refs=[{"path": "/not/read/by/bridge", "sha256": "a" * 64}],
    )

    reopened = BridgeStore(tmp_path / "bridge.db")
    result = reopened.wait_messages(
        participant_id=receiver["participant_id"], wait_seconds=0
    )
    assert result["messages"][0]["message_id"] == message["message_id"]
    assert result["messages"][0]["refs"][0]["path"] == "/not/read/by/bridge"


def test_delivery_migration_keeps_group_history_without_false_old_backlog(
    tmp_path: Path,
) -> None:
    database = tmp_path / "bridge.db"
    store = BridgeStore(database, poll_interval_seconds=0.05)
    sender = register(store, client="claude-code", name="旧发送者")
    target = register(store, client="codex", name="旧目标")
    observer = register(store, client="opencode", name="旧旁观者")
    resolved = store.send(
        authorized_session_id=sender["session_id"],
        sender_participant_id=sender["participant_id"],
        conversation_id="tools-room",
        body_text="旧版定向消息现在是公开 @。",
        audience_kind="participant",
        audience_value=target["participant_id"],
    )
    store.message_action(
        participant_id=target["participant_id"],
        message_id=resolved["message_id"],
        action="ack",
        authorized_session_id=target["session_id"],
    )
    unresolved = store.send(
        authorized_session_id=target["session_id"],
        sender_participant_id=target["participant_id"],
        conversation_id="tools-room",
        body_text="升级时仍未处理的群消息。",
        audience_kind="room",
    )
    historical_reply = store.send(
        authorized_session_id=observer["session_id"],
        sender_participant_id=observer["participant_id"],
        conversation_id="tools-room",
        body_text="这是升级前已经存在的历史引用。",
        audience_kind="room",
        reply_to=unresolved["message_id"],
    )

    # Simulate a pre-delivery-ledger database while preserving all durable
    # source rows.  Initializing the upgraded store must only add projections.
    with store._transaction() as connection:
        before_counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("participants", "memberships", "messages", "receipts")
        }
        connection.execute("DROP TABLE message_deliveries")
        connection.execute("PRAGMA user_version = 6")

    migrated = BridgeStore(database, poll_interval_seconds=0.05)
    with migrated._connection() as connection:
        after_counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("participants", "memberships", "messages", "receipts")
        }
        resolved_deliveries = connection.execute(
            "SELECT participant_id, state, actionable FROM message_deliveries "
            "WHERE message_id = ? ORDER BY participant_id",
            (resolved["message_id"],),
        ).fetchall()
        historical_reply_delivery = connection.execute(
            "SELECT priority, reasons_json FROM message_deliveries "
            "WHERE message_id = ? AND participant_id = ?",
            (historical_reply["message_id"], target["participant_id"]),
        ).fetchone()
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    assert after_counts == before_counts
    assert version == 19
    assert len(resolved_deliveries) == 2
    assert {row["state"] for row in resolved_deliveries} == {"acked"}
    assert {int(row["actionable"]) for row in resolved_deliveries} == {0}
    assert historical_reply_delivery["priority"] == "normal"
    assert "reply_wake" not in historical_reply_delivery["reasons_json"]

    observer_history = migrated.history(
        participant_id=observer["participant_id"],
        authorized_session_id=observer["session_id"],
        conversation_id="tools-room",
    )
    assert [item["message_id"] for item in observer_history["messages"]] == [
        resolved["message_id"],
        unresolved["message_id"],
        historical_reply["message_id"],
    ]
    observer_backlog = migrated.notification_snapshot(
        participant_id=observer["participant_id"],
        authorized_session_id=observer["session_id"],
        after_sequence=0,
    )["backlog"]
    assert observer_backlog["pending_count"] == 1
    assert observer_backlog["newest_sequence"] == unresolved["sequence"]

    dashboard_messages = ViewerRepository(database).messages("tools-room")
    resolved_view = next(
        item for item in dashboard_messages if item["message_id"] == resolved["message_id"]
    )
    assert resolved_view["ack_count"] == 1
    assert resolved_view["receipt_count"] == 2


def test_resume_preserves_id_and_rejects_client_type_change(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    participant = register(store, client="codex", name="原会话")
    resumed = store.register(
        client_type=participant["client_type"],
        session_alias=participant["session_alias"],
        conversation_id="tools-room",
        roles=["reviewer"],
        resume_participant_id=participant["participant_id"],
    )
    assert resumed["participant_id"] == participant["participant_id"]
    with pytest.raises(ConflictError, match="session_alias is immutable"):
        store.register(
            client_type=participant["client_type"],
            session_alias="恢复会话",
            conversation_id="tools-room",
            resume_participant_id=participant["participant_id"],
        )
    with pytest.raises(ConflictError):
        store.register(
            client_type="claude-code-另一个用户",
            session_alias=participant["session_alias"],
            conversation_id="tools-room",
            resume_participant_id=participant["participant_id"],
        )
    with store._connection() as connection:
        unchanged = connection.execute(
            "SELECT client_type, session_alias FROM participants "
            "WHERE participant_id = ?",
            (participant["participant_id"],),
        ).fetchone()
    assert unchanged["client_type"] == participant["client_type"]
    assert unchanged["session_alias"] == participant["session_alias"]


def test_database_trigger_blocks_direct_identity_changes(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    participant = register(store, client="codex", name="固定名字")

    with pytest.raises(sqlite3.IntegrityError, match="PARTICIPANT_IDENTITY_IMMUTABLE"):
        with store._transaction() as connection:
            connection.execute(
                "UPDATE participants SET session_alias = ? WHERE participant_id = ?",
                ("偷改名字", participant["participant_id"]),
            )


def test_open_registration_requires_existing_room_and_stores_only_token_hash(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    with pytest.raises(NotFoundError, match="unknown conversation"):
        store.register_agent_session(
            conversation_id="missing-room",
            product="codex",
            username="小团子",
            session_alias="群聊气氛助手",
        )

    store.create_user_room("open-room")
    registered = store.register_agent_session(
        conversation_id="open-room",
        product="codex",
        username="小团子",
        session_alias="群聊气氛助手",
        roles=["host"],
    )
    with store._connection() as connection:
        stored_token = connection.execute(
            "SELECT token_hash FROM agent_sessions WHERE session_id = ?",
            (registered["session_id"],),
        ).fetchone()[0]
        invite_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'invites'"
        ).fetchone()
    assert stored_token != registered["access_token"]
    assert len(stored_token) == 64
    assert invite_table is None


def test_same_fixed_identity_keeps_sessions_and_renews_them_sliding(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    first = register(store, client="codex", name="固定身份")
    second = store.register_agent_session(
        product="codex",
        username="固定身份",
        session_alias="固定身份",
        conversation_id="tools-room",
    )
    assert second["participant_id"] == first["participant_id"]
    with store._transaction() as connection:
        connection.execute(
            "UPDATE agent_sessions SET expires_at = ?, ttl_seconds = 600 "
            "WHERE session_id = ?",
            (time.time() + 1, first["session_id"]),
        )
    renewed = store.authenticate_session(first["access_token"])
    assert renewed["expires_at"] > time.time() + 590
    assert renewed["renewal_mode"] == "sliding"
    assert store.authenticate_session(second["access_token"])["session_id"] == second[
        "session_id"
    ]
    legacy_reconnect = store.register_agent_session(
        product="codex",
        username="固定身份",
        session_alias="旧客户端换了会话用途",
        conversation_id="tools-room",
    )
    assert legacy_reconnect["participant_id"] == first["participant_id"]
    assert legacy_reconnect["session_alias"] == first["session_alias"]
    assert legacy_reconnect["signature"] == first["signature"]


def test_initialize_preserves_recent_legacy_session_for_sliding_renewal(
    tmp_path: Path,
) -> None:
    database = tmp_path / "recent-legacy-session.db"
    store = BridgeStore(database)
    store.create_user_room("兼容旧会话")
    registered = store.register_agent_session(
        product="codex",
        username="recent-legacy",
        session_alias="最近活跃的旧会话",
        conversation_id="兼容旧会话",
        session_ttl_seconds=600,
    )
    stale = store.register_agent_session(
        product="codex",
        username="stale-legacy",
        session_alias="长期失效的旧会话",
        conversation_id="兼容旧会话",
        session_ttl_seconds=600,
    )
    now = time.time()
    with store._transaction() as conn:
        conn.execute(
            "UPDATE agent_sessions SET expires_at = ?, last_seen = ?, "
            "ttl_seconds = 600 WHERE session_id = ?",
            (now - 1, now - 10, registered["session_id"]),
        )
        conn.execute(
            "UPDATE agent_sessions SET expires_at = ?, last_seen = ?, "
            "ttl_seconds = 600 WHERE session_id = ?",
            (now - 1, now - 700, stale["session_id"]),
        )
        conn.execute("PRAGMA user_version = 9")

    restarted = BridgeStore(database)
    renewed = restarted.authenticate_session(registered["access_token"])
    assert renewed["session_id"] == registered["session_id"]
    assert renewed["expires_at"] > time.time() + 590
    with pytest.raises(AuthenticationError, match="expired"):
        restarted.authenticate_session(stale["access_token"])


def test_inactive_sessions_can_be_cleared_without_deleting_audit_links(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    expired = register(store, client="codex", name="会过期")
    active = register(store, client="opencode", name="仍在线")
    nickname = store.request_nickname(
        participant_id=expired["participant_id"],
        authorized_session_id=expired["session_id"],
        requested_display_name="旧会话申请的昵称",
    )
    now = time.time()
    with store._transaction() as connection:
        connection.execute(
            "UPDATE agent_sessions SET expires_at = ? WHERE session_id = ?",
            (now - 1, expired["session_id"]),
        )

    cleared = store.clear_inactive_sessions(now=now)
    assert cleared == {
        "cleared_count": 1,
        "cleared_at": now,
        "mode": "logical",
        "audit_links_preserved": True,
    }
    assert store.clear_inactive_sessions(now=now + 1)["cleared_count"] == 0
    with store._connection() as connection:
        expired_row = connection.execute(
            "SELECT cleared_at FROM agent_sessions WHERE session_id = ?",
            (expired["session_id"],),
        ).fetchone()
        nickname_row = connection.execute(
            "SELECT requested_session_id FROM nickname_requests WHERE request_id = ?",
            (nickname["request_id"],),
        ).fetchone()
    assert expired_row["cleared_at"] == now
    assert nickname_row["requested_session_id"] == expired["session_id"]
    with pytest.raises(AuthenticationError, match="cleared"):
        store.authenticate_session(expired["access_token"])
    assert store.authenticate_session(active["access_token"])["session_id"] == active[
        "session_id"
    ]
    repository = ViewerRepository(tmp_path / "bridge.db")
    assert [item["session_id"] for item in repository.sessions()] == [
        active["session_id"]
    ]
    assert repository.session_stats() == {
        "active_count": 1,
        "clearable_count": 0,
    }


def test_signature_and_owner_approved_nickname_are_separate_from_identity(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    store.create_user_room("profile-room")
    registered = store.register_agent_session(
        product="codex",
        username="固定身份",
        signature="喜欢把复杂系统说清楚。",
        conversation_id="profile-room",
    )
    assert registered["client_type"] == "codex-固定身份"
    assert registered["display_name"] == "codex-固定身份"
    assert registered["signature"] == "喜欢把复杂系统说清楚。"

    request = store.request_nickname(
        participant_id=registered["participant_id"],
        authorized_session_id=registered["session_id"],
        requested_display_name="小团子",
    )
    assert request["status"] == "pending"
    assert request["current_display_name"] == "codex-固定身份"
    with pytest.raises(ConflictError, match="still pending"):
        store.request_nickname(
            participant_id=registered["participant_id"],
            authorized_session_id=registered["session_id"],
            requested_display_name="另一昵称",
        )

    approved = store.review_nickname_request(
        request_id=request["request_id"],
        action="approve",
    )
    assert approved["status"] == "approved"
    assert approved["current_display_name"] == "小团子"
    participants = store.participants(
        participant_id=registered["participant_id"],
        conversation_id="profile-room",
        authorized_session_id=registered["session_id"],
    )["participants"]
    assert participants[0]["display_name"] == "小团子"

    updated = store.update_profile(
        participant_id=registered["participant_id"],
        authorized_session_id=registered["session_id"],
        signature="更喜欢底层单一权威。",
    )
    assert updated["signature"] == "更喜欢底层单一权威。"
    assert updated["display_name"] == "小团子"
    with pytest.raises(NicknameRateLimitError):
        store.request_nickname(
            participant_id=registered["participant_id"],
            authorized_session_id=registered["session_id"],
            requested_display_name="今天不能再换",
        )

    with pytest.raises(sqlite3.IntegrityError, match="NICKNAME_APPROVAL_REQUIRED"):
        with store._transaction() as connection:
            connection.execute(
                "UPDATE participants SET display_name = '绕过审批' "
                "WHERE participant_id = ?",
                (registered["participant_id"],),
            )


def test_database_rejects_message_without_live_mcp_session(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    sender = register(store, client="codex", name="认证发送者")
    with pytest.raises(sqlite3.IntegrityError, match="AUTHORIZED_SENDER_REQUIRED"):
        with store._transaction() as connection:
            connection.execute(
                """
                INSERT INTO messages
                    (message_id, conversation_id, sender_participant_id,
                     audience_kind, audience_value, message_kind, body,
                     refs_json, status, created_at, updated_at)
                VALUES (?, ?, ?, 'room', '*', 'message', ?, '[]', 'open', ?, ?)
                """,
                (
                    "msg_unauthenticated_direct_write",
                    "tools-room",
                    sender["participant_id"],
                    "脚本不能直接写入",
                    time.time(),
                    time.time(),
                ),
            )


def test_owner_web_message_uses_fixed_identity_and_independent_cooldown(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    agent = register(store, client="codex", name="群聊成员")

    owner_message = store.send_owner_message(
        conversation_id="tools-room",
        body_text="这是网页用户发出的消息。",
    )
    assert owner_message["sender_participant_id"] == "participant_web_owner"

    with pytest.raises(RateLimitError) as limited:
        store.send_owner_message(
            conversation_id="tools-room",
            body_text="网页用户发得太快。",
        )
    assert 0 < limited.value.retry_after_seconds <= MESSAGE_COOLDOWN_SECONDS

    # 限频按“发送者 + 房间”隔离；网页用户刚说过话不影响 Agent。
    agent_message = store.send(
        authorized_session_id=agent["session_id"],
        sender_participant_id=agent["participant_id"],
        conversation_id="tools-room",
        body_text="Agent 仍可独立发言。",
    )
    assert agent_message["sender_participant_id"] == agent["participant_id"]

    with store._connection() as connection:
        stored = connection.execute(
            "SELECT authorized_session_id FROM messages WHERE message_id = ?",
            (owner_message["message_id"],),
        ).fetchone()
    assert stored["authorized_session_id"] == "owner_web_ui"

    with pytest.raises(sqlite3.IntegrityError, match="AUTHORIZED_SENDER_REQUIRED"):
        with store._transaction() as connection:
            now = time.time()
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
                    "msg_spoofed_owner_binding",
                    "tools-room",
                    agent["participant_id"],
                    "不能借用网页授权冒充 Agent。",
                    "owner_web_ui",
                    now,
                    now,
                ),
            )


def test_nonmember_cannot_read_or_ack_other_room(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    sender = register(store, client="codex", name="房间一", room="room-one")
    receiver = register(store, client="claude-code", name="房间一接收", room="room-one")
    outsider = register(store, client="opencode", name="房间二", room="room-two")
    message = store.send(
        authorized_session_id=sender["session_id"],
        sender_participant_id=sender["participant_id"],
        conversation_id="room-one",
        body_text="房间内消息",
        audience_kind="participant",
        audience_value=receiver["participant_id"],
    )
    with pytest.raises(ConflictError):
        store.history(
            participant_id=outsider["participant_id"],
            conversation_id="room-one",
        )
    with pytest.raises(ConflictError):
        store.message_action(
            participant_id=outsider["participant_id"],
            message_id=message["message_id"],
            action="ack",
        )


def test_invalid_ids_and_oversized_body_are_rejected(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    with pytest.raises(ValidationError):
        register(store, client="codex", name="坏房间", room="../escape")
    participant = register(store, client="codex", name="正常")
    named = register(store, client="codex", name="小可爱")
    assert named["client_type"] == "codex-小可爱"
    with pytest.raises(ValidationError):
        register(store, client="codex/非法", name="非法身份")
    with pytest.raises(ValidationError):
        store.send(
            authorized_session_id=participant["session_id"],
            sender_participant_id=participant["participant_id"],
            conversation_id="tools-room",
            body_text="x" * 65_537,
        )


def test_chinese_room_names_are_supported_without_weakening_internal_ids(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    sender = register(store, client="codex", name="中文房主", room="大家沟通群")
    receiver = register(
        store,
        client="claude-code",
        name="中文成员",
        room="大家沟通群",
    )
    sent = store.send(
        authorized_session_id=sender["session_id"],
        sender_participant_id=sender["participant_id"],
        conversation_id="大家沟通群",
        body_text="中文房间可以正常通信。",
        audience_kind="participant",
        audience_value=receiver["participant_id"],
    )
    received = store.wait_messages(
        participant_id=receiver["participant_id"],
        wait_seconds=0,
    )
    assert received["messages"][0]["message_id"] == sent["message_id"]
    assert received["messages"][0]["conversation_id"] == "大家沟通群"

    with pytest.raises(ValidationError):
        store.heartbeat("参与者中文")
    with pytest.raises(ValidationError):
        store.create_user_room("非法/房间")


def test_room_names_are_unicode_normalized_before_uniqueness_check(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    first = store.create_user_room("讨论 e\u0301")
    assert first["conversation_id"] == "讨论 é"
    with pytest.raises(ConflictError, match="already exists"):
        store.create_user_room("讨论 é")


def test_product_username_identity_is_globally_unique(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first = register(store, client="codex", name="小傻瓜", room="room-one")
    assert first["client_type"] == "codex-小傻瓜"

    with pytest.raises(ConflictError, match="already registered"):
        register(store, client="codex", name="小傻瓜", room="room-two")

    second = register(store, client="codex", name="大笨蛋", room="room-two")
    assert second["client_type"] == "codex-大笨蛋"


def test_presence_and_participant_listing(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first = register(store, client="codex", name="在线", roles=["reviewer"])
    second = register(store, client="claude-code", name="离线")
    store.heartbeat(second["participant_id"], status="offline")

    listing = store.participants(
        participant_id=first["participant_id"],
        conversation_id="tools-room",
    )
    by_id = {item["participant_id"]: item for item in listing["participants"]}
    assert by_id[first["participant_id"]]["status"] == "online"
    assert by_id[first["participant_id"]]["roles"] == ["reviewer"]
    assert by_id[second["participant_id"]]["status"] == "offline"


def test_arbitrary_agent_types_sessions_and_broadcast_join_boundary(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first = register(store, client="my-agent", name="my-agent 会话一")
    second = register(store, client="my-agent", name="my-agent 会话二")
    sender = register(store, client="future-agent-v9", name="未来 Agent")
    assert first["participant_id"] != second["participant_id"]

    old_broadcast = store.send(
        authorized_session_id=sender["session_id"],
        sender_participant_id=sender["participant_id"],
        conversation_id="tools-room",
        body_text="加入前广播",
        audience_kind="broadcast",
    )
    assert store.wait_messages(
        participant_id=first["participant_id"], wait_seconds=0
    )["messages"][0]["message_id"] == old_broadcast["message_id"]

    late = register(store, client="another-agent", name="稍后加入")
    assert store.wait_messages(
        participant_id=late["participant_id"], wait_seconds=0
    )["messages"] == []

    expire_sender_cooldown(
        store,
        participant_id=sender["participant_id"],
        conversation_id="tools-room",
    )

    new_broadcast = store.send(
        authorized_session_id=sender["session_id"],
        sender_participant_id=sender["participant_id"],
        conversation_id="tools-room",
        body_text="加入后广播",
        audience_kind="broadcast",
    )
    assert store.wait_messages(
        participant_id=late["participant_id"], wait_seconds=0
    )["messages"][0]["message_id"] == new_broadcast["message_id"]


def test_message_cooldown_is_per_sender_and_per_room(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    sender = register(store, client="codex", name="限频发送者", room="room-one")
    other = register(store, client="claude-code", name="另一发送者", room="room-one")
    first = store.send(
        authorized_session_id=sender["session_id"],
        sender_participant_id=sender["participant_id"],
        conversation_id="room-one",
        body_text="第一条",
    )

    with pytest.raises(RateLimitError) as limited:
        store.send(
            authorized_session_id=sender["session_id"],
            sender_participant_id=sender["participant_id"],
            conversation_id="room-one",
            body_text="太快的第二条",
        )
    assert 0 < limited.value.retry_after_seconds <= MESSAGE_COOLDOWN_SECONDS
    assert limited.value.conversation_id == "room-one"

    other_message = store.send(
        authorized_session_id=other["session_id"],
        sender_participant_id=other["participant_id"],
        conversation_id="room-one",
        body_text="另一个人不受影响",
    )
    store.create_user_room("room-two")
    store.register(
        client_type=sender["client_type"],
        session_alias=sender["session_alias"],
        conversation_id="room-two",
        resume_participant_id=sender["participant_id"],
    )
    another_room = store.send(
        authorized_session_id=sender["session_id"],
        sender_participant_id=sender["participant_id"],
        conversation_id="room-two",
        body_text="同一个人在另一个房间可以说话",
    )

    expire_sender_cooldown(
        store,
        participant_id=sender["participant_id"],
        conversation_id="room-one",
    )
    after_cooldown = store.send(
        authorized_session_id=sender["session_id"],
        sender_participant_id=sender["participant_id"],
        conversation_id="room-one",
        body_text="冷却后可以再说",
    )
    assert {first["body"], other_message["body"], another_room["body"], after_cooldown["body"]} == {
        "第一条",
        "另一个人不受影响",
        "同一个人在另一个房间可以说话",
        "冷却后可以再说",
    }


def test_message_cooldown_is_atomic_for_concurrent_sends(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    sender = register(store, client="codex", name="并发发送者")

    def send(text: str) -> str:
        return store.send(
            authorized_session_id=sender["session_id"],
            sender_participant_id=sender["participant_id"],
            conversation_id="tools-room",
            body_text=text,
        )["message_id"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(send, text) for text in ("并发一", "并发二")]
        outcomes: list[str] = []
        for future in futures:
            try:
                future.result(timeout=3)
                outcomes.append("sent")
            except RateLimitError:
                outcomes.append("limited")

    assert outcomes.count("sent") == 1
    assert outcomes.count("limited") == 1
    with store._connection() as connection:
        count = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert count == 1


def test_database_trigger_blocks_cooldown_bypass(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    sender = register(store, client="codex", name="触发器发送者")
    first = store.send(
        authorized_session_id=sender["session_id"],
        sender_participant_id=sender["participant_id"],
        conversation_id="tools-room",
        body_text="第一条",
    )

    with pytest.raises(sqlite3.IntegrityError, match="MESSAGE_RATE_LIMITED"):
        with store._transaction() as connection:
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
                    "msg_direct_cooldown_bypass",
                    "tools-room",
                    sender["participant_id"],
                    "绕过",
                    sender["session_id"],
                    first["created_at"] + 1.0,
                    first["created_at"] + 1.0,
                ),
            )


def test_quoted_replies_are_one_level_and_messages_can_be_acked(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    sender = register(store, client="claude-code", name="问题发起者")
    receiver = register(store, client="codex", name="回答者")
    original = store.send(
        authorized_session_id=sender["session_id"],
        sender_participant_id=sender["participant_id"],
        conversation_id="tools-room",
        body_text="这是通知",
        audience_kind="participant",
        audience_value=receiver["participant_id"],
    )
    quoted = store.reply(
        authorized_session_id=receiver["session_id"],
        participant_id=receiver["participant_id"],
        message_id=original["message_id"],
        body_text="引用回复",
    )["reply"]
    with pytest.raises(ConflictError, match="one level"):
        store.send(
            authorized_session_id=sender["session_id"],
            sender_participant_id=sender["participant_id"],
            conversation_id="tools-room",
            body_text="不能继续套娃引用",
            audience_kind="participant",
            audience_value=receiver["participant_id"],
            reply_to=quoted["message_id"],
        )
    acked = store.message_action(
        participant_id=sender["participant_id"],
        message_id=quoted["message_id"],
        action="ack",
    )
    assert acked["action"] == "ack"


def test_database_trigger_blocks_nested_quoted_reply(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    sender = register(store, client="claude-code", name="通知者")
    receiver = register(store, client="codex", name="接收者")
    third = register(store, client="opencode", name="第三人")
    original = store.send(
        authorized_session_id=sender["session_id"],
        sender_participant_id=sender["participant_id"],
        conversation_id="tools-room",
        body_text="终态通知",
        audience_kind="participant",
        audience_value=receiver["participant_id"],
    )
    quoted = store.reply(
        authorized_session_id=receiver["session_id"],
        participant_id=receiver["participant_id"],
        message_id=original["message_id"],
        body_text="第一层引用",
    )["reply"]

    with pytest.raises(sqlite3.IntegrityError, match="REPLY_CHAIN_NOT_ALLOWED"):
        with store._transaction() as connection:
            connection.execute(
                """
                INSERT INTO messages
                    (message_id, conversation_id, sender_participant_id,
                     audience_kind, audience_value, message_kind, body,
                     refs_json, reply_to, status, authorized_session_id,
                     created_at, updated_at)
                VALUES (?, ?, ?, 'participant', ?, 'message', ?, '[]', ?, 'open', ?, ?, ?)
                """,
                (
                    "msg_direct_bad_reply",
                    "tools-room",
                    third["participant_id"],
                    sender["participant_id"],
                    "不该出现的嵌套回复",
                    quoted["message_id"],
                    third["session_id"],
                    quoted["created_at"] + 1.0,
                    quoted["created_at"] + 1.0,
                ),
            )


def test_agent_room_creation_limit_is_bound_to_participant_session(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    owner = register(store, client="codex", name="建房会话", room="owned-one")
    second = store.register(
        client_type=owner["client_type"],
        session_alias="建房会话",
        conversation_id="owned-two",
        resume_participant_id=owner["participant_id"],
        create_room_if_missing=True,
    )
    assert second["room_created"] is True
    assert second["owned_active_room_count"] == AGENT_ACTIVE_ROOM_LIMIT

    with pytest.raises(ConflictError, match="maximum"):
        store.register(
            client_type=owner["client_type"],
            session_alias="建房会话",
            conversation_id="owned-three",
            resume_participant_id=owner["participant_id"],
            create_room_if_missing=True,
        )

    another = register(
        store,
        client="claude-code",
        name="另一个会话",
        room="owned-three",
    )
    assert another["room_created"] is True


def test_authenticated_agent_room_creation_keeps_atomic_two_room_limit(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    store.create_user_room("owner-room")
    participant = store.register_agent_session(
        conversation_id="owner-room",
        product="codex",
        username="建房小助手",
        session_alias="建房会话",
    )
    assert participant["owned_active_room_count"] == 0

    for room_name, expected_count in (("agent-one", 1), ("agent-two", 2)):
        created = store.create_agent_room(
            authorized_session_id=participant["session_id"],
            participant_id=participant["participant_id"],
            conversation_id=room_name,
        )
        assert created["owned_active_room_count"] == expected_count
    with pytest.raises(ConflictError, match="maximum"):
        store.create_agent_room(
            authorized_session_id=participant["session_id"],
            participant_id=participant["participant_id"],
            conversation_id="agent-three",
        )

    with store._connection() as connection:
        owned = connection.execute(
            "SELECT COUNT(*) FROM rooms WHERE creator_participant_id = ?",
            (participant["participant_id"],),
        ).fetchone()[0]
    assert owned == 2


def test_user_room_and_joining_rooms_do_not_consume_agent_creation_quota(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    store.create_user_room("owner-room")
    participant = store.register(
        client_type="codex-访客",
        session_alias="访客",
        conversation_id="owner-room",
    )
    assert participant["room_created"] is False
    assert participant["owned_active_room_count"] == 0

    for room in ("agent-room-one", "agent-room-two"):
        result = store.register(
            client_type=participant["client_type"],
            session_alias="访客",
            conversation_id=room,
            resume_participant_id=participant["participant_id"],
            create_room_if_missing=True,
        )
    assert result["owned_active_room_count"] == 2

    store.create_user_room("second-owner-room")
    joined = store.register(
        client_type=participant["client_type"],
        session_alias="访客",
        conversation_id="second-owner-room",
        resume_participant_id=participant["participant_id"],
    )
    assert joined["owned_active_room_count"] == 2

    with pytest.raises(NotFoundError, match="create_room_if_missing"):
        store.register(
            client_type="hermes-误入",
            session_alias="误入",
            conversation_id="missing-room",
        )


def test_agent_room_limit_is_atomic_under_concurrent_creation(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    owner = register(store, client="codex", name="并发建房", room="base-room")

    def create(room: str) -> str:
        store.register(
            client_type=owner["client_type"],
            session_alias="并发建房",
            conversation_id=room,
            resume_participant_id=owner["participant_id"],
            create_room_if_missing=True,
        )
        return room

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(create, room) for room in ("race-one", "race-two")]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(("created", future.result(timeout=3)))
            except ConflictError:
                outcomes.append(("rejected", None))

    assert [kind for kind, _ in outcomes].count("created") == 1
    assert [kind for kind, _ in outcomes].count("rejected") == 1
    with store._connection() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM rooms WHERE creator_participant_id = ? "
            "AND status = 'active'",
            (owner["participant_id"],),
        ).fetchone()[0]
    assert count == 2


def test_ninety_days_without_messages_abandons_room_but_keeps_content(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    owner = register(store, client="codex", name="旧房主", room="old-room")
    message = store.send(
        authorized_session_id=owner["session_id"],
        sender_participant_id=owner["participant_id"],
        conversation_id="old-room",
        body_text="这条历史必须永久保留。",
        audience_kind="room",
    )
    archive_time = time.time()
    with store._transaction() as connection:
        connection.execute(
            "UPDATE rooms SET last_activity_at = ? WHERE conversation_id = ?",
            (archive_time - ROOM_ABANDON_AFTER_SECONDS, "old-room"),
        )

    archived = store.archive_stale_rooms(now=archive_time)
    assert archived["archived_conversation_ids"] == ["old-room"]
    with store._connection() as connection:
        room = connection.execute(
            "SELECT * FROM rooms WHERE conversation_id = 'old-room'"
        ).fetchone()
        membership = connection.execute(
            "SELECT active FROM memberships WHERE conversation_id = 'old-room'"
        ).fetchone()
        retained = connection.execute(
            "SELECT body FROM messages WHERE message_id = ?",
            (message["message_id"],),
        ).fetchone()
    assert room["status"] == "abandoned"
    assert room["abandoned_at"] == archive_time
    assert membership["active"] == 0
    assert retained["body"] == "这条历史必须永久保留。"

    with pytest.raises(ConflictError, match="abandoned"):
        store.register(
            client_type="claude-code-迟到者",
            session_alias="迟到者",
            conversation_id="old-room",
        )
    with pytest.raises(ConflictError, match="abandoned"):
        store.send(
            authorized_session_id=owner["session_id"],
            sender_participant_id=owner["participant_id"],
            conversation_id="old-room",
            body_text="不能再说话",
        )
    with pytest.raises(ConflictError, match="abandoned"):
        store.history(
            participant_id=owner["participant_id"],
            conversation_id="old-room",
        )

    replacement = store.register(
        client_type=owner["client_type"],
        session_alias="旧房主",
        conversation_id="replacement-room",
        resume_participant_id=owner["participant_id"],
        create_room_if_missing=True,
    )
    assert replacement["room_created"] is True
    assert replacement["owned_active_room_count"] == 1


def test_message_insert_updates_authoritative_room_activity(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    sender = register(store, client="codex", name="说话者", room="active-room")
    with store._connection() as connection:
        before = connection.execute(
            "SELECT last_activity_at FROM rooms WHERE conversation_id = 'active-room'"
        ).fetchone()[0]
    sent = store.send(
        authorized_session_id=sender["session_id"],
        sender_participant_id=sender["participant_id"],
        conversation_id="active-room",
        body_text="更新活跃时间",
        audience_kind="room",
    )
    with store._connection() as connection:
        after = connection.execute(
            "SELECT last_activity_at FROM rooms WHERE conversation_id = 'active-room'"
        ).fetchone()[0]
    assert after == sent["created_at"]
    assert after >= before


def test_reusable_invitation_accepts_distinct_agents_concurrently(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    WebAuthStore(tmp_path / "bridge.db")
    store.create_user_room("并发复用邀请群")
    with store._connection() as connection:
        admin_id = str(
            connection.execute(
                "SELECT user_id FROM web_users WHERE username = 'admin'"
            ).fetchone()[0]
        )
    invitation = store.create_agent_invitation(
        conversation_id="并发复用邀请群",
        product="codex",
        requested_mode="basic",
        adapter_kind="codex",
        created_by_web_user_id=admin_id,
        reusable=True,
    )
    invitation_token = str(invitation.pop("invitation_token"))

    def accept(index: int) -> dict:
        return store.accept_agent_invitation(
            invitation_token=invitation_token,
            product="codex",
            username=f"concurrent-{index}",
            signature=f"并发接入 {index}",
            enrollment_token="enroll_" + f"{index:048d}",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        registrations = list(pool.map(accept, range(12)))

    assert len({item["participant_id"] for item in registrations}) == 12
    assert len({item["connector_id"] for item in registrations}) == 12
    listed = store.list_agent_invitations(
        requesting_web_user_id=admin_id,
    )[0]
    assert listed["status"] == "active"
    assert listed["reusable"] is True
    assert listed["use_count"] == 12
    assert listed["connector_count"] == 12
    assert listed["active_connector_count"] == 12
    with store._connection() as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_version_fourteen_invitations_migrate_without_losing_connectors(
    tmp_path: Path,
) -> None:
    database = tmp_path / "version-fourteen.db"
    store = BridgeStore(database)
    WebAuthStore(database)
    store.create_user_room("旧邀请群")
    accepted_agent = store.register_agent_session(
        product="codex",
        username="legacy-invitee",
        signature="旧版已接入 Agent。",
        conversation_id="旧邀请群",
    )
    with store._connection() as connection:
        admin_id = str(
            connection.execute(
                "SELECT user_id FROM web_users WHERE username = 'admin'"
            ).fetchone()[0]
        )

    now = time.time()
    pending_token = "invite_v14_pending_abcdefghijklmnopqrstuvwxyz"
    enrollment_token = "enroll_" + ("e" * 48)
    connection = sqlite3.connect(database)
    connection.execute("DROP TABLE agent_connectors")
    connection.execute("DROP TABLE agent_invitations")
    connection.executescript(
        """
        CREATE TABLE agent_invitations (
            invitation_id TEXT PRIMARY KEY,
            token_hash TEXT NOT NULL UNIQUE,
            conversation_id TEXT NOT NULL,
            product TEXT NOT NULL,
            requested_mode TEXT NOT NULL
                CHECK (requested_mode IN ('basic', 'resident')),
            adapter_kind TEXT NOT NULL
                CHECK (adapter_kind IN ('codex', 'claude-code', 'manual')),
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'accepted', 'revoked', 'expired')),
            created_by_web_user_id TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            accepted_at REAL,
            accepted_participant_id TEXT,
            accepted_session_id TEXT,
            connector_id TEXT UNIQUE,
            enrollment_token_hash TEXT UNIQUE,
            enrollment_last_used_at REAL,
            setup_status TEXT NOT NULL DEFAULT 'awaiting_acceptance'
                CHECK (setup_status IN (
                    'awaiting_acceptance', 'awaiting_setup', 'configured',
                    'manual', 'failed', 'revoked'
                )),
            setup_detail_json TEXT NOT NULL DEFAULT '{}',
            setup_updated_at REAL,
            connector_last_seen_at REAL,
            revoked_at REAL,
            updated_at REAL NOT NULL,
            FOREIGN KEY (conversation_id) REFERENCES rooms(conversation_id),
            FOREIGN KEY (created_by_web_user_id) REFERENCES web_users(user_id),
            FOREIGN KEY (accepted_participant_id) REFERENCES participants(participant_id),
            FOREIGN KEY (accepted_session_id) REFERENCES agent_sessions(session_id)
        );
        CREATE INDEX idx_agent_invitations_room_created
            ON agent_invitations(conversation_id, created_at DESC);
        CREATE INDEX idx_agent_invitations_status_expires
            ON agent_invitations(status, expires_at);
        CREATE INDEX idx_agent_invitations_participant
            ON agent_invitations(accepted_participant_id, status, updated_at DESC);
        CREATE INDEX idx_agent_invitations_connector
            ON agent_invitations(connector_id, status);
        """
    )
    connection.execute(
        """
        INSERT INTO agent_invitations
            (invitation_id, token_hash, conversation_id, product,
             requested_mode, adapter_kind, status, created_by_web_user_id,
             created_at, expires_at, setup_status, updated_at)
        VALUES (:invitation_id, :token_hash, :conversation_id, :product,
                'basic', 'manual', 'pending', :admin_id,
                :created_at, :expires_at, 'awaiting_acceptance', :updated_at)
        """,
        {
            "invitation_id": "agent_invitation_v14_pending",
            "token_hash": BridgeStore._secret_hash(pending_token),
            "conversation_id": "旧邀请群",
            "product": "future-agent",
            "admin_id": admin_id,
            "created_at": now - 20,
            "expires_at": now + 3600,
            "updated_at": now - 20,
        },
    )
    connection.execute(
        """
        INSERT INTO agent_invitations
            (invitation_id, token_hash, conversation_id, product,
             requested_mode, adapter_kind, status, created_by_web_user_id,
             created_at, expires_at, accepted_at, accepted_participant_id,
             accepted_session_id, connector_id, enrollment_token_hash,
             enrollment_last_used_at, setup_status, setup_detail_json,
             setup_updated_at, connector_last_seen_at, updated_at)
        VALUES (:invitation_id, :token_hash, :conversation_id, 'codex',
                'resident', 'codex', 'accepted', :admin_id,
                :created_at, :expires_at, :accepted_at, :participant_id,
                :session_id, :connector_id, :enrollment_hash,
                :accepted_at, 'configured', '{"listener_service":"legacy"}',
                :accepted_at, :accepted_at, :accepted_at)
        """,
        {
            "invitation_id": "agent_invitation_v14_accepted",
            "token_hash": BridgeStore._secret_hash(
                "invite_v14_accepted_abcdefghijklmnopqrstuvwxyz"
            ),
            "conversation_id": "旧邀请群",
            "admin_id": admin_id,
            "created_at": now - 30,
            "expires_at": now + 3600,
            "accepted_at": now - 10,
            "participant_id": accepted_agent["participant_id"],
            "session_id": accepted_agent["session_id"],
            "connector_id": "connector_v14_accepted",
            "enrollment_hash": BridgeStore._secret_hash(enrollment_token),
        },
    )
    connection.execute(
        "UPDATE agent_sessions SET connector_id = ? WHERE session_id = ?",
        ("connector_v14_accepted", accepted_agent["session_id"]),
    )
    connection.execute("PRAGMA user_version = 14")
    connection.commit()
    connection.close()

    migrated = BridgeStore(database)
    listed = {
        item["invitation_id"]: item
        for item in migrated.list_agent_invitations(
            requesting_web_user_id=admin_id,
        )
    }
    assert listed["agent_invitation_v14_pending"]["status"] == "active"
    assert listed["agent_invitation_v14_pending"]["connector_count"] == 0
    accepted = listed["agent_invitation_v14_accepted"]
    assert accepted["status"] == "exhausted"
    assert accepted["use_count"] == 1
    assert accepted["connector_count"] == 1
    assert accepted["setup_status"] == "configured"
    renewed = migrated.register_agent_session_from_enrollment(
        enrollment_token=enrollment_token,
        product="codex",
        username="legacy-invitee",
        signature="迁移后仍可续期。",
    )
    assert renewed["connector_id"] == "connector_v14_accepted"
    newly_accepted = migrated.accept_agent_invitation(
        invitation_token=pending_token,
        product="future-agent",
        username="new-after-migration",
        signature="迁移后的单次邀请仍可接受。",
    )
    assert newly_accepted["invitation_reusable"] is False
    with migrated._connection() as migrated_connection:
        assert migrated_connection.execute("PRAGMA user_version").fetchone()[0] == 19
        assert migrated_connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'agent_invitations_v14'"
        ).fetchone() is None
        assert migrated_connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_existing_database_conversations_are_backfilled_as_legacy_rooms(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy.db"
    now = time.time()
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE participants (
            participant_id TEXT PRIMARY KEY,
            client_type TEXT NOT NULL,
            session_alias TEXT NOT NULL,
            capabilities_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'online',
            created_at REAL NOT NULL,
            last_seen REAL NOT NULL
        );
        CREATE TABLE memberships (
            conversation_id TEXT NOT NULL,
            participant_id TEXT NOT NULL,
            roles_json TEXT NOT NULL DEFAULT '[]',
            active INTEGER NOT NULL DEFAULT 1,
            joined_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (conversation_id, participant_id)
        );
        """
    )
    connection.execute(
        "INSERT INTO participants VALUES (?, ?, ?, '[]', 'online', ?, ?)",
        ("participant_legacy", "codex-旧会话", "旧会话", now, now),
    )
    connection.execute(
        "INSERT INTO memberships VALUES (?, ?, '[]', 1, ?, ?)",
        ("legacy-room", "participant_legacy", now, now),
    )
    connection.commit()
    connection.close()

    store = BridgeStore(database)
    with store._connection() as migrated:
        room = migrated.execute(
            "SELECT * FROM rooms WHERE conversation_id = 'legacy-room'"
        ).fetchone()
        version = migrated.execute("PRAGMA user_version").fetchone()[0]
    assert room["creator_kind"] == "legacy"
    assert room["status"] == "active"
    assert version == 19


def test_version_four_invite_sessions_migrate_without_losing_live_tokens(
    tmp_path: Path,
) -> None:
    database = tmp_path / "version-four.db"
    now = time.time()
    access_token = "session_legacy_open_registration_token"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE participants (
            participant_id TEXT PRIMARY KEY,
            client_type TEXT NOT NULL,
            session_alias TEXT NOT NULL,
            capabilities_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'online',
            created_at REAL NOT NULL,
            last_seen REAL NOT NULL
        );
        CREATE TABLE rooms (
            conversation_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            creator_kind TEXT NOT NULL,
            creator_participant_id TEXT,
            created_at REAL NOT NULL,
            last_activity_at REAL NOT NULL,
            abandoned_at REAL
        );
        CREATE TABLE invites (
            invite_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            code_hash TEXT NOT NULL,
            allowed_product TEXT NOT NULL,
            roles_json TEXT NOT NULL DEFAULT '[]',
            capabilities_json TEXT NOT NULL DEFAULT '[]',
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            used_at REAL,
            used_by_participant_id TEXT,
            revoked_at REAL
        );
        CREATE TABLE agent_sessions (
            session_id TEXT PRIMARY KEY,
            participant_id TEXT NOT NULL,
            invite_id TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            transport TEXT NOT NULL DEFAULT 'mcp',
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            last_seen REAL NOT NULL,
            revoked_at REAL,
            revoked_reason TEXT
        );
        """
    )
    connection.execute(
        "INSERT INTO participants VALUES (?, ?, ?, '[]', 'online', ?, ?)",
        ("participant_legacy_session", "codex-旧成员", "旧会话", now, now),
    )
    connection.execute(
        "INSERT INTO rooms VALUES (?, 'active', 'user', NULL, ?, ?, NULL)",
        ("旧聊天室", now, now),
    )
    connection.execute(
        "INSERT INTO invites VALUES (?, ?, ?, ?, '[]', '[]', ?, ?, ?, ?, NULL)",
        (
            "invite_legacy",
            "旧聊天室",
            "unused_hash",
            "codex",
            now,
            now + 3600,
            now,
            "participant_legacy_session",
        ),
    )
    connection.execute(
        "INSERT INTO agent_sessions VALUES (?, ?, ?, ?, 'mcp', ?, ?, ?, NULL, NULL)",
        (
            "session_legacy",
            "participant_legacy_session",
            "invite_legacy",
            BridgeStore._secret_hash(access_token),
            now,
            now + 3600,
            now,
        ),
    )
    connection.execute("PRAGMA user_version = 4")
    connection.commit()
    connection.close()

    store = BridgeStore(database)
    authenticated = store.authenticate_session(access_token)
    assert authenticated["session_id"] == "session_legacy"
    with store._connection() as migrated:
        columns = {
            row["name"] for row in migrated.execute("PRAGMA table_info(agent_sessions)")
        }
        invite_table = migrated.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'invites'"
        ).fetchone()
        room = migrated.execute(
            "SELECT registered_conversation_id FROM agent_sessions "
            "WHERE session_id = 'session_legacy'"
        ).fetchone()
        foreign_key_errors = migrated.execute("PRAGMA foreign_key_check").fetchall()
    assert "invite_id" not in columns
    assert "registered_conversation_id" in columns
    assert "ttl_seconds" in columns
    assert invite_table is None
    assert room["registered_conversation_id"] == "旧聊天室"
    assert authenticated["display_name"] == "codex-旧成员"
    assert authenticated["signature"] == "旧会话"
    assert authenticated["renewal_mode"] == "sliding"
    assert foreign_key_errors == []


def test_agent_inactivity_uses_speech_not_heartbeat_and_requires_reinvite(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    admin_id = admin_web_user_id(store)
    store.create_user_room("十天过期群")
    agent = invite_agent(
        store,
        admin_id=admin_id,
        room="十天过期群",
        username="inactive-agent",
    )
    sent = store.send(
        authorized_session_id=agent["session_id"],
        sender_participant_id=agent["participant_id"],
        conversation_id="十天过期群",
        body_text="这条发言会成为不活跃计时锚点。",
    )
    now = time.time()
    old_speech = now - (11 * 86_400)
    with store._transaction() as connection:
        connection.execute(
            "UPDATE agent_lifecycle_states "
            "SET access_granted_at = ?, last_spoke_at = ?, updated_at = ? "
            "WHERE participant_id = ?",
            (old_speech - 60, old_speech, old_speech, agent["participant_id"]),
        )
        connection.execute(
            "UPDATE agent_sessions SET expires_at = ?, last_seen = ? "
            "WHERE session_id = ?",
            (now + 3600, now, agent["session_id"]),
        )
        connection.execute(
            "UPDATE agent_connectors SET connector_last_seen_at = ? "
            "WHERE connector_id = ?",
            (now, agent["connector_id"]),
        )

    store.heartbeat(
        agent["participant_id"],
        authorized_session_id=agent["session_id"],
    )
    with store._connection() as connection:
        state_before_cleanup = connection.execute(
            "SELECT last_spoke_at FROM agent_lifecycle_states "
            "WHERE participant_id = ?",
            (agent["participant_id"],),
        ).fetchone()
    assert state_before_cleanup["last_spoke_at"] == old_speech

    cleared = store.clear_inactive_sessions(now=now)
    assert cleared["expired_agent_count"] == 1
    with store._connection() as connection:
        membership = connection.execute(
            "SELECT active FROM memberships WHERE conversation_id = ? "
            "AND participant_id = ?",
            ("十天过期群", agent["participant_id"]),
        ).fetchone()
        session = connection.execute(
            "SELECT revoked_at, cleared_at FROM agent_sessions WHERE session_id = ?",
            (agent["session_id"],),
        ).fetchone()
        connector = connection.execute(
            "SELECT revoked_at FROM agent_connectors WHERE connector_id = ?",
            (agent["connector_id"],),
        ).fetchone()
        lifecycle = connection.execute(
            "SELECT reinvite_required, expired_reason FROM agent_lifecycle_states "
            "WHERE participant_id = ?",
            (agent["participant_id"],),
        ).fetchone()
        preserved = connection.execute(
            "SELECT COUNT(*) FROM messages WHERE message_id = ?",
            (sent["message_id"],),
        ).fetchone()[0]
    assert membership["active"] == 0
    assert session["revoked_at"] is not None and session["cleared_at"] is not None
    assert connector["revoked_at"] is not None
    assert lifecycle["reinvite_required"] == 1
    assert lifecycle["expired_reason"] == "inactive"
    assert preserved == 1
    with pytest.raises(ConflictError, match="new invitation"):
        store.register_agent_session(
            product="codex",
            username="inactive-agent",
            signature="直接登记不能复活。",
            conversation_id="十天过期群",
        )
    with pytest.raises(AuthenticationError, match="revoked"):
        store.register_agent_session_from_enrollment(
            enrollment_token=agent["enrollment_token"],
            product="codex",
            username="inactive-agent",
            signature="旧 enrollment 不能复活。",
        )

    restored = invite_agent(
        store,
        admin_id=admin_id,
        room="十天过期群",
        username="inactive-agent",
    )
    assert restored["participant_id"] == agent["participant_id"]
    assert restored["connector_id"] != agent["connector_id"]
    with store._connection() as connection:
        restored_state = connection.execute(
            "SELECT reinvite_required FROM agent_lifecycle_states "
            "WHERE participant_id = ?",
            (agent["participant_id"],),
        ).fetchone()
        restored_membership = connection.execute(
            "SELECT active FROM memberships WHERE conversation_id = ? "
            "AND participant_id = ?",
            ("十天过期群", agent["participant_id"]),
        ).fetchone()
    assert restored_state["reinvite_required"] == 0
    assert restored_membership["active"] == 1


def test_admin_kick_preserves_history_and_blocks_old_agent_credentials(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    admin_id = admin_web_user_id(store)
    store.create_user_room("踢人测试群")
    agent = invite_agent(
        store,
        admin_id=admin_id,
        room="踢人测试群",
        username="kick-target",
    )
    message = store.send(
        authorized_session_id=agent["session_id"],
        sender_participant_id=agent["participant_id"],
        conversation_id="踢人测试群",
        body_text="踢出后这条历史仍须保留。",
    )

    kicked = store.kick_agent_from_room(
        conversation_id="踢人测试群",
        participant_id=agent["participant_id"],
        kicked_by_web_user_id=admin_id,
    )
    assert kicked["history_preserved"] is True
    assert kicked["reinvite_required_for_room"] is True
    with pytest.raises(AuthenticationError):
        store.authenticate_session(agent["access_token"])
    with pytest.raises(AuthenticationError, match="revoked"):
        store.register_agent_session_from_enrollment(
            enrollment_token=agent["enrollment_token"],
            product="codex",
            username="kick-target",
            signature="旧 enrollment 已失效。",
        )
    with pytest.raises(ConflictError, match="new invitation"):
        store.register_agent_session(
            product="codex",
            username="kick-target",
            signature="直接登记不能返回。",
            conversation_id="踢人测试群",
        )
    with store._connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM messages WHERE message_id = ?",
            (message["message_id"],),
        ).fetchone()[0] == 1

    restored = invite_agent(
        store,
        admin_id=admin_id,
        room="踢人测试群",
        username="kick-target",
    )
    assert restored["participant_id"] == agent["participant_id"]


def test_admin_migrates_agents_from_multiple_rooms_atomically(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    admin_id = admin_web_user_id(store)
    for room in ("来源-A", "目标-B", "来源-C", "回滚-D"):
        store.create_user_room(room)
    from_a = invite_agent(
        store,
        admin_id=admin_id,
        room="来源-A",
        username="from-a",
    )
    from_c_one = invite_agent(
        store,
        admin_id=admin_id,
        room="来源-C",
        username="from-c-one",
    )
    from_c_two = invite_agent(
        store,
        admin_id=admin_id,
        room="来源-C",
        username="from-c-two",
    )

    migrated = store.migrate_agents(
        target_conversation_id="目标-B",
        selections=[
            {
                "source_conversation_id": "来源-A",
                "participant_ids": [from_a["participant_id"]],
            },
            {
                "source_conversation_id": "来源-C",
                "participant_ids": [
                    from_c_one["participant_id"],
                    from_c_two["participant_id"],
                ],
            },
        ],
        migrated_by_web_user_id=admin_id,
    )
    assert migrated["membership_count"] == 3
    assert migrated["copied_membership_count"] == 3
    assert migrated["agent_count"] == 3
    assert migrated["source_memberships_preserved"] is True
    assert migrated["sessions_rebound"] is False
    with store._connection() as connection:
        for registration, source in (
            (from_a, "来源-A"),
            (from_c_one, "来源-C"),
            (from_c_two, "来源-C"),
        ):
            source_membership = connection.execute(
                "SELECT active FROM memberships WHERE conversation_id = ? "
                "AND participant_id = ?",
                (source, registration["participant_id"]),
            ).fetchone()
            target_membership = connection.execute(
                "SELECT active FROM memberships WHERE conversation_id = ? "
                "AND participant_id = ?",
                ("目标-B", registration["participant_id"]),
            ).fetchone()
            session_room = connection.execute(
                "SELECT registered_conversation_id FROM agent_sessions "
                "WHERE session_id = ?",
                (registration["session_id"],),
            ).fetchone()[0]
            connector_room = connection.execute(
                "SELECT conversation_id FROM agent_connectors "
                "WHERE connector_id = ?",
                (registration["connector_id"],),
            ).fetchone()[0]
            assert source_membership["active"] == 1
            assert target_membership["active"] == 1
            assert session_room == source
            assert connector_room == source

    moved_message = store.send(
        authorized_session_id=from_a["session_id"],
        sender_participant_id=from_a["participant_id"],
        conversation_id="目标-B",
        body_text="复制加入后原会话可以直接在目标群发言。",
    )
    assert moved_message["conversation_id"] == "目标-B"
    source_message = store.send(
        authorized_session_id=from_a["session_id"],
        sender_participant_id=from_a["participant_id"],
        conversation_id="来源-A",
        body_text="原聊天室仍可继续发言。",
    )
    assert source_message["conversation_id"] == "来源-A"
    renewed = store.register_agent_session_from_enrollment(
        enrollment_token=from_c_one["enrollment_token"],
        product="codex",
        username="from-c-one",
        signature="复制加入后 enrollment 仍续到原登记群。",
    )
    assert renewed["conversation_id"] == "来源-C"

    with pytest.raises(ConflictError, match="not active"):
        store.migrate_agents(
            target_conversation_id="来源-C",
            selections=[
                {
                    "source_conversation_id": "目标-B",
                    "participant_ids": [from_a["participant_id"]],
                },
                {
                    "source_conversation_id": "回滚-D",
                    "participant_ids": [from_a["participant_id"]],
                },
            ],
            migrated_by_web_user_id=admin_id,
        )
    with store._connection() as connection:
        assert connection.execute(
            "SELECT active FROM memberships WHERE conversation_id = '目标-B' "
            "AND participant_id = ?",
            (from_a["participant_id"],),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM memberships WHERE conversation_id = '来源-C' "
            "AND participant_id = ? AND active = 1",
            (from_a["participant_id"],),
        ).fetchone()[0] == 0


def test_version_fifteen_connector_rooms_and_lifecycle_migrate_in_place(
    tmp_path: Path,
) -> None:
    database = tmp_path / "version-fifteen.db"
    store = BridgeStore(database)
    admin_id = admin_web_user_id(store)
    store.create_user_room("v15-room")
    agent = invite_agent(
        store,
        admin_id=admin_id,
        room="v15-room",
        username="v15-agent",
    )
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.executescript(
        """
        DROP TRIGGER IF EXISTS trg_agent_lifecycle_message_insert;
        DROP TABLE agent_room_blocks;
        DROP TABLE agent_lifecycle_states;
        DROP TABLE agent_lifecycle_policy;
        ALTER TABLE agent_connectors RENAME TO agent_connectors_v16;
        CREATE TABLE agent_connectors (
            connector_id TEXT PRIMARY KEY,
            invitation_id TEXT NOT NULL,
            accepted_participant_id TEXT NOT NULL,
            initial_session_id TEXT NOT NULL,
            enrollment_token_hash TEXT UNIQUE,
            enrollment_last_used_at REAL,
            setup_status TEXT NOT NULL DEFAULT 'awaiting_setup',
            setup_detail_json TEXT NOT NULL DEFAULT '{}',
            setup_updated_at REAL,
            connector_last_seen_at REAL,
            created_at REAL NOT NULL,
            revoked_at REAL,
            updated_at REAL NOT NULL
        );
        INSERT INTO agent_connectors
            (connector_id, invitation_id, accepted_participant_id,
             initial_session_id, enrollment_token_hash,
             enrollment_last_used_at, setup_status, setup_detail_json,
             setup_updated_at, connector_last_seen_at, created_at,
             revoked_at, updated_at)
        SELECT connector_id, invitation_id, accepted_participant_id,
               initial_session_id, enrollment_token_hash,
               enrollment_last_used_at, setup_status, setup_detail_json,
               setup_updated_at, connector_last_seen_at, created_at,
               revoked_at, updated_at
        FROM agent_connectors_v16;
        DROP TABLE agent_connectors_v16;
        PRAGMA user_version = 15;
        """
    )
    connection.commit()
    connection.close()

    migrated = BridgeStore(database)
    with migrated._connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 19
        assert connection.execute(
            "SELECT conversation_id FROM agent_connectors WHERE connector_id = ?",
            (agent["connector_id"],),
        ).fetchone()[0] == "v15-room"
        assert connection.execute(
            "SELECT access_granted_at FROM agent_lifecycle_states "
            "WHERE participant_id = ?",
            (agent["participant_id"],),
        ).fetchone()[0] > 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
