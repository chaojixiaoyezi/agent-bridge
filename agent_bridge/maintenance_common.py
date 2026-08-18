"""Shared database and filesystem safety gates for maintenance operations."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote


class MaintenanceError(RuntimeError):
    """A maintenance gate failed before a release could be accepted."""


def _secure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _secure_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _readonly_sqlite(database: Path) -> sqlite3.Connection:
    resolved = database.expanduser().resolve()
    uri = f"file:{quote(resolved.as_posix(), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def database_diagnostics(database: str | Path) -> dict[str, Any]:
    path = Path(database).expanduser().resolve()
    if not path.is_file():
        raise MaintenanceError(f"database does not exist: {path}")
    try:
        with _readonly_sqlite(path) as connection:
            integrity_rows = [
                str(row[0])
                for row in connection.execute("PRAGMA integrity_check").fetchall()
            ]
            foreign_key_rows = [
                list(row)
                for row in connection.execute("PRAGMA foreign_key_check").fetchall()
            ]
            table_names = [
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                    "ORDER BY name"
                ).fetchall()
            ]
            counts: dict[str, int] = {}
            for table_name in table_names:
                escaped_name = table_name.replace('"', '""')
                row = connection.execute(
                    f'SELECT COUNT(*) FROM "{escaped_name}"'
                ).fetchone()
                counts[table_name] = int(row[0])
            user_version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            page_count = int(
                connection.execute("PRAGMA page_count").fetchone()[0]
            )
    except sqlite3.DatabaseError as exc:
        raise MaintenanceError(f"cannot inspect SQLite database: {path}") from exc
    return {
        "integrity_check": integrity_rows,
        "foreign_key_violations": foreign_key_rows,
        "user_version": user_version,
        "page_count": page_count,
        "counts": counts,
    }


def _assert_database_healthy(
    diagnostics: dict[str, Any],
    *,
    label: str,
) -> None:
    if diagnostics.get("integrity_check") != ["ok"]:
        raise MaintenanceError(f"{label} failed SQLite integrity_check")
    if diagnostics.get("foreign_key_violations"):
        raise MaintenanceError(f"{label} has foreign-key violations")


def _counts_do_not_decrease(
    before: dict[str, int],
    after: dict[str, int],
) -> bool:
    return all(after.get(table_name, 0) >= count for table_name, count in before.items())
