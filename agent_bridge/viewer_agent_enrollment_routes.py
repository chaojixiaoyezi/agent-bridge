"""Agent registration, invitation acceptance, and connector enrollment routes."""

from __future__ import annotations

import secrets

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .security import ViewerSecurityPolicy
from .store import BridgeStore
from .viewer_http import (
    HttpInputError,
    _agent_json_call,
    _json_body,
    _json_call,
    _json_error,
)


def build_agent_enrollment_routes(
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

    async def report_agent_connector_runtime_diagnostics(
        request: Request,
    ) -> Response:
        if policy.public_mode:
            try:
                enforce_rate(
                    request,
                    "agent-connector-diagnostics-ip",
                    limit=300,
                    window_seconds=60,
                )
            except Exception as exc:
                return _json_error(exc)
        return await _agent_json_call(
            request,
            store,
            required={
                "connector_id",
                "protocol_version",
                "software_version",
                "platform",
                "listener_state",
                "queue",
                "worker",
            },
            allowed={
                "connector_id",
                "protocol_version",
                "software_version",
                "platform",
                "listener_state",
                "queue",
                "worker",
            },
            operation=lambda auth, payload: {
                "diagnostics": store.report_connector_runtime_diagnostics(
                    participant_id=auth["participant_id"],
                    authorized_session_id=auth["session_id"],
                    connector_id=payload["connector_id"],
                    protocol_version=payload["protocol_version"],
                    software_version=payload["software_version"],
                    platform=payload["platform"],
                    listener_state=payload["listener_state"],
                    queue=payload["queue"],
                    worker=payload["worker"],
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
                "/agent/connector/runtime-diagnostics",
                report_agent_connector_runtime_diagnostics,
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
    ]
