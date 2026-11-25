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
import statistics
from typing import Any

from .bench_evolution import (
    MODEL_VERSION,
    append_buckets_to_spec,
    apply_proxy_aliases,
    build_evolution_plan,
    classify_residual_causes,
    compute_loso_cv,
    compute_robust_timings,
    fit_pairwise_simplex_map,
    infer_bucket_metadata,
)
from .pairwise_anchor import evaluate_pairwise_anchor, select_dual_anchor_prediction
from .estimator import (
    SIGMA_HIDDEN_JITTER,
    ProblemLedger,
    ScoreEstimator,
    _code_fingerprint,
    _parse_iso,
    parse_validation_perf,
    resolve_sha,
)
from .parhelion_paths import atomic_write_json, read_json_safe, workspace_bench_specs_dir

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
            pathlib.Path(specs_root)
            if specs_root is not None
            else workspace_bench_specs_dir(self.workspace)
        )
        self.verified_escapes = verified_escapes if verified_escapes is not None else {}
        # B0 backfill lane store (`<state>/backfill_timings.json`); built lazily on first use
        self.state_dir = pathlib.Path(state_dir) if state_dir else None
        self.backfill: Any = None
        self._perf_spec_cache: dict[str, dict[str, Any]] = {}
        self._timings_cache: dict[tuple[str, str, str | None], tuple[dict[str, float], dict[str, float], str | None, str]] = {}
        self._full_tel_cache: dict[tuple[str, str, str | None, bool], dict[str, Any]] = {}
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

    def _extract_full_telemetry(
        self,
        title: str,
        sha: str,
        target_soc: str | None = "soc-a",
        allow_future_siblings: bool = True,
    ) -> dict[str, Any]:
        """Extract ALL logged `profiler` buckets (spec + extra), spike-immune `robust_cases`,
        `min_cases`/`max_cases`, `block_dims`, compiled `.o` `kernel_binary_stats`,
        `cold_start_stats`, `regime`, `soc`, and `modality`.
        """
        cache_key = (title, sha, target_soc if allow_future_siblings else None, allow_future_siblings)
        if cache_key in self._full_tel_cache:
            return self._full_tel_cache[cache_key]

        repo = self.workspace / "bench" / title
        res_dir = self.workspace / "results" / title
        vlog = res_dir / sha / "validation.log"
        rj_path = res_dir / sha / "result.json"
        backfill = self._backfill_pairings(title, sha)

        rj_data: dict[str, Any] = {}
        if rj_path.is_file():
            try:
                rj_data = json.loads(rj_path.read_text())
            except Exception:
                rj_data = {}

        vlog_text = vlog.read_text(errors="replace") if vlog.is_file() else ""
        best_perf = parse_validation_perf(vlog_text) if vlog_text else None
        best_regime = self._detect_profiler_regime(rj_path, vlog_text) if best_perf is not None else "none"
        best_rj = rj_data

        needs_sibling = allow_future_siblings and (
            best_perf is None
            or best_regime != "fast_single_session_l0"
            or (best_perf.get("runtime_soc") != target_soc and not best_perf["profiler_cases"])
            or (title == "ConcatD" and len(best_perf["profiler_cases"]) < 12)
        )
        if needs_sibling and res_dir.is_dir():
            fp = _code_fingerprint(repo, sha)
            if fp:
                best_key = (
                    1 if (best_perf and best_perf["profiler_cases"] and best_regime == "fast_single_session_l0") else 0,
                    1 if (best_perf and best_perf["profiler_cases"]) else 0,
                    1 if (best_perf and best_perf.get("runtime_soc") == target_soc) else 0,
                    len(best_perf["profiler_cases"]) if best_perf else 0,
                )
                for cand_rj in sorted(res_dir.glob("*/result.json"), reverse=True):
                    c_sha = cand_rj.parent.name.lower()
                    cvlog = cand_rj.parent / "validation.log"
                    if c_sha == sha or len(c_sha) != 40 or not cvlog.is_file():
                        continue
                    try:
                        c_rj_d = json.loads(cand_rj.read_text())
                        if c_rj_d.get("exit_code") != 0:
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
                    if best_perf is None or cand_key > best_key:
                        best_perf = cp
                        best_regime = c_reg
                        best_rj = c_rj_d
                        best_key = cand_key

        bf_regime = self._backfill_regime(backfill)
        spec_ids = [b["id"] for b in (self.load_perf_spec(title).get("hidden_buckets") or []) if isinstance(b, dict) and b.get("id")]

        # Prefer newer fast_single_session_l0 backfill when live validation log is still legacy
        if (
            allow_future_siblings
            and backfill
            and backfill.get("cases")
            and bf_regime == "fast_single_session_l0"
            and best_regime != "fast_single_session_l0"
        ):
            bf_cases = {
                k: float(v)
                for k, v in (backfill["cases"] or {}).items()
                if isinstance(v, (int, float)) and v > 0 and ":" not in k and not k.startswith("__")
            }
            bf_cases = apply_proxy_aliases(bf_cases, spec_ids)
            if bf_cases:
                tel = {
                    "raw_cases": bf_cases,
                    "min_cases": {},
                    "max_cases": {},
                    "robust_cases": dict(bf_cases),
                    "torch_cases": dict(best_perf.get("torch_cases") or {}) if best_perf else {},
                    "block_dims": {},
                    "kernel_binary_stats": {},
                    "cold_start_stats": {},
                    "soc": backfill.get("runtime_soc") or target_soc,
                    "modality": "profiler",
                    "regime": bf_regime,
                }
                self._full_tel_cache[cache_key] = tel
                return tel

        raw_pool: dict[str, float] = {}
        min_pool: dict[str, float] = {}
        max_pool: dict[str, float] = {}
        blk_pool: dict[str, int] = {}
        torch_pool: dict[str, float] = {}
        bin_stats: dict[str, Any] = {}
        cold_stats: dict[str, Any] = {}
        soc: str | None = None
        modality = "none"

        if best_perf is not None:
            soc = best_perf.get("runtime_soc")
            torch_pool = dict(best_perf.get("torch_cases") or {})
            bin_stats = dict(best_perf.get("kernel_binary_stats") or {})
            cold_stats = dict(best_perf.get("cold_start_stats") or {})
            blk_pool = dict(best_perf.get("profiler_block_dims") or {})
            if best_perf["profiler_cases"]:
                raw_pool.update(best_perf["profiler_cases"])
                min_pool.update(best_perf.get("profiler_min_cases") or {})
                max_pool.update(best_perf.get("profiler_max_cases") or {})
                modality = "profiler"
            elif best_perf.get("has_hw_evidence"):
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

        # Backfill fills holes only
        if backfill and backfill.get("cases"):
            bf_cases = backfill["cases"] or {}
            bf_soc = backfill.get("runtime_soc")
            bf_mod = backfill.get("modality") or "profiler"
            epoch_ok = (bf_mod == "profiler") or not bf_soc or not soc or bf_soc == soc
            if epoch_ok:
                for k, v in bf_cases.items():
                    if k in raw_pool or not isinstance(v, (int, float)) or v <= 0 or ":" in k or k.startswith("__"):
                        continue
                    raw_pool[k] = float(v)
                    if modality == "none":
                        modality = "profiler" if bf_mod == "profiler" else bf_mod
                        soc = soc or bf_soc
                        if best_regime == "none":
                            best_regime = bf_regime

        # Supplement binary/cold stats from result.json if absent from log
        if not bin_stats.get("max_bytes") and isinstance(best_rj.get("kernel_o_max_bytes"), (int, float)) and best_rj["kernel_o_max_bytes"] > 0:
            bin_stats = {
                "count": int(best_rj.get("kernel_o_count") or 1),
                "max_bytes": int(best_rj["kernel_o_max_bytes"]),
                "sum_bytes": int(best_rj.get("kernel_o_sum_bytes") or best_rj["kernel_o_max_bytes"]),
                "max_text_bytes": int(best_rj.get("kernel_o_max_text_bytes") or 0),
            }
        if not cold_stats.get("first_iter_us") and isinstance(best_rj.get("first_iter_us"), (int, float)):
            cold_stats["first_iter_us"] = float(best_rj["first_iter_us"])
        if not cold_stats.get("cold_spread_median_us") and isinstance(best_rj.get("cold_spread_median_us"), (int, float)):
            cold_stats["cold_spread_median_us"] = float(best_rj["cold_spread_median_us"])
        elif not cold_stats.get("cold_spread_median_us") and max_pool and min_pool:
            spreads = [max_pool[k] - min_pool[k] for k in max_pool if k in min_pool and max_pool[k] >= min_pool[k]]
            if spreads:
                cold_stats["cold_spread_median_us"] = round(statistics.median(spreads), 3)

        raw_aliased = apply_proxy_aliases(raw_pool, spec_ids)
        min_aliased = apply_proxy_aliases(min_pool, spec_ids)
        robust = compute_robust_timings(raw_aliased, min_aliased)

        tel = {
            "raw_cases": raw_aliased,
            "min_cases": min_aliased,
            "max_cases": max_pool,
            "robust_cases": robust,
            "torch_cases": torch_pool,
            "block_dims": blk_pool,
            "kernel_binary_stats": bin_stats,
            "cold_start_stats": cold_stats,
            "soc": soc,
            "modality": modality,
            "regime": best_regime,
        }
        self._full_tel_cache[cache_key] = tel
        return tel

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
        def submitted_epoch(row):
            date = _parse_iso(row.get("submitted_at") or row.get("finished_at") or row.get("queued_at"))
            return date.timestamp() if date else 0.0
        live_rows.sort(key=submitted_epoch)

        escaped_commits: list[dict[str, Any]] = []
        seen_esc: set[str] = set()
        for r in live_rows:
            st = str(r.get("status", ""))
            csha = str(r.get("canonical_sha") or r.get("sha") or "")
            if st in ("Wrong Answer", "Time Limit Exceeded", "Runtime Error") and csha:
                vlog = self.workspace / "results" / title / csha / "validation.log"
                local_passed = vlog.is_file() and "EXTENDED_PASSED" in vlog.read_text(errors="replace")
                if not local_passed:
                    from .case_bench import read_doc
                    record = read_doc(vlog.parent/"result.json")
                    local_passed = record.get("layout")=="template" and record.get("verified") is True
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

        # Escape lock stays active after an unverified online WA/TLE/RE even if an OLD
        # already-passing SHA is rolled back / restored to recover board score. Only a
        # `verify-escape` entry OR a genuinely NEW non-restore passing SHA disarms it!
        prior_pass_before: list[set[str]] = []
        acc_pass: set[str] = set()
        for r in live_rows:
            prior_pass_before.append(set(acc_pass))
            st = str(r.get("status", ""))
            csha = str(r.get("canonical_sha") or r.get("sha") or "")
            if st == "Pass" and csha and isinstance(r.get("score"), (int, float)) and r["score"] > 0:
                acc_pass.add(csha)

        escape_lock = False
        escape_sha = None
        escape_status = None
        escape_row = {}
        for idx in range(len(live_rows) - 1, -1, -1):
            r = live_rows[idx]
            st = str(r.get("status", ""))
            csha = str(r.get("canonical_sha") or r.get("sha") or "")
            note_low = str(r.get("note") or "").strip().lower()
            is_restore_row = (csha in prior_pass_before[idx]) or any(
                note_low.startswith(pfx) for pfx in ("restore", "rollback", "board is 0", "closing restore")
            )
            if st == "Pass":
                if is_restore_row:
                    # Restoring a historical passing SHA recovers board score, NOT the escaped bug!
                    continue
                break
            if st in ("Wrong Answer", "Time Limit Exceeded", "Runtime Error"):
                ver_rec = self.verified_escapes.get(f"{title}:{csha}") or self.verified_escapes.get(f"{title}:{csha[:7]}")
                sid = r.get("submission_id")
                if not ver_rec and sid:
                    ver_rec = next((rec for key, rec in self.verified_escapes.items()
                                    if key.startswith(f"{title}:") and sid in rec.get("submission_ids", [])), None)
                if not ver_rec:
                    escape_lock = True
                    escape_sha = csha
                    escape_status = st
                    escape_row = r
                break

        esc_out = {
            "escape_lock": escape_lock,
            "escape_sha": escape_sha,
            "escape_short_sha": escape_sha[:7] if escape_sha else None,
            "escape_status": escape_status,
            "escape_submission_id": escape_row.get("submission_id"),
            "escape_account_id": escape_row.get("account_id"),
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
        """Fit Pairwise Correlation-Adaptive Simplex-MAP v2.0 calibration & LOSO-CV reliability."""
        ledger = self.estimator.get_problem_ledger(title, as_of=as_of)
        perf_spec = self.load_perf_spec(title)
        spec_buckets = list(perf_spec.get("hidden_buckets") or [])
        spec_ids = [b["id"] for b in spec_buckets if isinstance(b, dict) and b.get("id")]
        spec_id_set = set(spec_ids)
        cov_grid = self.build_coverage_grid(title)
        esc_info = self.inspect_escape_state(title, as_of=as_of)

        ref_now = as_of or datetime.datetime.now(datetime.timezone.utc)
        anchor_ts = _parse_iso(ledger.active_judged_at) if ledger.current_score > 0 else ref_now
        if anchor_ts is None:
            anchor_ts = ref_now

        rows = self.estimator._filtered_outcomes(title, as_of=as_of)
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

        sha_scores: dict[str, float] = {
            s: float(statistics.median(sha_draws[s])) for s in sha_order
        }

        # Collect FULL telemetry (spec + auto-discovered extra profiler buckets + binary/cold stats)
        pooled_torch: dict[str, float] = {}
        observed_min_tau: dict[str, float] = {}
        observed_samples: dict[str, list[float]] = {}
        tel_by_sha: dict[str, dict[str, Any]] = {}
        robust_by_sha: dict[str, dict[str, float]] = {}
        modality_by_sha: dict[str, str] = {}
        soc_by_sha: dict[str, str | None] = {}
        regime_by_sha: dict[str, str] = {}
        bin_bytes_by_sha: dict[str, int | None] = {}

        all_shas_to_scan = list(dict.fromkeys(sha_order + ([ledger.active_board_sha] if ledger.active_board_sha else [])))
        for csha in all_shas_to_scan:
            if not csha:
                continue
            tel = self._extract_full_telemetry(title, csha, allow_future_siblings=(as_of is None))
            tel_by_sha[csha] = tel
            rob = tel["robust_cases"]
            robust_by_sha[csha] = rob
            modality_by_sha[csha] = tel["modality"]
            soc_by_sha[csha] = tel["soc"]
            regime_by_sha[csha] = tel["regime"]
            b_max = (tel.get("kernel_binary_stats") or {}).get("max_bytes")
            bin_bytes_by_sha[csha] = int(b_max) if isinstance(b_max, (int, float)) and b_max > 0 else None
            pooled_torch.update(tel["torch_cases"])
            for bk, tv in rob.items():
                if tv > 0:
                    observed_samples.setdefault(bk, []).append(tv)
                    if bk not in observed_min_tau or tv < observed_min_tau[bk]:
                        observed_min_tau[bk] = tv

        # Build unified bucket list: all spec buckets first (append-only order), then auto-discovered extras
        extra_ids = sorted(
            bk for bk in observed_samples
            if bk not in spec_id_set and ":" not in bk and not bk.startswith("__")
        )
        effective_buckets: list[dict[str, Any]] = list(spec_buckets)
        for eid in extra_ids:
            med_v = float(statistics.median(observed_samples[eid]))
            effective_buckets.append(infer_bucket_metadata(eid, med_v))

        bucket_ids = [b["id"] for b in effective_buckets]
        strata_map = {b["id"]: str(b.get("stratum") or "M_MID_UB") for b in effective_buckets}
        spec_priors = {
            b["id"]: float(b.get("prior_w") or (1.0 / max(1, len(effective_buckets))))
            for b in effective_buckets
        }

        active_sha = ledger.active_board_sha or (sha_order[-1] if sha_order else None)
        active_tel = tel_by_sha.get(active_sha or "", {})
        active_timings = robust_by_sha.get(active_sha or "", {})
        preferred_mod = modality_by_sha.get(active_sha or "", "none")
        if preferred_mod == "none":
            preferred_mod = (
                "profiler"
                if any(m == "profiler" for m in modality_by_sha.values())
                else next((m for m in modality_by_sha.values() if m != "none"), "none")
            )
        preferred_soc = soc_by_sha.get(active_sha or "")
        preferred_regime = regime_by_sha.get(active_sha or "", "none")
        if preferred_regime == "none":
            preferred_regime = (
                "fast_single_session_l0"
                if any(r == "fast_single_session_l0" for r in regime_by_sha.values())
                else next((r for r in regime_by_sha.values() if r != "none"), "none")
            )

        # Build strictly same-(modality, soc, regime) pairwise observations (i < j)
        pairs: list[dict[str, Any]] = []
        paired_shas_set: set[str] = set()
        for i in range(len(sha_order)):
            for j in range(i + 1, len(sha_order)):
                sa, sb = sha_order[i], sha_order[j]
                if modality_by_sha.get(sa) != preferred_mod or modality_by_sha.get(sb) != preferred_mod:
                    continue
                if regime_by_sha.get(sa) != regime_by_sha.get(sb) or regime_by_sha.get(sa) == "none":
                    continue
                if soc_by_sha.get(sa) and soc_by_sha.get(sb) and soc_by_sha[sa] != soc_by_sha[sb]:
                    continue
                ta, tb = robust_by_sha.get(sa, {}), robust_by_sha.get(sb, {})
                if len(set(ta) & set(tb)) < 3:
                    continue
                ra = max(0.20, min(0.88, (sha_scores[sa] * 0.87) / 90.0))
                sw = (1.5 if regime_by_sha[sa] == preferred_regime else 0.5) * (1.4 if j == i + 1 else 0.7)
                ba, bb = bin_bytes_by_sha.get(sa), bin_bytes_by_sha.get(sb)
                dlb = math.log(bb / ba) if (ba and bb and ba > 0 and bb > 0) else None
                pairs.append({
                    "sa": sa,
                    "sb": sb,
                    "t_a": ta,
                    "t_b": tb,
                    "dy": sha_scores[sb] - sha_scores[sa],
                    "r_a": ra,
                    "sw": sw,
                    "d_log_bytes": dlb,
                })
                paired_shas_set.add(sa)
                paired_shas_set.add(sb)

        paired_shas = [s for s in sha_order if s in paired_shas_set]

        # Fit v2 Pairwise Correlation-Adaptive Simplex-MAP + Transfer Gain + Cold-Start slope
        fit = fit_pairwise_simplex_map(
            bucket_ids=bucket_ids,
            strata=strata_map,
            spec_priors=spec_priors,
            pairs=pairs,
            min_tau=observed_min_tau,
        )
        w0 = fit["w0"]
        w_hat = fit["w_hat"]
        diag_var = fit["diag_var"]
        transfer_gamma = float(fit["transfer_gamma"])
        alpha_cold = float(fit["alpha_cold"])
        b_corr = fit["bucket_correlations"]
        b_npairs = fit["bucket_pair_counts"]

        # True Leave-One-SHA-Out Cross-Validation for honest out-of-sample reliability
        cv = compute_loso_cv(
            bucket_ids=bucket_ids,
            strata=strata_map,
            spec_priors=spec_priors,
            sha_order=sha_order,
            sha_scores=sha_scores,
            sha_timings=robust_by_sha,
            sha_regimes=regime_by_sha,
            sha_socs=soc_by_sha,
            sha_bin_bytes=bin_bytes_by_sha,
            min_tau=observed_min_tau,
        )

        # Anchor-calibrated `theta0` & sane bounded `beta_p` (for display & backward compatibility)
        theta0 = self._compute_theta0(title, effective_buckets, pooled_torch, observed_min_tau, ledger)
        # Keep beta_p bounded strictly away from dead-zone saturation!
        raw_beta = (
            self._fit_beta_scalar(effective_buckets, theta0, w_hat, active_timings, ledger.hidden_score)
            if (active_timings and ledger.hidden_score > 0 and w_hat)
            else 1.0
        )
        beta_p = round(max(0.30, min(1.35, raw_beta)), 5)

        # Build observation ledger + High-Residual & Sign-Flip event audit across consecutive Pass SHAs
        obs_ledger: list[dict[str, Any]] = []
        high_residual_events: list[dict[str, Any]] = []
        cv_by_cand = {row["cand_sha"]: row for row in (cv.get("cv_transitions") or [])}
        abs_errs: list[float] = []
        sq_errs: list[float] = []
        last_pred: float | None = None
        last_actual: float | None = None
        last_res: float | None = None

        for idx_s, csha in enumerate(sha_order):
            raw_t_map = robust_by_sha.get(csha, {})
            s_cnt = round(sha_scores[csha], 2)
            s_raw = round(float(statistics.median(sha_raw[csha])), 2)
            cv_row = cv_by_cand.get(csha)
            pred_sc: float | None = None
            res_pts: float | None = None

            if cv_row is not None and idx_s > 0:
                prev_sha = cv_row["base_sha"]
                prev_sc = round(sha_scores[prev_sha], 2)
                pred_d = float(cv_row["cv_pred_delta"])
                act_d = float(cv_row["actual_delta"])
                pred_sc = round(max(0.0, min(98.0, prev_sc + pred_d)), 2)
                res_pts = round(s_cnt - pred_sc, 2)
                abs_errs.append(abs(res_pts))
                sq_errs.append(res_pts * res_pts)
                last_pred = pred_sc
                last_actual = s_cnt
                last_res = res_pts

                is_sign_flip = (act_d >= 2.0 and pred_d <= -1.0) or (act_d <= -2.0 and pred_d >= 1.0)
                if abs(res_pts) >= 3.0 or is_sign_flip:
                    causes = classify_residual_causes(
                        title=title,
                        base_tel=tel_by_sha.get(prev_sha, {}),
                        cand_tel=tel_by_sha.get(csha, {}),
                        spec_bucket_ids=spec_ids,
                        bucket_correlations=b_corr,
                        actual_delta=act_d,
                        pred_delta=pred_d,
                        thermal_drift_us=float(cv_row.get("thermal_drift_us", 0.0)),
                    )
                    high_residual_events.append({
                        "base_sha": prev_sha[:7],
                        "cand_sha": csha[:7],
                        "cand_full_sha": csha,
                        "actual_delta": round(act_d, 2),
                        "pred_delta": round(pred_d, 2),
                        "residual_eps": round(res_pts, 2),
                        "sign_flip": is_sign_flip,
                        "thermal_drift_us": cv_row.get("thermal_drift_us", 0.0),
                        "causes": causes,
                    })
            elif csha == active_sha:
                pred_sc = s_cnt
                res_pts = 0.0
                if last_pred is None:
                    last_pred = pred_sc
                    last_actual = s_cnt
                    last_res = 0.0

            obs_ledger.append({
                "sha": csha,
                "short_sha": csha[:7],
                "modality": modality_by_sha.get(csha, "none"),
                "regime": regime_by_sha.get(csha, "none"),
                "raw_score": s_raw,
                "contemporary_score": s_cnt,
                "predicted_score": pred_sc,
                "residual_pts": res_pts,
                "paired_buckets": len(raw_t_map),
                "total_buckets": len(effective_buckets),
                "kernel_o_max_bytes": bin_bytes_by_sha.get(csha),
            })

        mae_pts = cv["loocv_mae_pts"] if cv["loocv_mae_pts"] is not None else (
            round(sum(abs_errs) / len(abs_errs), 2) if abs_errs else (0.0 if last_pred is not None else None)
        )
        rmse_pts = cv["loocv_rmse_pts"] if cv["loocv_rmse_pts"] is not None else (
            round(math.sqrt(sum(sq_errs) / len(sq_errs)), 2) if sq_errs else mae_pts
        )

        # Spec-level unpaired census (preserves exact B0 backfill contract!)
        exact_active_timings: dict[str, float] = {}
        if active_sha and spec_buckets:
            exact_active_timings, _, _, _ = self._extract_bucket_timings(
                title, active_sha, spec_buckets, allow_future_siblings=(as_of is None)
            )
        unpaired_buckets = [b["id"] for b in spec_buckets if b["id"] not in exact_active_timings]

        # Active bucket ROI & 5-stratum summary
        active_phi = (
            self._build_phi_vector(effective_buckets, theta0, active_timings, beta_p, ledger.implied_hidden_r)
            if effective_buckets
            else []
        )
        buckets_out: list[dict[str, Any]] = []
        strat_prior_hid: dict[str, float] = {k: 0.0 for k in HIDDEN_STRATA_PRIORS}
        strat_learn_hid: dict[str, float] = {k: 0.0 for k in HIDDEN_STRATA_PRIORS}
        strat_pts_raw: dict[str, float] = {k: 0.0 for k in HIDDEN_STRATA_PRIORS}

        for idx, b in enumerate(effective_buckets):
            bid = b["id"]
            st_name = str(b.get("stratum") or "M_MID_UB")
            pw = w0[idx] if idx < len(w0) else 0.0
            lw = w_hat[idx] if idx < len(w_hat) else 0.0
            r_k = round(active_phi[idx] / 90.0, 4) if active_phi else ledger.implied_hidden_r
            cur_b_pts = round(90.0 * lw * r_k, 3)
            max_b_pts = round(90.0 * lw, 3)
            hr_b_pts = round(max(0.0, max_b_pts - cur_b_pts), 3)
            gain_2x = round(90.0 * lw * max(0.0, min(1.0, 2.0 * r_k) - r_k), 3)

            strat_prior_hid[st_name] = strat_prior_hid.get(st_name, 0.0) + pw
            strat_learn_hid[st_name] = strat_learn_hid.get(st_name, 0.0) + lw
            strat_pts_raw[st_name] = strat_pts_raw.get(st_name, 0.0) + cur_b_pts

            buckets_out.append({
                "id": bid,
                "stratum": st_name,
                "dtype": b.get("dtype", "float32"),
                "description": b.get("description", ""),
                "source": b.get("source", "spec" if bid in spec_id_set else "auto_discovered"),
                "in_spec": bid in spec_id_set,
                "theta0_us": theta0[idx],
                "tau_active_us": round(active_timings[bid], 3) if bid in active_timings else None,
                "observed": bid in active_timings,
                "prior_w": round(pw, 5),
                "learned_w": round(lw, 5),
                "posterior_std_w": round(math.sqrt(diag_var[idx]), 5) if idx < len(diag_var) else 0.05,
                "empirical_corr": b_corr.get(bid),
                "empirical_pairs": b_npairs.get(bid, 0),
                "efficiency_r": round(r_k, 3),
                "current_pts": round(cur_b_pts, 2),
                "max_pts": round(max_b_pts, 2),
                "headroom_pts": round(hr_b_pts, 2),
                "marginal_pts_per_2x": round(gain_2x, 2),
            })

        sum_hid_model = sum(strat_pts_raw.values())
        hid_scale = (
            (ledger.hidden_score / sum_hid_model)
            if (ledger.current_score > 0 and sum_hid_model > 1e-6)
            else (0.0 if ledger.current_score <= 0 else 1.0)
        )

        strata_summary: list[dict[str, Any]] = []
        for s_key, s_label, _default_pw100 in STRATA_ORDER:
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

        # Closed-loop diagnostic triggers (v2 upgraded!)
        diagnostics: list[dict[str, Any]] = []
        if unpaired_buckets:
            missing_dtypes = sorted({
                b["dtype"] for b in spec_buckets if b["id"] in unpaired_buckets and b["stratum"] == "M_MID_UB"
            })
            diagnostics.append({
                "code": "UNPAIRED_BUCKETS",
                "severity": "warning" if len(unpaired_buckets) >= 4 else "info",
                "message": (
                    f"{len(unpaired_buckets)}/{len(spec_buckets)} spec buckets lack direct timings "
                    f"(examples: {', '.join(unpaired_buckets[:4])})"
                    + (f"; missing M_MID_UB dtypes={missing_dtypes}" if missing_dtypes else "")
                ),
            })

        if extra_ids:
            high_sig_extra = [eid for eid in extra_ids if (b_corr.get(eid) or 0.0) >= 0.35]
            diagnostics.append({
                "code": "UNTRACKED_EXTRA_BUCKETS",
                "severity": "high" if (len(high_sig_extra) >= 2 or len(unpaired_buckets) >= len(spec_buckets)) else "info",
                "message": (
                    f"{len(extra_ids)} logged profiler bucket(s) outside cases_perf.json auto-included in v2 model"
                    + (f" ({len(high_sig_extra)} high-corr >= +0.35: {', '.join(high_sig_extra[:4])}); run `opsb bench evolve {title}`" if high_sig_extra else f"; run `opsb bench evolve {title}` to promote")
                ),
            })

        anti_corr_buckets = sorted(
            [(k, c) for k, c in b_corr.items() if b_npairs.get(k, 0) >= 4 and c <= -0.25],
            key=lambda x: (float(x[1]), str(x[0])),
        )
        if anti_corr_buckets:
            diagnostics.append({
                "code": "ANTI_CORR_SUPPRESSED",
                "severity": "warning",
                "message": (
                    f"Pairwise v2 suppressed {len(anti_corr_buckets)} anti-correlated bucket(s): "
                    + ", ".join(f"{k}(r={c:+.2f})" for k, c in anti_corr_buckets[:4])
                ),
            })

        if high_residual_events:
            latest_ev = high_residual_events[-1]
            diagnostics.append({
                "code": "HIGH_RESIDUAL_EVENT",
                "severity": "high" if latest_ev["sign_flip"] or abs(latest_ev["residual_eps"]) >= 5.0 else "warning",
                "message": (
                    f"{len(high_residual_events)} high-residual/sign-flip event(s); latest "
                    f"{latest_ev['base_sha']}->{latest_ev['cand_sha']}: actual={latest_ev['actual_delta']:+.2f} vs "
                    f"cv_pred={latest_ev['pred_delta']:+.2f} (eps={latest_ev['residual_eps']:+.2f}, causes={latest_ev['causes']})"
                ),
            })

        # Binary cold-start bloat check on latest two observations with `.o` stats
        bin_obs = [s for s in sha_order if bin_bytes_by_sha.get(s)]
        if len(bin_obs) >= 2:
            b_prev, b_cur = bin_bytes_by_sha[bin_obs[-2]], bin_bytes_by_sha[bin_obs[-1]]
            if b_prev and b_cur and b_cur > 131072 and b_cur > b_prev * 1.20:
                diagnostics.append({
                    "code": "BINARY_BLOAT_ALERT",
                    "severity": "high",
                    "message": (
                        f"Compiled kernel .o grew {b_prev/1024:.1f}KB -> {b_cur/1024:.1f}KB "
                        f"({100*(b_cur-b_prev)/b_prev:+.1f}%) on {bin_obs[-1][:7]}; risk of 3-run cold-start penalty"
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

        buckets_ranked = sorted(
            buckets_out,
            key=lambda x: (-float(x["headroom_pts"]), -float(x["marginal_pts_per_2x"]), str(x["id"])),
        )
        active_regime = regime_by_sha.get(active_sha or "", "none")

        cal_doc = {
            "model_version": MODEL_VERSION,
            "title": title,
            "updated_at": _utc_now(),
            "active_board_sha": active_sha,
            "active_profiler_regime": active_regime,
            "observations_m": len(sha_order),
            "paired_observations_m": len(paired_shas),
            "pairwise_count": len(pairs),
            "beta_tightness": beta_p,
            "transfer_gamma": transfer_gamma,
            "alpha_cold": alpha_cold,
            "decay_factor_gamma": ledger.decay_factor_gamma,
            "mae_pts": mae_pts,
            "rmse_pts": rmse_pts,
            "loocv_mae_pts": cv["loocv_mae_pts"],
            "loocv_rmse_pts": cv["loocv_rmse_pts"],
            "loocv_spearman": cv["loocv_spearman"],
            "loocv_dir_acc": cv["loocv_dir_acc"],
            "calibration_trust": cv["calibration_trust"],
            "last_predicted_score": last_pred,
            "last_actual_score": last_actual,
            "last_residual_pts": last_res,
            "escape_lock": esc_info["escape_lock"],
            "escape_sha": esc_info["escape_short_sha"],
            "escape_full_sha": esc_info["escape_sha"],
            "escape_status": esc_info["escape_status"],
            "escaped_commits": esc_info["escaped_commits"],
            "spec_bucket_count": len(spec_buckets),
            "effective_bucket_count": len(effective_buckets),
            "extra_discovered_buckets": extra_ids,
            "unpaired_bucket_count": len(unpaired_buckets),
            "unpaired_buckets": unpaired_buckets,
            "high_residual_events": high_residual_events,
            "diagnostics": diagnostics,
            "strata": strata_summary,
            "buckets": buckets_ranked,
            "posterior_diag_var": diag_var,
            "theta0_us": theta0,
            "observed_min_tau": {k: round(v, 4) for k, v in observed_min_tau.items()},
            "bucket_order": bucket_ids,
            "bucket_strata_order": [strata_map[bid] for bid in bucket_ids],
            "learned_weights_order": w_hat,
            "bucket_correlations": b_corr,
            "bucket_pair_counts": b_npairs,
            "cold_start_telemetry": {
                "active_kernel_o_max_bytes": bin_bytes_by_sha.get(active_sha or ""),
                "active_cold_start_stats": active_tel.get("cold_start_stats") or {},
            },
            "coverage_grid": cov_grid,
            "observation_ledger": obs_ledger,
        }

        if persist and as_of is None:
            out_file = self.workspace / "results" / title / "calibration.json"
            atomic_write_json(out_file, cal_doc, indent=2, ensure_ascii=False)
            # Auto-persist structured bench feedback whenever high-residual events are detected
            if high_residual_events:
                try:
                    self._write_feedback_from_cal(title, cal_doc, perf_spec, observed_samples)
                except Exception:
                    pass

        return cal_doc

    def load_or_calibrate(self, title: str, as_of: datetime.datetime | None = None) -> dict[str, Any]:
        c_key = (title, as_of.isoformat() if as_of else "LIVE")
        if c_key in self._cal_cache:
            return self._cal_cache[c_key]
        if as_of is not None:
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
            doc, ok = read_json_safe(cal_file)
            if ok and isinstance(doc, dict) and doc.get("model_version") == MODEL_VERSION:
                esc = self.inspect_escape_state(title)
                doc["escape_lock"] = esc["escape_lock"]
                doc["escape_sha"] = esc["escape_short_sha"]
                doc["escape_full_sha"] = esc["escape_sha"]
                doc["escape_status"] = esc["escape_status"]
                doc["escaped_commits"] = esc["escaped_commits"]
                self._cal_cache[c_key] = doc
                return doc
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
        noise_floor_pts: float = 2.5,
    ) -> dict[str, Any] | None:
        """Predict candidate score, uncertainty sigma, and `EXPLORE` upside using dual-anchor Pairwise v2.0."""
        if not cand_sha or not (base_sha or ledger.historical_best_sha):
            return None

        cand_tel = self._extract_full_telemetry(title, cand_sha, allow_future_siblings=(as_of is None))
        if not cand_tel["robust_cases"] or cand_tel["modality"] != "profiler":
            return None

        cal = self.load_or_calibrate(title, as_of=None if fast_cached_cal else as_of)
        perf_spec = self.load_perf_spec(title)
        spec_ids = [b["id"] for b in (perf_spec.get("hidden_buckets") or []) if isinstance(b, dict) and b.get("id")]

        latest_pred: dict[str, Any] | None = None
        if base_sha:
            base_tel = self._extract_full_telemetry(
                title, base_sha, target_soc=cand_tel["soc"], allow_future_siblings=(as_of is None)
            )
            anchor_latest_sc = ledger.current_score if ledger.current_score > 0 else (ledger.contemporary_best_score or 45.0)
            r_latest = (
                ledger.implied_hidden_r
                if ledger.implied_hidden_r > 0
                else max(0.20, min(0.88, (anchor_latest_sc * 0.87) / 90.0))
            )
            latest_pred = evaluate_pairwise_anchor(
                cand_sha=cand_sha,
                cand_tel=cand_tel,
                anchor_sha=base_sha,
                anchor_tel=base_tel,
                anchor_kind="latest",
                anchor_sc=anchor_latest_sc,
                r_anchor=r_latest,
                current_score=ledger.current_score,
                frontier_top1=ledger.frontier_top1,
                cal=cal,
                spec_ids=spec_ids,
                noise_floor_pts=noise_floor_pts,
            )

        best_pred: dict[str, Any] | None = None
        best_sha = ledger.historical_best_sha
        best_sc = ledger.contemporary_best_score if ledger.contemporary_best_score > 0 else ledger.raw_historical_best_score
        if best_sha and best_sha != base_sha and best_sc > 0:
            best_tel = self._extract_full_telemetry(
                title, best_sha, target_soc=cand_tel["soc"], allow_future_siblings=(as_of is None)
            )
            r_best = max(0.20, min(0.88, (best_sc * 0.87) / 90.0))
            best_pred = evaluate_pairwise_anchor(
                cand_sha=cand_sha,
                cand_tel=cand_tel,
                anchor_sha=best_sha,
                anchor_tel=best_tel,
                anchor_kind="best",
                anchor_sc=best_sc,
                r_anchor=r_best,
                current_score=ledger.current_score,
                frontier_top1=ledger.frontier_top1,
                cal=cal,
                spec_ids=spec_ids,
                noise_floor_pts=noise_floor_pts,
            )

        return select_dual_anchor_prediction(latest_pred, best_pred, ledger.current_score, best_sc)

    def _build_feedback_payload(
        self,
        title: str,
        cal: dict[str, Any],
        perf_spec: dict[str, Any],
        observed_samples: dict[str, list[float]] | None = None,
    ) -> dict[str, Any]:
        """Build structured bench health & evolution feedback payload for `bench_feedback.json`."""
        b_corr: dict[str, float] = dict(cal.get("bucket_correlations") or {})
        b_npairs: dict[str, int] = dict(cal.get("bucket_pair_counts") or {})
        buckets = list(cal.get("buckets") or [])
        by_id = {b["id"]: b for b in buckets}

        strong_positive = sorted(
            [
                {
                    "bucket": k,
                    "corr": round(c, 3),
                    "pairs": b_npairs.get(k, 0),
                    "in_spec": bool((by_id.get(k) or {}).get("in_spec", False)),
                    "stratum": (by_id.get(k) or {}).get("stratum"),
                    "learned_w": (by_id.get(k) or {}).get("learned_w"),
                    "tau_active_us": (by_id.get(k) or {}).get("tau_active_us"),
                }
                for k, c in b_corr.items()
                if c >= 0.35 and b_npairs.get(k, 0) >= 3
            ],
            key=lambda x: (-float(x["corr"]), -int(x["pairs"]), str(x["bucket"])),
        )

        misleading_anti = sorted(
            [
                {
                    "bucket": k,
                    "corr": round(c, 3),
                    "pairs": b_npairs.get(k, 0),
                    "in_spec": bool((by_id.get(k) or {}).get("in_spec", False)),
                    "stratum": (by_id.get(k) or {}).get("stratum"),
                    "learned_w": (by_id.get(k) or {}).get("learned_w"),
                    "tau_active_us": (by_id.get(k) or {}).get("tau_active_us"),
                }
                for k, c in b_corr.items()
                if c <= -0.20 and b_npairs.get(k, 0) >= 3
            ],
            key=lambda x: (float(x["corr"]), str(x["bucket"])),
        )

        all_discovered: dict[str, float] = {}
        if observed_samples:
            for bk, vals in observed_samples.items():
                if vals:
                    all_discovered[bk] = float(statistics.median(vals))
        else:
            for b in buckets:
                tv = b.get("tau_active_us") or (cal.get("observed_min_tau") or {}).get(b["id"])
                if isinstance(tv, (int, float)) and tv > 0:
                    all_discovered[b["id"]] = float(tv)

        evo_plan = build_evolution_plan(
            title=title,
            spec_doc=perf_spec,
            all_discovered=all_discovered,
            bucket_correlations=b_corr,
            bucket_pair_counts=b_npairs,
        )

        # Identify unexplained residual dimensions & actionable new-case recommendations
        unexplained_dims: list[str] = []
        suggested_new_cases: list[dict[str, Any]] = []
        unpaired = list(cal.get("unpaired_buckets") or [])
        if unpaired:
            proxy_unpaired = [u for u in unpaired if u.endswith("_proxy")]
            if proxy_unpaired:
                unexplained_dims.append(f"Unmeasured *_proxy buckets ({len(proxy_unpaired)}): {', '.join(proxy_unpaired[:5])}")
                for pu in proxy_unpaired[:4]:
                    suggested_new_cases.append({
                        "id": pu,
                        "action": "add_to_validation_profiler",
                        "reason": "Spec proxy bucket has no direct local profiler measurement",
                    })

        present_dtypes = {str(b.get("dtype") or "float32") for b in buckets if b.get("observed")}
        for need_dt in ("float16", "bfloat16"):
            if need_dt not in present_dtypes and title not in ("MultilabelMarginLoss",):
                unexplained_dims.append(f"No observed local profiler bucket with dtype={need_dt}")
                suggested_new_cases.append({
                    "id": f"auto_{need_dt}_mid_case",
                    "dtype": need_dt,
                    "action": "add_new_validation_case",
                    "reason": f"Missing {need_dt} coverage in active profiler spectrum",
                })

        events = list(cal.get("high_residual_events") or [])
        all_causes = sorted({c for ev in events for c in (ev.get("causes") or [])})
        if "SOC_CORE_MISMATCH" in all_causes or title == "QuantUpdateScatter":
            unexplained_dims.append("Local soc-a (40 AIV) vs online judge core-count / block_dim sensitivity")
        if "COLD_START_BINARY_BLOAT" in all_causes or title in ("PadV3Grad", "StridedSliceGrad"):
            unexplained_dims.append("3-run cold-start sensitivity to compiled kernel `.o` ELF size / .text bloat")
        if "MISSING_LARGE_N_DIM" in all_causes or title == "SyncBatchNormGatherStats":
            unexplained_dims.append("Missing large world_size / large-N reduction dimension (current logs vary C only)")
            suggested_new_cases.append({
                "id": "sbn_w64_c2048_largeN_f32",
                "dtype": "float32",
                "action": "add_new_validation_case",
                "reason": "Cover large world_size/N axis alongside large C",
            })
        if not (cal.get("cold_start_telemetry") or {}).get("active_kernel_o_max_bytes"):
            unexplained_dims.append("Active commit lacks `.o` binary size telemetry (will auto-populate on next `opsb test`)")

        return {
            "schema_version": "2.0",
            "title": title,
            "updated_at": _utc_now(),
            "model_version": cal.get("model_version", MODEL_VERSION),
            "health": {
                "loocv_mae_pts": cal.get("loocv_mae_pts"),
                "loocv_rmse_pts": cal.get("loocv_rmse_pts"),
                "loocv_spearman": cal.get("loocv_spearman"),
                "loocv_dir_acc": cal.get("loocv_dir_acc"),
                "calibration_trust": cal.get("calibration_trust"),
                "transfer_gamma": cal.get("transfer_gamma"),
                "alpha_cold": cal.get("alpha_cold"),
                "spec_buckets": cal.get("spec_bucket_count"),
                "effective_buckets": cal.get("effective_bucket_count"),
                "unpaired_spec_buckets": cal.get("unpaired_bucket_count"),
                "high_residual_event_count": len(events),
            },
            "high_residual_events": events[-8:],
            "strong_positive_buckets": strong_positive[:10],
            "misleading_anti_corr_buckets": misleading_anti[:10],
            "untracked_promote_candidates": evo_plan["promote_buckets"][:15],
            "unexplained_residual_dimensions": unexplained_dims,
            "suggested_new_cases": suggested_new_cases,
            "agent_guidance": (
                f"Prioritize optimizing {[x['bucket'] for x in strong_positive[:4]]}; "
                f"avoid sacrificing hot paths for anti-correlated {[x['bucket'] for x in misleading_anti[:3]]}."
                if (strong_positive or misleading_anti)
                else "Insufficient same-regime pairs yet; keep expanding balanced S/M/L/X profiler coverage."
            ),
        }

    def _write_feedback_from_cal(
        self,
        title: str,
        cal: dict[str, Any],
        perf_spec: dict[str, Any],
        observed_samples: dict[str, list[float]] | None = None,
    ) -> pathlib.Path:
        payload = self._build_feedback_payload(title, cal, perf_spec, observed_samples=observed_samples)
        fb_path = self.specs_root / title / "bench_feedback.json"
        atomic_write_json(fb_path, payload, indent=2, ensure_ascii=False)
        return fb_path

    def _inspect_commit_validation(self, title: str, sha: str) -> dict[str, Any]:
        req = sha.strip()
        res_dir = self.workspace / "results" / title
        resolved = req
        run_dir = res_dir / req
        if not run_dir.is_dir() and res_dir.is_dir():
            low = req.lower()
            matches = sorted(d for d in res_dir.iterdir() if d.is_dir() and d.name.lower().startswith(low))
            if matches:
                run_dir = matches[-1]
                resolved = run_dir.name
        info: dict[str, Any] = {"requested_sha": req, "resolved_sha": resolved, "found": run_dir.is_dir()}
        if not run_dir.is_dir():
            return info
        rj: dict[str, Any] = {}
        for jn in ("result.json", "result-fast.json", "result-build.json"):
            jp = run_dir / jn
            if jp.is_file():
                try:
                    rj = json.loads(jp.read_text(encoding="utf-8"))
                except Exception:
                    rj = {}
                break
        log_text = ""
        for ln in ("validation.log", "validation-fast.log", "build.log"):
            lp = run_dir / ln
            if lp.is_file():
                log_text = lp.read_text(errors="replace")
                info["log_file"] = str(lp)
                break
        parsed = parse_validation_perf(log_text) if log_text else {"cases": {}, "source": "none"}
        exit_code = rj.get("exit_code")
        passed = (exit_code == 0) or (exit_code is None and ("EXTENDED_PASSED" in log_text or "BUILD_ONLY_PASSED" in log_text))
        info.update({
            "passed": bool(passed),
            "exit_code": exit_code,
            "profiler_regime": rj.get("profiler_regime"),
            "perf_source": parsed.get("source"),
            "measured_bucket_count": len(parsed.get("cases") or {}),
            "phase_a_cpu_sec": rj.get("phase_a_cpu_sec"),
            "phase_b_die_sec": rj.get("phase_b_die_sec"),
        })
        return info

    def diagnose_problem(self, title: str, sha: str | None = None) -> dict[str, Any]:
        """Full bench health check, bucket correlation attribution, residual forensics, and `bench_feedback.json` write."""
        self._esc_cache.clear()
        self._cal_cache.clear()
        self._perf_spec_cache.pop(title, None)
        cal = self.calibrate_problem(title, persist=True)
        perf_spec = self.load_perf_spec(title)
        fb_payload = self._build_feedback_payload(title, cal, perf_spec)
        fb_path = self.specs_root / title / "bench_feedback.json"
        atomic_write_json(fb_path, fb_payload, indent=2, ensure_ascii=False)

        # Static scan of C++ kernel/tiling for uncovered slow/fallback branches (read-only)
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
                "in_spec": b.get("in_spec", True),
                "learned_w": b["learned_w"],
                "empirical_corr": b.get("empirical_corr"),
                "empirical_pairs": b.get("empirical_pairs", 0),
                "efficiency_r": b["efficiency_r"],
                "tau_active_us": b["tau_active_us"],
                "theta0_us": b["theta0_us"],
                "headroom_pts": b["headroom_pts"],
                "marginal_pts_per_2x": b["marginal_pts_per_2x"],
                "observed": b["observed"],
            }
            for b in cal["buckets"][:10]
        ]

        out = {
            "title": title,
            "model_version": cal.get("model_version", MODEL_VERSION),
            "updated_at": cal["updated_at"],
            "beta_tightness": cal["beta_tightness"],
            "transfer_gamma": cal.get("transfer_gamma"),
            "alpha_cold": cal.get("alpha_cold"),
            "mae_pts": cal["mae_pts"],
            "rmse_pts": cal["rmse_pts"],
            "loocv_mae_pts": cal.get("loocv_mae_pts"),
            "loocv_spearman": cal.get("loocv_spearman"),
            "loocv_dir_acc": cal.get("loocv_dir_acc"),
            "calibration_trust": cal.get("calibration_trust"),
            "escape_lock": cal["escape_lock"],
            "escape_sha": cal["escape_sha"],
            "spec_bucket_count": cal.get("spec_bucket_count"),
            "effective_bucket_count": cal.get("effective_bucket_count"),
            "extra_discovered_buckets": cal.get("extra_discovered_buckets", []),
            "unpaired_bucket_count": cal["unpaired_bucket_count"],
            "unpaired_buckets": cal["unpaired_buckets"],
            "high_residual_events": fb_payload["high_residual_events"],
            "strong_positive_buckets": fb_payload["strong_positive_buckets"],
            "misleading_anti_corr_buckets": fb_payload["misleading_anti_corr_buckets"],
            "untracked_promote_candidates": fb_payload["untracked_promote_candidates"],
            "unexplained_residual_dimensions": fb_payload["unexplained_residual_dimensions"],
            "suggested_new_cases": fb_payload["suggested_new_cases"],
            "agent_guidance": fb_payload["agent_guidance"],
            "feedback_file": str(fb_path),
            "diagnostics": cal["diagnostics"],
            "strata": cal["strata"],
            "top_roi_buckets": top_roi,
            "detected_cpp_branch_guards": fallback_branches[:10],
            "observation_ledger": cal["observation_ledger"],
        }
        if sha:
            cv = self._inspect_commit_validation(title, sha)
            out["requested_sha"] = sha
            out["sha"] = cv["resolved_sha"]
            out["commit_validation"] = cv
        return out

    def evolve_bench(
        self,
        title: str,
        apply: bool = True,
        min_pairs: int = 2,
        min_corr: float = 0.15,
    ) -> dict[str, Any]:
        """Online-feedback-driven append-only bench evolution (`opsb bench evolve [Title]`).

        Promotes high-signal logged `profiler` buckets into `ops-supervisor/bench-specs/<Title>/cases_perf.json`
        (strictly append-only, NEVER deleting old buckets!), persists `bench_feedback.json`,
        and re-calibrates to report Before vs After coverage & LOOCV MAE.
        """
        self._esc_cache.clear()
        self._cal_cache.clear()
        self._perf_spec_cache.pop(title, None)

        from .case_bench import has_reference
        from .case_feedback import evolve
        repo = self.workspace / self.estimator.problems_meta.get(title,{}).get("path",f"bench/{title}")
        if has_reference(repo,"HEAD"):
            return evolve(self.workspace,title,repo,self.estimator._filtered_outcomes(title),apply)

        cal_before = self.calibrate_problem(title, persist=False)
        spec_path = self.specs_root / title / "cases_perf.json"
        spec_doc = json.loads(spec_path.read_text(encoding="utf-8")) if spec_path.is_file() else {
            "problem": title, "public_anchors": [], "hidden_buckets": []
        }

        all_discovered: dict[str, float] = {}
        for b in cal_before.get("buckets") or []:
            tv = b.get("tau_active_us") or (cal_before.get("observed_min_tau") or {}).get(b["id"])
            if isinstance(tv, (int, float)) and tv > 0:
                all_discovered[b["id"]] = float(tv)

        plan = build_evolution_plan(
            title=title,
            spec_doc=spec_doc,
            all_discovered=all_discovered,
            bucket_correlations=dict(cal_before.get("bucket_correlations") or {}),
            bucket_pair_counts=dict(cal_before.get("bucket_pair_counts") or {}),
            min_pairs=min_pairs,
            min_corr=min_corr,
        )

        append_res = {
            "spec_path": str(spec_path),
            "appended_count": 0,
            "appended_ids": [],
            "total_buckets_after": plan["existing_bucket_count"],
        }
        if apply and plan["promote_buckets"] and spec_path.is_file():
            append_res = append_buckets_to_spec(spec_path, plan["promote_buckets"])
            self._perf_spec_cache.pop(title, None)
            self._cal_cache.clear()

        cal_after = self.calibrate_problem(title, persist=apply) if apply else cal_before
        fb_path = None
        if apply:
            fb_path = str(self._write_feedback_from_cal(title, cal_after, self.load_perf_spec(title)))

        return {
            "title": title,
            "applied": apply,
            "spec_path": str(spec_path),
            "feedback_file": fb_path,
            "severe_spec_blindness": plan["severe_spec_blindness"],
            "spec_buckets_before": plan["existing_bucket_count"],
            "spec_buckets_after": append_res["total_buckets_after"],
            "matched_existing_before": plan["matched_existing_count"],
            "appended_count": append_res["appended_count"],
            "appended_ids": append_res["appended_ids"] if apply else [b["id"] for b in plan["promote_buckets"]],
            "skipped_anti_corr": plan["skipped_anti_corr"],
            "loocv_mae_before": cal_before.get("loocv_mae_pts"),
            "loocv_mae_after": cal_after.get("loocv_mae_pts"),
            "loocv_spearman_after": cal_after.get("loocv_spearman"),
            "calibration_trust_after": cal_after.get("calibration_trust"),
            "effective_bucket_count": cal_after.get("effective_bucket_count"),
        }

    def verify_escape(
        self,
        title: str,
        bad_sha: str,
        good_sha: str,
        review: str = "",
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
        good_record = json.loads(good_res.read_text())
        g_rc = good_record.get("exit_code")
        g_txt = good_log.read_text(errors="replace")
        from .case_bench import has_reference,formal_evidence
        from .case_feedback import regression_evidence
        repo = self.workspace / self.estimator.problems_meta.get(title,{}).get("path",f"bench/{title}")
        reference_suite = has_reference(repo,c_good)
        evidence = {}
        if reference_suite:
            good = formal_evidence(good_res.parent,repo,good_record)
            if good:
                evidence = regression_evidence(good_res.parent.parent/c_bad,good)
            if not evidence and review.strip() and good and good["settings"].get("strict_fp32"):
                from .case_bench import formal_evidence, read_doc
                from .case_feedback import authorize_review_trial
                bad_folder = good_res.parent.parent / c_bad
                bad = formal_evidence(bad_folder, repo, read_doc(bad_folder / "result.json"))
                return authorize_review_trial(repo, bad_folder, bad, good, self.inspect_escape_state(title), review)
            if not evidence:
                return {"ok":False,"cleared":False,"title":title,"reason":"Need a matching failed case on bad SHA and a full formal pass on good SHA"}
        elif g_rc != 0 or "EXTENDED_PASSED" not in g_txt or "FAST_LANE=1" in g_txt:
            return {
                "ok": False,
                "cleared": False,
                "title": title,
                "bad_sha": c_bad,
                "good_sha": c_good,
                "reason": f"Good commit {c_good[:7]} did not pass full Q1 server validation (exit_code={g_rc})",
            }

        # Check that either validation/ or bench-specs/ grew or references the escape fix vs bad_sha
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

        if not (evidence or has_validation_update or spec_mentions_bad):
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

        state = self.inspect_escape_state(title)
        key = f"{title}:{c_bad}"
        ids = set(self.verified_escapes.get(key, {}).get("submission_ids", []))
        sid = state.get("escape_submission_id")
        if state["escape_lock"] and state["escape_sha"] != c_bad:
            if not sid:
                return {"ok": False, "cleared": False, "reason": "Active escape has no matching SHA or submission ID"}
            from .case_feedback import match_escape_submission
            try:
                matches = match_escape_submission(repo, c_bad, self.estimator.problems_meta[title]["problem_id"],
                                                  {"submission_id": sid, "account_id": state["escape_account_id"],
                                                   "status": state["escape_status"]})
            except Exception as exc:
                return {"ok": False, "cleared": False, "submission_id": sid,
                        "reason": f"Could not verify judge source: {type(exc).__name__}: {exc}"}
            if not matches:
                return {"ok": False, "cleared": False, "submission_id": sid,
                        "reason": "Bad commit source does not match the active judge submission"}
            ids.add(sid)
        rec = {
            **evidence,
            "submission_ids": sorted(ids),
            "bad_sha": c_bad,
            "good_sha": c_good,
            "verified_at": _utc_now(),
            "diff_files": diff_files,
        }
        self.verified_escapes[key] = rec
        self._esc_cache.clear()
        self._cal_cache.clear()
        cal = self.calibrate_problem(title, persist=True)
        return {
            "ok": True,
            "cleared": not cal["escape_lock"],
            "submission_ids": sorted(ids),
            **evidence,
            "title": title,
            "bad_sha": c_bad,
            "good_sha": c_good,
            "diff_files": diff_files,
            "escape_lock": cal["escape_lock"],
        }
