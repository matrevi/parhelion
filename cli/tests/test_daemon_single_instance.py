"""Single-instance tests for `opsb_core.daemon.server` (instance lock, slow-start wait)."""
from __future__ import annotations

import fcntl
import io
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

from opsb_core.daemon import server as daemon_server  # noqa: E402


class TestDaemonSingleInstance(unittest.TestCase):
    def test_second_daemon_exits_without_binding(self) -> None:
        """A held instance lock makes the newcomer exit before it binds or warms anything."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            instance_path = tmp / "daemon.instance.lock"
            bind_calls: list[tuple] = []
            with (
                mock.patch.object(daemon_server, "DAEMON_CONFIG_DIR", tmp),
                mock.patch.object(daemon_server, "DAEMON_INSTANCE_LOCK_PATH", instance_path),
                mock.patch.object(daemon_server, "DAEMON_STATE_PATH", tmp / "daemon.json"),
                mock.patch.object(daemon_server, "DAEMON_LOG_PATH", tmp / "daemon.log"),
                mock.patch.object(daemon_server, "ensure_parhelion_migrated", lambda: None),
                mock.patch.object(daemon_server, "_bind_server", side_effect=lambda *a, **k: bind_calls.append(a)),
                mock.patch("sys.stderr", new_callable=io.StringIO) as err,
            ):
                holder = instance_path.open("a")
                try:
                    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    rc = daemon_server.run_daemon_server(tmp / "ws", "npu", preferred_port=18910)
                finally:
                    holder.close()
            self.assertEqual(rc, 0)
            self.assertEqual(bind_calls, [])
            self.assertIn("another daemon owns this home", err.getvalue())

    def test_start_waits_for_alive_but_unhealthy_daemon(self) -> None:
        """A live pid without health is a daemon still starting, never a reason to spawn a twin."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            state_path = tmp / "daemon.json"
            state_path.write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "port": 18910,
                        "token": "test-token",
                        "started_at": "2026-11-27T00:00:00+00:00",
                        "workspace": str(tmp),
                        "ssh_host": "npu",
                        "version": 1,
                        "starting": True,
                    }
                ),
                encoding="utf-8",
            )
            health = {"ok": True, "pid": os.getpid(), "port": 18910, "stream_connected": True}
            probes: list[int] = []

            def _probe(_state=None, timeout: float = 1.2):
                probes.append(1)
                return None if len(probes) <= 2 else health

            with (
                mock.patch.object(daemon_server, "DAEMON_CONFIG_DIR", tmp),
                mock.patch.object(daemon_server, "DAEMON_STATE_PATH", state_path),
                mock.patch.object(daemon_server, "DAEMON_LOCK_PATH", tmp / "daemon.lock"),
                mock.patch.object(daemon_server, "DAEMON_INSTANCE_LOCK_PATH", tmp / "daemon.instance.lock"),
                mock.patch.object(daemon_server, "DAEMON_LOG_PATH", tmp / "daemon.log"),
                mock.patch.object(daemon_server, "ensure_parhelion_migrated", lambda: None),
                mock.patch.object(daemon_server, "probe_daemon_health", side_effect=_probe),
                mock.patch(
                    "opsb_core.daemon.server.subprocess.Popen",
                    side_effect=AssertionError("must not spawn a second daemon"),
                ),
            ):
                holder = (tmp / "daemon.instance.lock").open("a")  # the starting daemon holds it
                fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    res = daemon_server.start_daemon(workspace=tmp, ssh_host="npu", port=18910, background=True)
                finally:
                    holder.close()
            self.assertTrue(res["ok"])
            self.assertTrue(res["already_running"])
            self.assertEqual(res["pid"], os.getpid())
            self.assertEqual(res["port"], 18910)
            self.assertEqual(res["health"], health)
            self.assertEqual(len(probes), 3)


if __name__ == "__main__":
    unittest.main()
