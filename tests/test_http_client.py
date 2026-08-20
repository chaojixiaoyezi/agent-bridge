from __future__ import annotations

import json
import base64
import hashlib
import stat
import threading
from http.client import RemoteDisconnected
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from typing import Any
from urllib.error import HTTPError
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


def test_attachment_download_is_hash_verified_and_atomic(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"private attachment content"
    encoded_filename = base64.urlsafe_b64encode("../证据.txt".encode()).decode()

    class Response:
        def __init__(self) -> None:
            self.offset = 0
            self.headers = {
                "X-Attachment-Filename-B64": encoded_filename,
                "X-Attachment-Size": str(len(content)),
                "X-Attachment-SHA256": hashlib.sha256(content).hexdigest(),
                "X-Attachment-Kind": "file",
                "X-Attachment-Media-Type": "text/plain",
            }

        def read(self, limit: int) -> bytes:
            chunk = content[self.offset : self.offset + limit]
            self.offset += len(chunk)
            return chunk

        def close(self) -> None:
            return None

    captured: dict[str, str] = {}

    def open_request(request: Request, *, timeout: float):
        assert timeout == 30
        captured.update(dict(request.header_items()))
        return Response()

    monkeypatch.setattr("agent_bridge.http_client.urlopen", open_request)
    client = BridgeHttpClient("https://bridge.example.test")
    client.access_token = "session-private"
    result = client.download_attachment(
        attachment_id="attachment_private",
        destination_path=tmp_path,
    )

    destination = tmp_path / "证据.txt"
    assert destination.read_bytes() == content
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert result["saved_path"] == str(destination)
    assert result["sha256"] == hashlib.sha256(content).hexdigest()
    assert captured["Authorization"] == "Bearer session-private"
    assert not list(tmp_path.glob("*.part"))


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


def test_enrollment_rotation_reuses_private_pending_successor_after_lost_response(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enrollment_file = tmp_path / "enrollment.token"
    old_enrollment = "enroll_" + "o" * 64
    enrollment_file.write_text(f"{old_enrollment}\n", encoding="utf-8")
    enrollment_file.chmod(0o600)
    client = BridgeHttpClient(
        "https://bridge.example.test",
        enrollment_token=old_enrollment,
        connector_id="connector_rotation",
        enrollment_token_file=enrollment_file,
        enrollment_token_loader=lambda: enrollment_file.read_text(
            encoding="utf-8"
        ).strip(),
    )
    submitted_successors: list[str] = []

    def post(
        path: str,
        payload: dict[str, Any],
        *,
        authenticated: bool,
    ) -> dict[str, Any]:
        assert path == "/agent/connector/enrollment/rotate"
        assert authenticated is False
        submitted_successors.append(str(payload["new_enrollment_token"]))
        if len(submitted_successors) == 1:
            raise BridgeRemoteError("response lost")
        return {
            "connector": {
                "connector_id": "connector_rotation",
                "rotation_completed": True,
                "enrollment": {"credential_version": 2},
            }
        }

    monkeypatch.setattr(client, "_post", post)
    with pytest.raises(BridgeRemoteError, match="response lost"):
        client.rotate_enrollment()
    pending_file = tmp_path / ".enrollment.token.pending"
    assert pending_file.exists()
    assert enrollment_file.read_text(encoding="utf-8").strip() == old_enrollment
    assert stat.S_IMODE(pending_file.stat().st_mode) == 0o600

    result = client.rotate_enrollment()
    assert len(submitted_successors) == 2
    assert submitted_successors[0] == submitted_successors[1]
    assert submitted_successors[0].startswith("enroll_")
    assert enrollment_file.read_text(encoding="utf-8").strip() == (
        submitted_successors[0]
    )
    assert stat.S_IMODE(enrollment_file.stat().st_mode) == 0o600
    assert not pending_file.exists()
    assert submitted_successors[0] not in str(result)
    assert result == {
        "connector_id": "connector_rotation",
        "credential_version": 2,
        "rotation_completed": True,
    }


def test_registration_automatically_rotates_requested_file_credential(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enrollment_file = tmp_path / "enrollment.token"
    enrollment_file.write_text("enroll_" + "x" * 64 + "\n", encoding="utf-8")
    enrollment_file.chmod(0o600)
    client = BridgeHttpClient(
        "https://bridge.example.test",
        enrollment_token="enroll_" + "x" * 64,
        connector_id="connector_auto_rotation",
        enrollment_token_file=enrollment_file,
        enrollment_token_loader=lambda: enrollment_file.read_text(
            encoding="utf-8"
        ).strip(),
    )
    calls: list[str] = []

    def post(
        path: str,
        payload: dict[str, Any],
        *,
        authenticated: bool,
    ) -> dict[str, Any]:
        calls.append(path)
        if path == "/agent/register":
            assert authenticated is False
            return {
                "access_token": "session_rotating",
                "participant_id": "participant_rotating",
                "session_id": "session_rotating_id",
                "enrollment_rotation_required": True,
                "enrollment_credential_version": 1,
            }
        successor = str(payload["new_enrollment_token"])
        return {
            "connector": {
                "connector_id": "connector_auto_rotation",
                "rotation_completed": True,
                "enrollment": {"credential_version": 2},
                "successor_is_not_returned": successor != "",
            }
        }

    monkeypatch.setattr(client, "_post", post)
    registered = client.register(**FIXED_IDENTITY)
    assert calls == ["/agent/register", "/agent/connector/enrollment/rotate"]
    assert registered["enrollment_rotation_required"] is False
    assert registered["enrollment_rotation_pending"] is False
    assert registered["enrollment_credential_version"] == 2
    assert enrollment_file.read_text(encoding="utf-8").strip() != (
        "enroll_" + "x" * 64
    )


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


def test_http_status_survives_a_reset_while_reading_an_empty_error_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ResetBody:
        @staticmethod
        def read(_limit: int) -> bytes:
            raise ConnectionResetError("peer closed empty error body")

        @staticmethod
        def close() -> None:
            pass

    def reject(_request: Request, *, timeout: float):
        del timeout
        raise HTTPError(
            "http://127.0.0.1/agent/wait",
            302,
            "Found",
            {},
            ResetBody(),
        )

    monkeypatch.setattr("agent_bridge.http_client.urlopen", reject)
    client = BridgeHttpClient("http://127.0.0.1")
    client.access_token = "session-private"

    with pytest.raises(BridgeRemoteError, match="HTTP 302") as captured:
        client.post("/agent/wait", {"wait_seconds": 0})

    assert captured.value.status_code == 302


def test_structured_bridge_error_code_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject(_request: Request, *, timeout: float):
        del timeout
        body = BytesIO(
            json.dumps(
                {
                    "error": "native TUI lease expired; bind the session again",
                    "error_code": "native_session_lease_expired",
                }
            ).encode("utf-8")
        )
        raise HTTPError(
            "https://bridge.example.test/agent/native/channel/wait",
            409,
            "Conflict",
            {},
            body,
        )

    monkeypatch.setattr("agent_bridge.http_client.urlopen", reject)
    client = BridgeHttpClient("https://bridge.example.test")
    client.access_token = "session-private"

    with pytest.raises(BridgeRemoteError) as captured:
        client.post("/agent/native/channel/wait", {"wait_seconds": 0})

    assert captured.value.status_code == 409
    assert captured.value.error_code == "native_session_lease_expired"
