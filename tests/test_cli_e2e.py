"""End-to-end smoke test driving game.py as a subprocess, the way Hermes
Agent (per SKILL.md) will.
"""

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
GAME_PY = str(REPO_ROOT / "scripts" / "game.py")


def run_cli(args, env, stdin_data=""):
    # Default to an explicit empty pipe (not inherited stdin) so `init`/`reset`
    # deterministically take the no-payload path regardless of how pytest runs.
    proc = subprocess.run(
        [sys.executable, GAME_PY, *args],
        input=stdin_data,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, f"exit {proc.returncode}, stderr: {proc.stderr}"
    lines = proc.stdout.strip().splitlines()
    assert len(lines) == 1, f"expected exactly one JSON line, got: {proc.stdout!r}"
    return json.loads(lines[0])


def test_full_turn_cycle(tmp_path, monkeypatch):
    db_path = str(tmp_path / "e2e.db")
    env = {**__import__("os").environ, "HERMES_DB_PATH": db_path}

    init_result = run_cli(["init"], env)
    assert init_result == {"ok": True, "new_game": True}

    state = run_cli(["state"], env)
    assert state["ok"] is True
    assert state["room"]["coordinates"] == {"x": 0, "y": 0, "z": 0}

    move_result = run_cli(["move", "north"], env)
    assert move_result["ok"] is True
    assert move_result["needs_generation"] is True

    room_json = json.dumps(
        {
            "room_name": "Steam Landing",
            "description": "A narrow steel landing streaked with condensation.",
            "exits": ["east"],
            "entities": [
                {
                    "name": "Copper Coil",
                    "type": "item",
                    "description": "A coil of copper wire.",
                    "can_pickup": True,
                    "is_blocking": False,
                    "solution_condition": None,
                    "blocks_direction": None,
                    "traits": ["metallic"],
                }
            ],
        }
    )
    create_result = run_cli(["create-room", "north"], env, stdin_data=room_json)
    assert create_result["ok"] is True
    assert create_result["room"]["name"] == "Steam Landing"
    entity_id = create_result["room"]["entities"][0]["id"]

    state2 = run_cli(["state"], env)
    assert state2["room"]["name"] == "Steam Landing"

    # Entities in state must be flat — same shape the agent pipes to
    # create-room/apply (can_pickup, is_blocking, ...) — never nested under
    # a "properties" key. A live session once had to guess-and-retry when
    # these two shapes disagreed.
    coil = state2["room"]["entities"][0]
    assert "properties" not in coil
    assert coil["can_pickup"] is True
    assert coil["is_blocking"] is False
    assert coil["traits"] == ["metallic"]

    take_result = run_cli(["take", str(entity_id)], env)
    assert take_result["ok"] is True

    apply_json = json.dumps({"flags_set": {"picked_up_coil": True}})
    apply_result = run_cli(["apply"], env, stdin_data=apply_json)
    assert apply_result["ok"] is True
    assert apply_result["applied"]["flags_set"] == {"picked_up_coil": True}

    log_json = json.dumps({"player_input": "look", "narrative": "A landing streaked with steam."})
    log_result = run_cli(["log"], env, stdin_data=log_json)
    assert log_result == {"ok": True}

    state3 = run_cli(["state"], env)
    assert any(e["id"] == entity_id for e in state3["inventory"])
    assert state3["flags"] == {"picked_up_coil": True}
    assert state3["recent_turns"] == [{"player_input": "look", "narrative": "A landing streaked with steam."}]

    reset_result = run_cli(["reset"], env)
    assert reset_result == {"ok": True, "new_game": True}

    state4 = run_cli(["state"], env)
    assert state4["room"]["coordinates"] == {"x": 0, "y": 0, "z": 0}
    assert state4["inventory"] == []


def test_state_logs_previous_turn_via_stdin(tmp_path):
    import os

    db_path = str(tmp_path / "e2e_look_log.db")
    env = {**os.environ, "HERMES_DB_PATH": db_path}
    run_cli(["init"], env)

    # A pure look/inventory turn never calls `move`/`apply`/`log` — its
    # narrative only ever reaches turn_log via the *next* turn's `state`
    # call, piped as {"player_input", "narrative"} on stdin.
    payload = json.dumps({"player_input": "look around", "narrative": "Steam hisses overhead."})
    state = run_cli(["state"], env, stdin_data=payload)
    assert state["ok"] is True
    assert state["logged_previous_turn"] is True
    assert "log_error" not in state
    assert state["recent_turns"] == [{"player_input": "look around", "narrative": "Steam hisses overhead."}]

    # No payload (the normal case at the very start of a session) logs nothing.
    state2 = run_cli(["state"], env)
    assert state2["logged_previous_turn"] is False
    assert state2["recent_turns"] == [{"player_input": "look around", "narrative": "Steam hisses overhead."}]


def test_state_with_bad_log_payload_still_returns_state(tmp_path):
    import os

    db_path = str(tmp_path / "e2e_bad_log.db")
    env = {**os.environ, "HERMES_DB_PATH": db_path}
    run_cli(["init"], env)

    # Malformed JSON must not block the state read it's piggybacking on.
    state = run_cli(["state"], env, stdin_data="{not valid json")
    assert state["ok"] is True
    assert state["logged_previous_turn"] is False
    assert state["log_error"].startswith("invalid_json")

    # Same for JSON that doesn't match {"player_input", "narrative"}.
    state2 = run_cli(["state"], env, stdin_data=json.dumps({"wrong_field": "oops"}))
    assert state2["ok"] is True
    assert state2["logged_previous_turn"] is False
    assert state2["log_error"].startswith("validation_error")


def test_campaign_flag_resolves_from_any_cwd(tmp_path):
    """--campaign resolves against the skill directory, not the cwd — the
    agent's session runs from wherever it happens to be."""
    import os

    db_path = str(tmp_path / "e2e_campaign.db")
    env = {**os.environ, "HERMES_DB_PATH": db_path}

    # cwd deliberately somewhere with no campaigns/ directory.
    proc = subprocess.run(
        [sys.executable, GAME_PY, "init", "--campaign", "pirate_islands"],
        input="", capture_output=True, text=True, env=env, cwd=str(tmp_path),
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip()) == {"ok": True, "new_game": True}

    state = run_cli(["state"], env)
    assert state["zone"]["zone_name"] == "The Scattered Teeth"


def test_unknown_campaign_lists_the_real_ones(tmp_path):
    import os

    db_path = str(tmp_path / "e2e_bad_campaign.db")
    env = {**os.environ, "HERMES_DB_PATH": db_path}

    result = run_cli(["init", "--campaign", "no_such_campaign"], env)
    assert result["ok"] is False
    assert result["error"] == "unknown_campaign"
    assert "pirate_islands" in result["details"]


def test_db_flag_isolates_from_the_default_save(tmp_path):
    """--db must leave the save the engine would otherwise use completely
    untouched — that's what makes a throwaway/self-play game safe."""
    import os

    real_save = tmp_path / "real.db"
    throwaway = tmp_path / "throwaway.db"
    # HERMES_DB_PATH stands in for "the save normal play would use".
    env = {**os.environ, "HERMES_DB_PATH": str(real_save)}

    run_cli(["init", "--campaign", "clockwork_spire"], env)
    assert real_save.exists()
    before = real_save.read_bytes()

    # A whole separate game, played against the throwaway file.
    assert run_cli(["--db", str(throwaway), "init", "--campaign", "amber_tomb"], env) == {
        "ok": True,
        "new_game": True,
    }
    assert throwaway.exists()

    # The flag is accepted on either side of the subcommand.
    after_sub = run_cli(["state", "--db", str(throwaway)], env)
    assert after_sub["zone"]["zone_name"] == "Tomb of the Amber Pharaoh"

    # ...and the real save is byte-for-byte unchanged.
    assert real_save.read_bytes() == before
    assert run_cli(["state"], env)["zone"]["zone_name"] == "The Mechanical Spire"


def test_invalid_json_on_stdin_returns_ok_false(tmp_path):
    import os

    db_path = str(tmp_path / "e2e_invalid.db")
    env = {**os.environ, "HERMES_DB_PATH": db_path}
    run_cli(["init"], env)

    result = run_cli(["create-room", "north"], env, stdin_data="{not valid json")
    assert result["ok"] is False
    assert result["error"] in ("invalid_json", "validation_error")


def test_custom_world_via_cli(tmp_path):
    import os

    db_path = str(tmp_path / "e2e_world.db")
    env = {**os.environ, "HERMES_DB_PATH": db_path}

    world_json = json.dumps(
        {
            "zone_name": "Sunken Archive",
            "zone_description": "A flooded library-city beneath a glass sea.",
            "global_theme_rules": "Waterlogged stone, drowned books, bioluminescence. No fire.",
            "starting_room": {
                "room_name": "Reading Well",
                "description": "A circular chamber whose shelves spiral down into dark water.",
                "exits": ["down"],
                "entities": [],
            },
            "starting_inventory": [
                {
                    "name": "sealed lantern",
                    "type": "item",
                    "description": "A brass lantern holding a colony of glowing algae.",
                    "can_pickup": True,
                    "is_blocking": False,
                    "solution_condition": None,
                    "blocks_direction": None,
                    "traits": ["light-source", "waterproof"],
                }
            ],
        }
    )

    init_result = run_cli(["init"], env, stdin_data=world_json)
    assert init_result == {"ok": True, "new_game": True}

    state = run_cli(["state"], env)
    assert state["zone"]["zone_name"] == "Sunken Archive"
    assert state["room"]["name"] == "Reading Well"
    assert [e["name"] for e in state["inventory"]] == ["sealed lantern"]

    # init with a payload on an already-seeded DB is rejected loudly.
    again = run_cli(["init"], env, stdin_data=world_json)
    assert again["ok"] is False and again["error"] == "already_seeded"

    # Plain init stays idempotent.
    plain = run_cli(["init"], env)
    assert plain == {"ok": True, "new_game": False}

    # export-world returns the seed payload, byte-comparable field-wise.
    exported = run_cli(["export-world"], env)
    assert exported["ok"] is True
    assert exported["world"]["zone_name"] == "Sunken Archive"

    # Payload-less reset replays the same campaign.
    reset_result = run_cli(["reset"], env)
    assert reset_result == {"ok": True, "new_game": True}
    state2 = run_cli(["state"], env)
    assert state2["zone"]["zone_name"] == "Sunken Archive"
    assert [e["name"] for e in state2["inventory"]] == ["sealed lantern"]


def test_state_before_init_reports_not_initialized(tmp_path):
    import os

    db_path = str(tmp_path / "e2e_uninit.db")
    env = {**os.environ, "HERMES_DB_PATH": db_path}

    result = run_cli(["state"], env)
    assert result == {
        "ok": False,
        "error": "not_initialized",
        "details": "player_state is not seeded — run `python scripts/game.py init` first",
    }
