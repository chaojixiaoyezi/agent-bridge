from __future__ import annotations

import json
import secrets
import threading
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
    def __init__(
        self,
        base_url: str,
        *,
        registration_secret: str | None = None,
        enrollment_token: str | None = None,
        invitation_token: str | None = None,
        auto_registration: dict[str, Any] | None = None,
    ) -> None:
        normalized = str(base_url or "").strip().rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("AGENT_BRIDGE_URL must be an http(s) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "AGENT_BRIDGE_URL cannot contain credentials or query data"
            )
        self.base_url = normalized
        self.registration_secret = str(registration_secret or "").strip() or None
        self.enrollment_token = str(enrollment_token or "").strip() or None
        self.invitation_token = str(invitation_token or "").strip() or None
        self.auto_registration = (
            dict(auto_registration) if auto_registration is not None else None
        )
        self._registration_lock = threading.Lock()
        self.access_token: str | None = None
        self.participant_id: str | None = None
        self.session_id: str | None = None
        self._invitation_enrollment_token: str | None = None

    @staticmethod
    def _consume_session_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
        result = dict(payload)
        access_token = str(result.pop("access_token", ""))
        if not access_token:
            raise BridgeRemoteError("bridge did not return an Agent session token")
        return result, access_token

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
        payload, access_token = self._consume_session_payload(payload)
        self.access_token = access_token
        self.participant_id = str(payload["participant_id"])
        self.session_id = str(payload["session_id"])
        return payload

    def accept_invitation(
        self,
        *,
        product: str,
        username: str,
        signature: str,
        roles: list[str] | None = None,
        capabilities: list[str] | None = None,
    ) -> dict[str, Any]:
        if not self.invitation_token:
            raise BridgeRemoteError(
                "AGENT_BRIDGE_INVITATION_TOKEN is required to accept an invitation"
            )
        proposed_enrollment = self._invitation_enrollment_token
        if proposed_enrollment is None:
            proposed_enrollment = f"enroll_{secrets.token_urlsafe(32)}"
            self._invitation_enrollment_token = proposed_enrollment
        payload = self._post(
            "/agent/invitations/accept",
            {
                "product": product,
                "username": username,
                "signature": signature,
                "roles": roles or [],
                "capabilities": capabilities or [],
                "enrollment_token": proposed_enrollment,
            },
            authenticated=False,
        )
        payload, access_token = self._consume_session_payload(payload)
        returned_enrollment = str(payload.pop("enrollment_token", ""))
        if not returned_enrollment:
            raise BridgeRemoteError(
                "invitation acceptance did not return enrollment authority"
            )
        if not secrets.compare_digest(returned_enrollment, proposed_enrollment):
            raise BridgeRemoteError("bridge returned mismatched enrollment authority")
        self.access_token = access_token
        self.enrollment_token = proposed_enrollment
        self.participant_id = str(payload["participant_id"])
        self.session_id = str(payload["session_id"])
        payload["_enrollment_token"] = proposed_enrollment
        return payload

    def post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        self._ensure_auto_registered()
        previous_token = self.access_token
        try:
            return self._post(path, payload, authenticated=True, timeout=timeout)
        except BridgeRemoteError as exc:
            if exc.status_code != 401 or self.auto_registration is None:
                raise
            # A long-lived resident MCP can outlive its short Agent session.
            # Re-enroll once and retry the original authenticated request; no
            # session credential is written to argv, disk, or model context.
            with self._registration_lock:
                if self.access_token == previous_token:
                    self.access_token = None
                    self._register_from_fixed_identity()
            return self._post(path, payload, authenticated=True, timeout=timeout)

    def _ensure_auto_registered(self) -> None:
        if self.access_token is not None or self.auto_registration is None:
            return
        with self._registration_lock:
            if self.access_token is None:
                self._register_from_fixed_identity()

    def _register_from_fixed_identity(self) -> None:
        if self.auto_registration is None:
            return
        self.register(**self.auto_registration)

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
            "User-Agent": "agent-bridge-mcp/0.3",
        }
        if authenticated:
            if not self.access_token:
                raise BridgeRemoteError("call agent_register before using chat tools")
            headers["Authorization"] = f"Bearer {self.access_token}"
        elif path == "/agent/register":
            if self.enrollment_token:
                headers["X-Agent-Bridge-Enrollment"] = self.enrollment_token
            elif self.registration_secret:
                headers["X-Agent-Bridge-Registration"] = self.registration_secret
        elif path == "/agent/invitations/accept" and self.invitation_token:
            headers["X-Agent-Bridge-Invitation"] = self.invitation_token
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
