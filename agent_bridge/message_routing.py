"""Mention normalization, recipient routing, and durable delivery creation."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Sequence
from typing import Any

from .store_constants import (
    CHAT_AUTHORIZATION_FROZEN,
    MAX_MENTIONS_PER_MESSAGE,
    MESSAGE_COOLDOWN_SECONDS,
    OWNER_AUTHORIZATION_ID,
    OWNER_PARTICIPANT_ID,
    WEB_USER_MESSAGE_COOLDOWN_SECONDS,
    _ACKNOWLEDGEMENT_ONLY_PATTERN,
    _DIRECT_AGENT_REPLY_REQUEST_PATTERNS,
    _DIRECT_REVIEW_REQUEST_PATTERNS,
)
from .store_errors import ConflictError
from .validation import (
    ValidationError,
    compact_json,
    conversation_id as validate_conversation_id,
    opaque_id,
    token,
)


DELIVERY_SCHEMA = """
CREATE TABLE IF NOT EXISTS follows (
    conversation_id TEXT NOT NULL,
    follower_participant_id TEXT NOT NULL,
    followed_participant_id TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (
        conversation_id,
        follower_participant_id,
        followed_participant_id
    ),
    CHECK (follower_participant_id != followed_participant_id),
    FOREIGN KEY (conversation_id) REFERENCES rooms(conversation_id),
    FOREIGN KEY (follower_participant_id)
        REFERENCES participants(participant_id),
    FOREIGN KEY (followed_participant_id)
        REFERENCES participants(participant_id)
);

CREATE TABLE IF NOT EXISTS message_deliveries (
    message_id TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'delivered', 'acked', 'cancelled')),
    reasons_json TEXT NOT NULL DEFAULT '[]',
    priority TEXT NOT NULL DEFAULT 'normal'
        CHECK (priority IN ('normal', 'important', 'direct')),
    actionable INTEGER NOT NULL DEFAULT 0 CHECK (actionable IN (0, 1)),
    created_at REAL NOT NULL,
    first_delivered_at REAL,
    last_delivered_at REAL,
    acked_at REAL,
    delivery_stage TEXT NOT NULL DEFAULT 'queued'
        CHECK (delivery_stage IN (
            'queued', 'legacy_delivered', 'native_injected',
            'native_applied', 'replied', 'legacy_acked', 'cancelled'
        )),
    native_session_id TEXT,
    native_event_id TEXT,
    native_injected_at REAL,
    native_applied_at REAL,
    native_replied_at REAL,
    shadow_seen_at REAL,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    PRIMARY KEY (message_id, participant_id),
    FOREIGN KEY (message_id) REFERENCES messages(message_id),
    FOREIGN KEY (participant_id) REFERENCES participants(participant_id)
);

CREATE INDEX IF NOT EXISTS idx_follows_follower_active
    ON follows(follower_participant_id, active, conversation_id);
CREATE INDEX IF NOT EXISTS idx_follows_followed_active
    ON follows(followed_participant_id, active, conversation_id);
CREATE INDEX IF NOT EXISTS idx_message_deliveries_participant_state
    ON message_deliveries(participant_id, state, created_at, message_id);
CREATE INDEX IF NOT EXISTS idx_message_deliveries_message_state
    ON message_deliveries(message_id, state, participant_id);

DROP TRIGGER IF EXISTS trg_follows_require_memberships_insert;
CREATE TRIGGER trg_follows_require_memberships_insert
BEFORE INSERT ON follows
WHEN NEW.active = 1 AND (
    NOT EXISTS (
        SELECT 1 FROM memberships
        WHERE conversation_id = NEW.conversation_id
          AND participant_id = NEW.follower_participant_id
          AND active = 1
    )
    OR NOT EXISTS (
        SELECT 1 FROM memberships
        WHERE conversation_id = NEW.conversation_id
          AND participant_id = NEW.followed_participant_id
          AND active = 1
    )
)
BEGIN
    SELECT RAISE(ABORT, 'FOLLOW_REQUIRES_ACTIVE_MEMBERSHIPS');
END;

DROP TRIGGER IF EXISTS trg_follows_require_memberships_update;
CREATE TRIGGER trg_follows_require_memberships_update
BEFORE UPDATE OF active, conversation_id,
                 follower_participant_id, followed_participant_id ON follows
WHEN NEW.active = 1 AND (
    NOT EXISTS (
        SELECT 1 FROM memberships
        WHERE conversation_id = NEW.conversation_id
          AND participant_id = NEW.follower_participant_id
          AND active = 1
    )
    OR NOT EXISTS (
        SELECT 1 FROM memberships
        WHERE conversation_id = NEW.conversation_id
          AND participant_id = NEW.followed_participant_id
          AND active = 1
    )
)
BEGIN
    SELECT RAISE(ABORT, 'FOLLOW_REQUIRES_ACTIVE_MEMBERSHIPS');
END;

DROP TRIGGER IF EXISTS trg_memberships_disable_follows_after_leave;
CREATE TRIGGER trg_memberships_disable_follows_after_leave
AFTER UPDATE OF active ON memberships
WHEN NEW.active = 0 AND OLD.active = 1
BEGIN
    UPDATE follows
    SET active = 0, updated_at = CAST(strftime('%s', 'now') AS REAL)
    WHERE conversation_id = NEW.conversation_id
      AND (
          follower_participant_id = NEW.participant_id
          OR followed_participant_id = NEW.participant_id
      );
END;
"""


AUTHORIZATION_SCHEMA = f"""
CREATE INDEX IF NOT EXISTS idx_messages_authorized_session
    ON messages(authorized_session_id, sequence);
CREATE INDEX IF NOT EXISTS idx_messages_forwarded_from
    ON messages(forwarded_from_message_id, sequence);

DROP TRIGGER IF EXISTS trg_messages_require_live_mcp_session;
DROP TRIGGER IF EXISTS trg_messages_require_authorized_sender;
DROP TRIGGER IF EXISTS trg_messages_sender_cooldown;

CREATE TRIGGER trg_messages_sender_cooldown
BEFORE INSERT ON messages
WHEN NOT EXISTS (
    SELECT 1
    FROM web_sessions AS web_session
    JOIN web_users AS web_user ON web_user.user_id = web_session.user_id
    WHERE web_session.session_id = NEW.authorized_session_id
      AND web_user.participant_id = NEW.sender_participant_id
      AND web_user.role = 'admin'
      AND web_user.active = 1
      AND web_session.revoked_at IS NULL
      AND web_session.expires_at > CAST(strftime('%s', 'now') AS REAL)
)
AND EXISTS (
    SELECT 1 FROM messages AS previous
    WHERE previous.conversation_id = NEW.conversation_id
      AND previous.sender_participant_id = NEW.sender_participant_id
      AND previous.created_at > NEW.created_at - CASE
          WHEN NEW.authorized_session_id = '{OWNER_AUTHORIZATION_ID}'
               AND NEW.sender_participant_id = '{OWNER_PARTICIPANT_ID}'
          THEN {MESSAGE_COOLDOWN_SECONDS}
          WHEN EXISTS (
              SELECT 1
              FROM web_sessions AS web_session
              JOIN web_users AS web_user
                ON web_user.user_id = web_session.user_id
              WHERE web_session.session_id = NEW.authorized_session_id
                AND web_user.participant_id = NEW.sender_participant_id
                AND web_user.role = 'user'
                AND web_user.active = 1
                AND web_session.revoked_at IS NULL
                AND web_session.expires_at > CAST(strftime('%s', 'now') AS REAL)
          ) THEN MIN(
              COALESCE(
                  (SELECT cooldown_seconds FROM message_rate_defaults
                   WHERE actor_kind = 'web_user'),
                  {WEB_USER_MESSAGE_COOLDOWN_SECONDS}
              ),
              COALESCE(
                  (SELECT cooldown_seconds FROM message_rate_overrides
                   WHERE participant_id = NEW.sender_participant_id),
                  COALESCE(
                      (SELECT cooldown_seconds FROM message_rate_defaults
                       WHERE actor_kind = 'web_user'),
                      {WEB_USER_MESSAGE_COOLDOWN_SECONDS}
                  )
              )
          )
          ELSE MIN(
              COALESCE(
                  (SELECT cooldown_seconds FROM message_rate_defaults
                   WHERE actor_kind = 'agent'),
                  {MESSAGE_COOLDOWN_SECONDS}
              ),
              COALESCE(
                  (SELECT cooldown_seconds FROM message_rate_overrides
                   WHERE participant_id = NEW.sender_participant_id),
                  COALESCE(
                      (SELECT cooldown_seconds FROM message_rate_defaults
                       WHERE actor_kind = 'agent'),
                      {MESSAGE_COOLDOWN_SECONDS}
                  )
              )
          )
      END
)
BEGIN
    SELECT RAISE(ABORT, 'MESSAGE_RATE_LIMITED');
END;

CREATE TRIGGER trg_messages_require_authorized_sender
BEFORE INSERT ON messages
WHEN NOT (
    (
        NEW.authorized_session_id = '{OWNER_AUTHORIZATION_ID}'
        AND NEW.sender_participant_id = '{OWNER_PARTICIPANT_ID}'
    )
    OR EXISTS (
        SELECT 1
        FROM web_sessions AS web_session
        JOIN web_users AS web_user ON web_user.user_id = web_session.user_id
        WHERE web_session.session_id = NEW.authorized_session_id
          AND web_user.participant_id = NEW.sender_participant_id
          AND web_user.active = 1
          AND web_session.revoked_at IS NULL
          AND web_session.expires_at > CAST(strftime('%s', 'now') AS REAL)
    )
    OR EXISTS (
        SELECT 1
        FROM agent_sessions AS session
        WHERE session.session_id = NEW.authorized_session_id
          AND session.participant_id = NEW.sender_participant_id
          AND session.registered_conversation_id = NEW.conversation_id
          AND session.transport = 'mcp'
          AND session.cleared_at IS NULL
          AND session.revoked_at IS NULL
          AND session.expires_at > CAST(strftime('%s', 'now') AS REAL)
    )
)
BEGIN
    SELECT RAISE(ABORT, 'AUTHORIZED_SENDER_REQUIRED');
END;

DROP TRIGGER IF EXISTS trg_messages_forward_requires_source;
CREATE TRIGGER trg_messages_forward_requires_source
BEFORE INSERT ON messages
WHEN (
    NEW.message_kind = 'forward'
    AND (
        NEW.forwarded_from_message_id IS NULL
        OR NOT EXISTS (
            SELECT 1 FROM messages AS source
            WHERE source.message_id = NEW.forwarded_from_message_id
              AND source.conversation_id != NEW.conversation_id
        )
    )
) OR (
    NEW.message_kind != 'forward'
    AND NEW.forwarded_from_message_id IS NOT NULL
)
BEGIN
    SELECT RAISE(ABORT, 'INVALID_CROSS_ROOM_FORWARD');
END;

DROP TRIGGER IF EXISTS trg_messages_route_immutable;
CREATE TRIGGER trg_messages_route_immutable
BEFORE UPDATE OF conversation_id, sender_participant_id,
                 authorized_session_id, message_kind,
                 forwarded_from_message_id ON messages
WHEN NEW.sender_participant_id IS NOT OLD.sender_participant_id
  OR NEW.authorized_session_id IS NOT OLD.authorized_session_id
  OR (
      NEW.message_kind IS NOT OLD.message_kind
      AND NOT (
          OLD.message_kind = 'message'
          AND NEW.message_kind = 'task'
          AND EXISTS (
              SELECT 1 FROM room_tasks AS task
              WHERE task.source_message_id = OLD.message_id
          )
      )
  )
  OR NEW.forwarded_from_message_id IS NOT OLD.forwarded_from_message_id
  OR (
      NEW.conversation_id IS NOT OLD.conversation_id
      AND NOT (
          NOT EXISTS (
              SELECT 1 FROM rooms
              WHERE conversation_id = OLD.conversation_id
          )
          AND EXISTS (
              SELECT 1 FROM rooms
              WHERE conversation_id = NEW.conversation_id
          )
      )
  )
BEGIN
    SELECT RAISE(ABORT, 'MESSAGE_ROUTE_IMMUTABLE');
END;
"""


class MessageRoutingMixin:
    @staticmethod
    def _backfill_legacy_rooms(conn: sqlite3.Connection) -> None:
        """Give pre-room-table conversations a durable, non-owned room row."""
        rows = conn.execute(
            """
            WITH conversation_ids AS (
                SELECT conversation_id FROM memberships
                UNION
                SELECT conversation_id FROM messages
            )
            SELECT
                ids.conversation_id,
                (SELECT MIN(joined_at) FROM memberships
                 WHERE conversation_id = ids.conversation_id) AS first_joined_at,
                (SELECT MIN(created_at) FROM messages
                 WHERE conversation_id = ids.conversation_id) AS first_message_at,
                (SELECT MAX(created_at) FROM messages
                 WHERE conversation_id = ids.conversation_id) AS last_message_at
            FROM conversation_ids AS ids
            """
        ).fetchall()
        fallback_now = time.time()
        for row in rows:
            candidates = [
                float(value)
                for value in (row["first_joined_at"], row["first_message_at"])
                if value is not None
            ]
            created_at = min(candidates) if candidates else fallback_now
            last_activity_at = (
                float(row["last_message_at"])
                if row["last_message_at"] is not None
                else created_at
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO rooms
                    (conversation_id, status, creator_kind,
                     creator_participant_id, created_at, last_activity_at)
                VALUES (?, 'active', 'legacy', NULL, ?, ?)
                """,
                (str(row["conversation_id"]), created_at, last_activity_at),
            )

    @staticmethod
    def _normalize_mentions(values: Sequence[str] | None) -> list[str]:
        if values is None:
            return []
        if isinstance(values, (str, bytes)):
            raise ValidationError("mentions must be a list of participant_id values")
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            participant = opaque_id(value, field="mentions")
            if participant in seen:
                continue
            seen.add(participant)
            normalized.append(participant)
        if len(normalized) > MAX_MENTIONS_PER_MESSAGE:
            raise ValidationError(
                f"mentions cannot contain more than {MAX_MENTIONS_PER_MESSAGE} entries"
            )
        return normalized

    @staticmethod
    def _infer_text_mentions_locked(
        conn: sqlite3.Connection,
        *,
        conversation_id: str,
        sender_participant_id: str,
        body_text: str,
    ) -> list[str]:
        """Normalize exact visible @aliases into structured public mentions.

        Explicit participant IDs remain authoritative. This compatibility path
        exists for Agent clients that visibly write ``@name`` but forget the
        structured ``mentions`` argument. Ambiguous aliases are ignored.
        """

        rows = conn.execute(
            """
            SELECT participant.participant_id,
                   participant.client_type,
                   participant.display_name
            FROM memberships AS membership
            JOIN participants AS participant
              ON participant.participant_id = membership.participant_id
            WHERE membership.conversation_id = ?
              AND membership.active = 1
              AND participant.participant_id != ?
            """,
            (conversation_id, sender_participant_id),
        ).fetchall()
        alias_targets: dict[str, set[str]] = {}
        alias_display: dict[str, str] = {}
        for row in rows:
            participant_id = str(row["participant_id"])
            for candidate in (row["client_type"], row["display_name"]):
                visible = str(candidate or "").strip()
                if not visible:
                    continue
                folded = visible.casefold()
                alias_targets.setdefault(folded, set()).add(participant_id)
                alias_display.setdefault(folded, visible)
        inferred: list[str] = []
        for folded, targets in alias_targets.items():
            if len(targets) != 1:
                continue
            visible = alias_display[folded]
            # ``@全员`` is a reserved UI command.  Only the separately
            # authorized wake_all_agents flag can activate it, so plain text
            # must never become a personal mention merely because one Agent
            # happens to use the display name "全员".
            if visible.casefold() == "全员".casefold():
                continue
            # A visible mention may appear at the beginning, in the middle, or
            # at the end of a sentence.  The right boundary still prevents a
            # short nickname from matching the prefix of a longer token.
            pattern = (
                rf"@{re.escape(visible)}"
                r"(?=$|[\s,，。.!！?？:：;；、)）\]】}>》])"
            )
            if re.search(pattern, body_text, flags=re.IGNORECASE):
                inferred.append(next(iter(targets)))
        return sorted(set(inferred))

    @staticmethod
    def _visible_at_tokens(body_text: str) -> list[str]:
        """Return bounded user-visible @ tokens while ignoring email addresses."""

        pattern = re.compile(
            r"(?<![A-Za-z0-9._%+-])@"
            r"(?P<alias>[^\s@，,。.!！?？:：;；、()（）\[\]【】<>《》"
            r"'\"`]{1,128})"
        )
        result: list[str] = []
        for match in pattern.finditer(body_text):
            visible = str(match.group("alias") or "").strip()
            if visible and visible not in result:
                result.append(visible)
        return result[:MAX_MENTIONS_PER_MESSAGE]

    @classmethod
    def _mention_routing_diagnostics_locked(
        cls,
        conn: sqlite3.Connection,
        *,
        conversation_id: str,
        sender_participant_id: str,
        body_text: str,
        mentioned_participant_ids: Sequence[str],
        notification_mode: str,
        wake_all_agents: bool,
        notification_targeted: bool,
    ) -> dict[str, Any]:
        """Explain notification routing without making fuzzy prose authoritative."""

        rows = conn.execute(
            """
            SELECT participant.participant_id,
                   participant.client_type,
                   participant.display_name
            FROM memberships AS membership
            JOIN participants AS participant
              ON participant.participant_id = membership.participant_id
            WHERE membership.conversation_id = ?
              AND membership.active = 1
              AND participant.participant_id != ?
            ORDER BY membership.joined_at, participant.participant_id
            """,
            (conversation_id, sender_participant_id),
        ).fetchall()
        aliases: dict[str, set[str]] = {}
        participants: dict[str, dict[str, str]] = {}
        for row in rows:
            participant_id = str(row["participant_id"])
            participants[participant_id] = {
                "participant_id": participant_id,
                "display_name": str(row["display_name"]),
                "client_type": str(row["client_type"]),
            }
            for value in (row["display_name"], row["client_type"]):
                visible = str(value or "").strip()
                if visible:
                    aliases.setdefault(visible.casefold(), set()).add(participant_id)

        visible_tokens = cls._visible_at_tokens(body_text)
        resolved_visible: list[dict[str, str]] = []
        unresolved_visible: list[str] = []
        ambiguous_visible: list[str] = []
        reserved_visible: list[str] = []
        for visible in visible_tokens:
            if visible.casefold() == "全员".casefold():
                if wake_all_agents:
                    resolved_visible.append(
                        {"visible": visible, "kind": "wake_all"}
                    )
                else:
                    reserved_visible.append(visible)
                continue
            targets = aliases.get(visible.casefold(), set())
            if len(targets) == 1:
                resolved_visible.append(
                    {
                        "visible": visible,
                        "kind": "participant",
                        "participant_id": next(iter(targets)),
                    }
                )
            elif len(targets) > 1:
                ambiguous_visible.append(visible)
            else:
                unresolved_visible.append(visible)

        target_ids = list(dict.fromkeys(mentioned_participant_ids))
        targets = [
            participants[participant_id]
            for participant_id in target_ids
            if participant_id in participants
        ]
        warning = None
        if unresolved_visible or ambiguous_visible or reserved_visible:
            warning = (
                "visible_mention_unresolved: one or more visible @ names did not "
                "produce a structured notification; call agent_participants and "
                "resend with exact same-room participant IDs in mentions"
            )
        elif notification_mode == "ordinary":
            warning = (
                "ordinary_message_queued: no Agent is immediately notified; use "
                "notification_mode=mention with a structured target when timely "
                "attention is expected"
            )
        return {
            "notification_mode": notification_mode,
            "notified": bool(notification_targeted),
            "target_participants": targets,
            "wake_all_agents": wake_all_agents,
            "visible_tokens": visible_tokens,
            "resolved_visible": resolved_visible,
            "unresolved_visible": unresolved_visible,
            "ambiguous_visible": ambiguous_visible,
            "reserved_visible": reserved_visible,
            "warning": warning,
        }

    @staticmethod
    def _is_direct_review_request(body_text: str) -> bool:
        """Identify only explicit requests for review or confirmation.

        Status prose such as ``等待审批`` or ``需要审核`` is deliberately not
        enough.  The sender must use a request form, which keeps ordinary room
        discussion and progress updates on the existing interest-based path.
        """

        return any(
            pattern.search(body_text)
            for pattern in _DIRECT_REVIEW_REQUEST_PATTERNS
        )

    @classmethod
    def _is_direct_agent_reply_request(cls, body_text: str) -> bool:
        """Distinguish actionable Agent requests from courtesy mentions.

        Agent-to-Agent mentions stay optional by default.  An explicit task,
        question, review, or confirmation request requires one substantive
        response, which prevents delegated work from silently stalling while
        still avoiding acknowledgement loops.
        """

        return cls._is_direct_review_request(body_text) or any(
            pattern.search(body_text)
            for pattern in _DIRECT_AGENT_REPLY_REQUEST_PATTERNS
        )

    @staticmethod
    def _is_acknowledgement_only(body_text: str) -> bool:
        visible = re.sub(
            r"(?:^|\s)@[^s@，,。.!！?？:：;；]{1,128}",
            " ",
            str(body_text or ""),
        ).strip()
        return len(visible) <= 160 and bool(
            _ACKNOWLEDGEMENT_ONLY_PATTERN.fullmatch(visible)
        )

    @staticmethod
    def _assert_agent_identity_consistent_locked(
        conn: sqlite3.Connection,
        *,
        participant_id: str,
        body_text: str,
    ) -> None:
        """Reject only an Agent's explicit denial of its fixed public name."""

        participant = conn.execute(
            """
            SELECT participant.display_name, participant.client_type
            FROM participants AS participant
            LEFT JOIN web_users AS web_user
              ON web_user.participant_id = participant.participant_id
            WHERE participant.participant_id = ?
              AND web_user.user_id IS NULL
            """,
            (participant_id,),
        ).fetchone()
        if participant is None:
            return
        public_name = str(participant["display_name"] or "").strip()
        if not public_name:
            return
        denial = re.compile(
            rf"我(?:并|本来)?不是\s*@?{re.escape(public_name)}"
            rf"(?=$|[\s,，。.!！?？:：;；])",
            flags=re.IGNORECASE,
        )
        if denial.search(body_text):
            raise ConflictError(
                "sender_identity_contradiction: 你的固定公开昵称是 "
                f"{public_name}；@{public_name} 指向你本人。值守影子与执行席位"
                "共享这个公开身份，不能把它说成另一个人"
            )

    @staticmethod
    def _infer_named_review_targets_locked(
        conn: sqlite3.Connection,
        *,
        conversation_id: str,
        sender_participant_id: str,
        body_text: str,
    ) -> list[str]:
        """Resolve unique same-room names in an explicit review request.

        This is intentionally narrower than fuzzy name matching: only complete
        public ``display_name`` or ``client_type`` aliases are considered, and
        an alias shared by multiple active members is ignored.  Existing @
        inference remains authoritative for text that already contains @.
        """

        rows = conn.execute(
            """
            SELECT participant.participant_id,
                   participant.client_type,
                   participant.display_name
            FROM memberships AS membership
            JOIN participants AS participant
              ON participant.participant_id = membership.participant_id
            WHERE membership.conversation_id = ?
              AND membership.active = 1
              AND participant.participant_id != ?
            """,
            (conversation_id, sender_participant_id),
        ).fetchall()
        alias_targets: dict[str, set[str]] = {}
        aliases: dict[str, str] = {}
        for row in rows:
            participant_id = str(row["participant_id"])
            for candidate in (row["display_name"], row["client_type"]):
                visible = str(candidate or "").strip()
                if not visible or visible.casefold() == "全员".casefold():
                    continue
                folded = visible.casefold()
                alias_targets.setdefault(folded, set()).add(participant_id)
                aliases.setdefault(folded, visible)

        inferred: set[str] = set()
        occupied_spans: list[tuple[int, int]] = []
        for folded, targets in sorted(
            alias_targets.items(),
            key=lambda item: len(aliases[item[0]]),
            reverse=True,
        ):
            if len(targets) != 1:
                continue
            visible = aliases[folded]
            left_boundary = (
                r"(?<![A-Za-z0-9._:@-])"
                if visible[0].isascii()
                else r"(?<!@)"
            )
            right_boundary = (
                r"(?![A-Za-z0-9._:@-])" if visible[-1].isascii() else ""
            )
            pattern = rf"{left_boundary}{re.escape(visible)}{right_boundary}"
            for match in re.finditer(pattern, body_text, flags=re.IGNORECASE):
                span = match.span()
                if any(
                    span[0] < occupied_end and occupied_start < span[1]
                    for occupied_start, occupied_end in occupied_spans
                ):
                    continue
                inferred.add(next(iter(targets)))
                occupied_spans.append(span)
                break
        return sorted(inferred)

    @staticmethod
    def _reply_sender_locked(
        conn: sqlite3.Connection,
        *,
        conversation_id: str,
        sender_participant_id: str,
        reply_to: str | None,
    ) -> str | None:
        if reply_to is None:
            return None
        row = conn.execute(
            """
            SELECT original.sender_participant_id
            FROM messages AS original
            JOIN memberships AS membership
              ON membership.conversation_id = original.conversation_id
             AND membership.participant_id = original.sender_participant_id
             AND membership.active = 1
            WHERE original.message_id = ?
              AND original.conversation_id = ?
              AND original.sender_participant_id != ?
            """,
            (reply_to, conversation_id, sender_participant_id),
        ).fetchone()
        return str(row["sender_participant_id"]) if row is not None else None

    @staticmethod
    def _role_review_targets_locked(
        conn: sqlite3.Connection,
        *,
        conversation_id: str,
        sender_participant_id: str,
        role: str,
    ) -> list[str]:
        rows = conn.execute(
            """
            SELECT membership.participant_id, membership.roles_json
            FROM memberships AS membership
            WHERE membership.conversation_id = ?
              AND membership.active = 1
              AND membership.participant_id != ?
            """,
            (conversation_id, sender_participant_id),
        ).fetchall()
        return sorted(
            str(row["participant_id"])
            for row in rows
            if role in set(json.loads(str(row["roles_json"])))
        )

    @staticmethod
    def _rewrite_internal_text_mentions_locked(
        conn: sqlite3.Connection,
        *,
        conversation_id: str,
        sender_participant_id: str,
        body_text: str,
        include_inactive: bool = False,
    ) -> tuple[str, list[str]]:
        """Keep opaque participant IDs out of user-visible message bodies.

        MCP routing uses participant IDs, but visible chat text must use the
        member's current display name.  Some models copied an ID returned by
        ``agent_participants`` into ``@participant_...`` text and omitted the
        structured ``mentions`` argument.  Resolve only exact IDs belonging to
        the same room, so unrelated prose and unknown identifiers are left
        untouched.  The returned IDs let new messages retain real mention
        delivery semantics after the visible text is rewritten.
        """

        membership_filter = "" if include_inactive else "AND membership.active = 1"
        rows = conn.execute(
            """
            SELECT participant.participant_id,
                   participant.client_type,
                   participant.display_name
            FROM memberships AS membership
            JOIN participants AS participant
              ON participant.participant_id = membership.participant_id
            WHERE membership.conversation_id = ?
              AND participant.participant_id != ?
            """
            f" {membership_filter}",
            (conversation_id, sender_participant_id),
        ).fetchall()
        visible_names = {
            str(row["participant_id"]): str(
                row["display_name"] or row["client_type"]
            ).strip()
            for row in rows
        }
        if "@participant_" not in body_text:
            return body_text, []

        # Match known IDs rather than a broad participant-looking token.  The
        # negative lookahead permits Chinese text directly after an ID while
        # preventing a shorter ID from matching inside a longer opaque token.
        mentioned: list[str] = []
        rewritten = body_text
        if visible_names:
            alternatives = "|".join(
                re.escape(participant_id)
                for participant_id in sorted(visible_names, key=len, reverse=True)
            )
            pattern = re.compile(
                rf"@(?P<participant_id>{alternatives})(?![A-Za-z0-9._:-])"
            )

            def replace(match: re.Match[str]) -> str:
                participant_id = match.group("participant_id")
                if participant_id not in mentioned:
                    mentioned.append(participant_id)
                return f"@{visible_names[participant_id]}"

            rewritten = pattern.sub(replace, rewritten)

        # Never leak an unresolved opaque mention into user-visible chat.  It
        # may refer to a removed member or stale model context; keep the prose
        # readable but deliberately do not create a delivery for it.
        unresolved_pattern = re.compile(
            r"@participant_[A-Za-z0-9._:-]+(?![A-Za-z0-9._:-])"
        )
        rewritten = unresolved_pattern.sub("成员（已离开或不可用）", rewritten)
        return rewritten, mentioned

    @classmethod
    def _migrate_internal_participant_mentions_to_display_names(
        cls,
        conn: sqlite3.Connection,
    ) -> None:
        """Rewrite legacy Agent-authored opaque @ IDs without replaying them.

        This intentionally changes only body text.  Existing mention metadata,
        delivery priority, receipts, and notification cursors stay untouched,
        so opening an upgraded database cannot wake Agents for old messages.
        Web-authored messages are excluded to preserve the exact body hash of
        any historical admin authorization snapshot.
        """

        rows = conn.execute(
            """
            SELECT message.message_id,
                   message.conversation_id,
                   message.sender_participant_id,
                   message.body
            FROM messages AS message
            LEFT JOIN web_users AS web_user
              ON web_user.participant_id = message.sender_participant_id
            WHERE instr(message.body, '@participant_') > 0
              AND web_user.user_id IS NULL
              AND message.sender_participant_id != ?
            ORDER BY message.sequence
            """,
            (OWNER_PARTICIPANT_ID,),
        ).fetchall()
        for row in rows:
            rewritten, _mentioned = cls._rewrite_internal_text_mentions_locked(
                conn,
                conversation_id=str(row["conversation_id"]),
                sender_participant_id=str(row["sender_participant_id"]),
                body_text=str(row["body"]),
                include_inactive=True,
            )
            if rewritten != str(row["body"]):
                conn.execute(
                    "UPDATE messages SET body = ? WHERE message_id = ?",
                    (rewritten, str(row["message_id"])),
                )

    @staticmethod
    def _backfill_implicit_participant_mentions(conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT message_id, audience_value, mentions_json FROM messages "
            "WHERE audience_kind = 'participant'"
        ).fetchall()
        for row in rows:
            mentions = list(json.loads(str(row["mentions_json"] or "[]")))
            target = str(row["audience_value"])
            if target not in mentions:
                mentions.append(target)
                conn.execute(
                    "UPDATE messages SET mentions_json = ? WHERE message_id = ?",
                    (compact_json(mentions), str(row["message_id"])),
                )

    @classmethod
    def _delivery_candidates_locked(
        cls,
        conn: sqlite3.Connection,
        message: sqlite3.Row,
        *,
        include_inactive_memberships: bool = False,
        include_optional_wakes: bool = True,
    ) -> list[dict[str, Any]]:
        conversation = str(message["conversation_id"])
        sender = str(message["sender_participant_id"])
        created_at = float(message["created_at"])
        mention_ids = set(json.loads(str(message["mentions_json"] or "[]")))
        wake_all_agents = bool(message["wake_all_agents"])
        sender_is_web_user = conn.execute(
            "SELECT 1 FROM web_users WHERE participant_id = ?",
            (sender,),
        ).fetchone() is not None
        sender_is_human = sender_is_web_user or sender == OWNER_PARTICIPANT_ID
        acknowledgement_only = (
            not sender_is_human
            and cls._is_acknowledgement_only(str(message["body"]))
        )
        reply_target = None
        if message["reply_to"] is not None:
            replied = conn.execute(
                "SELECT sender_participant_id FROM messages WHERE message_id = ?",
                (str(message["reply_to"]),),
            ).fetchone()
            if replied is not None:
                reply_target = str(replied["sender_participant_id"])
        # Personal Agent mentions form a deterministic one-hop contract.  A
        # top-level mention, or a reply that brings in a third participant,
        # requires one response.  Mentioning the root author in a reply is the
        # closeout itself and stays optional, preventing acknowledgement loops.
        # Body text is deliberately not inspected.
        structured_agent_request = bool(
            not sender_is_human
            and str(message["notification_mode"]) == "mention"
        )
        membership_filter = "" if include_inactive_memberships else "AND active = 1"
        memberships = conn.execute(
            "SELECT membership.participant_id, membership.roles_json, "
            "membership.joined_at, web_user.user_id AS web_user_id "
            "FROM memberships AS membership "
            "LEFT JOIN web_users AS web_user "
            "ON web_user.participant_id = membership.participant_id "
            "WHERE membership.conversation_id = ? "
            f"{membership_filter.replace('active', 'membership.active')} "
            "AND membership.joined_at <= ?",
            (conversation, created_at),
        ).fetchall()
        followers = {
            str(row["follower_participant_id"])
            for row in conn.execute(
                "SELECT follower_participant_id FROM follows "
                "WHERE conversation_id = ? AND followed_participant_id = ? "
                "AND active = 1 AND created_at <= ?",
                (conversation, sender, created_at),
            ).fetchall()
        }
        quiet_participants = {
            str(row["participant_id"])
            for row in conn.execute(
                "SELECT participant_id FROM agent_room_dnd "
                "WHERE conversation_id = ? AND enabled_at <= ? AND expires_at > ?",
                (conversation, created_at, created_at),
            ).fetchall()
        }
        audience_kind = str(message["audience_kind"])
        candidates: list[dict[str, Any]] = []
        for membership in memberships:
            participant = str(membership["participant_id"])
            if participant == sender:
                continue
            is_agent = membership["web_user_id"] is None
            roles = set(json.loads(str(membership["roles_json"])))
            primary_recipient = cls._eligible(
                message,
                participant_id=participant,
                roles=roles,
            )
            reasons = ["room_activity"]
            if acknowledgement_only:
                reasons.append("echo_suppressed")
            if primary_recipient:
                reasons.append(f"audience:{audience_kind}")
            if participant in mention_ids:
                # Courtesy Agent mentions remain optional, while explicit
                # assignments/questions get one required response.  Human
                # personal mentions retain their existing required semantics.
                if participant in quiet_participants:
                    reasons.extend(("agent_mention", "quiet_optional"))
                elif sender_is_human:
                    reasons.append("mention")
                elif structured_agent_request and (
                    reply_target is None or participant != reply_target
                ):
                    reasons.append("agent_request")
                else:
                    reasons.append("agent_mention")
            if include_optional_wakes and wake_all_agents and is_agent:
                reasons.append("wake_all")
                if participant in quiet_participants:
                    reasons.append("quiet_optional")
            if include_optional_wakes and reply_target == participant and is_agent:
                reasons.append("reply_wake")
                if participant in quiet_participants:
                    reasons.append("quiet_optional")
            if participant in followers:
                reasons.append("follow")
            if participant in mention_ids:
                priority = "direct"
            elif "wake_all" in reasons or "reply_wake" in reasons:
                priority = "direct"
            elif (
                "follow" in reasons
                or (audience_kind == "role" and primary_recipient)
            ):
                priority = "important"
            else:
                priority = "normal"
            candidates.append(
                {
                    "participant_id": participant,
                    "reasons": reasons,
                    "priority": priority,
                    "actionable": (
                        primary_recipient
                        and audience_kind in {"participant", "role"}
                    ),
                }
            )
        return candidates

    @staticmethod
    def _migrate_agent_mentions_to_optional(conn: sqlite3.Connection) -> None:
        """Decouple historical Agent mentions from mandatory reply semantics."""
        conn.execute(
            """
            UPDATE message_deliveries
            SET reasons_json = replace(
                reasons_json,
                '"mention"',
                '"agent_mention"'
            )
            WHERE instr(reasons_json, '"mention"') > 0
              AND message_id IN (
                  SELECT message.message_id
                  FROM messages AS message
                  LEFT JOIN web_users AS web_user
                    ON web_user.participant_id = message.sender_participant_id
                  WHERE web_user.user_id IS NULL
                    AND message.sender_participant_id != ?
              )
            """,
            (OWNER_PARTICIPANT_ID,),
        )

    @classmethod
    def _create_message_deliveries_locked(
        cls,
        conn: sqlite3.Connection,
        message: sqlite3.Row,
    ) -> None:
        if str(message["message_kind"]) == "task":
            # Structured tasks have their own atomic claim ledger and resident
            # executor.  Keeping them out of the ordinary chat-delivery queue
            # prevents a later @ wake from making the read-only chat worker
            # discuss or acknowledge the task a second time.
            return
        mention_ids = set(json.loads(str(message["mentions_json"] or "[]")))
        candidates = cls._delivery_candidates_locked(conn, message)
        restriction = conn.execute(
            "SELECT 1 FROM message_restrictions WHERE message_id = ?",
            (str(message["message_id"]),),
        ).fetchone()
        if restriction is not None:
            allowed_recipients = {
                str(row["participant_id"])
                for row in conn.execute(
                    "SELECT participant_id FROM message_restriction_recipients "
                    "WHERE message_id = ?",
                    (str(message["message_id"]),),
                ).fetchall()
            }
            candidates = [
                item
                for item in candidates
                if str(item["participant_id"]) in allowed_recipients
            ]
        candidate_ids = {str(item["participant_id"]) for item in candidates}
        missing_mentions = sorted(mention_ids - candidate_ids)
        if missing_mentions:
            raise ConflictError(
                "mentioned participants must be active eligible recipients in the "
                f"same room: {', '.join(missing_mentions)}"
            )
        for candidate in candidates:
            conn.execute(
                """
                INSERT INTO message_deliveries
                    (message_id, participant_id, state, reasons_json,
                     priority, actionable, created_at)
                VALUES (?, ?, 'pending', ?, ?, ?, ?)
                """,
                (
                    str(message["message_id"]),
                    str(candidate["participant_id"]),
                    compact_json(candidate["reasons"]),
                    str(candidate["priority"]),
                    1 if candidate["actionable"] else 0,
                    float(message["created_at"]),
                ),
            )

    @classmethod
    def _admin_chat_authorization_targets_locked(
        cls,
        conn: sqlite3.Connection,
        message: sqlite3.Row,
    ) -> tuple[str, list[str]] | None:
        conversation_id = str(message["conversation_id"])
        mentioned = list(json.loads(str(message["mentions_json"] or "[]")))
        if bool(message["wake_all_agents"]):
            targets = cls._room_agent_ids_locked(
                conn,
                conversation_id=conversation_id,
                created_at=float(message["created_at"]),
            )
            return ("room_agents", targets) if targets else None

        agent_targets: list[str] = []
        for participant_id in mentioned:
            target = conn.execute(
                """
                SELECT participant.participant_id
                FROM memberships AS membership
                JOIN participants AS participant
                  ON participant.participant_id = membership.participant_id
                LEFT JOIN web_users AS web_user
                  ON web_user.participant_id = participant.participant_id
                WHERE membership.conversation_id = ?
                  AND membership.participant_id = ?
                  AND membership.active = 1
                  AND web_user.user_id IS NULL
                  AND participant.participant_id != ?
                """,
                (conversation_id, participant_id, OWNER_PARTICIPANT_ID),
            ).fetchone()
            if target is not None:
                agent_targets.append(str(target["participant_id"]))
        if agent_targets:
            return "participants", sorted(set(agent_targets))
        if mentioned:
            # Explicit @ targets that are only Web users must not spill authority
            # over to unrelated Agents in the same public room.
            return None

        if message["reply_to"] is not None:
            reply_author = conn.execute(
                """
                SELECT original.sender_participant_id
                FROM messages AS original
                LEFT JOIN web_users AS web_user
                  ON web_user.participant_id = original.sender_participant_id
                WHERE original.message_id = ?
                  AND original.conversation_id = ?
                  AND web_user.user_id IS NULL
                  AND original.sender_participant_id != ?
                """,
                (
                    str(message["reply_to"]),
                    conversation_id,
                    OWNER_PARTICIPANT_ID,
                ),
            ).fetchone()
            if reply_author is not None:
                return "reply_author", [str(reply_author["sender_participant_id"])]
            return None

        # An authenticated admin message without a narrower addressee applies
        # to Agents already in the room when it was sent. It does not wake them.
        targets = cls._room_agent_ids_locked(
            conn,
            conversation_id=conversation_id,
            created_at=float(message["created_at"]),
        )
        return ("room_agents", targets) if targets else None

    @staticmethod
    def _room_agent_ids_locked(
        conn: sqlite3.Connection,
        *,
        conversation_id: str,
        created_at: float,
    ) -> list[str]:
        rows = conn.execute(
            """
            SELECT membership.participant_id
            FROM memberships AS membership
            LEFT JOIN web_users AS web_user
              ON web_user.participant_id = membership.participant_id
            WHERE membership.conversation_id = ?
              AND membership.joined_at <= ?
              AND membership.active = 1
              AND web_user.user_id IS NULL
              AND membership.participant_id != ?
            ORDER BY membership.participant_id
            """,
            (conversation_id, created_at, OWNER_PARTICIPANT_ID),
        ).fetchall()
        return [str(row["participant_id"]) for row in rows]

    @classmethod
    def _insert_admin_chat_authorization_grant_locked(
        cls,
        conn: sqlite3.Connection,
        *,
        message: sqlite3.Row,
        issuer_web_user_id: str,
        issuer_username: str,
        issuer_role: str,
    ) -> None:
        if CHAT_AUTHORIZATION_FROZEN:
            return
        if issuer_role != "admin" or str(message["message_kind"]) == "forward":
            return
        target = cls._admin_chat_authorization_targets_locked(
            conn,
            message,
        )
        if target is None:
            return
        target_kind, targets = target
        conn.execute(
            """
            INSERT OR IGNORE INTO chat_authorization_grants
                (source_message_id, conversation_id, issuer_web_user_id,
                 issuer_username_snapshot, issuer_role_snapshot,
                 issuer_participant_id, body_sha256, target_kind,
                 target_participant_ids_json, authority_kind, created_at)
            VALUES (?, ?, ?, ?, 'admin', ?, ?, ?, ?, 'admin_chat', ?)
            """,
            (
                str(message["message_id"]),
                str(message["conversation_id"]),
                issuer_web_user_id,
                issuer_username,
                str(message["sender_participant_id"]),
                hashlib.sha256(str(message["body"]).encode("utf-8")).hexdigest(),
                target_kind,
                compact_json(targets),
                float(message["created_at"]),
            ),
        )

    @classmethod
    def _backfill_message_deliveries(
        cls,
        conn: sqlite3.Connection,
        *,
        include_optional_wakes: bool = True,
    ) -> None:
        """Build and reconcile the durable room-delivery ledger.

        Earlier Bridge versions treated ``audience_kind=participant`` as a
        visibility filter.  It is now a structured @ target: every member who
        was already in the room gets a delivery row, while only the target is
        actionable.  Reconciliation intentionally preserves existing delivery
        state and receipts.  It only revives the legacy ``cancelled`` rows that
        were created by the short-lived private-message migration.

        Resolved historical messages are inserted as acknowledged so an
        upgrade cannot manufacture months of unread notifications.  The room
        history remains complete, and explicit legacy receipts stay intact.
        """
        messages = conn.execute(
            """
            SELECT message.*
            FROM messages AS message
            ORDER BY message.sequence
            """
        ).fetchall()
        for message in messages:
            candidates = cls._delivery_candidates_locked(
                conn,
                message,
                include_inactive_memberships=True,
                include_optional_wakes=include_optional_wakes,
            )
            existing_deliveries = {
                str(row["participant_id"]): row
                for row in conn.execute(
                    "SELECT * FROM message_deliveries WHERE message_id = ?",
                    (str(message["message_id"]),),
                ).fetchall()
            }
            for candidate in candidates:
                participant_id = str(candidate["participant_id"])
                receipt = conn.execute(
                    "SELECT * FROM receipts WHERE message_id = ? AND participant_id = ?",
                    (
                        str(message["message_id"]),
                        participant_id,
                    ),
                ).fetchone()
                receipt_state = str(receipt["state"]) if receipt is not None else ""
                existing = existing_deliveries.get(participant_id)
                if existing is not None and str(existing["state"]) != "cancelled":
                    state = str(existing["state"])
                elif receipt_state == "acked" or str(message["status"]) != "open":
                    state = "acked"
                elif receipt_state == "delivered":
                    state = "delivered"
                else:
                    state = "pending"
                delivered_at = (
                    float(receipt["delivered_at"])
                    if receipt is not None and receipt["delivered_at"] is not None
                    else (
                        float(existing["first_delivered_at"])
                        if existing is not None
                        and existing["first_delivered_at"] is not None
                        else None
                    )
                )
                acked_at = (
                    float(receipt["acked_at"])
                    if receipt is not None and receipt["acked_at"] is not None
                    else (
                        float(existing["acked_at"])
                        if existing is not None and existing["acked_at"] is not None
                        else (
                            float(message["updated_at"])
                            if state == "acked"
                            else None
                        )
                    )
                )
                conn.execute(
                    """
                    INSERT INTO message_deliveries
                        (message_id, participant_id, state, reasons_json,
                         priority, actionable, created_at, first_delivered_at,
                         last_delivered_at, acked_at, attempt_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(message_id, participant_id) DO UPDATE SET
                        state = excluded.state,
                        reasons_json = excluded.reasons_json,
                        priority = excluded.priority,
                        actionable = excluded.actionable,
                        first_delivered_at = COALESCE(
                            message_deliveries.first_delivered_at,
                            excluded.first_delivered_at
                        ),
                        last_delivered_at = COALESCE(
                            message_deliveries.last_delivered_at,
                            excluded.last_delivered_at
                        ),
                        acked_at = COALESCE(
                            message_deliveries.acked_at,
                            excluded.acked_at
                        ),
                        attempt_count = MAX(
                            message_deliveries.attempt_count,
                            excluded.attempt_count
                        )
                    """,
                    (
                        str(message["message_id"]),
                        participant_id,
                        state,
                        compact_json(candidate["reasons"]),
                        str(candidate["priority"]),
                        1
                        if candidate["actionable"]
                        and str(message["status"]) == "open"
                        else 0,
                        float(message["created_at"]),
                        delivered_at,
                        delivered_at,
                        acked_at,
                        (
                            int(existing["attempt_count"])
                            if existing is not None
                            else (1 if delivered_at is not None else 0)
                        ),
                    ),
                )

    @staticmethod
    def _eligible(
        row: sqlite3.Row,
        *,
        participant_id: str,
        roles: set[str],
    ) -> bool:
        audience_kind = str(row["audience_kind"])
        audience_value = str(row["audience_value"])
        if audience_kind == "participant":
            return audience_value == participant_id
        if audience_kind in {"room", "broadcast"}:
            return audience_value in {"*", str(row["conversation_id"])}
        if audience_kind == "role":
            return audience_value in roles
        return False

    @staticmethod
    def _normalize_audience_value(
        audience_kind: str,
        value: str,
        conversation_id: str,
    ) -> str:
        if audience_kind == "participant":
            return opaque_id(value, field="audience_value")
        if audience_kind == "role":
            return token(value, field="audience_value")
        if audience_kind == "room":
            normalized = str(value or "*").strip()
            if normalized == "*":
                return conversation_id
            target = validate_conversation_id(normalized)
            if target != conversation_id:
                raise ValidationError("room audience must match conversation_id")
            return target
        return "*"
