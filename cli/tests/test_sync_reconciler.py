"""Unit tests for Wave A P0 fixes (V3-013 & V3-015): _wait_and_fetch_score, sync_error dedup & reconciliation."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

CLI_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = CLI_DIR.parent
for p in (str(CLI_DIR), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from opsb_core.commands.queue_cmd import run_submit  # noqa: E402
from opsb_core.mcp.handlers_bench import tool_parhelion_submit_gate  # noqa: E402
from opsb_core.services.contest_service import index_submission_history  # noqa: E402
from opsb_core.services.contest_service import displayed_score_bound, latest_score_evidence
from opsb_core.services.judge_score_fetch import reconstruct_timing_score
from opsb_core.services.queue_service import compute_workspace_snapshot  # noqa: E402
from opsb_core.services.submit_runner import _wait_and_fetch_score, spawn_jump_submit  # noqa: E402
from opsb_core.services.sync_reconciler import (  # noqa: E402
    collapse_reconciled_outcomes,
    find_unreconciled_sync_errors,
    has_pending_sync_error_for_commit,
    reconcile_sync_errors,
)
from scheduler.estimator import ScoreEstimator  # noqa: E402


def test_replay_uses_all_case_ids_and_checks_the_scoring_rule():
    rule = {"_id": "p", "score_mode": 1, "judge_revision": 0, "use_baseline": False,
            "ranking_submission_mode": "best", "testcases": [{"testcase_id": {"_id": "a"}}, {"testcase_id": "b"}]}
    times = [{"testcase_id": "b", "time": 3, "testcase_status": "Pass"},
             {"testcase_id": "a", "time": 1, "testcase_status": "Pass"}]
    detail = {"problem_id": "p", "valid": True, "status": "Pass", "result": times, "judge_revision": 0}
    cases = [{"_id": "a", "type": "default", "tbest": 1}, {"_id": "b", "type": "default", "tbest": 2}]
    ranked = {"score": 75, "status": "Pass", "result": list(reversed(times))}
    ranking = {"total_testcase_count": 2, "testcases": cases, "rows": [ranked]}
    assert reconstruct_timing_score(detail, rule, ranking)["score"] == 75
    zero = {"testcase_id": "c", "testcase_status": "Pass", "time": None, "score": 0}
    full = {**rule, "testcases": [*rule["testcases"], "c"]}
    counted = {**ranking, "testcases": [*cases, {"_id": "c", "type": "default", "tbest": None}],
               "total_testcase_count": 3, "rows": [{**ranked, "score": 50, "result": [*times, zero]}]}
    assert reconstruct_timing_score({**detail, "result": [*times, zero]}, full, counted)["score"] == 50
    assert reconstruct_timing_score({**detail, "result": [*times, {**zero, "time": 2}]}, full, counted) is None
    for changes in ({"result": times[:1]}, {"result": [times[0]] * 2}, {"public_only": True},
                    {"judge_revision": 1}, {"result": [times[0], {**times[1], "time": float("nan")}]},
                    {"status": "Judging"}):
        assert reconstruct_timing_score({**detail, **changes}, rule, ranking) is None
    for changes in ({"score_mode": 2}, {"use_baseline": True}, {"ranking_testcase_count": 1},
                    {"ranking_submission_mode": "latest"}, {"ranking_submission_mode": None}):
        assert reconstruct_timing_score(detail, {**rule, **changes}, ranking) is None
    assert reconstruct_timing_score(detail, rule, {**ranking, "rows": [{**ranked, "score": 74}]}) is None
    assert reconstruct_timing_score(detail, rule, {**ranking, "testcases": cases[:1]}) is None
    assert reconstruct_timing_score(detail, rule, {**ranking, "testcases": [cases[0], {**cases[1], "hidden": True}]}) is None

    roster = [str(i) for i in range(50)]
    public = [{"_id": k, "type": "default", "tbest": 1} for k in roster[:5]]
    partial = [{"testcase_id": k, "time": 1, "testcase_status": "Pass"} for k in roster[:5]]
    for count in (5, 50):
        payload = {"testcases": public, "total_testcase_count": count,
                   "rows": [{"status": "Pass", "score": 100, "result": partial}]}
        assert reconstruct_timing_score({**detail, "result": partial}, {**rule, "testcases": roster}, payload) is None


def test_replay_follows_submission_identity_and_preserves_official_zero():
    submission = {"status": "Pass", "submission_id": "new", "timing_score": {"score": 36.93},
                  "best_score_bound": {"score": 37.71, "board_submission_id": "old"}}
    board = {"listed": True, "our_score": 37.71, "board_submission_id": "old"}
    assert latest_score_evidence(submission, board) == (36.93, "official_timings")
    assert latest_score_evidence({**submission, "score": 0}, board) == (0, "submission")
    assert latest_score_evidence(submission, {**board, "board_submission_id": "new"}) == (37.71, "matching_board")
    assert displayed_score_bound(submission, "old") == 37.71
    assert displayed_score_bound(submission, "other") is None


def _make_ws(tmp_path: Path) -> tuple[Path, str]:
    ws = tmp_path / "ws"
    op_dir = ws / "bench" / "StridedSliceGrad"
    (op_dir / "op_host").mkdir(parents=True)
    (op_dir / "op_kernel").mkdir(parents=True)
    (op_dir / "op_kernel" / "k.cpp").write_text("// ok\n", encoding="utf-8")
    (ws / "results" / "scoring-evidence").mkdir(parents=True)
    (ws / "bench" / "problems.json").write_text(
        json.dumps([
            {
                "title": "StridedSliceGrad",
                "slug": "stridedslicegrad",
                "problem_id": "prob-1",
                "difficulty": 3,
                "path": "bench/StridedSliceGrad",
            }
        ]),
        encoding="utf-8",
    )
    (ws / "results" / "score-snapshot.json").write_text(
        json.dumps({"StridedSliceGrad": {"score": 71.57, "status": "Pass", "rank": 5, "submission_id": "old-sid", "public_times_us": [5.0] * 5}}),
        encoding="utf-8",
    )
    (ws / "results" / "submit-queue.jsonl").write_text("", encoding="utf-8")
    (ws / "results" / "submit-queue-outcome.jsonl").write_text("", encoding="utf-8")

    subprocess.run(["git", "-C", str(op_dir), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(op_dir), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(op_dir), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(op_dir), "add", "."], check=True)
    subprocess.run(["git", "-C", str(op_dir), "commit", "-q", "-m", "init"], check=True)
    sha = subprocess.run(["git", "-C", str(op_dir), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()

    val_dir = ws / "results" / "StridedSliceGrad" / sha
    val_dir.mkdir(parents=True)
    (val_dir / "result.json").write_text(json.dumps({"exit_code": 0, "sha": sha}), encoding="utf-8")
    (val_dir / "validation.log").write_text("EXTENDED_PASSED 40 cases\nEXTENDED_RC=0\n", encoding="utf-8")
    return ws, sha


@pytest.fixture()
def iso_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    p_home = tmp_path / "parhelion_home"
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("PARHELION_HOME", str(p_home))
    monkeypatch.setenv("OPSB_PYTHON", sys.executable)
    return p_home


@pytest.fixture()
def receipt_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fixtures date their receipts 2026-11-23; keep "now" inside the reconciler's 72 h window."""
    import datetime

    from opsb_core.services import sync_reconciler
    frozen = datetime.datetime(2026, 11, 23, 12, 0, tzinfo=datetime.timezone.utc)
    monkeypatch.setattr(sync_reconciler, "utc_now", lambda: frozen)


def _write_fake_ict_module(tmp_path: Path, *, ranking_ok: bool = True) -> Path:
    judge_dir = tmp_path / "fake_judge"
    judge_dir.mkdir(parents=True, exist_ok=True)
    ranking_body = (
        "class _Resp:\n"
        "    status_code = 200\n"
        "    def json(self):\n"
        "        return {'pages': 1, 'rows': [{'user_id': 'u1', 'submission_id': 'sid-123', 'score': 76.16, 'rank': 4}]}\n"
        if ranking_ok
        else
        "class _Resp:\n"
        "    status_code = 500\n"
        "    def json(self):\n"
        "        raise ValueError('boom')\n"
    )
    (judge_dir / "ict_cli.py").write_text(
        "class _Sess:\n"
        "    def get(self, url, params=None, timeout=20):\n"
        f"        return _Resp()\n"
        f"{ranking_body}"
        "class ICTClient:\n"
        "    BASE_URL = 'https://judge.invalid'\n"
        "    def __init__(self, session_file=None):\n"
        "        self.user_id = 'u1'\n"
        "        self.session = _Sess()\n"
        "    def load_session(self):\n"
        "        return True\n"
        "    def get_submission(self, sid):\n"
        "        return {'status': 'Pass', 'result': [{'time': 3.5, 'best_time': 3.2}]}\n",
        encoding="utf-8",
    )
    return judge_dir


def test_wait_and_fetch_score_defines_root_and_survives_ranking_failure(iso_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws, _ = _make_ws(tmp_path)
    monkeypatch.setattr("opsb_core.services.submit_runner.resolve_python_exe", lambda _w: sys.executable)

    # 1. Ranking succeeds -> updates score-snapshot.json and returns score
    judge_ok = _write_fake_ict_module(tmp_path / "ok", ranking_ok=True)
    with mock.patch("opsb_core.config.SUPERVISOR_ROOT", tmp_path / "none"):
        # Point fallback workspace/skills/problem-submit/scripts to fake_judge
        skill_scripts = ws / "skills" / "problem-submit" / "scripts"
        skill_scripts.mkdir(parents=True)
        (skill_scripts / "ict_cli.py").write_text((judge_ok / "ict_cli.py").read_text(encoding="utf-8"), encoding="utf-8")
        status, score, pub_times, pub_tbest = _wait_and_fetch_score(ws, "StridedSliceGrad", "sid-123")
        assert status == "Pass"
        assert score == 76.16
        assert pub_times == [3.5]
        assert pub_tbest == [3.2]
        snap = json.loads((ws / "results" / "score-snapshot.json").read_text(encoding="utf-8"))
        assert snap["StridedSliceGrad"]["score"] == 76.16
        assert snap["StridedSliceGrad"]["submission_id"] == "sid-123"

    # 2. Ranking fails -> non-fatal! Still returns status='Pass', score=None, pub_times, pub_tbest
    judge_fail = _write_fake_ict_module(tmp_path / "fail", ranking_ok=False)
    (skill_scripts / "ict_cli.py").write_text((judge_fail / "ict_cli.py").read_text(encoding="utf-8"), encoding="utf-8")
    with mock.patch("opsb_core.config.SUPERVISOR_ROOT", tmp_path / "none"):
        status2, score2, pub_times2, pub_tbest2 = _wait_and_fetch_score(ws, "StridedSliceGrad", "sid-124")
        assert status2 == "Pass"
        assert score2 is None
        assert pub_times2 == [3.5]
        assert pub_tbest2 == [3.2]


def test_sync_error_dedup_across_jump_submit_cli_and_mcp(iso_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws, sha = _make_ws(tmp_path)
    monkeypatch.setenv("OPSB_DISABLE_SYNC_RECONCILE", "1")
    out_path = ws / "results" / "submit-queue-outcome.jsonl"
    out_path.write_text(
        json.dumps({
            "title": "StridedSliceGrad",
            "sha": sha,
            "note": "mean ~ 76.5",
            "queued_at": "2026-11-23T08:40:11+00:00",
            "finished_at": "2026-11-23T08:48:27+00:00",
            "submitted_at": "2026-11-23T08:40:13+00:00",
            "status": "sync_error",
            "submission_id": "sid-sync-1",
            "account_id": "primary",
        }) + "\n",
        encoding="utf-8",
    )

    # 1. spawn_jump_submit blocks duplicate when force=False
    with mock.patch("opsb_core.services.submit_runner.compute_account_slots", return_value=[{"id": "primary", "label": "主账号", "enabled": True, "logged_in": True, "phase": "idle", "cooldown_remaining_sec": 0, "used_today": 1, "daily_limit": 50, "remaining_today": 49, "quota_exhausted": False}]):
        res = spawn_jump_submit(CLI_DIR / "opsb", ws, "StridedSliceGrad", sha, "retry")
        assert res["ok"] is False
        assert res["duplicate_sync_error"] is True

    # 2. run_submit blocks duplicate (exit code 4) unless --force
    ns = argparse.Namespace(problem="StridedSliceGrad", sha=sha, note="mean ~ 76.5", force=False, workspace=str(ws), json=True, now=False)
    with mock.patch("opsb_core.commands.queue_cmd.invoke_bridge", return_value=None):
        rc = run_submit(ns)
        assert rc == 4

    # 3. MCP tool_parhelion_submit_gate blocks duplicate when enqueue=True and force=False
    mcp_res = tool_parhelion_submit_gate(
        {"problem": "StridedSliceGrad", "sha": sha, "note": "mean ~ 76.5", "enqueue": True, "force": False},
        ws,
    )
    assert mcp_res["enqueued"] is False
    assert mcp_res.get("duplicate_sync_error") is True


def test_reconcile_sync_errors_self_heals_outcome_and_snapshot(iso_home: Path, tmp_path: Path, receipt_clock: None) -> None:
    ws, sha = _make_ws(tmp_path)
    sess_dir = iso_home / "sessions"
    sess_dir.mkdir(parents=True)
    (sess_dir / "primary.json").write_text(
        json.dumps({
            "version": 1,
            "base_url": "https://judge.invalid",
            "user_id": "u1",
            "logged_in_at": 9999999999,
            "cookies": [{"name": "sid", "value": "cookie1"}],
        }),
        encoding="utf-8",
    )
    (ws / "results" / "submit-queue.jsonl").write_text(
        json.dumps({"title": "StridedSliceGrad", "sha": sha, "note": "v2", "queued_at": "2026-11-23T08:40:11+00:00", "status": "queued"}) + "\n",
        encoding="utf-8",
    )
    (ws / "results" / "submit-queue-outcome.jsonl").write_text(
        json.dumps({
            "title": "StridedSliceGrad",
            "sha": sha,
            "note": "v2",
            "queued_at": "2026-11-23T08:40:11+00:00",
            "finished_at": "2026-11-23T08:48:27+00:00",
            "submitted_at": "2026-11-23T08:40:13+00:00",
            "status": "sync_error",
            "submission_id": "sid-heal-1",
            "account_id": "primary",
            "detail": "poll error: NameError",
        }) + "\n",
        encoding="utf-8",
    )

    def _fake_fetch(_base: str, _cookie: str, path: str, timeout: float = 5.0):
        if path == "/api/submissions/sid-heal-1":
            return {"status": "Pass", "result": [{"time": 3.4, "best_time": 3.1}]}
        if "/ranking" in path:
            return {"pages": 1, "rows": [{"user_id": "u1", "submission_id": "sid-heal-1", "score": 76.88, "rank": 3}]}
        raise AssertionError(path)

    with mock.patch("opsb_core.services.sync_reconciler._fetch_json", side_effect=_fake_fetch):
        reconciled = reconcile_sync_errors(ws, force=True, max_age_hours=0)  # fixed-date fixture

    assert len(reconciled) == 1
    assert reconciled[0]["status"] == "Pass"
    assert reconciled[0]["score"] == 76.88
    assert reconciled[0]["reconciled_from"] == "sync_error"

    # Snapshot updated
    snap_disk = json.loads((ws / "results" / "score-snapshot.json").read_text(encoding="utf-8"))
    assert snap_disk["StridedSliceGrad"]["score"] == 76.88
    assert snap_disk["StridedSliceGrad"]["submission_id"] == "sid-heal-1"

    # Collapsed readers see 1 Pass row, 0 sync_errors, and dedup no longer blocks
    raw_rows = [json.loads(l) for l in (ws / "results" / "submit-queue-outcome.jsonl").read_text().splitlines() if l.strip()]
    assert len(raw_rows) == 2
    assert len(collapse_reconciled_outcomes(raw_rows)) == 1
    assert find_unreconciled_sync_errors(raw_rows) == []
    assert has_pending_sync_error_for_commit(raw_rows, "StridedSliceGrad", sha) is None

    hist = index_submission_history(ws)
    assert len(hist["outcome_rows"]) == 1
    assert hist["outcome_rows"][0]["status"] == "Pass"

    est = ScoreEstimator(ws)
    assert len(est.outcomes) == 1
    assert est.outcomes[0]["status"] == "Pass"

    full_snap = compute_workspace_snapshot(ws, {"scheduler": {}})
    done_items = [x for x in full_snap["submission_queue"] if x["kind"] == "completed"]
    assert len(done_items) == 1
    assert done_items[0]["raw_status"] == "Pass"
    assert done_items[0]["actual_score"] == 76.88


def test_reconciled_wrong_answer_records_the_failing_case(iso_home: Path, tmp_path: Path, receipt_clock: None) -> None:
    """4.1 #8: every reconciled verdict carries the canonical receipt fields, WA included."""
    ws, sha = _make_ws(tmp_path)
    sess_dir = iso_home / "sessions"
    sess_dir.mkdir(parents=True)
    (sess_dir / "primary.json").write_text(json.dumps({
        "version": 1, "base_url": "https://judge.invalid", "user_id": "u1", "logged_in_at": 9999999999,
        "cookies": [{"name": "sid", "value": "cookie1"}]}), encoding="utf-8")
    (ws / "results" / "submit-queue-outcome.jsonl").write_text(json.dumps({
        "title": "StridedSliceGrad", "sha": sha, "queued_at": "2026-11-23T08:40:11+00:00",
        "finished_at": "2026-11-23T08:48:27+00:00", "submitted_at": "2026-11-23T08:40:13+00:00",
        "status": "sync_error", "submission_id": "sid-wa-1", "account_id": "primary",
        "detail": "poll error: NameError"}) + "\n", encoding="utf-8")
    detail = {"status": "Wrong Answer", "result": [
        {"testcase_id": "t1", "testcase_status": "Pass", "time": 3.4, "best_time": 3.1},
        {"testcase_id": "t2", "testcase_status": "Wrong Answer", "time": None}]}

    def _fake_fetch(_base: str, _cookie: str, path: str, timeout: float = 5.0):
        if path == "/api/submissions/sid-wa-1":
            return detail
        raise AssertionError(path)

    with mock.patch("opsb_core.services.sync_reconciler._fetch_json", side_effect=_fake_fetch), \
            mock.patch("scheduler.wa_repro.ingest", return_value=None) as ingest:
        reconciled = reconcile_sync_errors(ws, force=True, max_age_hours=0)  # fixed-date fixture
    assert len(reconciled) == 1
    # 4.1 #6: the appended WA verdict is handed to the reproducer planner once.
    assert ingest.call_count == 1 and ingest.call_args.args[1]["submission_id"] == "sid-wa-1"
    rows = [json.loads(line) for line in (ws / "results" / "submit-queue-outcome.jsonl").read_text().splitlines()]
    row = collapse_reconciled_outcomes(rows)[0]
    assert row["status"] == "Wrong Answer" and row["score"] == 0 and row["official_score"] == 0.0
    assert row["receipt_schema"] == 1 and row["beat_board"] is False
    assert [(c["testcase_id"], c["status"]) for c in row["case_results"]] == [("t1", "Pass"), ("t2", "Wrong Answer")]
    assert row["time_by_case"] == {"t1": 3.4, "t2": None} and row["tbest_by_case"] == {"t1": 3.1, "t2": None}
