"""The self-play harness must pin the save file itself.

`tools/selfplay.py` hands the host model a tool that runs the game CLI. If a
model can smuggle its own `--db` through that tool, a throwaway run can be
redirected at a real save — argparse takes the *last* `--db`, so prepending the
harness's own is not enough. Only stdlib imports run at selfplay module scope,
so these tests need neither pydantic-ai nor an API key.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

import selfplay


def test_strip_db_flag_removes_every_spelling():
    assert selfplay._strip_db_flag(["--db", "/evil.db", "state"]) == ["state"]
    assert selfplay._strip_db_flag(["--db=/evil.db", "state"]) == ["state"]
    assert selfplay._strip_db_flag(["state", "--db", "/evil.db"]) == ["state"]
    # ...without eating legitimate arguments
    assert selfplay._strip_db_flag(["move", "north"]) == ["move", "north"]
    assert selfplay._strip_db_flag(["reset", "--campaign", "amber_tomb"]) == [
        "reset", "--campaign", "amber_tomb",
    ]


def test_hijacked_db_cannot_escape_the_harness_save(tmp_path):
    harness_db = str(tmp_path / "harness.db")
    evil_db = tmp_path / "evil.db"

    selfplay.run_cli(harness_db, ["reset", "--campaign", "clockwork_spire"])

    for attempt in (
        ["--db", str(evil_db), "state"],
        [f"--db={evil_db}", "state"],
        ["state", "--db", str(evil_db)],
    ):
        result = json.loads(selfplay.run_cli(harness_db, attempt))
        assert result["zone"]["zone_name"] == "The Mechanical Spire", attempt

    assert not evil_db.exists(), "a model-supplied --db created a save outside the harness"
