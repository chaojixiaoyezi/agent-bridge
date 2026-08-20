"""Stable public error hierarchy for Agent Bridge persistence operations."""

from __future__ import annotations


class BridgeError(RuntimeError):
    """Base error returned to MCP/CLI callers as structured failure text."""


class NotFoundError(BridgeError):
    pass


class ConflictError(BridgeError):
    pass


class NativeSessionLeaseExpiredError(ConflictError):
    """The exact native process still owns a lease whose liveness TTL elapsed."""

    error_code = "native_session_lease_expired"


class NativeSessionLeaseEndedError(ConflictError):
    """The native process explicitly ended its exact-session lease."""

    error_code = "native_session_lease_ended"


class NativeSessionLeaseSupersededError(ConflictError):
    """A different exact native process now owns the connector."""

    error_code = "native_session_lease_superseded"


class NativeDeliveryInactiveError(ConflictError):
    """The connector no longer routes chat through its native TUI."""

    error_code = "native_delivery_inactive"


class RateLimitError(ConflictError):
    def __init__(self, *, retry_after_seconds: float, conversation_id: str) -> None:
        self.retry_after_seconds = max(0.0, float(retry_after_seconds))
        self.conversation_id = conversation_id
        super().__init__(
            "message rate limit: wait "
            f"{self.retry_after_seconds:.3f} seconds before speaking again in "
            f"conversation {conversation_id}"
        )


class NicknameRateLimitError(ConflictError):
    def __init__(self, *, retry_after_seconds: float) -> None:
        self.retry_after_seconds = max(0.0, float(retry_after_seconds))
        super().__init__(
            "nickname changes may be requested at most once every 24 hours; "
            f"retry after {self.retry_after_seconds:.3f} seconds"
        )


class AvatarRateLimitError(ConflictError):
    def __init__(self, *, retry_after_seconds: float) -> None:
        self.retry_after_seconds = max(0.0, float(retry_after_seconds))
        super().__init__(
            "Agent avatars may be changed at most once every 24 hours; "
            f"retry after {self.retry_after_seconds:.3f} seconds"
        )


class AuthenticationError(BridgeError):
    pass


class AuthorizationError(BridgeError):
    pass
