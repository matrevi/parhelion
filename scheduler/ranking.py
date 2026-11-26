"""Measured judge jitter, the two prediction tiers and the saturation transform (4.3 P29).

Three ideas, one module:

* **sigma from measurement.** The estimator's same-source jitter was a constant
  (:data:`SIGMA_FALLBACK`, ``estimator.SIGMA_HIDDEN_JITTER`` = 2.70 pts).
  ``judge_noise.sigma_for`` already pools the repeated judgments of one commit per problem, falls
  back to the contest-wide pooled value when a problem has fewer than ``MIN_DF`` degrees of
  freedom, and returns ``None`` when neither qualifies -- the constant is then the last resort.
* **two tiers.** A shot is a *same-generation retest* (the same SHA, or a change that never touches
  kernel source) or a *cross-generation change*. A retest is already unbiased, so its prediction is
  left alone; a cross-generation change over-predicts (about 3.7x on the last contest, R3 section
  1.5), so only that tier is transformed.
* **saturated, then shrunk.** The local speedup never multiplies the score. The judge scores a case
  as ``100 / (1 + ln(t / tbest) / ln 1.5)``, so the transform takes this problem's real per-case
  distribution (from a judged receipt), sets its level to the board score the prediction anchors on,
  divides every case time by the predicted speedup, and rescores with that same formula. Gains
  therefore saturate case by case instead of scaling linearly. The saturated gain is then multiplied
  by a shrinkage coefficient fitted on this problem's own earlier receipts -- strictly the ones
  judged before ``as_of``.

The tier decision runs ``git diff --numstat`` through :mod:`cli.opsb_core.ledger.gitref`, whose
``git_out`` is bounded by a five second timeout, and treats **any** failure as a cross-generation
change: "I could not measure the change" is never "nothing changed".
"""
from __future__ import annotations

import datetime
import math
import pathlib
from typing import Any

from . import receipts

#: 4.2.1's constant same-source re-judge jitter, the last resort when nothing is measured. Kept
#: equal to ``estimator.SIGMA_HIDDEN_JITTER`` (a test pins the two together).
SIGMA_FALLBACK = 2.70
SAME_GEN = "same_gen"
CROSS_GEN = "cross_gen"
#: "Kernel source" is the set the estimator's code fingerprint hashes: the two operator directories
#: and the compiled-language suffixes under them. A doc, script or test file never changes the
#: binary, so it never makes a change cross-generation.
KERNEL_DIRS = ("op_host", "op_kernel")
KERNEL_SUFFIXES = (".h", ".hpp", ".c", ".cc", ".cpp", ".cxx")
#: A speedup outside this range is not a measurement, it is a parse accident.
SPEEDUP_CLAMP = (0.05, 20.0)
#: Prior shrink of a cross-generation gain: 1 / 3.7, the measured over-prediction (R3 section 1.5).
SHRINK_PRIOR = round(1.0 / 3.7, 4)
#: The prior counts as this many fitted pairs, so one lucky pair cannot set the coefficient.
SHRINK_PRIOR_WEIGHT = 3.0
#: Ridge energy (points^2) that keeps a pair predicting ~nothing from setting the slope.
SLOPE_RIDGE = 1.0
#: Pairs a problem needs before its own slope is used; one pair is not a fit (cf. ``judge_noise``).
MIN_PAIRS = 2
LN_BASE = math.log(1.5)
#: Log-ratio shifts searched when a distribution's level is set to the anchor score.
SHIFT_BOUNDS = (-4.0, 4.0)
SHIFT_STEPS = 48


def jitter_sigma(workspace: pathlib.Path | str, title: str) -> dict[str, Any]:
    """``{sigma, df, source}`` for one problem: measured, contest-pooled, else the constant."""
    try:
        from . import judge_noise

        noise = judge_noise.sigma_for(workspace, title)
    except Exception:  # noqa: BLE001 - a missing measurement only falls back to the constant
        noise = None
    if noise and noise.get("sigma"):
        return {"sigma": float(noise["sigma"]), "df": noise.get("df"),
                "source": f"judge_noise:{noise.get('scope')}"}
    return {"sigma": SIGMA_FALLBACK, "df": 0, "source": "constant"}


def kernel_source(rel: str) -> bool:
    """Whether one changed path is kernel source (the only change that makes a new generation)."""
    parts = pathlib.PurePosixPath(str(rel)).parts
    if not parts or parts[0] not in KERNEL_DIRS:
        return False
    return str(rel).lower().endswith(KERNEL_SUFFIXES)


def _numstat(repo: pathlib.Path, baseline: str, sha: str) -> list[tuple[int, int, str]] | None:
    """``git diff --numstat`` rows, or ``None`` when the diff cannot be measured."""
    from cli.opsb_core.ledger.gitref import git_out

    out = git_out(repo, "diff", "--numstat", "--no-color", "--no-renames", baseline, sha)
    if out is None:
        return None
    rows: list[tuple[int, int, str]] = []
    for line in out.splitlines():
        fields = line.split("\t")
        if len(fields) < 3 or not fields[2].strip():
            continue
        try:
            added, removed = int(fields[0]), int(fields[1])
        except ValueError:  # a binary file reports "-"
            added = removed = 0
        rows.append((added, removed, fields[2].strip()))
    return rows


def classify(repo: pathlib.Path | str | None, sha: str, baseline: str | None) -> dict[str, Any]:
    """``{tier, reason, files, added, removed, kernel_files}``: one shot against its own baseline."""
    sha, baseline = str(sha or "").lower(), str(baseline or "").lower()
    blank = {"files": 0, "added": 0, "removed": 0, "kernel_files": []}
    if baseline and sha == baseline:
        return {"tier": SAME_GEN, "reason": "same SHA", **blank}
    if not baseline:
        return {"tier": CROSS_GEN, "reason": "no baseline to diff against", **blank}
    rows = _numstat(pathlib.Path(repo), baseline, sha) if repo and sha else None
    if rows is None:
        return {"tier": CROSS_GEN, "reason": "diff unavailable (repository or commit missing, or timed out)",
                **blank}
    if not rows:
        return {"tier": SAME_GEN, "reason": "identical trees", **blank}
    kernel = sorted(rel for _, _, rel in rows if kernel_source(rel))
    return {"tier": CROSS_GEN if kernel else SAME_GEN,
            "reason": (f"{len(kernel)} kernel source file(s) changed" if kernel
                       else f"{len(rows)} file(s) changed, none of them kernel source"),
            "files": len(rows), "added": sum(a for a, _, _ in rows),
            "removed": sum(r for _, r, _ in rows), "kernel_files": kernel[:8]}


def case_doc(row: dict[str, Any]) -> dict[str, dict[str, float]] | None:
    """``{times, tbest}`` of one receipt, keyed case by case; public cases first, one currency only."""
    times, tbest = row.get("public_times_us"), row.get("public_tbest_us")
    if isinstance(times, list) and isinstance(tbest, list) and min(len(times), len(tbest)) >= 2:
        pairs = [(times[i], tbest[i]) for i in range(min(len(times), len(tbest)))]
    elif isinstance(row.get("time_by_case"), dict) and isinstance(row.get("tbest_by_case"), dict):
        pairs = [(row["time_by_case"].get(k), row["tbest_by_case"][k]) for k in
                 sorted(set(row["time_by_case"]) & set(row["tbest_by_case"]))]
    else:
        pairs = [(c.get("time_us"), c.get("tbest_us")) for c in receipts.case_results(row)]
    out_t: dict[str, float] = {}
    out_b: dict[str, float] = {}
    for time_us, best_us in pairs:
        if not all(isinstance(v, (int, float)) and float(v) > 0 for v in (time_us, best_us)):
            continue
        out_t[str(len(out_t))], out_b[str(len(out_t))] = float(time_us), float(best_us)
    return {"times": out_t, "tbest": out_b} if len(out_t) >= 2 else None


def judged_doc(workspace: pathlib.Path | str, title: str, sha: str | None = None,
               as_of: datetime.datetime | None = None) -> dict[str, Any] | None:
    """Newest receipt before ``as_of`` (of one SHA, or of any SHA when ``sha`` is None) with times."""
    from .estimator import _parse_iso

    best: tuple[datetime.datetime, dict[str, Any]] | None = None
    wanted = str(sha or "").lower()
    for row in receipts.load(pathlib.Path(workspace), title):
        if not receipts.judged(row):
            continue
        csha = str(row.get("canonical_sha") or row.get("sha") or "").lower()
        if wanted and not csha.startswith(wanted[:40]) and not wanted.startswith(csha[:40]):
            continue
        when = _parse_iso(row.get("submitted_at") or row.get("finished_at") or row.get("queued_at"))
        if when is None or (as_of is not None and when >= as_of):
            continue
        doc = case_doc(row)
        if doc is not None and (best is None or when > best[0]):
            best = (when, doc)
    if best is None:
        return None
    return {**best[1], "at": best[0].isoformat(), "score": round(_score(best[1]["times"], best[1]["tbest"]), 2)}


def _score(times: dict[str, float], tbest: dict[str, float], shift: float = 0.0) -> float:
    """The judge's mean points over these cases after shifting every log time ratio by ``shift``."""
    points = [receipts.case_points(max(1.0, math.exp(math.log(times[k] / tbest[k]) + shift)), 1.0)
              for k in times if k in tbest and tbest[k] > 0 and times[k] > 0]
    return math.fsum(points) / len(points) if points else 0.0


def level_shift(doc: dict[str, dict[str, float]], target: float) -> float:
    """The log-time shift that makes this distribution score ``target`` (``0`` kept inside bounds)."""
    if target <= 0.0:
        return SHIFT_BOUNDS[1]
    low, high = SHIFT_BOUNDS
    if _score(doc["times"], doc["tbest"], low) < target:  # target above what the bound can reach
        return low
    if _score(doc["times"], doc["tbest"], high) > target:
        return high
    for _ in range(SHIFT_STEPS):
        mid = (low + high) / 2.0
        if _score(doc["times"], doc["tbest"], mid) > target:
            low = mid  # `_score` decreases in the shift
        else:
            high = mid
    return round((low + high) / 2.0, 6)


def saturate(doc: dict[str, dict[str, float]], speedup: float, anchor: float) -> dict[str, Any]:
    """Set the distribution's level to ``anchor``, divide every case time by ``speedup``, rescore.

    The mapping is the judge's own formula, so it saturates case by case: a case already at tbest can
    gain nothing, while a slow case still can. ``speedup == 1`` returns the anchor and a very large
    speedup approaches 100 -- both are pinned by tests.
    """
    low, high = SPEEDUP_CLAMP
    factor = min(high, max(low, float(speedup)))
    shift = level_shift(doc, float(anchor))
    predicted = _score(doc["times"], doc["tbest"], shift - math.log(factor))
    return {"anchor": round(float(anchor), 2), "level_shift": shift,
            "saturated_score": round(predicted, 2), "gain": round(predicted - float(anchor), 2)}


def fit_pairs(workspace: pathlib.Path | str, title: str | None = None,
              as_of: datetime.datetime | None = None) -> list[tuple[float, float]]:
    """``(saturated prediction, realized gain)`` from consecutive receipts judged before ``as_of``.

    Each pair replays the transform on a shot whose outcome is known: the earlier receipt's per-case
    times (level-set to its own score) divided by the geometric speedup the platform achieved gives
    the saturated prediction, and the two receipts' official scores give the realized gain. That is
    exactly the quantity the shrink has to correct -- "the judge-formula reading of this local ratio
    predicted X, the judge paid Y". Receipts without per-case times cannot form a pair; failed shots
    are skipped, because a hard failure is a different prediction problem (S1), not a ranking error.
    """
    from .estimator import _parse_iso

    by_title: dict[str, list[tuple[datetime.datetime, dict[str, Any]]]] = {}
    for row in receipts.load(pathlib.Path(workspace), title):
        if str(row.get("status") or "") != "Pass":
            continue
        score = receipts.official_score(row)
        when = _parse_iso(row.get("submitted_at") or row.get("finished_at") or row.get("queued_at"))
        if not score or score <= 0 or when is None or (as_of is not None and when >= as_of):
            continue
        if case_doc(row) is None:
            continue
        by_title.setdefault(str(row.get("title") or ""), []).append((when, row))
    out: list[tuple[float, float]] = []
    for rows in by_title.values():
        rows.sort(key=lambda pair: pair[0])
        for (_, earlier), (_, later) in zip(rows, rows[1:]):
            first = case_doc(earlier)
            if first is None:
                continue
            keys = sorted(set(first["times"]) & set(first["tbest"]))
            if len(keys) < 2:
                continue
            start = _score({k: first["times"][k] for k in keys}, {k: first["tbest"][k] for k in keys})
            later_doc = case_doc(later) or first
            ratios = [first["times"][k] / later_doc["times"][k] for k in keys
                      if k in later_doc["times"] and later_doc["times"][k] > 0]
            if start <= 0 or len(ratios) < 2:
                continue
            speedup = math.exp(sum(math.log(min(10.0, max(0.1, r))) for r in ratios) / len(ratios))
            predicted = _score({k: first["times"][k] for k in keys}, {k: first["tbest"][k] for k in keys},
                               -math.log(min(SPEEDUP_CLAMP[1], max(SPEEDUP_CLAMP[0], speedup))))
            paid = float(receipts.official_score(later) or 0.0) - float(receipts.official_score(earlier) or 0.0)
            out.append((round(predicted - start, 4), round(paid, 4)))
    return out


def _slope(pairs: list[tuple[float, float]]) -> float | None:
    """Ridge-regularised through-origin slope of realized on predicted, ``None`` without pairs.

    The ridge is one point squared of prior energy. Without it a single pair whose saturated
    prediction happens to be ~0 (two receipts with the same times) divides a small realized change by
    a vanishing prediction and produces a slope of -50, which then zeroes the problem's predictions.
    A pair that predicted nothing says nothing.
    """
    num = sum(predicted * actual for predicted, actual in pairs)
    den = sum(predicted * predicted for predicted, _ in pairs)
    if not pairs or den <= 1e-9:
        return None
    return round(num / (den + SLOPE_RIDGE), 4)


def _blend(slope: float, pairs: int) -> float:
    """Blend a fitted slope with :data:`SHRINK_PRIOR` at :data:`SHRINK_PRIOR_WEIGHT` pseudo-pairs."""
    value = (SHRINK_PRIOR_WEIGHT * SHRINK_PRIOR + pairs * slope) / (SHRINK_PRIOR_WEIGHT + pairs)
    return round(min(1.0, max(0.0, value)), 4)


def shrinkage(workspace: pathlib.Path | str, title: str,
              as_of: datetime.datetime | None = None) -> dict[str, Any]:
    """The shrink of one problem: its own pairs, else the contest pool, else the prior.

    The fitted slope is the through-origin regression of the realized gain on the saturated
    prediction, clipped to ``[0, 1]`` (a negative slope would claim the opposite ranking, which this
    transform never does). A problem needs :data:`MIN_PAIRS` pairs to speak for itself, and either
    slope is blended with :data:`SHRINK_PRIOR`, so one pair cannot swing the prediction. :func:`fit_pairs` filters on the receipt
    timestamp, never on file position, so nothing judged at or after ``as_of`` is ever read.
    """
    own = fit_pairs(workspace, title, as_of)
    own_hat = _slope(own) if len(own) >= MIN_PAIRS else None
    if own_hat is not None:
        return {"c": _blend(own_hat, len(own)), "source": "problem", "pairs": len(own),
                "c_hat": own_hat, "c_pooled": None, "c_prior": SHRINK_PRIOR}
    pooled = fit_pairs(workspace, None, as_of)
    pooled_hat = _slope(pooled) if len(pooled) >= MIN_PAIRS else None
    if pooled_hat is not None:
        return {"c": _blend(pooled_hat, len(pooled)), "source": "contest", "pairs": 0,
                "c_hat": None, "c_pooled": pooled_hat, "c_prior": SHRINK_PRIOR}
    return {"c": SHRINK_PRIOR, "source": "prior", "pairs": 0, "c_hat": None, "c_pooled": None,
            "c_prior": SHRINK_PRIOR}


def adjust(workspace: pathlib.Path | str, title: str, repo: pathlib.Path | str | None,
           sha: str | None, baseline: str | None, anchor: float, speedup: float,
           predicted_gain: float, as_of: datetime.datetime | None = None) -> dict[str, Any]:
    """Tier one prediction and, for a cross-generation change, saturate and shrink its gain.

    The saturated reading is first clamped to the model's own gain -- same sign, never larger in
    magnitude -- and then multiplied by the shrink. Two reasons for the clamp: a public per-case
    subset (five cases on ops-bench) has a narrower spread than the full roster, so an unclamped
    rescoring reads far more headroom than the judge will pay; and a transform whose job is to
    remove over-prediction must never invent a bigger gain than the calibrated model already had.
    The gain therefore can be reduced twice and raised never.
    """
    tier = classify(repo, sha or "", baseline)
    out: dict[str, Any] = {"tier": tier["tier"], "tier_reason": tier["reason"], "files": tier["files"],
                           "kernel_files": tier["kernel_files"], "speedup": round(float(speedup), 4),
                           "anchor": round(float(anchor), 2), "predicted_gain": round(float(predicted_gain), 2),
                           "applied": False, "gain": round(float(predicted_gain), 2),
                           "estimated_score": round(float(anchor) + float(predicted_gain), 2)}
    if tier["tier"] != CROSS_GEN or float(anchor) <= 0.0:
        return out
    doc = judged_doc(workspace, title, baseline, as_of)
    source = "baseline"
    if doc is None:  # no judged receipt for the baseline SHA: borrow this problem's newest spread
        doc, source = judged_doc(workspace, title, None, as_of), "shape"
    if doc is None:
        out["tier_reason"] = tier["reason"] + "; no per-case distribution to rescore"
        return out
    sat = saturate(doc, speedup, anchor)
    capped = min(sat["gain"], predicted_gain) if predicted_gain >= 0 else max(sat["gain"], predicted_gain)
    fit = shrinkage(workspace, title, as_of)
    out.update({"applied": True, "doc_source": source, "doc_at": doc.get("at"),
                "doc_cases": len(doc["times"]), "doc_score": doc.get("score"),
                "saturated_score": sat["saturated_score"], "saturated_gain": sat["gain"],
                "capped_gain": round(capped, 2), "model_is_smaller": capped != sat["gain"],
                "shrink": fit["c"], "shrink_source": fit["source"], "shrink_pairs": fit["pairs"],
                "shrink_hat": fit["c_hat"], "gain": round(fit["c"] * capped, 2)})
    out["estimated_score"] = round(max(0.0, float(anchor) + out["gain"]), 2)
    return out
