from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import agent_bridge.tui_wake as tui_wake
from agent_bridge.http_client import BridgeRemoteError
from agent_bridge.tui_adapter import validate_native_tui_binding


def _message(message_id: str, *, required: bool) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "sequence": int(message_id.removeprefix("msg-")),
        "body": f"消息 {message_id}",
        "delivery": {"reasons": ["mention"] if required else ["normal"]},
    }


def _configure_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    values = {
        "AGENT_BRIDGE_URL": "http://127.0.0.1:8765",
        "AGENT_BRIDGE_PRODUCT": "opencode",
        "AGENT_BRIDGE_USERNAME": "native-owner",
        "AGENT_BRIDGE_SIGNATURE": "真实本体",
        "AGENT_BRIDGE_CONVERSATION_ID": "原生群",
        "AGENT_BRIDGE_CONNECTOR_ID": "connector-native-one",
        "AGENT_BRIDGE_TUI_BINDING_FILE": str(tmp_path / "binding.json"),
        "AGENT_BRIDGE_TUI_LOCK_FILE": str(tmp_path / "turn.lock"),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


class FakeBridgeClient:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.pending = {str(item["message_id"]): item for item in messages}
        self.replies: list[dict[str, Any]] = []
        self.states: list[dict[str, Any]] = []
        self.delivery_stages: list[dict[str, Any]] = []
        self.wait_payloads: list[dict[str, Any]] = []

    def post(
        self, path: str, payload: dict[str, Any], **_kwargs: Any
    ) -> dict[str, Any]:
        if path == "/agent/wait":
            self.wait_payloads.append(payload)
            result: dict[str, Any] = {
                "messages": list(self.pending.values())[:20],
                "has_more": False,
            }
            if payload.get("compact_optional_backlog"):
                result["offline_compaction"] = {
                    "applied": True,
                    "compacted_optional_count": 42,
                    "history_preserved": True,
                }
            return result
        if path == "/agent/reply":
            self.replies.append(payload)
            self.pending.pop(str(payload["message_id"]), None)
            return {"ok": True}
        if path == "/agent/connector/tui-state":
            self.states.append(dict(payload))
            return {"connector": payload}
        if path == "/agent/connector/tui-delivery-stage":
            self.delivery_stages.append(dict(payload))
            return {"count": len(payload["message_ids"])}
        raise AssertionError(path)


def test_native_wake_replies_to_each_required_message_without_acking_it_early(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_environment(monkeypatch, tmp_path)
    binding = validate_native_tui_binding(
        adapter_kind="opencode",
        endpoint_id="endpoint-opencode",
        native_session_id="session-room-one",
        transport={
            "kind": "opencode-http",
            "base_url": "http://127.0.0.1:9201",
        },
    )
    bridge = FakeBridgeClient(
        [
            _message("msg-1", required=True),
            _message("msg-2", required=True),
            _message("msg-3", required=False),
        ]
    )
    prompts: list[str] = []
    resident_identity: dict[str, Any] = {}

    class FakeNative:
        def __init__(self, _binding: Any) -> None:
            pass

        def run_turn(self, prompt: str) -> tuple[str, list[str]]:
            prompts.append(prompt)
            return f"回复第 {len(prompts)} 条", []

    monkeypatch.setattr(tui_wake, "load_native_tui_binding", lambda _path: binding)

    def make_bridge(**identity: Any) -> FakeBridgeClient:
        resident_identity.update(identity)
        return bridge

    monkeypatch.setattr(tui_wake, "resident_http_client", make_bridge)
    monkeypatch.setattr(tui_wake, "NativeTuiClient", FakeNative)

    def acknowledge(_client: Any, message_ids: set[str]) -> None:
        for message_id in message_ids:
            bridge.pending.pop(message_id, None)

    monkeypatch.setattr(tui_wake, "acknowledge_messages", acknowledge)

    tui_wake.run_native_wake({"event_count": 2})

    assert [item["message_id"] for item in bridge.replies] == ["msg-1", "msg-2"]
    assert resident_identity["connector_component"] == "mcp"
    assert bridge.pending == {}
    assert '"message_id":"msg-2"' not in prompts[0]
    assert '"message_id":"msg-3"' in prompts[0]
    assert [item["state"] for item in bridge.states] == ["busy", "online"]
    assert [item["stage"] for item in bridge.delivery_stages] == [
        "injected",
        "applied",
        "injected",
        "applied",
    ]
    assert bridge.delivery_stages[0]["message_ids"] == ["msg-1", "msg-3"]
    assert bridge.delivery_stages[2]["message_ids"] == ["msg-2"]
    assert all("access_mode" not in item for item in bridge.states)


def test_native_wake_bounds_only_an_explicit_reconnect_backlog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_environment(monkeypatch, tmp_path)
    binding = validate_native_tui_binding(
        adapter_kind="opencode",
        endpoint_id="endpoint-opencode",
        native_session_id="session-room-one",
        transport={
            "kind": "opencode-http",
            "base_url": "http://127.0.0.1:9201",
        },
    )
    bridge = FakeBridgeClient([_message("msg-4", required=False)])
    prompts: list[str] = []

    class QuietNative:
        def __init__(self, _binding: Any) -> None:
            pass

        def run_turn(self, prompt: str) -> tuple[str, list[str]]:
            prompts.append(prompt)
            return tui_wake.SILENT_MARKER, []

    monkeypatch.setattr(tui_wake, "load_native_tui_binding", lambda _path: binding)
    monkeypatch.setattr(tui_wake, "resident_http_client", lambda **_kwargs: bridge)
    monkeypatch.setattr(tui_wake, "NativeTuiClient", QuietNative)

    def acknowledge(_client: Any, message_ids: set[str]) -> None:
        for message_id in message_ids:
            bridge.pending.pop(message_id, None)

    monkeypatch.setattr(tui_wake, "acknowledge_messages", acknowledge)

    tui_wake.run_native_wake(
        {"event_count": 1, "contains_backlog_event": True}
    )

    assert bridge.wait_payloads[0] == {
        "wait_seconds": 0,
        "limit": 20,
        "auto_claim_roles": True,
        "compact_optional_backlog": True,
        "keep_recent_optional": 20,
    }
    assert all(
        "compact_optional_backlog" not in payload
        for payload in bridge.wait_payloads[1:]
    )
    assert '"compacted_optional_count":42' in prompts[0]
    assert [item["stage"] for item in bridge.delivery_stages] == [
        "injected",
        "applied",
    ]


def test_native_wake_preserves_required_message_when_tui_is_silent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_environment(monkeypatch, tmp_path)
    binding = validate_native_tui_binding(
        adapter_kind="opencode",
        endpoint_id="endpoint-opencode",
        native_session_id="session-room-one",
        transport={
            "kind": "opencode-http",
            "base_url": "http://127.0.0.1:9201",
        },
    )
    bridge = FakeBridgeClient([_message("msg-1", required=True)])

    class SilentNative:
        def __init__(self, _binding: Any) -> None:
            pass

        def run_turn(self, _prompt: str) -> tuple[str, list[str]]:
            return tui_wake.SILENT_MARKER, []

    monkeypatch.setattr(tui_wake, "load_native_tui_binding", lambda _path: binding)
    monkeypatch.setattr(tui_wake, "resident_http_client", lambda **_kwargs: bridge)
    monkeypatch.setattr(tui_wake, "NativeTuiClient", SilentNative)
    monkeypatch.setattr(
        tui_wake,
        "acknowledge_messages",
        lambda _client, message_ids: [
            bridge.pending.pop(item, None) for item in message_ids
        ],
    )

    with pytest.raises(tui_wake.NativeTuiWakeError, match="omitted required reply"):
        tui_wake.run_native_wake({"event_count": 1})

    assert set(bridge.pending) == {"msg-1"}
    assert bridge.replies == []
    assert [item["stage"] for item in bridge.delivery_stages] == [
        "injected",
        "applied",
    ]
    assert [item["state"] for item in bridge.states] == ["busy", "error"]
    assert all("access_mode" not in item for item in bridge.states)


def test_native_wake_retries_short_reply_rate_limit_without_rerunning_tui(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_environment(monkeypatch, tmp_path)
    binding = validate_native_tui_binding(
        adapter_kind="opencode",
        endpoint_id="endpoint-opencode",
        native_session_id="session-room-one",
        transport={
            "kind": "opencode-http",
            "base_url": "http://127.0.0.1:9201",
        },
    )

    class RateLimitedBridge(FakeBridgeClient):
        def __init__(self) -> None:
            super().__init__([_message("msg-1", required=True)])
            self.reply_attempts = 0

        def post(
            self, path: str, payload: dict[str, Any], **kwargs: Any
        ) -> dict[str, Any]:
            if path == "/agent/reply":
                self.reply_attempts += 1
                if self.reply_attempts == 1:
                    raise BridgeRemoteError(
                        "rate limited",
                        status_code=429,
                        retry_after_seconds=5.5,
                    )
            return super().post(path, payload, **kwargs)

    bridge = RateLimitedBridge()
    native_turns: list[str] = []

    class FakeNative:
        def __init__(self, _binding: Any) -> None:
            pass

        def run_turn(self, prompt: str) -> tuple[str, list[str]]:
            native_turns.append(prompt)
            return "只生成一次", []

    monkeypatch.setattr(tui_wake, "load_native_tui_binding", lambda _path: binding)
    monkeypatch.setattr(tui_wake, "resident_http_client", lambda **_kwargs: bridge)
    monkeypatch.setattr(tui_wake, "NativeTuiClient", FakeNative)
    monkeypatch.setattr(tui_wake.time, "sleep", lambda seconds: sleeps.append(seconds))
    sleeps: list[float] = []

    tui_wake.run_native_wake({"event_count": 1})

    assert bridge.reply_attempts == 2
    assert len(native_turns) == 1
    assert bridge.replies == [{"message_id": "msg-1", "body": "只生成一次"}]
    assert sleeps == [5.6]
