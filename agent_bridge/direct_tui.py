"""Exact-TUI duty routing for invitation-bound Codex conversations.

The accepting Codex thread owns delivery through its MCP process.  This module
never scans Codex transcripts, resumes a second app-server writer, or launches a
shadow model.  A private connector manifest must explicitly name the exact
request thread before any credential is loaded.
"""

from __future__ import annotations

import atexit
import json
import os
import platform
import secrets
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .connector_contracts import _state_root
from .http_client import BridgeHttpClient, BridgeRemoteError
from .tui_binding import CODEX_THREAD_ID_PATTERN


DIRECT_TUI_DUTY_MODE = "direct_tui"
DIRECT_TUI_HEARTBEAT_SECONDS = 20.0
DIRECT_TUI_ROOM_SLICE_SECONDS = 2.0
DIRECT_TUI_TASK_RENEW_SECONDS = 120.0
NATIVE_EVENT_TERMINAL_STATES = {"replied", "superseded", "cancelled"}


class DirectTuiError(RuntimeError):
    pass


def normalized_thread_id(value: object) -> str:
    thread_id = str(value or "").strip().casefold()
    if CODEX_THREAD_ID_PATTERN.fullmatch(thread_id) is None:
        return ""
    return thread_id


def _read_private_file(path: Path, *, label: str) -> str:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise DirectTuiError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise DirectTuiError(f"{label} must be a regular private file")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise DirectTuiError(f"{label} belongs to another user")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise DirectTuiError(f"{label} permissions are too broad")
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise DirectTuiError(f"{label} is unreadable") from exc
    if not value:
        raise DirectTuiError(f"{label} is empty")
    return value


@dataclass
class DirectTuiConnection:
    thread_id: str
    connector_id: str
    conversation_id: str
    endpoint_id: str
    workspace_path: str
    manifest_file: Path
    client: BridgeHttpClient
    process_epoch: str
    lease_id: str | None = None
    desired_state: str = "online"
    active_task_id: str | None = None
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _heartbeat_thread: threading.Thread | None = field(default=None, repr=False)
    _last_task_renewed_at: float = field(default=0.0, repr=False)
    _native_request_id: str | None = field(default=None, repr=False)
    _native_route_token: str | None = field(default=None, repr=False)
    _native_event_id: str | None = field(default=None, repr=False)
    _native_event_message_ids: set[str] = field(default_factory=set, repr=False)
    _native_event_delivered: bool = field(default=False, repr=False)

    def _clear_native_event_locked(self) -> None:
        self._native_request_id = None
        self._native_route_token = None
        self._native_event_id = None
        self._native_event_message_ids.clear()
        self._native_event_delivered = False

    def _reset_stale_native_lease(
        self,
        exc: BridgeRemoteError,
        *,
        expected_lease_id: str | None = None,
    ) -> bool:
        if exc.error_code not in {
            "native_session_lease_ended",
            "native_session_lease_expired",
            "native_session_lease_superseded",
            "native_delivery_inactive",
        }:
            return False
        with self._lock:
            if expected_lease_id and self.lease_id != expected_lease_id:
                return True
            self.lease_id = None
            self._clear_native_event_locked()
        return True

    def ensure_bound(self) -> None:
        with self._lock:
            if self.lease_id:
                return
            result = self.client.bind_native_session(
                connector_id=self.connector_id,
                tui_endpoint_id=self.endpoint_id,
                native_session_id=self.thread_id,
                process_epoch=self.process_epoch,
                binding_source="resume",
                metadata={
                    "duty_mode": DIRECT_TUI_DUTY_MODE,
                    "mcp_process_id": os.getpid(),
                    "workspace_path": self.workspace_path,
                },
            )
            lease = result.get("lease")
            if not isinstance(lease, dict) or not str(lease.get("lease_id") or ""):
                raise DirectTuiError("Bridge did not return a native TUI lease")
            self.lease_id = str(lease["lease_id"])
            self._clear_native_event_locked()
            self._stop.clear()
            self._start_heartbeat_locked()

    def _start_heartbeat_locked(self) -> None:
        if self._heartbeat_thread is not None and self._heartbeat_thread.is_alive():
            return
        thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"agent-bridge-direct-{self.connector_id[-8:]}",
            daemon=True,
        )
        self._heartbeat_thread = thread
        thread.start()

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(DIRECT_TUI_HEARTBEAT_SECONDS):
            with self._lock:
                lease_id = self.lease_id
                state = self.desired_state
                task_id = self.active_task_id
            if not lease_id:
                return
            try:
                self.client.heartbeat_native_session(
                    connector_id=self.connector_id,
                    lease_id=lease_id,
                    process_epoch=self.process_epoch,
                    state=state,
                    active_task_id=task_id,
                    detail={
                        "duty_mode": DIRECT_TUI_DUTY_MODE,
                        "mcp_process_id": os.getpid(),
                    },
                )
                if (
                    task_id
                    and time.monotonic() - self._last_task_renewed_at
                    >= DIRECT_TUI_TASK_RENEW_SECONDS
                ):
                    self.renew_task(task_id)
            except BridgeRemoteError as exc:
                if self._reset_stale_native_lease(
                    exc,
                    expected_lease_id=lease_id,
                ):
                    return
                # A transient transport outage keeps the same fenced lease. An
                # explicit later agent_duty call may rebind after it expires.
            except (OSError, RuntimeError):
                continue

    def set_state(self, state: str, *, active_task_id: str | None = None) -> None:
        with self._lock:
            self.desired_state = state
            self.active_task_id = active_task_id
            if active_task_id is None:
                self._last_task_renewed_at = 0.0
            lease_id = self.lease_id
        if not lease_id:
            return
        try:
            self.client.heartbeat_native_session(
                connector_id=self.connector_id,
                lease_id=lease_id,
                process_epoch=self.process_epoch,
                state=state,
                active_task_id=active_task_id,
                detail={"duty_mode": DIRECT_TUI_DUTY_MODE},
            )
        except BridgeRemoteError as exc:
            self._reset_stale_native_lease(
                exc,
                expected_lease_id=lease_id,
            )

    @staticmethod
    def _remaining_required(event: dict[str, Any]) -> int:
        raw = event.get("required_message_ids")
        if isinstance(raw, list):
            return len([item for item in raw if str(item or "")])
        return max(0, int(event.get("required_reply_count") or 0))

    def _event_route(self) -> tuple[str, str, str, str] | None:
        with self._lock:
            if not all(
                (
                    self.lease_id,
                    self._native_request_id,
                    self._native_route_token,
                    self._native_event_id,
                )
            ):
                return None
            return (
                str(self.lease_id),
                str(self._native_request_id),
                str(self._native_route_token),
                str(self._native_event_id),
            )

    def apply_delivered_event(self) -> dict[str, Any] | None:
        """Acknowledge one batch only when this TUI asks to re-arm.

        Returning a tool result is the injection boundary.  The next explicit
        agent_duty call proves that the model had a chance to apply that batch;
        optional messages can then be acknowledged while required replies stay
        pending and are redelivered under the same idempotent event.
        """

        with self._lock:
            route = self._event_route()
            delivered = self._native_event_delivered
        if route is None or not delivered:
            return None
        lease_id, _request_id, route_token, event_id = route
        try:
            result = self.client.receive_native_channel_event(
                connector_id=self.connector_id,
                lease_id=lease_id,
                process_epoch=self.process_epoch,
                event_id=event_id,
                route_token=route_token,
                stage="applied",
            )
        except BridgeRemoteError as exc:
            if self._reset_stale_native_lease(
                exc,
                expected_lease_id=lease_id,
            ):
                return None
            raise
        event = result.get("event")
        with self._lock:
            self._native_event_delivered = False
            if isinstance(event, dict) and (
                str(event.get("state") or "") in NATIVE_EVENT_TERMINAL_STATES
                or self._remaining_required(event) == 0
            ):
                self._clear_native_event_locked()
        return result

    def wait_native_event(
        self,
        *,
        wait_seconds: float,
        limit: int,
    ) -> dict[str, Any]:
        result: dict[str, Any] | None = None
        lease_id = ""
        route_token = ""
        for attempt in range(2):
            self.ensure_bound()
            with self._lock:
                if self._native_request_id is None:
                    self._native_request_id = (
                        "request_direct_" + secrets.token_hex(20)
                    )
                    self._native_route_token = "route_" + secrets.token_hex(32)
                lease_id = str(self.lease_id or "")
                request_id = str(self._native_request_id)
                route_token = str(self._native_route_token)
            try:
                result = self.client.wait_native_channel_event(
                    connector_id=self.connector_id,
                    lease_id=lease_id,
                    process_epoch=self.process_epoch,
                    request_id=request_id,
                    route_token=route_token,
                    wait_seconds=wait_seconds,
                    limit=limit,
                )
                candidate = result.get("event")
                if (
                    isinstance(candidate, dict)
                    and str(candidate.get("state") or "") == "fetched"
                ):
                    injected = self.client.receive_native_channel_event(
                        connector_id=self.connector_id,
                        lease_id=lease_id,
                        process_epoch=self.process_epoch,
                        event_id=str(candidate.get("event_id") or ""),
                        route_token=route_token,
                        stage="injected",
                    )
                    injected_event = injected.get("event")
                    if isinstance(injected_event, dict):
                        candidate = {**candidate, **injected_event}
                        result = {**result, "event": candidate}
                break
            except BridgeRemoteError as exc:
                stale = self._reset_stale_native_lease(
                    exc,
                    expected_lease_id=lease_id,
                )
                if not stale or attempt == 1:
                    raise
        if result is None:
            raise DirectTuiError("Bridge returned no native event result")
        event = result.get("event")
        if not isinstance(event, dict):
            return result
        state = str(event.get("state") or "")
        if state in NATIVE_EVENT_TERMINAL_STATES or (
            state == "applied" and self._remaining_required(event) == 0
        ):
            with self._lock:
                self._clear_native_event_locked()
            return {**result, "event": None, "timed_out": True}
        event_id = str(event.get("event_id") or "")
        if not event_id:
            raise DirectTuiError("Bridge returned a native event without an id")
        message_ids = {
            str(item)
            for item in (event.get("message_ids") or [])
            if str(item or "")
        }
        with self._lock:
            self._native_event_id = event_id
            self._native_event_message_ids = message_ids
            self._native_event_delivered = True
        return {**result, "event": event, "timed_out": False}

    def reply_native_message(
        self,
        *,
        message_id: str,
        body: str,
        refs: list[dict[str, Any]] | None,
        mentions: list[str] | None,
    ) -> dict[str, Any] | None:
        with self._lock:
            route = self._event_route()
            belongs = message_id in self._native_event_message_ids
        if route is None or not belongs:
            return None
        lease_id, _request_id, route_token, event_id = route
        try:
            result = self.client.reply_native_channel_event(
                connector_id=self.connector_id,
                lease_id=lease_id,
                process_epoch=self.process_epoch,
                event_id=event_id,
                route_token=route_token,
                message_id=message_id,
                body=body,
                refs=refs,
                mentions=mentions,
            )
        except BridgeRemoteError as exc:
            if self._reset_stale_native_lease(
                exc,
                expected_lease_id=lease_id,
            ):
                return None
            raise
        native_event = result.get("native_event")
        with self._lock:
            if isinstance(native_event, dict) and (
                str(native_event.get("state") or "")
                in NATIVE_EVENT_TERMINAL_STATES
                or self._remaining_required(native_event) == 0
            ):
                self._clear_native_event_locked()
        return result

    def renew_task(self, task_id: str) -> None:
        self.client.post(
            "/agent/tasks/update",
            {
                "task_id": task_id,
                "status": "running",
                "result_summary": "",
                "execution_cwd": self.workspace_path,
                "execution_thread_id": self.thread_id,
            },
        )
        with self._lock:
            if self.active_task_id == task_id:
                self._last_task_renewed_at = time.monotonic()

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            lease_id = self.lease_id
            self.lease_id = None
            self._clear_native_event_locked()
        if lease_id:
            try:
                self.client.end_native_session(
                    connector_id=self.connector_id,
                    lease_id=lease_id,
                    process_epoch=self.process_epoch,
                )
            except (BridgeRemoteError, OSError, RuntimeError):
                pass


class DirectTuiRegistry:
    def __init__(
        self,
        *,
        home: Path | None = None,
        system_name: str | None = None,
    ) -> None:
        user_home = (home or Path.home()).expanduser().resolve()
        self.root = _state_root(user_home, system_name or platform.system())
        self._lock = threading.RLock()
        self._connections: dict[tuple[str, str], DirectTuiConnection] = {}
        self._active_connector: dict[str, str] = {}
        self._message_routes: dict[tuple[str, str], str] = {}
        self._task_routes: dict[tuple[str, str], str] = {}
        self._process_nonce = secrets.token_hex(12)

    def _manifest_connection(
        self,
        *,
        manifest_file: Path,
        thread_id: str,
    ) -> DirectTuiConnection | None:
        state_directory = manifest_file.parent
        if state_directory.is_symlink() or manifest_file.is_symlink():
            raise DirectTuiError("direct TUI connector paths cannot be symbolic links")
        raw = _read_private_file(manifest_file, label="connector manifest")
        try:
            manifest = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise DirectTuiError("connector manifest is invalid") from exc
        if not isinstance(manifest, dict):
            raise DirectTuiError("connector manifest is invalid")
        if str(manifest.get("duty_mode") or "") != DIRECT_TUI_DUTY_MODE:
            return None
        if str(manifest.get("product") or "").casefold() != "codex":
            return None
        source_thread = normalized_thread_id(
            manifest.get("execution_source_thread_id")
        )
        native_session = normalized_thread_id(manifest.get("tui_native_session_id"))
        if source_thread != thread_id or native_session != thread_id:
            return None
        connector_id = str(manifest.get("connector_id") or "").strip()
        if not connector_id or state_directory.name != connector_id:
            raise DirectTuiError("connector manifest identity does not match its directory")
        enrollment_file = state_directory / "enrollment.token"
        configured_enrollment = Path(
            str(manifest.get("enrollment_token_file") or "")
        ).expanduser()
        if configured_enrollment.resolve() != enrollment_file.resolve():
            raise DirectTuiError("connector enrollment file escaped its private directory")
        enrollment_token = _read_private_file(
            enrollment_file,
            label="connector enrollment credential",
        )
        conversation_id = str(manifest.get("conversation_id") or "").strip()
        endpoint_id = str(manifest.get("tui_endpoint_id") or "").strip()
        username = str(manifest.get("username") or "").strip()
        signature = str(manifest.get("signature") or "").strip()
        bridge_url = str(manifest.get("bridge_url") or "").strip()
        if not all((conversation_id, endpoint_id, username, signature, bridge_url)):
            raise DirectTuiError("direct TUI connector manifest is incomplete")

        def enrollment_loader(path: Path = enrollment_file) -> str:
            return _read_private_file(path, label="connector enrollment credential")

        client = BridgeHttpClient(
            bridge_url,
            trusted_http_host=(
                str(manifest.get("trusted_http_host") or "").strip() or None
            ),
            enrollment_token=enrollment_token,
            connector_id=connector_id,
            connector_component="mcp",
            enrollment_token_file=enrollment_file,
            enrollment_token_loader=enrollment_loader,
            auto_registration={
                "product": "codex",
                "username": username,
                "signature": signature,
                "conversation_id": conversation_id,
                "roles": list(manifest.get("roles") or []),
                "capabilities": list(manifest.get("capabilities") or []),
            },
        )
        return DirectTuiConnection(
            thread_id=thread_id,
            connector_id=connector_id,
            conversation_id=conversation_id,
            endpoint_id=endpoint_id,
            workspace_path=str(manifest.get("workspace_path") or ""),
            manifest_file=manifest_file,
            client=client,
            process_epoch=f"directtui_{os.getpid()}_{self._process_nonce}",
        )

    def connections_for_thread(
        self,
        thread_id: str,
        *,
        required: bool = False,
    ) -> list[DirectTuiConnection]:
        normalized = normalized_thread_id(thread_id)
        if not normalized:
            if required:
                raise DirectTuiError(
                    "agent_duty must be called by the exact Codex TUI that accepted "
                    "the invitation"
                )
            return []
        discovered: list[DirectTuiConnection] = []
        if self.root.is_dir():
            for manifest_file in sorted(self.root.glob("connector_*/connector.json")):
                candidate = self._manifest_connection(
                    manifest_file=manifest_file,
                    thread_id=normalized,
                )
                if candidate is None:
                    continue
                key = (normalized, candidate.connector_id)
                with self._lock:
                    existing = self._connections.get(key)
                    if existing is None:
                        self._connections[key] = candidate
                        existing = candidate
                discovered.append(existing)
        if required and not discovered:
            raise DirectTuiError(
                "this Codex TUI has no exact direct-duty connector; accept an "
                "invitation in this TUI before calling agent_duty"
            )
        return discovered

    def client_for(
        self,
        *,
        thread_id: str,
        conversation_id: str | None = None,
        resource_id: str | None = None,
        required: bool = False,
    ) -> BridgeHttpClient | None:
        normalized = normalized_thread_id(thread_id)
        connections = self.connections_for_thread(normalized, required=required)
        if not connections:
            return None
        selected: DirectTuiConnection | None = None
        conversation = str(conversation_id or "").strip()
        resource = str(resource_id or "").strip()
        if conversation:
            selected = next(
                (item for item in connections if item.conversation_id == conversation),
                None,
            )
            if selected is None:
                raise DirectTuiError(
                    "the requested room is not bound to this exact Codex TUI"
                )
        elif resource:
            with self._lock:
                connector_id = self._message_routes.get((normalized, resource))
                connector_id = connector_id or self._task_routes.get(
                    (normalized, resource)
                )
            selected = next(
                (item for item in connections if item.connector_id == connector_id),
                None,
            )
        if selected is None:
            with self._lock:
                active = self._active_connector.get(normalized)
            selected = next(
                (item for item in connections if item.connector_id == active),
                None,
            )
        if selected is None and len(connections) == 1:
            selected = connections[0]
        if selected is None:
            raise DirectTuiError(
                "this TUI serves multiple rooms; call agent_duty or provide a "
                "conversation_id before using this tool"
            )
        selected.ensure_bound()
        with self._lock:
            self._active_connector[normalized] = selected.connector_id
        return selected.client

    def _record_payload(
        self,
        *,
        thread_id: str,
        connection: DirectTuiConnection,
        payload: dict[str, Any],
    ) -> None:
        with self._lock:
            self._active_connector[thread_id] = connection.connector_id
            for message in payload.get("messages") or []:
                if isinstance(message, dict) and str(message.get("message_id") or ""):
                    self._message_routes[
                        (thread_id, str(message["message_id"]))
                    ] = connection.connector_id
                    for attachment in message.get("attachments") or []:
                        if isinstance(attachment, dict) and str(
                            attachment.get("attachment_id") or ""
                        ):
                            self._message_routes[
                                (thread_id, str(attachment["attachment_id"]))
                            ] = connection.connector_id
            task = payload.get("task")
            if isinstance(task, dict) and str(task.get("task_id") or ""):
                self._task_routes[(thread_id, str(task["task_id"]))] = (
                    connection.connector_id
                )
            direct_task_id = str(payload.get("task_id") or "")
            if direct_task_id:
                self._task_routes[(thread_id, direct_task_id)] = (
                    connection.connector_id
                )

    def duty(
        self,
        *,
        thread_id: str,
        wait_seconds: float,
        limit: int,
    ) -> dict[str, Any]:
        normalized = normalized_thread_id(thread_id)
        connections = self.connections_for_thread(normalized, required=True)
        for connection in connections:
            connection.ensure_bound()
            connection.apply_delivered_event()
            with connection._lock:
                active_task_id = connection.active_task_id
            connection.set_state(
                "busy" if active_task_id else "online",
                active_task_id=active_task_id,
            )

        for connection in connections:
            with connection._lock:
                active_task_id = connection.active_task_id
            if not active_task_id:
                continue
            input_payload = connection.client.post(
                "/agent/tasks/inputs",
                {
                    "task_id": active_task_id,
                    "action": "poll",
                    "limit": 50,
                },
            )
            if input_payload.get("inputs"):
                self._record_payload(
                    thread_id=normalized,
                    connection=connection,
                    payload=input_payload,
                )
                connection.set_state("busy", active_task_id=active_task_id)
                return {
                    "kind": "task_inputs",
                    "conversation_id": connection.conversation_id,
                    "connector_id": connection.connector_id,
                    **input_payload,
                    "next_action": (
                        "apply these structured inputs to the active task, "
                        "acknowledge them with agent_task_inputs, then call "
                        "agent_duty once to re-arm the event subscription"
                    ),
                }

        for connection in connections:
            with connection._lock:
                if connection.active_task_id:
                    continue
            task_payload = connection.client.post(
                "/agent/tasks/next",
                {"wait_seconds": 0},
            )
            task = task_payload.get("task")
            if isinstance(task, dict):
                self._record_payload(
                    thread_id=normalized,
                    connection=connection,
                    payload=task_payload,
                )
                task_id = str(task.get("task_id") or "") or None
                connection.set_state("busy", active_task_id=task_id)
                if task_id:
                    connection.renew_task(task_id)
                return {
                    "kind": "task",
                    "conversation_id": connection.conversation_id,
                    "connector_id": connection.connector_id,
                    **task_payload,
                    "next_action": (
                        "execute in this current TUI under its live local permissions; "
                        "report with agent_task_update, then call agent_duty once "
                        "to re-arm the event subscription"
                    ),
                }

        bounded_wait = max(0.0, min(float(wait_seconds), 120.0))
        normalized_limit = max(1, min(int(limit), 20))
        deadline = time.monotonic() + bounded_wait
        while True:
            for connection in connections:
                remaining = max(0.0, deadline - time.monotonic())
                request_wait = min(DIRECT_TUI_ROOM_SLICE_SECONDS, remaining)
                native = connection.wait_native_event(
                    wait_seconds=request_wait,
                    limit=normalized_limit,
                )
                event = native.get("event")
                if isinstance(event, dict) and event.get("messages"):
                    payload = {
                        "messages": event.get("messages") or [],
                        "count": len(event.get("messages") or []),
                        "timed_out": False,
                        "last_sequence": max(
                            (
                                int(item.get("sequence") or 0)
                                for item in event.get("messages") or []
                                if isinstance(item, dict)
                            ),
                            default=0,
                        ),
                        "backlog": event.get("backlog") or native.get("backlog") or {},
                        "native_event": {
                            "event_id": event.get("event_id"),
                            "state": event.get("state"),
                            "required_message_ids": event.get("required_message_ids")
                            or [],
                            "required_reply_count": event.get("required_reply_count")
                            or 0,
                        },
                    }
                    self._record_payload(
                        thread_id=normalized,
                        connection=connection,
                        payload=payload,
                    )
                    connection.set_state("busy")
                    return {
                        "kind": "messages",
                        "conversation_id": connection.conversation_id,
                        "connector_id": connection.connector_id,
                        **payload,
                        "next_action": (
                            "handle these messages as this current TUI, use the "
                            "normal Agent Bridge reply/send tools, then call "
                            "agent_duty once to re-arm; do not create a timeout loop"
                        ),
                    }
                if time.monotonic() >= deadline:
                    break
            if time.monotonic() >= deadline:
                break
        return {
            "kind": "timeout",
            "timed_out": True,
            "thread_id": normalized,
            "rooms": [
                {
                    "conversation_id": item.conversation_id,
                    "connector_id": item.connector_id,
                }
                for item in connections
            ],
            "next_action": (
                "no event arrived during this one-shot probe; stop here because an "
                "idle timeout never requests a new subscription"
            ),
        }

    def reply_native_message(
        self,
        *,
        thread_id: str,
        message_id: str,
        body: str,
        refs: list[dict[str, Any]] | None,
        mentions: list[str] | None,
    ) -> dict[str, Any] | None:
        normalized = normalized_thread_id(thread_id)
        connections = self.connections_for_thread(normalized)
        with self._lock:
            connector_id = self._message_routes.get((normalized, message_id))
        connection = next(
            (item for item in connections if item.connector_id == connector_id),
            None,
        )
        if connection is None:
            return None
        return connection.reply_native_message(
            message_id=message_id,
            body=body,
            refs=refs,
            mentions=mentions,
        )

    def task_updated(self, *, thread_id: str, task_id: str, terminal: bool) -> None:
        if not terminal:
            return
        connections = self.connections_for_thread(thread_id)
        with self._lock:
            connector_id = self._task_routes.get((thread_id, task_id))
        for connection in connections:
            if connection.connector_id == connector_id:
                connection.set_state("online")
                return

    def close(self) -> None:
        with self._lock:
            connections = list(self._connections.values())
        for connection in connections:
            connection.close()


DIRECT_TUI_REGISTRY = DirectTuiRegistry()
atexit.register(DIRECT_TUI_REGISTRY.close)
