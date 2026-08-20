from __future__ import annotations

import json
import sqlite3
from typing import Any

from .message_assets import MessageAssetMixin
from .validation import conversation_id as validate_conversation_id
from .viewer_delivery_projection import ViewerDeliveryProjectionMixin


class ViewerMessageQueries(ViewerDeliveryProjectionMixin, MessageAssetMixin):
    """Read-only message, thread, search, and receipt projections."""

    def messages(
        self,
        conversation_id: str,
        *,
        limit: int = 300,
        before_sequence: int | None = None,
        after_sequence: int | None = None,
        around_sequence: int | None = None,
    ) -> list[dict[str, Any]]:
        conversation = validate_conversation_id(conversation_id)
        normalized_limit = max(1, min(int(limit), 501))
        supplied_cursors = sum(
            value is not None
            for value in (before_sequence, after_sequence, around_sequence)
        )
        if supplied_cursors > 1:
            raise ValueError(
                "before_sequence, after_sequence, and around_sequence cannot "
                "be used together"
            )
        parameters: list[Any] = [conversation]
        sequence_clause = ""
        order = "DESC"
        if before_sequence is not None:
            sequence_clause = "AND m.sequence < ?"
            parameters.append(int(before_sequence))
        elif after_sequence is not None:
            sequence_clause = "AND m.sequence > ?"
            parameters.append(int(after_sequence))
            order = "ASC"
        elif around_sequence is not None:
            sequence_clause = """
                AND m.message_id IN (
                    SELECT candidate.message_id
                    FROM messages AS candidate
                    WHERE candidate.conversation_id = ?
                    ORDER BY ABS(candidate.sequence - ?), candidate.sequence DESC
                    LIMIT ?
                )
            """
            parameters.extend(
                (conversation, max(0, int(around_sequence)), normalized_limit)
            )
            order = "ASC"
        parameters.append(normalized_limit)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    m.*,
                    sender.session_alias AS sender_alias,
                    sender.client_type AS sender_client_type,
                    sender.display_name AS sender_display_name,
                    sender.signature AS sender_signature,
                    sender.avatar_key AS sender_avatar_key,
                    claimant.session_alias AS claimant_alias,
                    claimant.display_name AS claimant_display_name,
                    source.conversation_id AS forwarded_source_conversation_id,
                    source.sequence AS forwarded_source_sequence,
                    source.room_sequence AS forwarded_source_room_sequence,
                    source_sender.display_name AS forwarded_source_sender_display_name,
                    source_sender.client_type AS forwarded_source_sender_client_type,
                    grant.authority_kind AS authorization_kind,
                    grant.issuer_web_user_id AS authorization_issuer_user_id,
                    grant.issuer_username_snapshot
                        AS authorization_issuer_username,
                    grant.issuer_role_snapshot AS authorization_issuer_role,
                    grant.issuer_participant_id
                        AS authorization_issuer_participant_id,
                    grant.body_sha256 AS authorization_body_sha256,
                    grant.target_kind AS authorization_target_kind,
                    grant.target_participant_ids_json
                        AS authorization_target_participant_ids_json,
                    grant.created_at AS authorization_issued_at,
                    grant.revoked_at AS authorization_revoked_at,
                    grant.revoked_by_web_user_id
                        AS authorization_revoked_by_web_user_id,
                    grant.revocation_reason AS authorization_revocation_reason,
                    task.task_id AS room_task_id,
                    task.parent_task_id AS room_task_parent_id,
                    task.issuer_web_user_id AS room_task_issuer_web_user_id,
                    task.target_kind AS room_task_target_kind,
                    task.target_participant_ids_json
                        AS room_task_target_participant_ids_json,
                    task.status AS room_task_status,
                    task.claimed_by_participant_id
                        AS room_task_claimed_by_participant_id,
                    task.claimed_at AS room_task_claimed_at,
                    task.lease_expires_at AS room_task_lease_expires_at,
                    task.started_at AS room_task_started_at,
                    task.completed_at AS room_task_completed_at,
                    task.result_summary AS room_task_result_summary,
                    task.execution_cwd AS room_task_execution_cwd,
                    task.execution_thread_id AS room_task_execution_thread_id,
                    task.created_at AS room_task_created_at,
                    task.updated_at AS room_task_updated_at,
                    (
                        SELECT COUNT(*) FROM room_task_inputs AS task_input
                        WHERE task_input.source_message_id = m.message_id
                    ) AS body_input_count,
                    (
                        SELECT COUNT(*) FROM room_task_inputs AS task_input
                        WHERE task_input.source_message_id = m.message_id
                          AND task_input.first_delivered_at IS NOT NULL
                    ) AS body_input_delivered_count,
                    (
                        SELECT COUNT(*) FROM room_task_inputs AS task_input
                        WHERE task_input.source_message_id = m.message_id
                          AND task_input.applied_at IS NOT NULL
                    ) AS body_input_applied_count,
                    (
                        SELECT MAX(task_input.last_delivered_at)
                        FROM room_task_inputs AS task_input
                        WHERE task_input.source_message_id = m.message_id
                    ) AS body_input_last_delivered_at,
                    (
                        SELECT MAX(task_input.applied_at)
                        FROM room_task_inputs AS task_input
                        WHERE task_input.source_message_id = m.message_id
                    ) AS body_input_last_applied_at,
                    (
                        SELECT COUNT(*) FROM messages AS reply
                        WHERE reply.reply_to = m.message_id
                    ) AS reply_count,
                    0 AS ack_count,
                    0 AS receipt_count
                FROM messages AS m
                JOIN participants AS sender
                  ON sender.participant_id = m.sender_participant_id
                LEFT JOIN participants AS claimant
                  ON claimant.participant_id = m.claimed_by
                LEFT JOIN messages AS source
                  ON source.message_id = m.forwarded_from_message_id
                LEFT JOIN participants AS source_sender
                  ON source_sender.participant_id = source.sender_participant_id
                LEFT JOIN chat_authorization_grants AS grant
                  ON grant.source_message_id = m.message_id
                LEFT JOIN room_tasks AS task
                  ON task.source_message_id = m.message_id
                WHERE m.conversation_id = ? {sequence_clause}
                ORDER BY m.sequence {order}
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            projections = self._message_asset_projection_locked(
                connection,
                [str(row["message_id"]) for row in rows],
            )
            delivery_projections = self._message_delivery_projection_locked(
                connection,
                [str(row["message_id"]) for row in rows],
            )
        ordered_rows = (
            rows
            if after_sequence is not None or around_sequence is not None
            else reversed(rows)
        )
        result = []
        for row in ordered_rows:
            payload = self._message_payload(row)
            payload.update(projections[str(row["message_id"])])
            payload.update(delivery_projections[str(row["message_id"])])
            result.append(payload)
        return result

    def message_thread(
        self,
        conversation_id: str,
        message_id: str,
        *,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Return one root message plus direct replies in room order."""

        conversation = validate_conversation_id(conversation_id)
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id or len(normalized_message_id) > 200:
            raise ValueError("message_id is invalid")
        normalized_limit = max(2, min(int(limit), 500))
        with self._connection() as connection:
            selected = connection.execute(
                "SELECT message_id, reply_to FROM messages "
                "WHERE conversation_id = ? AND message_id = ?",
                (conversation, normalized_message_id),
            ).fetchone()
            if selected is None:
                raise ValueError("message does not exist in this room")
            root_message_id = str(selected["reply_to"] or selected["message_id"])
            total_reply_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM messages "
                    "WHERE conversation_id = ? AND reply_to = ?",
                    (conversation, root_message_id),
                ).fetchone()[0]
            )
            rows = connection.execute(
                """
                SELECT message.sequence, message.room_sequence,
                       message.message_id, message.reply_to,
                       message.sender_participant_id, message.sender_seat,
                       message.message_kind, message.body, message.created_at,
                       sender.client_type AS sender_client_type,
                       sender.display_name AS sender_display_name,
                       sender.signature AS sender_signature,
                       sender.avatar_key AS sender_avatar_key
                FROM messages AS message
                JOIN participants AS sender
                  ON sender.participant_id = message.sender_participant_id
                WHERE message.conversation_id = ?
                  AND (message.message_id = ? OR message.reply_to = ?)
                ORDER BY message.sequence
                LIMIT ?
                """,
                (
                    conversation,
                    root_message_id,
                    root_message_id,
                    normalized_limit + 1,
                ),
            ).fetchall()
            projections = self._message_asset_projection_locked(
                connection,
                [str(row["message_id"]) for row in rows],
            )
        has_more = total_reply_count + 1 > normalized_limit
        page = rows[:normalized_limit]
        messages = []
        for row in page:
            payload = self._thread_message_payload(row)
            payload.update(projections[str(row["message_id"])])
            messages.append(payload)
        return {
            "conversation_id": conversation,
            "root_message_id": root_message_id,
            "messages": messages,
            "reply_count": total_reply_count,
            "has_more": has_more,
        }

    def room_highlights(
        self,
        conversation_id: str,
        *,
        limit: int = 200,
    ) -> dict[str, Any]:
        conversation = validate_conversation_id(conversation_id)
        normalized_limit = max(1, min(int(limit), 500))
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT marker.*, message.sequence, message.room_sequence,
                       message.body, message.message_kind,
                       message.sender_participant_id,
                       message.created_at AS message_created_at,
                       sender.client_type AS sender_client_type,
                       sender.display_name AS sender_display_name,
                       sender.avatar_key AS sender_avatar_key,
                       creator.username AS created_by_username,
                       creator.display_name AS created_by_display_name,
                       updater.username AS updated_by_username,
                       updater.display_name AS updated_by_display_name
                FROM room_message_markers AS marker
                JOIN messages AS message
                  ON message.message_id = marker.message_id
                 AND message.conversation_id = marker.conversation_id
                JOIN participants AS sender
                  ON sender.participant_id = message.sender_participant_id
                JOIN web_users AS creator
                  ON creator.user_id = marker.created_by_web_user_id
                JOIN web_users AS updater
                  ON updater.user_id = marker.updated_by_web_user_id
                WHERE marker.conversation_id = ?
                ORDER BY CASE marker.marker_kind
                             WHEN 'decision' THEN 0 ELSE 1
                         END,
                         marker.updated_at DESC,
                         message.sequence DESC
                LIMIT ?
                """,
                (conversation, normalized_limit),
            ).fetchall()
        items = [self._room_highlight_payload(row) for row in rows]
        return {
            "conversation_id": conversation,
            "items": items,
            "pins": [item for item in items if item["marker_kind"] == "pin"],
            "decisions": [item for item in items if item["marker_kind"] == "decision"],
            "count": len(items),
        }

    @staticmethod
    def _thread_message_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "sequence": int(row["sequence"]),
            "room_sequence": int(row["room_sequence"] or row["sequence"]),
            "message_id": str(row["message_id"]),
            "reply_to": str(row["reply_to"]) if row["reply_to"] else None,
            "sender_participant_id": str(row["sender_participant_id"]),
            "sender_seat": str(row["sender_seat"] or "unknown"),
            "sender_client_type": str(row["sender_client_type"]),
            "sender_display_name": str(row["sender_display_name"]),
            "sender_signature": str(row["sender_signature"]),
            "sender_avatar_key": str(row["sender_avatar_key"] or "auto"),
            "message_kind": str(row["message_kind"] or "message"),
            "body": str(row["body"]),
            "created_at": float(row["created_at"]),
        }

    @staticmethod
    def _room_highlight_payload(row: sqlite3.Row) -> dict[str, Any]:
        body = str(row["body"])
        return {
            "conversation_id": str(row["conversation_id"]),
            "message_id": str(row["message_id"]),
            "sequence": int(row["sequence"]),
            "room_sequence": int(row["room_sequence"] or row["sequence"]),
            "marker_kind": str(row["marker_kind"]),
            "note": str(row["note"] or ""),
            "message_kind": str(row["message_kind"] or "message"),
            "body_preview": body[:1_000],
            "body_truncated": len(body) > 1_000,
            "sender_participant_id": str(row["sender_participant_id"]),
            "sender_client_type": str(row["sender_client_type"]),
            "sender_display_name": str(row["sender_display_name"]),
            "sender_avatar_key": str(row["sender_avatar_key"] or "auto"),
            "message_created_at": float(row["message_created_at"]),
            "created_by_web_user_id": str(row["created_by_web_user_id"]),
            "created_by_username": str(row["created_by_username"]),
            "created_by_display_name": str(row["created_by_display_name"]),
            "updated_by_web_user_id": str(row["updated_by_web_user_id"]),
            "updated_by_username": str(row["updated_by_username"]),
            "updated_by_display_name": str(row["updated_by_display_name"]),
            "marker_created_at": float(row["created_at"]),
            "marker_updated_at": float(row["updated_at"]),
        }

    def message_window_bounds(
        self,
        conversation_id: str,
        *,
        first_sequence: int | None,
        last_sequence: int | None,
    ) -> dict[str, bool]:
        conversation = validate_conversation_id(conversation_id)
        if first_sequence is None or last_sequence is None:
            return {"has_earlier": False, "has_later": False}
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT
                    EXISTS(
                        SELECT 1 FROM messages
                        WHERE conversation_id = ? AND sequence < ?
                    ) AS has_earlier,
                    EXISTS(
                        SELECT 1 FROM messages
                        WHERE conversation_id = ? AND sequence > ?
                    ) AS has_later
                """,
                (
                    conversation,
                    int(first_sequence),
                    conversation,
                    int(last_sequence),
                ),
            ).fetchone()
        return {
            "has_earlier": bool(row["has_earlier"]),
            "has_later": bool(row["has_later"]),
        }

    def search_messages(
        self,
        conversation_id: str,
        *,
        query: str = "",
        sender_participant_id: str | None = None,
        message_kind: str | None = None,
        notification_mode: str | None = None,
        thread_scope: str | None = None,
        marker_kind: str | None = None,
        room_sequence: int | None = None,
        created_after: float | None = None,
        created_before: float | None = None,
        before_sequence: int | None = None,
        limit: int = 30,
    ) -> dict[str, Any]:
        """Search one room with composable, server-validated filters."""

        conversation = validate_conversation_id(conversation_id)
        normalized_query = str(query or "").strip()
        normalized_sender = str(sender_participant_id or "").strip() or None
        normalized_message_kind = str(message_kind or "").strip().casefold() or None
        normalized_notification_mode = (
            str(notification_mode or "").strip().casefold() or None
        )
        normalized_thread_scope = str(thread_scope or "").strip().casefold() or None
        normalized_marker_kind = str(marker_kind or "").strip().casefold() or None
        normalized_room_sequence = (
            int(room_sequence) if room_sequence is not None else None
        )
        normalized_created_after = (
            float(created_after) if created_after is not None else None
        )
        normalized_created_before = (
            float(created_before) if created_before is not None else None
        )
        if not any(
            (
                normalized_query,
                normalized_sender,
                normalized_message_kind,
                normalized_notification_mode,
                normalized_thread_scope,
                normalized_marker_kind,
                normalized_room_sequence,
                normalized_created_after is not None,
                normalized_created_before is not None,
            )
        ):
            raise ValueError("at least one room search filter is required")
        if len(normalized_query) > 200:
            raise ValueError("query must be at most 200 characters")
        if normalized_sender is not None and len(normalized_sender) > 200:
            raise ValueError("sender_participant_id is invalid")
        if normalized_message_kind not in {None, "message", "task", "forward"}:
            raise ValueError("message_kind must be message, task, or forward")
        if normalized_notification_mode not in {None, "ordinary", "mention"}:
            raise ValueError("notification_mode must be ordinary or mention")
        if normalized_thread_scope not in {None, "roots", "replies"}:
            raise ValueError("thread_scope must be roots or replies")
        if normalized_marker_kind not in {None, "pin", "decision"}:
            raise ValueError("marker_kind must be pin or decision")
        if normalized_room_sequence is not None and normalized_room_sequence < 1:
            raise ValueError("room_sequence must be a positive integer")
        maximum_timestamp = 253_402_300_799.0
        for label, timestamp in (
            ("created_after", normalized_created_after),
            ("created_before", normalized_created_before),
        ):
            if timestamp is not None and not 0 <= timestamp <= maximum_timestamp:
                raise ValueError(f"{label} is outside the supported range")
        if (
            normalized_created_after is not None
            and normalized_created_before is not None
            and normalized_created_after >= normalized_created_before
        ):
            raise ValueError("created_after must be earlier than created_before")
        normalized_limit = max(1, min(int(limit), 50))
        clauses = ["message.conversation_id = ?"]
        parameters: list[Any] = [conversation]
        if normalized_query:
            clauses.append(
                "(instr(lower(message.body), lower(?)) > 0 OR EXISTS ("
                "SELECT 1 FROM message_links AS searched_link "
                "WHERE searched_link.message_id = message.message_id "
                "AND instr(lower(searched_link.url), lower(?)) > 0))"
            )
            parameters.extend((normalized_query, normalized_query))
        if normalized_sender is not None:
            clauses.append("message.sender_participant_id = ?")
            parameters.append(normalized_sender)
        if normalized_message_kind is not None:
            clauses.append("message.message_kind = ?")
            parameters.append(normalized_message_kind)
        if normalized_notification_mode is not None:
            clauses.append("message.notification_mode = ?")
            parameters.append(normalized_notification_mode)
        if normalized_thread_scope == "roots":
            clauses.append("message.reply_to IS NULL")
        elif normalized_thread_scope == "replies":
            clauses.append("message.reply_to IS NOT NULL")
        if normalized_marker_kind is not None:
            clauses.append(
                "EXISTS (SELECT 1 FROM room_message_markers AS marker_filter "
                "WHERE marker_filter.conversation_id = message.conversation_id "
                "AND marker_filter.message_id = message.message_id "
                "AND marker_filter.marker_kind = ?)"
            )
            parameters.append(normalized_marker_kind)
        if normalized_room_sequence is not None:
            clauses.append("message.room_sequence = ?")
            parameters.append(normalized_room_sequence)
        if normalized_created_after is not None:
            clauses.append("message.created_at >= ?")
            parameters.append(normalized_created_after)
        if normalized_created_before is not None:
            clauses.append("message.created_at < ?")
            parameters.append(normalized_created_before)
        if before_sequence is not None:
            clauses.append("message.sequence < ?")
            parameters.append(max(0, int(before_sequence)))
        parameters.append(normalized_limit + 1)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT message.sequence, message.room_sequence,
                       message.message_id,
                       message.sender_participant_id, message.message_kind,
                       message.notification_mode, message.reply_to,
                       message.body, message.created_at,
                       sender.client_type AS sender_client_type,
                       sender.display_name AS sender_display_name,
                       sender.avatar_key AS sender_avatar_key,
                       (
                           SELECT GROUP_CONCAT(marker.marker_kind)
                           FROM room_message_markers AS marker
                           WHERE marker.conversation_id = message.conversation_id
                             AND marker.message_id = message.message_id
                       ) AS marker_kinds
                FROM messages AS message
                JOIN participants AS sender
                  ON sender.participant_id = message.sender_participant_id
                WHERE {" AND ".join(clauses)}
                ORDER BY message.sequence DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        has_more = len(rows) > normalized_limit
        page = rows[:normalized_limit]
        results = []
        for row in page:
            body = str(row["body"])
            results.append(
                {
                    "sequence": int(row["sequence"]),
                    "room_sequence": int(row["room_sequence"] or row["sequence"]),
                    "message_id": str(row["message_id"]),
                    "sender_participant_id": str(row["sender_participant_id"]),
                    "sender_client_type": str(row["sender_client_type"]),
                    "sender_display_name": str(row["sender_display_name"]),
                    "sender_avatar_key": str(row["sender_avatar_key"] or "auto"),
                    "message_kind": str(row["message_kind"] or "message"),
                    "notification_mode": str(row["notification_mode"] or "ordinary"),
                    "reply_to": str(row["reply_to"]) if row["reply_to"] else None,
                    "marker_kinds": sorted(
                        value
                        for value in str(row["marker_kinds"] or "").split(",")
                        if value
                    ),
                    "body_preview": body[:500],
                    "body_truncated": len(body) > 500,
                    "created_at": float(row["created_at"]),
                }
            )
        return {
            "conversation_id": conversation,
            "query": normalized_query,
            "sender_participant_id": normalized_sender,
            "filters": {
                "message_kind": normalized_message_kind,
                "notification_mode": normalized_notification_mode,
                "thread_scope": normalized_thread_scope,
                "marker_kind": normalized_marker_kind,
                "room_sequence": normalized_room_sequence,
                "created_after": normalized_created_after,
                "created_before": normalized_created_before,
            },
            "results": results,
            "count": len(results),
            "has_more": has_more,
            "next_before_sequence": (
                int(page[-1]["sequence"]) if has_more and page else None
            ),
        }

    def search_messages_globally(
        self,
        *,
        query: str = "",
        conversation_id: str | None = None,
        sender_query: str = "",
        message_kind: str | None = None,
        notification_mode: str | None = None,
        created_after: float | None = None,
        created_before: float | None = None,
        before_sequence: int | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Search every room for an already-authorized global administrator."""

        normalized_query = str(query or "").strip()
        normalized_conversation = str(conversation_id or "").strip() or None
        if normalized_conversation is not None:
            normalized_conversation = validate_conversation_id(normalized_conversation)
        normalized_sender = str(sender_query or "").strip()
        normalized_message_kind = str(message_kind or "").strip().casefold() or None
        normalized_notification_mode = (
            str(notification_mode or "").strip().casefold() or None
        )
        normalized_created_after = (
            float(created_after) if created_after is not None else None
        )
        normalized_created_before = (
            float(created_before) if created_before is not None else None
        )
        if len(normalized_query) > 200:
            raise ValueError("query must be at most 200 characters")
        if len(normalized_sender) > 200:
            raise ValueError("sender_query must be at most 200 characters")
        if normalized_message_kind not in {None, "message", "task", "forward"}:
            raise ValueError("message_kind must be message, task, or forward")
        if normalized_notification_mode not in {None, "ordinary", "mention"}:
            raise ValueError("notification_mode must be ordinary or mention")
        maximum_timestamp = 253_402_300_799.0
        for label, timestamp in (
            ("created_after", normalized_created_after),
            ("created_before", normalized_created_before),
        ):
            if timestamp is not None and not 0 <= timestamp <= maximum_timestamp:
                raise ValueError(f"{label} is outside the supported range")
        if (
            normalized_created_after is not None
            and normalized_created_before is not None
            and normalized_created_after >= normalized_created_before
        ):
            raise ValueError("created_after must be earlier than created_before")
        normalized_limit = max(1, min(int(limit), 100))
        clauses: list[str] = []
        parameters: list[Any] = []
        if normalized_query:
            clauses.append(
                "(instr(lower(message.body), lower(?)) > 0 "
                "OR instr(lower(message.conversation_id), lower(?)) > 0 "
                "OR EXISTS (SELECT 1 FROM message_links AS searched_link "
                "WHERE searched_link.message_id = message.message_id "
                "AND instr(lower(searched_link.url), lower(?)) > 0))"
            )
            parameters.extend(
                (normalized_query, normalized_query, normalized_query)
            )
        if normalized_conversation is not None:
            clauses.append("message.conversation_id = ?")
            parameters.append(normalized_conversation)
        if normalized_sender:
            clauses.append(
                "(instr(lower(sender.display_name), lower(?)) > 0 "
                "OR instr(lower(sender.client_type), lower(?)) > 0 "
                "OR sender.participant_id = ?)"
            )
            parameters.extend((normalized_sender, normalized_sender, normalized_sender))
        if normalized_message_kind is not None:
            clauses.append("message.message_kind = ?")
            parameters.append(normalized_message_kind)
        if normalized_notification_mode is not None:
            clauses.append("message.notification_mode = ?")
            parameters.append(normalized_notification_mode)
        if normalized_created_after is not None:
            clauses.append("message.created_at >= ?")
            parameters.append(normalized_created_after)
        if normalized_created_before is not None:
            clauses.append("message.created_at < ?")
            parameters.append(normalized_created_before)
        if before_sequence is not None:
            clauses.append("message.sequence < ?")
            parameters.append(max(0, int(before_sequence)))
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(normalized_limit + 1)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT message.sequence, message.room_sequence,
                       message.message_id, message.conversation_id,
                       message.sender_participant_id, message.message_kind,
                       message.notification_mode, message.reply_to,
                       message.body, message.created_at,
                       sender.client_type AS sender_client_type,
                       sender.display_name AS sender_display_name,
                       sender.avatar_key AS sender_avatar_key,
                       room.status AS room_status,
                       redaction.redacted_at,
                       (SELECT GROUP_CONCAT(marker.marker_kind)
                        FROM room_message_markers AS marker
                        WHERE marker.conversation_id = message.conversation_id
                          AND marker.message_id = message.message_id)
                           AS marker_kinds
                FROM messages AS message
                JOIN participants AS sender
                  ON sender.participant_id = message.sender_participant_id
                JOIN rooms AS room
                  ON room.conversation_id = message.conversation_id
                LEFT JOIN history_message_redactions AS redaction
                  ON redaction.message_id = message.message_id
                {where_sql}
                ORDER BY message.sequence DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        has_more = len(rows) > normalized_limit
        page = rows[:normalized_limit]
        results = []
        for row in page:
            body = str(row["body"])
            results.append(
                {
                    "sequence": int(row["sequence"]),
                    "room_sequence": int(row["room_sequence"] or row["sequence"]),
                    "message_id": str(row["message_id"]),
                    "conversation_id": str(row["conversation_id"]),
                    "room_status": str(row["room_status"]),
                    "sender_participant_id": str(row["sender_participant_id"]),
                    "sender_client_type": str(row["sender_client_type"]),
                    "sender_display_name": str(row["sender_display_name"]),
                    "sender_avatar_key": str(row["sender_avatar_key"] or "auto"),
                    "message_kind": str(row["message_kind"] or "message"),
                    "notification_mode": str(row["notification_mode"] or "ordinary"),
                    "reply_to": str(row["reply_to"]) if row["reply_to"] else None,
                    "marker_kinds": sorted(
                        value
                        for value in str(row["marker_kinds"] or "").split(",")
                        if value
                    ),
                    "body_preview": body[:500],
                    "body_truncated": len(body) > 500,
                    "content_redacted": row["redacted_at"] is not None,
                    "created_at": float(row["created_at"]),
                }
            )
        return {
            "query": normalized_query,
            "conversation_id": normalized_conversation,
            "sender_query": normalized_sender,
            "filters": {
                "message_kind": normalized_message_kind,
                "notification_mode": normalized_notification_mode,
                "created_after": normalized_created_after,
                "created_before": normalized_created_before,
            },
            "results": results,
            "count": len(results),
            "has_more": has_more,
            "next_before_sequence": (
                int(page[-1]["sequence"]) if has_more and page else None
            ),
        }

    def message_receipts(
        self,
        conversation_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        conversation = validate_conversation_id(conversation_id)
        normalized_after = max(0, int(after_sequence))
        normalized_limit = max(1, min(int(limit), 1_000))
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT m.sequence, m.message_id
                FROM messages AS m
                WHERE m.conversation_id = ? AND m.sequence > ?
                ORDER BY m.sequence DESC
                LIMIT ?
                """,
                (conversation, normalized_after, normalized_limit),
            ).fetchall()
            projections = self._message_delivery_projection_locked(
                connection,
                [str(row["message_id"]) for row in rows],
            )
        result: list[dict[str, Any]] = []
        for row in reversed(rows):
            payload: dict[str, Any] = {
                "sequence": int(row["sequence"]),
                "message_id": str(row["message_id"]),
            }
            payload.update(projections[str(row["message_id"])])
            result.append(payload)
        return result

    @staticmethod
    def _message_payload(row: sqlite3.Row) -> dict[str, Any]:
        keys = set(row.keys())
        payload = {
            "sequence": int(row["sequence"]),
            "room_sequence": (
                int(row["room_sequence"])
                if "room_sequence" in keys and row["room_sequence"] is not None
                else int(row["sequence"])
            ),
            "message_id": str(row["message_id"]),
            "conversation_id": str(row["conversation_id"]),
            "sender_participant_id": str(row["sender_participant_id"]),
            "sender_seat": str(row["sender_seat"] or "unknown"),
            "sender_alias": str(row["sender_alias"]),
            "sender_client_type": str(row["sender_client_type"]),
            "sender_display_name": str(row["sender_display_name"]),
            "sender_signature": str(row["sender_signature"]),
            "sender_avatar_key": str(row["sender_avatar_key"] or "auto"),
            "audience_kind": str(row["audience_kind"]),
            "audience_value": str(row["audience_value"]),
            "message_kind": str(row["message_kind"] or "message"),
            "body": str(row["body"]),
            "refs": json.loads(str(row["refs_json"])),
            "mentions": json.loads(str(row["mentions_json"] or "[]")),
            "wake_all_agents": bool(row["wake_all_agents"]),
            "reply_to": str(row["reply_to"]) if row["reply_to"] else None,
            "status": str(row["status"]),
            "claimed_by": str(row["claimed_by"]) if row["claimed_by"] else None,
            "claimant_alias": str(row["claimant_alias"] or ""),
            "claimant_display_name": str(row["claimant_display_name"] or ""),
            "claim_until": (float(row["claim_until"]) if row["claim_until"] else None),
            "ack_count": int(row["ack_count"] or 0),
            "receipt_count": int(row["receipt_count"] or 0),
            "created_at": float(row["created_at"]),
            "thread_root_message_id": str(row["reply_to"] or row["message_id"]),
            "reply_count": int(row["reply_count"] or 0),
        }
        if row["forwarded_from_message_id"] is not None:
            payload["forwarded_from"] = {
                "message_id": str(row["forwarded_from_message_id"]),
                "conversation_id": str(row["forwarded_source_conversation_id"]),
                "sequence": int(row["forwarded_source_sequence"]),
                "room_sequence": int(
                    row["forwarded_source_room_sequence"]
                    or row["forwarded_source_sequence"]
                ),
                "sender_display_name": str(
                    row["forwarded_source_sender_display_name"] or ""
                ),
                "sender_client_type": str(
                    row["forwarded_source_sender_client_type"] or ""
                ),
            }
        if row["authorization_kind"] is not None:
            revoked_at = (
                float(row["authorization_revoked_at"])
                if row["authorization_revoked_at"] is not None
                else None
            )
            payload["authorization"] = {
                "kind": str(row["authorization_kind"]),
                "source_message_id": str(row["message_id"]),
                "issuer_user_id": str(row["authorization_issuer_user_id"]),
                "issuer_username": str(row["authorization_issuer_username"]),
                "issuer_role_at_send": str(row["authorization_issuer_role"]),
                "issuer_participant_id": str(
                    row["authorization_issuer_participant_id"]
                ),
                "body_sha256": str(row["authorization_body_sha256"]),
                "target_kind": str(row["authorization_target_kind"]),
                "target_participant_ids": json.loads(
                    str(row["authorization_target_participant_ids_json"] or "[]")
                ),
                "issued_at": float(row["authorization_issued_at"]),
                "status": (
                    "legacy_frozen"
                    if str(row["authorization_kind"]) == "legacy_frozen"
                    else ("revoked" if revoked_at is not None else "active")
                ),
                "revoked_at": revoked_at,
                "revoked_by_web_user_id": (
                    str(row["authorization_revoked_by_web_user_id"])
                    if row["authorization_revoked_by_web_user_id"] is not None
                    else None
                ),
                "revocation_reason": (
                    str(row["authorization_revocation_reason"])
                    if row["authorization_revocation_reason"] is not None
                    else None
                ),
                "semantics": (
                    "ordinary_chat_only"
                    if str(row["authorization_kind"]) == "legacy_frozen"
                    else "natural_language_minimum_necessary"
                ),
            }
        if row["room_task_id"] is not None:
            payload["task"] = {
                "task_id": str(row["room_task_id"]),
                "parent_task_id": (
                    str(row["room_task_parent_id"])
                    if row["room_task_parent_id"] is not None
                    else None
                ),
                "issuer_web_user_id": str(row["room_task_issuer_web_user_id"]),
                "target_kind": str(row["room_task_target_kind"]),
                "target_participant_ids": json.loads(
                    str(row["room_task_target_participant_ids_json"] or "[]")
                ),
                "status": str(row["room_task_status"]),
                "claimed_by_participant_id": (
                    str(row["room_task_claimed_by_participant_id"])
                    if row["room_task_claimed_by_participant_id"] is not None
                    else None
                ),
                "claimed_at": (
                    float(row["room_task_claimed_at"])
                    if row["room_task_claimed_at"] is not None
                    else None
                ),
                "lease_expires_at": (
                    float(row["room_task_lease_expires_at"])
                    if row["room_task_lease_expires_at"] is not None
                    else None
                ),
                "started_at": (
                    float(row["room_task_started_at"])
                    if row["room_task_started_at"] is not None
                    else None
                ),
                "completed_at": (
                    float(row["room_task_completed_at"])
                    if row["room_task_completed_at"] is not None
                    else None
                ),
                "result_summary": (
                    str(row["room_task_result_summary"])
                    if row["room_task_result_summary"] is not None
                    else None
                ),
                "execution_cwd": (
                    str(row["room_task_execution_cwd"])
                    if row["room_task_execution_cwd"] is not None
                    else None
                ),
                "execution_thread_id": (
                    str(row["room_task_execution_thread_id"])
                    if row["room_task_execution_thread_id"] is not None
                    else None
                ),
                "created_at": float(row["room_task_created_at"]),
                "updated_at": float(row["room_task_updated_at"]),
            }
        body_input_count = int(row["body_input_count"] or 0)
        if body_input_count:
            payload["body_delivery"] = {
                "count": body_input_count,
                "delivered_count": int(row["body_input_delivered_count"] or 0),
                "applied_count": int(row["body_input_applied_count"] or 0),
                "last_delivered_at": (
                    float(row["body_input_last_delivered_at"])
                    if row["body_input_last_delivered_at"] is not None
                    else None
                ),
                "last_applied_at": (
                    float(row["body_input_last_applied_at"])
                    if row["body_input_last_applied_at"] is not None
                    else None
                ),
            }
        return payload
