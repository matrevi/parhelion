"""Append-only stratified benchmark & adversarial correctness scaffolding (`scaffold_bench_specs`)."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from ..config import read_json, utc_now
from .common import _DTYPE_ALIASES, VALID_OPERATOR_FAMILIES, _atomic_write_json, _bench_specs_dir, _resolve_problem_info

_HIDDEN_WEIGHTS = {"S_SMALL_CORE": 0.30, "M_MID_UB": 0.35, "L_HBM_TILED": 0.20, "X_EXTREME": 0.15}

# Tuple format: (bucket_id, shape, dtype_slot_idx, description)
_FAMILY_SHAPE_TEMPLATES: dict[str, dict[str, list[tuple[str, list[int], int, str]]]] = {
    "elementwise": {
        "PUBLIC": [("pub_elem_small", [16, 256], 0, "Public elementwise small tensor"), ("pub_elem_mid", [256, 1024], 1, "Public elementwise mid UB tile")],
        "S_SMALL_CORE": [("s_elem_tail17", [8, 17], 0, "Small-core unaligned width=17"), ("s_elem_tail31", [16, 31], 1, "Small-core unaligned width=31")],
        "M_MID_UB": [("m_elem_tail63", [256, 63], 2, "Mid-UB unaligned width=63"), ("m_elem_vec4k", [128, 4096], 1, "Mid-UB aligned vectorized tile")],
        "L_HBM_TILED": [("l_elem_hbm_16k", [256, 16384], 0, "Large HBM bandwidth-bound 16M-byte sweep"), ("l_elem_hbm_bf16", [512, 8192], 2, "Large HBM bfloat16/mixed-dtype sweep")],
        "X_EXTREME": [("x_elem_tail127", [17, 127], 2, "Extreme unaligned prime shape (17, 127)"), ("x_elem_odd4097", [3, 4097], 0, "Extreme odd prime width=4097")],
    },
    "reduction": {
        "PUBLIC": [("pub_red_256x512", [256, 512], 0, "Public 2D inner reduction"), ("pub_red_64x1024", [64, 1024], 1, "Public 2D reduction half precision")],
        "S_SMALL_CORE": [("s_red_4x17", [4, 17], 0, "Small-core reduction width=17"), ("s_red_31x31", [31, 31], 2, "Small-core square prime (31, 31)")],
        "M_MID_UB": [("m_red_64x63", [64, 63], 1, "Mid-UB unaligned inner width=63"), ("m_red_512x512", [512, 512], 2, "Mid-UB multi-core row reduction")],
        "L_HBM_TILED": [("l_red_256x16384", [256, 16384], 0, "Large HBM two-pass long-row reduction"), ("l_red_4096x1024", [4096, 1024], 1, "Large HBM many-row reduction")],
        "X_EXTREME": [("x_red_3x127", [3, 127], 2, "Extreme 3-row width=127 cross-core check"), ("x_red_outer_4097x31", [4097, 31], 0, "Extreme tall outer reduction (4097, 31)")],
    },
    "transpose": {
        "PUBLIC": [("pub_tr_256x256", [256, 256], 0, "Public 2D square transpose"), ("pub_tr_64x128", [64, 128], 1, "Public 2D fp16/mixed transpose")],
        "S_SMALL_CORE": [("s_tr_17x31", [17, 31], 0, "Small-core odd transpose (17, 31)"), ("s_tr_31x63", [31, 63], 1, "Small-core unaligned transpose (31, 63)")],
        "M_MID_UB": [("m_tr_128x127", [128, 127], 2, "Mid-UB unaligned tail width=127"), ("m_tr_16x64x64", [16, 64, 64], 1, "Mid-UB 3D middle-axis permutation")],
        "L_HBM_TILED": [("l_tr_1024x1024", [1024, 1024], 0, "Large HBM bank-conflict-prone 1024x1024"), ("l_tr_32x256x256", [32, 256, 256], 2, "Large HBM 3D high-bandwidth transpose")],
        "X_EXTREME": [("x_tr_8x17x31x63", [8, 17, 31, 63], 2, "Extreme 4D all-prime-tail transpose"), ("x_tr_3x4097", [3, 4097], 0, "Extreme skinny stride-4097 transpose")],
    },
    "gather_scatter": {
        "PUBLIC": [("pub_gs_1024x128", [1024, 128], 0, "Public gather/scatter aligned table"), ("pub_gs_512x256", [512, 256], 1, "Public gather/scatter half precision")],
        "S_SMALL_CORE": [("s_gs_32x17", [32, 17], 0, "Small-core unaligned slice width=17"), ("s_gs_64x31", [64, 31], 2, "Small-core unaligned slice width=31")],
        "M_MID_UB": [("m_gs_1024x63", [1024, 63], 1, "Mid-UB unaligned slice width=63"), ("m_gs_2048x256", [2048, 256], 2, "Mid-UB dense-index update")],
        "L_HBM_TILED": [("l_gs_16384x512", [16384, 512], 0, "Large HBM random-access table"), ("l_gs_8192x1024", [8192, 1024], 1, "Large HBM wide embedding gather/scatter")],
        "X_EXTREME": [("x_gs_4097x127", [4097, 127], 2, "Extreme prime rows=4097 tail=127"), ("x_gs_dup_hot_31", [4097, 31], 0, "Extreme high-contention duplicate-index update")],
    },
    "pad_slice": {
        "PUBLIC": [("pub_ps_4x16x16", [4, 16, 16], 0, "Public 3D pad/slice baseline"), ("pub_ps_8x64x64", [8, 64, 64], 1, "Public 3D half-precision pad/slice")],
        "S_SMALL_CORE": [("s_ps_1x3x17", [1, 3, 17], 0, "Small-core narrow inner width=17"), ("s_ps_2x17x31", [2, 17, 31], 2, "Small-core prime dims (2, 17, 31)")],
        "M_MID_UB": [("m_ps_1x3x224x63", [1, 3, 224, 63], 2, "Mid-UB 4D vision tail width=63"), ("m_ps_16x64x128", [16, 64, 128], 1, "Mid-UB 3D asymmetric pad/slice")],
        "L_HBM_TILED": [("l_ps_32x128x512", [32, 128, 512], 0, "Large HBM 3D bulk copy"), ("l_ps_16x256x1024", [16, 256, 1024], 1, "Large HBM 3D wide-row pad/slice")],
        "X_EXTREME": [("x_ps_2x5x17x31x127", [2, 5, 17, 31, 127], 2, "Extreme 5D circular/reflect width=127"), ("x_ps_neg_crop_31", [8, 64, 31], 0, "Extreme negative-pad crop width=31")],
    },
    "sequence_dp": {
        "PUBLIC": [("pub_dp_t50_c32", [50, 16, 32], 0, "Public sequence DP T=50 C=32"), ("pub_dp_t100_c64", [100, 16, 64], 1, "Public sequence DP T=100 C=64")],
        "S_SMALL_CORE": [("s_dp_t17_c17", [17, 4, 17], 0, "Small-core prime sequence T=17 C=17"), ("s_dp_t31_c31", [31, 8, 31], 2, "Small-core prime sequence T=31 C=31")],
        "M_MID_UB": [("m_dp_t63_c127", [63, 16, 127], 2, "Mid-UB unaligned T=63 C=127"), ("m_dp_t128_c512", [128, 16, 512], 1, "Mid-UB medium vocab sequence")],
        "L_HBM_TILED": [("l_dp_t256_c4096", [256, 16, 4096], 0, "Large HBM large vocab=4096"), ("l_dp_t512_c1024", [512, 8, 1024], 1, "Large HBM long horizon T=512")],
        "X_EXTREME": [("x_dp_b1_t300_c63", [300, 1, 63], 0, "Extreme B=1 single-core dependency chain"), ("x_dp_ti_eq_si_127", [127, 8, 127], 2, "Extreme zero-blank slack T==S==127")],
    },
}

_FAMILY_EDGE_ATTRS: dict[str, list[str]] = {
    "elementwise": ["scalar_broadcast", "1d_singleton_broadcast"],
    "reduction": ["reduce_axis_last_and_0", "keepdim_toggle"],
    "transpose": ["non_contiguous_perm", "inner_dim_swap"],
    "gather_scatter": ["duplicate_indices", "unsorted_boundary_indices"],
    "pad_slice": ["negative_pad_crop", "asymmetric_reflect_circular"],
    "sequence_dp": ["ti_equals_si", "zero_length_or_b1_single_core"],
}


def _normalize_dtypes(dtypes: list[str] | None) -> list[str]:
    if not dtypes:
        return ["float32", "float16", "bfloat16"]
    out: list[str] = []
    for dt in dtypes:
        norm = _DTYPE_ALIASES.get(str(dt).strip().lower(), str(dt).strip().lower())
        if norm and norm not in out:
            out.append(norm)
    return out or ["float32", "float16", "bfloat16"]


def _dtype_bytes(dtype: str) -> int:
    return {"float16": 2, "bfloat16": 2, "int8": 1, "uint8": 1, "int64": 8}.get(dtype, 4)


def _build_family_perf_buckets(operator_family: str, dtypes: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Generate stratified buckets across all 5 strata + unaligned tail widths (17, 31, 63, 127)."""
    spec = _FAMILY_SHAPE_TEMPLATES[operator_family]
    dt_slots = [dtypes[0], dtypes[1 % len(dtypes)], dtypes[2 % len(dtypes)]]

    def _make_entry(bid: str, stratum: str, shape: list[int], dt_idx: int, desc: str, mult: float, floor: float) -> dict[str, Any]:
        dtype = dt_slots[dt_idx]
        io_bytes = math.prod(shape) * _dtype_bytes(dtype) * 2
        roof = round(max(3.4, 3.35 + io_bytes / (724e9 * 1e-6)), 3)
        torch_est = round(max(roof * mult, floor), 3)
        return {
            "id": bid, "stratum": stratum, "dtype": dtype, "shape": list(shape),
            "io_bytes": io_bytes, "roofline_us": roof, "tau_torch_us": torch_est,
            "torch_ref_us": torch_est, "description": desc, "source": "bench_scaffold",
        }

    pub_raw = spec["PUBLIC"]
    public_anchors = [
        {**_make_entry(bid, "PUBLIC", shape, dt_i, desc, 1.35, 4.5), "weight_pts": round(10.0 / len(pub_raw), 2)}
        for bid, shape, dt_i, desc in pub_raw
    ]
    hidden_buckets: list[dict[str, Any]] = []
    for stratum in ("S_SMALL_CORE", "M_MID_UB", "L_HBM_TILED", "X_EXTREME"):
        entries = spec[stratum]
        per_w = round(_HIDDEN_WEIGHTS[stratum] / len(entries), 6)
        max_w = 0.04 if stratum == "X_EXTREME" else 0.22
        for bid, shape, dt_i, desc in entries:
            hidden_buckets.append({**_make_entry(bid, stratum, shape, dt_i, desc, 1.4, 4.8), "prior_w": per_w, "max_w": max_w})
    return public_anchors, hidden_buckets


def _build_family_correctness_cases(operator_family: str, dtypes: list[str]) -> list[dict[str, Any]]:
    """Generate adversarial correctness cases (`empty/tiny`, `NaN/Inf/subnormal`, `odd-prime`, `max-broadcast`)."""
    edge_attrs = _FAMILY_EDGE_ATTRS[operator_family]
    templates = (
        ("empty_tiny", "G0_DEGEN", "D0_STD", [[0], [1], [1, 1]], ["empty_zero_elem", "scalar_tiny", *edge_attrs[:1]], [11, 29]),
        ("nan_inf_subnormal", "G4_DTYPE_CROSS", "D4_ULP_INF", [[4, 17], [16, 63]], ["NaN", "Inf", "-Inf", "subnormal", "0.0", "-0.0"], [13, 37]),
        ("overflow_cancel", "G2_CORE_TILE", "D1_OVERFLOW", [[40, 127], [41, 127]], ["core_boundary_40_41", "fp16_65504_overflow", "catastrophic_cancel_1000_plus_eps"], [17, 43]),
        ("noncontig_odd_prime", "G1_ODD_ALIGN", "D2_SUBNORM", [[3, 17], [5, 31], [7, 63], [11, 127]], ["non_contiguous_strided", "unaligned_prime_tails_17_31_63_127"], [19, 53]),
        ("max_broadcast", "G3_HIGH_RANK", "D3_CANCEL", [[2, 1, 17, 1, 31], [1, 5, 1, 63, 1]], ["max_rank_5d_broadcast", *edge_attrs], [23, 61]),
    )
    return [
        {
            "id": f"scaffold_{operator_family}_{suffix}", "geometry": geom, "distribution": dist,
            "shapes": shapes, "dtypes": dtypes, "edge_attrs": attrs, "seeds": seeds,
            "verified": False, "source": "bench_scaffold",
        }
        for suffix, geom, dist, shapes, attrs, seeds in templates
    ]


def scaffold_bench_specs(
    workspace: Path,
    title: str,
    operator_family: str = "elementwise",
    dtypes: list[str] | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Generate/append canonical stratified `cases_perf.json` + `cases_correctness.json` (strictly append-only)."""
    fam = (operator_family or "elementwise").strip().lower()
    if fam not in VALID_OPERATOR_FAMILIES:
        raise ValueError(f"Unsupported operator_family '{operator_family}'. Must be one of: {', '.join(VALID_OPERATOR_FAMILIES)}")

    norm_dtypes = _normalize_dtypes(dtypes)
    ws = Path(workspace).expanduser().resolve()
    canonical_title, _ = _resolve_problem_info(ws, title)
    target_dir = _bench_specs_dir(ws) / canonical_title
    perf_path, corr_path = target_dir / "cases_perf.json", target_dir / "cases_correctness.json"

    cand_public, cand_hidden = _build_family_perf_buckets(fam, norm_dtypes)
    cand_corr = _build_family_correctness_cases(fam, norm_dtypes)

    existing_perf_doc = read_json(perf_path, None) if perf_path.is_file() else None
    all_existing_perf_ids: set[str] = set()
    if isinstance(existing_perf_doc, dict):
        for k in ("public_anchors", "hidden_buckets", "buckets"):
            for item in existing_perf_doc.get(k) or []:
                if isinstance(item, dict) and item.get("id"):
                    all_existing_perf_ids.add(str(item["id"]))

    missing_public = [b for b in cand_public if b["id"] not in all_existing_perf_ids]
    missing_hidden = [b for b in cand_hidden if b["id"] not in all_existing_perf_ids]
    missing_perf_ids = [b["id"] for b in (*missing_public, *missing_hidden)]

    existing_corr_doc = read_json(corr_path, None) if corr_path.is_file() else None
    existing_corr_ids = {
        str(item["id"])
        for item in ((existing_corr_doc.get("cases") or []) if isinstance(existing_corr_doc, dict) else [])
        if isinstance(item, dict) and item.get("id")
    }
    missing_corr = [c for c in cand_corr if c["id"] not in existing_corr_ids]
    missing_corr_ids = [c["id"] for c in missing_corr]
    perf_existed, corr_existed = isinstance(existing_perf_doc, dict), isinstance(existing_corr_doc, dict)

    if apply:
        now_iso = utc_now().isoformat()
        if perf_existed:
            doc_p = dict(existing_perf_doc)
            if missing_public:
                doc_p["public_anchors"] = [*list(doc_p.get("public_anchors") or []), *missing_public]
            if missing_hidden:
                key_h = "hidden_buckets" if ("hidden_buckets" in doc_p or "buckets" not in doc_p) else "buckets"
                doc_p[key_h] = [*list(doc_p.get(key_h) or []), *missing_hidden]
            if missing_public or missing_hidden:
                doc_p["scaffold_appended_at"] = now_iso
                _atomic_write_json(perf_path, doc_p)
        else:
            _atomic_write_json(perf_path, {
                "problem": canonical_title, "version": "2.0", "append_only": True, "operator_family": fam,
                "protocol": {"profiler": "profiler_single_session", "warmup": 2, "repeat": 5, "sync_per_iter": True, "metric": "task_duration_median_us"},
                "hardware_roofline": {"soc": "soc-a", "hbm_bw_gbps": 724.0, "aiv_cores": 40, "launch_floor_us": 3.35},
                "strata_priors": {"PUBLIC": 0.10, "S_SMALL_CORE": 0.27, "M_MID_UB": 0.315, "L_HBM_TILED": 0.18, "X_EXTREME": 0.135},
                "public_anchors": cand_public, "hidden_buckets": cand_hidden, "scaffolded_at": now_iso,
            })

        if corr_existed:
            if missing_corr:
                doc_c = dict(existing_corr_doc)
                doc_c["cases"] = [*list(doc_c.get("cases") or []), *missing_corr]
                doc_c["scaffold_appended_at"] = now_iso
                _atomic_write_json(corr_path, doc_c)
        else:
            _atomic_write_json(corr_path, {
                "problem": canonical_title, "version": "1.0", "operator_family": fam,
                "sentinel_guard": {"prefill_pattern": "0x5A_NaN_canary", "head_guard_bytes": 32, "tail_guard_bytes": 32, "require_fp64_oracle": True},
                "geometry_layers": ["G0_DEGEN", "G1_ODD_ALIGN", "G2_CORE_TILE", "G3_HIGH_RANK", "G4_DTYPE_CROSS", "G5_EDGE_ATTR"],
                "distribution_layers": ["D0_STD", "D1_OVERFLOW", "D2_SUBNORM", "D3_CANCEL", "D4_ULP_INF", "D5_ESCAPE_REG"],
                "cases": cand_corr, "scaffolded_at": now_iso,
            })

    return {
        "title": canonical_title, "operator_family": fam, "dtypes": norm_dtypes,
        "applied": bool(apply), "target_dir": str(target_dir),
        "cases_perf_path": str(perf_path), "cases_correctness_path": str(corr_path),
        "perf_existed": perf_existed, "correctness_existed": corr_existed,
        "existing_perf_count": len(all_existing_perf_ids), "existing_correctness_count": len(existing_corr_ids),
        "appended_perf_ids": missing_perf_ids if apply else [], "pending_perf_ids": [] if apply else missing_perf_ids,
        "appended_correctness_ids": missing_corr_ids if apply else [], "pending_correctness_ids": [] if apply else missing_corr_ids,
        "strata_covered": ["PUBLIC", "S_SMALL_CORE", "M_MID_UB", "L_HBM_TILED", "X_EXTREME"],
    }
