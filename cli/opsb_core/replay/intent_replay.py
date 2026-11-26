"""4.3 P23: re-judge a replayed shot as ``intent=probe`` and ``intent=restore`` (the acceptance ruler).

The main replay answers what the 4.2.1 gate did. This module answers the two questions P23 has to
prove on real receipts: was a held shot worth taking as a probe (with its undo and its budget), and
would a rollback have been approved after a zeroed board or a judge-side failure (G4)?

It reads the same gate, one intent at a time. The hypothetical probe budget lives in a list of
timestamps the caller carries, so a series of shots is priced the way a live problem would see it.
"""
from __future__ import annotations

from typing import Any

#: Judge verdicts that mean the run itself failed; a restore follows any of them unconditionally.
FAILED_VERDICTS = ("Wrong Answer", "Runtime Error", "Time Limit Exceeded", "Compile Error",
                   "Memory Limit Exceeded", "System Error", "Fail")
NULL_BOARD = frozenset({"", "no-submission", "board-unknown", "unranked"})


def intents(gate: Any, title: str, shot: dict[str, Any], at: Any, before: tuple[float, str, str],
             verdict: Any, passed: set[str], probe_log: list[float], last_status: str) -> dict[str, Any]:
    """4.3 P23: re-judge one shot as `probe` (when the 4.2.1 gate held it) and as `restore`.

    The hypothetical probe budget is carried in ``probe_log``: a probe spends it only when this
    replay would approve it, so a series of shots is priced the way a live problem would see it.
    """
    from .gate_replay import _primary  # the gate's own precedence S1 -> S3 -> S2

    out: dict[str, Any] = {}
    if not verdict.approved and _primary(verdict)[0] == "NOISE_FLOOR":
        hours = float(gate.config.probe_window_hours)
        used = len([t for t in probe_log if (shot["ts"] - t) < hours * 3600.0])
        probe = gate._evaluate_impl(title, shot["sha"], as_of=at,
                                    override_board_score=float(before[0]),
                                    override_board_status=before[1] or None,
                                    intent="probe", probe_used=used)
        if probe.approved:
            probe_log.append(shot["ts"])
        out["probe"] = {"approved": bool(probe.approved), "code": probe.intent_code,
                        "budget_used": used, "status_tag": probe.status_tag,
                        "reason": probe.intent_reason[:240]}
    board_open = float(before[0]) <= 0 or before[1] in NULL_BOARD
    if shot["sha"] in passed and (board_open or last_status in FAILED_VERDICTS):
        restore = gate._evaluate_impl(title, shot["sha"], as_of=at,
                                      override_board_score=float(before[0]),
                                      override_board_status=before[1] or None,
                                      intent="restore")
        out["restore"] = {"approved": bool(restore.approved), "code": restore.intent_code,
                          "board_open": board_open, "after_failure": last_status in FAILED_VERDICTS,
                          "pricing": (restore.undo or {}).get("expected"),
                          "reason": restore.intent_reason[:240]}
    return out


