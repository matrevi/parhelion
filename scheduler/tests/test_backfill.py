"""B0 backfill lane tests (stdlib `unittest`; no NPU, no SSH, no die lock).

    python3 -m unittest discover -s scheduler/tests -v
    python3 -m pytest scheduler/tests -q     # also works when pytest is installed

Everything here runs offline: the live telemetry probe is replaced by injected dicts,
the remote session runner by a stub that returns `MS_PROF` log lines, and the unpaired
census by a fake scanner.  No test touches `~/.opsb`, `results/`, or `ssh npu`.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from scheduler.backfill import (  # noqa: E402
    LANE_BACKFILL,
    TICKET_TIER,
    build_measurement_plan,
    harness_names_case,
    is_spec_placeholder,
    spec_placeholder_reason,
    BackfillConfig,
    BackfillLedger,
    BackfillScheduler,
    bucket_key,
    build_perf_plan,
    coalesce_sessions,
    offline_npu_sentinel,
    operator_tickets_waiting,
    session_key_for,
    ticket_rank,
)

from scheduler.parhelion_paths import workspace_bench_specs_dir

SUP_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _find_workspace(start: pathlib.Path) -> pathlib.Path:
    """Resolve real workspace if configured in ~/.parhelion/config.json or ancestors."""
    for cand in (start, *start.parents):
        if (cand / "bench" / "problems.json").is_file():
            return cand
    cfg_path = pathlib.Path.home() / ".parhelion" / "config.json"
    if cfg_path.is_file():
        try:
            ws = json.loads(cfg_path.read_text()).get("active_workspace")
            if ws and pathlib.Path(ws).is_dir():
                return pathlib.Path(ws).resolve()
        except Exception:
            pass
    return start.parent


REAL_WORKSPACE = _find_workspace(pathlib.Path(__file__).resolve())
BENCH_SPECS = workspace_bench_specs_dir(REAL_WORKSPACE)
SHA = "a" * 40


def _row(title: str, bucket: str, headroom: float, sha: str = SHA) -> dict:
    return {
        "title": title,
        "bucket": bucket,
        "stratum": "M_MID_UB",
        "dtype": "float32",
        "baseline_sha": sha,
        "baseline_short": sha[:7],
        "modality": "profiler",
        "runtime_soc": "soc-a",
        "description": "",
        "headroom_pts": headroom,
        "key": bucket_key(title, bucket, sha),
    }


class _FakeScheduler(BackfillScheduler):
    """BackfillScheduler with the raw census injected (no repo/log scanning)."""

    def __init__(self, state_dir: pathlib.Path, rows: list[dict], **kw) -> None:
        super().__init__(state_dir, state_dir=state_dir, **kw)
        self.rows = list(rows)
        self.executed: list[dict] = []

    def _census_rows(self) -> list[dict]:
        out = []
        for row in self.rows:
            out.append({**row, "placeholder": is_spec_placeholder(row.get("bucket"))})
        return out

    def _recording_runner(self, lines: list[str]):
        def run(cfg: dict) -> dict:
            self.executed.append(cfg)
            return {"ok": bool(lines), "timing_lines": lines, "log": "/tmp/fake.log", "die": 0, "lock_wait_sec": 0.0}

        return run


def _free_npu() -> dict:
    return {
        "offline": False,
        "ssh_error": None,
        "die_locks": [{"die": 0, "state": "FREE", "holder_pid": None}],
        "tasks": [],
        "npu_tickets": [],
    }


class TicketPriorityProofTest(unittest.TestCase):
    """Invariant 1: a B0 ticket can never outrank an operator ticket."""

    def test_lane_tiers_are_strictly_ordered(self) -> None:
        self.assertEqual(TICKET_TIER["B0"], 0)
        self.assertEqual(TICKET_TIER["Q1"], 1)
        self.assertEqual(TICKET_TIER["Q0"], 2)

    def test_operator_ticket_always_wins_even_with_zero_prio(self) -> None:
        b0 = {"tier": 0, "prio": 10_000.0, "created": 0.0}      # maximally aged backfill
        q1 = {"tier": 1, "prio": 0.0001, "created": 9e9}         # freshest, lowest operator
        self.assertGreater(ticket_rank(q1), ticket_rank(b0))
        self.assertEqual(len(operator_tickets_waiting([b0, q1])), 1)

    def test_backfill_tickets_do_not_count_as_operator_waiters(self) -> None:
        tickets = [{"tier": 0, "prio": 5.0, "created": 1.0}, {"tier": 0, "prio": 9.0, "created": 2.0}]
        self.assertEqual(operator_tickets_waiting(tickets), [])
        self.assertEqual(operator_tickets_waiting(None), [])

    def test_malformed_ticket_is_ignored_not_crashing(self) -> None:
        # unparsable `tier` is ignored; missing `tier` defaults to 1 (fail-safe: blocks B0)
        self.assertEqual(operator_tickets_waiting([{"tier": "x"}, {}]), [{}])


class PlanPureFunctionTest(unittest.TestCase):
    def test_coalesces_multiple_buckets_into_one_session(self) -> None:
        rows = [_row("SyncBatchNormGatherStats", f"b{i}", 1.0) for i in range(5)]
        sessions = coalesce_sessions(rows)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["bucket_count"], 5)
        self.assertEqual(len(sessions[0]["bucket_keys"]), 5)

    def test_one_session_per_operator_sorted_by_headroom(self) -> None:
        rows = [_row("A", "a1", 1.0), _row("A", "a2", 2.0), _row("B", "b1", 10.0)]
        sessions = coalesce_sessions(rows)
        self.assertEqual([s["title"] for s in sessions], ["B", "A"])
        self.assertEqual(sessions[1]["headroom_pts"], 3.0)
        self.assertEqual(sessions[1]["buckets"], ["a2", "a1"])  # headroom desc, deterministic

    def test_skip_keys_and_bucket_cap_are_honoured(self) -> None:
        rows = [_row("A", f"a{i}", float(i)) for i in range(6)]
        skip = {rows[-1]["key"]}
        sessions = coalesce_sessions(rows, max_buckets=3, skip_bucket_keys=skip)
        self.assertEqual(sessions[0]["bucket_count"], 3)
        self.assertEqual(sessions[0]["deferred_buckets"], ["a1", "a0"])
        self.assertNotIn(rows[-1]["key"], sessions[0]["bucket_keys"])

    def test_session_key_is_content_addressed_and_order_insensitive(self) -> None:
        self.assertEqual(
            session_key_for("A", SHA, ["b", "a"]),
            session_key_for("A", SHA, ["a", "b"]),
        )
        self.assertNotEqual(session_key_for("A", SHA, ["a"]), session_key_for("A", "b" * 40, ["a"]))

    def test_bucket_key_changes_with_baseline(self) -> None:
        self.assertNotEqual(bucket_key("A", "b", "1" * 40), bucket_key("A", "b", "2" * 40))

    def test_perf_plan_variants(self) -> None:
        batch = build_perf_plan("ap.add_argument('--cases', default='')", ["x", "y"])
        self.assertEqual(batch["mode"], "cases_batch")
        self.assertEqual(batch["runs"], [["--cases", "x,y"]])

        full = build_perf_plan("ap.add_argument('--filter', default='')", ["x", "y"])
        self.assertEqual(full["mode"], "full_spectrum")
        self.assertEqual(full["runs"], [[]])

        per = build_perf_plan("ap.add_argument('--case', default='')", [f"c{i}" for i in range(9)], max_runs=3)
        self.assertEqual(per["mode"], "per_bucket")
        self.assertEqual(len(per["runs"]), 3)

        self.assertEqual(build_perf_plan("no flags", [])["runs"], [])


class MeasurementPlanTest(unittest.TestCase):
    """kperf (hardware Task Duration) must outrank perf.py; `--root` retargeting is recorded."""

    def _repo(self, tmp: str, files: dict[str, str]) -> pathlib.Path:
        repo = pathlib.Path(tmp) / "op"
        (repo / "validation").mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (repo / "validation" / name).write_text(text)
        return repo

    def test_kperf_wins_over_perf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp, {
                "kperf.py": "ap.add_argument('--cases', default='')\nap.add_argument('--device', default=0)\n"
                            "ap.add_argument('--root', default='')\n",
                "perf.py": "ap.add_argument('--cases', default='')\n",
            })
            plan = build_measurement_plan(repo, ["b1", "b2"])
            self.assertEqual(plan["tool"], "kperf")
            self.assertEqual(plan["mode"], "kperf_batch")
            self.assertEqual(plan["runs"], [["--cases", "b1,b2"]])
            self.assertTrue(plan["uses_root"])
            self.assertTrue(plan["supports_device"])

    def test_perf_fallback_without_kperf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp, {"perf.py": "ap.add_argument('--filter', default='')\n"})
            plan = build_measurement_plan(repo, ["b1"])
            self.assertEqual(plan["tool"], "perf")
            self.assertEqual(plan["mode"], "full_spectrum")
            self.assertFalse(plan["uses_root"])

    def test_empty_bucket_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(build_measurement_plan(pathlib.Path(tmp), [])["runs"], [])


class DeterministicFailureTest(unittest.TestCase):
    """A bucket id absent from the harness case table must not burn retries or builds."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.sched = _FakeScheduler(pathlib.Path(self.tmp.name), [_row("A", "ghost_proxy", 1.0)])
        self.sched.save_config(min_interval_sec=0.0)
        self.sched.ledger.append({
            "event": "finished", "session_key": "s0", "result": "case_not_in_harness",
            "bucket_keys": [bucket_key("A", "ghost_proxy", SHA)], "buckets": ["ghost_proxy"],
            "title": "A", "baseline_sha": SHA, "ended_epoch": self.sched.clock(),
        })

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_never_requeued_even_when_forced(self) -> None:
        self.assertEqual(self.sched.plan(), [])
        self.assertEqual(self.sched.plan(force=True), [])
        self.assertEqual(self.sched.dispatch(force=True, dry_run=False, npu=_free_npu())["reason"], "queue_empty")

    def test_reported_as_unmeasurable_not_queued(self) -> None:
        payload = self.sched.status(npu=_free_npu())
        self.assertEqual(payload["queued_count"], 0)
        self.assertEqual(payload["unmeasurable"][0]["title"], "A")
        self.assertEqual(payload["unmeasurable"][0]["buckets"], ["ghost_proxy"])


class UnmeasuredBucketTest(unittest.TestCase):
    """Harness ran fine but produced no row for an id -> deterministic, never retried."""

    def test_partial_success_parks_the_missing_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sched = _FakeScheduler(pathlib.Path(tmp), [_row("A", "real", 1.0), _row("A", "ghost", 0.5)])
            sched.save_config(min_interval_sec=0.0)
            sched.runner = sched._recording_runner(["MS_PROF case=real median=9.0 min=8.0"])
            out = sched.dispatch(force=True, dry_run=False, npu=_free_npu(), title="A")
            self.assertEqual(out["result"], "ok")
            self.assertEqual(list(out["measured"]), ["real"])
            rec = [r for r in sched.ledger.records() if r.get("event") == "finished"][-1]
            self.assertEqual(rec["unmeasured_buckets"], ["ghost"])
            # ghost is deterministic-unmeasurable: gone from the queue, reported instead
            self.assertEqual(sched.plan(), [])
            self.assertEqual(sched.plan(force=True), [])
            payload = sched.status(npu=_free_npu())
            self.assertEqual(payload["unmeasurable"][0]["buckets"], ["ghost"])
            self.assertEqual(payload["unmeasurable"][0]["reason"], "case_not_in_harness")


class BudgetTest(unittest.TestCase):
    def test_cold_cache_pays_the_build_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sched = _FakeScheduler(pathlib.Path(tmp), [_row("A", "a1", 1.0), _row("B", "b1", 1.0)])
            for job in sched.plan():
                self.assertEqual(job["est_build_sec"], sched.config().est_build_sec)
                self.assertEqual(job["budget_sec"], round(sched.est_session_sec() + sched.config().est_build_sec, 1))
            payload = sched.status(npu=_free_npu())
            self.assertAlmostEqual(payload["eta_sec"], payload["eta_lock_sec"] + payload["eta_build_sec"], places=1)

    def test_warm_cache_drops_the_build_cost(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sched = _FakeScheduler(pathlib.Path(tmp), [_row("A", "a1", 1.0)])
            # a *successful* session keeps its worktree -> warm cache for 7 days
            sched.ledger.append({
                "event": "finished", "session_key": "prev", "result": "ok", "measured_count": 1,
                "build_ok": True, "title": "A", "baseline_sha": SHA, "ended_epoch": sched.clock(),
            })
            job = sched.plan(force=True)[0]
            self.assertEqual(job["est_build_sec"], 0.0)

    def test_built_but_unmeasured_still_warms_the_cache(self) -> None:
        """A 136s build is not thrown away because the case table turned out to lack the id."""
        with tempfile.TemporaryDirectory() as tmp:
            sched = _FakeScheduler(pathlib.Path(tmp), [_row("A", "a1", 1.0)])
            sched.ledger.append({
                "event": "finished", "session_key": "prev", "result": "case_not_in_harness",
                "build_ok": True, "built": True, "title": "A", "baseline_sha": SHA,
                "ended_epoch": sched.clock(),
            })
            self.assertEqual(sched.plan(force=True)[0]["est_build_sec"], 0.0)

    def test_failed_session_does_not_warm_the_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sched = _FakeScheduler(pathlib.Path(tmp), [_row("A", "a1", 1.0)])
            sched.ledger.append({
                "event": "finished", "session_key": "prev", "result": "build_failed",
                "build_ok": False, "title": "A", "baseline_sha": SHA, "ended_epoch": sched.clock(),
            })
            self.assertEqual(sched.plan(force=True)[0]["est_build_sec"], sched.config().est_build_sec)


class ForceBypassTest(unittest.TestCase):
    def test_force_bypasses_auto_backoff_but_not_the_attempt_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sched = _FakeScheduler(pathlib.Path(tmp), [_row("A", "a1", 1.0)])
            key = bucket_key("A", "a1", SHA)
            sched.ledger.append({"event": "finished", "session_key": "s0", "result": "no_timing_evidence",
                                 "bucket_keys": [key], "ended_epoch": sched.clock()})
            self.assertEqual(sched.plan(), [])                     # auto: backoff
            self.assertEqual(len(sched.plan(force=True)), 1)       # manual: immediate retry
            for i in range(sched.config().max_attempts_per_key):
                sched.ledger.append({"event": "finished", "session_key": f"s{i}", "result": "no_timing_evidence",
                                     "bucket_keys": [key], "ended_epoch": sched.clock()})
            self.assertEqual(sched.plan(force=True), [])           # cap still applies


class LedgerTest(unittest.TestCase):
    def _ledger(self, tmp: str) -> BackfillLedger:
        return BackfillLedger(pathlib.Path(tmp) / "ledger.jsonl")

    def test_append_read_and_sha(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            led = self._ledger(tmp)
            self.assertEqual(led.records(), [])
            self.assertEqual(led.sha256(), "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
            led.append({"event": "started", "session_key": "s1", "pid": os.getpid(), "started_epoch": 1.0})
            first = led.sha256()
            led.append({"event": "finished", "session_key": "s1", "result": "ok", "bucket_keys": ["k1"], "ended_epoch": 2.0})
            self.assertEqual(len(led.records()), 2)
            self.assertNotEqual(first, led.sha256())

    def test_torn_tail_line_is_tolerated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            led = self._ledger(tmp)
            led.append({"event": "started", "session_key": "s1"})
            with led.path.open("a") as fh:
                fh.write('{"event": "finis')  # crash mid-write
            self.assertEqual(len(led.records()), 1)

    def test_success_and_attempt_joins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            led = self._ledger(tmp)
            led.append({"event": "started", "session_key": "s1"})
            led.append({"event": "finished", "session_key": "s1", "result": "ok",
                        "bucket_keys": ["k1", "k2"], "ended_epoch": 10.0})
            led.append({"event": "finished", "session_key": "s2", "result": "no_evidence",
                        "bucket_keys": ["k3"], "ended_epoch": 20.0})
            self.assertEqual(led.successful_bucket_keys(), {"k1", "k2"})
            att = led.attempts_by_bucket_key()
            self.assertEqual(att["k3"], [20.0])
            self.assertEqual(att["k1"], [10.0])


class AdmissionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = pathlib.Path(self.tmp.name)
        self.sched = _FakeScheduler(self.state, [_row("A", "a1", 1.0)])

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_offline_telemetry_fails_closed_even_when_forced(self) -> None:
        adm = self.sched.admission(force=True, npu=offline_npu_sentinel())
        self.assertFalse(adm["allowed"])
        self.assertIn("npu_telemetry_offline", adm["reasons"])

    def test_held_die_blocks(self) -> None:
        npu = _free_npu()
        npu["die_locks"] = [{"die": 0, "state": "HELD", "holder_pid": 42}]
        adm = self.sched.admission(force=True, npu=npu)
        self.assertIn("die_lock_held", adm["reasons"])

    def test_waiting_operator_ticket_blocks_even_forced(self) -> None:
        """The core fairness invariant: the UI button sdkot jump an operator queue."""
        npu = _free_npu()
        npu["npu_tickets"] = [{"tier": 1, "title": "ConcatD", "prio": 0.2, "created": 1.0}]
        adm = self.sched.admission(force=True, npu=npu)
        self.assertFalse(adm["allowed"])
        self.assertIn("operator_ticket_waiting", adm["reasons"])
        self.assertEqual(adm["details"]["operator_ticket_titles"], ["ConcatD"])

    def test_active_operator_session_blocks(self) -> None:
        npu = _free_npu()
        npu["tasks"] = [{"tag": "PROFILER", "title": "ConcatD"}]
        self.assertIn("operator_session_active", self.sched.admission(force=True, npu=npu)["reasons"])

    def test_idle_die_allows(self) -> None:
        adm = self.sched.admission(force=True, npu=_free_npu())
        self.assertTrue(adm["allowed"], adm["reasons"])
        self.assertEqual((adm["lane"], adm["tier"]), (LANE_BACKFILL, 0))

    def test_auto_throttle_blocks_but_force_bypasses(self) -> None:
        self.sched.ledger.append({"event": "started", "session_key": "x", "started_epoch": self.sched.clock()})
        auto = self.sched.admission(force=False, npu=_free_npu())
        self.assertIn("throttle_min_interval", auto["reasons"])
        forced = self.sched.admission(force=True, npu=_free_npu())
        self.assertTrue(forced["allowed"], forced["reasons"])

    def test_lane_disabled_blocks_auto_only(self) -> None:
        self.sched.save_config(enabled=False)
        self.assertIn("lane_disabled", self.sched.admission(force=False, npu=_free_npu())["reasons"])
        self.assertTrue(self.sched.admission(force=True, npu=_free_npu())["allowed"])

    def test_hourly_cap(self) -> None:
        sched = _FakeScheduler(self.state, [_row("A", "a1", 1.0)])
        sched.save_config(max_sessions_per_hour=2, min_interval_sec=0.0)
        now = sched.clock()
        for i in range(2):
            sched.ledger.append({"event": "started", "session_key": f"s{i}", "started_epoch": now - 10 - i})
        self.assertIn("throttle_hourly_cap", sched.admission(force=False, npu=_free_npu())["reasons"])


class DispatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = pathlib.Path(self.tmp.name)
        self.rows = [_row("A", "a1", 5.0), _row("A", "a2", 1.0), _row("B", "b1", 2.0)]
        self.sched = _FakeScheduler(self.state, self.rows)
        self.sched.save_config(min_interval_sec=0.0)
        self.lines = [
            "RUNTIME_SOC=soc-a",
            "MS_PROF case=a1 median=12.50 min=11.00",
            "MS_PROF case=a2 median=30.00 min=29.00",
        ]
        self.sched.runner = self.sched._recording_runner(self.lines)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_dry_run_never_executes(self) -> None:
        out = self.sched.dispatch(force=True, dry_run=True, npu=_free_npu())
        self.assertFalse(out["dispatched"])
        self.assertEqual(out["reason"], "dry_run")
        self.assertTrue(out["admission"]["allowed"])
        self.assertEqual(self.sched.executed, [])
        self.assertEqual(self.sched.ledger.records(), [])

    def test_blocked_dispatch_never_executes(self) -> None:
        npu = _free_npu()
        npu["npu_tickets"] = [{"tier": 2, "title": "ConcatD", "prio": 9.0, "created": 1.0}]
        out = self.sched.dispatch(force=True, dry_run=False, npu=npu)
        self.assertFalse(out["dispatched"])
        self.assertEqual(out["reason"], "admission_blocked")
        self.assertEqual(self.sched.executed, [])

    def test_one_session_coalesces_all_buckets_of_an_operator(self) -> None:
        out = self.sched.dispatch(force=True, dry_run=False, npu=_free_npu(), title="A")
        self.assertTrue(out["dispatched"])
        self.assertEqual(len(self.sched.executed), 1)
        self.assertEqual(sorted(self.sched.executed[0]["buckets"]), ["a1", "a2"])
        self.assertEqual(out["result"], "ok")
        self.assertEqual(out["measured"]["a1"], 12.5)
        # timings landed in the store, dedup keys are covered by a success record
        store = self.sched.timings_for("A", SHA)
        self.assertEqual(store["cases"]["a2"], 30.0)
        self.assertEqual(store["runtime_soc"], "soc-a")
        self.assertEqual(
            self.sched.ledger.successful_bucket_keys(),
            {bucket_key("A", "a1", SHA), bucket_key("A", "a2", SHA)},
        )

    def test_success_is_never_rerun(self) -> None:
        self.sched.dispatch(force=True, dry_run=False, npu=_free_npu(), title="A")
        # Even if detection still reports the bucket (stale spec), the ledger stops it.
        self.assertEqual([j["title"] for j in self.sched.plan()], ["B"])
        again = self.sched.dispatch(force=True, dry_run=False, npu=_free_npu(), title="A")
        self.assertEqual(again["reason"], "queue_empty")
        self.assertEqual(len(self.sched.executed), 1)

    def test_empty_queue_is_a_no_op(self) -> None:
        sched = _FakeScheduler(self.state, [])
        out = sched.dispatch(force=True, dry_run=False, npu=_free_npu())
        self.assertEqual(out["reason"], "queue_empty")
        self.assertFalse(out["dispatched"])

    def test_no_evidence_arms_backoff_and_bounded_retries(self) -> None:
        self.sched.runner = self.sched._recording_runner([])
        out = self.sched.dispatch(force=True, dry_run=False, npu=_free_npu(), title="A")
        self.assertEqual(out["result"], "no_timing_evidence")
        self.assertFalse(self.sched.timings_for("A", SHA))
        # backoff: not retried immediately
        self.assertNotIn("A", [j["title"] for j in self.sched.plan()])
        # after the backoff window a retry is allowed again
        later = self.sched.clock() + self.sched.config().backoff_sec + 1
        self.assertIn("A", [j["title"] for j in self.sched.plan(now=later)])
        # ... but only up to `max_attempts_per_key`
        for _ in range(self.sched.config().max_attempts_per_key):
            self.sched.ledger.append(
                {
                    "event": "finished",
                    "session_key": "old",
                    "result": "no_evidence",
                    "bucket_keys": [bucket_key("A", "a1", SHA), bucket_key("A", "a2", SHA)],
                    "ended_epoch": self.sched.clock(),
                }
            )
        self.assertNotIn("A", [j["title"] for j in self.sched.plan(now=self.sched.clock() + 1e6)])

    def test_exec_not_floor_is_normalized_for_exec_modality(self) -> None:
        self.sched.runner = self.sched._recording_runner(
            ["RUNTIME_SOC=soc-a", "a1 ws=1 call= 1.0/ 1.0/ 1.0 exec=  41.20"]
        )
        out = self.sched.dispatch(force=True, dry_run=False, npu=_free_npu(), title="A")
        self.assertEqual(out["result"], "ok")
        self.assertAlmostEqual(out["measured"]["a1"], 34.0, places=3)  # 41.2 - 7.2 host floor


class ResumeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.sched = _FakeScheduler(pathlib.Path(self.tmp.name), [_row("A", "a1", 1.0)])
        self.sched.save_config(min_interval_sec=0.0)
        self.sched.runner = self.sched._recording_runner(["MS_PROF case=a1 median=1.0 min=1.0"])

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_dead_pid_started_record_is_stale_and_retryable(self) -> None:
        self.sched.ledger.append(
            {"event": "started", "session_key": "dead", "pid": 999_999_999, "started_epoch": self.sched.clock()}
        )
        self.assertIsNone(self.sched.inflight_running())
        self.assertEqual(len(self.sched.stale_inflight()), 1)
        out = self.sched.dispatch(force=True, dry_run=False, npu=_free_npu())
        self.assertTrue(out["dispatched"])  # resumed, no manual cleanup needed

    def test_live_pid_blocks_a_second_acquisition(self) -> None:
        self.sched.ledger.append(
            {"event": "started", "session_key": "live", "pid": os.getpid(), "started_epoch": self.sched.clock()}
        )
        self.assertIsNotNone(self.sched.inflight_running())
        adm = self.sched.admission(force=True, npu=_free_npu())
        self.assertIn("backfill_inflight", adm["reasons"])
        self.assertFalse(self.sched.dispatch(force=True, dry_run=False, npu=_free_npu())["dispatched"])


class StatusContractTest(unittest.TestCase):
    REQUIRED = ("unpaired", "queued", "running", "last_run", "eta_sec", "ledger_sha")

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.sched = _FakeScheduler(
            pathlib.Path(self.tmp.name),
            [_row("A", "a1", 5.0), _row("A", "a2", 1.0), _row("B", "b1", 2.0)],
        )
        self.sched.save_config(min_interval_sec=0.0)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_required_keys_present_and_consistent(self) -> None:
        payload = self.sched.status(npu=_free_npu())
        for key in self.REQUIRED:
            self.assertIn(key, payload)
        self.assertEqual(payload["lane"], "B0")
        self.assertEqual(payload["tier"], 0)
        self.assertEqual(payload["unpaired_count"], 3)
        self.assertEqual(payload["queued_count"], 2)  # coalesced per operator
        # eta = perf budget + build budget for cold caches; eta_lock = die time only
        self.assertEqual(payload["eta_sec"], round(sum(j["budget_sec"] for j in payload["queued"]), 1))
        self.assertEqual(payload["eta_lock_sec"], round(sum(j["est_sec"] for j in payload["queued"]), 1))
        self.assertGreater(payload["eta_build_sec"], 0.0)
        self.assertEqual(len(payload["ledger_sha"]), 64)
        self.assertIsNone(payload["running"])
        self.assertIsNone(payload["last_run"])

    def test_eta_shrinks_after_a_session(self) -> None:
        before = self.sched.status(npu=_free_npu())["eta_sec"]
        self.sched.runner = self.sched._recording_runner(["MS_PROF case=a1 median=5.0 min=4.0",
                                                          "MS_PROF case=a2 median=6.0 min=5.0"])
        self.sched.dispatch(force=True, dry_run=False, npu=_free_npu(), title="A")
        payload = self.sched.status(npu=_free_npu())
        self.assertLess(payload["eta_sec"], before)
        self.assertEqual(payload["last_run"]["title"], "A")
        self.assertEqual(payload["last_run"]["measured_count"], 2)

    def test_summary_matches_calibration_docs(self) -> None:
        docs = {"A": {"unpaired_bucket_count": 3}, "B": {"unpaired_bucket_count": 0}}
        summary = self.sched.summary(docs)
        self.assertEqual(summary["unpaired_total"], 3)
        self.assertEqual(summary["unpaired_by_operator"], {"A": 3})
        self.assertTrue(summary["auto_enabled"])

    def test_config_roundtrip_and_unknown_keys_ignored(self) -> None:
        self.sched.save_config(min_interval_sec=42.0, bogus_key=1)
        self.assertEqual(self.sched.config().min_interval_sec, 42.0)
        self.assertFalse(hasattr(self.sched.config(), "bogus_key"))
        self.assertEqual(BackfillConfig.from_dict(None), BackfillConfig())


class CalibrationMergeTest(unittest.TestCase):
    """Backfilled pairings must fill holes in `_extract_bucket_timings`, never override."""

    TITLE = "SyncBatchNormGatherStats"

    def _ws(self, tmp: str, log_lines: list[str] | None) -> pathlib.Path:
        ws = pathlib.Path(tmp) / "ws"
        if log_lines is not None:
            d = ws / "results" / self.TITLE / SHA
            d.mkdir(parents=True, exist_ok=True)
            (d / "validation.log").write_text("\n".join(log_lines) + "\n")
        (ws / "results").mkdir(parents=True, exist_ok=True)
        return ws

    def _cal(self, ws: pathlib.Path, store_cases: dict | None, soc: str | None = "soc-a"):
        from scheduler.calibration import BenchmarkCalibrator
        from scheduler.estimator import ScoreEstimator

        cal = BenchmarkCalibrator(ws, estimator=ScoreEstimator(ws), specs_root=BENCH_SPECS)
        if store_cases is not None:

            class _Store:
                def timings_for(self, title, sha):
                    return {"cases": store_cases, "runtime_soc": soc, "modality": "profiler"}

            cal.backfill = _Store()
        else:
            cal.backfill = False
        return cal

    def _buckets(self) -> list[dict]:
        spec_path = BENCH_SPECS / self.TITLE / "cases_perf.json"
        if not spec_path.is_file():
            # The active workspace is whatever ~/.parhelion/config.json points at; contests
            # other than the ops-bench fixtures simply have no spec for this operator.
            self.skipTest(f"no bench spec for {self.TITLE} in the active workspace ({spec_path})")
        spec = json.loads(spec_path.read_text())
        return spec["hidden_buckets"]

    def test_backfill_fills_holes_without_overriding_the_log(self) -> None:
        buckets = self._buckets()
        b0, b1 = buckets[0]["id"], buckets[1]["id"]
        ws = self._ws(tempfile.mkdtemp(), ["RUNTIME_SOC=soc-a", f"MS_PROF case={b0} median=11.00 min=10.0"])
        cal = self._cal(ws, {b0: 99.0, b1: 21.5})
        timings, _t, soc, modality = cal._extract_bucket_timings(self.TITLE, SHA, buckets)
        self.assertEqual(timings[b0], 11.0)   # primary log wins
        self.assertEqual(timings[b1], 21.5)   # hole filled from B0
        self.assertEqual(soc, "soc-a")
        self.assertEqual(modality, "profiler")

    def test_cross_soc_backfill_is_rejected(self) -> None:
        """`exec`/`dev_us`-derived backfill values carry the host floor: same epoch only."""
        from scheduler.calibration import BenchmarkCalibrator
        from scheduler.estimator import ScoreEstimator

        buckets = self._buckets()
        b0, b1 = buckets[0]["id"], buckets[1]["id"]
        ws = self._ws(tempfile.mkdtemp(), ["RUNTIME_SOC=soc-a", f"MS_PROF case={b0} median=11.00 min=10.0"])
        cal = BenchmarkCalibrator(ws, estimator=ScoreEstimator(ws), specs_root=BENCH_SPECS)
        cal.backfill = type(
            "S", (), {"timings_for": lambda self, t, s: {"cases": {b1: 21.5}, "runtime_soc": "soc-b",
                                                          "modality": "exec_normalized"}}
        )()
        timings, _t, _soc, _m = cal._extract_bucket_timings(self.TITLE, SHA, buckets)
        self.assertNotIn(b1, timings)

    def test_cross_soc_profiler_backfill_is_accepted(self) -> None:
        """Hardware profiler Task Duration is device time, hence epoch-robust."""
        from scheduler.calibration import BenchmarkCalibrator
        from scheduler.estimator import ScoreEstimator

        buckets = self._buckets()
        b0, b1 = buckets[0]["id"], buckets[1]["id"]
        ws = self._ws(tempfile.mkdtemp(), ["RUNTIME_SOC=soc-b", f"MS_PROF case={b0} median=11.00 min=10.0"])
        cal = BenchmarkCalibrator(ws, estimator=ScoreEstimator(ws), specs_root=BENCH_SPECS)
        cal.backfill = type(
            "S", (), {"timings_for": lambda self, t, s: {"cases": {b1: 21.5}, "runtime_soc": "soc-a",
                                                          "modality": "profiler"}}
        )()
        timings, _t, _soc, _m = cal._extract_bucket_timings(self.TITLE, SHA, buckets)
        self.assertEqual(timings.get(b1), 21.5)

    def test_backfill_supplies_timings_when_no_log_exists(self) -> None:
        buckets = self._buckets()
        b0 = buckets[0]["id"]
        ws = self._ws(tempfile.mkdtemp(), None)
        cal = self._cal(ws, {b0: 12.0})
        timings, _t, soc, modality = cal._extract_bucket_timings(self.TITLE, SHA, buckets)
        self.assertEqual(timings, {b0: 12.0})
        self.assertEqual((soc, modality), ("soc-a", "profiler"))

    def test_no_store_keeps_legacy_behaviour(self) -> None:
        buckets = self._buckets()
        b0 = buckets[0]["id"]
        ws = self._ws(tempfile.mkdtemp(), ["RUNTIME_SOC=soc-a", f"MS_PROF case={b0} median=11.00 min=10.0"])
        cal = self._cal(ws, None)
        timings, _t, _s, _m = cal._extract_bucket_timings(self.TITLE, SHA, buckets)
        self.assertEqual(timings, {b0: 11.0})


class _StubEstimator:
    def __init__(self, titles: list[str]) -> None:
        self.problems_meta = {t: {"title": t} for t in titles}


class _StubCalibrator:
    """`unpaired_buckets()` / `load_or_calibrate()` over an in-memory census."""

    def __init__(self, census: dict[str, list[str]], sha: str = SHA) -> None:
        self._census = census
        self.sha = sha

    def unpaired_buckets(self, title: str) -> dict:
        ids = [str(x) for x in self._census.get(title) or []]
        return {
            "unpaired_details": [
                {"id": i, "stratum": "M_MID_UB", "dtype": "float32", "description": ""} for i in ids
            ],
            "unpaired_buckets": list(ids),
            "unpaired_count": len(ids),
            "baseline_sha": self.sha,
            "baseline_short": self.sha[:7],
            "modality": "profiler",
            "runtime_soc": "soc-a",
        }

    def load_or_calibrate(self, title: str) -> dict:
        return {"buckets": []}


def _fixture_workspace(root: pathlib.Path, harness: dict[str, list[str]]) -> pathlib.Path:
    """`<root>/bench/<Title>/validation/perf.py` with a declared case table."""
    (root / "bench").mkdir(parents=True, exist_ok=True)
    for title, ids in harness.items():
        vdir = root / "bench" / title / "validation"
        vdir.mkdir(parents=True, exist_ok=True)
        body = "".join(f"    ('{bid}', 'float32'),\n" for bid in ids)
        (vdir / "perf.py").write_text("CASES = [\n" + body + "]\n", encoding="utf-8")
    (root / "bench" / "problems.json").write_text("{}\n", encoding="utf-8")
    return root


class _CensusScheduler(BackfillScheduler):
    """The **real** `_census_rows()` detection path over a fixture workspace."""

    def __init__(
        self,
        workspace: pathlib.Path,
        state_dir: pathlib.Path,
        census: dict[str, list[str]],
        **kw,
    ) -> None:
        super().__init__(
            workspace,
            state_dir=state_dir,
            estimator=_StubEstimator(list(census)),
            calibrator=_StubCalibrator(census),
            **kw,
        )
        self.executed: list[dict] = []

    def _recording_runner(self, lines: list[str]):
        def run(cfg: dict) -> dict:
            self.executed.append(cfg)
            return {"ok": bool(lines), "timing_lines": lines, "log": "/tmp/fake.log", "die": 0,
                    "lock_wait_sec": 0.0}

        return run


class SpecPlaceholderTest(unittest.TestCase):
    """`*_proxy`, `<op>:median`, and unharnessed ids are documentation, not cases.

    All three [§6.5.1] rules must keep such an id out of `unpaired`/`queued`/`eta`, while a
    *real* case the harness failed to emit keeps flowing to `unmeasurable[case_not_in_harness]`.
    """

    CENSUS = {
        "Alpha": ["real_a1", "real_a2", "bf16_128x256_proxy", "Alpha:median", "ghost_bucket"],
        "Beta": ["only_placeholder_proxy", "beta_missing_case"],  # 100 % placeholder
        "Gamma": ["g_real"],                                     # healthy, a real deficit
    }
    HARNESS = {
        "Alpha": ["real_a1", "real_a2"],
        "Beta": ["b_other_case"],
        "Gamma": ["g_real"],
    }

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.ws = _fixture_workspace(self.root / "ws", self.HARNESS)
        self.state = self.root / "state"
        self.state.mkdir()
        self.sched = self._scheduler()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _scheduler(self) -> _CensusScheduler:
        return _CensusScheduler(self.ws, self.state, self.CENSUS)

    # -- rule 1..3 ---------------------------------------------------------------
    def test_three_rules_and_their_precedence(self) -> None:
        harness = "CASES = [\n    ('k_8x4096_mean', 'float32'),\n]\n"
        # rule 1: spectrum padding
        self.assertEqual(spec_placeholder_reason("t256_c1024_b16_proxy", harness), "proxy_suffix")
        self.assertEqual(spec_placeholder_reason("a_b_proxy", ""), "proxy_suffix")
        # rule 2: parser artifacts (`PROF_SUMMARY` leaks `<kernel>:metric` keys)
        self.assertEqual(spec_placeholder_reason("PReluGradUpdate:median", harness), "parser_artifact")
        self.assertEqual(spec_placeholder_reason("PReluGradUpdate:min", harness), "parser_artifact")
        # rule 3: the harness never names the id
        self.assertEqual(
            spec_placeholder_reason("sbn_w4_c64_small", harness), "not_in_harness_case_list"
        )
        self.assertIsNone(spec_placeholder_reason("k_8x4096_mean", harness))  # named
        # `None` ("no harness to ask") is not `False`: a missing repo suppresses nothing
        self.assertIsNone(spec_placeholder_reason("sbn_w4_c64_small", ""))
        self.assertIsNone(spec_placeholder_reason("sbn_w4_c64_small", None))
        # an id the lane already attempted is the ledger's business, never a placeholder
        self.assertIsNone(spec_placeholder_reason("sbn_w4_c64_small", harness, attempted=True))
        self.assertIsNone(spec_placeholder_reason("", harness))
        self.assertIsNone(spec_placeholder_reason(None, harness))
        # the token guard must not let `np.floor` vouch for a case named `floor`
        self.assertFalse(harness_names_case("x = np.floor(1.0)", "floor"))
        self.assertTrue(harness_names_case("CASES = [('floor', 'float32')]", "floor"))

    def test_census_splits_placeholders_from_real_cases(self) -> None:
        real = self.sched.scan_unpaired()
        ph = self.sched.spec_placeholders()
        self.assertEqual(
            [(r["title"], r["bucket"]) for r in real],
            [("Alpha", "real_a1"), ("Alpha", "real_a2"), ("Gamma", "g_real")],
        )
        self.assertEqual(
            sorted((r["title"], r["bucket"], r["placeholder_reason"]) for r in ph),
            [
                ("Alpha", "Alpha:median", "parser_artifact"),
                ("Alpha", "bf16_128x256_proxy", "proxy_suffix"),
                ("Alpha", "ghost_bucket", "not_in_harness_case_list"),
                ("Beta", "beta_missing_case", "not_in_harness_case_list"),
                ("Beta", "only_placeholder_proxy", "proxy_suffix"),
            ],
        )
        # every census row is classified exactly once
        self.assertEqual(len(real) + len(ph), sum(len(v) for v in self.CENSUS.values()))

    # -- contract fields ---------------------------------------------------------
    def test_status_fields_and_queue_hygiene(self) -> None:
        payload = self.sched.status(npu=_free_npu())
        self.assertEqual(payload["unpaired_total"], 3)
        self.assertEqual(payload["unpaired_count"], 3)
        self.assertEqual(payload["spec_placeholder_total"], 5)
        self.assertEqual(payload["spec_placeholder_by_operator"], {"Alpha": 3, "Beta": 2})
        self.assertEqual(len(payload["spec_placeholder"]), 5)
        self.assertIn("Beta|only_placeholder_proxy", payload["spec_placeholder_keys"])
        self.assertTrue(all(r["placeholder_reason"] for r in payload["spec_placeholder"]))
        self.assertTrue(all(not r["placeholder"] for r in payload["unpaired"]))

        self.assertEqual([q["title"] for q in payload["queued"]], ["Alpha", "Gamma"])
        self.assertEqual(payload["queued_count"], 2)
        self.assertNotIn("Beta", [q["title"] for q in payload["queued"]])
        for job in payload["queued"]:
            for bid in job["buckets"]:
                self.assertIsNone(spec_placeholder_reason(bid, self.sched._harness_source(job["title"])))
        # eta only pays for real sessions
        self.assertEqual(
            payload["eta_sec"], round(sum(j["budget_sec"] for j in payload["queued"]), 1)
        )

    def test_placeholder_only_operator_is_zero_and_not_queued(self) -> None:
        payload = self.sched.status(npu=_free_npu())
        self.assertNotIn("Beta", payload["unpaired_operators"])
        self.assertEqual(self.sched.summary({"Beta": {
            "unpaired_bucket_count": 2,
            "unpaired_buckets": ["only_placeholder_proxy", "beta_missing_case"],
        }})["unpaired_by_operator"], {})  # banner number is 0
        self.sched.save_config(min_interval_sec=0.0)
        out = self.sched.dispatch(force=True, dry_run=False, npu=_free_npu(), title="Beta")
        self.assertFalse(out.get("dispatched"))
        self.assertEqual(out["reason"], "queue_empty")
        self.assertEqual(self.sched.executed, [])

    def test_placeholder_never_reaches_the_ledger(self) -> None:
        self.sched.save_config(min_interval_sec=0.0)
        self.sched.runner = self.sched._recording_runner(["MS_PROF case=real_a1 median=9.0 min=8.0"])
        self.sched.dispatch(force=True, dry_run=False, npu=_free_npu(), title="Alpha")
        req = self.sched.executed[-1]["buckets"]
        self.assertEqual(req, ["real_a1", "real_a2"])
        self.assertFalse(any(spec_placeholder_reason(b) for b in req))

    # -- requirement 4: a real case the harness lacked is *not* a placeholder -----
    def test_attempted_case_missing_from_the_run_stays_unmeasurable(self) -> None:
        harness = self.sched._harness_source("Alpha")
        self.assertTrue(harness_names_case(harness, "real_a1"))
        rec = {
            "event": "finished", "lane": "B0", "title": "Alpha", "result": "case_not_in_harness",
            "ended_epoch": self.sched.clock(), "buckets": ["real_a1"],
            "bucket_keys": [bucket_key("Alpha", "real_a1", SHA)],
        }
        self.sched.ledger.append(rec)
        payload = self.sched.status(npu=_free_npu())
        unmeasurable = {r["title"]: r for r in payload["unmeasurable"]}
        self.assertIn("Alpha", unmeasurable)
        self.assertEqual(unmeasurable["Alpha"]["reason"], "case_not_in_harness")
        self.assertEqual(unmeasurable["Alpha"]["buckets"], ["real_a1"])
        self.assertTrue(payload["unmeasurable"][0].get("reason"))
        self.assertNotIn(
            "Alpha|real_a1", payload["spec_placeholder_keys"]
        )  # a real case, never a spec placeholder
        self.assertIn(("Alpha", "real_a1"), {(r["title"], r["bucket"]) for r in payload["unpaired"]})
        # the failed bucket is blocked (not retried) while the operator's other case still runs
        queued = {q["title"]: q["buckets"] for q in payload["queued"]}
        self.assertEqual(queued, {"Alpha": ["real_a2"], "Gamma": ["g_real"]})

    def test_unknown_harness_never_demotes_a_bucket_list(self) -> None:
        # a workspace whose `bench/<Title>/validation` is absent: "no opinion"
        bare = _fixture_workspace(self.root / "bare", {})
        sched = _CensusScheduler(bare, self.root / "state2", self.CENSUS)
        self.assertEqual(
            {r["bucket"] for r in sched.spec_placeholders()},
            {"bf16_128x256_proxy", "Alpha:median", "only_placeholder_proxy"},
        )
        self.assertEqual(
            sorted(r["bucket"] for r in sched.scan_unpaired()),
            ["beta_missing_case", "g_real", "ghost_bucket", "real_a1", "real_a2"],
        )

    def test_summary_subtracts_placeholders_from_cached_docs(self) -> None:
        docs = {
            "Alpha": {"unpaired_bucket_count": 5, "unpaired_buckets": list(self.CENSUS["Alpha"])},
            "Beta": {"unpaired_bucket_count": 2, "unpaired_buckets": list(self.CENSUS["Beta"])},
            "Gamma": {"unpaired_bucket_count": 1},
        }
        summary = self.sched.summary(docs)
        self.assertEqual(summary["unpaired_total"], 3)      # Alpha 2 + Gamma 1 (count fallback)
        self.assertEqual(summary["unpaired_by_operator"], {"Alpha": 2, "Gamma": 1})
        self.assertEqual(summary["spec_placeholder_total"], 5)
        self.assertEqual(summary["spec_placeholder_by_operator"], {"Alpha": 3, "Beta": 2})
        self.assertIn("Beta|beta_missing_case", summary["spec_placeholder_keys"])



# The live-workspace invariants below assert the ops-bench fixture set (ConcatD …), not just
# "some manifest exists": pointing the active workspace at another contest must skip them
# instead of failing on unknown titles.
_LIVE_FIXTURE_TITLES = ("ConcatD", "PadV3Grad", "ConfusionTransposeD")


def _has_live_fixtures() -> bool:
    if not (REAL_WORKSPACE / "bench" / "problems.json").is_file():
        return False
    return all((BENCH_SPECS / t / "cases_perf.json").is_file() for t in _LIVE_FIXTURE_TITLES)


@unittest.skipUnless(_has_live_fixtures(), "ops-bench live fixtures unavailable")
class RealWorkspaceReadOnlyTest(unittest.TestCase):
    """Read-only invariants against the live 10-operator workspace (never writes state/)."""

    def test_census_matches_calibration_unpaired_count(self) -> None:
        from scheduler.calibration import BenchmarkCalibrator
        from scheduler.estimator import ScoreEstimator

        est = ScoreEstimator(REAL_WORKSPACE)
        cal = BenchmarkCalibrator(REAL_WORKSPACE, estimator=est)
        checked = 0
        for title in sorted(est.problems_meta):
            if not (BENCH_SPECS / title / "cases_perf.json").is_file():
                continue
            census = cal.unpaired_buckets(title)
            doc = cal.calibrate_problem(title, persist=False)
            self.assertEqual(census["unpaired_count"], doc["unpaired_bucket_count"], title)
            self.assertEqual(
                sorted(census["unpaired_buckets"]), sorted(doc["unpaired_buckets"]), title
            )
            checked += 1
        self.assertGreaterEqual(checked, 8)

    def test_placeholder_split_is_a_partition_of_the_live_census(self) -> None:
        """`unpaired_total + spec_placeholder_total ==` the calibration census, on the real specs."""
        from scheduler.calibration import BenchmarkCalibrator
        from scheduler.estimator import ScoreEstimator

        est = ScoreEstimator(REAL_WORKSPACE)
        cal = BenchmarkCalibrator(REAL_WORKSPACE, estimator=est)
        with tempfile.TemporaryDirectory() as tmp:
            sched = BackfillScheduler(
                REAL_WORKSPACE, state_dir=pathlib.Path(tmp), estimator=est, calibrator=cal
            )
            real = sched.scan_unpaired()
            placeholders = sched.spec_placeholders()
            self.assertFalse((pathlib.Path(tmp) / "backfill_ledger.jsonl").is_file())
        total = 0
        for title in sorted(est.problems_meta):
            if not (BENCH_SPECS / title / "cases_perf.json").is_file():
                continue
            total += int(cal.calibrate_problem(title, persist=False)["unpaired_bucket_count"])
        self.assertEqual(len(real) + len(placeholders), total)
        self.assertTrue(placeholders, "the live specs still carry *_proxy rows")
        self.assertTrue(all(r["placeholder_reason"] for r in placeholders))
        self.assertTrue(all(not r["placeholder"] for r in real))
        # a `*_proxy` id must never be counted as a real case
        self.assertFalse([r for r in real if str(r["bucket"]).endswith("_proxy")])

    def test_bridge_status_is_additive_and_fast(self) -> None:
        import time as _time

        from scheduler.cli_bridge import SchedulerEngine

        with tempfile.TemporaryDirectory() as tmp:
            t0 = _time.monotonic()
            engine = SchedulerEngine(workspace=REAL_WORKSPACE, state_dir=pathlib.Path(tmp))
            payload = engine.status(offline=True)
            self.assertLess(_time.monotonic() - t0, 5.0)
        for legacy in ("npu_queue", "submit_queue", "quota", "calibrations", "problem_priorities", "config"):
            self.assertIn(legacy, payload)
        bf = payload["backfill"]
        self.assertEqual(bf["lane"], "B0")
        self.assertEqual(bf["tier"], 0)
        self.assertIn("unpaired_total", bf)

    def test_backfill_dry_run_offline_touches_nothing(self) -> None:
        from scheduler.cli_bridge import SchedulerEngine

        with tempfile.TemporaryDirectory() as tmp:
            state = pathlib.Path(tmp)
            engine = SchedulerEngine(workspace=REAL_WORKSPACE, state_dir=state)
            out = engine.backfill(title="ConcatD", dry_run=True, offline=True)
            self.assertTrue(out["ok"])
            self.assertFalse(out["dispatched"])
            self.assertIn(out["reason"], ("queue_empty", "dry_run", "dry_run_admission_blocked"))
            self.assertFalse((state / "backfill_ledger.jsonl").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
