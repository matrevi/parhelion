"""4-Stage Statistical & Safety Submission Gate + Token-Bucket Quota Manager.

Stages:
  1. SAFETY:
     - Verified `result.json` (`exit_code == 0`) & byte-consistent git tree (`op_host`/`op_kernel`)
     - Full suite (`FAST_LANE=1` barred), `EXTENDED_PASSED` present
     - Zero `EXTENDED_FAILED` / `EXTENDED_CASES_FAILED` / `VALIDATION_FAILED` / `STRESS_CASE_FAIL`
     - All `mismatched=0/...`, `GATE_BAD bad=0/...`, `GATE_DET identical=True`
     - Cross-core / barrier static+dynamic safety check (`SetAtomicAdd`/`SyncAll` requires
       `STRESS_DONE` / `DETERMINISTIC` / `GATE_DET` unless already proven online)
  2. NOISE_FLOOR:
     - Holds candidates with `delta_expected < noise_floor_pts` (default `+2.5` pts ~ `0.93 sigma`
       for `sigma = 2.70` pts) as `HOLD_LOW_GAIN`
     - Bypassed automatically when board is `0` / `no-submission` (`CRITICAL_RESTORE`) or `force=True`
  3. CIRCUIT_BREAKER:
     - Nygard 3-state machine (`CLOSED` / `OPEN` / `HALF_OPEN`) over rolling 12h window
     - Trips `CIRCUIT_OPEN (Switch Baseline)` on `>= 6` non-improving shots since last new high
       (counter resets automatically on any strictly higher Pass) or `>= 2` consecutive zeroing errors
     - Allows `CRITICAL_RESTORE` through `HALF_OPEN` so a zeroed board is never stranded at 0
  4. QUOTA_RESERVE:
     - Dual Token Bucket: 190s inter-submission rate limit (`RATE_WAIT`) + 50/day Beijing-day
       (`UTC+8` 00:00 reset) hard cap reserving `N_restore` slots for closing-time restores
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
    min_closing_reserve: int = 5
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
    window_best_score: float
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
            "estimate": self.estimate.to_dict(),
            "stages": [s.to_dict() for s in self.stages],
            "circuit_breaker": self.circuit_breaker.to_dict(),
            "quota": self.quota.to_dict(),
        }


_MISMATCH_RE = re.compile(r"mismatched=(\d+)/(\d+)")
_EXTENDED_PASS_RE = re.compile(r"EXTENDED_PASSED(?:\s+(\d+))?")
_STRESS_DONE_RE = re.compile(r"STRESS_DONE\s+runs=(\d+)\s+failures=(\d+)")
_GATE_BAD_RE = re.compile(r"GATE_BAD\s+bad=(\d+)/(\d+)")
_GATE_DET_RE = re.compile(r"GATE_DET\s+identical=(\w+)")


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
        """Collect (title, sha) pairs with confirmed online Pass in outcome or submissions.json."""
        passed: dict[tuple[str, str], datetime.datetime | None] = {}
        for r in self.estimator.outcomes:
            if r.get("status") == "Pass" and isinstance(r.get("score"), (int, float)) and r["score"] > 0:
                key = (r["title"], r.get("canonical_sha") or r.get("sha", ""))
                ts = _parse_iso(r.get("submitted_at") or r.get("finished_at"))
                if key not in passed or (ts and (passed[key] is None or ts < passed[key])):
                    passed[key] = ts

        # Also check results/scoring-evidence/my-score-table.json if available
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
            # Allow offline fixture workspaces without full git clones if result.json matches
            return True, "no_git_repo_fixture"
        cat = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-t", sha],
            capture_output=True,
            text=True,
        )
        if cat.returncode != 0 or cat.stdout.strip() != "commit":
            return False, f"commit {sha[:7]} not found in {title} git repository"

        # Ensure editable directories op_host & op_kernel exist in that commit tree
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

        # Bar fast-lane partial probes from online judging
        if "FAST_LANE=1" in log_text:
            return StageResult(
                stage="SAFETY",
                passed=False,
                code="FAST_LANE_ONLY",
                reason="Validated with FAST_LANE=1 subset only; run full Q1 validation before submitting",
            )

        # Check explicit failure markers
        for bad_marker in (
            "EXTENDED_FAILED",
            "EXTENDED_CASES_FAILED",
            "VALIDATION_FAILED",
            "STRESS_CASE_FAIL",
            "Wrong Answer:",
            "NON_DETERMINISTIC",
            "GATE_FAIL",
        ):
            if bad_marker in log_text:
                return StageResult(
                    stage="SAFETY",
                    passed=False,
                    code="LOG_FAILURE_MARKER",
                    reason=f"validation.log contains failure token '{bad_marker}'",
                )

        # Require EXTENDED_PASSED
        ext_matches = _EXTENDED_PASS_RE.findall(log_text)
        if not ext_matches:
            return StageResult(
                stage="SAFETY",
                passed=False,
                code="MISSING_EXTENDED_PASSED",
                reason="EXTENDED_PASSED marker absent from validation.log (silent rc=0 trap)",
            )
        ext_cases = max((int(x) for x in ext_matches if x and x.isdigit()), default=0)

        # Verify all sample/output mismatched=0/N
        mismatches = [(int(a), int(b)) for a, b in _MISMATCH_RE.findall(log_text)]
        bad_mm = [(a, b) for a, b in mismatches if a > 0]
        if bad_mm:
            return StageResult(
                stage="SAFETY",
                passed=False,
                code="NONZERO_MISMATCH",
                reason=f"Sample/output mismatch detected: mismatched={bad_mm[0][0]}/{bad_mm[0][1]}",
            )

        # Check GATE_BAD and GATE_DET if present
        for bad_n, tot_n in _GATE_BAD_RE.findall(log_text):
            if int(bad_n) > 0:
                return StageResult(
                    stage="SAFETY",
                    passed=False,
                    code="GATE_BAD_LANES",
                    reason=f"GATE_BAD reported {bad_n}/{tot_n} bad lanes",
                )
        for ident in _GATE_DET_RE.findall(log_text):
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

        has_det_or_stress = (
            stress_runs > 0
            or "DETERMINISTIC" in log_text
            or "GATE_DET identical=True" in log_text
            or "stress ok" in log_text
        )

        # Cross-core / barrier race check: if commit uses SetAtomicAdd / SyncAll and has never
        # passed online before as_of, require explicit multi-run determinism/stress gate
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

        return StageResult(
            stage="SAFETY",
            passed=True,
            code="SAFETY_OK",
            reason=(
                f"exit=0, EXTENDED_PASSED ({ext_cases or 'ok'} cases), mismatched=0"
                + (f", stress={stress_runs}" if stress_runs else "")
                + (", DET=True" if "DETERMINISTIC" in log_text or "GATE_DET identical=True" in log_text else "")
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
        """Stage 3 helper: evaluate 12h / 6-non-improving-shots Nygard circuit breaker."""
        ref_now = as_of or datetime.datetime.now(datetime.timezone.utc)
        cutoff = ref_now - datetime.timedelta(hours=self.config.circuit_window_hours)

        window_rows: list[tuple[datetime.datetime, str, float]] = []
        prior_best = -1.0
        for r in self.estimator.outcomes:
            if r.get("title") != title:
                continue
            st = str(r.get("status", ""))
            if st in ("rejected", "skipped", "duplicate"):
                continue
            ts = _parse_iso(r.get("submitted_at") or r.get("finished_at") or r.get("queued_at"))
            if ts is None or ts >= ref_now:
                continue
            sc = float(r["score"]) if isinstance(r.get("score"), (int, float)) and st == "Pass" else 0.0
            if ts < cutoff:
                if st == "Pass" and sc > prior_best:
                    prior_best = sc
                continue
            window_rows.append((ts, st, sc))

        window_rows.sort(key=lambda x: x[0])
        best_in_win = prior_best
        non_improving = 0
        consec_zeros = 0
        for _ts, st, sc in window_rows:
            if st == "Pass" and sc > best_in_win:
                best_in_win = sc
                non_improving = 0
                consec_zeros = 0
            else:
                non_improving += 1
                if st != "Pass" or sc <= 0.0:
                    consec_zeros += 1
                else:
                    consec_zeros = 0

        limit = self.config.circuit_limit_shots
        if non_improving >= limit:
            state = "OPEN"
            reason = (
                f"{non_improving}/{limit} consecutive non-improving shots in last "
                f"{self.config.circuit_window_hours:.0f}h (window peak {max(0.0, best_in_win):.2f}) — SWITCH BASELINE"
            )
        elif consec_zeros >= 2:
            state = "HALF_OPEN"
            reason = f"{consec_zeros} consecutive zeroing failures ({window_rows[-1][1]}); only baseline restore allowed"
        else:
            state = "CLOSED"
            reason = (
                f"{non_improving}/{limit} non-improving shots since peak {max(0.0, best_in_win):.2f} "
                f"({len(window_rows)} total in {self.config.circuit_window_hours:.0f}h)"
            )

        return CircuitBreakerStatus(
            title=title,
            state=state,
            shots_in_window=len(window_rows),
            consecutive_non_improving=non_improving,
            consecutive_zero_errors=consec_zeros,
            window_best_score=round(max(0.0, best_in_win), 2),
            window_hours=self.config.circuit_window_hours,
            limit_shots=limit,
            reason=reason,
        )

    def get_quota_status(self, as_of: datetime.datetime | None = None) -> QuotaStatus:
        """Stage 4 helper: compute Beijing-day 50-cap Token Bucket & closing restore reserve."""
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
            # Platform counts real submissions (not local pre-submit rejections unless daily_limit hit)
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

        # Also check submission-receipt.json files for direct submissions outside queue
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

        # Identify problems currently needing closing-time best-SHA restore or zero-recovery
        restore_targets: list[dict[str, Any]] = []
        for title in sorted(self.estimator.problems_meta):
            ledger = self.estimator.get_problem_ledger(title, as_of=as_of)
            needs_zero_fix = ledger.current_score <= 0.0 and ledger.historical_best_sha is not None
            needs_gap_fix = ledger.restore_gap >= self.config.noise_floor_pts and ledger.historical_best_sha is not None
            if needs_zero_fix or needs_gap_fix:
                gap = ledger.historical_best_mean if needs_zero_fix else ledger.restore_gap
                restore_targets.append({
                    "title": title,
                    "best_sha": ledger.historical_best_sha,
                    "current_score": ledger.current_score,
                    "expected_restore_score": ledger.historical_best_mean,
                    "expected_gain": round(gap, 2),
                    "weighted_gain": round(ledger.difficulty * gap, 2),
                    "critical": needs_zero_fix,
                })
        restore_targets.sort(key=lambda x: (x["critical"], x["weighted_gain"]), reverse=True)

        reserved = min(12, max(self.config.min_closing_reserve, len(restore_targets)))
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

        # Stage 1: SAFETY
        s1 = self.evaluate_safety(title, canonical_sha, as_of=as_of)

        # Stage 2: NOISE_FLOOR
        if is_critical_restore:
            s2 = StageResult(
                stage="NOISE_FLOOR",
                passed=True,
                code="BYPASS_CRITICAL_RESTORE",
                reason=(
                    f"Board is '{estimate.board_status}' (score={estimate.current_score:.2f}); "
                    f"CRITICAL_RESTORE bypasses +{self.config.noise_floor_pts:.1f}pt floor "
                    f"(est +{estimate.delta_expected:.2f} pts)"
                ),
                metrics={"delta_expected": estimate.delta_expected, "threshold": self.config.noise_floor_pts},
            )
        elif effective_force and estimate.delta_expected >= 0.0:
            s2 = StageResult(
                stage="NOISE_FLOOR",
                passed=True,
                code="FORCED_APPROVAL",
                reason=f"Explicit force/override approved (delta={estimate.delta_expected:+.2f} pts)",
                metrics={"delta_expected": estimate.delta_expected, "threshold": self.config.noise_floor_pts},
            )
        elif effective_force and estimate.delta_expected < 0.0:
            # Even with force=True, warn/block if historical mean is strictly worse than current board unless manual approve
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
                        f"Expected delta {estimate.delta_expected:+.2f} pts is negative "
                        f"(est {estimate.estimated_score:.2f} < board {estimate.current_score:.2f}); "
                        f"re-submitting trades score downward under tbest drift/jitter"
                    ),
                    metrics={"delta_expected": estimate.delta_expected, "threshold": self.config.noise_floor_pts},
                )
        elif estimate.delta_expected >= self.config.noise_floor_pts:
            s2 = StageResult(
                stage="NOISE_FLOOR",
                passed=True,
                code="ABOVE_NOISE_FLOOR",
                reason=(
                    f"Expected gain {estimate.delta_expected:+.2f} pts >= +{self.config.noise_floor_pts:.2f} pts "
                    f"(~{estimate.delta_expected / 2.70:.2f} sigma, P(gain>0)={estimate.prob_positive * 100:.0f}%)"
                ),
                metrics={"delta_expected": estimate.delta_expected, "threshold": self.config.noise_floor_pts},
            )
        else:
            s2 = StageResult(
                stage="NOISE_FLOOR",
                passed=False,
                code="BELOW_NOISE_FLOOR",
                reason=(
                    f"Expected gain {estimate.delta_expected:+.2f} pts < +{self.config.noise_floor_pts:.2f} pts "
                    f"noise floor (sigma=2.70 pts); holding to preserve daily quota"
                ),
                metrics={"delta_expected": estimate.delta_expected, "threshold": self.config.noise_floor_pts},
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

        # Resolve final verdict & canonical status_tag
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
            elif quota.rate_wait_sec > 0 and as_of is None:
                status_tag = "RATE_WAIT"
                gate_reason = f"All 4 stages passed; pacing {quota.rate_wait_sec}s for 190s rate limit"
            else:
                status_tag = "READY"
                gate_reason = f"All 4 stages passed (weighted E[delta]={estimate.weighted_delta:+.2f} pts)"

        # WSPT Submission Priority: CRITICAL_RESTORE gets +1000 tier, then difficulty * delta_expected
        priority_score = round(
            (1000.0 if (approved and is_critical_restore) else 0.0) + estimate.weighted_delta,
            2,
        )

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
            estimate=estimate,
            stages=stages,
            circuit_breaker=cb,
            quota=quota,
        )
