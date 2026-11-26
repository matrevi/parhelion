"""4.2 B: change-driven waits — hub revisions, coalescing, rate limit, caller scope, sources,
idle back-off, status not_modified, legacy wait shapes, local fallback and the daemon HTTP layer."""
from __future__ import annotations

import datetime
import http.client
import json
import os
import statistics
import sys
import threading
import time
import uuid
from pathlib import Path
from unittest import mock

import pytest

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core.agents import event_hub as eh  # noqa: E402
from opsb_core.agents.manager import AgentMeshManager  # noqa: E402
from opsb_core.mcp import handlers_agents, handlers_bench  # noqa: E402
from opsb_core.mcp.handlers_agents import bind_daemon_agent_manager  # noqa: E402
from opsb_core.mcp.tools import call_tool  # noqa: E402
from opsb_core.services import test_runs  # noqa: E402


@pytest.fixture()
def env(tmp_path: Path):
    ws = tmp_path / "ws"
    (ws / "results").mkdir(parents=True)
    with mock.patch.dict(os.environ, {"PARHELION_HOME": str(tmp_path / "home"), "PARHELION_DISABLE_DAEMON_PROXY": "1"}):
        mgr = AgentMeshManager()
        store = mgr._store(ws)
        for aid, prob in (("dee-aaaaaa", "P1"), ("dee-bbbbbb", "P2")):
            store.create_agent({"agent_id": aid, "driver": "deepseek", "problem": prob, "worktree": str(ws), "status": "idle"})
        try:
            yield ws, mgr, store
        finally:
            bind_daemon_agent_manager(None)
            handlers_agents._LOCAL_MANAGER = None
            mgr.stop_all()


def _hub(ws, store, **kw) -> eh.EventHub:
    return eh.EventHub(ws, store, background=False, **kw)


def test_rev_is_monotonic_and_other_boots_resync(env) -> None:
    ws, _, store = env
    hub = _hub(ws, store)
    revs = [hub.publish("agent", agent_id="dee-aaaaaa", line=f"e{i}").n for i in range(3)]
    assert revs == [1, 2, 3] and hub.rev == f"{hub.boot_id}:3"
    assert hub.parse_since(f"{hub.boot_id}:1") == 1 and hub.parse_since(None) == 3
    for stale in ("deadbeef:2", f"{hub.boot_id}:99", "garbage"):
        assert hub.parse_since(stale) is None
    res = hub.wait(since="deadbeef:2", timeout=5)
    assert res["resync"] is True and res["rev"] == hub.rev and "agents" in res["snapshot"]


def test_burst_coalesces_for_250ms(env) -> None:
    ws, _, store = env
    hub = _hub(ws, store)
    rev = hub.rev
    hub.publish("agent", agent_id="dee-aaaaaa", line="first")
    threading.Timer(0.1, lambda: hub.publish("agent", agent_id="dee-aaaaaa", line="second")).start()
    t0 = time.monotonic()
    res = hub.wait(since=rev, timeout=5)
    assert res["changed"] and res["events"] == ["first", "second"]
    assert 0.2 <= time.monotonic() - t0 < 1.0


def test_at_most_32_events_per_second(env) -> None:
    ws, _, store = env
    hub = _hub(ws, store, coalesce_sec=0.0)
    rev = hub.rev
    for i in range(50):
        hub.publish("agent", agent_id="dee-aaaaaa", line=f"e{i}")
    first = hub.wait(since=rev, timeout=5)
    assert len(first["events"]) == 32 and first["more"] is True
    t0 = time.monotonic()
    second = hub.wait(since=first["rev"], timeout=5)
    assert second["events"] == [f"e{i}" for i in range(32, 50)] and second["more"] is False
    assert time.monotonic() - t0 >= 0.8  # the per-caller budget refills after one second


def test_callers_see_only_their_scope(env) -> None:
    ws, _, store = env
    hub = _hub(ws, store, coalesce_sec=0.0, max_per_sec=100)
    rev = hub.rev
    pub = hub.publish
    pub("inbox", agent_id="dee-aaaaaa", problem="P1", audience="worker", line="to A")
    pub("inbox", agent_id="dee-bbbbbb", problem="P2", audience="worker", line="to B")
    pub("inbox", agent_id="dee-aaaaaa", problem="P1", audience="orchestrator", kind="result", line="A to orch")
    pub("inbox", agent_id="dee-bbbbbb", problem="P2", audience="orchestrator", kind="question", strategic=True,
        line="B asks orch")
    pub("test", agent_id="dee-aaaaaa", problem="P1", line="test A")
    pub("test", agent_id="dee-bbbbbb", problem="P2", line="test B")
    pub("judge", problem="P1", line="judge P1")
    pub("judge", problem="P2", line="judge P2")
    pub("agent", agent_id="dee-aaaaaa", problem="P1", line="seat A")
    worker = hub.wait(role="worker", agent_id="dee-aaaaaa", since=rev, timeout=1)
    assert worker["events"] == ["to A", "test A", "judge P1", "seat A"]
    orch = hub.wait(since=rev, timeout=1)
    assert orch["events"] == ["A to orch", "B asks orch", "test A", "test B", "judge P1", "judge P2", "seat A"]
    assert hub.wait(since=rev, timeout=1, agent_ids=["dee-bbbbbb"], until=["inbox", "test"])["events"] == [
        "B asks orch", "test B"]
    assert hub.wait(since=rev, timeout=1, problem="P1", until="judge")["events"] == ["judge P1"]
    assert hub.wait(since=rev, timeout=1, strategic_only=True, until=["inbox"])["events"] == ["B asks orch"]
    with pytest.raises(ValueError):
        hub.wait(role="worker", since=rev, timeout=1)
    with pytest.raises(ValueError):
        hub.wait(since=rev, until=["nope"], timeout=1)


def _record(ws: Path, status: str, **extra) -> str:
    rid = extra.pop("request_id", uuid.uuid4().hex)
    rec = {"request_id": rid, "problem": "P1", "sha": "abc1234" + "0" * 33, "level": "standard", "cases": [],
           "agent_id": "dee-aaaaaa", "role": "worker", "status": status, "pid": None, "log": "",
           "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "digest": None,
           "progress": {"phase": "case", "cases_done": 9, "selected": 15}, **extra}
    test_runs._write(ws, rec)
    return rid


def test_four_sources_come_from_platform_records(env) -> None:
    ws, _, store = env
    ledger = ws / "results" / "submit-queue-outcome.jsonl"
    ledger.write_text(json.dumps({"title": "P1", "sha": "1" * 40, "status": "Pass", "submission_id": "old"}) + "\n")
    hub = _hub(ws, store, coalesce_sec=0.0, max_per_sec=100)
    rev = hub.rev
    store.append_message("dee-aaaaaa", "orch_to_worker", "followup", "try the twiddle table")
    store.append_message("dee-aaaaaa", "worker_to_orch", "result", "abc1234 standard 15/15")
    store.append_message("dee-aaaaaa", "squad_note", "result", "not addressed to anybody waiting")
    store.update_agent("dee-aaaaaa", status="working")
    rid = _record(ws, "queued")
    with ledger.open("a") as fh:
        for row in ({"title": "P1", "sha": "2" * 40, "status": "Wrong Answer", "submission_id": "s-new"},
                    {"title": "P1", "sha": "2" * 40, "status": "Wrong Answer", "submission_id": "s-new"},
                    {"title": "P1", "sha": "1" * 40, "status": "Pass", "submission_id": "old"}):
            fh.write(json.dumps(row) + "\n")
    hub.scan_now()
    time.sleep(0.01)
    _record(ws, "finished", request_id=rid, digest={"summary": {"passed": 15, "total": 15}})
    hub.scan_now()
    lines = hub.wait(since=rev, timeout=1)["events"]
    sources = [ln.split()[0] for ln in lines]
    assert sources.count("inbox") == 1 and lines[0].startswith("inbox #") and "dee-aaaaaa result" in lines[0]
    assert any(ln.startswith("agent dee-aaaaaa P1 idle→working") for ln in lines)
    tests = [ln for ln in lines if ln.startswith("test ")]
    assert len(tests) == 2 and tests[0].endswith("queued") and "finished 15/15" in tests[1]
    assert [ln for ln in lines if ln.startswith("judge")] == [
        f"judge P1 2222222 Wrong Answer score None submission s-new"]
    worker = hub.wait(role="worker", agent_id="dee-aaaaaa", since=rev, timeout=1)["events"]
    assert any("followup: try the twiddle table" in ln for ln in worker)


def test_quiet_waits_back_off_to_the_cap(env) -> None:
    ws, _, store = env
    hub = _hub(ws, store)
    run_dir = ws / "run"
    run_dir.mkdir()
    (run_dir / "events.jsonl").write_text("".join(json.dumps({"event": "case"}) + "\n" for _ in range(9)))
    log = ws / "run.log"
    log.write_text(test_runs.STARTED_PREFIX + json.dumps({"dir": str(run_dir), "selected": 15, "run_id": "r1"}) + "\n")
    _record(ws, "running", pid=os.getpid(), log=str(log))  # a live run: refresh keeps it running
    hub.scan_now()
    first = hub.wait(role="worker", agent_id="dee-aaaaaa", timeout=0.05)
    assert first["changed"] is False and first["idle_streak"] == 1 and first["next_timeout"] == 37.5
    assert "your P1 standard run" in first["waiting_for"] and "9/15 cases" in first["waiting_for"]
    # Without an explicit timeout a watched running test means the full cap; otherwise the idle schedule.
    assert hub.default_timeout(("worker", "dee-aaaaaa"), "worker", "dee-aaaaaa", "P1", ("test",)) == eh.MAX_BLOCK_SEC
    assert hub.default_timeout(("orchestrator", "orchestrator"), "orchestrator", "", "P2", eh.SOURCES) == 25.0
    for _ in range(6):
        res = hub.wait(role="worker", agent_id="dee-aaaaaa", since=first["rev"], timeout=0.01)
    assert res["idle_streak"] == 7 and res["next_timeout"] == eh.MAX_BLOCK_SEC
    hub.publish("agent", agent_id="dee-aaaaaa", line="seat")
    changed = hub.wait(role="worker", agent_id="dee-aaaaaa", since=res["rev"], timeout=1)
    assert changed["changed"] and changed["next_timeout"] == eh.DEFAULT_BLOCK_SEC
    # 4.3.1: a managed DSH worker aborts an MCP request at 60 s (the desktop plugin at 120 s); every
    # wait the first sprint asked 60-100 s for died at exactly 60.0 s.
    assert eh.MAX_BLOCK_SEC < 60


def test_status_if_rev_answers_not_modified() -> None:
    payload = {"sampled_at": "t1", "contest": {"weighted_score": 1.0},
               "scheduler": {"rate_ready": True, "cooldown_remaining_sec": 30}, "problems": {"rows": []}}
    with mock.patch.object(handlers_bench, "_status_payload", side_effect=lambda a, w: json.loads(json.dumps(payload))):
        first = handlers_bench.tool_parhelion_status({}, Path("/tmp"))
        assert first["rev"].startswith("s1:") and first["contest"] == {"weighted_score": 1.0}
        payload.update(sampled_at="t2", scheduler={"rate_ready": True, "cooldown_remaining_sec": 12})
        assert handlers_bench.tool_parhelion_status({"if_rev": first["rev"]}, Path("/tmp")) == {
            "not_modified": True, "rev": first["rev"]}
        assert handlers_bench.tool_parhelion_status({"if_rev": first["rev"], "problem": "P1"}, Path("/tmp")).get("rev")
        payload["contest"] = {"weighted_score": 2.0}
        changed = handlers_bench.tool_parhelion_status({"if_rev": first["rev"]}, Path("/tmp"))
    assert "not_modified" not in changed and changed["rev"] != first["rev"]


AGENT_WAIT_KEYS = {"ok", "timed_out", "next_seq", "strategic_only", "messages", "squad_digest", "agents"}


def test_agent_wait_shape_is_unchanged_on_the_hub(env) -> None:
    ws, mgr, store = env
    local = call_tool("parhelion_agent_wait", {"timeout_sec": 0.5}, ws, role="orchestrator")
    bind_daemon_agent_manager(mgr)
    assert mgr.hub_enabled
    timer = threading.Timer(0.3, lambda: (store.append_message("dee-aaaaaa", "worker_to_orch", "question", "need a decision"),
                                          mgr._notify()))
    timer.start()
    t0 = time.monotonic()
    hubbed = call_tool("parhelion_agent_wait", {"timeout_sec": 10}, ws, role="orchestrator")
    assert time.monotonic() - t0 < 2.0
    assert set(local) == set(hubbed) == AGENT_WAIT_KEYS
    assert hubbed["timed_out"] is False and hubbed["messages"][0]["body"] == "need a decision"
    assert local["timed_out"] is True


def test_test_wait_shape_and_wakeup_on_the_hub(env) -> None:
    ws, mgr, store = env
    rid = _record(ws, "queued")
    bind_daemon_agent_manager(mgr)
    hub = mgr.event_hub(ws)
    threading.Timer(0.3, lambda: _record(ws, "finished", request_id=rid)).start()
    t0 = time.monotonic()
    view = call_tool("parhelion_test_wait", {"request_id": rid, "wait_sec": 20}, ws, role="worker", agent_id="dee-aaaaaa")
    assert view["status"] == "finished" and time.monotonic() - t0 < 3.0
    assert set(view) == set(test_runs.public_view({}))
    assert test_runs._read(ws, rid)["observed"] is True
    assert hub.n >= 1


def test_without_daemon_waits_fall_back_locally(env) -> None:
    ws, _, store = env
    rid = _record(ws, "finished")
    with mock.patch.object(test_runs, "wait", wraps=test_runs.wait) as spy:
        view = call_tool("parhelion_test_wait", {"request_id": rid}, ws, role="orchestrator")
    assert view["status"] == "finished" and spy.call_args.kwargs.get("block") is None
    res = call_tool("parhelion_wait", {"timeout": 0.2}, ws, role="orchestrator")
    assert res["changed"] is False and res["rev"].endswith(":0")
    w = call_tool("parhelion_wait", {"timeout": 0.1, "until": ["test"]}, ws, role="worker", agent_id="dee-aaaaaa")
    assert w["changed"] is False and "waiting_for" in w


def test_event_to_return_latency(env) -> None:
    """Event (bus write + manager notify) to parhelion_wait return, with the 250 ms coalescing window."""
    ws, mgr, store = env
    hub = mgr.event_hub(ws)
    lat = []
    for i in range(8):
        rev = hub.wait(timeout=0.01)["rev"]
        out: dict = {}
        waiter = threading.Thread(target=lambda: out.update(res=hub.wait(since=rev, timeout=5), t1=time.monotonic()))
        waiter.start()
        time.sleep(0.05)
        t0 = time.monotonic()
        store.append_message("dee-aaaaaa", "worker_to_orch", "result", f"done {i}")
        mgr._notify()
        waiter.join(6)
        assert out["res"]["changed"]
        lat.append(out["t1"] - t0)
    assert statistics.median(lat) < 0.45 and max(lat) < 0.8


def _post(port: int, path: str, payload: dict, timeout: float = 15) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    raw = json.dumps(payload).encode()
    conn.request("POST", path, body=raw, headers={"X-Opsb-Token": "tok", "Content-Type": "application/json",
                                                  "Content-Length": str(len(raw))})
    resp = conn.getresponse()
    body = json.loads(resp.read().decode() or "{}")
    conn.close()
    return resp.status, body


def test_daemon_http_routes_and_proxy_fallback(env) -> None:
    from opsb_core.daemon.npu_stream import NpuStreamManager
    from opsb_core.daemon.server import OpsbDaemonRuntime, _make_handler, _ThreadingHTTPServer
    ws, mgr, store = env
    with mock.patch.object(NpuStreamManager, "start", return_value=None), \
            mock.patch.object(NpuStreamManager, "stop", return_value=None):
        runtime = OpsbDaemonRuntime(workspace=ws, ssh_host="npu", port=0, token="tok")
        runtime.agent_mesh = mgr
        bind_daemon_agent_manager(mgr)
        stop = threading.Event()
        server = _ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(runtime, stop))
        port = int(server.server_address[1])
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            status, first = _post(port, "/v1/events/wait", {"workspace": str(ws), "timeout": 0.1})
            assert status == 200 and first["changed"] is False
            threading.Timer(0.3, lambda: (store.append_message("dee-bbbbbb", "worker_to_orch", "result", "ok"),
                                          mgr._notify())).start()
            status, got = _post(port, "/v1/events/wait", {"workspace": str(ws), "since": first["rev"], "timeout": 10})
            assert status == 200 and got["changed"] and "dee-bbbbbb result: ok" in got["events"][0]
            rid = _record(ws, "finished")
            status, view = _post(port, "/v1/events/test_wait", {"workspace": str(ws), "request_id": rid, "wait_sec": 5})
            assert status == 200 and view["status"] == "finished"
            assert _post(port, "/v1/events/test_wait", {"workspace": str(ws)})[0] == 400
            # A stdio process proxies to the daemon; a daemon without the route returns 404 -> local fallback.
            bind_daemon_agent_manager(None)
            state = {"port": port, "token": "tok"}
            with mock.patch.dict(os.environ, {"PARHELION_DISABLE_DAEMON_PROXY": "0"}), \
                    mock.patch("opsb_core.daemon.server.read_daemon_state", return_value=state), \
                    mock.patch("opsb_core.daemon.server.probe_daemon_health", return_value={"ok": True}):
                proxied = call_tool("parhelion_wait", {"timeout": 0.1}, ws, role="orchestrator")
                assert proxied["rev"].split(":")[0] == mgr.event_hub(ws).boot_id
                assert handlers_agents._proxy_daemon("POST", "/v1/events/absent", ws, {}, missing_ok=True) is None
        finally:
            stop.set()
            server.shutdown()
            server.server_close()
