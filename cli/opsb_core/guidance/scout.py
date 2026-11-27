"""P34 doc scout: when a seat stays stuck, the platform asks for a read-only scout of the official docs.

Flow (stuck-guidance §7):

1. ``due`` decides that a stuck seat needs a scout: the class is ``scale_mismatch``, or the seat's card was
   delivered ``scout.after_minutes`` ago without adoption and the seat is still stuck. A problem gets at
   most one request per ``scout.min_interval_minutes``, and one question (problem, class, focus cases,
   newest SHA) is never asked twice.
2. ``question`` builds the question from data: the stuck class and signal, the focus cases of the gap
   triage, and a class-specific ask in generic terms. Ledger text is cited by ref, never quoted.
3. ``request`` logs it in ``<state>/guidance/scouts.jsonl`` and sends the orchestrator a strategic
   ``scout_request`` with the available Harnesses and the exact call. The orchestrator picks the driver
   (``parhelion_sprint_start(problems=[...], role="scout", driver=...)``); the scout seat's brief carries
   the question (``pending_question``). With ``scout.auto_spawn`` the platform makes that call itself
   with the first available Harness.
4. ``answers`` finds the scout's findings (its ledger rows written after the request); the monitor turns
   them into a ``scout`` card for the stuck seat and they lead ``findings.md``.

The scout's findings are treated like any other teammate finding: there is no separate evidence level.
"""
from __future__ import annotations

import datetime
import hashlib
import time
from pathlib import Path
from typing import Any

from .. import config as _cfg
from . import cards

ASK = {
    "scale_mismatch": ("For these cases, which algorithm or execution unit does the official documentation "
                       "recommend at this size and shape, and what constraints come with it (alignment, buffer "
                       "sizes, supported data types)?"),
    "plateau": ("Small changes stopped paying. Which documented technique for this kind of computation moves the "
                "focus cases by a factor, not by percent, and under which conditions?"),
    "build_loop": ("The last builds fail with the same compiler error (its text is in the worker's test result). "
                   "What are the documented constraints and the correct use of the interface involved?"),
    "wa_loop": ("One case fails repeatedly with wrong output or a runtime error. What does the documentation "
                "specify about the numeric behaviour, rounding or boundary handling this case exercises?"),
    "no_candidate": ("The seat produced no passing candidate for an hour. Which documented pitfalls make a kernel "
                     "fail to build or validate in this setting?"),
}


def options(s: dict[str, Any]) -> dict[str, Any]:
    base = {"enabled": True, "after_minutes": 30, "min_interval_minutes": 60, "auto_spawn": False,
            "docs_dir": "", "driver": ""}
    raw = s.get("scout") if isinstance(s.get("scout"), dict) else {}
    return {**base, **raw}


def _log_path(ws: Path | str) -> Path:
    return cards.root(ws) / "scouts.jsonl"


def log(ws: Path | str) -> list[dict[str, Any]]:
    return _cfg.read_jsonl(_log_path(ws))


def key_of(problem: str, cls: str, triage: dict[str, Any] | None, sha: str) -> str:
    focus = (triage or {}).get("tiers", {}).get((triage or {}).get("focus") or "", {}).get("cases") or []
    raw = "|".join([problem, cls, ",".join(focus[:4]), str(sha or "")[:12]])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def due(ws: Path | str, problem: str, stuck: dict[str, Any] | None, card: dict[str, Any] | None,
        key: str, now: datetime.datetime, s: dict[str, Any]) -> str:
    """The reason a scout is due now ("" when it is not)."""
    opt = options(s)
    if not opt["enabled"] or not stuck:
        return ""
    reason = ""
    if stuck.get("class") == "scale_mismatch":
        reason = "scale_mismatch"
    elif card and card.get("status") == "delivered" and card.get("class") != "scout":
        delivered = _cfg.parse_iso(card.get("delivered_at"))
        if delivered and (now - delivered).total_seconds() >= float(opt["after_minutes"]) * 60:
            reason = f"still {stuck.get('class')} after {card.get('card_id')}"
    if not reason:
        return ""
    rows = [r for r in log(ws) if r.get("event") == "requested" and r.get("problem") == problem]
    if any(r.get("key") == key for r in rows):
        return ""
    last = max((_cfg.parse_iso(r.get("ts")) for r in rows), default=None, key=lambda t: t or now)
    if last and (now - last).total_seconds() < float(opt["min_interval_minutes"]) * 60:
        return ""
    return reason


def question(problem: str, stuck: dict[str, Any], triage: dict[str, Any] | None, *, reason: str,
             lead_ref: str = "") -> str:
    """The scout's question, built from data in generic terms (no ledger text)."""
    from .gap import fmt_ratio
    lines = [f"Problem {problem}: the seat is stuck ({stuck.get('class')}): {stuck.get('signal')}. Trigger: {reason}."]
    tiers = (triage or {}).get("tiers") or {}
    tier = "algorithm" if stuck.get("class") == "scale_mismatch" and "algorithm" in tiers else (triage or {}).get("focus")
    t = tiers.get(tier or "") or {}
    if t:
        cases = {c["case"]: c for c in (triage or {}).get("cases") or []}
        named = ", ".join(f"{c} ({fmt_ratio(cases[c]['ratio'])}x tbest, {cases[c]['ours_us']:g} us)"
                          for c in t["cases"][:4] if c in cases)
        lines.append(f"Focus: the {tier} tier, {named}; a {t['factor']:g}x there is worth about "
                     f"+{t['gain_typical_w']:.1f} weighted points (gap.md in your dossier).")
    lines.append("Ask: " + ASK.get(str(stuck.get("class")), ASK["plateau"]))
    lines.append("Already refuted on this problem: dead-ends.md in your dossier"
                 + (f"; the seat's current lead is {lead_ref} (findings.md)." if lead_ref else "."))
    return "\n".join(lines)


def request(manager: Any, ws: Path | str, problem: str, agent_id: str, reason: str, text: str, key: str,
            s: dict[str, Any], now: datetime.datetime | None = None) -> dict[str, Any]:
    """Log the request, tell the orchestrator which Harnesses it may choose and the exact call."""
    from ..drivers import availability
    opt = options(s)
    kinds = availability.available()
    pick = availability.choose(str(opt.get("driver") or ""))
    event = {"event": "requested", "problem": problem, "agent_id": agent_id, "key": key, "reason": reason,
             "question": text, "ts": (now or _cfg.utc_now()).isoformat(timespec="seconds")}
    with cards.locked(ws) as base:
        with (base / "scouts.jsonl").open("a", encoding="utf-8") as fh:
            import json
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    call = f'parhelion_sprint_start(problems=["{problem}"], role="scout", driver="<one of: {", ".join(kinds)}>")'
    body = (f"{problem}: doc scout requested for {agent_id} ({reason}). Available Harnesses: "
            f"{', '.join(kinds) or 'none'}. Choose one and call {call}. The platform stores the question and "
            "puts it in the scout's brief; the answer reaches the stuck seat as a guide card.")
    store = manager._store(Path(ws))
    store.append_message(agent_id, "worker_to_orch", "scout_request", body, problem=problem, priority="strategic",
                         event_key=f"scout:{key}")
    out: dict[str, Any] = {"requested": key, "driver": pick}
    if opt.get("auto_spawn") and pick:
        try:
            from ..agents.sprint import start_sprint
            res = start_sprint(manager, Path(ws), driver=pick, problems=[problem], role="scout")
            out["spawned"] = [a.get("agent_id") for a in res.get("assignments") or [] if a.get("agent_id")]
        except Exception as exc:  # an automatic spawn must never stop the guidance tick
            out["spawn_error"] = str(exc)[:200]
    return out


def pending_question(ws: Path | str, problem: str) -> str:
    """The newest question asked for ``problem`` (the scout seat's brief carries it)."""
    rows = [r for r in log(ws) if r.get("event") == "requested" and r.get("problem") == problem]
    return str(rows[-1].get("question") or "") if rows else ""


def open_request(ws: Path | str, problem: str, agent_id: str, live: set[str] | None = None) -> dict[str, Any] | None:
    """The newest unanswered request of this seat, or of a seat of the problem that is gone (``live``
    names the live seats): a replaced seat's question is answered to the seat that now works the problem."""
    rows = log(ws)
    done = {r.get("key") for r in rows if r.get("event") == "answered"}
    mine = [r for r in rows if r.get("event") == "requested" and r.get("problem") == problem
            and r.get("key") not in done
            and (r.get("agent_id") == agent_id or (live is not None and r.get("agent_id") not in live))]
    return mine[-1] if mine else None


def answers(rows: list[dict[str, Any]], scouts: set[str], since: str) -> list[dict[str, Any]]:
    """Scout insight and constraint rows written after ``since``, oldest first (the first is the lead)."""
    from ..ledger import view
    # Only what the scout wrote: the platform also writes into a scout's ledger (a boundary constraint, for one),
    # and the 4.4.4 run took that row for the answer and closed the request before the findings came.
    hit = [r for r in view.current(rows) if r.get("worker") in scouts and r.get("type") in ("insight", "constraint")
           and r.get("source") != "platform" and str(r.get("ts") or "") >= since]
    return sorted(hit, key=view.order_key)


def mark_answered(ws: Path | str, key: str, refs: list[str], card_id: str) -> None:
    import json
    with cards.locked(ws) as base:
        with (base / "scouts.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"event": "answered", "key": key, "refs": refs, "card_id": card_id,
                                 "ts": _cfg.utc_now().isoformat(timespec="seconds")}, ensure_ascii=False) + "\n")


def step(manager: Any, ws: Path, s: dict[str, Any], now: datetime.datetime, rec: dict[str, Any],
         found: dict[str, Any] | None, triage: dict[str, Any], exps: list[dict[str, Any]],
         rows: list[dict[str, Any]], scouts: set[str], *, lead_ref: str = "",
         live: set[str] | None = None) -> dict[str, Any] | None:
    """Raise a request for a seat that stays stuck; when a scout has answered, issue its card (returned)."""
    agent_id, problem = str(rec["agent_id"]), str(rec.get("problem") or "")
    card = (cards.read_seats(ws).get(agent_id) or {}).get("card")
    sha = str(exps[-1].get("sha") or "") if exps else ""
    key = key_of(problem, (found or {}).get("class", ""), triage, sha)
    reason = due(ws, problem, found, card, key, now, s)
    if reason:
        request(manager, ws, problem, agent_id, reason,
                question(problem, found or {}, triage, reason=reason, lead_ref=lead_ref), key, s, now=now)
    pending = open_request(ws, problem, agent_id, live)
    if not pending or not s.get("cards", True):
        return None
    found_rows = answers(rows, scouts, str(pending.get("ts") or ""))
    if not found_rows:
        return None
    from ..ledger import view
    refs = [view.ref(r) for r in found_rows[:3]]
    doc = {"signal": "doc scout answer", "evidence": {"refs": refs, "reason": pending.get("reason")}}
    text, meta = cards.build("scout", problem, doc, triage, limit=int(s.get("card_max_bytes") or 800))
    made = cards.issue(ws, agent_id, problem, "scout", text, meta, repeat_sec=0)
    if made:
        mark_answered(ws, str(pending.get("key")), refs, made["card_id"])
    return made


_MIRROR: dict[str, Any] = {"at": 0.0, "doc": None}


def mirror_status() -> dict[str, Any] | None:
    """The docs mirror (directory and file count), refreshed once a minute; ``web/`` is created so a
    worker's first save succeeds."""
    if time.monotonic() - float(_MIRROR["at"]) < 60 and _MIRROR["doc"] is not None:
        return _MIRROR["doc"]
    try:
        from ..commands.docs_cmd import mirror_dir, status
        (mirror_dir() / "web").mkdir(parents=True, exist_ok=True)
        _MIRROR["doc"] = status()
    except Exception:
        _MIRROR["doc"] = None
    _MIRROR["at"] = time.monotonic()
    return _MIRROR["doc"]
