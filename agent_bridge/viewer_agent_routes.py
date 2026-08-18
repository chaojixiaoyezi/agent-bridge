from __future__ import annotations

from starlette.routing import Route

from .security import ViewerSecurityPolicy
from .store import BridgeStore
from .viewer_agent_chat_routes import build_agent_chat_routes
from .viewer_agent_enrollment_routes import build_agent_enrollment_routes
from .viewer_agent_native_routes import build_agent_native_routes
from .viewer_agent_task_routes import build_agent_task_routes


def build_agent_routes(
    *,
    store: BridgeStore,
    policy: ViewerSecurityPolicy,
    required_registration_secret: str | None,
    enforce_rate,
) -> list[Route]:
    return [
        *build_agent_enrollment_routes(
            store=store,
            policy=policy,
            required_registration_secret=required_registration_secret,
            enforce_rate=enforce_rate,
        ),
        *build_agent_native_routes(store=store),
        *build_agent_chat_routes(
            store=store,
            policy=policy,
            enforce_rate=enforce_rate,
        ),
        *build_agent_task_routes(store=store),
    ]
