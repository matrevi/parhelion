"""Cost-efficiency metrics per worker and per problem (research R9).

- tokens and device-seconds per kept improvement: an approved local gate verdict that was
  not forced or overridden, counted once per ``(problem, sha7)``;
- tokens and device-seconds per official gain point: the gain of a judged receipt is how far
  its official score rose above the best official score that problem had received before it
  (board-level gain, not corrected for tbest drift);
- gain per judgement: official gain points divided by judged receipts.

Tokens come from ``metrics.usage`` (all prompt tokens plus output, and the uncached part),
device-seconds from the test-run registry (``device_batch.hold_sec``), verdicts from the gate
log, receipts from ``scheduler.receipts``. A gate verdict or receipt is attributed to the
worker that first tested that SHA, else to the worker that asked the gate for it.
"""
from __future__ import annotations

import collections
from pathlib import Path
from typing import Any

from ..metrics import usage as _usage
from . import sources
from .loader import AgentLog

OVERRIDE_STAGES = frozenset({"FORCED_APPROVAL", "OPERATOR_OVERRIDE", "OVERRIDE_CIRCUIT"})


def _in(ts: str, since: str | None, until: str | None) -> bool:
    return bool(ts) and not ((since and ts < since) or (until and ts > until))


def kept(row: dict[str, Any]) -> bool:
    stages = {str(s) for s in row.get("stages") or []}
    return bool(row.get("approved")) and not (stages & OVERRIDE_STAGES)


def _ratio(num: float, den: float) -> float | None:
    return round(num / den, 1) if den else None


def _owner_map(tests: list[dict[str, Any]], logs: list[AgentLog]) -> dict[tuple[str, str], str]:
    owner: dict[tuple[str, str], str] = {}
    for rec in sorted(tests, key=lambda r: str(r.get("created_at") or "")):
        key = (str(rec.get("problem") or ""), str(rec.get("sha") or "")[:7])
        if rec.get("agent_id") and key[1]:
            owner.setdefault(key, str(rec["agent_id"]))
    for log in logs:
        for call in log.tool_calls():
            if call.name == "parhelion_submit_gate":
                key = (str(call.args.get("problem") or ""), str(call.args.get("sha") or "")[:7])
                if key[0] and key[1]:
                    owner.setdefault(key, log.agent_id)
    return owner


def _receipt_gains(rows: list[dict[str, Any]], since: str | None, until: str | None) -> list[dict[str, Any]]:
    best: dict[str, float] = {}
    out = []
    for r in sorted(rows, key=lambda r: str(r.get("finished_at") or r.get("submitted_at") or "")):
        title = str(r.get("title") or "")
        ts = str(r.get("finished_at") or r.get("submitted_at") or "")
        score = r.get("official_score")
        prior = best.get(title, 0.0)
        if _in(ts, since, until):
            gain = max(0.0, float(score) - prior) if isinstance(score, (int, float)) else 0.0
            out.append({"title": title, "sha7": str(r.get("sha") or "")[:7], "ts": ts, "gain": round(gain, 3),
                        "status": r.get("status")})
        if isinstance(score, (int, float)):
            best[title] = max(prior, float(score))
    return out


def compute(logs: list[AgentLog], workspace: Path, since: str | None, until: str | None, *,
            usage_by_agent: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    tests = sources.test_records(workspace, since, until)
    gates = sources.gate_rows(workspace, since, until)
    receipts = _receipt_gains(sources.receipt_rows(workspace), since, until)
    owner = _owner_map(tests, logs)
    problem_of: dict[str, str] = {}
    for rec in tests:
        if rec.get("agent_id"):
            problem_of.setdefault(str(rec["agent_id"]), str(rec.get("problem") or ""))
    rows: dict[str, dict[str, Any]] = collections.defaultdict(lambda: collections.Counter())
    for log in logs:
        res = (usage_by_agent or {}).get(log.agent_id) or _usage.agent_usage(Path(log.path), log.agent_id)
        calls = [c for c in res.get("calls") or [] if _in(str(c.get("ts") or ""), since, until)]
        row = rows[log.agent_id]
        row["tokens_total"] += sum(c["input_miss"] + c["cache_read"] + c["cache_write"] + c["output"] for c in calls)
        row["tokens_uncached"] += sum(c["input_miss"] + c["cache_write"] + c["output"] for c in calls)
        row["model_calls"] += len(calls)
    for rec in tests:
        aid = str(rec.get("agent_id") or "")
        if aid:
            rows[aid]["tests"] += 1
            rows[aid]["device_sec"] += float(rec.get("hold_sec") or 0.0)
    kept_keys: set[tuple[str, str]] = set()
    for g in gates:
        key = (str(g.get("title") or ""), str(g.get("sha") or "")[:7])
        if kept(g) and key not in kept_keys:
            kept_keys.add(key)
            rows[owner.get(key, "(unattributed)")]["kept_improvements"] += 1
    for r in receipts:
        aid = owner.get((r["title"], r["sha7"]), "(unattributed)")
        rows[aid]["judgements"] += 1
        rows[aid]["official_gain"] += r["gain"]
    return {"per_worker": {aid: _view(c, problem_of.get(aid, "")) for aid, c in sorted(rows.items())},
            "per_problem": _per_problem(rows, problem_of, receipts),
            "totals": _view(sum(rows.values(), collections.Counter()), "")}


def _view(c: collections.Counter, problem: str) -> dict[str, Any]:
    gain = round(float(c["official_gain"]), 3)
    return {
        "problem": problem, "model_calls": int(c["model_calls"]), "tokens_total": int(c["tokens_total"]),
        "tokens_uncached": int(c["tokens_uncached"]), "tests": int(c["tests"]),
        "device_sec": round(float(c["device_sec"]), 1), "kept_improvements": int(c["kept_improvements"]),
        "judgements": int(c["judgements"]), "official_gain": gain,
        "tokens_per_kept": _ratio(c["tokens_total"], c["kept_improvements"]),
        "uncached_tokens_per_kept": _ratio(c["tokens_uncached"], c["kept_improvements"]),
        "device_sec_per_kept": _ratio(c["device_sec"], c["kept_improvements"]),
        "tokens_per_gain_point": _ratio(c["tokens_total"], gain),
        "uncached_tokens_per_gain_point": _ratio(c["tokens_uncached"], gain),
        "device_sec_per_gain_point": _ratio(c["device_sec"], gain),
        "gain_per_judgement": round(gain / c["judgements"], 3) if c["judgements"] else None,
    }


def _per_problem(rows: dict[str, collections.Counter], problem_of: dict[str, str],
                 receipts: list[dict[str, Any]]) -> dict[str, Any]:
    acc: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for aid, c in rows.items():
        prob = problem_of.get(aid)
        if prob:
            acc[prob].update({k: v for k, v in c.items() if k not in ("judgements", "official_gain")})
    for r in receipts:  # every judged receipt of the problem, whoever submitted it
        acc[r["title"]]["judgements"] += 1
        acc[r["title"]]["official_gain"] += r["gain"]
    return {p: _view(c, p) for p, c in sorted(acc.items())}
