"""Probe budget, undo targets and restore pricing of the 4.3 submission intents.

The three questions an intent asks about the *history* rather than about the gate:

* how many probes this problem already spent inside the rolling window (judged plus in flight);
* whether a ``latest`` board has an undo — a SHA that passed online with at least the board's score;
* what a rollback SHA is worth now: the mean of its own Pass draws, each discounted to the
  contemporary board with ``ScoreEstimator.discount_draw_to_contemporary``, with sigma from the
  measured judge noise (``scheduler.judge_noise``) or the 4.2.1 constant.

``scheduler/submit_intent.py`` owns the decisions; this module only reads receipts and queue rows.
"""
from __future__ import annotations

import datetime
import math
import pathlib
from typing import Any

from . import receipts

#: `SIGMA_HIDDEN_JITTER` (4.2.1's constant same-source re-judge jitter) when judge noise is unknown.
DEFAULT_SIGMA = 2.70
NULL_BOARD_STATUS = frozenset({"", "no-submission", "board-unknown", "unranked"})


def _now(as_of: datetime.datetime | None = None) -> datetime.datetime:
    return as_of or datetime.datetime.now(datetime.timezone.utc)


def _stamp(row: dict[str, Any]) -> datetime.datetime | None:
    from .estimator import _parse_iso
    return _parse_iso(row.get("submitted_at") or row.get("finished_at") or row.get("queued_at"))


def _short(sha: Any) -> str:
    return str(sha or "").strip().lower()[:7]


def _window_start(as_of: datetime.datetime | None, hours: float) -> datetime.datetime:
    return _now(as_of) - datetime.timedelta(hours=max(0.0, float(hours)))


def probe_budget(workspace: pathlib.Path, title: str, config: Any, outcomes: list[dict[str, Any]], *,
                 as_of: datetime.datetime | None = None, exclude_sha: str = "",
                 used_override: int | None = None) -> dict[str, Any]:
    """Judged plus in-flight probes of this problem inside the rolling window (quota is unrelated)."""
    from .submit_intent import _read_jsonl, queue_rows, receipt_intent  # readers live with the decisions

    hours, limit = float(config.probe_window_hours), int(config.probe_per_window)
    start = _window_start(as_of, hours)
    if used_override is not None:
        used = int(used_override)
        judged = in_flight = None
    else:
        judged = 0
        for row in outcomes:
            if str(row.get("title") or "") != title or receipt_intent(workspace, row) != "probe":
                continue
            when = _stamp(row)
            if when is None or when < start:
                continue
            if as_of is not None and when >= as_of:
                continue
            judged += 1
        done = {(str(o.get("title") or ""), _short(o.get("sha")), str(o.get("queued_at") or ""))
                for o in _read_jsonl(pathlib.Path(workspace) / "results" / "submit-queue-outcome.jsonl")}
        in_flight = 0
        for rec in queue_rows(workspace):
            if str(rec.get("title") or "") != title:
                continue
            if str(rec.get("intent") or "").strip().lower() != "probe":
                continue
            if exclude_sha and _short(rec.get("sha")) == _short(exclude_sha):
                continue  # the shot being judged never spends its own budget
            if (title, _short(rec.get("sha")), str(rec.get("queued_at") or "")) in done:
                continue
            when = _stamp(rec)
            if when is None or when < start:
                continue
            in_flight += 1
        used = judged + in_flight
    return {"window_hours": hours, "limit": limit, "used": used, "judged": judged, "in_flight": in_flight,
            "remaining": max(0, limit - used), "allowed": used < limit,
            "window_start": start.isoformat()}


def passed_draws(estimator: Any, title: str, sha: str,
                 as_of: datetime.datetime | None = None) -> list[tuple[datetime.datetime | None, float]]:
    """This problem's Pass receipts of one SHA with a positive official score, in file order."""
    from . import receipts
    short = _short(sha)
    out: list[tuple[datetime.datetime | None, float]] = []
    for row in estimator.outcomes:
        if str(row.get("title") or "") != title or row.get("status") != "Pass":
            continue
        csha = str(row.get("canonical_sha") or row.get("sha") or "").lower()
        if not short or not csha.startswith(short):
            continue
        score = receipts.official_score(row)
        if not isinstance(score, (int, float)) or score <= 0:
            continue
        when = _stamp(row)
        if as_of is not None and when is not None and when >= as_of:
            continue
        out.append((when, float(score)))
    return out


def restore_price(estimator: Any, ledger: Any, title: str, sha: str,
                  as_of: datetime.datetime | None = None, workspace: pathlib.Path | None = None) -> dict[str, Any]:
    """Price a rollback: mean contemporary score of the SHA's Pass draws, plus measured sigma."""
    with_draws = passed_draws(estimator, title, sha, as_of=as_of)
    if not with_draws:
        return {"applicable": False, "draws": 0, "reason": "no online Pass receipt for this SHA"}
    from .estimator import ScoreEstimator, _parse_iso
    anchor = _parse_iso(getattr(ledger, "active_judged_at", None)) if ledger.current_score > 0 else None
    anchor = anchor or _now(as_of)
    gamma = float(getattr(ledger, "decay_factor_gamma", 1.0) or 1.0)
    draws = []
    for when, score in with_draws:
        contemp = ScoreEstimator.discount_draw_to_contemporary(score, when, gamma, anchor)
        draws.append({"at": when.isoformat() if when else None, "score": round(score, 2),
                      "contemporary": round(contemp, 2)})
    expected = round(sum(d["contemporary"] for d in draws) / len(draws), 2)
    sigma, source = DEFAULT_SIGMA, "constant"
    if workspace is not None:
        try:
            from . import judge_noise
            noise = judge_noise.sigma_for(workspace, title)
        except Exception:  # noqa: BLE001 - a missing noise estimate only falls back to the constant
            noise = None
        if noise and noise.get("sigma"):
            sigma, source = float(noise["sigma"]), f"judge_noise:{noise.get('scope')}"
    board = float(ledger.current_score or 0.0)
    gain = round(expected - board, 2)
    prob = 1.0 if sigma <= 0 else round(0.5 * (1.0 + math.erf((gain / sigma) / math.sqrt(2.0))), 4)
    return {"applicable": True, "draws": len(draws), "details": draws, "expected": expected,
            "gamma": round(gamma, 4), "anchor": anchor.isoformat(), "board": round(board, 2),
            "gain": gain, "sigma": round(sigma, 3), "sigma_source": source, "prob_positive": prob}


def rollback_targets(estimator: Any, title: str, board: float,
                     as_of: datetime.datetime | None = None) -> list[dict[str, Any]]:
    """SHAs that already passed online with at least the board's score: the undo of a `latest` probe."""
    from . import receipts
    peak: dict[str, float] = {}
    for row in estimator.outcomes:
        if str(row.get("title") or "") != title or row.get("status") != "Pass":
            continue
        score = receipts.official_score(row)
        if not isinstance(score, (int, float)) or score <= 0:
            continue
        when = _stamp(row)
        if as_of is not None and when is not None and when >= as_of:
            continue
        csha = str(row.get("canonical_sha") or row.get("sha") or "").lower()
        if csha and score > peak.get(csha, 0.0):
            peak[csha] = float(score)
    return sorted(({"sha": sha, "score": round(score, 2)} for sha, score in peak.items()
                   if score + 0.01 >= float(board)), key=lambda d: -d["score"])


def undo_for_probe(estimator: Any, ledger: Any, title: str, keeps_best: bool,
                   as_of: datetime.datetime | None = None) -> dict[str, Any]:
    """Whether a probe can be taken back: a ``best`` board always can, a ``latest`` one needs a SHA."""
    board = float(ledger.current_score or 0.0)
    status = str(ledger.board_status or "")
    if keeps_best:
        return {"exists": True, "kind": "best_board_keeps_its_score", "targets": []}
    if board <= 0 or status in NULL_BOARD_STATUS:
        return {"exists": True, "kind": "board_open", "board_status": status, "targets": []}
    targets = rollback_targets(estimator, title, board, as_of=as_of)
    return {"exists": bool(targets), "kind": "proven_sha_at_or_above_board" if targets else "none",
            "board": round(board, 2), "targets": targets[:3]}


