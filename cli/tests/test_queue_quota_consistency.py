"""Unit tests for Wave B (V3-002, V3-010, V3-012): quota unification, multi-account cooldown & inflight visibility."""
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

from opsb_core.commands.breakthrough_cmd import run_queue_list  # noqa: E402
from opsb_core.commands.queue_cmd import run_queue  # noqa: E402
from opsb_core.config import ConfigRepository  # noqa: E402
from opsb_core.ipc_bridge import merge_bridge_enrichment  # noqa: E402
from opsb_core.mcp.handlers_bench import tool_parhelion_status  # noqa: E402
from opsb_core.services.judge_sync import compute_synchronized_pipeline  # noqa: E402
from opsb_core.services.queue_service import compute_workspace_snapshot  # noqa: E402
from scheduler.cli_bridge import SchedulerEngine  # noqa: E402
from scheduler.gate import GateConfig, SubmissionGate  # noqa: E402


def _make_multi_account_ws(tmp_path: Path, parhelion_home: Path) -> tuple[Path, str]:
    ws = tmp_path / "ws"
    op_dir = ws / "bench" / "ConcatD"
    (op_dir / "op_host").mkdir(parents=True)
    (op_dir / "op_kernel").mkdir(parents=True)
    (op_dir / "op_kernel" / "k.cpp").write_text("// ok\n", encoding="utf-8")
    (ws / "results" / "scoring-evidence").mkdir(parents=True)
    (ws / "bench" / "problems.json").write_text(
        json.dumps([
            {"title": "ConcatD", "slug": "concatd", "problem_id": "p1", "difficulty": 3, "path": "bench/ConcatD"},
        ]),
        encoding="utf-8",
    )
    (ws / "results" / "score-snapshot.json").write_text(
        json.dumps({"ConcatD": {"score": 77.96, "status": "Pass", "rank": 8, "public_times_us": [4.0] * 5}}),
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

    val_dir = ws / "results" / "ConcatD" / sha
    val_dir.mkdir(parents=True)
    (val_dir / "result.json").write_text(json.dumps({"exit_code": 0, "sha": sha}), encoding="utf-8")
    (val_dir / "validation.log").write_text("EXTENDED_PASSED 40 cases\nEXTENDED_RC=0\n", encoding="utf-8")

    # Configure 2 logged-in accounts + unified scheduler config
    parhelion_home.mkdir(parents=True, exist_ok=True)
    sess_dir = parhelion_home / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()
    (sess_dir / "primary.json").write_text(
        json.dumps({"version": 1, "base_url": "https://judge.invalid", "user_id": "uid-prim", "logged_in_at": now_ts, "cookies": [{"name": "c", "value": "1"}]}),
        encoding="utf-8",
    )
    (sess_dir / "teammate-2.json").write_text(
        json.dumps({"version": 1, "base_url": "https://judge.invalid", "user_id": "uid-tm2", "logged_in_at": now_ts, "cookies": [{"name": "c", "value": "2"}]}),
        encoding="utf-8",
    )
    (parhelion_home / "cj_accounts.json").write_text(
        json.dumps({"accounts": [
            {"id": "primary", "label": "主账号", "enabled": True},
            {"id": "teammate-2", "label": "队友2", "enabled": True, "session_file": str(sess_dir / "teammate-2.json")},
        ]}),
        encoding="utf-8",
    )
    (parhelion_home / "config.json").write_text(
        json.dumps({
            "active_workspace": str(ws),
            "workspaces": [str(ws)],
            "scheduler": {
                "auto_consumer_enabled": False,
                "daily_quota_limit": 50,
                "closing_reserve": 10,
                "noise_floor_delta": 1.9,
                "breakthrough_threshold": 5.0,
                "rate_limit_gap_sec": 190,
                "circuit_breaker_limit": 6,
            },
        }),
        encoding="utf-8",
    )
    return ws, sha


@pytest.fixture()
def iso_ph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    p_home = tmp_path / "parhelion_home"
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("PARHELION_HOME", str(p_home))
    monkeypatch.setenv("OPSB_DISABLE_SYNC_RECONCILE", "1")
    return p_home


def test_multi_account_quota_reserve_noise_floor_consistency(iso_ph: Path, tmp_path: Path) -> None:
    ws, sha = _make_multi_account_ws(tmp_path, iso_ph)
    # Record 1 recent submit 30s ago on teammate-2 ONLY; primary has NEVER submitted -> cooldown 0s!
    recent_iso = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=30)).isoformat()
    (ws / "results" / "ConcatD" / sha / "submission-receipt.json").write_text(
        json.dumps({"submission_id": "sid-tm2-1", "validated_commit": sha, "submitted_at": recent_iso, "account_id": "teammate-2", "user_id": "uid-tm2"}),
        encoding="utf-8",
    )

    # 1. gate.py must be multi-account aware: daily_limit=100, rate_wait_sec=0 (primary is ready!), reserved=10
    gate = SubmissionGate(ws, config=GateConfig(noise_floor_pts=1.9, min_closing_reserve=10))
    quota = gate.get_quota_status()
    assert quota.daily_limit == 100
    assert quota.used_today == 1
    assert quota.remaining_today == 99
    assert quota.reserved_for_closing == 10
    assert quota.discretionary_remaining == 89
    assert quota.rate_wait_sec == 0

    # 2. SchedulerEngine picks up config.json noise_floor=1.9 and closing_reserve=10
    engine = SchedulerEngine(workspace=ws)
    assert engine.config.noise_floor_pts == 1.9
    assert engine.config.min_closing_reserve == 10
    bridge = engine.status(offline=True)
    assert bridge["quota"]["daily_limit"] == 100
    assert bridge["quota"]["reserved_for_closing"] == 10
    assert bridge["quota"]["discretionary_remaining"] == 89
    assert bridge["quota"]["rate_wait_sec"] == 0
    assert bridge["config"]["noise_floor_pts"] == 1.9

    # 3. compute_workspace_snapshot + merge_bridge_enrichment matches parhelion_status & opsb queue config
    cfg = ConfigRepository.load()
    snap = compute_workspace_snapshot(ws, cfg)
    merge_bridge_enrichment(snap, bridge)
    assert snap["scheduler"]["daily_quota_limit"] == 100
    assert snap["scheduler"]["closing_reserve"] == 10
    # 4.2 P18: the shown free quota is the accounts' remaining quota; the reserve is not deducted.
    assert snap["scheduler"]["available_normal_shots"] == 99 == quota.remaining_today
    assert snap["scheduler"]["noise_floor_delta"] == 1.9
    assert snap["scheduler"]["cooldown_remaining_sec"] == 0

    with mock.patch("opsb_core.mcp.handlers_bench.invoke_bridge", return_value=bridge):
        mcp_st = tool_parhelion_status({"include_npu": False}, ws)
    assert mcp_st["scheduler"]["daily_quota_limit"] == 100
    assert mcp_st["scheduler"]["closing_reserve"] == 10
    assert mcp_st["scheduler"]["available_normal_shots"] == 99
    assert mcp_st["scheduler"]["noise_floor_delta"] == 1.9

    # The daemon hot frame follows the same display rule as the cold snapshot.
    from opsb_core.daemon.state_cache import WorkspaceStateCache

    ent = WorkspaceStateCache(ws)._entry(ws)
    ent.ensure_cold_fresh(synchronous=True)
    _, hot_sched = ent.build_hot_pipeline_and_scheduler()
    assert hot_sched["closing_reserve"] == 10
    assert hot_sched["available_normal_shots"] == 99

    buf = io.StringIO()
    args_cfg = argparse.Namespace(
        q_cmd="config",
        workspace=str(ws),
        auto_consumer=None,
        closing_reserve=None,
        noise_floor=None,
        breakthrough_threshold=None,
        lang=None,
    )
    with mock.patch("opsb_core.commands.queue_cmd.invoke_bridge", return_value=bridge), redirect_stdout(buf):
        rc = run_queue(args_cfg)
    assert rc == 0
    q_cfg_out = json.loads(buf.getvalue())["scheduler"]
    assert q_cfg_out["daily_quota_limit"] == 100
    assert q_cfg_out["closing_reserve"] == 10
    assert q_cfg_out["noise_floor_delta"] == 1.9


def test_inflight_transitional_sync_and_queue_list_visibility(iso_ph: Path, tmp_path: Path) -> None:
    ws, sha = _make_multi_account_ws(tmp_path, iso_ph)
    # Receipt just created 20s ago, remote poll returns None (network blip / immediate post-upload window)
    recent_iso = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=20)).isoformat()
    (ws / "results" / "ConcatD" / sha / "submission-receipt.json").write_text(
        json.dumps({"submission_id": "sid-trans-1", "validated_commit": sha, "submitted_at": recent_iso, "account_id": "primary", "user_id": "uid-prim"}),
        encoding="utf-8",
    )

    with mock.patch("opsb_core.services.judge_sync._query_remote_submission_cached", return_value=None):
        pipe = compute_synchronized_pipeline(ws, outcome_rows=[], queue_lock_held=False)
        assert pipe["is_busy"] is True
        assert pipe["phase"] == "remote_queued"
        assert pipe["remote_status"] == "Uploading/Syncing"
        assert len(pipe["active_submissions"]) == 1
        assert pipe["active_submissions"][0]["submission_id"] if "submission_id" in pipe["active_submissions"][0] else pipe["active_submissions"][0]["remote_submission_id"] == "sid-trans-1"

        # opsb queue list includes inflight item in both JSON and text!
        buf = io.StringIO()
        ls_args = argparse.Namespace(workspace=str(ws), breakthrough=False, min_delta=None, status="all", problem=None, json=True)
        with mock.patch("opsb_core.commands.breakthrough_cmd.invoke_bridge", return_value=None), redirect_stdout(buf):
            rc = run_queue_list(ls_args)
        assert rc == 0
        out = json.loads(buf.getvalue())
        assert len(out["active_submissions"]) == 1
        assert any(it["kind"] == "inflight" and it["title"] == "ConcatD" and it["short_sha"] == sha[:7] for it in out["items"])

        buf_txt = io.StringIO()
        ls_args_txt = argparse.Namespace(workspace=str(ws), breakthrough=False, min_delta=None, status="all", problem=None, json=False)
        with mock.patch("opsb_core.commands.breakthrough_cmd.invoke_bridge", return_value=None), redirect_stdout(buf_txt):
            rc2 = run_queue_list(ls_args_txt)
        assert rc2 == 0
        assert "INFLIGHT(" in buf_txt.getvalue()
        assert "ConcatD" in buf_txt.getvalue()


def test_forced_pending_queue_item_all_pass_respects_safety_and_quota(iso_ph: Path, tmp_path: Path) -> None:
    ws, sha = _make_multi_account_ws(tmp_path, iso_ph)
    # Pending item with force=true and gain below noise floor (0.54 < 1.9)
    (ws / "results" / "submit-queue.jsonl").write_text(
        json.dumps({
            "title": "ConcatD",
            "sha": sha,
            "note": "forced test below noise floor",
            "queued_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "status": "queued",
            "estimated_score": 78.50,
            "force": True,
        }) + "\n",
        encoding="utf-8",
    )

    cfg = ConfigRepository.load()
    snap = compute_workspace_snapshot(ws, cfg)
    pending = [it for it in snap["submission_queue"] if it["title"] == "ConcatD" and it["kind"] == "pending"]
    assert len(pending) == 1
    # 4.1 #9: the queue no longer judges a row itself; the gate verdict decides.
    assert pending[0]["badge"] == "NOT EVALUATED" and pending[0]["gates"]["evaluated"] is False

    def stages(safety=True, noise=True, circuit=True, dispatch=True, code="PASS"):
        return [{"stage": "SAFETY", "passed": safety, "code": code, "reason": "s1",
                 "metrics": {"next_step": "awaiting reproduction"} if not safety else {}},
                {"stage": "NOISE_FLOOR", "passed": noise, "reason": "s2"},
                {"stage": "CIRCUIT_BREAKER", "passed": circuit, "reason": "s3"},
                {"stage": "QUOTA_RESERVE", "passed": dispatch, "reason": "slots"}]

    unforced = {"title": "ConcatD", "sha": sha, "approved": False, "status_tag": "HOLD_LOW_GAIN",
                "priority_score": 1.62, "gate_reason": "below noise floor", "stages": stages(noise=False),
                "estimate": {"estimated_score": 78.50, "current_score": 77.96, "delta_expected": 0.54,
                             "ucb_delta": 4.2, "explore_eligible": False, "calibration_trust": 0.6}}
    forced = {"queue_state": "PENDING", "title": "ConcatD", "sha": sha, "status_tag": "READY", "priority_score": 1.62,
              "stages": stages(), "delta_expected": 0.54, "ucb_delta": 4.2, "calibration_trust": 0.6}
    bridge = {"quota": {"daily_limit": 100, "used_today": 1, "reserved_for_closing": 10, "discretionary_remaining": 89},
              "config": {"noise_floor_pts": 1.9, "daily_limit": 50}, "unsubmitted_candidates": [unforced]}
    merge_bridge_enrichment(snap, bridge)
    pending_after = [it for it in snap["submission_queue"] if it["title"] == "ConcatD" and it["kind"] == "pending"]
    # An unforced candidate verdict never applies to a forced queue row.
    assert pending_after[0]["gates"]["evaluated"] is False

    snap = compute_workspace_snapshot(ws, cfg)
    merge_bridge_enrichment(snap, {**bridge, "submit_queue": [forced]})
    pending_after = [it for it in snap["submission_queue"] if it["title"] == "ConcatD" and it["kind"] == "pending"]
    assert pending_after[0]["gates"]["all_pass"] is True and pending_after[0]["badge"] == "READY"
    assert pending_after[0]["gates"]["ucb_delta"] == 4.2

    # ESCAPE_LOCK is never bypassed even when force=True: the gate's S1 says so.
    snap = compute_workspace_snapshot(ws, cfg)
    merge_bridge_enrichment(snap, {**bridge, "submit_queue": [
        {**forced, "status_tag": "REJECTED_GATE", "stages": stages(safety=False, code="ESCAPE_LOCK_ACTIVE")}]})
    pending_esc = [it for it in snap["submission_queue"] if it["title"] == "ConcatD" and it["kind"] == "pending"]
    assert pending_esc[0]["badge"] == "ESCAPE LOCK" and pending_esc[0]["next_step"] == "awaiting reproduction"
    assert pending_esc[0]["gates"]["safety_ok"] is False and pending_esc[0]["gates"]["all_pass"] is False

    # No free submission slot: S1-S3 still pass, the card waits, and no quota wording appears.
    snap = compute_workspace_snapshot(ws, cfg)
    merge_bridge_enrichment(snap, {**bridge, "submit_queue": [
        {**forced, "status_tag": "QUOTA_EXHAUSTED", "stages": stages(dispatch=False)}]})
    pending_q = [it for it in snap["submission_queue"] if it["title"] == "ConcatD" and it["kind"] == "pending"]
    assert pending_q[0]["badge"] == "WAITING FOR SLOT" and pending_q[0]["gates"]["dispatch_ok"] is False
    assert pending_q[0]["gates"]["all_pass"] is True and "quota_ok" not in pending_q[0]["gates"]


def test_shown_free_shots_keep_the_closing_reserve_separate() -> None:
    """4.2 P18: `available_normal_shots` is free quota; the reserve is its own display field."""
    slots = [
        {"enabled": True, "logged_in": True, "daily_limit": 50, "used_today": 10},
        {"enabled": True, "logged_in": True, "daily_limit": 50, "used_today": 20},
    ]
    bridge = {
        "quota": {"daily_limit": 100, "used_today": 30, "remaining_today": 70,
                  "reserved_for_closing": 10, "discretionary_remaining": 60},
        "config": {"daily_limit": 50, "noise_floor_pts": 1.9},
    }
    payload = {"scheduler": {"pipeline": {"account_slots": slots}}, "problems": [], "contest": {},
               "submission_queue": []}
    merge_bridge_enrichment(payload, bridge)
    assert payload["scheduler"]["available_normal_shots"] == 70
    assert payload["scheduler"]["closing_reserve"] == 10

    # No live account slots: the bridge's own remaining quota wins over its reserve-adjusted number.
    payload = {"scheduler": {}, "problems": [], "contest": {}, "submission_queue": []}
    merge_bridge_enrichment(payload, bridge)
    assert payload["scheduler"]["available_normal_shots"] == 70
    assert payload["scheduler"]["closing_reserve"] == 10

    # A bridge that only reports the reserve-adjusted number still renders the full free quota.
    payload = {"scheduler": {}, "problems": [], "contest": {}, "submission_queue": []}
    merge_bridge_enrichment(payload, {"quota": {"discretionary_remaining": 60, "reserved_for_closing": 10},
                                      "config": {}})
    assert payload["scheduler"]["available_normal_shots"] == 70
    assert payload["scheduler"]["closing_reserve"] == 10
