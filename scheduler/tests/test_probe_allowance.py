"""Opening probe allowance: 2 free submissions per problem while the board is cold."""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

SUP_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(SUP_ROOT) not in sys.path:
    sys.path.insert(0, str(SUP_ROOT))

from scheduler.gate import GateConfig, SubmissionGate  # noqa: E402


def _make_workspace(root: pathlib.Path, *, exit_code: int, log: str) -> tuple[pathlib.Path, str, str]:
    ws = root / "ws"
    title = "ProbeOp"
    op = ws / "bench" / title
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
    (ws / "bench" / "problems.json").write_text(
        json.dumps([{"title": title, "slug": "probeop", "path": f"bench/{title}", "difficulty": 5}]),
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
    def test_rejected_answer_still_gets_two_opening_probes(self) -> None:
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

    def test_unsolved_problem_keeps_probing_after_the_bootstrap_window(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws, title, sha = _make_workspace(pathlib.Path(td), exit_code=2, log=BUILD_OK_LOG)
            for idx in range(5):
                d = ws / "results" / title / f"{sha[:7]}-probe{idx}"
                d.mkdir(parents=True)
                (d / "submission-receipt.json").write_text("{}", encoding="utf-8")
            gate = SubmissionGate(ws)
            probe = gate.probe_allowance(title, sha, unsolved=True)
            self.assertTrue(probe["allowed"])
            self.assertEqual(probe["remaining"], 0)
            self.assertFalse(probe["bootstrap_stage"])
            verdict = gate.evaluate(title, sha)
            self.assertTrue(verdict.approved)
            self.assertIn("Unsolved probe #6", verdict.gate_reason)

    def test_solved_problem_falls_back_to_the_strict_gate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws, title, sha = _make_workspace(pathlib.Path(td), exit_code=2, log=BUILD_OK_LOG)
            (ws / "results" / "score-snapshot.json").write_text(
                json.dumps({title: {"score": 80.0, "status": "Pass"}}), encoding="utf-8"
            )
            gate = SubmissionGate(ws)
            self.assertFalse(gate.probe_allowance(title, sha, unsolved=False)["allowed"])
            self.assertFalse(gate.evaluate(title, sha).approved)

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
            gate = SubmissionGate(ws, config=GateConfig(probe_submissions_per_problem=0, probe_while_unsolved=False))
            self.assertFalse(gate.probe_allowance(title, sha)["allowed"])
            self.assertFalse(gate.evaluate(title, sha).approved)

    def test_passed_board_is_not_a_cold_start(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws, title, sha = _make_workspace(pathlib.Path(td), exit_code=2, log=BUILD_OK_LOG)
            (ws / "results" / "score-snapshot.json").write_text(
                json.dumps({title: {"score": 80.0, "status": "Pass"}}), encoding="utf-8"
            )
            gate = SubmissionGate(ws)
            self.assertFalse(gate.evaluate(title, sha).status_tag == "PROBE")


if __name__ == "__main__":
    unittest.main()
