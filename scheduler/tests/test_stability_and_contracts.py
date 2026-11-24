"""Regression tests for scheduler state stability, QUOTA_EXHAUSTED, atomic I/O,
deterministic ordering, and Swift Codable contract alignment.
"""
from __future__ import annotations

import datetime
import json
import math
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from scheduler.backfill import BackfillScheduler  # noqa: E402
from scheduler.calibration import BenchmarkCalibrator  # noqa: E402
from scheduler.cli_bridge import SchedulerEngine  # noqa: E402
from scheduler.estimator import ProblemLedger, ScoreEstimate  # noqa: E402
from scheduler.gate import GateConfig, QuotaStatus, STATUS_TAGS, SubmissionGate  # noqa: E402
from scheduler.npu_scheduler import NpuTaskScheduler  # noqa: E402
from scheduler.parhelion_paths import (  # noqa: E402
    atomic_write_json,
    read_json_safe,
    sanitize_json_value,
)


def _make_multi_problem_ws(
    tmp: pathlib.Path,
    titles: tuple[str, ...] = ("ConcatD", "LayerNormV3"),
) -> tuple[pathlib.Path, dict[str, list[str]]]:
    ws = tmp / "ws"
    (ws / "results" / "scoring-evidence").mkdir(parents=True)
    problems_meta = []
    shas_by_title: dict[str, list[str]] = {}
    for title in titles:
        op_dir = ws / "bench" / title
        (op_dir / "op_host").mkdir(parents=True)
        (op_dir / "op_kernel").mkdir(parents=True)
        (ws / "results" / title).mkdir(parents=True)
        problems_meta.append(
            {"title": title, "slug": title.lower(), "path": f"bench/{title}", "difficulty": 2}
        )
        subprocess.run(["git", "-C", str(op_dir), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(op_dir), "config", "user.email", "t@example.com"], check=True)
        subprocess.run(["git", "-C", str(op_dir), "config", "user.name", "Test"], check=True)
        shas: list[str] = []
        for idx in range(2):
            (op_dir / "op_host" / "h.cpp").write_text(f"// {title} host {idx}\n", encoding="utf-8")
            (op_dir / "op_kernel" / "k.cpp").write_text(f"// {title} kernel {idx}\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(op_dir), "add", "."], check=True)
            subprocess.run(["git", "-C", str(op_dir), "commit", "-q", "-m", f"v{idx}"], check=True)
            sha = subprocess.check_output(["git", "-C", str(op_dir), "rev-parse", "HEAD"], text=True).strip()
            shas.append(sha)
            vdir = ws / "results" / title / sha
            vdir.mkdir(parents=True, exist_ok=True)
            (vdir / "result.json").write_text(
                json.dumps({"exit_code": 0, "sha": sha, "finished_at": f"2026-11-23T0{idx}:00:00Z"}),
                encoding="utf-8",
            )
            (vdir / "validation.log").write_text("EXTENDED_PASSED 40 cases\nEXTENDED_RC=0\n", encoding="utf-8")
        shas_by_title[title] = shas

    (ws / "bench" / "problems.json").write_text(json.dumps(problems_meta), encoding="utf-8")
    (ws / "results" / "submit-queue.jsonl").write_text("", encoding="utf-8")
    (ws / "results" / "submit-queue-outcome.jsonl").write_text("", encoding="utf-8")
    (ws / "results" / "score-snapshot.json").write_text(
        json.dumps({t: {"score": 60.0, "status": "Pass"} for t in titles}),
        encoding="utf-8",
    )
    return ws, shas_by_title


def _stub_estimate(title: str, sha: str, *, current: float = 60.0, est: float = 70.0) -> ScoreEstimate:
    delta = round(est - current, 2)
    return ScoreEstimate(
        title=title,
        sha=sha,
        short_sha=sha[:7],
        difficulty=2,
        board_status="Pass" if current > 0 else "no-submission",
        current_score=current,
        estimated_score=est,
        delta_expected=delta,
        weighted_delta=round(2 * delta, 2),
        confidence_sigma=2.0,
        prob_positive=0.85,
        recoverable_headroom=20.0,
        effective_speedup=1.1,
        estimation_method="pairwise_simplex_v2",
        baseline_sha=None,
        decay_factor_gamma=1.0,
        raw_historical_best_score=current,
        contemporary_best_score=current,
        true_restore_gain=0.0,
        matched_cases=16,
        improved_cases=8,
        regressed_cases=[],
        detail="stub",
        ucb_delta=delta + 2.0,
        calibration_trust=0.7,
        explore_eligible=False,
        explore_reason="",
        explore_evidence={},
    )


def _stub_ledger(title: str, *, current: float = 60.0, best_sha: str | None = None) -> ProblemLedger:
    return ProblemLedger(
        title=title,
        difficulty=2,
        board_status="Pass" if current > 0 else "no-submission",
        current_score=current,
        weighted_current_score=round(2 * current, 2),
        public_score=7.0,
        hidden_score=max(0.0, current - 7.0),
        implied_hidden_r=0.7,
        public_r_list=[0.7] * 5,
        frontier_top1=90.0,
        frontier_top5=80.0,
        target_score=90.0,
        recoverable_headroom=20.0,
        theoretical_headroom=30.0,
        active_board_sha="0" * 40,
        active_judged_score=current,
        active_judged_at="2026-11-23T00:00:00Z",
        decay_factor_gamma=1.0,
        raw_historical_best_score=current,
        contemporary_best_score=current,
        historical_best_score=current,
        historical_best_mean=current,
        historical_best_sha=best_sha,
        true_restore_gain=0.0,
        restore_gap=0.0,
    )


def _quota_stub(
    *,
    daily_limit: int = 50,
    used_today: int = 50,
    remaining_today: int = 0,
    reserved: int = 2,
    discretionary: int = 0,
    rate_wait_sec: int = 0,
) -> QuotaStatus:
    return QuotaStatus(
        daily_limit=daily_limit,
        used_today=used_today,
        remaining_today=remaining_today,
        needed_closing_restores=0,
        reserved_for_closing=reserved,
        discretionary_remaining=discretionary,
        closing_restore_targets=[],
        rate_limit_gap_sec=190,
        seconds_since_last_submit=10 if rate_wait_sec > 0 else 500,
        rate_wait_sec=rate_wait_sec,
        beijing_date="2026-11-23",
        next_reset_utc="2026-11-23T16:00:00+00:00",
    )


class QuotaExhaustedTagTest(unittest.TestCase):
    def test_quota_exhausted_in_status_tags_whitelist(self) -> None:
        self.assertIn("QUOTA_EXHAUSTED", STATUS_TAGS)
        self.assertIn("RATE_WAIT", STATUS_TAGS)

    def test_daily_limit_reached_emits_quota_exhausted_never_rate_wait(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws, shas = _make_multi_problem_ws(pathlib.Path(td), ("ConcatD",))
            cand_sha = shas["ConcatD"][1]
            gate = SubmissionGate(ws, config=GateConfig(noise_floor_pts=2.5, probe_submissions_per_problem=0))

            est = _stub_estimate("ConcatD", cand_sha, current=60.0, est=70.0)
            ledger = _stub_ledger("ConcatD", current=60.0, best_sha=shas["ConcatD"][0])

            # 1. Daily limit reached (even with rate_wait_sec=0) -> QUOTA_EXHAUSTED, approved=False
            with (
                mock.patch.object(gate.estimator, "estimate", return_value=est),
                mock.patch.object(gate.estimator, "get_problem_ledger", return_value=ledger),
                mock.patch.object(gate, "get_quota_status", return_value=_quota_stub(used_today=50, remaining_today=0, rate_wait_sec=0)),
            ):
                v = gate.evaluate("ConcatD", sha=cand_sha)
            self.assertFalse(v.approved)
            self.assertEqual(v.status_tag, "QUOTA_EXHAUSTED")
            s4 = next(s for s in v.stages if s.stage == "QUOTA_RESERVE")
            self.assertFalse(s4.passed)
            self.assertEqual(s4.code, "DAILY_LIMIT_REACHED")

            # 2. Even with force=True when daily limit is exhausted -> still QUOTA_EXHAUSTED
            with (
                mock.patch.object(gate.estimator, "estimate", return_value=est),
                mock.patch.object(gate.estimator, "get_problem_ledger", return_value=ledger),
                mock.patch.object(gate, "get_quota_status", return_value=_quota_stub(used_today=50, remaining_today=0, rate_wait_sec=0)),
            ):
                v_force = gate.evaluate("ConcatD", sha=cand_sha, force=True)
            self.assertFalse(v_force.approved)
            self.assertEqual(v_force.status_tag, "QUOTA_EXHAUSTED")

            # 3. Quota available + cooldown active -> approved=True, status_tag=RATE_WAIT
            with (
                mock.patch.object(gate.estimator, "estimate", return_value=est),
                mock.patch.object(gate.estimator, "get_problem_ledger", return_value=ledger),
                mock.patch.object(
                    gate,
                    "get_quota_status",
                    return_value=_quota_stub(used_today=10, remaining_today=40, discretionary=38, rate_wait_sec=120),
                ),
            ):
                v_rate = gate.evaluate("ConcatD", sha=cand_sha)
            self.assertTrue(v_rate.approved)
            self.assertEqual(v_rate.status_tag, "RATE_WAIT")

            # 4. Remaining slots reserved for closing -> HOLD_LOW_GAIN (not RATE_WAIT / QUOTA_EXHAUSTED)
            with (
                mock.patch.object(gate.estimator, "estimate", return_value=est),
                mock.patch.object(gate.estimator, "get_problem_ledger", return_value=ledger),
                mock.patch.object(
                    gate,
                    "get_quota_status",
                    return_value=_quota_stub(used_today=48, remaining_today=2, reserved=2, discretionary=0, rate_wait_sec=0),
                ),
            ):
                v_res = gate.evaluate("ConcatD", sha=cand_sha)
            self.assertFalse(v_res.approved)
            self.assertEqual(v_res.status_tag, "HOLD_LOW_GAIN")


class AtomicIOAndCorruptGuardTest(unittest.TestCase):
    def test_atomic_write_json_uses_pid_tmp_and_sanitizes_nan(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = pathlib.Path(td) / "state.json"
            seen_tmps: list[str] = []
            orig_replace = os.replace

            def _spy_replace(src: os.PathLike[str] | str, dst: os.PathLike[str] | str) -> None:
                seen_tmps.append(str(src))
                orig_replace(src, dst)

            with mock.patch("os.replace", side_effect=_spy_replace):
                atomic_write_json(target, {"a": 1, "bad_nan": float("nan"), "bad_inf": float("inf")})

            self.assertEqual(len(seen_tmps), 1)
            self.assertIn(f".{os.getpid()}.", pathlib.Path(seen_tmps[0]).name)
            parsed = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(parsed, {"a": 1, "bad_nan": None, "bad_inf": None})
            # No leftover tmp files
            self.assertEqual([p.name for p in pathlib.Path(td).iterdir()], ["state.json"])

    def test_read_json_safe_distinguishes_missing_from_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "x.json"
            self.assertEqual(read_json_safe(p, retries=0), (None, True))

            p.write_text("", encoding="utf-8")
            self.assertEqual(read_json_safe(p, retries=0), (None, False))

            p.write_text("{truncated", encoding="utf-8")
            self.assertEqual(read_json_safe(p, retries=0), (None, False))

            p.write_text('{"ok": true}', encoding="utf-8")
            self.assertEqual(read_json_safe(p, retries=0), ({"ok": True}, True))

    def test_scheduler_engine_refuses_to_overwrite_corrupt_state_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            ws, _ = _make_multi_problem_ws(td_p, ("ConcatD",))
            st_dir = td_p / "state"
            st_dir.mkdir(parents=True)
            corrupt_bytes = "{half-written"
            state_file = st_dir / "scheduler_state.json"
            state_file.write_text(corrupt_bytes, encoding="utf-8")

            with mock.patch.dict("os.environ", {"PARHELION_HOME": str(td_p / "ph")}, clear=False):
                eng = SchedulerEngine(workspace=ws, state_dir=st_dir)
                # Read-only status still succeeds and preserves the file intact
                st = eng.status(offline=True)
                self.assertIn("contest_totals", st)
                self.assertEqual(state_file.read_text(encoding="utf-8"), corrupt_bytes)
                # Mutating save MUST refuse to clobber the corrupt file
                with self.assertRaises(RuntimeError):
                    eng.update_config(noise_floor=3.0)
                self.assertEqual(state_file.read_text(encoding="utf-8"), corrupt_bytes)

    def test_backfill_refuses_to_overwrite_corrupt_timings_or_config(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            ws, shas = _make_multi_problem_ws(td_p, ("ConcatD",))
            st_dir = td_p / "bf_state"
            st_dir.mkdir(parents=True)
            sched = BackfillScheduler(ws, state_dir=st_dir)

            sched.timings_path.write_text("{corrupt", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                sched.store_timings("ConcatD", shas["ConcatD"][0], {"b1": 12.3})
            self.assertEqual(sched.timings_path.read_text(encoding="utf-8"), "{corrupt")

            sched.config_path.write_text("", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                sched.save_config(min_interval_sec=600.0)
            self.assertEqual(sched.config_path.read_text(encoding="utf-8"), "")


class StateMachineAndOrderingTest(unittest.TestCase):
    def test_submit_queue_overlap_dedupes_and_sorts_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            ws, shas = _make_multi_problem_ws(td_p, ("LayerNormV3", "ConcatD"))
            st_dir = td_p / "state"
            st_dir.mkdir(parents=True)

            # Put both items in held_items with identical held_at, and put ConcatD also in submit-queue.jsonl
            # (simulating the brief overlap window during HELD -> PENDING write-before-remove transition)
            c_sha = shas["ConcatD"][1]
            l_sha = shas["LayerNormV3"][1]
            (st_dir / "scheduler_state.json").write_text(
                json.dumps({
                    "held_items": {
                        f"LayerNormV3:{l_sha}": {"title": "LayerNormV3", "sha": l_sha, "held_at": "2026-11-23T01:00:00Z"},
                        f"ConcatD:{c_sha}": {"title": "ConcatD", "sha": c_sha, "held_at": "2026-11-23T01:00:00Z"},
                    }
                }),
                encoding="utf-8",
            )
            # Duplicate pending rows for ConcatD + equal-priority row for LayerNormV3 in submit-queue.jsonl
            (ws / "results" / "submit-queue.jsonl").write_text(
                "\n".join([
                    json.dumps({"title": "ConcatD", "sha": c_sha, "queued_at": "2026-11-23T02:00:00Z"}),
                    json.dumps({"title": "ConcatD", "sha": c_sha, "queued_at": "2026-11-23T02:05:00Z"}),
                ]) + "\n",
                encoding="utf-8",
            )

            with mock.patch.dict("os.environ", {"PARHELION_HOME": str(td_p / "ph")}, clear=False):
                eng = SchedulerEngine(workspace=ws, state_dir=st_dir)
                with (
                    mock.patch.object(
                        eng.estimator,
                        "estimate",
                        side_effect=lambda title, sha, **kw: _stub_estimate(title, sha, current=60.0, est=70.0),
                    ),
                    mock.patch.object(
                        eng.estimator,
                        "get_problem_ledger",
                        side_effect=lambda title, **kw: _stub_ledger(title, current=60.0),
                    ),
                ):
                    q = eng.build_submit_queue_view()

            # ConcatD must appear ONLY ONCE (as PENDING, never duplicated as HELD)
            concat_rows = [x for x in q if x["title"] == "ConcatD" and x["sha"] == c_sha]
            self.assertEqual(len(concat_rows), 1)
            self.assertEqual(concat_rows[0]["queue_state"], "PENDING")
            # LayerNormV3 remains HELD
            ln_rows = [x for x in q if x["title"] == "LayerNormV3" and x["sha"] == l_sha]
            self.assertEqual(len(ln_rows), 1)
            self.assertEqual(ln_rows[0]["queue_state"], "HELD")

    def test_drain_tie_breaker_is_true_fifo_not_wall_clock(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            ws, shas = _make_multi_problem_ws(td_p, ("LayerNormV3", "ConcatD"))
            c_sha = shas["ConcatD"][1]
            l_sha = shas["LayerNormV3"][1]
            # LayerNormV3 was queued EARLIER than ConcatD; both have identical priority_score
            (ws / "results" / "submit-queue.jsonl").write_text(
                "\n".join([
                    json.dumps({"title": "ConcatD", "sha": c_sha, "queued_at": "2026-11-23T02:10:00Z"}),
                    json.dumps({"title": "LayerNormV3", "sha": l_sha, "queued_at": "2026-11-23T02:00:00Z"}),
                ]) + "\n",
                encoding="utf-8",
            )
            with mock.patch.dict("os.environ", {"PARHELION_HOME": str(td_p / "ph")}, clear=False):
                eng = SchedulerEngine(workspace=ws, state_dir=td_p / "st")
                with (
                    mock.patch.object(eng, "refresh_models"),
                    mock.patch.object(
                        eng.estimator,
                        "estimate",
                        side_effect=lambda title, sha, **kw: _stub_estimate(title, sha, current=60.0, est=70.0),
                    ),
                    mock.patch.object(
                        eng.estimator,
                        "get_problem_ledger",
                        side_effect=lambda title, **kw: _stub_ledger(title, current=60.0),
                    ),
                    mock.patch.object(
                        eng.gate,
                        "get_quota_status",
                        return_value=_quota_stub(used_today=0, remaining_today=50, discretionary=48, rate_wait_sec=0),
                    ),
                ):
                    plan = eng.drain(once=True, dry_run=True)

            titles = [row["title"] for row in plan["approved_order"]]
            self.assertEqual(titles, ["LayerNormV3", "ConcatD"])

    def test_backfill_retry_after_failure_is_detected_as_inflight_running(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            ws, shas = _make_multi_problem_ws(td_p, ("ConcatD",))
            sched = BackfillScheduler(ws, state_dir=td_p / "bf")
            sk = "deadbeef12345678"
            # Attempt 1 started and finished with die_busy
            sched.ledger.append({"event": "started", "session_key": sk, "title": "ConcatD", "pid": 999999, "started_epoch": 100.0})
            sched.ledger.append({"event": "finished", "session_key": sk, "title": "ConcatD", "result": "die_busy", "ended_epoch": 105.0})
            self.assertIsNone(sched.inflight_running())

            # Attempt 2 retries the EXACT same session_key with current alive pid
            sched.ledger.append({"event": "started", "session_key": sk, "title": "ConcatD", "pid": os.getpid(), "started_epoch": 200.0})
            running = sched.inflight_running()
            self.assertIsNotNone(running)
            assert running is not None
            self.assertEqual(running["session_key"], sk)
            self.assertEqual(running["pid"], os.getpid())
            self.assertTrue(sched.summary({})["running"])

            # Attempt 2 completes -> no longer running
            sched.ledger.append({"event": "finished", "session_key": sk, "title": "ConcatD", "result": "ok", "ended_epoch": 220.0, "measured_count": 3})
            self.assertIsNone(sched.inflight_running())
            self.assertFalse(sched.summary({})["running"])

    def test_npu_queue_state_tag_invariants_and_deterministic_tie_breaker(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws, shas = _make_multi_problem_ws(pathlib.Path(td), ("LayerNormV3", "ConcatD"))
            npu = NpuTaskScheduler(ws)
            fake_remote = {
                "sampled_at": "2026-11-23T03:00:00+00:00",
                "probe_latency_ms": 12.5,
                "cpu_cores": 32,
                "load_1m": 1.0,
                "load_5m": 1.0,
                "devices": [],
                "die_locks": [],
                "npu_tickets": [],
                "raw_tasks": [
                    # Two tied CPU_BUILD tasks in reverse alphabetical order -> must sort ConcatD before LayerNormV3
                    {"pid": 202, "die": None, "state": "CPU_BUILD", "tag": "PRECISION", "problem": "LayerNormV3", "sha": shas["LayerNormV3"][0], "elapsed_sec": 10, "detail": "build"},
                    {"pid": 201, "die": None, "state": "CPU_BUILD", "tag": "BUILD", "problem": "ConcatD", "sha": shas["ConcatD"][0], "elapsed_sec": 10, "detail": "build"},
                    # Active backfill task -> must keep tag=BACKFILL and mlfq_level=B0
                    {"pid": 101, "die": 0, "state": "ACTIVE", "tag": "BACKFILL", "problem": "ConcatD", "sha": shas["ConcatD"][1], "elapsed_sec": 30, "detail": "bf"},
                    # Active task with bogus WAITING-DIE tag -> normalized to PRECISION
                    {"pid": 102, "die": 1, "state": "ACTIVE", "tag": "WAITING-DIE", "problem": "LayerNormV3", "sha": shas["LayerNormV3"][1], "elapsed_sec": 30, "detail": "run"},
                ],
            }
            fake_proc = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=json.dumps(fake_remote) + "\n", stderr="")
            with (
                mock.patch("subprocess.run", return_value=fake_proc),
                mock.patch.object(npu, "_scan_local_inflight", return_value=([], {})),
            ):
                res = npu.inspect_live_queue(offline=False)

            tasks = res["tasks"]
            active = [t for t in tasks if t["state"] == "ACTIVE"]
            builds = [t for t in tasks if t["state"] == "CPU_BUILD"]
            bf_task = next(t for t in active if t["pid"] == 101)
            self.assertEqual(bf_task["tag"], "BACKFILL")
            self.assertEqual(bf_task["mlfq_level"], "B0")

            norm_active = next(t for t in active if t["pid"] == 102)
            self.assertEqual(norm_active["tag"], "PRECISION")

            self.assertEqual([b["title"] for b in builds], ["ConcatD", "LayerNormV3"])
            self.assertTrue(all(b["tag"] == "BUILD" for b in builds))


class CliBridgeOfflineContractTest(unittest.TestCase):
    def test_status_offline_types_match_swift_codable_contract(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            ws, _ = _make_multi_problem_ws(td_p, ("ConcatD", "LayerNormV3"))
            with mock.patch.dict("os.environ", {"PARHELION_HOME": str(td_p / "ph")}, clear=False):
                eng = SchedulerEngine(workspace=ws, state_dir=td_p / "st")
                # Simulate a cached calibration doc with empty coverage_grid={} and NaN mae_pts
                orig_load = BenchmarkCalibrator.load_or_calibrate

                def _wrapped_load(cal_self: BenchmarkCalibrator, title: str, *args, **kwargs):
                    doc = dict(orig_load(cal_self, title, *args, **kwargs))
                    if title == "ConcatD":
                        doc["coverage_grid"] = {}
                        doc["mae_pts"] = float("nan")
                    return doc

                with mock.patch.object(BenchmarkCalibrator, "load_or_calibrate", autospec=True, side_effect=_wrapped_load):
                    payload = eng.status(offline=True)

            # Must serialize with allow_nan=False (RFC-8259 strict JSON for Swift JSONDecoder)
            encoded = json.dumps(payload, allow_nan=False)
            decoded = json.loads(encoded)

            hw = decoded["npu_hardware"]
            self.assertIsInstance(hw["offline"], bool)
            self.assertTrue(hw["offline"])
            self.assertIsInstance(hw["cpu_cores"], int)
            self.assertIsInstance(hw["probe_latency_ms"], (int, float))
            self.assertIsNone(hw["load_1m"])
            self.assertIsNone(hw["load_5m"])

            # Empty coverage_grid={} must be emitted as null (None), never {} (which crashes Swift BenchCoverageGrid)
            cal_c = decoded["calibrations"]["ConcatD"]
            self.assertIsNone(cal_c["coverage_grid"])
            self.assertIsNone(cal_c["mae_pts"])
            self.assertIsInstance(cal_c["escape_lock"], bool)
            self.assertIsInstance(cal_c["unpaired_bucket_count"], int)

            # Valid coverage_grid keeps rows/cols/cells
            cal_l = decoded["calibrations"]["LayerNormV3"]
            self.assertIsInstance(cal_l["coverage_grid"], dict)
            self.assertIsInstance(cal_l["coverage_grid"]["rows"], list)
            self.assertIsInstance(cal_l["coverage_grid"]["cols"], list)
            self.assertIsInstance(cal_l["coverage_grid"]["cells"], list)

            # BackfillPassthrough contract
            bf = decoded["backfill"]
            self.assertEqual(bf["lane"], "B0")
            self.assertIsInstance(bf["tier"], int)
            self.assertIsInstance(bf["auto_enabled"], bool)
            self.assertIsInstance(bf["unpaired_total"], int)
            self.assertIsInstance(bf["unpaired_by_operator"], dict)
            self.assertIsInstance(bf["running"], bool)
            self.assertIsInstance(bf["est_session_sec"], (int, float))

    def test_override_approve_and_skip_write_jsonl_before_mutating_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            ws, shas = _make_multi_problem_ws(td_p, ("ConcatD",))
            sha_approve = shas["ConcatD"][0]
            sha_skip = shas["ConcatD"][1]
            st_dir = td_p / "st"
            st_dir.mkdir(parents=True)
            (st_dir / "scheduler_state.json").write_text(
                json.dumps({
                    "held_items": {
                        f"ConcatD:{sha_approve}": {"title": "ConcatD", "sha": sha_approve, "held_at": "2026-11-23T01:00:00Z"},
                        f"ConcatD:{sha_skip}": {"title": "ConcatD", "sha": sha_skip, "held_at": "2026-11-23T01:01:00Z"},
                    }
                }),
                encoding="utf-8",
            )
            with mock.patch.dict("os.environ", {"PARHELION_HOME": str(td_p / "ph")}, clear=False):
                eng = SchedulerEngine(workspace=ws, state_dir=st_dir)
                events: list[str] = []
                orig_save = eng._save_state
                from scheduler import cli_bridge as cb_mod
                orig_append = cb_mod._append_jsonl

                def _spy_append(p: pathlib.Path, rec: dict) -> None:
                    events.append(f"append:{p.name}")
                    orig_append(p, rec)

                def _spy_save() -> None:
                    events.append("save_state")
                    orig_save()

                with (
                    mock.patch.object(cb_mod, "_append_jsonl", side_effect=_spy_append),
                    mock.patch.object(eng, "_save_state", side_effect=_spy_save),
                ):
                    eng.override("ConcatD", action="approve", sha=sha_approve, reason="go")
                    eng.override("ConcatD", action="skip", sha=sha_skip, reason="no")

            self.assertEqual(
                events,
                [
                    "append:submit-queue.jsonl",
                    "save_state",
                    "append:submit-queue-outcome.jsonl",
                    "save_state",
                ],
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
