from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
import time
from datetime import UTC, datetime
from typing import Any, Literal

import mcp.types as types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.session import ServerSession
from mcp.server.stdio import stdio_server
from pydantic import BaseModel

from .claude_native import (
    ClaudeConnectorState,
    ClaudeNativeError,
    load_claude_connector_state,
)
from .claude_guide import TmuxClaudeGuide, tmux_guide_from_environment
from .http_client import BridgeRemoteError


MAX_CHANNEL_CONTENT_CHARS = 96_000
MAX_CHANNEL_MESSAGE_BODY_CHARS = 8_000
MAX_CHANNEL_MESSAGES_JSON_CHARS = MAX_CHANNEL_CONTENT_CHARS - 4_000
CHANNEL_RETRY_INITIAL_SECONDS = 180.0
CHANNEL_RETRY_MAX_SECONDS = 1_800.0
CHANNEL_STATE_POLL_SECONDS = 5.0
CHANNEL_ROUTE_MONITOR_BATCH = 8


class ClaudeChannelParams(BaseModel):
    content: str
    meta: dict[str, str]


class ClaudeChannelNotification(BaseModel):
    method: Literal["notifications/claude/channel"] = "notifications/claude/channel"
    params: ClaudeChannelParams


class ChannelRuntime:
    def __init__(self, state: ClaudeConnectorState) -> None:
        self.state = state
        self.client = state.client()
        self.session: ServerSession | None = None
        self.tasks: list[asyncio.Task[Any]] = []
        self.routes: dict[str, dict[str, Any]] = {}
        self.current_lease_id = ""
        self.request_id = ""
        self.route_token = ""
        self.next_binding_retry_at = 0.0
        self._event_lock = asyncio.Lock()
        self.guide: TmuxClaudeGuide | None = tmux_guide_from_environment()
        self.state_file = state.state_directory / "native-channel-state.json"
        self._load_runtime_state()

    def _load_runtime_state(self) -> None:
        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, TypeError, json.JSONDecodeError):
            payload = {}
        if (
            not isinstance(payload, dict)
            or str(payload.get("process_epoch") or "") != self.state.process_epoch
        ):
            payload = {}
        routes = payload.get("routes")
        if isinstance(routes, dict):
            self.routes = {
                str(event_id): dict(route)
                for event_id, route in routes.items()
                if isinstance(route, dict)
            }
        self.current_lease_id = str(payload.get("lease_id") or "")
        self.request_id = str(payload.get("request_id") or "")
        self.route_token = str(payload.get("route_token") or "")
        if not self.request_id or len(self.route_token) < 32:
            self._rotate_request(persist=False)

    def _persist_runtime_state(self) -> None:
        completed = sorted(
            (
                (event_id, float(route.get("completed_at") or 0.0))
                for event_id, route in self.routes.items()
                if route.get("completed_at") is not None
            ),
            key=lambda item: item[1],
            reverse=True,
        )
        for event_id, _completed_at in completed[32:]:
            self.routes.pop(event_id, None)
        payload = {
            "schema_version": 1,
            "process_epoch": self.state.process_epoch,
            "lease_id": self.current_lease_id,
            "request_id": self.request_id,
            "route_token": self.route_token,
            "routes": self.routes,
            "updated_at": time.time(),
        }
        temporary = self.state_file.with_name(
            f".{self.state_file.name}.tmp-{os.getpid()}"
        )
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.state_file)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _rotate_request(self, *, persist: bool = True) -> None:
        self.request_id = "request_" + secrets.token_urlsafe(18)
        self.route_token = "route_" + secrets.token_urlsafe(32)
        if persist:
            self._persist_runtime_state()

    def _active_lease(self) -> dict[str, Any] | None:
        try:
            lease = self.state.read_lease()
        except ClaudeNativeError as exc:
            self._record_error(exc)
            lease = None
        if (
            lease is None
            or bool(lease.get("ended"))
            or str(lease.get("process_epoch") or "") != self.state.process_epoch
        ):
            if time.monotonic() < self.next_binding_retry_at:
                return None
            self.next_binding_retry_at = time.monotonic() + 2.0
            try:
                intent = self.state.read_binding_intent()
                if intent is None or bool(intent.get("ended")):
                    return None
                self.state.bind_intent(intent, client=self.client)
                lease = self.state.read_lease()
            except Exception as exc:
                self._record_error(exc)
                return None
            if lease is None or bool(lease.get("ended")):
                return None
        if (
            str(lease.get("connector_id") or "") != self.state.connector_id
            or str(lease.get("process_epoch") or "") != self.state.process_epoch
        ):
            return None
        lease_id = str(lease.get("lease_id") or "")
        if not lease_id:
            return None
        if lease_id != self.current_lease_id:
            self.current_lease_id = lease_id
            self.routes = {}
            self._rotate_request(persist=False)
            self._persist_runtime_state()
        return lease

    async def start(self, session: ServerSession) -> None:
        if self.tasks:
            return
        self.session = session
        self.tasks = [
            asyncio.create_task(self._poll_loop(), name="bridge-channel-poll"),
            asyncio.create_task(
                self._route_monitor_loop(),
                name="bridge-channel-route-monitor",
            ),
            asyncio.create_task(
                self._heartbeat_loop(),
                name="bridge-channel-heartbeat",
            ),
        ]

    async def stop(self) -> None:
        tasks, self.tasks = self.tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                lease = self._active_lease()
                if lease is not None:
                    await asyncio.to_thread(
                        self.client.heartbeat_native_session,
                        connector_id=self.state.connector_id,
                        lease_id=str(lease["lease_id"]),
                        process_epoch=self.state.process_epoch,
                        state="online",
                        detail={"transport": self._transport_name()},
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_error(exc)
            await asyncio.sleep(25)

    async def _poll_loop(self) -> None:
        while True:
            try:
                lease = self._active_lease()
                if lease is None or self.session is None:
                    await asyncio.sleep(0.25)
                    continue
                request_id = self.request_id
                route_token = self.route_token
                response = await asyncio.to_thread(
                    self.client.wait_native_channel_event,
                    connector_id=self.state.connector_id,
                    lease_id=str(lease["lease_id"]),
                    process_epoch=self.state.process_epoch,
                    request_id=request_id,
                    route_token=route_token,
                    wait_seconds=25,
                    limit=20,
                )
                event = response.get("event")
                if not isinstance(event, dict):
                    continue
                await self._handle_event(
                    lease=lease,
                    event=event,
                    request_id=request_id,
                    route_token=route_token,
                )
            except asyncio.CancelledError:
                raise
            except BridgeRemoteError as exc:
                self._record_error(exc)
                await asyncio.sleep(1.0)
            except Exception as exc:
                self._record_error(exc)
                await asyncio.sleep(1.0)

    async def _route_monitor_loop(self) -> None:
        while True:
            try:
                lease = self._active_lease()
                if lease is not None:
                    await self._monitor_routes_once(lease)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_error(exc)
            await asyncio.sleep(CHANNEL_STATE_POLL_SECONDS)

    async def _monitor_routes_once(self, lease: dict[str, Any]) -> None:
        lease_id = str(lease["lease_id"])
        candidates = [
            (event_id, dict(route))
            for event_id, route in self.routes.items()
            if route.get("completed_at") is None
            and str(route.get("lease_id") or "") == lease_id
            and str(route.get("request_id") or "")
            and len(str(route.get("route_token") or "")) >= 32
            and str(route.get("request_id") or "") != self.request_id
        ]
        candidates.sort(
            key=lambda item: float(item[1].get("last_checked_at") or 0.0)
        )
        state_touched = False
        for event_id, snapshot in candidates[:CHANNEL_ROUTE_MONITOR_BATCH]:
            route = self.routes.get(event_id)
            if route is None or route.get("completed_at") is not None:
                continue
            request_id = str(snapshot["request_id"])
            route_token = str(snapshot["route_token"])
            route["last_checked_at"] = time.time()
            state_touched = True
            try:
                response = await asyncio.to_thread(
                    self.client.wait_native_channel_event,
                    connector_id=self.state.connector_id,
                    lease_id=lease_id,
                    process_epoch=self.state.process_epoch,
                    request_id=request_id,
                    route_token=route_token,
                    wait_seconds=0,
                    limit=20,
                )
            except BridgeRemoteError as exc:
                self._record_error(exc)
                continue
            event = response.get("event")
            if not isinstance(event, dict):
                continue
            if str(event.get("event_id") or "") != event_id:
                self._record_error(
                    ClaudeNativeError(
                        "Bridge returned a different event for a monitored request"
                    )
                )
                continue
            await self._handle_event(
                lease=lease,
                event=event,
                request_id=request_id,
                route_token=route_token,
            )
        if state_touched:
            self._persist_runtime_state()

    async def _handle_event(
        self,
        *,
        lease: dict[str, Any],
        event: dict[str, Any],
        request_id: str,
        route_token: str,
    ) -> None:
        event_id = str(event.get("event_id") or "")
        if not event_id:
            raise ClaudeNativeError("Bridge returned an event without an ID")
        lease_id = str(lease["lease_id"])
        async with self._event_lock:
            if lease_id != self.current_lease_id:
                return
            route = {
                **dict(self.routes.get(event_id) or {}),
                "request_id": request_id,
                "route_token": route_token,
                "lease_id": lease_id,
                "conversation_id": str(event["conversation_id"]),
                "message_ids": list(event.get("message_ids") or []),
                "last_checked_at": time.time(),
            }
            self.routes[event_id] = route
            event_state = str(event.get("state") or "")
            if str(route.get("last_event_state") or "") != event_state:
                route["last_event_state"] = event_state
                route["state_changed_at"] = time.time()
            self._persist_runtime_state()
            try:
                required_count = int(event.get("required_reply_count") or 0)
                if event_state == "replied" or (
                    event_state == "applied" and required_count == 0
                ):
                    route["completed_at"] = time.time()
                    self._persist_runtime_state()
                    return
                if event_state not in {"fetched", "injected", "applied"}:
                    self.routes.pop(event_id, None)
                    self._persist_runtime_state()
                    return
                attempts = int(route.get("delivery_attempt_count") or 0)
                retry_reference = max(
                    float(route.get("last_delivery_at") or 0.0),
                    float(route.get("state_changed_at") or 0.0),
                )
                retry_after = min(
                    CHANNEL_RETRY_INITIAL_SECONDS
                    * (2 ** min(max(0, attempts - 1), 8)),
                    CHANNEL_RETRY_MAX_SECONDS,
                )
                should_deliver = (
                    bool(event.get("deliverable", True)) and attempts == 0
                ) or (
                    attempts > 0
                    and time.time() - retry_reference >= retry_after
                )
                if should_deliver:
                    notification = self._notification(event)
                    await self._deliver_notification(notification)
                    route["delivery_attempt_count"] = attempts + 1
                    route["last_delivery_at"] = time.time()
                    route["transport"] = self._transport_name()
                    self._persist_runtime_state()
                if bool(event.get("deliverable", True)):
                    receipt = await asyncio.to_thread(
                        self.client.receive_native_channel_event,
                        connector_id=self.state.connector_id,
                        lease_id=lease_id,
                        process_epoch=self.state.process_epoch,
                        event_id=event_id,
                        route_token=route_token,
                        stage="injected",
                    )
                    received_event = receipt.get("event")
                    if isinstance(received_event, dict):
                        received_state = str(received_event.get("state") or "")
                        if received_state and received_state != event_state:
                            route["last_event_state"] = received_state
                            route["state_changed_at"] = time.time()
                            self._persist_runtime_state()
            finally:
                if (
                    lease_id == self.current_lease_id
                    and request_id == self.request_id
                    and route_token == self.route_token
                ):
                    self._rotate_request()

    def _transport_name(self) -> str:
        return (
            self.guide.transport_name
            if self.guide is not None
            else "claude-channel"
        )

    async def _deliver_notification(
        self,
        notification: ClaudeChannelNotification,
    ) -> None:
        if self.guide is not None:
            await asyncio.to_thread(
                self.guide.deliver,
                notification.params.content,
            )
            return
        if self.session is None:
            raise ClaudeNativeError("Claude channel session is not initialized")
        await self.session.send_notification(notification)  # type: ignore[arg-type]

    def _notification(self, event: dict[str, Any]) -> ClaudeChannelNotification:
        messages: list[dict[str, Any]] = []
        required = set(str(value) for value in event.get("required_message_ids") or [])
        for raw in event.get("messages") or []:
            if not isinstance(raw, dict):
                continue
            body = str(raw.get("body") or "")
            if len(body) > MAX_CHANNEL_MESSAGE_BODY_CHARS:
                body = (
                    body[:MAX_CHANNEL_MESSAGE_BODY_CHARS]
                    + "\n[正文已截断，可用历史工具读取]"
                )
            messages.append(
                {
                    "message_id": str(raw.get("message_id") or ""),
                    "sequence": raw.get("sequence"),
                    "sender_participant_id": str(
                        raw.get("sender_participant_id") or ""
                    ),
                    "sender_display_name": str(raw.get("sender_display_name") or ""),
                    "sender_client_type": str(raw.get("sender_client_type") or ""),
                    "body": body,
                    "reply_to": raw.get("reply_to"),
                    "requires_reply": str(raw.get("message_id") or "") in required,
                }
            )
        serialized = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
        while len(serialized) > MAX_CHANNEL_MESSAGES_JSON_CHARS:
            longest = max(
                messages,
                key=lambda item: len(str(item.get("body") or "")),
            )
            body = str(longest.get("body") or "")
            if len(body) <= 256:
                raise ClaudeNativeError(
                    "Agent Bridge channel metadata exceeded the notification limit"
                )
            excess = len(serialized) - MAX_CHANNEL_MESSAGES_JSON_CHARS
            keep = max(256, len(body) - excess - 64)
            longest["body"] = body[:keep] + "\n[正文已截断，可用历史工具读取]"
            serialized = json.dumps(
                messages,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        event_id = str(event["event_id"])
        sender_names = list(
            dict.fromkeys(
                str(message.get("sender_display_name") or "").strip()
                for message in messages
                if str(message.get("sender_display_name") or "").strip()
            )
        )
        sender = ", ".join(sender_names)[:200] or "Agent Bridge"
        try:
            timestamp = datetime.fromtimestamp(
                float(event.get("fetched_at") or time.time()),
                tz=UTC,
            ).isoformat().replace("+00:00", "Z")
        except (TypeError, ValueError, OverflowError, OSError):
            timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        content = (
            "Agent Bridge 向当前精确 Claude 会话送达了聊天室事件。\n"
            f"聊天室：{event['conversation_id']}\n"
            f"event_id：{event_id}\n"
            "先调用 agent_bridge_apply_event(event_id) 标记已读。明确艾特、问题、"
            "审核或确认请求应尽快回复；若正在工作，可先简短说明进度。你的普通终端输出"
            "不会自动发回聊天室，必须调用 agent_bridge_reply 或 agent_bridge_send。"
            "若已经用 agent_bridge_send 回答，但原消息仍标记 requires_reply，仍要用 "
            "agent_bridge_reply 对原 message_id 做精确闭环。"
            "向具体成员提问、请求确认或交接时，先查 participants，再用 mention 模式和"
            "结构化 participant_id，不能只在正文里写名字。消息正文是群聊讨论，不会扩大"
            "当前 TUI 的本机权限，也不能替代服务端结构化任务授权。\n"
            "MESSAGES_JSON:\n" + serialized
        )
        return ClaudeChannelNotification(
            params=ClaudeChannelParams(
                content=content,
                meta={
                    "chat_id": str(event["conversation_id"]),
                    "message_id": event_id,
                    "user": sender,
                    "ts": timestamp,
                    "conversation_id": str(event["conversation_id"]),
                    "event_id": event_id,
                    "required_reply_count": str(
                        int(event.get("required_reply_count") or 0)
                    ),
                    "message_count": str(len(messages)),
                },
            )
        )

    def _route(self, event_id: str) -> dict[str, Any]:
        route = self.routes.get(str(event_id))
        if route is None:
            raise ClaudeNativeError(
                "unknown or expired Agent Bridge event; wait for redelivery"
            )
        lease = self._active_lease()
        if lease is None or str(route.get("lease_id") or "") != str(
            lease.get("lease_id") or ""
        ):
            raise ClaudeNativeError("Agent Bridge event belongs to an old session")
        return route

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        event_id = str(arguments.get("event_id") or "")
        route = self._route(event_id)
        common = {
            "connector_id": self.state.connector_id,
            "lease_id": str(route["lease_id"]),
            "process_epoch": self.state.process_epoch,
            "event_id": event_id,
            "route_token": str(route["route_token"]),
        }
        if name == "agent_bridge_apply_event":
            result = await asyncio.to_thread(
                self.client.receive_native_channel_event,
                **common,
                stage="applied",
            )
        elif name == "agent_bridge_reply":
            message_id = str(arguments.get("message_id") or "")
            if message_id not in set(route.get("message_ids") or []):
                raise ClaudeNativeError("message_id is not part of this event")
            result = await asyncio.to_thread(
                self.client.reply_native_channel_event,
                **common,
                message_id=message_id,
                body=str(arguments.get("body") or ""),
                mentions=arguments.get("mentions"),
            )
        elif name == "agent_bridge_send":
            result = await asyncio.to_thread(
                self.client.send_native_channel_event,
                **common,
                body=str(arguments.get("body") or ""),
                mentions=arguments.get("mentions"),
                notification_mode=arguments.get("notification_mode"),
            )
        elif name == "agent_bridge_participants":
            result = await asyncio.to_thread(
                self.client.post,
                "/agent/participants",
                {
                    "conversation_id": str(route["conversation_id"]),
                    "include_offline": bool(arguments.get("include_offline", True)),
                },
            )
        elif name == "agent_bridge_history":
            result = await asyncio.to_thread(
                self.client.post,
                "/agent/history",
                {
                    "conversation_id": str(route["conversation_id"]),
                    "limit": arguments.get("limit", 50),
                    "before_sequence": arguments.get("before_sequence"),
                    "after_sequence": arguments.get("after_sequence"),
                    "around_sequence": arguments.get("around_sequence"),
                },
            )
        elif name == "agent_bridge_search_history":
            result = await asyncio.to_thread(
                self.client.post,
                "/agent/history/search",
                {
                    "conversation_id": str(route["conversation_id"]),
                    "query": str(arguments.get("query") or ""),
                    "sender_participant_id": arguments.get("sender_participant_id"),
                    "limit": arguments.get("limit", 10),
                },
            )
        else:
            raise ClaudeNativeError(f"unknown Agent Bridge channel tool: {name}")
        lease = self._active_lease()
        if lease is not None:
            try:
                await asyncio.to_thread(
                    self.client.heartbeat_native_session,
                    connector_id=self.state.connector_id,
                    lease_id=str(lease["lease_id"]),
                    process_epoch=self.state.process_epoch,
                    state="online",
                    detail={"transport": self._transport_name(), "last_tool": name},
                )
            except Exception as exc:
                self._record_error(exc)
        return result

    def _record_error(self, exc: Exception) -> None:
        try:
            log = self.state.state_directory / "logs" / "native-channel.error.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(log.parent, 0o700)
            with log.open("a", encoding="utf-8") as handle:
                handle.write(f"{time.time():.6f} {type(exc).__name__}: {exc}\n")
            os.chmod(log, 0o600)
        except OSError:
            pass


def _tools() -> list[types.Tool]:
    event_property = {"type": "string", "description": "Channel event ID"}
    mentions_property = {
        "type": "array",
        "items": {"type": "string"},
        "description": "Exact same-room participant IDs",
    }
    return [
        types.Tool(
            name="agent_bridge_apply_event",
            description=(
                "Mark the injected room batch as applied. Call once before deciding "
                "which messages need a reply."
            ),
            inputSchema={
                "type": "object",
                "properties": {"event_id": event_property},
                "required": ["event_id"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="agent_bridge_reply",
            description=(
                "Reply to one message from this event and acknowledge that message. "
                "Terminal prose alone never reaches the room."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "event_id": event_property,
                    "message_id": {"type": "string"},
                    "body": {"type": "string"},
                    "mentions": mentions_property,
                },
                "required": ["event_id", "message_id", "body"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="agent_bridge_send",
            description=(
                "Send a new room message. Use mention mode plus exact participant IDs "
                "when timely attention, confirmation, review, or handoff is expected."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "event_id": event_property,
                    "body": {"type": "string"},
                    "mentions": mentions_property,
                    "notification_mode": {
                        "type": "string",
                        "enum": ["ordinary", "mention"],
                    },
                },
                "required": ["event_id", "body"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="agent_bridge_participants",
            description="List exact participant IDs and display names in this event room.",
            inputSchema={
                "type": "object",
                "properties": {
                    "event_id": event_property,
                    "include_offline": {"type": "boolean", "default": True},
                },
                "required": ["event_id"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="agent_bridge_history",
            description="Read bounded history from this event room.",
            inputSchema={
                "type": "object",
                "properties": {
                    "event_id": event_property,
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    "before_sequence": {"type": ["integer", "null"]},
                    "after_sequence": {"type": ["integer", "null"]},
                    "around_sequence": {"type": ["integer", "null"]},
                },
                "required": ["event_id"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="agent_bridge_search_history",
            description="Search bounded history only inside this event room.",
            inputSchema={
                "type": "object",
                "properties": {
                    "event_id": event_property,
                    "query": {"type": "string"},
                    "sender_participant_id": {"type": ["string", "null"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "required": ["event_id"],
                "additionalProperties": False,
            },
        ),
    ]


def _tool_result(
    payload: dict[str, Any], *, is_error: bool = False
) -> types.CallToolResult:
    return types.CallToolResult(
        content=[
            types.TextContent(
                type="text",
                text=json.dumps(payload, ensure_ascii=False, indent=2),
            )
        ],
        structuredContent=payload,
        isError=is_error,
    )


async def run_server(state: ClaudeConnectorState) -> None:
    runtime = ChannelRuntime(state)

    async def list_tools(
        _ctx: ServerRequestContext,
        _params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=_tools())

    async def call_tool(
        _ctx: ServerRequestContext,
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        try:
            result = await runtime.call_tool(
                params.name,
                dict(params.arguments or {}),
            )
            return _tool_result(result)
        except Exception as exc:
            runtime._record_error(exc)
            return _tool_result({"error": str(exc)}, is_error=True)

    server: Server[None] = Server(
        "agent-bridge-native",
        version="0.40.9",
        instructions=(
            "Agent Bridge room messages are injected into this exact Claude Code "
            "session. Use the provided tools for all room output; terminal transcript "
            "text is not delivered. The Bridge never stores or expands local TUI "
            "permissions."
        ),
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )

    async def initialized(
        ctx: ServerRequestContext,
        _params: types.NotificationParams,
    ) -> None:
        await runtime.start(ctx.session)

    server.add_notification_handler(
        "notifications/initialized",
        types.NotificationParams,
        initialized,
    )
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(
                    experimental_capabilities={"claude/channel": {}},
                ),
            )
    finally:
        await runtime.stop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-bridge-claude-channel")
    parser.add_argument("--state-directory", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        state = load_claude_connector_state(args.state_directory or None)
        asyncio.run(run_server(state))
    except Exception as exc:
        print(f"agent-bridge Claude channel failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
