"""Tests for problem template materialization and private-repo bootstrap (no network/git)."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core.services import problem_repo_service as svc  # noqa: E402


def _done(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TemplateMaterializeTests(unittest.TestCase):
    def test_writes_missing_files_and_keeps_user_edits(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "kernel.asc").write_text("USER EDIT\n", encoding="utf-8")
            res = svc.materialize_template(
                root,
                {"kernel.asc": "OFFICIAL\n", "main.asc": "MAIN\n", "AGENTS.md": "PROTECTED\n"},
            )
            self.assertEqual((root / "kernel.asc").read_text(encoding="utf-8"), "USER EDIT\n")
            self.assertEqual((root / "main.asc").read_text(encoding="utf-8"), "MAIN\n")
            self.assertFalse((root / "AGENTS.md").exists())
            self.assertIn("kernel.asc", res["skipped"])
            self.assertIn("AGENTS.md", res["skipped"])
            self.assertIn("main.asc", res["written"])
            self.assertTrue((root / ".gitignore").is_file())

    def test_overwrite_rewrites_but_still_skips_protected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "kernel.asc").write_text("OLD\n", encoding="utf-8")
            svc.materialize_template(root, {"kernel.asc": "NEW\n", "PROBLEM.md": "x"}, overwrite=True)
            self.assertEqual((root / "kernel.asc").read_text(encoding="utf-8"), "NEW\n")
            self.assertFalse((root / "PROBLEM.md").exists())

    def test_path_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "prob"
            root.mkdir()
            res = svc.materialize_template(root, {"../outside.txt": "nope"})
            self.assertFalse((Path(td) / "outside.txt").exists())
            self.assertIn("../outside.txt", res["skipped"])

    def test_fetch_rejects_empty_problem_id_and_parses_payload(self) -> None:
        with self.assertRaises(svc.ProblemBootstrapError):
            svc.fetch_problem_template("")

        payload = json.dumps({"code": 0, "data": {"files": [
            {"path": "kernel.asc", "content": "K"},
            {"path": "", "content": "ignored"},
        ], "tiling_h": "T"}}).encode()

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return payload

        with mock.patch.object(svc.urllib.request, "urlopen", return_value=_Resp()):
            files = svc.fetch_problem_template("pid-1")
        self.assertEqual(files["kernel.asc"], "K")
        self.assertEqual(files["tiling.h"], "T")


class PrivateRepoTests(unittest.TestCase):
    def test_create_repo_reports_created_and_existing(self) -> None:
        with mock.patch.object(svc, "_run", return_value=_done(0, stdout='{"full_name": "winbeau/ops-a"}')) as run:
            created = svc.ensure_private_repo("winbeau", "ops-a", description="d")
        self.assertTrue(created["created"])
        self.assertEqual(created["full_name"], "winbeau/ops-a")
        self.assertIn("--private", run.call_args[0][0])

        with mock.patch.object(svc, "_run", return_value=_done(1, stderr="Project with same name already exists")):
            existing = svc.ensure_private_repo("winbeau", "ops-a")
        self.assertTrue(existing["ok"])
        self.assertFalse(existing["created"])

    def test_create_repo_surfaces_failure_and_missing_cli(self) -> None:
        with mock.patch.object(svc, "_run", return_value=_done(1, stderr="HTTP 500 boom")):
            with self.assertRaises(svc.ProblemBootstrapError) as ctx:
                svc.ensure_private_repo("winbeau", "ops-a")
        self.assertIn("boom", str(ctx.exception))

        with mock.patch.object(svc, "_run", side_effect=FileNotFoundError):
            with self.assertRaises(svc.ProblemBootstrapError) as ctx:
                svc.ensure_private_repo("winbeau", "ops-a", gc_bin="gc-missing")
        self.assertIn("gitcode CLI not found", str(ctx.exception))

    def test_push_forces_only_for_a_freshly_created_repo(self) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _done(0, stdout="main")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(svc, "_run", side_effect=fake_run):
                svc.push_private_repo(root, "winbeau", "ops-a", created=True)
                svc.push_private_repo(root, "winbeau", "ops-a", created=False)
        push_cmds = [c for c in calls if c[:2] == ["git", "push"]]
        self.assertIn("-f", push_cmds[0])
        self.assertNotIn("-f", push_cmds[1])
        self.assertTrue(any(c[:2] == ["git", "remote"] for c in calls))

    def test_push_failure_is_reported_not_swallowed(self) -> None:
        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["git", "push"]:
                return _done(1, stderr="! [rejected] main -> main (fetch first)")
            if cmd[:3] == ["git", "remote", "get-url"]:
                return _done(0, stdout="https://gitcode.com/winbeau/ops-a.git")
            return _done(0)

        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(svc, "_run", side_effect=fake_run):
                with self.assertRaises(svc.ProblemBootstrapError) as ctx:
                    svc.push_private_repo(Path(td), "winbeau", "ops-a", created=False)
        self.assertIn("rejected", str(ctx.exception))


class BootstrapLoopTests(unittest.TestCase):
    def test_bootstrap_isolates_failures_per_problem(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in ("A", "B", "C"):
                (root / name).mkdir()
            problems = [
                {"title": "A", "path": "A", "repo_slug": "ops-a", "problem_id": "p1"},
                {"title": "B", "path": "B", "repo_slug": ""},
                {"title": "C", "path": "C", "repo_slug": "ops-c", "problem_id": "p3"},
            ]

            def fake_template(pid, **kwargs):
                if pid == "p1":
                    raise svc.ProblemBootstrapError("template down")
                return {"kernel.asc": "K"}

            with (
                mock.patch.object(svc, "fetch_problem_template", side_effect=fake_template),
                mock.patch.object(svc, "git_init_commit", return_value={"ok": True, "committed": False, "sha": "x"}),
                mock.patch.object(svc, "ensure_private_repo", return_value={"ok": True, "created": False}),
                mock.patch.object(svc, "push_private_repo", return_value={"ok": True}),
            ):
                res = svc.bootstrap_workspace_repos(root, problems, git_owner="winbeau")
        self.assertFalse(res["ok"])
        self.assertEqual(res["total"], 3)
        self.assertEqual(res["succeeded"], 1)          # only C completes
        self.assertEqual({f["title"] for f in res["failed"]}, {"A", "B"})


if __name__ == "__main__":
    unittest.main()
