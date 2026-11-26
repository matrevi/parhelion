"""4.2.1 gate fixes: board mode from the contest rule, TLE escape, advisory breaker, measured noise floor."""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from scheduler import judge_noise  # noqa: E402
from scheduler.gate import StageResult, SubmissionGate  # noqa: E402
from scheduler.tests.test_gate_confidence import BASE, CAND, _estimate  # noqa: E402

A, B, C = "a" * 40, "b" * 40, "d" * 40


def _ledger(best: float = 10.0) -> types.SimpleNamespace:
    return types.SimpleNamespace(confirmed_board_zero=False, historical_best_sha=BASE, contemporary_best_score=best,
                                 raw_historical_best_score=best, true_restore_gain=0.0)


def _verdict(estimate, *, mode: str | None = None, ledger=None, breaker_state: str = "CLOSED", **config):
    with tempfile.TemporaryDirectory() as td:
        gate = SubmissionGate(pathlib.Path(td))
        for key, value in config.items():
            setattr(gate.config, key, value)
        if mode:
            gate.estimator.problems_meta["Op"] = {"title": "Op", "ranking_submission_mode": mode}
        quota = types.SimpleNamespace(remaining_today=140, discretionary_remaining=138, rate_wait_sec=0,
                                      used_today=2, daily_limit=150, reserved_for_closing=2,
                                      needed_closing_restores=0)
        breaker = types.SimpleNamespace(state=breaker_state, reason=f"breaker {breaker_state}", to_dict=lambda: {})
        safe = StageResult(stage="SAFETY", passed=True, code="PASS", reason="ok", metrics={})
        with mock.patch.object(gate.estimator, "estimate", return_value=estimate), \
                mock.patch.object(gate.estimator, "get_problem_ledger", return_value=ledger or _ledger()), \
                mock.patch.object(gate, "get_circuit_breaker", return_value=breaker), \
                mock.patch.object(gate, "get_quota_status", return_value=quota), \
                mock.patch.object(gate, "probe_allowance", return_value={"allowed": False}), \
                mock.patch.object(gate, "evaluate_safety", return_value=safe), \
                mock.patch.object(gate, "_find_override", return_value={}), \
                mock.patch("scheduler.gate.resolve_sha", return_value=CAND):
            return gate._evaluate_impl("Op", CAND)


class BoardModeTest(unittest.TestCase):
    def test_board_mode_follows_the_problem_rule(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            gate = SubmissionGate(pathlib.Path(td))
            gate.estimator.problems_meta["Op"] = {"ranking_submission_mode": "latest"}
            self.assertFalse(gate._keeps_best("Op"))
            gate.estimator.problems_meta["Op"] = {"ranking_submission_mode": "best"}
            self.assertTrue(gate._keeps_best("Op"))
            self.assertTrue(gate._keeps_best("Unknown"))  # no rule: the old best-board behaviour
            gate.estimator.problems_meta["Op"] = {"ranking_submission_mode": "latest"}
            gate.config.board_mode_from_rules = False
            self.assertTrue(gate._keeps_best("Op"))

    def test_board_rule_comes_from_the_scraped_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = pathlib.Path(td)
            (ws / "opsb.workspace.json").write_text(json.dumps({"problems": [
                {"title": "Op", "ranking_submission_mode": "latest"}, {"title": "Other"}]}), encoding="utf-8")
            gate = SubmissionGate(ws)
            self.assertFalse(gate._keeps_best("Op"))
            self.assertTrue(gate._keeps_best("Other"))

    def test_latest_board_drops_the_expected_improvement_shortcut(self) -> None:
        est = _estimate(1.07, 5.68)  # EI 2.84 clears the floor only when a lower result cannot hurt
        self.assertEqual(_verdict(est, mode="best").stages[1].code, "EXPECTED_IMPROVEMENT")
        latest = _verdict(est, mode="latest")
        self.assertEqual(latest.stages[1].code, "LOW_GAIN_POSITIVE")
        self.assertEqual(latest.stages[1].metrics["board_mode"], "latest")

    def test_latest_board_measures_gain_against_the_current_board(self) -> None:
        # Board 57.32 below a historical best of 59.56; the candidate lifts the board by +1.09.
        est = _estimate(1.09, 0.5, current_score=57.32, estimated_score=58.41)
        held = _verdict(est, mode="best", ledger=_ledger(best=59.56))
        self.assertFalse(held.approved)
        self.assertEqual(held.stages[1].code, "BELOW_BEST_IN_RESTORE")
        latest = _verdict(est, mode="latest", ledger=_ledger(best=59.56))
        self.assertTrue(latest.approved, latest.gate_reason)
        self.assertEqual(latest.stages[1].code, "LOW_GAIN_POSITIVE")


class CircuitAdvisoryTest(unittest.TestCase):
    def test_open_breaker_approves_at_half_priority(self) -> None:
        est = _estimate(4.0, 0.5)
        closed = _verdict(est)
        opened = _verdict(est, breaker_state="OPEN")
        self.assertTrue(opened.approved, opened.gate_reason)
        self.assertEqual(opened.stages[2].code, "CIRCUIT_OPEN_ADVISORY")
        self.assertEqual(opened.status_tag, "EXPLORE")
        self.assertIn("[S3 advisory]", opened.gate_reason)
        self.assertAlmostEqual(opened.priority_score, closed.priority_score * 0.5, places=2)

    def test_switch_restores_refusal(self) -> None:
        opened = _verdict(_estimate(4.0, 0.5), breaker_state="OPEN", circuit_blocks=True)
        self.assertFalse(opened.approved)
        self.assertEqual(opened.status_tag, "CIRCUIT_OPEN")


class TleEscapeTest(unittest.TestCase):
    def _state(self, rows, *, arms: bool = False) -> dict:
        with tempfile.TemporaryDirectory() as td:
            gate = SubmissionGate(pathlib.Path(td))
            gate.config.tle_arms_escape_lock = arms
            with mock.patch.object(gate.estimator, "_filtered_outcomes", return_value=rows):
                return gate._escape_state("Op")

    def test_tle_no_longer_locks_but_names_the_commit(self) -> None:
        rows = [{"title": "Op", "status": "Pass", "sha": A, "score": 50.0, "submitted_at": "2026-11-20T01:00:00Z"},
                {"title": "Op", "status": "Time Limit Exceeded", "sha": B, "submission_id": "s-tle",
                 "submitted_at": "2026-11-20T02:00:00Z"}]
        state = self._state(rows)
        self.assertFalse(state["escape_lock"])
        self.assertEqual(state["tle_sha"], B)
        self.assertTrue(self._state(rows, arms=True)["escape_lock"])

    def test_an_older_wrong_answer_still_locks(self) -> None:
        rows = [{"title": "Op", "status": "Pass", "sha": A, "score": 50.0, "submitted_at": "2026-11-20T01:00:00Z"},
                {"title": "Op", "status": "Wrong Answer", "sha": C, "submission_id": "s-wa",
                 "submitted_at": "2026-11-20T02:00:00Z"},
                {"title": "Op", "status": "Time Limit Exceeded", "sha": B, "submission_id": "s-tle",
                 "submitted_at": "2026-11-20T03:00:00Z"}]
        state = self._state(rows)
        self.assertTrue(state["escape_lock"])
        self.assertEqual(state["escape_sha"], C)

    def test_the_timed_out_commit_cannot_go_again_unchanged(self) -> None:
        state = {"tle_sha": B, "tle_submission_id": "s-tle"}
        refused = SubmissionGate._tle_repeat(B, state, proven=False)
        self.assertIsNotNone(refused)
        self.assertEqual(refused.code, "TLE_SHA_REPEAT")
        self.assertIsNone(SubmissionGate._tle_repeat(B, state, proven=True))  # passed online before: noise
        self.assertIsNone(SubmissionGate._tle_repeat(A, state, proven=False))  # a different commit


class JudgeNoiseTest(unittest.TestCase):
    def _journal(self, ws: pathlib.Path, rows: list[dict]) -> None:
        path = ws / "results" / "submit-queue-outcome.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    def test_sigma_from_repeated_judgments_and_the_floor_it_sets(self) -> None:
        def row(sid, sha, score, when):
            return {"submission_id": sid, "title": "Op", "status": "Pass", "sha": sha, "score": score,
                    "submitted_at": when}
        rows = [row("1", A, 50.0, "2026-11-20T01:00:00Z"), row("2", A, 53.0, "2026-11-20T03:00:00Z"),
                row("3", A, 47.0, "2026-11-20T05:00:00Z"), row("4", B, 60.0, "2026-11-20T01:00:00Z"),
                row("5", B, 62.0, "2026-11-20T02:00:00Z"),
                # Three days apart: tbest drift, not judge noise, so these never pair.
                row("6", C, 70.0, "2026-11-20T01:00:00Z"), row("7", C, 61.0, "2026-11-23T01:00:00Z")]
        with tempfile.TemporaryDirectory() as td:
            ws = pathlib.Path(td)
            self._journal(ws, rows)
            info = judge_noise.sigma_for(ws, "Op")
            self.assertEqual(info["df"], 3)
            self.assertEqual(info["scope"], "problem")
            self.assertAlmostEqual(info["sigma"], (20.0 / 3) ** 0.5, places=2)
            gate = SubmissionGate(ws)
            floor, noise = gate._noise_floor("Op")
            self.assertAlmostEqual(floor, 2.58, places=2)
            self.assertEqual(noise["scope"], "problem")
            gate.config.noise_floor_from_judge_sigma = False
            self.assertEqual(gate._noise_floor("Op"), (2.5, None))

    def test_floor_is_clamped_and_falls_back_without_data(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = pathlib.Path(td)
            gate = SubmissionGate(ws)
            self.assertEqual(gate._noise_floor("Op"), (2.5, None))  # no journal
            with mock.patch.object(judge_noise, "sigma_for", return_value={"sigma": 16.0, "df": 9, "scope": "problem"}):
                self.assertEqual(gate._noise_floor("Op")[0], 5.0)
            with mock.patch.object(judge_noise, "sigma_for", return_value={"sigma": 1.0, "df": 9, "scope": "problem"}):
                self.assertEqual(gate._noise_floor("Op")[0], 2.5)


class SwitchTest(unittest.TestCase):
    def test_set_switch_parses_and_rejects(self) -> None:
        from scheduler.gate import GateConfig
        cfg = GateConfig()
        self.assertEqual(cfg.set_switch("circuit_blocks=true"), ("circuit_blocks", True))
        self.assertEqual(cfg.set_switch("low_gain_min_prob=0.6"), ("low_gain_min_prob", 0.6))
        self.assertTrue(cfg.circuit_blocks)
        for bad in ("noise_floor_pts=3", "circuit_blocks", "circuit_blocks=maybe"):
            with self.assertRaises(ValueError):
                cfg.set_switch(bad)

    def test_bridge_saves_switches(self) -> None:
        from scheduler.cli_bridge import SchedulerEngine
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "ws").mkdir()
            with mock.patch.dict("os.environ", {"PARHELION_HOME": str(root / "ph")}, clear=False):
                eng = SchedulerEngine(workspace=root / "ws", state_dir=root / "st")
                out = eng.update_config(gate=["circuit_blocks=on", "tle_arms_escape_lock=true"])
                self.assertTrue(out["config"]["circuit_blocks"])
                again = SchedulerEngine(workspace=root / "ws", state_dir=root / "st")
                self.assertTrue(again.config.circuit_blocks)
                self.assertTrue(again.calibrator.tle_arms_escape_lock)
                with self.assertRaises(ValueError):
                    again.update_config(gate=["keep_best_board=false"])


if __name__ == "__main__":
    unittest.main()
