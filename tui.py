"""Interactive full-screen view for the monitor (`--tui`): one collapsible line per poll.

`TuiModel` holds everything shown and is plain Python, so it is unit-tested
without a terminal. `TuiApp` draws the model with curses from the main
thread and turns keys into model actions; the monitor loop runs in a worker
thread and only ever calls the model's thread-safe `add_*` methods.
"""

from __future__ import annotations

import curses
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

MAX_ENTRIES = 1000
DETAIL_INDENT = "    "

# Styles a renderer maps to colors/attributes.
OK, NEW, ERROR, BLOCKED, EVENT, DETAIL, STATUS = "ok", "new", "error", "blocked", "event", "detail", "status"
GONE = "gone"  # a poll that found stock, all of which has since sold out


@dataclass
class PollEntry:
    poll_no: int
    at: datetime
    summary: str
    detail: list[str]
    style: str = OK  # OK, NEW (new stock or a wave), ERROR, BLOCKED (HTTP 541/403), GONE
    found: frozenset = frozenset()  # (store, part) keys this poll found newly available
    expanded: bool = False
    gone_at: datetime | None = None  # when the last of `found` stopped being available
    user_set: bool = False  # expanded/collapsed by hand: automatic open/close leaves it alone

    @property
    def notable(self) -> bool:
        return self.style == NEW

    @property
    def in_stock(self) -> bool:
        return bool(self.found) and self.gone_at is None


@dataclass
class EventEntry:
    at: datetime
    text: str
    style: str = EVENT


Entry = PollEntry | EventEntry


@dataclass
class Row:
    text: str
    style: str
    selected: bool = False


class TuiModel:
    """Entries newest first, a cursor over entries, and a scroll offset over rendered lines."""

    def __init__(self, max_entries: int = MAX_ENTRIES, clock: Callable[[], datetime] | None = None) -> None:
        self.max_entries = max_entries
        self.entries: list[Entry] = []
        self.cursor = 0
        self.offset = 0  # first rendered line shown
        self.version = 0  # bumped on every change, so the UI knows when to redraw
        self.polls = 0
        self.available = 0
        self.tunnel: str | None = None
        self.last_blocked: datetime | None = None
        self.last_poll: datetime | None = None
        self.stopping = False
        self.in_stock = 0
        self._lock = threading.RLock()
        self._clock = clock

    # -- adding (called from the monitor thread) -------------------------

    def add_poll(self, entry: PollEntry, available: int = 0, tunnel: str | None = None,
                 available_keys: frozenset | None = None) -> None:
        """`available_keys`: every (store, part) confirmed available now. A poll that found
        stock stays open while any of it is still available, then folds and dims itself."""
        with self._lock:
            entry.expanded = entry.notable
            if available_keys is not None:
                for e in self.entries:
                    if isinstance(e, PollEntry) and e.in_stock and not (e.found & available_keys):
                        e.gone_at = entry.at
                        e.style = GONE
                        if not e.user_set:
                            e.expanded = False
                self.in_stock = len(available_keys)
            self.polls += 1
            self.available = available
            self.tunnel = tunnel
            self.last_poll = entry.at
            if entry.style == BLOCKED:
                self.last_blocked = entry.at
            self._insert(entry)

    def add_event(self, text: str, at: datetime, style: str = EVENT) -> None:
        text = text.strip()
        if not text:
            return
        with self._lock:
            self._insert(EventEntry(at, text, style))

    def _insert(self, entry: Entry) -> None:
        following = self.cursor == 0
        self.entries.insert(0, entry)
        if not following:  # keep the selected entry, and the view, where they were
            self.cursor += 1
            self.offset += len(self._lines_of(entry))
        del self.entries[self.max_entries:]
        self.cursor = min(self.cursor, len(self.entries) - 1)
        self.version += 1

    # -- navigation (called from the UI thread) ----------------------------

    def move(self, n: int) -> None:
        with self._lock:
            if self.entries:
                self.cursor = max(0, min(len(self.entries) - 1, self.cursor + n))
            self.version += 1

    def home(self) -> None:
        with self._lock:
            self.cursor, self.offset = 0, 0
            self.version += 1

    def end(self) -> None:
        with self._lock:
            self.cursor = max(0, len(self.entries) - 1)
            self.version += 1

    def toggle(self) -> None:
        with self._lock:
            e = self.selected
            if isinstance(e, PollEntry):
                e.expanded = not e.expanded
                e.user_set = True
                self.version += 1

    def expand_all(self, expanded: bool = True) -> None:
        with self._lock:
            for e in self.entries:
                if isinstance(e, PollEntry):
                    e.expanded = expanded
            self.version += 1

    def collapse_all(self) -> None:
        self.expand_all(False)

    @property
    def selected(self) -> Entry | None:
        return self.entries[self.cursor] if self.entries else None

    # -- rendering ---------------------------------------------------------

    @staticmethod
    def _lines_of(entry: Entry) -> list[tuple[str, str]]:
        if isinstance(entry, EventEntry):
            return [(f"  • {entry.at.strftime('%H:%M:%S')}  {entry.text}", entry.style)]
        marker = "▾" if entry.expanded else "▸"
        star = "★ " if entry.found else ""
        gone = f"  (sold out by {entry.gone_at.strftime('%H:%M:%S')})" if entry.gone_at else ""
        lines = [(f"{marker} {star}{entry.summary}{gone}", entry.style)]
        if entry.expanded:
            lines += [(DETAIL_INDENT + d, DETAIL) for d in entry.detail]
        return lines

    def render(self, width: int, height: int) -> list[Row]:
        """The `height` body rows to show, each at most `width` characters."""
        with self._lock:
            if width <= 0 or height <= 0:
                return []
            lines: list[Row] = []
            cursor_line = 0
            for i, e in enumerate(self.entries):
                if i == self.cursor:
                    cursor_line = len(lines)
                for j, (text, style) in enumerate(self._lines_of(e)):
                    lines.append(Row(text, style, selected=(i == self.cursor and j == 0)))
            if cursor_line < self.offset:
                self.offset = cursor_line
            elif cursor_line >= self.offset + height:
                self.offset = cursor_line - height + 1
            self.offset = max(0, min(self.offset, max(0, len(lines) - height)))
            view = lines[self.offset:self.offset + height]
            return [Row(_fit(r.text, width), r.style, r.selected) for r in view]

    def status(self, width: int, now: datetime, interval_s: float | None = None) -> str:
        with self._lock:
            parts = [f"polls {self.polls}", f"in stock now {self.in_stock}", f"available {self.available}/144",
                     f"tunnel {self.tunnel or '-'}"]
            if self.last_blocked:
                parts.append(f"last 541 {self.last_blocked.strftime('%H:%M:%S')}")
            if self.last_poll and interval_s:
                due = max(0, interval_s - (now - self.last_poll).total_seconds())
                parts.append(f"next ~{due:.0f}s")
            if self.stopping:
                parts.append("STOPPING: finishing the current poll…")
            keys = "↑↓/jk move  Enter toggle  e/c expand/collapse all  g/G top/bottom  q quit"
            return _fit(" | ".join(parts) + "   " + keys, width)


def _fit(text: str, width: int) -> str:
    return text if len(text) <= width else text[:max(0, width - 1)] + "…"


class EventWriter:
    """A file-like object whose printed lines become model events (for print(..., file=...))."""

    def __init__(self, model: TuiModel, clock: Callable[[], datetime], style: str = EVENT) -> None:
        self.model, self.clock, self.style = model, clock, style
        self._buf = ""

    def write(self, text: str) -> int:
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self.model.add_event(line, self.clock(), self.style)
        return len(text)

    def flush(self) -> None:
        if self._buf:
            self.model.add_event(self._buf, self.clock(), self.style)
            self._buf = ""

    def isatty(self) -> bool:
        return False


# --------------------------------------------------------------------------
# curses layer
# --------------------------------------------------------------------------


class TuiApp:
    """Draws `model` until `worker` finishes. `q` (or Ctrl+C) sets `stop`; the
    worker then finishes its current poll and exits, and so does the UI."""

    POLL_MS = 200

    def __init__(self, model: TuiModel, stop: threading.Event, worker: threading.Thread,
                 clock: Callable[[], datetime], interval_s: float | None = None) -> None:
        self.model, self.stop, self.worker, self.clock, self.interval_s = model, stop, worker, clock, interval_s

    def run(self) -> None:
        curses.wrapper(self._main)

    def _request_stop(self) -> None:
        self.stop.set()
        self.model.stopping = True
        self.model.version += 1

    def _main(self, scr: Any) -> None:
        curses.curs_set(0)
        scr.keypad(True)
        scr.timeout(self.POLL_MS)
        styles = self._styles()
        drawn, last_tick, interrupts = -1, 0.0, 0
        while self.worker.is_alive():
            if self.model.version != drawn or time.monotonic() - last_tick >= 1.0:
                drawn, last_tick = self.model.version, time.monotonic()
                self._draw(scr, styles)
            try:
                ch = scr.getch()
            except KeyboardInterrupt:
                interrupts += 1
                if interrupts > 1:
                    break  # a second Ctrl+C leaves the screen now; teardown still runs after
                self._request_stop()
                continue
            self._key(ch)

    def _key(self, ch: int) -> None:
        m = self.model
        if ch in (ord("q"), ord("Q")):
            self._request_stop()
        elif ch in (curses.KEY_UP, ord("k")):
            m.move(-1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            m.move(1)
        elif ch == curses.KEY_PPAGE:
            m.move(-10)
        elif ch == curses.KEY_NPAGE:
            m.move(10)
        elif ch in (curses.KEY_HOME, ord("g")):
            m.home()
        elif ch in (curses.KEY_END, ord("G")):
            m.end()
        elif ch in (curses.KEY_ENTER, 10, 13, ord(" ")):
            m.toggle()
        elif ch == ord("e"):
            m.expand_all()
        elif ch == ord("c"):
            m.collapse_all()
        elif ch == curses.KEY_RESIZE:
            m.version += 1

    def _styles(self) -> dict[str, int]:
        base = {OK: curses.A_NORMAL, DETAIL: curses.A_NORMAL, EVENT: curses.A_DIM, STATUS: curses.A_REVERSE,
                NEW: curses.A_BOLD, ERROR: curses.A_BOLD, BLOCKED: curses.A_BOLD, GONE: curses.A_DIM}
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            for n, color in ((1, curses.COLOR_GREEN), (2, curses.COLOR_RED), (3, curses.COLOR_YELLOW),
                             (4, curses.COLOR_CYAN)):
                curses.init_pair(n, color, -1)
            base.update({NEW: curses.color_pair(1) | curses.A_BOLD, GONE: curses.color_pair(1) | curses.A_DIM,
                         ERROR: curses.color_pair(2),
                         BLOCKED: curses.color_pair(3) | curses.A_BOLD, EVENT: curses.color_pair(4)})
        return base

    def _draw(self, scr: Any, styles: dict[str, int]) -> None:
        h, w = scr.getmaxyx()
        scr.erase()
        width = max(1, w - 1)  # writing the last column of the last row raises in curses
        for y, row in enumerate(self.model.render(width, max(0, h - 1))):
            attr = styles.get(row.style, curses.A_NORMAL) | (curses.A_REVERSE if row.selected else 0)
            try:
                scr.addstr(y, 0, row.text, attr)
            except curses.error:
                pass
        try:
            scr.addstr(h - 1, 0, self.model.status(width, self.clock(), self.interval_s).ljust(width),
                       styles[STATUS])
        except curses.error:
            pass
        scr.refresh()
