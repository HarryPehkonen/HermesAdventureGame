## Phase 1: Database Initialization & Schema

To solve the looping/spatial map alignment issue, the database uses a unique constraint on an `(X, Y, Z)` coordinate grid. Before generating a new room, the engine attempts to fetch an existing node at those coordinates.

### SQLite Schema Script (`init_db.py`)

```python
import sqlite3
import json

def initialize_database(db_path="hermes_game.db"):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Enable foreign keys
    cursor.execute("PRAGMA foreign_keys = ON;")

    # 1. Game Meta Configuration
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS game_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            zone_name TEXT NOT NULL,
            zone_description TEXT NOT NULL,
            global_theme_rules TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    ''')

    # 2. Map Nodes (Rooms) with Unique 3D Spatial Coordinates
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            x INTEGER NOT NULL,
            y INTEGER NOT NULL,
            z INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            is_explored INTEGER DEFAULT 0,
            UNIQUE(x, y, z)
        );
    ''')

    # 3. Spatial Edges (Connections between rooms)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS edges (
            from_node_id INTEGER NOT NULL,
            to_node_id INTEGER NOT NULL,
            direction TEXT NOT NULL, -- 'north', 'south', 'east', 'west', 'up', 'down'
            is_locked INTEGER DEFAULT 0,
            lock_condition TEXT, -- Explains what clears the obstacle
            PRIMARY KEY (from_node_id, direction),
            FOREIGN KEY (from_node_id) REFERENCES nodes(id) ON DELETE CASCADE,
            FOREIGN KEY (to_node_id) REFERENCES nodes(id) ON DELETE CASCADE
        );
    ''')

    # 4. Entities (Items, Obstacles, NPCs)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id INTEGER, -- NULL means it's in player inventory or destroyed
            name TEXT NOT NULL,
            type TEXT NOT NULL, -- 'item', 'obstacle', 'npc'
            description TEXT NOT NULL,
            properties_json TEXT NOT NULL, -- State, inventory, or solution conditions
            FOREIGN KEY (node_id) REFERENCES nodes(id) ON DELETE SET NULL
        );
    ''')

    # 5. Player State Tracker
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS player_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            current_node_id INTEGER,
            hp INTEGER DEFAULT 100,
            max_hp INTEGER DEFAULT 100,
            state_flags_json TEXT NOT NULL, -- Event trackers, puzzle completions
            FOREIGN KEY (current_node_id) REFERENCES nodes(id)
        );
    ''')

    conn.commit()
    conn.close()
    print("Hermes Game Database initialized successfully.")

if __name__ == "__main__":
    initialize_database()

```

---

## Phase 2: Data & State Examples

### Character Profile (Player State JSON)

Stored in `player_state.state_flags_json` to track continuous world triggers:

```json
{
  "discovered_clockwork_password": true,
  "is_poisoned": false,
  "factions": {
    "scrappers": 10,
    "guardians": -5
  }
}

```

### Entity Examples (`entities.properties_json`)

* **An Item (Crowbar):**
```json
{
  "can_pickup": true,
  "damage_modifier": 2,
  "traits": ["heavy", "metallic", "lever"]
}

```


* **An Obstacle (Rusted Hydraulic Gate):**
```json
{
  "can_pickup": false,
  "is_blocking": true,
  "solution_condition": "Requires high leverage to pry open, or a high-voltage charge to override the mechanical lock.",
  "is_cleared": false
}

```


* **An NPC (Construct Repair Unit):**
```json
{
  "can_pickup": false,
  "status": "neutral",
  "dialogue_tree_node": "root",
  "wants_item": "copper_wiring",
  "rewards_item_id": 4
}

```



---

## Phase 3: The Just-In-Time Generation Logic

Follow this sequence for player movement:

```
[Player Types 'Go North']
           │
           ▼
[Calculate Target Coordinates: X, Y+1, Z]
           │
           ├──► [Exists in Database?] ──(Yes)──► [Move Player & Narrate]
           │
           └──(No)
               │
               ▼
   [Query Zone Config & Current Room Context]
               │
               ▼
   [Execute LLM JIT Prompt (Structured JSON)]
               │
               ▼
   [Validate Schema & Run DB Transaction]
               │
               ▼
   [Write New Node, Edges, and Entities]
               │
               ▼
   [Output Narrative to Player]

```

### Standard Coordinate Offsets

* **North:** `(X, Y+1, Z)` | **South:** `(X, Y-1, Z)`
* **East:** `(X+1, Y, Z)` | **West:** `(X-1, Y, Z)`
* **Up:** `(X, Y, Z+1)` | **Down:** `(X, Y, Z-1)`

---

## Phase 4: Prompt Specs

To ensure strict JSON formatting without overhead, use the following prompt specs inside the generation modules.

### Prompt 1: Room Generation (`world_builder.py`)

```text
SYSTEM: You are the procedural world-generation submodule of Hermes Agent. You output strictly valid JSON conforming to the schema provided. No markdown wrapping. No conversational text.

USER:
Macro Zone Theme: Mechanical Spire - an ancient, vertical labyrinth of clockwork gears and leaking steam.
Current Room: Rust-Chamber 4 (Steam hisses from wall pipes; floor is steel grating).
Player Moving: Up (Z + 1)
Adjacent Connected Spaces: None.

Generate the next sequential room. Provide 1 unique interactive entity.
JSON Output Format:
{
  "room_name": "String",
  "description": "String (2-3 sentences descriptive, immersive prose)",
  "entity": {
    "name": "String",
    "type": "item|obstacle|npc",
    "description": "String",
    "properties": {}
  }
}

```

### Prompt 2: Action Judge (`action_referee.py`)

```text
SYSTEM: You are the physics and logic referee for the Hermes Agent text game. You evaluate if a player's proposed action succeeds based on their current inventory, environment entities, and logical feasibility.

USER:
Environment: A heavy rusted iron hatch blocks the exit leading Up.
Hatch Property Condition: "Requires high leverage to pry open."
Player Action: "I use the heavy steel crowbar from my pack to wedge into the rim of the hatch and push down with all my weight."
Player Inventory: ["heavy_steel_crowbar", "faded_notebook", "small_flashlight"]

Evaluate the action.
JSON Output Format:
{
  "success": true,
  "narrative_outcome": "With a screech of scraping metal, the rusted latch shears off as you throw your weight onto the crowbar. The hatch swings open upward, venting a blast of hot air.",
  "state_changes": {
    "clear_obstacle": true,
    "damage_to_player": 0,
    "inventory_removed": []
  }
}

```

---

## Phase 5: Implementation Roadmap

1. Create `database.py` containing the schema setup script and basic state CRUD operations (Create, Read, Update, Delete).
2. Create `engine.py` to handle the coordinate calculations and movement checking logic.
3. Create `llm_client.py` using structured outputs to call your model for room generation and action evaluation.
4. Create `game_loop.py` to bind user inputs, execute database updates via the referee output, and stream back text.
