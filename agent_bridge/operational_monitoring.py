"""Operational sampling and alert persistence for Agent Bridge.

This module owns the monitoring schema and calculations.  The central store
retains connection, transaction, authorization, and connector-health
authority; public ``BridgeStore`` methods delegate here without changing the
API surface.
"""

from __future__ import annotations

import math
import sqlite3
import time
import uuid
from contextlib import AbstractContextManager
from typing import Any, Protocol

from .validation import ValidationError, opaque_id


REQUIRED_REPLY_DELAY_WARNING_SECONDS = 5 * 60
MONITORING_SAMPLE_INTERVAL_SECONDS = 60
MONITORING_RETENTION_SECONDS = 30 * 24 * 60 * 60
MONITORING_REPLY_LATENCY_WARNING_SECONDS = 10 * 60
MONITORING_TASK_NEEDS_INPUT_WARNING_SECONDS = 30 * 60
MONITORING_TASK_FAILURE_RATE_WARNING = 0.5
MONITORING_MIN_RATE_SAMPLE_COUNT = 3


OPERATIONAL_MONITORING_SCHEMA = """
CREATE TABLE IF NOT EXISTS operational_metric_samples (
    sample_minute INTEGER PRIMARY KEY,
    captured_at REAL NOT NULL,
    connector_count INTEGER NOT NULL CHECK (connector_count >= 0),
    connector_online_count INTEGER NOT NULL CHECK (connector_online_count >= 0),
    connector_offline_count INTEGER NOT NULL CHECK (connector_offline_count >= 0),
    connector_failed_count INTEGER NOT NULL CHECK (connector_failed_count >= 0),
    connector_attention_count INTEGER NOT NULL CHECK (connector_attention_count >= 0),
    pending_delivery_count INTEGER NOT NULL CHECK (pending_delivery_count >= 0),
    required_pending_count INTEGER NOT NULL CHECK (required_pending_count >= 0),
    delayed_required_count INTEGER NOT NULL CHECK (delayed_required_count >= 0),
    task_backlog_count INTEGER NOT NULL CHECK (task_backlog_count >= 0),
    task_queued_count INTEGER NOT NULL CHECK (task_queued_count >= 0),
    task_running_count INTEGER NOT NULL CHECK (task_running_count >= 0),
    task_needs_input_count INTEGER NOT NULL CHECK (task_needs_input_count >= 0),
    task_needs_input_delayed_count INTEGER NOT NULL
        CHECK (task_needs_input_delayed_count >= 0),
    task_expired_lease_count INTEGER NOT NULL
        CHECK (task_expired_lease_count >= 0),
    task_terminal_count_1h INTEGER NOT NULL CHECK (task_terminal_count_1h >= 0),
    task_failed_count_1h INTEGER NOT NULL CHECK (task_failed_count_1h >= 0),
    task_failure_rate_1h REAL NOT NULL CHECK (
        task_failure_rate_1h >= 0 AND task_failure_rate_1h <= 1
    ),
    reply_sample_count_1h INTEGER NOT NULL CHECK (reply_sample_count_1h >= 0),
    reply_latency_average_seconds REAL,
    reply_latency_p95_seconds REAL,
    native_queue_to_injected_sample_count_1h INTEGER NOT NULL DEFAULT 0
        CHECK (native_queue_to_injected_sample_count_1h >= 0),
    native_queue_to_injected_average_seconds REAL,
    native_queue_to_injected_p95_seconds REAL,
    native_injected_to_applied_sample_count_1h INTEGER NOT NULL DEFAULT 0
        CHECK (native_injected_to_applied_sample_count_1h >= 0),
    native_injected_to_applied_average_seconds REAL,
    native_injected_to_applied_p95_seconds REAL,
    native_applied_to_reply_sample_count_1h INTEGER NOT NULL DEFAULT 0
        CHECK (native_applied_to_reply_sample_count_1h >= 0),
    native_applied_to_reply_average_seconds REAL,
    native_applied_to_reply_p95_seconds REAL
);

CREATE INDEX IF NOT EXISTS idx_operational_metric_samples_captured
    ON operational_metric_samples(captured_at DESC);

CREATE TABLE IF NOT EXISTS operational_alerts (
    alert_id TEXT PRIMARY KEY,
    alert_key TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('warning', 'critical')),
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'resolved')),
    title TEXT NOT NULL,
    detail TEXT NOT NULL,
    current_value REAL NOT NULL,
    threshold_value REAL NOT NULL,
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    resolved_at REAL,
    occurrence_count INTEGER NOT NULL DEFAULT 1
        CHECK (occurrence_count >= 1),
    last_sample_minute INTEGER NOT NULL,
    acknowledged_at REAL,
    acknowledged_by_web_user_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (acknowledged_by_web_user_id) REFERENCES web_users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_operational_alerts_status_severity_updated
    ON operational_alerts(status, severity, updated_at DESC);

CREATE TABLE IF NOT EXISTS operational_monitoring_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    revision INTEGER NOT NULL DEFAULT 0,
    last_sample_at REAL,
    updated_at REAL NOT NULL
);

INSERT OR IGNORE INTO operational_monitoring_state
    (singleton, revision, last_sample_at, updated_at)
VALUES (1, 0, NULL, CAST(strftime('%s', 'now') AS REAL));

CREATE INDEX IF NOT EXISTS idx_messages_reply_target_sender_created
    ON messages(reply_to, sender_participant_id, created_at)
    WHERE reply_to IS NOT NULL;
"""


OPERATIONAL_MONITORING_COLUMN_ADDITIONS = {
    "native_queue_to_injected_sample_count_1h": (
        "INTEGER NOT NULL DEFAULT 0 CHECK "
        "(native_queue_to_injected_sample_count_1h >= 0)"
    ),
    "native_queue_to_injected_average_seconds": "REAL",
    "native_queue_to_injected_p95_seconds": "REAL",
    "native_injected_to_applied_sample_count_1h": (
        "INTEGER NOT NULL DEFAULT 0 CHECK "
        "(native_injected_to_applied_sample_count_1h >= 0)"
    ),
    "native_injected_to_applied_average_seconds": "REAL",
    "native_injected_to_applied_p95_seconds": "REAL",
    "native_applied_to_reply_sample_count_1h": (
        "INTEGER NOT NULL DEFAULT 0 CHECK "
        "(native_applied_to_reply_sample_count_1h >= 0)"
    ),
    "native_applied_to_reply_average_seconds": "REAL",
    "native_applied_to_reply_p95_seconds": "REAL",
}


class MonitoringStore(Protocol):
    def _connection(self) -> AbstractContextManager[sqlite3.Connection]: ...

    def _transaction(self) -> AbstractContextManager[sqlite3.Connection]: ...

    def admin_connector_health(
        self,
        *,
        requesting_web_user_id: str,
    ) -> dict[str, Any]: ...

    def _require_active_admin_locked(
        self,
        conn: sqlite3.Connection,
        web_user_id: str,
    ) -> sqlite3.Row: ...


def monitoring_sample_payload(row: sqlite3.Row) -> dict[str, Any]:
    integer_fields = {
        "sample_minute",
        "connector_count",
        "connector_online_count",
        "connector_offline_count",
        "connector_failed_count",
        "connector_attention_count",
        "pending_delivery_count",
        "required_pending_count",
        "delayed_required_count",
        "task_backlog_count",
        "task_queued_count",
        "task_running_count",
        "task_needs_input_count",
        "task_needs_input_delayed_count",
        "task_expired_lease_count",
        "task_terminal_count_1h",
        "task_failed_count_1h",
        "reply_sample_count_1h",
        "native_queue_to_injected_sample_count_1h",
        "native_injected_to_applied_sample_count_1h",
        "native_applied_to_reply_sample_count_1h",
    }
    return {
        key: (
            int(row[key])
            if key in integer_fields
            else float(row[key]) if row[key] is not None else None
        )
        for key in row.keys()
    }


def monitoring_alert_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "alert_id": str(row["alert_id"]),
        "alert_key": str(row["alert_key"]),
        "category": str(row["category"]),
        "severity": str(row["severity"]),
        "status": str(row["status"]),
        "title": str(row["title"]),
        "detail": str(row["detail"]),
        "current_value": float(row["current_value"]),
        "threshold_value": float(row["threshold_value"]),
        "first_seen_at": float(row["first_seen_at"]),
        "last_seen_at": float(row["last_seen_at"]),
        "resolved_at": (
            float(row["resolved_at"])
            if row["resolved_at"] is not None
            else None
        ),
        "occurrence_count": int(row["occurrence_count"]),
        "acknowledged_at": (
            float(row["acknowledged_at"])
            if row["acknowledged_at"] is not None
            else None
        ),
        "acknowledged_by_web_user_id": (
            str(row["acknowledged_by_web_user_id"])
            if row["acknowledged_by_web_user_id"] is not None
            else None
        ),
        "acknowledged_by_username": str(row["acknowledged_by_username"] or ""),
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
    }


def latency_statistics(
    values: list[float],
) -> tuple[int, float | None, float | None]:
    ordered = sorted(max(0.0, float(value)) for value in values)
    if not ordered:
        return 0, None, None
    average = sum(ordered) / len(ordered)
    p95 = ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]
    return len(ordered), average, p95


def _collect_sample(
    store: MonitoringStore,
    *,
    captured_at: float,
    authentication_error: type[Exception],
) -> tuple[dict[str, Any], list[float]]:
    with store._connection() as conn:
        administrator = conn.execute(
            "SELECT user_id FROM web_users "
            "WHERE role = 'admin' AND active = 1 "
            "ORDER BY created_at LIMIT 1"
        ).fetchone()
    if administrator is None:
        raise authentication_error("an active administrator is required")
    health = store.admin_connector_health(
        requesting_web_user_id=str(administrator["user_id"]),
    )

    window_start = captured_at - 60 * 60
    needs_input_cutoff = captured_at - MONITORING_TASK_NEEDS_INPUT_WARNING_SECONDS
    with store._connection() as conn:
        latency_rows = conn.execute(
            """
            SELECT MIN(MAX(0, reply.created_at - source.created_at)) AS latency
            FROM message_deliveries AS delivery
            JOIN messages AS source
              ON source.message_id = delivery.message_id
            JOIN messages AS reply
              ON reply.reply_to = source.message_id
             AND reply.sender_participant_id = delivery.participant_id
            WHERE source.created_at >= ?
              AND instr(delivery.reasons_json, '"quiet_optional"') = 0
              AND (
                  instr(delivery.reasons_json, '"mention"') > 0
                  OR instr(delivery.reasons_json, '"agent_request"') > 0
              )
            GROUP BY source.message_id, delivery.participant_id
            """,
            (window_start,),
        ).fetchall()
        native_latency_rows = conn.execute(
            """
            SELECT
                CASE
                    WHEN native_injected_at IS NOT NULL
                    THEN MAX(0, native_injected_at - created_at)
                END AS queue_to_injected,
                CASE
                    WHEN native_injected_at IS NOT NULL
                     AND native_applied_at IS NOT NULL
                    THEN MAX(0, native_applied_at - native_injected_at)
                END AS injected_to_applied,
                CASE
                    WHEN native_applied_at IS NOT NULL
                     AND native_replied_at IS NOT NULL
                    THEN MAX(0, native_replied_at - native_applied_at)
                END AS applied_to_reply
            FROM message_deliveries
            WHERE created_at >= ?
              AND (
                  native_injected_at IS NOT NULL
                  OR native_applied_at IS NOT NULL
                  OR native_replied_at IS NOT NULL
              )
            """,
            (window_start,),
        ).fetchall()
        task_rates = conn.execute(
            """
            SELECT
                SUM(
                    CASE WHEN status IN ('completed', 'failed')
                              AND updated_at >= ?
                         THEN 1 ELSE 0 END
                ) AS terminal_count,
                SUM(
                    CASE WHEN status = 'failed' AND updated_at >= ?
                         THEN 1 ELSE 0 END
                ) AS failed_count,
                SUM(
                    CASE WHEN status = 'needs_input' AND updated_at <= ?
                         THEN 1 ELSE 0 END
                ) AS delayed_needs_input_count
            FROM room_tasks
            """,
            (window_start, window_start, needs_input_cutoff),
        ).fetchone()

    latencies = sorted(
        float(row["latency"])
        for row in latency_rows
        if row["latency"] is not None
    )
    reply_count, reply_average, reply_p95 = latency_statistics(latencies)

    def native_stats(key: str) -> tuple[int, float | None, float | None]:
        return latency_statistics(
            [
                float(row[key])
                for row in native_latency_rows
                if row[key] is not None
            ]
        )

    queue_count, queue_average, queue_p95 = native_stats("queue_to_injected")
    applied_count, applied_average, applied_p95 = native_stats(
        "injected_to_applied"
    )
    reply_stage_count, reply_stage_average, reply_stage_p95 = native_stats(
        "applied_to_reply"
    )
    terminal_count = int(task_rates["terminal_count"] or 0)
    failed_count = int(task_rates["failed_count"] or 0)
    failure_rate = failed_count / terminal_count if terminal_count else 0.0
    status_counts = health.get("status_counts", {})
    task_health = health.get("tasks", {})
    backlog = health.get("backlog", {})
    delayed_required = sum(
        any(
            issue.get("code") == "required_reply_delayed"
            for issue in connector.get("issues", [])
        )
        for connector in health.get("connectors", [])
    )
    sample = {
        "sample_minute": int(captured_at // MONITORING_SAMPLE_INTERVAL_SECONDS)
        * MONITORING_SAMPLE_INTERVAL_SECONDS,
        "captured_at": captured_at,
        "connector_count": int(health.get("count", 0)),
        "connector_online_count": int(health.get("online_count", 0)),
        "connector_offline_count": int(status_counts.get("offline", 0)),
        "connector_failed_count": int(status_counts.get("failed", 0)),
        "connector_attention_count": int(health.get("attention_count", 0)),
        "pending_delivery_count": int(backlog.get("pending_count", 0)),
        "required_pending_count": int(backlog.get("required_pending_count", 0)),
        "delayed_required_count": int(delayed_required),
        "task_backlog_count": int(task_health.get("active_count", 0)),
        "task_queued_count": int(task_health.get("queued_count", 0)),
        "task_running_count": int(task_health.get("running_count", 0)),
        "task_needs_input_count": int(task_health.get("needs_input_count", 0)),
        "task_needs_input_delayed_count": int(
            task_rates["delayed_needs_input_count"] or 0
        ),
        "task_expired_lease_count": int(
            task_health.get("expired_lease_count", 0)
        ),
        "task_terminal_count_1h": terminal_count,
        "task_failed_count_1h": failed_count,
        "task_failure_rate_1h": failure_rate,
        "reply_sample_count_1h": reply_count,
        "reply_latency_average_seconds": reply_average,
        "reply_latency_p95_seconds": reply_p95,
        "native_queue_to_injected_sample_count_1h": queue_count,
        "native_queue_to_injected_average_seconds": queue_average,
        "native_queue_to_injected_p95_seconds": queue_p95,
        "native_injected_to_applied_sample_count_1h": applied_count,
        "native_injected_to_applied_average_seconds": applied_average,
        "native_injected_to_applied_p95_seconds": applied_p95,
        "native_applied_to_reply_sample_count_1h": reply_stage_count,
        "native_applied_to_reply_average_seconds": reply_stage_average,
        "native_applied_to_reply_p95_seconds": reply_stage_p95,
    }
    return sample, latencies


def _alert_specs(
    sample: dict[str, Any],
    *,
    reply_latencies: list[float],
) -> tuple[dict[str, Any], ...]:
    terminal_count = int(sample["task_terminal_count_1h"])
    failed_count = int(sample["task_failed_count_1h"])
    failure_rate = float(sample["task_failure_rate_1h"])
    latency_p95 = sample["reply_latency_p95_seconds"]
    unavailable_count = int(sample["connector_offline_count"]) + int(
        sample["connector_failed_count"]
    )
    return (
        {
            "key": "connector-unavailable",
            "active": unavailable_count > 0,
            "category": "connector",
            "severity": (
                "critical" if sample["connector_failed_count"] else "warning"
            ),
            "title": "自动值守连接不可用",
            "detail": (
                f"{sample['connector_offline_count']} 个离线，"
                f"{sample['connector_failed_count']} 个异常。"
            ),
            "value": float(unavailable_count),
            "threshold": 0.0,
        },
        {
            "key": "required-reply-delayed",
            "active": sample["delayed_required_count"] > 0,
            "category": "reply",
            "severity": "warning",
            "title": "必须回复等待过久",
            "detail": "存在超过 5 分钟仍未完成的个人艾特或明确请求。",
            "value": float(sample["delayed_required_count"]),
            "threshold": 0.0,
        },
        {
            "key": "task-lease-expired",
            "active": sample["task_expired_lease_count"] > 0,
            "category": "task",
            "severity": "critical",
            "title": "任务租约已经过期",
            "detail": "任务仍处于领取或运行状态，但执行租约已过期。",
            "value": float(sample["task_expired_lease_count"]),
            "threshold": 0.0,
        },
        {
            "key": "task-needs-input-delayed",
            "active": sample["task_needs_input_delayed_count"] > 0,
            "category": "task",
            "severity": "warning",
            "title": "任务等待输入超过 30 分钟",
            "detail": "需要用户或协作者补充信息的任务长时间没有继续。",
            "value": float(sample["task_needs_input_delayed_count"]),
            "threshold": 0.0,
        },
        {
            "key": "task-failure-rate-high",
            "active": (
                terminal_count >= MONITORING_MIN_RATE_SAMPLE_COUNT
                and failure_rate >= MONITORING_TASK_FAILURE_RATE_WARNING
            ),
            "category": "task",
            "severity": "warning",
            "title": "最近一小时任务失败率偏高",
            "detail": (
                f"最近一小时 {terminal_count} 个终态任务中 "
                f"{failed_count} 个失败。"
            ),
            "value": failure_rate,
            "threshold": MONITORING_TASK_FAILURE_RATE_WARNING,
        },
        {
            "key": "reply-latency-high",
            "active": (
                len(reply_latencies) >= MONITORING_MIN_RATE_SAMPLE_COUNT
                and latency_p95 is not None
                and latency_p95 >= MONITORING_REPLY_LATENCY_WARNING_SECONDS
            ),
            "category": "reply",
            "severity": "warning",
            "title": "最近一小时回复延迟偏高",
            "detail": (
                f"个人艾特/明确请求的 P95 回复延迟为 "
                f"{int(latency_p95 or 0)} 秒。"
            ),
            "value": float(latency_p95 or 0.0),
            "threshold": float(MONITORING_REPLY_LATENCY_WARNING_SECONDS),
        },
    )


def _persist_sample(
    store: MonitoringStore,
    *,
    sample: dict[str, Any],
    alert_specs: tuple[dict[str, Any], ...],
) -> sqlite3.Row:
    captured_at = float(sample["captured_at"])
    sample_minute = int(sample["sample_minute"])
    with store._transaction() as conn:
        conn.execute(
            """
            INSERT INTO operational_metric_samples (
                sample_minute, captured_at,
                connector_count, connector_online_count,
                connector_offline_count, connector_failed_count,
                connector_attention_count, pending_delivery_count,
                required_pending_count, delayed_required_count,
                task_backlog_count, task_queued_count, task_running_count,
                task_needs_input_count, task_needs_input_delayed_count,
                task_expired_lease_count, task_terminal_count_1h,
                task_failed_count_1h, task_failure_rate_1h,
                reply_sample_count_1h, reply_latency_average_seconds,
                reply_latency_p95_seconds,
                native_queue_to_injected_sample_count_1h,
                native_queue_to_injected_average_seconds,
                native_queue_to_injected_p95_seconds,
                native_injected_to_applied_sample_count_1h,
                native_injected_to_applied_average_seconds,
                native_injected_to_applied_p95_seconds,
                native_applied_to_reply_sample_count_1h,
                native_applied_to_reply_average_seconds,
                native_applied_to_reply_p95_seconds
            ) VALUES (
                :sample_minute, :captured_at,
                :connector_count, :connector_online_count,
                :connector_offline_count, :connector_failed_count,
                :connector_attention_count, :pending_delivery_count,
                :required_pending_count, :delayed_required_count,
                :task_backlog_count, :task_queued_count, :task_running_count,
                :task_needs_input_count, :task_needs_input_delayed_count,
                :task_expired_lease_count, :task_terminal_count_1h,
                :task_failed_count_1h, :task_failure_rate_1h,
                :reply_sample_count_1h, :reply_latency_average_seconds,
                :reply_latency_p95_seconds,
                :native_queue_to_injected_sample_count_1h,
                :native_queue_to_injected_average_seconds,
                :native_queue_to_injected_p95_seconds,
                :native_injected_to_applied_sample_count_1h,
                :native_injected_to_applied_average_seconds,
                :native_injected_to_applied_p95_seconds,
                :native_applied_to_reply_sample_count_1h,
                :native_applied_to_reply_average_seconds,
                :native_applied_to_reply_p95_seconds
            )
            ON CONFLICT(sample_minute) DO UPDATE SET
                captured_at = excluded.captured_at,
                connector_count = excluded.connector_count,
                connector_online_count = excluded.connector_online_count,
                connector_offline_count = excluded.connector_offline_count,
                connector_failed_count = excluded.connector_failed_count,
                connector_attention_count = excluded.connector_attention_count,
                pending_delivery_count = excluded.pending_delivery_count,
                required_pending_count = excluded.required_pending_count,
                delayed_required_count = excluded.delayed_required_count,
                task_backlog_count = excluded.task_backlog_count,
                task_queued_count = excluded.task_queued_count,
                task_running_count = excluded.task_running_count,
                task_needs_input_count = excluded.task_needs_input_count,
                task_needs_input_delayed_count =
                    excluded.task_needs_input_delayed_count,
                task_expired_lease_count = excluded.task_expired_lease_count,
                task_terminal_count_1h = excluded.task_terminal_count_1h,
                task_failed_count_1h = excluded.task_failed_count_1h,
                task_failure_rate_1h = excluded.task_failure_rate_1h,
                reply_sample_count_1h = excluded.reply_sample_count_1h,
                reply_latency_average_seconds =
                    excluded.reply_latency_average_seconds,
                reply_latency_p95_seconds = excluded.reply_latency_p95_seconds,
                native_queue_to_injected_sample_count_1h =
                    excluded.native_queue_to_injected_sample_count_1h,
                native_queue_to_injected_average_seconds =
                    excluded.native_queue_to_injected_average_seconds,
                native_queue_to_injected_p95_seconds =
                    excluded.native_queue_to_injected_p95_seconds,
                native_injected_to_applied_sample_count_1h =
                    excluded.native_injected_to_applied_sample_count_1h,
                native_injected_to_applied_average_seconds =
                    excluded.native_injected_to_applied_average_seconds,
                native_injected_to_applied_p95_seconds =
                    excluded.native_injected_to_applied_p95_seconds,
                native_applied_to_reply_sample_count_1h =
                    excluded.native_applied_to_reply_sample_count_1h,
                native_applied_to_reply_average_seconds =
                    excluded.native_applied_to_reply_average_seconds,
                native_applied_to_reply_p95_seconds =
                    excluded.native_applied_to_reply_p95_seconds
            """,
            sample,
        )
        for spec in alert_specs:
            existing = conn.execute(
                "SELECT * FROM operational_alerts WHERE alert_key = ?",
                (spec["key"],),
            ).fetchone()
            if spec["active"]:
                if existing is None:
                    conn.execute(
                        """
                        INSERT INTO operational_alerts (
                            alert_id, alert_key, category, severity, status,
                            title, detail, current_value, threshold_value,
                            first_seen_at, last_seen_at, occurrence_count,
                            last_sample_minute, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                        """,
                        (
                            f"opalert_{uuid.uuid4().hex}",
                            spec["key"],
                            spec["category"],
                            spec["severity"],
                            spec["title"],
                            spec["detail"],
                            spec["value"],
                            spec["threshold"],
                            captured_at,
                            captured_at,
                            sample_minute,
                            captured_at,
                            captured_at,
                        ),
                    )
                elif str(existing["status"]) == "resolved":
                    conn.execute(
                        """
                        UPDATE operational_alerts
                        SET category = ?, severity = ?, status = 'open',
                            title = ?, detail = ?, current_value = ?,
                            threshold_value = ?, first_seen_at = ?,
                            last_seen_at = ?, resolved_at = NULL,
                            occurrence_count = 1, last_sample_minute = ?,
                            acknowledged_at = NULL,
                            acknowledged_by_web_user_id = NULL,
                            updated_at = ?
                        WHERE alert_id = ?
                        """,
                        (
                            spec["category"],
                            spec["severity"],
                            spec["title"],
                            spec["detail"],
                            spec["value"],
                            spec["threshold"],
                            captured_at,
                            captured_at,
                            sample_minute,
                            captured_at,
                            str(existing["alert_id"]),
                        ),
                    )
                else:
                    occurrence_increment = int(
                        int(existing["last_sample_minute"]) != sample_minute
                    )
                    conn.execute(
                        """
                        UPDATE operational_alerts
                        SET category = ?, severity = ?, title = ?, detail = ?,
                            current_value = ?, threshold_value = ?,
                            last_seen_at = ?, occurrence_count =
                                occurrence_count + ?,
                            last_sample_minute = ?, updated_at = ?
                        WHERE alert_id = ?
                        """,
                        (
                            spec["category"],
                            spec["severity"],
                            spec["title"],
                            spec["detail"],
                            spec["value"],
                            spec["threshold"],
                            captured_at,
                            occurrence_increment,
                            sample_minute,
                            captured_at,
                            str(existing["alert_id"]),
                        ),
                    )
            elif existing is not None and str(existing["status"]) == "open":
                conn.execute(
                    """
                    UPDATE operational_alerts
                    SET status = 'resolved', resolved_at = ?, updated_at = ?
                    WHERE alert_id = ?
                    """,
                    (captured_at, captured_at, str(existing["alert_id"])),
                )
        conn.execute(
            "DELETE FROM operational_metric_samples WHERE captured_at < ?",
            (captured_at - MONITORING_RETENTION_SECONDS,),
        )
        conn.execute(
            """
            UPDATE operational_monitoring_state
            SET revision = revision + 1, last_sample_at = ?, updated_at = ?
            WHERE singleton = 1
            """,
            (captured_at, captured_at),
        )
        row = conn.execute(
            "SELECT * FROM operational_metric_samples WHERE sample_minute = ?",
            (sample_minute,),
        ).fetchone()
    assert row is not None
    return row


def record_operational_sample(
    store: MonitoringStore,
    *,
    authentication_error: type[Exception],
) -> dict[str, Any]:
    """Persist one idempotent minute of monitoring evidence."""

    sample, latencies = _collect_sample(
        store,
        captured_at=time.time(),
        authentication_error=authentication_error,
    )
    row = _persist_sample(
        store,
        sample=sample,
        alert_specs=_alert_specs(sample, reply_latencies=latencies),
    )
    return monitoring_sample_payload(row)


def operational_monitoring_dashboard(
    store: MonitoringStore,
    *,
    requesting_web_user_id: str,
    hours: object = 24,
) -> dict[str, Any]:
    requester = opaque_id(
        requesting_web_user_id,
        field="requesting_web_user_id",
    )
    if isinstance(hours, bool):
        raise ValidationError("monitoring hours must be an integer")
    try:
        normalized_hours = int(hours)
    except (TypeError, ValueError) as exc:
        raise ValidationError("monitoring hours must be an integer") from exc
    if not 1 <= normalized_hours <= 30 * 24:
        raise ValidationError("monitoring hours must be between 1 and 720")
    cutoff = time.time() - normalized_hours * 60 * 60
    with store._connection() as conn:
        store._require_active_admin_locked(conn, requester)
        rows = conn.execute(
            "SELECT * FROM operational_metric_samples "
            "WHERE captured_at >= ? ORDER BY captured_at",
            (cutoff,),
        ).fetchall()
        alert_rows = conn.execute(
            """
            SELECT alert.*, acknowledger.username
                AS acknowledged_by_username
            FROM operational_alerts AS alert
            LEFT JOIN web_users AS acknowledger
              ON acknowledger.user_id = alert.acknowledged_by_web_user_id
            WHERE alert.status = 'open' OR alert.updated_at >= ?
            ORDER BY CASE alert.status WHEN 'open' THEN 0 ELSE 1 END,
                     CASE alert.severity WHEN 'critical' THEN 0 ELSE 1 END,
                     alert.updated_at DESC
            LIMIT 100
            """,
            (cutoff,),
        ).fetchall()
        state = conn.execute(
            "SELECT * FROM operational_monitoring_state WHERE singleton = 1"
        ).fetchone()
    samples = [monitoring_sample_payload(row) for row in rows]
    if len(samples) > 360:
        stride = math.ceil(len(samples) / 359)
        compacted = samples[::stride]
        if compacted[-1]["sample_minute"] != samples[-1]["sample_minute"]:
            compacted.append(samples[-1])
        samples = compacted
    alerts = [monitoring_alert_payload(row) for row in alert_rows]
    latest = samples[-1] if samples else None

    def maximum(key: str) -> float:
        values = [
            float(sample[key])
            for sample in samples
            if sample.get(key) is not None
        ]
        return max(values, default=0.0)

    return {
        "hours": normalized_hours,
        "samples": samples,
        "sample_count": len(rows),
        "latest": latest,
        "alerts": alerts,
        "open_alert_count": sum(alert["status"] == "open" for alert in alerts),
        "unacknowledged_open_alert_count": sum(
            alert["status"] == "open" and alert["acknowledged_at"] is None
            for alert in alerts
        ),
        "summary": {
            "max_offline_connectors": max(
                (
                    int(sample["connector_offline_count"])
                    + int(sample["connector_failed_count"])
                    for sample in samples
                ),
                default=0,
            ),
            "max_required_pending": int(maximum("required_pending_count")),
            "max_task_backlog": int(maximum("task_backlog_count")),
            "max_reply_latency_p95_seconds": maximum(
                "reply_latency_p95_seconds"
            ),
            "max_native_queue_to_injected_p95_seconds": maximum(
                "native_queue_to_injected_p95_seconds"
            ),
            "max_native_injected_to_applied_p95_seconds": maximum(
                "native_injected_to_applied_p95_seconds"
            ),
            "max_native_applied_to_reply_p95_seconds": maximum(
                "native_applied_to_reply_p95_seconds"
            ),
            "max_task_failure_rate_1h": maximum("task_failure_rate_1h"),
        },
        "thresholds": {
            "required_reply_delay_seconds": REQUIRED_REPLY_DELAY_WARNING_SECONDS,
            "reply_latency_p95_seconds": MONITORING_REPLY_LATENCY_WARNING_SECONDS,
            "task_needs_input_delay_seconds": (
                MONITORING_TASK_NEEDS_INPUT_WARNING_SECONDS
            ),
            "task_failure_rate": MONITORING_TASK_FAILURE_RATE_WARNING,
            "minimum_rate_sample_count": MONITORING_MIN_RATE_SAMPLE_COUNT,
        },
        "sample_interval_seconds": MONITORING_SAMPLE_INTERVAL_SECONDS,
        "retention_days": int(MONITORING_RETENTION_SECONDS / 86400),
        "revision": int(state["revision"] if state is not None else 0),
        "last_sample_at": (
            float(state["last_sample_at"])
            if state is not None and state["last_sample_at"] is not None
            else None
        ),
        "server_time": time.time(),
    }


def acknowledge_operational_alert(
    store: MonitoringStore,
    *,
    alert_id: str,
    acknowledged_by_web_user_id: str,
    not_found_error: type[Exception],
) -> dict[str, Any]:
    normalized_alert_id = opaque_id(alert_id, field="alert_id")
    administrator = opaque_id(
        acknowledged_by_web_user_id,
        field="acknowledged_by_web_user_id",
    )
    now = time.time()
    with store._transaction() as conn:
        store._require_active_admin_locked(conn, administrator)
        existing = conn.execute(
            "SELECT alert_id FROM operational_alerts WHERE alert_id = ?",
            (normalized_alert_id,),
        ).fetchone()
        if existing is None:
            raise not_found_error("operational alert was not found")
        conn.execute(
            """
            UPDATE operational_alerts
            SET acknowledged_at = COALESCE(acknowledged_at, ?),
                acknowledged_by_web_user_id =
                    COALESCE(acknowledged_by_web_user_id, ?),
                updated_at = ?
            WHERE alert_id = ?
            """,
            (now, administrator, now, normalized_alert_id),
        )
        conn.execute(
            "UPDATE operational_monitoring_state "
            "SET revision = revision + 1, updated_at = ? "
            "WHERE singleton = 1",
            (now,),
        )
        row = conn.execute(
            """
            SELECT alert.*, acknowledger.username
                AS acknowledged_by_username
            FROM operational_alerts AS alert
            LEFT JOIN web_users AS acknowledger
              ON acknowledger.user_id = alert.acknowledged_by_web_user_id
            WHERE alert.alert_id = ?
            """,
            (normalized_alert_id,),
        ).fetchone()
    assert row is not None
    return monitoring_alert_payload(row)
