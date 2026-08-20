"""Read-only central connector and room health projection."""

from __future__ import annotations

import json
import time
from typing import Any

from .operational_monitoring import REQUIRED_REPLY_DELAY_WARNING_SECONDS
from .store_constants import CONNECTOR_ONLINE_WINDOW_SECONDS
from .validation import opaque_id


REMOTE_QUEUE_STALLED_SECONDS = 5 * 60.0


class ConnectorHealthMixin:
    def admin_connector_health(
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
            rows = conn.execute(
                """
                SELECT connector.*, invitation.product,
                       invitation.requested_mode, invitation.adapter_kind,
                       invitation.tui_adapter_kind,
                       invitation.status AS invitation_status,
                       participant.client_type, participant.display_name,
                       MAX(session.last_seen) AS session_last_seen_at,
                       MAX(
                           CASE WHEN session.expires_at > ?
                                THEN session.last_seen END
                       ) AS active_session_last_seen_at,
                       SUM(
                           CASE WHEN session.expires_at > ? THEN 1 ELSE 0 END
                       ) AS active_session_count,
                       (
                           SELECT MAX(message.created_at)
                           FROM messages AS message
                           WHERE message.sender_participant_id =
                                 connector.accepted_participant_id
                             AND message.conversation_id = connector.conversation_id
                       ) AS last_reply_at,
                       (
                           SELECT COUNT(*)
                           FROM message_deliveries AS delivery
                           JOIN messages AS pending_message
                             ON pending_message.message_id = delivery.message_id
                           WHERE delivery.participant_id =
                                 connector.accepted_participant_id
                             AND pending_message.conversation_id =
                                 connector.conversation_id
                             AND delivery.state IN ('pending', 'delivered')
                       ) AS pending_count,
                       (
                           SELECT MIN(pending_message.created_at)
                           FROM message_deliveries AS delivery
                           JOIN messages AS pending_message
                             ON pending_message.message_id = delivery.message_id
                           WHERE delivery.participant_id =
                                 connector.accepted_participant_id
                             AND pending_message.conversation_id =
                                 connector.conversation_id
                             AND delivery.state IN ('pending', 'delivered')
                       ) AS oldest_pending_at
                       ,(
                           SELECT COUNT(*)
                           FROM message_deliveries AS delivery
                           JOIN messages AS pending_message
                             ON pending_message.message_id = delivery.message_id
                           WHERE delivery.participant_id =
                                 connector.accepted_participant_id
                             AND pending_message.conversation_id =
                                 connector.conversation_id
                             AND delivery.state IN ('pending', 'delivered')
                             AND instr(delivery.reasons_json, '"quiet_optional"') = 0
                             AND (
                                 instr(delivery.reasons_json, '"mention"') > 0
                                 OR instr(
                                     delivery.reasons_json,
                                     '"agent_request"'
                                 ) > 0
                             )
                       ) AS required_pending_count
                       ,(
                           SELECT MIN(pending_message.created_at)
                           FROM message_deliveries AS delivery
                           JOIN messages AS pending_message
                             ON pending_message.message_id = delivery.message_id
                           WHERE delivery.participant_id =
                                 connector.accepted_participant_id
                             AND pending_message.conversation_id =
                                 connector.conversation_id
                             AND delivery.state IN ('pending', 'delivered')
                             AND instr(delivery.reasons_json, '"quiet_optional"') = 0
                             AND (
                                 instr(delivery.reasons_json, '"mention"') > 0
                                 OR instr(
                                     delivery.reasons_json,
                                     '"agent_request"'
                                 ) > 0
                             )
                       ) AS oldest_required_at
                       ,(
                           SELECT COUNT(*)
                           FROM room_tasks AS task
                           WHERE task.conversation_id = connector.conversation_id
                             AND task.claimed_by_participant_id =
                                 connector.accepted_participant_id
                             AND task.status IN (
                                 'queued', 'claimed', 'running', 'needs_input'
                             )
                       ) AS active_task_count
                       ,(
                           SELECT COUNT(*)
                           FROM room_tasks AS task
                           WHERE task.conversation_id = connector.conversation_id
                             AND task.claimed_by_participant_id =
                                 connector.accepted_participant_id
                             AND task.status IN ('claimed', 'running')
                             AND task.lease_expires_at IS NOT NULL
                             AND task.lease_expires_at <= ?
                       ) AS expired_task_lease_count
                FROM agent_connectors AS connector
                JOIN agent_invitations AS invitation
                  ON invitation.invitation_id = connector.invitation_id
                JOIN participants AS participant
                  ON participant.participant_id = connector.accepted_participant_id
                LEFT JOIN agent_sessions AS session
                  ON session.connector_id = connector.connector_id
                 AND session.revoked_at IS NULL
                 AND session.cleared_at IS NULL
                WHERE connector.revoked_at IS NULL
                GROUP BY connector.connector_id
                ORDER BY connector.conversation_id,
                         participant.display_name COLLATE NOCASE,
                         connector.created_at
                """,
                (now, now, now),
            ).fetchall()
            readiness_rows = conn.execute(
                "SELECT * FROM connector_component_readiness"
            ).fetchall()
            runtime_diagnostic_rows = conn.execute(
                "SELECT * FROM connector_runtime_diagnostics"
            ).fetchall()
            component_activity_rows = conn.execute(
                """
                SELECT connector_id, component, MAX(last_seen) AS last_seen_at,
                       SUM(
                           CASE WHEN expires_at > ? THEN 1 ELSE 0 END
                       ) AS active_session_count
                FROM agent_sessions
                WHERE connector_id IS NOT NULL
                  AND revoked_at IS NULL AND cleared_at IS NULL
                GROUP BY connector_id, component
                """,
                (now,),
            ).fetchall()
            backlog_row = conn.execute(
                """
                SELECT COUNT(*) AS pending_count,
                       SUM(
                           CASE WHEN
                               instr(delivery.reasons_json, '"quiet_optional"') = 0
                               AND (
                                   instr(delivery.reasons_json, '"mention"') > 0
                                   OR instr(
                                       delivery.reasons_json,
                                       '"agent_request"'
                                   ) > 0
                               )
                           THEN 1 ELSE 0 END
                       ) AS required_pending_count,
                       MIN(message.created_at) AS oldest_pending_at,
                       MIN(
                           CASE WHEN
                               instr(delivery.reasons_json, '"quiet_optional"') = 0
                               AND (
                                   instr(delivery.reasons_json, '"mention"') > 0
                                   OR instr(
                                       delivery.reasons_json,
                                       '"agent_request"'
                                   ) > 0
                               )
                           THEN message.created_at END
                       ) AS oldest_required_at
                FROM message_deliveries AS delivery
                JOIN messages AS message
                  ON message.message_id = delivery.message_id
                WHERE delivery.state IN ('pending', 'delivered')
                  AND NOT EXISTS (
                      SELECT 1 FROM web_users AS web_user
                      WHERE web_user.participant_id = delivery.participant_id
                  )
                """
            ).fetchone()
            room_backlog_rows = conn.execute(
                """
                SELECT message.conversation_id,
                       COUNT(*) AS pending_count,
                       SUM(
                           CASE WHEN
                               instr(delivery.reasons_json, '"quiet_optional"') = 0
                               AND (
                                   instr(delivery.reasons_json, '"mention"') > 0
                                   OR instr(
                                       delivery.reasons_json,
                                       '"agent_request"'
                                   ) > 0
                               )
                           THEN 1 ELSE 0 END
                       ) AS required_pending_count,
                       MIN(message.created_at) AS oldest_pending_at
                FROM message_deliveries AS delivery
                JOIN messages AS message
                  ON message.message_id = delivery.message_id
                WHERE delivery.state IN ('pending', 'delivered')
                  AND NOT EXISTS (
                      SELECT 1 FROM web_users AS web_user
                      WHERE web_user.participant_id = delivery.participant_id
                  )
                GROUP BY message.conversation_id
                """
            ).fetchall()
            task_row = conn.execute(
                """
                SELECT
                    SUM(
                        CASE WHEN status IN (
                            'queued', 'claimed', 'running', 'needs_input'
                        ) THEN 1 ELSE 0 END
                    ) AS active_count,
                    SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END)
                        AS queued_count,
                    SUM(CASE WHEN status = 'claimed' THEN 1 ELSE 0 END)
                        AS claimed_count,
                    SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END)
                        AS running_count,
                    SUM(CASE WHEN status = 'needs_input' THEN 1 ELSE 0 END)
                        AS needs_input_count,
                    SUM(
                        CASE WHEN status IN ('claimed', 'running')
                                  AND lease_expires_at IS NOT NULL
                                  AND lease_expires_at <= ?
                             THEN 1 ELSE 0 END
                    ) AS expired_lease_count,
                    MIN(
                        CASE WHEN status IN (
                            'queued', 'claimed', 'running', 'needs_input'
                        ) THEN created_at END
                    ) AS oldest_active_at
                FROM room_tasks
                """,
                (now,),
            ).fetchone()
            task_input_row = conn.execute(
                """
                SELECT
                    SUM(
                        CASE WHEN input.first_delivered_at IS NULL
                             THEN 1 ELSE 0 END
                    ) AS undelivered_count,
                    SUM(
                        CASE WHEN input.first_delivered_at IS NOT NULL
                                  AND input.applied_at IS NULL
                             THEN 1 ELSE 0 END
                    ) AS unapplied_count
                FROM room_task_inputs AS input
                JOIN room_tasks AS task ON task.task_id = input.task_id
                WHERE task.status IN ('queued', 'claimed', 'running', 'needs_input')
                """
            ).fetchone()
            room_task_rows = conn.execute(
                """
                SELECT conversation_id,
                       SUM(
                           CASE WHEN status IN (
                               'queued', 'claimed', 'running', 'needs_input'
                           ) THEN 1 ELSE 0 END
                       ) AS active_task_count,
                       SUM(CASE WHEN status = 'needs_input' THEN 1 ELSE 0 END)
                           AS needs_input_count,
                       SUM(
                           CASE WHEN status IN ('claimed', 'running')
                                      AND lease_expires_at IS NOT NULL
                                      AND lease_expires_at <= ?
                                THEN 1 ELSE 0 END
                       ) AS expired_lease_count
                FROM room_tasks
                GROUP BY conversation_id
                """,
                (now,),
            ).fetchall()
        readiness: dict[str, dict[str, dict[str, Any]]] = {}
        for component in readiness_rows:
            readiness.setdefault(str(component["connector_id"]), {})[
                str(component["component"])
            ] = {
                "protocol_version": int(component["protocol_version"]),
                "first_seen_at": float(component["first_seen_at"]),
                "last_seen_at": float(component["last_seen_at"]),
            }
        component_activity: dict[str, dict[str, dict[str, Any]]] = {}
        for component in component_activity_rows:
            component_activity.setdefault(str(component["connector_id"]), {})[
                str(component["component"])
            ] = {
                "last_seen_at": float(component["last_seen_at"]),
                "active_session_count": int(
                    component["active_session_count"] or 0
                ),
            }
        runtime_diagnostics = {
            str(row["connector_id"]): row for row in runtime_diagnostic_rows
        }

        def diagnostic_detail(raw_value: object) -> str | None:
            try:
                parsed = json.loads(str(raw_value or "{}"))
            except (TypeError, json.JSONDecodeError):
                return None
            if not isinstance(parsed, dict):
                return None
            for key in ("error", "message", "reason", "detail"):
                value = parsed.get(key)
                if isinstance(value, (str, int, float)) and str(value).strip():
                    return str(value).strip()[:500]
            return None

        connectors: list[dict[str, Any]] = []
        for row in rows:
            connector_id = str(row["connector_id"])
            ready = sorted(readiness.get(connector_id, {}))
            required = sorted(self._connector_required_components(row))
            last_seen = (
                float(row["connector_last_seen_at"])
                if row["connector_last_seen_at"] is not None
                else None
            )
            setup_status = str(row["setup_status"])
            online = bool(
                setup_status == "configured"
                and last_seen is not None
                and now - last_seen <= CONNECTOR_ONLINE_WINDOW_SECONDS
            )
            missing_components = sorted(set(required) - set(ready))
            required_pending_count = int(row["required_pending_count"] or 0)
            oldest_required_at = (
                float(row["oldest_required_at"])
                if row["oldest_required_at"] is not None
                else None
            )
            required_age = (
                max(0.0, now - oldest_required_at)
                if oldest_required_at is not None
                else None
            )
            native_last_seen = (
                float(row["tui_last_seen_at"])
                if row["tui_last_seen_at"] is not None
                else None
            )
            native_state = str(row["tui_state"] or "unbound")
            effective_native_state = native_state
            if (
                native_state in {"online", "busy"}
                and native_last_seen is not None
                and now - native_last_seen > CONNECTOR_ONLINE_WINDOW_SECONDS
            ):
                effective_native_state = "offline"
            issues: list[dict[str, str]] = []
            runtime_row = runtime_diagnostics.get(connector_id)
            runtime_report_age = (
                max(0.0, now - float(runtime_row["reported_at"]))
                if runtime_row is not None
                else None
            )
            runtime_report_fresh = bool(
                runtime_report_age is not None
                and runtime_report_age <= CONNECTOR_ONLINE_WINDOW_SECONDS
            )

            def add_issue(code: str, severity: str, label: str) -> None:
                issues.append({"code": code, "severity": severity, "label": label})

            if setup_status == "failed":
                add_issue("setup_failed", "error", "值守配置失败")
            elif setup_status == "awaiting_setup":
                add_issue("awaiting_setup", "info", "等待完成值守配置")
            elif setup_status == "configured" and not online:
                add_issue("listener_offline", "error", "listener 超过 75 秒未探活")
            if (
                setup_status == "configured"
                and int(row["binding_version"] or 1) >= 2
                and missing_components
            ):
                add_issue(
                    "missing_components",
                    "warning",
                    "缺少组件登记：" + "、".join(missing_components),
                )
            elif setup_status == "configured" and missing_components:
                add_issue(
                    "legacy_binding",
                    "info",
                    "旧版连接会在组件自然重连后补齐登记",
                )
            if setup_status == "configured" and runtime_row is None:
                add_issue(
                    "remote_diagnostics_pending",
                    "info",
                    "远端故障详情会在 listener 自然升级后出现",
                )
            elif (
                setup_status == "configured"
                and runtime_row is not None
                and not runtime_report_fresh
            ):
                add_issue(
                    "remote_diagnostics_stale",
                    "warning",
                    "远端故障详情超过 75 秒未更新",
                )
            elif setup_status == "configured" and runtime_row is not None:
                queue_state = str(runtime_row["queue_state"])
                worker_state = str(runtime_row["worker_state"])
                if queue_state == "unavailable":
                    add_issue(
                        "remote_queue_unavailable",
                        "error",
                        "远端 supervisor 队列不可读取",
                    )
                if worker_state == "offline":
                    add_issue(
                        "remote_worker_offline",
                        "error",
                        "远端聊天 worker 心跳已中断",
                    )
                elif worker_state == "error":
                    add_issue(
                        "remote_worker_error",
                        "error",
                        "远端聊天 adapter 报告错误",
                    )
                elif worker_state == "retrying":
                    add_issue(
                        "remote_worker_retrying",
                        "warning",
                        "远端聊天 adapter 正在重试",
                    )
                elif worker_state == "unknown":
                    add_issue(
                        "remote_worker_pending",
                        "info",
                        "等待远端聊天 worker 首次探活",
                    )
                oldest_local_work_at = min(
                    (
                        float(value)
                        for value in (
                            runtime_row["queue_oldest_pending_at"],
                            runtime_row["queue_oldest_inflight_at"],
                        )
                        if value is not None
                    ),
                    default=None,
                )
                if (
                    oldest_local_work_at is not None
                    and now - oldest_local_work_at >= REMOTE_QUEUE_STALLED_SECONDS
                    and worker_state in {"offline", "error", "retrying"}
                ):
                    add_issue(
                        "remote_queue_stalled",
                        "error",
                        "远端本机队列已有事件等待超过 5 分钟",
                    )
            if setup_status == "configured" and int(row["active_session_count"] or 0) == 0:
                add_issue("session_unavailable", "warning", "没有有效 Agent 会话")
            if row["tui_adapter_kind"] is not None:
                if effective_native_state == "error":
                    add_issue("native_tui_error", "error", "真实 TUI 报告异常")
                elif effective_native_state == "offline":
                    add_issue("native_tui_offline", "error", "真实 TUI 当前不可达")
                elif effective_native_state in {
                    "unbound",
                    "awaiting_confirmation",
                    "waiting_approval",
                }:
                    add_issue("native_tui_pending", "info", "等待真实 TUI 确认")
            if (
                required_age is not None
                and required_age >= REQUIRED_REPLY_DELAY_WARNING_SECONDS
            ):
                add_issue(
                    "required_reply_delayed",
                    "warning",
                    "必须回复已等待超过 5 分钟",
                )
            if int(row["expired_task_lease_count"] or 0) > 0:
                add_issue("task_lease_expired", "warning", "存在已过期任务租约")
            if row["enrollment_rotation_required_at"] is not None:
                add_issue(
                    "credential_rotation_required",
                    "warning",
                    "设备凭证等待自动轮换",
                )

            issue_codes = {item["code"] for item in issues}
            if setup_status == "failed" or issue_codes & {
                "native_tui_error",
                "remote_queue_unavailable",
                "remote_worker_error",
                "remote_queue_stalled",
            }:
                health_state = "failed"
            elif issue_codes & {
                "listener_offline",
                "native_tui_offline",
                "remote_worker_offline",
            }:
                health_state = "offline"
            elif setup_status == "manual":
                health_state = "manual"
            elif setup_status == "awaiting_setup":
                health_state = "setup"
            elif any(
                item["severity"] in {"warning", "error"} for item in issues
            ):
                health_state = "degraded"
            else:
                health_state = "healthy"
            pending_count = int(row["pending_count"] or 0)
            connectors.append(
                {
                    "connector_id": connector_id,
                    "conversation_id": str(row["conversation_id"]),
                    "participant_id": str(row["accepted_participant_id"]),
                    "client_type": str(row["client_type"]),
                    "display_name": str(row["display_name"]),
                    "product": str(row["product"]),
                    "adapter_kind": str(row["adapter_kind"]),
                    "tui_adapter_kind": (
                        str(row["tui_adapter_kind"])
                        if row["tui_adapter_kind"] is not None
                        else None
                    ),
                    "effective_adapter_kind": str(
                        row["tui_adapter_kind"] or row["adapter_kind"]
                    ),
                    "setup_status": setup_status,
                    "diagnostic_detail": (
                        diagnostic_detail(row["setup_detail_json"])
                        if setup_status == "failed"
                        else diagnostic_detail(row["tui_detail_json"])
                        if "native_tui_error" in issue_codes
                        else None
                    ),
                    "online": online,
                    "health_state": health_state,
                    "issues": issues,
                    "connector_last_seen_at": last_seen,
                    "connector_last_seen_age_seconds": (
                        max(0.0, now - last_seen) if last_seen is not None else None
                    ),
                    "session_last_seen_at": (
                        float(row["session_last_seen_at"])
                        if row["session_last_seen_at"] is not None
                        else None
                    ),
                    "active_session_last_seen_at": (
                        float(row["active_session_last_seen_at"])
                        if row["active_session_last_seen_at"] is not None
                        else None
                    ),
                    "active_session_count": int(row["active_session_count"] or 0),
                    "last_reply_at": (
                        float(row["last_reply_at"])
                        if row["last_reply_at"] is not None
                        else None
                    ),
                    "pending_count": pending_count,
                    "optional_pending_count": max(
                        0,
                        pending_count - required_pending_count,
                    ),
                    "required_pending_count": required_pending_count,
                    "oldest_pending_at": (
                        float(row["oldest_pending_at"])
                        if row["oldest_pending_at"] is not None
                        else None
                    ),
                    "oldest_required_at": oldest_required_at,
                    "oldest_required_age_seconds": required_age,
                    "active_task_count": int(row["active_task_count"] or 0),
                    "expired_task_lease_count": int(
                        row["expired_task_lease_count"] or 0
                    ),
                    "binding_version": int(row["binding_version"] or 1),
                    "enrollment": {
                        "credential_version": int(
                            row["enrollment_credential_version"] or 1
                        ),
                        "rotation_count": int(
                            row["enrollment_rotation_count"] or 0
                        ),
                        "last_used_at": (
                            float(row["enrollment_last_used_at"])
                            if row["enrollment_last_used_at"] is not None
                            else None
                        ),
                        "rotated_at": (
                            float(row["enrollment_rotated_at"])
                            if row["enrollment_rotated_at"] is not None
                            else None
                        ),
                        "credential_age_seconds": max(
                            0.0,
                            now
                            - float(
                                row["enrollment_rotated_at"]
                                or row["created_at"]
                            ),
                        ),
                        "rotation_required": (
                            row["enrollment_rotation_required_at"] is not None
                        ),
                        "rotation_required_at": (
                            float(row["enrollment_rotation_required_at"])
                            if row["enrollment_rotation_required_at"] is not None
                            else None
                        ),
                        "previous_valid_until": (
                            float(row["previous_enrollment_valid_until"])
                            if row["previous_enrollment_valid_until"] is not None
                            else None
                        ),
                    },
                    "ready_components": ready,
                    "missing_components": missing_components,
                    "component_registration": readiness.get(connector_id, {}),
                    "component_activity": component_activity.get(connector_id, {}),
                    "runtime_diagnostics": (
                        {
                            "available": True,
                            "protocol_version": int(
                                runtime_row["protocol_version"]
                            ),
                            "software_version": str(
                                runtime_row["software_version"]
                            ),
                            "platform": str(runtime_row["platform"]),
                            "reported_at": float(runtime_row["reported_at"]),
                            "report_age_seconds": runtime_report_age,
                            "fresh": runtime_report_fresh,
                            "listener": {
                                "state": str(runtime_row["listener_state"]),
                            },
                            "queue": {
                                "state": str(runtime_row["queue_state"]),
                                "pending_count": int(
                                    runtime_row["queue_pending_count"] or 0
                                ),
                                "inflight_count": int(
                                    runtime_row["queue_inflight_count"] or 0
                                ),
                                "deferred_count": int(
                                    runtime_row["queue_deferred_count"] or 0
                                ),
                                "retrying_count": int(
                                    runtime_row["queue_retrying_count"] or 0
                                ),
                                "max_attempt_count": int(
                                    runtime_row["queue_max_attempt_count"] or 0
                                ),
                                "oldest_pending_age_seconds": (
                                    max(
                                        0.0,
                                        now
                                        - float(
                                            runtime_row[
                                                "queue_oldest_pending_at"
                                            ]
                                        ),
                                    )
                                    if runtime_row[
                                        "queue_oldest_pending_at"
                                    ] is not None
                                    else None
                                ),
                                "oldest_inflight_age_seconds": (
                                    max(
                                        0.0,
                                        now
                                        - float(
                                            runtime_row[
                                                "queue_oldest_inflight_at"
                                            ]
                                        ),
                                    )
                                    if runtime_row[
                                        "queue_oldest_inflight_at"
                                    ] is not None
                                    else None
                                ),
                                "newest_event_id": (
                                    int(runtime_row["newest_event_id"])
                                    if runtime_row["newest_event_id"] is not None
                                    else None
                                ),
                            },
                            "worker": {
                                "kind": str(runtime_row["worker_kind"]),
                                "state": str(runtime_row["worker_state"]),
                                "process_epoch": (
                                    str(runtime_row["worker_process_epoch"])
                                    if runtime_row["worker_process_epoch"] is not None
                                    else None
                                ),
                                "started_at": (
                                    float(runtime_row["worker_started_at"])
                                    if runtime_row["worker_started_at"] is not None
                                    else None
                                ),
                                "last_seen_at": (
                                    float(runtime_row["worker_last_seen_at"])
                                    if runtime_row["worker_last_seen_at"] is not None
                                    else None
                                ),
                                "last_seen_age_seconds": (
                                    max(
                                        0.0,
                                        now - float(runtime_row["worker_last_seen_at"]),
                                    )
                                    if runtime_row["worker_last_seen_at"] is not None
                                    else None
                                ),
                                "last_success_at": (
                                    float(runtime_row["worker_last_success_at"])
                                    if runtime_row["worker_last_success_at"] is not None
                                    else None
                                ),
                                "last_failure_at": (
                                    float(runtime_row["worker_last_failure_at"])
                                    if runtime_row["worker_last_failure_at"] is not None
                                    else None
                                ),
                                "last_error_code": (
                                    str(runtime_row["worker_last_error_code"])
                                    if runtime_row["worker_last_error_code"] is not None
                                    else None
                                ),
                                "active_adapter_runs": int(
                                    runtime_row["active_adapter_runs"] or 0
                                ),
                            },
                        }
                        if runtime_row is not None
                        else {
                            "available": False,
                            "fresh": False,
                        }
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
                        "state": native_state,
                        "effective_state": effective_native_state,
                        "last_seen_at": native_last_seen,
                        "last_seen_age_seconds": (
                            max(0.0, now - native_last_seen)
                            if native_last_seen is not None
                            else None
                        ),
                        "active_task_id": (
                            str(row["tui_active_task_id"])
                            if row["tui_active_task_id"] is not None
                            else None
                        ),
                    },
                }
            )
        status_counts = {
            status: sum(item["health_state"] == status for item in connectors)
            for status in (
                "healthy",
                "degraded",
                "offline",
                "failed",
                "setup",
                "manual",
            )
        }
        room_health: dict[str, dict[str, Any]] = {}
        for row in room_backlog_rows:
            pending = int(row["pending_count"] or 0)
            required_pending = int(row["required_pending_count"] or 0)
            room_health[str(row["conversation_id"])] = {
                "conversation_id": str(row["conversation_id"]),
                "pending_count": pending,
                "required_pending_count": required_pending,
                "optional_pending_count": max(0, pending - required_pending),
                "oldest_pending_at": (
                    float(row["oldest_pending_at"])
                    if row["oldest_pending_at"] is not None
                    else None
                ),
                "active_task_count": 0,
                "needs_input_count": 0,
                "expired_lease_count": 0,
            }
        for row in room_task_rows:
            conversation = str(row["conversation_id"])
            payload = room_health.setdefault(
                conversation,
                {
                    "conversation_id": conversation,
                    "pending_count": 0,
                    "required_pending_count": 0,
                    "optional_pending_count": 0,
                    "oldest_pending_at": None,
                    "active_task_count": 0,
                    "needs_input_count": 0,
                    "expired_lease_count": 0,
                },
            )
            payload.update(
                {
                    "active_task_count": int(row["active_task_count"] or 0),
                    "needs_input_count": int(row["needs_input_count"] or 0),
                    "expired_lease_count": int(row["expired_lease_count"] or 0),
                }
            )
        pending_total = int(backlog_row["pending_count"] or 0)
        required_total = int(backlog_row["required_pending_count"] or 0)
        return {
            "connectors": connectors,
            "count": len(connectors),
            "online_count": sum(item["online"] for item in connectors),
            "attention_count": sum(
                item["health_state"] in {"degraded", "offline", "failed", "setup"}
                for item in connectors
            ),
            "status_counts": status_counts,
            "binding_v2_count": sum(
                item["binding_version"] >= 2 for item in connectors
            ),
            "runtime_diagnostic_count": sum(
                bool(item["runtime_diagnostics"].get("available"))
                for item in connectors
            ),
            "fresh_runtime_diagnostic_count": sum(
                bool(item["runtime_diagnostics"].get("fresh"))
                for item in connectors
            ),
            "backlog": {
                "pending_count": pending_total,
                "required_pending_count": required_total,
                "optional_pending_count": max(0, pending_total - required_total),
                "oldest_pending_at": (
                    float(backlog_row["oldest_pending_at"])
                    if backlog_row["oldest_pending_at"] is not None
                    else None
                ),
                "oldest_required_at": (
                    float(backlog_row["oldest_required_at"])
                    if backlog_row["oldest_required_at"] is not None
                    else None
                ),
            },
            "tasks": {
                "active_count": int(task_row["active_count"] or 0),
                "queued_count": int(task_row["queued_count"] or 0),
                "claimed_count": int(task_row["claimed_count"] or 0),
                "running_count": int(task_row["running_count"] or 0),
                "needs_input_count": int(task_row["needs_input_count"] or 0),
                "expired_lease_count": int(task_row["expired_lease_count"] or 0),
                "oldest_active_at": (
                    float(task_row["oldest_active_at"])
                    if task_row["oldest_active_at"] is not None
                    else None
                ),
                "undelivered_input_count": int(
                    task_input_row["undelivered_count"] or 0
                ),
                "unapplied_input_count": int(
                    task_input_row["unapplied_count"] or 0
                ),
            },
            "rooms": sorted(
                room_health.values(),
                key=lambda item: (
                    -int(item["required_pending_count"]),
                    -int(item["expired_lease_count"]),
                    -int(item["active_task_count"]),
                    -int(item["pending_count"]),
                    str(item["conversation_id"]),
                ),
            ),
            "required_reply_warning_seconds": (
                REQUIRED_REPLY_DELAY_WARNING_SECONDS
            ),
            "diagnostic_scope": (
                "Central Bridge facts plus sanitized remote listener, supervisor "
                "queue, and adapter status codes. No remote logs, paths, message "
                "content, credentials, or TUI permissions are collected."
            ),
            "server_time": now,
        }
