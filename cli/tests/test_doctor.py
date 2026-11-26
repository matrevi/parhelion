"""`opsb doctor clean`: test-pollution cleanup and session-home isolation."""
from __future__ import annotations

import argparse
import io
import json
import os
import sqlite3
import subprocess
import sys
import time
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


def _seed_agent_store(home: Path, data_dir: str, rows: list[tuple[str, str, int | None, int | None]]) -> None:
    """Create `workspaces/<data_dir>/state/agent_bus.db` with the real AOCI-free agent schema."""
    from opsb_core.agents import bus_store

    state = home / "workspaces" / data_dir / "state"
    state.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(state / "agent_bus.db")
    try:
        conn.executescript(bus_store._SCHEMA_SQL)
        for agent_id, status, pid, pgid in rows:
            conn.execute(
                "INSERT INTO agents (agent_id, workspace, driver, status, pid, pgid, created_at, "
                "updated_at, last_active_epoch) VALUES (?,?,?,?,?,?,?,?,?)",
                (agent_id, "/tmp/ws", "codex", status, pid, pgid, "t", "t", 0.0),
            )
        conn.commit()
    finally:
        conn.close()


def _sandbox(home: Path, data_dir: str, agent_id: str, payload: str = "x") -> Path:
    directory = home / "workspaces" / data_dir / "agents" / agent_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "codex_home").mkdir(exist_ok=True)
    (directory / "codex_home" / "log.txt").write_text(payload, encoding="utf-8")
    return directory


class DoctorAgentSandboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name).resolve()
        self.home = self.root / "home"
        self.addCleanup(self._td.cleanup)
        env = mock.patch.dict(os.environ, {"PARHELION_HOME": str(self.home), "OPSB_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        # A real data directory records its workspace; without meta.json the pre-existing
        # "no workspace metadata" rule would claim the whole directory and mask the agents scan.
        live = self.root / "live-ws"
        live.mkdir()
        _write_meta(self.home / "workspaces" / "ws-active-0000aaaa", str(live))
        _seed_agent_store(self.home, "ws-active-0000aaaa", [
            ("cod-finished", "stopped", None, None),
            ("cod-errored", "error", None, None),
            ("cod-running", "working", 4242, 4242),
        ])
        self.finished = _sandbox(self.home, "ws-active-0000aaaa", "cod-finished")
        self.errored = _sandbox(self.home, "ws-active-0000aaaa", "cod-errored")
        self.running = _sandbox(self.home, "ws-active-0000aaaa", "cod-running")
        self.orphan = _sandbox(self.home, "ws-active-0000aaaa", "cla-orphan")

    def test_dry_run_lists_without_moving(self) -> None:
        rc, report = _run(["doctor", "clean", "--agents", "--json"])
        self.assertEqual(rc, 0)
        self.assertFalse(report["applied"])
        names = {h["agent_id"]: h["reason"] for h in report["agents"]}
        self.assertEqual(names["cod-finished"], "agent stopped")
        self.assertEqual(names["cod-errored"], "agent error (resumable)")
        self.assertEqual(names["cla-orphan"], "no agent record")
        self.assertNotIn("cod-running", names)
        self.assertTrue(self.finished.is_dir())
        self.assertFalse(any(h.get("applied") for h in report["agents"]))

    def test_apply_moves_only_finished_and_orphan_sandboxes(self) -> None:
        rc, report = _run(["doctor", "clean", "--agents", "--apply", "--json"])
        self.assertEqual(rc, 0)
        moved = {h["agent_id"] for h in report["agents"] if h.get("applied")}
        self.assertEqual(moved, {"cod-finished", "cla-orphan"})
        self.assertFalse(self.finished.exists())
        self.assertTrue(self.errored.is_dir(), "an errored seat stays resumable by default")
        self.assertFalse(self.orphan.exists())
        self.assertTrue(self.running.is_dir(), "a live agent sandbox must survive")
        trash = Path(report["trash"])
        self.assertTrue((trash / "agents" / "ws-active-0000aaaa" / "cod-finished").is_dir())
        # The workspace data directory itself is never moved.
        self.assertTrue((self.home / "workspaces" / "ws-active-0000aaaa" / "state").is_dir())

    def test_include_errored_reclaims_errored_seats(self) -> None:
        rc, report = _run(["doctor", "clean", "--agents", "--include-errored", "--apply", "--json"])
        self.assertEqual(rc, 0)
        moved = {h["agent_id"] for h in report["agents"] if h.get("applied")}
        self.assertEqual(moved, {"cod-finished", "cod-errored", "cla-orphan"})
        self.assertTrue(self.running.is_dir())

    def test_unreadable_store_keeps_every_sandbox(self) -> None:
        (self.home / "workspaces" / "ws-active-0000aaaa" / "state" / "agent_bus.db").unlink()
        rc, report = _run(["doctor", "clean", "--agents", "--apply", "--json"])
        self.assertEqual(rc, 0)
        self.assertTrue(all(not h.get("applied") for h in report["agents"]))
        self.assertEqual({h["reason"] for h in report["agents"]}, {"agent store unreadable"})
        for directory in (self.finished, self.errored, self.running, self.orphan):
            self.assertTrue(directory.is_dir())


class DoctorLogRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name).resolve()
        self.home = self.root / "home"
        self.logs = self.home / "logs" / "sessions"
        self.logs.mkdir(parents=True)
        self.addCleanup(self._td.cleanup)
        env = mock.patch.dict(os.environ, {"PARHELION_HOME": str(self.home), "OPSB_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        now = time.time()
        for name, age_days in (("fresh.jsonl", 1), ("stale.jsonl", 30), ("ancient.jsonl", 90)):
            path = self.logs / name
            path.write_text("{}", encoding="utf-8")
            stamp = now - age_days * 86400
            os.utime(path, (stamp, stamp))

    def test_dry_run_removes_nothing(self) -> None:
        rc, report = _run(["doctor", "clean", "--logs", "14", "--json"])
        self.assertEqual(rc, 0)
        names = {h["name"] for h in report["logs"]}
        self.assertEqual(names, {"stale.jsonl", "ancient.jsonl"})
        self.assertEqual(len(list(self.logs.iterdir())), 3)

    def test_apply_prunes_only_expired_transcripts(self) -> None:
        rc, report = _run(["doctor", "clean", "--logs", "14", "--apply", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual({h["name"] for h in report["logs"] if h.get("applied")},
                         {"stale.jsonl", "ancient.jsonl"})
        self.assertEqual(sorted(p.name for p in self.logs.iterdir()), ["fresh.jsonl"])

    def test_flag_without_value_uses_default_window(self) -> None:
        args = _parse(["doctor", "clean", "--logs", "--json"])
        self.assertEqual(args.logs, doctor_cmd.DEFAULT_LOG_KEEP_DAYS)

    def test_size_cap_keeps_the_newest_logs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td).resolve() / "home"
            logs = home / "logs" / "sessions"
            logs.mkdir(parents=True)
            for index in range(4):
                path = logs / f"bulk-{index}.jsonl"
                path.write_bytes(b"x" * (400 * 1024))
                stamp = time.time() - index  # bulk-0 is the newest
                os.utime(path, (stamp, stamp))
            env = {"PARHELION_HOME": str(home), "OPSB_HOME": str(home)}
            with mock.patch.dict(os.environ, env):
                # Window is empty here: only the 1 MB budget can fire.
                rc, report = _run(["doctor", "clean", "--logs", "14", "--logs-max-mb", "1", "--json"])
            self.assertEqual(rc, 0)
            # Newest-first fill: 400K + 400K fits, everything older than that is reclaimed.
            self.assertEqual({h["name"] for h in report["logs"]},
                             {"bulk-2.jsonl", "bulk-3.jsonl"})
            self.assertEqual({h["reason"] for h in report["logs"]}, {"over 1 MB budget"})

    def test_zero_cap_disables_the_budget(self) -> None:
        for index in range(4):
            path = self.logs / f"bulk-{index}.jsonl"
            path.write_bytes(b"x" * (400 * 1024))
            stamp = time.time() - index
            os.utime(path, (stamp, stamp))
        rc, report = _run(["doctor", "clean", "--logs", "14", "--logs-max-mb", "0", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual({h["name"] for h in report["logs"]}, {"stale.jsonl", "ancient.jsonl"})


class DoctorTrashPurgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name).resolve()
        self.home = self.root / "home"
        self.trash = self.home / "trash"
        (self.trash / "20260101T000000Z" / "bus.db-1").mkdir(parents=True)
        (self.trash / "20260101T000000Z" / "bus.db-1" / "x").write_text("y", encoding="utf-8")
        self.addCleanup(self._td.cleanup)
        env = mock.patch.dict(os.environ, {"PARHELION_HOME": str(self.home), "OPSB_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)

    def test_dry_run_lists_batches(self) -> None:
        rc, report = _run(["doctor", "clean", "--purge-trash", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(report["trash_purge"]), 1)
        self.assertGreater(report["trash_purge"][0]["size_bytes"], 0)
        self.assertTrue((self.trash / "20260101T000000Z").is_dir())

    def test_apply_empties_trash(self) -> None:
        rc, report = _run(["doctor", "clean", "--purge-trash", "--apply", "--json"])
        self.assertEqual(rc, 0)
        self.assertTrue(report["trash_purge"][0]["applied"])
        self.assertTrue(self.trash.is_dir())
        self.assertEqual(list(self.trash.iterdir()), [])


class ConfigFilePermissionTests(unittest.TestCase):
    def test_save_keeps_config_private(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td).resolve()
            with mock.patch.dict(os.environ, {"PARHELION_HOME": str(home), "OPSB_HOME": str(home)}):
                config.ConfigRepository.save({"workspaces": [], "active_workspace": ""})
                mode = (home / "config.json").stat().st_mode & 0o777
            self.assertEqual(mode, 0o600)

    def test_migrated_config_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            legacy = root / ".config" / "parhelion"
            legacy.mkdir(parents=True)
            (legacy / "config.json").write_text("{}", encoding="utf-8")
            home = root / ".parhelion"
            env = {"PARHELION_HOME": str(home), "OPSB_HOME": str(home), "HOME": str(root)}
            with mock.patch.dict(os.environ, env):
                config.ensure_parhelion_migrated()
                mode = (home / "config.json").stat().st_mode & 0o777
            self.assertEqual(mode, 0o600)
