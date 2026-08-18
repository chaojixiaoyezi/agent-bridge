"""Codex app-server JSON-RPC transport."""

from __future__ import annotations

import json
import queue
import subprocess
import threading
from pathlib import Path
from typing import Any

from .codex_worker_contracts import CodexWorkerError


class CodexRpcError(CodexWorkerError):
    def __init__(self, method: str, error: Any) -> None:
        self.method = method
        self.error = error
        if isinstance(error, dict):
            message = str(error.get("message") or error)
        else:
            message = str(error)
        super().__init__(f"Codex app-server {method} failed: {message}")


class JsonRpcProcess:
    def __init__(self, command: list[str], *, cwd: Path, environment: dict[str, str]):
        self._command = command
        self._cwd = cwd
        self._environment = environment
        self._process: subprocess.Popen[str] | None = None
        self._pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._notifications: queue.Queue[dict[str, Any]] = queue.Queue()
        self._next_request_id = 1
        self._reader: threading.Thread | None = None
        self._closed = threading.Event()

    def start(self) -> None:
        if self._process is not None:
            raise CodexWorkerError("Codex app-server is already started")
        try:
            process = subprocess.Popen(
                self._command,
                cwd=self._cwd,
                env=self._environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=None,
                text=True,
                encoding="utf-8",
                bufsize=1,
                shell=False,
            )
        except OSError as exc:
            raise CodexWorkerError("cannot start Codex app-server") from exc
        if process.stdin is None or process.stdout is None:
            process.terminate()
            raise CodexWorkerError("Codex app-server pipes are unavailable")
        self._process = process
        self._reader = threading.Thread(
            target=self._read_messages,
            name="agent-bridge-codex-rpc",
            daemon=True,
        )
        self._reader.start()
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "agent-bridge-codex-worker",
                    "version": "0.10.0",
                },
                "capabilities": {"experimentalApi": True},
            },
            timeout=30,
        )
        self.notify("initialized", {})

    def _write(self, payload: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            raise CodexWorkerError("Codex app-server is not running")
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            try:
                process.stdin.write(encoded + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise CodexWorkerError("Codex app-server input closed") from exc

    def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float = 30,
    ) -> dict[str, Any]:
        with self._pending_lock:
            request_id = self._next_request_id
            self._next_request_id += 1
            result_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
            self._pending[request_id] = result_queue
        try:
            self._write({"id": request_id, "method": method, "params": params})
            try:
                response = result_queue.get(timeout=max(0.1, float(timeout)))
            except queue.Empty as exc:
                raise CodexWorkerError(
                    f"Codex app-server {method} timed out"
                ) from exc
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)
        if "error" in response:
            raise CodexRpcError(method, response["error"])
        result = response.get("result")
        if result is None:
            return {}
        if not isinstance(result, dict):
            raise CodexWorkerError(f"Codex app-server {method} returned non-object")
        return result

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"method": method, "params": params})

    def poll_notification(self, timeout: float = 0) -> dict[str, Any] | None:
        try:
            return self._notifications.get(timeout=max(0.0, float(timeout)))
        except queue.Empty:
            return None

    def _read_messages(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        try:
            for raw_line in process.stdout:
                try:
                    message = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(message, dict):
                    continue
                request_id = message.get("id")
                if isinstance(request_id, int) and (
                    "result" in message or "error" in message
                ):
                    with self._pending_lock:
                        target = self._pending.get(request_id)
                    if target is not None:
                        target.put(message)
                    continue
                if isinstance(message.get("method"), str):
                    if request_id is not None:
                        self._reject_server_request(message)
                    else:
                        self._notifications.put(message)
        finally:
            self._closed.set()
            with self._pending_lock:
                pending = list(self._pending.values())
            failure = {
                "error": {
                    "code": -32000,
                    "message": "Codex app-server exited",
                }
            }
            for target in pending:
                try:
                    target.put_nowait(failure)
                except queue.Full:
                    pass

    def _reject_server_request(self, message: dict[str, Any]) -> None:
        try:
            self._write(
                {
                    "id": message.get("id"),
                    "error": {
                        "code": -32601,
                        "message": (
                            "resident room reviewer cannot approve or answer "
                            "interactive host requests"
                        ),
                    },
                }
            )
        except CodexWorkerError:
            pass

    def is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def close(self) -> None:
        process = self._process
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        self._process = None
