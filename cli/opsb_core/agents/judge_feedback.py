"""Compute strategic events from paid judge receipts at a common score epoch."""
from __future__ import annotations

from .. import config as _cfg


def judge_feedback(rows, threshold, gamma):
    import sys
    if str(_cfg.SUPERVISOR_ROOT) not in sys.path:
        sys.path.insert(0, str(_cfg.SUPERVISOR_ROOT))
    from scheduler.estimator import ScoreEstimator
    from ..services.judge_score_fetch import TERMINAL_JUDGE_STATUSES

    receipts = {}
    for row in rows:
        sid = row.get("submission_id")
        stamp = _cfg.parse_iso(row.get("submitted_at") or row.get("finished_at"))
        if sid and stamp and row.get("status") in TERMINAL_JUDGE_STATUSES:
            receipts[sid] = (stamp, row)
    ordered = sorted(receipts.values(), key=lambda item: (item[0], str(item[1]["submission_id"])))
    previous = []
    last_rotation = 0
    for stamp, row in ordered:
        if row["status"] == "Pass" and row.get("score") is None:
            continue
        score = float(row.get("score") or 0)
        def peak(history):
            return max((ScoreEstimator.discount_draw_to_contemporary(s, ts, gamma, stamp)
                        for ts, s in history), default=0.0)
        prior = peak(previous)
        previous.append((stamp, score))
        events = []
        if row["status"] == "Pass" and score * gamma - prior > threshold:
            events.append(("breakthrough", "Review the measured gain and delegate a reusable KB entry with evidence."))
        if len(previous) - last_rotation >= 6 and peak(previous[-6:]) - peak(previous[:-6]) <= threshold:
            events.append(("stagnation", "Summarize the last six paid attempts and assign a new worker an orthogonal direction."))
            last_rotation = len(previous)
        yield row, events
