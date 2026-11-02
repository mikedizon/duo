"""The --tui view: model behavior, Monitor output into it, and the threaded run."""

from __future__ import annotations

import contextlib
import io
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from helpers import NS512, T0, WATERSIDE, Clock, m, outcome, quiet_logs, results
import tui

AT = datetime(2026, 10, 28, 13, 55, 22, tzinfo=timezone.utc)


def setUpModule():
    quiet_logs()


def poll(n: int, style: str = tui.OK, detail_lines: int = 3) -> tui.PollEntry:
    return tui.PollEntry(n, AT + timedelta(seconds=30 * n), f"#{n} summary", [f"row {i}" for i in range(detail_lines)],
                         style)


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.m = tui.TuiModel()

    def test_newest_first_and_cap(self):
        small = tui.TuiModel(max_entries=5)
        for n in range(1, 9):
            small.add_poll(poll(n))
        self.assertEqual([e.poll_no for e in small.entries], [8, 7, 6, 5, 4])
        self.assertEqual(small.polls, 8)

    def test_collapsed_by_default_notable_expanded(self):
        self.m.add_poll(poll(1))
        self.m.add_poll(poll(2, tui.NEW))
        self.m.add_poll(poll(3, tui.BLOCKED))
        self.assertEqual([e.expanded for e in self.m.entries], [False, True, False])
        rows = self.m.render(80, 20)
        self.assertEqual(rows[0].text, "▸ #3 summary")
        self.assertEqual(rows[1].text, "▾ #2 summary")
        self.assertEqual(rows[2].text, tui.DETAIL_INDENT + "row 0")
        self.assertEqual(rows[2].style, tui.DETAIL)

    def test_toggle_expand_collapse_all(self):
        for n in (1, 2):
            self.m.add_poll(poll(n))
        self.m.toggle()
        self.assertTrue(self.m.entries[0].expanded)
        self.assertEqual(len(self.m.render(80, 20)), 2 + 3)
        self.m.toggle()
        self.assertEqual(len(self.m.render(80, 20)), 2)
        self.m.expand_all()
        self.assertEqual(len(self.m.render(80, 20)), 2 + 6)
        self.m.collapse_all()
        self.assertEqual(len(self.m.render(80, 20)), 2)

    def test_cursor_moves_and_clamps(self):
        for n in range(1, 4):
            self.m.add_poll(poll(n))
        self.m.move(-5)
        self.assertEqual(self.m.cursor, 0)
        self.m.move(1)
        self.assertEqual(self.m.selected.poll_no, 2)
        self.m.move(99)
        self.assertEqual(self.m.selected.poll_no, 1)
        self.m.home()
        self.assertEqual(self.m.selected.poll_no, 3)
        self.m.end()
        self.assertEqual(self.m.selected.poll_no, 1)

    def test_follow_newest_only_at_top(self):
        self.m.add_poll(poll(1))
        self.m.add_poll(poll(2))
        self.assertEqual(self.m.selected.poll_no, 2)  # at top: follows the newest
        self.m.move(1)
        self.m.add_poll(poll(3))
        self.assertEqual(self.m.selected.poll_no, 1)  # reading older polls: selection stays put

    def test_selected_row_marked_and_kept_in_view(self):
        for n in range(1, 30):
            self.m.add_poll(poll(n))
        self.m.move(20)
        rows = self.m.render(80, 5)
        self.assertEqual(len(rows), 5)
        self.assertEqual([r.text for r in rows if r.selected], [f"▸ #{29 - 20} summary"])

    def test_width_and_tiny_screens(self):
        self.m.add_poll(tui.PollEntry(1, AT, "x" * 300, ["y" * 300], tui.NEW))
        for width in (1, 10, 79):
            self.assertTrue(all(len(r.text) <= width for r in self.m.render(width, 10)))
        self.assertEqual(self.m.render(0, 10), [])
        self.assertEqual(self.m.render(10, 0), [])
        self.assertLessEqual(len(self.m.status(20, AT)), 20)

    def test_events_interleave_and_blank_lines_dropped(self):
        self.m.add_poll(poll(1))
        self.m.add_event("Selected tunnel: 45", AT)
        self.m.add_event("   ", AT)
        self.m.add_poll(poll(2))
        texts = [r.text for r in self.m.render(80, 10)]
        self.assertEqual(texts[0], "▸ #2 summary")
        self.assertIn("Selected tunnel: 45", texts[1])
        self.assertEqual(texts[2], "▸ #1 summary")
        self.m.move(1)
        self.m.toggle()  # events don't expand
        self.assertEqual(len(self.m.render(80, 10)), 3)

    def test_status_line(self):
        self.m.add_poll(poll(1, tui.BLOCKED), available=2, tunnel="45 (37.19.221.240)")
        s = self.m.status(200, AT + timedelta(seconds=40), interval_s=30)
        for part in ("polls 1", "available 2/144", "tunnel 45 (37.19.221.240)", "last 541", "next ~20s", "q quit"):
            self.assertIn(part, s)

    def test_event_writer(self):
        w = tui.EventWriter(self.m, lambda: AT)
        print("first line\nsecond", file=w)
        w.write("partial")
        w.flush()
        self.assertEqual([e.text for e in reversed(self.m.entries)], ["first line", "second", "partial"])

    def test_threaded_adds(self):
        def add(base):
            for n in range(200):
                self.m.add_poll(poll(base + n))
        threads = [threading.Thread(target=add, args=(i * 1000,)) for i in range(4)]
        for t in threads:
            t.start()
        for _ in range(50):
            self.m.render(80, 24)
        for t in threads:
            t.join()
        self.assertEqual(self.m.polls, 800)


class MonitorSinkTests(unittest.TestCase):
    def mon(self, check, label=None):
        model = tui.TuiModel()
        out = io.StringIO()
        mon = m.Monitor(30.0, timedelta(minutes=10), check=check, out=out, color=False, notifier=mock.Mock(),
                        wall=Clock(T0), sink=model, vpn_label=label)
        return mon, model, out

    def test_poll_becomes_entry_not_printed(self):
        mon, model, out = self.mon(lambda **_: outcome(results()), label=lambda: "47 (37.19.221.244)")
        mon.poll()
        self.assertEqual(out.getvalue(), "")
        (e,) = model.entries
        self.assertEqual(e.style, tui.OK)
        self.assertFalse(e.expanded)
        self.assertRegex(e.summary, r"^#1 \d\d:\d\d:\d\d  0/144  47 \(37\.19\.221\.244\)  req 2  ok$")
        self.assertTrue(any("Waterside Shops" in line for line in e.detail))
        self.assertTrue(any(line.startswith("Available 0/144") for line in e.detail))
        self.assertEqual(model.tunnel, "47 (37.19.221.244)")

    def test_new_stock_expanded_and_named(self):
        mon, model, _ = self.mon(lambda **_: outcome(results([(WATERSIDE, NS512)])))
        mon.poll()
        e = model.entries[0]
        self.assertEqual(e.style, tui.NEW)
        self.assertTrue(e.expanded)
        self.assertIn("NEW: Waterside Shops 512GB Night Sky", e.summary)
        self.assertTrue(any("AVAILABLE" in line for line in e.detail))  # the transition line
        self.assertEqual(model.available, 1)

    def test_541_and_errors_flagged(self):
        blocked = m.FetchError("blocked", "HTTP 541", 541)
        mon, model, _ = self.mon(lambda **_: outcome(results(), [blocked]))
        mon.poll()
        self.assertEqual(model.entries[0].style, tui.BLOCKED)
        self.assertIn("HTTP 541/403", model.entries[0].summary)
        self.assertIsNotNone(model.last_blocked)
        mon2, model2, _ = self.mon(lambda **_: outcome(results(), [m.FetchError("network", "x")]))
        mon2.poll()
        self.assertEqual(model2.entries[0].style, tui.ERROR)
        self.assertIn("errors: network", model2.entries[0].summary)

    def test_without_sink_output_unchanged(self):
        out = io.StringIO()
        mon = m.Monitor(30.0, timedelta(minutes=10), check=lambda **_: outcome(results()), out=out, color=False,
                        notifier=mock.Mock(), wall=Clock(T0))
        mon.poll()
        self.assertIn("Poll #1", out.getvalue())
        self.assertIn("Waterside Shops", out.getvalue())


class StayOpenTests(unittest.TestCase):
    """A poll that found stock stays open while that stock lasts, then folds itself."""

    def run_polls(self, seq, toggle_first_found=False):
        model = tui.TuiModel()
        it = iter(seq)
        mon = m.Monitor(30.0, timedelta(minutes=10), check=lambda **_: next(it), out=io.StringIO(), color=False,
                        notifier=mock.Mock(), wall=Clock(T0), sink=model)
        found = None
        for i in range(len(seq)):
            mon.wall.advance(seconds=30)
            mon.poll()
            if found is None and model.entries[0].found:
                found = model.entries[0]
                if toggle_first_found:
                    model.toggle()  # the user closes it by hand right away
        return model, found

    def test_open_while_in_stock_then_folds_when_sold_out(self):
        model, found = self.run_polls([outcome(results()), outcome(results([(WATERSIDE, NS512)])),
                                       outcome(results([(WATERSIDE, NS512)])), outcome(results([(WATERSIDE, NS512)]))])
        self.assertTrue(found.expanded)
        self.assertEqual(found.style, tui.NEW)
        self.assertEqual(model.in_stock, 1)
        row = next(r for r in model.render(200, 50) if "★" in r.text)
        self.assertTrue(row.text.startswith("▾ ★ #2 "))

        model, found = self.run_polls([outcome(results([(WATERSIDE, NS512)])), outcome(results([(WATERSIDE, NS512)])),
                                       outcome(results())])
        self.assertFalse(found.expanded)
        self.assertEqual(found.style, tui.GONE)
        self.assertIsNotNone(found.gone_at)
        self.assertEqual(model.in_stock, 0)
        row = next(r for r in model.render(200, 50) if "★" in r.text)
        self.assertTrue(row.text.startswith("▸ ★ #1 "))
        self.assertIn("(sold out by ", row.text)
        self.assertIn("NEW: Waterside Shops 512GB Night Sky", row.text)  # still says what it found

    def test_error_poll_is_not_sold_out(self):
        from helpers import E, store_status
        errored = outcome(store_status(results(), WATERSIDE, E), [m.FetchError("network", "x")])
        model, found = self.run_polls([outcome(results([(WATERSIDE, NS512)])), errored])
        self.assertTrue(found.expanded)
        self.assertIsNone(found.gone_at)  # the last confirmed status is still available

    def test_user_choice_is_kept(self):
        model, found = self.run_polls([outcome(results([(WATERSIDE, NS512)])), outcome(results())],
                                      toggle_first_found=True)
        self.assertEqual(found.style, tui.GONE)
        self.assertFalse(found.expanded)  # closed by hand, stays closed
        model.home()
        model.move(1)
        model.toggle()  # reopen by hand after it sold out
        self.assertTrue(found.expanded)
        model.add_poll(poll(99), available_keys=frozenset())
        self.assertTrue(found.expanded)  # automatic folding leaves it alone now


class CliTests(unittest.TestCase):
    def test_tui_only_for_monitoring(self):
        for argv in (["--tui", "--once"], ["--tui", "--dry-run"], ["--tui", "--report"], ["--tui", "--vpn-list"],
                     ["--tui", "--vpn-test"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as cm:
                m.main(argv)
            self.assertEqual(cm.exception.code, 2)

    def test_tui_needs_a_terminal(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm, \
                mock.patch("sys.stdout", new=io.StringIO()):
            m.main(["--tui"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("--tui needs an interactive terminal", err.getvalue())


class FakeApp:
    """Stands in for the curses UI: waits for N polls, then presses q."""

    polls_before_quit = 3

    def __init__(self, model, stop, worker, clock, interval):
        self.model, self.stop, self.worker = model, stop, worker

    def run(self):
        deadline = time.monotonic() + 10
        while self.model.polls < self.polls_before_quit and self.worker.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.stop.set()
        while self.worker.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)


class ThreadedRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "h.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def fast_loop(self, mon, *, stop, network=None, **_):
        while not stop.is_set():
            mon.poll()
            m.vpn_say("Selected tunnel: 45")  # a tunnel event from the worker thread
            stop.wait(0.01)

    def test_quit_stops_worker_then_normal_teardown(self):
        args = m.build_parser().parse_args(["--tui", "--db", str(self.db), "--interval", "30"])
        out = io.StringIO()
        with mock.patch.object(m, "TUI_APP_FACTORY", FakeApp), \
                mock.patch.object(m, "run_loop", self.fast_loop), \
                mock.patch.object(m, "check_all", lambda **_: outcome(results())), \
                mock.patch("signal.signal"), contextlib.redirect_stdout(out):
            rc = m.run_monitor(args)
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertNotIn("Poll #", text)  # matrices went to the TUI, not the terminal
        self.assertIn("Last events:", text)
        self.assertIn("Selected tunnel: 45", text)
        self.assertIn("Stopped.", text)
        self.assertIsNone(m.CONSOLE_SINK)
        from inventory_db import HistoryDB
        db = HistoryDB(self.db)
        self.assertGreaterEqual(db.query("SELECT COUNT(*) FROM polls")[0][0], 3)
        self.assertIsNotNone(db.query("SELECT ended_utc FROM monitor_runs")[0][0])
        db.close()

    def test_loop_exception_reraised_in_main_thread(self):
        from wg_errors import HaltedError

        def failing_loop(mon, *, stop, network=None, **_):
            mon.poll()
            raise HaltedError("network restoration failed")

        args = m.build_parser().parse_args(["--tui", "--db", str(self.db)])
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(m, "TUI_APP_FACTORY", FakeApp), mock.patch.object(m, "run_loop", failing_loop), \
                mock.patch.object(m, "check_all", lambda **_: outcome(results())), mock.patch("signal.signal"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = m.run_monitor(args)
        self.assertEqual(rc, m.VPN_EXIT_FAILED)
        self.assertIn("halted", err.getvalue())


if __name__ == "__main__":
    unittest.main()
