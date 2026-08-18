"""Agent profile, avatar, nickname, follow, and room DND state."""

from __future__ import annotations

import time
import uuid
from typing import Any

from .avatars import AGENT_AVATAR_CHANGE_COOLDOWN_SECONDS, normalize_avatar_key
from .store_constants import NICKNAME_REQUEST_COOLDOWN_SECONDS
from .store_errors import (
    AuthenticationError,
    AvatarRateLimitError,
    ConflictError,
    NicknameRateLimitError,
    NotFoundError,
)
from .validation import (
    ValidationError,
    alias,
    conversation_id as validate_conversation_id,
    display_name as validate_display_name,
    opaque_id,
)


PROFILE_SCHEMA = """
CREATE TABLE IF NOT EXISTS nickname_requests (
    request_id TEXT PRIMARY KEY,
    participant_id TEXT NOT NULL,
    requested_display_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected')),
    requested_at REAL NOT NULL,
    requested_session_id TEXT NOT NULL,
    reviewed_at REAL,
    review_note TEXT,
    reviewed_by_web_user_id TEXT,
    FOREIGN KEY (participant_id) REFERENCES participants(participant_id),
    FOREIGN KEY (requested_session_id) REFERENCES agent_sessions(session_id),
    FOREIGN KEY (reviewed_by_web_user_id) REFERENCES web_users(user_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_participants_display_name_unique
    ON participants(display_name COLLATE NOCASE);
CREATE UNIQUE INDEX IF NOT EXISTS idx_nickname_requests_one_pending
    ON nickname_requests(participant_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_nickname_requests_status_requested
    ON nickname_requests(status, requested_at DESC);
CREATE INDEX IF NOT EXISTS idx_nickname_requests_participant_requested
    ON nickname_requests(participant_id, requested_at DESC);

DROP TRIGGER IF EXISTS trg_participants_display_name_requires_pending_request;
CREATE TRIGGER trg_participants_display_name_requires_pending_request
BEFORE UPDATE OF display_name ON participants
WHEN NEW.display_name != OLD.display_name
AND NOT EXISTS (
    SELECT 1 FROM web_users
    WHERE participant_id = OLD.participant_id AND active = 1
)
AND NOT EXISTS (
        SELECT 1 FROM nickname_requests
        WHERE participant_id = OLD.participant_id
          AND requested_display_name = NEW.display_name
          AND status = 'pending'
    )
BEGIN
    SELECT RAISE(ABORT, 'NICKNAME_APPROVAL_REQUIRED');
END;
"""


class ParticipantProfileMixin:
    def update_profile(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        signature: object | None = None,
        avatar_key: object | None = None,
    ) -> dict[str, Any]:
        participant = opaque_id(participant_id, field="participant_id")
        session = opaque_id(authorized_session_id, field="authorized_session_id")
        if signature is None and avatar_key is None:
            raise ValidationError("signature or avatar_key is required")
        normalized_signature = (
            alias(signature, field="signature")
            if signature is not None
            else None
        )
        normalized_avatar = (
            normalize_avatar_key(avatar_key)
            if avatar_key is not None
            else None
        )
        now = time.time()
        with self._transaction() as conn:
            self._require_live_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=now,
            )
            current = conn.execute(
                "SELECT * FROM participants WHERE participant_id = ?",
                (participant,),
            ).fetchone()
            if current is None:
                raise NotFoundError(f"unknown participant: {participant}")
            next_avatar_changed_at = (
                self._next_avatar_changed_at(
                    current_avatar=str(current["avatar_key"] or "auto"),
                    current_changed_at=current["avatar_changed_at"],
                    next_avatar=normalized_avatar,
                    now=now,
                )
                if normalized_avatar is not None
                else current["avatar_changed_at"]
            )
            updated = conn.execute(
                "UPDATE participants SET signature = COALESCE(?, signature), "
                "avatar_key = COALESCE(?, avatar_key), avatar_changed_at = ?, "
                "profile_updated_at = ?, "
                "last_seen = ? "
                "WHERE participant_id = ?",
                (
                    normalized_signature,
                    normalized_avatar,
                    next_avatar_changed_at,
                    now,
                    now,
                    participant,
                ),
            ).rowcount
            if not updated:
                raise NotFoundError(f"unknown participant: {participant}")
            row = conn.execute(
                "SELECT * FROM participants WHERE participant_id = ?",
                (participant,),
            ).fetchone()
        return self._participant_profile_payload(row)

    @staticmethod
    def _next_avatar_changed_at(
        *,
        current_avatar: str,
        current_changed_at: object | None,
        next_avatar: str,
        now: float,
    ) -> float | None:
        changed_at = (
            float(current_changed_at)
            if current_changed_at is not None
            else None
        )
        if next_avatar == current_avatar:
            return changed_at
        if changed_at is not None:
            retry_after = (
                changed_at + AGENT_AVATAR_CHANGE_COOLDOWN_SECONDS - now
            )
            if retry_after > 0:
                raise AvatarRateLimitError(retry_after_seconds=retry_after)
        # Picking an avatar for an identity still using ``auto`` is
        # initialization, not its first daily change. Every later different
        # selection starts a rolling 24-hour cooldown.
        if current_avatar == "auto" and changed_at is None:
            return None
        return now

    def request_nickname(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        requested_display_name: str,
    ) -> dict[str, Any]:
        participant = opaque_id(participant_id, field="participant_id")
        session = opaque_id(authorized_session_id, field="authorized_session_id")
        requested = validate_display_name(requested_display_name)
        now = time.time()
        with self._transaction() as conn:
            self._require_live_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=now,
            )
            profile = conn.execute(
                "SELECT display_name FROM participants WHERE participant_id = ?",
                (participant,),
            ).fetchone()
            if profile is None:
                raise NotFoundError(f"unknown participant: {participant}")
            if str(profile["display_name"]).casefold() == requested.casefold():
                raise ConflictError("requested nickname is already active")
            collision = conn.execute(
                "SELECT participant_id FROM participants "
                "WHERE display_name = ? COLLATE NOCASE AND participant_id != ?",
                (requested, participant),
            ).fetchone()
            if collision is not None:
                raise ConflictError("requested nickname is already in use")
            pending = conn.execute(
                "SELECT request_id FROM nickname_requests "
                "WHERE participant_id = ? AND status = 'pending'",
                (participant,),
            ).fetchone()
            if pending is not None:
                raise ConflictError(
                    f"nickname request {pending['request_id']} is still pending"
                )
            latest = conn.execute(
                "SELECT requested_at FROM nickname_requests "
                "WHERE participant_id = ? ORDER BY requested_at DESC LIMIT 1",
                (participant,),
            ).fetchone()
            if latest is not None:
                retry_after = (
                    float(latest["requested_at"])
                    + NICKNAME_REQUEST_COOLDOWN_SECONDS
                    - now
                )
                if retry_after > 0:
                    raise NicknameRateLimitError(
                        retry_after_seconds=retry_after,
                    )
            request_id = f"nickname_{uuid.uuid4().hex}"
            conn.execute(
                """
                INSERT INTO nickname_requests
                    (request_id, participant_id, requested_display_name,
                     status, requested_at, requested_session_id)
                VALUES (?, ?, ?, 'pending', ?, ?)
                """,
                (request_id, participant, requested, now, session),
            )
            row = conn.execute(
                "SELECT request.*, profile.client_type, profile.display_name, "
                "profile.signature FROM nickname_requests AS request "
                "JOIN participants AS profile "
                "ON profile.participant_id = request.participant_id "
                "WHERE request.request_id = ?",
                (request_id,),
            ).fetchone()
        return self._nickname_request_payload(row)

    def list_nickname_requests(
        self,
        *,
        status: str | None = "pending",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        normalized_status = str(status or "").strip().lower()
        if normalized_status and normalized_status not in {
            "pending",
            "approved",
            "rejected",
        }:
            raise ValidationError("status must be pending, approved, or rejected")
        normalized_limit = max(1, min(int(limit), 500))
        where_clause = "WHERE request.status = ?" if normalized_status else ""
        parameters: tuple[Any, ...] = (
            (normalized_status, normalized_limit)
            if normalized_status
            else (normalized_limit,)
        )
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT request.*, profile.client_type, profile.display_name,
                       profile.signature
                FROM nickname_requests AS request
                JOIN participants AS profile
                  ON profile.participant_id = request.participant_id
                {where_clause}
                ORDER BY request.requested_at DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [self._nickname_request_payload(row) for row in rows]

    def review_nickname_request(
        self,
        *,
        request_id: str,
        action: str,
        review_note: str | None = None,
        reviewed_by_web_user_id: str | None = None,
    ) -> dict[str, Any]:
        request = opaque_id(request_id, field="request_id")
        normalized_action = str(action or "").strip().lower()
        if normalized_action not in {"approve", "reject"}:
            raise ValidationError("action must be approve or reject")
        note = alias(review_note, field="review_note") if review_note else None
        reviewer = (
            opaque_id(reviewed_by_web_user_id, field="reviewed_by_web_user_id")
            if reviewed_by_web_user_id
            else None
        )
        now = time.time()
        status = "approved" if normalized_action == "approve" else "rejected"
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM nickname_requests WHERE request_id = ?",
                (request,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"unknown nickname request: {request}")
            if str(row["status"]) != "pending":
                raise ConflictError(
                    f"nickname request is already {row['status']}"
                )
            if status == "approved":
                requested = str(row["requested_display_name"])
                collision = conn.execute(
                    "SELECT participant_id FROM participants "
                    "WHERE display_name = ? COLLATE NOCASE AND participant_id != ?",
                    (requested, str(row["participant_id"])),
                ).fetchone()
                if collision is not None:
                    raise ConflictError("requested nickname is already in use")
                conn.execute(
                    "UPDATE participants SET display_name = ?, profile_updated_at = ? "
                    "WHERE participant_id = ?",
                    (requested, now, str(row["participant_id"])),
                )
            if reviewer is not None:
                administrator = conn.execute(
                    "SELECT user_id FROM web_users WHERE user_id = ? "
                    "AND role = 'admin' AND active = 1",
                    (reviewer,),
                ).fetchone()
                if administrator is None:
                    raise AuthenticationError(
                        "an active administrator is required to review nicknames"
                    )
            conn.execute(
                "UPDATE nickname_requests SET status = ?, reviewed_at = ?, "
                "review_note = ?, reviewed_by_web_user_id = ? WHERE request_id = ?",
                (status, now, note, reviewer, request),
            )
            reviewed = conn.execute(
                "SELECT request.*, profile.client_type, profile.display_name, "
                "profile.signature FROM nickname_requests AS request "
                "JOIN participants AS profile "
                "ON profile.participant_id = request.participant_id "
                "WHERE request.request_id = ?",
                (request,),
            ).fetchone()
        return self._nickname_request_payload(reviewed)

    def set_follow(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        conversation_id: str,
        followed_participant_id: str,
        following: bool = True,
    ) -> dict[str, Any]:
        follower = opaque_id(participant_id, field="participant_id")
        followed = opaque_id(
            followed_participant_id,
            field="followed_participant_id",
        )
        if follower == followed:
            raise ConflictError("an Agent cannot follow itself")
        session = opaque_id(authorized_session_id, field="authorized_session_id")
        conversation = validate_conversation_id(conversation_id)
        now = time.time()
        with self._transaction() as conn:
            self._require_live_room_session(
                conn,
                session_id=session,
                participant_id=follower,
                conversation_id=conversation,
                now=now,
            )
            self._require_membership(conn, follower, conversation)
            self._require_membership(conn, followed, conversation)
            conn.execute(
                """
                INSERT INTO follows
                    (conversation_id, follower_participant_id,
                     followed_participant_id, active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    conversation_id,
                    follower_participant_id,
                    followed_participant_id
                ) DO UPDATE SET
                    active = excluded.active,
                    updated_at = excluded.updated_at
                """,
                (
                    conversation,
                    follower,
                    followed,
                    1 if following else 0,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT follow.*, profile.client_type, profile.display_name,
                       profile.signature
                FROM follows AS follow
                JOIN participants AS profile
                  ON profile.participant_id = follow.followed_participant_id
                WHERE follow.conversation_id = ?
                  AND follow.follower_participant_id = ?
                  AND follow.followed_participant_id = ?
                """,
                (conversation, follower, followed),
            ).fetchone()
        return self._follow_payload(row)

    def following(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        conversation_id: str,
        include_inactive: bool = False,
    ) -> dict[str, Any]:
        follower = opaque_id(participant_id, field="participant_id")
        session = opaque_id(authorized_session_id, field="authorized_session_id")
        conversation = validate_conversation_id(conversation_id)
        with self._connection() as conn:
            now = time.time()
            self._require_live_room_session(
                conn,
                session_id=session,
                participant_id=follower,
                conversation_id=conversation,
                now=now,
            )
            self._require_membership(conn, follower, conversation)
            active_filter = "" if include_inactive else "AND follow.active = 1"
            rows = conn.execute(
                """
                SELECT follow.*, profile.client_type, profile.display_name,
                       profile.signature
                FROM follows AS follow
                JOIN participants AS profile
                  ON profile.participant_id = follow.followed_participant_id
                WHERE follow.conversation_id = ?
                  AND follow.follower_participant_id = ?
                """
                f" {active_filter} "
                "ORDER BY profile.display_name, follow.followed_participant_id",
                (conversation, follower),
            ).fetchall()
        follows = [self._follow_payload(row) for row in rows]
        return {
            "conversation_id": conversation,
            "participant_id": follower,
            "following": follows,
            "count": len(follows),
        }

    def set_room_dnd(
        self,
        *,
        participant_id: str,
        authorized_session_id: str,
        conversation_id: str,
        enabled: bool = True,
        _now: float | None = None,
    ) -> dict[str, Any]:
        """Suppress only digest wakes in one room until the next local midnight."""

        participant = opaque_id(participant_id, field="participant_id")
        session = opaque_id(
            authorized_session_id,
            field="authorized_session_id",
        )
        conversation = validate_conversation_id(conversation_id)
        if not isinstance(enabled, bool):
            raise ValidationError("enabled must be a boolean")
        now = float(time.time() if _now is None else _now)
        with self._transaction() as conn:
            self._require_live_room_session(
                conn,
                session_id=session,
                participant_id=participant,
                conversation_id=conversation,
                now=now,
            )
            self._require_membership(conn, participant, conversation)
            if enabled:
                expires_at = self._next_business_midnight(now)
                conn.execute(
                    """
                    INSERT INTO agent_room_dnd
                        (participant_id, conversation_id, enabled_at,
                         expires_at, timezone_name, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(participant_id, conversation_id) DO UPDATE SET
                        enabled_at = excluded.enabled_at,
                        expires_at = excluded.expires_at,
                        timezone_name = excluded.timezone_name,
                        updated_at = excluded.updated_at
                    """,
                    (
                        participant,
                        conversation,
                        now,
                        expires_at,
                        self.business_timezone_name,
                        now,
                    ),
                )
            else:
                conn.execute(
                    "DELETE FROM agent_room_dnd "
                    "WHERE participant_id = ? AND conversation_id = ?",
                    (participant, conversation),
                )
            row = conn.execute(
                "SELECT * FROM agent_room_dnd "
                "WHERE participant_id = ? AND conversation_id = ?",
                (participant, conversation),
            ).fetchone()
        active = row is not None and float(row["expires_at"]) > now
        return {
            "participant_id": participant,
            "conversation_id": conversation,
            "active": active,
            "enabled_at": float(row["enabled_at"]) if active else None,
            "expires_at": float(row["expires_at"]) if active else None,
            "timezone": (
                str(row["timezone_name"])
                if active
                else self.business_timezone_name
            ),
            "digest_wake_suppressed": active,
            "direct_notifications_optional": active,
        }
