"""Shared transport policy for Agent Bridge clients and resident connectors."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse


TRUSTED_HTTP_HOST_ENV = "AGENT_BRIDGE_TRUSTED_HTTP_HOST"

_PRIVATE_HTTP_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        # Tailscale IPv4 addresses are allocated from the shared 100.64/10 range.
        "100.64.0.0/10",
        # Includes Tailscale's fd7a:115c:a1e0::/48 allocation.
        "fc00::/7",
    )
)


class BridgeTransportError(ValueError):
    pass


def _parsed_bridge_url(value: str):
    normalized = str(value or "").strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise BridgeTransportError("Bridge URL must be an http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise BridgeTransportError(
            "Bridge URL cannot contain credentials or query data"
        )
    return normalized, parsed


def _ip_address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    candidate = str(value or "").strip()
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped
    return address


def _canonical_host(value: str) -> str:
    address = _ip_address(value)
    if address is not None:
        return address.compressed
    return str(value or "").strip().rstrip(".").casefold()


def _is_loopback_host(value: str) -> bool:
    if _canonical_host(value) == "localhost":
        return True
    address = _ip_address(value)
    return bool(address and address.is_loopback)


def _is_private_http_address(value: str) -> bool:
    address = _ip_address(value)
    return bool(
        address is not None
        and any(address.version == network.version and address in network for network in _PRIVATE_HTTP_NETWORKS)
    )


def invitation_trusted_http_host(value: str) -> str | None:
    """Return the exact private IP an invitation may pin, if one is needed."""

    _normalized, parsed = _parsed_bridge_url(value)
    hostname = str(parsed.hostname or "")
    if parsed.scheme != "http" or _is_loopback_host(hostname):
        return None
    if not _is_private_http_address(hostname):
        return None
    return _canonical_host(hostname)


def validate_bridge_url(
    value: str,
    *,
    trusted_http_host: str | None = None,
    allow_insecure_http: bool = False,
) -> str:
    """Validate one endpoint without turning private trust into a global bypass.

    HTTPS and loopback HTTP are always accepted. A remote HTTP endpoint is only
    accepted when it is a literal RFC1918/Tailnet/ULA address and the caller pins
    that exact address. ``allow_insecure_http`` remains an explicit legacy/manual
    escape hatch for the listener CLI; generated invitations never use it.
    """

    normalized, parsed = _parsed_bridge_url(value)
    hostname = str(parsed.hostname or "")
    if parsed.scheme != "http" or _is_loopback_host(hostname):
        return normalized
    if allow_insecure_http:
        return normalized
    pinned_host = _canonical_host(str(trusted_http_host or ""))
    actual_host = _canonical_host(hostname)
    if (
        pinned_host
        and pinned_host == actual_host
        and _is_private_http_address(actual_host)
    ):
        return normalized
    raise BridgeTransportError(
        "remote Agent Bridge HTTP requires HTTPS or an invitation-pinned "
        "private/Tailnet IP"
    )
