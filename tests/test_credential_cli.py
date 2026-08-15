from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_bridge.credential_cli import CredentialCliError, rotate_connector_credential
from agent_bridge.http_client import BridgeHttpClient


def test_credential_cli_uses_private_connector_state_without_returning_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "connector_state"
    state.mkdir()
    secret = "enroll_" + "q" * 64
    (state / "connector.json").write_text(
        json.dumps(
            {
                "connector_id": "connector_local_rotation",
                "bridge_url": "https://bridge.example.test",
            }
        ),
        encoding="utf-8",
    )
    (state / "enrollment.token").write_text(f"{secret}\n", encoding="utf-8")
    captured: dict[str, object] = {}

    def rotate(self: BridgeHttpClient) -> dict[str, object]:
        captured["connector_id"] = self.connector_id
        captured["enrollment_file"] = self.enrollment_token_file
        captured["loaded"] = self.enrollment_token_loader()
        return {
            "connector_id": "connector_local_rotation",
            "credential_version": 3,
            "rotation_completed": True,
        }

    monkeypatch.setattr(BridgeHttpClient, "rotate_enrollment", rotate)
    result = rotate_connector_credential(state)
    assert result["credential_version"] == 3
    assert captured == {
        "connector_id": "connector_local_rotation",
        "enrollment_file": state / "enrollment.token",
        "loaded": secret,
    }
    assert secret not in str(result)


def test_credential_cli_rejects_incomplete_state(tmp_path: Path) -> None:
    with pytest.raises(CredentialCliError, match="connector state"):
        rotate_connector_credential(tmp_path)
