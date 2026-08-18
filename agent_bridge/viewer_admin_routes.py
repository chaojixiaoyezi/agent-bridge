from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .connector import configure_resident_connector
from .resident_health import local_connector_template, split_supported_identity
from .security import ViewerSecurityPolicy
from .store import BridgeStore
from .validation import ValidationError
from .viewer_http import (
    _int_query,
    _json_body,
    _json_call,
    _json_error,
    _optional_float_query,
    _optional_positive_int_query,
)
from .viewer_store import ViewerRepository
from .web_auth import WebAuthStore


def build_admin_routes(
    *,
    project_root: Path,
    store: BridgeStore,
    repository: ViewerRepository,
    web_auth: WebAuthStore,
    policy: ViewerSecurityPolicy,
    runtime_instance_id: str,
    runtime_leader: asyncio.Event,
    enable_resident_repair: bool,
    authenticated_web_user,
    authenticated_admin,
    web_room_access_scope,
    require_web_intent,
    enforce_rate,
) -> list[Route]:
    PROJECT_ROOT = project_root

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
                allowed={"conversation_id"},
            )
            return JSONResponse(
                {
                    "room": store.create_web_user_room(
                        authorized_session_id=str(identity["session_id"]),
                        web_user_id=str(identity["user_id"]),
                        participant_id=str(identity["participant_id"]),
                        conversation_id=payload["conversation_id"],
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

    async def agent_lifecycle_configuration(request: Request) -> Response:
        try:
            identity = authenticated_admin(request)
            return JSONResponse(
                store.agent_lifecycle_configuration(
                    requesting_web_user_id=str(identity["user_id"]),
                )
            )
        except Exception as exc:
            return _json_error(exc)

    async def update_agent_lifecycle_configuration(request: Request) -> Response:
        try:
            require_web_intent(request, intent="update-agent-lifecycle")
            identity = authenticated_admin(request)
            payload = await _json_body(
                request,
                required={"inactivity_days"},
                allowed={"inactivity_days", "unactivated_inactivity_days"},
            )
            return JSONResponse(
                store.update_agent_lifecycle_configuration(
                    inactivity_days=payload["inactivity_days"],
                    unactivated_inactivity_days=payload.get(
                        "unactivated_inactivity_days"
                    ),
                    updated_by_web_user_id=str(identity["user_id"]),
                )
            )
        except Exception as exc:
            return _json_error(exc)

    async def admin_room_members(request: Request) -> Response:
        try:
            identity = authenticated_admin(request)
            return JSONResponse(
                store.admin_room_agents(
                    requesting_web_user_id=str(identity["user_id"]),
                )
            )
        except Exception as exc:
            return _json_error(exc)

    async def connector_health(request: Request) -> Response:
        try:
            identity = authenticated_admin(request)
            return JSONResponse(
                store.admin_connector_health(
                    requesting_web_user_id=str(identity["user_id"]),
                )
            )
        except Exception as exc:
            return _json_error(exc)

    async def operational_monitoring_dashboard(request: Request) -> Response:
        try:
            identity = authenticated_admin(request)
            hours = request.query_params.get("hours", "24")
            payload = await asyncio.to_thread(
                store.operational_monitoring_dashboard,
                requesting_web_user_id=str(identity["user_id"]),
                hours=hours,
            )
            runtime_status = await asyncio.to_thread(
                store.runtime_coordination_status,
                current_instance_id=runtime_instance_id,
            )
            if payload["latest"] is None and (
                runtime_leader.is_set()
                or int(runtime_status["active_instance_count"]) == 0
            ):
                await asyncio.to_thread(store.record_operational_sample)
                payload = await asyncio.to_thread(
                    store.operational_monitoring_dashboard,
                    requesting_web_user_id=str(identity["user_id"]),
                    hours=hours,
                )
            payload["runtime_coordination"] = runtime_status
            return JSONResponse(payload)
        except Exception as exc:
            return _json_error(exc)

    async def admin_audit_events(request: Request) -> Response:
        try:
            identity = authenticated_admin(request)
            return JSONResponse(
                await asyncio.to_thread(
                    store.admin_audit_events,
                    requesting_web_user_id=str(identity["user_id"]),
                    limit=request.query_params.get("limit", "100"),
                    before_sequence=request.query_params.get("before_sequence"),
                    query=request.query_params.get("query", ""),
                    category=request.query_params.get("category", ""),
                    outcome=request.query_params.get("outcome", ""),
                    actor_web_user_id=request.query_params.get(
                        "actor_web_user_id",
                        "",
                    ),
                    conversation_id=request.query_params.get(
                        "conversation_id",
                        "",
                    ),
                    hours=request.query_params.get("hours", "168"),
                )
            )
        except Exception as exc:
            return _json_error(exc)

    async def admin_history_search(request: Request) -> Response:
        try:
            authenticated_admin(request)
            if policy.public_mode:
                enforce_rate(
                    request,
                    "admin-history-search-session",
                    subject=request.state.web_identity["session_id"],
                    limit=120,
                    window_seconds=60,
                )
            return JSONResponse(
                await asyncio.to_thread(
                    repository.search_messages_globally,
                    query=request.query_params.get("q", ""),
                    conversation_id=request.query_params.get("conversation_id"),
                    sender_query=request.query_params.get("sender", ""),
                    message_kind=request.query_params.get("message_kind"),
                    notification_mode=request.query_params.get("notification_mode"),
                    created_after=_optional_float_query(request, "created_after"),
                    created_before=_optional_float_query(request, "created_before"),
                    before_sequence=_optional_positive_int_query(
                        request,
                        "before_sequence",
                    ),
                    limit=_int_query(request, "limit", default=50, maximum=100),
                )
            )
        except Exception as exc:
            return _json_error(exc)

    async def history_retention_configuration(request: Request) -> Response:
        try:
            identity = authenticated_admin(request)
            return JSONResponse(
                await asyncio.to_thread(
                    store.history_retention_configuration,
                    requesting_web_user_id=str(identity["user_id"]),
                )
            )
        except Exception as exc:
            return _json_error(exc)

    async def update_history_retention_configuration(
        request: Request,
    ) -> Response:
        try:
            require_web_intent(request, intent="update-history-retention")
            identity = authenticated_admin(request)
            payload = await _json_body(
                request,
                required={"mode", "retention_days"},
                allowed={"mode", "retention_days"},
            )
            return JSONResponse(
                {
                    "policy": await asyncio.to_thread(
                        store.update_history_retention_policy,
                        updated_by_web_user_id=str(identity["user_id"]),
                        mode=payload["mode"],
                        retention_days=payload["retention_days"],
                    )
                }
            )
        except Exception as exc:
            return _json_error(exc)

    async def preview_history_redaction(request: Request) -> Response:
        try:
            require_web_intent(request, intent="preview-history-redaction")
            identity = authenticated_admin(request)
            payload = await _json_body(
                request,
                required={"reason"},
                allowed={"reason", "conversation_id"},
            )
            return JSONResponse(
                {
                    "preview": await asyncio.to_thread(
                        store.preview_history_redaction,
                        created_by_web_user_id=str(identity["user_id"]),
                        reason=payload["reason"],
                        conversation_id=payload.get("conversation_id"),
                    )
                }
            )
        except Exception as exc:
            return _json_error(exc)

    async def execute_history_redaction(request: Request) -> Response:
        try:
            require_web_intent(request, intent="execute-history-redaction")
            identity = authenticated_admin(request)
            payload = await _json_body(
                request,
                required={"preview_id", "confirmation_phrase"},
                allowed={"preview_id", "confirmation_phrase"},
            )
            return JSONResponse(
                {
                    "result": await asyncio.to_thread(
                        store.execute_history_redaction,
                        executed_by_web_user_id=str(identity["user_id"]),
                        preview_id=payload["preview_id"],
                        confirmation_phrase=payload["confirmation_phrase"],
                    )
                }
            )
        except Exception as exc:
            return _json_error(exc)

    async def export_room_history(request: Request) -> Response:
        try:
            require_web_intent(request, intent="export-room-history")
            identity = authenticated_admin(request)
            conversation = request.path_params["conversation_id"]
            payload = await asyncio.to_thread(
                store.export_room_history,
                requesting_web_user_id=str(identity["user_id"]),
                conversation_id=conversation,
            )
            body = json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8")
            encoded_name = quote(
                f"{conversation}-history-{int(payload['exported_at'])}.json"
            )
            return Response(
                body,
                media_type="application/json",
                headers={
                    "Content-Disposition": (
                        "attachment; filename=agent-bridge-room-history.json; "
                        f"filename*=UTF-8''{encoded_name}"
                    )
                },
            )
        except Exception as exc:
            return _json_error(exc)

    async def acknowledge_operational_alert(request: Request) -> Response:
        try:
            require_web_intent(request, intent="acknowledge-operational-alert")
            identity = authenticated_admin(request)
            return JSONResponse(
                {
                    "alert": await asyncio.to_thread(
                        store.acknowledge_operational_alert,
                        alert_id=request.path_params["alert_id"],
                        acknowledged_by_web_user_id=str(identity["user_id"]),
                    )
                }
            )
        except Exception as exc:
            return _json_error(exc)

    async def request_connector_enrollment_rotation(request: Request) -> Response:
        try:
            require_web_intent(request, intent="request-connector-rotation")
            identity = authenticated_admin(request)
            return JSONResponse(
                {
                    "connector": store.request_agent_connector_enrollment_rotation(
                        connector_id=request.path_params["connector_id"],
                        requested_by_web_user_id=str(identity["user_id"]),
                    )
                }
            )
        except Exception as exc:
            return _json_error(exc)

    async def revoke_connector_device(request: Request) -> Response:
        try:
            require_web_intent(request, intent="revoke-connector-device")
            identity = authenticated_admin(request)
            return JSONResponse(
                {
                    "connector": store.revoke_agent_connector(
                        connector_id=request.path_params["connector_id"],
                        revoked_by_web_user_id=str(identity["user_id"]),
                    )
                }
            )
        except Exception as exc:
            return _json_error(exc)

    async def kick_room_agent(request: Request) -> Response:
        try:
            require_web_intent(request, intent="kick-agent")
            identity = authenticated_web_user(request)
            return JSONResponse(
                {
                    "agent": store.kick_agent_from_room(
                        conversation_id=request.path_params["conversation_id"],
                        participant_id=request.path_params["participant_id"],
                        kicked_by_web_user_id=str(identity["user_id"]),
                    )
                }
            )
        except Exception as exc:
            return _json_error(exc)

    async def migrate_room_agents(request: Request) -> Response:
        try:
            require_web_intent(request, intent="migrate-agents")
            identity = authenticated_admin(request)
            payload = await _json_body(
                request,
                required={"target_conversation_id", "selections"},
                allowed={"target_conversation_id", "selections"},
            )
            migration = await asyncio.to_thread(
                store.migrate_agents,
                target_conversation_id=payload["target_conversation_id"],
                selections=payload["selections"],
                migrated_by_web_user_id=str(identity["user_id"]),
            )
            seats = await provision_migrated_room_seats(
                conversation_id=str(migration["target_conversation_id"]),
                participant_ids=[
                    str(agent["participant_id"])
                    for agent in migration["agents"]
                ],
                web_identity=identity,
            )
            migration["room_seats"] = seats
            return JSONResponse({"migration": migration})
        except Exception as exc:
            return _json_error(exc)

    async def message_rate_configuration(request: Request) -> Response:
        try:
            identity = authenticated_admin(request)
            return JSONResponse(
                store.message_rate_configuration(
                    requesting_web_user_id=str(identity["user_id"]),
                )
            )
        except Exception as exc:
            return _json_error(exc)

    async def update_global_message_rate(request: Request) -> Response:
        try:
            require_web_intent(request, intent="update-global-message-rate")
            identity = authenticated_admin(request)
            payload = await _json_body(
                request,
                required={"cooldown_seconds"},
                allowed={"cooldown_seconds"},
            )
            return JSONResponse(
                {
                    "global": store.update_global_message_rate(
                        actor_kind=request.path_params["actor_kind"],
                        cooldown_seconds=payload["cooldown_seconds"],
                        updated_by_web_user_id=str(identity["user_id"]),
                    )
                }
            )
        except Exception as exc:
            return _json_error(exc)

    async def search_message_rate_participants(request: Request) -> Response:
        try:
            identity = authenticated_admin(request)
            return JSONResponse(
                {
                    "participants": store.search_message_rate_participants(
                        requesting_web_user_id=str(identity["user_id"]),
                        query=request.query_params.get("query", ""),
                        actor_kind=request.query_params.get("actor_kind", "all"),
                        limit=_int_query(request, "limit", default=50, maximum=100),
                    )
                }
            )
        except Exception as exc:
            return _json_error(exc)

    async def set_participant_message_rate(request: Request) -> Response:
        try:
            require_web_intent(request, intent="set-participant-message-rate")
            identity = authenticated_admin(request)
            payload = await _json_body(
                request,
                required={"cooldown_seconds"},
                allowed={"cooldown_seconds"},
            )
            return JSONResponse(
                {
                    "participant": store.set_participant_message_rate(
                        participant_id=request.path_params["participant_id"],
                        cooldown_seconds=payload["cooldown_seconds"],
                        updated_by_web_user_id=str(identity["user_id"]),
                    )
                }
            )
        except Exception as exc:
            return _json_error(exc)

    async def clear_participant_message_rate(request: Request) -> Response:
        try:
            require_web_intent(request, intent="clear-participant-message-rate")
            identity = authenticated_admin(request)
            return JSONResponse(
                {
                    "participant": store.clear_participant_message_rate(
                        participant_id=request.path_params["participant_id"],
                        updated_by_web_user_id=str(identity["user_id"]),
                    )
                }
            )
        except Exception as exc:
            return _json_error(exc)

    async def list_sessions(request: Request) -> Response:
        try:
            authenticated_admin(request)
        except Exception as exc:
            return _json_error(exc)
        return _json_call(
            lambda: {
                "sessions": repository.sessions(limit=200),
                "stats": repository.session_stats(),
            },
            before=store.clear_inactive_sessions,
        )

    async def clear_inactive_sessions(request: Request) -> Response:
        try:
            require_web_intent(request, intent="clear-inactive-sessions")
            authenticated_admin(request)
            return JSONResponse(store.clear_inactive_sessions())
        except Exception as exc:
            return _json_error(exc)

    async def revoke_session(request: Request) -> Response:
        try:
            require_web_intent(request, intent="revoke-session")
            authenticated_admin(request)
            return JSONResponse(
                {
                    "session": store.revoke_session(
                        request.path_params["session_id"],
                    )
                }
            )
        except Exception as exc:
            return _json_error(exc)

    async def provision_migrated_room_seats(
        *,
        conversation_id: str,
        participant_ids: list[str],
        web_identity: dict[str, object],
    ) -> dict[str, object]:
        """Best-effort local provisioning after additive membership migration."""

        projected = {
            str(participant["participant_id"]): participant
            for participant in repository.participants(conversation_id)
        }
        provisioned: list[dict[str, object]] = []
        unavailable: list[dict[str, str]] = []
        for participant_id in participant_ids:
            participant = projected.get(participant_id)
            if participant is None:
                unavailable.append(
                    {
                        "participant_id": participant_id,
                        "reason": "目标聊天室成员投影尚未建立",
                    }
                )
                continue
            if participant.get("connector_id"):
                provisioned.append(
                    {
                        "participant_id": participant_id,
                        "connector_id": str(participant["connector_id"]),
                        "created": False,
                    }
                )
                continue
            identity = split_supported_identity(str(participant["client_type"]))
            if identity is None or not enable_resident_repair:
                unavailable.append(
                    {
                        "participant_id": participant_id,
                        "reason": "该产品不能由本机自动创建独立值守席位",
                    }
                )
                continue
            product, username = identity
            template = await asyncio.to_thread(
                local_connector_template,
                str(participant["client_type"]),
            )
            if template is None:
                unavailable.append(
                    {
                        "participant_id": participant_id,
                        "reason": "本机没有可安全复制的原值守工作区配置",
                    }
                )
                continue
            try:
                registration = await asyncio.to_thread(
                    store.provision_existing_agent_room_connector,
                    conversation_id=conversation_id,
                    participant_id=participant_id,
                    created_by_web_user_id=str(web_identity["user_id"]),
                )
                local_port = int(os.environ.get("AGENT_BRIDGE_VIEWER_PORT", "8765"))
                setup = await asyncio.to_thread(
                    configure_resident_connector,
                    connector_id=str(registration["connector_id"]),
                    enrollment_token=str(registration["enrollment_token"]),
                    bridge_url=f"http://127.0.0.1:{local_port}",
                    product=str(registration["product"]),
                    username=str(registration.get("username") or username),
                    signature=str(participant["signature"]),
                    conversation_id=conversation_id,
                    adapter_kind=str(registration["adapter_kind"]),
                    requested_mode=str(registration["requested_mode"]),
                    roles=list(registration.get("roles") or []),
                    capabilities=list(registration.get("capabilities") or []),
                    workspace_path=str(template.get("workspace_path") or PROJECT_ROOT),
                    activate=True,
                )
                await asyncio.to_thread(
                    store.report_agent_connector_setup,
                    participant_id=str(registration["participant_id"]),
                    authorized_session_id=str(registration["session_id"]),
                    connector_id=str(registration["connector_id"]),
                    setup_status=setup.status,
                    detail=setup.public_payload(),
                )
                provisioned.append(
                    {
                        "participant_id": participant_id,
                        "connector_id": str(registration["connector_id"]),
                        "created": True,
                        "resident_status": setup.status,
                    }
                )
            except Exception as exc:
                unavailable.append(
                    {
                        "participant_id": participant_id,
                        "reason": f"独立值守席位创建失败：{exc}",
                    }
                )
        return {
            "target_conversation_id": conversation_id,
            "provisioned": provisioned,
            "unavailable": unavailable,
            "all_ready": not unavailable,
        }

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
            Route(
                "/api/agent-lifecycle",
                agent_lifecycle_configuration,
                methods=["GET"],
            ),
            Route(
                "/api/agent-lifecycle",
                update_agent_lifecycle_configuration,
                methods=["PATCH"],
            ),
            Route(
                "/api/admin/room-members",
                admin_room_members,
                methods=["GET"],
            ),
            Route(
                "/api/admin/connectors/health",
                connector_health,
                methods=["GET"],
            ),
            Route(
                "/api/admin/monitoring",
                operational_monitoring_dashboard,
                methods=["GET"],
            ),
            Route(
                "/api/admin/audit",
                admin_audit_events,
                methods=["GET"],
            ),
            Route(
                "/api/admin/history/search",
                admin_history_search,
                methods=["GET"],
            ),
            Route(
                "/api/admin/history/retention",
                history_retention_configuration,
                methods=["GET"],
            ),
            Route(
                "/api/admin/history/retention",
                update_history_retention_configuration,
                methods=["PATCH"],
            ),
            Route(
                "/api/admin/history/redaction-preview",
                preview_history_redaction,
                methods=["POST"],
            ),
            Route(
                "/api/admin/history/redaction-execute",
                execute_history_redaction,
                methods=["POST"],
            ),
            Route(
                "/api/admin/rooms/{conversation_id:str}/history-export",
                export_room_history,
                methods=["POST"],
            ),
            Route(
                "/api/admin/monitoring/alerts/{alert_id:str}/acknowledge",
                acknowledge_operational_alert,
                methods=["POST"],
            ),
            Route(
                "/api/admin/connectors/{connector_id:str}/rotation-request",
                request_connector_enrollment_rotation,
                methods=["POST"],
            ),
            Route(
                "/api/admin/connectors/{connector_id:str}/revoke",
                revoke_connector_device,
                methods=["POST"],
            ),
            Route(
                "/api/rooms/{conversation_id:str}/participants/"
                "{participant_id:str}/kick",
                kick_room_agent,
                methods=["POST"],
            ),
            Route(
                "/api/room-memberships/migrate",
                migrate_room_agents,
                methods=["POST"],
            ),
            Route(
                "/api/message-rates",
                message_rate_configuration,
                methods=["GET"],
            ),
            Route(
                "/api/message-rates/global/{actor_kind:str}",
                update_global_message_rate,
                methods=["PATCH"],
            ),
            Route(
                "/api/message-rates/participants/search",
                search_message_rate_participants,
                methods=["GET"],
            ),
            Route(
                "/api/message-rates/participants/{participant_id:str}",
                set_participant_message_rate,
                methods=["PUT"],
            ),
            Route(
                "/api/message-rates/participants/{participant_id:str}",
                clear_participant_message_rate,
                methods=["DELETE"],
            ),
            Route("/api/sessions", list_sessions, methods=["GET"]),
            Route(
                "/api/sessions/cleanup",
                clear_inactive_sessions,
                methods=["POST"],
            ),
            Route(
                "/api/sessions/{session_id:str}/revoke",
                revoke_session,
                methods=["POST"],
            ),
    ]
