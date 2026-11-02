"""Historical wave listing, timing-pattern analysis, and CSV export.

Everything here describes API observations. A wave is the moment Apple's
pickup endpoint was first seen reporting stock; it is not a delivery time,
and a date with no observed wave does not mean no shipment arrived.
"""

from __future__ import annotations

import csv
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from itertools import combinations
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence

from inventory_db import HistoryDB
from inventory_model import (
    LOCAL_TZ, PREFERRED_PARTS, SKU_BY_PART, SKUS, STORES, Store, from_utc_text, local, to_utc_text,
)
from inventory_waves import NEW_AVAILABILITY

BUCKET_S = 300  # coverage resolution: 5 minutes
WINDOW_WIDTHS = (5, 10, 15, 30)  # minutes
MIN_SUPPORT_DATES = 3
MIN_COVERED_DATES = 5
CONSISTENT_SUPPORT_DATES = 5
CONSISTENT_RATIO = 0.6
RECURRING_DELTA_MIN = 15
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

CAVEAT = (
    "Note: waves are observed changes in Apple's pickup API, not verified deliveries. "
    "A date without an observed wave does not mean no shipment occurred."
)


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


@dataclass
class MemberRow:
    part: str
    first_at: datetime
    last_unavailable_at: datetime | None
    timing: str
    duration_s: float | None

    @property
    def code(self) -> str:
        return SKU_BY_PART[self.part].code if self.part in SKU_BY_PART else self.part


@dataclass
class WaveRow:
    wave_id: int
    store_id: str
    first_at: datetime
    last_at: datetime
    sku_count: int
    supporting_polls: int
    present_at_startup: bool
    gap_affected: bool
    coverage_gap: bool
    confidence: str
    event_class: str
    members: list[MemberRow] = field(default_factory=list)

    @property
    def store_name(self) -> str:
        s = STORES.get(self.store_id)
        return s.name if s else self.store_id

    @property
    def parts(self) -> set[str]:
        return {m.part for m in self.members}

    def offset_s(self, m: MemberRow) -> float:
        return (m.first_at - self.first_at).total_seconds()

    def first_poll_parts(self) -> set[str]:
        return {m.part for m in self.members if m.first_at == self.first_at}


def _dt(text: str | None) -> datetime | None:
    return from_utc_text(text) if text else None


def load_waves(db: HistoryDB, since: datetime | None = None,
               store_ids: Iterable[str] | None = None) -> list[WaveRow]:
    sql = "SELECT * FROM store_restock_waves WHERE 1 = 1"
    params: list[object] = []
    if since is not None:
        sql += " AND first_detected_utc >= ?"
        params.append(to_utc_text(since))
    ids = list(store_ids) if store_ids is not None else None
    if ids is not None:
        sql += f" AND store_id IN ({','.join('?' * len(ids))})"
        params += ids
    sql += " ORDER BY first_detected_utc, wave_id"
    waves = {
        r["wave_id"]: WaveRow(
            r["wave_id"], r["store_id"], from_utc_text(r["first_detected_utc"]),
            from_utc_text(r["last_transition_utc"]), r["sku_count"], r["supporting_polls"],
            bool(r["present_at_startup"]), bool(r["gap_affected"]), bool(r["coverage_gap"]),
            r["confidence"], r["event_class"])
        for r in db.query(sql, params)
    }
    if waves:
        rows = db.query(
            f"SELECT * FROM restock_wave_members WHERE wave_id IN ({','.join('?' * len(waves))})"
            " ORDER BY first_available_utc, part", list(waves))
        for r in rows:
            waves[r["wave_id"]].members.append(MemberRow(
                r["part"], from_utc_text(r["first_available_utc"]), _dt(r["last_unavailable_utc"]),
                r["timing"], r["duration_s"]))
    return list(waves.values())


def load_coverage(db: HistoryDB, since: datetime | None = None) -> dict[str, set[int]]:
    """5-minute buckets (epoch // 300) in which each store returned usable data."""
    sql = ("SELECT r.store_id, CAST(strftime('%s', p.started_utc) AS INTEGER) / ? AS b"
           " FROM polls p JOIN poll_store_results r ON r.poll_id = p.poll_id"
           " WHERE r.outcome IN ('ok', 'partial')")
    params: list[object] = [BUCKET_S]
    if since is not None:
        sql += " AND p.started_utc >= ?"
        params.append(to_utc_text(since))
    sql += " GROUP BY r.store_id, b"
    cov: dict[str, set[int]] = defaultdict(set)
    for r in db.query(sql, params):
        cov[r["store_id"]].add(int(r["b"]))
    return cov


# --------------------------------------------------------------------------
# Coverage helpers
# --------------------------------------------------------------------------


def _local_midnight(d: date) -> datetime:
    return datetime.combine(d, time(0), LOCAL_TZ)


def _buckets_between(start: datetime, end: datetime) -> range:
    s = int(start.timestamp()) // BUCKET_S
    e = (int(end.timestamp()) - 1) // BUCKET_S
    return range(s, e + 1)


def date_coverage(buckets: set[int]) -> dict[date, float]:
    """Fraction of each local calendar day with usable polling data."""
    counts: Counter[date] = Counter()
    for b in buckets:
        counts[datetime.fromtimestamp(b * BUCKET_S, LOCAL_TZ).date()] += 1
    out = {}
    for d, n in counts.items():
        total = len(_buckets_between(_local_midnight(d), _local_midnight(d + timedelta(days=1))))
        out[d] = min(1.0, n / total)
    return out


def window_covered(buckets: set[int], d: date, start_min: float, end_min: float,
                   required: float = 0.5) -> bool:
    """True when at least `required` of the 5-minute buckets in the window had data."""
    start = _local_midnight(d) + timedelta(minutes=start_min)
    end = _local_midnight(d) + timedelta(minutes=max(end_min, start_min + BUCKET_S / 60))
    rng = _buckets_between(start, end)
    if not rng:
        return False
    return sum(1 for b in rng if b in buckets) / len(rng) >= required


# --------------------------------------------------------------------------
# Timing analysis
# --------------------------------------------------------------------------


def minute_of_day(dt: datetime) -> float:
    t = local(dt)
    return t.hour * 60 + t.minute + t.second / 60


def hhmm(minutes: float) -> str:
    m = int(round(minutes))
    return f"{m // 60:02d}:{m % 60:02d}"


@dataclass
class WindowCandidate:
    width: int
    start_min: float
    end_min: float
    dates: list[date]
    times: list[float]  # earliest wave start per supporting date
    monitored_dates: int = 0
    covered_dates: int = 0

    @property
    def support(self) -> int:
        return len(self.dates)

    @property
    def spread_min(self) -> float:
        return max(self.times) - min(self.times) if self.times else 0.0

    @property
    def stdev_min(self) -> float | None:
        return statistics.pstdev(self.times) if len(self.times) >= 2 else None

    @property
    def assessment(self) -> str:
        return assess(self.support, self.covered_dates)


def assess(support: int, covered: int) -> str:
    if support < MIN_SUPPORT_DATES:
        return f"insufficient: {support} supporting date(s), need at least {MIN_SUPPORT_DATES}"
    if covered < MIN_COVERED_DATES:
        return f"insufficient: only {covered} date(s) had polling coverage of this window, need {MIN_COVERED_DATES}"
    ratio = support / covered if covered else 0.0
    if support >= CONSISTENT_SUPPORT_DATES and ratio >= CONSISTENT_RATIO:
        return (f"repeated in observed data on {support}/{covered} covered dates "
                "(descriptive only; not a confirmed schedule)")
    return f"weak: {support}/{covered} covered dates"


def best_window(points: Sequence[tuple[date, float]], width: int) -> WindowCandidate | None:
    """Time-of-day window of `width` minutes containing waves on the most distinct dates."""
    pts = sorted(points, key=lambda p: p[1])
    best: tuple[int, int, int] | None = None  # (distinct dates, i, j)
    j = 0
    for i in range(len(pts)):
        if j < i:
            j = i
        while j + 1 < len(pts) and pts[j + 1][1] - pts[i][1] <= width:
            j += 1
        n = len({d for d, _ in pts[i:j + 1]})
        if best is None or n > best[0]:
            best = (n, i, j)
    if best is None or best[0] < 2:
        return None
    _, i, j = best
    per_date: dict[date, float] = {}
    for d, m in pts[i:j + 1]:
        per_date[d] = min(m, per_date.get(d, m))
    dates = sorted(per_date)
    return WindowCandidate(width, pts[i][1], pts[j][1], dates, [per_date[d] for d in dates])


def consecutive_day_comparison(points: Sequence[tuple[date, float]]) -> list[tuple[date, float, float | None]]:
    """(date, earliest start minute, delta vs previous calendar day or None)."""
    earliest: dict[date, float] = {}
    for d, m in points:
        earliest[d] = min(m, earliest.get(d, m))
    out = []
    prev: date | None = None
    for d in sorted(earliest):
        delta = earliest[d] - earliest[prev] if prev is not None and d - prev == timedelta(days=1) else None
        out.append((d, earliest[d], delta))
        prev = d
    return out


@dataclass
class SkuOrder:
    code: str
    appearances: int = 0
    first_poll: int = 0
    offsets: list[float] = field(default_factory=list)


def sku_ordering(waves: Sequence[WaveRow]) -> tuple[list[SkuOrder], list[tuple[str, str, int, int]]]:
    """Per-SKU first-poll rates in multi-SKU waves, plus pairwise precedence.

    Pairs are (a, b, waves_where_a_strictly_preceded_b, waves_containing_both).
    """
    stats = {s.part: SkuOrder(s.code) for s in SKUS}
    before: Counter[tuple[str, str]] = Counter()
    shared: Counter[tuple[str, str]] = Counter()
    for w in waves:
        if len(w.members) < 2:
            continue
        for m in w.members:
            st = stats.setdefault(m.part, SkuOrder(m.code))
            st.appearances += 1
            off = w.offset_s(m)
            st.offsets.append(off)
            if off == 0:
                st.first_poll += 1
        times = {m.part: m.first_at for m in w.members}
        for a, b in combinations(sorted(times), 2):
            shared[(a, b)] += 1
            if times[a] < times[b]:
                before[(a, b)] += 1
            elif times[b] < times[a]:
                before[(b, a)] += 1
    out_pairs = []
    for (a, b), n in shared.items():
        ca, cb = SKU_BY_PART[a].code, SKU_BY_PART[b].code
        out_pairs.append((ca, cb, before[(a, b)], n))
        out_pairs.append((cb, ca, before[(b, a)], n))
    return [s for s in stats.values() if s.appearances], out_pairs


# --------------------------------------------------------------------------
# Report formatting
# --------------------------------------------------------------------------


def _bar(n: int, scale: int) -> str:
    return "#" * max(1, round(20 * n / scale)) if n else ""


def _median_time(minutes: Sequence[float]) -> str:
    return hhmm(statistics.median(minutes))


def resolve_store(name_or_id: str) -> Store | None:
    q = name_or_id.strip().lower()
    for s in STORES.values():
        if q in (s.store_id.lower(), s.name.lower()):
            return s
    matches = [s for s in STORES.values() if q in s.name.lower()]
    return matches[0] if len(matches) == 1 else None


def store_section(store: Store, waves: Sequence[WaveRow], buckets: set[int],
                  detailed: bool) -> list[str]:
    L: list[str] = []
    L.append(f"=== {store.name} [{store.store_id}], {store.city} ({store.region}) ===")
    dcov = date_coverage(buckets)
    monitored = sorted(dcov)
    timed = [w for w in waves if w.event_class == NEW_AVAILABILITY]
    other = Counter(w.event_class for w in waves if w.event_class != NEW_AVAILABILITY)
    L.append(f"Monitored dates: {len(monitored)}"
             + (f" (mean daily coverage {statistics.mean(dcov.values()):.0%})" if dcov else ""))
    L.append(f"Waves observed: {len(waves)}; with reliable timing: {len(timed)}"
             + (f"; excluded from timing: {dict(other)}" if other else ""))
    if not waves:
        L.append("No waves in range.")
        return L

    points = [(local(w.first_at).date(), minute_of_day(w.first_at)) for w in timed]
    wave_dates = {d for d, _ in points}
    L.append(f"Dates with >=1 timed wave: {len(wave_dates)} of {len(monitored)} monitored")

    if points:
        mins = [m for _, m in points]
        L.append(f"Earliest start: {hhmm(min(mins))}   Latest start: {hhmm(max(mins))}")
        if len(mins) >= 2 and max(mins) - min(mins) <= 12 * 60:
            L.append(f"Mean start: {hhmm(statistics.mean(mins))}   Median start: {_median_time(mins)}"
                     f"   Std dev: {statistics.pstdev(mins):.0f} min")
        elif len(mins) >= 2:
            L.append("Mean/median start not meaningful: start times span more than 12 hours.")
        else:
            L.append("Mean/median start not meaningful: only one timed wave.")

        hours = Counter(int(m // 60) for m in mins)
        top = max(hours.values())
        L.append("Time of day (local):")
        for h in sorted(hours):
            L.append(f"  {h:02d}:00  {hours[h]:3d}  {_bar(hours[h], top)}")
        dow = Counter(local(w.first_at).weekday() for w in timed)
        monitored_dow = Counter(d.weekday() for d in monitored)
        L.append("Day of week (waves / monitored dates):")
        for i, name in enumerate(WEEKDAYS):
            if monitored_dow[i] or dow[i]:
                L.append(f"  {name}  {dow[i]:3d} / {monitored_dow[i]}")

        L.append("Repeated timing windows (timed waves only):")
        found_any = False
        for width in WINDOW_WIDTHS:
            cand = best_window(points, width)
            if cand is None:
                L.append(f"  {width:>2}-min: no window with waves on 2+ dates")
                continue
            found_any = True
            cand.monitored_dates = len(monitored)
            cand.covered_dates = sum(
                1 for d in monitored if window_covered(buckets, d, cand.start_min, cand.end_min))
            stdev = f"{cand.stdev_min:.1f} min" if cand.stdev_min is not None else "n/a"
            L.append(
                f"  {width:>2}-min: {hhmm(cand.start_min)}-{hhmm(cand.end_min)}  "
                f"supporting dates {cand.support}, monitored {cand.monitored_dates}, "
                f"window covered on {cand.covered_dates} "
                f"({cand.covered_dates / cand.monitored_dates:.0%} completeness), "
                f"spread {cand.spread_min:.0f} min, sd {stdev}")
            L.append(f"          -> {cand.assessment}")
            if width == 15 and cand.support >= 2:
                days = ", ".join(f"{WEEKDAYS[d.weekday()]} {d:%m/%d} {hhmm(t)}"
                                 for d, t in zip(cand.dates, cand.times))
                L.append(f"          Highlight: recurring {hhmm(cand.start_min)}-{hhmm(cand.end_min)} window: {days}")
        if not found_any:
            L.append("  No time window recurs on more than one date yet.")

        L.append("Consecutive days (earliest timed wave):")
        for d, m, delta in consecutive_day_comparison(points):
            mark = ""
            if delta is not None:
                mark = f"  {delta:+.0f} min vs prior day" + (
                    "  <- within 15 min" if abs(delta) <= RECURRING_DELTA_MIN else "")
            L.append(f"  {WEEKDAYS[d.weekday()]} {d}  {hhmm(m)}{mark}")

    sizes = [len(w.members) for w in waves]
    L.append(f"Configurations per wave: mean {statistics.mean(sizes):.1f} of {len(SKUS)} "
             f"({statistics.mean(sizes) / len(SKUS):.0%}), max {max(sizes)}")
    L.extend(correlation_lines(waves, detailed))
    if detailed:
        L.append("Wave detail:")
        L.extend("  " + wave_line(w) for w in waves)
    return L


def correlation_lines(waves: Sequence[WaveRow], detailed: bool) -> list[str]:
    L = ["Configuration relationships:"]
    n = len(waves)
    all8 = sum(1 for w in waves if len(w.parts) == len(SKUS))
    multi = [w for w in waves if len(w.members) >= 2]
    simultaneous = sum(1 for w in multi if w.first_poll_parts() == w.parts)
    L.append(f"  All {len(SKUS)} SKUs in one wave: {all8}/{n}")
    L.append(f"  Single-SKU waves: {n - len(multi)}/{n}; multi-SKU waves: {len(multi)}")
    if multi:
        L.append(f"  Multi-SKU waves where every SKU appeared in the first poll: "
                 f"{simultaneous}/{len(multi)} ({simultaneous / len(multi):.0%})")

    caps = sorted({s.capacity_gb: s.capacity for s in SKUS}.items())
    L.append("  Storage size: in waves / first-poll in multi-SKU waves")
    for gb, cap in caps:
        in_w = sum(1 for w in waves if any(SKU_BY_PART[p].capacity_gb == gb for p in w.parts))
        first = sum(1 for w in multi if any(SKU_BY_PART[p].capacity_gb == gb for p in w.first_poll_parts()))
        L.append(f"    {cap:>5}: {in_w}/{n}  first {first}/{len(multi)}")

    def colors(w: WaveRow) -> set[str]:
        return {SKU_BY_PART[p].color for p in w.parts}
    both = sum(1 for w in waves if len(colors(w)) == 2)
    ns_only = sum(1 for w in waves if colors(w) == {"Night Sky"})
    sw_only = sum(1 for w in waves if colors(w) == {"Star White"})
    L.append(f"  Colors: both {both}, Night Sky only {ns_only}, Star White only {sw_only}")

    order, pairs = sku_ordering(waves)
    if order:
        L.append("  SKU order in multi-SKU waves (first-poll rate, mean offset):")
        for s in sorted(order, key=lambda s: -s.first_poll / s.appearances):
            L.append(f"    {s.code:<6} {s.first_poll}/{s.appearances} first, "
                     f"mean +{statistics.mean(s.offsets) / 60:.1f} min")
        consistent = [(a, b, k, t) for a, b, k, t in pairs if t >= 3 and k / t >= 0.8]
        if consistent:
            L.append("  Consistent precedence (>=3 shared waves, >=80% earlier):")
            for a, b, k, t in sorted(consistent, key=lambda x: (-x[2] / x[3], -x[3])):
                L.append(f"    {a} before {b}: {k}/{t}")
        else:
            L.append("  No SKU consistently precedes another (needs >=3 shared waves, >=80%).")

    pref = set(PREFERRED_PARTS)
    with_pref = [w for w in waves if w.parts & pref]
    without = [w for w in waves if not (w.parts & pref)]
    L.append(f"  Waves including 1TB/2TB Night Sky: {len(with_pref)}; without: {len(without)}")
    for label, group in (("with", with_pref), ("without", without)):
        timed = [minute_of_day(w.first_at) for w in group if w.event_class == NEW_AVAILABILITY]
        if timed:
            L.append(f"    {label:<7}: median start {_median_time(timed)} (n={len(timed)}), "
                     f"median size {statistics.median(len(w.members) for w in group):.0f}")
    leads = []
    for w in with_pref:
        pref_first = min(m.first_at for m in w.members if m.part in pref)
        other = [m.first_at for m in w.members if m.part not in pref]
        if other and min(other) < pref_first:
            leads.append((pref_first - min(other)).total_seconds() / 60)
    if with_pref:
        L.append(f"    Other SKUs appeared before 1TB/2TB NS in the same wave: {len(leads)}/{len(with_pref)}"
                 + (f", median lead {statistics.median(leads):.1f} min" if leads else ""))
    same_day_lead = []
    for w in with_pref:
        earlier = [x for x in without if x.store_id == w.store_id
                   and local(x.first_at).date() == local(w.first_at).date() and x.first_at < w.first_at]
        if earlier:
            same_day_lead.append((w.first_at - max(x.first_at for x in earlier)).total_seconds() / 60)
    if with_pref:
        L.append(f"    Same-day earlier wave without 1TB/2TB NS at this store: {len(same_day_lead)}/{len(with_pref)}"
                 + (f", median lead {statistics.median(same_day_lead):.0f} min" if same_day_lead else ""))
    return L


def wave_line(w: WaveRow) -> str:
    codes = ",".join(m.code for m in w.members)
    span = (w.last_at - w.first_at).total_seconds() / 60
    flags = []
    if w.present_at_startup:
        flags.append("startup")
    if w.gap_affected:
        flags.append("gap")
    if w.coverage_gap:
        flags.append("partial-coverage")
    return (f"#{w.wave_id:<5} {local(w.first_at):%Y-%m-%d %a %H:%M:%S %Z}  {w.store_name:<22} "
            f"{len(w.members)} SKU  +{span:.1f}m  [{codes}]  {w.event_class}/{w.confidence}"
            f"  polls={w.supporting_polls}" + (f"  ({', '.join(flags)})" if flags else ""))


def format_waves(db: HistoryDB, since: datetime | None = None, store: Store | None = None) -> str:
    waves = load_waves(db, since, [store.store_id] if store else None)
    if not waves:
        return "No restock waves recorded."
    lines = [f"{len(waves)} restock wave(s), oldest first. Times in America/New_York.", ""]
    lines += [wave_line(w) for w in waves]
    lines += ["", "Member detail:"]
    for w in waves:
        for m in w.members:
            lu = f"last unavailable {local(m.last_unavailable_at):%m-%d %H:%M:%S}" if m.last_unavailable_at else "last unavailable unknown"
            dur = f", available {m.duration_s / 60:.1f} min" if m.duration_s is not None else ""
            lines.append(f"  #{w.wave_id} {m.code:<6} {SKU_BY_PART[m.part].label:<18} "
                         f"first {local(m.first_at):%m-%d %H:%M:%S}  {lu}  timing={m.timing}{dur}")
    lines += ["", CAVEAT]
    return "\n".join(lines)


def format_report(db: HistoryDB, days: int | None = None, store: Store | None = None,
                  now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days) if days else None
    stores = [store] if store else list(STORES.values())
    waves = load_waves(db, since, [s.store_id for s in stores])
    coverage = load_coverage(db, since)
    by_store: dict[str, list[WaveRow]] = defaultdict(list)
    for w in waves:
        by_store[w.store_id].append(w)
    scope = f"last {days} day(s)" if days else "all history"
    lines = [f"Pickup availability timing report ({scope}), times in America/New_York",
             f"Generated {local(now):%Y-%m-%d %H:%M %Z}. {len(waves)} wave(s) across {len(stores)} store(s).",
             CAVEAT, ""]
    if not coverage:
        lines.append("No polling history in range.")
        return "\n".join(lines)
    for s in stores:
        if not by_store.get(s.store_id) and not coverage.get(s.store_id):
            continue
        lines += store_section(s, by_store.get(s.store_id, []), coverage.get(s.store_id, set()),
                               detailed=store is not None)
        lines.append("")
    if store is None:
        quiet = [s.name for s in stores if not by_store.get(s.store_id)]
        if quiet:
            lines.append("Stores with no waves in range: " + ", ".join(quiet))
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------
# CSV export
# --------------------------------------------------------------------------

EXPORTS = {
    "polls.csv": "SELECT * FROM polls ORDER BY poll_id",
    "poll_store_results.csv": "SELECT * FROM poll_store_results ORDER BY poll_id, store_id",
    "sku_observations_non_unavailable.csv": "SELECT * FROM sku_observations ORDER BY poll_id, store_id, part",
    "availability_events.csv": "SELECT * FROM availability_events ORDER BY observed_utc, event_id",
    "store_restock_waves.csv": "SELECT * FROM store_restock_waves ORDER BY first_detected_utc, wave_id",
    "restock_wave_members.csv": "SELECT * FROM restock_wave_members ORDER BY wave_id, first_available_utc",
    "vpn_sessions.csv": "SELECT * FROM vpn_sessions ORDER BY session_id",
    "vpn_events.csv": "SELECT * FROM vpn_events ORDER BY at_utc, event_id",
}
LOCAL_COLUMNS = {"started_utc", "finished_utc", "observed_utc", "first_detected_utc", "last_transition_utc",
                 "first_available_utc", "last_unavailable_utc", "ended_utc", "previous_confirmed_utc",
                 "last_seen_available_utc", "ready_utc", "at_utc"}


def export_csv(db: HistoryDB, out_dir: Path) -> list[tuple[Path, int]]:
    """Write one CSV per table. UTC columns get a *_local companion column."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, sql in EXPORTS.items():
        cur = db.conn.execute(sql)
        cols = [c[0] for c in cur.description]
        extra = [c for c in cols if c in LOCAL_COLUMNS]
        path = out_dir / name
        n = 0
        with path.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(cols + [c.replace("_utc", "_local") for c in extra])
            for row in cur:
                vals = list(row)
                locs = [local(from_utc_text(row[c])).isoformat() if row[c] else "" for c in extra]
                w.writerow(vals + locs)
                n += 1
        written.append((path, n))
    return written
