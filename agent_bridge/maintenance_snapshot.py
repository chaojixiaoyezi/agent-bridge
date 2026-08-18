"""Non-destructive snapshot, verification, and restore rehearsal operations."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .maintenance_common import (
    MaintenanceError,
    _assert_database_healthy,
    _counts_do_not_decrease,
    _readonly_sqlite,
    _secure_directory,
    _secure_file,
    _sha256,
    database_diagnostics,
)
from .store import BridgeStore


SNAPSHOT_FORMAT_VERSION = 1


MANIFEST_NAME = "manifest.json"


_SAFE_LABEL_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")


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
    attachment_root: str | Path | None = None,
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

        resolved_attachment_root = (
            Path(attachment_root).expanduser().resolve()
            if attachment_root is not None
            else Path(
                os.environ.get(
                    "AGENT_BRIDGE_ATTACHMENT_ROOT",
                    str(source_database.parent / "attachments"),
                )
            ).expanduser().resolve()
        )
        with _readonly_sqlite(partial / relative_database) as snapshot_connection:
            attachment_table_exists = snapshot_connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'message_attachments'"
            ).fetchone()
            attachment_ids = (
                [
                    str(row[0])
                    for row in snapshot_connection.execute(
                        "SELECT attachment_id FROM message_attachments "
                        "ORDER BY attachment_id"
                    ).fetchall()
                ]
                if attachment_table_exists is not None
                else []
            )
        for attachment_id in attachment_ids:
            source_attachment = (
                resolved_attachment_root
                / "blobs"
                / attachment_id[-2:]
                / f"{attachment_id}.blob"
            )
            if not source_attachment.is_file():
                raise MaintenanceError(
                    "referenced message attachment is missing: "
                    f"{source_attachment}"
                )
            relative_attachment = (
                Path("attachments")
                / "blobs"
                / attachment_id[-2:]
                / f"{_safe_snapshot_label(attachment_id)}.blob"
            )
            destination_attachment = partial / relative_attachment
            _secure_directory(destination_attachment.parent)
            shutil.copyfile(source_attachment, destination_attachment)
            _secure_file(destination_attachment)
            artifacts.append(
                {
                    "kind": "file",
                    "role": "message_attachment",
                    "attachment_id": attachment_id,
                    "source": str(source_attachment),
                    "path": relative_attachment.as_posix(),
                    "size_bytes": destination_attachment.stat().st_size,
                    "sha256": _sha256(destination_attachment),
                }
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
        rehearsal_root = Path(tmp)
        rehearsal_database = rehearsal_root / "bridge.db"
        source = _artifact_path(bundle, central_artifacts[0]["path"])
        shutil.copyfile(source, rehearsal_database)
        _secure_file(rehearsal_database)
        attachment_artifacts = [
            artifact
            for artifact in manifest["artifacts"]
            if isinstance(artifact, dict)
            and artifact.get("kind") == "file"
            and artifact.get("role") == "message_attachment"
        ]
        for artifact in attachment_artifacts:
            attachment_source = _artifact_path(bundle, artifact["path"])
            attachment_destination = rehearsal_root / str(artifact["path"])
            _secure_directory(attachment_destination.parent)
            shutil.copyfile(attachment_source, attachment_destination)
            _secure_file(attachment_destination)
        before = database_diagnostics(rehearsal_database)
        restored_store = BridgeStore(rehearsal_database)
        after = database_diagnostics(rehearsal_database)
        _assert_database_healthy(after, label="restored rehearsal database")
        if after["user_version"] < before["user_version"]:
            raise MaintenanceError("restore rehearsal reduced the schema version")
        if not _counts_do_not_decrease(before["counts"], after["counts"]):
            raise MaintenanceError("restore rehearsal lost database rows")
        for artifact in attachment_artifacts:
            attachment_id = str(artifact.get("attachment_id") or "")
            restored_attachment = restored_store._attachment_blob_path(attachment_id)
            if not restored_attachment.is_file():
                raise MaintenanceError(
                    "restore rehearsal did not place a referenced attachment: "
                    f"{attachment_id}"
                )
            if _sha256(restored_attachment) != str(artifact["sha256"]):
                raise MaintenanceError(
                    "restore rehearsal attachment hash changed: "
                    f"{attachment_id}"
                )
    return {
        "status": "ok",
        "manifest": str(path),
        "verified_artifacts": len(verification["artifacts"]),
        "schema_before": before["user_version"],
        "schema_after": after["user_version"],
        "counts_preserved": True,
        "restored_attachment_count": len(attachment_artifacts),
        "live_database_modified": False,
    }
