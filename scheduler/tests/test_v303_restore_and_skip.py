"""Unit tests for V3-024 `[RESTORE]` gate tightening and permanent `skip` suppression in `scheduler`."""
from __future__ import annotations

import datetime
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from scheduler.cli_bridge import SchedulerEngine  # noqa: E402
from scheduler.estimator import ProblemLedger, ScoreEstimate, resolve_sha  # noqa: E402
from scheduler.gate import GateConfig, SubmissionGate  # noqa: E402


def _make_git_ws(tmp: pathlib.Path, title: str = "StridedSliceGrad") -> tuple[pathlib.Path, list[str]]:
    ws = tmp / "ws"
    op_dir = ws / "bench" / title
    (op_dir / "op_host").mkdir(parents=True)
    (op_dir / "op_kernel").mkdir(parents=True)
    (ws / "results" / title).mkdir(parents=True)
    (ws / "results" / "scoring-evidence").mkdir(parents=True)
    (ws / "bench" / "problems.json").write_text(
        json.dumps([{"title": title, "slug": title.lower(), "path": f"bench/{title}", "difficulty": 3}]),
        encoding="utf-8",
    )
    (ws / "results" / "submit-queue.jsonl").write_text("", encoding="utf-8")
    (ws / "results" / "submit-queue-outcome.jsonl").write_text("", encoding="utf-8")

    subprocess.run(["git", "-C", str(op_dir), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(op_dir), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(op_dir), "config", "user.name", "Test"], check=True)

    shas: list[str] = []
    for idx in range(3):
        (op_dir / "op_host" / "h.cpp").write_text(f"// host {idx}\n", encoding="utf-8")
        (op_dir / "op_kernel" / "k.cpp").write_text(f"// version {idx}\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(op_dir), "add", "."], check=True)
        subprocess.run(["git", "-C", str(op_dir), "commit", "-q", "-m", f"v{idx}"], check=True)
        sha = subprocess.check_output(["git", "-C", str(op_dir), "rev-parse", "HEAD"], text=True).strip()
        shas.append(sha)
        vdir = ws / "results" / title / sha
        vdir.mkdir(parents=True, exist_ok=True)
        (vdir / "result.json").write_text(
            json.dumps({"exit_code": 0, "sha": sha, "finished_at": f"2026-11-23T0{idx}:00:00Z"}),
            encoding="utf-8",
        )
        (vdir / "validation.log").write_text("EXTENDED_PASSED 40 cases\nEXTENDED_RC=0\n", encoding="utf-8")
    return ws, shas


def _fake_estimate(
    title: str,
    sha: str,
    *,
    current_score: float,
    estimated_score: float,
    ucb_delta: float,
    explore_eligible: bool = False,
) -> ScoreEstimate:
    delta = round(estimated_score - current_score, 2)
    return ScoreEstimate(
        title=title,
        sha=sha,
        short_sha=sha[:7],
        difficulty=3,
        board_status="Pass" if current_score > 0 else "no-submission",
        current_score=current_score,
        estimated_score=estimated_score,
        delta_expected=delta,
        weighted_delta=round(3 * delta, 2),
        confidence_sigma=2.2,
        prob_positive=0.75 if delta > 0 else 0.25,
        recoverable_headroom=10.0,
        effective_speedup=1.05,
        estimation_method="pairwise_simplex_v2",
        baseline_sha=None,
        decay_factor_gamma=0.99,
        raw_historical_best_score=72.13,
        contemporary_best_score=72.13,
        true_restore_gain=max(0.0, round(72.13 - current_score, 2)),
        matched_cases=20,
        improved_cases=5,
        regressed_cases=[],
        detail="synthetic",
        ucb_delta=ucb_delta,
        calibration_trust=0.60,
        explore_eligible=explore_eligible,
        explore_reason="synthetic explore" if explore_eligible else "",
        explore_evidence={},
    )


def _fake_ledger(title: str, *, current_score: float, best_score: float, best_sha: str) -> ProblemLedger:
    return ProblemLedger(
        title=title,
        difficulty=3,
        board_status="Pass" if current_score > 0 else "no-submission",
        current_score=current_score,
        weighted_current_score=round(3 * current_score, 2),
        public_score=7.0,
        hidden_score=max(0.0, current_score - 7.0),
        implied_hidden_r=0.7,
        public_r_list=[0.7] * 5,
        frontier_top1=85.0,
        frontier_top5=75.0,
        target_score=85.0,
        recoverable_headroom=10.0,
        theoretical_headroom=30.0,
        active_board_sha="0" * 40,
        active_judged_score=current_score,
        active_judged_at="2026-11-23T10:00:00Z",
        decay_factor_gamma=0.99,
        raw_historical_best_score=best_score,
        contemporary_best_score=best_score,
        historical_best_score=best_score,
        historical_best_mean=best_score,
        historical_best_sha=best_sha,
        true_restore_gain=max(0.0, round(best_score - current_score, 2)),
        restore_gap=max(0.0, round(best_score - current_score, 2)),
    )


class RestoreRegimeGateTest(unittest.TestCase):
    def test_restore_regime_blocks_inferior_non_best_even_with_explore_and_allows_best_or_new_best(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws, (best_sha, cand_sha, cur_sha) = _make_git_ws(pathlib.Path(td))
            (ws / "results" / "score-snapshot.json").write_text(
                json.dumps({"StridedSliceGrad": {"score": 66.83, "status": "Pass"}}),
                encoding="utf-8",
            )
            gate = SubmissionGate(ws, config=GateConfig(noise_floor_pts=1.9, min_closing_reserve=2))
            ledger = _fake_ledger("StridedSliceGrad", current_score=66.83, best_score=72.13, best_sha=best_sha)

            # 1. Non-best candidate est=70.40 < best=72.13 (even though +3.57 > cur and explore_eligible=True) -> HELD!
            inf_est = _fake_estimate(
                "StridedSliceGrad", cand_sha, current_score=66.83, estimated_score=70.40, ucb_delta=6.66, explore_eligible=True
            )
            with (
                mock.patch.object(gate.estimator, "estimate", return_value=inf_est),
                mock.patch.object(gate.estimator, "get_problem_ledger", return_value=ledger),
            ):
                v_inf = gate.evaluate("StridedSliceGrad", sha=cand_sha)
            self.assertFalse(v_inf.approved)
            self.assertEqual(v_inf.status_tag, "HOLD_LOW_GAIN")
            s2_inf = next(s for s in v_inf.stages if s.stage == "NOISE_FLOOR")
            self.assertFalse(s2_inf.passed)
            self.assertEqual(s2_inf.code, "BELOW_BEST_IN_RESTORE")
            self.assertAlmostEqual(s2_inf.metrics["ref_best"], 72.13, places=2)
            self.assertAlmostEqual(s2_inf.metrics["delta_vs_best"], -1.73, places=2)

            # 2. Non-best candidate slightly below best (71.80 < 72.13) with high UCB above best -> STILL HELD (delta_vs_best <= 0)!
            near_est = _fake_estimate(
                "StridedSliceGrad", cand_sha, current_score=66.83, estimated_score=71.80, ucb_delta=8.50, explore_eligible=True
            )
            with (
                mock.patch.object(gate.estimator, "estimate", return_value=near_est),
                mock.patch.object(gate.estimator, "get_problem_ledger", return_value=ledger),
            ):
                v_near = gate.evaluate("StridedSliceGrad", sha=cand_sha)
            self.assertFalse(v_near.approved)
            self.assertEqual(v_near.status_tag, "HOLD_LOW_GAIN")
            self.assertEqual(next(s for s in v_near.stages if s.stage == "NOISE_FLOOR").code, "BELOW_BEST_IN_RESTORE")

            # 3. True new-best candidate (est=73.29 > best=72.13, ucb_vs_best >= 1.9) -> qualifies as EXPLORE!
            exp_est = _fake_estimate(
                "StridedSliceGrad", cand_sha, current_score=66.83, estimated_score=73.29, ucb_delta=9.50, explore_eligible=False
            )
            with (
                mock.patch.object(gate.estimator, "estimate", return_value=exp_est),
                mock.patch.object(gate.estimator, "get_problem_ledger", return_value=ledger),
            ):
                v_exp = gate.evaluate("StridedSliceGrad", sha=cand_sha)
            self.assertTrue(v_exp.approved)
            self.assertEqual(v_exp.status_tag, "EXPLORE")
            self.assertEqual(next(s for s in v_exp.stages if s.stage == "NOISE_FLOOR").code, "EXPLORE_HIGH_UPSIDE")

            # 4. New-best candidate >= best + noise_floor (est=74.50 >= 72.13 + 1.9) -> qualifies as READY!
            ready_est = _fake_estimate(
                "StridedSliceGrad", cand_sha, current_score=66.83, estimated_score=74.50, ucb_delta=10.50
            )
            with (
                mock.patch.object(gate.estimator, "estimate", return_value=ready_est),
                mock.patch.object(gate.estimator, "get_problem_ledger", return_value=ledger),
            ):
                v_ready = gate.evaluate("StridedSliceGrad", sha=cand_sha)
            self.assertTrue(v_ready.approved)
            self.assertEqual(v_ready.status_tag, "READY")
            self.assertEqual(next(s for s in v_ready.stages if s.stage == "NOISE_FLOOR").code, "ABOVE_NOISE_FLOOR")

            # 5. Restoring `historical_best_sha` itself IS allowed to consume cur -> best!
            rest_est = _fake_estimate(
                "StridedSliceGrad", best_sha, current_score=66.83, estimated_score=72.13, ucb_delta=7.50
            )
            with (
                mock.patch.object(gate.estimator, "estimate", return_value=rest_est),
                mock.patch.object(gate.estimator, "get_problem_ledger", return_value=ledger),
            ):
                v_rest = gate.evaluate("StridedSliceGrad", sha=best_sha)
            self.assertTrue(v_rest.approved)
            self.assertEqual(v_rest.status_tag, "READY")
            self.assertEqual(next(s for s in v_rest.stages if s.stage == "NOISE_FLOOR").code, "ABOVE_NOISE_FLOOR")


class PermanentSkipSuppressionTest(unittest.TestCase):
    def test_gate_matches_skip_override_by_short_prefix_and_blocks_without_cli_force(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws, (best_sha, cand_sha, _) = _make_git_ws(pathlib.Path(td))
            (ws / "results" / "score-snapshot.json").write_text(
                json.dumps({"StridedSliceGrad": {"score": 0.0, "status": "no-submission"}}),
                encoding="utf-8",
            )
            # Stored with uppercase 7-char prefix override!
            gate = SubmissionGate(
                ws,
                config=GateConfig(noise_floor_pts=1.9),
                overrides={f"StridedSliceGrad:{cand_sha[:7].upper()}": {"action": "skip", "reason": "bad candidate"}},
            )
            v_blocked = gate.evaluate("StridedSliceGrad", sha=cand_sha, speedup=1.3)
            self.assertFalse(v_blocked.approved)
            self.assertEqual(v_blocked.status_tag, "SKIPPED")
            s2 = next(s for s in v_blocked.stages if s.stage == "NOISE_FLOOR")
            self.assertFalse(s2.passed)
            self.assertEqual(s2.code, "OPERATOR_SKIPPED")

            # Explicit CLI force=True CAN override if explicitly requested
            v_forced = gate.evaluate("StridedSliceGrad", sha=cand_sha, speedup=1.3, force=True)
            self.assertTrue(v_forced.approved)
            self.assertNotEqual(v_forced.status_tag, "SKIPPED")

    def test_discover_unsubmitted_candidates_never_reactivates_7char_or_40char_skipped_sha(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            ws, (_s0, _s1, newest_sha) = _make_git_ws(td_p)
            (ws / "results" / "score-snapshot.json").write_text(
                json.dumps({"StridedSliceGrad": {"score": 70.0, "status": "Pass"}}),
                encoding="utf-8",
            )
            # Case 1: skipped with 7-char SHA in submit-queue-outcome.jsonl
            (ws / "results" / "submit-queue-outcome.jsonl").write_text(
                json.dumps({"title": "StridedSliceGrad", "sha": newest_sha[:7], "status": "skipped", "finished_at": "2026-11-23T05:00:00Z"}) + "\n",
                encoding="utf-8",
            )
            with mock.patch.dict("os.environ", {"PARHELION_HOME": str(td_p / "ph")}, clear=False):
                eng = SchedulerEngine(workspace=ws, state_dir=td_p / "st1")
                self.assertEqual(eng._discover_unsubmitted_candidates(), [])

            # Case 2: skipped ONLY via 7-char override in scheduler_state.json (empty outcome file)
            (ws / "results" / "submit-queue-outcome.jsonl").write_text("", encoding="utf-8")
            st2 = td_p / "st2"
            st2.mkdir(parents=True)
            (st2 / "scheduler_state.json").write_text(
                json.dumps({"overrides": {f"StridedSliceGrad:{newest_sha[:7]}": {"action": "skip", "reason": "skip"}}}),
                encoding="utf-8",
            )
            with mock.patch.dict("os.environ", {"PARHELION_HOME": str(td_p / "ph")}, clear=False):
                eng2 = SchedulerEngine(workspace=ws, state_dir=st2)
                self.assertEqual(eng2._discover_unsubmitted_candidates(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
