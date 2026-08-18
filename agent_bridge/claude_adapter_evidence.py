"""Structured Agent Bridge tool-evidence parsing for Claude stream output."""

from __future__ import annotations

import json
from typing import Any

from .claude_adapter_contracts import BRIDGE_TOOLS, ClaudeToolEvidence


def _walk_objects(value: Any):
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _walk_objects(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_objects(nested)
    elif isinstance(value, str) and len(value) <= 1_000_000:
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                decoded = json.loads(stripped)
            except json.JSONDecodeError:
                return
            yield from _walk_objects(decoded)


def _bridge_tool_name(value: object) -> str | None:
    name = str(value or "")
    for tool in BRIDGE_TOOLS:
        if name == tool or name.endswith(f"__{tool}"):
            return tool
    return None


def _mention_ids(value: Any) -> set[str]:
    result: set[str] = set()
    for item in _walk_objects(value):
        messages = item.get("messages")
        if not isinstance(messages, list):
            continue
        for message in messages:
            if not isinstance(message, dict):
                continue
            delivery = message.get("delivery")
            if not isinstance(delivery, dict):
                delivery = message
            priority = str(delivery.get("priority") or "")
            reasons = delivery.get("reasons")
            requires_reply = (
                bool({"mention", "agent_request"}.intersection(reasons))
                if isinstance(reasons, list)
                else priority in {"mention", "direct"}
            )
            if not requires_reply:
                continue
            message_id = str(message.get("message_id") or "")
            if message_id:
                result.add(message_id)
    return result


def _wait_result_evidence(
    value: Any,
) -> tuple[set[str], set[str], int | None]:
    inspected: set[str] = set()
    mentions: set[str] = set()
    required_count: int | None = None
    for item in _walk_objects(value):
        backlog = item.get("backlog")
        if isinstance(backlog, dict) and "required_reply_count" in backlog:
            observed = max(0, int(backlog.get("required_reply_count") or 0))
            required_count = max(required_count or 0, observed)
        messages = item.get("messages")
        if not isinstance(messages, list):
            continue
        for message in messages:
            if not isinstance(message, dict):
                continue
            message_id = str(message.get("message_id") or "")
            if message_id:
                inspected.add(message_id)
        mentions.update(_mention_ids(item))
    return inspected, mentions, required_count


def _tool_evidence(output: str) -> ClaudeToolEvidence:
    tool_uses: dict[str, tuple[str, dict[str, Any]]] = {}
    successful_results: dict[str, Any] = {}
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        for item in _walk_objects(event):
            item_type = str(item.get("type") or "")
            if item_type == "tool_use":
                tool_name = _bridge_tool_name(item.get("name"))
                tool_use_id = str(item.get("id") or "")
                tool_input = item.get("input")
                if tool_name and tool_use_id and isinstance(tool_input, dict):
                    tool_uses[tool_use_id] = (tool_name, tool_input)
            elif item_type == "tool_result":
                tool_use_id = str(item.get("tool_use_id") or "")
                if tool_use_id and not bool(item.get("is_error", False)):
                    successful_results[tool_use_id] = item.get("content")

    successful_tools: set[str] = set()
    inspected_messages: set[str] = set()
    resolved_messages: set[str] = set()
    awaited_mentions: set[str] = set()
    replied_mentions: set[str] = set()
    nickname_requested = False
    required_reply_count_observed: int | None = None
    for tool_use_id, (tool_name, tool_input) in tool_uses.items():
        if tool_use_id not in successful_results:
            continue
        successful_tools.add(tool_name)
        if tool_name == "agent_wait":
            inspected, mentions, required_count = _wait_result_evidence(
                successful_results[tool_use_id]
            )
            inspected_messages.update(inspected)
            awaited_mentions.update(mentions)
            if required_count is not None:
                required_reply_count_observed = max(
                    required_reply_count_observed or 0,
                    required_count,
                )
        elif tool_name == "agent_reply":
            message_id = str(tool_input.get("message_id") or "")
            if message_id:
                replied_mentions.add(message_id)
                resolved_messages.add(message_id)
        elif (
            tool_name == "agent_message_action"
            and str(tool_input.get("action") or "") == "ack"
        ):
            message_id = str(tool_input.get("message_id") or "")
            if message_id:
                resolved_messages.add(message_id)
        elif tool_name == "agent_request_nickname":
            nickname_requested = True
    return ClaudeToolEvidence(
        successful_tools=frozenset(successful_tools),
        inspected_messages=frozenset(inspected_messages),
        resolved_messages=frozenset(resolved_messages),
        awaited_mentions=frozenset(awaited_mentions),
        replied_mentions=frozenset(replied_mentions),
        nickname_requested=nickname_requested,
        required_reply_count_observed=required_reply_count_observed,
    )
