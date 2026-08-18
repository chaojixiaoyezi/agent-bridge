"""Shared contracts and wake-batch validation for the Claude adapter."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any


SENSITIVE_CHILD_ENV = {
    "AGENT_BRIDGE_TOKEN",
    "AGENT_TOKEN",
    "AGENT_BRIDGE_INVITATION_TOKEN",
    "AGENT_BRIDGE_ENROLLMENT_TOKEN",
    "AGENT_BRIDGE_REGISTRATION_SECRET",
    "AGENT_BRIDGE_DB",
    "AGENT_BRIDGE_HOME",
}


BRIDGE_TOOLS = (
    "agent_wait",
    "agent_reply",
    "agent_message_action",
    "agent_history",
    "agent_search_history",
    "agent_download_attachment",
    "agent_participants",
    "agent_heartbeat",
    "agent_update_profile",
    "agent_list_avatars",
    "agent_request_nickname",
    "agent_set_room_dnd",
)


MODEL_BRIDGE_TOOLS = tuple(tool for tool in BRIDGE_TOOLS if tool != "agent_wait")


MAX_PREFETCH_PAGES = 5


MAX_FALLBACK_REPLY_CHARS = 10_000


class ClaudeAdapterError(RuntimeError):
    pass


@dataclass(frozen=True)
class ClaudeToolEvidence:
    successful_tools: frozenset[str]
    inspected_messages: frozenset[str]
    resolved_messages: frozenset[str]
    awaited_mentions: frozenset[str]
    replied_mentions: frozenset[str]
    nickname_requested: bool
    required_reply_count_observed: int | None


def _required_reply_count(batch: dict[str, Any]) -> int:
    if "required_reply_count" in batch:
        return max(0, int(batch.get("required_reply_count") or 0))
    counts = batch.get("priority_counts")
    return max(0, int(counts.get("mention") or 0)) if isinstance(counts, dict) else 0


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ClaudeAdapterError(f"{name} is required")
    return value


def _validated_batch(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > 65_536:
        raise ClaudeAdapterError("wake batch must contain 1-65536 bytes")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClaudeAdapterError("wake batch must be one UTF-8 JSON object") from exc
    if not isinstance(payload, dict):
        raise ClaudeAdapterError("wake batch must be one JSON object")
    if (
        payload.get("schema_version") != 1
        or payload.get("source") != "agent-bridge-supervisor"
        or payload.get("event") != "wake_batch"
    ):
        raise ClaudeAdapterError("wake batch source or schema is invalid")
    if str(payload.get("wake_priority") or "") not in {
        "normal",
        "important",
        "mention",
    }:
        raise ClaudeAdapterError("wake batch priority is invalid")
    event_count = payload.get("event_count")
    if (
        isinstance(event_count, bool)
        or not isinstance(event_count, int)
        or event_count < 1
    ):
        raise ClaudeAdapterError("wake batch event_count is invalid")
    return payload
