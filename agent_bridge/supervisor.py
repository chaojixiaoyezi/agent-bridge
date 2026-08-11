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
from pathlib import Path
from typing import Any, Sequence


PRIORITIES = {"normal": 0, "important": 1, "mention": 2}
SENSITIVE_CHILD_ENV = {
    "AGENT_BRIDGE_TOKEN",
    "AGENT_TOKEN",
    "AGENT_BRIDGE_REGISTRATION_SECRET",
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
    "has_new",
    "has_room_activity",
    "backlog",
    "new_since_cursor",
    "room_activity_since_cursor",
    "server_time",
}


class SupervisorError(RuntimeError):
    pass


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
    idempotency_key = f"{participant_id}:{event_name}:{suffix}"
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
            last_error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_wake_events_dispatch
            ON wake_events(state, available_at, created_at);
        """
    )
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return connection


def enqueue_event(database: Path, raw: bytes, *, now: float | None = None) -> bool:
    payload, encoded, idempotency_key = _validated_envelope(raw)
    created_at = float(time.time() if now is None else now)
    with _connect(database) as connection:
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
    with _connect(database) as connection:
        counts = {
            str(row["state"]): int(row["count"])
            for row in connection.execute(
                "SELECT state, COUNT(*) AS count FROM wake_events GROUP BY state"
            ).fetchall()
        }
        newest = connection.execute(
            "SELECT MAX(event_id) AS event_id FROM wake_events"
        ).fetchone()
    return {
        "database": str(database.expanduser().resolve()),
        "counts": {
            "pending": counts.get("pending", 0),
            "inflight": counts.get("inflight", 0),
            "deferred": counts.get("deferred", 0),
            "handled": counts.get("handled", 0),
        },
        "newest_event_id": newest["event_id"] if newest is not None else None,
    }


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
) -> list[sqlite3.Row]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            """
            UPDATE wake_events
            SET state = 'pending', claimed_at = NULL,
                available_at = MIN(available_at, ?),
                last_error = COALESCE(last_error, 'recovered stale inflight event')
            WHERE state = 'inflight' AND claimed_at < ?
            """,
            (now, now - 7200),
        )
        rows = connection.execute(
            """
            SELECT * FROM wake_events
            WHERE state IN ('pending', 'deferred') AND available_at <= ?
            ORDER BY created_at, idempotency_key
            LIMIT ?
            """,
            (now, max(1, min(int(limit), 1000))),
        ).fetchall()
        if not rows:
            connection.execute("COMMIT")
            return []
        newest_created_at = max(float(row["created_at"]) for row in rows)
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
            SET state = 'inflight', claimed_at = ?, attempt_count = attempt_count + 1
            WHERE idempotency_key = ?
            """,
            [(now, key) for key in keys],
        )
        connection.execute("COMMIT")
        return rows
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def _batch_envelope(rows: Sequence[sqlite3.Row]) -> bytes:
    priorities = Counter(str(row["priority"]) for row in rows)
    highest = max(priorities, key=lambda priority: PRIORITIES[priority])
    event_ids = [int(row["event_id"]) for row in rows if row["event_id"] is not None]
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
    with _connect(database) as connection:
        rows = _claim_batch(
            connection,
            wake_policy=wake_policy,
            debounce=debounce,
            now=current_time,
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
            retry_at = current_time + min(
                300.0,
                max(1.0, 2.0 ** min(max(highest_attempt, 1), 8)),
            )
            connection.executemany(
                """
                UPDATE wake_events
                SET state = 'pending', claimed_at = NULL, available_at = ?,
                    last_error = ?
                WHERE idempotency_key = ?
                """,
                [(retry_at, str(exc)[-1000:], key) for key in keys],
            )
            raise
        connection.executemany(
            """
            UPDATE wake_events
            SET state = 'handled', handled_at = ?, claimed_at = NULL,
                last_error = NULL
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
