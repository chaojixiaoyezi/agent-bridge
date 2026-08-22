"""Room and Web-account administration routes."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .store import BridgeStore
from .validation import ValidationError
from .viewer_http import _int_query, _json_body, _json_call, _json_error
from .viewer_store import ViewerRepository
from .web_auth import WebAuthStore


def build_admin_access_routes(
    *,
    store: BridgeStore,
    repository: ViewerRepository,
    web_auth: WebAuthStore,
    authenticated_web_user,
    authenticated_admin,
    web_room_access_scope,
    require_web_intent,
) -> list[Route]:
    async def rooms(request: Request) -> Response:
        try:
            identity = authenticated_web_user(request)
        except Exception as exc:
            return _json_error(exc)

        def payload() -> dict:
            access_scope = web_room_access_scope(identity)
            projected = repository.rooms(
                limit=_int_query(request, "limit", default=200, maximum=500),
                visible_conversation_ids=access_scope["conversation_ids"],
            )
            user_id = str(identity["user_id"])
            room_permissions = store.room_web_permissions_bulk(
                requesting_web_user_id=user_id,
                conversation_ids=[str(room["conversation_id"]) for room in projected],
            )
            task_permissions = store.room_task_permissions_bulk(
                authorized_session_id=str(identity["session_id"]),
                participant_id=str(identity["participant_id"]),
                conversation_ids=[str(room["conversation_id"]) for room in projected],
            )
            wake_policies = store.room_wake_policies_bulk(
                conversation_ids=[str(room["conversation_id"]) for room in projected]
            )
            for room in projected:
                room.update(room_permissions[str(room["conversation_id"])])
                permissions = task_permissions[str(room["conversation_id"])]
                room.update(
                    {
                        key: permissions[key]
                        for key in (
                            "can_assign_tasks",
                            "can_cancel_tasks",
                            "can_manage_task_permissions",
                            "allow_global_admin",
                        )
                    }
                )
                room["wake_policy"] = wake_policies[str(room["conversation_id"])]
            return {"rooms": projected}

        return _json_call(payload, before=store.archive_stale_rooms)

    async def create_room(request: Request) -> Response:
        try:
            require_web_intent(request, intent="create-room")
            identity = authenticated_web_user(request)
            payload = await _json_body(
                request,
                required={"conversation_id"},
                allowed={"conversation_id", "room_kind"},
            )
            return JSONResponse(
                {
                    "room": store.create_web_user_room(
                        authorized_session_id=str(identity["session_id"]),
                        web_user_id=str(identity["user_id"]),
                        participant_id=str(identity["participant_id"]),
                        conversation_id=payload["conversation_id"],
                        room_kind=payload.get("room_kind", "chat"),
                    )
                },
                status_code=201,
            )
        except Exception as exc:
            return _json_error(exc)

    async def web_user_room_permissions(request: Request) -> Response:
        try:
            identity = authenticated_admin(request)
            return JSONResponse(
                store.search_web_user_room_permissions(
                    requesting_web_user_id=str(identity["user_id"]),
                    query=str(request.query_params.get("query") or ""),
                    limit=_int_query(request, "limit", default=50, maximum=100),
                )
            )
        except Exception as exc:
            return _json_error(exc)

    async def web_registration_codes(request: Request) -> Response:
        try:
            identity = authenticated_admin(request)
            if request.method == "GET":
                return JSONResponse(
                    web_auth.list_registration_codes(
                        requesting_web_user_id=str(identity["user_id"]),
                        limit=_int_query(request, "limit", default=100, maximum=200),
                    )
                )
            require_web_intent(request, intent="create-registration-code")
            payload = await _json_body(
                request,
                required=set(),
                allowed={"label", "max_uses", "expires_in_hours"},
            )
            expires_in_hours = payload.get("expires_in_hours", 24)
            if isinstance(expires_in_hours, bool):
                raise ValidationError("expires_in_hours must be a number")
            try:
                expires_in_seconds = float(expires_in_hours) * 60 * 60
            except (TypeError, ValueError) as exc:
                raise ValidationError("expires_in_hours must be a number") from exc
            return JSONResponse(
                {
                    "registration_code": web_auth.create_registration_code(
                        created_by_web_user_id=str(identity["user_id"]),
                        label=payload.get("label", ""),
                        max_uses=payload.get("max_uses", 1),
                        expires_in_seconds=expires_in_seconds,
                    )
                },
                status_code=201,
            )
        except Exception as exc:
            return _json_error(exc)

    async def revoke_web_registration_code(request: Request) -> Response:
        try:
            require_web_intent(request, intent="revoke-registration-code")
            identity = authenticated_admin(request)
            return JSONResponse(
                {
                    "registration_code": web_auth.revoke_registration_code(
                        code_id=request.path_params["code_id"],
                        revoked_by_web_user_id=str(identity["user_id"]),
                    )
                }
            )
        except Exception as exc:
            return _json_error(exc)

    async def update_web_user_room_permission(request: Request) -> Response:
        try:
            require_web_intent(request, intent="manage-room-permission")
            identity = authenticated_admin(request)
            payload = await _json_body(
                request,
                required={"can_create_rooms", "room_limit"},
                allowed={"can_create_rooms", "room_limit"},
            )
            return JSONResponse(
                {
                    "user": store.update_web_user_room_permission(
                        requesting_web_user_id=str(identity["user_id"]),
                        target_web_user_id=request.path_params["user_id"],
                        can_create_rooms=payload["can_create_rooms"],
                        room_limit=payload["room_limit"],
                    )
                }
            )
        except Exception as exc:
            return _json_error(exc)

    async def room_web_users(request: Request) -> Response:
        try:
            identity = authenticated_web_user(request)
            return JSONResponse(
                store.search_room_web_users(
                    requesting_web_user_id=str(identity["user_id"]),
                    conversation_id=request.path_params["conversation_id"],
                    query=str(request.query_params.get("query") or ""),
                    limit=_int_query(request, "limit", default=100, maximum=200),
                )
            )
        except Exception as exc:
            return _json_error(exc)

    async def update_room_web_user(request: Request) -> Response:
        try:
            intent = (
                "invite-room-web-user"
                if request.method == "PUT"
                else "remove-room-web-user"
            )
            require_web_intent(request, intent=intent)
            identity = authenticated_web_user(request)
            payload: dict[str, object] = {}
            if request.method == "PUT" and await request.body():
                payload = await _json_body(
                    request,
                    required=set(),
                    allowed={"access_role"},
                )
            return JSONResponse(
                {
                    "user": store.manage_room_web_member(
                        requesting_web_user_id=str(identity["user_id"]),
                        conversation_id=request.path_params["conversation_id"],
                        target_web_user_id=request.path_params["user_id"],
                        active=request.method == "PUT",
                        access_role=payload.get("access_role", "member"),
                    )
                }
            )
        except Exception as exc:
            return _json_error(exc)

    async def rename_room(request: Request) -> Response:
        try:
            require_web_intent(request, intent="rename-room")
            identity = authenticated_web_user(request)
            payload = await _json_body(
                request,
                required={"new_conversation_id"},
                allowed={"new_conversation_id"},
            )
            return JSONResponse(
                {
                    "room": store.rename_room(
                        conversation_id=request.path_params["conversation_id"],
                        new_conversation_id=payload["new_conversation_id"],
                        renamed_by_web_user_id=str(identity["user_id"]),
                    )
                }
            )
        except Exception as exc:
            return _json_error(exc)

    return [
            Route("/api/rooms", rooms, methods=["GET"]),
            Route("/api/rooms", create_room, methods=["POST"]),
            Route(
                "/api/admin/web-users/room-permissions",
                web_user_room_permissions,
                methods=["GET"],
            ),
            Route(
                "/api/admin/web-registration-codes",
                web_registration_codes,
                methods=["GET", "POST"],
            ),
            Route(
                "/api/admin/web-registration-codes/{code_id:str}/revoke",
                revoke_web_registration_code,
                methods=["POST"],
            ),
            Route(
                "/api/admin/web-users/{user_id:str}/room-permission",
                update_web_user_room_permission,
                methods=["PATCH"],
            ),
            Route(
                "/api/admin/rooms/{conversation_id:str}/web-users",
                room_web_users,
                methods=["GET"],
            ),
            Route(
                "/api/admin/rooms/{conversation_id:str}/web-users/{user_id:str}",
                update_room_web_user,
                methods=["PUT", "DELETE"],
            ),
            Route(
                "/api/rooms/{conversation_id:str}/web-users",
                room_web_users,
                methods=["GET"],
            ),
            Route(
                "/api/rooms/{conversation_id:str}/web-users/{user_id:str}",
                update_room_web_user,
                methods=["PUT", "DELETE"],
            ),
            Route(
                "/api/rooms/{conversation_id:str}",
                rename_room,
                methods=["PATCH"],
            ),
    ]
