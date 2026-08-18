from __future__ import annotations

import asyncio
import time

from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from .security import ViewerSecurityPolicy
from .store import AuthenticationError
from .viewer_http import _event_cursor, _json_error, _sse_event
from .viewer_store import ViewerRepository
from .web_auth import WebAuthenticationError, WebAuthStore


def build_event_routes(
    *,
    repository: ViewerRepository,
    web_auth: WebAuthStore,
    policy: ViewerSecurityPolicy,
    web_session_cookie: str,
    authenticated_web_user,
    web_room_access_scope,
    enforce_rate,
) -> list[Route]:
    async def owner_events(request: Request) -> Response:
        try:
            if policy.public_mode:
                enforce_rate(
                    request,
                    "web-events-ip",
                    limit=60,
                    window_seconds=60,
                )
            session_token = request.cookies.get(web_session_cookie)
            initial_identity = authenticated_web_user(request)
            initial_scope = web_room_access_scope(initial_identity)
            cursor = _event_cursor(request.headers.get("last-event-id"))
        except Exception as exc:
            return _json_error(exc)

        async def stream():
            nonlocal cursor
            access_scope = initial_scope
            previous_revision: list[object] | None = None
            last_output = time.monotonic()
            last_authentication = time.monotonic()
            while not await request.is_disconnected():
                monotonic_now = time.monotonic()
                if monotonic_now - last_authentication >= 60:
                    try:
                        await asyncio.to_thread(web_auth.authenticate, session_token)
                    except WebAuthenticationError as exc:
                        yield _sse_event(
                            "session_closed",
                            {"error": str(exc)},
                            event_id=cursor,
                        )
                        return
                    last_authentication = monotonic_now
                try:
                    # Re-evaluate the ACL before every event snapshot so an
                    # open SSE connection cannot retain a revoked room scope.
                    access_scope = await asyncio.to_thread(
                        web_room_access_scope,
                        initial_identity,
                    )
                except (WebAuthenticationError, AuthenticationError) as exc:
                    yield _sse_event(
                        "session_closed",
                        {"error": str(exc)},
                        event_id=cursor,
                    )
                    return
                snapshot = await asyncio.to_thread(
                    repository.event_snapshot,
                    after_sequence=cursor,
                    visible_conversation_ids=access_scope["conversation_ids"],
                    include_admin_state=bool(access_scope["is_admin"]),
                )
                revision = list(snapshot["state_revision"])
                if previous_revision is None or revision != previous_revision:
                    event = "state" if previous_revision is None else "state_changed"
                    cursor = int(snapshot["cursor"])
                    yield _sse_event(event, snapshot, event_id=cursor)
                    previous_revision = revision
                    last_output = time.monotonic()
                elif time.monotonic() - last_output >= 20:
                    yield f": keepalive {int(time.time())}\n\n".encode()
                    last_output = time.monotonic()
                await asyncio.sleep(1)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    return [
        Route("/api/events", owner_events, methods=["GET"]),
    ]
