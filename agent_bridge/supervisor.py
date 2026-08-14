from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Any, Sequence


PRIORITIES = {"normal": 0, "important": 1, "mention": 2}
MAX_RETRY_DELAY_SECONDS = 30.0
HANDLED_EVENT_RETENTION_SECONDS = 7 * 24 * 60 * 60
DEFERRED_EVENT_RETENTION_SECONDS = 30 * 24 * 60 * 60
SENSITIVE_CHILD_ENV = {
    "AGENT_BRIDGE_TOKEN",
    "AGENT_TOKEN",
    "AGENT_BRIDGE_REGISTRATION_SECRET",
    "AGENT_BRIDGE_INVITATION_TOKEN",
    "AGENT_BRIDGE_ENROLLMENT_TOKEN",
    "AGENT_BRIDGE_DB",
    "AGENT_BRIDGE_HOME",
}
FORBIDDEN_PAYLOAD_KEYS = {
    "body",
    "body_text",
    "content",
    "message",
    "messages",
    "quote",
    "refs",
    "text",
}
ALLOWED_ENVELOPE_KEYS = {
    "schema_version",
    "source",
    "event",
    "event_id",
    "participant_id",
    "cursor",
    "wake_priority",
    "required_reply_count",
    "has_new",
    "has_room_activity",
    "backlog",
    "new_since_cursor",
    "room_activity_since_cursor",
    "server_time",
}


class SupervisorError(RuntimeError):
    pass


def _retry_delay(attempt_count: int) -> float:
    return min(
        MAX_RETRY_DELAY_SECONDS,
        max(1.0, 2.0 ** min(max(int(attempt_count), 1), 8)),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-bridge-supervisor",
        description=(
            "Durably queue metadata-only Agent Bridge notifications and dispatch "
            "coalesced wake batches to one local Agent adapter."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    enqueue = subparsers.add_parser("enqueue")
    enqueue.add_argument("--database", required=True)

    run = subparsers.add_parser("run")
    run.add_argument("--database", required=True)
    run.add_argument(
        "--adapter-command-json",
        default=os.environ.get("AGENT_BRIDGE_ADAPTER_COMMAND_JSON"),
        required=os.environ.get("AGENT_BRIDGE_ADAPTER_COMMAND_JSON") is None,
    )
    run.add_argument(
        "--wake-policy",
        choices=("all", "important", "mention"),
        default=os.environ.get("AGENT_BRIDGE_AGENT_WAKE_POLICY", "all"),
    )
    run.add_argument(
        "--debounce",
        type=float,
        default=float(os.environ.get("AGENT_BRIDGE_AGENT_WAKE_DEBOUNCE", "3")),
    )
    run.add_argument(
        "--poll-interval",
        type=float,
        default=float(os.environ.get("AGENT_BRIDGE_AGENT_WAKE_POLL", "1")),
    )
    run.add_argument(
        "--adapter-timeout",
        type=float,
        default=float(os.environ.get("AGENT_BRIDGE_AGENT_WAKE_TIMEOUT", "3600")),
    )
    run.add_argument("--once", action="store_true")

    status = subparsers.add_parser("status")
    status.add_argument("--database", required=True)
    return parser


def _parse_json_argv(value: str) -> tuple[str, ...]:
    try:
        raw = json.loads(value)
    except json.JSONDecodeError as exc:
        raise SupervisorError("adapter command must be a JSON argv array") from exc
    if (
        not isinstance(raw, list)
        or not raw
        or len(raw) > 64
        or any(
            not isinstance(item, str) or not item or "\x00" in item or len(item) > 4096
            for item in raw
        )
    ):
        raise SupervisorError(
            "adapter command must contain 1-64 non-empty string arguments"
        )
    return tuple(raw)


def _assert_metadata_only(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).casefold() in FORBIDDEN_PAYLOAD_KEYS:
                raise SupervisorError("wake envelope must not contain message content")
            _assert_metadata_only(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_metadata_only(nested)


def _validated_envelope(raw: bytes) -> tuple[dict[str, Any], bytes, str]:
    if not raw or len(raw) > 1_048_576:
        raise SupervisorError("wake envelope must contain 1-1048576 bytes")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SupervisorError("wake envelope must be one UTF-8 JSON object") from exc
    if not isinstance(payload, dict):
        raise SupervisorError("wake envelope must be one JSON object")
    if set(payload) - ALLOWED_ENVELOPE_KEYS:
        raise SupervisorError("wake envelope contains unsupported fields")
    if payload.get("schema_version") != 1 or payload.get("source") != "agent-bridge":
        raise SupervisorError("wake envelope source or schema is invalid")
    priority = str(payload.get("wake_priority") or "")
    if priority not in PRIORITIES:
        raise SupervisorError("wake envelope priority is invalid")
    participant_id = str(payload.get("participant_id") or "").strip()
    event_name = str(payload.get("event") or "").strip()
    if not participant_id or not event_name:
        raise SupervisorError("wake envelope participant and event are required")
    event_id = payload.get("event_id")
    if event_id is not None and (
        isinstance(event_id, bool) or not isinstance(event_id, int) or event_id < 0
    ):
        raise SupervisorError("wake envelope event_id must be a non-negative integer")
    required_reply_count = payload.get("required_reply_count", 0)
    if (
        isinstance(required_reply_count, bool)
        or not isinstance(required_reply_count, int)
        or required_reply_count < 0
    ):
        raise SupervisorError(
            "wake envelope required_reply_count must be a non-negative integer"
        )
    _assert_metadata_only(payload)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    suffix = (
        str(event_id) if event_id is not None else hashlib.sha256(encoded).hexdigest()
    )
    # A digest can deliberately promote the same room cursor from normal to
    # mention later.  Priority is part of the durable event identity so that
    # this safe escalation is not mistaken for a duplicate.
    idempotency_key = f"{participant_id}:{event_name}:{suffix}:{priority}"
    return payload, encoded, idempotency_key


def _connect(database: Path) -> sqlite3.Connection:
    path = database.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS wake_events (
            idempotency_key TEXT PRIMARY KEY,
            event_id INTEGER,
            participant_id TEXT NOT NULL,
            event_name TEXT NOT NULL,
            priority TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending', 'inflight', 'handled', 'deferred')),
            created_at REAL NOT NULL,
            available_at REAL NOT NULL,
            claimed_at REAL,
            handled_at REAL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            claim_owner TEXT,
            adapter_run_id TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_wake_events_dispatch
            ON wake_events(state, available_at, created_at);
        CREATE INDEX IF NOT EXISTS idx_wake_events_priority_dispatch
            ON wake_events(state, available_at, priority, created_at);
        """
    )
    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(wake_events)").fetchall()
    }
    if "claim_owner" not in columns:
        connection.execute("ALTER TABLE wake_events ADD COLUMN claim_owner TEXT")
    if "adapter_run_id" not in columns:
        connection.execute("ALTER TABLE wake_events ADD COLUMN adapter_run_id TEXT")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return connection


def enqueue_event(database: Path, raw: bytes, *, now: float | None = None) -> bool:
    payload, encoded, idempotency_key = _validated_envelope(raw)
    created_at = float(time.time() if now is None else now)
    with closing(_connect(database)) as connection:
        _prune_old_events_locked(connection, now=created_at)
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO wake_events
                (idempotency_key, event_id, participant_id, event_name,
                 priority, payload_json, created_at, available_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                idempotency_key,
                payload.get("event_id"),
                str(payload["participant_id"]),
                str(payload["event"]),
                str(payload["wake_priority"]),
                encoded.decode("utf-8"),
                created_at,
                created_at,
            ),
        )
        return cursor.rowcount > 0


def queue_status(database: Path) -> dict[str, Any]:
    with closing(_connect(database)) as connection:
        now = time.time()
        counts = {
            str(row["state"]): int(row["count"])
            for row in connection.execute(
                "SELECT state, COUNT(*) AS count FROM wake_events GROUP BY state"
            ).fetchall()
        }
        newest = connection.execute(
            "SELECT MAX(event_id) AS event_id FROM wake_events"
        ).fetchone()
        active_runs = int(
            connection.execute(
                """
                SELECT COUNT(DISTINCT adapter_run_id) AS count
                FROM wake_events
                WHERE state = 'inflight' AND adapter_run_id IS NOT NULL
                """
            ).fetchone()["count"]
        )
        oldest = {
            str(row["state"]): float(row["oldest_created_at"])
            for row in connection.execute(
                "SELECT state, MIN(created_at) AS oldest_created_at "
                "FROM wake_events GROUP BY state"
            ).fetchall()
            if row["oldest_created_at"] is not None
        }
    return {
        "database": str(database.expanduser().resolve()),
        "counts": {
            "pending": counts.get("pending", 0),
            "inflight": counts.get("inflight", 0),
            "deferred": counts.get("deferred", 0),
            "handled": counts.get("handled", 0),
        },
        "newest_event_id": newest["event_id"] if newest is not None else None,
        "active_adapter_runs": active_runs,
        "oldest_age_seconds": {
            state: max(0.0, now - created_at)
            for state, created_at in oldest.items()
        },
        # Status is intentionally read-only. Retention runs on enqueue/claim,
        # where a single consistent clock is already available.
        "pruned": {"handled": 0, "deferred": 0},
    }


def _prune_old_events_locked(
    connection: sqlite3.Connection,
    *,
    now: float,
) -> dict[str, int]:
    handled = connection.execute(
        "DELETE FROM wake_events WHERE state = 'handled' "
        "AND COALESCE(handled_at, created_at) < ?",
        (now - HANDLED_EVENT_RETENTION_SECONDS,),
    ).rowcount
    deferred = connection.execute(
        "DELETE FROM wake_events WHERE state = 'deferred' AND created_at < ?",
        (now - DEFERRED_EVENT_RETENTION_SECONDS,),
    ).rowcount
    return {"handled": int(handled), "deferred": int(deferred)}


def _priority_allowed(priority: str, wake_policy: str) -> bool:
    minimum = {"all": 0, "important": 1, "mention": 2}[wake_policy]
    return PRIORITIES[priority] >= minimum


def _claim_batch(
    connection: sqlite3.Connection,
    *,
    wake_policy: str,
    debounce: float,
    now: float,
    limit: int = 256,
    claim_owner: str | None = None,
) -> list[sqlite3.Row]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        _prune_old_events_locked(connection, now=now)
        connection.execute(
            """
            UPDATE wake_events
            SET state = 'pending', claimed_at = NULL,
                available_at = MIN(available_at, ?),
                claim_owner = NULL, adapter_run_id = NULL,
                last_error = COALESCE(last_error, 'recovered stale inflight event')
            WHERE state = 'inflight' AND claimed_at < ?
            """,
            (now, now - 7200),
        )
        rows = connection.execute(
            """
            SELECT * FROM wake_events
            WHERE state IN ('pending', 'deferred') AND available_at <= ?
            ORDER BY
                CASE priority
                    WHEN 'mention' THEN 2
                    WHEN 'important' THEN 1
                    ELSE 0
                END DESC,
                created_at,
                idempotency_key
            LIMIT ?
            """,
            (now, max(1, min(int(limit), 1000))),
        ).fetchall()
        if not rows:
            connection.execute("COMMIT")
            return []
        highest_priority = max(
            PRIORITIES[str(row["priority"])] for row in rows
        )
        newest_created_at = max(
            float(row["created_at"])
            for row in rows
            if PRIORITIES[str(row["priority"])] == highest_priority
        )
        if newest_created_at + max(0.0, debounce) > now:
            connection.execute("COMMIT")
            return []
        highest = max(rows, key=lambda row: PRIORITIES[str(row["priority"])])
        if not _priority_allowed(str(highest["priority"]), wake_policy):
            connection.executemany(
                "UPDATE wake_events SET state = 'deferred' WHERE idempotency_key = ?",
                [(str(row["idempotency_key"]),) for row in rows],
            )
            connection.execute("COMMIT")
            return []
        keys = [str(row["idempotency_key"]) for row in rows]
        connection.executemany(
            """
            UPDATE wake_events
            SET state = 'inflight', claimed_at = ?, attempt_count = attempt_count + 1,
                claim_owner = ?, adapter_run_id = NULL
            WHERE idempotency_key = ?
            """,
            [(now, claim_owner, key) for key in keys],
        )
        connection.execute("COMMIT")
        return rows
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def _batch_envelope(rows: Sequence[sqlite3.Row]) -> bytes:
    priorities = Counter(str(row["priority"]) for row in rows)
    highest = max(priorities, key=lambda priority: PRIORITIES[priority])
    event_ids = sorted(
        int(row["event_id"]) for row in rows if row["event_id"] is not None
    )
    # Each listener event carries a snapshot of the same participant's current
    # mandatory backlog.  Coalescing events must therefore keep the largest
    # snapshot, not sum repeated snapshots of the same outstanding mentions.
    required_by_participant: dict[str, int] = {}
    for row in rows:
        try:
            event_payload = json.loads(str(row["payload_json"]))
        except json.JSONDecodeError:
            continue
        if isinstance(event_payload, dict):
            participant_id = str(event_payload.get("participant_id") or "")
            required_by_participant[participant_id] = max(
                required_by_participant.get(participant_id, 0),
                int(event_payload.get("required_reply_count") or 0),
            )
    required_reply_count = sum(required_by_participant.values())
    payload = {
        "schema_version": 1,
        "source": "agent-bridge-supervisor",
        "event": "wake_batch",
        "batch_id": hashlib.sha256(
            "\n".join(str(row["idempotency_key"]) for row in rows).encode("utf-8")
        ).hexdigest(),
        "event_count": len(rows),
        "event_ids": event_ids,
        "first_event_id": min(event_ids) if event_ids else None,
        "last_event_id": max(event_ids) if event_ids else None,
        "participant_ids": sorted({str(row["participant_id"]) for row in rows}),
        "wake_priority": highest,
        "required_reply_count": required_reply_count,
        "priority_counts": {
            "normal": priorities.get("normal", 0),
            "important": priorities.get("important", 0),
            "mention": priorities.get("mention", 0),
        },
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def claim_batch(
    database: Path,
    *,
    wake_policy: str,
    debounce: float,
    claim_owner: str,
    now: float | None = None,
    limit: int = 256,
) -> list[sqlite3.Row]:
    current_time = float(time.time() if now is None else now)
    with closing(_connect(database)) as connection:
        return _claim_batch(
            connection,
            wake_policy=wake_policy,
            debounce=max(0.0, min(float(debounce), 300.0)),
            now=current_time,
            limit=limit,
            claim_owner=claim_owner,
        )


def recover_inflight(
    database: Path,
    *,
    reason: str,
    now: float | None = None,
) -> int:
    current_time = float(time.time() if now is None else now)
    detail = str(reason or "adapter owner restarted")[-1000:]
    with closing(_connect(database)) as connection:
        cursor = connection.execute(
            """
            UPDATE wake_events
            SET state = 'pending', claimed_at = NULL,
                available_at = MIN(available_at, ?),
                claim_owner = NULL, adapter_run_id = NULL,
                last_error = ?
            WHERE state = 'inflight'
            """,
            (current_time, detail),
        )
        return int(cursor.rowcount)


def attach_adapter_run(
    database: Path,
    *,
    idempotency_keys: Sequence[str],
    claim_owner: str,
    adapter_run_id: str,
) -> int:
    keys = tuple(str(key) for key in idempotency_keys)
    if not keys:
        return 0
    run_id = str(adapter_run_id or "").strip()
    if not run_id:
        raise SupervisorError("adapter run id is required")
    with closing(_connect(database)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            cursor_count = 0
            for key in keys:
                cursor = connection.execute(
                    """
                    UPDATE wake_events
                    SET adapter_run_id = ?
                    WHERE idempotency_key = ? AND state = 'inflight'
                      AND claim_owner = ?
                    """,
                    (run_id, key, claim_owner),
                )
                cursor_count += int(cursor.rowcount)
            if cursor_count != len(keys):
                raise SupervisorError(
                    "wake batch ownership changed before adapter attachment"
                )
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
    return cursor_count


def finish_adapter_run(
    database: Path,
    *,
    adapter_run_id: str,
    successful: bool,
    error: str | None = None,
    now: float | None = None,
) -> int:
    current_time = float(time.time() if now is None else now)
    run_id = str(adapter_run_id or "").strip()
    if not run_id:
        raise SupervisorError("adapter run id is required")
    with closing(_connect(database)) as connection:
        rows = connection.execute(
            """
            SELECT idempotency_key, attempt_count
            FROM wake_events
            WHERE state = 'inflight' AND adapter_run_id = ?
            """,
            (run_id,),
        ).fetchall()
        if not rows:
            return 0
        if successful:
            cursor = connection.execute(
                """
                UPDATE wake_events
                SET state = 'handled', handled_at = ?, claimed_at = NULL,
                    claim_owner = NULL, adapter_run_id = NULL, last_error = NULL
                WHERE state = 'inflight' AND adapter_run_id = ?
                """,
                (current_time, run_id),
            )
            return int(cursor.rowcount)
        highest_attempt = max(int(row["attempt_count"]) for row in rows)
        retry_at = current_time + _retry_delay(highest_attempt)
        cursor = connection.execute(
            """
            UPDATE wake_events
            SET state = 'pending', claimed_at = NULL, available_at = ?,
                claim_owner = NULL, adapter_run_id = NULL, last_error = ?
            WHERE state = 'inflight' AND adapter_run_id = ?
            """,
            (retry_at, str(error or "adapter turn failed")[-1000:], run_id),
        )
        return int(cursor.rowcount)


def _run_adapter(
    command: Sequence[str],
    encoded: bytes,
    *,
    timeout: float,
) -> None:
    environment = dict(os.environ)
    for name in SENSITIVE_CHILD_ENV:
        environment.pop(name, None)
    try:
        completed = subprocess.run(
            list(command),
            input=encoded + b"\n",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=environment,
            shell=False,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SupervisorError("Agent adapter did not complete") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace")[-1000:].strip()
        raise SupervisorError(
            f"Agent adapter exited with status {completed.returncode}"
            + (f": {detail}" if detail else "")
        )


def process_once(
    database: Path,
    *,
    adapter_command: Sequence[str],
    wake_policy: str,
    debounce: float,
    adapter_timeout: float,
    now: float | None = None,
) -> int:
    current_time = float(time.time() if now is None else now)
    with closing(_connect(database)) as connection:
        rows = _claim_batch(
            connection,
            wake_policy=wake_policy,
            debounce=debounce,
            now=current_time,
            claim_owner=f"sync:{os.getpid()}",
        )
        if not rows:
            return 0
        keys = [str(row["idempotency_key"]) for row in rows]
        try:
            _run_adapter(
                adapter_command,
                _batch_envelope(rows),
                timeout=max(1.0, min(float(adapter_timeout), 86_400.0)),
            )
        except SupervisorError as exc:
            highest_attempt = max(int(item["attempt_count"]) + 1 for item in rows)
            retry_at = current_time + _retry_delay(highest_attempt)
            connection.executemany(
                """
                UPDATE wake_events
                SET state = 'pending', claimed_at = NULL, available_at = ?,
                    claim_owner = NULL, adapter_run_id = NULL, last_error = ?
                WHERE idempotency_key = ?
                """,
                [(retry_at, str(exc)[-1000:], key) for key in keys],
            )
            raise
        connection.executemany(
            """
            UPDATE wake_events
            SET state = 'handled', handled_at = ?, claimed_at = NULL,
                claim_owner = NULL, adapter_run_id = NULL, last_error = NULL
            WHERE idempotency_key = ?
            """,
            [(current_time, key) for key in keys],
        )
        return len(rows)


def run_forever(
    database: Path,
    *,
    adapter_command: Sequence[str],
    wake_policy: str,
    debounce: float,
    poll_interval: float,
    adapter_timeout: float,
    once: bool,
) -> None:
    delay = max(0.1, min(float(poll_interval), 60.0))
    recover_inflight(
        database,
        reason="recovered after adapter supervisor restart",
    )
    while True:
        try:
            handled = process_once(
                database,
                adapter_command=adapter_command,
                wake_policy=wake_policy,
                debounce=max(0.0, min(float(debounce), 300.0)),
                adapter_timeout=adapter_timeout,
            )
            if once:
                return
            if handled == 0:
                time.sleep(delay)
        except KeyboardInterrupt:
            return
        except SupervisorError as exc:
            print(f"agent-bridge-supervisor: {exc}", file=sys.stderr)
            if once:
                raise
            time.sleep(delay)


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    database = Path(args.database).expanduser()
    try:
        if args.command == "enqueue":
            enqueue_event(database, sys.stdin.buffer.read(1_048_577))
        elif args.command == "status":
            print(json.dumps(queue_status(database), ensure_ascii=False))
        elif args.command == "run":
            run_forever(
                database,
                adapter_command=_parse_json_argv(args.adapter_command_json),
                wake_policy=args.wake_policy,
                debounce=args.debounce,
                poll_interval=args.poll_interval,
                adapter_timeout=args.adapter_timeout,
                once=bool(args.once),
            )
    except SupervisorError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
