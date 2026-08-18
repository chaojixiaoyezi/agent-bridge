from __future__ import annotations

import json
import math
import sqlite3

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .security import (
    RequestRateLimitExceeded,
    ViewerSecurityPolicy,
)
from .store import (
    AvatarRateLimitError,
    AuthenticationError,
    AuthorizationError,
    BridgeStore,
    ConflictError,
    NicknameRateLimitError,
    NotFoundError,
    RateLimitError,
)
from .validation import ValidationError
from .web_auth import (
    WebAuthenticationError,
    WebAuthorizationError,
    WebConflictError,
)


class HttpInputError(ValueError):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        self.status_code = status_code
        super().__init__(message)


def _public_web_identity(identity: dict[str, object]) -> dict[str, object]:
    fields = (
        "user_id",
        "username",
        "role",
        "is_admin",
        "participant_id",
        "display_name",
        "signature",
        "avatar_key",
        "must_change_password",
        "can_create_rooms",
        "room_limit",
        "created_at",
        "password_changed_at",
        "last_login_at",
        "email_masked",
        "email_verified",
        "email_verified_at",
        "pending_email_masked",
        "email_verification_pending",
        "email_updated_at",
    )
    return {field: identity[field] for field in fields}


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
    except (AuthenticationError, WebAuthenticationError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=401)
    except (AuthorizationError, WebAuthorizationError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)
    except (
        RateLimitError,
        NicknameRateLimitError,
        AvatarRateLimitError,
        RequestRateLimitExceeded,
    ) as exc:
        return JSONResponse(
            {
                "error": str(exc),
                "retry_after_seconds": exc.retry_after_seconds,
            },
            status_code=429,
            headers={
                "Retry-After": str(max(1, math.ceil(exc.retry_after_seconds)))
            },
        )
    except (ConflictError, WebConflictError) as exc:
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


def _event_cursor(value: str | None) -> int:
    if value is None or not value.strip():
        return 0
    try:
        cursor = int(value)
    except ValueError as exc:
        raise HttpInputError("Last-Event-ID must be a non-negative integer") from exc
    if cursor < 0:
        raise HttpInputError("Last-Event-ID must be a non-negative integer")
    return cursor


def _sse_event(
    event: str,
    payload: dict,
    *,
    event_id: int | None = None,
) -> bytes:
    lines: list[str] = []
    if event_id is not None:
        lines.append(f"id: {int(event_id)}")
    lines.append(f"event: {event}")
    lines.append(
        "data: "
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )
    return ("\n".join(lines) + "\n\n").encode("utf-8")


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
    if isinstance(exc, HTTPException):
        return JSONResponse(
            {"error": str(exc.detail or "invalid HTTP request")},
            status_code=exc.status_code,
        )
    if isinstance(exc, HttpInputError):
        return JSONResponse({"error": str(exc)}, status_code=exc.status_code)
    if isinstance(exc, (AuthenticationError, WebAuthenticationError)):
        return JSONResponse({"error": str(exc)}, status_code=401)
    if isinstance(exc, (AuthorizationError, WebAuthorizationError)):
        return JSONResponse({"error": str(exc)}, status_code=403)
    if isinstance(
        exc,
        (
            RateLimitError,
            NicknameRateLimitError,
            AvatarRateLimitError,
            RequestRateLimitExceeded,
        ),
    ):
        return JSONResponse(
            {
                "error": str(exc),
                "retry_after_seconds": exc.retry_after_seconds,
            },
            status_code=429,
            headers={
                "Retry-After": str(max(1, math.ceil(exc.retry_after_seconds)))
            },
        )
    if isinstance(exc, (ConflictError, WebConflictError)):
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


def _is_same_origin_intent(
    request: Request,
    *,
    intent: str,
    policy: ViewerSecurityPolicy | None = None,
) -> bool:
    host = request.headers.get("host", "")
    if not host:
        return False
    expected_origin = f"{request.url.scheme}://{host}"
    origin = request.headers.get("origin")
    if origin != expected_origin:
        return False
    if policy is not None and not policy.origin_allowed(origin):
        return False
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site and fetch_site != "same-origin":
        return False
    return request.headers.get("x-agent-bridge-intent") == intent


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


def _optional_positive_int_query(request: Request, key: str) -> int | None:
    raw = request.query_params.get(key)
    if raw is None or not raw.strip():
        return None
    value = int(raw)
    if value < 1:
        raise ValueError(f"{key} must be a positive integer")
    return value


def _optional_float_query(request: Request, key: str) -> float | None:
    raw = request.query_params.get(key)
    if raw is None or not raw.strip():
        return None
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError(f"{key} must be finite")
    return value
