"""Unit tests for v3.0.3 production guardrails (V3-024):
1. `[RESTORE]` gate tightening in `queue_service.eval_gates()` & `queue_cmd.run_submit()`
2. Per-operator inflight mutual exclusion in `queue_cmd.run_submit()`, `submit_runner.spawn_jump_submit()`, and `SchedulerEngine.drain()`
3. Permanent suppression for `opsb queue skip` (7-char & 40-char SHA, `queue_cmd`, `queue_service`, `ipc_bridge`, `cancel_waiting_cooldown_for_commit`)
"""
from __future__ import annotations

import argparse
import datetime
import io
import json
import os
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

from opsb_core.commands.queue_cmd import run_queue, run_submit  # noqa: E402
from opsb_core.config import ConfigRepository  # noqa: E402
from opsb_core.ipc_bridge import merge_bridge_enrichment  # noqa: E402
from opsb_core.services.queue_service import compute_workspace_snapshot  # noqa: E402
from opsb_core.services.submit_runner import (  # noqa: E402
    cancel_waiting_cooldown_for_commit,
    inflight_file_for,
    spawn_jump_submit,
)
from scheduler.cli_bridge import SchedulerEngine  # noqa: E402


@pytest.fixture()
def iso_ph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    p_home = tmp_path / "parhelion_home"
    p_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("PARHELION_HOME", str(p_home))
    monkeypatch.setenv("OPSB_DISABLE_SYNC_RECONCILE", "1")
    return p_home


def _setup_ws(tmp_path: Path, p_home: Path, *, auto_consumer: bool = False) -> tuple[Path, dict[str, list[str]]]:
    ws = tmp_path / "ws"
    (ws / "results" / "scoring-evidence").mkdir(parents=True)
    probs = [
        {"title": "StridedSliceGrad", "slug": "stridedslicegrad", "problem_id": "p1", "difficulty": 3, "path": "bench/StridedSliceGrad"},
        {"title": "PadV3Grad", "slug": "padv3grad", "problem_id": "p2", "difficulty": 3, "path": "bench/PadV3Grad"},
    ]
    (ws / "bench").mkdir(parents=True)
    (ws / "bench" / "problems.json").write_text(json.dumps(probs), encoding="utf-8")
    (ws / "results" / "submit-queue.jsonl").write_text("", encoding="utf-8")
    (ws / "results" / "submit-queue-outcome.jsonl").write_text("", encoding="utf-8")

    shas_by_op: dict[str, list[str]] = {}
    for p in probs:
        t = p["title"]
        op_dir = ws / p["path"]
        (op_dir / "op_host").mkdir(parents=True)
        (op_dir / "op_kernel").mkdir(parents=True)
        subprocess.run(["git", "-C", str(op_dir), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(op_dir), "config", "user.email", "t@example.com"], check=True)
        subprocess.run(["git", "-C", str(op_dir), "config", "user.name", "Test"], check=True)
        shas: list[str] = []
        for idx in range(4):
            (op_dir / "op_host" / "h.cpp").write_text(f"// {t} host v{idx}\n", encoding="utf-8")
            (op_dir / "op_kernel" / "k.cpp").write_text(f"// {t} v{idx}\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(op_dir), "add", "."], check=True)
            subprocess.run(["git", "-C", str(op_dir), "commit", "-q", "-m", f"v{idx}"], check=True)
            sha = subprocess.check_output(["git", "-C", str(op_dir), "rev-parse", "HEAD"], text=True).strip()
            shas.append(sha)
            vdir = ws / "results" / t / sha
            vdir.mkdir(parents=True, exist_ok=True)
            (vdir / "result.json").write_text(
                json.dumps({"exit_code": 0, "sha": sha, "finished_at": f"2026-11-23T0{idx}:00:00Z"}),
                encoding="utf-8",
            )
            (vdir / "validation.log").write_text("EXTENDED_PASSED 40 cases\nEXTENDED_RC=0\n", encoding="utf-8")
        shas_by_op[t] = shas

    (ws / "results" / "score-snapshot.json").write_text(
        json.dumps({
            "StridedSliceGrad": {"score": 66.83, "status": "Pass", "rank": 8, "public_times_us": [5.0] * 5},
            "PadV3Grad": {"score": 70.00, "status": "Pass", "rank": 6, "public_times_us": [4.0] * 5},
        }),
        encoding="utf-8",
    )

    sess_dir = p_home / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()
    (sess_dir / "primary.json").write_text(
        json.dumps({"version": 1, "base_url": "https://judge.invalid", "user_id": "u1", "logged_in_at": now_ts, "cookies": [{"name": "c", "value": "1"}]}),
        encoding="utf-8",
    )
    (p_home / "cj_accounts.json").write_text(
        json.dumps({"accounts": [{"id": "primary", "label": "主账号", "enabled": True}]}),
        encoding="utf-8",
    )
    (p_home / "config.json").write_text(
        json.dumps({
            "active_workspace": str(ws),
            "workspaces": [str(ws)],
            "scheduler": {
                "auto_consumer_enabled": auto_consumer,
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
    return ws, shas_by_op


def test_restore_regime_eval_gates_and_run_submit_block_inferior_candidate(iso_ph: Path, tmp_path: Path) -> None:
    ws, shas = _setup_ws(tmp_path, iso_ph)
    best_sha, cand_low_sha, cand_high_sha, cur_sha = shas["StridedSliceGrad"]

    # Historical score-table shows best_sha=72.13, later outcome replaced by cur_sha=66.83 -> [RESTORE +5.3]
    (ws / "results" / "scoring-evidence" / "my-score-table.json").write_text(
        json.dumps([
            {"title": "StridedSliceGrad", "sha": best_sha, "status": "Pass", "score": 72.13, "at": "2026-11-23T08:00:00+00:00"},
        ]),
        encoding="utf-8",
    )
    (ws / "results" / "submit-queue-outcome.jsonl").write_text(
        json.dumps({
            "title": "StridedSliceGrad", "sha": cur_sha, "status": "Pass", "score": 66.83,
            "submission_id": "sid-cur", "submitted_at": "2026-11-23T10:00:00+00:00",
            "finished_at": "2026-11-23T10:02:00+00:00",
        }) + "\n",
        encoding="utf-8",
    )

    # Queue: (1) non-best candidate est=70.40 (< best=72.13), (2) non-best est=74.50 (>= best+1.9), (3) restoring best_sha est=72.13
    (ws / "results" / "submit-queue.jsonl").write_text(
        "\n".join([
            json.dumps({
                "title": "StridedSliceGrad", "sha": cand_low_sha, "note": "mean ~ 70.40",
                "queued_at": "2026-11-23T10:57:00+00:00", "status": "queued", "estimated_score": 70.40,
            }),
            json.dumps({
                "title": "StridedSliceGrad", "sha": cand_high_sha, "note": "mean ~ 74.50",
                "queued_at": "2026-11-23T10:58:00+00:00", "status": "queued", "estimated_score": 74.50,
            }),
            json.dumps({
                "title": "StridedSliceGrad", "sha": best_sha, "note": "restore best 72.13",
                "queued_at": "2026-11-23T10:59:00+00:00", "status": "queued", "estimated_score": 72.13,
            }),
        ]) + "\n",
        encoding="utf-8",
    )

    cfg = ConfigRepository.load()
    snap = compute_workspace_snapshot(ws, cfg)
    pending = [it for it in snap["submission_queue"] if it["kind"] == "pending"]
    by_q = {it["queued_at"]: it for it in pending}
    assert by_q["2026-11-23T10:57:00+00:00"]["gates"]["noise_ok"] is False
    assert by_q["2026-11-23T10:57:00+00:00"]["badge"] == "HOLD: NOISE"
    assert by_q["2026-11-23T10:58:00+00:00"]["gates"]["noise_ok"] is True
    assert by_q["2026-11-23T10:58:00+00:00"]["badge"] == "READY"
    assert by_q["2026-11-23T10:59:00+00:00"]["gates"]["noise_ok"] is True
    assert by_q["2026-11-23T10:59:00+00:00"]["badge"] == "READY"

    # Non-forced `opsb submit` with est=70.40 must ALSO be held (rc=3) in [RESTORE] regime
    ns = argparse.Namespace(
        problem="StridedSliceGrad", sha=cand_low_sha, note="mean ~ 70.40", force=False, workspace=str(ws), json=True, now=False
    )
    with mock.patch("opsb_core.commands.queue_cmd.invoke_bridge", return_value=None), redirect_stdout(io.StringIO()):
        rc = run_submit(ns)
    assert rc == 3


def test_per_operator_inflight_mutex_python_auto_dispatch_and_drain(iso_ph: Path, tmp_path: Path) -> None:
    ws, shas = _setup_ws(tmp_path, iso_ph, auto_consumer=True)
    s_sha1, s_sha2, _, _ = shas["StridedSliceGrad"]
    p_sha1, _, _, _ = shas["PadV3Grad"]

    # Create an active remote receipt for StridedSliceGrad s_sha1 (not yet in outcomes)
    recent_iso = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=20)).isoformat()
    (ws / "results" / "StridedSliceGrad" / s_sha1 / "submission-receipt.json").write_text(
        json.dumps({"submission_id": "sid-inflight-1", "validated_commit": s_sha1, "submitted_at": recent_iso, "account_id": "primary", "user_id": "u1"}),
        encoding="utf-8",
    )

    # 1. `opsb submit StridedSliceGrad <s_sha2>` with auto_consumer=True & now=False MUST keep queued and NOT auto-dispatch!
    ns_auto = argparse.Namespace(
        problem="StridedSliceGrad", sha=s_sha2, note="mean ~ 78.00", force=False, workspace=str(ws), json=True, now=False
    )
    buf = io.StringIO()
    with (
        mock.patch("opsb_core.commands.queue_cmd.invoke_bridge", return_value=None),
        mock.patch("opsb_core.services.judge_sync._query_remote_submission_cached", return_value=None),
        mock.patch("opsb_core.commands.queue_cmd.spawn_jump_submit") as m_spawn,
        redirect_stdout(buf),
    ):
        rc = run_submit(ns_auto)
    assert rc == 0
    m_spawn.assert_not_called()
    out = json.loads(buf.getvalue())
    assert out["enqueued"] is True
    assert out["dispatched"]["deferred_operator_inflight"] is True

    # 2. `spawn_jump_submit(..., enforce_operator_mutex=True)` also enforces mutex directly for different SHA
    with mock.patch("opsb_core.services.submit_runner.compute_account_slots", return_value=[{"id": "primary", "label": "主账号", "enabled": True, "logged_in": True, "phase": "idle", "cooldown_remaining_sec": 0, "used_today": 1, "daily_limit": 50, "remaining_today": 49, "quota_exhausted": False}]):
        blocked = spawn_jump_submit(CLI_DIR / "opsb", ws, "StridedSliceGrad", s_sha2, "auto", enforce_operator_mutex=True)
        assert blocked["ok"] is False
        assert blocked["operator_inflight_mutex"] is True

    # 3. Explicit `--now` manual action REMAINS allowed!
    ns_now = argparse.Namespace(
        problem="StridedSliceGrad", sha=s_sha2, note="mean ~ 78.00", force=True, workspace=str(ws), json=True, now=True
    )
    with (
        mock.patch("opsb_core.commands.queue_cmd.invoke_bridge", return_value=None),
        mock.patch("opsb_core.services.judge_sync._query_remote_submission_cached", return_value=None),
        mock.patch("opsb_core.commands.queue_cmd.spawn_jump_submit", return_value={"ok": True, "started": True}) as m_now,
        redirect_stdout(io.StringIO()),
    ):
        rc_now = run_submit(ns_now)
    assert rc_now == 0
    m_now.assert_called_once()

    # 4. Different operator (`PadV3Grad`) CAN still auto-dispatch while `StridedSliceGrad` is inflight!
    ns_other = argparse.Namespace(
        problem="PadV3Grad", sha=p_sha1, note="1.60x faster mean ~ 78.00", force=False, workspace=str(ws), json=True, now=False
    )
    with (
        mock.patch("opsb_core.commands.queue_cmd.invoke_bridge", return_value=None),
        mock.patch("opsb_core.services.judge_sync._query_remote_submission_cached", return_value=None),
        mock.patch("opsb_core.commands.queue_cmd.spawn_jump_submit", return_value={"ok": True, "started": True}) as m_other,
        redirect_stdout(io.StringIO()),
    ):
        rc_other = run_submit(ns_other)
    assert rc_other == 0
    m_other.assert_called_once()

    # 5. `SchedulerEngine.drain(dry_run=True)` also holds `StridedSliceGrad` under `INFLIGHT_MUTEX` while letting `PadV3Grad` proceed
    eng = SchedulerEngine(workspace=ws, state_dir=tmp_path / "sched_st")
    dry = eng.drain(dry_run=True)
    assert [x["title"] for x in dry["approved_order"]] == ["PadV3Grad"]
    assert any(h["title"] == "StridedSliceGrad" and h["status_tag"] == "INFLIGHT_MUTEX" for h in dry["held_or_rejected"])


def test_queue_skip_resolves_sha_dedups_outcomes_and_permanently_suppresses(iso_ph: Path, tmp_path: Path) -> None:
    ws, shas = _setup_ws(tmp_path, iso_ph)
    cand_sha = shas["StridedSliceGrad"][1]

    # Queue forced item first (`dad8589`-style auto-consumer enqueue), then skip using 7-char short SHA
    (ws / "results" / "submit-queue.jsonl").write_text(
        json.dumps({
            "title": "StridedSliceGrad", "sha": cand_sha, "note": "queued",
            "queued_at": "2026-11-23T11:03:22+00:00", "status": "queued", "estimated_score": 75.0, "force": True,
        }) + "\n",
        encoding="utf-8",
    )

    eng = SchedulerEngine(workspace=ws, state_dir=tmp_path / "sched_st")
    buf = io.StringIO()
    skip_args = argparse.Namespace(q_cmd="skip", problem="stridedslicegrad", sha=cand_sha[:7], reason="bad worktree", workspace=str(ws))
    with (
        mock.patch("opsb_core.commands.queue_cmd.invoke_bridge", side_effect=lambda _ws, _sub, extra: eng.override(title=extra[1], sha=extra[3], action="skip", reason=extra[7])),
        redirect_stdout(buf),
    ):
        rc = run_queue(skip_args)
    assert rc == 0
    out_lines = [json.loads(l) for l in (ws / "results" / "submit-queue-outcome.jsonl").read_text().splitlines() if l.strip()]
    # Single canonical 40-char skipped record (no duplicate lines!)
    assert len(out_lines) == 1
    assert out_lines[0]["title"] == "StridedSliceGrad"
    assert out_lines[0]["sha"] == cand_sha
    assert out_lines[0]["status"] == "skipped"

    # Now verify 7-char skipped outcome ALSO suppresses a 40-char queued item & prevents ipc_bridge re-injection!
    (ws / "results" / "submit-queue-outcome.jsonl").write_text(
        json.dumps({
            "title": "StridedSliceGrad", "sha": cand_sha[:7], "status": "skipped",
            "queued_at": "2026-11-23T11:03:25+00:00", "finished_at": "2026-11-23T11:03:25+00:00",
        }) + "\n",
        encoding="utf-8",
    )
    snap = compute_workspace_snapshot(ws, ConfigRepository.load())
    assert not any(it["kind"] == "pending" and it["title"] == "StridedSliceGrad" for it in snap["submission_queue"])

    fake_bridge = {
        "quota": {"daily_limit": 50, "used_today": 0, "reserved_for_closing": 10, "discretionary_remaining": 40},
        "config": {"noise_floor_pts": 1.9, "daily_limit": 50},
        "unsubmitted_candidates": [
            {
                "title": "StridedSliceGrad", "sha": cand_sha, "approved": True, "status_tag": "READY",
                "priority_score": 15.0, "gate_reason": "ready", "stages": [], "estimate": {"estimated_score": 75.0, "current_score": 66.83},
            },
            {
                "title": "PadV3Grad", "sha": shas["PadV3Grad"][0], "approved": False, "status_tag": "SKIPPED",
                "priority_score": 0.0, "gate_reason": "skipped", "stages": [], "estimate": {"estimated_score": 72.0, "current_score": 70.0},
            },
        ],
    }
    merge_bridge_enrichment(snap, fake_bridge)
    assert not any(it["kind"] == "candidate" and it["title"] == "StridedSliceGrad" and it["short_sha"] == cand_sha[:7] for it in snap["submission_queue"])
    assert not any(it["kind"] == "candidate" and it["title"] == "PadV3Grad" for it in snap["submission_queue"])


def test_cancel_waiting_cooldown_for_skipped_commit(iso_ph: Path) -> None:
    inf = inflight_file_for("teammate-2")
    inf.write_text(
        json.dumps({"pid": 999999, "title": "StridedSliceGrad", "sha": "dad8589abcdef", "phase": "waiting_cooldown"}),
        encoding="utf-8",
    )
    with (
        mock.patch("opsb_core.services.submit_runner._pid_alive", side_effect=[True, False]),
        mock.patch("opsb_core.services.submit_runner.os.kill") as m_kill,
    ):
        killed = cancel_waiting_cooldown_for_commit("StridedSliceGrad", "dad8589")
    assert killed == 1
    m_kill.assert_called_once()
    assert not inf.exists()
