from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from .config import BridgeConfig
from .store import (
    AuthenticationError,
    BridgeStore,
    ConflictError,
    NotFoundError,
    RateLimitError,
)
from .validation import ValidationError
from .viewer_store import ViewerRepository


WEB_ROOT = Path(__file__).with_name("web")
ALLOWED_BIND_HOSTS = {"127.0.0.1", "0.0.0.0"}


class SecurityHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "no-store"
                headers["Content-Security-Policy"] = (
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
                    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
                )
                headers["Referrer-Policy"] = "no-referrer"
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
            await send(message)

        await self.app(scope, receive, send_with_headers)


def create_app(database: str | Path) -> Starlette:
    # Read projections stay query_only. Owner room creation and owner-authored
    # chat both go through the same BridgeStore authority used by MCP and CLI.
    store = BridgeStore(database)
    repository = ViewerRepository(database)

    async def index(_: Request) -> Response:
        return FileResponse(WEB_ROOT / "index.html", media_type="text/html")

    async def stylesheet(_: Request) -> Response:
        return FileResponse(WEB_ROOT / "app.css", media_type="text/css")

    async def javascript(_: Request) -> Response:
        return FileResponse(
            WEB_ROOT / "app.js",
            media_type="application/javascript",
        )

    async def health(_: Request) -> Response:
        return _json_call(repository.health)

    async def rooms(request: Request) -> Response:
        return _json_call(
            lambda: {
                "rooms": repository.rooms(
                    limit=_int_query(request, "limit", default=200, maximum=500)
                )
            },
            before=store.archive_stale_rooms,
        )

    async def create_room(request: Request) -> Response:
        if not _is_owner_ui_request(request, intent="create-room"):
            return JSONResponse(
                {"error": "room creation is only accepted from this local page"},
                status_code=403,
            )
        try:
            payload = await _json_body(
                request,
                required={"conversation_id"},
                allowed={"conversation_id"},
            )
        except HttpInputError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status_code)
        return _json_call(
            lambda: {"room": store.create_user_room(payload["conversation_id"])},
            success_status=201,
        )

    async def list_sessions(_: Request) -> Response:
        return _json_call(lambda: {"sessions": repository.sessions(limit=200)})

    async def revoke_session(request: Request) -> Response:
        if not _is_owner_ui_request(request, intent="revoke-session"):
            return JSONResponse(
                {"error": "session revocation is only accepted from the local owner page"},
                status_code=403,
            )
        if not _is_loopback_request(request):
            return JSONResponse(
                {"error": "open this page through 127.0.0.1 to revoke sessions"},
                status_code=403,
            )
        return _json_call(
            lambda: {
                "session": store.revoke_session(
                    request.path_params["session_id"],
                )
            }
        )

    async def register_agent(request: Request) -> Response:
        try:
            payload = await _json_body(
                request,
                required={
                    "product",
                    "username",
                    "session_alias",
                    "conversation_id",
                },
                allowed={
                    "product",
                    "username",
                    "session_alias",
                    "conversation_id",
                    "roles",
                    "capabilities",
                },
            )
        except HttpInputError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status_code)
        return _json_call(
            lambda: store.register_agent_session(
                product=payload["product"],
                username=payload["username"],
                session_alias=payload["session_alias"],
                conversation_id=payload["conversation_id"],
                roles=payload.get("roles"),
                capabilities=payload.get("capabilities"),
            ),
            success_status=201,
        )

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
                allowed={"wait_seconds", "limit", "auto_claim_roles"},
            )
            result = await asyncio.to_thread(
                store.wait_messages,
                participant_id=auth["participant_id"],
                authorized_session_id=auth["session_id"],
                wait_seconds=payload.get("wait_seconds", 30),
                limit=payload.get("limit", 20),
                auto_claim_roles=payload.get("auto_claim_roles", True),
            )
            return JSONResponse(result)
        except Exception as exc:
            return _json_error(exc)

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
            allowed={"message_id", "body", "refs"},
            operation=lambda auth, payload: store.reply(
                authorized_session_id=auth["session_id"],
                participant_id=auth["participant_id"],
                message_id=payload["message_id"],
                body_text=payload["body"],
                refs=payload.get("refs"),
            ),
        )

    async def agent_history(request: Request) -> Response:
        return await _agent_json_call(
            request,
            store,
            required={"conversation_id"},
            allowed={"conversation_id", "limit", "before_sequence"},
            operation=lambda auth, payload: store.history(
                participant_id=auth["participant_id"],
                conversation_id=payload["conversation_id"],
                limit=payload.get("limit", 50),
                before_sequence=payload.get("before_sequence"),
                authorized_session_id=auth["session_id"],
            ),
        )

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

    async def messages(request: Request) -> Response:
        before = request.query_params.get("before_sequence")
        return _json_call(
            lambda: {
                "conversation_id": request.path_params["conversation_id"],
                "messages": repository.messages(
                    request.path_params["conversation_id"],
                    limit=_int_query(request, "limit", default=300, maximum=500),
                    before_sequence=int(before) if before is not None else None,
                ),
            }
        )

    async def owner_send_message(request: Request) -> Response:
        if not _is_owner_ui_request(request, intent="send-message"):
            return JSONResponse(
                {"error": "messages are only accepted from this owner page"},
                status_code=403,
            )
        try:
            payload = await _json_body(
                request,
                required={"body"},
                allowed={"body"},
            )
        except HttpInputError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status_code)
        return _json_call(
            lambda: {
                "message": store.send_owner_message(
                    conversation_id=request.path_params["conversation_id"],
                    body_text=payload["body"],
                )
            },
            success_status=201,
        )

    async def participants(request: Request) -> Response:
        return _json_call(
            lambda: {
                "conversation_id": request.path_params["conversation_id"],
                "participants": repository.participants(
                    request.path_params["conversation_id"]
                ),
            }
        )

    app = Starlette(
        debug=False,
        routes=[
            Route("/", index, methods=["GET"]),
            Route("/assets/app.css", stylesheet, methods=["GET"]),
            Route("/assets/app.js", javascript, methods=["GET"]),
            Route("/api/health", health, methods=["GET"]),
            Route("/api/rooms", rooms, methods=["GET"]),
            Route("/api/rooms", create_room, methods=["POST"]),
            Route("/api/sessions", list_sessions, methods=["GET"]),
            Route(
                "/api/sessions/{session_id:str}/revoke",
                revoke_session,
                methods=["POST"],
            ),
            Route("/agent/register", register_agent, methods=["POST"]),
            Route("/agent/heartbeat", agent_heartbeat, methods=["POST"]),
            Route("/agent/send", agent_send, methods=["POST"]),
            Route("/agent/rooms/create", agent_create_room, methods=["POST"]),
            Route("/agent/wait", agent_wait, methods=["POST"]),
            Route("/agent/action", agent_action, methods=["POST"]),
            Route("/agent/reply", agent_reply, methods=["POST"]),
            Route("/agent/history", agent_history, methods=["POST"]),
            Route("/agent/participants", agent_participants, methods=["POST"]),
            Route(
                "/api/rooms/{conversation_id:str}/messages",
                messages,
                methods=["GET"],
            ),
            Route(
                "/api/rooms/{conversation_id:str}/messages",
                owner_send_message,
                methods=["POST"],
            ),
            Route(
                "/api/rooms/{conversation_id:str}/participants",
                participants,
                methods=["GET"],
            ),
        ],
    )
    app.add_middleware(SecurityHeadersMiddleware)
    return app


def _json_call(
    callable_,
    *,
    before=None,
    success_status: int = 200,
) -> JSONResponse:
    try:
        if before is not None:
            before()
        return JSONResponse(callable_(), status_code=success_status)
    except AuthenticationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=401)
    except RateLimitError as exc:
        return JSONResponse(
            {
                "error": str(exc),
                "retry_after_seconds": exc.retry_after_seconds,
            },
            status_code=429,
        )
    except ConflictError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except NotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except (TypeError, ValueError, ValidationError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except sqlite3.Error:
        return JSONResponse(
            {"error": "SQLite is temporarily unavailable"},
            status_code=503,
        )


class HttpInputError(ValueError):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        self.status_code = status_code
        super().__init__(message)


async def _json_body(
    request: Request,
    *,
    required: set[str],
    allowed: set[str],
) -> dict:
    content_type = request.headers.get("content-type", "").split(";", 1)[0]
    if content_type.strip().lower() != "application/json":
        raise HttpInputError("Content-Type must be application/json", status_code=415)
    raw = await request.body()
    if len(raw) > 70_000:
        raise HttpInputError("request body is too large", status_code=413)
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HttpInputError("invalid JSON body") from exc
    if not isinstance(payload, dict):
        raise HttpInputError("JSON body must be an object")
    keys = set(payload)
    missing = required - keys
    extras = keys - allowed
    if missing:
        raise HttpInputError(f"missing fields: {', '.join(sorted(missing))}")
    if extras:
        raise HttpInputError(f"unsupported fields: {', '.join(sorted(extras))}")
    return payload


def _authenticate_request(request: Request, store: BridgeStore) -> dict:
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthenticationError("Bearer agent session token is required")
    return store.authenticate_session(token)


async def _agent_json_call(
    request: Request,
    store: BridgeStore,
    *,
    required: set[str],
    allowed: set[str],
    operation,
) -> Response:
    try:
        auth = _authenticate_request(request, store)
        payload = await _json_body(request, required=required, allowed=allowed)
        return JSONResponse(operation(auth, payload))
    except Exception as exc:
        return _json_error(exc)


def _json_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, HttpInputError):
        return JSONResponse({"error": str(exc)}, status_code=exc.status_code)
    if isinstance(exc, AuthenticationError):
        return JSONResponse({"error": str(exc)}, status_code=401)
    if isinstance(exc, RateLimitError):
        return JSONResponse(
            {
                "error": str(exc),
                "retry_after_seconds": exc.retry_after_seconds,
            },
            status_code=429,
        )
    if isinstance(exc, ConflictError):
        return JSONResponse({"error": str(exc)}, status_code=409)
    if isinstance(exc, NotFoundError):
        return JSONResponse({"error": str(exc)}, status_code=404)
    if isinstance(exc, (TypeError, ValueError, ValidationError)):
        return JSONResponse({"error": str(exc)}, status_code=400)
    if isinstance(exc, sqlite3.Error):
        return JSONResponse(
            {"error": "SQLite is temporarily unavailable"},
            status_code=503,
        )
    return JSONResponse({"error": "internal bridge error"}, status_code=500)


def _is_owner_ui_request(request: Request, *, intent: str) -> bool:
    host = request.headers.get("host", "")
    if not host:
        return False
    expected_origin = f"{request.url.scheme}://{host}"
    if request.headers.get("origin") != expected_origin:
        return False
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site and fetch_site != "same-origin":
        return False
    return request.headers.get("x-agent-bridge-intent") == intent


def _is_loopback_request(request: Request) -> bool:
    if request.client is None:
        return False
    return request.client.host in {"127.0.0.1", "::1", "testclient"}


def _int_query(
    request: Request,
    key: str,
    *,
    default: int,
    maximum: int,
) -> int:
    raw = request.query_params.get(key)
    value = int(raw) if raw is not None else default
    return max(1, min(value, maximum))


def main() -> None:
    config = BridgeConfig.from_env()
    host = os.environ.get("AGENT_BRIDGE_VIEWER_HOST", "0.0.0.0").strip()
    if host not in ALLOWED_BIND_HOSTS:
        raise RuntimeError("AGENT_BRIDGE_VIEWER_HOST must be 0.0.0.0 or 127.0.0.1")
    try:
        port = int(os.environ.get("AGENT_BRIDGE_VIEWER_PORT", "8765"))
    except ValueError as exc:
        raise RuntimeError("AGENT_BRIDGE_VIEWER_PORT must be an integer") from exc
    if not 1024 <= port <= 65535:
        raise RuntimeError("AGENT_BRIDGE_VIEWER_PORT must be between 1024 and 65535")
    uvicorn.run(
        create_app(config.database),
        host=host,
        port=port,
        access_log=False,
        log_level="info",
        server_header=False,
    )


if __name__ == "__main__":
    main()
