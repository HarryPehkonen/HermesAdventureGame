"""Low-level CRUD and edge/entity primitives: TECHNICAL_DETAILS.md §3."""

import database


def test_singleton_player_state_enforced(conn):
    database.insert_node(conn, 0, 0, 0, "Room", "Desc")
    database.insert_player_state(conn, 1, 100, 100)
    row = database.get_player_state(conn)
    assert row["id"] == 1
    assert row["current_node_id"] == 1


def test_frontier_edge_has_null_target(conn):
    node_id = database.insert_node(conn, 0, 0, 0, "Room", "Desc")
    database.insert_edge(conn, node_id, "north")
    edge = database.get_edge(conn, node_id, "north")
    assert edge["to_node_id"] is None
    assert edge["is_locked"] == 0


def test_no_edge_row_means_wall(conn):
    node_id = database.insert_node(conn, 0, 0, 0, "Room", "Desc")
    assert database.get_edge(conn, node_id, "south") is None


def test_set_edge_target_resolves_frontier(conn):
    a = database.insert_node(conn, 0, 0, 0, "A", "Desc")
    b = database.insert_node(conn, 0, 1, 0, "B", "Desc")
    database.insert_edge(conn, a, "north")
    database.set_edge_target(conn, a, "north", b)
    edge = database.get_edge(conn, a, "north")
    assert edge["to_node_id"] == b


def test_delete_edge_closes_frontier(conn):
    a = database.insert_node(conn, 0, 0, 0, "A", "Desc")
    database.insert_edge(conn, a, "north")
    database.delete_edge(conn, a, "north")
    assert database.get_edge(conn, a, "north") is None


def test_entity_holder_transitions(conn):
    node_id = database.insert_node(conn, 0, 0, 0, "Room", "Desc")
    eid = database.insert_entity(
        conn, node_id, "room", "Key", "item", "A key.", {"can_pickup": True}
    )
    ent = database.get_entity(conn, eid)
    assert ent["holder"] == "room"
    assert ent["node_id"] == node_id

    database.set_entity_holder(conn, eid, "player", None)
    ent = database.get_entity(conn, eid)
    assert ent["holder"] == "player"
    assert ent["node_id"] is None

    database.set_entity_holder(conn, eid, "gone", None)
    ent = database.get_entity(conn, eid)
    assert ent["holder"] == "gone"


def test_unlock_edges_by_blocking_entity_unlocks_both_sides_of_a_real_pair(conn):
    a = database.insert_node(conn, 0, 0, 0, "A", "Desc")
    b = database.insert_node(conn, 0, 1, 0, "B", "Desc")
    obstacle_id = database.insert_entity(
        conn, a, "room", "Hatch", "obstacle", "A hatch.", {"is_blocking": True}
    )
    database.insert_edge(
        conn, a, "north", to_node_id=b, is_locked=True,
        lock_condition="Need a crowbar.", blocking_entity_id=obstacle_id,
    )
    database.insert_edge(
        conn, b, "south", to_node_id=a, is_locked=True,
        lock_condition="Need a crowbar.", blocking_entity_id=obstacle_id,
    )

    affected = database.unlock_edges_by_blocking_entity(conn, obstacle_id)

    assert affected == 2
    edge_a = database.get_edge(conn, a, "north")
    edge_b = database.get_edge(conn, b, "south")
    assert edge_a["is_locked"] == 0 and edge_a["blocking_entity_id"] is None
    assert edge_b["is_locked"] == 0 and edge_b["blocking_entity_id"] is None


def test_unlock_edges_by_blocking_entity_unlocks_lone_frontier(conn):
    a = database.insert_node(conn, 0, 0, 0, "A", "Desc")
    obstacle_id = database.insert_entity(
        conn, a, "room", "Hatch", "obstacle", "A hatch.", {"is_blocking": True}
    )
    database.insert_edge(
        conn, a, "north", is_locked=True,
        lock_condition="Need a crowbar.", blocking_entity_id=obstacle_id,
    )

    affected = database.unlock_edges_by_blocking_entity(conn, obstacle_id)

    assert affected == 1
    edge_a = database.get_edge(conn, a, "north")
    assert edge_a["is_locked"] == 0
    assert edge_a["to_node_id"] is None  # still a frontier, just unlocked
