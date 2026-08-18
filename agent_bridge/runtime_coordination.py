"""Multi-viewer runtime leadership and lease coordination."""

from __future__ import annotations

import time
from typing import Any

from .operational_monitoring import (
    acknowledge_operational_alert as acknowledge_monitoring_alert,
    operational_monitoring_dashboard as build_operational_monitoring_dashboard,
    record_operational_sample as persist_operational_sample,
)
from .store_constants import (
    RUNTIME_HEARTBEAT_INTERVAL_SECONDS,
    RUNTIME_INSTANCE_ACTIVE_SECONDS,
    RUNTIME_INSTANCE_RETENTION_SECONDS,
    RUNTIME_LEASE_TTL_SECONDS,
)
from .store_errors import AuthenticationError, NotFoundError
from .validation import ValidationError, opaque_id


RUNTIME_COORDINATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS bridge_runtime_instances (
    instance_id TEXT PRIMARY KEY,
    service_kind TEXT NOT NULL,
    node_name TEXT NOT NULL,
    process_id INTEGER NOT NULL CHECK (process_id > 0),
    software_version TEXT NOT NULL,
    started_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    stopped_at REAL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_bridge_runtime_instances_activity
    ON bridge_runtime_instances(stopped_at, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS bridge_runtime_leases (
    lease_name TEXT PRIMARY KEY,
    holder_instance_id TEXT,
    fencing_token INTEGER NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
    acquired_at REAL,
    renewed_at REAL,
    expires_at REAL,
    FOREIGN KEY (holder_instance_id)
        REFERENCES bridge_runtime_instances(instance_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_bridge_runtime_leases_holder
    ON bridge_runtime_leases(holder_instance_id, expires_at);

CREATE TABLE IF NOT EXISTS shared_request_rate_windows (
    bucket TEXT NOT NULL,
    subject_hash TEXT NOT NULL,
    events_json TEXT NOT NULL DEFAULT '[]',
    last_seen_at REAL NOT NULL,
    PRIMARY KEY (bucket, subject_hash)
);

CREATE INDEX IF NOT EXISTS idx_shared_request_rate_windows_activity
    ON shared_request_rate_windows(last_seen_at);
"""


class RuntimeCoordinationMixin:
    def coordinate_runtime_instance(
        self,
        *,
        instance_id: str,
        node_name: str,
        process_id: int,
        software_version: str,
        service_kind: str = "viewer",
        lease_name: str = "viewer-maintenance",
        lease_ttl_seconds: float = RUNTIME_LEASE_TTL_SECONDS,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Heartbeat one process and atomically acquire or renew its lease."""

        normalized_instance_id = opaque_id(instance_id, field="instance_id")
        normalized_lease_name = opaque_id(lease_name, field="lease_name")
        normalized_node_name = str(node_name or "").strip()[:255]
        normalized_service_kind = str(service_kind or "").strip()[:64]
        normalized_version = str(software_version or "").strip()[:64]
        normalized_process_id = int(process_id)
        ttl = max(5.0, min(float(lease_ttl_seconds), 300.0))
        current_time = time.time() if now is None else float(now)
        if not normalized_node_name:
            raise ValidationError("node_name is required")
        if not normalized_service_kind:
            raise ValidationError("service_kind is required")
        if not normalized_version:
            raise ValidationError("software_version is required")
        if normalized_process_id <= 0:
            raise ValidationError("process_id must be positive")

        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO bridge_runtime_instances (
                    instance_id, service_kind, node_name, process_id,
                    software_version, started_at, last_seen_at,
                    stopped_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, '{}')
                ON CONFLICT(instance_id) DO UPDATE SET
                    service_kind = excluded.service_kind,
                    node_name = excluded.node_name,
                    process_id = excluded.process_id,
                    software_version = excluded.software_version,
                    last_seen_at = excluded.last_seen_at,
                    stopped_at = NULL
                """,
                (
                    normalized_instance_id,
                    normalized_service_kind,
                    normalized_node_name,
                    normalized_process_id,
                    normalized_version,
                    current_time,
                    current_time,
                ),
            )
            lease = conn.execute(
                "SELECT * FROM bridge_runtime_leases WHERE lease_name = ?",
                (normalized_lease_name,),
            ).fetchone()
            leader = False
            if lease is None:
                conn.execute(
                    """
                    INSERT INTO bridge_runtime_leases (
                        lease_name, holder_instance_id, fencing_token,
                        acquired_at, renewed_at, expires_at
                    ) VALUES (?, ?, 1, ?, ?, ?)
                    """,
                    (
                        normalized_lease_name,
                        normalized_instance_id,
                        current_time,
                        current_time,
                        current_time + ttl,
                    ),
                )
                leader = True
            else:
                holder = (
                    str(lease["holder_instance_id"])
                    if lease["holder_instance_id"] is not None
                    else None
                )
                expires_at = (
                    float(lease["expires_at"])
                    if lease["expires_at"] is not None
                    else 0.0
                )
                if (
                    holder == normalized_instance_id
                    and expires_at > current_time
                ):
                    conn.execute(
                        """
                        UPDATE bridge_runtime_leases
                        SET renewed_at = ?, expires_at = ?
                        WHERE lease_name = ? AND holder_instance_id = ?
                        """,
                        (
                            current_time,
                            current_time + ttl,
                            normalized_lease_name,
                            normalized_instance_id,
                        ),
                    )
                    leader = True
                elif holder is None or expires_at <= current_time:
                    conn.execute(
                        """
                        UPDATE bridge_runtime_leases
                        SET holder_instance_id = ?,
                            fencing_token = fencing_token + 1,
                            acquired_at = ?, renewed_at = ?, expires_at = ?
                        WHERE lease_name = ?
                        """,
                        (
                            normalized_instance_id,
                            current_time,
                            current_time,
                            current_time + ttl,
                            normalized_lease_name,
                        ),
                    )
                    leader = True
            conn.execute(
                """
                DELETE FROM bridge_runtime_instances
                WHERE COALESCE(stopped_at, last_seen_at) <= ?
                  AND instance_id NOT IN (
                      SELECT holder_instance_id
                      FROM bridge_runtime_leases
                      WHERE holder_instance_id IS NOT NULL
                  )
                """,
                (current_time - RUNTIME_INSTANCE_RETENTION_SECONDS,),
            )
            lease = conn.execute(
                "SELECT fencing_token, expires_at "
                "FROM bridge_runtime_leases WHERE lease_name = ?",
                (normalized_lease_name,),
            ).fetchone()
        return {
            "instance_id": normalized_instance_id,
            "lease_name": normalized_lease_name,
            "leader": leader,
            "fencing_token": int(lease["fencing_token"]),
            "lease_expires_at": float(lease["expires_at"]),
            "heartbeat_interval_seconds": RUNTIME_HEARTBEAT_INTERVAL_SECONDS,
            "lease_ttl_seconds": ttl,
            "server_time": current_time,
        }

    def stop_runtime_instance(
        self,
        *,
        instance_id: str,
        now: float | None = None,
    ) -> None:
        normalized_instance_id = opaque_id(instance_id, field="instance_id")
        current_time = time.time() if now is None else float(now)
        with self._transaction() as conn:
            conn.execute(
                """
                UPDATE bridge_runtime_leases
                SET holder_instance_id = NULL,
                    renewed_at = ?, expires_at = ?
                WHERE holder_instance_id = ?
                """,
                (current_time, current_time, normalized_instance_id),
            )
            conn.execute(
                """
                UPDATE bridge_runtime_instances
                SET last_seen_at = ?, stopped_at = ?
                WHERE instance_id = ?
                """,
                (current_time, current_time, normalized_instance_id),
            )

    def runtime_coordination_status(
        self,
        *,
        current_instance_id: str | None = None,
        lease_name: str = "viewer-maintenance",
        now: float | None = None,
    ) -> dict[str, Any]:
        current_time = time.time() if now is None else float(now)
        normalized_current = (
            opaque_id(current_instance_id, field="current_instance_id")
            if current_instance_id is not None
            else None
        )
        normalized_lease_name = opaque_id(lease_name, field="lease_name")
        active_after = current_time - RUNTIME_INSTANCE_ACTIVE_SECONDS
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM bridge_runtime_instances
                ORDER BY last_seen_at DESC
                LIMIT 50
                """
            ).fetchall()
            lease = conn.execute(
                "SELECT * FROM bridge_runtime_leases WHERE lease_name = ?",
                (normalized_lease_name,),
            ).fetchone()
        instances = []
        for row in rows:
            active = (
                row["stopped_at"] is None
                and float(row["last_seen_at"]) > active_after
            )
            instances.append(
                {
                    "instance_id": str(row["instance_id"]),
                    "service_kind": str(row["service_kind"]),
                    "node_name": str(row["node_name"]),
                    "process_id": int(row["process_id"]),
                    "software_version": str(row["software_version"]),
                    "started_at": float(row["started_at"]),
                    "last_seen_at": float(row["last_seen_at"]),
                    "stopped_at": (
                        float(row["stopped_at"])
                        if row["stopped_at"] is not None
                        else None
                    ),
                    "active": active,
                    "current": str(row["instance_id"]) == normalized_current,
                }
            )
        holder_instance_id = (
            str(lease["holder_instance_id"])
            if lease is not None and lease["holder_instance_id"] is not None
            else None
        )
        lease_expires_at = (
            float(lease["expires_at"])
            if lease is not None and lease["expires_at"] is not None
            else None
        )
        active_ids = {
            str(instance["instance_id"])
            for instance in instances
            if instance["active"]
        }
        leader_healthy = bool(
            holder_instance_id in active_ids
            and lease_expires_at is not None
            and lease_expires_at > current_time
        )
        return {
            "coordination_mode": "sqlite_lease",
            "storage_backend": "sqlite",
            "deployment_scope": "single_node",
            "multi_host_ha_ready": False,
            "shared_request_rate_limits": True,
            "heartbeat_interval_seconds": RUNTIME_HEARTBEAT_INTERVAL_SECONDS,
            "active_instance_timeout_seconds": RUNTIME_INSTANCE_ACTIVE_SECONDS,
            "active_instance_count": len(active_ids),
            "leader_healthy": leader_healthy,
            "current_instance_id": normalized_current,
            "current_role": (
                "leader"
                if leader_healthy and holder_instance_id == normalized_current
                else "follower"
            ),
            "lease": {
                "lease_name": normalized_lease_name,
                "holder_instance_id": holder_instance_id,
                "fencing_token": (
                    int(lease["fencing_token"]) if lease is not None else 0
                ),
                "acquired_at": (
                    float(lease["acquired_at"])
                    if lease is not None and lease["acquired_at"] is not None
                    else None
                ),
                "renewed_at": (
                    float(lease["renewed_at"])
                    if lease is not None and lease["renewed_at"] is not None
                    else None
                ),
                "expires_at": lease_expires_at,
            },
            "instances": instances,
            "server_time": current_time,
        }

    def record_operational_sample(self) -> dict[str, Any]:
        return persist_operational_sample(
            self,
            authentication_error=AuthenticationError,
        )

    def operational_monitoring_dashboard(
        self,
        *,
        requesting_web_user_id: str,
        hours: object = 24,
    ) -> dict[str, Any]:
        return build_operational_monitoring_dashboard(
            self,
            requesting_web_user_id=requesting_web_user_id,
            hours=hours,
        )

    def acknowledge_operational_alert(
        self,
        *,
        alert_id: str,
        acknowledged_by_web_user_id: str,
    ) -> dict[str, Any]:
        return acknowledge_monitoring_alert(
            self,
            alert_id=alert_id,
            acknowledged_by_web_user_id=acknowledged_by_web_user_id,
            not_found_error=NotFoundError,
        )
