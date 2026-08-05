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

import config
import database
import engine
import models
from models import RoomGeneration, StateChanges, TurnLogEntry, WorldInit


def _print(result: dict) -> None:
    print(json.dumps(result, separators=(",", ":")))


def _read_stdin_json() -> dict:
    return json.loads(sys.stdin.read())


def _read_optional_stdin_json() -> "dict | None":
    """Optional JSON payload on stdin. A TTY (interactive terminal, no pipe)
    or an empty/whitespace pipe both mean "no payload" — only actual content
    is parsed. This keeps plain `init`/`reset`/`state` working in agent and
    CI contexts where stdin is an empty pipe or /dev/null."""
    if sys.stdin.isatty():
        return None
    raw = sys.stdin.read().strip()
    if not raw:
        return None
    return json.loads(raw)


def _load_campaign(name: str) -> WorldInit:
    """A predefined campaign by name, resolved against the installed skill
    directory — so `--campaign pirate_islands` works from any cwd, unlike a
    relative `< campaigns/pirate_islands.json` redirect."""
    campaigns_dir = config.SKILL_ROOT / "campaigns"
    path = campaigns_dir / f"{name}.json"
    if not path.is_file():
        available = ", ".join(sorted(p.stem for p in campaigns_dir.glob("*.json")))
        raise FileNotFoundError(f"no campaign named {name!r} — available: {available}")
    return WorldInit.model_validate(json.loads(path.read_text()))


def _world_init_from(args) -> "WorldInit | None":
    """--campaign <name> wins; otherwise an optional WorldInit on stdin."""
    if getattr(args, "campaign", None):
        return _load_campaign(args.campaign)
    payload = _read_optional_stdin_json()
    return WorldInit.model_validate(payload) if payload is not None else None


def cmd_init(conn, args):
    return engine.init_game(conn, _world_init_from(args))


def cmd_reset(conn, args):
    return engine.reset_game(conn, _world_init_from(args))


def cmd_export_world(conn, args):
    return engine.export_world(conn)


def cmd_state(conn, args):
    """`state` doubles as the logging point for the *previous* turn: pipe
    its {"player_input", "narrative"} on stdin and it's written to turn_log
    before this turn's state is read. This runs every turn (including pure
    look/inventory turns, which never call `log` otherwise), so a bad or
    missing payload must never block the state read it's piggybacking on."""
    logged_previous_turn = False
    log_error = None
    payload = None
    try:
        payload = _read_optional_stdin_json()
    except json.JSONDecodeError as e:
        log_error = f"invalid_json: {e}"
    if payload is not None:
        try:
            entry = TurnLogEntry.model_validate(payload)
            engine.log_turn(conn, entry.player_input, entry.narrative)
            logged_previous_turn = True
        except pydantic.ValidationError as e:
            log_error = f"validation_error: {e}"
    result = {
        "ok": True,
        "logged_previous_turn": logged_previous_turn,
        **engine.get_full_state(conn),
    }
    if log_error is not None:
        result["log_error"] = log_error
    return result


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


def cmd_set_images(conn, args):
    return engine.set_image_mode(conn, args.mode)


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
    "set-images": cmd_set_images,
}


def _add_db_option(parser: argparse.ArgumentParser) -> None:
    # SUPPRESS so that supplying --db on one side of the subcommand isn't
    # clobbered by the other side's unset default.
    parser.add_argument(
        "--db",
        metavar="PATH",
        default=argparse.SUPPRESS,
        help="save file to use instead of the default (wins over HERMES_DB_PATH); "
        "accepted before or after the subcommand",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="game.py", description="Hermes Adventure Game engine")
    _add_db_option(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser(
        "init",
        help="seed a new game if the DB is empty (idempotent); pass --campaign "
        "or pipe a WorldInit JSON on stdin for a custom campaign",
    )
    reset_p = sub.add_parser(
        "reset",
        help="wipe all data and restart: replays the stored campaign, or pass "
        "--campaign / pipe a WorldInit JSON on stdin to start a different one",
    )
    for p in (init_p, reset_p):
        p.add_argument(
            "--campaign",
            metavar="NAME",
            help="a predefined campaign from campaigns/ by name (e.g. pirate_islands) — "
            "resolved against the skill directory, so it works from any cwd",
        )
    sub.add_parser("export-world", help="print the stored WorldInit payload (shareable)")
    sub.add_parser(
        "state",
        help="print the full current situation; optionally pipe the previous turn's "
        '{"player_input", "narrative"} JSON on stdin to log it first',
    )

    move_p = sub.add_parser("move", help="move in a direction")
    move_p.add_argument("direction", choices=list(models.DIRECTIONS))

    create_room_p = sub.add_parser(
        "create-room", help="submit a RoomGeneration JSON on stdin for the given direction"
    )
    create_room_p.add_argument("direction", choices=list(models.DIRECTIONS))

    sub.add_parser("apply", help="submit a StateChanges JSON on stdin")

    take_p = sub.add_parser("take", help="pick up an obvious item by entity id")
    take_p.add_argument("entity_id", type=int)

    sub.add_parser(
        "log",
        help='append {"player_input", "narrative"} JSON on stdin to turn_log — manual '
        "fallback; normal play logs via `state` (see above)",
    )

    images_p = sub.add_parser(
        "set-images",
        help="set the image generation mode: never, on_demand, significant_moments, or always",
    )
    images_p.add_argument(
        "mode",
        choices=["never", "on_demand", "significant_moments", "always"],
        help="never = text only; on_demand = images when player asks; "
        "significant_moments = new rooms, victories, deaths, obstacle clears; "
        "always = every turn with a room change or action",
    )

    for subparser in sub.choices.values():
        _add_db_option(subparser)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    conn = database.get_connection(getattr(args, "db", None) or config.DB_PATH)
    try:
        database.init_schema(conn)  # always safe: CREATE TABLE IF NOT EXISTS
        try:
            result = COMMANDS[args.command](conn, args)
        except pydantic.ValidationError as e:
            result = {"ok": False, "error": "validation_error", "details": str(e)}
        except json.JSONDecodeError as e:
            result = {"ok": False, "error": "invalid_json", "details": str(e)}
        except FileNotFoundError as e:
            result = {"ok": False, "error": "unknown_campaign", "details": str(e)}
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
