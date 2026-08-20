"""Sanitized runtime diagnostics reported by remote connector listeners."""

from __future__ import annotations

import math
import time
from typing import Any

from .store_errors import AuthenticationError, AuthorizationError, NotFoundError
from .validation import ValidationError, opaque_id, token


CONNECTOR_RUNTIME_DIAGNOSTICS_SCHEMA = """
CREATE TABLE IF NOT EXISTS connector_runtime_diagnostics (
    connector_id TEXT PRIMARY KEY,
    participant_id TEXT NOT NULL,
    reporter_component TEXT NOT NULL
        CHECK (reporter_component = 'listener'),
    protocol_version INTEGER NOT NULL
        CHECK (protocol_version >= 1),
    software_version TEXT NOT NULL,
    platform TEXT NOT NULL,
    listener_state TEXT NOT NULL
        CHECK (listener_state IN ('online', 'degraded', 'error')),
    queue_state TEXT NOT NULL
        CHECK (queue_state IN ('ready', 'unavailable')),
    queue_pending_count INTEGER NOT NULL DEFAULT 0
        CHECK (queue_pending_count >= 0),
    queue_inflight_count INTEGER NOT NULL DEFAULT 0
        CHECK (queue_inflight_count >= 0),
    queue_deferred_count INTEGER NOT NULL DEFAULT 0
        CHECK (queue_deferred_count >= 0),
    queue_retrying_count INTEGER NOT NULL DEFAULT 0
        CHECK (queue_retrying_count >= 0),
    queue_max_attempt_count INTEGER NOT NULL DEFAULT 0
        CHECK (queue_max_attempt_count >= 0),
    queue_oldest_pending_at REAL,
    queue_oldest_inflight_at REAL,
    newest_event_id INTEGER,
    worker_kind TEXT NOT NULL,
    worker_state TEXT NOT NULL
        CHECK (worker_state IN (
            'idle', 'busy', 'retrying', 'error', 'offline', 'unknown'
        )),
    worker_process_epoch TEXT,
    worker_started_at REAL,
    worker_last_seen_at REAL,
    worker_last_success_at REAL,
    worker_last_failure_at REAL,
    worker_last_error_code TEXT,
    active_adapter_runs INTEGER NOT NULL DEFAULT 0
        CHECK (active_adapter_runs >= 0),
    reported_at REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (connector_id) REFERENCES agent_connectors(connector_id),
    FOREIGN KEY (participant_id) REFERENCES participants(participant_id)
);

CREATE INDEX IF NOT EXISTS idx_connector_runtime_diagnostics_reported
    ON connector_runtime_diagnostics(reported_at DESC);
"""


RUNTIME_LISTENER_STATES = {"online", "degraded", "error"}
RUNTIME_QUEUE_STATES = {"ready", "unavailable"}
RUNTIME_WORKER_STATES = {
    "idle",
    "busy",
    "retrying",
    "error",
    "offline",
    "unknown",
}
RUNTIME_WORKER_KINDS = {"codex-worker", "supervisor", "unknown"}
RUNTIME_ERROR_CODES = {
    "adapter_contract_error",
    "adapter_exit",
    "adapter_missing",
    "adapter_session_error",
    "adapter_timeout",
    "adapter_unknown",
    "queue_unavailable",
    "worker_restarted",
}
MAX_RUNTIME_COUNT = 10_000_000
MAX_RUNTIME_AGE_SECONDS = 366 * 24 * 60 * 60


def _exact_object(
    value: object,
    *,
    field: str,
    allowed: set[str],
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{field} must be an object")
    extras = set(value) - allowed
    if extras:
        raise ValidationError(
            f"{field} contains unsupported fields: {', '.join(sorted(extras))}"
        )
    return value


def _bounded_count(value: object, *, field: str) -> int:
    if isinstance(value, bool):
        raise ValidationError(f"{field} must be a non-negative integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} must be a non-negative integer") from exc
    if str(value).strip() != str(normalized) and not isinstance(value, int):
        raise ValidationError(f"{field} must be a non-negative integer")
    if not 0 <= normalized <= MAX_RUNTIME_COUNT:
        raise ValidationError(f"{field} is outside the supported range")
    return normalized


def _bounded_age(value: object, *, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValidationError(f"{field} must be a finite non-negative number")
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            f"{field} must be a finite non-negative number"
        ) from exc
    if not math.isfinite(normalized) or not 0 <= normalized <= MAX_RUNTIME_AGE_SECONDS:
        raise ValidationError(f"{field} is outside the supported range")
    return normalized


def _age_to_timestamp(now: float, age: float | None) -> float | None:
    return None if age is None else max(0.0, now - age)


class ConnectorRuntimeDiagnosticsMixin:
    """Persist one latest, structured report per durable connector."""

    def report_connector_runtime_diagnostics(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        connector_id: str,
        protocol_version: object,
        software_version: object,
        platform: object,
        listener_state: object,
        queue: object,
        worker: object,
    ) -> dict[str, Any]:
        participant = opaque_id(participant_id, field="participant_id")
        session_id = opaque_id(
            authorized_session_id,
            field="authorized_session_id",
        )
        connector = opaque_id(connector_id, field="connector_id")
        if isinstance(protocol_version, bool):
            raise ValidationError("runtime diagnostic protocol must be an integer")
        try:
            protocol = int(protocol_version)
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "runtime diagnostic protocol must be an integer"
            ) from exc
        if protocol != 1:
            raise ValidationError("unsupported runtime diagnostic protocol")
        version = token(str(software_version or ""), field="software_version")
        if len(version) > 64:
            raise ValidationError("software_version is too long")
        normalized_platform = token(str(platform or ""), field="platform")
        if normalized_platform not in {"Darwin", "Linux", "Windows", "Other"}:
            raise ValidationError("unsupported runtime diagnostic platform")
        normalized_listener = str(listener_state or "").strip().lower()
        if normalized_listener not in RUNTIME_LISTENER_STATES:
            raise ValidationError("unsupported listener diagnostic state")

        queue_payload = _exact_object(
            queue,
            field="queue",
            allowed={
                "state",
                "pending_count",
                "inflight_count",
                "deferred_count",
                "retrying_count",
                "max_attempt_count",
                "oldest_pending_age_seconds",
                "oldest_inflight_age_seconds",
                "newest_event_id",
            },
        )
        queue_state = str(queue_payload.get("state") or "").strip().lower()
        if queue_state not in RUNTIME_QUEUE_STATES:
            raise ValidationError("unsupported queue diagnostic state")
        pending_count = _bounded_count(
            queue_payload.get("pending_count", 0),
            field="queue.pending_count",
        )
        inflight_count = _bounded_count(
            queue_payload.get("inflight_count", 0),
            field="queue.inflight_count",
        )
        deferred_count = _bounded_count(
            queue_payload.get("deferred_count", 0),
            field="queue.deferred_count",
        )
        retrying_count = _bounded_count(
            queue_payload.get("retrying_count", 0),
            field="queue.retrying_count",
        )
        max_attempt_count = _bounded_count(
            queue_payload.get("max_attempt_count", 0),
            field="queue.max_attempt_count",
        )
        oldest_pending_age = _bounded_age(
            queue_payload.get("oldest_pending_age_seconds"),
            field="queue.oldest_pending_age_seconds",
        )
        oldest_inflight_age = _bounded_age(
            queue_payload.get("oldest_inflight_age_seconds"),
            field="queue.oldest_inflight_age_seconds",
        )
        newest_event_id = queue_payload.get("newest_event_id")
        if newest_event_id is not None:
            newest_event_id = _bounded_count(
                newest_event_id,
                field="queue.newest_event_id",
            )

        worker_payload = _exact_object(
            worker,
            field="worker",
            allowed={
                "kind",
                "state",
                "process_epoch",
                "started_age_seconds",
                "last_seen_age_seconds",
                "last_success_age_seconds",
                "last_failure_age_seconds",
                "last_error_code",
                "active_adapter_runs",
            },
        )
        worker_kind = str(worker_payload.get("kind") or "unknown").strip().lower()
        if worker_kind not in RUNTIME_WORKER_KINDS:
            raise ValidationError("unsupported worker diagnostic kind")
        worker_state = str(worker_payload.get("state") or "unknown").strip().lower()
        if worker_state not in RUNTIME_WORKER_STATES:
            raise ValidationError("unsupported worker diagnostic state")
        process_epoch_raw = str(worker_payload.get("process_epoch") or "").strip()
        process_epoch = (
            opaque_id(process_epoch_raw, field="worker.process_epoch")
            if process_epoch_raw
            else None
        )
        started_age = _bounded_age(
            worker_payload.get("started_age_seconds"),
            field="worker.started_age_seconds",
        )
        last_seen_age = _bounded_age(
            worker_payload.get("last_seen_age_seconds"),
            field="worker.last_seen_age_seconds",
        )
        last_success_age = _bounded_age(
            worker_payload.get("last_success_age_seconds"),
            field="worker.last_success_age_seconds",
        )
        last_failure_age = _bounded_age(
            worker_payload.get("last_failure_age_seconds"),
            field="worker.last_failure_age_seconds",
        )
        error_code_raw = str(worker_payload.get("last_error_code") or "").strip()
        error_code = error_code_raw or None
        if error_code is not None and error_code not in RUNTIME_ERROR_CODES:
            raise ValidationError("unsupported worker diagnostic error code")
        active_adapter_runs = _bounded_count(
            worker_payload.get("active_adapter_runs", 0),
            field="worker.active_adapter_runs",
        )

        now = time.time()
        with self._transaction() as conn:
            session = self._require_live_session(
                conn,
                session_id=session_id,
                participant_id=participant,
                now=now,
            )
            if str(session["connector_id"] or "") != connector:
                raise AuthenticationError("connector does not belong to this session")
            if str(session["component"] or "") != "listener":
                raise AuthorizationError(
                    "only the authenticated connector listener may report runtime diagnostics"
                )
            active_connector = conn.execute(
                "SELECT 1 FROM agent_connectors WHERE connector_id = ? "
                "AND accepted_participant_id = ? AND revoked_at IS NULL",
                (connector, participant),
            ).fetchone()
            if active_connector is None:
                raise NotFoundError("active connector invitation was not found")
            conn.execute(
                """
                INSERT INTO connector_runtime_diagnostics (
                    connector_id, participant_id, reporter_component,
                    protocol_version, software_version, platform, listener_state,
                    queue_state, queue_pending_count, queue_inflight_count,
                    queue_deferred_count, queue_retrying_count,
                    queue_max_attempt_count, queue_oldest_pending_at,
                    queue_oldest_inflight_at, newest_event_id, worker_kind,
                    worker_state, worker_process_epoch, worker_started_at,
                    worker_last_seen_at, worker_last_success_at,
                    worker_last_failure_at, worker_last_error_code,
                    active_adapter_runs, reported_at, created_at, updated_at
                ) VALUES (
                    ?, ?, 'listener', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(connector_id) DO UPDATE SET
                    participant_id = excluded.participant_id,
                    reporter_component = excluded.reporter_component,
                    protocol_version = excluded.protocol_version,
                    software_version = excluded.software_version,
                    platform = excluded.platform,
                    listener_state = excluded.listener_state,
                    queue_state = excluded.queue_state,
                    queue_pending_count = excluded.queue_pending_count,
                    queue_inflight_count = excluded.queue_inflight_count,
                    queue_deferred_count = excluded.queue_deferred_count,
                    queue_retrying_count = excluded.queue_retrying_count,
                    queue_max_attempt_count = excluded.queue_max_attempt_count,
                    queue_oldest_pending_at = excluded.queue_oldest_pending_at,
                    queue_oldest_inflight_at = excluded.queue_oldest_inflight_at,
                    newest_event_id = excluded.newest_event_id,
                    worker_kind = excluded.worker_kind,
                    worker_state = excluded.worker_state,
                    worker_process_epoch = excluded.worker_process_epoch,
                    worker_started_at = excluded.worker_started_at,
                    worker_last_seen_at = excluded.worker_last_seen_at,
                    worker_last_success_at = excluded.worker_last_success_at,
                    worker_last_failure_at = excluded.worker_last_failure_at,
                    worker_last_error_code = excluded.worker_last_error_code,
                    active_adapter_runs = excluded.active_adapter_runs,
                    reported_at = excluded.reported_at,
                    updated_at = excluded.updated_at
                """,
                (
                    connector,
                    participant,
                    protocol,
                    version,
                    normalized_platform,
                    normalized_listener,
                    queue_state,
                    pending_count,
                    inflight_count,
                    deferred_count,
                    retrying_count,
                    max_attempt_count,
                    _age_to_timestamp(now, oldest_pending_age),
                    _age_to_timestamp(now, oldest_inflight_age),
                    newest_event_id,
                    worker_kind,
                    worker_state,
                    process_epoch,
                    _age_to_timestamp(now, started_age),
                    _age_to_timestamp(now, last_seen_age),
                    _age_to_timestamp(now, last_success_age),
                    _age_to_timestamp(now, last_failure_age),
                    error_code,
                    active_adapter_runs,
                    now,
                    now,
                    now,
                ),
            )
            conn.execute(
                "UPDATE agent_connectors SET connector_last_seen_at = ?, "
                "updated_at = ? WHERE connector_id = ?",
                (now, now, connector),
            )
        return {
            "connector_id": connector,
            "protocol_version": protocol,
            "reported_at": now,
            "accepted": True,
        }
