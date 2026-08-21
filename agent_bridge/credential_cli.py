from __future__ import annotations

import argparse
import json
from pathlib import Path

from .http_client import BridgeHttpClient, BridgeRemoteError


class CredentialCliError(RuntimeError):
    pass


def rotate_connector_credential(state_directory: str | Path) -> dict[str, object]:
    state = Path(state_directory).expanduser().resolve()
    manifest_file = state / "connector.json"
    enrollment_file = state / "enrollment.token"
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        enrollment = enrollment_file.read_text(encoding="utf-8").strip()
    except (OSError, TypeError, json.JSONDecodeError) as exc:
        raise CredentialCliError(
            "connector state must contain readable connector.json and enrollment.token"
        ) from exc
    if not isinstance(manifest, dict) or not enrollment:
        raise CredentialCliError("connector state is incomplete")
    connector_id = str(manifest.get("connector_id") or "").strip()
    bridge_url = str(manifest.get("bridge_url") or "").strip()
    if not connector_id or not bridge_url:
        raise CredentialCliError("connector manifest is missing identity or Bridge URL")

    def load_enrollment() -> str:
        return enrollment_file.read_text(encoding="utf-8").strip()

    client = BridgeHttpClient(
        bridge_url,
        trusted_http_host=(
            str(manifest.get("trusted_http_host") or "").strip() or None
        ),
        enrollment_token=enrollment,
        connector_id=connector_id,
        enrollment_token_file=enrollment_file,
        enrollment_token_loader=load_enrollment,
    )
    return client.rotate_enrollment()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Atomically rotate one local Agent Bridge connector enrollment; "
            "the credential itself is never printed"
        )
    )
    parser.add_argument("--state-directory", required=True)
    return parser


def main(argv: list[str] | None = None) -> None:
    try:
        result = rotate_connector_credential(
            build_parser().parse_args(argv).state_directory
        )
    except (CredentialCliError, BridgeRemoteError, OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
