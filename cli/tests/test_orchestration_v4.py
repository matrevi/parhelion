"""Acceptance tests for 4.0 orchestration: sprint modes, provider breaker, token compaction,
squad addressing, state card, test-result delivery and lean briefs (work package C)."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from unittest import mock

import pytest

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core.agents.adapters import BaseWorkerAdapter, emit_context  # noqa: E402
from opsb_core.agents.manager import AgentMeshManager, _WorkerRunner, compact_threshold  # noqa: E402
from opsb_core.drivers.base import resolve_workspace_state_dir  # noqa: E402

TEST_RUNS_MOD = "opsb_core.services.test_runs"


class FakeAdapter(BaseWorkerAdapter):
    """Deterministic in-memory worker; a prompt containing FAIL_BALANCE raises a provider fault."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.turns: list[str] = []
        self.compactions: list[str] = []

    def send_turn(self, prompt, on_event=None):
        self.turns.append(prompt)
        if "FAIL_BALANCE" in prompt:
            raise RuntimeError("Provider error: Insufficient Balance on the account")
        return f"turn {len(self.turns)} done"

    def compact(self, checkpoint):
        self.compactions.append(checkpoint)
        return "compacted"

    def stop(self):
        pass


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PARHELION_HOME", str(tmp_path / "phome"))
    monkeypatch.setenv("PARHELION_DISABLE_DAEMON_PROXY", "1")
    ws = (tmp_path / "contest").resolve()
    ws.mkdir()
    mgr = AgentMeshManager(adapter_factory=FakeAdapter)
    monkeypatch.setattr(mgr, "_cached_doctor", lambda: {"diagnostics": [
        dict(kind="deepseek", auth_ok=True, binary_path="dsh", installed=True)]})
    yield ws, mgr
    mgr.stop_all()


def _adapter(mgr, agent_id):
    return mgr._runners[agent_id].adapter


def _spawn(mgr, ws, *, driver="deepseek", problem="Demo", axis="", role="worker"):
    res = mgr.spawn_agent(ws, driver=driver, task="work", problem=problem, axis=axis, role_label=role,
                          worktree_mode="readonly", _sprint=True)
    aid = res["agent"]["agent_id"]
    assert _wait(lambda: mgr._store(ws).get_agent(aid)["status"] == "idle")
    return aid


def _make_contest(ws: Path, count: int) -> list[str]:
    template = ws.parent / "template-repo"
    template.mkdir()
    (template / "kernel.asc").write_text("// kernel\n")
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "T"],
                 ["add", "."], ["commit", "-q", "-m", "baseline"]):
        subprocess.run(["git", "-C", str(template), *args], check=True)
    titles = [f"Op{i:02d}" for i in range(count)]
    for title in titles:
        shutil.copytree(template, ws / title)
    (ws / "opsb.workspace.json").write_text(json.dumps({
        "layout": "clean-agents-first",
        "problems": [{"title": t, "path": t, "difficulty": 2} for t in titles],
    }))
    return titles


# 1. sprint modes ---------------------------------------------------------------------------

def test_sprint_modes_open_one_two_or_three_seats_per_problem(env):
    from opsb_core.agents.sprint import start_sprint
    from opsb_core.mcp.handlers_agents import bind_daemon_agent_manager
    from opsb_core.mcp.tools import call_tool
    ws, mgr = env
    titles = _make_contest(ws, 14)
    solo = start_sprint(mgr, ws, driver="deepseek")
    assert solo["mode"] == "solo" and solo["ok"]
    assert [s["state"] for s in solo["assignments"]] == ["started"] * 14
    assert {s["problem"] for s in solo["assignments"]} == set(titles)
    agents = mgr._store(ws).list_agents(limit=-1)
    assert len(agents) == 14
    assert {(a["axis"], a["role_label"], Path(a["worktree"]).name) for a in agents} == {("solo", "worker", "sprint-solo")}
    first = agents[0]["agent_id"]
    assert _wait(lambda: mgr._runners[first].adapter.turns)
    assert "STATE " in _adapter(mgr, first).turns[0]
    assert all(s["state"] == "existing" for s in start_sprint(mgr, ws, driver="deepseek")["assignments"])

    dual = start_sprint(mgr, ws, driver="deepseek", mode="dual", preview=True)["assignments"]
    assert len(dual) == 28 and {s["axis"] for s in dual} == {"algorithm", "dataflow"}
    full = start_sprint(mgr, ws, driver="deepseek", mode="full", preview=True)["assignments"]
    assert len(full) == 42 and {s["axis"] for s in full} == {"algorithm", "dataflow", "calibration"}
    with pytest.raises(ValueError, match="mode"):
        start_sprint(mgr, ws, driver="deepseek", mode="triple")

    bind_daemon_agent_manager(mgr)
    try:
        via_tool = call_tool("parhelion_sprint_start", {"driver": "deepseek", "mode": "dual", "preview": True}, ws)
        assert via_tool["mode"] == "dual" and len(via_tool["assignments"]) == 28
        with pytest.raises(ValueError, match="mode"):
            call_tool("parhelion_sprint_start", {"driver": "deepseek", "mode": "bogus"}, ws)
    finally:
        bind_daemon_agent_manager(None)


# 2-3. provider fault circuit breaker ------------------------------------------------------

def test_provider_fault_suspends_driver_once_and_admin_spawn_clears_it(env):
    ws, mgr = env
    store = mgr._store(ws)
    failing, peer_a, peer_b = (_spawn(mgr, ws, axis=f"a{i}") for i in range(3))
    other = _spawn(mgr, ws, driver="pi", axis="other")
    mgr.send_message(ws, failing, "FAIL_BALANCE now")
    assert _wait(lambda: all(store.get_agent(a)["status"] == "error" for a in (failing, peer_a, peer_b)))
    assert mgr.driver_suspended(ws, "deepseek")
    assert "Insufficient Balance" in store.get_agent(failing)["error"]
    assert {store.get_agent(a)["error"] for a in (peer_a, peer_b)} == {"suspended: provider fault"}
    assert store.get_agent(other)["status"] == "idle"
    doc = json.loads((resolve_workspace_state_dir(ws) / "driver_suspensions.json").read_text())
    assert set(doc) == {"deepseek"} and len(doc["deepseek"]["reason"]) <= 300
    strategic = [m for m in store.list_messages(limit=200, strategic_only=True, for_orchestrator=True)
                 if m["agent_id"] == "platform"]
    assert len(strategic) == 1 and strategic[0]["kind"] == "error" and "deepseek" in strategic[0]["body"]
    # A second fault report for the same driver adds no further alert.
    assert mgr.suspend_driver(ws, "deepseek", "rate limit")["new"] is False

    turns_before = len(_adapter(mgr, peer_a).turns)
    sent = mgr.send_message(ws, peer_a, "keep going")
    assert sent["ok"] and sent["woken"] is False and sent["reason"] == "driver suspended"
    time.sleep(0.1)
    assert len(_adapter(mgr, peer_a).turns) == turns_before
    assert store.get_agent(peer_a)["status"] == "error"

    blocked = mgr.spawn_agent(ws, driver="deepseek", task="auto", worktree_mode="readonly", _sprint=True)
    assert blocked == {"ok": False, "blocked": True, "reason": "driver suspended"}
    admin = mgr.spawn_agent(ws, driver="deepseek", task="resume after top-up", worktree_mode="readonly")
    assert admin["ok"] and not mgr.driver_suspended(ws, "deepseek")


def test_preview_reports_suspension_and_explicit_sprint_clears_it(env):
    from opsb_core.agents.sprint import start_sprint
    ws, mgr = env
    _make_contest(ws, 1)
    mgr.suspend_driver(ws, "deepseek", "HTTP 402 payment required")
    preview = start_sprint(mgr, ws, driver="deepseek", preview=True)
    assert [s["reason"] for s in preview["assignments"]] == ["driver suspended"]
    assert mgr.driver_suspended(ws, "deepseek")
    started = start_sprint(mgr, ws, driver="deepseek")
    assert [s["state"] for s in started["assignments"]] == ["started"]
    assert not mgr.driver_suspended(ws, "deepseek")


def test_provider_fault_pattern_ignores_ordinary_errors():
    from opsb_core.agents.manager import PROVIDER_FAULT
    for text in ("Insufficient Balance", "insufficient_quota", "HTTP 429 Too Many Requests", "401 Unauthorized",
                 "invalid API key", "Payment Required", "rate limit reached"):
        assert PROVIDER_FAULT.search(text), text
    for text in ("compile failed at line 4029", "timeout after 60s", "case 401x mismatch"):
        assert not PROVIDER_FAULT.search(text), text


# 4. token-based compaction ----------------------------------------------------------------

def test_compact_threshold_is_window_adaptive_and_configurable():
    assert compact_threshold(1_000_000, cfg={}) == 300_000
    assert compact_threshold(200_000, cfg={}) == 120_000
    assert compact_threshold(None, cfg={}) == 120_000
    assert compact_threshold(2_000_000, cfg={}) == 400_000
    assert compact_threshold(1_000_000, cfg={"agents": {"compact_tokens": 250_000}}) == 250_000
    assert compact_threshold(1_000_000, cfg={"agents": {"compact_tokens": 10_000}}) == 80_000
    assert compact_threshold(1_000_000, cfg={"agents": {"compact_tokens": 900_000}}) == 600_000


def _run_turn_with_usage(tmp_path, used, window, config=None, stage=""):
    from opsb_core.agents.bus_store import AgentBusStore
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    store = AgentBusStore(ws)
    aid = f"dee-{used}-{window}"
    store.create_agent({"agent_id": aid, "driver": "deepseek", "problem": "Demo", "worktree_mode": "readonly"})
    adapter = FakeAdapter(ws, ws, "deepseek", aid, tmp_path / "log.jsonl")
    done = threading.Event()
    holder = {}

    def send_turn(prompt, on_event=None):
        emit_context(used, window, on_event)
        if stage:  # a worker reports its stage result during the turn
            holder["runner"]._stage_result = stage
        return "stage reply " + "x" * 3000

    adapter.send_turn = send_turn
    runner = holder["runner"] = _WorkerRunner(aid, store, adapter, lambda: None)
    original = runner._on_notify

    def notify():
        original()
        if (store.get_agent(aid) or {}).get("status") == "idle":
            done.set()
    runner._on_notify = notify
    try:
        with mock.patch("opsb_core.config.ConfigRepository.load", return_value=config or {}):
            runner.enqueue("work")
            assert done.wait(5)
        return adapter.compactions, runner
    finally:
        runner.stop()


def test_runner_compacts_by_absolute_tokens_with_state_card(tmp_path, monkeypatch):
    monkeypatch.setenv("PARHELION_HOME", str(tmp_path / "phome"))
    assert _run_turn_with_usage(tmp_path, 290_000, 1_000_000)[0] == []
    compactions, runner = _run_turn_with_usage(tmp_path, 300_000, 1_000_000)
    assert len(compactions) == 1 and runner._context_tokens == 0
    checkpoint = compactions[0]
    assert len(checkpoint) <= 4000
    assert "\nSTATE Demo" in checkpoint
    assert checkpoint.endswith("Continue from the state card. Do not re-read files you already summarized.")
    assert checkpoint.startswith("x")  # no stage result: the reply tail seeds the checkpoint
    assert len(_run_turn_with_usage(tmp_path, 120_000, 200_000)[0]) == 1
    assert _run_turn_with_usage(tmp_path, 119_999, 200_000)[0] == []
    override = {"agents": {"compact_tokens": 10_000}}  # clamped up to 80k
    assert _run_turn_with_usage(tmp_path, 79_999, 1_000_000, override)[0] == []
    assert len(_run_turn_with_usage(tmp_path, 80_000, 1_000_000, override)[0]) == 1
    staged, _ = _run_turn_with_usage(tmp_path, 700, 1_000, stage="Verified abc1234: +3% on bucket small")
    assert staged[0].startswith("Verified abc1234")  # 70% of the window also triggers


# 5. squad addressing ------------------------------------------------------------------------

def test_squad_addresses_by_role_name_all_and_lists_teammates_on_error(env):
    ws, mgr = env
    store = mgr._store(ws)
    sender = _spawn(mgr, ws, axis="algorithm")
    dataflow = _spawn(mgr, ws, axis="dataflow")
    author = _spawn(mgr, ws, axis="calibration", role="bench-author")
    _spawn(mgr, ws, problem="Elsewhere", axis="dataflow")

    one = mgr.squad(ws, sender, recipient="DataFlow", message="repeat bucket small", request_id="r1")
    assert one["recipient"] == dataflow and not one["duplicate"]
    assert mgr.squad(ws, sender, recipient="sprint-dataflow", message="repeat bucket small",
                     request_id="r1")["duplicate"]
    assert mgr.squad(ws, sender, recipient="bench-author", message="check")["recipient"] == author

    everyone = mgr.squad(ws, sender, recipient="all", message="x" * 5000)
    assert everyone["ok"] and set(everyone["recipients"]) == {dataflow, author} and len(everyone["seqs"]) == 2
    body = next(m["body"] for m in store.list_messages(agent_id=author, limit=50, include_squad=True)
                if m["seq"] == everyone["seqs"][everyone["recipients"].index(author)])
    assert body.endswith("…[truncated]") and body.count("x") == 4000

    with pytest.raises(ValueError) as missing:
        mgr.squad(ws, sender, recipient="optimizer", message="hi")
    assert f"{dataflow}:dataflow:worker" in str(missing.value) and f"{author}:calibration:bench-author" in str(missing.value)
    with pytest.raises(ValueError, match="several"):
        _spawn(mgr, ws, axis="solo")
        mgr.squad(ws, sender, recipient="worker", message="hi")


# 6. state card -------------------------------------------------------------------------------

def _fake_test_runs(monkeypatch, *, mark=True):
    calls = {"marked": []}

    def records(ws):
        root = resolve_workspace_state_dir(ws) / "test_runs"
        return [json.loads(p.read_text()) for p in sorted(root.glob("*.json"))] if root.is_dir() else []

    def list_undelivered(ws):
        return [r for r in records(ws) if r["status"] in ("finished", "failed") and not r.get("delivered")
                and r.get("agent_id")]

    def mark_delivered(ws, request_id):
        calls["marked"].append(request_id)
        if mark:
            path = resolve_workspace_state_dir(ws) / "test_runs" / f"{request_id}.json"
            rec = json.loads(path.read_text())
            rec["delivered"] = True
            path.write_text(json.dumps(rec))

    def compact_line(record):
        return f"{record['problem']} {record['sha'][:7]} {record['level']} {record['status']} 15/15"[:600]

    module = types.ModuleType(TEST_RUNS_MOD)
    module.list_undelivered, module.mark_delivered, module.compact_line = list_undelivered, mark_delivered, compact_line
    monkeypatch.setitem(sys.modules, TEST_RUNS_MOD, module)
    return calls


def _write_run(ws, request_id, *, agent_id="", problem="Demo", observed=False, created="2026-11-25T10:00:00Z",
               sha="a" * 40, status="finished"):
    root = resolve_workspace_state_dir(ws) / "test_runs"
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{request_id}.json").write_text(json.dumps(dict(
        request_id=request_id, problem=problem, sha=sha, level="standard", cases=[], remeasure=False,
        agent_id=agent_id, role="worker", created_at=created, status=status, run_id="r-" + request_id,
        exit_code=0, digest=None, report="", finished_at=created, observed=observed, delivered=False)))


def test_state_card_empty_ledger_and_byte_budget(env, monkeypatch):
    from opsb_core.services import state_card
    ws, _ = env
    empty = state_card.build(ws, "Demo")
    assert empty.startswith("STATE Demo @ ") and "best: n/a" in empty and "last tests: n/a" in empty
    assert empty.splitlines()[-1].startswith("rules: device work only via parhelion_test")
    assert len(empty.encode("utf-8")) <= 2000

    results = ws / "results"
    results.mkdir()
    rows = [dict(title="Demo", sha=f"{i:02d}" + "c" * 38, submission_id=f"sub-{i:08d}-long", status="Pass", score=40 + i)
            for i in range(30)]
    rows[5]["score"] = 99.5
    rows.append(dict(title="Demo", sha="f" * 40, submission_id="last", status="Wrong Answer", score=0,
                     public_times_us=[9.4, 12.0, 3.3], public_tbest_us=[8.8, 11.0]))
    rows.append(dict(title="Other", sha="e" * 40, submission_id="x", status="Pass", score=100))
    (results / "submit-queue-outcome.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    _fake_test_runs(monkeypatch)
    for i in range(5):
        _write_run(ws, f"{i:032x}", agent_id="dee-1" if i % 2 else "dee-2", created=f"2026-11-25T10:0{i}:00Z")
    suite = types.ModuleType("scheduler.suite")
    suite.load_suite = lambda _ws, _title: {"suite_version": 3, "timed": ["05", "local/odd"]}
    monkeypatch.setitem(sys.modules, "scheduler.suite", suite)

    card = state_card.build(ws, "Demo", agent_id="dee-1")
    lines = card.splitlines()
    assert lines[1].startswith("best: 99.50 05ccccc (sub-0000")
    assert lines[2].startswith("last receipts: Wrong Answer 0.00 fffffff; Pass 69.00")
    assert lines[3] == "public gap (ours/tbest µs): 01 9.4/8.8 · 02 12.0/11.0 · 03 3.3/-"
    assert lines[4].count("Demo aaaaaaa standard finished") == 2  # only dee-1's runs (2 of 5)
    assert lines[5] == "suite: v3 · timed 2 · levels smoke/standard/final"
    assert len(card.encode("utf-8")) <= 2000
    suite.load_suite = lambda _ws, _title: {"suite_version": 0, "timed": [], "timing_scope": "official"}
    assert state_card.build(ws, "Demo").splitlines()[5].startswith("suite: v0 · timed official+0 ·")

    monkeypatch.setattr(state_card, "_receipts", mock.Mock(side_effect=RuntimeError("boom")))
    assert state_card.build(ws, "Demo") == "STATE Demo: unavailable"


def test_state_card_truncates_to_2000_bytes_keeping_rules(env, monkeypatch):
    from opsb_core.services import state_card
    ws, _ = env
    monkeypatch.setattr(state_card, "_tests_line", lambda *_: "last tests: " + "测" * 2000)
    card = state_card.build(ws, "Demo")
    assert len(card.encode("utf-8")) <= 2000 and card.endswith(state_card.RULES_LINE)


# 7. test result delivery ---------------------------------------------------------------------

def test_deliver_test_results_wakes_idle_unobserved_and_never_repeats(env, monkeypatch):
    ws, mgr = env
    store = mgr._store(ws)
    worker = _spawn(mgr, ws)
    calls = _fake_test_runs(monkeypatch, mark=False)  # rely on the bus event key for idempotence
    unseen, seen = "1" * 32, "2" * 32
    _write_run(ws, unseen, agent_id=worker, observed=False)
    _write_run(ws, seen, agent_id=worker, observed=True)
    _write_run(ws, "3" * 32, agent_id="", observed=False)  # no requester: never delivered
    adapter = _adapter(mgr, worker)
    turns = len(adapter.turns)

    assert mgr.deliver_test_results(ws) == 2
    assert _wait(lambda: len(adapter.turns) == turns + 1 and store.get_agent(worker)["status"] == "idle")
    assert f"parhelion_test_wait request_id={unseen}" in adapter.turns[-1]
    msgs = [m for m in store.list_messages(agent_id=worker, limit=100) if m["kind"] == "test_result"]
    assert sorted(m["body"] for m in msgs) == ["Demo aaaaaaa standard finished 15/15"] * 2
    assert {m["direction"] for m in msgs} == {"orch_to_worker"}

    mgr.deliver_test_results(ws)
    time.sleep(0.1)
    assert len(adapter.turns) == turns + 1
    assert len([m for m in store.list_messages(agent_id=worker, limit=100) if m["kind"] == "test_result"]) == 2
    assert sorted(calls["marked"]) == [unseen, unseen, seen, seen]


def test_deliver_test_results_without_registry_module(env, monkeypatch):
    ws, mgr = env
    monkeypatch.setitem(sys.modules, TEST_RUNS_MOD, None)  # import fails as if package A were absent
    assert mgr.deliver_test_results(ws) == 0


def test_judgement_delivery_does_not_wake_error_workers(env):
    ws, mgr = env
    store = mgr._store(ws)
    worker = _spawn(mgr, ws)
    store.update_agent(worker, status="error", error="crashed")
    (ws / "results").mkdir()
    (ws / "results/submit-queue-outcome.jsonl").write_text(json.dumps(dict(
        title="Demo", sha="b" * 40, submission_id="s1", status="Pass", score=50, agent_id=worker,
        submitted_at="2026-11-25T00:00:00Z")) + "\n")
    turns = len(_adapter(mgr, worker).turns)
    mgr.deliver_judgements(ws, 5.0)
    assert [m["kind"] for m in store.list_messages(agent_id=worker, limit=50)].count("judge_result") == 1
    time.sleep(0.05)
    assert len(_adapter(mgr, worker).turns) == turns


# 8. lean briefs ------------------------------------------------------------------------------

def test_session_and_bench_author_briefs_fit_budget(env):
    from opsb_core.drivers.brief import MAX_BRIEF_BYTES, compose_bench_author_brief, compose_session_brief
    ws, _ = env
    op = ws / "Demo"
    op.mkdir()
    for name in ("CMakeLists.txt", "run.sh", "kernel.asc", "main.asc", "reference.py"):
        (op / name).write_text("x\n")
    brief = compose_session_brief(ws, "Demo", goal="Own this problem end to end. " * 8,
                                  worktree=op / ".worktrees" / "sprint-solo", branch="sprint-solo", agent_id="dee-1")
    body, card = brief.split("\n\nSTATE ", 1)
    assert len(body.encode("utf-8")) <= MAX_BRIEF_BYTES == 1500
    for needle in ("parhelion_test_wait", "parhelion_submit_gate", "parhelion_squad", "parhelion_kb_query",
                   "sprint-solo", "kernel.asc"):
        assert needle in body
    assert body.index("parhelion_test ") < body.index("parhelion_test_wait") < body.index("parhelion_submit_gate")
    assert card.startswith("Demo @ ")
    bench = compose_bench_author_brief(ws, "Demo")
    assert len(bench.encode("utf-8")) <= MAX_BRIEF_BYTES
    assert "level=final" in bench and "opsb bench suite add-variant Demo" in bench


def test_guides_use_async_test_tools_and_keep_strategy():
    from opsb_core.services.templates_md import PROBLEM_AGENTS_MD, WORKSPACE_AGENTS_MD
    for text in (PROBLEM_AGENTS_MD, WORKSPACE_AGENTS_MD):
        assert "parhelion_test_plan" not in text and "profile" not in text.lower()
    assert "parhelion_test_wait" in PROBLEM_AGENTS_MD and "`final`" in PROBLEM_AGENTS_MD
    assert "`ssh`" in PROBLEM_AGENTS_MD and "`flock`" in PROBLEM_AGENTS_MD
    assert "frozen golden oracle" in PROBLEM_AGENTS_MD
    assert "three opening probes" in WORKSPACE_AGENTS_MD and "mode=solo" in WORKSPACE_AGENTS_MD
    PROBLEM_AGENTS_MD.format(problem="P", git_owner="o", repo_slug="r", difficulty=2, npu_host="h")
    WORKSPACE_AGENTS_MD.format(contest_title="C")
