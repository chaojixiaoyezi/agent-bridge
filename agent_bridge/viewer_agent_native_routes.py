"""Native TUI lease and exact-session channel routes."""

from __future__ import annotations

import asyncio

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .store import BridgeStore
from .viewer_http import (
    _agent_json_call,
    _authenticate_request,
    _json_body,
    _json_error,
)


def build_agent_native_routes(*, store: BridgeStore) -> list[Route]:
    async def bind_native_agent_session(request: Request) -> Response:
        return await _agent_json_call(
            request,
            store,
            required={
                "connector_id",
                "tui_endpoint_id",
                "native_session_id",
                "process_epoch",
                "binding_source",
            },
            allowed={
                "connector_id",
                "tui_endpoint_id",
                "native_session_id",
                "process_epoch",
                "binding_source",
                "replace_existing_session",
                "metadata",
            },
            operation=lambda auth, payload: store.bind_native_agent_session(
                participant_id=auth["participant_id"],
                authorized_session_id=auth["session_id"],
                connector_id=payload["connector_id"],
                tui_endpoint_id=payload["tui_endpoint_id"],
                native_session_id=payload["native_session_id"],
                process_epoch=payload["process_epoch"],
                binding_source=payload["binding_source"],
                replace_existing_session=payload.get(
                    "replace_existing_session",
                    False,
                ),
                metadata=payload.get("metadata"),
            ),
        )

    async def heartbeat_native_agent_session(request: Request) -> Response:
        return await _agent_json_call(
            request,
            store,
            required={"connector_id", "lease_id", "process_epoch"},
            allowed={
                "connector_id",
                "lease_id",
                "process_epoch",
                "state",
                "active_task_id",
                "detail",
            },
            operation=lambda auth, payload: {
                "lease": store.heartbeat_native_agent_session(
                    participant_id=auth["participant_id"],
                    authorized_session_id=auth["session_id"],
                    connector_id=payload["connector_id"],
                    lease_id=payload["lease_id"],
                    process_epoch=payload["process_epoch"],
                    state=payload.get("state", "online"),
                    active_task_id=payload.get("active_task_id"),
                    detail=payload.get("detail"),
                )
            },
        )

    async def end_native_agent_session(request: Request) -> Response:
        return await _agent_json_call(
            request,
            store,
            required={"connector_id", "lease_id", "process_epoch"},
            allowed={"connector_id", "lease_id", "process_epoch"},
            operation=lambda auth, payload: {
                "lease": store.end_native_agent_session(
                    participant_id=auth["participant_id"],
                    authorized_session_id=auth["session_id"],
                    connector_id=payload["connector_id"],
                    lease_id=payload["lease_id"],
                    process_epoch=payload["process_epoch"],
                )
            },
        )

    async def fallback_native_agent_session(request: Request) -> Response:
        return await _agent_json_call(
            request,
            store,
            required={"connector_id", "lease_id", "process_epoch"},
            allowed={"connector_id", "lease_id", "process_epoch"},
            operation=lambda auth, payload: store.fallback_native_agent_session(
                participant_id=auth["participant_id"],
                authorized_session_id=auth["session_id"],
                connector_id=payload["connector_id"],
                lease_id=payload["lease_id"],
                process_epoch=payload["process_epoch"],
            ),
        )

    async def wait_native_channel_event(request: Request) -> Response:
        try:
            auth = _authenticate_request(request, store)
            payload = await _json_body(
                request,
                required={
                    "connector_id",
                    "lease_id",
                    "process_epoch",
                    "request_id",
                    "route_token",
                },
                allowed={
                    "connector_id",
                    "lease_id",
                    "process_epoch",
                    "request_id",
                    "route_token",
                    "wait_seconds",
                    "limit",
                },
            )
            result = await asyncio.to_thread(
                store.wait_native_channel_event,
                participant_id=auth["participant_id"],
                authorized_session_id=auth["session_id"],
                connector_id=payload["connector_id"],
                lease_id=payload["lease_id"],
                process_epoch=payload["process_epoch"],
                request_id=payload["request_id"],
                route_token=payload["route_token"],
                wait_seconds=payload.get("wait_seconds", 30),
                limit=payload.get("limit", 20),
            )
            return JSONResponse(result)
        except Exception as exc:
            return _json_error(exc)

    async def receive_native_channel_event(request: Request) -> Response:
        return await _agent_json_call(
            request,
            store,
            required={
                "connector_id",
                "lease_id",
                "process_epoch",
                "event_id",
                "route_token",
                "stage",
            },
            allowed={
                "connector_id",
                "lease_id",
                "process_epoch",
                "event_id",
                "route_token",
                "stage",
            },
            operation=lambda auth, payload: store.receive_native_channel_event(
                participant_id=auth["participant_id"],
                authorized_session_id=auth["session_id"],
                connector_id=payload["connector_id"],
                lease_id=payload["lease_id"],
                process_epoch=payload["process_epoch"],
                event_id=payload["event_id"],
                route_token=payload["route_token"],
                stage=payload["stage"],
            ),
        )

    async def reply_native_channel_event(request: Request) -> Response:
        return await _agent_json_call(
            request,
            store,
            required={
                "connector_id",
                "lease_id",
                "process_epoch",
                "event_id",
                "route_token",
                "message_id",
                "body",
            },
            allowed={
                "connector_id",
                "lease_id",
                "process_epoch",
                "event_id",
                "route_token",
                "message_id",
                "body",
                "refs",
                "mentions",
            },
            operation=lambda auth, payload: store.reply_native_channel_event(
                participant_id=auth["participant_id"],
                authorized_session_id=auth["session_id"],
                connector_id=payload["connector_id"],
                lease_id=payload["lease_id"],
                process_epoch=payload["process_epoch"],
                event_id=payload["event_id"],
                route_token=payload["route_token"],
                message_id=payload["message_id"],
                body_text=payload["body"],
                refs=payload.get("refs"),
                mentions=payload.get("mentions"),
            ),
        )

    async def send_native_channel_event(request: Request) -> Response:
        return await _agent_json_call(
            request,
            store,
            required={
                "connector_id",
                "lease_id",
                "process_epoch",
                "event_id",
                "route_token",
                "body",
            },
            allowed={
                "connector_id",
                "lease_id",
                "process_epoch",
                "event_id",
                "route_token",
                "body",
                "mentions",
                "notification_mode",
            },
            operation=lambda auth, payload: store.send_native_channel_event(
                participant_id=auth["participant_id"],
                authorized_session_id=auth["session_id"],
                connector_id=payload["connector_id"],
                lease_id=payload["lease_id"],
                process_epoch=payload["process_epoch"],
                event_id=payload["event_id"],
                route_token=payload["route_token"],
                body_text=payload["body"],
                mentions=payload.get("mentions"),
                notification_mode=payload.get("notification_mode"),
            ),
        )

    return [
            Route(
                "/agent/native/session/bind",
                bind_native_agent_session,
                methods=["POST"],
            ),
            Route(
                "/agent/native/session/heartbeat",
                heartbeat_native_agent_session,
                methods=["POST"],
            ),
            Route(
                "/agent/native/session/end",
                end_native_agent_session,
                methods=["POST"],
            ),
            Route(
                "/agent/native/session/fallback",
                fallback_native_agent_session,
                methods=["POST"],
            ),
            Route(
                "/agent/native/channel/wait",
                wait_native_channel_event,
                methods=["POST"],
            ),
            Route(
                "/agent/native/channel/receipt",
                receive_native_channel_event,
                methods=["POST"],
            ),
            Route(
                "/agent/native/channel/reply",
                reply_native_channel_event,
                methods=["POST"],
            ),
            Route(
                "/agent/native/channel/send",
                send_native_channel_event,
                methods=["POST"],
            ),
    ]
