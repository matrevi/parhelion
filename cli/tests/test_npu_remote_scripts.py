"""Remote NPU ticket scripts must survive ssh's argument joining.

ssh concatenates the remote command words into one shell command line, so a multi-line
``python3 -c <script>`` is re-split by the remote shell and python only sees ``import``.
These tests replay that joining through a local ``sh -c`` and run the real scripts.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

from opsb_core.services import npu_service

_REAL_RUN = subprocess.run


def _fake_ssh(calls: list[list[str]]):
    def run(argv, **kw):
        assert argv[0] == "ssh"
        calls.append(list(argv))
        remote = list(argv[len(npu_service.SSH_CONTROL_OPTS) + 2:])
        if remote and remote[0] == "python3":
            remote[0] = sys.executable
        # what sshd does: one command line handed to the login shell
        return _REAL_RUN(["sh", "-c", " ".join(remote)], input=kw.get("input"),
                         capture_output=True, text=True, timeout=kw.get("timeout", 6.0))
    return run


def test_cancel_kills_the_task_through_a_joined_command_line(monkeypatch):
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        calls: list[list[str]] = []
        monkeypatch.setattr(npu_service.subprocess, "run", _fake_ssh(calls))
        res = npu_service.cancel_npu_task("npu-test", proc.pid)
        assert res == {"ok": True, "pid": proc.pid}, res
        deadline = time.monotonic() + 5
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert proc.returncode == -signal.SIGTERM
        assert "-c" not in calls[0]
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)


def test_boost_script_is_sent_on_stdin_and_compiles(monkeypatch):
    seen: dict = {}

    def run(argv, **kw):
        seen["argv"] = argv
        seen["input"] = kw.get("input")
        return subprocess.CompletedProcess(argv, 0, '{"ok": true}', "")

    monkeypatch.setattr(npu_service.subprocess, "run", run)
    res = npu_service.boost_npu_ticket("npu-test", 4242, "Prob", "abc1234")
    assert res["ok"] is True
    assert seen["argv"][-2:] == ["python3", "-"]
    compile(seen["input"], "<boost>", "exec")
    assert "4242" in seen["input"]
