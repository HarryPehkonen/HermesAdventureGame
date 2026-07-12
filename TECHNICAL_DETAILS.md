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
| `game.py init` | optional `WorldInit` JSON on stdin | seeds DB if empty — from the payload (custom campaign) or the built-in default; `{"ok": true, "new_game": bool}`. A payload against an already-seeded DB fails loudly with `already_seeded` (never silently ignored). |
| `game.py reset` | optional `WorldInit` JSON on stdin | wipes all rows and re-seeds. No payload = **replays the stored campaign** (same theme, room, loadout); with payload = starts a different campaign |
| `game.py export-world` | — | prints the stored `WorldInit` payload — shareable; another player pipes it to `init` |
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

**Seed content:** campaigns are seeded from a `WorldInit` payload (§2) —
zone theme, starting room at (0, 0, 0), and starting inventory — negotiated
with the player in SKILL.md's Lobby Mode and piped to `init`. When no payload
is supplied, `init` falls back to the built-in default campaign hardcoded in
`config.py` (zone "Mechanical Spire" from PLAN.md Phase 4; starting room
"Rust-Chamber" with 3 frontier exits and no entities). Either way the payload
is **persisted** in `game_config.world_init_json`, so `reset` can replay the
same campaign and `export-world` can share it. Everything beyond the starting
room is agent-generated at play time — a replay of the same campaign
regenerates a fresh world beyond room one.

**Stdin payload detection** (`init`/`reset`): a TTY or an empty/whitespace
pipe means "no payload"; only actual stdin content is parsed as `WorldInit`.
Never require a payload — agent subprocesses routinely run with an empty
pipe or `/dev/null` on stdin.

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
    blocks_direction: Optional[Direction] = None  # obstacles only; must be one of this room's exits
    cleared_by_flag: Optional[str] = None      # obstacles only; auto-cleared when this flag is set (§5.5)
    traits: list[str] = []

class RoomGeneration(BaseModel):
    room_name: str
    description: str                  # 2-3 sentences
    exits: list[Direction]            # CLI forces the return direction in
    entities: list[GeneratedEntity]   # keep to 0-2

class StateChanges(BaseModel):
    obstacles_cleared_entity_ids: list[int] = []   # may clear several per turn
    damage_to_player: int = 0                  # clamped to 0..100
    healing_to_player: int = 0                 # clamped to 0..100; HP capped at max_hp
    items_removed_from_inventory: list[int] = []   # entity IDs
    items_added_to_inventory: list[int] = []       # must be in current room
    entities_destroyed: list[int] = []
    flags_set: dict[str, bool] = {}
```

```python
class WorldInit(BaseModel):
    zone_name: str
    zone_description: str
    global_theme_rules: str
    starting_room: RoomGeneration
    starting_inventory: list[GeneratedEntity] = []
    win_flag: Optional[str] = None     # flag that completes the campaign (§5.6); None = endless sandbox
    win_message: Optional[str] = None  # narrated on victory; must be paired with win_flag
```

`WorldInit` validators: `starting_room.exits` must be non-empty (no softlocked
starts); `starting_inventory` entries must be `type: "item"`; and starting-room
obstacles' `blocks_direction` must be in `starting_room.exits` — the seed room
has no implicit return direction, so this check lives at the model level here,
unlike `RoomGeneration` (next note).

Note on `blocks_direction` validation for `RoomGeneration`: the Pydantic layer
does **not** check `blocks_direction ∈ exits` — `create-room` does, *after*
appending the implicit return direction. This allows a cave-in/trap room that
blocks the way the player came in without the agent listing that direction in
`exits`.

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
    blocking_entity_id INTEGER,          -- the obstacle that locks this edge, if any
    PRIMARY KEY (from_node_id, direction),
    FOREIGN KEY (from_node_id) REFERENCES nodes(id) ON DELETE CASCADE,
    FOREIGN KEY (to_node_id) REFERENCES nodes(id) ON DELETE CASCADE,
    FOREIGN KEY (blocking_entity_id) REFERENCES entities(id) ON DELETE SET NULL
);
```

No edge row = wall (no generation, no LLM effort). Frontier = generate.
Target set = move (if unlocked).

### 3.3 `blocks_direction`: linking an obstacle to the exit it locks

An obstacle only locks movement when the agent sets `blocks_direction` to one
of the room's own `exits`. At `create-room` time: for that direction's edge,
set `is_locked = 1`, `lock_condition = solution_condition`, and
`blocking_entity_id = <the new entity's id>` — on **both** rows of the pair
(the reverse edge is locked from the other side too; a hatch blocks passage
either direction until cleared). `apply`'s `obstacles_cleared_entity_ids`
finds the edge pairs by `blocking_entity_id` and unlocks both rows of each. An obstacle with
`is_blocking = true` but no `blocks_direction` is narrative-only (blocks
*access to something in the room*, not movement) and locks nothing.

### 3.4 `player_state`: enforce the singleton

```sql
id INTEGER PRIMARY KEY CHECK (id = 1)
```

### 3.5 New table: `turn_log`

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

### 3.6 `game_config`: persist the campaign seed

`game_config` gains `world_init_json TEXT NOT NULL` — the exact `WorldInit`
payload the campaign was seeded from. `reset` (no payload) replays it;
`export-world` prints it for sharing. The full SQLite DB file remains the
save game: copying `hermes_game.db` transfers a campaign *in progress*, while
the exported `WorldInit` shares a campaign *from the beginning*.

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
   matching frontier back toward us, link both edge pairs, carrying the
   neighbor frontier's lock state onto our side; if that frontier was locked,
   return `locked` without moving (a lock blocks passage in either direction,
   §3.3), otherwise move (no generation). If found but walled on that side,
   return `no_exit` and close our frontier.
3. Truly empty → return `needs_generation` + context (§1).

On `create-room <direction>` (agent has invented the room):

1. Re-verify the target coordinates are still empty and the frontier still
   exists (defends against duplicate/replayed calls).
2. In **one transaction**: insert the node; force the return direction into
   `exits`; insert entities (`holder='room'`) first so their IDs exist; for
   each exit insert an edge pair — pairing with an existing neighbor only if
   the neighbor has a matching frontier toward us (a neighbor's wall stays a
   wall), otherwise a frontier; for any entity with `blocks_direction` set,
   lock that edge pair per §3.3; resolve the crossed edge pair; move the
   player.
3. Return the new room state.

## 5. `apply` validation rules

**Dead-player gate:** once HP is 0, `move`, `take`, `create-room`, and
`apply` all return `{"ok": false, "error": "player_dead"}` — the engine
enforces death mechanically rather than trusting the agent to notice the
flag. `state`, `log`, and `reset` remain available (the agent needs them to
narrate the death and restart).

Each sub-change is validated independently; invalid ones are **rejected and
reported**, valid ones still apply — the turn never hard-fails because the
agent hallucinated one entity ID.

- `items_added_to_inventory`: entity must be `holder='room'` in the *current*
  room; obstacles/NPCs are never takeable. `can_pickup` is **not** checked
  here — `apply` is the referee's channel, and the agent has already judged
  the acquisition plausible (e.g. prying a fixed part loose). Only the
  mechanical `take` command enforces `can_pickup`.
- `items_removed_from_inventory`: entity must be `holder='player'`. Removal
  sets `holder='gone'` unless also listed in a room drop (v1: gone).
- `obstacles_cleared_entity_ids`: each must be an obstacle in the current
  room; sets `is_cleared: true` in `properties_json` and, if any edge pair
  has `blocking_entity_id` equal to that entity, unlocks both rows of the
  pair (§3.3). A list — one clever action may clear several obstacles.
- `entities_destroyed`: must be in the current room or inventory →
  `holder='gone'`. A destroyed entity also unlocks any edges it was blocking
  (same unlock as clearing) — the row survives as `holder='gone'`, so the
  FK's `ON DELETE SET NULL` never fires and without this the door would jam
  permanently.
- `damage_to_player` / `healing_to_player`: each clamped 0–100, then applied
  as a **net** change (`hp - damage + healing`) clamped to `0..max_hp` — same
  -turn healing offsets damage but can't revive through a lethal net total or
  overheal past `max_hp`. The response includes `"player_dead": true` when HP
  hits 0 — the agent narrates death and offers a restart via `game.py reset`.
- `flags_set`: merged into `state_flags_json` (no validation — free namespace).

## 5.5 Environmental states & flag-linked obstacles

From `improvement_plan.pdf`, amended so auto-resolution is engine-side.

**Composition over hardcoding.** Temporary conditions (darkness, flooding,
gas) are never baked into a room's stored `description` — that would need
2^N descriptions for N conditions. The base description stays neutral,
permanent architecture; each hostile condition is an `obstacle` entity
(usually `is_blocking: true` with a `blocks_direction` and a
`solution_condition`), and the agent synthesizes prose from base + active
obstacles each turn.

**Multi-room puzzles via lazy evaluation.** A puzzle solved in Room A
(breaker flipped → `flags_set: {"power_restored": true}`) may resolve
obstacles in rooms far away. `apply` only accepts obstacle clears in the
current room, so distant effects are carried by the flag and resolved
lazily. The link is machine-readable, not prompt discipline: an obstacle's
optional `cleared_by_flag` (stored in `properties_json`) names the flag
that resolves it, and the **engine** clears it — sets `is_cleared`, unlocks
any edges it blocks via `blocking_entity_id` — at these points:

1. `apply` with `flags_set`: current-room obstacles, immediately
   (reported as `applied.auto_cleared_obstacles`).
2. `move` into a room: that room's satisfied obstacles
   (reported as `auto_cleared`).
3. `move` through a locked edge whose blocking obstacle's flag is
   satisfied: the obstacle clears and the move succeeds — necessary
   because the blocker may sit on the far side of the only way in.
4. `create-room`: safety net; SKILL.md tells the agent not to generate
   obstacles a set flag already neutralizes, but a slip-through clears
   instantly (reported as `auto_cleared`).

An uncleared satisfied obstacle in a *far* room is therefore normal
(pending lazy resolution); one in the player's *current* room is corruption
— doctor.py checks exactly that as an integrity finding.

**Generation context** (`_build_generation_context`) includes the player's
`flags` so newly generated rooms respect already-solved puzzles.

The agent's role shrinks to the creative parts: inventing the puzzle,
proposing the flag, choosing which generated obstacles carry
`cleared_by_flag`, and narrating auto-cleared results. It never decides
*whether* a flag-linked obstacle resolves.

## 5.6 Win condition

A campaign may name a `win_flag` (paired with a `win_message`) in its
`WorldInit`; both default to `None`, which is the pre-existing behavior — an
endless sandbox. Existing saves and exports remain valid.

The win is **derived state, exactly like `player_dead`**: the flag lives in
`player_state.state_flags_json`, the flag *name* lives in the stored
`world_init_json`, so `game_won` is computable from the DB alone and
survives context loss. Three touchpoints:

- `state` always reports `win_flag` (the goal — so an amnesiac session
  knows what it is refereeing toward), `game_won`, and, once won,
  `win_message`.
- `apply` adds `game_won: true` + `win_message` to its normal result when
  `flags_set` *newly* sets the win flag — appended to, never replacing,
  `applied`/`rejected`/`player_dead` (a lethal winning action reports both).
- doctor's summary shows the goal flag and a `** WON **` marker.

Winning never gates commands: the player may keep exploring post-win
(`state` keeps saying `game_won: true`; SKILL.md frames post-win play as an
epilogue). Dying still blocks everything. `reset` replays the campaign with
flags wiped, so `game_won` naturally returns to false.

The agent's role: negotiate the goal at campaign creation, judge *when* it
is accomplished, and set the flag via `apply` — the engine decides that the
game is won.

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
- **Visibility & clues (no pixel-hunting):** the room's entity list is the
  complete set of mechanically real things, and the narration contract
  keeps it that way — every entity is always narrated, examining
  non-entity scenery never yields anything real (no inventing items as
  search rewards), entities with non-obvious uses are telegraphed with a
  clue (generator marks them with a trait like `conspicuous` so the hint
  survives context loss), and secrets are gated by visible puzzles
  (obstacles, locks, flags), never by guessing which noun to examine.
  This is narration-level prompt discipline by necessity — there is no
  state for the engine to enforce, and the worst violation is flavor
  inconsistency, not save corruption.

## 7. Error handling

- CLI: all anticipated failures are `{"ok": false, "error": ...}` JSON with
  exit 0. Unexpected exceptions may exit 1, but stdout must still be JSON
  (`{"ok": false, "error": "internal", ...}`).
- Agent (via SKILL.md): on a validation error, fix the proposal and retry
  once; if it still fails, narrate around it ("Nothing happens.") rather than
  surfacing errors to the player.

## 7.5 doctor.py (save inspector / consistency checker)

A separate human-facing script, deliberately **not** a `game.py` subcommand:
`game.py`'s stdout is a strict one-JSON-object contract, and mixing in a
plain-text report risks the agent confusing the two.

- `python doctor.py` — short summary (which DB file, campaign, player, room,
  HP, inventory, world stats, turn count) plus any failed checks.
- `-v` / `--verbose` — adds the full room graph, inventory with descriptions,
  destroyed entities, and the complete turn history.
- `--check-only` — checks only, each failure printed with its reason.
- `--repair-log` — insert placeholder turn_log rows for history gaps, then
  re-run the checks (see below).
- `--db PATH` — inspect another save; defaults to `config.DB_PATH` so it
  always looks at the same file `game.py` uses.
- Exit codes: 0 all checks passed, 1 an integrity check failed, 2 DB
  missing, 3 only history-gap checks failed. Read-only apart from
  `--repair-log`, which touches nothing but turn_log.

Findings come in two kinds with different lifecycles:

- **Integrity** (exit 1): the current world state is inconsistent —
  edge-pair reciprocity/geometry/lock symmetry (§3.2, §3.3), locks pointing
  at destroyed or already-cleared blockers, entity holder/node_id
  consistency (§3.1), JSON validity, player HP/position sanity. Never
  auto-repaired; the agent must leave these to the developer.
- **History gaps** (exit 3): turn_log is incomplete — a visited room with no
  logged turns, or the player having moved since the last log entry.
  Heuristic, not proof: turn_log is itself the only record of turns. A gap
  is a permanent scar, so without repair it would fail every future check
  forever and train everyone to ignore the alarm. `--repair-log` plugs old
  gaps with clearly-marked placeholder rows (`player_input` =
  `(turn not logged)`); any *new* gap fails again, which is the regression
  signal that matters. The summary reports the accumulated gap-marker count
  as a sloppiness audit trail.

SKILL.md tells the agent: run `doctor.py --check-only` when starting or
resuming a session; on exit 3 run `--repair-log` once and move on; on exit 1
repair nothing and alert the player out of character. Mid-session, a
just-missed log entry should instead be logged late with its real content
while it is still in the agent's context (only sound if the player is still
in the same room, since `log` stamps the current node).

## 8. File layout & build order

| Step | File | Contents |
|---|---|---|
| 1 | `config.py` | DB path, constants |
| 2 | `models.py` | Pydantic proposal schemas (§2) |
| 3 | `database.py` | schema DDL (§3), CRUD, edge-pair helpers, transactions |
| 4 | `engine.py` | coordinate math, movement/generation logic (§4), apply rules (§5) |
| 5 | `game.py` | argparse CLI, stdin JSON handling, JSON output (§1) |
| 6 | `SKILL.md` | agent contract (§6) — draft exists; finalize against the built CLI |
| 7 | `doctor.py` | save inspector & consistency checks (§7.5), human-readable output |

Dependencies: stdlib + `pydantic` only. Each step should be testable before
the next: pytest for edge-pair consistency, holder transitions, frontier
linking/loop closure, and apply-rule rejections; then an end-to-end smoke test
driving `game.py` as a subprocess the way the agent will.
