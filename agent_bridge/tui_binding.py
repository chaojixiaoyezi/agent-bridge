"""Native TUI binding contracts and loopback transport validation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


NATIVE_TUI_ADAPTERS = {
    "deepseek-harness",
    "opencode",
    "hermes",
    "pi",
    "qwen-code",
}


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


MAX_PROMPT_CHARS = 100_000


DEFAULT_TURN_TIMEOUT_SECONDS = 3_600.0


QWEN_CLIENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class NativeTuiError(RuntimeError):
    pass


@dataclass(frozen=True)
class NativeTuiBinding:
    adapter_kind: str
    endpoint_id: str
    native_session_id: str
    capabilities: tuple[str, ...]
    transport: dict[str, Any]

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "adapter_kind": self.adapter_kind,
            "endpoint_id": self.endpoint_id,
            "native_session_id": self.native_session_id,
            "capabilities": list(self.capabilities),
            "transport": self.transport,
        }


def _identifier(value: object, *, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 256 or "\x00" in normalized:
        raise NativeTuiError(f"{field} must contain 1-256 safe characters")
    if any(character.isspace() for character in normalized):
        raise NativeTuiError(f"{field} cannot contain whitespace")
    return normalized


def _local_url(
    value: object,
    *,
    schemes: set[str],
    field: str,
    allow_query: bool = False,
) -> str:
    normalized = str(value or "").strip().rstrip("/")
    parsed = urlparse(normalized)
    if (
        parsed.scheme not in schemes
        or parsed.hostname not in LOOPBACK_HOSTS
        or parsed.username
        or parsed.password
        or parsed.fragment
        or (parsed.query and not allow_query)
    ):
        raise NativeTuiError(f"{field} must be a safe loopback URL")
    return normalized


def _local_file(value: object, *, field: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise NativeTuiError(f"{field} is required")
    path = Path(raw).expanduser().resolve()
    if not path.is_absolute() or not path.parent.is_dir():
        raise NativeTuiError(f"{field} parent directory does not exist")
    return str(path)


def validate_native_tui_binding(
    *,
    adapter_kind: str,
    endpoint_id: str,
    native_session_id: str,
    access_mode: object = None,
    transport: dict[str, Any] | None = None,
    capabilities: list[str] | tuple[str, ...] | None = None,
) -> NativeTuiBinding:
    adapter = str(adapter_kind or "").strip().lower()
    if adapter not in NATIVE_TUI_ADAPTERS:
        raise NativeTuiError("unsupported native TUI adapter")
    endpoint = _identifier(endpoint_id, field="tui_endpoint_id")
    native_session = _identifier(
        native_session_id,
        field="tui_native_session_id",
    )
    # Kept as an ignored compatibility argument for v1 callers. Permissions
    # belong to the live TUI runtime and can change between turns; persisting a
    # claimed mode here would be stale and could never grant real authority.
    del access_mode
    raw_transport = dict(transport or {})
    kind = str(raw_transport.get("kind") or adapter).strip().lower()
    normalized: dict[str, Any]
    if adapter == "deepseek-harness":
        if kind not in {"deepseek-harness", "deepseek-http"}:
            raise NativeTuiError("DeepSeek Harness requires deepseek-http transport")
        normalized = {
            "kind": "deepseek-http",
            "base_url": _local_url(
                raw_transport.get("base_url"),
                schemes={"http"},
                field="DeepSeek Harness base_url",
            ),
        }
    elif adapter == "opencode":
        if kind not in {"opencode", "opencode-http"}:
            raise NativeTuiError("OpenCode requires opencode-http transport")
        normalized = {
            "kind": "opencode-http",
            "base_url": _local_url(
                raw_transport.get("base_url"),
                schemes={"http"},
                field="OpenCode base_url",
            ),
        }
        directory = str(raw_transport.get("directory") or "").strip()
        if directory:
            normalized["directory"] = str(Path(directory).expanduser().resolve())
    elif adapter == "hermes":
        if kind not in {"hermes", "hermes-websocket"}:
            raise NativeTuiError("Hermes requires hermes-websocket transport")
        normalized = {
            "kind": "hermes-websocket",
            "websocket_url": _local_url(
                raw_transport.get("websocket_url"),
                schemes={"ws"},
                field="Hermes websocket_url",
                allow_query=True,
            ),
        }
        stored_session_id = str(raw_transport.get("stored_session_id") or "").strip()
        if stored_session_id:
            normalized["stored_session_id"] = _identifier(
                stored_session_id,
                field="Hermes stored_session_id",
            )
    elif adapter == "pi":
        if kind not in {"pi", "pi-extension"}:
            raise NativeTuiError("Pi requires pi-extension transport")
        normalized = {
            "kind": "pi-extension",
            "command_file": _local_file(
                raw_transport.get("command_file"),
                field="Pi command_file",
            ),
            "event_file": _local_file(
                raw_transport.get("event_file"),
                field="Pi event_file",
            ),
            "session_file": _local_file(
                raw_transport.get("session_file"),
                field="Pi session_file",
            ),
        }
        relay_paths = {
            normalized["command_file"],
            normalized["event_file"],
            normalized["session_file"],
        }
        if len(relay_paths) != 3:
            raise NativeTuiError(
                "Pi command_file, event_file, and session_file must be distinct"
            )
    else:
        if kind in {"qwen-daemon", "qcode-daemon"}:
            normalized = {
                "kind": "qwen-daemon",
                "base_url": _local_url(
                    raw_transport.get("base_url"),
                    schemes={"http"},
                    field="Qwen Code daemon base_url",
                ),
            }
            token_file = str(raw_transport.get("token_file") or "").strip()
            if token_file:
                normalized["token_file"] = _local_file(
                    token_file,
                    field="Qwen Code token_file",
                )
            client_id = str(raw_transport.get("client_id") or "").strip()
            if client_id:
                if QWEN_CLIENT_ID_PATTERN.fullmatch(client_id) is None:
                    raise NativeTuiError(
                        "Qwen Code client_id must contain 1-128 URL-safe characters"
                    )
                normalized["client_id"] = client_id
        elif kind in {"qwen-code", "qwen-dual-file", "qcode"}:
            normalized = {
                "kind": "qwen-dual-file",
                "input_file": _local_file(
                    raw_transport.get("input_file"),
                    field="Qwen Code input_file",
                ),
                "event_file": _local_file(
                    raw_transport.get("event_file"),
                    field="Qwen Code event_file",
                ),
            }
            if normalized["input_file"] == normalized["event_file"]:
                raise NativeTuiError(
                    "Qwen Code input_file and event_file must be distinct"
                )
        else:
            raise NativeTuiError(
                "Qwen Code requires qwen-daemon or qwen-dual-file transport"
            )
    capability_values = tuple(
        sorted(
            {_identifier(item, field="tui_capability") for item in (capabilities or ())}
        )
    )
    return NativeTuiBinding(
        adapter_kind=adapter,
        endpoint_id=endpoint,
        native_session_id=native_session,
        capabilities=capability_values,
        transport=normalized,
    )


def load_native_tui_binding(path: Path) -> NativeTuiBinding:
    try:
        raw = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NativeTuiError("native TUI binding file is unreadable") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") not in {1, 2}:
        raise NativeTuiError("native TUI binding schema is unsupported")
    return validate_native_tui_binding(
        adapter_kind=str(raw.get("adapter_kind") or ""),
        endpoint_id=str(raw.get("endpoint_id") or ""),
        native_session_id=str(raw.get("native_session_id") or ""),
        access_mode=raw.get("access_mode"),
        capabilities=raw.get("capabilities")
        if isinstance(raw.get("capabilities"), list)
        else [],
        transport=raw.get("transport")
        if isinstance(raw.get("transport"), dict)
        else {},
    )
