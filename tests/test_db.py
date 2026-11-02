"""SQLite persistence, migrations, transactions, and restart behavior."""

from __future__ import annotations

import io
import sqlite3
import tempfile
import unittest
import unittest.mock
from datetime import timedelta
from pathlib import Path

from helpers import (
    A, COCONUT, E, NS1T, NS256, T0, WATERSIDE, Clock, m, outcome, quiet_logs, results, store_status,
)
from inventory_db import LATEST_VERSION, MIGRATIONS, HistoryDB, PollRecord, _statements
from inventory_model import from_utc_text

S = timedelta(seconds=20)


def setUpModule():
    quiet_logs()


def make_monitor(db, clock, run_id=None, interval=20.0):
    mon = m.Monitor(interval, timedelta(minutes=10), db=db, run_id=run_id, wall=clock,
                    out=io.StringIO(), color=False, notifier=lambda *a, **k: None)
    return mon


def feed(mon, clock, res, errors=()):
    mon.process(outcome(res, errors), clock(), 0.5)
    clock.advance(seconds=20)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "h.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def test_fresh_database_is_latest(self):
        db = HistoryDB(self.path)
        self.assertEqual(db.version, LATEST_VERSION)
        tables = {r[0] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("polls", "sku_observations", "availability_events", "poll_store_results",
                  "store_restock_waves", "restock_wave_members", "monitor_runs"):
            self.assertIn(t, tables)
        db.close()

    def test_v1_to_latest_preserves_history_and_backs_up(self):
        conn = sqlite3.connect(self.path)
        for stmt in _statements(MIGRATIONS[1]):
            conn.execute(stmt)
        conn.execute("PRAGMA user_version = 1")
        conn.execute("INSERT INTO polls (poll_id, started_utc, finished_utc, duration_ms, requests,"
                     " error_count, available_count) VALUES (1, '2026-10-01T13:00:00.000+00:00',"
                     " '2026-10-01T13:00:01.000+00:00', 1000, 2, 0, 1)")
        conn.execute("INSERT INTO poll_store_results VALUES (1, 'R169', 'ok')")
        conn.execute("INSERT INTO sku_observations VALUES (1, 'R169', 'MK1L4LL/A', 'available')")
        conn.execute("INSERT INTO availability_events (poll_id, store_id, part, event_type, observed_utc,"
                     " timing) VALUES (1, 'R169', 'MK1L4LL/A', 'became_available',"
                     " '2026-10-01T13:00:00.000+00:00', 'exact')")
        conn.commit()
        conn.close()

        db = HistoryDB(self.path)
        self.assertEqual(db.version, LATEST_VERSION)
        self.assertEqual(db.query("SELECT COUNT(*) FROM polls")[0][0], 1)
        self.assertEqual(db.query("SELECT COUNT(*) FROM sku_observations")[0][0], 1)
        self.assertEqual(db.query("SELECT COUNT(*) FROM availability_events")[0][0], 1)
        self.assertIsNone(db.query("SELECT run_id FROM polls")[0][0])
        backups = list(Path(self.tmp.name).glob("h.sqlite3.v1-backup-*"))
        self.assertEqual(len(backups), 1)
        b = sqlite3.connect(backups[0])
        self.assertEqual(b.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual(b.execute("SELECT COUNT(*) FROM polls").fetchone()[0], 1)
        b.close()
        # Migrated history still seeds state.
        self.assertEqual(db.load_prior_state()[(WATERSIDE, NS1T)].status, A)
        db.close()

    def test_reopen_is_idempotent(self):
        HistoryDB(self.path).close()
        db = HistoryDB(self.path)
        self.assertEqual(db.version, LATEST_VERSION)
        self.assertEqual(list(Path(self.tmp.name).glob("*backup*")), [])
        db.close()

    def test_newer_schema_refused(self):
        conn = sqlite3.connect(self.path)
        conn.execute(f"PRAGMA user_version = {LATEST_VERSION + 1}")
        conn.close()
        with self.assertRaises(RuntimeError):
            HistoryDB(self.path)

    def test_failed_migration_rolls_back(self):
        conn = sqlite3.connect(self.path)
        for stmt in _statements(MIGRATIONS[1]):
            conn.execute(stmt)
        conn.execute("CREATE TABLE monitor_runs (x)")  # collides with v2
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()
        with self.assertRaises(sqlite3.OperationalError):
            HistoryDB(self.path)
        conn = sqlite3.connect(self.path)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(polls)")]
        self.assertNotIn("run_id", cols)
        conn.close()


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.db = HistoryDB(":memory:")
        self.clock = Clock()
        self.run = self.db.start_run(T0, 20.0, 600.0)
        self.mon = make_monitor(self.db, self.clock, self.run)

    def tearDown(self):
        self.db.close()

    def count(self, table, where="1=1", params=()):
        return self.db.query(f"SELECT COUNT(*) FROM {table} WHERE {where}", params)[0][0]

    def test_compact_observation_encoding(self):
        feed(self.mon, self.clock, results([(WATERSIDE, NS1T)]))
        self.assertEqual(self.count("polls"), 1)
        self.assertEqual(self.count("poll_store_results"), 18)
        self.assertEqual(self.count("sku_observations"), 1)
        row = self.db.query("SELECT * FROM polls")[0]
        self.assertEqual((row["available_count"], row["run_id"]), (1, self.run))

    def test_error_store_writes_no_sku_rows(self):
        feed(self.mon, self.clock, store_status(results(), WATERSIDE, E), [m.FetchError("network", "x")])
        self.assertEqual(self.db.query("SELECT outcome FROM poll_store_results WHERE store_id = 'R169'")[0][0],
                         "error")
        self.assertEqual(self.count("sku_observations"), 0)
        self.assertEqual(self.db.query("SELECT error_summary FROM polls")[0][0], "network")

    def test_events_and_waves_persisted(self):
        feed(self.mon, self.clock, results())
        feed(self.mon, self.clock, results([(WATERSIDE, NS1T)]))
        feed(self.mon, self.clock, results([(WATERSIDE, NS1T), (WATERSIDE, NS256)]))
        self.assertEqual(self.count("availability_events", "event_type = 'became_available'"), 2)
        waves = self.db.query("SELECT * FROM store_restock_waves")
        self.assertEqual(len(waves), 1)
        w = waves[0]
        self.assertEqual((w["store_id"], w["sku_count"], w["supporting_polls"], w["event_class"],
                          w["confidence"]), (WATERSIDE, 2, 2, "new_availability", "complete"))
        members = self.db.query("SELECT * FROM restock_wave_members ORDER BY first_available_utc")
        self.assertEqual([r["part"] for r in members], [NS1T, NS256])
        self.assertEqual(members[0]["color"], "Night Sky")
        self.assertEqual(members[0]["capacity"], "1TB")
        self.assertEqual(from_utc_text(members[0]["last_unavailable_utc"]), T0)

    def test_member_duration_when_observable(self):
        feed(self.mon, self.clock, results())
        feed(self.mon, self.clock, results([(WATERSIDE, NS1T)]))
        feed(self.mon, self.clock, results([(WATERSIDE, NS1T)]))
        feed(self.mon, self.clock, results())
        r = self.db.query("SELECT * FROM restock_wave_members")[0]
        self.assertEqual(r["duration_s"], 40.0)
        self.assertIsNotNone(r["ended_utc"])
        self.assertEqual(self.count("availability_events", "event_type = 'became_unavailable'"), 1)

    def test_duration_unknown_after_gap(self):
        feed(self.mon, self.clock, results())
        feed(self.mon, self.clock, results([(WATERSIDE, NS1T)]))
        feed(self.mon, self.clock, store_status(results(), WATERSIDE, E))
        feed(self.mon, self.clock, results())
        r = self.db.query("SELECT * FROM restock_wave_members")[0]
        self.assertIsNone(r["duration_s"])
        self.assertIsNotNone(r["ended_utc"])

    def test_duplicate_poll_rejected_atomically(self):
        rec = PollRecord(T0, T0 + S, 2, [], results(), {sid: "ok" for sid in m.STORES})
        self.db.record_poll(self.run, rec)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.record_poll(self.run, rec)
        self.assertEqual(self.count("polls"), 1)
        self.assertEqual(self.count("poll_store_results"), 18)

    def test_failure_mid_write_leaves_nothing(self):
        bad = {sid: "ok" for sid in m.STORES}
        bad[WATERSIDE] = "bogus"  # violates CHECK after polls row inserted
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.record_poll(self.run, PollRecord(T0, T0 + S, 2, [], results(), bad))
        self.assertEqual(self.count("polls"), 0)

    def test_db_failure_does_not_stop_monitoring(self):
        with unittest.mock.patch.object(self.db, "record_poll", side_effect=sqlite3.OperationalError("disk")):
            feed(self.mon, self.clock, results([(WATERSIDE, NS1T)]))
        self.assertEqual(self.mon.poll_no, 1)

    def test_event_unique_constraint(self):
        feed(self.mon, self.clock, results([(WATERSIDE, NS1T)]))
        row = self.db.query("SELECT * FROM availability_events")[0]
        self.db.conn.execute(
            "INSERT OR IGNORE INTO availability_events (store_id, part, event_type, observed_utc, timing)"
            " VALUES (?, ?, ?, ?, ?)", (row["store_id"], row["part"], row["event_type"], row["observed_utc"], "x"))
        self.assertEqual(self.count("availability_events"), 1)


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "h.sqlite3"
        self.clock = Clock()

    def tearDown(self):
        self.tmp.cleanup()

    def run_session(self, seq, gap_before=timedelta(0)):
        self.clock.advance(seconds=gap_before.total_seconds())
        db = HistoryDB(self.path)
        run = db.start_run(self.clock(), 20.0, 600.0)
        mon = make_monitor(db, self.clock, run)
        mon.seed_from_history()
        for r in seq:
            feed(mon, self.clock, r)
        db.end_run(run, self.clock(), mon.waves.close_all())
        db.close()

    def waves(self):
        db = HistoryDB(self.path)
        rows = db.query("SELECT * FROM store_restock_waves ORDER BY wave_id")
        db.close()
        return rows

    def test_first_ever_run_marks_startup(self):
        self.run_session([results([(WATERSIDE, NS1T)])])
        (w,) = self.waves()
        self.assertEqual((w["event_class"], w["present_at_startup"], w["confidence"]),
                         ("present_at_startup", 1, "uncertain"))

    def test_still_available_after_restart_no_new_wave(self):
        self.run_session([results(), results([(WATERSIDE, NS1T)])])
        self.run_session([results([(WATERSIDE, NS1T)])] * 3, gap_before=timedelta(hours=1))
        self.assertEqual(len(self.waves()), 1)

    def test_appeared_during_downtime_is_uncertain(self):
        self.run_session([results(), results()])
        self.run_session([results([(WATERSIDE, NS1T)])], gap_before=timedelta(hours=2))
        (w,) = self.waves()
        self.assertEqual((w["event_class"], w["gap_affected"], w["present_at_startup"]),
                         ("uncertain_timing", 1, 1))
        db = HistoryDB(self.path)
        mem = db.query("SELECT * FROM restock_wave_members")[0]
        self.assertEqual(mem["timing"], "after_gap")
        self.assertIsNotNone(mem["last_unavailable_utc"])
        db.close()

    def test_new_run_after_restart_detects_transition(self):
        self.run_session([results(), results([(WATERSIDE, NS1T)])])
        self.run_session([results([(WATERSIDE, NS1T)]), results(), results([(WATERSIDE, NS1T)])],
                         gap_before=timedelta(minutes=30))
        rows = self.waves()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["event_class"], "new_availability")

    def test_waves_closed_and_stale_open_waves_closed_on_start(self):
        self.run_session([results(), results([(COCONUT, NS1T)])])
        self.assertTrue(all(r["closed"] for r in self.waves()))
        db = HistoryDB(self.path)
        db.conn.execute("UPDATE store_restock_waves SET closed = 0")  # simulate crash
        db.start_run(self.clock(), 20.0, 600.0)
        self.assertEqual(db.query("SELECT COUNT(*) FROM store_restock_waves WHERE closed = 0")[0][0], 0)
        db.close()

    def test_run_rows(self):
        self.run_session([results()])
        db = HistoryDB(self.path)
        (run,) = db.query("SELECT * FROM monitor_runs")
        self.assertIsNotNone(run["ended_utc"])
        self.assertEqual(run["wave_window_s"], 600.0)
        db.close()


if __name__ == "__main__":
    unittest.main()
