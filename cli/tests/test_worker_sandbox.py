"""Worker sandbox tests (P15, 4.2 K): launch environment and read-path denials."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core import config as _cfg  # noqa: E402
from opsb_core import worker_sandbox  # noqa: E402

REPO_ROOT = Path(_cfg.CLI_DIR).resolve().parent


def _platform_pythonpath() -> str:
    return os.pathsep.join([str(REPO_ROOT), str(_cfg.CLI_DIR), "/opt/unrelated"])


# 1. process environment ------------------------------------------------------------------------

def test_worker_env_drops_platform_source_paths_and_private_variables():
    base = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/Users/someone",
        "PYTHONPATH": _platform_pythonpath(),
        "PARHELION_HOME": "/tmp/platform-home",
        "PARHELION_MCP_ROLE": "orchestrator",
        "OPSB_WORKSPACE": "/tmp/competition",
        "JUDGE_SESSION_FILE": "/Users/someone/.judge/session.json",
        "UNRELATED": "kept",
    }
    env = worker_sandbox.worker_env(base, {"PARHELION_MCP_ROLE": "worker", "PARHELION_AGENT_ID": "dee-1"})
    entries = env.get("PYTHONPATH", "").split(os.pathsep)
    assert entries == ["/opt/unrelated"], entries
    for source in (str(REPO_ROOT), str(_cfg.CLI_DIR), str(REPO_ROOT / "scheduler"), str(REPO_ROOT / "runtime")):
        assert source not in env.get("PYTHONPATH", "")
    assert env["PARHELION_HOME"] == "/tmp/platform-home"  # the one platform variable a worker keeps
    assert "OPSB_WORKSPACE" not in env
    assert "JUDGE_SESSION_FILE" not in env
    assert env["PARHELION_MCP_ROLE"] == "worker" and env["PARHELION_AGENT_ID"] == "dee-1"
    assert env["PATH"] == "/usr/bin:/bin" and env["UNRELATED"] == "kept"


def test_worker_env_drops_pythonpath_when_only_platform_entries_remain():
    env = worker_sandbox.worker_env({"PYTHONPATH": os.pathsep.join([str(REPO_ROOT), str(_cfg.CLI_DIR)])}, {})
    assert "PYTHONPATH" not in env


def test_adapter_launch_environment_has_no_platform_source_paths(monkeypatch):
    """The daemon's managed-adapter path is the one every external worker is launched from."""
    from opsb_core.agents.adapters import BaseWorkerAdapter

    captured: dict[str, object] = {}

    class _FakeProc:
        pid = 424242
        stderr = None

        def poll(self):
            return None

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env") or {}
        captured["cwd"] = kwargs.get("cwd")
        return _FakeProc()

    monkeypatch.setattr("opsb_core.agents.adapters.subprocess.Popen", _fake_popen)
    monkeypatch.setattr("opsb_core.agents.adapters.prepare_worker_command",
                        lambda cmd, env, ws, aid: captured.setdefault("prepared_env", dict(env)) and cmd or cmd)
    monkeypatch.setenv("PYTHONPATH", _platform_pythonpath())
    monkeypatch.setenv("PARHELION_SESSION_ID", "leaked-session")

    tmp = Path(os.environ["PARHELION_HOME"]) / "p15-adapter"
    tmp.mkdir(parents=True, exist_ok=True)
    adapter = BaseWorkerAdapter(tmp / "ws", tmp / "wt", "claude", "cla-abc123", tmp / "log.jsonl")
    adapter._launch(["/bin/echo", "hi"], {"PARHELION_SESSION_ID": "own-session"})

    env = captured["env"]
    assert "leaked-session" not in str(env.get("PARHELION_SESSION_ID"))
    assert env["PARHELION_SESSION_ID"] == "own-session"
    assert env["PARHELION_AGENT_ID"] == "cla-abc123"
    assert str(REPO_ROOT) not in str(env.get("PYTHONPATH") or "")
    assert captured["cwd"] == str((tmp / "wt").resolve())


def test_every_headless_runner_filters_its_child_environment(monkeypatch):
    """``worker_env`` is the single choke point: every driver's spawn must go through it."""
    from opsb_core.drivers import antigravity_acp, base, claude_driver, codex_appserver, pi_driver

    calls: list[str] = []

    def _record(base_env=None, overrides=None, **_kwargs):
        calls.append("yes")
        merged = dict(base_env or {})
        merged.update(overrides or {})
        return merged

    monkeypatch.setattr("opsb_core.worker_sandbox.worker_env", _record)
    monkeypatch.setattr("opsb_core.agents.adapters.worker_sandbox.worker_env", _record)
    tmp = Path(os.environ["PARHELION_HOME"]) / "p15-runners"
    tmp.mkdir(parents=True, exist_ok=True)
    log = tmp / "run.log"

    def _no_spawn(*_a, **_k):
        raise RuntimeError("stop before spawn")

    monkeypatch.setattr("opsb_core.drivers.base.subprocess.Popen", _no_spawn)
    monkeypatch.setattr("opsb_core.drivers.antigravity_acp.subprocess.Popen", _no_spawn)
    monkeypatch.setattr("opsb_core.drivers.claude_driver.subprocess.Popen", _no_spawn)
    monkeypatch.setattr("opsb_core.drivers.codex_appserver.subprocess.Popen", _no_spawn)
    monkeypatch.setattr("opsb_core.drivers.pi_driver.subprocess.Popen", _no_spawn)

    class _Driver(base.BaseDriver):
        def discover(self):  # pragma: no cover - the runner never asks
            raise NotImplementedError

        def sync_workspace(self, workspace, mcp_url=None):  # pragma: no cover
            raise NotImplementedError

        def build_launch_command(self, *args, **kwargs):  # pragma: no cover
            raise NotImplementedError

    with pytest.raises(RuntimeError):
        _Driver()._execute_headless_turn(
            tmp, tmp, "brief", "sid", "sid", None, ["/bin/echo", "x"], {"PARHELION_HOME": "/h"}, log)
    with pytest.raises(RuntimeError):
        antigravity_acp.run_acp_headless_turn(tmp, tmp, "brief", "sid", "sid", None,
                                              ["/bin/echo", "x"], {}, log)
    with pytest.raises(RuntimeError):
        codex_appserver.run_codex_headless_turn(tmp, tmp, "brief", "sid", "sid", None,
                                                ["/bin/echo", "x"], {}, log)
    with pytest.raises(RuntimeError):
        claude_driver.ClaudeDriver()._execute_headless_turn(
            tmp, tmp, "brief", "sid", "sid", None, ["/bin/echo", "x"], {}, log)
    with pytest.raises(RuntimeError):
        pi_driver.PiDriver()._execute_headless_turn(
            tmp, tmp, "brief", "sid", "sid", None, ["/bin/echo", "x"], {}, log)
    assert len(calls) == 5


# 2. readable paths -----------------------------------------------------------------------------

def test_seatbelt_profile_excludes_credential_stores(tmp_path):
    home = tmp_path / "user"
    ph = tmp_path / "platform-home"
    profile = "\n".join(worker_sandbox.seatbelt_profile(
        home=home, parhelion=ph, extra=[("subpath", str(home / ".ssh"))]))
    assert "(deny file-read* file-write* (subpath " + json.dumps(str(ph / "sessions")) + "))" in profile
    assert "(deny file-read* file-write* (subpath " + json.dumps(str(home / ".judge")) + "))" in profile
    assert "(deny file-read* file-write* (literal " + json.dumps(str(ph / "cj_accounts.json")) + "))" in profile
    assert "(deny file-read* file-write* (literal " + json.dumps(str(ph / "daemon.json")) + "))" in profile
    assert 'regex #"/\\.cache_remote_judge_[^/]*$"' in profile
    assert "(deny file-read* file-write* (subpath " + json.dumps(str(home / ".ssh")) + "))" in profile
    # workspace state and bench specs stay readable: the worker's MCP tools need them.
    assert str(ph / "workspaces") not in profile
    assert "bench-specs" not in profile


def test_seatbelt_profile_really_denies_the_read_on_this_machine(tmp_path):
    """The profile is executed, not just rendered: canonical paths matter (``/var`` is a symlink)."""
    import subprocess

    if sys.platform != "darwin" or not Path(worker_sandbox.SANDBOX_EXEC).exists():
        pytest.skip("sandbox-exec is macOS only")
    home, ph = tmp_path / "home", tmp_path / "ph"
    (ph / "sessions").mkdir(parents=True)
    (home / ".judge").mkdir(parents=True)
    (ph / "cj_accounts.json").write_text("{}", encoding="utf-8")
    (ph / "sessions" / "primary.json").write_text("{}", encoding="utf-8")
    (home / ".judge" / "session.json").write_text("{}", encoding="utf-8")
    readable = ph / "workspaces" / "ws-1" / "state" / "test_runs" / "x.json"
    readable.parent.mkdir(parents=True)
    readable.write_text("{}", encoding="utf-8")
    profile = "\n".join(worker_sandbox.seatbelt_profile(home=home, parhelion=ph))
    for target in (ph / "cj_accounts.json", ph / "sessions" / "primary.json", home / ".judge" / "session.json"):
        proc = subprocess.run([worker_sandbox.SANDBOX_EXEC, "-p", profile, "/bin/cat", str(target)],
                              capture_output=True, text=True)
        assert proc.returncode != 0 and not proc.stdout.strip(), target
    proc = subprocess.run([worker_sandbox.SANDBOX_EXEC, "-p", profile, "/bin/cat", str(readable)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_config_json_is_denied_only_when_it_carries_credentials(tmp_path):
    ph = tmp_path / "platform-home"
    ph.mkdir()
    plain = {"scheduler": {"daily_quota_limit": 50}, "worker_models": {"deepseek": "m"}}
    (ph / "config.json").write_text(json.dumps(plain), encoding="utf-8")
    assert str(ph / "config.json") not in worker_sandbox.read_deny_paths(home=tmp_path, parhelion=ph)["files"]
    leaky = {**plain, "judge": {"cookie": "x", "user_id": "1"}}
    (ph / "config.json").write_text(json.dumps(leaky), encoding="utf-8")
    assert str(ph / "config.json") in worker_sandbox.read_deny_paths(home=tmp_path, parhelion=ph)["files"]


def test_worker_access_binds_one_scoped_route_and_sandboxes_the_process(tmp_path, monkeypatch):
    from opsb_core import worker_access

    if sys.platform != "darwin":
        pytest.skip("managed worker isolation is macOS only")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PARHELION_HOME", str(home))
    monkeypatch.setattr(_cfg, "DAEMON_STATE_PATH", home / "daemon.json")
    (home / "daemon.json").write_text(json.dumps({"port": 1234, "token": "daemon-secret"}), encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()

    env: dict[str, str] = {"SSH_AUTH_SOCK": "/tmp/agent", "PATH": "/bin"}
    cmd = worker_access.prepare_worker_command(["/bin/echo", "hi"], env, ws, "dee-abc")
    assert cmd[0] == worker_sandbox.SANDBOX_EXEC and cmd[-2:] == ["/bin/echo", "hi"]
    profile = cmd[2]
    relay = json.loads(worker_access.relay_file(ws, "dee-abc").read_text())
    worker_access.prepare_worker_command(["/bin/echo"], {}, ws, "dee-other")
    relay_other = worker_access.relay_file(ws, "dee-other")
    assert relay["token"] == worker_access.worker_token("daemon-secret", ws, "dee-abc")
    assert relay["token"] != json.loads(relay_other.read_text())["token"]  # no seat shares a route
    assert relay["token"] != "daemon-secret"  # the daemon token never reaches a worker
    assert "SSH_AUTH_SOCK" not in env
    assert str(home / "cj_accounts.json") in profile and str(home / "sessions") in profile
    assert str(Path.home() / ".judge") in profile and "remote tcp" in profile


def test_worker_codex_config_renders_permission_excludes(tmp_path, monkeypatch):
    import tomllib

    from opsb_core.drivers.codex_appserver import render_codex_config_toml

    ph = tmp_path / "platform-home"
    (ph / "sessions").mkdir(parents=True)
    (ph / "cj_accounts.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("PARHELION_HOME", str(ph))
    ws = tmp_path / "ws"
    wt = ws / ".worktrees" / "sprint-solo"
    wt.mkdir(parents=True)
    doc = tomllib.loads(render_codex_config_toml(ws, role="worker", agent_id="cod-1", worktree=wt))
    profile = doc["permissions"]["parhelion_worker"]
    assert doc["default_permissions"] == "parhelion_worker"
    assert profile["extends"] == ":workspace"
    files = profile["filesystem"]
    assert files[str(ph / "cj_accounts.json")] == "deny"
    assert files[str(ph / "sessions")] == "deny"
    assert files[str(Path.home() / ".judge")] == "deny"
    assert files[str(wt)] == "write"
    assert "parhelion_agent_report" in doc["mcp_servers"]["parhelion"]["tools"]
