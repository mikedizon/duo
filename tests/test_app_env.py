"""The .env loader and DUO_ALERT_PARTS (which configurations open the purchase page)."""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import NS1T, NS2T, NS512, SW256, WATERSIDE, COCONUT, m, quiet_logs
from app_env import EnvFileError, load_env, parse_env
from inventory_model import ALL_PARTS, PREFERRED_PARTS, SKU_BY_PART, parse_alert_parts


def setUpModule():
    quiet_logs()


class ParseEnvTests(unittest.TestCase):
    def test_format(self):
        text = ("# comment\n\nexport A=1\nB = two words \nC='quoted # not a comment'\nD=\"x\"\n"
                "E=value # trailing comment\nF=\n")
        self.assertEqual(parse_env(text), {"A": "1", "B": "two words", "C": "quoted # not a comment",
                                           "D": "x", "E": "value", "F": ""})

    def test_bad_lines(self):
        for text in ("JUSTAKEY\n", "1BAD=x\n", "has space=x\n"):
            with self.subTest(text=text), self.assertRaises(EnvFileError):
                parse_env(text)

    def test_load_does_not_override_real_environment(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text("A=from-file\nB=from-file\n")
            env = {"A": "real"}
            self.assertEqual(load_env(p, env), {"A": "from-file", "B": "from-file"})
            self.assertEqual(env, {"A": "real", "B": "from-file"})
            self.assertEqual(load_env(Path(d) / "missing.env", env), {})


class AlertPartsTests(unittest.TestCase):
    def test_codes_parts_and_order(self):
        self.assertEqual(parse_alert_parts("ns2t, NS1T"), (NS2T, NS1T))
        self.assertEqual(parse_alert_parts(f"{NS512},NS512,SW256"), (NS512, SW256))

    def test_all(self):
        parts = parse_alert_parts("ALL")
        self.assertEqual(parts[:2], PREFERRED_PARTS)
        self.assertEqual(sorted(parts), sorted(ALL_PARTS))

    def test_invalid(self):
        for value in ("NS3T", "", " , "):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_alert_parts(value)


class ChoosePurchaseTests(unittest.TestCase):
    def test_default_policy_unchanged(self):
        self.assertEqual(m.PURCHASE_ALERT_PARTS, PREFERRED_PARTS)
        self.assertIsNone(m.choose_purchase([(WATERSIDE, SW256)], lambda k: False))
        self.assertEqual(m.choose_purchase([(WATERSIDE, NS2T)], lambda k: False).part, NS2T)
        self.assertIsNone(m.choose_purchase([(WATERSIDE, NS2T)], lambda k: k == (COCONUT, NS1T)))

    def test_all_configurations(self):
        with mock.patch.object(m, "PURCHASE_ALERT_PARTS", parse_alert_parts("all")):
            self.assertEqual(m.choose_purchase([(WATERSIDE, SW256)], lambda k: False).part, SW256)
            self.assertEqual(m.choose_purchase([(WATERSIDE, SW256), (COCONUT, NS512)], lambda k: False).part, NS512)
            # A higher-priority configuration already available elsewhere: its page was already opened.
            self.assertIsNone(m.choose_purchase([(WATERSIDE, SW256)], lambda k: k == (COCONUT, NS1T)))
            self.assertEqual(m.choose_purchase([(WATERSIDE, NS1T)], lambda k: True).part, NS1T)


class MainEnvTests(unittest.TestCase):
    def run_main(self, env_text: str | None, environ: dict[str, str] | None = None):
        with tempfile.TemporaryDirectory() as d:
            env_file = Path(d) / ".env"
            if env_text is not None:
                env_file.write_text(env_text)
            base = {k: v for k, v in os.environ.items() if k != "DUO_ALERT_PARTS"}
            with mock.patch.object(m, "DEFAULT_ENV_FILE", env_file), \
                    mock.patch.dict("os.environ", {**base, **(environ or {})}, clear=True), \
                    mock.patch.object(m, "PURCHASE_ALERT_PARTS", PREFERRED_PARTS), \
                    mock.patch.object(m, "run_dry_run", return_value=0), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                try:
                    rc = m.main(["--dry-run"])
                except SystemExit as exc:
                    rc = exc.code
                return rc, m.PURCHASE_ALERT_PARTS, err.getvalue()

    def test_env_file_sets_alert_parts(self):
        rc, parts, _ = self.run_main("DUO_ALERT_PARTS=NS512,NS1T\n")
        self.assertEqual((rc, parts), (0, (NS512, NS1T)))

    def test_real_environment_wins(self):
        rc, parts, _ = self.run_main("DUO_ALERT_PARTS=all\n", {"DUO_ALERT_PARTS": "NS2T"})
        self.assertEqual(parts, (NS2T,))

    def test_no_env_file_keeps_default(self):
        rc, parts, _ = self.run_main(None)
        self.assertEqual((rc, parts), (0, PREFERRED_PARTS))

    def test_invalid_value_is_a_usage_error(self):
        rc, _, err = self.run_main("DUO_ALERT_PARTS=NS9T\n")
        self.assertEqual(rc, 2)
        self.assertIn("DUO_ALERT_PARTS: unknown configuration 'NS9T'", err)

    def test_malformed_env_file(self):
        rc, _, err = self.run_main("not a valid line\n")
        self.assertEqual(rc, 2)
        self.assertIn(".env line 1", err)


if __name__ == "__main__":
    unittest.main()
