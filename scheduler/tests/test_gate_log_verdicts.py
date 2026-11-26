"""S1 `SAFETY` log-verdict regression tests (real `validation.log` fixtures).

Bug under test: the historical S1 rule was a plain substring scan, so
`results/CTCLossV2/2b9d49de…/validation.log` was rejected because a `git checkout` echo
quoted the *previous* commit subject — `Previous HEAD position was 39fea6c test(det_bad):
print first 24 mismatched (t, s) coordinates on GATE_FAIL` — even though the log ended in
`VALIDATION_PASSED` and that source scored `52.85 -> 56.21 (+3.36)` online.

The rule is now line-anchored and reflog-aware; a green tail is diagnostic only
(`run-npu.sh` prints `VALIDATION_PASSED` unconditionally, so it cannot override a verdict).

    python3 -m unittest discover -s scheduler/tests -v
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from cli.opsb_core.device_env import device_text  # noqa: E402
from scheduler.feedback_log import feedback_log_path, tail_feedback  # noqa: E402
from scheduler.gate import SubmissionGate, parse_log_verdicts  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"

#: Bench worktree prefix of the live workspace: configured, with the neutral default
#: `bench` when nothing is configured.
WORK_PREFIX = device_text("work_prefix")


def _find_workspace(start: pathlib.Path) -> pathlib.Path:
    for cand in (start, *start.parents):
        if (cand / WORK_PREFIX / "problems.json").is_file():
            return cand
    return start.parent


WORKSPACE = _find_workspace(pathlib.Path(__file__).resolve())


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(errors="replace")


class ReflogEchoFalsePositiveTest(unittest.TestCase):
    """The real false-positive: a commit subject on a reflog line must not veto the log."""

    def test_reflog_echo_is_prose_not_a_verdict(self) -> None:
        v = parse_log_verdicts(_fixture("ctclossv2_2b9d49d_reflog_echo.log"))
        self.assertEqual(v["failures"], [])
        self.assertTrue(v["green_tail"])
        self.assertEqual(len(v["echo_tokens_skipped"]), 1)
        self.assertIn("GATE_FAIL", v["echo_tokens_skipped"][0]["text"])
        # fixture carries one provenance comment line, so the echo sits on line 4
        self.assertEqual(v["echo_tokens_skipped"][0]["line"], 4)

    def test_s1_accepts_the_historically_rejected_log(self) -> None:
        gate = SubmissionGate(WORKSPACE)
        self.assertIsNone(gate._check_log_failure_markers(_fixture("ctclossv2_2b9d49d_reflog_echo.log")))

    def test_real_log_bytes_from_results_still_pass(self) -> None:
        real = WORKSPACE / "results/CTCLossV2/2b9d49de0f68d1396199557303c6017588ef800d/validation.log"
        if not real.is_file():
            self.skipTest("live results/ unavailable")
        gate = SubmissionGate(WORKSPACE)
        text = real.read_text(errors="replace")
        self.assertIsNone(gate._check_log_failure_markers(text))
        # the rejected token really is the checkout echo on line 3 of the raw log
        v = parse_log_verdicts(text)
        self.assertEqual(v["echo_tokens_skipped"][0]["line"], 3)
        self.assertIn("GATE_FAIL", v["echo_tokens_skipped"][0]["text"])


class GenuineFailureStillFailsTest(unittest.TestCase):
    def test_anchored_extended_failed_line_fails(self) -> None:
        gate = SubmissionGate(WORKSPACE)
        res = gate._check_log_failure_markers(_fixture("anchored_failure_with_prose.log"))
        self.assertIsNotNone(res)
        self.assertFalse(res.passed)
        self.assertEqual(res.code, "LOG_FAILURE_MARKER")
        self.assertEqual(res.metrics["token"], "EXTENDED_FAILED")
        self.assertEqual(res.metrics["line"], 10)

    def test_nonzero_extended_rc_fails_even_with_green_tail(self) -> None:
        gate = SubmissionGate(WORKSPACE)
        res = gate._check_log_failure_markers(_fixture("layer_norm_extended_rc1_with_green_tail.log"))
        self.assertIsNotNone(res)
        self.assertEqual(res.metrics["token"], "EXTENDED_RC")
        self.assertTrue(res.metrics["green_tail"])  # VALIDATION_PASSED present and ignored

    def test_each_anchored_token_is_fatal(self) -> None:
        gate = SubmissionGate(WORKSPACE)
        for line in (
            "EXTENDED_FAILED case=x",
            "EXTENDED_CASES_FAILED 3/17",
            "VALIDATION_FAILED rc=2",
            "STRESS_CASE_FAIL runs=5 failures=1",
            "NON_DETERMINISTIC run=3",
            "GATE_FAIL batch=7",
            "Wrong Answer: mixed_pad (3/17 mismatched)",
            "EXTENDED_RC=1",
        ):
            res = gate._check_log_failure_markers("EXTENDED_PASSED 17 cases\n" + line + "\nVALIDATION_PASSED\n")
            self.assertIsNotNone(res, line)
            self.assertFalse(res.passed, line)


class ProseContextsAreIgnoredTest(unittest.TestCase):
    """Markers quoted in tables, stack traces, file names or mid-line prose are not verdicts."""

    def test_no_false_positive_from_quoted_contexts(self) -> None:
        log = "\n".join(
            [
                "| case | status |",
                "| odd_prime_3d | GATE_FAIL |",
                'Traceback (most recent call last): File "validation/extended.py", raise SystemExit("GATE_FAIL")',
                "# TODO: port the GATE_FAIL reporter from 39fea6c",
                "note: EXTENDED_FAILED handling lives in validation/run-npu.sh",
                "EXTENDED_PASSED 17 cases",
                "VALIDATION_PASSED",
            ]
        )
        gate = SubmissionGate(WORKSPACE)
        self.assertIsNone(gate._check_log_failure_markers(log))

    def test_leading_runner_tag_is_still_fatal(self) -> None:
        gate = SubmissionGate(WORKSPACE)
        res = gate._check_log_failure_markers("[ERROR] EXTENDED_FAILED case=odd_prime_3d\nVALIDATION_PASSED\n")
        self.assertIsNotNone(res)
        self.assertEqual(res.metrics["token"], "EXTENDED_FAILED")


class AuditSnapshotTest(unittest.TestCase):
    """Guards the queue audit: exactly one historical S1:SAFETY rejection, and it was false."""

    def test_only_the_reflog_echo_was_rejected_by_this_rule(self) -> None:
        queue = WORKSPACE / "results/submit-queue.jsonl"
        if not queue.is_file():
            self.skipTest("submit-queue.jsonl unavailable")
        import json

        hits = [
            json.loads(line)
            for line in queue.read_text(errors="replace").splitlines()
            if line.strip() and "S1:SAFETY" in line
        ]
        for rec in hits:
            self.assertEqual(rec["title"], "CTCLossV2")
            self.assertTrue(rec["sha"].startswith("2b9d49d"))
            log = WORKSPACE / "results" / rec["title"] / rec["sha"] / "validation.log"
            self.assertIsNone(SubmissionGate(WORKSPACE)._check_log_failure_markers(log.read_text(errors="replace")))


def _gate_workspace(root: pathlib.Path) -> tuple[pathlib.Path, str, str]:
    """Minimal workspace with one committed candidate and a validated result."""
    ws = root / "ws"
    title = "LogOp"
    op = ws / WORK_PREFIX / title
    (op / "op_kernel").mkdir(parents=True)
    (op / "op_kernel" / "k.cpp").write_text("// kernel\n", encoding="utf-8")
    for cmd in (
        ["init", "-q"],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "Test"],
        ["add", "."],
        ["commit", "-qm", "v0"],
    ):
        subprocess.run(["git", "-C", str(op), *cmd], check=True)
    sha = subprocess.check_output(["git", "-C", str(op), "rev-parse", "HEAD"], text=True).strip()
    vdir = ws / "results" / title / sha
    vdir.mkdir(parents=True)
    (vdir / "result.json").write_text(
        json.dumps({"title": title, "sha": sha, "exit_code": 0, "finished_at": "2026-11-25T00:00:00Z"}),
        encoding="utf-8",
    )
    (vdir / "validation.log").write_text("EXTENDED_PASSED 3 cases\nVALIDATION_PASSED\n", encoding="utf-8")
    (ws / WORK_PREFIX / "problems.json").write_text(
        json.dumps([{"title": title, "slug": "logop", "path": f"{WORK_PREFIX}/{title}", "difficulty": 5}]),
        encoding="utf-8",
    )
    (ws / "results" / "submit-queue.jsonl").write_text("", encoding="utf-8")
    (ws / "results" / "score-snapshot.json").write_text("{}", encoding="utf-8")
    return ws, title, sha


class GateLogThrottleTest(unittest.TestCase):
    """Repeated status refreshes must not append one gate line per evaluation."""

    def test_three_identical_evaluations_log_one_gate_record(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws, title, sha = _gate_workspace(pathlib.Path(td))
            gate = SubmissionGate(ws)
            verdicts = [gate.evaluate(title, sha) for _ in range(3)]
            self.assertEqual(len({(v.status_tag, v.approved, v.gate_reason) for v in verdicts}), 1)
            rows = tail_feedback(ws, kind="gate")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["title"], title)
            self.assertEqual(rows[0]["sha"], sha[:7])
            self.assertEqual(len(feedback_log_path(ws).read_text(encoding="utf-8").splitlines()), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
