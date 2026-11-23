"""Dual-anchor Pairwise-v2 candidate evaluation and anchor selection."""
from __future__ import annotations

import math
from typing import Any

from .bench_evolution import (
    build_pairwise_phi_delta,
    detect_thermal_drift,
    evaluate_explore_eligibility,
    infer_bucket_metadata,
)
from .estimator import SIGMA_HIDDEN_JITTER


def evaluate_pairwise_anchor(
    *,
    cand_sha: str,
    cand_tel: dict[str, Any],
    anchor_sha: str,
    anchor_tel: dict[str, Any],
    anchor_kind: str,
    anchor_sc: float,
    r_anchor: float,
    current_score: float,
    frontier_top1: float,
    cal: dict[str, Any],
    spec_ids: list[str],
    noise_floor_pts: float = 2.5,
) -> dict[str, Any] | None:
    """Evaluate a candidate commit against one baseline anchor (`latest` or `best`)."""
    cand_rob: dict[str, float] = cand_tel.get("robust_cases") or {}
    base_rob: dict[str, float] = anchor_tel.get("robust_cases") or {}
    if not cand_rob or not base_rob or anchor_tel.get("modality") != "profiler" or anchor_sc <= 0:
        return None

    common_all = sorted(set(cand_rob) & set(base_rob))
    if len(common_all) < 3:
        return None

    cand_regime = str(cand_tel.get("regime") or "none")
    base_regime = str(anchor_tel.get("regime") or "none")
    regime_mismatch = bool(
        cand_regime != "none" and base_regime != "none" and cand_regime != base_regime
    )

    cal_ids = list(cal.get("bucket_order") or [])
    cal_strata_list = list(cal.get("bucket_strata_order") or [])
    cal_w = list(cal.get("learned_weights_order") or [])
    cal_var = list(cal.get("posterior_diag_var") or [])
    b_corr = dict(cal.get("bucket_correlations") or {})
    b_npairs = dict(cal.get("bucket_pair_counts") or {})
    # Historical + baseline minimums only: never fold cand_rob into min_tau
    min_tau = dict(cal.get("observed_min_tau") or {})
    transfer_gamma = float(cal.get("transfer_gamma") or 0.75)
    alpha_cold = float(cal.get("alpha_cold") or 0.0)
    cal_trust = float(cal.get("calibration_trust") or 0.35)
    loocv_mae = cal.get("loocv_mae_pts")
    last_res = cal.get("last_residual_pts")

    w_map = {bid: float(cal_w[i]) for i, bid in enumerate(cal_ids) if i < len(cal_w)}
    var_map = {bid: float(cal_var[i]) for i, bid in enumerate(cal_ids) if i < len(cal_var)}
    strata_map = {bid: str(cal_strata_list[i]) for i, bid in enumerate(cal_ids) if i < len(cal_strata_list)}

    all_bucket_ids = list(dict.fromkeys(cal_ids + common_all))
    for bid in all_bucket_ids:
        if bid in base_rob and base_rob[bid] > 0:
            if bid not in min_tau or base_rob[bid] < min_tau[bid]:
                min_tau[bid] = base_rob[bid]
        if bid not in strata_map:
            strata_map[bid] = infer_bucket_metadata(bid, min_tau.get(bid, base_rob.get(bid, 8.0)))["stratum"]
        if bid not in w_map:
            w_map[bid] = 1.0 / max(1, len(all_bucket_ids))
        if bid not in var_map:
            var_map[bid] = 0.003

    thermal_drift_us = 0.0
    cand_eff = cand_rob
    if not regime_mismatch:
        thermal_drift_us, cand_eff = detect_thermal_drift(base_rob, cand_rob)

    dphi = build_pairwise_phi_delta(all_bucket_ids, base_rob, cand_eff, min_tau, r_anchor)
    common_mask = [(bid in base_rob and bid in cand_eff) for bid in all_bucket_ids]
    obs_w = sum(w_map[bid] for bid, fl in zip(all_bucket_ids, common_mask) if fl) or 1.0

    raw_hid_weighted = sum(
        (w_map[bid] / obs_w) * dp for bid, dp, fl in zip(all_bucket_ids, dphi, common_mask) if fl
    )
    raw_delta = transfer_gamma * raw_hid_weighted

    b_bytes = (anchor_tel.get("kernel_binary_stats") or {}).get("max_bytes")
    c_bytes = (cand_tel.get("kernel_binary_stats") or {}).get("max_bytes")
    binary_delta_pct: float | None = None
    if isinstance(b_bytes, (int, float)) and isinstance(c_bytes, (int, float)) and b_bytes > 0 and c_bytes > 0:
        binary_delta_pct = round(100.0 * (c_bytes - b_bytes) / b_bytes, 2)
        dlb = math.log(c_bytes / b_bytes)
        if alpha_cold < -1e-4:
            raw_delta += max(-5.0, min(3.0, alpha_cold * dlb))
        elif c_bytes > 131072 and binary_delta_pct >= 25.0:
            raw_delta -= min(3.0, 2.5 * dlb)

    if regime_mismatch:
        big_num = 0.0
        big_den = 0.0
        for bid, dp, fl in zip(all_bucket_ids, dphi, common_mask):
            if fl and base_rob[bid] >= 15.0:
                big_num += w_map[bid] * dp
                big_den += w_map[bid]
        if big_den >= 0.25 * obs_w:
            raw_delta = 0.55 * transfer_gamma * (big_num / big_den)
        else:
            raw_delta = 0.30 * raw_delta
        if raw_delta > 6.0:
            raw_delta = 6.0 + 0.30 * (raw_delta - 6.0)

    matched_spec = sum(1 for sid in spec_ids if sid in base_rob and sid in cand_eff)
    unpaired_spec_frac = max(0.0, 1.0 - matched_spec / max(1, len(spec_ids))) if spec_ids else 0.0
    if unpaired_spec_frac >= 0.35 and len(common_all) < 12 and raw_delta > 0:
        raw_delta *= max(0.65, 1.0 - 0.35 * unpaired_spec_frac)

    n_pairs = int(cal.get("pairwise_count") or 0)
    if not regime_mismatch and n_pairs < 4 and abs(raw_delta) > 7.5:
        sgn = 1.0 if raw_delta > 0 else -1.0
        raw_delta = sgn * (7.5 + 0.50 * (abs(raw_delta) - 7.5))

    hard_cap = min(98.0, max(frontier_top1 + 3.0, anchor_sc + 25.0))
    est_score = max(0.0, min(hard_cap, anchor_sc + max(-25.0, raw_delta)))
    ref_board_sc = current_score if current_score > 0 else anchor_sc
    delta_vs_board = round(est_score - ref_board_sc, 2)

    improved = 0
    regressed: list[str] = []
    epistemic_var = 0.0
    for bid, dp, fl in zip(all_bucket_ids, dphi, common_mask):
        if not fl:
            continue
        epistemic_var += ((transfer_gamma * dp / obs_w) ** 2) * var_map[bid]
        ratio = base_rob[bid] / max(1e-6, cand_eff[bid])
        if ratio > 1.03:
            improved += 1
        elif ratio < 0.95:
            regressed.append(f"{bid}({ratio:.2f}x)")

    loocv_extra = max(0.0, (float(loocv_mae) if isinstance(loocv_mae, (int, float)) else 4.8) - SIGMA_HIDDEN_JITTER) * 1.15
    unpaired_sigma = 2.4 * unpaired_spec_frac if len(common_all) < max(12, len(spec_ids)) else 0.0
    regime_sigma = 4.5 if regime_mismatch else 0.0
    total_sigma = round(
        math.sqrt(SIGMA_HIDDEN_JITTER ** 2 + epistemic_var + loocv_extra ** 2 + unpaired_sigma ** 2 + regime_sigma ** 2),
        2,
    )

    explore_ok, ucb_delta, explore_reason, explore_evidence = evaluate_explore_eligibility(
        delta_pred=delta_vs_board,
        sigma_pred=total_sigma,
        calibration_trust=cal_trust,
        noise_floor_pts=noise_floor_pts,
        bucket_ids=all_bucket_ids,
        strata=strata_map,
        base_timings=base_rob,
        cand_timings=cand_eff,
        bucket_correlations=b_corr,
        bucket_pair_counts=b_npairs,
        binary_delta_pct=binary_delta_pct,
        last_residual_pts=float(last_res) if isinstance(last_res, (int, float)) else None,
    )

    eff_speedup = round(est_score / ref_board_sc, 4) if ref_board_sc > 0 else 1.0
    notes: list[str] = []
    if thermal_drift_us > 0:
        notes.append(f"drift=-{thermal_drift_us:.2f}us")
    if binary_delta_pct is not None:
        notes.append(f".o={binary_delta_pct:+.1f}%")
    if regime_mismatch:
        notes.append(f"REGIME_MISMATCH({base_regime}->{cand_regime})")
    extra_note = (" · " + ", ".join(notes)) if notes else ""

    return {
        "estimated_score": round(est_score, 2),
        "sigma": total_sigma,
        "ucb_delta": ucb_delta,
        "calibration_trust": round(cal_trust, 3),
        "explore_eligible": explore_ok,
        "explore_reason": explore_reason,
        "explore_evidence": explore_evidence,
        "thermal_drift_us": round(thermal_drift_us, 3),
        "binary_delta_pct": binary_delta_pct,
        "effective_speedup": eff_speedup,
        "matched_cases": len(common_all),
        "matched_spec_cases": matched_spec,
        "improved_cases": improved,
        "regressed_cases": regressed,
        "regime_mismatch": regime_mismatch,
        "base_regime": base_regime,
        "cand_regime": cand_regime,
        "anchor_kind": anchor_kind,
        "anchor_sha": anchor_sha,
        "anchor_score": round(anchor_sc, 2),
        "method": "pairwise_simplex_v2",
        "detail": (
            f"Pairwise-v2 ({len(common_all)} buckets [{matched_spec}/{len(spec_ids)} spec] "
            f"anchor={anchor_kind}({anchor_sha[:7]}), gamma={transfer_gamma:.2f}, trust={cal_trust:.2f}, "
            f"UCB={ucb_delta:+.2f}, improved={improved}, regressed={len(regressed)}){extra_note}"
        ),
    }


def select_dual_anchor_prediction(
    latest_pred: dict[str, Any] | None,
    best_pred: dict[str, Any] | None,
    current_score: float,
    best_score: float,
) -> dict[str, Any] | None:
    """Select the cleaner/higher-validity anchor between latest online baseline and historical best."""
    if best_pred is None:
        return latest_pred
    if latest_pred is None:
        return best_pred

    best_clean = bool(best_pred.get("matched_cases", 0) >= 8 and not best_pred.get("regime_mismatch"))
    is_restore_regime = bool(current_score > 0 and best_score - current_score >= 1.5)

    if is_restore_regime and best_clean:
        return best_pred
    if is_restore_regime and best_pred.get("matched_cases", 0) >= 8 and latest_pred.get("regime_mismatch"):
        if float(best_pred["estimated_score"]) >= float(latest_pred["estimated_score"]):
            return best_pred
    if latest_pred.get("regime_mismatch") and best_clean:
        return best_pred
    if latest_pred.get("matched_cases", 0) < 6 and best_clean:
        return best_pred
    if current_score <= 0 and best_clean:
        return best_pred
    return latest_pred
