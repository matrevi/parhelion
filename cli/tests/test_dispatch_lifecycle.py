"""Concurrent producers, scheduling and the judge's upload acknowledgement."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from opsb_core.daemon.server import OpsbDaemonRuntime


ROOT = Path(__file__).resolve().parents[2]


def test_receipt_starts_only_its_account_cooldown(tmp_path, monkeypatch):
    import datetime as dt
    from opsb_core.services import submit_runner as runner

    now = dt.datetime.now(dt.timezone.utc)
    monkeypatch.setattr(runner, "utc_now", lambda: now)
    monkeypatch.setattr(runner, "list_judge_accounts", lambda: [
        {"id": name, "logged_in": True} for name in ("primary", "teammate")
    ])
    monkeypatch.setattr(runner, "inflight_file_for", lambda aid: tmp_path / aid)
    receipt = tmp_path / "results/Demo/abcdef/submission-receipt.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({"submission_id": "sid", "account_id": "primary",
                                   "submitted_at": (now - dt.timedelta(seconds=23)).isoformat()}))
    outcomes = [{"submission_id": "sid", "status": "judged", "finished_at": now.isoformat()}]
    slots = runner.compute_account_slots(tmp_path, outcomes, gap_sec=190)
    assert [s["cooldown_remaining_sec"] for s in slots] == [167, 0]
    monkeypatch.setattr(runner, "judge_cooldown_remaining", lambda ws: 45)
    slots = runner.compute_account_slots(tmp_path, outcomes, gap_sec=190)
    assert [s["cooldown_remaining_sec"] for s in slots] == [167, 45]


def test_simultaneous_producers_and_held_writers(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    code = """
import sys, time
from pathlib import Path
from scheduler.cli_bridge import SchedulerEngine
from cli.opsb_core.services.queue_service import enqueue_once
ws, title = Path(sys.argv[1]), sys.argv[2]
engine = SchedulerEngine(ws, state_dir=ws/'state')
(ws/title).touch()
while not (ws/'go').exists(): time.sleep(.01)
engine.held_items[title] = {'title': title}
engine._save_state()
enqueue_once(ws, {'title':'Shared', 'sha':'abcdef12345', 'queued_at':title})
"""
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PARHELION_HOME": str(tmp_path / "ph")}
    workers = [subprocess.Popen([sys.executable, "-c", code, str(workspace), title], env=env)
               for title in ("first", "second")]
    import time
    try:
        for _ in range(500):
            if all((workspace / t).exists() for t in ("first", "second")):
                break
            time.sleep(.01)
        else:
            pytest.fail("producer initialization timed out")
        (workspace / "go").touch()
        assert [p.wait(timeout=6) for p in workers] == [0, 0]
    finally:
        for p in workers:
            if p.poll() is None:
                p.kill()
                p.wait()
    assert len((workspace / "results/submit-queue.jsonl").read_text().splitlines()) == 1
    assert set(json.loads((workspace / "state/scheduler_state.json").read_text())["held_items"]) == {"first", "second"}


def test_dispatch_skips_held_and_busy_title(tmp_path, monkeypatch):
    runtime = Mock()
    launch = Mock(side_effect=[{"operator_inflight": True}, {"ok": True}])
    monkeypatch.setattr("opsb_core.daemon.server.spawn_jump_submit", launch)
    rows = [{"kind": "pending", "gates": {"all_pass": passed}, "title": t, "sha": "0123456789"}
            for t, passed in (("held", False), ("busy", True), ("ready", True), ("later", True))]
    rows.insert(0, {"kind": "candidate", "title": "tested-only", "sha": "0123456789", "gates": {"all_pass": True}})
    OpsbDaemonRuntime.dispatch_next(runtime, tmp_path, {"submission_queue": rows}, {})
    assert [c.args[2] for c in launch.call_args_list] == ["busy", "ready"]
    assert all(c.kwargs["enforce_operator_mutex"] for c in launch.call_args_list)


def test_promote_requested_candidate_preserves_worker(tmp_path, monkeypatch):
    promote = Mock(return_value={"enqueued": True})
    launch = Mock()
    monkeypatch.setattr("opsb_core.daemon.server.invoke_bridge", promote)
    monkeypatch.setattr("opsb_core.daemon.server.spawn_jump_submit", launch)
    row = dict(kind="candidate", requested_submission=True, title="Op", sha="abc1234",
               gates={"all_pass": True}, agent_id="worker", request_note="measured")
    OpsbDaemonRuntime.dispatch_next(Mock(), tmp_path, {"submission_queue": [row]}, {})
    assert promote.call_args.args == (tmp_path, "submit", [
        "--title", "Op", "--sha", "abc1234", "--note", "measured", "--agent-id", "worker"])
    launch.assert_not_called()


@pytest.mark.parametrize("outcome", ["packaging", "rejected", "early_exit"])
def test_manual_dispatch_waits_for_worker(tmp_path, monkeypatch, outcome):
    from opsb_core.services import submit_runner as runner
    from opsb_core.cli_app import build_parser

    args = build_parser().parse_args(["queue", "_worker-submit-one", "Op", "abcdef1",
                                     "--account-id", "demo", "--force"])
    assert args.force and args.account_id == "demo"

    worker = tmp_path / "worker.py"
    worker.write_text("import os, sys, json\n"
                      "if os.environ['OUTCOME'] == 'early_exit': sys.exit(2)\n"
                      "with os.fdopen(int(os.environ['OPSB_START_ACK_FD']), 'w') as ack:\n"
                      "    print(json.dumps({'ok': os.environ['OUTCOME'] == 'packaging', 'error': 'held'}), file=ack)\n")
    monkeypatch.setenv("OUTCOME", outcome)
    monkeypatch.setattr(runner, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(runner, "resolve_python_exe", lambda _: sys.executable)
    monkeypatch.setattr(runner, "inflight_file_for", lambda _: tmp_path / "inflight")
    monkeypatch.setattr(runner, "judge_cooldown_remaining", lambda _: 0)
    monkeypatch.setattr(runner, "compute_account_slots", lambda _: [
        dict(id="demo", phase="idle", label="Demo", logged_in=True, enabled=True)])
    result = runner.spawn_jump_submit(worker, tmp_path, "Op", "abcdef1", "test", force=True, confirm_start=True)
    assert result["ok"] == (outcome == "packaging")
    if outcome == "rejected":
        assert result["error"] == "held"
    if outcome == "early_exit":
        assert "exit 2" in result["error"]


def test_reference_acl_timings_remain_diagnostic():
    from scheduler.estimator import parse_validation_perf
    key = "a" * 64 + "-" + "b" * 64
    log = "\n".join(json.dumps(dict(case=k, dev_us=540.0)) for k in (key, "legacy-case"))
    assert parse_validation_perf(log)["dev_event_cases"] == {"legacy-case": 540.0}


def test_review_trial_binds_changed_root_kernel_and_receipt(tmp_path):
    from scheduler.case_feedback import authorize_review_trial
    def git(*args):
        return subprocess.check_output(["git", "-C", tmp_path, *args], text=True).strip()
    git("init", "-q")
    def commit(content):
        (tmp_path / "kernel.asc").write_text(content)
        git("add", "kernel.asc")
        git("-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-qm", content)
        return git("rev-parse", "HEAD")
    bad_sha, good_sha = commit("old"), commit("fixed")
    folder = tmp_path / bad_sha
    folder.mkdir()
    bad = dict(sha=bad_sha, run_id="old", engine="e", oracle="o",
               environment={"soc": "a"}, settings={"strict_fp32": True})
    good = dict(bad, sha=good_sha, run_id="new")
    state = dict(escape_lock=True, escape_sha=bad_sha, escape_submission_id="sid", escape_status="Wrong Answer")
    result = authorize_review_trial(tmp_path, folder, bad, good, state, "reviewed")
    assert result["ok"] and not result["cleared"]
    assert not authorize_review_trial(tmp_path, folder, bad, dict(good, sha=commit("different")), state, "new")["ok"]


def test_second_worker_uploads_while_first_is_polling(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from opsb_core.services import submit_runner as runner

    polling, second_upload, release = threading.Event(), threading.Event(), threading.Event()
    def upload(ws, title, sha, **kwargs):
        if title == "second":
            second_upload.set()
        return {"submission_id": title}

    def poll(ws, title, sid, **kwargs):
        if title == "first":
            polling.set()
            assert release.wait(5)
        return "sync_error", None, [], []

    monkeypatch.setattr(runner, "_submit_via_worktree", upload)
    monkeypatch.setattr(runner, "_wait_and_fetch_score", poll)
    monkeypatch.setattr(runner, "list_judge_accounts", lambda: [])
    monkeypatch.setattr(runner, "compute_account_slots", lambda ws: [])
    monkeypatch.setattr(runner, "inflight_file_for", lambda aid: tmp_path / "tracker.json")
    monkeypatch.setattr("opsb_core.ipc_bridge.invoke_bridge", lambda *args: {"approved": True})
    with ThreadPoolExecutor(2) as workers:
        first = workers.submit(runner.run_worker_submit_one, tmp_path, "first", "abcd123", "")
        try:
            assert polling.wait(4)
            second = workers.submit(runner.run_worker_submit_one, tmp_path, "second", "abcd234", "")
            assert second_upload.wait(3)
            assert second.result(timeout=3) == 0
        finally:
            release.set()
        assert first.result(timeout=3) == 0
    history = (tmp_path / "results/submit-queue-outcome.jsonl").read_text().splitlines()
    assert {json.loads(row)["submission_id"] for row in history} == {"first", "second"}


@pytest.mark.parametrize("mode", ["accepted", "existing", "cooldown"])
def test_judge_upload_receipt(tmp_path, monkeypatch, capsys, mode):
    spec = importlib.util.spec_from_file_location("platform_upload", ROOT / "runtime/submit-validated.py")
    uploader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(uploader)
    sha = "1" * 40
    result = tmp_path / "results/Demo" / sha
    result.mkdir(parents=True)
    (result / "result.json").write_text(json.dumps({"exit_code": 0, "sha": sha, "finished_at": "2026-01-01"}))
    (tmp_path / "opsb.workspace.json").write_text(json.dumps({"problems": [{"title":"Demo","path":"Demo","problem_id":"p"}]}))
    client = Mock(user_id="user", BASE_URL="https://judge.invalid")
    files = [{"path": "kernel.asc", "content": "kernel"}]
    client.prepare_submission.return_value = {"files": files}
    client._get_json.return_value = [{"_id":"sid","create_time":"2026-11-01T01:00:00Z"}] if mode == "existing" else []
    client.get_submission.return_value = {"files":files, "status":"Pass"}
    resp = client.session.post.return_value
    resp.status_code = 429 if mode == "cooldown" else 200
    resp.text = ""
    resp.json.return_value = {"data":{"retry_after_seconds": 17, "submissionId":"sid"}}
    monkeypatch.setattr(uploader, "ICTClient", lambda **kw: client)
    monkeypatch.setattr(uploader, "source_matches", lambda *args: True)
    monkeypatch.setattr(sys, "argv", ["submit", "Demo", "--workspace", str(tmp_path), "--sha", sha])
    monkeypatch.setenv("OPSB_SUBMIT_TRACE", str(tmp_path / "trace"))
    if mode == "cooldown":
        with pytest.raises(SystemExit) as err:
            uploader.main()
        assert err.value.code == 75
        assert json.loads(capsys.readouterr().out)["retry_after"] == 17
        assert not (result / "submission-receipt.json").exists()
    else:
        uploader.main()
        receipt = json.loads((result / "submission-receipt.json").read_text())
        assert receipt == json.loads(capsys.readouterr().out)
        assert receipt["submission_id"] == "sid"
        assert receipt["validated_commit"] == sha
    assert client.session.post.call_count == (0 if mode == "existing" else 1)
