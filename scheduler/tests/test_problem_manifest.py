"""Problem-manifest precedence: clean `opsb.workspace.json` first, legacy problems.json fallback."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest

SUP_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(SUP_ROOT) not in sys.path:
    sys.path.insert(0, str(SUP_ROOT))

from scheduler.parhelion_paths import load_problem_manifest  # noqa: E402


def _load_runtime_module():
    spec = importlib.util.spec_from_file_location("npu_test_manifest", SUP_ROOT / "runtime" / "npu-test.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


class ManifestPrecedenceTests(unittest.TestCase):
    def _write_clean(self, ws: pathlib.Path, problems: list[dict]) -> None:
        (ws / "opsb.workspace.json").write_text(
            json.dumps({"layout": "clean-agents-first", "contest_id": "demo", "git_owner": "winbeau", "problems": problems}),
            encoding="utf-8",
        )

    def _write_legacy(self, ws: pathlib.Path, problems: list[dict]) -> None:
        (ws / "bench").mkdir(parents=True, exist_ok=True)
        (ws / "bench" / "problems.json").write_text(json.dumps(problems), encoding="utf-8")

    def test_clean_layout_wins_over_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = pathlib.Path(td)
            self._write_clean(ws, [{"title": "OpA", "slug": "opa", "repo_slug": "accel-mat-opa", "difficulty": 4}])
            self._write_legacy(ws, [{"title": "LegacyOp", "slug": "legacy"}])
            rows = load_problem_manifest(ws)
        self.assertEqual([r["title"] for r in rows], ["OpA"])
        self.assertEqual(rows[0]["path"], "OpA")
        self.assertEqual(rows[0]["repo_slug"], "accel-mat-opa")
        self.assertEqual(rows[0]["difficulty"], 4)

    def test_legacy_layout_keeps_legacy_paths_and_repo_naming(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = pathlib.Path(td)
            self._write_legacy(ws, [{"title": "ConcatD", "slug": "concatd"}])
            rows = load_problem_manifest(ws)
        self.assertEqual(rows[0]["path"], "bench/ConcatD")
        self.assertEqual(rows[0]["repo_slug"], "bench-concatd")

    def test_empty_manifest_returns_empty_list(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(load_problem_manifest(pathlib.Path(td)), [])

    def test_runtime_loader_matches_scheduler_loader(self) -> None:
        runtime = _load_runtime_module()
        with tempfile.TemporaryDirectory() as td:
            ws = pathlib.Path(td)
            self._write_clean(ws, [{"title": "OpA", "slug": "opa"}])
            self.assertEqual(
                [r["title"] for r in runtime.load_problem_manifest(ws)],
                [r["title"] for r in load_problem_manifest(ws)],
            )

    def test_embedded_remote_scripts_stay_compilable(self) -> None:
        """The ssh workers are embedded Python strings; a bad edit must never ship silently."""
        runtime = _load_runtime_module()
        for name in ("REMOTE_3PHASE", "REMOTE_TEMPLATE"):
            with self.subTest(script=name):
                compile(getattr(runtime, name), name, "exec")
        self.assertIn("MS_PROF", runtime.REMOTE_TEMPLATE)
        self.assertNotIn("_run_profiler", runtime.REMOTE_3PHASE)

    def test_remote_root_defaults_to_contest_id(self) -> None:
        runtime = _load_runtime_module()
        with tempfile.TemporaryDirectory() as td:
            ws = pathlib.Path(td)
            self.assertEqual(runtime._remote_root_for(ws, {}), f"/home/developer/work/{ws.name}")
            self.assertEqual(runtime._remote_root_for(ws, {"contest_id": "mat-chem-sim-pred"}),
                             "/home/developer/work/mat-chem-sim-pred")
            self.assertEqual(runtime._remote_root_for(ws, {"npu_remote_root": "/data/work/x/"}), "/data/work/x")


if __name__ == "__main__":
    unittest.main()
