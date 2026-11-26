"""Tests for Parhelion Local MCP JSON-RPC 2.0 Server & 9 High-Signal Tools."""
from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core.commands.driver_mcp_cmd import register_driver_and_mcp_subcommands  # noqa: E402
from opsb_core.mcp import (  # noqa: E402
    MCP_PROTOCOL_VERSION,
    SERVER_INFO,
    call_tool,
    handle_mcp_http_request,
    list_tool_schemas,
    serve_stdio,
)


def _make_mcp_workspace(tmp_path: Path) -> tuple[Path, Path, str]:
    ws = tmp_path / "ws"
    op_dir = ws / "bench" / "PadV3Grad"
    (op_dir / "op_host").mkdir(parents=True)
    (op_dir / "op_host" / "tiling.cpp").write_text("// tiling\n")
    (op_dir / "op_kernel").mkdir(parents=True)
    (op_dir / "op_kernel" / "pad_v3_grad.cpp").write_text("// impl\n", encoding="utf-8")
    (ws / "results" / "scoring-evidence").mkdir(parents=True)
    (ws / "bench" / "problems.json").write_text(
        json.dumps([{"title": "PadV3Grad", "slug": "padv3grad", "problem_id": "p1", "difficulty": 3, "path": "bench/PadV3Grad"}]),
        encoding="utf-8",
    )
    (ws / "results" / "score-snapshot.json").write_text(
        json.dumps({"PadV3Grad": {"score": 42.77, "status": "Pass", "rank": 57, "public_times_us": [5.95, 5.95, 5.95, 5.95, 5.95]}}),
        encoding="utf-8",
    )
    (ws / "results" / "submit-queue.jsonl").write_text("", encoding="utf-8")
    (ws / "results" / "submit-queue-outcome.jsonl").write_text("", encoding="utf-8")

    subprocess.run(["git", "-C", str(op_dir), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(op_dir), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(op_dir), "config", "user.name", "Test User"], check=True)
    subprocess.run(["git", "-C", str(op_dir), "add", "."], check=True)
    subprocess.run(["git", "-C", str(op_dir), "commit", "-q", "-m", "baseline"], check=True)
    sha = subprocess.run(["git", "-C", str(op_dir), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    return ws, op_dir, sha


@pytest.fixture()
def isolated_mcp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    p_home = tmp_path / "parhelion_home"
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("PARHELION_HOME", str(p_home))
    return p_home


def test_refresh_guides_preserves_workspace_policy(isolated_mcp_env, tmp_path):
    from opsb_core.services.scaffold_service import scaffold_workspace, write_agent_guides
    ws = tmp_path / "contest"
    problem = dict(title="Demo", difficulty=3)
    scaffold_workspace(ws, contest_id="test", problems=[problem])
    manifest = ws / "opsb.workspace.json"
    meta = json.loads(manifest.read_text())
    meta["bench_strict_fp32"] = True
    meta["bench_observations"] = {"Demo": ["measured floor"]}
    meta["problems"][0]["custom_field"] = 17
    meta["frozen_oracles"]["Demo"] = "existing-pin"
    manifest.write_text(json.dumps(meta))
    before = manifest.read_bytes()
    write_agent_guides(ws, meta)
    assert manifest.read_bytes() == before
    scaffold_workspace(ws, contest_id="test", problems=[problem])
    assert json.loads(manifest.read_text()) == meta
    corr = json.loads((ws / "Demo/bench/cases_correctness.json").read_text())
    assert all(c["verified"] is False for c in corr["cases"])


def test_sprint_replay_and_candidate_feedback(isolated_mcp_env, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from opsb_core.agents.adapters import BaseWorkerAdapter
    from opsb_core.agents.manager import AgentMeshManager
    from opsb_core.config import ConfigRepository
    from opsb_core.daemon.server import OpsbDaemonRuntime
    from opsb_core.mcp.handlers_agents import bind_daemon_agent_manager
    ws, repo, sha = _make_mcp_workspace(tmp_path)
    second = repo.parent / "Other"
    subprocess.run(["git", "clone", "-q", str(repo), str(second)], check=True)
    manifest = repo.parent / "problems.json"
    manifest.write_text(json.dumps(json.loads(manifest.read_text()) + [{"title": "Other", "path": "bench/Other"}]))
    config = ConfigRepository.load()
    config["active_workspace"] = str(ws)
    ConfigRepository.save(config)
    class TestAdapter(BaseWorkerAdapter):
        def send_turn(self, prompt, on_event=None):
            return "experiment done"
    mgr = AgentMeshManager(adapter_factory=TestAdapter)
    driver = "deepseek"
    monkeypatch.setattr(mgr, "_cached_doctor", lambda: {"diagnostics": [
        dict(kind=driver, auth_ok=True, binary_path="dsh", installed=True)]})
    bind_daemon_agent_manager(mgr)
    def start(preview=False):
        return call_tool("parhelion_sprint_start", dict(driver=driver, enable_auto_submit=True, preview=preview, mode="full"), ws)
    try:
        assert not start(True)["auto_submit_enabled"]
        with ThreadPoolExecutor(2) as pool:
            first, replay = pool.map(lambda _: start(), range(2))
        assert first["ok"] and replay["ok"] and first["auto_submit_enabled"]
        slots = first["assignments"] + replay["assignments"]
        assert {s["state"] for s in slots} == {"started", "existing"}
        started = [s for s in slots if s["state"] == "started"]
        assert len(started) == 6 and len({s["agent_id"] for s in slots}) == 6
        owners = [s["agent_id"] for s in started if s["problem"] == "PadV3Grad"]
        assert len({mgr._store(ws).get_agent(a)["worktree"] for a in owners}) == 3
        author = next(s["agent_id"] for s in started if s["problem"] == "PadV3Grad" and s["axis"] == "calibration")
        peers = call_tool("parhelion_squad", {}, ws, role="worker", agent_id=author)["peers"]
        assert {p["agent_id"] for p in peers} == set(owners) - {author}
        other = next(s["agent_id"] for s in started if s["problem"] == "Other")
        with pytest.raises(ValueError, match="teammate"):
            call_tool("parhelion_squad", dict(recipient=other, message="test"), ws, role="worker", agent_id=author)
        payload = dict(recipient=owners[0], message="Repeat the small bucket", request_id="m1")
        assert not call_tool("parhelion_squad", payload, ws, role="worker", agent_id=author)["duplicate"]
        assert call_tool("parhelion_squad", payload, ws, role="worker", agent_id=author)["duplicate"]
        res = ws / "results" / "PadV3Grad" / sha
        res.mkdir(parents=True)
        (res / "result.json").write_text('{"exit_code": 0}')
        (res / "validation.log").write_text("EXTENDED_PASSED 40 cases\nEXTENDED_RC=0\n")
        gate = call_tool("parhelion_submit_gate", dict(problem="PadV3Grad", sha=sha, enqueue=True),
                         ws, role="worker", agent_id=owners[0])
        assert gate["enqueued"]
        record = json.loads((ws / "results/submit-queue.jsonl").read_text().splitlines()[-1])
        assert record["agent_id"] == owners[0]
        def judge(_cli, _ws, title, uploaded_sha, _note, **kwargs):
            receipt = dict(title=title, sha=uploaded_sha, submitted_at="2026-11-01T00:00:04Z",
                           status="Pass", score=45, queued_at=record["queued_at"], submission_id="sprint-test")
            (ws / "results/submit-queue-outcome.jsonl").write_text(json.dumps(receipt) + "\n")
            return {"ok": True}
        monkeypatch.setattr("opsb_core.daemon.server.spawn_jump_submit", judge)
        OpsbDaemonRuntime.dispatch_next(mock.Mock(), ws, {"submission_queue": [
            dict(kind="pending", gates=gate["gates"], title="PadV3Grad", sha=sha)]}, {})
        mgr.deliver_judgements(ws, 5)
        mgr.deliver_judgements(ws, 5)
        author_inbox = mgr.worker_inbox(ws, author)["messages"]
        calibration = [m for m in author_inbox if m["kind"] == "calibration"]
        assert len(calibration) == 1 and json.loads(calibration[0]["body"])["sha"] == sha
        mgr.worker_report(ws, author, kind="result", summary="Calibration checked")
        visible = call_tool("parhelion_agent_messages", {"limit": 999}, ws)["messages"]
        assert all(m["direction"] not in ("squad_to_worker", "squad_note") for m in visible)
        assert mgr._store(ws).get_agent(author)["unread_for_orch"] == 0
        mgr.worker_report(ws, author, kind="question", summary="Need administrator decision")
        assert mgr._store(ws).get_agent(author)["unread_for_orch"] == 1
        for owner in owners:
            inbox = call_tool("parhelion_agent_inbox", {}, ws, role="worker", agent_id=owner)
            results = [m for m in inbox["messages"] if m["kind"] == "judge_result"]
            assert len(results) == int(owner == owners[0])
        from opsb_core.agents.sprint import start_sprint
        observer = AgentMeshManager(adapter_factory=TestAdapter)
        monkeypatch.setattr(observer, "_cached_doctor", mgr._cached_doctor)
        store = observer._store(ws)
        assert len(store.list_agents(include_stopped=False, limit=-1)) == 6
        store.update_agent(author, branch="calibration-followup")
        assert all(s["state"] == "existing" for s in start_sprint(observer, ws, driver=driver, preview=True, mode="full")["assignments"])
        with pytest.raises(RuntimeError, match="owned by another"):
            observer._enqueue_to_worker(ws, store.get_agent(author), "follow-up")
        store.update_agent(author, status="error", error="insufficient balance")
        plan = start_sprint(observer, ws, driver=driver, mode="full")
        held = [s for s in plan["assignments"] if s["state"] == "blocked"]
        assert len(held) == 1 and held[0]["agent_id"] == author and held[0]["reason"] == "insufficient balance"
        assert len(store.list_agents(limit=-1)) == 6
    finally:
        mgr.stop_all()
        bind_daemon_agent_manager(None)


def test_calibration_selects_pre_submission_evidence(tmp_path):
    from opsb_core.agents.judge_feedback import calibration_evidence
    sha = "a" * 40
    row = dict(title="Demo", sha=sha, submission_id="s1", submitted_at="2026-11-01T02:00:00Z", status="Wrong Answer", score=0)
    for rid, hour, actual_sha in [("before", "01", sha), ("after", "03", sha), ("other", "00", "b"*40)]:
        dest = tmp_path / "results" / "Demo" / sha / "bench-runs" / rid / "report.json"
        dest.parent.mkdir(parents=True)
        dest.write_text(json.dumps(dict(run_id=rid, title="Demo", sha=actual_sha, formal=True,
            finished_at=f"2026-11-01T{hour}:00:00Z", cases=[dict(id="09", status="WA")])))
    message, paths = calibration_evidence(tmp_path, row)
    report = json.loads(message)["local"]["formal"]
    assert report["run_id"] == "before" and report["failed_cases"] == ["09"]
    assert len(paths) == 1 and (tmp_path / paths[0]).exists()


def test_worker_mcp_case_plan_and_role(isolated_mcp_env, tmp_path):
    ws, _, sha = _make_mcp_workspace(tmp_path)
    plan = call_tool("parhelion_test_plan", dict(problem="PadV3Grad", cases=["01", "03"]), ws, role="worker")
    assert plan["sha"] == sha and not plan["executed"] and plan["level"] == "standard"
    assert plan["command"][-6:] == ["--level", "standard", "--case", "01", "--case", "03"]
    for name, value in (("profile", "p.json"), ("experiment", "paired"), ("timing", "all"), ("repeats", 3), ("bucket", "small")):
        with pytest.raises(ValueError, match="Workers select a level"):
            call_tool("parhelion_test_plan", {"problem": "PadV3Grad", name: value}, ws, role="worker")
    orch = call_tool("parhelion_test_plan", dict(problem="PadV3Grad", cases=["01"], timing="all", repeats=3,
                     experiment="paired"), ws)
    assert orch["command"][-8:] == ["--case", "01", "--experiment", "paired", "--timing", "all", "--repeats", "3"]
    assert list_tool_schemas(role="typo") == []
    with pytest.raises(ValueError, match="Unknown problem"):
        call_tool("parhelion_test_plan", {"problem": "missing"}, ws, role="worker")


def test_mcp_stdio_handshake_and_error_handling(isolated_mcp_env: Path, tmp_path: Path) -> None:
    ws, _, _ = _make_mcp_workspace(tmp_path)

    requests = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": MCP_PROTOCOL_VERSION}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "parhelion_status", "arguments": {"include_npu": False}}}),
        json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nonexistent_tool", "arguments": {}}}),
        json.dumps({"jsonrpc": "2.0", "id": 5, "method": "unknown/method"}),
        "{not-valid-json",
    ]) + "\n"

    stdout = io.StringIO()
    stderr = io.StringIO()
    rc = serve_stdio(ws, stdin=io.StringIO(requests), stdout=stdout, stderr=stderr)
    assert rc == 0

    lines = [json.loads(ln) for ln in stdout.getvalue().splitlines() if ln.strip()]
    # notifications/initialized emits no response, so 6 responses total; replies may arrive out of order
    assert len(lines) == 6
    by_id = {resp.get("id"): resp for resp in lines}
    init_resp, list_resp, status_resp, bad_tool_resp, bad_method_resp, parse_err_resp = (
        by_id[1], by_id[2], by_id[3], by_id[4], by_id[5], by_id[None])

    assert init_resp["id"] == 1
    assert init_resp["result"]["protocolVersion"] == MCP_PROTOCOL_VERSION
    assert init_resp["result"]["serverInfo"] == SERVER_INFO
    assert init_resp["result"]["capabilities"] == {"tools": {}}

    tools = list_resp["result"]["tools"]
    assert len(tools) == 26  # +3 ledger (P06), +3 escape (P07)
    assert {t["name"] for t in tools} == {t["name"] for t in list_tool_schemas()}

    assert status_resp["result"]["isError"] is False
    st_payload = json.loads(status_resp["result"]["content"][0]["text"])
    assert st_payload["problems"]["rows"][0][st_payload["problems"]["columns"].index("title")] == "PadV3Grad"
    assert st_payload["npu"]["skipped"] is True

    assert bad_tool_resp["error"]["code"] == -32602
    assert "Unknown tool" in bad_tool_resp["error"]["message"]
    assert bad_method_resp["error"]["code"] == -32601
    assert parse_err_resp["error"]["code"] == -32700
    assert "parse error" in stderr.getvalue().lower()


def test_mcp_http_handler(isolated_mcp_env: Path, tmp_path: Path) -> None:
    ws, _, _ = _make_mcp_workspace(tmp_path)

    code_bad, body_bad = handle_mcp_http_request(b"not-json", ws)
    assert code_bad == 400
    assert body_bad["error"]["code"] == -32700

    code_notif, body_notif = handle_mcp_http_request(
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode("utf-8"),
        ws,
    )
    assert code_notif == 202
    assert body_notif is None

    code_ok, body_ok = handle_mcp_http_request(
        json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "parhelion_bench_diagnose", "arguments": {"problem": "PadV3Grad"}}}).encode("utf-8"),
        ws,
    )
    assert code_ok == 200
    assert body_ok["result"]["isError"] is False
    diag = json.loads(body_ok["result"]["content"][0]["text"])
    assert diag["title"] == "PadV3Grad"
    assert len(diag["top_headroom_strata"]) >= 1


def test_evidence_gate_kb_record_feedback_and_query(isolated_mcp_env: Path, tmp_path: Path) -> None:
    ws, _, sha = _make_mcp_workspace(tmp_path)

    # 1. Evidence-gate rejects hallucinated/unbacked writes via MCP tools/call
    for bad_args in [
        {"pillar": "community", "title": "Unbacked", "summary": "No sha", "problem": "PadV3Grad", "sha": "bad", "verdict": "win", "delta_raw": 2.0},
        {"pillar": "community", "title": "Unbacked", "summary": "No metric", "problem": "PadV3Grad", "sha": sha, "verdict": "win"},
        {"pillar": "invalid", "title": "BadPillar", "summary": "x", "problem": "PadV3Grad", "sha": sha, "verdict": "win", "delta_raw": 1.0},
    ]:
        _, resp = handle_mcp_http_request(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "parhelion_kb_record", "arguments": bad_args}}).encode("utf-8"),
            ws,
        )
        assert resp["result"]["isError"] is True
        err_obj = json.loads(resp["result"]["content"][0]["text"])
        assert "Evidence gate" in err_obj["error"] or "Invalid" in err_obj["error"]

    # 2. Valid evidence-backed write succeeds (unverified community card with delta_raw < 1.5 initially)
    _, rec_resp = handle_mcp_http_request(
        json.dumps({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "parhelion_kb_record",
                "arguments": {
                    "pillar": "community",
                    "title": "Zero TPipe Static UB",
                    "summary": "Eliminate TPipe ctor overhead on small shapes.",
                    "problem": "PadV3Grad",
                    "sha": sha,
                    "verdict": "win",
                    "bottlenecks": ["cold-launch-tax"],
                    "regimes": ["S_SMALL_CORE"],
                    "portability": "portable",
                    "delta_raw": 0.9,
                },
            },
        }).encode("utf-8"),
        ws,
    )
    assert rec_resp["result"]["isError"] is False
    rec_data = json.loads(rec_resp["result"]["content"][0]["text"])
    entry_id = rec_data["entry"]["id"]
    assert rec_data["entry"]["status"] == "unverified"

    # 3. Query returns the recorded entry
    q_res = call_tool("parhelion_kb_query", {"bottlenecks": ["cold-launch-tax"], "digest": True, "limit": 8}, ws)
    assert any(e["id"] == entry_id for e in q_res["entries"])
    assert "digest" in q_res

    # 4. Feedback with delta_raw >= 1.5 promotes unverified -> verified
    fb_res = call_tool(
        "parhelion_kb_feedback",
        {"entry_id": entry_id, "verdict": "win", "problem": "PadV3Grad", "sha": sha, "delta_raw": 3.4},
        ws,
    )
    assert fb_res["entry"]["status"] == "verified"
    assert fb_res["entry"]["wins"] == 2

    # Audit log recorded
    audit_lines = (isolated_mcp_env / "kb-evolution.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(audit_lines) == 2

    # 5. Snippet record writes .h file, and 3 fails demote entry to anti-pattern
    snip_res = call_tool(
        "parhelion_kb_record",
        {
            "pillar": "snippets",
            "title": "Bad Unaligned Copy",
            "summary": "Unaligned DataCopy fails on odd tails",
            "problem": "PadV3Grad",
            "sha": sha,
            "verdict": "fail",
            "code": "// test snippet\n",
            "note": "profiler saw regressed runtime",
        },
        ws,
    )
    snip_id = snip_res["entry"]["id"]
    assert (isolated_mcp_env / "snippets" / "code" / f"{snip_id}.h").is_file()
    call_tool("parhelion_kb_feedback", {"entry_id": snip_id, "verdict": "fail", "problem": "PadV3Grad", "sha": sha, "delta_raw": -2.0}, ws)
    demoted = call_tool("parhelion_kb_feedback", {"entry_id": snip_id, "verdict": "fail", "problem": "PadV3Grad", "sha": sha, "delta_raw": -1.5}, ws)
    assert demoted["entry"]["status"] == "anti-pattern"
    q_ap = call_tool("parhelion_kb_query", {"include_anti_patterns": True, "limit": 8}, ws)
    assert any(ap["id"] == snip_id for ap in q_ap["anti_patterns"])


def test_escape_lock_blocks_submit_gate(isolated_mcp_env: Path, tmp_path: Path) -> None:
    ws, _, sha = _make_mcp_workspace(tmp_path)
    res_dir = ws / "results" / "PadV3Grad" / sha
    res_dir.mkdir(parents=True)
    (res_dir / "result.json").write_text(json.dumps({"exit_code": 0, "sha": sha}), encoding="utf-8")
    (res_dir / "validation.log").write_text("EXTENDED_PASSED 40 cases\nEXTENDED_RC=0\n", encoding="utf-8")
    (ws / "results" / "PadV3Grad" / "calibration.json").write_text(
        json.dumps({"escape_lock": True, "escape_sha": "deadbee", "strata": []}),
        encoding="utf-8",
    )

    (ws / "results" / "submit-queue-outcome.jsonl").write_text("".join(
        json.dumps({"title": "PadV3Grad", "status": "Wrong Answer", "submission_id": f"sid-{i}",
                    "sha": "deadbee", "finished_at": f"2026-01-01T00:00:0{i}Z"}) + "\n"
        for i in range(3)))

    blocked = call_tool("parhelion_submit_gate", {"problem": "PadV3Grad", "sha": sha, "enqueue": True}, ws)
    assert blocked["gates"]["safety_ok"] is False
    assert blocked["enqueued"] is False
    assert any("ESCAPE_LOCK" in r for r in blocked["reasons"])


def test_lazy_fast_feedback_and_test_and_submit_gate(isolated_mcp_env: Path, tmp_path: Path) -> None:
    ws, _, sha = _make_mcp_workspace(tmp_path)

    # Real merged Track 1 fast_feedback integration (`available=True`)
    pmu_real = call_tool("parhelion_pmu_card", {"problem": "PadV3Grad"}, ws)
    assert pmu_real["available"] is True
    assert pmu_real["title"] == "PadV3Grad"
    assert "dominant_bottleneck" in pmu_real
    assert "markdown_card" in pmu_real
    c3_real = call_tool("parhelion_elf_cold3_check", {"problem": "PadV3Grad"}, ws)
    assert c3_real["available"] is True
    assert c3_real["title"] == "PadV3Grad"
    assert "elf_guard" in c3_real and "cold3_probe" in c3_real

    # Fallback when Track 1 is unavailable (`available=False`)
    with mock.patch.dict(sys.modules, {"opsb_core.services.fast_feedback_service": None}):
        pmu_fb = call_tool("parhelion_pmu_card", {"problem": "PadV3Grad"}, ws)
        assert pmu_fb["available"] is False
        c3_fb = call_tool("parhelion_elf_cold3_check", {"problem": "PadV3Grad"}, ws)
        assert c3_fb["available"] is False

    # Lazy delegation with mocked Track 1 responses
    fake_ff = mock.MagicMock()
    fake_ff.build_pmu_torch_card.return_value = {"title": "PadV3Grad", "dominant_bottleneck": "mte2-bound", "markdown_card": "card"}
    fake_ff.evaluate_elf_guard.return_value = {"status": "warn", "alerts": []}
    fake_ff.evaluate_cold3_probe.return_value = {"status": "redline", "bimodal_spike_detected": True}
    with mock.patch.dict(sys.modules, {"opsb_core.services.fast_feedback_service": fake_ff}):
        pmu_ok = call_tool("parhelion_pmu_card", {"problem": "PadV3Grad"}, ws)
        assert pmu_ok["available"] is True
        assert pmu_ok["dominant_bottleneck"] == "mte2-bound"
        elf_ok = call_tool("parhelion_elf_cold3_check", {"problem": "PadV3Grad"}, ws)
        assert elf_ok["status"] == "redline"

    # Dry-run test plan (never runs SSH)
    plan = call_tool("parhelion_test_plan", {"problem": "PadV3Grad", "mode": "fast"}, ws)
    assert plan["executed"] is False
    assert plan["lane"]["queue"] == "Q0"
    assert plan["command"][-2:] == ["--level", "smoke"] and "--fast" not in plan["command"]

    # Submit gate: blocks when no NPU validation result exists
    gate_fail = call_tool("parhelion_submit_gate", {"problem": "PadV3Grad", "sha": sha, "enqueue": True}, ws)
    assert gate_fail["gates"]["safety_ok"] is False
    assert gate_fail["enqueued"] is False

    # Write passing local NPU result & enqueue
    res_dir = ws / "results" / "PadV3Grad" / sha
    res_dir.mkdir(parents=True)
    (res_dir / "result.json").write_text(json.dumps({"exit_code": 0}), encoding="utf-8")
    (res_dir / "validation.log").write_text("EXTENDED_PASSED 40 cases\nEXTENDED_RC=0\n", encoding="utf-8")

    gate_ok = call_tool(
        "parhelion_submit_gate",
        {"problem": "PadV3Grad", "sha": sha, "note": "mean ~58.0", "enqueue": True},
        ws,
    )
    assert gate_ok["gates"]["all_pass"] is True
    assert gate_ok["enqueued"] is True
    q_txt = (ws / "results" / "submit-queue.jsonl").read_text(encoding="utf-8")
    assert sha in q_txt


def test_mcp_cli_subcommands(isolated_mcp_env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws, _, _ = _make_mcp_workspace(tmp_path)
    parser = argparse.ArgumentParser(prog="opsb")
    sub = parser.add_subparsers(dest="cmd", required=True)
    register_driver_and_mcp_subcommands(sub)

    args = parser.parse_args(["mcp", "list-tools", "--json"])
    assert args.func(args) == 0
    tools_out = json.loads(capsys.readouterr().out)
    assert len(tools_out["tools"]) == 26  # +3 ledger (P06), +3 escape (P07)

    args = parser.parse_args(["mcp", "call", "parhelion_status", "--args-json", '{"include_npu":false}', "--workspace", str(ws)])
    assert args.func(args) == 0
    call_out = json.loads(capsys.readouterr().out)
    assert call_out["contest"]["problem_count"] == 1


def _out_of_order_requests() -> str:
    return "".join(json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                               "params": {"name": "parhelion_status", "arguments": {}}}) + "\n" for i in (1, 2))


def _slow_first(msg):
    import time
    if msg["id"] == 1:
        time.sleep(1.0)
    return {"jsonrpc": "2.0", "id": msg["id"], "result": {"pad": "x" * 20000, "id": msg["id"]}}


@pytest.mark.parametrize("worker", [False, True])
def test_mcp_stdio_dispatches_concurrently(isolated_mcp_env: Path, tmp_path: Path, monkeypatch, worker) -> None:
    import time
    from opsb_core.mcp import server as mcp_server

    seen: list[str] = []
    if worker:
        monkeypatch.setattr(mcp_server, "forward_worker_request",
                            lambda ws, aid, msg: (seen.append(aid), _slow_first(msg))[1])
    else:
        monkeypatch.setattr(mcp_server, "handle_mcp_payload", lambda msg, **kw: _slow_first(msg))
    stdout = io.StringIO()
    started = time.monotonic()
    rc = serve_stdio(tmp_path, role="worker" if worker else "orchestrator", agent_id="w-1" if worker else None,
                     stdin=io.StringIO(_out_of_order_requests()), stdout=stdout, stderr=io.StringIO())
    assert rc == 0 and time.monotonic() - started < 1.8
    replies = [json.loads(ln) for ln in stdout.getvalue().splitlines()]
    assert [r["id"] for r in replies] == [2, 1]
    assert all(r["result"]["pad"] == "x" * 20000 for r in replies)
    if worker:
        assert seen == ["w-1", "w-1"]
