"""4.2 A1/A2/A3: per-call cache usage from driver logs, miss reasons, cursors, hot mesh, MCP metering.

All fixtures are synthetic (field structure of the real logs, made-up text); DSH zstd files
are compressed on the spot.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core.metrics import mcp_meter, shape, store, usage  # noqa: E402

T0 = datetime.datetime(2026, 1, 2, 3, 0, 0, tzinfo=datetime.timezone.utc)


def zstd_frame(data: bytes) -> bytes:
    try:
        from compression import zstd
        return zstd.compress(data)
    except ImportError:
        exe = shutil.which("zstd")
        if not exe:
            pytest.skip("no zstd codec available")
        return subprocess.run([exe, "-q", "-c"], input=data, capture_output=True, check=True).stdout


def jsonl(rows: list[dict]) -> bytes:
    return "".join(json.dumps(r) + "\n" for r in rows).encode()


class DshEvents:
    """Builder for a DSH native session event stream."""

    def __init__(self, start: datetime.datetime = T0) -> None:
        self.rows: list[dict] = []
        self.ms = int(start.timestamp() * 1000)

    def _add(self, kind: str, data: dict, *, op=None, step_sec: float = 5.0) -> None:
        self.ms += int(step_sec * 1000)
        row = {"type": kind, "seq": len(self.rows), "time": self.ms, "data": data}
        if op is not None:
            row["surfaceOp"] = op
        self.rows.append(row)

    def system(self, text: str, op=None) -> None:
        self._add("system/message", {"message": {"role": "system", "content": [{"type": "text", "text": text}]}},
                  op=op or "append")

    def header(self, names: list[str]) -> None:
        tools = [{"name": n, "description": f"tool {n}", "parameters": {"type": "object"}} for n in names]
        self._add("request/header", {"header": {"config": {"model": "model-x"}, "tools": tools}, "reason": "initial"})

    def user(self, text: str, op=None) -> None:
        self._add("user/message", {"content": [{"type": "text", "text": text}], "role": "user"}, op=op or "append")

    def call(self, inp: int, read: int, out: int = 50, *, gap_sec: float = 5.0) -> None:
        self._add("assistant/message", {"message": {"role": "assistant", "source": {"model": "model-x"}},
                                        "usage": {"inputTokens": inp, "outputTokens": out, "cacheReadTokens": read,
                                                  "cacheWriteTokens": 0, "totalTokens": inp + out + read}},
                  op="append", step_sec=gap_sec)


class MeshLog:
    """Builder for a platform ACP mesh log (``{ts, dir, msg}`` rows)."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.next_id = 1
        self.t = T0

    def _ts(self) -> str:
        self.t += datetime.timedelta(seconds=1)
        return self.t.isoformat()

    def new_session(self, cwd: str, sid: str) -> None:
        rid = self.next_id
        self.next_id += 1
        self.rows.append({"ts": self._ts(), "dir": "out", "msg": {"jsonrpc": "2.0", "id": rid, "method": "session/new",
                                                                   "params": {"cwd": cwd, "mcpServers": []}}})
        self.rows.append({"ts": self._ts(), "dir": "in", "msg": {"jsonrpc": "2.0", "id": rid,
                                                                  "result": {"sessionId": sid, "configOptions": []}}})

    def usage(self, sid: str, used: int) -> None:
        self.rows.append({"ts": self._ts(), "dir": "in", "msg": {"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": sid, "update": {"sessionUpdate": "usage_update", "used": used, "size": 1_000_000}}}})


def _six_reason_session(ev: DshEvents) -> None:
    ev.system("You are a worker.")
    ev.header(["bash", "read"])
    ev.call(10_000, 0)                      # cold_start
    ev.call(500, 10_000)                    # hit
    ev.system("You are a worker (v2).", op={"op": "replace", "startSeq": 0, "endSeq": 0})
    ev.call(10_000, 1_000)                  # system_changed
    ev.call(500, 11_000)
    ev.header(["bash", "read", "edit"])
    ev.call(11_000, 500)                    # tools_changed
    ev.call(300, 11_500)
    ev.user("older turn rewritten", op={"op": "replace", "startSeq": 3, "endSeq": 3})
    ev.call(6_000, 4_000)                   # compaction_or_truncation (declared rewrite)
    ev.call(500, 10_000)
    ev.call(10_000, 500, gap_sec=7_200)     # ttl_gap
    ev.call(500, 10_500)
    ev.call(9_000, 1_000)                   # unknown


def _write_worker(tmp: Path, agent_id: str = "dee-abc123") -> tuple[Path, Path, Path]:
    dsh_home = tmp / "dsh"
    cwd = str(tmp / "worktree")
    mesh = MeshLog()
    mesh.new_session(cwd, "sess-a")
    mesh.usage("sess-a", 12_000)
    mesh.new_session(cwd, "sess-b")
    ev_a = DshEvents()
    _six_reason_session(ev_a)
    ev_b = DshEvents(T0 + datetime.timedelta(hours=3))
    ev_b.system("You are a worker (v2).")
    ev_b.header(["bash", "read", "edit"])
    ev_b.call(9_000, 1_000)                 # new context after a platform handoff
    ev_b.call(400, 10_000)
    for sid, ev in (("sess-a", ev_a), ("sess-b", ev_b)):
        p = usage.dsh_session_path(cwd, sid, dsh_home)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(zstd_frame(jsonl(ev.rows)))
    log = tmp / "logs" / f"mesh-{agent_id}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_bytes(jsonl(mesh.rows))
    return log, dsh_home, Path(cwd)


def test_dsh_usage_records_and_six_miss_reasons(tmp_path: Path) -> None:
    log, dsh_home, _ = _write_worker(tmp_path)
    res = usage.agent_usage(log, dsh_home=dsh_home)
    assert res["status"] == "ok" and res["driver"] == "deepseek"
    calls = res["calls"]
    assert len(calls) == 13
    first = calls[0]
    assert {"agent_id", "driver", "model", "ts", "input_miss", "cache_read", "cache_write", "output", "call_seq"} <= set(first)
    assert first["agent_id"] == "dee-abc123" and first["model"] == "model-x" and first["input_miss"] == 10_000
    assert [c["call_seq"] for c in calls] == list(range(13))
    reasons = [c["miss_reason"] for c in calls if c["miss"]]
    assert reasons == ["cold_start", "system_changed", "tools_changed", "compaction_or_truncation", "ttl_gap",
                       "unknown", "compaction_or_truncation"]
    summary = shape.summarize(calls)
    assert summary["last_miss_reason"] == "compaction_or_truncation"
    expected = summary["cache_read"] / (summary["cache_read"] + summary["input_miss"]) * 100
    assert summary["cache_hit_pct"] == round(expected, 1)


def test_unavailable_drivers_and_missing_native_log(tmp_path: Path) -> None:
    agy = tmp_path / "mesh-ant-000001.jsonl"
    agy.write_bytes(jsonl(MeshLog().rows))
    assert usage.agent_usage(agy)["status"] == "unavailable"
    mesh = MeshLog()
    mesh.new_session(str(tmp_path / "wt"), "missing")
    dee = tmp_path / "mesh-dee-000002.jsonl"
    dee.write_bytes(jsonl(mesh.rows))
    res = usage.agent_usage(dee, dsh_home=tmp_path / "nohome")
    assert res["status"] == "unavailable" and res["calls"] == []


def test_codex_usage_counts_cached_inside_input(tmp_path: Path) -> None:
    ts = T0.isoformat()

    def tok(total: int, inp: int, cached: int, out: int) -> dict:
        return {"ts": ts, "dir": "in", "msg": {"method": "thread/tokenUsage/updated", "params": {"tokenUsage": {
            "total": {"totalTokens": total}, "last": {"inputTokens": inp, "cachedInputTokens": cached,
                                                      "outputTokens": out, "totalTokens": inp + out},
            "modelContextWindow": 200_000}}}}

    rows = [
        {"ts": ts, "dir": "out", "msg": {"id": 1, "method": "thread/start", "params": {"cwd": "/w"}}},
        {"ts": ts, "dir": "in", "msg": {"id": 1, "result": {"model": "codex-model", "thread": {"id": "th1"}}}},
        tok(1_010, 1_000, 0, 10), tok(1_010, 1_000, 0, 10),          # duplicate notification is skipped
        tok(2_520, 1_500, 1_000, 10),
        {"ts": ts, "dir": "out", "msg": {"id": 2, "method": "thread/compact/start", "params": {"threadId": "th1"}}},
        tok(3_100, 1_200, 100, 20),
    ]
    log = tmp_path / "mesh-cod-aaaaaa.jsonl"
    log.write_bytes(jsonl(rows))
    res = usage.agent_usage(log)
    assert res["status"] == "ok" and res["driver"] == "codex"
    calls = res["calls"]
    assert [(c["input_miss"], c["cache_read"]) for c in calls] == [(1_000, 0), (500, 1_000), (1_100, 100)]
    assert calls[0]["model"] == "codex-model"
    assert [c["miss_reason"] for c in calls if c["miss"]] == ["cold_start", "compaction_or_truncation"]


def test_claude_usage_one_record_per_message_with_cache_writes(tmp_path: Path) -> None:
    def asst(mid: str, inp: int, write: int, read: int, out: int) -> dict:
        return {"type": "assistant", "message": {"id": mid, "model": "claude-x", "usage": {
            "input_tokens": inp, "cache_creation_input_tokens": write, "cache_read_input_tokens": read,
            "output_tokens": out}}}

    rows = [{"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-x", "tools": ["Bash", "Read"]},
            asst("m1", 5, 2_000, 0, 10), asst("m1", 5, 2_000, 0, 30), asst("m2", 3, 100, 2_000, 5),
            {"type": "result", "result": "done"}]
    log = tmp_path / "mesh-cla-bbbbbb.jsonl"
    log.write_bytes(jsonl(rows))
    res = usage.agent_usage(log)
    calls = res["calls"]
    assert res["status"] == "ok" and len(calls) == 2
    assert (calls[0]["input_miss"], calls[0]["cache_write"], calls[0]["output"]) == (5, 2_000, 30)
    summary = shape.summarize(calls)
    assert summary["cache_hit_pct"] == round(100 * 2_000 / (2_000 + 8 + 2_100), 1)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_store_parses_incrementally_without_touching_sources(tmp_path: Path) -> None:
    dsh_home = tmp_path / "dsh"
    cwd = str(tmp_path / "wt")
    ev = DshEvents()
    _six_reason_session(ev)
    half = len(ev.rows) // 2
    session = usage.dsh_session_path(cwd, "sess-a", dsh_home)
    session.parent.mkdir(parents=True)
    session.write_bytes(zstd_frame(jsonl(ev.rows[:half])))
    mesh = MeshLog()
    mesh.new_session(cwd, "sess-a")
    log = tmp_path / "mesh-dee-cccccc.jsonl"
    log.write_bytes(jsonl(mesh.rows))
    ws = tmp_path / "ws"
    ws.mkdir()
    with mock.patch.dict(os.environ, {"PARHELION_HOME": str(tmp_path / "home")}):
        before = (_digest(log), _digest(session))
        first = store.refresh(ws, "dee-cccccc", "deepseek", log_path=log, dsh_home=dsh_home)
        assert (_digest(log), _digest(session)) == before
        with session.open("ab") as fh:  # DSH appends one more frame
            fh.write(zstd_frame(jsonl(ev.rows[half:])))
        second = store.refresh(ws, "dee-cccccc", "deepseek", log_path=log, dsh_home=dsh_home)
        cursor_dir = store.metrics_dir(ws) / "usage"
        assert (cursor_dir / "dee-cccccc.json").is_file()
    full = shape.summarize(usage.agent_usage(log, dsh_home=dsh_home)["calls"])
    assert first["summary"]["calls"] < second["summary"]["calls"] == full["calls"]
    for key in ("input_miss", "cache_read", "miss_events", "reasons", "last_miss_reason", "cache_hit_pct"):
        assert second["summary"][key] == full[key]


def test_usage_monitor_is_non_blocking_and_fields_are_optional(tmp_path: Path) -> None:
    log, dsh_home, _ = _write_worker(tmp_path, "dee-dddddd")
    ws = tmp_path / "ws"
    ws.mkdir()
    assert store.hot_fields({"status": "unavailable"}) == {}
    with mock.patch.dict(os.environ, {"PARHELION_HOME": str(tmp_path / "home"), "DSH_HOME": str(dsh_home)}):
        mon = store.UsageMonitor(min_interval_sec=0.0)
        target = {"key": "dee-dddddd", "driver": "deepseek", "log_path": log}
        assert mon.fields(ws, [target]) == {"dee-dddddd": {}}  # first call never parses inline
        deadline = time.time() + 10
        got: dict = {}
        while time.time() < deadline and not got:
            time.sleep(0.05)
            got = mon.fields(ws, [target])["dee-dddddd"]
    assert set(got) == {"cache_hit_pct", "input_miss_tokens", "cache_read_tokens", "last_miss_reason"}
    assert got["last_miss_reason"] == "compaction_or_truncation"


def test_hot_mesh_rows_carry_cache_fields_only_when_known(tmp_path: Path) -> None:
    from opsb_core.agents.manager import AgentMeshManager
    ws = tmp_path / "ws"
    ws.mkdir()
    with mock.patch.dict(os.environ, {"PARHELION_HOME": str(tmp_path / "home")}):
        mgr = AgentMeshManager()
        mgr._cached_doctor = lambda: {"active_driver": "codex", "diagnostics": []}
        mgr._observed_driver_sessions = lambda w, d: ({}, [])
        store_ = mgr._store(ws)
        store_.create_agent({"agent_id": "dee-eeeeee", "driver": "deepseek", "problem": "P1", "worktree": str(ws)})
        rows = mgr.build_hot_mesh(ws)["workers"]
        assert rows and "cache_hit_pct" not in rows[0]
        fields = {"cache_hit_pct": 91.5, "input_miss_tokens": 10, "cache_read_tokens": 90, "last_miss_reason": "cold_start"}
        with mock.patch.object(mgr._usage_monitor, "fields", return_value={"dee-eeeeee": fields}):
            row = mgr.build_hot_mesh(ws)["workers"][0]
        assert {k: row[k] for k in fields} == fields


def _call(ws: Path, name: str = "parhelion_status") -> dict:
    from opsb_core.mcp.server import handle_jsonrpc_message
    return handle_jsonrpc_message({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                   "params": {"name": name, "arguments": {"problem": "P1"}}},
                                  ws, role="orchestrator", agent_id="orch-1")


def test_mcp_tool_calls_are_metered_and_failures_never_break_the_call(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    with mock.patch.dict(os.environ, {"PARHELION_HOME": str(tmp_path / "home")}):
        with mock.patch("opsb_core.mcp.server.call_tool", return_value={"ok": True, "text": "é" * 10}):
            ok = _call(ws)
        with mock.patch("opsb_core.mcp.server.call_tool", side_effect=RuntimeError("boom")):
            err = _call(ws)
        rows = mcp_meter.read_rows(ws)
        with mock.patch("opsb_core.mcp.server.call_tool", return_value={"ok": True}), \
                mock.patch.object(mcp_meter._cfg, "append_jsonl_locked", side_effect=OSError("disk full")):
            still = _call(ws)
    assert ok["result"]["isError"] is False and err["result"]["isError"] is True
    assert still["result"]["isError"] is False
    assert len(rows) == 2
    row = rows[0]
    assert set(row) == {"ts", "agent_id", "role", "tool", "args_bytes", "result_bytes", "ms", "is_error",
                        "spilled", "bound_bytes"}
    assert row["spilled"] is False and row["bound_bytes"] == row["result_bytes"]
    assert row["tool"] == "parhelion_status" and row["agent_id"] == "orch-1" and row["role"] == "orchestrator"
    assert row["args_bytes"] == len(json.dumps({"problem": "P1"}, separators=(",", ":")))
    assert row["result_bytes"] == len(ok["result"]["content"][0]["text"].encode("utf-8"))
    assert rows[1]["is_error"] is True


def test_mcp_meter_rotates_and_summarizes_hourly(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    with mock.patch.dict(os.environ, {"PARHELION_HOME": str(tmp_path / "home")}):
        for i in range(30):
            assert mcp_meter.record_call(ws, agent_id="w", role="worker", tool="t", args={"i": i},
                                         result_text="x" * (i + 1), ms=1.0, is_error=False, limit=600)
        base = store.metrics_dir(ws)
        assert (base / mcp_meter.ROTATED_NAME).is_file()
        assert (base / mcp_meter.LOG_NAME).stat().st_size <= 600 + 200
        rows = mcp_meter.read_rows(ws)
    assert 0 < len(rows) < 30  # older generations beyond .1 are dropped
    summary = mcp_meter.summarize([
        {"ts": "2026-01-02T03:10:00+00:00", "tool": "a", "result_bytes": b, "args_bytes": 2, "ms": 1.0, "is_error": False}
        for b in range(1, 101)] + [{"ts": "2026-01-02T04:00:00+00:00", "tool": "a", "result_bytes": 5}])
    hours = {(h["tool"], h["hour"]): h for h in summary["hourly"]}
    assert hours[("a", "2026-01-02T03")]["calls"] == 100
    assert hours[("a", "2026-01-02T03")]["result_bytes_p95"] == 95
    assert hours[("a", "2026-01-02T03")]["result_bytes_mean"] == 50.5
    assert summary["per_tool"]["a"]["calls"] == 101
