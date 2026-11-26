"""Noise-aware per-case verdicts: improved / indistinguishable / regressed (4.2 R3).

A paired change is ``delta = ln(before / after)`` of two warm minima on the same input. Its
noise band is the combined relative spread of the two readings: by default the coefficient of
variation of each side's warm samples, combined in quadrature. A report whose ``timing_source``
is not the plain event timer (paired A/B data or profiler timing, P10) may give a per-case
interval ``interval_us: [low, high]``; that interval then sets the side's band instead.

- ``improved``: ``delta`` above the band by more than ``MARGIN`` (1%);
- ``regressed``: the mirror image;
- ``indistinguishable``: everything else, including a case without samples on either side.

A case whose repeated samples spread more than ``NOISY_CV`` (3%) on either side is ``noisy``: it
keeps its verdict for inspection but never counts toward a gain claim (``summarize``). Raw
microsecond differences are never evidence of improvement on their own.
"""
from __future__ import annotations

import math
import statistics
from typing import Any

MARGIN = 0.01
NOISY_CV = 0.03
EVENT_TIMER = "event_warm"


def _positive(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def cv(samples: Any) -> float | None:
    values = [float(v) for v in samples or [] if _positive(v)]
    if len(values) < 2:
        return None
    return statistics.pstdev(values) / statistics.fmean(values)


def band(row: dict[str, Any], timing_source: str | None = None) -> float | None:
    """Relative half-width of one reading: a P10/profiler interval when given, else the sample CV."""
    interval = row.get("interval_us")
    if (timing_source not in (None, EVENT_TIMER) and isinstance(interval, list) and len(interval) == 2
            and all(_positive(v) for v in interval) and interval[0] <= interval[1]):
        return math.log(interval[1] / interval[0]) / 2
    return cv(row.get("device_us"))


def classify(delta: float, noise: float | None, margin: float = MARGIN) -> str:
    edge = (noise or 0.0) + margin
    if noise is None:
        return "indistinguishable"
    return "improved" if delta > edge else "regressed" if delta < -edge else "indistinguishable"


def paired(before: dict[str, Any], after: dict[str, Any], *, before_source: str | None = None,
           after_source: str | None = None, margin: float = MARGIN, noisy_cv: float = NOISY_CV
           ) -> dict[str, Any] | None:
    """The verdict on one case, or None when either side has no warm minimum."""
    b_us, a_us = before.get("min_us"), after.get("min_us")
    if not (_positive(b_us) and _positive(a_us)):
        return None
    delta = math.log(b_us / a_us)
    sides = [band(before, before_source), band(after, after_source)]
    noise = math.sqrt(sum(s * s for s in sides)) if all(s is not None for s in sides) else None
    spreads = [cv(before.get("device_us")), cv(after.get("device_us"))]
    return {"verdict": classify(delta, noise, margin), "delta": round(delta, 4),
            "band": None if noise is None else round(noise, 4),
            "noisy": any(s is not None and s > noisy_cv for s in spreads)}


def summarize(per_case: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Counts for a test summary; noisy cases are listed apart and claim nothing."""
    clean = {k: v for k, v in per_case.items() if not v["noisy"]}
    return {
        "improved": sorted(k for k, v in clean.items() if v["verdict"] == "improved"),
        "regressed": sorted(k for k, v in clean.items() if v["verdict"] == "regressed"),
        "indistinguishable": sum(v["verdict"] == "indistinguishable" for v in clean.values()),
        "noisy": sorted(k for k, v in per_case.items() if v["noisy"]),
    }


def outcome(summary: dict[str, Any]) -> str:
    """One word for an experiment (ledger ``experiment.outcome``): any regression wins over gains."""
    if summary.get("regressed"):
        return "regressed"
    return "improved" if summary.get("improved") else "indistinguishable"


def noisy_cases(report: dict[str, Any], noisy_cv: float = NOISY_CV) -> list[str]:
    """Timed cases of one run whose repeated samples spread beyond ``noisy_cv``."""
    return sorted(r["id"] for r in report.get("cases") or []
                  if (cv(r.get("device_us")) or 0.0) > noisy_cv and r.get("id"))
