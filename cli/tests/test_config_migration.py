"""Unit tests for ~/.parhelion/ migration, PARHELION_HOME override, and portable workspace resolution."""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))


class ConfigMigrationTests(unittest.TestCase):
    def test_auto_migrates_legacy_config_and_daemon_into_parhelion(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td)
            legacy_opsb = fake_home / ".config" / "opsb"
            legacy_parhelion = fake_home / ".config" / "parhelion"
            legacy_opsb.mkdir(parents=True)
            legacy_parhelion.mkdir(parents=True)

            ws_dir = fake_home / "bench"
            (ws_dir / "bench").mkdir(parents=True)
            (ws_dir / "bench" / "problems.json").write_text("[]", encoding="utf-8")

            (legacy_opsb / "config.json").write_text(
                json.dumps({"active_workspace": str(ws_dir), "workspaces": [str(ws_dir)], "npu_ssh_host": "npu-custom"}),
                encoding="utf-8",
            )
            (legacy_opsb / "cj_accounts.json").write_text(json.dumps({"accounts": [{"id": "teammate-2"}]}), encoding="utf-8")
            (legacy_parhelion / "daemon.json").write_text(json.dumps({"pid": 123, "port": 18910, "token": "tok"}), encoding="utf-8")
            (legacy_parhelion / "daemon.log").write_text("started\n", encoding="utf-8")

            with mock.patch("pathlib.Path.home", return_value=fake_home), mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("PARHELION_HOME", None)
                os.environ.pop("OPSB_HOME", None)
                import opsb_core.config as cfg_mod

                cfg_mod = importlib.reload(cfg_mod)
                loaded = cfg_mod.ConfigRepository.load()
                self.assertEqual(loaded["active_workspace"], str(ws_dir))
                self.assertEqual(loaded["npu_ssh_host"], "npu-custom")
                self.assertTrue((fake_home / ".parhelion" / "config.json").is_file())
                self.assertTrue((fake_home / ".parhelion" / "cj_accounts.json").is_file())
                self.assertTrue((fake_home / ".parhelion" / "daemon.json").is_file())
                self.assertTrue((fake_home / ".parhelion" / "logs" / "daemon.log").is_file())

    def test_empty_workspace_discovery_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td)
            p_home = fake_home / "custom_parhelion"
            empty_cwd = fake_home / "empty_dir"
            empty_cwd.mkdir()

            with (
                mock.patch("pathlib.Path.home", return_value=fake_home),
                mock.patch("pathlib.Path.cwd", return_value=empty_cwd),
                mock.patch.dict(os.environ, {"PARHELION_HOME": str(p_home)}, clear=False),
            ):
                os.environ.pop("OPSB_WORKSPACE", None)
                import opsb_core.config as cfg_mod

                cfg_mod = importlib.reload(cfg_mod)
                with mock.patch.object(cfg_mod, "SUPERVISOR_ROOT", fake_home / "App.app" / "Contents" / "Resources"):
                    loaded = cfg_mod.ConfigRepository.load()
                    self.assertEqual(loaded["active_workspace"], "")
                    self.assertEqual(loaded["workspaces"], [])

    def test_workspace_scoped_bench_specs_state_and_shared_breakthrough(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td)
            p_home = fake_home / "parhelion_home"
            ws_a = fake_home / "contest_a"
            ws_b = fake_home / "contest_b"
            ws_a.mkdir()
            ws_b.mkdir()
            with mock.patch.dict(os.environ, {"PARHELION_HOME": str(p_home)}, clear=False):
                import opsb_core.config as cfg_mod

                cfg_mod = importlib.reload(cfg_mod)
                self.assertEqual(cfg_mod.breakthrough_dir(), p_home.resolve() / "breakthrough")
                self.assertEqual(cfg_mod.workspace_bench_specs_dir(ws_a).parent.parent, p_home.resolve() / "workspaces")
                self.assertNotEqual(cfg_mod.workspace_data_dir(ws_a), cfg_mod.workspace_data_dir(ws_b))
                self.assertTrue(cfg_mod.workspace_state_dir(ws_a).name == "state")
                self.assertTrue(cfg_mod.workspace_bench_specs_dir(ws_a).name == "bench-specs")

    def test_concurrent_migration_safe_and_never_copies_ephemeral_caches(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td)
            legacy_opsb = fake_home / ".config" / "opsb"
            legacy_opsb.mkdir(parents=True)
            (legacy_opsb / "config.json").write_text(json.dumps({"language": "zh"}), encoding="utf-8")
            (legacy_opsb / "cj_accounts.json").write_text(json.dumps({"accounts": []}), encoding="utf-8")
            (legacy_opsb / ".cache_scheduler_bridge.json").write_text(json.dumps({"ts": 1.0, "data": {"stale": True}}), encoding="utf-8")
            (legacy_opsb / ".board_slim_cache.json").write_text(json.dumps({"stale": True}), encoding="utf-8")

            with mock.patch("pathlib.Path.home", return_value=fake_home), mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("PARHELION_HOME", None)
                os.environ.pop("OPSB_HOME", None)
                import opsb_core.config as cfg_mod

                cfg_mod = importlib.reload(cfg_mod)
                errors: list[BaseException] = []
                barrier = threading.Barrier(12)

                def worker() -> None:
                    try:
                        barrier.wait(timeout=3.0)
                        cfg_mod.ensure_parhelion_migrated()
                        cfg_mod.ConfigRepository.load()
                    except BaseException as exc:  # pragma: no cover
                        errors.append(exc)

                threads = [threading.Thread(target=worker) for _ in range(12)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=5.0)

                self.assertEqual(errors, [])
                ph = fake_home / ".parhelion"
                self.assertTrue((ph / "config.json").is_file())
                self.assertTrue((ph / "cj_accounts.json").is_file())
                # Ephemeral TTL caches must never be migrated from legacy directories
                self.assertFalse((ph / "cache" / ".cache_scheduler_bridge.json").exists())
                self.assertFalse((ph / "cache" / ".board_slim_cache.json").exists())
                # No leftover .migtmp temp files
                self.assertEqual(list(ph.rglob("*.migtmp")), [])
                # Subsequent invalidate + load must not copy stale cache back
                cfg_mod.invalidate_ttl_cache("scheduler_bridge")
                cfg_mod.ConfigRepository.load()
                self.assertFalse((ph / "cache" / ".cache_scheduler_bridge.json").exists())
                self.assertIn(ph.resolve(), cfg_mod._MIGRATED_HOMES)


if __name__ == "__main__":
    unittest.main()
