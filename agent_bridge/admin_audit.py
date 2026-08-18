"""Append-only administrator governance audit ledger."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any

from .validation import (
    ValidationError,
    alias,
    compact_json,
    conversation_id as validate_conversation_id,
    opaque_id,
)


ADMIN_AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS admin_audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    occurred_at REAL NOT NULL,
    actor_web_user_id TEXT NOT NULL,
    actor_username TEXT NOT NULL,
    actor_display_name TEXT NOT NULL,
    actor_role TEXT NOT NULL,
    category TEXT NOT NULL,
    action TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('success', 'denied', 'failed')),
    status_code INTEGER NOT NULL CHECK (status_code BETWEEN 100 AND 599),
    http_method TEXT NOT NULL,
    route TEXT NOT NULL,
    conversation_id TEXT,
    target_kind TEXT,
    target_id TEXT,
    request_id TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (actor_web_user_id) REFERENCES web_users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_admin_audit_events_time
    ON admin_audit_events(occurred_at DESC, sequence DESC);
CREATE INDEX IF NOT EXISTS idx_admin_audit_events_actor
    ON admin_audit_events(actor_web_user_id, sequence DESC);
CREATE INDEX IF NOT EXISTS idx_admin_audit_events_category_outcome
    ON admin_audit_events(category, outcome, sequence DESC);
CREATE INDEX IF NOT EXISTS idx_admin_audit_events_room
    ON admin_audit_events(conversation_id, sequence DESC)
    WHERE conversation_id IS NOT NULL;

CREATE TRIGGER IF NOT EXISTS trg_admin_audit_events_no_update
BEFORE UPDATE ON admin_audit_events
BEGIN
    SELECT RAISE(ABORT, 'ADMIN_AUDIT_APPEND_ONLY');
END;

CREATE TRIGGER IF NOT EXISTS trg_admin_audit_events_no_delete
BEFORE DELETE ON admin_audit_events
BEGIN
    SELECT RAISE(ABORT, 'ADMIN_AUDIT_APPEND_ONLY');
END;
"""


class AdminAuditMixin:
    @staticmethod
    def _admin_audit_event_payload(row: sqlite3.Row) -> dict[str, Any]:
        try:
            detail = json.loads(str(row["detail_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            detail = {}
        return {
            "sequence": int(row["sequence"]),
            "event_id": str(row["event_id"]),
            "occurred_at": float(row["occurred_at"]),
            "actor_web_user_id": str(row["actor_web_user_id"]),
            "actor_username": str(row["actor_username"]),
            "actor_display_name": str(row["actor_display_name"]),
            "actor_role": str(row["actor_role"]),
            "category": str(row["category"]),
            "action": str(row["action"]),
            "outcome": str(row["outcome"]),
            "status_code": int(row["status_code"]),
            "http_method": str(row["http_method"]),
            "route": str(row["route"]),
            "conversation_id": (
                str(row["conversation_id"])
                if row["conversation_id"] is not None
                else None
            ),
            "target_kind": (
                str(row["target_kind"])
                if row["target_kind"] is not None
                else None
            ),
            "target_id": (
                str(row["target_id"])
                if row["target_id"] is not None
                else None
            ),
            "request_id": str(row["request_id"]),
            "detail": detail if isinstance(detail, dict) else {},
        }

    def record_admin_audit_event(
        self,
        *,
        actor_web_user_id: str,
        actor_username: str,
        actor_display_name: str,
        actor_role: str,
        category: str,
        action: str,
        outcome: str,
        status_code: int,
        http_method: str,
        route: str,
        request_id: str,
        conversation_id: str | None = None,
        target_kind: str | None = None,
        target_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append a sanitized Web governance event without request bodies.

        This ledger deliberately records route metadata and stable target IDs,
        never passwords, registration codes, invitation tokens, message bodies,
        email addresses, cookies, or authorization headers.
        """

        actor = opaque_id(actor_web_user_id, field="actor_web_user_id")
        username = alias(actor_username, field="actor_username")
        display = alias(actor_display_name, field="actor_display_name")
        role = alias(actor_role, field="actor_role")
        normalized_category = alias(category, field="audit.category")
        normalized_action = alias(action, field="audit.action")
        normalized_outcome = str(outcome or "").strip().lower()
        if normalized_outcome not in {"success", "denied", "failed"}:
            raise ValidationError("audit outcome is invalid")
        try:
            normalized_status = int(status_code)
        except (TypeError, ValueError) as exc:
            raise ValidationError("audit status_code must be an integer") from exc
        if not 100 <= normalized_status <= 599:
            raise ValidationError("audit status_code must be between 100 and 599")
        normalized_method = str(http_method or "").strip().upper()
        if normalized_method not in {"POST", "PUT", "PATCH", "DELETE"}:
            raise ValidationError("audit http_method is invalid")
        normalized_route = alias(route, field="audit.route")
        normalized_request = opaque_id(request_id, field="audit.request_id")
        normalized_conversation = (
            validate_conversation_id(conversation_id)
            if conversation_id is not None
            else None
        )
        normalized_target_kind = (
            alias(target_kind, field="audit.target_kind")
            if target_kind is not None
            else None
        )
        normalized_target_id = (
            alias(target_id, field="audit.target_id")
            if target_id is not None
            else None
        )
        if (normalized_target_kind is None) != (normalized_target_id is None):
            raise ValidationError("audit target kind and id must be provided together")
        detail_payload = detail if isinstance(detail, dict) else {}
        detail_json = compact_json(detail_payload)
        if len(detail_json) > 4096:
            raise ValidationError("audit detail is too large")
        occurred_at = time.time()
        event_id = f"audit_{uuid.uuid4().hex}"
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO admin_audit_events (
                    event_id, occurred_at, actor_web_user_id, actor_username,
                    actor_display_name, actor_role, category, action, outcome,
                    status_code, http_method, route, conversation_id,
                    target_kind, target_id, request_id, detail_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    occurred_at,
                    actor,
                    username,
                    display,
                    role,
                    normalized_category,
                    normalized_action,
                    normalized_outcome,
                    normalized_status,
                    normalized_method,
                    normalized_route,
                    normalized_conversation,
                    normalized_target_kind,
                    normalized_target_id,
                    normalized_request,
                    detail_json,
                ),
            )
            row = conn.execute(
                "SELECT * FROM admin_audit_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        return self._admin_audit_event_payload(row)

    def admin_audit_events(
        self,
        *,
        requesting_web_user_id: str,
        limit: object = 100,
        before_sequence: object | None = None,
        query: object = "",
        category: object = "",
        outcome: object = "",
        actor_web_user_id: object = "",
        conversation_id: object = "",
        hours: object = 168,
    ) -> dict[str, Any]:
        requester = opaque_id(
            requesting_web_user_id,
            field="requesting_web_user_id",
        )
        if isinstance(limit, bool):
            raise ValidationError("audit limit must be an integer")
        try:
            normalized_limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise ValidationError("audit limit must be an integer") from exc
        if not 1 <= normalized_limit <= 200:
            raise ValidationError("audit limit must be between 1 and 200")
        normalized_before: int | None = None
        if before_sequence is not None and before_sequence != "":
            if isinstance(before_sequence, bool):
                raise ValidationError("audit cursor must be an integer")
            try:
                normalized_before = int(before_sequence)
            except (TypeError, ValueError) as exc:
                raise ValidationError("audit cursor must be an integer") from exc
            if normalized_before <= 0:
                raise ValidationError("audit cursor must be positive")
        if isinstance(hours, bool):
            raise ValidationError("audit hours must be an integer")
        try:
            normalized_hours = int(hours)
        except (TypeError, ValueError) as exc:
            raise ValidationError("audit hours must be an integer") from exc
        if normalized_hours not in {0, 24, 168, 720}:
            raise ValidationError("audit hours must be 0, 24, 168, or 720")
        normalized_query = str(query or "").strip()
        if len(normalized_query) > 200:
            raise ValidationError("audit query exceeds 200 characters")
        normalized_category = str(category or "").strip()
        if normalized_category:
            normalized_category = alias(
                normalized_category,
                field="audit.category",
            )
        normalized_outcome = str(outcome or "").strip().lower()
        if normalized_outcome and normalized_outcome not in {
            "success",
            "denied",
            "failed",
        }:
            raise ValidationError("audit outcome is invalid")
        normalized_actor = str(actor_web_user_id or "").strip()
        if normalized_actor:
            normalized_actor = opaque_id(
                normalized_actor,
                field="audit.actor_web_user_id",
            )
        normalized_conversation = str(conversation_id or "").strip()
        if normalized_conversation:
            normalized_conversation = validate_conversation_id(
                normalized_conversation
            )

        clauses: list[str] = []
        parameters: list[Any] = []
        if normalized_before is not None:
            clauses.append("sequence < ?")
            parameters.append(normalized_before)
        if normalized_hours:
            clauses.append("occurred_at >= ?")
            parameters.append(time.time() - normalized_hours * 60 * 60)
        if normalized_category:
            clauses.append("category = ?")
            parameters.append(normalized_category)
        if normalized_outcome:
            clauses.append("outcome = ?")
            parameters.append(normalized_outcome)
        if normalized_actor:
            clauses.append("actor_web_user_id = ?")
            parameters.append(normalized_actor)
        if normalized_conversation:
            clauses.append("conversation_id = ?")
            parameters.append(normalized_conversation)
        if normalized_query:
            escaped_query = (
                normalized_query.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            clauses.append(
                "(actor_username LIKE ? ESCAPE '\\' COLLATE NOCASE "
                "OR actor_display_name LIKE ? ESCAPE '\\' COLLATE NOCASE "
                "OR action LIKE ? ESCAPE '\\' COLLATE NOCASE "
                "OR COALESCE(conversation_id, '') LIKE ? ESCAPE '\\' COLLATE NOCASE "
                "OR COALESCE(target_id, '') LIKE ? ESCAPE '\\' COLLATE NOCASE "
                "OR request_id LIKE ? ESCAPE '\\' COLLATE NOCASE)"
            )
            needle = f"%{escaped_query}%"
            parameters.extend([needle] * 6)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection() as conn:
            self._require_active_admin_locked(conn, requester)
            rows = conn.execute(
                f"SELECT * FROM admin_audit_events {where_sql} "
                "ORDER BY sequence DESC LIMIT ?",
                (*parameters, normalized_limit + 1),
            ).fetchall()
            facet_rows = conn.execute(
                "SELECT category, COUNT(*) AS count FROM admin_audit_events "
                "GROUP BY category ORDER BY category"
            ).fetchall()
            actor_rows = conn.execute(
                """
                SELECT actor_web_user_id, actor_username, actor_display_name,
                       MAX(sequence) AS latest_sequence, COUNT(*) AS count
                FROM admin_audit_events
                GROUP BY actor_web_user_id, actor_username, actor_display_name
                ORDER BY latest_sequence DESC
                LIMIT 100
                """
            ).fetchall()
            totals = conn.execute(
                """
                SELECT COUNT(*) AS total_count,
                       COALESCE(MAX(sequence), 0) AS latest_sequence,
                       SUM(CASE WHEN outcome = 'success' THEN 1 ELSE 0 END)
                           AS success_count,
                       SUM(CASE WHEN outcome = 'denied' THEN 1 ELSE 0 END)
                           AS denied_count,
                       SUM(CASE WHEN outcome = 'failed' THEN 1 ELSE 0 END)
                           AS failed_count
                FROM admin_audit_events
                """
            ).fetchone()
        has_more = len(rows) > normalized_limit
        page_rows = rows[:normalized_limit]
        events = [self._admin_audit_event_payload(row) for row in page_rows]
        return {
            "events": events,
            "has_more": has_more,
            "next_before_sequence": (
                events[-1]["sequence"] if has_more and events else None
            ),
            "filters": {
                "query": normalized_query,
                "category": normalized_category,
                "outcome": normalized_outcome,
                "actor_web_user_id": normalized_actor,
                "conversation_id": normalized_conversation,
                "hours": normalized_hours,
            },
            "facets": {
                "categories": [
                    {"category": str(row["category"]), "count": int(row["count"])}
                    for row in facet_rows
                ],
                "actors": [
                    {
                        "user_id": str(row["actor_web_user_id"]),
                        "username": str(row["actor_username"]),
                        "display_name": str(row["actor_display_name"]),
                        "count": int(row["count"]),
                    }
                    for row in actor_rows
                ],
            },
            "summary": {
                "total_count": int(totals["total_count"] or 0),
                "latest_sequence": int(totals["latest_sequence"] or 0),
                "success_count": int(totals["success_count"] or 0),
                "denied_count": int(totals["denied_count"] or 0),
                "failed_count": int(totals["failed_count"] or 0),
                "append_only": True,
            },
            "server_time": time.time(),
        }
