"""Measured judge noise per problem (4.2.1): how far the same commit's official score moves.

The gate's noise floor was a constant 2.5 points while the same binary judged again moved by about
3 points (P19 measured a pooled sigma of 3.04 on the last contest). This module estimates that
sigma from the receipt journal:

* only ``Pass`` receipts with a positive official score count, grouped by commit;
* a commit's judgments are split where two are more than ``WINDOW_HOURS`` apart, because the
  board's tbest keeps tightening over days and that drift is not judge noise;
* deviations from each cluster's mean give a pooled variance with ``n - 1`` degrees of freedom
  per cluster.

A problem with fewer than ``MIN_DF`` degrees of freedom uses the contest-wide pooled value; with
neither, :func:`sigma_for` returns ``None`` and the gate keeps its configured floor. The journal is
read through ``receipts`` (deduplicated by ``submission_id``) and cached by file size and mtime.

Same-source verdict flips are the other face of the same noise (4.3 P24): one commit judged twice
with different verdict classes (``Pass`` then ``WA``, ``Pass`` then ``TLE``, ``TLE`` then ``Pass``).
:func:`verdict_flips` walks the journal per commit in submission order and reports every change, so
the platform can record it as judge noise and the rules view can show the count.
"""
from __future__ import annotations

import datetime
import math
import pathlib
from typing import Any

from . import receipts

WINDOW_HOURS = 12.0
MIN_DF = 3
#: Verdict classes of a judged receipt. Two receipts of one commit in different classes are a flip;
#: a change from one class to another and back counts twice, because each change is a separate event.
VERDICT_CLASS = {
    "Pass": "pass",
    "Accepted": "pass",
    "Wrong Answer": "wa",
    "Runtime Error": "re",
    "Time Limit Exceeded": "tle",
    "Compile Error": "ce",
    "Memory Limit Exceeded": "mle",
    "System Error": "se",
    "Fail": "fail",
}
_CACHE: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}


def _epoch(row: dict[str, Any]) -> float | None:
    raw = str(row.get("submitted_at") or row.get("finished_at") or "")
    if not raw:
        return None
    try:
        when = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return when.timestamp()


def clusters(points: list[tuple[float, float]], window_hours: float = WINDOW_HOURS) -> list[list[float]]:
    """Scores of one commit, split where consecutive judgments are more than the window apart."""
    out: list[list[float]] = []
    last = None
    for when, score in sorted(points):
        if last is None or when - last > window_hours * 3600.0:
            out.append([])
        out[-1].append(score)
        last = when
    return out


def _pooled(groups: list[list[float]]) -> dict[str, Any]:
    squares, df = 0.0, 0
    for scores in groups:
        if len(scores) < 2:
            continue
        mean = sum(scores) / len(scores)
        squares += sum((s - mean) ** 2 for s in scores)
        df += len(scores) - 1
    return {"sigma": round(math.sqrt(squares / df), 3) if df else None, "df": df}


def estimate(workspace: pathlib.Path | str) -> dict[str, Any]:
    """``{"problems": {title: {sigma, df}}, "pooled": {sigma, df}}`` from the receipt journal."""
    path = receipts.ledger_path(pathlib.Path(workspace))
    try:
        stat = path.stat()
        key = (stat.st_size, stat.st_mtime_ns)
    except OSError:
        return {"problems": {}, "pooled": {"sigma": None, "df": 0}}
    cached = _CACHE.get(str(path))
    if cached and cached[0] == key:
        return cached[1]
    by_commit: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for row in receipts.canonical(receipts.read_rows(path)):
        if str(row.get("status") or "") != "Pass":
            continue
        score = receipts.official_score(row)
        when = _epoch(row)
        sha = str(row.get("canonical_sha") or row.get("sha") or "")
        title = str(row.get("title") or "")
        if score is None or score <= 0 or when is None or not sha or not title:
            continue
        by_commit.setdefault((title, sha[:12]), []).append((when, float(score)))
    per_problem: dict[str, list[list[float]]] = {}
    for (title, _sha), points in by_commit.items():
        per_problem.setdefault(title, []).extend(clusters(points))
    doc = {
        "problems": {title: _pooled(groups) for title, groups in per_problem.items()},
        "pooled": _pooled([g for groups in per_problem.values() for g in groups]),
    }
    _CACHE[str(path)] = (key, doc)
    return doc


def sigma_for(workspace: pathlib.Path | str, title: str) -> dict[str, Any] | None:
    """``{sigma, df, scope}`` for one problem (``scope`` is ``problem`` or ``contest``), or ``None``."""
    doc = estimate(workspace)
    own = doc["problems"].get(title) or {}
    if own.get("sigma") is not None and own.get("df", 0) >= MIN_DF:
        return {"sigma": own["sigma"], "df": own["df"], "scope": "problem"}
    pooled = doc["pooled"]
    if pooled.get("sigma") is not None and pooled.get("df", 0) >= MIN_DF:
        return {"sigma": pooled["sigma"], "df": pooled["df"], "scope": "contest"}
    return None


def verdict_flips(rows: list[dict[str, Any]], title: str | None = None) -> list[dict[str, Any]]:
    """Every same-source verdict flip in canonical receipt ``rows`` (4.3 P24).

    Receipts of one commit are walked in submission order; a receipt whose verdict class differs
    from the previous judged receipt of the same commit is one flip. Rows without a judge verdict
    (``rejected``/``skipped``/``sync_error``) and rows without a commit are ignored.
    """
    groups: dict[tuple[str, str], list[tuple[float, dict[str, Any]]]] = {}
    for row in rows:
        cls = VERDICT_CLASS.get(str(row.get("status") or ""))
        when = _epoch(row)
        sha = str(row.get("canonical_sha") or row.get("sha") or "")
        name = str(row.get("title") or "")
        if cls is None or when is None or not sha or not name or (title and name != title):
            continue
        groups.setdefault((name, sha[:12]), []).append((when, row))
    out: list[dict[str, Any]] = []
    for (name, _sha), items in sorted(groups.items()):
        items.sort(key=lambda item: item[0])
        previous: dict[str, Any] | None = None
        for _when, row in items:
            if previous is not None and VERDICT_CLASS[str(row["status"])] != VERDICT_CLASS[str(previous["status"])]:
                out.append({
                    "title": name,
                    "submission_id": str(row.get("submission_id") or ""),
                    "previous_submission_id": str(previous.get("submission_id") or ""),
                    "sha": str(row.get("canonical_sha") or row.get("sha") or ""),
                    "previous_status": str(previous.get("status") or ""),
                    "status": str(row.get("status") or ""),
                    "at": str(row.get("submitted_at") or row.get("finished_at") or ""),
                })
            previous = row
    out.sort(key=lambda flip: (flip["title"], flip["at"]))
    return out


def flip_count(rows: list[dict[str, Any]], title: str | None = None) -> int:
    """How many times a problem's judged commit changed verdict class (``rules`` view)."""
    return len(verdict_flips(rows, title))
