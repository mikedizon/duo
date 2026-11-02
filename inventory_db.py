"""SQLite history for the pickup monitor.

Schema versions are tracked with PRAGMA user_version and applied in order,
each inside one transaction. Before upgrading a database that already holds
data, a backup copy is written next to it.

Observation encoding (lossless, compact): every poll stores one
poll_store_results row per store. For a store whose outcome is 'ok' or
'partial', a SKU with no sku_observations row was confirmed UNAVAILABLE.
Rows are written only for available, ineligible, unknown and error
statuses. Stores with outcome 'unknown' or 'error' have no SKU rows; the
outcome applies to all of their SKUs.

All timestamps are ISO 8601 text in UTC (see inventory_model.to_utc_text).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from inventory_model import (
    ALL_PARTS, SKU_BY_PART, Key, Observation, Status, from_utc_text, to_utc_text,
)
from inventory_waves import (
    BECAME_UNAVAILABLE, EXACT, PriorState, Transition, Wave, WaveUpdate,
)

log = logging.getLogger("iphone_monitor")

MIGRATIONS: dict[int, str] = {
    # v1: polling history, SKU observations, SKU-level availability events.
    1: """
    CREATE TABLE polls (
        poll_id INTEGER PRIMARY KEY,
        started_utc TEXT NOT NULL UNIQUE,
        finished_utc TEXT NOT NULL,
        duration_ms INTEGER NOT NULL,
        requests INTEGER NOT NULL,
        error_count INTEGER NOT NULL,
        error_summary TEXT,
        available_count INTEGER NOT NULL
    );
    CREATE TABLE poll_store_results (
        poll_id INTEGER NOT NULL REFERENCES polls(poll_id) ON DELETE CASCADE,
        store_id TEXT NOT NULL,
        outcome TEXT NOT NULL CHECK (outcome IN ('ok', 'partial', 'unknown', 'error')),
        PRIMARY KEY (poll_id, store_id)
    ) WITHOUT ROWID;
    CREATE TABLE sku_observations (
        poll_id INTEGER NOT NULL REFERENCES polls(poll_id) ON DELETE CASCADE,
        store_id TEXT NOT NULL,
        part TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('available', 'ineligible', 'unknown', 'error')),
        PRIMARY KEY (poll_id, store_id, part)
    ) WITHOUT ROWID;
    CREATE TABLE availability_events (
        event_id INTEGER PRIMARY KEY,
        poll_id INTEGER REFERENCES polls(poll_id),
        store_id TEXT NOT NULL,
        part TEXT NOT NULL,
        event_type TEXT NOT NULL CHECK (event_type IN ('became_available', 'became_unavailable')),
        observed_utc TEXT NOT NULL,
        previous_status TEXT,
        previous_confirmed_utc TEXT,
        last_unavailable_utc TEXT,
        timing TEXT NOT NULL,
        UNIQUE (store_id, part, event_type, observed_utc)
    );
    CREATE INDEX idx_events_store_time ON availability_events (store_id, observed_utc);
    """,
    # v2: monitor runs and store-level restock waves.
    2: """
    CREATE TABLE monitor_runs (
        run_id INTEGER PRIMARY KEY,
        started_utc TEXT NOT NULL UNIQUE,
        ended_utc TEXT,
        interval_s REAL NOT NULL,
        wave_window_s REAL NOT NULL
    );
    ALTER TABLE polls ADD COLUMN run_id INTEGER REFERENCES monitor_runs(run_id);
    CREATE INDEX idx_polls_run ON polls (run_id);
    CREATE TABLE store_restock_waves (
        wave_id INTEGER PRIMARY KEY,
        store_id TEXT NOT NULL,
        first_detected_utc TEXT NOT NULL,
        last_transition_utc TEXT NOT NULL,
        sku_count INTEGER NOT NULL,
        supporting_polls INTEGER NOT NULL DEFAULT 0,
        present_at_startup INTEGER NOT NULL DEFAULT 0,
        gap_affected INTEGER NOT NULL DEFAULT 0,
        coverage_gap INTEGER NOT NULL DEFAULT 0,
        confidence TEXT NOT NULL,
        event_class TEXT NOT NULL,
        closed INTEGER NOT NULL DEFAULT 0,
        run_id INTEGER REFERENCES monitor_runs(run_id),
        UNIQUE (store_id, first_detected_utc)
    );
    CREATE INDEX idx_waves_store_time ON store_restock_waves (store_id, first_detected_utc);
    CREATE TABLE restock_wave_members (
        wave_id INTEGER NOT NULL REFERENCES store_restock_waves(wave_id) ON DELETE CASCADE,
        part TEXT NOT NULL,
        color TEXT NOT NULL,
        capacity TEXT NOT NULL,
        first_available_utc TEXT NOT NULL,
        last_unavailable_utc TEXT,
        timing TEXT NOT NULL,
        last_seen_available_utc TEXT,
        ended_utc TEXT,
        duration_s REAL,
        PRIMARY KEY (wave_id, part)
    );
    """,
    # v3: WireGuard tunnel sessions and lifecycle events; polls record the tunnel they used.
    3: """
    CREATE TABLE vpn_sessions (
        session_id INTEGER PRIMARY KEY,
        config_name TEXT NOT NULL,
        config_path TEXT NOT NULL,
        wg_name TEXT NOT NULL,
        interface TEXT,
        endpoint TEXT NOT NULL,
        copy_path TEXT NOT NULL,
        started_utc TEXT NOT NULL,
        ready_utc TEXT,
        ended_utc TEXT,
        state TEXT NOT NULL,
        public_ip TEXT,
        previous_ip TEXT,
        ip_changed INTEGER,
        connect_ms INTEGER,
        start_reason TEXT,
        rotation_reason TEXT,
        failure_category TEXT,
        teardown_outcome TEXT,
        baseline_json TEXT NOT NULL
    );
    CREATE INDEX idx_vpn_sessions_open ON vpn_sessions (ended_utc);
    CREATE TABLE vpn_events (
        event_id INTEGER PRIMARY KEY,
        session_id INTEGER REFERENCES vpn_sessions(session_id),
        at_utc TEXT NOT NULL,
        event TEXT NOT NULL,
        state TEXT NOT NULL,
        fields_json TEXT NOT NULL
    );
    CREATE INDEX idx_vpn_events_time ON vpn_events (at_utc);
    ALTER TABLE polls ADD COLUMN vpn_session_id INTEGER REFERENCES vpn_sessions(session_id);
    """,
    # v4: tunnels are WireGuard-app services; record which one (by UUID) a session started.
    4: """
    ALTER TABLE vpn_sessions ADD COLUMN service_id TEXT;
    """,
    # v5: where a geolocation service (ipinfo.io) places each tunnel's public IP.
    5: """
    ALTER TABLE vpn_sessions ADD COLUMN location TEXT;
    """,
    # v6: store lists discovered from DUO_ZIPS (see stores_config), cached per configuration.
    6: """
    CREATE TABLE store_sets (
        fingerprint TEXT PRIMARY KEY,
        description TEXT NOT NULL,
        saved_utc TEXT NOT NULL,
        region_zips_json TEXT NOT NULL
    );
    CREATE TABLE store_set_members (
        fingerprint TEXT NOT NULL REFERENCES store_sets(fingerprint) ON DELETE CASCADE,
        store_id TEXT NOT NULL,
        name TEXT NOT NULL,
        city TEXT NOT NULL,
        state TEXT NOT NULL,
        zip_code TEXT NOT NULL,
        region TEXT NOT NULL,
        poll_zip TEXT NOT NULL,
        distance_mi REAL NOT NULL,
        position INTEGER NOT NULL,
        PRIMARY KEY (fingerprint, store_id)
    );
    """,
}
LATEST_VERSION = max(MIGRATIONS)
VPN_SESSION_COLUMNS = frozenset({
    "interface", "ready_utc", "ended_utc", "state", "public_ip", "location", "previous_ip", "ip_changed", "connect_ms",
    "rotation_reason", "failure_category", "teardown_outcome",
})


def _statements(script: str) -> list[str]:
    return [s.strip() for s in script.split(";") if s.strip()]


@dataclass
class PollRecord:
    started: datetime
    finished: datetime
    requests: int
    errors: Sequence[Any]  # FetchError-like: has .kind
    results: Mapping[Key, Observation]
    store_outcomes: Mapping[str, str]
    transitions: Sequence[Transition] = ()
    waves: WaveUpdate = field(default_factory=WaveUpdate)
    vpn_session_id: int | None = None

    @property
    def available_count(self) -> int:
        return sum(1 for o in self.results.values() if o.status is Status.AVAILABLE)


class HistoryDB:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        # check_same_thread=False: with --tui the monitor loop runs in a worker thread. Only one thread
        # uses the connection at a time (the worker while running, then the main thread after it stops).
        self.conn = sqlite3.connect(str(self.path), isolation_level=None, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if str(path) != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.migrate()

    def close(self) -> None:
        self.conn.close()

    # -- schema -------------------------------------------------------------

    @property
    def version(self) -> int:
        return int(self.conn.execute("PRAGMA user_version").fetchone()[0])

    def _has_data(self) -> bool:
        tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return "polls" in tables and self.conn.execute("SELECT 1 FROM polls LIMIT 1").fetchone() is not None

    def backup(self, dest: Path) -> None:
        target = sqlite3.connect(str(dest))
        try:
            self.conn.backup(target)
        finally:
            target.close()

    def migrate(self) -> None:
        current = self.version
        if current > LATEST_VERSION:
            raise RuntimeError(
                f"{self.path} has schema v{current}, newer than this script (v{LATEST_VERSION})")
        if current == LATEST_VERSION:
            return
        if current > 0 and self._has_data() and str(self.path) != ":memory:":
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            dest = self.path.with_name(f"{self.path.name}.v{current}-backup-{stamp}")
            self.backup(dest)
            log.warning("database backed up before migration",
                        extra={"fields": {"backup": str(dest), "from": current, "to": LATEST_VERSION}})
        for version in range(current + 1, LATEST_VERSION + 1):
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                for stmt in _statements(MIGRATIONS[version]):
                    self.conn.execute(stmt)
                self.conn.execute(f"PRAGMA user_version = {version}")
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
            log.info("schema migrated", extra={"fields": {"version": version}})

    # -- writing ------------------------------------------------------------

    def start_run(self, started: datetime, interval_s: float, wave_window_s: float) -> int:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            # Waves left open by a crashed run can no longer be extended.
            self.conn.execute("UPDATE store_restock_waves SET closed = 1 WHERE closed = 0")
            cur = self.conn.execute(
                "INSERT INTO monitor_runs (started_utc, interval_s, wave_window_s) VALUES (?, ?, ?)",
                (to_utc_text(started), interval_s, wave_window_s))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return int(cur.lastrowid)

    def end_run(self, run_id: int, ended: datetime, open_waves: Iterable[Wave] = ()) -> None:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for w in open_waves:
                if w.wave_id is not None:
                    self.conn.execute("UPDATE store_restock_waves SET closed = 1 WHERE wave_id = ?", (w.wave_id,))
            self.conn.execute("UPDATE monitor_runs SET ended_utc = ? WHERE run_id = ?",
                              (to_utc_text(ended), run_id))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def record_poll(self, run_id: int | None, rec: PollRecord) -> int:
        """Write one poll and everything derived from it in one transaction."""
        c = self.conn
        c.execute("BEGIN IMMEDIATE")
        try:
            kinds = sorted({getattr(e, "kind", "error") for e in rec.errors})
            cur = c.execute(
                "INSERT INTO polls (started_utc, finished_utc, duration_ms, requests, error_count,"
                " error_summary, available_count, run_id, vpn_session_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (to_utc_text(rec.started), to_utc_text(rec.finished),
                 round((rec.finished - rec.started).total_seconds() * 1000), rec.requests,
                 len(rec.errors), ",".join(kinds) or None, rec.available_count, run_id, rec.vpn_session_id))
            poll_id = int(cur.lastrowid)
            c.executemany(
                "INSERT INTO poll_store_results (poll_id, store_id, outcome) VALUES (?, ?, ?)",
                [(poll_id, sid, outcome) for sid, outcome in rec.store_outcomes.items()])
            rows = [
                (poll_id, o.store_id, o.part, o.status.value)
                for o in rec.results.values()
                if o.status is not Status.UNAVAILABLE
                and rec.store_outcomes.get(o.store_id) in ("ok", "partial")
            ]
            c.executemany(
                "INSERT INTO sku_observations (poll_id, store_id, part, status) VALUES (?, ?, ?, ?)", rows)
            for t in rec.transitions:
                c.execute(
                    "INSERT OR IGNORE INTO availability_events (poll_id, store_id, part, event_type,"
                    " observed_utc, previous_status, previous_confirmed_utc, last_unavailable_utc, timing)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (poll_id, t.store_id, t.part, t.kind, to_utc_text(t.at),
                     t.previous_status.value if t.previous_status else None,
                     _opt(t.previous_confirmed_at), _opt(t.last_unavailable_at), t.timing))
                if t.kind == BECAME_UNAVAILABLE:
                    self._end_member(t)
            self._write_waves(run_id, rec.waves)
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
        return poll_id

    def _end_member(self, t: Transition) -> None:
        # Duration is recorded only when both ends were observed without a gap.
        exact_end = 1 if t.timing == EXACT else 0
        self.conn.execute(
            """
            UPDATE restock_wave_members
               SET ended_utc = :ended,
                   last_seen_available_utc = :last_seen,
                   duration_s = CASE WHEN :exact_end = 1 AND timing = :exact
                                     THEN round((julianday(:ended) - julianday(first_available_utc)) * 86400, 3)
                                     END
             WHERE part = :part AND ended_utc IS NULL
               AND wave_id IN (SELECT wave_id FROM store_restock_waves WHERE store_id = :store)
            """,
            {"ended": to_utc_text(t.at), "last_seen": _opt(t.previous_confirmed_at),
             "exact_end": exact_end, "exact": EXACT, "part": t.part, "store": t.store_id})

    def _write_waves(self, run_id: int | None, upd: WaveUpdate) -> None:
        c = self.conn
        for w in upd.opened:
            cur = c.execute(
                "INSERT OR IGNORE INTO store_restock_waves (store_id, first_detected_utc, last_transition_utc,"
                " sku_count, confidence, event_class, run_id) VALUES (?, ?, ?, 0, ?, ?, ?)",
                (w.store_id, to_utc_text(w.first_at), to_utc_text(w.last_at), w.confidence, w.event_class, run_id))
            if cur.rowcount:
                w.wave_id = int(cur.lastrowid)
            else:
                w.wave_id = int(c.execute(
                    "SELECT wave_id FROM store_restock_waves WHERE store_id = ? AND first_detected_utc = ?",
                    (w.store_id, to_utc_text(w.first_at))).fetchone()[0])
        for w, m in upd.joined:
            sku = SKU_BY_PART[m.part]
            c.execute(
                "INSERT OR IGNORE INTO restock_wave_members (wave_id, part, color, capacity,"
                " first_available_utc, last_unavailable_utc, timing) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (w.wave_id, m.part, sku.color, sku.capacity, to_utc_text(m.first_available_at),
                 _opt(m.last_unavailable_at), m.timing))
        closed_ids = {id(w) for w in upd.closed}
        for w in list(upd.touched) + list(upd.closed):
            c.execute(
                "UPDATE store_restock_waves SET last_transition_utc = ?, sku_count = ?, supporting_polls = ?,"
                " present_at_startup = ?, gap_affected = ?, coverage_gap = ?, confidence = ?,"
                " event_class = ?, closed = ? WHERE wave_id = ?",
                (to_utc_text(w.last_at), len(w.members), w.supporting_polls, int(w.present_at_startup),
                 int(w.gap_affected), int(w.coverage_gap), w.confidence, w.event_class,
                 int(id(w) in closed_ids), w.wave_id))

    # -- WireGuard sessions (wg_manager.MetadataStore) ------------------------

    def start_vpn_session(self, *, config_name: str, config_path: str, wg_name: str, endpoint: str,
                          copy_path: str, started: datetime, baseline_json: str, reason: str | None,
                          service_id: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO vpn_sessions (config_name, config_path, wg_name, endpoint, copy_path, started_utc,"
            " state, start_reason, baseline_json, service_id) VALUES (?, ?, ?, ?, ?, ?, 'CONNECTING', ?, ?, ?)",
            (config_name, config_path, wg_name, endpoint, copy_path, to_utc_text(started), reason, baseline_json,
             service_id))
        return int(cur.lastrowid)

    def update_vpn_session(self, session_id: int, **fields: Any) -> None:
        unknown = set(fields) - VPN_SESSION_COLUMNS
        if unknown:
            raise ValueError(f"unknown vpn_sessions columns: {sorted(unknown)}")
        if not fields:
            return
        cols, vals = [], []
        for k, v in fields.items():
            cols.append(f"{k} = ?")
            vals.append(to_utc_text(v) if isinstance(v, datetime) else int(v) if isinstance(v, bool) else v)
        self.conn.execute(f"UPDATE vpn_sessions SET {', '.join(cols)} WHERE session_id = ?", (*vals, session_id))

    def record_vpn_event(self, session_id: int | None, at: datetime, event: str, state: str,
                         fields: Mapping[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO vpn_events (session_id, at_utc, event, state, fields_json) VALUES (?, ?, ?, ?, ?)",
            (session_id, to_utc_text(at), event, state, json.dumps(dict(fields), default=str, sort_keys=True)))

    def open_vpn_sessions(self) -> list[dict[str, Any]]:
        """Sessions never closed, oldest first: candidates for orphan recovery."""
        rows = self.query("SELECT * FROM vpn_sessions WHERE ended_utc IS NULL ORDER BY session_id")
        out = []
        for r in rows:
            d = dict(r)
            d["started"] = from_utc_text(d["started_utc"])
            out.append(d)
        return out

    def last_public_ip(self) -> str | None:
        row = self.query("SELECT public_ip FROM vpn_sessions WHERE public_ip IS NOT NULL"
                         " ORDER BY session_id DESC LIMIT 1")
        return row[0][0] if row else None

    def last_blocked_poll_ip(self) -> str | None:
        """Public IP of the tunnel behind the latest poll Apple refused with 541, if any."""
        row = self.query(
            "SELECT s.public_ip FROM polls p JOIN vpn_sessions s ON s.session_id = p.vpn_session_id"
            " WHERE (',' || p.error_summary || ',') LIKE '%,blocked,%' ORDER BY p.poll_id DESC LIMIT 1")
        return row[0][0] if row else None

    # -- discovered store sets (stores_config) -------------------------------

    def save_store_set(self, fingerprint: str, description: str, found: Sequence[Any],
                       region_zips: Mapping[str, Sequence[str]], saved: datetime) -> None:
        """Replace the cached store list for this configuration (found: stores_config.FoundStore)."""
        c = self.conn
        c.execute("BEGIN IMMEDIATE")
        try:
            c.execute("DELETE FROM store_sets WHERE fingerprint = ?", (fingerprint,))
            c.execute("INSERT INTO store_sets (fingerprint, description, saved_utc, region_zips_json) VALUES (?, ?, ?, ?)",
                      (fingerprint, description, to_utc_text(saved),
                       json.dumps({k: list(v) for k, v in region_zips.items()})))
            c.executemany(
                "INSERT INTO store_set_members (fingerprint, store_id, name, city, state, zip_code, region, poll_zip,"
                " distance_mi, position) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(fingerprint, f.store.store_id, f.store.name, f.store.city, f.state, f.store.zip_code,
                  f.store.region, f.store.poll_zip, f.distance_mi, i) for i, f in enumerate(found)])
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise

    def load_store_set(self, fingerprint: str) -> tuple[list[dict[str, Any]], dict[str, list[str]], datetime] | None:
        """(member rows nearest first, region ZIP lists, saved time) for this configuration, or None."""
        head = self.query("SELECT saved_utc, region_zips_json FROM store_sets WHERE fingerprint = ?", (fingerprint,))
        if not head:
            return None
        rows = [dict(r) for r in self.query(
            "SELECT * FROM store_set_members WHERE fingerprint = ? ORDER BY position", (fingerprint,))]
        if not rows:
            return None
        return rows, json.loads(head[0]["region_zips_json"]), from_utc_text(head[0]["saved_utc"])

    def latest_vpn_session(self) -> dict[str, Any] | None:
        row = self.query("SELECT * FROM vpn_sessions ORDER BY session_id DESC LIMIT 1")
        return dict(row[0]) if row else None

    # -- reading ------------------------------------------------------------

    def query(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def recent_polls(self, since: datetime) -> list[tuple[datetime, int, str | None]]:
        """(started, requests, error_summary) for polls since `since`, oldest first."""
        return [(from_utc_text(r["started_utc"]), int(r["requests"]), r["error_summary"])
                for r in self.query("SELECT started_utc, requests, error_summary FROM polls"
                                    " WHERE started_utc >= ? ORDER BY started_utc", (to_utc_text(since),))]

    def load_prior_state(self) -> dict[Key, PriorState]:
        """Last confirmed status per key, from each store's latest usable poll."""
        prior: dict[Key, PriorState] = {}
        latest = self.query(
            "SELECT r.store_id, MAX(r.poll_id) AS poll_id FROM poll_store_results r"
            " WHERE r.outcome IN ('ok', 'partial') GROUP BY r.store_id")
        for row in latest:
            sid, poll_id = row["store_id"], row["poll_id"]
            at = from_utc_text(self.query("SELECT started_utc FROM polls WHERE poll_id = ?", (poll_id,))[0][0])
            explicit = {r["part"]: Status(r["status"]) for r in self.query(
                "SELECT part, status FROM sku_observations WHERE poll_id = ? AND store_id = ?", (poll_id, sid))}
            for part in ALL_PARTS:
                status = explicit.get(part, Status.UNAVAILABLE)
                if not status.is_confirmed:
                    continue
                last_unavail = None
                if status is Status.AVAILABLE:
                    ev = self.query(
                        "SELECT last_unavailable_utc FROM availability_events WHERE store_id = ? AND part = ?"
                        " AND event_type = 'became_available' ORDER BY observed_utc DESC LIMIT 1", (sid, part))
                    if ev and ev[0][0]:
                        last_unavail = from_utc_text(ev[0][0])
                prior[(sid, part)] = PriorState(status, at, last_unavail)
        return prior


def _opt(dt: datetime | None) -> str | None:
    return to_utc_text(dt) if dt is not None else None
