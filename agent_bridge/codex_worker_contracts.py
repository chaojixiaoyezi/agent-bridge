"""Shared contracts for the resident Codex worker layers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


SENSITIVE_CHILD_ENV = {
    "AGENT_BRIDGE_TOKEN",
    "AGENT_TOKEN",
    "AGENT_BRIDGE_REGISTRATION_SECRET",
    "AGENT_BRIDGE_INVITATION_TOKEN",
    "AGENT_BRIDGE_ENROLLMENT_TOKEN",
    "AGENT_BRIDGE_DB",
    "AGENT_BRIDGE_HOME",
}


THREAD_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


BRIDGE_MCP_TOOLS = (
    "agent_heartbeat",
    "agent_wait",
    "agent_notifications",
    "agent_message_action",
    "agent_reply",
    "agent_send",
    "agent_history",
    "agent_search_history",
    "agent_participants",
    "agent_update_profile",
    "agent_list_avatars",
    "agent_request_nickname",
    "agent_set_room_dnd",
)


class CodexWorkerError(RuntimeError):
    pass


@dataclass
class TurnEvidence:
    completed_bridge_tools: set[str] = field(default_factory=set)
    failed_bridge_tools: list[str] = field(default_factory=list)
    inspected_message_ids: set[str] = field(default_factory=set)
    resolved_message_ids: set[str] = field(default_factory=set)
    mention_message_ids: set[str] = field(default_factory=set)
    replied_message_ids: set[str] = field(default_factory=set)
    required_reply_count_observed: int | None = None


def _required_reply_count(batch: dict[str, Any]) -> int:
    if "required_reply_count" in batch:
        return max(0, int(batch.get("required_reply_count") or 0))
    counts = batch.get("priority_counts")
    return max(0, int(counts.get("mention") or 0)) if isinstance(counts, dict) else 0
