"""WireGuardManager lifecycle over WireGuard-app tunnels: mapping, verification,
rotation, teardown and recovery."""

from __future__ import annotations

import logging
import random
import tempfile
import threading
import unittest
from pathlib import Path

from helpers import quiet_logs
from wg_fakes import DIRECT_IP, FakeClock, FakePlatform, FakeService, endpoint, uuid
from wg_errors import (
    ConflictingVpnError, ConnectivityError, DnsError, HaltedError, InterfaceError, InvalidTransition,
    LockHeldError, NoConfigsError, NotImportedError, PublicIpError, RestoreError, RotationInProgress,
    RoutingError, TunnelUpError,
)
from wg_manager import (
    ALLOWED, HANDSHAKE_VERIFIED, ConnState, ManagerSettings, MemoryStore, NetworkGate, ProcessLock,
    RotationPolicy, StateMachine, WireGuardManager,
)

S = ConnState


def setUpModule():
    quiet_logs()


class Base(unittest.TestCase):
    n_configs = 3

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.plat = FakePlatform(self.clock, services=self.n_configs)
        self.store = MemoryStore()
        self.console: list[str] = []
        self.mgr = self.make_manager()

    def make_manager(self, **settings) -> WireGuardManager:
        return WireGuardManager(self.plat, self.store, ManagerSettings(**settings),
                                rng=random.Random(7), clock=self.clock, wall=self.clock.wall,
                                sleep=self.clock.sleep, http_get=self.plat.http_get, console=self.console.append)

    def tearDown(self):
        self.tmp.cleanup()

    def events(self) -> list[str]:
        return [e["event"] for e in self.store.events]

    def commands(self, kind: str) -> list[str]:
        return [c[1] for c in self.plat.calls if c[0] == kind]

    def assertHostRestored(self):
        self.assertIsNone(self.plat.tunnel)
        self.assertEqual(self.plat.ifaces, ["lo0", "en0", "utun0", "utun1", "utun2"])
        self.assertTrue(all(s.status == "Disconnected" for s in self.plat.svcs.values()))
        self.assertEqual(self.plat.resolvers(), ["192.168.0.1"])


class StateMachineTests(unittest.TestCase):
    def test_every_transition(self):
        for src in S:
            for dst in S:
                sm = StateMachine()
                sm.state = src
                with self.subTest(src=src, dst=dst):
                    if dst in ALLOWED[src]:
                        sm.to(dst)
                        self.assertIs(sm.state, dst)
                    else:
                        with self.assertRaises(InvalidTransition):
                            sm.to(dst)
                        self.assertIs(sm.state, src)

    def test_ready_only_via_verifying(self):
        self.assertEqual([s for s in S if S.READY in ALLOWED[s]], [S.VERIFYING])


class RotationPolicyTests(unittest.TestCase):
    def test_time_based(self):
        p = RotationPolicy.from_options(15, None)
        self.assertIsNone(p.due(0, 899, 1000))
        self.assertEqual(p.due(0, 900, 0), "time:15min")

    def test_cycle_based(self):
        p = RotationPolicy.from_options(None, 10)
        self.assertIsNone(p.due(0, 10 ** 6, 9))
        self.assertEqual(p.due(0, 0, 10), "cycles:10")

    def test_none_and_conflicts(self):
        self.assertIsNone(RotationPolicy().due(0, 10 ** 9, 10 ** 9))
        with self.assertRaises(ValueError):
            RotationPolicy.from_options(15, 10)
        with self.assertRaises(ValueError):
            RotationPolicy("minutes", 0)
        with self.assertRaises(ValueError):
            RotationPolicy("weekly", 1)


class DiscoveryTests(Base):
    def test_every_wireguard_app_tunnel_is_eligible(self):
        self.assertEqual([t.name for t in self.mgr.discover_tunnels()], ["100", "101", "102"])
        self.assertEqual(self.mgr.discover_tunnels()[0].endpoint, endpoint(0))

    def test_non_wireguard_services_never_considered(self):
        self.assertNotIn("ProtonVPN", [t.name for t in self.mgr.discover_tunnels()])

    def test_no_tunnels(self):
        self.plat.svcs = {k: v for k, v in self.plat.svcs.items() if "wireguard" not in v.kind}
        with self.assertRaises(NoConfigsError) as cm:
            self.mgr.connect()
        self.assertIn("import tunnels in the WireGuard app", str(cm.exception))

    def test_unknown_tunnel_name(self):
        with self.assertRaises(NotImportedError):
            self.mgr.connect("999")
        self.assertEqual(self.commands("start"), [])

    def test_tunnel_added_while_running_is_picked_up_at_next_switch(self):
        self.mgr.connect("100")
        self.plat.add_service(FakeService(7, name="130"))
        self.assertNotIn("130", [t.name for t in self.mgr.discover_tunnels()])  # cached until a switch
        self.mgr.rng = random.Random(0)
        names = set()
        for _ in range(12):
            names.add(self.mgr.rotate("test").config)
        self.assertIn("130", names)
        self.assertIn("TUNNELS_ADDED", self.events())
        self.assertIn("New tunnel(s) available: 130", self.console)

    def test_tunnel_removed_while_running_is_dropped(self):
        self.mgr.connect("100")
        del self.plat.svcs[uuid(2)]
        for _ in range(6):
            self.assertNotEqual(self.mgr.rotate("test").config, "102")
        self.assertIn("TUNNELS_REMOVED", self.events())


class SelectionTests(Base):
    n_configs = 4

    def test_random_selection_covers_all_tunnels(self):
        seen = set()
        for seed in range(40):
            self.mgr.rng = random.Random(seed)
            seen.add(self.mgr.select_config().name)
        self.assertEqual(seen, {"100", "101", "102", "103"})

    def test_never_same_config_twice_in_a_row(self):
        for seed in range(30):
            self.mgr.rng = random.Random(seed)
            self.mgr.last_config = uuid(1)
            self.assertNotEqual(self.mgr.select_config().key, uuid(1))

    def test_single_tunnel_may_repeat(self):
        for i in (1, 2, 3):
            del self.plat.svcs[uuid(i)]
        self.mgr.discover_tunnels(refresh=True)
        self.mgr.last_config = uuid(0)
        self.assertEqual(self.mgr.select_config().key, uuid(0))


class ConnectTests(Base):
    def test_successful_connection(self):
        st = self.mgr.connect("100")
        self.assertEqual(st.state, "READY")
        self.assertEqual((st.config, st.service, st.interface), ("100", "100", "utun9"))
        self.assertEqual(st.handshake, HANDSHAKE_VERIFIED)
        self.assertEqual(st.public_ip, "185.0.0.1")
        self.assertIsNone(st.ip_changed)
        self.assertEqual(self.commands("start"), [uuid(0)])
        row = self.store.sessions[st.session_id]
        self.assertEqual((row["service_id"], row["wg_name"], row["interface"], row["state"]),
                         (uuid(0), "100", "utun9", "READY"))
        self.assertIn('"default_interface": "en0"', row["baseline_json"])
        self.assertEqual(self.events()[-1], "READY")
        for line in ("CONNECTING", "Interface: utun9", f"Handshake: {HANDSHAKE_VERIFIED}",
                     "Public IP: 185.0.0.1", "Connectivity: VERIFIED", "State: READY"):
            self.assertIn(line, self.console)

    def test_session_recorded_before_start(self):
        real = self.plat.start

        def start(u):
            self.assertEqual(self.store.open_vpn_sessions()[0]["service_id"], u)
            return real(u)
        self.plat.start = start
        self.mgr.connect("100")

    def test_start_command_fails(self):
        self.plat.start_rc = 1
        with self.assertRaises(TunnelUpError) as cm:
            self.mgr.connect("100")
        self.assertIn("No service", str(cm.exception))
        self.assertIs(self.mgr.state, S.DISCONNECTED)
        self.assertEqual(self.store.open_vpn_sessions(), [])
        self.assertHostRestored()

    def test_never_connects_is_bounded_and_stopped(self):
        self.plat.never_connects = True
        t0 = self.clock.t
        with self.assertRaises(TunnelUpError) as cm:
            self.mgr.connect("100")
        self.assertIn("not connected within 20s (status Connecting)", str(cm.exception))
        self.assertLess(self.clock.t - t0, 60)
        self.assertEqual(self.commands("stop"), [uuid(0)])
        self.assertHostRestored()

    def test_invalid_service(self):
        self.plat.goes_invalid = True
        with self.assertRaises(TunnelUpError):
            self.mgr.connect("100")
        self.assertIs(self.mgr.state, S.DISCONNECTED)

    def test_slow_connect_accepted(self):
        self.plat.connect_after_polls = 10
        self.assertEqual(self.mgr.connect("100").state, "READY")

    def test_traffic_not_through_tunnel(self):
        self.plat.probe_via_tunnel = False
        with self.assertRaises(RoutingError):
            self.mgr.connect("100")
        self.assertHostRestored()

    def test_endpoint_reported_via_tunnel_is_normal(self):
        # The WireGuard app's socket bypasses the routing table; route get shows the tunnel.
        self.plat.endpoint_via_tunnel = True
        self.assertEqual(self.mgr.connect("100").state, "READY")

    def test_interface_without_ipv4(self):
        self.plat.iface_address = "fe80::1"
        with self.assertRaises(InterfaceError):
            self.mgr.connect("100")
        self.assertHostRestored()

    def test_dns_not_applied(self):
        self.plat.tunnel_dns_active = False
        with self.assertRaises(DnsError):
            self.mgr.connect("100")
        self.assertHostRestored()

    def test_dns_resolution_failure(self):
        self.plat.dns_resolves = False
        with self.assertRaises(DnsError):
            self.mgr.connect("100")
        self.assertHostRestored()

    def test_public_ip_invalid_responses(self):
        for body in (b"<html>blocked</html>", b"10.0.0.1", b"1" * 100):
            with self.subTest(body=body):
                mgr = self.make_manager()
                self.plat.ip_responses = [DIRECT_IP.encode(), body, body]
                with self.assertRaises(PublicIpError):
                    mgr.connect("100")
                self.assertHostRestored()

    def test_public_ip_failure_bounded(self):
        self.plat.ip_responses = [DIRECT_IP.encode(), OSError("timed out"), OSError("timed out")]
        with self.assertRaises(ConnectivityError):
            self.mgr.connect("100")
        self.assertEqual(len([c for c in self.plat.calls if c[0] == "http" and c[2]]), 2)

    def test_tunnel_not_carrying_traffic(self):
        self.plat.tunnel_ip = DIRECT_IP
        with self.assertRaises(ConnectivityError) as cm:
            self.mgr.connect("100")
        self.assertIn("not using the tunnel", str(cm.exception))

    def test_conflicting_default_route_refused(self):
        self.plat.default_iface = "utun3"
        with self.assertRaises(ConflictingVpnError):
            self.mgr.connect("100")
        self.assertEqual(self.commands("start"), [])

    def test_tunnel_started_by_user_blocks_and_is_untouched(self):
        self.plat.user_starts("101")
        with self.assertRaises(ConflictingVpnError) as cm:
            self.mgr.connect("100")
        self.assertIn("101", str(cm.exception))
        self.assertEqual(self.commands("start"), [])
        self.assertEqual(self.commands("stop"), [])
        self.assertEqual(self.plat.svc("101").status, "Connected")

    def test_connect_while_ready_is_invalid(self):
        self.mgr.connect("100")
        with self.assertRaises(InvalidTransition):
            self.mgr.connect("101")
        self.assertEqual(len(self.commands("start")), 1)

    def test_random_connect_tries_other_tunnels_bounded(self):
        self.plat.start_rc = 1
        with self.assertRaises(TunnelUpError):
            self.mgr.connect()
        self.assertEqual(len(self.commands("start")), 3)

    def test_no_privileged_commands(self):
        self.mgr.connect("100")
        self.mgr.disconnect()
        kinds = {c[0] for c in self.plat.calls}
        self.assertTrue(kinds <= {"snapshot", "status", "start", "stop", "http"})


class PublicIpTests(Base):
    def test_same_ip_recorded_and_accepted(self):
        self.mgr.connect("100")
        st = self.mgr.rotate("test")
        self.assertEqual(st.state, "READY")
        self.assertFalse(st.ip_changed)
        self.assertIn("SAME_PUBLIC_IP", self.events())
        self.assertEqual(len(self.commands("start")), 2)

    def test_changed_ip(self):
        self.mgr.connect("100")
        self.plat.tunnel_ip = "185.0.0.2"
        self.assertTrue(self.mgr.rotate("test").ip_changed)
        self.assertIn("Public IP changed: YES", self.console)


class LocationTests(Base):
    def test_location_shown_and_recorded(self):
        st = self.mgr.connect("100")
        self.assertEqual(st.location, "Miami, Florida, US")
        self.assertIn("Location: Miami, Florida, US", self.console)
        self.assertIn("Location: Miami, Florida, US", st.lines())
        self.assertEqual(self.store.sessions[st.session_id]["location"], "Miami, Florida, US")
        self.assertTrue(any(c[0] == "http" and "ipinfo.io" in c[1] for c in self.plat.calls))

    def test_ipinfo_down_falls_back_to_ipify(self):
        self.plat.city = None  # ipinfo.io unreachable; ipify still answers
        st = self.mgr.connect("100")
        self.assertEqual(st.state, "READY")
        self.assertEqual(st.public_ip, "185.0.0.1")
        self.assertIsNone(st.location)
        self.assertIn("Location: unknown", self.console)
        urls = [c[1] for c in self.plat.calls if c[0] == "http" and c[2]]
        self.assertEqual(urls[-2:], ["https://ipinfo.io/json", "https://api.ipify.org"])

    def test_parse_ip_response(self):
        from wg_manager import parse_ip_response
        self.assertEqual(parse_ip_response(b'{"ip": "37.19.221.244", "city": "Miami", "region": "Florida", '
                                           b'"country": "US", "org": "AS9009 M247"}'),
                         ("37.19.221.244", "Miami, Florida, US"))
        self.assertEqual(parse_ip_response(b"37.19.221.244\n"), ("37.19.221.244", None))
        self.assertEqual(parse_ip_response(b'{"ip": "37.19.221.244"}'), ("37.19.221.244", None))
        self.assertEqual(parse_ip_response(b'{"ip": "37.19.221.244", "city": "", "country": "US"}'),
                         ("37.19.221.244", "US"))
        for bad in (b'{"ip": "10.0.0.1", "city": "x"}', b'{"bogon": true}', b"[1, 2]", b"{not json",
                    b"<html>", b"x" * 5000):
            with self.subTest(bad=bad[:20]), self.assertRaises(PublicIpError):
                parse_ip_response(bad)


class TeardownTests(Base):
    def test_clean_disconnect(self):
        st = self.mgr.connect("100")
        self.mgr.disconnect()
        self.assertHostRestored()
        self.assertIs(self.mgr.state, S.DISCONNECTED)
        self.assertEqual(self.commands("stop"), [uuid(0)])
        row = self.store.sessions[st.session_id]
        self.assertEqual(row["teardown_outcome"], "clean")
        self.assertIsNotNone(row["ended_utc"])

    def test_disconnect_is_idempotent(self):
        self.mgr.connect("100")
        self.mgr.disconnect()
        self.mgr.disconnect()
        self.assertTrue(self.mgr.cleanup())
        self.assertEqual(len(self.commands("stop")), 1)

    def test_cleanup_without_connection(self):
        self.assertTrue(self.mgr.cleanup())
        self.assertEqual(self.commands("stop"), [])

    def test_service_that_will_not_stop_halts(self):
        self.mgr.connect("100")
        self.plat.stop_ignored = True
        with self.assertRaises(RestoreError) as cm:
            self.mgr.disconnect()
        self.assertIn("still Connected", str(cm.exception))
        self.assertTrue(self.mgr.halted)
        self.assertIs(self.mgr.state, S.ERROR)
        self.assertTrue(any('scutil --nc stop "100"' in line for line in self.console))
        self.mgr.active = None
        with self.assertRaises(HaltedError):
            self.mgr.connect("101")

    def test_slow_disconnect_is_waited_out_not_a_halt(self):
        # 2026-10-28 live: 87 never connected, then stayed "Disconnecting" ~20 s after stop.
        self.mgr.connect("100")
        self.plat.disconnecting_polls = 40  # 40 polls x 0.5 s = 20 s
        self.mgr.disconnect()
        self.assertFalse(self.mgr.halted)
        self.assertHostRestored()
        self.assertEqual(self.commands("stop").count(uuid(0)), 2)  # one more stop after 15 s
        notes = [e for e in self.store.events if e["event"] == "DISCONNECTED"][0]["notes"]
        self.assertTrue(any("still Disconnecting after 15s; stop sent again" in n for n in notes))

    def test_disconnect_that_never_finishes_halts(self):
        self.mgr.connect("100")
        self.plat.disconnecting_polls = 1000  # 500 s: far past 15 + 30 s
        with self.assertRaises(RestoreError) as cm:
            self.mgr.disconnect()
        self.assertIn("still Disconnecting after 45s", str(cm.exception))

    def test_interface_left_behind_halts(self):
        self.mgr.connect("100")
        self.plat.stop_leaves_interface = True
        with self.assertRaises(RestoreError) as cm:
            self.mgr.disconnect()
        self.assertIn("utun9 still exists", str(cm.exception))

    def test_dns_left_behind_halts(self):
        self.mgr.connect("100")
        self.plat.stop_leaves_dns = True
        with self.assertRaises(RestoreError) as cm:
            self.mgr.disconnect()
        self.assertIn("tunnel DNS servers are still in the system resolver", str(cm.exception))
        self.assertEqual(self.store.sessions[1]["teardown_outcome"], "restore_failed")

    def test_post_teardown_connectivity_failure_halts(self):
        self.mgr.connect("100")
        self.plat.direct_ok = False
        with self.assertRaises(RestoreError):
            self.mgr.disconnect()
        self.assertTrue(self.mgr.halted)

    def test_only_own_service_is_stopped(self):
        self.mgr.connect("100")
        self.mgr.disconnect()
        self.assertEqual(set(self.commands("stop")), {uuid(0)})


class RotationTests(Base):
    def test_rotate_picks_another_tunnel(self):
        self.mgr.connect()
        for _ in range(5):
            prev = self.mgr.get_status().config
            st = self.mgr.rotate("test")
            self.assertNotEqual(st.config, prev)
            self.assertEqual(st.state, "READY")
        self.assertEqual(self.events().count("ROTATION_STARTED"), 5)
        self.assertTrue(any(line.startswith("Previous tunnel: ") for line in self.console))

    def test_stop_before_replacement_and_one_tunnel_at_a_time(self):
        self.mgr.connect("100")
        self.mgr.rotate("test")
        kinds = [c[0] for c in self.plat.calls if c[0] in ("start", "stop")]
        self.assertEqual(kinds, ["start", "stop", "start"])
        self.assertEqual(sum(1 for s in self.plat.svcs.values() if s.status == "Connected"), 1)

    def test_concurrent_rotation_refused(self):
        self.mgr.connect("100")
        entered, release = threading.Event(), threading.Event()
        real_stop = self.plat.stop

        def slow_stop(u):
            entered.set()
            release.wait(5)
            return real_stop(u)

        self.plat.stop = slow_stop
        t = threading.Thread(target=self.mgr.rotate, args=("a",))
        t.start()
        self.assertTrue(entered.wait(5))
        with self.assertRaises(RotationInProgress):
            self.mgr.rotate("b")
        with self.assertRaises(RotationInProgress):
            self.mgr.connect()
        release.set()
        t.join(5)
        self.assertEqual(self.mgr.get_status().state, "READY")

    def test_interrupted_rotation_leaves_no_tunnel(self):
        self.mgr.connect("100")
        self.plat.start_raises = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.mgr.rotate("test")
        self.assertHostRestored()
        self.assertTrue(self.mgr.cleanup())
        self.assertIn("interrupted", self.mgr.last_failure)

    def test_soft_avoid_falls_back_when_everything_is_avoided(self):
        self.mgr.connect("100")
        self.assertEqual(self.mgr.rotate("x", avoid=[uuid(i) for i in range(3)]).state, "READY")
        self.assertEqual(self.mgr.config_ips[uuid(0)], "185.0.0.1")


class OrphanTests(Base):
    def crash_with_tunnel(self) -> int:
        st = self.mgr.connect("100")
        self.mgr = self.make_manager()  # process died; WireGuard app keeps the tunnel up
        return st.session_id

    def test_own_tunnel_stopped_on_startup(self):
        sid = self.crash_with_tunnel()
        self.assertEqual(self.mgr.recover_orphans(), "recovered")
        self.assertHostRestored()
        self.assertEqual(self.store.sessions[sid]["teardown_outcome"], "recovered_orphan")
        self.assertIn("ORPHAN_DETECTED", self.events())
        self.assertEqual(self.mgr.connect().state, "READY")

    def test_own_tunnel_already_down(self):
        sid = self.crash_with_tunnel()
        self.plat.drop_tunnel()
        self.assertEqual(self.mgr.recover_orphans(), "recovered")
        self.assertEqual(self.commands("stop"), [])  # nothing left to stop
        self.assertEqual(self.store.sessions[sid]["teardown_outcome"], "recovered_orphan")

    def test_recovered_even_if_tunnel_was_deleted_from_the_app_list(self):
        self.crash_with_tunnel()
        self.plat.svcs[uuid(0)].name = "renamed in the WireGuard app"
        self.assertEqual(self.mgr.recover_orphans(), "recovered")  # stopped by UUID
        self.assertHostRestored()

    def test_foreign_tunnel_is_not_an_orphan(self):
        self.plat.user_starts("102")
        self.assertEqual(self.mgr.recover_orphans(), "clean")
        self.assertEqual(self.plat.svc("102").status, "Connected")
        self.assertEqual(self.commands("stop"), [])

    def test_legacy_wg_quick_session_closed(self):
        self.store.start_vpn_session(config_name="45.conf", config_path="/x/45.conf", wg_name="duowg", endpoint="e",
                                     copy_path="/t/duowg.conf", started=self.clock.wall(), baseline_json="{}",
                                     reason="start")
        self.assertEqual(self.mgr.recover_orphans(), "legacy")
        self.assertEqual(self.store.sessions[1]["teardown_outcome"], "legacy_unverified")
        self.assertEqual(self.commands("stop"), [])

    def test_older_open_sessions_superseded(self):
        self.store.start_vpn_session(config_name="old", config_path="", wg_name="101",
                                     endpoint="e", copy_path="", started=self.clock.wall(),
                                     baseline_json=self.plat.snapshot().to_json(), reason=None, service_id=uuid(1))
        sid = self.crash_with_tunnel()
        self.mgr.recover_orphans()
        self.assertEqual(self.store.sessions[1]["teardown_outcome"], "superseded")
        self.assertEqual(self.store.sessions[sid]["teardown_outcome"], "recovered_orphan")
        self.assertEqual(self.store.open_vpn_sessions(), [])

    def test_clean_start(self):
        self.assertEqual(self.mgr.recover_orphans(), "clean")
        self.assertEqual(self.events(), [])


class HealthTests(Base):
    def test_healthy(self):
        self.mgr.connect("100")
        self.assertTrue(self.mgr.health_check())

    def test_lost_tunnel_marks_error(self):
        self.mgr.connect("100")
        self.plat.drop_tunnel()
        self.assertFalse(self.mgr.health_check())
        self.assertIs(self.mgr.state, S.ERROR)
        self.assertIn("CONNECTION_LOST", self.events())

    def test_connected_but_no_traffic_is_lost(self):
        self.mgr.connect("100")
        self.assertTrue(self.mgr.health_check())  # status alone still says Connected
        self.plat.dead_endpoints.add(endpoint(0))
        self.assertTrue(self.mgr.health_check())
        self.assertFalse(self.mgr.health_check(traffic=True))
        self.assertIs(self.mgr.state, S.ERROR)
        lost = [e for e in self.store.events if e["event"] == "CONNECTION_LOST"][0]
        self.assertIn("no traffic through the tunnel", lost["detail"])

    def test_traffic_check_passes_on_working_tunnel(self):
        self.mgr.connect("100")
        self.assertTrue(self.mgr.health_check(traffic=True))
        self.assertIs(self.mgr.state, S.READY)

    def test_live_status_reports_service_state(self):
        self.mgr.connect("100")
        self.plat.drop_tunnel()
        self.assertEqual(self.mgr.get_status(live=True).handshake, "NONE (service Disconnected)")


class GateTests(Base):
    def gate(self, policy=RotationPolicy(), backoff=(30.0, 60.0)) -> NetworkGate:
        g = NetworkGate(self.mgr, policy, clock=self.clock, reconnect_backoff=backoff)
        g.start()
        return g

    def test_permitted_when_ready(self):
        g = self.gate()
        self.assertTrue(g.permitted())
        self.assertTrue(g.before_cycle())
        self.assertEqual(g.session_id, 1)

    def test_cycle_rotation_at_boundary(self):
        g = self.gate(RotationPolicy("cycles", 2))
        for _ in range(2):
            self.assertTrue(g.before_cycle())
            g.after_cycle()
        self.assertEqual(self.events().count("ROTATION_STARTED"), 0)
        self.assertTrue(g.before_cycle())
        self.assertEqual(self.events().count("ROTATION_STARTED"), 1)
        self.assertEqual(g.session_id, 2)

    def test_time_rotation(self):
        g = self.gate(RotationPolicy("minutes", 15))
        self.clock.advance(14 * 60)
        self.assertTrue(g.before_cycle())
        self.assertEqual(self.events().count("ROTATION_STARTED"), 0)
        self.clock.advance(60)
        self.assertTrue(g.before_cycle())
        self.assertEqual(self.events().count("ROTATION_STARTED"), 1)

    def test_failed_rotation_pauses_then_reconnects(self):
        g = self.gate(RotationPolicy("cycles", 1))
        g.after_cycle()
        self.plat.start_rc = 1
        self.assertFalse(g.before_cycle())
        self.assertFalse(g.permitted())
        self.assertEqual(g.retry_after_s, 30.0)
        self.assertFalse(g.before_cycle())
        starts = len(self.commands("start"))
        self.plat.start_rc = 0
        self.clock.advance(31)
        self.assertTrue(g.before_cycle())
        self.assertEqual(len(self.commands("start")), starts + 1)

    def test_reconnect_attempts_bounded_then_halt(self):
        g = self.gate(backoff=(30.0, 60.0))
        self.plat.drop_tunnel()
        g.after_cycle(network_errors=1)
        self.assertFalse(g.permitted())
        self.plat.start_rc = 1
        self.assertFalse(g.before_cycle())
        self.clock.advance(31)
        self.assertFalse(g.before_cycle())
        self.clock.advance(61)
        with self.assertRaises(HaltedError):
            g.before_cycle()
        self.assertTrue(self.mgr.halted)

    def test_dead_tunnel_replaced_by_another_before_next_operation(self):
        g = self.gate()
        first = self.mgr.active.service_id
        self.plat.dead_endpoints.add(self.mgr.active.endpoint)
        g.after_cycle(network_errors=2)
        self.assertFalse(g.permitted())  # no operation runs through the dead tunnel
        self.assertIn(first, g.dead_configs)
        self.assertTrue(g.before_cycle())
        self.assertTrue(g.permitted())
        self.assertNotEqual(self.mgr.active.service_id, first)
        self.assertEqual(self.mgr.get_status().state, "READY")

    def test_network_errors_on_a_working_tunnel_keep_it(self):
        g = self.gate()
        sid = g.session_id
        g.after_cycle(network_errors=2)  # e.g. Apple was slow; the tunnel itself is fine
        self.assertTrue(g.before_cycle())
        self.assertEqual(g.session_id, sid)
        self.assertEqual(g.dead_configs, set())

    def test_restore_failure_during_rotation_halts(self):
        g = self.gate(RotationPolicy("cycles", 1))
        g.after_cycle()
        self.plat.stop_ignored = True
        with self.assertRaises(HaltedError):
            g.before_cycle()


class ProcessLockTests(unittest.TestCase):
    def test_second_holder_refused(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.lock"
            with ProcessLock(path):
                with self.assertRaises(LockHeldError):
                    ProcessLock(path).acquire()
            again = ProcessLock(path)  # free again once the first holder releases it
            again.acquire()
            again.release()


if __name__ == "__main__":
    unittest.main()
