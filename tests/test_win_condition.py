"""Tests for the campaign win condition — WorldInit.win_flag,
TECHNICAL_DETAILS.md §5.6: derived from persisted state like player_dead,
reported once by apply, never gates post-win play."""

import pytest
from pydantic import ValidationError

import engine
from models import RoomGeneration, StateChanges, WorldInit

WIN_FLAG = "reactor_restarted"
WIN_MESSAGE = "The core hums back to life. You've won."


def _world(**overrides):
    base = dict(
        zone_name="Derelict Starship",
        zone_description="A dead ship adrift.",
        global_theme_rules="Everything is cold metal and vacuum.",
        starting_room=RoomGeneration(
            room_name="Cryo Bay",
            description="Frosted pods line the walls.",
            exits=["north"],
        ),
        win_flag=WIN_FLAG,
        win_message=WIN_MESSAGE,
    )
    base.update(overrides)
    return WorldInit(**base)


@pytest.fixture
def win_conn(conn):
    """A seeded campaign whose goal is WIN_FLAG."""
    result = engine.init_game(conn, _world())
    assert result["ok"], result
    return conn


def _apply(conn, **changes):
    result = engine.apply_changes(conn, StateChanges(**changes))
    assert result["ok"], result
    return result


class TestModel:
    def test_win_fields_accepted_together(self):
        assert _world().win_flag == WIN_FLAG

    def test_win_fields_default_to_sandbox(self):
        world = _world(win_flag=None, win_message=None)
        assert world.win_flag is None and world.win_message is None

    def test_win_flag_without_message_rejected(self):
        with pytest.raises(ValidationError, match="set together"):
            _world(win_message=None)

    def test_win_message_without_flag_rejected(self):
        with pytest.raises(ValidationError, match="set together"):
            _world(win_flag=None)

    def test_blank_win_flag_rejected(self):
        with pytest.raises(ValidationError, match="non-empty"):
            _world(win_flag="   ")


class TestSandboxUnaffected:
    def test_default_campaign_has_no_win_condition(self, game_conn):
        state = engine.get_full_state(game_conn)
        assert state["win_flag"] is None
        assert state["game_won"] is False
        assert state["win_message"] is None

    def test_flags_in_sandbox_never_win(self, game_conn):
        result = _apply(game_conn, flags_set={"anything": True})
        assert "game_won" not in result
        assert engine.get_full_state(game_conn)["game_won"] is False


class TestWinning:
    def test_state_exposes_goal_before_winning(self, win_conn):
        state = engine.get_full_state(win_conn)
        assert state["win_flag"] == WIN_FLAG
        assert state["game_won"] is False
        assert state["win_message"] is None  # not leaked pre-win

    def test_apply_reports_the_win_once(self, win_conn):
        result = _apply(win_conn, flags_set={WIN_FLAG: True})
        assert result["game_won"] is True
        assert result["win_message"] == WIN_MESSAGE
        assert result["applied"]["flags_set"] == {WIN_FLAG: True}
        # Later applies are ordinary again; state carries the win instead.
        result = _apply(win_conn, flags_set={"epilogue_flag": True})
        assert "game_won" not in result
        state = engine.get_full_state(win_conn)
        assert state["game_won"] is True
        assert state["win_message"] == WIN_MESSAGE

    def test_unrelated_flag_does_not_win(self, win_conn):
        result = _apply(win_conn, flags_set={"power_restored": True})
        assert "game_won" not in result
        assert engine.get_full_state(win_conn)["game_won"] is False

    def test_free_roam_after_winning(self, win_conn):
        _apply(win_conn, flags_set={WIN_FLAG: True})
        result = engine.move(win_conn, "north")
        assert result["ok"] and result.get("needs_generation"), result

    def test_lethal_victory_reports_both_and_death_still_gates(self, win_conn):
        result = _apply(win_conn, damage_to_player=100, flags_set={WIN_FLAG: True})
        assert result["game_won"] is True
        assert result["player_dead"] is True
        assert engine.move(win_conn, "north") == {"ok": False, "error": "player_dead"}

    def test_reset_clears_the_win(self, win_conn):
        _apply(win_conn, flags_set={WIN_FLAG: True})
        assert engine.reset_game(win_conn)["ok"]  # replays the stored campaign
        state = engine.get_full_state(win_conn)
        assert state["win_flag"] == WIN_FLAG  # goal survives the reset
        assert state["game_won"] is False  # progress does not
