"""Business logic: coordinate math, movement/generation, apply rules, state
assembly. Combines database.py primitives into the operations game.py's CLI
commands expose. See TECHNICAL_DETAILS.md §4 (movement/generation) and §5
(apply validation rules) for the specs these functions implement.
"""

import json
import sqlite3
from typing import Optional

import config
import database
import models
from models import GeneratedEntity, RoomGeneration, StateChanges, WorldInit

_DIRECTION_ORDER = {d: i for i, d in enumerate(models.DIRECTIONS)}


# --- seeding / lifecycle ----------------------------------------------------

def default_world_init() -> WorldInit:
    """The built-in Mechanical Spire campaign, used when no WorldInit payload
    is supplied (plain `init` on an empty DB)."""
    return WorldInit(
        zone_name=config.ZONE_NAME,
        zone_description=config.ZONE_DESCRIPTION,
        global_theme_rules=config.GLOBAL_THEME_RULES,
        starting_room=RoomGeneration(
            room_name=config.START_ROOM_NAME,
            description=config.START_ROOM_DESCRIPTION,
            exits=list(config.START_ROOM_EXITS),
        ),
    )


def _seed_new_game(conn: sqlite3.Connection, world: WorldInit) -> None:
    database.insert_zone_config(
        conn,
        world.zone_name,
        world.zone_description,
        world.global_theme_rules,
        world.model_dump_json(),
    )
    room = world.starting_room
    node_id = database.insert_node(conn, 0, 0, 0, room.room_name, room.description)
    for direction in room.exits:
        database.insert_edge(conn, node_id, direction)  # frontier

    entity_ids: list[int] = []
    for entity in room.entities:
        eid = database.insert_entity(
            conn, node_id, "room", entity.name, entity.type,
            entity.description, _entity_properties(entity),
        )
        entity_ids.append(eid)
    for entity, eid in zip(room.entities, entity_ids):
        if entity.blocks_direction is not None:
            _lock_edge_pair(conn, node_id, entity.blocks_direction, entity.solution_condition, eid)

    for item in world.starting_inventory:
        database.insert_entity(
            conn, None, "player", item.name, item.type,
            item.description, _entity_properties(item),
        )

    database.insert_player_state(conn, node_id, config.STARTING_HP, config.STARTING_HP)


def init_game(conn: sqlite3.Connection, world: Optional[WorldInit] = None) -> dict:
    database.init_schema(conn)
    if database.is_seeded(conn):
        if world is not None:
            # Silently ignoring the payload would leave the agent narrating a
            # world that doesn't match the DB — fail loudly instead.
            return {
                "ok": False,
                "error": "already_seeded",
                "details": "a campaign already exists; pipe the WorldInit to `reset` to replace it",
            }
        return {"ok": True, "new_game": False}
    with conn:
        _seed_new_game(conn, world or default_world_init())
    return {"ok": True, "new_game": True}


def reset_game(conn: sqlite3.Connection, world: Optional[WorldInit] = None) -> dict:
    """No payload: replay the stored campaign from the beginning (same theme,
    starting room, and loadout — the world beyond regenerates at play time).
    With a payload: wipe and start a different campaign."""
    database.init_schema(conn)
    if world is None:
        stored = database.get_world_init_json(conn)
        world = WorldInit.model_validate_json(stored) if stored else default_world_init()
    with conn:
        database.wipe_all_data(conn)
        _seed_new_game(conn, world)
    return {"ok": True, "new_game": True}


def export_world(conn: sqlite3.Connection) -> dict:
    """The stored WorldInit payload — shareable: another player pipes it to
    `init` to start the same campaign from the beginning."""
    stored = database.get_world_init_json(conn)
    if stored is None:
        return {"ok": False, "error": "not_initialized"}
    return {"ok": True, "world": json.loads(stored)}


# --- state assembly ----------------------------------------------------

def _win_info(conn: sqlite3.Connection) -> tuple[Optional[str], Optional[str]]:
    """The campaign's (win_flag, win_message) from the stored WorldInit, or
    (None, None) for an endless-sandbox campaign (§5.6)."""
    stored = database.get_world_init_json(conn)
    if stored is None:
        return None, None
    world = json.loads(stored)
    return world.get("win_flag"), world.get("win_message")


def _room_view(conn: sqlite3.Connection, node_id: int) -> dict:
    node = database.get_node(conn, node_id)
    edges = database.get_edges_for_node(conn, node_id)
    exits = sorted(
        (
            {
                "direction": e["direction"],
                "locked": bool(e["is_locked"]),
                "lock_condition": e["lock_condition"],
                "generated": e["to_node_id"] is not None,
            }
            for e in edges
        ),
        key=lambda ex: _DIRECTION_ORDER[ex["direction"]],
    )
    entities = [database.entity_to_dict(r) for r in database.get_room_entities(conn, node_id)]
    return {
        "id": node["id"],
        "name": node["name"],
        "description": node["description"],
        "coordinates": {"x": node["x"], "y": node["y"], "z": node["z"]},
        "exits": exits,
        "entities": entities,
    }


def get_full_state(conn: sqlite3.Connection) -> dict:
    zone = database.get_zone_config(conn)
    player_row = database.get_player_state(conn)
    room = _room_view(conn, player_row["current_node_id"])
    inventory = [database.entity_to_dict(r) for r in database.get_inventory(conn)]
    recent_turns = [
        {"player_input": t["player_input"], "narrative": t["narrative_output"]}
        for t in database.get_recent_turns(conn, config.RECENT_TURNS_LIMIT)
    ]
    hp = player_row["hp"]
    flags = json.loads(player_row["state_flags_json"])
    # Like player_dead, the win is derived from persisted state, so a fresh
    # session with no memory of earlier turns still knows the goal and
    # whether it has been met (§5.6).
    win_flag, win_message = _win_info(conn)
    game_won = bool(win_flag and flags.get(win_flag))
    return {
        "zone": dict(zone) if zone else None,
        "room": room,
        "inventory": inventory,
        "hp": hp,
        "max_hp": player_row["max_hp"],
        "player_dead": hp <= 0,
        "flags": flags,
        "win_flag": win_flag,
        "game_won": game_won,
        "win_message": win_message if game_won else None,
        "recent_turns": recent_turns,
    }


def _build_generation_context(
    conn: sqlite3.Connection, current_node: sqlite3.Row, direction: str, tx: int, ty: int, tz: int
) -> dict:
    zone = database.get_zone_config(conn)
    flags = json.loads(database.get_player_state(conn)["state_flags_json"])
    neighbors = []
    for d in models.DIRECTIONS:
        ndx, ndy, ndz = models.DIRECTION_OFFSETS[d]
        neighbor = database.get_node_by_coords(conn, tx + ndx, ty + ndy, tz + ndz)
        if neighbor is None:
            continue
        opp = models.OPPOSITE_DIRECTION[d]
        has_facing_edge = database.get_edge(conn, neighbor["id"], opp) is not None
        one_line = neighbor["description"].split(". ")[0].strip()
        if one_line and one_line[-1] not in ".!?":
            one_line += "."
        neighbors.append(
            {
                "direction_from_target": d,
                "name": neighbor["name"],
                "description": one_line,
                "has_exit_facing_target": has_facing_edge,
            }
        )
    return {
        "target_coordinates": {"x": tx, "y": ty, "z": tz},
        "direction_of_travel": direction,
        "exiting_room": {"name": current_node["name"], "description": current_node["description"]},
        "zone": dict(zone) if zone else None,
        "flags": flags,
        "neighbors": neighbors,
    }


# --- movement ------------------------------------------------------------

def move(conn: sqlite3.Connection, direction: str) -> dict:
    if direction not in models.DIRECTIONS:
        return {"ok": False, "error": "invalid_direction"}

    player_row = database.get_player_state(conn)
    if player_row["hp"] <= 0:
        return {"ok": False, "error": "player_dead"}
    current_node_id = player_row["current_node_id"]
    flags = json.loads(player_row["state_flags_json"])
    edge = database.get_edge(conn, current_node_id, direction)

    if edge is None:
        return {"ok": False, "error": "no_exit"}
    if edge["is_locked"]:
        # A flag set elsewhere may already satisfy the blocking obstacle.
        # Resolve it lazily here (§5.5) — otherwise the player could never
        # reach the room whose entry would have cleared it.
        blocker = (
            database.get_entity(conn, edge["blocking_entity_id"])
            if edge["blocking_entity_id"] is not None
            else None
        )
        with conn:
            unlocked = _flag_clear_obstacle(conn, blocker, flags)
        if not unlocked:
            return {"ok": False, "error": "locked", "lock_condition": edge["lock_condition"]}
        edge = database.get_edge(conn, current_node_id, direction)
    if edge["to_node_id"] is not None:
        with conn:
            database.set_player_node(conn, edge["to_node_id"])
            auto_cleared = _auto_clear_room_obstacles(conn, edge["to_node_id"], flags)
        result = {"ok": True, "moved": True, "room": _room_view(conn, edge["to_node_id"])}
        if auto_cleared:
            result["auto_cleared"] = auto_cleared
        return result

    # Frontier: check whether the target coordinates already have a room
    # (loops can make a frontier point at a room generated from another side).
    current_node = database.get_node(conn, current_node_id)
    dx, dy, dz = models.DIRECTION_OFFSETS[direction]
    tx, ty, tz = current_node["x"] + dx, current_node["y"] + dy, current_node["z"] + dz
    target_node = database.get_node_by_coords(conn, tx, ty, tz)

    if target_node is not None:
        opp = models.OPPOSITE_DIRECTION[direction]
        neighbor_edge = database.get_edge(conn, target_node["id"], opp)
        if neighbor_edge is not None and neighbor_edge["to_node_id"] is None:
            # Matching frontier on the other side: link both rows, carrying
            # over whatever lock state that frontier already had. A lock on
            # the far side blocks passage in either direction (§3.3), so the
            # player only moves if the linked passage is unlocked — or if a
            # flag already satisfies the blocking obstacle (§5.5), which
            # clears it and unlocks both freshly-linked rows.
            still_locked = bool(neighbor_edge["is_locked"])
            auto_cleared: list[int] = []
            with conn:
                database.set_edge_target(conn, current_node_id, direction, target_node["id"])
                database.set_edge_target(conn, target_node["id"], opp, current_node_id)
                if still_locked:
                    database.lock_edge(
                        conn,
                        current_node_id,
                        direction,
                        neighbor_edge["lock_condition"],
                        neighbor_edge["blocking_entity_id"],
                    )
                    blocker = (
                        database.get_entity(conn, neighbor_edge["blocking_entity_id"])
                        if neighbor_edge["blocking_entity_id"] is not None
                        else None
                    )
                    if _flag_clear_obstacle(conn, blocker, flags):
                        still_locked = False
                if not still_locked:
                    database.set_player_node(conn, target_node["id"])
                    auto_cleared = _auto_clear_room_obstacles(conn, target_node["id"], flags)
            if still_locked:
                return {
                    "ok": False,
                    "error": "locked",
                    "lock_condition": neighbor_edge["lock_condition"],
                }
            result = {"ok": True, "moved": True, "room": _room_view(conn, target_node["id"])}
            if auto_cleared:
                result["auto_cleared"] = auto_cleared
            return result
        else:
            # Neighbor exists but doesn't connect back to us — a wall on
            # their side. Our frontier was wrong; close it permanently.
            with conn:
                database.delete_edge(conn, current_node_id, direction)
            return {"ok": False, "error": "no_exit"}

    context = _build_generation_context(conn, current_node, direction, tx, ty, tz)
    return {"ok": True, "needs_generation": True, "context": context}


# --- room generation ---------------------------------------------------

def _entity_properties(entity: GeneratedEntity) -> dict:
    props = {
        "can_pickup": entity.can_pickup,
        "is_blocking": entity.is_blocking,
        "traits": list(entity.traits),
    }
    if entity.type == "obstacle":
        props["solution_condition"] = entity.solution_condition
        props["is_cleared"] = False
        if entity.cleared_by_flag is not None:
            props["cleared_by_flag"] = entity.cleared_by_flag
    if entity.blocks_direction is not None:
        props["blocks_direction"] = entity.blocks_direction
    return props


def _flag_clear_obstacle(conn: sqlite3.Connection, ent: Optional[sqlite3.Row], flags: dict) -> bool:
    """Clear an uncleared obstacle whose `cleared_by_flag` is satisfied by the
    player's flags, unlocking any edges it blocks. Returns True if it cleared
    just now. Caller provides the transaction."""
    if ent is None or ent["type"] != "obstacle" or ent["holder"] != "room":
        return False
    props = json.loads(ent["properties_json"])
    flag = props.get("cleared_by_flag")
    if not flag or props.get("is_cleared") or not flags.get(flag):
        return False
    props["is_cleared"] = True
    database.set_entity_properties(conn, ent["id"], props)
    database.unlock_edges_by_blocking_entity(conn, ent["id"])
    return True


def _auto_clear_room_obstacles(conn: sqlite3.Connection, node_id: int, flags: dict) -> list[int]:
    """Engine-side lazy auto-resolution (TECHNICAL_DETAILS.md §5.5): when the
    player is in a room, every flag-linked obstacle there whose flag is set
    clears deterministically — the agent only narrates the change."""
    if not flags:
        return []
    cleared: list[int] = []
    for ent in database.get_room_entities(conn, node_id):
        if _flag_clear_obstacle(conn, ent, flags):
            cleared.append(ent["id"])
    return cleared


def _lock_edge_pair(
    conn: sqlite3.Connection,
    node_id: int,
    direction: str,
    lock_condition: Optional[str],
    blocking_entity_id: int,
) -> None:
    edge = database.get_edge(conn, node_id, direction)
    database.lock_edge(conn, node_id, direction, lock_condition, blocking_entity_id)
    if edge is not None and edge["to_node_id"] is not None:
        opp = models.OPPOSITE_DIRECTION[direction]
        database.lock_edge(conn, edge["to_node_id"], opp, lock_condition, blocking_entity_id)


def create_room(conn: sqlite3.Connection, direction: str, room: RoomGeneration) -> dict:
    if direction not in models.DIRECTIONS:
        return {"ok": False, "error": "invalid_direction"}

    player_row = database.get_player_state(conn)
    if player_row["hp"] <= 0:
        return {"ok": False, "error": "player_dead"}
    current_node_id = player_row["current_node_id"]
    edge = database.get_edge(conn, current_node_id, direction)

    if edge is None:
        return {"ok": False, "error": "no_exit"}
    if edge["is_locked"]:
        return {"ok": False, "error": "locked", "lock_condition": edge["lock_condition"]}
    if edge["to_node_id"] is not None:
        return {"ok": False, "error": "already_generated"}

    current_node = database.get_node(conn, current_node_id)
    dx, dy, dz = models.DIRECTION_OFFSETS[direction]
    tx, ty, tz = current_node["x"] + dx, current_node["y"] + dy, current_node["z"] + dz

    if database.get_node_by_coords(conn, tx, ty, tz) is not None:
        return {"ok": False, "error": "already_generated"}

    return_direction = models.OPPOSITE_DIRECTION[direction]
    exits = list(dict.fromkeys([*room.exits, return_direction]))

    # blocks_direction must reference one of the room's exits — checked here
    # rather than in the Pydantic model because the implicit return direction
    # only exists after the append above (blocking the way back = trap room,
    # and the agent needn't list it in `exits` to block it).
    exit_set = set(exits)
    for entity in room.entities:
        if entity.blocks_direction is not None and entity.blocks_direction not in exit_set:
            return {
                "ok": False,
                "error": "validation_error",
                "details": (
                    f"entity {entity.name!r} has blocks_direction="
                    f"{entity.blocks_direction!r}, which is not one of this "
                    f"room's exits {sorted(exit_set)} (the implicit return "
                    f"direction {return_direction!r} counts as an exit)"
                ),
            }

    with conn:
        node_id = database.insert_node(conn, tx, ty, tz, room.room_name, room.description)

        entity_ids: list[int] = []
        for entity in room.entities:
            eid = database.insert_entity(
                conn,
                node_id,
                "room",
                entity.name,
                entity.type,
                entity.description,
                _entity_properties(entity),
            )
            entity_ids.append(eid)

        # Resolve the edge pair we just crossed.
        database.set_edge_target(conn, current_node_id, direction, node_id)
        database.insert_edge(conn, node_id, return_direction, to_node_id=current_node_id)

        # Remaining declared exits: pair with an existing neighbor's matching
        # frontier, otherwise a new frontier — never a frontier pointing at
        # an already-occupied, non-connecting coordinate (TECHNICAL_DETAILS.md
        # §4: "a neighbor's wall stays a wall").
        for d in exits:
            if d == return_direction:
                continue
            ndx, ndy, ndz = models.DIRECTION_OFFSETS[d]
            nx, ny, nz = tx + ndx, ty + ndy, tz + ndz
            neighbor = database.get_node_by_coords(conn, nx, ny, nz)
            if neighbor is not None:
                opp = models.OPPOSITE_DIRECTION[d]
                neighbor_edge = database.get_edge(conn, neighbor["id"], opp)
                if neighbor_edge is not None and neighbor_edge["to_node_id"] is None:
                    database.set_edge_target(conn, neighbor["id"], opp, node_id)
                    database.insert_edge(
                        conn,
                        node_id,
                        d,
                        to_node_id=neighbor["id"],
                        is_locked=bool(neighbor_edge["is_locked"]),
                        lock_condition=neighbor_edge["lock_condition"],
                        blocking_entity_id=neighbor_edge["blocking_entity_id"],
                    )
                # else: neighbor's wall wins — this declared exit is dropped.
            else:
                database.insert_edge(conn, node_id, d)  # frontier

        # Apply obstacle locks now that every edge for this room exists.
        for entity, eid in zip(room.entities, entity_ids):
            if entity.blocks_direction is not None:
                _lock_edge_pair(conn, node_id, entity.blocks_direction, entity.solution_condition, eid)

        database.set_player_node(conn, node_id)
        # Safety net: SKILL.md tells the agent not to generate flag-linked
        # obstacles a current flag already neutralizes, but if one slips
        # through it clears immediately rather than lingering as a lie.
        auto_cleared = _auto_clear_room_obstacles(
            conn, node_id, json.loads(player_row["state_flags_json"])
        )

    result = {"ok": True, "room": _room_view(conn, node_id)}
    if auto_cleared:
        result["auto_cleared"] = auto_cleared
    return result


# --- take / apply --------------------------------------------------------

def _validate_takeable(
    ent: Optional[sqlite3.Row], current_node_id: int, require_pickup: bool = True
) -> Optional[str]:
    """`require_pickup=True` is the mechanical `take` path. The referee path
    (`apply`) passes False: the agent has already judged the acquisition
    plausible, so `can_pickup` doesn't gate it — only location and type do
    (TECHNICAL_DETAILS.md §5)."""
    if ent is None:
        return "entity_not_found"
    if ent["holder"] != "room" or ent["node_id"] != current_node_id:
        return "entity_not_in_current_room"
    if ent["type"] != "item":
        return "not_an_item"
    if require_pickup:
        props = json.loads(ent["properties_json"])
        if not props.get("can_pickup", False):
            return "cannot_be_picked_up"
    return None


def take_item(conn: sqlite3.Connection, entity_id: int) -> dict:
    player_row = database.get_player_state(conn)
    if player_row["hp"] <= 0:
        return {"ok": False, "error": "player_dead"}
    current_node_id = player_row["current_node_id"]
    ent = database.get_entity(conn, entity_id)
    reason = _validate_takeable(ent, current_node_id)
    if reason is not None:
        return {"ok": False, "error": reason}
    with conn:
        database.set_entity_holder(conn, entity_id, "player", None)
    return {"ok": True, "taken": database.entity_to_dict(database.get_entity(conn, entity_id))}


def apply_changes(conn: sqlite3.Connection, changes: StateChanges) -> dict:
    player_row = database.get_player_state(conn)
    if player_row["hp"] <= 0:
        return {"ok": False, "error": "player_dead"}
    current_node_id = player_row["current_node_id"]
    applied: dict = {}
    rejected: list[dict] = []
    new_hp = player_row["hp"]
    edges_unlocked = 0

    with conn:
        added = []
        for eid in changes.items_added_to_inventory:
            ent = database.get_entity(conn, eid)
            reason = _validate_takeable(ent, current_node_id, require_pickup=False)
            if reason is not None:
                rejected.append({"change": f"items_added_to_inventory:{eid}", "reason": reason})
                continue
            database.set_entity_holder(conn, eid, "player", None)
            added.append(eid)
        if added:
            applied["items_added_to_inventory"] = added

        removed = []
        for eid in changes.items_removed_from_inventory:
            ent = database.get_entity(conn, eid)
            if ent is None:
                rejected.append(
                    {"change": f"items_removed_from_inventory:{eid}", "reason": "entity_not_found"}
                )
                continue
            if ent["holder"] != "player":
                rejected.append(
                    {"change": f"items_removed_from_inventory:{eid}", "reason": "not_in_inventory"}
                )
                continue
            database.set_entity_holder(conn, eid, "gone", None)
            removed.append(eid)
        if removed:
            applied["items_removed_from_inventory"] = removed

        cleared = []
        for eid in changes.obstacles_cleared_entity_ids:
            ent = database.get_entity(conn, eid)
            if ent is None:
                rejected.append(
                    {"change": f"obstacles_cleared_entity_ids:{eid}", "reason": "entity_not_found"}
                )
            elif ent["type"] != "obstacle" or ent["holder"] != "room" or ent["node_id"] != current_node_id:
                rejected.append(
                    {
                        "change": f"obstacles_cleared_entity_ids:{eid}",
                        "reason": "not_an_obstacle_in_current_room",
                    }
                )
            else:
                props = json.loads(ent["properties_json"])
                props["is_cleared"] = True
                database.set_entity_properties(conn, eid, props)
                edges_unlocked += database.unlock_edges_by_blocking_entity(conn, eid)
                cleared.append(eid)
        if cleared:
            applied["obstacles_cleared_entity_ids"] = cleared

        destroyed = []
        for eid in changes.entities_destroyed:
            ent = database.get_entity(conn, eid)
            if ent is None:
                rejected.append({"change": f"entities_destroyed:{eid}", "reason": "entity_not_found"})
                continue
            in_room = ent["holder"] == "room" and ent["node_id"] == current_node_id
            in_inventory = ent["holder"] == "player"
            if not (in_room or in_inventory):
                rejected.append({"change": f"entities_destroyed:{eid}", "reason": "not_accessible"})
                continue
            database.set_entity_holder(conn, eid, "gone", None)
            # A destroyed obstacle no longer blocks anything: unlock any edge
            # it was locking, or the door jams forever (its row survives as
            # holder='gone', so the FK's ON DELETE SET NULL never fires).
            edges_unlocked += database.unlock_edges_by_blocking_entity(conn, eid)
            destroyed.append(eid)
        if destroyed:
            applied["entities_destroyed"] = destroyed

        if edges_unlocked:
            applied["edges_unlocked"] = edges_unlocked

        if changes.damage_to_player or changes.healing_to_player:
            dmg = max(0, min(100, changes.damage_to_player))
            heal = max(0, min(100, changes.healing_to_player))
            # Net first, then clamp: healing offsets damage in the same turn
            # but can't revive through a lethal net total or exceed max_hp.
            new_hp = max(0, min(player_row["max_hp"], player_row["hp"] - dmg + heal))
            database.set_player_hp(conn, new_hp)
            if dmg:
                applied["damage_to_player"] = dmg
            if heal:
                applied["healing_to_player"] = heal
            applied["hp"] = new_hp

        newly_won = False
        if changes.flags_set:
            flags = json.loads(player_row["state_flags_json"])
            win_flag, win_message = _win_info(conn)
            newly_won = bool(
                win_flag and changes.flags_set.get(win_flag) and not flags.get(win_flag)
            )
            flags.update(changes.flags_set)
            database.set_player_flags(conn, flags)
            applied["flags_set"] = changes.flags_set
            # Flags resolve current-room obstacles immediately; obstacles in
            # other rooms resolve lazily when the player next goes there.
            auto_cleared = _auto_clear_room_obstacles(conn, current_node_id, flags)
            if auto_cleared:
                applied["auto_cleared_obstacles"] = auto_cleared

    result = {"ok": True, "applied": applied, "rejected": rejected, "player_dead": new_hp <= 0}
    if newly_won:
        # Reported once, at the moment of victory; `state` keeps answering
        # game_won afterward. Winning never gates later commands (§5.6) —
        # the player is free to keep exploring. Dying still does.
        result["game_won"] = True
        result["win_message"] = win_message
    return result


def log_turn(conn: sqlite3.Connection, player_input: str, narrative: str) -> dict:
    player_row = database.get_player_state(conn)
    with conn:
        database.insert_turn_log(conn, player_row["current_node_id"], player_input, narrative)
    return {"ok": True}
