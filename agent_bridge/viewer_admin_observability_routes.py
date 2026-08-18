"""Lifecycle, monitoring, audit, and history administration routes."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .security import ViewerSecurityPolicy
from .store import BridgeStore
from .viewer_http import (
    _int_query,
    _json_body,
    _json_error,
    _optional_float_query,
    _optional_positive_int_query,
)
from .viewer_store import ViewerRepository


def build_admin_observability_routes(
    *,
    store: BridgeStore,
    repository: ViewerRepository,
    policy: ViewerSecurityPolicy,
    runtime_instance_id: str,
    runtime_leader: asyncio.Event,
    authenticated_admin,
    require_web_intent,
    enforce_rate,
) -> list[Route]:
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

    return [
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
    ]
