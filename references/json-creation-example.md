# JSON Creation Example for Adventure Game Host

When creating rooms in the adventure game, you need to pipe valid JSON to the game.py create-room command. Here's a proven approach that avoids bash quoting issues:

## Method: Temporary File Approach (Recommended)

1. Create the JSON object in Python or using a JSON tool
2. Write it to a temporary file
3. Pipe the file contents to game.py

```python
import json
import tempfile
import subprocess
import os

# Create your room data
room_data = {
    "room_name": "Firebox Access Chamber",
    "description": "Your room description here...",
    "exits": ["up"],
    "entities": [
        {
            "name": "example item",
            "type": "item",
            "description": "Item description",
            "can_pickup": True,
            "is_blocking": False,
            "traits": ["example"]
        }
    ]
}

# Write to temporary file
with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
    json.dump(room_data, f)
    temp_file = f.name

# Pipe to game.py
bash_command = f"cat {temp_file} | python3 game.py create-room down"
result = subprocess.run(bash_command, shell=True, capture_output=True, text=True)

# Clean up
os.unlink(temp_file)
```

## Bash Alternative (for simple JSON)

For very simple JSON objects, you can use bash with careful escaping:

```bash
# Simple object - use single quotes and escape internal double quotes
echo '{"room_name":"Simple Room","description":"A basic room.","exits":["north"],"entities":[]}' | python3 game.py create-room north
```

## Common Pitfalls

- **Unescaped quotes**: Internal double quotes must be escaped as \\\"
- **Newlines in description**: JSON strings cannot contain unescaped newlines
- **Trailing commas**: JSON does not allow trailing commas in arrays or objects
- **UTF-8 encoding**: Ensure proper encoding for special characters

The temporary file method is preferred for complex room descriptions with multiple entities as it avoids bash quoting complexities entirely.