"""``opsb replay stuck``: run the P30 detector over a past sprint, read only, and list the stuck episodes.

For every worker ledger of every problem, the detector (``guidance.stuck.classify``) is evaluated every
``step`` minutes on the rows written up to that moment, with the gap triage of the newest receipt judged
by then. Consecutive steps of one class form an episode. Next to each worker the replay prints what the
ledger says actually happened: experiments, outcomes and new local bests, so a reader can check that the
detector fires where the worker was in fact stuck and stays quiet where it made progress.

Nothing is written: ledgers, receipts and the case map are only read (``case_map.current(write=False)``).
"""
from __future__ import annotations

import datetime
from collections import Counter
from pathlib import Path
from typing import Any

from ..guidance import gap as _gap, settings, stuck


def _receipts_until(rows: list[dict[str, Any]], until: datetime.datetime) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        ts = stuck.parse_ts(row.get("finished_at") or row.get("submitted_at"))
        if ts is not None and ts <= until:
            out.append(row)
    return out


def _triage(ws: Path, problem: str, receipts: list[dict[str, Any]], s: dict[str, Any]) -> dict[str, Any]:
    from ..services import state_card_ext as ext
    got = ext.receipt_cases(receipts)
    names = ext.labels(ws, problem) if got else {}
    rows = [{"case": names.get(k, f"#{i:02d}"), "judge": k, "ours_us": (v or {}).get("ours"),
             "tbest_us": (v or {}).get("tbest")} for i, (k, v) in enumerate((got.get("cases") or {}).items(), 1)]
    return _gap.triage(rows, ext.weight_of(ws, problem), s)


def worker(ws: Path, problem: str, rows: list[dict[str, Any]], receipts: list[dict[str, Any]], *,
           step: float, since: datetime.datetime | None, until: datetime.datetime | None,
           s: dict[str, Any]) -> dict[str, Any]:
    from ..ledger import view
    rows = sorted(rows, key=lambda r: str(r.get("ts") or ""))
    stamps = [t for t in (stuck.parse_ts(r.get("ts")) for r in rows) if t]
    if not stamps:
        return {}
    start = stamps[0]
    first = max(start, since) if since else start
    last = min(stamps[-1] + datetime.timedelta(minutes=step * 2), until) if until else stamps[-1]
    episodes: list[dict[str, Any]] = []
    t = first
    gap_cache: dict[int, dict[str, Any]] = {}
    while t <= last:
        upto = [r for r in rows if (stuck.parse_ts(r.get("ts")) or t) <= t]
        cur = view.current(upto)
        exps = [r for r in cur if r.get("type") == "experiment"]
        gates = [r for r in cur if r.get("type") == "hypothesis" and str(r.get("ekey") or "").startswith("gate:")]
        judged = _receipts_until(receipts, t)
        tri = gap_cache.setdefault(len(judged), _triage(ws, problem, judged, s))
        hit = stuck.classify(exps, now=t, start=start, gates=gates, gap=tri, settings=s)
        cls = hit["class"] if hit else ""
        if episodes and episodes[-1]["class"] == cls:
            episodes[-1]["until"] = t.isoformat(timespec="minutes")
            episodes[-1]["steps"] += 1
        else:
            episodes.append({"class": cls, "from": t.isoformat(timespec="minutes"),
                             "until": t.isoformat(timespec="minutes"), "steps": 1,
                             "signal": (hit or {}).get("signal", "")})
        t += datetime.timedelta(minutes=step)
    cur = view.current(rows)
    exps = [r for r in cur if r.get("type") == "experiment"]
    prog = stuck.progress(sorted(exps, key=lambda r: str(r.get("ts") or "")), s)
    return {"start": start.isoformat(timespec="minutes"), "end": stamps[-1].isoformat(timespec="minutes"),
            "experiments": len(exps), "outcomes": dict(Counter(str(e.get("outcome")) for e in exps)),
            "new_bests": [str(e.get("ts"))[11:16] for e in prog["events"]],
            "episodes": [e for e in episodes if e["class"]], "steps": sum(e["steps"] for e in episodes),
            "stuck_steps": sum(e["steps"] for e in episodes if e["class"])}


def session_counts(doc: dict[str, Any], path: Path, step: float) -> None:
    """Aggregate tool-call counts of one worker's mesh log inside its stuck episodes (no content kept).

    Per episode: calls, ``parhelion_kb_query`` calls, ``parhelion_test_wait`` calls, and the minutes from the
    episode start to the first ``test_wait`` (when a card riding on that reply would have arrived).
    """
    from .loader import load_agent
    try:
        calls = [(stuck.parse_ts(c.ts), c.name) for c in load_agent(path).tool_calls()]
    except Exception:
        return
    calls = [(ts, name) for ts, name in calls if ts]
    for ep in doc.get("episodes") or []:
        lo = stuck.parse_ts(ep["from"])
        hi = stuck.parse_ts(ep["until"]) + datetime.timedelta(minutes=step)
        inside = [(ts, name) for ts, name in calls if lo <= ts <= hi]
        waits = [ts for ts, name in inside if name.endswith("test_wait")]
        ep["calls"] = {"total": len(inside), "kb_query": sum(1 for _, n in inside if n.endswith("kb_query")),
                       "test_wait": len(waits),
                       "first_test_wait_min": round((waits[0] - lo).total_seconds() / 60.0, 1) if waits else None}


def run(ws: Path | str, *, since: str | None = None, until: str | None = None, step: float = 5.0,
        problems: list[str] | None = None, sessions: Path | str | None = None) -> dict[str, Any]:
    from .. import config as _cfg
    from ..ledger import store
    from ..services import state_card_ext as ext
    root = Path(ws).expanduser().resolve()
    s = settings()
    lo, hi = stuck.parse_ts(since) if since else None, stuck.parse_ts(until) if until else None
    names = problems or [str(p.get("title")) for p in (_cfg.read_json(root / "opsb.workspace.json", {}) or {})
                         .get("problems") or [] if p.get("title")]
    out: dict[str, Any] = {"workspace": str(root), "step_min": step, "since": since, "until": until,
                           "problems": {}}
    for problem in names:
        receipts = ext.receipts_rows(root, problem)
        seats = {}
        for name in store.workers(root, problem):
            if name == "platform":
                continue
            rows = store.read_rows(store.problem_dir(root, problem) / f"{name}.jsonl")
            rows = [{**r, "worker": r.get("worker") or name} for r in rows]
            if hi:
                rows = [r for r in rows if (stuck.parse_ts(r.get("ts")) or hi) <= hi]
            if lo and not any((stuck.parse_ts(r.get("ts")) or lo) >= lo for r in rows):
                continue
            doc = worker(root, problem, rows, receipts, step=step, since=lo, until=hi, s=s)
            if doc and doc.get("experiments"):
                log = Path(sessions).expanduser() / f"mesh-{name}.jsonl" if sessions else None
                if log is not None and log.is_file():
                    session_counts(doc, log, step)
                seats[name] = doc
        if seats:
            out["problems"][problem] = seats
    totals = Counter()
    for seats in out["problems"].values():
        for doc in seats.values():
            for ep in doc["episodes"]:
                totals[ep["class"]] += 1
    out["episodes_by_class"] = dict(totals)
    counted = [ep["calls"] for seats in out["problems"].values() for d in seats.values()
               for ep in d["episodes"] if ep.get("calls")]
    if counted:
        delays = sorted(c["first_test_wait_min"] for c in counted if c["first_test_wait_min"] is not None)
        out["sessions"] = {"episodes": len(counted), "calls": sum(c["total"] for c in counted),
                           "kb_query": sum(c["kb_query"] for c in counted),
                           "test_wait": sum(c["test_wait"] for c in counted),
                           "episodes_with_test_wait": len(delays),
                           "median_first_test_wait_min": delays[len(delays) // 2] if delays else None}
    return out


def render(doc: dict[str, Any]) -> str:
    lines = [f"stuck replay of {doc['workspace']} every {doc['step_min']:g} min"
             + (f" from {doc['since']}" if doc.get("since") else "") + (f" to {doc['until']}" if doc.get("until") else "")]
    for problem, seats in doc["problems"].items():
        lines.append(f"\n{problem}")
        for name, w in seats.items():
            share = w["stuck_steps"] / max(1, w["steps"])
            outs = " ".join(f"{k}:{v}" for k, v in sorted(w["outcomes"].items()))
            lines.append(f"  {name} {w['start'][11:]}-{w['end'][11:]} exps {w['experiments']} ({outs}) "
                         f"new bests {','.join(w['new_bests']) or '-'} · stuck {share:.0%} of steps")
            for ep in w["episodes"]:
                calls = ep.get("calls")
                tail = (f" [calls {calls['total']}, kb_query {calls['kb_query']}, test_wait {calls['test_wait']}"
                        + (f", first test_wait +{calls['first_test_wait_min']:g} min" if calls["first_test_wait_min"]
                           is not None else "") + "]") if calls else ""
                lines.append(f"    {ep['from'][11:]}-{ep['until'][11:]} {ep['class']}: {ep['signal']}{tail}")
    lines.append("\nepisodes by class: " + ", ".join(f"{k} {v}" for k, v in sorted(doc["episodes_by_class"].items())))
    if doc.get("sessions"):
        s = doc["sessions"]
        lines.append(f"session logs, inside {s['episodes']} episodes: {s['calls']} tool calls, {s['kb_query']} "
                     f"kb_query, {s['test_wait']} test_wait; {s['episodes_with_test_wait']} episodes had a test_wait, "
                     f"median first one +{s['median_first_test_wait_min']} min after the episode began")
    return "\n".join(lines)
