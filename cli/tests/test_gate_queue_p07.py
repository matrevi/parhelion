"""4.1 #9-#12: queue lamps read the gate, honest approve output, judge messages, escape-lock MCP tools."""
from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import pytest

CLI_DIR = Path(__file__).resolve().parents[1]
for p in (str(CLI_DIR), str(CLI_DIR.parent)):
    if p not in sys.path:
        sys.path.insert(0, p)

from opsb_core.agents.bus_store import AgentBusStore  # noqa: E402
from opsb_core.commands import queue_cmd  # noqa: E402
from opsb_core.mcp import handlers_escape  # noqa: E402
from opsb_core.mcp.tools import call_tool  # noqa: E402
from opsb_core.services import failure_text, gate_view  # noqa: E402
from scheduler import candidate_archive, cli_bridge  # noqa: E402


def _stages(s1=True, s2=True, s3=True, s4=True, code="PASS"):
    return [{"stage": "SAFETY", "passed": s1, "code": code, "reason": "safety reason",
             "metrics": {} if s1 else {"next_step": "awaiting reproduction: run final on bad"}},
            {"stage": "NOISE_FLOOR", "passed": s2, "code": "EXPECTED_IMPROVEMENT", "reason": "E[gain] 2.1 >= 1.9"},
            {"stage": "CIRCUIT_BREAKER", "passed": s3, "code": "CIRCUIT_CLOSED", "reason": "breaker reason"},
            {"stage": "QUOTA_RESERVE", "passed": s4, "code": "QUOTA_OK" if s4 else "DAILY_LIMIT_REACHED",
             "reason": "no account slot is free until the reset (50/50 used today); quota detail"}]


@pytest.mark.parametrize("s1,s2,s3,s4,code,tag,badge", [
    (True, True, True, True, "PASS", "READY", "READY"),
    (True, True, True, True, "PASS", "EXPLORE", "EXPLORE"),
    (True, True, True, True, "PASS", "RATE_WAIT", "RATE WAIT"),
    (True, False, True, True, "PASS", "HOLD_LOW_GAIN", "HOLD: LOW GAIN"),
    (True, True, False, True, "PASS", "CIRCUIT_OPEN", "CIRCUIT OPEN"),
    (True, False, False, True, "PASS", "CIRCUIT_OPEN", "CIRCUIT OPEN"),
    (False, True, True, True, "ESCAPE_LOCK_ACTIVE", "REJECTED_GATE", "ESCAPE LOCK"),
    (False, True, True, True, "EXIT_CODE_2", "REJECTED_GATE", "TEST FAILED"),
    (False, True, True, True, "REFERENCE_SUITE_REQUIRED", "REJECTED_GATE", "NOT TESTED"),
    (True, True, True, False, "PASS", "QUOTA_EXHAUSTED", "WAITING FOR SLOT"),
    (True, False, True, False, "PASS", "HOLD_LOW_GAIN", "HOLD: LOW GAIN"),
])
def test_queue_lamps_mirror_every_gate_stage(s1, s2, s3, s4, code, tag, badge):
    # The dispatch stage never withholds approval: a candidate waiting for a slot stays approved.
    verdict = {"approved": s1 and s2 and s3, "status_tag": tag, "priority_score": 4.2,
               "stages": _stages(s1, s2, s3, s4, code), "estimate": {"delta_expected": 1.5, "ucb_delta": 3.0}}
    fields = gate_view.card_fields(verdict)
    gates = fields["gates"]
    assert (gates["safety_ok"], gates["noise_ok"], gates["circuit_ok"], gates["dispatch_ok"]) == (s1, s2, s3, s4)
    assert gates["all_pass"] == (s1 and s2 and s3) and gates["evaluated"] is True and "quota_ok" not in gates
    assert fields["badge"] == badge and gates["wspt_score"] == 4.2 and gates["ucb_delta"] == 3.0
    text = json.dumps(fields, ensure_ascii=False).lower()
    assert "quota" not in text and "额度" not in text  # the dispatch stage's reason never reaches a card
    assert fields["next_step"] == ("" if s1 else "awaiting reproduction: run final on bad")


def test_without_a_verdict_nothing_is_guessed():
    fields = gate_view.card_fields(None)
    assert fields["badge"] == "NOT EVALUATED" and fields["gates"]["evaluated"] is False
    assert fields["gates"]["all_pass"] is False and "Not evaluated" in fields["detail"]


def _bridge_ws(tmp_path):
    ws = tmp_path / "ws"
    (ws / "results").mkdir(parents=True)
    return ws


@pytest.mark.parametrize("case", ["queued", "already", "unsafe"])
def test_approve_reports_what_happened(tmp_path, case):
    ws = _bridge_ws(tmp_path)
    sha = "a" * 40
    verdict = {"sha": sha, "status_tag": "READY", "stages": _stages()}
    res = {"queued": {"action": "approve", "override_written": True, "enqueued": True, "already_queued": None,
                      "verdict": verdict},
           "already": {"action": "approve", "override_written": True, "enqueued": False,
                       "already_queued": {"queued_at": "2026-11-26T01:00:00+00:00", "forced": False,
                                          "status": "waiting in the submit queue"}, "verdict": verdict},
           "unsafe": {"action": "approve", "enqueued": False, "error": "Cannot approve unsafe commit — Stage 1 SAFETY failed: x"},
           }[case]
    args = argparse.Namespace(q_cmd="approve", problem="Op", sha=sha, note="", workspace=str(ws))
    out = io.StringIO()
    with mock.patch.object(queue_cmd, "invoke_bridge", return_value=res), \
            mock.patch.object(queue_cmd.ConfigRepository, "resolve_workspace", return_value=ws), redirect_stdout(out):
        rc = queue_cmd.run_queue(args)
    doc = json.loads(out.getvalue().strip().splitlines()[-1])
    if case == "queued":
        assert rc == 0 and doc["override_written"] and doc["enqueued"] and "already_queued" not in doc
    elif case == "already":
        assert rc == 0 and doc["override_written"] and doc["enqueued"] is False
        assert doc["already_queued"]["queued_at"] == "2026-11-26T01:00:00+00:00" and "override applies" in doc["reason"]
    else:
        assert rc == 2 and doc["override_written"] is False and doc["enqueued"] is False and "SAFETY" in doc["reason"]


def test_bridge_finds_the_row_still_waiting(tmp_path):
    ws = _bridge_ws(tmp_path)
    sha = "b" * 40
    rows = [{"title": "Op", "sha": sha, "queued_at": "t1", "force": False}, {"title": "Op", "sha": sha, "queued_at": "t2"}]
    (ws / "results" / "submit-queue.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (ws / "results" / "submit-queue-outcome.jsonl").write_text(
        json.dumps({"title": "Op", "sha": sha, "queued_at": "t1", "status": "Pass", "submission_id": "s"}) + "\n")
    assert cli_bridge._pending_queue_row(ws, "Op", sha)["queued_at"] == "t2"
    assert cli_bridge._pending_queue_row(ws, "Op", "c" * 40) is None


STACK = ('Traceback (most recent call last):\n  File "submit.py", line 202, in <module>\n    main()\n'
         '  File "judge_client.py", line 65, in _check_response\n    raise JudgeError(...)\n'
         'judge_client.JudgeError: HTTP 400: 提交代码中存在不合规内容，请检查并删除后提交。')


def test_rejected_rows_keep_the_judge_message_apart_from_the_stack(tmp_path):
    judge, local = failure_text.split(STACK + " | trace: {\"step\": \"posted\"}")
    assert judge == "提交代码中存在不合规内容，请检查并删除后提交。"
    assert local.startswith("judge_client.JudgeError: HTTP 400") and "Traceback" not in local
    with mock.patch.object(failure_text._cfg, "LOGS_DIR", tmp_path / "logs"):
        fields = failure_text.record_fields("Op", "a" * 40, STACK)
    assert fields["judge_message"] == judge and "Traceback" not in fields["detail"]
    assert Path(fields["local_error_log"]).read_text() == STACK
    # History rows written before 4.1 are split on read; a card shows the judge message, else the error.
    legacy = {"status": "rejected", "detail": STACK}
    assert failure_text.display(legacy) == judge
    plain = {"status": "rejected", "detail": "submit-validated.py exited 0 without a JSON receipt (stderr='')"}
    assert failure_text.display(plain) == plain["detail"] and failure_text.card_text(plain)["judge_message"] is None


def test_candidate_archive_keeps_the_incumbents_against_dead_ends():
    base = {"01": 10.0, "02": 100.0}
    archive = []
    for sha, score, minima in (("a", 20.0, base), ("b", 19.0, {"01": 8.0, "02": 90.0}), ("c", 18.0, {"01": 12.0, "02": 70.0})):
        archive = candidate_archive.admit(archive, {"sha": sha, "predicted": score, "minima": minima})
    assert [c["sha"] for c in archive] == ["a", "b", "c"]
    dead_end = candidate_archive.admit(archive, {"sha": "d", "predicted": 12.0, "minima": {"01": 30.0, "02": 300.0}})
    assert [c["sha"] for c in dead_end] == ["a", "b", "c"]
    twin = candidate_archive.admit(archive, {"sha": "e", "predicted": 19.5, "minima": {"01": 10.1, "02": 100.5}})
    assert [c["sha"] for c in twin] == ["a", "b", "c"]  # the same idea as a, scored lower: not a new candidate
    better = candidate_archive.admit(archive, {"sha": "f", "predicted": 21.0, "minima": {"01": 5.0, "02": 60.0}})
    assert [c["sha"] for c in better] == ["f", "a", "b"]


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def escape_ws(tmp_path, monkeypatch):
    monkeypatch.setenv("PARHELION_HOME", str(tmp_path / "home"))
    ws = tmp_path / "ws"
    repo = ws / "Op"
    repo.mkdir(parents=True)
    (ws / "opsb.workspace.json").write_text(json.dumps({"problems": [{"title": "Op", "path": "Op"}]}))
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    shas = []
    for n in range(3):
        (repo / "kernel.asc").write_text(f"kernel v{n}\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", f"v{n}")
        shas.append(_git(repo, "rev-parse", "HEAD"))
    for sha in shas:
        folder = ws / "results" / "Op" / sha
        folder.mkdir(parents=True)
        (folder / "result.json").write_text(json.dumps({"sha": sha, "exit_code": 0, "scheme": "reference", "run_id": "1" * 32}))
    (ws / "results" / "submit-queue-outcome.jsonl").write_text(json.dumps(
        {"title": "Op", "sha": shas[0], "status": "Wrong Answer", "submission_id": "sid-1", "score": 0,
         "submitted_at": "2026-11-26T01:00:00+00:00"}) + "\n")
    state = {"escape_lock": True, "escape_status": "Wrong Answer", "escape_sha": shas[0], "escape_submission_id": "sid-1"}
    evidence = lambda folder, repo, record: {"engine": "e", "oracle": "o", "environment": {"host": "x"},  # noqa: E731
                                             "settings": {"strict_fp32": True}, "run_id": "r-" + folder.name[:7],
                                             "sha": folder.name}
    monkeypatch.setattr(handlers_escape, "_escape_state", lambda workspace, title: state)
    monkeypatch.setattr("scheduler.case_bench.formal_evidence", evidence)
    AgentBusStore(ws).create_agent({"agent_id": "w-op", "driver": "fake", "problem": "Op"})
    AgentBusStore(ws).create_agent({"agent_id": "w-other", "driver": "fake", "problem": "Other"})
    return ws, shas


def _trial(ws, shas):
    files = list((ws / "results" / "Op" / shas[0]).glob("escape-trial-*.json"))
    return json.loads(files[0].read_text()) if files else None


def test_escape_rebind_follows_the_one_trial_rule(escape_ws):
    ws, shas = escape_ws
    call = lambda **a: call_tool("parhelion_escape_rebind", {"problem": "Op", "receipt": "sid-1", **a}, ws)  # noqa: E731
    assert call(sha=shas[1])["ok"] and _trial(ws, shas)["good_sha"] == shas[1]
    second = call(sha=shas[2], review="a better repair")
    assert second["ok"] and second["replaces"] == shas[1] and _trial(ws, shas)["good_sha"] == shas[2]
    # Once the bound repair was submitted the binding is final; a refusal leaves the file untouched.
    with (ws / "results" / "submit-queue-outcome.jsonl").open("a") as fh:
        fh.write(json.dumps({"title": "Op", "sha": shas[2], "status": "Pass", "submission_id": "sid-2"}) + "\n")
    before = _trial(ws, shas)
    refused = call(sha=shas[1])
    assert refused["ok"] is False and "already bound" in refused["reason"] and _trial(ws, shas) == before
    wrong = call_tool("parhelion_escape_rebind", {"problem": "Op", "receipt": "sid-x", "sha": shas[1]}, ws)
    assert wrong["ok"] is False and "not the problem's active" in wrong["reason"]


def test_escape_tools_scope_workers_to_their_problem(escape_ws):
    ws, shas = escape_ws
    status = call_tool("parhelion_escape_status", {"problem": "op"}, ws, role="worker", agent_id="w-op")
    assert status["problem"] == "Op" and status["escape_lock"] and status["locks"][0]["active"]
    assert status["locks"][0]["repro"]["state"] == "none" and "wa-repro" in status["next_step"]
    with pytest.raises(PermissionError):
        call_tool("parhelion_escape_status", {"problem": "Op"}, ws, role="worker", agent_id="w-other")
    with pytest.raises(PermissionError):
        call_tool("parhelion_escape_rebind", {"problem": "Op", "receipt": "sid-1", "sha": shas[1]}, ws, role="worker")
    assert call_tool("parhelion_escape_status", {"problem": "Op"}, ws)["active_sha"] == shas[0][:7]  # orchestrator
    none = call_tool("parhelion_escape_reproducer", {"problem": "Op", "receipt": "sid-1"}, ws, role="worker", agent_id="w-op")
    assert none["state"] == "none" and "wa-repro" in none["next_step"]


def test_escape_reproducer_returns_the_exact_test_call(escape_ws):
    from scheduler import wa_repro

    ws, shas = escape_ws
    manifest = wa_repro.manifest_path(ws, "Op")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"receipts": {"sid-1": {"sha": shas[0], "variants": ["local/wa-x-00", "local/wa-x-01"]}}}))
    out = call_tool("parhelion_escape_reproducer", {"problem": "Op", "receipt": "sid-1"}, ws, role="worker", agent_id="w-op")
    assert out["state"] == "pending" and out["variants"] == ["local/wa-x-00", "local/wa-x-01"]
    assert out["reproduce"] == {"tool": "parhelion_test", "arguments": {"problem": "Op", "sha": shas[0], "level": "final"}}
    assert out["verify_fix"]["arguments"]["level"] == "final" and "awaiting reproduction" in out["next_step"]
