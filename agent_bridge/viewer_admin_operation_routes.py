"""Connector and room-membership administration routes."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .connector import configure_resident_connector
from .resident_health import local_connector_template, split_supported_identity
from .store import BridgeStore
from .viewer_http import _json_body, _json_error
from .viewer_store import ViewerRepository


def build_admin_operation_routes(
    *,
    project_root: Path,
    store: BridgeStore,
    repository: ViewerRepository,
    enable_resident_repair: bool,
    authenticated_web_user,
    authenticated_admin,
    require_web_intent,
) -> list[Route]:
    PROJECT_ROOT = project_root

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
    ]
