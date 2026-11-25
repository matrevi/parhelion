"""Unit tests for `opsb_core.probe.npu_stream_probe` (DCMI mock, fallback TTL, /proc/locks, NDJSON stream)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core.probe import npu_stream_probe as sp  # noqa: E402


class TestNpuStreamProbe(unittest.TestCase):
    def test_no_flock_calls_exist_in_stream_probe(self) -> None:
        import ast

        src = Path(sp.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotEqual(alias.name, "fcntl")
            elif isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "fcntl")
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    self.assertNotEqual(func.attr, "flock")
                elif isinstance(func, ast.Name):
                    self.assertNotEqual(func.id, "flock")

    def test_parse_proc_locks_holder_and_waiter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dev0 = Path(tmp) / "bench-device0.lock"
            val = Path(tmp) / "bench-validation.lock"
            dev0.write_text("", encoding="utf-8")
            val.write_text("", encoding="utf-8")
            st0 = os.stat(dev0)
            stv = os.stat(val)

            fake_locks = "\n".join(
                [
                    f"4: FLOCK  ADVISORY  WRITE 1001 08:10:{st0.st_ino} 0 EOF",
                    f"5: -> FLOCK  ADVISORY  WRITE 1002 08:10:{st0.st_ino} 0 EOF",
                    f"6: FLOCK  ADVISORY  READ 1003 08:10:{stv.st_ino} 0 EOF",
                    "7: FLOCK  ADVISORY  WRITE 9999 08:10:999999999 0 EOF",
                    f"8: FLOCK  ADVISORY  WRITE 1234 08:11:{st0.st_ino} 0 EOF",
                ]
            )
            fake_map = (
                {(os.makedev(8, 16), st0.st_ino): str(dev0), (os.makedev(8, 16), stv.st_ino): str(val)},
                {st0.st_ino: str(dev0), stv.st_ino: str(val)},
            )
            orig_open = open

            def _fake_open(path, *args, **kwargs):
                if str(path) == "/proc/locks":
                    import io

                    return io.StringIO(fake_locks)
                return orig_open(path, *args, **kwargs)

            with (
                mock.patch.object(sp, "_known_lock_inodes", return_value=fake_map),
                mock.patch("builtins.open", side_effect=_fake_open),
            ):
                locks = sp.read_locks_procfs()

            self.assertEqual(len(locks), 3)
            self.assertEqual(locks[0]["pid"], 1001)
            self.assertFalse(locks[0]["waiting"])
            self.assertEqual(locks[0]["mode"], "WRITE")
            self.assertEqual(locks[1]["pid"], 1002)
            self.assertTrue(locks[1]["waiting"])
            self.assertEqual(locks[1]["mode"], "WRITE*")
            self.assertEqual(locks[2]["pid"], 1003)
            self.assertEqual(locks[2]["mode"], "READ")

    def test_dcmi_fallback_to_3s_cached_npu_smi(self) -> None:
        fake_smi = {
            "available": True,
            "smi_version": "25.5.0",
            "devices": [
                {
                    "npu_id": 5,
                    "chip_id": 0,
                    "soc_name": "soc-a",
                    "health": "OK",
                    "bus_id": "0000:0A:00.0",
                    "power_w": 98.5,
                    "temp_c": 36,
                    "aicore_pct": 12,
                    "aivector_pct": 44,
                    "hbm_bw_pct": 3,
                    "ctrlcpu_pct": 5,
                    "hbm_used_mib": 3500,
                    "hbm_total_mib": 65536,
                }
            ],
            "npu_processes": [],
            "error": None,
        }
        with (
            mock.patch.object(sp.DcmiProbe, "init", return_value=False),
            mock.patch.object(sp, "read_npu_smi_fallback", return_value=fake_smi) as m_smi,
            mock.patch.object(sp, "read_locks_procfs", return_value=[]),
            mock.patch.object(sp, "read_processes_procfs", return_value={}),
            mock.patch.object(sp, "_parse_proc_stat", return_value={"cpu": (100, 200)}),
            mock.patch.object(sp, "read_memory", return_value={"total_mib": 1000, "used_mib": 200, "available_mib": 800, "total_gib": 1.0, "used_gib": 0.2, "cached_gib": 0.1}),
        ):
            session = sp.StreamProbeSession(stream_mode=True, interval=1.0)
            f1 = session.sample_frame()
            f2 = session.sample_frame()
            self.assertEqual(f1["probe_backend"], "npu-smi")
            self.assertEqual(f2["probe_backend"], "npu-smi")
            self.assertEqual(f1["primary_npu"]["power_w"], 98.5)
            self.assertEqual(f1["primary_npu"]["aivector_pct"], 44)
            # Second call within 3s must hit in-memory cache without re-spawning npu-smi
            self.assertEqual(m_smi.call_count, 1)

    def test_dcmi_backend_and_stream_ndjson(self) -> None:
        fake_dcmi_payload = {
            "available": True,
            "smi_version": "25.5.0",
            "devices": [
                {
                    "npu_id": 5,
                    "chip_id": 0,
                    "soc_name": "soc-a",
                    "health": "OK",
                    "bus_id": "0000:0A:00.0",
                    "power_w": 102.4,
                    "temp_c": 37,
                    "aicore_pct": 0,
                    "aivector_pct": 50,
                    "hbm_bw_pct": 2,
                    "ctrlcpu_pct": 4,
                    "hbm_used_mib": 3600,
                    "hbm_total_mib": 65536,
                }
            ],
            "npu_processes": [{"npu_chip": "5 0", "pid": 4242, "name": "python3", "memory_mib": 128}],
            "error": None,
        }
        lines_out: list[str] = []

        def _capture_stdout(txt: str) -> None:
            lines_out.append(txt)

        with (
            mock.patch.object(sp.DcmiProbe, "init", return_value=True),
            mock.patch.object(sp.DcmiProbe, "refresh_slow_pmu", return_value=True),
            mock.patch.object(sp.DcmiProbe, "sample_fast", return_value=fake_dcmi_payload),
            mock.patch.object(sp, "read_locks_procfs", return_value=[]),
            mock.patch.object(sp, "read_processes_procfs", return_value={}),
            mock.patch.object(sp, "_parse_proc_stat", return_value={"cpu": (100, 200)}),
            mock.patch.object(sp, "read_memory", return_value={"total_mib": 1000, "used_mib": 200, "available_mib": 800, "total_gib": 1.0, "used_gib": 0.2, "cached_gib": 0.1}),
            mock.patch("sys.stdout.write", side_effect=_capture_stdout),
            mock.patch("sys.stdout.flush"),
        ):
            rc = sp.main(["--stream", "--interval", "0.25", "--count", "3"])

        self.assertEqual(rc, 0)
        ndjson_lines = [json.loads(line) for line in "".join(lines_out).splitlines() if line.strip()]
        self.assertEqual(len(ndjson_lines), 3)
        for frame in ndjson_lines:
            self.assertEqual(frame["probe_backend"], "dcmi")
            self.assertIn("sample_ms", frame)
            self.assertEqual(frame["primary_npu"]["soc_name"], "soc-a")
            self.assertEqual(frame["npu_processes"][0]["memory_mib"], 128)


if __name__ == "__main__":
    unittest.main()
