"""P08: ``parhelion_agent_peek`` (worktree read, log tail, diff stat, path and role limits) and the
DSH mount plugin's role decision."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from opsb_core.agents.bus_store import AgentBusStore
from opsb_core.drivers.base import resolve_session_logs_dir
from opsb_core.mcp import peek
from opsb_core.mcp.server import handle_jsonrpc_message
from opsb_core.mcp.tools import is_tool_allowed_for_role


@pytest.fixture()
def contest(tmp_path, monkeypatch):
    monkeypatch.setenv("PARHELION_HOME", str(tmp_path / "phome"))
    monkeypatch.setenv("PARHELION_DISABLE_DAEMON_PROXY", "1")
    ws = (tmp_path / "contest").resolve()
    ws.mkdir()
    worktree = ws / "DemoOp" / ".worktrees" / "sprint-solo"
    (worktree / "op_kernel").mkdir(parents=True)
    (worktree / "kernel.asc").write_text("\n".join(f"line {i}" for i in range(1, 61)) + "\n", encoding="utf-8")
    (worktree / "op_kernel" / "copy.asc").write_text("__aicore__ void CopyIn(int x) {\n}\n", encoding="utf-8")
    store = AgentBusStore(ws)
    store.create_agent({"agent_id": "dee-1", "driver": "deepseek", "problem": "DemoOp",
                        "worktree": str(worktree), "branch": "sprint-solo", "status": "running"})
    return ws, worktree


def _rpc(tool: str, args: dict, ws: Path, role: str = "orchestrator") -> dict:
    return handle_jsonrpc_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": tool, "arguments": args}},
                                  workspace=ws, role=role)


def test_files_lists_the_worktree(contest):
    ws, worktree = contest
    out = peek.peek({"agent_id": "dee-1", "what": "files"}, ws)
    assert out["ok"] is True and out["problem"] == "DemoOp"
    assert "kernel.asc" in "\n".join(out["entries"])
    assert "op_kernel/" in "\n".join(out["entries"])
    assert str(worktree) == out["worktree"]


def test_file_reads_a_relative_path_and_a_line_range(contest):
    ws, _ = contest
    out = peek.peek({"agent_id": "dee-1", "what": "file", "path": "kernel.asc", "lines": "10-12"}, ws)
    assert out["from"] == 10 and out["to"] == 12
    assert "line 10" in out["text"] and "line 12" in out["text"]
    assert "line 13" not in out["text"]
    assert out["total_lines"] == 60
    default = peek.peek({"agent_id": "dee-1", "what": "file", "path": "op_kernel/copy.asc"}, ws)
    assert "CopyIn" in default["text"]


@pytest.mark.parametrize("path", ["../outside.txt", "op_kernel/../../escape.txt", "/etc/passwd"])
def test_file_refuses_paths_outside_the_worktree(contest, path):
    ws, _ = contest
    with pytest.raises(peek.PeekError):
        peek.peek({"agent_id": "dee-1", "what": "file", "path": path}, ws)


def test_file_refuses_a_symlink_that_escapes(tmp_path, contest):
    ws, worktree = contest
    outside = tmp_path / "secret.txt"
    outside.write_text("nope\n", encoding="utf-8")
    (worktree / "link.txt").symlink_to(outside)
    with pytest.raises(peek.PeekError):
        peek.peek({"agent_id": "dee-1", "what": "file", "path": "link.txt"}, ws)


def test_diff_stat_runs_git_in_the_worktree(contest):
    ws, worktree = contest
    if not shutil.which("git"):
        pytest.skip("git is not installed")
    subprocess.run(["git", "-C", str(worktree), "init", "-q"], check=True, timeout=30)
    subprocess.run(["git", "-C", str(worktree), "-c", "user.email=p@example.invalid", "-c", "user.name=P08",
                    "add", "-A"], check=True, timeout=30)
    subprocess.run(["git", "-C", str(worktree), "-c", "user.email=p@example.invalid", "-c", "user.name=P08",
                    "-c", "commit.gpgsign=false", "commit", "-qm", "init"], check=True, timeout=30)
    (worktree / "kernel.asc").write_text("changed\n", encoding="utf-8")
    out = peek.peek({"agent_id": "dee-1", "what": "diff_stat"}, ws)
    assert "kernel.asc" in out["stat"]
    assert out["dirty"] is True


def test_log_tail_is_read_only_and_redacts_credentials(contest):
    ws, _ = contest
    logs = resolve_session_logs_dir()
    logs.mkdir(parents=True, exist_ok=True)
    rows = [
        {"ts": "2026-11-26T10:00:00Z", "dir": "out",
         "msg": {"method": "session/update", "params": {"update": {"content": {"text": f"step {i}"}}}}}
        for i in range(60)
    ]
    rows.append({"ts": "2026-11-26T10:01:00Z", "dir": "out",
                 "msg": {"method": "session/update",
                         "params": {"update": {"content": {"text": "Cookie: session=abc123 secret"}}}}})
    (logs / "mesh-dee-1.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    before = (logs / "mesh-dee-1.jsonl").read_bytes()
    out = peek.peek({"agent_id": "dee-1", "what": "log_tail", "lines": 5}, ws)
    assert out["lines"] == 5 and out["truncated"] is True
    assert "step 59" in out["text"]
    assert "abc123" not in out["text"] and "<redacted>" in out["text"]
    assert (logs / "mesh-dee-1.jsonl").read_bytes() == before  # never written


def test_unknown_agent_and_bad_view_are_errors(contest):
    ws, _ = contest
    with pytest.raises(peek.PeekError):
        peek.peek({"agent_id": "nobody", "what": "files"}, ws)
    with pytest.raises(peek.PeekError):
        peek.peek({"what": "files"}, ws)
    with pytest.raises(peek.PeekError):
        peek.peek({"agent_id": "dee-1", "what": "everything"}, ws)


def test_peek_is_orchestrator_only(contest):
    ws, _ = contest
    assert is_tool_allowed_for_role("parhelion_agent_peek", "orchestrator")
    assert not is_tool_allowed_for_role("parhelion_agent_peek", "worker")
    blocked = _rpc("parhelion_agent_peek", {"agent_id": "dee-1", "what": "files"}, ws, role="worker")
    assert blocked["error"]["code"] == -32601
    assert "disabled for role 'worker'" in blocked["error"]["message"]
    allowed = _rpc("parhelion_agent_peek", {"agent_id": "dee-1", "what": "files"}, ws)
    assert allowed["result"]["isError"] is False
    payload = json.loads(allowed["result"]["content"][0]["text"])
    assert payload["ok"] is True and payload["agent_id"] == "dee-1"


def test_output_stays_under_the_current_ceiling(contest):
    ws, worktree = contest
    (worktree / "big.txt").write_text("\n".join(["x" * 100] * 2000), encoding="utf-8")
    out = peek.peek({"agent_id": "dee-1", "what": "file", "path": "big.txt", "lines": "1-800"}, ws)
    assert len(json.dumps(out, ensure_ascii=False).encode("utf-8")) <= peek._max_out() <= peek.MAX_OUT_BYTES
    from opsb_core.mcp.bound import output_cap
    assert peek._max_out() < output_cap()  # answered inline, never spilled by the MCP bound
    assert out.get("truncated") is True


def test_dsh_plugin_roles_and_mount_config_with_node():
    """Fabricated plugin context: the real ``apply()`` runs once and the server configs are checked."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    script = Path(__file__).parent / "node" / "parhelion_plugin_check.mjs"
    proc = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    roles = {case["id"]: case["role"] for case in data["cases"]}
    assert roles == {"main": "orchestrator", "worktree-worker": "worker", "native-child": "worker",
                     "child-in-worktree": "worker", "unclassified": "worker", "elsewhere": None}
    assert data["cases"][-1]["mounted"] is False
    assert data["event"] == ["agent/created"]
    mounts = {m["agent"]: m for m in data["mounts"]}
    assert set(mounts) == {"main", "worktree-worker", "native-child", "child-in-worktree", "unclassified"}
    assert all(m["serverName"] == "parhelion" for m in mounts.values())
    assert mounts["main"]["role"] == "orchestrator"
    assert all(m["role"] == "worker" for k, m in mounts.items() if k != "main")
    for m in mounts.values():
        assert m["args"][:3] == ["/tmp/fake-opsb", "mcp", "serve"]
        assert "--workspace" in m["args"] and "--role" in m["args"]
