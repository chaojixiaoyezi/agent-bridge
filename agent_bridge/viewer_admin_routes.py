"""Composition root for authenticated Web and administrator routes."""

from __future__ import annotations

import asyncio
from pathlib import Path

from starlette.routing import Route

from .security import ViewerSecurityPolicy
from .store import BridgeStore
from .viewer_admin_access_routes import build_admin_access_routes
from .viewer_admin_observability_routes import build_admin_observability_routes
from .viewer_admin_operation_routes import build_admin_operation_routes
from .viewer_admin_rate_session_routes import build_admin_rate_session_routes
from .viewer_store import ViewerRepository
from .web_auth import WebAuthStore


def build_admin_routes(
    *,
    project_root: Path,
    store: BridgeStore,
    repository: ViewerRepository,
    web_auth: WebAuthStore,
    policy: ViewerSecurityPolicy,
    runtime_instance_id: str,
    runtime_leader: asyncio.Event,
    enable_resident_repair: bool,
    authenticated_web_user,
    authenticated_admin,
    web_room_access_scope,
    require_web_intent,
    enforce_rate,
) -> list[Route]:
    return [
        *build_admin_access_routes(
            store=store,
            repository=repository,
            web_auth=web_auth,
            authenticated_web_user=authenticated_web_user,
            authenticated_admin=authenticated_admin,
            web_room_access_scope=web_room_access_scope,
            require_web_intent=require_web_intent,
        ),
        *build_admin_observability_routes(
            store=store,
            repository=repository,
            policy=policy,
            runtime_instance_id=runtime_instance_id,
            runtime_leader=runtime_leader,
            authenticated_admin=authenticated_admin,
            require_web_intent=require_web_intent,
            enforce_rate=enforce_rate,
        ),
        *build_admin_operation_routes(
            project_root=project_root,
            store=store,
            repository=repository,
            enable_resident_repair=enable_resident_repair,
            authenticated_web_user=authenticated_web_user,
            authenticated_admin=authenticated_admin,
            require_web_intent=require_web_intent,
        ),
        *build_admin_rate_session_routes(
            store=store,
            repository=repository,
            authenticated_admin=authenticated_admin,
            require_web_intent=require_web_intent,
        ),
    ]
