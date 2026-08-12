from __future__ import annotations

from collections.abc import Iterable, Sequence

from .config import read_enrollment_token, read_registration_secret
from .http_client import BridgeHttpClient


def resident_http_client(
    *,
    bridge_url: str,
    product: str,
    username: str,
    signature: str,
    conversation_id: str,
    roles: Sequence[str],
    capabilities: Sequence[str],
) -> BridgeHttpClient:
    """Build a private deterministic client for post-turn bookkeeping."""

    return BridgeHttpClient(
        bridge_url,
        registration_secret=read_registration_secret(),
        enrollment_token=read_enrollment_token(),
        auto_registration={
            "product": product,
            "username": username,
            "signature": signature,
            "conversation_id": conversation_id,
            "roles": list(roles),
            "capabilities": list(capabilities),
        },
    )


def acknowledge_messages(
    client: BridgeHttpClient,
    message_ids: Iterable[str],
) -> frozenset[str]:
    """Acknowledge a bounded set after a successful model turn."""

    acknowledged: set[str] = set()
    for message_id in sorted({str(item).strip() for item in message_ids if str(item).strip()}):
        client.post(
            "/agent/action",
            {
                "message_id": message_id,
                "action": "ack",
                "lease_seconds": 120.0,
            },
        )
        acknowledged.add(message_id)
    return frozenset(acknowledged)
