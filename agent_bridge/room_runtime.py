"""Integration-room identity limits and durable native runtime projection."""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from .store_constants import OWNER_PARTICIPANT_ID
from .store_errors import AuthorizationError, ConflictError, NotFoundError
from .validation import ValidationError, opaque_id, token


ROOM_KINDS = frozenset({"chat", "integration"})
ROOM_RUNTIME_EVENT_KINDS = frozenset(
    {
        "turn_started",
        "assistant_text",
        "tool_started",
        "tool_completed",
        "tool_failed",
        "approval_required",
        "turn_completed",
        "runtime_error",
    }
)
MAX_RUNTIME_EVENT_SUMMARY_CHARS = 12_000


ROOM_RUNTIME_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS room_runtime_events (
    event_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    source TEXT NOT NULL,
    native_session_id TEXT,
    event_kind TEXT NOT NULL
        CHECK (event_kind IN (
            'turn_started', 'assistant_text', 'tool_started',
            'tool_completed', 'tool_failed', 'approval_required',
            'turn_completed', 'runtime_error'
        )),
    tool_use_id TEXT,
    tool_name TEXT,
    summary TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    FOREIGN KEY (task_id) REFERENCES room_tasks(task_id),
    FOREIGN KEY (conversation_id) REFERENCES rooms(conversation_id),
    FOREIGN KEY (participant_id) REFERENCES participants(participant_id)
);

CREATE INDEX IF NOT EXISTS idx_room_runtime_events_task_sequence
    ON room_runtime_events(task_id, event_sequence);
CREATE INDEX IF NOT EXISTS idx_room_runtime_events_room_sequence
    ON room_runtime_events(conversation_id, event_sequence);

CREATE TRIGGER IF NOT EXISTS trg_integration_room_one_agent_insert
BEFORE INSERT ON memberships
WHEN NEW.active = 1
 AND EXISTS (
    SELECT 1 FROM rooms
    WHERE conversation_id = NEW.conversation_id
      AND room_kind = 'integration'
 )
 AND NEW.participant_id != '{OWNER_PARTICIPANT_ID}'
 AND NOT EXISTS (
    SELECT 1 FROM web_users
    WHERE participant_id = NEW.participant_id
 )
 AND EXISTS (
    SELECT 1
    FROM memberships AS existing
    LEFT JOIN web_users AS web_user
      ON web_user.participant_id = existing.participant_id
    WHERE existing.conversation_id = NEW.conversation_id
      AND existing.active = 1
      AND existing.participant_id != NEW.participant_id
      AND existing.participant_id != '{OWNER_PARTICIPANT_ID}'
      AND web_user.user_id IS NULL
 )
BEGIN
    SELECT RAISE(ABORT, 'INTEGRATION_ROOM_AGENT_LIMIT');
END;

CREATE TRIGGER IF NOT EXISTS trg_integration_room_one_agent_update
BEFORE UPDATE OF active, conversation_id, participant_id ON memberships
WHEN NEW.active = 1
 AND EXISTS (
    SELECT 1 FROM rooms
    WHERE conversation_id = NEW.conversation_id
      AND room_kind = 'integration'
 )
 AND NEW.participant_id != '{OWNER_PARTICIPANT_ID}'
 AND NOT EXISTS (
    SELECT 1 FROM web_users
    WHERE participant_id = NEW.participant_id
 )
 AND EXISTS (
    SELECT 1
    FROM memberships AS existing
    LEFT JOIN web_users AS web_user
      ON web_user.participant_id = existing.participant_id
    WHERE existing.conversation_id = NEW.conversation_id
      AND existing.active = 1
      AND existing.participant_id != NEW.participant_id
      AND existing.participant_id != '{OWNER_PARTICIPANT_ID}'
      AND web_user.user_id IS NULL
 )
BEGIN
    SELECT RAISE(ABORT, 'INTEGRATION_ROOM_AGENT_LIMIT');
END;
"""


def normalize_room_kind(value: object) -> str:
    normalized = str(value or "chat").strip().casefold()
    if normalized not in ROOM_KINDS:
        raise ValidationError("room_kind must be chat or integration")
    return normalized


class RoomRuntimeMixin:
    @staticmethod
    def _assert_integration_agent_slot_locked(
        conn: sqlite3.Connection,
        *,
        conversation_id: str,
        participant_id: str,
    ) -> None:
        room = conn.execute(
            "SELECT room_kind FROM rooms WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if room is None or str(room["room_kind"]) != "integration":
            return
        existing = conn.execute(
            """
            SELECT membership.participant_id
            FROM memberships AS membership
            LEFT JOIN web_users AS web_user
              ON web_user.participant_id = membership.participant_id
            WHERE membership.conversation_id = ?
              AND membership.active = 1
              AND membership.participant_id != ?
              AND membership.participant_id != ?
              AND web_user.user_id IS NULL
            LIMIT 1
            """,
            (conversation_id, participant_id, OWNER_PARTICIPANT_ID),
        ).fetchone()
        if existing is not None:
            raise ConflictError("整合聊天室只允许加入一个 Agent")

    def append_room_runtime_event(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        task_id: str,
        event_id: str,
        source: str,
        event_kind: str,
        native_session_id: str | None = None,
        tool_use_id: str | None = None,
        tool_name: str | None = None,
        summary: str | None = None,
    ) -> dict[str, Any]:
        """Append one idempotent, display-safe event from the claiming task seat."""

        participant = opaque_id(participant_id, field="participant_id")
        session = opaque_id(authorized_session_id, field="authorized_session_id")
        task = opaque_id(task_id, field="task_id")
        event = opaque_id(event_id, field="event_id")
        normalized_source = token(source, field="source")
        normalized_kind = str(event_kind or "").strip().casefold()
        if normalized_kind not in ROOM_RUNTIME_EVENT_KINDS:
            raise ValidationError("unsupported room runtime event kind")

        def optional_text(value: object, *, field: str, maximum: int = 200) -> str | None:
            if value is None:
                return None
            normalized = str(value).strip()
            if not normalized:
                return None
            if len(normalized) > maximum or any(ord(char) < 32 for char in normalized):
                raise ValidationError(f"{field} is invalid")
            return normalized

        native_session = optional_text(
            native_session_id,
            field="native_session_id",
            maximum=200,
        )
        normalized_tool_use_id = optional_text(tool_use_id, field="tool_use_id")
        normalized_tool_name = optional_text(tool_name, field="tool_name")
        normalized_summary = str(summary or "").strip()
        if len(normalized_summary) > MAX_RUNTIME_EVENT_SUMMARY_CHARS:
            raise ValidationError(
                f"summary must be at most {MAX_RUNTIME_EVENT_SUMMARY_CHARS} characters"
            )
        now = time.time()
        with self._transaction() as conn:
            session_row = self._require_live_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=now,
            )
            task_row = conn.execute(
                """
                SELECT task.*, room.room_kind
                FROM room_tasks AS task
                JOIN rooms AS room
                  ON room.conversation_id = task.conversation_id
                WHERE task.task_id = ?
                """,
                (task,),
            ).fetchone()
            if task_row is None:
                raise NotFoundError(f"unknown task: {task}")
            conversation = str(task_row["conversation_id"])
            if conversation != str(session_row["registered_conversation_id"]):
                raise AuthorizationError("task belongs to another room")
            if str(task_row["room_kind"]) != "integration":
                raise ConflictError(
                    "runtime projection is only available in integration rooms"
                )
            if str(task_row["claimed_by_participant_id"] or "") != participant:
                raise AuthorizationError(
                    "only the Agent that claimed this task may project runtime events"
                )
            if str(task_row["status"]) not in {"claimed", "running", "needs_input"}:
                raise ConflictError("task is no longer accepting runtime events")
            conn.execute(
                """
                INSERT INTO room_runtime_events
                    (event_id, task_id, conversation_id, participant_id,
                     source, native_session_id, event_kind, tool_use_id,
                     tool_name, summary, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO NOTHING
                """,
                (
                    event,
                    task,
                    conversation,
                    participant,
                    normalized_source,
                    native_session,
                    normalized_kind,
                    normalized_tool_use_id,
                    normalized_tool_name,
                    normalized_summary,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM room_runtime_events WHERE event_id = ?",
                (event,),
            ).fetchone()
            if row is None:
                raise NotFoundError("runtime event disappeared")
            expected_payload = (
                task,
                conversation,
                participant,
                normalized_source,
                native_session,
                normalized_kind,
                normalized_tool_use_id,
                normalized_tool_name,
                normalized_summary,
            )
            stored_payload = (
                str(row["task_id"]),
                str(row["conversation_id"]),
                str(row["participant_id"]),
                str(row["source"]),
                (
                    str(row["native_session_id"])
                    if row["native_session_id"] is not None
                    else None
                ),
                str(row["event_kind"]),
                str(row["tool_use_id"]) if row["tool_use_id"] is not None else None,
                str(row["tool_name"]) if row["tool_name"] is not None else None,
                str(row["summary"] or ""),
            )
            if stored_payload != expected_payload:
                raise ConflictError(
                    "runtime event id was already used with a different payload"
                )
            conn.execute(
                "UPDATE room_tasks SET updated_at = MAX(updated_at, ?) "
                "WHERE task_id = ?",
                (now, task),
            )
            conn.execute(
                "UPDATE rooms SET last_activity_at = MAX(last_activity_at, ?) "
                "WHERE conversation_id = ? AND status = 'active'",
                (now, conversation),
            )
        return self._room_runtime_event_payload(row)

    @staticmethod
    def _room_runtime_event_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "event_sequence": int(row["event_sequence"]),
            "event_id": str(row["event_id"]),
            "task_id": str(row["task_id"]),
            "conversation_id": str(row["conversation_id"]),
            "participant_id": str(row["participant_id"]),
            "source": str(row["source"]),
            "native_session_id": (
                str(row["native_session_id"])
                if row["native_session_id"] is not None
                else None
            ),
            "event_kind": str(row["event_kind"]),
            "tool_use_id": (
                str(row["tool_use_id"]) if row["tool_use_id"] is not None else None
            ),
            "tool_name": (
                str(row["tool_name"]) if row["tool_name"] is not None else None
            ),
            "summary": str(row["summary"] or ""),
            "created_at": float(row["created_at"]),
        }
