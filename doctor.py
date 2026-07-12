#!/usr/bin/env python3
"""Save-file inspector and consistency checker.

Deliberately a separate script from game.py: game.py's stdout is a strict
one-JSON-object contract consumed by Hermes Agent, while this prints plain
text for humans (and for the agent's `--check-only` sanity check at session
start). Read-only, except that `--repair-log` inserts clearly-marked
placeholder rows into turn_log — never anything else.

Findings come in two kinds with different lifecycles:

- "integrity": the current world state is inconsistent (broken edge pairs,
  stale locks, orphaned entities). Always fatal, never auto-repaired.
- "history": turn_log is incomplete — a past turn went unlogged. The world
  state is fine, only the record has a hole. Without repair these would fail
  every future check forever (the gap is a permanent scar), so --repair-log
  plugs old gaps with placeholder markers; any NEW gap fails again, which is
  the regression alarm that matters.

The history checks can only be heuristic: turn_log is itself the only record
of turns, so this looks for symptoms (visited rooms with no logged turns,
the player having moved since the last log entry) rather than proof.

Exit codes: 0 = all checks passed, 1 = an integrity check failed,
2 = database missing, 3 = only history-gap checks failed (repairable
with --repair-log).
"""

import argparse
import json
import os
import sqlite3
import sys
from typing import NamedTuple

import config
import database
import models


class Finding(NamedTuple):
    kind: str  # "integrity" (world state broken) or "history" (turn_log gap)
    message: str


def _integrity(message: str) -> Finding:
    return Finding("integrity", message)


def _history(message: str) -> Finding:
    return Finding("history", message)


GAP_INPUT = "(turn not logged)"
GAP_ROOM_NARRATIVE = (
    "(history gap: this room was visited but its turns were not recorded)"
)
GAP_MOVES_NARRATIVE = (
    "(history gap: turns since the last logged entry were not recorded)"
)


# --- checks ------------------------------------------------------------

def _load_json(text: str):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def run_checks(conn: sqlite3.Connection) -> list[Finding]:
    """Every Finding returned is one failed check, with the reason inline."""
    if not database.is_seeded(conn):
        return [
            _integrity("not seeded: player_state is empty — run `python game.py init` first")
        ]

    findings: list[Finding] = []
    nodes = conn.execute("SELECT * FROM nodes ORDER BY id;").fetchall()
    node_by_id = {n["id"]: n for n in nodes}
    edges = conn.execute("SELECT * FROM edges;").fetchall()
    edge_map = {(e["from_node_id"], e["direction"]): e for e in edges}
    entities = conn.execute("SELECT * FROM entities ORDER BY id;").fetchall()
    turns = conn.execute("SELECT * FROM turn_log ORDER BY id;").fetchall()
    player = database.get_player_state(conn)

    def room(node_id) -> str:
        n = node_by_id.get(node_id)
        return f"node {node_id} ({n['name']!r})" if n else f"node {node_id} (missing!)"

    # turn_log coverage. A single seeded room with no turns is a fresh game,
    # not a logging failure.
    if not turns and len(nodes) > 1:
        findings.append(_history(
            f"turn_log is empty but {len(nodes)} rooms exist — "
            "this campaign was played without logging any turns"
        ))
    elif turns:
        logged_node_ids = {t["node_id"] for t in turns}
        for n in nodes:
            if n["id"] not in logged_node_ids:
                findings.append(_history(
                    f"unlogged room: {room(n['id'])} was visited but has no "
                    "turn_log entries — at least one turn there went unlogged"
                ))
        last = turns[-1]
        if last["node_id"] != player["current_node_id"]:
            findings.append(_history(
                f"stale log position: last logged turn (#{last['id']}) was in "
                f"{room(last['node_id'])} but the player is in "
                f"{room(player['current_node_id'])} — moves since then were not logged"
            ))

    # edge pairing and geometry (TECHNICAL_DETAILS.md §3.2)
    for e in edges:
        from_id, direction, to_id = e["from_node_id"], e["direction"], e["to_node_id"]
        label = f"edge {room(from_id)} --{direction}--> "
        if to_id is None:
            continue  # frontier: nothing to pair or measure yet
        fn, tn = node_by_id.get(from_id), node_by_id.get(to_id)
        if fn and tn:
            dx, dy, dz = models.DIRECTION_OFFSETS[direction]
            expected = (fn["x"] + dx, fn["y"] + dy, fn["z"] + dz)
            actual = (tn["x"], tn["y"], tn["z"])
            if expected != actual:
                findings.append(_integrity(
                    f"{label}{room(to_id)}: target sits at {actual}, but one step "
                    f"{direction} from the source is {expected}"
                ))
        back = edge_map.get((to_id, models.OPPOSITE_DIRECTION[direction]))
        if back is None:
            findings.append(_integrity(
                f"{label}{room(to_id)}: no return edge (one-way passage)"
            ))
        elif back["to_node_id"] != from_id:
            findings.append(_integrity(
                f"{label}{room(to_id)}: return edge points at "
                f"{room(back['to_node_id'])} instead of back"
            ))
        elif bool(e["is_locked"]) != bool(back["is_locked"]) or (
            e["is_locked"] and e["blocking_entity_id"] != back["blocking_entity_id"]
        ):
            findings.append(_integrity(
                f"{label}{room(to_id)}: lock state differs from its return edge "
                "(both sides of a passage must lock together)"
            ))

    # locks must point at a live, uncleared obstacle
    entity_by_id = {ent["id"]: ent for ent in entities}
    for e in edges:
        if not e["is_locked"]:
            continue
        label = f"locked edge {room(e['from_node_id'])} --{e['direction']}-->"
        blocker_id = e["blocking_entity_id"]
        if blocker_id is None:
            findings.append(_integrity(f"{label}: locked but has no blocking entity"))
            continue
        blocker = entity_by_id.get(blocker_id)
        if blocker is None:
            findings.append(_integrity(
                f"{label}: blocking entity {blocker_id} does not exist"
            ))
        elif blocker["holder"] == "gone":
            findings.append(_integrity(
                f"{label}: still locked by entity {blocker_id} "
                f"({blocker['name']!r}), which was destroyed"
            ))
        else:
            props = _load_json(blocker["properties_json"]) or {}
            if props.get("is_cleared"):
                findings.append(_integrity(
                    f"{label}: still locked by entity {blocker_id} "
                    f"({blocker['name']!r}), which is already cleared"
                ))

    # entity placement (TECHNICAL_DETAILS.md §3.1: holder decides node_id)
    for ent in entities:
        label = f"entity {ent['id']} ({ent['name']!r})"
        if ent["holder"] == "room" and ent["node_id"] is None:
            findings.append(_integrity(
                f"{label}: holder is 'room' but node_id is NULL (orphaned)"
            ))
        if ent["holder"] != "room" and ent["node_id"] is not None:
            findings.append(_integrity(
                f"{label}: holder is '{ent['holder']}' but node_id is still set"
            ))
        if _load_json(ent["properties_json"]) is None:
            findings.append(_integrity(f"{label}: properties_json is not valid JSON"))

    # player sanity
    if player["current_node_id"] not in node_by_id:
        findings.append(_integrity(
            f"player_state.current_node_id = {player['current_node_id']} "
            "does not reference an existing room"
        ))
    if not 0 <= player["hp"] <= player["max_hp"]:
        findings.append(_integrity(
            f"hp out of range: {player['hp']} (expected 0..{player['max_hp']})"
        ))
    if _load_json(player["state_flags_json"]) is None:
        findings.append(_integrity("player_state.state_flags_json is not valid JSON"))

    return findings


# --- repair ------------------------------------------------------------

def repair_history_gaps(conn: sqlite3.Connection) -> list[str]:
    """Insert placeholder turn_log rows so old, unfixable logging gaps stop
    failing every future check (a new gap after this fails again). Touches
    nothing but turn_log; integrity findings are never repaired here."""
    if not database.is_seeded(conn):
        return []
    nodes = conn.execute("SELECT * FROM nodes ORDER BY id;").fetchall()
    turns = conn.execute("SELECT * FROM turn_log ORDER BY id;").fetchall()
    player = database.get_player_state(conn)
    if not turns and len(nodes) == 1:
        return []  # fresh game — nothing unlogged yet

    actions: list[str] = []
    logged_node_ids = {t["node_id"] for t in turns}
    last_node_id = turns[-1]["node_id"] if turns else None
    with conn:
        for n in nodes:
            if n["id"] not in logged_node_ids:
                database.insert_turn_log(conn, n["id"], GAP_INPUT, GAP_ROOM_NARRATIVE)
                actions.append(f"gap marker for unlogged node {n['id']} ({n['name']!r})")
                last_node_id = n["id"]
        # Keep the last log entry in the player's current room, or the
        # stale-position check would still (or newly) fail.
        if last_node_id != player["current_node_id"]:
            database.insert_turn_log(
                conn, player["current_node_id"], GAP_INPUT, GAP_MOVES_NARRATIVE
            )
            actions.append("gap marker for unlogged moves since the last logged turn")
    return actions


# --- report ------------------------------------------------------------

def _direction_sorted(edges: list[sqlite3.Row]) -> list[sqlite3.Row]:
    order = {d: i for i, d in enumerate(models.DIRECTIONS)}
    return sorted(edges, key=lambda e: order[e["direction"]])


def print_report(conn: sqlite3.Connection, db_path: str, findings: list[Finding], verbose: bool) -> None:
    size_kb = os.path.getsize(db_path) / 1024
    print(f"Database:  {os.path.abspath(db_path)}  ({size_kb:.0f} KB)")

    if not database.is_seeded(conn):
        _print_findings(findings)
        return

    nodes = conn.execute("SELECT * FROM nodes ORDER BY id;").fetchall()
    node_by_id = {n["id"]: n for n in nodes}
    edges = conn.execute("SELECT * FROM edges;").fetchall()
    turns = conn.execute("SELECT * FROM turn_log ORDER BY id;").fetchall()
    inventory = database.get_inventory(conn)
    gone = conn.execute("SELECT * FROM entities WHERE holder = 'gone' ORDER BY id;").fetchall()
    room_entities = conn.execute("SELECT * FROM entities WHERE holder = 'room' ORDER BY id;").fetchall()
    player = database.get_player_state(conn)
    zone = database.get_zone_config(conn)
    flags = _load_json(player["state_flags_json"]) or {}

    here = node_by_id.get(player["current_node_id"])
    linked_passages = {
        frozenset((e["from_node_id"], e["to_node_id"]))
        for e in edges
        if e["to_node_id"] is not None
    }
    frontiers = sum(1 for e in edges if e["to_node_id"] is None)
    # A locked linked passage has two locked rows (one per side); a locked
    # frontier has one. Count passages, not rows.
    locked = len(
        {
            frozenset((e["from_node_id"], e["to_node_id"]))
            for e in edges
            if e["is_locked"] and e["to_node_id"] is not None
        }
    ) + sum(1 for e in edges if e["is_locked"] and e["to_node_id"] is None)

    if zone:
        print(f"Campaign:  {zone['zone_name']}")
    where = f"{here['name']} ({here['x']},{here['y']},{here['z']})" if here else "<missing room>"
    dead = "  ** DEAD **" if player["hp"] <= 0 else ""
    print(f"Player:    {where} — HP {player['hp']}/{player['max_hp']}{dead}")
    inv_names = ", ".join(r["name"] for r in inventory) or "empty"
    print(f"Inventory: {len(inventory)} item(s) — {inv_names}")
    print(
        f"World:     {len(nodes)} rooms · {len(linked_passages)} linked passages · "
        f"{frontiers} unexplored exits · {locked} locked · "
        f"{len(room_entities)} room entities · {len(gone)} gone"
    )
    if turns:
        last = turns[-1]
        last_room = node_by_id.get(last["node_id"])
        last_where = last_room["name"] if last_room else f"node {last['node_id']}"
        gaps = sum(1 for t in turns if t["player_input"] == GAP_INPUT)
        gap_note = f" ({gaps} gap markers)" if gaps else ""
        print(
            f"Turns:     {len(turns)} logged{gap_note} · "
            f"last at {last['created_at']} UTC in {last_where}"
        )
    else:
        print("Turns:     none logged yet")
    if flags:
        print("Flags:     " + ", ".join(f"{k}={v}" for k, v in sorted(flags.items())))

    if verbose:
        print("\n--- Rooms ---")
        edges_by_node: dict[int, list[sqlite3.Row]] = {}
        for e in edges:
            edges_by_node.setdefault(e["from_node_id"], []).append(e)
        entities_by_node: dict[int, list[sqlite3.Row]] = {}
        for ent in room_entities:
            entities_by_node.setdefault(ent["node_id"], []).append(ent)
        for n in nodes:
            marker = "  <- player" if n["id"] == player["current_node_id"] else ""
            print(f"[{n['id']}] {n['name']} ({n['x']},{n['y']},{n['z']}){marker}")
            for e in _direction_sorted(edges_by_node.get(n["id"], [])):
                if e["to_node_id"] is None:
                    dest = "(unexplored)"
                else:
                    tn = node_by_id.get(e["to_node_id"])
                    dest = f"[{e['to_node_id']}] {tn['name']}" if tn else f"[{e['to_node_id']}] ???"
                lock = f"  [locked: {e['lock_condition'] or 'no condition'}]" if e["is_locked"] else ""
                print(f"    {e['direction']:<5} -> {dest}{lock}")
            for ent in entities_by_node.get(n["id"], []):
                props = _load_json(ent["properties_json"]) or {}
                cleared = ", cleared" if props.get("is_cleared") else ""
                print(f"    * {ent['name']} ({ent['type']}{cleared}, entity {ent['id']})")

        if inventory:
            print("\n--- Inventory ---")
            for ent in inventory:
                print(f"[{ent['id']}] {ent['name']} — {ent['description']}")
        if gone:
            print("\n--- Destroyed / consumed ---")
            for ent in gone:
                print(f"[{ent['id']}] {ent['name']} ({ent['type']})")

        print(f"\n--- Turn history ({len(turns)} turns) ---")
        for t in turns:
            n = node_by_id.get(t["node_id"])
            where = n["name"] if n else f"node {t['node_id']}"
            print(f"#{t['id']}  {t['created_at']} UTC  [{where}]")
            print(f"  > {t['player_input']}")
            print(f"  {t['narrative_output']}\n")

    print()
    _print_findings(findings)


def _print_findings(findings: list[Finding]) -> None:
    if not findings:
        print("Checks:    all passed")
        return
    integrity = sum(1 for f in findings if f.kind == "integrity")
    history = len(findings) - integrity
    parts = [s for s in (
        f"{integrity} integrity" if integrity else "",
        f"{history} history" if history else "",
    ) if s]
    print(f"Checks:    {len(findings)} FAILED ({', '.join(parts)})")
    for f in findings:
        print(f"  ! [{f.kind}] {f.message}")


# --- entry point ---------------------------------------------------------

def _exit_code(findings: list[Finding]) -> int:
    if any(f.kind == "integrity" for f in findings):
        return 1
    return 3 if findings else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="doctor.py",
        description="Inspect a Hermes Adventure Game save and check its consistency.",
    )
    parser.add_argument(
        "--db",
        default=config.DB_PATH,
        help=f"database file to inspect (default: {config.DB_PATH}, same as game.py)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="also dump all rooms, inventory, and the full turn history",
    )
    parser.add_argument(
        "--check-only", action="store_true",
        help="run only the consistency checks; print reasons for any failure "
        "and exit 1 (integrity) or 3 (history gaps only); skips the summary",
    )
    parser.add_argument(
        "--repair-log", action="store_true",
        help="insert placeholder turn_log rows for history gaps (old logging "
        "misses stop failing; new ones still will), then re-run the checks",
    )
    args = parser.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"doctor: database file not found: {args.db}", file=sys.stderr)
        return 2

    conn = database.get_connection(args.db)
    try:
        if args.repair_log:
            actions = repair_history_gaps(conn)
            if actions:
                print(f"doctor: inserted {len(actions)} gap marker(s) in {args.db}:")
                for a in actions:
                    print(f"  + {a}")
            else:
                print(f"doctor: no history gaps to repair in {args.db}")

        findings = run_checks(conn)
        if args.check_only or args.repair_log:
            if findings:
                print(f"doctor: {len(findings)} check(s) failed in {args.db}:")
                for f in findings:
                    print(f"  ! [{f.kind}] {f.message}")
            else:
                print(f"doctor: all checks passed in {args.db}")
        else:
            print_report(conn, args.db, findings, args.verbose)
        return _exit_code(findings)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
