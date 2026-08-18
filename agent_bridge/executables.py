from __future__ import annotations

import os
import shutil
from pathlib import Path


def resolve_executable_path(command: str) -> str | None:
    """Return the canonical executable path used to locate sibling helpers.

    Codex locates ``codex-code-mode-host`` beside its own executable. Invoking a
    bundle binary through a symlink such as ``/opt/homebrew/bin/codex`` makes it
    search beside the symlink instead, so resident processes must launch the
    resolved target rather than the alias returned by ``PATH`` lookup.
    """

    discovered = shutil.which(str(command or "").strip())
    if discovered is None:
        return None
    try:
        resolved = Path(discovered).expanduser().resolve(strict=True)
    except OSError:
        return None
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        return None
    return str(resolved)
