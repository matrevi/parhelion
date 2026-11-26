"""Fold replayed gate verdicts into the three blocks P21 reports (4.3): gate, outcome, credibility.

Every function here is pure: it takes the per-shot records produced by
``cli/opsb_core/replay/gate_replay.py`` and returns JSON-ready counts. Nothing in this module reads
the workspace, the board or the gate — that separation is what lets the replay engine stay a small
"copy the workspace, judge each shot" loop.

The three blocks answer the sub-plan's questions directly:

- ``blocked``: how many shots each stage/code refused, and whether the gate named a next step;
- ``events``: blocked new highs, blocked restores, frozen shots, restore approvals;
- ``outcome``: the sprint result computed from receipts alone (no gate), so a gate change cannot
  move the ruler it is measured against;
- ``credibility``: S1/S3/board/restore read receipts only (credible); S2 needs local pairings
  (approximate, reported as the share of shots that carry one).
"""
from __future__ import annotations

from typing import Any

#: Judge verdicts that mean the run itself failed (not a Pass judged below the board).
HARD_FAILURES = ("Time Limit Exceeded", "Wrong Answer", "Runtime Error")
#: Board statuses that do not prove the board is zero (see ``ProblemLedger.confirmed_board_zero``).
NULL_BOARD_STATUS = frozenset({"", "no-submission", "board-unknown", "unranked"})
#: Two scores closer than this are the same peak (the judge reports two decimals).
TIE_PTS = 0.01


def _rate(count: int, total: int) -> float:
    return round(count / total, 4) if total else 0.0


def seed(prior: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-problem peak of the shots before ``--since`` so a windowed run is not a fresh contest."""
    peak: dict[str, float] = {}
    for shot in prior:
        score = shot.get("score") or 0.0
        if shot.get("status") == "Pass" and score > peak.get(shot["title"], 0.0):
            peak[shot["title"]] = float(score)
    return {"peak": peak}


def blocked(shots: list[dict[str, Any]]) -> dict[str, Any]:
    """Block counts split by the stage and the code that actually blocked the shot."""
    refused = [s for s in shots if not s["approved"]]
    by_stage: dict[str, int] = {}
    by_code: dict[str, list[int]] = {}
    for shot in refused:
        by_stage[shot["stage"]] = by_stage.get(shot["stage"], 0) + 1
        row = by_code.setdefault(f"{shot['stage']}:{shot['code']}", [0, 0])
        row[0] += 1
        row[1] += 1 if shot["next_step"] else 0
    return {
        "shots": len(shots), "blocked": len(refused), "blocked_rate": _rate(len(refused), len(shots)),
        "by_stage": dict(sorted(by_stage.items(), key=lambda kv: -kv[1])),
        "by_stage_code": dict(sorted(by_code.items(), key=lambda kv: -kv[1][0])),
        "blocked_with_next_step": sum(1 for s in refused if s["next_step"]),
    }


def events(shots: list[dict[str, Any]], seed_data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Blocked new highs, blocked restores, frozen shots and the restore-approval ratio.

    A shot is a *new high* when its recorded score beat the problem's peak of that moment; it is a
    *restore* when its SHA had already passed online; the board is *zeroed* when the shot in front of
    it shows 0 with a status that proves it. ``frozen`` counts, after each judge-side failure, how
    many of that problem's next shots S1 refused before anything got through again.
    """
    peak: dict[str, float] = dict((seed_data or {}).get("peak") or {})
    passed: dict[str, set[str]] = {}
    seen: dict[str, int] = {}
    new_highs: list[dict[str, Any]] = []
    restores: list[dict[str, Any]] = []
    zeros: list[dict[str, Any]] = []
    approvals: list[bool] = []
    frozen: list[dict[str, Any]] = []
    for shot in shots:
        title, score = shot["title"], shot["score"] or 0.0
        before = peak.get(title, 0.0)
        seen[title] = seen.get(title, 0) + 1
        is_restore = shot["sha"] in passed.get(title, set())
        if shot["status"] == "Pass" and score > before:
            peak[title] = score
            if not shot["approved"]:
                new_highs.append({"title": title, "sha": shot["short_sha"], "at": shot["submitted_at"],
                                  "score": round(score, 2), "previous_peak": round(before, 2),
                                  "code": shot["code"], "shot": seen[title]})
        if is_restore and not shot["approved"]:
            restores.append({"title": title, "sha": shot["short_sha"], "at": shot["submitted_at"],
                             "code": shot["code"], "shot": seen[title]})
        if shot["board_score"] == 0 and shot["board_status"] not in NULL_BOARD_STATUS:
            zeros.append({"title": title, "sha": shot["short_sha"], "at": shot["submitted_at"],
                          "is_restore": is_restore, "approved": shot["approved"], "code": shot["code"]})
            if is_restore:
                approvals.append(bool(shot["approved"]))
        if shot["status"] == "Pass" and score > 0:
            passed.setdefault(title, set()).add(shot["sha"])
    for idx, shot in enumerate(shots):
        if shot["status"] not in HARD_FAILURES:
            continue
        streak = 0
        for nxt in shots[idx + 1:]:
            if nxt["title"] != shot["title"]:
                continue
            if not nxt["approved"] and nxt["stage"] == "SAFETY":
                streak += 1
                continue
            break
        if streak:
            frozen.append({"title": shot["title"], "after": shot["submitted_at"],
                           "after_status": shot["status"], "blocked_s1": streak})
    frozen.sort(key=lambda f: -f["blocked_s1"])
    approved = sum(1 for ok in approvals if ok)
    return {
        "blocked_new_highs": new_highs, "blocked_restores": restores,
        "frozen": {"total": sum(f["blocked_s1"] for f in frozen),
                   "max_streak": max((f["blocked_s1"] for f in frozen), default=0), "events": frozen},
        "restore": {"zero_events": len(zeros), "restore_attempts": len(approvals),
                    "restore_approved": approved,
                    "restore_approve_rate": _rate(approved, len(approvals)) if approvals else None,
                    "approved_per_zero": _rate(approved, len(zeros)) if zeros else None,
                    "events": zeros},
    }


def outcome(shots: list[dict[str, Any]], seed_data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Sprint result from receipts alone — the history the gate was replayed against.

    ``points`` sums the board's own change over the window, so it telescopes to
    ``final board - board at the window start`` in either board mode. ``recovery`` measures, after a
    zeroed board, how long until the board was non-zero again.
    """
    peak: dict[str, float] = dict((seed_data or {}).get("peak") or {})
    same: set[tuple[str, str]] = set()
    count = {"new_high": 0, "tie": 0, "regression": 0, "hard_failure": 0, "resubmit": 0}
    points = 0.0
    recovery: list[float] = []
    last: dict[str, float] = {}
    zeros: dict[str, float] = {}
    for shot in shots:
        title, score = shot["title"], shot["score"]
        key = (title, shot["sha"])
        before = peak.get(title, 0.0)
        count["resubmit"] += 1 if key in same else 0
        same.add(key)
        if shot["status"] in HARD_FAILURES:
            count["hard_failure"] += 1
        if shot["status"] == "Pass" and (score or 0.0) > 0:
            if before > 0 and score > before + TIE_PTS:
                count["new_high"] += 1
            elif before > 0 and abs(score - before) <= TIE_PTS:
                count["tie"] += 1
            elif before > 0:
                count["regression"] += 1
            peak[title] = max(before, float(score))
        points += shot["board_after"] - shot["board_score"]
        last[title] = shot["board_after"]
        if shot["board_score"] > 0 and shot["board_after"] <= 0:
            zeros[title] = shot["ts"]
        elif zeros.get(title) is not None and shot["board_after"] > 0:
            recovery.append((shot["ts"] - zeros.pop(title)) / 60.0)
    total = len(shots)
    closing = {t: round(max(0.0, p - last[t]), 2) for t, p in sorted(peak.items()) if t in last}
    return {
        "judged": total, "new_highs": count["new_high"], "new_high_rate": _rate(count["new_high"], total),
        "ties": count["tie"], "tie_rate": _rate(count["tie"], total),
        "regressions": count["regression"], "regression_rate": _rate(count["regression"], total),
        "hard_failures": count["hard_failure"], "hard_fail_rate": _rate(count["hard_failure"], total),
        "points": round(points, 2),
        "points_per_judgement": round(points / total, 3) if total else 0.0,
        "same_source_resubmits": count["resubmit"], "same_source_rate": _rate(count["resubmit"], total),
        "recovery_median_min": round(sorted(recovery)[len(recovery) // 2], 1) if recovery else None,
        "recovery_events": len(recovery),
        "closing_gap": round(sum(closing.values()), 2), "closing_by_problem": closing,
    }


def credibility(shots: list[dict[str, Any]]) -> dict[str, Any]:
    """S1/S3/board/restore read receipts only (credible); S2 needs local pairings (approximate)."""
    paired = sum(1 for s in shots if s["paired_cases"] > 0)
    methods: dict[str, int] = {}
    for shot in shots:
        methods[shot["method"]] = methods.get(shot["method"], 0) + 1
    return {"s1_s3_board_restore": "credible", "s2": "approximate",
            "s2_paired_shots": paired, "s2_paired_rate": _rate(paired, len(shots)),
            "estimation_methods": dict(sorted(methods.items(), key=lambda kv: -kv[1]))}
