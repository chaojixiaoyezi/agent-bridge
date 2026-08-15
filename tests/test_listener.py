from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.request import urlopen

import pytest

import agent_bridge.listener as listener
from agent_bridge.listener import (
    ListenerError,
    Registration,
    _iter_sse_events,
    _parse_json_argv,
    _read_cursor,
    _register,
    _run_wake_command,
    _validated_base_url,
    _validated_webhook,
    _wake_envelope,
    _write_cursor,
)
from agent_bridge.store import BridgeStore
from agent_bridge.supervisor import queue_status


BRIDGE_ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def bridge_server(database: Path):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    environment = dict(os.environ)
    environment.update(
        {
            "AGENT_BRIDGE_DB": str(database),
            "AGENT_BRIDGE_VIEWER_HOST": "127.0.0.1",
            "AGENT_BRIDGE_VIEWER_PORT": str(port),
        }
    )
    process = subprocess.Popen(
        [str(BRIDGE_ROOT / "bin" / "agent-bridge-viewer")],
        cwd=str(BRIDGE_ROOT),
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 8
        while True:
            if process.poll() is not None:
                raise RuntimeError(process.stderr.read())
            try:
                with urlopen(f"{url}/api/health", timeout=0.25) as response:
                    if response.status == 200:
                        break
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("Agent Bridge test server did not start")
            time.sleep(0.05)
        yield url
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_sse_parser_keeps_only_structured_metadata() -> None:
    events = list(
        _iter_sse_events(
            [
                b": keepalive\n",
                b"\n",
                b"id: 42\n",
                b"event: message_available\n",
                b'data: {"pending_count":3,"priority":"mention"}\n',
                b"\n",
            ]
        )
    )
    assert events == [
        {
            "event": "message_available",
            "id": 42,
            "data": {"pending_count": 3, "priority": "mention"},
        }
    ]


def test_remote_listener_requires_tls_and_loopback_wake_webhook() -> None:
    assert (
        _validated_base_url(
            "http://127.0.0.1:8765/",
            allow_insecure_http=False,
        )
        == "http://127.0.0.1:8765"
    )
    assert (
        _validated_base_url(
            "https://bridge.example.test",
            allow_insecure_http=False,
        )
        == "https://bridge.example.test"
    )
    with pytest.raises(ListenerError, match="bearer token"):
        _validated_base_url(
            "http://192.168.1.20:8765",
            allow_insecure_http=False,
        )
    assert _validated_webhook("http://localhost:9988/wake") is not None
    with pytest.raises(ListenerError, match="loopback"):
        _validated_webhook("https://remote.example.test/wake")


def test_cursor_file_contains_only_sequence(tmp_path: Path) -> None:
    cursor_file = tmp_path / "listener.cursor"
    _write_cursor(cursor_file, 912)
    assert cursor_file.read_text(encoding="utf-8") == "912\n"
    assert _read_cursor(cursor_file) == 912


def notification_event(
    *,
    event: str = "message_available",
    cursor: int = 42,
    mention: int = 1,
) -> dict:
    manifest = {
        "activity_count": 1,
        "pending_count": 1,
        "priority_counts": {
            "normal": 0,
            "important": 0,
            "mention": mention,
        },
    }
    return {
        "event": event,
        "id": cursor,
        "data": {
            "participant_id": "participant_listener",
            "cursor": cursor,
            "has_new": True,
            "has_room_activity": True,
            "backlog": {
                "pending_count": 1,
                "priority_counts": manifest["priority_counts"],
            },
            "new_since_cursor": {
                "pending_count": 1,
                "priority_counts": manifest["priority_counts"],
            },
            "room_activity_since_cursor": manifest,
            "server_time": 123.0,
        },
    }


def test_wake_envelope_is_metadata_only_and_policy_aware() -> None:
    event = notification_event()
    envelope = _wake_envelope(event, wake_policy="mention")
    assert envelope is not None
    assert envelope["wake_priority"] == "mention"
    assert envelope["cursor"] == 42
    assert "body" not in str(envelope).lower()

    ordinary = notification_event(mention=0)
    assert _wake_envelope(ordinary, wake_policy="mention") is None
    assert _wake_envelope(ordinary, wake_policy="all") is not None


def test_wake_command_is_argv_only_and_strips_session_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _parse_json_argv('["/usr/bin/true","--accept"]') == (
        "/usr/bin/true",
        "--accept",
    )
    with pytest.raises(ListenerError, match="JSON argv"):
        _parse_json_argv("/usr/bin/true; touch /tmp/no")

    monkeypatch.setenv("AGENT_BRIDGE_TOKEN", "never-forward")
    monkeypatch.setenv("AGENT_TOKEN", "never-forward-either")
    monkeypatch.setenv("AGENT_BRIDGE_REGISTRATION_SECRET", "never-forward-secret")
    monkeypatch.setenv("AGENT_BRIDGE_INVITATION_TOKEN", "never-forward-invite")
    monkeypatch.setenv("AGENT_BRIDGE_ENROLLMENT_TOKEN", "never-forward-enrollment")
    monkeypatch.setenv("AGENT_BRIDGE_DB", "/private/bridge.db")
    monkeypatch.setenv("AGENT_BRIDGE_HOME", "/private/bridge-home")
    captured: dict = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(listener.subprocess, "run", fake_run)
    _run_wake_command(("/usr/bin/true", "--accept"), b'{"cursor":42}', timeout=3)

    assert captured["command"] == ["/usr/bin/true", "--accept"]
    assert captured["shell"] is False
    assert captured["input"] == b'{"cursor":42}\n'
    assert "AGENT_BRIDGE_TOKEN" not in captured["env"]
    assert "AGENT_TOKEN" not in captured["env"]
    assert "AGENT_BRIDGE_REGISTRATION_SECRET" not in captured["env"]
    assert "AGENT_BRIDGE_INVITATION_TOKEN" not in captured["env"]
    assert "AGENT_BRIDGE_ENROLLMENT_TOKEN" not in captured["env"]
    assert "AGENT_BRIDGE_DB" not in captured["env"]
    assert "AGENT_BRIDGE_HOME" not in captured["env"]


def test_auto_registration_sends_optional_authority_header_without_persisting_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b'{"access_token":"session-memory-only"}'

    def fake_urlopen(request, **kwargs):
        captured["request"] = request
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(listener, "urlopen", fake_urlopen)
    token = _register(
        base_url="https://bridge.example.test",
        registration=Registration(
            product="codex",
            username="listener",
            signature="event driven",
            conversation_id="tools-room",
        ),
        registration_secret="registration-authority",
    )

    assert token == "session-memory-only"
    request = captured["request"]
    assert request.get_header("X-agent-bridge-registration") == (
        "registration-authority"
    )
    assert b"session-memory-only" not in request.data

    _register(
        base_url="https://bridge.example.test",
        registration=Registration(
            product="codex",
            username="listener",
            signature="event driven",
            conversation_id="tools-room",
        ),
        registration_secret="must-not-win",
        enrollment_token="enroll_connector-authority",
        connector_id="connector_authority",
    )
    request = captured["request"]
    assert request.get_header("X-agent-bridge-enrollment") == (
        "enroll_connector-authority"
    )
    assert request.get_header("X-agent-bridge-connector") == "connector_authority"
    assert request.get_header("X-agent-bridge-registration") is None


def test_listener_rotates_requested_credential_without_losing_registered_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enrollment_file = tmp_path / "enrollment.token"
    enrollment = "enroll_" + "l" * 64
    enrollment_file.write_text(f"{enrollment}\n", encoding="utf-8")
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, base_url: str, **kwargs) -> None:
            captured["base_url"] = base_url
            captured.update(kwargs)

        def rotate_enrollment(self) -> dict[str, object]:
            captured["rotated"] = True
            return {
                "connector_id": "connector_listener_rotation",
                "credential_version": 2,
                "rotation_completed": True,
            }

    monkeypatch.setattr(listener, "BridgeHttpClient", FakeClient)

    def loader() -> str:
        return enrollment_file.read_text(encoding="utf-8").strip()

    assert listener._rotate_enrollment_if_requested(
        registration_result={"enrollment_rotation_required": True},
        base_url="https://bridge.example.test",
        enrollment_token=enrollment,
        connector_id="connector_listener_rotation",
        enrollment_token_file=enrollment_file,
        enrollment_token_loader=loader,
    ) is True
    assert captured["base_url"] == "https://bridge.example.test"
    assert captured["connector_id"] == "connector_listener_rotation"
    assert captured["enrollment_token_file"] == enrollment_file
    assert captured["enrollment_token_loader"] is loader
    assert captured["rotated"] is True


def test_listener_retries_failed_sink_and_advances_cursor_only_after_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor_file = tmp_path / "listener.cursor"
    event = notification_event(cursor=77)
    wire = (
        "id: 77\n"
        "event: message_available\n"
        f"data: {listener.json.dumps(event['data'], separators=(',', ':'))}\n\n"
    ).encode()
    opened: list[object] = []
    sink_attempts: list[bytes] = []

    class Headers:
        @staticmethod
        def get_content_type():
            return "text/event-stream"

    class Response:
        headers = Headers()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            return iter(wire.splitlines(keepends=True))

    def fake_urlopen(request, **_kwargs):
        opened.append(request)
        return Response()

    def flaky_webhook(_webhook, encoded, *, timeout):
        sink_attempts.append(encoded)
        assert timeout == 2
        if len(sink_attempts) == 1:
            raise ListenerError("supervisor queue unavailable")

    monkeypatch.setattr(listener, "urlopen", fake_urlopen)
    monkeypatch.setattr(listener, "_post_webhook", flaky_webhook)
    monkeypatch.setattr(listener.time, "sleep", lambda _seconds: None)

    listener.listen(
        base_url="https://bridge.example.test",
        access_token="memory-token",
        registration=None,
        registration_secret=None,
        webhook="http://127.0.0.1:9000/wake",
        command=None,
        wake_policy="all",
        wake_timeout=2,
        cursor_file=cursor_file,
        once=True,
    )

    assert len(opened) == 2
    assert len(sink_attempts) == 2
    assert sink_attempts[0] == sink_attempts[1]
    assert _read_cursor(cursor_file) == 77


def test_remote_listener_auto_registers_and_delivers_existing_backlog_to_supervisor(
    tmp_path: Path,
) -> None:
    database = tmp_path / "bridge.db"
    store = BridgeStore(database)
    store.create_user_room("跨机器通知群")
    receiver = store.register_agent_session(
        product="codex",
        username="远端监听者",
        signature="事件驱动，不轮询模型。",
        conversation_id="跨机器通知群",
    )
    sender = store.register_agent_session(
        product="claude-code",
        username="远端发送者",
        signature="只发送结构化提醒。",
        conversation_id="跨机器通知群",
    )
    sent = store.send(
        authorized_session_id=sender["session_id"],
        sender_participant_id=sender["participant_id"],
        conversation_id="跨机器通知群",
        body_text="这段正文绝不能进入 wake envelope。",
        audience_kind="room",
        mentions=[receiver["participant_id"]],
    )
    wake_file = tmp_path / "wake.json"
    cursor_file = tmp_path / "listener.cursor"
    writer = [
        str(BRIDGE_ROOT / ".venv" / "bin" / "python"),
        "-c",
        (
            "import pathlib,sys; "
            "pathlib.Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())"
        ),
        str(wake_file),
    ]

    with bridge_server(database) as server_url:
        environment = dict(os.environ)
        for name in (
            "AGENT_BRIDGE_TOKEN",
            "AGENT_TOKEN",
            "AGENT_BRIDGE_REGISTRATION_SECRET",
        ):
            environment.pop(name, None)
        environment.update(
            {
                "AGENT_BRIDGE_URL": server_url,
                "AGENT_BRIDGE_PRODUCT": "codex",
                "AGENT_BRIDGE_USERNAME": "远端监听者",
                "AGENT_BRIDGE_SIGNATURE": "事件驱动，不轮询模型。",
                "AGENT_BRIDGE_CONVERSATION_ID": "跨机器通知群",
                "AGENT_BRIDGE_CURSOR_FILE": str(cursor_file),
                "AGENT_BRIDGE_WAKE_COMMAND_JSON": json.dumps(writer),
                "AGENT_BRIDGE_WAKE_POLICY": "mention",
            }
        )
        completed = subprocess.run(
            [str(BRIDGE_ROOT / "bin" / "agent-bridge-listen"), "--once"],
            cwd=str(BRIDGE_ROOT),
            env=environment,
            capture_output=True,
            text=True,
            timeout=12,
            check=False,
        )

    assert completed.returncode == 0, completed.stderr
    wake = json.loads(wake_file.read_text(encoding="utf-8"))
    assert wake["participant_id"] == receiver["participant_id"]
    assert wake["wake_priority"] == "mention"
    assert wake["backlog"]["pending_count"] == 1
    assert wake["event_id"] == sent["sequence"]
    assert "这段正文" not in wake_file.read_text(encoding="utf-8")
    assert _read_cursor(cursor_file) == sent["sequence"]


def test_remote_listener_and_builtin_supervisor_survive_an_offline_backlog(
    tmp_path: Path,
) -> None:
    database = tmp_path / "bridge.db"
    store = BridgeStore(database)
    store.create_user_room("离线重放群")
    receiver = store.register_agent_session(
        product="codex",
        username="离线接收者",
        signature="重连后先收元数据。",
        conversation_id="离线重放群",
    )
    sender = store.register_agent_session(
        product="my-agent",
        username="离线发送者",
        signature="正文只留在中央消息库。",
        conversation_id="离线重放群",
    )
    sent = store.send(
        authorized_session_id=sender["session_id"],
        sender_participant_id=sender["participant_id"],
        conversation_id="离线重放群",
        body_text="接收机器现在断线，但恢复后必须知道有这条消息。",
        audience_kind="room",
        mentions=[receiver["participant_id"]],
    )
    queue = tmp_path / "remote" / "wake-queue.db"
    cursor_file = tmp_path / "remote" / "listener.cursor"
    enqueue_command = [
        str(BRIDGE_ROOT / "bin" / "agent-bridge-supervisor"),
        "enqueue",
        "--database",
        str(queue),
    ]

    with bridge_server(database) as server_url:
        environment = dict(os.environ)
        for name in (
            "AGENT_BRIDGE_TOKEN",
            "AGENT_TOKEN",
            "AGENT_BRIDGE_REGISTRATION_SECRET",
        ):
            environment.pop(name, None)
        environment.update(
            {
                "AGENT_BRIDGE_URL": server_url,
                "AGENT_BRIDGE_PRODUCT": "codex",
                "AGENT_BRIDGE_USERNAME": "离线接收者",
                "AGENT_BRIDGE_SIGNATURE": "重连后先收元数据。",
                "AGENT_BRIDGE_CONVERSATION_ID": "离线重放群",
                "AGENT_BRIDGE_CURSOR_FILE": str(cursor_file),
                "AGENT_BRIDGE_WAKE_COMMAND_JSON": json.dumps(enqueue_command),
                "AGENT_BRIDGE_WAKE_POLICY": "all",
            }
        )
        listener_run = subprocess.run(
            [str(BRIDGE_ROOT / "bin" / "agent-bridge-listen"), "--once"],
            cwd=str(BRIDGE_ROOT),
            env=environment,
            capture_output=True,
            text=True,
            timeout=12,
            check=False,
        )

    assert listener_run.returncode == 0, listener_run.stderr
    assert queue_status(queue)["counts"]["pending"] == 1
    assert _read_cursor(cursor_file) == sent["sequence"]

    captured = tmp_path / "wake-batch.json"
    adapter = [
        str(BRIDGE_ROOT / ".venv" / "bin" / "python"),
        "-c",
        (
            "import pathlib,sys; "
            "pathlib.Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())"
        ),
        str(captured),
    ]
    supervisor_run = subprocess.run(
        [
            str(BRIDGE_ROOT / "bin" / "agent-bridge-supervisor"),
            "run",
            "--database",
            str(queue),
            "--adapter-command-json",
            json.dumps(adapter),
            "--wake-policy",
            "all",
            "--debounce",
            "0",
            "--once",
        ],
        cwd=str(BRIDGE_ROOT),
        capture_output=True,
        text=True,
        timeout=12,
        check=False,
    )

    assert supervisor_run.returncode == 0, supervisor_run.stderr
    assert queue_status(queue)["counts"]["handled"] == 1
    batch = json.loads(captured.read_text(encoding="utf-8"))
    assert batch["wake_priority"] == "mention"
    assert batch["last_event_id"] == sent["sequence"]
    assert "接收机器现在断线" not in captured.read_text(encoding="utf-8")
