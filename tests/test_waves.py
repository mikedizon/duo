"""State transitions, duplicate suppression, and restock wave grouping."""

from __future__ import annotations

import unittest
from datetime import timedelta

from helpers import (
    A, COCONUT, E, I, K, NS1T, NS256, NS2T, SW1T, SW256, SW512, T0, U, WATERSIDE,
    quiet_logs, results, store_status, with_status,
)
from inventory_model import ALL_KEYS, STORES
from inventory_waves import (
    AFTER_GAP, BECAME_AVAILABLE, BECAME_UNAVAILABLE, COMPLETE, EXACT, NEW_AVAILABILITY,
    PARTIAL_COVERAGE, PRESENT_AT_STARTUP, STARTUP, UNCERTAIN, UNCERTAIN_TIMING,
    AlertTracker, InventoryTracker, PriorState, WaveDetector,
)

GAP = timedelta(seconds=60)
S = timedelta(seconds=20)
OK = {sid: True for sid in STORES}


def setUpModule():
    quiet_logs()


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.t = InventoryTracker(ALL_KEYS, GAP)

    def test_144_independent_keys(self):
        self.assertEqual(len(ALL_KEYS), 144)
        self.t.observe(results(), T0)
        tr = self.t.observe(results([(WATERSIDE, NS1T)]), T0 + S)
        self.assertEqual([(x.store_id, x.part) for x in tr], [(WATERSIDE, NS1T)])
        tr = self.t.observe(results([(WATERSIDE, NS1T), (COCONUT, SW256)]), T0 + 2 * S)
        self.assertEqual([(x.store_id, x.part) for x in tr], [(COCONUT, SW256)])

    def test_first_poll_available_is_startup(self):
        (tr,) = self.t.observe(results([(WATERSIDE, NS1T)]), T0)
        self.assertEqual((tr.kind, tr.timing, tr.at_startup), (BECAME_AVAILABLE, STARTUP, True))

    def test_exact_transition(self):
        self.t.observe(results(), T0)
        (tr,) = self.t.observe(results([(WATERSIDE, NS1T)]), T0 + S)
        self.assertEqual(tr.timing, EXACT)
        self.assertEqual(tr.last_unavailable_at, T0)
        self.assertFalse(tr.at_startup)

    def test_unknown_between_makes_after_gap(self):
        for st in (E, K):
            with self.subTest(st=st):
                t = InventoryTracker(ALL_KEYS, GAP)
                t.observe(results(), T0)
                t.observe(store_status(results(), WATERSIDE, st), T0 + S)
                (tr,) = t.observe(results([(WATERSIDE, NS1T)]), T0 + 2 * S)
                self.assertEqual(tr.timing, AFTER_GAP)
                self.assertEqual(tr.last_unavailable_at, T0)

    def test_missing_results_count_as_gap(self):
        self.t.observe(results(), T0)
        self.t.observe({}, T0 + S)
        (tr,) = self.t.observe(results([(WATERSIDE, NS1T)]), T0 + 2 * S)
        self.assertEqual(tr.timing, AFTER_GAP)

    def test_downtime_gap(self):
        self.t.observe(results(), T0)
        (tr,) = self.t.observe(results([(WATERSIDE, NS1T)]), T0 + timedelta(minutes=10))
        self.assertEqual(tr.timing, AFTER_GAP)

    def test_continuous_availability_one_transition(self):
        self.t.observe(results(), T0)
        n = 0
        seq = [results([(WATERSIDE, NS1T)]), results([(WATERSIDE, NS1T)]),
               store_status(results(), WATERSIDE, E), results([(WATERSIDE, NS1T)])]
        for i, r in enumerate(seq, 1):
            n += len(self.t.observe(r, T0 + i * S))
        self.assertEqual(n, 1)

    def test_became_unavailable_and_ineligible(self):
        self.t.observe(results([(WATERSIDE, NS1T)]), T0)
        (tr,) = self.t.observe(with_status(results(), {(WATERSIDE, NS1T): I}), T0 + S)
        self.assertEqual((tr.kind, tr.status), (BECAME_UNAVAILABLE, I))

    def test_seed_available_continues_after_restart(self):
        self.t.seed({(WATERSIDE, NS1T): PriorState(A, T0 - timedelta(hours=1))})
        self.assertEqual(self.t.observe(results([(WATERSIDE, NS1T)]), T0), [])

    def test_seed_unavailable_then_available_is_after_gap(self):
        prior_t = T0 - timedelta(hours=2)
        self.t.seed({(WATERSIDE, NS1T): PriorState(U, prior_t)})
        (tr,) = self.t.observe(results([(WATERSIDE, NS1T)]), T0)
        self.assertEqual((tr.timing, tr.at_startup, tr.last_unavailable_at), (AFTER_GAP, True, prior_t))


class AlertTrackerTests(unittest.TestCase):
    def test_duplicate_suppression_per_key(self):
        keys = [(sid, p) for sid in STORES for p in (NS1T, NS2T)]
        t = AlertTracker(keys)
        self.assertEqual(t.update(results([(WATERSIDE, NS1T)])), [(WATERSIDE, NS1T)])
        self.assertEqual(t.update(results([(WATERSIDE, NS1T)])), [])
        self.assertEqual(t.update(results([(WATERSIDE, NS1T), (COCONUT, NS1T)])), [(COCONUT, NS1T)])
        t.update(store_status(results([(COCONUT, NS1T)]), WATERSIDE, E))  # errors do not re-arm
        self.assertEqual(t.update(results([(WATERSIDE, NS1T), (COCONUT, NS1T)])), [])
        t.update(results([(COCONUT, NS1T)]))  # Waterside confirmed unavailable: re-armed
        self.assertEqual(t.update(results([(WATERSIDE, NS1T), (COCONUT, NS1T)])), [(WATERSIDE, NS1T)])

    def test_untracked_keys_ignored(self):
        t = AlertTracker([(WATERSIDE, NS1T)])
        self.assertEqual(t.update(results([(WATERSIDE, SW256)])), [])


class WaveHarness:
    def __init__(self, window_min: float = 10):
        self.tracker = InventoryTracker(ALL_KEYS, GAP)
        self.det = WaveDetector(timedelta(minutes=window_min))

    def poll(self, res, at, store_ok=None):
        tr = self.tracker.observe(res, at)
        return self.det.process(tr, res, store_ok or OK, at)


class WaveTests(unittest.TestCase):
    def setUp(self):
        self.h = WaveHarness()
        self.h.poll(results(), T0 - S)

    def test_single_sku_wave(self):
        upd = self.h.poll(results([(WATERSIDE, NS256)]), T0)
        self.assertEqual(len(upd.opened), 1)
        w = upd.opened[0]
        self.assertEqual((w.store_id, list(w.members)), (WATERSIDE, [NS256]))
        self.assertEqual((w.event_class, w.confidence), (NEW_AVAILABILITY, COMPLETE))

    def test_simultaneous_multi_sku(self):
        upd = self.h.poll(results([(WATERSIDE, NS256), (WATERSIDE, NS1T), (WATERSIDE, SW1T)]), T0)
        self.assertEqual(len(upd.opened), 1)
        self.assertEqual(set(upd.opened[0].members), {NS256, NS1T, SW1T})

    def test_staggered_within_window_is_one_wave(self):
        avail = []
        opened = 0
        for i, key in enumerate([(WATERSIDE, SW256), (WATERSIDE, SW512), (WATERSIDE, NS1T), (WATERSIDE, NS2T)]):
            avail.append(key)
            opened += len(self.h.poll(results(avail), T0 + i * S).opened)
        self.assertEqual(opened, 1)
        w = self.h.det.open[WATERSIDE]
        self.assertEqual(len(w.members), 4)
        self.assertEqual((w.first_at, w.last_at), (T0, T0 + 3 * S))
        self.assertEqual(w.members[NS2T].last_unavailable_at, T0 + 2 * S)
        self.assertEqual(w.supporting_polls, 4)

    def test_window_boundary_and_outside(self):
        first = self.h.poll(results([(WATERSIDE, NS256)]), T0).opened[0]
        t = T0
        while t < T0 + timedelta(minutes=10) - S:  # keep polling, still available
            t += S
            self.h.poll(results([(WATERSIDE, NS256)]), t)
        upd = self.h.poll(results([(WATERSIDE, NS256), (WATERSIDE, SW1T)]), T0 + timedelta(minutes=10))
        self.assertEqual(upd.opened, [])
        self.assertIn(SW1T, first.members)
        upd = self.h.poll(results([(WATERSIDE, NS256), (WATERSIDE, SW1T), (WATERSIDE, NS1T)]),
                          T0 + timedelta(minutes=10) + S)
        self.assertEqual([w.store_id for w in upd.closed], [WATERSIDE])
        self.assertEqual(len(upd.opened), 1)
        self.assertEqual(list(upd.opened[0].members), [NS1T])

    def test_remaining_available_never_opens_new_wave(self):
        self.h.poll(results([(WATERSIDE, NS256)]), T0)
        opened = 0
        for i in range(1, 100):  # 33 minutes, three windows
            opened += len(self.h.poll(results([(WATERSIDE, NS256)]), T0 + i * S).opened)
        self.assertEqual(opened, 0)

    def test_stores_are_independent(self):
        upd = self.h.poll(results([(WATERSIDE, NS256), (COCONUT, NS256)]), T0)
        self.assertEqual(sorted(w.store_id for w in upd.opened), sorted([COCONUT, WATERSIDE]))

    def test_transitions_only_not_counts(self):
        # Already-available SKUs at another store don't open waves when nothing changes.
        self.h.poll(results([(COCONUT, NS256), (COCONUT, SW256)]), T0)
        upd = self.h.poll(results([(COCONUT, NS256), (COCONUT, SW256)]), T0 + timedelta(minutes=20))
        self.assertEqual(upd.opened, [])

    def test_unavailable_then_available_again_new_wave(self):
        self.h.poll(results([(WATERSIDE, NS256)]), T0)
        self.h.poll(results(), T0 + timedelta(minutes=30))
        upd = self.h.poll(results([(WATERSIDE, NS256)]), T0 + timedelta(minutes=30) + S)
        self.assertEqual(len(upd.opened), 1)

    def test_flap_within_window_does_not_duplicate_member(self):
        self.h.poll(results([(WATERSIDE, NS256)]), T0)
        self.h.poll(results(), T0 + S)
        upd = self.h.poll(results([(WATERSIDE, NS256)]), T0 + 2 * S)
        self.assertEqual(upd.opened, [])
        self.assertEqual(len(self.h.det.open[WATERSIDE].members), 1)

    def test_coverage_gap_marks_partial(self):
        upd = self.h.poll(results([(WATERSIDE, NS256)]), T0)
        w = upd.opened[0]
        self.h.poll(store_status(results(), WATERSIDE, E), T0 + S, store_ok={**OK, WATERSIDE: False})
        self.assertTrue(w.coverage_gap)
        self.assertEqual(w.confidence, PARTIAL_COVERAGE)
        self.assertEqual(w.supporting_polls, 1)

    def test_after_gap_classification(self):
        self.h.poll(store_status(results(), WATERSIDE, E), T0, store_ok={**OK, WATERSIDE: False})
        w = self.h.poll(results([(WATERSIDE, NS256)]), T0 + S).opened[0]
        self.assertEqual((w.event_class, w.confidence, w.gap_affected), (UNCERTAIN_TIMING, UNCERTAIN, True))

    def test_startup_classification(self):
        h = WaveHarness()
        w = h.poll(results([(WATERSIDE, NS256)]), T0).opened[0]
        self.assertEqual((w.event_class, w.confidence, w.present_at_startup),
                         (PRESENT_AT_STARTUP, UNCERTAIN, True))

    def test_configurable_window(self):
        h = WaveHarness(window_min=1)
        h.poll(results(), T0 - S)
        h.poll(results([(WATERSIDE, NS256)]), T0)
        upd = h.poll(results([(WATERSIDE, NS256), (WATERSIDE, NS1T)]), T0 + timedelta(minutes=2))
        self.assertEqual(len(upd.opened), 1)


if __name__ == "__main__":
    unittest.main()
