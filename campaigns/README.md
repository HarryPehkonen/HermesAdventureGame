# Predefined Campaigns

Each file is a complete `WorldInit` payload (see TECHNICAL_DETAILS.md §2).
Start one:

```bash
adventure-game init  --campaign derelict_starship   # on an empty save
adventure-game reset --campaign derelict_starship   # replace the current campaign
```

`--campaign` takes the filename without `.json` and resolves it here in the
skill directory, so it works from any cwd. Piping a file still works too
(`adventure-game reset < some_world.json`) — that's how you start a campaign
that doesn't live in this directory.

**`reset` wipes the current world.** Run `adventure-game export-world` first
if you want to keep what you're playing — the exported JSON is itself a
campaign file you can drop in this directory.

| File | Zone | Goal (`win_flag`) |
|---|---|---|
| `derelict_starship.json` | DSV Erebus | `reactor_online` — restart the dead freighter's reactor |
| `pirate_islands.json` | The Scattered Teeth | `found_blackbeards_gold` — sail island to island to the hoard |
| `clockwork_spire.json` | The Mechanical Spire | `great_clock_wound` — climb the spire, wake the Great Clock |
| `drowned_archive.json` | The Drowned Archive | `founders_folio_recovered` — drain the library, reach the deep vault |
| `blackthorn_manor.json` | Blackthorn Manor | `curse_lifted` — name the wrong and set it right |
| `amber_tomb.json` | Tomb of the Amber Pharaoh | `scarab_brought_to_daylight` — take the scarab and get back out |

## Writing your own

Only the starting room and starting inventory are handcrafted; every other
room is generated during play, steered entirely by `global_theme_rules`.
That string is the campaign's real design surface — it should cover:

- **What a room means** (a ship compartment; a league of open sea vs. one
  area of an island) and what the six directions represent. The engine's
  grid never changes; the *scale* of a step is pure narration.
- **Tone and period**, plus what must never appear.
- **Environmental style**: hostile conditions are obstacle entities, never
  baked into descriptions, ideally with `cleared_by_flag` naming suggested
  flags so one fix opens distant passages.
- **The arc toward the win**: intermediate flag names the generator should
  gravitate to, and what finally sets the `win_flag`.

Validate a new file with:

```bash
PYTHONPATH=scripts python -c "import json, models; models.WorldInit.model_validate(json.load(open('campaigns/FILE.json'))); print('ok')"
```

(`tests/test_campaigns.py` does the same for every file here, plus seeds
each one into a scratch database.)
