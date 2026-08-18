"""Exact native TUI lease, event routing, and delivery-stage state."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Sequence
from typing import Any

from .store_constants import (
    NATIVE_CHANNEL_MAX_MESSAGES,
    NATIVE_CHANNEL_MAX_WAIT_SECONDS,
    NATIVE_SESSION_LEASE_SECONDS,
    NATIVE_TUI_ADAPTERS,
    TUI_STATES,
)
from .store_errors import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    NotFoundError,
)
from .validation import ValidationError, compact_json, opaque_id, string_tokens


NATIVE_SESSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS native_session_leases (
    lease_id TEXT PRIMARY KEY,
    connector_id TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    tui_endpoint_id TEXT NOT NULL,
    native_session_id TEXT NOT NULL,
    process_epoch TEXT NOT NULL,
    binding_source TEXT NOT NULL
        CHECK (binding_source IN ('startup', 'resume')),
    started_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    ended_at REAL,
    superseded_at REAL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (connector_id) REFERENCES agent_connectors(connector_id),
    FOREIGN KEY (participant_id) REFERENCES participants(participant_id)
);

CREATE INDEX IF NOT EXISTS idx_native_session_leases_connector_activity
    ON native_session_leases(connector_id, ended_at, expires_at DESC);
CREATE INDEX IF NOT EXISTS idx_native_session_leases_identity
    ON native_session_leases(
        tui_endpoint_id, native_session_id, process_epoch, expires_at DESC
    );

CREATE TABLE IF NOT EXISTS native_channel_events (
    event_id TEXT PRIMARY KEY,
    connector_id TEXT NOT NULL,
    lease_id TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    message_ids_json TEXT NOT NULL DEFAULT '[]',
    route_token_hash TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'fetched'
        CHECK (state IN (
            'fetched', 'injected', 'applied', 'replied',
            'superseded', 'cancelled'
        )),
    fetched_at REAL NOT NULL,
    injected_at REAL,
    applied_at REAL,
    replied_at REAL,
    superseded_at REAL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (connector_id) REFERENCES agent_connectors(connector_id),
    FOREIGN KEY (lease_id) REFERENCES native_session_leases(lease_id),
    FOREIGN KEY (participant_id) REFERENCES participants(participant_id),
    FOREIGN KEY (conversation_id) REFERENCES rooms(conversation_id)
);

CREATE INDEX IF NOT EXISTS idx_native_channel_events_connector_state
    ON native_channel_events(connector_id, state, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_native_channel_events_lease_state
    ON native_channel_events(lease_id, state, fetched_at DESC);
"""


class NativeSessionMixin:
    @staticmethod
    def _native_session_lease_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "lease_id": str(row["lease_id"]),
            "connector_id": str(row["connector_id"]),
            "participant_id": str(row["participant_id"]),
            "tui_endpoint_id": str(row["tui_endpoint_id"]),
            "native_session_id": str(row["native_session_id"]),
            "process_epoch": str(row["process_epoch"]),
            "binding_source": str(row["binding_source"]),
            "started_at": float(row["started_at"]),
            "last_seen_at": float(row["last_seen_at"]),
            "expires_at": float(row["expires_at"]),
            "ended_at": (
                float(row["ended_at"]) if row["ended_at"] is not None else None
            ),
            "superseded_at": (
                float(row["superseded_at"])
                if row["superseded_at"] is not None
                else None
            ),
            "metadata": json.loads(str(row["metadata_json"] or "{}")),
        }

    @staticmethod
    def _native_event_message_requires_reply(row: sqlite3.Row) -> bool:
        try:
            reasons = set(json.loads(str(row["delivery_reasons_json"] or "[]")))
        except (TypeError, json.JSONDecodeError):
            reasons = set()
        return bool(
            row["delivery_actionable"]
            or "mention" in reasons
            or "agent_request" in reasons
        )

    @classmethod
    def _supersede_native_channel_events_locked(
        cls,
        conn: sqlite3.Connection,
        *,
        connector_id: str,
        now: float,
    ) -> None:
        """Release unread deliveries from an obsolete native process.

        A process restart gets a new lease and therefore a new route token.
        Only deliveries that are still unread/unanswered are released; receipts
        already acknowledged by the old process remain authoritative.
        """

        open_event_ids = [
            str(row["event_id"])
            for row in conn.execute(
                "SELECT event_id FROM native_channel_events "
                "WHERE connector_id = ? "
                "AND state IN ('fetched', 'injected', 'applied')",
                (connector_id,),
            ).fetchall()
        ]
        if open_event_ids:
            placeholders = ",".join("?" for _ in open_event_ids)
            conn.execute(
                f"""
                UPDATE message_deliveries
                SET delivery_stage = 'queued',
                    native_session_id = NULL,
                    native_event_id = NULL
                WHERE native_event_id IN ({placeholders})
                  AND state IN ('pending', 'delivered')
                """,
                open_event_ids,
            )
        conn.execute(
            """
            UPDATE native_channel_events
            SET state = 'superseded', superseded_at = COALESCE(superseded_at, ?),
                updated_at = ?
            WHERE connector_id = ?
              AND state IN ('fetched', 'injected', 'applied')
            """,
            (now, now, connector_id),
        )

    def _require_current_native_lease_locked(
        self,
        conn: sqlite3.Connection,
        *,
        participant_id: str,
        authorized_session_id: str,
        connector_id: str,
        lease_id: str,
        process_epoch: str,
        now: float,
    ) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row]:
        session = self._require_live_session(
            conn,
            session_id=authorized_session_id,
            participant_id=participant_id,
            now=now,
        )
        if str(session["connector_id"] or "") != connector_id:
            raise AuthenticationError("connector does not belong to this session")
        lease = conn.execute(
            "SELECT * FROM native_session_leases WHERE lease_id = ?",
            (lease_id,),
        ).fetchone()
        if lease is None:
            raise NotFoundError("native TUI lease was not found")
        if (
            str(lease["connector_id"]) != connector_id
            or str(lease["participant_id"]) != participant_id
            or not self._constant_time_eq(str(lease["process_epoch"]), process_epoch)
        ):
            raise AuthenticationError("native TUI lease does not match")
        if lease["ended_at"] is not None or float(lease["expires_at"]) <= now:
            raise ConflictError("native TUI lease expired; bind the session again")
        connector = self._agent_connector_row_locked(conn, connector_id)
        if (
            str(connector["accepted_participant_id"]) != participant_id
            or connector["revoked_at"] is not None
            or str(connector["invitation_status"]) == "revoked"
        ):
            raise AuthenticationError("active connector binding was not found")
        if str(connector["native_lease_id"] or "") != lease_id:
            raise ConflictError("native TUI lease has been superseded")
        if str(connector["native_delivery_mode"] or "") != "native_preferred":
            raise ConflictError("native delivery is not active for this connector")
        return session, lease, connector

    def _require_native_channel_event_locked(
        self,
        conn: sqlite3.Connection,
        *,
        participant_id: str,
        authorized_session_id: str,
        connector_id: str,
        lease_id: str,
        process_epoch: str,
        event_id: str,
        route_token: str,
        now: float,
    ) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row]:
        _session, lease, connector = self._require_current_native_lease_locked(
            conn,
            participant_id=participant_id,
            authorized_session_id=authorized_session_id,
            connector_id=connector_id,
            lease_id=lease_id,
            process_epoch=process_epoch,
            now=now,
        )
        event = conn.execute(
            "SELECT * FROM native_channel_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if event is None:
            raise NotFoundError("native channel event was not found")
        if (
            str(event["connector_id"]) != connector_id
            or str(event["lease_id"]) != lease_id
            or str(event["participant_id"]) != participant_id
        ):
            raise AuthenticationError("native channel event does not match")
        if not self._constant_time_eq(
            str(event["route_token_hash"]),
            self._secret_hash(route_token),
        ):
            raise AuthenticationError("native channel route token does not match")
        if str(event["state"]) in {"superseded", "cancelled"}:
            raise ConflictError("native channel event is no longer active")
        return event, lease, connector

    def bind_native_agent_session(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        connector_id: str,
        tui_endpoint_id: str,
        native_session_id: str,
        process_epoch: str,
        binding_source: str,
        replace_existing_session: bool = False,
        metadata: object = None,
    ) -> dict[str, Any]:
        """Bind one exact, live TUI process to its durable connector.

        The binding comes from the TUI lifecycle hook.  It is never inferred
        from a process list, transcript directory, or central database scan.
        Re-running a hook for the same process epoch is idempotent.  A distinct
        native session must opt into replacement explicitly.
        """

        participant = opaque_id(participant_id, field="participant_id")
        authorized = opaque_id(
            authorized_session_id,
            field="authorized_session_id",
        )
        connector = opaque_id(connector_id, field="connector_id")
        endpoint = opaque_id(tui_endpoint_id, field="tui_endpoint_id")
        native_session = opaque_id(
            native_session_id,
            field="native_session_id",
        )
        epoch = opaque_id(process_epoch, field="process_epoch")
        source = str(binding_source or "").strip().lower()
        if source not in {"startup", "resume"}:
            raise ValidationError("binding_source must be startup or resume")
        normalized_metadata = self._connector_detail(metadata)
        now = time.time()
        expires_at = now + NATIVE_SESSION_LEASE_SECONDS
        with self._transaction() as conn:
            live_session = self._require_live_session(
                conn,
                session_id=authorized,
                participant_id=participant,
                now=now,
            )
            if str(live_session["connector_id"] or "") != connector:
                raise AuthenticationError("connector does not belong to this session")
            connector_row = self._agent_connector_row_locked(conn, connector)
            if (
                str(connector_row["accepted_participant_id"]) != participant
                or connector_row["revoked_at"] is not None
                or str(connector_row["invitation_status"]) == "revoked"
            ):
                raise AuthenticationError("active connector binding was not found")
            bound_endpoint = str(connector_row["tui_endpoint_id"] or "")
            if bound_endpoint and not self._constant_time_eq(bound_endpoint, endpoint):
                raise AuthenticationError("native TUI endpoint does not match")
            bound_session = str(connector_row["tui_native_session_id"] or "")
            if (
                bound_session
                and not self._constant_time_eq(bound_session, native_session)
                and not bool(replace_existing_session)
            ):
                raise ConflictError(
                    "connector is bound to another native TUI session; "
                    "explicit replacement is required"
                )

            existing = conn.execute(
                """
                SELECT * FROM native_session_leases
                WHERE connector_id = ? AND native_session_id = ?
                  AND process_epoch = ? AND ended_at IS NULL
                ORDER BY started_at DESC LIMIT 1
                """,
                (connector, native_session, epoch),
            ).fetchone()
            if existing is None:
                self._supersede_native_channel_events_locked(
                    conn,
                    connector_id=connector,
                    now=now,
                )
                conn.execute(
                    """
                    UPDATE native_session_leases
                    SET superseded_at = COALESCE(superseded_at, ?),
                        ended_at = COALESCE(ended_at, ?)
                    WHERE connector_id = ? AND ended_at IS NULL
                    """,
                    (now, now, connector),
                )
                lease_id = f"lease_{uuid.uuid4().hex}"
                conn.execute(
                    """
                    INSERT INTO native_session_leases (
                        lease_id, connector_id, participant_id,
                        tui_endpoint_id, native_session_id, process_epoch,
                        binding_source, started_at, last_seen_at, expires_at,
                        metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        lease_id,
                        connector,
                        participant,
                        endpoint,
                        native_session,
                        epoch,
                        source,
                        now,
                        now,
                        expires_at,
                        compact_json(normalized_metadata),
                    ),
                )
            else:
                lease_id = str(existing["lease_id"])
                conn.execute(
                    """
                    UPDATE native_session_leases
                    SET last_seen_at = ?, expires_at = ?,
                        binding_source = ?, metadata_json = ?
                    WHERE lease_id = ?
                    """,
                    (
                        now,
                        expires_at,
                        source,
                        compact_json(normalized_metadata),
                        lease_id,
                    ),
                )
            conn.execute(
                """
                UPDATE agent_connectors
                SET tui_endpoint_id = ?, tui_native_session_id = ?,
                    tui_state = 'online', tui_last_seen_at = ?,
                    native_delivery_mode = 'native_preferred',
                    native_lease_id = ?, native_process_epoch = ?,
                    native_lease_expires_at = ?, native_binding_source = ?,
                    connector_last_seen_at = ?, updated_at = ?
                WHERE connector_id = ?
                """,
                (
                    endpoint,
                    native_session,
                    now,
                    lease_id,
                    epoch,
                    expires_at,
                    source,
                    now,
                    now,
                    connector,
                ),
            )
            lease = conn.execute(
                "SELECT * FROM native_session_leases WHERE lease_id = ?",
                (lease_id,),
            ).fetchone()
            updated_connector = self._agent_connector_row_locked(conn, connector)
        return {
            "lease": self._native_session_lease_payload(lease),
            "connector": self._agent_connector_payload(
                updated_connector,
                now=now,
            ),
        }

    def heartbeat_native_agent_session(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        connector_id: str,
        lease_id: str,
        process_epoch: str,
        state: str = "online",
        active_task_id: str | None = None,
        detail: object = None,
    ) -> dict[str, Any]:
        participant = opaque_id(participant_id, field="participant_id")
        authorized = opaque_id(
            authorized_session_id,
            field="authorized_session_id",
        )
        connector = opaque_id(connector_id, field="connector_id")
        lease = opaque_id(lease_id, field="lease_id")
        epoch = opaque_id(process_epoch, field="process_epoch")
        normalized_state = str(state or "").strip().lower()
        if normalized_state not in TUI_STATES - {"unbound", "awaiting_confirmation"}:
            raise ValidationError("unsupported native TUI state")
        task_id = (
            opaque_id(active_task_id, field="active_task_id")
            if str(active_task_id or "").strip()
            else None
        )
        normalized_detail = self._connector_detail(detail)
        now = time.time()
        expires_at = now + NATIVE_SESSION_LEASE_SECONDS
        with self._transaction() as conn:
            live_session = self._require_live_session(
                conn,
                session_id=authorized,
                participant_id=participant,
                now=now,
            )
            if str(live_session["connector_id"] or "") != connector:
                raise AuthenticationError("connector does not belong to this session")
            row = conn.execute(
                "SELECT * FROM native_session_leases WHERE lease_id = ?",
                (lease,),
            ).fetchone()
            if row is None:
                raise NotFoundError("native TUI lease was not found")
            if (
                str(row["connector_id"]) != connector
                or str(row["participant_id"]) != participant
                or not self._constant_time_eq(str(row["process_epoch"]), epoch)
            ):
                raise AuthenticationError("native TUI lease does not match")
            if row["ended_at"] is not None or float(row["expires_at"]) <= now:
                raise ConflictError("native TUI lease expired; bind the session again")
            current = conn.execute(
                "SELECT native_lease_id FROM agent_connectors "
                "WHERE connector_id = ? AND revoked_at IS NULL",
                (connector,),
            ).fetchone()
            if current is None or str(current["native_lease_id"] or "") != lease:
                raise ConflictError("native TUI lease has been superseded")
            conn.execute(
                "UPDATE native_session_leases SET last_seen_at = ?, expires_at = ?, "
                "metadata_json = ? WHERE lease_id = ?",
                (now, expires_at, compact_json(normalized_detail), lease),
            )
            conn.execute(
                """
                UPDATE agent_connectors
                SET tui_state = ?, tui_last_seen_at = ?,
                    tui_active_task_id = ?, tui_detail_json = ?,
                    native_lease_expires_at = ?, connector_last_seen_at = ?,
                    updated_at = ?
                WHERE connector_id = ? AND native_lease_id = ?
                """,
                (
                    normalized_state,
                    now,
                    task_id,
                    compact_json(normalized_detail),
                    expires_at,
                    now,
                    now,
                    connector,
                    lease,
                ),
            )
            updated = conn.execute(
                "SELECT * FROM native_session_leases WHERE lease_id = ?",
                (lease,),
            ).fetchone()
        return self._native_session_lease_payload(updated)

    def end_native_agent_session(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        connector_id: str,
        lease_id: str,
        process_epoch: str,
    ) -> dict[str, Any]:
        participant = opaque_id(participant_id, field="participant_id")
        authorized = opaque_id(
            authorized_session_id,
            field="authorized_session_id",
        )
        connector = opaque_id(connector_id, field="connector_id")
        lease = opaque_id(lease_id, field="lease_id")
        epoch = opaque_id(process_epoch, field="process_epoch")
        now = time.time()
        with self._transaction() as conn:
            live_session = self._require_live_session(
                conn,
                session_id=authorized,
                participant_id=participant,
                now=now,
            )
            if str(live_session["connector_id"] or "") != connector:
                raise AuthenticationError("connector does not belong to this session")
            row = conn.execute(
                "SELECT * FROM native_session_leases WHERE lease_id = ?",
                (lease,),
            ).fetchone()
            if row is None:
                raise NotFoundError("native TUI lease was not found")
            if (
                str(row["connector_id"]) != connector
                or str(row["participant_id"]) != participant
                or not self._constant_time_eq(str(row["process_epoch"]), epoch)
            ):
                raise AuthenticationError("native TUI lease does not match")
            conn.execute(
                "UPDATE native_session_leases SET ended_at = COALESCE(ended_at, ?), "
                "expires_at = MIN(expires_at, ?) WHERE lease_id = ?",
                (now, now, lease),
            )
            conn.execute(
                """
                UPDATE agent_connectors
                SET tui_state = 'offline', tui_last_seen_at = ?,
                    tui_active_task_id = NULL,
                    native_lease_id = NULL, native_process_epoch = NULL,
                    native_lease_expires_at = NULL,
                    connector_last_seen_at = ?, updated_at = ?
                WHERE connector_id = ? AND native_lease_id = ?
                """,
                (now, now, now, connector, lease),
            )
            updated = conn.execute(
                "SELECT * FROM native_session_leases WHERE lease_id = ?",
                (lease,),
            ).fetchone()
        return self._native_session_lease_payload(updated)

    def fallback_native_agent_session(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        connector_id: str,
        lease_id: str,
        process_epoch: str,
    ) -> dict[str, Any]:
        """Explicitly return one connector to its legacy shadow worker."""

        participant = opaque_id(participant_id, field="participant_id")
        authorized = opaque_id(
            authorized_session_id,
            field="authorized_session_id",
        )
        connector = opaque_id(connector_id, field="connector_id")
        lease = opaque_id(lease_id, field="lease_id")
        epoch = opaque_id(process_epoch, field="process_epoch")
        now = time.time()
        with self._transaction() as conn:
            self._require_current_native_lease_locked(
                conn,
                participant_id=participant,
                authorized_session_id=authorized,
                connector_id=connector,
                lease_id=lease,
                process_epoch=epoch,
                now=now,
            )
            self._supersede_native_channel_events_locked(
                conn,
                connector_id=connector,
                now=now,
            )
            conn.execute(
                "UPDATE native_session_leases "
                "SET ended_at = COALESCE(ended_at, ?), "
                "expires_at = MIN(expires_at, ?) WHERE lease_id = ?",
                (now, now, lease),
            )
            conn.execute(
                """
                UPDATE agent_connectors
                SET native_delivery_mode = 'legacy_shadow',
                    tui_state = 'offline', tui_last_seen_at = ?,
                    tui_active_task_id = NULL,
                    native_lease_id = NULL, native_process_epoch = NULL,
                    native_lease_expires_at = NULL,
                    connector_last_seen_at = ?, updated_at = ?
                WHERE connector_id = ? AND native_lease_id = ?
                """,
                (now, now, now, connector, lease),
            )
            updated = self._agent_connector_row_locked(conn, connector)
        return {
            "rolled_back": True,
            "connector": self._agent_connector_payload(updated, now=now),
        }

    def _native_channel_event_payload_locked(
        self,
        conn: sqlite3.Connection,
        event: sqlite3.Row,
    ) -> dict[str, Any]:
        try:
            raw_ids = json.loads(str(event["message_ids_json"] or "[]"))
        except (TypeError, json.JSONDecodeError):
            raw_ids = []
        message_ids = [str(value) for value in raw_ids if str(value)]
        messages: list[dict[str, Any]] = []
        required_message_ids: list[str] = []
        for message_id in message_ids:
            row = conn.execute(
                """
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
                FROM messages AS message
                JOIN message_deliveries AS delivery
                  ON delivery.message_id = message.message_id
                WHERE message.message_id = ?
                  AND delivery.participant_id = ?
                """,
                (message_id, str(event["participant_id"])),
            ).fetchone()
            if row is None:
                continue
            if self._native_event_message_requires_reply(row):
                required_message_ids.append(message_id)
            payload = self._message_payload(
                row,
                authorization=self._chat_authorization_for_message_locked(
                    conn,
                    message_id=message_id,
                    recipient_participant_id=str(event["participant_id"]),
                ),
            )
            payload.update(
                self._message_asset_projection_locked(conn, [message_id])[message_id]
            )
            messages.append(payload)
        state = str(event["state"])
        return {
            "event_id": str(event["event_id"]),
            "connector_id": str(event["connector_id"]),
            "lease_id": str(event["lease_id"]),
            "conversation_id": str(event["conversation_id"]),
            "state": state,
            "deliverable": state == "fetched",
            "messages": messages,
            "message_ids": [str(item["message_id"]) for item in messages],
            "required_message_ids": required_message_ids,
            "required_reply_count": len(required_message_ids),
            "fetched_at": float(event["fetched_at"]),
            "injected_at": (
                float(event["injected_at"])
                if event["injected_at"] is not None
                else None
            ),
            "applied_at": (
                float(event["applied_at"])
                if event["applied_at"] is not None
                else None
            ),
            "replied_at": (
                float(event["replied_at"])
                if event["replied_at"] is not None
                else None
            ),
        }

    def wait_native_channel_event(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        connector_id: str,
        lease_id: str,
        process_epoch: str,
        request_id: str,
        route_token: str,
        wait_seconds: float = 30.0,
        limit: int = NATIVE_CHANNEL_MAX_MESSAGES,
    ) -> dict[str, Any]:
        """Fetch one idempotent event only when the native TUI should wake."""

        participant = opaque_id(participant_id, field="participant_id")
        authorized = opaque_id(
            authorized_session_id,
            field="authorized_session_id",
        )
        connector = opaque_id(connector_id, field="connector_id")
        lease = opaque_id(lease_id, field="lease_id")
        epoch = opaque_id(process_epoch, field="process_epoch")
        request = opaque_id(request_id, field="request_id")
        route = opaque_id(route_token, field="route_token")
        if len(route) < 32:
            raise ValidationError("route_token must contain at least 32 characters")
        wait_for = max(
            0.0,
            min(float(wait_seconds), NATIVE_CHANNEL_MAX_WAIT_SECONDS),
        )
        normalized_limit = max(1, min(int(limit), NATIVE_CHANNEL_MAX_MESSAGES))
        event_id = "event_" + hashlib.sha256(
            f"{connector}\0{lease}\0{request}".encode("utf-8")
        ).hexdigest()[:40]
        route_hash = self._secret_hash(route)
        deadline = time.monotonic() + wait_for

        while True:
            now = time.time()
            with self._transaction() as conn:
                _session, native_lease, connector_row = (
                    self._require_current_native_lease_locked(
                        conn,
                        participant_id=participant,
                        authorized_session_id=authorized,
                        connector_id=connector,
                        lease_id=lease,
                        process_epoch=epoch,
                        now=now,
                    )
                )
                conversation = str(connector_row["conversation_id"])
                existing = conn.execute(
                    "SELECT * FROM native_channel_events WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                if existing is not None:
                    if not self._constant_time_eq(
                        str(existing["route_token_hash"]),
                        route_hash,
                    ):
                        raise AuthenticationError(
                            "native channel request token does not match"
                        )
                    event_payload = self._native_channel_event_payload_locked(
                        conn,
                        existing,
                    )
                else:
                    event_payload = None
            if event_payload is not None:
                event_payload["backlog"] = self._pending_manifest(
                    participant,
                    conversation_id=conversation,
                    native_unassigned_only=True,
                )
                return {"timed_out": False, "event": event_payload}

            backlog = self._pending_manifest(
                participant,
                conversation_id=conversation,
                native_unassigned_only=True,
            )
            if int(backlog["priority_counts"]["mention"]) > 0:
                fetched_at = time.time()
                with self._transaction() as conn:
                    self._require_current_native_lease_locked(
                        conn,
                        participant_id=participant,
                        authorized_session_id=authorized,
                        connector_id=connector,
                        lease_id=lease,
                        process_epoch=epoch,
                        now=fetched_at,
                    )
                    existing = conn.execute(
                        "SELECT * FROM native_channel_events WHERE event_id = ?",
                        (event_id,),
                    ).fetchone()
                    if existing is not None:
                        if not self._constant_time_eq(
                            str(existing["route_token_hash"]),
                            route_hash,
                        ):
                            raise AuthenticationError(
                                "native channel request token does not match"
                            )
                        event = existing
                    else:
                        rows = conn.execute(
                            """
                            SELECT message.*,
                                   delivery.reasons_json
                                       AS delivery_reasons_json,
                                   delivery.priority AS delivery_priority,
                                   delivery.actionable AS delivery_actionable
                            FROM message_deliveries AS delivery
                            JOIN messages AS message
                              ON message.message_id = delivery.message_id
                            JOIN memberships AS membership
                              ON membership.conversation_id =
                                 message.conversation_id
                             AND membership.participant_id =
                                 delivery.participant_id
                             AND membership.active = 1
                            JOIN rooms AS room
                              ON room.conversation_id = message.conversation_id
                             AND room.status = 'active'
                            WHERE delivery.participant_id = ?
                              AND delivery.state IN ('pending', 'delivered')
                              AND delivery.native_event_id IS NULL
                              AND message.sender_participant_id != ?
                              AND message.conversation_id = ?
                            ORDER BY
                                CASE
                                    WHEN instr(
                                        delivery.reasons_json,
                                        '"mention"'
                                    ) > 0
                                      OR instr(
                                        delivery.reasons_json,
                                        '"agent_request"'
                                    ) > 0
                                    THEN 3
                                    WHEN delivery.priority IN (
                                        'direct', 'mention'
                                    ) THEN 2
                                    WHEN delivery.priority = 'important' THEN 1
                                    ELSE 0
                                END DESC,
                                message.sequence
                            LIMIT 500
                            """,
                            (participant, participant, conversation),
                        ).fetchall()
                        selected: list[sqlite3.Row] = []
                        for row in rows:
                            if (
                                bool(row["delivery_actionable"])
                                and str(row["claimed_by"] or "")
                                and str(row["claimed_by"]) != participant
                                and float(row["claim_until"] or 0.0) > fetched_at
                            ):
                                continue
                            selected.append(row)
                            if len(selected) >= normalized_limit:
                                break
                        message_ids = [
                            str(row["message_id"]) for row in selected
                        ]
                        if not message_ids:
                            event = None
                        else:
                            conn.execute(
                                """
                                INSERT INTO native_channel_events (
                                    event_id, connector_id, lease_id,
                                    participant_id, conversation_id,
                                    message_ids_json, route_token_hash,
                                    state, fetched_at, updated_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'fetched', ?, ?)
                                """,
                                (
                                    event_id,
                                    connector,
                                    lease,
                                    participant,
                                    conversation,
                                    compact_json(message_ids),
                                    route_hash,
                                    fetched_at,
                                    fetched_at,
                                ),
                            )
                            for message_id in message_ids:
                                conn.execute(
                                    """
                                    UPDATE message_deliveries
                                    SET state = 'delivered',
                                        delivery_stage = 'queued',
                                        native_session_id = ?,
                                        native_event_id = ?,
                                        first_delivered_at = COALESCE(
                                            first_delivered_at, ?
                                        ),
                                        last_delivered_at = ?,
                                        attempt_count = attempt_count + 1
                                    WHERE message_id = ?
                                      AND participant_id = ?
                                      AND state IN ('pending', 'delivered')
                                      AND native_event_id IS NULL
                                    """,
                                    (
                                        str(native_lease["native_session_id"]),
                                        event_id,
                                        fetched_at,
                                        fetched_at,
                                        message_id,
                                        participant,
                                    ),
                                )
                                conn.execute(
                                    """
                                    INSERT INTO receipts (
                                        message_id, participant_id, state,
                                        delivered_at
                                    ) VALUES (?, ?, 'delivered', ?)
                                    ON CONFLICT(
                                        message_id, participant_id
                                    ) DO UPDATE SET
                                        state = CASE
                                            WHEN receipts.state = 'acked'
                                            THEN 'acked'
                                            ELSE 'delivered'
                                        END,
                                        delivered_at = COALESCE(
                                            receipts.delivered_at,
                                            excluded.delivered_at
                                        )
                                    """,
                                    (message_id, participant, fetched_at),
                                )
                            event = conn.execute(
                                "SELECT * FROM native_channel_events "
                                "WHERE event_id = ?",
                                (event_id,),
                            ).fetchone()
                    event_payload = (
                        self._native_channel_event_payload_locked(conn, event)
                        if event is not None
                        else None
                    )
                if event_payload is not None:
                    event_payload["backlog"] = self._pending_manifest(
                        participant,
                        conversation_id=conversation,
                        native_unassigned_only=True,
                    )
                    return {"timed_out": False, "event": event_payload}

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {
                    "timed_out": True,
                    "event": None,
                    "conversation_id": conversation,
                    "backlog": backlog,
                }
            time.sleep(min(max(self.poll_interval_seconds, 0.2), remaining))

    def receive_native_channel_event(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        connector_id: str,
        lease_id: str,
        process_epoch: str,
        event_id: str,
        route_token: str,
        stage: str,
    ) -> dict[str, Any]:
        """Record injection or model application without faking a reply."""

        participant = opaque_id(participant_id, field="participant_id")
        authorized = opaque_id(
            authorized_session_id,
            field="authorized_session_id",
        )
        connector = opaque_id(connector_id, field="connector_id")
        lease = opaque_id(lease_id, field="lease_id")
        epoch = opaque_id(process_epoch, field="process_epoch")
        event_identifier = opaque_id(event_id, field="event_id")
        route = opaque_id(route_token, field="route_token")
        normalized_stage = str(stage or "").strip().lower()
        if normalized_stage not in {"injected", "applied"}:
            raise ValidationError("native event stage must be injected or applied")
        now = time.time()
        optional_acked = 0
        with self._transaction() as conn:
            event, _native_lease, _connector = (
                self._require_native_channel_event_locked(
                    conn,
                    participant_id=participant,
                    authorized_session_id=authorized,
                    connector_id=connector,
                    lease_id=lease,
                    process_epoch=epoch,
                    event_id=event_identifier,
                    route_token=route,
                    now=now,
                )
            )
            message_ids = list(json.loads(str(event["message_ids_json"] or "[]")))
            if normalized_stage == "injected":
                conn.execute(
                    """
                    UPDATE native_channel_events
                    SET state = CASE
                            WHEN state = 'fetched' THEN 'injected'
                            ELSE state
                        END,
                        injected_at = COALESCE(injected_at, ?),
                        updated_at = ?
                    WHERE event_id = ?
                    """,
                    (now, now, event_identifier),
                )
                conn.execute(
                    """
                    UPDATE message_deliveries
                    SET delivery_stage = CASE
                            WHEN delivery_stage = 'queued'
                            THEN 'native_injected'
                            ELSE delivery_stage
                        END,
                        native_injected_at = COALESCE(native_injected_at, ?)
                    WHERE native_event_id = ? AND participant_id = ?
                      AND state IN ('pending', 'delivered')
                    """,
                    (now, event_identifier, participant),
                )
            else:
                delivery_rows = conn.execute(
                    """
                    SELECT delivery.message_id,
                           delivery.reasons_json AS delivery_reasons_json,
                           delivery.actionable AS delivery_actionable,
                           delivery.state
                    FROM message_deliveries AS delivery
                    WHERE delivery.native_event_id = ?
                      AND delivery.participant_id = ?
                    """,
                    (event_identifier, participant),
                ).fetchall()
                for delivery in delivery_rows:
                    message_id = str(delivery["message_id"])
                    requires_reply = self._native_event_message_requires_reply(
                        delivery
                    )
                    conn.execute(
                        """
                        UPDATE message_deliveries
                        SET delivery_stage = CASE
                                WHEN native_replied_at IS NOT NULL THEN 'replied'
                                ELSE 'native_applied'
                            END,
                            native_injected_at = COALESCE(
                                native_injected_at, ?
                            ),
                            native_applied_at = COALESCE(native_applied_at, ?),
                            state = CASE
                                WHEN ? = 0 AND state != 'cancelled' THEN 'acked'
                                ELSE state
                            END,
                            acked_at = CASE
                                WHEN ? = 0 THEN COALESCE(acked_at, ?)
                                ELSE acked_at
                            END
                        WHERE message_id = ? AND participant_id = ?
                        """,
                        (
                            now,
                            now,
                            1 if requires_reply else 0,
                            1 if requires_reply else 0,
                            now,
                            message_id,
                            participant,
                        ),
                    )
                    if not requires_reply:
                        optional_acked += 1
                        conn.execute(
                            """
                            INSERT INTO receipts (
                                message_id, participant_id, state,
                                delivered_at, acked_at
                            ) VALUES (?, ?, 'acked', ?, ?)
                            ON CONFLICT(
                                message_id, participant_id
                            ) DO UPDATE SET
                                state = 'acked',
                                delivered_at = COALESCE(
                                    receipts.delivered_at,
                                    excluded.delivered_at
                                ),
                                acked_at = COALESCE(
                                    receipts.acked_at,
                                    excluded.acked_at
                                )
                            """,
                            (message_id, participant, now, now),
                        )
                conn.execute(
                    """
                    UPDATE native_channel_events
                    SET state = CASE
                            WHEN state = 'replied' THEN state
                            ELSE 'applied'
                        END,
                        injected_at = COALESCE(injected_at, ?),
                        applied_at = COALESCE(applied_at, ?),
                        updated_at = ?
                    WHERE event_id = ?
                    """,
                    (now, now, now, event_identifier),
                )
            updated = conn.execute(
                "SELECT * FROM native_channel_events WHERE event_id = ?",
                (event_identifier,),
            ).fetchone()
            result = self._native_channel_event_payload_locked(conn, updated)
        return {
            "event": result,
            "stage": normalized_stage,
            "optional_acked_count": optional_acked,
            "message_count": len(message_ids),
        }

    def reply_native_channel_event(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        connector_id: str,
        lease_id: str,
        process_epoch: str,
        event_id: str,
        route_token: str,
        message_id: str,
        body_text: str,
        mentions: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        participant = opaque_id(participant_id, field="participant_id")
        authorized = opaque_id(
            authorized_session_id,
            field="authorized_session_id",
        )
        connector = opaque_id(connector_id, field="connector_id")
        lease = opaque_id(lease_id, field="lease_id")
        epoch = opaque_id(process_epoch, field="process_epoch")
        event_identifier = opaque_id(event_id, field="event_id")
        route = opaque_id(route_token, field="route_token")
        message = opaque_id(message_id, field="message_id")
        now = time.time()
        with self._transaction() as conn:
            event, _native_lease, _connector = (
                self._require_native_channel_event_locked(
                    conn,
                    participant_id=participant,
                    authorized_session_id=authorized,
                    connector_id=connector,
                    lease_id=lease,
                    process_epoch=epoch,
                    event_id=event_identifier,
                    route_token=route,
                    now=now,
                )
            )
            message_ids = set(json.loads(str(event["message_ids_json"] or "[]")))
            if message not in message_ids:
                raise AuthorizationError(
                    "reply target is not part of this native channel event"
                )
        self.receive_native_channel_event(
            participant_id=participant,
            authorized_session_id=authorized,
            connector_id=connector,
            lease_id=lease,
            process_epoch=epoch,
            event_id=event_identifier,
            route_token=route,
            stage="applied",
        )
        reply_result = self.reply(
            authorized_session_id=authorized,
            participant_id=participant,
            message_id=message,
            body_text=body_text,
            mentions=mentions,
        )
        replied_at = time.time()
        with self._transaction() as conn:
            event, _native_lease, _connector = (
                self._require_native_channel_event_locked(
                    conn,
                    participant_id=participant,
                    authorized_session_id=authorized,
                    connector_id=connector,
                    lease_id=lease,
                    process_epoch=epoch,
                    event_id=event_identifier,
                    route_token=route,
                    now=replied_at,
                )
            )
            conn.execute(
                """
                UPDATE message_deliveries
                SET delivery_stage = 'replied', native_replied_at = ?
                WHERE message_id = ? AND participant_id = ?
                  AND native_event_id = ?
                """,
                (replied_at, message, participant, event_identifier),
            )
            rows = conn.execute(
                """
                SELECT delivery.state,
                       delivery.reasons_json AS delivery_reasons_json,
                       delivery.actionable AS delivery_actionable
                FROM message_deliveries AS delivery
                WHERE delivery.native_event_id = ?
                  AND delivery.participant_id = ?
                """,
                (event_identifier, participant),
            ).fetchall()
            remaining_required = sum(
                1
                for row in rows
                if self._native_event_message_requires_reply(row)
                and str(row["state"]) != "acked"
            )
            conn.execute(
                """
                UPDATE native_channel_events
                SET state = CASE WHEN ? = 0 THEN 'replied' ELSE 'applied' END,
                    replied_at = CASE
                        WHEN ? = 0 THEN COALESCE(replied_at, ?)
                        ELSE replied_at
                    END,
                    updated_at = ?
                WHERE event_id = ?
                """,
                (
                    remaining_required,
                    remaining_required,
                    replied_at,
                    replied_at,
                    event_identifier,
                ),
            )
            updated = conn.execute(
                "SELECT * FROM native_channel_events WHERE event_id = ?",
                (event_identifier,),
            ).fetchone()
            event_payload = self._native_channel_event_payload_locked(conn, updated)
        return {
            **reply_result,
            "native_event": event_payload,
            "remaining_required_reply_count": remaining_required,
        }

    def send_native_channel_event(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        connector_id: str,
        lease_id: str,
        process_epoch: str,
        event_id: str,
        route_token: str,
        body_text: str,
        mentions: Sequence[str] | None = None,
        notification_mode: str | None = None,
    ) -> dict[str, Any]:
        participant = opaque_id(participant_id, field="participant_id")
        authorized = opaque_id(
            authorized_session_id,
            field="authorized_session_id",
        )
        connector = opaque_id(connector_id, field="connector_id")
        lease = opaque_id(lease_id, field="lease_id")
        epoch = opaque_id(process_epoch, field="process_epoch")
        event_identifier = opaque_id(event_id, field="event_id")
        route = opaque_id(route_token, field="route_token")
        with self._transaction() as conn:
            event, _native_lease, _connector = (
                self._require_native_channel_event_locked(
                    conn,
                    participant_id=participant,
                    authorized_session_id=authorized,
                    connector_id=connector,
                    lease_id=lease,
                    process_epoch=epoch,
                    event_id=event_identifier,
                    route_token=route,
                    now=time.time(),
                )
            )
            conversation = str(event["conversation_id"])
        self.receive_native_channel_event(
            participant_id=participant,
            authorized_session_id=authorized,
            connector_id=connector,
            lease_id=lease,
            process_epoch=epoch,
            event_id=event_identifier,
            route_token=route,
            stage="applied",
        )
        sent = self.send(
            authorized_session_id=authorized,
            sender_participant_id=participant,
            conversation_id=conversation,
            body_text=body_text,
            audience_kind="room",
            audience_value="*",
            mentions=mentions,
            notification_mode=notification_mode,
        )
        return {"message": sent, "event_id": event_identifier}

    def report_agent_tui_state(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        connector_id: str,
        tui_endpoint_id: str,
        tui_native_session_id: str,
        state: str,
        access_mode: object = None,
        capabilities: Sequence[str] | None = None,
        active_task_id: str | None = None,
        detail: object = None,
    ) -> dict[str, Any]:
        """Heartbeat one immutable room-to-native-session binding."""

        participant = opaque_id(participant_id, field="participant_id")
        session_id = opaque_id(
            authorized_session_id,
            field="authorized_session_id",
        )
        connector = opaque_id(connector_id, field="connector_id")
        endpoint = opaque_id(tui_endpoint_id, field="tui_endpoint_id")
        native_session = opaque_id(
            tui_native_session_id,
            field="tui_native_session_id",
        )
        normalized_state = str(state or "").strip().lower()
        if normalized_state not in TUI_STATES - {"unbound", "awaiting_confirmation"}:
            raise ValidationError("unsupported native TUI state")
        # Accepted only so already-running pre-v35 workers continue reporting
        # during a rolling upgrade. The value is intentionally discarded.
        del access_mode
        normalized_capabilities = string_tokens(
            capabilities,
            field="tui_capabilities",
        )
        task_id = (
            opaque_id(active_task_id, field="active_task_id")
            if str(active_task_id or "").strip()
            else None
        )
        normalized_detail = self._connector_detail(detail)
        now = time.time()
        with self._transaction() as conn:
            live_session = self._require_live_session(
                conn,
                session_id=session_id,
                participant_id=participant,
                now=now,
            )
            if str(live_session["connector_id"] or "") != connector:
                raise AuthenticationError("connector does not belong to this session")
            bound = conn.execute(
                "SELECT tui_endpoint_id, tui_native_session_id "
                "FROM agent_connectors WHERE connector_id = ? "
                "AND accepted_participant_id = ? AND revoked_at IS NULL",
                (connector, participant),
            ).fetchone()
            if bound is None:
                raise NotFoundError("active connector invitation was not found")
            if not self._constant_time_eq(
                endpoint,
                str(bound["tui_endpoint_id"] or ""),
            ) or not self._constant_time_eq(
                native_session,
                str(bound["tui_native_session_id"] or ""),
            ):
                raise AuthenticationError("native TUI binding does not match")
            conn.execute(
                """
                UPDATE agent_connectors
                SET tui_state = ?, tui_capabilities_json = ?, tui_last_seen_at = ?,
                    tui_active_task_id = ?, tui_detail_json = ?,
                    connector_last_seen_at = ?, updated_at = ?
                WHERE connector_id = ?
                """,
                (
                    normalized_state,
                    compact_json(normalized_capabilities),
                    now,
                    task_id,
                    compact_json(normalized_detail),
                    now,
                    now,
                    connector,
                ),
            )
            row = conn.execute(
                """
                SELECT connector.*, invitation.product, invitation.adapter_kind,
                       invitation.tui_adapter_kind,
                       invitation.status AS invitation_status
                FROM agent_connectors AS connector
                JOIN agent_invitations AS invitation
                  ON invitation.invitation_id = connector.invitation_id
                WHERE connector.connector_id = ?
                """,
                (connector,),
            ).fetchone()
            endpoint_room_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM agent_connectors "
                    "WHERE tui_endpoint_id = ? AND revoked_at IS NULL",
                    (endpoint,),
                ).fetchone()[0]
            )
        payload = self._agent_connector_payload(row, now=now)
        payload["tui"]["room_binding_count"] = endpoint_room_count
        return payload

    def report_native_tui_delivery_stage(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        connector_id: str,
        tui_endpoint_id: str,
        tui_native_session_id: str,
        message_ids: Sequence[str],
        stage: str,
    ) -> dict[str, Any]:
        """Record exact native TUI injection/application milestones.

        This endpoint is telemetry, not an acknowledgement.  It only accepts a
        room-bound ``mcp`` session for the connector's immutable native binding,
        and it never changes delivery state or reply requirements.
        """

        participant = opaque_id(participant_id, field="participant_id")
        session_id = opaque_id(
            authorized_session_id,
            field="authorized_session_id",
        )
        connector = opaque_id(connector_id, field="connector_id")
        endpoint = opaque_id(tui_endpoint_id, field="tui_endpoint_id")
        native_session = opaque_id(
            tui_native_session_id,
            field="tui_native_session_id",
        )
        normalized_ids = [
            opaque_id(value, field="message_id")
            for value in dict.fromkeys(message_ids)
        ]
        if not normalized_ids:
            raise ValidationError("message_ids must contain at least one message")
        if len(normalized_ids) > 100:
            raise ValidationError("message_ids cannot contain more than 100 entries")
        normalized_stage = str(stage or "").strip().lower()
        if normalized_stage not in {"injected", "applied"}:
            raise ValidationError("native TUI delivery stage must be injected or applied")

        now = time.time()
        placeholders = ",".join("?" for _ in normalized_ids)
        with self._transaction() as conn:
            live_session = self._require_live_session(
                conn,
                session_id=session_id,
                participant_id=participant,
                now=now,
            )
            if str(live_session["component"] or "") != "mcp":
                raise AuthorizationError(
                    "only the bound native TUI adapter may report delivery stages"
                )
            if str(live_session["connector_id"] or "") != connector:
                raise AuthenticationError("connector does not belong to this session")
            conversation = str(live_session["registered_conversation_id"])
            bound = conn.execute(
                """
                SELECT connector.tui_endpoint_id,
                       connector.tui_native_session_id,
                       connector.conversation_id,
                       invitation.tui_adapter_kind
                FROM agent_connectors AS connector
                JOIN agent_invitations AS invitation
                  ON invitation.invitation_id = connector.invitation_id
                WHERE connector.connector_id = ?
                  AND connector.accepted_participant_id = ?
                  AND connector.revoked_at IS NULL
                  AND invitation.status != 'revoked'
                """,
                (connector, participant),
            ).fetchone()
            if bound is None:
                raise NotFoundError("active connector invitation was not found")
            if str(bound["tui_adapter_kind"] or "") not in NATIVE_TUI_ADAPTERS:
                raise AuthorizationError(
                    "connector is not configured for a native TUI adapter"
                )
            if conversation != str(bound["conversation_id"]):
                raise AuthenticationError("native TUI connector room does not match")
            if not self._constant_time_eq(
                endpoint,
                str(bound["tui_endpoint_id"] or ""),
            ) or not self._constant_time_eq(
                native_session,
                str(bound["tui_native_session_id"] or ""),
            ):
                raise AuthenticationError("native TUI binding does not match")
            rows = conn.execute(
                f"""
                SELECT delivery.message_id, delivery.state
                FROM message_deliveries AS delivery
                JOIN messages AS message
                  ON message.message_id = delivery.message_id
                WHERE delivery.participant_id = ?
                  AND message.conversation_id = ?
                  AND delivery.message_id IN ({placeholders})
                """,
                (participant, conversation, *normalized_ids),
            ).fetchall()
            if {str(row["message_id"]) for row in rows} != set(normalized_ids):
                raise AuthorizationError(
                    "one or more deliveries do not belong to this native TUI room"
                )
            if any(str(row["state"]) == "cancelled" for row in rows):
                raise ConflictError("cancelled deliveries cannot advance native stages")
            if normalized_stage == "injected":
                conn.execute(
                    f"""
                    UPDATE message_deliveries
                    SET delivery_stage = CASE
                            WHEN delivery_stage IN ('queued', 'legacy_delivered')
                            THEN 'native_injected'
                            ELSE delivery_stage
                        END,
                        native_session_id = COALESCE(native_session_id, ?),
                        native_injected_at = COALESCE(native_injected_at, ?)
                    WHERE participant_id = ?
                      AND message_id IN ({placeholders})
                    """,
                    (native_session, now, participant, *normalized_ids),
                )
            else:
                conn.execute(
                    f"""
                    UPDATE message_deliveries
                    SET delivery_stage = CASE
                            WHEN delivery_stage = 'replied' THEN 'replied'
                            ELSE 'native_applied'
                        END,
                        native_session_id = COALESCE(native_session_id, ?),
                        native_injected_at = COALESCE(native_injected_at, ?),
                        native_applied_at = COALESCE(native_applied_at, ?)
                    WHERE participant_id = ?
                      AND message_id IN ({placeholders})
                    """,
                    (
                        native_session,
                        now,
                        now,
                        participant,
                        *normalized_ids,
                    ),
                )
        return {
            "stage": normalized_stage,
            "message_ids": normalized_ids,
            "count": len(normalized_ids),
            "recorded_at": now,
        }
