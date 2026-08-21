from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .http_client import BridgeHttpClient
from .validation import opaque_id


MAX_HOOK_INPUT_BYTES = 1024 * 1024


class ClaudeNativeError(RuntimeError):
    pass


@dataclass(frozen=True)
class ClaudeConnectorState:
    state_directory: Path
    manifest: dict[str, Any]
    connector_id: str
    bridge_url: str
    endpoint_id: str
    process_epoch: str
    lease_file: Path
    binding_intent_file: Path

    def client(self) -> BridgeHttpClient:
        enrollment_file = self.state_directory / "enrollment.token"

        def enrollment_loader() -> str | None:
            try:
                value = enrollment_file.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ClaudeNativeError(
                    "cannot read Claude connector enrollment credential"
                ) from exc
            return value or None

        return BridgeHttpClient(
            self.bridge_url,
            trusted_http_host=(
                str(self.manifest.get("trusted_http_host") or "").strip() or None
            ),
            enrollment_token_file=enrollment_file,
            enrollment_token_loader=enrollment_loader,
            connector_id=self.connector_id,
            connector_component="mcp",
            auto_registration={
                "product": str(self.manifest["product"]),
                "username": str(self.manifest["username"]),
                "signature": str(self.manifest["signature"]),
                "conversation_id": str(self.manifest["conversation_id"]),
                "roles": list(self.manifest.get("roles") or []),
                "capabilities": list(self.manifest.get("capabilities") or []),
            },
        )

    def read_lease(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.lease_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, TypeError, json.JSONDecodeError) as exc:
            raise ClaudeNativeError("Claude native lease file is invalid") from exc
        if not isinstance(payload, dict):
            raise ClaudeNativeError("Claude native lease file is invalid")
        return payload

    def write_lease(self, payload: dict[str, Any]) -> None:
        _atomic_private_write(
            self.lease_file,
            (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )

    def refresh_lease(self, server_lease: dict[str, Any]) -> dict[str, Any]:
        """Persist authoritative liveness fields for the exact local lease."""

        current = self.read_lease()
        if current is None:
            raise ClaudeNativeError("Claude native lease file is missing")
        for field, expected in (
            ("lease_id", current.get("lease_id")),
            ("connector_id", self.connector_id),
            ("process_epoch", self.process_epoch),
        ):
            actual = str(server_lease.get(field) or "")
            if actual and actual != str(expected or ""):
                raise ClaudeNativeError(
                    f"Claude native lease refresh returned mismatched {field}"
                )
        updated = dict(current)
        for field in ("last_seen_at", "expires_at"):
            if server_lease.get(field) is not None:
                updated[field] = float(server_lease[field])
        if server_lease.get("ended_at") is not None:
            updated["ended"] = True
            updated["ended_at"] = float(server_lease["ended_at"])
        self.write_lease(updated)
        return updated

    def read_binding_intent(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.binding_intent_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, TypeError, json.JSONDecodeError) as exc:
            raise ClaudeNativeError("Claude native binding intent is invalid") from exc
        if not isinstance(payload, dict):
            raise ClaudeNativeError("Claude native binding intent is invalid")
        return payload

    def write_binding_intent(self, payload: dict[str, Any]) -> None:
        _atomic_private_write(
            self.binding_intent_file,
            (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )

    def bind_intent(
        self,
        intent: dict[str, Any],
        *,
        client: BridgeHttpClient | None = None,
    ) -> dict[str, Any]:
        if (
            str(intent.get("connector_id") or "") != self.connector_id
            or str(intent.get("tui_endpoint_id") or "") != self.endpoint_id
            or str(intent.get("process_epoch") or "") != self.process_epoch
            or bool(intent.get("ended"))
        ):
            raise ClaudeNativeError(
                "Claude native binding intent does not match this process"
            )
        result = (client or self.client()).bind_native_session(
            connector_id=self.connector_id,
            tui_endpoint_id=self.endpoint_id,
            native_session_id=str(intent.get("native_session_id") or ""),
            process_epoch=self.process_epoch,
            binding_source=str(intent.get("binding_source") or ""),
            replace_existing_session=bool(intent.get("replace_existing_session")),
            metadata=dict(intent.get("metadata") or {}),
        )
        lease = dict(result["lease"])
        self.write_lease(
            {
                "schema_version": 1,
                "connector_id": self.connector_id,
                "lease_id": str(lease["lease_id"]),
                "tui_endpoint_id": self.endpoint_id,
                "native_session_id": str(intent["native_session_id"]),
                "process_epoch": self.process_epoch,
                "binding_source": str(intent["binding_source"]),
                "bound_at": time.time(),
                "last_seen_at": float(lease["last_seen_at"]),
                "expires_at": float(lease["expires_at"]),
                "ended": False,
            }
        )
        return result


def load_claude_connector_state(
    state_directory: str | Path | None = None,
) -> ClaudeConnectorState:
    configured = str(
        state_directory
        if state_directory is not None
        else os.environ.get("AGENT_BRIDGE_CLAUDE_STATE_DIRECTORY", "")
    ).strip()
    if not configured:
        raise ClaudeNativeError("Claude connector state directory is not configured")
    state = Path(configured).expanduser().resolve()
    try:
        manifest = json.loads((state / "connector.json").read_text(encoding="utf-8"))
    except (OSError, TypeError, json.JSONDecodeError) as exc:
        raise ClaudeNativeError("Claude connector manifest is invalid") from exc
    if not isinstance(manifest, dict):
        raise ClaudeNativeError("Claude connector manifest is invalid")
    if str(manifest.get("adapter_kind") or "") != "claude-code":
        raise ClaudeNativeError("connector is not configured for Claude Code")
    channel = manifest.get("claude_channel")
    if not isinstance(channel, dict):
        raise ClaudeNativeError("Claude native channel artifacts are not configured")
    connector_id = opaque_id(
        str(manifest.get("connector_id") or ""),
        field="connector_id",
    )
    endpoint = opaque_id(
        str(channel.get("tui_endpoint_id") or ""),
        field="tui_endpoint_id",
    )
    process_epoch = opaque_id(
        os.environ.get("AGENT_BRIDGE_CLAUDE_PROCESS_EPOCH", ""),
        field="process_epoch",
    )
    bridge_url = str(manifest.get("bridge_url") or "").strip().rstrip("/")
    if not bridge_url:
        raise ClaudeNativeError("Claude connector Bridge URL is missing")
    return ClaudeConnectorState(
        state_directory=state,
        manifest=manifest,
        connector_id=connector_id,
        bridge_url=bridge_url,
        endpoint_id=endpoint,
        process_epoch=process_epoch,
        lease_file=state / "native-lease.json",
        binding_intent_file=state / "native-binding-intent.json",
    )


def _atomic_private_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_bytes(data)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
