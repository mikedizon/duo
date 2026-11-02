"""WireGuard connection lifecycle: selection, connection, verification,
rotation, teardown and recovery.

Application-independent: it knows nothing about what the caller does over
the network. The caller asks `NetworkGate.before_cycle()` whether network
operations are permitted, and rotation happens only inside that call, which
the caller makes between operations.

Tunnels are the WireGuard app's system VPN services (see `wg_macos`): every
tunnel imported into the WireGuard app is eligible, and the list is re-read
each time a new tunnel is chosen, so tunnels added while running are picked
up. Tunnels are controlled by service UUID, and the application stops only a
tunnel it started itself. macOS applies and removes the tunnel's routes and
DNS; this module only verifies them.
"""

from __future__ import annotations

import enum
import fcntl
import ipaddress
import json
import logging
import os
import random
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any, Callable, Iterable, Protocol, Sequence

from wg_errors import (
    ConflictingVpnError, ConnectivityError, DnsError, HaltedError, InterfaceError, InvalidTransition,
    LockHeldError, NoConfigsError, NotImportedError, PublicIpError, RestoreError, RotationInProgress,
    RoutingError, TunnelUpError, WireGuardError,
)
from wg_macos import NetworkSnapshot, Result, Route, VpnService

log = logging.getLogger("iphone_monitor")


# --------------------------------------------------------------------------
# State machine
# --------------------------------------------------------------------------


class ConnState(enum.Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    VERIFYING = "VERIFYING"
    READY = "READY"
    ROTATING = "ROTATING"
    DISCONNECTING = "DISCONNECTING"
    ERROR = "ERROR"


S = ConnState
ALLOWED: dict[ConnState, frozenset[ConnState]] = {
    S.DISCONNECTED: frozenset({S.CONNECTING, S.DISCONNECTING}),  # DISCONNECTING: orphan recovery
    S.CONNECTING: frozenset({S.CONNECTED, S.ERROR, S.DISCONNECTING}),
    S.CONNECTED: frozenset({S.VERIFYING, S.ERROR, S.DISCONNECTING}),
    S.VERIFYING: frozenset({S.READY, S.ERROR, S.DISCONNECTING}),
    S.READY: frozenset({S.VERIFYING, S.ROTATING, S.DISCONNECTING, S.ERROR}),
    S.ROTATING: frozenset({S.DISCONNECTING, S.ERROR}),
    S.DISCONNECTING: frozenset({S.DISCONNECTED, S.ERROR}),
    S.ERROR: frozenset({S.DISCONNECTING, S.DISCONNECTED}),
}


class StateMachine:
    def __init__(self, on_change: Callable[[ConnState, ConnState], None] | None = None) -> None:
        self.state = S.DISCONNECTED
        self.on_change = on_change

    def to(self, new: ConnState) -> None:
        if new not in ALLOWED[self.state]:
            raise InvalidTransition(f"{self.state.value} -> {new.value} is not allowed")
        old, self.state = self.state, new
        if self.on_change:
            self.on_change(old, new)


# --------------------------------------------------------------------------
# Rotation policy
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RotationPolicy:
    kind: str = "none"  # none | minutes | cycles
    value: float = 0

    def __post_init__(self) -> None:
        if self.kind not in ("none", "minutes", "cycles"):
            raise ValueError(f"unknown rotation kind {self.kind!r}")
        if self.kind != "none" and not (0 < self.value < float("inf")):
            raise ValueError("rotation value must be positive and finite")

    @classmethod
    def from_options(cls, minutes: float | None, cycles: int | None) -> "RotationPolicy":
        if minutes is not None and cycles is not None:
            raise ValueError("choose time-based or cycle-based rotation, not both")
        if minutes is not None:
            return cls("minutes", minutes)
        if cycles is not None:
            return cls("cycles", cycles)
        return cls()

    def due(self, connected_monotonic: float, now_monotonic: float, cycles: int) -> str | None:
        """The rotation reason if rotation is due, else None."""
        if self.kind == "minutes" and now_monotonic - connected_monotonic >= self.value * 60:
            return f"time:{self.value:g}min"
        if self.kind == "cycles" and cycles >= self.value:
            return f"cycles:{int(self.value)}"
        return None

    def describe(self) -> str:
        return {"none": "no rotation", "minutes": f"every {self.value:g} min",
                "cycles": f"every {int(self.value)} cycles"}[self.kind]


# --------------------------------------------------------------------------
# Collaborator protocols
# --------------------------------------------------------------------------


class Platform(Protocol):
    def wireguard_services(self) -> list[VpnService]: ...
    def service_endpoint(self, uuid: str) -> str | None: ...
    def status(self, uuid: str) -> str: ...
    def start(self, uuid: str) -> Result: ...
    def stop(self, uuid: str) -> Result: ...
    def snapshot(self) -> NetworkSnapshot: ...
    def interfaces(self) -> list[str]: ...
    def interface_addresses(self, iface: str) -> list[str] | None: ...
    def route_get(self, dest: str) -> Route: ...
    def resolvers(self) -> list[str]: ...
    def resolve_host(self, host: str, timeout: float) -> bool: ...


class MetadataStore(Protocol):
    def start_vpn_session(self, *, config_name: str, config_path: str, wg_name: str, endpoint: str,
                          copy_path: str, started: datetime, baseline_json: str, reason: str | None,
                          service_id: str | None = None) -> int: ...
    def update_vpn_session(self, session_id: int, **fields: Any) -> None: ...
    def record_vpn_event(self, session_id: int | None, at: datetime, event: str, state: str,
                         fields: dict[str, Any]) -> None: ...
    def open_vpn_sessions(self) -> list[dict[str, Any]]: ...
    def last_public_ip(self) -> str | None: ...


class MemoryStore:
    """In-memory MetadataStore for tests and for runs without a database."""

    def __init__(self) -> None:
        self.sessions: dict[int, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []

    def start_vpn_session(self, **kw: Any) -> int:
        sid = len(self.sessions) + 1
        self.sessions[sid] = {"session_id": sid, "ended_utc": None, "interface": None, **kw}
        return sid

    def update_vpn_session(self, session_id: int, **fields: Any) -> None:
        self.sessions[session_id].update(fields)

    def record_vpn_event(self, session_id: int | None, at: datetime, event: str, state: str,
                         fields: dict[str, Any]) -> None:
        self.events.append({"session_id": session_id, "at": at, "event": event, "state": state, **fields})

    def open_vpn_sessions(self) -> list[dict[str, Any]]:
        return [dict(s) for s in self.sessions.values() if s.get("ended_utc") is None]

    def last_public_ip(self) -> str | None:
        ips = [s.get("public_ip") for s in self.sessions.values() if s.get("public_ip")]
        return ips[-1] if ips else None


def default_http_get(url: str, timeout: float, max_bytes: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "duo-wireguard-check/1.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as resp:
        return resp.read(max_bytes + 1)


# --------------------------------------------------------------------------
# Settings, status
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ManagerSettings:
    connect_timeout_s: float = 20.0
    disconnect_timeout_s: float = 15.0
    # A tunnel still "Disconnecting" (or "Connecting") after that gets a second stop and this much
    # longer: one that never connected took ~20 s to come down live on 2026-10-28.
    disconnect_grace_s: float = 30.0
    status_poll_s: float = 0.5
    settle_s: float = 5.0  # time for macOS to apply or remove routes, DNS and the interface
    dns_timeout_s: float = 10.0  # 5 s failed fresh tunnels 44, 45 and 46 on 2026-10-28 that later worked
    ip_url: str = "https://ipinfo.io/json"  # IP plus city/region/country, in one request
    ip_fallback_url: str = "https://api.ipify.org"  # IP only; used for the retry if ipinfo fails
    ip_timeout_s: float = 10.0
    ip_attempts: int = 2
    ip_retry_delay_s: float = 2.0
    probe_host: str = "api.ipify.org"
    route_probe_ip: str = "1.1.1.1"
    max_configs_per_connect: int = 3


@dataclass(frozen=True)
class Tunnel:
    """A WireGuard-app tunnel this application may use."""

    service: VpnService
    endpoint: str | None  # RemoteAddress from `scutil --nc show`

    @property
    def key(self) -> str:
        return self.service.uuid

    @property
    def name(self) -> str:
        return self.service.name


@dataclass
class ActiveTunnel:
    service_id: str
    service_name: str
    endpoint: str | None
    session_id: int | None
    baseline: NetworkSnapshot
    started: datetime
    started_monotonic: float
    reason: str | None = None
    interface: str | None = None
    tunnel_resolvers: list[str] = field(default_factory=list)  # resolvers the tunnel added
    public_ip: str | None = None
    location: str | None = None  # where a geolocation service places public_ip, e.g. "Miami, Florida, US"
    previous_ip: str | None = None
    ip_changed: bool | None = None
    handshake: str = "PENDING"
    connect_ms: int | None = None
    ready_monotonic: float | None = None
    start_invoked: bool = False

    @property
    def name(self) -> str:
        return self.service_name


@dataclass
class TunnelStatus:
    state: str
    config: str | None
    service: str | None
    interface: str | None
    handshake: str
    public_ip: str | None
    location: str | None
    previous_ip: str | None
    ip_changed: bool | None
    connect_ms: int | None
    uptime_s: float | None
    halted: bool
    last_failure: str | None
    session_id: int | None

    def lines(self) -> list[str]:
        yn = {True: "YES", False: "NO", None: "UNKNOWN"}
        return [
            f"State: {self.state}" + ("  (HALTED: connection management stopped)" if self.halted else ""),
            f"Tunnel: {self.config or '-'}",
            f"Interface: {self.interface or '-'}",
            f"Handshake: {self.handshake}",
            f"Public IP: {self.public_ip or '-'}",
            f"Location: {self.location or '-'}",
            f"Public IP changed: {yn[self.ip_changed]}",
            f"Connect time: {self.connect_ms} ms" if self.connect_ms is not None else "Connect time: -",
            f"Uptime: {self.uptime_s:.0f} s" if self.uptime_s is not None else "Uptime: -",
        ] + ([f"Last failure: {self.last_failure}"] if self.last_failure else [])


RECOVERY_STEPS = (
    "Manual network recovery:",
    "  1. scutil --nc list | grep com.wireguard   # a (Connected) line is a WireGuard-app tunnel",
    '  2. scutil --nc stop "{service}"             # or deactivate it in the WireGuard app',
    "  3. route -n get default                   # interface should be your Wi-Fi/Ethernet (e.g. en0)",
    "  4. scutil --dns | head                     # nameservers should no longer be the VPN's (e.g. 10.2.0.1)",
    "  5. curl -s https://api.ipify.org           # ordinary connectivity",
    "Then restart the application; it re-checks for leftover tunnels on startup.",
)

MAX_IP_BODY = 4096


def parse_ip_response(body: bytes) -> tuple[str, str | None]:
    """IPv4 and location from an ipinfo.io JSON object, or the IP from a plain-text body (ipify).
    Raises PublicIpError unless the IP is a valid, global IPv4 address."""
    if len(body) > MAX_IP_BODY:
        raise PublicIpError("public IP response too long")
    text = body.decode("utf-8", "replace").strip()
    location = None
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            raise PublicIpError("public IP response is not valid JSON") from None
        if not isinstance(data, dict):
            raise PublicIpError("public IP response is not a JSON object")
        text = str(data.get("ip", ""))
        parts = [str(data[k]).strip() for k in ("city", "region", "country")
                 if isinstance(data.get(k), str) and data[k].strip()]
        location = ", ".join(parts)[:80] or None
    try:
        ip = ipaddress.IPv4Address(text)
    except ValueError:
        raise PublicIpError("public IP response is not an IPv4 address") from None
    if not ip.is_global:
        raise PublicIpError("public IP response is not a global address")
    return str(ip), location


HANDSHAKE_VERIFIED = "VERIFIED_BY_TRAFFIC"  # the WireGuard app exposes no handshake time; traffic proves it


# --------------------------------------------------------------------------
# Manager
# --------------------------------------------------------------------------


class WireGuardManager:
    def __init__(
        self,
        platform: Platform,
        store: MetadataStore | None = None,
        settings: ManagerSettings | None = None,
        *,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], None] | None = None,
        http_get: Callable[[str, float, int], bytes] | None = None,
        console: Callable[[str], None] | None = None,
    ) -> None:
        self.platform = platform
        self.store = store if store is not None else MemoryStore()
        self.settings = settings or ManagerSettings()
        self.rng = rng or random.SystemRandom()
        self.clock, self.wall = clock, wall
        self.sleep = sleep or time.sleep
        self.http_get = http_get or default_http_get
        self.console = console
        self.sm = StateMachine(self._on_state)
        self.active: ActiveTunnel | None = None
        self.last_config: str | None = None  # service UUID of the last tunnel used
        self.last_verified_ip: str | None = self.store.last_public_ip()
        self.config_ips: dict[str, str] = {}  # tunnel (service UUID) -> public IP it last verified with
        self.direct_ip: str | None = None
        self.halted = False
        self.last_failure: str | None = None
        self._op_lock = threading.Lock()
        self._tunnels: list[Tunnel] | None = None

    # -- events ------------------------------------------------------------

    @property
    def state(self) -> ConnState:
        return self.sm.state

    def _say(self, text: str) -> None:
        if self.console:
            self.console(text)

    def _event(self, event: str, console: str | None = None, **fields: Any) -> None:
        a = self.active
        base: dict[str, Any] = {
            "event": event, "state": self.state.value,
            "config": a.name if a else None, "interface": a.interface if a else None,
        }
        base.update(fields)
        log.info(event.lower(), extra={"fields": base})
        try:
            self.store.record_vpn_event(a.session_id if a else None, self.wall(), event, self.state.value,
                                        {k: v for k, v in base.items() if k not in ("event", "state")})
        except Exception:
            log.exception("failed to record vpn event; continuing")
        if console is not None:
            self._say(console)

    def _on_state(self, old: ConnState, new: ConnState) -> None:
        self._say(new.value if new is not S.READY else f"State: {new.value}")
        log.debug("state", extra={"fields": {"from": old.value, "to": new.value}})

    def _to(self, new: ConnState) -> None:
        if self.state is not new:
            self.sm.to(new)

    def _wait(self, done: Callable[[], bool], timeout: float) -> bool:
        """Poll `done` every status_poll_s until it is true or `timeout` passes. Bounded."""
        deadline = self.clock() + timeout
        while True:
            if done():
                return True
            if self.clock() >= deadline:
                return False
            self.sleep(self.settings.status_poll_s)

    # -- configurations and tunnels -----------------------------------------

    def discover_tunnels(self, refresh: bool = False) -> list[Tunnel]:
        """Every tunnel in the WireGuard app. Cached; refresh=True re-reads it and reports changes."""
        if self._tunnels is not None and not refresh:
            return self._tunnels
        tunnels = [Tunnel(svc, self.platform.service_endpoint(svc.uuid))
                   for svc in self.platform.wireguard_services()]
        if self._tunnels is not None:
            old = {t.key for t in self._tunnels}
            new = {t.key for t in tunnels}
            added = [t.name for t in tunnels if t.key not in old]
            removed = [t.name for t in self._tunnels if t.key not in new]
            if added:
                self._event("TUNNELS_ADDED", f"New tunnel(s) available: {', '.join(added)}", tunnels=added)
            if removed:
                self._event("TUNNELS_REMOVED", f"Tunnel(s) no longer in the WireGuard app: {', '.join(removed)}",
                            tunnels=removed)
        self._tunnels = tunnels
        return tunnels

    def tunnel_named(self, name: str) -> Tunnel:
        for t in self.discover_tunnels(refresh=True):
            if name in (t.name, t.key):
                return t
        raise NotImportedError(f"no WireGuard-app tunnel named {name!r}; import it in the WireGuard app")

    def select_config(self, exclude: Iterable[str] = ()) -> Tunnel:
        """Random tunnel, never the last-used one when another exists."""
        excluded = set(exclude)
        pool = [t for t in self.discover_tunnels() if t.key not in excluded]
        if not pool:
            raise NoConfigsError("no WireGuard tunnels to use; import tunnels in the WireGuard app")
        if len(pool) > 1:
            pool = [t for t in pool if t.key != self.last_config] or pool
        return self.rng.choice(pool)

    # -- public lifecycle API ----------------------------------------------

    def connect(self, tunnel: str | None = None, reason: str = "start") -> TunnelStatus:
        """Connect one tunnel by name or UUID (or a random one, trying up to N) and verify it fully."""
        with self._exclusive():
            if tunnel is not None:
                self._connect(self.tunnel_named(tunnel), reason)
            else:
                self._connect_any(reason)
            return self.get_status()

    def disconnect(self, reason: str = "requested") -> None:
        with self._exclusive():
            self._teardown(reason)

    def rotate(self, reason: str = "requested", avoid: Iterable[str] = ()) -> TunnelStatus:
        """Replace the tunnel with a different one. `avoid` (service UUIDs) is a soft
        preference: those tunnels are skipped unless nothing else is usable."""
        with self._exclusive():
            prev = self.active.name if self.active else None
            if self.state is S.READY:
                self._to(S.ROTATING)
                self._event("ROTATION_STARTED", "ROTATION_STARTED", reason=reason)
                if prev:
                    self._say(f"Previous tunnel: {prev}")
            self._teardown(f"rotation:{reason}")
            self._connect_any(reason, avoid)
            return self.get_status()

    def cleanup(self) -> bool:
        """Idempotent teardown for finally blocks and signal paths. True if the host is restored."""
        if not self._op_lock.acquire(timeout=60):
            log.error("cleanup could not acquire the lifecycle lock")
            return False
        try:
            self._teardown("cleanup")
            return True
        except WireGuardError as exc:
            log.error("cleanup incomplete", extra={"fields": {"category": exc.category, "error": str(exc)}})
            return False
        finally:
            self._op_lock.release()

    def get_status(self, live: bool = False) -> TunnelStatus:
        a = self.active
        handshake = a.handshake if a else "NONE"
        if live and a:
            st = self.platform.status(a.service_id)
            if st != "Connected":
                handshake = f"NONE (service {st})"
        uptime = (self.clock() - a.ready_monotonic) if a and a.ready_monotonic is not None else None
        return TunnelStatus(self.state.value, a.name if a else None, a.service_name if a else None,
                            a.interface if a else None, handshake, a.public_ip if a else None,
                            a.location if a else None, a.previous_ip if a else None, a.ip_changed if a else None,
                            a.connect_ms if a else None, uptime, self.halted, self.last_failure,
                            a.session_id if a else None)

    def health_check(self, traffic: bool = False) -> bool:
        """Check that the READY tunnel is still usable. Marks loss as ERROR.

        The WireGuard app keeps reporting "Connected" when the server stops
        forwarding traffic (seen live on 2026-10-28), so with `traffic` the
        check also fetches the public IP through the tunnel.
        """
        a = self.active
        if self.state is not S.READY or a is None:
            return False
        problem = None
        st = self.platform.status(a.service_id)
        if st != "Connected":
            problem = f"WireGuard service is {st}"
        elif a.interface and a.interface not in self.platform.interfaces():
            problem = "interface disappeared"
        elif traffic:
            try:
                ip = self.verify_connectivity()
                if self.direct_ip is not None and ip == self.direct_ip:
                    problem = "traffic is leaving outside the tunnel"
            except WireGuardError as exc:
                problem = f"no traffic through the tunnel ({exc.category})"
        if problem is None:
            return True
        self.last_failure = f"connection_lost: {problem}"
        self._to(S.ERROR)
        self._event("CONNECTION_LOST", f"Connection lost: {problem}", failure_category="connection_lost",
                    detail=problem)
        return False

    def get_public_ip(self) -> str:
        """Externally visible IPv4 address. Raises ConnectivityError or PublicIpError."""
        return self.lookup_public_ip()[0]

    def lookup_public_ip(self) -> tuple[str, str | None]:
        """(public IPv4, location or None). The first attempt asks ipinfo.io (IP and location);
        the retry asks ipify (IP only), so a location-service outage never fails verification."""
        s = self.settings
        urls = (s.ip_url, s.ip_fallback_url)
        last: WireGuardError | None = None
        for attempt in range(s.ip_attempts):
            if attempt:
                self.sleep(s.ip_retry_delay_s)
            try:
                body = self.http_get(urls[attempt % 2], s.ip_timeout_s, MAX_IP_BODY)
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                last = ConnectivityError(f"public IP request failed: {type(exc).__name__}")
                continue
            try:
                return parse_ip_response(body)
            except PublicIpError as exc:
                last = exc
        assert last is not None
        raise last

    def verify_connectivity(self) -> str:
        """DNS resolution plus a verified public IP. Returns the IP."""
        return self._verify_connectivity()[0]

    def _verify_connectivity(self) -> tuple[str, str | None]:
        if not self.platform.resolve_host(self.settings.probe_host, self.settings.dns_timeout_s):
            raise DnsError(f"could not resolve {self.settings.probe_host} within {self.settings.dns_timeout_s:g}s")
        return self.lookup_public_ip()

    def recover_orphans(self) -> str:
        """Stop a tunnel this application started in an earlier run that never shut it down,
        and verify the host network against that run's baseline. Returns a short outcome."""
        with self._exclusive():
            sessions = self.store.open_vpn_sessions()
            if not sessions:
                return "clean"
            self._event("ORPHAN_DETECTED", "Found a session left open by an earlier run; recovering.",
                        open_sessions=len(sessions))
            for row in sessions[:-1]:
                self.store.update_vpn_session(row["session_id"], ended_utc=self.wall(),
                                              teardown_outcome="superseded", state=S.DISCONNECTED.value)
            row = sessions[-1]
            if not row.get("service_id"):
                # Recorded by the former wg-quick mode; there is no WireGuard-app service to stop.
                self.store.update_vpn_session(row["session_id"], ended_utc=self.wall(),
                                              teardown_outcome="legacy_unverified", state=S.DISCONNECTED.value)
                self._event("LEGACY_SESSION_CLOSED", "Closed a session from the former wg-quick mode without "
                            "checks; see the README if the network misbehaves.", session=row["session_id"])
                return "legacy"
            self.active = self._tunnel_from_row(row)
            self._teardown("orphan_recovery", outcome_label="recovered_orphan")
            return "recovered"

    # -- internals ---------------------------------------------------------

    class _Exclusive:
        def __init__(self, mgr: "WireGuardManager") -> None:
            self.mgr = mgr

        def __enter__(self) -> None:
            if not self.mgr._op_lock.acquire(blocking=False):
                raise RotationInProgress("another connection operation is already running")

        def __exit__(self, *exc: object) -> None:
            self.mgr._op_lock.release()

    def _exclusive(self) -> "_Exclusive":
        return self._Exclusive(self)

    def _tunnel_from_row(self, row: dict[str, Any]) -> ActiveTunnel:
        return ActiveTunnel(row["service_id"], row.get("wg_name") or row["service_id"], row.get("endpoint") or None,
                            row["session_id"], NetworkSnapshot.from_json(row["baseline_json"]),
                            row.get("started") or self.wall(), self.clock(), interface=row.get("interface"),
                            start_invoked=True)

    def _connect_any(self, reason: str, avoid: Iterable[str] = ()) -> None:
        self.discover_tunnels(refresh=True)  # pick up tunnels added to (or removed from) the WireGuard app
        tried: list[str] = []
        soft = list(avoid)
        last_exc: WireGuardError | None = None
        for _ in range(self.settings.max_configs_per_connect):
            try:
                try:
                    tunnel = self.select_config(exclude=tried + soft)
                except NoConfigsError:
                    if not soft:
                        raise
                    tunnel = self.select_config(exclude=tried)
            except NoConfigsError:
                if last_exc is None:
                    raise
                break
            tried.append(tunnel.key)
            try:
                self._connect(tunnel, reason)
                return
            except (HaltedError, RestoreError, ConflictingVpnError):
                raise
            except WireGuardError as exc:
                last_exc = exc
                if self.active is not None or self.state is not S.DISCONNECTED:
                    raise  # the failed attempt was not fully torn down; never stack another tunnel
        assert last_exc is not None
        raise last_exc

    def _connect(self, tunnel: Tunnel, reason: str) -> None:
        if self.halted:
            raise HaltedError("connection management is halted after a failed network restoration")
        if self.state not in (S.DISCONNECTED, S.ERROR) or self.active is not None:
            raise InvalidTransition(f"cannot connect from {self.state.value}")
        if self.state is S.ERROR:
            self._to(S.DISCONNECTED)
        svc, s = tunnel.service, self.settings
        t0 = self.clock()
        self._say(f"Selected tunnel: {svc.name}")
        self._to(S.CONNECTING)
        try:
            snap = self.platform.snapshot()
            if not snap.default_is_physical:
                raise ConflictingVpnError(
                    f"default route uses {snap.default_interface or 'no interface'}, not a physical interface; "
                    "another VPN may be active. Disconnect it first; this application never stops other VPNs.")
            busy = [x.name for x in self.platform.wireguard_services() if x.active]
            if busy:
                raise ConflictingVpnError(
                    f"WireGuard tunnel(s) {', '.join(busy)} already connected outside this application. "
                    "Deactivate them in the WireGuard app first; this application never stops a tunnel "
                    "it did not start.")
            if snap.connected_vpns:
                self._event("OTHER_VPN_CONNECTED", f"Note: other VPN services connected: {', '.join(snap.connected_vpns)}",
                            services=snap.connected_vpns)
            if self.direct_ip is None:
                try:
                    self.direct_ip = self.get_public_ip()
                except WireGuardError:
                    self.direct_ip = None  # unknown; the tunnel IP is still validated on its own
            started = self.wall()
            sid = self.store.start_vpn_session(
                config_name=svc.name, config_path="", wg_name=svc.name, endpoint=tunnel.endpoint or "",
                copy_path="", started=started, baseline_json=snap.to_json(), reason=reason, service_id=svc.uuid)
            self.active = ActiveTunnel(svc.uuid, svc.name, tunnel.endpoint, sid, snap, started, t0, reason=reason,
                                       previous_ip=self.last_verified_ip)
            self.last_config = svc.uuid
            self._event("CONNECTING", None, reason=reason, endpoint=tunnel.endpoint, service=svc.name)

            self.active.start_invoked = True
            r = self.platform.start(svc.uuid)
            if not r.ok:
                raise TunnelUpError(f"scutil --nc start {svc.name!r} failed"
                                    + (" (timed out)" if r.timed_out else f": {_last_line(r.stderr or r.stdout)}"))
            last = ["?"]

            def connected() -> bool:
                last[0] = self.platform.status(svc.uuid)
                if last[0] == "Invalid":
                    raise TunnelUpError(f"WireGuard service {svc.name!r} is invalid")
                return last[0] == "Connected"

            if not self._wait(connected, s.connect_timeout_s):
                raise TunnelUpError(f"{svc.name!r} not connected within {s.connect_timeout_s:g}s (status {last[0]})")

            iface: list[str | None] = [None]

            def tunnel_route() -> bool:
                iface[0] = self.platform.route_get(s.route_probe_ip).interface
                return bool(iface[0] and iface[0].startswith("utun") and iface[0] not in snap.interfaces)

            if not self._wait(tunnel_route, s.settle_s):
                raise RoutingError(f"traffic to {s.route_probe_ip} routes via {iface[0]}, not a new tunnel interface")
            self.active.interface = iface[0]
            self.store.update_vpn_session(sid, interface=iface[0], state=S.CONNECTED.value)
            self._to(S.CONNECTED)
            self._say(f"Interface: {iface[0]}")
            self._to(S.VERIFYING)
            self._verify(self.active)
            a = self.active
            a.connect_ms = round((self.clock() - t0) * 1000)
            a.ready_monotonic = self.clock()
            self._to(S.READY)
            self.last_failure = None
            self.last_verified_ip = a.public_ip
            if a.public_ip:
                self.config_ips[svc.uuid] = a.public_ip
            self.store.update_vpn_session(sid, ready_utc=self.wall(), state=S.READY.value, public_ip=a.public_ip,
                                          location=a.location,
                                          previous_ip=a.previous_ip, ip_changed=a.ip_changed,
                                          connect_ms=a.connect_ms)
            self._event("READY", None, public_ip=a.public_ip, location=a.location, ip_changed=a.ip_changed,
                        handshake=a.handshake,
                        connect_ms=a.connect_ms, reason=reason)
        except BaseException as exc:
            category = getattr(exc, "category", "interrupted" if isinstance(exc, KeyboardInterrupt) else "unexpected")
            self.last_failure = f"{category}: {exc}"
            if self.state is not S.ERROR:
                self._to(S.ERROR)
            self._event("CONNECT_FAILED", f"Connection failed ({category}): {exc}", failure_category=category,
                        connect_ms=round((self.clock() - t0) * 1000), reason=reason)
            if self.active is not None and self.active.session_id is not None:
                self.store.update_vpn_session(self.active.session_id, failure_category=category)
            if self.active is not None:
                self._teardown("connect_failed")  # may raise RestoreError, chained to exc
            else:
                self._to(S.DISCONNECTED)
            raise

    def _verify(self, a: ActiveTunnel) -> None:
        s, iface = self.settings, a.interface
        assert iface is not None
        addrs = self.platform.interface_addresses(iface)
        if addrs is None:
            raise InterfaceError(f"{iface} does not exist")
        if not any("." in x for x in addrs):
            raise InterfaceError(f"{iface} has no IPv4 address")

        # No endpoint-route check: the WireGuard app keeps its own UDP socket on the physical
        # link, so `route -n get <endpoint>` reports the tunnel even when everything works
        # (observed live 2026-10-28). The traffic checks below are what prove the tunnel.

        base = set(a.baseline.resolvers)
        added: list[str] = []

        def tunnel_dns_active() -> bool:
            added[:] = [r for r in self.platform.resolvers() if r not in base]
            return bool(added)

        if not self._wait(tunnel_dns_active, s.settle_s):
            raise DnsError("the system resolver did not change after connecting; the tunnel's DNS is not active")
        a.tunnel_resolvers = list(added)

        ip, location = self._verify_connectivity()
        if self.direct_ip is not None and ip == self.direct_ip:
            raise ConnectivityError("public IP equals the direct (untunneled) IP; traffic is not using the tunnel")
        a.public_ip, a.location = ip, location
        a.handshake = HANDSHAKE_VERIFIED
        a.ip_changed = None if a.previous_ip is None else ip != a.previous_ip
        yn = {True: "YES", False: "NO", None: "UNKNOWN (no previous IP)"}[a.ip_changed]
        self._say(f"Handshake: {HANDSHAKE_VERIFIED}")
        self._say(f"Public IP: {ip}")
        self._say(f"Location: {location or 'unknown'}")
        self._say(f"Public IP changed: {yn}")
        if a.ip_changed is False:
            self._event("SAME_PUBLIC_IP", None, public_ip=ip, previous_ip=a.previous_ip)
        self._say("Connectivity: VERIFIED")

    def _teardown(self, reason: str, outcome_label: str = "clean") -> None:
        """Stop only the application's own tunnel, then verify the host is back to its baseline.

        Idempotent. On anything that does not return to baseline, sets `halted`
        and raises RestoreError with manual recovery steps on the console.
        """
        a = self.active
        if a is None:
            if self.state is S.ERROR:
                self._to(S.DISCONNECTED)
            return
        if self.state is not S.DISCONNECTING:
            self._to(S.DISCONNECTING)
        problems: list[str] = []
        notes: list[str] = []
        try:
            self._teardown_steps(a, problems, notes)
        except WireGuardError as exc:  # never stop halfway in DISCONNECTING
            problems.append(f"{exc.category}: {exc}")

        outcome = outcome_label if not problems else "restore_failed"
        uptime = round(self.clock() - a.ready_monotonic, 1) if a.ready_monotonic is not None else None
        if a.session_id is not None:
            self.store.update_vpn_session(a.session_id, ended_utc=self.wall(), teardown_outcome=outcome,
                                          state=(S.DISCONNECTED if not problems else S.ERROR).value,
                                          rotation_reason=reason)
        if problems:
            self._to(S.ERROR)
            self.halted = True
            self.last_failure = f"{RestoreError.category}: {'; '.join(problems)}"
            self._event("RECOVERY_FAILED", "Network restoration incomplete: " + "; ".join(problems),
                        failure_category=RestoreError.category, problems=problems, notes=notes, reason=reason)
            for line in RECOVERY_STEPS:
                self._say(line.format(service=a.service_name))
            raise RestoreError("; ".join(problems))
        self._event("DISCONNECTED", None, reason=reason, uptime_s=uptime, notes=notes, outcome=outcome)
        self.active = None
        self._to(S.DISCONNECTED)

    def _teardown_steps(self, a: ActiveTunnel, problems: list[str], notes: list[str]) -> None:
        s = self.settings
        status = self.platform.status(a.service_id)
        pre_stop_resolvers = self.platform.resolvers()  # for recovery, where the tunnel's DNS was not recorded
        if status != "Disconnected" or a.start_invoked:
            if status != "Disconnected":
                r = self.platform.stop(a.service_id)
                if not r.ok:
                    notes.append(f"scutil --nc stop failed: {_last_line(r.stderr or r.stdout)}")
            last = [status]

            def stopped() -> bool:
                last[0] = self.platform.status(a.service_id)
                return last[0] in ("Disconnected", "Invalid")

            if not self._wait(stopped, s.disconnect_timeout_s):
                if last[0] in ("Disconnecting", "Connecting", "Reasserting"):  # still moving: stop again, wait more
                    notes.append(f"still {last[0]} after {s.disconnect_timeout_s:g}s; stop sent again")
                    self.platform.stop(a.service_id)
                    self._wait(stopped, s.disconnect_grace_s)
                if last[0] not in ("Disconnected", "Invalid"):
                    total = s.disconnect_timeout_s + (s.disconnect_grace_s if notes else 0)
                    problems.append(f"WireGuard service {a.service_name!r} is still {last[0]} after {total:g}s")
                    return

        base = a.baseline
        if a.interface and not self._wait(lambda: a.interface not in self.platform.interfaces(), s.settle_s):
            problems.append(f"{a.interface} still exists")
        probe: list[str | None] = [None]

        def routed_normally() -> bool:
            probe[0] = self.platform.route_get(s.route_probe_ip).interface
            return probe[0] == base.default_interface

        if not self._wait(routed_normally, s.settle_s):
            problems.append(f"traffic routes via {probe[0]}, baseline was {base.default_interface}")
        tunnel_dns = set(a.tunnel_resolvers or pre_stop_resolvers) - set(base.resolvers)
        if tunnel_dns and not self._wait(lambda: not tunnel_dns & set(self.platform.resolvers()), s.settle_s):
            problems.append("tunnel DNS servers are still in the system resolver")
        if problems:
            return
        try:
            self.direct_ip = self.verify_connectivity()
        except WireGuardError as exc:
            problems.append(f"ordinary connectivity check failed after teardown: {exc}")


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return (lines[-1] if lines else "")[:200]


# --------------------------------------------------------------------------
# Process lock
# --------------------------------------------------------------------------


class ProcessLock:
    """Exclusive, non-blocking flock so only one process manages the app tunnel."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._fh: IO[str] | None = None

    def acquire(self) -> None:
        fh = open(self.path, "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise LockHeldError(f"another process holds {self.path}; only one instance may manage the tunnel") from None
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps({"pid": os.getpid()}))
        fh.flush()
        self._fh = fh

    def release(self) -> None:
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None

    def __enter__(self) -> "ProcessLock":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


# --------------------------------------------------------------------------
# Gate for the calling application
# --------------------------------------------------------------------------


class NetworkGate:
    """Tells the application whether network operations are permitted, and
    rotates or reconnects only when the application calls before_cycle().

    Never retries the application's requests. Scheduled rotation never
    touches the application's throttling state. When the remote service
    refuses the current public IP (`on_blocked`), the gate switches to a
    different tunnel at the next boundary, at most `block_limit` times in a
    row; after that it declines and the application's own cooldown applies.
    """

    RECONNECT_BACKOFF_S = (30.0, 60.0, 120.0)
    MAX_BLOCK_SWITCHES = 3

    def __init__(self, manager: WireGuardManager, policy: RotationPolicy,
                 clock: Callable[[], float] = time.monotonic,
                 reconnect_backoff: Sequence[float] = RECONNECT_BACKOFF_S) -> None:
        self.manager = manager
        self.policy = policy
        self.clock = clock
        self.reconnect_backoff = tuple(reconnect_backoff)
        self.cycles = 0
        self.connected_at: float | None = None
        self._attempts = 0
        self._next_attempt = 0.0
        self.refused_ips: set[str] = set()
        self.dead_configs: set[str] = set()  # tunnels (UUIDs) that stopped passing traffic (this run)
        self.block_switches = 0
        self._pending: str | None = None

    def start(self) -> TunnelStatus:
        self.manager.recover_orphans()
        status = self.manager.connect(reason="start")
        self._mark_connected()
        return status

    def _mark_connected(self) -> None:
        self.cycles = 0
        self.connected_at = self.clock()
        self._attempts = 0

    def permitted(self) -> bool:
        return self.manager.state is S.READY and not self.manager.halted

    @property
    def session_id(self) -> int | None:
        a = self.manager.active
        return a.session_id if a and self.permitted() else None

    @property
    def public_ip(self) -> str | None:
        a = self.manager.active
        return a.public_ip if a and self.permitted() else None

    @property
    def label(self) -> str | None:
        """The tunnel network operations go through, e.g. "89 (138.199.50.129)"."""
        a = self.manager.active
        if not (a and self.permitted()):
            return None
        return f"{a.name} ({a.public_ip}, {a.location})" if a.location else f"{a.name} ({a.public_ip})"

    @property
    def retry_after_s(self) -> float:
        return max(1.0, self._next_attempt - self.clock())

    @property
    def block_limit(self) -> int:
        """Switches allowed in a row after refusals: every other config once, at most MAX_BLOCK_SWITCHES."""
        return max(0, min(self.MAX_BLOCK_SWITCHES, len(self.manager.discover_tunnels()) - 1))

    def resume_check(self, refused_ip: str | None) -> bool:
        """True when the current tunnel's IP differs from `refused_ip` (a refusal from an earlier run)."""
        if refused_ip:
            self.refused_ips.add(refused_ip)
        return self.public_ip is not None and self.public_ip != refused_ip

    def on_blocked(self) -> bool:
        """The remote service refused the current public IP. True: a different tunnel will
        be used for the next operation. False: switching is exhausted; apply the cooldown."""
        ip = self.public_ip
        if ip:
            self.refused_ips.add(ip)
        n, limit = self.block_switches, self.block_limit
        self.manager._event("BLOCKED_BY_REMOTE", None, public_ip=ip, switches_in_a_row=n, limit=limit)
        if n >= limit:
            self.manager._event("BLOCKED_FALLBACK_COOLDOWN",
                                f"Tunnel switching exhausted ({n}/{limit} in a row); using the normal cooldown.",
                                public_ip=ip, switches_in_a_row=n, limit=limit)
            return False
        self.block_switches += 1
        self._pending = "blocked:541"
        return True

    def _refused_configs(self) -> list[str]:
        refused = [p for p, ip in self.manager.config_ips.items() if ip in self.refused_ips]
        return refused + [p for p in self.dead_configs if p not in refused]

    def before_cycle(self) -> bool:
        """Called at a safe boundary, before the next network operation."""
        if self.manager.halted:
            raise HaltedError(self.manager.last_failure or "connection management halted")
        if self.permitted():
            if self._pending is not None:
                return self._switch_after_block()
            assert self.connected_at is not None
            reason = self.policy.due(self.connected_at, self.clock(), self.cycles)
            if reason is None:
                return True
            return self._replace(reason)
        if self.clock() < self._next_attempt:
            return False
        return self._replace("reconnect", self._refused_configs())

    def _switch_after_block(self) -> bool:
        reason, self._pending = self._pending or "blocked", None
        if not self._replace(reason, self._refused_configs()):
            return False
        if self.public_ip in self.refused_ips:
            # Same exit IP as one already refused: one more try avoiding every config seen with
            # a refused IP (now including this one), then accept whatever comes.
            self.manager._event("REFUSED_IP_AGAIN", f"New tunnel has an already-refused IP ({self.public_ip}); "
                                "trying one more configuration.", public_ip=self.public_ip)
            return self._replace(f"{reason}:same_ip", self._refused_configs())
        return True

    def _replace(self, reason: str, avoid: Sequence[str] = ()) -> bool:
        try:
            self.manager.rotate(reason, avoid)
        except (HaltedError, RestoreError):
            raise HaltedError(self.manager.last_failure or "network restoration failed") from None
        except WireGuardError as exc:
            if self._attempts >= len(self.reconnect_backoff):
                self.manager.halted = True
                raise HaltedError(f"could not establish a verified tunnel after {self._attempts + 1} attempts: "
                                  f"{exc}") from None
            self._next_attempt = self.clock() + self.reconnect_backoff[self._attempts]
            self._attempts += 1
            log.warning("tunnel not ready; network operations paused", extra={"fields": {
                "category": exc.category, "retry_in_s": self.reconnect_backoff[self._attempts - 1]}})
            return False
        self._mark_connected()
        return True

    def after_cycle(self, network_errors: int = 0, blocked: bool = False) -> None:
        self.cycles += 1
        if not blocked:
            self.block_switches = 0
        if network_errors:
            a = self.manager.active
            if not self.manager.health_check(traffic=True) and a is not None:
                self.dead_configs.add(a.service_id)  # reconnect prefers any other tunnel

    def close(self) -> bool:
        return self.manager.cleanup()
