"""Session 0 / custom-campaign seeding: WorldInit validation, init/reset
semantics, replay, and export (TECHNICAL_DETAILS.md §1 seeding + §2)."""

import pydantic
import pytest

import config
import database
import engine
from models import GeneratedEntity, RoomGeneration, StateChanges, WorldInit


def _starship_world(**overrides) -> WorldInit:
    defaults = dict(
        zone_name="Derelict Starship 'Vesper'",
        zone_description="A drifting colony ship, power failing deck by deck.",
        global_theme_rules="Shipboard spaces only; failing but plausible technology; no magic.",
        starting_room=RoomGeneration(
            room_name="Cryo Bay 7",
            description="Rows of frosted cryopods line the walls under pulsing red light.",
            exits=["north", "east"],
            entities=[
                GeneratedEntity(
                    name="Jammed Bulkhead",
                    type="obstacle",
                    description="A bulkhead door wedged half-open.",
                    can_pickup=False,
                    is_blocking=True,
                    solution_condition="Needs the manual release lever forced.",
                    blocks_direction="east",
                )
            ],
        ),
        starting_inventory=[
            GeneratedEntity(
                name="maintenance multitool",
                type="item",
                description="A worn multitool from the ship's engineering locker.",
                can_pickup=True,
                is_blocking=False,
                traits=["metallic", "versatile"],
            )
        ],
    )
    defaults.update(overrides)
    return WorldInit(**defaults)


# --- model validation ---------------------------------------------------

def test_world_init_valid_payload():
    world = _starship_world()
    assert world.starting_room.room_name == "Cryo Bay 7"


def test_world_init_rejects_room_with_no_exits():
    with pytest.raises(pydantic.ValidationError, match="at least one exit"):
        _starship_world(
            starting_room=RoomGeneration(room_name="Box", description="Sealed.", exits=[])
        )


def test_world_init_rejects_non_item_in_inventory():
    npc = GeneratedEntity(
        name="Ship's Cat", type="npc", description="A cat.",
        can_pickup=False, is_blocking=False,
    )
    with pytest.raises(pydantic.ValidationError, match="type 'item'"):
        _starship_world(starting_inventory=[npc])


def test_world_init_rejects_blocks_direction_not_in_seed_exits():
    hatch = GeneratedEntity(
        name="Hatch", type="obstacle", description="Desc.",
        can_pickup=False, is_blocking=True, blocks_direction="up",
    )
    with pytest.raises(pydantic.ValidationError, match="blocks_direction"):
        _starship_world(
            starting_room=RoomGeneration(
                room_name="Bay", description="Desc.", exits=["north"], entities=[hatch]
            )
        )


# --- engine seeding -------------------------------------------------------

def test_init_with_custom_world(conn):
    result = engine.init_game(conn, _starship_world())
    assert result == {"ok": True, "new_game": True}

    state = engine.get_full_state(conn)
    assert state["zone"]["zone_name"] == "Derelict Starship 'Vesper'"
    assert state["room"]["name"] == "Cryo Bay 7"
    assert state["room"]["coordinates"] == {"x": 0, "y": 0, "z": 0}
    assert [e["name"] for e in state["inventory"]] == ["maintenance multitool"]
    assert [e["name"] for e in state["room"]["entities"]] == ["Jammed Bulkhead"]

    exits = {e["direction"]: e for e in state["room"]["exits"]}
    assert set(exits) == {"north", "east"}
    assert exits["east"]["locked"] is True
    assert exits["east"]["lock_condition"] == "Needs the manual release lever forced."
    assert exits["north"]["locked"] is False


def test_init_payload_on_seeded_db_is_rejected(game_conn):
    result = engine.init_game(game_conn, _starship_world())
    assert result["ok"] is False
    assert result["error"] == "already_seeded"
    # DB untouched: still the default campaign.
    assert engine.get_full_state(game_conn)["room"]["name"] == config.START_ROOM_NAME


def test_reset_replays_stored_custom_world(conn):
    engine.init_game(conn, _starship_world())
    # Make progress: take damage, explore, log.
    engine.apply_changes(conn, StateChanges(damage_to_player=60))
    engine.create_room(
        conn, "north", RoomGeneration(room_name="Corridor 12", description="Desc.", exits=[])
    )
    engine.log_turn(conn, "go north", "You drift into the corridor.")

    result = engine.reset_game(conn)  # no payload → replay same campaign
    assert result == {"ok": True, "new_game": True}

    state = engine.get_full_state(conn)
    assert state["zone"]["zone_name"] == "Derelict Starship 'Vesper'"
    assert state["room"]["name"] == "Cryo Bay 7"
    assert state["hp"] == config.STARTING_HP
    assert [e["name"] for e in state["inventory"]] == ["maintenance multitool"]
    assert state["recent_turns"] == []
    # Explored rooms are gone; the world regenerates on replay.
    assert database.get_node_by_coords(conn, 0, 1, 0) is None


def test_reset_with_new_payload_switches_campaign(conn):
    engine.init_game(conn)  # default Mechanical Spire
    result = engine.reset_game(conn, _starship_world())
    assert result == {"ok": True, "new_game": True}

    state = engine.get_full_state(conn)
    assert state["zone"]["zone_name"] == "Derelict Starship 'Vesper'"

    # And a payload-less reset now replays the NEW campaign, not the default.
    engine.reset_game(conn)
    assert engine.get_full_state(conn)["zone"]["zone_name"] == "Derelict Starship 'Vesper'"


def test_default_init_stores_replayable_world(conn):
    engine.init_game(conn)  # no payload → built-in default
    exported = engine.export_world(conn)
    assert exported["ok"] is True
    world = WorldInit.model_validate(exported["world"])
    assert world.zone_name == config.ZONE_NAME
    assert world.starting_room.room_name == config.START_ROOM_NAME


def test_export_world_roundtrip(conn, tmp_path):
    original = _starship_world()
    engine.init_game(conn, original)

    exported = engine.export_world(conn)
    assert exported["ok"] is True

    # A friend seeds a fresh DB from the exported payload.
    friend_conn = database.get_connection(str(tmp_path / "friend.db"))
    database.init_schema(friend_conn)
    result = engine.init_game(friend_conn, WorldInit.model_validate(exported["world"]))
    assert result == {"ok": True, "new_game": True}

    friend_state = engine.get_full_state(friend_conn)
    assert friend_state["zone"]["zone_name"] == original.zone_name
    assert friend_state["room"]["name"] == original.starting_room.room_name
    assert [e["name"] for e in friend_state["inventory"]] == ["maintenance multitool"]
    friend_conn.close()


def test_export_world_before_init(conn):
    assert engine.export_world(conn) == {"ok": False, "error": "not_initialized"}
