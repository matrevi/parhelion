"""The interval around a gate's predicted score (4.2 R2 item 3).

With a per-case receipt prediction (scheduler.receipt_predict) every judge case has a
predicted time ``t = T_base / r``. A case's time is bounded per case and scored with the judge's
formula at both ends (the score falls monotonically with every case time, so the box bounds the
score):

- a case whose local case is ``trusted`` or ``noisy`` (scheduler.case_calibration) spans
  ``t * exp(+-h)``, ``h`` its calibration discrepancy (median |local - judge| change, at least
  the judge's own noise ``JUDGE_BAND``);
- a ``divergent`` case's contribution is unknown, not zero: it spans from ``T_base * exp(D)``
  slower than its baseline (or than the prediction, whichever is slower) to ``T_base * exp(-D)``
  faster than the faster of the two, ``D = max(ln 2, its discrepancy)``. These cases are listed.
- a case without a local proxy keeps ``T_base`` within the judge's noise.

Other estimation methods only have a Gaussian sigma: their interval is ``estimate +- Z * sigma``,
widened by the share of divergent cases among the calibrated ones, which are listed. Quota never
enters this computation.
"""
from __future__ import annotations

import math
from typing import Any

from . import case_calibration, receipts

Z = 1.2816  # two-sided 80% for the Gaussian fallback
UNKNOWN_SPAN = math.log(2.0)


def per_case(rp: dict[str, Any], table: dict[str, Any], shift: float = 0.0) -> dict[str, Any] | None:
    """The interval of a receipt prediction; None when its cases cannot be scored."""
    cases, labels = rp.get("cases") or {}, table.get("cases") or {}
    fast: dict[str, float | None] = {}
    slow: dict[str, float | None] = {}
    tbest: dict[str, float | None] = {}
    divergent = []
    for key, c in cases.items():
        tbest[key] = c.get("tbest")
        t_base, t_pred, lid = c.get("t_base"), c.get("t_pred"), c.get("local")
        info = labels.get(lid) if lid else None
        spread = max(case_calibration.JUDGE_BAND, (info or {}).get("discrepancy") or 0.0)
        if c.get("ratio") is None or t_base is None:
            fast[key], slow[key] = t_pred * math.exp(-case_calibration.JUDGE_BAND), \
                t_pred * math.exp(case_calibration.JUDGE_BAND)
        elif info and info.get("label") == "divergent":
            span = max(UNKNOWN_SPAN, spread)
            divergent.append(lid)
            fast[key], slow[key] = min(t_base, t_pred) * math.exp(-span), max(t_base, t_pred) * math.exp(span)
        else:
            fast[key], slow[key] = t_pred * math.exp(-spread), t_pred * math.exp(spread)
    for key, value in tbest.items():
        if value is None:
            fast.pop(key, None)
            slow.pop(key, None)
    high, low = receipts.judge_score(fast, tbest), receipts.judge_score(slow, tbest)
    if high is None or low is None:
        return None
    return {"method": "per_case", "low": round(max(0.0, low + shift), 2), "high": round(high + shift, 2),
            "divergent": sorted(divergent), "unknown_cases": len(divergent),
            "labels": {k: v for k, v in (table.get("labels") or {}).items()}}


def gaussian(estimate: float, sigma: float, table: dict[str, Any]) -> dict[str, Any]:
    divergent = list((table.get("labels") or {}).get("divergent") or [])
    widen = 1.0 + len(divergent) / max(1, len(table.get("cases") or {})) if divergent else 1.0
    half = Z * max(0.0, sigma) * widen
    return {"method": "sigma", "low": round(max(0.0, estimate - half), 2), "high": round(estimate + half, 2),
            "divergent": sorted(divergent), "unknown_cases": len(divergent),
            "labels": {k: v for k, v in (table.get("labels") or {}).items()}}


_TABLES: dict[tuple, dict[str, Any]] = {}


def _stamp(file) -> tuple[int, int]:
    try:
        st = file.stat()
        return st.st_mtime_ns, st.st_size
    except OSError:
        return 0, 0


def calibration_table(ws, title: str, as_of=None) -> dict[str, Any]:
    """The problem's calibration table, cached per process until receipts or records change."""
    key = (str(ws), title, as_of.isoformat() if as_of else None, _stamp(receipts.ledger_path(ws)),
           _stamp(case_calibration.path(ws, title)))
    if key not in _TABLES:
        if len(_TABLES) > 256:
            _TABLES.clear()
        _TABLES[key] = case_calibration.table(ws, title, write=as_of is None, as_of=as_of)
    return _TABLES[key]


def build(ws, title: str, rp: dict[str, Any] | None, estimate: float, sigma: float, *,
          shift: float = 0.0, as_of=None) -> dict[str, Any]:
    """The interval the gate returns with its predicted score; never raises."""
    try:
        table = calibration_table(ws, title, as_of)
    except Exception:  # noqa: BLE001 - calibration is advisory; the estimate stands without it
        table = {}
    try:
        out = per_case(rp, table, shift) if rp else None
    except (TypeError, ValueError, ZeroDivisionError):
        out = None
    return out or gaussian(estimate, sigma, table)
