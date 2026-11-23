"""Tests for Parhelion Unified Single-Driver Runtime (`opsb_core.drivers` and `opsb driver`)."""
from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
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
    CodexAppServerClient,
    ClaudeStreamJsonClient,
    PiRpcClient,
    VALID_DRIVERS,
    compose_session_brief,
    doctor_drivers,
    get_active_driver_kind,
    get_driver,
    launch_problem_session,
    list_sessions,
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
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("PARHELION_HOME", str(p_home))
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

    monkeypatch.setenv("ANTIGRAVITY_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("PI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

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
        assert diag["version"] == "1.2.3"
        assert str(fake_bin) in diag["binary_path"]

    # Missing binary cleanly reports installed=False without throwing
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
        assert codex_diag.auth_ok is False
        assert "not found" in codex_diag.detail


def test_sync_workspace_codex_isolated_and_claude_symlink(isolated_env: Path, tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()

    # Create pre-existing ~/.codex/config.toml to assert Codex sync NEVER mutates it
    user_codex = Path(tmp_path / "home" / ".codex")
    user_codex.mkdir(parents=True)
    global_toml = user_codex / "config.toml"
    original_global = '# untouched user codex config\nmodel = "gpt-5"\n'
    global_toml.write_text(original_global, encoding="utf-8")

    codex_res = get_driver("codex").sync_workspace(ws)
    assert codex_res["ok"] is True
    codex_cfg = Path(codex_res["codex_config_toml"])
    assert codex_cfg.is_file()
    assert str(isolated_env) in str(codex_cfg)
    toml_txt = codex_cfg.read_text(encoding="utf-8")
    assert "[mcp_servers.parhelion]" in toml_txt
    assert "mcp" in toml_txt and "serve" in toml_txt
    assert global_toml.read_text(encoding="utf-8") == original_global

    # Claude workspace sync creates `.claude/skills -> ../.agents/skills` inside workspace only
    claude_res = get_driver("claude").sync_workspace(ws)
    assert claude_res["ok"] is True
    link_p = ws / ".claude" / "skills"
    assert link_p.is_symlink()
    assert link_p.readlink() == Path("..") / ".agents" / "skills"
    assert (ws / ".agents" / "skills").is_dir()
    # Idempotent re-sync
    claude_res2 = get_driver("claude").sync_workspace(ws)
    assert claude_res2["symlink_status"] == "preserved"


def test_build_launch_command_all_six_drivers(isolated_env: Path, tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    mcp_cfg = isolated_env / "mcp_config.json"

    # 1. Antigravity
    cmd_ag, env_ag = get_driver("antigravity").build_launch_command(ws, "tune", "sess-1", resume_id="ag-99", mcp_config_path=mcp_cfg)
    assert cmd_ag[-1] == "acp"
    assert env_ag["PARHELION_RESUME_ID"] == "ag-99"
    assert env_ag["PARHELION_MCP_CONFIG"] == str(mcp_cfg)

    # 2. Codex
    cmd_cx, env_cx = get_driver("codex").build_launch_command(ws, "tune", "sess-2", mcp_config_path=mcp_cfg)
    assert cmd_cx[1:] == ["app-server", "--listen", "stdio://"]
    assert env_cx["CODEX_HOME"] == str(mcp_cfg.parent)

    # 3. Claude (valid UUID coercion + hygiene env + strict mcp)
    cmd_cl, env_cl = get_driver("claude").build_launch_command(ws, "tune", "non-uuid-sess", mcp_config_path=mcp_cfg)
    assert "-p" in cmd_cl
    assert "--input-format" in cmd_cl and "stream-json" in cmd_cl
    assert "--output-format" in cmd_cl
    assert "--strict-mcp-config" in cmd_cl
    assert str(mcp_cfg) in cmd_cl
    sid_idx = cmd_cl.index("--session-id")
    uuid.UUID(cmd_cl[sid_idx + 1])  # must be valid UUID
    assert env_cl["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"

    cmd_cl_res, _ = get_driver("claude").build_launch_command(ws, "tune", "s", resume_id="prev-thread", mcp_config_path=mcp_cfg)
    assert "--resume" in cmd_cl_res
    r_idx = cmd_cl_res.index("--resume")
    uuid.UUID(cmd_cl_res[r_idx + 1])

    # 4. Pi
    (ws / ".agents" / "skills").mkdir(parents=True, exist_ok=True)
    cmd_pi, env_pi = get_driver("pi").build_launch_command(ws, "tune", "sess-4", mcp_config_path=mcp_cfg)
    assert "--mode" in cmd_pi and "rpc" in cmd_pi
    assert "--no-context-files" in cmd_pi
    assert "--session-id" in cmd_pi and "sess-4" in cmd_pi
    assert "--skill" in cmd_pi and str(ws / ".agents" / "skills") in cmd_pi
    assert env_pi["PARHELION_MCP_CONFIG"] == str(mcp_cfg)

    # 5. Gemini
    cmd_gm, env_gm = get_driver("gemini").build_launch_command(ws, "tune", "sess-5", resume_id="gem-r", mcp_config_path=mcp_cfg)
    assert cmd_gm[1:3] == ["-p", "tune"]
    assert cmd_gm[3:5] == ["--resume", "gem-r"]
    assert env_gm["PARHELION_MCP_CONFIG"] == str(mcp_cfg)

    # 6. DeepSeek
    cmd_ds, env_ds = get_driver("deepseek").build_launch_command(ws, "tune", "sess-6", mcp_config_path=mcp_cfg)
    assert "--cwd" in cmd_ds and str(ws.resolve()) in cmd_ds
    assert "-p" in cmd_ds and "tune" in cmd_ds
    assert env_ds["PARHELION_MCP_CONFIG"] == str(mcp_cfg)


def test_acp_and_codex_and_stream_clients(tmp_path: Path) -> None:
    # ACP client: session/load with replayedPrompts=0 falls back to session/new
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

    # Codex client
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

    # Claude & Pi formatters
    assert json.loads(ClaudeStreamJsonClient.encode_user_message("hi"))["type"] == "user"
    assert json.loads(PiRpcClient.encode_prompt("hi"))["type"] == "prompt"


def test_dry_run_launch_worktree_brief_and_session_persistence(isolated_env: Path, tmp_path: Path) -> None:
    ws, op_dir = _make_git_workspace(tmp_path)

    # Select active driver and verify config persistence
    sel = select_active_driver("codex")
    assert sel["active_driver"] == "codex"
    assert get_active_driver_kind() == "codex"

    # Prepare worktree + idempotent reuse + use_main
    wt1 = prepare_session_worktree(ws, "DemoOp", track_name="track-a")
    assert wt1 == (op_dir / ".worktrees" / "track-a").resolve()
    assert (wt1 / ".git").exists()
    wt1_again = prepare_session_worktree(ws, "DemoOp", track_name="track-a")
    assert wt1_again == wt1
    assert prepare_session_worktree(ws, "DemoOp", use_main=True) == op_dir.resolve()

    # Compose brief stays < 3KB both standalone and with Track 1/2 services mocked
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
    assert "SNP-1" in enriched_brief
    assert "Bucket S1" in enriched_brief
    assert len(enriched_brief.encode("utf-8")) < 3072

    # Dry-run launch persists into isolated driver_sessions.json
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

    args = parser.parse_args(["driver", "sessions", "--workspace", str(ws), "--json"])
    assert args.func(args) == 0
    se_out = json.loads(capsys.readouterr().out)
    assert len(se_out["sessions"]) == 1


def test_headless_worker_job_updates_session_status(isolated_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from opsb_core.drivers.registry import _run_worker_job
    from opsb_core.drivers.session_store import get_session

    ws, op_dir = _make_git_workspace(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    dsh = fake_bin / "dsh"
    dsh.write_text("#!/bin/sh\necho '{\"ok\": true}'\nexit 0\n", encoding="utf-8")
    dsh.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")

    # Launch dry-run to create a real session record, then drive _run_worker_job against fake dsh
    drv = get_driver("deepseek")
    rec = drv.launch_session(ws, "DemoOp", op_dir, "optimize", mode="dry-run")
    log_path = isolated_env / "logs" / "sessions" / f"{rec.session_id}.jsonl"
    job_path = tmp_path / "job.json"
    job_path.write_text(
        json.dumps(
            {
                "driver": "deepseek",
                "workspace": str(ws),
                "problem": "DemoOp",
                "worktree": str(op_dir),
                "brief": "optimize",
                "session_id": rec.session_id,
                "native_session_id": rec.native_session_id,
                "resume_id": None,
                "cmd": [str(dsh), "--cwd", str(op_dir), "-p", "optimize"],
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

