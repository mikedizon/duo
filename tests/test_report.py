"""Wave history, timing-pattern analysis, correlations, and CSV export."""

from __future__ import annotations

import csv
import io
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from helpers import COCONUT, NS1T, NS256, SW256, SW2T, WATERSIDE, m, outcome, quiet_logs, results
import inventory_report as rep
from inventory_db import HistoryDB
from inventory_model import LOCAL_TZ, STORES

# Mon 2026-10-26 .. Sun 2026-11-01 (DST ends Nov 1). Polls 08:00-11:59 local, every minute.
DAYS = [date(2026, 10, 26) + timedelta(days=i) for i in range(7)]
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=LOCAL_TZ)

# (day index, store, part) -> local HH:MM it becomes available; everything clears at 11:00.
SCENARIO = {
    (1, WATERSIDE, SW256): "09:42", (1, WATERSIDE, NS256): "09:42", (1, WATERSIDE, NS1T): "09:44",  # Tue
    (2, WATERSIDE, SW256): "09:38", (2, WATERSIDE, NS1T): "09:40",  # Wed
    (3, WATERSIDE, SW256): "09:45", (3, WATERSIDE, NS1T): "09:45",  # Thu
    (5, COCONUT, SW2T): "10:30",  # Sat
}


def setUpModule():
    quiet_logs()


def _at(d: date, hhmm: str) -> datetime:
    h, mi = map(int, hhmm.split(":"))
    return datetime(d.year, d.month, d.day, h, mi, tzinfo=LOCAL_TZ)


def build_history(db: HistoryDB) -> None:
    run = db.start_run(_at(DAYS[0], "08:00"), 60.0, 600.0)
    mon = m.Monitor(60.0, timedelta(minutes=10), db=db, run_id=run, out=io.StringIO(), color=False,
                    notifier=lambda *a, **k: None)
    for di, d in enumerate(DAYS):
        t = _at(d, "08:00")
        end = _at(d, "12:00")
        while t < end:
            local_hm = t.strftime("%H:%M")
            avail = [(sid, part) for (i, sid, part), start in SCENARIO.items()
                     if i == di and start <= local_hm < "11:00"]
            mon.process(outcome(results(avail)), t, 0.5)
            t += timedelta(minutes=1)
    db.end_run(run, _at(DAYS[-1], "12:00"), mon.waves.close_all())


class HistoryCase(unittest.TestCase):
    db: HistoryDB

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = Path(cls.tmp.name) / "h.sqlite3"
        cls.db = HistoryDB(cls.path)
        build_history(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        cls.tmp.cleanup()


class LoadTests(HistoryCase):
    def test_waves_loaded(self):
        waves = rep.load_waves(self.db)
        self.assertEqual([(w.store_id, len(w.members)) for w in waves],
                         [(WATERSIDE, 3), (WATERSIDE, 2), (WATERSIDE, 2), (COCONUT, 1)])
        self.assertTrue(all(w.event_class == "new_availability" for w in waves))
        self.assertTrue(all(w.confidence == "complete" for w in waves))
        tue = waves[0]
        self.assertEqual(rep.hhmm(rep.minute_of_day(tue.first_at)), "09:42")
        self.assertEqual(tue.first_poll_parts(), {SW256, NS256})
        self.assertTrue(all(mem.duration_s is not None for w in waves for mem in w.members))

    def test_coverage_across_dst(self):
        cov = rep.date_coverage(rep.load_coverage(self.db)[WATERSIDE])
        self.assertEqual(sorted(cov), DAYS)
        self.assertAlmostEqual(cov[DAYS[0]], 48 / 288)
        self.assertAlmostEqual(cov[DAYS[6]], 48 / 300)  # Nov 1 has 25 hours

    def test_window_covered(self):
        buckets = rep.load_coverage(self.db)[WATERSIDE]
        self.assertTrue(rep.window_covered(buckets, DAYS[4], 9 * 60 + 38, 9 * 60 + 45))
        self.assertFalse(rep.window_covered(buckets, DAYS[4], 14 * 60, 14 * 60 + 10))


class AnalysisTests(unittest.TestCase):
    def test_best_window(self):
        pts = [(DAYS[1], 582.0), (DAYS[2], 578.0), (DAYS[3], 585.0), (DAYS[4], 900.0)]
        c = rep.best_window(pts, 10)
        self.assertEqual((c.support, rep.hhmm(c.start_min), rep.hhmm(c.end_min)), (3, "09:38", "09:45"))
        self.assertEqual(c.spread_min, 7.0)
        self.assertIsNone(rep.best_window(pts, 2))  # nothing on 2+ dates within 2 min
        self.assertIsNone(rep.best_window([(DAYS[1], 582.0), (DAYS[1], 583.0)], 10))  # same date twice

    def test_assessment_thresholds(self):
        self.assertTrue(rep.assess(2, 30).startswith("insufficient"))
        self.assertTrue(rep.assess(4, 4).startswith("insufficient"))
        self.assertTrue(rep.assess(3, 7).startswith("weak"))
        self.assertTrue(rep.assess(6, 8).startswith("repeated"))
        self.assertTrue(rep.assess(5, 20).startswith("weak"))

    def test_consecutive_days(self):
        rows = rep.consecutive_day_comparison([(DAYS[1], 582.0), (DAYS[2], 578.0), (DAYS[4], 585.0)])
        self.assertEqual([r[2] for r in rows], [None, -4.0, None])

    def test_sku_ordering(self):
        def wave(*members):
            w = rep.WaveRow(1, WATERSIDE, members[0][1], members[-1][1], len(members), 1,
                            False, False, False, "complete", "new_availability")
            w.members = [rep.MemberRow(p, t, None, "exact", None) for p, t in members]
            return w
        t = datetime(2026, 10, 27, 13, 0, tzinfo=LOCAL_TZ)
        waves = [wave((SW256, t), (NS1T, t + timedelta(minutes=2))) for _ in range(3)]
        order, pairs = rep.sku_ordering(waves)
        first = {o.code: o.first_poll for o in order}
        self.assertEqual(first, {"SW256": 3, "NS1T": 0})
        self.assertIn(("SW256", "NS1T", 3, 3), pairs)
        self.assertIn(("NS1T", "SW256", 0, 3), pairs)


class ReportTextTests(HistoryCase):
    def test_store_report_patterns(self):
        text = rep.format_report(self.db, 8, rep.resolve_store("Waterside Shops"), now=NOW)
        self.assertIn("=== Waterside Shops [R169]", text)
        self.assertIn("Waves observed: 3; with reliable timing: 3", text)
        self.assertIn("Dates with >=1 timed wave: 3 of 7 monitored", text)
        self.assertIn("Earliest start: 09:38   Latest start: 09:45", text)
        self.assertIn("Median start: 09:42", text)
        self.assertIn("10-min: 09:38-09:45  supporting dates 3, monitored 7, window covered on 7", text)
        self.assertIn("weak: 3/7 covered dates", text)
        self.assertIn("Highlight: recurring 09:38-09:45 window: Tue 10/27 09:42, Wed 10/28 09:38, Thu 10/29 09:45",
                      text)
        self.assertIn(" 5-min:", text)
        self.assertIn("Wed 2026-10-28  09:38  -4 min vs prior day  <- within 15 min", text)
        self.assertIn("Tue    1 / 1", text)
        self.assertIn("Fri    0 / 1", text)
        self.assertIn("SW256  3/3 first", text)
        self.assertIn("Other SKUs appeared before 1TB/2TB NS in the same wave: 2/3", text)
        self.assertIn("Waves including 1TB/2TB Night Sky: 3; without: 0", text)
        self.assertIn("Colors: both 3, Night Sky only 0, Star White only 0", text)
        self.assertIn("Wave detail:", text)
        self.assertNotIn("repeated in observed data", text)
        self.assertIn(rep.CAVEAT, text)

    def test_all_store_report(self):
        text = rep.format_report(self.db, 8, None, now=NOW)
        self.assertIn("4 wave(s) across 18 store(s)", text)
        self.assertIn("=== Coconut Point [R185]", text)
        self.assertIn("Mean/median start not meaningful: only one timed wave.", text)
        self.assertIn("Stores with no waves in range:", text)
        self.assertNotIn("Wave detail:", text)

    def test_days_filter(self):
        text = rep.format_report(self.db, 1, None, now=NOW)
        self.assertIn("0 wave(s)", text)

    def test_waves_listing(self):
        text = rep.format_waves(self.db)
        self.assertIn("4 restock wave(s)", text)
        self.assertIn("2026-10-27 Tue 09:42:00 EDT  Waterside Shops", text)
        self.assertIn("[SW256,NS256,NS1T]", text)
        self.assertIn("last unavailable 10-27 09:41:00", text)
        self.assertIn("available 78.0 min", text)

    def test_cli_report_and_waves(self):
        for argv, needle in ((["--report", "--days", "30", "--store", "Waterside Shops"], "Waterside Shops [R169]"),
                             (["--report", "--store", "r185"], "Coconut Point [R185]"),
                             (["--waves"], "restock wave(s)")):
            with self.subTest(argv=argv), mock.patch("sys.stdout", new=io.StringIO()) as out:
                self.assertEqual(m.main(argv + ["--db", str(self.path)]), 0)
            self.assertIn(needle, out.getvalue())


class ExportTests(HistoryCase):
    def test_export_csv(self):
        out_dir = Path(self.tmp.name) / "export"
        with mock.patch("sys.stdout", new=io.StringIO()) as out:
            self.assertEqual(m.main(["--export-csv", str(out_dir), "--db", str(self.path)]), 0)
        files = {p.name for p in out_dir.iterdir()}
        self.assertEqual(files, set(rep.EXPORTS))
        with (out_dir / "store_restock_waves.csv").open() as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 4)
        self.assertTrue(rows[0]["first_detected_local"].endswith("-04:00"))
        self.assertTrue(rows[0]["first_detected_utc"].endswith("+00:00"))
        with (out_dir / "polls.csv").open() as fh:
            self.assertEqual(sum(1 for _ in fh) - 1, 7 * 240)
        with (out_dir / "availability_events.csv").open() as fh:
            kinds = {r["event_type"] for r in csv.DictReader(fh)}
        self.assertEqual(kinds, {"became_available", "became_unavailable"})
        self.assertIn("polls.csv", out.getvalue())


class EmptyHistoryTests(unittest.TestCase):
    def test_empty(self):
        db = HistoryDB(":memory:")
        self.assertEqual(rep.format_waves(db), "No restock waves recorded.")
        self.assertIn("No polling history in range.", rep.format_report(db, 7))
        db.close()

    def test_resolve_store(self):
        self.assertEqual(rep.resolve_store("waterside shops").store_id, WATERSIDE)
        self.assertEqual(rep.resolve_store("R185").store_id, COCONUT)
        self.assertEqual(rep.resolve_store("Brickell").store_id, "R623")
        self.assertIsNone(rep.resolve_store("Mall"))  # ambiguous: Florida Mall, The Gardens Mall
        self.assertEqual(len(STORES), 18)


if __name__ == "__main__":
    unittest.main()
