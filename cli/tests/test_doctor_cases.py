"""`opsb doctor --cases` reports the prepared-case cache; only `clean --purge-cases --apply` deletes it."""
from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core.commands import doctor_cases  # noqa: E402
from opsb_core.commands.doctor_cmd import register_doctor_subcommands, run_doctor  # noqa: E402


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="opsb")
    register_doctor_subcommands(parser.add_subparsers(dest="command"))
    return parser.parse_args(argv)


def _run(argv: list[str]) -> tuple[int, dict]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = run_doctor(_parse(argv))
    return rc, json.loads(buf.getvalue())


class DoctorCasesTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.home = root / "home"
        self.home.mkdir()
        (self.home / "config.json").write_text(json.dumps({"npu_ssh_host": "dev-host"}))
        self.cache = root / "cache"
        entry = self.cache / "Demo_Op" / ("a" * 40)
        entry.mkdir(parents=True)
        (entry / "input.bin").write_bytes(b"x" * 1000)
        (entry / ".complete").write_text(json.dumps({"bytes": 1000}))
        old = time.time() - 7200
        os.utime(entry, (old, old))
        self.entry = entry
        stats = [{"title": "Demo_Op", "hits": 3, "misses": 1}, {"title": "Demo_Op", "hits": 0, "misses": 4, "evicted": 2}]
        (self.cache / ".stats.jsonl").write_text("".join(json.dumps(s) + "\n" for s in stats))
        env = mock.patch.dict(os.environ, {"PARHELION_HOME": str(self.home), "OPSB_HOME": str(self.home),
                                           "OPSB_CASE_CACHE_DIR": str(self.cache)})
        env.start()
        self.addCleanup(env.stop)

    def test_report_shows_use_budget_hit_rate_and_oldest_entry(self) -> None:
        rc, out = _run(["doctor", "--cases", "--json"])
        self.assertEqual(rc, 0)
        doc = out["cases"]
        self.assertEqual((doc["total_bytes"], doc["entries"], doc["budget_bytes"]), (1000, 1, 24 << 30))
        self.assertEqual([(p["title"], p["bytes"]) for p in doc["problems"]], [("Demo_Op", 1000)])
        self.assertAlmostEqual(doc["oldest_used"], self.entry.stat().st_mtime, places=3)
        self.assertEqual(doc["recent"], {"runs": 2, "hits": 3, "misses": 5, "hit_rate": 0.375, "evicted": 2})
        _, last = _run(["doctor", "--cases", "--recent", "1", "--json"])
        self.assertEqual(last["cases"]["recent"]["hit_rate"], 0.0)

    def test_human_report(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(run_doctor(_parse(["doctor", "--cases"])), 0)
        text = buf.getvalue()
        self.assertIn("Demo_Op", text)
        self.assertIn("37.5% hit rate", text)
        self.assertIn("24.0 GiB global budget", text)

    def test_clean_apply_keeps_the_cache_and_purge_needs_apply(self) -> None:
        _, report = _run(["doctor", "clean", "--apply", "--json"])
        self.assertNotIn("case_purge", report)
        self.assertTrue(self.entry.is_dir())
        _, report = _run(["doctor", "clean", "--purge-cases", "--json"])
        self.assertFalse(report["case_purge"]["applied"])
        self.assertTrue(self.entry.is_dir())
        rc, report = _run(["doctor", "clean", "--purge-cases", "--apply", "--json"])
        self.assertEqual(rc, 0)
        self.assertTrue(report["case_purge"]["applied"])
        self.assertEqual(report["case_purge"]["report"]["purged"], {"entries": 1, "bytes": 1000})
        self.assertFalse(self.entry.exists())
        self.assertTrue((self.cache / ".stats.jsonl").is_file())  # statistics survive a purge

    def test_remote_report_is_read_only_over_batch_mode_ssh(self) -> None:
        calls: list[tuple[list[str], str]] = []
        real_run = subprocess.run

        def fake_run(cmd, **kwargs):
            calls.append((list(cmd), kwargs.get("input") or ""))
            # Execute the shipped script locally, as the host would.
            return real_run([sys.executable, "-"], **kwargs)

        with mock.patch.object(doctor_cases.subprocess, "run", side_effect=fake_run):
            rc, out = _run(["doctor", "--cases", "--remote", "--json"])
        self.assertEqual(rc, 0)
        cmd, script = calls[0]
        self.assertEqual(cmd[:3], ["ssh", "-o", "BatchMode=yes"])
        self.assertIn("dev-host", cmd)
        self.assertIn("PURGE = False", script)
        self.assertEqual(out["cases"]["host"], "dev-host")
        self.assertTrue(self.entry.is_dir())

    def test_remote_report_uses_the_configured_device_cache_dir(self) -> None:
        configured = self.home / "configured-cache"
        configured.mkdir()
        (self.home / "config.json").write_text(json.dumps({
            "npu_ssh_host": "dev-host", "device": {"case_cache_dir": str(configured)}}))
        scripts: list[str] = []
        real_run = subprocess.run

        def fake_run(cmd, **kwargs):
            scripts.append(kwargs.get("input") or "")
            return real_run([sys.executable, "-"], **kwargs)

        with mock.patch.object(doctor_cases.subprocess, "run", side_effect=fake_run), \
                mock.patch.dict(os.environ, {"OPSB_CASE_CACHE_DIR": "", "OPSB_DEVICE_ISOLATED": "0"}):
            rc, out = _run(["doctor", "--cases", "--remote", "--json"])
        self.assertEqual(rc, 0)
        self.assertIn(f"CONFIGURED_DIR = {str(configured)!r}", scripts[0])
        self.assertEqual(out["cases"]["root"], str(configured))

    def test_remote_report_follows_case_tmp_dir_when_no_cache_dir_is_set(self) -> None:
        # Runs keep the cache next to their scratch, so a scratch-only setting moves the report too.
        scratch = self.home / "scratch"
        (scratch / "opsb-case-cache").mkdir(parents=True)
        (self.home / "config.json").write_text(json.dumps({
            "npu_ssh_host": "dev-host", "device": {"case_tmp_dir": str(scratch)}}))
        real_run = subprocess.run
        with mock.patch.object(doctor_cases.subprocess, "run",
                               side_effect=lambda cmd, **kw: real_run([sys.executable, "-"], **kw)), \
                mock.patch.dict(os.environ, {"OPSB_CASE_CACHE_DIR": "", "OPSB_DEVICE_ISOLATED": "0"}):
            rc, out = _run(["doctor", "--cases", "--remote", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(out["cases"]["root"], str(scratch / "opsb-case-cache"))

    def test_unreachable_host_reports_error(self) -> None:
        failed = subprocess.CompletedProcess(["ssh"], 255, stdout="", stderr="Connection reset by peer\n")
        with mock.patch.object(doctor_cases.subprocess, "run", return_value=failed):
            rc, out = _run(["doctor", "--cases", "--remote", "--json"])
        self.assertEqual(rc, 1)
        self.assertIsNone(out["cases"])
        self.assertIn("Connection reset", out["error"])


if __name__ == "__main__":
    unittest.main()
