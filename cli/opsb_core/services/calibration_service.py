"""Per-problem 5-stratum score calibration, residual ledger, and 6x5 bench coverage reader."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..config import read_json

STRATA_PRIORS: list[tuple[str, float]] = [
    ("PUBLIC", 0.10),
    ("S_SMALL_CORE", 0.27),
    ("M_MID_UB", 0.315),
    ("L_HBM_TILED", 0.18),
    ("X_EXTREME", 0.135),
]

GEOM_ROWS = ["G0_DEGEN", "G1_ODD_ALIGN", "G2_CORE_TILE", "G3_HIGH_RANK", "G4_DTYPE_CROSS", "G5_EDGE_ATTR"]
DATA_COLS = ["D0_STD", "D1_OVERFLOW", "D2_SUBNORM", "D3_CANCEL", "D4_ULP_INF"]


def _inspect_coverage_grid(repo_dir: Path) -> dict[str, Any]:
    corr_json = repo_dir / "bench" / "cases_correctness.json"
    cells = [[0 for _ in DATA_COLS] for _ in GEOM_ROWS]

    if corr_json.is_file():
        data = read_json(corr_json, {})
        cases = data.get("cases") if isinstance(data, dict) else (data if isinstance(data, list) else [])
        for c in cases or []:
            if not isinstance(c, dict):
                continue
            g = str(c.get("geometry") or c.get("geom") or "G0")
            d = str(c.get("distribution") or c.get("dist") or "D0")
            gi = next((i for i, r in enumerate(GEOM_ROWS) if r.startswith(g[:2].upper())), 0)
            di = next((i for i, col in enumerate(DATA_COLS) if col.startswith(d[:2].upper())), 0)
            cells[gi][di] = 2 if c.get("verified", True) else max(cells[gi][di], 1)
        return {"rows": GEOM_ROWS, "cols": DATA_COLS, "cells": cells}

    ext_py = repo_dir / "validation" / "extended.py"
    if ext_py.is_file():
        try:
            txt = ext_py.read_text(encoding="utf-8", errors="replace").lower()
        except Exception:
            txt = ""
        has_empty = any(k in txt for k in ("empty", "0,", "(1,)", "numel == 0", "degenerate"))
        has_odd = any(re.search(p, txt) for p in (r"\b13\b", r"\b17\b", r"\b31\b", r"\b33\b", r"\b63\b", r"\b65\b", r"\b127\b", r"\b4097\b"))
        has_tile = any(k in txt for k in ("chunk", "tile", "10000", "20000", "8192", "65536", "multi"))
        has_rank = any(k in txt for k in ("5d", "6d", "rank", "transpose", "4d", "broadcast"))
        has_dtype = sum(1 for k in ("float16", "bfloat16", "float32", "int32", "int8") if k in txt) >= 2
        has_edge = any(k in txt for k in ("neg", "reflect", "circular", "stride", "zero_infinity", "broadcast"))

        has_ovf = any(k in txt for k in ("1e4", "1e5", "65504", "large", "overflow", "max"))
        has_sub = any(k in txt for k in ("1e-5", "1e-6", "1e-7", "eps", "small", "tiny"))
        has_can = any(k in txt for k in ("1000", "offset", "cancel", "4097", "256", "mean"))
        has_ulp = any(k in txt for k in ("inf", "nan", "0.5", "rint", "round", "identical"))

        g_flags = [has_empty, has_odd, has_tile, has_rank, has_dtype, has_edge]
        d_flags = [True, has_ovf, has_sub, has_can, has_ulp]
        for gi, gf in enumerate(g_flags):
            for di, df in enumerate(d_flags):
                if gf and df:
                    cells[gi][di] = 2 if di == 0 else 1
                elif di == 0:
                    cells[gi][di] = 1
    return {"rows": GEOM_ROWS, "cols": DATA_COLS, "cells": cells}


def build_problem_calibration(
    workspace: Path,
    repo_dir: Path,
    title: str,
    cur_score: float,
    public_pts: float,
    hidden_pts: float,
    implied_hidden_r: float,
    history: list[dict[str, Any]],
    est_by_sha7: dict[str, float],
) -> dict[str, Any]:
    cal_file = workspace / "results" / title / "calibration.json"
    disk_cal = read_json(cal_file, None)

    obs_by_sha7: dict[str, dict[str, Any]] = {}
    if isinstance(disk_cal, dict):
        for obs in disk_cal.get("observation_ledger") or []:
            if isinstance(obs, dict) and obs.get("short_sha"):
                obs_by_sha7[str(obs["short_sha"])[:7]] = obs

    # Annotate history entries with estimated_score & residual_gap (`actual - est`)
    residuals: list[float] = []
    last_pred: float | None = None
    last_res: float | None = None
    for h in history:
        s7 = str(h.get("sha") or "")[:7]
        obs_entry = obs_by_sha7.get(s7)
        est = obs_entry.get("predicted_score") if obs_entry and obs_entry.get("predicted_score") is not None else h.get("estimated_score")
        if est is None and s7 in est_by_sha7:
            est = est_by_sha7[s7]
        if est is not None:
            h["estimated_score"] = round(float(est), 2)
            if h.get("status") == "Pass" and h.get("score") is not None:
                res = (
                    round(float(obs_entry["residual_pts"]), 2)
                    if (obs_entry and obs_entry.get("residual_pts") is not None)
                    else round(float(h["score"]) - float(est), 2)
                )
                h["residual_gap"] = res
                residuals.append(abs(res))
                if last_pred is None:
                    last_pred = round(float(est), 2)
                    last_res = res

    mae_pts = round(sum(residuals) / len(residuals), 2) if residuals else None

    # Escape Lock: triggered if most recent online evaluation failed (WA/TLE/RE) after passing local check
    latest_online = next((h for h in history if h.get("status") not in ("rejected", "skipped", "duplicate")), None)
    inferred_escape = bool(
        latest_online and latest_online.get("status") in ("Wrong Answer", "Time Limit Exceeded", "Runtime Error") and cur_score == 0
    )

    cov_grid = _inspect_coverage_grid(repo_dir)
    if isinstance(disk_cal, dict) and disk_cal.get("strata"):
        merged = dict(disk_cal)
        merged.setdefault("mae_pts", mae_pts)
        merged.setdefault("last_predicted_score", last_pred)
        merged.setdefault("last_residual_pts", last_res)
        merged.setdefault("escape_lock", inferred_escape)
        merged.setdefault("unpaired_bucket_count", 0)
        merged.setdefault("coverage_grid", cov_grid)
        return merged

    pub_r = round(min(1.0, public_pts / 10.0), 4) if public_pts > 0 else 0.0
    # Tilt strata efficiency slightly around implied_hidden_r using public vs hidden spread
    spread = (pub_r - implied_hidden_r) if (pub_r > 0 and implied_hidden_r > 0) else 0.0
    rel_tilts = {
        "S_SMALL_CORE": 1.0 + 0.20 * spread,
        "M_MID_UB": 1.02,
        "L_HBM_TILED": 0.98 - 0.10 * spread,
        "X_EXTREME": 0.92 - 0.15 * spread,
    }
    raw_hid_pts = {}
    for name, pw in STRATA_PRIORS[1:]:
        r_est = min(1.0, max(0.0, implied_hidden_r * rel_tilts[name]))
        raw_hid_pts[name] = (pw, r_est, 100.0 * pw * r_est)
    sum_raw_hid = sum(v[2] for v in raw_hid_pts.values())
    scale = (hidden_pts / sum_raw_hid) if (sum_raw_hid > 1e-6 and hidden_pts > 0) else 1.0

    strata_out = [
        {
            "stratum": "PUBLIC",
            "prior_w": 0.10,
            "learned_w": 0.10,
            "efficiency_r": round(pub_r, 3),
            "current_pts": round(public_pts, 2),
            "headroom_pts": round(max(0.0, 10.0 - public_pts), 2),
        }
    ]
    for name, pw in STRATA_PRIORS[1:]:
        _, r_est, p_raw = raw_hid_pts[name]
        c_pts = round(min(100.0 * pw, p_raw * scale), 2) if hidden_pts > 0 else 0.0
        eff = round(c_pts / (100.0 * pw), 3) if pw > 0 else 0.0
        strata_out.append(
            {
                "stratum": name,
                "prior_w": pw,
                "learned_w": pw,
                "efficiency_r": eff,
                "current_pts": c_pts,
                "headroom_pts": round(max(0.0, 100.0 * pw - c_pts), 2),
            }
        )

    return {
        "mae_pts": mae_pts,
        "last_predicted_score": last_pred,
        "last_residual_pts": last_res,
        "escape_lock": inferred_escape,
        "escape_sha": latest_online.get("sha")[:7] if (inferred_escape and latest_online and latest_online.get("sha")) else None,
        "unpaired_bucket_count": 0,
        "strata": strata_out,
        "coverage_grid": cov_grid,
    }
