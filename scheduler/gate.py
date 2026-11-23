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
     - Holds candidates with `delta_expected < noise_floor_pts` (default `+2.5` pts ~ `0.93 sigma`
       for `sigma = 2.70` pts) as `HOLD_LOW_GAIN` (and yields `delta_expected = 0.0` when
       `candidate_sha == active_board_sha` under `tbest(t)` non-stationarity)
     - Bypassed automatically when board is `0` / `no-submission` (`CRITICAL_RESTORE`) or `force=True`
  3. CIRCUIT_BREAKER:
     - Nygard 3-state machine (`CLOSED` / `OPEN` / `HALF_OPEN`) over rolling 12h window
     - Compares decay-adjusted contemporary scores (`gamma_p` + residual `-0.015 pts/hr`) rather
       than raw 8-day-old peaks so multi-day `tbest(t)` tightening never manufactures false streaks
  4. QUOTA_RESERVE:
     - Dual Token Bucket: 190s inter-submission rate limit (`RATE_WAIT`) + 50/day Beijing-day
       (`UTC+8` 00:00 reset) hard cap reserving `N_restore` slots strictly for genuine
       decay-discounted closing restores (`true_restore_gain >= noise_floor_pts` or `S_live == 0`)
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

STATUS_TAGS: tuple[str, ...] = (
    "READY",
    "EXPLORE",
    "CRITICAL_RESTORE",
    "RATE_WAIT",
    "JUDGING",
    "PASS_UP",
    "PASS_DOWN",
    "HOLD_LOW_GAIN",
    "CIRCUIT_OPEN",
    "REJECTED_GATE",
    "ZEROED_ERR",
)

BEIJING_TZ = datetime.timezone(datetime.timedelta(hours=8))
DAILY_HARD_LIMIT = 50
MIN_SUBMIT_GAP_SEC = 190


@dataclasses.dataclass
class GateConfig:
    noise_floor_pts: float = 2.5
    min_closing_reserve: int = 2
    circuit_limit_shots: int = 6
    circuit_window_hours: float = 12.0
    rate_limit_gap_sec: int = MIN_SUBMIT_GAP_SEC
    daily_limit: int = DAILY_HARD_LIMIT
    q0_quantum_sec: int = 180
    aging_tau_sec: float = 600.0

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GateConfig:
        valid = {f.name for f in dataclasses.fields(cls)}
        filtered = {k: v for k, v in data.items() if k in valid and v is not None}
        return cls(**filtered)


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
            if r.get("status") == "Pass" and isinstance(r.get("score"), (int, float)) and r["score"] > 0:
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
        repo = self.workspace / "bench" / title
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
        repo = self.workspace / "bench" / title
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
        if not ("op_host" in entries and "op_kernel" in entries):
            return False, f"commit {sha[:7]} missing op_host/ or op_kernel/ tree"
        return True, "git_tree_verified"

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
        if not ext_matches:
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
        # block unproven new commits until `verify-escape` confirms a separating regression test!
        esc_state = self.estimator.get_calibrator().inspect_escape_state(title, as_of=as_of)
        if esc_state.get("escape_lock") and not proven_prior:
            esc_s7 = esc_state.get("escape_short_sha") or "unknown"
            esc_st = esc_state.get("escape_status") or "Online Failure"
            return StageResult(
                stage="SAFETY",
                passed=False,
                code="ESCAPE_LOCK_ACTIVE",
                reason=(
                    f"ESCAPE_LOCK active after {esc_s7} failed online ({esc_st}) despite passing local S1; "
                    f"add regression case & run `verify-escape --title {title} --bad-sha {esc_s7} --good-sha {sha[:7]}` "
                    f"(or restore a previously proven Pass SHA)"
                ),
                metrics={"escape_sha": esc_state.get("escape_sha"), "escape_status": esc_st},
            )

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
            raw_sc = float(r["score"]) if isinstance(r.get("score"), (int, float)) and st == "Pass" else 0.0
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
                if st != "Pass" or contemp_sc <= 0.0:
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
        """Stage 4 helper: Beijing-day 50-cap Token Bucket & decay-discounted closing reserve."""
        ref_now = as_of or datetime.datetime.now(datetime.timezone.utc)
        bj_now = ref_now.astimezone(BEIJING_TZ)
        bj_midnight = bj_now.replace(hour=0, minute=0, second=0, microsecond=0)
        next_bj_midnight = bj_midnight + datetime.timedelta(days=1)

        used_ids: set[str] = set()
        last_submit_ts: datetime.datetime | None = None

        for r in self.estimator.outcomes:
            st = str(r.get("status", ""))
            if st in ("skipped", "duplicate"):
                continue
            if st == "rejected":
                if "daily submission limit" in str(r.get("detail", "")) or "daily_limit" in str(r.get("detail", "")):
                    ts_r = _parse_iso(r.get("finished_at") or r.get("queued_at"))
                    if ts_r and bj_midnight <= ts_r.astimezone(BEIJING_TZ) < ref_now.astimezone(BEIJING_TZ):
                        used_ids.update(f"limit_fill_{i}" for i in range(self.config.daily_limit))
                continue
            ts = _parse_iso(r.get("submitted_at") or r.get("finished_at"))
            if ts is None or ts >= ref_now:
                continue
            if last_submit_ts is None or ts > last_submit_ts:
                last_submit_ts = ts
            if ts.astimezone(BEIJING_TZ) >= bj_midnight:
                sid = str(r.get("submission_id") or f"{r.get('title')}:{r.get('sha')}:{ts.isoformat()}")
                used_ids.add(sid)

        if as_of is None:
            for rec_file in (self.workspace / "results").glob("*/*/submission-receipt.json"):
                try:
                    rdata = json.loads(rec_file.read_text())
                    ts = _parse_iso(rdata.get("submitted_at"))
                    if ts and ts <= ref_now:
                        if last_submit_ts is None or ts > last_submit_ts:
                            last_submit_ts = ts
                        if ts.astimezone(BEIJING_TZ) >= bj_midnight and rdata.get("submission_id"):
                            used_ids.add(str(rdata["submission_id"]))
                except Exception:
                    continue

        used_today = min(self.config.daily_limit, len(used_ids))
        remaining_today = max(0, self.config.daily_limit - used_today)

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
        restore_targets.sort(key=lambda x: (x["critical"], x["weighted_gain"]), reverse=True)

        # Dynamic closing reserve: exact needed restores + 1 safety buffer (floored by min_closing_reserve)
        reserved = min(12, max(self.config.min_closing_reserve, len(restore_targets) + (1 if restore_targets else 0)))
        discretionary = max(0, remaining_today - reserved)

        if last_submit_ts is not None:
            since_last = max(0, int((ref_now - last_submit_ts).total_seconds()))
            rate_wait = max(0, self.config.rate_limit_gap_sec - since_last)
        else:
            since_last = None
            rate_wait = 0

        return QuotaStatus(
            daily_limit=self.config.daily_limit,
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

    def evaluate(
        self,
        title: str,
        sha: str | None = None,
        speedup: float | None = None,
        note: str = "",
        force: bool = False,
        as_of: datetime.datetime | None = None,
        override_board_score: float | None = None,
        override_board_status: str | None = None,
    ) -> GateVerdict:
        """Run all 4 stages (SAFETY -> NOISE_FLOOR -> CIRCUIT_BREAKER -> QUOTA_RESERVE)."""
        canonical_sha = resolve_sha(self.workspace, title, sha)
        ov_key = f"{title}:{canonical_sha}"
        ov = self.overrides.get(ov_key, {})
        ov_action = ov.get("action")
        effective_force = bool(force or ov_action == "approve")

        estimate = self.estimator.estimate(
            title=title,
            sha=canonical_sha,
            speedup=speedup,
            note=note,
            as_of=as_of,
            override_board_score=override_board_score,
            override_board_status=override_board_status,
        )
        cb = self.get_circuit_breaker(title, as_of=as_of)
        quota = self.get_quota_status(as_of=as_of)

        is_critical_restore = (
            estimate.current_score <= 0.0 or estimate.board_status != "Pass"
        )
        is_already_active_on_board = (
            as_of is None
            and override_board_score is None
            and not is_critical_restore
            and speedup is None
            and estimate.estimation_method == "active_board_identity"
        )

        # Stage 1: SAFETY
        s1 = self.evaluate_safety(title, canonical_sha, as_of=as_of)

        # Stage 2: NOISE_FLOOR (Uncertainty-Aware with `EXPLORE` Anti-Burial Channel)
        is_explore_pass = False
        if is_critical_restore:
            s2 = StageResult(
                stage="NOISE_FLOOR",
                passed=True,
                code="BYPASS_CRITICAL_RESTORE",
                reason=(
                    f"Board is '{estimate.board_status}' (score={estimate.current_score:.2f}); "
                    f"CRITICAL_RESTORE bypasses +{self.config.noise_floor_pts:.1f}pt floor "
                    f"(contemp est +{estimate.delta_expected:.2f} pts)"
                ),
                metrics={"delta_expected": estimate.delta_expected, "threshold": self.config.noise_floor_pts},
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
                metrics={"delta_expected": 0.0, "threshold": self.config.noise_floor_pts},
            )
        elif effective_force and estimate.delta_expected >= 0.0:
            s2 = StageResult(
                stage="NOISE_FLOOR",
                passed=True,
                code="FORCED_APPROVAL",
                reason=f"Explicit force/override approved (contemp delta={estimate.delta_expected:+.2f} pts)",
                metrics={"delta_expected": estimate.delta_expected, "threshold": self.config.noise_floor_pts},
            )
        elif estimate.delta_expected >= self.config.noise_floor_pts:
            s2 = StageResult(
                stage="NOISE_FLOOR",
                passed=True,
                code="ABOVE_NOISE_FLOOR",
                reason=(
                    f"Expected gain {estimate.delta_expected:+.2f} pts >= +{self.config.noise_floor_pts:.2f} pts "
                    f"(sigma={estimate.confidence_sigma:.2f}, UCB={estimate.ucb_delta:+.2f}, "
                    f"P(gain>0)={estimate.prob_positive * 100:.0f}%)"
                ),
                metrics={
                    "delta_expected": estimate.delta_expected,
                    "ucb_delta": estimate.ucb_delta,
                    "sigma": estimate.confidence_sigma,
                    "calibration_trust": estimate.calibration_trust,
                    "threshold": self.config.noise_floor_pts,
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
                    "threshold": self.config.noise_floor_pts,
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
                    metrics={"delta_expected": estimate.delta_expected, "threshold": self.config.noise_floor_pts},
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
                        "threshold": self.config.noise_floor_pts,
                    },
                )
        else:
            s2 = StageResult(
                stage="NOISE_FLOOR",
                passed=False,
                code="BELOW_NOISE_FLOOR",
                reason=(
                    f"Expected gain {estimate.delta_expected:+.2f} pts < +{self.config.noise_floor_pts:.2f} pts "
                    f"noise floor (sigma={estimate.confidence_sigma:.2f}, UCB={estimate.ucb_delta:+.2f}, "
                    f"trust={estimate.calibration_trust:.2f}); holding to preserve daily quota"
                ),
                metrics={
                    "delta_expected": estimate.delta_expected,
                    "ucb_delta": estimate.ucb_delta,
                    "sigma": estimate.confidence_sigma,
                    "calibration_trust": estimate.calibration_trust,
                    "threshold": self.config.noise_floor_pts,
                },
            )

        # Stage 3: CIRCUIT_BREAKER
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
        elif cb.state == "OPEN":
            s3 = StageResult(
                stage="CIRCUIT_BREAKER",
                passed=False,
                code="CIRCUIT_OPEN_SWITCH_BASELINE",
                reason=cb.reason,
                metrics=cb.to_dict(),
            )
        elif cb.state == "HALF_OPEN" and (title, canonical_sha) not in self._prior_online_pass_shas:
            s3 = StageResult(
                stage="CIRCUIT_BREAKER",
                passed=False,
                code="HALF_OPEN_UNPROVEN_SHA",
                reason=cb.reason,
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

        # Stage 4: QUOTA_RESERVE
        if quota.remaining_today <= 0:
            s4 = StageResult(
                stage="QUOTA_RESERVE",
                passed=False,
                code="DAILY_LIMIT_REACHED",
                reason=(
                    f"50/day hard limit reached ({quota.used_today}/{quota.daily_limit}); "
                    f"next reset at {quota.next_reset_utc}"
                ),
                metrics={"remaining": 0, "reserved": quota.reserved_for_closing},
            )
        elif is_critical_restore or effective_force:
            s4 = StageResult(
                stage="QUOTA_RESERVE",
                passed=True,
                code="RESERVE_ELIGIBLE",
                reason=(
                    f"Eligible to use closing/emergency reserve ({quota.remaining_today} total remaining, "
                    f"{quota.reserved_for_closing} reserved)"
                ),
                metrics={"remaining": quota.remaining_today, "reserved": quota.reserved_for_closing},
            )
        elif quota.discretionary_remaining <= 0:
            s4 = StageResult(
                stage="QUOTA_RESERVE",
                passed=False,
                code="RESERVED_FOR_CLOSING",
                reason=(
                    f"Only {quota.remaining_today} slot(s) left today — reserved for {quota.reserved_for_closing} "
                    f"closing restores; holding discretionary submission"
                ),
                metrics={"remaining": quota.remaining_today, "reserved": quota.reserved_for_closing},
            )
        else:
            s4 = StageResult(
                stage="QUOTA_RESERVE",
                passed=True,
                code="QUOTA_OK",
                reason=(
                    f"{quota.discretionary_remaining} discretionary slot(s) available "
                    f"({quota.used_today}/{quota.daily_limit} used, {quota.reserved_for_closing} reserved)"
                ),
                metrics={"remaining": quota.remaining_today, "reserved": quota.reserved_for_closing},
            )

        stages = [s1, s2, s3, s4]

        if ov_action == "skip":
            approved = False
            status_tag = "REJECTED_GATE"
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
        elif not s4.passed:
            approved = False
            status_tag = "HOLD_LOW_GAIN" if s4.code == "RESERVED_FOR_CLOSING" else "RATE_WAIT"
            gate_reason = f"[S4:QUOTA_RESERVE] {s4.reason}"
        else:
            approved = True
            if is_critical_restore:
                status_tag = "CRITICAL_RESTORE"
                gate_reason = f"CRITICAL_RESTORE approved: {s2.reason}"
            elif is_explore_pass:
                if quota.rate_wait_sec > 0 and as_of is None:
                    status_tag = "RATE_WAIT"
                    gate_reason = f"EXPLORE approved (pacing {quota.rate_wait_sec}s): {s2.reason}"
                else:
                    status_tag = "EXPLORE"
                    gate_reason = s2.reason
            elif quota.rate_wait_sec > 0 and as_of is None:
                status_tag = "RATE_WAIT"
                gate_reason = f"All 4 stages passed; pacing {quota.rate_wait_sec}s for 190s rate limit"
            else:
                status_tag = "READY"
                gate_reason = f"All 4 stages passed (weighted E[delta]={estimate.weighted_delta:+.2f} pts)"

        if approved and is_critical_restore:
            priority_score = round(1000.0 + estimate.weighted_delta, 2)
        elif approved and is_explore_pass:
            # Ordered below high-confidence READY gains while still actionable
            priority_score = round(estimate.difficulty * max(0.5, 0.5 * estimate.ucb_delta), 2)
        else:
            priority_score = round(estimate.weighted_delta, 2)

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
        )
