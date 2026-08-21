"""Create one stable local Codex endpoint bound to the accepting TUI thread."""

from __future__ import annotations

import os
import platform
import stat
import uuid
from pathlib import Path

from .connector_contracts import _state_root
from .tui_binding import (
    CODEX_THREAD_ID_PATTERN,
    NativeTuiBinding,
    NativeTuiError,
    validate_native_tui_binding,
)


CODEX_ENDPOINT_DIRECTORY = "codex-endpoints"


def _read_endpoint_id(endpoint_file: Path) -> str:
    try:
        metadata = endpoint_file.lstat()
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError) as exc:
        raise NativeTuiError("Codex endpoint identity is unreadable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise NativeTuiError("Codex endpoint identity must be a private file")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise NativeTuiError("Codex endpoint identity belongs to another user")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise NativeTuiError("Codex endpoint identity permissions are too broad")
    try:
        existing = endpoint_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise NativeTuiError("Codex endpoint identity is unreadable") from exc
    if not existing.startswith("codex_endpoint_") or len(existing) > 256:
        raise NativeTuiError("Codex endpoint identity is invalid")
    return existing


def _read_or_create_endpoint_id(root: Path, *, thread_id: str) -> str:
    endpoint_directory = root / CODEX_ENDPOINT_DIRECTORY
    endpoint_directory.mkdir(parents=True, exist_ok=True)
    os.chmod(endpoint_directory, 0o700)
    endpoint_file = endpoint_directory / f"{thread_id}.id"
    try:
        return _read_endpoint_id(endpoint_file)
    except FileNotFoundError:
        pass

    candidate = "codex_endpoint_" + uuid.uuid4().hex
    try:
        descriptor = os.open(
            endpoint_file,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        return _read_endpoint_id(endpoint_file)
    except OSError as exc:
        raise NativeTuiError("Codex endpoint identity cannot be created") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(candidate + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        try:
            endpoint_file.unlink()
        except OSError:
            pass
        raise NativeTuiError("Codex endpoint identity cannot be created") from exc
    return candidate


def codex_native_binding(
    *,
    thread_id: str | None,
    workspace: str | Path,
    home: Path | None = None,
    system_name: str | None = None,
    binary: str = "codex",
) -> NativeTuiBinding:
    """Bind the exact accepting Codex thread without opening a second writer."""

    # Retained for callers of the short-lived invitation CLI. Direct duty is
    # performed by the accepting TUI through MCP and never launches this binary.
    del binary

    normalized_thread = str(thread_id or "").strip().casefold()
    if CODEX_THREAD_ID_PATTERN.fullmatch(normalized_thread) is None:
        raise NativeTuiError(
            "Codex resident invitations must be accepted inside the exact TUI "
            "being bound (CODEX_THREAD_ID is missing or invalid)"
        )
    cwd = Path(workspace).expanduser().resolve()
    if not cwd.is_dir():
        raise NativeTuiError("Codex TUI working directory does not exist")
    user_home = (home or Path.home()).expanduser().resolve()
    host_system = system_name or platform.system()
    endpoint_id = _read_or_create_endpoint_id(
        _state_root(user_home, host_system),
        thread_id=normalized_thread,
    )
    return validate_native_tui_binding(
        adapter_kind="codex",
        endpoint_id=endpoint_id,
        native_session_id=normalized_thread,
        capabilities=["chat", "structured-task", "direct-duty"],
        transport={
            "kind": "codex-mcp-duty",
            "cwd": str(cwd),
        },
    )
