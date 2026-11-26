"""Opening probe allowance: first three acknowledged submissions per problem."""
from __future__ import annotations

import dataclasses
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SUP_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(SUP_ROOT) not in sys.path:
    sys.path.insert(0, str(SUP_ROOT))

from cli.opsb_core.device_env import device_text  # noqa: E402
from scheduler.gate import GateConfig, SubmissionGate  # noqa: E402

#: Bench worktree prefix of the live workspace: configured, with the neutral default
#: `bench` when nothing is configured.
WORK_PREFIX = device_text("work_prefix")


def _make_workspace(root: pathlib.Path, *, exit_code: int, log: str) -> tuple[pathlib.Path, str, str]:
    ws = root / "ws"
    title = "ProbeOp"
    op = ws / WORK_PREFIX / title
    (op / "op_host").mkdir(parents=True)
    (op / "op_kernel").mkdir(parents=True)
    (op / "op_host" / "h.cpp").write_text("// host\n", encoding="utf-8")
    (op / "op_kernel" / "k.cpp").write_text("// kernel\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(op), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(op), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(op), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(op), "add", "."], check=True)
    subprocess.run(["git", "-C", str(op), "commit", "-qm", "v0"], check=True)
    sha = subprocess.check_output(["git", "-C", str(op), "rev-parse", "HEAD"], text=True).strip()
    vdir = ws / "results" / title / sha
    vdir.mkdir(parents=True)
    (vdir / "result.json").write_text(
        json.dumps({"title": title, "sha": sha, "exit_code": exit_code,
                    "layout": "template", "phase_a_cpu_sec": 13.4, "finished_at": "2026-11-25T00:00:00Z"}),
        encoding="utf-8",
    )
    (vdir / "validation.log").write_text(log, encoding="utf-8")
    (ws / WORK_PREFIX / "problems.json").write_text(
        json.dumps([{"title": title, "slug": "probeop", "path": f"{WORK_PREFIX}/{title}", "difficulty": 5}]),
        encoding="utf-8",
    )
    (ws / "results" / "submit-queue.jsonl").write_text("", encoding="utf-8")
    (ws / "results" / "score-snapshot.json").write_text("{}", encoding="utf-8")
    return ws, title, sha


BUILD_OK_LOG = (
    "[TEMPLATE] cmake build (-j8)\n"
    "[100%] Built target sample\n"
    "Sample kernel completed; outputs saved for verification.\n"
    "Wrong Answer: idx.bin, mismatched=160/160\n"
)


class ProbeAllowanceTests(unittest.TestCase):
    def test_rejected_answer_still_gets_three_opening_probes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws, title, sha = _make_workspace(pathlib.Path(td), exit_code=2, log=BUILD_OK_LOG)
            gate = SubmissionGate(ws)
            probe = gate.probe_allowance(title, sha, unsolved=True)
            self.assertTrue(probe["allowed"])
            self.assertEqual(probe["remaining"], 3)
            self.assertTrue(probe["bootstrap_stage"])
            verdict = gate.evaluate(title, sha)
            self.assertTrue(verdict.approved)
            self.assertEqual(verdict.status_tag, "PROBE")
            self.assertIn("Probe 1/3", verdict.gate_reason)
            self.assertEqual([s.code for s in verdict.stages], ["PROBE_ALLOWANCE"] * 4)

    def test_probe_waits_for_a_slot_without_losing_approval(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws, title, sha = _make_workspace(pathlib.Path(td), exit_code=2, log=BUILD_OK_LOG)
            gate = SubmissionGate(ws)
            no_slot = dataclasses.replace(gate.get_quota_status(), used_today=50, remaining_today=0,
                                          discretionary_remaining=0)
            with mock.patch.object(gate, "get_quota_status", return_value=no_slot):
                verdict = gate.evaluate(title, sha)
            self.assertTrue(verdict.approved)
            self.assertEqual(verdict.status_tag, "PROBE")
            self.assertEqual([s.code for s in verdict.stages],
                             ["PROBE_ALLOWANCE"] * 3 + ["DAILY_LIMIT_REACHED"])
            self.assertFalse(verdict.stages[3].passed)

    def test_fourth_submission_requires_full_validation_even_when_unsolved(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws, title, sha = _make_workspace(pathlib.Path(td), exit_code=2, log=BUILD_OK_LOG)
            for idx in range(3):
                d = ws / "results" / title / f"{sha[:7]}-probe{idx}"
                d.mkdir(parents=True)
                (d / "submission-receipt.json").write_text(json.dumps({"submission_id": f"sid-{idx}"}), encoding="utf-8")
            gate = SubmissionGate(ws)
            probe = gate.probe_allowance(title, sha, unsolved=True)
            self.assertFalse(probe["allowed"])
            self.assertEqual(probe["remaining"], 0)
            self.assertFalse(probe["bootstrap_stage"])
            verdict = gate.evaluate(title, sha)
            self.assertFalse(verdict.approved)

    def test_early_ac_keeps_remaining_opening_probes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws, title, sha = _make_workspace(pathlib.Path(td), exit_code=2, log=BUILD_OK_LOG)
            (ws / "results" / "score-snapshot.json").write_text(
                json.dumps({title: {"score": 80.0, "status": "Pass"}}), encoding="utf-8"
            )
            gate = SubmissionGate(ws)
            self.assertTrue(gate.probe_allowance(title, sha)["allowed"])
            self.assertTrue(gate.evaluate(title, sha).approved)

    def test_compile_failure_never_gets_a_probe(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws, title, sha = _make_workspace(
                pathlib.Path(td), exit_code=1,
                log="[TEMPLATE] command failed (1): cmake --build build-npu\n",
            )
            gate = SubmissionGate(ws)
            self.assertFalse(gate.probe_allowance(title, sha)["allowed"])
            verdict = gate.evaluate(title, sha)
            self.assertFalse(verdict.approved)

    def test_allowance_can_be_disabled_by_config(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws, title, sha = _make_workspace(pathlib.Path(td), exit_code=2, log=BUILD_OK_LOG)
            gate = SubmissionGate(ws, config=GateConfig(probe_submissions_per_problem=0))
            self.assertFalse(gate.probe_allowance(title, sha)["allowed"])
            self.assertFalse(gate.evaluate(title, sha).approved)

    def test_counts_distinct_judge_ids_including_repeated_sha(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws, title, sha = _make_workspace(pathlib.Path(td), exit_code=2, log=BUILD_OK_LOG)
            results = ws / "results"
            (results / title / sha / "submission-manifest.json").write_text("[]")
            rows = [{"title": title, "sha": sha, "submission_id": sid, "status": "Wrong Answer"}
                    for sid in ["a", "a", "b"]]
            (results / "submit-queue-outcome.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
            probe = SubmissionGate(ws).probe_allowance(title, sha)
            self.assertEqual(probe["remaining"], 1)


if __name__ == "__main__":
    unittest.main()
