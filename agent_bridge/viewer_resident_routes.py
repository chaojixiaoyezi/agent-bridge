"""Composition root for Web-side Agent access and resident management."""

from __future__ import annotations

from pathlib import Path

from starlette.routing import Route

from .store import BridgeStore
from .viewer_resident_access_routes import build_resident_access_routes
from .viewer_resident_management_routes import build_resident_management_routes
from .viewer_store import ViewerRepository


def build_resident_routes(
    *,
    project_root: Path,
    store: BridgeStore,
    repository: ViewerRepository,
    required_registration_secret: str | None,
    enable_resident_repair: bool,
    authenticated_web_user,
    authenticated_admin,
    require_web_intent,
) -> tuple[list[Route], list[Route]]:
    invitation_routes = build_resident_access_routes(
        project_root=project_root,
        store=store,
        required_registration_secret=required_registration_secret,
        authenticated_web_user=authenticated_web_user,
        require_web_intent=require_web_intent,
    )
    management_routes = build_resident_management_routes(
        store=store,
        repository=repository,
        enable_resident_repair=enable_resident_repair,
        authenticated_admin=authenticated_admin,
        require_web_intent=require_web_intent,
    )
    return invitation_routes, management_routes
