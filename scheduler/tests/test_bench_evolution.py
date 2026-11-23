"""Unit & historical regression tests for Parhelion Local Bench Evolution & Calibration v2.0.

Covers:
  - Rich log telemetry parsing (`min_us`, `max_us`, `KERNEL_BINARY_STATS`, `COLD_START_STATS`)
  - Spike-immune robust timings (`compute_robust_timings`)
  - Flat-floor additive thermal drift cancellation (`detect_thermal_drift`) vs true regressions
  - Non-saturating pairwise `ΔΦ` (fixes `beta=3.0` dead-zone saturation)
  - Correlation-adaptive Pairwise Simplex-MAP + `X_EXTREME` cap unlock + `.o` cold-start slope
  - Uncertainty-aware G2 `EXPLORE` channel (structural minority-bucket & `.o`-slimming upside + vetoes)
  - Strictly append-only `evolve_bench` & `bench_feedback.json` persistence (Iron Rule #2)
  - Live-corpus causal regressions (`PadV3Grad 37fd777`, `LayerNormV3 b676b33`, `LayerNormV3 4d36702`)

    python3 -m unittest discover -s scheduler/tests -v
"""
from __future__ import annotations

import datetime
import json
import math
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from scheduler.bench_evolution import (  # noqa: E402
    append_buckets_to_spec,
    apply_proxy_aliases,
    build_evolution_plan,
    build_pairwise_phi_delta,
    compute_robust_timings,
    compute_spearman,
    detect_thermal_drift,
    evaluate_explore_eligibility,
    fit_pairwise_simplex_map,
)
from scheduler.calibration import BenchmarkCalibrator  # noqa: E402
from scheduler.estimator import ProblemLedger, parse_validation_perf, resolve_sha  # noqa: E402
from scheduler.gate import SubmissionGate  # noqa: E402
from scheduler.pairwise_anchor import select_dual_anchor_prediction  # noqa: E402


def _find_workspace(start: pathlib.Path) -> pathlib.Path:
    for cand in (start, *start.parents):
        if (cand / "bench" / "problems.json").is_file():
            return cand
    return start.parent


REAL_WORKSPACE = _find_workspace(pathlib.Path(__file__).resolve())


class RichTelemetryAndDenoisingTest(unittest.TestCase):
    def test_parses_min_max_binary_and_cold_start_stats(self) -> None:
        log = (
            "RUNTIME_SOC=soc-a\n"
            'KERNEL_BINARY_STATS {"count": 4, "max_bytes": 176128, "sum_bytes": 512000, "max_text_bytes": 98304}\n'
            'KPERF_JSON [{"case": "tiny_a", "block_dim": 20, "median_us": 3.14, "min_us": 1.60, "max_us": 4.20}]\n'
            'COLD_START_STATS {"cases": 1, "first_iter_us": 4.2, "cold_spread_median_us": 2.6}\n'
            "MS_PROF case=mid_b median=10.0 min=9.2 max=11.5\n"
        )
        parsed = parse_validation_perf(log)
        self.assertEqual(parsed["profiler_cases"], {"tiny_a": 3.14, "mid_b": 10.0})
        self.assertEqual(parsed["profiler_min_cases"], {"tiny_a": 1.60, "mid_b": 9.2})
        self.assertEqual(parsed["profiler_max_cases"], {"tiny_a": 4.20, "mid_b": 11.5})
        self.assertEqual(parsed["profiler_block_dims"], {"tiny_a": 20})
        self.assertEqual(parsed["kernel_binary_stats"]["max_bytes"], 176128)
        self.assertEqual(parsed["cold_start_stats"]["first_iter_us"], 4.2)

    def test_spike_immune_robust_timings_dampens_single_session_jitter(self) -> None:
        # Modeled directly after ConcatD c8b5521 tiny32 (median spiked 2.16->3.14 while min improved 2.05->1.60)
        base_rob = compute_robust_timings({"tiny32": 2.16, "steady": 10.0}, {"tiny32": 2.05, "steady": 9.8})
        cand_rob = compute_robust_timings({"tiny32": 3.14, "steady": 10.0}, {"tiny32": 1.60, "steady": 9.8})
        self.assertGreater(base_rob["tiny32"] / cand_rob["tiny32"], 1.15)
        self.assertAlmostEqual(base_rob["steady"], cand_rob["steady"], places=4)

    def test_proxy_alias_bridges_missing_proxy_suffix(self) -> None:
        aliased = apply_proxy_aliases({"bf16_mid": 4.2, "other": 5.0}, ["bf16_mid_proxy", "other"])
        self.assertEqual(aliased["bf16_mid_proxy"], 4.2)
        self.assertEqual(aliased["bf16_mid"], 4.2)


class ThermalDriftCancellationTest(unittest.TestCase):
    def test_detects_uniform_additive_floor_drift_like_b676b33(self) -> None:
        base = {
            "bf16_1x65536_proxy": 3023.8,
            "bf16_4096x1024_proxy": 211.6,
            "f32_params_axis0_proxy": 13.86,
            "f32_4x6x8": 2.91,
            "f32_256x1000": 7.93,
            "f32_64x1280": 6.44,
            "f32_odd_1023x513_proxy": 9.38,
            "f16_512x512": 6.40,
            "bf16_1024x1024": 93.12,
            "f32_256x300": 5.92,
            "f32_256x512": 5.77,
            "f16_4096x1024": 22.14,
            "f32_1024x1024": 10.07,
        }
        cand = {
            "bf16_1x65536_proxy": 1844.4,
            "bf16_4096x1024_proxy": 145.5,
            "f32_params_axis0_proxy": 9.80,
            "f32_4x6x8": 2.61,
            "f32_256x1000": 8.91,
            "f32_64x1280": 7.45,
            "f32_odd_1023x513_proxy": 10.49,
            "f16_512x512": 7.52,
            "bf16_1024x1024": 94.25,
            "f32_256x300": 7.06,
            "f32_256x512": 6.93,
            "f16_4096x1024": 23.32,
            "f32_1024x1024": 11.38,
        }
        drift, adj = detect_thermal_drift(base, cand)
        self.assertGreater(drift, 0.80)
        self.assertLess(drift, 1.20)
        # Flat-drifted untouched cases snap back near 1.0x
        self.assertGreater(base["f32_256x512"] / adj["f32_256x512"], 0.95)

    def test_does_not_mask_true_multi_bucket_regression_like_4d36702(self) -> None:
        base = {
            "bf16_4096x1024_proxy": 211.6,
            "f32_params_axis0_proxy": 13.86,
            "f32_4x6x8": 2.91,
            "f16_128x256_proxy": 4.67,
            "f32_64x128_proxy": 3.80,
            "f32_odd_1023x513_proxy": 9.38,
            "f16_512x512": 6.40,
            "f32_256x512": 5.77,
            "f32_256x1000": 7.93,
            "f32_1024x1024": 10.07,
        }
        cand = {
            "bf16_4096x1024_proxy": 142.8,
            "f32_params_axis0_proxy": 8.95,
            "f32_4x6x8": 4.87,
            "f16_128x256_proxy": 6.66,
            "f32_64x128_proxy": 5.84,
            "f32_odd_1023x513_proxy": 11.43,
            "f16_512x512": 10.26,
            "f32_256x512": 10.06,
            "f32_256x1000": 12.34,
            "f32_1024x1024": 11.78,
        }
        drift, adj = detect_thermal_drift(base, cand)
        self.assertEqual(drift, 0.0)
        self.assertEqual(adj, cand)

    def test_detects_multiplicative_proportional_drift_and_rejects_slowdowns(self) -> None:
        # 12 buckets spanning 3us..80us: 3 sped up >=1.25x, 9 slowed ~1.09x proportionally
        # (additive diffs range +0.72us..+7.20us so flat-floor additive drift won't trigger).
        base = {
            "fast_a": 20.0, "fast_b": 15.0, "fast_c": 12.0,
            "s1": 3.0, "s2": 4.0,
            "m1": 8.0, "m2": 12.0, "m3": 18.0, "m4": 25.0, "l1": 40.0, "l2": 60.0, "l3": 80.0,
        }
        cand = {
            "fast_a": 14.0, "fast_b": 10.5, "fast_c": 9.0,
            "s1": 3.27, "s2": 4.36,
            "m1": 8.72, "m2": 13.08, "m3": 19.62, "m4": 27.25, "l1": 43.60, "l2": 65.40, "l3": 87.20,
        }
        drift, adj = detect_thermal_drift(base, cand)
        self.assertGreater(drift, 1.0)
        self.assertAlmostEqual(adj["l1"], 43.60 / (1.0 + 0.85 * 0.09), delta=0.15)
        self.assertAlmostEqual(adj["m1"], 8.72 / (1.0 + 0.85 * 0.09), delta=0.05)

        # Reject when >=3 buckets suffer severe regression >1.25x
        bad_reg = dict(cand)
        bad_reg.update({"m1": 10.5, "m2": 15.5, "m3": 23.0})
        d_reg, a_reg = detect_thermal_drift(base, bad_reg)
        self.assertEqual(d_reg, 0.0)
        self.assertEqual(a_reg, bad_reg)

        # Reject when mid/large >=6.0us buckets show 0 drift while only small buckets tick up
        small_only = {k: (v if base[k] >= 6.0 and not k.startswith("fast") else cand[k]) for k, v in base.items()}
        small_only.update({"s3": 4.32, "s4": 3.24, "s5": 3.80})
        base_small = {**base, "s3": 4.0, "s4": 3.0, "s5": 3.5}
        d_sm, _ = detect_thermal_drift(base_small, small_only)
        self.assertEqual(d_sm, 0.0)


class PairwiseCalibrationAndExploreTest(unittest.TestCase):
    def test_phi_delta_excludes_cand_from_tmin_and_raises_soft_cap_to_096(self) -> None:
        # Candidate sets a 2x record (tb=5.0 vs ta=10.0) when historical min_tau[k]=10.0:
        # tmin must stay 10.0 (not 5.0!), and soft saturation knee must sit at 0.96 (not 0.92).
        bids = ["record_set", "near_knee", "above_knee"]
        ta = {"record_set": 10.0, "near_knee": 10.0, "above_knee": 10.0}
        tb = {"record_set": 5.0, "near_knee": 8.42, "above_knee": 6.0}
        min_tau = {"record_set": 10.0, "near_knee": 10.0, "above_knee": 10.0}
        dphi = build_pairwise_phi_delta(bids, ta, tb, min_tau, r_anchor_a=0.80)
        # near_knee: r_a=0.80, raw_rb=0.80*(10/8.42)=0.9501 < 0.96 => linear 90*(0.9501-0.80)=+13.51
        self.assertGreater(dphi[1], 13.0)
        # record_set: raw_rb=1.60 -> 0.96 + 0.04*tanh((1.60-0.96)/0.25) > 0.99 -> dphi > +17.0
        self.assertGreater(dphi[0], 17.0)
        # r_a=0.88*(10/8.9)^0.75=0.96 (capped at 0.96) -> tiny slowdown 10->10.5 drops linearly from 0.96
        dphi_hi = build_pairwise_phi_delta(["b"], {"b": 10.0}, {"b": 10.5}, {"b": 9.0}, r_anchor_a=0.88)
        self.assertLess(dphi_hi[0], -3.0)

    def test_dual_anchor_prefers_best_pass_sha_in_restore_regime(self) -> None:
        latest = {"estimated_score": 60.4, "matched_cases": 24, "regime_mismatch": False, "anchor_kind": "latest"}
        best = {"estimated_score": 72.8, "matched_cases": 24, "regime_mismatch": False, "anchor_kind": "best"}
        chosen = select_dual_anchor_prediction(latest, best, current_score=59.74, best_score=71.33)
        self.assertIs(chosen, best)

        # In non-restore regime (gap < 1.5) with both same-regime, keep latest anchor
        chosen_norm = select_dual_anchor_prediction(latest, best, current_score=70.50, best_score=71.33)
        self.assertIs(chosen_norm, latest)

    def test_predict_candidate_does_not_fold_cand_timings_into_min_tau(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = pathlib.Path(tmp)
            op = ws / "bench" / "DemoOp"
            (op / "op_kernel").mkdir(parents=True)
            (ws / "bench" / "problems.json").write_text(
                json.dumps([{"title": "DemoOp", "slug": "demoop", "difficulty": 2, "path": "bench/DemoOp"}])
            )
            base_sha = "a" * 40
            best_sha = "b" * 40
            cand_sha = "c" * 40
            (ws / "results").mkdir(parents=True)
            (ws / "results" / "score-snapshot.json").write_text(
                json.dumps({"DemoOp": {"status": "Pass", "score": 55.0, "public_times_us": [4.0] * 5}})
            )
            outcomes = [
                {"title": "DemoOp", "sha": best_sha, "canonical_sha": best_sha, "status": "Pass", "score": 70.0, "submitted_at": "2026-11-22T10:00:00+00:00"},
                {"title": "DemoOp", "sha": base_sha, "canonical_sha": base_sha, "status": "Pass", "score": 55.0, "submitted_at": "2026-11-22T12:00:00+00:00"},
            ]
            (ws / "results" / "submit-queue-outcome.jsonl").write_text(
                "\n".join(json.dumps(r) for r in outcomes) + "\n"
            )
            cases_base = [{"mode": "custom", "case": f"b_{i}", "median_us": 10.0} for i in range(10)]
            cases_best = [{"mode": "custom", "case": f"b_{i}", "median_us": 7.5} for i in range(10)]
            cases_cand = [{"mode": "custom", "case": f"b_{i}", "median_us": 6.5} for i in range(10)]
            for s, cs in [(base_sha, cases_base), (best_sha, cases_best), (cand_sha, cases_cand)]:
                d = ws / "results" / "DemoOp" / s
                d.mkdir(parents=True)
                (d / "result.json").write_text(json.dumps({"exit_code": 0, "profiler_regime": "fast_single_session_l0"}))
                (d / "validation.log").write_text(
                    "RUNTIME_SOC=soc-a\nEXTENDED_PASSED 10 cases\nKPERF_JSON " + json.dumps(cs) + "\n"
                )
            cal = BenchmarkCalibrator(ws, state_dir=ws / "state")
            ledger = ProblemLedger(
                title="DemoOp", difficulty=2, board_status="Pass", current_score=55.0, weighted_current_score=110.0,
                public_score=7.0, hidden_score=48.0, implied_hidden_r=0.533, public_r_list=[0.7] * 5,
                frontier_top1=85.0, frontier_top5=75.0, target_score=75.0, recoverable_headroom=30.0,
                theoretical_headroom=45.0, active_board_sha=base_sha, active_judged_score=55.0,
                active_judged_at="2026-11-22T12:00:00+00:00", decay_factor_gamma=1.0,
                raw_historical_best_score=70.0, contemporary_best_score=70.0, historical_best_score=70.0,
                historical_best_mean=70.0, historical_best_sha=best_sha, true_restore_gain=15.0, restore_gap=15.0,
            )
            pred = cal.predict_candidate("DemoOp", cand_sha, base_sha, ledger)
            self.assertIsNotNone(pred)
            assert pred is not None
            self.assertEqual(pred["anchor_kind"], "best")
            self.assertIn(f"anchor=best({best_sha[:7]})", pred["detail"])
            self.assertGreater(pred["estimated_score"], 72.0)
    def test_non_saturating_phi_delta_penalizes_regressions_even_with_extreme_outlier(self) -> None:
        # Even when one bucket has 3000us (which caused V1 beta->3.0 saturation!), small buckets keep full sensitivity
        bids = ["bf16_huge", "f32_mid", "f16_mid"]
        ta = {"bf16_huge": 3000.0, "f32_mid": 5.8, "f16_mid": 6.4}
        tb = {"bf16_huge": 2800.0, "f32_mid": 10.1, "f16_mid": 10.3}
        min_tau = {"bf16_huge": 1800.0, "f32_mid": 5.8, "f16_mid": 6.4}
        dphi = build_pairwise_phi_delta(bids, ta, tb, min_tau, r_anchor_a=0.72)
        self.assertLess(dphi[1], -20.0)  # Strong negative signal, NOT zeroed out!
        self.assertLess(dphi[2], -18.0)

    def test_pairwise_simplex_map_reweights_pos_corr_and_suppresses_anti_corr(self) -> None:
        bids = ["pos_ext", "pos_mid", "anti_mid", "neutral"]
        strata = {"pos_ext": "X_EXTREME", "pos_mid": "M_MID_UB", "anti_mid": "M_MID_UB", "neutral": "S_SMALL_CORE"}
        priors = {"pos_ext": 0.0375, "pos_mid": 0.32, "anti_mid": 0.32, "neutral": 0.3225}
        min_tau = {"pos_ext": 4.0, "pos_mid": 5.0, "anti_mid": 5.0, "neutral": 3.0}

        # Synthetic 6 pairs where pos_ext & pos_mid track dy strongly, anti_mid moves opposite,
        # and larger binary size hurts residual
        pairs = []
        for idx, (s_pos, s_anti, dy, dlb) in enumerate([
            (1.40, 0.75, +8.0, -0.20),
            (1.25, 0.85, +5.0, -0.10),
            (0.75, 1.35, -7.5, +0.25),
            (1.50, 0.70, +9.5, -0.15),
            (0.80, 1.30, -6.0, +0.30),
            (1.15, 0.90, +2.5, +0.05),
        ]):
            ta = {"pos_ext": 6.0, "pos_mid": 7.0, "anti_mid": 7.0, "neutral": 3.2}
            tb = {"pos_ext": 6.0 / s_pos, "pos_mid": 7.0 / s_pos, "anti_mid": 7.0 / s_anti, "neutral": 3.2}
            pairs.append({"t_a": ta, "t_b": tb, "dy": dy, "r_a": 0.55, "sw": 1.0, "d_log_bytes": dlb})

        fit = fit_pairwise_simplex_map(bids, strata, priors, pairs, min_tau)
        w = dict(zip(bids, fit["w_hat"]))
        self.assertGreater(fit["bucket_correlations"]["pos_ext"], 0.8)
        self.assertLess(fit["bucket_correlations"]["anti_mid"], -0.8)
        # X_EXTREME cap unlocked above 0.05 because data strongly supports pos_ext!
        self.assertGreater(w["pos_ext"], 0.08)
        # Anti-correlated bucket heavily suppressed below prior!
        self.assertLess(w["anti_mid"], 0.03)
        self.assertLessEqual(fit["alpha_cold"], 0.0)

    def test_explore_channel_protects_minority_breakthrough_and_binary_slimming(self) -> None:
        bids = ["lg_a", "lg_b", "bigpad", "anti_1", "anti_2"]
        strata = {k: "M_MID_UB" for k in bids}
        base = {"lg_a": 60.0, "lg_b": 50.0, "bigpad": 9.3, "anti_1": 5.0, "anti_2": 5.0}
        cand = {"lg_a": 42.0, "lg_b": 34.0, "bigpad": 6.7, "anti_1": 10.0, "anti_2": 9.5}
        corr = {"lg_a": 0.5, "lg_b": 0.5, "bigpad": 0.6, "anti_1": -0.5, "anti_2": -0.5}
        npairs = {k: 10 for k in bids}

        ok, ucb, reason, ev = evaluate_explore_eligibility(
            delta_pred=-0.8,
            sigma_pred=3.2,
            calibration_trust=0.45,
            noise_floor_pts=2.5,
            bucket_ids=bids,
            strata=strata,
            base_timings=base,
            cand_timings=cand,
            bucket_correlations=corr,
            bucket_pair_counts=npairs,
        )
        self.assertTrue(ok)
        self.assertIn("Structural minority-bucket upside", reason)
        self.assertEqual(ev["strong_improved_count"], 3)

        # Binary slimming >= 20% also triggers EXPLORE
        ok_slim, _, reason_slim, _ = evaluate_explore_eligibility(
            delta_pred=-0.5,
            sigma_pred=3.0,
            calibration_trust=0.60,
            noise_floor_pts=2.5,
            bucket_ids=["a", "b"],
            strata={"a": "M_MID_UB", "b": "M_MID_UB"},
            base_timings={"a": 10.0, "b": 10.0},
            cand_timings={"a": 9.8, "b": 10.1},
            bucket_correlations={},
            bucket_pair_counts={},
            binary_delta_pct=-24.5,
        )
        self.assertTrue(ok_slim)
        self.assertIn("binary slimming", reason_slim.lower())

    def test_explore_vetoes_regression_on_high_corr_buckets(self) -> None:
        bids = ["pos_1", "pos_2", "tail_1", "tail_2", "tail_3"]
        strata = {k: "M_MID_UB" for k in bids}
        base = {"pos_1": 6.0, "pos_2": 8.0, "tail_1": 20.0, "tail_2": 20.0, "tail_3": 20.0}
        cand = {"pos_1": 9.0, "pos_2": 11.0, "tail_1": 12.0, "tail_2": 12.0, "tail_3": 12.0}
        corr = {"pos_1": 0.85, "pos_2": 0.78, "tail_1": 0.1, "tail_2": 0.1, "tail_3": 0.1}
        npairs = {k: 12 for k in bids}

        ok, _, _, ev = evaluate_explore_eligibility(
            delta_pred=-2.0,
            sigma_pred=3.5,
            calibration_trust=0.50,
            noise_floor_pts=2.5,
            bucket_ids=bids,
            strata=strata,
            base_timings=base,
            cand_timings=cand,
            bucket_correlations=corr,
            bucket_pair_counts=npairs,
        )
        self.assertFalse(ok)
        self.assertTrue(ev["veto_pos_corr"])


class AppendOnlyEvolutionTest(unittest.TestCase):
    def test_append_buckets_to_spec_is_strictly_append_only_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sp = pathlib.Path(tmp) / "cases_perf.json"
            orig = {
                "problem": "DemoOp",
                "version": "1.0",
                "hidden_buckets": [
                    {"id": "old_a", "stratum": "S_SMALL_CORE", "dtype": "float32", "prior_w": 0.5, "max_w": 0.25},
                    {"id": "old_b", "stratum": "M_MID_UB", "dtype": "float32", "prior_w": 0.5, "max_w": 0.25},
                ],
            }
            sp.write_text(json.dumps(orig, indent=2), encoding="utf-8")

            plan = build_evolution_plan(
                title="DemoOp",
                spec_doc=orig,
                all_discovered={"old_a": 3.5, "old_b": 8.0, "new_good": 14.0, "new_bad": 9.0},
                bucket_correlations={"new_good": 0.72, "new_bad": -0.45},
                bucket_pair_counts={"new_good": 6, "new_bad": 6},
            )
            self.assertEqual([b["id"] for b in plan["promote_buckets"]], ["new_good"])
            self.assertEqual([b["id"] for b in plan["skipped_anti_corr"]], ["new_bad"])

            res1 = append_buckets_to_spec(sp, plan["promote_buckets"])
            self.assertEqual(res1["appended_ids"], ["new_good"])
            updated = json.loads(sp.read_text(encoding="utf-8"))
            ids = [b["id"] for b in updated["hidden_buckets"]]
            self.assertEqual(ids, ["old_a", "old_b", "new_good"])  # Old buckets preserved in exact order!
            self.assertAlmostEqual(sum(b["prior_w"] for b in updated["hidden_buckets"]), 1.0, places=4)

            # Second run must be a strict no-op (idempotent)
            res2 = append_buckets_to_spec(sp, plan["promote_buckets"])
            self.assertEqual(res2["appended_count"], 0)


@unittest.skipUnless((REAL_WORKSPACE / "results" / "submit-queue-outcome.jsonl").is_file(), "live workspace unavailable")
class LiveHistoricalCausalRegressionTest(unittest.TestCase):
    """End-to-end strictly causal checks on the exact triggering historical events."""

    def test_padv3grad_37fd777_is_not_buried_in_hold_low_gain(self) -> None:
        gate = SubmissionGate(REAL_WORKSPACE)
        sha = resolve_sha(REAL_WORKSPACE, "PadV3Grad", "37fd777")
        # Strictly before 37fd777 was submitted (2026-11-22T16:03:32Z)
        as_of = datetime.datetime(2026, 11, 22, 16, 3, 0, tzinfo=datetime.timezone.utc)
        verdict = gate.evaluate(
            title="PadV3Grad",
            sha=sha,
            as_of=as_of,
            override_board_score=42.45,
            override_board_status="Pass",
        )
        s2 = next(s for s in verdict.stages if s.stage == "NOISE_FLOOR")
        self.assertTrue(s2.passed, f"PadV3Grad 37fd777 (+9.09) must NOT be killed by S2! Got: {s2.reason}")
        self.assertIn(s2.code, ("EXPLORE_HIGH_UPSIDE", "ABOVE_NOISE_FLOOR"))

    def test_layernorm_b676b33_cancels_thermal_drift_and_passes_s2(self) -> None:
        gate = SubmissionGate(REAL_WORKSPACE)
        sha = resolve_sha(REAL_WORKSPACE, "LayerNormV3", "b676b33")
        as_of = datetime.datetime(2026, 11, 22, 16, 0, 0, tzinfo=datetime.timezone.utc)
        verdict = gate.evaluate(
            title="LayerNormV3",
            sha=sha,
            as_of=as_of,
            override_board_score=67.91,
            override_board_status="Pass",
        )
        self.assertGreater(verdict.estimate.thermal_drift_us, 0.7)
        self.assertGreater(verdict.estimate.delta_expected, 2.5)
        s2 = next(s for s in verdict.stages if s.stage == "NOISE_FLOOR")
        self.assertTrue(s2.passed)

    def test_layernorm_4d36702_true_regression_is_held(self) -> None:
        gate = SubmissionGate(REAL_WORKSPACE)
        sha = resolve_sha(REAL_WORKSPACE, "LayerNormV3", "4d36702")
        as_of = datetime.datetime(2026, 11, 22, 14, 18, 0, tzinfo=datetime.timezone.utc)
        verdict = gate.evaluate(
            title="LayerNormV3",
            sha=sha,
            as_of=as_of,
            override_board_score=73.65,
            override_board_status="Pass",
        )
        self.assertLess(verdict.estimate.delta_expected, -5.0)
        s2 = next(s for s in verdict.stages if s.stage == "NOISE_FLOOR")
        self.assertFalse(s2.passed)
        self.assertFalse(verdict.approved)

    def test_prelu_558797b_true_regression_is_held_without_force(self) -> None:
        gate = SubmissionGate(REAL_WORKSPACE)
        sha = resolve_sha(REAL_WORKSPACE, "PReluGradUpdate", "558797b")
        as_of = datetime.datetime(2026, 11, 22, 16, 10, 0, tzinfo=datetime.timezone.utc)
        verdict = gate.evaluate(
            title="PReluGradUpdate",
            sha=sha,
            as_of=as_of,
            override_board_score=78.01,
            override_board_status="Pass",
            force=False,
        )
        s2 = next(s for s in verdict.stages if s.stage == "NOISE_FLOOR")
        self.assertFalse(s2.passed)
        self.assertEqual(verdict.status_tag, "HOLD_LOW_GAIN")

    def test_strided_slice_grad_dd1fefc_uses_best_anchor_in_restore_regime(self) -> None:
        gate = SubmissionGate(REAL_WORKSPACE)
        sha = resolve_sha(REAL_WORKSPACE, "StridedSliceGrad", "dd1fefc")
        as_of = datetime.datetime(2026, 11, 23, 9, 37, 10, tzinfo=datetime.timezone.utc)
        verdict = gate.evaluate(
            title="StridedSliceGrad",
            sha=sha,
            as_of=as_of,
            override_board_score=59.74,
            override_board_status="Pass",
        )
        self.assertIn("anchor=best(ba4a357)", verdict.estimate.detail)
        self.assertGreater(verdict.estimate.estimated_score, 70.0)
        s2 = next(s for s in verdict.stages if s.stage == "NOISE_FLOOR")
        self.assertTrue(s2.passed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
