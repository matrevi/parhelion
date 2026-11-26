"""4.3 P25 wiring: the ``close`` view, ``opsb close-sweep``, the gated enqueue and the tool table."""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

CLI_DIR = pathlib.Path(__file__).resolve().parents[1]
ROOT = CLI_DIR.parent
for entry in (str(CLI_DIR), str(ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from opsb_core.mcp import list_tool_schemas  # noqa: E402
from opsb_core.mcp.handlers_report import tool_parhelion_report  # noqa: E402
from scheduler.tests.test_close_sweep import TITLE, WORK_PREFIX, _git, _workspace  # noqa: E402

PASS_LOG = "EXTENDED_PASSED 12 cases\nEXTENDED_RC=0\n"


def _add_second_problem(ws: pathlib.Path) -> str:
    """A copy of the first problem repository under its own title, so role filtering is visible."""
    other = "OtherOp"
    shutil.copytree(ws / WORK_PREFIX / TITLE, ws / WORK_PREFIX / other)
    manifest = json.loads((ws / WORK_PREFIX / "problems.json").read_text())
    manifest.append({"title": other, "slug": other.lower(), "path": f"{WORK_PREFIX}/{other}", "difficulty": 2})
    (ws / WORK_PREFIX / "problems.json").write_text(json.dumps(manifest))
    rows = (ws / "results" / "submit-queue-outcome.jsonl").read_text().splitlines()
    # a receipt id is unique across problems: the journal dedupes by `submission_id`
    (ws / "results" / "submit-queue-outcome.jsonl").write_text("".join(
        line + "\n" for line in rows +
        [json.dumps({**json.loads(r), "title": other, "submission_id": f"{json.loads(r)['submission_id']}-other"})
         for r in rows]))
    (ws / "results" / other).mkdir(parents=True, exist_ok=True)
    (ws / "results" / other / ".judge-rule.json").write_text(json.dumps({"ranking_submission_mode": "latest"}))
    return other


def _bind_worker(ws: pathlib.Path, agent: str, problem: str) -> None:
    from opsb_core.agents.bus_store import AgentBusStore

    AgentBusStore(ws).create_agent({"agent_id": agent, "driver": "deepseek", "problem": problem})


def _validate(ws: pathlib.Path, sha: str) -> None:
    target = ws / "results" / TITLE / sha
    target.mkdir(parents=True, exist_ok=True)
    (target / "result.json").write_text(json.dumps(
        {"title": TITLE, "sha": sha, "exit_code": 0, "layout": "template",
         "finished_at": "2026-11-20T00:00:00Z"}))
    (target / "validation.log").write_text(PASS_LOG)


def test_the_report_schema_declares_the_close_view_three_places() -> None:
    for role in ("orchestrator", "worker"):
        tools = {t["name"]: t for t in list_tool_schemas(role=role)}
        view = tools["parhelion_report"]["inputSchema"]["properties"]["view"]
        assert "close" in view["enum"]
        assert "close" in tools["parhelion_report"]["description"]
    from opsb_core.mcp.handlers_report import HELP, VIEWS

    assert "close" in VIEWS and "close" in HELP["views"]


def test_the_close_view_serves_every_problem_to_the_orchestrator_and_one_to_a_worker(
        tmp_path: pathlib.Path) -> None:
    ws, best, _, _ = _workspace(tmp_path)
    other = _add_second_problem(ws)
    seen = tool_parhelion_report({"view": "close"}, ws)
    assert seen["view"] == "close" and len(seen["problems"]) == 2
    assert [r[0] for r in seen["restore"]] == [TITLE, other]
    assert seen["totals"]["restore"] == 2 and seen["totals"]["restore_gap"] == 10.0
    assert seen["columns"][0] == "title" and "sigma_hat" in seen["columns"]

    _bind_worker(ws, "dee-w1", TITLE)
    own = tool_parhelion_report({"_worker_request": True, "_caller_agent_id": "dee-w1",
                                 "problem": TITLE, "view": "close"}, ws)
    assert [row[0] for row in own["problems"]] == [TITLE]
    assert [row[0] for row in own["restore"]] == [TITLE]
    assert own["merge"] == [] and own["totals"]["restore"] == 1
    with pytest.raises(PermissionError):
        tool_parhelion_report({"_worker_request": True, "_caller_agent_id": "dee-w1",
                               "problem": other, "view": "close"}, ws)
    _bind_worker(ws, "dee-none", "")
    with pytest.raises(PermissionError):
        tool_parhelion_report({"_worker_request": True, "_caller_agent_id": "dee-none", "view": "close"}, ws)
    assert best  # the fixture really has a best SHA


def test_the_close_view_help_documents_the_columns() -> None:
    doc = tool_parhelion_report({"view": "help"}, pathlib.Path("/tmp"))
    text = doc["views"]["close"]
    for word in ("restore", "merge", "tag", "G5", "3KB"):
        assert word in text


def test_opsb_close_sweep_parser_takes_enqueue_and_json() -> None:
    from opsb_core.cli_app import build_parser

    args = build_parser().parse_args(["close-sweep", "-w", "/tmp/ws"])
    assert args.enqueue is False and args.json is False and args.workspace == "/tmp/ws"
    both = build_parser().parse_args(["close-sweep", "--enqueue", "--json"])
    assert both.enqueue is True and both.json is True


def test_the_enqueue_hands_every_restore_to_the_gate_with_the_restore_intent(
        tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from opsb_core.config import ConfigRepository
    from scheduler.cli_bridge import SchedulerEngine

    ws, best, _, _ = _workspace(tmp_path)
    other = _add_second_problem(ws)
    engine = SchedulerEngine(workspace=ws, state_dir=tmp_path / "state")
    seen: list[dict] = []

    def fake_submit(**kwargs):
        seen.append(kwargs)
        return {"enqueued": True, "verdict": {"approved": True, "status_tag": "READY",
                                              "intent_code": "RESTORE_INTENT", "priority_score": 12.0,
                                              "gate_reason": "priced"}}

    monkeypatch.setattr(engine, "submit", fake_submit)
    doc = engine.close_sweep(enqueue=True)
    assert [call["title"] for call in seen] == [TITLE, other]
    assert {(call["sha"], call["intent"]) for call in seen} == {(best[:7], "restore")}
    assert all("force" not in call and "approve" not in call for call in seen)  # never forced or approved
    assert doc["enqueued"][0]["status_tag"] == "READY" and doc["enqueued"][0]["enqueued"] is True
    assert doc["enqueued"][0]["intent_code"] == "RESTORE_INTENT"
    assert ConfigRepository.load() is not None  # the run touched no config


def _run_cli(ws: pathlib.Path, home: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PARHELION_HOME"] = str(home)
    env["OPSB_HOME"] = str(home)
    env["OPSB_DEVICE_ISOLATED"] = "1"
    return subprocess.run([sys.executable, str(CLI_DIR / "opsb"), *args, "-w", str(ws)],
                          capture_output=True, text=True, env=env, timeout=180)


def test_opsb_close_sweep_json_reports_the_three_lists(tmp_path: pathlib.Path) -> None:
    ws, best, _, _ = _workspace(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    before = (ws / "results" / "submit-queue.jsonl").read_text()
    proc = _run_cli(ws, home, "close-sweep", "--json")
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    assert doc["totals"]["restore"] == 1 and doc["restore"][0]["best_sha"] == best[:7]
    assert doc["restore"][0]["gap"] == 5.0 and doc["closing"]["restore_boost"] == 3.0
    assert (ws / "results" / "submit-queue.jsonl").read_text() == before  # no --enqueue, no write
    assert not (home / "workspaces").exists() or not any((home / "workspaces").rglob("submit-queue.jsonl"))


def test_opsb_close_sweep_enqueue_queues_only_through_the_gate_and_keeps_the_auto_switch(
        tmp_path: pathlib.Path) -> None:
    ws, best, _, _ = _workspace(tmp_path)
    _validate(ws, best)
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps(
        {"scheduler": {"auto_consumer_enabled": False, "daily_quota_limit": 50}}))
    proc = _run_cli(ws, home, "close-sweep", "--enqueue", "--json")
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    assert doc["enqueued"] and doc["enqueued"][0]["title"] == TITLE
    assert doc["enqueued"][0]["approved"] is True, doc["enqueued"][0]
    rows = [json.loads(line) for line in
            (ws / "results" / "submit-queue.jsonl").read_text().splitlines() if line.strip()]
    assert len(rows) == 1 and rows[0]["sha"] == best and rows[0]["intent"] == "restore"
    assert "force" not in rows[0] and "approved" not in rows[0]
    after = json.loads((home / "config.json").read_text())
    assert after["scheduler"]["auto_consumer_enabled"] is False  # the switch is the administrator's
    again = _run_cli(ws, home, "close-sweep", "--enqueue", "--json")
    assert again.returncode == 0
    still = [line for line in (ws / "results" / "submit-queue.jsonl").read_text().splitlines() if line.strip()]
    assert len(still) == 1  # one row per SHA in flight: a second sweep adds nothing


def test_enqueue_once_requeues_a_restore_but_keeps_one_row_for_an_exploit(
        tmp_path: pathlib.Path) -> None:
    from opsb_core.services.queue_service import enqueue_once

    ws, best, _, _ = _workspace(tmp_path)
    (ws / "results" / "submit-queue-outcome.jsonl").write_text(json.dumps(
        {"title": TITLE, "sha": best, "status": "Pass", "score": 60.0, "submission_id": "s-best",
         "queued_at": "2026-11-20T00:00:00Z"}) + "\n")
    row = {"title": TITLE, "sha": best, "note": "n", "queued_at": "2026-11-26T00:00:00Z", "status": "queued"}
    assert enqueue_once(ws, {**row, "queued_at": "2026-11-26T00:00:01Z"}) is False       # 4.2.1 rule
    assert enqueue_once(ws, {**row, "queued_at": "2026-11-26T00:00:02Z", "intent": "restore"}) is True
    assert enqueue_once(ws, {**row, "queued_at": "2026-11-26T00:00:03Z", "intent": "restore"}) is False
    from opsb_core.config import workspace_state_dir

    state = workspace_state_dir(ws) / "scheduler_state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"config": {"submit_intents": False}}))
    assert enqueue_once(ws, {**row, "queued_at": "2026-11-26T00:00:04Z", "intent": "restore"}) is False


def test_the_tool_table_stays_within_the_budget() -> None:
    """The `close` view is an enum value, not a new tool: re-check the byte budget here too."""
    budget = {"orchestrator": 15_000, "worker": 13_000}
    for role, limit in budget.items():
        size = len(json.dumps(list_tool_schemas(role=role), ensure_ascii=False,
                              separators=(",", ":")).encode("utf-8"))
        assert size <= limit, (role, size)
        if role == "orchestrator":
            # `main` itself is 14951 of the 15000 budget (P21/P22/P23/P24/P26/P27/P28 merged), so the
            # goal is the ceiling plus a little air, not the old 14800 planning number.
            assert size <= 14_950, "the closing view must not push the orchestrator table to the ceiling"


def test_the_status_payload_carries_the_slot_rows_and_the_hint(tmp_path: pathlib.Path) -> None:
    """P25 slot utilisation reaches the status card and the brief; the gate never reads it."""
    from opsb_core.config import ConfigRepository
    from opsb_core.services.queue_service import compute_workspace_snapshot

    ws, *_ = _workspace(tmp_path)
    sched = compute_workspace_snapshot(ws, ConfigRepository.load())["scheduler"]
    assert isinstance(sched["per_slot"], list) and sched["per_slot"][0]["slot"] == 1
    assert set(sched["per_slot"][0]) == {"slot", "used", "limit", "utilization"}
    assert "slot_hint" in sched and isinstance(sched["day_elapsed_pct"], float)
    from opsb_core.mcp.handlers_bench import tool_parhelion_status

    brief = tool_parhelion_status({"format": "brief"}, ws)
    assert "slot_hint" in brief["scheduler"]
