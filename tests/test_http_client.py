from __future__ import annotations

import json
from typing import Any
from urllib.request import Request

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


def test_enrollment_registration_sends_connector_identity_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def read(self) -> bytes:
            return (
                b'{"access_token":"session_private",'
                b'"participant_id":"participant_private",'
                b'"session_id":"session_private_id"}'
            )

    def open_request(request: Request, *, timeout: float):
        del timeout
        captured.update(dict(request.header_items()))
        return Response()

    monkeypatch.setattr("agent_bridge.http_client.urlopen", open_request)
    client = BridgeHttpClient(
        "https://bridge.example.test",
        enrollment_token="enroll_private",
        connector_id="connector_private",
    )
    client.register(**FIXED_IDENTITY)

    assert captured["X-agent-bridge-enrollment"] == "enroll_private"
    assert captured["X-agent-bridge-connector"] == "connector_private"


def test_invitation_acceptance_declares_strict_connector_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def read(self) -> bytes:
            enrollment = captured["payload"]["enrollment_token"]
            return json.dumps(
                {
                    "access_token": "session_private",
                    "participant_id": "participant_private",
                    "session_id": "session_private_id",
                    "connector_id": "connector_private",
                    "enrollment_token": enrollment,
                }
            ).encode("utf-8")

    def open_request(request: Request, *, timeout: float):
        del timeout
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return Response()

    monkeypatch.setattr("agent_bridge.http_client.urlopen", open_request)
    client = BridgeHttpClient(
        "https://bridge.example.test",
        invitation_token="invite_private",
    )
    accepted = client.accept_invitation(
        product="claude-code",
        username="worker",
        signature="严格连接器",
    )

    assert captured["payload"]["connector_binding_version"] == 2
    assert accepted["connector_id"] == "connector_private"
    assert client.connector_id == "connector_private"
