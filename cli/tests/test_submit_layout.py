"""Submit plumbing for clean-agents-first layout: manifest preference and source matching."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

SUP_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _submit_module():
    spec = importlib.util.spec_from_file_location("submit_validated", SUP_ROOT / "runtime" / "submit-validated.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


class SubmitManifestTests(unittest.TestCase):
    def test_load_problems_prefers_clean_manifest(self) -> None:
        mod = _submit_module()
        with tempfile.TemporaryDirectory() as td:
            ws = pathlib.Path(td)
            (ws / "opsb.workspace.json").write_text(
                json.dumps({"problems": [{"title": "RevINNorm", "path": "RevINNorm", "problem_id": "pid-1"}]}),
                encoding="utf-8",
            )
            (ws / "bench").mkdir()
            (ws / "bench" / "problems.json").write_text(
                json.dumps([{"title": "LegacyOp", "path": "bench/LegacyOp"}]), encoding="utf-8"
            )
            rows = mod._load_problems(ws)
        self.assertEqual([r["title"] for r in rows], ["RevINNorm"])
        self.assertEqual(rows[0]["path"], "RevINNorm")

    def test_legacy_manifest_still_supported(self) -> None:
        mod = _submit_module()
        with tempfile.TemporaryDirectory() as td:
            ws = pathlib.Path(td)
            (ws / "bench").mkdir()
            (ws / "bench" / "problems.json").write_text(
                json.dumps([{"title": "ConcatD", "path": "bench/ConcatD"}]), encoding="utf-8"
            )
            rows = mod._load_problems(ws)
        self.assertEqual(rows[0]["title"], "ConcatD")

    def test_source_matches_requires_editable_files_and_identical_bytes(self) -> None:
        mod = _submit_module()
        with tempfile.TemporaryDirectory() as td:
            repo = pathlib.Path(td)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
            (repo / "kernel.asc").write_text("KERNEL\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
            subprocess.run(
                ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"], cwd=repo, check=True
            )
            sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
            ).stdout.strip()
            files = [{"path": "kernel.asc", "content": "KERNEL\n", "editable": True}]
            self.assertTrue(mod.source_matches(repo, sha, files))
            self.assertFalse(mod.source_matches(repo, sha, [{"path": "kernel.asc", "content": "OTHER\n", "editable": True}]))
            self.assertFalse(mod.source_matches(repo, sha, [{"path": "kernel.asc", "content": "KERNEL\n", "editable": False}]))


if __name__ == "__main__":
    unittest.main()
