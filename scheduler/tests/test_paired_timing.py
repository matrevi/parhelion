"""Paired A/B intervals, timing-source selection and the gate's preference for paired evidence."""
from __future__ import annotations

import json
import math
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from scheduler import paired_timing, timing  # noqa: E402
from scheduler.estimator import ScoreEstimator  # noqa: E402

CAND, BASE = "c" * 40, "b" * 40


def _write(folder: pathlib.Path, sha: str, run: str, cases: list[dict], finished: str, **extra) -> None:
    path = folder / sha / "bench-runs" / run / "report.json"
    path.parent.mkdir(parents=True)
    doc = {"formal": True, "verified": True, "exit_code": 0, "sha": sha, "timing_score_eligible": True,
           "suite_version": 0, "finished_at": finished, "run_id": run, "cases": cases}
    doc.update(extra)
    path.write_text(json.dumps(doc))


def _case(cid, event, profiler=None):
    row = {"id": cid, "source": "official", "status": "PASS", "event_min_us": event,
           "timing_source": "profiler" if profiler else "event", "min_us": profiler or event}
    if profiler:
        row["profiler_min_us"] = profiler
    return row


class IntervalTest(unittest.TestCase):
    def test_log_t_interval_of_paired_ratios(self) -> None:
        a = [10.0, 10.2, 9.9, 10.1, 10.0]
        b = [20.0, 20.6, 19.7, 20.1, 20.2]
        ci = paired_timing.interval(a, b)
        self.assertAlmostEqual(ci["ratio"], 2.0, delta=0.01)
        self.assertLess(ci["lo"], ci["ratio"])
        self.assertGreater(ci["hi"], ci["ratio"])
        self.assertEqual((ci["n"], ci["method"]), (5, "log_t"))
        d = [math.log(y / x) for x, y in zip(a, b)]
        half = 2.776 * (sum((v - sum(d) / 5) ** 2 for v in d) / 4) ** 0.5 / 5 ** 0.5
        self.assertAlmostEqual(ci["hi"], round(math.exp(sum(d) / 5 + half), 4), places=4)

    def test_overhead_is_removed_before_the_logs(self) -> None:
        a, b = [4.0, 4.0, 4.1], [6.0, 6.1, 6.0]
        self.assertGreater(paired_timing.interval(a, b, offset_us=2.0)["ratio"],
                           paired_timing.interval(a, b)["ratio"])

    def test_bootstrap_is_deterministic_and_close_to_t(self) -> None:
        a = [10.0, 10.2, 9.9, 10.1, 10.0, 10.3]
        b = [15.0, 15.5, 14.8, 15.2, 15.1, 15.6]
        boot = paired_timing.bootstrap(a, b)
        self.assertEqual(boot, paired_timing.bootstrap(a, b))
        t = paired_timing.interval(a, b)
        self.assertLess(abs(math.log(boot["lo"] / t["lo"])), 0.02)

    def test_pairing_removes_drift_that_separate_runs_keep(self) -> None:
        drift = [1.0, 1.4, 0.8, 1.3, 0.9]  # the host's timer mode moves both arms together
        a = [10.0 * k for k in drift]
        b = [12.0 * k for k in drift]
        paired = paired_timing.interval(a, b)
        apart = paired_timing.unpaired_interval(a, b)
        self.assertAlmostEqual(paired["ratio"], 1.2, places=3)
        self.assertLess(paired_timing.width(paired), 0.001)
        self.assertGreater(paired_timing.width(apart), 0.3)

    def test_single_pair_has_no_interval(self) -> None:
        self.assertEqual(paired_timing.interval([2.0], [3.0])["lo"], None)
        self.assertIsNone(paired_timing.interval([], []))


class SourceTest(unittest.TestCase):
    def test_case_value_reads_one_timer_and_schema_one_reports(self) -> None:
        row = _case("01", 20.0, 4.0)
        self.assertEqual(timing.case_value(row, "profiler"), 4.0)
        self.assertEqual(timing.case_value(row, "event"), 20.0)
        old = {"id": "01", "min_us": 21.0}  # report schema 1
        self.assertEqual(timing.case_value(old, "event"), 21.0)
        self.assertIsNone(timing.case_value(old, "profiler"))

    def test_order_attributed_profiles_are_ignored(self) -> None:
        bad = {"device_batch": {"profiler": [{"notes": ["630 tasks / 105 executions (order)"]}]},
               "cases": [_case("01", 20.0, 900.0)]}
        good = {"device_batch": {"profiler": [{"notes": ["96 tasks, 90 of 90 executions attributed"]}]},
                "cases": [_case("01", 21.0, 4.0)]}
        self.assertFalse(timing.profiler_trusted(bad))
        self.assertEqual(timing.best_minima([bad, good], "profiler"), {"01": 4.0})
        self.assertEqual(timing.best_minima([bad, good], "event"), {"01": 20.0})  # event stays valid
        self.assertIsNone(timing.doc_value(bad, bad["cases"][0], "profiler"))

    def test_profiler_is_used_only_when_it_covers_every_case(self) -> None:
        cand = {"profiler": {"01": 4.0, "02": 8.0}, "event": {"01": 20.0, "02": 25.0}}
        base = {"profiler": {"01": 5.0, "02": 9.0}, "event": {"01": 22.0, "02": 26.0}}
        self.assertEqual(timing.common_source(cand, base)[0], "profiler")
        base["profiler"].pop("02")
        source, a, b = timing.common_source(cand, base)
        self.assertEqual((source, a["01"], b["01"]), ("event", 20.0, 22.0))
        self.assertEqual(timing.common_source(cand, {"event": {}}), None)


class GatePreferenceTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = pathlib.Path(tmp.name)
        self.folder = self.ws / "results" / "Op"
        self.est = ScoreEstimator.__new__(ScoreEstimator)
        self.est.workspace = self.ws
        self.est.outcomes = []

    def test_separate_runs_pair_within_one_timer(self) -> None:
        _write(self.folder, BASE, "b1", [_case("01", 20.0, 4.0), _case("02", 30.0, 8.0)], "2026-11-26T01:00:00+00:00")
        _write(self.folder, CAND, "c1", [_case("01", 21.0, 2.0), _case("02", 29.0, 4.0)], "2026-11-26T02:00:00+00:00")
        pair = self.est._case_bench_pair("Op", CAND, BASE)
        self.assertEqual(pair["source"], "profiler")
        self.assertAlmostEqual(pair["geo"], 2.0)
        # The baseline lost its profiler reading on one case: both sides fall back to events.
        _write(self.folder, BASE, "b2", [_case("01", 20.0, 4.0), _case("02", 30.0)], "2026-11-26T03:00:00+00:00")
        (self.folder / BASE / "bench-runs" / "b1" / "report.json").unlink()
        pair = self.est._case_bench_pair("Op", CAND, BASE)
        self.assertEqual(pair["source"], "event")
        self.assertAlmostEqual(pair["geo"], math.sqrt(20 / 21 * 30 / 29))

    def test_paired_run_wins_over_separate_minima(self) -> None:
        _write(self.folder, BASE, "b1", [_case("01", 20.0), _case("02", 30.0)], "2026-11-26T01:00:00+00:00")
        _write(self.folder, CAND, "c1", [_case("01", 10.0), _case("02", 15.0)], "2026-11-26T02:00:00+00:00")
        paired_cases = [dict(_case("01", 10.0), base_status="PASS", pair={"ratio": 1.1, "lo": 1.05, "hi": 1.15, "n": 5}),
                        dict(_case("02", 15.0), base_status="PASS", pair={"ratio": 1.2, "lo": 1.1, "hi": 1.3, "n": 5})]
        _write(self.folder, CAND, "p1", paired_cases, "2026-11-26T03:00:00+00:00", formal=False, verified=False,
               compare={"base_sha": BASE, "source": "event"})
        pair = self.est._case_bench_pair("Op", CAND, BASE)
        self.assertEqual(pair["paired"], "p1")
        self.assertAlmostEqual(pair["geo"], math.sqrt(1.1 * 1.2))
        self.assertEqual(self.est._case_bench_minima("Op", CAND)[2], 1)  # the paired run is not formal

    def test_complete_profiler_minima_outrank_an_event_timed_paired_run(self) -> None:
        _write(self.folder, BASE, "b1", [_case("01", 20.0, 4.0), _case("02", 30.0, 9.0)], "2026-11-26T01:00:00+00:00")
        _write(self.folder, CAND, "c1", [_case("01", 21.0, 2.0), _case("02", 29.0, 3.0)], "2026-11-26T02:00:00+00:00")
        paired_cases = [dict(_case("01", 10.0), base_status="PASS", pair={"ratio": 1.1, "lo": 0.5, "hi": 2.0, "n": 4}),
                        dict(_case("02", 15.0), base_status="PASS", pair={"ratio": 1.2, "lo": 0.6, "hi": 2.2, "n": 4})]
        _write(self.folder, CAND, "p1", paired_cases, "2026-11-26T03:00:00+00:00", formal=False, verified=False,
               compare={"base_sha": BASE, "source": "event"})
        pair = self.est._case_bench_pair("Op", CAND, BASE)
        self.assertEqual((pair["source"], pair.get("paired")), ("profiler", None))
        self.assertAlmostEqual(pair["geo"], math.sqrt(2.0 * 3.0))


if __name__ == "__main__":
    unittest.main()
