from __future__ import annotations

from pathlib import Path

import agent_bridge.task_worker as task_worker
from agent_bridge.http_client import BridgeRemoteError
from agent_bridge.task_worker import (
    CodexTaskHost,
    _task_poll_retry_delay,
    _task_prompt,
)


THREAD = "019f0000-0000-7000-8000-000000000001"
FORKED = "019f0000-0000-7000-8000-000000000002"


def test_codex_task_host_forks_inviting_tui_without_overriding_permissions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    requests: list[tuple[str, dict]] = []

    class FakeRpc:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def start(self) -> None:
            pass

        def request(self, method: str, params: dict, **_kwargs):
            requests.append((method, params))
            if method == "thread/fork":
                return {"thread": {"id": FORKED}}
            return {}

        def close(self) -> None:
            pass

    monkeypatch.setattr(task_worker, "JsonRpcProcess", FakeRpc)
    monkeypatch.setattr(task_worker.shutil, "which", lambda _name: "/usr/bin/true")
    state_file = tmp_path / "task-thread"
    host = CodexTaskHost(state_file=state_file, source_thread_id=THREAD)
    host.start(
        binary="codex",
        cwd=tmp_path,
        mcp_arguments=[],
        environment={},
    )

    fork = next(params for method, params in requests if method == "thread/fork")
    assert fork["threadId"] == THREAD
    assert fork["cwd"] == str(tmp_path)
    assert "sandbox" not in fork
    assert "approvalPolicy" not in fork
    assert state_file.read_text(encoding="utf-8").strip() == FORKED


def test_task_prompt_treats_cwd_as_starting_point_and_local_permissions_as_limit(
    tmp_path: Path,
) -> None:
    prompt = _task_prompt(
        {
            "task_id": "task_123",
            "body": "审计另一个目录，需要时分工。",
            "target_participant_ids": ["participant_a"],
        },
        conversation="任务群",
        cwd=tmp_path,
    )

    assert "不是普通聊天" in prompt
    assert "当前目录只是起点" in prompt
    assert "本机权限" in prompt
    assert "agent_task_delegate" in prompt
    assert "完成和失败终态" in prompt
    assert str(tmp_path) in prompt


def test_task_poll_retries_rolling_upgrade_without_masking_permanent_errors() -> None:
    assert _task_poll_retry_delay(
        BridgeRemoteError("old viewer", status_code=404), 0
    ) == 1.0
    assert _task_poll_retry_delay(
        BridgeRemoteError("offline", status_code=None), 8
    ) == 16.0
    assert _task_poll_retry_delay(
        BridgeRemoteError(
            "busy",
            status_code=429,
            retry_after_seconds=120,
        ),
        2,
    ) == 30.0
    assert (
        _task_poll_retry_delay(
            BridgeRemoteError("forbidden", status_code=403),
            0,
        )
        is None
    )
