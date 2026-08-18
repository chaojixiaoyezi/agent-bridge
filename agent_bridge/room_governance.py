"""Room creation, membership governance, access, and knowledge markers."""

from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Sequence
from typing import Any

from .store_constants import (
    AGENT_ACTIVE_ROOM_LIMIT,
    OWNER_CLIENT_TYPE,
    OWNER_PARTICIPANT_ID,
    OWNER_SESSION_ALIAS,
    ROOM_ABANDON_AFTER_SECONDS,
    ROOM_MESSAGE_MARKER_KINDS,
)
from .store_errors import (
    AuthenticationError,
    AuthorizationError,
    BridgeError,
    ConflictError,
    NotFoundError,
)
from .validation import (
    ValidationError,
    alias,
    client_identity,
    compact_json,
    conversation_id as validate_conversation_id,
    opaque_id,
    string_tokens,
)
from .web_auth import MAX_WEB_USER_ROOM_LIMIT


ROOM_KNOWLEDGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS room_message_markers (
    conversation_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    marker_kind TEXT NOT NULL CHECK (marker_kind IN ('pin', 'decision')),
    note TEXT NOT NULL DEFAULT '',
    created_by_web_user_id TEXT NOT NULL,
    updated_by_web_user_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (conversation_id, message_id, marker_kind),
    FOREIGN KEY (conversation_id) REFERENCES rooms(conversation_id),
    FOREIGN KEY (message_id) REFERENCES messages(message_id),
    FOREIGN KEY (created_by_web_user_id) REFERENCES web_users(user_id),
    FOREIGN KEY (updated_by_web_user_id) REFERENCES web_users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_room_message_markers_room_kind_updated
    ON room_message_markers(
        conversation_id, marker_kind, updated_at DESC, message_id
    );
"""


ROOM_GOVERNANCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS room_web_owners (
    conversation_id TEXT PRIMARY KEY,
    web_user_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES rooms(conversation_id),
    FOREIGN KEY (web_user_id) REFERENCES web_users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_room_web_owners_user
    ON room_web_owners(web_user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS room_web_members (
    conversation_id TEXT NOT NULL,
    web_user_id TEXT NOT NULL,
    access_role TEXT NOT NULL DEFAULT 'member'
        CHECK (access_role IN ('member', 'moderator')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    invited_by_web_user_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (conversation_id, web_user_id),
    FOREIGN KEY (conversation_id) REFERENCES rooms(conversation_id),
    FOREIGN KEY (web_user_id) REFERENCES web_users(user_id),
    FOREIGN KEY (invited_by_web_user_id) REFERENCES web_users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_room_web_members_user_active
    ON room_web_members(web_user_id, active, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_room_web_members_room_active
    ON room_web_members(conversation_id, active, access_role, updated_at DESC);
"""


class RoomGovernanceMixin:
    @staticmethod
    def _ensure_owner_membership_locked(
        conn: sqlite3.Connection,
        *,
        conversation_id: str,
        now: float,
    ) -> None:
        conn.execute(
            """
            INSERT INTO participants
                (participant_id, client_type, session_alias, display_name, signature,
                 profile_updated_at, capabilities_json, status, created_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, '[]', 'online', ?, ?)
            ON CONFLICT(participant_id) DO UPDATE SET
                status = 'online',
                last_seen = excluded.last_seen
            """,
            (
                OWNER_PARTICIPANT_ID,
                OWNER_CLIENT_TYPE,
                OWNER_SESSION_ALIAS,
                OWNER_SESSION_ALIAS,
                OWNER_SESSION_ALIAS,
                float(now),
                float(now),
                float(now),
            ),
        )
        conn.execute(
            """
            INSERT INTO memberships
                (conversation_id, participant_id, roles_json, active,
                 joined_at, updated_at)
            VALUES (?, ?, '["owner"]', 1, ?, ?)
            ON CONFLICT(conversation_id, participant_id) DO UPDATE SET
                roles_json = excluded.roles_json,
                active = 1,
                updated_at = excluded.updated_at
            """,
            (
                conversation_id,
                OWNER_PARTICIPANT_ID,
                float(now),
                float(now),
            ),
        )

    @staticmethod
    def _ensure_web_membership_locked(
        conn: sqlite3.Connection,
        *,
        conversation_id: str,
        participant_id: str,
        display_name: str,
        signature: str,
        role: str,
        now: float,
    ) -> None:
        participant = conn.execute(
            "SELECT participant_id FROM participants WHERE participant_id = ?",
            (participant_id,),
        ).fetchone()
        if participant is None:
            raise AuthenticationError("web user participant identity is missing")
        conn.execute(
            "UPDATE participants SET display_name = ?, signature = ?, "
            "profile_updated_at = ?, status = 'online', last_seen = ? "
            "WHERE participant_id = ?",
            (display_name, signature, now, now, participant_id),
        )
        membership_role = "admin" if role == "admin" else "web-user"
        conn.execute(
            """
            INSERT INTO memberships
                (conversation_id, participant_id, roles_json, active,
                 joined_at, updated_at)
            VALUES (?, ?, ?, 1, ?, ?)
            ON CONFLICT(conversation_id, participant_id) DO UPDATE SET
                roles_json = excluded.roles_json,
                active = 1,
                updated_at = excluded.updated_at
            """,
            (
                conversation_id,
                participant_id,
                compact_json([membership_role]),
                now,
                now,
            ),
        )

    @staticmethod
    def _archive_stale_rooms_locked(
        conn: sqlite3.Connection,
        *,
        now: float,
    ) -> list[str]:
        cutoff = float(now) - ROOM_ABANDON_AFTER_SECONDS
        rows = conn.execute(
            """
            SELECT conversation_id
            FROM rooms
            WHERE status = 'active' AND last_activity_at <= ?
            ORDER BY conversation_id
            """,
            (cutoff,),
        ).fetchall()
        conversation_ids = [str(row["conversation_id"]) for row in rows]
        if not conversation_ids:
            return []
        conn.execute(
            """
            UPDATE rooms
            SET status = 'abandoned', abandoned_at = ?
            WHERE status = 'active' AND last_activity_at <= ?
            """,
            (float(now), cutoff),
        )
        conn.execute(
            """
            UPDATE memberships
            SET active = 0, updated_at = ?
            WHERE active = 1 AND conversation_id IN (
                SELECT conversation_id FROM rooms
                WHERE status = 'abandoned' AND abandoned_at = ?
            )
            """,
            (float(now), float(now)),
        )
        return conversation_ids

    def register(
        self,
        *,
        client_type: str,
        session_alias: str,
        conversation_id: str,
        roles: Sequence[str] | None = None,
        capabilities: Sequence[str] | None = None,
        resume_participant_id: str | None = None,
        create_room_if_missing: bool = False,
    ) -> dict[str, Any]:
        normalized_client = client_identity(client_type)
        normalized_alias = alias(session_alias)
        normalized_conversation = validate_conversation_id(conversation_id)
        normalized_roles = string_tokens(roles, field="roles")
        normalized_capabilities = string_tokens(capabilities, field="capabilities")
        now = time.time()

        with self._transaction() as conn:
            self._archive_stale_rooms_locked(conn, now=now)
            if resume_participant_id:
                participant_id = opaque_id(
                    resume_participant_id,
                    field="resume_participant_id",
                )
                existing = conn.execute(
                    "SELECT * FROM participants WHERE participant_id = ?",
                    (participant_id,),
                ).fetchone()
                if existing is None:
                    raise NotFoundError(f"unknown participant: {participant_id}")
                if str(existing["client_type"]) != normalized_client:
                    raise ConflictError("participant client_type does not match")
                if str(existing["session_alias"]) != normalized_alias:
                    raise ConflictError(
                        "session_alias is immutable and must match the original "
                        "registration"
                    )
                conn.execute(
                    """
                    UPDATE participants
                    SET capabilities_json = ?, profile_updated_at = ?,
                        status = 'online', last_seen = ?
                    WHERE participant_id = ?
                    """,
                    (
                        compact_json(normalized_capabilities),
                        now,
                        now,
                        participant_id,
                    ),
                )
            else:
                duplicate = conn.execute(
                    "SELECT participant_id FROM participants WHERE client_type = ?",
                    (normalized_client,),
                ).fetchone()
                if duplicate is not None:
                    raise ConflictError(
                        "client identity is already registered as "
                        f"{duplicate['participant_id']}; choose another username or "
                        "resume that participant"
                    )
                participant_id = f"participant_{uuid.uuid4().hex}"
                conn.execute(
                    """
                    INSERT INTO participants
                        (participant_id, client_type, session_alias,
                         display_name, signature, profile_updated_at,
                         capabilities_json, status, created_at, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'online', ?, ?)
                    """,
                    (
                        participant_id,
                        normalized_client,
                        normalized_alias,
                        normalized_client,
                        normalized_alias,
                        now,
                        compact_json(normalized_capabilities),
                        now,
                        now,
                    ),
                )

            room = conn.execute(
                "SELECT * FROM rooms WHERE conversation_id = ?",
                (normalized_conversation,),
            ).fetchone()
            room_created = False
            if room is None:
                if not create_room_if_missing:
                    raise NotFoundError(
                        f"unknown conversation: {normalized_conversation}; "
                        "set create_room_if_missing=true to create it"
                    )
                owned_count = self._agent_active_room_count(conn, participant_id)
                if owned_count >= AGENT_ACTIVE_ROOM_LIMIT:
                    raise ConflictError(
                        "this agent session already owns the maximum of "
                        f"{AGENT_ACTIVE_ROOM_LIMIT} active rooms"
                    )
                conn.execute(
                    """
                    INSERT INTO rooms
                        (conversation_id, status, creator_kind,
                         creator_participant_id, created_at, last_activity_at)
                    VALUES (?, 'active', 'agent', ?, ?, ?)
                    """,
                    (normalized_conversation, participant_id, now, now),
                )
                room_created = True
            elif str(room["status"]) != "active":
                raise ConflictError(
                    f"conversation {normalized_conversation} is abandoned and "
                    "cannot be joined"
                )

            conn.execute(
                """
                INSERT INTO memberships
                    (conversation_id, participant_id, roles_json, active,
                     joined_at, updated_at)
                VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(conversation_id, participant_id) DO UPDATE SET
                    roles_json = excluded.roles_json,
                    active = 1,
                    updated_at = excluded.updated_at
                """,
                (
                    normalized_conversation,
                    participant_id,
                    compact_json(normalized_roles),
                    now,
                    now,
                ),
            )
            owned_count = self._agent_active_room_count(conn, participant_id)
            profile = conn.execute(
                "SELECT display_name, signature FROM participants "
                "WHERE participant_id = ?",
                (participant_id,),
            ).fetchone()

        return {
            "participant_id": participant_id,
            "client_type": normalized_client,
            "session_alias": normalized_alias,
            "display_name": str(profile["display_name"]),
            "signature": str(profile["signature"]),
            "conversation_id": normalized_conversation,
            "roles": normalized_roles,
            "capabilities": normalized_capabilities,
            "status": "online",
            "room_created": room_created,
            "owned_active_room_count": owned_count,
            "owned_active_room_limit": AGENT_ACTIVE_ROOM_LIMIT,
        }

    def create_user_room(self, conversation_id: str) -> dict[str, Any]:
        """Create an owner-managed room without consuming an agent quota."""
        conversation = validate_conversation_id(conversation_id)
        now = time.time()
        with self._transaction() as conn:
            self._archive_stale_rooms_locked(conn, now=now)
            existing = conn.execute(
                "SELECT status FROM rooms WHERE conversation_id = ?",
                (conversation,),
            ).fetchone()
            if existing is not None:
                state = str(existing["status"])
                raise ConflictError(
                    f"conversation {conversation} already exists with status {state}"
                )
            conn.execute(
                """
                INSERT INTO rooms
                    (conversation_id, status, creator_kind,
                     creator_participant_id, created_at, last_activity_at)
                VALUES (?, 'active', 'user', NULL, ?, ?)
                """,
                (conversation, now, now),
            )
        return {
            "conversation_id": conversation,
            "status": "active",
            "creator_kind": "user",
            "created_at": now,
            "last_activity_at": now,
        }

    def create_web_user_room(
        self,
        *,
        authorized_session_id: str,
        web_user_id: str,
        participant_id: str,
        conversation_id: str,
    ) -> dict[str, Any]:
        """Create a Web-owned room under an authenticated account permission."""

        session_id = opaque_id(
            authorized_session_id,
            field="authorized_session_id",
        )
        user_id = opaque_id(web_user_id, field="web_user_id")
        participant = opaque_id(participant_id, field="participant_id")
        conversation = validate_conversation_id(conversation_id)
        now = time.time()
        with self._transaction() as conn:
            self._archive_stale_rooms_locked(conn, now=now)
            identity = self._require_live_web_session(
                conn,
                session_id=session_id,
                participant_id=participant,
                now=now,
            )
            if str(identity["user_id"]) != user_id:
                raise AuthenticationError("web user session identity does not match")
            is_admin = str(identity["role"]) == "admin"
            can_create = is_admin or bool(identity["can_create_rooms"])
            if not can_create:
                raise AuthorizationError("管理员尚未授予你创建聊天室的权限")
            existing = conn.execute(
                "SELECT status FROM rooms WHERE conversation_id = ?",
                (conversation,),
            ).fetchone()
            if existing is not None:
                raise ConflictError(
                    f"conversation {conversation} already exists with status "
                    f"{existing['status']}"
                )
            owned_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM room_web_owners AS ownership "
                    "JOIN rooms AS room "
                    "ON room.conversation_id = ownership.conversation_id "
                    "WHERE ownership.web_user_id = ? AND room.status = 'active'",
                    (user_id,),
                ).fetchone()[0]
            )
            room_limit = int(identity["room_limit"])
            if not is_admin and owned_count >= room_limit:
                raise ConflictError(
                    "this web user already owns the maximum of "
                    f"{room_limit} active rooms"
                )
            conn.execute(
                "INSERT INTO rooms "
                "(conversation_id, status, creator_kind, creator_participant_id, "
                "created_at, last_activity_at) "
                "VALUES (?, 'active', 'user', NULL, ?, ?)",
                (conversation, now, now),
            )
            conn.execute(
                "INSERT INTO room_web_owners "
                "(conversation_id, web_user_id, created_at) VALUES (?, ?, ?)",
                (conversation, user_id, now),
            )
            conn.execute(
                "INSERT INTO room_web_members "
                "(conversation_id, web_user_id, access_role, active, "
                "invited_by_web_user_id, created_at, updated_at) "
                "VALUES (?, ?, 'member', 1, ?, ?, ?)",
                (conversation, user_id, user_id, now, now),
            )
            self._ensure_web_membership_locked(
                conn,
                conversation_id=conversation,
                participant_id=participant,
                display_name=str(identity["display_name"]),
                signature=str(identity["signature"]),
                role=str(identity["role"]),
                now=now,
            )
            owned_count += 1
        return {
            "conversation_id": conversation,
            "status": "active",
            "creator_kind": "user",
            "owner_web_user_id": user_id,
            "creator_participant_id": participant,
            "created_at": now,
            "last_activity_at": now,
            "owned_active_room_count": owned_count,
            "owned_active_room_limit": None if is_admin else room_limit,
            "is_room_owner": True,
        }

    def web_room_access_scope(
        self,
        *,
        authorized_session_id: str,
        participant_id: str,
    ) -> dict[str, Any]:
        """Return the authoritative Web room scope for one live session."""

        session = opaque_id(authorized_session_id, field="authorized_session_id")
        participant = opaque_id(participant_id, field="participant_id")
        with self._connection() as conn:
            identity = self._require_live_web_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=time.time(),
            )
            is_admin = str(identity["role"]) == "admin"
            if is_admin:
                conversations = None
            else:
                rows = conn.execute(
                    """
                    SELECT conversation_id
                    FROM room_web_owners
                    WHERE web_user_id = ?
                    UNION
                    SELECT conversation_id
                    FROM room_web_members
                    WHERE web_user_id = ? AND active = 1
                    ORDER BY conversation_id
                    """,
                    (str(identity["user_id"]), str(identity["user_id"])),
                ).fetchall()
                conversations = [str(row["conversation_id"]) for row in rows]
        return {
            "web_user_id": str(identity["user_id"]),
            "is_admin": is_admin,
            "conversation_ids": conversations,
        }

    def require_web_room_access(
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
            access = self._require_web_room_access_locked(
                conn,
                web_identity=identity,
                conversation_id=conversation,
            )
        return access

    def room_web_permissions_bulk(
        self,
        *,
        requesting_web_user_id: str,
        conversation_ids: Sequence[str],
    ) -> dict[str, dict[str, Any]]:
        requester = opaque_id(
            requesting_web_user_id,
            field="requesting_web_user_id",
        )
        conversations = list(
            dict.fromkeys(
                validate_conversation_id(value) for value in conversation_ids
            )
        )
        if not conversations:
            return {}
        with self._connection() as conn:
            web_user = conn.execute(
                "SELECT role FROM web_users WHERE user_id = ? AND active = 1",
                (requester,),
            ).fetchone()
            if web_user is None:
                raise AuthenticationError("active Web user is required")
            placeholders = ",".join("?" for _ in conversations)
            rows = conn.execute(
                f"""
                SELECT room.conversation_id, room.status,
                       CASE WHEN owner.web_user_id IS NOT NULL THEN 1 ELSE 0 END
                           AS is_room_owner,
                       access.access_role
                FROM rooms AS room
                LEFT JOIN room_web_owners AS owner
                  ON owner.conversation_id = room.conversation_id
                 AND owner.web_user_id = ?
                LEFT JOIN room_web_members AS access
                  ON access.conversation_id = room.conversation_id
                 AND access.web_user_id = ?
                 AND access.active = 1
                WHERE room.conversation_id IN ({placeholders})
                """,
                (requester, requester, *conversations),
            ).fetchall()
            by_conversation = {
                str(row["conversation_id"]): row for row in rows
            }
            result: dict[str, dict[str, Any]] = {}
            for conversation in conversations:
                row = by_conversation.get(conversation)
                if row is None:
                    raise NotFoundError(f"unknown conversation: {conversation}")
                result[conversation] = self._room_web_permission_payload(
                    conversation_id=conversation,
                    room_status=str(row["status"]),
                    is_global_admin=str(web_user["role"]) == "admin",
                    is_room_owner=bool(row["is_room_owner"]),
                    access_role=(
                        str(row["access_role"])
                        if row["access_role"] is not None
                        else None
                    ),
                )
            return result

    def search_room_web_users(
        self,
        *,
        requesting_web_user_id: str,
        conversation_id: str,
        query: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        requester = opaque_id(
            requesting_web_user_id,
            field="requesting_web_user_id",
        )
        conversation = validate_conversation_id(conversation_id)
        normalized_query = str(query or "").strip()
        if len(normalized_query) > 64 or any(
            ord(character) < 32 for character in normalized_query
        ):
            raise ValidationError("query must contain at most 64 visible characters")
        normalized_limit = max(1, min(int(limit), 200))
        escaped = (
            normalized_query.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        pattern = f"%{escaped}%"
        with self._connection() as conn:
            manager_permissions = self._room_web_permissions_locked(
                conn,
                web_user_id=requester,
                conversation_id=conversation,
            )
            if not manager_permissions["can_manage_web_members"]:
                raise AuthorizationError("你没有管理这个聊天室成员的权限")
            if conn.execute(
                "SELECT 1 FROM rooms WHERE conversation_id = ?",
                (conversation,),
            ).fetchone() is None:
                raise NotFoundError(f"unknown conversation: {conversation}")
            rows = conn.execute(
                """
                SELECT web_user.user_id, web_user.username,
                       web_user.display_name, web_user.signature,
                       web_user.avatar_key,
                       CASE WHEN owner.web_user_id IS NOT NULL THEN 1 ELSE 0 END
                           AS is_room_owner,
                       COALESCE(access.active, 0) AS room_access_active,
                       COALESCE(access.access_role, 'member') AS access_role,
                       access.updated_at AS access_updated_at
                FROM web_users AS web_user
                LEFT JOIN room_web_owners AS owner
                  ON owner.conversation_id = ?
                 AND owner.web_user_id = web_user.user_id
                LEFT JOIN room_web_members AS access
                  ON access.conversation_id = ?
                 AND access.web_user_id = web_user.user_id
                WHERE web_user.role = 'user' AND web_user.active = 1
                  AND (? = '' OR web_user.username LIKE ? ESCAPE '\\'
                       OR web_user.display_name LIKE ? ESCAPE '\\'
                       OR web_user.signature LIKE ? ESCAPE '\\')
                ORDER BY is_room_owner DESC, room_access_active DESC,
                         web_user.display_name COLLATE NOCASE,
                         web_user.username COLLATE NOCASE
                LIMIT ?
                """,
                (
                    conversation,
                    conversation,
                    normalized_query,
                    pattern,
                    pattern,
                    pattern,
                    normalized_limit,
                ),
            ).fetchall()
        users = [self._room_web_user_payload(row) for row in rows]
        return {
            "conversation_id": conversation,
            "users": users,
            "count": len(users),
            "query": normalized_query,
            "permissions": manager_permissions,
        }

    def manage_room_web_member(
        self,
        *,
        requesting_web_user_id: str,
        conversation_id: str,
        target_web_user_id: str,
        active: bool,
        access_role: str = "member",
    ) -> dict[str, Any]:
        requester = opaque_id(
            requesting_web_user_id,
            field="requesting_web_user_id",
        )
        target = opaque_id(target_web_user_id, field="target_web_user_id")
        conversation = validate_conversation_id(conversation_id)
        if not isinstance(active, bool):
            raise ValidationError("active must be a boolean")
        normalized_role = str(access_role or "member").strip().lower()
        if normalized_role not in {"member", "moderator"}:
            raise ValidationError("access_role must be member or moderator")
        now = time.time()
        with self._transaction() as conn:
            manager_permissions = self._room_web_permissions_locked(
                conn,
                web_user_id=requester,
                conversation_id=conversation,
            )
            if not manager_permissions["can_manage_web_members"]:
                raise AuthorizationError("你没有管理这个聊天室成员的权限")
            room = conn.execute(
                "SELECT status FROM rooms WHERE conversation_id = ?",
                (conversation,),
            ).fetchone()
            if room is None:
                raise NotFoundError(f"unknown conversation: {conversation}")
            target_row = conn.execute(
                "SELECT * FROM web_users "
                "WHERE user_id = ? AND role = 'user' AND active = 1",
                (target,),
            ).fetchone()
            if target_row is None:
                raise NotFoundError("目标普通用户不存在或已停用")
            owner = conn.execute(
                "SELECT 1 FROM room_web_owners "
                "WHERE conversation_id = ? AND web_user_id = ?",
                (conversation, target),
            ).fetchone()
            if not active and owner is not None:
                raise ConflictError("不能移除聊天室创建者")
            current_access = conn.execute(
                "SELECT access_role, active FROM room_web_members "
                "WHERE conversation_id = ? AND web_user_id = ?",
                (conversation, target),
            ).fetchone()
            target_is_moderator = bool(
                current_access is not None
                and current_access["active"]
                and str(current_access["access_role"]) == "moderator"
            )
            if (
                manager_permissions["room_role"] == "moderator"
                and target_is_moderator
            ):
                raise AuthorizationError("聊天室管理员不能修改其他管理员")
            if (
                active
                and normalized_role == "moderator"
                and not manager_permissions["can_delegate_room_moderators"]
            ):
                raise AuthorizationError("只有全局管理员或创建者可以委派管理员")
            if active:
                if str(room["status"]) != "active":
                    raise ConflictError("不能向已废弃的聊天室添加 Web 用户")
                conn.execute(
                    """
                    INSERT INTO room_web_members
                        (conversation_id, web_user_id, access_role, active,
                         invited_by_web_user_id, created_at, updated_at)
                    VALUES (?, ?, ?, 1, ?, ?, ?)
                    ON CONFLICT(conversation_id, web_user_id) DO UPDATE SET
                        access_role = excluded.access_role,
                        active = 1,
                        invited_by_web_user_id = excluded.invited_by_web_user_id,
                        updated_at = excluded.updated_at
                    """,
                    (
                        conversation,
                        target,
                        normalized_role,
                        requester,
                        now,
                        now,
                    ),
                )
                self._ensure_web_membership_locked(
                    conn,
                    conversation_id=conversation,
                    participant_id=str(target_row["participant_id"]),
                    display_name=str(target_row["display_name"]),
                    signature=str(target_row["signature"]),
                    role=str(target_row["role"]),
                    now=now,
                )
            else:
                conn.execute(
                    "UPDATE room_web_members SET active = 0, updated_at = ? "
                    "WHERE conversation_id = ? AND web_user_id = ?",
                    (now, conversation, target),
                )
                conn.execute(
                    "UPDATE memberships SET active = 0, updated_at = ? "
                    "WHERE conversation_id = ? AND participant_id = ?",
                    (now, conversation, str(target_row["participant_id"])),
                )
                conn.execute(
                    "DELETE FROM room_task_grants "
                    "WHERE conversation_id = ? AND web_user_id = ?",
                    (conversation, target),
                )
            row = conn.execute(
                """
                SELECT web_user.user_id, web_user.username,
                       web_user.display_name, web_user.signature,
                       web_user.avatar_key,
                       CASE WHEN owner.web_user_id IS NOT NULL THEN 1 ELSE 0 END
                           AS is_room_owner,
                       COALESCE(access.active, 0) AS room_access_active,
                       COALESCE(access.access_role, 'member') AS access_role,
                       access.updated_at AS access_updated_at
                FROM web_users AS web_user
                LEFT JOIN room_web_owners AS owner
                  ON owner.conversation_id = ?
                 AND owner.web_user_id = web_user.user_id
                LEFT JOIN room_web_members AS access
                  ON access.conversation_id = ?
                 AND access.web_user_id = web_user.user_id
                WHERE web_user.user_id = ?
                """,
                (conversation, conversation, target),
            ).fetchone()
        return self._room_web_user_payload(row)

    def search_web_user_room_permissions(
        self,
        *,
        requesting_web_user_id: str,
        query: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        requester = opaque_id(
            requesting_web_user_id,
            field="requesting_web_user_id",
        )
        normalized_query = str(query or "").strip()
        if len(normalized_query) > 64 or any(
            ord(character) < 32 for character in normalized_query
        ):
            raise ValidationError("query must contain at most 64 visible characters")
        normalized_limit = max(1, min(int(limit), 100))
        escaped = (
            normalized_query.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        pattern = f"%{escaped}%"
        with self._connection() as conn:
            self._require_active_rate_admin_locked(conn, requester)
            rows = conn.execute(
                """
                SELECT web_user.*,
                       COUNT(CASE WHEN room.status = 'active' THEN 1 END)
                           AS owned_active_room_count,
                       COUNT(ownership.conversation_id) AS owned_room_count
                FROM web_users AS web_user
                LEFT JOIN room_web_owners AS ownership
                  ON ownership.web_user_id = web_user.user_id
                LEFT JOIN rooms AS room
                  ON room.conversation_id = ownership.conversation_id
                WHERE web_user.role = 'user' AND web_user.active = 1
                  AND (? = '' OR web_user.username LIKE ? ESCAPE '\\'
                       OR web_user.display_name LIKE ? ESCAPE '\\'
                       OR web_user.signature LIKE ? ESCAPE '\\')
                GROUP BY web_user.user_id
                ORDER BY web_user.display_name COLLATE NOCASE, web_user.username
                LIMIT ?
                """,
                (
                    normalized_query,
                    pattern,
                    pattern,
                    pattern,
                    normalized_limit,
                ),
            ).fetchall()
        users = [self._web_user_room_permission_payload(row) for row in rows]
        return {"users": users, "count": len(users), "query": normalized_query}

    def update_web_user_room_permission(
        self,
        *,
        requesting_web_user_id: str,
        target_web_user_id: str,
        can_create_rooms: bool,
        room_limit: int,
    ) -> dict[str, Any]:
        requester = opaque_id(
            requesting_web_user_id,
            field="requesting_web_user_id",
        )
        target = opaque_id(target_web_user_id, field="target_web_user_id")
        if not isinstance(can_create_rooms, bool):
            raise ValidationError("can_create_rooms must be a boolean")
        if isinstance(room_limit, bool) or not isinstance(room_limit, int):
            raise ValidationError("room_limit must be an integer")
        normalized_limit = room_limit
        if not 1 <= normalized_limit <= MAX_WEB_USER_ROOM_LIMIT:
            raise ValidationError(
                f"room_limit must be between 1 and {MAX_WEB_USER_ROOM_LIMIT}"
            )
        now = time.time()
        with self._transaction() as conn:
            self._require_active_rate_admin_locked(conn, requester)
            row = conn.execute(
                "SELECT * FROM web_users WHERE user_id = ? AND active = 1",
                (target,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"unknown active web user: {target}")
            if str(row["role"]) != "user":
                raise ConflictError("administrator room creation is always enabled")
            conn.execute(
                "UPDATE web_users SET can_create_rooms = ?, room_limit = ?, "
                "updated_at = ? WHERE user_id = ?",
                (1 if can_create_rooms else 0, normalized_limit, now, target),
            )
            updated = conn.execute(
                """
                SELECT web_user.*,
                       COUNT(CASE WHEN room.status = 'active' THEN 1 END)
                           AS owned_active_room_count,
                       COUNT(ownership.conversation_id) AS owned_room_count
                FROM web_users AS web_user
                LEFT JOIN room_web_owners AS ownership
                  ON ownership.web_user_id = web_user.user_id
                LEFT JOIN rooms AS room
                  ON room.conversation_id = ownership.conversation_id
                WHERE web_user.user_id = ?
                GROUP BY web_user.user_id
                """,
                (target,),
            ).fetchone()
        return self._web_user_room_permission_payload(updated)

    def room(self, conversation_id: str) -> dict[str, Any]:
        """Return one room's authoritative identity and lifecycle state."""

        conversation = validate_conversation_id(conversation_id)
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM rooms WHERE conversation_id = ?",
                (conversation,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"unknown conversation: {conversation}")
        return {
            "conversation_id": str(row["conversation_id"]),
            "status": str(row["status"]),
            "creator_kind": str(row["creator_kind"]),
            "created_at": float(row["created_at"]),
            "last_activity_at": float(row["last_activity_at"]),
            "abandoned_at": (
                float(row["abandoned_at"])
                if row["abandoned_at"] is not None
                else None
            ),
        }

    @staticmethod
    def _normalize_room_marker_kind(value: str) -> str:
        normalized = str(value or "").strip().casefold()
        if normalized not in ROOM_MESSAGE_MARKER_KINDS:
            raise ValidationError("marker_kind must be pin or decision")
        return normalized

    @staticmethod
    def _normalize_room_marker_note(value: str | None) -> str:
        normalized = str(value or "").strip()
        if len(normalized) > 2_000:
            raise ValidationError("marker note must contain at most 2000 characters")
        if any(
            ord(character) < 32 and character not in "\t\n\r"
            for character in normalized
        ):
            raise ValidationError("marker note contains invalid control characters")
        return normalized

    def set_room_message_marker(
        self,
        *,
        conversation_id: str,
        message_id: str,
        marker_kind: str,
        note: str | None,
        requesting_web_user_id: str,
    ) -> dict[str, Any]:
        """Pin a room message or retain it as an explicit decision record."""

        conversation = validate_conversation_id(conversation_id)
        message = opaque_id(message_id, field="message_id")
        kind = self._normalize_room_marker_kind(marker_kind)
        normalized_note = self._normalize_room_marker_note(note)
        actor = opaque_id(
            requesting_web_user_id,
            field="requesting_web_user_id",
        )
        now = time.time()
        with self._transaction() as conn:
            permissions = self._room_web_permissions_locked(
                conn,
                web_user_id=actor,
                conversation_id=conversation,
            )
            if not permissions["can_manage_highlights"]:
                raise AuthorizationError(
                    "只有管理员、聊天室创建者或聊天室管理员可以维护房间要点"
                )
            room_message = conn.execute(
                "SELECT message_id FROM messages "
                "WHERE message_id = ? AND conversation_id = ?",
                (message, conversation),
            ).fetchone()
            if room_message is None:
                raise NotFoundError(
                    f"unknown message {message} in conversation {conversation}"
                )
            conn.execute(
                """
                INSERT INTO room_message_markers
                    (conversation_id, message_id, marker_kind, note,
                     created_by_web_user_id, updated_by_web_user_id,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id, message_id, marker_kind) DO UPDATE
                SET note = excluded.note,
                    updated_by_web_user_id = excluded.updated_by_web_user_id,
                    updated_at = excluded.updated_at
                """,
                (
                    conversation,
                    message,
                    kind,
                    normalized_note,
                    actor,
                    actor,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM room_message_markers "
                "WHERE conversation_id = ? AND message_id = ? "
                "AND marker_kind = ?",
                (conversation, message, kind),
            ).fetchone()
        return self._room_message_marker_payload(row)

    def remove_room_message_marker(
        self,
        *,
        conversation_id: str,
        message_id: str,
        marker_kind: str,
        requesting_web_user_id: str,
    ) -> dict[str, Any]:
        conversation = validate_conversation_id(conversation_id)
        message = opaque_id(message_id, field="message_id")
        kind = self._normalize_room_marker_kind(marker_kind)
        actor = opaque_id(
            requesting_web_user_id,
            field="requesting_web_user_id",
        )
        with self._transaction() as conn:
            permissions = self._room_web_permissions_locked(
                conn,
                web_user_id=actor,
                conversation_id=conversation,
            )
            if not permissions["can_manage_highlights"]:
                raise AuthorizationError(
                    "只有管理员、聊天室创建者或聊天室管理员可以维护房间要点"
                )
            row = conn.execute(
                "SELECT * FROM room_message_markers "
                "WHERE conversation_id = ? AND message_id = ? "
                "AND marker_kind = ?",
                (conversation, message, kind),
            ).fetchone()
            if row is None:
                raise NotFoundError("room message marker does not exist")
            conn.execute(
                "DELETE FROM room_message_markers "
                "WHERE conversation_id = ? AND message_id = ? "
                "AND marker_kind = ?",
                (conversation, message, kind),
            )
        payload = self._room_message_marker_payload(row)
        payload["removed"] = True
        return payload

    @staticmethod
    def _room_message_marker_payload(
        row: sqlite3.Row | None,
    ) -> dict[str, Any]:
        if row is None:
            raise NotFoundError("room message marker disappeared")
        return {
            "conversation_id": str(row["conversation_id"]),
            "message_id": str(row["message_id"]),
            "marker_kind": str(row["marker_kind"]),
            "note": str(row["note"] or ""),
            "created_by_web_user_id": str(row["created_by_web_user_id"]),
            "updated_by_web_user_id": str(row["updated_by_web_user_id"]),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    def rename_room(
        self,
        *,
        conversation_id: str,
        new_conversation_id: str,
        renamed_by_web_user_id: str,
    ) -> dict[str, Any]:
        """Rename one room while preserving every foreign-key-linked record."""

        current = validate_conversation_id(conversation_id)
        renamed = validate_conversation_id(new_conversation_id)
        actor = opaque_id(
            renamed_by_web_user_id,
            field="renamed_by_web_user_id",
        )
        now = time.time()
        with self._transaction() as conn:
            room_permissions = self._room_web_permissions_locked(
                conn,
                web_user_id=actor,
                conversation_id=current,
            )
            if not room_permissions["can_rename_room"]:
                raise AuthorizationError("只有全局管理员或聊天室创建者可以重命名")
            room = conn.execute(
                "SELECT * FROM rooms WHERE conversation_id = ?",
                (current,),
            ).fetchone()
            if room is None:
                raise NotFoundError(f"unknown conversation: {current}")
            if renamed == current:
                return {
                    "previous_conversation_id": current,
                    "conversation_id": current,
                    "status": str(room["status"]),
                    "renamed_at": now,
                }
            duplicate = conn.execute(
                "SELECT status FROM rooms WHERE conversation_id = ?",
                (renamed,),
            ).fetchone()
            if duplicate is not None:
                raise ConflictError(
                    f"conversation {renamed} already exists with status "
                    f"{duplicate['status']}"
                )
            conn.execute("PRAGMA defer_foreign_keys = ON")
            conn.execute(
                "UPDATE rooms SET conversation_id = ?, "
                "last_activity_at = MAX(last_activity_at, ?) "
                "WHERE conversation_id = ?",
                (renamed, now, current),
            )
            for table in (
                "memberships",
                "agent_sessions",
                "messages",
                "room_message_sequences",
                "room_message_markers",
                "agent_invitations",
                "agent_connectors",
                "agent_room_blocks",
                "room_web_owners",
                "room_web_members",
                "room_task_policies",
                "room_task_grants",
                "room_wake_policies",
                "room_tasks",
                "chat_authorization_grants",
                "a2a_access_grants",
            ):
                column = (
                    "registered_conversation_id"
                    if table == "agent_sessions"
                    else "conversation_id"
                )
                conn.execute(
                    f"UPDATE {table} SET {column} = ? WHERE {column} = ?",
                    (renamed, current),
                )
            conn.execute(
                "UPDATE follows SET conversation_id = ?, updated_at = ? "
                "WHERE conversation_id = ?",
                (renamed, now, current),
            )
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise BridgeError("room rename would violate database relationships")
        return {
            "previous_conversation_id": current,
            "conversation_id": renamed,
            "status": str(room["status"]),
            "renamed_at": now,
        }

    def create_agent_room(
        self,
        *,
        authorized_session_id: str,
        participant_id: str,
        conversation_id: str,
    ) -> dict[str, Any]:
        session = opaque_id(
            authorized_session_id,
            field="authorized_session_id",
        )
        participant = opaque_id(participant_id, field="participant_id")
        conversation = validate_conversation_id(conversation_id)
        now = time.time()
        with self._transaction() as conn:
            self._archive_stale_rooms_locked(conn, now=now)
            self._require_live_session(
                conn,
                session_id=session,
                participant_id=participant,
                now=now,
            )
            existing = conn.execute(
                "SELECT status FROM rooms WHERE conversation_id = ?",
                (conversation,),
            ).fetchone()
            if existing is not None:
                raise ConflictError(
                    f"conversation {conversation} already exists with status "
                    f"{existing['status']}"
                )
            owned_count = self._agent_active_room_count(conn, participant)
            if owned_count >= AGENT_ACTIVE_ROOM_LIMIT:
                raise ConflictError(
                    "this agent session already owns the maximum of "
                    f"{AGENT_ACTIVE_ROOM_LIMIT} active rooms"
                )
            conn.execute(
                """
                INSERT INTO rooms
                    (conversation_id, status, creator_kind,
                     creator_participant_id, created_at, last_activity_at)
                VALUES (?, 'active', 'agent', ?, ?, ?)
                """,
                (conversation, participant, now, now),
            )
            conn.execute(
                """
                INSERT INTO memberships
                    (conversation_id, participant_id, roles_json, active,
                     joined_at, updated_at)
                VALUES (?, ?, '[]', 1, ?, ?)
                """,
                (conversation, participant, now, now),
            )
            owned_count += 1
        return {
            "conversation_id": conversation,
            "status": "active",
            "creator_kind": "agent",
            "creator_participant_id": participant,
            "created_at": now,
            "last_activity_at": now,
            "owned_active_room_count": owned_count,
            "owned_active_room_limit": AGENT_ACTIVE_ROOM_LIMIT,
        }

    @staticmethod
    def _require_membership(
        conn: sqlite3.Connection,
        participant_id: str,
        conversation_id: str,
    ) -> sqlite3.Row:
        RoomGovernanceMixin._require_active_room(conn, conversation_id)
        participant = conn.execute(
            "SELECT participant_id FROM participants WHERE participant_id = ?",
            (participant_id,),
        ).fetchone()
        if participant is None:
            raise NotFoundError(f"unknown participant: {participant_id}")
        membership = conn.execute(
            "SELECT * FROM memberships WHERE conversation_id = ? "
            "AND participant_id = ? AND active = 1",
            (conversation_id, participant_id),
        ).fetchone()
        if membership is None:
            raise ConflictError(
                f"participant {participant_id} is not in conversation {conversation_id}"
            )
        return membership

    @staticmethod
    def _require_active_room(
        conn: sqlite3.Connection,
        conversation_id: str,
    ) -> sqlite3.Row:
        room = conn.execute(
            "SELECT * FROM rooms WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if room is None:
            raise NotFoundError(f"unknown conversation: {conversation_id}")
        if str(room["status"]) != "active":
            raise ConflictError(
                f"conversation {conversation_id} is abandoned and cannot be entered"
            )
        return room

    @staticmethod
    def _require_web_room_access_locked(
        conn: sqlite3.Connection,
        *,
        web_identity: sqlite3.Row,
        conversation_id: str,
    ) -> dict[str, Any]:
        room = conn.execute(
            "SELECT status FROM rooms WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if room is None:
            raise NotFoundError(f"unknown conversation: {conversation_id}")
        user_id = str(web_identity["user_id"])
        is_admin = str(web_identity["role"]) == "admin"
        owner = conn.execute(
            "SELECT 1 FROM room_web_owners "
            "WHERE conversation_id = ? AND web_user_id = ?",
            (conversation_id, user_id),
        ).fetchone()
        member = conn.execute(
            "SELECT access_role FROM room_web_members "
            "WHERE conversation_id = ? AND web_user_id = ? AND active = 1",
            (conversation_id, user_id),
        ).fetchone()
        is_owner = owner is not None
        if not is_admin and not is_owner and member is None:
            raise AuthorizationError("你无权访问这个聊天室")
        return {
            "conversation_id": conversation_id,
            "room_status": str(room["status"]),
            "is_admin": is_admin,
            "is_room_owner": is_owner,
            "access_role": (
                "admin"
                if is_admin
                else "owner"
                if is_owner
                else str(member["access_role"])
            ),
        }

    @staticmethod
    def _room_web_permissions_locked(
        conn: sqlite3.Connection,
        *,
        web_user_id: str,
        conversation_id: str,
    ) -> dict[str, Any]:
        web_user = conn.execute(
            "SELECT user_id, role FROM web_users "
            "WHERE user_id = ? AND active = 1",
            (web_user_id,),
        ).fetchone()
        if web_user is None:
            raise AuthenticationError("active Web user is required")
        room = conn.execute(
            "SELECT status FROM rooms WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if room is None:
            raise NotFoundError(f"unknown conversation: {conversation_id}")
        owner = conn.execute(
            "SELECT 1 FROM room_web_owners "
            "WHERE conversation_id = ? AND web_user_id = ?",
            (conversation_id, web_user_id),
        ).fetchone()
        access = conn.execute(
            "SELECT access_role FROM room_web_members "
            "WHERE conversation_id = ? AND web_user_id = ? AND active = 1",
            (conversation_id, web_user_id),
        ).fetchone()
        return RoomGovernanceMixin._room_web_permission_payload(
            conversation_id=conversation_id,
            room_status=str(room["status"]),
            is_global_admin=str(web_user["role"]) == "admin",
            is_room_owner=owner is not None,
            access_role=(
                str(access["access_role"]) if access is not None else None
            ),
        )

    @staticmethod
    def _room_web_permission_payload(
        *,
        conversation_id: str,
        room_status: str,
        is_global_admin: bool,
        is_room_owner: bool,
        access_role: str | None,
    ) -> dict[str, Any]:
        is_moderator = access_role == "moderator"
        if not is_global_admin and not is_room_owner and access_role is None:
            raise AuthorizationError("你无权访问这个聊天室")
        room_role = (
            "global_admin"
            if is_global_admin
            else "owner"
            if is_room_owner
            else "moderator"
            if is_moderator
            else "member"
        )
        can_manage = is_global_admin or is_room_owner or is_moderator
        return {
            "conversation_id": conversation_id,
            "room_status": room_status,
            "room_role": room_role,
            "is_global_admin": is_global_admin,
            "is_room_owner": is_room_owner,
            "is_room_moderator": is_moderator,
            "can_wake_all": can_manage,
            "can_manage_wake_policy": can_manage,
            "can_manage_highlights": can_manage,
            "can_manage_web_members": can_manage,
            "can_delegate_room_moderators": is_global_admin or is_room_owner,
            "can_invite_agents": can_manage,
            "can_kick_agents": can_manage,
            "can_rename_room": is_global_admin or is_room_owner,
        }

    @staticmethod
    def _web_user_room_permission_payload(
        row: sqlite3.Row | None,
    ) -> dict[str, Any]:
        if row is None:
            raise NotFoundError("web user row disappeared")
        return {
            "user_id": str(row["user_id"]),
            "username": str(row["username"]),
            "display_name": str(row["display_name"]),
            "signature": str(row["signature"]),
            "avatar_key": str(row["avatar_key"] or "auto"),
            "can_create_rooms": bool(row["can_create_rooms"]),
            "room_limit": int(row["room_limit"]),
            "owned_active_room_count": int(
                row["owned_active_room_count"] or 0
            ),
            "owned_room_count": int(row["owned_room_count"] or 0),
        }

    @staticmethod
    def _room_web_user_payload(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            raise NotFoundError("web room member row disappeared")
        return {
            "user_id": str(row["user_id"]),
            "username": str(row["username"]),
            "display_name": str(row["display_name"]),
            "signature": str(row["signature"]),
            "avatar_key": str(row["avatar_key"] or "auto"),
            "is_room_owner": bool(row["is_room_owner"]),
            "has_room_access": bool(row["room_access_active"])
            or bool(row["is_room_owner"]),
            "access_role": (
                "owner"
                if bool(row["is_room_owner"])
                else str(row["access_role"] or "member")
            ),
            "access_updated_at": (
                float(row["access_updated_at"])
                if row["access_updated_at"] is not None
                else None
            ),
        }
