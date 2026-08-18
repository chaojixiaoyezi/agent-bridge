"""Agent profile, room chat, notification, and history routes."""

from __future__ import annotations

import asyncio
import base64
import time

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
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
    _agent_json_call,
    _authenticate_request,
    _event_cursor,
    _json_body,
    _json_error,
    _sse_event,
)


def build_agent_chat_routes(
    *,
    store: BridgeStore,
    policy: ViewerSecurityPolicy,
    enforce_rate,
) -> list[Route]:
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
                "links",
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
                links=payload.get("links"),
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

    async def agent_download_attachment(request: Request) -> Response:
        try:
            auth = _authenticate_request(request, store)
            payload = await _json_body(
                request,
                required={"attachment_id"},
                allowed={"attachment_id"},
            )
            record = store.attachment_record(
                attachment_id=payload["attachment_id"],
                participant_id=auth["participant_id"],
                authorized_session_id=auth["session_id"],
            )
            encoded_filename = base64.urlsafe_b64encode(
                str(record["filename"]).encode("utf-8")
            ).decode("ascii")
            return FileResponse(
                record["path"],
                media_type="application/octet-stream",
                filename=record["filename"],
                content_disposition_type="attachment",
                headers={
                    "Cache-Control": "private, no-store",
                    "X-Content-Type-Options": "nosniff",
                    "X-Attachment-Filename-B64": encoded_filename,
                    "X-Attachment-Media-Type": str(record["media_type"]),
                    "X-Attachment-Kind": str(record["kind"]),
                    "X-Attachment-Size": str(record["size_bytes"]),
                    "X-Attachment-SHA256": str(record["sha256"]),
                },
            )
        except Exception as exc:
            return _json_error(exc)

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

    return [
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
            Route(
                "/agent/attachments/download",
                agent_download_attachment,
                methods=["POST"],
            ),
            Route("/agent/participants", agent_participants, methods=["POST"]),
    ]
