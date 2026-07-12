"""Pydantic schemas for the JSON proposals Hermes Agent sends to game.py.

These are the untrusted-input boundary described in TECHNICAL_DETAILS.md §0:
Hermes Agent proposes, the engine validates. Mirrors TECHNICAL_DETAILS.md §2,
amended per §3.3 with `blocks_direction`.

`extra="forbid"` on every model: an unexpected field almost always means the
agent misunderstood the contract, and failing loudly with a clear message is
more useful than silently ignoring it.
"""

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, model_validator

Direction = Literal["north", "south", "east", "west", "up", "down"]

DIRECTIONS: tuple[Direction, ...] = (
    "north",
    "south",
    "east",
    "west",
    "up",
    "down",
)

OPPOSITE_DIRECTION: dict[Direction, Direction] = {
    "north": "south",
    "south": "north",
    "east": "west",
    "west": "east",
    "up": "down",
    "down": "up",
}

# (dx, dy, dz) per TECHNICAL_DETAILS.md / PLAN.md "Standard Coordinate Offsets".
DIRECTION_OFFSETS: dict[Direction, tuple[int, int, int]] = {
    "north": (0, 1, 0),
    "south": (0, -1, 0),
    "east": (1, 0, 0),
    "west": (-1, 0, 0),
    "up": (0, 0, 1),
    "down": (0, 0, -1),
}


class GeneratedEntity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: Literal["item", "obstacle", "npc"]
    description: str
    can_pickup: bool
    is_blocking: bool
    solution_condition: Optional[str] = None  # obstacles only
    blocks_direction: Optional[Direction] = None  # obstacles only
    cleared_by_flag: Optional[str] = None  # obstacles only: flag that auto-clears it
    traits: list[str] = []

    @model_validator(mode="after")
    def _obstacle_only_fields(self) -> "GeneratedEntity":
        if self.blocks_direction is not None:
            if self.type != "obstacle":
                raise ValueError(
                    "blocks_direction may only be set on an entity with "
                    f"type 'obstacle' (got type={self.type!r})"
                )
            if not self.is_blocking:
                raise ValueError(
                    "blocks_direction requires is_blocking=true"
                )
        if self.cleared_by_flag is not None and self.type != "obstacle":
            raise ValueError(
                "cleared_by_flag may only be set on an entity with "
                f"type 'obstacle' (got type={self.type!r})"
            )
        return self


class RoomGeneration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    room_name: str
    description: str
    exits: list[Direction]
    entities: list[GeneratedEntity] = []

    @model_validator(mode="after")
    def _no_duplicate_exits(self) -> "RoomGeneration":
        if len(set(self.exits)) != len(self.exits):
            raise ValueError(f"duplicate direction in exits: {self.exits}")
        # blocks_direction ∈ exits is checked in engine.create_room, not here:
        # the CLI appends the implicit return direction after validation, and
        # an obstacle is allowed to block that return exit (a cave-in / trap
        # room) without the agent listing it.
        return self


class StateChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")

    obstacles_cleared_entity_ids: list[int] = []
    damage_to_player: int = 0
    healing_to_player: int = 0
    items_removed_from_inventory: list[int] = []
    items_added_to_inventory: list[int] = []
    entities_destroyed: list[int] = []
    flags_set: dict[str, bool] = {}


class WorldInit(BaseModel):
    """Seed payload for a new campaign: theme, starting room, and starting
    inventory, negotiated with the player in SKILL.md's Lobby Mode. Piped to
    `game.py init` (new game) or `game.py reset` (switch campaigns); persisted
    in game_config so `reset` without a payload replays the same campaign."""

    model_config = ConfigDict(extra="forbid")

    zone_name: str
    zone_description: str
    global_theme_rules: str
    starting_room: RoomGeneration
    starting_inventory: list[GeneratedEntity] = []
    win_flag: Optional[str] = None  # flag that completes the campaign; None = endless sandbox
    win_message: Optional[str] = None  # narrated on victory; paired with win_flag

    @model_validator(mode="after")
    def _win_condition_is_paired(self) -> "WorldInit":
        if (self.win_flag is None) != (self.win_message is None):
            raise ValueError(
                "win_flag and win_message must be set together (or both "
                "omitted for an endless sandbox campaign)"
            )
        if self.win_flag is not None and not self.win_flag.strip():
            raise ValueError("win_flag must be a non-empty flag name")
        return self

    @model_validator(mode="after")
    def _seed_is_playable(self) -> "WorldInit":
        if not self.starting_room.exits:
            raise ValueError(
                "starting_room must have at least one exit, or the game "
                "starts softlocked"
            )
        # Unlike create-room, the seed room has no implicit return direction,
        # so blocks_direction ∈ exits is checked here at the model level.
        exit_set = set(self.starting_room.exits)
        for entity in self.starting_room.entities:
            if (
                entity.blocks_direction is not None
                and entity.blocks_direction not in exit_set
            ):
                raise ValueError(
                    f"starting_room entity {entity.name!r} has "
                    f"blocks_direction={entity.blocks_direction!r}, which is "
                    f"not in the starting room's exits {self.starting_room.exits}"
                )
        for item in self.starting_inventory:
            if item.type != "item":
                raise ValueError(
                    f"starting_inventory entries must have type 'item' — "
                    f"{item.name!r} has type {item.type!r}"
                )
        return self


class TurnLogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    player_input: str
    narrative: str
