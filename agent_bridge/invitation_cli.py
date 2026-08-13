from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from pathlib import Path
from urllib.parse import urlparse

from .connector import ConnectorSetupError, configure_resident_connector
from .http_client import BridgeHttpClient, BridgeRemoteError
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


def _supported_local_bridge_url(value: str) -> str:
    normalized = str(value or "").strip().rstrip("/")
    parsed = urlparse(normalized)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise InvitationCliError(
            "direct invitation acceptance currently requires the local Agent Bridge"
        )
    return normalized


def accept_invitation(args: argparse.Namespace) -> dict[str, object]:
    invitation_token = _stdin_invitation_token()
    bridge_url = _supported_local_bridge_url(args.bridge_url)
    product = token(args.product, field="product_name")
    username = agent_username(args.username)
    signature = alias(args.signature, field="signature")
    roles = string_tokens(args.role, field="roles")
    capabilities = string_tokens(args.capability, field="capabilities")
    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        raise InvitationCliError("Agent workspace does not exist")
    client = BridgeHttpClient(
        bridge_url,
        invitation_token=invitation_token,
    )
    accepted = client.accept_invitation(
        product=product,
        username=username,
        signature=signature,
        roles=roles,
        capabilities=capabilities,
    )
    enrollment_token = str(accepted.pop("_enrollment_token", ""))
    connector_id = str(accepted["connector_id"])
    try:
        setup = configure_resident_connector(
            connector_id=connector_id,
            enrollment_token=enrollment_token,
            bridge_url=bridge_url,
            product=product,
            username=username,
            signature=signature,
            conversation_id=str(accepted["conversation_id"]),
            adapter_kind=str(accepted["adapter_kind"]),
            requested_mode=str(accepted["requested_mode"]),
            roles=roles,
            capabilities=capabilities,
            workspace_path=str(workspace),
            enable_resident=not args.basic,
        )
        setup_payload = setup.public_payload()
    except (ConnectorSetupError, OSError) as exc:
        setup_payload = {
            "status": "failed",
            "platform": platform.system() or "unknown",
            "adapter_kind": str(accepted["adapter_kind"]),
            "connector_id": connector_id,
            "state_directory": "",
            "listener_service": None,
            "worker_service": None,
            "task_service": None,
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
    return {
        "invitation_accepted": True,
        "invitation_consumed": not bool(accepted.get("invitation_reusable", False)),
        "participant_id": accepted.get("participant_id"),
        "conversation_id": accepted.get("conversation_id"),
        "resident_setup": setup_payload,
        "connector": connector,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Accept one Agent Bridge invitation without configuring MCP"
    )
    parser.add_argument("--bridge-url", required=True)
    parser.add_argument("--product", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--signature", required=True)
    parser.add_argument("--workspace", default=os.getcwd())
    parser.add_argument("--role", action="append", default=[])
    parser.add_argument("--capability", action="append", default=[])
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
