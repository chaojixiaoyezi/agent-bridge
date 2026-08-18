from __future__ import annotations

import asyncio
import secrets
import time

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .avatars import avatar_catalog_payload
from .security import ViewerSecurityPolicy
from .store import (
    AuthenticationError,
    BridgeStore,
    ConflictError,
    NotFoundError,
)
from .validation import ValidationError
from .viewer_http import (
    HttpInputError,
    _agent_json_call,
    _authenticate_request,
    _event_cursor,
    _json_body,
    _json_call,
    _json_error,
    _sse_event,
)


def build_agent_routes(
    *,
    store: BridgeStore,
    policy: ViewerSecurityPolicy,
    required_registration_secret: str | None,
    enforce_rate,
) -> list[Route]:
    async def register_agent(request: Request) -> Response:
        if policy.public_mode:
            try:
                enforce_rate(
                    request,
                    "agent-register-ip",
                    limit=120,
                    window_seconds=60,
                )
            except Exception as exc:
                return _json_error(exc)
        try:
            payload = await _json_body(
                request,
                required={
                    "product",
                    "username",
                    "conversation_id",
                },
                allowed={
                    "product",
                    "username",
                    "session_alias",
                    "signature",
                    "conversation_id",
                    "roles",
                    "capabilities",
                },
            )
        except HttpInputError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status_code)
        enrollment_token = request.headers.get("x-agent-bridge-enrollment", "").strip()
        if enrollment_token:
            connector_id = request.headers.get(
                "x-agent-bridge-connector",
                "",
            ).strip()
            component = request.headers.get(
                "x-agent-bridge-component",
                "",
            ).strip()
            protocol_header = request.headers.get(
                "x-agent-bridge-protocol",
                "2",
            ).strip()
            return _json_call(
                lambda: store.register_agent_session_from_enrollment(
                    enrollment_token=enrollment_token,
                    connector_id=connector_id or None,
                    connector_component=component or None,
                    connector_protocol_version=int(protocol_header),
                    product=payload["product"],
                    username=payload["username"],
                    session_alias=payload.get("session_alias"),
                    signature=payload.get("signature"),
                    roles=payload.get("roles"),
                    capabilities=payload.get("capabilities"),
                ),
                success_status=201,
            )
        if required_registration_secret is not None and not secrets.compare_digest(
            request.headers.get("x-agent-bridge-registration", ""),
            required_registration_secret,
        ):
            return JSONResponse(
                {"error": "registration authorization is required"},
                status_code=401,
            )
        return _json_call(
            lambda: store.register_agent_session(
                product=payload["product"],
                username=payload["username"],
                session_alias=payload.get("session_alias"),
                signature=payload.get("signature"),
                conversation_id=payload["conversation_id"],
                roles=payload.get("roles"),
                capabilities=payload.get("capabilities"),
            ),
            success_status=201,
        )

    async def rotate_agent_enrollment(request: Request) -> Response:
        if policy.public_mode:
            try:
                enforce_rate(
                    request,
                    "agent-enrollment-rotation-ip",
                    limit=30,
                    window_seconds=60 * 60,
                )
            except Exception as exc:
                return _json_error(exc)
        enrollment_token = request.headers.get(
            "x-agent-bridge-enrollment",
            "",
        ).strip()
        connector_id = request.headers.get(
            "x-agent-bridge-connector",
            "",
        ).strip()
        if not enrollment_token or not connector_id:
            return JSONResponse(
                {"error": "connector enrollment authorization is required"},
                status_code=401,
            )
        try:
            payload = await _json_body(
                request,
                required={"new_enrollment_token"},
                allowed={"new_enrollment_token"},
            )
            return JSONResponse(
                {
                    "connector": store.rotate_agent_connector_enrollment(
                        connector_id=connector_id,
                        current_enrollment_token=enrollment_token,
                        new_enrollment_token=payload["new_enrollment_token"],
                    )
                }
            )
        except Exception as exc:
            return _json_error(exc)

    async def accept_agent_invitation(request: Request) -> Response:
        if policy.public_mode:
            try:
                enforce_rate(
                    request,
                    "agent-invitation-ip",
                    limit=60,
                    window_seconds=60,
                )
            except Exception as exc:
                return _json_error(exc)
        invitation_token = request.headers.get(
            "x-agent-bridge-invitation",
            "",
        ).strip()
        if not invitation_token:
            return JSONResponse(
                {"error": "Agent invitation authorization is required"},
                status_code=401,
            )
        try:
            payload = await _json_body(
                request,
                required={"product", "username", "signature"},
                allowed={
                    "product",
                    "username",
                    "signature",
                    "avatar_key",
                    "roles",
                    "capabilities",
                    "enrollment_token",
                    "connector_binding_version",
                    "tui_endpoint_id",
                    "tui_native_session_id",
                    "tui_access_mode",
                    "tui_confirmed",
                },
            )
        except HttpInputError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status_code)
        return _json_call(
            lambda: store.accept_agent_invitation(
                invitation_token=invitation_token,
                product=payload["product"],
                username=payload["username"],
                signature=payload["signature"],
                avatar_key=payload.get("avatar_key", "auto"),
                roles=payload.get("roles"),
                capabilities=payload.get("capabilities"),
                enrollment_token=payload.get("enrollment_token"),
                connector_binding_version=payload.get(
                    "connector_binding_version",
                    1,
                ),
                tui_endpoint_id=payload.get("tui_endpoint_id"),
                tui_native_session_id=payload.get("tui_native_session_id"),
                # Pre-v35 clients may still send this field; the store ignores
                # it because only the live local TUI can decide permissions.
                tui_access_mode=payload.get("tui_access_mode", "unknown"),
                tui_confirmed=payload.get("tui_confirmed", False),
            ),
            success_status=201,
        )

    async def report_agent_connector_setup(request: Request) -> Response:
        return await _agent_json_call(
            request,
            store,
            required={"connector_id", "setup_status"},
            allowed={"connector_id", "setup_status", "detail"},
            operation=lambda auth, payload: {
                "connector": store.report_agent_connector_setup(
                    participant_id=auth["participant_id"],
                    authorized_session_id=auth["session_id"],
                    connector_id=payload["connector_id"],
                    setup_status=payload["setup_status"],
                    detail=payload.get("detail"),
                )
            },
        )

    async def report_agent_tui_state(request: Request) -> Response:
        return await _agent_json_call(
            request,
            store,
            required={
                "connector_id",
                "tui_endpoint_id",
                "tui_native_session_id",
                "state",
            },
            allowed={
                "connector_id",
                "tui_endpoint_id",
                "tui_native_session_id",
                "state",
                "access_mode",
                "capabilities",
                "active_task_id",
                "detail",
            },
            operation=lambda auth, payload: {
                "connector": store.report_agent_tui_state(
                    participant_id=auth["participant_id"],
                    authorized_session_id=auth["session_id"],
                    connector_id=payload["connector_id"],
                    tui_endpoint_id=payload["tui_endpoint_id"],
                    tui_native_session_id=payload["tui_native_session_id"],
                    state=payload["state"],
                    access_mode=payload.get("access_mode"),
                    capabilities=payload.get("capabilities"),
                    active_task_id=payload.get("active_task_id"),
                    detail=payload.get("detail"),
                )
            },
        )

    async def report_native_tui_delivery_stage(request: Request) -> Response:
        return await _agent_json_call(
            request,
            store,
            required={
                "connector_id",
                "tui_endpoint_id",
                "tui_native_session_id",
                "message_ids",
                "stage",
            },
            allowed={
                "connector_id",
                "tui_endpoint_id",
                "tui_native_session_id",
                "message_ids",
                "stage",
            },
            operation=lambda auth, payload: store.report_native_tui_delivery_stage(
                participant_id=auth["participant_id"],
                authorized_session_id=auth["session_id"],
                connector_id=payload["connector_id"],
                tui_endpoint_id=payload["tui_endpoint_id"],
                tui_native_session_id=payload["tui_native_session_id"],
                message_ids=payload["message_ids"],
                stage=payload["stage"],
            ),
        )

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

    async def agent_heartbeat(request: Request) -> Response:
        return await _agent_json_call(
            request,
            store,
            required=set(),
            allowed={"status"},
            operation=lambda auth, payload: store.heartbeat(
                auth["participant_id"],
                status=payload.get("status", "online"),
                authorized_session_id=auth["session_id"],
            ),
        )

    async def agent_update_profile(request: Request) -> Response:
        return await _agent_json_call(
            request,
            store,
            required=set(),
            allowed={"signature", "avatar_key"},
            operation=lambda auth, payload: store.update_profile(
                participant_id=auth["participant_id"],
                authorized_session_id=auth["session_id"],
                signature=payload.get("signature"),
                avatar_key=payload.get("avatar_key"),
            ),
        )

    async def agent_avatars(request: Request) -> Response:
        return await _agent_json_call(
            request,
            store,
            required=set(),
            allowed={"vendor"},
            operation=lambda _auth, payload: avatar_catalog_payload(
                vendor=payload.get("vendor"),
            ),
        )

    async def agent_request_nickname(request: Request) -> Response:
        return await _agent_json_call(
            request,
            store,
            required={"display_name"},
            allowed={"display_name"},
            operation=lambda auth, payload: store.request_nickname(
                participant_id=auth["participant_id"],
                authorized_session_id=auth["session_id"],
                requested_display_name=payload["display_name"],
            ),
        )

    async def agent_set_follow(request: Request) -> Response:
        return await _agent_json_call(
            request,
            store,
            required={"conversation_id", "followed_participant_id"},
            allowed={
                "conversation_id",
                "followed_participant_id",
                "following",
            },
            operation=lambda auth, payload: store.set_follow(
                participant_id=auth["participant_id"],
                authorized_session_id=auth["session_id"],
                conversation_id=payload["conversation_id"],
                followed_participant_id=payload["followed_participant_id"],
                following=payload.get("following", True),
            ),
        )

    async def agent_following(request: Request) -> Response:
        return await _agent_json_call(
            request,
            store,
            required={"conversation_id"},
            allowed={"conversation_id", "include_inactive"},
            operation=lambda auth, payload: store.following(
                participant_id=auth["participant_id"],
                authorized_session_id=auth["session_id"],
                conversation_id=payload["conversation_id"],
                include_inactive=payload.get("include_inactive", False),
            ),
        )

    async def agent_set_room_dnd(request: Request) -> Response:
        return await _agent_json_call(
            request,
            store,
            required={"conversation_id"},
            allowed={"conversation_id", "enabled"},
            operation=lambda auth, payload: store.set_room_dnd(
                participant_id=auth["participant_id"],
                authorized_session_id=auth["session_id"],
                conversation_id=payload["conversation_id"],
                enabled=payload.get("enabled", True),
            ),
        )

    async def agent_send(request: Request) -> Response:
        return await _agent_json_call(
            request,
            store,
            required={"conversation_id", "body"},
            allowed={
                "conversation_id",
                "body",
                "audience_kind",
                "audience_value",
                "reply_to",
                "refs",
                "mentions",
                "notification_mode",
            },
            operation=lambda auth, payload: store.send(
                authorized_session_id=auth["session_id"],
                sender_participant_id=auth["participant_id"],
                conversation_id=payload["conversation_id"],
                body_text=payload["body"],
                audience_kind=payload.get("audience_kind", "room"),
                audience_value=payload.get("audience_value", "*"),
                reply_to=payload.get("reply_to"),
                refs=payload.get("refs"),
                mentions=payload.get("mentions"),
                notification_mode=payload.get("notification_mode"),
            ),
        )

    async def agent_create_room(request: Request) -> Response:
        return await _agent_json_call(
            request,
            store,
            required={"conversation_id"},
            allowed={"conversation_id"},
            operation=lambda auth, payload: store.create_agent_room(
                authorized_session_id=auth["session_id"],
                participant_id=auth["participant_id"],
                conversation_id=payload["conversation_id"],
            ),
        )

    async def agent_wait(request: Request) -> Response:
        try:
            auth = _authenticate_request(request, store)
            payload = await _json_body(
                request,
                required=set(),
                allowed={
                    "wait_seconds",
                    "limit",
                    "auto_claim_roles",
                    "compact_optional_backlog",
                    "keep_recent_optional",
                },
            )
            compact_requested = payload.get("compact_optional_backlog", False)
            if not isinstance(compact_requested, bool):
                raise ValidationError("compact_optional_backlog must be boolean")
            compaction = None
            if compact_requested:
                compaction = await asyncio.to_thread(
                    store.compact_optional_backlog,
                    participant_id=auth["participant_id"],
                    authorized_session_id=auth["session_id"],
                    keep_recent=payload.get("keep_recent_optional", 20),
                )
            result = await asyncio.to_thread(
                store.wait_messages,
                participant_id=auth["participant_id"],
                authorized_session_id=auth["session_id"],
                wait_seconds=payload.get("wait_seconds", 30),
                limit=payload.get("limit", 20),
                auto_claim_roles=payload.get("auto_claim_roles", True),
            )
            if compaction is not None:
                result["offline_compaction"] = compaction
            return JSONResponse(result)
        except Exception as exc:
            return _json_error(exc)

    async def agent_compact_backlog(request: Request) -> Response:
        return await _agent_json_call(
            request,
            store,
            required=set(),
            allowed={"keep_recent_optional"},
            operation=lambda auth, payload: store.compact_optional_backlog(
                participant_id=auth["participant_id"],
                authorized_session_id=auth["session_id"],
                keep_recent=payload.get("keep_recent_optional", 20),
            ),
        )

    async def agent_notifications(request: Request) -> Response:
        return await _agent_json_call(
            request,
            store,
            required=set(),
            allowed={"after_sequence"},
            operation=lambda auth, payload: store.notification_snapshot(
                participant_id=auth["participant_id"],
                authorized_session_id=auth["session_id"],
                after_sequence=payload.get("after_sequence"),
            ),
        )

    async def agent_events(request: Request) -> Response:
        try:
            if policy.public_mode:
                enforce_rate(
                    request,
                    "agent-events-ip",
                    limit=120,
                    window_seconds=60,
                )
            auth = _authenticate_request(request, store)
            cursor = _event_cursor(request.headers.get("last-event-id"))
        except Exception as exc:
            return _json_error(exc)

        async def stream():
            nonlocal cursor
            try:
                if auth.get("connector_id"):
                    await asyncio.to_thread(
                        store.touch_agent_connector,
                        participant_id=auth["participant_id"],
                        authorized_session_id=auth["session_id"],
                        connector_id=auth["connector_id"],
                    )
                snapshot = await asyncio.to_thread(
                    store.notification_snapshot,
                    participant_id=auth["participant_id"],
                    authorized_session_id=auth["session_id"],
                    after_sequence=cursor,
                )
                cursor = int(snapshot["cursor"])
                yield _sse_event("backlog", snapshot, event_id=cursor)
                while not await request.is_disconnected():
                    snapshot = await asyncio.to_thread(
                        store.wait_for_notification,
                        participant_id=auth["participant_id"],
                        authorized_session_id=auth["session_id"],
                        after_sequence=cursor,
                        wait_seconds=20,
                    )
                    if auth.get("connector_id"):
                        await asyncio.to_thread(
                            store.touch_agent_connector,
                            participant_id=auth["participant_id"],
                            authorized_session_id=auth["session_id"],
                            connector_id=auth["connector_id"],
                        )
                    if snapshot["has_room_activity"]:
                        cursor = int(snapshot["cursor"])
                        yield _sse_event(
                            "message_available",
                            snapshot,
                            event_id=cursor,
                        )
                    else:
                        yield f": keepalive {int(time.time())}\n\n".encode()
            except (AuthenticationError, ConflictError, NotFoundError) as exc:
                yield _sse_event(
                    "session_closed",
                    {"error": str(exc)},
                    event_id=cursor,
                )

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    async def agent_action(request: Request) -> Response:
        return await _agent_json_call(
            request,
            store,
            required={"message_id", "action"},
            allowed={"message_id", "action", "lease_seconds"},
            operation=lambda auth, payload: store.message_action(
                participant_id=auth["participant_id"],
                message_id=payload["message_id"],
                action=payload["action"],
                lease_seconds=payload.get("lease_seconds", 120),
                authorized_session_id=auth["session_id"],
            ),
        )

    async def agent_reply(request: Request) -> Response:
        return await _agent_json_call(
            request,
            store,
            required={"message_id", "body"},
            allowed={"message_id", "body", "refs", "mentions"},
            operation=lambda auth, payload: store.reply(
                authorized_session_id=auth["session_id"],
                participant_id=auth["participant_id"],
                message_id=payload["message_id"],
                body_text=payload["body"],
                refs=payload.get("refs"),
                mentions=payload.get("mentions"),
            ),
        )

    async def agent_history(request: Request) -> Response:
        return await _agent_json_call(
            request,
            store,
            required={"conversation_id"},
            allowed={
                "conversation_id",
                "limit",
                "before_sequence",
                "after_sequence",
                "around_sequence",
            },
            operation=lambda auth, payload: store.history(
                participant_id=auth["participant_id"],
                conversation_id=payload["conversation_id"],
                limit=payload.get("limit", 50),
                before_sequence=payload.get("before_sequence"),
                after_sequence=payload.get("after_sequence"),
                around_sequence=payload.get("around_sequence"),
                authorized_session_id=auth["session_id"],
            ),
        )

    async def agent_search_history(request: Request) -> Response:
        return await _agent_json_call(
            request,
            store,
            required={"conversation_id"},
            allowed={
                "conversation_id",
                "query",
                "message_id",
                "sequence",
                "sender_participant_id",
                "created_after",
                "created_before",
                "limit",
            },
            operation=lambda auth, payload: store.search_history(
                participant_id=auth["participant_id"],
                authorized_session_id=auth["session_id"],
                conversation_id=payload["conversation_id"],
                query=payload.get("query", ""),
                message_id=payload.get("message_id"),
                sequence=payload.get("sequence"),
                sender_participant_id=payload.get("sender_participant_id"),
                created_after=payload.get("created_after"),
                created_before=payload.get("created_before"),
                limit=payload.get("limit", 10),
            ),
        )

    async def agent_participants(request: Request) -> Response:
        return await _agent_json_call(
            request,
            store,
            required={"conversation_id"},
            allowed={"conversation_id", "include_offline"},
            operation=lambda auth, payload: store.participants(
                participant_id=auth["participant_id"],
                conversation_id=payload["conversation_id"],
                include_offline=payload.get("include_offline", True),
                authorized_session_id=auth["session_id"],
            ),
        )

    async def agent_task_next(request: Request) -> Response:
        try:
            auth = _authenticate_request(request, store)
            payload = await _json_body(
                request,
                required=set(),
                allowed={"wait_seconds"},
            )
            result = await asyncio.to_thread(
                store.wait_next_task,
                participant_id=auth["participant_id"],
                authorized_session_id=auth["session_id"],
                wait_seconds=payload.get("wait_seconds", 20),
            )
            if auth.get("connector_id"):
                await asyncio.to_thread(
                    store.touch_agent_connector,
                    participant_id=auth["participant_id"],
                    authorized_session_id=auth["session_id"],
                    connector_id=auth["connector_id"],
                )
            return JSONResponse(result)
        except Exception as exc:
            return _json_error(exc)

    async def agent_task_inputs(request: Request) -> Response:
        try:
            auth = _authenticate_request(request, store)
            payload = await _json_body(
                request,
                required={"task_id", "action"},
                allowed={"task_id", "action", "input_ids", "limit"},
            )
            action = str(payload["action"] or "").strip().lower()
            if action == "poll":
                result = await asyncio.to_thread(
                    store.poll_agent_task_inputs,
                    participant_id=auth["participant_id"],
                    authorized_session_id=auth["session_id"],
                    task_id=payload["task_id"],
                    limit=payload.get("limit", 50),
                )
            elif action == "ack":
                input_ids = payload.get("input_ids")
                if not isinstance(input_ids, list):
                    raise ValidationError("input_ids must be a list")
                result = await asyncio.to_thread(
                    store.acknowledge_agent_task_inputs,
                    participant_id=auth["participant_id"],
                    authorized_session_id=auth["session_id"],
                    task_id=payload["task_id"],
                    input_ids=input_ids,
                )
            else:
                raise ValidationError("unsupported task input action")
            if auth.get("connector_id"):
                await asyncio.to_thread(
                    store.touch_agent_connector,
                    participant_id=auth["participant_id"],
                    authorized_session_id=auth["session_id"],
                    connector_id=auth["connector_id"],
                )
            return JSONResponse(result)
        except Exception as exc:
            return _json_error(exc)

    async def agent_task_update(request: Request) -> Response:
        return await _agent_json_call(
            request,
            store,
            required={"task_id", "status"},
            allowed={
                "task_id",
                "status",
                "result_summary",
                "execution_cwd",
                "execution_thread_id",
            },
            operation=lambda auth, payload: {
                "task": store.update_agent_task(
                    participant_id=auth["participant_id"],
                    authorized_session_id=auth["session_id"],
                    task_id=payload["task_id"],
                    status=payload["status"],
                    result_summary=payload.get("result_summary"),
                    execution_cwd=payload.get("execution_cwd"),
                    execution_thread_id=payload.get("execution_thread_id"),
                )
            },
        )

    async def agent_task_delegate(request: Request) -> Response:
        return await _agent_json_call(
            request,
            store,
            required={"parent_task_id", "body", "target_participant_ids"},
            allowed={"parent_task_id", "body", "target_participant_ids"},
            operation=lambda auth, payload: {
                "task": store.delegate_agent_task(
                    participant_id=auth["participant_id"],
                    authorized_session_id=auth["session_id"],
                    parent_task_id=payload["parent_task_id"],
                    body_text=payload["body"],
                    target_participant_ids=payload["target_participant_ids"],
                )
            },
        )

    return [
            Route("/agent/register", register_agent, methods=["POST"]),
            Route(
                "/agent/connector/enrollment/rotate",
                rotate_agent_enrollment,
                methods=["POST"],
            ),
            Route(
                "/agent/invitations/accept",
                accept_agent_invitation,
                methods=["POST"],
            ),
            Route(
                "/agent/connector/setup",
                report_agent_connector_setup,
                methods=["POST"],
            ),
            Route(
                "/agent/connector/tui-state",
                report_agent_tui_state,
                methods=["POST"],
            ),
            Route(
                "/agent/connector/tui-delivery-stage",
                report_native_tui_delivery_stage,
                methods=["POST"],
            ),
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
            Route("/agent/heartbeat", agent_heartbeat, methods=["POST"]),
            Route("/agent/profile", agent_update_profile, methods=["POST"]),
            Route("/agent/avatars", agent_avatars, methods=["POST"]),
            Route(
                "/agent/nickname/request",
                agent_request_nickname,
                methods=["POST"],
            ),
            Route("/agent/follow", agent_set_follow, methods=["POST"]),
            Route("/agent/following", agent_following, methods=["POST"]),
            Route("/agent/room-dnd", agent_set_room_dnd, methods=["POST"]),
            Route("/agent/send", agent_send, methods=["POST"]),
            Route("/agent/rooms/create", agent_create_room, methods=["POST"]),
            Route("/agent/wait", agent_wait, methods=["POST"]),
            Route(
                "/agent/backlog/compact",
                agent_compact_backlog,
                methods=["POST"],
            ),
            Route("/agent/notifications", agent_notifications, methods=["POST"]),
            Route("/agent/events", agent_events, methods=["GET"]),
            Route("/agent/action", agent_action, methods=["POST"]),
            Route("/agent/reply", agent_reply, methods=["POST"]),
            Route("/agent/history", agent_history, methods=["POST"]),
            Route(
                "/agent/history/search",
                agent_search_history,
                methods=["POST"],
            ),
            Route("/agent/participants", agent_participants, methods=["POST"]),
            Route("/agent/tasks/next", agent_task_next, methods=["POST"]),
            Route("/agent/tasks/inputs", agent_task_inputs, methods=["POST"]),
            Route("/agent/tasks/update", agent_task_update, methods=["POST"]),
            Route("/agent/tasks/delegate", agent_task_delegate, methods=["POST"]),
    ]
