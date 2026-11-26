"""Physical plausibility of local timings (4.2 R1 item 3).

A warm minimum is ``implausible`` when

- it is below the case's analytic floor: the bytes the case moves (inputs plus outputs, measured
  by the runner) over the device's peak bandwidth (``peak_bandwidth_gbps`` in the ``device``
  section of ``config.json``; without it only the ratio checks apply); or
- it is more than ``FACTOR`` (3x) faster than the best earlier reading of the same input on this
  problem, or more than ``HISTORY_FACTOR`` (2x) the largest earlier jump on that input.

The flag is written on the run's case row and the reading stays in the immutable report, but it
is not gate evidence (scheduler.case_map.local_minima, the estimator's case-bench minima) until
another run of the same SHA reads the same input within ``AGREE`` (10%) and one of the two ran
in a cold session (``reuse_session`` false). The next formal run of a SHA with an unconfirmed
flag runs cold on its own (scheduler.case_bench.run).
"""
from __future__ import annotations

import math
from typing import Any

FACTOR = 3.0
HISTORY_FACTOR = 2.0
AGREE = 0.10


def _positive(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def peak_bandwidth() -> float | None:
    """Configured device peak bandwidth in bytes per second, or None."""
    try:
        from cli.opsb_core.device_env import device
        value = float(device("peak_bandwidth_gbps"))
    except Exception:  # noqa: BLE001 - unconfigured, unparsable or CLI package absent
        return None
    return value * 1e9 if math.isfinite(value) and value > 0 else None


def _readings(doc: dict[str, Any]):
    for row in doc.get("cases") or []:
        if row.get("status") == "PASS" and _positive(row.get("min_us")) and isinstance(row.get("key"), str):
            yield row


def _cold(doc: dict[str, Any]) -> bool:
    return (doc.get("settings") or {}).get("reuse_session") is False


def unconfirmed(docs: list[dict[str, Any]]) -> dict[str, set[str]]:
    """``{run_id: case IDs}`` of flagged readings no other run of the same SHA has confirmed."""
    by_input: dict[tuple[str, str], list[tuple[str, bool, float]]] = {}
    for doc in docs:
        for row in _readings(doc):
            by_input.setdefault((str(doc.get("sha")), row["key"]), []).append(
                (str(doc.get("run_id")), _cold(doc), float(row["min_us"])))
    out: dict[str, set[str]] = {}
    for doc in docs:
        run, cold = str(doc.get("run_id")), _cold(doc)
        for row in _readings(doc):
            if not row.get("implausible"):
                continue
            value = float(row["min_us"])
            confirmed = any(other != run and (cold or other_cold) and abs(value - w) <= AGREE * min(value, w)
                            for other, other_cold, w in by_input[(str(doc.get("sha")), row["key"])])
            if not confirmed:
                out.setdefault(run, set()).add(row["id"])
    return out


def gate_minimum(doc: dict[str, Any], row: dict[str, Any], blocked: dict[str, set[str]]) -> bool:
    """Whether a reading may serve as gate evidence."""
    return row.get("id") not in blocked.get(str(doc.get("run_id")), set())


def history(reports: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Per input key: the best usable earlier reading and the largest earlier improvement ratio."""
    blocked = unconfirmed(reports)
    out: dict[str, dict[str, float]] = {}
    for doc in sorted(reports, key=lambda d: d.get("finished_at") or ""):
        if doc.get("timing_score_eligible") is not True:
            continue
        for row in _readings(doc):
            if not gate_minimum(doc, row, blocked):
                continue
            value = float(row["min_us"])
            seen = out.setdefault(row["key"], {"best": value, "max_jump": 1.0})
            if value < seen["best"]:
                seen["max_jump"] = max(seen["max_jump"], seen["best"] / value)
                seen["best"] = value
    return out


def check(report: dict[str, Any], rows: dict[str, dict[str, Any]], reports: list[dict[str, Any]],
          bandwidth: float | None = None, factor: float = FACTOR,
          history_factor: float = HISTORY_FACTOR) -> list[str]:
    """Flag this run's implausible readings in place (``row["implausible"]``); return their IDs."""
    earlier = history([d for d in reports if d.get("run_id") != report.get("run_id")])
    flagged = []
    for key in report.get("selected") or []:
        row = rows.get(key) or {}
        value = row.get("min_us")
        if row.get("status") != "PASS" or not _positive(value):
            continue
        reasons, detail = [], {}
        if bandwidth and type(row.get("bytes")) is int and row["bytes"] > 0:
            floor = row["bytes"] / bandwidth * 1e6
            detail["floor_us"] = round(floor, 3)
            if value < floor:
                reasons.append(f"below the bandwidth floor of {floor:.3g} us")
        seen = earlier.get(row.get("key"))
        if seen:
            jump = seen["best"] / value
            detail.update(previous_best_us=round(seen["best"], 3), jump=round(jump, 3))
            if jump > factor:
                reasons.append(f"{jump:.2f}x faster than the best earlier reading (limit {factor:g}x)")
            elif seen["max_jump"] > 1.0 and jump > history_factor * seen["max_jump"]:
                reasons.append(f"{jump:.2f}x jump, over {history_factor:g}x the largest earlier "
                               f"jump ({seen['max_jump']:.2f}x)")
        if reasons:
            row["implausible"] = {"reasons": reasons, **detail}
            flagged.append(key)
    return flagged


def pending(docs: list[dict[str, Any]]) -> bool:
    """Whether any run of this SHA still has an unconfirmed implausible reading."""
    return bool(unconfirmed(docs))
