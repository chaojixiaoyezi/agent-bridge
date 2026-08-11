from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class BridgeRemoteError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message)


class BridgeHttpClient:
    def __init__(self, base_url: str) -> None:
        normalized = str(base_url or "").strip().rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("AGENT_BRIDGE_URL must be an http(s) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("AGENT_BRIDGE_URL cannot contain credentials or query data")
        self.base_url = normalized
        self.access_token: str | None = None
        self.participant_id: str | None = None
        self.session_id: str | None = None

    def register(
        self,
        *,
        product: str,
        username: str,
        session_alias: str | None = None,
        signature: str | None = None,
        conversation_id: str,
        roles: list[str] | None = None,
        capabilities: list[str] | None = None,
    ) -> dict[str, Any]:
        registration = {
            "product": product,
            "username": username,
            "conversation_id": conversation_id,
            "roles": roles or [],
            "capabilities": capabilities or [],
        }
        if session_alias:
            registration["session_alias"] = session_alias
        if signature:
            registration["signature"] = signature
        payload = self._post(
            "/agent/register",
            registration,
            authenticated=False,
        )
        token = str(payload.pop("access_token", ""))
        if not token:
            raise BridgeRemoteError("bridge registration did not return a session token")
        self.access_token = token
        self.participant_id = str(payload["participant_id"])
        self.session_id = str(payload["session_id"])
        return payload

    def post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        return self._post(path, payload, authenticated=True, timeout=timeout)

    def _post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        authenticated: bool,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "agent-bridge-mcp/0.2",
        }
        if authenticated:
            if not self.access_token:
                raise BridgeRemoteError("call agent_register before using chat tools")
            headers["Authorization"] = f"Bearer {self.access_token}"
        request = Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=max(1.0, float(timeout))) as response:
                raw = response.read()
        except HTTPError as exc:
            raw = exc.read()
            try:
                error_payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                error_payload = {}
            raise BridgeRemoteError(
                str(error_payload.get("error") or f"bridge HTTP {exc.code}"),
                status_code=exc.code,
                retry_after_seconds=error_payload.get("retry_after_seconds"),
            ) from exc
        except URLError as exc:
            raise BridgeRemoteError(f"cannot reach Agent Bridge: {exc.reason}") from exc
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BridgeRemoteError("Agent Bridge returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise BridgeRemoteError("Agent Bridge returned a non-object response")
        return result
