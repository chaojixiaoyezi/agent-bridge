"""Realtime delivery, backlog, notification, claim, release, and receipt state."""

from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter
from collections.abc import Sequence
from typing import Any

from .store_constants import (
    DEFAULT_OFFLINE_BACKLOG_KEEP_MESSAGES,
    MAX_OFFLINE_BACKLOG_KEEP_MESSAGES,
    MAX_WAIT_MESSAGES_PAGE_SIZE,
    MESSAGE_ACTIONS,
)
from .store_errors import (
    AuthorizationError,
    BridgeError,
    ConflictError,
    NotFoundError,
)
from .validation import ValidationError, compact_json, opaque_id


ROOM_MESSAGE_SEQUENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS room_message_sequences (
    conversation_id TEXT PRIMARY KEY,
    last_sequence INTEGER NOT NULL DEFAULT 0 CHECK (last_sequence >= 0),
    FOREIGN KEY (conversation_id) REFERENCES rooms(conversation_id)
);

DROP TRIGGER IF EXISTS trg_messages_assign_room_sequence;
CREATE TRIGGER trg_messages_assign_room_sequence
AFTER INSERT ON messages
WHEN NEW.room_sequence IS NULL
BEGIN
    INSERT INTO room_message_sequences (conversation_id, last_sequence)
    VALUES (NEW.conversation_id, 1)
    ON CONFLICT(conversation_id) DO UPDATE
    SET last_sequence = room_message_sequences.last_sequence + 1;

    UPDATE messages
    SET room_sequence = (
        SELECT last_sequence
        FROM room_message_sequences
        WHERE conversation_id = NEW.conversation_id
    )
    WHERE sequence = NEW.sequence;
END;

DROP TRIGGER IF EXISTS trg_messages_sync_explicit_room_sequence;
CREATE TRIGGER trg_messages_sync_explicit_room_sequence
AFTER INSERT ON messages
WHEN NEW.room_sequence IS NOT NULL
BEGIN
    INSERT INTO room_message_sequences (conversation_id, last_sequence)
    VALUES (NEW.conversation_id, NEW.room_sequence)
    ON CONFLICT(conversation_id) DO UPDATE
    SET last_sequence = MAX(
        room_message_sequences.last_sequence,
        excluded.last_sequence
    );
END;

DROP TRIGGER IF EXISTS trg_messages_room_sequence_immutable;
CREATE TRIGGER trg_messages_room_sequence_immutable
BEFORE UPDATE OF room_sequence ON messages
WHEN OLD.room_sequence IS NOT NULL
 AND NEW.room_sequence IS NOT OLD.room_sequence
BEGIN
    SELECT RAISE(ABORT, 'ROOM_MESSAGE_SEQUENCE_IMMUTABLE');
END;
"""


class MessageDeliveryMixin:
    def compact_optional_backlog(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        keep_recent: int = DEFAULT_OFFLINE_BACKLOG_KEEP_MESSAGES,
    ) -> dict[str, Any]:
        """Cancel only old optional deliveries while preserving room history.

        This is used for an explicit reconnect backlog event. Required replies,
        actionable participant/role deliveries, and the newest optional window
        remain in the normal delivery queue. Cancelled rows keep their original
        message in history/search and gain an audit reason instead of pretending
        the Agent read or acknowledged their bodies.
        """

        participant = opaque_id(participant_id, field="participant_id")
        session = opaque_id(
            authorized_session_id,
            field="authorized_session_id",
        )
        if isinstance(keep_recent, bool):
            raise ValidationError("keep_recent must be an integer")
        try:
            keep = int(keep_recent)
        except (TypeError, ValueError) as exc:
            raise ValidationError("keep_recent must be an integer") from exc
        if not 1 <= keep <= MAX_OFFLINE_BACKLOG_KEEP_MESSAGES:
            raise ValidationError(
                "keep_recent must be between 1 and "
                f"{MAX_OFFLINE_BACKLOG_KEEP_MESSAGES}"
            )

        now = time.time()
        compacted_count = 0
        oldest_sequence: int | None = None
        newest_sequence: int | None = None
        sender_counts: Counter[tuple[str, str, str]] = Counter()
        with self._transaction() as conn:
            self._archive_stale_rooms_locked(conn, now=now)
            session_row = self._require_live_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=now,
            )
            conversation = str(session_row["registered_conversation_id"])
            self._require_membership(conn, participant, conversation)

            candidate_where = """
                delivery.participant_id = ?
                AND delivery.state IN ('pending', 'delivered')
                AND delivery.actionable = 0
                AND instr(delivery.reasons_json, '"mention"') = 0
                AND instr(delivery.reasons_json, '"agent_request"') = 0
                AND message.conversation_id = ?
            """
            optional_total = int(
                conn.execute(
                    "SELECT COUNT(*) FROM message_deliveries AS delivery "
                    "JOIN messages AS message "
                    "ON message.message_id = delivery.message_id "
                    f"WHERE {candidate_where}",
                    (participant, conversation),
                ).fetchone()[0]
            )
            protected_pending = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM message_deliveries AS delivery
                    JOIN messages AS message
                      ON message.message_id = delivery.message_id
                    WHERE delivery.participant_id = ?
                      AND delivery.state IN ('pending', 'delivered')
                      AND message.conversation_id = ?
                      AND (
                          delivery.actionable = 1
                          OR instr(delivery.reasons_json, '"mention"') > 0
                          OR instr(
                              delivery.reasons_json,
                              '"agent_request"'
                          ) > 0
                      )
                    """,
                    (participant, conversation),
                ).fetchone()[0]
            )

            while optional_total - compacted_count > keep:
                rows = conn.execute(
                    """
                    SELECT delivery.message_id, delivery.reasons_json,
                           message.sequence,
                           sender.participant_id AS sender_participant_id,
                           sender.client_type AS sender_client_type,
                           sender.display_name AS sender_display_name
                    FROM message_deliveries AS delivery
                    JOIN messages AS message
                      ON message.message_id = delivery.message_id
                    JOIN participants AS sender
                      ON sender.participant_id = message.sender_participant_id
                    WHERE """
                    + candidate_where
                    + " ORDER BY message.sequence DESC LIMIT 500 OFFSET ?",
                    (participant, conversation, keep),
                ).fetchall()
                if not rows:
                    break
                updates: list[tuple[str, str, str]] = []
                for row in rows:
                    try:
                        reasons = list(json.loads(str(row["reasons_json"] or "[]")))
                    except (TypeError, json.JSONDecodeError):
                        reasons = []
                    if "offline_compacted" not in reasons:
                        reasons.append("offline_compacted")
                    updates.append(
                        (
                            compact_json(reasons),
                            str(row["message_id"]),
                            participant,
                        )
                    )
                    sequence = int(row["sequence"])
                    oldest_sequence = (
                        sequence
                        if oldest_sequence is None
                        else min(oldest_sequence, sequence)
                    )
                    newest_sequence = (
                        sequence
                        if newest_sequence is None
                        else max(newest_sequence, sequence)
                    )
                    sender_counts[
                        (
                            str(row["sender_participant_id"]),
                            str(row["sender_client_type"]),
                            str(row["sender_display_name"]),
                        )
                    ] += 1
                conn.executemany(
                    """
                    UPDATE message_deliveries
                    SET state = 'cancelled', delivery_stage = 'cancelled',
                        reasons_json = ?, actionable = 0
                    WHERE message_id = ? AND participant_id = ?
                      AND state IN ('pending', 'delivered')
                    """,
                    updates,
                )
                compacted_count += len(rows)

        sender_summary = [
            {
                "participant_id": sender[0],
                "client_type": sender[1],
                "display_name": sender[2],
                "message_count": count,
            }
            for sender, count in sorted(
                sender_counts.items(),
                key=lambda item: (-item[1], item[0][2], item[0][0]),
            )[:10]
        ]
        return {
            "applied": compacted_count > 0,
            "conversation_id": conversation,
            "compacted_optional_count": compacted_count,
            "kept_recent_optional_count": min(optional_total, keep),
            "protected_pending_count": protected_pending,
            "oldest_compacted_sequence": oldest_sequence,
            "newest_compacted_sequence": newest_sequence,
            "sender_counts": sender_summary,
            "other_sender_message_count": max(
                0,
                compacted_count
                - sum(item["message_count"] for item in sender_summary),
            ),
            "history_preserved": True,
            "history_hint": (
                "Use agent_history with before_sequence/around_sequence or "
                "agent_search_history when older context is relevant."
            ),
        }

    def _native_delivery_handoff(
        self,
        *,
        participant_id: str,
        authorized_session_id: str | None,
    ) -> dict[str, Any] | None:
        if authorized_session_id is None:
            return None
        participant = opaque_id(participant_id, field="participant_id")
        session_id = opaque_id(
            authorized_session_id,
            field="authorized_session_id",
        )
        now = time.time()
        with self._connection() as conn:
            session = self._require_live_session(
                conn,
                session_id=session_id,
                participant_id=participant,
                now=now,
            )
            component = str(session["component"] or "unknown")
            connector_id = str(session["connector_id"] or "")
            if component not in {"listener", "chat"} or not connector_id:
                return None
            connector = conn.execute(
                "SELECT native_delivery_mode, native_lease_id, "
                "native_lease_expires_at, tui_native_session_id "
                "FROM agent_connectors WHERE connector_id = ? "
                "AND accepted_participant_id = ? AND revoked_at IS NULL",
                (connector_id, participant),
            ).fetchone()
            if (
                connector is None
                or str(connector["native_delivery_mode"] or "")
                != "native_preferred"
            ):
                return None
        return {
            "active": True,
            "connector_id": connector_id,
            "component": component,
            "lease_id": str(connector["native_lease_id"] or "") or None,
            "native_session_id": (
                str(connector["tui_native_session_id"] or "") or None
            ),
            "lease_expires_at": (
                float(connector["native_lease_expires_at"])
                if connector["native_lease_expires_at"] is not None
                else None
            ),
            "reason": "exact_native_session_owns_delivery",
        }

    def wait_messages(
        self,
        *,
        participant_id: str,
        authorized_session_id: str | None = None,
        wait_seconds: float = 30.0,
        limit: int = 20,
        auto_claim_roles: bool = True,
    ) -> dict[str, Any]:
        participant = opaque_id(participant_id, field="participant_id")
        wait_for = max(0.0, min(float(wait_seconds), 120.0))
        # Keep each Agent context page small and predictable.  Callers can
        # follow ``has_more`` for up to five pages (100 messages) per model
        # turn without one request flooding the context window.
        normalized_limit = max(1, min(int(limit), MAX_WAIT_MESSAGES_PAGE_SIZE))
        deadline = time.monotonic() + wait_for
        conversation = self._authorized_session_room(
            participant_id=participant,
            authorized_session_id=authorized_session_id,
        )
        with self._connection() as conn:
            if conversation is None:
                # Internal callers may intentionally omit a session and read
                # the participant's aggregate inbox.
                self_row = conn.execute(
                    """
                    SELECT participant.participant_id, participant.client_type,
                           participant.display_name, participant.signature,
                           COALESCE((
                               SELECT membership.roles_json
                               FROM memberships AS membership
                               WHERE membership.participant_id =
                                     participant.participant_id
                                 AND membership.active = 1
                               ORDER BY membership.updated_at DESC
                               LIMIT 1
                           ), '[]') AS roles_json
                    FROM participants AS participant
                    WHERE participant.participant_id = ?
                    """,
                    (participant,),
                ).fetchone()
            else:
                self_row = conn.execute(
                    """
                    SELECT participant.participant_id, participant.client_type,
                           participant.display_name, participant.signature,
                           membership.roles_json
                    FROM participants AS participant
                    JOIN memberships AS membership
                      ON membership.participant_id = participant.participant_id
                     AND membership.conversation_id = ?
                     AND membership.active = 1
                    WHERE participant.participant_id = ?
                    """,
                    (conversation, participant),
                ).fetchone()
        if self_row is None:
            raise ConflictError("Agent is not an active member of its session room")
        self_identity = {
            "participant_id": str(self_row["participant_id"]),
            "client_type": str(self_row["client_type"]),
            "display_name": str(self_row["display_name"]),
            "signature": str(self_row["signature"]),
            "roles": json.loads(str(self_row["roles_json"] or "[]")),
            "identity_rule": (
                "display_name is your fixed public name; a shadow listener and "
                "task executor are seats of this same public identity"
            ),
        }

        native_handoff = self._native_delivery_handoff(
            participant_id=participant,
            authorized_session_id=authorized_session_id,
        )
        if native_handoff is not None:
            if wait_for > 0:
                time.sleep(wait_for)
            self.heartbeat(
                participant,
                authorized_session_id=authorized_session_id,
            )
            backlog = self._pending_manifest(
                participant,
                conversation_id=conversation,
            )
            return {
                "participant_id": participant,
                "conversation_id": conversation,
                "self_identity": self_identity,
                "messages": [],
                "count": 0,
                "timed_out": True,
                "last_sequence": None,
                "backlog": backlog,
                "pending_count": backlog["pending_count"],
                "has_more": backlog["pending_count"] > 0,
                "native_handoff": native_handoff,
            }

        while True:
            messages = self._pending_messages(
                participant,
                limit=normalized_limit,
                auto_claim_roles=bool(auto_claim_roles),
                authorized_session_id=authorized_session_id,
                conversation_id=conversation,
            )
            if messages:
                backlog = self._pending_manifest(
                    participant,
                    conversation_id=conversation,
                )
                return {
                    "participant_id": participant,
                    "conversation_id": conversation,
                    "self_identity": self_identity,
                    "messages": messages,
                    "count": len(messages),
                    "timed_out": False,
                    "last_sequence": max(item["sequence"] for item in messages),
                    "backlog": backlog,
                    "pending_count": backlog["pending_count"],
                    "has_more": backlog["pending_count"] > len(messages),
                }
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.heartbeat(
                    participant,
                    authorized_session_id=authorized_session_id,
                )
                backlog = self._pending_manifest(
                    participant,
                    conversation_id=conversation,
                )
                return {
                    "participant_id": participant,
                    "conversation_id": conversation,
                    "self_identity": self_identity,
                    "messages": [],
                    "count": 0,
                    "timed_out": True,
                    "last_sequence": None,
                    "backlog": backlog,
                    "pending_count": backlog["pending_count"],
                    "has_more": backlog["pending_count"] > 0,
                }
            time.sleep(min(self.poll_interval_seconds, remaining))

    def notification_snapshot(
        self,
        *,
        participant_id: str,
        authorized_session_id: str | None = None,
        after_sequence: int | None = None,
    ) -> dict[str, Any]:
        """Return wake-up metadata without reading bodies or consuming delivery state."""
        participant = opaque_id(participant_id, field="participant_id")
        requested_cursor = max(0, int(after_sequence or 0))
        now = time.time()
        conversation: str | None = None
        native_handoff: dict[str, Any] | None = None
        with self._transaction() as conn:
            self._archive_stale_rooms_locked(conn, now=now)
            if authorized_session_id is not None:
                session_row = self._require_live_session(
                    conn,
                    session_id=opaque_id(
                        authorized_session_id,
                        field="authorized_session_id",
                    ),
                    participant_id=participant,
                    now=now,
                )
                conversation = str(session_row["registered_conversation_id"])
                component = str(session_row["component"] or "unknown")
                connector_id = str(session_row["connector_id"] or "")
                if component in {"listener", "chat"} and connector_id:
                    connector = conn.execute(
                        "SELECT native_delivery_mode, native_lease_id, "
                        "native_lease_expires_at, tui_native_session_id "
                        "FROM agent_connectors WHERE connector_id = ? "
                        "AND accepted_participant_id = ? AND revoked_at IS NULL",
                        (connector_id, participant),
                    ).fetchone()
                    if (
                        connector is not None
                        and str(connector["native_delivery_mode"] or "")
                        == "native_preferred"
                    ):
                        native_handoff = {
                            "active": True,
                            "connector_id": connector_id,
                            "component": component,
                            "lease_id": (
                                str(connector["native_lease_id"] or "") or None
                            ),
                            "native_session_id": (
                                str(connector["tui_native_session_id"] or "")
                                or None
                            ),
                            "lease_expires_at": (
                                float(connector["native_lease_expires_at"])
                                if connector["native_lease_expires_at"] is not None
                                else None
                            ),
                            "reason": "exact_native_session_owns_delivery",
                        }
            known = conn.execute(
                "SELECT participant_id FROM participants WHERE participant_id = ?",
                (participant,),
            ).fetchone()
            if known is None:
                raise NotFoundError(f"unknown participant: {participant}")
            if conversation is None:
                room_sequence = int(
                    conn.execute(
                        "SELECT COALESCE(MAX(sequence), 0) FROM messages"
                    ).fetchone()[0]
                )
            else:
                self._require_membership(conn, participant, conversation)
                room_sequence = int(
                    conn.execute(
                        "SELECT COALESCE(MAX(sequence), 0) FROM messages "
                        "WHERE conversation_id = ?",
                        (conversation,),
                    ).fetchone()[0]
                )
        # A corrupt or manually edited Last-Event-ID must not suppress every
        # future event forever.  Global message sequence is monotonic, so it is
        # the largest cursor the server can currently have issued.
        cursor = min(requested_cursor, room_sequence)
        backlog = self._pending_manifest(
            participant,
            conversation_id=conversation,
        )
        new_since_cursor = self._pending_manifest(
            participant,
            after_sequence=cursor,
            conversation_id=conversation,
        )
        room_activity_since_cursor = self._activity_manifest(
            participant,
            after_sequence=cursor,
            conversation_id=conversation,
        )
        result = {
            "participant_id": participant,
            "conversation_id": conversation,
            # Cursor tracks this connector room's append-only sequence, not
            # unread state. Another room cannot wake or advance this listener.
            "cursor": room_sequence,
            "has_new": new_since_cursor["pending_count"] > 0,
            "has_room_activity": room_activity_since_cursor["activity_count"] > 0,
            "backlog": backlog,
            "new_since_cursor": new_since_cursor,
            "room_activity_since_cursor": room_activity_since_cursor,
            "server_time": time.time(),
        }
        if native_handoff is not None:
            result["has_new"] = False
            result["has_room_activity"] = False
            result["native_handoff"] = native_handoff
        return result

    def wait_for_notification(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        after_sequence: int | None = None,
        wait_seconds: float = 25.0,
    ) -> dict[str, Any]:
        """Wait for room activity without repeatedly rebuilding delivery aggregates.

        The append-only global sequence is the cheap change detector.  Full
        participant-scoped manifests and sliding-session renewal run only when
        that sequence changes (plus the initial snapshot), while delivery rows
        remain the authoritative backlog and are never consumed here.
        """
        wait_for = max(0.0, min(float(wait_seconds), 60.0))
        deadline = time.monotonic() + wait_for
        snapshot = self.notification_snapshot(
            participant_id=participant_id,
            authorized_session_id=authorized_session_id,
            after_sequence=after_sequence,
        )
        if snapshot["has_room_activity"]:
            snapshot["timed_out"] = False
            return snapshot
        observed_sequence = int(snapshot["cursor"])
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                snapshot["timed_out"] = True
                return snapshot
            time.sleep(min(max(self.poll_interval_seconds, 0.5), remaining))
            latest_sequence = self._message_sequence(
                snapshot.get("conversation_id")
            )
            if latest_sequence <= observed_sequence:
                continue
            observed_sequence = latest_sequence
            snapshot = self.notification_snapshot(
                participant_id=participant_id,
                authorized_session_id=authorized_session_id,
                after_sequence=after_sequence,
            )
            if snapshot["has_room_activity"]:
                snapshot["timed_out"] = False
                return snapshot

    def _message_sequence(self, conversation_id: object = None) -> int:
        """Read a monotonic room change key without renewing a session."""

        with self._connection() as conn:
            if conversation_id is not None:
                return int(
                    conn.execute(
                        "SELECT COALESCE(MAX(sequence), 0) FROM messages "
                        "WHERE conversation_id = ?",
                        (str(conversation_id),),
                    ).fetchone()[0]
                )
            return int(
                conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) FROM messages"
                ).fetchone()[0]
            )

    def message_action(
        self,
        *,
        participant_id: str,
        message_id: str,
        action: str,
        lease_seconds: float = 120.0,
        authorized_session_id: str | None = None,
    ) -> dict[str, Any]:
        participant = opaque_id(participant_id, field="participant_id")
        message = opaque_id(message_id, field="message_id")
        normalized_action = str(action or "").strip().lower()
        if normalized_action not in MESSAGE_ACTIONS:
            raise ValidationError(f"unsupported action: {normalized_action}")
        if normalized_action == "claim":
            return self._claim(
                participant,
                message,
                lease_seconds=lease_seconds,
                authorized_session_id=authorized_session_id,
            )
        if normalized_action == "release":
            return self._release(
                participant,
                message,
                authorized_session_id=authorized_session_id,
            )
        return self._ack(
            participant,
            message,
            authorized_session_id=authorized_session_id,
        )

    def reply(
        self,
        *,
        authorized_session_id: str,
        participant_id: str,
        message_id: str,
        body_text: str,
        refs: Sequence[dict[str, Any]] | None = None,
        mentions: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        participant = opaque_id(participant_id, field="participant_id")
        original_id = opaque_id(message_id, field="message_id")
        with self._connection() as conn:
            original = conn.execute(
                "SELECT * FROM messages WHERE message_id = ?",
                (original_id,),
            ).fetchone()
        if original is None:
            raise NotFoundError(f"unknown message: {original_id}")
        with self._connection() as conn:
            self._require_live_room_session(
                conn,
                session_id=opaque_id(
                    authorized_session_id,
                    field="authorized_session_id",
                ),
                participant_id=participant,
                conversation_id=str(original["conversation_id"]),
                now=time.time(),
            )
        self._require_eligible_participant(participant, original_id)
        continued_top_level = original["reply_to"] is not None
        continuation_target: str | None = None
        continuation_mentions = list(mentions or [])
        if continued_top_level:
            candidate = str(original["sender_participant_id"])
            with self._connection() as conn:
                active_sender = conn.execute(
                    "SELECT 1 FROM memberships "
                    "WHERE conversation_id = ? AND participant_id = ? "
                    "AND active = 1",
                    (str(original["conversation_id"]), candidate),
                ).fetchone()
            if candidate != participant and active_sender is not None:
                continuation_target = candidate
                if candidate not in continuation_mentions:
                    continuation_mentions.append(candidate)
        claim_acquired = False
        actionable = self._delivery_is_actionable(participant, original_id)
        if actionable and str(original["audience_kind"]) in {"participant", "role"}:
            claim_now = time.time()
            claim_acquired = not (
                str(original["claimed_by"] or "") == participant
                and float(original["claim_until"] or 0.0) > claim_now
            )
            self._claim(
                participant,
                original_id,
                lease_seconds=120.0,
                authorized_session_id=authorized_session_id,
            )
        try:
            reply_message = self.send(
                authorized_session_id=authorized_session_id,
                sender_participant_id=participant,
                conversation_id=str(original["conversation_id"]),
                body_text=body_text,
                audience_kind="room",
                audience_value="*",
                reply_to=None if continued_top_level else original_id,
                refs=refs,
                mentions=continuation_mentions,
            )
        except Exception:
            if claim_acquired:
                try:
                    self._release(participant, original_id)
                except BridgeError:
                    pass
            raise
        self._ack(participant, original_id)
        replied_at = time.time()
        with self._transaction() as conn:
            conn.execute(
                """
                UPDATE message_deliveries
                SET delivery_stage = 'replied',
                    native_replied_at = COALESCE(native_replied_at, ?)
                WHERE message_id = ? AND participant_id = ?
                  AND native_applied_at IS NOT NULL
                  AND state != 'cancelled'
                """,
                (replied_at, original_id, participant),
            )
        return {
            "reply": reply_message,
            "original_message_id": original_id,
            "original_acked": True,
            "continued_top_level": continued_top_level,
            "continuation_notified_participant_id": continuation_target,
        }

    def _pending_messages(
        self,
        participant_id: str,
        *,
        limit: int,
        auto_claim_roles: bool,
        authorized_session_id: str | None,
        conversation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        now = time.time()
        with self._connection() as conn:
            if authorized_session_id is not None:
                session_row = self._require_live_session(
                    conn,
                    session_id=opaque_id(
                        authorized_session_id,
                        field="authorized_session_id",
                    ),
                    participant_id=participant_id,
                    now=now,
                )
                bound_room = str(session_row["registered_conversation_id"])
                if conversation_id is not None and bound_room != conversation_id:
                    raise AuthorizationError(
                        f"Agent session is bound to conversation {bound_room}; "
                        f"use a room-specific connector for {conversation_id}"
                    )
                conversation_id = bound_room
            participant = conn.execute(
                "SELECT * FROM participants WHERE participant_id = ?",
                (participant_id,),
            ).fetchone()
            if participant is None:
                raise NotFoundError(f"unknown participant: {participant_id}")
            room_clause = (
                "AND message.conversation_id = ?" if conversation_id else ""
            )
            parameters: list[Any] = [participant_id, participant_id]
            if conversation_id:
                parameters.append(conversation_id)
            rows = conn.execute(
                f"""
                SELECT message.*,
                       delivery.state AS delivery_state,
                       delivery.reasons_json AS delivery_reasons_json,
                       delivery.priority AS delivery_priority,
                       delivery.actionable AS delivery_actionable,
                       delivery.first_delivered_at AS delivery_first_delivered_at,
                       delivery.last_delivered_at AS delivery_last_delivered_at,
                       delivery.acked_at AS delivery_acked_at,
                       delivery.delivery_stage AS delivery_stage,
                       delivery.native_session_id AS delivery_native_session_id,
                       delivery.native_event_id AS delivery_native_event_id,
                       delivery.native_injected_at AS delivery_native_injected_at,
                       delivery.native_applied_at AS delivery_native_applied_at,
                       delivery.native_replied_at AS delivery_native_replied_at,
                       delivery.shadow_seen_at AS delivery_shadow_seen_at,
                       delivery.attempt_count AS delivery_attempt_count
                FROM message_deliveries AS delivery
                JOIN messages AS message
                  ON message.message_id = delivery.message_id
                JOIN memberships AS membership
                  ON membership.conversation_id = message.conversation_id
                 AND membership.participant_id = delivery.participant_id
                 AND membership.active = 1
                JOIN rooms AS room
                  ON room.conversation_id = message.conversation_id
                 AND room.status = 'active'
                WHERE delivery.participant_id = ?
                  AND delivery.state IN ('pending', 'delivered')
                  AND message.sender_participant_id != ?
                  {room_clause}
                ORDER BY
                    CASE
                        WHEN instr(delivery.reasons_json, '"mention"') > 0
                          OR instr(delivery.reasons_json, '"agent_request"') > 0
                        THEN 3
                        WHEN delivery.priority IN ('direct', 'mention') THEN 2
                        WHEN delivery.priority = 'important' THEN 1
                        ELSE 0
                    END DESC,
                    message.sequence
                LIMIT 500
                """,
                parameters,
            ).fetchall()

        selected: list[sqlite3.Row] = []
        for row in rows:
            claim_until = float(row["claim_until"] or 0.0)
            claimed_by = str(row["claimed_by"] or "")
            actionable = bool(row["delivery_actionable"])
            if (
                actionable
                and claimed_by
                and claimed_by != participant_id
                and claim_until > now
            ):
                continue
            if (
                actionable
                and
                str(row["audience_kind"]) == "role"
                and auto_claim_roles
            ):
                try:
                    self._claim(
                        participant_id,
                        str(row["message_id"]),
                        lease_seconds=120.0,
                        authorized_session_id=authorized_session_id,
                    )
                except ConflictError:
                    continue
                with self._connection() as conn:
                    row = conn.execute(
                        """
                        SELECT message.*,
                               delivery.state AS delivery_state,
                               delivery.reasons_json AS delivery_reasons_json,
                               delivery.priority AS delivery_priority,
                               delivery.actionable AS delivery_actionable,
                               delivery.first_delivered_at
                                   AS delivery_first_delivered_at,
                               delivery.last_delivered_at
                                   AS delivery_last_delivered_at,
                               delivery.acked_at AS delivery_acked_at,
                               delivery.delivery_stage AS delivery_stage,
                               delivery.native_session_id
                                   AS delivery_native_session_id,
                               delivery.native_event_id AS delivery_native_event_id,
                               delivery.native_injected_at
                                   AS delivery_native_injected_at,
                               delivery.native_applied_at
                                   AS delivery_native_applied_at,
                               delivery.native_replied_at
                                   AS delivery_native_replied_at,
                               delivery.shadow_seen_at AS delivery_shadow_seen_at,
                               delivery.attempt_count AS delivery_attempt_count
                        FROM messages AS message
                        JOIN message_deliveries AS delivery
                          ON delivery.message_id = message.message_id
                        WHERE message.message_id = ?
                          AND delivery.participant_id = ?
                        """,
                        (str(row["message_id"]), participant_id),
                    ).fetchone()
            selected.append(row)
            if len(selected) >= limit:
                break

        if not selected:
            return []
        delivered_at = time.time()
        delivered_rows: list[sqlite3.Row] = []
        with self._transaction() as conn:
            self._archive_stale_rooms_locked(conn, now=delivered_at)
            if authorized_session_id is not None:
                session_row = self._require_live_session(
                    conn,
                    session_id=opaque_id(
                        authorized_session_id,
                        field="authorized_session_id",
                    ),
                    participant_id=participant_id,
                    now=delivered_at,
                )
                if conversation_id is not None and str(
                    session_row["registered_conversation_id"]
                ) != conversation_id:
                    raise AuthorizationError(
                        "Agent session room changed while messages were delivered"
                    )
            conn.execute(
                "UPDATE participants SET status = 'online', last_seen = ? "
                "WHERE participant_id = ?",
                (delivered_at, participant_id),
            )
            for row in selected:
                try:
                    self._require_membership(
                        conn,
                        participant_id,
                        str(row["conversation_id"]),
                    )
                except (ConflictError, NotFoundError):
                    continue
                updated = conn.execute(
                    """
                    UPDATE message_deliveries
                    SET state = 'delivered',
                        delivery_stage = CASE
                            WHEN delivery_stage IN (
                                'native_injected', 'native_applied', 'replied'
                            ) THEN delivery_stage
                            ELSE 'legacy_delivered'
                        END,
                        first_delivered_at = COALESCE(first_delivered_at, ?),
                        last_delivered_at = ?,
                        attempt_count = attempt_count + 1
                    WHERE message_id = ? AND participant_id = ?
                      AND state IN ('pending', 'delivered')
                    """,
                    (
                        delivered_at,
                        delivered_at,
                        str(row["message_id"]),
                        participant_id,
                    ),
                ).rowcount
                if not updated:
                    continue
                conn.execute(
                    """
                    INSERT INTO receipts
                        (message_id, participant_id, state, delivered_at)
                    VALUES (?, ?, 'delivered', ?)
                    ON CONFLICT(message_id, participant_id) DO UPDATE SET
                        state = CASE
                            WHEN receipts.state = 'acked' THEN 'acked'
                            ELSE 'delivered'
                        END,
                        delivered_at = COALESCE(receipts.delivered_at, excluded.delivered_at)
                    """,
                    (str(row["message_id"]), participant_id, delivered_at),
                )
                delivered = conn.execute(
                    """
                    SELECT message.*,
                           delivery.state AS delivery_state,
                           delivery.reasons_json AS delivery_reasons_json,
                           delivery.priority AS delivery_priority,
                           delivery.actionable AS delivery_actionable,
                           delivery.first_delivered_at
                               AS delivery_first_delivered_at,
                           delivery.last_delivered_at
                               AS delivery_last_delivered_at,
                           delivery.acked_at AS delivery_acked_at,
                           delivery.delivery_stage AS delivery_stage,
                           delivery.native_session_id AS delivery_native_session_id,
                           delivery.native_event_id AS delivery_native_event_id,
                           delivery.native_injected_at AS delivery_native_injected_at,
                           delivery.native_applied_at AS delivery_native_applied_at,
                           delivery.native_replied_at AS delivery_native_replied_at,
                           delivery.shadow_seen_at AS delivery_shadow_seen_at,
                           delivery.attempt_count AS delivery_attempt_count
                    FROM messages AS message
                    JOIN message_deliveries AS delivery
                      ON delivery.message_id = message.message_id
                    WHERE message.message_id = ?
                      AND delivery.participant_id = ?
                    """,
                    (str(row["message_id"]), participant_id),
                ).fetchone()
                if delivered is not None:
                    delivered_rows.append(delivered)
        with self._connection() as conn:
            return [
                self._message_payload(
                    row,
                    authorization=self._chat_authorization_for_message_locked(
                        conn,
                        message_id=str(row["message_id"]),
                        recipient_participant_id=participant_id,
                    ),
                )
                for row in delivered_rows
            ]

    def _apply_room_wake_policies(
        self,
        *,
        participant_id: str,
        conversations: list[dict[str, Any]],
        conversation_id: str | None,
        count_key: str,
        now: float,
        native_unassigned_only: bool = False,
    ) -> None:
        """Promote optional room activity to a wake without requiring reply."""

        by_room = {
            str(item["conversation_id"]): item for item in conversations
        }
        candidate_rooms = (
            [conversation_id]
            if conversation_id is not None
            else list(by_room)
        )
        if not candidate_rooms:
            return
        with self._connection() as conn:
            for room_id in candidate_rooms:
                policy_row = conn.execute(
                    "SELECT * FROM room_wake_policies WHERE conversation_id = ?",
                    (room_id,),
                ).fetchone()
                policy = self._room_wake_policy_payload(
                    policy_row,
                    conversation_id=room_id,
                )
                mode = str(policy["mode"])
                item = by_room.get(room_id)
                dnd_row = conn.execute(
                    "SELECT enabled_at, expires_at, timezone_name "
                    "FROM agent_room_dnd "
                    "WHERE participant_id = ? AND conversation_id = ?",
                    (participant_id, room_id),
                ).fetchone()
                dnd_active = bool(
                    dnd_row is not None and float(dnd_row["expires_at"]) > now
                )
                threshold_reset_at = (
                    float(dnd_row["expires_at"])
                    if dnd_row is not None and not dnd_active
                    else None
                )
                promote = bool(
                    mode == "all"
                    and item is not None
                    and int(item.get("policy_eligible_count") or 0) > 0
                )
                digest_pending_count = 0
                digest_oldest_created_at: float | None = None
                if mode == "digest" and not dnd_active:
                    metrics = conn.execute(
                        """
                        SELECT COUNT(*) AS pending_count,
                               MIN(message.sequence) AS oldest_sequence,
                               MAX(message.sequence) AS newest_sequence,
                               MIN(message.created_at) AS oldest_created_at,
                               MAX(message.created_at) AS newest_created_at
                        FROM message_deliveries AS delivery
                        JOIN messages AS message
                          ON message.message_id = delivery.message_id
                        JOIN memberships AS membership
                          ON membership.conversation_id = message.conversation_id
                         AND membership.participant_id = delivery.participant_id
                         AND membership.active = 1
                        WHERE delivery.participant_id = ?
                          AND message.conversation_id = ?
                          AND delivery.state IN ('pending', 'delivered')
                          AND message.sender_participant_id != ?
                          AND message.notification_mode = 'ordinary'
                          AND message.created_at >= ?
                          AND (? = 0 OR delivery.native_event_id IS NULL)
                          AND instr(
                              delivery.reasons_json,
                              '"echo_suppressed"'
                          ) = 0
                        """,
                        (
                            participant_id,
                            room_id,
                            participant_id,
                            threshold_reset_at or 0.0,
                            1 if native_unassigned_only else 0,
                        ),
                    ).fetchone()
                    digest_pending_count = int(metrics["pending_count"] or 0)
                    digest_oldest_created_at = (
                        float(metrics["oldest_created_at"])
                        if metrics["oldest_created_at"] is not None
                        else None
                    )
                    promote = digest_pending_count > 0 and (
                        digest_pending_count >= int(policy["digest_min_messages"])
                        or (
                            digest_oldest_created_at is not None
                            and digest_oldest_created_at
                            <= now - float(policy["digest_after_seconds"])
                        )
                    )
                    if promote and item is None:
                        item = {
                            "conversation_id": room_id,
                            count_key: digest_pending_count,
                            "oldest_sequence": int(metrics["oldest_sequence"]),
                            "newest_sequence": int(metrics["newest_sequence"]),
                            "priority_counts": {
                                "mention": 0,
                                "important": 0,
                                "normal": digest_pending_count,
                            },
                            "required_reply_count": 0,
                            "policy_eligible_count": digest_pending_count,
                        }
                        if count_key == "pending_count":
                            item["oldest_created_at"] = digest_oldest_created_at
                            item["newest_created_at"] = float(
                                metrics["newest_created_at"]
                            )
                        conversations.append(item)
                        by_room[room_id] = item
                if item is not None:
                    item["wake_policy"] = policy
                    item["policy_promoted"] = promote
                    item["digest_pending_count"] = digest_pending_count
                    item["digest_oldest_created_at"] = digest_oldest_created_at
                    item["dnd"] = {
                        "active": dnd_active,
                        "expires_at": (
                            float(dnd_row["expires_at"])
                            if dnd_row is not None
                            else None
                        ),
                        "timezone": (
                            str(dnd_row["timezone_name"])
                            if dnd_row is not None
                            else self.business_timezone_name
                        ),
                        "threshold_reset_at": threshold_reset_at,
                    }
                    if promote:
                        # Wake a mention-only worker; required replies remain
                        # unchanged so every Agent may still choose silence.
                        item["priority_counts"]["mention"] = max(
                            1,
                            int(item["priority_counts"]["mention"]),
                        )

    def _pending_manifest(
        self,
        participant_id: str,
        *,
        after_sequence: int | None = None,
        conversation_id: str | None = None,
        native_unassigned_only: bool = False,
    ) -> dict[str, Any]:
        now = time.time()
        sequence_clause = ""
        parameters: list[Any] = [participant_id, participant_id]
        if after_sequence is not None:
            sequence_clause = "AND message.sequence > ?"
            parameters.append(max(0, int(after_sequence)))
        room_clause = ""
        if conversation_id is not None:
            room_clause = "AND message.conversation_id = ?"
            parameters.append(conversation_id)
        parameters.extend((participant_id, now))
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT message.conversation_id,
                       COUNT(*) AS pending_count,
                       MIN(message.sequence) AS oldest_sequence,
                       MAX(message.sequence) AS newest_sequence,
                       MIN(message.created_at) AS oldest_created_at,
                       MAX(message.created_at) AS newest_created_at,
                       SUM(CASE WHEN delivery.priority IN ('direct', 'mention')
                                THEN 1 ELSE 0 END) AS mention_count,
                       SUM(CASE WHEN instr(delivery.reasons_json, '"mention"') > 0
                                      OR instr(
                                          delivery.reasons_json,
                                          '"agent_request"'
                                      ) > 0
                                THEN 1 ELSE 0 END) AS required_reply_count,
                       SUM(CASE WHEN delivery.priority = 'important' THEN 1 ELSE 0 END)
                           AS important_count,
                       SUM(CASE WHEN delivery.priority = 'normal' THEN 1 ELSE 0 END)
                           AS normal_count,
                       SUM(CASE
                               WHEN instr(
                                   delivery.reasons_json,
                                   '"echo_suppressed"'
                               ) = 0
                               THEN 1 ELSE 0
                           END) AS policy_eligible_count
                FROM message_deliveries AS delivery
                JOIN messages AS message
                  ON message.message_id = delivery.message_id
                JOIN memberships AS membership
                  ON membership.conversation_id = message.conversation_id
                 AND membership.participant_id = delivery.participant_id
                 AND membership.active = 1
                JOIN rooms AS room
                  ON room.conversation_id = message.conversation_id
                 AND room.status = 'active'
                WHERE delivery.participant_id = ?
                  AND delivery.state IN ('pending', 'delivered')
                  AND (? = 0 OR delivery.native_event_id IS NULL)
                  AND message.sender_participant_id != ?
                  {sequence_clause}
                  {room_clause}
                  AND (
                      delivery.actionable = 0
                      OR message.claimed_by IS NULL
                      OR message.claimed_by = ''
                      OR message.claimed_by = ?
                      OR COALESCE(message.claim_until, 0) <= ?
                  )
                GROUP BY message.conversation_id
                ORDER BY oldest_sequence
                """,
                [
                    parameters[0],
                    1 if native_unassigned_only else 0,
                    *parameters[1:],
                ],
            ).fetchall()
        conversations = [
            {
                "conversation_id": str(row["conversation_id"]),
                "pending_count": int(row["pending_count"]),
                "oldest_sequence": int(row["oldest_sequence"]),
                "newest_sequence": int(row["newest_sequence"]),
                "oldest_created_at": float(row["oldest_created_at"]),
                "newest_created_at": float(row["newest_created_at"]),
                "priority_counts": {
                    "mention": int(row["mention_count"] or 0),
                    "important": int(row["important_count"] or 0),
                    "normal": int(row["normal_count"] or 0),
                },
                "required_reply_count": int(row["required_reply_count"] or 0),
                "policy_eligible_count": int(
                    row["policy_eligible_count"] or 0
                ),
            }
            for row in rows
        ]
        self._apply_room_wake_policies(
            participant_id=participant_id,
            conversations=conversations,
            conversation_id=conversation_id,
            count_key="pending_count",
            now=now,
            native_unassigned_only=native_unassigned_only,
        )
        priority_counts = {
            priority: sum(
                int(item["priority_counts"][priority]) for item in conversations
            )
            for priority in ("mention", "important", "normal")
        }
        pending_count = sum(int(item["pending_count"]) for item in conversations)
        required_reply_count = sum(
            int(item["required_reply_count"]) for item in conversations
        )
        return {
            "pending_count": pending_count,
            "required_reply_count": required_reply_count,
            "priority_counts": priority_counts,
            "oldest_sequence": (
                min(item["oldest_sequence"] for item in conversations)
                if conversations
                else None
            ),
            "newest_sequence": (
                max(item["newest_sequence"] for item in conversations)
                if conversations
                else None
            ),
            "conversations": conversations,
        }

    def _activity_manifest(
        self,
        participant_id: str,
        *,
        after_sequence: int,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        """Summarize visible room activity independently from unread state."""
        room_clause = ""
        parameters: list[Any] = [
            participant_id,
            participant_id,
            max(0, int(after_sequence)),
        ]
        if conversation_id is not None:
            room_clause = "AND message.conversation_id = ?"
            parameters.append(conversation_id)
        now = time.time()
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT message.conversation_id,
                       COUNT(*) AS activity_count,
                       MIN(message.sequence) AS oldest_sequence,
                       MAX(message.sequence) AS newest_sequence,
                       SUM(CASE WHEN delivery.priority IN ('direct', 'mention')
                                THEN 1 ELSE 0 END) AS mention_count,
                       SUM(CASE WHEN instr(delivery.reasons_json, '"mention"') > 0
                                      OR instr(
                                          delivery.reasons_json,
                                          '"agent_request"'
                                      ) > 0
                                THEN 1 ELSE 0 END) AS required_reply_count,
                       SUM(CASE WHEN delivery.priority = 'important' THEN 1 ELSE 0 END)
                           AS important_count,
                       SUM(CASE WHEN delivery.priority = 'normal' THEN 1 ELSE 0 END)
                           AS normal_count,
                       SUM(CASE
                               WHEN instr(
                                   delivery.reasons_json,
                                   '"echo_suppressed"'
                               ) = 0
                               THEN 1 ELSE 0
                           END) AS policy_eligible_count
                FROM message_deliveries AS delivery
                JOIN messages AS message
                  ON message.message_id = delivery.message_id
                JOIN memberships AS membership
                  ON membership.conversation_id = message.conversation_id
                 AND membership.participant_id = delivery.participant_id
                 AND membership.active = 1
                JOIN rooms AS room
                  ON room.conversation_id = message.conversation_id
                 AND room.status = 'active'
                WHERE delivery.participant_id = ?
                  AND delivery.state != 'cancelled'
                  AND message.sender_participant_id != ?
                  AND message.sequence > ?
                  {room_clause}
                GROUP BY message.conversation_id
                ORDER BY oldest_sequence
                """,
                parameters,
            ).fetchall()
        conversations = [
            {
                "conversation_id": str(row["conversation_id"]),
                "activity_count": int(row["activity_count"]),
                "oldest_sequence": int(row["oldest_sequence"]),
                "newest_sequence": int(row["newest_sequence"]),
                "priority_counts": {
                    "mention": int(row["mention_count"] or 0),
                    "important": int(row["important_count"] or 0),
                    "normal": int(row["normal_count"] or 0),
                },
                "required_reply_count": int(row["required_reply_count"] or 0),
                "policy_eligible_count": int(
                    row["policy_eligible_count"] or 0
                ),
            }
            for row in rows
        ]
        self._apply_room_wake_policies(
            participant_id=participant_id,
            conversations=conversations,
            conversation_id=conversation_id,
            count_key="activity_count",
            now=now,
        )
        priority_counts = {
            priority: sum(
                int(item["priority_counts"][priority]) for item in conversations
            )
            for priority in ("mention", "important", "normal")
        }
        return {
            "activity_count": sum(
                int(item["activity_count"]) for item in conversations
            ),
            "required_reply_count": sum(
                int(item["required_reply_count"]) for item in conversations
            ),
            "priority_counts": priority_counts,
            "oldest_sequence": (
                min(item["oldest_sequence"] for item in conversations)
                if conversations
                else None
            ),
            "newest_sequence": (
                max(item["newest_sequence"] for item in conversations)
                if conversations
                else None
            ),
            "conversations": conversations,
        }

    def _claim(
        self,
        participant_id: str,
        message_id: str,
        *,
        lease_seconds: float,
        authorized_session_id: str | None = None,
    ) -> dict[str, Any]:
        participant = opaque_id(participant_id, field="participant_id")
        message = opaque_id(message_id, field="message_id")
        lease = max(5.0, min(float(lease_seconds), 3_600.0))
        now = time.time()
        claim_until = now + lease
        with self._transaction() as conn:
            self._archive_stale_rooms_locked(conn, now=now)
            if authorized_session_id is not None:
                self._require_live_session(
                    conn,
                    session_id=opaque_id(
                        authorized_session_id,
                        field="authorized_session_id",
                    ),
                    participant_id=participant,
                    now=now,
                )
            row = conn.execute(
                "SELECT * FROM messages WHERE message_id = ?",
                (message,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"unknown message: {message}")
            if authorized_session_id is not None:
                live_session = self._require_live_room_session(
                    conn,
                    session_id=opaque_id(
                        authorized_session_id,
                        field="authorized_session_id",
                    ),
                    participant_id=participant,
                    conversation_id=str(row["conversation_id"]),
                    now=now,
                )
                self._require_session_write_authority_locked(
                    conn,
                    session=live_session,
                )
            delivery = self._require_eligible_row(conn, participant, row)
            if str(row["audience_kind"]) not in {"participant", "role"}:
                raise ConflictError(
                    "room and broadcast messages use per-participant receipts"
                )
            if not bool(delivery["actionable"]):
                raise ConflictError(
                    "this participant may read and acknowledge the group message "
                    "but is not an actionable @ recipient"
                )
            existing_claim = str(row["claimed_by"] or "")
            existing_until = float(row["claim_until"] or 0.0)
            if existing_claim and existing_claim != participant and existing_until > now:
                raise ConflictError(
                    f"message is claimed by {existing_claim} until {existing_until}"
                )
            conn.execute(
                "UPDATE messages SET claimed_by = ?, claim_until = ?, updated_at = ? "
                "WHERE message_id = ?",
                (participant, claim_until, now, message),
            )
        return {
            "message_id": message,
            "action": "claim",
            "claimed_by": participant,
            "claim_until": claim_until,
        }

    def _release(
        self,
        participant_id: str,
        message_id: str,
        *,
        authorized_session_id: str | None = None,
    ) -> dict[str, Any]:
        participant = opaque_id(participant_id, field="participant_id")
        message = opaque_id(message_id, field="message_id")
        with self._transaction() as conn:
            now = time.time()
            self._archive_stale_rooms_locked(conn, now=now)
            if authorized_session_id is not None:
                self._require_live_session(
                    conn,
                    session_id=opaque_id(
                        authorized_session_id,
                        field="authorized_session_id",
                    ),
                    participant_id=participant,
                    now=now,
                )
            row = conn.execute(
                "SELECT * FROM messages WHERE message_id = ?",
                (message,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"unknown message: {message}")
            if authorized_session_id is not None:
                live_session = self._require_live_room_session(
                    conn,
                    session_id=opaque_id(
                        authorized_session_id,
                        field="authorized_session_id",
                    ),
                    participant_id=participant,
                    conversation_id=str(row["conversation_id"]),
                    now=now,
                )
                self._require_session_write_authority_locked(
                    conn,
                    session=live_session,
                )
            self._require_active_room(conn, str(row["conversation_id"]))
            if str(row["claimed_by"] or "") != participant:
                raise ConflictError("only the current claimant can release a message")
            conn.execute(
                "UPDATE messages SET claimed_by = NULL, claim_until = NULL, "
                "updated_at = ? WHERE message_id = ?",
                (time.time(), message),
            )
        return {"message_id": message, "action": "release", "released": True}

    def _ack(
        self,
        participant_id: str,
        message_id: str,
        *,
        authorized_session_id: str | None = None,
    ) -> dict[str, Any]:
        participant = opaque_id(participant_id, field="participant_id")
        message = opaque_id(message_id, field="message_id")
        now = time.time()
        with self._transaction() as conn:
            self._archive_stale_rooms_locked(conn, now=now)
            if authorized_session_id is not None:
                self._require_live_session(
                    conn,
                    session_id=opaque_id(
                        authorized_session_id,
                        field="authorized_session_id",
                    ),
                    participant_id=participant,
                    now=now,
                )
            row = conn.execute(
                "SELECT * FROM messages WHERE message_id = ?",
                (message,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"unknown message: {message}")
            if authorized_session_id is not None:
                live_session = self._require_live_room_session(
                    conn,
                    session_id=opaque_id(
                        authorized_session_id,
                        field="authorized_session_id",
                    ),
                    participant_id=participant,
                    conversation_id=str(row["conversation_id"]),
                    now=now,
                )
                self._require_session_write_authority_locked(
                    conn,
                    session=live_session,
                )
            delivery = self._require_eligible_row(conn, participant, row)
            actionable = bool(delivery["actionable"])
            claimed_by = str(row["claimed_by"] or "")
            claim_until = float(row["claim_until"] or 0.0)
            if (
                actionable
                and claimed_by
                and claimed_by != participant
                and claim_until > now
            ):
                raise ConflictError("message is currently claimed by another participant")
            conn.execute(
                """
                UPDATE message_deliveries
                SET state = 'acked',
                    delivery_stage = CASE
                        WHEN native_replied_at IS NOT NULL THEN 'replied'
                        WHEN native_applied_at IS NOT NULL THEN 'native_applied'
                        ELSE 'legacy_acked'
                    END,
                    first_delivered_at = COALESCE(first_delivered_at, ?),
                    last_delivered_at = COALESCE(last_delivered_at, ?),
                    acked_at = ?
                WHERE message_id = ? AND participant_id = ?
                  AND state != 'cancelled'
                """,
                (now, now, now, message, participant),
            )
            conn.execute(
                """
                INSERT INTO receipts
                    (message_id, participant_id, state, delivered_at, acked_at)
                VALUES (?, ?, 'acked', ?, ?)
                ON CONFLICT(message_id, participant_id) DO UPDATE SET
                    state = 'acked',
                    delivered_at = COALESCE(receipts.delivered_at, excluded.delivered_at),
                    acked_at = excluded.acked_at
                """,
                (message, participant, now, now),
            )
            globally_resolved = actionable and str(row["audience_kind"]) in {
                "participant",
                "role",
            }
            if globally_resolved:
                conn.execute(
                    "UPDATE messages SET status = 'acked', updated_at = ? "
                    "WHERE message_id = ?",
                    (now, message),
                )
                conn.execute(
                    "UPDATE message_deliveries SET actionable = 0 "
                    "WHERE message_id = ? AND actionable = 1",
                    (message,),
                )
        return {
            "message_id": message,
            "action": "ack",
            "acked_by": participant,
            "acked_at": now,
        }

    def _require_eligible_participant(self, participant_id: str, message_id: str) -> None:
        with self._transaction() as conn:
            self._archive_stale_rooms_locked(conn, now=time.time())
            row = conn.execute(
                "SELECT * FROM messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"unknown message: {message_id}")
            self._require_eligible_row(conn, participant_id, row)

    def _require_eligible_row(
        self,
        conn: sqlite3.Connection,
        participant_id: str,
        row: sqlite3.Row,
    ) -> sqlite3.Row:
        self._require_membership(
            conn,
            participant_id,
            str(row["conversation_id"]),
        )
        delivery = conn.execute(
            "SELECT state, actionable, priority FROM message_deliveries "
            "WHERE message_id = ? AND participant_id = ?",
            (str(row["message_id"]), participant_id),
        ).fetchone()
        if delivery is None or str(delivery["state"]) == "cancelled":
            raise ConflictError("participant is not an eligible recipient")
        return delivery

    def _delivery_is_actionable(
        self,
        participant_id: str,
        message_id: str,
    ) -> bool:
        with self._connection() as conn:
            delivery = conn.execute(
                "SELECT actionable FROM message_deliveries "
                "WHERE message_id = ? AND participant_id = ? "
                "AND state != 'cancelled'",
                (message_id, participant_id),
            ).fetchone()
        return bool(delivery is not None and delivery["actionable"])
