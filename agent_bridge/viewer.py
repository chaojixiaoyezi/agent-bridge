from __future__ import annotations

import os
import secrets
import socket
import tomllib
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request

from .config import BridgeConfig
from .email_delivery import EmailDelivery, SMTPEmailDelivery
from .security import (
    MAX_REQUEST_BODY_BYTES,
    PublicTransportMiddleware,
    SlidingWindowRateLimiter,
    ViewerSecurityPolicy,
    request_client_key,
)
from .store import (
    BridgeStore,
)
from .viewer_http import (
    _event_cursor as _event_cursor,
    _is_same_origin_intent,
    _sse_event as _sse_event,
)
from .viewer_admin_routes import build_admin_routes
from .viewer_agent_routes import build_agent_routes
from .viewer_auth_routes import build_auth_routes
from .viewer_event_routes import build_event_routes
from .viewer_middleware import AdminAuditMiddleware, SecurityHeadersMiddleware
from .viewer_public_routes import build_public_routes
from .viewer_resident_routes import build_resident_routes
from .viewer_room_routes import build_room_routes
from .viewer_runtime import build_viewer_runtime
from .viewer_store import ViewerRepository
from .web_auth import (
    WebAuthorizationError,
    WebAuthStore,
)


WEB_ROOT = Path(__file__).with_name("web")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALLOWED_BIND_HOSTS = {"127.0.0.1", "0.0.0.0"}


def _runtime_software_version() -> str:
    try:
        return package_version("agent-bridge")
    except PackageNotFoundError:
        try:
            with (PROJECT_ROOT / "pyproject.toml").open("rb") as handle:
                return str(tomllib.load(handle)["project"]["version"])
        except (KeyError, OSError, tomllib.TOMLDecodeError):
            return "source"


def create_app(
    database: str | Path,
    *,
    registration_secret: str | None = None,
    captcha_generator: Callable[[], str] | None = None,
    enable_resident_repair: bool = False,
    security_policy: ViewerSecurityPolicy | None = None,
    email_delivery: EmailDelivery | None = None,
) -> Starlette:
    policy = security_policy or ViewerSecurityPolicy()
    required_registration_secret = str(registration_secret or "").strip() or None
    # Read projections stay query_only. Web and Agent writes both go through the
    # same BridgeStore authority used by MCP and CLI.
    store = BridgeStore(database)
    repository = ViewerRepository(database)
    web_auth = WebAuthStore(
        database,
        captcha_generator=captcha_generator,
        session_ttl_seconds=policy.web_session_ttl_seconds,
    )
    resolved_email_delivery = (
        email_delivery
        if email_delivery is not None
        else SMTPEmailDelivery.from_env(public_mode=policy.public_mode)
    )
    policy.validate_runtime(
        agent_registration_secret=required_registration_secret,
        bootstrap_admin_ready=web_auth.bootstrap_admin_ready(),
        database=database,
    )
    request_limiter = SlidingWindowRateLimiter(database)
    web_session_cookie = policy.web_session_cookie_name
    runtime_instance_id = f"viewer-{secrets.token_hex(12)}"
    runtime_node_name = socket.gethostname() or "localhost"
    runtime_version = _runtime_software_version()
    lifespan, runtime_leader = build_viewer_runtime(
        store=store,
        runtime_instance_id=runtime_instance_id,
        runtime_node_name=runtime_node_name,
        runtime_version=runtime_version,
        enable_resident_repair=enable_resident_repair,
    )

    def authenticated_web_user(
        request: Request,
        *,
        allow_password_change: bool = False,
    ) -> dict[str, object]:
        identity = web_auth.authenticate(request.cookies.get(web_session_cookie))
        request.state.web_identity = identity
        if identity["must_change_password"] and not allow_password_change:
            raise WebAuthorizationError("请先修改初始密码后再使用聊天室")
        return identity

    def authenticated_admin(request: Request) -> dict[str, object]:
        identity = authenticated_web_user(request)
        if not identity["is_admin"]:
            raise WebAuthorizationError("此操作仅限管理员")
        return identity

    def web_room_access_scope(identity: dict[str, object]) -> dict[str, object]:
        return store.web_room_access_scope(
            authorized_session_id=str(identity["session_id"]),
            participant_id=str(identity["participant_id"]),
        )

    def require_web_room_access(
        identity: dict[str, object],
        conversation_id: str,
    ) -> dict[str, object]:
        return store.require_web_room_access(
            authorized_session_id=str(identity["session_id"]),
            participant_id=str(identity["participant_id"]),
            conversation_id=conversation_id,
        )

    def require_web_intent(request: Request, *, intent: str) -> None:
        if not _is_same_origin_intent(request, intent=intent, policy=policy):
            raise WebAuthorizationError("请求来源校验失败，请从当前网页重试")

    def enforce_rate(
        request: Request,
        bucket: str,
        *,
        subject: object | None = None,
        limit: int,
        window_seconds: float,
    ) -> None:
        request_limiter.check(
            bucket,
            subject if subject is not None else request_client_key(request),
            limit=limit,
            window_seconds=window_seconds,
        )

    invitation_routes, resident_management_routes = build_resident_routes(
        project_root=PROJECT_ROOT,
        store=store,
        repository=repository,
        required_registration_secret=required_registration_secret,
        enable_resident_repair=enable_resident_repair,
        authenticated_web_user=authenticated_web_user,
        authenticated_admin=authenticated_admin,
        require_web_intent=require_web_intent,
    )

    app = Starlette(
        debug=False,
        lifespan=lifespan,
        max_body_size=MAX_REQUEST_BODY_BYTES,
        routes=[
            *build_public_routes(
                web_root=WEB_ROOT,
                store=store,
                repository=repository,
                web_auth=web_auth,
                policy=policy,
                required_registration_secret=required_registration_secret,
                resolved_email_delivery=resolved_email_delivery,
                runtime_instance_id=runtime_instance_id,
                web_session_cookie=web_session_cookie,
                authenticated_web_user=authenticated_web_user,
                authenticated_admin=authenticated_admin,
                require_web_intent=require_web_intent,
                enforce_rate=enforce_rate,
            ),
            *build_auth_routes(
                web_auth=web_auth,
                policy=policy,
                web_session_cookie=web_session_cookie,
                resolved_email_delivery=resolved_email_delivery,
                authenticated_web_user=authenticated_web_user,
                require_web_intent=require_web_intent,
                enforce_rate=enforce_rate,
            ),
            *build_admin_routes(
                project_root=PROJECT_ROOT,
                store=store,
                repository=repository,
                web_auth=web_auth,
                policy=policy,
                runtime_instance_id=runtime_instance_id,
                runtime_leader=runtime_leader,
                enable_resident_repair=enable_resident_repair,
                authenticated_web_user=authenticated_web_user,
                authenticated_admin=authenticated_admin,
                web_room_access_scope=web_room_access_scope,
                require_web_intent=require_web_intent,
                enforce_rate=enforce_rate,
            ),
            *build_agent_routes(
                store=store,
                policy=policy,
                required_registration_secret=required_registration_secret,
                enforce_rate=enforce_rate,
            ),
            *invitation_routes,
            *build_event_routes(
                repository=repository,
                web_auth=web_auth,
                policy=policy,
                web_session_cookie=web_session_cookie,
                authenticated_web_user=authenticated_web_user,
                web_room_access_scope=web_room_access_scope,
                enforce_rate=enforce_rate,
            ),
            *build_room_routes(
                store=store,
                repository=repository,
                policy=policy,
                authenticated_web_user=authenticated_web_user,
                authenticated_admin=authenticated_admin,
                web_room_access_scope=web_room_access_scope,
                require_web_room_access=require_web_room_access,
                require_web_intent=require_web_intent,
                enforce_rate=enforce_rate,
            ),
            *resident_management_routes,
        ],
    )
    app.state.runtime_instance_id = runtime_instance_id
    app.state.runtime_leader = False
    if policy.public_mode:
        app.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=list(policy.allowed_hosts),
            www_redirect=False,
        )
        app.add_middleware(PublicTransportMiddleware, enabled=True)
    app.add_middleware(AdminAuditMiddleware, store=store)
    app.add_middleware(
        SecurityHeadersMiddleware,
        public_mode=policy.public_mode,
        hsts_seconds=policy.hsts_seconds,
    )
    return app


def main() -> None:
    config = BridgeConfig.from_env()
    security_policy = ViewerSecurityPolicy.from_env()
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
        create_app(
            config.database,
            registration_secret=config.registration_secret,
            enable_resident_repair=True,
            security_policy=security_policy,
        ),
        host=host,
        port=port,
        access_log=False,
        log_level="info",
        server_header=False,
        proxy_headers=security_policy.proxy_headers_enabled,
        forwarded_allow_ips=security_policy.forwarded_allow_ips or "127.0.0.1",
        ssl_certfile=(
            str(security_policy.tls_cert_file)
            if security_policy.tls_cert_file is not None
            else None
        ),
        ssl_keyfile=(
            str(security_policy.tls_key_file)
            if security_policy.tls_key_file is not None
            else None
        ),
    )


if __name__ == "__main__":
    main()
