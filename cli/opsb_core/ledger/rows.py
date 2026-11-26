"""Pure builders of platform-written entries: no file or git access, shared with the replay.

Each builder takes plain dicts (a test-run record, a receipt row, a gate estimate) and returns
``(entry type, fields, ekey)`` for ``Ledger.append``; ``ekey`` is the evidence key that makes the
write idempotent.

Constants of the judgements (documented in docs/plan-4.1-4.2/reports/P05-replay.md):

``SPEEDUP_BETTER`` / ``SPEEDUP_WORSE``  geomean warm speedup against the baseline SHA that makes an
                    all-pass run ``better`` (>= 1.02) or ``worse`` (<= 0.98); between is ``neutral``;
``NOISE_MARGIN``    a run whose timer read slower than its own best (``timing.noisy``) is ``noise``
                    unless the speedup is further than this from 1;
``CONFIRM_SIGMAS``  a hypothesis is ``confirmed`` when the official gain is at least ``mu - 1 sigma``
                    of the gate's prediction (inside mu +/- sigma or better); ``refuted`` when the gain
                    is <= 0 or the verdict is WA/RE/TLE/CE; ``inconclusive`` otherwise.
"""
from __future__ import annotations

import math
import re
from typing import Any

SPEEDUP_BETTER = 1.02
SPEEDUP_WORSE = 0.98
NOISE_MARGIN = 0.10
CONFIRM_SIGMAS = 1.0
COMPILE_ERROR = re.compile(r"compil|build|nvcc|ccec|cmake|linker|syntax|undefined reference", re.I)
FAIL_ORDER = ("WA", "RE", "ERROR", "TLE")
FAIL_OUTCOME = {"WA": "WA", "RE": "RE", "ERROR": "RE", "TLE": "TLE"}
JUDGE_OUTCOME = {"Wrong Answer": "WA", "Runtime Error": "RE", "Time Limit Exceeded": "TLE", "Compile Error": "compile_fail"}


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _round(value: Any, digits: int = 3) -> Any:
    n = _num(value)
    return None if n is None else round(n, digits)


def _geomean(values: list[float]) -> float | None:
    values = [v for v in values if v > 0]
    return math.exp(sum(math.log(v) for v in values) / len(values)) if values else None


def _digest(record: dict[str, Any]) -> dict[str, Any]:
    return record["digest"] if isinstance(record.get("digest"), dict) else {}


def outcome_of(record: dict[str, Any]) -> str | None:
    """The experiment outcome of a settled test run; None for a run that says nothing (cancelled, build ok)."""
    status = str(record.get("status") or "")
    if status == "failed":  # a crashed command is a platform fault unless it says it did not compile
        text = str(record.get("error") or "")
        return "compile_fail" if record.get("level") == "build" or COMPILE_ERROR.search(text) else None
    if status != "finished" or not _digest(record):
        return None
    counts = (_digest(record).get("summary") or {}).get("counts") or {}
    for key in FAIL_ORDER:
        if counts.get(key):
            return FAIL_OUTCOME[key]
    speedup = _num((_digest(record).get("baseline") or {}).get("geomean_speedup"))
    noisy = bool((_digest(record).get("timing") or {}).get("noisy"))
    if speedup is None:
        return "noise" if noisy else "neutral"
    if noisy and abs(speedup - 1.0) <= NOISE_MARGIN:
        return "noise"
    return "better" if speedup >= SPEEDUP_BETTER else "worse" if speedup <= SPEEDUP_WORSE else "neutral"


def _time_delta_pct(speedup: Any) -> float | None:
    n = _num(speedup)
    return round((1.0 / n - 1.0) * 100.0, 1) if n and n > 0 else None


def test_entries(record: dict[str, Any], *, diff_hash: str = "", subject: str = "") -> list[tuple[str, dict[str, Any], str]]:
    """The ``measurement`` and ``experiment`` entries of one settled test run (empty when it is not one)."""
    rid, sha = str(record.get("request_id") or ""), str(record.get("sha") or "")
    outcome = outcome_of(record)
    if not rid or not sha or outcome is None:
        return []
    digest, level = _digest(record), str(record.get("level") or "")
    summary = digest.get("summary") or {}
    timing = digest.get("timing") or {}
    baseline = digest.get("baseline") or {}
    cases = {k: c for k, c in (timing.get("cases") or {}).items() if isinstance(c, dict)}
    evidence = [f"test_run:{rid}"]
    out: list[tuple[str, dict[str, Any], str]] = []
    mins = {k: _round(c.get("min_us"), 2) for k, c in cases.items() if _num(c.get("min_us"))}
    if mins:
        drift = _num(timing.get("drift"))
        out.append(("measurement", {
            "key": "test.min_us", "value": _round(_geomean([float(v) for v in mins.values()]), 2), "unit": "us",
            "claim": f"{level} test {sha[:7]}: {summary.get('passed')}/{summary.get('total')} pass, "
                     f"{len(mins)} timed cases", "sha": sha, "test_run_id": rid, "test_level": level,
            "suite_version": digest.get("suite_version"), "n_repeats": timing.get("repeats"),
            "per_case_us": mins, "median_us": {k: _round(c.get("median_us"), 2) for k, c in cases.items()
                                               if k in mins and _num(c.get("median_us"))},
            "noise_band": {"drift": drift, "noisy": bool(timing.get("noisy"))} if drift is not None else None,
            "evidence": evidence}, f"test:{rid}:measurement"))
    speedup = _num(baseline.get("geomean_speedup"))
    failures = [f for f in digest.get("failures") or [] if isinstance(f, dict)]
    actual = {"level": level, "passed": summary.get("passed"), "total": summary.get("total"),
              "counts": {k: v for k, v in (summary.get("counts") or {}).items() if k != "PASS" and v},
              "speedup": _round(speedup, 4), "noisy": bool(timing.get("noisy")) or None}
    delta = ({"geomean_pct": _time_delta_pct(speedup),
              "cases": {k: _time_delta_pct(v) for k, v in (baseline.get("cases") or {}).items()}}
             if speedup is not None or baseline.get("cases") else None)
    claim = f"{sha[:7]} {level}: {outcome}" + (f" ({speedup:.3f}x vs {baseline.get('sha')})" if speedup else "")
    why = ""
    if failures:
        why = f"case {failures[0].get('id')} {failures[0].get('status')}: {failures[0].get('detail') or ''}"
    elif outcome in ("RE", "compile_fail"):
        why = str(record.get("error") or "").strip().splitlines()[-1] if record.get("error") else ""
    out.append(("experiment", {
        "claim": claim, "why": why, "sha": sha, "base_sha": baseline.get("sha"), "outcome": outcome,
        "change_summary": subject, "diff_hash": diff_hash, "test_run_id": rid, "per_case_us": mins or None,
        "delta_vs_baseline": delta, "actual_outcome": {k: v for k, v in actual.items() if v not in (None, {}, "")},
        "scope": {"cases": [str(f.get("id")) for f in failures]} if failures else None,
        "submitted": False, "evidence": evidence}, f"test:{rid}:experiment"))
    return out


def gate_hypothesis(sha: str, *, note: str = "", subject: str = "", mu: Any = None, sigma: Any = None,
                    enqueued: bool = False) -> tuple[str, dict[str, Any], str]:
    """The hypothesis a submission request implies when the worker named none (``source=platform``)."""
    effect = ({"delta_mu": _round(mu, 2), "delta_sigma": _round(sigma, 2)}
              if _num(mu) is not None and _num(sigma) is not None else None)
    return "hypothesis", {
        "claim": note.strip() or subject.strip() or f"candidate {sha[:7]}",
        "why": "created by the platform from the submission request and the gate's estimate",
        "sha": sha, "status": "queued" if enqueued else "open", "priority": 3, "confidence": "inferred",
        "predicted_effect": effect}, f"gate:{sha}"


def receipt_entry(row: dict[str, Any], *, gain: float | None = None, prior: float | None = None) -> tuple[str, dict[str, Any], str]:
    """The ``measurement`` of a judged receipt (``row`` is canonical: ``scheduler.receipts.normalize``)."""
    sid, sha, status = str(row.get("submission_id") or ""), str(row.get("sha") or ""), str(row.get("status") or "")
    score = _num(row.get("official_score"))
    times = {k: _round(v, 2) for k, v in (row.get("time_by_case") or {}).items() if _num(v)}
    tbest = {k: _round(v, 2) for k, v in (row.get("tbest_by_case") or {}).items() if _num(v)}
    gain_text = f", {gain:+.2f} vs same-epoch best {prior:.2f}" if gain is not None and prior is not None else ""
    return "measurement", {
        "key": "official.score", "value": score, "unit": "pts", "receipt_id": sid, "sha": sha,
        "claim": f"official {status} {score} for {sha[:7]}{gain_text}",
        "per_case_us": times or None, "tbest_us": tbest or None, "evidence": [f"receipt:{sid}"]}, f"receipt:{sid}"


def judge(row: dict[str, Any], gain: float | None, predicted_effect: Any) -> tuple[str, str] | None:
    """``(status, note)`` of the hypothesis behind a judged SHA; None while the gain is unknown."""
    status = str(row.get("status") or "")
    if status not in ("Pass", "Accepted"):
        return "refuted", f"official verdict {status}"
    if gain is None:
        return None
    if gain <= 0:
        return "refuted", f"official gain {gain:+.2f} <= 0"
    pred = predicted_effect if isinstance(predicted_effect, dict) else {}
    mu, sigma = _num(pred.get("delta_mu")), _num(pred.get("delta_sigma"))
    if mu is None or sigma is None:
        return "inconclusive", f"official gain {gain:+.2f}; no numeric prediction"
    if gain >= mu - CONFIRM_SIGMAS * sigma:
        return "confirmed", f"official gain {gain:+.2f} within predicted {mu:+.2f}±{sigma:.2f} or better"
    return "inconclusive", f"official gain {gain:+.2f} below predicted {mu:+.2f}±{sigma:.2f}"


def receipt_outcome(row: dict[str, Any]) -> str | None:
    """The experiment outcome a judge verdict implies (WA/RE/TLE/compile_fail); None for a pass."""
    return JUDGE_OUTCOME.get(str(row.get("status") or ""))
