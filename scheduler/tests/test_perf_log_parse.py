"""Regression: aggregate statistic keys must never enter the case-id pool.

`PROF_SUMMARY` reports *kernel-level* aggregates (`<kernel>.median_us`, `<kernel>.min_us`,
operator `custom_median_us`).  `estimator.parse_validation_perf()` used to publish them as
`profiler_cases[f"{kernel}:median"]` / `f"{kernel}:min"`, and because `bench-specs` carries two
bucket ids spelled exactly `<operator>:min` / `<operator>:median`, that parser artifact became a
fake "paired" bucket with real score headroom (§6.5.1 of `SCHEDULER_DESIGN.md`).

The pool is now case-names-only; the aggregates live in `prof_kernel_stats` / `prof_summary`.

    python3 -m pytest scheduler/tests -q
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from scheduler.backfill import spec_placeholder_reason  # noqa: E402
from scheduler.estimator import parse_validation_perf  # noqa: E402


def _find_workspace(start: pathlib.Path) -> pathlib.Path:
    for cand in (start, *start.parents):
        if (cand / "bench" / "problems.json").is_file():
            return cand
    return start.parent


REAL_WORKSPACE = _find_workspace(pathlib.Path(__file__).resolve())

#: One PReluGradUpdate-shaped log: kernel aggregates only, no per-case row.
PROF_ONLY_LOG = (
    "RUNTIME_SOC=soc-a\n"
    'PROF_SUMMARY {"file": "/tmp/prlgu_prof/PROF_000001/op_summary.csv", '
    '"kernels": {"PReluGradUpdate": {"n": 6, "median_us": 9.37, "min_us": 8.68, "p50_us": 9.4}}, '
    '"custom_median_us": 9.37, "torchref_median_us": 8.1}\n'
)

#: The same aggregates next to genuine case rows; the real cases must still be parsed.
MIXED_LOG = (
    "RUNTIME_SOC=soc-a\n"
    'PROF_SUMMARY {"kernels": {"PReluGradUpdate": {"n": 6, "median_us": 9.37, "min_us": 8.68}}, '
    '"custom_median_us": 9.37}\n'
    "MS_PROF case=4d_16_64_32_32 median=9.37 min=8.68\n"
    "KPERF 2d_100_3 median= 5.0us min= 4.5us\n"
    'KPERF_JSON [{"case": "real_case_a", "median_us": 7.5}]\n'
    'KERNEL_JSON {"case": "real_case_b", "median_us": 6.25}\n'
    'KERNEL_PERF {"case": "real_case_c", "kernel_median_us": 5.5}\n'
    'PERF {"case": "real_case_d", "ops": {"op": {"median_us": 4.5}}}\n'
    'PERF_JSON [{"case": "dev_case", "dev_us": 12.0}]\n'
)


def _aggregate_keys(pool: dict[str, float]) -> list[str]:
    """Pool keys that sdkot be a harness case name: `*:stat` or `__sentinel__`."""
    return [k for k in pool if ":" in k or k.startswith("__")]


class AggregateKeysAreNotCasesTest(unittest.TestCase):
    def test_prof_summary_only_log_yields_no_case(self) -> None:
        parsed = parse_validation_perf(PROF_ONLY_LOG)
        self.assertEqual(parsed["profiler_cases"], {})
        self.assertEqual(parsed["cases"], {})
        self.assertFalse(parsed["is_profiler"])
        # provenance and the raw aggregates survive, just not as case ids
        self.assertEqual(parsed["source"], "prof_summary")
        self.assertEqual(
            parsed["prof_kernel_stats"]["PReluGradUpdate"],
            {"median_us": 9.37, "min_us": 8.68, "n": 6.0},
        )
        self.assertEqual(parsed["prof_summary"]["custom_median_us"], 9.37)
        self.assertEqual(parsed["prof_summary"]["torchref_median_us"], 8.1)
        self.assertEqual(parsed["runtime_soc"], "soc-a")

    def test_aggregate_stat_keys_never_become_case_ids(self) -> None:
        for log in (PROF_ONLY_LOG, MIXED_LOG):
            parsed = parse_validation_perf(log)
            self.assertEqual(_aggregate_keys(parsed["profiler_cases"]), [])
            self.assertEqual(_aggregate_keys(parsed["cases"]), [])
            self.assertEqual(_aggregate_keys(parsed["torch_cases"]), [])

    def test_min_median_p50_do_not_invent_buckets(self) -> None:
        parsed = parse_validation_perf(MIXED_LOG)
        self.assertEqual(
            sorted(parsed["profiler_cases"]),
            sorted(["4d_16_64_32_32", "2d_100_3", "real_case_a", "real_case_b", "real_case_c", "real_case_d"]),
        )
        self.assertEqual(parsed["profiler_cases"]["4d_16_64_32_32"], 9.37)  # case= row keeps `median=`
        for suffix in ("min", "median", "p50"):
            for name in ("PReluGradUpdate", "4d_16_64_32_32"):
                fake = f"{name}:{suffix}"
                self.assertNotIn(fake, parsed["profiler_cases"])
                self.assertIsNotNone(spec_placeholder_reason(fake))

    def test_spec_artifact_ids_can_no_longer_be_paired(self) -> None:
        """The two PReluGradUpdate spec buckets are unpaired forever, by construction."""
        parsed = parse_validation_perf(PROF_ONLY_LOG)
        for bid in ("PReluGradUpdate:min", "PReluGradUpdate:median"):
            self.assertNotIn(bid, parsed["profiler_cases"])
            self.assertNotIn(bid, parsed["cases"])
            self.assertEqual(spec_placeholder_reason(bid), "parser_artifact")


@unittest.skipUnless(
    (REAL_WORKSPACE / "results").is_dir(), "real results/ corpus unavailable"
)
class LiveCorpusHasNoAggregateKeysTest(unittest.TestCase):
    """Invariant over every real `validation.log` (333 logs today, < 1 s)."""

    def test_no_validation_log_leaks_an_aggregate_into_the_case_pool(self) -> None:
        logs = sorted((REAL_WORKSPACE / "results").glob("*/*/validation.log"))
        self.assertGreater(len(logs), 100)
        offenders: list[tuple[str, str, str]] = []
        for path in logs:
            parsed = parse_validation_perf(path.read_text(errors="replace"))
            for key in _aggregate_keys(parsed["profiler_cases"]):
                offenders.append((path.parent.parent.name, path.parent.name[:7], key))
        self.assertEqual(offenders, [])

    def test_the_prlu_prof_summary_log_still_carries_kernel_evidence(self) -> None:
        with_prof_only = [
            path
            for path in sorted((REAL_WORKSPACE / "results" / "PReluGradUpdate").glob("*/validation.log"))
            if "PROF_SUMMARY " in (txt := path.read_text(errors="replace"))
            and "KPERF_JSON " not in txt
            and "MS_PROF " not in txt
            and "KPERF " not in txt
        ]
        self.assertTrue(with_prof_only, "PReluGradUpdate has PROF_SUMMARY-only legacy logs")
        parsed = parse_validation_perf(with_prof_only[-1].read_text(errors="replace"))
        self.assertIn("PReluGradUpdate", parsed["prof_kernel_stats"])
        self.assertEqual(parsed["profiler_cases"], {})  # no case-level row in this log at all


if __name__ == "__main__":
    unittest.main(verbosity=2)


class HardwareEvidenceShadowsHostEnqueueTest(unittest.TestCase):
    """Aggregate-only `profiler` evidence must not silently become `exec=` evidence.

    The ladder `profiler > exec > dev_us` exists because the host-enqueue floor
    (`~11.1 us` on `soc-a`) is a different physical quantity.  A log that *did* run
    `profiler` but only returned kernel aggregates has no case-level profiler timing — so the
    correct answer is "profiler evidence, nothing attributable", not "fall through to exec=".
    """

    LOG_HEAD = "RUNTIME_SOC=soc-a\n"
    PROF = (
        'PROF_SUMMARY {"kernels": {"Foo": {"n": 4, "median_us": 9.0, "min_us": 8.0}}, '
        '"custom_median_us": 9.0}\n'
    )
    EXEC = "case_a ws=1 call= 1.0/ 1.0/ 1.0 exec= 40.0\ncase_b ws=1 call= 1.0/ 1.0/ 1.0 exec= 55.0\n"

    def _timings(self, log_text: str) -> tuple[dict[str, float], str | None, str]:
        from scheduler.calibration import BenchmarkCalibrator

        with tempfile.TemporaryDirectory() as tmp:
            ws = pathlib.Path(tmp) / "ws"
            run = ws / "results" / "FooOp" / ("a" * 40)
            run.mkdir(parents=True)
            (run / "validation.log").write_text(log_text, encoding="utf-8")
            cal = BenchmarkCalibrator(ws, state_dir=pathlib.Path(tmp) / "state")
            timings, _torch, soc, modality = cal._extract_bucket_timings(
                "FooOp", "a" * 40, [{"id": "case_a"}, {"id": "case_b"}], allow_future_siblings=False
            )
        return timings, soc, modality

    def test_aggregate_only_profiler_does_not_downgrade_to_exec(self) -> None:
        parsed = parse_validation_perf(self.LOG_HEAD + self.PROF + self.EXEC)
        self.assertTrue(parsed["has_hw_evidence"])
        self.assertEqual(parsed["profiler_cases"], {})
        self.assertTrue(parsed["same_soc_exec_cases"])  # the exec rows are still there
        timings, soc, modality = self._timings(self.LOG_HEAD + self.PROF + self.EXEC)
        self.assertEqual(modality, "profiler")       # provenance kept ...
        self.assertEqual(timings, {})              # ... but nothing is attributable to a case
        self.assertEqual(soc, "soc-a")

    def test_without_hardware_evidence_the_exec_fallback_still_works(self) -> None:
        parsed = parse_validation_perf(self.LOG_HEAD + self.EXEC)
        self.assertFalse(parsed["has_hw_evidence"])
        timings, _soc, modality = self._timings(self.LOG_HEAD + self.EXEC)
        self.assertEqual(modality, "exec")
        self.assertEqual(sorted(timings), ["case_a", "case_b"])
        self.assertAlmostEqual(timings["case_a"], 32.8, places=3)  # 40.0 - 7.2 host floor
