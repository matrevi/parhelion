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

    def _run(self, approved=True, config=CONFIG):
        cand = {"title": "Op", "sha": SHA, "approved": approved, "status_tag": "READY", "gate_reason": "ok",
                "estimate": {"expected_improvement": 8.9, "estimation_method": "case_bench_paired"}}
        with mock.patch.object(cli_bridge, "read_parhelion_scheduler_config", return_value=dict(config)):
            self.engine._auto_request([cand])
        return self.engine.held_items.get(f"Op:{SHA}")

    def test_requests_an_approved_candidate_after_the_grace_period(self) -> None:
        self._validated(300)
        held = self._run()
        self.assertEqual(held["requested_by"], "platform")
        self.assertEqual(self.saved, 1)

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

    def test_waits_for_a_fresh_receipt_to_reach_the_board(self) -> None:
        self._validated(300)
        (self.ws / "results" / "submit-queue-outcome.jsonl").write_text(
            json.dumps({"title": "Op", "sha": "b" * 40, "queued_at": "q", "status": "Pass",
                        "finished_at": _ago(20)}) + "\n")
        self.assertIsNone(self._run())


if __name__ == "__main__":
    unittest.main()
