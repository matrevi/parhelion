"""Unit tests for DeepSeek Harness Desktop driver (`dsh_driver.py`) and 10-op subagent launch (`ALL`)."""
from __future__ import annotations

import fcntl
import json
import os
import pathlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core.drivers.brief import (  # noqa: E402
    MAX_BRIEF_BYTES,
    MAX_ORCHESTRATOR_BRIEF_BYTES,
    compose_contest_subagent_brief,
    compose_bench_author_brief,
    compose_session_brief,
)
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
            self.assertLessEqual(len(brief.encode("utf-8")), MAX_ORCHESTRATOR_BRIEF_BYTES)
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


    def test_clean_layout_briefs_encode_official_template_rules(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = pathlib.Path(td) / "mat-chem-sim-pred"
            ws.mkdir()
            (ws / "opsb.workspace.json").write_text(
                json.dumps(
                    {
                        "layout": "clean-agents-first",
                        "title": "第二届石油和化工行业人工智能创新大赛-算子赛道",
                        "contest_id": "mat-chem-sim-pred",
                        "contest_url": "https://judge.invalid/public/mat-chem-sim-pred",
                        "git_owner": "winbeau",
                        "repo_prefix": "accel-mat",
                        "problems": [
                            {"title": "NeighborListPbcFixedK", "difficulty": 5, "repo_slug": "accel-mat-neighborlistpbcfixedk", "path": "NeighborListPbcFixedK", "problem_id": "p1"},
                            {"title": "RevINNorm", "difficulty": 2, "repo_slug": "accel-mat-revinnorm", "path": "RevINNorm", "problem_id": "p2"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            for name in ("NeighborListPbcFixedK", "RevINNorm"):
                op = ws / name
                op.mkdir()
                # official template layout marker used by the brief dispatcher
                (op / "CMakeLists.txt").write_text("project(sample LANGUAGES ASC CXX)\n", encoding="utf-8")
                (op / "run.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
                (op / "kernel.asc").write_text("// TODO\n", encoding="utf-8")

            single = compose_session_brief(ws, "NeighborListPbcFixedK")
            self.assertIn("kernel.asc", single)
            self.assertIn("opsb test NeighborListPbcFixedK", single)
            self.assertIn("git push origin HEAD", single)
            self.assertNotIn("git push origin main", single)
            self.assertNotIn("申请测试槽", single)
            self.assertIn("opsb submit NeighborListPbcFixedK", single)
            self.assertIn("parhelion_test_wait", single)
            self.assertLessEqual(len(single.split("\n\nSTATE ")[0].encode("utf-8")), MAX_BRIEF_BYTES)

            bench = compose_bench_author_brief(ws, "NeighborListPbcFixedK")
            self.assertIn("cases_perf.json", bench)
            self.assertIn("不要改 kernel.asc", bench)
            self.assertIn("bench-specs", bench)
            self.assertLessEqual(len(bench.encode("utf-8")), MAX_BRIEF_BYTES)

            sprint = compose_contest_subagent_brief(ws)
            self.assertTrue(sprint.startswith("# Parhelion Contest Orchestrator"))
            self.assertTrue(sprint.isascii())
            self.assertIn("parhelion_sprint_start", sprint)
            self.assertIn("NeighborListPbcFixedK", sprint)
            self.assertIn("RevINNorm", sprint)
            self.assertIn("Each worker directly queues validation", sprint)
            self.assertNotIn("SLOT-GRANT", sprint)
            self.assertIn("Workers enqueue their own candidates via opsb submit", sprint)
            self.assertLessEqual(len(sprint.encode("utf-8")), MAX_ORCHESTRATOR_BRIEF_BYTES)

    def test_new_contest_gui_stages_english_brief_before_templates_arrive(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td).resolve() / "new-contest"
            ws.mkdir()
            manifest = ws / "opsb.workspace.json"
            manifest.write_text(json.dumps({"problems": []}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"PARHELION_HOME": str(ws.parent / "phome")}):
                launch = launch_problem_session(ws, "ALL", driver_kind="deepseek", mode="gui", dry_run=True)
            brief = launch["brief"]
            self.assertEqual(Path(launch["brief_path"]).read_text().strip(), brief)
            self.assertIn(str(ws), brief)
            self.assertTrue(brief.isascii())
            self.assertNotIn("10-Operator", brief)
            for rule in (
                "PUBLIC", "S_SMALL_CORE", "M_MID_UB", "L_HBM_TILED", "X_EXTREME",
                "opening probes until the first scored Pass, at most 8", "even after an early AC",
                "HTTP 429 without a judge ID", "After that, apply the safety, gain and circuit gates",
                "intent=probe", "intent=restore", "Respect manual holds",
                "opsb submit <op> <sha>", "predictions separate from judged scores",
            ):
                self.assertIn(rule, brief)

            problems = [{"title": f"Operator{i:04d}"} for i in range(300)]
            manifest.write_text(json.dumps({"problems": problems}), encoding="utf-8")
            self.assertEqual(compose_contest_subagent_brief(ws, goal="优先恢复零分"), brief + "\n\nUser goal: 优先恢复零分")
            long_goal = compose_contest_subagent_brief(ws, goal="测试" * MAX_ORCHESTRATOR_BRIEF_BYTES)
            self.assertTrue(long_goal.startswith(brief))
            self.assertLessEqual(len(long_goal.encode("utf-8")), MAX_ORCHESTRATOR_BRIEF_BYTES)

    def test_subagent_capacity_is_pinned_to_twenty_idempotently(self) -> None:
        from opsb_core.drivers.dsh_driver import ensure_subagent_capacity

        with tempfile.TemporaryDirectory() as td:
            home = pathlib.Path(td)
            patch = home / "profiles" / "desktop" / "cordis.patch.yml"
            patch.parent.mkdir(parents=True)
            patch.write_text(
                "- id: ui-chat\n  name: '@deepseek-ai/dsh-client-ui-chat'\n  config:\n    transcriptView: standard\n"
                "- id: subagent\n  name: '@deepseek-ai/dsh-subagent'\n  config:\n    maxActiveSubagents: 5\n"
                "- id: ui-theme\n  name: '@deepseek-ai/dsh-client-ui-theme'\n",
                encoding="utf-8",
            )
            first = ensure_subagent_capacity(dsh_home=home)
            self.assertTrue(first["changed"])
            self.assertEqual(first["value"], 20)
            text = patch.read_text(encoding="utf-8")
            self.assertIn("maxActiveSubagents: 20", text)
            self.assertIn("ui-theme", text)          # unrelated entries preserved
            self.assertNotIn("maxActiveSubagents: 5", text)
            second = ensure_subagent_capacity(dsh_home=home)
            self.assertFalse(second["changed"])
            self.assertEqual(second["value"], 20)

    def test_subagent_capacity_adds_block_when_profile_has_none(self) -> None:
        from opsb_core.drivers.dsh_driver import ensure_subagent_capacity

        with tempfile.TemporaryDirectory() as td:
            home = pathlib.Path(td)
            patch = home / "profiles" / "acp" / "cordis.patch.yml"
            patch.parent.mkdir(parents=True)
            patch.write_text("[]\n", encoding="utf-8")
            res = ensure_subagent_capacity(profile="acp", dsh_home=home)
            self.assertTrue(res["changed"])
            self.assertIn("maxActiveSubagents: 20", patch.read_text(encoding="utf-8"))
            self.assertFalse(ensure_subagent_capacity(profile="missing", dsh_home=home)["exists"])

    def test_parhelion_mcp_plugin_is_installed_and_kept_current(self) -> None:
        from opsb_core.drivers.dsh_driver import PARHELION_MCP_PLUGIN, ensure_parhelion_mcp_plugin

        with tempfile.TemporaryDirectory() as td:
            home = pathlib.Path(td)
            patch = home / "profiles" / "desktop" / "cordis.patch.yml"
            patch.parent.mkdir(parents=True)
            patch.write_text("- insert:\n    - id: global-aoci-mcp\n      name: ./global-aoci-mcp.mjs\n", encoding="utf-8")
            first = ensure_parhelion_mcp_plugin(dsh_home=home, command="/py", opsb_script="/cli/opsb")
            self.assertTrue(first["changed"])
            plugin = patch.parent / "global-parhelion-mcp.mjs"
            self.assertEqual(plugin.read_text(encoding="utf-8"), PARHELION_MCP_PLUGIN.read_text(encoding="utf-8"))
            text = patch.read_text(encoding="utf-8")
            self.assertIn("global-aoci-mcp", text)          # existing mounts preserved
            self.assertIn("- id: global-parhelion-mcp", text)
            self.assertIn('command: "/py"', text)
            self.assertFalse(ensure_parhelion_mcp_plugin(dsh_home=home, command="/py", opsb_script="/cli/opsb")["changed"])
            moved = ensure_parhelion_mcp_plugin(dsh_home=home, command="/py2", opsb_script="/cli/opsb")
            self.assertTrue(moved["changed"])
            text = patch.read_text(encoding="utf-8")
            self.assertEqual(text.count("- id: global-parhelion-mcp"), 1)   # rewritten, not duplicated
            self.assertIn('command: "/py2"', text)
            self.assertFalse(ensure_parhelion_mcp_plugin(profile="missing", dsh_home=home)["exists"])

    def _seed_dsh_home(self, root: Path) -> Path:
        home = root / ".dsh"
        store = home / "storages" / "workspace.json"
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(
            json.dumps({"unit": {"name": "workspace", "version": 2}, "global": {"workspaceIds": []}, "tables": {"workspaces": {}}}),
            encoding="utf-8",
        )
        return home

    def test_ensure_dsh_workspace_registers_folder_and_links_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = self._seed_dsh_home(root)
            ws = root / "mat-chem-sim-pred" / "FlashAttentionScore"
            ws.mkdir(parents=True)
            drv = DeepSeekDriver()

            first = drv.ensure_dsh_workspace(ws, session_id="sess-alpha", dsh_home=home)
            self.assertTrue(first["ok"])
            self.assertTrue(first["created"])
            self.assertTrue(first["session_linked"])

            doc = json.loads((home / "storages" / "workspace.json").read_text(encoding="utf-8"))
            entry = doc["tables"]["workspaces"][first["workspace_id"]]
            self.assertEqual(entry["path"], os.path.realpath(ws))
            self.assertEqual(entry["title"], "FlashAttentionScore")
            self.assertEqual(entry["sessionIds"], ["sess-alpha"])
            self.assertIn(first["workspace_id"], doc["global"]["workspaceIds"])
            self.assertTrue((home / "storages" / "workspace.json.parhelion.bak").is_file())

            again = drv.ensure_dsh_workspace(ws, session_id="sess-alpha", dsh_home=home)
            self.assertTrue(again["ok"])
            self.assertFalse(again["created"])
            self.assertFalse(again["session_linked"])
            self.assertEqual(again["workspace_id"], first["workspace_id"])

    def test_ensure_dsh_workspace_rejects_unknown_schema(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / ".dsh"
            store = home / "storages" / "workspace.json"
            store.parent.mkdir(parents=True, exist_ok=True)
            store.write_text(json.dumps({"unit": {"name": "workspace", "version": 99}, "tables": {}}), encoding="utf-8")
            before = store.read_text(encoding="utf-8")
            res = DeepSeekDriver().ensure_dsh_workspace(root / "ws", dsh_home=home)
            self.assertFalse(res["ok"])
            self.assertIn("unsupported", res["reason"])
            self.assertEqual(store.read_text(encoding="utf-8"), before)

    def test_queue_dsh_prompt_appends_inbox_splice_frame(self) -> None:
        import shutil
        import subprocess

        zstd_bin = shutil.which("zstd")
        if not zstd_bin:
            self.skipTest("zstd CLI unavailable")
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "session.v4.jsonl.zstd"
            header = (
                '{"type":"session","version":4,"id":"sess-1","cwd":"/tmp/ws","createdAt":1,'
                '"isSeeded":false,"delegationDepth":0}\n'
                '{"type":"approval/policy","seq":2,"time":1,"data":{"policy":"never"}}\n'
            ).encode()
            log.write_bytes(subprocess.run([zstd_bin, "-q", "-c"], input=header, capture_output=True).stdout)

            self.assertTrue(DeepSeekDriver._queue_dsh_prompt(log, "Parhelion brief body"))
            raw = subprocess.run([zstd_bin, "-dc", str(log)], capture_output=True).stdout.decode()
            lines = [json.loads(x) for x in raw.splitlines() if x.strip()]
            self.assertEqual(lines[0]["type"], "session")
            spliced = lines[-1]
            self.assertEqual(spliced["type"], "agent/inbox/spliced")
            self.assertEqual(spliced["seq"], 3)
            self.assertEqual(spliced["data"]["target"], "next-turn")
            self.assertEqual(spliced["data"]["inserted"][0]["content"][0]["text"], "Parhelion brief body")
            self.assertEqual(spliced["data"]["inserted"][0]["role"], "user")

    def test_gui_launch_quits_running_desktop_before_seeding(self) -> None:
        order: list[str] = []

        class _Diag:
            gui_installed = True
            installed = True
            gui_app_path = "/Applications/DeepSeek Harness.app"
            detail = "ready"

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = root / "ws"
            ws.mkdir()
            drv = DeepSeekDriver()
            with (
                mock.patch.dict(os.environ, {"PARHELION_HOME": str(root / "phome"), "DSH_HOME": str(root / "dsh-home")}, clear=False),
                mock.patch.object(DeepSeekDriver, "discover", return_value=_Diag()),
                mock.patch.object(DeepSeekDriver, "sync_workspace", return_value={"mcp_config_path": "x"}),
                mock.patch("opsb_core.drivers.dsh_driver.stage_brief_and_clipboard", return_value=(root / "brief.md", True)),
                mock.patch("opsb_core.drivers.dsh_driver._dsh_desktop_running", return_value=True),
                mock.patch("opsb_core.drivers.dsh_driver._quit_dsh_desktop", side_effect=lambda *a, **k: order.append("quit") or True),
                mock.patch.object(DeepSeekDriver, "preseed_dsh_conversation",
                                  side_effect=lambda *a, **k: order.append("seed") or {"ok": True, "session_id": "sid-1"}),
                mock.patch.object(DeepSeekDriver, "ensure_dsh_workspace",
                                  side_effect=lambda *a, **k: order.append("workspace") or {"ok": True, "workspace_id": "wid-1", "created": True}),
                mock.patch("opsb_core.drivers.dsh_driver.launch_macos_deep_link",
                           side_effect=lambda *a, **k: order.append("open")),
            ):
                rec = drv.launch_session(ws, "PadV3Grad", ws, "brief body", mode="gui")
            self.assertEqual(order, ["quit", "seed", "workspace", "open"])
            self.assertEqual(rec.native_session_id, "sid-1")
            self.assertEqual(rec.project_id, "wid-1")
            self.assertTrue(drv.last_launch_meta["dsh_workspace"]["restarted"])
            self.assertFalse(drv.last_launch_meta["dsh_workspace"]["needs_reload"])


    def test_gui_launch_skips_restart_for_known_workspace(self) -> None:
        order: list[str] = []

        class _Diag:
            gui_installed = True
            installed = True
            gui_app_path = "/Applications/DeepSeek Harness.app"
            detail = "ready"

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = root / "mat-chem-sim-pred"
            ws.mkdir()
            drv = DeepSeekDriver()
            with (
                mock.patch.dict(os.environ, {"PARHELION_HOME": str(root / "phome"), "DSH_HOME": str(root / "dsh-home")}, clear=False),
                mock.patch.object(DeepSeekDriver, "discover", return_value=_Diag()),
                mock.patch.object(DeepSeekDriver, "sync_workspace", return_value={"mcp_config_path": "x"}),
                mock.patch.object(DeepSeekDriver, "dsh_workspace_exists", return_value=True),
                mock.patch("opsb_core.drivers.dsh_driver.stage_brief_and_clipboard", return_value=(root / "brief.md", True)),
                mock.patch("opsb_core.drivers.dsh_driver._dsh_desktop_running", return_value=True),
                mock.patch("opsb_core.drivers.dsh_driver._quit_dsh_desktop", side_effect=lambda *a, **k: order.append("quit") or True),
                mock.patch.object(DeepSeekDriver, "preseed_dsh_conversation",
                                  side_effect=lambda *a, **k: order.append("seed") or {"ok": True, "session_id": "sid-2"}),
                mock.patch.object(DeepSeekDriver, "ensure_dsh_workspace",
                                  side_effect=lambda *a, **k: order.append("workspace") or {"ok": True, "workspace_id": "wid-2", "created": False, "session_linked": True}),
                mock.patch("opsb_core.drivers.dsh_driver.launch_macos_deep_link",
                           side_effect=lambda *a, **k: order.append("open")),
            ):
                rec = drv.launch_session(ws, "RevINNorm", ws, "brief body", mode="gui")
            self.assertEqual(order, ["seed", "workspace", "open"])
            meta = drv.last_launch_meta["dsh_workspace"]
            self.assertFalse(meta["restarted"])
            self.assertTrue(meta["needs_reload"])

    def test_preseed_reports_missing_cli_without_touching_store(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = self._seed_dsh_home(root)
            ws = root / "ws"
            ws.mkdir()
            with mock.patch.object(DeepSeekDriver, "_find_binary", return_value=""):
                res = DeepSeekDriver().preseed_dsh_conversation(ws, "brief", dsh_home=home)
            self.assertFalse(res["ok"])
            self.assertIn("dsh CLI not found", res["reason"])
            doc = json.loads((home / "storages" / "workspace.json").read_text(encoding="utf-8"))
            self.assertEqual(doc["tables"]["workspaces"], {})

    def test_gui_dry_run_keeps_dsh_store_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = self._seed_dsh_home(root)
            ws = root / "ops-bench"
            (ws / "results").mkdir(parents=True)
            (ws / "bench" / "PadV3Grad").mkdir(parents=True)
            (ws / "bench" / "problems.json").write_text(
                json.dumps([{"title": "PadV3Grad", "slug": "pad_v3_grad", "problem_id": "p1", "difficulty": 3, "path": "bench/PadV3Grad"}]),
                encoding="utf-8",
            )
            before = (home / "storages" / "workspace.json").read_text(encoding="utf-8")
            with mock.patch.dict(os.environ, {"PARHELION_HOME": str(root / "phome"), "DSH_HOME": str(home)}, clear=False):
                res = launch_problem_session(ws, "PadV3Grad", driver_kind="deepseek", mode="gui", dry_run=True)
            self.assertTrue(res["ok"])
            self.assertEqual(res["deep_link"], "dsh://open")
            self.assertFalse(res["dsh_seed"]["ok"])
            self.assertEqual((home / "storages" / "workspace.json").read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
