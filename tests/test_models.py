"""Pydantic-layer validation: TECHNICAL_DETAILS.md §2 / §3.3."""

import pydantic
import pytest

from models import GeneratedEntity, RoomGeneration


def _obstacle(**overrides):
    defaults = dict(
        name="Rusted Hatch",
        type="obstacle",
        description="A heavy rusted hatch.",
        can_pickup=False,
        is_blocking=True,
        solution_condition="Requires a crowbar.",
        blocks_direction="north",
    )
    defaults.update(overrides)
    return GeneratedEntity(**defaults)


def test_valid_obstacle_with_blocks_direction():
    entity = _obstacle()
    assert entity.blocks_direction == "north"


def test_blocks_direction_requires_obstacle_type():
    with pytest.raises(pydantic.ValidationError):
        _obstacle(type="item", can_pickup=True)


def test_blocks_direction_requires_is_blocking_true():
    with pytest.raises(pydantic.ValidationError):
        _obstacle(is_blocking=False)


def test_room_generation_rejects_duplicate_exits():
    with pytest.raises(pydantic.ValidationError):
        RoomGeneration(room_name="X", description="Y", exits=["north", "north"])


def test_room_generation_accepts_blocks_direction_not_in_exits():
    """blocks_direction ∈ exits is deliberately NOT model-level validation:
    the engine appends the implicit return direction first, then checks
    (engine.create_room). Blocking the unlisted way back must stay legal."""
    room = RoomGeneration(
        room_name="X",
        description="Y",
        exits=["east"],
        entities=[_obstacle(blocks_direction="north")],
    )
    assert room.entities[0].blocks_direction == "north"


def test_room_generation_accepts_blocks_direction_in_exits():
    room = RoomGeneration(
        room_name="X",
        description="Y",
        exits=["north", "east"],
        entities=[_obstacle(blocks_direction="north")],
    )
    assert room.entities[0].blocks_direction == "north"


def test_extra_fields_forbidden():
    with pytest.raises(pydantic.ValidationError):
        RoomGeneration(room_name="X", description="Y", exits=[], unexpected_field=True)
