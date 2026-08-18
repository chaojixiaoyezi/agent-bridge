from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from .a2a_gateway import (
    A2A_PROTOCOL_VERSION,
    A2ARequestError,
    agent_card,
    handle_jsonrpc,
    jsonrpc_error,
)
from .avatars import avatar_asset_path, avatar_catalog_payload
from .email_delivery import EmailDelivery
from .security import MAX_REQUEST_BODY_BYTES, ViewerSecurityPolicy
from .store import (
    AuthenticationError,
    AuthorizationError,
    BridgeStore,
    ConflictError,
    NotFoundError,
)
from .viewer_http import (
    _json_body,
    _json_call,
    _json_error,
    _public_web_identity,
)
from .viewer_store import ViewerRepository
from .web_auth import WebAuthenticationError, WebAuthStore


WEB_JAVASCRIPT_ASSETS = (
    "app.js",
    "app-layout.js",
    "app-chat-render.js",
    "app-agent-operations.js",
    "app-room-controller.js",
    "app-governance.js",
    "app-interactions.js",
)
WEB_STYLESHEET_ASSETS = (
    "app.css",
    "app-chat.css",
    "app-dialogs.css",
    "app-responsive.css",
)


def build_public_routes(
    *,
    web_root: Path,
    store: BridgeStore,
    repository: ViewerRepository,
    web_auth: WebAuthStore,
    policy: ViewerSecurityPolicy,
    required_registration_secret: str | None,
    resolved_email_delivery: EmailDelivery | None,
    runtime_instance_id: str,
    web_session_cookie: str,
    authenticated_web_user,
    authenticated_admin,
    require_web_intent,
    enforce_rate,
) -> list[Route]:
    WEB_ROOT = web_root

    async def index(_: Request) -> Response:
        return FileResponse(WEB_ROOT / "index.html", media_type="text/html")

    def stylesheet_asset(filename: str):
        async def serve(_: Request) -> Response:
            return FileResponse(WEB_ROOT / filename, media_type="text/css")

        return serve

    def javascript_asset(filename: str):
        async def serve(_: Request) -> Response:
            return FileResponse(
                WEB_ROOT / filename,
                media_type="application/javascript",
            )

        return serve

    async def avatar_asset(request: Request) -> Response:
        path = avatar_asset_path(
            request.path_params["vendor"],
            request.path_params["filename"],
        )
        if path is None:
            return Response(status_code=404)
        return FileResponse(path, media_type="image/webp")

    async def health(request: Request) -> Response:
        public_health = {
            "status": "ok",
            "server_time": time.time(),
            "open_registration_enabled": required_registration_secret is None,
            "registration_secret_required": required_registration_secret is not None,
            "web_registration_mode": policy.web_registration_mode,
            "admin_registration_codes_enabled": True,
            "email_delivery_enabled": resolved_email_delivery is not None,
            "public_security_mode": policy.public_mode,
            "web_login_required": True,
        }
        if not request.cookies.get(web_session_cookie):
            return JSONResponse(public_health)
        try:
            identity = authenticated_web_user(
                request,
                allow_password_change=True,
            )
        except WebAuthenticationError:
            response = JSONResponse(public_health)
            response.delete_cookie(
                web_session_cookie,
                path="/",
                secure=policy.secure_cookies or request.url.scheme == "https",
                httponly=True,
                samesite="strict",
            )
            return response
        except Exception as exc:
            return _json_error(exc)

        def payload() -> dict:
            result = (
                repository.health()
                if bool(identity["is_admin"])
                else dict(public_health)
            )
            result.update(public_health)
            result["current_user"] = _public_web_identity(
                web_auth.authenticate(request.cookies.get(web_session_cookie))
            )
            if bool(identity["is_admin"]):
                result["security"] = {
                    "public_mode": policy.public_mode,
                    "https_required": policy.public_mode,
                    "trusted_host_count": len(policy.allowed_hosts),
                    "web_registration_mode": policy.web_registration_mode,
                    "request_body_limit_bytes": MAX_REQUEST_BODY_BYTES,
                    "web_session_ttl_seconds": policy.web_session_ttl_seconds,
                }
                result["runtime_coordination"] = (
                    store.runtime_coordination_status(
                        current_instance_id=runtime_instance_id,
                    )
                )
            result["message_rate_limits"] = store.message_rate_summary(
                web_participant_id=str(identity["participant_id"]),
                web_role=str(identity["role"]),
            )
            return result

        return _json_call(payload)

    async def avatars(request: Request) -> Response:
        try:
            authenticated_web_user(request, allow_password_change=True)
            return JSONResponse(avatar_catalog_payload())
        except Exception as exc:
            return _json_error(exc)

    async def a2a_agent_card(request: Request) -> Response:
        return JSONResponse(agent_card(str(request.base_url).rstrip("/")))

    async def a2a_rpc(request: Request) -> Response:
        request_id: object = None
        try:
            if policy.public_mode:
                enforce_rate(
                    request,
                    "a2a-rpc-ip",
                    limit=120,
                    window_seconds=60,
                )
            if request.headers.get("a2a-version", A2A_PROTOCOL_VERSION) != (
                A2A_PROTOCOL_VERSION
            ):
                raise A2ARequestError(
                    "unsupported A2A-Version; expected 1.0",
                    code=-32600,
                )
            authorization = request.headers.get("authorization", "")
            scheme, _, access_token = authorization.partition(" ")
            if scheme.casefold() != "bearer" or not access_token.strip():
                raise AuthenticationError("A2A bearer token is required")
            raw = await request.body()
            if not raw or len(raw) > 70_000:
                raise A2ARequestError("invalid JSON-RPC request size", code=-32600)
            try:
                rpc_request = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise A2ARequestError("invalid JSON", code=-32700) from exc
            if isinstance(rpc_request, dict):
                request_id = rpc_request.get("id")
            result = await asyncio.to_thread(
                handle_jsonrpc,
                store,
                access_token=access_token.strip(),
                request=rpc_request,
            )
            return JSONResponse(
                result,
                headers={"A2A-Version": A2A_PROTOCOL_VERSION},
            )
        except AuthenticationError as exc:
            return JSONResponse(
                jsonrpc_error(request_id=request_id, code=-32001, message=str(exc)),
                status_code=401,
                headers={"A2A-Version": A2A_PROTOCOL_VERSION},
            )
        except A2ARequestError as exc:
            return JSONResponse(
                jsonrpc_error(
                    request_id=request_id,
                    code=exc.code,
                    message=str(exc),
                ),
                headers={"A2A-Version": A2A_PROTOCOL_VERSION},
            )
        except (NotFoundError, ConflictError, AuthorizationError) as exc:
            return JSONResponse(
                jsonrpc_error(request_id=request_id, code=-32002, message=str(exc)),
                headers={"A2A-Version": A2A_PROTOCOL_VERSION},
            )

    async def a2a_grants(request: Request) -> Response:
        try:
            identity = authenticated_admin(request)
            if request.method == "GET":
                return JSONResponse(
                    store.list_a2a_access_grants(
                        requesting_web_user_id=str(identity["user_id"]),
                        conversation_id=request.query_params.get("conversation_id"),
                    )
                )
            require_web_intent(request, intent="create-a2a-grant")
            payload = await _json_body(
                request,
                required={"conversation_id", "label"},
                allowed={"conversation_id", "label", "ttl_seconds"},
            )
            return JSONResponse(
                {
                    "grant": store.create_a2a_access_grant(
                        conversation_id=payload["conversation_id"],
                        label=payload["label"],
                        ttl_seconds=payload.get(
                            "ttl_seconds",
                            30 * 24 * 60 * 60,
                        ),
                        created_by_web_user_id=str(identity["user_id"]),
                    )
                },
                status_code=201,
            )
        except Exception as exc:
            return _json_error(exc)

    async def revoke_a2a_grant(request: Request) -> Response:
        try:
            require_web_intent(request, intent="revoke-a2a-grant")
            identity = authenticated_admin(request)
            return JSONResponse(
                {
                    "grant": store.revoke_a2a_access_grant(
                        grant_id=request.path_params["grant_id"],
                        revoked_by_web_user_id=str(identity["user_id"]),
                    )
                }
            )
        except Exception as exc:
            return _json_error(exc)

    return [
            Route("/", index, methods=["GET"]),
            *[
                Route(
                    f"/assets/{filename}",
                    stylesheet_asset(filename),
                    methods=["GET"],
                )
                for filename in WEB_STYLESHEET_ASSETS
            ],
            *[
                Route(
                    f"/assets/{filename}",
                    javascript_asset(filename),
                    methods=["GET"],
                )
                for filename in WEB_JAVASCRIPT_ASSETS
            ],
            Route(
                "/assets/avatars/{vendor:str}/{filename:str}",
                avatar_asset,
                methods=["GET"],
            ),
            Route("/api/health", health, methods=["GET"]),
            Route("/api/avatars", avatars, methods=["GET"]),
            Route(
                "/.well-known/agent-card.json",
                a2a_agent_card,
                methods=["GET"],
            ),
            Route("/a2a", a2a_rpc, methods=["POST"]),
            Route("/api/a2a/grants", a2a_grants, methods=["GET", "POST"]),
            Route(
                "/api/a2a/grants/{grant_id:str}/revoke",
                revoke_a2a_grant,
                methods=["POST"],
            ),
    ]
