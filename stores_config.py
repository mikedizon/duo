"""Which Apple Stores to monitor: discovered from Apple's pickup answers for the
configured ZIP code(s).

Configuration (environment or .env):
  DUO_ZIPS=10001,11201        search ZIPs; each becomes one request per poll
  DUO_MAX_MILES=25            keep stores within this distance of any search ZIP
  DUO_NEARBY_ZIPS_10001=...   optional extra ZIPs that request may rotate through

Apple answers a ZIP with its 12 nearest stores and their distances. A store
within DUO_MAX_MILES of any search ZIP is monitored, and is requested through
the search ZIP it is nearest to. Without DUO_ZIPS the built-in Florida set in
inventory_model is used unchanged.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from inventory_model import Store

DEFAULT_MAX_MILES = 25.0
NEARBY_STORE_ZIP_MILES = 15.0  # a store's own ZIP this close to the search ZIP joins its rotation
_ZIP = re.compile(r"^\d{5}$")


class StoreConfigError(ValueError):
    """Configuration or discovery problem that stops startup, with a message for the user."""


@dataclass(frozen=True)
class StoreConfig:
    zips: tuple[str, ...]
    max_miles: float = DEFAULT_MAX_MILES
    nearby: Mapping[str, tuple[str, ...]] = None  # type: ignore[assignment]

    @property
    def fingerprint(self) -> str:
        """Identifies the configuration, so a cached store list is reused only for the same one."""
        nearby = self.nearby or {}
        text = "zips=" + ",".join(self.zips) + f";miles={self.max_miles:g};nearby=" + ";".join(
            f"{z}:{','.join(nearby.get(z, ()))}" for z in self.zips)
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    def describe(self) -> str:
        return f"within {self.max_miles:g} mi of {', '.join(self.zips)}"


def _zip_list(value: str, name: str) -> tuple[str, ...]:
    zips = tuple(dict.fromkeys(z.strip() for z in value.replace(" ", ",").split(",") if z.strip()))
    bad = [z for z in zips if not _ZIP.match(z)]
    if bad:
        raise StoreConfigError(f"{name}: not a 5-digit ZIP code: {', '.join(bad)}")
    return zips


def from_env(environ: Mapping[str, str]) -> StoreConfig | None:
    """The configured search, or None when DUO_ZIPS is unset (built-in Florida set)."""
    raw = environ.get("DUO_ZIPS", "").strip()
    if not raw:
        return None
    zips = _zip_list(raw, "DUO_ZIPS")
    if not zips:
        raise StoreConfigError("DUO_ZIPS: no ZIP codes given")
    miles_text = environ.get("DUO_MAX_MILES", "").strip()
    try:
        miles = float(miles_text) if miles_text else DEFAULT_MAX_MILES
    except ValueError:
        raise StoreConfigError("DUO_MAX_MILES: must be a number of miles") from None
    if not 0 < miles <= 500:
        raise StoreConfigError("DUO_MAX_MILES: must be more than 0 and at most 500")
    nearby = {z: _zip_list(environ[f"DUO_NEARBY_ZIPS_{z}"], f"DUO_NEARBY_ZIPS_{z}")
              for z in zips if environ.get(f"DUO_NEARBY_ZIPS_{z}", "").strip()}
    return StoreConfig(zips, miles, nearby)


@dataclass(frozen=True)
class FoundStore:
    store: Store
    state: str
    distance_mi: float  # from the search ZIP it is assigned to


def _stores_in(payload: Any, zip_code: str) -> list[dict[str, Any]]:
    try:
        stores = payload["body"]["stores"]
    except (KeyError, TypeError):
        raise StoreConfigError(f"Apple's answer for {zip_code} has no store list") from None
    if not isinstance(stores, list):
        raise StoreConfigError(f"Apple's answer for {zip_code} has no store list")
    return [s for s in stores if isinstance(s, dict)]


def discover(cfg: StoreConfig, fetch: Callable[[str], Any]) -> tuple[list[FoundStore], dict[str, tuple[str, ...]]]:
    """One request per search ZIP. Returns the stores to monitor (nearest first) and each
    search ZIP's rotation list. Raises StoreConfigError."""
    best: dict[str, tuple[float, str, dict[str, Any]]] = {}  # store id -> (distance, search zip, raw)
    for zip_code in cfg.zips:
        try:
            payload = fetch(zip_code)
        except Exception as exc:  # FetchError and anything unexpected: stop with a clear message
            raise StoreConfigError(f"could not look up stores near {zip_code}: {exc}") from None
        for raw in _stores_in(payload, zip_code):
            sid = str(raw.get("storeNumber") or "").strip()
            try:
                dist = float(raw.get("storedistance"))
            except (TypeError, ValueError):
                continue
            if sid and (sid not in best or dist < best[sid][0]):
                best[sid] = (dist, zip_code, raw)
    if not best:
        raise StoreConfigError(f"Apple listed no stores near {', '.join(cfg.zips)}")
    kept = sorted(((d, z, r) for d, z, r in best.values() if d <= cfg.max_miles), key=lambda t: t[0])
    if not kept:
        d, z, r = min(best.values(), key=lambda t: t[0])
        raise StoreConfigError(f"no Apple Store within {cfg.max_miles:g} mi of {', '.join(cfg.zips)}; the nearest is "
                               f"{r.get('storeName', '?')} at {d:g} mi from {z}. Raise DUO_MAX_MILES.")
    found: list[FoundStore] = []
    for dist, zip_code, raw in kept:
        address = raw.get("address") if isinstance(raw.get("address"), dict) else {}
        postal = str(address.get("postalCode") or "").strip()[:5]
        found.append(FoundStore(
            Store(str(raw["storeNumber"]).strip(), str(raw.get("storeName") or raw["storeNumber"]).strip(),
                  str(raw.get("city") or "").strip(), postal if _ZIP.match(postal) else zip_code,
                  f"near {zip_code}", zip_code),
            str(raw.get("state") or "").strip(), dist))
    nearby = cfg.nearby or {}
    region_zips: dict[str, tuple[str, ...]] = {}
    for zip_code in cfg.zips:
        if not any(f.store.poll_zip == zip_code for f in found):
            continue
        local = [f.store.zip_code for f in found if f.store.poll_zip == zip_code
                 and f.distance_mi <= NEARBY_STORE_ZIP_MILES]
        region_zips[zip_code] = tuple(dict.fromkeys((zip_code, *nearby.get(zip_code, ()), *local)))
    return found, region_zips
