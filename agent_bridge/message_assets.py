"""Structured links and recipient-restricted message attachments."""

from __future__ import annotations

import hashlib
import os
import time
import unicodedata
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .store_constants import OWNER_PARTICIPANT_ID
from .store_errors import AuthorizationError, NotFoundError
from .validation import ValidationError, opaque_id


MAX_MESSAGE_LINKS = 8
MAX_MESSAGE_ATTACHMENTS = 5
MAX_ATTACHMENT_BYTES = 12 * 1024 * 1024
MAX_ATTACHMENTS_TOTAL_BYTES = 25 * 1024 * 1024
MAX_LINK_URL_CHARS = 2_048
MAX_ATTACHMENT_FILENAME_CHARS = 180


MESSAGE_ASSET_SCHEMA = """
CREATE TABLE IF NOT EXISTS message_restrictions (
    message_id TEXT PRIMARY KEY,
    target_kind TEXT NOT NULL
        CHECK (target_kind IN ('participants', 'room_agents')),
    created_by_web_user_id TEXT,
    created_at REAL NOT NULL,
    FOREIGN KEY (message_id) REFERENCES messages(message_id) ON DELETE CASCADE,
    FOREIGN KEY (created_by_web_user_id) REFERENCES web_users(user_id)
);

CREATE TABLE IF NOT EXISTS message_restriction_recipients (
    message_id TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    PRIMARY KEY (message_id, participant_id),
    FOREIGN KEY (message_id)
        REFERENCES message_restrictions(message_id) ON DELETE CASCADE,
    FOREIGN KEY (participant_id) REFERENCES participants(participant_id)
);

CREATE TABLE IF NOT EXISTS message_attachments (
    attachment_id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    attachment_kind TEXT NOT NULL CHECK (attachment_kind IN ('image', 'file')),
    filename TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    sha256 TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE (message_id, position),
    FOREIGN KEY (message_id) REFERENCES messages(message_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS message_links (
    link_id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    url TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE (message_id, position),
    FOREIGN KEY (message_id) REFERENCES messages(message_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_message_restriction_recipient
    ON message_restriction_recipients(participant_id, message_id);
CREATE INDEX IF NOT EXISTS idx_message_attachments_message
    ON message_attachments(message_id, position);
CREATE INDEX IF NOT EXISTS idx_message_links_message
    ON message_links(message_id, position);
"""


def _safe_filename(value: object) -> str:
    raw = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    normalized = unicodedata.normalize("NFC", raw).strip().strip(".")
    normalized = "".join(
        character
        for character in normalized
        if not unicodedata.category(character).startswith("C")
    )
    if not normalized:
        normalized = "attachment.bin"
    if len(normalized) > MAX_ATTACHMENT_FILENAME_CHARS:
        suffix = Path(normalized).suffix[:20]
        stem_limit = MAX_ATTACHMENT_FILENAME_CHARS - len(suffix)
        normalized = f"{normalized[:stem_limit]}{suffix}"
    return normalized


def _safe_declared_media_type(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if (
        not normalized
        or len(normalized) > 127
        or "/" not in normalized
        or any(character.isspace() or ord(character) < 33 for character in normalized)
    ):
        return "application/octet-stream"
    return normalized


def _image_media_type(content: bytes) -> str | None:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return None


class MessageAssetMixin:
    """Own attachment storage, structured link projection, and visibility ACLs."""

    attachment_root: Path

    def _initialize_message_asset_storage(self) -> None:
        self.attachment_root.mkdir(parents=True, exist_ok=True)
        self._attachment_staging_root().mkdir(parents=True, exist_ok=True)
        self._attachment_blob_root().mkdir(parents=True, exist_ok=True)
        for directory in (
            self.attachment_root,
            self._attachment_staging_root(),
            self._attachment_blob_root(),
        ):
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass
        self._cleanup_orphaned_attachment_files()

    def _attachment_staging_root(self) -> Path:
        return self.attachment_root / ".staging"

    def _attachment_blob_root(self) -> Path:
        return self.attachment_root / "blobs"

    def _attachment_blob_path(self, attachment_id: str) -> Path:
        normalized = opaque_id(attachment_id, field="attachment_id")
        shard = normalized[-2:]
        return self._attachment_blob_root() / shard / f"{normalized}.blob"

    def _cleanup_orphaned_attachment_files(self) -> None:
        cutoff = time.time() - 86_400
        for path in self._attachment_staging_root().glob("*.part"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue
        try:
            with self._connection() as conn:
                referenced = {
                    str(row["attachment_id"])
                    for row in conn.execute(
                        "SELECT attachment_id FROM message_attachments"
                    ).fetchall()
                }
        except Exception:
            return
        for path in self._attachment_blob_root().glob("*/*.blob"):
            try:
                if path.stem not in referenced and path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue

    @staticmethod
    def _normalize_message_links(
        links: Sequence[str] | None,
    ) -> list[dict[str, str]]:
        if links is None:
            return []
        if isinstance(links, (str, bytes)):
            raise ValidationError("links must be an array of HTTP(S) URLs")
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw in links:
            value = str(raw or "").strip()
            if not value or len(value) > MAX_LINK_URL_CHARS:
                raise ValidationError(
                    f"each link must contain 1-{MAX_LINK_URL_CHARS} characters"
                )
            if any(ord(character) < 32 for character in value):
                raise ValidationError("links cannot contain control characters")
            parsed = urlsplit(value)
            if (
                parsed.scheme.lower() not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValidationError(
                    "links must be absolute HTTP(S) URLs without embedded credentials"
                )
            try:
                port = f":{parsed.port}" if parsed.port is not None else ""
            except ValueError as exc:
                raise ValidationError("link port is invalid") from exc
            hostname = parsed.hostname.encode("idna").decode("ascii").lower()
            netloc = f"[{hostname}]" if ":" in hostname else hostname
            normalized = urlunsplit(
                (
                    parsed.scheme.lower(),
                    f"{netloc}{port}",
                    parsed.path or "/",
                    parsed.query,
                    parsed.fragment,
                )
            )
            if normalized in seen:
                continue
            seen.add(normalized)
            display = f"{parsed.hostname}{parsed.path or ''}"
            if len(display) > 120:
                display = f"{display[:117]}…"
            result.append(
                {
                    "url": normalized,
                    "host": str(parsed.hostname),
                    "display": display or str(parsed.hostname),
                }
            )
        if len(result) > MAX_MESSAGE_LINKS:
            raise ValidationError(f"one message accepts at most {MAX_MESSAGE_LINKS} links")
        return result

    def _stage_message_attachments(
        self,
        attachments: Sequence[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        if attachments is None:
            return []
        if isinstance(attachments, (str, bytes, bytearray)):
            raise ValidationError("attachments must be an array")
        raw_items = list(attachments)
        if len(raw_items) > MAX_MESSAGE_ATTACHMENTS:
            raise ValidationError(
                f"one message accepts at most {MAX_MESSAGE_ATTACHMENTS} files"
            )
        staged: list[dict[str, Any]] = []
        total = 0
        try:
            for position, item in enumerate(raw_items):
                if not isinstance(item, dict):
                    raise ValidationError("each attachment must be an object")
                content = item.get("content")
                if not isinstance(content, bytes):
                    raise ValidationError("attachment content must be bytes")
                size = len(content)
                if size <= 0:
                    raise ValidationError("attachments cannot be empty")
                if size > MAX_ATTACHMENT_BYTES:
                    raise ValidationError(
                        f"each attachment must be at most {MAX_ATTACHMENT_BYTES} bytes"
                    )
                total += size
                if total > MAX_ATTACHMENTS_TOTAL_BYTES:
                    raise ValidationError(
                        "attachment total exceeds "
                        f"{MAX_ATTACHMENTS_TOTAL_BYTES} bytes"
                    )
                attachment_id = f"attachment_{uuid.uuid4().hex}"
                stage_path = self._attachment_staging_root() / f"{attachment_id}.part"
                descriptor = os.open(
                    stage_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                try:
                    with os.fdopen(descriptor, "wb") as handle:
                        handle.write(content)
                        handle.flush()
                        os.fsync(handle.fileno())
                except Exception:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                    raise
                detected_image = _image_media_type(content)
                declared = _safe_declared_media_type(item.get("media_type"))
                staged.append(
                    {
                        "attachment_id": attachment_id,
                        "position": position,
                        "attachment_kind": "image" if detected_image else "file",
                        "filename": _safe_filename(item.get("filename")),
                        "media_type": detected_image or declared,
                        "size_bytes": size,
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "stage_path": stage_path,
                        "final_path": self._attachment_blob_path(attachment_id),
                    }
                )
        except Exception:
            self._discard_staged_message_attachments(staged, include_final=True)
            raise
        return staged

    @staticmethod
    def _discard_staged_message_attachments(
        staged: Sequence[dict[str, Any]],
        *,
        include_final: bool,
    ) -> None:
        for item in staged:
            candidates = [item.get("stage_path")]
            if include_final:
                candidates.append(item.get("final_path"))
            for candidate in candidates:
                if not isinstance(candidate, Path):
                    continue
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    pass

    @staticmethod
    def _restricted_message_recipient_ids_locked(
        conn,
        *,
        conversation_id: str,
        sender_participant_id: str,
        mentioned_participant_ids: Sequence[str],
        wake_all_agents: bool,
        created_at: float,
    ) -> tuple[str, list[str]]:
        if wake_all_agents:
            rows = conn.execute(
                """
                SELECT membership.participant_id
                FROM memberships AS membership
                LEFT JOIN web_users AS web_user
                  ON web_user.participant_id = membership.participant_id
                WHERE membership.conversation_id = ?
                  AND membership.active = 1
                  AND membership.joined_at <= ?
                  AND web_user.user_id IS NULL
                  AND membership.participant_id != ?
                  AND membership.participant_id != ?
                ORDER BY membership.participant_id
                """,
                (
                    conversation_id,
                    created_at,
                    sender_participant_id,
                    OWNER_PARTICIPANT_ID,
                ),
            ).fetchall()
            recipients = [str(row["participant_id"]) for row in rows]
            target_kind = "room_agents"
        else:
            requested = sorted(set(mentioned_participant_ids))
            if not requested:
                raise ValidationError(
                    "files and images require at least one structured Agent mention "
                    "or @all"
                )
            placeholders = ",".join("?" for _ in requested)
            rows = conn.execute(
                f"""
                SELECT membership.participant_id
                FROM memberships AS membership
                LEFT JOIN web_users AS web_user
                  ON web_user.participant_id = membership.participant_id
                WHERE membership.conversation_id = ?
                  AND membership.active = 1
                  AND membership.joined_at <= ?
                  AND web_user.user_id IS NULL
                  AND membership.participant_id != ?
                  AND membership.participant_id IN ({placeholders})
                ORDER BY membership.participant_id
                """,
                (
                    conversation_id,
                    created_at,
                    OWNER_PARTICIPANT_ID,
                    *requested,
                ),
            ).fetchall()
            recipients = [str(row["participant_id"]) for row in rows]
            missing = sorted(set(requested) - set(recipients))
            if missing:
                raise ValidationError(
                    "attachment recipients must be active Agents in the same room: "
                    + ", ".join(missing)
                )
            target_kind = "participants"
        if not recipients:
            raise ValidationError("the restricted message has no eligible Agent recipient")
        return target_kind, recipients

    def _persist_message_assets_locked(
        self,
        conn,
        *,
        message_id: str,
        links: Sequence[dict[str, str]],
        attachments: Sequence[dict[str, Any]],
        conversation_id: str,
        sender_participant_id: str,
        mentioned_participant_ids: Sequence[str],
        wake_all_agents: bool,
        created_by_web_user_id: str | None,
        created_at: float,
        inherited_target_kind: str | None = None,
        inherited_recipient_ids: Sequence[str] | None = None,
    ) -> None:
        inherited_recipients = sorted(
            set(str(value) for value in inherited_recipient_ids or [] if value)
        )
        target_kind: str | None = None
        recipients: list[str] = []
        if attachments:
            target_kind, recipients = self._restricted_message_recipient_ids_locked(
                conn,
                conversation_id=conversation_id,
                sender_participant_id=sender_participant_id,
                mentioned_participant_ids=mentioned_participant_ids,
                wake_all_agents=wake_all_agents,
                created_at=created_at,
            )
            if inherited_recipients:
                expanded = sorted(set(recipients) - set(inherited_recipients))
                if expanded:
                    raise AuthorizationError(
                        "a reply cannot expand the fixed recipients of a restricted "
                        "message"
                    )
        elif inherited_recipients:
            target_kind = (
                str(inherited_target_kind)
                if inherited_target_kind in {"participants", "room_agents"}
                else "participants"
            )
            recipients = inherited_recipients
        if recipients:
            conn.execute(
                "INSERT INTO message_restrictions "
                "(message_id, target_kind, created_by_web_user_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (message_id, target_kind, created_by_web_user_id, created_at),
            )
            conn.executemany(
                "INSERT INTO message_restriction_recipients "
                "(message_id, participant_id) VALUES (?, ?)",
                [(message_id, recipient) for recipient in recipients],
            )
        for item in attachments:
            final_path = item["final_path"]
            final_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(final_path.parent, 0o700)
            except OSError:
                pass
            os.replace(item["stage_path"], final_path)
            try:
                os.chmod(final_path, 0o600)
            except OSError:
                pass
            conn.execute(
                """
                INSERT INTO message_attachments
                    (attachment_id, message_id, position, attachment_kind,
                     filename, media_type, size_bytes, sha256, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["attachment_id"],
                    message_id,
                    item["position"],
                    item["attachment_kind"],
                    item["filename"],
                    item["media_type"],
                    item["size_bytes"],
                    item["sha256"],
                    created_at,
                ),
            )
        for position, link in enumerate(links):
            conn.execute(
                "INSERT INTO message_links "
                "(link_id, message_id, position, url, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    f"link_{uuid.uuid4().hex}",
                    message_id,
                    position,
                    link["url"],
                    created_at,
                ),
            )

    @classmethod
    def _message_asset_projection_locked(
        cls,
        conn,
        message_ids: Sequence[str],
    ) -> dict[str, dict[str, Any]]:
        normalized_ids = list(dict.fromkeys(str(value) for value in message_ids if value))
        result = {
            message_id: {
                "visibility": {"kind": "room"},
                "attachments": [],
                "links": [],
            }
            for message_id in normalized_ids
        }
        if not normalized_ids:
            return result
        for offset in range(0, len(normalized_ids), 400):
            chunk = normalized_ids[offset : offset + 400]
            placeholders = ",".join("?" for _ in chunk)
            restrictions = conn.execute(
                f"""
                SELECT restriction.message_id, restriction.target_kind,
                       recipient.participant_id,
                       participant.display_name, participant.client_type
                FROM message_restrictions AS restriction
                LEFT JOIN message_restriction_recipients AS recipient
                  ON recipient.message_id = restriction.message_id
                LEFT JOIN participants AS participant
                  ON participant.participant_id = recipient.participant_id
                WHERE restriction.message_id IN ({placeholders})
                ORDER BY restriction.message_id, participant.display_name,
                         recipient.participant_id
                """,
                chunk,
            ).fetchall()
            for row in restrictions:
                message_id = str(row["message_id"])
                visibility = result[message_id].setdefault(
                    "visibility",
                    {
                        "kind": "restricted",
                        "target_kind": str(row["target_kind"]),
                    },
                )
                if visibility.get("kind") != "restricted":
                    visibility.clear()
                    visibility.update(
                        {
                            "kind": "restricted",
                            "target_kind": str(row["target_kind"]),
                            "recipients": [],
                        }
                    )
                if row["participant_id"] is not None:
                    visibility.setdefault("recipients", []).append(
                        {
                            "participant_id": str(row["participant_id"]),
                            "display_name": str(row["display_name"] or ""),
                            "client_type": str(row["client_type"] or ""),
                        }
                    )
            attachment_rows = conn.execute(
                f"SELECT * FROM message_attachments "
                f"WHERE message_id IN ({placeholders}) "
                "ORDER BY message_id, position",
                chunk,
            ).fetchall()
            for row in attachment_rows:
                result[str(row["message_id"])]["attachments"].append(
                    {
                        "attachment_id": str(row["attachment_id"]),
                        "kind": str(row["attachment_kind"]),
                        "filename": str(row["filename"]),
                        "media_type": str(row["media_type"]),
                        "size_bytes": int(row["size_bytes"]),
                        "sha256": str(row["sha256"]),
                    }
                )
            link_rows = conn.execute(
                f"SELECT * FROM message_links "
                f"WHERE message_id IN ({placeholders}) "
                "ORDER BY message_id, position",
                chunk,
            ).fetchall()
            for row in link_rows:
                normalized = cls._normalize_message_links([str(row["url"])])[0]
                result[str(row["message_id"])]["links"].append(
                    {
                        "link_id": str(row["link_id"]),
                        **normalized,
                    }
                )
        return result

    @staticmethod
    def _participant_message_visibility_sql(message_alias: str = "message") -> str:
        return f"""
        (
            NOT EXISTS (
                SELECT 1 FROM message_restrictions AS visibility_restriction
                WHERE visibility_restriction.message_id = {message_alias}.message_id
            )
            OR EXISTS (
                SELECT 1
                FROM message_restriction_recipients AS visibility_recipient
                WHERE visibility_recipient.message_id = {message_alias}.message_id
                  AND visibility_recipient.participant_id = ?
            )
        )
        """

    def attachment_record(
        self,
        *,
        attachment_id: str,
        conversation_id: str | None = None,
        participant_id: str | None = None,
        authorized_session_id: str | None = None,
    ) -> dict[str, Any]:
        attachment = opaque_id(attachment_id, field="attachment_id")
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT asset.*, message.conversation_id
                FROM message_attachments AS asset
                JOIN messages AS message ON message.message_id = asset.message_id
                WHERE asset.attachment_id = ?
                """,
                (attachment,),
            ).fetchone()
            if row is None:
                raise NotFoundError("attachment does not exist")
            room = str(row["conversation_id"])
            if conversation_id is not None and room != str(conversation_id):
                raise NotFoundError("attachment does not exist in this room")
            if participant_id is not None:
                participant = opaque_id(participant_id, field="participant_id")
                session = opaque_id(
                    authorized_session_id,
                    field="authorized_session_id",
                )
                self._require_live_room_session(
                    conn,
                    session_id=session,
                    participant_id=participant,
                    conversation_id=room,
                    now=time.time(),
                )
                allowed = conn.execute(
                    "SELECT 1 FROM message_restriction_recipients "
                    "WHERE message_id = ? AND participant_id = ?",
                    (str(row["message_id"]), participant),
                ).fetchone()
                if allowed is None:
                    raise AuthorizationError(
                        "this attachment was not addressed to this Agent"
                    )
        path = self._attachment_blob_path(attachment)
        if not path.is_file():
            raise NotFoundError("attachment content is unavailable")
        return {
            "attachment_id": attachment,
            "message_id": str(row["message_id"]),
            "conversation_id": str(row["conversation_id"]),
            "kind": str(row["attachment_kind"]),
            "filename": str(row["filename"]),
            "media_type": str(row["media_type"]),
            "size_bytes": int(row["size_bytes"]),
            "sha256": str(row["sha256"]),
            "path": path,
        }

    def message_is_restricted(self, message_id: str) -> bool:
        normalized = opaque_id(message_id, field="message_id")
        with self._connection() as conn:
            return conn.execute(
                "SELECT 1 FROM message_restrictions WHERE message_id = ?",
                (normalized,),
            ).fetchone() is not None
