"""Structured per-Agent delivery projections for the Web viewer."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Sequence
from typing import Any


class ViewerDeliveryProjectionMixin:
    """Share one delivery authority between message and receipt APIs."""

    @classmethod
    def _message_delivery_projection_locked(
        cls,
        connection: sqlite3.Connection,
        message_ids: Sequence[str],
    ) -> dict[str, dict[str, Any]]:
        """Project milestones from structured facts, never message prose."""

        normalized_ids = list(
            dict.fromkeys(str(value) for value in message_ids if value)
        )
        status_keys = (
            "replied",
            "read",
            "injected",
            "acknowledged",
            "notified",
            "queued",
            "offline",
            "unavailable",
            "cancelled",
        )

        def empty_projection() -> dict[str, Any]:
            summary = {key: 0 for key in status_keys}
            summary.update(
                {
                    "total": 0,
                    "received": 0,
                    "unreceived": 0,
                    "unreplied": 0,
                    "dnd": 0,
                }
            )
            return {
                "ack_count": 0,
                "receipt_count": 0,
                "agent_delivery_summary": summary,
                "agent_deliveries": [],
            }

        result = {message_id: empty_projection() for message_id in normalized_ids}
        if not normalized_ids:
            return result

        now = time.time()
        endpoint_online_after = now - 75.0
        for offset in range(0, len(normalized_ids), 400):
            chunk = normalized_ids[offset : offset + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows = connection.execute(
                f"""
                WITH selected_delivery AS MATERIALIZED (
                    SELECT delivery.*, message.conversation_id
                    FROM message_deliveries AS delivery
                    JOIN messages AS message
                      ON message.message_id = delivery.message_id
                    WHERE delivery.message_id IN ({placeholders})
                ),
                active_sessions AS MATERIALIZED (
                    SELECT DISTINCT participant_id
                    FROM agent_sessions
                    WHERE cleared_at IS NULL
                      AND revoked_at IS NULL
                      AND expires_at > ?
                ),
                active_connectors AS MATERIALIZED (
                    SELECT DISTINCT accepted_participant_id AS participant_id
                    FROM agent_connectors
                    WHERE revoked_at IS NULL
                      AND setup_status = 'configured'
                      AND (
                          COALESCE(connector_last_seen_at, 0) >= ?
                          OR COALESCE(tui_last_seen_at, 0) >= ?
                      )
                ),
                exact_replies AS MATERIALIZED (
                    SELECT DISTINCT reply.reply_to AS message_id,
                           reply.sender_participant_id AS participant_id
                    FROM selected_delivery AS delivery
                    JOIN messages AS reply
                      ON reply.reply_to = delivery.message_id
                     AND reply.sender_participant_id = delivery.participant_id
                )
                SELECT delivery.message_id, delivery.participant_id,
                       delivery.state, delivery.delivery_stage,
                       delivery.priority, delivery.actionable,
                       delivery.created_at, delivery.first_delivered_at,
                       delivery.last_delivered_at, delivery.acked_at,
                       delivery.native_injected_at,
                       delivery.native_applied_at,
                       delivery.native_replied_at,
                       participant.client_type,
                       participant.display_name,
                       participant.avatar_key,
                       membership.active AS membership_active,
                       receipt.state AS receipt_state,
                       dnd.expires_at AS dnd_expires_at,
                       active_session.participant_id IS NOT NULL
                           AS active_agent_session,
                       active_connector.participant_id IS NOT NULL
                           AS active_connector,
                       exact_reply.message_id IS NOT NULL AS exact_reply
                FROM selected_delivery AS delivery
                JOIN participants AS participant
                  ON participant.participant_id = delivery.participant_id
                LEFT JOIN memberships AS membership
                  ON membership.conversation_id = delivery.conversation_id
                 AND membership.participant_id = delivery.participant_id
                LEFT JOIN receipts AS receipt
                  ON receipt.message_id = delivery.message_id
                 AND receipt.participant_id = delivery.participant_id
                LEFT JOIN agent_room_dnd AS dnd
                  ON dnd.conversation_id = delivery.conversation_id
                 AND dnd.participant_id = delivery.participant_id
                 AND dnd.expires_at > ?
                LEFT JOIN active_sessions AS active_session
                  ON active_session.participant_id = delivery.participant_id
                LEFT JOIN active_connectors AS active_connector
                  ON active_connector.participant_id = delivery.participant_id
                LEFT JOIN exact_replies AS exact_reply
                  ON exact_reply.message_id = delivery.message_id
                 AND exact_reply.participant_id = delivery.participant_id
                ORDER BY delivery.message_id,
                         participant.display_name,
                         delivery.participant_id
                """,
                (
                    *chunk,
                    now,
                    endpoint_online_after,
                    endpoint_online_after,
                    now,
                ),
            ).fetchall()
            for row in rows:
                message_id = str(row["message_id"])
                projection = result[message_id]
                projection["receipt_count"] += 1
                if str(row["receipt_state"] or "") == "acked":
                    projection["ack_count"] += 1

                client_type = str(row["client_type"] or "")
                if client_type.startswith("web-user"):
                    continue
                status = cls._delivery_display_status(row)
                active_endpoint = bool(row["active_agent_session"]) or bool(
                    row["active_connector"]
                )
                dnd_expires_at = (
                    float(row["dnd_expires_at"])
                    if row["dnd_expires_at"] is not None
                    else None
                )
                projection["agent_deliveries"].append(
                    {
                        "participant_id": str(row["participant_id"]),
                        "client_type": client_type,
                        "display_name": str(row["display_name"] or client_type),
                        "avatar_key": str(row["avatar_key"] or "auto"),
                        "status": status,
                        "state": str(row["state"]),
                        "delivery_stage": str(row["delivery_stage"] or "queued"),
                        "priority": str(row["priority"] or "normal"),
                        "actionable": bool(row["actionable"]),
                        "membership_active": bool(row["membership_active"]),
                        "active_endpoint": active_endpoint,
                        "dnd_active": dnd_expires_at is not None,
                        "dnd_expires_at": dnd_expires_at,
                        "status_at": cls._delivery_status_timestamp(row, status),
                    }
                )
                summary = projection["agent_delivery_summary"]
                summary["total"] += 1
                summary[status] += 1
                if status in {
                    "replied",
                    "read",
                    "injected",
                    "acknowledged",
                    "notified",
                }:
                    summary["received"] += 1
                if status in {"queued", "offline", "unavailable"}:
                    summary["unreceived"] += 1
                if status in {
                    "read",
                    "injected",
                    "acknowledged",
                    "notified",
                }:
                    summary["unreplied"] += 1
                if dnd_expires_at is not None:
                    summary["dnd"] += 1
        return result

    @staticmethod
    def _delivery_display_status(row: sqlite3.Row) -> str:
        if bool(row["exact_reply"]) or str(row["delivery_stage"]) == "replied":
            return "replied"
        if (
            str(row["state"]) == "cancelled"
            or str(row["delivery_stage"]) == "cancelled"
        ):
            return "cancelled"
        if str(row["delivery_stage"]) == "native_applied":
            return "read"
        if str(row["delivery_stage"]) == "native_injected":
            return "injected"
        if (
            str(row["delivery_stage"]) == "legacy_acked"
            or str(row["state"]) == "acked"
            or str(row["receipt_state"] or "") == "acked"
        ):
            return "acknowledged"
        if (
            str(row["delivery_stage"]) == "legacy_delivered"
            or str(row["state"]) == "delivered"
            or row["first_delivered_at"] is not None
        ):
            return "notified"
        if not bool(row["membership_active"]):
            return "unavailable"
        if not bool(row["active_agent_session"]) and not bool(
            row["active_connector"]
        ):
            return "offline"
        return "queued"

    @staticmethod
    def _delivery_status_timestamp(row: sqlite3.Row, status: str) -> float:
        candidates = {
            "replied": ("native_replied_at", "acked_at", "last_delivered_at"),
            "read": ("native_applied_at", "native_injected_at"),
            "injected": ("native_injected_at",),
            "acknowledged": ("acked_at", "last_delivered_at"),
            "notified": ("last_delivered_at", "first_delivered_at"),
        }.get(status, ())
        for key in candidates:
            if row[key] is not None:
                return float(row[key])
        return float(row["created_at"])
