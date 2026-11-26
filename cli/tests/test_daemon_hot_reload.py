"""The daemon reloads stateless scheduling modules in place; a broken edit keeps the old code."""
from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import textwrap
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core.daemon import hot_reload  # noqa: E402
from opsb_core.daemon.hot_reload import ModuleReloader  # noqa: E402
from opsb_core.daemon.server import (  # noqa: E402
    OpsbDaemonRuntime, _ThreadingHTTPServer, _make_handler, request_daemon_reload,
)

LOGIC_V1 = """
def decide():
    return "v1"

def helper():
    return 1
"""


class ModuleReloaderTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.pkg = Path(tmp.name) / "hotpkg_p09"
        self.pkg.mkdir()
        (self.pkg / "__init__.py").write_text("")
        self.logic = self.pkg / "logic.py"
        self.logic.write_text(LOGIC_V1)
        (self.pkg / "user.py").write_text("from .logic import decide\n\ndef use():\n    return decide()\n")
        sys.path.insert(0, tmp.name)
        self.addCleanup(sys.path.remove, tmp.name)
        self.addCleanup(self._forget)
        import hotpkg_p09.logic  # noqa: F401
        import hotpkg_p09.user  # noqa: F401
        self.mod = sys.modules["hotpkg_p09.logic"]
        self.user = sys.modules["hotpkg_p09.user"]
        self.reloader = ModuleReloader(["hotpkg_p09.logic"])

    @staticmethod
    def _forget() -> None:
        for name in [n for n in sys.modules if n == "hotpkg_p09" or n.startswith("hotpkg_p09.")]:
            del sys.modules[name]

    def _edit(self, source: str) -> None:
        self.logic.write_text(textwrap.dedent(source))
        later = time.time() + 5  # a distinct mtime even on coarse clocks
        os.utime(self.logic, (later, later))

    def test_unchanged_source_is_not_reloaded(self) -> None:
        self.assertIsNone(self.reloader.poll())

    def test_edit_takes_effect_for_module_and_imported_names(self) -> None:
        self._edit('def decide():\n    return "v2 changed"\n\ndef helper():\n    return 2\n')
        result = self.reloader.poll()
        self.assertTrue(result["ok"])
        self.assertEqual(self.mod.decide(), "v2 changed")
        self.assertEqual(self.user.decide(), "v2 changed")  # `from .logic import decide` re-pointed
        self.assertEqual(self.user.use(), "v2 changed")
        self.assertGreaterEqual(result["modules"][0]["rebound"], 1)
        self.assertIsNone(self.reloader.poll())

    def test_syntax_error_keeps_the_old_module_and_waits_for_the_next_edit(self) -> None:
        self._edit("def decide(:\n    return 'broken'\n")
        result = self.reloader.poll()
        self.assertFalse(result["ok"])
        self.assertIn("SyntaxError", result["modules"][0]["error"])
        self.assertEqual(self.mod.decide(), "v1")
        self.assertIn("hotpkg_p09.logic", self.reloader.status()["errors"])
        self.assertIsNone(self.reloader.poll())  # no retry storm on an unchanged broken file
        self._edit('def decide():\n    return "fixed"\n')
        self.assertTrue(self.reloader.poll()["ok"])
        self.assertEqual(self.mod.decide(), "fixed")
        self.assertEqual(self.reloader.status()["errors"], {})

    def test_error_while_executing_restores_the_previous_namespace(self) -> None:
        self._edit('def decide():\n    return "half"\n\nNEW_NAME = 1\nraise RuntimeError("boom at import")\n')
        result = self.reloader.poll()
        self.assertFalse(result["ok"])
        self.assertIn("boom at import", result["modules"][0]["error"])
        self.assertEqual(self.mod.decide(), "v1")
        self.assertEqual(self.user.use(), "v1")
        self.assertFalse(hasattr(self.mod, "NEW_NAME"))
        self.assertIs(sys.modules["hotpkg_p09.logic"], self.mod)

    def test_stateful_modules_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            ModuleReloader(["pkg.agents.manager"], protected=["pkg.agents.manager"])

    def test_runtime_reload_runs_between_ticks_and_refreshes_state(self) -> None:
        fake = types.SimpleNamespace(
            _tick_lock=threading.Lock(), reloader=self.reloader, state_cache=mock.Mock(),
            notify_subscribers=mock.Mock(), resolve_ws=lambda ws: Path(ws or "/tmp"),
        )
        self.assertEqual(OpsbDaemonRuntime.reload_modules(fake, changed_only=True), {"ok": True, "modules": []})
        fake.state_cache.invalidate.assert_not_called()
        self._edit('def decide():\n    return "tick"\n')
        with fake._tick_lock:  # a dispatch tick in progress holds the reload back
            worker = threading.Thread(target=OpsbDaemonRuntime.reload_modules, args=(fake,),
                                      kwargs={"changed_only": True})
            worker.start()
            time.sleep(0.1)
            self.assertEqual(self.mod.decide(), "v1")
        worker.join(5)
        self.assertEqual(self.mod.decide(), "tick")
        fake.state_cache.invalidate.assert_called_once()
        fake.notify_subscribers.assert_called_once()


class DaemonWhitelistTests(unittest.TestCase):
    def test_daemon_reloads_only_stateless_scheduling_modules(self) -> None:
        reloader = hot_reload.daemon_reloader()
        package = hot_reload._PACKAGE
        self.assertEqual(reloader.names, (f"{package}.ipc_bridge", f"{package}.daemon.dispatch"))
        stateful = {f"{package}.{name}" for name in hot_reload.STATEFUL_MODULES}
        self.assertFalse(stateful & set(reloader.names))
        self.assertIsNone(reloader.poll())


class DispatchPreflightTests(unittest.TestCase):
    def test_upload_waits_for_and_obeys_the_judge_compile_check(self) -> None:
        from opsb_core.daemon import dispatch
        row = {"kind": "pending", "gates": {"all_pass": True}, "title": "Op", "sha": "a" * 40}
        for action, uploads in (("wait", 0), ("hold", 0), ("upload", 1)):
            runtime, launch, backoff = mock.Mock(), mock.Mock(return_value={"ok": True}), {}
            dispatch.dispatch_next(runtime, Path("/tmp/ws"), {"submission_queue": [row]}, backoff,
                                   bridge=mock.Mock(), launch=launch, cli=Path("opsb"),
                                   preflight=lambda *_args, _a=action: {"action": _a})
            self.assertEqual(launch.call_count, uploads, action)
            self.assertTrue(backoff, action)
            self.assertEqual(runtime.state_cache.invalidate.called, action != "wait", action)

    def test_unconfigured_preflight_needs_no_subprocess(self) -> None:
        from opsb_core.daemon import dispatch
        with mock.patch.object(dispatch.subprocess, "run", side_effect=AssertionError("spawned")):
            self.assertEqual(dispatch.judge_preflight(Path("/tmp/ws"), "Op", "a" * 40)["action"], "upload")


class ReloadRouteTests(unittest.TestCase):
    def test_route_needs_the_admin_token_and_reports_modules(self) -> None:
        calls = []
        runtime = types.SimpleNamespace(
            token="admin-token", resolve_ws=lambda ws: Path(tempfile.gettempdir()),
            reload_modules=lambda: calls.append(1) or {"ok": True, "modules": [
                {"module": "opsb_core.ipc_bridge", "ok": True, "rebound": 2}]},
            reloader=types.SimpleNamespace(status=lambda: {"errors": {}}),
        )
        server = _ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(runtime, threading.Event()))
        port = int(server.server_address[1])
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", "/v1/daemon/reload", body=b"{}", headers={"X-Opsb-Token": "worker-token"})
            self.assertEqual(conn.getresponse().status, 401)
            conn.close()
            self.assertEqual(calls, [])
            reply = request_daemon_reload({"pid": os.getpid(), "port": port, "token": "admin-token"})
            self.assertTrue(reply["ok"])
            self.assertEqual(reply["modules"][0]["module"], "opsb_core.ipc_bridge")
            self.assertEqual(calls, [1])
        finally:
            server.shutdown()
            server.server_close()

    def test_client_reports_a_missing_daemon(self) -> None:
        self.assertFalse(request_daemon_reload({"pid": 0, "port": 1, "token": "x"})["ok"])


if __name__ == "__main__":
    unittest.main()
