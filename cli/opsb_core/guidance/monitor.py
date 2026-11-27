"""The daemon tick of the stuck guidance (P30-P32), the hot-state fields and the reply attachment.

Every ``guidance.tick_sec`` the daemon calls ``tick``: for each live implementer seat it classifies the
stuck state (``stuck.classify``), records it in ``seats.json`` (the hot state reads it), refreshes the
seat's dossier when its inputs changed, issues a card on a new class, delivers an undelivered card
through the inbox when the worker is idle (or after ``inbox_after_sec`` otherwise), and follows each
live card's outcome: adoption by a ledger row that cites it, and the first new local best after it.
Each seat runs under its own guard, so one bad seat never stops the others or the daemon tick.
"""
from __future__ import annotations

import datetime
import sys
import time
from pathlib import Path
from typing import Any

from . import cards, dossier, gap as _gap, scout as _scout, settings, stuck

NON_IMPLEMENTER = frozenset({"bench-author", "recon", "knowledge", "scout"})
KB_TTL_SEC = 300.0
_LAST: dict[str, float] = {}
_FINGERPRINTS: dict[tuple[str, str], tuple[Any, ...]] = {}
_KB: dict[str, Any] = {"at": 0.0, "entries": []}


def _warn(what: str, exc: BaseException) -> None:
    print(f"guidance: {what} failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)


def _fp(path: Path) -> tuple[int, int]:
    try:
        st = path.stat()
        return st.st_mtime_ns, st.st_size
    except OSError:
        return 0, 0


class Context:
    """Per-tick caches: ledger rows, gap triage and KB entries are read once per problem."""

    def __init__(self, ws: Path, s: dict[str, Any], now: datetime.datetime) -> None:
        self.ws, self.s, self.now = ws, s, now
        self._rows: dict[str, list[dict[str, Any]]] = {}
        self._gap: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        self._errors: dict[str, dict[str, str]] | None = None
        self.scouts: set[str] = set()  # agent ids of doc-scout seats (any status)
        self.live: set[str] = set()  # agent ids of the live seats this tick

    def rows(self, problem: str) -> list[dict[str, Any]]:
        if problem not in self._rows:
            from ..ledger import store
            self._rows[problem] = store.read_problem(self.ws, problem)
        return self._rows[problem]

    def gap(self, problem: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """``(triage, meta)`` from the newest receipt with per-case times (no network, no writes)."""
        if problem not in self._gap:
            from ..services import state_card_ext as ext
            receipts = ext.receipts_rows(self.ws, problem)
            got = ext.receipt_cases(receipts)
            names = ext.labels(self.ws, problem) if got else {}
            case_rows = [{"case": names.get(k, f"#{i:02d}"), "judge": k, "ours_us": (v or {}).get("ours"),
                          "tbest_us": (v or {}).get("tbest")}
                         for i, (k, v) in enumerate((got.get("cases") or {}).items(), start=1)]
            weight = ext.weight_of(self.ws, problem)
            meta = {"sha": got.get("sha"), "observed_at": got.get("at"),
                    "standing": ext.standing(self.ws, problem, weight)}
            self._gap[problem] = (_gap.triage(case_rows, weight, self.s), meta)
        return self._gap[problem]

    def errors(self, agent_id: str) -> dict[str, str]:
        if self._errors is None:
            from ..services import test_runs
            self._errors = {}
            for rec in test_runs._all_records(self.ws):
                if rec.get("status") == "failed" and rec.get("agent_id"):
                    sig = stuck.compile_signature(rec.get("error"))
                    if sig:
                        self._errors.setdefault(str(rec["agent_id"]), {})[str(rec["request_id"])] = sig
        return self._errors.get(agent_id, {})


def kb_entries() -> list[dict[str, Any]]:
    if time.monotonic() - float(_KB["at"]) > KB_TTL_SEC or not _KB["entries"]:
        try:
            from ..knowledge.query import list_kb_entries
            _KB["entries"] = list_kb_entries(auto_seed=False).get("entries") or []
        except Exception as exc:  # a missing or broken KB only leaves the KB files empty
            _warn("kb read", exc)
            _KB["entries"] = []
        _KB["at"] = time.monotonic()
    return list(_KB["entries"])


def valid_worktree(ws: Path, problem: str, worktree: str) -> Path | None:
    """The seat's worktree when it is a git tree inside the problem repository (never the contest root)."""
    if not worktree:
        return None
    try:
        from ..drivers.brief import resolve_problem_repo
        path = Path(worktree).expanduser().resolve()
        repo = resolve_problem_repo(ws, problem)[1]
    except Exception:
        return None
    if path == ws.resolve() or not (path / ".git").exists() or not path.is_dir():
        return None
    return path if path == repo or repo in path.parents else None


def lead_of(rows: list[dict[str, Any]], agent_id: str) -> dict[str, Any] | None:
    """The seat's most urgent open hypothesis, else its newest unexploited insight, else a teammate's insight.

    Only the ref reaches the card; the worker reads the row in findings.md.
    """
    from ..ledger import view
    insights = sorted(view.unexploited_insights(rows), key=view.order_key, reverse=True)
    hyps = [r for r in view.open_hypotheses(rows) if r.get("worker") == agent_id and r.get("source") == "worker"]
    own = [r for r in insights if r.get("worker") == agent_id and r.get("source") == "worker"]
    mates = [r for r in insights if r.get("worker") != agent_id and r.get("source") == "worker"]
    row = (hyps or own or mates or [None])[0]
    return {"ref": view.ref(row), "claim": row.get("claim")} if row else None


def refresh_dossier(ctx: Context, problem: str, agent_id: str, worktree: Path, triage: dict[str, Any],
                    meta: dict[str, Any], cls: str) -> list[str]:
    from ..ledger import store as lstore
    led = lstore.problem_dir(ctx.ws, problem)
    inputs = (cls, triage.get("focus"), meta.get("sha"), meta.get("observed_at"), tuple(sorted(ctx.scouts)),
              tuple(sorted((p.name, _fp(p)) for p in led.glob("*.jsonl"))) if led.is_dir() else (),
              _fp(cards.root(ctx.ws) / "cards.jsonl"), round(float(_KB["at"]), 0),
              (_scout.mirror_status() or {}).get("files"))
    key = (str(worktree), problem)
    if _FINGERPRINTS.get(key) == inputs and (dossier.path_of(worktree) / "index.md").is_file():
        return []
    rows = ctx.rows(problem)
    entries = kb_entries()
    words = dossier.keywords(problem, rows)
    tier = str(triage.get("focus") or "")
    terms = ctx.s.get("generic_terms") if isinstance(ctx.s.get("generic_terms"), dict) else None
    built = ctx.now.isoformat(timespec="seconds")
    files = {
        "index.md": dossier.render_index(problem, _gap.summary_line(triage), built, _scout.mirror_status()),
        "gap.md": dossier.render_gap(problem, triage, meta),
        "findings.md": dossier.render_findings(problem, rows, ctx.scouts),
        "dead-ends.md": dossier.render_dead_ends(problem, rows),
        "hardware.md": dossier.render_hardware(problem, dossier.rank_hardware(problem, entries, words, tier), terms),
        "prior-art.md": dossier.render_prior_art(problem, dossier.rank_prior(problem, entries, words, tier), tier,
                                                 terms),
        "cards.md": dossier.render_cards(problem, cards.history(ctx.ws, problem)),
    }
    changed = dossier.write(worktree, files)
    _FINGERPRINTS[key] = inputs
    return changed


def kb_refs(ctx: Context, problem: str, tier: str) -> list[str]:
    """Dossier anchors of citable KB entries for a card (``prior-art.md#2 (Problem, online_receipt)``)."""
    entries = kb_entries()
    words = dossier.keywords(problem, ctx.rows(problem))
    own, rest = dossier.rank_prior(problem, entries, words, tier)
    out = [f"prior-art.md#{i} ({e.get('problem') or e.get('pillar')}, online_receipt)"
           for i, e in enumerate([*own, *rest], start=1) if dossier.citable(e)]
    firm, _ = dossier.rank_hardware(problem, entries, words, tier)
    out += [f"hardware.md#{i} (online_receipt)" for i, e in enumerate(firm, start=1) if dossier.citable(e)]
    return out[:2]


def follow(ctx: Context, agent_id: str, card: dict[str, Any], own: list[dict[str, Any]],
           exps: list[dict[str, Any]]) -> None:
    """Record a live card's outcome: adoption or decline, the next local best, expiry."""
    cid, status = str(card.get("card_id")), str(card.get("status"))
    if status not in cards.LIVE:
        return
    if status in ("delivered",):
        found = cards.adoption(card, own)
        if found:
            cards.mark(ctx.ws, agent_id, cid, found[0], adopted_by=found[1])
            status = found[0]
    since = str(card.get("delivered_at") or "")
    if since and not card.get("next_best"):
        events = [e for e in stuck.progress(exps, ctx.s)["events"] if str(e.get("ts") or "") > since]
        if events:
            first = events[0]
            minutes = (stuck.parse_ts(first["ts"]) - stuck.parse_ts(since)).total_seconds() / 60.0
            best = {"speedup": round(float(first["speedup"]), 4), "minutes": round(minutes, 1), "ref": first["id"]}
            cards.set_next_best(ctx.ws, agent_id, cid, best)
    issued = stuck.parse_ts(card.get("issued_at"))
    window = float(ctx.s.get("outcome_window_minutes") or 120) * 60.0
    if issued and (ctx.now - issued).total_seconds() > window and status in ("issued", "delivered"):
        cards.mark(ctx.ws, agent_id, cid, "not_adopted", outcome="no ledger row cited the card in the window")


def deliver(manager: Any, ctx: Context, rec: dict[str, Any], card: dict[str, Any]) -> bool:
    """Inbox delivery of an undelivered card: at once when idle, after ``inbox_after_sec`` otherwise."""
    if card.get("status") != "issued":
        return False
    issued = stuck.parse_ts(card.get("issued_at"))
    age = (ctx.now - issued).total_seconds() if issued else 0.0
    idle = rec.get("status") == "idle"
    if not idle and age < float(ctx.s.get("inbox_after_sec") or 600):
        return False
    agent_id = rec["agent_id"]
    text = f"{card['text']}\nDossier: {cards.DOSSIER}/index.md (read it with read)."
    store = manager._store(ctx.ws)
    store.append_message(agent_id, "orch_to_worker", "guide", text, problem=str(rec.get("problem") or ""),
                         event_key=f"guide:{card['card_id']}")
    woke = manager._enqueue_to_worker(ctx.ws, rec, text, spawn_runner=False)
    cards.mark(ctx.ws, agent_id, card["card_id"], "delivered", via="inbox" if woke.get("ok") else "bus",
               delivered_at=ctx.now.isoformat(timespec="seconds"))
    return True


def seat(manager: Any, ctx: Context, rec: dict[str, Any]) -> dict[str, Any]:
    agent_id, problem = str(rec["agent_id"]), str(rec.get("problem") or "")
    from ..ledger import view
    rows = ctx.rows(problem)
    own = [r for r in rows if r.get("worker") == agent_id]
    cur = view.current(own)
    exps = [r for r in cur if r.get("type") == "experiment"]
    gates = [r for r in cur if r.get("type") == "hypothesis" and str(r.get("ekey") or "").startswith("gate:")]
    triage, meta = ctx.gap(problem)
    start = stuck.parse_ts(rec.get("created_at"))
    found = stuck.classify(exps, now=ctx.now, start=start, gates=gates, errors=ctx.errors(agent_id),
                           gap=triage, settings=ctx.s)
    cls = found["class"] if found else ""
    state = cards.update_seat(ctx.ws, agent_id, {
        "problem": problem, "class": cls, "signal": (found or {}).get("signal", ""),
        "since": (found or {}).get("since"), "checked_at": ctx.now.isoformat(timespec="seconds")})
    worktree = valid_worktree(ctx.ws, problem, str(rec.get("worktree") or ""))
    if worktree is not None:
        try:
            refresh_dossier(ctx, problem, agent_id, worktree, triage, meta, cls)
        except Exception as exc:
            _warn(f"dossier {problem}/{agent_id}", exc)
    card = state.get("card") if isinstance(state.get("card"), dict) else None
    scout_live = bool(card and card.get("class") == "scout" and card.get("status") in cards.LIVE)
    if found and ctx.s.get("cards", True) and not scout_live and (
            not card or card.get("class") != cls or card.get("status") not in cards.LIVE):
        tier = "algorithm" if cls == "scale_mismatch" else str(triage.get("focus") or "")
        text, meta_card = cards.build(cls, problem, found, triage, lead=lead_of(rows, agent_id),
                                      kb=kb_refs(ctx, problem, tier), limit=int(ctx.s.get("card_max_bytes") or 800))
        made = cards.issue(ctx.ws, agent_id, problem, cls, text, meta_card,
                           repeat_sec=float(ctx.s.get("repeat_minutes") or 30) * 60.0)
        card = made or card
        if made and worktree is not None:
            _FINGERPRINTS.pop((str(worktree), problem), None)  # cards.md shows the new card next tick
    if card and ctx.s.get("cards", True):
        deliver(manager, ctx, rec, card)
        fresh = (cards.read_seats(ctx.ws).get(agent_id) or {}).get("card") or card
        follow(ctx, agent_id, fresh, own, exps)
    scouting(manager, ctx, rec, found, triage, exps, rows, worktree)
    return {"agent_id": agent_id, "class": cls}


def scouting(manager: Any, ctx: Context, rec: dict[str, Any], found: dict[str, Any] | None,
             triage: dict[str, Any], exps: list[dict[str, Any]], rows: list[dict[str, Any]],
             worktree: Path | None) -> None:
    """4.4 P34: the doc-scout step of one seat (``guidance.scout.step``)."""
    lead = lead_of(rows, str(rec["agent_id"]))
    made = _scout.step(manager, ctx.ws, ctx.s, ctx.now, rec, found, triage, exps, rows, ctx.scouts,
                       lead_ref=(lead or {}).get("ref", ""), live=ctx.live)
    if made and worktree is not None:
        _FINGERPRINTS.pop((str(worktree), str(rec.get("problem") or "")), None)


def tick(manager: Any, ws: Path | str, *, force: bool = False, now: datetime.datetime | None = None) -> dict[str, Any]:
    """One guidance pass over the live seats (throttled to ``guidance.tick_sec``; never raises)."""
    try:
        s = settings()
        if not s.get("enabled", True):
            return {"skipped": "guidance.enabled is off"}
        root = Path(ws).expanduser().resolve()
        if not force and time.monotonic() - _LAST.get(str(root), 0.0) < float(s.get("tick_sec") or 30):
            return {"skipped": "throttled"}
        _LAST[str(root)] = time.monotonic()
        ctx = Context(root, s, now or datetime.datetime.now(datetime.timezone.utc))
        every = manager._store(root).list_agents(include_stopped=False, limit=-1)
        recs = [r for r in every if r.get("problem") and r.get("role_label") not in NON_IMPLEMENTER]
        ctx.scouts = {str(r["agent_id"]) for r in manager._store(root).list_agents(limit=-1)
                      if r.get("role_label") == "scout"}
        scout_recs = [r for r in every if r.get("problem") and r.get("role_label") == "scout"]
        ctx.live = {str(r["agent_id"]) for r in every}
    except Exception as exc:
        _warn("tick", exc)
        return {"error": str(exc)[:200]}
    out = []
    for rec in recs:
        try:
            out.append(seat(manager, ctx, rec))
        except Exception as exc:
            _warn(f"seat {rec.get('agent_id')}", exc)
    for rec in scout_recs:  # a doc scout reads the same dossier as the seat it answers
        try:
            problem = str(rec.get("problem") or "")
            tree = valid_worktree(ctx.ws, problem, str(rec.get("worktree") or ""))
            if tree is not None:
                triage, meta = ctx.gap(problem)
                refresh_dossier(ctx, problem, str(rec["agent_id"]), tree, triage, meta, "")
        except Exception as exc:
            _warn(f"scout dossier {rec.get('agent_id')}", exc)
    return {"seats": out}


def hot_fields(ws: Path | str) -> dict[str, dict[str, Any]]:
    """``{agent_id: {"stuck": {...} | None, "guide": {...} | None}}`` for the agent-mesh hot state."""
    try:
        doc = cards.read_seats(ws)
    except Exception:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for agent_id, seat_doc in doc.items():
        if not isinstance(seat_doc, dict):
            continue
        card = seat_doc.get("card") if isinstance(seat_doc.get("card"), dict) else None
        out[agent_id] = {
            "stuck": {"class": seat_doc["class"], "signal": seat_doc.get("signal", ""),
                      "since": seat_doc.get("since")} if seat_doc.get("class") else None,
            "guide": {"card_id": card.get("card_id"), "class": card.get("class"),
                      "status": card.get("status")} if card else None,
        }
    return out


def attach(reply: Any, ws: Path | str, agent_id: str | None) -> Any:
    """Add the worker's undelivered card to a ``parhelion_test_wait`` reply (a dict); never raises."""
    if not agent_id or not isinstance(reply, dict):
        return reply
    try:
        if not settings().get("cards", True):
            return reply
        card = cards.take_for_reply(ws, agent_id)
    except Exception as exc:
        _warn("attach", exc)
        return reply
    return {**reply, "guide": card} if card else reply


def seed_line(ws: Path | str, agent_id: str) -> str:
    try:
        return cards.seed_line(ws, agent_id)
    except Exception:
        return ""
