from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

from .codex_rpc import CodexRpcError as CodexRpcError
from .codex_rpc import JsonRpcProcess as JsonRpcProcess
from .codex_thread import CodexThreadHost as CodexThreadHost
from .codex_worker_contracts import (
    BRIDGE_MCP_TOOLS as BRIDGE_MCP_TOOLS,
    SENSITIVE_CHILD_ENV as SENSITIVE_CHILD_ENV,
    THREAD_ID_PATTERN as THREAD_ID_PATTERN,
    CodexWorkerError,
    TurnEvidence,
    _required_reply_count,
)
from .supervisor import (
    SupervisorError,
    _batch_envelope,
    attach_adapter_run,
    claim_batch,
    finish_adapter_run,
    recover_inflight,
)


def _split_env_tokens(name: str) -> tuple[str, ...]:
    return tuple(
        item.strip() for item in os.environ.get(name, "").split(",") if item.strip()
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-bridge-codex-worker",
        description=(
            "Run one persistent Codex app-server and route durable Agent Bridge "
            "wake batches into a dedicated, serial Agent task."
        ),
    )
    parser.add_argument("--database", required=True)
    parser.add_argument(
        "--wake-policy",
        choices=("all", "important", "mention"),
        default=os.environ.get("AGENT_BRIDGE_AGENT_WAKE_POLICY", "mention"),
    )
    parser.add_argument(
        "--debounce",
        type=float,
        default=float(os.environ.get("AGENT_BRIDGE_AGENT_WAKE_DEBOUNCE", "3")),
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=float(os.environ.get("AGENT_BRIDGE_AGENT_WAKE_POLL", "0.5")),
    )
    parser.add_argument(
        "--codex-binary",
        default=os.environ.get("AGENT_BRIDGE_CODEX_BINARY", "codex"),
    )
    parser.add_argument(
        "--cwd",
        default=os.environ.get("AGENT_BRIDGE_CODEX_CWD", os.getcwd()),
    )
    parser.add_argument(
        "--thread-state-file",
        default=os.environ.get("AGENT_BRIDGE_CODEX_THREAD_STATE_FILE"),
        required=os.environ.get("AGENT_BRIDGE_CODEX_THREAD_STATE_FILE") is None,
    )
    parser.add_argument(
        "--thread-name",
        default=os.environ.get(
            "AGENT_BRIDGE_CODEX_THREAD_NAME",
            "Agent Bridge 聊天室值守",
        ),
    )
    parser.add_argument(
        "--bridge-mcp-command",
        default=os.environ.get("AGENT_BRIDGE_MCP_COMMAND"),
        required=os.environ.get("AGENT_BRIDGE_MCP_COMMAND") is None,
    )
    parser.add_argument(
        "--bridge-url",
        default=os.environ.get("AGENT_BRIDGE_URL", "http://127.0.0.1:8765"),
    )
    parser.add_argument(
        "--product",
        default=os.environ.get("AGENT_BRIDGE_PRODUCT", "codex"),
    )
    parser.add_argument("--username", default=os.environ.get("AGENT_BRIDGE_USERNAME"))
    parser.add_argument("--signature", default=os.environ.get("AGENT_BRIDGE_SIGNATURE"))
    parser.add_argument(
        "--conversation",
        default=os.environ.get("AGENT_BRIDGE_CONVERSATION_ID"),
    )
    parser.add_argument("--role", action="append", default=None)
    parser.add_argument("--capability", action="append", default=None)
    parser.add_argument("--once", action="store_true", help=argparse.SUPPRESS)
    return parser


def _validated_required(value: str | None, name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise CodexWorkerError(f"{name} is required")
    return normalized


def _finish_turn(
    database: Path,
    *,
    host: CodexThreadHost,
    run_id: str,
    status: str,
    error: str | None,
    evidence: TurnEvidence,
    batch_required_reply: bool,
) -> tuple[bool, str | None]:
    required_tools = {"agent_wait"}
    if (
        evidence.required_reply_count_observed is not None
        and len(evidence.mention_message_ids)
        < evidence.required_reply_count_observed
    ):
        required_tools.add("all-personal-mentions-from-agent_wait-pages")
    if batch_required_reply and evidence.required_reply_count_observed is None:
        if not evidence.mention_message_ids:
            required_tools.add("mention-delivery-from-agent_wait")
    if evidence.mention_message_ids.difference(evidence.replied_message_ids):
        required_tools.add("agent_reply-to-every-mentioned-message")
    missing_tools = sorted(
        tool
        for tool in required_tools
        if tool not in evidence.completed_bridge_tools
    )
    evidence_error = None
    if missing_tools:
        evidence_error = (
            "Codex turn completed without required Agent Bridge tool evidence: "
            + ", ".join(missing_tools)
        )
        if evidence.failed_bridge_tools:
            evidence_error += "; failures: " + "; ".join(
                evidence.failed_bridge_tools[-3:]
            )
    successful = status == "completed" and not missing_tools
    if successful:
        try:
            host.acknowledge_optional_messages(evidence)
        except Exception as exc:
            successful = False
            evidence_error = (
                "Codex turn completed but deterministic optional-message "
                f"ack failed: {exc}"
            )
    completion_error = (
        None
        if successful
        else error
        or evidence_error
        or f"Codex turn ended with status {status}"
    )
    finish_adapter_run(
        database,
        adapter_run_id=run_id,
        successful=successful,
        error=completion_error,
    )
    return successful, completion_error


def _host_from_args(args: argparse.Namespace) -> CodexThreadHost:
    roles = tuple(args.role) if args.role is not None else _split_env_tokens(
        "AGENT_BRIDGE_ROLES"
    )
    capabilities = (
        tuple(args.capability)
        if args.capability is not None
        else _split_env_tokens("AGENT_BRIDGE_CAPABILITIES")
    )
    return CodexThreadHost(
        codex_binary=args.codex_binary,
        cwd=Path(args.cwd),
        thread_state_file=Path(args.thread_state_file),
        thread_name=args.thread_name,
        bridge_mcp_command=Path(args.bridge_mcp_command),
        bridge_url=_validated_required(args.bridge_url, "bridge URL"),
        product=_validated_required(args.product, "product"),
        username=_validated_required(args.username, "username"),
        signature=_validated_required(args.signature, "signature"),
        conversation=_validated_required(args.conversation, "conversation"),
        roles=roles,
        capabilities=capabilities,
    )


def run_session(args: argparse.Namespace) -> None:
    database = Path(args.database).expanduser()
    claim_owner = f"codex-worker:{os.getpid()}:{uuid.uuid4().hex}"
    recover_inflight(
        database,
        reason="recovered after resident Codex worker restart",
    )
    host: CodexThreadHost | None = None
    submitted_batches = 0
    mention_required_by_run: dict[str, bool] = {}
    delay = max(0.1, min(float(args.poll_interval), 30.0))
    try:
        while True:
            if host is not None:
                while True:
                    completion = host.poll_turn_completion()
                    if completion is None:
                        break
                    run_id, status, error, evidence = completion
                    batch_required_reply = mention_required_by_run.pop(run_id, False)
                    successful, completion_error = _finish_turn(
                        database,
                        host=host,
                        run_id=run_id,
                        status=status,
                        error=error,
                        evidence=evidence,
                        batch_required_reply=batch_required_reply,
                    )
                    if args.once and submitted_batches > 0:
                        if not successful:
                            raise CodexWorkerError(
                                completion_error
                                or f"Codex turn ended with status {status}"
                            )
                        return
                if not host.rpc.is_alive():
                    raise CodexWorkerError("Codex app-server exited unexpectedly")

            rows = claim_batch(
                database,
                wake_policy=args.wake_policy,
                debounce=args.debounce,
                claim_owner=claim_owner,
            )
            if rows:
                if host is None:
                    host = _host_from_args(args)
                    host.start()
                batch = json.loads(_batch_envelope(rows).decode("utf-8"))
                if bool(batch.get("contains_backlog_event")):
                    batch["offline_compaction"] = host.compact_offline_backlog()
                run_id = host.submit(batch)
                mention_required_by_run[run_id] = (
                    mention_required_by_run.get(run_id, False)
                    or _required_reply_count(batch) > 0
                )
                attach_adapter_run(
                    database,
                    idempotency_keys=[str(row["idempotency_key"]) for row in rows],
                    claim_owner=claim_owner,
                    adapter_run_id=run_id,
                )
                submitted_batches += 1
                continue
            if args.once and submitted_batches == 0:
                return
            time.sleep(delay)
    finally:
        if host is not None:
            host.close()


def run_forever(args: argparse.Namespace) -> None:
    while True:
        try:
            run_session(args)
            return
        except KeyboardInterrupt:
            return
        except (CodexWorkerError, SupervisorError, OSError, ValueError) as exc:
            recover_inflight(
                Path(args.database).expanduser(),
                reason=str(exc),
            )
            print(f"agent-bridge-codex-worker: {exc}", file=sys.stderr)
            if args.once:
                raise
            time.sleep(2)


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        run_forever(args)
    except (CodexWorkerError, SupervisorError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
