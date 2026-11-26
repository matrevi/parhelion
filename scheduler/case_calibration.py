"""Per-case calibration of local predictions against the judge (4.2 R2).

For every canonical receipt with official per-case times (scheduler.judge_cases.observations)
one record pairs, per mapped case (scheduler.case_map):

- ``local``: the local predicted change against the same-generation baseline,
  ``ln(L_base / L_cand)`` on warm minima corrected by the map's launch overhead, over the two
  SHAs' formal, verified, timing-eligible runs (the code does not change after its receipt, so
  later runs measure the same thing; ``late_runs`` counts the candidate's runs after the receipt)
  without unconfirmed implausible readings (scheduler.plausibility);
- ``band``: the local noise band of that change (scheduler.verdicts: sample CVs in quadrature);
- ``judge_delta``: ``ln(T_base / T_cand)`` of the two official times;
- the baseline used: the board SHA at submission time (scheduler.receipt_predict.board_sha),
  else the newest earlier judged SHA.

Records live in ``results/<title>/.calibration-pairs.jsonl``, one per ``submission_id`` (a later
line for the same ID replaces the earlier one; replaying receipts appends nothing). A record is
recomputed only when the case map's method or pairing key changes.

Per case and per problem: sign agreement over decisive pairs (both changes beyond noise), the
Spearman rank correlation, and the regression rate (of the local wins, the share the judge saw
as a loss). Labels: ``trusted``, ``divergent`` (local and judge disagree), else ``noisy``
(too little or too mixed evidence).
"""
from __future__ import annotations

import datetime
import fcntl
import json
import math
import pathlib
import statistics
from typing import Any

from . import case_map, judge_cases, pairing, plausibility, receipts, verdicts

FILE = ".calibration-pairs.jsonl"
JUDGE_BAND = 0.03
MIN_DECISIVE = 3
TRUST_AGREE, TRUST_REGRESS = 0.75, 0.2
DIVERGE_AGREE, DIVERGE_REGRESS = 0.5, 0.5
LABELS = ("trusted", "noisy", "divergent")
_MEMO: dict[tuple[str, str, str, str], dict[str, Any]] = {}


def path(ws: pathlib.Path, title: str) -> pathlib.Path:
    return ws / "results" / title / FILE


def load(ws: pathlib.Path, title: str) -> dict[str, dict[str, Any]]:
    """Stored records by submission ID; the last line of an ID wins."""
    out: dict[str, dict[str, Any]] = {}
    try:
        lines = path(ws, title).read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("submission_id"):
            out[str(row["submission_id"])] = row
    return out


def _usable(ws, title, sha) -> list[dict[str, Any]]:
    return [d for d in case_map._reports(ws, title, sha)
            if d.get("verified") is True and d.get("exit_code") == 0 and d.get("timing_score_eligible") is True]


def _local_groups(docs: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    """``{pairing_key: {case_id: best row}}`` over one SHA's usable runs."""
    blocked = plausibility.unconfirmed(docs)
    groups: dict[str, dict[str, dict[str, Any]]] = {}
    for doc in docs:
        best = groups.setdefault(pairing.pairing_key(doc), {})
        for row in doc.get("cases") or []:
            value = row.get("min_us")
            if (row.get("source") == "official" and row.get("status") == "PASS"
                    and isinstance(value, (int, float)) and value > 0 and plausibility.gate_minimum(doc, row, blocked)
                    and (row["id"] not in best or value < best[row["id"]]["min_us"])):
                best[row["id"]] = row
    return groups


def _baseline(obs: list[dict[str, Any]], o: dict[str, Any], board: str | None):
    at = judge_cases.parse_iso(o["at"])
    earlier = [b for b in obs if b["sha"] != o["sha"] and (s := judge_cases.parse_iso(b["at"])) and at and s < at]
    for b in reversed(earlier):
        if board and b["sha"] == board:
            return b, "board"
    return (earlier[-1], "previous") if earlier else (None, None)


def _signature(doc: dict[str, Any]) -> list[Any]:
    return [doc.get("method"), doc.get("pairing_key")]


def pair_record(ws, title, o, obs, doc, board: str | None) -> dict[str, Any]:
    """The calibration record of one observation (see the module docstring)."""
    base, role = _baseline(obs, o, board)
    record: dict[str, Any] = {"submission_id": o["submission_id"], "sha": o["sha"], "at": o["at"],
                              "map": _signature(doc), "baseline": None, "cases": {}}
    at = judge_cases.parse_iso(o["at"])
    if base is None or at is None:
        record["reason"] = "no earlier judged SHA with per-case times"
        return record
    record["baseline"] = {"sha": base["sha"], "submission_id": base["submission_id"], "role": role}
    cand_docs = _usable(ws, title, o["sha"])
    cand, prior = _local_groups(cand_docs), _local_groups(_usable(ws, title, base["sha"]))
    shared = [k for k in cand if k in prior]
    if not shared:
        record["reason"] = "no shared formal timing runs of the two SHAs"
        return record
    record["late_runs"] = sum(1 for d in cand_docs if (t := judge_cases.parse_iso(d.get("finished_at"))) and t >= at)
    key = max(shared, key=lambda k: len(set(cand[k]) & set(prior[k])))
    record["pairing_key"] = key
    offset = float(doc.get("offset_us") or 0.0)
    record["offset_us"] = offset
    for pair in doc.get("pairs") or []:
        lid, jk = pair["local"], pair["judge"]
        if not pair.get("judge_mapped") or lid not in cand[key] or lid not in prior[key]:
            continue
        if jk not in o["times"] or jk not in base["times"]:
            continue
        before, after = prior[key][lid], cand[key][lid]
        local = math.log(case_map.kernel_us(before["min_us"], offset) / case_map.kernel_us(after["min_us"], offset))
        bands = [verdicts.band(before), verdicts.band(after)]
        record["cases"][lid] = {
            "judge": jk, "local": round(local, 4),
            "band": round(math.sqrt(sum(b * b for b in bands)), 4) if None not in bands else None,
            "judge_delta": round(math.log(base["times"][jk] / o["times"][jk]), 4)}
    return record


def refresh(ws: pathlib.Path, title: str, *, write: bool = True,
            as_of: datetime.datetime | None = None) -> list[dict[str, Any]]:
    """Every record of the problem, computing the missing ones; new lines are appended on ``write``."""
    stored = load(ws, title)
    try:
        doc = case_map.current(ws, title, write=False)
    except (OSError, ValueError, KeyError, TypeError):
        return [r for r in stored.values() if _before(r, as_of)]
    rows = receipts.load(ws, title)
    obs = judge_cases.observations(ws, title, rows)
    from . import submit_intent
    from .receipt_predict import board_sha

    # 4.3 P23 (R5 §7): a forced or overridden shot is not evidence about the local prediction, so it
    # leaves the per-case table; a probe stays and is marked, because it is a normal gated shot.
    by_sid = {str(r.get("submission_id") or ""): r for r in rows if r.get("submission_id")}
    marks = {o["submission_id"]: submit_intent.calibration_marks(
        ws, by_sid.get(o["submission_id"]) or {"title": title, "sha": o["sha"]}) for o in obs}
    obs = [o for o in obs if not marks[o["submission_id"]]["excluded"]]

    fresh = []
    for o in obs:
        if not case_map.usable(doc) or not _before(o, as_of):
            continue
        known = stored.get(o["submission_id"])
        if known is not None and known.get("map") == _signature(doc):
            continue
        # A record depends only on evidence before its receipt: computed once per process and map.
        memo = (str(ws), title, o["submission_id"], json.dumps(_signature(doc)))
        if memo not in _MEMO:
            _MEMO[memo] = pair_record(ws, title, o, obs, doc, board_sha(ws, title, judge_cases.parse_iso(o["at"])))
        record = {**_MEMO[memo], "intent": marks[o["submission_id"]]["intent"]}
        stored[o["submission_id"]] = record
        fresh.append(record)
    if write and fresh and path(ws, title).parent.is_dir() and as_of is None:
        _append(ws, title, fresh)
    return [r for r in stored.values() if _before(r, as_of)]


def _before(record: dict[str, Any], as_of: datetime.datetime | None) -> bool:
    stamp = judge_cases.parse_iso(record.get("at"))
    return as_of is None or (stamp is not None and stamp < as_of)


def _append(ws: pathlib.Path, title: str, records: list[dict[str, Any]]) -> None:
    target = path(ws, title)
    with target.with_name(FILE + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        present = load(ws, title)
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        lines = [json.dumps({**r, "computed_at": now}, sort_keys=True, allow_nan=False) for r in records
                 if present.get(r["submission_id"], {}).get("map") != r["map"]]
        if lines:
            with target.open("a", encoding="utf-8") as out:
                out.write("\n".join(lines) + "\n")


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2
        i = j + 1
    return ranks


def spearman(x: list[float], y: list[float]) -> float | None:
    if len(x) < 3:
        return None
    rx, ry = _ranks(x), _ranks(y)
    if statistics.pstdev(rx) == 0 or statistics.pstdev(ry) == 0:
        return None
    return round(statistics.correlation(rx, ry), 3)


def stats(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Agreement statistics of a list of ``{local, band, judge_delta}`` pairs."""
    def beyond(e: dict[str, Any]) -> float:
        return (e.get("band") or 0.0) + verdicts.MARGIN

    decisive = [e for e in entries if abs(e["local"]) > beyond(e) and abs(e["judge_delta"]) > JUDGE_BAND]
    agree = [(e["local"] > 0) == (e["judge_delta"] > 0) for e in decisive]
    wins = [e for e in entries if e["local"] > beyond(e)]
    losses = [e for e in wins if e["judge_delta"] < -JUDGE_BAND]
    return {
        "pairs": len(entries), "decisive": len(decisive),
        "sign_agreement": round(sum(agree) / len(agree), 3) if agree else None,
        "rank_corr": spearman([e["local"] for e in entries], [e["judge_delta"] for e in entries]),
        "local_wins": len(wins),
        "regression_rate": round(len(losses) / len(wins), 3) if wins else None,
        "discrepancy": round(statistics.median(abs(e["local"] - e["judge_delta"]) for e in entries), 4)
        if entries else None,
    }


def label(st: dict[str, Any]) -> str:
    if st["local_wins"] >= 2 and (st["regression_rate"] or 0.0) >= DIVERGE_REGRESS:
        return "divergent"
    if st["decisive"] >= 2 and (st["sign_agreement"] or 0.0) < DIVERGE_AGREE:
        return "divergent"
    if st["pairs"] >= 4 and st["rank_corr"] is not None and st["rank_corr"] < 0:
        return "divergent"
    if (st["decisive"] >= MIN_DECISIVE and (st["sign_agreement"] or 0.0) >= TRUST_AGREE
            and (st["regression_rate"] is None or st["regression_rate"] <= TRUST_REGRESS)):
        return "trusted"
    return "noisy"


def table(ws: pathlib.Path, title: str, *, write: bool = False, as_of: datetime.datetime | None = None,
          records: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Per-case and per-problem statistics and labels from the calibration records."""
    records = refresh(ws, title, write=write, as_of=as_of) if records is None else records
    by_case: dict[str, list[dict[str, Any]]] = {}
    judge_of: dict[str, str] = {}
    for record in records:
        for lid, entry in (record.get("cases") or {}).items():
            by_case.setdefault(lid, []).append(entry)
            judge_of[lid] = entry.get("judge")
    cases = {}
    for lid, entries in sorted(by_case.items()):
        st = stats(entries)
        cases[lid] = {"judge": judge_of[lid], "label": label(st), **st}
    overall = stats([e for entries in by_case.values() for e in entries])
    return {
        "title": title, "receipts": len(records), "paired_receipts": sum(bool(r.get("cases")) for r in records),
        "cases": cases, "problem": {"label": label(overall), **overall},
        "labels": {name: sorted(k for k, v in cases.items() if v["label"] == name) for name in LABELS},
    }
