"""Per-case speedup and confidence interval from A/B paired runs (4.1 route #2).

A paired run (``opsb test --compare <base>``) alternates candidate A and baseline B inside
one device job, so round r of a case gives the pair (A_r, B_r) under the same host state. The
per-case ratio is the geometric mean of B_r / A_r (above 1: the candidate is faster). Its
interval is the t interval of the log differences; ``bootstrap`` gives a percentile interval
with a fixed seed for checking. Event timing carries an additive launch overhead on both arms;
``offset_us`` removes it before the logs are taken (the profiler needs none).

Paired evidence lives in the candidate's ``bench-runs`` with ``compare.base_sha``; ``load``
returns the newest one for a (candidate, baseline) pair and one suite version, which the gate's
Stage 2 prefers over minima from separate runs.
"""
from __future__ import annotations

import json
import math
import pathlib
import random
import statistics
from typing import Any

from . import pairing

#: Two-sided 95% Student t quantiles by degrees of freedom; 1.96 beyond the table.
T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262,
       10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110,
       18: 2.101, 19: 2.093, 20: 2.086, 25: 2.060, 30: 2.042, 40: 2.021, 60: 2.000, 120: 1.980}
MIN_KERNEL_SHARE = 0.1  # an overhead larger than the reading leaves a tenth of it as kernel time


def t95(df: int) -> float:
    if df <= 0:
        return float("inf")
    keys = [k for k in sorted(T95) if k >= df]
    return T95[keys[0]] if keys else 1.96


def _kernel(value: float, offset: float) -> float:
    return max(value - offset, MIN_KERNEL_SHARE * value)


def log_diffs(a: list[float], b: list[float], offset_us: float = 0.0) -> list[float]:
    """``ln(B_r) - ln(A_r)`` over complete, positive pairs (overhead removed first)."""
    return [math.log(_kernel(y, offset_us)) - math.log(_kernel(x, offset_us))
            for x, y in zip(a, b) if x and y and x > 0 and y > 0]


def interval(a: list[float], b: list[float], offset_us: float = 0.0) -> dict[str, Any] | None:
    """Geometric-mean ratio B/A with its 95% t interval on the log differences."""
    d = log_diffs(a, b, offset_us)
    if not d:
        return None
    mean = statistics.fmean(d)
    if len(d) < 2:
        return {"ratio": round(math.exp(mean), 4), "lo": None, "hi": None, "n": 1, "method": "log_t"}
    half = t95(len(d) - 1) * statistics.stdev(d) / math.sqrt(len(d))
    return {"ratio": round(math.exp(mean), 4), "lo": round(math.exp(mean - half), 4),
            "hi": round(math.exp(mean + half), 4), "n": len(d), "method": "log_t"}


def bootstrap(a: list[float], b: list[float], offset_us: float = 0.0, iterations: int = 2000,
              seed: int = 0) -> dict[str, Any] | None:
    """Percentile interval of the geometric-mean ratio by resampling pairs (deterministic)."""
    d = log_diffs(a, b, offset_us)
    if len(d) < 2:
        return None
    rng = random.Random(seed)
    means = sorted(statistics.fmean(rng.choices(d, k=len(d))) for _ in range(iterations))
    lo, hi = means[int(0.025 * iterations)], means[min(iterations - 1, int(0.975 * iterations))]
    return {"ratio": round(math.exp(statistics.fmean(d)), 4), "lo": round(math.exp(lo), 4),
            "hi": round(math.exp(hi), 4), "n": len(d), "method": "bootstrap"}


def unpaired_interval(a: list[float], b: list[float], offset_us: float = 0.0) -> dict[str, Any] | None:
    """Welch interval of the log means from separate runs, to compare widths with ``interval``."""
    la = [math.log(_kernel(x, offset_us)) for x in a if x and x > 0]
    lb = [math.log(_kernel(y, offset_us)) for y in b if y and y > 0]
    if len(la) < 2 or len(lb) < 2:
        return None
    va, vb = statistics.variance(la) / len(la), statistics.variance(lb) / len(lb)
    se = math.sqrt(va + vb)
    df = (va + vb) ** 2 / ((va ** 2) / (len(la) - 1) + (vb ** 2) / (len(lb) - 1)) if se > 0 else len(la) + len(lb) - 2
    mean = statistics.fmean(lb) - statistics.fmean(la)
    half = t95(max(1, int(df))) * se
    return {"ratio": round(math.exp(mean), 4), "lo": round(math.exp(mean - half), 4),
            "hi": round(math.exp(mean + half), 4), "n": min(len(la), len(lb)), "method": "welch_log"}


def width(ci: dict[str, Any] | None) -> float | None:
    """Relative width ``hi/lo - 1`` of an interval."""
    if not ci or not ci.get("lo") or not ci.get("hi"):
        return None
    return round(ci["hi"] / ci["lo"] - 1.0, 4)


def case_pairs(row: dict[str, Any]) -> tuple[str, list[float], list[float]] | None:
    """``(source, A samples, B samples)`` of a paired report row, one timing source for both arms."""
    pairs = row.get("pairs") or {}
    for source in ("profiler", "event"):
        rounds = pairs.get(source)
        if rounds:
            return source, [p[0] for p in rounds], [p[1] for p in rounds]
    return None


def load(workspace: pathlib.Path, title: str, cand_sha: str, base_sha: str, version: Any = None,
         as_of: Any = None) -> dict[str, Any] | None:
    """Newest verified paired report of ``cand_sha`` against ``base_sha`` (``version``: a pairing key)."""
    best = None
    for path in (pathlib.Path(workspace) / "results" / title / cand_sha / "bench-runs").glob("*/report.json"):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        compare = doc.get("compare") if isinstance(doc, dict) else None
        if not (isinstance(compare, dict) and compare.get("base_sha") == base_sha and doc.get("sha") == cand_sha
                and doc.get("exit_code") == 0 and (version is None or pairing.pairing_key(doc) == version)):
            continue
        finished = str(doc.get("finished_at") or "")
        if as_of is not None and (not finished or finished >= as_of.isoformat()):
            continue
        if best is None or finished > str(best.get("finished_at") or ""):
            best = doc
    return best


def ratios(doc: dict[str, Any], official_only: bool = True) -> dict[str, dict[str, Any]]:
    """``{case: interval}`` of a paired report (base over candidate), passed cases only."""
    out = {}
    for row in doc.get("cases") or []:
        if official_only and row.get("source") != "official":
            continue
        ci = row.get("pair")
        if row.get("status") == "PASS" and row.get("base_status") == "PASS" and isinstance(ci, dict) and ci.get("ratio"):
            out[row["id"]] = ci
    return out
