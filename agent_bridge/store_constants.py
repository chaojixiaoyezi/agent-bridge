"""Shared durable-store protocol constants.

These names remain re-exported by :mod:`agent_bridge.store` for compatibility.
"""

from __future__ import annotations

import re


AUDIENCE_KINDS = {"participant", "room", "role", "broadcast"}


PRESENCE_STATES = {"online", "offline"}


MESSAGE_ACTIONS = {"claim", "ack", "release"}


DELIVERY_STATES = {"pending", "delivered", "acked", "cancelled"}


MESSAGE_COOLDOWN_SECONDS = 15.0


WEB_USER_MESSAGE_COOLDOWN_SECONDS = 60.0


MAX_MESSAGE_COOLDOWN_SECONDS = 24 * 60 * 60


RATE_LIMIT_ACTOR_KINDS = {"agent", "web_user"}


AGENT_ACTIVE_ROOM_LIMIT = 2


ROOM_ABANDON_AFTER_SECONDS = 90 * 24 * 60 * 60


DEFAULT_SESSION_TTL_SECONDS = 2 * 60 * 60


CONNECTOR_SESSION_IDLE_RETIRE_SECONDS = 15 * 60


CONNECTOR_SESSION_MIN_RETAIN = 6


NICKNAME_REQUEST_COOLDOWN_SECONDS = 24 * 60 * 60


MAX_MENTIONS_PER_MESSAGE = 64


MAX_WAIT_MESSAGES_PAGE_SIZE = 20


DEFAULT_OFFLINE_BACKLOG_KEEP_MESSAGES = 20


MAX_OFFLINE_BACKLOG_KEEP_MESSAGES = 100


MAX_HISTORY_SEARCH_TERMS = 8


MAX_HISTORY_SEARCH_QUERY_LENGTH = 256


MAX_TASK_TARGETS = 64


RUNTIME_HEARTBEAT_INTERVAL_SECONDS = 10.0


RUNTIME_LEASE_TTL_SECONDS = 30.0


RUNTIME_INSTANCE_ACTIVE_SECONDS = 45.0


RUNTIME_INSTANCE_RETENTION_SECONDS = 7 * 24 * 60 * 60


NATIVE_SESSION_LEASE_SECONDS = 90.0


NATIVE_CHANNEL_MAX_WAIT_SECONDS = 60.0


NATIVE_CHANNEL_MAX_MESSAGES = 20


TASK_CLAIM_LEASE_SECONDS = 10 * 60.0


TASK_INPUT_REDELIVERY_SECONDS = 30.0


TASK_STATUSES = {
    "queued",
    "claimed",
    "running",
    "needs_input",
    "completed",
    "failed",
    "cancelled",
}


DEFAULT_INVITATION_TTL_SECONDS = 30 * 60


MAX_INVITATION_TTL_SECONDS = 24 * 60 * 60


CONNECTOR_ONLINE_WINDOW_SECONDS = 75.0


ENROLLMENT_PREVIOUS_GRACE_SECONDS = 24 * 60 * 60


DEFAULT_AGENT_INACTIVITY_DAYS = 10


DEFAULT_UNACTIVATED_AGENT_INACTIVITY_DAYS = 3


MIN_AGENT_INACTIVITY_DAYS = 1


MAX_AGENT_INACTIVITY_DAYS = 3650


INVITATION_MODES = {"basic", "resident"}


INVITATION_ADAPTERS = {"codex", "claude-code", "manual"}


NATIVE_TUI_ADAPTERS = {
    "codex",
    "deepseek-harness",
    "opencode",
    "hermes",
    "pi",
    "qwen-code",
}


TUI_STATES = {
    "unbound",
    "awaiting_confirmation",
    "online",
    "busy",
    "waiting_approval",
    "offline",
    "error",
}


INVITATION_STATUSES = {"active", "exhausted", "revoked", "expired"}


CONNECTOR_SETUP_STATUSES = {
    "awaiting_setup",
    "configured",
    "manual",
    "failed",
    "revoked",
}


CONNECTOR_COMPONENTS = {"listener", "chat", "task", "mcp"}


SESSION_COMPONENTS = CONNECTOR_COMPONENTS | {"a2a", "unknown"}


MESSAGE_SENDER_SEATS = {"main", "shadow", "executor", "web", "a2a", "unknown"}


ROOM_WAKE_MODES = {"mention", "digest", "all"}


MESSAGE_NOTIFICATION_MODES = {"ordinary", "mention"}


ROOM_MESSAGE_MARKER_KINDS = {"pin", "decision"}


DEFAULT_ROOM_WAKE_MODE = "digest"


DEFAULT_ROOM_DIGEST_MIN_MESSAGES = 10


DEFAULT_ROOM_DIGEST_AFTER_SECONDS = 2 * 60 * 60


CHAT_AUTHORIZATION_FROZEN = True


OWNER_PARTICIPANT_ID = "participant_web_owner"


OWNER_AUTHORIZATION_ID = "owner_web_ui"


OWNER_CLIENT_TYPE = "web-user"


OWNER_SESSION_ALIAS = "本机用户"


_REVIEW_TERMS = (
    "确认",
    "审核",
    "审查",
    "复核",
    "验收",
    "批准",
    "审批",
    "过目",
)


_REVIEW_TERM_PATTERN = "|".join(re.escape(term) for term in _REVIEW_TERMS)


_DIRECT_REVIEW_REQUEST_PATTERNS = (
    re.compile(
        rf"(?:请|麻烦|烦请|劳烦|能否|可否)"
        rf"[^。！？\n]{{0,64}}(?:{_REVIEW_TERM_PATTERN})",
        flags=re.IGNORECASE,
    ),
    re.compile(
        rf"(?:{_REVIEW_TERM_PATTERN})[^。！？\n]{{0,12}}"
        r"(?:一下|下吧|一下吧|好吗|可以吗|行吗|\?|？)",
        flags=re.IGNORECASE,
    ),
    re.compile(
        rf"需要(?:你|您)[^。！？\n]{{0,32}}(?:{_REVIEW_TERM_PATTERN})",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"(?:please|could\s+you|can\s+you|would\s+you|need\s+you\s+to)"
        r"[^.!?\n]{0,80}"
        r"(?:review|confirm|approve|verify|sign[ -]?off)",
        flags=re.IGNORECASE,
    ),
)


_AGENT_ASSIGNMENT_ACTION_PATTERN = (
    r"(?:负责|处理|执行|完成|实现|修改|开发|核对|检查|分析|调研|测试|验证|"
    r"接手|跟进|给出|回复|答复|排查|修复|审计|评审)"
)


_DIRECT_AGENT_REPLY_REQUEST_PATTERNS = (
    re.compile(
        rf"(?:请|麻烦|烦请|劳烦|需要(?:你|您)|由(?:你|您)|交给(?:你|您)|"
        rf"安排(?:你|您))[^。！？\n]{{0,64}}{_AGENT_ASSIGNMENT_ACTION_PATTERN}",
        flags=re.IGNORECASE,
    ),
    re.compile(
        rf"@[^\s@，,。.!！?？:：;；]{{1,128}}\s*[：:,，]\s*"
        rf"(?:请|麻烦|烦请|劳烦|你来|由你|{_AGENT_ASSIGNMENT_ACTION_PATTERN})",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"@[^\s@，,。.!！?？:：;；]{1,128}[^。！？\n]{0,64}"
        r"(?:请问|能否|可否|是否|怎么看|你觉得|你认为|有没有|为什么|为何|怎么)",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"@[A-Za-z0-9._:-]{1,128}\s*[:,]?\s*"
        r"(?:please|could\s+you|can\s+you|would\s+you|own|take|handle|"
        r"implement|fix|review|verify|test|investigate|reply)",
        flags=re.IGNORECASE,
    ),
)


_ACKNOWLEDGEMENT_ONLY_PATTERN = re.compile(
    r"^(?:收到|明白|好的|好|知悉|已知悉|记下了|了解|同意|认可|"
    r"复核口径一致|口径一致|已阅|ok|okay|got\s+it|acknowledged)"
    r"(?:[，,。.!！\s]*(?:谢谢|感谢|后续按此执行|按此执行|会跟进))*[。.!！\s]*$",
    flags=re.IGNORECASE,
)
