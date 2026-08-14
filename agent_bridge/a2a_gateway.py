from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from .store import BridgeStore
from .validation import ValidationError


A2A_PROTOCOL_VERSION = "1.0"

TASK_STATE = {
    "queued": "TASK_STATE_SUBMITTED",
    "claimed": "TASK_STATE_WORKING",
    "running": "TASK_STATE_WORKING",
    "needs_input": "TASK_STATE_INPUT_REQUIRED",
    "completed": "TASK_STATE_COMPLETED",
    "failed": "TASK_STATE_FAILED",
    "cancelled": "TASK_STATE_CANCELED",
}


class A2ARequestError(ValueError):
    def __init__(self, message: str, *, code: int = -32602) -> None:
        self.code = code
        super().__init__(message)


def agent_card(base_url: str) -> dict[str, Any]:
    endpoint = f"{base_url.rstrip('/')}/a2a"
    return {
        "name": "Agent Bridge Room Task Gateway",
        "description": (
            "Submit authenticated, room-scoped structured tasks without "
            "impersonating ordinary Agent Bridge chat."
        ),
        "version": "1.0.0",
        "protocolVersion": A2A_PROTOCOL_VERSION,
        "supportedInterfaces": [
            {
                "url": endpoint,
                "protocolBinding": "JSONRPC",
                "protocolVersion": A2A_PROTOCOL_VERSION,
            }
        ],
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "extendedAgentCard": False,
        },
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "room-task",
                "name": "Room-scoped structured task",
                "description": (
                    "Create a durable task for eligible Agent members in the "
                    "room bound to the supplied access grant."
                ),
                "tags": ["task", "agent-bridge", "room"],
                "examples": ["Audit the current change and report evidence."],
                "inputModes": ["text/plain"],
                "outputModes": ["text/plain"],
            }
        ],
    }


def _required_object(value: object, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise A2ARequestError(f"{field} must be an object")
    return value


def _message_text(message: dict[str, Any]) -> str:
    parts = message.get("parts")
    if not isinstance(parts, list) or not parts:
        raise A2ARequestError("message.parts must be a non-empty array")
    texts: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            raise A2ARequestError("message parts must be objects")
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            texts.append(text.strip())
    if not texts:
        raise A2ARequestError("at least one text part is required")
    return "\n\n".join(texts)


def _task_result(task: dict[str, Any]) -> dict[str, Any]:
    status = str(task["status"])
    timestamp = datetime.fromtimestamp(
        float(task.get("updated_at") or time.time()),
        tz=timezone.utc,
    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    result: dict[str, Any] = {
        "id": str(task["task_id"]),
        "contextId": str(task["context_id"]),
        "status": {
            "state": TASK_STATE.get(status, "TASK_STATE_UNSPECIFIED"),
            "timestamp": timestamp,
        },
        "history": [
            {
                "messageId": str(task.get("request_message_id") or ""),
                "contextId": str(task["context_id"]),
                "taskId": str(task["task_id"]),
                "role": "ROLE_USER",
                "parts": [{"text": str(task["body"])}],
            }
        ],
        "metadata": {
            "agentBridgeConversationId": str(task["conversation_id"]),
            "targetParticipantIds": list(task.get("target_participant_ids") or []),
        },
    }
    summary = str(task.get("result_summary") or "").strip()
    if summary:
        if status == "completed":
            result["artifacts"] = [
                {
                    "artifactId": f"artifact_{task['task_id']}",
                    "name": "Agent Bridge task result",
                    "parts": [{"text": summary}],
                }
            ]
        else:
            result["status"]["message"] = {
                "messageId": f"status_{task['task_id']}",
                "contextId": str(task["context_id"]),
                "taskId": str(task["task_id"]),
                "role": "ROLE_AGENT",
                "parts": [{"text": summary}],
            }
    return result


def handle_jsonrpc(
    store: BridgeStore,
    *,
    access_token: str,
    request: object,
) -> dict[str, Any]:
    envelope = _required_object(request, field="request")
    request_id = envelope.get("id")
    if envelope.get("jsonrpc") != "2.0":
        raise A2ARequestError("jsonrpc must be 2.0", code=-32600)
    method = str(envelope.get("method") or "")
    params = _required_object(envelope.get("params", {}), field="params")
    try:
        if method == "SendMessage":
            message = _required_object(params.get("message"), field="message")
            metadata = message.get("metadata")
            if metadata is None:
                metadata = params.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            targets = metadata.get("targetParticipantIds")
            if targets is not None and not isinstance(targets, list):
                raise A2ARequestError(
                    "metadata.targetParticipantIds must be an array"
                )
            task = store.create_a2a_room_task(
                access_token=access_token,
                body_text=_message_text(message),
                context_id=message.get("contextId") or params.get("contextId"),
                target_participant_ids=targets,
            )
        elif method == "GetTask":
            task = store.get_a2a_room_task(
                access_token=access_token,
                task_id=str(params.get("id") or ""),
            )
        elif method == "CancelTask":
            task = store.cancel_a2a_room_task(
                access_token=access_token,
                task_id=str(params.get("id") or ""),
            )
        else:
            raise A2ARequestError(f"method not found: {method}", code=-32601)
    except ValidationError as exc:
        raise A2ARequestError(str(exc)) from exc
    return {"jsonrpc": "2.0", "id": request_id, "result": _task_result(task)}


def jsonrpc_error(
    *,
    request_id: object,
    code: int,
    message: str,
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": int(code), "message": str(message)},
    }
