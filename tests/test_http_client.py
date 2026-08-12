from __future__ import annotations

from typing import Any

import pytest

from agent_bridge.http_client import BridgeHttpClient, BridgeRemoteError


FIXED_IDENTITY: dict[str, Any] = {
    "product": "claude-code",
    "username": "resident",
    "signature": "fixed connector identity",
    "conversation_id": "room",
    "roles": ["reviewer"],
    "capabilities": ["history"],
}


def test_resident_client_registers_before_first_call_and_renews_once_on_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = BridgeHttpClient(
        "https://bridge.example.test",
        enrollment_token="enroll_private",
        auto_registration=FIXED_IDENTITY,
    )
    registrations: list[dict[str, Any]] = []
    authenticated_tokens: list[str | None] = []

    def register(**identity: Any) -> dict[str, Any]:
        registrations.append(identity)
        client.access_token = f"session-{len(registrations)}"
        return {"participant_id": "participant-resident"}

    def post(
        path: str,
        payload: dict[str, Any],
        *,
        authenticated: bool,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        assert path == "/agent/wait"
        assert payload == {"wait_seconds": 0}
        assert authenticated is True
        assert timeout == 17
        authenticated_tokens.append(client.access_token)
        if len(authenticated_tokens) == 1:
            raise BridgeRemoteError("expired", status_code=401)
        return {"messages": []}

    monkeypatch.setattr(client, "register", register)
    monkeypatch.setattr(client, "_post", post)

    assert client.post(
        "/agent/wait",
        {"wait_seconds": 0},
        timeout=17,
    ) == {"messages": []}
    assert registrations == [FIXED_IDENTITY, FIXED_IDENTITY]
    assert authenticated_tokens == ["session-1", "session-2"]


def test_resident_client_does_not_loop_when_retried_call_is_still_unauthorized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = BridgeHttpClient(
        "https://bridge.example.test",
        auto_registration=FIXED_IDENTITY,
    )
    registration_count = 0
    post_count = 0

    def register(**identity: Any) -> dict[str, Any]:
        nonlocal registration_count
        assert identity == FIXED_IDENTITY
        registration_count += 1
        client.access_token = f"session-{registration_count}"
        return {}

    def post(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal post_count
        post_count += 1
        raise BridgeRemoteError("still unauthorized", status_code=401)

    monkeypatch.setattr(client, "register", register)
    monkeypatch.setattr(client, "_post", post)

    with pytest.raises(BridgeRemoteError, match="still unauthorized"):
        client.post("/agent/wait", {"wait_seconds": 0})
    assert registration_count == 2
    assert post_count == 2
