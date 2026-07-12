# Predefined Campaigns

Each file is a complete `WorldInit` payload (see TECHNICAL_DETAILS.md §2).
Start one:

```bash
python game.py init  < campaigns/derelict_starship.json   # on an empty save
python game.py reset < campaigns/derelict_starship.json   # replace the current campaign
```

**`reset` wipes the current world.** Run `python game.py export-world` first
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
python -c "import json, models; models.WorldInit.model_validate(json.load(open('campaigns/FILE.json'))); print('ok')"
```

(`tests/test_campaigns.py` does the same for every file here, plus seeds
each one into a scratch database.)
