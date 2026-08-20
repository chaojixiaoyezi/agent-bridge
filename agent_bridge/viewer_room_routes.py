from __future__ import annotations

import asyncio
import json

from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from .resident_health import local_resident_snapshot, room_resident_detail
from .message_assets import (
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS_TOTAL_BYTES,
    MAX_MESSAGE_ATTACHMENTS,
)
from .security import ViewerSecurityPolicy
from .store import BridgeStore
from .validation import opaque_id
from .viewer_http import (
    _int_query,
    _json_body,
    _json_call,
    _json_error,
    _optional_float_query,
    _optional_positive_int_query,
)
from .viewer_store import ViewerRepository


def build_room_routes(
    *,
    store: BridgeStore,
    repository: ViewerRepository,
    policy: ViewerSecurityPolicy,
    authenticated_web_user,
    authenticated_admin,
    web_room_access_scope,
    require_web_room_access,
    require_web_intent,
    enforce_rate,
) -> list[Route]:
    async def pending_responses(request: Request) -> Response:
        try:
            identity = authenticated_web_user(request)
            access_scope = web_room_access_scope(identity)
            visible_rooms = access_scope["conversation_ids"]
            if visible_rooms is None:
                managed_rooms = None
            else:
                permissions = store.room_web_permissions_bulk(
                    requesting_web_user_id=str(identity["user_id"]),
                    conversation_ids=visible_rooms,
                )
                managed_rooms = [
                    conversation_id
                    for conversation_id, room_permissions in permissions.items()
                    if room_permissions["can_manage_web_members"]
                ]
            return JSONResponse(
                repository.pending_response_center(
                    participant_id=str(identity["participant_id"]),
                    visible_conversation_ids=visible_rooms,
                    managed_conversation_ids=managed_rooms,
                    limit=_int_query(request, "limit", default=100, maximum=200),
                )
            )
        except Exception as exc:
            return _json_error(exc)

    async def acknowledge_pending_responses(request: Request) -> Response:
        try:
            require_web_intent(
                request,
                intent="acknowledge-pending-responses",
            )
            identity = authenticated_web_user(request)
            conversation = request.path_params["conversation_id"]
            require_web_room_access(identity, conversation)
            payload = await _json_body(
                request,
                required={"message_ids"},
                allowed={"message_ids"},
            )
            raw_message_ids = payload["message_ids"]
            if not isinstance(raw_message_ids, list):
                raise ValueError("message_ids must be a list")
            result = store.acknowledge_web_pending_messages(
                participant_id=str(identity["participant_id"]),
                conversation_id=conversation,
                message_ids=[
                    opaque_id(message_id, field="message_id")
                    for message_id in raw_message_ids
                ],
            )
            return JSONResponse({"acknowledgement": result})
        except Exception as exc:
            return _json_error(exc)

    async def messages(request: Request) -> Response:
        try:
            identity = authenticated_web_user(request)
            require_web_room_access(
                identity,
                request.path_params["conversation_id"],
            )
        except Exception as exc:
            return _json_error(exc)
        before = request.query_params.get("before_sequence")
        after = request.query_params.get("after_sequence")
        around = request.query_params.get("around_sequence")
        if sum(value is not None for value in (before, after, around)) > 1:
            return JSONResponse(
                {
                    "error": "before_sequence, after_sequence, and "
                    "around_sequence cannot be combined"
                },
                status_code=400,
            )
        limit = _int_query(request, "limit", default=300, maximum=500)
        try:
            around_sequence = int(around) if around is not None else None
            page = repository.messages(
                request.path_params["conversation_id"],
                limit=limit if around_sequence is not None else limit + 1,
                before_sequence=int(before) if before is not None else None,
                after_sequence=int(after) if after is not None else None,
                around_sequence=around_sequence,
            )
            if around_sequence is not None:
                bounds = repository.message_window_bounds(
                    request.path_params["conversation_id"],
                    first_sequence=page[0]["sequence"] if page else None,
                    last_sequence=page[-1]["sequence"] if page else None,
                )
            else:
                bounds = None
        except Exception as exc:
            return _json_error(exc)
        has_more = (
            bool(bounds["has_earlier"] or bounds["has_later"])
            if bounds is not None
            else len(page) > limit
        )
        if bounds is None and has_more:
            page = page[:limit] if after is not None else page[-limit:]
        has_earlier = (
            bool(bounds["has_earlier"])
            if bounds is not None
            else bool(has_more and after is None)
        )
        has_later = (
            bool(bounds["has_later"])
            if bounds is not None
            else bool(has_more and after is not None)
        )
        return _json_call(
            lambda: {
                "conversation_id": request.path_params["conversation_id"],
                "messages": page,
                "first_sequence": page[0]["sequence"] if page else None,
                "last_sequence": page[-1]["sequence"] if page else None,
                "has_more": has_more,
                "has_earlier": has_earlier,
                "has_later": has_later,
                "around_sequence": around_sequence,
            }
        )

    async def room_message_thread(request: Request) -> Response:
        try:
            identity = authenticated_web_user(request)
            conversation = request.path_params["conversation_id"]
            require_web_room_access(identity, conversation)
            return JSONResponse(
                repository.message_thread(
                    conversation,
                    request.path_params["message_id"],
                    limit=_int_query(request, "limit", default=200, maximum=500),
                )
            )
        except Exception as exc:
            return _json_error(exc)

    async def room_highlights(request: Request) -> Response:
        try:
            identity = authenticated_web_user(request)
            conversation = request.path_params["conversation_id"]
            require_web_room_access(identity, conversation)
            return JSONResponse(
                repository.room_highlights(
                    conversation,
                    limit=_int_query(request, "limit", default=200, maximum=500),
                )
            )
        except Exception as exc:
            return _json_error(exc)

    async def room_message_marker(request: Request) -> Response:
        try:
            require_web_intent(request, intent="manage-room-highlight")
            identity = authenticated_web_user(request)
            conversation = request.path_params["conversation_id"]
            require_web_room_access(identity, conversation)
            if request.method == "PUT":
                payload = await _json_body(
                    request,
                    required=set(),
                    allowed={"note"},
                )
                marker = store.set_room_message_marker(
                    conversation_id=conversation,
                    message_id=request.path_params["message_id"],
                    marker_kind=request.path_params["marker_kind"],
                    note=payload.get("note"),
                    requesting_web_user_id=str(identity["user_id"]),
                )
            else:
                marker = store.remove_room_message_marker(
                    conversation_id=conversation,
                    message_id=request.path_params["message_id"],
                    marker_kind=request.path_params["marker_kind"],
                    requesting_web_user_id=str(identity["user_id"]),
                )
            return JSONResponse({"marker": marker})
        except Exception as exc:
            return _json_error(exc)

    async def search_room_messages(request: Request) -> Response:
        try:
            identity = authenticated_web_user(request)
            require_web_room_access(
                identity,
                request.path_params["conversation_id"],
            )
            if policy.public_mode:
                enforce_rate(
                    request,
                    "room-search-ip",
                    limit=120,
                    window_seconds=60,
                )
            sender = request.query_params.get("sender_participant_id")
            payload = repository.search_messages(
                request.path_params["conversation_id"],
                query=request.query_params.get("q", ""),
                sender_participant_id=(
                    opaque_id(sender, field="sender_participant_id") if sender else None
                ),
                message_kind=request.query_params.get("message_kind"),
                notification_mode=request.query_params.get("notification_mode"),
                thread_scope=request.query_params.get("thread_scope"),
                marker_kind=request.query_params.get("marker_kind"),
                room_sequence=_optional_positive_int_query(
                    request,
                    "room_sequence",
                ),
                created_after=_optional_float_query(request, "created_after"),
                created_before=_optional_float_query(request, "created_before"),
                before_sequence=(
                    int(request.query_params["before_sequence"])
                    if "before_sequence" in request.query_params
                    else None
                ),
                limit=_int_query(request, "limit", default=25, maximum=50),
            )
            return JSONResponse(payload)
        except Exception as exc:
            return _json_error(exc)

    async def message_receipts(request: Request) -> Response:
        try:
            identity = authenticated_web_user(request)
            require_web_room_access(
                identity,
                request.path_params["conversation_id"],
            )
            after_raw = request.query_params.get("after_sequence")
            after = max(0, min(int(after_raw or 0), 2_147_483_647))
            limit = _int_query(request, "limit", default=500, maximum=1_000)
            receipts = repository.message_receipts(
                request.path_params["conversation_id"],
                after_sequence=after,
                limit=limit,
            )
        except Exception as exc:
            return _json_error(exc)
        return JSONResponse(
            {
                "conversation_id": request.path_params["conversation_id"],
                "receipts": receipts,
            }
        )

    async def web_send_message(request: Request) -> Response:
        try:
            require_web_intent(request, intent="send-message")
            identity = authenticated_web_user(request)
            conversation = request.path_params["conversation_id"]
            require_web_room_access(identity, conversation)
            content_type = request.headers.get("content-type", "").split(";", 1)[0]
            attachments: list[dict[str, object]] = []
            if content_type.strip().lower() == "multipart/form-data":
                if policy.public_mode:
                    enforce_rate(
                        request,
                        "web-attachment-upload",
                        subject=identity["user_id"],
                        limit=12,
                        window_seconds=60,
                    )
                async with request.form(
                    max_files=MAX_MESSAGE_ATTACHMENTS,
                    max_fields=12,
                    max_part_size=MAX_ATTACHMENT_BYTES + 1,
                ) as form:
                    allowed_fields = {
                        "body",
                        "mentions",
                        "links",
                        "reply_to",
                        "wake_all_agents",
                        "files",
                    }
                    unsupported = {
                        key for key, _value in form.multi_items()
                        if key not in allowed_fields
                    }
                    if unsupported:
                        raise ValueError(
                            "unsupported fields: " + ", ".join(sorted(unsupported))
                        )

                    def structured_array(field: str) -> list:
                        raw = str(form.get(field) or "[]")
                        try:
                            value = json.loads(raw)
                        except json.JSONDecodeError as exc:
                            raise ValueError(f"{field} must be a JSON array") from exc
                        if not isinstance(value, list):
                            raise ValueError(f"{field} must be a JSON array")
                        return value

                    wake_raw = str(form.get("wake_all_agents") or "false").lower()
                    if wake_raw not in {"true", "false", "1", "0"}:
                        raise ValueError("wake_all_agents must be a boolean")
                    payload = {
                        "body": str(form.get("body") or ""),
                        "mentions": structured_array("mentions"),
                        "links": structured_array("links"),
                        "reply_to": str(form.get("reply_to") or "") or None,
                        "wake_all_agents": wake_raw in {"true", "1"},
                    }
                    total_size = 0
                    for uploaded in form.getlist("files"):
                        if not isinstance(uploaded, UploadFile):
                            raise ValueError("files must use multipart file parts")
                        content = await uploaded.read(MAX_ATTACHMENT_BYTES + 1)
                        if len(content) > MAX_ATTACHMENT_BYTES:
                            raise ValueError(
                                f"each attachment must be at most {MAX_ATTACHMENT_BYTES} bytes"
                            )
                        total_size += len(content)
                        if total_size > MAX_ATTACHMENTS_TOTAL_BYTES:
                            raise ValueError(
                                "attachment total exceeds "
                                f"{MAX_ATTACHMENTS_TOTAL_BYTES} bytes"
                            )
                        attachments.append(
                            {
                                "filename": uploaded.filename,
                                "media_type": uploaded.content_type,
                                "content": content,
                            }
                        )
            else:
                payload = await _json_body(
                    request,
                    required=set(),
                    allowed={
                        "body",
                        "mentions",
                        "links",
                        "reply_to",
                        "wake_all_agents",
                    },
                )
            return JSONResponse(
                {
                    "message": store.send_web_message(
                        authorized_session_id=str(identity["session_id"]),
                        participant_id=str(identity["participant_id"]),
                        conversation_id=conversation,
                        body_text=payload.get("body", ""),
                        mentions=payload.get("mentions"),
                        links=payload.get("links"),
                        attachments=attachments,
                        reply_to=payload.get("reply_to"),
                        wake_all_agents=payload.get("wake_all_agents", False),
                    )
                },
                status_code=201,
            )
        except Exception as exc:
            return _json_error(exc)

    async def web_attachment(request: Request) -> Response:
        try:
            identity = authenticated_web_user(request)
            conversation = request.path_params["conversation_id"]
            require_web_room_access(identity, conversation)
            record = store.attachment_record(
                attachment_id=request.path_params["attachment_id"],
                conversation_id=conversation,
            )
            return FileResponse(
                record["path"],
                media_type=record["media_type"],
                filename=record["filename"],
                content_disposition_type=(
                    "inline" if record["kind"] == "image" else "attachment"
                ),
                headers={
                    "Cache-Control": "private, no-store",
                    "X-Content-Type-Options": "nosniff",
                    "X-Attachment-SHA256": str(record["sha256"]),
                },
            )
        except Exception as exc:
            return _json_error(exc)

    async def web_send_task(request: Request) -> Response:
        try:
            require_web_intent(request, intent="send-task")
            identity = authenticated_web_user(request)
            payload = await _json_body(
                request,
                required={"body"},
                allowed={"body", "target_participant_ids", "reply_to"},
            )
            return JSONResponse(
                {
                    "message": store.send_web_task(
                        authorized_session_id=str(identity["session_id"]),
                        participant_id=str(identity["participant_id"]),
                        conversation_id=request.path_params["conversation_id"],
                        body_text=payload["body"],
                        target_participant_ids=payload.get("target_participant_ids"),
                        reply_to=payload.get("reply_to"),
                    )
                },
                status_code=201,
            )
        except Exception as exc:
            return _json_error(exc)

    async def convert_web_message_to_task(request: Request) -> Response:
        try:
            require_web_intent(request, intent="convert-message-to-task")
            identity = authenticated_web_user(request)
            payload = await _json_body(
                request,
                required=set(),
                allowed={"target_participant_ids"},
            )
            return JSONResponse(
                {
                    "message": store.convert_web_message_to_task(
                        authorized_session_id=str(identity["session_id"]),
                        participant_id=str(identity["participant_id"]),
                        message_id=request.path_params["message_id"],
                        target_participant_ids=payload.get("target_participant_ids"),
                    )
                }
            )
        except Exception as exc:
            return _json_error(exc)

    async def room_wake_policy(request: Request) -> Response:
        try:
            identity = authenticated_web_user(request)
            conversation = request.path_params["conversation_id"]
            if request.method == "GET":
                return JSONResponse(
                    store.room_wake_policy(
                        authorized_session_id=str(identity["session_id"]),
                        participant_id=str(identity["participant_id"]),
                        conversation_id=conversation,
                    )
                )
            require_web_intent(request, intent="manage-wake-policy")
            payload = await _json_body(
                request,
                required={"mode"},
                allowed={
                    "mode",
                    "digest_min_messages",
                    "digest_after_seconds",
                },
            )
            return JSONResponse(
                store.update_room_wake_policy(
                    authorized_session_id=str(identity["session_id"]),
                    participant_id=str(identity["participant_id"]),
                    conversation_id=conversation,
                    mode=payload["mode"],
                    digest_min_messages=payload.get("digest_min_messages", 5),
                    digest_after_seconds=payload.get(
                        "digest_after_seconds",
                        300,
                    ),
                )
            )
        except Exception as exc:
            return _json_error(exc)

    async def room_task_permissions(request: Request) -> Response:
        try:
            identity = authenticated_web_user(request)
            return JSONResponse(
                store.room_task_permissions(
                    authorized_session_id=str(identity["session_id"]),
                    participant_id=str(identity["participant_id"]),
                    conversation_id=request.path_params["conversation_id"],
                )
            )
        except Exception as exc:
            return _json_error(exc)

    async def update_room_task_policy(request: Request) -> Response:
        try:
            require_web_intent(request, intent="manage-task-permissions")
            identity = authenticated_web_user(request)
            payload = await _json_body(
                request,
                required={"allow_global_admin"},
                allowed={"allow_global_admin"},
            )
            return JSONResponse(
                store.update_room_task_policy(
                    authorized_session_id=str(identity["session_id"]),
                    participant_id=str(identity["participant_id"]),
                    conversation_id=request.path_params["conversation_id"],
                    allow_global_admin=payload["allow_global_admin"],
                )
            )
        except Exception as exc:
            return _json_error(exc)

    async def update_room_task_grant(request: Request) -> Response:
        try:
            require_web_intent(request, intent="manage-task-permissions")
            identity = authenticated_web_user(request)
            payload = await _json_body(
                request,
                required={"can_assign_tasks", "can_cancel_tasks"},
                allowed={"can_assign_tasks", "can_cancel_tasks"},
            )
            return JSONResponse(
                store.update_room_task_grant(
                    authorized_session_id=str(identity["session_id"]),
                    participant_id=str(identity["participant_id"]),
                    conversation_id=request.path_params["conversation_id"],
                    target_web_user_id=request.path_params["user_id"],
                    can_assign_tasks=payload["can_assign_tasks"],
                    can_cancel_tasks=payload["can_cancel_tasks"],
                )
            )
        except Exception as exc:
            return _json_error(exc)

    async def cancel_web_task(request: Request) -> Response:
        try:
            require_web_intent(request, intent="cancel-task")
            identity = authenticated_web_user(request)
            return JSONResponse(
                {
                    "task": store.cancel_web_task(
                        authorized_session_id=str(identity["session_id"]),
                        participant_id=str(identity["participant_id"]),
                        task_id=request.path_params["task_id"],
                    )
                }
            )
        except Exception as exc:
            return _json_error(exc)

    async def forward_web_message(request: Request) -> Response:
        try:
            require_web_intent(request, intent="forward-message")
            identity = authenticated_admin(request)
            payload = await _json_body(
                request,
                required={"target_conversation_id"},
                allowed={"target_conversation_id", "note"},
            )
            return JSONResponse(
                {
                    "message": store.forward_web_message(
                        authorized_session_id=str(identity["session_id"]),
                        participant_id=str(identity["participant_id"]),
                        source_message_id=request.path_params["message_id"],
                        target_conversation_id=payload["target_conversation_id"],
                        note=payload.get("note"),
                    )
                },
                status_code=201,
            )
        except Exception as exc:
            return _json_error(exc)

    async def revoke_chat_authorization(request: Request) -> Response:
        try:
            require_web_intent(request, intent="revoke-chat-authorization")
            identity = authenticated_admin(request)
            payload = await _json_body(
                request,
                required=set(),
                allowed={"reason"},
            )
            return JSONResponse(
                {
                    "authorization": store.revoke_chat_authorization(
                        source_message_id=request.path_params["message_id"],
                        revoked_by_web_user_id=str(identity["user_id"]),
                        reason=payload.get("reason"),
                    )
                }
            )
        except Exception as exc:
            return _json_error(exc)

    async def participants(request: Request) -> Response:
        try:
            identity = authenticated_web_user(request)
            require_web_room_access(
                identity,
                request.path_params["conversation_id"],
            )
            projected = repository.participants(request.path_params["conversation_id"])
            local_residents = await asyncio.to_thread(local_resident_snapshot)
            for participant in projected:
                connector_id = str(participant.get("connector_id") or "")
                room_local = room_resident_detail(
                    local_residents,
                    client_type=str(participant["client_type"]),
                    connector_id=connector_id or None,
                    conversation_id=request.path_params["conversation_id"],
                )
                if room_local is None:
                    continue
                participant["local_resident"] = room_local
                if room_local["resident_status"] == "online":
                    participant["resident_status"] = "online"
            return JSONResponse(
                {
                    "conversation_id": request.path_params["conversation_id"],
                    "participants": projected,
                }
            )
        except Exception as exc:
            return _json_error(exc)

    return [
        Route("/api/pending-responses", pending_responses, methods=["GET"]),
        Route(
            "/api/rooms/{conversation_id:str}/pending-responses/acknowledge",
            acknowledge_pending_responses,
            methods=["POST"],
        ),
        Route(
            "/api/rooms/{conversation_id:str}/messages",
            messages,
            methods=["GET"],
        ),
        Route(
            "/api/rooms/{conversation_id:str}/threads/{message_id:str}",
            room_message_thread,
            methods=["GET"],
        ),
        Route(
            "/api/rooms/{conversation_id:str}/highlights",
            room_highlights,
            methods=["GET"],
        ),
        Route(
            "/api/rooms/{conversation_id:str}/messages/{message_id:str}/"
            "markers/{marker_kind:str}",
            room_message_marker,
            methods=["PUT", "DELETE"],
        ),
        Route(
            "/api/rooms/{conversation_id:str}/search",
            search_room_messages,
            methods=["GET"],
        ),
        Route(
            "/api/rooms/{conversation_id:str}/receipts",
            message_receipts,
            methods=["GET"],
        ),
        Route(
            "/api/rooms/{conversation_id:str}/messages",
            web_send_message,
            methods=["POST"],
        ),
        Route(
            "/api/rooms/{conversation_id:str}/attachments/{attachment_id:str}",
            web_attachment,
            methods=["GET"],
        ),
        Route(
            "/api/rooms/{conversation_id:str}/tasks",
            web_send_task,
            methods=["POST"],
        ),
        Route(
            "/api/messages/{message_id:str}/convert-to-task",
            convert_web_message_to_task,
            methods=["POST"],
        ),
        Route(
            "/api/rooms/{conversation_id:str}/wake-policy",
            room_wake_policy,
            methods=["GET", "PATCH"],
        ),
        Route(
            "/api/rooms/{conversation_id:str}/task-permissions",
            room_task_permissions,
            methods=["GET"],
        ),
        Route(
            "/api/rooms/{conversation_id:str}/task-policy",
            update_room_task_policy,
            methods=["PATCH"],
        ),
        Route(
            "/api/rooms/{conversation_id:str}/task-grants/{user_id:str}",
            update_room_task_grant,
            methods=["PUT"],
        ),
        Route(
            "/api/tasks/{task_id:str}/cancel",
            cancel_web_task,
            methods=["POST"],
        ),
        Route(
            "/api/messages/{message_id:str}/authorization/revoke",
            revoke_chat_authorization,
            methods=["POST"],
        ),
        Route(
            "/api/messages/{message_id:str}/forward",
            forward_web_message,
            methods=["POST"],
        ),
        Route(
            "/api/rooms/{conversation_id:str}/participants",
            participants,
            methods=["GET"],
        ),
    ]
