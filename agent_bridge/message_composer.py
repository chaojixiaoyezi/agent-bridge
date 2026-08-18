"""Authenticated message composition, wake policy, and explicit forwarding."""

from __future__ import annotations

import math
import sqlite3
import time
import uuid
from collections.abc import Sequence
from typing import Any

from .store_constants import (
    AUDIENCE_KINDS,
    DEFAULT_ROOM_DIGEST_AFTER_SECONDS,
    DEFAULT_ROOM_DIGEST_MIN_MESSAGES,
    DEFAULT_ROOM_WAKE_MODE,
    MAX_MENTIONS_PER_MESSAGE,
    MESSAGE_COOLDOWN_SECONDS,
    MESSAGE_NOTIFICATION_MODES,
    OWNER_AUTHORIZATION_ID,
    OWNER_PARTICIPANT_ID,
    ROOM_WAKE_MODES,
)
from .store_errors import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    NotFoundError,
)
from .validation import (
    ValidationError,
    body,
    compact_json,
    conversation_id as validate_conversation_id,
    message_refs,
    opaque_id,
)


ROOM_WAKE_POLICY_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS room_wake_policies (
    conversation_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL DEFAULT 'mention'
        CHECK (mode IN ('mention', 'digest', 'all')),
    digest_min_messages INTEGER NOT NULL DEFAULT {DEFAULT_ROOM_DIGEST_MIN_MESSAGES}
        CHECK (digest_min_messages BETWEEN 1 AND 500),
    digest_after_seconds REAL NOT NULL DEFAULT {DEFAULT_ROOM_DIGEST_AFTER_SECONDS}
        CHECK (digest_after_seconds BETWEEN 30 AND 86400),
    updated_by_web_user_id TEXT,
    updated_at REAL NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES rooms(conversation_id),
    FOREIGN KEY (updated_by_web_user_id) REFERENCES web_users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_room_wake_policies_updated
    ON room_wake_policies(updated_at DESC);

CREATE TABLE IF NOT EXISTS agent_room_dnd (
    participant_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    enabled_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    timezone_name TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (participant_id, conversation_id),
    FOREIGN KEY (participant_id) REFERENCES participants(participant_id),
    FOREIGN KEY (conversation_id) REFERENCES rooms(conversation_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_room_dnd_expiry
    ON agent_room_dnd(expires_at, participant_id, conversation_id);
"""


class MessageComposerMixin:
    def send(
        self,
        *,
        authorized_session_id: str,
        sender_participant_id: str,
        conversation_id: str,
        body_text: str,
        audience_kind: str = "room",
        audience_value: str = "*",
        reply_to: str | None = None,
        refs: Sequence[dict[str, Any]] | None = None,
        mentions: Sequence[str] | None = None,
        links: Sequence[str] | None = None,
        notification_mode: str | None = None,
        wake_all_agents: bool = False,
        _owner_ui: bool = False,
        _web_user: bool = False,
        _message_kind: str = "message",
        _forwarded_from_message_id: str | None = None,
        _suppress_chat_authorization: bool = False,
        _suppress_mention_inference: bool = False,
        _task_request: dict[str, Any] | None = None,
        _staged_attachments: Sequence[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        session = opaque_id(
            authorized_session_id,
            field="authorized_session_id",
        )
        sender = opaque_id(sender_participant_id, field="sender_participant_id")
        conversation = validate_conversation_id(conversation_id)
        normalized_links = self._normalize_message_links(links)
        staged_attachments = list(_staged_attachments or [])
        raw_body = str(body_text or "")
        normalized_body = body(raw_body) if raw_body.strip() else ""
        if not normalized_body and not normalized_links and not staged_attachments:
            normalized_body = body(raw_body)
        normalized_audience = str(audience_kind or "").strip().lower()
        if normalized_audience not in AUDIENCE_KINDS:
            raise ValidationError(f"unsupported audience_kind: {normalized_audience}")
        normalized_refs = message_refs(refs)
        normalized_mentions = self._normalize_mentions(mentions)
        requested_notification_mode = (
            str(notification_mode).strip().lower()
            if notification_mode is not None
            else None
        )
        if (
            requested_notification_mode is not None
            and requested_notification_mode not in MESSAGE_NOTIFICATION_MODES
        ):
            raise ValidationError(
                "notification_mode must be ordinary or mention"
            )
        if not isinstance(wake_all_agents, bool):
            raise ValidationError("wake_all_agents must be a boolean")
        normalized_wake_all = bool(wake_all_agents)
        normalized_reply = (
            opaque_id(reply_to, field="reply_to") if reply_to else None
        )
        normalized_message_kind = str(_message_kind or "message").strip().lower()
        normalized_forward = (
            opaque_id(
                _forwarded_from_message_id,
                field="forwarded_from_message_id",
            )
            if _forwarded_from_message_id
            else None
        )
        if normalized_message_kind not in {"message", "forward", "task"}:
            raise ValidationError("unsupported internal message kind")
        if (normalized_message_kind == "forward") != bool(normalized_forward):
            raise ValidationError("cross-room forwards require one source message")
        if (normalized_message_kind == "task") != bool(_task_request):
            raise ValidationError("structured task messages require task metadata")
        if staged_attachments and not (_owner_ui or _web_user):
            raise AuthorizationError(
                "only an authenticated Web user can upload files or images"
            )
        if staged_attachments and normalized_message_kind != "message":
            raise ValidationError(
                "files and images are supported only in ordinary chat messages"
            )
        if normalized_wake_all and normalized_audience != "room":
            raise ValidationError("wake_all_agents requires a room audience")
        normalized_target = self._normalize_audience_value(
            normalized_audience,
            audience_value,
            conversation,
        )
        if (
            normalized_audience == "participant"
            and normalized_target not in normalized_mentions
        ):
            normalized_mentions.append(normalized_target)
        now = time.time()
        message_id = f"msg_{uuid.uuid4().hex}"
        task_id = f"task_{uuid.uuid4().hex}" if _task_request else None
        task_target_kind: str | None = None
        task_target_ids: list[str] = []
        review_routing: dict[str, Any] | None = None
        coordination_routing: dict[str, Any] | None = None
        mention_routing: dict[str, Any] | None = None
        sender_seat = "unknown"
        web_identity: sqlite3.Row | None = None
        body_routing: list[dict[str, Any]] = []
        inherited_restriction_target_kind: str | None = None
        inherited_restriction_recipients: list[str] = []

        with self._transaction() as conn:
            self._archive_stale_rooms_locked(conn, now=now)
            cooldown_seconds = MESSAGE_COOLDOWN_SECONDS
            if _owner_ui:
                sender_seat = "web"
                if session != OWNER_AUTHORIZATION_ID or sender != OWNER_PARTICIPANT_ID:
                    raise AuthenticationError("invalid owner UI sender binding")
                self._require_active_room(conn, conversation)
                self._ensure_owner_membership_locked(
                    conn,
                    conversation_id=conversation,
                    now=now,
                )
            elif _web_user:
                sender_seat = "web"
                web_identity = self._require_live_web_session(
                    conn,
                    session_id=session,
                    participant_id=sender,
                    now=now,
                )
                self._require_web_room_access_locked(
                    conn,
                    web_identity=web_identity,
                    conversation_id=conversation,
                )
                self._require_active_room(conn, conversation)
                self._ensure_web_membership_locked(
                    conn,
                    conversation_id=conversation,
                    participant_id=sender,
                    display_name=str(web_identity["display_name"]),
                    signature=str(web_identity["signature"]),
                    role=str(web_identity["role"]),
                    now=now,
                )
                if _task_request is not None:
                    task_permissions = self._room_task_permissions_locked(
                        conn,
                        conversation_id=conversation,
                        web_identity=web_identity,
                    )
                    if not task_permissions["can_assign_tasks"]:
                        raise AuthorizationError(
                            "你没有在这个聊天室布置结构化任务的权限"
                        )
                    task_target_kind, task_target_ids = (
                        self._resolve_task_targets_locked(
                            conn,
                            conversation_id=conversation,
                            requested_participant_ids=_task_request.get(
                                "target_participant_ids"
                            ),
                        )
                    )
                if normalized_wake_all:
                    room_permissions = self._room_web_permissions_locked(
                        conn,
                        web_user_id=str(web_identity["user_id"]),
                        conversation_id=conversation,
                    )
                    if not room_permissions["can_wake_all"]:
                        raise AuthorizationError(
                            "只有全局管理员、创建者或聊天室管理员可以使用 @全员"
                        )
                cooldown_seconds = (
                    0.0
                    if str(web_identity["role"]) == "admin"
                    else self._effective_message_cooldown_locked(
                        conn,
                        participant_id=sender,
                        actor_kind="web_user",
                    )
                )
            else:
                live_session = self._require_live_room_session(
                    conn,
                    session_id=session,
                    participant_id=sender,
                    conversation_id=conversation,
                    now=now,
                )
                self._require_session_write_authority_locked(
                    conn,
                    session=live_session,
                )
                sender_seat = {
                    "mcp": "main",
                    "chat": "shadow",
                    "task": "executor",
                    "a2a": "a2a",
                }.get(str(live_session["component"] or "unknown"), "unknown")
                self._require_membership(conn, sender, conversation)
                cooldown_seconds = self._effective_message_cooldown_locked(
                    conn,
                    participant_id=sender,
                    actor_kind="agent",
                )
                if normalized_wake_all:
                    raise AuthorizationError("Agent 不能发起结构化 @全员")
            internal_mentions: list[str] = []
            if not _suppress_mention_inference and normalized_body:
                normalized_body, internal_mentions = (
                    self._rewrite_internal_text_mentions_locked(
                        conn,
                        conversation_id=conversation,
                        sender_participant_id=sender,
                        body_text=normalized_body,
                    )
                )
            # A display name can be longer than its opaque ID, so enforce the
            # body limit again after the user-visible rewrite.
            if normalized_body:
                normalized_body = body(normalized_body)
            for inferred in internal_mentions:
                if inferred not in normalized_mentions:
                    normalized_mentions.append(inferred)
            if not _suppress_mention_inference and normalized_body:
                for inferred in self._infer_text_mentions_locked(
                    conn,
                    conversation_id=conversation,
                    sender_participant_id=sender,
                    body_text=normalized_body,
                ):
                    if inferred not in normalized_mentions:
                        normalized_mentions.append(inferred)
            if len(normalized_mentions) > MAX_MENTIONS_PER_MESSAGE:
                raise ValidationError(
                    "mentions cannot contain more than "
                    f"{MAX_MENTIONS_PER_MESSAGE} entries"
                )
            if not _owner_ui and not _web_user:
                self._assert_agent_identity_consistent_locked(
                    conn,
                    participant_id=sender,
                    body_text=normalized_body,
                )
            if normalized_audience == "participant":
                self._require_membership(conn, normalized_target, conversation)
            if normalized_reply:
                original = conn.execute(
                    "SELECT conversation_id, reply_to FROM messages "
                    "WHERE message_id = ?",
                    (normalized_reply,),
                ).fetchone()
                if original is None:
                    raise NotFoundError(f"unknown reply_to message: {normalized_reply}")
                if str(original["conversation_id"]) != conversation:
                    raise ConflictError("reply_to belongs to a different conversation")
                if original["reply_to"] is not None:
                    raise ConflictError(
                        "reply chains are limited to one level; continue the "
                        "conversation with a new message"
                    )
                reply_restriction = conn.execute(
                    "SELECT target_kind FROM message_restrictions "
                    "WHERE message_id = ?",
                    (normalized_reply,),
                ).fetchone()
                if reply_restriction is not None:
                    inherited_restriction_target_kind = str(
                        reply_restriction["target_kind"]
                    )
                    inherited_restriction_recipients = [
                        str(row["participant_id"])
                        for row in conn.execute(
                            "SELECT participant_id "
                            "FROM message_restriction_recipients "
                            "WHERE message_id = ? ORDER BY participant_id",
                            (normalized_reply,),
                        ).fetchall()
                    ]
                    if (
                        not _owner_ui
                        and not _web_user
                        and sender not in inherited_restriction_recipients
                    ):
                        raise AuthorizationError(
                            "reply target is not visible to this Agent"
                        )
                    if _task_request is not None:
                        unauthorized_task_targets = sorted(
                            set(task_target_ids)
                            - set(inherited_restriction_recipients)
                        )
                        if unauthorized_task_targets:
                            raise AuthorizationError(
                                "a task reply cannot expand the fixed recipients "
                                "of a restricted message"
                            )
            if (
                not _owner_ui
                and not _web_user
                and normalized_message_kind == "message"
                and self._is_direct_agent_reply_request(normalized_body)
            ):
                is_review_request = self._is_direct_review_request(normalized_body)
                review_targets = list(normalized_mentions)
                review_source = "structured_or_visible_mention"
                if normalized_audience in {"participant", "role"}:
                    # The audience itself is already an explicit routing
                    # decision.  Participant audiences have been folded into
                    # mentions above; role messages use their claimable role
                    # delivery without manufacturing a personal @.
                    review_source = f"audience:{normalized_audience}"
                    if normalized_audience == "role":
                        review_targets = self._role_review_targets_locked(
                            conn,
                            conversation_id=conversation,
                            sender_participant_id=sender,
                            role=normalized_target,
                        )
                elif not review_targets:
                    review_targets = self._infer_named_review_targets_locked(
                        conn,
                        conversation_id=conversation,
                        sender_participant_id=sender,
                        body_text=normalized_body,
                    )
                    review_source = "named_member"
                if not review_targets and normalized_audience == "room":
                    reply_sender = self._reply_sender_locked(
                        conn,
                        conversation_id=conversation,
                        sender_participant_id=sender,
                        reply_to=normalized_reply,
                    )
                    if reply_sender is not None:
                        review_targets = [reply_sender]
                        review_source = "reply_author"
                if (
                    normalized_audience not in {"participant", "role"}
                    and not review_targets
                ):
                    warning_code = (
                        "review_or_confirmation_target_required"
                        if is_review_request
                        else "coordination_target_required"
                    )
                    coordination_routing = {
                        "requested": True,
                        "notified": False,
                        "source": "unresolved",
                        "target_participant_ids": [],
                        "warning": (
                            f"{warning_code}: no target was notified; immediately "
                            "resend with a same-room member's exact name, structured "
                            "mentions, reply_to, or a participant/role audience"
                        ),
                    }
                elif (
                    requested_notification_mode == "ordinary"
                    and normalized_audience == "room"
                    and normalized_reply is None
                ):
                    coordination_routing = {
                        "requested": True,
                        "notified": False,
                        "source": "explicit_ordinary",
                        "target_participant_ids": [],
                        "warning": (
                            "direct_request_sent_as_ordinary: an exact target name "
                            "was found, but notification_mode=ordinary explicitly "
                            "kept this in backlog; resend in mention mode if timely "
                            "attention is expected"
                        ),
                    }
                    review_targets = []
                if normalized_audience != "role" and review_targets:
                    for review_target in review_targets:
                        if review_target not in normalized_mentions:
                            normalized_mentions.append(review_target)
                if len(normalized_mentions) > MAX_MENTIONS_PER_MESSAGE:
                    raise ValidationError(
                        "mentions cannot contain more than "
                        f"{MAX_MENTIONS_PER_MESSAGE} entries"
                    )
                if coordination_routing is None:
                    coordination_routing = {
                        "requested": True,
                        "notified": True,
                        "source": review_source,
                        "target_participant_ids": sorted(set(review_targets)),
                    }
                if is_review_request:
                    review_routing = dict(coordination_routing)
            if normalized_forward:
                source = conn.execute(
                    "SELECT conversation_id, message_kind FROM messages "
                    "WHERE message_id = ?",
                    (normalized_forward,),
                ).fetchone()
                if source is None:
                    raise NotFoundError(
                        f"unknown forwarded source message: {normalized_forward}"
                    )
                if str(source["conversation_id"]) == conversation:
                    raise ConflictError("cross-room forward target must differ")
                if str(source["message_kind"]) == "forward":
                    raise ConflictError(
                        "forward chains are not allowed; forward the original message"
                    )
            has_notification_target = bool(
                normalized_mentions
                or normalized_wake_all
                or normalized_reply
                or normalized_audience in {"participant", "role"}
            )
            effective_notification_mode = (
                requested_notification_mode
                or ("mention" if has_notification_target else "ordinary")
            )
            if effective_notification_mode == "mention" and not has_notification_target:
                raise ValidationError(
                    "mention mode requires mentions, reply_to, or a participant/role "
                    "audience"
                )
            if effective_notification_mode == "ordinary" and has_notification_target:
                raise ValidationError(
                    "ordinary mode cannot include mentions, reply_to, @全员, or a "
                    "participant/role audience"
                )
            mention_routing = self._mention_routing_diagnostics_locked(
                conn,
                conversation_id=conversation,
                sender_participant_id=sender,
                body_text=normalized_body,
                mentioned_participant_ids=normalized_mentions,
                notification_mode=effective_notification_mode,
                wake_all_agents=normalized_wake_all,
                notification_targeted=has_notification_target,
            )
            self._assert_speaking_cooldown(
                conn,
                participant_id=sender,
                conversation_id=conversation,
                now=now,
                cooldown_seconds=cooldown_seconds,
            )
            try:
                conn.execute(
                    """
                    INSERT INTO messages
                        (message_id, conversation_id, sender_participant_id,
                         audience_kind, audience_value, message_kind, body,
                         refs_json, mentions_json, wake_all_agents, reply_to, status,
                         authorized_session_id, forwarded_from_message_id,
                         sender_seat, notification_mode, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        conversation,
                        sender,
                        normalized_audience,
                        normalized_target,
                        normalized_message_kind,
                        normalized_body,
                        compact_json(normalized_refs),
                        compact_json(normalized_mentions),
                        1 if normalized_wake_all else 0,
                        normalized_reply,
                        session,
                        normalized_forward,
                        sender_seat,
                        effective_notification_mode,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                error_text = str(exc)
                if "MESSAGE_RATE_LIMITED" in error_text:
                    self._assert_speaking_cooldown(
                        conn,
                        participant_id=sender,
                        conversation_id=conversation,
                        now=now,
                        cooldown_seconds=cooldown_seconds,
                    )
                if "REPLY_CHAIN_NOT_ALLOWED" in error_text:
                    raise ConflictError(
                        "reply chains are limited to one level; continue the "
                        "conversation with a new message"
                    ) from exc
                if (
                    "LIVE_MCP_SESSION_REQUIRED" in error_text
                    or "AUTHORIZED_SENDER_REQUIRED" in error_text
                ):
                    raise AuthenticationError(
                        "an authenticated Agent session or web user session is required "
                        "to chat"
                    ) from exc
                raise
            row = conn.execute(
                "SELECT * FROM messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            self._persist_message_assets_locked(
                conn,
                message_id=message_id,
                links=normalized_links,
                attachments=staged_attachments,
                conversation_id=conversation,
                sender_participant_id=sender,
                mentioned_participant_ids=normalized_mentions,
                wake_all_agents=normalized_wake_all,
                created_by_web_user_id=(
                    str(web_identity["user_id"])
                    if web_identity is not None
                    else None
                ),
                created_at=now,
                inherited_target_kind=inherited_restriction_target_kind,
                inherited_recipient_ids=inherited_restriction_recipients,
            )
            task_payload = None
            if _task_request is not None:
                conn.execute(
                    """
                    INSERT INTO room_tasks
                        (task_id, conversation_id, source_message_id,
                         parent_task_id, issuer_web_user_id,
                         issuer_participant_id, target_kind,
                         target_participant_ids_json, body, status,
                         source_sequence, context_start_sequence,
                         context_end_sequence,
                         created_at, updated_at)
                    VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        conversation,
                        message_id,
                        str(web_identity["user_id"]),
                        sender,
                        task_target_kind,
                        compact_json(task_target_ids),
                        normalized_body,
                        int(row["sequence"]),
                        max(1, int(row["sequence"]) - 20),
                        int(row["sequence"]),
                        now,
                        now,
                    ),
                )
                task_row = conn.execute(
                    "SELECT * FROM room_tasks WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                task_payload = self._task_payload(task_row)
            elif (
                _web_user
                and web_identity is not None
                and normalized_message_kind == "message"
                and normalized_mentions
            ):
                task_permissions = self._room_task_permissions_locked(
                    conn,
                    conversation_id=conversation,
                    web_identity=web_identity,
                )
                if task_permissions["can_assign_tasks"]:
                    body_routing = self._route_web_message_to_body_locked(
                        conn,
                        message=row,
                        issuer_web_user_id=str(web_identity["user_id"]),
                        mentioned_participant_ids=normalized_mentions,
                        now=now,
                    )
                    created_task_ids = [
                        str(route["task_id"])
                        for route in body_routing
                        if route["mode"] == "queued"
                    ]
                    if created_task_ids:
                        task_row = conn.execute(
                            "SELECT * FROM room_tasks WHERE task_id = ?",
                            (created_task_ids[0],),
                        ).fetchone()
                        task_payload = self._task_payload(task_row)
            if (
                _web_user
                and str(web_identity["role"]) == "admin"
                and not _suppress_chat_authorization
            ):
                self._insert_admin_chat_authorization_grant_locked(
                    conn,
                    message=row,
                    issuer_web_user_id=str(web_identity["user_id"]),
                    issuer_username=str(web_identity["username"]),
                    issuer_role=str(web_identity["role"]),
                )
            self._create_message_deliveries_locked(conn, row)
            if body_routing:
                routed_targets = sorted(
                    {
                        str(route["target_participant_id"])
                        for route in body_routing
                    }
                )
                placeholders = ",".join("?" for _ in routed_targets)
                conn.execute(
                    f"""
                    UPDATE message_deliveries
                    SET state = 'cancelled', delivery_stage = 'cancelled',
                        actionable = 0
                    WHERE message_id = ?
                      AND participant_id IN ({placeholders})
                      AND state IN ('pending', 'delivered')
                    """,
                    (message_id, *routed_targets),
                )
            payload = self._message_payload(
                row,
                authorization=self._chat_authorization_for_message_locked(
                    conn,
                    message_id=message_id,
                    recipient_participant_id=None,
                ),
            )
            payload.update(
                self._message_asset_projection_locked(conn, [message_id])[message_id]
            )
            if task_payload is not None:
                payload["task"] = task_payload
            if body_routing:
                payload["body_routing"] = body_routing
            if review_routing is not None:
                payload["review_routing"] = review_routing
            if coordination_routing is not None:
                payload["coordination_routing"] = coordination_routing
            if mention_routing is not None:
                payload["mention_routing"] = mention_routing
        return payload

    def send_owner_message(
        self,
        *,
        conversation_id: str,
        body_text: str,
        mentions: Sequence[str] | None = None,
        wake_all_agents: bool = False,
        reply_to: str | None = None,
        links: Sequence[str] | None = None,
        attachments: Sequence[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Send one owner-authored room message through the local web authority."""
        staged = self._stage_message_attachments(attachments)
        try:
            result = self.send(
                authorized_session_id=OWNER_AUTHORIZATION_ID,
                sender_participant_id=OWNER_PARTICIPANT_ID,
                conversation_id=conversation_id,
                body_text=body_text,
                audience_kind="room",
                audience_value="*",
                mentions=mentions,
                links=links,
                wake_all_agents=wake_all_agents,
                reply_to=reply_to,
                _owner_ui=True,
                _staged_attachments=staged,
            )
        except Exception:
            self._discard_staged_message_attachments(staged, include_final=True)
            raise
        self._discard_staged_message_attachments(staged, include_final=False)
        return result

    def send_web_message(
        self,
        *,
        authorized_session_id: str,
        participant_id: str,
        conversation_id: str,
        body_text: str,
        mentions: Sequence[str] | None = None,
        wake_all_agents: bool = False,
        reply_to: str | None = None,
        links: Sequence[str] | None = None,
        attachments: Sequence[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Send one authenticated web user's room message under its own identity."""
        staged = self._stage_message_attachments(attachments)
        try:
            result = self.send(
                authorized_session_id=authorized_session_id,
                sender_participant_id=participant_id,
                conversation_id=conversation_id,
                body_text=body_text,
                audience_kind="room",
                audience_value="*",
                mentions=mentions,
                links=links,
                wake_all_agents=wake_all_agents,
                reply_to=reply_to,
                _web_user=True,
                _staged_attachments=staged,
            )
        except Exception:
            self._discard_staged_message_attachments(staged, include_final=True)
            raise
        self._discard_staged_message_attachments(staged, include_final=False)
        return result

    @staticmethod
    def _room_wake_policy_payload(
        row: sqlite3.Row | None,
        *,
        conversation_id: str,
    ) -> dict[str, Any]:
        return {
            "conversation_id": conversation_id,
            "mode": str(row["mode"]) if row is not None else DEFAULT_ROOM_WAKE_MODE,
            "digest_min_messages": (
                int(row["digest_min_messages"])
                if row is not None
                else DEFAULT_ROOM_DIGEST_MIN_MESSAGES
            ),
            "digest_after_seconds": (
                float(row["digest_after_seconds"])
                if row is not None
                else float(DEFAULT_ROOM_DIGEST_AFTER_SECONDS)
            ),
            "updated_at": (
                float(row["updated_at"]) if row is not None else None
            ),
        }

    def room_wake_policies_bulk(
        self,
        *,
        conversation_ids: Sequence[str],
    ) -> dict[str, dict[str, Any]]:
        conversations = [
            validate_conversation_id(value) for value in conversation_ids
        ]
        if not conversations:
            return {}
        placeholders = ",".join("?" for _ in conversations)
        with self._connection() as conn:
            rows = {
                str(row["conversation_id"]): row
                for row in conn.execute(
                    f"SELECT * FROM room_wake_policies "
                    f"WHERE conversation_id IN ({placeholders})",
                    conversations,
                ).fetchall()
            }
        return {
            conversation: self._room_wake_policy_payload(
                rows.get(conversation),
                conversation_id=conversation,
            )
            for conversation in conversations
        }

    def room_wake_policy(
        self,
        *,
        authorized_session_id: str,
        participant_id: str,
        conversation_id: str,
    ) -> dict[str, Any]:
        session = opaque_id(authorized_session_id, field="authorized_session_id")
        participant = opaque_id(participant_id, field="participant_id")
        conversation = validate_conversation_id(conversation_id)
        with self._connection() as conn:
            identity = self._require_live_web_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=time.time(),
            )
            self._require_web_room_access_locked(
                conn,
                web_identity=identity,
                conversation_id=conversation,
            )
            row = conn.execute(
                "SELECT * FROM room_wake_policies WHERE conversation_id = ?",
                (conversation,),
            ).fetchone()
        return self._room_wake_policy_payload(row, conversation_id=conversation)

    def update_room_wake_policy(
        self,
        *,
        authorized_session_id: str,
        participant_id: str,
        conversation_id: str,
        mode: str,
        digest_min_messages: object = DEFAULT_ROOM_DIGEST_MIN_MESSAGES,
        digest_after_seconds: object = DEFAULT_ROOM_DIGEST_AFTER_SECONDS,
    ) -> dict[str, Any]:
        session = opaque_id(authorized_session_id, field="authorized_session_id")
        participant = opaque_id(participant_id, field="participant_id")
        conversation = validate_conversation_id(conversation_id)
        normalized_mode = str(mode or "").strip().lower()
        if normalized_mode not in ROOM_WAKE_MODES:
            raise ValidationError("room wake mode must be mention, digest, or all")
        if isinstance(digest_min_messages, bool):
            raise ValidationError("digest_min_messages must be an integer")
        try:
            minimum = int(digest_min_messages)
            after_seconds = float(digest_after_seconds)
        except (TypeError, ValueError) as exc:
            raise ValidationError("invalid room digest configuration") from exc
        if not 1 <= minimum <= 500:
            raise ValidationError("digest_min_messages must be between 1 and 500")
        if not math.isfinite(after_seconds) or not 30 <= after_seconds <= 86_400:
            raise ValidationError(
                "digest_after_seconds must be between 30 and 86400"
            )
        now = time.time()
        with self._transaction() as conn:
            identity = self._require_live_web_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=now,
            )
            self._require_web_room_access_locked(
                conn,
                web_identity=identity,
                conversation_id=conversation,
            )
            self._require_active_room(conn, conversation)
            room_permissions = self._room_web_permissions_locked(
                conn,
                web_user_id=str(identity["user_id"]),
                conversation_id=conversation,
            )
            if not room_permissions["can_manage_wake_policy"]:
                raise AuthorizationError(
                    "只有全局管理员、创建者或聊天室管理员可以调整唤醒策略"
                )
            conn.execute(
                """
                INSERT INTO room_wake_policies
                    (conversation_id, mode, digest_min_messages,
                     digest_after_seconds, updated_by_web_user_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    mode = excluded.mode,
                    digest_min_messages = excluded.digest_min_messages,
                    digest_after_seconds = excluded.digest_after_seconds,
                    updated_by_web_user_id = excluded.updated_by_web_user_id,
                    updated_at = excluded.updated_at
                """,
                (
                    conversation,
                    normalized_mode,
                    minimum,
                    after_seconds,
                    str(identity["user_id"]),
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM room_wake_policies WHERE conversation_id = ?",
                (conversation,),
            ).fetchone()
        return self._room_wake_policy_payload(row, conversation_id=conversation)

    def forward_web_message(
        self,
        *,
        authorized_session_id: str,
        participant_id: str,
        source_message_id: str,
        target_conversation_id: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Explicitly copy one message into another room with durable provenance."""

        session = opaque_id(authorized_session_id, field="authorized_session_id")
        participant = opaque_id(participant_id, field="participant_id")
        source_id = opaque_id(source_message_id, field="source_message_id")
        target = validate_conversation_id(target_conversation_id)
        normalized_note = str(note or "").strip()
        if len(normalized_note) > 2_000 or any(
            ord(character) < 32 and character not in "\t\n\r"
            for character in normalized_note
        ):
            raise ValidationError("forward note must contain at most 2000 characters")
        with self._connection() as conn:
            web_identity = self._require_live_web_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=time.time(),
            )
            if str(web_identity["role"]) != "admin":
                raise AuthorizationError("只有管理员可以跨聊天室转发消息")
            source = conn.execute(
                """
                SELECT message.*, sender.display_name AS sender_display_name,
                       sender.client_type AS sender_client_type
                FROM messages AS message
                JOIN participants AS sender
                  ON sender.participant_id = message.sender_participant_id
                WHERE message.message_id = ?
                """,
                (source_id,),
            ).fetchone()
            if source is None:
                raise NotFoundError(f"unknown source message: {source_id}")
            if conn.execute(
                "SELECT 1 FROM message_restrictions WHERE message_id = ?",
                (source_id,),
            ).fetchone() is not None:
                raise AuthorizationError(
                    "包含文件或图片的定向消息不能跨聊天室转发"
                )
            source_links = [
                str(row["url"])
                for row in conn.execute(
                    "SELECT url FROM message_links WHERE message_id = ? "
                    "ORDER BY position",
                    (source_id,),
                ).fetchall()
            ]
            if str(source["message_kind"]) == "forward":
                raise ConflictError(
                    "forward chains are not allowed; forward the original message"
                )
            if str(source["conversation_id"]) == target:
                raise ConflictError("cross-room forward target must differ")
            source_label = (
                str(source["sender_display_name"])
                or str(source["sender_client_type"])
            )
            header = (
                "【管理员显式转发 · 来源「"
                f"{source['conversation_id']}」#"
                f"{int(source['room_sequence'] or source['sequence'])} · "
                f"{source_label}】"
            )
            sections = [header]
            if normalized_note:
                sections.append(f"转发说明：{normalized_note}")
            sections.append(f"原文：\n{source['body']}")
            forwarded_body = "\n\n".join(sections)
            # Validate before entering send's write transaction, including the
            # small provenance header added around the immutable source body.
            forwarded_body = body(forwarded_body)
        return self.send(
            authorized_session_id=session,
            sender_participant_id=participant,
            conversation_id=target,
            body_text=forwarded_body,
            audience_kind="room",
            audience_value="*",
            _web_user=True,
            _message_kind="forward",
            _forwarded_from_message_id=source_id,
            _suppress_chat_authorization=True,
            _suppress_mention_inference=True,
            links=source_links,
        )
