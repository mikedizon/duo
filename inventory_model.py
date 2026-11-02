"""Shared configuration and value types for the iPhone Duo pickup monitor."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/New_York")

PURCHASE_BASE = "https://www.apple.com/shop/buy-iphone/iphone-duo/7.6-inch-display-"


class Status(enum.Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    INELIGIBLE = "ineligible"
    UNKNOWN = "unknown"  # response received but could not be interpreted
    ERROR = "error"  # request failed

    @property
    def is_confirmed(self) -> bool:
        """True when Apple gave a definite answer for this store and part."""
        return self in (Status.AVAILABLE, Status.UNAVAILABLE, Status.INELIGIBLE)


@dataclass(frozen=True)
class Sku:
    part: str
    code: str  # short column label, e.g. NS1T
    color: str
    capacity: str
    capacity_gb: int

    @property
    def label(self) -> str:
        return f"{self.capacity} {self.color}"

    @property
    def purchase_url(self) -> str:
        slug = f"{self.capacity.lower()}-{self.color.lower().replace(' ', '-')}-verizon"
        return PURCHASE_BASE + slug


# Part numbers verified 2026-10-27 from the analytics "products" list on
# https://www.apple.com/shop/buy-iphone/iphone-duo (sku, partNumber, name),
# and each was confirmed present in every store's partsAvailability from
# pickup-message requests batching parts.0..parts.7.
SKUS: tuple[Sku, ...] = (
    Sku("MK1F4LL/A", "NS256", "Night Sky", "256GB", 256),
    Sku("MK1H4LL/A", "NS512", "Night Sky", "512GB", 512),
    Sku("MK1L4LL/A", "NS1T", "Night Sky", "1TB", 1024),
    Sku("MK1R4LL/A", "NS2T", "Night Sky", "2TB", 2048),
    Sku("MK1E4LL/A", "SW256", "Star White", "256GB", 256),
    Sku("MK1G4LL/A", "SW512", "Star White", "512GB", 512),
    Sku("MK1J4LL/A", "SW1T", "Star White", "1TB", 1024),
    Sku("MK1Q4LL/A", "SW2T", "Star White", "2TB", 2048),
)
SKU_BY_PART: dict[str, Sku] = {s.part: s for s in SKUS}
ALL_PARTS: tuple[str, ...] = tuple(s.part for s in SKUS)

# Purchase priority: open the first of these that is newly available.
PREFERRED_PARTS: tuple[str, ...] = ("MK1L4LL/A", "MK1R4LL/A")  # 1TB NS, then 2TB NS
SKU_BY_CODE: dict[str, Sku] = {s.code: s for s in SKUS}


def parse_alert_parts(value: str) -> tuple[str, ...]:
    """Purchase-alert parts from a comma list of codes (NS1T) or part numbers, in priority
    order, or `all` (1TB NS, 2TB NS, then every other configuration). Raises ValueError."""
    text = value.strip()
    if text.lower() == "all":
        return PREFERRED_PARTS + tuple(p for p in ALL_PARTS if p not in PREFERRED_PARTS)
    out: list[str] = []
    for item in (i.strip() for i in text.split(",")):
        if not item:
            continue
        sku = SKU_BY_CODE.get(item.upper()) or SKU_BY_PART.get(item.upper())
        if sku is None:
            raise ValueError(f"unknown configuration {item!r}; use codes like "
                             + ", ".join(s.code for s in SKUS) + " or 'all'")
        if sku.part not in out:
            out.append(sku.part)
    if not out:
        raise ValueError("no configurations given")
    return tuple(out)


@dataclass(frozen=True)
class Store:
    store_id: str
    name: str
    city: str
    zip_code: str
    region: str
    poll_zip: str  # regional ZIP whose response is used for this store


# Store numbers and names verified 2026-10-27 against pickup-message
# responses for ZIPs 34108, 33607, 32839 and 33131. Jacksonville (R111) is
# returned too but is outside the monitored regions and is ignored.
# 33607 lists all SW Florida, Tampa Bay and Orlando stores; 33131 lists all
# Southeast Florida stores. Two requests per cycle cover all 18.
_TAMPA, _MIAMI = "33607", "33131"
FLORIDA_STORES: tuple[Store, ...] = (
    Store("R169", "Waterside Shops", "Naples", "34108", "Southwest Florida", _TAMPA),
    Store("R185", "Coconut Point", "Estero", "33928", "Southwest Florida", _TAMPA),
    Store("R615", "University Town Center", "Sarasota", "34243", "Southwest Florida", _TAMPA),
    Store("R019", "International Plaza", "Tampa", "33607", "Tampa Bay", _TAMPA),
    Store("R289", "Brandon", "Brandon", "33511", "Tampa Bay", _TAMPA),
    Store("R143", "Florida Mall", "Orlando", "32809", "Orlando", _TAMPA),
    Store("R053", "Millenia", "Orlando", "32839", "Orlando", _TAMPA),
    Store("R291", "Altamonte", "Altamonte Springs", "32701", "Orlando", _TAMPA),
    Store("R012", "The Falls", "Miami", "33176", "Southeast Florida", _MIAMI),
    Store("R312", "Dadeland", "Miami", "33156", "Southeast Florida", _MIAMI),
    Store("R752", "Miami Worldcenter", "Miami", "33132", "Southeast Florida", _MIAMI),
    Store("R623", "Brickell City Centre", "Miami", "33130", "Southeast Florida", _MIAMI),
    Store("R115", "Lincoln Road", "Miami Beach", "33139", "Southeast Florida", _MIAMI),
    Store("R087", "Aventura", "Aventura", "33180", "Southeast Florida", _MIAMI),
    Store("R214", "The Galleria", "Fort Lauderdale", "33304", "Southeast Florida", _MIAMI),
    Store("R074", "Boca Raton", "Boca Raton", "33431", "Southeast Florida", _MIAMI),
    Store("R024", "Wellington Green", "Wellington", "33414", "Southeast Florida", _MIAMI),
    Store("R127", "The Gardens Mall", "Palm Beach Gardens", "33410", "Southeast Florida", _MIAMI),
)

# ZIPs each region's request may use, picked at random per poll (2026-10-28).
# All are real ZIPs within roughly 10 miles of the region's center ZIP, and
# each list includes the center. Only the centers were verified to list every
# store of their region; a ZIP whose response misses a store is dropped for the
# rest of the run (see iphone_monitor.ZipPicker) and the store's own ZIP fills it.
FLORIDA_REGION_ZIPS: dict[str, tuple[str, ...]] = {
    _TAMPA: ("33602", "33603", "33604", "33606", "33607", "33609", "33610", "33611", "33612", "33614",
             "33618", "33629"),
    _MIAMI: ("33125", "33127", "33128", "33129", "33130", "33131", "33132", "33133", "33135", "33136",
             "33137", "33145"),
}

Key = tuple[str, str]  # (store_id, part)

# The monitored store set. Built-in default: the Florida set above. With DUO_ZIPS
# configured, stores_config replaces it at startup via configure_stores(). These
# three are mutated in place, never rebound, so `from inventory_model import STORES`
# elsewhere keeps seeing the current set.
STORES: dict[str, Store] = {}
REGION_ZIPS: dict[str, tuple[str, ...]] = {}
ALL_KEYS: list[Key] = []


def configure_stores(stores: Iterable[Store], region_zips: Mapping[str, Sequence[str]]) -> None:
    """Replace the monitored store set. Every store's poll_zip must be a key of `region_zips`."""
    stores = list(stores)
    if not stores:
        raise ValueError("no stores to monitor")
    missing = {s.poll_zip for s in stores} - set(region_zips)
    if missing:
        raise ValueError(f"stores refer to regions without ZIPs: {sorted(missing)}")
    STORES.clear()
    STORES.update({s.store_id: s for s in stores})
    REGION_ZIPS.clear()
    REGION_ZIPS.update({r: tuple(dict.fromkeys(zs)) for r, zs in region_zips.items() if r in {s.poll_zip for s in stores}})
    ALL_KEYS[:] = [(sid, p) for sid in STORES for p in ALL_PARTS]


def configure_florida_default() -> None:
    configure_stores(FLORIDA_STORES, FLORIDA_REGION_ZIPS)


configure_florida_default()


@dataclass(frozen=True)
class Observation:
    store_id: str
    part: str
    status: Status
    detail: str = ""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_utc_text(dt: datetime) -> str:
    """Canonical stored form: ISO 8601, UTC, millisecond precision."""
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def from_utc_text(text: str) -> datetime:
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def local(dt: datetime) -> datetime:
    return dt.astimezone(LOCAL_TZ)


def fmt_local(dt: datetime, with_date: bool = True) -> str:
    return local(dt).strftime("%Y-%m-%d %H:%M:%S %Z" if with_date else "%H:%M:%S %Z")
