from __future__ import annotations

import argparse
import json
from typing import Any

from .config import BridgeConfig
from .store import BridgeStore
from .viewer_store import ViewerRepository


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-bridge",
        description=(
            "Owner administration and read-only inspection. Agent chat uses "
            "invitation or explicitly authorized registration plus authenticated "
            "short-lived sessions."
        ),
    )
    parser.add_argument("--database", help="Override bridge.db path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_room = subparsers.add_parser("create-room")
    create_room.add_argument("--conversation", required=True)

    subparsers.add_parser("rooms")
    subparsers.add_parser("sessions")

    revoke = subparsers.add_parser("revoke-session")
    revoke.add_argument("--session", required=True)

    history = subparsers.add_parser("history")
    history.add_argument("--conversation", required=True)
    history.add_argument("--limit", type=int, default=100)
    history.add_argument("--before-sequence", type=int)

    participants = subparsers.add_parser("participants")
    participants.add_argument("--conversation", required=True)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    config = BridgeConfig.from_env()
    database = args.database or str(config.database)
    store = BridgeStore(database, poll_interval_seconds=config.poll_interval_seconds)
    repository = ViewerRepository(database)

    if args.command == "create-room":
        result = store.create_user_room(args.conversation)
    elif args.command == "rooms":
        result = {"rooms": repository.rooms(limit=500)}
    elif args.command == "sessions":
        result = {"sessions": repository.sessions(limit=500)}
    elif args.command == "revoke-session":
        result = store.revoke_session(args.session)
    elif args.command == "history":
        result = {
            "conversation_id": args.conversation,
            "messages": repository.messages(
                args.conversation,
                limit=args.limit,
                before_sequence=args.before_sequence,
            ),
        }
    elif args.command == "participants":
        result = {
            "conversation_id": args.conversation,
            "participants": repository.participants(args.conversation),
        }
    else:
        raise AssertionError(f"unhandled command: {args.command}")
    _print(result)


if __name__ == "__main__":
    main()
