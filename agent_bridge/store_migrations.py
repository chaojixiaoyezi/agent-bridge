"""Idempotent compatibility migrations for historical Agent Bridge databases."""

from __future__ import annotations

import sqlite3
import time

from .agent_connectors import INVITATION_SCHEMA
from .message_delivery import ROOM_MESSAGE_SEQUENCE_SCHEMA
from .store_constants import CHAT_AUTHORIZATION_FROZEN, OWNER_PARTICIPANT_ID
from .store_errors import BridgeError
from .store_schema import _agent_sessions_table_sql
from .web_auth import DEFAULT_WEB_USER_ROOM_LIMIT, MAX_WEB_USER_ROOM_LIMIT


class StoreMigrationMixin:
    @staticmethod
    def _migrate_web_user_room_permissions(conn: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(web_users)").fetchall()
        }
        if "can_create_rooms" not in columns:
            conn.execute(
                "ALTER TABLE web_users ADD COLUMN can_create_rooms INTEGER "
                "NOT NULL DEFAULT 0 CHECK (can_create_rooms IN (0, 1))"
            )
        if "room_limit" not in columns:
            conn.execute(
                "ALTER TABLE web_users ADD COLUMN room_limit INTEGER "
                f"NOT NULL DEFAULT {DEFAULT_WEB_USER_ROOM_LIMIT} "
                f"CHECK (room_limit BETWEEN 1 AND {MAX_WEB_USER_ROOM_LIMIT})"
            )
        if "avatar_key" not in columns:
            conn.execute(
                "ALTER TABLE web_users ADD COLUMN avatar_key TEXT "
                "NOT NULL DEFAULT 'auto'"
            )
        for column, declaration in (
            ("email", "TEXT COLLATE NOCASE"),
            ("email_verified_at", "REAL"),
            ("pending_email", "TEXT COLLATE NOCASE"),
            ("email_updated_at", "REAL"),
        ):
            if column not in columns:
                conn.execute(
                    f"ALTER TABLE web_users ADD COLUMN {column} {declaration}"
                )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_web_users_verified_email_unique "
            "ON web_users(email COLLATE NOCASE) WHERE email IS NOT NULL"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_web_users_pending_email_unique "
            "ON web_users(pending_email COLLATE NOCASE) "
            "WHERE pending_email IS NOT NULL"
        )

    @staticmethod
    def _initialize_room_message_sequences_locked(
        conn: sqlite3.Connection,
    ) -> None:
        """Backfill stable room-local labels without changing global cursors."""

        conn.execute(
            """
            WITH ranked AS (
                SELECT message_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY conversation_id ORDER BY sequence
                       ) AS assigned_sequence
                FROM messages
            )
            UPDATE messages
            SET room_sequence = (
                SELECT ranked.assigned_sequence
                FROM ranked
                WHERE ranked.message_id = messages.message_id
            )
            WHERE room_sequence IS NULL
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS room_message_sequences (
                conversation_id TEXT PRIMARY KEY,
                last_sequence INTEGER NOT NULL DEFAULT 0
                    CHECK (last_sequence >= 0),
                FOREIGN KEY (conversation_id) REFERENCES rooms(conversation_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO room_message_sequences (conversation_id, last_sequence)
            SELECT conversation_id, MAX(room_sequence)
            FROM messages
            GROUP BY conversation_id
            ON CONFLICT(conversation_id) DO UPDATE
            SET last_sequence = MAX(
                room_message_sequences.last_sequence,
                excluded.last_sequence
            )
            """
        )
        conn.executescript(ROOM_MESSAGE_SEQUENCE_SCHEMA)
        # An old process can insert in the narrow migration window before the
        # trigger exists. Repair such rows once more, then seed counters from
        # the authoritative room order before enforcing uniqueness.
        conn.execute(
            """
            WITH ranked AS (
                SELECT message_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY conversation_id ORDER BY sequence
                       ) AS assigned_sequence
                FROM messages
            )
            UPDATE messages
            SET room_sequence = (
                SELECT ranked.assigned_sequence
                FROM ranked
                WHERE ranked.message_id = messages.message_id
            )
            WHERE room_sequence IS NULL
            """
        )
        conn.execute(
            """
            INSERT INTO room_message_sequences (conversation_id, last_sequence)
            SELECT conversation_id, MAX(room_sequence)
            FROM messages
            GROUP BY conversation_id
            ON CONFLICT(conversation_id) DO UPDATE
            SET last_sequence = MAX(
                room_message_sequences.last_sequence,
                excluded.last_sequence
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "idx_messages_conversation_room_sequence "
            "ON messages(conversation_id, room_sequence)"
        )

    @staticmethod
    def _backfill_room_web_members(conn: sqlite3.Connection) -> None:
        """Preserve explicit pre-v30 Web participation without opening rooms."""

        conn.execute(
            """
            INSERT OR IGNORE INTO room_web_members
                (conversation_id, web_user_id, access_role, active,
                 invited_by_web_user_id, created_at, updated_at)
            SELECT ownership.conversation_id, ownership.web_user_id,
                   'member', 1, ownership.web_user_id,
                   ownership.created_at, ownership.created_at
            FROM room_web_owners AS ownership
            JOIN web_users AS web_user
              ON web_user.user_id = ownership.web_user_id
             AND web_user.active = 1
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO room_web_members
                (conversation_id, web_user_id, access_role, active,
                 invited_by_web_user_id, created_at, updated_at)
            SELECT membership.conversation_id, web_user.user_id,
                   'member', 1, NULL,
                   membership.joined_at, membership.updated_at
            FROM memberships AS membership
            JOIN web_users AS web_user
              ON web_user.participant_id = membership.participant_id
             AND web_user.active = 1
             AND web_user.role = 'user'
            WHERE membership.active = 1
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO room_web_members
                (conversation_id, web_user_id, access_role, active,
                 invited_by_web_user_id, created_at, updated_at)
            SELECT grant_row.conversation_id, grant_row.web_user_id,
                   'member', 1, grant_row.granted_by_web_user_id,
                   grant_row.created_at, grant_row.updated_at
            FROM room_task_grants AS grant_row
            JOIN web_users AS web_user
              ON web_user.user_id = grant_row.web_user_id
             AND web_user.active = 1
            """
        )

    @staticmethod
    def _migrate_invited_sessions(conn: sqlite3.Connection) -> None:
        """Remove invite-bound session storage while preserving live sessions.

        Version 4 stored one invite id on every MCP session.  Open registration
        keeps the room that established the session but no longer stores or
        accepts invitation codes.  The migration runs before the version 5
        schema and removes the obsolete invites table in the same transaction.
        """
        session_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'agent_sessions'"
        ).fetchone()
        if session_table is None:
            return
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(agent_sessions)").fetchall()
        }
        if "registered_conversation_id" in columns:
            return
        if "invite_id" not in columns:
            raise BridgeError("unsupported agent_sessions schema")
        invite_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'invites'"
        ).fetchone()
        if invite_table is None:
            raise BridgeError("cannot migrate invite sessions without invites table")

        conn.execute("DROP TRIGGER IF EXISTS trg_messages_require_live_mcp_session")
        conn.execute("DROP TRIGGER IF EXISTS trg_messages_require_authorized_sender")
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("BEGIN IMMEDIATE")
            source_count = int(
                conn.execute("SELECT COUNT(*) FROM agent_sessions").fetchone()[0]
            )
            conn.execute(
                _agent_sessions_table_sql(
                    "agent_sessions_open_registration",
                    if_not_exists=False,
                )
            )
            conn.execute(
                """
                INSERT INTO agent_sessions_open_registration
                    (session_id, participant_id, registered_conversation_id,
                     token_hash, transport, created_at, expires_at, last_seen,
                     revoked_at, revoked_reason)
                SELECT
                    session.session_id,
                    session.participant_id,
                    invite.conversation_id,
                    session.token_hash,
                    session.transport,
                    session.created_at,
                    session.expires_at,
                    session.last_seen,
                    session.revoked_at,
                    session.revoked_reason
                FROM agent_sessions AS session
                JOIN invites AS invite ON invite.invite_id = session.invite_id
                """
            )
            copied_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM agent_sessions_open_registration"
                ).fetchone()[0]
            )
            if copied_count != source_count:
                raise BridgeError(
                    "agent session migration would lose rows: "
                    f"source={source_count}, copied={copied_count}"
                )
            conn.execute("DROP TABLE agent_sessions")
            conn.execute(
                "ALTER TABLE agent_sessions_open_registration "
                "RENAME TO agent_sessions"
            )
            conn.execute("DROP TABLE invites")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _migrate_reusable_agent_invitations(conn: sqlite3.Connection) -> None:
        """Split v14's one-connector invitation row into reusable grants."""

        invitation_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'agent_invitations'"
        ).fetchone()
        if invitation_table is None:
            return
        columns = {
            str(row["name"])
            for row in conn.execute(
                "PRAGMA table_info(agent_invitations)"
            ).fetchall()
        }
        if "reuse_policy" in columns:
            connector_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'agent_connectors'"
            ).fetchone()
            if connector_table is None:
                raise BridgeError("reusable invitations require agent_connectors")
            return
        if "connector_id" not in columns:
            raise BridgeError("unsupported agent_invitations schema")

        legacy_table = "agent_invitations_v14"
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(f"DROP TABLE IF EXISTS {legacy_table}")
            conn.execute(
                f"ALTER TABLE agent_invitations RENAME TO {legacy_table}"
            )
            for index_name in (
                "idx_agent_invitations_room_created",
                "idx_agent_invitations_status_expires",
                "idx_agent_invitations_participant",
                "idx_agent_invitations_connector",
            ):
                conn.execute(f"DROP INDEX IF EXISTS {index_name}")
            for statement in INVITATION_SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute(
                f"""
                INSERT INTO agent_invitations
                    (invitation_id, token_hash, conversation_id, product,
                     requested_mode, adapter_kind, reuse_policy, max_uses,
                     use_count, status, created_by_web_user_id, created_at,
                     expires_at, first_accepted_at, last_accepted_at,
                     revoked_at, updated_at)
                SELECT invitation_id, token_hash, conversation_id, product,
                       requested_mode, adapter_kind, 'single', 1,
                       CASE WHEN accepted_at IS NOT NULL THEN 1 ELSE 0 END,
                       CASE status
                           WHEN 'pending' THEN 'active'
                           WHEN 'accepted' THEN 'exhausted'
                           ELSE status
                       END,
                       created_by_web_user_id, created_at, expires_at,
                       accepted_at, accepted_at, revoked_at, updated_at
                FROM {legacy_table}
                """
            )
            conn.execute(
                f"""
                INSERT INTO agent_connectors
                    (connector_id, invitation_id, conversation_id,
                     accepted_participant_id,
                     initial_session_id, enrollment_token_hash,
                     enrollment_last_used_at, setup_status,
                     setup_detail_json, setup_updated_at,
                     connector_last_seen_at, created_at, revoked_at, updated_at)
                SELECT connector_id, invitation_id, conversation_id,
                       accepted_participant_id,
                       accepted_session_id, enrollment_token_hash,
                       enrollment_last_used_at,
                       CASE WHEN setup_status = 'awaiting_acceptance'
                            THEN 'awaiting_setup' ELSE setup_status END,
                       setup_detail_json, setup_updated_at,
                       connector_last_seen_at,
                       COALESCE(accepted_at, updated_at),
                       CASE WHEN status = 'revoked' THEN revoked_at ELSE NULL END,
                       updated_at
                FROM {legacy_table}
                WHERE connector_id IS NOT NULL
                  AND accepted_participant_id IS NOT NULL
                  AND accepted_session_id IS NOT NULL
                  AND enrollment_token_hash IS NOT NULL
                """
            )
            conn.execute(f"DROP TABLE {legacy_table}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    @staticmethod
    def _migrate_agent_connector_conversations(conn: sqlite3.Connection) -> None:
        """Give every v15 connector its own movable room binding."""

        connector_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'agent_connectors'"
        ).fetchone()
        if connector_table is None:
            return
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(agent_connectors)").fetchall()
        }
        if "conversation_id" in columns:
            return
        conn.execute(
            "ALTER TABLE agent_connectors ADD COLUMN conversation_id TEXT "
            "REFERENCES rooms(conversation_id)"
        )
        conn.execute(
            """
            UPDATE agent_connectors
            SET conversation_id = (
                SELECT invitation.conversation_id
                FROM agent_invitations AS invitation
                WHERE invitation.invitation_id = agent_connectors.invitation_id
            )
            """
        )
        missing = int(
            conn.execute(
                "SELECT COUNT(*) FROM agent_connectors "
                "WHERE conversation_id IS NULL OR trim(conversation_id) = ''"
            ).fetchone()[0]
        )
        if missing:
            raise BridgeError(
                "agent connector room migration would leave "
                f"{missing} connector(s) without a room"
            )

    @staticmethod
    def _migrate_connector_identity_bindings(conn: sqlite3.Connection) -> None:
        """Snapshot immutable connector identity without invalidating v22 clients."""

        connector_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'agent_connectors'"
        ).fetchone()
        if connector_table is None:
            return
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(agent_connectors)").fetchall()
        }
        additions = {
            "binding_version": (
                "INTEGER NOT NULL DEFAULT 1 CHECK (binding_version IN (1, 2))"
            ),
            "requested_username": "TEXT",
            "bound_client_type": "TEXT",
            "bound_roles_json": "TEXT",
            "bound_capabilities_json": "TEXT",
            "previous_enrollment_token_hash": "TEXT",
            "previous_enrollment_valid_until": "REAL",
            "enrollment_rotated_at": "REAL",
            "enrollment_rotation_count": (
                "INTEGER NOT NULL DEFAULT 0 "
                "CHECK (enrollment_rotation_count >= 0)"
            ),
            "enrollment_credential_version": (
                "INTEGER NOT NULL DEFAULT 1 "
                "CHECK (enrollment_credential_version >= 1)"
            ),
            "enrollment_rotation_required_at": "REAL",
            "enrollment_rotation_requested_by_web_user_id": "TEXT",
            "revoked_by_web_user_id": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in columns:
                conn.execute(
                    f"ALTER TABLE agent_connectors ADD COLUMN {name} {declaration}"
                )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_connectors_rotation_required "
            "ON agent_connectors(enrollment_rotation_required_at, revoked_at)"
        )
        conn.execute(
            """
            UPDATE agent_connectors
            SET bound_client_type = (
                    SELECT participant.client_type
                    FROM participants AS participant
                    WHERE participant.participant_id =
                          agent_connectors.accepted_participant_id
                )
            WHERE bound_client_type IS NULL OR trim(bound_client_type) = ''
            """
        )
        conn.execute(
            """
            UPDATE agent_connectors
            SET requested_username = (
                    SELECT CASE
                        WHEN substr(
                                 participant.client_type,
                                 1,
                                 length(invitation.product) + 1
                             ) = invitation.product || '-'
                        THEN substr(
                                 participant.client_type,
                                 length(invitation.product) + 2
                             )
                        ELSE participant.client_type
                    END
                    FROM participants AS participant
                    JOIN agent_invitations AS invitation
                      ON invitation.invitation_id = agent_connectors.invitation_id
                    WHERE participant.participant_id =
                          agent_connectors.accepted_participant_id
                )
            WHERE requested_username IS NULL OR trim(requested_username) = ''
            """
        )
        conn.execute(
            """
            UPDATE agent_connectors
            SET bound_roles_json = COALESCE((
                    SELECT membership.roles_json
                    FROM memberships AS membership
                    WHERE membership.conversation_id =
                          agent_connectors.conversation_id
                      AND membership.participant_id =
                          agent_connectors.accepted_participant_id
                ), '[]')
            WHERE bound_roles_json IS NULL OR trim(bound_roles_json) = ''
            """
        )
        conn.execute(
            """
            UPDATE agent_connectors
            SET bound_capabilities_json = COALESCE((
                    SELECT participant.capabilities_json
                    FROM participants AS participant
                    WHERE participant.participant_id =
                          agent_connectors.accepted_participant_id
                ), '[]')
            WHERE bound_capabilities_json IS NULL
               OR trim(bound_capabilities_json) = ''
            """
        )
        incomplete = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM agent_connectors
                WHERE requested_username IS NULL OR trim(requested_username) = ''
                   OR bound_client_type IS NULL OR trim(bound_client_type) = ''
                   OR bound_roles_json IS NULL OR trim(bound_roles_json) = ''
                   OR bound_capabilities_json IS NULL
                      OR trim(bound_capabilities_json) = ''
                """
            ).fetchone()[0]
        )
        if incomplete:
            raise BridgeError(
                "connector identity migration left "
                f"{incomplete} incomplete binding(s)"
            )

    @staticmethod
    def _migrate_native_tui_bindings(conn: sqlite3.Connection) -> None:
        """Add native-TUI state without rebuilding live invitation tables."""

        invitation_columns = {
            str(row["name"])
            for row in conn.execute(
                "PRAGMA table_info(agent_invitations)"
            ).fetchall()
        }
        if "tui_adapter_kind" not in invitation_columns:
            conn.execute(
                "ALTER TABLE agent_invitations ADD COLUMN tui_adapter_kind TEXT"
            )
        # This column is reserved for the native-session bridge. Early v26
        # development builds briefly copied first-party adapter names here;
        # clear them so legacy Codex/Claude invitations keep their unchanged
        # resident path after an in-place upgrade.
        conn.execute(
            "UPDATE agent_invitations SET tui_adapter_kind = NULL "
            "WHERE tui_adapter_kind IN ('codex', 'claude-code')"
        )

        connector_columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(agent_connectors)").fetchall()
        }
        legacy_access_mode = "tui_access_mode" in connector_columns
        additions = {
            "tui_endpoint_id": "TEXT",
            "tui_native_session_id": "TEXT",
            "tui_state": "TEXT NOT NULL DEFAULT 'unbound'",
            "tui_capabilities_json": "TEXT NOT NULL DEFAULT '[]'",
            "tui_last_seen_at": "REAL",
            "tui_active_task_id": "TEXT",
            "tui_detail_json": "TEXT NOT NULL DEFAULT '{}'",
            "native_delivery_mode": (
                "TEXT NOT NULL DEFAULT 'legacy_shadow' "
                "CHECK (native_delivery_mode IN "
                "('legacy_shadow', 'native_preferred'))"
            ),
            "native_lease_id": "TEXT",
            "native_process_epoch": "TEXT",
            "native_lease_expires_at": "REAL",
            "native_binding_source": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in connector_columns:
                conn.execute(
                    f"ALTER TABLE agent_connectors ADD COLUMN {name} {declaration}"
                )
        if legacy_access_mode:
            # v35 deliberately stops persisting a guessed TUI permission mode.
            # Keep the legacy column in upgraded SQLite databases so the
            # migration remains additive, but erase its stale value. The
            # bound local runtime is the only permission authority for every
            # turn and may change independently at any time.
            conn.execute(
                "UPDATE agent_connectors SET tui_access_mode = 'unknown' "
                "WHERE tui_access_mode IS NULL OR tui_access_mode <> 'unknown'"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_connectors_tui_endpoint "
            "ON agent_connectors(tui_endpoint_id, revoked_at, tui_last_seen_at DESC)"
        )

    @staticmethod
    def _repair_connector_room_bindings(conn: sqlite3.Connection) -> None:
        """Repair pre-v21 connector/session room drift without moving memberships.

        An invitation is the immutable authority that created a connector. Room
        renames update both rows atomically, so a mismatch means an older
        migration rebound only the central connector record while its local
        resident configuration stayed in the invitation room.
        """

        conn.execute(
            """
            UPDATE agent_connectors
            SET conversation_id = (
                    SELECT invitation.conversation_id
                    FROM agent_invitations AS invitation
                    WHERE invitation.invitation_id = agent_connectors.invitation_id
                ),
                updated_at = MAX(updated_at, CAST(strftime('%s', 'now') AS REAL))
            WHERE EXISTS (
                SELECT 1 FROM agent_invitations AS invitation
                WHERE invitation.invitation_id = agent_connectors.invitation_id
                  AND invitation.conversation_id != agent_connectors.conversation_id
            )
            """
        )
        conn.execute(
            """
            UPDATE agent_sessions
            SET registered_conversation_id = (
                    SELECT connector.conversation_id
                    FROM agent_connectors AS connector
                    WHERE connector.connector_id = agent_sessions.connector_id
                )
            WHERE connector_id IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM agent_connectors AS connector
                  WHERE connector.connector_id = agent_sessions.connector_id
                    AND connector.conversation_id
                        != agent_sessions.registered_conversation_id
              )
            """
        )

    @staticmethod
    def _restore_legacy_migrated_memberships(conn: sqlite3.Connection) -> None:
        """Restore source-room membership removed by the old move semantics.

        Migration is additive, while each resident connector remains bound to one
        room. Older releases deactivated the source membership and recorded a
        ``migrated`` block. Reactivate only memberships whose source room is still
        active; abandoned-room history remains untouched.
        """

        conn.execute(
            """
            UPDATE memberships
            SET active = 1,
                updated_at = MAX(
                    updated_at,
                    COALESCE((
                        SELECT block.blocked_at
                        FROM agent_room_blocks AS block
                        WHERE block.conversation_id = memberships.conversation_id
                          AND block.participant_id = memberships.participant_id
                          AND block.reason = 'migrated'
                    ), updated_at)
                )
            WHERE active = 0
              AND EXISTS (
                  SELECT 1 FROM rooms AS room
                  WHERE room.conversation_id = memberships.conversation_id
                    AND room.status = 'active'
              )
              AND EXISTS (
                  SELECT 1 FROM agent_room_blocks AS block
                  WHERE block.conversation_id = memberships.conversation_id
                    AND block.participant_id = memberships.participant_id
                    AND block.reason = 'migrated'
              )
            """
        )
        conn.execute(
            """
            DELETE FROM agent_room_blocks
            WHERE reason = 'migrated'
              AND EXISTS (
                  SELECT 1 FROM memberships AS membership
                  WHERE membership.conversation_id = agent_room_blocks.conversation_id
                    AND membership.participant_id = agent_room_blocks.participant_id
                    AND membership.active = 1
              )
            """
        )

    @staticmethod
    def _backfill_agent_lifecycle_states(conn: sqlite3.Connection) -> None:
        """Seed inactivity anchors without changing any current membership."""

        conn.execute(
            """
            INSERT OR IGNORE INTO agent_lifecycle_states
                (participant_id, access_granted_at, last_spoke_at,
                 reinvite_required, expired_at, expired_reason, updated_at)
            SELECT participant.participant_id,
                   MAX(
                       participant.created_at,
                       COALESCE((
                           SELECT MAX(membership.joined_at)
                           FROM memberships AS membership
                           WHERE membership.participant_id = participant.participant_id
                       ), participant.created_at),
                       COALESCE((
                           SELECT MAX(session.created_at)
                           FROM agent_sessions AS session
                           WHERE session.participant_id = participant.participant_id
                       ), participant.created_at),
                       COALESCE((
                           SELECT MAX(connector.created_at)
                           FROM agent_connectors AS connector
                           WHERE connector.accepted_participant_id = participant.participant_id
                       ), participant.created_at)
                   ),
                   (
                       SELECT MAX(message.created_at)
                       FROM messages AS message
                       WHERE message.sender_participant_id = participant.participant_id
                   ),
                   0, NULL, NULL, participant.last_seen
            FROM participants AS participant
            WHERE participant.participant_id != ?
              AND NOT EXISTS (
                  SELECT 1 FROM web_users AS web_user
                  WHERE web_user.participant_id = participant.participant_id
              )
            """,
            (OWNER_PARTICIPANT_ID,),
        )

    @staticmethod
    def _freeze_legacy_chat_authorizations(conn: sqlite3.Connection) -> None:
        """Keep the old ledger for audit while removing all chat authority.

        Ordinary room prose is deliberately not an execution authorization
        boundary.  Existing rows remain queryable so a rolling upgrade loses no
        history, but every row is projected as frozen and no new row is created.
        """

        if not CHAT_AUTHORIZATION_FROZEN:
            return
        now = time.time()
        conn.execute(
            """
            UPDATE chat_authorization_grants
            SET authority_kind = 'legacy_frozen',
                revoked_at = COALESCE(revoked_at, ?),
                revocation_reason = COALESCE(
                    revocation_reason,
                    'chat_authorization_feature_frozen'
                )
            WHERE authority_kind != 'legacy_frozen'
               OR revoked_at IS NULL
            """,
            (now,),
        )
