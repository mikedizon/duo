"""Fetching, parsing, regional batching, notifications, display, loop, CLI modes."""

from __future__ import annotations

import io
import random
import subprocess
import tempfile
import threading
import unittest
import urllib.error
from datetime import timedelta
from email.message import Message
from pathlib import Path
from unittest import mock

from helpers import (
    A, COCONUT, E, FLORIDA_MALL, I, K, NS1T, NS256, NS2T, SW1T, SW256, T0, U, WATERSIDE, Clock,
    drop_store, fixture_fetch, load_fixture, m, outcome, quiet_logs, results, set_display, store_status,
)
from inventory_model import ALL_KEYS, ALL_PARTS, SKU_BY_PART, SKUS, STORES

TAMPA_STORES = [sid for sid, s in STORES.items() if s.poll_zip == "33607"]
MIAMI_STORES = [sid for sid, s in STORES.items() if s.poll_zip == "33131"]
URL_1T = SKU_BY_PART[NS1T].purchase_url
URL_2T = SKU_BY_PART[NS2T].purchase_url


def setUpModule():
    quiet_logs()


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def read(self, n: int = -1) -> bytes:
        return self._body if n < 0 else self._body[:n]

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


def http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(m.build_url("33607"), code, "err", headers, io.BytesIO(b""))


class ConfigTests(unittest.TestCase):
    def test_eight_verified_skus(self):
        self.assertEqual(len(SKUS), 8)
        self.assertEqual(len({s.part for s in SKUS}), 8)
        combos = {(s.color, s.capacity) for s in SKUS}
        self.assertEqual(combos, {(c, cap) for c in ("Night Sky", "Star White")
                                  for cap in ("256GB", "512GB", "1TB", "2TB")})
        self.assertEqual(SKU_BY_PART["MK1L4LL/A"].label, "1TB Night Sky")
        self.assertEqual(SKU_BY_PART["MK1R4LL/A"].label, "2TB Night Sky")

    def test_purchase_urls(self):
        self.assertEqual(URL_1T, "https://www.apple.com/shop/buy-iphone/iphone-duo/"
                                 "7.6-inch-display-1tb-night-sky-verizon")
        self.assertEqual(URL_2T, "https://www.apple.com/shop/buy-iphone/iphone-duo/"
                                 "7.6-inch-display-2tb-night-sky-verizon")

    def test_18_stores_144_keys_jacksonville_excluded(self):
        self.assertEqual(len(STORES), 18)
        self.assertEqual(len(ALL_KEYS), 144)
        self.assertNotIn("R111", STORES)

    def test_regional_fixtures_cover_assigned_stores(self):
        for zip_code, ids in (("33607", TAMPA_STORES), ("33131", MIAMI_STORES)):
            listed = {s["storeNumber"] for s in load_fixture(zip_code)["body"]["stores"]}
            self.assertTrue(set(ids) <= listed, zip_code)


class ParseTests(unittest.TestCase):
    def test_real_fixtures_all_eight_unavailable(self):
        for zip_code, ids in (("33607", TAMPA_STORES), ("33131", MIAMI_STORES)):
            r = m.parse_store_results(load_fixture(zip_code), ids)
            self.assertEqual(set(r), set(ids))
            for sid in ids:
                self.assertEqual(set(r[sid]), set(ALL_PARTS))
                self.assertEqual({o.status for o in r[sid].values()}, {U})

    def test_each_sku_parsed_independently(self):
        for part in ALL_PARTS:
            with self.subTest(part=part):
                r = m.parse_store_results(set_display(load_fixture("33607"), WATERSIDE, part, "available"),
                                          TAMPA_STORES)
                avail = [(sid, p) for sid in r for p, o in r[sid].items() if o.status is A]
                self.assertEqual(avail, [(WATERSIDE, part)])

    def test_ineligible(self):
        r = m.parse_store_results(set_display(load_fixture("33607"), COCONUT, SW1T, "ineligible"), TAMPA_STORES)
        self.assertIs(r[COCONUT][SW1T].status, I)

    def test_unmonitored_store_ignored(self):
        p = load_fixture("33607")
        for part in ALL_PARTS:
            set_display(p, "R111", part, "available")  # Jacksonville
        r = m.parse_store_results(p, TAMPA_STORES)
        self.assertNotIn("R111", r)
        self.assertFalse(any(o.status is A for s in r.values() for o in s.values()))

    def test_malformed_payloads_never_available(self):
        for payload in [None, [], "x", {}, {"body": None}, {"body": {"stores": {}}},
                        {"head": {"status": "500"}, "body": {"stores": []}}]:
            with self.subTest(payload=payload):
                r = m.parse_store_results(payload, TAMPA_STORES)
                self.assertEqual(set(r), set(TAMPA_STORES))
                self.assertEqual({o.status for s in r.values() for o in s.values()}, {K})

    def test_malformed_part_entries(self):
        def store(p):
            return next(s for s in p["body"]["stores"] if s["storeNumber"] == WATERSIDE)
        cases = {
            "part missing": lambda p: store(p)["partsAvailability"].pop(NS1T),
            "display int": lambda p: store(p)["partsAvailability"][NS1T].__setitem__("pickupDisplay", 1),
            "display case": lambda p: store(p)["partsAvailability"][NS1T].__setitem__("pickupDisplay", "Available"),
            "wrong partNumber": lambda p: store(p)["partsAvailability"][NS1T].update(
                partNumber="MK1R4LL/A", pickupDisplay="available"),
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                p = load_fixture("33607")
                mutate(p)
                r = m.parse_store_results(p, TAMPA_STORES)
                self.assertIs(r[WATERSIDE][NS1T].status, K)
                self.assertIs(r[WATERSIDE][NS256].status, U)  # other parts unaffected

    def test_partsAvailability_missing_all_unknown(self):
        p = load_fixture("33607")
        next(s for s in p["body"]["stores"] if s["storeNumber"] == WATERSIDE).pop("partsAvailability")
        r = m.parse_store_results(p, TAMPA_STORES)
        self.assertEqual({o.status for o in r[WATERSIDE].values()}, {K})

    def test_classify_store(self):
        obs = lambda *sts: [m.Observation("R1", "p", s) for s in sts]  # noqa: E731
        self.assertEqual(m.classify_store(obs(U, A, I)), "ok")
        self.assertEqual(m.classify_store(obs(U, K)), "partial")
        self.assertEqual(m.classify_store(obs(K, E)), "unknown")
        self.assertEqual(m.classify_store(obs(E, E)), "error")


class FetchTests(unittest.TestCase):
    def test_url_batches_eight_parts(self):
        url = m.build_url("33607")
        for i, part in enumerate(ALL_PARTS):
            self.assertIn(f"parts.{i}={part.replace('/', '%2F')}", url)
        self.assertIn("pl=true", url)
        self.assertIn("location=33607", url)

    def test_success_and_timeout_passed(self):
        opener = mock.Mock(return_value=FakeResponse((Path(__file__).parent / "fixtures" /
                                                      "pickup8_33607_unavailable.json").read_bytes()))
        data = m.fetch_inventory("33607", opener=opener)
        self.assertEqual(data["head"]["status"], "200")
        self.assertEqual(opener.call_args.kwargs["timeout"], m.HTTP_TIMEOUT)

    def test_http_errors_classified(self):
        for code, kind in [(429, "rate_limited"), (403, "forbidden"), (503, "server"), (404, "http")]:
            with self.subTest(code=code):
                with self.assertRaises(m.FetchError) as cm:
                    m.fetch_inventory("33607", opener=mock.Mock(side_effect=http_error(code, "30")))
                self.assertEqual((cm.exception.kind, cm.exception.retry_after), (kind, 30.0))

    def test_network_and_decode_errors(self):
        for exc in [urllib.error.URLError("dns"), TimeoutError(), ConnectionResetError(), OSError()]:
            with self.subTest(exc=exc), self.assertRaises(m.FetchError) as cm:
                m.fetch_inventory("33607", opener=mock.Mock(side_effect=exc))
            self.assertEqual(cm.exception.kind, "network")
        with self.assertRaises(m.FetchError) as cm:
            m.fetch_inventory("33607", opener=mock.Mock(return_value=FakeResponse(b"<html>")))
        self.assertEqual(cm.exception.kind, "decode")

    def test_retry_after_parsing(self):
        self.assertAlmostEqual(m.parse_retry_after("Wed, 21 Oct 2015 07:28:30 GMT", now=1445412480.0), 30.0)
        self.assertIsNone(m.parse_retry_after("garbage"))
        self.assertEqual(m.parse_retry_after("999999"), m.MAX_RETRY_AFTER)


class KeepOrder:
    """rng stand-in that leaves the regional order as configured."""

    def shuffle(self, seq):
        pass


class Reverse:
    def shuffle(self, seq):
        seq.reverse()


class CheckAllTests(unittest.TestCase):
    def test_two_regional_requests_cover_144(self):
        fetch = fixture_fetch()
        out = m.check_all(fetch, rng=KeepOrder())
        self.assertEqual(fetch.calls, ["33607", "33131"])
        self.assertEqual(out.requests, 2)
        self.assertEqual(len(out.results), 144)
        self.assertEqual(set(out.store_outcomes.values()), {"ok"})

    def test_missing_store_falls_back_to_own_zip(self):
        fetch = fixture_fetch({"33607": drop_store(load_fixture("33607"), WATERSIDE),
                               "34108": set_display(load_fixture("33607"), WATERSIDE, NS1T, "available")})
        out = m.check_all(fetch, rng=KeepOrder())
        self.assertEqual(fetch.calls, ["33607", "33131", "34108"])
        self.assertIs(out.results[(WATERSIDE, NS1T)].status, A)

    def test_missing_store_whose_zip_is_regional_is_unknown(self):
        fetch = fixture_fetch({"33607": drop_store(load_fixture("33607"), "R019")})  # Tampa's own ZIP
        out = m.check_all(fetch, rng=KeepOrder())
        self.assertEqual(fetch.calls, ["33607", "33131"])
        self.assertEqual({out.results[("R019", p)].status for p in ALL_PARTS}, {K})
        self.assertEqual(out.store_outcomes["R019"], "unknown")

    def test_rate_limit_halts_cycle(self):
        fetch = fixture_fetch({"33607": m.FetchError("rate_limited", "429", 429, 60.0)})
        out = m.check_all(fetch, rng=KeepOrder())
        self.assertEqual(fetch.calls, ["33607"])
        self.assertEqual(len(out.results), 144)
        self.assertEqual({o.status for o in out.results.values()}, {E})
        self.assertEqual(len(out.errors), 1)

    def test_network_failure_one_region_other_still_checked(self):
        fetch = fixture_fetch({"33607": m.FetchError("network", "down")})
        out = m.check_all(fetch, rng=KeepOrder())
        self.assertEqual(fetch.calls, ["33607", "33131"])
        self.assertIs(out.results[(WATERSIDE, NS1T)].status, E)
        self.assertIs(out.results[("R012", NS1T)].status, U)
        self.assertEqual(out.store_outcomes[WATERSIDE], "error")


    def test_order_is_randomized_and_results_identical(self):
        orders, snapshots = set(), []
        for seed in range(20):
            fetch = fixture_fetch({"33607": set_display(load_fixture("33607"), WATERSIDE, NS1T, "available")})
            out = m.check_all(fetch, rng=random.Random(seed))
            orders.add(tuple(fetch.calls))
            snapshots.append({k: o.status for k, o in out.results.items()})
        self.assertEqual(orders, {("33607", "33131"), ("33131", "33607")})
        self.assertTrue(all(snap == snapshots[0] for snap in snapshots))
        self.assertEqual(list(out.results)[:8], [(WATERSIDE, p) for p in ALL_PARTS])  # display order stable

    def test_rate_limit_halts_whichever_region_is_second(self):
        fetch = fixture_fetch({"33131": m.FetchError("rate_limited", "429", 429)})
        out = m.check_all(fetch, rng=Reverse())
        self.assertEqual(fetch.calls, ["33131"])
        self.assertEqual({out.results[(WATERSIDE, p)].status for p in ALL_PARTS}, {E})
        self.assertEqual(out.results[(WATERSIDE, NS1T)].detail, "skipped after rate limit")


class ZipPickerTests(unittest.TestCase):
    def test_random_nearby_zips_cover_all_stores(self):
        from inventory_model import REGION_ZIPS
        picker = m.ZipPicker(rng=random.Random(3))
        used = {r: set() for r in REGION_ZIPS}
        last = {}
        for _ in range(120):
            fetch = fixture_fetch()
            out = m.check_all(fetch=fetch, rng=random.Random(0), picker=picker)
            self.assertEqual(len(out.results), 144)
            self.assertEqual(out.requests, 2)
            for zip_code in fetch.calls:
                region = next(r for r, zs in REGION_ZIPS.items() if zip_code in zs)
                self.assertNotEqual(last.get(region), zip_code)  # never the same ZIP twice in a row
                last[region] = zip_code
                used[region].add(zip_code)
        for region, zs in REGION_ZIPS.items():
            self.assertEqual(used[region], set(zs))  # every candidate gets used

    def test_zip_that_misses_a_store_is_dropped_and_fallback_fills_it(self):
        picker = m.ZipPicker({"33607": ("33607", "33629"), "33131": ("33131",)}, rng=random.Random(0))
        short = drop_store(load_fixture("33607"), WATERSIDE)  # 33629's answer lacks Naples
        fetch = fixture_fetch({"33629": short, "34108": load_fixture("33607")})
        picker.last["33607"] = "33607"  # so the next Tampa pick is 33629
        out = m.check_all(fetch=fetch, rng=random.Random(0), picker=picker)
        self.assertIn("33629", fetch.calls)
        self.assertIn("34108", fetch.calls)  # Waterside's own ZIP fills the gap
        self.assertEqual(out.results[(WATERSIDE, NS1T)].status, U)
        self.assertEqual(picker.demoted, {"33607": {"33629"}})
        for _ in range(10):
            self.assertEqual(picker.pick("33607"), "33607")  # dropped ZIP never comes back this run

    def test_center_zip_never_dropped(self):
        picker = m.ZipPicker({"33607": ("33607",)})
        picker.demote("33607", "33607")
        self.assertEqual(picker.pick("33607"), "33607")
        self.assertEqual(picker.demoted, {})

    def test_probe_is_one_request(self):
        fetch = fixture_fetch()
        out = m.check_all(fetch=fetch, zips=["33131"], picker=m.ZipPicker(rng=random.Random(1)))
        self.assertEqual(out.requests, 1)
        self.assertIn(fetch.calls[0], __import__("inventory_model").REGION_ZIPS["33131"])

    def test_without_picker_center_zips_are_used(self):
        fetch = fixture_fetch()
        m.check_all(fetch=fetch, rng=random.Random(0))
        self.assertEqual(sorted(fetch.calls), ["33131", "33607"])


class PurchaseChoiceTests(unittest.TestCase):
    def test_choices(self):
        never = lambda k: False  # noqa: E731
        self.assertEqual(m.choose_purchase([(WATERSIDE, NS1T)], never).part, NS1T)
        self.assertEqual(m.choose_purchase([(WATERSIDE, NS2T), (COCONUT, NS1T)], never).part, NS1T)
        self.assertEqual(m.choose_purchase([(WATERSIDE, NS2T)], never).part, NS2T)
        self.assertIsNone(m.choose_purchase([(WATERSIDE, NS2T)], lambda k: k == (COCONUT, NS1T)))
        self.assertIsNone(m.choose_purchase([], never))


class NotifyTests(unittest.TestCase):
    def call(self, waves, keys, avail=lambda k: False):
        chime, browser, out = mock.Mock(), mock.Mock(return_value=True), io.StringIO()
        m.notify_cycle(waves, keys, avail, T0, chime=chime, browser=browser, out=out)
        return chime, browser, out.getvalue()

    def wave(self, store_id, *parts):
        from inventory_waves import Wave, WaveMember
        w = Wave(store_id, T0, T0)
        for p in parts:
            w.members[p] = WaveMember(p, T0, None, "exact", False)
        return w

    def test_unrelated_wave_chimes_without_browser(self):
        chime, browser, text = self.call([self.wave(WATERSIDE, SW256)], [])
        chime.assert_called_once()
        browser.assert_not_called()
        self.assertIn("RESTOCK WAVE", text)

    def test_preferred_opens_1tb(self):
        chime, browser, text = self.call([self.wave(WATERSIDE, NS1T)], [(WATERSIDE, NS1T)])
        chime.assert_called_once()
        browser.assert_called_once_with(URL_1T)
        self.assertIn("Trade-in: iPhone 16 Pro Max", text)

    def test_2tb_only(self):
        _, browser, _ = self.call([], [(COCONUT, NS2T)])
        browser.assert_called_once_with(URL_2T)

    def test_2tb_suppressed_when_1tb_available(self):
        chime, browser, text = self.call([], [(COCONUT, NS2T)], avail=lambda k: k == (WATERSIDE, NS1T))
        chime.assert_called_once()
        browser.assert_not_called()
        self.assertIn("not opening the 2TB page", text)

    def test_many_waves_one_chime_one_tab(self):
        waves = [self.wave(WATERSIDE, NS1T, SW256), self.wave(COCONUT, NS2T), self.wave(FLORIDA_MALL, NS256)]
        chime, browser, text = self.call(waves, [(WATERSIDE, NS1T), (COCONUT, NS2T)])
        chime.assert_called_once()
        browser.assert_called_once_with(URL_1T)
        self.assertEqual(text.count("RESTOCK WAVE"), 3)

    def test_nothing_new_does_nothing(self):
        chime, browser, text = self.call([], [])
        chime.assert_not_called()
        browser.assert_not_called()
        self.assertEqual(text, "")

    def test_play_chime_safe(self):
        runner = mock.Mock()
        m.play_chime(runner=runner)
        self.assertEqual(runner.call_args.args[0], ["afplay", m.SOUND_FILE])
        self.assertNotIn("shell", runner.call_args.kwargs)
        with mock.patch("sys.stdout", new=io.StringIO()):
            self.assertIsNone(m.play_chime(runner=mock.Mock(side_effect=FileNotFoundError())))
            self.assertIsNone(m.play_chime("/nonexistent.aiff", runner=runner))

    def test_browser_safe(self):
        self.assertFalse(m.open_purchase_page(URL_1T, opener=mock.Mock(side_effect=OSError())))
        self.assertFalse(m.open_purchase_page(URL_1T, opener=mock.Mock(return_value=False)))


class DisplayTests(unittest.TestCase):
    def test_matrix_lists_everything(self):
        res = store_status(results([(WATERSIDE, NS1T), (COCONUT, SW256)]), FLORIDA_MALL, E)
        text = m.render_matrix(152, T0, outcome(res, [m.FetchError("network", "x")]),
                               {(WATERSIDE, NS1T)}, 1, 0.84)
        self.assertIn("[2026-10-27 09:42:00 EDT] Poll #152", text)
        for s in STORES.values():
            self.assertIn(s.name, text)
        for s in SKUS:
            self.assertIn(s.code, text)
        self.assertIn("Available 2/144", text)
        self.assertIn("Newly available 1", text)
        self.assertIn("New waves 1", text)
        self.assertIn("Unknown/error cells 8", text)
        self.assertIn("Duration 0.84s", text)
        self.assertIn("Errors 1 (network)", text)
        fm = next(line for line in text.splitlines() if line.startswith("Florida Mall"))
        self.assertEqual(fm.count("?"), 8)
        self.assertNotIn("\033[", text)

    def test_green_for_newly_available(self):
        text = m.render_matrix(1, T0, outcome(results([(WATERSIDE, NS1T), (COCONUT, NS1T)])),
                               {(WATERSIDE, NS1T)}, 1, 0.1, color=True)
        ws = next(line for line in text.splitlines() if line.startswith("Waterside"))
        cp = next(line for line in text.splitlines() if line.startswith("Coconut"))
        self.assertIn(m.GREEN, ws)
        self.assertNotIn(m.GREEN, cp)


class MonitorTests(unittest.TestCase):
    def make(self):
        self.clock = Clock()
        self.notifier = mock.Mock()
        self.out = io.StringIO()
        return m.Monitor(20.0, timedelta(minutes=10), wall=self.clock, out=self.out, color=False,
                         notifier=self.notifier)

    def feed(self, mon, res):
        mon.process(outcome(res), self.clock(), 0.3)
        self.clock.advance(seconds=20)

    def test_unrelated_sku_wave_notifies_without_purchase(self):
        mon = self.make()
        self.feed(mon, results())
        self.feed(mon, results([(WATERSIDE, SW256)]))
        waves, keys = self.notifier.call_args.args[:2]
        self.assertEqual(len(waves), 1)
        self.assertEqual(keys, [])
        self.assertIn("256GB Star White", self.out.getvalue())

    def test_preferred_and_dedup(self):
        mon = self.make()
        self.feed(mon, results())
        self.feed(mon, results([(WATERSIDE, NS1T)]))
        self.assertEqual(self.notifier.call_args.args[1], [(WATERSIDE, NS1T)])
        self.notifier.reset_mock()
        for _ in range(5):
            self.feed(mon, results([(WATERSIDE, NS1T)]))
        self.notifier.assert_not_called()

    def test_initial_availability_alerts(self):
        mon = self.make()
        self.feed(mon, results([(WATERSIDE, NS1T), (COCONUT, SW1T)]))
        waves, keys = self.notifier.call_args.args[:2]
        self.assertEqual(sorted(w.store_id for w in waves), sorted([WATERSIDE, COCONUT]))
        self.assertEqual(keys, [(WATERSIDE, NS1T)])

    def test_staggered_wave_notifies_once(self):
        mon = self.make()
        self.feed(mon, results())
        avail = []
        for key in [(WATERSIDE, SW256), (WATERSIDE, NS256), (WATERSIDE, SW1T)]:
            avail.append(key)
            self.feed(mon, results(avail))
        self.assertEqual(self.notifier.call_count, 1)
        self.assertEqual(self.out.getvalue().count("AVAILABLE"), 3)

    def test_unexpected_check_exception(self):
        mon = self.make()
        mon.check = mock.Mock(side_effect=RuntimeError("boom"))
        out = mon.poll()
        self.assertEqual({o.status for o in out.results.values()}, {E})


class BackoffTests(unittest.TestCase):
    def make(self):
        return m.Backoff(20.0, rand=lambda a, b: 1.0)

    def test_sequence(self):
        b = self.make()
        self.assertEqual(b.next_delay([]), 20.0)
        hard = [m.FetchError("server", "503", 503)]
        self.assertEqual([b.next_delay(hard) for _ in range(3)], [40.0, 80.0, 160.0])
        self.assertEqual(b.next_delay([]), 20.0)
        soft = [m.FetchError("network", "x")]
        self.assertEqual([b.next_delay(soft) for _ in range(4)], [20.0, 20.0, 40.0, 80.0])

    def test_cap_and_retry_after(self):
        b = self.make()
        for _ in range(20):
            d = b.next_delay([m.FetchError("rate_limited", "429", 429)])
        self.assertEqual(d, m.BACKOFF_CAP)
        self.assertEqual(self.make().next_delay([m.FetchError("rate_limited", "429", 429, 300.0)]), 300.0)


class FakeMonoClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeStop:
    def __init__(self, clock):
        self.clock = clock

    def is_set(self):
        return False

    def wait(self, seconds):
        assert seconds >= 0
        self.clock.t += seconds


BLOCKED = m.FetchError("blocked", "HTTP 541", 541)


class LoopHarness:
    """Drives run_loop on a fake monotonic clock."""

    def __init__(self, outcomes, request_time=0.0):
        self.clock = FakeMonoClock()
        self.calls: list[tuple[float, tuple | None]] = []
        self.outcomes = outcomes
        self.request_time = request_time

    def check(self, zips=None):
        self.calls.append((self.clock.t, tuple(zips) if zips is not None else None))
        self.clock.t += self.request_time
        item = self.outcomes(len(self.calls)) if callable(self.outcomes) else self.outcomes[len(self.calls) - 1]
        return item

    def run(self, cycles):
        return self.run_at(20.0, cycles)

    def run_at(self, interval, cycles):
        self.mon = m.Monitor(interval, timedelta(minutes=10), check=self.check, out=io.StringIO(), color=False,
                             notifier=mock.Mock(), wall=Clock(), mono=self.clock)
        m.run_loop(self.mon, stop=FakeStop(self.clock), backoff=m.Backoff(interval, rand=lambda a, b: 1.0),
                   clock=self.clock, max_cycles=cycles, rng=random.Random(0))
        return self

    @property
    def gaps(self):
        t = [c[0] for c in self.calls]
        return [round(b - a, 6) for a, b in zip(t, t[1:])]


class LoopTests(unittest.TestCase):
    def test_interval_alone_sets_the_pace(self):
        for interval in (20.0, 30.0, 60.0):
            with self.subTest(interval=interval):
                gaps = LoopHarness([outcome(results())] * 200).run_at(interval, 200).gaps
                self.assertEqual(set(gaps), {interval})  # no request budget: never slowed down

    def test_fixed_cadence(self):
        self.assertEqual(LoopHarness([outcome(results())] * 5, 1.5).run(5).gaps, [20.0] * 4)

    def test_overrun_no_burst(self):
        self.assertEqual(LoopHarness([outcome(results())] * 3, 25.0).run(3).gaps, [25.0, 25.0])

    def test_backoff_applied(self):
        err = outcome(results(), [m.FetchError("rate_limited", "429", 429)])
        h = LoopHarness([err, err, outcome(results()), outcome(results())]).run(4)
        self.assertEqual(h.gaps, [40.0, 80.0, 20.0])

    def test_stop_event(self):
        stop = threading.Event()
        stop.set()
        check = mock.Mock()
        m.run_loop(m.Monitor(20.0, timedelta(minutes=10), check=check, out=io.StringIO()), stop=stop)
        check.assert_not_called()


class CooldownTests(unittest.TestCase):
    def test_541_classified_blocked_and_halts_cycle(self):
        with self.assertRaises(m.FetchError) as cm:
            m.fetch_inventory("33607", opener=mock.Mock(side_effect=http_error(541)))
        self.assertEqual(cm.exception.kind, "blocked")
        fetch = fixture_fetch({"33607": BLOCKED})
        out = m.check_all(fetch, rng=KeepOrder())
        self.assertEqual(fetch.calls, ["33607"])
        self.assertEqual({o.status for o in out.results.values()}, {E})

    def test_escalating_cooldown_single_probe_then_resume(self):
        seq = [outcome(results(), [BLOCKED]),          # full poll refused
               outcome(results(), [BLOCKED]),          # probe 1 refused
               outcome(results(), [BLOCKED]),          # probe 2 refused
               outcome(results()),                     # probe 3 ok
               outcome(results()), outcome(results())]  # full polls
        h = LoopHarness(seq).run(len(seq))
        self.assertEqual(h.gaps, [300.0, 600.0, 1200.0, 0.0, 20.0])
        kinds = [None if z is None else len(z) for _, z in h.calls]
        self.assertEqual(kinds, [None, 1, 1, 1, None, None])
        self.assertTrue(all(z[0] in m.regional_zips() for _, z in h.calls if z))
        text = h.mon.stream.getvalue()
        self.assertIn("Sending nothing for 5 min", text)
        self.assertIn("Sending nothing for 20 min", text)
        self.assertIn("Probe succeeded; resuming full polls.", text)
        self.assertIn("[cooldown probe: one region only]", text)

    def test_cooldown_caps_at_30_min(self):
        seq = [outcome(results(), [BLOCKED])] * 7
        h = LoopHarness(seq).run(len(seq))
        self.assertEqual(h.gaps, [300.0, 600.0, 1200.0, 1800.0, 1800.0, 1800.0])

    def test_forbidden_also_cools_down(self):
        h = LoopHarness([outcome(results(), [m.FetchError("forbidden", "403", 403)]), outcome(results())]).run(2)
        self.assertEqual(h.gaps, [300.0])
        self.assertEqual(len(h.calls[1][1]), 1)

    def test_probe_with_network_error_stays_in_cooldown(self):
        seq = [outcome(results(), [BLOCKED]), outcome(results(), [m.FetchError("network", "x")]),
               outcome(results())]
        h = LoopHarness(seq).run(3)
        self.assertEqual([None if z is None else len(z) for _, z in h.calls], [None, 1, 1])
        self.assertEqual(h.gaps, [300.0, 20.0])


class SeedTests(unittest.TestCase):
    def test_cooldown_restored_from_history(self):
        from inventory_db import HistoryDB, PollRecord
        db = HistoryDB(":memory:")
        clock = Clock(T0)
        for i in range(30):  # 60 requests over the last 10 minutes, last one blocked
            at = T0 - timedelta(seconds=600) + timedelta(seconds=20 * i)
            errs = [BLOCKED] if i == 29 else []
            db.record_poll(None, PollRecord(at, at, 2, errs, results(), {sid: "ok" for sid in STORES}))
        mono = FakeMonoClock()
        mon = m.Monitor(20.0, timedelta(minutes=10), db=db, wall=clock, out=io.StringIO(), mono=mono)
        mon.seed_from_history()
        self.assertTrue(mon.cooldown.active)
        self.assertAlmostEqual(mon.cooldown.until - mono(), 280.0)
        db.close()

    def test_quiet_history_no_cooldown(self):
        from inventory_db import HistoryDB
        db = HistoryDB(":memory:")
        mon = m.Monitor(20.0, timedelta(minutes=10), db=db, wall=Clock(), out=io.StringIO())
        mon.seed_from_history()
        self.assertFalse(mon.cooldown.active)
        db.close()


class ModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "h.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def test_dry_run_no_http_no_history_real_alert_paths(self):
        boom = AssertionError("dry run must not do this")
        proc = mock.Mock(spec=subprocess.Popen)
        with mock.patch.object(m, "fetch_inventory", side_effect=boom) as fetch, \
             mock.patch.object(m, "check_all", side_effect=boom) as check, \
             mock.patch("urllib.request.urlopen", side_effect=boom) as urlopen, \
             mock.patch("inventory_db.HistoryDB", side_effect=boom) as hdb, \
             mock.patch("subprocess.Popen", return_value=proc) as popen, \
             mock.patch("webbrowser.open", return_value=True) as wb, \
             mock.patch("sys.stdout", new=io.StringIO()) as out:
            code = m.main(["--dry-run", "--db", str(self.db)])
        self.assertEqual(code, 0)
        for mk in (fetch, check, urlopen, hdb):
            mk.assert_not_called()
        self.assertFalse(self.db.exists())
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0], ["afplay", m.SOUND_FILE])
        wb.assert_called_once_with(URL_1T)
        text = out.getvalue()
        self.assertIn("SIMULATED", text)
        self.assertIn("Waterside Shops", text)
        self.assertIn("no history was written", text)

    def test_once_no_notify_no_history(self):
        fetch = fixture_fetch({"33607": set_display(load_fixture("33607"), WATERSIDE, NS1T, "available")})
        with mock.patch.object(m, "fetch_inventory", fetch), \
             mock.patch.object(m, "notify_cycle") as notify, \
             mock.patch("subprocess.Popen") as popen, \
             mock.patch("webbrowser.open") as wb, \
             mock.patch("sys.stdout", new=io.StringIO()) as out:
            code = m.main(["--once", "--db", str(self.db)])
        self.assertEqual(code, 0)
        for mk in (notify, popen, wb):
            mk.assert_not_called()
        self.assertFalse(self.db.exists())
        self.assertIn("Available 1/144", out.getvalue())

    def test_once_exit_code_on_error(self):
        fetch = fixture_fetch({"33607": m.FetchError("network", "down")})
        with mock.patch.object(m, "fetch_inventory", fetch), mock.patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(m.main(["--once"]), 2)

    def test_cli_validation(self):
        p = m.build_parser()
        self.assertEqual(p.parse_args([]).interval, 20.0)
        self.assertEqual(p.parse_args([]).wave_window, 10.0)
        self.assertEqual(p.parse_args(["--interval", "30"]).interval, 30.0)
        bad = [["--interval", "1"], ["--interval", "nan"], ["--wave-window", "0"], ["--days", "0"],
               ["--dry-run", "--once"], ["--report", "--waves"]]
        for argv in bad:
            with self.subTest(argv=argv), mock.patch("sys.stderr", new=io.StringIO()), \
                    self.assertRaises(SystemExit):
                p.parse_args(argv)
        for argv in (["--days", "7"], ["--store", "Waterside Shops"]):
            with self.subTest(argv=argv), mock.patch("sys.stderr", new=io.StringIO()), \
                    self.assertRaises(SystemExit):
                m.main(argv)

    def test_report_without_history(self):
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertEqual(m.main(["--report", "--db", str(self.db)]), 1)
        self.assertIn("No history database", err.getvalue())
        self.assertFalse(self.db.exists())

    def test_unknown_store(self):
        with mock.patch("sys.stderr", new=io.StringIO()) as err, self.assertRaises(SystemExit):
            m.main(["--report", "--store", "Nowhere", "--db", str(self.db)])
        self.assertIn("unknown store", err.getvalue())


if __name__ == "__main__":
    unittest.main()
