from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .maintenance_common import MaintenanceError
from .maintenance_common import database_diagnostics as database_diagnostics
from .maintenance_deploy import (
    DEFAULT_DEPLOY_TIMEOUT_SECONDS,
    DEFAULT_HEALTH_URL,
    DEFAULT_VIEWER_LABEL,
    deploy_viewer,
)
from .maintenance_deploy import parse_launchctl_list as parse_launchctl_list
from .maintenance_snapshot import create_snapshot, rehearse_restore, verify_snapshot


def _json_print(value: Any, *, stream: Any = sys.stdout) -> None:
    print(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
        file=stream,
    )


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
