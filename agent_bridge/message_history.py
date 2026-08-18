"""Read-only room history, search, participant, and message projections."""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections.abc import Sequence
from typing import Any

from .store_constants import (
    MAX_HISTORY_SEARCH_QUERY_LENGTH,
    MAX_HISTORY_SEARCH_TERMS,
)
from .store_errors import NotFoundError
from .validation import (
    ValidationError,
    conversation_id as validate_conversation_id,
    opaque_id,
)


class MessageHistoryMixin:
    def history(
        self,
        *,
        participant_id: str,
        conversation_id: str,
        limit: int = 50,
        before_sequence: int | None = None,
        after_sequence: int | None = None,
        around_sequence: int | None = None,
        authorized_session_id: str | None = None,
    ) -> dict[str, Any]:
        participant = opaque_id(participant_id, field="participant_id")
        conversation = validate_conversation_id(conversation_id)
        normalized_limit = max(1, min(int(limit), 200))
        supplied_cursors = sum(
            value is not None
            for value in (before_sequence, after_sequence, around_sequence)
        )
        if supplied_cursors > 1:
            raise ValidationError(
                "before_sequence, after_sequence, and around_sequence cannot "
                "be used together"
            )
        with self._transaction() as conn:
            now = time.time()
            self._archive_stale_rooms_locked(conn, now=now)
            if authorized_session_id is not None:
                self._require_live_room_session(
                    conn,
                    session_id=opaque_id(
                        authorized_session_id,
                        field="authorized_session_id",
                    ),
                    participant_id=participant,
                    conversation_id=conversation,
                    now=now,
                )
            self._require_membership(conn, participant, conversation)
            if after_sequence is not None:
                rows = conn.execute(
                    "SELECT * FROM messages WHERE conversation_id = ? "
                    "AND sequence > ? ORDER BY sequence LIMIT ?",
                    (conversation, int(after_sequence), normalized_limit),
                ).fetchall()
            elif around_sequence is not None:
                center = max(0, int(around_sequence))
                rows = conn.execute(
                    "SELECT * FROM messages WHERE conversation_id = ? "
                    "ORDER BY ABS(sequence - ?), sequence LIMIT ?",
                    (conversation, center, normalized_limit),
                ).fetchall()
            elif before_sequence is None:
                rows = conn.execute(
                    "SELECT * FROM messages WHERE conversation_id = ? "
                    "ORDER BY sequence DESC LIMIT ?",
                    (conversation, normalized_limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM messages WHERE conversation_id = ? "
                    "AND sequence < ? ORDER BY sequence DESC LIMIT ?",
                    (conversation, int(before_sequence), normalized_limit),
                ).fetchall()
        if around_sequence is not None:
            ordered_rows = sorted(rows, key=lambda row: int(row["sequence"]))
        else:
            ordered_rows = rows if after_sequence is not None else list(reversed(rows))
        with self._connection() as conn:
            messages = [
                self._message_payload(
                    row,
                    authorization=self._chat_authorization_for_message_locked(
                        conn,
                        message_id=str(row["message_id"]),
                        recipient_participant_id=participant,
                    ),
                )
                for row in ordered_rows
            ]
        first_sequence = messages[0]["sequence"] if messages else None
        last_sequence = messages[-1]["sequence"] if messages else None
        with self._connection() as conn:
            if around_sequence is not None:
                has_earlier = bool(
                    first_sequence is not None
                    and conn.execute(
                        "SELECT 1 FROM messages WHERE conversation_id = ? "
                        "AND sequence < ? LIMIT 1",
                        (conversation, first_sequence),
                    ).fetchone()
                )
                has_later = bool(
                    last_sequence is not None
                    and conn.execute(
                        "SELECT 1 FROM messages WHERE conversation_id = ? "
                        "AND sequence > ? LIMIT 1",
                        (conversation, last_sequence),
                    ).fetchone()
                )
                has_more = has_earlier or has_later
            elif after_sequence is not None:
                has_more = bool(
                    last_sequence is not None
                    and conn.execute(
                        "SELECT 1 FROM messages WHERE conversation_id = ? "
                        "AND sequence > ? LIMIT 1",
                        (conversation, last_sequence),
                    ).fetchone()
                )
            else:
                has_more = bool(
                    first_sequence is not None
                    and conn.execute(
                        "SELECT 1 FROM messages WHERE conversation_id = ? "
                        "AND sequence < ? LIMIT 1",
                        (conversation, first_sequence),
                    ).fetchone()
                )
        return {
            "conversation_id": conversation,
            "messages": messages,
            "count": len(messages),
            "first_sequence": first_sequence,
            "last_sequence": last_sequence,
            "has_more": has_more,
            "next_after_sequence": (
                last_sequence if after_sequence is not None and has_more else None
            ),
            "around_sequence": (
                max(0, int(around_sequence))
                if around_sequence is not None
                else None
            ),
        }

    def search_history(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        conversation_id: str,
        query: str = "",
        message_id: str | None = None,
        sequence: int | None = None,
        sender_participant_id: str | None = None,
        created_after: float | None = None,
        created_before: float | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Search joined-room history without consuming or acknowledging delivery."""

        participant = opaque_id(participant_id, field="participant_id")
        session_id = opaque_id(
            authorized_session_id,
            field="authorized_session_id",
        )
        conversation = validate_conversation_id(conversation_id)
        normalized_query = str(query or "").strip()
        if len(normalized_query) > MAX_HISTORY_SEARCH_QUERY_LENGTH or any(
            ord(character) < 32 and character not in "\t\n\r"
            for character in normalized_query
        ):
            raise ValidationError(
                "query must contain at most "
                f"{MAX_HISTORY_SEARCH_QUERY_LENGTH} visible characters"
            )
        terms = normalized_query.split()
        if len(terms) > MAX_HISTORY_SEARCH_TERMS:
            raise ValidationError(
                f"query cannot contain more than {MAX_HISTORY_SEARCH_TERMS} terms"
            )
        normalized_message_id = (
            opaque_id(message_id, field="message_id") if message_id else None
        )
        if sequence is not None and isinstance(sequence, bool):
            raise ValidationError("sequence must be an integer")
        normalized_sequence = max(0, int(sequence)) if sequence is not None else None
        normalized_sender = (
            opaque_id(sender_participant_id, field="sender_participant_id")
            if sender_participant_id
            else None
        )
        normalized_after = self._finite_history_timestamp(
            created_after,
            field="created_after",
        )
        normalized_before = self._finite_history_timestamp(
            created_before,
            field="created_before",
        )
        if (
            normalized_after is not None
            and normalized_before is not None
            and normalized_after > normalized_before
        ):
            raise ValidationError("created_after cannot be later than created_before")
        if not any(
            (
                terms,
                normalized_message_id,
                normalized_sequence is not None,
                normalized_sender,
                normalized_after is not None,
                normalized_before is not None,
            )
        ):
            raise ValidationError("history search requires a query or exact filter")
        normalized_limit = max(1, min(int(limit), 20))

        conditions = ["message.conversation_id = ?"]
        parameters: list[Any] = [conversation]
        if normalized_message_id is not None:
            conditions.append("message.message_id = ?")
            parameters.append(normalized_message_id)
        if normalized_sequence is not None:
            conditions.append("message.sequence = ?")
            parameters.append(normalized_sequence)
        if normalized_sender is not None:
            conditions.append("message.sender_participant_id = ?")
            parameters.append(normalized_sender)
        if normalized_after is not None:
            conditions.append("message.created_at >= ?")
            parameters.append(normalized_after)
        if normalized_before is not None:
            conditions.append("message.created_at <= ?")
            parameters.append(normalized_before)
        for term in terms:
            escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            conditions.append("message.body LIKE ? ESCAPE '\\'")
            parameters.append(f"%{escaped}%")
        parameters.append(normalized_limit)

        now = time.time()
        with self._connection() as conn:
            self._require_live_room_session(
                conn,
                session_id=session_id,
                participant_id=participant,
                conversation_id=conversation,
                now=now,
            )
            self._require_membership(conn, participant, conversation)
            rows = conn.execute(
                f"""
                SELECT message.*,
                       sender.display_name AS sender_display_name,
                       sender.client_type AS sender_client_type,
                       original.sequence AS replied_sequence,
                       original.sender_participant_id AS replied_sender_participant_id,
                       original_sender.display_name AS replied_sender_display_name
                FROM messages AS message
                JOIN participants AS sender
                  ON sender.participant_id = message.sender_participant_id
                LEFT JOIN messages AS original
                  ON original.message_id = message.reply_to
                LEFT JOIN participants AS original_sender
                  ON original_sender.participant_id = original.sender_participant_id
                WHERE {' AND '.join(conditions)}
                ORDER BY message.sequence DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()

        with self._connection() as conn:
            results = [
                self._history_search_payload(
                    row,
                    terms=terms,
                    authorization=self._chat_authorization_for_message_locked(
                        conn,
                        message_id=str(row["message_id"]),
                        recipient_participant_id=participant,
                    ),
                )
                for row in rows
            ]
        return {
            "conversation_id": conversation,
            "query": normalized_query,
            "results": results,
            "count": len(results),
            "limit": normalized_limit,
            "state_changed": False,
            "context_hint": (
                "Call agent_history with around_sequence set to a result sequence "
                "to inspect nearby messages."
            ),
        }

    def participants(
        self,
        *,
        participant_id: str,
        conversation_id: str,
        include_offline: bool = True,
        online_window_seconds: float = 90.0,
        authorized_session_id: str | None = None,
    ) -> dict[str, Any]:
        caller = opaque_id(participant_id, field="participant_id")
        conversation = validate_conversation_id(conversation_id)
        now = time.time()
        with self._transaction() as conn:
            self._archive_stale_rooms_locked(conn, now=now)
            if authorized_session_id is not None:
                self._require_live_room_session(
                    conn,
                    session_id=opaque_id(
                        authorized_session_id,
                        field="authorized_session_id",
                    ),
                    participant_id=caller,
                    conversation_id=conversation,
                    now=now,
                )
            self._require_membership(conn, caller, conversation)
            rows = conn.execute(
                """
                SELECT p.*, m.roles_json,
                       EXISTS (
                           SELECT 1
                           FROM agent_sessions AS session
                           WHERE session.participant_id = p.participant_id
                             AND session.registered_conversation_id = m.conversation_id
                             AND session.cleared_at IS NULL
                             AND session.revoked_at IS NULL
                             AND session.expires_at > ?
                             AND session.last_seen >= ?
                       ) AS room_agent_online,
                       EXISTS (
                           SELECT 1
                           FROM web_sessions AS web_session
                           JOIN web_users AS web_user
                             ON web_user.user_id = web_session.user_id
                           WHERE web_user.participant_id = p.participant_id
                             AND web_user.active = 1
                             AND web_session.revoked_at IS NULL
                             AND web_session.expires_at > ?
                             AND web_session.last_seen >= ?
                       ) AS room_web_online
                FROM memberships AS m
                JOIN participants AS p ON p.participant_id = m.participant_id
                WHERE m.conversation_id = ? AND m.active = 1
                ORDER BY p.display_name, p.participant_id
                """,
                (
                    now,
                    now - float(online_window_seconds),
                    now,
                    now - float(online_window_seconds),
                    conversation,
                ),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            online = bool(
                (
                    str(row["status"]) == "online"
                    and row["room_agent_online"]
                )
                or row["room_web_online"]
            )
            if not include_offline and not online:
                continue
            result.append(
                {
                    "participant_id": str(row["participant_id"]),
                    "client_type": str(row["client_type"]),
                    "session_alias": str(row["session_alias"]),
                    "display_name": str(row["display_name"]),
                    "signature": str(row["signature"]),
                    "roles": json.loads(str(row["roles_json"])),
                    "capabilities": json.loads(str(row["capabilities_json"])),
                    "status": "online" if online else "offline",
                    "last_seen": float(row["last_seen"]),
                }
            )
        return {
            "conversation_id": conversation,
            "participants": result,
            "count": len(result),
        }

    @staticmethod
    def _finite_history_timestamp(
        value: float | None,
        *,
        field: str,
    ) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValidationError(f"{field} must be a finite Unix timestamp")
        normalized = float(value)
        if not math.isfinite(normalized) or normalized < 0:
            raise ValidationError(f"{field} must be a finite Unix timestamp")
        return normalized

    @staticmethod
    def _history_search_payload(
        row: sqlite3.Row,
        *,
        terms: Sequence[str],
        authorization: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body_text = str(row["body"])
        folded = body_text.casefold()
        offsets = [folded.find(term.casefold()) for term in terms if term]
        offsets = [offset for offset in offsets if offset >= 0]
        start = max(0, (min(offsets) if offsets else 0) - 70)
        end = min(len(body_text), start + 240)
        snippet = body_text[start:end]
        if start > 0:
            snippet = "…" + snippet
        if end < len(body_text):
            snippet += "…"
        payload = {
            "message_id": str(row["message_id"]),
            "sequence": int(row["sequence"]),
            "sender_participant_id": str(row["sender_participant_id"]),
            "sender_seat": (
                str(row["sender_seat"] or "unknown")
                if "sender_seat" in set(row.keys())
                else "unknown"
            ),
            "sender_display_name": str(row["sender_display_name"]),
            "sender_client_type": str(row["sender_client_type"]),
            "created_at": float(row["created_at"]),
            "snippet": snippet,
            "reply": (
                {
                    "message_id": str(row["reply_to"]),
                    "sequence": int(row["replied_sequence"]),
                    "sender_participant_id": str(
                        row["replied_sender_participant_id"]
                    ),
                    "sender_display_name": str(
                        row["replied_sender_display_name"] or ""
                    ),
                }
                if row["reply_to"] is not None
                else None
            ),
        }
        if authorization is not None:
            payload["authorization"] = authorization
        return payload

    @staticmethod
    def _message_payload(
        row: sqlite3.Row | None,
        *,
        authorization: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if row is None:
            raise NotFoundError("message row disappeared")
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
            "sender_seat": (
                str(row["sender_seat"] or "unknown")
                if "sender_seat" in set(row.keys())
                else "unknown"
            ),
            "notification_mode": (
                str(row["notification_mode"] or "ordinary")
                if "notification_mode" in set(row.keys())
                else (
                    "mention"
                    if row["reply_to"]
                    or bool(row["wake_all_agents"])
                    or json.loads(str(row["mentions_json"] or "[]"))
                    else "ordinary"
                )
            ),
            "audience_kind": str(row["audience_kind"]),
            "message_kind": str(row["message_kind"] or "message"),
            "audience_value": str(row["audience_value"]),
            "body": str(row["body"]),
            "refs": json.loads(str(row["refs_json"])),
            "mentions": json.loads(str(row["mentions_json"] or "[]")),
            "wake_all_agents": bool(row["wake_all_agents"]),
            "reply_to": str(row["reply_to"]) if row["reply_to"] else None,
            "status": str(row["status"]),
            "claimed_by": str(row["claimed_by"]) if row["claimed_by"] else None,
            "claim_until": float(row["claim_until"]) if row["claim_until"] else None,
            "created_at": float(row["created_at"]),
        }
        if (
            "forwarded_from_message_id" in keys
            and row["forwarded_from_message_id"] is not None
        ):
            payload["forwarded_from_message_id"] = str(
                row["forwarded_from_message_id"]
            )
        if authorization is not None:
            payload["authorization"] = authorization
        if "delivery_state" in keys:
            reasons = json.loads(str(row["delivery_reasons_json"] or "[]"))
            payload["delivery"] = {
                "state": str(row["delivery_state"]),
                "reasons": reasons,
                "priority": (
                    "mention"
                    if str(row["delivery_priority"]) == "direct"
                    else str(row["delivery_priority"])
                ),
                "actionable": bool(row["delivery_actionable"]),
                "first_delivered_at": (
                    float(row["delivery_first_delivered_at"])
                    if row["delivery_first_delivered_at"] is not None
                    else None
                ),
                "last_delivered_at": (
                    float(row["delivery_last_delivered_at"])
                    if row["delivery_last_delivered_at"] is not None
                    else None
                ),
                "acked_at": (
                    float(row["delivery_acked_at"])
                    if row["delivery_acked_at"] is not None
                    else None
                ),
                "attempt_count": int(row["delivery_attempt_count"]),
            }
        return payload
