"""Queue-card lamps, badge and reason read from a gate verdict, never recomputed (4.1 #9).

The submission gate (``scheduler.gate.SubmissionGate``) decides; the queue only shows it. A
verdict-like dict carries ``stages`` (S1 SAFETY, S2 NOISE_FLOOR, S3 CIRCUIT_BREAKER and the
dispatch stage QUOTA_RESERVE), ``approved``, ``status_tag`` and the estimate numbers.

- Three lamps mirror S1-S3 one for one. The dispatch stage is not a gate (AGENTS.md section 3:
  account slots and pacing only order dispatch), so it is ``dispatch_ok`` beside the lamps and
  its reason is never quoted on a card; a card blocked only there reads "waiting for a slot".
- Without a verdict the card is ``evaluated=false`` and says NOT EVALUATED: nothing is guessed.
"""
from __future__ import annotations

from typing import Any

LAMPS = (("safety_ok", "SAFETY", "S1"), ("noise_ok", "NOISE_FLOOR", "S2"), ("circuit_ok", "CIRCUIT_BREAKER", "S3"))
DISPATCH_STAGE = "QUOTA_RESERVE"
NOT_EVALUATED = "NOT EVALUATED"
WAITING = "WAITING FOR SLOT"
TAG_BADGES = {
    "EXPLORE": "EXPLORE", "RATE_WAIT": "RATE WAIT", "CRITICAL_RESTORE": "CRITICAL RESTORE",
    "PROBE": "PROBE", "READY": "READY", "SKIPPED": "SKIPPED",
}
# 4.3 P23 stage codes of an approved probe/restore; a CRITICAL_RESTORE keeps its own badge.
INTENT_BADGES = {"PROBE_INTENT": "PROBE", "RESTORE_INTENT": "RESTORE"}
NOT_TESTED = ("NO_VALIDATION_RECORD", "VALIDATION_INCOMPLETE", "REFERENCE_SUITE_REQUIRED", "MISSING_LOG",
              "MISSING_EXTENDED_PASSED", "MISSING_CROSS_CORE_STRESS", "FAST_LANE_ONLY")
TEST_FAILED = ("NONZERO_MISMATCH", "LOG_FAILURE_MARKER", "STRESS_FAILURES", "NON_DETERMINISTIC_GATE", "GATE_BAD_LANES")


def _stages(verdict: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(s.get("stage")): s for s in verdict.get("stages") or [] if isinstance(s, dict)}


def _number(value: Any, digits: int) -> float | None:
    return round(float(value), digits) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def not_evaluated(**numbers: Any) -> dict[str, Any]:
    """Lamps of a card no gate has judged: all dark, flagged as such."""
    return {"evaluated": False, "safety_ok": False, "noise_ok": False, "circuit_ok": False, "all_pass": False,
            "dispatch_ok": None, "delta_expected": numbers.get("delta_expected") or 0.0,
            "wspt_score": numbers.get("wspt_score") or 0.0}


def lamps(verdict: dict[str, Any] | None) -> dict[str, Any]:
    """``gates`` of a card: S1-S3 exactly as judged, the dispatch stage separate."""
    stages = _stages(verdict or {})
    if not all(stage in stages for _, stage, _ in LAMPS):
        return not_evaluated()
    est = verdict.get("estimate") if isinstance(verdict.get("estimate"), dict) else verdict
    out: dict[str, Any] = {key: bool(stages[stage].get("passed")) for key, stage, _ in LAMPS}
    out.update(
        evaluated=True,
        all_pass=all(out[key] for key, _, _ in LAMPS),
        dispatch_ok=bool(stages[DISPATCH_STAGE].get("passed")) if DISPATCH_STAGE in stages else None,
        delta_expected=_number(est.get("delta_expected"), 2) or 0.0,
        wspt_score=_number(verdict.get("priority_score"), 2) or 0.0,
    )
    for key, digits in (("ucb_delta", 2), ("calibration_trust", 3)):
        if _number(est.get(key), digits) is not None:
            out[key] = _number(est.get(key), digits)
    if isinstance(est.get("explore_eligible"), bool):
        out["explore_eligible"] = est["explore_eligible"]
    return out


def safety_badge(code: str) -> str:
    if code in ("ESCAPE_LOCK_ACTIVE", "ESCAPE_SHA_REPEAT"):
        return "ESCAPE LOCK"
    if code == "JUDGE_TIME_RISK":  # 4.3 P24: held because the judge would likely time out
        return "HOLD: JUDGE TIME"
    if code == "JUDGE_COMPILE_FAILED":  # P09: the judge-toolchain compile check failed
        return "COMPILE FAILED"
    if code in NOT_TESTED:
        return "NOT TESTED"
    if code.startswith("EXIT_CODE_") or code in TEST_FAILED:
        return "TEST FAILED"
    return "REJECTED"


def _first_failed(stages: dict[str, dict[str, Any]]) -> tuple[str, str, dict[str, Any]] | None:
    # The gate's own precedence: safety, then the breaker, then expected improvement.
    for key, stage, label in (LAMPS[0], LAMPS[2], LAMPS[1]):
        if stage in stages and not stages[stage].get("passed"):
            return stage, label, stages[stage]
    return None


def intent_badge(stages: dict[str, dict[str, Any]]) -> str:
    """The badge of an approved 4.3 intent shot (S2 passed with an intent code), else ``""``."""
    result = stages.get("NOISE_FLOOR") or {}
    if not result.get("passed"):
        return ""
    return INTENT_BADGES.get(str(result.get("code") or ""), "")


def badge(verdict: dict[str, Any] | None) -> str:
    stages = _stages(verdict or {})
    if not all(stage in stages for _, stage, _ in LAMPS):
        return NOT_EVALUATED
    tag = str(verdict.get("status_tag") or "")
    if tag == "SKIPPED":
        return "SKIPPED"
    failed = _first_failed(stages)
    if failed is not None:
        stage, _, result = failed
        if stage == "SAFETY":
            return safety_badge(str(result.get("code") or ""))
        return "CIRCUIT OPEN" if stage == "CIRCUIT_BREAKER" else "HOLD: LOW GAIN"
    if DISPATCH_STAGE in stages and not stages[DISPATCH_STAGE].get("passed"):
        return WAITING
    if tag != "CRITICAL_RESTORE":
        # 4.3 P23: a probe reads PROBE and a priced restore RESTORE, whatever tier they were
        # approved at; the critical restore keeps its own badge and its dispatch priority.
        intent = intent_badge(stages)
        if intent:
            return intent
    return TAG_BADGES.get(tag, "READY")


def reason(verdict: dict[str, Any] | None) -> str:
    """Why the card stands where it does, in the gate's words; dispatch details stay off the card."""
    stages = _stages(verdict or {})
    if not all(stage in stages for _, stage, _ in LAMPS):
        return "Not evaluated by the submission gate yet"
    if verdict.get("status_tag") == "SKIPPED":
        return str(verdict.get("gate_reason") or "Skipped by operator")
    failed = _first_failed(stages)
    if failed is not None:
        stage, label, result = failed
        return f"[{label}:{stage}] {result.get('reason') or ''}".strip()
    passed = f"Gate stages S1-S3 passed: {stages['NOISE_FLOOR'].get('reason') or ''}".strip()
    if DISPATCH_STAGE in stages and not stages[DISPATCH_STAGE].get("passed"):
        return "Waiting for a free submission slot; " + passed
    return passed


def next_step(verdict: dict[str, Any] | None) -> str:
    """The S1 escape lock's next step, else the 4.3 intent's (probe undo command, restore pricing)."""
    stages = _stages(verdict or {})
    for stage in ("SAFETY", "NOISE_FLOOR"):
        result = stages.get(stage) or {}
        metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
        step = str(metrics.get("next_step") or "")
        if step:
            return step
    return ""


def health_of(verdict: dict[str, Any] | None) -> dict[str, Any] | None:
    """S1's mechanical health view (4.3 P24, ``scheduler.health``), or None when it did not run."""
    s1 = _stages(verdict or {}).get("SAFETY") or {}
    metrics = s1.get("metrics") if isinstance(s1.get("metrics"), dict) else {}
    health = metrics.get("health")
    return health if isinstance(health, dict) else None


def card_fields(verdict: dict[str, Any] | None) -> dict[str, Any]:
    """``gates``, ``badge``, ``detail``, ``next_step`` and the health view of one queue card."""
    return {"gates": lamps(verdict), "badge": badge(verdict), "detail": reason(verdict),
            "next_step": next_step(verdict), "health": health_of(verdict)}
