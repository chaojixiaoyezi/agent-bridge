from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
from http.client import HTTPException
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .message_assets import MAX_ATTACHMENT_BYTES, _safe_filename
from .transport_security import (
    TRUSTED_HTTP_HOST_ENV,
    invitation_trusted_http_host,
    validate_bridge_url,
)


MAX_BRIDGE_RESPONSE_BYTES = 16 * 1024 * 1024


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


_BRIDGE_OPENER = build_opener(_RejectRedirects())


def urlopen(request: Request, *, timeout: float):
    """Open one API request without forwarding credentials through redirects."""

    return _BRIDGE_OPENER.open(request, timeout=timeout)


def _response_bytes(response: Any) -> bytes:
    raw = response.read(MAX_BRIDGE_RESPONSE_BYTES + 1)
    if len(raw) > MAX_BRIDGE_RESPONSE_BYTES:
        raise BridgeRemoteError("Agent Bridge response exceeded the safety limit")
    return raw


class BridgeRemoteError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
        error_code: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.error_code = str(error_code or "").strip() or None
        super().__init__(message)


class BridgeHttpClient:
    def __init__(
        self,
        base_url: str,
        *,
        registration_secret: str | None = None,
        enrollment_token: str | None = None,
        connector_id: str | None = None,
        connector_component: str | None = None,
        invitation_token: str | None = None,
        trusted_http_host: str | None = None,
        auto_registration: dict[str, Any] | None = None,
        enrollment_token_file: str | Path | None = None,
        enrollment_token_loader: Callable[[], str | None] | None = None,
    ) -> None:
        normalized_invitation = str(invitation_token or "").strip() or None
        configured_trusted_host = (
            str(
                trusted_http_host
                or os.environ.get(TRUSTED_HTTP_HOST_ENV, "")
            ).strip()
            or None
        )
        if configured_trusted_host is None and normalized_invitation:
            configured_trusted_host = invitation_trusted_http_host(base_url)
        normalized = validate_bridge_url(
            base_url,
            trusted_http_host=configured_trusted_host,
            # This low-level client predates connector transport policy and is
            # also used by explicitly configured legacy deployments. Admission
            # boundaries (invitation preflight and listener startup) enforce the
            # pin; keeping syntax-only validation here avoids disconnecting an
            # already running legacy Agent during a rolling upgrade.
            allow_insecure_http=True,
        )
        self.base_url = normalized
        self.trusted_http_host = configured_trusted_host
        self.registration_secret = str(registration_secret or "").strip() or None
        self.enrollment_token = str(enrollment_token or "").strip() or None
        self.connector_id = str(connector_id or "").strip() or None
        self.connector_component = (
            str(
                connector_component
                or os.environ.get("AGENT_BRIDGE_COMPONENT", "")
            ).strip().lower()
            or None
        )
        self.invitation_token = normalized_invitation
        self.enrollment_token_file = (
            Path(enrollment_token_file).expanduser()
            if enrollment_token_file is not None
            else None
        )
        self.enrollment_token_loader = enrollment_token_loader
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
        self._maybe_rotate_enrollment(payload)
        return payload

    def accept_invitation(
        self,
        *,
        product: str,
        username: str,
        signature: str,
        avatar_key: str = "auto",
        roles: list[str] | None = None,
        capabilities: list[str] | None = None,
        tui_endpoint_id: str | None = None,
        tui_native_session_id: str | None = None,
        tui_access_mode: str = "unknown",
        tui_confirmed: bool = False,
    ) -> dict[str, Any]:
        # v1 callers may still pass this field. It is deliberately not sent:
        # central Bridge authority cannot be derived from a cached TUI mode.
        del tui_access_mode
        if not self.invitation_token:
            raise BridgeRemoteError(
                "AGENT_BRIDGE_INVITATION_TOKEN is required to accept an invitation"
            )
        # Keep legacy low-level clients wire-compatible, but never send an
        # invitation capability until the invitation-specific transport gate
        # has passed. This executes before enrollment generation or HTTP I/O.
        validate_bridge_url(
            self.base_url,
            trusted_http_host=self.trusted_http_host,
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
                "avatar_key": avatar_key,
                "roles": roles or [],
                "capabilities": capabilities or [],
                "enrollment_token": proposed_enrollment,
                "connector_binding_version": 2,
                **(
                    {
                        "tui_endpoint_id": tui_endpoint_id,
                        "tui_native_session_id": tui_native_session_id,
                        "tui_confirmed": tui_confirmed,
                    }
                    if tui_endpoint_id or tui_native_session_id or tui_confirmed
                    else {}
                ),
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
        self.connector_id = str(payload["connector_id"])
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

    def download_attachment(
        self,
        *,
        attachment_id: str,
        destination_path: str | Path,
        overwrite: bool = False,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Download one server-authorized attachment with hash verification."""

        self._ensure_auto_registered()
        previous_token = self.access_token
        try:
            return self._download_attachment_once(
                attachment_id=attachment_id,
                destination_path=destination_path,
                overwrite=overwrite,
                timeout=timeout,
            )
        except BridgeRemoteError as exc:
            if exc.status_code != 401 or self.auto_registration is None:
                raise
            with self._registration_lock:
                if self.access_token == previous_token:
                    self.access_token = None
                    self._register_from_fixed_identity()
            return self._download_attachment_once(
                attachment_id=attachment_id,
                destination_path=destination_path,
                overwrite=overwrite,
                timeout=timeout,
            )

    def _download_attachment_once(
        self,
        *,
        attachment_id: str,
        destination_path: str | Path,
        overwrite: bool,
        timeout: float,
    ) -> dict[str, Any]:
        if not self.access_token:
            raise BridgeRemoteError("call agent_register before using chat tools")
        request = Request(
            f"{self.base_url}/agent/attachments/download",
            data=json.dumps(
                {"attachment_id": str(attachment_id)},
                ensure_ascii=False,
            ).encode("utf-8"),
            headers={
                "Accept": "application/octet-stream",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.access_token}",
                "User-Agent": "agent-bridge-mcp/0.3",
            },
            method="POST",
        )
        try:
            response = urlopen(request, timeout=max(1.0, float(timeout)))
        except HTTPError as exc:
            try:
                raw = _response_bytes(exc)
            except (OSError, HTTPException):
                raw = b""
            try:
                error_payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                error_payload = {}
            raise BridgeRemoteError(
                str(error_payload.get("error") or f"bridge HTTP {exc.code}"),
                status_code=exc.code,
                error_code=error_payload.get("error_code"),
            ) from exc
        except (URLError, OSError, HTTPException) as exc:
            reason = getattr(exc, "reason", None) or str(exc) or type(exc).__name__
            raise BridgeRemoteError(f"cannot reach Agent Bridge: {reason}") from exc

        temporary: Path | None = None
        try:
            encoded_filename = response.headers.get("X-Attachment-Filename-B64", "")
            try:
                original_filename = base64.b64decode(
                    encoded_filename.encode("ascii"),
                    altchars=b"-_",
                    validate=True,
                ).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                original_filename = f"{attachment_id}.bin"
            original_filename = _safe_filename(original_filename)
            raw_destination = str(destination_path).strip()
            if not raw_destination:
                raise BridgeRemoteError("destination_path cannot be empty")
            destination = Path(raw_destination).expanduser()
            if destination.is_dir():
                destination = destination / original_filename
            destination = destination.resolve()
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() and not overwrite:
                raise BridgeRemoteError(
                    f"destination already exists: {destination}; set overwrite=true "
                    "to replace it"
                )
            try:
                declared_size = int(
                    response.headers.get("X-Attachment-Size", "0") or 0
                )
            except (TypeError, ValueError) as exc:
                raise BridgeRemoteError(
                    "attachment size header is invalid"
                ) from exc
            if not 1 <= declared_size <= MAX_ATTACHMENT_BYTES:
                raise BridgeRemoteError("attachment exceeded the safety limit")
            expected_sha256 = response.headers.get("X-Attachment-SHA256", "").lower()
            if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
                raise BridgeRemoteError("attachment hash header is invalid")
            temporary = destination.parent / (
                f".{destination.name}.{secrets.token_hex(8)}.part"
            )
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            digest = hashlib.sha256()
            size = 0
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    while True:
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > MAX_ATTACHMENT_BYTES:
                            raise BridgeRemoteError(
                                "attachment exceeded the safety limit"
                            )
                        digest.update(chunk)
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise
            actual_sha256 = digest.hexdigest()
            if declared_size and size != declared_size:
                raise BridgeRemoteError("attachment size verification failed")
            if expected_sha256 and actual_sha256 != expected_sha256:
                raise BridgeRemoteError("attachment hash verification failed")
            if overwrite:
                os.replace(temporary, destination)
            else:
                try:
                    os.link(temporary, destination)
                except FileExistsError as exc:
                    raise BridgeRemoteError(
                        f"destination already exists: {destination}"
                    ) from exc
                temporary.unlink()
            temporary = None
            try:
                os.chmod(destination, 0o600)
            except OSError:
                pass
            return {
                "attachment_id": str(attachment_id),
                "saved_path": str(destination),
                "filename": original_filename,
                "kind": response.headers.get("X-Attachment-Kind", "file"),
                "media_type": response.headers.get(
                    "X-Attachment-Media-Type",
                    "application/octet-stream",
                ),
                "size_bytes": size,
                "sha256": actual_sha256,
            }
        finally:
            response.close()
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
    def bind_native_session(
        self,
        *,
        connector_id: str,
        tui_endpoint_id: str,
        native_session_id: str,
        process_epoch: str,
        binding_source: str,
        replace_existing_session: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.post(
            "/agent/native/session/bind",
            {
                "connector_id": connector_id,
                "tui_endpoint_id": tui_endpoint_id,
                "native_session_id": native_session_id,
                "process_epoch": process_epoch,
                "binding_source": binding_source,
                "replace_existing_session": bool(replace_existing_session),
                "metadata": metadata or {},
            },
        )

    def heartbeat_native_session(
        self,
        *,
        connector_id: str,
        lease_id: str,
        process_epoch: str,
        state: str = "online",
        active_task_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.post(
            "/agent/native/session/heartbeat",
            {
                "connector_id": connector_id,
                "lease_id": lease_id,
                "process_epoch": process_epoch,
                "state": state,
                "active_task_id": active_task_id,
                "detail": detail or {},
            },
        )

    def end_native_session(
        self,
        *,
        connector_id: str,
        lease_id: str,
        process_epoch: str,
    ) -> dict[str, Any]:
        return self.post(
            "/agent/native/session/end",
            {
                "connector_id": connector_id,
                "lease_id": lease_id,
                "process_epoch": process_epoch,
            },
        )

    def wait_native_channel_event(
        self,
        *,
        connector_id: str,
        lease_id: str,
        process_epoch: str,
        request_id: str,
        route_token: str,
        wait_seconds: float = 30.0,
        limit: int = 20,
    ) -> dict[str, Any]:
        bounded_wait = max(0.0, min(float(wait_seconds), 60.0))
        return self.post(
            "/agent/native/channel/wait",
            {
                "connector_id": connector_id,
                "lease_id": lease_id,
                "process_epoch": process_epoch,
                "request_id": request_id,
                "route_token": route_token,
                "wait_seconds": bounded_wait,
                "limit": limit,
            },
            timeout=bounded_wait + 10.0,
        )

    def receive_native_channel_event(
        self,
        *,
        connector_id: str,
        lease_id: str,
        process_epoch: str,
        event_id: str,
        route_token: str,
        stage: str,
    ) -> dict[str, Any]:
        return self.post(
            "/agent/native/channel/receipt",
            {
                "connector_id": connector_id,
                "lease_id": lease_id,
                "process_epoch": process_epoch,
                "event_id": event_id,
                "route_token": route_token,
                "stage": stage,
            },
        )

    def reply_native_channel_event(
        self,
        *,
        connector_id: str,
        lease_id: str,
        process_epoch: str,
        event_id: str,
        route_token: str,
        message_id: str,
        body: str,
        refs: list[dict[str, Any]] | None = None,
        mentions: list[str] | None = None,
    ) -> dict[str, Any]:
        return self.post(
            "/agent/native/channel/reply",
            {
                "connector_id": connector_id,
                "lease_id": lease_id,
                "process_epoch": process_epoch,
                "event_id": event_id,
                "route_token": route_token,
                "message_id": message_id,
                "body": body,
                "refs": refs,
                "mentions": mentions,
            },
        )

    def send_native_channel_event(
        self,
        *,
        connector_id: str,
        lease_id: str,
        process_epoch: str,
        event_id: str,
        route_token: str,
        body: str,
        mentions: list[str] | None = None,
        notification_mode: str | None = None,
    ) -> dict[str, Any]:
        return self.post(
            "/agent/native/channel/send",
            {
                "connector_id": connector_id,
                "lease_id": lease_id,
                "process_epoch": process_epoch,
                "event_id": event_id,
                "route_token": route_token,
                "body": body,
                "mentions": mentions,
                "notification_mode": notification_mode,
            },
        )

    def _ensure_auto_registered(self) -> None:
        if self.access_token is not None or self.auto_registration is None:
            return
        with self._registration_lock:
            if self.access_token is None:
                self._register_from_fixed_identity()

    def _register_from_fixed_identity(self) -> None:
        if self.auto_registration is None:
            return
        self._reload_enrollment_token()
        self.register(**self.auto_registration)

    def rotate_enrollment(self) -> dict[str, Any]:
        self._reload_enrollment_token()
        if not self.enrollment_token or not self.connector_id:
            raise BridgeRemoteError(
                "connector enrollment and connector id are required for rotation"
            )
        if self.enrollment_token_file is None:
            raise BridgeRemoteError(
                "automatic rotation requires AGENT_BRIDGE_ENROLLMENT_TOKEN_FILE"
            )
        pending_file = self.enrollment_token_file.with_name(
            f".{self.enrollment_token_file.name}.pending"
        )
        successor = _load_or_create_pending_enrollment(pending_file)
        response = self._post(
            "/agent/connector/enrollment/rotate",
            {"new_enrollment_token": successor},
            authenticated=False,
        )
        connector = response.get("connector")
        if not isinstance(connector, dict) or not secrets.compare_digest(
            str(connector.get("connector_id") or ""),
            self.connector_id,
        ):
            raise BridgeRemoteError("bridge returned mismatched connector rotation")
        _atomic_private_secret_write(self.enrollment_token_file, successor)
        self.enrollment_token = successor
        try:
            pending_file.unlink()
        except FileNotFoundError:
            pass
        enrollment = connector.get("enrollment")
        if not isinstance(enrollment, dict):
            enrollment = {}
        return {
            "connector_id": self.connector_id,
            "credential_version": int(enrollment.get("credential_version", 1)),
            "rotation_completed": bool(connector.get("rotation_completed")),
        }

    def _reload_enrollment_token(self) -> None:
        if self.enrollment_token_loader is None:
            return
        value = str(self.enrollment_token_loader() or "").strip()
        if value:
            self.enrollment_token = value

    def _maybe_rotate_enrollment(self, registration: dict[str, Any]) -> None:
        if not bool(registration.get("enrollment_rotation_required")):
            return
        if self.enrollment_token_file is None:
            registration["enrollment_rotation_pending"] = True
            return
        try:
            result = self.rotate_enrollment()
        except (BridgeRemoteError, OSError, RuntimeError):
            # Keep the active Agent session and a private pending successor.
            # The next fixed-identity registration retries the same rotation.
            registration["enrollment_rotation_pending"] = True
            return
        registration["enrollment_rotation_required"] = False
        registration["enrollment_rotation_pending"] = False
        registration["enrollment_credential_version"] = result[
            "credential_version"
        ]

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
        elif path in {
            "/agent/register",
            "/agent/connector/enrollment/rotate",
        }:
            if self.enrollment_token:
                headers["X-Agent-Bridge-Enrollment"] = self.enrollment_token
                if self.connector_id:
                    headers["X-Agent-Bridge-Connector"] = self.connector_id
                    if self.connector_component and path == "/agent/register":
                        headers["X-Agent-Bridge-Component"] = self.connector_component
                        headers["X-Agent-Bridge-Protocol"] = "2"
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
                raw = _response_bytes(response)
        except HTTPError as exc:
            try:
                raw = _response_bytes(exc)
            except (OSError, HTTPException):
                # Some servers close an empty redirect/error response before
                # urllib finishes reading its synthetic HTTPError body. Keep
                # the authoritative HTTP status instead of misreporting that
                # secondary socket reset as a transport outage.
                raw = b""
            try:
                error_payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                error_payload = {}
            raise BridgeRemoteError(
                str(error_payload.get("error") or f"bridge HTTP {exc.code}"),
                status_code=exc.code,
                retry_after_seconds=error_payload.get("retry_after_seconds"),
                error_code=error_payload.get("error_code"),
            ) from exc
        except (URLError, OSError, HTTPException) as exc:
            reason = getattr(exc, "reason", None) or str(exc) or type(exc).__name__
            raise BridgeRemoteError(f"cannot reach Agent Bridge: {reason}") from exc
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BridgeRemoteError("Agent Bridge returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise BridgeRemoteError("Agent Bridge returned a non-object response")
        return result


def _load_or_create_pending_enrollment(path: Path) -> str:
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existing = ""
    except OSError as exc:
        raise BridgeRemoteError("cannot read pending enrollment rotation") from exc
    if existing:
        return existing
    candidate = f"enroll_{secrets.token_urlsafe(32)}"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        return _load_or_create_pending_enrollment(path)
    try:
        os.write(descriptor, f"{candidate}\n".encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return candidate


def _atomic_private_secret_write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        os.write(descriptor, f"{value}\n".encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
