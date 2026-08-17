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
    invitation_token: str | None
    enrollment_token: str | None
    enrollment_token_file: Path | None
    connector_id: str | None
    auto_register: bool
    allow_direct_registration: bool
    auto_register_username: str
    auto_register_signature: str
    auto_register_conversation_id: str
    auto_register_roles: tuple[str, ...]
    auto_register_capabilities: tuple[str, ...]

    @classmethod
    def from_env(cls) -> "BridgeConfig":
        home = Path(os.environ.get("AGENT_BRIDGE_HOME", "~/.agent-bridge")).expanduser()
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
            )
            .strip()
            .rstrip("/"),
            poll_interval_seconds=poll_interval,
            maximum_wait_seconds=maximum_wait,
            registration_secret=read_registration_secret(),
            invitation_token=read_invitation_token(),
            enrollment_token=read_enrollment_token(),
            enrollment_token_file=read_enrollment_token_file(),
            connector_id=read_connector_id(),
            auto_register=_truthy(os.environ.get("AGENT_BRIDGE_AUTO_REGISTER")),
            allow_direct_registration=_truthy(
                os.environ.get("AGENT_BRIDGE_ALLOW_DIRECT_REGISTRATION")
            ),
            auto_register_username=os.environ.get(
                "AGENT_BRIDGE_USERNAME",
                "",
            ).strip(),
            auto_register_signature=os.environ.get(
                "AGENT_BRIDGE_SIGNATURE",
                "",
            ).strip(),
            auto_register_conversation_id=os.environ.get(
                "AGENT_BRIDGE_CONVERSATION_ID",
                "",
            ).strip(),
            auto_register_roles=_split_tokens(os.environ.get("AGENT_BRIDGE_ROLES")),
            auto_register_capabilities=_split_tokens(
                os.environ.get("AGENT_BRIDGE_CAPABILITIES")
            ),
        )


def read_registration_secret() -> str | None:
    """Load optional registration authority without putting it on argv."""

    return _read_secret(
        direct_name="AGENT_BRIDGE_REGISTRATION_SECRET",
        file_name="AGENT_BRIDGE_REGISTRATION_SECRET_FILE",
        label="Agent Bridge registration secret",
    )


def read_invitation_token() -> str | None:
    """Load invitation authority for the initial MCP acceptance process."""

    return _read_secret(
        direct_name="AGENT_BRIDGE_INVITATION_TOKEN",
        file_name="AGENT_BRIDGE_INVITATION_TOKEN_FILE",
        label="Agent Bridge invitation token",
    )


def read_enrollment_token() -> str | None:
    """Load a connector-scoped re-registration credential."""

    return _read_secret(
        direct_name="AGENT_BRIDGE_ENROLLMENT_TOKEN",
        file_name="AGENT_BRIDGE_ENROLLMENT_TOKEN_FILE",
        label="Agent Bridge enrollment token",
    )


def read_enrollment_token_file() -> Path | None:
    """Return the private file that can be atomically rotated, if configured."""

    value = os.environ.get("AGENT_BRIDGE_ENROLLMENT_TOKEN_FILE", "").strip()
    return Path(value).expanduser() if value else None


def read_connector_id() -> str | None:
    """Load the non-secret connector identity paired with enrollment authority."""

    return os.environ.get("AGENT_BRIDGE_CONNECTOR_ID", "").strip() or None


def _read_secret(*, direct_name: str, file_name: str, label: str) -> str | None:
    """Read a secret from an environment value or a private file."""

    direct = os.environ.get(direct_name, "").strip()
    if direct:
        return direct
    path_value = os.environ.get(file_name, "").strip()
    if not path_value:
        return None
    path = Path(path_value).expanduser()
    try:
        secret = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"cannot read {label} file") from exc
    if not secret:
        raise RuntimeError(f"{label} file is empty")
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


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _split_tokens(value: str | None) -> tuple[str, ...]:
    return tuple(item.strip() for item in str(value or "").split(",") if item.strip())
