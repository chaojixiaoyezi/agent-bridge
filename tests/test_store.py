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
    BridgeStore,
    ConflictError,
    NicknameRateLimitError,
    NotFoundError,
    RateLimitError,
)
from agent_bridge.validation import ValidationError, product_username
from agent_bridge.viewer_store import ViewerRepository


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
        "mention",
    ]
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
    assert follower_direct["delivery"]["priority"] == "important"
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
    assert first_batch["count"] == 100
    assert first_batch["pending_count"] == 240
    assert first_batch["has_more"] is True
    assert first_batch["backlog"]["priority_counts"] == {
        "mention": 0,
        "important": 0,
        "normal": 240,
    }
    assert first_batch["messages"][0]["body"] == "历史消息 0"
    assert first_batch["messages"][-1]["body"] == "历史消息 99"

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
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    assert after_counts == before_counts
    assert version == 9
    assert len(resolved_deliveries) == 2
    assert {row["state"] for row in resolved_deliveries} == {"acked"}
    assert {int(row["actionable"]) for row in resolved_deliveries} == {0}

    observer_history = migrated.history(
        participant_id=observer["participant_id"],
        authorized_session_id=observer["session_id"],
        conversation_id="tools-room",
    )
    assert [item["message_id"] for item in observer_history["messages"]] == [
        resolved["message_id"],
        unresolved["message_id"],
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
    assert version == 9


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
