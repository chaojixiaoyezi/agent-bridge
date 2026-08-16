from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
from dataclasses import dataclass
from typing import Mapping


MAX_GUIDE_BYTES = 128 * 1024
TMUX_PANE_PATTERN = re.compile(r"^%[0-9]{1,12}$")


class ClaudeGuideError(RuntimeError):
    pass


@dataclass(frozen=True)
class TmuxClaudeGuide:
    """Inject one authenticated Bridge prompt into the exact owning TUI pane."""

    binary: str
    pane: str

    @property
    def transport_name(self) -> str:
        return "claude-tmux-guide"

    def deliver(self, prompt: str) -> None:
        encoded = str(prompt or "").encode("utf-8")
        if not encoded or len(encoded) > MAX_GUIDE_BYTES or b"\x00" in encoded:
            raise ClaudeGuideError(
                "Claude guide prompt must contain 1-131072 safe UTF-8 bytes"
            )
        buffer_name = f"agent-bridge-{os.getpid()}-{secrets.token_hex(8)}"
        loaded = subprocess.run(
            [self.binary, "load-buffer", "-b", buffer_name, "-"],
            input=encoded,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=5,
            check=False,
        )
        if loaded.returncode != 0:
            raise ClaudeGuideError(self._failure("load", loaded.stderr))
        try:
            # tmux executes both commands in one server request. Bracketed paste
            # keeps multiline room JSON as one prompt; Enter submits it to the
            # already-running Claude TUI and becomes a steering message if a
            # model turn is active.
            delivered = subprocess.run(
                [
                    self.binary,
                    "paste-buffer",
                    "-d",
                    "-p",
                    "-b",
                    buffer_name,
                    "-t",
                    self.pane,
                    ";",
                    "send-keys",
                    "-t",
                    self.pane,
                    "Enter",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=5,
                check=False,
            )
            if delivered.returncode != 0:
                raise ClaudeGuideError(
                    self._failure("paste and submit", delivered.stderr)
                )
        finally:
            # paste-buffer -d normally removes it. This best-effort cleanup is
            # for a pane that disappeared between load and delivery.
            subprocess.run(
                [self.binary, "delete-buffer", "-b", buffer_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
            )

    @staticmethod
    def _failure(operation: str, stderr: bytes) -> str:
        detail = stderr.decode("utf-8", errors="replace").strip()[-300:]
        suffix = f": {detail}" if detail else ""
        return f"tmux Claude guide could not {operation}{suffix}"


def tmux_guide_from_environment(
    environment: Mapping[str, str] | None = None,
) -> TmuxClaudeGuide | None:
    values = os.environ if environment is None else environment
    pane = str(values.get("TMUX_PANE") or "").strip()
    server = str(values.get("TMUX") or "").strip()
    if not server or TMUX_PANE_PATTERN.fullmatch(pane) is None:
        return None
    binary = shutil.which("tmux", path=values.get("PATH"))
    if not binary:
        return None
    return TmuxClaudeGuide(binary=binary, pane=pane)
