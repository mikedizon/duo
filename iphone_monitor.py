#!/usr/bin/env python3
"""Observe Apple Store in-store pickup inventory for all iPhone Duo configs.

Polls Apple's retail pickup endpoint for the eight iPhone Duo SKUs
(256GB-2TB, Night Sky and Star White) at the Apple Stores near your ZIP(s), prints a
store x SKU matrix every cycle, groups per-store availability transitions
into restock waves, and keeps the history in SQLite for timing analysis.

Purchase page opens only for 1TB Night Sky (preferred) or 2TB Night Sky.
Never purchases anything. Standard library only. Python 3.11+, macOS.
"""

from __future__ import annotations

import argparse
import email.utils
import json
import logging
import os
import random
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

if sys.version_info < (3, 11):  # pragma: no cover
    sys.exit("iphone_monitor.py requires Python 3.11 or newer.")

from inventory_model import (  # noqa: E402
    ALL_KEYS, ALL_PARTS, PREFERRED_PARTS, REGION_ZIPS, SKU_BY_PART, SKUS, STORES, Key, Observation,
    Sku, Status, Store, fmt_local, from_utc_text, local, utcnow,
)
from inventory_waves import (  # noqa: E402
    BECAME_AVAILABLE, AlertTracker, InventoryTracker, Transition, Wave, WaveDetector, WaveUpdate,
)

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

ENDPOINT = "https://www.apple.com/shop/retail/pickup-message"
SOUND_FILE = "/System/Library/Sounds/Glass.aiff"
AFPLAY = "afplay"
DEFAULT_DB = Path(__file__).resolve().with_name("inventory_history.sqlite3")
DEFAULT_ENV_FILE = Path(__file__).resolve().with_name(".env")
# Configurations that open the purchase page, highest priority first. Set from
# DUO_ALERT_PARTS (environment or .env) in main(); defaults to 1TB, then 2TB Night Sky.
PURCHASE_ALERT_PARTS: tuple[str, ...] = PREFERRED_PARTS

DEFAULT_INTERVAL = 20.0
MIN_INTERVAL = 5.0
DEFAULT_WAVE_WINDOW_MIN = 10.0
HTTP_TIMEOUT = 10.0  # seconds; applies to connect and to each socket read
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
BACKOFF_CAP = 15 * 60.0
MAX_RETRY_AFTER = 60 * 60.0
NETWORK_FAILURES_BEFORE_BACKOFF = 3
# After HTTP 541 or 403: send nothing for these durations, then probe once.
COOLDOWN_STEPS_S = (300.0, 600.0, 1200.0, 1800.0)
BLOCKED_KINDS = ("blocked", "forbidden")
USER_AGENT = "iphone-pickup-monitor/2.0 (personal stock check; Python urllib)"

MANUAL_CHECKLIST = (
    "Trade-in: iPhone 16 Pro Max, good condition",
    "Payment: Verizon carrier financing, 36 months",
    "AppleCare: use existing AppleCare One subscription",
)

GREEN, BOLD, RESET = "\033[32m", "\033[1m", "\033[0m"

log = logging.getLogger("iphone_monitor")


class FetchError(Exception):
    """A failed inventory request. `kind` drives backoff decisions."""

    KINDS = ("rate_limited", "blocked", "forbidden", "server", "http", "network", "decode")

    def __init__(
        self,
        kind: str,
        message: str,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        assert kind in self.KINDS, kind
        self.kind = kind
        self.status_code = status_code
        self.retry_after = retry_after


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------


class KeyValueFormatter(logging.Formatter):
    """Appends `extra={"fields": {...}}` to the message as key=value pairs."""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        fields = getattr(record, "fields", None)
        if isinstance(fields, Mapping) and fields:
            pairs = " ".join(f"{k}={json.dumps(v, default=str)}" for k, v in fields.items())
            return f"{base} {pairs}"
        return base


def configure_logging(verbose: bool) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        KeyValueFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    log.handlers[:] = [handler]
    log.setLevel(logging.DEBUG if verbose else logging.WARNING)
    log.propagate = False


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------


def build_url(zip_code: str, parts: Sequence[str] = ALL_PARTS) -> str:
    params = [("pl", "true"), ("location", zip_code)]
    params += [(f"parts.{i}", p) for i, p in enumerate(parts)]
    return f"{ENDPOINT}?{urllib.parse.urlencode(params)}"


def parse_retry_after(value: str | None, now: float | None = None) -> float | None:
    """Parse a Retry-After header (seconds or HTTP-date) into seconds."""
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        current = now if now is not None else time.time()
        seconds = when.timestamp() - current
    if seconds != seconds:  # NaN
        return None
    return max(0.0, min(seconds, MAX_RETRY_AFTER))


def _classify_http_status(code: int) -> str:
    if code == 429:
        return "rate_limited"
    if code == 403:
        return "forbidden"
    if code == 541:  # Apple's "Page Not Found" refusal seen once a client is throttled
        return "blocked"
    if 500 <= code <= 599:
        return "server"
    return "http"


Opener = Callable[..., Any]


def fetch_inventory(
    zip_code: str,
    *,
    opener: Opener | None = None,
    timeout: float = HTTP_TIMEOUT,
) -> Any:
    """GET the pickup endpoint for `zip_code` (all eight parts) and return JSON.

    Raises FetchError for every failure mode; never returns a partial result.
    """
    open_fn = opener if opener is not None else urllib.request.urlopen
    url = build_url(zip_code)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        method="GET",
    )
    started = time.monotonic()
    try:
        with open_fn(request, timeout=timeout) as response:
            code = getattr(response, "status", None) or response.getcode()
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        retry_after = parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)
        kind = _classify_http_status(exc.code)
        raise FetchError(kind, f"HTTP {exc.code} for {zip_code}", exc.code, retry_after) from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise FetchError("network", f"network error for {zip_code}: {reason}") from exc
    except Exception as exc:  # http.client.HTTPException (IncompleteRead, etc.)
        raise FetchError("network", f"transport error for {zip_code}: {exc!r}") from exc

    elapsed_ms = round((time.monotonic() - started) * 1000)
    log.debug(
        "fetched",
        extra={"fields": {"zip": zip_code, "http": code, "bytes": len(raw), "ms": elapsed_ms}},
    )
    if code != 200:
        raise FetchError(_classify_http_status(int(code)), f"HTTP {code} for {zip_code}", int(code))
    if len(raw) > MAX_RESPONSE_BYTES:
        raise FetchError("decode", f"response for {zip_code} exceeds {MAX_RESPONSE_BYTES} bytes")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FetchError("decode", f"invalid JSON for {zip_code}: {exc}") from exc


# --------------------------------------------------------------------------
# Parsing and store matching
# --------------------------------------------------------------------------

_PICKUP_DISPLAY_MAP = {
    "available": Status.AVAILABLE,
    "unavailable": Status.UNAVAILABLE,
    "ineligible": Status.INELIGIBLE,
}
_name_warned: set[str] = set()


def parse_store_result(store: Mapping[str, Any], store_id: str, part: str) -> Observation:
    """Interpret one part at one store entry. Anything unexpected is UNKNOWN."""
    parts = store.get("partsAvailability")
    if not isinstance(parts, Mapping):
        return Observation(store_id, part, Status.UNKNOWN, "partsAvailability missing")
    entry = parts.get(part)
    if not isinstance(entry, Mapping):
        return Observation(store_id, part, Status.UNKNOWN, f"no entry for {part}")
    entry_part = entry.get("partNumber")
    if entry_part is not None and entry_part != part:
        return Observation(store_id, part, Status.UNKNOWN, f"partNumber mismatch: {entry_part!r}")
    display = entry.get("pickupDisplay")
    if not isinstance(display, str):
        return Observation(store_id, part, Status.UNKNOWN, f"pickupDisplay not a string: {display!r}")
    status = _PICKUP_DISPLAY_MAP.get(display)
    if status is None:
        return Observation(store_id, part, Status.UNKNOWN, f"unrecognized pickupDisplay {display!r}")
    quote = entry.get("pickupSearchQuote")
    return Observation(store_id, part, status, quote if isinstance(quote, str) else display)


def _unknown_store(store_id: str, parts: Iterable[str], detail: str) -> dict[str, Observation]:
    return {p: Observation(store_id, p, Status.UNKNOWN, detail) for p in parts}


def parse_store_results(
    payload: Any, wanted_ids: Iterable[str], parts: Sequence[str] = ALL_PARTS
) -> dict[str, dict[str, Observation]]:
    """Extract {store_id: {part: Observation}} for `wanted_ids`.

    A structurally malformed payload yields UNKNOWN for every wanted store
    and part. A well-formed payload that does not list a store omits it, so
    the caller can try that store's own ZIP. Other stores are ignored.
    """
    wanted = list(wanted_ids)
    if not isinstance(payload, Mapping):
        return {sid: _unknown_store(sid, parts, "payload is not an object") for sid in wanted}
    head = payload.get("head")
    if isinstance(head, Mapping) and "status" in head and str(head.get("status")) != "200":
        return {sid: _unknown_store(sid, parts, f"head.status={head.get('status')!r}") for sid in wanted}
    body = payload.get("body")
    if not isinstance(body, Mapping):
        return {sid: _unknown_store(sid, parts, "body missing") for sid in wanted}
    stores = body.get("stores")
    if not isinstance(stores, list):
        return {sid: _unknown_store(sid, parts, "body.stores is not a list") for sid in wanted}

    results: dict[str, dict[str, Observation]] = {}
    for store in stores:
        if not isinstance(store, Mapping):
            continue
        store_id = store.get("storeNumber")
        if store_id not in wanted or store_id in results:
            continue
        expected = STORES.get(store_id)
        name = store.get("storeName")
        if expected is not None and name != expected.name and store_id not in _name_warned:
            _name_warned.add(store_id)
            log.warning("store name differs from expected; matching on store number only",
                        extra={"fields": {"store": store_id, "expected": expected.name, "got": name}})
        results[store_id] = {p: parse_store_result(store, store_id, p) for p in parts}
    return results


def classify_store(observations: Iterable[Observation]) -> str:
    statuses = [o.status for o in observations]
    if statuses and all(s is Status.ERROR for s in statuses):
        return "error"
    confirmed = sum(1 for s in statuses if s.is_confirmed)
    if statuses and confirmed == len(statuses):
        return "ok"
    return "partial" if confirmed else "unknown"


@dataclass
class CheckOutcome:
    results: dict[Key, Observation]
    errors: list[FetchError]
    requests: int
    store_outcomes: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.store_outcomes:
            by_store: dict[str, list[Observation]] = {}
            for o in self.results.values():
                by_store.setdefault(o.store_id, []).append(o)
            self.store_outcomes = {sid: classify_store(obs) for sid, obs in by_store.items()}

    @property
    def store_ok(self) -> dict[str, bool]:
        return {sid: o == "ok" for sid, o in self.store_outcomes.items()}


def _error_results(store_ids: Iterable[str], detail: str) -> dict[Key, Observation]:
    return {(sid, p): Observation(sid, p, Status.ERROR, detail) for sid in store_ids for p in ALL_PARTS}


class ZipPicker:
    """Chooses the ZIP each region's request uses: random from the region's list,
    never the same twice in a row, skipping ZIPs whose response missed a store."""

    def __init__(self, region_zips: Mapping[str, Sequence[str]] = REGION_ZIPS,
                 rng: random.Random | None = None) -> None:
        self.zips = {region: list(dict.fromkeys(zs)) for region, zs in region_zips.items()}
        self.rng = rng or random.Random()
        self.last: dict[str, str] = {}
        self.demoted: dict[str, set[str]] = {}

    def pick(self, region: str) -> str:
        pool = [z for z in self.zips.get(region, [region]) if z not in self.demoted.get(region, set())] or [region]
        if len(pool) > 1:
            pool = [z for z in pool if z != self.last.get(region)] or pool
        choice = self.rng.choice(pool)
        self.last[region] = choice
        return choice

    def demote(self, region: str, zip_code: str) -> None:
        """Stop using `zip_code` for `region` this run. The region's own center ZIP is never dropped."""
        if zip_code == region or zip_code in self.demoted.get(region, set()):
            return
        self.demoted.setdefault(region, set()).add(zip_code)
        log.info("zip dropped: response missed stores", extra={"fields": {"region": region, "zip": zip_code}})


ZIP_PICKER = ZipPicker()


def reset_zip_picker() -> None:
    """Rebuild the shared picker after the store set (and its ZIP lists) changed."""
    global ZIP_PICKER
    ZIP_PICKER = ZipPicker()


def check_all(
    fetch: Callable[[str], Any] | None = None,
    stores: Mapping[str, Store] = STORES,
    rng: random.Random | None = None,
    zips: Sequence[str] | None = None,
    picker: ZipPicker | None = None,
) -> CheckOutcome:
    """Check every configured store and SKU with as few requests as possible.

    One request per regional ZIP (each batching all eight parts), in a
    random order each cycle. A store missing from a well-formed regional
    response gets one request to its own ZIP. After a 429, 403 or 541 no
    further requests are made this cycle.

    `zips` limits the cycle to those regions, named by their center ZIP
    (used for a single cooldown probe); stores of other regions get no
    result at all. With a `picker`, each region's request uses a ZIP it
    chooses (random nearby ZIP); without one, the region's center ZIP.
    """
    fetch = fetch or fetch_inventory
    groups: dict[str, list[str]] = {}
    for sid, s in stores.items():
        groups.setdefault(s.poll_zip, []).append(sid)
    order = list(groups)
    (rng or random).shuffle(order)
    if zips is not None:
        order = [z for z in order if z in zips]

    results: dict[Key, Observation] = {}
    errors: list[FetchError] = []
    requests = 0
    halted = False
    missing: list[str] = []

    def run(zip_code: str, wanted: list[str]) -> dict[str, dict[str, Observation]] | None:
        nonlocal requests, halted
        if halted:
            results.update(_error_results(wanted, "skipped after rate limit"))
            return None
        requests += 1
        try:
            return parse_store_results(fetch(zip_code), wanted)
        except FetchError as exc:
            log.warning(str(exc), extra={"fields": {"kind": exc.kind, "zip": zip_code}})
            errors.append(exc)
            if exc.kind in ("rate_limited", *BLOCKED_KINDS):
                halted = True
            results.update(_error_results(wanted, exc.kind))
            return None

    requested: set[str] = set()
    for region in order:
        wanted = groups[region]
        zip_code = picker.pick(region) if picker is not None else region
        requested.add(zip_code)
        log.debug("regional request", extra={"fields": {"region": region, "zip": zip_code}})
        parsed = run(zip_code, wanted)
        if parsed is None:
            continue
        lacking = [sid for sid in wanted if sid not in parsed]
        for sid in wanted:
            if sid in parsed:
                results.update({(sid, p): o for p, o in parsed[sid].items()})
        missing.extend(lacking)
        if lacking and picker is not None:
            picker.demote(region, zip_code)

    for sid in missing:
        own_zip = stores[sid].zip_code
        if own_zip in requested:  # its own ZIP was already this poll's regional request
            results.update({(sid, p): o for p, o in _unknown_store(sid, ALL_PARTS, "store not in response").items()})
            continue
        parsed = run(own_zip, [sid])
        if parsed is None:
            continue
        found = parsed.get(sid) or _unknown_store(sid, ALL_PARTS, "store not in response")
        results.update({(sid, p): o for p, o in found.items()})

    ordered = {k: results[k] for k in ((sid, p) for sid in stores for p in ALL_PARTS) if k in results}
    return CheckOutcome(ordered, errors, requests)


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------


def play_chime(
    sound: str = SOUND_FILE,
    runner: Callable[..., subprocess.Popen[bytes]] | None = None,
) -> subprocess.Popen[bytes] | None:
    """Start `afplay <sound>` without blocking. Never raises."""
    runner = runner or subprocess.Popen
    if not Path(sound).is_file():
        log.error("sound file missing", extra={"fields": {"path": sound}})
        sys.stdout.write("\a")
        sys.stdout.flush()
        return None
    try:
        return runner(
            [AFPLAY, sound],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, ValueError) as exc:
        log.error("afplay failed", extra={"fields": {"error": repr(exc)}})
        sys.stdout.write("\a")
        sys.stdout.flush()
        return None


def open_purchase_page(url: str, opener: Callable[[str], bool] | None = None) -> bool:
    """Open `url` in the default browser. Never raises."""
    opener = opener or webbrowser.open
    try:
        ok = bool(opener(url))
    except Exception as exc:  # webbrowser.Error, OSError
        log.error("browser open failed", extra={"fields": {"error": repr(exc)}})
        return False
    if not ok:
        log.error("browser reported failure", extra={"fields": {"url": url}})
    return ok


def choose_purchase(new_keys: Sequence[Key], is_available: Callable[[Key], bool]) -> Sku | None:
    """Pick which purchase page to open for this cycle, if any.

    The highest-priority newly available part in PURCHASE_ALERT_PARTS wins, but
    only while no higher-priority part is currently available at any store
    (that page was already opened for it). With the default order this means
    1TB Night Sky always wins and 2TB Night Sky opens only without 1TB.
    """
    new_parts = {p for _, p in new_keys}
    for i, part in enumerate(PURCHASE_ALERT_PARTS):
        if part not in new_parts:
            continue
        higher = PURCHASE_ALERT_PARTS[:i]
        if any(is_available((sid, h)) for sid in STORES for h in higher):
            return None
        return SKU_BY_PART[part]
    return None


def format_wave_banner(wave: Wave, simulated: bool) -> str:
    store = STORES[wave.store_id]
    tag = "SIMULATED " if simulated else ""
    codes = ", ".join(SKU_BY_PART[p].label for p in wave.members)
    wid = f" #{wave.wave_id}" if wave.wave_id is not None else ""
    return (f">>> {tag}RESTOCK WAVE{wid} at Apple {store.name} [{store.store_id}], {store.city}: "
            f"{codes}  [{wave.event_class}/{wave.confidence}]  {fmt_local(wave.first_at)}")


def format_purchase_banner(keys: Sequence[Key], sku: Sku | None, at: datetime, simulated: bool) -> str:
    bar = "!" * 72
    tag = "SIMULATED (dry run, no API request made) - " if simulated else ""
    lines = [bar, f"  {tag}PREFERRED MODEL AVAILABLE FOR PICKUP", ""]
    for sid, part in keys:
        s = STORES[sid]
        lines.append(f"  -> iPhone Duo {SKU_BY_PART[part].label} at Apple {s.name}, {s.city} [{sid}]")
    lines += ["", f"  Time: {fmt_local(at)}"]
    if sku is not None:
        lines += [f"  Opening: {sku.purchase_url}", "  Complete manually:",
                  *(f"    - {item}" for item in MANUAL_CHECKLIST)]
    else:
        lines.append("  1TB Night Sky is already available elsewhere; not opening the 2TB page.")
    lines.append(bar)
    return "\n".join(lines)


def notify_cycle(
    new_waves: Sequence[Wave],
    purchase_keys: Sequence[Key],
    is_available: Callable[[Key], bool],
    at: datetime,
    *,
    simulated: bool = False,
    chime: Callable[[], Any] | None = None,
    browser: Callable[[str], bool] | None = None,
    out: Any = None,
) -> Any:
    """Alert for one polling cycle: at most one chime and one browser tab.

    Every new wave prints a banner. The chime plays once if there is any new
    wave or newly available preferred SKU. The purchase page opens only for
    1TB/2TB Night Sky (see choose_purchase).
    """
    if not new_waves and not purchase_keys:
        return None
    stream = out if out is not None else sys.stdout
    for w in new_waves:
        print(format_wave_banner(w, simulated), file=stream)
    sku = choose_purchase(purchase_keys, is_available) if purchase_keys else None
    if purchase_keys:
        print(format_purchase_banner(purchase_keys, sku, at, simulated), file=stream)
    stream.flush()
    proc = (chime or play_chime)()
    if sku is not None:
        (browser or open_purchase_page)(sku.purchase_url)
    log.info("alert sent", extra={"fields": {
        "waves": [w.store_id for w in new_waves], "purchase": [list(k) for k in purchase_keys],
        "opened": sku.code if sku else None, "simulated": simulated}})
    return proc


# --------------------------------------------------------------------------
# Backoff
# --------------------------------------------------------------------------


class Backoff:
    """Computes the delay before the next poll.

    429/403/5xx raise the backoff level immediately. Network, decode and
    other HTTP errors raise it after NETWORK_FAILURES_BEFORE_BACKOFF
    consecutive failing cycles. A cycle with no errors resets everything.
    """

    def __init__(
        self,
        interval: float,
        cap: float = BACKOFF_CAP,
        rand: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.interval = interval
        self.cap = max(cap, interval)
        self.level = 0
        self.soft_failures = 0
        self._rand = rand

    def next_delay(self, errors: Sequence[FetchError]) -> float:
        if not errors:
            if self.level:
                log.info("backoff reset", extra={"fields": {"interval": self.interval}})
            self.level = 0
            self.soft_failures = 0
            return self.interval

        hard = any(e.kind in ("rate_limited", "forbidden", "server") for e in errors)
        if hard:
            self.level += 1
        else:
            self.soft_failures += 1
            if self.soft_failures >= NETWORK_FAILURES_BEFORE_BACKOFF:
                self.level += 1
        if self.level == 0:
            return self.interval

        delay = min(self.cap, self.interval * (2 ** self.level))
        delay = min(self.cap, delay * self._rand(1.0, 1.1))
        retry_after = max((e.retry_after or 0.0) for e in errors)
        delay = max(delay, retry_after)
        log.warning(
            "backing off",
            extra={"fields": {"level": self.level, "delay_s": round(delay, 1), "retry_after": retry_after or None}},
        )
        return delay


# --------------------------------------------------------------------------
# Blocked cooldown
# --------------------------------------------------------------------------


class Cooldown:
    """Quiet period after Apple refuses requests (HTTP 541 or 403).

    The first refusal starts COOLDOWN_STEPS_S[0] seconds of silence. When it
    expires, one probe request is allowed. A refused probe moves to the next
    (longer) step; a clean probe ends the cooldown.
    """

    def __init__(self, steps: Sequence[float] = COOLDOWN_STEPS_S) -> None:
        self.steps = tuple(steps)
        self.level = -1
        self.until: float | None = None

    @property
    def active(self) -> bool:
        return self.level >= 0

    def trip(self, now: float) -> float:
        self.level = min(self.level + 1, len(self.steps) - 1)
        duration = self.steps[self.level]
        self.until = now + duration
        return duration

    def resume(self, now: float, remaining_s: float) -> None:
        """Re-enter a cooldown that a previous run started."""
        self.level = 0
        self.until = now + max(0.0, remaining_s)

    def clear(self) -> None:
        self.level = -1
        self.until = None


# --------------------------------------------------------------------------
# Terminal display
# --------------------------------------------------------------------------

SYMBOL = {Status.AVAILABLE: "✓", Status.UNAVAILABLE: "-", Status.INELIGIBLE: "x",
          Status.UNKNOWN: "?", Status.ERROR: "?"}
def name_width() -> int:
    """Width of the store-name column for the current store set."""
    return max((len(s.name) for s in STORES.values()), default=10) + 2
LEGEND = "Legend: ✓ available  - unavailable  x ineligible  ? unknown/error  (green = newly available)"


def use_color(stream: Any) -> bool:
    return bool(getattr(stream, "isatty", lambda: False)()) and "NO_COLOR" not in os.environ


def render_matrix(
    poll_no: int,
    at: datetime,
    outcome: CheckOutcome,
    newly: set[Key],
    new_waves: int,
    duration_s: float,
    *,
    color: bool = False,
    title_suffix: str = "",
    footer: str = "",
) -> str:
    widths = [max(len(s.code), 5) + 1 for s in SKUS]
    lines = [f"[{fmt_local(at)}] Poll #{poll_no}{title_suffix}", ""]
    lines.append("STORE".ljust(name_width()) + "".join(s.code.center(w) for s, w in zip(SKUS, widths)))
    for sid, store in STORES.items():
        cells = []
        for sku, w in zip(SKUS, widths):
            obs = outcome.results.get((sid, sku.part))
            sym = SYMBOL[obs.status] if obs else "?"
            cell = sym.center(w)
            if color and (sid, sku.part) in newly:
                cell = cell.replace(sym, f"{GREEN}{BOLD}{sym}{RESET}")
            cells.append(cell)
        lines.append(store.name.ljust(name_width()) + "".join(cells))
    available = sum(1 for o in outcome.results.values() if o.status is Status.AVAILABLE)
    total = len(STORES) * len(SKUS)
    unknown = sum(1 for o in outcome.results.values() if not o.status.is_confirmed) + (total - len(outcome.results))
    errs = ", ".join(sorted({e.kind for e in outcome.errors})) or "none"
    lines += [
        "",
        f"Available {available}/{total} | Newly available {len(newly)} | New waves {new_waves} | "
        f"Unknown/error cells {unknown} | Duration {duration_s:.2f}s | Requests {outcome.requests} | "
        f"Errors {len(outcome.errors)} ({errs})" + (f" | {footer}" if footer else ""),
    ]
    return "\n".join(lines)


def format_transition(t: Transition, wave: Wave | None) -> str:
    store = STORES[t.store_id]
    sku = SKU_BY_PART[t.part]
    what = "AVAILABLE" if t.kind == BECAME_AVAILABLE else t.status.value.upper()
    extra = f"  timing={t.timing}" if t.timing != "exact" else ""
    w = f"  wave #{wave.wave_id}" if wave is not None and wave.wave_id is not None else ""
    return f"{fmt_local(t.at, with_date=False)}  {store.name:<{name_width()}}{sku.label:<18} {what}{w}{extra}"


# --------------------------------------------------------------------------
# Monitor
# --------------------------------------------------------------------------


@dataclass
class Monitor:
    interval: float
    wave_window: timedelta
    db: Any = None  # inventory_db.HistoryDB, or None for dry runs and tests
    run_id: int | None = None
    check: Callable[..., CheckOutcome] | None = None  # called with zips=
    notifier: Callable[..., Any] | None = None
    wall: Callable[[], datetime] = utcnow
    out: Any = None
    color: bool | None = None
    simulated: bool = False
    mono: Callable[[], float] = time.monotonic  # clock for the cooldown; run_loop uses the same one
    cooldown: Cooldown | None = None
    poll_no: int = 0
    vpn_session: Callable[[], int | None] | None = None  # tunnel session the next poll runs through
    vpn_label: Callable[[], str | None] | None = None  # e.g. "89 (138.199.50.129)" for the footer
    sink: Any = None  # tui.TuiModel: polls become collapsible entries instead of printed matrices

    def __post_init__(self) -> None:
        gap = timedelta(seconds=max(2.5 * self.interval, 60.0))
        self.tracker = InventoryTracker(ALL_KEYS, gap)
        self.waves = WaveDetector(self.wave_window)
        self.alerts = AlertTracker([(sid, p) for sid in STORES for p in PURCHASE_ALERT_PARTS])
        self.stream = self.out if self.out is not None else sys.stdout
        if self.color is None:
            self.color = use_color(self.stream)
        if self.cooldown is None:
            self.cooldown = Cooldown()

    def seed_from_history(self) -> None:
        """Restore SKU state and any cooldown from history."""
        if self.db is None:
            return
        prior = self.db.load_prior_state()
        self.tracker.seed(prior)
        now = self.wall()
        recent = self.db.recent_polls(now - timedelta(hours=2))
        if recent:
            last_at, _, summary = recent[-1]
            if summary and any(k in summary.split(",") for k in BLOCKED_KINDS):
                age = (now - last_at).total_seconds()
                self.cooldown.resume(self.mono(), self.cooldown.steps[0] - age)
        log.info("seeded state from history", extra={"fields": {
            "keys": len(prior), "cooldown": self.cooldown.active}})

    def _check(self, zips: Sequence[str] | None) -> CheckOutcome:
        try:
            if self.check is None:
                return check_all(zips=zips, picker=ZIP_PICKER)
            return self.check(zips=zips)
        except Exception as exc:  # a bug or an API shape we did not anticipate
            log.exception("check failed unexpectedly")
            return CheckOutcome(_error_results(STORES, "internal error"), [FetchError("decode", repr(exc))], 0)

    def poll(self, zips: Sequence[str] | None = None) -> CheckOutcome:
        started = self.wall()
        t0 = time.monotonic()
        outcome = self._check(zips)
        return self.process(outcome, started, time.monotonic() - t0, probe=zips is not None)

    def process(self, outcome: CheckOutcome, at: datetime, duration_s: float,
                probe: bool = False) -> CheckOutcome:
        self.poll_no += 1
        transitions = self.tracker.observe(outcome.results, at)
        upd = self.waves.process(transitions, outcome.results, outcome.store_ok, at)
        purchase_keys = self.alerts.update(outcome.results)

        if self.db is not None and not self.simulated:
            from inventory_db import PollRecord
            try:
                self.db.record_poll(self.run_id, PollRecord(
                    at, at + timedelta(seconds=duration_s), outcome.requests, outcome.errors,
                    outcome.results, outcome.store_outcomes, transitions, upd,
                    vpn_session_id=self.vpn_session() if self.vpn_session else None))
            except Exception:
                log.exception("failed to record poll; continuing")

        newly = {(t.store_id, t.part) for t in transitions if t.kind == BECAME_AVAILABLE}
        suffix = "  [SIMULATED - not recorded]" if self.simulated else ""
        if probe:
            suffix += "  [cooldown probe: one region only]"
        tunnel = self.vpn_label() if self.vpn_label is not None else None
        footer = f"Tunnel {tunnel or 'none'}" if self.vpn_label is not None else ""
        wave_of = {(w.store_id, m.part): w for w, m in upd.joined}
        if self.sink is not None:
            self._to_sink(outcome, at, newly, upd, transitions, wave_of, duration_s, suffix, footer, tunnel)
        else:
            print(render_matrix(self.poll_no, at, outcome, newly, len(upd.opened), duration_s,
                                color=bool(self.color), title_suffix=suffix, footer=footer), file=self.stream)
            for t in transitions:
                print(format_transition(t, wave_of.get((t.store_id, t.part))), file=self.stream)
            print(file=self.stream, flush=True)
        for w in upd.closed:
            log.info("wave closed", extra={"fields": {"wave": w.wave_id, "store": w.store_id, "skus": len(w.members)}})

        if upd.opened or purchase_keys:
            (self.notifier or notify_cycle)(
                upd.opened, purchase_keys, self.tracker.is_available, at,
                simulated=self.simulated, out=self.stream)
        return outcome

    def _to_sink(self, outcome: CheckOutcome, at: datetime, newly: set[Key], upd: WaveUpdate,
                  transitions: Sequence[Transition], wave_of: Mapping[Key, Wave], duration_s: float,
                  suffix: str, footer: str, tunnel: str | None) -> None:
        import tui
        detail = render_matrix(self.poll_no, at, outcome, newly, len(upd.opened), duration_s, color=False,
                               title_suffix=suffix, footer=footer).splitlines()
        detail += [format_transition(t, wave_of.get((t.store_id, t.part))) for t in transitions]
        available = sum(1 for o in outcome.results.values() if o.status is Status.AVAILABLE)
        kinds = sorted({e.kind for e in outcome.errors})
        blocked = any(k in BLOCKED_KINDS for k in kinds)
        style = tui.NEW if (newly or upd.opened) else tui.BLOCKED if blocked else tui.ERROR if kinds else tui.OK
        state = ("HTTP 541/403" if blocked else "errors: " + ",".join(kinds)) if kinds else "ok"
        summary = (f"#{self.poll_no} {local(at).strftime('%H:%M:%S')}  {available}/{len(ALL_KEYS)}  "
                   f"{tunnel or ('no tunnel' if self.vpn_label is not None else 'direct')}  "
                   f"req {outcome.requests}  {state}{suffix}")
        if newly:
            names = [f"{STORES[sid].name} {SKU_BY_PART[p].label}" for sid, p in sorted(newly)]
            summary += "  NEW: " + ", ".join(names[:2]) + (f" +{len(names) - 2} more" if len(names) > 2 else "")
        # Confirmed state, not this poll's raw results: a store that errored this poll keeps its last status.
        available_now = frozenset(k for k in ALL_KEYS if self.tracker.is_available(k))
        self.sink.add_poll(tui.PollEntry(self.poll_no, local(at), summary, detail, style, found=frozenset(newly)),
                           available=available, tunnel=tunnel, available_keys=available_now)

    def say(self, text: str) -> None:
        print(f"[{fmt_local(self.wall())}] {text}", file=self.stream, flush=True)


def regional_zips() -> tuple[str, ...]:
    """The region keys (one request each per poll) for the current store set."""
    return tuple(dict.fromkeys(s.poll_zip for s in STORES.values()))


def run_loop(
    monitor: Monitor,
    *,
    stop: threading.Event,
    backoff: Backoff | None = None,
    clock: Callable[[], float] = time.monotonic,
    max_cycles: int | None = None,
    rng: random.Random | None = None,
    network: Any = None,
) -> None:
    """Poll until `stop` is set. Polls are strictly sequential.

    A poll starts at the later of its fixed-step schedule slot (no drift,
    no catch-up bursts) and the end of any cooldown. During a cooldown nothing is sent; when it
    expires a single-region probe runs, and only a clean probe resumes full
    polls. 429/5xx/network errors use the exponential Backoff instead.

    `network` (a wg_manager.NetworkGate) is asked before each poll whether
    network operations are permitted; tunnel rotation happens only inside
    that call, between polls. Scheduled rotation never changes the
    cooldown or backoff. On HTTP 541 the gate may instead switch to a
    different tunnel: then no cooldown starts and the next full poll goes
    out on its schedule through the new tunnel. Once the gate
    declines (switching exhausted), the normal cooldown applies. 403 and
    429 are never handled by switching.
    """
    backoff = backoff or Backoff(monitor.interval)
    cooldown = monitor.cooldown
    rng = rng or random.Random()
    next_due = clock()
    cycles = 0
    while not stop.is_set():
        now = clock()
        start = next_due
        if cooldown.active and cooldown.until is not None and cooldown.until > start:
            start = cooldown.until
        probe = cooldown.active
        if start > now + 1e-6:
            stop.wait(start - now)
            continue
        if network is not None:
            if not network.before_cycle():
                stop.wait(network.retry_after_s)
                continue
            if stop.is_set():
                return

        outcome = monitor.poll(zips=[rng.choice(regional_zips())] if probe else None)
        cycles += 1
        blocked_541 = any(e.kind == "blocked" for e in outcome.errors)
        if network is not None:
            network.after_cycle(sum(1 for e in outcome.errors if e.kind == "network"), blocked=blocked_541)
        if max_cycles is not None and cycles >= max_cycles:
            return
        t = clock()
        if blocked_541 and network is not None:
            refused_ip = network.public_ip
            if network.on_blocked():
                monitor.say(f"Apple refused requests (HTTP 541) from {refused_ip or 'this tunnel'}; switching tunnel "
                            f"({network.block_switches}/{network.block_limit}) instead of cooling down.")
                if probe:
                    cooldown.clear()
                next_due = t
                continue
        if any(e.kind in BLOCKED_KINDS for e in outcome.errors):
            duration = cooldown.trip(t)
            resume_at = monitor.wall() + timedelta(seconds=duration)
            monitor.say(f"Apple refused requests (HTTP 541/403). Sending nothing for {duration / 60:.0f} min; "
                        f"one probe request at {fmt_local(resume_at, with_date=False)}.")
            next_due = t + duration
            continue
        if probe and not outcome.errors:
            cooldown.clear()
            backoff.next_delay([])
            monitor.say("Probe succeeded; resuming full polls.")
            next_due = t
            continue
        next_due += backoff.next_delay(outcome.errors)
        if next_due < t:
            next_due = t


STORE_CACHE_MAX_AGE = timedelta(days=7)


def setup_stores(db: Any = None, *, refresh: bool = False, allow_discovery: bool = True,
                 fetch: Callable[[str], Any] | None = None) -> str:
    """Apply the store set for this run and say where it came from.

    Without DUO_ZIPS: the built-in Florida set. With it: the cached list for the
    same configuration if under STORE_CACHE_MAX_AGE old, otherwise one lookup per
    ZIP (through the tunnel when one is up), cached in `db`. With
    `allow_discovery=False` (no requests allowed), a stale cache is used as is.
    Raises stores_config.StoreConfigError.
    """
    from inventory_model import Store, configure_florida_default, configure_stores
    from stores_config import StoreConfigError, discover, from_env
    cfg = from_env(os.environ)
    if cfg is None:
        configure_florida_default()
        reset_zip_picker()
        return "the built-in Florida store list (set DUO_ZIPS in .env to use your own ZIP codes)"
    cached = None if (refresh or db is None) else db.load_store_set(cfg.fingerprint)
    now = utcnow()
    if cached is not None and (now - cached[2] <= STORE_CACHE_MAX_AGE or not allow_discovery):
        rows, region_zips, saved = cached
        configure_stores([Store(r["store_id"], r["name"], r["city"], r["zip_code"], r["region"], r["poll_zip"])
                          for r in rows], region_zips)
        reset_zip_picker()
        age_h = (now - saved).total_seconds() / 3600
        age = f"{age_h:.0f} h" if age_h < 48 else f"{age_h / 24:.0f} days"
        return f"{len(STORES)} stores {cfg.describe()} (saved list, {age} old; --refresh-stores to look again)"
    if not allow_discovery:
        raise StoreConfigError("no saved store list for DUO_ZIPS yet; run `python3 iphone_monitor.py --list-stores` "
                               "once to look the stores up")
    found, region_zips = discover(cfg, fetch or fetch_inventory)
    if db is not None:
        db.save_store_set(cfg.fingerprint, cfg.describe(), found, region_zips, now)
    configure_stores([f.store for f in found], region_zips)
    reset_zip_picker()
    return f"{len(STORES)} stores {cfg.describe()} (looked up just now, {len(cfg.zips)} request(s))"


def run_list_stores(args: argparse.Namespace, out: Any = None, fetch: Callable[[str], Any] | None = None) -> int:
    """Resolve and print the monitored stores (looking them up if needed), then exit."""
    from inventory_db import HistoryDB
    from stores_config import StoreConfigError, from_env
    stream = out if out is not None else sys.stdout
    db = HistoryDB(args.db) if from_env(os.environ) is not None else None
    try:
        source = setup_stores(db, refresh=args.refresh_stores, fetch=fetch)
        distances: dict[str, float] = {}
        cfg = from_env(os.environ)
        if db is not None and cfg is not None:
            saved = db.load_store_set(cfg.fingerprint)
            distances = {r["store_id"]: r["distance_mi"] for r in saved[0]} if saved else {}
    except StoreConfigError as exc:
        print(f"Stores: {exc}", file=sys.stderr)
        return 1
    finally:
        if db is not None:
            db.close()
    print(f"Monitoring {source}:", file=stream)
    w = name_width()
    print(f"  {'STORE':<{w}} {'ID':<6} {'CITY':<22} {'ZIP':<6} {'MILES':>6}  REQUESTED VIA", file=stream)
    for s in STORES.values():
        miles = f"{distances[s.store_id]:.1f}" if s.store_id in distances else "-"
        print(f"  {s.name:<{w}} {s.store_id:<6} {s.city:<22} {s.zip_code:<6} {miles:>6}  "
              f"{s.poll_zip} (rotates {len(REGION_ZIPS.get(s.poll_zip, ()))} ZIPs)", file=stream)
    print(f"{len(STORES)} stores, {len(regional_zips())} request(s) per poll", file=stream, flush=True)
    return 0


def run_once(check: Callable[[], CheckOutcome] | None = None, out: Any = None) -> int:
    """One check of every store and SKU. Prints the matrix; no alerts, no history."""
    stream = out if out is not None else sys.stdout
    started = utcnow()
    t0 = time.monotonic()
    outcome = check() if check is not None else check_all(picker=ZIP_PICKER)
    print(render_matrix(1, started, outcome, set(), 0, time.monotonic() - t0, color=use_color(stream)),
          file=stream)
    print(LEGEND, file=stream, flush=True)
    return 0 if len(outcome.results) == len(ALL_KEYS) and all(
        o.status.is_confirmed for o in outcome.results.values()) else 2


def simulated_outcome(**_: Any) -> CheckOutcome:
    """Every key unavailable except 1TB Night Sky at the first monitored store."""
    results = {(sid, p): Observation(sid, p, Status.UNAVAILABLE, "SIMULATED") for sid, p in ALL_KEYS}
    first = next(iter(STORES))
    results[(first, PREFERRED_PARTS[0])] = Observation(first, PREFERRED_PARTS[0], Status.AVAILABLE, "SIMULATED")
    return CheckOutcome(results, [], 0)


def run_dry_run(out: Any = None, wait_for_sound: float = 5.0) -> int:
    """Simulate 1TB Night Sky at the first monitored store through the real alert path.

    No HTTP requests and no database: the Monitor has db=None and
    simulated=True, so nothing is recorded.
    """
    procs: list[Any] = []

    def notifier(*args: Any, **kwargs: Any) -> Any:
        proc = notify_cycle(*args, **kwargs)
        procs.append(proc)
        return proc

    mon = Monitor(DEFAULT_INTERVAL, timedelta(minutes=DEFAULT_WAVE_WINDOW_MIN), db=None,
                  check=simulated_outcome, notifier=notifier, out=out, simulated=True)
    mon.poll()
    for proc in procs:
        if proc is not None:
            # Let the chime finish before exiting so the test is audible.
            try:
                proc.wait(timeout=wait_for_sound)
            except subprocess.TimeoutExpired:
                pass
    print("Dry run complete. No inventory requests were made and no history was written.",
          file=out if out is not None else sys.stdout, flush=True)
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _interval(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}") from None
    if not seconds >= MIN_INTERVAL:  # also rejects NaN
        raise argparse.ArgumentTypeError(f"interval must be >= {MIN_INTERVAL:g} seconds")
    if seconds == float("inf"):
        raise argparse.ArgumentTypeError("interval must be finite")
    return seconds


def _positive_minutes(value: str) -> float:
    try:
        m = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}") from None
    if not (0 < m < float("inf")):
        raise argparse.ArgumentTypeError("must be a positive number of minutes")
    return m


def _positive_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from None
    if n < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return n


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Observe in-store pickup inventory for all iPhone Duo configurations "
        "at the Apple Stores near your ZIP code(s) and analyze restock timing."
    )
    p.add_argument("--interval", type=_interval, default=DEFAULT_INTERVAL,
                   help=f"seconds between polls (default {DEFAULT_INTERVAL:g}, min {MIN_INTERVAL:g})")
    p.add_argument("--wave-window", type=_positive_minutes, default=DEFAULT_WAVE_WINDOW_MIN, metavar="MIN",
                   help=f"minutes from a wave's first transition during which later transitions "
                        f"join it (default {DEFAULT_WAVE_WINDOW_MIN:g})")
    p.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"history database (default {DEFAULT_DB.name})")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="simulate 1TB Night Sky at the first monitored store; no API requests, no history")
    mode.add_argument("--once", action="store_true",
                      help="check all stores once, print the matrix, exit; no alerts, no history")
    mode.add_argument("--waves", action="store_true", help="list recorded restock waves")
    mode.add_argument("--report", action="store_true", help="timing and configuration analysis")
    mode.add_argument("--export-csv", nargs="?", const="", metavar="DIR",
                      help="export history tables to CSV (default ./exports/<timestamp>)")
    mode.add_argument("--list-stores", action="store_true",
                      help="show the stores that will be monitored (looks them up from DUO_ZIPS if needed), then exit")
    mode.add_argument("--vpn-list", action="store_true",
                      help="list the WireGuard-app tunnels the monitor may use, without connecting")
    mode.add_argument("--vpn-test", action="store_true",
                      help="connect a random WireGuard-app tunnel, verify it, report, disconnect, exit")
    mode.add_argument("--vpn-status", action="store_true",
                      help="show the tunnel the monitor is using: interface, health, public IP and location")
    p.add_argument("--days", type=_positive_int, help="limit --report/--waves to the last N days")
    p.add_argument("--store", help="limit --report/--waves to one store (name or ID, e.g. 'Waterside Shops')")
    p.add_argument("--verbose", action="store_true", help="detailed diagnostic logging")
    p.add_argument("--refresh-stores", action="store_true",
                   help="look the stores up again from DUO_ZIPS instead of using the saved list")
    p.add_argument("--tui", action="store_true",
                   help="interactive full-screen view: one collapsible line per poll (monitoring only)")
    vpn = p.add_argument_group("WireGuard")
    vpn.add_argument("--vpn-enabled", action="store_true",
                     help="run polls only through a verified application-managed WireGuard tunnel")
    rot = vpn.add_mutually_exclusive_group()
    rot.add_argument("--vpn-rotate-minutes", type=_positive_minutes, metavar="MIN",
                     help="switch to another tunnel every MIN minutes (between polls)")
    rot.add_argument("--vpn-rotate-every-cycles", type=_positive_int, metavar="N",
                     help="switch to another tunnel after N completed polls")
    return p


def _open_existing_db_quiet(path: Path) -> Any:
    """The history database if it already exists (for the store cache), else None; never creates one."""
    from inventory_db import HistoryDB
    return HistoryDB(path) if path.exists() else None


def _open_existing_db(path: Path) -> Any:
    from inventory_db import HistoryDB
    if not path.exists():
        print(f"No history database at {path}. Start monitoring first.", file=sys.stderr)
        return None
    return HistoryDB(path)


# --------------------------------------------------------------------------
# WireGuard integration
# --------------------------------------------------------------------------

VPN_EXIT_FAILED = 3  # tunnel could not be established, verified or torn down safely


CONSOLE_SINK: Callable[[str], None] | None = None  # set while the TUI owns the screen


def vpn_say(text: str) -> None:
    if CONSOLE_SINK is not None:
        CONSOLE_SINK(text)
        return
    print(f"[{fmt_local(utcnow(), with_date=False)}] {text}", flush=True)


def _vpn_platform() -> Any:
    from wg_macos import MacPlatform
    return MacPlatform()


VPN_MANAGER_OPTIONS: dict[str, Any] = {}  # extra WireGuardManager keyword arguments (tests inject fakes)


def _vpn_manager(args: argparse.Namespace, store: Any, platform: Any = None) -> Any:
    from wg_manager import WireGuardManager
    return WireGuardManager(platform or _vpn_platform(), store, console=vpn_say,
                            **VPN_MANAGER_OPTIONS)


def _vpn_lock(db_path: Path) -> Any:
    from wg_manager import ProcessLock
    lock = ProcessLock(db_path.with_name(db_path.name + ".vpn.lock"))
    lock.acquire()
    return lock


def shielded(fn: Callable[[], Any]) -> Any:
    """Run `fn` (network restoration) with SIGINT/SIGTERM deferred to a log line."""
    if threading.current_thread() is not threading.main_thread():
        return fn()

    def note(signum: int, _frame: Any) -> None:
        log.warning("signal received during network restoration; finishing cleanup first",
                    extra={"fields": {"signal": signum}})
        print("Finishing network restoration before exiting…", file=sys.stderr, flush=True)

    old_int = signal.signal(signal.SIGINT, note)
    old_term = signal.signal(signal.SIGTERM, note)
    try:
        return fn()
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)


def _vpn_cleanup(mgr: Any) -> bool:
    if mgr is None:
        return True
    ok = bool(shielded(mgr.cleanup))
    if not ok:
        print("WireGuard teardown or network restoration did not complete. See the steps above; the next "
              "start will retry recovery.", file=sys.stderr, flush=True)
    return ok


def _vpn_error(exc: Exception) -> None:
    category = getattr(exc, "category", type(exc).__name__)
    log.error("wireguard failure", extra={"fields": {"category": category, "error": str(exc)}})
    print(f"WireGuard: {exc} [{category}]", file=sys.stderr, flush=True)


def run_vpn_list(args: argparse.Namespace, out: Any = None, platform: Any = None) -> int:
    """Every WireGuard-app tunnel the monitor may use, with its endpoint and status."""
    from wg_manager import MemoryStore
    stream = out if out is not None else sys.stdout
    tunnels = _vpn_manager(args, MemoryStore(), platform).discover_tunnels()
    print(f"{len(tunnels)} WireGuard-app tunnel(s)", file=stream)
    print(f"{'TUNNEL':<24} {'ENDPOINT':<24} STATUS", file=stream)
    for t in sorted(tunnels, key=lambda t: t.name):
        print(f"{t.name:<24} {t.endpoint or '-':<24} {t.service.status}", file=stream)
    if not tunnels:
        print("Import tunnels in the WireGuard app (Import Tunnel(s) from File).", file=stream)
    return 0 if tunnels else 1


def run_vpn_test(args: argparse.Namespace, platform: Any = None) -> int:
    """Random config: connect, verify, report, disconnect cleanly."""
    from inventory_db import HistoryDB
    from wg_errors import WireGuardError
    db = HistoryDB(args.db)
    mgr = lock = None
    rc = 0
    try:
        lock = _vpn_lock(args.db)
        mgr = _vpn_manager(args, db, platform)
        mgr.recover_orphans()
        status = mgr.connect(reason="vpn_test")
        print("\nWireGuard test result: PASS", flush=True)
        for line in status.lines():
            print(f"  {line}", flush=True)
    except WireGuardError as exc:
        _vpn_error(exc)
        print("WireGuard test result: FAIL", flush=True)
        rc = 1
    except KeyboardInterrupt:
        rc = 130
    finally:
        if not _vpn_cleanup(mgr):
            rc = VPN_EXIT_FAILED
        if lock is not None:
            lock.release()
        db.close()
    return rc


def run_vpn_status(args: argparse.Namespace, platform: Any = None, out: Any = None) -> int:
    """Exit 0 when the app tunnel is up and healthy, 1 when there is none, 2 when unhealthy."""
    from inventory_db import HistoryDB
    from wg_errors import WireGuardError
    stream = out if out is not None else sys.stdout
    if not args.db.exists():
        print("No application-managed tunnel (no history database).", file=stream)
        return 1
    db = HistoryDB(args.db)
    try:
        plat = platform or _vpn_platform()
        mgr = _vpn_manager(args, db, plat)
        row = db.latest_vpn_session()
        service_id = row.get("service_id") if row else None
        live = plat.status(service_id) if service_id else None
        if row is None or (row["ended_utc"] is not None and live not in ("Connected", "Connecting")):
            print("No application-managed tunnel is active.", file=stream)
            if row is not None:
                print(f"Last session: {row['config_name']} ended {fmt_local(from_utc_text(row['ended_utc']))} "
                      f"({row['teardown_outcome'] or 'unknown outcome'})", file=stream)
            return 1
        if row["ended_utc"] is not None:
            print(f"WireGuard tunnel {row['wg_name']!r} is {live}, but this application's last session for it "
                  "is closed; it was started outside the application.", file=stream)
            return 2
        mgr.active = mgr._tunnel_from_row(row)
        healthy = live == "Connected" and bool(row["interface"]) and row["interface"] in plat.interfaces()
        try:
            ip = mgr.get_public_ip() if live == "Connected" else None
        except WireGuardError as exc:
            ip, healthy = None, False
            print(f"Public IP check failed: {exc}", file=stream)
        print(f"WireGuard tunnel: {row['wg_name']} ({live})", file=stream)
        print(f"Interface: {row['interface'] or '-'}", file=stream)
        print(f"Recorded state: {row['state']}", file=stream)
        if ip is not None:
            match = "matches" if ip == row["public_ip"] else f"differs from recorded {row['public_ip'] or '-'}"
            healthy = healthy and ip == row["public_ip"]
            print(f"Verified public IP: {ip} ({match})", file=stream)
            if row.get("location"):
                print(f"Location: {row['location']}", file=stream)
        print(f"Health: {'HEALTHY' if healthy else 'UNHEALTHY'}", file=stream, flush=True)
        return 0 if healthy else 2
    except WireGuardError as exc:
        _vpn_error(exc)
        return 2
    finally:
        db.close()


def run_once_vpn(args: argparse.Namespace, platform: Any = None) -> int:
    from inventory_db import HistoryDB
    from stores_config import StoreConfigError
    from wg_errors import WireGuardError
    db = HistoryDB(args.db)
    mgr = lock = None
    try:
        lock = _vpn_lock(args.db)
        mgr = _vpn_manager(args, db, platform)
        mgr.recover_orphans()
        mgr.connect(reason="once")
        print(f"Stores: {setup_stores(db, refresh=args.refresh_stores)}", flush=True)
        rc = run_once()
    except WireGuardError as exc:
        _vpn_error(exc)
        rc = VPN_EXIT_FAILED
    except StoreConfigError as exc:
        print(f"Stores: {exc}", file=sys.stderr, flush=True)
        rc = 1
    finally:
        if not _vpn_cleanup(mgr):
            rc = VPN_EXIT_FAILED
        if lock is not None:
            lock.release()
        db.close()
    return rc


TUI_APP_FACTORY: Callable[..., Any] | None = None  # tests substitute a fake UI for tui.TuiApp


class _ModelLogHandler(logging.Handler):
    """Log records as TUI event lines, so nothing writes over the curses screen."""

    def __init__(self, model: Any, clock: Callable[[], datetime]) -> None:
        super().__init__(logging.WARNING)
        self.model, self.clock = model, clock
        self.setFormatter(KeyValueFormatter("%(levelname)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        import tui
        self.model.add_event(self.format(record), self.clock(), tui.ERROR if record.levelno >= logging.ERROR
                             else tui.EVENT)


def run_with_tui(mon: Monitor, stop: threading.Event, gate: Any, args: argparse.Namespace) -> None:
    """Run the monitor loop in a worker thread while the curses UI owns the main thread.

    The UI returns once the worker has stopped (after `q`, Ctrl+C or SIGTERM).
    Output is restored before returning, so the caller's teardown messages
    print to the normal terminal. An exception from the loop is re-raised here.
    """
    global CONSOLE_SINK
    import tui
    clock = lambda: local(utcnow())  # noqa: E731
    model = tui.TuiModel()
    old_stream, old_handlers, old_level = mon.stream, log.handlers[:], log.level
    mon.sink, mon.stream, mon.color = model, tui.EventWriter(model, clock), False
    handlers: list[logging.Handler] = [_ModelLogHandler(model, clock)]
    if args.verbose:
        fh = logging.FileHandler(args.db.with_name("iphone_monitor.log"))
        fh.setFormatter(KeyValueFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        handlers.append(fh)
    log.handlers[:] = handlers
    log.setLevel(logging.DEBUG if args.verbose else logging.WARNING)
    CONSOLE_SINK = lambda text: model.add_event(text, clock())  # noqa: E731
    errors: list[BaseException] = []

    def work() -> None:
        try:
            run_loop(mon, stop=stop, network=gate)
        except BaseException as exc:  # re-raised in the main thread below
            errors.append(exc)

    worker = threading.Thread(target=work, name="monitor-loop", daemon=True)
    worker.start()
    try:
        (TUI_APP_FACTORY or tui.TuiApp)(model, stop, worker, clock, mon.interval).run()
    finally:
        stop.set()
        worker.join()
        CONSOLE_SINK = None
        for h in log.handlers:
            h.close()
        log.handlers[:], log.level = old_handlers, old_level
        mon.sink, mon.stream = None, old_stream
        events = [e for e in model.entries if isinstance(e, tui.EventEntry)][:8][::-1]
        if events:
            print("Last events:", flush=True)
            for e in events:
                print(f"  {e.at.strftime('%H:%M:%S')}  {e.text}", flush=True)
        print(f"{model.polls} poll(s) this session.", flush=True)
    if errors:
        raise errors[0]


def run_monitor(args: argparse.Namespace, platform: Any = None) -> int:
    from inventory_db import HistoryDB
    from stores_config import StoreConfigError
    from wg_errors import WireGuardError
    db = HistoryDB(args.db)
    started = utcnow()
    run_id = db.start_run(started, args.interval, args.wave_window * 60)

    stop = threading.Event()
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        # Ensure Ctrl+C raises KeyboardInterrupt even if the parent shell
        # started us with SIGINT ignored.
        signal.signal(signal.SIGINT, signal.default_int_handler)

    mon: Monitor | None = None
    mgr = lock = gate = None
    rc = 0
    try:
        if args.vpn_enabled:
            from wg_manager import NetworkGate, RotationPolicy
            lock = _vpn_lock(args.db)
            mgr = _vpn_manager(args, db, platform)
            policy = RotationPolicy.from_options(args.vpn_rotate_minutes, args.vpn_rotate_every_cycles)
            gate = NetworkGate(mgr, policy)
            print(f"WireGuard: connecting before the first poll ({policy.describe()}). "
                  "Polls run only while the tunnel is verified.", flush=True)
            gate.start()
        print(f"Stores: {setup_stores(db, refresh=args.refresh_stores)}", flush=True)
        mon = Monitor(args.interval, timedelta(minutes=args.wave_window), db=db, run_id=run_id,
                      vpn_session=(lambda: gate.session_id) if gate is not None else None,
                      vpn_label=(lambda: gate.label) if gate is not None else None)
        mon.seed_from_history()
        if gate is not None and mon.cooldown.active:
            refused_ip = db.last_blocked_poll_ip()
            if gate.resume_check(refused_ip):
                mon.cooldown.clear()
                print(f"Previous run ended in an HTTP 541 cooldown from {refused_ip}; the new tunnel's IP "
                      f"{gate.public_ip} differs, so polling resumes now.", flush=True)
        print(f"Monitoring {len(SKUS)} iPhone Duo SKUs at {len(STORES)} stores every {args.interval:g}s "
              f"(wave window {args.wave_window:g} min). History: {args.db}. Ctrl+C to stop.", flush=True)
        if mon.cooldown.active:
            print("Resuming a cooldown from the previous run.", flush=True)
        print(LEGEND, flush=True)
        print("Purchase page opens for: " + ", ".join(SKU_BY_PART[p].code for p in PURCHASE_ALERT_PARTS)
              + " (highest priority first; set DUO_ALERT_PARTS in .env)", flush=True)
        if args.tui:
            run_with_tui(mon, stop, gate, args)
        else:
            run_loop(mon, stop=stop, network=gate)
    except KeyboardInterrupt:
        pass
    except WireGuardError as exc:
        _vpn_error(exc)
        print("Polling stopped: no verified tunnel, and polls never run without one.", file=sys.stderr, flush=True)
        rc = VPN_EXIT_FAILED
    except StoreConfigError as exc:
        print(f"Stores: {exc}", file=sys.stderr, flush=True)
        rc = 1
    finally:
        try:
            if not _vpn_cleanup(mgr):
                rc = VPN_EXIT_FAILED
            if lock is not None:
                lock.release()
        finally:
            try:
                db.end_run(run_id, utcnow(), mon.waves.close_all() if mon is not None else ())
            finally:
                db.close()
    print("\nStopped.", flush=True)
    return rc


def _check_tui_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if not args.tui:
        return
    if (args.dry_run or args.once or args.report or args.waves or args.export_csv is not None
            or args.vpn_list or args.vpn_test or args.vpn_status or args.list_stores):
        parser.error("--tui applies only to monitoring")
    if not (sys.stdout.isatty() and sys.stdin.isatty()):
        parser.error("--tui needs an interactive terminal")


def _check_vpn_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    vpn_mode = args.vpn_list or args.vpn_test or args.vpn_status
    rotating = args.vpn_rotate_minutes is not None or args.vpn_rotate_every_cycles is not None
    if args.vpn_enabled and (args.dry_run or args.report or args.waves or args.export_csv is not None or vpn_mode):
        parser.error("--vpn-enabled applies only to monitoring and --once")
    if rotating and not args.vpn_enabled:
        parser.error("--vpn-rotate-minutes/--vpn-rotate-every-cycles require --vpn-enabled")
    if rotating and args.once:
        parser.error("rotation does not apply to --once")


def _apply_env(parser: argparse.ArgumentParser) -> None:
    """Load .env (real environment variables win) and apply DUO_ALERT_PARTS."""
    global PURCHASE_ALERT_PARTS
    from app_env import EnvFileError, load_env
    from inventory_model import parse_alert_parts
    try:
        defined = load_env(DEFAULT_ENV_FILE)
    except EnvFileError as exc:
        parser.error(str(exc))
    if defined:
        log.info("loaded .env", extra={"fields": {"path": str(DEFAULT_ENV_FILE), "keys": sorted(defined)}})
    value = os.environ.get("DUO_ALERT_PARTS")
    if value:
        try:
            PURCHASE_ALERT_PARTS = parse_alert_parts(value)
        except ValueError as exc:
            parser.error(f"DUO_ALERT_PARTS: {exc}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose)
    _apply_env(parser)
    analysis = args.report or args.waves
    if (args.days or args.store) and not analysis:
        parser.error("--days and --store apply only to --report and --waves")
    _check_vpn_args(parser, args)
    _check_tui_args(parser, args)
    from stores_config import StoreConfigError, from_env
    try:
        from_env(os.environ)
    except StoreConfigError as exc:
        parser.error(str(exc))

    if args.dry_run:
        from stores_config import StoreConfigError
        try:
            db = _open_existing_db_quiet(args.db)
            try:
                setup_stores(db, allow_discovery=False)
            finally:
                if db is not None:
                    db.close()
        except StoreConfigError as exc:
            print(f"Stores: {exc}", file=sys.stderr)
            return 1
        return run_dry_run()
    if args.vpn_list:
        return run_vpn_list(args)
    if args.vpn_test:
        return run_vpn_test(args)
    if args.vpn_status:
        return run_vpn_status(args)
    if args.list_stores:
        return run_list_stores(args)
    if args.once:
        if args.vpn_enabled:
            return run_once_vpn(args)
        from stores_config import StoreConfigError
        try:
            db = _open_existing_db_quiet(args.db)
            try:
                print(f"Stores: {setup_stores(db, refresh=args.refresh_stores)}", flush=True)
            finally:
                if db is not None:
                    db.close()
        except StoreConfigError as exc:
            print(f"Stores: {exc}", file=sys.stderr)
            return 1
        return run_once()
    if analysis or args.export_csv is not None:
        import inventory_report as rep
        from stores_config import StoreConfigError
        db = _open_existing_db_quiet(args.db)
        try:
            setup_stores(db, allow_discovery=False)
        except StoreConfigError as exc:  # reports still work on the built-in list
            print(f"Stores: {exc}", file=sys.stderr)
        store = None
        if args.store:
            store = rep.resolve_store(args.store)
            if store is None:
                if db is not None:
                    db.close()
                parser.error(f"unknown store {args.store!r}; choose from: "
                             + ", ".join(s.name for s in STORES.values()))
        if db is None:
            _open_existing_db(args.db)  # prints the "no history database" message
            return 1
        try:
            if args.waves:
                since = utcnow() - timedelta(days=args.days) if args.days else None
                print(rep.format_waves(db, since, store))
            elif args.report:
                print(rep.format_report(db, args.days, store), end="")
            else:
                out_dir = Path(args.export_csv) if args.export_csv else (
                    Path("exports") / local(utcnow()).strftime("%Y%m%d-%H%M%S"))
                for path, n in rep.export_csv(db, out_dir):
                    print(f"{path}  ({n} rows)")
        finally:
            db.close()
        return 0
    return run_monitor(args)


if __name__ == "__main__":
    sys.exit(main())
