"""macOS platform layer: scutil --nc parsing, argv construction, host-state parsers."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest import mock

from helpers import quiet_logs
from wg_errors import InterfaceError
from wg_macos import (
    SCUTIL, MacPlatform, NetworkSnapshot, Result, SubprocessRunner, parse_ifconfig_inet, parse_nc_list,
    parse_nc_show, parse_nc_status, parse_route_get, parse_scutil_dns,
)

FIX = Path(__file__).parent / "fixtures" / "wg"
UUID_88 = "EF0DCDE6-40CD-4F20-29DA-4B32B2146442"


def setUpModule():
    quiet_logs()


def fixture(name: str) -> str:
    return (FIX / name).read_text()


class ScutilParserTests(unittest.TestCase):
    def test_nc_list_from_this_mac(self):
        services = parse_nc_list(fixture("nc_list_wireguard.txt"))
        wg = [s for s in services if s.is_wireguard]
        self.assertEqual(sorted(s.name for s in wg), sorted(["106", "89", "88", "138", "87", "44", "46", "115", "47", "45"]))
        s88 = next(s for s in wg if s.name == "88")
        self.assertEqual((s88.uuid, s88.status, s88.active), (UUID_88, "Disconnected", False))
        others = {s.name for s in services if not s.is_wireguard}
        self.assertTrue({"ProtonVPN", "Tailscale"} <= others)

    def test_nc_list_connected_and_odd_names(self):
        text = ('* (Connected)      EF0DCDE6-40CD-4F20-29DA-4B32B2146442 VPN (com.wireguard.macos) "NL #7 (free)"  '
                '[VPN:com.wireguard.macos]\n')
        (s,) = parse_nc_list(text)
        self.assertEqual((s.name, s.status, s.active, s.is_wireguard), ("NL #7 (free)", "Connected", True, True))

    def test_nc_show_remote_address(self):
        self.assertEqual(parse_nc_show(fixture("nc_show_88.txt")), "138.199.50.139:51820")
        self.assertIsNone(parse_nc_show("No service\n"))

    def test_nc_status(self):
        self.assertEqual(parse_nc_status(fixture("nc_status_disconnected.txt")), "Disconnected")
        self.assertEqual(parse_nc_status("Connected\nExtended Status <dictionary> {\n"), "Connected")
        self.assertEqual(parse_nc_status(""), "Invalid")
        self.assertEqual(parse_nc_status("No service\n"), "Invalid")


class HostParserTests(unittest.TestCase):
    def test_route_default(self):
        r = parse_route_get(fixture("route_default.txt"))
        self.assertEqual((r.destination, r.interface), ("default", "en0"))

    def test_route_empty(self):
        r = parse_route_get("route: writing to routing socket: not in table\n")
        self.assertEqual((r.destination, r.gateway, r.interface), (None, None, None))

    def test_ifconfig_inet(self):
        self.assertEqual(parse_ifconfig_inet(fixture("ifconfig_utun.txt")), ["10.2.0.2", "fe80::1", "2a07:b944::2:2"])

    def test_scutil_dns_ignores_scoped(self):
        self.assertEqual(parse_scutil_dns(fixture("scutil_dns.txt")), ["10.2.0.1"])

    def test_snapshot_round_trip_and_legacy_baseline(self):
        snap = NetworkSnapshot("en0", "192.168.0.1", ["en0"], [], ["1.1.1.1"])
        self.assertEqual(NetworkSnapshot.from_json(snap.to_json()), snap)
        self.assertTrue(snap.default_is_physical)
        self.assertFalse(NetworkSnapshot("utun3", None, [], []).default_is_physical)
        # A baseline recorded by the former wg-quick mode (extra keys, no resolvers) still loads.
        old = '{"default_interface": "en0", "default_gateway": "g", "interfaces": [], "wg_names": {}, "dns": {}}'
        self.assertEqual(NetworkSnapshot.from_json(old).resolvers, [])


class FakeRunner:
    def __init__(self, outputs: dict[tuple, Result] | None = None) -> None:
        self.outputs = outputs or {}
        self.calls: list[tuple] = []

    def run(self, argv, *, timeout) -> Result:
        self.calls.append(tuple(argv))
        return self.outputs.get(tuple(argv), Result(tuple(argv), 0, ""))


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.runner = FakeRunner()
        self.plat = MacPlatform(self.runner)

    def test_start_stop_status_by_uuid_without_sudo(self):
        self.runner.outputs[(SCUTIL, "--nc", "status", UUID_88)] = Result((), 0, "Connected\n")
        self.plat.start(UUID_88)
        self.assertEqual(self.plat.status(UUID_88), "Connected")
        self.plat.stop(UUID_88)
        self.assertEqual(self.runner.calls, [(SCUTIL, "--nc", "start", UUID_88), (SCUTIL, "--nc", "status", UUID_88),
                                             (SCUTIL, "--nc", "stop", UUID_88)])
        self.assertFalse(any("sudo" in a for argv in self.runner.calls for a in argv))

    def test_names_are_never_passed_to_scutil(self):
        for bad in ("88", '"88"; rm -rf /', "", "41ff98c2-f16b-41c5-b8d6-d5e4c2e63ac2x"):
            with self.subTest(bad=bad), self.assertRaises(InterfaceError):
                self.plat.start(bad)
        self.assertEqual(self.runner.calls, [])

    def test_failed_status_is_invalid(self):
        self.runner.outputs[(SCUTIL, "--nc", "status", UUID_88)] = Result((), 1, "", "No service")
        self.assertEqual(self.plat.status(UUID_88), "Invalid")

    def test_wireguard_services_and_endpoint(self):
        self.runner.outputs[(SCUTIL, "--nc", "list")] = Result((), 0, fixture("nc_list_wireguard.txt"))
        self.runner.outputs[(SCUTIL, "--nc", "show", UUID_88)] = Result((), 0, fixture("nc_show_88.txt"))
        self.assertEqual(len(self.plat.wireguard_services()), 10)
        self.assertEqual(self.plat.service_endpoint(UUID_88), "138.199.50.139:51820")


class RunnerTests(unittest.TestCase):
    def test_no_shell_file_capture_and_no_stdin(self):
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as run:
            res = SubprocessRunner().run([SCUTIL, "--nc", "list"], timeout=5)
        self.assertTrue(res.ok)
        kwargs = run.call_args.kwargs
        self.assertNotIn("shell", kwargs)
        self.assertNotEqual(kwargs["stdout"], subprocess.PIPE)
        self.assertEqual((kwargs["timeout"], kwargs["stdin"]), (5, subprocess.DEVNULL))

    def test_timeout_reported(self):
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("x", 1)):
            res = SubprocessRunner().run(["/x"], timeout=1)
        self.assertTrue(res.timed_out)
        self.assertFalse(res.ok)


if __name__ == "__main__":
    unittest.main()
