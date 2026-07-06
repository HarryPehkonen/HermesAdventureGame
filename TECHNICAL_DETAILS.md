# Technical Details

Implementation spec for the Hermes Adventure Game. This document extends
`PLAN.md`; where the two disagree, this document wins. It exists so the
execution phase (Sonnet) can implement mechanically without re-deriving design
decisions.

**Superseded parts of PLAN.md:** Phase 4 (LLM prompt specs) and the
`llm_client.py` / `game_loop.py` items in Phase 5 no longer apply — see §0.

## 0. Architecture: Hermes Agent is the narrator

There is **no LLM client in the Python code**. Hermes Agent itself plays the
Game Host: it invents room content, judges free-form player actions, and
narrates — guided by `SKILL.md`. The Python side is a deterministic CLI state
machine that owns the authoritative game state in SQLite.

```
User ⇄ (Telegram etc.) ⇄ Hermes Agent ⇄ game.py CLI ⇄ hermes_game.db
        ≤3000 chars/turn     │
                             └─ SKILL.md (protocol + narration rules)
```

Division of labor:

| Concern | Owner |
|---|---|
| Room names, descriptions, entities (creativity) | Hermes Agent |
| Action plausibility judgment (refereeing) | Hermes Agent |
| Narration, tone, message length | Hermes Agent (per SKILL.md) |
| Map topology, coordinates, edge consistency | `game.py` |
| Inventory, HP, flags, entity locations | `game.py` |
| Validating/rejecting agent-proposed state changes | `game.py` |

The agent never mutates state directly and never trusts its own memory of the
world — it reads state from the CLI every turn and proposes changes as JSON,
which the CLI validates before applying. Every proposal is treated as
untrusted input: referenced entity IDs must exist in the claimed location,
damage is clamped to 0–100, and each command's writes happen in one SQLite
transaction.

## 1. CLI contract (`game.py`)

Single entry point with subcommands. **All output is compact JSON on stdout.**
Game-level failures (invalid move, bad proposal) return
`{"ok": false, "error": "<short reason>"}` with exit code 0 so the agent can
read and react; exit code 1 is reserved for actual bugs. Never print
tracebacks or prose to stdout.

Proposals are passed as JSON on stdin (not argv — avoids shell-quoting
breakage with apostrophes in generated prose).

| Command | Input | Output (on success) |
|---|---|---|
| `game.py init` | — | seeds DB if empty (idempotent); `{"ok": true, "new_game": bool}` |
| `game.py reset` | — | deletes all rows and re-seeds (player death / restart) |
| `game.py state` | — | full situation: current room, exits (with lock/frontier status), room entities, inventory, hp, flags — everything needed to narrate a turn |
| `game.py move <dir>` | direction arg | one of: `{"ok":true,"moved":true,"room":{...}}` · `{"ok":true,"needs_generation":true,"context":{...}}` · `{"ok":false,"error":"no_exit"|"locked",...}` |
| `game.py create-room <dir>` | `RoomGeneration` JSON on stdin | validates, writes node + edges + entities transactionally, moves player; returns the new room state |
| `game.py apply` | `StateChanges` JSON on stdin | applies validated changes; returns `{"applied":{...},"rejected":[{"change":...,"reason":...}]}` |
| `game.py take <entity_id>` | id arg | moves item to inventory if `can_pickup` and unblocked, else error |
| `game.py log` | `{"player_input":...,"narrative":...}` on stdin | appends to `turn_log`; returns `{"ok":true}` |

`move` with `needs_generation` returns a `context` object containing
everything the agent needs to invent the room without further calls: target
coordinates, direction of travel, the room being exited (name + description),
the zone theme from `game_config`, and all already-generated rooms at the six
coordinates adjacent to the target (name + one-line description + whether they
have an exit facing the target). Room-at-target-already-exists is handled
inside `move` (loops close automatically; see §4) — `needs_generation` is only
returned when the target coordinates are truly empty.

**Seed content:** `init` hardcodes (in `config.py`) the starting zone from
PLAN.md Phase 4 — zone "Mechanical Spire", an ancient vertical labyrinth of
clockwork gears and leaking steam — plus one handcrafted starting room at
(0, 0, 0) ("Rust-Chamber", steam-hissing pipes, steel-grate floor) with 3
frontier exits (north, east, up) and no entities. Everything beyond that
first room is agent-generated at play time.

## 2. Proposal schemas (validated with Pydantic inside the CLI)

```python
from typing import Literal, Optional
from pydantic import BaseModel

Direction = Literal["north", "south", "east", "west", "up", "down"]

class GeneratedEntity(BaseModel):
    name: str
    type: Literal["item", "obstacle", "npc"]
    description: str
    can_pickup: bool
    is_blocking: bool
    solution_condition: Optional[str] = None   # obstacles only
    traits: list[str] = []

class RoomGeneration(BaseModel):
    room_name: str
    description: str                  # 2-3 sentences
    exits: list[Direction]            # CLI forces the return direction in
    entities: list[GeneratedEntity]   # keep to 0-2

class StateChanges(BaseModel):
    obstacle_cleared_entity_id: Optional[int] = None
    damage_to_player: int = 0                  # clamped to 0..100
    items_removed_from_inventory: list[int] = []   # entity IDs
    items_added_to_inventory: list[int] = []       # must be in current room
    entities_destroyed: list[int] = []
    flags_set: dict[str, bool] = {}
```

These schemas are documented verbatim in `SKILL.md` so the agent emits them
directly. Validation failures return `{"ok": false, "error": ...}` with a
message specific enough for the agent to fix and retry once.

## 3. Schema amendments to PLAN.md

### 3.1 `entities`: make location explicit

`node_id IS NULL` meaning "inventory **or** destroyed" is ambiguous. Add a
holder column:

```sql
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id INTEGER,                    -- set when holder = 'room'
    holder TEXT NOT NULL DEFAULT 'room'
        CHECK (holder IN ('room', 'player', 'gone')),
    name TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('item', 'obstacle', 'npc')),
    description TEXT NOT NULL,
    properties_json TEXT NOT NULL,
    FOREIGN KEY (node_id) REFERENCES nodes(id) ON DELETE SET NULL
);
```

Inventory = `SELECT * FROM entities WHERE holder = 'player'`.

### 3.2 `edges`: frontier edges and pair consistency

1. **Ungenerated neighbors.** A new room's exit may lead into empty space.
   Make `to_node_id` nullable: NULL target = *frontier* (exit into ungenerated
   space). Crossing it triggers generation, then the target is filled in.
2. **Directional consistency.** Every passage is two rows (A→north and
   B→south). `database.py` exposes helpers that always write/update both rows
   in the same transaction — including lock state, so a door can't be locked
   from one side and open from the other.

```sql
CREATE TABLE IF NOT EXISTS edges (
    from_node_id INTEGER NOT NULL,
    to_node_id INTEGER,                 -- NULL = frontier (room not generated yet)
    direction TEXT NOT NULL
        CHECK (direction IN ('north','south','east','west','up','down')),
    is_locked INTEGER NOT NULL DEFAULT 0,
    lock_condition TEXT,
    PRIMARY KEY (from_node_id, direction),
    FOREIGN KEY (from_node_id) REFERENCES nodes(id) ON DELETE CASCADE,
    FOREIGN KEY (to_node_id) REFERENCES nodes(id) ON DELETE CASCADE
);
```

No edge row = wall (no generation, no LLM effort). Frontier = generate.
Target set = move (if unlocked).

### 3.3 `player_state`: enforce the singleton

```sql
id INTEGER PRIMARY KEY CHECK (id = 1)
```

### 3.4 New table: `turn_log`

Player input + narrative per turn. Gives the agent continuity across turns
(and across Hermes Agent context resets — the DB, not the agent's memory, is
the source of truth) and doubles as a save/replay record. `state` returns the
last ~6 rows.

```sql
CREATE TABLE IF NOT EXISTS turn_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id INTEGER,
    player_input TEXT NOT NULL,
    narrative_output TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (node_id) REFERENCES nodes(id)
);
```

Everything else in the PLAN.md schema stands as written.

## 4. Movement & generation algorithm (inside the CLI)

On `move <direction>` from node at `(x, y, z)`:

1. Look up edge `(current_node, direction)`:
   - no row → `{"ok": false, "error": "no_exit"}`
   - locked → `{"ok": false, "error": "locked", "lock_condition": ...}`
   - target set → move player, return room state
2. Frontier edge: compute target coordinates from the offset table and
   **check `nodes` for an existing room there** — loops can make a frontier
   point at a room generated from another side. If found *and* it has a
   matching frontier back toward us, link both edge pairs and move (no
   generation). If found but walled on that side, return `no_exit` and close
   our frontier.
3. Truly empty → return `needs_generation` + context (§1).

On `create-room <direction>` (agent has invented the room):

1. Re-verify the target coordinates are still empty and the frontier still
   exists (defends against duplicate/replayed calls).
2. In **one transaction**: insert the node; force the return direction into
   `exits`; for each exit insert an edge — pairing with an existing neighbor
   only if the neighbor has a matching frontier toward us (a neighbor's wall
   stays a wall), otherwise a frontier; insert entities (`holder='room'`);
   resolve the crossed edge pair; move the player.
3. Return the new room state.

## 5. `apply` validation rules

Each sub-change is validated independently; invalid ones are **rejected and
reported**, valid ones still apply — the turn never hard-fails because the
agent hallucinated one entity ID.

- `items_added_to_inventory`: entity must be `holder='room'` in the *current*
  room; obstacles/NPCs are never takeable.
- `items_removed_from_inventory`: entity must be `holder='player'`. Removal
  sets `holder='gone'` unless also listed in a room drop (v1: gone).
- `obstacle_cleared_entity_id`: must be an obstacle in the current room; sets
  `is_cleared: true` in `properties_json` and unlocks the associated edge
  **pair** if one references it.
- `entities_destroyed`: must be in the current room or inventory → `holder='gone'`.
- `damage_to_player`: clamped 0–100; HP floor 0. The response includes
  `"player_dead": true` when HP hits 0 — the agent narrates death and offers
  a restart via `game.py reset`.
- `flags_set`: merged into `state_flags_json` (no validation — free namespace).

## 6. SKILL.md (the agent-facing contract)

`SKILL.md` in the repo root tells Hermes Agent how to host the game. A full
draft ships in this repo; keep these invariants when editing it:

- **Message budget: hard cap 3000 characters per user-facing message** (chat
  transports like Telegram). Target ~1200–1800. Room descriptions 2–3
  sentences; never dump raw JSON to the player.
- The DB is the only truth: read `state` at the start of every turn; never
  answer from memory of previous turns.
- Protocol per turn: interpret player intent → run the matching CLI command →
  if `needs_generation`, invent a room consistent with the returned context
  and call `create-room` → for free-form actions, judge plausibility yourself,
  then submit the outcome via `apply` → `log` the turn → narrate.
- Mechanical commands (`move`, `take`, `state`) need no judgment call; only
  creative/ambiguous actions require the agent to referee.

## 7. Error handling

- CLI: all anticipated failures are `{"ok": false, "error": ...}` JSON with
  exit 0. Unexpected exceptions may exit 1, but stdout must still be JSON
  (`{"ok": false, "error": "internal", ...}`).
- Agent (via SKILL.md): on a validation error, fix the proposal and retry
  once; if it still fails, narrate around it ("Nothing happens.") rather than
  surfacing errors to the player.

## 8. File layout & build order

| Step | File | Contents |
|---|---|---|
| 1 | `config.py` | DB path, constants |
| 2 | `models.py` | Pydantic proposal schemas (§2) |
| 3 | `database.py` | schema DDL (§3), CRUD, edge-pair helpers, transactions |
| 4 | `engine.py` | coordinate math, movement/generation logic (§4), apply rules (§5) |
| 5 | `game.py` | argparse CLI, stdin JSON handling, JSON output (§1) |
| 6 | `SKILL.md` | agent contract (§6) — draft exists; finalize against the built CLI |

Dependencies: stdlib + `pydantic` only. Each step should be testable before
the next: pytest for edge-pair consistency, holder transitions, frontier
linking/loop closure, and apply-rule rejections; then an end-to-end smoke test
driving `game.py` as a subprocess the way the agent will.
