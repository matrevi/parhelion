"""Submission intents (4.3 P23): ``exploit`` (the 4.2.1 gate), ``probe`` and ``restore``.

The gate used to have one coarse switch for a speculative shot (``force``) and one narrow restore
rule (``is_critical_restore``, board confirmed zero). 4.3 gives the two useful cases their own
intent, each with a condition instead of a bypass:

* ``probe`` — a speculative shot that must be undoable and budgeted. S1 stays exactly as it is (no
  escape hatch); S2 drops the noise floor and asks only for a minimum P(gain>0). Under ``latest``
  the shot needs an undo; each problem may probe a bounded number of times per window. Whenever a
  condition fails the shot is judged as ``exploit`` and the reply says why.
* ``restore`` — a rollback to a SHA that already passed online, priced from its own Pass draws at
  the contemporary board. Gain is measured against the current board with the 4.2.1 S2 rule, except
  that a zeroed board or a previous judge-side failure approves unconditionally.

Intent is recorded in the queue row (``results/submit-queue.jsonl``) and read back from the receipt
row, so calibration, the ledger and the replay can tell a speculative shot from a normal one. A
worker that asks for ``exploit`` still adopts a pending row's intent: the dispatch path re-evaluates
a queued SHA without an explicit intent, and it must see the same verdict the worker got.
"""
from __future__ import annotations

import dataclasses
import datetime
import json
import pathlib
from typing import Any

from . import receipts
from .intent_pricing import (
    DEFAULT_SIGMA, NULL_BOARD_STATUS, probe_budget, restore_price, rollback_targets, undo_for_probe,
)

INTENTS: tuple[str, ...] = ("exploit", "probe", "restore")
PROBE_CODE = "PROBE_INTENT"
RESTORE_CODE = "RESTORE_INTENT"
#: Judge verdicts after which a restore should go out without waiting for a better estimate.
RESTORE_AFTER_FAILURE = frozenset({"Wrong Answer", "Runtime Error", "Time Limit Exceeded",
                                   "Compile Error", "Memory Limit Exceeded", "System Error", "Fail"})

_QUEUE_CACHE: dict[str, tuple[tuple[int, int], list[dict[str, Any]]]] = {}


def normalize_intent(raw: Any) -> str:
    """``exploit`` | ``probe`` | ``restore``; anything else is a caller error, not a default."""
    text = str(raw or "exploit").strip().lower()
    if text not in INTENTS:
        raise ValueError(f"intent must be one of {', '.join(INTENTS)}, not {text!r}")
    return text


def _read_jsonl(path: pathlib.Path) -> list[dict[str, Any]]:
    try:
        stat = path.stat()
    except OSError:
        return []
    hit = _QUEUE_CACHE.get(str(path))
    if hit and hit[0] == (stat.st_mtime_ns, stat.st_size):
        return hit[1]
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    except OSError:
        return []
    _QUEUE_CACHE[str(path)] = ((stat.st_mtime_ns, stat.st_size), rows)
    return rows


def _short(sha: Any) -> str:
    return str(sha or "").strip().lower()[:7]


def _stamp(row: dict[str, Any]) -> datetime.datetime | None:
    from .estimator import _parse_iso
    return _parse_iso(row.get("submitted_at") or row.get("finished_at") or row.get("queued_at"))


def queue_rows(workspace: pathlib.Path) -> list[dict[str, Any]]:
    return _read_jsonl(pathlib.Path(workspace) / "results" / "submit-queue.jsonl")


def queued_intent(workspace: pathlib.Path, title: str, sha: str) -> str:
    """The intent of the *pending* queue row of this (title, SHA), or ``""``.

    Only a pending row counts: once the receipt exists the row is history, and a later submission of
    the same SHA must be free to choose its own intent.
    """
    short = _short(sha)
    if not short:
        return ""
    done = {(str(o.get("title") or ""), _short(o.get("sha")), str(o.get("queued_at") or ""))
            for o in _read_jsonl(pathlib.Path(workspace) / "results" / "submit-queue-outcome.jsonl")}
    found = ""
    for rec in queue_rows(workspace):
        intent = str(rec.get("intent") or "").strip().lower()
        if not intent or intent not in INTENTS or str(rec.get("title") or "") != title:
            continue
        if _short(rec.get("sha")) != short:
            continue
        if (title, short, str(rec.get("queued_at") or "")) in done:
            continue
        found = intent
    return found


def resolve_intent(workspace: pathlib.Path, title: str, sha: str, requested: Any) -> tuple[str, str]:
    """``(effective, source)``: an explicit non-exploit request, else the pending row, else exploit."""
    try:
        asked = normalize_intent(requested)
    except ValueError:
        asked = "exploit"
    if asked != "exploit":
        return asked, "request"
    queued = queued_intent(workspace, title, sha)
    if queued:
        return queued, "queue"
    return "exploit", "default"


def receipt_intent(workspace: pathlib.Path, row: dict[str, Any]) -> str:
    """The intent behind one receipt row: its own field, else the queue row that produced it."""
    own = str(row.get("intent") or "").strip().lower()
    if own in INTENTS:
        return own
    title, short = str(row.get("title") or ""), _short(row.get("sha"))
    queued_at = str(row.get("queued_at") or "")
    best = ""
    for rec in queue_rows(workspace):
        intent = str(rec.get("intent") or "").strip().lower()
        if intent not in INTENTS or str(rec.get("title") or "") != title or _short(rec.get("sha")) != short:
            continue
        if queued_at and str(rec.get("queued_at") or "") == queued_at:
            return intent
        best = intent
    return best or "exploit"


def forced(row: dict[str, Any], workspace: pathlib.Path | None = None) -> bool:
    """A forced or operator-overridden shot: R5 §7 keeps its receipt out of the calibration samples.

    The flag rides on the receipt; a reconciler may rebuild a row without it, so the queue row that
    produced the receipt is the fallback (matched by ``queued_at`` first, then by SHA).
    """
    if bool(row.get("force")) or str(row.get("source") or "") == "operator-override":
        return True
    if workspace is None:
        return False
    title, short, queued_at = str(row.get("title") or ""), _short(row.get("sha")), str(row.get("queued_at") or "")
    matched, all_forced = [], True
    for rec in queue_rows(workspace):
        if str(rec.get("title") or "") != title or _short(rec.get("sha")) != short:
            continue
        if queued_at and str(rec.get("queued_at") or "") == queued_at:
            return bool(rec.get("force"))
        matched.append(bool(rec.get("force")))
        all_forced = all_forced and bool(rec.get("force"))
    return bool(matched) and all_forced


#: GateConfig.calibration_excludes_forced, set by the gate before each evaluation (4.2.1 when False).
EXCLUDE_FORCED: bool = True


def calibration_marks(workspace: pathlib.Path, row: dict[str, Any]) -> dict[str, Any]:
    """``{excluded, intent}`` for one receipt row: forced shots leave the calibration samples."""
    intent = receipt_intent(workspace, row)
    return {"excluded": bool(EXCLUDE_FORCED) and forced(row, workspace), "intent": intent}


def has_scored_pass(outcomes: list[dict[str, Any]], title: str,
                    as_of: datetime.datetime | None = None) -> bool:
    """Whether the problem already has a Pass receipt with a positive score (the opening window ends)."""
    for row in outcomes:
        if str(row.get("title") or "") != title or row.get("status") != "Pass":
            continue
        score = receipts.official_score(row)
        if not isinstance(score, (int, float)) or score <= 0:
            continue
        when = _stamp(row)
        if as_of is not None and when is not None and when >= as_of:
            continue
        return True
    return False


@dataclasses.dataclass
class Decision:
    """One intent's verdict contribution: a stage, a priority, or a fall back to `exploit`."""

    intent: str
    requested: str
    code: str
    approved: bool | None
    reason: str
    metrics: dict[str, Any] = dataclasses.field(default_factory=dict)
    critical: bool = False
    explore: bool = False
    priority: float | None = None
    fallback: str = ""


def _restore_command(title: str, sha: str, why: str) -> str:
    return f'opsb submit {title} {sha[:7]} --intent restore --note "{why}"'


def plan(*, intent: str, requested: str, estimator: Any, ledger: Any, estimate: Any, title: str,
         sha: str, difficulty: int, floor: float, keeps_best: bool, config: Any,
         outcomes: list[dict[str, Any]], last_status: str = "",
         as_of: datetime.datetime | None = None, probe_used: int | None = None) -> Decision:
    """Decide one non-exploit intent; ``approved=None`` means "judge this shot as exploit"."""
    effective = normalize_intent(intent)
    if effective == "exploit":
        return Decision(effective, requested, "EXPLOIT", None, "")
    if not config.submit_intents:
        return Decision("exploit", effective, "INTENTS_DISABLED", None,
                        "submit_intents is off: every intent is judged as exploit")
    if effective == "probe":
        return _probe(estimator=estimator, ledger=ledger, estimate=estimate, title=title, sha=sha,
                      difficulty=difficulty, keeps_best=keeps_best, config=config, outcomes=outcomes,
                      as_of=as_of, requested=requested, probe_used=probe_used)
    return _restore(estimator=estimator, ledger=ledger, estimate=estimate, title=title, sha=sha,
                    difficulty=difficulty, floor=floor, keeps_best=keeps_best, last_status=last_status,
                    as_of=as_of, requested=requested)


def _probe(*, estimator: Any, ledger: Any, estimate: Any, title: str, sha: str, difficulty: int,
           keeps_best: bool, config: Any, outcomes: list[dict[str, Any]],
           as_of: datetime.datetime | None, requested: str, probe_used: int | None) -> Decision:
    unknown = (str(getattr(estimate, "estimation_method", "")) == "anchor_fallback"
               or "no perf telemetry" in str(getattr(estimate, "detail", "")).lower())
    prob = float(getattr(estimate, "prob_positive", 0.0) or 0.0)
    undo = undo_for_probe(estimator, ledger, title, keeps_best, as_of=as_of)
    budget = probe_budget(estimator.workspace, title, config, outcomes, as_of=as_of,
                          exclude_sha=sha, used_override=probe_used)
    metrics: dict[str, Any] = {
        "intent": "probe", "requested_intent": requested, "probability_known": not unknown,
        "prob_positive": round(prob, 4), "min_prob": float(config.probe_min_prob),
        "undo": undo, "budget": budget,
        "expected_improvement": round(float(getattr(estimate, "expected_improvement", 0.0) or 0.0), 2),
    }
    if not unknown and prob < float(config.probe_min_prob):
        return Decision("exploit", requested, "PROBE_NO_ODDS", None,
                        f"P(gain>0)={prob:.2f} < probe_min_prob={config.probe_min_prob:g}: judged as exploit",
                        metrics, fallback="low_probability")
    if not undo["exists"]:
        return Decision("exploit", requested, "PROBE_NO_UNDO", None,
                        ("no rollback target under latest: no SHA at or above the board "
                         f"{float(ledger.current_score or 0.0):.2f} has passed online: judged as exploit"),
                        metrics, fallback="no_undo")
    if not budget["allowed"]:
        return Decision("exploit", requested, "PROBE_BUDGET_SPENT", None,
                        (f"probe budget spent: {budget['used']}/{budget['limit']} in "
                         f"{budget['window_hours']:g}h: judged as exploit"),
                        metrics, fallback="budget_spent")
    target = (undo.get("targets") or [{}])[0].get("sha") if undo.get("kind") == "proven_sha_at_or_above_board" else ""
    metrics["restore_command"] = _restore_command(title, target, "undo a failed probe") if target else ""
    metrics["next_step"] = (
        f"if this probe loses, roll back with `{metrics['restore_command']}`" if target
        else "this board keeps its best score, so a failed probe costs only the attempt")
    priority = round(difficulty * float(getattr(estimate, "expected_improvement", 0.0) or 0.0) * 0.5, 2)
    odds = "estimate unknown" if unknown else f"P(gain>0)={prob:.2f}"
    return Decision("probe", requested, PROBE_CODE, True,
                    f"PROBE intent: {odds}, undo={undo['kind']}, "
                    f"budget {budget['used'] + 1}/{budget['limit']} in {budget['window_hours']:g}h",
                    metrics, priority=priority)


def _restore(*, estimator: Any, ledger: Any, estimate: Any, title: str, sha: str, difficulty: int,
             floor: float, keeps_best: bool, last_status: str,
             as_of: datetime.datetime | None, requested: str) -> Decision:
    price = restore_price(estimator, ledger, title, sha, as_of=as_of, workspace=estimator.workspace)
    metrics: dict[str, Any] = {"intent": "restore", "requested_intent": requested, "pricing": price,
                               "last_status": last_status}
    if not price["applicable"]:
        return Decision("exploit", requested, "RESTORE_UNPRICED", None,
                        f"{sha[:7]} has no online Pass receipt: judged as exploit", metrics,
                        fallback="no_online_pass")
    if keeps_best:
        on_board = (str(getattr(ledger, "historical_best_sha", "") or "").lower().startswith(sha[:7].lower())
                    or str(getattr(ledger, "active_board_sha", "") or "").lower().startswith(sha[:7].lower()))
        if not on_board:
            return Decision("restore", requested, "RESTORE_NOT_ON_BEST_BOARD", False,
                            (f"the best board keeps its best score: {sha[:7]} is not on it, so restoring "
                             "it cannot raise the board"), metrics)
        return Decision("exploit", requested, "RESTORE_ON_BEST_BOARD", None,
                        f"{sha[:7]} is the board's best SHA: judged with the normal best-board rule",
                        metrics, fallback="best_board_owns_it")
    board = float(price["board"])
    status = str(ledger.board_status or "")
    if board <= 0 or status in NULL_BOARD_STATUS:
        return Decision("restore", requested, RESTORE_CODE, True,
                        (f"RESTORE intent: the board is open ({status or 'no-submission'}), "
                         f"{sha[:7]} pays {price['expected']:.2f} vs {price['sigma_source']} sigma "
                         f"{price['sigma']:.2f}"), metrics, critical=True)
    if last_status in RESTORE_AFTER_FAILURE:
        return Decision("restore", requested, RESTORE_CODE, True,
                        (f"RESTORE intent: the last shot returned {last_status}, {sha[:7]} pays "
                         f"{price['expected']:.2f} >= board {board:.2f}"), metrics, critical=True)
    gain = float(price["gain"])
    if gain >= floor:
        return Decision("restore", requested, RESTORE_CODE, True,
                        (f"RESTORE intent: expected {price['expected']:.2f} - board {board:.2f} = "
                         f"{gain:+.2f} >= +{floor:.2f} pts (sigma={price['sigma']:.2f})"),
                        metrics, priority=round(difficulty * gain, 2))
    if gain > 0.0 and price["prob_positive"] >= float(estimate and 0.5):
        return Decision("restore", requested, RESTORE_CODE, True,
                        (f"RESTORE intent: expected gain {gain:+.2f} is below the +{floor:.2f} pt floor but "
                         f"P(gain>0)={price['prob_positive']:.2f}; approved at low priority"),
                        metrics, explore=True, priority=round(difficulty * gain * 0.5, 2))
    return Decision("restore", requested, RESTORE_CODE, False,
                    (f"RESTORE intent: expected {price['expected']:.2f} - board {board:.2f} = {gain:+.2f} "
                     f"with P(gain>0)={price['prob_positive']:.2f}; holding"),
                    metrics)


__all__ = [
    "DEFAULT_SIGMA",
    "Decision",
    "INTENTS",
    "PROBE_CODE",
    "RESTORE_CODE",
    "calibration_marks",
    "forced",
    "has_scored_pass",
    "normalize_intent",
    "plan",
    "probe_budget",
    "queue_rows",
    "queued_intent",
    "receipt_intent",
    "resolve_intent",
    "restore_price",
    "rollback_targets",
    "undo_for_probe",
]
