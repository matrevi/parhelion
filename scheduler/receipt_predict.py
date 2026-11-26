"""Predict a candidate's official score from a judged SHA's per-case times (4.1 #7).

For candidate c and a judged base b (preferably the board SHA):

- ``T_b[j]``: b's official time on judge case j (receipt, live board);
- ``r[j] = (L_b[i] - off) / (L_c[i] - off)`` on the local case i mapped to j, over formal,
  timing-eligible runs of the same suite version; ``off`` is the map's launch overhead (lower
  quartile of local - judge on sub-50 us cases, so a systematically slower local reading is not
  mistaken for overhead);
- a slow-mode reading of b (its local/judge ratio more than SPIKE times its own typical ratio
  on the other mapped cases) is replaced by the reading its judged time implies, because one
  25 us event reading of a 2 us kernel would otherwise predict a 10x speedup;
- the event timer resolves a few microseconds, so ``r[j]`` is shrunk on a log scale with weight
  ``T_b / (T_b + NOISE_US)``: a 3 us judge case barely moves, a 300 us one moves fully;
- ``t_c[j] = T_b[j] / r[j]``, scored with the judge's own formula against the newest tbest:
  ``100 / (1 + ln(t / tbest) / ln 1.5)`` averaged over the full roster (untimed cases 0).
  ``t_c < tbest`` scores 100 and is listed under ``refresh_tbest``.

A judge case without a usable local proxy keeps ``T_b[j]`` (no change predicted); the share
of timed cases with a proxy is ``coverage``. ``base_score`` is b replayed against the same
tbest, so ``estimated_score - base_score`` is a same-generation gain (AGENTS.md section 4).
"""
from __future__ import annotations

import datetime
import math
import pathlib
import statistics
from typing import Any

from . import case_map, judge_cases, receipts

METHOD = "receipt_per_case"
RATIO_CLAMP = (0.1, 10.0)  # a per-case change beyond 10x either way is treated as measurement noise
# Event timing resolves a kernel only to a few microseconds, so on a case the judge runs in
# T us a local ratio counts with weight T / (T + NOISE_US) (log scale); see P01-backtest.md.
NOISE_US = 10.0
SPIKE = 3.0


def calibrated_offset(pairs: list[dict[str, Any]], local: dict[str, float], judged: dict[str, float]) -> float:
    """Lower-quartile additive overhead (local - judge) over mapped cases the judge runs in < 50 us."""
    gaps = [local[p["local"]] - judged[p["judge"]] for p in pairs
            if p.get("judge_mapped") and not p.get("scale_mismatch") and p["local"] in local
            and p["judge"] in judged and judged[p["judge"]] < case_map.MIN_JUDGED_US]
    return case_map.lower_quartile(gaps)


def despike(pairs: list[dict[str, Any]], local: dict[str, float], judged: dict[str, float]) -> dict[str, float]:
    """b's local readings with slow-mode spikes replaced by the reading its judged time implies."""
    same = [p for p in pairs if not p.get("scale_mismatch") and p["local"] in local and p["judge"] in judged]
    logs = {p["local"]: math.log(local[p["local"]] / judged[p["judge"]]) for p in same}
    if len(logs) < 3:
        return dict(local)
    typical = statistics.median(logs.values())
    out = dict(local)
    for p in same:
        if logs[p["local"]] - typical > math.log(SPIKE):
            out[p["local"]] = judged[p["judge"]] * math.exp(typical)
    return out


def board_sha(workspace: pathlib.Path, title: str, as_of: datetime.datetime | None = None) -> str | None:
    """SHA of the newest receipt before ``as_of`` that refreshed the board (``beat_board``)."""
    best: tuple[datetime.datetime, str] | None = None
    for row in receipts.load(workspace, title):
        stamp = judge_cases.parse_iso(row.get("submitted_at") or row.get("finished_at"))
        if (receipts.beat_board(row) is True and stamp is not None and (as_of is None or stamp < as_of)
                and row.get("sha") and (best is None or stamp > best[0])):
            best = (stamp, str(row["sha"]))
    return best[1] if best else None


def _bases(workspace: pathlib.Path, title: str, preferred: list[str | None], obs: list[dict[str, Any]],
           timed: list[str]) -> list[tuple[str, dict[str, Any], str]]:
    """Judged SHAs with official times on every timed case: preferred SHAs first, then newest."""
    full: dict[str, dict[str, Any]] = {}
    for o in obs:
        if all(k in o["times"] for k in timed):
            full[o["sha"]] = o  # observations are sorted by time: the newest wins
    out, seen = [], set()
    for sha in preferred:
        if sha and sha in full and sha not in seen:
            out.append((sha, full[sha], "board" if sha == preferred[0] else "best"))
            seen.add(sha)
    for sha, o in sorted(full.items(), key=lambda kv: kv[1]["at"], reverse=True):
        if sha not in seen:
            out.append((sha, o, "other"))
            seen.add(sha)
    return out


def predict(workspace: pathlib.Path, title: str, cand_sha: str, base_shas: list[str | None],
            as_of: datetime.datetime | None = None, *, tbest_override: dict[str, float | None] | None = None,
            doc: dict[str, Any] | None = None, allow_other: bool = True,
            write_map: bool = False) -> dict[str, Any] | None:
    """Per-case prediction for ``cand_sha`` or None when the evidence does not support one.

    ``base_shas`` are tried first (board, then historical best); with ``allow_other`` any other
    judged SHA with full official times may serve, newest first. Far bases extrapolate across
    large per-case changes and are less accurate (P01-backtest.md), so the gate does not use them.
    The stored case map is refreshed only with ``write_map`` (the gate); analysis stays read-only.
    """
    if doc is None:
        doc = (case_map.build(workspace, title, as_of) if as_of
               else case_map.current(workspace, title, write=write_map))
    pairs = [p for p in doc.get("pairs") or [] if p.get("judge_mapped")]
    if not case_map.usable(doc) or not pairs:
        return None
    keys, untimed = list(doc["judge_ids"]), set(doc.get("judge_untimed") or [])
    timed = [k for k in keys if k not in untimed]
    rows = receipts.load(workspace, title)
    order = {"keys": keys, "kind": doc.get("judge_id_kind"), "count": len(keys), "untimed": sorted(untimed)}
    if tbest_override is not None:
        tbest, seen_at, tbest_source = dict(tbest_override), None, "override"
    else:
        tbest, seen_at, tbest_source = judge_cases.tbest_now(workspace, title, as_of, rows, order)
    if set(tbest) != set(keys):
        return None
    version = doc.get("pairing_key")
    cand, cand_runs = case_map.local_minima(workspace, title, cand_sha, version, strict=True, as_of=as_of)
    if not cand:
        return None
    obs = judge_cases.observations(workspace, title, rows, order, as_of)
    for base_sha, base_obs, role in _bases(workspace, title, base_shas, obs, timed):
        if base_sha == cand_sha or (role == "other" and not allow_other):
            continue
        base, base_runs = case_map.local_minima(workspace, title, base_sha, version, strict=True, as_of=as_of)
        usable_pairs = [p for p in pairs if p["local"] in base and p["local"] in cand]
        result = _score(doc, keys, tbest, base_obs, base, cand, usable_pairs, timed, {
            "base_sha": base_sha, "base_role": role, "base_submission_id": base_obs["submission_id"],
            "base_source": base_obs["source"], "runs": [cand_runs, base_runs],
            "tbest_observed_at": seen_at, "tbest_source": tbest_source}) if usable_pairs else None
        if result is not None:
            return result
    return None


def _score(doc, keys, tbest, base_obs, base, cand, pairs, timed, meta) -> dict[str, Any]:
    judged = base_obs["times"]
    offset = doc.get("offset_us")
    offset = calibrated_offset(pairs, base, judged) if offset is None else offset
    base = despike(pairs, base, judged)
    proxy = {p["judge"]: p for p in pairs}
    predicted: dict[str, float | None] = {}
    ratios: dict[str, float] = {}
    for key in keys:
        if tbest.get(key) is None:
            predicted[key] = None
            continue
        t_b = judged[key]
        pair = proxy.get(key)
        if pair is None:
            predicted[key] = t_b
            continue
        local_id = pair["local"]
        ratio = case_map.kernel_us(base[local_id], offset) / case_map.kernel_us(cand[local_id], offset)
        ratio = min(RATIO_CLAMP[1], max(RATIO_CLAMP[0], ratio)) ** (t_b / (t_b + NOISE_US))
        ratios[key] = ratio
        predicted[key] = t_b / ratio
    score = receipts.judge_score(predicted, tbest)
    base_score = receipts.judge_score({k: judged.get(k) for k in keys}, tbest)
    if score is None or base_score is None:
        return None
    coverage = len(ratios) / max(1, len(timed))
    geo = math.exp(sum(math.log(r) for r in ratios.values()) / len(ratios)) if ratios else 1.0
    return {
        "method": METHOD, "estimated_score": score, "base_score": base_score,
        "gain": round(score - base_score, 2), "coverage": round(coverage, 3), "matched": len(ratios),
        "timed": len(timed), "geo": round(geo, 4), "offset_us": offset,
        "improved": sorted(k for k, r in ratios.items() if r > 1.03),
        "regressed": sorted(k for k, r in ratios.items() if r < 0.95),
        "refresh_tbest": sorted(k for k, t in predicted.items() if t is not None and tbest.get(k)
                                and t < tbest[k]),
        "map_method": doc.get("method"), "map_confidence": doc.get("confidence"),
        "cases": {k: {"t_base": round(judged[k], 3), "ratio": round(ratios[k], 4) if k in ratios else None,
                      "t_pred": round(predicted[k], 3), "tbest": round(tbest[k], 3),
                      "points": round(receipts.case_points(predicted[k], tbest[k]), 3)}
                  for k in keys if predicted.get(k) is not None},
        **meta,
    }
