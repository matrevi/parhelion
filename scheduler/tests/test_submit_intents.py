"""4.3 P23 submission intents: exploit, probe (undo + budget), restore (pricing), opening window."""
from __future__ import annotations

import dataclasses
import datetime
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from cli.opsb_core.device_env import device_text  # noqa: E402
from scheduler import submit_intent  # noqa: E402
from scheduler.estimator import ScoreEstimate, ScoreEstimator, _normal_cdf  # noqa: E402
from scheduler.gate import (  # noqa: E402
    GATE_SWITCHES, GateConfig, STAGE_NEXT_STEPS, StageResult, SubmissionGate, with_next_step,
)

WORK_PREFIX = device_text("work_prefix")
UTC = datetime.timezone.utc
BASE, OTHER = "b" * 40, "a" * 40

PASS_LOG = "EXTENDED_PASSED 12 cases\nEXTENDED_RC=0\n"


def _estimate(delta: float = 0.5, sigma: float = 2.0, **extra) -> ScoreEstimate:
    fields = dict(
        title="IntOp", sha="c" * 40, short_sha="c" * 7, difficulty=3, board_status="Pass",
        current_score=50.0, estimated_score=50.0 + delta, delta_expected=delta, weighted_delta=3 * delta,
        confidence_sigma=sigma, prob_positive=_normal_cdf(delta / sigma) if sigma else (1.0 if delta > 0 else 0.0),
        recoverable_headroom=0.0, effective_speedup=1.0, estimation_method="case_bench_paired",
        baseline_sha=BASE, decay_factor_gamma=1.0, raw_historical_best_score=55.0,
        contemporary_best_score=55.0, true_restore_gain=0.0, matched_cases=12, improved_cases=8,
        regressed_cases=[], detail="", calibration_trust=0.4,
        expected_improvement=round(max(0.0, delta) + sigma * 0.4 / 2.0, 2),
    )
    fields.update(extra)
    return ScoreEstimate(**fields)


def _make_workspace(root: pathlib.Path, *, board: float = 50.0, board_status: str = "Pass",
                    mode: str = "latest", receipts: list[dict] | None = None,
                    queue: list[dict] | None = None, title: str = "IntOp",
                    validated: tuple[str, ...] = (BASE,), active_sid: str | None = None,
                    probes: int = 0) -> tuple[pathlib.Path, str, str]:
    ws = root / "ws"
    op = ws / WORK_PREFIX / title
    (op / "op_host").mkdir(parents=True)
    (op / "op_kernel").mkdir()
    (op / "op_host" / "h.cpp").write_text("// host\n", encoding="utf-8")
    (op / "op_kernel" / "k.cpp").write_text("// kernel\n", encoding="utf-8")
    for cmd in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "T"],
                ["add", "."], ["commit", "-qm", "v0"]):
        subprocess.run(["git", "-C", str(op), *cmd], check=True)
    sha = subprocess.check_output(["git", "-C", str(op), "rev-parse", "HEAD"], text=True).strip()
    vdir = ws / "results" / title / sha
    vdir.mkdir(parents=True)
    (vdir / "result.json").write_text(
        json.dumps({"title": title, "sha": sha, "exit_code": 0, "layout": "template",
                    "finished_at": "2026-11-25T00:00:00Z"}), encoding="utf-8")
    (vdir / "validation.log").write_text(PASS_LOG, encoding="utf-8")
    for one in validated:  # a SHA that passed online keeps its validation record for S1
        extra = ws / "results" / title / one
        extra.mkdir(parents=True, exist_ok=True)
        (extra / "result.json").write_text(
            json.dumps({"title": title, "sha": one, "exit_code": 0, "layout": "template"}), encoding="utf-8")
        (extra / "validation.log").write_text(PASS_LOG, encoding="utf-8")
    for idx in range(probes):  # acknowledged judge submissions: they spend the opening window
        ack = ws / "results" / title / f"ack-{idx}"
        ack.mkdir(parents=True, exist_ok=True)
        (ack / "submission-receipt.json").write_text(json.dumps({"submission_id": f"ack-{idx}"}),
                                                     encoding="utf-8")
    (ws / "results" / title / ".judge-rule.json").write_text(
        json.dumps({"ranking_submission_mode": mode}), encoding="utf-8")
    results = ws / "results"
    def _rows(items: list[dict] | None) -> str:
        """`@cand` stands for this fixture's commit, so a receipt can name the SHA it validated."""
        return "".join(json.dumps({k: (sha if v == "@cand" else v) for k, v in r.items()}) + "\n"
                       for r in items or [])
    (results / "submit-queue-outcome.jsonl").write_text(_rows(receipts), encoding="utf-8")
    (results / "submit-queue.jsonl").write_text(_rows(queue), encoding="utf-8")
    row = {"score": board, "status": board_status}
    if active_sid:
        row["submission_id"] = active_sid
    snapshot = {title: row} if board or board_status else {}
    (results / "score-snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
    (ws / WORK_PREFIX / "problems.json").write_text(
        json.dumps([{"title": title, "slug": title.lower(), "path": f"{WORK_PREFIX}/{title}",
                     "difficulty": 3}]), encoding="utf-8")
    return ws, title, sha


def _pass_row(sha: str, score: float, *, when: str, title: str = "IntOp", sid: str | None = None,
              **extra) -> dict:
    row = {"title": title, "sha": sha, "status": "Pass", "score": score,
           "submission_id": sid or f"sid-{sha[:5]}-{when}", "submitted_at": when, "queued_at": when}
    row.update(extra)
    return row


def _verdict(root: pathlib.Path, *, intent: str = "exploit", estimate: ScoreEstimate | None = None,
             config: GateConfig | None = None, candidate: str | None = None, **ws_kwargs):
    """Judge one candidate with the real ledger, budget and pricing; only the estimate is synthetic."""
    ws, title, sha = _make_workspace(root, **ws_kwargs)
    gate = SubmissionGate(ws, config=config or GateConfig())
    sha = candidate or sha
    est = estimate or _estimate(sha=sha, short_sha=sha[:7])
    with mock.patch.object(gate.estimator, "estimate", return_value=est):
        return ws, gate, gate._evaluate_impl(title, sha, intent=intent)


class ProbeIntentTest(unittest.TestCase):
    def test_probe_with_a_rollback_target_is_approved(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws, gate, verdict = _verdict(
                pathlib.Path(td), intent="probe",
                receipts=[_pass_row(BASE, 55.0, when="2026-11-20T00:00:00Z")])
            self.assertTrue(verdict.approved)
            self.assertEqual(verdict.status_tag, "PROBE")
            self.assertEqual(verdict.intent, "probe")
            self.assertEqual(verdict.intent_code, "PROBE_INTENT")
            s2 = verdict.stages[1]
            self.assertEqual(s2.code, "PROBE_INTENT")
            self.assertTrue(s2.metrics["undo"]["exists"])
            self.assertEqual(s2.metrics["undo"]["kind"], "proven_sha_at_or_above_board")
            # priority = weight * E[max(0, gain)] * 0.5
            self.assertAlmostEqual(verdict.priority_score,
                                   round(3 * verdict.estimate.expected_improvement * 0.5, 2))
            # the reply names the rollback for a failed probe
            self.assertIn("--intent restore", s2.metrics["next_step"])
            self.assertIn(BASE[:7], s2.metrics["restore_command"])

    def test_probe_without_undo_is_judged_as_exploit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _, _, verdict = _verdict(
                pathlib.Path(td), intent="probe",
                receipts=[_pass_row(BASE, 40.0, when="2026-11-20T00:00:00Z")])
            self.assertEqual(verdict.intent, "exploit")
            self.assertEqual(verdict.intent_code, "PROBE_NO_UNDO")
            self.assertEqual(verdict.requested_intent, "probe")
            self.assertIn("no rollback target", verdict.intent_reason)
            self.assertNotEqual(verdict.status_tag, "PROBE")

    def test_probe_budget_spent_is_judged_as_exploit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            now = datetime.datetime.now(UTC)
            recent = (now - datetime.timedelta(hours=1)).isoformat()
            _, _, verdict = _verdict(
                pathlib.Path(td), intent="probe",
                receipts=[_pass_row(BASE, 55.0, when="2026-11-20T00:00:00Z"),
                          _pass_row(OTHER, 51.0, when=recent, sid="p1", intent="probe"),
                          _pass_row(OTHER, 52.0, when=recent, sid="p2", intent="probe")])
            self.assertEqual(verdict.intent, "exploit")
            self.assertEqual(verdict.intent_code, "PROBE_BUDGET_SPENT")
            self.assertIn("budget spent", verdict.intent_reason)

    def test_probe_budget_rolls_over_after_the_window(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            old = (datetime.datetime.now(UTC) - datetime.timedelta(hours=20)).isoformat()
            _, _, verdict = _verdict(
                pathlib.Path(td), intent="probe",
                receipts=[_pass_row(BASE, 55.0, when="2026-11-20T00:00:00Z"),
                          _pass_row(OTHER, 51.0, when=old, sid="p1", intent="probe"),
                          _pass_row(OTHER, 52.0, when=old, sid="p2", intent="probe")])
            self.assertEqual(verdict.intent, "probe")
            self.assertTrue(verdict.stages[1].metrics["budget"]["allowed"])

    def test_probe_below_min_probability_is_judged_as_exploit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _, _, verdict = _verdict(
                pathlib.Path(td), intent="probe", estimate=_estimate(delta=-2.0, sigma=1.0),
                receipts=[_pass_row(BASE, 55.0, when="2026-11-20T00:00:00Z")])
            self.assertEqual(verdict.intent_code, "PROBE_NO_ODDS")
            self.assertEqual(verdict.intent, "exploit")

    def test_probe_with_an_unknown_estimate_is_allowed_and_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _, _, verdict = _verdict(
                pathlib.Path(td), intent="probe",
                estimate=_estimate(estimation_method="anchor_fallback", delta=0.0, sigma=3.0),
                receipts=[_pass_row(BASE, 55.0, when="2026-11-20T00:00:00Z")])
            self.assertEqual(verdict.intent, "probe")
            metrics = verdict.stages[1].metrics
            self.assertFalse(metrics["probability_known"])
            self.assertIn("estimate unknown", verdict.stages[1].reason)

    def test_best_board_needs_no_rollback_target(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _, _, verdict = _verdict(pathlib.Path(td), intent="probe", mode="best",
                                     receipts=[_pass_row(BASE, 40.0, when="2026-11-20T00:00:00Z")])
            self.assertEqual(verdict.intent, "probe")
            self.assertEqual(verdict.stages[1].metrics["undo"]["kind"], "best_board_keeps_its_score")

    def test_active_board_sha_holds_as_exploit_and_probes_with_intent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws, title, sha = _make_workspace(
                pathlib.Path(td), receipts=[_pass_row(BASE, 55.0, when="2026-11-20T00:00:00Z")])
            gate = SubmissionGate(ws)
            active = _estimate(sha=BASE, short_sha=BASE[:7], delta=0.0, sigma=3.0,
                               estimation_method="active_board_identity", current_score=50.0)
            with mock.patch.object(gate.estimator, "estimate", return_value=dataclasses.replace(
                    active, sha=sha, short_sha=sha[:7])):
                exploit = gate._evaluate_impl(title, sha)
            self.assertFalse(exploit.approved)
            self.assertEqual(exploit.stages[1].code, "ALREADY_ACTIVE_ON_BOARD")
            with mock.patch.object(gate.estimator, "estimate", return_value=dataclasses.replace(
                    active, sha=sha, short_sha=sha[:7], prob_positive=0.5)):
                probed = gate._evaluate_impl(title, sha, intent="probe")
            self.assertTrue(probed.approved)
            self.assertEqual(probed.status_tag, "PROBE")
            # the reply shows what a re-roll costs: this SHA's own draws and the noise sigma
            self.assertIn("undo", probed.stages[1].metrics)


class RestoreIntentTest(unittest.TestCase):
    def test_restore_prices_the_shas_own_pass_draws(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _, _, verdict = _verdict(
                pathlib.Path(td), intent="restore", board=50.0, active_sid="sid-other",
                receipts=[_pass_row("@cand", 55.0, when="2026-11-20T00:00:00Z"),
                          _pass_row("@cand", 60.0, when="2026-11-20T02:00:00Z", sid="sid-2"),
                          _pass_row(OTHER, 50.0, when="2026-11-21T00:00:00Z", sid="sid-other")])
            pricing = verdict.stages[1].metrics["pricing"]
            self.assertTrue(pricing["applicable"])
            self.assertEqual(pricing["draws"], 2)
            self.assertEqual(pricing["sigma_source"], "constant")  # no repeated judgments to measure
            self.assertAlmostEqual(pricing["sigma"], submit_intent.DEFAULT_SIGMA)
            self.assertAlmostEqual(pricing["gamma"], 1.0)  # the board is its own judged score
            # both draws keep gamma 1 but lose the 0.015 pts/hr residual drift to the anchor
            self.assertAlmostEqual(pricing["expected"], 57.16)
            self.assertAlmostEqual(pricing["gain"], 7.16)
            self.assertAlmostEqual(verdict.stages[1].metrics["pricing"]["expected"],
                                   round(sum(d["contemporary"] for d in pricing["details"]) / 2.0, 2))

    def test_each_draw_is_discounted_to_the_contemporary_board(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _, _, verdict = _verdict(
                pathlib.Path(td), intent="restore", board=40.0, active_sid="sid-other",
                receipts=[_pass_row("@cand", 60.0, when="2026-11-18T00:00:00Z"),
                          _pass_row(OTHER, 40.0, when="2026-11-21T00:00:00Z", sid="sid-other")])
            pricing = verdict.stages[1].metrics["pricing"]
            # gamma = 40/40 = 1, but the draw is three days older than the active judgment: the
            # residual pre-active drift subtracts 0.015 pts/hr from it.
            self.assertAlmostEqual(pricing["details"][0]["contemporary"], 58.92)
            self.assertAlmostEqual(pricing["expected"], 58.92)

    def test_restore_above_the_floor_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _, _, verdict = _verdict(
                pathlib.Path(td), intent="restore", board=50.0, active_sid="sid-other",
                receipts=[_pass_row("@cand", 60.0, when="2026-11-20T00:00:00Z"),
                          _pass_row(OTHER, 50.0, when="2026-11-21T00:00:00Z", sid="sid-other")])
            self.assertTrue(verdict.approved)
            self.assertEqual(verdict.status_tag, "READY")
            self.assertEqual(verdict.stages[1].code, "RESTORE_INTENT")
            self.assertAlmostEqual(verdict.priority_score,
                                   round(3 * verdict.stages[1].metrics["pricing"]["gain"], 2))

    def test_restore_below_the_floor_but_positive_is_low_priority(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _, _, verdict = _verdict(
                pathlib.Path(td), intent="restore", board=50.0, active_sid="sid-other",
                receipts=[_pass_row("@cand", 51.0, when="2026-11-20T00:00:00Z"),
                          _pass_row(OTHER, 50.0, when="2026-11-21T00:00:00Z", sid="sid-other")])
            self.assertTrue(verdict.approved)
            self.assertEqual(verdict.status_tag, "EXPLORE")
            self.assertEqual(verdict.stages[1].code, "RESTORE_INTENT")

    def test_restore_holds_when_the_priced_gain_is_negative(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _, _, verdict = _verdict(
                pathlib.Path(td), intent="restore", board=50.0, active_sid="sid-other",
                receipts=[_pass_row("@cand", 45.0, when="2026-11-20T00:00:00Z"),
                          _pass_row(OTHER, 50.0, when="2026-11-21T00:00:00Z", sid="sid-other")])
            self.assertFalse(verdict.approved)
            self.assertEqual(verdict.stages[1].code, "RESTORE_INTENT")
            self.assertIn("holding", verdict.stages[1].reason)

    def test_restore_is_approved_when_the_board_is_open(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _, _, verdict = _verdict(
                pathlib.Path(td), intent="restore", board=0.0, board_status="Pass",
                receipts=[_pass_row("@cand", 45.0, when="2026-11-20T00:00:00Z")])
            self.assertTrue(verdict.approved)
            self.assertEqual(verdict.status_tag, "CRITICAL_RESTORE")
            self.assertTrue(verdict.is_critical_restore)  # the 4.2.1 "board confirmed zero" relaxation

    def test_restore_is_approved_after_a_failed_shot(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _, _, verdict = _verdict(
                pathlib.Path(td), intent="restore", board=50.0, active_sid="sid-other",
                receipts=[_pass_row("@cand", 45.0, when="2026-11-20T00:00:00Z"),
                          _pass_row(OTHER, 50.0, when="2026-11-21T00:00:00Z", sid="sid-other"),
                          {"title": "IntOp", "sha": OTHER, "status": "Wrong Answer", "score": 0,
                           "submission_id": "sid-wa", "submitted_at": "2026-11-22T00:00:00Z"}])
            self.assertTrue(verdict.approved)
            self.assertEqual(verdict.status_tag, "CRITICAL_RESTORE")

    def test_restore_without_an_online_pass_is_judged_as_exploit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _, _, verdict = _verdict(
                pathlib.Path(td), intent="restore", probes=3,
                config=GateConfig(opening_until_first_score=False))
            self.assertEqual(verdict.intent, "exploit")
            self.assertEqual(verdict.intent_code, "RESTORE_UNPRICED")
            self.assertIn("no online Pass receipt", verdict.intent_reason)

    def test_restore_off_the_best_board_is_held(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _, _, verdict = _verdict(
                pathlib.Path(td), intent="restore", mode="best", board=60.0,
                receipts=[_pass_row("@cand", 55.0, when="2026-11-19T00:00:00Z"),
                          _pass_row(OTHER, 60.0, when="2026-11-20T00:00:00Z")])
            self.assertFalse(verdict.approved)
            self.assertEqual(verdict.intent, "restore")
            self.assertEqual(verdict.intent_code, "RESTORE_NOT_ON_BEST_BOARD")
            self.assertIn("best board keeps its best score", verdict.intent_reason)

    def test_restoring_the_best_board_sha_uses_the_normal_best_rule(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _, _, verdict = _verdict(
                pathlib.Path(td), intent="restore", mode="best", board=40.0,
                receipts=[_pass_row("@cand", 60.0, when="2026-11-20T00:00:00Z")])
            self.assertEqual(verdict.intent, "exploit")  # judged by the 4.2.1 best-board rule
            self.assertEqual(verdict.requested_intent, "restore")
            self.assertEqual(verdict.intent_code, "RESTORE_ON_BEST_BOARD")


class IntentSwitchTest(unittest.TestCase):
    def test_submit_intents_off_judges_every_intent_as_exploit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _, _, verdict = _verdict(
                pathlib.Path(td), intent="probe", config=GateConfig(submit_intents=False),
                receipts=[_pass_row(BASE, 55.0, when="2026-11-20T00:00:00Z")])
            self.assertEqual(verdict.intent, "exploit")
            self.assertEqual(verdict.intent_code, "INTENTS_DISABLED")

    def test_each_intent_switch_restores_the_421_behaviour(self) -> None:
        config = GateConfig()
        self.assertEqual((config.probe_per_window, config.probe_window_hours, config.probe_min_prob),
                         (2, 12.0, 0.3))
        self.assertEqual(config.set_switch("probe_per_window=0"), ("probe_per_window", 0))
        self.assertEqual(config.set_switch("probe_min_prob=0.9"), ("probe_min_prob", 0.9))
        self.assertEqual(config.set_switch("probe_window_hours=6"), ("probe_window_hours", 6.0))
        self.assertEqual(config.set_switch("opening_until_first_score=false"),
                         ("opening_until_first_score", False))
        self.assertEqual(config.set_switch("opening_max_probes=3"), ("opening_max_probes", 3))
        self.assertEqual(config.set_switch("submit_intents=false"), ("submit_intents", False))
        with self.assertRaises(ValueError):
            config.set_switch("probe_per_window=two")
        with self.assertRaises(ValueError):
            config.set_switch("probe_window_hours=soon")

    def test_zero_probe_budget_disables_probes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _, _, verdict = _verdict(
                pathlib.Path(td), intent="probe", config=GateConfig(probe_per_window=0),
                receipts=[_pass_row(BASE, 55.0, when="2026-11-20T00:00:00Z")])
            self.assertEqual(verdict.intent_code, "PROBE_BUDGET_SPENT")


class OpeningWindowTest(unittest.TestCase):
    def _allowance(self, ws: pathlib.Path, title: str, sha: str, **config) -> dict:
        return SubmissionGate(ws, config=GateConfig(**config) if config else GateConfig()).probe_allowance(
            title, sha, unsolved=True)

    def test_window_runs_until_the_first_scored_pass(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws, title, sha = _make_workspace(pathlib.Path(td))
            self.assertEqual(self._allowance(ws, title, sha)["bootstrap"], 8)
            wa = [{"title": title, "sha": OTHER, "status": "Wrong Answer", "score": 0,
                   "submission_id": f"sid-{i}", "submitted_at": f"2026-11-2{i}T00:00:00Z"} for i in range(5)]
            (ws / "results" / "submit-queue-outcome.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in wa), encoding="utf-8")
            probe = self._allowance(ws, title, sha)
            self.assertTrue(probe["allowed"])       # five failures, still no score: keep going
            self.assertFalse(probe["first_score_seen"])
            (ws / "results" / "submit-queue-outcome.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in wa + [_pass_row(OTHER, 51.0, when="2026-11-26T00:00:00Z")]),
                encoding="utf-8")
            scored = self._allowance(ws, title, sha)
            self.assertFalse(scored["allowed"])
            self.assertTrue(scored["first_score_seen"])
            self.assertTrue(scored["until_first_score"])

    def test_cap_and_switch_off(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws, title, sha = _make_workspace(pathlib.Path(td))
            for idx in range(2):
                d = ws / "results" / title / f"{sha[:7]}-probe{idx}"
                d.mkdir(parents=True)
                (d / "submission-receipt.json").write_text(json.dumps({"submission_id": f"sid-{idx}"}),
                                                           encoding="utf-8")
            capped = self._allowance(ws, title, sha, opening_max_probes=2)
            self.assertFalse(capped["allowed"])
            self.assertEqual(capped["bootstrap"], 2)
            baseline = self._allowance(ws, title, sha, opening_until_first_score=False)
            self.assertEqual(baseline["bootstrap"], 3)
            self.assertTrue(baseline["allowed"])

    def test_an_armed_escape_lock_still_stops_an_opening_probe(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws, title, sha = _make_workspace(pathlib.Path(td))
            (ws / "results" / title / "calibration.json").write_text(
                json.dumps({"escape_lock": True, "escape_sha": OTHER, "strata": []}), encoding="utf-8")
            (ws / "results" / "submit-queue-outcome.jsonl").write_text(json.dumps(
                {"title": title, "sha": OTHER, "status": "Wrong Answer", "score": 0,
                 "submission_id": "sid-wa", "submitted_at": "2026-11-21T00:00:00Z"}) + "\n",
                encoding="utf-8")
            gate = SubmissionGate(ws)
            self.assertTrue(gate.probe_allowance(title, sha)["allowed"])  # the window itself is open
            verdict = gate.evaluate(title, sha)
            self.assertFalse(verdict.approved)  # but the lock is never bypassed by it
            self.assertEqual(verdict.stages[0].code, "ESCAPE_LOCK_ACTIVE")


class CalibrationExclusionTest(unittest.TestCase):
    def test_forced_receipts_leave_the_estimator_residuals(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            forced = _pass_row(BASE, 55.0, when="2026-11-20T00:00:00Z", force=True)
            normal = _pass_row(OTHER, 54.0, when="2026-11-21T00:00:00Z")
            ws, title, sha = _make_workspace(
                pathlib.Path(td), receipts=[forced, normal])
            for one in (BASE, OTHER):
                (ws / "results" / title / one / "bench-runs").mkdir(parents=True)
            estimator = ScoreEstimator(ws)
            with mock.patch.object(estimator, "_replay_projection", return_value={"projected": 40.0}):
                rows = estimator._receipt_residuals()
            self.assertEqual([r["residual"] for r in rows], [14.0])  # only the gated receipt
            self.assertEqual(rows[0]["intent"], "exploit")

    def test_the_switch_puts_forced_receipts_back(self) -> None:
        # calibration_excludes_forced=false is the 4.2.1 sample: forced receipts count again.
        with tempfile.TemporaryDirectory() as td:
            forced = _pass_row(BASE, 55.0, when="2026-11-20T00:00:00Z", force=True)
            normal = _pass_row(OTHER, 54.0, when="2026-11-21T00:00:00Z")
            ws, title, sha = _make_workspace(pathlib.Path(td), receipts=[forced, normal])
            for one in (BASE, OTHER):
                (ws / "results" / title / one / "bench-runs").mkdir(parents=True)
            estimator = ScoreEstimator(ws)
            try:
                submit_intent.EXCLUDE_FORCED = False
                with mock.patch.object(estimator, "_replay_projection", return_value={"projected": 40.0}):
                    rows = estimator._receipt_residuals()
            finally:
                submit_intent.EXCLUDE_FORCED = True
            self.assertEqual(sorted(r["residual"] for r in rows), [14.0, 15.0])
            self.assertIn("calibration_excludes_forced", GATE_SWITCHES)

    def test_probe_receipts_stay_and_are_marked(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            probe = _pass_row(BASE, 55.0, when="2026-11-20T00:00:00Z", intent="probe")
            ws, title, sha = _make_workspace(pathlib.Path(td), receipts=[probe])
            (ws / "results" / title / BASE / "bench-runs").mkdir(parents=True)
            estimator = ScoreEstimator(ws)
            with mock.patch.object(estimator, "_replay_projection", return_value={"projected": 40.0}):
                rows = estimator._receipt_residuals()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["intent"], "probe")

    def test_intent_round_trips_through_the_queue_row(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws, title, sha = _make_workspace(pathlib.Path(td))
            (ws / "results" / "submit-queue.jsonl").write_text(
                json.dumps({"title": title, "sha": sha, "queued_at": "2026-11-26T00:00:00Z",
                            "status": "queued", "intent": "probe"}) + "\n", encoding="utf-8")
            self.assertEqual(submit_intent.queued_intent(ws, title, sha), "probe")
            self.assertEqual(submit_intent.resolve_intent(ws, title, sha, None), ("probe", "queue"))
            self.assertEqual(submit_intent.resolve_intent(ws, title, sha, "restore"), ("restore", "request"))
            # a receipt without its own intent field still reads as a probe through its queue row
            self.assertEqual(submit_intent.receipt_intent(
                ws, {"title": title, "sha": sha, "queued_at": "2026-11-26T00:00:00Z"}), "probe")
            # once the shot is judged the row is history: a later submission chooses its own intent
            (ws / "results" / "submit-queue-outcome.jsonl").write_text(
                json.dumps({"title": title, "sha": sha, "queued_at": "2026-11-26T00:00:00Z",
                            "status": "Pass", "score": 51, "submission_id": "sid-1"}) + "\n",
                encoding="utf-8")
            self.assertEqual(submit_intent.queued_intent(ws, title, sha), "")


class NextStepTest(unittest.TestCase):
    def test_every_known_block_code_names_a_next_step(self) -> None:
        for code in STAGE_NEXT_STEPS:
            stage = with_next_step(StageResult(stage="NOISE_FLOOR", passed=False, code=code, reason="r"))
            self.assertTrue(stage.metrics["next_step"].strip(), code)

    def test_unknown_and_exit_codes_still_get_a_step(self) -> None:
        unknown = with_next_step(StageResult(stage="SAFETY", passed=False, code="SOMETHING_NEW", reason="r"))
        self.assertIn("read the stage reason", unknown.metrics["next_step"])
        failed_run = with_next_step(StageResult(stage="SAFETY", passed=False, code="EXIT_CODE_2", reason="r"))
        self.assertIn("result.json", failed_run.metrics["next_step"])

    def test_a_passed_stage_is_not_touched(self) -> None:
        metrics = {"keep": 1}
        passed = with_next_step(StageResult(stage="NOISE_FLOOR", passed=True, code="READY_FOR_TEST",
                                           reason="ok", metrics=dict(metrics)))
        self.assertEqual(passed.metrics, metrics)

    def test_a_blocked_verdict_carries_a_next_step(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _, _, verdict = _verdict(
                pathlib.Path(td), estimate=_estimate(delta=-5.0, sigma=1.0),
                config=GateConfig(opening_until_first_score=False),
                receipts=[_pass_row(BASE, 55.0, when="2026-11-20T00:00:00Z"),
                          _pass_row(OTHER, 54.0, when="2026-11-21T00:00:00Z"),
                          _pass_row(BASE, 53.0, when="2026-11-22T00:00:00Z", sid="sid-3")])
            self.assertFalse(verdict.approved)
            stage = verdict.stages[1]
            self.assertFalse(stage.passed)
            self.assertTrue(str(stage.metrics.get("next_step") or "").strip())


class QueueRowIntentTest(unittest.TestCase):
    def test_the_engine_writes_the_effective_intent_into_the_queue_row(self) -> None:
        from scheduler.cli_bridge import SchedulerEngine

        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            ws, title, sha = _make_workspace(
                root, receipts=[_pass_row(BASE, 55.0, when="2026-11-20T00:00:00Z")])
            engine = SchedulerEngine(workspace=ws, state_dir=root / "state")
            with mock.patch.object(engine.gate.estimator, "estimate",
                                   return_value=_estimate(sha=sha, short_sha=sha[:7])):
                reply = engine.submit(title=title, sha=sha, note="speculative", intent="probe")
            self.assertTrue(reply["enqueued"])
            self.assertEqual(reply["verdict"]["intent"], "probe")
            self.assertEqual(reply["verdict"]["requested_intent"], "probe")
            row = json.loads((ws / "results" / "submit-queue.jsonl").read_text().splitlines()[-1])
            self.assertEqual(row["intent"], "probe")

    def test_a_fallen_back_probe_is_queued_as_the_exploit_it_became(self) -> None:
        from scheduler.cli_bridge import SchedulerEngine

        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            ws, title, sha = _make_workspace(
                root, board=50.0,
                receipts=[_pass_row(BASE, 40.0, when="2026-11-20T00:00:00Z"),
                          _pass_row(OTHER, 45.0, when="2026-11-21T00:00:00Z")])
            engine = SchedulerEngine(workspace=ws, state_dir=root / "state")
            with mock.patch.object(engine.gate.estimator, "estimate",
                                   return_value=_estimate(sha=sha, short_sha=sha[:7])):
                reply = engine.submit(title=title, sha=sha, note="speculative", intent="probe")
            verdict = reply["verdict"]
            self.assertEqual(verdict["intent_code"], "PROBE_NO_UNDO")
            rows = (ws / "results" / "submit-queue.jsonl").read_text().splitlines()
            if reply["enqueued"]:
                self.assertNotIn("intent", json.loads(rows[-1]))
            else:
                self.assertEqual(rows, [])


class GateViewTest(unittest.TestCase):
    def _verdict_dict(self, code: str, *, tag: str, passed: bool = True, next_step: str = "") -> dict:
        metrics = {"next_step": next_step} if next_step else {}
        return {
            "approved": passed, "status_tag": tag,
            "stages": [
                {"stage": "SAFETY", "passed": True, "code": "PASS", "reason": "ok", "metrics": {}},
                {"stage": "NOISE_FLOOR", "passed": passed, "code": code, "reason": "r", "metrics": metrics},
                {"stage": "CIRCUIT_BREAKER", "passed": True, "code": "CIRCUIT_CLOSED", "reason": "ok", "metrics": {}},
                {"stage": "QUOTA_RESERVE", "passed": True, "code": "QUOTA_OK", "reason": "ok", "metrics": {}},
            ],
        }

    def test_probe_and_restore_codes_get_their_badge_and_next_step(self) -> None:
        from cli.opsb_core.services import gate_view

        probe = self._verdict_dict("PROBE_INTENT", tag="PROBE", next_step="roll back with x")
        self.assertEqual(gate_view.badge(probe), "PROBE")
        self.assertEqual(gate_view.next_step(probe), "roll back with x")
        restore = self._verdict_dict("RESTORE_INTENT", tag="READY")
        self.assertEqual(gate_view.badge(restore), "RESTORE")
        critical = self._verdict_dict("RESTORE_INTENT", tag="CRITICAL_RESTORE")
        self.assertEqual(gate_view.badge(critical), "CRITICAL RESTORE")

    def test_a_held_restore_reads_as_a_hold(self) -> None:
        from cli.opsb_core.services import gate_view

        held = self._verdict_dict("RESTORE_INTENT", tag="HOLD_LOW_GAIN", passed=False)
        self.assertEqual(gate_view.badge(held), "HOLD: LOW GAIN")


if __name__ == "__main__":
    unittest.main()
