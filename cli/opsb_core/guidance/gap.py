"""The gap triage of one problem: where the points are and at what scale to think (stuck-guidance §3.2).

Per judged case: our time over tbest (the ratio), the judge points ``100 / (1 + ln(t/tbest) / ln 1.5)``,
the weighted headroom ``(100 - points) x weight / n`` and the weighted gain of a 2x, 10x and 100x
speedup on that case alone. The scoring is logarithmic, so a 2x on a case at 2x tbest is worth tens of
points while a 2x on a case at 400x tbest is worth well under one: the triage says this in numbers.

Tiers by ratio (``guidance.tier_bounds``, default 3 and 50):

``algorithm``  >= 50x: only a different algorithm or execution unit pays (an order of magnitude or more);
``datapath``   3-50x: data movement and tiling (coalesced transfers, double buffering, on-chip reuse);
``tuning``     < 3x: micro-tuning (unrolling, fewer synchronisations, scalar overhead).

Each tier also gets the gain if every case in it improved by the tier's typical factor
(``guidance.tier_factors``): the cases of one tier usually share a code path, so one change moves them
together. Pure: the caller passes the case rows (``services.state_card_ext.case_rows`` shape).
"""
from __future__ import annotations

import math
from typing import Any

TIERS = ("algorithm", "datapath", "tuning")
TIER_TEXT = {
    "algorithm": "a different algorithm or execution unit (for example a large matrix product moved from the "
                 "vector unit to the matrix unit, or an O(N^2) pass replaced by sorting or bucketing)",
    "datapath": "data movement and tiling (coalesced transfers, double buffering, reuse of the on-chip buffer)",
    "tuning": "micro-tuning (loop unrolling, merged synchronisation, less scalar overhead)",
}
FACTORS = (2.0, 10.0, 100.0)


def points(time_us: float, tbest_us: float) -> float:
    """The judge formula of ``scheduler.receipts.case_points`` (100 at or below tbest)."""
    if not tbest_us or tbest_us <= 0 or not time_us or time_us <= 0:
        return 0.0
    return 100.0 / (1.0 + math.log(max(time_us, tbest_us) / tbest_us) / math.log(1.5))


def tier_of(ratio: float, bounds: tuple[float, float]) -> str:
    low, high = bounds
    return "algorithm" if ratio >= high else "datapath" if ratio >= low else "tuning"


def _bounds(settings: dict[str, Any]) -> tuple[float, float]:
    raw = settings.get("tier_bounds") or [3, 50]
    try:
        return float(raw[0]), float(raw[1])
    except (TypeError, ValueError, IndexError):
        return 3.0, 50.0


def _factors(settings: dict[str, Any]) -> dict[str, float]:
    raw = settings.get("tier_factors") if isinstance(settings.get("tier_factors"), dict) else {}
    base = {"algorithm": 10.0, "datapath": 2.0, "tuning": 1.3}
    for name in TIERS:
        try:
            base[name] = max(1.0, float(raw.get(name, base[name])))
        except (TypeError, ValueError):
            pass
    return base


def triage(rows: list[dict[str, Any]], weight: float, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """``{cases, tiers, n, weight, focus}``; cases without both times are left out (and counted)."""
    from . import settings as _settings
    s = settings or _settings()
    bounds, factors = _bounds(s), _factors(s)
    timed = [r for r in rows if (r.get("ours_us") or 0) > 0 and (r.get("tbest_us") or 0) > 0]
    n = len(timed)
    w = float(weight or 1.0)
    cases: list[dict[str, Any]] = []
    for row in timed:
        ours, best = float(row["ours_us"]), float(row["tbest_us"])
        now = points(ours, best)
        ratio = ours / best
        share = w / n
        cases.append({
            "case": str(row.get("case")), "judge": str(row.get("judge") or ""), "ours_us": round(ours, 2),
            "tbest_us": round(best, 2), "ratio": round(ratio, 2), "points": round(now, 2),
            "headroom_w": round((100.0 - now) * share, 2),
            **{f"gain{int(f)}_w": round((points(ours / f, best) - now) * share, 2) for f in FACTORS},
            "tier": tier_of(ratio, bounds),
        })
    tiers: dict[str, dict[str, Any]] = {}
    for name in TIERS:
        members = [c for c in cases if c["tier"] == name]
        if not members:
            continue
        factor = factors[name]
        typical = sum((points(c["ours_us"] / factor, c["tbest_us"]) - c["points"]) * w / n for c in members)
        tiers[name] = {
            "cases": [c["case"] for c in sorted(members, key=lambda c: -c["ratio"])],
            "ratio_range": [min(c["ratio"] for c in members), max(c["ratio"] for c in members)],
            "headroom_w": round(sum(c["headroom_w"] for c in members), 2),
            "factor": factor, "gain_typical_w": round(typical, 2),
            **{f"gain{int(f)}_w": round(sum(c[f"gain{int(f)}_w"] for c in members), 2) for f in FACTORS},
            "method": TIER_TEXT[name],
        }
    focus = max(tiers, key=lambda t: (tiers[t]["gain_typical_w"], -TIERS.index(t))) if tiers else ""
    small = [((points(c["ours_us"] / 1.1, c["tbest_us"]) - c["points"]) * w / n, c["case"]) for c in cases]
    best10 = max(small, key=lambda x: (x[0], x[1])) if small else None
    return {"cases": sorted(cases, key=lambda c: (-c["headroom_w"], c["case"])), "tiers": tiers, "n": n,
            "untimed": len(rows) - n, "weight": w, "focus": focus,
            "best_10pct": {"gain_w": round(best10[0], 2), "case": best10[1]} if best10 else None,
            "headroom_w": round(sum(c["headroom_w"] for c in cases), 2)}


def fmt_ratio(value: float) -> str:
    """``4.3`` below ten, ``239`` above: enough digits to read the scale, no more."""
    return f"{value:.1f}" if value < 10 else f"{value:.0f}"


def summary_line(doc: dict[str, Any]) -> str:
    """One sentence for the index and the start-up pack: the best tier and what its typical change earns."""
    tiers = doc.get("tiers") or {}
    focus = doc.get("focus")
    if not focus or focus not in tiers:
        return "gap triage: no judged receipt with per-case times yet"
    t = tiers[focus]
    low, high = t["ratio_range"]
    parts = [f"most points per change: the {focus} tier ({len(t['cases'])} cases at {fmt_ratio(low)}-"
             f"{fmt_ratio(high)}x tbest), "
             f"a {t['factor']:g}x there earns about +{t['gain_typical_w']:.1f} weighted pts"]
    if "algorithm" in tiers and focus != "algorithm":
        a = tiers["algorithm"]
        parts.append(f"the {len(a['cases'])} cases at >= {fmt_ratio(a['ratio_range'][0])}x hold {a['headroom_w']:.1f} pts "
                     f"but a 2x there earns only +{a['gain2_w']:.1f}; they need 10-100x (+{a['gain10_w']:.1f}"
                     f" / +{a['gain100_w']:.1f})")
    return "; ".join(parts)
