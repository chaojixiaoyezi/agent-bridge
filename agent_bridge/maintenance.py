from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .store import BridgeStore


SNAPSHOT_FORMAT_VERSION = 1
DEFAULT_VIEWER_LABEL = "com.xiaoyezi.agent-bridge-viewer"
DEFAULT_HEALTH_URL = "http://127.0.0.1:8765/api/health"
DEFAULT_DEPLOY_TIMEOUT_SECONDS = 90.0
MANIFEST_NAME = "manifest.json"
_SAFE_LABEL_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")


class MaintenanceError(RuntimeError):
    """A maintenance gate failed before a release could be accepted."""


def _json_print(value: Any, *, stream: Any = sys.stdout) -> None:
    print(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
        file=stream,
    )


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


def _online_backup(source: Path, destination: Path) -> dict[str, Any]:
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if not source.is_file():
        raise MaintenanceError(f"database does not exist: {source}")
    if source == destination:
        raise MaintenanceError("backup destination must differ from its source")
    if destination.exists():
        raise MaintenanceError(f"backup destination already exists: {destination}")
    _secure_directory(destination.parent)
    try:
        with _readonly_sqlite(source) as source_connection:
            with sqlite3.connect(str(destination), timeout=5.0) as target_connection:
                source_connection.backup(target_connection)
    except sqlite3.DatabaseError as exc:
        raise MaintenanceError(f"online SQLite backup failed: {source}") from exc
    _secure_file(destination)
    diagnostics = database_diagnostics(destination)
    _assert_database_healthy(diagnostics, label=str(destination))
    return diagnostics


def _safe_snapshot_label(value: str | None) -> str:
    normalized = _SAFE_LABEL_PATTERN.sub("-", str(value or "").strip()).strip(
        ".-"
    )
    return normalized[:64] or "manual"


def _git_head(repo_root: Path) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _snapshot_database_artifact(
    *,
    source: Path,
    destination: Path,
    relative_path: Path,
    role: str,
) -> dict[str, Any]:
    diagnostics = _online_backup(source, destination)
    return {
        "kind": "sqlite",
        "role": role,
        "source": str(source.expanduser().resolve()),
        "path": relative_path.as_posix(),
        "size_bytes": destination.stat().st_size,
        "sha256": _sha256(destination),
        "diagnostics": diagnostics,
    }


def create_snapshot(
    *,
    database: str | Path,
    output_root: str | Path,
    viewer_plist: str | Path | None = None,
    connector_queues_root: str | Path | None = None,
    label: str | None = None,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    source_database = Path(database).expanduser().resolve()
    root = Path(output_root).expanduser().resolve()
    _secure_directory(root)
    now = datetime.now(timezone.utc)
    suffix = uuid.uuid4().hex[:8]
    bundle_name = (
        f"snapshot-{now.strftime('%Y%m%dT%H%M%SZ')}-"
        f"{_safe_snapshot_label(label)}-{suffix}"
    )
    partial = Path(tempfile.mkdtemp(prefix=f".{bundle_name}-", dir=root))
    _secure_directory(partial)
    final_bundle = root / bundle_name
    try:
        artifacts: list[dict[str, Any]] = []
        relative_database = Path("databases") / "bridge.db"
        artifacts.append(
            _snapshot_database_artifact(
                source=source_database,
                destination=partial / relative_database,
                relative_path=relative_database,
                role="central",
            )
        )

        if connector_queues_root is not None:
            queue_root = Path(connector_queues_root).expanduser().resolve()
            if queue_root.is_dir():
                queue_sources = sorted(queue_root.rglob("wake-queue.db"))
                for index, queue_source in enumerate(queue_sources, start=1):
                    relative_source = queue_source.relative_to(queue_root)
                    safe_parts = [
                        _safe_snapshot_label(part)
                        for part in relative_source.parts[:-1]
                    ]
                    relative_queue = (
                        Path("databases")
                        / "connector-queues"
                        / Path(*safe_parts)
                        / f"wake-queue-{index}.db"
                    )
                    artifacts.append(
                        _snapshot_database_artifact(
                            source=queue_source,
                            destination=partial / relative_queue,
                            relative_path=relative_queue,
                            role="connector_queue",
                        )
                    )

        if viewer_plist is not None:
            plist_source = Path(viewer_plist).expanduser().resolve()
            if not plist_source.is_file():
                raise MaintenanceError(
                    f"viewer service definition does not exist: {plist_source}"
                )
            relative_plist = Path("service") / plist_source.name
            plist_destination = partial / relative_plist
            _secure_directory(plist_destination.parent)
            shutil.copyfile(plist_source, plist_destination)
            _secure_file(plist_destination)
            artifacts.append(
                {
                    "kind": "file",
                    "role": "viewer_service",
                    "source": str(plist_source),
                    "path": relative_plist.as_posix(),
                    "size_bytes": plist_destination.stat().st_size,
                    "sha256": _sha256(plist_destination),
                }
            )

        resolved_repo_root = (
            Path(repo_root).expanduser().resolve()
            if repo_root is not None
            else Path(__file__).resolve().parents[1]
        )
        manifest = {
            "format_version": SNAPSHOT_FORMAT_VERSION,
            "created_at": now.isoformat(),
            "label": _safe_snapshot_label(label),
            "git_head": _git_head(resolved_repo_root),
            "artifacts": artifacts,
        }
        manifest_path = partial / MANIFEST_NAME
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        _secure_file(manifest_path)
        partial.rename(final_bundle)
    except Exception:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    return {
        "status": "ok",
        "snapshot": str(final_bundle),
        "manifest": str(final_bundle / MANIFEST_NAME),
        "artifact_count": len(artifacts),
        "git_head": manifest["git_head"],
    }


def _load_manifest(manifest_path: str | Path) -> tuple[Path, dict[str, Any]]:
    path = Path(manifest_path).expanduser().resolve()
    if not path.is_file():
        raise MaintenanceError(f"snapshot manifest does not exist: {path}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MaintenanceError(f"cannot read snapshot manifest: {path}") from exc
    if not isinstance(manifest, dict):
        raise MaintenanceError("snapshot manifest must be a JSON object")
    if manifest.get("format_version") != SNAPSHOT_FORMAT_VERSION:
        raise MaintenanceError("unsupported snapshot manifest version")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise MaintenanceError("snapshot manifest has no artifacts")
    return path, manifest


def _artifact_path(bundle: Path, relative_value: object) -> Path:
    relative = Path(str(relative_value or ""))
    if relative.is_absolute() or not relative.parts:
        raise MaintenanceError("snapshot artifact path must be relative")
    resolved = (bundle / relative).resolve()
    try:
        resolved.relative_to(bundle.resolve())
    except ValueError as exc:
        raise MaintenanceError("snapshot artifact escapes its bundle") from exc
    return resolved


def verify_snapshot(manifest_path: str | Path) -> dict[str, Any]:
    path, manifest = _load_manifest(manifest_path)
    bundle = path.parent
    verified: list[dict[str, Any]] = []
    for raw_artifact in manifest["artifacts"]:
        if not isinstance(raw_artifact, dict):
            raise MaintenanceError("snapshot artifact must be a JSON object")
        artifact = dict(raw_artifact)
        artifact_path = _artifact_path(bundle, artifact.get("path"))
        if not artifact_path.is_file():
            raise MaintenanceError(f"snapshot artifact is missing: {artifact_path}")
        expected_size = int(artifact.get("size_bytes", -1))
        if artifact_path.stat().st_size != expected_size:
            raise MaintenanceError(f"snapshot artifact size changed: {artifact_path}")
        if _sha256(artifact_path) != str(artifact.get("sha256", "")):
            raise MaintenanceError(f"snapshot artifact hash changed: {artifact_path}")
        result = {
            "path": artifact["path"],
            "kind": artifact.get("kind"),
            "role": artifact.get("role"),
            "sha256": artifact["sha256"],
        }
        if artifact.get("kind") == "sqlite":
            diagnostics = database_diagnostics(artifact_path)
            _assert_database_healthy(diagnostics, label=str(artifact_path))
            if diagnostics != artifact.get("diagnostics"):
                raise MaintenanceError(
                    f"snapshot database diagnostics changed: {artifact_path}"
                )
            result["user_version"] = diagnostics["user_version"]
            result["counts"] = diagnostics["counts"]
        verified.append(result)
    return {
        "status": "ok",
        "manifest": str(path),
        "git_head": manifest.get("git_head"),
        "artifacts": verified,
    }


def _counts_do_not_decrease(
    before: dict[str, int],
    after: dict[str, int],
) -> bool:
    return all(after.get(table_name, 0) >= count for table_name, count in before.items())


def rehearse_restore(
    manifest_path: str | Path,
    *,
    work_root: str | Path | None = None,
) -> dict[str, Any]:
    verification = verify_snapshot(manifest_path)
    path, manifest = _load_manifest(manifest_path)
    bundle = path.parent
    central_artifacts = [
        artifact
        for artifact in manifest["artifacts"]
        if isinstance(artifact, dict)
        and artifact.get("kind") == "sqlite"
        and artifact.get("role") == "central"
    ]
    if len(central_artifacts) != 1:
        raise MaintenanceError("snapshot must contain exactly one central database")
    root = (
        Path(work_root).expanduser().resolve()
        if work_root is not None
        else None
    )
    if root is not None:
        _secure_directory(root)
    with tempfile.TemporaryDirectory(prefix="agent-bridge-restore-", dir=root) as tmp:
        rehearsal_database = Path(tmp) / "bridge.db"
        source = _artifact_path(bundle, central_artifacts[0]["path"])
        shutil.copyfile(source, rehearsal_database)
        _secure_file(rehearsal_database)
        before = database_diagnostics(rehearsal_database)
        BridgeStore(rehearsal_database)
        after = database_diagnostics(rehearsal_database)
        _assert_database_healthy(after, label="restored rehearsal database")
        if after["user_version"] < before["user_version"]:
            raise MaintenanceError("restore rehearsal reduced the schema version")
        if not _counts_do_not_decrease(before["counts"], after["counts"]):
            raise MaintenanceError("restore rehearsal lost database rows")
    return {
        "status": "ok",
        "manifest": str(path),
        "verified_artifacts": len(verification["artifacts"]),
        "schema_before": before["user_version"],
        "schema_after": after["user_version"],
        "counts_preserved": True,
        "live_database_modified": False,
    }


def parse_launchctl_list(output: str, *, viewer_label: str) -> dict[str, int]:
    processes: dict[str, int] = {}
    for raw_line in output.splitlines():
        parts = raw_line.split(None, 2)
        if len(parts) != 3 or not parts[0].isdigit():
            continue
        label = parts[2].strip()
        compact = label.casefold().replace("-", "")
        if label == viewer_label or "agentbridge" not in compact:
            continue
        processes[label] = int(parts[0])
    return processes


def _run_command(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        check=False,
        capture_output=True,
        text=True,
    )


def _launchd_agent_processes(
    *,
    viewer_label: str,
    command_runner: Callable[
        [Sequence[str]], subprocess.CompletedProcess[str]
    ] = _run_command,
) -> dict[str, int]:
    result = command_runner(["launchctl", "list"])
    if result.returncode != 0:
        raise MaintenanceError(
            f"cannot list launchd services: {result.stderr.strip()}"
        )
    return parse_launchctl_list(result.stdout, viewer_label=viewer_label)


def _launchd_viewer_pid(
    *,
    service_target: str,
    command_runner: Callable[
        [Sequence[str]], subprocess.CompletedProcess[str]
    ] = _run_command,
) -> int | None:
    result = command_runner(["launchctl", "print", service_target])
    if result.returncode != 0:
        return None
    match = re.search(r"^\s*pid\s*=\s*(\d+)\s*$", result.stdout, re.MULTILINE)
    return int(match.group(1)) if match else None


def _read_health(url: str) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=2.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise MaintenanceError(f"health endpoint is not ready: {url}") from exc
    if not isinstance(payload, dict):
        raise MaintenanceError("health endpoint returned a non-object payload")
    return payload


def _validate_deployed_database(
    *,
    baseline: dict[str, Any],
    current: dict[str, Any],
) -> None:
    _assert_database_healthy(current, label="deployed central database")
    if current["user_version"] < baseline["user_version"]:
        raise MaintenanceError("deployment reduced the central schema version")
    if not _counts_do_not_decrease(baseline["counts"], current["counts"]):
        raise MaintenanceError("deployment lost central database rows")


def deploy_viewer(
    *,
    database: str | Path,
    health_url: str = DEFAULT_HEALTH_URL,
    viewer_label: str = DEFAULT_VIEWER_LABEL,
    expected_registration_mode: str | None = None,
    timeout_seconds: float = DEFAULT_DEPLOY_TIMEOUT_SECONDS,
    user_id: int | None = None,
    command_runner: Callable[
        [Sequence[str]], subprocess.CompletedProcess[str]
    ] = _run_command,
    health_reader: Callable[[str], dict[str, Any]] = _read_health,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    if sys.platform != "darwin" and command_runner is _run_command:
        raise MaintenanceError("viewer rolling deployment currently requires macOS")
    baseline_database = database_diagnostics(database)
    _assert_database_healthy(baseline_database, label="central database")
    try:
        baseline_health = health_reader(health_url)
    except MaintenanceError as exc:
        raise MaintenanceError("refusing deployment because preflight health failed") from exc
    if baseline_health.get("status") != "ok":
        raise MaintenanceError("refusing deployment because preflight status is not ok")
    active_registration_mode = str(
        baseline_health.get("web_registration_mode", "")
    )
    required_registration_mode = (
        str(expected_registration_mode).strip()
        if expected_registration_mode is not None
        else active_registration_mode
    )
    if active_registration_mode != required_registration_mode:
        raise MaintenanceError(
            "preflight registration mode differs from the required mode"
        )

    effective_uid = os.getuid() if user_id is None else int(user_id)
    service_target = f"gui/{effective_uid}/{viewer_label}"
    baseline_agents = _launchd_agent_processes(
        viewer_label=viewer_label,
        command_runner=command_runner,
    )
    baseline_viewer_pid = _launchd_viewer_pid(
        service_target=service_target,
        command_runner=command_runner,
    )
    kickstart = command_runner(["launchctl", "kickstart", "-k", service_target])
    if kickstart.returncode != 0:
        raise MaintenanceError(
            f"viewer kickstart failed: {kickstart.stderr.strip()}"
        )

    deadline = time.monotonic() + max(1.0, float(timeout_seconds))
    last_error = "viewer did not become ready"
    deployed_health: dict[str, Any] | None = None
    deployed_viewer_pid: int | None = None
    while time.monotonic() < deadline:
        try:
            candidate_health = health_reader(health_url)
            candidate_pid = _launchd_viewer_pid(
                service_target=service_target,
                command_runner=command_runner,
            )
            if candidate_health.get("status") != "ok":
                last_error = "viewer health status is not ok"
            elif (
                candidate_health.get("web_registration_mode")
                != required_registration_mode
            ):
                last_error = "viewer registration mode changed"
            elif candidate_pid is None:
                last_error = "viewer has no launchd PID"
            elif baseline_viewer_pid is not None and candidate_pid == baseline_viewer_pid:
                last_error = "viewer PID did not roll"
            else:
                deployed_health = candidate_health
                deployed_viewer_pid = candidate_pid
                break
        except MaintenanceError as exc:
            last_error = str(exc)
        sleep(0.5)
    if deployed_health is None or deployed_viewer_pid is None:
        raise MaintenanceError(f"viewer failed its post-deploy gate: {last_error}")

    deployed_agents = _launchd_agent_processes(
        viewer_label=viewer_label,
        command_runner=command_runner,
    )
    if deployed_agents != baseline_agents:
        raise MaintenanceError("Agent launchd PID set changed during viewer deployment")
    current_database = database_diagnostics(database)
    _validate_deployed_database(
        baseline=baseline_database,
        current=current_database,
    )
    return {
        "status": "ok",
        "service": service_target,
        "viewer_pid_before": baseline_viewer_pid,
        "viewer_pid_after": deployed_viewer_pid,
        "agent_process_count": len(deployed_agents),
        "agent_processes_unchanged": True,
        "web_registration_mode": required_registration_mode,
        "database_schema_before": baseline_database["user_version"],
        "database_schema_after": current_database["user_version"],
        "database_counts_preserved": True,
    }


def release_viewer(
    *,
    database: str | Path,
    output_root: str | Path,
    viewer_plist: str | Path,
    connector_queues_root: str | Path | None,
    label: str,
    health_url: str,
    viewer_label: str,
    expected_registration_mode: str | None,
    timeout_seconds: float,
) -> dict[str, Any]:
    snapshot = create_snapshot(
        database=database,
        output_root=output_root,
        viewer_plist=viewer_plist,
        connector_queues_root=connector_queues_root,
        label=label,
    )
    verification = verify_snapshot(snapshot["manifest"])
    rehearsal = rehearse_restore(snapshot["manifest"])
    deployment = deploy_viewer(
        database=database,
        health_url=health_url,
        viewer_label=viewer_label,
        expected_registration_mode=expected_registration_mode,
        timeout_seconds=timeout_seconds,
    )
    return {
        "status": "ok",
        "snapshot": snapshot,
        "verification": {
            "status": verification["status"],
            "artifact_count": len(verification["artifacts"]),
        },
        "restore_rehearsal": rehearsal,
        "deployment": deployment,
    }


def build_parser() -> argparse.ArgumentParser:
    bridge_home = Path(
        os.environ.get("AGENT_BRIDGE_HOME", "~/.agent-bridge")
    ).expanduser()
    default_database = Path(
        os.environ.get("AGENT_BRIDGE_DB", str(bridge_home / "bridge.db"))
    ).expanduser()
    parser = argparse.ArgumentParser(
        prog="agent-bridge-maintain",
        description=(
            "Online snapshots, restore rehearsals, and guarded viewer-only "
            "rolling deployments. This tool never overwrites a live database."
        ),
    )
    parser.add_argument(
        "--database",
        default=str(default_database),
        help="central bridge.db path",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot = subparsers.add_parser("snapshot")
    snapshot.add_argument("--output-root", required=True)
    snapshot.add_argument("--viewer-plist")
    snapshot.add_argument("--connector-queues-root")
    snapshot.add_argument("--label", default="manual")

    verify = subparsers.add_parser("verify")
    verify.add_argument("--manifest", required=True)

    rehearse = subparsers.add_parser("rehearse-restore")
    rehearse.add_argument("--manifest", required=True)
    rehearse.add_argument("--work-root")

    deploy = subparsers.add_parser("deploy-viewer")
    deploy.add_argument("--health-url", default=DEFAULT_HEALTH_URL)
    deploy.add_argument("--viewer-label", default=DEFAULT_VIEWER_LABEL)
    deploy.add_argument("--expected-registration-mode")
    deploy.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_DEPLOY_TIMEOUT_SECONDS,
    )

    release = subparsers.add_parser("release-viewer")
    release.add_argument("--output-root", required=True)
    release.add_argument("--viewer-plist", required=True)
    release.add_argument("--connector-queues-root")
    release.add_argument("--label", required=True)
    release.add_argument("--health-url", default=DEFAULT_HEALTH_URL)
    release.add_argument("--viewer-label", default=DEFAULT_VIEWER_LABEL)
    release.add_argument("--expected-registration-mode")
    release.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_DEPLOY_TIMEOUT_SECONDS,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "snapshot":
            result = create_snapshot(
                database=args.database,
                output_root=args.output_root,
                viewer_plist=args.viewer_plist,
                connector_queues_root=args.connector_queues_root,
                label=args.label,
            )
        elif args.command == "verify":
            result = verify_snapshot(args.manifest)
        elif args.command == "rehearse-restore":
            result = rehearse_restore(
                args.manifest,
                work_root=args.work_root,
            )
        elif args.command == "deploy-viewer":
            result = deploy_viewer(
                database=args.database,
                health_url=args.health_url,
                viewer_label=args.viewer_label,
                expected_registration_mode=args.expected_registration_mode,
                timeout_seconds=args.timeout_seconds,
            )
        elif args.command == "release-viewer":
            result = release_viewer(
                database=args.database,
                output_root=args.output_root,
                viewer_plist=args.viewer_plist,
                connector_queues_root=args.connector_queues_root,
                label=args.label,
                health_url=args.health_url,
                viewer_label=args.viewer_label,
                expected_registration_mode=args.expected_registration_mode,
                timeout_seconds=args.timeout_seconds,
            )
        else:
            raise AssertionError(f"unhandled command: {args.command}")
    except MaintenanceError as exc:
        _json_print(
            {"status": "error", "error": str(exc)},
            stream=sys.stderr,
        )
        return 1
    _json_print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
