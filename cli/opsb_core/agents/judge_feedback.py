"""Compute strategic events from paid judge receipts at a common score epoch."""
from __future__ import annotations

import json
import re

from .. import config as _cfg


def calibration_evidence(ws, row):
    """Pair a receipt with immutable reports that existed when that SHA was submitted."""
    title, sha = row["title"], row.get("sha") or ""
    data = {k: row[k] for k in ("submission_id", "title", "sha", "submitted_at", "status", "score",
            "cases_passed", "cases_total", "estimated_score", "predicted_score",
            "public_times_us", "public_tbest_us") if k in row}
    reports = {}
    cutoff = _cfg.parse_iso(row.get("submitted_at"))
    if cutoff and re.fullmatch(r"[0-9a-f]{40}", sha) and title not in (".", "..") and "/" not in title:
        folder = ws / "results" / title / sha
        for path in sorted(folder.glob("bench-runs/*/report.json")):
            run = _cfg.read_json(path, {})
            if not isinstance(run, dict):
                continue
            stamp = _cfg.parse_iso(run.get("finished_at"))
            if (not stamp or not stamp.tzinfo or not cutoff.tzinfo or stamp > cutoff or run.get("title") != title or run.get("sha") != sha
                    or run.get("run_id") != path.parent.name):
                continue
            group = "formal" if run.get("formal") else "experiment"
            if group not in reports or stamp > reports[group][0]:
                reports[group] = stamp, run, path
    data["local"] = {}
    artifacts = []
    for group, (_, run, path) in reports.items():
        ref = str(path.relative_to(ws))
        artifacts.append(ref)
        data["local"][group] = {"report": ref, "run_id": run["run_id"], "finished_at": run["finished_at"], "summary": run.get("summary"),
            "timing_source": run.get("timing_source"), "timing_score_eligible": run.get("timing_score_eligible"),
            "failed_cases": [c.get("id") for c in run.get("cases", []) if c.get("status") not in ("PASS", "NOT_RUN")]}
    if not reports:
        data["local_evidence_status"] = "No matching pre-submission case report"
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")), artifacts


def _ordered_receipts(rows):
    """Terminal receipts (one per submission) oldest first, as ``(stamp, row)``."""
    import sys
    if str(_cfg.SUPERVISOR_ROOT) not in sys.path:
        sys.path.insert(0, str(_cfg.SUPERVISOR_ROOT))
    from ..services.judge_score_fetch import TERMINAL_JUDGE_STATUSES

    receipts = {}
    for row in rows:
        sid = row.get("submission_id")
        stamp = _cfg.parse_iso(row.get("submitted_at") or row.get("finished_at"))
        if sid and stamp and row.get("status") in TERMINAL_JUDGE_STATUSES:
            receipts[sid] = (stamp, row)
    return sorted(receipts.values(), key=lambda item: (item[0], str(item[1]["submission_id"])))


def epoch_gains(rows, gamma):
    """``{submission_id: (gain, prior)}``: each scored receipt against the best earlier score at its epoch.

    Same arithmetic as the breakthrough test of ``judge_feedback`` (``score * gamma - prior``), so the
    ledger's verdicts on hypotheses and the platform's breakthrough events use one gain.
    """
    from scheduler import receipts as _receipts
    from scheduler.estimator import ScoreEstimator

    previous, out = [], {}
    for stamp, row in _ordered_receipts(rows):
        score = _receipts.official_score(row)
        if score is None:
            continue
        prior = max((ScoreEstimator.discount_draw_to_contemporary(s, ts, gamma, stamp) for ts, s in previous),
                    default=0.0)
        previous.append((stamp, score))
        out[row["submission_id"]] = (round(score * gamma - prior, 2), round(prior, 2))
    return out


def judge_feedback(rows, threshold, gamma):
    import sys
    if str(_cfg.SUPERVISOR_ROOT) not in sys.path:
        sys.path.insert(0, str(_cfg.SUPERVISOR_ROOT))
    from scheduler import receipts as _receipts
    from scheduler.estimator import ScoreEstimator

    ordered = _ordered_receipts(rows)
    previous = []
    last_rotation = 0
    for stamp, row in ordered:
        score = _receipts.official_score(row)
        if score is None:  # a Pass whose score no source has yet
            yield row, []
            continue
        def peak(history):
            return max((ScoreEstimator.discount_draw_to_contemporary(s, ts, gamma, stamp) for ts, s in history),
                       default=0.0)
        prior = peak(previous)
        previous.append((stamp, score))
        events = []
        if row["status"] == "Pass" and score * gamma - prior > threshold:
            events.append(("breakthrough", "Review the measured gain and delegate a reusable KB entry with evidence."))
        if len(previous) - last_rotation >= 6 and peak(previous[-6:]) - peak(previous[:-6]) <= threshold:
            # 4.3 P26 item 6: the strategic message carries the call the orchestrator would otherwise have
            # to assemble by hand. Auto-replacement (off by default, 00-PLAN D4) uses the same event.
            title = str(row.get("title") or "").strip().replace('"', "")
            call = f' Executable: parhelion_sprint_start(replace=["{title}"]).' if title else ""
            events.append(("stagnation", "Summarize the last six paid attempts and assign a new worker an "
                                         f"orthogonal direction.{call}"))
            last_rotation = len(previous)
        yield row, events
