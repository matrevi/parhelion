"""Does the gate's priority order match what the judge actually paid? (4.3 P29)

Three questions about one replay group, asked of the same per-shot records P21 produces:

* **Spearman** -- between the gate's priority and the gain the shot really earned. Pooled over every
  approved shot (``spearman``), and also between the *predicted* gain and the real one
  (``spearman_predicted``), which is the fallback the sub-plan allows when a moment holds only one
  candidate and the priority-based correlation has nothing to rank.
* **top-1 hit rate** -- inside one moment (a wall-clock bucket, default 30 minutes, across problems,
  because the queue orders them against each other), did the highest-priority candidate turn out to
  be the one that gained the most?
* **gain error** -- predicted minus realized gain: median, mean, and a band per predicted-gain range.

Only shots the gate would actually dispatch (``approved``) and whose outcome the judge scored are
ranked: a blocked shot was never a candidate, and a ``Pass`` the ranking row never scored has no
gain to compare against. The realized gain is the judged score minus the board in front of the shot
-- the same quantity the estimator predicts as ``delta_expected`` -- so a hard failure counts as
losing the whole board in front of it; ``ex_hard`` repeats the headline numbers without them so a
reader can see the ranking is not being judged on S1's job.

Confidence intervals are cluster bootstraps that resample **problems** (the natural unit: a
problem's shots share its history and its tbest), never individual shots.
"""
from __future__ import annotations

import random
import statistics
from typing import Any, Callable

BOOTSTRAP_ROUNDS = 1000
BOOTSTRAP_SEED = 20261126
CI_LEVEL = 0.95
MOMENT_SECONDS = 1800
#: Judge verdicts that mean the run itself failed (see ``gate_metrics.HARD_FAILURES``).
HARD_FAILURES = ("Time Limit Exceeded", "Wrong Answer", "Runtime Error")
#: ``(low, high, label)`` of the predicted-gain bands in the error block; the top band is open.
ERROR_BANDS = ((-1e9, 0.0, "<=0"), (0.0, 2.0, "0-2"), (2.0, 5.0, "2-5"), (5.0, 10.0, "5-10"),
               (10.0, 1e9, ">10"))


def ranks(values: list[float]) -> list[float]:
    """Ranks 1..n with ties sharing their average rank."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    pos = 0
    while pos < len(order):
        end = pos
        while end + 1 < len(order) and values[order[end + 1]] == values[order[pos]]:
            end += 1
        shared = (pos + end) / 2.0 + 1.0
        for k in range(pos, end + 1):
            out[order[k]] = shared
        pos = end + 1
    return out


def spearman(x: list[float], y: list[float]) -> float | None:
    """Rank correlation, ``None`` when either side is constant (there is no order to agree on)."""
    if len(x) < 2 or len(x) != len(y):
        return None
    rx, ry = ranks(x), ranks(y)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return round(num / den, 4) if den > 1e-12 else None


def rows(shots: list[dict[str, Any]], moment_seconds: float = MOMENT_SECONDS,
         approved_only: bool = True) -> list[dict[str, Any]]:
    """The rankable shots of one replay group: judged to a score, predicted, and timed.

    ``approved_only`` keeps the gate's own candidate set (what it would have dispatched);
    ``approved_only=False`` also ranks the shots it held, which is the counterfactual reading of
    "did the estimate order this problem's shots correctly".
    """
    out: list[dict[str, Any]] = []
    for shot in shots:
        if approved_only and not shot.get("approved"):
            continue
        if shot.get("score") is None:
            continue
        priority, predicted = shot.get("priority"), shot.get("predicted_gain")
        if not isinstance(priority, (int, float)) or not isinstance(predicted, (int, float)):
            continue
        when = float(shot.get("ts") or 0.0)
        out.append({"title": str(shot.get("title") or ""), "ts": when,
                    "bucket": int(when // max(1.0, moment_seconds)),
                    "priority": float(priority), "predicted": float(predicted),
                    "gain": round(float(shot["score"]) - float(shot.get("board_score") or 0.0), 4),
                    "hard": str(shot.get("status") or "") in HARD_FAILURES})
    return out


def moments(picked: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-moment Spearman (weighted by pairs) and the top-1 hit rate."""
    groups: dict[int, list[dict[str, Any]]] = {}
    for row in picked:
        groups.setdefault(row["bucket"], []).append(row)
    weighted = pairs = used = hits = 0
    for group in groups.values():
        if len(group) < 2:
            continue
        used += 1
        rho = spearman([r["priority"] for r in group], [r["gain"] for r in group])
        if rho is not None:
            weighted += rho * len(group)
            pairs += len(group)
        best = max(group, key=lambda r: (r["priority"], -r["ts"]))
        if best["gain"] >= max(r["gain"] for r in group):
            hits += 1
    return {"spearman": round(weighted / pairs, 4) if pairs else None, "pairs": pairs,
            "top1": round(hits / used, 4) if used else None, "moments": used, "hits": hits}


def _band(value: float) -> str:
    for low, high, label in ERROR_BANDS:
        if low <= value < high:
            return label
    return ERROR_BANDS[-1][2]


def errors(picked: list[dict[str, Any]]) -> dict[str, Any]:
    """Predicted minus realized gain, overall and per predicted-gain band."""
    diffs = [round(r["predicted"] - r["gain"], 4) for r in picked]
    bands: dict[str, list[float]] = {}
    for row in picked:
        bands.setdefault(_band(row["predicted"]), []).append(round(row["predicted"] - row["gain"], 4))
    return {
        "n": len(diffs),
        "median": round(statistics.median(diffs), 4) if diffs else None,
        "mean": round(statistics.fmean(diffs), 4) if diffs else None,
        "bands": [{"band": label, "n": len(bands[label]),
                   "mean": round(statistics.fmean(bands[label]), 4),
                   "median": round(statistics.median(bands[label]), 4)}
                  for _, _, label in ERROR_BANDS if bands.get(label)],
    }


def _cell(value: float | None, ci: list[float] | None, n: int, **extra: Any) -> dict[str, Any]:
    """One metric cell; ``unstable`` marks a bootstrap interval that does not bracket its own point.

    With a handful of moments and problems (mat-chem) a cluster bootstrap can put the interval away
    from the estimate, which means the interval is not usable evidence -- say so in the data instead
    of letting a reader mistake it for a precise bound.
    """
    out: dict[str, Any] = {"value": value, "ci95": ci, "n": n, **extra}
    if value is not None and ci and not ci[0] <= value <= ci[1]:
        out["unstable"] = True
    return out


def _percentile(samples: list[float], level: float = CI_LEVEL) -> list[float] | None:
    if not samples:
        return None
    samples = sorted(samples)
    last = len(samples) - 1
    return [round(samples[int((1 - level) / 2 * last)], 4), round(samples[int((1 + level) / 2 * last)], 4)]


def cluster_ci(units: dict[str, list[dict[str, Any]]], stat: Callable[[list[dict[str, Any]]], float | None],
               *, rounds: int = BOOTSTRAP_ROUNDS, seed: int = BOOTSTRAP_SEED) -> list[float] | None:
    """Percentile CI of ``stat`` resampling whole problems with replacement."""
    keys = [key for key, value in units.items() if value]
    if not keys:
        return None
    rng = random.Random(seed)
    samples: list[float] = []
    for _ in range(rounds):
        picked: list[dict[str, Any]] = []
        for _ in range(len(keys)):
            picked.extend(units[keys[rng.randrange(len(keys))]])
        value = stat(picked)
        if value is not None:
            samples.append(value)
    return _percentile(samples)


def _block(picked: list[dict[str, Any]], rounds: int = BOOTSTRAP_ROUNDS,
           seed: int = BOOTSTRAP_SEED) -> dict[str, Any]:
    """The headline cells of one population, with cluster-bootstrap intervals."""
    units: dict[str, list[dict[str, Any]]] = {}
    for row in picked:
        units.setdefault(row["title"], []).append(row)
    clear = [r for r in picked if not r["hard"]]
    by_shot = lambda rows_: spearman([r["priority"] for r in rows_], [r["gain"] for r in rows_])
    by_pred = lambda rows_: spearman([r["predicted"] for r in rows_], [r["gain"] for r in rows_])
    summary = moments(picked)
    return {
        "spearman": _cell(by_shot(picked), cluster_ci(units, by_shot, rounds=rounds, seed=seed), len(picked)),
        "spearman_predicted": _cell(by_pred(picked), cluster_ci(units, by_pred, rounds=rounds, seed=seed),
                                    len(picked)),
        "spearman_moment": _cell(summary["spearman"],
                                 cluster_ci(units, lambda rows_: moments(rows_)["spearman"],
                                            rounds=rounds, seed=seed),
                                 summary["pairs"], moments=summary["moments"]),
        "top1": _cell(summary["top1"],
                      cluster_ci(units, lambda rows_: moments(rows_)["top1"], rounds=rounds, seed=seed),
                      summary["moments"], moments=summary["moments"], hits=summary["hits"]),
        "error": errors(picked),
        "ex_hard": {"spearman": by_shot(clear), "spearman_moment": moments(clear)["spearman"],
                    "top1": moments(clear)["top1"], "error_median": errors(clear)["median"], "n": len(clear)},
    }


def evaluate(shots: list[dict[str, Any]], *, moment_seconds: float = MOMENT_SECONDS,
             rounds: int = BOOTSTRAP_ROUNDS, seed: int = BOOTSTRAP_SEED) -> dict[str, Any]:
    """The three metrics plus cluster-bootstrap intervals for one replay group.

    Two populations: the gate's candidates (``approved``) and, under ``counterfactual``, every
    judged shot of the group -- the second is the only one with enough shots on a contest whose
    S1 blocks most of the history on evidence-format drift.
    """
    approved = rows(shots, moment_seconds, approved_only=True)
    return {
        "shots": {"total": len(shots), "ranked": len(approved),
                  "unpriced": sum(1 for s in shots if s.get("approved") and s.get("score") is None),
                  "blocked": sum(1 for s in shots if not s.get("approved")),
                  "hard_failures": sum(1 for r in approved if r["hard"]),
                  "problems": len({r["title"] for r in approved})},
        "moment_seconds": moment_seconds,
        **_block(approved, rounds, seed),
        "counterfactual": _block(rows(shots, moment_seconds, approved_only=False), rounds, seed),
    }


def compare(current: dict[str, Any], new: dict[str, Any],
            metrics: tuple[str, ...] = ("spearman", "spearman_moment", "top1")) -> dict[str, Any]:
    """Per metric: the current scheme's value against the new scheme's value and lower CI bound."""
    out: dict[str, Any] = {}
    for name in metrics:
        have = (current.get(name) or {}).get("value")
        mine = new.get(name) or {}
        low = (mine.get("ci95") or [None, None])[0]
        out[name] = {"current": have, "new": mine.get("value"), "new_ci95": mine.get("ci95"),
                     "beats": bool(have is not None and low is not None and low > have)}
    return out


def verdict(per_contest: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The sub-plan's enable rule: one ranking metric must beat the current scheme in *every* contest.

    "Spearman or top-1, lower confidence bound above the current scheme" is read as the same metric
    clearing the bar in both replays, not one metric here and another there.
    """
    ranked = [name for name in ("spearman", "spearman_moment", "top1")
              if per_contest and all((block.get(name) or {}).get("beats") for block in per_contest.values())]
    return {"enabled": bool(ranked), "metrics": ranked, "per_contest": per_contest}
