from __future__ import annotations

import asyncio
import secrets

from starlette.datastructures import MutableHeaders
from starlette.routing import compile_path

from .store import BridgeStore


ADMIN_AUDIT_ACTIONS: dict[tuple[str, str], tuple[str, str]] = {
    ("POST", "/api/a2a/grants"): ("access", "a2a_grant.create"),
    ("POST", "/api/a2a/grants/{grant_id:str}/revoke"): (
        "access",
        "a2a_grant.revoke",
    ),
    ("POST", "/api/admin/web-registration-codes"): (
        "access",
        "registration_code.create",
    ),
    ("POST", "/api/admin/web-registration-codes/{code_id:str}/revoke"): (
        "access",
        "registration_code.revoke",
    ),
    ("POST", "/api/rooms"): ("room", "room.create"),
    ("PATCH", "/api/admin/web-users/{user_id:str}/room-permission"): (
        "permission",
        "room_creation_permission.update",
    ),
    ("PUT", "/api/admin/rooms/{conversation_id:str}/web-users/{user_id:str}"): (
        "membership",
        "web_room_member.upsert",
    ),
    ("DELETE", "/api/admin/rooms/{conversation_id:str}/web-users/{user_id:str}"): (
        "membership",
        "web_room_member.remove",
    ),
    ("PUT", "/api/rooms/{conversation_id:str}/web-users/{user_id:str}"): (
        "membership",
        "web_room_member.upsert",
    ),
    ("DELETE", "/api/rooms/{conversation_id:str}/web-users/{user_id:str}"): (
        "membership",
        "web_room_member.remove",
    ),
    ("PATCH", "/api/rooms/{conversation_id:str}"): ("room", "room.rename"),
    ("PATCH", "/api/agent-lifecycle"): (
        "lifecycle",
        "agent_lifecycle.update",
    ),
    ("POST", "/api/admin/monitoring/alerts/{alert_id:str}/acknowledge"): (
        "monitoring",
        "monitoring_alert.acknowledge",
    ),
    ("PATCH", "/api/admin/history/retention"): (
        "history",
        "history.retention_policy.update",
    ),
    ("POST", "/api/admin/history/redaction-preview"): (
        "history",
        "history.redaction.preview",
    ),
    ("POST", "/api/admin/history/redaction-execute"): (
        "history",
        "history.redaction.execute",
    ),
    (
        "POST",
        "/api/admin/rooms/{conversation_id:str}/history-export",
    ): ("history", "history.export"),
    ("POST", "/api/admin/connectors/{connector_id:str}/rotation-request"): (
        "connector",
        "connector.rotation_request",
    ),
    ("POST", "/api/admin/connectors/{connector_id:str}/revoke"): (
        "connector",
        "connector.revoke",
    ),
    (
        "POST",
        "/api/rooms/{conversation_id:str}/participants/{participant_id:str}/kick",
    ): ("membership", "agent_room_member.kick"),
    ("POST", "/api/room-memberships/migrate"): (
        "membership",
        "agent_room_member.copy",
    ),
    ("PATCH", "/api/message-rates/global/{actor_kind:str}"): (
        "rate_limit",
        "message_rate.global_update",
    ),
    ("PUT", "/api/message-rates/participants/{participant_id:str}"): (
        "rate_limit",
        "message_rate.override_set",
    ),
    ("DELETE", "/api/message-rates/participants/{participant_id:str}"): (
        "rate_limit",
        "message_rate.override_clear",
    ),
    ("POST", "/api/agent-access"): ("access", "agent_invitation.create"),
    ("POST", "/api/agent-invitations/{invitation_id:str}/revoke"): (
        "access",
        "agent_invitation.revoke",
    ),
    ("POST", "/api/sessions/cleanup"): ("session", "session.cleanup"),
    ("POST", "/api/sessions/{session_id:str}/revoke"): (
        "session",
        "session.revoke",
    ),
    (
        "PUT",
        "/api/rooms/{conversation_id:str}/messages/{message_id:str}/markers/{marker_kind:str}",
    ): ("knowledge", "message_marker.set"),
    (
        "DELETE",
        "/api/rooms/{conversation_id:str}/messages/{message_id:str}/markers/{marker_kind:str}",
    ): ("knowledge", "message_marker.remove"),
    ("POST", "/api/rooms/{conversation_id:str}/tasks"): ("task", "task.create"),
    ("POST", "/api/messages/{message_id:str}/convert-to-task"): (
        "task",
        "task.convert_from_message",
    ),
    ("PATCH", "/api/rooms/{conversation_id:str}/wake-policy"): (
        "policy",
        "wake_policy.update",
    ),
    ("PATCH", "/api/rooms/{conversation_id:str}/task-policy"): (
        "permission",
        "task_policy.update",
    ),
    ("PUT", "/api/rooms/{conversation_id:str}/task-grants/{user_id:str}"): (
        "permission",
        "task_grant.update",
    ),
    ("POST", "/api/tasks/{task_id:str}/cancel"): ("task", "task.cancel"),
    ("POST", "/api/messages/{message_id:str}/authorization/revoke"): (
        "authorization",
        "chat_authorization.revoke",
    ),
    ("POST", "/api/messages/{message_id:str}/forward"): (
        "knowledge",
        "message.forward",
    ),
    ("POST", "/api/rooms/{conversation_id:str}/residents/repair"): (
        "connector",
        "room_residents.repair",
    ),
    ("POST", "/api/nickname-requests/{request_id:str}/review"): (
        "identity",
        "nickname_request.review",
    ),
}


ADMIN_AUDIT_TARGET_PARAMETERS = (
    ("grant_id", "a2a_grant"),
    ("code_id", "registration_code"),
    ("user_id", "web_user"),
    ("alert_id", "monitoring_alert"),
    ("connector_id", "connector"),
    ("participant_id", "participant"),
    ("actor_kind", "rate_scope"),
    ("invitation_id", "agent_invitation"),
    ("session_id", "session"),
    ("task_id", "task"),
    ("message_id", "message"),
    ("request_id", "nickname_request"),
)


ADMIN_AUDIT_ROUTE_MATCHERS = tuple(
    (
        method,
        route_path,
        specification,
        compile_path(route_path)[0],
        compile_path(route_path)[2],
    )
    for (method, route_path), specification in ADMIN_AUDIT_ACTIONS.items()
)


class SecurityHeadersMiddleware:
    def __init__(self, app, *, public_mode: bool = False, hsts_seconds: int = 0):
        self.app = app
        self.public_mode = bool(public_mode)
        self.hsts_seconds = max(0, int(hsts_seconds))

    async def __call__(self, scope, receive, send):
        request_id = f"req_{secrets.token_hex(12)}"
        scope["agent_bridge.request_id"] = request_id

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                path = str(scope.get("path") or "")
                headers["Cache-Control"] = (
                    "public, max-age=31536000, immutable"
                    if path.startswith("/assets/")
                    else "no-store"
                )
                headers["Content-Security-Policy"] = (
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; "
                    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
                )
                headers["Referrer-Policy"] = "no-referrer"
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
                headers["Permissions-Policy"] = (
                    "camera=(), geolocation=(), microphone=(), payment=(), usb=()"
                )
                headers["Cross-Origin-Opener-Policy"] = "same-origin"
                headers["Cross-Origin-Resource-Policy"] = "same-origin"
                headers["X-Permitted-Cross-Domain-Policies"] = "none"
                headers["X-Request-ID"] = request_id
                if self.public_mode and self.hsts_seconds > 0:
                    headers["Strict-Transport-Security"] = (
                        f"max-age={self.hsts_seconds}"
                    )
            await send(message)

        await self.app(scope, receive, send_with_headers)


class AdminAuditMiddleware:
    def __init__(self, app, *, store: BridgeStore):
        self.app = app
        self.store = store

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_method = str(scope.get("method") or "").upper()
        request_path = str(scope.get("path") or "")
        matched_route = None
        matched_parameters: dict[str, str] = {}
        for (
            method,
            route_path,
            specification,
            path_regex,
            convertors,
        ) in ADMIN_AUDIT_ROUTE_MATCHERS:
            if request_method != method:
                continue
            match = path_regex.match(request_path)
            if match is None:
                continue
            matched_route = (route_path, specification)
            matched_parameters = {
                key: str(convertors[key].convert(value))
                for key, value in match.groupdict().items()
            }
            break
        status_code = 500

        async def send_with_status(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        await self.app(scope, receive, send_with_status)
        identity = (scope.get("state") or {}).get("web_identity")
        if matched_route is None or not isinstance(identity, dict):
            return
        route_path, specification = matched_route
        category, action = specification
        outcome = (
            "success"
            if 200 <= status_code < 400
            else "denied"
            if status_code in {401, 403, 429}
            else "failed"
        )
        path_parameters = matched_parameters
        target_kind = None
        target_id = None
        for parameter, kind in ADMIN_AUDIT_TARGET_PARAMETERS:
            value = path_parameters.get(parameter)
            if value:
                target_kind = kind
                target_id = value
                break
        try:
            await asyncio.to_thread(
                self.store.record_admin_audit_event,
                actor_web_user_id=str(identity["user_id"]),
                actor_username=str(identity["username"]),
                actor_display_name=str(identity["display_name"]),
                actor_role=str(identity["role"]),
                category=category,
                action=action,
                outcome=outcome,
                status_code=status_code,
                http_method=request_method,
                route=route_path,
                request_id=str(
                    scope.get("agent_bridge.request_id")
                    or f"req_{secrets.token_hex(12)}"
                ),
                conversation_id=path_parameters.get("conversation_id"),
                target_kind=target_kind,
                target_id=target_id,
                detail={"path_parameters": path_parameters},
            )
        except Exception:
            # Auditing is append-only but deliberately sidecar-only: a damaged
            # audit table must never turn a completed governance action into a
            # failed chat/API response or interrupt existing Agent sessions.
            pass
