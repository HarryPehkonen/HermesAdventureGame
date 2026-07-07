"""Repo-root conftest: puts the flat top-level modules (config, database,
engine, models, game) on sys.path for `tests/` and provides shared fixtures.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest

import database
import engine


@pytest.fixture
def conn(tmp_path):
    """A connection with the schema created but nothing seeded."""
    db_path = str(tmp_path / "test.db")
    c = database.get_connection(db_path)
    database.init_schema(c)
    yield c
    c.close()


@pytest.fixture
def game_conn(conn):
    """A connection with a freshly seeded game — player standing in the
    starting room."""
    engine.init_game(conn)
    return conn
