"""Tests for flag-linked environmental obstacles — cleared_by_flag,
TECHNICAL_DETAILS.md §5.5: the engine, not the agent, resolves obstacles
whose flag is set, lazily where needed."""

import json

import pytest
from pydantic import ValidationError

import config
import database
import doctor
import engine
from models import GeneratedEntity, RoomGeneration, StateChanges


def _darkness(**overrides):
    base = dict(
        name="wall of darkness",
        type="obstacle",
        description="An impenetrable dark.",
        can_pickup=False,
        is_blocking=True,
        solution_condition="restore power",
        cleared_by_flag="power_restored",
    )
    base.update(overrides)
    return GeneratedEntity(**base)


def _create_room(conn, direction, name, exits=(), entities=()):
    result = engine.move(conn, direction)
    assert result.get("needs_generation"), result
    result = engine.create_room(
        conn,
        direction,
        RoomGeneration(
            room_name=name,
            description="A room.",
            exits=list(exits),
            entities=list(entities),
        ),
    )
    assert result["ok"], result
    return result


def _set_flag(conn, flag="power_restored"):
    result = engine.apply_changes(conn, StateChanges(flags_set={flag: True}))
    assert result["ok"], result
    return result


def _entity_row(conn, name):
    return conn.execute("SELECT * FROM entities WHERE name = ?;", (name,)).fetchone()


def _props(conn, name):
    return json.loads(_entity_row(conn, name)["properties_json"])


class TestModel:
    def test_cleared_by_flag_requires_obstacle(self):
        with pytest.raises(ValidationError, match="cleared_by_flag"):
            GeneratedEntity(
                name="lamp",
                type="item",
                description="A lamp.",
                can_pickup=True,
                is_blocking=False,
                cleared_by_flag="power_restored",
            )

    def test_cleared_by_flag_stored_in_properties(self, game_conn):
        _create_room(game_conn, "north", "Dark Hall", entities=[_darkness()])
        props = _props(game_conn, "wall of darkness")
        assert props["cleared_by_flag"] == "power_restored"
        assert props["is_cleared"] is False


class TestAutoClear:
    def test_flag_set_clears_current_room_obstacle(self, game_conn):
        _create_room(
            game_conn, "north", "Dark Hall", exits=["north"],
            entities=[_darkness(blocks_direction="north")],
        )
        assert engine.move(game_conn, "north")["error"] == "locked"
        ent_id = _entity_row(game_conn, "wall of darkness")["id"]

        result = _set_flag(game_conn)
        assert result["applied"]["auto_cleared_obstacles"] == [ent_id]
        assert _props(game_conn, "wall of darkness")["is_cleared"] is True
        # the darkness no longer locks the north frontier
        assert engine.move(game_conn, "north").get("needs_generation")

    def test_unrelated_flag_clears_nothing(self, game_conn):
        _create_room(game_conn, "north", "Dark Hall", entities=[_darkness()])
        result = _set_flag(game_conn, flag="angered_repair_unit")
        assert "auto_cleared_obstacles" not in result["applied"]
        assert _props(game_conn, "wall of darkness")["is_cleared"] is False

    def test_far_room_obstacle_clears_lazily_on_entry(self, game_conn):
        _create_room(game_conn, "north", "Dark Hall", entities=[_darkness()])
        assert engine.move(game_conn, "south")["ok"]  # back to the start room
        _set_flag(game_conn)
        # Lazy: the flag alone does not touch the far room…
        assert _props(game_conn, "wall of darkness")["is_cleared"] is False
        # …entering it does.
        result = engine.move(game_conn, "north")
        assert result["ok"]
        assert result["auto_cleared"] == [_entity_row(game_conn, "wall of darkness")["id"]]
        assert _props(game_conn, "wall of darkness")["is_cleared"] is True

    def test_locked_passage_opens_when_blocker_is_beyond_it(self, game_conn):
        # The headline multi-room case: the blocker sits on the far side of
        # the very passage it locks. Dark Hall's darkness seals its south
        # exit — the passage back to the start room — behind the player.
        _create_room(
            game_conn, "north", "Dark Hall", exits=["east"],
            entities=[_darkness(blocks_direction="south")],
        )
        assert engine.move(game_conn, "south")["error"] == "locked"
        # Loop around the block: Dark Hall -> east -> south -> west lands on
        # the start room's east frontier and links up.
        _create_room(game_conn, "east", "Gear Walk", exits=["south"])
        _create_room(game_conn, "south", "Steam Duct", exits=["west"])
        result = engine.move(game_conn, "west")
        assert result["ok"] and result["room"]["name"] == config.START_ROOM_NAME

        # From the start room the north passage is locked by an obstacle in
        # ANOTHER room. Setting the flag here clears nothing yet…
        assert engine.move(game_conn, "north")["error"] == "locked"
        result = _set_flag(game_conn)
        assert "auto_cleared_obstacles" not in result["applied"]
        # …but moving through the locked passage resolves it and succeeds.
        result = engine.move(game_conn, "north")
        assert result["ok"] and result["moved"], result
        assert result["room"]["name"] == "Dark Hall"
        assert _props(game_conn, "wall of darkness")["is_cleared"] is True
        locked_rows = game_conn.execute(
            "SELECT COUNT(*) AS c FROM edges WHERE is_locked = 1;"
        ).fetchone()["c"]
        assert locked_rows == 0

    def test_generation_safety_net(self, game_conn):
        _set_flag(game_conn)
        result = _create_room(game_conn, "north", "Dark Hall", entities=[_darkness()])
        ent_id = _entity_row(game_conn, "wall of darkness")["id"]
        assert result["auto_cleared"] == [ent_id]
        assert _props(game_conn, "wall of darkness")["is_cleared"] is True

    def test_generation_context_includes_flags(self, game_conn):
        _set_flag(game_conn)
        result = engine.move(game_conn, "north")
        assert result["needs_generation"]
        assert result["context"]["flags"] == {"power_restored": True}


class TestDoctorIntegration:
    def test_uncleared_satisfied_obstacle_in_current_room_is_integrity(self, game_conn):
        engine.log_turn(game_conn, "look", "You look.")
        _create_room(game_conn, "north", "Dark Hall", entities=[_darkness()])
        engine.log_turn(game_conn, "go north", "You enter the dark hall.")
        assert doctor.run_checks(game_conn) == []
        # Set the flag behind the engine's back so auto-clear never ran.
        with game_conn:
            database.set_player_flags(game_conn, {"power_restored": True})
        findings = doctor.run_checks(game_conn)
        assert any("should have auto-cleared" in f.message for f in findings)
        assert {f.kind for f in findings} == {"integrity"}

    def test_satisfied_obstacle_in_far_room_is_fine(self, game_conn):
        engine.log_turn(game_conn, "look", "You look.")
        _create_room(game_conn, "north", "Dark Hall", entities=[_darkness()])
        engine.log_turn(game_conn, "go north", "You enter the dark hall.")
        assert engine.move(game_conn, "south")["ok"]
        engine.log_turn(game_conn, "go south", "Back to the rust-chamber.")
        with game_conn:
            database.set_player_flags(game_conn, {"power_restored": True})
        # Lazy resolution pending in a room the player is not in: normal.
        assert doctor.run_checks(game_conn) == []
