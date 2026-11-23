"""Parhelion Local Bench Evolution & Pairwise Residual Calibration Engine (v2.0).

Pure mathematical & structural feedback modules backing `BenchmarkCalibrator`:
  1. Spike-Immune Denoising (`compute_robust_timings`):
     Blends `median_us` and `min_us`, clamping transient single-session bus/cache spikes
     (e.g. `ConcatD c8b5521 tiny32`, `StridedSliceGrad 8f498a9 r2_shrink`).
  2. Flat-Floor Thermal/Clock Drift Cancellation (`detect_thermal_drift`):
     Detects uniform additive session floor offsets (`+0.45..+1.65 us` tight modal cluster
     across latency scales, e.g. `LayerNormV3 b676b33`) without masking real kernel slowdowns
     (`LayerNormV3 4d36702`).
  3. Non-Saturating Anchored Pairwise Efficiency Deltas (`build_pairwise_phi_delta`):
     Computes `ΔΦ_k(A->B) = 90 * (r_k(B) - r_k(A))` anchored smoothly to `observed_min_tau`,
     eliminating `beta -> 3.0` dead-zone saturation.
  4. Correlation-Adaptive Pairwise Simplex-MAP + Transfer Gain (`fit_pairwise_simplex_map`):
     Learns weights `w_hat` over ALL spec + auto-discovered `profiler` buckets strictly within
     same-`(modality, soc, regime)` pairs, tilts priors/caps by empirical correlation `r_k`
     with online `ΔS`, learns problem transfer gain `gamma_transfer`, and optionally fits
     compiled `.o` binary-size cold-start slope `alpha_cold <= 0`.
  5. Strict Leave-One-SHA-Out Cross-Validation (`compute_loso_cv`):
     Computes honest out-of-sample `loocv_mae_pts`, `loocv_rmse_pts`, `loocv_spearman`,
     `loocv_dir_acc`, and `calibration_trust in [0.15, 1.0]`.
  6. Uncertainty-Aware `EXPLORE` Channel (`evaluate_explore_eligibility`):
     Protects minority-bucket structural breakthroughs (`>= 3` buckets `>= 1.25x` or `.o`
     slimming `>= 20%`) and high-uncertainty UCB upsides while vetoing genuine regressions
     on high-correlation or small-core buckets.
  7. Residual Root-Cause Attribution & Append-Only Bench Evolution (`classify_residual_causes`,
     `build_evolution_plan`, `append_buckets_to_spec`).
"""
from __future__ import annotations

import datetime
import json
import math
import pathlib
import re
import statistics
from typing import Any

from .estimator import SIGMA_HIDDEN_JITTER

MODEL_VERSION = "2.0"


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _huber_grad(residual: float, delta: float = SIGMA_HIDDEN_JITTER) -> float:
    if abs(residual) <= delta:
        return residual
    return delta if residual > 0.0 else -delta


def project_capped_simplex(
    weights: list[float],
    upper_bounds: list[float],
    min_floor: float = 0.002,
) -> list[float]:
    """Exact water-filling projection onto `{w >= min_floor : sum(w) = 1, w_k <= u_k}`."""
    k_len = len(weights)
    if k_len == 0:
        return []
    lb = min(min_floor, 0.5 / k_len)
    w = [max(lb, float(x)) for x in weights]
    caps = [max(lb + 1e-6, float(u)) for u in upper_bounds]
    if sum(caps) <= 1.0 + 1e-9:
        tot_c = sum(caps)
        return [c / tot_c for c in caps]

    rem_total = max(0.0, 1.0 - k_len * lb)
    z_in = [max(1e-9, w[i] - lb) for i in range(k_len)]
    z_caps = [max(1e-9, caps[i] - lb) for i in range(k_len)]

    fixed = [False] * k_len
    z_out = [0.0] * k_len
    rem_mass = rem_total

    for _ in range(k_len + 1):
        free_sum = sum(z_in[i] for i in range(k_len) if not fixed[i])
        if free_sum <= 0.0:
            n_free = sum(1 for i in range(k_len) if not fixed[i])
            for i in range(k_len):
                if not fixed[i]:
                    z_out[i] = min(z_caps[i], rem_mass / max(1, n_free))
            break

        clipped_any = False
        scale = rem_mass / free_sum
        for i in range(k_len):
            if fixed[i]:
                continue
            cand = z_in[i] * scale
            if cand > z_caps[i] + 1e-12:
                z_out[i] = z_caps[i]
                fixed[i] = True
                rem_mass = max(0.0, rem_mass - z_caps[i])
                clipped_any = True
            else:
                z_out[i] = cand
        if not clipped_any:
            break

    out = [lb + z_out[i] for i in range(k_len)]
    s_final = sum(out)
    if s_final > 0 and abs(s_final - 1.0) > 1e-7:
        diff = 1.0 - s_final
        for i in range(k_len):
            if lb <= out[i] + diff <= caps[i]:
                out[i] += diff
                break
    return out


def compute_pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    dx = math.sqrt(sum((a - mx) ** 2 for a in xs))
    dy = math.sqrt(sum((b - my) ** 2 for b in ys))
    if dx <= 1e-12 or dy <= 1e-12:
        return 0.0
    return num / (dx * dy)


def compute_spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(xs) != len(ys):
        return None

    def _ranks(vals: list[float]) -> list[float]:
        indexed = sorted(enumerate(vals), key=lambda t: t[1])
        ranks = [0.0] * len(vals)
        i = 0
        while i < len(indexed):
            j = i
            while j + 1 < len(indexed) and indexed[j + 1][1] == indexed[i][1]:
                j += 1
            avg_r = 0.5 * (i + j) + 1.0
            for k in range(i, j + 1):
                ranks[indexed[k][0]] = avg_r
            i = j + 1
        return ranks

    return compute_pearson(_ranks(xs), _ranks(ys))


def compute_robust_timings(
    med_cases: dict[str, float],
    min_cases: dict[str, float] | None = None,
) -> dict[str, float]:
    """Spike-immune per-bucket latency blending `median_us` and `min_us`.

    When single-session `profiler` suffers a transient bus/cache stall (`median > 1.18 * min`),
    weight `min_us` strongly (75%) and clamp the median spike at `1.25 * min`.
    """
    mn = min_cases or {}
    out: dict[str, float] = {}
    for k, m_val in med_cases.items():
        if not isinstance(m_val, (int, float)) or m_val <= 0:
            continue
        mf = float(m_val)
        min_v = mn.get(k)
        if isinstance(min_v, (int, float)) and 0 < float(min_v) <= mf:
            mv = float(min_v)
            if mf > mv * 1.18:
                out[k] = round(0.25 * min(mf, mv * 1.25) + 0.75 * mv, 4)
            else:
                out[k] = round(0.55 * mf + 0.45 * mv, 4)
        else:
            out[k] = round(mf, 4)
    return out


def detect_thermal_drift(
    base_timings: dict[str, float],
    cand_timings: dict[str, float],
) -> tuple[float, dict[str, float]]:
    """Detect and cancel uniform additive or proportional multiplicative NPU thermal/clock drift.

    1. Flat-floor additive signature (verified on `LayerNormV3 b676b33` vs true regression `4d36702`):
       - `>= 35%` of common buckets (at least 5) cluster within `+-0.32 us` around a modal
         additive offset `delta in [0.45, 1.65] us`, flat across `<= 10 us` vs `> 10 us`
         (`<= 0.45 us`), with `< 3` small/mid buckets (`tb <= 15 us`) slowed by `> 1.85 us`.
    2. Multiplicative proportional clock-scaling signature (fallback when additive does not trigger):
       - `>= 8` common buckets, `strong_fast >= 2`, `< 3` severe regressions (`tc / tb > 1.25`),
       - `>= 35%` (at least 5) buckets cluster tightly within `+-0.035` of modal `med_ratio`
         in `tc / tb in [1.04, 1.15]` (preferring mid/large `tb >= 6.0 us` when available),
       - normalizes `cand_timings` by `mult = 1.0 + 0.85 * (med_ratio - 1.0)` (`v / mult`).

    Returns `(drift_us, drift_adjusted_cand_timings)`.
    """
    common = sorted(set(base_timings) & set(cand_timings))
    if len(common) < 8:
        return 0.0, dict(cand_timings)

    entries: list[tuple[str, float, float, float, float]] = []
    strong_fast = 0
    severe_small_reg = 0
    severe_mult_reg = 0
    for k in common:
        tb, tc = base_timings[k], cand_timings[k]
        if tb <= 0 or tc <= 0:
            continue
        speedup = tb / tc
        slowdown = tc / tb
        diff = tc - tb
        entries.append((k, tb, tc, diff, slowdown))
        if speedup >= 1.25:
            strong_fast += 1
        if tb <= 15.0 and diff > 1.85:
            severe_small_reg += 1
        if slowdown > 1.25:
            severe_mult_reg += 1

    if len(entries) < 8 or strong_fast < 2:
        return 0.0, dict(cand_timings)

    min_cluster = max(5, int(math.ceil(0.35 * len(entries))))

    # 1. Flat-floor additive drift cancellation
    if severe_small_reg < 3:
        pos_cands = [diff for _k, _tb, _tc, diff, _sl in entries if 0.45 <= diff <= 1.65]
        if len(pos_cands) >= 4:
            best_cluster: list[tuple[float, float]] = []
            for center in pos_cands:
                cl = [(diff, tb) for _k, tb, _tc, diff, _sl in entries if abs(diff - center) <= 0.32]
                if len(cl) > len(best_cluster):
                    best_cluster = cl
            if len(best_cluster) >= min_cluster:
                small_cl = [d for d, tb in best_cluster if tb <= 10.0]
                large_cl = [d for d, tb in best_cluster if tb > 10.0]
                if not (small_cl and large_cl and abs(statistics.median(large_cl) - statistics.median(small_cl)) > 0.45):
                    modal_delta = statistics.median(d for d, _ in best_cluster)
                    drift = round(0.85 * modal_delta, 4)
                    adjusted = {
                        k: (round(max(1.0, max(0.5 * base_timings[k], v - drift)), 4) if k in base_timings else v)
                        for k, v in cand_timings.items()
                    }
                    return drift, adjusted

    # 2. Multiplicative proportional drift cancellation
    if severe_mult_reg >= 3:
        return 0.0, dict(cand_timings)

    band = [(k, tb, tc, sl) for k, tb, tc, _diff, sl in entries if 1.04 <= sl <= 1.15]
    if len(band) < min_cluster:
        return 0.0, dict(cand_timings)

    mid_all_count = sum(1 for _k, tb, _tc, _diff, _sl in entries if tb >= 6.0)
    mid_band = [item for item in band if item[1] >= 6.0]
    prefer_mid = mid_all_count >= 5
    if prefer_mid and len(mid_band) < 3:
        return 0.0, dict(cand_timings)

    centers = [sl for _k, _tb, _tc, sl in (mid_band if prefer_mid else band)]
    best_mult_cl: list[tuple[str, float, float, float]] = []
    for center in centers:
        cl = [item for item in band if abs(item[3] - center) <= 0.035]
        if len(cl) > len(best_mult_cl):
            best_mult_cl = cl

    if len(best_mult_cl) < min_cluster:
        return 0.0, dict(cand_timings)

    mid_in_cl = [sl for _k, tb, _tc, sl in best_mult_cl if tb >= 6.0]
    if prefer_mid and len(mid_in_cl) < 3:
        return 0.0, dict(cand_timings)

    med_ratio = statistics.median(
        mid_in_cl if (prefer_mid and mid_in_cl) else [sl for _k, _tb, _tc, sl in best_mult_cl]
    )
    tight = [item for item in band if abs(item[3] - med_ratio) <= 0.035]
    if len(tight) < min_cluster:
        return 0.0, dict(cand_timings)
    if prefer_mid and sum(1 for _k, tb, _tc, _sl in tight if tb >= 6.0) < 3:
        return 0.0, dict(cand_timings)

    mult = 1.0 + 0.85 * (med_ratio - 1.0)
    adjusted = {
        k: (
            round(min(v, max(1.0, v / mult)) if v > 1.0 else v / mult, 4)
            if k in base_timings
            else v
        )
        for k, v in cand_timings.items()
    }
    equiv_drifts = [tc - adjusted[k] for k, _tb, tc, _sl in tight if k in adjusted]
    equiv_drift_us = round(statistics.median(equiv_drifts), 4) if equiv_drifts else 0.0
    return equiv_drift_us, adjusted


def infer_bucket_metadata(case_id: str, observed_us: float = 8.0) -> dict[str, Any]:
    """Infer stratum, dtype, roofline_us, and prior_w for an auto-discovered logged case."""
    low = case_id.lower()
    if "bf16" in low or "bfloat16" in low:
        dtype = "bfloat16"
    elif "fp16" in low or "f16" in low or "half" in low:
        dtype = "float16"
    elif "i64" in low or "int64" in low:
        dtype = "int64"
    elif "i32" in low or "int32" in low:
        dtype = "int32"
    elif "i8" in low or "int8" in low or "u8" in low:
        dtype = "int8"
    else:
        dtype = "float32"

    if any(
        tok in low
        for tok in (
            "odd", "unalign", "step", "neg", "circular", "bigpad", "p64", "p30",
            "prime", "axis0", "extreme", "rank6", "sparse", "dup",
        )
    ):
        stratum = "X_EXTREME"
        max_w = 0.05
        prior_w = 0.035
    elif (
        any(tok in low for tok in ("tiny", "small", "sample", "pub_", "4x4", "1x1x8", "2x2x2", "4096", "_c1", "_c33", "_c64", "_c128"))
        or observed_us <= 4.0
    ):
        stratum = "S_SMALL_CORE"
        max_w = 0.22
        prior_w = 0.050
    elif (
        any(tok in low for tok in ("lg_", "huge", "big", "_1m", "262144", "65536", "16384", "4096x", "2048x", "256x256", "720x1280"))
        or observed_us >= 18.0
    ):
        stratum = "L_HBM_TILED"
        max_w = 0.22
        prior_w = 0.050
    else:
        stratum = "M_MID_UB"
        max_w = 0.22
        prior_w = 0.045

    roof_us = round(max(2.2, observed_us * 0.72), 3)
    return {
        "id": case_id,
        "stratum": stratum,
        "dtype": dtype,
        "io_bytes": 0,
        "roofline_us": roof_us,
        "torch_ref_us": round(max(roof_us * 1.4, observed_us * 1.25), 3),
        "prior_w": prior_w,
        "max_w": max_w,
        "description": f"Auto-discovered profiler bucket ({dtype}, {stratum})",
        "source": "auto_discovered",
    }


def apply_proxy_aliases(
    timings: dict[str, float],
    spec_bucket_ids: list[str],
) -> dict[str, float]:
    """Bridge `*_proxy` spec bucket IDs to exact un-suffixed logged cases when absent."""
    out = dict(timings)
    for bid in spec_bucket_ids:
        if bid.endswith("_proxy") and bid not in out:
            stripped = bid[:-6]
            if stripped in out:
                out[bid] = out[stripped]
    return out


def build_pairwise_phi_delta(
    bucket_ids: list[str],
    t_a: dict[str, float],
    t_b: dict[str, float],
    min_tau: dict[str, float],
    r_anchor_a: float,
) -> list[float]:
    """Compute non-saturating pairwise hidden delta vector `ΔΦ_k(A->B) = 90 * (r_k(B) - r_k(A))`.

    Anchors each bucket's baseline efficiency smoothly around `r_anchor_a` scaled by
    `(min_tau / tau_a)^0.75` (never folding `tb` into `tmin`, preventing self-swallowing on
    record-setting candidates) and uses smooth `tanh` soft-saturation above `0.96`.
    """
    r_base = max(0.20, min(0.88, r_anchor_a))
    dphi: list[float] = []
    for k in bucket_ids:
        ta = t_a.get(k)
        tb = t_b.get(k)
        if ta is None or tb is None or ta <= 0 or tb <= 0:
            dphi.append(0.0)
            continue
        tmin = min(min_tau.get(k, ta), ta)
        r_a = max(0.12, min(0.96, r_base * ((tmin / ta) ** 0.75)))
        ratio = max(0.25, min(3.5, ta / tb))
        raw_rb = r_a * ratio
        if raw_rb > 0.96:
            r_b = 0.96 + 0.04 * math.tanh((raw_rb - 0.96) / 0.25)
        else:
            r_b = max(0.05, raw_rb)
        dphi.append(90.0 * (r_b - r_a))
    return dphi


def fit_pairwise_simplex_map(
    bucket_ids: list[str],
    strata: dict[str, str],
    spec_priors: dict[str, float],
    pairs: list[dict[str, Any]],
    min_tau: dict[str, float],
    steps: int = 120,
    lambda_kl: float = 12.0,
    lambda_l2: float = 18.0,
) -> dict[str, Any]:
    """Jointly fit correlation-adaptive weights `w_hat`, transfer gain `gamma_transfer`,
    diagonal Laplace posterior variance `diag_var`, per-bucket correlations, and optional
    cold-start binary-size penalty `alpha_cold <= 0`.

    Each entry in `pairs` is a dict:
      `{"t_a": dict, "t_b": dict, "dy": float, "r_a": float, "sw": float, "d_log_bytes": float | None}`.
    """
    k_len = len(bucket_ids)
    if k_len == 0:
        return {
            "w0": [],
            "w_hat": [],
            "caps": [],
            "diag_var": [],
            "transfer_gamma": 0.72,
            "alpha_cold": 0.0,
            "bucket_correlations": {},
            "bucket_pair_counts": {},
        }

    # 1. Per-bucket empirical Pearson correlation with online ΔS across same-regime pairs
    b_corr: dict[str, float] = {}
    b_npairs: dict[str, int] = {}
    for k in bucket_ids:
        xs: list[float] = []
        ys: list[float] = []
        for p in pairs:
            ta_m, tb_m = p["t_a"], p["t_b"]
            dy = float(p["dy"])
            if k in ta_m and k in tb_m and ta_m[k] > 0 and tb_m[k] > 0 and abs(dy) >= 0.5:
                xs.append(math.log(ta_m[k] / tb_m[k]))
                ys.append(dy)
        b_npairs[k] = len(xs)
        if len(xs) >= 3:
            c = compute_pearson(xs, ys)
            if c is not None:
                b_corr[k] = round(c, 4)

    # 2. Correlation-informed prior tilt & adaptive caps
    raw_w0: list[float] = []
    caps: list[float] = []
    for k in bucket_ids:
        base_p = float(spec_priors.get(k, 0.045))
        r_k = b_corr.get(k, 0.0)
        n_k = b_npairs.get(k, 0)
        conf = min(1.0, max(0.0, (n_k - 2) / 8.0))
        tilt = math.exp(1.25 * conf * r_k) if n_k >= 3 else 1.0
        raw_w0.append(max(0.002, base_p * tilt))

        if strata.get(k) == "X_EXTREME":
            cap = 0.05 if (n_k < 4 or r_k < 0.25) else min(0.18, 0.05 + 0.16 * max(0.0, r_k))
        else:
            cap = 0.22
        if n_k >= 4 and r_k <= -0.25:
            cap = min(cap, max(0.006, 0.035 * (1.0 + r_k)))
        caps.append(round(cap, 5))

    s_w0 = sum(raw_w0) or 1.0
    w0 = project_capped_simplex([x / s_w0 for x in raw_w0], caps, min_floor=0.002)

    if len(pairs) < 2:
        return {
            "w0": [round(x, 6) for x in w0],
            "w_hat": [round(x, 6) for x in w0],
            "caps": caps,
            "diag_var": [0.003] * k_len,
            "transfer_gamma": 0.72,
            "alpha_cold": 0.0,
            "bucket_correlations": b_corr,
            "bucket_pair_counts": b_npairs,
        }

    # 3. Pairwise ΔΦ matrix & common-bucket weight masks
    dphi_rows: list[list[float]] = []
    dy_rows: list[float] = []
    sw_rows: list[float] = []
    masks: list[list[bool]] = []
    dlogb_rows: list[float | None] = []
    for p in pairs:
        t_a, t_b = p["t_a"], p["t_b"]
        dphi_rows.append(build_pairwise_phi_delta(bucket_ids, t_a, t_b, min_tau, float(p["r_a"])))
        dy_rows.append(float(p["dy"]))
        sw_rows.append(float(p["sw"]))
        masks.append([(k in t_a and k in t_b) for k in bucket_ids])
        dlogb_rows.append(p.get("d_log_bytes"))

    w = list(w0)
    gamma_t = 0.75
    alpha_cold = 0.0

    for _outer in range(2):
        for step in range(steps):
            eta = 0.014 / math.sqrt(1.0 + 0.08 * step)
            grad = [0.0] * k_len
            for dp, dy, sw, m, dlb in zip(dphi_rows, dy_rows, sw_rows, masks, dlogb_rows):
                ow = sum(wk for wk, fl in zip(w, m) if fl) or 1.0
                scale = gamma_t / ow
                cold_term = (alpha_cold * dlb) if dlb is not None else 0.0
                pred = scale * sum(wk * pk for wk, pk, fl in zip(w, dp, m) if fl) + cold_term
                g = sw * _huber_grad(pred - dy, delta=SIGMA_HIDDEN_JITTER) * scale
                for idx in range(k_len):
                    if m[idx]:
                        grad[idx] += g * dp[idx]
            for idx in range(k_len):
                wk_s = max(1e-6, w[idx])
                w0_s = max(1e-6, w0[idx])
                grad[idx] += lambda_kl * (math.log(wk_s / w0_s) + 1.0) + lambda_l2 * (w[idx] - w0[idx])
            gm = sum(grad) / k_len
            unnorm = [w[i] * math.exp(max(-5.0, min(5.0, -eta * (grad[i] - gm)))) for i in range(k_len)]
            w_next = project_capped_simplex(unnorm, caps, min_floor=0.002)
            if max(abs(a - b) for a, b in zip(w_next, w)) < 1e-6:
                w = w_next
                break
            w = w_next

        # Update transfer gain `gamma_t` with Huber IRLS + Bayesian ridge prior toward 0.75
        raw_preds = []
        for dp, m in zip(dphi_rows, masks):
            ow = sum(wk for wk, fl in zip(w, m) if fl) or 1.0
            raw_preds.append(sum(wk * pk for wk, pk, fl in zip(w, dp, m) if fl) / ow)

        prior_prec = 35.0
        num = prior_prec * 0.75
        den = prior_prec
        for rp, dy, sw, dlb in zip(raw_preds, dy_rows, sw_rows, dlogb_rows):
            cold_term = (alpha_cold * dlb) if dlb is not None else 0.0
            adj_dy = dy - cold_term
            res = abs(gamma_t * rp - adj_dy)
            hw = 1.0 if res <= 3.5 else (3.5 / res)
            num += sw * hw * rp * adj_dy
            den += sw * hw * rp * rp
        gamma_t = max(0.30, min(1.10, num / max(1e-6, den)))

        # Fit non-positive cold-start binary-size penalty `alpha_cold <= 0` when >= 4 paired `.o` sizes exist
        cold_pts = [
            (dlb, dy - gamma_t * rp, sw)
            for rp, dy, sw, dlb in zip(raw_preds, dy_rows, sw_rows, dlogb_rows)
            if dlb is not None and abs(dlb) >= 0.03
        ]
        if len(cold_pts) >= 4:
            c_num = 0.0
            c_den = 2.0  # ridge prior toward 0
            for dlb, resid, sw in cold_pts:
                c_num += sw * dlb * resid
                c_den += sw * dlb * dlb
            # Cold-start bloat can only hurt (or be neutral): clamp to [-8.0, 0.0] pts per log(size)
            alpha_cold = round(max(-8.0, min(0.0, c_num / max(1e-6, c_den))), 4)

    # Diagonal Laplace variance
    diag_var: list[float] = []
    for idx in range(k_len):
        h_kk = lambda_kl / max(1e-4, w[idx]) + lambda_l2
        for dp, dy, sw, m, dlb in zip(dphi_rows, dy_rows, sw_rows, masks, dlogb_rows):
            if not m[idx]:
                continue
            ow = sum(wk for wk, fl in zip(w, m) if fl) or 1.0
            cold_term = (alpha_cold * dlb) if dlb is not None else 0.0
            pred = (gamma_t / ow) * sum(wk * pk for wk, pk, fl in zip(w, dp, m) if fl) + cold_term
            if abs(pred - dy) <= 4.0:
                h_kk += sw * (((gamma_t / ow) * dp[idx]) ** 2)
        diag_var.append(round(1.0 / max(1e-3, h_kk), 6))

    return {
        "w0": [round(x, 6) for x in w0],
        "w_hat": [round(x, 6) for x in w],
        "caps": caps,
        "diag_var": diag_var,
        "transfer_gamma": round(gamma_t, 4),
        "alpha_cold": alpha_cold,
        "bucket_correlations": b_corr,
        "bucket_pair_counts": b_npairs,
    }


def compute_loso_cv(
    bucket_ids: list[str],
    strata: dict[str, str],
    spec_priors: dict[str, float],
    sha_order: list[str],
    sha_scores: dict[str, float],
    sha_timings: dict[str, dict[str, float]],
    sha_regimes: dict[str, str],
    sha_socs: dict[str, str | None],
    sha_bin_bytes: dict[str, int | None],
    min_tau: dict[str, float],
) -> dict[str, Any]:
    """Strict Leave-One-SHA-Out cross-validation across same-regime adjacent transitions."""
    transitions: list[tuple[str, str, float]] = []
    for idx in range(1, len(sha_order)):
        sa, sb = sha_order[idx - 1], sha_order[idx]
        if sha_regimes.get(sa) != sha_regimes.get(sb) or sha_regimes.get(sa) == "none":
            continue
        if sha_socs.get(sa) and sha_socs.get(sb) and sha_socs[sa] != sha_socs[sb]:
            continue
        if len(set(sha_timings.get(sa, {})) & set(sha_timings.get(sb, {}))) < 3:
            continue
        transitions.append((sa, sb, sha_scores[sb] - sha_scores[sa]))

    if len(transitions) < 2:
        return {
            "loocv_mae_pts": None,
            "loocv_rmse_pts": None,
            "loocv_spearman": None,
            "loocv_dir_acc": None,
            "calibration_trust": 0.35,
            "n_eval": len(transitions),
            "cv_transitions": [],
        }

    y_acts: list[float] = []
    y_preds: list[float] = []
    cv_rows: list[dict[str, Any]] = []

    for hold_sa, hold_sb, dy_act in transitions:
        train_pairs: list[dict[str, Any]] = []
        for i in range(len(sha_order)):
            for j in range(i + 1, len(sha_order)):
                sa, sb = sha_order[i], sha_order[j]
                if sa == hold_sb or sb == hold_sb:
                    continue
                if sha_regimes.get(sa) != sha_regimes.get(sb) or sha_regimes.get(sa) == "none":
                    continue
                if sha_socs.get(sa) and sha_socs.get(sb) and sha_socs[sa] != sha_socs[sb]:
                    continue
                if len(set(sha_timings.get(sa, {})) & set(sha_timings.get(sb, {}))) < 3:
                    continue
                ra = max(0.20, min(0.88, (sha_scores[sa] * 0.87) / 90.0))
                sw = (1.5 if sha_regimes[sa] == sha_regimes[hold_sb] else 0.5) * (1.4 if j == i + 1 else 0.7)
                ba, bb = sha_bin_bytes.get(sa), sha_bin_bytes.get(sb)
                dlb = math.log(bb / ba) if (ba and bb and ba > 0 and bb > 0) else None
                train_pairs.append({
                    "t_a": sha_timings[sa],
                    "t_b": sha_timings[sb],
                    "dy": sha_scores[sb] - sha_scores[sa],
                    "r_a": ra,
                    "sw": sw,
                    "d_log_bytes": dlb,
                })

        fit = fit_pairwise_simplex_map(
            bucket_ids, strata, spec_priors, train_pairs, min_tau, steps=55
        )
        w_cv = fit["w_hat"]
        g_cv = fit["transfer_gamma"]
        a_cold = fit["alpha_cold"]

        ta, tb = sha_timings[hold_sa], sha_timings[hold_sb]
        drift_us, tb_adj = detect_thermal_drift(ta, tb)
        ra = max(0.20, min(0.88, (sha_scores[hold_sa] * 0.87) / 90.0))
        dphi = build_pairwise_phi_delta(bucket_ids, ta, tb_adj, min_tau, ra)
        m = [(k in ta and k in tb_adj) for k in bucket_ids]
        ow = sum(wk for wk, fl in zip(w_cv, m) if fl) or 1.0
        ba, bb = sha_bin_bytes.get(hold_sa), sha_bin_bytes.get(hold_sb)
        dlb = math.log(bb / ba) if (ba and bb and ba > 0 and bb > 0) else None
        pred = g_cv * sum((wk / ow) * dp for wk, dp, fl in zip(w_cv, dphi, m) if fl) + (
            a_cold * dlb if dlb is not None else 0.0
        )
        y_acts.append(dy_act)
        y_preds.append(pred)
        cv_rows.append({
            "base_sha": hold_sa,
            "base_short": hold_sa[:7],
            "cand_sha": hold_sb,
            "cand_short": hold_sb[:7],
            "actual_delta": round(dy_act, 2),
            "cv_pred_delta": round(pred, 2),
            "residual_eps": round(dy_act - pred, 2),
            "thermal_drift_us": round(drift_us, 3),
        })

    errs = [abs(a - p) for a, p in zip(y_acts, y_preds)]
    mae = sum(errs) / len(errs)
    rmse = math.sqrt(sum(e * e for e in errs) / len(errs))
    sp = compute_spearman(y_acts, y_preds)
    sig = [(a, p) for a, p in zip(y_acts, y_preds) if abs(a) >= 1.5]
    d_acc = sum(1 for a, p in sig if (a > 0 and p > 0) or (a < 0 and p < 0)) / len(sig) if sig else 0.5

    t_n = min(1.0, len(y_acts) / 7.0)
    t_sp = max(0.0, min(1.0, ((sp if sp is not None else 0.2) + 0.10) / 0.80))
    t_mae = max(0.0, min(1.0, 1.0 - max(0.0, mae - 2.5) / 6.0))
    trust = round(0.15 + 0.85 * t_n * (0.55 * t_sp + 0.45 * t_mae), 3)

    return {
        "loocv_mae_pts": round(mae, 2),
        "loocv_rmse_pts": round(rmse, 2),
        "loocv_spearman": round(sp, 3) if sp is not None else None,
        "loocv_dir_acc": round(d_acc, 3),
        "calibration_trust": trust,
        "n_eval": len(y_acts),
        "cv_transitions": cv_rows,
    }


def evaluate_explore_eligibility(
    delta_pred: float,
    sigma_pred: float,
    calibration_trust: float,
    noise_floor_pts: float,
    bucket_ids: list[str],
    strata: dict[str, str],
    base_timings: dict[str, float],
    cand_timings: dict[str, float],
    bucket_correlations: dict[str, float],
    bucket_pair_counts: dict[str, int],
    binary_delta_pct: float | None = None,
    last_residual_pts: float | None = None,
) -> tuple[bool, float, str, dict[str, Any]]:
    """Uncertainty-aware anti-burial check for G2 `EXPLORE` channel.

    Prevents burying high-upside structural breakthroughs (like `PadV3Grad 37fd777`, +9.09)
    while strictly vetoing clear regressions on high-correlation or small-core buckets
    (like `LayerNormV3 4d36702`, `PReluGradUpdate 558797b`, `ConfusionTransposeD 4b4303f`).
    """
    k_ucb = 0.85 + 0.55 * (1.0 - max(0.15, min(1.0, calibration_trust)))
    ucb = round(delta_pred + k_ucb * sigma_pred, 2)

    strong_improved: list[dict[str, Any]] = []
    moderate_improved: list[dict[str, Any]] = []
    top_regressed: list[dict[str, Any]] = []
    pos_corr_ratios: list[float] = []
    severe_small_regs: list[str] = []

    for k in bucket_ids:
        tb = base_timings.get(k)
        tc = cand_timings.get(k)
        if not (tb and tc and tb > 0 and tc > 0):
            continue
        ratio = tb / tc
        ck = float(bucket_correlations.get(k, 0.0))
        nk = int(bucket_pair_counts.get(k, 0))
        is_anti_corr = (nk >= 4 and ck <= -0.20)

        if ratio >= 1.25 and not is_anti_corr:
            strong_improved.append({"bucket": k, "ratio": round(ratio, 3), "corr": round(ck, 2)})
        elif ratio >= 1.12 and not is_anti_corr:
            moderate_improved.append({"bucket": k, "ratio": round(ratio, 3), "corr": round(ck, 2)})
        if ratio < 0.92:
            top_regressed.append({"bucket": k, "ratio": round(ratio, 3), "corr": round(ck, 2)})
        if nk >= 4 and ck >= 0.50:
            pos_corr_ratios.append(ratio)
        if strata.get(k) == "S_SMALL_CORE" and ratio < 0.78 and not is_anti_corr:
            severe_small_regs.append(f"{k}({ratio:.2f}x)")

    strong_improved.sort(key=lambda x: x["ratio"], reverse=True)
    top_regressed.sort(key=lambda x: x["ratio"])

    pos_corr_geo = (
        round(math.exp(sum(math.log(max(0.2, min(4.0, r))) for r in pos_corr_ratios) / len(pos_corr_ratios)), 3)
        if pos_corr_ratios
        else 1.0
    )
    binary_slim = bool(binary_delta_pct is not None and binary_delta_pct <= -20.0)
    high_res_hist = bool(last_residual_pts is not None and abs(last_residual_pts) >= 3.5)

    # Strict regression vetoes: never promote confirmed true regressions to EXPLORE
    pos_corr_veto = bool(len(pos_corr_ratios) >= 2 and pos_corr_geo < 0.88)
    small_core_veto = bool(calibration_trust >= 0.65 and len(severe_small_regs) >= 2)

    evidence = {
        "ucb_delta": ucb,
        "k_ucb": round(k_ucb, 3),
        "calibration_trust": round(calibration_trust, 3),
        "strong_improved_count": len(strong_improved),
        "strong_improved": strong_improved[:6],
        "top_regressed": top_regressed[:6],
        "pos_corr_geo": pos_corr_geo,
        "pos_corr_count": len(pos_corr_ratios),
        "binary_delta_pct": round(binary_delta_pct, 2) if binary_delta_pct is not None else None,
        "binary_slim_upside": binary_slim,
        "veto_pos_corr": pos_corr_veto,
        "veto_small_core": small_core_veto,
    }

    if delta_pred >= noise_floor_pts:
        return False, ucb, "", evidence
    if pos_corr_veto or small_core_veto:
        return False, ucb, "", evidence

    # Channel 1: Strong minority-bucket structural breakthrough (>= 3 buckets >= 1.25x)
    if len(strong_improved) >= 3 and (delta_pred >= -5.0 or calibration_trust < 0.55):
        top_s = ", ".join(f"{x['bucket']}({x['ratio']:.2f}x)" for x in strong_improved[:3])
        reg_s = ", ".join(f"{x['bucket']}({x['ratio']:.2f}x)" for x in top_regressed[:2]) or "none"
        return (
            True,
            ucb,
            f"Structural minority-bucket upside ({len(strong_improved)} buckets >=1.25x: {top_s}; "
            f"regressed: {reg_s}; trust={calibration_trust:.2f}, UCB={ucb:+.2f})",
            evidence,
        )

    # Channel 2: Major kernel binary `.o` cold-start slimming (>= 20% smaller) without core regression
    if binary_slim and delta_pred >= -3.5 and (len(strong_improved) + len(moderate_improved) >= 1 or delta_pred >= -1.0):
        return (
            True,
            ucb,
            f"Kernel binary slimming ({binary_delta_pct:+.1f}% .o size -> cold-start upside; "
            f"d_pred={delta_pred:+.2f}, UCB={ucb:+.2f})",
            evidence,
        )

    # Channel 3: High-uncertainty UCB upside (requires multi-bucket evidence to avoid single-case noise)
    multi_bucket_signal = len(strong_improved) >= 2 or (len(strong_improved) >= 1 and len(moderate_improved) >= 2)
    if (
        ucb >= noise_floor_pts + 2.2
        and delta_pred >= -0.5
        and (calibration_trust < 0.52 or high_res_hist)
        and multi_bucket_signal
    ):
        top_s = ", ".join(f"{x['bucket']}({x['ratio']:.2f}x)" for x in (strong_improved + moderate_improved)[:3])
        return (
            True,
            ucb,
            f"High-uncertainty UCB upside (UCB={ucb:+.2f} >= +{noise_floor_pts + 2.2:.1f}, "
            f"trust={calibration_trust:.2f}, improved={top_s})",
            evidence,
        )

    return False, ucb, "", evidence


def classify_residual_causes(
    title: str,
    base_tel: dict[str, Any],
    cand_tel: dict[str, Any],
    spec_bucket_ids: list[str],
    bucket_correlations: dict[str, float],
    actual_delta: float,
    pred_delta: float,
    thermal_drift_us: float,
) -> list[str]:
    """Identify structured root causes behind a high-residual (`|eps| >= 3.0`) or sign-flip event."""
    causes: list[str] = []
    eps = actual_delta - pred_delta

    if base_tel.get("regime") != cand_tel.get("regime") and base_tel.get("regime") != "none" and cand_tel.get("regime") != "none":
        causes.append("REGIME_MISMATCH")

    if abs(thermal_drift_us) >= 0.4:
        causes.append("THERMAL_DRIFT")

    spec_set = set(spec_bucket_ids)
    cand_ms = cand_tel.get("robust_cases") or {}
    base_ms = base_tel.get("robust_cases") or {}
    common = set(cand_ms) & set(base_ms)
    extra_common = common - spec_set
    unpaired_spec = [b for b in spec_bucket_ids if b not in common]
    if len(spec_bucket_ids) > 0 and len(unpaired_spec) >= max(3, int(0.30 * len(spec_bucket_ids))):
        causes.append("UNPAIRED_PROXY")

    # Check if extra untracked buckets diverged sharply from spec buckets
    if extra_common and (common & spec_set):
        spec_r = [base_ms[k] / cand_ms[k] for k in (common & spec_set) if cand_ms[k] > 0]
        extra_r = [base_ms[k] / cand_ms[k] for k in extra_common if cand_ms[k] > 0]
        if spec_r and extra_r:
            g_spec = math.exp(sum(math.log(max(0.2, min(4.0, r))) for r in spec_r) / len(spec_r))
            g_extra = math.exp(sum(math.log(max(0.2, min(4.0, r))) for r in extra_r) / len(extra_r))
            if abs(g_extra - g_spec) >= 0.12:
                causes.append("UNTRACKED_BUCKETS")

    # Check anti-correlated buckets driving wrong sign
    anti_moved = [
        k for k in common
        if bucket_correlations.get(k, 0.0) <= -0.25 and abs((base_ms[k] / max(1e-6, cand_ms[k])) - 1.0) >= 0.15
    ]
    if anti_moved:
        causes.append("ANTI_CORR_DOMINANCE")

    # Check `.o` binary bloat / cold-start
    b_bytes = (base_tel.get("kernel_binary_stats") or {}).get("max_bytes")
    c_bytes = (cand_tel.get("kernel_binary_stats") or {}).get("max_bytes")
    if b_bytes and c_bytes and b_bytes > 0:
        pct = 100.0 * (c_bytes - b_bytes) / b_bytes
        if pct >= 15.0 and eps <= -3.0:
            causes.append("COLD_START_BINARY_BLOAT")
        elif pct <= -15.0 and eps >= 3.0:
            causes.append("COLD_START_BINARY_SLIM")

    # Check 40-AIV vs 20-AIV block_dim shift
    b_blks = base_tel.get("block_dims") or {}
    c_blks = cand_tel.get("block_dims") or {}
    common_blks = set(b_blks) & set(c_blks)
    if common_blks:
        changed_20_40 = sum(1 for k in common_blks if (b_blks[k] >= 20) != (c_blks[k] >= 20) or abs(b_blks[k] - c_blks[k]) >= 12)
        if changed_20_40 >= max(3, len(common_blks) // 3):
            causes.append("SOC_CORE_MISMATCH")

    # Check missing orthogonal dimensions (e.g., SyncBN large-N, unaligned/bf16)
    if title == "SyncBatchNormGatherStats" and eps <= -3.0:
        causes.append("MISSING_LARGE_N_DIM")
    if not causes:
        causes.append("UNEXPLAINED_RESIDUAL")
    return causes


def build_evolution_plan(
    title: str,
    spec_doc: dict[str, Any],
    all_discovered: dict[str, float],
    bucket_correlations: dict[str, float],
    bucket_pair_counts: dict[str, int],
    min_pairs: int = 2,
    min_corr: float = 0.15,
) -> dict[str, Any]:
    """Select append-only candidate buckets from real `validation.log` observations to promote into `cases_perf.json`."""
    existing_buckets = list(spec_doc.get("hidden_buckets") or [])
    existing_ids = {b["id"] for b in existing_buckets if isinstance(b, dict) and b.get("id")}
    matched_existing = [bid for bid in existing_ids if bid in all_discovered]

    # Severe spec blindness (e.g., SyncBatchNormGatherStats 0/13 matched!): promote ALL active measured buckets
    severe_blindness = len(existing_ids) > 0 and len(matched_existing) < min(3, len(existing_ids))

    promote: list[dict[str, Any]] = []
    skipped_anti_corr: list[dict[str, Any]] = []

    for cid, med_us in sorted(all_discovered.items()):
        if cid in existing_ids or ":" in cid or cid.startswith("__"):
            continue
        # Don't duplicate a `*_proxy` spec bucket that already aliases to `cid`
        if f"{cid}_proxy" in existing_ids:
            continue
        ck = bucket_correlations.get(cid)
        nk = bucket_pair_counts.get(cid, 0)
        if ck is not None and nk >= 3 and ck <= -0.15 and not severe_blindness:
            skipped_anti_corr.append({"id": cid, "corr": round(ck, 3), "pairs": nk, "observed_us": round(med_us, 3)})
            continue

        qualifies = (
            severe_blindness
            or (ck is not None and nk >= min_pairs and ck >= min_corr)
            or (ck is None and len(matched_existing) < 10)
        )
        if qualifies:
            meta = infer_bucket_metadata(cid, med_us)
            meta["source"] = "auto_evolved_v2"
            if ck is not None:
                meta["empirical_corr"] = round(ck, 3)
                meta["empirical_pairs"] = nk
                meta["description"] = f"Promoted by bench evolve (r={ck:+.2f}, pairs={nk}, {meta['dtype']}, {meta['stratum']})"
            else:
                meta["description"] = f"Promoted by bench evolve (active logged bucket, {meta['dtype']}, {meta['stratum']})"
            promote.append(meta)

    promote.sort(key=lambda x: (x.get("empirical_corr", 0.0), x.get("empirical_pairs", 0)), reverse=True)
    return {
        "title": title,
        "existing_bucket_count": len(existing_buckets),
        "matched_existing_count": len(matched_existing),
        "severe_spec_blindness": severe_blindness,
        "promote_buckets": promote,
        "skipped_anti_corr": skipped_anti_corr,
    }


def append_buckets_to_spec(
    spec_path: pathlib.Path,
    new_buckets: list[dict[str, Any]],
) -> dict[str, Any]:
    """Strictly append-only update to `cases_perf.json` (Iron Rule #2: NEVER deletes old buckets)."""
    if not spec_path.is_file():
        raise FileNotFoundError(f"Spec file not found: {spec_path}")
    doc = json.loads(spec_path.read_text(encoding="utf-8"))
    existing = list(doc.get("hidden_buckets") or [])
    existing_ids = {b["id"] for b in existing if isinstance(b, dict) and b.get("id")}

    appended: list[str] = []
    for nb in new_buckets:
        bid = str(nb.get("id") or "")
        if not bid or bid in existing_ids or ":" in bid or bid.startswith("__"):
            continue
        entry = {
            "id": bid,
            "stratum": nb.get("stratum", "M_MID_UB"),
            "dtype": nb.get("dtype", "float32"),
            "io_bytes": int(nb.get("io_bytes", 0)),
            "roofline_us": float(nb.get("roofline_us", 4.0)),
            "torch_ref_us": float(nb.get("torch_ref_us", 10.0)),
            "prior_w": float(nb.get("prior_w", 0.03)),
            "max_w": float(nb.get("max_w", 0.20)),
            "description": str(nb.get("description", "Auto-evolved bucket")),
            "source": str(nb.get("source", "auto_evolved_v2")),
            "added_at": _utc_now(),
        }
        existing.append(entry)
        existing_ids.add(bid)
        appended.append(bid)

    if appended:
        # Normalize prior_w smoothly to sum == 1.0 while keeping every old bucket intact
        total_w = sum(float(b.get("prior_w") or 0.01) for b in existing) or 1.0
        for b in existing:
            b["prior_w"] = round(float(b.get("prior_w") or 0.01) / total_w, 6)
        doc["hidden_buckets"] = existing
        doc["evolved_at"] = _utc_now()
        doc["version"] = "2.0"
        tmp = spec_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(spec_path)

    return {
        "spec_path": str(spec_path),
        "appended_count": len(appended),
        "appended_ids": appended,
        "total_buckets_after": len(existing),
    }
