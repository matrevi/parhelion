"""4-Stage Statistical & Safety Submission Gate + Decay-Aware Quota & Circuit Breaker.

Stages:
  1. SAFETY:
     - Verified `result.json` (`exit_code == 0`) & byte-consistent git tree (`op_host`/`op_kernel`)
     - Full suite (`FAST_LANE=1` barred), `EXTENDED_PASSED` present
     - Zero `EXTENDED_FAILED` / `EXTENDED_CASES_FAILED` / `VALIDATION_FAILED` / `STRESS_CASE_FAIL`
     - All `mismatched=0/...`, `GATE_BAD ... bad=0/...`, `GATE_DET ... identical=True`
     - Cross-core / barrier static+dynamic safety check (`SetAtomicAdd`/`SyncAll` requires
       `STRESS_DONE` / `DETERMINISTIC` / `GATE_DET` unless already proven online)
  2. NOISE_FLOOR:
     - The floor is `clamp(sigma_hat, noise_floor_pts, 2 * noise_floor_pts)` where `sigma_hat` is the
       measured judge noise of the problem (`judge_noise`), else the configured `+2.5` pts.
     - When the problem's rule keeps the best score (`ranking_submission_mode = best`), a candidate
       also passes when E[max(0, gain)] clears the floor and P(gain > 0) >= 0.2
       (`EXPECTED_IMPROVEMENT`). Under `latest` the board shows the last submission, so the gain is
       measured against the current board and neither that shortcut nor the [RESTORE] "beat the
       historical best" comparison applies.
     - Below the floor, a candidate with E[gain] > 0 and P(gain > 0) >= 0.5 is approved at low
       priority (`LOW_GAIN_POSITIVE`, tagged EXPLORE); the rest hold as `HOLD_LOW_GAIN` (and
       `delta_expected = 0.0` when `candidate_sha == active_board_sha`).
     - Bypassed automatically when board is `0` / `no-submission` (`CRITICAL_RESTORE`) or `force=True`
  3. CIRCUIT_BREAKER:
     - Nygard 3-state machine (`CLOSED` / `OPEN` / `HALF_OPEN`) over rolling 12h window
     - Compares decay-adjusted contemporary scores (`gamma_p` + residual `-0.015 pts/hr`) rather
       than raw 8-day-old peaks so multi-day `tbest(t)` tightening never manufactures false streaks
     - Advisory (4.2.1): an open breaker no longer refuses; the candidate is approved at half
       priority, tagged EXPLORE, with the breaker's reason. `GateConfig.circuit_blocks` restores refusal.
  A TLE receipt no longer arms the escape lock (no reproducer can clear a timing verdict); the commit
  that timed out cannot be resubmitted unchanged until it has passed online (`TLE_SHA_REPEAT`).
  4. QUOTA_RESERVE (dispatch slot, not a gate):
     - Reports whether an account slot is free: 190s inter-submission pacing (`RATE_WAIT`) and the
       50/day Beijing-day (`UTC+8` 00:00 reset) per-account cap. With every slot used the stage
       fails and the verdict is `QUOTA_EXHAUSTED` (never `RATE_WAIT`), but the candidate stays
       approved and waits. Daily allowance never enters approval, a hold or the ranking; the
       closing-restore count in `QuotaStatus` is informational only.
     - Orders approved submissions by `(CRITICAL_RESTORE desc, difficulty * delta_expected desc)`
"""
from __future__ import annotations

import dataclasses
import datetime
import json
import pathlib
import re
import subprocess
from typing import Any

from .estimator import (
    CROSS_CORE_OPS,
    ScoreEstimate,
    ScoreEstimator,
    _parse_iso,
    resolve_sha,
)
from . import receipts
from . import submit_intent
from .parhelion_paths import list_eligible_account_slots
from .probes import OPENING_PROBES, compile_evidence, submitted_attempts


def _dev(key: str, default: str = "") -> str:
    """Deployment setting from `~/.parhelion/config.json` ("device").

    `scheduler` may be imported without the CLI package on `sys.path`, so the
    accessor is imported lazily and a missing CLI falls back to the neutral default.
    """
    try:
        from cli.opsb_core.device_env import device_text
        return device_text(key) or default
    except Exception:
        return default


STATUS_TAGS: tuple[str, ...] = (
    "READY",
    "EXPLORE",
    "CRITICAL_RESTORE",
    "RATE_WAIT",
    "QUOTA_EXHAUSTED",
    "JUDGING",
    "PASS_UP",
    "PASS_DOWN",
    "HOLD_LOW_GAIN",
    "CIRCUIT_OPEN",
    "REJECTED_GATE",
    "SKIPPED",
    "ZEROED_ERR",
)

BEIJING_TZ = datetime.timezone(datetime.timedelta(hours=8))
DAILY_HARD_LIMIT = 50
MIN_SUBMIT_GAP_SEC = 190

#: S1 refusals an opening probe may never bypass: locks and identity, not missing measurement.
OPENING_HARD_CODES = frozenset({
    "ESCAPE_LOCK_ACTIVE", "TLE_SHA_REPEAT", "UNRESOLVED_SHA",
    "GIT_TREE_MISMATCH", "CORRUPT_RESULT_JSON", "JUDGE_COMPILE_FAILED",
})

# 4.3 P23 §6a: every failed stage names the next action, so a held shot is never a dead end (P21
# measured 6/50 = 12% before this). S1's escape lock and TLE carry their own contextual step; this
# table fills the rest by stage code, with the reason string as the last resort.
STAGE_NEXT_STEPS: dict[str, str] = {
    # S1 SAFETY
    "CORRUPT_RESULT_JSON": "re-run parhelion_test level=final for this SHA so result.json parses",
    "UNRESOLVED_SHA": "commit the change and hand over the full `git rev-parse HEAD` SHA",
    "SHA_MISMATCH": "hand over the SHA whose tree the validation really ran on",
    "GIT_TREE_MISMATCH": "commit the working-tree change, then re-run the validation on that commit",
    "MISSING_LOG": "re-run parhelion_test level=final to produce validation.log",
    "REFERENCE_SUITE_REQUIRED": "run parhelion_test level=final with the reference scheme",
    "FAST_LANE_ONLY": "run the full suite (level standard or final), not the fast lane",
    "MISSING_EXTENDED_PASSED": "run parhelion_test level=final; EXTENDED_PASSED is required",
    "NONZERO_MISMATCH": "fix the mismatching cases and re-test",
    "GATE_BAD_LANES": "make the lanes deterministic (GATE_BAD bad=0) and re-test",
    "NON_DETERMINISTIC_GATE": "remove the non-determinism and re-test until GATE_DET identical=True",
    "STRESS_FAILURES": "fix the stress-case failures and re-test",
    "MISSING_CROSS_CORE_STRESS": "add a cross-core/barrier stress run (SetAtomicAdd/SyncAll) and re-test",
    "JUDGE_COMPILE_FAILED": "make the SHA compile with the judge toolchain, then re-test",
    # S2 NOISE_FLOOR
    "OPERATOR_SKIPPED": "clear the operator skip to make this SHA eligible again",
    "ALREADY_ACTIVE_ON_BOARD": "this SHA is already the board entry: change it, or re-roll with intent=probe",
    "BELOW_NOISE_FLOOR": "add a paired measurement that clears the floor, or send this SHA as intent=probe",
    "BELOW_BEST_IN_RESTORE": "land a SHA above the best, or send this SHA as intent=probe",
    "NEGATIVE_EXPECTED_RESTORE": "the estimate is below the board: improve the kernel, or use intent=probe",
    "RESTORE_INTENT": "restore a proven SHA whose priced gain clears the floor, or wait for an open board",
    "PROBE_NO_UNDO": "no rollback target: land a higher proven SHA first, or improve the kernel",
    "PROBE_BUDGET_SPENT": "the probe budget is spent for this window: wait for it to roll over, or improve the kernel",
    "PROBE_NO_ODDS": "P(gain>0) is below probe_min_prob: record a paired measurement first",
    # S3 CIRCUIT_BREAKER
    "CIRCUIT_OPEN_SWITCH_BASELINE": "try an orthogonal mechanism direction (six non-improving shots in the window)",
    "HALF_OPEN_UNPROVEN_SHA": "land a SHA that passed online before, or send this one as intent=probe",
    # S4 dispatch (never withholds approval, but the card still says what happens next)
    "DAILY_LIMIT_REACHED": "wait for the Beijing-day reset; approval is unaffected",
}


def with_next_step(stage: StageResult) -> StageResult:
    """A failed stage always carries ``metrics['next_step']`` (4.3 P23 §6a). Passed stages are left alone."""
    if stage.passed:
        return stage
    metrics = dict(stage.metrics or {})
    if not str(metrics.get("next_step") or "").strip():
        step = STAGE_NEXT_STEPS.get(stage.code, "")
        if not step and stage.code.startswith("EXIT_CODE_"):
            step = "read the failing case in result.json, fix it and re-run parhelion_test"
        metrics["next_step"] = step or (
            f"read the stage reason and address it before the next shot: {stage.reason}")
        stage.metrics = metrics
    return stage


@dataclasses.dataclass
class GateConfig:
    noise_floor_pts: float = 2.5
    # The board keeps each problem's best score, so a shot whose expected improvement
    # E[max(0, gain)] clears the floor is worth taking even when the point estimate does not.
    keep_best_board: bool = True
    min_prob_positive: float = 0.2
    # Floor of the displayed closing-restore count; it never gates, holds or ranks a candidate.
    min_closing_reserve: int = 2
    circuit_limit_shots: int = 6
    circuit_window_hours: float = 12.0
    rate_limit_gap_sec: int = MIN_SUBMIT_GAP_SEC
    daily_limit: int = DAILY_HARD_LIMIT
    q0_quantum_sec: int = 180
    aging_tau_sec: float = 600.0
    # Only judge-acknowledged attempts consume the opening window.
    probe_submissions_per_problem: int = OPENING_PROBES
    # 4.2.1 (docs/v4.3/research/antigravity/00-SUMMARY.md §4.1/4.3/4.4/4.9). Each switch restores the
    # 4.2.0 behaviour when flipped.
    # Read each problem's `ranking_submission_mode`; under `latest` the board shows the last submission,
    # so neither E[max(0, gain)] nor "must beat the historical best" applies.
    board_mode_from_rules: bool = True
    # A TLE arms the escape lock (4.2.0). No reproducer can clear a timing verdict, so it froze the problem.
    tle_arms_escape_lock: bool = False
    # An open breaker refuses (4.2.0). Advisory: approve at half priority and say why.
    circuit_blocks: bool = False
    # Noise floor from measured judge noise: clamp(sigma_hat, noise_floor_pts, 2 * noise_floor_pts).
    noise_floor_from_judge_sigma: bool = True
    # Below the floor but more likely to gain than lose: approve at low priority instead of holding.
    approve_low_gain_positive: bool = True
    low_gain_min_prob: float = 0.5
    # 4.3 P23 submission intents (`intent=exploit|probe|restore`). Each switch on its own restores
    # the 4.2.1 behaviour: `submit_intents=false` judges every intent as exploit.
    submit_intents: bool = True
    # Opening exemption runs until the problem's first scored Pass, at most `opening_max_probes`
    # judged receipts (D1). Off: the 4.2.1 fixed `probe_submissions_per_problem` window.
    opening_until_first_score: bool = True
    opening_max_probes: int = 8
    # A probe is a trial, not a quota spend: `probe_per_window` probes per `probe_window_hours`.
    probe_per_window: int = 2
    probe_window_hours: float = 12.0
    probe_min_prob: float = 0.3
    # 4.3 P24 (docs/v4.3/P24-escape-health.md). Each switch restores the 4.2.1 behaviour when flipped.
    # A WA/RE whose reproducer variants pass on the bad SHA's own formal final runs releases the lock
    # for every other SHA (EXPLORE, half priority) and only keeps the bad SHA itself out.
    escape_unreproduced_releases: bool = True
    # Formal final runs that must pass every variant before the failure counts as unreproduced.
    unrepro_runs: int = 2
    # S1 mechanical health: compile seconds, template instantiations, kernel object bytes, cold ratio.
    health_checks: bool = True
    # An approved candidate with health warnings ranks at this factor (hint only, never a hold).
    health_penalty: float = 0.8
    # Judge time risk holds an exploit submission; false reports it as a hint and lets it through.
    health_time_risk_holds: bool = True
    # Share of the judge's time limit at which compile + predicted run counts as risky (00-PLAN D3).
    health_time_margin: float = 0.8
    # The judge compiles on its own machine; the device host's seconds are scaled by this factor.
    judge_compile_factor: float = 1.0
    # 4.3 P29 prediction tiers. `jitter_sigma_from_judge_noise` (on) replaces the estimator's
    # constant same-source jitter with the problem's measured judge noise; off restores 2.70.
    # `ranking_v2` (off) additionally puts a cross-generation change through the saturation
    # transform and the shrink fitted on the problem's receipts. Both move only the predicted gain
    # and the priority it feeds, never a hold reason.
    jitter_sigma_from_judge_noise: bool = True
    ranking_v2: bool = False

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def set_switch(self, text: str) -> tuple[str, Any]:
        """Apply one ``name=value`` gate switch (``opsb queue config --gate``); unknown names raise."""
        name, sep, raw = str(text).partition("=")
        name, raw = name.strip(), raw.strip().lower()
        kind = GATE_SWITCHES.get(name)
        if not sep or kind is None:
            raise ValueError(f"unknown gate switch {text!r}; use one of {', '.join(sorted(GATE_SWITCHES))}")
        if kind is bool:
            if raw not in ("1", "0", "true", "false", "on", "off", "yes", "no"):
                raise ValueError(f"{name} takes true or false, not {raw!r}")
            value: Any = raw in ("1", "true", "on", "yes")
        else:
            try:
                parsed = float(raw) if kind is float else int(float(raw))
            except ValueError:
                raise ValueError(f"{name} takes a {'number' if kind is float else 'whole number'}, not {raw!r}")
            low, high = GATE_SWITCH_BOUNDS.get(name, (0.0, 1.0) if kind is float else (0, None))
            value = kind(parsed)
            value = max(low, value) if low is not None else value
            value = min(high, value) if high is not None else value
        setattr(self, name, value)
        return name, value

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GateConfig:
        valid = {f.name for f in dataclasses.fields(cls)}
        filtered = {k: v for k, v in data.items() if k in valid and v is not None}
        return cls(**filtered)


# The gate switches `opsb queue config --gate name=value` may set.
GATE_SWITCHES: dict[str, type] = {
    "board_mode_from_rules": bool,
    "tle_arms_escape_lock": bool,
    "circuit_blocks": bool,
    "noise_floor_from_judge_sigma": bool,
    "approve_low_gain_positive": bool,
    "low_gain_min_prob": float,
    "submit_intents": bool,
    "opening_until_first_score": bool,
    "opening_max_probes": int,
    "probe_per_window": int,
    "probe_window_hours": float,
    "probe_min_prob": float,
    # 4.3 P24
    "escape_unreproduced_releases": bool,
    "unrepro_runs": int,
    "health_checks": bool,
    "health_penalty": float,
    "health_time_risk_holds": bool,
    "health_time_margin": float,
    "judge_compile_factor": float,
    # 4.3 P29
    "jitter_sigma_from_judge_noise": bool,
    "ranking_v2": bool,
}

#: Clamp per switch; a float not listed here keeps the 4.2.1 ``[0, 1]`` clamp (a probability).
GATE_SWITCH_BOUNDS: dict[str, tuple[Any, Any]] = {
    "probe_window_hours": (0.0, 168.0),
    "opening_max_probes": (0, 64),
    "probe_per_window": (0, 64),
    # The judge compiles on its own machine, which may be slower than the device host.
    "judge_compile_factor": (0.1, 4.0),
    "unrepro_runs": (1, 16),
}


@dataclasses.dataclass
class StageResult:
    stage: str
    passed: bool
    code: str
    reason: str
    metrics: dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class CircuitBreakerStatus:
    title: str
    state: str  # CLOSED | OPEN | HALF_OPEN
    shots_in_window: int
    consecutive_non_improving: int
    consecutive_zero_errors: int
    window_best_score: float       # Decay-adjusted contemporary peak
    raw_window_best_score: float   # Unadjusted historical score
    decay_factor_gamma: float
    window_hours: float
    limit_shots: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class QuotaStatus:
    daily_limit: int
    used_today: int
    remaining_today: int
    needed_closing_restores: int
    reserved_for_closing: int
    discretionary_remaining: int
    closing_restore_targets: list[dict[str, Any]]
    rate_limit_gap_sec: int
    seconds_since_last_submit: int | None
    rate_wait_sec: int
    beijing_date: str
    next_reset_utc: str

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class GateVerdict:
    title: str
    sha: str
    short_sha: str
    approved: bool
    status_tag: str
    gate_reason: str
    priority_score: float
    is_critical_restore: bool
    forced: bool
    decay_factor_gamma: float
    raw_historical_best_score: float
    contemporary_best_score: float
    true_restore_gain: float
    estimate: ScoreEstimate
    stages: list[StageResult]
    circuit_breaker: CircuitBreakerStatus
    quota: QuotaStatus
    # 4.3 P23: the intent the shot was judged with, what the caller asked for, and the undo plan
    # (`probe`) or pricing (`restore`) that decided it. `exploit` carries the 4.2.1 verdict.
    intent: str = "exploit"
    requested_intent: str = "exploit"
    intent_reason: str = ""
    intent_code: str = "EXPLOIT"
    undo: dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "sha": self.sha,
            "short_sha": self.short_sha,
            "approved": self.approved,
            "status_tag": self.status_tag,
            "gate_reason": self.gate_reason,
            "priority_score": self.priority_score,
            "is_critical_restore": self.is_critical_restore,
            "forced": self.forced,
            "decay_factor_gamma": self.decay_factor_gamma,
            "raw_historical_best_score": self.raw_historical_best_score,
            "contemporary_best_score": self.contemporary_best_score,
            "true_restore_gain": self.true_restore_gain,
            "intent": self.intent,
            "requested_intent": self.requested_intent,
            "intent_code": self.intent_code,
            "intent_reason": self.intent_reason,
            "undo": self.undo,
            "estimate": self.estimate.to_dict(),
            "stages": [s.to_dict() for s in verdict_stages_list(self.stages)],
            "circuit_breaker": self.circuit_breaker.to_dict(),
            "quota": self.quota.to_dict(),
        }


def verdict_stages_list(stages: list[StageResult]) -> list[StageResult]:
    return stages


_MISMATCH_RE = re.compile(r"mismatched=(\d+)/(\d+)")

#: `git` reflog / fetch echo lines: text on them is prose (a commit subject), never a verdict.
#: Real false positive this guards: `Previous HEAD position was 39fea6c test(det_bad): print
#: first 24 mismatched (t, s) coordinates on GATE_FAIL` made S1 reject a commit that passed.
_GIT_ECHO_RE = re.compile(
    r"^\s*(?:"
    r"Previous HEAD position was\b"
    r"|HEAD is now at\b"
    r"|From\s+(?:https?://|git@|\S+\.git\b)"
    r"|\*\s*(?:branch|\[new branch\]|\[tag\])"
    r"|\d+\s+files?\s+changed\b"
    r"|[0-9a-f]{7,40}\.\.[0-9a-f]{7,40}\s"
    r"|[0-9a-f]{7,40}\s+(?:[A-Za-z]|\[)"
    r")"
)

#: Tokens that, on a *reflog echo* line, are prose — reported but never fatal.
_FAILURE_TOKENS: tuple[str, ...] = (
    "EXTENDED_FAILED",
    "EXTENDED_CASES_FAILED",
    "VALIDATION_FAILED",
    "STRESS_CASE_FAIL",
    "Wrong Answer:",
    "NON_DETERMINISTIC",
    "GATE_FAIL",
)

#: Structured, **line-anchored** failure verdicts. Only these may veto S1.
_ANCHORED_FAILURE_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("EXTENDED_FAILED", re.compile(r"^(?:\[[A-Z_]+\]\s*)?EXTENDED_FAILED\b")),
    ("EXTENDED_CASES_FAILED", re.compile(r"^(?:\[[A-Z_]+\]\s*)?EXTENDED_CASES_FAILED\b")),
    ("VALIDATION_FAILED", re.compile(r"^(?:\[[A-Z_]+\]\s*)?VALIDATION_FAILED\b")),
    ("STRESS_CASE_FAIL", re.compile(r"^(?:\[[A-Z_]+\]\s*)?STRESS_CASE_FAIL\b")),
    ("NON_DETERMINISTIC", re.compile(r"^(?:\[[A-Z_]+\]\s*)?NON_DETERMINISTIC\b")),
    ("GATE_FAIL", re.compile(r"^(?:\[[A-Z_]+\]\s*)?GATE_FAIL\b")),
    ("Wrong Answer:", re.compile(r"^Wrong Answer:")),
    # A non-passing extended suite is fatal even when `run-npu.sh` still prints VALIDATION_PASSED
    ("EXTENDED_RC", re.compile(r"^EXTENDED_RC=(?!0\b)\d+")),
)


def parse_log_verdicts(log_text: str) -> dict[str, Any]:
    """Line-anchored failure verdicts plus green-tail evidence for a `validation.log`.

    Semantics (see `SubmissionGate._check_log_failure_markers`):
      * only a line whose *first* token is the verdict can fail a log;
      * `git` reflog / fetch echoes are excluded (a commit subject may quote any token);
      * `VALIDATION_PASSED` / `EXTENDED_RC=0` is recorded as `green_tail` for diagnostics but
        **never** overrides an anchored failure (`run-npu.sh` prints it unconditionally).
    """
    failures: list[dict[str, Any]] = []
    echoes: list[dict[str, Any]] = []
    green_tail = False
    for lineno, raw in enumerate(log_text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if _GIT_ECHO_RE.match(line):
            if any(tok in line for tok in _FAILURE_TOKENS):
                echoes.append({"line": lineno, "text": line[:200]})
            continue
        if line.startswith("VALIDATION_PASSED") or re.match(r"^EXTENDED_RC=0\b", line):
            green_tail = True
        for token, rx in _ANCHORED_FAILURE_RES:
            if rx.match(line):
                failures.append({"token": token, "line": lineno, "text": line[:200]})
                break
    return {"failures": failures, "echo_tokens_skipped": echoes, "green_tail": green_tail}
_EXTENDED_PASS_RE = re.compile(r"EXTENDED_PASSED(?:\s+(\d+))?")
_STRESS_DONE_RE = re.compile(r"STRESS_DONE\s+runs=(\d+)\s+failures=(\d+)")
_GATE_BAD_RE = re.compile(r"GATE_BAD\b.*?\bbad=(\d+)/(\d+)")
_GATE_DET_RE = re.compile(r"GATE_DET\b.*?\bidentical=(\w+)")


class SubmissionGate:
    """Evaluates candidates across the 4-Stage Gate and manages Token-Bucket quota."""

    def __init__(
        self,
        workspace: pathlib.Path,
        estimator: ScoreEstimator | None = None,
        config: GateConfig | None = None,
        overrides: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.estimator = estimator or ScoreEstimator(self.workspace)
        self.config = config or GateConfig()
        self.overrides = overrides or {}
        self._prior_online_pass_shas = self._scan_prior_online_pass_shas()

    def _scan_prior_online_pass_shas(self) -> dict[tuple[str, str], datetime.datetime | None]:
        passed: dict[tuple[str, str], datetime.datetime | None] = {}
        for r in self.estimator.outcomes:
            if r.get("status") == "Pass" and (receipts.official_score(r) or 0.0) > 0:
                key = (r["title"], r.get("canonical_sha") or r.get("sha", ""))
                ts = _parse_iso(r.get("submitted_at") or r.get("finished_at"))
                if key not in passed or (ts and (passed[key] is None or ts < passed[key])):
                    passed[key] = ts

        mstab = self.workspace / "results" / "scoring-evidence" / "my-score-table.json"
        if mstab.is_file():
            try:
                for row in json.loads(mstab.read_text()):
                    if row.get("status") == "Pass" and row.get("sha"):
                        csha = resolve_sha(self.workspace, row["title"], row["sha"])
                        key = (row["title"], csha)
                        ts = _parse_iso(row.get("at"))
                        if key not in passed or (ts and (passed[key] is None or ts < passed[key])):
                            passed[key] = ts
            except Exception:
                pass
        return passed


    def _result_record(self, title: str, sha: str) -> tuple[dict[str, Any], str]:
        base = self.workspace / "results" / title
        if not base.is_dir():
            return {}, ""
        exact = base / sha / "result.json"
        if exact.is_file():
            try:
                return json.loads(exact.read_text()), str(exact.parent / "validation.log")
            except (OSError, ValueError):
                return {}, str(exact.parent / "validation.log")
        best: tuple[float, dict[str, Any], pathlib.Path] | None = None
        for cand in base.glob("*/result.json"):
            try:
                doc = json.loads(cand.read_text())
            except (OSError, ValueError):
                continue
            if sha and not str(doc.get("sha") or cand.parent.name).startswith(sha[:7]):
                continue
            ts = cand.stat().st_mtime
            if best is None or ts > best[0]:
                best = (ts, doc, cand.parent)
        if best is None:
            return {}, ""
        return best[1], str(best[2] / "validation.log")

    def _compile_ok(self, title: str, sha: str) -> tuple[bool, str]:
        """Probe submissions only require that the remote build/run really executed."""
        record, log_path = self._result_record(title, sha)
        return compile_evidence(record, pathlib.Path(log_path))

    def probe_allowance(self, title: str, sha: str = "", *, unsolved: bool = True,
                        as_of: datetime.datetime | None = None) -> dict[str, Any]:
        """Opening probes: the first acknowledged submissions may use compile-only evidence.

        4.3 (00-PLAN D1): with ``opening_until_first_score`` the window runs until the problem's
        first scored Pass, at most ``opening_max_probes`` judged receipts; switched off it is the
        4.2.1 fixed window of ``probe_submissions_per_problem``.
        """
        base = int(self.config.probe_submissions_per_problem)
        until_first = bool(self.config.opening_until_first_score) and base > 0
        bootstrap = int(self.config.opening_max_probes) if until_first else base
        scored = submit_intent.has_scored_pass(self.estimator.outcomes, title, as_of=as_of)
        used = submitted_attempts(self.workspace, title, self.estimator.outcomes)
        within_bootstrap = used < bootstrap and not (until_first and scored)
        allowed, why = self._compile_ok(title, sha) if within_bootstrap else (False, "opening probes used")
        return {
            "bootstrap": bootstrap,
            "used": used,
            "remaining": max(0, bootstrap - used),
            "bootstrap_stage": bool(within_bootstrap),
            "until_first_score": until_first,
            "first_score_seen": scored,
            "allowed": bool(allowed),
            "reason": why,
        }

    def _check_log_failure_markers(self, log_text: str) -> StageResult | None:
        """S1 log-verdict rule: structured, line-anchored, reflog-echo aware.

        Replaces the historical substring scan, which rejected
        `results/CTCLossV2/2b9d49d/validation.log` because a `git checkout` echo quoted the
        *previous* commit subject (`... coordinates on GATE_FAIL`) even though the log ended
        in `VALIDATION_PASSED` and the source scored `52.85 -> 56.21 (+3.36)` online.
        """
        verdicts = parse_log_verdicts(log_text)
        if verdicts["failures"]:
            first = verdicts["failures"][0]
            return StageResult(
                stage="SAFETY",
                passed=False,
                code="LOG_FAILURE_MARKER",
                reason=f"validation.log contains failure verdict '{first['token']}' (line {first['line']})",
                metrics={
                    "token": first["token"],
                    "line": first["line"],
                    "text": first["text"],
                    "failure_count": len(verdicts["failures"]),
                    "echo_tokens_skipped": len(verdicts["echo_tokens_skipped"]),
                    "green_tail": verdicts["green_tail"],
                },
            )
        return None

    def _check_kernel_cross_core_tokens(self, title: str, sha: str) -> list[str]:
        repo = self.workspace / (self.estimator.problems_meta.get(title, {}).get("path") or f"{_dev('work_prefix')}/{title}")
        if not (repo / ".git").exists() or not sha:
            return []
        ls_proc = subprocess.run(
            ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", sha, "op_kernel"],
            capture_output=True,
            text=True,
        )
        if ls_proc.returncode != 0:
            return []
        found: set[str] = set()
        for rel in ls_proc.stdout.splitlines():
            rel = rel.strip()
            if not rel.endswith((".h", ".cpp", ".cc", ".cxx")):
                continue
            show = subprocess.run(
                ["git", "-C", str(repo), "show", f"{sha}:{rel}"],
                capture_output=True,
                text=True,
            )
            if show.returncode != 0:
                continue
            src = show.stdout
            for tok in ("SetAtomicAdd", "SyncAll", "IBSet", "IBWait", "DataCacheCleanAndInvalid"):
                if re.search(rf"\b{tok}\b", src):
                    found.add(tok)
        return sorted(found)

    def _verify_git_commit_exists(self, title: str, sha: str) -> tuple[bool, str]:
        repo = self.workspace / (self.estimator.problems_meta.get(title, {}).get("path") or f"{_dev('work_prefix')}/{title}")
        if not (repo / ".git").exists():
            return True, "no_git_repo_fixture"
        cat = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-t", sha],
            capture_output=True,
            text=True,
        )
        if cat.returncode != 0 or cat.stdout.strip() != "commit":
            return False, f"commit {sha[:7]} not found in {title} git repository"

        tree = subprocess.run(
            ["git", "-C", str(repo), "ls-tree", "--name-only", sha],
            capture_output=True,
            text=True,
        )
        entries = set(tree.stdout.splitlines())
        if "kernel.asc" in entries and "CMakeLists.txt" in entries:
            return True, "template_tree_verified"
        if not ("op_host" in entries and "op_kernel" in entries):
            return False, f"commit {sha[:7]} missing op_host/ or op_kernel/ tree"
        return True, "git_tree_verified"

    def _escape_state(self, title: str, as_of: datetime.datetime | None = None) -> dict[str, Any]:
        """The calibrator's escape state, with the 4.2.1 TLE switch applied."""
        calibrator = self.estimator.get_calibrator()
        calibrator.tle_arms_escape_lock = bool(self.config.tle_arms_escape_lock)
        return calibrator.inspect_escape_state(title, as_of=as_of)

    @staticmethod
    def _tle_repeat(sha: str, state: dict[str, Any], proven: bool) -> StageResult | None:
        """4.2.1: a TLE no longer locks the problem, but the commit that timed out cannot go again unchanged."""
        tle_sha = str(state.get("tle_sha") or "")
        if not tle_sha or proven or not (sha.startswith(tle_sha) or tle_sha.startswith(sha)):
            return None
        return StageResult(
            stage="SAFETY", passed=False, code="TLE_SHA_REPEAT",
            reason=(f"{sha[:7]} timed out online (submission {state.get('tle_submission_id') or '?'}) and has "
                    "never passed online; change the kernel, or restore a commit that passed"),
            metrics={"tle_sha": tle_sha, "tle_submission_id": state.get("tle_submission_id"),
                     "next_step": "commit a faster or different kernel, or restore a proven Pass SHA"})

    def _escape_lock_stage(self, title: str, sha: str, state: dict[str, Any],
                           as_of: datetime.datetime | None, *, fix_releases: bool) -> StageResult:
        """4.3 P24: the escape lock's S1 decision — hold, release, refuse the bad SHA, or pass a fix.

        The reproducer state decides (``scheduler.wa_repro``): ``fixed`` clears the lock when the
        caller accepts it (the reference-scheme branch always did), ``unreproduced`` releases every
        other SHA and keeps the bad SHA out unchanged, and everything else holds as 4.2.1 did.
        """
        from . import wa_repro

        repro = wa_repro.status(self.workspace, title, state.get("escape_submission_id"),
                                state.get("escape_sha") or "", cand_sha=sha, as_of=as_of,
                                unrepro_runs=int(self.config.unrepro_runs))
        esc = wa_repro.escalation(repro, sha, release=bool(self.config.escape_unreproduced_releases),
                                  fix_releases=fix_releases,
                                  online_status=str(state.get("escape_status") or ""))
        metrics = dict(esc["metrics"])
        if esc["action"] == "pass":
            metrics.update(run_id=repro.get("fixed_run"), reproduced_run=repro.get("reproduced_run"))
        return StageResult(stage="SAFETY", passed=esc["action"] in ("pass", "release"), code=esc["code"],
                           reason=esc["reason"], metrics=metrics)

    def evaluate_safety(
        self,
        title: str,
        sha: str,
        as_of: datetime.datetime | None = None,
    ) -> StageResult:
        """Stage 1: Server validation exit=0, full extended suite, mismatch=0, cross-core determinism."""
        if not sha or not re.fullmatch(r"[0-9a-f]{40}", sha):
            return StageResult(
                stage="SAFETY",
                passed=False,
                code="UNRESOLVED_SHA",
                reason=f"SHA '{sha}' does not resolve to a 40-char validated commit",
            )

        val_dir = self.workspace / "results" / title / sha
        res_file = val_dir / "result.json"
        log_file = val_dir / "validation.log"

        if not res_file.is_file():
            in_flight = log_file.is_file()
            return StageResult(
                stage="SAFETY",
                passed=False,
                code="VALIDATION_INCOMPLETE" if in_flight else "NO_VALIDATION_RECORD",
                reason=(
                    f"Validation for {title} {sha[:7]} is still in-flight (no result.json)"
                    if in_flight
                    else f"No server validation record in results/{title}/{sha[:7]}/result.json"
                ),
            )

        try:
            res_data = json.loads(res_file.read_text())
        except Exception as exc:
            return StageResult(
                stage="SAFETY",
                passed=False,
                code="CORRUPT_RESULT_JSON",
                reason=f"Failed to parse result.json: {exc}",
            )

        exit_code = res_data.get("exit_code")
        if exit_code != 0:
            hint = " (SSH transport drop; re-run validation)" if exit_code == 255 else ""
            rid = res_data.get("run_id")
            if not hint and isinstance(rid, str) and re.fullmatch("[0-9a-f]{32}", rid):
                try:
                    report = json.loads((res_file.parent / "bench-runs" / rid / "report.json").read_text())
                    failed = [f"{c['id']} {c['status']}" for c in report.get("cases") or []
                              if c.get("status") not in ("PASS", "NOT_RUN")]
                    if failed:
                        hint = ": " + ", ".join(failed[:4]) + (" …" if len(failed) > 4 else "")
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            return StageResult(
                stage="SAFETY",
                passed=False,
                code=f"EXIT_CODE_{exit_code}",
                reason=f"Server validation failed with exit_code={exit_code}{hint}",
                metrics={"exit_code": exit_code},
            )

        if str(res_data.get("sha", "")).lower() != sha:
            return StageResult(
                stage="SAFETY",
                passed=False,
                code="SHA_MISMATCH",
                reason=f"result.json SHA {res_data.get('sha')} != requested {sha}",
            )

        git_ok, git_msg = self._verify_git_commit_exists(title, sha)
        if not git_ok:
            return StageResult(
                stage="SAFETY",
                passed=False,
                code="GIT_TREE_MISMATCH",
                reason=git_msg,
            )

        if not log_file.is_file():
            return StageResult(
                stage="SAFETY",
                passed=False,
                code="MISSING_LOG",
                reason="validation.log missing alongside result.json",
            )

        from .case_bench import formal_evidence, has_reference
        repo = self.workspace / (self.estimator.problems_meta.get(title, {}).get("path") or f"{_dev('work_prefix')}/{title}")
        if res_data.get("scheme") == "reference" or has_reference(repo,sha):
            report = formal_evidence(val_dir, repo, res_data)
            if not report:
                return StageResult(stage="SAFETY",passed=False,code="REFERENCE_SUITE_REQUIRED",
                                   reason="Run opsb test on this commit with the full current case profile")
            state = self._escape_state(title, as_of=as_of)
            prior = self._prior_online_pass_shas.get((title, sha))
            if state.get("escape_lock") and not (prior and (as_of is None or prior < as_of)):
                from .case_bench import read_doc
                from .case_feedback import trial_path
                sid = state.get("escape_submission_id")
                bad = state.get("escape_sha") or ""
                trial = read_doc(trial_path(val_dir.parent / bad, sid)) if sid and re.fullmatch(r"[a-f0-9]{40}", bad) and as_of is None else {}
                if trial.get("good_sha") != sha or trial.get("good_run") != report["run_id"]:
                    # WA reproducers (4.1 #6) clear a proven fix; 4.3 P24 releases an unreproduced lock.
                    return self._escape_lock_stage(title, sha, state, as_of, fix_releases=True)
            repeat = self._tle_repeat(sha, state, bool(prior and (as_of is None or prior < as_of)))
            if repeat is not None:
                return repeat
            from .judge_compile import stage1 as judge_compile_stage1
            rejected, compile_note = judge_compile_stage1(self.workspace, title, sha)
            if rejected:
                return StageResult(stage="SAFETY", passed=False, **rejected)
            return StageResult(stage="SAFETY",passed=True,code="PASS",
                               reason=f"Full reference suite passed ({len(report['cases'])} cases){compile_note}",
                               metrics={"run_id":report["run_id"],"extended_cases":len(report["cases"])})

        log_text = log_file.read_text(errors="replace")

        if "FAST_LANE=1" in log_text:
            return StageResult(
                stage="SAFETY",
                passed=False,
                code="FAST_LANE_ONLY",
                reason="Validated with FAST_LANE=1 subset only; run full Q1 validation before submitting",
            )

        marker_result = self._check_log_failure_markers(log_text)
        if marker_result is not None:
            return marker_result

        ext_matches = _EXTENDED_PASS_RE.findall(log_text)
        template_verified = res_data.get("layout") == "template" and res_data.get("verified") is True
        if not ext_matches and not template_verified:
            return StageResult(
                stage="SAFETY",
                passed=False,
                code="MISSING_EXTENDED_PASSED",
                reason="EXTENDED_PASSED marker absent from validation.log (silent rc=0 trap)",
            )
        ext_cases = max((int(x) for x in ext_matches if x and x.isdigit()), default=0)

        mismatches = [(int(a), int(b)) for a, b in _MISMATCH_RE.findall(log_text)]
        bad_mm = [(a, b) for a, b in mismatches if a > 0]
        if bad_mm:
            return StageResult(
                stage="SAFETY",
                passed=False,
                code="NONZERO_MISMATCH",
                reason=f"Sample/output mismatch detected: mismatched={bad_mm[0][0]}/{bad_mm[0][1]}",
            )

        for bad_n, tot_n in _GATE_BAD_RE.findall(log_text):
            if int(bad_n) > 0:
                return StageResult(
                    stage="SAFETY",
                    passed=False,
                    code="GATE_BAD_LANES",
                    reason=f"GATE_BAD reported {bad_n}/{tot_n} bad lanes",
                )

        det_matches = _GATE_DET_RE.findall(log_text)
        for ident in det_matches:
            if ident.lower() != "true":
                return StageResult(
                    stage="SAFETY",
                    passed=False,
                    code="NON_DETERMINISTIC_GATE",
                    reason=f"GATE_DET reported identical={ident}",
                )

        stress_runs = 0
        for s_runs, s_fail in _STRESS_DONE_RE.findall(log_text):
            stress_runs = max(stress_runs, int(s_runs))
            if int(s_fail) > 0:
                return StageResult(
                    stage="SAFETY",
                    passed=False,
                    code="STRESS_FAILURES",
                    reason=f"STRESS_DONE reported {s_fail}/{s_runs} failures",
                )

        has_det_tag = "DETERMINISTIC" in log_text or any(ident.lower() == "true" for ident in det_matches)
        has_det_or_stress = stress_runs > 0 or has_det_tag or "stress ok" in log_text

        cc_tokens = self._check_kernel_cross_core_tokens(title, sha)
        first_pass_ts = self._prior_online_pass_shas.get((title, sha))
        proven_prior = first_pass_ts is not None and (as_of is None or first_pass_ts < as_of)

        if cc_tokens and not has_det_or_stress and not proven_prior:
            return StageResult(
                stage="SAFETY",
                passed=False,
                code="MISSING_CROSS_CORE_STRESS",
                reason=(
                    f"Kernel uses cross-core primitive(s) {cc_tokens} without multi-run "
                    f"determinism/stress verification (STRESS_DONE / DETERMINISTIC / GATE_DET)"
                ),
                metrics={"cross_core_tokens": cc_tokens, "extended_cases": ext_cases},
            )

        # Closed-Loop Escape Lock: if the latest online submission escaped local S1 (`WA`/`TLE`/`RE`),
        # block unproven new commits until `verify-escape` confirms a separating regression test. 4.3 P24
        # releases the lock when the failure did not reproduce (an unreproduced verdict is judge noise).
        esc_state = self._escape_state(title, as_of=as_of)
        if esc_state.get("escape_lock") and not proven_prior:
            return self._escape_lock_stage(title, sha, esc_state, as_of, fix_releases=False)

        repeat = self._tle_repeat(sha, esc_state, proven_prior)
        if repeat is not None:
            return repeat

        return StageResult(
            stage="SAFETY",
            passed=True,
            code="SAFETY_OK",
            reason=(
                f"exit=0, EXTENDED_PASSED ({ext_cases or 'ok'} cases), mismatched=0"
                + (f", stress={stress_runs}" if stress_runs else "")
                + (", DET=True" if has_det_tag else "")
            ),
            metrics={
                "exit_code": 0,
                "extended_cases": ext_cases,
                "stress_runs": stress_runs,
                "deterministic_verified": has_det_or_stress,
                "cross_core_tokens": cc_tokens,
                "is_cross_core_op": title in CROSS_CORE_OPS,
            },
        )

    def get_circuit_breaker(
        self,
        title: str,
        as_of: datetime.datetime | None = None,
    ) -> CircuitBreakerStatus:
        """Stage 3 helper: evaluate 12h / 6-shot breaker using decay-adjusted contemporary peaks."""
        ref_now = as_of or datetime.datetime.now(datetime.timezone.utc)
        cutoff = ref_now - datetime.timedelta(hours=self.config.circuit_window_hours)
        ledger = self.estimator.get_problem_ledger(title, as_of=as_of)
        gamma_p = ledger.decay_factor_gamma
        anchor_ts = _parse_iso(ledger.active_judged_at) if ledger.current_score > 0 else ref_now
        if anchor_ts is None:
            anchor_ts = ref_now

        window_rows: list[tuple[datetime.datetime, str, float, float]] = []
        prior_contemp_best = -1.0
        raw_best_seen = 0.0

        for r in self.estimator.outcomes:
            if r.get("title") != title:
                continue
            st = str(r.get("status", ""))
            if st in ("rejected", "skipped", "duplicate", "sync_error"):
                continue
            ts = _parse_iso(r.get("submitted_at") or r.get("finished_at") or r.get("queued_at"))
            if ts is None or ts >= ref_now:
                continue
            raw_sc = (receipts.official_score(r) or 0.0) if st == "Pass" else 0.0
            if raw_sc > raw_best_seen:
                raw_best_seen = raw_sc

            # Project historical score to contemporary ref_now epoch so 8-day-old Nov 10 peaks
            # do not falsely penalize Nov 18 passes that beat the decayed baseline.
            contemp_sc = (
                self.estimator.discount_draw_to_contemporary(raw_sc, ts, gamma_p, anchor_ts)
                if st == "Pass" and raw_sc > 0
                else 0.0
            )
            if ts < cutoff:
                if st == "Pass" and contemp_sc > prior_contemp_best:
                    prior_contemp_best = contemp_sc
                continue
            window_rows.append((ts, st, raw_sc, contemp_sc))

        window_rows.sort(key=lambda x: x[0])
        best_contemp_in_win = prior_contemp_best
        non_improving = 0
        consec_zeros = 0
        for _ts, st, _raw_sc, contemp_sc in window_rows:
            if st == "Pass" and contemp_sc > best_contemp_in_win + 1e-4:
                best_contemp_in_win = contemp_sc
                non_improving = 0
                consec_zeros = 0
            else:
                non_improving += 1
                # The breaker counts failed verdicts only: a Pass that did not beat the board
                # (score only in `timing_score`) is a non-improving shot, not a zeroing one.
                if st != "Pass":
                    consec_zeros += 1
                else:
                    consec_zeros = 0

        disp_best = round(max(0.0, best_contemp_in_win), 2)
        limit = self.config.circuit_limit_shots
        if non_improving >= limit:
            state = "OPEN"
            reason = (
                f"{non_improving}/{limit} consecutive non-improving shots in last "
                f"{self.config.circuit_window_hours:.0f}h (contemp peak {disp_best:.2f}, gamma={gamma_p:.4f}) — SWITCH BASELINE"
            )
        elif consec_zeros >= 2:
            state = "HALF_OPEN"
            reason = f"{consec_zeros} consecutive zeroing failures ({window_rows[-1][1]}); only baseline restore allowed"
        else:
            state = "CLOSED"
            reason = (
                f"{non_improving}/{limit} non-improving shots since contemp peak {disp_best:.2f} "
                f"({len(window_rows)} total in {self.config.circuit_window_hours:.0f}h, gamma={gamma_p:.4f})"
            )

        return CircuitBreakerStatus(
            title=title,
            state=state,
            shots_in_window=len(window_rows),
            consecutive_non_improving=non_improving,
            consecutive_zero_errors=consec_zeros,
            window_best_score=disp_best,
            raw_window_best_score=round(raw_best_seen, 2),
            decay_factor_gamma=gamma_p,
            window_hours=self.config.circuit_window_hours,
            limit_shots=limit,
            reason=reason,
        )

    def get_quota_status(self, as_of: datetime.datetime | None = None) -> QuotaStatus:
        """Stage 4 helper: multi-account Beijing-day slot counts and pacing (closing count is display-only)."""
        ref_now = as_of or datetime.datetime.now(datetime.timezone.utc)
        bj_now = ref_now.astimezone(BEIJING_TZ)
        bj_midnight = bj_now.replace(hour=0, minute=0, second=0, microsecond=0)
        next_bj_midnight = bj_midnight + datetime.timedelta(days=1)

        eligible_slots = list_eligible_account_slots() if as_of is None else []
        slot_ids = [s["id"] for s in eligible_slots] if eligible_slots else ["primary"]
        slot_id_set = set(slot_ids)
        uid_to_aid = {s["user_id"]: s["id"] for s in eligible_slots if s.get("user_id")}
        per_acc_limit = self.config.daily_limit

        used_ids_by_aid: dict[str, set[str]] = {aid: set() for aid in slot_ids}
        last_ts_by_aid: dict[str, datetime.datetime | None] = {aid: None for aid in slot_ids}

        def _map_aid(raw_aid: Any, raw_uid: Any = None) -> str:
            aid = str(raw_aid or "")
            if aid in slot_id_set:
                return aid
            if raw_uid and str(raw_uid) in uid_to_aid:
                return uid_to_aid[str(raw_uid)]
            return "primary" if "primary" in slot_id_set else slot_ids[0]

        for r in self.estimator.outcomes:
            st = str(r.get("status", ""))
            if st in ("skipped", "duplicate"):
                continue
            aid = _map_aid(r.get("account_id"), r.get("user_id"))
            if st == "rejected":
                if "daily submission limit" in str(r.get("detail", "")) or "daily_limit" in str(r.get("detail", "")):
                    ts_r = _parse_iso(r.get("finished_at") or r.get("queued_at"))
                    if ts_r and bj_midnight <= ts_r.astimezone(BEIJING_TZ) < ref_now.astimezone(BEIJING_TZ):
                        used_ids_by_aid[aid].update(f"limit_fill_{i}" for i in range(per_acc_limit))
                continue
            if not r.get("submission_id"):
                continue
            ts = _parse_iso(r.get("submitted_at") or r.get("finished_at"))
            if ts is None or ts >= ref_now:
                continue
            cur_last = last_ts_by_aid[aid]
            if cur_last is None or ts > cur_last:
                last_ts_by_aid[aid] = ts
            if ts.astimezone(BEIJING_TZ) >= bj_midnight:
                sid = str(r["submission_id"])
                used_ids_by_aid[aid].add(sid)

        if as_of is None:
            for rec_file in (self.workspace / "results").glob("*/*/submission-receipt.json"):
                try:
                    rdata = json.loads(rec_file.read_text())
                    ts = _parse_iso(rdata.get("submitted_at"))
                    if ts and ts <= ref_now:
                        aid = _map_aid(rdata.get("account_id"), rdata.get("user_id"))
                        cur_last = last_ts_by_aid[aid]
                        if cur_last is None or ts > cur_last:
                            last_ts_by_aid[aid] = ts
                        if ts.astimezone(BEIJING_TZ) >= bj_midnight and rdata.get("submission_id"):
                            used_ids_by_aid[aid].add(str(rdata["submission_id"]))
                except Exception:
                    continue

        used_per_aid = {aid: min(per_acc_limit, len(used_ids_by_aid[aid])) for aid in slot_ids}
        rem_per_aid = {aid: max(0, per_acc_limit - used_per_aid[aid]) for aid in slot_ids}
        total_daily_limit = per_acc_limit * len(slot_ids)
        used_today = sum(used_per_aid.values())
        remaining_today = sum(rem_per_aid.values())

        # Identify genuine closing restore targets using decay-discounted `true_restore_gain`!
        restore_targets: list[dict[str, Any]] = []
        for title in sorted(self.estimator.problems_meta):
            ledger = self.estimator.get_problem_ledger(title, as_of=as_of)
            if ledger.historical_best_sha is None:
                continue
            # Never mark as restore target if historical_best_sha IS ALREADY active on board!
            if ledger.current_score > 0.0 and ledger.historical_best_sha == ledger.active_board_sha:
                continue

            needs_zero_fix = ledger.current_score <= 0.0
            needs_gap_fix = ledger.true_restore_gain >= self.config.noise_floor_pts
            if needs_zero_fix or needs_gap_fix:
                gain = ledger.contemporary_best_score if needs_zero_fix else ledger.true_restore_gain
                restore_targets.append({
                    "title": title,
                    "best_sha": ledger.historical_best_sha,
                    "active_board_sha": ledger.active_board_sha,
                    "current_score": ledger.current_score,
                    "raw_historical_best_score": ledger.raw_historical_best_score,
                    "contemporary_best_score": ledger.contemporary_best_score,
                    "decay_factor_gamma": ledger.decay_factor_gamma,
                    "true_restore_gain": round(gain, 2),
                    "expected_restore_score": ledger.contemporary_best_score,
                    "expected_gain": round(gain, 2),
                    "weighted_gain": round(ledger.difficulty * gain, 2),
                    "critical": needs_zero_fix,
                })
        restore_targets.sort(
            key=lambda x: (
                0 if x["critical"] else 1,
                -float(x["weighted_gain"]),
                str(x["title"]),
                str(x.get("best_sha") or ""),
            )
        )

        # Closing-restore count for display (needed restores + 1, floored by min_closing_reserve).
        # It never holds or reorders a candidate: daily allowance is not a gate threshold.
        reserve_cap = max(12, self.config.min_closing_reserve)
        reserved = min(reserve_cap, max(self.config.min_closing_reserve, len(restore_targets) + (1 if restore_targets else 0)))
        discretionary = max(0, remaining_today - reserved)

        # Effective cooldown = minimum wait across non-exhausted eligible accounts (V3-012)
        non_exhausted = [aid for aid in slot_ids if rem_per_aid[aid] > 0]
        if non_exhausted:
            best_wait: int | None = None
            best_since: int | None = None
            for aid in non_exhausted:
                lts = last_ts_by_aid[aid]
                if lts is None:
                    best_wait = 0
                    best_since = None
                    break
                since_i = max(0, int((ref_now - lts).total_seconds()))
                wait_i = max(0, self.config.rate_limit_gap_sec - since_i)
                if best_wait is None or wait_i < best_wait:
                    best_wait = wait_i
                    best_since = since_i
            rate_wait = best_wait if best_wait is not None else 0
            since_last = best_since
        else:
            all_ts = [t for t in last_ts_by_aid.values() if t is not None]
            since_last = max(0, int((ref_now - max(all_ts)).total_seconds())) if all_ts else None
            rate_wait = 0

        return QuotaStatus(
            daily_limit=total_daily_limit,
            used_today=used_today,
            remaining_today=remaining_today,
            needed_closing_restores=len(restore_targets),
            reserved_for_closing=reserved,
            discretionary_remaining=discretionary,
            closing_restore_targets=restore_targets,
            rate_limit_gap_sec=self.config.rate_limit_gap_sec,
            seconds_since_last_submit=since_last,
            rate_wait_sec=rate_wait,
            beijing_date=bj_now.strftime("%Y-%m-%d"),
            next_reset_utc=next_bj_midnight.astimezone(datetime.timezone.utc).isoformat(),
        )

    def _find_override(self, title: str, canonical_sha: str) -> dict[str, Any]:
        """Lookup operator override by exact `<title>:<sha>` or 7-char SHA prefix (case-insensitive)."""
        if not self.overrides or not title or not canonical_sha:
            return {}
        c_low = canonical_sha.lower()
        short_low = c_low[:7]
        exact_key = f"{title}:{canonical_sha}"
        if exact_key in self.overrides:
            return self.overrides[exact_key]
        prefix_match: dict[str, Any] = {}
        for k, v in self.overrides.items():
            if ":" not in k or not isinstance(v, dict):
                continue
            kt, ksha = k.split(":", 1)
            if kt != title:
                continue
            k_low = ksha.lower()
            if k_low == c_low:
                return v
            if len(short_low) >= 7 and len(k_low) >= 7 and k_low[:7] == short_low:
                prefix_match = v
        return prefix_match

    def evaluate(self, *args, **kwargs):
        """Public entry: evaluate, then durably record the verdict for later analysis."""
        verdict = self._evaluate_impl(*args, **kwargs)
        try:
            from .feedback_log import log_feedback_on_change

            # Status refreshes re-evaluate the same candidate constantly; only a changed
            # verdict is worth a log line.
            stage_codes = [s.code for s in verdict.stages]
            log_feedback_on_change(
                self.workspace,
                "gate",
                f"{verdict.title}:{verdict.sha[:7]}",
                json.dumps([verdict.status_tag, verdict.approved, stage_codes, verdict.gate_reason], sort_keys=True),
                title=verdict.title,
                sha=verdict.sha[:7],
                status=verdict.status_tag,
                approved=verdict.approved,
                stages=stage_codes,
                reason=verdict.gate_reason,
                probe=next((s.metrics for s in verdict.stages if s.code == "PROBE_ALLOWANCE"), None),
            )
        except Exception:
            pass
        return verdict

    def _apply_health(self, title: str, sha: str, board_sha: str, s1: StageResult, *,
                      probe: bool = False) -> StageResult:
        """4.3 P24: attach the S1 mechanical health and hold an exploit whose judge time is at risk.

        Warnings never hold: they only scale the priority (``health_penalty``). The judge time risk is
        the one new hold of 4.3 (00-PLAN D3), and only for an exploit: an approved ``intent=probe``
        (P23) is the documented way to send such a shot anyway, so for a probe the risk is a hint.
        ``health_time_risk_holds`` turns it back into a hint for every intent; the opening probe
        lane bypasses S1 entirely.
        """
        if not self.config.health_checks:
            return s1
        from . import health

        try:
            view = health.check(self.workspace, title, sha, board_sha,
                                compile_factor=float(self.config.judge_compile_factor),
                                time_margin=float(self.config.health_time_margin))
        except Exception as exc:  # noqa: BLE001 - a health fault never decides a submission
            view = {"checked": False, "skipped": f"{type(exc).__name__}: {exc}"}
        s1.metrics["health"] = view
        risk = view.get("time_risk") or {}
        if not (s1.passed and risk.get("risk")):
            return s1
        step = health.next_step(view)
        if probe or not self.config.health_time_risk_holds:  # a hint only, never a hold
            s1.reason = f"{s1.reason}; judge time risk: {step}"
            return s1
        return StageResult(stage="SAFETY", passed=False, code="JUDGE_TIME_RISK",
                           reason=f"Judge time risk: {step}",
                           metrics={**s1.metrics, "health": view, "judge_time_risk": risk,
                                    "next_step": step})

    def _slot_stage(self, quota: QuotaStatus) -> StageResult:
        """Stage 4: whether an account slot is free right now.

        It is resource timing, not a threshold: a failed stage only delays dispatch (the queue
        shows it as waiting for a slot) and never withholds approval or holds a candidate.
        """
        if quota.remaining_today <= 0:
            return StageResult(
                stage="QUOTA_RESERVE",
                passed=False,
                code="DAILY_LIMIT_REACHED",
                reason=(
                    f"no account slot is free until the reset at {quota.next_reset_utc} "
                    f"({quota.used_today}/{quota.daily_limit} used today)"
                ),
                metrics={"remaining": 0},
            )
        return StageResult(
            stage="QUOTA_RESERVE",
            passed=True,
            code="QUOTA_OK",
            reason=f"{quota.remaining_today} submission slot(s) free today ({quota.used_today}/{quota.daily_limit} used)",
            metrics={"remaining": quota.remaining_today},
        )

    def _keeps_best(self, title: str) -> bool:
        """4.2.1: True when the board keeps the problem's best score (``best``), False under ``latest``."""
        if not self.config.keep_best_board:
            return False
        if not self.config.board_mode_from_rules:
            return True
        meta = self.estimator.problems_meta.get(title) or {}
        return str(meta.get("ranking_submission_mode") or "").strip().lower() != "latest"

    def _opening_hold(self, title: str, sha: str, as_of: datetime.datetime | None = None) -> StageResult | None:
        """A lock or identity refusal still stops an opening probe (4.3 D1).

        The opening window substitutes *evidence completeness* (a remote build that ran instead of
        the full extended suite). It never forgives an escape lock, a TLE re-submit, an unresolved
        or mismatched SHA, or a corrupt result: those are facts about the SHA, not a missing
        measurement.
        """
        safety = self.evaluate_safety(title, sha, as_of=as_of)
        if not safety.passed and safety.code in OPENING_HARD_CODES:
            return safety
        return None

    def _last_judged_status(self, title: str, as_of: datetime.datetime | None = None) -> str:
        """Status of the problem's last judged receipt: a failed verdict arms the restore path."""
        last: tuple[datetime.datetime, str] | None = None
        for row in self.estimator.outcomes:
            if str(row.get("title") or "") != title or not receipts.judged(row):
                continue
            when = _parse_iso(row.get("submitted_at") or row.get("finished_at"))
            if when is None or (as_of is not None and when >= as_of):
                continue
            if last is None or when >= last[0]:
                last = (when, str(row.get("status") or ""))
        return last[1] if last else ""

    def _noise_floor(self, title: str) -> tuple[float, dict[str, Any] | None]:
        """4.2.1: ``clamp(sigma_hat, floor, 2 * floor)`` from repeated judgments, else the configured floor."""
        base = float(self.config.noise_floor_pts)
        if not self.config.noise_floor_from_judge_sigma:
            return base, None
        try:
            from . import judge_noise
            noise = judge_noise.sigma_for(self.workspace, title)
        except Exception:
            noise = None
        if not noise:
            return base, None
        return round(min(2.0 * base, max(base, float(noise["sigma"]))), 2), noise

    def _s2_metrics(self, estimate: ScoreEstimate, floor: float | None = None,
                    noise: dict[str, Any] | None = None, keeps_best: bool = True) -> dict[str, Any]:
        return {
            "board_mode": "best" if keeps_best else "latest",
            "judge_noise": noise,
            "delta_expected": estimate.delta_expected,
            "expected_improvement": estimate.expected_improvement,
            "prob_positive": estimate.prob_positive,
            "ucb_delta": estimate.ucb_delta,
            "sigma": estimate.confidence_sigma,
            "calibration_trust": estimate.calibration_trust,
            "calibration_samples": estimate.calibration_samples,
            "calibration_bias": estimate.calibration_bias,
            "threshold": self.config.noise_floor_pts if floor is None else floor,
            "prediction_interval": estimate.prediction_interval,
        }

    def _evaluate_impl(
        self,
        title: str,
        sha: str | None = None,
        speedup: float | None = None,
        note: str = "",
        force: bool = False,
        as_of: datetime.datetime | None = None,
        override_board_score: float | None = None,
        override_board_status: str | None = None,
        intent: str = "exploit",
        probe_used: int | None = None,
    ) -> GateVerdict:
        """Run all 4 stages (SAFETY -> NOISE_FLOOR -> CIRCUIT_BREAKER -> QUOTA_RESERVE).

        ``intent`` (4.3 P23) is ``exploit`` (the 4.2.1 gate), ``probe`` or ``restore``;
        ``probe_used`` lets the replay price a hypothetical probe series against the budget.
        """
        canonical_sha = resolve_sha(self.workspace, title, sha)
        ov = self._find_override(title, canonical_sha)
        ov_action = ov.get("action")
        explicit_skip = (ov_action == "skip") and (not force)
        effective_force = bool(force or (ov_action == "approve"))

        estimate = self.estimator.estimate(
            title=title,
            sha=canonical_sha,
            speedup=speedup,
            note=note,
            as_of=as_of,
            override_board_score=override_board_score,
            override_board_status=override_board_status,
            # 4.3 P29: measured judge jitter (on) and the two-tier cross-generation prediction (off).
            jitter_from_noise=self.config.jitter_sigma_from_judge_noise,
            ranking_v2=self.config.ranking_v2,
        )
        ledger = self.estimator.get_problem_ledger(
            title,
            as_of=as_of,
            override_board_score=override_board_score,
            override_board_status=override_board_status,
        )
        cb = self.get_circuit_breaker(title, as_of=as_of)
        quota = self.get_quota_status(as_of=as_of)
        keeps_best = self._keeps_best(title)  # 4.2.1: the contest's own board rule
        floor, noise = self._noise_floor(title)  # 4.2.1: measured judge noise

        # 4.3 P23: what kind of shot this is. `exploit` leaves the 4.2.1 gate exactly as it was; a
        # probe or restore either gets its own S2 rule or is handed back to the exploit evaluation
        # with the reason (a speculative request is never refused outright).
        requested_intent, intent_source = submit_intent.resolve_intent(
            self.workspace, title, canonical_sha, intent)
        intent_decision: submit_intent.Decision | None = None
        if requested_intent != "exploit":
            intent_decision = submit_intent.plan(
                intent=requested_intent, requested=requested_intent, estimator=self.estimator,
                ledger=ledger, estimate=estimate, title=title, sha=canonical_sha,
                difficulty=int(estimate.difficulty), floor=floor, keeps_best=keeps_best,
                config=self.config, outcomes=self.estimator.outcomes,
                last_status=self._last_judged_status(title, as_of=as_of),
                as_of=as_of, probe_used=probe_used)

        is_critical_restore = (
            ledger.confirmed_board_zero
            and bool(ledger.historical_best_sha)
            and canonical_sha.lower().startswith(ledger.historical_best_sha.lower()[:7])
        )
        if intent_decision is not None and intent_decision.critical and intent_decision.approved:
            # 4.3: a restore approved because the board is open or the last shot failed is the
            # `is_critical_restore` recovery case, no longer limited to "board confirmed zero".
            is_critical_restore = True
        is_already_active_on_board = (
            as_of is None
            and override_board_score is None
            and not is_critical_restore
            and speedup is None
            and estimate.estimation_method == "active_board_identity"
        )
        ref_best = float(ledger.contemporary_best_score or ledger.raw_historical_best_score or 0.0)
        is_restore_regime = (
            keeps_best
            and (not is_critical_restore)
            and bool(ledger.historical_best_sha)
            and (ref_best - estimate.current_score >= 1.5)
        )
        is_restoring_best = bool(
            ledger.historical_best_sha
            and canonical_sha.lower().startswith(ledger.historical_best_sha.lower()[:7])
        )
        delta_vs_best = round(estimate.estimated_score - ref_best, 2)
        ucb_vs_best = round(
            estimate.estimated_score + max(0.0, estimate.ucb_delta - estimate.delta_expected) - ref_best,
            2,
        )
        ev = estimate.explore_evidence or {}
        restore_explore_ok = (
            delta_vs_best > 0.0
            and ucb_vs_best >= floor
            and not bool(ev.get("veto_pos_corr"))
            and not bool(ev.get("veto_small_core"))
        )

        # Acknowledged submission IDs define the opening window (D1: until the first score).
        probe = self.probe_allowance(title, canonical_sha, unsolved=is_critical_restore, as_of=as_of)
        if as_of is None and probe["allowed"] and not explicit_skip:
            hold = self._opening_hold(title, canonical_sha, as_of=as_of)
            if hold is not None:
                probe = {**probe, "allowed": False, "reason": hold.reason, "opening_hold": hold.code}
        if as_of is None and probe["allowed"] and not explicit_skip:
            slot = self._slot_stage(quota)
            stages = [
                StageResult(
                    stage="SAFETY", passed=True, code="PROBE_ALLOWANCE",
                    reason=(
                        f"Probe {probe['used'] + 1}/{probe['bootstrap']}: {probe['reason']}"
                    ),
                    metrics=dict(probe),
                ),
                StageResult(stage="NOISE_FLOOR", passed=True, code="PROBE_ALLOWANCE",
                            reason="Probe submissions are not gated on score improvement",
                            metrics={"delta_expected": estimate.delta_expected,
                                     "threshold": floor}),
                StageResult(stage="CIRCUIT_BREAKER", passed=True, code="PROBE_ALLOWANCE",
                            reason="Circuit breaker is not armed for opening probes",
                            metrics={"remaining": probe["remaining"]}),
                StageResult(stage="QUOTA_RESERVE", passed=True, code="PROBE_ALLOWANCE",
                            reason="Opening probes dispatch on the next free account slot",
                            metrics={"remaining": probe["remaining"]}) if slot.passed else slot,
            ]
            return GateVerdict(
                title=title,
                sha=canonical_sha,
                short_sha=canonical_sha[:7],
                approved=True,
                status_tag="PROBE",
                gate_reason=(
                    (
                        f"Probe {probe['used'] + 1}/{probe['bootstrap']} for {title}: {probe['reason']}"
                    )
                ),
                priority_score=round(500.0 + estimate.difficulty, 2),
                is_critical_restore=False,
                forced=False,
                decay_factor_gamma=estimate.decay_factor_gamma,
                raw_historical_best_score=float(ledger.raw_historical_best_score or 0.0),
                contemporary_best_score=float(ledger.contemporary_best_score or 0.0),
                true_restore_gain=0.0,
                estimate=estimate,
                stages=stages,
                circuit_breaker=cb,
                quota=quota,
                requested_intent=requested_intent,
                intent_reason="opening window: compile-only evidence, no intent needed",
            )

        # Stage 1: SAFETY (4.3 P24: plus the mechanical health and the judge time-risk hold)
        board_sha = str(getattr(ledger, "active_board_sha", "") or "")
        probe_accepted = bool(intent_decision is not None and intent_decision.intent == "probe"
                              and intent_decision.approved)
        s1 = self._apply_health(title, canonical_sha, board_sha,
                                self.evaluate_safety(title, canonical_sha, as_of=as_of),
                                probe=probe_accepted)
        escape_released = s1.code == "ESCAPE_UNREPRODUCED"
        health_warnings = bool(((s1.metrics or {}).get("health") or {}).get("warnings"))

        # Stage 2: NOISE_FLOOR (Uncertainty-Aware with `EXPLORE` Anti-Burial Channel)
        is_explore_pass = False
        is_ei_pass = False
        is_low_gain_pass = False
        is_probe_pass = False
        if explicit_skip:
            s2 = StageResult(
                stage="NOISE_FLOOR",
                passed=False,
                code="OPERATOR_SKIPPED",
                reason=f"Skipped by operator override: {ov.get('reason', 'manual skip')}",
                metrics={"delta_expected": estimate.delta_expected, "threshold": floor},
            )
        elif is_critical_restore:
            s2 = StageResult(
                stage="NOISE_FLOOR",
                passed=True,
                code="BYPASS_CRITICAL_RESTORE",
                reason=(
                    f"Board is '{estimate.board_status}' (score={estimate.current_score:.2f}); "
                    f"CRITICAL_RESTORE bypasses +{floor:.1f}pt floor "
                    f"(contemp est +{estimate.delta_expected:.2f} pts)"
                ),
                metrics={"delta_expected": estimate.delta_expected, "threshold": floor},
            )
        elif intent_decision is not None and intent_decision.approved is not None:
            # 4.3 P23: the probe or restore rule decided this shot, not the 4.2.1 noise floor.
            is_probe_pass = intent_decision.intent == "probe" and bool(intent_decision.approved)
            is_explore_pass = bool(intent_decision.approved and intent_decision.explore)
            is_low_gain_pass = is_explore_pass
            s2 = StageResult(
                stage="NOISE_FLOOR",
                passed=bool(intent_decision.approved),
                code=intent_decision.code,
                reason=intent_decision.reason,
                metrics={**self._s2_metrics(estimate, floor, noise, keeps_best),
                         **intent_decision.metrics,
                         "next_step": intent_decision.metrics.get("next_step", "")},
            )
        elif is_already_active_on_board and ov_action != "approve":
            s2 = StageResult(
                stage="NOISE_FLOOR",
                passed=False,
                code="ALREADY_ACTIVE_ON_BOARD",
                reason=(
                    f"SHA {estimate.short_sha} is already the active board submission "
                    f"(S_live={estimate.current_score:.2f}, gamma={estimate.decay_factor_gamma:.4f}); "
                    f"restore_gain=0.00 pts"
                ),
                metrics={"delta_expected": 0.0, "threshold": floor},
            )
        elif effective_force and estimate.delta_expected >= 0.0:
            s2 = StageResult(
                stage="NOISE_FLOOR",
                passed=True,
                code="FORCED_APPROVAL",
                reason=f"Explicit force/override approved (contemp delta={estimate.delta_expected:+.2f} pts)",
                metrics={"delta_expected": estimate.delta_expected, "threshold": floor},
            )
        elif is_restore_regime and not is_restoring_best and not effective_force:
            if delta_vs_best >= floor:
                s2 = StageResult(
                    stage="NOISE_FLOOR",
                    passed=True,
                    code="ABOVE_NOISE_FLOOR",
                    reason=(
                        f"New-best gain vs best({(ledger.historical_best_sha or '')[:7]}={ref_best:.2f}): "
                        f"delta_vs_best={delta_vs_best:+.2f} pts >= +{floor:.2f} pts "
                        f"(sigma={estimate.confidence_sigma:.2f}, UCB_vs_best={ucb_vs_best:+.2f})"
                    ),
                    metrics={
                        "delta_expected": estimate.delta_expected,
                        "ucb_delta": estimate.ucb_delta,
                        "sigma": estimate.confidence_sigma,
                        "calibration_trust": estimate.calibration_trust,
                        "threshold": floor,
                        "ref_best": ref_best,
                        "delta_vs_best": delta_vs_best,
                        "ucb_vs_best": ucb_vs_best,
                    },
                )
            elif restore_explore_ok:
                is_explore_pass = True
                explore_msg = estimate.explore_reason or (
                    f"new-best upside in [RESTORE] (est={estimate.estimated_score:.2f} > "
                    f"best={ref_best:.2f}, UCB_vs_best={ucb_vs_best:+.2f} >= +{floor:.2f})"
                )
                s2 = StageResult(
                    stage="NOISE_FLOOR",
                    passed=True,
                    code="EXPLORE_HIGH_UPSIDE",
                    reason=f"EXPLORE [HIGH-UNCERTAINTY UPSIDE]: {explore_msg}",
                    metrics={
                        "delta_expected": estimate.delta_expected,
                        "ucb_delta": estimate.ucb_delta,
                        "sigma": estimate.confidence_sigma,
                        "calibration_trust": estimate.calibration_trust,
                        "threshold": floor,
                        "ref_best": ref_best,
                        "delta_vs_best": delta_vs_best,
                        "ucb_vs_best": ucb_vs_best,
                        "explore_evidence": estimate.explore_evidence,
                    },
                )
            else:
                s2 = StageResult(
                    stage="NOISE_FLOOR",
                    passed=False,
                    code="BELOW_BEST_IN_RESTORE",
                    reason=(
                        f"In [RESTORE] regime (best={ref_best:.2f} vs board={estimate.current_score:.2f}): "
                        f"est={estimate.estimated_score:.2f} gives delta_vs_best={delta_vs_best:+.2f}, "
                        f"UCB_vs_best={ucb_vs_best:+.2f} < +{floor:.2f}; "
                        f"holding to avoid eating cur->best delta"
                    ),
                    metrics={
                        "delta_expected": estimate.delta_expected,
                        "ucb_delta": estimate.ucb_delta,
                        "sigma": estimate.confidence_sigma,
                        "calibration_trust": estimate.calibration_trust,
                        "threshold": floor,
                        "ref_best": ref_best,
                        "delta_vs_best": delta_vs_best,
                        "ucb_vs_best": ucb_vs_best,
                    },
                )
        elif estimate.delta_expected >= floor:
            s2 = StageResult(
                stage="NOISE_FLOOR",
                passed=True,
                code="ABOVE_NOISE_FLOOR",
                reason=(
                    f"Expected gain {estimate.delta_expected:+.2f} pts >= +{floor:.2f} pts "
                    f"(sigma={estimate.confidence_sigma:.2f}, UCB={estimate.ucb_delta:+.2f}, "
                    f"P(gain>0)={estimate.prob_positive * 100:.0f}%)"
                ),
                metrics={
                    "delta_expected": estimate.delta_expected,
                    "ucb_delta": estimate.ucb_delta,
                    "sigma": estimate.confidence_sigma,
                    "calibration_trust": estimate.calibration_trust,
                    "threshold": floor,
                },
            )
        elif estimate.explore_eligible and not (ov_action == "skip"):
            is_explore_pass = True
            s2 = StageResult(
                stage="NOISE_FLOOR",
                passed=True,
                code="EXPLORE_HIGH_UPSIDE",
                reason=f"EXPLORE [HIGH-UNCERTAINTY UPSIDE]: {estimate.explore_reason}",
                metrics={
                    "delta_expected": estimate.delta_expected,
                    "ucb_delta": estimate.ucb_delta,
                    "sigma": estimate.confidence_sigma,
                    "calibration_trust": estimate.calibration_trust,
                    "threshold": floor,
                    "explore_evidence": estimate.explore_evidence,
                },
            )
        elif effective_force and estimate.delta_expected < 0.0:
            if ov_action == "approve":
                s2 = StageResult(
                    stage="NOISE_FLOOR",
                    passed=True,
                    code="OPERATOR_OVERRIDE",
                    reason=f"Operator explicitly overrode negative expected delta ({estimate.delta_expected:+.2f} pts)",
                    metrics={"delta_expected": estimate.delta_expected, "threshold": floor},
                )
            else:
                s2 = StageResult(
                    stage="NOISE_FLOOR",
                    passed=False,
                    code="NEGATIVE_EXPECTED_RESTORE",
                    reason=(
                        f"Decay-adjusted delta {estimate.delta_expected:+.2f} pts is negative "
                        f"(contemp {estimate.estimated_score:.2f} < board {estimate.current_score:.2f}, "
                        f"UCB={estimate.ucb_delta:+.2f}, trust={estimate.calibration_trust:.2f}, "
                        f"gamma={estimate.decay_factor_gamma:.4f})"
                    ),
                    metrics={
                        "delta_expected": estimate.delta_expected,
                        "ucb_delta": estimate.ucb_delta,
                        "sigma": estimate.confidence_sigma,
                        "calibration_trust": estimate.calibration_trust,
                        "threshold": floor,
                    },
                )
        elif (
            keeps_best
            and estimate.expected_improvement >= floor
            and estimate.prob_positive >= self.config.min_prob_positive
        ):
            is_ei_pass = True
            s2 = StageResult(
                stage="NOISE_FLOOR",
                passed=True,
                code="EXPECTED_IMPROVEMENT",
                reason=(
                    f"Expected improvement {estimate.expected_improvement:+.2f} pts >= "
                    f"+{floor:.2f} pts (gain {estimate.delta_expected:+.2f}, "
                    f"sigma={estimate.confidence_sigma:.2f}, P(gain>0)={estimate.prob_positive:.2f}, "
                    f"confidence={estimate.calibration_trust:.2f} from {estimate.calibration_samples:g} receipt(s)); "
                    f"the board keeps the best score"
                ),
                metrics=self._s2_metrics(estimate, floor, noise, keeps_best),
            )
        elif (
            self.config.approve_low_gain_positive
            and estimate.delta_expected > 0.0
            and estimate.prob_positive >= self.config.low_gain_min_prob
        ):
            is_low_gain_pass = True
            s2 = StageResult(
                stage="NOISE_FLOOR",
                passed=True,
                code="LOW_GAIN_POSITIVE",
                reason=(
                    f"Expected gain {estimate.delta_expected:+.2f} pts is below the +{floor:.2f} pt noise floor "
                    f"but more likely up than down (P(gain>0)={estimate.prob_positive:.2f}); approved at low priority"
                ),
                metrics=self._s2_metrics(estimate, floor, noise, keeps_best),
            )
        else:
            low_odds = estimate.prob_positive < self.config.min_prob_positive
            s2 = StageResult(
                stage="NOISE_FLOOR",
                passed=False,
                code="BELOW_NOISE_FLOOR",
                reason=(
                    f"Expected improvement {estimate.expected_improvement:+.2f} pts < "
                    f"+{floor:.2f} pts (gain {estimate.delta_expected:+.2f}, "
                    f"sigma={estimate.confidence_sigma:.2f}, P(gain>0)={estimate.prob_positive:.2f}, "
                    f"confidence={estimate.calibration_trust:.2f} from {estimate.calibration_samples:g} receipt(s))"
                    + ("; gain is unlikely" if low_odds else "")
                    + ("" if keeps_best else "; the board shows the latest submission")
                ),
                metrics=self._s2_metrics(estimate, floor, noise, keeps_best),
            )

        # Stage 3: CIRCUIT_BREAKER (4.2.1: advisory unless GateConfig.circuit_blocks)
        circuit_advisory = False
        if is_critical_restore:
            s3 = StageResult(
                stage="CIRCUIT_BREAKER",
                passed=True,
                code="HALF_OPEN_CRITICAL_RESTORE",
                reason="CRITICAL_RESTORE permitted through circuit breaker to recover zeroed board",
                metrics=cb.to_dict(),
            )
        elif ov_action == "approve":
            s3 = StageResult(
                stage="CIRCUIT_BREAKER",
                passed=True,
                code="OVERRIDE_CIRCUIT",
                reason="Operator override bypassed circuit breaker",
                metrics=cb.to_dict(),
            )
        elif cb.state == "OPEN" and self.config.circuit_blocks:
            s3 = StageResult(
                stage="CIRCUIT_BREAKER",
                passed=False,
                code="CIRCUIT_OPEN_SWITCH_BASELINE",
                reason=cb.reason,
                metrics=cb.to_dict(),
            )
        elif cb.state == "HALF_OPEN" and self.config.circuit_blocks and (title, canonical_sha) not in self._prior_online_pass_shas:
            s3 = StageResult(
                stage="CIRCUIT_BREAKER",
                passed=False,
                code="HALF_OPEN_UNPROVEN_SHA",
                reason=cb.reason,
                metrics=cb.to_dict(),
            )
        elif cb.state == "OPEN" or (cb.state == "HALF_OPEN" and (title, canonical_sha) not in self._prior_online_pass_shas):
            circuit_advisory = True
            s3 = StageResult(
                stage="CIRCUIT_BREAKER",
                passed=True,
                code="CIRCUIT_OPEN_ADVISORY" if cb.state == "OPEN" else "HALF_OPEN_ADVISORY",
                reason=f"{cb.reason}; advisory only: approved at half priority, try an orthogonal direction",
                metrics=cb.to_dict(),
            )
        else:
            s3 = StageResult(
                stage="CIRCUIT_BREAKER",
                passed=True,
                code="CIRCUIT_CLOSED",
                reason=cb.reason,
                metrics=cb.to_dict(),
            )

        # Stage 4: dispatch slot (orders dispatch, never withholds approval)
        s4 = self._slot_stage(quota)

        stages = [with_next_step(s) for s in (s1, s2, s3, s4)]

        if explicit_skip:
            approved = False
            status_tag = "SKIPPED"
            gate_reason = f"Skipped by operator override: {ov.get('reason', 'manual skip')}"
        elif not s1.passed:
            approved = False
            status_tag = "REJECTED_GATE"
            gate_reason = f"[S1:SAFETY] {s1.reason}"
        elif not s3.passed:
            approved = False
            status_tag = "CIRCUIT_OPEN"
            gate_reason = f"[S3:CIRCUIT_BREAKER] {s3.reason}"
        elif not s2.passed:
            approved = False
            status_tag = "HOLD_LOW_GAIN"
            gate_reason = f"[S2:NOISE_FLOOR] {s2.reason}"
        else:
            # Slots and pacing only delay dispatch; the candidate stays approved while it waits.
            approved = True
            if not s4.passed:
                # `RATE_WAIT` strictly means "pacing the 190s inter-submit gap". With no account slot
                # free until the Beijing-day reset there is no countdown to show, so it keeps its own
                # tag; conflating the two made the UI treat a reset wait as a countdown.
                status_tag = "QUOTA_EXHAUSTED"
                gate_reason = f"Gate stages S1-S3 passed; waiting for a free submission slot ({s4.reason})"
            elif is_critical_restore:
                status_tag = "CRITICAL_RESTORE"
                gate_reason = f"CRITICAL_RESTORE approved: {s2.reason}"
            elif is_probe_pass:
                # 4.3 P23: a speculative shot with an undo and a budget, never a READY exploit.
                status_tag = "PROBE"
                gate_reason = f"PROBE intent approved: {s2.reason}"
            elif is_explore_pass or is_low_gain_pass or circuit_advisory or escape_released:
                why = " | ".join(
                    ([f"[S1 escape released] {s1.reason}"] if escape_released else [])
                    + ([f"[S3 advisory] {s3.reason}"] if circuit_advisory else [])
                    + [s2.reason])
                if quota.rate_wait_sec > 0 and as_of is None:
                    status_tag = "RATE_WAIT"
                    gate_reason = f"EXPLORE approved (pacing {quota.rate_wait_sec}s): {why}"
                else:
                    status_tag = "EXPLORE"
                    gate_reason = why
            elif quota.rate_wait_sec > 0 and as_of is None:
                status_tag = "RATE_WAIT"
                gate_reason = f"All 4 stages passed; pacing {quota.rate_wait_sec}s for 190s rate limit"
            else:
                status_tag = "READY"
                gate_reason = f"All 4 stages passed (weighted E[delta]={estimate.weighted_delta:+.2f} pts)"

        if approved and is_critical_restore:
            priority_score = round(1000.0 + estimate.weighted_delta, 2)
        elif approved and intent_decision is not None and intent_decision.priority is not None:
            # 4.3 P23: probe = weight * E[max(0, gain)] * 0.5, below a READY exploit of the same
            # problem; a restore priced above the floor uses weight * gain.
            priority_score = round(float(intent_decision.priority), 2)
        elif approved and is_explore_pass:
            # Ordered below high-confidence READY gains while still actionable
            priority_score = round(estimate.difficulty * max(0.5, 0.5 * estimate.ucb_delta), 2)
        elif approved and is_ei_pass:
            priority_score = round(estimate.difficulty * estimate.expected_improvement, 2)
        elif approved and is_low_gain_pass:
            priority_score = round(estimate.difficulty * max(0.0, estimate.delta_expected) * 0.5, 2)
        else:
            priority_score = round(estimate.weighted_delta, 2)
        if approved and circuit_advisory and not is_critical_restore:
            priority_score = round(priority_score * 0.5, 2)
        # 4.3 P24: a released escape lock is a hint, so it ranks at half priority; health warnings
        # (elf_guard alerts and metric growth over the board SHA) scale it once more.
        if approved and escape_released and not is_critical_restore:
            priority_score = round(priority_score * 0.5, 2)
        if approved and health_warnings:
            priority_score = round(priority_score * float(self.config.health_penalty), 2)

        return GateVerdict(
            title=title,
            sha=canonical_sha,
            short_sha=estimate.short_sha,
            approved=approved,
            status_tag=status_tag,
            gate_reason=gate_reason,
            priority_score=priority_score,
            is_critical_restore=is_critical_restore,
            forced=effective_force,
            decay_factor_gamma=estimate.decay_factor_gamma,
            raw_historical_best_score=estimate.raw_historical_best_score,
            contemporary_best_score=estimate.contemporary_best_score,
            true_restore_gain=estimate.true_restore_gain,
            estimate=estimate,
            stages=stages,
            circuit_breaker=cb,
            quota=quota,
            intent=(intent_decision.intent if intent_decision is not None else "exploit"),
            requested_intent=requested_intent,
            intent_code=(intent_decision.code if intent_decision is not None else "EXPLOIT"),
            intent_reason=(
                (intent_decision.reason or f"judged as exploit ({intent_source})") if intent_decision is not None
                else ("queued as a " + requested_intent if requested_intent != "exploit" else "")),
            undo=(intent_decision.metrics.get("undo") or intent_decision.metrics.get("pricing") or {})
            if intent_decision is not None else {},
        )
