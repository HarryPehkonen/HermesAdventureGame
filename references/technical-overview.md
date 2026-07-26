# Technical Overview

Agent-facing summary of the engine. Full specifications live in
`TECHNICAL_DETAILS.md` at the repo root; this is the short version a host
session actually needs.

## Architecture

- **`game.py` + `engine.py` own all truth** in SQLite: the 3D room grid,
  edges (exits), entities, player state, turn log, and campaign config.
- **You propose, the engine disposes.** Room generation and state changes
  are JSON proposals validated by Pydantic (`models.py`) and by engine
  rules. Rejections come back with reasons — fix and retry once, then
  narrate around it.
- Every `game.py` command prints exactly one JSON object and exits 0, even
  for game-level failures (`{"ok": false, "error": ...}`). Exit 1 means an
  actual bug.

## Commands

| Command | stdin | Purpose |
|---|---|---|
| `init` | optional WorldInit | seed a new campaign if the save is empty (idempotent) |
| `reset` | optional WorldInit | wipe and restart: replay stored campaign, or start the piped one |
| `export-world` | — | print the stored WorldInit (shareable, restartable) |
| `state` | optional `{"player_input", "narrative"}` | logs that (the *previous* turn) if present, then the full current situation — run this first, every turn |
| `move <dir>` | — | move north/south/east/west/up/down |
| `create-room <dir>` | RoomGeneration | materialize the room you invented for a frontier |
| `take <entity_id>` | — | pick up an obvious item |
| `apply` | StateChanges | submit refereed outcomes (damage, items, obstacles, flags) |
| `log` | `{"player_input", "narrative"}` | manual fallback — append a turn directly; normal play logs via `state`'s stdin instead |

## Response fields worth knowing

- **`state`**: `zone`, `room` (with `exits`: direction/locked/generated),
  `inventory`, `hp`/`max_hp`, `player_dead`, `flags`, `win_flag`
  (null = endless sandbox), `game_won`, `win_message` (only once won),
  `recent_turns`, `logged_previous_turn` (whether a piped payload was
  written to `turn_log`), `log_error` (present only if the payload was
  rejected — state is still returned either way).
- **`move`**: `moved` + `room`; or `needs_generation` + `context` (theme,
  flags, exiting room, neighbors — respect all of it); or errors
  `no_exit` / `locked` (with `lock_condition`) / `player_dead`. May include
  `auto_cleared` (see below).
- **`apply`**: `applied` + `rejected` (narrate only what was applied),
  `player_dead`, and `game_won` + `win_message` when `flags_set` newly sets
  the campaign's win flag.

## Mechanics

- Rooms live on a 3D lattice; exits are edges, and an exit with no room
  behind it yet is a *frontier* — moving into it triggers `needs_generation`.
  Loops are real: a frontier can connect to an existing room. A neighbor's
  wall always wins over a declared exit.
- Obstacles can lock exits (`blocks_direction`) and can name a flag that
  resolves them (`cleared_by_flag`). The **engine** auto-clears such
  obstacles when the flag is set — current room immediately at `apply`,
  distant rooms lazily on entry, including through the locked passage
  itself. Results report ids under `auto_cleared` /
  `auto_cleared_obstacles`; narrate the change, never re-submit it.
- Death (`hp` 0) blocks every command until `reset`. Winning blocks
  nothing — post-win play is an epilogue.
- `doctor.py` (plain text, not JSON) inspects the save: `--check-only`
  exits 0 (clean), 3 (turn-log gaps — run `--repair-log` once), 1 (state
  corruption — leave it alone, tell the developer), 2 (no save file).

## Extending the game

- New command: function in `engine.py` → entry in `game.py` `COMMANDS` and
  `build_parser()` → document in SKILL.md.
- New rules or schema fields: `engine.py` validation, `models.py` schemas.
- New campaigns: JSON files in `campaigns/` (see `campaigns/README.md`);
  the built-in default lives in `config.py`.
