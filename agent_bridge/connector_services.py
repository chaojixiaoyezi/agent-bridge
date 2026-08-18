"""launchd and systemd service rendering and activation for connectors."""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

from .connector_contracts import ConnectorSetupError


def _launchd_plist(
    *,
    label: str,
    program_arguments: list[str],
    environment: dict[str, str],
    stdout_path: Path,
    stderr_path: Path,
) -> bytes:
    return plistlib.dumps(
        {
            "Label": label,
            "ProgramArguments": program_arguments,
            "EnvironmentVariables": environment,
            "RunAtLoad": True,
            "KeepAlive": True,
            "ProcessType": "Background",
            "StandardOutPath": str(stdout_path),
            "StandardErrorPath": str(stderr_path),
        },
        fmt=plistlib.FMT_XML,
        sort_keys=False,
    )


def _run_checked(command: list[str], *, description: str) -> None:
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConnectorSetupError(f"{description} failed") from exc
    if completed.returncode != 0:
        raise ConnectorSetupError(f"{description} failed")


def _activate_launchd(services: list[tuple[str, Path]]) -> None:
    domain = f"gui/{os.getuid()}"
    for label, plist_path in services:
        try:
            existing = subprocess.run(
                ["launchctl", "print", f"{domain}/{label}"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ConnectorSetupError(
                f"checking existing connector service {label} failed"
            ) from exc
        if existing.returncode == 0:
            _run_checked(
                ["launchctl", "bootout", f"{domain}/{label}"],
                description=f"stopping existing connector service {label}",
            )
        _run_checked(
            ["launchctl", "bootstrap", domain, str(plist_path)],
            description=f"starting connector service {label}",
        )


def _systemd_quote(value: str) -> str:
    escaped = value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    return '"' + escaped + '"'


def _systemd_unit(
    *,
    description: str,
    program_arguments: list[str],
    environment: dict[str, str],
) -> bytes:
    environment_lines = "\n".join(
        f"Environment={_systemd_quote(f'{key}={value}')}"
        for key, value in sorted(environment.items())
    )
    command = " ".join(_systemd_quote(argument) for argument in program_arguments)
    content = (
        "[Unit]\n"
        f"Description={description}\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"{environment_lines}\n"
        f"ExecStart={command}\n"
        "Restart=always\n"
        "RestartSec=2\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    return content.encode("utf-8")


def _activate_systemd(units: list[Path]) -> None:
    _run_checked(["systemctl", "--user", "daemon-reload"], description="systemd reload")
    _run_checked(
        ["systemctl", "--user", "enable", "--now", *[unit.name for unit in units]],
        description="starting connector services",
    )
