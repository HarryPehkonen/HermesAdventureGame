"""Every predefined campaign in campaigns/ must be a valid, playable
WorldInit with a win condition."""

import json
from pathlib import Path

import pytest

import engine
from models import WorldInit

CAMPAIGN_DIR = Path(__file__).parent.parent / "campaigns"
CAMPAIGNS = sorted(CAMPAIGN_DIR.glob("*.json"))


def test_campaigns_exist():
    assert len(CAMPAIGNS) >= 6


@pytest.mark.parametrize("path", CAMPAIGNS, ids=lambda p: p.stem)
def test_campaign_is_valid_and_playable(path, conn):
    world = WorldInit.model_validate(json.loads(path.read_text()))
    # Every predefined campaign ships with a goal (sandboxes need no file —
    # that's what plain `init` gives you).
    assert world.win_flag and world.win_message

    result = engine.init_game(conn, world)
    assert result == {"ok": True, "new_game": True}
    state = engine.get_full_state(conn)
    assert state["zone"]["zone_name"] == world.zone_name
    assert state["win_flag"] == world.win_flag
    assert state["game_won"] is False
    assert state["room"]["exits"], "starting room must not be softlocked"
    # No starting room ships pre-locked: the player can always take a first step.
    assert any(not e["locked"] for e in state["room"]["exits"])
