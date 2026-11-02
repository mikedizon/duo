"""Shared fixtures and builders for the test suite."""

from __future__ import annotations

import atexit
import json
import logging
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Keep every default temp path inside a throwaway directory, never the user's real one.
_TEST_TMP = tempfile.mkdtemp(prefix="duo-tests-")
tempfile.tempdir = _TEST_TMP
atexit.register(shutil.rmtree, _TEST_TMP, True)

import iphone_monitor as m  # noqa: E402

# Never read the user's real .env, and start every run from the default alert list.
m.DEFAULT_ENV_FILE = Path(_TEST_TMP) / "absent.env"
os.environ.pop("DUO_ALERT_PARTS", None)
from inventory_model import ALL_KEYS, ALL_PARTS, REGION_ZIPS, Key, Observation, Status  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
# Any ZIP from a region's list answers with that region's recorded fixture.
REGION_OF_ZIP = {z: region for region, zs in REGION_ZIPS.items() for z in zs}
A, U, I, K, E = (Status.AVAILABLE, Status.UNAVAILABLE, Status.INELIGIBLE, Status.UNKNOWN, Status.ERROR)

NS256, NS512, NS1T, NS2T, SW256, SW512, SW1T, SW2T = ALL_PARTS
WATERSIDE, COCONUT, FLORIDA_MALL = "R169", "R185", "R143"

# 2026-10-27 09:42:00 EDT
T0 = datetime(2026, 10, 27, 13, 42, 0, tzinfo=timezone.utc)


def quiet_logs() -> None:
    logging.disable(logging.CRITICAL)


def load_fixture(zip_code: str) -> dict:
    return json.loads((FIXTURES / f"pickup8_{zip_code}_unavailable.json").read_text())


def set_display(payload: dict, store_id: str, part: str, value) -> dict:
    for store in payload["body"]["stores"]:
        if store["storeNumber"] == store_id:
            store["partsAvailability"][part]["pickupDisplay"] = value
            return payload
    raise KeyError(store_id)


def drop_store(payload: dict, store_id: str) -> dict:
    payload["body"]["stores"] = [s for s in payload["body"]["stores"] if s["storeNumber"] != store_id]
    return payload


def fixture_fetch(overrides: Mapping[str, object] | None = None):
    """fetch(zip) returning the real regional fixtures (or overrides by ZIP, or by region center ZIP)."""
    overrides = dict(overrides or {})
    calls: list[str] = []

    def fetch(zip_code: str):
        calls.append(zip_code)
        key = zip_code if zip_code in overrides else REGION_OF_ZIP.get(zip_code, zip_code)  # region-wide override
        if key in overrides:
            val = overrides[key]
            if isinstance(val, Exception):
                raise val
            return val
        return load_fixture(REGION_OF_ZIP.get(zip_code, zip_code))

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def results(available: Iterable[Key] = (), **_: object) -> dict[Key, Observation]:
    avail = set(available)
    return {k: Observation(k[0], k[1], A if k in avail else U) for k in ALL_KEYS}


def with_status(base: dict[Key, Observation], statuses: Mapping[Key, Status]) -> dict[Key, Observation]:
    out = dict(base)
    for k, st in statuses.items():
        out[k] = Observation(k[0], k[1], st)
    return out


def store_status(base: dict[Key, Observation], store_id: str, status: Status) -> dict[Key, Observation]:
    return with_status(base, {(store_id, p): status for p in ALL_PARTS})


def outcome(res: dict[Key, Observation], errors=()) -> "m.CheckOutcome":
    return m.CheckOutcome(dict(res), list(errors), 2)


class Clock:
    """Wall clock for Monitor tests."""

    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> datetime:
        self.now += timedelta(**kw)
        return self.now
