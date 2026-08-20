from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Sequence
from typing import Any

from .store import CONNECTOR_ONLINE_WINDOW_SECONDS
from .validation import conversation_id as validate_conversation_id


class ViewerActivityQueries:
    """Read-only pending-work, event, and participant projections."""

    def pending_response_center(
        self,
        *,
        participant_id: str,
        visible_conversation_ids: Sequence[str] | None,
        managed_conversation_ids: Sequence[str] | None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Project unresolved required replies and active room tasks.

        Delivery reasons are the authority for whether a chat reply is required.
        A linked reply from the exact target also resolves the projection for Web
        users, whose browser does not consume the Agent delivery queue.
        """

        participant = str(participant_id or "").strip()
        if not participant:
            raise ValueError("participant_id is required")
        normalized_limit = max(1, min(int(limit), 200))
        visible = (
            None
            if visible_conversation_ids is None
            else list(
                dict.fromkeys(
                    validate_conversation_id(value)
                    for value in visible_conversation_ids
                )
            )
        )
        if visible == []:
            return {
                "pending_responses": [],
                "active_tasks": [],
                "counts": {
                    "pending_responses": 0,
                    "incoming": 0,
                    "outgoing": 0,
                    "oversight": 0,
                    "active_tasks": 0,
                    "needs_input_tasks": 0,
                    "needs_me": 0,
                    "waiting_other": 0,
                    "informational": 0,
                    "attention_total": 0,
                    "total": 0,
                },
                "has_more": False,
            }
        managed = (
            None
            if managed_conversation_ids is None
            else list(
                dict.fromkeys(
                    validate_conversation_id(value)
                    for value in managed_conversation_ids
                )
            )
        )

        room_clauses: list[str] = []
        room_parameters: list[Any] = []
        if visible is not None:
            placeholders = ",".join("?" for _ in visible)
            room_clauses.append(f"message.conversation_id IN ({placeholders})")
            room_parameters.extend(visible)

        access_clauses = [
            "delivery.participant_id = ?",
            "message.sender_participant_id = ?",
        ]
        access_parameters: list[Any] = [participant, participant]
        if managed is None:
            access_clauses.append("1 = 1")
        elif managed:
            placeholders = ",".join("?" for _ in managed)
            access_clauses.append(f"message.conversation_id IN ({placeholders})")
            access_parameters.extend(managed)

        response_where = [
            "delivery.state IN ('pending', 'delivered')",
            "(instr(delivery.reasons_json, '\"mention\"') > 0 "
            "OR instr(delivery.reasons_json, '\"agent_request\"') > 0)",
            "NOT EXISTS ("
            "SELECT 1 FROM messages AS exact_reply "
            "WHERE exact_reply.reply_to = message.message_id "
            "AND exact_reply.sender_participant_id = delivery.participant_id"
            ")",
            f"({' OR '.join(access_clauses)})",
            *room_clauses,
        ]
        response_parameters = [*access_parameters, *room_parameters, normalized_limit]

        task_room_clauses: list[str] = []
        task_room_parameters: list[Any] = []
        if visible is not None:
            placeholders = ",".join("?" for _ in visible)
            task_room_clauses.append(f"task.conversation_id IN ({placeholders})")
            task_room_parameters.extend(visible)
        task_access_clauses = ["task.issuer_participant_id = ?"]
        task_access_parameters: list[Any] = [participant]
        if managed is None:
            task_access_clauses.append("1 = 1")
        elif managed:
            placeholders = ",".join("?" for _ in managed)
            task_access_clauses.append(f"task.conversation_id IN ({placeholders})")
            task_access_parameters.extend(managed)
        task_where = [
            "task.status IN ('queued', 'claimed', 'running', 'needs_input')",
            f"({' OR '.join(task_access_clauses)})",
            *task_room_clauses,
        ]
        task_parameters = [
            *task_access_parameters,
            *task_room_parameters,
            normalized_limit,
        ]

        with self._connection() as connection:
            response_rows = connection.execute(
                f"""
                SELECT message.message_id, message.conversation_id,
                       message.sequence, message.room_sequence,
                       message.body, message.created_at,
                       message.sender_participant_id,
                       sender.client_type AS sender_client_type,
                       sender.display_name AS sender_display_name,
                       sender.avatar_key AS sender_avatar_key,
                       delivery.participant_id AS target_participant_id,
                       target.client_type AS target_client_type,
                       target.display_name AS target_display_name,
                       target.avatar_key AS target_avatar_key,
                       delivery.state AS delivery_state,
                       delivery.delivery_stage,
                       delivery.reasons_json,
                       delivery.first_delivered_at,
                       delivery.last_delivered_at,
                       COUNT(*) OVER() AS total_count
                FROM message_deliveries AS delivery
                JOIN messages AS message
                  ON message.message_id = delivery.message_id
                JOIN participants AS sender
                  ON sender.participant_id = message.sender_participant_id
                JOIN participants AS target
                  ON target.participant_id = delivery.participant_id
                JOIN memberships AS target_membership
                  ON target_membership.conversation_id = message.conversation_id
                 AND target_membership.participant_id = delivery.participant_id
                 AND target_membership.active = 1
                JOIN rooms AS room
                  ON room.conversation_id = message.conversation_id
                 AND room.status = 'active'
                WHERE {" AND ".join(response_where)}
                ORDER BY CASE
                             WHEN delivery.participant_id = ? THEN 0
                             WHEN message.sender_participant_id = ? THEN 1
                             ELSE 2
                         END,
                         message.created_at, message.sequence,
                         delivery.participant_id
                LIMIT ?
                """,
                [
                    *response_parameters[:-1],
                    participant,
                    participant,
                    response_parameters[-1],
                ],
            ).fetchall()
            task_rows = connection.execute(
                f"""
                SELECT task.*, source_message.room_sequence
                                   AS source_room_sequence,
                       issuer.client_type AS issuer_client_type,
                       issuer.display_name AS issuer_display_name,
                       claimant.client_type AS claimant_client_type,
                       claimant.display_name AS claimant_display_name,
                       COUNT(*) OVER() AS total_count
                FROM room_tasks AS task
                JOIN rooms AS room
                  ON room.conversation_id = task.conversation_id
                 AND room.status = 'active'
                JOIN participants AS issuer
                  ON issuer.participant_id = task.issuer_participant_id
                LEFT JOIN participants AS claimant
                  ON claimant.participant_id = task.claimed_by_participant_id
                LEFT JOIN messages AS source_message
                  ON source_message.message_id = task.source_message_id
                WHERE {" AND ".join(task_where)}
                ORDER BY CASE task.status
                             WHEN 'needs_input' THEN 0
                             WHEN 'running' THEN 1
                             WHEN 'claimed' THEN 2
                             ELSE 3
                         END,
                         task.updated_at, task.created_at
                LIMIT ?
                """,
                task_parameters,
            ).fetchall()

        now = time.time()
        response_items: list[dict[str, Any]] = []
        direction_counts = {"incoming": 0, "outgoing": 0, "oversight": 0}
        for row in response_rows:
            sender_id = str(row["sender_participant_id"])
            target_id = str(row["target_participant_id"])
            if target_id == participant:
                direction = "incoming"
                attention_kind = "needs_me"
            elif sender_id == participant:
                direction = "outgoing"
                attention_kind = "waiting_other"
            else:
                direction = "oversight"
                attention_kind = "informational"
            direction_counts[direction] += 1
            body = str(row["body"])
            response_items.append(
                {
                    "message_id": str(row["message_id"]),
                    "conversation_id": str(row["conversation_id"]),
                    "sequence": int(row["sequence"]),
                    "room_sequence": int(row["room_sequence"] or row["sequence"]),
                    "body_preview": body[:500],
                    "body_truncated": len(body) > 500,
                    "created_at": float(row["created_at"]),
                    "age_seconds": max(0.0, now - float(row["created_at"])),
                    "direction": direction,
                    "attention_kind": attention_kind,
                    "sender": {
                        "participant_id": sender_id,
                        "client_type": str(row["sender_client_type"]),
                        "display_name": str(row["sender_display_name"]),
                        "avatar_key": str(row["sender_avatar_key"] or "auto"),
                    },
                    "target": {
                        "participant_id": target_id,
                        "client_type": str(row["target_client_type"]),
                        "display_name": str(row["target_display_name"]),
                        "avatar_key": str(row["target_avatar_key"] or "auto"),
                    },
                    "delivery_state": str(row["delivery_state"]),
                    "delivery_stage": str(row["delivery_stage"] or "queued"),
                    "delivery_reasons": json.loads(str(row["reasons_json"] or "[]")),
                    "first_delivered_at": (
                        float(row["first_delivered_at"])
                        if row["first_delivered_at"] is not None
                        else None
                    ),
                    "last_delivered_at": (
                        float(row["last_delivered_at"])
                        if row["last_delivered_at"] is not None
                        else None
                    ),
                }
            )

        task_items: list[dict[str, Any]] = []
        for row in task_rows:
            body = str(row["body"])
            task_items.append(
                {
                    "task_id": str(row["task_id"]),
                    "conversation_id": str(row["conversation_id"]),
                    "source_message_id": (
                        str(row["source_message_id"])
                        if row["source_message_id"] is not None
                        else None
                    ),
                    "source_sequence": (
                        int(row["source_sequence"])
                        if row["source_sequence"] is not None
                        else None
                    ),
                    "source_room_sequence": (
                        int(row["source_room_sequence"])
                        if row["source_room_sequence"] is not None
                        else None
                    ),
                    "body_preview": body[:500],
                    "body_truncated": len(body) > 500,
                    "status": str(row["status"]),
                    "issuer_participant_id": str(row["issuer_participant_id"]),
                    "issuer_display_name": str(row["issuer_display_name"]),
                    "issuer_client_type": str(row["issuer_client_type"]),
                    "claimed_by_participant_id": (
                        str(row["claimed_by_participant_id"])
                        if row["claimed_by_participant_id"] is not None
                        else None
                    ),
                    "claimant_display_name": str(row["claimant_display_name"] or ""),
                    "claimant_client_type": str(row["claimant_client_type"] or ""),
                    "created_at": float(row["created_at"]),
                    "updated_at": float(row["updated_at"]),
                    "age_seconds": max(0.0, now - float(row["created_at"])),
                    "attention_kind": (
                        "needs_me"
                        if str(row["issuer_participant_id"]) == participant
                        and str(row["status"]) == "needs_input"
                        else "waiting_other"
                        if str(row["issuer_participant_id"]) == participant
                        else "informational"
                    ),
                }
            )

        response_total = int(response_rows[0]["total_count"]) if response_rows else 0
        task_total = int(task_rows[0]["total_count"]) if task_rows else 0
        # Direction counts above cover the bounded page.  Count exact totals
        # when a page was truncated so the top-bar badge never understates work.
        if response_total > len(response_rows):
            with self._connection() as connection:
                grouped = connection.execute(
                    f"""
                    SELECT CASE
                               WHEN delivery.participant_id = ? THEN 'incoming'
                               WHEN message.sender_participant_id = ? THEN 'outgoing'
                               ELSE 'oversight'
                           END AS direction,
                           COUNT(*) AS count
                    FROM message_deliveries AS delivery
                    JOIN messages AS message
                      ON message.message_id = delivery.message_id
                    JOIN memberships AS target_membership
                      ON target_membership.conversation_id = message.conversation_id
                     AND target_membership.participant_id = delivery.participant_id
                     AND target_membership.active = 1
                    JOIN rooms AS room
                      ON room.conversation_id = message.conversation_id
                     AND room.status = 'active'
                    WHERE {" AND ".join(response_where)}
                    GROUP BY direction
                    """,
                    [participant, participant, *access_parameters, *room_parameters],
                ).fetchall()
            direction_counts = {
                "incoming": 0,
                "outgoing": 0,
                "oversight": 0,
            }
            for row in grouped:
                direction_counts[str(row["direction"])] = int(row["count"])

        needs_input_tasks = sum(
            1 for item in task_items if item["status"] == "needs_input"
        )
        if task_total > len(task_rows):
            with self._connection() as connection:
                needs_input_tasks = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM room_tasks AS task "
                        f"JOIN rooms AS room ON room.conversation_id = "
                        f"task.conversation_id AND room.status = 'active' "
                        f"WHERE {' AND '.join(task_where)} "
                        "AND task.status = 'needs_input'",
                        [*task_access_parameters, *task_room_parameters],
                    ).fetchone()[0]
                )

        response_attention_counts = {
            "needs_me": direction_counts["incoming"],
            "waiting_other": direction_counts["outgoing"],
            "informational": direction_counts["oversight"],
        }
        task_attention_counts = {
            "needs_me": 0,
            "waiting_other": 0,
            "informational": 0,
        }
        for item in task_items:
            task_attention_counts[str(item["attention_kind"])] += 1
        if task_total > len(task_items):
            # The bounded page cannot prove the category totals for omitted
            # tasks.  Count them directly so the top-level badge never hides
            # actionable work behind informational oversight rows.
            with self._connection() as connection:
                grouped_tasks = connection.execute(
                    f"""
                    SELECT CASE
                               WHEN task.issuer_participant_id = ?
                                AND task.status = 'needs_input' THEN 'needs_me'
                               WHEN task.issuer_participant_id = ?
                               THEN 'waiting_other'
                               ELSE 'informational'
                           END AS attention_kind,
                           COUNT(*) AS count
                    FROM room_tasks AS task
                    JOIN rooms AS room
                      ON room.conversation_id = task.conversation_id
                     AND room.status = 'active'
                    WHERE {" AND ".join(task_where)}
                    GROUP BY attention_kind
                    """,
                    [
                        participant,
                        participant,
                        *task_access_parameters,
                        *task_room_parameters,
                    ],
                ).fetchall()
            task_attention_counts = {
                "needs_me": 0,
                "waiting_other": 0,
                "informational": 0,
            }
            for row in grouped_tasks:
                task_attention_counts[str(row["attention_kind"])] = int(
                    row["count"]
                )

        attention_counts = {
            key: response_attention_counts[key] + task_attention_counts[key]
            for key in ("needs_me", "waiting_other", "informational")
        }

        return {
            "pending_responses": response_items,
            "active_tasks": task_items,
            "counts": {
                "pending_responses": response_total,
                **direction_counts,
                "active_tasks": task_total,
                "needs_input_tasks": needs_input_tasks,
                **attention_counts,
                "attention_total": (
                    attention_counts["needs_me"]
                    + attention_counts["waiting_other"]
                ),
                "total": response_total + task_total,
            },
            "has_more": (
                response_total > len(response_items) or task_total > len(task_items)
            ),
        }

    def event_snapshot(
        self,
        *,
        after_sequence: int = 0,
        visible_conversation_ids: Sequence[str] | None = None,
        include_admin_state: bool = True,
    ) -> dict[str, Any]:
        requested_cursor = max(0, int(after_sequence))
        visible = (
            None
            if visible_conversation_ids is None
            else {validate_conversation_id(value) for value in visible_conversation_ids}
        )
        now = time.time()
        with self._connection() as connection:
            global_sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) FROM messages"
                ).fetchone()[0]
            )
            cursor = min(requested_cursor, global_sequence)
            changed_rooms = [
                {
                    "conversation_id": str(row["conversation_id"]),
                    "message_count": int(row["message_count"]),
                    "first_sequence": int(row["first_sequence"]),
                    "last_sequence": int(row["last_sequence"]),
                }
                for row in connection.execute(
                    """
                    SELECT conversation_id, COUNT(*) AS message_count,
                           MIN(sequence) AS first_sequence,
                           MAX(sequence) AS last_sequence
                    FROM messages
                    WHERE sequence > ?
                    GROUP BY conversation_id
                    ORDER BY first_sequence
                    """,
                    (cursor,),
                ).fetchall()
                if visible is None or str(row["conversation_id"]) in visible
            ]
            if visible is None:
                visible_message_revision = global_sequence
            elif not visible:
                visible_message_revision = 0
            else:
                placeholders = ",".join("?" for _ in visible)
                visible_message_revision = int(
                    connection.execute(
                        f"SELECT COALESCE(MAX(sequence), 0) FROM messages "
                        f"WHERE conversation_id IN ({placeholders})",
                        sorted(visible),
                    ).fetchone()[0]
                )
            pending_nicknames = int(
                connection.execute(
                    "SELECT COUNT(*) FROM nickname_requests WHERE status = 'pending'"
                ).fetchone()[0]
            )
            nickname_revision = float(
                connection.execute(
                    "SELECT COALESCE(MAX(MAX(requested_at, "
                    "COALESCE(reviewed_at, 0))), 0) FROM nickname_requests"
                ).fetchone()[0]
            )
            participant_revision = float(
                connection.execute(
                    "SELECT COALESCE(MAX(profile_updated_at), 0) FROM participants"
                ).fetchone()[0]
            )
            membership_revision = float(
                connection.execute(
                    "SELECT COALESCE(MAX(updated_at), 0) FROM memberships"
                ).fetchone()[0]
            )
            online_revision = str(
                connection.execute(
                    "SELECT COALESCE(GROUP_CONCAT(participant_id, '|'), '') "
                    "FROM (SELECT participant_id FROM participants "
                    "WHERE status = 'online' AND last_seen >= ? "
                    "ORDER BY participant_id)",
                    (now - 90.0,),
                ).fetchone()[0]
            )
            active_session_revision = str(
                connection.execute(
                    "SELECT COALESCE(GROUP_CONCAT(session_key, '|'), '') FROM ("
                    "SELECT 'agent:' || session_id AS session_key "
                    "FROM agent_sessions WHERE cleared_at IS NULL "
                    "AND revoked_at IS NULL AND expires_at > ? "
                    "UNION ALL "
                    "SELECT 'web:' || web_session.session_id AS session_key "
                    "FROM web_sessions AS web_session "
                    "JOIN web_users AS web_user ON web_user.user_id = web_session.user_id "
                    "WHERE web_user.active = 1 AND web_session.revoked_at IS NULL "
                    "AND web_session.expires_at > ? "
                    "ORDER BY session_key)",
                    (now, now),
                ).fetchone()[0]
            )
            connector_revision = str(
                connection.execute(
                    "SELECT COALESCE(GROUP_CONCAT(connector_state, '|'), '') FROM ("
                    "SELECT 'invite:' || invitation_id || ':' || status || ':' || "
                    "CAST(use_count AS TEXT) || ':' || CAST(updated_at AS TEXT) "
                    "AS connector_state FROM agent_invitations "
                    "UNION ALL "
                    "SELECT 'connector:' || connector_id || ':' || conversation_id || ':' || "
                    "setup_status || ':' || "
                    "CASE WHEN connector_last_seen_at >= ? THEN 'online' "
                    "ELSE 'offline' END || ':' || "
                    "COALESCE(tui_state, 'unbound') || ':' || "
                    "CASE WHEN tui_last_seen_at >= ? THEN 'fresh' "
                    "ELSE 'stale' END || ':' || "
                    "COALESCE(tui_active_task_id, '') || ':' || "
                    "COALESCE(CAST(revoked_at AS TEXT), '') AS connector_state "
                    "FROM agent_connectors ORDER BY connector_state)",
                    (
                        now - CONNECTOR_ONLINE_WINDOW_SECONDS,
                        now - CONNECTOR_ONLINE_WINDOW_SECONDS,
                    ),
                ).fetchone()[0]
            )
            session_revocation_revision = float(
                connection.execute(
                    "SELECT COALESCE(MAX(COALESCE(revoked_at, 0)), 0) "
                    "FROM agent_sessions"
                ).fetchone()[0]
            )
            session_clear_revision = float(
                connection.execute(
                    "SELECT COALESCE(MAX(COALESCE(cleared_at, 0)), 0) "
                    "FROM agent_sessions"
                ).fetchone()[0]
            )
            room_revision = float(
                connection.execute(
                    "SELECT COALESCE(MAX(MAX(last_activity_at, "
                    "COALESCE(abandoned_at, 0))), 0) FROM rooms"
                ).fetchone()[0]
            )
            rate_revision = int(
                connection.execute(
                    "SELECT revision FROM message_rate_state WHERE singleton = 1"
                ).fetchone()[0]
            )
            web_user_permission_revision = float(
                connection.execute(
                    "SELECT MAX(revision) FROM ("
                    "SELECT COALESCE(MAX(updated_at), 0) AS revision FROM web_users "
                    "UNION ALL SELECT COALESCE(MAX(updated_at), 0) "
                    "FROM room_web_members)"
                ).fetchone()[0]
                or 0
            )
            task_revision = float(
                connection.execute(
                    "SELECT COALESCE(MAX(updated_at), 0) FROM room_tasks"
                ).fetchone()[0]
            )
            task_permission_revision = float(
                connection.execute(
                    "SELECT MAX(revision) FROM ("
                    "SELECT COALESCE(MAX(updated_at), 0) AS revision "
                    "FROM room_task_policies "
                    "UNION ALL SELECT COALESCE(MAX(updated_at), 0) "
                    "FROM room_task_grants)"
                ).fetchone()[0]
                or 0
            )
            receipt_revision = str(
                connection.execute(
                    """
                    SELECT CAST(delivery.delivery_count AS TEXT)
                        || ':' || CAST(delivery.latest_milestone AS TEXT)
                        || ':' || CAST(delivery.cancelled_count AS TEXT)
                        || ':' || CAST(receipt.receipt_count AS TEXT)
                        || ':' || CAST(receipt.latest_ack AS TEXT)
                        || ':' || CAST(dnd.dnd_count AS TEXT)
                        || ':' || CAST(dnd.active_dnd_count AS TEXT)
                        || ':' || CAST(dnd.latest_update AS TEXT)
                    FROM (
                        SELECT COUNT(*) AS delivery_count,
                               COALESCE(MAX(MAX(
                                created_at,
                                COALESCE(first_delivered_at, 0),
                                COALESCE(last_delivered_at, 0),
                                COALESCE(acked_at, 0),
                                COALESCE(native_injected_at, 0),
                                COALESCE(native_applied_at, 0),
                                COALESCE(native_replied_at, 0),
                                COALESCE(shadow_seen_at, 0)
                               )), 0) AS latest_milestone,
                               COALESCE(SUM(
                                   state = 'cancelled'
                                   OR delivery_stage = 'cancelled'
                               ), 0) AS cancelled_count
                        FROM message_deliveries
                    ) AS delivery
                    CROSS JOIN (
                        SELECT COUNT(*) AS receipt_count,
                               COALESCE(MAX(acked_at), 0) AS latest_ack
                        FROM receipts
                    ) AS receipt
                    CROSS JOIN (
                        SELECT COUNT(*) AS dnd_count,
                               COALESCE(SUM(expires_at > ?), 0)
                                   AS active_dnd_count,
                               COALESCE(MAX(updated_at), 0) AS latest_update
                        FROM agent_room_dnd
                    ) AS dnd
                    """,
                    (now,),
                ).fetchone()[0]
            )
            if visible is None:
                highlight_revision: object = str(
                    connection.execute(
                        "SELECT CAST(COUNT(*) AS TEXT) || ':' || "
                        "CAST(COALESCE(MAX(updated_at), 0) AS TEXT) "
                        "FROM room_message_markers"
                    ).fetchone()[0]
                )
            elif not visible:
                highlight_revision = "0:0"
            else:
                placeholders = ",".join("?" for _ in visible)
                highlight_revision = str(
                    connection.execute(
                        f"SELECT CAST(COUNT(*) AS TEXT) || ':' || "
                        f"CAST(COALESCE(MAX(updated_at), 0) AS TEXT) "
                        f"FROM room_message_markers "
                        f"WHERE conversation_id IN ({placeholders})",
                        sorted(visible),
                    ).fetchone()[0]
                )
            monitoring_revision = int(
                connection.execute(
                    "SELECT revision FROM operational_monitoring_state "
                    "WHERE singleton = 1"
                ).fetchone()[0]
            )
        if not include_admin_state:
            pending_nicknames = 0
            nickname_revision = 0.0

            def private_revision(label: str, value: object) -> str:
                encoded = json.dumps(
                    [label, value],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                return hashlib.sha256(encoded).hexdigest()[:20]

            participant_revision = private_revision(
                "participants",
                participant_revision,
            )
            membership_revision = private_revision(
                "memberships",
                membership_revision,
            )
            online_revision = private_revision("online", online_revision)
            active_session_revision = private_revision(
                "sessions",
                active_session_revision,
            )
            session_revocation_revision = private_revision(
                "session-revocations",
                session_revocation_revision,
            )
            session_clear_revision = private_revision(
                "session-clears",
                session_clear_revision,
            )
            room_revision = private_revision("rooms", room_revision)
            connector_revision = private_revision(
                "connectors",
                connector_revision,
            )
            web_user_permission_revision = private_revision(
                "permissions",
                web_user_permission_revision,
            )
            task_revision = private_revision("tasks", task_revision)
            task_permission_revision = private_revision(
                "task-permissions",
                task_permission_revision,
            )
            receipt_revision = private_revision("receipts", receipt_revision)
            highlight_revision = private_revision(
                "highlights",
                highlight_revision,
            )
            monitoring_revision = 0
            combined_task_revision: object = private_revision(
                "task-state",
                [task_revision, task_permission_revision],
            )
        else:
            combined_task_revision = max(
                task_revision,
                task_permission_revision,
            )
        state_revisions = {
            "messages": visible_message_revision,
            "nicknames": nickname_revision,
            "participants": participant_revision,
            "memberships": membership_revision,
            "online": online_revision,
            "sessions": [
                active_session_revision,
                session_revocation_revision,
                session_clear_revision,
            ],
            "rooms": room_revision,
            "connectors": connector_revision,
            "permissions": web_user_permission_revision,
            "tasks": task_revision,
            "task_permissions": task_permission_revision,
            "receipts": receipt_revision,
            "highlights": highlight_revision,
            "rates": rate_revision,
            "monitoring": monitoring_revision,
        }
        return {
            "cursor": max(cursor, global_sequence),
            "changed_rooms": changed_rooms,
            "pending_nickname_requests": pending_nicknames,
            # Keep the positional revision for older Web clients while giving
            # newer clients named facets they can refresh independently.
            "state_revisions": state_revisions,
            "state_revision": [
                visible_message_revision,
                nickname_revision,
                participant_revision,
                membership_revision,
                online_revision,
                active_session_revision,
                session_revocation_revision,
                session_clear_revision,
                room_revision,
                connector_revision,
                web_user_permission_revision,
                combined_task_revision,
                rate_revision,
                receipt_revision,
                highlight_revision,
                monitoring_revision,
            ],
            "server_time": now,
        }

    def participants(self, conversation_id: str) -> list[dict[str, Any]]:
        conversation = validate_conversation_id(conversation_id)
        now = time.time()
        online_after = now - 90.0
        connector_online_after = now - 75.0
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    p.*,
                    m.roles_json,
                    m.joined_at,
                    m.active AS membership_active,
                    room.status AS room_status,
                    connector.connector_id,
                    COALESCE(
                        invitation.tui_adapter_kind,
                        invitation.adapter_kind
                    ) AS connector_adapter_kind,
                    connector.setup_status AS connector_setup_status,
                    connector.connector_last_seen_at,
                    connector.tui_endpoint_id,
                    connector.tui_native_session_id,
                    connector.tui_state,
                    connector.tui_last_seen_at,
                    connector.tui_active_task_id,
                    connector.native_delivery_mode,
                    connector.native_lease_id,
                    connector.native_lease_expires_at,
                    CASE
                        WHEN lifecycle.participant_id IS NOT NULL THEN
                            MAX(
                                COALESCE(
                                    lifecycle.access_granted_at,
                                    m.joined_at
                                ),
                                COALESCE(
                                    lifecycle.last_spoke_at,
                                    lifecycle.access_granted_at,
                                    m.joined_at
                                )
                            ) + (
                                CASE
                                    WHEN lifecycle.last_spoke_at IS NULL
                                     AND NOT EXISTS (
                                         SELECT 1
                                         FROM agent_sessions AS life_session
                                         WHERE life_session.participant_id =
                                               p.participant_id
                                           AND life_session.cleared_at IS NULL
                                           AND life_session.revoked_at IS NULL
                                           AND life_session.expires_at > ?
                                     )
                                     AND NOT EXISTS (
                                         SELECT 1
                                         FROM agent_connectors AS life_connector
                                         WHERE life_connector.accepted_participant_id =
                                               p.participant_id
                                           AND life_connector.revoked_at IS NULL
                                           AND life_connector.setup_status = 'configured'
                                           AND COALESCE(
                                               life_connector.connector_last_seen_at,
                                               0
                                           ) >= ?
                                     )
                                    THEN policy.unactivated_inactivity_days
                                    ELSE policy.inactivity_days
                                END
                            ) * 86400.0
                        ELSE NULL
                    END AS inactivity_expires_at,
                    (
                        SELECT COUNT(*) FROM agent_sessions AS session
                        WHERE session.participant_id = p.participant_id
                          AND session.registered_conversation_id = m.conversation_id
                          AND session.cleared_at IS NULL
                          AND session.revoked_at IS NULL
                          AND session.expires_at > ?
                    ) AS active_agent_session_count,
                    (
                        SELECT COUNT(*) FROM agent_sessions AS session
                        WHERE session.participant_id = p.participant_id
                          AND session.registered_conversation_id = m.conversation_id
                          AND session.cleared_at IS NULL
                          AND session.revoked_at IS NULL
                          AND session.expires_at > ?
                          AND session.last_seen >= ?
                    ) AS online_agent_session_count,
                    (
                        SELECT COUNT(*)
                        FROM web_sessions AS web_session
                        JOIN web_users AS web_user
                          ON web_user.user_id = web_session.user_id
                        WHERE web_user.participant_id = p.participant_id
                          AND web_user.active = 1
                          AND web_session.revoked_at IS NULL
                          AND web_session.expires_at > ?
                    ) AS active_web_session_count,
                    (
                        SELECT COUNT(*)
                        FROM web_sessions AS web_session
                        JOIN web_users AS web_user
                          ON web_user.user_id = web_session.user_id
                        WHERE web_user.participant_id = p.participant_id
                          AND web_user.active = 1
                          AND web_session.revoked_at IS NULL
                          AND web_session.expires_at > ?
                          AND web_session.last_seen >= ?
                    ) AS online_web_session_count
                FROM memberships AS m
                JOIN participants AS p
                  ON p.participant_id = m.participant_id
                JOIN rooms AS room
                  ON room.conversation_id = m.conversation_id
                LEFT JOIN agent_connectors AS connector
                  ON connector.connector_id = (
                    SELECT recent.connector_id
                    FROM agent_connectors AS recent
                    JOIN agent_invitations AS recent_invitation
                      ON recent_invitation.invitation_id = recent.invitation_id
                    WHERE recent.accepted_participant_id = p.participant_id
                      AND recent.conversation_id = m.conversation_id
                      AND recent_invitation.status != 'revoked'
                      AND recent.revoked_at IS NULL
                    ORDER BY recent.updated_at DESC
                    LIMIT 1
                  )
                LEFT JOIN agent_invitations AS invitation
                  ON invitation.invitation_id = connector.invitation_id
                LEFT JOIN agent_lifecycle_states AS lifecycle
                  ON lifecycle.participant_id = p.participant_id
                JOIN agent_lifecycle_policy AS policy
                  ON policy.singleton = 1
                WHERE m.conversation_id = ?
                ORDER BY
                    CASE
                        WHEN room.status = 'active'
                         AND m.active = 1
                         AND (
                            online_agent_session_count > 0
                            OR online_web_session_count > 0
                            OR (
                                connector.setup_status = 'configured'
                                AND connector.connector_last_seen_at >= ?
                            )
                         ) THEN 0
                        ELSE 1
                    END,
                    p.display_name,
                    p.participant_id
                """,
                (
                    now,
                    connector_online_after,
                    now,
                    now,
                    online_after,
                    now,
                    now,
                    online_after,
                    conversation,
                    connector_online_after,
                ),
            ).fetchall()
        return [
            {
                "participant_id": str(row["participant_id"]),
                "client_type": str(row["client_type"]),
                "session_alias": str(row["session_alias"]),
                "display_name": str(row["display_name"]),
                "signature": str(row["signature"]),
                "avatar_key": str(row["avatar_key"] or "auto"),
                "roles": json.loads(str(row["roles_json"])),
                "capabilities": json.loads(str(row["capabilities_json"])),
                "status": (
                    "online"
                    if str(row["room_status"]) == "active"
                    and int(row["membership_active"]) == 1
                    and (
                        int(row["online_agent_session_count"] or 0) > 0
                        or int(row["online_web_session_count"] or 0) > 0
                        or (
                            str(row["connector_setup_status"] or "") == "configured"
                            and row["connector_last_seen_at"] is not None
                            and float(row["connector_last_seen_at"])
                            >= connector_online_after
                        )
                    )
                    else "offline"
                ),
                "membership_active": bool(row["membership_active"]),
                "room_status": str(row["room_status"]),
                "last_seen": float(row["last_seen"]),
                "joined_at": float(row["joined_at"]),
                "inactivity_expires_at": (
                    float(row["inactivity_expires_at"])
                    if row["inactivity_expires_at"] is not None
                    else None
                ),
                "active_session_count": int(row["active_agent_session_count"] or 0)
                + int(row["active_web_session_count"] or 0),
                "connector_id": (
                    str(row["connector_id"])
                    if row["connector_id"] is not None
                    else None
                ),
                "connector_adapter_kind": str(row["connector_adapter_kind"] or ""),
                "connector_setup_status": str(row["connector_setup_status"] or ""),
                "connector_last_seen_at": (
                    float(row["connector_last_seen_at"])
                    if row["connector_last_seen_at"] is not None
                    else None
                ),
                "native_tui": {
                    "endpoint_id": (
                        str(row["tui_endpoint_id"])
                        if row["tui_endpoint_id"] is not None
                        else None
                    ),
                    "native_session_id": (
                        str(row["tui_native_session_id"])
                        if row["tui_native_session_id"] is not None
                        else None
                    ),
                    "state": (
                        "offline"
                        if str(row["tui_state"] or "unbound") in {"online", "busy"}
                        and (
                            row["tui_last_seen_at"] is None
                            or float(row["tui_last_seen_at"]) < connector_online_after
                            or (
                                str(row["native_delivery_mode"] or "")
                                == "native_preferred"
                                and (
                                    row["native_lease_id"] is None
                                    or row["native_lease_expires_at"] is None
                                    or float(row["native_lease_expires_at"]) <= now
                                )
                            )
                        )
                        else str(row["tui_state"] or "unbound")
                    ),
                    "last_seen_at": (
                        float(row["tui_last_seen_at"])
                        if row["tui_last_seen_at"] is not None
                        else None
                    ),
                    "active_task_id": (
                        str(row["tui_active_task_id"])
                        if row["tui_active_task_id"] is not None
                        else None
                    ),
                },
                "resident_status": (
                    "online"
                    if str(row["connector_setup_status"] or "") == "configured"
                    and row["connector_last_seen_at"] is not None
                    and float(row["connector_last_seen_at"]) >= connector_online_after
                    else (
                        "offline"
                        if str(row["connector_setup_status"] or "") == "configured"
                        else str(row["connector_setup_status"] or "none")
                    )
                ),
            }
            for row in rows
            if str(row["room_status"]) != "active" or int(row["membership_active"]) == 1
        ]
