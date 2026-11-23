"""Unit tests for Parhelion v3.0.4 fixes (V3-025, V3-026, V3-028 + #51 skip precedence)."""
from __future__ import annotations

import argparse
import datetime
import io
import json
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import pytest

CLI_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = CLI_DIR.parent
for p in (str(CLI_DIR), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from opsb_core.commands.queue_cmd import run_queue  # noqa: E402
from opsb_core.config import ConfigRepository  # noqa: E402
from opsb_core.daemon.state_cache import WorkspaceStateCache  # noqa: E402
from opsb_core.ipc_bridge import merge_bridge_enrichment  # noqa: E402
from opsb_core.services.judge_sync import compute_synchronized_pipeline  # noqa: E402
from opsb_core.services.queue_service import compute_workspace_snapshot  # noqa: E402
from opsb_core.services.submit_runner import _wait_and_fetch_score, spawn_jump_submit  # noqa: E402
from opsb_core.services.sync_reconciler import (  # noqa: E402
    collapse_reconciled_outcomes,
    find_orphan_receipts,
    reconcile_sync_errors,
)


@pytest.fixture()
def iso_ph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    p_home = tmp_path / "parhelion_home"
    p_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("PARHELION_HOME", str(p_home))
    monkeypatch.setenv("OPSB_PYTHON", sys.executable)
    monkeypatch.setenv("OPSB_DISABLE_SYNC_RECONCILE", "1")
    return p_home


def _setup_ws(tmp_path: Path, p_home: Path) -> tuple[Path, str]:
    ws = tmp_path / "ws"
    op_dir = ws / "bench" / "StridedSliceGrad"
    (op_dir / "op_host").mkdir(parents=True)
    (op_dir / "op_kernel").mkdir(parents=True)
    (op_dir / "op_kernel" / "k.cpp").write_text("// ok\n", encoding="utf-8")
    (ws / "results" / "scoring-evidence").mkdir(parents=True)
    (ws / "bench" / "problems.json").write_text(
        json.dumps([{"title": "StridedSliceGrad", "slug": "stridedslicegrad", "problem_id": "prob-1", "difficulty": 3, "path": "bench/StridedSliceGrad"}]),
        encoding="utf-8",
    )
    (ws / "results" / "score-snapshot.json").write_text(
        json.dumps({"StridedSliceGrad": {"score": 66.83, "status": "Pass", "rank": 15, "submission_id": "sid-prev", "submission_time": "2026-11-23T10:00:00+00:00", "public_times_us": [5.0] * 5}}),
        encoding="utf-8",
    )
    (ws / "results" / "submit-queue.jsonl").write_text("", encoding="utf-8")
    (ws / "results" / "submit-queue-outcome.jsonl").write_text("", encoding="utf-8")
    for cmd in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "Test"], ["add", "."], ["commit", "-q", "-m", "init"]):
        subprocess.run(["git", "-C", str(op_dir), *cmd], check=True)
    sha = subprocess.check_output(["git", "-C", str(op_dir), "rev-parse", "HEAD"], text=True).strip()
    vdir = ws / "results" / "StridedSliceGrad" / sha
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / "result.json").write_text(json.dumps({"exit_code": 0, "sha": sha}), encoding="utf-8")
    (vdir / "validation.log").write_text("EXTENDED_PASSED 40 cases\nEXTENDED_RC=0\n", encoding="utf-8")
    sess_dir = p_home / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()
    (sess_dir / "primary.json").write_text(json.dumps({"version": 1, "base_url": "https://judge.invalid", "user_id": "u1", "logged_in_at": now_ts, "cookies": [{"name": "c", "value": "1"}]}), encoding="utf-8")
    (p_home / "cj_accounts.json").write_text(json.dumps({"accounts": [{"id": "primary", "label": "主账号", "enabled": True}]}), encoding="utf-8")
    (p_home / "config.json").write_text(json.dumps({"active_workspace": str(ws), "workspaces": [str(ws)], "scheduler": {"auto_consumer_enabled": False, "daily_quota_limit": 50, "closing_reserve": 10, "noise_floor_delta": 1.9}}), encoding="utf-8")
    return ws, sha


def test_v3_025_strict_sid_match_no_user_id_fallback_and_retry_on_lag(iso_ph: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws, _ = _setup_ws(tmp_path, iso_ph)
    monkeypatch.setattr("opsb_core.services.submit_runner.resolve_python_exe", lambda _w: sys.executable)
    skill_scripts = ws / "skills" / "problem-submit" / "scripts"
    skill_scripts.mkdir(parents=True)
    counter_file = tmp_path / "rank_calls.txt"

    # 1. Ranking returns stale user_id='u1' (sid-prev, 66.83) on first 2 attempts, then sid-new (72.45) on 3rd!
    (skill_scripts / "ict_cli.py").write_text(
        "from pathlib import Path\n"
        f"COUNTER = Path({str(counter_file)!r})\n"
        "class _Resp:\n"
        "    status_code = 200\n"
        "    def __init__(self, payload): self._p = payload\n"
        "    def json(self): return self._p\n"
        "class _Sess:\n"
        "    def get(self, url, params=None, timeout=20):\n"
        "        n = int(COUNTER.read_text()) if COUNTER.exists() else 0\n"
        "        n += 1\n"
        "        COUNTER.write_text(str(n))\n"
        "        if n < 3:\n"
        "            return _Resp({'pages': 1, 'rows': [{'user_id': 'u1', 'submission_id': 'sid-prev', 'score': 66.83, 'rank': 15}]})\n"
        "        return _Resp({'pages': 1, 'rows': [{'user_id': 'u1', 'submission_id': 'sid-new', 'score': 72.45, 'rank': 8}]})\n"
        "class ICTClient:\n"
        "    BASE_URL = 'https://judge.invalid'\n"
        "    def __init__(self, session_file=None):\n"
        "        self.user_id = 'u1'\n"
        "        self.session = _Sess()\n"
        "    def load_session(self): return True\n"
        "    def get_submission(self, sid):\n"
        "        return {'status': 'Pass', 'result': [{'time': 3.4, 'best_time': 3.1}]}\n",
        encoding="utf-8",
    )

    with mock.patch("opsb_core.config.SUPERVISOR_ROOT", tmp_path / "none"):
        st, sc, pt, pb = _wait_and_fetch_score(ws, "StridedSliceGrad", "sid-new", ranking_retry_delays=(0.01, 0.01, 0.01, 0.01))
    assert st == "Pass"
    assert sc == 72.45
    assert int(counter_file.read_text()) == 3
    snap = json.loads((ws / "results" / "score-snapshot.json").read_text(encoding="utf-8"))
    assert snap["StridedSliceGrad"]["submission_id"] == "sid-new"
    assert snap["StridedSliceGrad"]["score"] == 72.45

    # 2. If ranking NEVER contains sid-other (only stale same-user sid-prev=66.83), MUST NOT return 66.83!
    (skill_scripts / "ict_cli.py").write_text(
        "class _Resp:\n"
        "    status_code = 200\n"
        "    def json(self):\n"
        "        return {'pages': 1, 'rows': [{'user_id': 'u1', 'submission_id': 'sid-prev', 'score': 66.83, 'rank': 15}]}\n"
        "class _Sess:\n"
        "    def get(self, url, params=None, timeout=20): return _Resp()\n"
        "class ICTClient:\n"
        "    BASE_URL = 'https://judge.invalid'\n"
        "    def __init__(self, session_file=None):\n"
        "        self.user_id = 'u1'\n"
        "        self.session = _Sess()\n"
        "    def load_session(self): return True\n"
        "    def get_submission(self, sid):\n"
        "        if sid == 'sid-with-sub-score':\n"
        "            return {'status': 'Pass', 'score': 70.12, 'result': [{'time': 3.6, 'best_time': 3.1}]}\n"
        "        return {'status': 'Pass', 'result': [{'time': 3.6, 'best_time': 3.1}]}\n",
        encoding="utf-8",
    )
    with mock.patch("opsb_core.config.SUPERVISOR_ROOT", tmp_path / "none"):
        st2, sc2, _, _ = _wait_and_fetch_score(ws, "StridedSliceGrad", "sid-other", ranking_retry_delays=(0.01, 0.01))
        st3, sc3, _, _ = _wait_and_fetch_score(ws, "StridedSliceGrad", "sid-with-sub-score", ranking_retry_delays=(0.01,))
    assert st2 == "Pass" and sc2 is None
    assert st3 == "Pass" and sc3 == 70.12


def test_v3_025_sync_reconciler_backfills_pass_score_none_strictly(iso_ph: Path, tmp_path: Path) -> None:
    ws, sha = _setup_ws(tmp_path, iso_ph)
    out_path = ws / "results" / "submit-queue-outcome.jsonl"
    recent_iso = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=30)).isoformat()
    out_path.write_text(
        json.dumps({
            "title": "StridedSliceGrad", "sha": sha, "note": "dad8589",
            "queued_at": recent_iso, "submitted_at": recent_iso, "finished_at": recent_iso,
            "status": "Pass", "submission_id": "sid-lag-pass", "score": None, "account_id": "primary",
        }) + "\n",
        encoding="utf-8",
    )

    # First reconcile sees lagging ranking with ONLY user_id='u1', sid='sid-prev' (66.83) -> must NOT adopt 66.83!
    def _lag_fetch(_base: str, _cookie: str, path: str, timeout: float = 5.0):
        if path == "/api/submissions/sid-lag-pass":
            return {"status": "Pass", "result": [{"time": 3.5, "best_time": 3.2}]}
        if "/ranking" in path:
            return {"pages": 1, "rows": [{"user_id": "u1", "submission_id": "sid-prev", "score": 66.83, "rank": 15}]}
        raise AssertionError(path)

    with mock.patch("opsb_core.services.sync_reconciler._fetch_json", side_effect=_lag_fetch):
        assert reconcile_sync_errors(ws, force=True) == []

    # Second reconcile sees sid-lag-pass appear on ranking with 73.40 -> reconciles & collapses cleanly
    def _ready_fetch(_base: str, _cookie: str, path: str, timeout: float = 5.0):
        if path == "/api/submissions/sid-lag-pass":
            return {"status": "Pass", "result": [{"time": 3.5, "best_time": 3.2}]}
        if "/ranking" in path:
            return {"pages": 1, "rows": [{"user_id": "u1", "submission_id": "sid-lag-pass", "score": 73.40, "rank": 4}]}
        raise AssertionError(path)

    with mock.patch("opsb_core.services.sync_reconciler._fetch_json", side_effect=_ready_fetch):
        fixed = reconcile_sync_errors(ws, force=True)
    assert len(fixed) == 1
    assert fixed[0]["score"] == 73.40
    assert fixed[0]["reconciled_from"] == "Pass"
    raw_rows = [json.loads(l) for l in out_path.read_text().splitlines() if l.strip()]
    collapsed = collapse_reconciled_outcomes(raw_rows)
    assert len(collapsed) == 1
    assert collapsed[0]["score"] == 73.40
    snap = json.loads((ws / "results" / "score-snapshot.json").read_text(encoding="utf-8"))
    assert snap["StridedSliceGrad"]["score"] == 73.40


def test_v3_026_submit_now_blocks_terminal_unless_forced(iso_ph: Path, tmp_path: Path) -> None:
    ws, sha = _setup_ws(tmp_path, iso_ph)
    out_path = ws / "results" / "submit-queue-outcome.jsonl"
    old_iso = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=3)).isoformat()
    slot = [{"id": "primary", "label": "主账号", "enabled": True, "logged_in": True, "phase": "idle", "active_title": None, "cooldown_remaining_sec": 0, "cooldown_ends_epoch": 0.0, "used_today": 1, "daily_limit": 50, "remaining_today": 49, "quota_exhausted": False}]

    for idx, st in enumerate(("Pass", "Wrong Answer", "Time Limit Exceeded", "Runtime Error", "Compile Error", "skipped", "sync_error")):
        rec = {
            "title": "StridedSliceGrad", "sha": sha,
            "queued_at": old_iso, "finished_at": old_iso, "submitted_at": old_iso,
            "status": st,
            **({"submission_id": f"sid-{idx}", "score": 66.02 if st == "Pass" else 0} if st != "skipped" else {}),
        }
        out_path.write_text(json.dumps(rec) + "\n", encoding="utf-8")

        # Even with '[RESTORE]' inside Pairwise-v2 note and >600s age, force=False rejects with duplicate_terminal!
        with mock.patch("opsb_core.services.submit_runner.compute_account_slots", return_value=slot):
            res = spawn_jump_submit(CLI_DIR / "opsb", ws, "StridedSliceGrad", sha, "Pairwise-v2 EXPLORE: new-best upside in [RESTORE] (est=72.77)", force=False)
            assert res["ok"] is False and res.get("duplicate_terminal") is True
        buf = io.StringIO()
        ns = argparse.Namespace(q_cmd="submit-now", problem="StridedSliceGrad", sha=sha, note="auto", account_id=None, force=False, workspace=str(ws))
        with mock.patch("opsb_core.services.submit_runner.compute_account_slots", return_value=slot), redirect_stdout(buf):
            assert run_queue(ns) == 2
        assert json.loads(buf.getvalue())["duplicate_terminal"] is True

    # Upload failure (`rejected` without submission_id) does NOT block retry when force=False
    out_path.write_text(json.dumps({"title": "StridedSliceGrad", "sha": sha, "queued_at": old_iso, "finished_at": old_iso, "status": "rejected", "detail": "HTTP 502"}) + "\n", encoding="utf-8")
    with (
        mock.patch("opsb_core.services.submit_runner.compute_account_slots", return_value=slot),
        mock.patch("opsb_core.services.submit_runner.subprocess.Popen", return_value=mock.MagicMock(pid=42423)),
    ):
        assert spawn_jump_submit(CLI_DIR / "opsb", ws, "StridedSliceGrad", sha, "retry", force=False)["ok"] is True

    # Manual explicit --force succeeds even with Pass outcome (spawns worker)
    out_path.write_text(json.dumps({"title": "StridedSliceGrad", "sha": sha, "queued_at": old_iso, "finished_at": old_iso, "submitted_at": old_iso, "status": "Pass", "submission_id": "sid-0", "score": 66.02}) + "\n", encoding="utf-8")
    with (
        mock.patch("opsb_core.services.submit_runner.compute_account_slots", return_value=slot),
        mock.patch("opsb_core.services.submit_runner.subprocess.Popen", return_value=mock.MagicMock(pid=42424)) as m_popen,
    ):
        assert spawn_jump_submit(CLI_DIR / "opsb", ws, "StridedSliceGrad", sha, "manual resubmit", force=True)["ok"] is True
    m_popen.assert_called_once()


def test_v3_028_orphan_receipt_adoption_pipeline_visibility_and_skip_precedence(iso_ph: Path, tmp_path: Path) -> None:
    ws, sha = _setup_ws(tmp_path, iso_ph)
    now = datetime.datetime.now(datetime.timezone.utc)
    sub_iso = (now - datetime.timedelta(seconds=660)).isoformat()  # 11m ago (>300s old cap, <2100s TTL)
    q_iso = (now - datetime.timedelta(seconds=662)).isoformat()
    skip_iso = (now - datetime.timedelta(seconds=60)).isoformat()

    # Exact V3-028 + #51 repro: forced queue row, orphan receipt (no inflight*.json), emergency skipped row (no sid)
    (ws / "results" / "submit-queue.jsonl").write_text(
        json.dumps({"title": "StridedSliceGrad", "sha": sha, "note": "Pairwise-v2 est=73.98", "queued_at": q_iso, "status": "queued", "estimated_score": 73.98, "force": True}) + "\n",
        encoding="utf-8",
    )
    (ws / "results" / "submit-queue-outcome.jsonl").write_text(
        json.dumps({"title": "StridedSliceGrad", "sha": sha, "note": "emergency skip", "queued_at": skip_iso, "finished_at": skip_iso, "status": "skipped"}) + "\n",
        encoding="utf-8",
    )
    (ws / "results" / "StridedSliceGrad" / sha / "submission-receipt.json").write_text(
        json.dumps({"submission_id": "6b04242a85ad43bbe09c2789", "validated_commit": sha, "submitted_at": sub_iso, "account_id": "primary", "user_id": "u1"}),
        encoding="utf-8",
    )

    # 1. Orphan discovery ignores `skipped` row without `submission_id`
    out_rows = [json.loads(l) for l in (ws / "results" / "submit-queue-outcome.jsonl").read_text().splitlines() if l.strip()]
    orphans = find_orphan_receipts(ws, out_rows)
    assert [o["submission_id"] for o in orphans] == ["6b04242a85ad43bbe09c2789"]

    # 2. When remote probe fails (None) at age=660s (>180s!), judge_sync MUST STILL keep it in active_submissions & inflight_keys
    with mock.patch("opsb_core.services.judge_sync._query_remote_submission_cached", return_value=None):
        pipe_none = compute_synchronized_pipeline(ws, outcome_rows=out_rows, queue_lock_held=False)
    assert f"StridedSliceGrad|{sha[:7]}" in pipe_none["inflight_keys"]
    assert len(pipe_none["active_submissions"]) == 1

    # 3. When remote is already terminal Pass at age=660s (>300s!), BOTH judge_sync & daemon state_cache MUST expose phase='fetching_score' + inflight_keys
    remote_pass = {"ok": True, "id": "6b04242a85ad43bbe09c2789", "judge_id": 1496282, "status": "Pass", "create_time": sub_iso, "vis_pass": 5, "total_cases": 5, "server_offset_sec": 0.0}
    with mock.patch("opsb_core.services.judge_sync._query_remote_submission_cached", return_value=remote_pass):
        pipe_fetch = compute_synchronized_pipeline(ws, outcome_rows=out_rows, queue_lock_held=False)
    assert pipe_fetch["phase"] == "fetching_score"
    assert f"StridedSliceGrad|{sha[:7]}" in pipe_fetch["inflight_keys"]
    assert pipe_fetch["active_submissions"][0]["phase"] == "fetching_score"

    cache = WorkspaceStateCache(ws)
    ent = cache._entry(ws)
    with mock.patch.object(ent, "_get_remote_info_nonblocking", return_value=remote_pass):
        hot_pipe, _ = ent.build_hot_pipeline_and_scheduler()
    assert hot_pipe["phase"] == "fetching_score"
    assert f"StridedSliceGrad|{sha[:7]}" in hot_pipe["inflight_keys"]

    # 4. Excluded from READY queue & unsubmitted_candidates even if outcome is empty or skipped
    snap_pre = compute_workspace_snapshot(ws, ConfigRepository.load())
    merge_bridge_enrichment(snap_pre, {
        "quota": {"daily_limit": 50, "used_today": 1, "reserved_for_closing": 10, "discretionary_remaining": 39},
        "config": {"noise_floor_pts": 1.9, "daily_limit": 50},
        "unsubmitted_candidates": [{"title": "StridedSliceGrad", "sha": sha, "approved": True, "status_tag": "READY", "priority_score": 20.0, "gate_reason": "ready", "stages": [], "estimate": {"estimated_score": 73.98, "current_score": 66.83}}],
    })
    assert not any(it["kind"] in ("pending", "candidate") and it["short_sha"] == sha[:7] for it in snap_pre["submission_queue"])

    # 5. reconcile_sync_errors adopts orphan receipt -> appends Pass 69.02, updates score-snapshot, and History overrides `skipped`!
    def _orphan_fetch(_base: str, _cookie: str, path: str, timeout: float = 5.0):
        if path == "/api/submissions/6b04242a85ad43bbe09c2789":
            return {"status": "Pass", "result": [{"time": 4.92, "best_time": 3.72}]}
        if "/ranking" in path:
            return {"pages": 1, "rows": [{"user_id": "u1", "submission_id": "6b04242a85ad43bbe09c2789", "score": 69.02, "rank": 14}]}
        raise AssertionError(path)

    with mock.patch("opsb_core.services.sync_reconciler._fetch_json", side_effect=_orphan_fetch):
        reconciled = reconcile_sync_errors(ws, force=True)
    assert len(reconciled) == 1
    assert reconciled[0]["status"] == "Pass"
    assert reconciled[0]["score"] == 69.02
    assert reconciled[0]["reconciled_from"] == "orphan_receipt"

    snap_disk = json.loads((ws / "results" / "score-snapshot.json").read_text(encoding="utf-8"))
    assert snap_disk["StridedSliceGrad"]["score"] == 69.02
    assert snap_disk["StridedSliceGrad"]["submission_id"] == "6b04242a85ad43bbe09c2789"
    assert snap_disk["StridedSliceGrad"]["rank"] == 14

    snap_post = compute_workspace_snapshot(ws, ConfigRepository.load())
    done_for_sha = [it for it in snap_post["submission_queue"] if it["kind"] == "completed" and it["short_sha"] == sha[:7]]
    assert len(done_for_sha) == 1
    assert done_for_sha[0]["raw_status"] == "Pass"
    assert done_for_sha[0]["actual_score"] == 69.02
    assert done_for_sha[0]["submission_id"] == "6b04242a85ad43bbe09c2789"
    assert not any(it["kind"] in ("pending", "candidate") and it["short_sha"] == sha[:7] for it in snap_post["submission_queue"])
    out_after = [json.loads(l) for l in (ws / "results" / "submit-queue-outcome.jsonl").read_text().splitlines() if l.strip()]
    pipe_post = compute_synchronized_pipeline(ws, outcome_rows=out_after, queue_lock_held=False)
    assert pipe_post["inflight_keys"] == []
    assert pipe_post["active_submissions"] == []


def test_v3_028_orphan_receipt_without_outcome_or_queue_file(iso_ph: Path, tmp_path: Path) -> None:
    ws, sha = _setup_ws(tmp_path, iso_ph)
    (ws / "results" / "submit-queue-outcome.jsonl").unlink()
    (ws / "results" / "submit-queue.jsonl").unlink()
    sub_iso = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=450)).isoformat()
    (ws / "results" / "StridedSliceGrad" / sha / "submission-receipt.json").write_text(
        json.dumps({"submission_id": "sid-orphan-bare", "validated_commit": sha, "submitted_at": sub_iso, "account_id": "primary", "user_id": "u1"}),
        encoding="utf-8",
    )

    def _fetch(_base: str, _cookie: str, path: str, timeout: float = 5.0):
        if path == "/api/submissions/sid-orphan-bare":
            return {"status": "Pass", "result": [{"time": 4.1, "best_time": 3.7}]}
        if "/ranking" in path:
            return {"pages": 1, "rows": [{"user_id": "u1", "submission_id": "sid-orphan-bare", "score": 71.25, "rank": 9}]}
        raise AssertionError(path)

    with mock.patch("opsb_core.services.sync_reconciler._fetch_json", side_effect=_fetch):
        reconciled = reconcile_sync_errors(ws, force=True)
    assert len(reconciled) == 1
    assert reconciled[0]["score"] == 71.25

    snap = compute_workspace_snapshot(ws, ConfigRepository.load())
    done_for_sha = [it for it in snap["submission_queue"] if it["kind"] == "completed" and it["short_sha"] == sha[:7]]
    assert len(done_for_sha) == 1
    assert done_for_sha[0]["raw_status"] == "Pass"
    assert done_for_sha[0]["actual_score"] == 71.25
