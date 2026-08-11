"""Local durable mailbox shared by multiple agent clients."""

from .store import BridgeError, BridgeStore

__all__ = ["BridgeError", "BridgeStore"]
