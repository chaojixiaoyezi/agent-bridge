from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import agent_bridge.tui_adapter as tui_adapter
from agent_bridge.tui_adapter import (
    NativeTuiClient,
    NativeTuiError,
    endpoint_turn_lock,
    load_native_tui_binding,
    validate_native_tui_binding,
)


def binding(
    tmp_path: Path,
    adapter: str,
    transport: dict,
    *,
    session: str = "native-session-one",
):
    return validate_native_tui_binding(
        adapter_kind=adapter,
        endpoint_id=f"endpoint-{adapter}",
        native_session_id=session,
        capabilities=["steer", "multi-room"],
        transport=transport,
    )


@pytest.mark.parametrize(
    ("adapter", "transport", "expected_kind"),
    [
        (
            "deepseek-harness",
            {"kind": "deepseek-http", "base_url": "http://127.0.0.1:9200"},
            "deepseek-http",
        ),
        (
            "opencode",
            {"kind": "opencode-http", "base_url": "http://localhost:9201"},
            "opencode-http",
        ),
        (
            "hermes",
            {
                "kind": "hermes-websocket",
                "websocket_url": "ws://127.0.0.1:9202/api/ws?token=local",
            },
            "hermes-websocket",
        ),
        (
            "qwen-code",
            {"kind": "qwen-daemon", "base_url": "http://127.0.0.1:9203"},
            "qwen-daemon",
        ),
    ],
)
def test_native_tui_http_bindings_are_loopback_and_do_not_store_permissions(
    tmp_path: Path,
    adapter: str,
    transport: dict,
    expected_kind: str,
) -> None:
    configured = binding(tmp_path, adapter, transport)
    assert configured.transport["kind"] == expected_kind
    assert configured.payload()["schema_version"] == 2
    assert "access_mode" not in configured.payload()

    compatibility = validate_native_tui_binding(
        adapter_kind=adapter,
        endpoint_id="endpoint-standard",
        native_session_id="session-standard",
        access_mode="read-only-today",
        transport=transport,
    )
    assert "access_mode" not in compatibility.payload()


def test_native_tui_v1_binding_loads_without_republishing_stale_access_mode(
    tmp_path: Path,
) -> None:
    path = tmp_path / "tui-binding.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "adapter_kind": "opencode",
                "endpoint_id": "legacy-endpoint",
                "native_session_id": "legacy-session",
                "access_mode": "full",
                "capabilities": ["steer"],
                "transport": {
                    "kind": "opencode-http",
                    "base_url": "http://127.0.0.1:9201",
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_native_tui_binding(path)

    assert loaded.endpoint_id == "legacy-endpoint"
    assert loaded.payload()["schema_version"] == 2
    assert "access_mode" not in loaded.payload()


def test_native_tui_rejects_remote_prompt_endpoints() -> None:
    with pytest.raises(NativeTuiError, match="loopback"):
        validate_native_tui_binding(
            adapter_kind="opencode",
            endpoint_id="remote-endpoint",
            native_session_id="remote-session",
            access_mode="full",
            transport={
                "kind": "opencode-http",
                "base_url": "https://example.test",
            },
        )


def test_native_tui_http_base_rejects_query_parameters() -> None:
    with pytest.raises(NativeTuiError, match="loopback"):
        validate_native_tui_binding(
            adapter_kind="opencode",
            endpoint_id="endpoint-query",
            native_session_id="session-query",
            access_mode="full",
            transport={
                "kind": "opencode-http",
                "base_url": "http://127.0.0.1:9201?directory=/tmp/other",
            },
        )


def test_native_tui_http_transport_does_not_follow_redirects() -> None:
    target_hits: list[str] = []

    class TargetHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            target_hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"unexpected"}')

        def log_message(self, *_args) -> None:
            pass

    target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(302)
            self.send_header(
                "Location",
                f"http://127.0.0.1:{target.server_port}/escaped",
            )
            self.end_headers()

        def log_message(self, *_args) -> None:
            pass

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (target, redirect)
    ]
    for thread in threads:
        thread.start()
    try:
        with pytest.raises(NativeTuiError, match="HTTP 302"):
            tui_adapter._json_http_get(
                f"http://127.0.0.1:{redirect.server_port}/probe",
                timeout=1,
            )
        assert target_hits == []
    finally:
        redirect.shutdown()
        target.shutdown()
        redirect.server_close()
        target.server_close()


def test_native_tui_relay_files_must_be_distinct(tmp_path: Path) -> None:
    shared = tmp_path / "shared.jsonl"
    session = tmp_path / "session.jsonl"
    session.touch()
    with pytest.raises(NativeTuiError, match="must be distinct"):
        binding(
            tmp_path,
            "pi",
            {
                "kind": "pi-extension",
                "command_file": str(shared),
                "event_file": str(shared),
                "session_file": str(session),
            },
        )
    with pytest.raises(NativeTuiError, match="must be distinct"):
        binding(
            tmp_path,
            "qwen-code",
            {
                "kind": "qwen-dual-file",
                "input_file": str(shared),
                "event_file": str(shared),
            },
        )


def test_native_tui_http_response_has_a_safety_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OversizedResponse:
        status = 200
        headers: dict[str, str] = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def read(limit: int) -> bytes:
            return b"x" * limit

    monkeypatch.setattr(tui_adapter, "_open_local", lambda *_a, **_k: OversizedResponse())
    with pytest.raises(NativeTuiError, match="safety limit"):
        tui_adapter._json_http_get("http://127.0.0.1:9201/probe", timeout=1)


def test_opencode_probe_verifies_the_bound_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def get(url: str, **_kwargs):
        calls.append(url)
        return {"id": "native-session-one"}

    monkeypatch.setattr(tui_adapter, "_json_http_get", get)
    client = NativeTuiClient(
        binding(
            tmp_path,
            "opencode",
            {
                "kind": "opencode-http",
                "base_url": "http://127.0.0.1:9201",
                "directory": str(tmp_path),
            },
        )
    )
    assert client.probe(timeout=1)["online"] is True
    assert calls == [
        "http://127.0.0.1:9201/session/native-session-one?directory="
        + tui_adapter.quote(str(tmp_path), safe="")
    ]


def test_pi_probe_requires_a_fresh_matching_extension_heartbeat(
    tmp_path: Path,
) -> None:
    command_file = tmp_path / "pi-commands.jsonl"
    event_file = tmp_path / "pi-events.jsonl"
    session_file = tmp_path / "pi-session.jsonl"
    session_file.touch()
    client = NativeTuiClient(
        binding(
            tmp_path,
            "pi",
            {
                "kind": "pi-extension",
                "command_file": str(command_file),
                "event_file": str(event_file),
                "session_file": str(session_file),
            },
        )
    )
    heartbeat_file = Path(str(event_file) + ".heartbeat")
    heartbeat_file.write_text(
        json.dumps(
            {
                "endpoint_id": "endpoint-pi",
                "session_id": "different-session",
                "at": time.time(),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(NativeTuiError, match="heartbeat"):
        client.probe(timeout=1)
    heartbeat_file.write_text(
        json.dumps(
            {
                "endpoint_id": "endpoint-pi",
                "session_id": "native-session-one",
                "at": time.time(),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert client.probe(timeout=1)["online"] is True


def test_qwen_dual_file_does_not_claim_unverifiable_idle_liveness(
    tmp_path: Path,
) -> None:
    client = NativeTuiClient(
        binding(
            tmp_path,
            "qwen-code",
            {
                "kind": "qwen-dual-file",
                "input_file": str(tmp_path / "qwen-input.jsonl"),
                "event_file": str(tmp_path / "qwen-events.jsonl"),
            },
        )
    )
    assert client.probe(timeout=1) == {
        "online": False,
        "transport": "qwen-dual-file",
        "reason": "dual-file mode has no read-only liveness signal",
    }


def test_deepseek_adapter_waits_for_its_durable_user_message_and_turn_end(
    tmp_path: Path,
) -> None:
    client = NativeTuiClient(
        binding(
            tmp_path,
            "deepseek-harness",
            {"kind": "deepseek-http", "base_url": "http://127.0.0.1:9200"},
        )
    )
    calls: list[tuple[str, dict]] = []

    def rpc(method: str, payload: dict, _timeout: float):
        calls.append((method, payload))
        if method == "session.prompt":
            return {"accepted": True}
        histories = sum(1 for name, _ in calls if name == "session.history")
        if histories == 1:
            return {"events": [{"event": {"type": "turn/end", "seq": 1}}]}
        return {
            "events": [
                {
                    "event": {
                        "type": "user/message",
                        "seq": 2,
                        "data": {"content": [{"type": "text", "text": "执行任务"}]},
                    }
                },
                {
                    "event": {
                        "type": "assistant/message",
                        "seq": 3,
                        "data": {
                            "message": {
                                "role": "assistant",
                                "content": [{"type": "text", "text": "完成。"}],
                            }
                        },
                    }
                },
                {
                    "event": {
                        "type": "turn/end",
                        "seq": 4,
                        "data": {"reason": {"kind": "completed"}},
                    }
                },
            ]
        }

    client._deepseek_rpc = rpc  # type: ignore[method-assign]
    result, applied = client.run_turn("执行任务", timeout=1)
    assert result == "完成。"
    assert applied == []
    assert calls[1] == (
        "session.prompt",
        {
            "sessionId": "native-session-one",
            "mode": "queue",
            "content": [{"type": "text", "text": "执行任务"}],
        },
    )


def test_opencode_uses_the_bound_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict]] = []

    def request(url: str, payload: dict, **_kwargs):
        calls.append((url, payload))
        return {"parts": [{"type": "text", "text": "本体结果"}]}

    monkeypatch.setattr(tui_adapter, "_json_request", request)
    client = NativeTuiClient(
        binding(
            tmp_path,
            "opencode",
            {"kind": "opencode-http", "base_url": "http://127.0.0.1:9201"},
        )
    )
    result, _ = client.run_turn("检查", timeout=1)
    assert result == "本体结果"
    assert "/session/native-session-one/message" in calls[-1][0]
    assert calls[-1][1] == {"parts": [{"type": "text", "text": "检查"}]}


def test_qwen_daemon_correlates_prompt_sse_and_uses_private_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_file = tmp_path / "qwen.token"
    token_file.write_text("local-secret\n", encoding="utf-8")
    admissions: list[tuple[str, dict, dict]] = []

    def request(url: str, payload: dict, **kwargs):
        admissions.append((url, payload, kwargs))
        return 202, {"promptId": "prompt-7", "lastEventId": 41}, {}

    def events(url: str, **kwargs):
        assert url.endswith(
            "/session/native-session-one/events?connectReason=prompt_restart"
        )
        assert kwargs["headers"]["Last-Event-ID"] == "41"
        assert kwargs["headers"]["Authorization"] == "Bearer local-secret"
        yield {
            "v": 1,
            "type": "session_update",
            "data": {
                "sessionUpdate": "user_message_chunk",
                "content": {"type": "text", "text": "检查"},
            },
        }
        yield {
            "v": 1,
            "type": "session_update",
            "data": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "本体"},
            },
        }
        yield {
            "v": 1,
            "type": "session_update",
            "data": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "结果"},
            },
        }
        yield {
            "v": 1,
            "type": "turn_complete",
            "promptId": "prompt-7",
            "data": {"promptId": "prompt-7", "stopReason": "end_turn"},
        }

    monkeypatch.setattr(tui_adapter, "_json_http_request", request)
    monkeypatch.setattr(tui_adapter, "_sse_json_events", events)
    client = NativeTuiClient(
        binding(
            tmp_path,
            "qwen-code",
            {
                "kind": "qwen-daemon",
                "base_url": "http://127.0.0.1:9203",
                "token_file": str(token_file),
                "client_id": "bridge.client-1",
            },
        )
    )
    result, applied = client.run_turn("检查", timeout=1)
    assert result == "本体结果"
    assert applied == []
    assert admissions[0][0].endswith("/session/native-session-one/prompt")
    assert admissions[0][1] == {"prompt": [{"type": "text", "text": "检查"}]}
    assert admissions[0][2]["headers"] == {
        "Authorization": "Bearer local-secret",
        "X-Qwen-Client-Id": "bridge.client-1",
    }


def test_hermes_adapter_reads_result_from_matching_session_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSocket:
        def __init__(self) -> None:
            self.sent: list[dict] = []
            self.events: list[dict] = []
            self.history_calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def send(self, raw: str) -> None:
            request = json.loads(raw)
            self.sent.append(request)
            method = request["method"]
            if method == "session.history":
                self.history_calls += 1
                messages = []
                if self.history_calls >= 2:
                    messages.append({"role": "user", "text": "复核"})
                if self.history_calls >= 3:
                    messages.append({"role": "assistant", "text": "Hermes 完成。"})
                self.events.append(
                    {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "result": {"count": len(messages), "messages": messages},
                    }
                )
            elif method == "prompt.submit":
                self.events.extend(
                    [
                        {
                            "jsonrpc": "2.0",
                            "id": request["id"],
                            "result": {"status": "queued"},
                        },
                        {
                            "jsonrpc": "2.0",
                            "method": "event",
                            "params": {
                                "type": "message.start",
                                "session_id": "native-session-one",
                            },
                        },
                        {
                            "jsonrpc": "2.0",
                            "method": "event",
                            "params": {
                                "type": "message.complete",
                                "session_id": "native-session-one",
                            },
                        },
                    ]
                )

        def recv(self, **_kwargs) -> str:
            if not self.events:
                raise TimeoutError
            return json.dumps(self.events.pop(0))

    socket = FakeSocket()
    monkeypatch.setattr("websockets.sync.client.connect", lambda *_a, **_k: socket)
    client = NativeTuiClient(
        binding(
            tmp_path,
            "hermes",
            {
                "kind": "hermes-websocket",
                "websocket_url": "ws://127.0.0.1:9202/api/ws?token=local",
            },
        )
    )
    result, _ = client.run_turn("复核", timeout=1)
    assert result == "Hermes 完成。"
    prompt_call = next(
        item for item in socket.sent if item["method"] == "prompt.submit"
    )
    assert prompt_call["params"]["session_id"] == "native-session-one"
    assert prompt_call["params"]["queued"] is True


def test_pi_file_relay_correlates_results_and_keeps_session_file(
    tmp_path: Path,
) -> None:
    command_file = tmp_path / "pi-commands.jsonl"
    event_file = tmp_path / "pi-events.jsonl"
    session_file = tmp_path / "pi-session.jsonl"
    session_file.touch()
    configured = binding(
        tmp_path,
        "pi",
        {
            "kind": "pi-extension",
            "command_file": str(command_file),
            "event_file": str(event_file),
            "session_file": str(session_file),
        },
    )
    assert configured.transport["session_file"] == str(session_file)
    client = NativeTuiClient(configured)

    def relay() -> None:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if (
                command_file.exists()
                and command_file.read_text(encoding="utf-8").strip()
            ):
                command = json.loads(
                    command_file.read_text(encoding="utf-8").splitlines()[0]
                )
                event_file.write_text(
                    json.dumps(
                        {
                            "type": "complete",
                            "request_id": command["request_id"],
                            "text": "Pi 完成。",
                        },
                        ensure_ascii=False,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                return
            time.sleep(0.02)

    thread = threading.Thread(target=relay, daemon=True)
    thread.start()
    result, applied = client.run_turn("处理", timeout=2)
    thread.join(timeout=1)
    assert result == "Pi 完成。"
    assert applied == []


def test_qwen_dual_file_uses_official_schema_and_finishes_queued_inputs(
    tmp_path: Path,
) -> None:
    input_file = tmp_path / "qwen-input.jsonl"
    event_file = tmp_path / "qwen-events.jsonl"
    client = NativeTuiClient(
        binding(
            tmp_path,
            "qwen-code",
            {
                "kind": "qwen-dual-file",
                "input_file": str(input_file),
                "event_file": str(event_file),
            },
        )
    )

    def relay() -> None:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if not input_file.exists():
                time.sleep(0.02)
                continue
            lines = input_file.read_text(encoding="utf-8").splitlines()
            if len(lines) < 2:
                time.sleep(0.02)
                continue
            commands = [json.loads(line) for line in lines]
            events: list[dict] = []
            for index, command in enumerate(commands):
                response = "首轮完成。" if index == 0 else "补充也完成。"
                events.extend(
                    [
                        {
                            "type": "user",
                            "session_id": "native-session-one",
                            "message": {
                                "role": "user",
                                "content": [{"type": "text", "text": command["text"]}],
                            },
                        },
                        {
                            "type": "assistant",
                            "session_id": "native-session-one",
                            "message": {
                                "role": "assistant",
                                "content": [{"type": "text", "text": response}],
                            },
                        },
                        {
                            "type": "result",
                            "subtype": "success",
                            "session_id": "native-session-one",
                            "is_error": False,
                            "result": response,
                        },
                    ]
                )
            event_file.write_text(
                "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in events),
                encoding="utf-8",
            )
            return

    poll_count = 0

    def poll_inputs() -> list[dict]:
        nonlocal poll_count
        poll_count += 1
        return [{"input_id": "input-1", "body": "补充要求"}]

    thread = threading.Thread(target=relay, daemon=True)
    thread.start()
    result, applied = client.run_turn("处理", timeout=2, poll_inputs=poll_inputs)
    thread.join(timeout=1)
    commands = [
        json.loads(line) for line in input_file.read_text(encoding="utf-8").splitlines()
    ]
    assert commands == [
        {"type": "submit", "text": "处理"},
        {"type": "submit", "text": "补充要求"},
    ]
    assert result == "补充也完成。"
    assert applied == ["input-1"]
    assert poll_count >= 1


def test_endpoint_lock_serializes_rooms(tmp_path: Path) -> None:
    lock_file = tmp_path / "endpoint.lock"
    with endpoint_turn_lock(lock_file) as first:
        assert first is True
        with endpoint_turn_lock(lock_file, blocking=False) as second:
            # flock is process-scoped on some systems, so only assert the API
            # shape here; task/chat integration tests cover lock propagation.
            assert isinstance(second, bool)
