"""Fakes for WireGuard tests: scripted WireGuard-app services on a fake macOS host.

FakePlatform models what `scutil --nc start/stop` does with a WireGuard-app
tunnel: the service goes Connecting, then Connected, a new utunN carries the
traffic and the tunnel's DNS joins the system resolver; stopping reverses it.
Knobs make each step fail. Nothing here runs a command or touches the real
network.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import helpers  # noqa: F401  (puts the project root on sys.path)
from wg_macos import NetworkSnapshot, Result, Route, VpnService

DIRECT_IP = "93.184.215.14"  # must be is_global; documentation ranges are rejected
GATEWAY = "192.168.0.1"
T0 = datetime(2026, 10, 27, 13, 0, 0, tzinfo=timezone.utc)
WG_KIND = "VPN (com.wireguard.macos)"


def endpoint(n: int) -> str:
    return f"203.0.113.{10 + n}:51820"


def uuid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.wall_t = T0

    def __call__(self) -> float:
        return self.t

    def wall(self) -> datetime:
        return self.wall_t + timedelta(seconds=self.t - 1000.0)

    def sleep(self, s: float) -> None:
        self.t += s

    def advance(self, s: float) -> None:
        self.t += s


class FakeService:
    def __init__(self, n: int, name: str | None = None, ep: str | None = None, kind: str = WG_KIND) -> None:
        self.uuid = uuid(n)
        self.name = name if name is not None else str(100 + n)
        self.endpoint = ep if ep is not None else endpoint(n)
        self.kind = kind
        self.status = "Disconnected"
        self.countdown = 0


class FakePlatform:
    def __init__(self, clock: FakeClock | None = None, services: int = 10) -> None:
        self.clock = clock or FakeClock()
        self.svcs: dict[str, FakeService] = {}
        for i in range(services):
            self.add_service(FakeService(i))
        self.add_service(FakeService(99, name="ProtonVPN", ep="", kind="VPN (ch.protonvpn.mac)"))
        self.ifaces = ["lo0", "en0", "utun0", "utun1", "utun2"]
        self.default_iface = "en0"
        self.resolver_list = ["192.168.0.1"]
        self.tunnel: dict[str, Any] | None = None
        self.calls: list[tuple] = []
        self._next_utun = 9
        # failure knobs
        self.start_rc = 0
        self.connect_after_polls = 1  # status() calls while Connecting before it reports Connected
        self.never_connects = False
        self.goes_invalid = False
        self.start_raises: BaseException | None = None
        self.probe_via_tunnel = True
        self.endpoint_via_tunnel = False
        self.tunnel_dns_active = True
        self.iface_address = "10.2.0.2"
        self.dns_resolves = True
        self.stop_ignored = False  # service stays Connected after stop
        self.disconnecting_polls = 0  # status() calls a stopped service reports "Disconnecting" first
        self.stop_leaves_interface = False
        self.stop_leaves_dns = False
        self.tunnel_ip = "185.0.0.1"
        self.ip_by_endpoint: dict[str, str] = {}  # endpoint "host:port" -> public IP through that tunnel
        self.city = "Miami"  # what the fake ipinfo.io says; None: ipinfo is down (ipify still works)
        self.ip_responses: list[bytes | Exception] = []  # consumed first, then tunnel/direct IP
        self.direct_ok = True
        self.dead_endpoints: set[str] = set()  # these tunnels stay Connected but pass no traffic (seen live)

    def add_service(self, svc: FakeService) -> FakeService:
        self.svcs[svc.uuid] = svc
        return svc

    def svc(self, name: str) -> FakeService:
        return next(s for s in self.svcs.values() if s.name == name)

    # -- HTTP --------------------------------------------------------------

    def http_get(self, url: str, timeout: float, max_bytes: int) -> bytes:
        self.calls.append(("http", url, self.tunnel is not None))
        if self.ip_responses:
            r = self.ip_responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        if self.tunnel is not None:
            if self.tunnel["endpoint"] in self.dead_endpoints:
                raise OSError("timed out")
            ip = self.ip_by_endpoint.get(self.tunnel["endpoint"], self.tunnel_ip)
        elif not self.direct_ok:
            raise OSError("network down")
        else:
            ip = DIRECT_IP
        if "ipinfo.io" in url:
            if self.city is None:
                raise OSError("ipinfo unavailable")
            return json.dumps({"ip": ip, "city": self.city, "region": "Florida", "country": "US",
                               "org": "AS9009 M247 Europe SRL"}).encode()
        return ip.encode()

    # -- services ----------------------------------------------------------

    def wireguard_services(self) -> list[VpnService]:
        return [VpnService(s.uuid, s.name, s.kind, s.status) for s in self.svcs.values() if "wireguard" in s.kind]

    def service_endpoint(self, uuid: str) -> str | None:
        return self.svcs[uuid].endpoint or None

    def status(self, uuid: str) -> str:
        self.calls.append(("status", uuid))
        s = self.svcs.get(uuid)
        if s is None:
            return "Invalid"
        if s.status == "Disconnecting":
            s.countdown -= 1
            if s.countdown <= 0:
                self._finish_stop(s)
            return s.status
        if s.status == "Connecting":
            if self.goes_invalid:
                s.status = "Invalid"
            elif not self.never_connects:
                s.countdown -= 1
                if s.countdown <= 0:
                    s.status = "Connected"
                    self._create(s)
        return s.status

    def start(self, uuid: str) -> Result:
        self.calls.append(("start", uuid))
        if self.start_raises is not None:
            exc, self.start_raises = self.start_raises, None
            s = self.svcs[uuid]
            s.status = "Connected"
            self._create(s)  # interrupted after the tunnel came up
            raise exc
        if self.start_rc:
            return Result(("scutil",), self.start_rc, "", "No service\n")
        s = self.svcs[uuid]
        s.status = "Connecting"
        s.countdown = self.connect_after_polls
        return Result(("scutil",), 0)

    def stop(self, uuid: str) -> Result:
        self.calls.append(("stop", uuid))
        s = self.svcs[uuid]
        if self.stop_ignored:
            return Result(("scutil",), 0)
        if self.disconnecting_polls and s.status != "Disconnecting":
            s.status, s.countdown = "Disconnecting", self.disconnecting_polls
        if s.status == "Disconnecting":
            return Result(("scutil",), 0)  # it finishes later, in status()
        self._finish_stop(s)
        return Result(("scutil",), 0)

    def _finish_stop(self, s: FakeService) -> None:
        s.status = "Disconnected"
        t = self.tunnel
        if t is not None and t["uuid"] == s.uuid:
            if not self.stop_leaves_interface:
                self.ifaces.remove(t["iface"])
            if not self.stop_leaves_dns:
                self.tunnel = None
            else:
                t["routes"] = False

    def _create(self, s: FakeService) -> None:
        iface = f"utun{self._next_utun}"
        self._next_utun += 1
        self.ifaces.append(iface)
        self.tunnel = {"uuid": s.uuid, "iface": iface, "endpoint": s.endpoint, "routes": True}

    def user_starts(self, name: str) -> None:
        """Someone starts a tunnel in the WireGuard app, outside the application."""
        s = self.svc(name)
        s.status = "Connected"
        self._create(s)

    def drop_tunnel(self) -> None:
        """The tunnel goes away by itself (extension crash, network change)."""
        t = self.tunnel
        assert t is not None
        self.svcs[t["uuid"]].status = "Disconnected"
        self.ifaces.remove(t["iface"])
        self.tunnel = None

    # -- host state --------------------------------------------------------

    def snapshot(self) -> NetworkSnapshot:
        self.calls.append(("snapshot",))
        active = [s.name for s in self.svcs.values() if s.status in ("Connected", "Connecting")]
        return NetworkSnapshot(self.default_iface, GATEWAY, list(self.ifaces), active, self.resolvers())

    def interfaces(self) -> list[str]:
        return list(self.ifaces)

    def interface_addresses(self, iface: str) -> list[str] | None:
        if iface not in self.ifaces:
            return None
        return [self.iface_address] if self.tunnel and self.tunnel["iface"] == iface else []

    def route_get(self, dest: str) -> Route:
        t = self.tunnel
        if dest != "default" and t and t.get("routes") and self.probe_via_tunnel:
            if dest != t["endpoint"].split(":")[0] or self.endpoint_via_tunnel:
                return Route("0.0.0.0", t["iface"], t["iface"])
        return Route("default", GATEWAY, self.default_iface)

    def resolvers(self) -> list[str]:
        if self.tunnel and self.tunnel_dns_active:
            return ["2a07:b944::2:1", "10.2.0.1"]
        return list(self.resolver_list)

    def resolve_host(self, host: str, timeout: float) -> bool:
        if self.tunnel is not None:
            return self.dns_resolves and self.tunnel["endpoint"] not in self.dead_endpoints
        return self.direct_ok


class Capture(logging.Handler):
    """Collects formatted log output, including key=value fields."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        import iphone_monitor
        self.setFormatter(iphone_monitor.KeyValueFormatter("%(levelname)s %(message)s"))
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))
