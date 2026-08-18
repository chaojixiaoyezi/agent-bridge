"""Agent task claim, input, update, and delegation routes."""

from __future__ import annotations

import asyncio

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .store import BridgeStore
from .validation import ValidationError
from .viewer_http import (
    _agent_json_call,
    _authenticate_request,
    _json_body,
    _json_error,
)


def build_agent_task_routes(*, store: BridgeStore) -> list[Route]:
    async def agent_task_next(request: Request) -> Response:
        try:
            auth = _authenticate_request(request, store)
            payload = await _json_body(
                request,
                required=set(),
                allowed={"wait_seconds"},
            )
            result = await asyncio.to_thread(
                store.wait_next_task,
                participant_id=auth["participant_id"],
                authorized_session_id=auth["session_id"],
                wait_seconds=payload.get("wait_seconds", 20),
            )
            if auth.get("connector_id"):
                await asyncio.to_thread(
                    store.touch_agent_connector,
                    participant_id=auth["participant_id"],
                    authorized_session_id=auth["session_id"],
                    connector_id=auth["connector_id"],
                )
            return JSONResponse(result)
        except Exception as exc:
            return _json_error(exc)

    async def agent_task_inputs(request: Request) -> Response:
        try:
            auth = _authenticate_request(request, store)
            payload = await _json_body(
                request,
                required={"task_id", "action"},
                allowed={"task_id", "action", "input_ids", "limit"},
            )
            action = str(payload["action"] or "").strip().lower()
            if action == "poll":
                result = await asyncio.to_thread(
                    store.poll_agent_task_inputs,
                    participant_id=auth["participant_id"],
                    authorized_session_id=auth["session_id"],
                    task_id=payload["task_id"],
                    limit=payload.get("limit", 50),
                )
            elif action == "ack":
                input_ids = payload.get("input_ids")
                if not isinstance(input_ids, list):
                    raise ValidationError("input_ids must be a list")
                result = await asyncio.to_thread(
                    store.acknowledge_agent_task_inputs,
                    participant_id=auth["participant_id"],
                    authorized_session_id=auth["session_id"],
                    task_id=payload["task_id"],
                    input_ids=input_ids,
                )
            else:
                raise ValidationError("unsupported task input action")
            if auth.get("connector_id"):
                await asyncio.to_thread(
                    store.touch_agent_connector,
                    participant_id=auth["participant_id"],
                    authorized_session_id=auth["session_id"],
                    connector_id=auth["connector_id"],
                )
            return JSONResponse(result)
        except Exception as exc:
            return _json_error(exc)

    async def agent_task_update(request: Request) -> Response:
        return await _agent_json_call(
            request,
            store,
            required={"task_id", "status"},
            allowed={
                "task_id",
                "status",
                "result_summary",
                "execution_cwd",
                "execution_thread_id",
            },
            operation=lambda auth, payload: {
                "task": store.update_agent_task(
                    participant_id=auth["participant_id"],
                    authorized_session_id=auth["session_id"],
                    task_id=payload["task_id"],
                    status=payload["status"],
                    result_summary=payload.get("result_summary"),
                    execution_cwd=payload.get("execution_cwd"),
                    execution_thread_id=payload.get("execution_thread_id"),
                )
            },
        )

    async def agent_task_delegate(request: Request) -> Response:
        return await _agent_json_call(
            request,
            store,
            required={"parent_task_id", "body", "target_participant_ids"},
            allowed={"parent_task_id", "body", "target_participant_ids"},
            operation=lambda auth, payload: {
                "task": store.delegate_agent_task(
                    participant_id=auth["participant_id"],
                    authorized_session_id=auth["session_id"],
                    parent_task_id=payload["parent_task_id"],
                    body_text=payload["body"],
                    target_participant_ids=payload["target_participant_ids"],
                )
            },
        )

    return [
            Route("/agent/tasks/next", agent_task_next, methods=["POST"]),
            Route("/agent/tasks/inputs", agent_task_inputs, methods=["POST"]),
            Route("/agent/tasks/update", agent_task_update, methods=["POST"]),
            Route("/agent/tasks/delegate", agent_task_delegate, methods=["POST"]),
    ]
