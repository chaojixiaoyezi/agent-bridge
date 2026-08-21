from __future__ import annotations

import pytest

from agent_bridge.config import BridgeConfig
from agent_bridge.http_client import BridgeHttpClient
from agent_bridge.transport_security import (
    BridgeTransportError,
    invitation_trusted_http_host,
    validate_bridge_url,
)


def test_private_http_requires_an_exact_invitation_pin() -> None:
    bridge_url = "http://100.79.24.67:8765/"
    assert invitation_trusted_http_host(bridge_url) == "100.79.24.67"
    with pytest.raises(BridgeTransportError, match="invitation-pinned"):
        validate_bridge_url(bridge_url)
    assert (
        validate_bridge_url(
            bridge_url,
            trusted_http_host="100.79.24.67",
        )
        == "http://100.79.24.67:8765"
    )
    with pytest.raises(BridgeTransportError, match="invitation-pinned"):
        validate_bridge_url(
            bridge_url,
            trusted_http_host="100.79.24.68",
        )


@pytest.mark.parametrize(
    "bridge_url,expected_host",
    [
        ("http://10.2.3.4:8765", "10.2.3.4"),
        ("http://172.20.1.2:8765", "172.20.1.2"),
        ("http://192.168.1.9:8765", "192.168.1.9"),
        ("http://[fd7a:115c:a1e0::42]:8765", "fd7a:115c:a1e0::42"),
        ("http://127.0.0.1:8765", None),
        ("https://bridge.example.test", None),
        ("http://203.0.113.8:8765", None),
        ("http://bridge.internal:8765", None),
    ],
)
def test_only_literal_private_network_addresses_can_be_invitation_pinned(
    bridge_url: str,
    expected_host: str | None,
) -> None:
    assert invitation_trusted_http_host(bridge_url) == expected_host


def test_public_http_cannot_be_unlocked_with_a_trust_pin() -> None:
    with pytest.raises(BridgeTransportError, match="HTTPS"):
        validate_bridge_url(
            "http://203.0.113.8:8765",
            trusted_http_host="203.0.113.8",
        )
    with pytest.raises(BridgeTransportError, match="HTTPS"):
        validate_bridge_url(
            "http://bridge.example.test:8765",
            trusted_http_host="bridge.example.test",
        )


def test_legacy_invitation_infers_private_pin_without_breaking_legacy_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AGENT_BRIDGE_TRUSTED_HTTP_HOST", raising=False)
    legacy_client = BridgeHttpClient("http://100.79.24.67:8765")
    assert legacy_client.trusted_http_host is None

    client = BridgeHttpClient(
        "http://100.79.24.67:8765",
        invitation_token="invite_legacy-private",
    )
    assert client.base_url == "http://100.79.24.67:8765"
    assert client.trusted_http_host == "100.79.24.67"


def test_public_http_invitation_fails_before_network_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = BridgeHttpClient(
        "http://203.0.113.8:8765",
        invitation_token="invite_public-http",
    )
    network_calls: list[object] = []
    monkeypatch.setattr(
        client,
        "_post",
        lambda *_args, **_kwargs: network_calls.append(object()),
    )

    with pytest.raises(BridgeTransportError, match="HTTPS"):
        client.accept_invitation(
            product="codex",
            username="blocked-agent",
            signature="不应发送。",
        )

    assert network_calls == []


def test_invitation_does_not_silently_correct_a_mismatched_explicit_pin() -> None:
    client = BridgeHttpClient(
        "http://100.79.24.67:8765",
        invitation_token="invite_wrong-pin",
        trusted_http_host="100.79.24.68",
    )

    with pytest.raises(BridgeTransportError, match="invitation-pinned"):
        client.accept_invitation(
            product="codex",
            username="blocked-agent",
            signature="不应发送。",
        )


def test_bridge_config_upgrades_an_old_private_invitation_in_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_BRIDGE_URL", "http://100.79.24.67:8765")
    monkeypatch.setenv("AGENT_BRIDGE_INVITATION_TOKEN", "invite_legacy-private")
    monkeypatch.delenv("AGENT_BRIDGE_TRUSTED_HTTP_HOST", raising=False)

    config = BridgeConfig.from_env()

    assert config.trusted_http_host == "100.79.24.67"
