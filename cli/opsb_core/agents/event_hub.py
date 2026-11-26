"""Change-driven waits (4.2 B): one revisioned event stream per workspace, replacing polling.

``rev = "<boot_id>:<n>"`` where ``n`` grows by one per event and ``boot_id`` is new for every hub
(daemon start). A ``since`` from another boot, or older than the retained window, gets
``resync=true`` and a snapshot summary instead of an error.

``wait`` blocks on the hub's condition until an event visible to the caller arrives:

- callers: a worker sees its own inbox, its own tests, its own seat status and receipts of its
  problem; the orchestrator sees worker-to-orchestrator messages and directives, every test,
  every seat and every receipt (optionally narrowed by ``agent_ids`` / ``problem``);
- bursts: delivery waits until ``COALESCE_SEC`` after the first pending event, then returns at
  most ``MAX_PER_SEC`` events per caller per second (``more=true`` when some remain);
- quiet: without a change the reply is ``{changed: false, rev, waiting_for, idle_streak,
  next_timeout}``; ``next_timeout`` grows with ``idle_streak`` up to ``MAX_BLOCK_SEC`` (a managed
  DSH worker aborts an MCP request at 60 s, the desktop plugin at 120 s), and a call without
  ``timeout`` uses it, or the full cap while a watched test run is in flight.

Sources are scanned by ``event_sources.SourceScanner`` on a background thread, immediately
when the owning manager reports a change (``kick``) and every ``SCAN_SEC`` while somebody waits.
"""
from __future__ import annotations

import collections
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .bus_store import AgentBusStore
from .event_sources import SourceScanner

SOURCES = ("inbox", "test", "agent", "judge")
#: Below the 60 s request timeout of a managed DSH worker (4.3.1: 100 s timed out every long wait).
MAX_BLOCK_SEC = 50.0
DEFAULT_BLOCK_SEC = 25.0
IDLE_GROWTH = 1.5
COALESCE_SEC = 0.25
MAX_PER_SEC = 32
RETAIN = 4000
SCAN_SEC = 0.5
IDLE_SCAN_SEC = 5.0
ACTIVE_WINDOW_SEC = 150.0
MIN_SCAN_GAP_SEC = 0.05


@dataclass(frozen=True)
class Event:
    n: int
    source: str
    mono: float
    agent_id: str
    problem: str
    ref: str
    kind: str
    line: str
    audience: str = ""
    strategic: bool = False


def inbox_predicate(agent_ids: set[str], problem: str | None, strategic_only: bool) -> Callable[[Event], bool]:
    """Matches the bus rows ``agent_wait`` collects (``list_messages(for_orchestrator=True, ...)``)."""
    prob = str(problem or "").lower()

    def pred(ev: Event) -> bool:
        return (ev.source == "inbox" and ev.audience == "orchestrator"
                and (not agent_ids or ev.agent_id in agent_ids)
                and (not prob or ev.problem.lower() == prob)
                and (not strategic_only or ev.strategic))

    return pred


def normalize_sources(until: Any) -> tuple[str, ...]:
    if until in (None, "", []):
        return SOURCES
    items = [until] if isinstance(until, str) else list(until)
    picked = tuple(s for s in SOURCES if s in {str(x).strip().lower() for x in items})
    if not picked:
        raise ValueError(f"until must name some of: {', '.join(SOURCES)}")
    return picked


class EventHub:
    def __init__(self, workspace: Path, store: AgentBusStore, *, coalesce_sec: float = COALESCE_SEC,
                 max_per_sec: int = MAX_PER_SEC, scan_sec: float = SCAN_SEC, background: bool = True) -> None:
        self.ws = Path(workspace).expanduser().resolve()
        self.store = store
        self.boot_id = secrets.token_hex(4)
        self.coalesce_sec = float(coalesce_sec)
        self.max_per_sec = int(max_per_sec)
        self.scan_sec = float(scan_sec)
        self._n = 0
        self._events: collections.deque[Event] = collections.deque(maxlen=RETAIN)
        self._cond = threading.Condition()
        self._scan_lock = threading.Lock()
        self._kick = threading.Event()
        self._stop = threading.Event()
        self._active_until = 0.0
        self._last_scan = 0.0
        self._idle: dict[tuple[str, str], int] = {}
        self._sent: dict[tuple[str, str], collections.deque[float]] = {}
        self.scanner = SourceScanner(self.ws, store)
        self._thread: threading.Thread | None = None
        if background:
            self._thread = threading.Thread(target=self._loop, name=f"event-hub-{self.ws.name}", daemon=True)
            self._thread.start()

    # ------------------------------------------------------------------ revisions
    @property
    def n(self) -> int:
        with self._cond:
            return self._n

    @property
    def rev(self) -> str:
        return f"{self.boot_id}:{self.n}"

    def parse_since(self, since: Any) -> int | None:
        """Event index after ``since``; None means resync (other boot, future, or expired window)."""
        if since in (None, ""):
            return self.n
        boot, _, num = str(since).rpartition(":")
        if boot != self.boot_id or not num.isdigit():
            return None
        n = int(num)
        with self._cond:
            oldest = self._events[0].n if self._events else self._n + 1
            if n > self._n or (n < oldest - 1 and len(self._events) == self._events.maxlen):
                return None
        return n

    # ------------------------------------------------------------------ publishing
    def publish(self, source: str, *, agent_id: str = "", problem: str = "", ref: str = "", kind: str = "",
                line: str = "", audience: str = "", strategic: bool = False) -> Event:
        with self._cond:
            self._n += 1
            ev = Event(self._n, source, time.monotonic(), agent_id, problem, ref, kind, line, audience, strategic)
            self._events.append(ev)
            self._cond.notify_all()
            return ev

    def kick(self) -> None:
        if time.monotonic() < self._active_until:
            self._kick.set()

    def scan_now(self) -> int:
        with self._scan_lock:  # publish under the scan lock so event order follows scan order
            self._last_scan = time.monotonic()
            found = self.scanner.scan()
            for ev in found:
                self.publish(**ev)
        return len(found)

    def _loop(self) -> None:
        while not self._stop.is_set():
            active = time.monotonic() < self._active_until
            self._kick.wait(self.scan_sec if active else IDLE_SCAN_SEC)
            self._kick.clear()
            if self._stop.is_set():
                break
            gap = MIN_SCAN_GAP_SEC - (time.monotonic() - self._last_scan)
            if gap > 0:
                time.sleep(gap)
            try:
                self.scan_now()
            except Exception:
                pass

    def stop(self) -> None:
        self._stop.set()
        self._kick.set()

    def _touch(self) -> None:
        self._active_until = time.monotonic() + ACTIVE_WINDOW_SEC

    # ------------------------------------------------------------------ visibility
    def _visible(self, ev: Event, role: str, agent_id: str, problem: str, sources: tuple[str, ...],
                 agent_ids: set[str], strategic_only: bool) -> bool:
        if ev.source not in sources:
            return False
        if role == "worker":
            if ev.source == "inbox":
                return ev.audience == "worker" and ev.agent_id == agent_id
            if ev.source == "judge":
                return bool(problem) and ev.problem.lower() == problem.lower()
            return ev.agent_id == agent_id
        if ev.source == "inbox" and (ev.audience != "orchestrator" or (strategic_only and not ev.strategic)):
            return False
        if agent_ids and ev.source != "judge" and ev.agent_id not in agent_ids:
            return False
        return not problem or ev.problem.lower() == problem.lower()

    def _pending(self, after: int, pred: Callable[[Event], bool]) -> list[Event]:
        out = []
        for ev in reversed(self._events):
            if ev.n <= after:
                break
            if pred(ev):
                out.append(ev)
        out.reverse()
        return out

    def wait_until(self, pred: Callable[[Event], bool], timeout: float, since_n: int | None = None) -> bool:
        """Block until an event after ``since_n`` satisfies ``pred`` (internal waits; no coalescing)."""
        self._touch()
        after = self.n if since_n is None else since_n
        self._kick.set()
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._cond:
            while not self._pending(after, pred):
                rem = deadline - time.monotonic()
                if rem <= 0:
                    return False
                self._cond.wait(rem)
            return True

    # ------------------------------------------------------------------ parhelion_wait
    def next_timeout(self, key: tuple[str, str]) -> float:
        return round(min(MAX_BLOCK_SEC, DEFAULT_BLOCK_SEC * IDLE_GROWTH ** self._idle.get(key, 0)), 1)

    def default_timeout(self, key: tuple[str, str], role: str, agent_id: str, problem: str,
                        sources: tuple[str, ...]) -> float:
        """Block for the full cap while a watched test is running: its end is certain to arrive.

        Otherwise start at ``DEFAULT_BLOCK_SEC`` and lengthen with the caller's idle streak.
        """
        if "test" in sources and self.scanner.active_tests(agent_id if role == "worker" else None, problem or None):
            return MAX_BLOCK_SEC
        return self.next_timeout(key)

    def wait(self, *, role: str = "orchestrator", agent_id: str | None = None, since: Any = None, until: Any = None,
             timeout: float | None = None, agent_ids: list[str] | None = None, problem: str | None = None,
             strategic_only: bool = False) -> dict[str, Any]:
        role = "worker" if role == "worker" else "orchestrator"
        me = str(agent_id or "")
        if role == "worker" and not me:
            raise ValueError("A worker wait needs its bound agent_id")
        sources = normalize_sources(until)
        key = (role, me or "orchestrator")
        if role == "worker":
            rec = self.store.get_agent(me) or {}
            scope_problem = str(rec.get("problem") or "")
        else:
            scope_problem = str(problem or "").strip()
        ids = {str(x) for x in (agent_ids or []) if str(x).strip()}
        self._touch()
        self.scan_now()  # catch up on anything that changed while nobody was waiting
        cursor = self.parse_since(since)
        if cursor is None:
            return {"changed": True, "resync": True, "rev": self.rev,
                    "snapshot": self.scanner.snapshot(role, me or None),
                    "note": "Revision unknown to this daemon boot; continue with the returned rev."}
        budget = (float(timeout) if timeout is not None
                  else self.default_timeout(key, role, me, scope_problem, sources))
        budget = max(0.0, min(MAX_BLOCK_SEC, budget))
        deadline = time.monotonic() + budget

        def pred(ev: Event) -> bool:
            return self._visible(ev, role, me, scope_problem, sources, ids, bool(strategic_only))

        with self._cond:
            while True:
                now = time.monotonic()
                pending = self._pending(cursor, pred)
                if pending:
                    hold = pending[0].mono + self.coalesce_sec - now
                    sent = self._sent.setdefault(key, collections.deque())
                    while sent and sent[0] <= now - 1.0:
                        sent.popleft()
                    room = self.max_per_sec - len(sent)
                    if hold > 0:
                        self._cond.wait(hold)
                        continue
                    if room <= 0:
                        wait_for = sent[0] + 1.0 - now
                        if now + wait_for > deadline + self.coalesce_sec:
                            return {"changed": False, "rev": f"{self.boot_id}:{cursor}", "throttled": True,
                                    "pending": len(pending), "next_timeout": self.next_timeout(key)}
                        self._cond.wait(wait_for)
                        continue
                    batch = pending[:room]
                    sent.extend([now] * len(batch))
                    self._idle[key] = 0
                    return {"changed": True, "rev": f"{self.boot_id}:{batch[-1].n}", "events": [e.line for e in batch],
                            "more": len(pending) > len(batch), "next_timeout": self.next_timeout(key)}
                rem = deadline - now
                if rem <= 0:
                    self._idle[key] = self._idle.get(key, 0) + 1
                    return {"changed": False, "rev": f"{self.boot_id}:{self._n}",
                            "waiting_for": self.scanner.waiting_for(role, me or None, scope_problem, sources),
                            "idle_streak": self._idle[key], "next_timeout": self.next_timeout(key)}
                self._cond.wait(rem)

    # ------------------------------------------------------------------ helpers for legacy waits
    def test_blocker(self, request_id: str) -> Callable[[float], None]:
        """``block(remaining)`` for ``test_runs.wait``: returns on the run's next state change."""
        cursor = [self.n]

        def block(remaining: float) -> None:
            pred = lambda ev: ev.source == "test" and ev.ref == request_id  # noqa: E731
            self.wait_until(pred, min(remaining, 5.0), since_n=cursor[0])
            cursor[0] = self.n

        return block
