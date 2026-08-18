from __future__ import annotations

import json
import os
import signal
import socket
import sqlite3
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest
import uvicorn

from agent_bridge.codex_worker import CodexThreadHost
from agent_bridge.store import MESSAGE_COOLDOWN_SECONDS, BridgeStore
from agent_bridge.supervisor import queue_status
from agent_bridge.viewer import create_app


pytestmark = pytest.mark.live_codex

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CODEX_BINARY = Path("/opt/homebrew/bin/codex")
PROCESS_TIMEOUT_SECONDS = 420.0
BRIDGE_ENVIRONMENT_KEYS = {
    "AGENT_BRIDGE_AUTO_REGISTER",
    "AGENT_BRIDGE_CAPABILITIES",
    "AGENT_BRIDGE_CLIENT_TYPE",
    "AGENT_BRIDGE_COMPONENT",
    "AGENT_BRIDGE_CONNECTOR_ID",
    "AGENT_BRIDGE_CONVERSATION_ID",
    "AGENT_BRIDGE_DB",
    "AGENT_BRIDGE_ENROLLMENT_TOKEN",
    "AGENT_BRIDGE_ENROLLMENT_TOKEN_FILE",
    "AGENT_BRIDGE_HOME",
    "AGENT_BRIDGE_INVITATION_TOKEN",
    "AGENT_BRIDGE_INVITATION_TOKEN_FILE",
    "AGENT_BRIDGE_REGISTRATION_SECRET",
    "AGENT_BRIDGE_REGISTRATION_SECRET_FILE",
    "AGENT_BRIDGE_ROLES",
    "AGENT_BRIDGE_SIGNATURE",
    "AGENT_BRIDGE_TOKEN",
    "AGENT_BRIDGE_URL",
    "AGENT_BRIDGE_USERNAME",
    "AGENT_TOKEN",
}


def _live_tests_enabled() -> bool:
    return os.environ.get("AGENT_BRIDGE_RUN_LIVE_CODEX_TESTS", "").strip() == "1"


def _isolated_environment(**overrides: str) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in BRIDGE_ENVIRONMENT_KEYS
    }
    environment.update(overrides)
    return environment


def _wait_until(
    probe: Callable[[], Any],
    *,
    timeout: float,
    description: str,
) -> Any:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            result = probe()
            if result:
                return result
        except (OSError, sqlite3.Error) as exc:
            last_error = exc
        time.sleep(0.05)
    detail = f"; last error: {last_error}" if last_error is not None else ""
    raise AssertionError(f"timed out waiting for {description}{detail}")


def _database_rows(database: Path, query: str, parameters: tuple = ()) -> list[dict]:
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(query, parameters).fetchall()]


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def _log_tail(path: Path, *, lines: int = 80) -> str:
    if not path.exists():
        return ""
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


@contextmanager
def _running_bridge(database: Path, registration_secret: str) -> Iterator[str]:
    app = create_app(
        database,
        registration_secret=registration_secret,
        enable_resident_repair=False,
    )
    listener_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener_socket.bind(("127.0.0.1", 0))
    listener_socket.listen(128)
    port = int(listener_socket.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            lifespan="on",
        )
    )
    import threading

    thread = threading.Thread(
        target=lambda: server.run(sockets=[listener_socket]),
        name="agent-bridge-live-codex-test-viewer",
        daemon=True,
    )
    thread.start()
    _wait_until(
        lambda: server.started or not thread.is_alive(),
        timeout=10,
        description="isolated Bridge startup",
    )
    if not server.started:
        server.should_exit = True
        thread.join(timeout=2)
        listener_socket.close()
        raise AssertionError("isolated Bridge did not start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener_socket.close()


@contextmanager
def _running_listener(
    *,
    base_url: str,
    queue_database: Path,
    cursor_file: Path,
    registration_secret: str,
    product: str,
    username: str,
    signature: str,
    conversation_id: str,
    log_prefix: Path,
) -> Iterator[subprocess.Popen[str]]:
    wake_command = [
        str(REPOSITORY_ROOT / "bin" / "agent-bridge-supervisor"),
        "enqueue",
        "--database",
        str(queue_database),
    ]
    command = [
        str(REPOSITORY_ROOT / "bin" / "agent-bridge-listen"),
        "--url",
        base_url,
        "--wake-command-json",
        json.dumps(wake_command, separators=(",", ":")),
        "--wake-policy",
        "mention",
        "--wake-timeout",
        "15",
        "--cursor-file",
        str(cursor_file),
        "--product",
        product,
        "--username",
        username,
        "--signature",
        signature,
        "--conversation",
        conversation_id,
        "--role",
        "reviewer",
        "--capability",
        "history",
    ]
    stdout_path = log_prefix.with_suffix(".stdout.log")
    stderr_path = log_prefix.with_suffix(".stderr.log")
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr:
        process = subprocess.Popen(
            command,
            cwd=REPOSITORY_ROOT,
            env=_isolated_environment(
                AGENT_BRIDGE_REGISTRATION_SECRET=registration_secret,
            ),
            stdout=stdout,
            stderr=stderr,
            text=True,
            start_new_session=True,
        )
        try:
            yield process
        finally:
            _terminate_process(process)
    if process.returncode not in {0, -signal.SIGTERM}:
        raise AssertionError(
            f"listener exited with {process.returncode}: {_log_tail(stderr_path)}"
        )


def _run_worker(
    *,
    base_url: str,
    queue_database: Path,
    thread_state_file: Path,
    registration_secret_file: Path,
    workspace: Path,
    product: str,
    username: str,
    signature: str,
    conversation_id: str,
    log_prefix: Path,
) -> float:
    command = [
        str(REPOSITORY_ROOT / "bin" / "agent-bridge-codex-worker"),
        "--database",
        str(queue_database),
        "--wake-policy",
        "mention",
        "--debounce",
        "0",
        "--poll-interval",
        "0.1",
        "--codex-binary",
        str(CODEX_BINARY),
        "--cwd",
        str(workspace),
        "--thread-state-file",
        str(thread_state_file),
        "--thread-name",
        f"Agent Bridge isolated smoke {username}",
        "--bridge-mcp-command",
        str(REPOSITORY_ROOT / "bin" / "agent-bridge-mcp"),
        "--bridge-url",
        base_url,
        "--product",
        product,
        "--username",
        username,
        "--signature",
        signature,
        "--conversation",
        conversation_id,
        "--role",
        "reviewer",
        "--capability",
        "history",
        "--once",
    ]
    stdout_path = log_prefix.with_suffix(".stdout.log")
    stderr_path = log_prefix.with_suffix(".stderr.log")
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=REPOSITORY_ROOT,
        env=_isolated_environment(
            AGENT_BRIDGE_REGISTRATION_SECRET_FILE=str(registration_secret_file),
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=PROCESS_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        _terminate_process(process)
        stdout, stderr = process.communicate()
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        raise AssertionError(
            "real Codex worker timed out; stderr tail: " + _log_tail(stderr_path)
        ) from exc
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    if process.returncode != 0:
        raise AssertionError(
            "real Codex worker failed with "
            f"{process.returncode}; stderr tail: {_log_tail(stderr_path)}; "
            f"stdout tail: {_log_tail(stdout_path)}"
        )
    return round(time.monotonic() - started, 3)


def _archive_test_thread(
    *,
    thread_state_file: Path,
    registration_secret_file: Path,
    workspace: Path,
    base_url: str,
    product: str,
    username: str,
    signature: str,
    conversation_id: str,
) -> tuple[bool, str | None]:
    previous_bridge_environment = {
        key: os.environ.get(key) for key in BRIDGE_ENVIRONMENT_KEYS
    }
    for key in BRIDGE_ENVIRONMENT_KEYS:
        os.environ.pop(key, None)
    os.environ["AGENT_BRIDGE_REGISTRATION_SECRET_FILE"] = str(
        registration_secret_file
    )
    host: CodexThreadHost | None = None
    try:
        host = CodexThreadHost(
            codex_binary=str(CODEX_BINARY),
            cwd=workspace,
            thread_state_file=thread_state_file,
            thread_name=f"Agent Bridge isolated smoke {username}",
            bridge_mcp_command=REPOSITORY_ROOT / "bin" / "agent-bridge-mcp",
            bridge_url=base_url,
            product=product,
            username=username,
            signature=signature,
            conversation=conversation_id,
            roles=("reviewer",),
            capabilities=("history",),
        )
        host.start()
        host.rpc.request(
            "thread/archive",
            {"threadId": host.thread_id},
            timeout=60,
        )
        return True, None
    except Exception as exc:  # cleanup must not hide the primary E2E result
        return False, str(exc)
    finally:
        if host is not None:
            host.close()
        for key in BRIDGE_ENVIRONMENT_KEYS:
            previous = previous_bridge_environment[key]
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous


def test_real_codex_listener_worker_reconnect_identity_and_room_isolation(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    if not _live_tests_enabled():
        pytest.skip(
            "set AGENT_BRIDGE_RUN_LIVE_CODEX_TESTS=1 to run the real Codex smoke"
        )
    if not CODEX_BINARY.exists():
        pytest.skip(f"real Codex binary is unavailable at {CODEX_BINARY}")

    suffix = uuid.uuid4().hex[:8]
    database = tmp_path / "bridge.db"
    queue_database = tmp_path / "wake-queue.db"
    cursor_file = tmp_path / "listener.cursor"
    thread_state_file = tmp_path / "codex-thread-id"
    registration_secret_file = tmp_path / "registration-secret"
    workspace = tmp_path / "workspace"
    product = "codex"
    username = f"isolated-smoke-{suffix}"
    client_type = f"{product}-{username}"
    signature = "isolated real Codex smoke reviewer"
    room = f"isolated-codex-{suffix}"
    other_room = f"isolated-control-{suffix}"
    registration_secret = f"registration-{uuid.uuid4().hex}"
    request.addfinalizer(lambda: registration_secret_file.unlink(missing_ok=True))
    registration_secret_file.write_text(registration_secret + "\n", encoding="utf-8")
    registration_secret_file.chmod(0o600)
    workspace.mkdir()
    (workspace / "README.txt").write_text(
        "This temporary directory belongs only to the Agent Bridge live smoke test.\n",
        encoding="utf-8",
    )

    store = BridgeStore(database)
    store.create_user_room(room)
    store.create_user_room(other_room)
    worker_durations: list[float] = []
    test_thread_archived = False
    archive_error: str | None = None

    with _running_bridge(database, registration_secret) as base_url:
        try:
            for round_number in (1, 2):
                with _running_listener(
                    base_url=base_url,
                    queue_database=queue_database,
                    cursor_file=cursor_file,
                    registration_secret=registration_secret,
                    product=product,
                    username=username,
                    signature=signature,
                    conversation_id=room,
                    log_prefix=tmp_path / f"listener-{round_number}",
                ) as listener:
                    participant = _wait_until(
                        lambda: (
                            rows[0]
                            if (
                                rows := _database_rows(
                                    database,
                                    "SELECT participant_id, client_type FROM participants "
                                    "WHERE client_type = ?",
                                    (client_type,),
                                )
                            )
                            else None
                        ),
                        timeout=20,
                        description=f"round {round_number} listener registration",
                    )
                    assert listener.poll() is None
                    participant_id = str(participant["participant_id"])

                    if round_number == 2:
                        previous_owner_rows = _database_rows(
                            database,
                            "SELECT MAX(created_at) AS created_at FROM messages "
                            "WHERE conversation_id = ? "
                            "AND sender_participant_id = 'participant_web_owner'",
                            (room,),
                        )
                        previous_created_at = float(
                            previous_owner_rows[0]["created_at"] or 0.0
                        )
                        remaining = (
                            previous_created_at
                            + MESSAGE_COOLDOWN_SECONDS
                            + 0.15
                            - time.time()
                        )
                        if remaining > 0:
                            time.sleep(remaining)

                    body = (
                        f"第一轮真实链路检查，请引用回复确认，@{client_type}"
                        if round_number == 1
                        else f"第二轮请在 @{client_type} 收到后引用回复确认重连正常。"
                    )
                    message = store.send_owner_message(
                        conversation_id=room,
                        body_text=body,
                    )
                    assert message["mentions"] == [participant_id]
                    assert message["notification_mode"] == "mention"
                    message_id = str(message["message_id"])

                    _wait_until(
                        lambda: queue_status(queue_database)["counts"]["pending"] > 0,
                        timeout=20,
                        description=f"round {round_number} durable wake enqueue",
                    )

                    worker_durations.append(
                        _run_worker(
                            base_url=base_url,
                            queue_database=queue_database,
                            thread_state_file=thread_state_file,
                            registration_secret_file=registration_secret_file,
                            workspace=workspace,
                            product=product,
                            username=username,
                            signature=signature,
                            conversation_id=room,
                            log_prefix=tmp_path / f"worker-{round_number}",
                        )
                    )

                    replies = _database_rows(
                        database,
                        "SELECT message_id, sender_participant_id, body, reply_to "
                        "FROM messages WHERE conversation_id = ? "
                        "AND sender_participant_id = ? AND reply_to = ?",
                        (room, participant_id, message_id),
                    )
                    assert len(replies) == 1
                    assert str(replies[0]["body"]).strip()
                    delivery = _database_rows(
                        database,
                        "SELECT state, reasons_json, acked_at FROM message_deliveries "
                        "WHERE message_id = ? AND participant_id = ?",
                        (message_id, participant_id),
                    )
                    assert len(delivery) == 1
                    assert delivery[0]["state"] == "acked"
                    assert delivery[0]["acked_at"] is not None
                    assert "mention" in json.loads(str(delivery[0]["reasons_json"]))

                    active_counts = queue_status(queue_database)["counts"]
                    assert active_counts["pending"] == 0
                    assert active_counts["inflight"] == 0
                    assert active_counts["deferred"] == 0

                if round_number == 1:
                    first_thread_id = thread_state_file.read_text(
                        encoding="utf-8"
                    ).strip()
                    assert first_thread_id
                else:
                    assert (
                        thread_state_file.read_text(encoding="utf-8").strip()
                        == first_thread_id
                    )

            identities = _database_rows(
                database,
                "SELECT participant_id, client_type FROM participants "
                "WHERE client_type = ?",
                (client_type,),
            )
            assert identities == [
                {"participant_id": participant_id, "client_type": client_type}
            ]
            memberships = _database_rows(
                database,
                "SELECT conversation_id FROM memberships "
                "WHERE participant_id = ? AND active = 1 ORDER BY conversation_id",
                (participant_id,),
            )
            assert memberships == [{"conversation_id": room}]
            assert not _database_rows(
                database,
                "SELECT message_id FROM messages WHERE conversation_id = ?",
                (other_room,),
            )
            assert not _database_rows(
                database,
                "SELECT message_id FROM message_deliveries "
                "WHERE participant_id = ? AND message_id IN "
                "(SELECT message_id FROM messages WHERE conversation_id = ?)",
                (participant_id, other_room),
            )
            assert queue_status(queue_database)["counts"]["handled"] == 2
        finally:
            if thread_state_file.exists():
                test_thread_archived, archive_error = _archive_test_thread(
                    thread_state_file=thread_state_file,
                    registration_secret_file=registration_secret_file,
                    workspace=workspace,
                    base_url=base_url,
                    product=product,
                    username=username,
                    signature=signature,
                    conversation_id=room,
                )

    assert test_thread_archived, f"test Codex thread was not archived: {archive_error}"
    print(
        "live-codex-smoke="
        + json.dumps(
            {
                "archived": test_thread_archived,
                "identity_count": 1,
                "queue_handled": 2,
                "room_isolation": True,
                "same_thread_after_restart": True,
                "worker_durations_seconds": worker_durations,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
