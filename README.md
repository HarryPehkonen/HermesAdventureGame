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
.venv/bin/python -m pytest -q                   # optional: verify (114 tests)
```

## Installing as an agent skill

```bash
./install.sh --profile adventure     # into ~/.hermes/profiles/adventure/skills/
./install.sh --global                # into ~/.hermes/skills/ (all profiles)
./install.sh --skills-dir ~/.claude/skills   # any SKILL.md-style host
```

With no flags it auto-detects: one Hermes profile is used automatically,
several make it stop and ask rather than install where your agent isn't
looking. Re-running is safe. `./install.sh --help` lists the rest.

It does two things:

1. **Symlinks the whole repo** into the skills directory as
   `adventure-game-host` — the skill needs `scripts/`, `campaigns/`, and
   `references/` beside `SKILL.md`, and keeps its save in `data/`.
2. **Writes `adventure-game` and `adventure-doctor` launchers** into
   `~/.local/bin` (`--bin-dir` to change, `--no-bin` to skip).

The launchers are the part that matters. `SKILL.md` drives the engine
through them, and they carry absolute paths, so the agent can run the game
from whatever directory its session happens to be in. Without them the
agent has to search the filesystem for `game.py` on every cold session —
under a chat gateway, the working directory is never this repo. For the
same reason the save file is located relative to the installed skill, not
the current directory.

> Installing by hand instead? Symlink the repo yourself, then either put
> equivalent launchers on PATH or accept that the agent must `cd` here
> first — `python scripts/game.py …` assumes this repo is the cwd.

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
adventure-game reset --campaign pirate_islands
```

## Save management & tools

The save is a single SQLite file at `data/hermes_game.db` inside the
installed skill, resolved from the code's own location so it doesn't move
with your working directory. `HERMES_DB_PATH` overrides it — that's the hook
for pointing a container at a mounted volume, and what the tests use. It's
runtime state, not source: kept out of `scripts/` and gitignored. Copying
the file copies the game in progress.

`adventure-doctor` is the human-side inspector — handy during development:

```bash
adventure-doctor                 # summary: campaign, player, world stats, checks
adventure-doctor -v              # + full room graph, inventory, turn history
adventure-doctor --check-only    # consistency checks; exit 0/1/2/3
adventure-doctor --repair-log    # plug old turn-log gaps with markers
adventure-doctor --db other.db   # inspect a different save
```

Useful engine commands (each prints one JSON object):

```bash
adventure-game state             # the full current situation
adventure-game export-world      # shareable campaign seed — others can
                                 # `init` it to play your campaign fresh
adventure-game reset             # restart the current campaign from turn one
```

Both are thin wrappers around `scripts/game.py` and `scripts/doctor.py`;
run those directly (from this repo) if you skipped the launchers.

## Project layout

| Path | Role |
|---|---|
| `scripts/game.py` | CLI entry point — the JSON contract the agent drives |
| `scripts/engine.py` | rules: movement, generation, refereeing limits, win/auto-clear logic |
| `scripts/database.py` | SQLite schema and row-level helpers |
| `scripts/models.py` | Pydantic schemas for everything the agent proposes |
| `scripts/config.py` | DB path and the built-in default campaign |
| `scripts/doctor.py` | save inspector / consistency checker (human-facing) |
| `data/` | the save file — runtime state, gitignored, auto-created |
| `install.sh` | symlinks the skill and installs the PATH launchers |
| `SKILL.md` | the Game Host contract the agent runs on |
| `campaigns/` | predefined worlds (`README.md` there explains writing your own) |
| `references/` | deeper agent-facing guidance |
| `PLAN.md`, `TECHNICAL_DETAILS.md` | design history and full specification |
