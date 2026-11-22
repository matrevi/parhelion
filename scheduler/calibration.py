"""Closed-Loop Simplex MAP Benchmark Calibration & Escape-Lock Engine.

Models the hidden 45-case contest score `S_hid(SHA) in [0, 90]` via a stratified
K-bucket physical roofline + Entropic Mirror Descent (Exponentiated Gradient) MAP
estimator on the upper-bounded probability simplex:

    S_hat(SHA) = S_pub_hat(SHA) + 90 * sum_{k=1..K} w_hat_k * min(1, beta_p * theta_k^(0) / tau_k(SHA))
    theta_k^(0) = max(tau_k^roof, eta_p * tau_k^torch)

Three-tier self-calibrating hierarchy (pure Python 3 stdlib, <5 ms for all 10 ops):
  1. Tier-1 Scalar Tightness Anchor (`beta_p`):
     Exact 1D bisection so the active board commit has zero anchor bias (`S_hat(SHA*) == S_live`).
  2. Tier-2 Capped Simplex MAP (`w_hat in Delta^{K-1}`, `w_k <= 0.04` on `X_EXTREME`):
     Minimizes Huber loss (delta = 2.70 pts) + KL(w || w^(0)) + L2 shrinkage via
     Exponentiated Gradient with exact water-filling Bregman projection.
     Automatically prunes synthetic tail weights (e.g. ConcatD `unalign_*` after 2dd9acd)
     and computes diagonal Laplace posterior covariance `Sigma_w` -> epistemic `sigma_delta`.
  3. Three Closed-Loop Residual Diagnostic Triggers (`|S_actual - S_hat| > 1.5 * sigma`):
     - `UNPAIRED_BUCKETS`: missing local `profiler` / `bf16` / `fp16` bucket coverage
     - `PROLOGUE_TAX_ALERT` / `PRUNE_PHANTOM_WEIGHT`: small-shape dispatch tax vs tail gain
     - `ESCAPE_LOCK`: hard Stage-1 safety lock when an online `WA`/`TLE`/`RE` escaped local S1,
       cleared via `verify-escape --title <T> --bad-sha <S1> --good-sha <S2>`.
"""
from __future__ import annotations

import datetime
import json
import math
import pathlib
import re
from typing import Any

from .estimator import (
    SIGMA_HIDDEN_JITTER,
    ProblemLedger,
    ScoreEstimator,
    _code_fingerprint,
    _parse_iso,
    parse_validation_perf,
    resolve_sha,
)

STRATA_ORDER: tuple[tuple[str, str, float], ...] = (
    ("PUBLIC", "Public", 0.10),
    ("S_SMALL_CORE", "Small-Core", 0.27),
    ("M_MID_UB", "Mid-UB", 0.315),
    ("L_HBM_TILED", "Large-HBM", 0.18),
    ("X_EXTREME", "Extreme", 0.135),
)

HIDDEN_STRATA_PRIORS: dict[str, float] = {
    "S_SMALL_CORE": 0.30,
    "M_MID_UB": 0.35,
    "L_HBM_TILED": 0.20,
    "X_EXTREME": 0.15,
}

GEOM_ROWS: list[str] = [
    "G0_DEGEN",
    "G1_ODD_ALIGN",
    "G2_CORE_TILE",
    "G3_HIGH_RANK",
    "G4_DTYPE_CROSS",
    "G5_EDGE_ATTR",
]
DATA_COLS: list[str] = [
    "D0_STD",
    "D1_OVERFLOW",
    "D2_SUBNORM",
    "D3_CANCEL",
    "D4_ULP_INF",
]


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _huber_grad(residual: float, delta: float = SIGMA_HIDDEN_JITTER) -> float:
    if abs(residual) <= delta:
        return residual
    return delta if residual > 0.0 else -delta


def project_capped_simplex(
    weights: list[float],
    upper_bounds: list[float],
    min_floor: float = 0.005,
) -> list[float]:
    """Exact multiplicative / water-filling projection onto `{w >= min_floor : sum(w) = 1, w_k <= u_k}`."""
    k_len = len(weights)
    if k_len == 0:
        return []
    lb = min(min_floor, 0.5 / k_len)
    w = [max(lb, float(x)) for x in weights]
    caps = [max(lb + 1e-6, float(u)) for u in upper_bounds]
    if sum(caps) <= 1.0 + 1e-9:
        tot_c = sum(caps)
        return [c / tot_c for c in caps]

    # Shift by `lb` so `z_i = w_i - lb >= 0`, `sum(z) = 1 - k_len * lb`, `z_i <= caps_i - lb`
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


class BenchmarkCalibrator:
    """Fits and serves Stratified Simplex MAP score models and `ESCAPE_LOCK` state."""

    def __init__(
        self,
        workspace: pathlib.Path,
        estimator: ScoreEstimator | None = None,
        specs_root: pathlib.Path | None = None,
        verified_escapes: dict[str, dict[str, Any]] | None = None,
        state_dir: pathlib.Path | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.estimator = estimator or ScoreEstimator(self.workspace)
        self.specs_root = (
            specs_root
            or (pathlib.Path(__file__).resolve().parents[1] / "bench-specs")
        )
        self.verified_escapes = verified_escapes or {}
        # B0 backfill lane store (`<state>/backfill_timings.json`); built lazily on first use
        self.state_dir = pathlib.Path(state_dir) if state_dir else None
        self.backfill: Any = None
        self._perf_spec_cache: dict[str, dict[str, Any]] = {}
        self._timings_cache: dict[tuple[str, str, str | None], tuple[dict[str, float], dict[str, float], str | None, str]] = {}
        self._regime_cache: dict[tuple[str, str, str | None], str] = {}
        self._cal_cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._esc_cache: dict[tuple[str, str], dict[str, Any]] = {}

    def _spec_paths(self, title: str) -> tuple[pathlib.Path, pathlib.Path]:
        repo_bench = self.workspace / "bench" / title / "bench"
        sup_bench = self.specs_root / title
        corr = (repo_bench / "cases_correctness.json") if (repo_bench / "cases_correctness.json").is_file() else (sup_bench / "cases_correctness.json")
        perf = (repo_bench / "cases_perf.json") if (repo_bench / "cases_perf.json").is_file() else (sup_bench / "cases_perf.json")
        return corr, perf

    def load_perf_spec(self, title: str) -> dict[str, Any]:
        if title in self._perf_spec_cache:
            return self._perf_spec_cache[title]
        _, perf_path = self._spec_paths(title)
        doc: dict[str, Any] = {"problem": title, "public_anchors": [], "hidden_buckets": []}
        if perf_path.is_file():
            try:
                doc = json.loads(perf_path.read_text())
            except Exception:
                pass
        self._perf_spec_cache[title] = doc
        return doc

    def build_coverage_grid(self, title: str) -> dict[str, Any]:
        corr_path, _ = self._spec_paths(title)
        cells = [[0 for _ in DATA_COLS] for _ in GEOM_ROWS]
        case_counts = [[0 for _ in DATA_COLS] for _ in GEOM_ROWS]
        d5_escapes: list[dict[str, Any]] = []

        # Also inspect problem repo's live `validation/extended.py` to upgrade cells dynamically
        ext_py = self.workspace / "bench" / title / "validation" / "extended.py"
        ext_txt = ext_py.read_text(errors="replace").lower() if ext_py.is_file() else ""
        live_col_boost = {
            0: True,
            1: any(k in ext_txt for k in ("1e4", "1e5", "65504", "large", "overflow")),
            2: any(k in ext_txt for k in ("1e-5", "1e-6", "1e-7", "eps", "tiny")),
            3: any(k in ext_txt for k in ("1000", "offset", "cancel", "welford")),
            4: any(k in ext_txt for k in ("inf", "nan", "0.5", "rint", "identical")),
        }

        if corr_path.is_file():
            try:
                doc = json.loads(corr_path.read_text())
                for c in doc.get("cases") or []:
                    if not isinstance(c, dict):
                        continue
                    dist = str(c.get("distribution") or "D0")
                    geom = str(c.get("geometry") or "G0")
                    if dist.startswith("D5"):
                        d5_escapes.append({
                            "id": c.get("id"),
                            "sha_bad": c.get("sha_bad"),
                            "description": c.get("desc") or c.get("description", ""),
                            "verified": bool(c.get("verified", True)),
                        })
                        continue
                    gi = next((i for i, r in enumerate(GEOM_ROWS) if r.startswith(geom[:2].upper())), 0)
                    di = next((i for i, col in enumerate(DATA_COLS) if col.startswith(dist[:2].upper())), 0)
                    case_counts[gi][di] += len(c.get("seeds") or [1])
                    ver = bool(c.get("verified", False) or live_col_boost.get(di, False))
                    cells[gi][di] = 2 if ver else max(cells[gi][di], 1)
            except Exception:
                pass

        return {
            "rows": GEOM_ROWS,
            "cols": DATA_COLS,
            "row_labels": GEOM_ROWS,
            "col_labels": DATA_COLS,
            "cells": cells,
            "case_counts": case_counts,
            "d5_escape_count": len(d5_escapes),
            "d5_escape_regressions": d5_escapes,
        }

    # -- B0 backfill lane: paired-timing store (`<state>/backfill_timings.json`) -----
    def _backfill_store(self) -> Any:
        """Lazily build the `BackfillScheduler` used as the backfilled-pairing store."""
        if self.backfill is None:
            try:
                from .backfill import BackfillScheduler

                self.backfill = BackfillScheduler(
                    self.workspace,
                    state_dir=self.state_dir,
                    estimator=self.estimator,
                )
            except Exception:
                self.backfill = False
        return self.backfill or None

    def _backfill_pairings(self, title: str, sha: str) -> dict[str, Any] | None:
        store = self._backfill_store()
        if store is None or not sha:
            return None
        try:
            return store.timings_for(title, sha)
        except Exception:
            return None

    def unpaired_buckets(self, title: str, as_of: datetime.datetime | None = None) -> dict[str, Any]:
        """Authoritative pairing census for the pinned baseline commit (B0 detection source).

        Uses exactly the timing-selection path of `calibrate_problem()` so the census can
        never disagree with `calibration.json::unpaired_bucket_count`.
        """
        ledger = self.estimator.get_problem_ledger(title, as_of=as_of)
        perf_spec = self.load_perf_spec(title)
        buckets = list(perf_spec.get("hidden_buckets") or [])
        sha = ledger.active_board_sha or ""
        timings: dict[str, float] = {}
        soc: str | None = None
        modality = "none"
        if sha and buckets:
            timings, _torch, soc, modality = self._extract_bucket_timings(
                title, sha, buckets, allow_future_siblings=(as_of is None)
            )
        paired = [b["id"] for b in buckets if b["id"] in timings]
        unpaired = [b for b in buckets if b["id"] not in timings]
        return {
            "title": title,
            "baseline_sha": sha or None,
            "baseline_short": sha[:7] if sha else None,
            "bucket_count": len(buckets),
            "paired_count": len(paired),
            "unpaired_count": len(unpaired),
            "paired_buckets": paired,
            "unpaired_buckets": [b["id"] for b in unpaired],
            "unpaired_details": [
                {
                    "id": b["id"],
                    "stratum": b.get("stratum"),
                    "dtype": b.get("dtype"),
                    "description": b.get("description", ""),
                }
                for b in unpaired
            ],
            "modality": modality,
            "runtime_soc": soc,
            "observed_us": {k: round(float(v), 3) for k, v in timings.items()},
        }

    @staticmethod
    def _detect_profiler_regime(res_json_path: pathlib.Path, log_text: str) -> str:
        """Identify measurement regime so fast single-session l0 timings never mix blindly with legacy PMU runs."""
        if res_json_path.is_file():
            try:
                rd = json.loads(res_json_path.read_text())
                reg = rd.get("profiler_regime")
                if isinstance(reg, str) and reg:
                    return reg
                if rd.get("phase_c_cpu_sec") is not None:
                    return "fast_single_session_l0"
            except Exception:
                pass
        if "[PHASE-C] Lock-free verify + profiler parse" in log_text or "fast_profiler" in log_text:
            return "fast_single_session_l0"
        return "legacy_multiproc_pmu"

    @staticmethod
    def _backfill_regime(backfill: dict[str, Any] | None) -> str:
        if not isinstance(backfill, dict):
            return "legacy_multiproc_pmu"
        if backfill.get("profiler_regime") == "fast_single_session_l0":
            return "fast_single_session_l0"
        mat = str(backfill.get("updated_at") or backfill.get("measured_at") or "")
        if mat >= "2026-11-22T10:50:00":
            return "fast_single_session_l0"
        return "legacy_multiproc_pmu"

    def _extract_bucket_timings(
        self,
        title: str,
        sha: str,
        buckets: list[dict[str, Any]],
        target_soc: str | None = "soc-a",
        allow_future_siblings: bool = True,
    ) -> tuple[dict[str, float], dict[str, float], str | None, str]:
        """Extract normalized per-bucket timings (us), torch_npu refs, SoC, and timing modality."""
        t_key = (title, sha, target_soc if allow_future_siblings else None)
        if t_key in self._timings_cache:
            return self._timings_cache[t_key]
        repo = self.workspace / "bench" / title
        res_dir = self.workspace / "results" / title
        vlog = res_dir / sha / "validation.log"
        rj_path = res_dir / sha / "result.json"
        backfill = self._backfill_pairings(title, sha)

        vlog_text = vlog.read_text(errors="replace") if vlog.is_file() else ""
        best_perf = parse_validation_perf(vlog_text) if vlog_text else None
        best_regime = self._detect_profiler_regime(rj_path, vlog_text) if best_perf is not None else "none"
        # Scan C++-identical sibling commits in live mode whenever the direct log is missing,
        # incomplete, on a different SoC, OR measured under the legacy multi-proc PMU regime!
        needs_sibling_search = allow_future_siblings and (
            best_perf is None
            or best_regime != "fast_single_session_l0"
            or (best_perf.get("runtime_soc") != target_soc and not best_perf["profiler_cases"])
            or (title == "ConcatD" and len(best_perf["profiler_cases"]) < 12)
        )
        if needs_sibling_search and res_dir.is_dir():
            fp = _code_fingerprint(repo, sha)
            if fp:
                best_score_key = (
                    1 if (best_perf and best_perf["profiler_cases"] and best_regime == "fast_single_session_l0") else 0,
                    1 if (best_perf and best_perf["profiler_cases"]) else 0,
                    1 if (best_perf and best_perf.get("runtime_soc") == target_soc) else 0,
                    len(best_perf["profiler_cases"]) if best_perf else 0,
                )
                # Only inspect passing `soc-a` logs (check tiny result.json first!)
                for cand_rj in sorted(res_dir.glob("*/result.json"), reverse=True):
                    c_sha = cand_rj.parent.name.lower()
                    cvlog = cand_rj.parent / "validation.log"
                    if c_sha == sha or len(c_sha) != 40 or not cvlog.is_file():
                        continue
                    try:
                        if json.loads(cand_rj.read_text()).get("exit_code") != 0:
                            continue
                    except Exception:
                        continue
                    if _code_fingerprint(repo, c_sha) != fp:
                        continue
                    ctxt = cvlog.read_text(errors="replace")
                    if target_soc and f"RUNTIME_SOC={target_soc}" not in ctxt:
                        continue
                    cp = parse_validation_perf(ctxt)
                    c_reg = self._detect_profiler_regime(cand_rj, ctxt)
                    cand_key = (
                        1 if (cp["profiler_cases"] and c_reg == "fast_single_session_l0") else 0,
                        1 if cp["profiler_cases"] else 0,
                        1 if cp.get("runtime_soc") == target_soc else 0,
                        len(cp["profiler_cases"]),
                    )
                    if best_perf is None or cand_key > best_score_key:
                        best_perf = cp
                        best_regime = c_reg
                        best_score_key = cand_key

        # If a newer fast_single_session_l0 B0 backfill exists for this baseline SHA and our
        # validation log is still legacy, prefer the fresh backfill timings so comparisons match regime!
        bf_regime = self._backfill_regime(backfill)
        if (
            allow_future_siblings
            and backfill
            and backfill.get("cases")
            and bf_regime == "fast_single_session_l0"
            and best_regime != "fast_single_session_l0"
        ):
            bf_raw = backfill["cases"] or {}
            bf_matched = (
                {b["id"]: float(bf_raw[b["id"]]) for b in buckets if b["id"] in bf_raw and bf_raw[b["id"]] and bf_raw[b["id"]] > 0}
                if buckets
                else {k: float(v) for k, v in bf_raw.items() if v and v > 0}
            )
            if bf_matched:
                bf_res: tuple[dict[str, float], dict[str, float], str | None, str] = (
                    bf_matched,
                    dict(best_perf.get("torch_cases") or {}) if best_perf else {},
                    backfill.get("runtime_soc") or target_soc,
                    "profiler",
                )
                self._timings_cache[t_key] = bf_res
                self._regime_cache[t_key] = bf_regime
                return bf_res

        if best_perf is None:
            # B0 backfill may have paired the baseline even when no `validation.log` exists at all
            if backfill and backfill.get("cases"):
                solo_res: tuple[dict[str, float], dict[str, float], str | None, str] = (
                    {k: float(v) for k, v in (backfill["cases"] or {}).items() if v and v > 0},
                    {},
                    backfill.get("runtime_soc"),
                    "profiler",
                )
                self._timings_cache[t_key] = solo_res
                self._regime_cache[t_key] = bf_regime
                return solo_res
            empty_res: tuple[dict[str, float], dict[str, float], str | None, str] = ({}, {}, None, "none")
            self._timings_cache[t_key] = empty_res
            self._regime_cache[t_key] = "none"
            return empty_res

        self._regime_cache[t_key] = best_regime

        soc = best_perf.get("runtime_soc")
        raw_pool: dict[str, float] = {}
        modality = "none"
        if best_perf["profiler_cases"]:
            raw_pool.update(best_perf["profiler_cases"])
            modality = "profiler"
        elif best_perf.get("has_hw_evidence"):
            # Hardware `profiler` ran but only reported kernel-level aggregates (`PROF_SUMMARY`),
            # which name no case: keep the modality (and therefore the homogeneity rule below)
            # on `profiler` and leave the pool empty.  Falling through to `exec=`/`dev_us` would
            # silently swap the measurement floor instead of reporting "no case-level evidence".
            modality = "profiler"
        elif best_perf["same_soc_exec_cases"]:
            host_ovh = 7.2 if soc == "soc-a" else 3.2
            for k, v in best_perf["same_soc_exec_cases"].items():
                raw_pool[k] = max(3.6, v - host_ovh)
            modality = "exec"
        elif best_perf["dev_event_cases"]:
            dev_ovh = 5.2 if soc == "soc-a" else 1.8
            for k, v in best_perf["dev_event_cases"].items():
                raw_pool[k] = max(3.6, v - dev_ovh)
            modality = "dev_us"

        # B0 backfill pairings only *fill holes* — a primary `validation.log` timing always
        # wins.  Hardware `profiler` Task Duration is pure device kernel time, so it is
        # epoch-robust (§3.2's `RUNTIME_SOC` caution is about host-enqueue floors);
        # `exec`/`dev_us`-derived backfill values DO carry that floor and stay same-epoch only.
        if backfill and backfill.get("cases"):
            bf_cases = backfill["cases"] or {}
            bf_soc = backfill.get("runtime_soc")
            bf_mod = backfill.get("modality") or "profiler"
            epoch_ok = (bf_mod == "profiler") or not bf_soc or not soc or bf_soc == soc
            if epoch_ok:
                for k, v in bf_cases.items():
                    if k in raw_pool or not isinstance(v, (int, float)) or v <= 0:
                        continue
                    raw_pool[k] = float(v)
                    if modality == "none":
                        modality = "profiler" if bf_mod == "profiler" else bf_mod
                        soc = soc or bf_soc

        matched: dict[str, float] = {}
        for b in buckets:
            bid = b["id"]
            if bid in raw_pool and raw_pool[bid] > 0:
                matched[bid] = float(raw_pool[bid])
        res_tup = (matched, dict(best_perf.get("torch_cases") or {}), soc, modality)
        self._timings_cache[t_key] = res_tup
        return res_tup

    def _compute_theta0(
        self,
        title: str,
        buckets: list[dict[str, Any]],
        observed_torch: dict[str, float],
        observed_min_tau: dict[str, float],
        ledger: ProblemLedger,
    ) -> list[float]:
        """Compute physical prior `theta_k^(0)`, respecting `tbest_k <= min_m tau_{m,k}`."""
        eta_p = max(0.45, min(0.88, (ledger.frontier_top1 / 100.0) * 0.85))
        theta0: list[float] = []
        for b in buckets:
            bid = b["id"]
            roof_us = float(b.get("roofline_us") or 3.5)
            t_ref = float(observed_torch.get(bid) or b.get("torch_ref_us") or roof_us)
            prior_t = max(2.20, min(roof_us, eta_p * t_ref) if bid in observed_min_tau else max(roof_us, eta_p * t_ref))
            if bid in observed_min_tau and observed_min_tau[bid] > 0:
                # Leaderboard axiom: site-wide tbest_k sdkot exceed our own fastest measured kernel!
                prior_t = min(prior_t, observed_min_tau[bid] * 0.98)
            theta0.append(round(max(1.80, prior_t), 4))
        return theta0

    @staticmethod
    def _build_phi_vector(
        buckets: list[dict[str, Any]],
        theta0: list[float],
        timings: dict[str, float],
        beta: float,
        fallback_r: float,
    ) -> list[float]:
        """Compute `Phi_k = 90 * min(1.0, beta * theta0_k / tau_k)` with stratum-mean imputation."""
        raw_r: dict[int, float] = {}
        by_strat: dict[str, list[float]] = {}
        for idx, b in enumerate(buckets):
            bid = b["id"]
            tau = timings.get(bid)
            if tau is not None and tau > 0:
                r_val = min(1.0, max(0.01, (beta * theta0[idx]) / tau))
                raw_r[idx] = r_val
                by_strat.setdefault(b["stratum"], []).append(r_val)

        all_obs = list(raw_r.values())
        global_mean_r = (sum(all_obs) / len(all_obs)) if all_obs else max(0.05, min(0.95, fallback_r))

        phi: list[float] = []
        for idx, b in enumerate(buckets):
            if idx in raw_r:
                r_k = raw_r[idx]
            else:
                s_vals = by_strat.get(b["stratum"])
                r_k = (sum(s_vals) / len(s_vals)) if s_vals else global_mean_r
            phi.append(90.0 * r_k)
        return phi

    def _fit_beta_scalar(
        self,
        buckets: list[dict[str, Any]],
        theta0: list[float],
        w0: list[float],
        anchor_timings: dict[str, float],
        target_hidden_pts: float,
    ) -> float:
        if not anchor_timings or target_hidden_pts <= 0.0:
            return 1.0
        fallback_r = max(0.05, min(0.95, target_hidden_pts / 90.0))
        lo, hi = 0.12, 3.00
        for _ in range(42):
            mid = 0.5 * (lo + hi)
            phi = self._build_phi_vector(buckets, theta0, anchor_timings, mid, fallback_r)
            pred = sum(wk * pk for wk, pk in zip(w0, phi))
            if pred < target_hidden_pts:
                lo = mid
            else:
                hi = mid
        return round(0.5 * (lo + hi), 5)

    def _fit_simplex_map(
        self,
        w0: list[float],
        caps: list[float],
        phi_rows: list[list[float]],
        y_targets: list[float],
        sample_weights: list[float],
        lambda_kl: float = 14.0,
        lambda_l2: float = 20.0,
        steps: int = 160,
    ) -> tuple[list[float], list[float]]:
        """Entropic Mirror Descent (Exponentiated Gradient) on upper-bounded simplex."""
        k_len = len(w0)
        if not phi_rows or k_len == 0:
            return list(w0), [0.002] * k_len

        w = project_capped_simplex(w0, caps)
        for step in range(steps):
            eta = 0.018 / math.sqrt(1.0 + 0.08 * step)
            grad = [0.0] * k_len
            for m_idx, (phi_m, y_m, sw_m) in enumerate(zip(phi_rows, y_targets, sample_weights)):
                pred_m = sum(wk * pk for wk, pk in zip(w, phi_m))
                g_m = sw_m * _huber_grad(pred_m - y_m)
                for k in range(k_len):
                    grad[k] += g_m * phi_m[k]

            for k in range(k_len):
                wk_safe = max(1e-6, w[k])
                w0_safe = max(1e-6, w0[k])
                grad[k] += lambda_kl * (math.log(wk_safe / w0_safe) + 1.0) + lambda_l2 * (w[k] - w0[k])

            # Center gradient for numerical stability before exponentiation
            g_mean = sum(grad) / k_len
            unnorm = [
                w[k] * math.exp(max(-6.0, min(6.0, -eta * (grad[k] - g_mean))))
                for k in range(k_len)
            ]
            w_next = project_capped_simplex(unnorm, caps)
            max_diff = max(abs(a - b) for a, b in zip(w_next, w))
            w = w_next
            if max_diff < 1e-6:
                break

        # Diagonal Laplace Hessian inverse `Sigma_{kk} = 1 / H_{kk}`
        diag_var: list[float] = []
        for k in range(k_len):
            h_kk = lambda_kl / max(1e-4, w[k]) + lambda_l2
            for phi_m, y_m, sw_m in zip(phi_rows, y_targets, sample_weights):
                pred_m = sum(wk * pk for wk, pk in zip(w, phi_m))
                if abs(pred_m - y_m) <= SIGMA_HIDDEN_JITTER * 1.5:
                    h_kk += sw_m * (phi_m[k] ** 2)
            diag_var.append(1.0 / max(1e-3, h_kk))

        return [round(x, 6) for x in w], diag_var

    def inspect_escape_state(
        self,
        title: str,
        as_of: datetime.datetime | None = None,
    ) -> dict[str, Any]:
        """Detect historical & active online escapes (`WA`/`TLE`/`RE` after local `EXTENDED_PASSED`)."""
        e_key = (title, as_of.isoformat() if as_of else "LIVE")
        if e_key in self._esc_cache:
            return self._esc_cache[e_key]
        rows = self.estimator._filtered_outcomes(title, as_of=as_of)
        live_rows = [r for r in rows if str(r.get("status", "")) not in ("rejected", "skipped", "duplicate")]

        escaped_commits: list[dict[str, Any]] = []
        seen_esc: set[str] = set()
        for r in live_rows:
            st = str(r.get("status", ""))
            csha = str(r.get("canonical_sha") or r.get("sha") or "")
            if st in ("Wrong Answer", "Time Limit Exceeded", "Runtime Error") and csha:
                vlog = self.workspace / "results" / title / csha / "validation.log"
                local_passed = vlog.is_file() and "EXTENDED_PASSED" in vlog.read_text(errors="replace")
                if local_passed and csha not in seen_esc:
                    seen_esc.add(csha)
                    ver_rec = self.verified_escapes.get(f"{title}:{csha}") or self.verified_escapes.get(f"{title}:{csha[:7]}")
                    escaped_commits.append({
                        "sha": csha,
                        "short_sha": csha[:7],
                        "online_status": st,
                        "at": r.get("submitted_at") or r.get("finished_at"),
                        "verified_regression": bool(ver_rec),
                        "verified_good_sha": ver_rec.get("good_sha") if ver_rec else None,
                    })

        # Escape lock is active ONLY when the latest online evaluation was an unverified escape!
        escape_lock = False
        escape_sha = None
        escape_status = None
        if live_rows:
            last = live_rows[-1]
            last_st = str(last.get("status", ""))
            last_sha = str(last.get("canonical_sha") or last.get("sha") or "")
            if last_st in ("Wrong Answer", "Time Limit Exceeded", "Runtime Error"):
                ver_rec = self.verified_escapes.get(f"{title}:{last_sha}") or self.verified_escapes.get(f"{title}:{last_sha[:7]}")
                if not ver_rec:
                    escape_lock = True
                    escape_sha = last_sha
                    escape_status = last_st

        esc_out = {
            "escape_lock": escape_lock,
            "escape_sha": escape_sha,
            "escape_short_sha": escape_sha[:7] if escape_sha else None,
            "escape_status": escape_status,
            "escaped_commits": escaped_commits,
        }
        self._esc_cache[e_key] = esc_out
        return esc_out

    def calibrate_problem(
        self,
        title: str,
        persist: bool = True,
        as_of: datetime.datetime | None = None,
    ) -> dict[str, Any]:
        """Fit full Simplex MAP calibration for `title` and optionally write `results/<title>/calibration.json`."""
        ledger = self.estimator.get_problem_ledger(title, as_of=as_of)
        perf_spec = self.load_perf_spec(title)
        buckets = list(perf_spec.get("hidden_buckets") or [])
        cov_grid = self.build_coverage_grid(title)
        esc_info = self.inspect_escape_state(title, as_of=as_of)

        ref_now = as_of or datetime.datetime.now(datetime.timezone.utc)
        anchor_ts = _parse_iso(ledger.active_judged_at) if ledger.current_score > 0 else ref_now
        if anchor_ts is None:
            anchor_ts = ref_now

        rows = self.estimator._filtered_outcomes(title, as_of=as_of)
        # Aggregate contemporary-equivalent scores per distinct passing SHA
        sha_draws: dict[str, list[float]] = {}
        sha_raw: dict[str, list[float]] = {}
        sha_order: list[str] = []
        for r in rows:
            if r.get("status") != "Pass" or not isinstance(r.get("score"), (int, float)) or r["score"] <= 0:
                continue
            csha = str(r.get("canonical_sha") or r.get("sha") or "")
            if not csha:
                continue
            ts = _parse_iso(r.get("submitted_at") or r.get("finished_at") or r.get("queued_at"))
            raw_s = float(r["score"])
            if as_of is None and csha == ledger.active_board_sha and ledger.current_score > 0:
                cnt_s = ledger.current_score
            else:
                cnt_s = self.estimator.discount_draw_to_contemporary(raw_s, ts, ledger.decay_factor_gamma, anchor_ts)
            if csha not in sha_draws:
                sha_order.append(csha)
            sha_draws.setdefault(csha, []).append(cnt_s)
            sha_raw.setdefault(csha, []).append(raw_s)

        if not sha_order and ledger.active_board_sha and ledger.current_score > 0:
            sha_order.append(ledger.active_board_sha)
            sha_draws[ledger.active_board_sha] = [ledger.current_score]
            sha_raw[ledger.active_board_sha] = [ledger.current_score]

        # Collect timings, modalities, min_tau, earliest_tau, & torch refs across passing SHAs + active SHA
        pooled_torch: dict[str, float] = {}
        observed_min_tau: dict[str, float] = {}
        earliest_tau: dict[str, float] = {}
        timings_by_sha: dict[str, dict[str, float]] = {}
        modality_by_sha: dict[str, str] = {}
        soc_by_sha: dict[str, str | None] = {}
        all_shas_to_scan = list(dict.fromkeys(sha_order + ([ledger.active_board_sha] if ledger.active_board_sha else [])))
        for csha in all_shas_to_scan:
            if not csha:
                continue
            b_tim, t_tim, soc_v, mod = self._extract_bucket_timings(
                title, csha, buckets, allow_future_siblings=(as_of is None)
            )
            timings_by_sha[csha] = b_tim
            modality_by_sha[csha] = mod
            soc_by_sha[csha] = soc_v
            pooled_torch.update(t_tim)
            for bk, tv in b_tim.items():
                if tv > 0:
                    earliest_tau.setdefault(bk, tv)
                    if bk not in observed_min_tau or tv < observed_min_tau[bk]:
                        observed_min_tau[bk] = tv

        active_sha = ledger.active_board_sha or (sha_order[-1] if sha_order else None)
        active_timings = timings_by_sha.get(active_sha or "", {})
        preferred_mod = modality_by_sha.get(active_sha or "", "none")
        if preferred_mod == "none":
            preferred_mod = (
                "profiler"
                if any(m == "profiler" for m in modality_by_sha.values())
                else next((m for m in modality_by_sha.values() if m != "none"), "none")
            )
        preferred_soc = soc_by_sha.get(active_sha or "")

        def _is_compatible_obs(csha: str) -> bool:
            if modality_by_sha.get(csha) != preferred_mod or not timings_by_sha.get(csha):
                return False
            # Host-enqueue / aclrtRecordEvent modalities (`exec`, `dev_us`) have different floors across SoCs!
            if preferred_mod in ("exec", "dev_us") and preferred_soc and soc_by_sha.get(csha) != preferred_soc:
                return False
            return True

        # Backfill buckets added in later benchmark revisions with their earliest observed baseline tau
        completed_timings_by_sha: dict[str, dict[str, float]] = {}
        for csha, t_map in timings_by_sha.items():
            if _is_compatible_obs(csha):
                merged_t = dict(earliest_tau)
                merged_t.update(t_map)
                completed_timings_by_sha[csha] = merged_t
            else:
                completed_timings_by_sha[csha] = dict(t_map)

        theta0 = self._compute_theta0(title, buckets, pooled_torch, observed_min_tau, ledger)
        w0 = [float(b.get("prior_w") or (1.0 / max(1, len(buckets)))) for b in buckets]
        caps = [float(b.get("max_w") or (0.04 if b.get("stratum") == "X_EXTREME" else 0.25)) for b in buckets]
        pub_ratio = (ledger.public_score / ledger.current_score) if ledger.current_score > 0 else 0.13

        def _solve_beta_for_weights(cur_w: list[float]) -> float:
            act_comp = completed_timings_by_sha.get(active_sha or "", {})
            if active_sha and act_comp and ledger.hidden_score > 0 and _is_compatible_obs(active_sha):
                return self._fit_beta_scalar(buckets, theta0, cur_w, act_comp, ledger.hidden_score)
            paired_betas: list[float] = []
            for csha in sha_order:
                if _is_compatible_obs(csha):
                    s_med = sorted(sha_draws[csha])[len(sha_draws[csha]) // 2]
                    y_h = max(0.0, s_med * (1.0 - pub_ratio))
                    paired_betas.append(self._fit_beta_scalar(buckets, theta0, cur_w, completed_timings_by_sha[csha], y_h))
            return round(sum(paired_betas) / len(paired_betas), 5) if paired_betas else 1.0

        # Joint 3-pass Block-Coordinate MAP update for `(beta_p, w_hat)` under homogeneous modality
        w_cur = project_capped_simplex(w0, caps) if w0 else []
        beta_p = _solve_beta_for_weights(w_cur)
        phi_rows: list[list[float]] = []
        y_targets: list[float] = []
        s_weights: list[float] = []
        paired_shas: list[str] = []
        diag_var = [0.0025] * len(w0)

        for _alt in range(3):
            phi_rows = []
            y_targets = []
            s_weights = []
            paired_shas = []
            for csha in sha_order:
                if not _is_compatible_obs(csha):
                    continue
                t_map = completed_timings_by_sha[csha]
                draws = sorted(sha_draws[csha])
                s_cnt = draws[len(draws) // 2]
                s_pub_est = ledger.public_score if csha == ledger.active_board_sha else min(9.5, round(s_cnt * pub_ratio, 2))
                y_hid = max(0.0, min(90.0, s_cnt - s_pub_est))
                phi_m = self._build_phi_vector(buckets, theta0, t_map, beta_p, ledger.implied_hidden_r or (y_hid / 90.0))
                phi_rows.append(phi_m)
                y_targets.append(y_hid)
                sw = min(3.0, float(len(draws))) * (1.8 if csha == active_sha else 1.0)
                s_weights.append(sw)
                paired_shas.append(csha)
            if len(phi_rows) >= 2:
                w_cur, diag_var = self._fit_simplex_map(w0, caps, phi_rows, y_targets, s_weights)
                beta_p = _solve_beta_for_weights(w_cur)
            else:
                break

        w_hat = w_cur

        # Evaluate historical observation ledger & residuals
        obs_ledger: list[dict[str, Any]] = []
        abs_errs: list[float] = []
        sq_errs: list[float] = []
        last_pred: float | None = None
        last_actual: float | None = None
        last_res: float | None = None

        for csha in sha_order:
            raw_t_map = timings_by_sha.get(csha, {})
            comp_t_map = completed_timings_by_sha.get(csha, {})
            same_mod = _is_compatible_obs(csha)
            draws = sorted(sha_draws[csha])
            s_cnt = round(draws[len(draws) // 2], 2)
            s_raw = round(sorted(sha_raw[csha])[len(sha_raw[csha]) // 2], 2)
            s_pub_est = ledger.public_score if csha == ledger.active_board_sha else min(9.5, round(s_cnt * pub_ratio, 2))
            if raw_t_map and same_mod and w_hat:
                phi_m = self._build_phi_vector(buckets, theta0, comp_t_map, beta_p, ledger.implied_hidden_r or 0.5)
                pred_sc = round(s_pub_est + sum(wk * pk for wk, pk in zip(w_hat, phi_m)), 2)
                res_pts = round(s_cnt - pred_sc, 2)
                abs_errs.append(abs(res_pts))
                sq_errs.append(res_pts * res_pts)
                last_pred = pred_sc
                last_actual = s_cnt
                last_res = res_pts
            else:
                pred_sc = s_cnt if (csha == ledger.active_board_sha and not raw_t_map) else None
                res_pts = 0.0 if pred_sc is not None else None
                if pred_sc is not None:
                    last_pred = pred_sc
                    last_actual = s_cnt
                    last_res = 0.0

            obs_ledger.append({
                "sha": csha,
                "short_sha": csha[:7],
                "modality": modality_by_sha.get(csha, "none"),
                "raw_score": s_raw,
                "contemporary_score": s_cnt,
                "predicted_score": pred_sc,
                "residual_pts": res_pts,
                "paired_buckets": len(raw_t_map),
                "total_buckets": len(buckets),
            })

        mae_pts = round(sum(abs_errs) / len(abs_errs), 2) if abs_errs else (0.0 if last_pred is not None else None)
        rmse_pts = round(math.sqrt(sum(sq_errs) / len(sq_errs)), 2) if sq_errs else mae_pts

        # Active bucket ROI & 5-stratum summary (`PUBLIC` + 4 hidden strata on 100-pt scale)
        active_phi = (
            self._build_phi_vector(buckets, theta0, active_timings, beta_p, ledger.implied_hidden_r)
            if buckets
            else []
        )
        unpaired_buckets = [b["id"] for b in buckets if b["id"] not in active_timings]

        buckets_out: list[dict[str, Any]] = []
        strat_prior_hid: dict[str, float] = {k: 0.0 for k in HIDDEN_STRATA_PRIORS}
        strat_learn_hid: dict[str, float] = {k: 0.0 for k in HIDDEN_STRATA_PRIORS}
        strat_pts_raw: dict[str, float] = {k: 0.0 for k in HIDDEN_STRATA_PRIORS}

        for idx, b in enumerate(buckets):
            st_name = b["stratum"]
            pw = w0[idx]
            lw = w_hat[idx]
            r_k = round(active_phi[idx] / 90.0, 4) if active_phi else ledger.implied_hidden_r
            cur_b_pts = round(90.0 * lw * r_k, 3)
            max_b_pts = round(90.0 * lw, 3)
            hr_b_pts = round(max(0.0, max_b_pts - cur_b_pts), 3)
            # Marginal gain from a 2.0x speedup on bucket k: 90 * lw * (min(1, 2*r_k) - r_k)
            gain_2x = round(90.0 * lw * max(0.0, min(1.0, 2.0 * r_k) - r_k), 3)

            strat_prior_hid[st_name] = strat_prior_hid.get(st_name, 0.0) + pw
            strat_learn_hid[st_name] = strat_learn_hid.get(st_name, 0.0) + lw
            strat_pts_raw[st_name] = strat_pts_raw.get(st_name, 0.0) + cur_b_pts

            buckets_out.append({
                "id": b["id"],
                "stratum": st_name,
                "dtype": b.get("dtype", "float32"),
                "description": b.get("description", ""),
                "theta0_us": theta0[idx],
                "tau_active_us": round(active_timings[b["id"]], 3) if b["id"] in active_timings else None,
                "observed": b["id"] in active_timings,
                "prior_w": round(pw, 5),
                "learned_w": round(lw, 5),
                "posterior_std_w": round(math.sqrt(diag_var[idx]), 5),
                "efficiency_r": round(r_k, 3),
                "current_pts": round(cur_b_pts, 2),
                "max_pts": round(max_b_pts, 2),
                "headroom_pts": round(hr_b_pts, 2),
                "marginal_pts_per_2x": round(gain_2x, 2),
            })

        # Scale hidden stratum current_pts gently if board is live so sum(strata.current_pts) == ledger.current_score
        sum_hid_model = sum(strat_pts_raw.values())
        hid_scale = (ledger.hidden_score / sum_hid_model) if (ledger.current_score > 0 and sum_hid_model > 1e-6) else (0.0 if ledger.current_score <= 0 else 1.0)

        strata_summary: list[dict[str, Any]] = []
        for s_key, s_label, default_pw100 in STRATA_ORDER:
            if s_key == "PUBLIC":
                pub_eff = round(min(1.0, ledger.public_score / 10.0), 3) if ledger.public_score > 0 else 0.0
                strata_summary.append({
                    "stratum": "PUBLIC",
                    "label": s_label,
                    "prior_w": 0.10,
                    "learned_w": 0.10,
                    "hidden_prior_w": 0.0,
                    "hidden_learned_w": 0.0,
                    "efficiency_r": pub_eff,
                    "current_pts": round(ledger.public_score, 2),
                    "headroom_pts": round(max(0.0, 10.0 - ledger.public_score), 2),
                })
            else:
                hpw = strat_prior_hid.get(s_key, HIDDEN_STRATA_PRIORS[s_key])
                hlw = strat_learn_hid.get(s_key, HIDDEN_STRATA_PRIORS[s_key])
                pw_100 = round(0.90 * hpw, 4)
                lw_100 = round(0.90 * hlw, 4)
                max_s_pts = 100.0 * lw_100
                cur_s_pts = round(min(max_s_pts, strat_pts_raw.get(s_key, 0.0) * hid_scale), 2)
                eff_s = round(cur_s_pts / max_s_pts, 3) if max_s_pts > 1e-6 else 0.0
                strata_summary.append({
                    "stratum": s_key,
                    "label": s_label,
                    "prior_w": pw_100,
                    "learned_w": lw_100,
                    "hidden_prior_w": round(hpw, 4),
                    "hidden_learned_w": round(hlw, 4),
                    "efficiency_r": eff_s,
                    "current_pts": cur_s_pts,
                    "headroom_pts": round(max(0.0, max_s_pts - cur_s_pts), 2),
                })

        # Three Closed-Loop Residual Diagnostic Triggers
        diagnostics: list[dict[str, Any]] = []
        if unpaired_buckets:
            missing_dtypes = sorted({
                b["dtype"] for b in buckets if b["id"] in unpaired_buckets and b["stratum"] == "M_MID_UB"
            })
            diagnostics.append({
                "code": "UNPAIRED_BUCKETS",
                "severity": "warning" if len(unpaired_buckets) >= 4 else "info",
                "message": (
                    f"{len(unpaired_buckets)}/{len(buckets)} hidden proxy buckets lack local timings "
                    f"(examples: {', '.join(unpaired_buckets[:4])})"
                    + (f"; missing M_MID_UB dtypes={missing_dtypes}" if missing_dtypes else "")
                ),
            })

        ext_pw = strat_prior_hid.get("X_EXTREME", 0.15)
        ext_lw = strat_learn_hid.get("X_EXTREME", 0.15)
        if ext_pw - ext_lw >= 0.015:
            diagnostics.append({
                "code": "PRUNE_PHANTOM_WEIGHT",
                "severity": "high",
                "message": (
                    f"Simplex MAP pruned X_EXTREME weight {ext_pw:.3f} -> {ext_lw:.3f} "
                    f"after online score gap showed synthetic tail speedups over-predicted hidden gains"
                ),
            })

        # Detect prologue/small-core regression across consecutive commits (e.g. ConcatD 899a2dc -> 2dd9acd)
        if len(paired_shas) >= 2:
            s_prev, s_last = paired_shas[-2], paired_shas[-1]
            t_prev, t_last = timings_by_sha[s_prev], timings_by_sha[s_last]
            small_regs = [
                b["id"]
                for b in buckets
                if b["stratum"] == "S_SMALL_CORE"
                and b["id"] in t_prev
                and b["id"] in t_last
                and t_last[b["id"]] > t_prev[b["id"]] * 1.06
            ]
            if small_regs and sha_draws[s_last][-1] < sha_draws[s_prev][-1] - 2.0:
                diagnostics.append({
                    "code": "PROLOGUE_TAX_ALERT",
                    "severity": "high",
                    "message": (
                        f"S_SMALL_CORE latency regressed on {small_regs} ({s_prev[:7]} -> {s_last[:7]}), "
                        f"driving {sha_draws[s_last][-1] - sha_draws[s_prev][-1]:+.2f} pt online drop via high-r harmonic derivative"
                    ),
                })

        if esc_info["escape_lock"]:
            diagnostics.append({
                "code": "ESCAPE_LOCK_ACTIVE",
                "severity": "critical",
                "message": (
                    f"Latest online submission {esc_info['escape_short_sha']} failed with {esc_info['escape_status']} "
                    f"after passing local S1! Add regression case & run verify-escape before new unproven submit."
                ),
            })
        elif esc_info["escaped_commits"]:
            diagnostics.append({
                "code": "ESCAPE_HISTORICAL",
                "severity": "info",
                "message": (
                    f"{len(esc_info['escaped_commits'])} historical online escape(s) recorded in D5 regression bank: "
                    + ", ".join(f"{e['short_sha']}({e['online_status']})" for e in esc_info["escaped_commits"])
                ),
            })

        buckets_ranked = sorted(buckets_out, key=lambda x: (x["headroom_pts"], x["marginal_pts_per_2x"]), reverse=True)

        active_regime = (
            self._regime_cache.get((title, active_sha, "soc-a" if as_of is None else None), "none")
            if active_sha
            else "none"
        )
        cal_doc = {
            "title": title,
            "updated_at": _utc_now(),
            "active_board_sha": active_sha,
            "active_profiler_regime": active_regime,
            "observations_m": len(sha_order),
            "paired_observations_m": len(phi_rows),
            "beta_tightness": beta_p,
            "decay_factor_gamma": ledger.decay_factor_gamma,
            "mae_pts": mae_pts,
            "rmse_pts": rmse_pts,
            "last_predicted_score": last_pred,
            "last_actual_score": last_actual,
            "last_residual_pts": last_res,
            "escape_lock": esc_info["escape_lock"],
            "escape_sha": esc_info["escape_short_sha"],
            "escape_full_sha": esc_info["escape_sha"],
            "escape_status": esc_info["escape_status"],
            "escaped_commits": esc_info["escaped_commits"],
            "unpaired_bucket_count": len(unpaired_buckets),
            "unpaired_buckets": unpaired_buckets,
            "diagnostics": diagnostics,
            "strata": strata_summary,
            "buckets": buckets_ranked,
            "posterior_diag_var": [round(v, 6) for v in diag_var],
            "theta0_us": theta0,
            "bucket_order": [b["id"] for b in buckets],
            "learned_weights_order": w_hat,
            "coverage_grid": cov_grid,
            "observation_ledger": obs_ledger,
        }

        if persist and as_of is None:
            out_file = self.workspace / "results" / title / "calibration.json"
            out_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = out_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(cal_doc, indent=2, ensure_ascii=False) + "\n")
            tmp.replace(out_file)

        return cal_doc

    def load_or_calibrate(self, title: str, as_of: datetime.datetime | None = None) -> dict[str, Any]:
        c_key = (title, as_of.isoformat() if as_of else "LIVE")
        if c_key in self._cal_cache:
            return self._cal_cache[c_key]
        if as_of is not None:
            # Causal key: depends only on number of outcomes strictly before `as_of`!
            n_prior = len(self.estimator._filtered_outcomes(title, as_of=as_of))
            n_key = (title, f"N={n_prior}")
            if n_key in self._cal_cache:
                self._cal_cache[c_key] = self._cal_cache[n_key]
                return self._cal_cache[n_key]
            doc = self.calibrate_problem(title, persist=False, as_of=as_of)
            self._cal_cache[n_key] = doc
            self._cal_cache[c_key] = doc
            return doc
        cal_file = self.workspace / "results" / title / "calibration.json"
        if cal_file.is_file():
            try:
                doc = json.loads(cal_file.read_text())
                esc = self.inspect_escape_state(title)
                doc["escape_lock"] = esc["escape_lock"]
                doc["escape_sha"] = esc["escape_short_sha"]
                doc["escape_full_sha"] = esc["escape_sha"]
                doc["escape_status"] = esc["escape_status"]
                doc["escaped_commits"] = esc["escaped_commits"]
                self._cal_cache[c_key] = doc
                return doc
            except Exception:
                pass
        doc = self.calibrate_problem(title, persist=True)
        self._cal_cache[c_key] = doc
        return doc

    def predict_candidate(
        self,
        title: str,
        cand_sha: str,
        base_sha: str | None,
        ledger: ProblemLedger,
        as_of: datetime.datetime | None = None,
        fast_cached_cal: bool = False,
    ) -> dict[str, Any] | None:
        """Predict candidate score & delta using calibrated Simplex MAP weights when bucket timings exist.

        Uses anchored differential prediction:
            S_hat(cand) = S_anchor + sum_k w_hat_k * (Phi_k(cand) - Phi_k(base)) + dS_pub
        so zero-change commits evaluate to `delta = 0.0` and harmonic saturation `min(1, beta*theta0/tau)`
        naturally bounds extreme-tail speedups while penalizing small-core regressions!
        """
        perf_spec = self.load_perf_spec(title)
        buckets = list(perf_spec.get("hidden_buckets") or [])
        if not buckets:
            return None

        cand_timings, cand_torch, cand_soc, cand_mod = self._extract_bucket_timings(
            title, cand_sha, buckets, allow_future_siblings=(as_of is None)
        )
        cand_regime = self._regime_cache.get((title, cand_sha, "soc-a" if as_of is None else None), "none")
        # Simplex-MAP bucket model operates strictly on hardware `profiler` Task Duration buckets!
        if not cand_timings or cand_mod != "profiler":
            return None

        base_timings: dict[str, float] = {}
        base_regime = "none"
        if base_sha:
            base_timings, base_torch, _, base_mod = self._extract_bucket_timings(
                title, base_sha, buckets, target_soc=cand_soc, allow_future_siblings=(as_of is None)
            )
            base_regime = self._regime_cache.get((title, base_sha, cand_soc if as_of is None else None), "none")
            if base_mod != "profiler":
                return None
        else:
            base_torch = {}

        regime_mismatch = bool(
            as_of is None
            and base_sha
            and cand_regime != "none"
            and base_regime != "none"
            and cand_regime != base_regime
        )

        common_buckets = [b["id"] for b in buckets if b["id"] in cand_timings and b["id"] in base_timings]
        if len(common_buckets) < 3:
            return None

        cal = self.load_or_calibrate(title, as_of=None if fast_cached_cal else as_of)
        beta_p = float(cal.get("beta_tightness") or 1.0)
        obs_min = {k: min(cand_timings[k], base_timings[k]) for k in common_buckets}
        theta0 = list(cal.get("theta0_us") or self._compute_theta0(title, buckets, {**base_torch, **cand_torch}, obs_min, ledger))
        w_hat = list(cal.get("learned_weights_order") or [float(b.get("prior_w", 1.0 / len(buckets))) for b in buckets])
        diag_var = list(cal.get("posterior_diag_var") or [0.0025] * len(buckets))

        if len(w_hat) != len(buckets) or len(theta0) != len(buckets):
            return None

        anchor_sc = ledger.current_score if ledger.current_score > 0 else (ledger.contemporary_best_score or 45.0)
        fallback_r = ledger.implied_hidden_r if ledger.implied_hidden_r > 0 else max(0.1, (anchor_sc * 0.87) / 90.0)

        phi_base = self._build_phi_vector(buckets, theta0, base_timings, beta_p, fallback_r)
        phi_cand = self._build_phi_vector(buckets, theta0, cand_timings, beta_p, fallback_r)

        delta_hid = 0.0
        epistemic_var = 0.0
        improved = 0
        regressed: list[str] = []

        for idx, b in enumerate(buckets):
            bid = b["id"]
            if bid in cand_timings and bid in base_timings:
                d_phi = phi_cand[idx] - phi_base[idx]
                delta_hid += w_hat[idx] * d_phi
                epistemic_var += (d_phi ** 2) * diag_var[idx]
                ratio = base_timings[bid] / max(1e-6, cand_timings[bid])
                if ratio > 1.03:
                    improved += 1
                elif ratio < 0.95:
                    regressed.append(f"{bid}({ratio:.2f}x)")

        # Public 10-pt shift tracks S_SMALL_CORE + M_MID_UB efficiency change (damped by 0.45)
        small_mid_idx = [
            i for i, b in enumerate(buckets)
            if b["stratum"] in ("S_SMALL_CORE", "M_MID_UB") and b["id"] in common_buckets
        ]
        if small_mid_idx:
            sm_w = sum(w_hat[i] for i in small_mid_idx) or 1.0
            sm_d_r = sum(w_hat[i] * (phi_cand[i] - phi_base[i]) / 90.0 for i in small_mid_idx) / sm_w
            delta_pub = 10.0 * 0.45 * sm_d_r
        else:
            delta_pub = (10.0 / 90.0) * 0.40 * delta_hid

        raw_delta = delta_hid + delta_pub
        # Guard against phantom speedups when comparing fast_single_session_l0 vs legacy_multiproc_pmu
        if regime_mismatch and raw_delta > 0:
            raw_delta *= 0.35
        # Bayesian shrinkage on large extrapolations when M_paired < 2 (single-anchor regime)
        m_paired = int(cal.get("paired_observations_m") or 0)
        if m_paired < 2 and abs(raw_delta) > 6.0:
            sign = 1.0 if raw_delta > 0 else -1.0
            raw_delta = sign * (6.0 + 0.55 * (abs(raw_delta) - 6.0))
        hard_cap = min(98.0, max(ledger.frontier_top1 + 3.0, anchor_sc + 30.0))
        est_score = max(0.0, min(hard_cap, anchor_sc + raw_delta))
        unpaired_frac = max(0.0, 1.0 - len(common_buckets) / max(1, len(buckets)))
        unpaired_sigma = 2.6 * unpaired_frac
        regime_sigma = 5.0 if regime_mismatch else 0.0
        total_sigma = math.sqrt(SIGMA_HIDDEN_JITTER ** 2 + epistemic_var + unpaired_sigma ** 2 + regime_sigma ** 2)
        eff_speedup = round(est_score / anchor_sc, 4) if anchor_sc > 0 else 1.0
        regime_note = (
            f" · [REGIME_MISMATCH: base={base_regime} vs cand={cand_regime}; run opsb bench backfill]"
            if regime_mismatch
            else ""
        )

        return {
            "estimated_score": round(est_score, 2),
            "sigma": round(total_sigma, 2),
            "effective_speedup": eff_speedup,
            "matched_cases": len(common_buckets),
            "improved_cases": improved,
            "regressed_cases": regressed,
            "regime_mismatch": regime_mismatch,
            "base_regime": base_regime,
            "cand_regime": cand_regime,
            "method": "simplex_map_calibrated",
            "detail": (
                f"Simplex-MAP ({len(common_buckets)}/{len(buckets)} buckets vs {(base_sha or '')[:7]}, "
                f"beta={beta_p:.3f}, d_hid={delta_hid:+.2f}, d_pub={delta_pub:+.2f}, "
                f"improved={improved}, regressed={len(regressed)}){regime_note}"
            ),
        }

    def diagnose_problem(self, title: str) -> dict[str, Any]:
        """Detailed ROI, phantom-weight, C++ slow-path scan, and escape report for `opsb bench diagnose`."""
        self._esc_cache.clear()
        self._cal_cache.clear()
        cal = self.calibrate_problem(title, persist=True)

        # Static scan of C++ kernel/tiling for uncovered slow/fallback branches
        repo = self.workspace / "bench" / title
        fallback_branches: list[str] = []
        for sub in ("op_host", "op_kernel"):
            sdir = repo / sub
            if not sdir.is_dir():
                continue
            for cf in sorted(sdir.rglob("*")):
                if cf.suffix not in (".h", ".cpp", ".cc", ".cxx"):
                    continue
                for lineno, ln in enumerate(cf.read_text(errors="replace").splitlines(), 1):
                    low = ln.lower()
                    if any(tok in low for tok in ("fallback", "scalar", "slow", "generic", "unaligned")):
                        if re.search(r"\b(if|else|case|void|bool)\b", low):
                            fallback_branches.append(f"{cf.relative_to(repo)}:{lineno}: {ln.strip()[:90]}")
                            if len(fallback_branches) >= 12:
                                break

        top_roi = [
            {
                "bucket": b["id"],
                "stratum": b["stratum"],
                "dtype": b["dtype"],
                "learned_w": b["learned_w"],
                "efficiency_r": b["efficiency_r"],
                "tau_active_us": b["tau_active_us"],
                "theta0_us": b["theta0_us"],
                "headroom_pts": b["headroom_pts"],
                "marginal_pts_per_2x": b["marginal_pts_per_2x"],
                "observed": b["observed"],
            }
            for b in cal["buckets"][:8]
        ]

        return {
            "title": title,
            "updated_at": cal["updated_at"],
            "beta_tightness": cal["beta_tightness"],
            "mae_pts": cal["mae_pts"],
            "rmse_pts": cal["rmse_pts"],
            "escape_lock": cal["escape_lock"],
            "escape_sha": cal["escape_sha"],
            "unpaired_bucket_count": cal["unpaired_bucket_count"],
            "unpaired_buckets": cal["unpaired_buckets"],
            "diagnostics": cal["diagnostics"],
            "strata": cal["strata"],
            "top_roi_buckets": top_roi,
            "detected_cpp_branch_guards": fallback_branches[:10],
            "observation_ledger": cal["observation_ledger"],
        }

    def verify_escape(
        self,
        title: str,
        bad_sha: str,
        good_sha: str,
    ) -> dict[str, Any]:
        """Verify that a regression test separates `bad_sha` from passing `good_sha`, clearing `ESCAPE_LOCK`."""
        c_bad = resolve_sha(self.workspace, title, bad_sha)
        c_good = resolve_sha(self.workspace, title, good_sha)
        if not re.fullmatch(r"[0-9a-f]{40}", c_bad):
            raise ValueError(f"Could not resolve bad SHA: {bad_sha}")
        if not re.fullmatch(r"[0-9a-f]{40}", c_good):
            raise ValueError(f"Could not resolve good SHA: {good_sha}")
        if c_bad == c_good:
            raise ValueError("--bad-sha and --good-sha must be distinct commits")

        # Require `good_sha` to have a clean passing validation (`result.json` exit_code == 0 & EXTENDED_PASSED)
        good_res = self.workspace / "results" / title / c_good / "result.json"
        good_log = self.workspace / "results" / title / c_good / "validation.log"
        if not good_res.is_file() or not good_log.is_file():
            return {
                "ok": False,
                "cleared": False,
                "title": title,
                "bad_sha": c_bad,
                "good_sha": c_good,
                "reason": f"Good commit {c_good[:7]} has no server validation record in results/{title}/{c_good[:7]}/",
            }
        g_rc = json.loads(good_res.read_text()).get("exit_code")
        g_txt = good_log.read_text(errors="replace")
        if g_rc != 0 or "EXTENDED_PASSED" not in g_txt or "FAST_LANE=1" in g_txt:
            return {
                "ok": False,
                "cleared": False,
                "title": title,
                "bad_sha": c_bad,
                "good_sha": c_good,
                "reason": f"Good commit {c_good[:7]} did not pass full Q1 server validation (exit_code={g_rc})",
            }

        # Check that either validation/ or bench-specs/ grew or references the escape fix vs bad_sha
        repo = self.workspace / "bench" / title
        diff_files: list[str] = []
        if (repo / ".git").exists():
            dp = self.estimator  # check git diff between bad_sha and good_sha
            import subprocess
            proc = subprocess.run(
                ["git", "-C", str(repo), "diff", "--name-only", c_bad, c_good],
                capture_output=True,
                text=True,
            )
            if proc.returncode == 0:
                diff_files = [x.strip() for x in proc.stdout.splitlines() if x.strip()]

        has_validation_update = any(
            f.startswith(("validation/", "bench/")) for f in diff_files
        )
        corr_path, _ = self._spec_paths(title)
        spec_mentions_bad = False
        if corr_path.is_file():
            spec_mentions_bad = c_bad[:7] in corr_path.read_text(errors="replace")

        if not (has_validation_update or spec_mentions_bad):
            return {
                "ok": False,
                "cleared": False,
                "title": title,
                "bad_sha": c_bad,
                "good_sha": c_good,
                "diff_files": diff_files,
                "reason": (
                    f"Neither {title}/validation/* nor bench/cases_correctness.json was updated with a "
                    f"regression reproducer between {c_bad[:7]} and {c_good[:7]}"
                ),
            }

        rec = {
            "bad_sha": c_bad,
            "good_sha": c_good,
            "verified_at": _utc_now(),
            "diff_files": diff_files,
        }
        self.verified_escapes[f"{title}:{c_bad}"] = rec
        self._esc_cache.clear()
        self._cal_cache.clear()
        cal = self.calibrate_problem(title, persist=True)
        return {
            "ok": True,
            "cleared": not cal["escape_lock"],
            "title": title,
            "bad_sha": c_bad,
            "good_sha": c_good,
            "diff_files": diff_files,
            "escape_lock": cal["escape_lock"],
        }
