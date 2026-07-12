"""Tests for doctor.py — the save inspector / consistency checker."""

import database
import doctor
import engine
from models import GeneratedEntity, RoomGeneration


def _generate_room(conn, direction, name="Gear Gallery", entities=None, exits=None):
    result = engine.move(conn, direction)
    assert result.get("needs_generation"), result
    room = RoomGeneration(
        room_name=name,
        description="A test room.",
        exits=exits if exits is not None else [],
        entities=entities or [],
    )
    result = engine.create_room(conn, direction, room)
    assert result["ok"], result
    return result["room"]["id"]


def _log_here(conn, text="do something"):
    engine.log_turn(conn, text, "Something happens.")


def _messages(findings):
    return [f.message for f in findings]


def _kinds(findings):
    return {f.kind for f in findings}


class TestChecks:
    def test_unseeded_db_fails_with_reason(self, conn):
        findings = doctor.run_checks(conn)
        assert len(findings) == 1
        assert findings[0].kind == "integrity"
        assert "not seeded" in findings[0].message

    def test_fresh_game_passes(self, game_conn):
        assert doctor.run_checks(game_conn) == []

    def test_fully_logged_game_passes(self, game_conn):
        _log_here(game_conn, "look around")
        _generate_room(game_conn, "north")
        _log_here(game_conn, "go north")
        assert doctor.run_checks(game_conn) == []

    def test_empty_log_with_multiple_rooms(self, game_conn):
        _generate_room(game_conn, "north")
        findings = doctor.run_checks(game_conn)
        assert any("turn_log is empty but 2 rooms exist" in m for m in _messages(findings))
        assert _kinds(findings) == {"history"}

    def test_unlogged_room_and_stale_position(self, game_conn):
        _log_here(game_conn, "look around")  # start room is logged
        _generate_room(game_conn, "north")  # new room never logged
        findings = doctor.run_checks(game_conn)
        assert any("unlogged room" in m for m in _messages(findings))
        assert any("stale log position" in m for m in _messages(findings))
        assert _kinds(findings) == {"history"}

    def test_one_way_edge_detected(self, game_conn):
        _log_here(game_conn)
        node_id = _generate_room(game_conn, "north")
        _log_here(game_conn)
        game_conn.execute(
            "DELETE FROM edges WHERE from_node_id = ? AND direction = 'south';",
            (node_id,),
        )
        findings = doctor.run_checks(game_conn)
        assert any("one-way passage" in m for m in _messages(findings))
        assert _kinds(findings) == {"integrity"}

    def test_destroyed_blocker_still_locking(self, game_conn):
        _log_here(game_conn)
        gate = GeneratedEntity(
            name="rusted gate",
            type="obstacle",
            description="A heavy gate.",
            can_pickup=False,
            is_blocking=True,
            solution_condition="pry it open",
            blocks_direction="north",
        )
        _generate_room(game_conn, "north", entities=[gate], exits=["north"])
        _log_here(game_conn)
        assert doctor.run_checks(game_conn) == []
        # Corrupt the save: destroy the gate without unlocking its edge
        # (engine.apply_changes would normally unlock — bypass it).
        game_conn.execute(
            "UPDATE entities SET holder = 'gone', node_id = NULL WHERE name = 'rusted gate';"
        )
        findings = doctor.run_checks(game_conn)
        assert any("which was destroyed" in m for m in _messages(findings))
        assert _kinds(findings) == {"integrity"}

    def test_orphaned_room_entity(self, game_conn):
        _log_here(game_conn)
        valve = GeneratedEntity(
            name="corroded valve",
            type="item",
            description="A valve.",
            can_pickup=True,
            is_blocking=False,
        )
        _generate_room(game_conn, "north", entities=[valve])
        _log_here(game_conn)
        game_conn.execute(
            "UPDATE entities SET node_id = NULL WHERE name = 'corroded valve';"
        )
        findings = doctor.run_checks(game_conn)
        assert any("orphaned" in m for m in _messages(findings))

    def test_hp_out_of_range(self, game_conn):
        _log_here(game_conn)
        database.set_player_hp(game_conn, 150)
        findings = doctor.run_checks(game_conn)
        assert any("hp out of range" in m for m in _messages(findings))


class TestRepair:
    def test_fresh_game_needs_no_repair(self, game_conn):
        assert doctor.repair_history_gaps(game_conn) == []

    def test_repair_clears_history_gaps(self, game_conn):
        _log_here(game_conn)
        _generate_room(game_conn, "north")  # unlogged; player now here
        assert _kinds(doctor.run_checks(game_conn)) == {"history"}
        actions = doctor.repair_history_gaps(game_conn)
        assert len(actions) == 1
        assert "gap marker" in actions[0]
        assert doctor.run_checks(game_conn) == []
        markers = game_conn.execute(
            "SELECT * FROM turn_log WHERE player_input = ?;", (doctor.GAP_INPUT,)
        ).fetchall()
        assert len(markers) == 1

    def test_repair_stale_position_only(self, game_conn):
        _log_here(game_conn)
        _generate_room(game_conn, "north")
        _log_here(game_conn)
        # Walk back to the (already-logged) start room without logging.
        result = engine.move(game_conn, "south")
        assert result["ok"], result
        findings = doctor.run_checks(game_conn)
        assert len(findings) == 1
        assert "stale log position" in findings[0].message
        actions = doctor.repair_history_gaps(game_conn)
        assert actions == ["gap marker for unlogged moves since the last logged turn"]
        assert doctor.run_checks(game_conn) == []

    def test_repair_is_idempotent(self, game_conn):
        _log_here(game_conn)
        _generate_room(game_conn, "north")
        assert doctor.repair_history_gaps(game_conn) != []
        assert doctor.repair_history_gaps(game_conn) == []

    def test_repair_does_not_touch_integrity(self, game_conn):
        _log_here(game_conn)
        database.set_player_hp(game_conn, 150)
        assert doctor.repair_history_gaps(game_conn) == []
        findings = doctor.run_checks(game_conn)
        assert _kinds(findings) == {"integrity"}

    def test_new_gap_after_repair_fails_again(self, game_conn):
        _log_here(game_conn)
        _generate_room(game_conn, "north", exits=["north"])
        doctor.repair_history_gaps(game_conn)
        assert doctor.run_checks(game_conn) == []
        _generate_room(game_conn, "north", name="Steam Shaft")  # new unlogged room
        assert _kinds(doctor.run_checks(game_conn)) == {"history"}


class TestCli:
    def _seeded_db(self, tmp_path):
        db_path = str(tmp_path / "doctor_test.db")
        conn = database.get_connection(db_path)
        database.init_schema(conn)
        engine.init_game(conn)
        return db_path, conn

    def test_missing_db_exits_2(self, tmp_path, capsys):
        assert doctor.main(["--db", str(tmp_path / "nope.db")]) == 2
        assert "not found" in capsys.readouterr().err

    def test_clean_db_exits_0(self, tmp_path, capsys):
        db_path, conn = self._seeded_db(tmp_path)
        conn.close()
        assert doctor.main(["--db", db_path, "--check-only"]) == 0
        assert "all checks passed" in capsys.readouterr().out

    def test_history_gaps_exit_3_with_reason(self, tmp_path, capsys):
        db_path, conn = self._seeded_db(tmp_path)
        engine.move(conn, "north")
        engine.create_room(
            conn,
            "north",
            RoomGeneration(room_name="X", description="A room.", exits=[]),
        )
        conn.close()
        assert doctor.main(["--db", db_path, "--check-only"]) == 3
        out = capsys.readouterr().out
        assert "check(s) failed" in out
        assert "[history]" in out
        assert "turn_log is empty" in out

    def test_integrity_failure_exits_1(self, tmp_path, capsys):
        db_path, conn = self._seeded_db(tmp_path)
        engine.log_turn(conn, "look", "You look around.")
        database.set_player_hp(conn, 150)
        conn.commit()
        conn.close()
        assert doctor.main(["--db", db_path, "--check-only"]) == 1
        assert "[integrity]" in capsys.readouterr().out

    def test_repair_log_flow(self, tmp_path, capsys):
        db_path, conn = self._seeded_db(tmp_path)
        engine.move(conn, "north")
        engine.create_room(
            conn,
            "north",
            RoomGeneration(room_name="X", description="A room.", exits=[]),
        )
        conn.close()
        assert doctor.main(["--db", db_path, "--check-only"]) == 3
        capsys.readouterr()
        assert doctor.main(["--db", db_path, "--repair-log"]) == 0
        out = capsys.readouterr().out
        assert "gap marker" in out
        assert "all checks passed" in out
        # A second repair has nothing to do and the save stays clean.
        assert doctor.main(["--db", db_path, "--repair-log"]) == 0
        assert "no history gaps to repair" in capsys.readouterr().out

    def test_summary_and_verbose_output(self, tmp_path, capsys):
        db_path, conn = self._seeded_db(tmp_path)
        engine.log_turn(conn, "look", "You look around.")
        conn.close()
        assert doctor.main(["--db", db_path]) == 0
        out = capsys.readouterr().out
        assert "Player:" in out
        assert "Turn history" not in out  # only with -v

        assert doctor.main(["--db", db_path, "-v"]) == 0
        out = capsys.readouterr().out
        assert "--- Rooms ---" in out
        assert "--- Turn history (1 turns) ---" in out
        assert "You look around." in out
