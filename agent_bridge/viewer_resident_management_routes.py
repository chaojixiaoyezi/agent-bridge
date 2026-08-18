"""Web routes for resident repair and nickname governance."""

from __future__ import annotations

import asyncio
import os

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .connector import adapter_kind_for_product, configure_resident_connector
from .resident_health import (
    configure_existing_connector_from_disk,
    local_connector_template,
    repair_known_identity_services,
    split_supported_identity,
)
from .store import BridgeStore
from .validation import conversation_id as validate_conversation_id
from .viewer_http import _int_query, _json_body, _json_error
from .viewer_store import ViewerRepository


def build_resident_management_routes(
    *,
    store: BridgeStore,
    repository: ViewerRepository,
    enable_resident_repair: bool,
    authenticated_admin,
    require_web_intent,
) -> list[Route]:
    async def repair_room_residents(request: Request) -> Response:
        try:
            require_web_intent(request, intent="repair-room-residents")
            web_identity = authenticated_admin(request)
            conversation = validate_conversation_id(
                request.path_params["conversation_id"]
            )
            projected = repository.participants(conversation)
            repaired: list[dict[str, object]] = []
            unavailable: list[dict[str, str]] = []
            for participant in projected:
                client_type = str(participant["client_type"])
                if split_supported_identity(client_type) is None:
                    continue
                connector_id = str(participant.get("connector_id") or "")
                local = None
                if connector_id:
                    local = await asyncio.to_thread(
                        repair_known_identity_services,
                        client_type,
                        connector_id=connector_id,
                        conversation_id=conversation,
                        enable_disabled=True,
                    )
                configured = None
                if local is None:
                    if not connector_id and enable_resident_repair:
                        product_and_username = split_supported_identity(client_type)
                        if product_and_username is not None:
                            product, username = product_and_username
                            template = await asyncio.to_thread(
                                local_connector_template,
                                client_type,
                            )
                            if template is None:
                                unavailable.append(
                                    {
                                        "participant_id": str(
                                            participant["participant_id"]
                                        ),
                                        "display_name": str(
                                            participant["display_name"]
                                        ),
                                        "reason": (
                                            "本机没有可安全复制的原值守工作区配置，"
                                            "需重新邀请接入"
                                        ),
                                    }
                                )
                                continue
                            try:
                                invitation = await asyncio.to_thread(
                                    store.create_agent_invitation,
                                    conversation_id=conversation,
                                    product=product,
                                    requested_mode="resident",
                                    adapter_kind=adapter_kind_for_product(product),
                                    created_by_web_user_id=str(web_identity["user_id"]),
                                )
                                invitation_token = str(
                                    invitation.pop("invitation_token")
                                )
                                registration = await asyncio.to_thread(
                                    store.accept_agent_invitation,
                                    invitation_token=invitation_token,
                                    product=product,
                                    username=username,
                                    signature=str(participant["signature"]),
                                    roles=list(participant.get("roles") or []),
                                    capabilities=list(
                                        participant.get("capabilities") or []
                                    ),
                                )
                                local_port = int(
                                    os.environ.get(
                                        "AGENT_BRIDGE_VIEWER_PORT",
                                        "8765",
                                    )
                                )
                                setup = await asyncio.to_thread(
                                    configure_resident_connector,
                                    connector_id=str(registration["connector_id"]),
                                    enrollment_token=str(
                                        registration["enrollment_token"]
                                    ),
                                    bridge_url=f"http://127.0.0.1:{local_port}",
                                    product=product,
                                    username=str(
                                        registration.get("username") or username
                                    ),
                                    signature=str(participant["signature"]),
                                    conversation_id=conversation,
                                    adapter_kind=adapter_kind_for_product(product),
                                    requested_mode="resident",
                                    roles=list(participant.get("roles") or []),
                                    capabilities=list(
                                        participant.get("capabilities") or []
                                    ),
                                    workspace_path=str(
                                        template.get("workspace_path") or ""
                                    ),
                                    activate=True,
                                )
                                configured = setup.public_payload()
                                await asyncio.to_thread(
                                    store.report_agent_connector_setup,
                                    participant_id=str(registration["participant_id"]),
                                    authorized_session_id=str(
                                        registration["session_id"]
                                    ),
                                    connector_id=str(registration["connector_id"]),
                                    setup_status=setup.status,
                                    detail=setup.public_payload(),
                                )
                                local = await asyncio.to_thread(
                                    repair_known_identity_services,
                                    client_type,
                                    connector_id=str(registration["connector_id"]),
                                    conversation_id=conversation,
                                )
                            except Exception as exc:
                                unavailable.append(
                                    {
                                        "participant_id": str(
                                            participant["participant_id"]
                                        ),
                                        "display_name": str(
                                            participant["display_name"]
                                        ),
                                        "reason": f"自动补建值守失败：{exc}",
                                    }
                                )
                                continue
                    if not connector_id and local is None:
                        unavailable.append(
                            {
                                "participant_id": str(participant["participant_id"]),
                                "display_name": str(participant["display_name"]),
                                "reason": "旧式手动会话没有私有 connector，需重新接受值守邀请",
                            }
                        )
                        continue
                    if local is None:
                        configured = await asyncio.to_thread(
                            configure_existing_connector_from_disk,
                            client_type,
                            connector_id=connector_id or None,
                            conversation_id=conversation,
                        )
                        local = await asyncio.to_thread(
                            repair_known_identity_services,
                            client_type,
                            connector_id=connector_id or None,
                            conversation_id=conversation,
                        )
                if local is None:
                    unavailable.append(
                        {
                            "participant_id": str(participant["participant_id"]),
                            "display_name": str(participant["display_name"]),
                            "reason": "本机没有这个 Agent 的私有值守配置，需重新邀请接入",
                        }
                    )
                    continue
                repaired.append(
                    {
                        "participant_id": str(participant["participant_id"]),
                        "display_name": str(participant["display_name"]),
                        "resident_status": str(local["resident_status"]),
                        "repaired_services": list(local.get("repaired_services") or []),
                        "configured": configured is not None,
                    }
                )
            return JSONResponse(
                {
                    "conversation_id": conversation,
                    "checked_count": len(repaired) + len(unavailable),
                    "online_count": sum(
                        item["resident_status"] == "online" for item in repaired
                    ),
                    "repaired": repaired,
                    "unavailable": unavailable,
                }
            )
        except Exception as exc:
            return _json_error(exc)

    async def nickname_requests(request: Request) -> Response:
        try:
            authenticated_admin(request)
            status = request.query_params.get("status", "pending")
            return JSONResponse(
                {
                    "requests": store.list_nickname_requests(
                        status=status,
                        limit=_int_query(
                            request,
                            "limit",
                            default=200,
                            maximum=500,
                        ),
                    )
                }
            )
        except Exception as exc:
            return _json_error(exc)

    async def review_nickname_request(request: Request) -> Response:
        try:
            require_web_intent(request, intent="review-nickname")
            identity = authenticated_admin(request)
            payload = await _json_body(
                request,
                required={"action"},
                allowed={"action", "review_note"},
            )
            return JSONResponse(
                {
                    "request": store.review_nickname_request(
                        request_id=request.path_params["request_id"],
                        action=payload["action"],
                        review_note=payload.get("review_note"),
                        reviewed_by_web_user_id=str(identity["user_id"]),
                    )
                }
            )
        except Exception as exc:
            return _json_error(exc)

    return [
        Route(
            "/api/rooms/{conversation_id:str}/residents/repair",
            repair_room_residents,
            methods=["POST"],
        ),
        Route("/api/nickname-requests", nickname_requests, methods=["GET"]),
        Route(
            "/api/nickname-requests/{request_id:str}/review",
            review_nickname_request,
            methods=["POST"],
        ),
    ]
