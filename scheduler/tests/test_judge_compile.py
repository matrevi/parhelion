"""Compile-only check with the judge's toolchain: records, Stage 1 hook and the upload preflight."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from scheduler import judge_compile
from scheduler.gate import SubmissionGate

SHA = "10f21fd26e555c0efd19c13957a3d1e31d854ebf"
# Neutral stand-ins: the real toolchain path and flags live only in ~/.parhelion/config.json.
DEVICE = {"judge_toolchain_root": "/opt/judge-toolchain", "judge_toolchain_env": "",
          "judge_compile_flags": "-DJUDGE_FLAG=1"}
BUILD_ERROR_LOG = """[TEMPLATE] toolchain /opt/judge-toolchain/toolkit
[TEMPLATE] cmake configure
[TEMPLATE] cmake build (-j1)
/srv/work/contest/Op/kernel.asc:12:3: error: use of undeclared identifier 'Foo'
/srv/work/contest/Op/kernel.asc:40:9: error: no matching function for call to 'Bar'
2 errors generated.
[TEMPLATE] command failed (2): bash -lc source /opt/judge-toolchain/set_env.sh && cmake --build build-judge --parallel 1
"""


class JudgeCompileTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = pathlib.Path(tmp.name)
        (self.ws / "opsb.workspace.json").write_text(json.dumps({"problems": [{"title": "Op", "path": "Op"}]}))
        repo = self.ws / "Op"
        repo.mkdir()
        for name in ("CMakeLists.txt", "run.sh", "kernel.asc"):
            (repo / name).write_text("x\n")
        self.folder = self.ws / "results" / "Op" / SHA
        self.device = dict(DEVICE)
        patcher = mock.patch.object(judge_compile, "_device", side_effect=lambda key: self.device.get(key))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fake_runner(self, code: int, log: str, calls: list) -> mock.Mock:
        def run(cmd, **kwargs):
            calls.append((cmd, kwargs["env"]))
            self.folder.mkdir(parents=True, exist_ok=True)
            (self.folder / "judge-build.log").write_text(log)
            return subprocess.CompletedProcess(cmd, code, stdout="{}", stderr="")
        return mock.patch.object(judge_compile.subprocess, "run", side_effect=run)

    def test_unconfigured_check_is_skipped_with_a_hint(self) -> None:
        self.device = {}
        self.assertFalse(judge_compile.settings()["configured"])
        self.assertEqual(judge_compile.run_check(self.ws, "Op", SHA)["status"], "skipped")
        self.assertFalse(judge_compile.record_path(self.ws, "Op", SHA).exists())
        self.assertEqual(judge_compile.stage1(self.ws, "Op", SHA), (None, "; judge compile check: not configured"))
        self.assertEqual(judge_compile.preflight(self.ws, "Op", SHA)["action"], "upload")

    def test_build_failure_is_recorded_and_rejects_stage_one(self) -> None:
        calls: list = []
        with self._fake_runner(2, BUILD_ERROR_LOG, calls):
            record = judge_compile.run_check(self.ws, "Op", SHA, host="dev-host")
        cmd, env = calls[0]
        self.assertIn("--build-only", cmd)
        self.assertEqual(cmd[cmd.index("--npu-host") + 1], "dev-host")
        self.assertEqual(env["OPSB_TOOLCHAIN_ENV"], "/opt/judge-toolchain/set_env.sh")
        self.assertEqual(env["OPSB_COMPILE_CHECK"], "judge")
        self.assertEqual(json.loads(env["OPSB_CMAKE_ARGS"]), ["-DJUDGE_FLAG=1"])
        self.assertEqual((record["status"], record["ok"]), ("fail", False))
        self.assertEqual(record["summary"], "kernel.asc:12:3: error: use of undeclared identifier 'Foo' | "
                                            "kernel.asc:40:9: error: no matching function for call to 'Bar'")
        rejected, note = judge_compile.stage1(self.ws, "Op", SHA)
        self.assertEqual(rejected["code"], "JUDGE_COMPILE_FAILED")
        self.assertIn("undeclared identifier", rejected["reason"])
        self.assertEqual(judge_compile.preflight(self.ws, "Op", SHA)["action"], "hold")

    def test_transport_or_configure_errors_never_block(self) -> None:
        log = "[TEMPLATE] command failed (1): bash -lc source /x/set_env.sh && cmake -S . -B build-judge\n"
        with self._fake_runner(1, log, []):
            self.assertEqual(judge_compile.run_check(self.ws, "Op", SHA)["status"], "error")
        self.assertIsNone(judge_compile.stage1(self.ws, "Op", SHA)[0])
        self.assertEqual(judge_compile.preflight(self.ws, "Op", SHA)["action"], "upload")
        self.assertEqual(judge_compile.classify(255, "kex_exchange_identification: Connection reset"), "error")

    def test_pass_and_stale_identity(self) -> None:
        with self._fake_runner(0, "[TEMPLATE] cmake build (-j1)\n", []):
            self.assertEqual(judge_compile.run_check(self.ws, "Op", SHA)["status"], "pass")
        self.assertEqual(judge_compile.stage1(self.ws, "Op", SHA), (None, "; compiles with the judge toolchain"))
        self.device["judge_compile_flags"] = ["-DOTHER=1"]  # another configuration: the record is stale
        self.assertIsNone(judge_compile.read_record(self.ws, "Op", SHA))
        self.assertIn("pending", judge_compile.stage1(self.ws, "Op", SHA)[1])

    def test_preflight_starts_one_background_check(self) -> None:
        child = types.SimpleNamespace(pid=os.getpid())  # alive for the duration of the test
        with mock.patch.object(judge_compile.subprocess, "Popen", return_value=child) as popen:
            first = judge_compile.preflight(self.ws, "Op", SHA)
            second = judge_compile.preflight(self.ws, "Op", SHA)
        self.assertEqual((first["action"], first["status"]), ("wait", "started"))
        self.assertEqual((second["action"], second["status"]), ("wait", "running"))
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(popen.call_args.args[0][1:4], ["-m", "scheduler.judge_compile", "check"])

    def test_gate_stage_one_uses_the_record(self) -> None:
        self.folder.mkdir(parents=True)
        (self.folder / "result.json").write_text(json.dumps(
            {"exit_code": 0, "sha": SHA, "scheme": "reference", "run_id": "a" * 32}))
        (self.folder / "validation.log").write_text("")
        gate = SubmissionGate(self.ws)
        calibrator = types.SimpleNamespace(inspect_escape_state=lambda *a, **k: {})
        report = {"run_id": "a" * 32, "cases": [{"id": "01"}, {"id": "02"}]}
        with mock.patch.object(gate.estimator, "get_calibrator", return_value=calibrator), \
                mock.patch("scheduler.case_bench.formal_evidence", return_value=report):
            self.device = {}
            unconfigured = gate.evaluate_safety("Op", SHA)
            self.device = dict(DEVICE)
            with self._fake_runner(2, BUILD_ERROR_LOG, []):
                judge_compile.run_check(self.ws, "Op", SHA)
            failed = gate.evaluate_safety("Op", SHA)
        self.assertTrue(unconfigured.passed)
        self.assertEqual(unconfigured.reason, "Full reference suite passed (2 cases); judge compile check: not configured")
        self.assertFalse(failed.passed)
        self.assertEqual(failed.code, "JUDGE_COMPILE_FAILED")


if __name__ == "__main__":
    unittest.main()
