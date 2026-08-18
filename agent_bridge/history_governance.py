"""Administrator-controlled history retention, redaction, and export."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from typing import Any

from .validation import (
    ValidationError,
    alias,
    conversation_id as validate_conversation_id,
    opaque_id,
)


HISTORY_REDACTION_PREVIEW_TTL_SECONDS = 10 * 60
HISTORY_REDACTION_MAX_MESSAGES_PER_OPERATION = 5_000
HISTORY_REDACTED_MESSAGE_BODY = "[历史内容已按保留策略清除]"
HISTORY_REDACTED_TASK_BODY = "[相关任务内容已按保留策略清除]"


HISTORY_GOVERNANCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS history_retention_policy (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    mode TEXT NOT NULL DEFAULT 'forever'
        CHECK (mode IN ('forever', 'manual_redaction')),
    retention_days INTEGER NOT NULL DEFAULT 365
        CHECK (retention_days BETWEEN 90 AND 36500),
    abandoned_only INTEGER NOT NULL DEFAULT 1 CHECK (abandoned_only = 1),
    updated_by_web_user_id TEXT,
    updated_at REAL NOT NULL,
    FOREIGN KEY (updated_by_web_user_id) REFERENCES web_users(user_id)
);

INSERT OR IGNORE INTO history_retention_policy (
    singleton, mode, retention_days, abandoned_only,
    updated_by_web_user_id, updated_at
) VALUES (1, 'forever', 365, 1, NULL, CAST(strftime('%s', 'now') AS REAL));

CREATE TABLE IF NOT EXISTS history_redaction_previews (
    preview_id TEXT PRIMARY KEY,
    created_by_web_user_id TEXT NOT NULL,
    conversation_id TEXT,
    cutoff_at REAL NOT NULL,
    maximum_sequence INTEGER NOT NULL,
    eligible_message_count INTEGER NOT NULL CHECK (eligible_message_count > 0),
    reason TEXT NOT NULL,
    confirmation_hash TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    consumed_at REAL,
    FOREIGN KEY (created_by_web_user_id) REFERENCES web_users(user_id),
    FOREIGN KEY (conversation_id) REFERENCES rooms(conversation_id)
);

CREATE INDEX IF NOT EXISTS idx_history_redaction_previews_expiry
    ON history_redaction_previews(expires_at, consumed_at);

CREATE TABLE IF NOT EXISTS history_message_redactions (
    message_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    original_body_sha256 TEXT NOT NULL,
    original_refs_sha256 TEXT NOT NULL,
    original_mentions_sha256 TEXT NOT NULL,
    reason TEXT NOT NULL,
    preview_id TEXT NOT NULL,
    redacted_by_web_user_id TEXT NOT NULL,
    redacted_at REAL NOT NULL,
    FOREIGN KEY (message_id) REFERENCES messages(message_id),
    FOREIGN KEY (conversation_id) REFERENCES rooms(conversation_id),
    FOREIGN KEY (preview_id) REFERENCES history_redaction_previews(preview_id),
    FOREIGN KEY (redacted_by_web_user_id) REFERENCES web_users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_history_message_redactions_room_time
    ON history_message_redactions(conversation_id, redacted_at DESC);

CREATE TRIGGER IF NOT EXISTS trg_history_message_redactions_no_update
BEFORE UPDATE ON history_message_redactions
BEGIN
    SELECT RAISE(ABORT, 'HISTORY_REDACTION_APPEND_ONLY');
END;

CREATE TRIGGER IF NOT EXISTS trg_history_message_redactions_no_delete
BEFORE DELETE ON history_message_redactions
BEGIN
    SELECT RAISE(ABORT, 'HISTORY_REDACTION_APPEND_ONLY');
END;

CREATE INDEX IF NOT EXISTS idx_messages_created_sequence
    ON messages(created_at DESC, sequence DESC);
"""


class HistoryGovernanceMixin:
    @staticmethod
    def _history_policy_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "mode": str(row["mode"]),
            "retention_days": int(row["retention_days"]),
            "abandoned_only": bool(row["abandoned_only"]),
            "automatic_enforcement": False,
            "updated_by_web_user_id": (
                str(row["updated_by_web_user_id"])
                if row["updated_by_web_user_id"] is not None
                else None
            ),
            "updated_by_username": str(row["updated_by_username"] or ""),
            "updated_at": float(row["updated_at"]),
        }
    @staticmethod
    def _history_redaction_candidates_locked(
        conn: sqlite3.Connection,
        *,
        cutoff_at: float,
        maximum_sequence: int | None = None,
        conversation_id: str | None = None,
        limit: int = HISTORY_REDACTION_MAX_MESSAGES_PER_OPERATION,
    ) -> list[sqlite3.Row]:
        clauses = [
            "message.created_at < ?",
            "room.status = 'abandoned'",
            "redaction.message_id IS NULL",
            "message.body != ?",
        ]
        parameters: list[Any] = [cutoff_at, HISTORY_REDACTED_MESSAGE_BODY]
        if maximum_sequence is not None:
            clauses.append("message.sequence <= ?")
            parameters.append(maximum_sequence)
        if conversation_id is not None:
            clauses.append("message.conversation_id = ?")
            parameters.append(conversation_id)
        return conn.execute(
            f"""
            SELECT message.sequence, message.message_id,
                   message.conversation_id, message.body,
                   message.refs_json, message.mentions_json
            FROM messages AS message
            JOIN rooms AS room
              ON room.conversation_id = message.conversation_id
            LEFT JOIN history_message_redactions AS redaction
              ON redaction.message_id = message.message_id
            WHERE {" AND ".join(clauses)}
            ORDER BY message.sequence
            LIMIT ?
            """,
            [
                *parameters,
                max(1, min(int(limit), HISTORY_REDACTION_MAX_MESSAGES_PER_OPERATION)),
            ],
        ).fetchall()

    @staticmethod
    def _history_redaction_candidate_count_locked(
        conn: sqlite3.Connection,
        *,
        cutoff_at: float,
        maximum_sequence: int | None = None,
        conversation_id: str | None = None,
    ) -> int:
        clauses = [
            "message.created_at < ?",
            "room.status = 'abandoned'",
            "redaction.message_id IS NULL",
            "message.body != ?",
        ]
        parameters: list[Any] = [cutoff_at, HISTORY_REDACTED_MESSAGE_BODY]
        if maximum_sequence is not None:
            clauses.append("message.sequence <= ?")
            parameters.append(maximum_sequence)
        if conversation_id is not None:
            clauses.append("message.conversation_id = ?")
            parameters.append(conversation_id)
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM messages AS message
            JOIN rooms AS room
              ON room.conversation_id = message.conversation_id
            LEFT JOIN history_message_redactions AS redaction
              ON redaction.message_id = message.message_id
            WHERE {" AND ".join(clauses)}
            """,
            parameters,
        ).fetchone()
        return int(row["count"] or 0)

    def history_retention_configuration(
        self,
        *,
        requesting_web_user_id: str,
    ) -> dict[str, Any]:
        requester = opaque_id(
            requesting_web_user_id,
            field="requesting_web_user_id",
        )
        now = time.time()
        with self._connection() as conn:
            self._require_active_admin_locked(conn, requester)
            policy = conn.execute(
                """
                SELECT policy.*, updater.username AS updated_by_username
                FROM history_retention_policy AS policy
                LEFT JOIN web_users AS updater
                  ON updater.user_id = policy.updated_by_web_user_id
                WHERE policy.singleton = 1
                """
            ).fetchone()
            room_rows = conn.execute(
                """
                SELECT room.conversation_id, room.status, room.abandoned_at,
                       COUNT(message.message_id) AS message_count,
                       MIN(message.created_at) AS oldest_message_at,
                       MAX(message.created_at) AS newest_message_at,
                       SUM(CASE WHEN redaction.message_id IS NOT NULL
                                THEN 1 ELSE 0 END) AS redacted_count
                FROM rooms AS room
                LEFT JOIN messages AS message
                  ON message.conversation_id = room.conversation_id
                LEFT JOIN history_message_redactions AS redaction
                  ON redaction.message_id = message.message_id
                GROUP BY room.conversation_id
                ORDER BY CASE room.status WHEN 'abandoned' THEN 0 ELSE 1 END,
                         room.last_activity_at DESC
                """
            ).fetchall()
            totals = conn.execute(
                """
                SELECT COUNT(*) AS message_count,
                       (SELECT COUNT(*) FROM history_message_redactions)
                           AS redacted_count
                FROM messages
                """
            ).fetchone()
        policy_payload = self._history_policy_payload(policy)
        cutoff_at = now - policy_payload["retention_days"] * 86400
        eligible_count = 0
        if policy_payload["mode"] == "manual_redaction":
            with self._connection() as conn:
                eligible_count = self._history_redaction_candidate_count_locked(
                    conn,
                    cutoff_at=cutoff_at,
                )
        return {
            "policy": policy_payload,
            "cutoff_at": cutoff_at,
            "eligible_message_count": eligible_count,
            "message_count": int(totals["message_count"] or 0),
            "redacted_message_count": int(totals["redacted_count"] or 0),
            "rooms": [
                {
                    "conversation_id": str(row["conversation_id"]),
                    "status": str(row["status"]),
                    "abandoned_at": (
                        float(row["abandoned_at"])
                        if row["abandoned_at"] is not None
                        else None
                    ),
                    "message_count": int(row["message_count"] or 0),
                    "redacted_count": int(row["redacted_count"] or 0),
                    "oldest_message_at": (
                        float(row["oldest_message_at"])
                        if row["oldest_message_at"] is not None
                        else None
                    ),
                    "newest_message_at": (
                        float(row["newest_message_at"])
                        if row["newest_message_at"] is not None
                        else None
                    ),
                }
                for row in room_rows
            ],
            "redaction_semantics": {
                "deletes_rows": False,
                "removes_message_body": True,
                "removes_refs_and_mentions": True,
                "removes_related_task_content": True,
                "preserves_ids_routes_receipts_and_audit": True,
                "requires_preview_and_typed_confirmation": True,
                "maximum_messages_per_operation": (
                    HISTORY_REDACTION_MAX_MESSAGES_PER_OPERATION
                ),
            },
            "server_time": now,
        }

    def update_history_retention_policy(
        self,
        *,
        updated_by_web_user_id: str,
        mode: object,
        retention_days: object,
    ) -> dict[str, Any]:
        administrator = opaque_id(
            updated_by_web_user_id,
            field="updated_by_web_user_id",
        )
        normalized_mode = str(mode or "").strip().lower()
        if normalized_mode not in {"forever", "manual_redaction"}:
            raise ValidationError(
                "history retention mode must be forever or manual_redaction"
            )
        if isinstance(retention_days, bool):
            raise ValidationError("retention_days must be an integer")
        try:
            normalized_days = int(retention_days)
        except (TypeError, ValueError) as exc:
            raise ValidationError("retention_days must be an integer") from exc
        if not 90 <= normalized_days <= 36500:
            raise ValidationError("retention_days must be between 90 and 36500")
        now = time.time()
        with self._transaction() as conn:
            self._require_active_admin_locked(conn, administrator)
            conn.execute(
                """
                UPDATE history_retention_policy
                SET mode = ?, retention_days = ?, abandoned_only = 1,
                    updated_by_web_user_id = ?, updated_at = ?
                WHERE singleton = 1
                """,
                (normalized_mode, normalized_days, administrator, now),
            )
            row = conn.execute(
                """
                SELECT policy.*, updater.username AS updated_by_username
                FROM history_retention_policy AS policy
                LEFT JOIN web_users AS updater
                  ON updater.user_id = policy.updated_by_web_user_id
                WHERE policy.singleton = 1
                """
            ).fetchone()
        return self._history_policy_payload(row)

    def preview_history_redaction(
        self,
        *,
        created_by_web_user_id: str,
        reason: object,
        conversation_id: object = "",
    ) -> dict[str, Any]:
        administrator = opaque_id(
            created_by_web_user_id,
            field="created_by_web_user_id",
        )
        normalized_reason = alias(str(reason or ""), field="redaction.reason")
        normalized_conversation = str(conversation_id or "").strip()
        if normalized_conversation:
            normalized_conversation = validate_conversation_id(normalized_conversation)
        else:
            normalized_conversation = None
        now = time.time()
        with self._transaction() as conn:
            self._require_active_admin_locked(conn, administrator)
            policy = conn.execute(
                "SELECT * FROM history_retention_policy WHERE singleton = 1"
            ).fetchone()
            if str(policy["mode"]) != "manual_redaction":
                raise self._history_conflict_error(
                    "history retention is set to forever; enable manual redaction first"
                )
            if normalized_conversation is not None:
                room = conn.execute(
                    "SELECT status FROM rooms WHERE conversation_id = ?",
                    (normalized_conversation,),
                ).fetchone()
                if room is None:
                    raise self._history_not_found_error("conversation was not found")
                if str(room["status"]) != "abandoned":
                    raise self._history_conflict_error(
                        "history redaction is limited to abandoned rooms"
                    )
            cutoff_at = now - int(policy["retention_days"]) * 86400
            total_candidate_count = self._history_redaction_candidate_count_locked(
                conn,
                cutoff_at=cutoff_at,
                conversation_id=normalized_conversation,
            )
            candidates = self._history_redaction_candidates_locked(
                conn,
                cutoff_at=cutoff_at,
                conversation_id=normalized_conversation,
            )
            if not candidates:
                raise self._history_conflict_error("no messages currently match the retention policy")
            maximum_sequence = max(int(row["sequence"]) for row in candidates)
            preview_id = f"history_preview_{uuid.uuid4().hex}"
            confirmation_phrase = (
                f"REDACT-{len(candidates)}-{secrets.token_hex(3).upper()}"
            )
            conn.execute(
                "DELETE FROM history_redaction_previews "
                "WHERE consumed_at IS NULL AND expires_at <= ?",
                (now,),
            )
            conn.execute(
                """
                INSERT INTO history_redaction_previews (
                    preview_id, created_by_web_user_id, conversation_id,
                    cutoff_at, maximum_sequence, eligible_message_count,
                    reason, confirmation_hash, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    preview_id,
                    administrator,
                    normalized_conversation,
                    cutoff_at,
                    maximum_sequence,
                    len(candidates),
                    normalized_reason,
                    hashlib.sha256(confirmation_phrase.encode("utf-8")).hexdigest(),
                    now,
                    now + HISTORY_REDACTION_PREVIEW_TTL_SECONDS,
                ),
            )
            room_counts = conn.execute(
                """
                SELECT message.conversation_id, COUNT(*) AS count
                FROM messages AS message
                JOIN rooms AS room
                  ON room.conversation_id = message.conversation_id
                LEFT JOIN history_message_redactions AS redaction
                  ON redaction.message_id = message.message_id
                WHERE message.created_at < ?
                  AND message.sequence <= ?
                  AND room.status = 'abandoned'
                  AND redaction.message_id IS NULL
                  AND message.body != ?
                  AND (? IS NULL OR message.conversation_id = ?)
                GROUP BY message.conversation_id
                ORDER BY message.conversation_id
                """,
                (
                    cutoff_at,
                    maximum_sequence,
                    HISTORY_REDACTED_MESSAGE_BODY,
                    normalized_conversation,
                    normalized_conversation,
                ),
            ).fetchall()
        return {
            "preview_id": preview_id,
            "conversation_id": normalized_conversation,
            "cutoff_at": cutoff_at,
            "maximum_sequence": maximum_sequence,
            "eligible_message_count": len(candidates),
            "room_counts": [
                {
                    "conversation_id": str(row["conversation_id"]),
                    "message_count": int(row["count"]),
                }
                for row in room_counts
            ],
            "reason": normalized_reason,
            "confirmation_phrase": confirmation_phrase,
            "expires_at": now + HISTORY_REDACTION_PREVIEW_TTL_SECONDS,
            "changes_data": False,
            "total_eligible_message_count": total_candidate_count,
            "more_eligible_messages_may_remain": total_candidate_count
            > len(candidates),
        }

    def execute_history_redaction(
        self,
        *,
        executed_by_web_user_id: str,
        preview_id: str,
        confirmation_phrase: object,
    ) -> dict[str, Any]:
        administrator = opaque_id(
            executed_by_web_user_id,
            field="executed_by_web_user_id",
        )
        normalized_preview = opaque_id(preview_id, field="preview_id")
        phrase = str(confirmation_phrase or "").strip()
        if not phrase:
            raise ValidationError("confirmation_phrase is required")
        now = time.time()
        with self._transaction() as conn:
            self._require_active_admin_locked(conn, administrator)
            preview = conn.execute(
                "SELECT * FROM history_redaction_previews WHERE preview_id = ?",
                (normalized_preview,),
            ).fetchone()
            if preview is None:
                raise self._history_not_found_error("history redaction preview was not found")
            if str(preview["created_by_web_user_id"]) != administrator:
                raise self._history_authorization_error(
                    "history redaction must be confirmed by its preview creator"
                )
            if preview["consumed_at"] is not None:
                raise self._history_conflict_error("history redaction preview was already used")
            if float(preview["expires_at"]) <= now:
                raise self._history_conflict_error("history redaction preview has expired")
            expected_hash = str(preview["confirmation_hash"])
            actual_hash = hashlib.sha256(phrase.encode("utf-8")).hexdigest()
            if not secrets.compare_digest(actual_hash, expected_hash):
                raise ValidationError("history redaction confirmation is incorrect")
            conversation = (
                str(preview["conversation_id"])
                if preview["conversation_id"] is not None
                else None
            )
            candidates = self._history_redaction_candidates_locked(
                conn,
                cutoff_at=float(preview["cutoff_at"]),
                maximum_sequence=int(preview["maximum_sequence"]),
                conversation_id=conversation,
            )
            expected_count = int(preview["eligible_message_count"])
            if len(candidates) != expected_count:
                raise self._history_conflict_error(
                    "history changed after preview; create a new preview before redacting"
                )
            conn.execute(
                "CREATE TEMP TABLE history_redaction_targets "
                "(message_id TEXT PRIMARY KEY)"
            )
            conn.executemany(
                "INSERT INTO history_redaction_targets(message_id) VALUES (?)",
                ((str(row["message_id"]),) for row in candidates),
            )
            conn.executemany(
                """
                INSERT INTO history_message_redactions (
                    message_id, conversation_id, original_body_sha256,
                    original_refs_sha256, original_mentions_sha256, reason,
                    preview_id, redacted_by_web_user_id, redacted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        str(row["message_id"]),
                        str(row["conversation_id"]),
                        hashlib.sha256(str(row["body"]).encode("utf-8")).hexdigest(),
                        hashlib.sha256(
                            str(row["refs_json"]).encode("utf-8")
                        ).hexdigest(),
                        hashlib.sha256(
                            str(row["mentions_json"]).encode("utf-8")
                        ).hexdigest(),
                        str(preview["reason"]),
                        normalized_preview,
                        administrator,
                        now,
                    )
                    for row in candidates
                ),
            )
            message_count = conn.execute(
                """
                UPDATE messages
                SET body = ?, refs_json = '[]', mentions_json = '[]',
                    updated_at = ?
                WHERE message_id IN (
                    SELECT message_id FROM history_redaction_targets
                )
                """,
                (HISTORY_REDACTED_MESSAGE_BODY, now),
            ).rowcount
            task_count = conn.execute(
                """
                UPDATE room_tasks
                SET body = ?,
                    result_summary = CASE WHEN result_summary IS NULL
                        THEN NULL ELSE ? END,
                    execution_cwd = NULL, execution_thread_id = NULL,
                    updated_at = ?
                WHERE source_message_id IN (
                    SELECT message_id FROM history_redaction_targets
                )
                """,
                (HISTORY_REDACTED_TASK_BODY, HISTORY_REDACTED_TASK_BODY, now),
            ).rowcount
            task_input_count = conn.execute(
                """
                UPDATE room_task_inputs
                SET body = ?
                WHERE source_message_id IN (
                    SELECT message_id FROM history_redaction_targets
                )
                """,
                (HISTORY_REDACTED_TASK_BODY,),
            ).rowcount
            marker_count = conn.execute(
                """
                UPDATE room_message_markers
                SET note = ?, updated_by_web_user_id = ?, updated_at = ?
                WHERE message_id IN (
                    SELECT message_id FROM history_redaction_targets
                )
                """,
                (HISTORY_REDACTED_TASK_BODY, administrator, now),
            ).rowcount
            conn.execute(
                "UPDATE history_redaction_previews SET consumed_at = ? "
                "WHERE preview_id = ?",
                (now, normalized_preview),
            )
        return {
            "preview_id": normalized_preview,
            "redacted_message_count": int(message_count),
            "redacted_task_count": int(task_count),
            "redacted_task_input_count": int(task_input_count),
            "redacted_marker_count": int(marker_count),
            "redacted_at": now,
            "mode": "content_redaction",
            "rows_deleted": False,
            "identifiers_and_audit_preserved": True,
        }

    def export_room_history(
        self,
        *,
        requesting_web_user_id: str,
        conversation_id: str,
    ) -> dict[str, Any]:
        requester = opaque_id(
            requesting_web_user_id,
            field="requesting_web_user_id",
        )
        conversation = validate_conversation_id(conversation_id)

        def parse_json(value: object, fallback: Any) -> Any:
            try:
                return json.loads(str(value))
            except (TypeError, ValueError, json.JSONDecodeError):
                return fallback

        with self._connection() as conn:
            self._require_active_admin_locked(conn, requester)
            room = conn.execute(
                "SELECT * FROM rooms WHERE conversation_id = ?",
                (conversation,),
            ).fetchone()
            if room is None:
                raise self._history_not_found_error("conversation was not found")
            member_rows = conn.execute(
                """
                SELECT membership.*, participant.client_type,
                       participant.display_name, participant.signature,
                       participant.avatar_key, participant.capabilities_json,
                       participant.created_at AS participant_created_at
                FROM memberships AS membership
                JOIN participants AS participant
                  ON participant.participant_id = membership.participant_id
                WHERE membership.conversation_id = ?
                ORDER BY membership.joined_at, membership.participant_id
                """,
                (conversation,),
            ).fetchall()
            web_member_rows = conn.execute(
                """
                SELECT member.*, user.username, user.display_name, user.role
                FROM room_web_members AS member
                JOIN web_users AS user ON user.user_id = member.web_user_id
                WHERE member.conversation_id = ?
                ORDER BY member.created_at, member.web_user_id
                """,
                (conversation,),
            ).fetchall()
            message_rows = conn.execute(
                """
                SELECT message.*,
                       sender.client_type AS sender_client_type,
                       sender.display_name AS sender_display_name,
                       sender.avatar_key AS sender_avatar_key,
                       (SELECT COUNT(*) FROM receipts AS receipt
                        WHERE receipt.message_id = message.message_id
                          AND receipt.state = 'acked') AS ack_count,
                       (SELECT COUNT(*) FROM message_deliveries AS delivery
                        WHERE delivery.message_id = message.message_id)
                           AS delivery_count,
                       redaction.redacted_at, redaction.reason AS redaction_reason
                FROM messages AS message
                JOIN participants AS sender
                  ON sender.participant_id = message.sender_participant_id
                LEFT JOIN history_message_redactions AS redaction
                  ON redaction.message_id = message.message_id
                WHERE message.conversation_id = ?
                ORDER BY message.sequence
                """,
                (conversation,),
            ).fetchall()
            marker_rows = conn.execute(
                "SELECT * FROM room_message_markers WHERE conversation_id = ? "
                "ORDER BY updated_at, message_id, marker_kind",
                (conversation,),
            ).fetchall()
            task_rows = conn.execute(
                "SELECT * FROM room_tasks WHERE conversation_id = ? "
                "ORDER BY created_at, task_id",
                (conversation,),
            ).fetchall()
            schema_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        exported_at = time.time()
        return {
            "export_format": "agent-bridge-room-history",
            "export_format_version": 1,
            "bridge_schema_version": schema_version,
            "exported_at": exported_at,
            "conversation": {
                "conversation_id": conversation,
                "status": str(room["status"]),
                "creator_kind": str(room["creator_kind"]),
                "creator_participant_id": (
                    str(room["creator_participant_id"])
                    if room["creator_participant_id"] is not None
                    else None
                ),
                "created_at": float(room["created_at"]),
                "last_activity_at": float(room["last_activity_at"]),
                "abandoned_at": (
                    float(room["abandoned_at"])
                    if room["abandoned_at"] is not None
                    else None
                ),
            },
            "members": [
                {
                    "participant_id": str(row["participant_id"]),
                    "client_type": str(row["client_type"]),
                    "display_name": str(row["display_name"]),
                    "signature": str(row["signature"]),
                    "avatar_key": str(row["avatar_key"] or "auto"),
                    "capabilities": parse_json(row["capabilities_json"], []),
                    "roles": parse_json(row["roles_json"], []),
                    "active": bool(row["active"]),
                    "joined_at": float(row["joined_at"]),
                    "updated_at": float(row["updated_at"]),
                    "participant_created_at": float(row["participant_created_at"]),
                }
                for row in member_rows
            ],
            "web_members": [
                {
                    "user_id": str(row["web_user_id"]),
                    "username": str(row["username"]),
                    "display_name": str(row["display_name"]),
                    "role": str(row["role"]),
                    "access_role": str(row["access_role"]),
                    "active": bool(row["active"]),
                    "created_at": float(row["created_at"]),
                    "updated_at": float(row["updated_at"]),
                }
                for row in web_member_rows
            ],
            "messages": [
                {
                    "sequence": int(row["sequence"]),
                    "room_sequence": int(row["room_sequence"] or row["sequence"]),
                    "message_id": str(row["message_id"]),
                    "sender_participant_id": str(row["sender_participant_id"]),
                    "sender_client_type": str(row["sender_client_type"]),
                    "sender_display_name": str(row["sender_display_name"]),
                    "sender_avatar_key": str(row["sender_avatar_key"] or "auto"),
                    "audience_kind": str(row["audience_kind"]),
                    "audience_value": str(row["audience_value"]),
                    "message_kind": str(row["message_kind"]),
                    "notification_mode": str(row["notification_mode"]),
                    "sender_seat": str(row["sender_seat"]),
                    "body": str(row["body"]),
                    "refs": parse_json(row["refs_json"], []),
                    "mentions": parse_json(row["mentions_json"], []),
                    "wake_all_agents": bool(row["wake_all_agents"]),
                    "reply_to": str(row["reply_to"]) if row["reply_to"] else None,
                    "forwarded_from_message_id": (
                        str(row["forwarded_from_message_id"])
                        if row["forwarded_from_message_id"]
                        else None
                    ),
                    "status": str(row["status"]),
                    "ack_count": int(row["ack_count"] or 0),
                    "delivery_count": int(row["delivery_count"] or 0),
                    "content_redacted": row["redacted_at"] is not None,
                    "redacted_at": (
                        float(row["redacted_at"])
                        if row["redacted_at"] is not None
                        else None
                    ),
                    "redaction_reason": str(row["redaction_reason"] or ""),
                    "created_at": float(row["created_at"]),
                    "updated_at": float(row["updated_at"]),
                }
                for row in message_rows
            ],
            "markers": [dict(row) for row in marker_rows],
            "tasks": [
                {
                    **dict(row),
                    "target_participant_ids": parse_json(
                        row["target_participant_ids_json"],
                        [],
                    ),
                }
                for row in task_rows
            ],
            "counts": {
                "members": len(member_rows),
                "web_members": len(web_member_rows),
                "messages": len(message_rows),
                "markers": len(marker_rows),
                "tasks": len(task_rows),
            },
            "sensitive_fields_omitted": [
                "session tokens",
                "connector credentials",
                "passwords",
                "cookies",
                "authorization headers",
            ],
        }
