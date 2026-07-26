#!/usr/bin/env python3
"""CLI entry point for the Hermes Adventure Game engine.

See TECHNICAL_DETAILS.md §1 for the full command/JSON contract. Every command
prints exactly one JSON object to stdout and exits 0, even on a game-level
failure ({"ok": false, ...}); exit 1 is reserved for actual bugs.
"""

import argparse
import json
import sys

import pydantic

import database
import engine
import models
from models import RoomGeneration, StateChanges, TurnLogEntry, WorldInit


def _print(result: dict) -> None:
    print(json.dumps(result, separators=(",", ":")))


def _read_stdin_json() -> dict:
    return json.loads(sys.stdin.read())


def _read_optional_world_init() -> "WorldInit | None":
    """Optional WorldInit payload on stdin. A TTY (interactive terminal, no
    pipe) or an empty/whitespace pipe both mean "no payload" — only actual
    content is parsed. This keeps plain `init`/`reset` working in agent and
    CI contexts where stdin is an empty pipe or /dev/null."""
    if sys.stdin.isatty():
        return None
    raw = sys.stdin.read().strip()
    if not raw:
        return None
    return WorldInit.model_validate(json.loads(raw))


def cmd_init(conn, args):
    return engine.init_game(conn, _read_optional_world_init())


def cmd_reset(conn, args):
    return engine.reset_game(conn, _read_optional_world_init())


def cmd_export_world(conn, args):
    return engine.export_world(conn)


def cmd_state(conn, args):
    return {"ok": True, **engine.get_full_state(conn)}


def cmd_move(conn, args):
    return engine.move(conn, args.direction)


def cmd_create_room(conn, args):
    room = RoomGeneration.model_validate(_read_stdin_json())
    return engine.create_room(conn, args.direction, room)


def cmd_apply(conn, args):
    changes = StateChanges.model_validate(_read_stdin_json())
    return engine.apply_changes(conn, changes)


def cmd_take(conn, args):
    return engine.take_item(conn, args.entity_id)


def cmd_log(conn, args):
    entry = TurnLogEntry.model_validate(_read_stdin_json())
    return engine.log_turn(conn, entry.player_input, entry.narrative)


COMMANDS = {
    "init": cmd_init,
    "reset": cmd_reset,
    "export-world": cmd_export_world,
    "state": cmd_state,
    "move": cmd_move,
    "create-room": cmd_create_room,
    "apply": cmd_apply,
    "take": cmd_take,
    "log": cmd_log,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="game.py", description="Hermes Adventure Game engine")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "init",
        help="seed a new game if the DB is empty (idempotent); "
        "optionally pipe a WorldInit JSON on stdin for a custom campaign",
    )
    sub.add_parser(
        "reset",
        help="wipe all data and restart: replays the stored campaign, "
        "or pipe a WorldInit JSON on stdin to start a different one",
    )
    sub.add_parser("export-world", help="print the stored WorldInit payload (shareable)")
    sub.add_parser("state", help="print the full current situation")

    move_p = sub.add_parser("move", help="move in a direction")
    move_p.add_argument("direction", choices=list(models.DIRECTIONS))

    create_room_p = sub.add_parser(
        "create-room", help="submit a RoomGeneration JSON on stdin for the given direction"
    )
    create_room_p.add_argument("direction", choices=list(models.DIRECTIONS))

    sub.add_parser("apply", help="submit a StateChanges JSON on stdin")

    take_p = sub.add_parser("take", help="pick up an obvious item by entity id")
    take_p.add_argument("entity_id", type=int)

    sub.add_parser("log", help='append {"player_input", "narrative"} JSON on stdin to turn_log')

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    conn = database.get_connection()
    try:
        database.init_schema(conn)  # always safe: CREATE TABLE IF NOT EXISTS
        try:
            result = COMMANDS[args.command](conn, args)
        except pydantic.ValidationError as e:
            result = {"ok": False, "error": "validation_error", "details": str(e)}
        except json.JSONDecodeError as e:
            result = {"ok": False, "error": "invalid_json", "details": str(e)}
        except RuntimeError as e:
            result = {"ok": False, "error": "not_initialized", "details": str(e)}
        _print(result)
        return 0
    except Exception as e:  # pragma: no cover - defensive, not a game-level failure
        _print({"ok": False, "error": "internal", "details": str(e)})
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
