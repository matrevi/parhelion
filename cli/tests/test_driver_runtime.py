"""Tests for Parhelion Unified Single-Driver Runtime (`opsb_core.drivers` and `opsb driver`)."""
from __future__ import annotations

import argparse
import io
import json
import os
import plistlib
import sqlite3
import subprocess
import sys
import tomllib
import urllib.parse
import uuid
from pathlib import Path
from unittest import mock

import pytest

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core.commands.driver_mcp_cmd import register_driver_and_mcp_subcommands  # noqa: E402
from opsb_core.drivers import (  # noqa: E402
    AcpSessionClient,
    AntigravityDriver,
    ClaudeStreamJsonClient,
    CodexAppServerClient,
    CodexDriver,
    PiRpcClient,
    VALID_DRIVERS,
    compact_prompt_for_url,
    compose_session_brief,
    doctor_drivers,
    get_active_driver_kind,
    get_driver,
    inspect_macos_app_bundle,
    is_flock_held,
    launch_problem_session,
    list_all_sessions,
    list_sessions,
    open_sqlite_readonly_wal,
    prepare_session_worktree,
    select_active_driver,
    sessions_file_path,
)


def _make_git_workspace(tmp_path: Path) -> tuple[Path, Path]:
    ws = tmp_path / "contest_ws"
    op_dir = ws / "bench" / "DemoOp"
    (op_dir / "op_host").mkdir(parents=True)
    (op_dir / "op_kernel").mkdir(parents=True)
    (op_dir / "op_kernel" / "demo.cpp").write_text("// kernel\n", encoding="utf-8")
    (ws / "results" / "scoring-evidence").mkdir(parents=True)
    (ws / "bench" / "problems.json").write_text(
        json.dumps([{"title": "DemoOp", "slug": "demo_op", "problem_id": "p1", "difficulty": 3, "path": "bench/DemoOp"}]),
        encoding="utf-8",
    )
    (ws / "results" / "score-snapshot.json").write_text(
        json.dumps({"DemoOp": {"score": 68.5, "status": "Pass", "rank": 4, "public_times_us": [2.0, 2.0, 2.0, 2.0, 2.0]}}),
        encoding="utf-8",
    )
    (ws / "results" / "submit-queue.jsonl").write_text("", encoding="utf-8")
    (ws / "results" / "submit-queue-outcome.jsonl").write_text("", encoding="utf-8")

    subprocess.run(["git", "-C", str(op_dir), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(op_dir), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(op_dir), "config", "user.name", "Test User"], check=True)
    subprocess.run(["git", "-C", str(op_dir), "add", "."], check=True)
    subprocess.run(["git", "-C", str(op_dir), "commit", "-q", "-m", "initial demo op"], check=True)
    return ws, op_dir


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    p_home = tmp_path / "parhelion_home"
    codex_home = fake_home / ".codex"
    gemini_home = fake_home / ".gemini"
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("PARHELION_HOME", str(p_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("GEMINI_HOME", str(gemini_home))
    return p_home


def test_discover_all_six_drivers_mocked(isolated_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    bins = {
        "agy": fake_bin / "agy",
        "codex": fake_bin / "codex",
        "claude": fake_bin / "claude",
        "pi": fake_bin / "pi",
        "gemini": fake_bin / "gemini",
        "dsh": fake_bin / "dsh",
    }
    for b in bins.values():
        b.write_text("#!/bin/sh\necho 1.2.3\n", encoding="utf-8")
        b.chmod(0o755)

    monkeypatch.setenv("PATH", str(fake_bin))
    monkeypatch.setenv("ANTIGRAVITY_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("PI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    report = doctor_drivers()
    assert len(report["diagnostics"]) == 6
    seen = {d["kind"]: d for d in report["diagnostics"]}
    assert set(seen.keys()) == set(VALID_DRIVERS)
    for kind, diag in seen.items():
        assert diag["installed"] is True, kind
        assert diag["auth_ok"] is True, kind
        assert diag["version"] == "1.2.3", (kind, diag["binary_path"])
        assert str(fake_bin) in diag["binary_path"]

    # Missing binary & missing GUI bundle cleanly reports installed=False without throwing
    empty_bin = tmp_path / "empty_bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    with (
        mock.patch("opsb_core.drivers.base.shutil.which", return_value=None),
        mock.patch("opsb_core.drivers.base.Path.is_file", return_value=False),
        mock.patch("opsb_core.drivers.antigravity_driver.which_binary", return_value=""),
        mock.patch("opsb_core.drivers.codex_driver.which_binary", return_value=""),
    ):
        codex_diag = get_driver("codex").discover()
        assert codex_diag.installed is False
        assert codex_diag.gui_installed is False
        assert codex_diag.auth_ok is False
        assert "not found" in codex_diag.detail


def test_gui_bundle_discovery_and_bundled_codex_fallback(isolated_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    apps = tmp_path / "Apps"
    agy_app = apps / "Antigravity.app"
    (agy_app / "Contents").mkdir(parents=True)
    with (agy_app / "Contents" / "Info.plist").open("wb") as fh:
        plistlib.dump(
            {
                "CFBundleIdentifier": "com.google.antigravity",
                "CFBundleShortVersionString": "2.34.0",
                "CFBundleURLTypes": [{"CFBundleURLSchemes": ["antigravity"]}],
            },
            fh,
        )

    cx_app = apps / "ChatGPT.app"
    (cx_app / "Contents" / "Resources" / "codex-cli" / "bin").mkdir(parents=True)
    with (cx_app / "Contents" / "Info.plist").open("wb") as fh:
        plistlib.dump(
            {
                "CFBundleIdentifier": "com.openai.codex",
                "CFBundleShortVersionString": "26.1117.41037",
                "CFBundleURLTypes": [{"CFBundleURLSchemes": ["codex", "http"]}],
            },
            fh,
        )
    bundled_codex = cx_app / "Contents" / "Resources" / "codex-cli" / "bin" / "codex"
    bundled_codex.write_text("#!/bin/sh\necho codex-cli 0.171.0\n", encoding="utf-8")
    bundled_codex.chmod(0o755)

    # Mismatched bundle id must be rejected
    wrong_app = apps / "Wrong.app"
    (wrong_app / "Contents").mkdir(parents=True)
    with (wrong_app / "Contents" / "Info.plist").open("wb") as fh:
        plistlib.dump({"CFBundleIdentifier": "com.example.other"}, fh)
    assert inspect_macos_app_bundle([wrong_app], {"com.openai.codex"})["gui_installed"] is False

    empty_bin = tmp_path / "empty_bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))

    ag = AntigravityDriver()
    cx = CodexDriver()
    with (
        mock.patch.object(ag, "_gui_candidates", return_value=[agy_app]),
        mock.patch("opsb_core.drivers.antigravity_driver.which_binary", return_value=""),
        mock.patch.object(cx, "_gui_candidates", return_value=[cx_app]),
        mock.patch("opsb_core.drivers.codex_driver.which_binary", return_value=""),
    ):
        ag_diag = ag.discover()
        assert ag_diag.installed is True
        assert ag_diag.gui_installed is True
        assert ag_diag.gui_bundle_id == "com.google.antigravity"
        assert ag_diag.gui_version == "2.34.0"
        assert ag_diag.url_scheme == "antigravity"

        cx_diag = cx.discover()
        assert cx_diag.installed is True
        assert cx_diag.gui_installed is True
        assert cx_diag.gui_bundle_id == "com.openai.codex"
        assert cx_diag.gui_version == "26.1117.41037"
        assert cx_diag.url_scheme == "codex"
        assert cx_diag.binary_path == str(bundled_codex)
        assert "0.171.0" in cx_diag.version


def test_sync_workspace_codex_antigravity_and_claude_symlink(isolated_env: Path, tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    real_ws = Path(os.path.realpath(ws))

    # 1. Codex sync writes `<ws>/.codex/config.toml`, trusts `realpath(ws)` in isolated CODEX_HOME without clobbering existing TOML
    user_codex = Path(os.environ["CODEX_HOME"])
    user_codex.mkdir(parents=True, exist_ok=True)
    global_toml = user_codex / "config.toml"
    global_toml.write_text('# existing user codex config\nmodel = "gpt-5"\n', encoding="utf-8")

    codex_res = CodexDriver().sync_workspace(ws)
    assert codex_res["ok"] is True
    assert codex_res["trust_added"] is True
    ws_toml = Path(codex_res["workspace_codex_toml"])
    assert ws_toml == real_ws / ".codex" / "config.toml"
    ws_parsed = tomllib.loads(ws_toml.read_text(encoding="utf-8"))
    assert "parhelion" in ws_parsed["mcp_servers"]

    trusted_parsed = tomllib.loads(global_toml.read_text(encoding="utf-8"))
    assert trusted_parsed["model"] == "gpt-5"
    assert trusted_parsed["projects"][str(real_ws)]["trust_level"] == "trusted"

    # Idempotent second Codex sync leaves config.toml untouched
    codex_res2 = CodexDriver().sync_workspace(ws)
    assert codex_res2["trust_added"] is False

    # Isolated headless CODEX_HOME config also exists under PARHELION_HOME
    iso_cfg = Path(codex_res["codex_config_toml"])
    assert iso_cfg.is_file()
    assert str(isolated_env) in str(iso_cfg)

    # 2. Antigravity sync registers project UUID idempotently, writes `.agents/plugins/parhelion/`, handles 0-byte mcp_config.json
    g_home = Path(os.environ["GEMINI_HOME"])
    (g_home / "config").mkdir(parents=True, exist_ok=True)
    (g_home / "config" / "mcp_config.json").write_text("", encoding="utf-8")  # 0-byte empty file!

    ag_res1 = AntigravityDriver().sync_workspace(ws, register_global_mcp=True)
    assert ag_res1["created_new_project"] is True
    proj_id = ag_res1["project_id"]
    assert Path(ag_res1["project_file"]).is_file()
    assert (real_ws / ".agents" / "plugins" / "parhelion" / "plugin.json").is_file()
    assert (real_ws / ".agents" / "plugins" / "parhelion" / "mcp_config.json").is_file()
    global_mcp = json.loads((g_home / "config" / "mcp_config.json").read_text(encoding="utf-8"))
    assert "parhelion" in global_mcp["mcpServers"]

    ag_res2 = AntigravityDriver().sync_workspace(ws)
    assert ag_res2["created_new_project"] is False
    assert ag_res2["project_id"] == proj_id

    # 3. Claude workspace sync creates `.claude/skills -> ../.agents/skills` inside workspace only
    claude_res = get_driver("claude").sync_workspace(ws)
    assert claude_res["ok"] is True
    link_p = ws / ".claude" / "skills"
    assert link_p.is_symlink()
    assert link_p.readlink() == Path("..") / ".agents" / "skills"
    assert (ws / ".agents" / "skills").is_dir()
    claude_res2 = get_driver("claude").sync_workspace(ws)
    assert claude_res2["symlink_status"] == "preserved"


def test_gui_deep_links_url_compaction_and_dry_run_launch(isolated_env: Path, tmp_path: Path) -> None:
    ws, _ = _make_git_workspace(tmp_path)
    cx = CodexDriver()
    ag = AntigravityDriver()

    # URL encoding + new-thread vs existing-thread Codex deep links
    u_new = cx.build_gui_deep_link(ws, prompt="hello world & 中文", project_id="proj-1")
    assert u_new.startswith("codex://threads/new?")
    assert "mode=codex" in u_new
    assert "projectId=proj-1" in u_new
    assert "hello%20world" in u_new

    u_old = cx.build_gui_deep_link(ws, prompt="continue", thread_id="thr-abc")
    assert u_old == "codex://threads/thr-abc?prompt=continue"

    # Compaction fallback above 6000 chars
    brief_p = ws / ".parhelion" / "brief.md"
    huge = "x" * 7000
    compacted = compact_prompt_for_url(huge, brief_p, base_url_len=120, max_url_len=6000)
    assert str(brief_p) in compacted
    assert len(compacted) < 200

    # Dry-run GUI launch for Codex & Antigravity skips `open` and `pbcopy`, stages `.parhelion/brief.md`, saves SessionRecord
    with mock.patch("opsb_core.drivers.base.subprocess.run") as mock_sub:
        cx_launch = launch_problem_session(ws, "DemoOp", driver_kind="codex", mode="gui", dry_run=True, track_name="gui-cx")
        ag_launch = launch_problem_session(ws, "DemoOp", driver_kind="antigravity", mode="gui", dry_run=True, track_name="gui-ag")
        for call in mock_sub.call_args_list:
            assert call.args[0][0] not in ("open", "pbcopy")

    assert cx_launch["deep_link"].startswith("codex://threads/new?")
    assert Path(cx_launch["brief_path"]).is_file()
    assert cx_launch["clipboard_copied"] is False
    assert cx_launch["session"]["launch_mode"] == "gui"
    assert cx_launch["session"]["status"] == "idle"

    assert ag_launch["deep_link"].startswith("antigravity://open?")
    assert ag_launch["project_id"]
    assert Path(ag_launch["brief_path"]).is_file()
    assert ag_launch["session"]["deep_link"] == ag_launch["deep_link"]


def test_observe_gui_sessions_wal_and_flock(isolated_env: Path, tmp_path: Path) -> None:
    ws, op_dir = _make_git_workspace(tmp_path)
    real_op = Path(os.path.realpath(op_dir))
    real_ws = Path(os.path.realpath(ws))

    # 1. Antigravity WAL-mode conversation_summaries.db + live flock presence probe
    g_home = Path(os.environ["GEMINI_HOME"])
    ag_dir = g_home / "antigravity"
    (ag_dir / "presence").mkdir(parents=True, exist_ok=True)
    brain_logs = ag_dir / "brain" / "conv-live-1" / ".system_generated" / "logs"
    brain_logs.mkdir(parents=True, exist_ok=True)
    (brain_logs / "transcript.jsonl").write_text(
        json.dumps({"step_index": 7, "tool_calls": [{"name": "run_command"}]}) + "\n",
        encoding="utf-8",
    )

    proj_dir = g_home / "config" / "projects"
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / "proj-uuid-1.json").write_text(
        json.dumps({"id": "proj-uuid-1", "name": "contest_ws", "projectResources": {"resources": [{"folderUri": real_ws.as_uri()}]}}),
        encoding="utf-8",
    )

    ag_db = ag_dir / "conversation_summaries.db"
    writer = sqlite3.connect(str(ag_db))
    writer.execute("PRAGMA journal_mode=WAL;")
    writer.execute(
        """
        CREATE TABLE conversation_summaries (
            conversation_id TEXT PRIMARY KEY,
            title TEXT,
            preview TEXT,
            step_count INTEGER,
            status TEXT,
            project_id TEXT,
            workspace_uris TEXT,
            not_fully_idle INTEGER,
            killed INTEGER,
            last_modified_time TEXT,
            last_user_input_time TEXT,
            parent_conversation_id TEXT,
            agent_name TEXT
        )
        """
    )
    writer.commit()
    # Insert uncheckpointed WAL row while keeping writer open
    writer.execute(
        """
        INSERT INTO conversation_summaries VALUES (
            'conv-live-1', 'Tune DemoOp', 'Optimizing MTE2', 12,
            'CASCADE_RUN_STATUS_IDLE', 'proj-uuid-1', ?, 1, 0,
            '2026-11-23T01:20:00Z', '2026-11-23T01:15:00Z', '', 'antigravity'
        )
        """,
        (json.dumps([real_op.as_uri()]),),
    )
    writer.commit()

    lock_file = ag_dir / "presence" / "conv-live-1.lock"
    lock_file.write_text("", encoding="utf-8")
    # Hold exclusive flock in a child process to verify cross-process `is_flock_held`
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, sys, time; fh=open(sys.argv[1],'r'); fcntl.flock(fh, fcntl.LOCK_EX); sys.stdout.write('LOCKED\\n'); sys.stdout.flush(); time.sleep(5)",
            str(lock_file),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "LOCKED"
        assert is_flock_held(lock_file) is True
        ag_obs = AntigravityDriver().observe_gui_sessions(workspace=ws)
        assert len(ag_obs) == 1
        assert ag_obs[0]["native_session_id"] == "conv-live-1"
        assert ag_obs[0]["status"] == "running"
        assert ag_obs[0]["presence_lock_held"] is True
        assert ag_obs[0]["observed_steps"] == 12
        assert ag_obs[0]["last_tool"] == "run_command"
        assert ag_obs[0]["problem"] == "DemoOp"
    finally:
        holder.terminate()
        holder.wait(timeout=5)
        writer.close()
    assert is_flock_held(lock_file) is False

    # 2. Codex WAL-mode state_5.sqlite + thread_history_1.sqlite
    c_home = Path(os.environ["CODEX_HOME"])
    c_home.mkdir(parents=True, exist_ok=True)
    cx_state = c_home / "state_5.sqlite"
    cx_hist = c_home / "thread_history_1.sqlite"

    s_conn = sqlite3.connect(str(cx_state))
    s_conn.execute("PRAGMA journal_mode=WAL;")
    s_conn.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT)")
    s_conn.execute("CREATE TABLE project_roots (project_id TEXT, path TEXT)")
    s_conn.execute(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY, name TEXT, title TEXT, cwd TEXT, project_id TEXT,
            source TEXT, model TEXT, tokens_used INTEGER, git_branch TEXT, git_sha TEXT,
            updated_at_ms INTEGER, archived INTEGER, rollout_path TEXT
        )
        """
    )
    s_conn.commit()
    s_conn.execute("INSERT INTO projects VALUES ('cx-p1', 'contest_ws')")
    s_conn.execute("INSERT INTO project_roots VALUES ('cx-p1', ?)", (str(real_ws),))
    s_conn.execute(
        """
        INSERT INTO threads VALUES (
            'thr-live-99', 'Parhelion DemoOp', 'Title', ?, 'cx-p1',
            'vscode', 'gpt-5', 4200, 'main', 'abcdef1', 1795396800000, 0, ''
        )
        """,
        (str(real_op),),
    )
    s_conn.commit()

    h_conn = sqlite3.connect(str(cx_hist))
    h_conn.execute("PRAGMA journal_mode=WAL;")
    h_conn.execute("CREATE TABLE thread_turns (thread_id TEXT, rollout_ordinal INTEGER, status TEXT)")
    h_conn.commit()
    h_conn.execute("INSERT INTO thread_turns VALUES ('thr-live-99', 1, 'completed')")
    h_conn.execute("INSERT INTO thread_turns VALUES ('thr-live-99', 2, 'inProgress')")
    h_conn.commit()

    try:
        cx_obs = CodexDriver().observe_gui_sessions(workspace=ws)
        assert len(cx_obs) == 1
        assert cx_obs[0]["native_session_id"] == "thr-live-99"
        assert cx_obs[0]["status"] == "running"
        assert cx_obs[0]["tokens_used"] == 4200
        assert cx_obs[0]["observed_steps"] == 2
        assert cx_obs[0]["deep_link"] == "codex://threads/thr-live-99"
        assert cx_obs[0]["problem"] == "DemoOp"

        # Test `list_all_sessions` + `opsb driver sessions --include-gui` vs `--no-gui`
        merged = list_all_sessions(ws, include_gui=True)
        assert len(merged) == 2
        no_gui = list_all_sessions(ws, include_gui=False)
        assert len(no_gui) == 0

        # Also test `open_sqlite_readonly_wal` direct invocation
        ro = open_sqlite_readonly_wal(cx_state)
        ro.close()
    finally:
        s_conn.close()
        h_conn.close()


def test_build_launch_command_all_six_drivers(isolated_env: Path, tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    mcp_cfg = isolated_env / "mcp_config.json"

    cmd_ag, env_ag = get_driver("antigravity").build_launch_command(ws, "tune", "sess-1", resume_id="ag-99", mcp_config_path=mcp_cfg)
    assert cmd_ag[-1] == "acp"
    assert env_ag["PARHELION_RESUME_ID"] == "ag-99"
    assert env_ag["PARHELION_MCP_CONFIG"] == str(mcp_cfg)

    cmd_cx, env_cx = get_driver("codex").build_launch_command(ws, "tune", "sess-2", mcp_config_path=mcp_cfg)
    assert cmd_cx[1:] == ["app-server", "--listen", "stdio://"]
    assert env_cx["CODEX_HOME"] == str(mcp_cfg.parent)

    cmd_cl, env_cl = get_driver("claude").build_launch_command(ws, "tune", "non-uuid-sess", mcp_config_path=mcp_cfg)
    assert "-p" in cmd_cl
    assert "--input-format" in cmd_cl and "stream-json" in cmd_cl
    assert "--output-format" in cmd_cl
    assert "--strict-mcp-config" in cmd_cl
    assert str(mcp_cfg) in cmd_cl
    sid_idx = cmd_cl.index("--session-id")
    uuid.UUID(cmd_cl[sid_idx + 1])
    assert env_cl["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"

    cmd_cl_res, _ = get_driver("claude").build_launch_command(ws, "tune", "s", resume_id="prev-thread", mcp_config_path=mcp_cfg)
    assert "--resume" in cmd_cl_res
    r_idx = cmd_cl_res.index("--resume")
    uuid.UUID(cmd_cl_res[r_idx + 1])

    (ws / ".agents" / "skills").mkdir(parents=True, exist_ok=True)
    cmd_pi, env_pi = get_driver("pi").build_launch_command(ws, "tune", "sess-4", mcp_config_path=mcp_cfg)
    assert "--mode" in cmd_pi and "rpc" in cmd_pi
    assert "--no-context-files" in cmd_pi
    assert "--session-id" in cmd_pi and "sess-4" in cmd_pi
    assert "--skill" in cmd_pi and str(ws / ".agents" / "skills") in cmd_pi
    assert env_pi["PARHELION_MCP_CONFIG"] == str(mcp_cfg)

    cmd_gm, env_gm = get_driver("gemini").build_launch_command(ws, "tune", "sess-5", resume_id="gem-r", mcp_config_path=mcp_cfg)
    assert cmd_gm[1:3] == ["-p", "tune"]
    assert cmd_gm[3:5] == ["--resume", "gem-r"]
    assert env_gm["PARHELION_MCP_CONFIG"] == str(mcp_cfg)

    cmd_ds, env_ds = get_driver("deepseek").build_launch_command(ws, "tune", "sess-6", mcp_config_path=mcp_cfg)
    assert "--profile" in cmd_ds and "acp" in cmd_ds
    assert env_ds["PARHELION_WORKTREE"] == str(ws.resolve())
    assert env_ds["PARHELION_MCP_CONFIG"] == str(mcp_cfg)


def test_acp_and_codex_and_stream_clients(tmp_path: Path) -> None:
    acp_responses = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "old-s", "replayedPrompts": 0}}),
        json.dumps({"jsonrpc": "2.0", "id": 3, "result": {"sessionId": "fresh-s"}}),
        json.dumps({"jsonrpc": "2.0", "method": "session/update", "params": {"update": {"sessionUpdate": "tool_call", "toolCallId": "tc1", "status": "in_progress"}}}),
        json.dumps({"jsonrpc": "2.0", "method": "session/update", "params": {"update": {"sessionUpdate": "tool_call_update", "toolCallId": "tc1", "status": "completed"}}}),
        json.dumps({"jsonrpc": "2.0", "id": 4, "result": {"stopReason": "end_turn"}}),
    ]) + "\n"
    acp_in = io.StringIO()
    acp_out = io.StringIO(acp_responses)
    acp_log = io.StringIO()
    client = AcpSessionClient(acp_in, acp_out, log_stream=acp_log, quiet_window_sec=0.0)
    client.initialize()
    sid, resumed = client.open_session(tmp_path, mcp_servers=[], system_append="kb-hint", resume_id="old-s")
    assert resumed is False
    assert sid == "fresh-s"
    turn = client.send_prompt(sid, "optimize kernel")
    assert turn["stopReason"] == "end_turn"
    assert len(client.open_tool_calls) == 0
    sent_methods = [json.loads(ln)["method"] for ln in acp_in.getvalue().splitlines()]
    assert sent_methods == ["initialize", "session/load", "session/new", "session/prompt"]

    cx_responses = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"userAgent": "codex"}}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"thread": {"id": "thr_123"}}}),
        json.dumps({"jsonrpc": "2.0", "id": 3, "result": {"turn": {"id": "t_1"}}}),
        json.dumps({"jsonrpc": "2.0", "method": "turn/completed", "params": {"status": "completed"}}),
    ]) + "\n"
    cx_in = io.StringIO()
    cx_client = CodexAppServerClient(cx_in, io.StringIO(cx_responses))
    cx_client.initialize()
    tid = cx_client.open_thread(tmp_path)
    assert tid == "thr_123"
    cx_client.start_turn(tid, "hello")
    cx_methods = [json.loads(ln)["method"] for ln in cx_in.getvalue().splitlines()]
    assert cx_methods == ["initialize", "initialized", "thread/start", "turn/start"]

    assert json.loads(ClaudeStreamJsonClient.encode_user_message("hi"))["type"] == "user"
    assert json.loads(PiRpcClient.encode_prompt("hi"))["type"] == "prompt"


def test_explicit_worker_model_requires_matching_protocol_receipt(tmp_path):
    old, new = "v4-flash", "deepseek-flash"
    setting = {"id": "model", "currentValue": old, "options": [{"options": [
        {"name": "DeepSeek-V41-Flash", "value": new}, {"name": "DeepSeek-V4-Flash", "value": old}
    ]}]}
    wire = io.StringIO()
    responses = [dict(sessionId="s", configOptions=[setting]), dict(configOptions=[setting])]
    client = AcpSessionClient(wire, io.StringIO("".join(
        json.dumps({"id": i, "result": reply}) + "\n" for i, reply in enumerate(responses, 1))))
    with pytest.raises(RuntimeError, match="did not confirm"):
        client.open_session(tmp_path, [], model="DeepSeek V4.1 Flash")
    sent = [json.loads(line) for line in wire.getvalue().splitlines()]
    assert [row["method"] for row in sent] == ["session/new", "session/set_config_option"]
    assert sent[1]["params"]["value"] == new

    response = {"id": 1, "result": {"thread": {"id": "t"}, "model": "other"}}
    codex = CodexAppServerClient(io.StringIO(), io.StringIO(json.dumps(response) + "\n"))
    with pytest.raises(RuntimeError, match="did not confirm"):
        codex.open_thread(tmp_path, model="gpt-6.2-astra")


def test_dry_run_launch_worktree_brief_and_session_persistence(isolated_env: Path, tmp_path: Path) -> None:
    ws, op_dir = _make_git_workspace(tmp_path)

    sel = select_active_driver("codex")
    assert sel["active_driver"] == "codex"
    assert get_active_driver_kind() == "codex"

    wt1 = prepare_session_worktree(ws, "DemoOp", track_name="track-a")
    assert wt1 == (op_dir / ".worktrees" / "track-a").resolve()
    assert (wt1 / ".git").exists()
    wt1_again = prepare_session_worktree(ws, "DemoOp", track_name="track-a")
    assert wt1_again == wt1
    assert prepare_session_worktree(ws, "DemoOp", use_main=True) == op_dir.resolve()

    brief = compose_session_brief(ws, "DemoOp", goal="Cut S_SMALL_CORE latency")
    assert "DemoOp" in brief
    assert "68.50" in brief
    assert len(brief.encode("utf-8")) < 3072

    fake_ff = mock.MagicMock()
    fake_ff.build_pmu_torch_card.return_value = {"dominant_bottleneck": "mte2-bound", "markdown_card": "Bucket S1: 2.1x torch"}
    fake_ff.evaluate_elf_guard.return_value = {"status": "warn", "alerts": [{"code": "ICACHE"}]}
    fake_ff.evaluate_cold3_probe.return_value = {"status": "ok", "bimodal_spike_detected": False}
    fake_kb = mock.MagicMock()
    fake_kb.query_kb.return_value = {"entries": [{"id": "SNP-1"}], "anti_patterns": []}
    fake_kb.format_prompt_cache_digest.return_value = "## KB Digest\n- SNP-1: Zero TPipe"

    with mock.patch.dict(sys.modules, {
        "opsb_core.services.fast_feedback_service": fake_ff,
        "opsb_core.services.triad_kb_service": fake_kb,
    }):
        enriched_brief = compose_session_brief(ws, "DemoOp", goal="Cut S_SMALL_CORE latency")
    # 4.0 briefs no longer inline KB digests or fast-feedback cards; workers query the KB on demand.
    assert "SNP-1" not in enriched_brief
    assert "Bucket S1" not in enriched_brief
    assert "parhelion_kb_query" in enriched_brief
    assert len(enriched_brief.encode("utf-8")) < 3072

    res = launch_problem_session(ws, "DemoOp", driver_kind="claude", mode="dry-run", track_name="track-b", goal="Test dry run")
    assert res["ok"] is True
    sess = res["session"]
    assert sess["driver"] == "claude"
    assert sess["status"] == "idle"
    assert sess["pid"] is None
    assert sess["problem"] == "DemoOp"

    store_file = sessions_file_path(ws)
    assert store_file.is_file()
    assert str(isolated_env) in str(store_file)
    persisted = list_sessions(ws, problem="DemoOp", driver="claude")
    assert len(persisted) == 1
    assert persisted[0].session_id == sess["session_id"]


def test_driver_cli_subcommands(isolated_env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws, _ = _make_git_workspace(tmp_path)
    parser = argparse.ArgumentParser(prog="opsb")
    sub = parser.add_subparsers(dest="cmd", required=True)
    register_driver_and_mcp_subcommands(sub)

    args = parser.parse_args(["driver", "select", "pi", "--json"])
    assert args.func(args) == 0
    assert json.loads(capsys.readouterr().out)["active_driver"] == "pi"

    args = parser.parse_args(["driver", "list", "--json"])
    assert args.func(args) == 0
    ls_out = json.loads(capsys.readouterr().out)
    assert ls_out["active_driver"] == "pi"
    assert len(ls_out["drivers"]) == 6

    args = parser.parse_args(["driver", "launch", "DemoOp", "--mode", "dry-run", "--track", "cli-t", "--workspace", str(ws), "--json"])
    assert args.func(args) == 0
    la_out = json.loads(capsys.readouterr().out)
    assert la_out["session"]["driver"] == "pi"

    # `--mode gui --dry-run` works for codex
    args = parser.parse_args(["driver", "launch", "DemoOp", "--driver", "codex", "--mode", "gui", "--dry-run", "--track", "cli-gui", "--workspace", str(ws), "--json"])
    assert args.func(args) == 0
    gui_out = json.loads(capsys.readouterr().out)
    assert gui_out["deep_link"].startswith("codex://threads/new?")
    assert gui_out["dry_run"] is True

    args = parser.parse_args(["driver", "sessions", "--workspace", str(ws), "--json"])
    assert args.func(args) == 0
    se_out = json.loads(capsys.readouterr().out)
    assert len(se_out["sessions"]) == 2


def test_headless_worker_job_updates_session_status(isolated_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from opsb_core.drivers.registry import _run_worker_job
    from opsb_core.drivers.session_store import get_session

    ws, op_dir = _make_git_workspace(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    gem = fake_bin / "gemini"
    gem.write_text("#!/bin/sh\necho '{\"ok\": true}'\nexit 0\n", encoding="utf-8")
    gem.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin))
    monkeypatch.setenv("GEMINI_API_KEY", "k")

    drv = get_driver("gemini")
    rec = drv.launch_session(ws, "DemoOp", op_dir, "optimize", mode="dry-run")
    log_path = isolated_env / "logs" / "sessions" / f"{rec.session_id}.jsonl"
    job_path = tmp_path / "job.json"
    job_path.write_text(
        json.dumps(
            {
                "driver": "gemini",
                "workspace": str(ws),
                "problem": "DemoOp",
                "worktree": str(op_dir),
                "brief": "optimize",
                "session_id": rec.session_id,
                "native_session_id": rec.native_session_id,
                "resume_id": None,
                "cmd": [str(gem), "-p", "optimize"],
                "env": {},
                "log_path": str(log_path),
            }
        ),
        encoding="utf-8",
    )
    rc = _run_worker_job(job_path)
    assert rc == 0
    updated = get_session(ws, rec.session_id)
    assert updated is not None
    assert updated.status == "completed"
    assert updated.pid is None
    assert log_path.is_file()
