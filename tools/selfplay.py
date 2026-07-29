#!/usr/bin/env python3
"""Two AIs play the adventure game: one hosts, one plays blind.

The point is the asymmetry. The **host** gets SKILL.md and a tool that drives
the `game.py` CLI, so it invents rooms, referees actions, and owns the save.
The **player** gets no tools at all — only the narration text. That isn't a
policy the player is asked to respect; it is a capability it does not have, so
a blind run stays blind by construction.

Provider-agnostic via Pydantic AI: pass any `provider:model` string, or point
`--base-url` at any OpenAI-compatible endpoint (Ollama, OpenRouter, vLLM, or
`hermes proxy`, which fronts whatever provider Hermes is signed into).

    pip install -r requirements-selfplay.txt
    tools/selfplay.py --campaign amber_tomb --turns 40

The game always runs against a throwaway `--db`, injected by the harness on
every CLI call, so a real save cannot be touched even if a model tries.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GAME_PY = REPO_ROOT / "scripts" / "game.py"
SKILL_MD = REPO_ROOT / "SKILL.md"

# SKILL.md tells the host to run `adventure-game …` in a shell. Here it gets a
# tool instead, and the save is pinned by the harness — restate both, since the
# rest of the contract (turn protocol, room JSON, refereeing) applies unchanged.
HOST_ADDENDUM = """
--- SELF-PLAY HARNESS NOTES (these override SKILL.md where they conflict) ---

You have no shell. Drive the engine only through the `adventure_game` tool:

  SKILL.md says          ->  you call
  adventure-game state   ->  adventure_game(args=["state"])
  adventure-game move north -> adventure_game(args=["move", "north"])
  ... < /tmp/room.json   ->  adventure_game(args=[...], stdin="<the JSON>")

Never pass `--db`; the harness pins the save file itself and will override you.

You are hosting for a player who sees ONLY your narration — no JSON, no entity
ids, no tool output. Everything they need to act must be in your prose. Never
address the harness, and never break character to explain mechanics.

Length: the 3000-character chat limit still applies. Stay well under it.
"""

PLAYER_INSTRUCTIONS = """
You are playing a text adventure game. You cannot see the game's internals —
only what the narrator tells you. That is the whole game: read the description,
form a theory, act on it.

Reply with ONE action, phrased as you would type it into a parser: plain
imperative prose, one or two sentences at most. Examples:

  go north
  take the brass cog
  pry the hatch open with the crowbar
  ask the ghost what she wants
  what does the valve wheel do?

Play to win. Explore systematically, pick up what looks useful, examine
anything the narrator lingers on, and try to work out what the goal is. If you
get stuck, try a different direction rather than repeating a failed action.

Output only the action itself — no commentary, no explanation, no quotes.
"""


def build_model(spec: str, base_url: str | None, api_key: str | None):
    """A `provider:model` string, or an OpenAI-compatible endpoint."""
    if not base_url:
        return spec
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    return OpenAIChatModel(
        spec, provider=OpenAIProvider(base_url=base_url, api_key=api_key or "not-needed")
    )


def _strip_db_flag(args: list[str]) -> list[str]:
    """Drop any caller-supplied --db.

    argparse lets the *last* --db win, so simply prepending the harness's own
    would be overridden by a model that passes one — silently sending the run
    at another save file. Strip theirs; ours is then the only one.
    """
    cleaned: list[str] = []
    skip_value = False
    for arg in args:
        if skip_value:
            skip_value = False
            continue
        if arg == "--db":
            skip_value = True
            continue
        if arg.startswith("--db="):
            continue
        cleaned.append(arg)
    return cleaned


def run_cli(db_path: str, args: list[str], stdin: str = "") -> str:
    """One `game.py` invocation, pinned to the harness's save file."""
    proc = subprocess.run(
        [sys.executable, str(GAME_PY), "--db", db_path, *_strip_db_flag(args)],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    if proc.returncode != 0:
        return json.dumps({"ok": False, "error": "cli_failed", "details": proc.stderr[:500]})
    return proc.stdout.strip()


def game_status(db_path: str) -> dict:
    """Win/death, read straight from the save — never from the host's claims."""
    try:
        state = json.loads(run_cli(db_path, ["state"]))
    except json.JSONDecodeError:
        return {}
    return {
        "won": bool(state.get("game_won")),
        "dead": bool(state.get("player_dead")),
        "room": (state.get("room") or {}).get("name"),
        "hp": state.get("hp"),
    }


def build_agents(args):
    from pydantic_ai import Agent

    host_model = build_model(args.host_model, args.base_url, args.api_key)
    player_model = build_model(args.player_model or args.host_model, args.base_url, args.api_key)

    host = Agent(host_model, instructions=SKILL_MD.read_text() + HOST_ADDENDUM)
    player = Agent(player_model, instructions=PLAYER_INSTRUCTIONS)  # no tools, by design

    db_path = args.db

    @host.tool_plain
    def adventure_game(args: list[str], stdin: str = "") -> str:
        """Run the adventure-game engine. Returns one JSON object.

        Args:
            args: CLI arguments, e.g. ["state"], ["move", "north"], ["apply"].
            stdin: JSON payload for commands that read stdin (create-room,
                apply, log, and the previous turn's entry for state).
        """
        return run_cli(db_path, args, stdin)

    return host, player


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="selfplay.py", description="Two AIs play the adventure game (host + blind player)"
    )
    p.add_argument("--campaign", default="amber_tomb", help="campaign to play (default: amber_tomb)")
    p.add_argument("--db", default="/tmp/selfplay.db", help="throwaway save (default: /tmp/selfplay.db)")
    p.add_argument("--turns", type=int, default=30, help="max turns before stopping (default: 30)")
    p.add_argument(
        "--host-model",
        default="anthropic:claude-opus-5",
        help="model for the Game Host, as provider:model (default: anthropic:claude-opus-5)",
    )
    p.add_argument("--player-model", default=None, help="model for the player (default: same as host)")
    p.add_argument("--base-url", default=None, help="OpenAI-compatible endpoint (e.g. hermes proxy, Ollama)")
    p.add_argument("--api-key", default=None, help="API key for --base-url (default: 'not-needed')")
    p.add_argument("--transcript", default=None, help="also write the transcript to this file")
    args = p.parse_args(argv)

    try:
        host, player = build_agents(args)
    except ImportError:
        print(
            "pydantic-ai is not installed. Run:\n"
            "    pip install -r requirements-selfplay.txt",
            file=sys.stderr,
        )
        return 2

    lines: list[str] = []

    def emit(text: str) -> None:
        print(text, flush=True)
        lines.append(text)

    # Fresh campaign in the throwaway save.
    reset = json.loads(run_cli(args.db, ["reset", "--campaign", args.campaign]))
    if not reset.get("ok"):
        print(f"could not start campaign: {reset}", file=sys.stderr)
        return 1

    emit(f"=== {args.campaign} ===")
    emit(f"host:   {args.host_model}")
    emit(f"player: {args.player_model or args.host_model}")
    emit(f"save:   {args.db}\n")

    host_history: list = []
    player_history: list = []

    result = host.run_sync(
        "Begin the session: read the state and narrate the opening scene to the player.",
        message_history=host_history,
    )
    host_history = result.all_messages()
    narration = result.output
    emit(f"[HOST]\n{narration}\n")

    outcome = "turn limit reached"
    for turn in range(1, args.turns + 1):
        pres = player.run_sync(narration, message_history=player_history)
        player_history = pres.all_messages()
        command = pres.output.strip()
        emit(f"[PLAYER {turn}] {command}")

        hres = host.run_sync(f"The player says: {command}", message_history=host_history)
        host_history = hres.all_messages()
        narration = hres.output
        emit(f"[HOST]\n{narration}\n")

        status = game_status(args.db)
        if status.get("won"):
            outcome = f"WON on turn {turn}"
            break
        if status.get("dead"):
            outcome = f"DIED on turn {turn}"
            break

    final = game_status(args.db)
    emit(f"=== {outcome} ===")
    emit(f"final room: {final.get('room')}  hp: {final.get('hp')}")
    emit(f"\nsave kept at {args.db}")
    emit(f"replay every logged turn with:  adventure-doctor --db {args.db} -v")

    if args.transcript:
        Path(args.transcript).write_text("\n".join(lines))
        print(f"transcript written to {args.transcript}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
