"""macOS platform layer: WireGuard-app tunnels controlled through `scutil --nc`.

Tunnels are the WireGuard app's system VPN services (NetworkExtension
provider `com.wireguard.macos`). Starting and stopping them needs no
privileges; macOS applies and removes the tunnel's routes and DNS itself.
Everything here is a read or one of `scutil --nc start/stop` for a given
service UUID. Commands are argv lists behind the `Runner` protocol so tests
can script every output; nothing uses a shell or sudo.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import subprocess
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol, Sequence

from wg_errors import InterfaceError, SubprocessError

log = logging.getLogger("iphone_monitor")

ROUTE = "/sbin/route"
IFCONFIG = "/sbin/ifconfig"
SCUTIL = "/usr/sbin/scutil"
MAX_OUTPUT_BYTES = 1024 * 1024
WIREGUARD_PROVIDER = "com.wireguard.macos"

_UUID = re.compile(r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$")
_PHYSICAL = re.compile(r"^(en|bridge)\d+$")
_NC_LINE = re.compile(
    r"^\*?\s*\((?P<status>[^)]+)\)\s+(?P<uuid>[0-9A-Fa-f-]{36})\s+(?P<kind>.*?)\s+\"(?P<name>[^\"]+)\"")
STATUSES = ("Connected", "Connecting", "Disconnected", "Disconnecting", "Reasserting", "Invalid")


@dataclass(frozen=True)
class Result:
    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


class Runner(Protocol):
    def run(self, argv: Sequence[str], *, timeout: float) -> Result: ...


class SubprocessRunner:
    """Runs argv lists without privileges. Output goes to temporary files, not pipes,
    so a background process that inherits the descriptors can't keep run() waiting."""

    def run(self, argv: Sequence[str], *, timeout: float) -> Result:
        full = list(argv)
        log.debug("run", extra={"fields": {"argv": full[:6]}})
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            try:
                cp = subprocess.run(full, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                    timeout=timeout, check=False)
                rc, timed_out = cp.returncode, False
            except subprocess.TimeoutExpired:
                rc, timed_out = -1, True
            except OSError as exc:
                raise SubprocessError(f"could not run {Path(full[0]).name}: {exc.strerror}") from None
            return Result(tuple(full), rc, _read(out), _read(err), timed_out)


def _read(f: Any) -> str:
    f.seek(0)
    return f.read(MAX_OUTPUT_BYTES).decode("utf-8", "replace")


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class VpnService:
    uuid: str
    name: str
    kind: str  # e.g. "VPN (com.wireguard.macos)"
    status: str

    @property
    def is_wireguard(self) -> bool:
        return f"({WIREGUARD_PROVIDER})" in self.kind

    @property
    def active(self) -> bool:
        return self.status in ("Connected", "Connecting", "Reasserting")


@dataclass(frozen=True)
class Route:
    destination: str | None
    gateway: str | None
    interface: str | None


@dataclass
class NetworkSnapshot:
    default_interface: str | None
    default_gateway: str | None
    interfaces: list[str]
    connected_vpns: list[str]  # names of VPN services that are connected or connecting
    resolvers: list[str] = field(default_factory=list)

    @property
    def default_is_physical(self) -> bool:
        return bool(self.default_interface and _PHYSICAL.match(self.default_interface))

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "NetworkSnapshot":
        data = json.loads(text)
        return cls(**{k: data.get(k, [] if k in ("interfaces", "connected_vpns", "resolvers") else None)
                      for k in cls.__dataclass_fields__})


# --------------------------------------------------------------------------
# Output parsers (pure)
# --------------------------------------------------------------------------


def parse_nc_list(text: str) -> list[VpnService]:
    """Services from `scutil --nc list`."""
    out = []
    for line in text.splitlines():
        mt = _NC_LINE.match(line.strip())
        if mt:
            out.append(VpnService(mt.group("uuid").upper(), mt.group("name"), mt.group("kind").strip(),
                                  mt.group("status").strip()))
    return out


def parse_nc_show(text: str) -> str | None:
    """`RemoteAddress` (the WireGuard endpoint, host:port) from `scutil --nc show`."""
    for line in text.splitlines():
        key, sep, value = line.strip().partition(" : ")
        if sep and key == "RemoteAddress":
            return value.strip() or None
    return None


def parse_nc_status(text: str) -> str:
    """First line of `scutil --nc status`: Connected, Connecting, Disconnected, ... or Invalid."""
    first = text.strip().splitlines()[0].strip() if text.strip() else ""
    return first if first in STATUSES else "Invalid"


def parse_route_get(text: str) -> Route:
    """Destination, gateway and interface from `route -n get <dest>`."""
    found: dict[str, str | None] = {"destination": None, "gateway": None, "interface": None}
    for line in text.splitlines():
        key, _, value = line.strip().partition(":")
        if key in found:
            found[key] = value.strip() or None
    return Route(found["destination"], found["gateway"], found["interface"])


def parse_ifconfig_list(text: str) -> list[str]:
    return text.split()


def parse_ifconfig_inet(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in ("inet", "inet6"):
            out.append(parts[1].split("%", 1)[0])
    return out


def parse_scutil_dns(text: str) -> list[str]:
    """Nameservers of the default (non-scoped) resolvers in `scutil --dns`."""
    out: list[str] = []
    section = text.split("DNS configuration (for scoped queries)", 1)[0]
    for line in section.splitlines():
        line = line.strip()
        if line.startswith("nameserver["):
            ns = line.split(":", 1)[1].strip()
            if ns not in out:
                out.append(ns)
    return out


# --------------------------------------------------------------------------
# Platform
# --------------------------------------------------------------------------


class MacPlatform:
    """Reads macOS network state and starts/stops WireGuard-app services by UUID."""

    QUERY_TIMEOUT = 10.0

    def __init__(self, runner: Runner | None = None) -> None:
        self.runner = runner or SubprocessRunner()

    def _q(self, argv: Sequence[str]) -> Result:
        return self.runner.run(argv, timeout=self.QUERY_TIMEOUT)

    # -- services ----------------------------------------------------------

    def services(self) -> list[VpnService]:
        return parse_nc_list(self._q([SCUTIL, "--nc", "list"]).stdout)

    def wireguard_services(self) -> list[VpnService]:
        return [s for s in self.services() if s.is_wireguard]

    def service_endpoint(self, uuid: str) -> str | None:
        return parse_nc_show(self._q([SCUTIL, "--nc", "show", _check_uuid(uuid)]).stdout)

    def status(self, uuid: str) -> str:
        r = self._q([SCUTIL, "--nc", "status", _check_uuid(uuid)])
        return parse_nc_status(r.stdout) if r.ok else "Invalid"

    def start(self, uuid: str) -> Result:
        return self._q([SCUTIL, "--nc", "start", _check_uuid(uuid)])

    def stop(self, uuid: str) -> Result:
        return self._q([SCUTIL, "--nc", "stop", _check_uuid(uuid)])

    # -- host state --------------------------------------------------------

    def snapshot(self) -> NetworkSnapshot:
        default = self.route_get("default")
        vpns = [s.name for s in self.services() if s.active]
        return NetworkSnapshot(default.interface, default.gateway, self.interfaces(), vpns, self.resolvers())

    def interfaces(self) -> list[str]:
        return parse_ifconfig_list(self._q([IFCONFIG, "-l"]).stdout)

    def interface_addresses(self, iface: str) -> list[str] | None:
        r = self._q([IFCONFIG, iface])
        return parse_ifconfig_inet(r.stdout) if r.ok else None

    def route_get(self, dest: str) -> Route:
        r = self._q([ROUTE, "-n", "get", dest])
        return parse_route_get(r.stdout) if r.ok else Route(None, None, None)

    def resolvers(self) -> list[str]:
        return parse_scutil_dns(self._q([SCUTIL, "--dns"]).stdout)

    def resolve_host(self, host: str, timeout: float) -> bool:
        """Whether `host` resolves within `timeout` (getaddrinfo itself has no timeout)."""
        done = threading.Event()
        ok: list[bool] = [False]

        def work() -> None:
            try:
                ok[0] = bool(socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
            except OSError:
                ok[0] = False
            finally:
                done.set()

        threading.Thread(target=work, name="dns-probe", daemon=True).start()
        return done.wait(timeout) and ok[0]


def _check_uuid(uuid: str) -> str:
    if not _UUID.match(uuid):
        raise InterfaceError(f"not a VPN service UUID: {uuid!r}")
    return uuid
