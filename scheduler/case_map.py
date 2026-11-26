"""Which local official case stands for which judge testcase (4.1 #5).

Pairing by sorted local ID against the judge's roster is only right when the two lists are
the same roster. ``build`` decides per problem and writes ``results/<title>/.case-map.json``:

- ``roster``: the judge roster has as many cases as the template's official catalog, so the
  n-th local case is the n-th judge case (the template enumerates the judge's cases in order;
  untimed judge cases line up with empty local cases, e.g. K=0).
- ``assignment``: otherwise, per submission with full official times, the order-preserving
  match of log times under one common offset; ``confident`` needs two submissions that agree.
- ``unmapped``: neither holds; consumers fall back to their previous behaviour.

A paired case can still be a different workload: the template's parameters are not always
the judge's data (FFT local 05 is 8x256^3 and runs seconds; the judge's 5th case takes
milliseconds with a 24 us tbest). The local device is the judge's device type, so on a case
the judge runs for MIN_JUDGED_US or longer (launch overhead no longer matters) the same
workload reads within a small factor of the judge. A pair whose local/judge ratio there is
beyond SCALE_OUTLIER either way is ``scale_mismatch``; it stays ``judge_mapped`` only when the
two times have moved together across COMOVE_SHAS SHAs, so local speedups still transfer.
Unmapped local cases keep their correctness role; they only leave timing offsets and score
prediction. The map changes only with new evidence.
"""
from __future__ import annotations

import datetime
import json
import math
import pathlib
import statistics
from collections import Counter
from typing import Any

from . import judge_cases, pairing, plausibility, receipts, timing
from .parhelion_paths import atomic_write_json

MAP_FILE = ".case-map.json"
SCALE_OUTLIER = 4.0      # local/judge ratio beyond which a sized case is another workload
MIN_JUDGED_US = 50.0     # below this the local launch overhead dominates the ratio
COMOVE_SHAS = 3          # a scale-mismatched pair still transfers speedups when it co-moves
COMOVE_LOG_SD = 0.1


def map_path(workspace: pathlib.Path, title: str) -> pathlib.Path:
    return workspace / "results" / title / MAP_FILE


_REPORTS: dict[str, tuple[tuple[int, int], Any]] = {}


def _report(path: pathlib.Path) -> Any:
    """A parsed run report; finished reports never change, so one parse per process and version."""
    try:
        stat = path.stat()
        sig = (stat.st_mtime_ns, stat.st_size)
        hit = _REPORTS.get(str(path))
        if hit is None or hit[0] != sig:
            _REPORTS[str(path)] = hit = (sig, json.loads(path.read_text(encoding="utf-8")))
        return hit[1]
    except (OSError, ValueError):
        return None


def _reports(workspace: pathlib.Path, title: str, sha: str = "*"):
    for path in sorted((workspace / "results" / title).glob(f"{sha}/bench-runs/*/report.json")):
        doc = _report(path)
        if isinstance(doc, dict) and doc.get("formal") is True and doc.get("sha") == path.parents[2].name:
            yield doc


def local_catalog(workspace: pathlib.Path, title: str) -> tuple[Any, list[str]]:
    """``(pairing key, sorted official IDs)`` of the newest formal run of any SHA (scheduler.pairing)."""
    newest = max(_reports(workspace, title), key=lambda d: d.get("finished_at") or "", default=None)
    if newest is None:
        return None, []
    ids = sorted({c["id"] for c in newest.get("cases") or [] if c.get("source") == "official" and c.get("id")})
    return pairing.pairing_key(newest), ids


def local_minima(workspace: pathlib.Path, title: str, sha: str, version: Any, *, strict: bool = False,
                 as_of: datetime.datetime | None = None, source: str = "event") -> tuple[dict[str, float], int]:
    """Best warm minimum per official case in one timing source over formal runs with one
    official-input identity.

    ``version`` is a pairing key (scheduler.pairing); None accepts every run.

    ``strict`` keeps only verified, timing-eligible runs (the estimator's rule); otherwise any
    formal run counts for the cases it timed and passed. ``source`` is ``event`` (every run has
    it) or ``profiler`` (scheduler.timing); the two are never combined.
    """
    minima: dict[str, float] = {}
    runs = 0
    docs = list(_reports(workspace, title, sha))
    blocked = plausibility.unconfirmed(docs)  # implausible readings wait for a cold confirmation
    for doc in docs:
        if version is not None and pairing.pairing_key(doc) != version:
            continue
        if strict and not (doc.get("verified") is True and doc.get("exit_code") == 0
                           and doc.get("timing_score_eligible") is True):
            continue
        if as_of is not None:
            done = judge_cases.parse_iso(doc.get("finished_at"))
            if done is None or done >= as_of:
                continue
        runs += 1
        if source == "profiler" and not timing.profiler_trusted(doc):
            continue
        for case in doc.get("cases") or []:
            value = timing.case_value(case, source)
            if (case.get("source") == "official" and case.get("status") in ("PASS", None) and value is not None
                    and plausibility.gate_minimum(doc, case, blocked)):
                minima[case["id"]] = min(minima.get(case["id"], value), value)
    return minima, runs


def _assign(local: dict[str, float], judge: dict[str, float]) -> list[tuple[str, str]]:
    """Order-preserving min-cost match of log times with one common offset (alternating fit)."""
    a = sorted(local.items(), key=lambda kv: kv[1])
    b = sorted(judge.items(), key=lambda kv: kv[1])
    if not a or not b:
        return []
    swap = len(a) > len(b)
    if swap:
        a, b = b, a
    la, lb = [math.log(v) for _, v in a], [math.log(v) for _, v in b]
    offset = 0.0  # same device type: start from equal times, then fit the common offset
    pairs: list[tuple[int, int]] = []
    for _ in range(6):
        m, n = len(la), len(lb)
        inf = float("inf")
        cost = [[inf] * (n + 1) for _ in range(m + 1)]
        cost[0] = [0.0] * (n + 1)
        for i in range(1, m + 1):
            for j in range(i, n + 1):
                take = cost[i - 1][j - 1] + (la[i - 1] - lb[j - 1] - offset) ** 2
                cost[i][j] = min(cost[i][j - 1], take)
        i, j, found = m, n, []
        while i > 0:
            if cost[i][j] == cost[i][j - 1] and j > i:
                j -= 1
                continue
            found.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        found.reverse()
        if found == pairs:
            break
        pairs = found
        offset = statistics.mean(la[p] - lb[q] for p, q in pairs)
    out = [(a[p][0], b[q][0]) for p, q in pairs]
    return sorted((y, x) for x, y in out) if swap else sorted(out)


def kernel_us(local_us: float, offset_us: float) -> float:
    """Local reading minus the launch overhead, never below 10% of the reading."""
    return max(local_us - offset_us, 0.1 * local_us)


def lower_quartile(values: list[float]) -> float:
    """Lower quartile, clipped at 0; 0 when there are no values."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return round(max(0.0, ordered[(len(ordered) - 1) // 4]), 3)


def launch_offset(samples: list[tuple[str, float, float]]) -> float:
    """Additive local launch overhead over cases the judge runs < 50 us, from every judged SHA.

    The lower quartile of (local - judge): slow-mode readings and problems whose local runs are
    systematically slower only ever add to the gap, so the median would mistake them for overhead.
    """
    return lower_quartile([lo - ju for _, lo, ju in samples if ju < MIN_JUDGED_US])


def _pair_stats(local_id: str, key: str, samples: list[tuple[str, float, float]]) -> dict[str, Any]:
    ratios = [lo / ju for _, lo, ju in samples]
    logs = [math.log(r) for r in ratios]
    shas = {sha for sha, _, _ in samples}
    return {"local": local_id, "judge": key, "samples": len(samples), "shas": len(shas),
            "scale": round(statistics.median(ratios), 4) if ratios else None,
            "judge_us": round(statistics.median(j for _, _, j in samples), 3) if samples else None,
            "log_sd": round(statistics.pstdev(logs), 4) if len(logs) > 1 else None}


def _judge(pairs: list[dict[str, Any]], untimed: set[str]) -> None:
    for pair in pairs:
        pair.update(judge_mapped=True, scale_mismatch=False, reason="")
        if pair["judge"] in untimed:
            pair.update(judge_mapped=False, reason="judge does not time this case")
            continue
        scale = pair["scale"]
        if not scale or (pair["judge_us"] or 0) < MIN_JUDGED_US or 1 / SCALE_OUTLIER <= scale <= SCALE_OUTLIER:
            continue
        pair["scale_mismatch"] = True
        if pair["shas"] >= COMOVE_SHAS and pair["log_sd"] is not None and pair["log_sd"] <= COMOVE_LOG_SD:
            pair["reason"] = f"local x{scale:.3g} of the judge, but moves with it across {pair['shas']} SHAs"
        else:
            pair.update(judge_mapped=False, reason=f"local x{scale:.3g} of the judge at "
                                                   f"{pair['judge_us']:.0f} us: a different workload")


def build(workspace: pathlib.Path, title: str, as_of: datetime.datetime | None = None) -> dict[str, Any]:
    """The map from evidence on disk; ``as_of`` replays it from receipts and runs before that time."""
    rows = receipts.load(workspace, title)
    order = judge_cases.roster(workspace, title, rows)
    version, local_ids = local_catalog(workspace, title)
    obs = judge_cases.observations(workspace, title, rows, order, as_of)
    minima = {}
    for o in obs:
        if o["sha"] not in minima:
            minima[o["sha"]] = local_minima(workspace, title, o["sha"], version, as_of=as_of)[0]
    doc: dict[str, Any] = {
        "title": title, "pairing_key": version, "judge_ids": order["keys"], "judge_id_kind": order["kind"],
        "judge_count": order["count"], "judge_untimed": order["untimed"], "local_ids": local_ids,
        "method": "unmapped", "confidence": "none", "pairs": [],
        "evidence": [{"submission_id": o["submission_id"], "sha": o["sha"], "source": o["source"],
                      "cases": len(o["times"]), "local_cases": len(minima.get(o["sha"]) or {})}
                     for o in obs if minima.get(o["sha"])],
    }
    if order["keys"] and local_ids and len(order["keys"]) == len(local_ids):
        doc["method"] = "roster"
        matched = list(zip(local_ids, order["keys"]))
    else:
        full = [o for o in obs if len(o["times"]) == len(judge_cases._timed(order)) and minima.get(o["sha"])]
        votes = Counter(tuple(_assign(minima[o["sha"]], o["times"])) for o in full)
        best, agree = votes.most_common(1)[0] if votes else ((), 0)
        doc["assignment_votes"] = [{"pairs": len(p), "submissions": n} for p, n in votes.most_common(3)]
        matched = list(best) if agree >= 2 else []
        doc["method"] = "assignment" if matched else "unmapped"
    by_pair = {(local_id, key): [(o["sha"], minima[o["sha"]][local_id], o["times"][key]) for o in obs
                                 if local_id in (minima.get(o["sha"]) or {}) and key in o["times"]]
               for local_id, key in matched}
    doc["offset_us"] = launch_offset([s for samples in by_pair.values() for s in samples])
    pairs = [_pair_stats(local_id, key, by_pair[(local_id, key)]) for local_id, key in matched]
    _judge(pairs, set(order["untimed"]))
    doc["pairs"] = pairs
    shas = {o["sha"] for o in obs if minima.get(o["sha"])}
    if doc["method"] == "assignment":
        doc["confidence"] = "confident"
    elif doc["method"] == "roster":
        doc["confidence"] = "high" if len(shas) >= 2 else ("medium" if shas else "structural")
    mapped = {p["local"] for p in pairs if p["judge_mapped"]}
    doc["unmapped_local"] = [cid for cid in local_ids if cid not in mapped]
    doc["unmapped_judge"] = [k for k in order["keys"] if k not in {p["judge"] for p in pairs if p["judge_mapped"]}]
    return doc


def current(workspace: pathlib.Path, title: str, *, rebuild: bool = False, write: bool = True) -> dict[str, Any]:
    """The stored map, rebuilt when its evidence changed (or on ``rebuild``); written only on change."""
    path = map_path(workspace, title)
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = None
    doc = build(workspace, title)
    if isinstance(stored, dict) and not rebuild and {k: v for k, v in stored.items() if k != "built_at"} == doc:
        return stored
    doc["built_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if write and (path.parent.is_dir() or rebuild):
        atomic_write_json(path, doc)
    return doc


def usable(doc: dict[str, Any] | None) -> bool:
    return bool(doc) and doc.get("method") in ("roster", "assignment") and bool(doc.get("pairs"))


def mapped_pairs(doc: dict[str, Any] | None) -> dict[str, str]:
    """``{local_id: judge_key}`` for pairs that may carry timing (empty when the map is unusable)."""
    if not usable(doc):
        return {}
    return {p["local"]: p["judge"] for p in doc["pairs"] if p.get("judge_mapped")}


def summary(doc: dict[str, Any]) -> dict[str, Any]:
    pairs = doc.get("pairs") or []
    return {"title": doc.get("title"), "method": doc.get("method"), "confidence": doc.get("confidence"),
            "judge_count": doc.get("judge_count"), "local_count": len(doc.get("local_ids") or []),
            "mapped": sum(1 for p in pairs if p.get("judge_mapped")), "evidence": len(doc.get("evidence") or []),
            "unmapped_local": doc.get("unmapped_local"), "unmapped_judge_count": len(doc.get("unmapped_judge") or []),
            "notes": {p["local"]: p["reason"] for p in pairs if p.get("reason")}}
