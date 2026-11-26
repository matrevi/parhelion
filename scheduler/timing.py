"""Timing sources of case-bench reports and how comparisons pick one (4.1 route #1, #2).

Since report schema 2 every timed case says which timer its ``min_us`` / ``median_us`` come
from: ``profiler`` (device kernel task time) or ``event`` (ACL events around the launch). The
event samples are recorded in every run, the profiler samples when the profiler attributed a
task to every timed execution. Older reports are event-only.

A comparison never mixes sources: both sides read the same timer, the profiler when it covers
every case the comparison would otherwise use, else the event timer.
"""
from __future__ import annotations

import math
import statistics
from typing import Any, Iterable

SOURCES = ("profiler", "event")


def _positive(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def source_minima(row: dict[str, Any]) -> None:
    """Add ``<source>_min_us`` / ``<source>_median_us`` for every source with samples."""
    for source in SOURCES:
        values = [v for v in row.get(f"{source}_us") or [] if _positive(v)]
        if source == "event" and not values and (row.get("timing_source") or "event") == "event":
            values = [v for v in row.get("device_us") or [] if _positive(v)]
        if values:
            row[f"{source}_min_us"] = min(values)
            row[f"{source}_median_us"] = statistics.median(values)


def run_source(rows: dict[str, dict[str, Any]], report: dict[str, Any]) -> str:
    """Report-level label: ``profiler``, ``event_warm`` (event only, the 4.0 label) or ``mixed``."""
    used = {rows[k].get("timing_source") or "event" for k in report.get("timed", [])
            if k in rows and rows[k].get("min_us")}
    if used == {"profiler"}:
        return "profiler"
    return "mixed" if len(used) > 1 else "event_warm"


def case_value(case: dict[str, Any], source: str, stat: str = "min") -> float | None:
    """A case's warm minimum (or median) in one timing source; None when that source has none."""
    value = case.get(f"{source}_{stat}_us")
    if value is None and source == "event" and (case.get("timing_source") or "event") == "event":
        value = case.get(f"{stat}_us")  # schema 1 reports: the event timer only
    return float(value) if _positive(value) else None


def profiler_trusted(doc: dict[str, Any]) -> bool:
    """False for runs whose profiler tasks were assigned by order rather than by time window.

    The first P10 build fell back to splitting tasks evenly across executions when one window
    held no task; with a kernel-less case that shifted every later case's reading (P10-timing.md).
    Those runs say ``(order)`` in their profiler notes; their event timings stay valid.
    """
    for entry in (doc.get("device_batch") or {}).get("profiler") or []:
        if any("(order)" in str(note) for note in (entry or {}).get("notes") or []):
            return False
    return True


def doc_value(doc: dict[str, Any], case: dict[str, Any], source: str) -> float | None:
    """``case_value`` for a case of ``doc``, ignoring profiler readings of untrusted runs."""
    if source == "profiler" and not profiler_trusted(doc):
        return None
    return case_value(case, source)


def best_minima(docs: Iterable[dict[str, Any]], source: str, *, official_only: bool = True,
                passed_only: bool = False) -> dict[str, float]:
    """Best warm minimum per case over ``docs`` in one source."""
    out: dict[str, float] = {}
    for doc in docs:
        if source == "profiler" and not profiler_trusted(doc):
            continue
        for case in doc.get("cases") or []:
            if official_only and case.get("source") != "official":
                continue
            if passed_only and case.get("status") not in ("PASS", None):
                continue
            value = case_value(case, source)
            if value is not None and case.get("id"):
                out[case["id"]] = min(out.get(case["id"], value), value)
    return out


def common_source(cand: dict[str, dict[str, float]], base: dict[str, dict[str, float]],
                  keys: Iterable[str] | None = None) -> tuple[str, dict[str, float], dict[str, float]] | None:
    """The source both sides share on the most cases; the profiler wins when it covers as many.

    ``cand`` / ``base`` map a source to ``{case: minimum}``; ``keys`` restricts the cases that
    count (for example mapped judge cases). Event minima exist for every timed case, so the
    profiler is chosen only when its coverage is complete.
    """
    wanted = set(keys) if keys is not None else None
    best = None
    for source in SOURCES:
        a, b = cand.get(source) or {}, base.get(source) or {}
        common = set(a) & set(b) if wanted is None else set(a) & set(b) & wanted
        if common and (best is None or len(common) > best[0]):
            best = (len(common), source, a, b)
    return None if best is None else (best[1], best[2], best[3])
