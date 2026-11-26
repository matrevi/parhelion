"""`opsb doctor clean`: test-pollution cleanup and session-home isolation."""
from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))
SUP_ROOT = CLI_DIR.parent
if str(SUP_ROOT) not in sys.path:
    sys.path.insert(0, str(SUP_ROOT))

from opsb_core import config, device_env  # noqa: E402
from opsb_core.commands import doctor_cmd  # noqa: E402
from opsb_core.commands.doctor_cmd import register_doctor_subcommands, remote_ticket_script, run_doctor  # noqa: E402


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="opsb")
    register_doctor_subcommands(parser.add_subparsers(dest="command"))
    return parser.parse_args(argv)


def _run(argv: list[str]) -> tuple[int, dict]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = run_doctor(_parse(argv))
    return rc, json.loads(buf.getvalue())


def _write_meta(directory: Path, workspace: str) -> None:
    directory.mkdir(parents=True)
    (directory / "meta.json").write_text(json.dumps({"workspace": workspace}), encoding="utf-8")


class DoctorLocalCleanTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name).resolve()
        self.home = self.root / "home"
        self.wsroot = self.home / "workspaces"
        self.wsroot.mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"PARHELION_HOME": str(self.home), "OPSB_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self._td.cleanup)

        live_ws = self.root / "live-ws"
        live_ws.mkdir()
        _write_meta(self.wsroot / "ws-0000aaaa", str(live_ws))  # normal: path exists, unregistered
        (self.wsroot / "bus.db-1234abcd" / "state").mkdir(parents=True)  # bus.db residue
        _write_meta(self.wsroot / "ws-1111bbbb", "/private/var/folders/x/T/pytest-of-u/pytest-1/t0/ws")
        # Registered workspace whose path would otherwise look like pytest pollution.
        registered = self.root / "pytest-of-u" / "kept-ws"
        registered.mkdir(parents=True)
        self.registered_dir = self.wsroot / config.workspace_id(registered)
        _write_meta(self.registered_dir, str(registered))
        (self.home / "config.json").write_text(
            json.dumps({"workspaces": [str(registered)], "active_workspace": str(registered), "npu_ssh_host": "dev-host"}),
            encoding="utf-8",
        )

    def test_dry_run_moves_nothing(self) -> None:
        before = sorted(p.name for p in self.wsroot.iterdir())
        rc, report = _run(["doctor", "clean", "--json"])
        self.assertEqual(rc, 0)
        self.assertFalse(report["applied"])
        self.assertEqual(sorted(h["name"] for h in report["local"]), ["bus.db-1234abcd", "ws-1111bbbb"])
        self.assertEqual(report["remote"], [])
        self.assertEqual(sorted(p.name for p in self.wsroot.iterdir()), before)
        self.assertFalse((self.home / "trash").exists())

    def test_apply_moves_only_polluted_dirs_to_trash(self) -> None:
        rc, report = _run(["doctor", "clean", "--apply", "--json"])
        self.assertEqual(rc, 0)
        self.assertTrue(report["applied"])
        remaining = sorted(p.name for p in self.wsroot.iterdir())
        self.assertEqual(remaining, sorted(["ws-0000aaaa", self.registered_dir.name]))
        stamps = list((self.home / "trash").iterdir())
        self.assertEqual(len(stamps), 1)
        self.assertEqual(sorted(p.name for p in stamps[0].iterdir()), ["bus.db-1234abcd", "ws-1111bbbb"])
        self.assertTrue((stamps[0] / "bus.db-1234abcd" / "state").is_dir())  # moved intact, not deleted
        self.assertTrue(all(h["applied"] for h in report["local"]))

    def test_missing_workspace_path_counts_only_for_ws_prefix(self) -> None:
        gone = str(self.root / "deleted-ws")
        _write_meta(self.wsroot / "ws-2222cccc", gone)
        _write_meta(self.wsroot / "sprint-x-3333dddd", gone)
        _, report = _run(["doctor", "clean", "--json"])
        names = {h["name"] for h in report["local"]}
        self.assertIn("ws-2222cccc", names)
        self.assertNotIn("sprint-x-3333dddd", names)

    def test_human_table_output(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(run_doctor(_parse(["doctor", "clean"])), 0)
        text = buf.getvalue()
        self.assertIn("dry-run", text)
        self.assertIn("bus.db-1234abcd", text)
        self.assertIn("would move", text)

    def test_remote_uses_batch_mode_and_dry_run_never_deletes(self) -> None:
        calls: list[tuple[list[str], str]] = []

        def fake_run(cmd, **kwargs):
            calls.append((list(cmd), kwargs.get("input") or ""))
            out = 'OPSB_DOCTOR [{"path": "/tmp/opsb-npu-tickets/4242.json", "pid": 4242, "applied": false}]\n'
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

        with mock.patch.object(doctor_cmd.subprocess, "run", side_effect=fake_run):
            rc, report = _run(["doctor", "clean", "--remote", "--json"])
            self.assertEqual(rc, 0)
            _run(["doctor", "clean", "--remote", "--apply", "--json"])
        dry_cmd, dry_script = calls[0]
        self.assertEqual(dry_cmd[0], "ssh")
        self.assertIn("BatchMode=yes", dry_cmd)
        self.assertIn("dev-host", dry_cmd)
        for token in ("unlink", "remove", "rmtree", "rm "):
            self.assertNotIn(token, dry_script)
        self.assertIn("os.unlink", calls[1][1])
        self.assertEqual(report["remote"][0]["pid"], 4242)
        self.assertEqual(report["remote"][0]["host"], "dev-host")

    def test_remote_failure_reports_error(self) -> None:
        failed = subprocess.CompletedProcess(["ssh"], 255, stdout="", stderr="Permission denied (publickey).\n")
        with mock.patch.object(doctor_cmd.subprocess, "run", return_value=failed):
            rc, report = _run(["doctor", "clean", "--remote", "--json"])
        self.assertEqual(rc, 1)
        self.assertIn("Permission denied", report["remote_error"])
        self.assertEqual(report["remote"], [])


class RemoteTicketScriptTests(unittest.TestCase):
    """Run the generated host script locally against a temporary ticket directory."""

    def _tickets(self, root: Path) -> int:
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        (root / f"{dead.pid}.json").write_text(json.dumps({"pid": dead.pid}), encoding="utf-8")
        (root / f"{os.getpid()}.json").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        (root / "broken.json").write_text("{not json", encoding="utf-8")
        (root / ".device-admission").write_text("", encoding="utf-8")
        return dead.pid

    def _exec(self, script: str) -> list[dict]:
        out = subprocess.run([sys.executable, "-"], input=script, capture_output=True, text=True, check=True).stdout
        line = next(ln for ln in out.splitlines() if ln.startswith("OPSB_DOCTOR "))
        return json.loads(line[len("OPSB_DOCTOR "):])

    def test_lists_then_deletes_only_dead_tickets(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            dead_pid = self._tickets(root)
            hits = self._exec(remote_ticket_script(False, str(root)))
            self.assertEqual([h["pid"] for h in hits], [dead_pid])
            self.assertTrue((root / f"{dead_pid}.json").exists())
            hits = self._exec(remote_ticket_script(True, str(root)))
            self.assertTrue(hits[0]["applied"])
            self.assertEqual(
                sorted(p.name for p in root.iterdir()),
                sorted([f"{os.getpid()}.json", "broken.json", ".device-admission"]),
            )


class SessionHomeIsolationTests(unittest.TestCase):
    def test_config_home_is_not_the_real_home(self) -> None:
        real = (Path.home() / ".parhelion").resolve()
        tmp_root = Path(tempfile.gettempdir()).resolve()
        self.assertNotEqual(config.PARHELION_HOME, real)
        self.assertTrue(config.PARHELION_HOME.is_relative_to(tmp_root), config.PARHELION_HOME)
        self.assertNotEqual(device_env.parhelion_home(), real)
        from scheduler.parhelion_paths import get_parhelion_home

        self.assertNotEqual(get_parhelion_home(), real)

    def test_conftest_session_home_is_a_temp_dir(self) -> None:
        if os.environ.get("PARHELION_TEST_KEEP_HOME"):
            self.skipTest("PARHELION_TEST_KEEP_HOME set")
        session = os.environ.get("PARHELION_TEST_SESSION_HOME", "")
        self.assertTrue(Path(session).name.startswith("parhelion-test-home-"))
        self.assertEqual(os.environ.get("PARHELION_HOME"), session)
        self.assertEqual(os.environ.get("OPSB_HOME"), session)


if __name__ == "__main__":
    unittest.main()


def test_clean_flags_metadata_less_and_empty_worktree_dirs(tmp_path):
    from opsb_core.commands import doctor_cmd

    root = tmp_path / "workspaces"
    (root / "tmpabc12345-deadbeef" / "agents").mkdir(parents=True)
    split = root / "sprint-solo-12345678"
    (split / "state").mkdir(parents=True)
    (split / "meta.json").write_text('{"workspace": "/c/Op/.worktrees/sprint-solo"}')
    kept = root / "sprint-dual-87654321"
    (kept / "state").mkdir(parents=True)
    (kept / "meta.json").write_text('{"workspace": "/c/Op/.worktrees/sprint-dual"}')
    (kept / "state" / "scheduler_state.json").write_text("{}")
    hits = {h["name"]: h["reason"] for h in doctor_cmd.scan_local(tmp_path, {"workspaces": []})}
    assert hits == {"tmpabc12345-deadbeef": "no workspace metadata",
                    "sprint-solo-12345678": "empty worktree state"}
