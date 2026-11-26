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
     - The board keeps the best score, so a candidate also passes when its expected improvement
       E[max(0, gain)] under the calibrated sigma clears the floor and P(gain > 0) >= 0.2
       (`EXPECTED_IMPROVEMENT`); otherwise it holds on `delta_expected < noise_floor_pts` (default `+2.5` pts ~ `0.93 sigma`
       for `sigma = 2.70` pts) as `HOLD_LOW_GAIN` (and yields `delta_expected = 0.0` when
       `candidate_sha == active_board_sha` under `tbest(t)` non-stationarity)
     - Bypassed automatically when board is `0` / `no-submission` (`CRITICAL_RESTORE`) or `force=True`
  3. CIRCUIT_BREAKER:
     - Nygard 3-state machine (`CLOSED` / `OPEN` / `HALF_OPEN`) over rolling 12h window
     - Compares decay-adjusted contemporary scores (`gamma_p` + residual `-0.015 pts/hr`) rather
       than raw 8-day-old peaks so multi-day `tbest(t)` tightening never manufactures false streaks
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

    def probe_allowance(self, title: str, sha: str = "", *, unsolved: bool = True) -> dict[str, Any]:
        """The first three acknowledged submissions may use compile-only evidence."""
        bootstrap = int(self.config.probe_submissions_per_problem)
        used = submitted_attempts(self.workspace, title, self.estimator.outcomes)
        within_bootstrap = used < bootstrap
        allowed, why = self._compile_ok(title, sha) if within_bootstrap else (False, "opening probes used")
        return {
            "bootstrap": bootstrap,
            "used": used,
            "remaining": max(0, bootstrap - used),
            "bootstrap_stage": bool(within_bootstrap),
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
            state = self.estimator.get_calibrator().inspect_escape_state(title, as_of=as_of)
            prior = self._prior_online_pass_shas.get((title, sha))
            if state.get("escape_lock") and not (prior and (as_of is None or prior < as_of)):
                from .case_bench import read_doc
                from .case_feedback import trial_path
                sid = state.get("escape_submission_id")
                bad = state.get("escape_sha") or ""
                trial = read_doc(trial_path(val_dir.parent / bad, sid)) if sid and re.fullmatch(r"[a-f0-9]{40}", bad) and as_of is None else {}
                if trial.get("good_sha") != sha or trial.get("good_run") != report["run_id"]:
                    # WA reproducers (4.1 #6): a fix passing every variant the bad SHA fails clears S1.
                    from . import wa_repro
                    repro = wa_repro.status(self.workspace, title, sid, bad, cand_sha=sha, as_of=as_of)
                    if repro["state"] == "fixed":
                        return StageResult(stage="SAFETY", passed=True, code="ESCAPE_REPRO_FIXED",
                                           reason="ESCAPE LOCK " + wa_repro.next_step(repro),
                                           metrics={"submission_id": sid, "bad_sha": bad, "run_id": repro["fixed_run"],
                                                    "reproduced_run": repro["reproduced_run"]})
                    step = wa_repro.next_step(repro)
                    return StageResult(stage="SAFETY", passed=False, code="ESCAPE_LOCK_ACTIVE",
                                       reason=("ESCAPE_LOCK " + step if step else
                                               "ESCAPE_LOCK: reproduce the failure or request an exact-SHA reviewed trial"),
                                       metrics={"submission_id": sid, "bad_sha": bad, "repro_state": repro["state"],
                                                "next_step": step or "reproduce the failure locally or request an "
                                                                     "exact-SHA reviewed trial"})
            return StageResult(stage="SAFETY",passed=True,code="PASS",
                               reason=f"Full reference suite passed ({len(report['cases'])} cases)",
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
                metrics={"escape_sha": esc_state.get("escape_sha"), "escape_status": esc_st,
                         "next_step": f"add a regression case, then verify-escape --bad-sha {esc_s7} --good-sha {sha[:7]}"},
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

    def _s2_metrics(self, estimate: ScoreEstimate) -> dict[str, Any]:
        return {
            "delta_expected": estimate.delta_expected,
            "expected_improvement": estimate.expected_improvement,
            "prob_positive": estimate.prob_positive,
            "ucb_delta": estimate.ucb_delta,
            "sigma": estimate.confidence_sigma,
            "calibration_trust": estimate.calibration_trust,
            "calibration_samples": estimate.calibration_samples,
            "calibration_bias": estimate.calibration_bias,
            "threshold": self.config.noise_floor_pts,
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
    ) -> GateVerdict:
        """Run all 4 stages (SAFETY -> NOISE_FLOOR -> CIRCUIT_BREAKER -> QUOTA_RESERVE)."""
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
        )
        ledger = self.estimator.get_problem_ledger(
            title,
            as_of=as_of,
            override_board_score=override_board_score,
            override_board_status=override_board_status,
        )
        cb = self.get_circuit_breaker(title, as_of=as_of)
        quota = self.get_quota_status(as_of=as_of)

        is_critical_restore = (
            ledger.confirmed_board_zero
            and bool(ledger.historical_best_sha)
            and canonical_sha.lower().startswith(ledger.historical_best_sha.lower()[:7])
        )
        is_already_active_on_board = (
            as_of is None
            and override_board_score is None
            and not is_critical_restore
            and speedup is None
            and estimate.estimation_method == "active_board_identity"
        )
        ref_best = float(ledger.contemporary_best_score or ledger.raw_historical_best_score or 0.0)
        is_restore_regime = (
            (not is_critical_restore)
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
            and ucb_vs_best >= self.config.noise_floor_pts
            and not bool(ev.get("veto_pos_corr"))
            and not bool(ev.get("veto_small_core"))
        )

        # Acknowledged submission IDs define the three opening probes.
        probe = self.probe_allowance(title, canonical_sha, unsolved=is_critical_restore)
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
                                     "threshold": self.config.noise_floor_pts}),
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
            )

        # Stage 1: SAFETY
        s1 = self.evaluate_safety(title, canonical_sha, as_of=as_of)

        # Stage 2: NOISE_FLOOR (Uncertainty-Aware with `EXPLORE` Anti-Burial Channel)
        is_explore_pass = False
        is_ei_pass = False
        if explicit_skip:
            s2 = StageResult(
                stage="NOISE_FLOOR",
                passed=False,
                code="OPERATOR_SKIPPED",
                reason=f"Skipped by operator override: {ov.get('reason', 'manual skip')}",
                metrics={"delta_expected": estimate.delta_expected, "threshold": self.config.noise_floor_pts},
            )
        elif is_critical_restore:
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
        elif is_restore_regime and not is_restoring_best and not effective_force:
            if delta_vs_best >= self.config.noise_floor_pts:
                s2 = StageResult(
                    stage="NOISE_FLOOR",
                    passed=True,
                    code="ABOVE_NOISE_FLOOR",
                    reason=(
                        f"New-best gain vs best({(ledger.historical_best_sha or '')[:7]}={ref_best:.2f}): "
                        f"delta_vs_best={delta_vs_best:+.2f} pts >= +{self.config.noise_floor_pts:.2f} pts "
                        f"(sigma={estimate.confidence_sigma:.2f}, UCB_vs_best={ucb_vs_best:+.2f})"
                    ),
                    metrics={
                        "delta_expected": estimate.delta_expected,
                        "ucb_delta": estimate.ucb_delta,
                        "sigma": estimate.confidence_sigma,
                        "calibration_trust": estimate.calibration_trust,
                        "threshold": self.config.noise_floor_pts,
                        "ref_best": ref_best,
                        "delta_vs_best": delta_vs_best,
                        "ucb_vs_best": ucb_vs_best,
                    },
                )
            elif restore_explore_ok:
                is_explore_pass = True
                explore_msg = estimate.explore_reason or (
                    f"new-best upside in [RESTORE] (est={estimate.estimated_score:.2f} > "
                    f"best={ref_best:.2f}, UCB_vs_best={ucb_vs_best:+.2f} >= +{self.config.noise_floor_pts:.2f})"
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
                        "threshold": self.config.noise_floor_pts,
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
                        f"UCB_vs_best={ucb_vs_best:+.2f} < +{self.config.noise_floor_pts:.2f}; "
                        f"holding to avoid eating cur->best delta"
                    ),
                    metrics={
                        "delta_expected": estimate.delta_expected,
                        "ucb_delta": estimate.ucb_delta,
                        "sigma": estimate.confidence_sigma,
                        "calibration_trust": estimate.calibration_trust,
                        "threshold": self.config.noise_floor_pts,
                        "ref_best": ref_best,
                        "delta_vs_best": delta_vs_best,
                        "ucb_vs_best": ucb_vs_best,
                    },
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
        elif (
            self.config.keep_best_board
            and estimate.expected_improvement >= self.config.noise_floor_pts
            and estimate.prob_positive >= self.config.min_prob_positive
        ):
            is_ei_pass = True
            s2 = StageResult(
                stage="NOISE_FLOOR",
                passed=True,
                code="EXPECTED_IMPROVEMENT",
                reason=(
                    f"Expected improvement {estimate.expected_improvement:+.2f} pts >= "
                    f"+{self.config.noise_floor_pts:.2f} pts (gain {estimate.delta_expected:+.2f}, "
                    f"sigma={estimate.confidence_sigma:.2f}, P(gain>0)={estimate.prob_positive:.2f}, "
                    f"confidence={estimate.calibration_trust:.2f} from {estimate.calibration_samples:g} receipt(s)); "
                    f"the board keeps the best score"
                ),
                metrics=self._s2_metrics(estimate),
            )
        else:
            low_odds = estimate.prob_positive < self.config.min_prob_positive
            s2 = StageResult(
                stage="NOISE_FLOOR",
                passed=False,
                code="BELOW_NOISE_FLOOR",
                reason=(
                    f"Expected improvement {estimate.expected_improvement:+.2f} pts < "
                    f"+{self.config.noise_floor_pts:.2f} pts (gain {estimate.delta_expected:+.2f}, "
                    f"sigma={estimate.confidence_sigma:.2f}, P(gain>0)={estimate.prob_positive:.2f}, "
                    f"confidence={estimate.calibration_trust:.2f} from {estimate.calibration_samples:g} receipt(s))"
                    + ("; gain is unlikely" if low_odds else "")
                ),
                metrics=self._s2_metrics(estimate),
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

        # Stage 4: dispatch slot (orders dispatch, never withholds approval)
        s4 = self._slot_stage(quota)

        stages = [s1, s2, s3, s4]

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
        elif approved and is_ei_pass:
            priority_score = round(estimate.difficulty * estimate.expected_improvement, 2)
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
