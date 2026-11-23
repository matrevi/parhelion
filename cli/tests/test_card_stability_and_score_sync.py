"""Regression tests for card stability, cold/hot ETag monotonicity, NPU handoff smoothing & team score sync."""
from __future__ import annotations

import datetime
import json
import os
import sys
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

CLI_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = CLI_DIR.parent
for p in (str(CLI_DIR), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from opsb_core.config import ConfigRepository  # noqa: E402
from opsb_core.daemon.npu_stream import NpuStreamManager  # noqa: E402
from opsb_core.daemon.state_cache import WorkspaceStateCache  # noqa: E402
from opsb_core.ipc_bridge import merge_bridge_enrichment  # noqa: E402
from opsb_core.services.board_refresh_service import (  # noqa: E402
    inspect_board_freshness,
    refresh_board_and_recalibrate,
)
from opsb_core.services.inflight_io import (  # noqa: E402
    read_inflight,
    reap_inflight_if_stale,
    write_inflight_atomic,
    write_inflight_unless_owned,
)
from opsb_core.services.judge_sync import _inspect_local_worker, compute_synchronized_pipeline  # noqa: E402
from opsb_core.services.npu_service import enrich_npu_payload  # noqa: E402
from opsb_core.services.queue_service import compute_workspace_snapshot  # noqa: E402
from opsb_core.services.submit_runner import (  # noqa: E402
    _wait_and_fetch_score,
    compute_account_slots,
    inflight_file_for,
)


@pytest.fixture()
def iso_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    p_home = tmp_path / "parhelion_home"
    p_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("PARHELION_HOME", str(p_home))
    monkeypatch.setenv("OPSB_PYTHON", sys.executable)
    monkeypatch.setenv("OPSB_DISABLE_SYNC_RECONCILE", "1")
    monkeypatch.setenv("OPSB_DISABLE_BOARD_REFRESH", "1")
    return p_home


def _make_ws(tmp_path: Path, p_home: Path) -> Path:
    ws = tmp_path / "ws"
    for title, pid, diff in [("ConcatD", "p-concat", 3), ("PadV3Grad", "p-pad", 3)]:
        git_dir = ws / "bench" / title / ".git" / "refs" / "heads"
        git_dir.mkdir(parents=True, exist_ok=True)
        (ws / "bench" / title / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git_dir / "main").write_text("1111111222222233333334444444555555566666\n", encoding="utf-8")
    (ws / "results" / "scoring-evidence").mkdir(parents=True, exist_ok=True)
    (ws / "bench" / "problems.json").write_text(
        json.dumps([
            {"title": "ConcatD", "slug": "concatd", "problem_id": "p-concat", "difficulty": 3, "path": "bench/ConcatD"},
            {"title": "PadV3Grad", "slug": "padv3grad", "problem_id": "p-pad", "difficulty": 3, "path": "bench/PadV3Grad"},
        ]),
        encoding="utf-8",
    )
    (ws / "results" / "score-snapshot.json").write_text(
        json.dumps({
            "ConcatD": {"score": 80.0, "status": "Pass", "rank": 5, "submission_id": "sid-u1", "public_times_us": [2.0] * 5},
            "PadV3Grad": {"score": 70.0, "status": "Pass", "rank": 10, "submission_id": "sid-p1", "public_times_us": [3.0] * 5},
        }),
        encoding="utf-8",
    )
    (ws / "results" / "submit-queue.jsonl").write_text("", encoding="utf-8")
    (ws / "results" / "submit-queue-outcome.jsonl").write_text("", encoding="utf-8")

    # Configure two logged-in teammate accounts
    sess_dir = p_home / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    s1 = sess_dir / "primary.json"
    s2 = sess_dir / "teammate-2.json"
    exp = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=12)).isoformat()
    s1.write_text(json.dumps({"base_url": "https://judge.invalid", "user_id": "u1", "expires_at": exp, "cookies": [{"name": "sid", "value": "c1"}]}), encoding="utf-8")
    s2.write_text(json.dumps({"base_url": "https://judge.invalid", "user_id": "u2", "expires_at": exp, "cookies": [{"name": "sid", "value": "c2"}]}), encoding="utf-8")
    (p_home / "cj_accounts.json").write_text(
        json.dumps([
            {"id": "primary", "label": "主账号", "enabled": True, "is_primary": True, "session_file": str(s1)},
            {"id": "teammate-2", "label": "队友2", "enabled": True, "is_primary": False, "session_file": str(s2)},
        ]),
        encoding="utf-8",
    )
    return ws


def test_cold_recompute_monotonic_no_rollback_and_live_npu_injection(iso_env: Path, tmp_path: Path) -> None:
    ws = _make_ws(tmp_path, iso_env)
    cache = WorkspaceStateCache(ws)
    ent = cache._entry(ws)

    slow_entered = threading.Event()
    slow_release = threading.Event()
    call_idx = [0]
    real_compute = compute_workspace_snapshot

    def _controlled_compute(workspace: Path, cfg: dict) -> dict:
        idx = call_idx[0]
        call_idx[0] += 1
        snap = real_compute(workspace, cfg)
        if idx == 0:
            # Slow older recompute with score 80.0
            slow_entered.set()
            slow_release.wait(timeout=5.0)
        return snap

    with (
        mock.patch("opsb_core.daemon.state_cache.compute_workspace_snapshot", side_effect=_controlled_compute),
        mock.patch("opsb_core.daemon.state_cache.invoke_bridge", return_value=None),
        mock.patch("opsb_core.daemon.state_cache.probe_backfill_compact", return_value=None),
        mock.patch("opsb_core.daemon.state_cache.refresh_if_stale_async", return_value={"stale": False}),
        mock.patch("opsb_core.daemon.state_cache.get_auth_status", return_value={"judge": {"logged_in": False, "session_file": "", "base_url": ""}, "judge_accounts": [], "gitcode": {"logged_in": False, "cli_path": ""}}),
        mock.patch("opsb_core.daemon.state_cache.get_skills_status", return_value={"mode": "agents-md", "available_skills": [], "allowed_targets": [], "targets": {}}),
        mock.patch("opsb_core.services.ranking_service.team_ranking", return_value=None),
    ):
        # Start slow background recompute (seq=1)
        ent.ensure_cold_fresh(synchronous=False)
        assert slow_entered.wait(timeout=3.0)

        # Mutate score-snapshot and trigger newer synchronous recompute via invalidate (seq=2)
        (ws / "results" / "score-snapshot.json").write_text(
            json.dumps({
                "ConcatD": {"score": 91.25, "status": "Pass", "rank": 2, "submission_id": "sid-new", "public_times_us": [1.5] * 5},
                "PadV3Grad": {"score": 70.0, "status": "Pass", "rank": 10, "submission_id": "sid-p1", "public_times_us": [3.0] * 5},
            }),
            encoding="utf-8",
        )
        new_etag = cache.invalidate(ws)
        assert new_etag

        # Now let the older slow recompute finish — it MUST NOT roll back etag or score!
        slow_release.set()
        time.sleep(0.15)

        code, etag_after, raw = cache.get_snapshot(
            ws,
            live_npu={"online": True, "ssh_host": "npu", "die_locks": [{"die": 0, "state": "BUSY"}], "tasks": [{"pid": 1, "problem": "ConcatD", "state": "RUNNING"}]},
        )
        assert code == 200
        assert etag_after == new_etag
        snap_out = json.loads(raw.decode("utf-8"))
        assert snap_out["problems"][0]["current_score"] == 91.25
        # Live NPU injected without changing ETag
        assert snap_out["npu"]["online"] is True
        assert snap_out["npu"]["tasks"][0]["problem"] == "ConcatD"

        # Matching If-None-Match still returns 304
        code304, etag304, body304 = cache.get_snapshot(ws, if_none_match=f'"{new_etag}"', live_npu={"online": True, "tasks": []})
        assert code304 == 304
        assert etag304 == new_etag
        assert body304 == b""


def test_receipt_in_existing_sha_dir_immediately_detected_in_hot_and_cold(iso_env: Path, tmp_path: Path) -> None:
    ws = _make_ws(tmp_path, iso_env)
    sha = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
    sha_dir = ws / "results" / "ConcatD" / sha
    sha_dir.mkdir(parents=True, exist_ok=True)
    # Pre-existing validation artifact inside `<sha>` dir BEFORE submission (same as real opsb test -> submit flow)
    (sha_dir / "result.json").write_text(json.dumps({"exit_code": 0}), encoding="utf-8")

    cache = WorkspaceStateCache(ws)
    ent = cache._entry(ws)
    fp_before = ent.compute_cold_fingerprint()
    # Warm `_get_receipts_cached` with an earlier receipt so `_cached_receipts` is non-empty
    old_sha = "0000000111111122222223333333444444455555"
    old_dir = ws / "results" / "PadV3Grad" / old_sha
    old_dir.mkdir(parents=True, exist_ok=True)
    old_iso = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=3)).isoformat()
    (old_dir / "submission-receipt.json").write_text(
        json.dumps({"submission_id": "sid-old", "validated_commit": old_sha, "submitted_at": old_iso, "account_id": "primary"}),
        encoding="utf-8",
    )
    assert len(ent._get_receipts_cached()) == 1

    # Write new submission-receipt.json inside the PRE-EXISTING `ConcatD/<sha>` dir (does NOT bump `results/ConcatD` mtime!)
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    (sha_dir / "submission-receipt.json").write_text(
        json.dumps({"submission_id": "sid-fresh", "validated_commit": sha, "submitted_at": now_iso, "account_id": "primary"}),
        encoding="utf-8",
    )
    fp_after = ent.compute_cold_fingerprint()
    assert fp_after != fp_before

    with mock.patch.object(ent, "_get_remote_info_nonblocking", return_value=None):
        pipe, _ = ent.build_hot_pipeline_and_scheduler()
    assert f"ConcatD|{sha[:7]}" in pipe["inflight_keys"]
    assert any(a["id"] == f"act-primary-ConcatD-{sha[:7]}" and a["remote_submission_id"] == "sid-fresh" for a in pipe["active_submissions"])


def test_torn_inflight_json_never_unlinked_and_owner_guard(iso_env: Path, tmp_path: Path) -> None:
    ws = _make_ws(tmp_path, iso_env)
    inf_path = inflight_file_for("primary")
    # Simulate a half-written/truncated file from a legacy writer
    inf_path.write_text('{"pid": 12345, "phase": "up', encoding="utf-8")

    assert read_inflight(inf_path) is None
    assert reap_inflight_if_stale(inf_path, None) is False
    # Neither compute_account_slots nor _inspect_local_worker may unlink a torn file
    _ = compute_account_slots(ws)
    _ = _inspect_local_worker(queue_lock_held=False)
    assert inf_path.is_file()

    # Atomic write + owner-aware guard prevents spawner from regressing worker phase
    my_pid = os.getpid()
    write_inflight_atomic(
        inf_path,
        {"pid": my_pid, "title": "ConcatD", "sha": "abcdef1234567", "phase": "uploading", "started_at": "2026-11-23T10:00:00+00:00"},
    )
    overwritten = write_inflight_unless_owned(
        inf_path,
        {"pid": my_pid, "title": "ConcatD", "sha": "abcdef1234567", "phase": "packaging", "started_at": "2026-11-23T10:00:01+00:00"},
    )
    assert overwritten is False
    assert read_inflight(inf_path)["phase"] == "uploading"


def test_bridge_transient_timeout_preserves_last_good_candidates(iso_env: Path, tmp_path: Path) -> None:
    ws = _make_ws(tmp_path, iso_env)
    cache = WorkspaceStateCache(ws)
    ent = cache._entry(ws)
    cand_sha = "fedcba9876543210fedcba9876543210fedcba98"
    good_bridge = {
        "quota": {"daily_limit": 50, "used_today": 0, "reserved_for_closing": 2, "discretionary_remaining": 48},
        "config": {"noise_floor_pts": 2.0, "daily_limit": 50},
        "unsubmitted_candidates": [
            {
                "title": "ConcatD",
                "sha": cand_sha,
                "approved": True,
                "status_tag": "READY",
                "priority_score": 18.0,
                "gate_reason": "ready",
                "stages": [{"stage": "SAFETY", "passed": True}, {"stage": "NOISE_FLOOR", "passed": True}, {"stage": "CIRCUIT_BREAKER", "passed": True}, {"stage": "QUOTA_RESERVE", "passed": True}],
                "estimate": {"estimated_score": 86.0, "current_score": 80.0, "delta_expected": 6.0},
            }
        ],
    }
    bridge_calls = [good_bridge, None]

    with (
        mock.patch("opsb_core.daemon.state_cache.invoke_bridge", side_effect=lambda *a, **kw: bridge_calls.pop(0)),
        mock.patch("opsb_core.daemon.state_cache.probe_backfill_compact", return_value=None),
        mock.patch("opsb_core.daemon.state_cache.refresh_if_stale_async", return_value={"stale": False}),
        mock.patch("opsb_core.daemon.state_cache.get_auth_status", return_value={"judge": {"logged_in": False, "session_file": "", "base_url": ""}, "judge_accounts": [], "gitcode": {"logged_in": False, "cli_path": ""}}),
        mock.patch("opsb_core.daemon.state_cache.get_skills_status", return_value={"mode": "agents-md", "available_skills": [], "allowed_targets": [], "targets": {}}),
        mock.patch("opsb_core.services.ranking_service.team_ranking", return_value=None),
    ):
        ent.ensure_cold_fresh(synchronous=True)
        assert any(it["id"] == f"live-ConcatD-{cand_sha[:7]}" for it in ent.cold_payload["submission_queue"])

        # Second recompute where bridge transiently times out (returns None): candidate MUST stay visible!
        cache.invalidate(ws)
        assert any(it["id"] == f"live-ConcatD-{cand_sha[:7]}" for it in ent.cold_payload["submission_queue"])


def test_quota_exhausted_badge_bridge_sync_and_deterministic_sort(iso_env: Path, tmp_path: Path) -> None:
    ws = _make_ws(tmp_path, iso_env)
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    sha_a = "aaaaaaa111111111111111111111111111111111"
    sha_b = "bbbbbbb222222222222222222222222222222222"
    (ws / "results" / "submit-queue.jsonl").write_text(
        "\n".join([
            json.dumps({"title": "PadV3Grad", "sha": sha_b, "note": "est 75", "queued_at": now_iso, "status": "queued", "estimated_score": 75.0}),
            json.dumps({"title": "ConcatD", "sha": sha_a, "note": "est 85", "queued_at": now_iso, "status": "queued", "estimated_score": 85.0}),
        ]) + "\n",
        encoding="utf-8",
    )

    cfg = ConfigRepository.load()
    # Both have identical delta=5.0 * diff=3 -> wspt=15.0; deterministic tie-breaker sorts ConcatD before PadV3Grad
    snap = compute_workspace_snapshot(ws, cfg)
    pending = [it for it in snap["submission_queue"] if it["kind"] == "pending"]
    assert [it["title"] for it in pending] == ["ConcatD", "PadV3Grad"]
    assert [it["id"] for it in pending] == [f"live-ConcatD-{sha_a[:7]}", f"live-PadV3Grad-{sha_b[:7]}"]

    # Bridge upgrades ConcatD to QUOTA_EXHAUSTED -> badge MUST sync with gates (never stay READY!)
    merge_bridge_enrichment(
        snap,
        {
            "quota": {"daily_limit": 50, "used_today": 50, "reserved_for_closing": 2, "discretionary_remaining": 0},
            "config": {"noise_floor_pts": 2.0, "daily_limit": 50},
            "unsubmitted_candidates": [
                {
                    "title": "ConcatD",
                    "sha": sha_a,
                    "approved": False,
                    "status_tag": "QUOTA_EXHAUSTED",
                    "priority_score": 15.0,
                    "gate_reason": "daily limit reached",
                    "stages": [
                        {"stage": "SAFETY", "passed": True},
                        {"stage": "NOISE_FLOOR", "passed": True},
                        {"stage": "CIRCUIT_BREAKER", "passed": True},
                        {"stage": "QUOTA_RESERVE", "passed": False},
                    ],
                    "estimate": {"estimated_score": 85.0, "current_score": 80.0, "delta_expected": 5.0},
                }
            ],
        },
    )
    c_item = next(it for it in snap["submission_queue"] if it["title"] == "ConcatD")
    assert c_item["badge"] == "QUOTA EXHAUSTED"
    assert c_item["gates"]["quota_ok"] is False
    assert c_item["gates"]["all_pass"] is False

    # Closing reserve exhaustion in queue_service (without bridge) -> HOLD: RESERVE, never READY
    cfg["scheduler"]["closing_reserve"] = 200
    snap_res = compute_workspace_snapshot(ws, cfg)
    for it in snap_res["submission_queue"]:
        if it["kind"] == "pending":
            assert it["badge"] == "HOLD: RESERVE"
            assert it["gates"]["quota_ok"] is False

    # Full daily quota exhaustion across all accounts -> judge_sync idle `can_submit` MUST be False (hot-cold parity)
    fake_slots = [
        {"id": "primary", "label": "主账号", "logged_in": True, "enabled": True, "cooldown_remaining_sec": 0, "cooldown_ends_epoch": 0.0, "used_today": 50, "daily_limit": 50, "remaining_today": 0, "quota_exhausted": True, "phase": "idle", "active_title": None},
    ]
    with mock.patch("opsb_core.services.submit_runner.compute_account_slots", return_value=fake_slots):
        pipe = compute_synchronized_pipeline(ws, outcome_rows=[], queue_lock_held=False)
    assert pipe["phase"] == "idle"
    assert pipe["can_submit"] is False


def test_npu_stream_handoff_smoothing_and_deterministic_sort() -> None:
    mgr = NpuStreamManager(host="npu", interval=1.0)
    t0 = 1000.0

    # Frame 1: ConcatD in Phase A CPU build, two queued tasks with unsorted order
    f1 = enrich_npu_payload(
        {
            "die_locks": [{"die": 1, "lock_path": "/tmp/d1", "state": "FREE", "elapsed_sec": 0}, {"die": 0, "lock_path": "/tmp/d0", "state": "FREE", "elapsed_sec": 0}],
            "parallel_cpu_builds": [{"pid": 200, "title": "ConcatD", "sha": "abc1234", "short_sha": "abc1234", "elapsed_sec": 5}],
            "tasks": [
                {"pid": 302, "state": "WAITING", "stage": "WAITING-DIE", "lane": "Q1", "problem": "PadV3Grad", "elapsed_sec": 3, "detail": "", "subprocesses": 1},
                {"pid": 301, "state": "WAITING", "stage": "WAITING-DIE", "lane": "Q0", "problem": "LayerNormV3", "elapsed_sec": 1, "detail": "", "subprocesses": 1},
            ],
        },
        host="npu",
        mac_waiters=[],
    )
    assert [lk["die"] for lk in f1["die_locks"]] == [0, 1]
    assert [t["problem"] for t in f1["tasks"]] == ["LayerNormV3", "PadV3Grad"]
    out1 = mgr._smooth_lane_handoffs(f1, now_m=t0)
    assert len(out1["parallel_cpu_builds"]) == 1

    # Frame 2 (0.8s later): ConcatD disappears for 1 frame during Phase A -> Ticket handoff
    f2 = enrich_npu_payload({"die_locks": [], "parallel_cpu_builds": [], "tasks": list(f1["tasks"])}, host="npu", mac_waiters=[])
    out2 = mgr._smooth_lane_handoffs(f2, now_m=t0 + 0.8)
    assert any(b["title"] == "ConcatD" for b in out2["parallel_cpu_builds"])

    # Frame 3 (1.2s later): ConcatD lands in WAITING-DIE -> immediately moves to tasks & leaves parallel_cpu_builds
    f3 = enrich_npu_payload(
        {
            "die_locks": [],
            "parallel_cpu_builds": [],
            "tasks": [{"pid": 201, "state": "WAITING", "stage": "WAITING-DIE", "lane": "Q1", "problem": "ConcatD", "elapsed_sec": 1, "detail": "", "subprocesses": 1}],
        },
        host="npu",
        mac_waiters=[],
    )
    out3 = mgr._smooth_lane_handoffs(f3, now_m=t0 + 1.2)
    assert not any(b["title"] == "ConcatD" for b in out3.get("parallel_cpu_builds") or [])
    assert any(t["problem"] == "ConcatD" and t["stage"] == "WAITING-DIE" for t in out3["tasks"])

    # Frame 4 (4.0s later): ConcatD truly finished -> expires cleanly past grace window
    f4 = enrich_npu_payload({"die_locks": [], "parallel_cpu_builds": [], "tasks": []}, host="npu", mac_waiters=[])
    out4 = mgr._smooth_lane_handoffs(f4, now_m=t0 + 4.0)
    assert out4["tasks"] == []


def test_multi_account_team_best_snapshot_preserved_and_drift_refresh(iso_env: Path, tmp_path: Path) -> None:
    ws = _make_ws(tmp_path, iso_env)
    (ws / "results" / "scoring-evidence" / "team-ranking.json").write_text(
        json.dumps({"team_name": "XJU第二少年团", "rank": 3, "total": 794, "score": 466.52, "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}),
        encoding="utf-8",
    )

    # 1. Multi-account submission: teammate-2 (`u2`) submits sid-u2 and scores 74.50 while primary (`u1`) holds 80.00.
    # Outcome score MUST be 74.50 (accurate per-submission), while score-snapshot.json MUST stay 80.00 (team best)!
    fake_judge = tmp_path / "fake_judge"
    fake_judge.mkdir()
    (fake_judge / "ict_cli.py").write_text(
        "class _Resp:\n"
        "    def __init__(self, body):\n"
        "        self.status_code = 200\n"
        "        self._body = body\n"
        "    def json(self):\n"
        "        return self._body\n"
        "class _Sess:\n"
        "    def get(self, url, params=None, timeout=20):\n"
        "        return _Resp({\n"
        "            'pages': 1,\n"
        "            'rows': [\n"
        "                {'user_id': 'u1', 'submission_id': 'sid-u1', 'score': 80.0, 'rank': 5, 'times': [2.0]*5},\n"
        "                {'user_id': 'u2', 'submission_id': 'sid-u2', 'score': 74.5, 'rank': 18, 'times': [2.5]*5},\n"
        "            ],\n"
        "        })\n"
        "class ICTClient:\n"
        "    BASE_URL = 'https://judge.invalid'\n"
        "    def __init__(self, session_file=None):\n"
        "        self.session = _Sess()\n"
        "    def load_session(self):\n"
        "        return True\n"
        "    def get_submission(self, sid):\n"
        "        return {'status': 'Pass', 'result': [{'time': 2.5, 'best_time': 2.0}] * 5}\n",
        encoding="utf-8",
    )
    (ws / "skills" / "problem-submit" / "scripts").mkdir(parents=True, exist_ok=True)
    (ws / "skills" / "problem-submit" / "scripts" / "ict_cli.py").write_text((fake_judge / "ict_cli.py").read_text(encoding="utf-8"), encoding="utf-8")

    with mock.patch("opsb_core.config.SUPERVISOR_ROOT", tmp_path / "no_runtime"):
        st, sc, _, _ = _wait_and_fetch_score(ws, "ConcatD", "sid-u2", ranking_retry_delays=(0.01,))
    assert st == "Pass"
    assert sc == 74.5
    snap_disk = json.loads((ws / "results" / "score-snapshot.json").read_text(encoding="utf-8"))
    assert snap_disk["ConcatD"]["score"] == 80.0
    assert snap_disk["ConcatD"]["submission_id"] == "sid-u1"
    assert snap_disk["ConcatD"]["rank"] == 5

    # 2. Score drift (weighted vs team_rank_score >= 0.5) triggers board refresh even when < 24h old
    one_hr_ago = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)).isoformat()
    (ws / "results" / "scoring-evidence" / "live-board-tbest.json").write_text(
        json.dumps({"fetched_at": one_hr_ago, "problems": {}}),
        encoding="utf-8",
    )
    fresh_no_drift = inspect_board_freshness(ws, score_drift=0.1)
    assert fresh_no_drift["stale"] is False
    fresh_drift = inspect_board_freshness(ws, score_drift=16.52)
    assert fresh_drift["stale"] is True
    assert fresh_drift["should_refresh"] is True
    assert "score_drift=16.52" in fresh_drift["reason"]

    # 3. board_refresh matches remote teammate by `team_name` AND handles ranking rows omitting `status`
    (ws / "results" / "score-snapshot.json").write_text(
        json.dumps({
            "ConcatD": {"score": 0.0, "status": "Wrong Answer", "rank": None},
            "PadV3Grad": {"score": 70.0, "status": "Pass", "rank": 10},
        }),
        encoding="utf-8",
    )

    def _fake_get_json(_base: str, _cookie: str, path: str):
        if "p-concat" in path:
            # Remote teammate `u-remote-teammate` (not logged in locally!), matched via `team_name`, and NO `status` key!
            return {
                "pages": 1,
                "total_testcase_count": 50,
                "testcases": [{"id": f"c{i}", "tbest": 1.0} for i in range(5)],
                "rows": [
                    {"user_id": "u-remote-teammate", "team_name": "XJU第二少年团", "submission_id": "sid-remote", "score": 85.5, "rank": 3, "times": [1.2] * 5},
                ],
            }
        return {
            "pages": 1,
            "total_testcase_count": 50,
            "testcases": [{"id": f"c{i}", "tbest": 1.0} for i in range(5)],
            "rows": [
                {"user_id": "u1", "submission_id": "sid-p1", "score": 70.0, "rank": 10, "times": [3.0] * 5},
            ],
        }

    with (
        mock.patch("opsb_core.services.board_refresh_service._get_json", side_effect=_fake_get_json),
        mock.patch("opsb_core.services.ranking_service.refresh_async", return_value=None),
    ):
        res = refresh_board_and_recalibrate(ws, force=False, recalibrate=False, score_drift=16.52)
    assert res["ok"] is True
    assert res["skipped"] is False
    snap_after = json.loads((ws / "results" / "score-snapshot.json").read_text(encoding="utf-8"))
    assert snap_after["ConcatD"]["status"] == "Pass"
    assert snap_after["ConcatD"]["score"] == 85.5
    assert snap_after["ConcatD"]["submission_id"] == "sid-remote"


def test_cold_etag_stable_across_periodic_timer_ticks(iso_env: Path, tmp_path: Path) -> None:
    ws = _make_ws(tmp_path, iso_env)
    cache = WorkspaceStateCache(ws)
    ent = cache._entry(ws)
    tick = [0]
    real_compute = compute_workspace_snapshot

    def _ticking_compute(workspace: Path, cfg: dict) -> dict:
        snap = real_compute(workspace, cfg)
        t = tick[0]
        tick[0] += 1
        snap["sampled_at"] = f"2026-11-23T10:0{t}:00+00:00"
        snap["problems"][0]["head_age_min"] = 10 + t
        snap["scheduler"]["cooldown_remaining_sec"] = max(0, 120 - t * 30)
        snap["scheduler"]["pipeline"]["cooldown_remaining_sec"] = max(0, 120 - t * 30)
        snap["scheduler"]["pipeline"]["judge_elapsed_sec"] = t * 30
        return snap

    with (
        mock.patch("opsb_core.daemon.state_cache.compute_workspace_snapshot", side_effect=_ticking_compute),
        mock.patch("opsb_core.daemon.state_cache.invoke_bridge", return_value=None),
        mock.patch("opsb_core.daemon.state_cache.probe_backfill_compact", return_value=None),
        mock.patch("opsb_core.daemon.state_cache.refresh_if_stale_async", side_effect=lambda *a, **kw: {"stale": False, "refresh_age_hours": 1.0 + tick[0]}),
        mock.patch("opsb_core.daemon.state_cache.get_auth_status", return_value={"judge": {"logged_in": False, "session_file": "", "base_url": ""}, "judge_accounts": [], "gitcode": {"logged_in": False, "cli_path": ""}}),
        mock.patch("opsb_core.daemon.state_cache.get_skills_status", return_value={"mode": "agents-md", "available_skills": [], "allowed_targets": [], "targets": {}}),
        mock.patch("opsb_core.services.ranking_service.team_ranking", return_value=None),
    ):
        ent.ensure_cold_fresh(synchronous=True)
        etag1 = ent.cold_etag
        # Force age-based periodic recompute without any underlying file change
        with ent._lock:
            ent.cold_computed_at = 0.0
            ent._last_fp_check = 0.0
        ent.ensure_cold_fresh(synchronous=True, max_age_sec=1.0)
        etag2 = ent.cold_etag
        assert etag1 == etag2

