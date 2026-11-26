"""P32 guide cards: one experiment of at most 800 bytes, one active card per seat, every outcome logged.

A card names the situation in numbers, one hypothesis, one variable to change, the expected effect,
how to judge it and its sources (dossier anchors, the worker's own ledger refs, KB entries only when
verified or backed by an online receipt). It is built from a template per stuck class and data; the
worker's own unexploited insight or open hypothesis becomes the hypothesis when one exists, because
most knowledge is what the agents searched and probed themselves.

Store: ``<workspace state>/guidance/``. ``seats.json`` holds each seat's stuck class and active card
(read by the hot state), ``cards.jsonl`` is the append-only log of issued, delivered, adopted, declined,
next-best and closed events (P35 evaluates it). All writes hold ``.lock``.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
from pathlib import Path
from typing import Any, Iterator

from .. import config as _cfg

LIVE = ("issued", "delivered", "adopted")
FINAL = ("declined", "not_adopted", "superseded", "closed")
DOSSIER = ".parhelion/dossier"
DECLINE = re.compile(r"\b(ignor\w*|skip\w*|declin\w*|not applicable|n/a|reject\w*)\b", re.I)
NO_OUTPUT = re.compile(r"missing|no output|unexpected output|timeout|time limit|killed|signal", re.I)
CHANGE = {
    "algorithm": "replace the algorithm (or execution unit) of the path these cases share; keep the small-case path",
    "datapath": "one data-movement change on these cases (transfer size, double buffering or on-chip reuse)",
    "tuning": "one micro change on these cases (unroll, merged synchronisation or less scalar work)",
}
SHORT = {"algorithm": "a different algorithm or execution unit", "datapath": "data movement and tiling",
         "tuning": "micro-tuning"}


def root(ws: Path | str) -> Path:
    from ..drivers.base import resolve_workspace_state_dir
    return resolve_workspace_state_dir(ws) / "guidance"


@contextlib.contextmanager
def locked(ws: Path | str) -> Iterator[Path]:
    base = root(ws)
    base.mkdir(parents=True, exist_ok=True)
    with (base / ".lock").open("a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield base
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def read_seats(ws: Path | str) -> dict[str, Any]:
    doc = _cfg.read_json(root(ws) / "seats.json", {})
    return doc if isinstance(doc, dict) else {}


def _write_seats(base: Path, doc: dict[str, Any]) -> None:
    tmp = base / f".seats.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, base / "seats.json")


def _log(base: Path, event: dict[str, Any]) -> None:
    with (base / "cards.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": _cfg.utc_now().isoformat(timespec="seconds"), **event}, ensure_ascii=False) + "\n")


def history(ws: Path | str, problem: str = "") -> list[dict[str, Any]]:
    """Every card of the log (optionally one problem), newest first, with its latest status and outcome."""
    cards: dict[str, dict[str, Any]] = {}
    for row in _cfg.read_jsonl(root(ws) / "cards.jsonl"):
        cid = str(row.get("card_id") or "")
        if not cid:
            continue
        if row.get("event") == "issued":
            cards[cid] = {**row, "status": "issued", "issued_at": row.get("ts")}
        elif cid in cards:
            card = cards[cid]
            if row.get("status"):
                card["status"] = row["status"]
            if row.get("outcome"):
                card["outcome"] = row["outcome"]
            if row.get("event") == "next_best":
                card["next_best"] = {k: row.get(k) for k in ("speedup", "minutes", "ref")}
    out = [c for c in cards.values() if not problem or c.get("problem") == problem]
    return sorted(out, key=lambda c: str(c.get("issued_at") or ""), reverse=True)


def update_seat(ws: Path | str, agent_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    with locked(ws) as base:
        doc = read_seats(ws)
        seat = {**(doc.get(agent_id) or {}), **fields}
        doc[agent_id] = seat
        _write_seats(base, doc)
        return seat


def issue(ws: Path | str, agent_id: str, problem: str, cls: str, text: str, meta: dict[str, Any],
          *, repeat_sec: float) -> dict[str, Any] | None:
    """Make ``text`` the seat's active card; None when this seat got a card of ``cls`` within ``repeat_sec``
    (stuck-guidance §5: one class is not repeated inside the window, even after another card replaced it)."""
    now = _cfg.utc_now()
    with locked(ws) as base:
        doc = read_seats(ws)
        seat = doc.get(agent_id) or {}
        old = seat.get("card") if isinstance(seat.get("card"), dict) else None
        log = _cfg.read_jsonl(base / "cards.jsonl")
        for row in log:
            issued = _cfg.parse_iso(row.get("ts")) if row.get("event") == "issued" else None
            if (issued and row.get("agent_id") == agent_id and row.get("class") == cls
                    and (now - issued).total_seconds() < repeat_sec):
                return None
        number = 1 + sum(1 for r in log if r.get("event") == "issued")
        cid = f"G-{number}"
        text = text.replace("G-?", cid)
        if old and old.get("status") in LIVE:
            _log(base, {"event": "closed", "card_id": old["card_id"], "agent_id": agent_id, "status": "superseded",
                        "outcome": f"superseded by {cid}" + (" (adopted)" if old.get("status") == "adopted" else "")})
        card = {"card_id": cid, "class": cls, "status": "issued", "issued_at": now.isoformat(timespec="seconds"),
                "text": text, **meta}
        _log(base, {"event": "issued", "card_id": cid, "agent_id": agent_id, "problem": problem, "class": cls,
                    "text": text, **{k: v for k, v in meta.items() if k in ("targets", "baseline", "sources")}})
        seat["card"] = card
        doc[agent_id] = seat
        _write_seats(base, doc)
        return card


def mark(ws: Path | str, agent_id: str, card_id: str, status: str, **extra: Any) -> dict[str, Any] | None:
    """Move the seat's active card to ``status`` (logged); None when the card is no longer active."""
    with locked(ws) as base:
        doc = read_seats(ws)
        seat = doc.get(agent_id) or {}
        card = seat.get("card") if isinstance(seat.get("card"), dict) else None
        if not card or card.get("card_id") != card_id:
            return None
        card.update({"status": status, **extra})
        _log(base, {"event": "delivered" if status == "delivered" else "status", "card_id": card_id,
                    "agent_id": agent_id, "status": status, **extra})
        doc[agent_id] = seat
        _write_seats(base, doc)
        return card


def set_next_best(ws: Path | str, agent_id: str, card_id: str, best: dict[str, Any]) -> None:
    """Record the first new local best after delivery (once per card), in the seat and the log."""
    with locked(ws) as base:
        doc = read_seats(ws)
        card = (doc.get(agent_id) or {}).get("card")
        if not isinstance(card, dict) or card.get("card_id") != card_id or card.get("next_best"):
            return
        card["next_best"] = best
        _log(base, {"event": "next_best", "card_id": card_id, "agent_id": agent_id, **best})
        _write_seats(base, doc)


def take_for_reply(ws: Path | str, agent_id: str) -> dict[str, Any] | None:
    """The undelivered card to attach to this worker's ``parhelion_test_wait`` reply (marked delivered)."""
    seat = read_seats(ws).get(agent_id) or {}
    card = seat.get("card") if isinstance(seat.get("card"), dict) else None
    if not card or card.get("status") != "issued":
        return None
    done = mark(ws, agent_id, card["card_id"], "delivered", via="test_wait",
                delivered_at=_cfg.utc_now().isoformat(timespec="seconds"))
    return {"id": card["card_id"], "class": card.get("class"), "text": card.get("text"),
            "dossier": f"{DOSSIER}/index.md"} if done else None


def seed_line(ws: Path | str, agent_id: str) -> str:
    """The one line a compaction or handoff seed keeps: card id and status, plus where the dossier is."""
    seat = read_seats(ws).get(agent_id) or {}
    card = seat.get("card") if isinstance(seat.get("card"), dict) else None
    if not card:
        return ""
    return (f"guide card {card.get('card_id')} ({card.get('class')}): {card.get('status')}; dossier "
            f"{DOSSIER}/index.md")


# ---- the card text (pure) -------------------------------------------------------------------------

def _sha7(value: Any) -> str:
    return str(value or "")[:7]


def _fit(lines: list[str], limit: int) -> str:
    """Join and fit to ``limit`` bytes: the head and the sources line stay whole, the middle lines shrink."""
    lines = [line for line in lines if line]
    text = "\n".join(lines)
    while len(text.encode("utf-8")) > limit and len(lines) > 2:
        longest = max(range(1, len(lines) - 1), key=lambda i: len(lines[i]))
        line = lines[longest]
        lines[longest] = line[: max(40, len(line) - (len(text.encode("utf-8")) - limit) - 2)].rstrip() + "…"
        new = "\n".join(lines)
        if new == text:
            break
        text = new
    return text.encode("utf-8")[:limit].decode("utf-8", "ignore")


def build(cls: str, problem: str, stuck: dict[str, Any], gap: dict[str, Any] | None, *,
          lead: dict[str, Any] | None = None, kb: list[str] | None = None, limit: int = 800) -> tuple[str, dict[str, Any]]:
    """``(text, meta)`` of a card; the id placeholder ``G-?`` is replaced when the card is issued."""
    ev = stuck.get("evidence") or {}
    tiers = (gap or {}).get("tiers") or {}
    head = f"[guide G-? · {cls} · {problem}]"
    sources = [f"{DOSSIER}/index.md"]
    targets: dict[str, Any] = {}
    # The worker's own lead is cited by ref, never quoted: ledger text is free-form and the card stays generic.
    lead_line = f"Untested lead of yours: {lead.get('ref')} (findings.md)." if lead else ""
    if cls == "build_loop":
        sig = str(ev.get("signature") or "") if ev.get("same_error") else ""
        lines = [head, f"{stuck.get('signal')}" + (f" (error signature {sig}; the text is in your test result)."
                                                   if sig else "."),
                 "Hypothesis: one interface or syntax use in the newest change is wrong; the rest is not the cause.",
                 f"Change one thing: start from {_sha7(ev.get('last_ok_sha')) or 'the last SHA that built'} and "
                 "re-apply only the smallest piece; check it with a build_only test.",
                 "Judge: the build passes = add the next piece; the same error again = drop that piece and record a "
                 "failure citing G-?."]
        sources += ["dead-ends.md", "findings.md"]
    elif cls == "wa_loop":
        case = str(ev.get("case") or "?")
        targets["cases"] = [case]
        silent = bool(NO_OUTPUT.search(str(ev.get("detail") or "")))
        base = _sha7(ev.get("last_pass_sha")) or "the board SHA"
        lines = [head, f"{stuck.get('signal')}: {str(ev.get('detail') or '')[:60]}",
                 ("Hypothesis: case " + case + " produces no output (crash, resource limit or the environment), "
                  "not a numeric error." if silent else
                  "Hypothesis: one boundary or numeric condition of case " + case + " is wrong; the rest passes."),
                 f"Change one thing: run only case {case} (cases=[\"{case}\"]) on {base} and on HEAD before editing.",
                 f"Judge: {base} fails too = the cause is outside your diff, record a constraint and stop editing "
                 "for it; only HEAD fails = bisect your diff."]
        sources += ["dead-ends.md", "findings.md"]
    elif cls in ("scale_mismatch", "plateau"):
        tier = "algorithm" if cls == "scale_mismatch" and "algorithm" in tiers else str((gap or {}).get("focus") or "")
        t = tiers.get(tier) or {}
        cases = list(t.get("cases") or [])[:4]
        targets["cases"] = cases
        focus = next((c for c in (gap or {}).get("cases") or [] if cases and c["case"] == cases[0]), None)
        factor = float(t.get("factor") or 2.0)
        best10 = (gap or {}).get("best_10pct") or {}
        points = "Points: no judged per-case times yet."
        if t:
            from .gap import fmt_ratio
            low, high = t["ratio_range"]
            points = (f"Points: {tier} tier ({len(t.get('cases') or [])} cases, {fmt_ratio(low)}-{fmt_ratio(high)}x "
                      f"tbest): {factor:g}x there = +{t['gain_typical_w']:.1f} w pts"
                      + (f", 100x = +{t['gain100_w']:.1f}" if tier == "algorithm" else "")
                      + (f"; a 10% change earns at most +{best10['gain_w']:.1f} ({best10['case']})." if best10
                         else "."))
        hyp = (f"Hypothesis: {', '.join(cases) or 'these cases'} need {SHORT.get(tier, 'a change of method')}, "
               "not more tuning.")
        target = (f"{focus['case']} local time below {focus['ours_us'] / factor:.4g} us (now {focus['ours_us']:g})"
                  if focus else "the focus case gets faster beyond noise")
        lines = [head, str(stuck.get("signal") or ""), points, hyp,
                 f"Change one thing: {CHANGE.get(tier, 'one change on the focus cases')}.",
                 f"Judge: {target} = it holds; slower or WA = revert and record a dead end citing G-?.",
                 "Unsure which stage holds the time? Probe first: time that case with one stage stubbed and note "
                 "the split as an insight." if cls == "scale_mismatch" else ""]
        sources += ["gap.md", *(list(kb or [])[:2] or ["prior-art.md", "hardware.md"])]
    else:  # no_candidate
        lines = [head, str(stuck.get("signal") or ""),
                 "Hypothesis: the working tree mixes a working part and a broken part.",
                 "Change one thing: run standard on your best passing SHA, or cut HEAD back to the part that passes.",
                 "Judge: a passing standard run = request the gate on it; failing again = record a failure and "
                 "restore that SHA."]
        sources += ["findings.md", "dead-ends.md"]
    lines.append(lead_line)
    lines.append("Sources: " + ", ".join(sources) + ". Cite G-? in your next note.")
    meta = {"targets": targets, "sources": sources,
            "baseline": {k: ev.get(k) for k in ("best", "runs", "case") if ev.get(k) is not None}}
    return _fit(lines, limit), meta


def adoption(card: dict[str, Any], rows: list[dict[str, Any]]) -> tuple[str, str] | None:
    """``(status, ref)`` when a worker row written after delivery cites the card: adopted or declined."""
    cid = str(card.get("card_id") or "")
    since = str(card.get("delivered_at") or card.get("issued_at") or "")
    pattern = re.compile(rf"\b{re.escape(cid)}\b")
    for row in sorted(rows, key=lambda r: str(r.get("ts") or "")):
        if str(row.get("ts") or "") < since or row.get("source") == "platform":
            continue
        blob = " ".join(str(row.get(k) or "") for k in ("claim", "why", "reason", "text", "resolution_note",
                                                       "links", "evidence", "lesson", "approach"))
        if pattern.search(blob):
            from ..ledger import view
            return ("declined" if DECLINE.search(blob) else "adopted"), view.ref(row)
    return None
