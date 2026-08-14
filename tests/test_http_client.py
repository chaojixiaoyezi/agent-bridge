from __future__ import annotations

import json
import threading
from http.client import RemoteDisconnected
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


@pytest.mark.parametrize(
    "failure",
    [RemoteDisconnected("rolling restart"), TimeoutError("long poll timed out")],
)
def test_transport_disconnects_become_retryable_bridge_errors(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    def open_request(request: Request, *, timeout: float):
        del request, timeout
        raise failure

    monkeypatch.setattr("agent_bridge.http_client.urlopen", open_request)
    client = BridgeHttpClient("https://bridge.example.test")
    client.access_token = "session-private"

    with pytest.raises(BridgeRemoteError, match="cannot reach Agent Bridge") as caught:
        client.post("/agent/tasks/next", {"wait_seconds": 20}, timeout=30)

    assert caught.value.status_code is None


def test_enrollment_registration_sends_connector_identity_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def read(self, _limit: int | None = None) -> bytes:
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

        def read(self, _limit: int | None = None) -> bytes:
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


def test_bridge_client_does_not_forward_session_tokens_through_redirects() -> None:
    target_headers: list[dict[str, str]] = []

    class TargetHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            target_headers.append(dict(self.headers.items()))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args: Any) -> None:
            pass

    target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.send_response(302)
            self.send_header(
                "Location",
                f"http://127.0.0.1:{target.server_port}/escaped",
            )
            self.end_headers()

        def log_message(self, *_args: Any) -> None:
            pass

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (target, redirect)
    ]
    for thread in threads:
        thread.start()
    try:
        client = BridgeHttpClient(f"http://127.0.0.1:{redirect.server_port}")
        client.access_token = "session-private"
        with pytest.raises(BridgeRemoteError, match="HTTP 302"):
            client.post("/agent/wait", {"wait_seconds": 0})
        assert target_headers == []
    finally:
        redirect.shutdown()
        target.shutdown()
        redirect.server_close()
        target.server_close()
