# Hermes Adventure Game

A text adventure engine that turns Hermes Agent into a Game Host.

The split: a **deterministic Python engine** (`game.py` + SQLite) owns all
truth — the map, inventory, HP, flags, locked doors — while the **LLM agent**
does what it's good at: inventing rooms, refereeing creative actions, and
narrating. The agent can only *propose* changes as JSON; the engine
validates and applies them. Because every fact lives in the database, a
game survives context resets, restarts, and days away — any fresh session
picks up exactly where you left off.

## Requirements

- Python 3.10+
- `pydantic` (the only dependency)

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -r requirements-dev.txt   # optional: pytest
.venv/bin/python -m pytest -q                   # optional: verify (110 tests)
```

## Installing as an agent skill

The agent-facing contract is `SKILL.md`. Symlink the **whole repo** into
your agent's skills directory — the skill needs `game.py`, `campaigns/`,
and `references/` sitting next to it, and it creates its save database
there too:

```bash
ln -s /path/to/HermesAdventureGame ~/.hermes/skills/adventure-game-host
```

Any agent host that discovers `SKILL.md`-style skills works the same way
(for Claude Code, the directory is `~/.claude/skills/`). Adjust the target
to wherever your agent looks for skills.

## Playing

Tell your agent something like **"let's play the adventure game"**. The
skill takes it from there:

- **New save** — the agent offers a campaign: one of the six predefined
  ones below, a custom world it negotiates with you (theme, starting
  point, win condition), or the built-in endless Mechanical Spire sandbox.
- **Existing save** — it resumes, re-describing where you stand; nothing
  is lost between sessions.

Then just play in plain language: `go north`, `take the wrench`,
`pry the hatch open with the crowbar`, `ask the ghost what she wants`,
`what does this valve do?`. Movement and pickups are mechanical; anything
creative is refereed on plausibility — clever, physically sensible plans
work, and you never need exact magic words.

Worth knowing:

- **No pixel-hunting.** Everything interactive is always mentioned, and
  anything with hidden depth is visibly clued. Examining random scenery
  never reveals anything, so don't grind — follow the clues.
- **Examine freely.** Asking what something is or does gets you complete,
  practical information before you commit to using it.
- **Campaigns have goals.** Reach one and you win — you can keep exploring
  afterward as an epilogue, or reset for a new game. Dying ends the run.

### Predefined campaigns

| Campaign | Premise | You win when… |
|---|---|---|
| `derelict_starship` | Wake alone on a powerless freighter | the reactor is back online |
| `pirate_islands` | Sail the Scattered Teeth after a torn map | you find Blackbeard's gold |
| `clockwork_spire` | A vertical labyrinth of dead machinery | the Great Clock strikes again |
| `drowned_archive` | A flooded library and its pump-works | the Founders' Folio is recovered |
| `blackthorn_manor` | A gothic curse bound by house logic | the wrong is named and set right |
| `amber_tomb` | A 1920s dig into a trapped necropolis | the scarab sees daylight again |

Ask the agent to switch campaigns any time (it will warn you: switching
wipes the current world — `export-world` first if you want it back), or
start one directly:

```bash
python game.py reset < campaigns/pirate_islands.json
```

## Save management & tools

The save is a single SQLite file next to the code (`hermes_game.db`, set by
`DB_PATH` in `config.py`; the `HERMES_DB_PATH` env var overrides it).
Copying the file copies the game in progress.

`doctor.py` is the human-side inspector — handy during development:

```bash
python doctor.py             # summary: campaign, player, world stats, checks
python doctor.py -v          # + full room graph, inventory, turn history
python doctor.py --check-only    # consistency checks; exit 0/1/2/3
python doctor.py --repair-log    # plug old turn-log gaps with markers
python doctor.py --db other.db   # inspect a different save
```

Useful engine commands (each prints one JSON object):

```bash
python game.py state             # the full current situation
python game.py export-world      # shareable campaign seed — others can
                                 # `init` it to play your campaign fresh
python game.py reset             # restart the current campaign from turn one
```

## Project layout

| File | Role |
|---|---|
| `game.py` | CLI entry point — the JSON contract the agent drives |
| `engine.py` | rules: movement, generation, refereeing limits, win/auto-clear logic |
| `database.py` | SQLite schema and row-level helpers |
| `models.py` | Pydantic schemas for everything the agent proposes |
| `config.py` | DB path and the built-in default campaign |
| `doctor.py` | save inspector / consistency checker (human-facing) |
| `SKILL.md` | the Game Host contract the agent runs on |
| `campaigns/` | predefined worlds (`README.md` there explains writing your own) |
| `references/` | deeper agent-facing guidance |
| `PLAN.md`, `TECHNICAL_DETAILS.md` | design history and full specification |
