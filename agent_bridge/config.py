from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BridgeConfig:
    home: Path
    database: Path
    client_type: str
    server_url: str
    poll_interval_seconds: float
    maximum_wait_seconds: float
    registration_secret: str | None

    @classmethod
    def from_env(cls) -> "BridgeConfig":
        home = Path(
            os.environ.get("AGENT_BRIDGE_HOME", "~/.agent-bridge")
        ).expanduser()
        database = Path(
            os.environ.get("AGENT_BRIDGE_DB", str(home / "bridge.db"))
        ).expanduser()
        poll_interval = _bounded_float(
            os.environ.get("AGENT_BRIDGE_POLL_SECONDS"),
            default=0.2,
            minimum=0.05,
            maximum=2.0,
        )
        maximum_wait = _bounded_float(
            os.environ.get("AGENT_BRIDGE_MAX_WAIT_SECONDS"),
            default=45.0,
            minimum=1.0,
            maximum=120.0,
        )
        return cls(
            home=home,
            database=database,
            client_type=os.environ.get("AGENT_BRIDGE_CLIENT_TYPE", "").strip(),
            server_url=os.environ.get(
                "AGENT_BRIDGE_URL",
                "http://127.0.0.1:8765",
            ).strip().rstrip("/"),
            poll_interval_seconds=poll_interval,
            maximum_wait_seconds=maximum_wait,
            registration_secret=read_registration_secret(),
        )


def read_registration_secret() -> str | None:
    """Load optional registration authority without putting it on argv."""

    direct = os.environ.get("AGENT_BRIDGE_REGISTRATION_SECRET", "").strip()
    if direct:
        return direct
    path_value = os.environ.get(
        "AGENT_BRIDGE_REGISTRATION_SECRET_FILE",
        "",
    ).strip()
    if not path_value:
        return None
    path = Path(path_value).expanduser()
    try:
        secret = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError("cannot read Agent Bridge registration secret file") from exc
    if not secret:
        raise RuntimeError("Agent Bridge registration secret file is empty")
    return secret


def _bounded_float(
    value: str | None,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        parsed = float(value) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, minimum), maximum)
