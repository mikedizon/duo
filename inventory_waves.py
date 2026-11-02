"""Per-SKU state transitions, purchase-alert suppression, and restock waves.

Terminology
-----------
transition  A change in confirmed status for one (store, SKU) key between
            two confirmed observations. UNKNOWN/ERROR observations never
            create or reset a transition; they only mark the key "dirty",
            meaning the moment of change is uncertain.
wave        A store-level cluster of became_available transitions whose
            first member opened the wave and whose later members were
            observed no more than `window` after it. A wave is an analytical
            grouping of API observations, not evidence of a delivery.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Mapping

from inventory_model import Key, Observation, Status

log = logging.getLogger("iphone_monitor")

# Transition timing classes
EXACT = "exact"  # previous confirmed observation was the immediately preceding usable poll
AFTER_GAP = "after_gap"  # failed polls or downtime between the two confirmed observations
STARTUP = "startup"  # first observation ever for this key; no earlier state known

BECAME_AVAILABLE = "became_available"
BECAME_UNAVAILABLE = "became_unavailable"


@dataclass(frozen=True)
class Transition:
    store_id: str
    part: str
    kind: str
    at: datetime
    status: Status
    previous_status: Status | None
    previous_confirmed_at: datetime | None
    last_unavailable_at: datetime | None
    timing: str
    at_startup: bool  # first confirmed observation of this key in this run


@dataclass
class KeyState:
    status: Status | None = None
    confirmed_at: datetime | None = None
    last_unavailable_at: datetime | None = None
    dirty: bool = False  # an UNKNOWN/ERROR result arrived since the last confirmed one
    seen_this_run: bool = False


@dataclass(frozen=True)
class PriorState:
    """Last confirmed state loaded from history at startup."""

    status: Status
    confirmed_at: datetime
    last_unavailable_at: datetime | None = None


class InventoryTracker:
    """Tracks confirmed status per (store, part) and emits transitions."""

    def __init__(self, keys: Iterable[Key], gap_threshold: timedelta) -> None:
        self.state: dict[Key, KeyState] = {k: KeyState() for k in keys}
        self.gap_threshold = gap_threshold

    def seed(self, prior: Mapping[Key, PriorState]) -> None:
        for key, p in prior.items():
            st = self.state.get(key)
            if st is None or not p.status.is_confirmed:
                continue
            st.status = p.status
            st.confirmed_at = p.confirmed_at
            st.last_unavailable_at = p.last_unavailable_at
            if p.status is not Status.AVAILABLE:
                st.last_unavailable_at = max(filter(None, (p.last_unavailable_at, p.confirmed_at)))

    def is_available(self, key: Key) -> bool:
        st = self.state.get(key)
        return st is not None and st.status is Status.AVAILABLE

    def observe(self, results: Mapping[Key, Observation], at: datetime) -> list[Transition]:
        out: list[Transition] = []
        for key, st in self.state.items():
            obs = results.get(key)
            if obs is None or not obs.status.is_confirmed:
                st.dirty = True
                continue
            prev = st.status
            first_in_run = not st.seen_this_run
            if prev is None:
                timing = STARTUP
            elif st.dirty or st.confirmed_at is None or at - st.confirmed_at > self.gap_threshold:
                timing = AFTER_GAP
            else:
                timing = EXACT

            now_avail = obs.status is Status.AVAILABLE
            was_avail = prev is Status.AVAILABLE
            if now_avail and not was_avail:
                out.append(Transition(key[0], key[1], BECAME_AVAILABLE, at, obs.status, prev,
                                      st.confirmed_at, st.last_unavailable_at, timing, first_in_run))
            elif was_avail and not now_avail:
                out.append(Transition(key[0], key[1], BECAME_UNAVAILABLE, at, obs.status, prev,
                                      st.confirmed_at, st.last_unavailable_at, timing, first_in_run))
            if not now_avail:
                st.last_unavailable_at = at
            st.status = obs.status
            st.confirmed_at = at
            st.dirty = False
            st.seen_this_run = True
        return out


class AlertTracker:
    """Purchase-alert suppression per (store, part).

    Alerts when a key is seen AVAILABLE and has not alerted yet in this run.
    Re-arms only on a confirmed UNAVAILABLE/INELIGIBLE. UNKNOWN/ERROR leave
    state untouched.
    """

    def __init__(self, keys: Iterable[Key]) -> None:
        self.alerted: dict[Key, bool] = {k: False for k in keys}

    def update(self, results: Mapping[Key, Observation]) -> list[Key]:
        to_alert: list[Key] = []
        for key in self.alerted:
            obs = results.get(key)
            if obs is None or not obs.status.is_confirmed:
                continue
            if obs.status is Status.AVAILABLE:
                if not self.alerted[key]:
                    self.alerted[key] = True
                    to_alert.append(key)
            else:
                self.alerted[key] = False
        return to_alert


# --------------------------------------------------------------------------
# Waves
# --------------------------------------------------------------------------

NEW_AVAILABILITY = "new_availability"
PRESENT_AT_STARTUP = "present_at_startup"
UNCERTAIN_TIMING = "uncertain_timing"

COMPLETE = "complete"
PARTIAL_COVERAGE = "partial_coverage"
UNCERTAIN = "uncertain"


@dataclass
class WaveMember:
    part: str
    first_available_at: datetime
    last_unavailable_at: datetime | None
    timing: str
    at_startup: bool


@dataclass
class Wave:
    store_id: str
    first_at: datetime
    last_at: datetime
    members: dict[str, WaveMember] = field(default_factory=dict)
    supporting_polls: int = 0
    coverage_gap: bool = False  # store had UNKNOWN/ERROR results while the wave was open
    wave_id: int | None = None

    @property
    def present_at_startup(self) -> bool:
        """Some member was already available on this run's first poll of it."""
        return any(m.at_startup for m in self.members.values())

    @property
    def gap_affected(self) -> bool:
        return any(m.timing != EXACT for m in self.members.values())

    @property
    def event_class(self) -> str:
        if any(m.timing == STARTUP for m in self.members.values()):
            return PRESENT_AT_STARTUP
        if any(m.timing == AFTER_GAP for m in self.members.values()):
            return UNCERTAIN_TIMING
        return NEW_AVAILABILITY

    @property
    def confidence(self) -> str:
        if self.event_class != NEW_AVAILABILITY:
            return UNCERTAIN
        return PARTIAL_COVERAGE if self.coverage_gap else COMPLETE


@dataclass
class WaveUpdate:
    opened: list[Wave] = field(default_factory=list)
    joined: list[tuple[Wave, WaveMember]] = field(default_factory=list)  # includes opening members
    touched: list[Wave] = field(default_factory=list)  # every open wave this poll
    closed: list[Wave] = field(default_factory=list)


class WaveDetector:
    """Groups became_available transitions per store into waves.

    A wave opens on a store's first became_available transition while no
    wave is open there. Later transitions at that store join it while
    `at - wave.first_at <= window`. After that the wave closes; the next
    transition opens a new one. A key that stays available produces no
    transition, so it can never open a second wave.
    """

    def __init__(self, window: timedelta) -> None:
        self.window = window
        self.open: dict[str, Wave] = {}

    def process(
        self,
        transitions: Iterable[Transition],
        results: Mapping[Key, Observation],
        store_ok: Mapping[str, bool],
        at: datetime,
    ) -> WaveUpdate:
        upd = WaveUpdate()
        for sid in list(self.open):
            if at - self.open[sid].first_at > self.window:
                upd.closed.append(self.open.pop(sid))

        for t in transitions:
            if t.kind != BECAME_AVAILABLE:
                continue
            wave = self.open.get(t.store_id)
            if wave is None:
                wave = Wave(t.store_id, t.at, t.at)
                self.open[t.store_id] = wave
                upd.opened.append(wave)
            if t.part in wave.members:
                continue  # flapped within the window; first appearance stands
            member = WaveMember(t.part, t.at, t.last_unavailable_at, t.timing, t.at_startup)
            wave.members[t.part] = member
            wave.last_at = max(wave.last_at, t.at)
            upd.joined.append((wave, member))

        for sid, wave in self.open.items():
            if any(
                (r := results.get((sid, p))) is not None and r.status is Status.AVAILABLE
                for p in wave.members
            ):
                wave.supporting_polls += 1
            if not store_ok.get(sid, False):
                wave.coverage_gap = True
            upd.touched.append(wave)
        return upd

    def close_all(self) -> list[Wave]:
        waves = list(self.open.values())
        self.open.clear()
        return waves
