"""Tests for the asynchronous test-run registry and the non-blocking MCP test tools."""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core.mcp import call_tool  # noqa: E402
from opsb_core.mcp import handlers_bench  # noqa: E402
from opsb_core.services import test_runs  # noqa: E402

DIGEST = {
    "level": "standard", "suite_version": 3, "formal": True,
    "summary": {"total": 15, "passed": 15, "counts": {"PASS": 15}},
    "failures": [],
    "timing": {"eligible": True, "source": "event_warm", "warmup": 2, "repeats": 5,
               "cases": {"01": {"min_us": 9.2, "median_us": 9.4, "cold_us": 558.8},
                         "02": {"min_us": 4.1, "median_us": 4.3, "cold_us": 300.0}}},
    "public": None,
    "baseline": {"sha": "abc1234", "run_id": "r0", "geomean_speedup": 1.07, "cases": {"01": 1.12}},
    "hint": "",
}

FAKE_OPSB = textwrap.dedent('''\
    #!{python}
    """Fake `opsb test` following the documented stdout protocol (OPSB_RUN_STARTED ... final JSON)."""
    import json, os, sys, time
    from pathlib import Path
    ctl = Path(os.environ["FAKE_OPSB_DIR"])
    argv = sys.argv[1:]
    run_id = "run-%d" % os.getpid()
    run_dir = ctl / "bench-runs" / run_id
    run_dir.mkdir(parents=True)
    with (ctl / "invocations.jsonl").open("a") as fh:
        fh.write(json.dumps({{"argv": argv, "role": os.environ.get("PARHELION_MCP_ROLE"), "pid": os.getpid()}}) + "\\n")
    level = argv[argv.index("--level") + 1] if "--level" in argv else "standard"
    print("OPSB_RUN_STARTED " + json.dumps({{"run_id": run_id, "dir": str(run_dir), "level": level, "selected": 3}}), flush=True)
    with (run_dir / "events.jsonl").open("w") as ev:
        ev.write(json.dumps({{"event": "launch", "run_id": run_id}}) + "\\n")
        ev.write(json.dumps({{"event": "device_batch", "run_id": run_id, "wait_sec": 1.5}}) + "\\n")
        ev.write(json.dumps({{"event": "case", "run_id": run_id, "id": "01", "status": "PASS"}}) + "\\n")
    print("compiling...", flush=True)
    deadline = time.time() + 20
    while not (ctl / "release").exists() and time.time() < deadline:
        time.sleep(0.02)
    code = int(os.environ.get("FAKE_OPSB_EXIT", "0"))
    digest = json.loads((ctl / "digest.json").read_text())
    print(json.dumps({{"run_id": run_id, "exit_code": code, "level": level, "formal": True,
                      "summary": digest["summary"], "digest": digest, "report": str(run_dir / "report.json")}}), flush=True)
    sys.exit(code)
''')


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PARHELION_HOME", str(tmp_path / "parhelion_home"))
    monkeypatch.setattr(test_runs, "POLL_SEC", 0.05)
    ws = tmp_path / "ws"
    repo = ws / "bench" / "Demo"
    repo.mkdir(parents=True)
    (repo / "kernel.cpp").write_text("// v1\n")
    (ws / "bench" / "problems.json").write_text(json.dumps([{"title": "Demo", "path": "bench/Demo"}]))
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "baseline kernel")
    (repo / "kernel.cpp").write_text("// v2\n")
    _git(repo, "commit", "-q", "-am", "unroll inner loop")
    sha = _git(repo, "rev-parse", "HEAD")

    ctl = tmp_path / "fake"
    ctl.mkdir()
    (ctl / "digest.json").write_text(json.dumps(DIGEST))
    script = ctl / "opsb"
    script.write_text(FAKE_OPSB.format(python=sys.executable))
    script.chmod(0o755)
    monkeypatch.setenv("FAKE_OPSB_DIR", str(ctl))
    monkeypatch.setenv(test_runs.CMD_OVERRIDE_ENV, str(script))
    yield {"ws": ws.resolve(), "repo": repo, "sha": sha, "ctl": ctl}
    (ctl / "release").touch()
    for rec in test_runs._all_records(ws.resolve()):
        if rec.get("status") == "running" and isinstance(rec.get("pid"), int):
            try:
                os.killpg(rec["pid"], 9)
            except OSError:
                pass


def _invocations(ctl: Path) -> list[dict]:
    path = ctl / "invocations.jsonl"
    return [json.loads(ln) for ln in path.read_text().splitlines()] if path.exists() else []


def _wait_for(pred, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def test_submit_join_wait_reuse_and_remeasure(env):
    ws, sha, ctl = env["ws"], env["sha"], env["ctl"]
    started = time.monotonic()
    first = test_runs.submit(ws, problem="demo", sha=sha[:7], level="standard", agent_id="w-1", role="worker")
    assert time.monotonic() - started < 2.0
    assert first["status"] == "running" and first["problem"] == "Demo" and first["sha7"] == sha[:7]
    assert len(first["request_id"]) == 32

    again = test_runs.submit(ws, problem="Demo", sha=sha, level="standard", agent_id="w-2", role="worker")
    assert again["request_id"] == first["request_id"] and again["joined"] is True

    _wait_for(lambda: len(_invocations(ctl)) == 1)
    inv = _invocations(ctl)[0]
    assert inv["role"] == "worker"
    assert inv["argv"][:7] == ["test", "Demo", "--workspace", str(ws), "--sha", sha, "--level"]
    assert inv["argv"][7] == "standard"

    progress = test_runs.wait(ws, first["request_id"], 1)
    assert progress["status"] == "running"
    assert progress["progress"] == {"phase": "case", "cases_done": 1, "selected": 3, "wait_sec": 1.5}

    (ctl / "release").touch()
    done = test_runs.wait(ws, first["request_id"], 10)
    assert done["status"] == "finished" and done["exit_code"] == 0
    assert done["digest"] == DIGEST
    assert done["run_id"].startswith("run-") and done["report"].endswith("report.json")
    record = test_runs._load(ws, first["request_id"])
    assert record["observed"] is True and record["agent_id"] == "w-1" and record["finished_at"]
    assert len(json.dumps(done, ensure_ascii=False, separators=(",", ":")).encode()) <= test_runs.VIEW_LIMIT

    reused = test_runs.submit(ws, problem="Demo", sha=sha, level="standard")
    assert reused["request_id"] == first["request_id"] and reused["reused"] is True
    fresh = test_runs.submit(ws, problem="Demo", sha=sha, level="standard", remeasure=True)
    assert fresh["request_id"] != first["request_id"]
    final = test_runs.wait(ws, fresh["request_id"], 10)
    assert final["status"] == "finished"
    assert "--remeasure" in _invocations(ctl)[-1]["argv"]
    assert _invocations(ctl)[-1]["role"] is None


def test_failed_exit_and_build_only_level(env, monkeypatch):
    ws, sha, ctl = env["ws"], env["sha"], env["ctl"]
    (ctl / "release").touch()
    monkeypatch.setenv("FAKE_OPSB_EXIT", "1")
    view = test_runs.submit(ws, problem="Demo", sha=sha, build_only=True)
    assert view["level"] == "build"
    done = test_runs.wait(ws, view["request_id"], 10)
    assert done["status"] == "failed" and done["exit_code"] == 1
    assert done["error"] and len(done["error"]) <= test_runs.ERROR_LIMIT
    assert "--build-only" in _invocations(ctl)[-1]["argv"]
    # A failed run is never reused
    again = test_runs.submit(ws, problem="Demo", sha=sha, build_only=True)
    assert again["request_id"] != view["request_id"]
    test_runs.wait(ws, again["request_id"], 10)

    monkeypatch.setenv("FAKE_OPSB_EXIT", "2")
    wa = test_runs.wait(ws, test_runs.submit(ws, problem="Demo", sha=sha, level="smoke")["request_id"], 10)
    assert wa["status"] == "finished" and wa["exit_code"] == 2


def test_refresh_finalizes_when_reaper_thread_is_lost(env):
    ws, sha = env["ws"], env["sha"]
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    rid = "a" * 32
    log = test_runs.registry_dir(ws) / f"{rid}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("OPSB_RUN_STARTED {\"run_id\": \"r9\"}\nnoise\n"
                   + json.dumps({"run_id": "r9", "exit_code": 0, "digest": DIGEST, "report": "/x/report.json"}) + "\n")
    test_runs._write(ws, {"request_id": rid, "problem": "Demo", "sha": sha, "level": "smoke", "cases": [],
                          "build_only": False, "status": "running", "pid": dead.pid, "log": str(log),
                          "agent_id": "w-9", "delivered": False, "created_at": "2026-01-01T00:00:00+00:00"})
    rec = test_runs.refresh(ws, rid)
    assert rec["status"] == "finished" and rec["run_id"] == "r9" and rec["digest"] == DIGEST

    pending = test_runs.list_undelivered(ws)
    assert [r["request_id"] for r in pending] == [rid]
    line = test_runs.compact_line(pending[0])
    assert line == f"Demo {sha[:7]} smoke finished 15/15 · 慢例 01 9.2µs · 相对基线 ×1.07"
    test_runs.mark_delivered(ws, rid)
    assert test_runs.list_undelivered(ws) == []


def test_cancel_kills_process_group(env):
    ws, sha = env["ws"], env["sha"]
    view = test_runs.submit(ws, problem="Demo", sha=sha, cases=["03", "01"], agent_id="w-1", role="worker")
    pid = test_runs._load(ws, view["request_id"])["pid"]
    _wait_for(lambda: _invocations(env["ctl"]))
    assert ["--case", "03", "--case", "01"] == _invocations(env["ctl"])[0]["argv"][8:12]
    out = test_runs.cancel(ws, view["request_id"])
    assert out["status"] == "cancelled"
    with pytest.raises(ProcessLookupError):
        os.killpg(pid, 0)
    time.sleep(0.2)  # the reaper thread must not overwrite the cancellation
    assert test_runs._load(ws, view["request_id"])["status"] == "cancelled"
    assert test_runs.cancel(ws, view["request_id"])["status"] == "cancelled"


def test_invalid_inputs_list_recent_commits(env):
    ws = env["ws"]
    with pytest.raises(ValueError) as err:
        test_runs.submit(ws, problem="Demo", sha="deadbeefdeadbeef")
    msg = str(err.value)
    assert "unroll inner loop" in msg and "baseline kernel" in msg and env["sha"][:7] in msg
    with pytest.raises(ValueError, match="level"):
        test_runs.submit(ws, problem="Demo", level="exhaustive")
    with pytest.raises(ValueError, match="cases"):
        test_runs.submit(ws, problem="Demo", cases=[""])
    with pytest.raises(ValueError, match="Unknown test request"):
        test_runs.wait(ws, "b" * 32, 1)


def test_gc_removes_old_records_hourly(env):
    ws = env["ws"]
    old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=4)).isoformat()
    for rid, finished in (("c" * 32, old), ("d" * 32, datetime.datetime.now(datetime.timezone.utc).isoformat())):
        test_runs._write(ws, {"request_id": rid, "status": "finished", "finished_at": finished})
        (test_runs.registry_dir(ws) / f"{rid}.log").write_text("x")
    assert test_runs.gc(ws) == 1
    assert not (test_runs.registry_dir(ws) / f"{'c' * 32}.log").exists()
    assert test_runs._read(ws, "d" * 32) is not None
    test_runs._write(ws, {"request_id": "e" * 32, "status": "failed", "finished_at": old})
    assert test_runs.gc(ws) == 0  # throttled to once per hour


def test_public_view_and_compact_line_are_bounded():
    big = {**DIGEST, "timing": {**DIGEST["timing"], "cases": {f"{i:02d}": {"min_us": float(i), "median_us": 1.0,
           "cold_us": 2.0, "note": "x" * 80} for i in range(20)}}}
    view = test_runs.public_view({"request_id": "f" * 32, "problem": "Demo", "sha": "1" * 40, "status": "finished",
                                  "digest": big, "error": "e" * 5000, "reason": "r" * 3000})
    assert len(json.dumps(view, ensure_ascii=False, separators=(",", ":")).encode()) <= test_runs.VIEW_LIMIT
    line = test_runs.compact_line({"problem": "P" * 700, "sha": "1" * 40, "status": "failed", "error": "boom"})
    assert len(line) <= 600
    failed = test_runs.compact_line({"problem": "Demo", "sha": "1" * 40, "level": "smoke", "status": "failed",
                                     "error": "line one\nssh: connect refused"})
    assert failed == "Demo 1111111 smoke failed ?/? · ssh: connect refused"


def test_mcp_test_tools_roles_and_alias(env):
    ws, sha, ctl = env["ws"], env["sha"], env["ctl"]
    sub = call_tool("parhelion_test", {"problem": "Demo", "level": "smoke"}, ws, role="worker", agent_id="w-1")
    assert sub["status"] == "running"
    assert test_runs._load(ws, sub["request_id"])["agent_id"] == "w-1"
    with pytest.raises(PermissionError, match="own"):
        call_tool("parhelion_test_cancel", {"request_id": sub["request_id"]}, ws, role="worker", agent_id="w-2")
    assert call_tool("parhelion_test_cancel", {"request_id": sub["request_id"]}, ws,
                     role="worker", agent_id="w-1")["status"] == "cancelled"

    orch = call_tool("parhelion_test", {"problem": "Demo", "cases": ["01"]}, ws)
    assert test_runs._load(ws, orch["request_id"])["agent_id"] is None
    (ctl / "release").touch()
    waited = call_tool("parhelion_test_wait", {"request_id": orch["request_id"], "wait_sec": 10}, ws)
    assert waited["status"] == "finished" and waited["digest"] == DIGEST

    legacy = call_tool("parhelion_test_plan", {"problem": "Demo", "mode": "fast", "execute": True}, ws,
                       role="worker", agent_id="w-1")
    assert legacy["deprecated"] == "use parhelion_test + parhelion_test_wait"
    assert legacy["status"] == "finished" and legacy["level"] == "smoke"
    assert _invocations(ctl)[-1]["argv"][7] == "smoke"
    suite_all = call_tool("parhelion_test_plan", {"problem": "Demo", "suite": "all"}, ws)
    assert suite_all["level"] == "standard" and suite_all["command"][-2:] == ["--level", "standard"]
    with pytest.raises(ValueError, match="Workers select a level"):
        call_tool("parhelion_test_plan", {"problem": "Demo", "profile": "x.json", "execute": True}, ws,
                  role="worker", agent_id="w-1")
    with pytest.raises(ValueError, match="preview-only"):
        call_tool("parhelion_test_plan", {"problem": "Demo", "timing": "all", "execute": True}, ws)


def _snapshot(count: int) -> dict:
    titles = ["NeighborList", "RevINNorm", "PadV3Grad", "ConcatD", "SoftmaxCrossEntropyGrad", "LayerNormV4",
              "ScatterElementsV2", "GroupedMatmulAdd", "RotaryPositionEmbedding", "TopKV3", "MoeGatingTopKSoftmax",
              "DynamicQuantV2", "ApplyAdamWV2", "IndexPutWithSort"]
    problems = [{
        "title": titles[i % len(titles)], "difficulty": 3, "status": "Pass", "current_score": 42.7712345 + i,
        "best_score": 45.1098765 + i, "leader_score": 61.2345678 + i, "rank": 57 + i, "wspt_tag": "Q1",
        "head_sha": f"{i:02d}" + "abcdef0123456789abcdef0123456789abcdef"[:38],
        "best_sha": f"{i:02d}" + "0123456789abcdef0123456789abcdef012345"[:38],
        "calibration": {"escape_lock": False, "strata": [{"headroom_pts": 1.0}] * 10, "coverage_grid": [[1] * 20] * 5},
    } for i in range(count)]
    queue = [{"title": p["title"], "sha": p["head_sha"], "status": "pending", "kind": "pending",
              "estimated_score": 50.123456, "delta_expected": 2.3456789, "gates": {"all_pass": True},
              "note": "n" * 200} for p in problems]
    return {
        "sampled_at": "2026-11-25T12:00:00+00:00",
        "contest": {"title": "Kernel Contest Round 2", "workspace": "/Users/someone/contests/round-2-workspace",
                    "weighted_score": 512.3, "max_weighted_score": 1400, "raw_score": 600.1,
                    "passing_count": 12, "problem_count": count},
        "scheduler": {"rate_ready": True, "cooldown_remaining_sec": 0, "used_today_24h": 37, "daily_quota_limit": 100,
                      "closing_reserve": 10, "available_normal_shots": 53, "noise_floor_delta": 1.9, "actionable_count": 4},
        "problems": problems, "submission_queue": queue,
    }


def test_status_default_is_compact(tmp_path, monkeypatch):
    monkeypatch.setenv("PARHELION_HOME", str(tmp_path / "ph"))
    monkeypatch.setattr(handlers_bench, "compute_workspace_snapshot", lambda ws, cfg: _snapshot(14))
    monkeypatch.setattr(handlers_bench, "invoke_bridge", lambda *a, **k: None)
    out = handlers_bench.tool_parhelion_status({}, tmp_path)
    size = len(json.dumps(out, ensure_ascii=False, separators=(",", ":")).encode())
    assert size <= 2048, size
    assert "queue_preview" not in out
    cols = out["problems"]["columns"]
    assert cols == ["title", "status", "current_score", "best_score", "leader_score", "rank", "head_sha", "best_sha"]
    assert len(out["problems"]["rows"]) == 14 and out["problems"]["rows"][0][cols.index("head_sha")] == "00abcde"

    with_queue = handlers_bench.tool_parhelion_status({"include_queue": True}, tmp_path)
    assert len(with_queue["queue_preview"]) == 5
    assert set(with_queue["queue_preview"][0]) == {"title", "sha7", "status", "estimated_score", "delta_expected"}

    verbose = handlers_bench.tool_parhelion_status({"verbose": True}, tmp_path)
    assert verbose["problems"][0]["title"] == "NeighborList" and len(verbose["queue_preview"]) == 12
    one = handlers_bench.tool_parhelion_status({"problem": "revinnorm"}, tmp_path)
    assert [r[0] for r in one["problems"]["rows"]] == ["RevINNorm"]


def test_diagnose_trims_residuals_and_grid(tmp_path, monkeypatch):
    snap = _snapshot(1)
    snap["problems"][0]["calibration"]["high_residual_events"] = [{"i": i} for i in range(8)]
    monkeypatch.setenv("PARHELION_HOME", str(tmp_path / "ph"))
    monkeypatch.setattr(handlers_bench, "compute_workspace_snapshot", lambda ws, cfg: snap)
    compact = handlers_bench.tool_parhelion_bench_diagnose({"problem": "NeighborList"}, tmp_path)
    assert compact["high_residual_events"] == [{"i": 5}, {"i": 6}, {"i": 7}] and "coverage_grid" not in compact
    full = handlers_bench.tool_parhelion_bench_diagnose({"problem": "NeighborList", "verbose": True}, tmp_path)
    assert full["coverage_grid"] == [[1] * 20] * 5


def test_kb_query_compact_by_default(tmp_path, monkeypatch):
    from opsb_core.mcp import handlers_kb

    class FakeKB:
        @staticmethod
        def query_kb(**kwargs):
            items = [{"id": f"HW-{i}", "pillar": "hardware", "title": f"Law {i}", "confidence": 0.8,
                      "bottlenecks": ["cold-launch-tax"], "regimes": ["S_SMALL"], "dtypes": [],
                      "summary": "s" * 2000, "evidence": [{"note": "n" * 500}] * 5, "code": "c" * 3000}
                     for i in range(kwargs["limit"])]
            return {"ok": True, "results": items, "entries": items, "anti_patterns": [], "result_count": len(items),
                    "anti_pattern_count": 0, "query": kwargs}

    monkeypatch.setattr(handlers_kb, "_load_triad_kb_module", lambda: FakeKB)
    out = handlers_kb.tool_parhelion_kb_query({"bottlenecks": ["cold-launch-tax"]}, tmp_path)
    assert out["result_count"] == 3 and len(out["results"]) == 3 and out["entries"] == out["results"]
    entry = out["results"][0]
    assert set(entry) == {"id", "pillar", "title", "confidence", "tags", "summary"}
    assert len(entry["summary"]) <= 600 and entry["tags"] == ["cold-launch-tax", "S_SMALL"]
    full = handlers_kb.tool_parhelion_kb_query({"full": True}, tmp_path)
    assert len(full["results"]) == 8 and "evidence" in full["results"][0]
