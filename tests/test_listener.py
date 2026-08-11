from __future__ import annotations

from pathlib import Path

import pytest

from agent_bridge.listener import (
    ListenerError,
    _iter_sse_events,
    _read_cursor,
    _validated_base_url,
    _validated_webhook,
    _write_cursor,
)


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
