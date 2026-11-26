"""Confidence-aware submission gate: case-bench pairing, receipt calibration, expected improvement."""
from __future__ import annotations

import dataclasses
import datetime
import json
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from scheduler.estimator import ScoreEstimate, ScoreEstimator, expected_improvement  # noqa: E402
from scheduler.gate import SubmissionGate, StageResult  # noqa: E402

UTC = datetime.timezone.utc
CAND, BASE = "c" * 40, "b" * 40


def _estimate(delta: float, sigma: float, **extra) -> ScoreEstimate:
    from scheduler.estimator import _normal_cdf
    fields = dict(
        title="Op", sha=CAND, short_sha=CAND[:7], difficulty=4, board_status="Pass", current_score=10.0,
        estimated_score=10.0 + delta, delta_expected=delta, weighted_delta=4 * delta, confidence_sigma=sigma,
        prob_positive=_normal_cdf(delta / sigma), recoverable_headroom=0.0, effective_speedup=1.0,
        estimation_method="case_bench_paired", baseline_sha=BASE, decay_factor_gamma=1.0,
        raw_historical_best_score=10.0, contemporary_best_score=10.0, true_restore_gain=0.0, matched_cases=15,
        improved_cases=10, regressed_cases=[], detail="", calibration_trust=0.3,
        expected_improvement=round(expected_improvement(delta, sigma), 2),
    )
    fields.update(extra)
    return ScoreEstimate(**fields)


class ExpectedImprovementTest(unittest.TestCase):
    def test_values(self) -> None:
        self.assertAlmostEqual(expected_improvement(1.07, 5.68), 2.84, places=2)
        self.assertEqual(expected_improvement(2.0, 0.0), 2.0)
        self.assertEqual(expected_improvement(-1.0, 0.0), 0.0)
        # More uncertainty is more upside when a lower result cannot lower the board.
        self.assertGreater(expected_improvement(0.5, 4.0), expected_improvement(0.5, 1.0))


class GateStageTwoTest(unittest.TestCase):
    def _verdict(self, estimate: ScoreEstimate, **config):
        with tempfile.TemporaryDirectory() as td:
            gate = SubmissionGate(pathlib.Path(td))
            for key, value in config.items():
                setattr(gate.config, key, value)
            ledger = types.SimpleNamespace(
                confirmed_board_zero=False, historical_best_sha=BASE, contemporary_best_score=10.0,
                raw_historical_best_score=10.0, true_restore_gain=0.0)
            quota = types.SimpleNamespace(remaining_today=140, discretionary_remaining=138, rate_wait_sec=0,
                                          used_today=2, daily_limit=150, reserved_for_closing=2,
                                          needed_closing_restores=0)
            breaker = types.SimpleNamespace(state="CLOSED", reason="closed", to_dict=lambda: {})
            safe = StageResult(stage="SAFETY", passed=True, code="PASS", reason="ok", metrics={})
            with mock.patch.object(gate.estimator, "estimate", return_value=estimate), \
                    mock.patch.object(gate.estimator, "get_problem_ledger", return_value=ledger), \
                    mock.patch.object(gate, "get_circuit_breaker", return_value=breaker), \
                    mock.patch.object(gate, "get_quota_status", return_value=quota), \
                    mock.patch.object(gate, "probe_allowance", return_value={"allowed": False}), \
                    mock.patch.object(gate, "evaluate_safety", return_value=safe), \
                    mock.patch.object(gate, "_find_override", return_value={}), \
                    mock.patch("scheduler.gate.resolve_sha", return_value=CAND):
                return gate._evaluate_impl("Op", CAND)

    def test_uncertain_small_gain_passes_on_expected_improvement(self) -> None:
        verdict = self._verdict(_estimate(1.07, 5.68))
        self.assertTrue(verdict.approved, verdict.gate_reason)
        s2 = verdict.stages[1]
        self.assertEqual(s2.code, "EXPECTED_IMPROVEMENT")
        self.assertAlmostEqual(verdict.priority_score, 4 * 2.84, places=1)
        self.assertNotIn("quota", s2.reason)

    def test_confident_small_gain_still_holds(self) -> None:
        verdict = self._verdict(_estimate(1.07, 0.8))
        self.assertFalse(verdict.approved)
        self.assertEqual(verdict.stages[1].code, "BELOW_NOISE_FLOOR")
        self.assertNotIn("preserve daily quota", verdict.gate_reason)

    def test_unlikely_gain_holds_despite_upside(self) -> None:
        est = _estimate(-20.0, 22.0)  # EI 2.17 clears the floor, P(gain>0) = 0.18 does not
        self.assertGreater(est.expected_improvement, 1.9)
        verdict = self._verdict(est)
        self.assertFalse(verdict.approved)

    def test_rule_can_be_switched_off(self) -> None:
        verdict = self._verdict(_estimate(1.07, 5.68), keep_best_board=False)
        self.assertFalse(verdict.approved)


def _report(folder: pathlib.Path, sha: str, run: str, minima: dict[str, float], finished: str) -> None:
    path = folder / sha / "bench-runs" / run / "report.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "formal": True, "verified": True, "exit_code": 0, "sha": sha, "timing_score_eligible": True,
        "suite_version": 0, "finished_at": finished,
        "cases": [{"id": cid, "source": "official", "min_us": us} for cid, us in minima.items()]}))


class CaseBenchEvidenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = pathlib.Path(self.tmp.name)
        self.est = ScoreEstimator.__new__(ScoreEstimator)
        self.est.workspace = self.ws
        self.est.outcomes = []
        folder = self.ws / "results" / "Op"
        _report(folder, BASE, "r1", {"01": 10.0, "02": 100.0}, "2026-11-26T01:00:00+00:00")
        _report(folder, CAND, "r2", {"01": 12.0, "02": 50.0}, "2026-11-26T02:00:00+00:00")
        _report(folder, CAND, "r3", {"01": 5.0, "02": 60.0}, "2026-11-26T03:00:00+00:00")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_pairing_uses_best_minimum_over_runs(self) -> None:
        pair = self.est._case_bench_pair("Op", CAND, BASE)
        self.assertEqual(pair["matched"], 2)
        self.assertEqual(pair["runs_cand"], 2)
        self.assertAlmostEqual(pair["geo"], (2.0 * 2.0) ** 0.5)

    def test_replay_ignores_runs_after_the_cutoff(self) -> None:
        pair = self.est._case_bench_pair("Op", CAND, BASE, as_of=datetime.datetime(2026, 11, 26, 2, 30, tzinfo=UTC))
        self.assertEqual(pair["runs_cand"], 1)
        self.assertAlmostEqual(pair["geo"], ((10 / 12) * 2.0) ** 0.5)

    def test_confidence_weights_own_problem_receipts(self) -> None:
        at = datetime.datetime(2026, 11, 26, tzinfo=UTC)
        self.est._residuals = [
            {"title": "Op", "at": at, "residual": -4.0},
            {"title": "Op", "at": at, "residual": -2.0},
            {"title": "Other", "at": at, "residual": 2.0},
        ]
        conf = self.est.prediction_confidence("Op")
        self.assertEqual(conf["n_eff"], 2.5)
        self.assertAlmostEqual(conf["mean"], (-4 - 2 + 1) / 2.5)
        self.assertIsNotNone(conf["var"])
        early = self.est.prediction_confidence("Op", as_of=at)
        self.assertEqual(early["n_eff"], 0.0)


if __name__ == "__main__":
    unittest.main()
