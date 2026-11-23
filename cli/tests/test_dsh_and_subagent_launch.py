"""Unit tests for DeepSeek Harness Desktop driver (`dsh_driver.py`) and 10-op subagent launch (`ALL`)."""
from __future__ import annotations

import fcntl
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

from opsb_core.drivers.brief import MAX_BRIEF_BYTES, compose_contest_subagent_brief  # noqa: E402
from opsb_core.drivers.dsh_driver import DeepSeekDriver, _dsh_project_key  # noqa: E402
from opsb_core.drivers.registry import launch_problem_session  # noqa: E402


class DshDriverAndSubagentLaunchTests(unittest.TestCase):
    def test_dsh_project_key_matches_persistence_encoding(self) -> None:
        self.assertEqual(
            _dsh_project_key("/Users/winbeau/Developer/personal/parhelion"),
            "--Users-winbeau-Developer-personal-parhelion--",
        )

    def test_dsh_discover_detects_desktop_bundle_and_bundled_cli(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app = root / "DeepSeek Harness.app"
            (app / "Contents" / "Resources" / "runtime" / "cli" / "bin").mkdir(parents=True, exist_ok=True)
            plist = app / "Contents" / "Info.plist"
            plist.write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>com.deepseek.dsh</string>
  <key>CFBundleShortVersionString</key><string>0.2.0-rc.2</string>
  <key>CFBundleURLTypes</key><array><dict><key>CFBundleURLSchemes</key><array><string>dsh</string></array></dict></array>
</dict></plist>
""",
                encoding="utf-8",
            )
            cli_bin = app / "Contents" / "Resources" / "runtime" / "cli" / "bin" / "dsh"
            cli_bin.write_text("#!/bin/sh\necho 0.2.0-rc.2\n", encoding="utf-8")
            os.chmod(cli_bin, 0o755)

            dsh_home = root / ".dsh"
            (dsh_home / "profiles" / "desktop").mkdir(parents=True, exist_ok=True)

            drv = DeepSeekDriver()
            with (
                mock.patch.object(drv, "_gui_candidates", return_value=[app]),
                mock.patch("opsb_core.drivers.dsh_driver.which_binary", return_value=""),
                mock.patch.dict(os.environ, {"DSH_HOME": str(dsh_home)}, clear=False),
            ):
                diag = drv.discover()
                self.assertTrue(diag.installed)
                self.assertTrue(diag.gui_installed)
                self.assertTrue(diag.auth_ok)
                self.assertEqual(diag.gui_bundle_id, "com.deepseek.dsh")
                self.assertEqual(diag.gui_version, "0.2.0-rc.2")
                self.assertEqual(diag.url_scheme, "dsh")
                self.assertEqual(diag.binary_path, str(cli_bin))
                self.assertEqual(diag.protocol, "acp-stdio")

    def test_dsh_observe_gui_sessions_reads_projcache_and_flock(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = (root / "ops-bench").resolve()
            op_dir = ws / "PadV3Grad"
            op_dir.mkdir(parents=True, exist_ok=True)
            dsh_home = root / ".dsh"
            proj_dir = dsh_home / "storages" / "session_projcache" / "sessions"
            proj_dir.mkdir(parents=True, exist_ok=True)
            (dsh_home / "storages" / "workspace.json").write_text(
                json.dumps(
                    {
                        "global": {"archivedSessionIds": ["sess-arch"]},
                        "tables": {
                            "workspaces": {
                                "ws-1": {"path": str(ws), "title": "ops-bench", "sessionIds": ["sess-run", "sess-idle", "sess-arch"]}
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            def _write_sess(sid: str, cwd: Path, title: str, open_turn: int | None, blank: bool = False) -> None:
                (proj_dir / f"{sid}.json").write_text(
                    json.dumps(
                        {
                            "record": {
                                "identity": {"cwd": str(cwd), "createdAt": 1763900000000},
                                "rows": {
                                    "title": {"val": title},
                                    "sessionListMetadata": {"val": {"blank": blank, "lastPromptAt": 1763900500000}},
                                    "turnBoundary": {"val": {"openTurnStartSeq": open_turn}},
                                    "sessionStats": {"val": {"steps": 12, "openStep": None, "pendingCalls": {}}},
                                    "tokenUsage": {"val": {"totals": {"uncachedInputTokens": 100, "outputTokens": 50, "cacheReadTokens": 20}}},
                                },
                            }
                        }
                    ),
                    encoding="utf-8",
                )

            _write_sess("sess-run", op_dir, "Optimize PadV3Grad", open_turn=5)
            _write_sess("sess-idle", ws, "Contest Overview", open_turn=None)
            _write_sess("sess-arch", ws, "Archived Session", open_turn=None)
            _write_sess("sess-blank", ws, "", open_turn=None, blank=True)

            lock_dir = dsh_home / "sessions" / _dsh_project_key(str(op_dir)) / "sess-run"
            lock_dir.mkdir(parents=True, exist_ok=True)
            lock_file = lock_dir / "session.lock"
            lock_file.write_text("", encoding="utf-8")

            drv = DeepSeekDriver()
            # Simulate active lock held by DSH Desktop Host
            with mock.patch("opsb_core.drivers.dsh_driver.is_flock_held", side_effect=lambda p: p == lock_file):
                rows = drv.observe_gui_sessions(workspace=ws, limit=10, dsh_home=dsh_home)

            by_id = {r["native_session_id"]: r for r in rows}
            self.assertEqual(set(by_id.keys()), {"sess-run", "sess-idle"})
            self.assertEqual(by_id["sess-run"]["status"], "running")
            self.assertEqual(by_id["sess-run"]["problem"], "PadV3Grad")
            self.assertEqual(by_id["sess-run"]["deep_link"], "dsh://open")
            self.assertEqual(by_id["sess-run"]["observed_steps"], 12)
            self.assertEqual(by_id["sess-run"]["tokens_used"], 170)
            self.assertEqual(by_id["sess-idle"]["status"], "idle")

    def test_launch_all_10_ops_subagent_orchestration_brief_and_workspace_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = (root / "ops-bench").resolve()
            (ws / "bench").mkdir(parents=True, exist_ok=True)
            (ws / "results").mkdir(parents=True, exist_ok=True)
            (ws / "bench" / "problems.json").write_text(
                json.dumps(
                    [
                        {"title": "PadV3Grad", "slug": "pad_v3_grad", "problem_id": "p1", "difficulty": 3, "path": "bench/PadV3Grad"},
                        {"title": "ConcatD", "slug": "concat_d", "problem_id": "p2", "difficulty": 2, "path": "bench/ConcatD"},
                    ]
                ),
                encoding="utf-8",
            )
            (ws / "results" / "score-snapshot.json").write_text(
                json.dumps(
                    {
                        "PadV3Grad": {"score": 70.0, "status": "Pass"},
                        "ConcatD": {"score": 75.4, "status": "Pass"},
                    }
                ),
                encoding="utf-8",
            )
            brief = compose_contest_subagent_brief(ws)
            self.assertLessEqual(len(brief.encode("utf-8")), MAX_BRIEF_BYTES)
            self.assertIn("Subagent", brief)
            self.assertIn("PadV3Grad", brief)
            self.assertIn("ConcatD", brief)

            p_home = root / "phome"
            with mock.patch.dict(os.environ, {"PARHELION_HOME": str(p_home)}, clear=False):
                res = launch_problem_session(ws, "ALL", driver_kind="deepseek", mode="gui", dry_run=True)
            self.assertTrue(res["ok"])
            self.assertEqual(res["session"]["problem"], "ALL-10")
            self.assertEqual(Path(res["worktree"]).resolve(), ws)
            self.assertEqual(res["deep_link"], "dsh://open")
            self.assertIn("Subagent", res["brief"])

    def test_list_all_sessions_preserves_multi_driver_quota(self) -> None:
        from opsb_core.drivers.registry import list_all_sessions

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = (root / "ops-bench").resolve()
            ws.mkdir()
            p_home = root / "phome"
            ag_rows = [
                {"session_id": f"ag-{i}", "driver": "antigravity", "native_session_id": f"ag-{i}",
                 "updated_at": f"2026-11-23 12:{59 - i:02d}:00+00:00", "status": "idle", "problem": "ops-bench"}
                for i in range(30)
            ]
            dsh_rows = [
                {"session_id": f"dsh-{i}", "driver": "deepseek", "native_session_id": f"dsh-{i}",
                 "updated_at": f"2026-11-18T10:{50 - i:02d}:00+00:00", "status": "idle", "problem": "ops-bench"}
                for i in range(10)
            ]
            cx_rows = [
                {"session_id": "cx-1", "driver": "codex", "native_session_id": "cx-1",
                 "updated_at": "2026-11-09T10:00:00+00:00", "status": "idle", "problem": "ops-bench"}
            ]

            def _fake_obs(self_drv, workspace=None, limit=20):
                return {"antigravity": ag_rows, "deepseek": dsh_rows, "codex": cx_rows}.get(self_drv.kind, [])

            with (
                mock.patch.dict(os.environ, {"PARHELION_HOME": str(p_home)}, clear=False),
                mock.patch("opsb_core.drivers.antigravity_driver.AntigravityDriver.observe_gui_sessions", _fake_obs),
                mock.patch("opsb_core.drivers.dsh_driver.DeepSeekDriver.observe_gui_sessions", _fake_obs),
                mock.patch("opsb_core.drivers.codex_driver.CodexDriver.observe_gui_sessions", _fake_obs),
            ):
                merged = list_all_sessions(ws, limit=20)
            self.assertEqual(len(merged), 20)
            drivers_seen = {r["driver"] for r in merged}
            self.assertEqual(drivers_seen, {"antigravity", "deepseek", "codex"})


if __name__ == "__main__":
    unittest.main()
