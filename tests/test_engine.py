"""Business-logic tests: TECHNICAL_DETAILS.md §4 (movement/generation) and
§5 (apply validation rules).
"""

import config
import database
import engine
from models import GeneratedEntity, RoomGeneration, StateChanges


def test_seed_creates_start_room(game_conn):
    state = engine.get_full_state(game_conn)
    assert state["room"]["name"] == config.START_ROOM_NAME
    assert state["room"]["coordinates"] == {"x": 0, "y": 0, "z": 0}
    assert {e["direction"] for e in state["room"]["exits"]} == set(config.START_ROOM_EXITS)
    for e in state["room"]["exits"]:
        assert e["locked"] is False
        assert e["generated"] is False
    assert state["hp"] == config.STARTING_HP
    assert state["player_dead"] is False
    assert state["inventory"] == []
    assert state["recent_turns"] == []


def test_move_no_exit_where_no_edge_row(game_conn):
    # START_ROOM_EXITS is north/east/up — south is a genuine wall.
    assert "south" not in config.START_ROOM_EXITS
    result = engine.move(game_conn, "south")
    assert result == {"ok": False, "error": "no_exit"}


def test_move_into_frontier_needs_generation(game_conn):
    result = engine.move(game_conn, "north")
    assert result["ok"] is True
    assert result["needs_generation"] is True
    ctx = result["context"]
    assert ctx["direction_of_travel"] == "north"
    assert ctx["target_coordinates"] == {"x": 0, "y": 1, "z": 0}
    assert ctx["exiting_room"]["name"] == config.START_ROOM_NAME
    # The only existing neighbor of (0,1,0) is the start room itself, to the
    # south, which correctly has a matching north-facing frontier.
    assert ctx["neighbors"] == [
        {
            "direction_from_target": "south",
            "name": config.START_ROOM_NAME,
            "description": config.START_ROOM_DESCRIPTION.split(". ")[0].strip() + (
                "" if config.START_ROOM_DESCRIPTION.split(". ")[0].strip().endswith((".", "!", "?")) else "."
            ),
            "has_exit_facing_target": True,
        }
    ]


def test_create_room_basic_and_forces_return_direction(game_conn):
    room = RoomGeneration(
        room_name="Steam Landing",
        description="A narrow steel landing streaked with condensation.",
        exits=["east"],
    )
    result = engine.create_room(game_conn, "north", room)

    assert result["ok"] is True
    view = result["room"]
    assert view["name"] == "Steam Landing"
    exits_by_dir = {e["direction"]: e for e in view["exits"]}
    assert set(exits_by_dir) == {"south", "east"}  # south forced in
    assert exits_by_dir["south"]["generated"] is True
    assert exits_by_dir["east"]["generated"] is False

    # start room's north edge now resolves to the new room
    edge = database.get_edge(game_conn, 1, "north")
    assert edge["to_node_id"] == view["id"]

    # player has moved
    state = engine.get_full_state(game_conn)
    assert state["room"]["name"] == "Steam Landing"


def test_create_room_rejects_wall_direction(game_conn):
    result = engine.create_room(game_conn, "south", RoomGeneration(room_name="X", description="Y", exits=[]))
    assert result == {"ok": False, "error": "no_exit"}


def test_create_room_rejects_already_generated(game_conn):
    engine.create_room(
        game_conn, "north", RoomGeneration(room_name="Steam Landing", description="Desc.", exits=[])
    )
    back = engine.move(game_conn, "south")
    assert back["ok"] is True and back["moved"] is True

    result = engine.create_room(
        game_conn, "north", RoomGeneration(room_name="Other", description="Nope.", exits=[])
    )
    assert result == {"ok": False, "error": "already_generated"}


def test_move_already_generated_room_moves_directly(game_conn):
    engine.create_room(
        game_conn, "north", RoomGeneration(room_name="Steam Landing", description="Desc.", exits=[])
    )
    engine.move(game_conn, "south")

    result = engine.move(game_conn, "north")
    assert result["ok"] is True
    assert result["moved"] is True
    assert "needs_generation" not in result
    assert result["room"]["name"] == "Steam Landing"


def test_create_room_pairs_declared_exit_with_existing_neighbor_frontier(game_conn):
    # Path 1: start -> north -> Room A, declaring an "east" exit (frontier at (1,1,0))
    r = engine.create_room(
        game_conn, "north", RoomGeneration(room_name="Room A", description="Desc A.", exits=["east"])
    )
    room_a_id = r["room"]["id"]
    engine.move(game_conn, "south")

    # Path 2: start -> east -> Room E -> north -> Room D
    engine.create_room(
        game_conn, "east", RoomGeneration(room_name="Room E", description="Desc E.", exits=["north"])
    )
    r3 = engine.create_room(
        game_conn, "north", RoomGeneration(room_name="Room D", description="Desc D.", exits=["west"])
    )

    room_d = r3["room"]
    west_exit = next(e for e in room_d["exits"] if e["direction"] == "west")
    assert west_exit["generated"] is True  # paired with Room A's pre-existing frontier

    edge_a_east = database.get_edge(game_conn, room_a_id, "east")
    assert edge_a_east["to_node_id"] == room_d["id"]


def test_create_room_drops_exit_when_neighbor_has_wall(game_conn):
    # Room A (north of start) created WITHOUT an east exit -> a wall there.
    r = engine.create_room(
        game_conn, "north", RoomGeneration(room_name="Room A", description="Desc A.", exits=[])
    )
    room_a_id = r["room"]["id"]
    engine.move(game_conn, "south")

    engine.create_room(
        game_conn, "east", RoomGeneration(room_name="Room E", description="Desc E.", exits=["north"])
    )
    r3 = engine.create_room(
        game_conn, "north", RoomGeneration(room_name="Room D", description="Desc D.", exits=["west"])
    )

    room_d = r3["room"]
    assert "west" not in {e["direction"] for e in room_d["exits"]}
    assert database.get_edge(game_conn, room_a_id, "east") is None


def test_move_links_dangling_mutual_frontiers(game_conn):
    """Defensive case from TECHNICAL_DETAILS.md §4 step 2: if a node somehow
    already exists at the target coordinates with its own matching frontier
    still unresolved, `move` links both sides rather than trying to generate
    a duplicate room."""
    start = database.get_player_state(game_conn)
    start_node = database.get_node(game_conn, start["current_node_id"])
    ghost_id = database.insert_node(
        game_conn, start_node["x"], start_node["y"] + 1, start_node["z"], "Ghost Room", "Shouldn't exist yet."
    )
    database.insert_edge(game_conn, ghost_id, "south")
    game_conn.commit()

    result = engine.move(game_conn, "north")

    assert result["ok"] is True
    assert result["moved"] is True
    assert result["room"]["id"] == ghost_id
    edge_a = database.get_edge(game_conn, start_node["id"], "north")
    assert edge_a["to_node_id"] == ghost_id
    edge_b = database.get_edge(game_conn, ghost_id, "south")
    assert edge_b["to_node_id"] == start_node["id"]


def test_move_locked_mutual_frontier_links_but_blocks(game_conn):
    """A lock on the far side of a loop-closing frontier must block passage:
    link the pair and copy the lock, but do NOT move the player (§3.3 — a
    lock blocks passage in either direction)."""
    start = database.get_player_state(game_conn)
    start_node = database.get_node(game_conn, start["current_node_id"])
    ghost_id = database.insert_node(
        game_conn, start_node["x"], start_node["y"] + 1, start_node["z"],
        "Sealed Landing", "A landing behind a hatch.",
    )
    hatch_id = database.insert_entity(
        game_conn, ghost_id, "room", "Rusted Hatch", "obstacle",
        "A heavy hatch.", {"is_blocking": True, "is_cleared": False},
    )
    database.insert_edge(
        game_conn, ghost_id, "south", is_locked=True,
        lock_condition="Requires a crowbar.", blocking_entity_id=hatch_id,
    )
    game_conn.commit()

    result = engine.move(game_conn, "north")

    assert result == {
        "ok": False,
        "error": "locked",
        "lock_condition": "Requires a crowbar.",
    }
    # Player did not move.
    assert database.get_player_state(game_conn)["current_node_id"] == start_node["id"]
    # Both rows are linked and locked by the same obstacle.
    edge_a = database.get_edge(game_conn, start_node["id"], "north")
    edge_b = database.get_edge(game_conn, ghost_id, "south")
    assert edge_a["to_node_id"] == ghost_id and edge_a["is_locked"] == 1
    assert edge_b["to_node_id"] == start_node["id"] and edge_b["is_locked"] == 1
    assert edge_a["blocking_entity_id"] == hatch_id

    # A later move re-checks the (now linked) locked edge — still blocked.
    again = engine.move(game_conn, "north")
    assert again["ok"] is False and again["error"] == "locked"


def test_move_neighbor_wall_closes_our_frontier(game_conn):
    start = database.get_player_state(game_conn)
    start_node = database.get_node(game_conn, start["current_node_id"])
    database.insert_node(
        game_conn, start_node["x"], start_node["y"] + 1, start_node["z"], "Sealed Room", "No way in."
    )
    game_conn.commit()

    result = engine.move(game_conn, "north")

    assert result == {"ok": False, "error": "no_exit"}
    assert database.get_edge(game_conn, start_node["id"], "north") is None


def test_obstacle_blocks_direction_locks_edge_and_move(game_conn):
    hatch = GeneratedEntity(
        name="Rusted Hatch",
        type="obstacle",
        description="A heavy rusted hatch.",
        can_pickup=False,
        is_blocking=True,
        solution_condition="Requires a crowbar to pry open.",
        blocks_direction="east",
    )
    room = RoomGeneration(room_name="Landing", description="Desc.", exits=["east"], entities=[hatch])
    result = engine.create_room(game_conn, "north", room)

    view = result["room"]
    east_exit = next(e for e in view["exits"] if e["direction"] == "east")
    assert east_exit["locked"] is True
    assert east_exit["lock_condition"] == "Requires a crowbar to pry open."

    entity_id = view["entities"][0]["id"]
    edge_row = database.get_edge(game_conn, view["id"], "east")
    assert edge_row["blocking_entity_id"] == entity_id

    move_result = engine.move(game_conn, "east")
    assert move_result == {
        "ok": False,
        "error": "locked",
        "lock_condition": "Requires a crowbar to pry open.",
    }


def test_apply_clears_obstacle_and_unlocks_edge(game_conn):
    hatch = GeneratedEntity(
        name="Rusted Hatch",
        type="obstacle",
        description="A heavy rusted hatch.",
        can_pickup=False,
        is_blocking=True,
        solution_condition="Requires a crowbar to pry open.",
        blocks_direction="east",
    )
    room = RoomGeneration(room_name="Landing", description="Desc.", exits=["east"], entities=[hatch])
    result = engine.create_room(game_conn, "north", room)
    entity_id = result["room"]["entities"][0]["id"]

    apply_result = engine.apply_changes(
        game_conn, StateChanges(obstacles_cleared_entity_ids=[entity_id])
    )

    assert apply_result["ok"] is True
    assert apply_result["applied"]["obstacles_cleared_entity_ids"] == [entity_id]
    assert apply_result["applied"]["edges_unlocked"] == 1
    assert apply_result["rejected"] == []

    edge_row = database.get_edge(game_conn, result["room"]["id"], "east")
    assert edge_row["is_locked"] == 0

    move_result = engine.move(game_conn, "east")
    assert move_result["ok"] is True
    assert move_result["needs_generation"] is True


def test_apply_rejects_invalid_entity_but_applies_valid_ones(game_conn):
    coil = GeneratedEntity(
        name="Copper Coil",
        type="item",
        description="A coil of copper wire.",
        can_pickup=True,
        is_blocking=False,
    )
    room = RoomGeneration(room_name="Storage", description="Desc.", exits=[], entities=[coil])
    result = engine.create_room(game_conn, "north", room)
    item_id = result["room"]["entities"][0]["id"]
    bogus_id = item_id + 999

    apply_result = engine.apply_changes(
        game_conn, StateChanges(items_added_to_inventory=[item_id, bogus_id])
    )

    assert apply_result["applied"]["items_added_to_inventory"] == [item_id]
    assert len(apply_result["rejected"]) == 1
    assert apply_result["rejected"][0]["reason"] == "entity_not_found"

    state = engine.get_full_state(game_conn)
    assert any(e["id"] == item_id for e in state["inventory"])
    assert not any(e["id"] == item_id for e in state["room"]["entities"])


def test_apply_awards_item_regardless_of_can_pickup(game_conn):
    """`apply` is the referee's channel: a can_pickup=false item can still be
    awarded there (the agent judged the acquisition plausible), while the
    mechanical `take` command keeps rejecting it."""
    gear = GeneratedEntity(
        name="Brass Gear", type="item", description="Bolted to the machine.",
        can_pickup=False, is_blocking=False,
    )
    room = RoomGeneration(room_name="Gear Hall", description="Desc.", exits=[], entities=[gear])
    result = engine.create_room(game_conn, "north", room)
    gear_id = result["room"]["entities"][0]["id"]

    take_result = engine.take_item(game_conn, gear_id)
    assert take_result == {"ok": False, "error": "cannot_be_picked_up"}

    apply_result = engine.apply_changes(
        game_conn, StateChanges(items_added_to_inventory=[gear_id])
    )
    assert apply_result["applied"]["items_added_to_inventory"] == [gear_id]
    assert apply_result["rejected"] == []

    state = engine.get_full_state(game_conn)
    assert any(e["id"] == gear_id for e in state["inventory"])


def test_apply_cannot_take_obstacle_or_npc(game_conn):
    hatch = GeneratedEntity(
        name="Rusted Hatch", type="obstacle", description="Desc.",
        can_pickup=False, is_blocking=True,
    )
    room = RoomGeneration(room_name="Landing", description="Desc.", exits=[], entities=[hatch])
    result = engine.create_room(game_conn, "north", room)
    entity_id = result["room"]["entities"][0]["id"]

    apply_result = engine.apply_changes(
        game_conn, StateChanges(items_added_to_inventory=[entity_id])
    )
    assert apply_result["applied"] == {}
    assert apply_result["rejected"][0]["reason"] == "not_an_item"


def test_apply_damage_clamped_and_death(game_conn):
    result = engine.apply_changes(game_conn, StateChanges(damage_to_player=150))

    assert result["applied"]["damage_to_player"] == 100  # clamped
    assert result["applied"]["hp"] == 0
    assert result["player_dead"] is True

    state = engine.get_full_state(game_conn)
    assert state["hp"] == 0
    assert state["player_dead"] is True


def test_apply_flags_merge(game_conn):
    engine.apply_changes(game_conn, StateChanges(flags_set={"met_repair_unit": True}))
    engine.apply_changes(game_conn, StateChanges(flags_set={"has_key": True}))

    state = engine.get_full_state(game_conn)
    assert state["flags"] == {"met_repair_unit": True, "has_key": True}


def test_take_item_success_and_failure(game_conn):
    coil = GeneratedEntity(
        name="Copper Coil", type="item", description="Desc.", can_pickup=True, is_blocking=False
    )
    heavy_door = GeneratedEntity(
        name="Blast Door", type="obstacle", description="Desc.", can_pickup=False, is_blocking=True
    )
    room = RoomGeneration(room_name="Storage", description="Desc.", exits=[], entities=[coil, heavy_door])
    result = engine.create_room(game_conn, "north", room)
    coil_id = result["room"]["entities"][0]["id"]
    door_id = result["room"]["entities"][1]["id"]

    ok = engine.take_item(game_conn, coil_id)
    assert ok["ok"] is True
    assert ok["taken"]["id"] == coil_id

    fail = engine.take_item(game_conn, door_id)
    assert fail == {"ok": False, "error": "not_an_item"}

    missing = engine.take_item(game_conn, 99999)
    assert missing == {"ok": False, "error": "entity_not_found"}


def test_create_room_rejects_blocks_direction_that_is_not_an_exit(game_conn):
    hatch = GeneratedEntity(
        name="Hatch", type="obstacle", description="Desc.",
        can_pickup=False, is_blocking=True, blocks_direction="west",
    )
    room = RoomGeneration(room_name="X", description="Y.", exits=["east"], entities=[hatch])
    result = engine.create_room(game_conn, "north", room)
    assert result["ok"] is False
    assert result["error"] == "validation_error"
    assert "west" in result["details"]


def test_create_room_obstacle_may_block_implicit_return_direction(game_conn):
    """Cave-in / trap room: the obstacle blocks the way the player came in,
    without the agent listing that direction in exits."""
    cave_in = GeneratedEntity(
        name="Collapsed Gearwork", type="obstacle",
        description="Fallen gears choke the passage behind you.",
        can_pickup=False, is_blocking=True,
        solution_condition="Requires the debris to be levered aside.",
        blocks_direction="south",  # return direction of a northward move
    )
    room = RoomGeneration(room_name="Trap Room", description="Desc.", exits=[], entities=[cave_in])
    result = engine.create_room(game_conn, "north", room)

    assert result["ok"] is True
    south_exit = next(e for e in result["room"]["exits"] if e["direction"] == "south")
    assert south_exit["locked"] is True

    # Locked from both sides, and the player is sealed in until it's cleared.
    back = engine.move(game_conn, "south")
    assert back["ok"] is False and back["error"] == "locked"

    hatch_id = result["room"]["entities"][0]["id"]
    engine.apply_changes(game_conn, StateChanges(obstacles_cleared_entity_ids=[hatch_id]))
    back = engine.move(game_conn, "south")
    assert back["ok"] is True and back["moved"] is True


def test_apply_clears_multiple_obstacles_in_one_turn(game_conn):
    hatch = GeneratedEntity(
        name="Northern Hatch", type="obstacle", description="Desc.",
        can_pickup=False, is_blocking=True,
        solution_condition="Steam pressure.", blocks_direction="north",
    )
    vent = GeneratedEntity(
        name="Eastern Vent", type="obstacle", description="Desc.",
        can_pickup=False, is_blocking=True,
        solution_condition="Steam pressure.", blocks_direction="east",
    )
    room = RoomGeneration(
        room_name="Junction", description="Desc.", exits=["north", "east"],
        entities=[hatch, vent],
    )
    result = engine.create_room(game_conn, "north", room)
    ids = [e["id"] for e in result["room"]["entities"]]

    apply_result = engine.apply_changes(
        game_conn, StateChanges(obstacles_cleared_entity_ids=ids)
    )

    assert apply_result["applied"]["obstacles_cleared_entity_ids"] == ids
    assert apply_result["applied"]["edges_unlocked"] == 2
    assert apply_result["rejected"] == []
    view = engine.get_full_state(game_conn)["room"]
    assert all(not e["locked"] for e in view["exits"])


def test_destroying_blocking_obstacle_unlocks_its_edges(game_conn):
    hatch = GeneratedEntity(
        name="Rusted Hatch", type="obstacle", description="Desc.",
        can_pickup=False, is_blocking=True,
        solution_condition="Explosives would do it.", blocks_direction="east",
    )
    room = RoomGeneration(room_name="Landing", description="Desc.", exits=["east"], entities=[hatch])
    result = engine.create_room(game_conn, "north", room)
    hatch_id = result["room"]["entities"][0]["id"]

    apply_result = engine.apply_changes(
        game_conn, StateChanges(entities_destroyed=[hatch_id])
    )

    assert apply_result["applied"]["entities_destroyed"] == [hatch_id]
    assert apply_result["applied"]["edges_unlocked"] == 1

    east = engine.move(game_conn, "east")
    assert east["ok"] is True and east["needs_generation"] is True
    # And the hatch is gone from the room.
    state = engine.get_full_state(game_conn)
    assert engine.move(game_conn, "west") is not None  # sanity: engine still coherent
    assert all(e["name"] != "Rusted Hatch" for e in state["room"]["entities"])


def test_healing_offsets_damage_and_caps_at_max_hp(game_conn):
    engine.apply_changes(game_conn, StateChanges(damage_to_player=50))

    # Heal past max_hp: capped.
    result = engine.apply_changes(game_conn, StateChanges(healing_to_player=80))
    assert result["applied"]["healing_to_player"] == 80
    assert result["applied"]["hp"] == 100  # capped at max_hp

    # Damage and healing in the same turn net out before clamping: a lethal
    # net total still kills even if healing alone would have exceeded 0.
    engine.apply_changes(game_conn, StateChanges(damage_to_player=90))  # hp -> 10
    result = engine.apply_changes(
        game_conn, StateChanges(damage_to_player=50, healing_to_player=20)
    )
    assert result["applied"]["hp"] == 0
    assert result["player_dead"] is True


def test_dead_player_is_mechanically_dead(game_conn):
    coil = GeneratedEntity(
        name="Copper Coil", type="item", description="Desc.", can_pickup=True, is_blocking=False
    )
    room = RoomGeneration(room_name="Storage", description="Desc.", exits=["east"], entities=[coil])
    created = engine.create_room(game_conn, "north", room)
    coil_id = created["room"]["entities"][0]["id"]

    engine.apply_changes(game_conn, StateChanges(damage_to_player=100))

    assert engine.move(game_conn, "east") == {"ok": False, "error": "player_dead"}
    assert engine.take_item(game_conn, coil_id) == {"ok": False, "error": "player_dead"}
    assert engine.apply_changes(game_conn, StateChanges(healing_to_player=50)) == {
        "ok": False,
        "error": "player_dead",
    }
    assert engine.create_room(
        game_conn, "east", RoomGeneration(room_name="X", description="Y.", exits=[])
    ) == {"ok": False, "error": "player_dead"}

    # The narration path stays open: state, log, and reset still work.
    state = engine.get_full_state(game_conn)
    assert state["player_dead"] is True
    assert engine.log_turn(game_conn, "die", "You expire in the steam.") == {"ok": True}
    reset = engine.reset_game(game_conn)
    assert reset == {"ok": True, "new_game": True}
    assert engine.get_full_state(game_conn)["hp"] == config.STARTING_HP


def test_log_turn_and_recent_turns(game_conn):
    engine.log_turn(game_conn, "look around", "You see a rusty chamber.")
    engine.log_turn(game_conn, "go north", "You head north.")

    state = engine.get_full_state(game_conn)
    assert state["recent_turns"] == [
        {"player_input": "look around", "narrative": "You see a rusty chamber."},
        {"player_input": "go north", "narrative": "You head north."},
    ]


def test_reset_game_wipes_and_reseeds(game_conn):
    engine.create_room(
        game_conn, "north", RoomGeneration(room_name="Steam Landing", description="Desc.", exits=[])
    )
    engine.apply_changes(game_conn, StateChanges(damage_to_player=40, flags_set={"x": True}))
    engine.log_turn(game_conn, "hi", "hello")

    result = engine.reset_game(game_conn)
    assert result == {"ok": True, "new_game": True}

    state = engine.get_full_state(game_conn)
    assert state["room"]["name"] == config.START_ROOM_NAME
    assert state["hp"] == config.STARTING_HP
    assert state["flags"] == {}
    assert state["inventory"] == []
    assert state["recent_turns"] == []


def test_init_is_idempotent(conn):
    first = engine.init_game(conn)
    assert first == {"ok": True, "new_game": True}
    second = engine.init_game(conn)
    assert second == {"ok": True, "new_game": False}
