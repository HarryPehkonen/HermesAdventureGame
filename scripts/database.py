"""SQLite schema and low-level CRUD helpers.

Every function here takes an open `sqlite3.Connection` as its first argument
and performs straightforward row reads/writes — no cross-table business logic
(coordinate math, edge pairing/locking, validation). That lives in engine.py.
This module holds the authoritative schema; TECHNICAL_DETAILS.md §3 records
the reasoning behind the non-obvious parts.
"""

import json
import os
import sqlite3
from typing import Optional

import config

DIRECTIONS_SQL = "'north','south','east','west','up','down'"


def get_connection(db_path: str = config.DB_PATH) -> sqlite3.Connection:
    # sqlite3 refuses to create a missing parent directory (e.g. data/ on a
    # fresh clone) — create it up front so the default DB_PATH just works.
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create all tables if they don't already exist. Idempotent."""
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS game_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            zone_name TEXT NOT NULL,
            zone_description TEXT NOT NULL,
            global_theme_rules TEXT NOT NULL,
            world_init_json TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            x INTEGER NOT NULL,
            y INTEGER NOT NULL,
            z INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            is_explored INTEGER NOT NULL DEFAULT 1,
            UNIQUE(x, y, z)
        );

        CREATE TABLE IF NOT EXISTS entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id INTEGER,
            holder TEXT NOT NULL DEFAULT 'room'
                CHECK (holder IN ('room', 'player', 'gone')),
            name TEXT NOT NULL,
            type TEXT NOT NULL CHECK (type IN ('item', 'obstacle', 'npc')),
            description TEXT NOT NULL,
            properties_json TEXT NOT NULL,
            FOREIGN KEY (node_id) REFERENCES nodes(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS edges (
            from_node_id INTEGER NOT NULL,
            to_node_id INTEGER,
            direction TEXT NOT NULL CHECK (direction IN ({DIRECTIONS_SQL})),
            is_locked INTEGER NOT NULL DEFAULT 0,
            lock_condition TEXT,
            blocking_entity_id INTEGER,
            PRIMARY KEY (from_node_id, direction),
            FOREIGN KEY (from_node_id) REFERENCES nodes(id) ON DELETE CASCADE,
            FOREIGN KEY (to_node_id) REFERENCES nodes(id) ON DELETE CASCADE,
            FOREIGN KEY (blocking_entity_id) REFERENCES entities(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS player_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            current_node_id INTEGER,
            hp INTEGER NOT NULL DEFAULT 100,
            max_hp INTEGER NOT NULL DEFAULT 100,
            state_flags_json TEXT NOT NULL DEFAULT '{{}}',
            FOREIGN KEY (current_node_id) REFERENCES nodes(id)
        );

        CREATE TABLE IF NOT EXISTS turn_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id INTEGER,
            player_input TEXT NOT NULL,
            narrative_output TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (node_id) REFERENCES nodes(id)
        );
        """
    )


def wipe_all_data(conn: sqlite3.Connection) -> None:
    """Delete every row from every table (schema stays). Used by `reset`."""
    for table in ("turn_log", "edges", "entities", "player_state", "nodes", "game_config"):
        conn.execute(f"DELETE FROM {table};")


def is_seeded(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT 1 FROM player_state WHERE id = 1;").fetchone()
    return row is not None


# --- game_config -------------------------------------------------------

def insert_zone_config(
    conn: sqlite3.Connection,
    zone_name: str,
    zone_description: str,
    global_theme_rules: str,
    world_init_json: str,
) -> int:
    cur = conn.execute(
        "INSERT INTO game_config "
        "(zone_name, zone_description, global_theme_rules, world_init_json) "
        "VALUES (?, ?, ?, ?);",
        (zone_name, zone_description, global_theme_rules, world_init_json),
    )
    return cur.lastrowid


def get_world_init_json(conn: sqlite3.Connection) -> Optional[str]:
    """The WorldInit payload this campaign was seeded from — replayed by
    `reset` and printed by `export-world`."""
    row = conn.execute(
        "SELECT world_init_json FROM game_config ORDER BY id DESC LIMIT 1;"
    ).fetchone()
    return row["world_init_json"] if row else None


def get_zone_config(conn: sqlite3.Connection) -> Optional[dict]:
    row = conn.execute(
        "SELECT zone_name, zone_description, global_theme_rules "
        "FROM game_config ORDER BY id DESC LIMIT 1;"
    ).fetchone()
    return dict(row) if row else None


# --- nodes ---------------------------------------------------------------

def insert_node(
    conn: sqlite3.Connection, x: int, y: int, z: int, name: str, description: str
) -> int:
    cur = conn.execute(
        "INSERT INTO nodes (x, y, z, name, description, is_explored) "
        "VALUES (?, ?, ?, ?, ?, 1);",
        (x, y, z, name, description),
    )
    return cur.lastrowid


def get_node(conn: sqlite3.Connection, node_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM nodes WHERE id = ?;", (node_id,)).fetchone()


def get_node_by_coords(conn: sqlite3.Connection, x: int, y: int, z: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM nodes WHERE x = ? AND y = ? AND z = ?;", (x, y, z)
    ).fetchone()


# --- edges -----------------------------------------------------------------

def insert_edge(
    conn: sqlite3.Connection,
    from_node_id: int,
    direction: str,
    to_node_id: Optional[int] = None,
    is_locked: bool = False,
    lock_condition: Optional[str] = None,
    blocking_entity_id: Optional[int] = None,
) -> None:
    conn.execute(
        "INSERT INTO edges "
        "(from_node_id, direction, to_node_id, is_locked, lock_condition, blocking_entity_id) "
        "VALUES (?, ?, ?, ?, ?, ?);",
        (from_node_id, direction, to_node_id, int(is_locked), lock_condition, blocking_entity_id),
    )


def get_edge(conn: sqlite3.Connection, from_node_id: int, direction: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM edges WHERE from_node_id = ? AND direction = ?;",
        (from_node_id, direction),
    ).fetchone()


def get_edges_for_node(conn: sqlite3.Connection, node_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM edges WHERE from_node_id = ?;", (node_id,)
    ).fetchall()


def set_edge_target(
    conn: sqlite3.Connection, from_node_id: int, direction: str, to_node_id: int
) -> None:
    conn.execute(
        "UPDATE edges SET to_node_id = ? WHERE from_node_id = ? AND direction = ?;",
        (to_node_id, from_node_id, direction),
    )


def delete_edge(conn: sqlite3.Connection, from_node_id: int, direction: str) -> None:
    conn.execute(
        "DELETE FROM edges WHERE from_node_id = ? AND direction = ?;",
        (from_node_id, direction),
    )


def lock_edge(
    conn: sqlite3.Connection,
    from_node_id: int,
    direction: str,
    lock_condition: Optional[str],
    blocking_entity_id: int,
) -> None:
    conn.execute(
        "UPDATE edges SET is_locked = 1, lock_condition = ?, blocking_entity_id = ? "
        "WHERE from_node_id = ? AND direction = ?;",
        (lock_condition, blocking_entity_id, from_node_id, direction),
    )


def unlock_edges_by_blocking_entity(conn: sqlite3.Connection, entity_id: int) -> int:
    """Unlock every edge row locked by this obstacle (both sides of a pair,
    or the single row of a still-frontier edge). Returns rows affected."""
    cur = conn.execute(
        "UPDATE edges SET is_locked = 0, lock_condition = NULL, blocking_entity_id = NULL "
        "WHERE blocking_entity_id = ?;",
        (entity_id,),
    )
    return cur.rowcount


# --- entities --------------------------------------------------------------

def insert_entity(
    conn: sqlite3.Connection,
    node_id: Optional[int],
    holder: str,
    name: str,
    type_: str,
    description: str,
    properties: dict,
) -> int:
    cur = conn.execute(
        "INSERT INTO entities (node_id, holder, name, type, description, properties_json) "
        "VALUES (?, ?, ?, ?, ?, ?);",
        (node_id, holder, name, type_, description, json.dumps(properties)),
    )
    return cur.lastrowid


def get_entity(conn: sqlite3.Connection, entity_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM entities WHERE id = ?;", (entity_id,)).fetchone()


def get_room_entities(conn: sqlite3.Connection, node_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM entities WHERE holder = 'room' AND node_id = ?;", (node_id,)
    ).fetchall()


def get_inventory(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM entities WHERE holder = 'player';").fetchall()


def set_entity_holder(
    conn: sqlite3.Connection, entity_id: int, holder: str, node_id: Optional[int]
) -> None:
    conn.execute(
        "UPDATE entities SET holder = ?, node_id = ? WHERE id = ?;",
        (holder, node_id, entity_id),
    )


def set_entity_properties(conn: sqlite3.Connection, entity_id: int, properties: dict) -> None:
    conn.execute(
        "UPDATE entities SET properties_json = ? WHERE id = ?;",
        (json.dumps(properties), entity_id),
    )


def entity_to_dict(row: sqlite3.Row) -> dict:
    # Flat, not nested under "properties": this is the same shape the agent
    # submits to `create-room`/`apply` (can_pickup, is_blocking, traits, ...),
    # so reading state and authoring a new room don't disagree on the schema.
    return {
        "id": row["id"],
        "name": row["name"],
        "type": row["type"],
        "description": row["description"],
        **json.loads(row["properties_json"]),
    }


# --- player_state ------------------------------------------------------

def insert_player_state(conn: sqlite3.Connection, current_node_id: int, hp: int, max_hp: int) -> None:
    conn.execute(
        "INSERT INTO player_state (id, current_node_id, hp, max_hp, state_flags_json) "
        "VALUES (1, ?, ?, ?, '{}');",
        (current_node_id, hp, max_hp),
    )


def get_player_state(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM player_state WHERE id = 1;").fetchone()
    if row is None:
        raise RuntimeError("player_state is not seeded — run `python scripts/game.py init` first")
    return row


def set_player_node(conn: sqlite3.Connection, node_id: int) -> None:
    conn.execute("UPDATE player_state SET current_node_id = ? WHERE id = 1;", (node_id,))


def set_player_hp(conn: sqlite3.Connection, hp: int) -> None:
    conn.execute("UPDATE player_state SET hp = ? WHERE id = 1;", (hp,))


def set_player_flags(conn: sqlite3.Connection, flags: dict) -> None:
    conn.execute(
        "UPDATE player_state SET state_flags_json = ? WHERE id = 1;", (json.dumps(flags),)
    )


# --- turn_log ------------------------------------------------------------

def insert_turn_log(
    conn: sqlite3.Connection, node_id: Optional[int], player_input: str, narrative_output: str
) -> None:
    conn.execute(
        "INSERT INTO turn_log (node_id, player_input, narrative_output) VALUES (?, ?, ?);",
        (node_id, player_input, narrative_output),
    )


def get_recent_turns(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM turn_log ORDER BY id DESC LIMIT ?;", (limit,)
    ).fetchall()
    return list(reversed(rows))  # chronological order
