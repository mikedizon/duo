"""DUO_ZIPS: discovering the stores to monitor from Apple's answers, caching them, and using them."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from helpers import NS1T, T0, Clock, fixture_fetch, load_fixture, m, outcome, quiet_logs, results
import inventory_model as im
from inventory_db import HistoryDB
from stores_config import StoreConfig, StoreConfigError, discover, from_env

FLORIDA_IDS = {s.store_id for s in im.FLORIDA_STORES}


def setUpModule():
    quiet_logs()


class Base(unittest.TestCase):
    def tearDown(self):
        im.configure_florida_default()
        m.reset_zip_picker()


class FromEnvTests(Base):
    def test_unset_means_builtin(self):
        self.assertIsNone(from_env({}))
        self.assertIsNone(from_env({"DUO_ZIPS": "  "}))

    def test_parse(self):
        cfg = from_env({"DUO_ZIPS": "10001, 11201 10001", "DUO_MAX_MILES": "12.5", "DUO_NEARBY_ZIPS_10001": "10002,10003"})
        self.assertEqual(cfg.zips, ("10001", "11201"))
        self.assertEqual(cfg.max_miles, 12.5)
        self.assertEqual(cfg.nearby, {"10001": ("10002", "10003")})
        self.assertEqual(cfg.describe(), "within 12.5 mi of 10001, 11201")
        self.assertEqual(from_env({"DUO_ZIPS": "10001"}).max_miles, 25.0)

    def test_errors(self):
        for env, msg in (({"DUO_ZIPS": "1000"}, "not a 5-digit ZIP"), ({"DUO_ZIPS": "10001,abcde"}, "abcde"),
                         ({"DUO_ZIPS": "10001", "DUO_MAX_MILES": "far"}, "number of miles"),
                         ({"DUO_ZIPS": "10001", "DUO_MAX_MILES": "0"}, "more than 0"),
                         ({"DUO_ZIPS": "10001", "DUO_NEARBY_ZIPS_10001": "x"}, "DUO_NEARBY_ZIPS_10001")):
            with self.subTest(env=env), self.assertRaises(StoreConfigError) as cm:
                from_env(env)
            self.assertIn(msg, str(cm.exception))

    def test_fingerprint_changes_with_config(self):
        a = StoreConfig(("33131",), 25.0, {})
        self.assertEqual(a.fingerprint, StoreConfig(("33131",), 25.0, {}).fingerprint)
        self.assertNotEqual(a.fingerprint, StoreConfig(("33131",), 30.0, {}).fingerprint)
        self.assertNotEqual(a.fingerprint, StoreConfig(("33131", "33607"), 25.0, {}).fingerprint)

    def test_bad_config_is_a_usage_error(self):
        with mock.patch.dict("os.environ", {"DUO_ZIPS": "123"}), contextlib.redirect_stderr(io.StringIO()) as err, \
                self.assertRaises(SystemExit) as cm:
            m.main(["--list-stores"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("DUO_ZIPS: not a 5-digit ZIP code: 123", err.getvalue())


class DiscoverTests(Base):
    def test_radius_keeps_only_nearby_stores(self):
        found, region_zips = discover(StoreConfig(("33131",), 25.0, {}), fixture_fetch())
        self.assertEqual([f.store.name for f in found],
                         ["Brickell City Centre", "Miami Worldcenter", "Lincoln Road", "Dadeland", "The Falls", "Aventura"])
        self.assertEqual(found[0].store.zip_code, "33130")
        self.assertEqual(found[0].state, "FL")
        self.assertAlmostEqual(found[0].distance_mi, 0.19)
        self.assertTrue(all(f.store.poll_zip == "33131" for f in found))
        # rotation: the search ZIP, then its stores' own ZIPs within 15 mi
        self.assertEqual(region_zips, {"33131": ("33131", "33130", "33132", "33139", "33156", "33176", "33180")})

    def test_two_zips_reproduce_the_florida_set(self):
        found, region_zips = discover(StoreConfig(("33607", "33131"), 110.0, {}), fixture_fetch())
        self.assertEqual({f.store.store_id for f in found}, FLORIDA_IDS)
        by_id = {f.store.store_id: f.store for f in found}
        self.assertEqual(by_id["R019"].poll_zip, "33607")  # International Plaza: nearest to 33607
        self.assertEqual(by_id["R169"].poll_zip, "33131")  # Waterside: 104.7 mi from 33131, 129.7 from 33607
        self.assertNotIn("R111", by_id)  # Jacksonville, 169 mi: outside the radius
        self.assertEqual(set(region_zips), {"33607", "33131"})

    def test_nearby_zips_from_config_come_first(self):
        _, region_zips = discover(StoreConfig(("33131",), 5.0, {"33131": ("33125", "33127")}), fixture_fetch())
        self.assertEqual(region_zips["33131"][:3], ("33131", "33125", "33127"))

    def test_nothing_within_radius(self):
        with self.assertRaises(StoreConfigError) as cm:
            discover(StoreConfig(("33131",), 0.1, {}), fixture_fetch())
        self.assertIn("the nearest is Brickell City Centre at 0.19 mi from 33131. Raise DUO_MAX_MILES.", str(cm.exception))

    def test_refused_or_broken_answer(self):
        with self.assertRaises(StoreConfigError) as cm:
            discover(StoreConfig(("33131",)), fixture_fetch({"33131": m.FetchError("blocked", "HTTP 541", 541)}))
        self.assertIn("could not look up stores near 33131", str(cm.exception))
        with self.assertRaises(StoreConfigError):
            discover(StoreConfig(("33131",)), fixture_fetch({"33131": {"body": {}}}))


class SetupTests(Base):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = HistoryDB(Path(self.tmp.name) / "h.sqlite3")
        self.env = mock.patch.dict("os.environ", {"DUO_ZIPS": "33131", "DUO_MAX_MILES": "25"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.db.close()
        self.tmp.cleanup()
        super().tearDown()

    def test_lookup_then_cache(self):
        fetch = fixture_fetch()
        text = m.setup_stores(self.db, fetch=fetch)
        self.assertIn("6 stores within 25 mi of 33131 (looked up just now, 1 request(s))", text)
        self.assertEqual(fetch.calls, ["33131"])
        self.assertEqual(len(im.STORES), 6)
        self.assertEqual(len(im.ALL_KEYS), 6 * 8)
        self.assertEqual(m.regional_zips(), ("33131",))
        fetch2 = fixture_fetch()
        text = m.setup_stores(self.db, fetch=fetch2)
        self.assertIn("saved list", text)
        self.assertEqual(fetch2.calls, [])  # no request: cached
        self.assertEqual(list(im.STORES)[0], "R623")

    def test_cache_expires_and_refresh_forces(self):
        m.setup_stores(self.db, fetch=fixture_fetch())
        later = m.utcnow() + timedelta(days=8)
        fetch = fixture_fetch()
        with mock.patch.object(m, "utcnow", return_value=later):
            self.assertIn("looked up just now", m.setup_stores(self.db, fetch=fetch))
        self.assertEqual(fetch.calls, ["33131"])
        fetch = fixture_fetch()
        self.assertIn("looked up just now", m.setup_stores(self.db, refresh=True, fetch=fetch))
        self.assertEqual(fetch.calls, ["33131"])

    def test_no_requests_allowed_uses_stale_cache_or_explains(self):
        with self.assertRaises(StoreConfigError) as cm:
            m.setup_stores(self.db, allow_discovery=False, fetch=fixture_fetch())
        self.assertIn("--list-stores", str(cm.exception))
        m.setup_stores(self.db, fetch=fixture_fetch())
        with mock.patch.object(m, "utcnow", return_value=m.utcnow() + timedelta(days=30)):
            self.assertIn("saved list", m.setup_stores(self.db, allow_discovery=False, fetch=fixture_fetch()))

    def test_config_change_misses_cache(self):
        m.setup_stores(self.db, fetch=fixture_fetch())
        with mock.patch.dict("os.environ", {"DUO_MAX_MILES": "5"}):
            fetch = fixture_fetch()
            self.assertIn("3 stores within 5 mi", m.setup_stores(self.db, fetch=fetch))
            self.assertEqual(fetch.calls, ["33131"])

    def test_unset_restores_builtin(self):
        m.setup_stores(self.db, fetch=fixture_fetch())
        with mock.patch.dict("os.environ", {"DUO_ZIPS": ""}):
            self.assertIn("built-in Florida", m.setup_stores(self.db))
        self.assertEqual(set(im.STORES), FLORIDA_IDS)
        self.assertEqual(len(im.ALL_KEYS), 144)


class SmallStoreSetTests(Base):
    def setUp(self):
        found, region_zips = discover(StoreConfig(("33131",), 5.0, {}), fixture_fetch())  # 3 stores
        im.configure_stores([f.store for f in found], region_zips)
        m.reset_zip_picker()

    def test_check_matrix_monitor_and_dry_run_use_the_new_set(self):
        miami = load_fixture("33131")
        fetch = fixture_fetch({z: miami for z in im.REGION_ZIPS["33131"]})  # any ZIP the picker may choose
        for _ in range(20):
            out = m.check_all(fetch=fetch, picker=m.ZIP_PICKER)
            self.assertEqual(out.requests, 1)
        self.assertGreater(len(set(fetch.calls)), 1)  # it rotated through the derived ZIPs
        self.assertEqual(len(out.results), 3 * 8)
        text = m.render_matrix(1, T0, out, set(), 0, 0.1)
        self.assertIn("Brickell City Centre", text)
        self.assertNotIn("Waterside Shops", text)
        self.assertIn("Available 0/24", text)
        out_stream = io.StringIO()
        mon = m.Monitor(30.0, timedelta(minutes=10), check=lambda **_: outcome(results()), out=out_stream,
                        color=False, notifier=mock.Mock(), wall=Clock(T0))
        mon.poll()
        self.assertIn("Available 0/24", out_stream.getvalue())
        self.assertNotIn("Waterside Shops", out_stream.getvalue())
        sim = m.simulated_outcome()
        self.assertEqual([k for k, o in sim.results.items() if o.status is im.Status.AVAILABLE], [("R623", NS1T)])

    def test_list_stores_output(self):
        args = m.build_parser().parse_args(["--list-stores", "--db", "/nonexistent/h.sqlite3"])
        out = io.StringIO()
        with mock.patch.dict("os.environ", {"DUO_ZIPS": ""}):
            self.assertEqual(m.run_list_stores(args, out=out), 0)  # built-in: no DB, no request
        self.assertIn("built-in Florida store list", out.getvalue())
        self.assertIn("18 stores, 2 request(s) per poll", out.getvalue())


if __name__ == "__main__":
    unittest.main()
