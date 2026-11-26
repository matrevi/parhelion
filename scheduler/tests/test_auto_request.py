"""Approved candidates that no worker requested are requested by the platform after a grace period."""
from __future__ import annotations

import datetime
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from scheduler import cli_bridge  # noqa: E402
from scheduler.cli_bridge import SchedulerEngine  # noqa: E402

SHA = "a" * 40
CONFIG = {"auto_consumer_enabled": True, "auto_request_grace_sec": 120}
CLEAN = [{"stage": "SAFETY", "code": "SAFETY_OK"}, {"stage": "NOISE_FLOOR", "code": "ABOVE_NOISE_FLOOR"},
         {"stage": "CIRCUIT_BREAKER", "code": "CIRCUIT_CLOSED"}]


def _ago(seconds: float) -> str:
    return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds)).isoformat()


class AutoRequestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = pathlib.Path(self.tmp.name)
        (self.ws / "results" / "Op" / SHA).mkdir(parents=True)
        self.engine = SchedulerEngine.__new__(SchedulerEngine)
        self.engine.workspace = self.ws
        self.engine.held_items = {}
        self.saved = 0
        self.engine._save_state = lambda: setattr(self, "saved", self.saved + 1)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _validated(self, seconds_ago: float) -> None:
        (self.ws / "results" / "Op" / SHA / "result.json").write_text(json.dumps({"finished_at": _ago(seconds_ago)}))

    def _run(self, approved=True, config=CONFIG, stages=None):
        cand = {"title": "Op", "sha": SHA, "approved": approved, "status_tag": "READY", "gate_reason": "ok",
                "stages": stages if stages is not None else CLEAN,
                "estimate": {"expected_improvement": 8.9, "estimation_method": "case_bench_paired"}}
        with mock.patch.object(cli_bridge, "read_parhelion_scheduler_config", return_value=dict(config)):
            self.engine._auto_request([cand])
        return self.engine.held_items.get(f"Op:{SHA}")

    def test_requests_an_approved_candidate_after_the_grace_period(self) -> None:
        self._validated(300)
        held = self._run()
        self.assertEqual(held["requested_by"], "platform")
        self.assertEqual(self.saved, 1)

    def test_never_requests_an_explore_approval_on_the_workers_behalf(self) -> None:
        """4.3.2: below-floor EXPLORE approvals are the worker's bet; in the first 4.3 sprint the
        platform requested 13 of them (E +0.03..+0.94, P 0.50-0.65) for 4 weighted points."""
        self._validated(300)
        low_gain = [dict(s) for s in CLEAN]
        low_gain[1]["code"] = "LOW_GAIN_POSITIVE"
        self.assertIsNone(self._run(stages=low_gain))
        upside = [dict(s) for s in CLEAN]
        upside[1]["code"] = "EXPLORE_HIGH_UPSIDE"
        self.assertIsNone(self._run(stages=upside))
        advisory = [dict(s) for s in CLEAN]
        advisory[2]["code"] = "CIRCUIT_OPEN_ADVISORY"
        self.assertIsNone(self._run(stages=advisory))
        self.assertEqual(self._run()["requested_by"], "platform")

    def test_waits_while_the_worker_may_still_act(self) -> None:
        self._validated(30)
        self.assertIsNone(self._run())

    def test_never_requests_a_held_or_unapproved_candidate(self) -> None:
        self._validated(300)
        self.assertIsNone(self._run(approved=False))

    def test_respects_the_switches(self) -> None:
        self._validated(300)
        self.assertIsNone(self._run(config={"auto_consumer_enabled": False}))
        self.assertIsNone(self._run(config={**CONFIG, "auto_request_candidates": False}))

    def test_waits_for_an_open_submission_of_the_same_problem(self) -> None:
        self._validated(300)
        (self.ws / "results" / "submit-queue.jsonl").write_text(
            json.dumps({"title": "Op", "sha": "b" * 40, "queued_at": _ago(10)}) + "\n")
        self.assertIsNone(self._run())

    def _run_with_view(self, queue):
        cand = {"title": "Op", "sha": SHA, "approved": True, "status_tag": "READY", "gate_reason": "ok", "stages": CLEAN,
                "estimate": {"expected_improvement": 8.9, "estimation_method": "case_bench_paired"}}
        with mock.patch.object(cli_bridge, "read_parhelion_scheduler_config", return_value=dict(CONFIG)):
            self.engine._auto_request([cand], queue)
        return self.engine.held_items.get(f"Op:{SHA}")

    def test_a_stale_queue_row_the_view_retired_does_not_block_the_problem(self) -> None:
        """4.3.1: a row queued days ago whose receipt carries another ``queued_at`` format kept the
        problem "busy" forever, so an approved candidate sat READY and was never submitted."""
        self._validated(300)
        (self.ws / "results" / "submit-queue.jsonl").write_text(
            json.dumps({"title": "Op", "sha": "b" * 40, "queued_at": "2026-11-24T17:00:32.569946+00:00"}) + "\n")
        (self.ws / "results" / "submit-queue-outcome.jsonl").write_text(
            json.dumps({"title": "Op", "sha": "b" * 40, "queued_at": "2026-11-24T17:30:46.875Z", "status": "Pass",
                        "finished_at": "2026-11-24T17:40:00Z"}) + "\n")
        view = [{"kind": "completed", "title": "Op", "sha": "b" * 40}]
        self.assertEqual(self._run_with_view(view)["requested_by"], "platform")

    def test_a_pending_item_in_the_view_still_blocks_the_problem(self) -> None:
        self._validated(300)
        self.assertIsNone(self._run_with_view([{"kind": "pending", "title": "Op", "sha": "b" * 40}]))

    def test_waits_for_a_fresh_receipt_to_reach_the_board(self) -> None:
        self._validated(300)
        (self.ws / "results" / "submit-queue-outcome.jsonl").write_text(
            json.dumps({"title": "Op", "sha": "b" * 40, "queued_at": "q", "status": "Pass",
                        "finished_at": _ago(20)}) + "\n")
        self.assertIsNone(self._run())


if __name__ == "__main__":
    unittest.main()
