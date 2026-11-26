"""The one reader for judge receipts in ``results/submit-queue-outcome.jsonl`` (4.1 #8).

Several writers append receipt rows (the submit poll, the sync reconciler, the judge-history
reconcile, the backfill below), and they record the score in different places. A Pass that
refreshed the team's best carries ``score`` (read from the ranking row). A Pass that did not
carries ``score=None``; its replayed score and all per-case times live in ``timing_score``.
WA/RE/TLE rows carry ``score=0`` and, before 4.1, no per-case detail.

``normalize`` adds one canonical shape to a row without removing any legacy field, so old
readers and the GUI keep working:

- ``official_score``: judged score; 0.0 for a judged non-Pass; None when no source has it.
- ``beat_board``: True when the score came from the ranking row, False for a Pass that only
  has a replayed score, None when unknown.
- ``case_results``: ``[{testcase_id, status, time_us, tbest_us}]`` in the judge's order.
- ``time_by_case`` / ``tbest_by_case``: ``{testcase_id: us}`` whenever known.
- ``tbest_observed_at``: when the tbest values were read (they drift; AGENTS.md section 4).
- ``receipt_schema``: 1.

Rows are deduplicated by ``submission_id``. ``score_rank`` is the score part of the
preference order that every dedupe uses, so a normalized row wins over the row it was built
from. This module only uses the standard library: CLI writers import it lazily.
"""
from __future__ import annotations

import json
import math
import pathlib
from typing import Any

SCHEMA = 1
FORMULA = "judge_log1p5_mean"
BACKFILL_SOURCE = "receipt-backfill"
JUDGED = frozenset({
    "Pass", "Accepted", "Fail", "Wrong Answer", "Compile Error", "Runtime Error",
    "Time Limit Exceeded", "Memory Limit Exceeded", "System Error",
})
NOT_JUDGED = frozenset({"rejected", "skipped", "duplicate", "sync_error"})


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def judged(row: dict[str, Any]) -> bool:
    """A judge verdict exists for this row (not a rejection, skip or interrupted poll)."""
    return bool(row.get("submission_id")) and str(row.get("status") or "") in JUDGED


def is_pass(row: dict[str, Any]) -> bool:
    return str(row.get("status") or "") in ("Pass", "Accepted")


def is_normalized(row: dict[str, Any]) -> bool:
    return row.get("receipt_schema") == SCHEMA


def _timing(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("timing_score")
    return value if isinstance(value, dict) else {}


def official_score(row: dict[str, Any]) -> float | None:
    """The judged score: ``score`` on a Pass, else ``timing_score.score``; 0 for WA/RE/TLE/CE."""
    status = str(row.get("status") or "")
    if status not in JUDGED:
        return None
    if not is_pass(row):
        return 0.0
    for value in (row.get("score"), _timing(row).get("score"), row.get("official_score")):
        if _num(value) is not None:
            return _num(value)
    return None


def beat_board(row: dict[str, Any]) -> bool | None:
    """Whether this receipt became the team's board entry when it was judged."""
    if not judged(row):
        return None
    if not is_pass(row):
        return False
    if _num(row.get("score")) is not None:
        return True
    if _num(_timing(row).get("score")) is not None:
        return False
    return row.get("beat_board") if isinstance(row.get("beat_board"), bool) else None


def _clean_results(items: Any) -> list[dict[str, Any]]:
    out = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("testcase_id"):
            out.append({"testcase_id": str(item["testcase_id"]), "status": item.get("status"),
                        "time_us": _num(item.get("time_us")), "tbest_us": _num(item.get("tbest_us"))})
    return out


def case_results(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-case verdicts in judge order: stored ``case_results``, else rebuilt from ``timing_score``."""
    stored = _clean_results(row.get("case_results"))
    if stored:
        return stored
    timing = _timing(row)
    times, tbest = timing.get("time_by_case"), timing.get("tbest_by_case")
    if not isinstance(times, dict) or not times:
        return []
    tbest = tbest if isinstance(tbest, dict) else {}
    # A replayed timing score exists only when every case passed.
    return [{"testcase_id": str(k), "status": "Pass", "time_us": _num(v), "tbest_us": _num(tbest.get(k))}
            for k, v in times.items()]


def _by_case(row: dict[str, Any], field: str, result_key: str) -> dict[str, float | None]:
    for source in (row.get(field), _timing(row).get(field)):
        if isinstance(source, dict) and source:
            return {str(k): _num(v) for k, v in source.items()}
    return {r["testcase_id"]: r[result_key] for r in _clean_results(row.get("case_results"))}


def case_times(row: dict[str, Any]) -> dict[str, float | None]:
    """``{testcase_id: judged time in us}``; None marks a case the judge did not time."""
    return _by_case(row, "time_by_case", "time_us")


def case_tbest(row: dict[str, Any]) -> dict[str, float | None]:
    """``{testcase_id: site-wide best time in us}`` as observed with this receipt."""
    return _by_case(row, "tbest_by_case", "tbest_us")


def public_times(row: dict[str, Any]) -> tuple[list[float], list[float]]:
    """Legacy positional public lists. They skip untimed cases, so positions can shift."""
    return ([v for v in (_num(x) for x in row.get("public_times_us") or []) if v is not None],
            [v for v in (_num(x) for x in row.get("public_tbest_us") or []) if v is not None])


def tbest_observed_at(row: dict[str, Any]) -> str | None:
    return row.get("tbest_observed_at") or row.get("reconciled_at") or row.get("finished_at")


def normalize(row: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``row`` with the canonical receipt fields; legacy fields are kept as they are."""
    out = dict(row)
    out["official_score"] = official_score(row)
    out["beat_board"] = beat_board(row)
    results, times, tbest = case_results(row), case_times(row), case_tbest(row)
    if results:
        out["case_results"] = results
    if times:
        out["time_by_case"] = times
    if tbest:
        out["tbest_by_case"] = tbest
        out["tbest_observed_at"] = tbest_observed_at(row)
    out["receipt_schema"] = SCHEMA
    return out


def effective_source(row: dict[str, Any]) -> str:
    """The writer of the row a backfill was built from; a backfill does not upgrade its origin."""
    if row.get("source") == BACKFILL_SOURCE:
        return str(row.get("backfilled_from_source") or "")
    return str(row.get("source") or "")


def score_rank(row: dict[str, Any]) -> tuple[int, int, int]:
    """(score known, per-case detail known, normalized): the tail of every dedupe key."""
    if official_score(row) is not None:
        scored = 2
    elif row.get("score") is not None or row.get("best_score_bound"):
        scored = 1
    else:
        scored = 0
    return scored, int(bool(case_times(row))), int(is_normalized(row))


def rank(row: dict[str, Any]) -> tuple[int, int, int, int, int]:
    status = str(row.get("status") or "")
    verdict = int(status in JUDGED)
    return (verdict, int(effective_source(row) != "judge-reconcile"), *score_rank(row))


def canonical(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per ``submission_id`` (best by ``rank``, later rows win ties); others unchanged."""
    best: dict[str, tuple[tuple[int, ...], int]] = {}
    for idx, row in enumerate(rows):
        sid = str(row.get("submission_id") or "").strip()
        if sid:
            key = rank(row)
            if sid not in best or key >= best[sid][0]:
                best[sid] = (key, idx)
    keep = {idx for _, idx in best.values()}
    return [row for idx, row in enumerate(rows)
            if idx in keep or not str(row.get("submission_id") or "").strip()]


_ROWS: dict[str, tuple[tuple[int, int], list[dict[str, Any]]]] = {}


def read_rows(path: pathlib.Path) -> list[dict[str, Any]]:
    """Every JSON object line of the journal (parsed once per file version; callers get copies)."""
    try:
        stat = path.stat()
        sig = (stat.st_mtime_ns, stat.st_size)
        hit = _ROWS.get(str(path))
        if hit is None or hit[0] != sig:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            rows = []
            for line in lines:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
            _ROWS[str(path)] = hit = (sig, rows)
    except OSError:
        return []
    return [dict(row) for row in hit[1]]


def ledger_path(workspace: pathlib.Path) -> pathlib.Path:
    return workspace / "results" / "submit-queue-outcome.jsonl"


def load(workspace: pathlib.Path, title: str | None = None) -> list[dict[str, Any]]:
    """Canonical receipt rows of the workspace (optionally of one problem), in file order."""
    rows = canonical(read_rows(ledger_path(workspace)))
    return [r for r in rows if title is None or r.get("title") == title]


def case_points(time_us: float | None, tbest_us: float | None) -> float:
    """Judge points of one case: 100 / (1 + ln(t / tbest) / ln 1.5); 100 at or below tbest."""
    if tbest_us is None or tbest_us <= 0 or time_us is None or time_us <= 0:
        return 0.0
    return 100.0 / (1.0 + math.log(max(time_us, tbest_us) / tbest_us) / math.log(1.5))


def judge_score(times: dict[str, float | None], tbest: dict[str, float | None]) -> float | None:
    """Mean points over every roster case; a case without tbest scores 0 (judge_score_fetch.replay)."""
    if not tbest:
        return None
    points = []
    for key, base in tbest.items():
        if base is None:
            continue
        if times.get(key) is None:
            return None
        points.append(case_points(times[key], base))
    return round(math.fsum(points) / len(tbest), 2) if points else None
