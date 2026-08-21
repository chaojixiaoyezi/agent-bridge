from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from pathlib import Path

from .avatars import normalize_avatar_key
from .connector import ConnectorSetupError, configure_resident_connector
from .codex_native_binding import codex_native_binding
from .http_client import BridgeHttpClient, BridgeRemoteError
from .transport_security import (
    BridgeTransportError,
    invitation_trusted_http_host,
    validate_bridge_url,
)
from .tui_adapter import NativeTuiError, validate_native_tui_binding
from .validation import agent_username, alias, string_tokens, token


class InvitationCliError(RuntimeError):
    pass


def _stdin_invitation_token() -> str:
    if sys.stdin.isatty():
        raise InvitationCliError("invitation token must be piped on standard input")
    value = sys.stdin.read(4097).strip()
    if not value or len(value.encode("utf-8")) > 4096:
        raise InvitationCliError("invitation token is missing or too large")
    return value


def _supported_bridge_url(
    value: str,
    *,
    trusted_http_host: str | None = None,
) -> str:
    try:
        return validate_bridge_url(value, trusted_http_host=trusted_http_host)
    except BridgeTransportError as exc:
        raise InvitationCliError(str(exc)) from exc


def accept_invitation(args: argparse.Namespace) -> dict[str, object]:
    invitation_token = _stdin_invitation_token()
    trusted_http_host = (
        str(getattr(args, "trusted_http_host", "") or "").strip()
        or invitation_trusted_http_host(args.bridge_url)
    )
    bridge_url = _supported_bridge_url(
        args.bridge_url,
        trusted_http_host=trusted_http_host,
    )
    product = token(args.product, field="product_name")
    username = agent_username(args.username)
    signature = alias(args.signature, field="signature")
    avatar_key = normalize_avatar_key(getattr(args, "avatar_key", "auto"))
    roles = string_tokens(args.role, field="roles")
    capabilities = string_tokens(args.capability, field="capabilities")
    tui_transport: dict[str, object] | None = None
    tui_binding = None
    auto_confirm_tui_binding = False
    tui_transport_json = str(getattr(args, "tui_transport_json", "") or "")
    if tui_transport_json:
        try:
            raw_transport = json.loads(tui_transport_json)
        except json.JSONDecodeError as exc:
            raise InvitationCliError(
                "--tui-transport-json must be a JSON object"
            ) from exc
        if not isinstance(raw_transport, dict):
            raise InvitationCliError("--tui-transport-json must be a JSON object")
        tui_transport = raw_transport
        try:
            tui_binding = validate_native_tui_binding(
                adapter_kind=getattr(args, "tui_adapter", ""),
                endpoint_id=getattr(args, "tui_endpoint_id", ""),
                native_session_id=getattr(args, "tui_session_id", ""),
                capabilities=getattr(args, "tui_capability", []),
                transport=tui_transport,
            )
        except NativeTuiError as exc:
            raise InvitationCliError(str(exc)) from exc
    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        raise InvitationCliError("Agent workspace does not exist")
    execution_source_thread_id = (
        str(
            getattr(args, "execution_source_thread_id", "")
            or os.environ.get("CODEX_THREAD_ID", "")
        ).strip()
        or None
    )
    if product.casefold() == "codex" and tui_binding is None:
        try:
            tui_binding = codex_native_binding(
                thread_id=execution_source_thread_id,
                workspace=workspace,
            )
        except NativeTuiError as exc:
            raise InvitationCliError(str(exc)) from exc
        auto_confirm_tui_binding = True
    client_arguments = {"invitation_token": invitation_token}
    if trusted_http_host:
        client_arguments["trusted_http_host"] = trusted_http_host
    client = BridgeHttpClient(bridge_url, **client_arguments)
    acceptance_payload: dict[str, object] = {
        "product": product,
        "username": username,
        "signature": signature,
        "avatar_key": avatar_key,
        "roles": roles,
        "capabilities": capabilities,
    }
    if tui_binding is not None:
        acceptance_payload.update(
            {
                "tui_endpoint_id": tui_binding.endpoint_id,
                "tui_native_session_id": tui_binding.native_session_id,
                "tui_confirmed": (
                    auto_confirm_tui_binding
                    or bool(getattr(args, "confirm_tui_binding", False))
                ),
            }
        )
    accepted = client.accept_invitation(
        **acceptance_payload,
    )
    enrollment_token = str(accepted.pop("_enrollment_token", ""))
    connector_id = str(accepted["connector_id"])
    assigned_username = str(accepted.get("username") or username)
    try:
        setup = configure_resident_connector(
            connector_id=connector_id,
            enrollment_token=enrollment_token,
            bridge_url=bridge_url,
            trusted_http_host=trusted_http_host,
            product=product,
            username=assigned_username,
            signature=signature,
            conversation_id=str(accepted["conversation_id"]),
            adapter_kind=str(accepted["adapter_kind"]),
            requested_mode=str(accepted["requested_mode"]),
            tui_adapter_kind=(
                accepted.get("tui_adapter_kind")
                or (tui_binding.adapter_kind if tui_binding else None)
            ),
            tui_endpoint_id=(tui_binding.endpoint_id if tui_binding else None),
            tui_native_session_id=(
                tui_binding.native_session_id if tui_binding else None
            ),
            tui_capabilities=(list(tui_binding.capabilities) if tui_binding else None),
            tui_transport=(tui_binding.transport if tui_binding else None),
            roles=list(accepted.get("roles") or []),
            capabilities=list(accepted.get("capabilities") or []),
            workspace_path=str(workspace),
            execution_source_thread_id=execution_source_thread_id,
            enable_resident=not args.basic,
        )
        setup_payload = setup.public_payload()
    except (ConnectorSetupError, OSError) as exc:
        setup_payload = {
            "status": "failed",
            "platform": platform.system() or "unknown",
            "adapter_kind": str(
                accepted.get("tui_adapter_kind") or accepted["adapter_kind"]
            ),
            "connector_id": connector_id,
            "state_directory": "",
            "listener_service": None,
            "worker_service": None,
            "task_service": None,
            "launch_command": None,
            "detail": str(exc),
        }
    report_detail = {
        key: value
        for key, value in setup_payload.items()
        if key not in {"status", "connector_id", "state_directory"}
    }
    try:
        connector = client.post(
            "/agent/connector/setup",
            {
                "connector_id": connector_id,
                "setup_status": setup_payload["status"],
                "detail": report_detail,
            },
        )["connector"]
    except Exception as exc:
        setup_payload["report_warning"] = (
            "local setup finished but Bridge status reporting failed: " + str(exc)
        )
        connector = None
    result = {
        "invitation_accepted": True,
        "invitation_consumed": not bool(accepted.get("invitation_reusable", False)),
        "participant_id": accepted.get("participant_id"),
        "conversation_id": accepted.get("conversation_id"),
        "resident_setup": setup_payload,
        "connector": connector,
    }
    if setup_payload.get("duty_mode") == "direct_tui":
        result["direct_tui_duty"] = {
            "body_seat": "this_exact_tui",
            "shadow_installed": False,
            "required_next_tool": "agent_duty",
            "instruction": (
                "Call agent_duty now in this same TUI. Handle returned room "
                "events here and call agent_duty again after every event or timeout."
            ),
        }
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Accept one Agent Bridge invitation without configuring MCP"
    )
    parser.add_argument("--bridge-url", required=True)
    parser.add_argument(
        "--trusted-http-host",
        default=os.environ.get("AGENT_BRIDGE_TRUSTED_HTTP_HOST", ""),
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--product", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--signature", required=True)
    parser.add_argument("--avatar-key", default="auto")
    parser.add_argument("--workspace", default=os.getcwd())
    parser.add_argument(
        "--execution-source-thread-id",
        default=os.environ.get("CODEX_THREAD_ID", ""),
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--role", action="append", default=[])
    parser.add_argument("--capability", action="append", default=[])
    parser.add_argument("--tui-adapter", default="")
    parser.add_argument("--tui-endpoint-id", default="")
    parser.add_argument("--tui-session-id", default="")
    parser.add_argument(
        "--tui-access-mode",
        default="",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--tui-capability", action="append", default=[])
    parser.add_argument("--tui-transport-json", default="")
    parser.add_argument("--confirm-tui-binding", action="store_true")
    parser.add_argument("--basic", action="store_true")
    return parser


def main() -> None:
    try:
        result = accept_invitation(build_parser().parse_args())
    except (InvitationCliError, BridgeRemoteError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
