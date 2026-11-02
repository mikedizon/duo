"""CLI options and application lifecycle integration for WireGuard management."""

from __future__ import annotations

import argparse
import contextlib
import io
import random
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from helpers import Clock, m, outcome, quiet_logs, results
from test_iphone_monitor import BLOCKED, FakeStop
from wg_fakes import FakeClock, FakePlatform, FakeService, uuid
from wg_manager import ManagerSettings, MemoryStore, NetworkGate, ProcessLock, RotationPolicy, WireGuardManager
from inventory_db import HistoryDB


def setUpModule():
    quiet_logs()


class Env(unittest.TestCase):
    """Temp config dir, temp DB, fake host network; the real network and scutil are never used."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db_path = root / "h.sqlite3"
        self.clock = FakeClock()
        self.plat = FakePlatform(self.clock, services=3)
        self.patches = [mock.patch.dict(m.VPN_MANAGER_OPTIONS, {
            "settings": ManagerSettings(), "clock": self.clock, "wall": self.clock.wall,
            "sleep": self.clock.sleep, "http_get": self.plat.http_get})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def args(self, *argv: str) -> argparse.Namespace:
        parser = m.build_parser()
        a = parser.parse_args([*argv, "--db", str(self.db_path)])
        m._check_vpn_args(parser, a)
        return a

    def run_cli(self, fn, *args, **kw) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = fn(*args, **kw)
        return rc, out.getvalue()

    def assertHostRestored(self):
        self.assertIsNone(self.plat.tunnel)
        self.assertEqual(self.plat.ifaces, ["lo0", "en0", "utun0", "utun1", "utun2"])
        self.assertTrue(all(sv.status == "Disconnected" for sv in self.plat.svcs.values()))


class ArgumentTests(unittest.TestCase):
    def assertUsageError(self, argv: list[str]) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            m.main(argv)
        self.assertEqual(cm.exception.code, 2)

    def test_conflicts_rejected(self):
        for argv in (
            ["--vpn-rotate-minutes", "15"],
            ["--vpn-rotate-every-cycles", "10"],
            ["--vpn-enabled", "--vpn-rotate-minutes", "15", "--vpn-rotate-every-cycles", "10"],
            ["--vpn-enabled", "--dry-run"],
            ["--vpn-enabled", "--report"],
            ["--vpn-enabled", "--export-csv"],
            ["--vpn-enabled", "--vpn-test"],
            ["--vpn-list", "--vpn-test"],
            ["--vpn-status", "--once"],
            ["--once", "--vpn-enabled", "--vpn-rotate-minutes", "5"],
            ["--vpn-enabled", "--vpn-rotate-minutes", "0"],
            ["--vpn-enabled", "--vpn-rotate-every-cycles", "-1"],
            ["--vpn-enabled", "--vpn-rotate-minutes", "nan"],
        ):
            with self.subTest(argv=argv):
                self.assertUsageError(argv)

    def test_valid_combinations(self):
        p = m.build_parser()
        for argv in (["--vpn-enabled"], ["--vpn-enabled", "--vpn-rotate-minutes", "15"],
                     ["--vpn-enabled", "--vpn-rotate-every-cycles", "10"], ["--vpn-enabled", "--once"],
                     ["--vpn-list"], ["--vpn-test"], ["--vpn-status"]):
            with self.subTest(argv=argv):
                a = p.parse_args(argv)
                m._check_vpn_args(p, a)


class ListTests(Env):
    def test_lists_wireguard_app_tunnels_without_connecting(self):
        rc, out = self.run_cli(m.run_vpn_list, self.args("--vpn-list"), platform=self.plat)
        self.assertEqual(rc, 0)
        self.assertIn("3 WireGuard-app tunnel(s)", out)
        self.assertIn("100                      203.0.113.10:51820       Disconnected", out)
        self.assertNotIn("ProtonVPN", out)
        self.assertFalse([c for c in self.plat.calls if c[0] in ("start", "stop")])

    def test_no_tunnels(self):
        for i in range(3):
            del self.plat.svcs[uuid(i)]
        rc, out = self.run_cli(m.run_vpn_list, self.args("--vpn-list"), platform=self.plat)
        self.assertEqual(rc, 1)
        self.assertIn("Import tunnels in the WireGuard app", out)


class TestModeTests(Env):
    def test_pass_and_clean_disconnect(self):
        rc, out = self.run_cli(m.run_vpn_test, self.args("--vpn-test"), platform=self.plat)
        self.assertEqual(rc, 0, out)
        self.assertIn("WireGuard test result: PASS", out)
        self.assertIn("Interface: utun9", out)
        self.assertIn("Public IP: 185.0.0.1", out)
        self.assertIn("State: READY", out)
        self.assertIn("DISCONNECTED", out)
        self.assertHostRestored()
        db = HistoryDB(self.db_path)
        row = db.latest_vpn_session()
        self.assertEqual(row["teardown_outcome"], "clean")
        self.assertEqual(row["start_reason"], "vpn_test")
        events = [r["event"] for r in db.query("SELECT event FROM vpn_events ORDER BY event_id")]
        self.assertEqual(events[0], "CONNECTING")
        self.assertIn("READY", events)
        self.assertEqual(events[-1], "DISCONNECTED")
        db.close()

    def test_failure_reported_and_host_clean(self):
        self.plat.never_connects = True
        rc, out = self.run_cli(m.run_vpn_test, self.args("--vpn-test"), platform=self.plat)
        self.assertEqual(rc, 1)
        self.assertIn("WireGuard test result: FAIL", out)
        self.assertIn("[tunnel_start_failed]", out)
        self.assertHostRestored()

    def test_lock_held_by_another_process(self):
        with ProcessLock(self.db_path.with_name(self.db_path.name + ".vpn.lock")):
            rc, out = self.run_cli(m.run_vpn_test, self.args("--vpn-test"), platform=self.plat)
        self.assertEqual(rc, 1)
        self.assertIn("only one instance", out)
        self.assertFalse([c for c in self.plat.calls if c[0] == "start"])

    def test_recovers_orphan_before_testing(self):
        db = HistoryDB(self.db_path)
        WireGuardManager(self.plat, db, ManagerSettings(), clock=self.clock,
                         wall=self.clock.wall, http_get=self.plat.http_get).connect("100")
        db.close()  # "crash": tunnel left up
        rc, out = self.run_cli(m.run_vpn_test, self.args("--vpn-test"), platform=self.plat)
        self.assertEqual(rc, 0, out)
        self.assertIn("recovering", out)
        db = HistoryDB(self.db_path)
        outcomes = [r[0] for r in db.query("SELECT teardown_outcome FROM vpn_sessions ORDER BY session_id")]
        self.assertEqual(outcomes, ["recovered_orphan", "clean"])
        db.close()
        self.assertHostRestored()


class StatusTests(Env):
    def test_no_database(self):
        rc, out = self.run_cli(m.run_vpn_status, self.args("--vpn-status"), platform=self.plat)
        self.assertEqual(rc, 1)
        self.assertIn("No application-managed tunnel", out)

    def test_live_tunnel_healthy_then_gone(self):
        db = HistoryDB(self.db_path)
        mgr = WireGuardManager(self.plat, db, ManagerSettings(), clock=self.clock,
                               wall=self.clock.wall, http_get=self.plat.http_get)
        mgr.connect("101")
        rc, out = self.run_cli(m.run_vpn_status, self.args("--vpn-status"), platform=self.plat)
        self.assertEqual(rc, 0, out)
        for line in ("WireGuard tunnel: 101 (Connected)", "Interface: utun9",
                     "Verified public IP: 185.0.0.1 (matches)", "Health: HEALTHY"):
            self.assertIn(line, out)
        self.plat.tunnel_ip = "185.9.9.9"
        rc, out = self.run_cli(m.run_vpn_status, self.args("--vpn-status"), platform=self.plat)
        self.assertEqual(rc, 2)
        self.assertIn("UNHEALTHY", out)
        mgr.disconnect()
        db.close()
        rc, out = self.run_cli(m.run_vpn_status, self.args("--vpn-status"), platform=self.plat)
        self.assertEqual(rc, 1)
        self.assertIn("Last session: 101", out)
        self.assertEqual([c[0] for c in self.plat.calls if c[0] in ("start", "stop")], ["start", "stop"])  # status changed nothing

    def test_tunnel_started_outside_the_app(self):
        db = HistoryDB(self.db_path)
        mgr = WireGuardManager(self.plat, db, ManagerSettings(), clock=self.clock,
                               wall=self.clock.wall, http_get=self.plat.http_get)
        mgr.connect("101")
        mgr.disconnect()
        db.close()
        self.plat.user_starts("101")
        rc, out = self.run_cli(m.run_vpn_status, self.args("--vpn-status"), platform=self.plat)
        self.assertEqual(rc, 2)
        self.assertIn("started outside the application", out)


class ScriptedGate:
    def __init__(self, permits, switch_on_block=()):
        self.permits = list(permits)
        self.switch_on_block = list(switch_on_block)
        self.after: list[tuple[int, bool]] = []
        self.blocked_calls = 0
        self.retry_after_s = 7.0
        self.public_ip = "185.0.0.1"
        self.block_switches = 0
        self.block_limit = 3

    def before_cycle(self):
        return self.permits.pop(0)

    def after_cycle(self, network_errors=0, blocked=False):
        self.after.append((network_errors, blocked))

    def on_blocked(self):
        self.blocked_calls += 1
        ok = self.switch_on_block.pop(0)
        self.block_switches += ok
        return ok


class LoopGateTests(unittest.TestCase):
    def harness(self, outcomes, gate):
        clock = FakeClock()
        calls = []

        def check(zips=None):
            calls.append((clock.t, tuple(zips) if zips else None))
            return outcomes[len(calls) - 1]

        mon = m.Monitor(20.0, timedelta(minutes=10), check=check, out=io.StringIO(), color=False,
                        notifier=mock.Mock(), wall=Clock(), mono=clock)
        m.run_loop(mon, stop=FakeStop(clock), backoff=m.Backoff(20.0, rand=lambda a, b: 1.0), clock=clock,
                   max_cycles=len(outcomes), rng=random.Random(0), network=gate)
        return calls, mon

    def test_no_poll_while_not_permitted(self):
        gate = ScriptedGate([False, False, True, True])
        calls, _ = self.harness([outcome(results())] * 2, gate)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], 1000.0 + 14.0)  # waited retry_after_s twice before the first poll
        self.assertEqual(gate.after, [(0, False), (0, False)])

    def test_network_errors_reported_to_gate(self):
        gate = ScriptedGate([True, True])
        self.harness([outcome(results(), [m.FetchError("network", "x"), m.FetchError("network", "y")]),
                      outcome(results())], gate)
        self.assertEqual(gate.after, [(2, False), (0, False)])

    def test_541_switch_skips_cooldown(self):
        gate = ScriptedGate([True] * 3, switch_on_block=[True])
        calls, mon = self.harness([outcome(results(), [BLOCKED]), outcome(results()), outcome(results())], gate)
        self.assertEqual(gate.blocked_calls, 1)
        self.assertFalse(mon.cooldown.active)
        self.assertEqual([z for _, z in calls], [None, None, None])  # full polls, never a one-region probe
        self.assertEqual(calls[1][0] - calls[0][0], 0.0)  # next poll right away
        self.assertIn("switching tunnel (1/3) instead of cooling down", mon.stream.getvalue())
        self.assertEqual(gate.after[0], (0, True))

    def test_541_switch_declined_uses_cooldown(self):
        gate = ScriptedGate([True] * 2, switch_on_block=[False])
        calls, mon = self.harness([outcome(results(), [BLOCKED]), outcome(results())], gate)
        self.assertEqual(calls[1][0] - calls[0][0], 300.0)
        self.assertEqual(len(calls[1][1]), 1)  # cooldown probe
        self.assertIn("Sending nothing for 5 min", mon.stream.getvalue())

    def test_403_never_switches(self):
        gate = ScriptedGate([True] * 2)
        calls, _ = self.harness([outcome(results(), [m.FetchError("forbidden", "403", 403)]), outcome(results())],
                                gate)
        self.assertEqual(gate.blocked_calls, 0)
        self.assertEqual(calls[1][0] - calls[0][0], 300.0)


class LifecycleTests(Env):
    """Real Monitor + HistoryDB + NetworkGate + WireGuardManager over the fake host."""

    def run_monitored(self, outcomes, policy, gate_enabled=True):
        db = HistoryDB(":memory:")
        mgr = WireGuardManager(self.plat, db, ManagerSettings(),
                               rng=random.Random(1), clock=self.clock, wall=self.clock.wall,
                               sleep=self.clock.sleep, http_get=self.plat.http_get)
        gate = NetworkGate(mgr, policy, clock=self.clock) if gate_enabled else None
        if gate:
            gate.start()
        calls = []

        def check(zips=None):
            assert gate is None or gate.permitted(), "poll ran without a verified tunnel"
            calls.append((self.clock.t, tuple(zips) if zips else None))
            return outcomes[len(calls) - 1]

        mon = m.Monitor(20.0, timedelta(minutes=10), db=db, check=check, out=io.StringIO(), color=False,
                        notifier=mock.Mock(), wall=self.clock.wall, mono=self.clock,
                        vpn_session=(lambda: gate.session_id) if gate else None)
        with contextlib.redirect_stdout(io.StringIO()):
            m.run_loop(mon, stop=FakeStop(self.clock), backoff=m.Backoff(20.0, rand=lambda a, b: 1.0),
                       clock=self.clock, max_cycles=len(outcomes), rng=random.Random(0), network=gate)
        return db, mgr, mon, calls

    def test_cycle_rotation_links_polls_to_sessions(self):
        db, mgr, _, calls = self.run_monitored([outcome(results())] * 5, RotationPolicy("cycles", 2))
        sessions = [r[0] for r in db.query("SELECT vpn_session_id FROM polls ORDER BY poll_id")]
        self.assertEqual(sessions, [1, 1, 2, 2, 3])
        configs = [r[0] for r in db.query("SELECT config_name FROM vpn_sessions ORDER BY session_id")]
        self.assertTrue(all(a != b for a, b in zip(configs, configs[1:])))
        self.assertEqual(len(calls), 5)
        mgr.cleanup()
        self.assertHostRestored()

    def test_rotation_preserves_cooldown_and_backoff(self):
        forbidden = m.FetchError("forbidden", "HTTP 403", 403)  # 403 always cools down; 541 switches tunnels
        seq = [outcome(results()), outcome(results(), [forbidden]), outcome(results()),
               outcome(results()), outcome(results())]
        _, _, mon_plain, plain = self.run_monitored(seq, RotationPolicy(), gate_enabled=False)
        self.plat = FakePlatform(self.clock)
        _, mgr, mon_vpn, vpn = self.run_monitored(seq, RotationPolicy("cycles", 1))
        gaps = lambda c: [round(b[0] - a[0], 3) for a, b in zip(c, c[1:])]  # noqa: E731
        # Same schedule with and without scheduled rotation: 300 s cooldown after the 403, then a
        # single-region probe; scheduled rotation never resets the cooldown or resends the refused poll.
        self.assertEqual([z is None for _, z in vpn], [z is None for _, z in plain])
        self.assertEqual(gaps(plain)[1], 300.0)
        self.assertGreaterEqual(gaps(vpn)[1], 300.0)
        self.assertEqual(len(vpn[2][1]), 1)
        self.assertGreaterEqual(sum(1 for e in mgr.store.query("SELECT event FROM vpn_events")
                                    if e[0] == "ROTATION_STARTED"), 3)
        mgr.cleanup()

    def test_lost_tunnel_pauses_polls_until_reconnected(self):
        seq = [outcome(results(), [m.FetchError("network", "x")])] + [outcome(results())] * 2
        db = HistoryDB(":memory:")
        mgr = WireGuardManager(self.plat, db, ManagerSettings(),
                               clock=self.clock, wall=self.clock.wall, sleep=self.clock.sleep,
                               http_get=self.plat.http_get)
        gate = NetworkGate(mgr, RotationPolicy(), clock=self.clock)
        gate.start()
        calls = []

        def check(zips=None):
            assert gate.permitted()
            calls.append(self.clock.t)
            self.clock.advance(1.0)  # polls take time; keeps started_utc unique
            if len(calls) == 1:
                self.plat.drop_tunnel()  # tunnel dies during the poll
            return seq[len(calls) - 1]

        mon = m.Monitor(20.0, timedelta(minutes=10), db=db, check=check, out=io.StringIO(), color=False,
                        notifier=mock.Mock(), wall=self.clock.wall, mono=self.clock,
                        vpn_session=lambda: gate.session_id)
        with contextlib.redirect_stdout(io.StringIO()):
            m.run_loop(mon, stop=FakeStop(self.clock), backoff=m.Backoff(20.0, rand=lambda a, b: 1.0),
                       clock=self.clock, max_cycles=3, rng=random.Random(0), network=gate)
        events = [r[0] for r in db.query("SELECT event FROM vpn_events ORDER BY event_id")]
        self.assertIn("CONNECTION_LOST", events)
        self.assertEqual([r[0] for r in db.query("SELECT vpn_session_id FROM polls ORDER BY poll_id")], [1, 2, 2])
        row = db.query("SELECT teardown_outcome, rotation_reason FROM vpn_sessions WHERE session_id = 1")[0]
        self.assertEqual(tuple(row), ("clean", "rotation:reconnect"))  # DNS left by the dead tunnel restored
        mgr.cleanup()
        self.assertHostRestored()


class BlockSwitchTests(Env):
    """HTTP 541 with the real gate, manager and fake host."""

    def setUp(self):
        super().setUp()
        for i in (3, 4):  # five tunnels: 100..104
            self.plat.add_service(FakeService(i))
        for i in range(5):
            self.plat.ip_by_endpoint[f"203.0.113.{10 + i}:51820"] = f"185.0.0.{10 + i}"

    def run_seq(self, seq, db=None, rng=None):
        db = db or HistoryDB(":memory:")
        mgr = WireGuardManager(self.plat, db, ManagerSettings(),
                               rng=rng or random.Random(3), clock=self.clock, wall=self.clock.wall, sleep=self.clock.sleep,
                               http_get=self.plat.http_get)
        gate = NetworkGate(mgr, RotationPolicy(), clock=self.clock)
        gate.start()
        calls = []

        def check(zips=None):
            assert gate.permitted()
            calls.append((self.clock.t, tuple(zips) if zips else None, gate.public_ip))
            self.clock.advance(1.0)
            return seq[len(calls) - 1]

        mon = m.Monitor(20.0, timedelta(minutes=10), db=db, check=check, out=io.StringIO(), color=False,
                        notifier=mock.Mock(), wall=self.clock.wall,
                        mono=self.clock, vpn_session=lambda: gate.session_id)
        with contextlib.redirect_stdout(io.StringIO()):
            m.run_loop(mon, stop=FakeStop(self.clock), backoff=m.Backoff(20.0, rand=lambda a, b: 1.0),
                       clock=self.clock, max_cycles=len(seq), rng=random.Random(0), network=gate)
        return db, mgr, gate, mon, calls

    def events(self, db):
        return [r[0] for r in db.query("SELECT event FROM vpn_events ORDER BY event_id")]

    def test_541_switches_tunnel_and_keeps_polling(self):
        db, mgr, gate, mon, calls = self.run_seq([outcome(results(), [BLOCKED]), outcome(results()),
                                                  outcome(results())])
        self.assertNotEqual(calls[0][2], calls[1][2])  # different public IP after the refusal
        self.assertEqual([c[1] for c in calls], [None, None, None])
        self.assertFalse(mon.cooldown.active)
        self.assertEqual([r[0] for r in db.query("SELECT vpn_session_id FROM polls ORDER BY poll_id")], [1, 2, 2])
        reasons = [r[0] for r in db.query("SELECT start_reason FROM vpn_sessions ORDER BY session_id")]
        self.assertEqual(reasons, ["start", "blocked:541"])
        self.assertIn("BLOCKED_BY_REMOTE", self.events(db))
        self.assertEqual(gate.block_switches, 0)  # clean poll resets the run of switches
        mgr.cleanup()
        self.assertHostRestored()

    def test_three_switches_then_fallback_cooldown(self):
        seq = [outcome(results(), [BLOCKED])] * 4 + [outcome(results())]
        db, mgr, gate, mon, calls = self.run_seq(seq)
        ips = [c[2] for c in calls]
        self.assertEqual(len(set(ips[:4])), 4)  # four different tunnels tried
        self.assertEqual(calls[4][0] - calls[3][0], 1.0 + 300.0)  # poll time + 5-minute cooldown after the 4th refusal
        self.assertEqual(len(calls[4][1]), 1)  # then the usual single-region probe
        self.assertIn("BLOCKED_FALLBACK_COOLDOWN", self.events(db))
        self.assertEqual(self.events(db).count("ROTATION_STARTED"), 3)
        mgr.cleanup()

    def test_refused_ip_again_tries_one_more_config(self):
        class First(random.Random):
            def choice(self, seq):
                return seq[0]  # deterministic: first eligible config by name

        self.plat.ip_by_endpoint["203.0.113.11:51820"] = "185.0.0.10"  # 101.conf exits from 100.conf's IP
        db, mgr, gate, mon, calls = self.run_seq([outcome(results(), [BLOCKED]), outcome(results())], rng=First())
        self.assertEqual(calls[0][2], "185.0.0.10")  # 100.conf, refused
        self.assertIn("REFUSED_IP_AGAIN", self.events(db))  # switch landed on 101.conf, same IP
        self.assertEqual(calls[1][2], "185.0.0.12")  # one more try avoided both: 102.conf
        self.assertEqual(self.events(db).count("ROTATION_STARTED"), 2)
        mgr.cleanup()

    def test_same_ip_everywhere_is_accepted_after_one_extra_try(self):
        self.plat.ip_by_endpoint.clear()  # every tunnel exits from 185.0.0.1
        db, mgr, gate, mon, calls = self.run_seq([outcome(results(), [BLOCKED]), outcome(results())])
        self.assertEqual(self.events(db).count("ROTATION_STARTED"), 2)
        self.assertIn("REFUSED_IP_AGAIN", self.events(db))
        self.assertIn("SAME_PUBLIC_IP", self.events(db))
        self.assertEqual(len(calls), 2)
        mgr.cleanup()

    def test_soft_avoid_falls_back_when_everything_is_avoided(self):
        mgr = WireGuardManager(self.plat, MemoryStore(), ManagerSettings(),
                               clock=self.clock, wall=self.clock.wall, sleep=self.clock.sleep, http_get=self.plat.http_get)
        mgr.connect("100")
        st = mgr.rotate("x", avoid=[uuid(i) for i in range(5)])
        self.assertEqual(st.state, "READY")
        self.assertEqual(mgr.config_ips[uuid(0)], "185.0.0.10")
        mgr.cleanup()

    def test_restart_clears_cooldown_only_for_a_different_ip(self):
        db = HistoryDB(":memory:")
        db.start_vpn_session(config_name="x", config_path="x", wg_name="duowg", endpoint="e", copy_path="c",
                             started=self.clock.wall(), baseline_json="{}", reason="start")
        db.update_vpn_session(1, public_ip="185.0.0.99", ended_utc=self.clock.wall())
        from inventory_db import PollRecord
        db.record_poll(None, PollRecord(self.clock.wall(), self.clock.wall(), 2, [BLOCKED], results(),
                                        {sid: "ok" for sid in m.STORES}, vpn_session_id=1))
        self.assertEqual(db.last_blocked_poll_ip(), "185.0.0.99")
        mgr = WireGuardManager(self.plat, db, ManagerSettings(),
                               clock=self.clock, wall=self.clock.wall, sleep=self.clock.sleep, http_get=self.plat.http_get)
        gate = NetworkGate(mgr, RotationPolicy(), clock=self.clock)
        gate.start()
        self.assertTrue(gate.resume_check(db.last_blocked_poll_ip()))
        self.assertFalse(gate.resume_check(gate.public_ip))
        mgr.cleanup()


class DeadTunnelTests(Env):
    """2026-10-28 live: 47.conf passed traffic for two polls, then stayed Connected with no traffic."""

    def test_tunnel_that_stops_passing_traffic_is_replaced(self):
        db = HistoryDB(":memory:")
        mgr = WireGuardManager(self.plat, db, ManagerSettings(), clock=self.clock,
                               wall=self.clock.wall, sleep=self.clock.sleep, http_get=self.plat.http_get)
        gate = NetworkGate(mgr, RotationPolicy(), clock=self.clock)
        gate.start()
        network_error = outcome(results(), [m.FetchError("network", "timed out")])
        seq = [outcome(results()), network_error, outcome(results()), outcome(results())]
        calls = []

        def check(zips=None):
            assert gate.permitted()
            calls.append(gate.session_id)
            self.clock.advance(1.0)
            if len(calls) == 2:
                self.plat.dead_endpoints.add(self.plat.tunnel["endpoint"])  # dies during this poll
            return seq[len(calls) - 1]

        mon = m.Monitor(60.0, timedelta(minutes=10), db=db, check=check, out=io.StringIO(), color=False,
                        notifier=mock.Mock(), wall=self.clock.wall, mono=self.clock,
                        vpn_session=lambda: gate.session_id)
        with contextlib.redirect_stdout(io.StringIO()):
            m.run_loop(mon, stop=FakeStop(self.clock), backoff=m.Backoff(60.0, rand=lambda a, b: 1.0),
                       clock=self.clock, max_cycles=4, rng=random.Random(0), network=gate)
        self.assertEqual(calls, [1, 1, 2, 2])  # one failed poll, then a different tunnel
        configs = [r[0] for r in db.query("SELECT config_name FROM vpn_sessions ORDER BY session_id")]
        self.assertNotEqual(configs[0], configs[1])
        events = [r[0] for r in db.query("SELECT event FROM vpn_events ORDER BY event_id")]
        self.assertIn("CONNECTION_LOST", events)
        mgr.cleanup()
        self.assertHostRestored()


class RunMonitorTests(Env):
    def run_monitor(self, cycles: int, *argv: str) -> tuple[int, str]:
        real_loop = m.run_loop

        def bounded(mon, **kw):
            kw.setdefault("max_cycles", cycles)
            return real_loop(mon, clock=self.clock, stop=FakeStop(self.clock), **{k: v for k, v in kw.items()
                                                                                  if k not in ("stop",)})

        def check(zips=None, picker=None):
            time.sleep(0.002)  # Monitor stamps polls with the real wall clock; keep started_utc unique
            return outcome(results())

        with mock.patch.object(m, "run_loop", bounded), mock.patch.object(m, "check_all", check), \
                mock.patch("signal.signal"):
            return self.run_cli(m.run_monitor, self.args("--vpn-enabled", *argv), platform=self.plat)

    def test_monitor_connects_rotates_and_tears_down(self):
        rc, out = self.run_monitor(4, "--vpn-rotate-every-cycles", "2")
        self.assertEqual(rc, 0, out)
        self.assertIn("every 2 cycles", out)
        self.assertIn("ROTATION_STARTED", out)
        self.assertHostRestored()
        db = HistoryDB(self.db_path)
        self.assertEqual([r[0] for r in db.query("SELECT vpn_session_id FROM polls ORDER BY poll_id")], [1, 1, 2, 2])
        self.assertIsNotNone(db.query("SELECT ended_utc FROM monitor_runs")[0][0])
        configs = [r[0] for r in db.query("SELECT config_name FROM vpn_sessions ORDER BY session_id")]
        self.assertIn(f"| Tunnel {configs[0]} (185.0.0.1, Miami, Florida, US)", out)  # footer: tunnel, IP, city
        self.assertIn(f"| Tunnel {configs[1]} (185.0.0.1, Miami, Florida, US)", out)
        locations = [r[0] for r in db.query("SELECT location FROM vpn_sessions ORDER BY session_id")]
        self.assertEqual(locations, ["Miami, Florida, US", "Miami, Florida, US"])
        self.assertEqual(db.open_vpn_sessions(), [])
        db.close()

    def test_fails_closed_without_tunnel(self):
        self.plat.start_rc = 1
        with mock.patch.object(m, "check_all", side_effect=AssertionError("polled without tunnel")):
            rc, out = self.run_cli(m.run_monitor, self.args("--vpn-enabled"), platform=self.plat)
        self.assertEqual(rc, m.VPN_EXIT_FAILED)
        self.assertIn("polls never run without one", out)
        db = HistoryDB(self.db_path)
        self.assertEqual(db.query("SELECT COUNT(*) FROM polls")[0][0], 0)
        self.assertIsNotNone(db.query("SELECT ended_utc FROM monitor_runs")[0][0])
        db.close()
        self.assertHostRestored()

    def test_restore_failure_exits_with_instructions(self):
        self.plat.stop_ignored = True
        rc, out = self.run_monitor(1)
        self.assertEqual(rc, m.VPN_EXIT_FAILED)
        self.assertIn("Manual network recovery", out)
        self.assertIn("did not complete", out)


class MemoryStoreParityTests(unittest.TestCase):
    def test_history_db_store_round_trip(self):
        from datetime import datetime, timezone
        db = HistoryDB(":memory:")
        t = datetime(2026, 10, 27, 13, 0, tzinfo=timezone.utc)
        sid = db.start_vpn_session(config_name="a.conf", config_path="/x/a.conf", wg_name="duowg",
                                   endpoint="203.0.113.1:51820", copy_path="/t/duowg.conf", started=t,
                                   baseline_json="{}", reason="start")
        self.assertEqual(len(db.open_vpn_sessions()), 1)
        self.assertEqual(db.open_vpn_sessions()[0]["started"], t)
        self.assertIsNone(db.open_vpn_sessions()[0]["service_id"])  # v3-era row shape still accepted
        db.update_vpn_session(sid, public_ip="185.0.0.1", ip_changed=True, ended_utc=t, teardown_outcome="clean")
        self.assertEqual(db.open_vpn_sessions(), [])
        self.assertEqual(db.last_public_ip(), "185.0.0.1")
        self.assertEqual(db.latest_vpn_session()["ip_changed"], 1)
        db.record_vpn_event(sid, t, "READY", "READY", {"public_ip": "185.0.0.1"})
        self.assertEqual(db.query("SELECT fields_json FROM vpn_events")[0][0], '{"public_ip": "185.0.0.1"}')
        with self.assertRaises(ValueError):
            db.update_vpn_session(sid, baseline_json="tamper")
        sid2 = db.start_vpn_session(config_name="b.conf", config_path="/x/b.conf", wg_name="b", endpoint="e",
                                    copy_path="", started=t, baseline_json="{}", reason="start",
                                    service_id="EF0DCDE6-40CD-4F20-29DA-4B32B2146442")
        self.assertEqual(db.open_vpn_sessions()[-1]["service_id"], "EF0DCDE6-40CD-4F20-29DA-4B32B2146442")
        db.update_vpn_session(sid2, ended_utc=t)
        db.close()
        self.assertIsNone(MemoryStore().last_public_ip())


if __name__ == "__main__":
    unittest.main()
