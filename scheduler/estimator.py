"""Expected Score Estimator & Non-Stationary `tbest(t)` Decay Ledger.

Models the contest's time-varying harmonic efficiency objective:
    S_p(SHA, t) = 2 * sum_{i=1..50} (tbest_i(t) / tau_i(SHA))
                = S_{p,vis}(t) (<= 10) + S_{p,hid}(t) (<= 90)
    contest_total(t) = sum_p (difficulty_p * S_p(SHA_p, t))   [max 2300]

Key Non-Stationarity Invariant (`tbest(t)` Monotonic Tightening):
  Site-wide `tbest_i(t)` tightens monotonically over wall-clock time `t`
  (observed `gamma_p(11-10 -> 11-18) ~ 0.947..0.987`, `-0.5 to -2.5 pts` over 8 days
  on untouched board submissions). Comparing `S_live(t_now)` directly against raw
  historical `S(SHA*, t_0)` manufactures phantom restore gaps.

  Therefore, for each problem `p`:
    1. Match the active board submission (`score-snapshot.json` `submission_id` /
       `submission_time` or latest `Pass` outcome) to identify `(active_sha, S_judged, t_active)`.
    2. Compute observed post-active decay factor:
           gamma_p = clamp(S_live(t_now) / S_judged(active_sha, t_active), 0.5, 1.0)
    3. Identity Rule: if `candidate_sha == active_sha` (and `S_live > 0`), its
       `contemporary_equiv_score` is identically `S_live(t_now)` (`true_restore_gain = 0.0`).
    4. Superseded SHA Rule: for any historical draw `(s_k, t_k)` of `candidate_sha != active_sha`,
       discount to `t_now` via `gamma_p` plus residual pre-active drift `-0.015 pts/hr`
       strictly over `max(0, t_active - t_k)`:
           s_contemp = max(0.0, s_k * gamma_p - 0.015 * max(0.0, (t_anchor - t_k)_hrs))
"""
from __future__ import annotations

import dataclasses
import datetime
import json
import math
import pathlib
import re
import statistics
import subprocess
from typing import Any

# Measured platform constants (from results/scoring-rules.md & 8-day snapshot telemetry)
SIGMA_HIDDEN_JITTER: float = 2.70           # pts per re-run on identical source
RESIDUAL_PRE_ACTIVE_DRIFT_HR: float = 0.015  # pts/hr residual tbest tightening over (t_active - t_cand)

# Difficulty weights (sum = 23, max contest total = 2300)
PROBLEM_DIFFICULTY: dict[str, int] = {
    "ConcatD": 3,
    "ConfusionTransposeD": 3,
    "CTCLossV2": 2,
    "LayerNormV3": 2,
    "MultilabelMarginLoss": 2,
    "PadV3Grad": 3,
    "PReluGradUpdate": 1,
    "QuantUpdateScatter": 2,
    "StridedSliceGrad": 3,
    "SyncBatchNormGatherStats": 2,
}

# Operators with cross-core reductions, GM flag handshakes, or sequential barrier chains
CROSS_CORE_OPS: frozenset[str] = frozenset({
    "CTCLossV2",
    "LayerNormV3",
    "MultilabelMarginLoss",
    "SyncBatchNormGatherStats",
})

# Frozen public-case site-wide tbest (us) per problem (5 visible cases each)
PUBLIC_TBEST_FALLBACK: dict[str, list[float]] = {
    "ConcatD": [3.523333, 3.490000, 3.693333, 3.593333, 3.496667],
    "ConfusionTransposeD": [3.663333, 3.450000, 3.453333, 3.496667, 3.430000],
    "CTCLossV2": [4.920000, 4.603333, 4.543333, 5.400000, 5.313333],
    "LayerNormV3": [4.040000, 3.956667, 5.993333, 3.990000, 6.050000],
    "MultilabelMarginLoss": [4.270000, 4.230000, 4.463333, 4.496667, 4.866667],
    "PadV3Grad": [3.996667, 3.943500, 3.890167, 4.166667, 3.963333],
    "PReluGradUpdate": [3.783333, 3.750000, 3.760000, 3.713333, 3.726667],
    "QuantUpdateScatter": [3.740000, 3.970000, 4.200000, 3.836667, 4.260000],
    "StridedSliceGrad": [3.960000, 3.813333, 3.756667, 3.726667, 3.836667],
    "SyncBatchNormGatherStats": [3.936667, 3.890000, 3.916667, 3.933333, 3.980000],
}

# Site-wide rank-1 and rank-5 scores (measured in scoring-evidence/board-summary.json)
FRONTIER_FALLBACK: dict[str, tuple[float, float]] = {
    "ConcatD": (86.48, 73.29),
    "ConfusionTransposeD": (85.96, 77.16),
    "CTCLossV2": (80.44, 65.02),
    "LayerNormV3": (72.79, 66.55),
    "MultilabelMarginLoss": (65.55, 61.80),
    "PadV3Grad": (73.99, 57.17),
    "PReluGradUpdate": (84.68, 78.63),
    "QuantUpdateScatter": (90.11, 81.03),
    "StridedSliceGrad": (89.50, 69.68),
    "SyncBatchNormGatherStats": (92.19, 76.73),
}

# Verified closing-checklist fallback SHAs (used only if outcome history is absent)
VERIFIED_BEST_FALLBACK: dict[str, tuple[str, float]] = {
    "ConcatD": ("899a2dc6a824a519c73b0686c69ee9b9f916fb12", 52.87),
    "ConfusionTransposeD": ("fa81ef9b64c2e6abf66f0115f7c1be37b45dd3ce", 43.55),
    "CTCLossV2": ("0d5125cd90baa4e421abfa72a042a021a1eaba50", 52.85),
    "LayerNormV3": ("af0807c2c6001fdcfd870ebb05b8262864556033", 43.66),
    "MultilabelMarginLoss": ("59f80657c0fc0dcf1fbd1e735cf36b2c34651bb3", 56.64),
    "PadV3Grad": ("0fc367de124f4cd43c729b34437daa67a0bc2069", 30.38),
    "PReluGradUpdate": ("33363f0417074ae62c0a5705a6f48539958e5eae", 56.84),
    "QuantUpdateScatter": ("d5244737fa3a3232b4ca899faa1a121a1fba0f82", 49.04),
    "StridedSliceGrad": ("45eb08f088780645c7309a923029119ada0454e5", 48.96),
    "SyncBatchNormGatherStats": ("544439ed46c30a2252aa5769a4818352005dd4bb", 57.45),
}


@dataclasses.dataclass
class ProblemLedger:
    title: str
    difficulty: int
    board_status: str
    current_score: float
    weighted_current_score: float
    public_score: float
    hidden_score: float
    implied_hidden_r: float
    public_r_list: list[float]
    frontier_top1: float
    frontier_top5: float
    target_score: float
    recoverable_headroom: float
    theoretical_headroom: float
    active_board_sha: str | None
    active_judged_score: float | None
    active_judged_at: str | None
    decay_factor_gamma: float
    raw_historical_best_score: float
    contemporary_best_score: float
    historical_best_score: float
    historical_best_mean: float
    historical_best_sha: str | None
    true_restore_gain: float
    restore_gap: float

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class ScoreEstimate:
    title: str
    sha: str
    short_sha: str
    difficulty: int
    board_status: str
    current_score: float
    estimated_score: float
    delta_expected: float
    weighted_delta: float
    confidence_sigma: float
    prob_positive: float
    recoverable_headroom: float
    effective_speedup: float
    estimation_method: str
    baseline_sha: str | None
    decay_factor_gamma: float
    raw_historical_best_score: float
    contemporary_best_score: float
    true_restore_gain: float
    matched_cases: int
    improved_cases: int
    regressed_cases: list[str]
    detail: str
    ucb_delta: float = 0.0
    calibration_trust: float = 0.5
    explore_eligible: bool = False
    explore_reason: str = ""
    explore_evidence: dict[str, Any] = dataclasses.field(default_factory=dict)
    thermal_drift_us: float = 0.0
    binary_delta_pct: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _parse_iso(ts: str | None) -> datetime.datetime | None:
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def resolve_sha(workspace: pathlib.Path, title: str, sha_prefix: str | None) -> str:
    """Normalize a short or symbolic SHA to its canonical 40-char commit hash."""
    repo = workspace / next((r["path"] for r in load_problem_manifest(workspace) if r["title"]==title),
                            f"bench/{title}")
    if not sha_prefix:
        if (repo / ".git").exists():
            res = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
            )
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip().lower()
        return ""

    cand = sha_prefix.strip().lower()
    if re.fullmatch(r"[0-9a-f]{40}", cand):
        return cand

    # 1. Check results/<Title>/<40-hex> directories first (fast, zero subprocess)
    res_dir = workspace / "results" / title
    if res_dir.is_dir():
        matches = [
            d.name.lower()
            for d in res_dir.iterdir()
            if d.is_dir() and re.fullmatch(r"[0-9a-f]{40}", d.name.lower()) and d.name.lower().startswith(cand)
        ]
        if len(matches) == 1:
            return matches[0]

    # 2. Check outcome / queue JSONL records
    for fname in ("submit-queue-outcome.jsonl", "submit-queue.jsonl"):
        fpath = workspace / "results" / fname
        if fpath.is_file():
            for line in fpath.read_text(errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                rsha = str(rec.get("sha", "")).lower()
                if rec.get("title") == title and len(rsha) == 40 and rsha.startswith(cand):
                    return rsha

    # 3. Fallback to git rev-parse in problem repo
    if (repo / ".git").exists():
        res = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify","--end-of-options",sha_prefix.strip()+"^{commit}"],
            capture_output=True,
            text=True,
        )
        if res.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", res.stdout.strip().lower()):
            return res.stdout.strip().lower()

    return cand


_MS_PROF_RE = re.compile(
    r"^MS_PROF\s+case=(?P<case>\S+).*?\bmedian=(?P<med>[\d.]+).*?\bmin=(?P<min>[\d.]+)"
)
_SPECTRUM_RE = re.compile(
    r"^SPECTRUM_SUMMARY\s+cases=(?P<n>\d+)\s+sum_med=(?P<smed>[\d.]+).*?\bvs_torch_med=(?P<vmed>[\d.]+)"
)
_KPERF_TXT_RE = re.compile(
    r"^KPERF\s+(?P<case>\S+).*?\bmedian=\s*(?P<med>[\d.]+)us\s+min=\s*(?P<min>[\d.]+)us"
)
_EXEC_ROW_RE = re.compile(
    r"^(?P<case>[A-Za-z0-9_./-]+)\s+ws=\d+\s+call=\s*[\d.]+/\s*[\d.]+/\s*[\d.]+\s+exec=\s*(?P<exec>[\d.]+)"
)
_RUNTIME_SOC_RE = re.compile(r"^RUNTIME_SOC=(\S+)")


from .parhelion_paths import atomic_write_json, get_parhelion_home, load_problem_manifest, read_json_safe

_FP_CACHE: dict[str, str | None] = {}
_FP_CACHE_LOADED: bool = False
_FP_CACHE_CORRUPT: bool = False


def _fp_cache_file() -> pathlib.Path:
    return get_parhelion_home() / "cache" / "code_fp_cache.json"


def _code_fingerprint(repo: pathlib.Path, sha: str) -> str | None:
    """Compute comment-stripped SHA-256 fingerprint of `op_host/` + `op_kernel/` at `sha`."""
    global _FP_CACHE_LOADED, _FP_CACHE_CORRUPT
    if not (repo / ".git").exists() or not sha:
        return None
    fp_file = _fp_cache_file()
    if not _FP_CACHE_LOADED:
        _FP_CACHE_LOADED = True
        data, ok = read_json_safe(fp_file)
        if not ok or (data is not None and not isinstance(data, dict)):
            _FP_CACHE_CORRUPT = True
        elif isinstance(data, dict):
            _FP_CACHE.update(data)
    cache_key = f"{repo.name}:{sha}"
    if cache_key in _FP_CACHE:
        return _FP_CACHE[cache_key]
    ls = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", sha, "op_host", "op_kernel"],
        capture_output=True,
        text=True,
    )
    if ls.returncode != 0 or not ls.stdout.strip():
        _FP_CACHE[cache_key] = None
        return None
    import hashlib

    h = hashlib.sha256()
    for rel in sorted(ls.stdout.splitlines()):
        rel = rel.strip()
        if not rel.endswith((".h", ".cpp", ".cc", ".cxx", ".c")):
            continue
        show = subprocess.run(["git", "-C", str(repo), "show", f"{sha}:{rel}"], capture_output=True, text=True)
        if show.returncode != 0:
            continue
        txt = re.sub(r"/\*.*?\*/", "", show.stdout, flags=re.S)
        txt = re.sub(r"//.*?$", "", txt, flags=re.M)
        txt = re.sub(r"\s+", " ", txt).strip()
        h.update(f"{rel}:{txt}\n".encode("utf-8"))
    digest = h.hexdigest()
    _FP_CACHE[cache_key] = digest
    if not _FP_CACHE_CORRUPT:
        try:
            atomic_write_json(fp_file, _FP_CACHE, indent=None, separators=(",", ":"), trailing_newline=False)
        except Exception:
            pass
    return digest


def parse_validation_perf(log_text: str) -> dict[str, Any]:
    """Extract normalized device-side kernel timings (us) and `RUNTIME_SOC` across all 10 repos.

    Hierarchy of timing fidelity:
      1. `profiler_cases`: hardware `profiler` Task Duration (`MS_PROF`, `KPERF_JSON`, `KPERF`,
         `KERNEL_JSON`, `KERNEL_PERF`, `PERF`).  **Only real case names enter this pool.**  The
         aggregate `PROF_SUMMARY` rows (`kernels.<name>.median_us/min_us`, `custom_median_us`)
         describe a *kernel*, not a case, so they are parked in `prof_kernel_stats` instead: a
         `<kernel>:median` key used to collide with a literal `bench-specs` bucket id and turn a
         parser artifact into a fake "paired" bucket (see `SCHEDULER_DESIGN.md` §6.5.1).
         Hardware always wins over `aclrtRecordEvent` (`dev_us`), which hits a ~12.5 us
         host-enqueue floor on `soc-a`.
      2. `same_soc_exec_cases`: `exec=` timings (`> 0 us`) safe ONLY when paired against a
         same-`RUNTIME_SOC` baseline above the `11.5 us` `soc-a` enqueue floor.
      3. `exec_cases`: compute-bound `exec >= 150.0 us` fallback when comparing across SoC epochs.
      4. `dev_event_cases`: `dev_us` (`aclrtRecordEvent`), used only when `profiler_cases` is empty
         AND `runtime_soc` matches between candidate and baseline.
    """
    profiler_cases: dict[str, float] = {}
    profiler_min_cases: dict[str, float] = {}
    profiler_max_cases: dict[str, float] = {}
    profiler_first_iter_cases: dict[str, float] = {}
    profiler_block_dims: dict[str, int] = {}
    kernel_binary_stats: dict[str, Any] = {}
    cold_start_stats: dict[str, Any] = {}
    torch_cases: dict[str, float] = {}
    same_soc_exec_cases: dict[str, float] = {}
    exec_cases: dict[str, float] = {}
    dev_event_cases: dict[str, float] = {}
    prof_kernel_stats: dict[str, dict[str, float]] = {}
    prof_summary: dict[str, float] = {}
    #: Hardware `profiler` ran at all — set even when only kernel-level aggregates came back, so a
    #: caller can tell "profiling happened, nothing maps to a case" from "no hardware evidence".
    hw_evidence = False
    vs_torch_pct: float | None = None
    runtime_soc: str | None = None
    source_kind: str = "none"

    def _is_valid_case_id(name: str) -> bool:
        return bool(name) and ":" not in name and not name.startswith("__")

    for raw_line in log_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        m_soc = _RUNTIME_SOC_RE.match(line)
        if m_soc:
            runtime_soc = m_soc.group(1).strip().lower()
            continue

        m_ms = _MS_PROF_RE.match(line)
        if m_ms:
            cname = m_ms.group("case")
            if _is_valid_case_id(cname):
                profiler_cases[cname] = float(m_ms.group("med"))
                profiler_min_cases[cname] = float(m_ms.group("min"))
                m_mx = re.search(r"\bmax=([\d.]+)", line)
                if m_mx:
                    profiler_max_cases[cname] = float(m_mx.group(1))
                source_kind = "profiler_spectrum"
            continue

        m_sp = _SPECTRUM_RE.match(line)
        if m_sp:
            vs_torch_pct = float(m_sp.group("vmed"))
            continue

        m_kp = _KPERF_TXT_RE.match(line)
        if m_kp:
            cname = m_kp.group("case")
            if _is_valid_case_id(cname):
                profiler_cases[cname] = float(m_kp.group("med"))
                profiler_min_cases[cname] = float(m_kp.group("min"))
                m_mx = re.search(r"\bmax=\s*([\d.]+)us", line)
                if m_mx:
                    profiler_max_cases[cname] = float(m_mx.group(1))
                if source_kind == "none":
                    source_kind = "kperf_table"
            continue

        for prefix, tag in (
            ("KPERF_JSON ", "kperf_json"),
            ("KERNEL_JSON ", "kernel_json"),
            ("KERNEL_PERF ", "kernel_perf"),
            ("PERF ", "perf_ops"),
            ("PROF_SUMMARY ", "prof_summary"),
            ("PERF_JSON ", "perf_json"),
            ("KERNEL_BINARY_STATS ", "kernel_binary_stats"),
            ("COLD_START_STATS ", "cold_start_stats"),
        ):
            if line.startswith(prefix):
                payload_str = line[len(prefix):].strip()
                try:
                    payload = json.loads(payload_str)
                except ValueError:
                    break

                if tag == "kernel_binary_stats" and isinstance(payload, dict):
                    kernel_binary_stats = dict(payload)
                elif tag == "cold_start_stats" and isinstance(payload, dict):
                    cold_start_stats = dict(payload)
                elif tag == "kperf_json" and isinstance(payload, list):
                    for item in payload:
                        if not isinstance(item, dict):
                            continue
                        cname = str(item.get("case") or item.get("label") or "")
                        med = item.get("median_us")
                        if _is_valid_case_id(cname) and isinstance(med, (int, float)) and med > 0:
                            if item.get("mode") == "torch":
                                torch_cases[cname] = float(med)
                            else:
                                profiler_cases[cname] = float(med)
                                if isinstance(item.get("min_us"), (int, float)) and item["min_us"] > 0:
                                    profiler_min_cases[cname] = float(item["min_us"])
                                if isinstance(item.get("max_us"), (int, float)) and item["max_us"] > 0:
                                    profiler_max_cases[cname] = float(item["max_us"])
                                fi = item.get("first_iter_us") or item.get("cold_us")
                                if isinstance(fi, (int, float)) and fi > 0:
                                    profiler_first_iter_cases[cname] = float(fi)
                                if isinstance(item.get("block_dim"), int) and item["block_dim"] > 0:
                                    profiler_block_dims[cname] = int(item["block_dim"])
                                source_kind = tag
                elif tag == "kernel_json" and isinstance(payload, dict):
                    cname = str(payload.get("case") or payload.get("label") or "")
                    med = payload.get("median_us")
                    if _is_valid_case_id(cname) and isinstance(med, (int, float)) and med > 0:
                        if payload.get("mode") == "torch":
                            torch_cases[cname] = float(med)
                        else:
                            profiler_cases[cname] = float(med)
                            if isinstance(payload.get("min_us"), (int, float)) and payload["min_us"] > 0:
                                profiler_min_cases[cname] = float(payload["min_us"])
                            if isinstance(payload.get("max_us"), (int, float)) and payload["max_us"] > 0:
                                profiler_max_cases[cname] = float(payload["max_us"])
                            fi = payload.get("first_iter_us") or payload.get("cold_us")
                            if isinstance(fi, (int, float)) and fi > 0:
                                profiler_first_iter_cases[cname] = float(fi)
                            if isinstance(payload.get("block_dim"), int) and payload["block_dim"] > 0:
                                profiler_block_dims[cname] = int(payload["block_dim"])
                            source_kind = tag
                elif tag == "kernel_perf" and isinstance(payload, dict):
                    cname = str(payload.get("case") or "")
                    med = payload.get("kernel_median_us")
                    if _is_valid_case_id(cname) and isinstance(med, (int, float)) and med > 0:
                        if payload.get("source") == "torch":
                            torch_cases[cname] = float(med)
                        else:
                            profiler_cases[cname] = float(med)
                            for mk in ("kernel_min_us", "min_us"):
                                if isinstance(payload.get(mk), (int, float)) and payload[mk] > 0:
                                    profiler_min_cases[cname] = float(payload[mk])
                                    break
                            for xk in ("kernel_max_us", "max_us"):
                                if isinstance(payload.get(xk), (int, float)) and payload[xk] > 0:
                                    profiler_max_cases[cname] = float(payload[xk])
                                    break
                            fi = payload.get("first_iter_us") or payload.get("cold_us")
                            if isinstance(fi, (int, float)) and fi > 0:
                                profiler_first_iter_cases[cname] = float(fi)
                            if isinstance(payload.get("block_dim"), int) and payload["block_dim"] > 0:
                                profiler_block_dims[cname] = int(payload["block_dim"])
                            source_kind = tag
                elif tag == "perf_ops" and isinstance(payload, dict):
                    cname = str(payload.get("case") or "")
                    ops = payload.get("ops") or {}
                    if _is_valid_case_id(cname) and isinstance(ops, dict) and ops:
                        first_op = next(iter(ops.values()))
                        med = first_op.get("median_us") if isinstance(first_op, dict) else None
                        if isinstance(med, (int, float)) and med > 0:
                            if payload.get("mode") == "torch":
                                torch_cases[cname] = float(med)
                            else:
                                profiler_cases[cname] = float(med)
                                if isinstance(first_op.get("min_us"), (int, float)) and first_op["min_us"] > 0:
                                    profiler_min_cases[cname] = float(first_op["min_us"])
                                if isinstance(first_op.get("max_us"), (int, float)) and first_op["max_us"] > 0:
                                    profiler_max_cases[cname] = float(first_op["max_us"])
                                fi = first_op.get("first_iter_us") or first_op.get("cold_us")
                                if isinstance(fi, (int, float)) and fi > 0:
                                    profiler_first_iter_cases[cname] = float(fi)
                                source_kind = tag
                elif tag == "prof_summary" and isinstance(payload, dict):
                    # Hardware profiler Task Duration, but *aggregated over kernels*: these are not
                    # case ids.  Park them in `prof_kernel_stats` (additive) instead of
                    # `profiler_cases`, otherwise `f"{kernel}:median"` matches a spec bucket id
                    # literally and manufactures a paired bucket out of a parser key.
                    kernels = payload.get("kernels") or {}
                    if isinstance(kernels, dict) and kernels:
                        for kname, kstat in kernels.items():
                            if not isinstance(kstat, dict):
                                continue
                            stat: dict[str, float] = {}
                            if isinstance(kstat.get("median_us"), (int, float)) and kstat["median_us"] > 0:
                                stat["median_us"] = float(kstat["median_us"])
                            if isinstance(kstat.get("min_us"), (int, float)) and kstat["min_us"] > 0:
                                stat["min_us"] = float(kstat["min_us"])
                            if isinstance(kstat.get("n"), (int, float)):
                                stat["n"] = float(kstat["n"])
                            if stat:
                                prof_kernel_stats[str(kname)] = stat
                        source_kind = "prof_summary"
                        hw_evidence = True
                    if isinstance(payload.get("custom_median_us"), (int, float)) and payload["custom_median_us"] > 0:
                        prof_summary["custom_median_us"] = float(payload["custom_median_us"])
                        source_kind = "prof_summary"
                        hw_evidence = True
                    if isinstance(payload.get("torchref_median_us"), (int, float)) and payload["torchref_median_us"] > 0:
                        prof_summary["torchref_median_us"] = float(payload["torchref_median_us"])
                elif tag == "perf_json" and isinstance(payload, list):
                    for item in payload:
                        if not isinstance(item, dict):
                            continue
                        cname = str(item.get("case") or item.get("label") or "")
                        dev_us = item.get("dev_us")
                        ex_us = item.get("exec_us")
                        if _is_valid_case_id(cname) and isinstance(dev_us, (int, float)) and dev_us > 0:
                            dev_event_cases[cname] = float(dev_us)
                        elif _is_valid_case_id(cname) and isinstance(ex_us, (int, float)) and ex_us > 0:
                            same_soc_exec_cases[cname] = float(ex_us)
                            if ex_us >= 150.0:
                                exec_cases[cname] = float(ex_us)
                break
        else:
            if line.startswith("{") and line.endswith("}") and ("median_us" in line or "dev_us" in line):
                try:
                    item = json.loads(line)
                    cname = str(item.get("case") or item.get("label") or "")
                    if _is_valid_case_id(cname):
                        if isinstance(item.get("median_us"), (int, float)) and item["median_us"] > 0:
                            if item.get("mode") == "torch":
                                torch_cases[cname] = float(item["median_us"])
                            else:
                                profiler_cases.setdefault(cname, float(item["median_us"]))
                                if isinstance(item.get("min_us"), (int, float)) and item["min_us"] > 0:
                                    profiler_min_cases.setdefault(cname, float(item["min_us"]))
                                if isinstance(item.get("max_us"), (int, float)) and item["max_us"] > 0:
                                    profiler_max_cases.setdefault(cname, float(item["max_us"]))
                                if source_kind == "none":
                                    source_kind = "inline_json"
                        elif isinstance(item.get("dev_us"), (int, float)) and item["dev_us"] > 0:
                            dev_event_cases.setdefault(cname, float(item["dev_us"]))
                except ValueError:
                    pass
                continue

            m_ex = _EXEC_ROW_RE.match(line)
            if m_ex:
                val = float(m_ex.group("exec"))
                if val > 0.0:
                    same_soc_exec_cases[m_ex.group("case")] = val
                if val >= 150.0:
                    exec_cases[m_ex.group("case")] = val

    chosen = profiler_cases if profiler_cases else exec_cases
    if not profiler_cases and exec_cases:
        source_kind = "exec_above_floor"

    return {
        "cases": chosen,
        "profiler_cases": profiler_cases,
        "profiler_min_cases": profiler_min_cases,
        "profiler_max_cases": profiler_max_cases,
        "profiler_first_iter_cases": profiler_first_iter_cases,
        "profiler_block_dims": profiler_block_dims,
        "kernel_binary_stats": kernel_binary_stats,
        "cold_start_stats": cold_start_stats,
        "same_soc_exec_cases": same_soc_exec_cases,
        # Legacy reference runs exported process-scoped timings under a pair of
        # SHA-256 digests (protocol and input). Retain other event producers.
        "dev_event_cases": {k: v for k, v in dev_event_cases.items()
                            if not re.fullmatch(r"[0-9a-f]{64}-[0-9a-f]{64}", k)},
        "torch_cases": torch_cases,
        # additive: `PROF_SUMMARY` aggregates are kernel-level evidence, never case ids
        "prof_kernel_stats": prof_kernel_stats,
        "prof_summary": prof_summary,
        "has_hw_evidence": hw_evidence or bool(profiler_cases),
        "vs_torch_pct": vs_torch_pct,
        "runtime_soc": runtime_soc,
        "source": source_kind,
        "is_profiler": bool(profiler_cases),
    }


def _extract_note_speedup(note: str) -> tuple[float | None, int, str]:
    """Conservative parser for commit/queue notes when logs use incompatible schemas."""
    if not note:
        return None, 0, ""

    ratios: list[float] = []
    for m in re.finditer(r"(?<![A-Za-z0-9])(\d+(?:\.\d+)?)\s*->\s*(\d+(?:\.\d+)?)\s*(?:us)?", note):
        tail = note[m.end(): m.end() + 14].lower()
        if re.match(r"^\s*(?:blocks?|cores?|runs?|tiles?|lanes?|elems?|b|kb)\b", tail):
            continue
        before, after = float(m.group(1)), float(m.group(2))
        if before >= 200 and after >= 200 and "us" not in m.group(0).lower() and before < after:
            continue
        if before > 0 and after > 0:
            ratios.append(max(0.25, min(3.5, before / after)))

    has_vs_prev = "vs prev" in note.lower()
    for m in re.finditer(r"(?<![A-Za-z0-9~])(\d+\.\d+)x\b", note):
        after_ctx = note[m.end(): m.end() + 18].lower()
        if has_vs_prev and "vs scalar" in after_ctx:
            continue
        if "faster than" in after_ctx and ratios:
            continue
        val = float(m.group(1))
        if 0.1 <= val <= 50.0:
            ratios.append(max(0.25, min(3.5, val)))

    for m in re.finditer(r"([+-])\s*(\d+(?:\.\d+)?)\s*(?:~\s*(\d+(?:\.\d+)?))?\s*%", note):
        sign = m.group(1)
        v1 = float(m.group(2))
        v2 = float(m.group(3)) if m.group(3) else v1
        pct = 0.5 * (v1 + v2)
        if 0.0 < pct < 95.0:
            if sign == "-":
                ratios.append(max(0.25, min(3.5, 1.0 / (1.0 - pct / 100.0))))
            else:
                ratios.append(max(0.25, min(3.5, 1.0 / (1.0 + pct / 100.0))))

    low = note.lower()
    if "flat" in low or "anchors 1.0x" in low:
        ratios.extend([1.0, 1.0, 1.0])

    if "0 gain" in low or "<=3%" in low:
        return 1.01, 1, "note reports negligible (<=3% / ~0) spectrum gain"

    if not ratios:
        return None, 0, ""

    geo = math.exp(sum(math.log(r) for r in ratios) / len(ratios))
    # Commit/queue notes report cherry-picked microbenchmarks, not 50-case harmonic means:
    # shrink conservatively to avoid +20..+31 pt over-prediction hallucinations.
    coverage = min(0.26, 0.10 + 0.03 * len(ratios))
    k_eff = max(0.80, min(1.16, 1.0 + coverage * (geo - 1.0)))
    return round(k_eff, 4), len(ratios), f"note_parsed({len(ratios)} signals, raw_geo={geo:.2f}x, damped={k_eff:.3f}x)"


def _project_score_from_speedup(
    current_score: float,
    public_score: float,
    hidden_score: float,
    k_eff: float,
    frontier_top1: float,
) -> float:
    """Map effective whole-spectrum speedup k_eff to expected 50-case score."""
    if current_score <= 0.0:
        return 0.0
    if abs(k_eff - 1.0) < 1e-6:
        return current_score

    r_vis = max(0.05, min(0.98, public_score / 10.0)) if public_score > 0 else max(0.05, current_score / 100.0)
    r_hid = max(0.05, min(0.98, hidden_score / 90.0)) if hidden_score > 0 else max(0.05, current_score / 100.0)

    def _sat(r: float, k: float) -> float:
        if k >= 1.0:
            val = (r * k) / (1.0 + 0.35 * r * (k - 1.0))
        else:
            val = r * k
        return max(0.0, min(0.98, val))

    k_vis = 1.0 + 0.40 * (k_eff - 1.0)
    new_vis = 10.0 * _sat(r_vis, k_vis)
    new_hid = 90.0 * _sat(r_hid, k_eff)
    projected = new_vis + new_hid

    hard_cap = min(98.0, max(frontier_top1 + 3.0, current_score + 35.0))
    return round(max(0.0, min(hard_cap, projected)), 2)


class ScoreEstimator:
    """Computes decay-aware problem ledgers, contemporary scores, and candidate estimates."""

    def __init__(
        self,
        workspace: pathlib.Path,
        verified_escapes: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.verified_escapes = verified_escapes if verified_escapes is not None else {}
        self._calibrator: Any = None
        self.problems_meta = self._load_problems()
        self.public_tbest = self._load_public_tbest()
        self.frontier = self._load_frontier()
        self.outcomes = self._load_outcomes()
        self.snapshot = self._load_snapshot()

    def get_calibrator(self) -> Any:
        if self._calibrator is None:
            from .calibration import BenchmarkCalibrator

            self._calibrator = BenchmarkCalibrator(
                self.workspace,
                estimator=self,
                verified_escapes=self.verified_escapes,
            )
        return self._calibrator

    def _load_problems(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        # Clean-agents-first workspaces carry the roster in opsb.workspace.json; the legacy
        # bench/problems.json path is only the fallback. Reading a single hard-coded
        # path made every newly scraped contest fall back to the old 10-problem roster.
        for item in load_problem_manifest(self.workspace):
            title = str(item.get("title") or "")
            if not title:
                continue
            out[title] = dict(item)
            out[title].setdefault("difficulty", PROBLEM_DIFFICULTY.get(title, 2))
        ev_file = self.workspace / "results" / "scoring-evidence" / "problems-all.json"
        if ev_file.is_file():
            try:
                ev = json.loads(ev_file.read_text())
                items = ev.values() if isinstance(ev, dict) else ev
                for v in items:
                    if isinstance(v, dict) and v.get("title") in out and v.get("difficulty"):
                        out[v["title"]]["difficulty"] = int(v["difficulty"])
            except Exception:
                pass
        if not out:
            for title, diff in PROBLEM_DIFFICULTY.items():
                out[title] = {"title": title, "difficulty": diff, "path": f"bench/{title}"}
        return out

    def _load_public_tbest(self) -> dict[str, list[float]]:
        tbest = {k: list(v) for k, v in PUBLIC_TBEST_FALLBACK.items()}
        snap2 = self.workspace / "results" / "scoring-evidence" / "ranking-snapshot-2.json"
        if snap2.is_file():
            try:
                data = json.loads(snap2.read_text()).get("problems", {})
                for title, pdata in data.items():
                    vals = list((pdata.get("tbest") or {}).values())
                    if len(vals) == 5:
                        tbest[title] = [float(x) for x in vals]
            except Exception:
                pass
        # Live 24h auto-refreshed tbest(t) overrides historical snapshot
        live_bt = self.workspace / "results" / "scoring-evidence" / "live-board-tbest.json"
        if live_bt.is_file():
            try:
                data = json.loads(live_bt.read_text()).get("problems", {})
                for title, pdata in data.items():
                    vals = pdata.get("tbest") or []
                    if len(vals) == 5 and all(float(x) > 0 for x in vals):
                        tbest[title] = [float(x) for x in vals]
            except Exception:
                pass
        return tbest

    def _load_frontier(self) -> dict[str, tuple[float, float]]:
        front = dict(FRONTIER_FALLBACK)
        bsum = self.workspace / "results" / "scoring-evidence" / "board-summary.json"
        if bsum.is_file():
            try:
                boards = json.loads(bsum.read_text()).get("boards", {})
                for title, bdata in boards.items():
                    rows = bdata.get("rows") or []
                    if rows:
                        top1 = float(rows[0]["score"])
                        top5 = float(rows[min(4, len(rows) - 1)]["score"])
                        front[title] = (top1, top5)
            except Exception:
                pass
        # Live 24h auto-refreshed frontier Top1/Top5 overrides historical snapshot
        live_bt = self.workspace / "results" / "scoring-evidence" / "live-board-tbest.json"
        if live_bt.is_file():
            try:
                data = json.loads(live_bt.read_text()).get("problems", {})
                for title, pdata in data.items():
                    t1 = pdata.get("frontier_top1")
                    t5 = pdata.get("frontier_top5")
                    if t1 is not None and t5 is not None and float(t1) > 0:
                        front[title] = (float(t1), float(t5))
            except Exception:
                pass
        return front

    def _load_outcomes(self) -> list[dict[str, Any]]:
        path = self.workspace / "results" / "submit-queue-outcome.jsonl"
        if not path.is_file():
            return []
        records: list[dict[str, Any]] = []
        for line in path.read_text(errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                rec["canonical_sha"] = resolve_sha(self.workspace, rec.get("title", ""), rec["sha"]) if rec.get("sha") else ""
                records.append(rec)
            except ValueError:
                continue
        best_idx: dict[str, int] = {}
        best_rank: dict[str, tuple[int, int, int]] = {}
        for idx, rec in enumerate(records):
            sid = str(rec.get("submission_id") or "").strip()
            if not sid:
                continue
            st = str(rec.get("status") or "")
            is_sync_err = st == "sync_error" or (st == "rejected" and ("poll error:" in str(rec.get("detail") or "").lower() or bool(sid)))
            rank = (0 if is_sync_err else 1, int(rec.get("source") != "judge-reconcile"), int(rec.get("score") is not None))
            prev = best_rank.get(sid)
            if prev is None or rank >= prev:
                best_rank[sid] = rank
                best_idx[sid] = idx
        return [
            rec for idx, rec in enumerate(records)
            if not str(rec.get("submission_id") or "").strip() or best_idx.get(str(rec.get("submission_id") or "").strip()) == idx
        ]

    def _load_snapshot(self) -> dict[str, dict[str, Any]]:
        path = self.workspace / "results" / "score-snapshot.json"
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}

    def _filtered_outcomes(self, title: str, as_of: datetime.datetime | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for r in self.outcomes:
            if r.get("title") != title:
                continue
            if as_of is not None:
                ts = _parse_iso(r.get("submitted_at") or r.get("finished_at") or r.get("queued_at"))
                if ts is None or ts >= as_of:
                    continue
            rows.append(r)
        return rows

    def _match_active_submission(
        self,
        title: str,
        rows: list[dict[str, Any]],
        as_of: datetime.datetime | None,
        current_score: float,
    ) -> tuple[str | None, float | None, datetime.datetime | None]:
        """Match live board row (`submission_id` / `submission_time`) to `(active_sha, S_judged, t_active)`."""
        live_rows = [
            r for r in rows
            if str(r.get("status", "")) not in ("rejected", "skipped", "duplicate")
        ]
        if as_of is not None:
            if not live_rows:
                return None, None, None
            last = live_rows[-1]
            csha = last.get("canonical_sha") or last.get("sha")
            sc = float(last["score"]) if isinstance(last.get("score"), (int, float)) and last.get("status") == "Pass" else None
            ts = _parse_iso(last.get("submitted_at") or last.get("finished_at") or last.get("queued_at"))
            return csha, sc, ts

        snap_row = self.snapshot.get(title) or {}
        snap_id = snap_row.get("submission_id")
        snap_ts = _parse_iso(snap_row.get("submission_time"))

        # 1. Exact ID or timestamp match (<60s) against submit-queue-outcome.jsonl
        best_match: dict[str, Any] | None = None
        best_dt = 60.0
        for r in live_rows:
            if snap_id is not None and str(r.get("submission_id", "")) == str(snap_id):
                best_match = r
                break
            if snap_ts is not None and r.get("status") == "Pass":
                rts = _parse_iso(r.get("submitted_at") or r.get("finished_at"))
                if rts is not None:
                    dt = abs((rts - snap_ts).total_seconds())
                    if dt <= best_dt:
                        best_dt = dt
                        best_match = r

        if best_match is not None:
            csha = best_match.get("canonical_sha") or best_match.get("sha")
            sc = float(best_match["score"]) if isinstance(best_match.get("score"), (int, float)) else None
            ts = _parse_iso(best_match.get("submitted_at") or best_match.get("finished_at")) or snap_ts
            return csha, sc, ts

        # 2. Check results/<Title>/submissions.json if numeric ID matches
        sub_json = self.workspace / "results" / title / "submissions.json"
        if snap_id is not None and sub_json.is_file():
            try:
                for srec in json.loads(sub_json.read_text()):
                    if (srec.get("ID") == snap_id or srec.get("_id") == snap_id) and srec.get("validated_commit"):
                        csha = str(srec["validated_commit"]).lower()
                        ts = _parse_iso(srec.get("create_time")) or snap_ts
                        # Check if outcome has a matching SHA score
                        sha_sc = next(
                            (
                                float(r["score"])
                                for r in reversed(live_rows)
                                if (r.get("canonical_sha") or r.get("sha")) == csha
                                and r.get("status") == "Pass"
                                and isinstance(r.get("score"), (int, float))
                            ),
                            current_score if current_score > 0 else None,
                        )
                        return csha, sha_sc, ts
            except Exception:
                pass

        # 3. Fallback to latest Pass record in outcomes
        pass_rows = [
            r for r in live_rows
            if r.get("status") == "Pass" and isinstance(r.get("score"), (int, float)) and r["score"] > 0
        ]
        if pass_rows:
            last_p = pass_rows[-1]
            csha = last_p.get("canonical_sha") or last_p.get("sha")
            sc = float(last_p["score"])
            ts = _parse_iso(last_p.get("submitted_at") or last_p.get("finished_at")) or snap_ts
            return csha, sc, ts

        return None, None, snap_ts

    @staticmethod
    def discount_draw_to_contemporary(
        raw_score: float,
        cand_ts: datetime.datetime | None,
        gamma_p: float,
        anchor_ts: datetime.datetime | None,
    ) -> float:
        """Convert historical score `S(SHA, t_cand)` to contemporary equivalent at `t_now`.

        Applies observed post-active decay `gamma_p = clamp(S_live / S_judged_active, 0.5, 1.0)`
        plus residual drift `-0.015 pts/hr` strictly over any pre-active gap `max(0, t_anchor - t_cand)`.
        """
        if raw_score <= 0.0:
            return 0.0
        gap_hrs = 0.0
        if anchor_ts is not None and cand_ts is not None:
            gap_hrs = max(0.0, (anchor_ts - cand_ts).total_seconds() / 3600.0)
        contemp = raw_score * gamma_p - RESIDUAL_PRE_ACTIVE_DRIFT_HR * gap_hrs
        return round(max(0.0, contemp), 4)

    def discount_historical_score(
        self,
        title: str,
        raw_score: float,
        cand_ts: datetime.datetime | None,
        as_of: datetime.datetime | None = None,
    ) -> float:
        """Public helper (used by CircuitBreaker) to discount any historical score to `as_of`/now."""
        ledger = self.get_problem_ledger(title, as_of=as_of)
        ref_now = as_of or datetime.datetime.now(datetime.timezone.utc)
        anchor_ts = _parse_iso(ledger.active_judged_at) if ledger.current_score > 0 else ref_now
        if anchor_ts is None:
            anchor_ts = ref_now
        return round(
            self.discount_draw_to_contemporary(raw_score, cand_ts, ledger.decay_factor_gamma, anchor_ts),
            2,
        )

    def get_problem_ledger(
        self,
        title: str,
        as_of: datetime.datetime | None = None,
        override_board_score: float | None = None,
        override_board_status: str | None = None,
    ) -> ProblemLedger:
        diff = int(self.problems_meta.get(title, {}).get("difficulty", PROBLEM_DIFFICULTY.get(title, 2)))
        top1, top5 = self.frontier.get(title, (80.0, 65.0))
        rows = self._filtered_outcomes(title, as_of=as_of)
        ref_now = as_of or datetime.datetime.now(datetime.timezone.utc)

        # Collect historical passing (ts, score) draws grouped by canonical SHA
        draws_by_sha: dict[str, list[tuple[datetime.datetime | None, float]]] = {}
        replayed_score: float = 0.0
        replayed_status: str = "no-submission"

        for r in rows:
            st = str(r.get("status", ""))
            if st in ("rejected", "skipped", "duplicate"):
                continue
            csha = r.get("canonical_sha") or r.get("sha")
            sc = float(r["score"]) if isinstance(r.get("score"), (int, float)) else 0.0
            ts = _parse_iso(r.get("submitted_at") or r.get("finished_at") or r.get("queued_at"))
            replayed_status = st
            replayed_score = sc if st == "Pass" else 0.0
            if st == "Pass" and sc > 0 and csha:
                draws_by_sha.setdefault(csha, []).append((ts, sc))

        # Determine live vs replayed board score
        if override_board_score is not None:
            current_score = float(override_board_score)
            board_status = override_board_status or ("Pass" if current_score > 0 else "no-submission")
        elif as_of is not None:
            current_score = replayed_score
            board_status = replayed_status
        else:
            snap_row = self.snapshot.get(title) or {}
            board_status = str(snap_row.get("status") or ("Pass" if replayed_score > 0 else "no-submission"))
            raw_sc = snap_row.get("score")
            current_score = float(raw_sc) if isinstance(raw_sc, (int, float)) and board_status == "Pass" else 0.0

        # Match active board submission and compute observed decay factor gamma_p
        active_sha, active_judged_sc, active_ts = self._match_active_submission(
            title, rows, as_of=as_of, current_score=current_score
        )
        if current_score > 0.0 and active_judged_sc is not None and active_judged_sc > 0.0:
            gamma_p = round(max(0.5, min(1.0, current_score / active_judged_sc)), 4)
            anchor_ts = active_ts or ref_now
        else:
            gamma_p = 1.0
            anchor_ts = ref_now

        # Evaluate raw & contemporary-equivalent scores across all candidate SHAs
        best_sha: str | None = None
        raw_best_peak: float = 0.0
        best_contemp_peak: float = -1.0
        contemp_draws_map: dict[str, list[float]] = {}

        if draws_by_sha:
            for sha_k, sha_draws in draws_by_sha.items():
                raw_mx = max(s for _, s in sha_draws)
                if raw_mx > raw_best_peak:
                    raw_best_peak = raw_mx

                discounted = [
                    self.discount_draw_to_contemporary(s, t_k, gamma_p, anchor_ts)
                    for t_k, s in sha_draws
                ]
                contemp_draws_map[sha_k] = discounted
                sha_peak_c = max(discounted)
                if best_sha is None or sha_peak_c > best_contemp_peak + 1e-6 or (
                    abs(sha_peak_c - best_contemp_peak) <= 1e-6 and sha_k == active_sha
                ):
                    best_contemp_peak = sha_peak_c
                    best_sha = sha_k

        if best_sha is not None and best_sha in contemp_draws_map:
            # Identity Rule: if best_sha IS the active board SHA (in live mode), its contemporary
            # equivalent score is identically S_live(t_now) (true_restore_gain = 0.0).
            if as_of is None and override_board_score is None and best_sha == active_sha and current_score > 0.0:
                contemp_best_score = current_score
            else:
                contemp_best_score = float(statistics.median(contemp_draws_map[best_sha]))
        else:
            contemp_best_score = current_score if current_score > 0.0 else 0.0

        if best_sha is None and as_of is None and title in VERIFIED_BEST_FALLBACK:
            fb_sha, fb_sc = VERIFIED_BEST_FALLBACK[title]
            if (self.workspace / "results" / title / fb_sha / "result.json").is_file():
                best_sha = fb_sha
                raw_best_peak = max(raw_best_peak, fb_sc)
                if fb_sha == active_sha and current_score > 0.0:
                    contemp_best_score = current_score
                else:
                    contemp_best_score = round(fb_sc * gamma_p, 2)

        if current_score > raw_best_peak:
            raw_best_peak = current_score
        if current_score > contemp_best_score:
            contemp_best_score = current_score
            if active_sha:
                best_sha = active_sha

        contemp_best_score = round(contemp_best_score, 2)
        raw_best_peak = round(raw_best_peak, 2)
        true_restore_gain = (
            0.0
            if (best_sha is None or (best_sha == active_sha and current_score > 0.0))
            else round(max(0.0, contemp_best_score - current_score), 2)
        )

        # Decompose public vs hidden score
        pub_tbest = self.public_tbest.get(title, [4.0] * 5)
        snap_times = (self.snapshot.get(title) or {}).get("public_times_us") if as_of is None else None
        public_r_list: list[float] = []
        if current_score > 0 and isinstance(snap_times, list) and len(snap_times) == 5:
            for b_us, t_us in zip(pub_tbest, snap_times):
                r_i = min(1.0, float(b_us) / float(t_us)) if t_us and t_us > 0 else 0.0
                public_r_list.append(round(r_i, 4))
            public_score = round(2.0 * sum(public_r_list), 2)
            hidden_score = round(max(0.0, current_score - public_score), 2)
        elif current_score > 0:
            public_score = round(min(9.0, current_score * 0.13), 2)
            hidden_score = round(max(0.0, current_score - public_score), 2)
            public_r_list = [round(public_score / 10.0, 4)] * 5
        else:
            public_score = 0.0
            hidden_score = 0.0
            public_r_list = [0.0] * 5

        implied_hidden_r = round(hidden_score / 90.0, 4) if hidden_score > 0 else 0.0
        target_score = round(current_score + 20.0 if current_score >= 40.0 else 60.0, 1)

        effective_ceiling = max(top1, contemp_best_score, current_score)
        recoverable_headroom = round(max(0.0, effective_ceiling - current_score), 2)
        theoretical_headroom = round(max(0.0, 100.0 - current_score), 2)

        return ProblemLedger(
            title=title,
            difficulty=diff,
            board_status=board_status,
            current_score=round(current_score, 2),
            weighted_current_score=round(diff * current_score, 2),
            public_score=public_score,
            hidden_score=hidden_score,
            implied_hidden_r=implied_hidden_r,
            public_r_list=public_r_list,
            frontier_top1=top1,
            frontier_top5=top5,
            target_score=target_score,
            recoverable_headroom=recoverable_headroom,
            theoretical_headroom=theoretical_headroom,
            active_board_sha=active_sha,
            active_judged_score=round(active_judged_sc, 2) if active_judged_sc is not None else None,
            active_judged_at=active_ts.isoformat() if active_ts is not None else None,
            decay_factor_gamma=gamma_p,
            raw_historical_best_score=raw_best_peak,
            contemporary_best_score=contemp_best_score,
            historical_best_score=raw_best_peak,
            historical_best_mean=contemp_best_score,
            historical_best_sha=best_sha,
            true_restore_gain=true_restore_gain,
            restore_gap=true_restore_gain,
        )

    def _load_perf_for_sha(self, title: str, sha: str | None) -> dict[str, Any]:
        empty: dict[str, Any] = {
            "cases": {},
            "profiler_cases": {},
            "profiler_min_cases": {},
            "profiler_max_cases": {},
            "profiler_first_iter_cases": {},
            "profiler_block_dims": {},
            "kernel_binary_stats": {},
            "cold_start_stats": {},
            "same_soc_exec_cases": {},
            "dev_event_cases": {},
            "torch_cases": {},
            "prof_kernel_stats": {},
            "prof_summary": {},
            "has_hw_evidence": False,
            "vs_torch_pct": None,
            "runtime_soc": None,
            "source": "none",
            "is_profiler": False,
        }
        if not sha:
            return empty
        vlog = self.workspace / "results" / title / sha / "validation.log"
        if not vlog.is_file():
            return empty
        return parse_validation_perf(vlog.read_text(errors="replace"))

    def _select_same_soc_baseline(
        self,
        title: str,
        cand_sha: str,
        cand_perf: dict[str, Any],
        default_base_sha: str | None,
        as_of: datetime.datetime | None = None,
    ) -> tuple[str | None, dict[str, Any]]:
        """Prefer a baseline validation on the same `RUNTIME_SOC` as `cand_sha`.

        Avoids cross-SoC (`soc-b` vs `soc-a`) host-enqueue floor mismatches
        (~7.0 us vs ~11.1-12.5 us) by matching:
          1. `default_base_sha` itself if validated on `cand_soc`
          2. Any passing `cand_soc` validation whose comment-stripped `op_host`/`op_kernel`
             fingerprint matches `default_base_sha` (e.g. `7926c36`, `b08510f`, `9c743dc`)
          3. Earliest passing `cand_soc` baseline validation finished prior to `cand_sha`
             (e.g. `af5b55a` for `PReluGradUpdate`, `cca8ba3` for `Multilabel`, `4eee530` for `ConcatD`)
        """
        base_perf = (
            self._load_perf_for_sha(title, default_base_sha)
            if default_base_sha and default_base_sha != cand_sha
            else self._load_perf_for_sha(title, None)
        )
        cand_soc = cand_perf.get("runtime_soc")
        if not cand_soc or not default_base_sha or default_base_sha == cand_sha:
            return default_base_sha, base_perf

        if base_perf.get("runtime_soc") == cand_soc:
            return default_base_sha, base_perf

        res_dir = self.workspace / "results" / title
        if not res_dir.is_dir():
            return default_base_sha, base_perf

        # Find cand finished_at timestamp if available so we only pick prior baselines
        cand_rj = res_dir / cand_sha / "result.json"
        cand_ts = None
        if cand_rj.is_file():
            try:
                cand_ts = _parse_iso(json.loads(cand_rj.read_text()).get("finished_at"))
            except Exception:
                pass
        cutoff_ts = as_of or cand_ts

        same_soc_runs: list[tuple[datetime.datetime, str, dict[str, Any]]] = []
        for rj in res_dir.glob("*/result.json"):
            sha_d = rj.parent.name.lower()
            if sha_d == cand_sha or not re.fullmatch(r"[0-9a-f]{40}", sha_d):
                continue
            try:
                rdata = json.loads(rj.read_text())
            except Exception:
                continue
            if rdata.get("exit_code") != 0:
                continue
            fts = _parse_iso(rdata.get("finished_at"))
            if cutoff_ts is not None and fts is not None and fts > cutoff_ts:
                continue
            vlog = rj.parent / "validation.log"
            if not vlog.is_file():
                continue
            txt = vlog.read_text(errors="replace")
            if "EXTENDED_PASSED" not in txt or "FAST_LANE=1" in txt:
                continue
            p_info = parse_validation_perf(txt)
            if p_info.get("runtime_soc") != cand_soc:
                continue
            if not (p_info["profiler_cases"] or p_info["same_soc_exec_cases"] or p_info["dev_event_cases"]):
                continue
            same_soc_runs.append((fts or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc), sha_d, p_info))

        if not same_soc_runs:
            return default_base_sha, base_perf

        same_soc_runs.sort(key=lambda x: (x[0], str(x[1])))
        repo = self.workspace / "bench" / title
        target_fp = _code_fingerprint(repo, default_base_sha)
        if target_fp:
            for _fts, sha_d, p_info in reversed(same_soc_runs):
                if _code_fingerprint(repo, sha_d) == target_fp:
                    return sha_d, p_info

        # If default_base_sha already shares hardware profiler_cases with cand_perf, keep
        # default_base_sha rather than substituting a different C++ implementation!
        if cand_perf.get("profiler_cases") and base_perf.get("profiler_cases"):
            if set(cand_perf["profiler_cases"]).intersection(base_perf["profiler_cases"]):
                return default_base_sha, base_perf

        # Fallback (when default_base_sha lacks profiler or uses cross-SoC dev_us/exec=):
        # pick earliest passing baseline run on cand_soc (e.g. af5b55a for PReluGradUpdate)
        return same_soc_runs[0][1], same_soc_runs[0][2]

    def estimate(
        self,
        title: str,
        sha: str | None = None,
        speedup: float | None = None,
        note: str = "",
        as_of: datetime.datetime | None = None,
        override_board_score: float | None = None,
        override_board_status: str | None = None,
        fast_cached_cal: bool = False,
    ) -> ScoreEstimate:
        """Estimate contemporary online score, expected delta, and uncertainty sigma."""
        canonical_sha = resolve_sha(self.workspace, title, sha)
        short_sha = canonical_sha[:7] if canonical_sha else "unknown"
        ledger = self.get_problem_ledger(
            title,
            as_of=as_of,
            override_board_score=override_board_score,
            override_board_status=override_board_status,
        )
        ref_now = as_of or datetime.datetime.now(datetime.timezone.utc)
        anchor_ts = _parse_iso(ledger.active_judged_at) if ledger.current_score > 0 else ref_now
        if anchor_ts is None:
            anchor_ts = ref_now

        rows = self._filtered_outcomes(title, as_of=as_of)
        prior_same_sha_scores: list[tuple[datetime.datetime | None, float]] = []
        for r in rows:
            if r.get("status") == "Pass" and isinstance(r.get("score"), (int, float)) and r["score"] > 0:
                if (r.get("canonical_sha") or r.get("sha")) == canonical_sha:
                    ts = _parse_iso(r.get("submitted_at") or r.get("finished_at"))
                    prior_same_sha_scores.append((ts, float(r["score"])))

        cand_perf = self._load_perf_for_sha(title, canonical_sha)
        raw_base_sha = ledger.active_board_sha or ledger.historical_best_sha
        base_sha, base_perf = self._select_same_soc_baseline(
            title, canonical_sha, cand_perf, raw_base_sha, as_of=as_of
        )
        same_soc = bool(
            cand_perf.get("runtime_soc")
            and cand_perf.get("runtime_soc") == base_perf.get("runtime_soc")
        )

        # Select highest-fidelity compatible case dictionary between cand_perf and base_perf
        pair_cand_cases: dict[str, float] = {}
        pair_base_cases: dict[str, float] = {}
        pair_kind = cand_perf.get("source", "none")
        exec_floor_us = 0.0

        if cand_perf.get("profiler_cases") and base_perf.get("profiler_cases"):
            pair_cand_cases = cand_perf["profiler_cases"]
            pair_base_cases = base_perf["profiler_cases"]
            pair_kind = cand_perf["source"]
        elif ((cand_perf.get("has_hw_evidence") and not cand_perf.get("profiler_cases"))
              or (base_perf.get("has_hw_evidence") and not base_perf.get("profiler_cases"))):
            # Hardware `profiler` ran but only kernel-level aggregates came back (`PROF_SUMMARY`):
            # they name no case, so there is no A/B pairing.  Do not silently substitute the
            # host-enqueue (`exec=`) or `aclrtRecordEvent` (`dev_us`) floors for them.
            pair_kind = "prof_summary_aggregate"
        elif same_soc and cand_perf.get("runtime_soc") == "soc-a" and cand_perf.get("same_soc_exec_cases") and base_perf.get("same_soc_exec_cases"):
            # Same-SoC exec= pairing on soc-a (> 11.5 us above 11.1 us enqueue floor)
            floor_thresh = 11.5
            exec_floor_us = 11.1
            for k_c in set(cand_perf["same_soc_exec_cases"]).intersection(base_perf["same_soc_exec_cases"]):
                tb = base_perf["same_soc_exec_cases"][k_c]
                tc = cand_perf["same_soc_exec_cases"][k_c]
                if max(tb, tc) > floor_thresh:
                    pair_base_cases[k_c] = max(exec_floor_us, tb)
                    pair_cand_cases[k_c] = max(exec_floor_us, tc)
            pair_kind = f"same_soc_exec({cand_perf['runtime_soc']})"
        elif same_soc and cand_perf.get("dev_event_cases") and base_perf.get("dev_event_cases"):
            floor_thresh = 13.0 if cand_perf.get("runtime_soc") == "soc-a" else 7.8
            for k_c in set(cand_perf["dev_event_cases"]).intersection(base_perf["dev_event_cases"]):
                tb = base_perf["dev_event_cases"][k_c]
                tc = cand_perf["dev_event_cases"][k_c]
                if max(tb, tc) > floor_thresh:
                    pair_base_cases[k_c] = tb
                    pair_cand_cases[k_c] = tc
            pair_kind = f"same_soc_dev_us({cand_perf['runtime_soc']})"
        elif same_soc and cand_perf.get("cases") and base_perf.get("cases"):
            pair_cand_cases = cand_perf["cases"]
            pair_base_cases = base_perf["cases"]

        matched_cases = 0
        improved_cases = 0
        regressed_cases: list[str] = []
        effective_speedup = 1.0
        method = "baseline_prior"
        detail = ""
        cal_trust = 0.5
        explore_eligible = False
        explore_reason = ""
        explore_evidence: dict[str, Any] = {}
        thermal_drift_us = 0.0
        binary_delta_pct: float | None = None
        ucb_delta_override: float | None = None

        # Reference anchor when current board is 0 / no-submission
        anchor_score = ledger.current_score
        anchor_pub = ledger.public_score
        anchor_hid = ledger.hidden_score
        if anchor_score <= 0.0:
            if ledger.contemporary_best_score > 0:
                anchor_score = ledger.contemporary_best_score
            elif title in VERIFIED_BEST_FALLBACK and as_of is None:
                anchor_score = round(VERIFIED_BEST_FALLBACK[title][1] * ledger.decay_factor_gamma, 2)
            else:
                anchor_score = round(0.55 * ledger.frontier_top5, 2)
            anchor_pub = round(min(9.0, anchor_score * 0.13), 2)
            anchor_hid = round(max(0.0, anchor_score - anchor_pub), 2)

        # 1. Explicit caller speedup override (--speedup k)
        if speedup is not None and speedup > 0:
            effective_speedup = float(speedup)
            estimated_score = _project_score_from_speedup(
                anchor_score, anchor_pub, anchor_hid, effective_speedup, ledger.frontier_top1
            )
            sigma = math.sqrt(SIGMA_HIDDEN_JITTER ** 2 + 2.0 ** 2)
            method = "explicit_speedup"
            detail = f"explicit speedup={effective_speedup:.3f}x over anchor={anchor_score:.2f}"

        # 2a. Active-Board Identity Invariant (live mode: candidate_sha == active_board_sha
        #     OR comment-stripped op_host/op_kernel C++ code is identical to active_board_sha!)
        elif (
            as_of is None
            and override_board_score is None
            and canonical_sha
            and ledger.active_board_sha
            and ledger.current_score > 0.0
            and (
                canonical_sha == ledger.active_board_sha
                or (
                    _code_fingerprint(self.workspace / "bench" / title, canonical_sha) is not None
                    and _code_fingerprint(self.workspace / "bench" / title, canonical_sha)
                    == _code_fingerprint(self.workspace / "bench" / title, ledger.active_board_sha)
                )
            )
        ):
            estimated_score = ledger.current_score
            effective_speedup = 1.0
            m = max(1, len(prior_same_sha_scores))
            sigma = round(SIGMA_HIDDEN_JITTER * math.sqrt(1.0 + 1.0 / m), 2)
            method = "active_board_identity"
            same_lbl = (
                f"SHA {short_sha} is already active on board"
                if canonical_sha == ledger.active_board_sha
                else f"SHA {short_sha} is C++-source-identical to active {ledger.active_board_sha[:7]}"
            )
            detail = (
                f"{same_lbl} (S_live={ledger.current_score:.2f}, "
                f"S_judged={ledger.active_judged_score}, gamma={ledger.decay_factor_gamma:.4f}); restore_gain=0.00"
            )

        # 2b. Historical same-SHA restore (Decay-discounted contemporary equivalent)
        elif prior_same_sha_scores:
            m = len(prior_same_sha_scores)
            contemp_draws = [
                self.discount_draw_to_contemporary(s, t_k, ledger.decay_factor_gamma, anchor_ts)
                for t_k, s in prior_same_sha_scores
            ]
            # In live mode use median (robust to single -2sigma outlier); in 2-draw replay median == mean
            estimated_score = round(float(statistics.median(contemp_draws)), 2)
            effective_speedup = round(estimated_score / anchor_score, 4) if anchor_score > 0 else 1.0
            sigma = round(SIGMA_HIDDEN_JITTER * math.sqrt(1.0 + 1.0 / m), 2)
            method = "historical_sha_decay_adjusted"
            raw_str = "/".join(f"{s:.2f}" for _, s in prior_same_sha_scores)
            cnt_str = "/".join(f"{c:.2f}" for c in contemp_draws)
            detail = (
                f"same-SHA {m} draw(s) raw=[{raw_str}] -> contemp=[{cnt_str}] "
                f"(gamma={ledger.decay_factor_gamma:.4f}) => est={estimated_score:.2f}"
            )

        # 3. Calibrated Pairwise Simplex-MAP Bucket Model -> Paired A/B Fallback
        elif (pair_cand_cases and pair_base_cases) or (cand_perf.get("profiler_cases") and base_sha):
            cal_pred = self.get_calibrator().predict_candidate(
                title=title,
                cand_sha=canonical_sha,
                base_sha=base_sha,
                ledger=ledger,
                as_of=as_of,
                fast_cached_cal=fast_cached_cal,
            )
            common = sorted(set(pair_cand_cases).intersection(pair_base_cases))
            if cal_pred is not None:
                estimated_score = cal_pred["estimated_score"]
                sigma = cal_pred["sigma"]
                effective_speedup = cal_pred["effective_speedup"]
                matched_cases = cal_pred["matched_cases"]
                improved_cases = cal_pred["improved_cases"]
                regressed_cases = cal_pred["regressed_cases"]
                method = cal_pred["method"]
                detail = cal_pred["detail"]
                cal_trust = float(cal_pred.get("calibration_trust", 0.5))
                explore_eligible = bool(cal_pred.get("explore_eligible", False))
                explore_reason = str(cal_pred.get("explore_reason", ""))
                explore_evidence = dict(cal_pred.get("explore_evidence") or {})
                thermal_drift_us = float(cal_pred.get("thermal_drift_us", 0.0))
                binary_delta_pct = cal_pred.get("binary_delta_pct")
                if cal_pred.get("ucb_delta") is not None:
                    ucb_delta_override = float(cal_pred["ucb_delta"])
            elif common:
                matched_cases = len(common)
                log_ratios: list[float] = []
                for cname in common:
                    t_b = pair_base_cases[cname]
                    t_c = pair_cand_cases[cname]
                    if t_b <= 0 or t_c <= 0:
                        continue
                    ratio = t_b / t_c
                    if ratio > 1.03:
                        improved_cases += 1
                    elif ratio < 0.95:
                        regressed_cases.append(f"{cname}({ratio:.2f}x)")
                    log_ratios.append(math.log(max(0.25, min(3.5, ratio))))

                raw_geo = math.exp(sum(log_ratios) / len(log_ratios)) if log_ratios else 1.0
                # PROF_SUMMARY aggregates multi-iteration hardware profiler Task Duration directly
                coverage = 0.72 if pair_kind == "prof_summary" else min(0.85, matched_cases / 10.0)
                reg_penalty = 0.03 * len(regressed_cases)
                effective_speedup = max(0.35, min(1.80, 1.0 + coverage * (raw_geo - 1.0) - reg_penalty))
                estimated_score = _project_score_from_speedup(
                    anchor_score, anchor_pub, anchor_hid, effective_speedup, ledger.frontier_top1
                )
                model_err = 1.8 if (cand_perf["is_profiler"] and (matched_cases >= 10 or pair_kind == "prof_summary")) else 2.8
                sigma = round(math.sqrt(SIGMA_HIDDEN_JITTER ** 2 + model_err ** 2), 2)
                method = f"paired_{pair_kind}"
                soc_tag = f" [{cand_perf.get('runtime_soc')}]" if cand_perf.get("runtime_soc") else ""
                detail = (
                    f"{matched_cases} paired cases vs {(base_sha or '')[:7]}{soc_tag}: raw_geo={raw_geo:.3f}x, "
                    f"eff={effective_speedup:.3f}x, improved={improved_cases}, regressed={len(regressed_cases)}"
                )
            else:
                effective_speedup, matched_cases, detail = _extract_note_speedup(note)
                if effective_speedup is None:
                    effective_speedup = 1.0
                    detail = f"unmatched perf schema ({cand_perf['source']} vs {base_perf['source']}); neutral 1.00x"
                estimated_score = _project_score_from_speedup(
                    anchor_score, anchor_pub, anchor_hid, effective_speedup, ledger.frontier_top1
                )
                if matched_cases:
                    # Clamp unverified note-heuristic delta conservatively
                    estimated_score = round(max(anchor_score - 5.0, min(anchor_score + 6.0, estimated_score)), 2)
                sigma = round(math.sqrt(SIGMA_HIDDEN_JITTER ** 2 + 5.0 ** 2), 2)
                cal_trust = 0.25
                method = "note_heuristic" if matched_cases else "unmatched_neutral"

        # 4. Torch reference spectrum ratio or commit/queue note heuristic
        else:
            note_k, n_sig, note_det = _extract_note_speedup(note)
            if note_k is not None:
                effective_speedup = note_k
                matched_cases = n_sig
                estimated_score = _project_score_from_speedup(
                    anchor_score, anchor_pub, anchor_hid, effective_speedup, ledger.frontier_top1
                )
                estimated_score = round(max(anchor_score - 5.0, min(anchor_score + 6.0, estimated_score)), 2)
                sigma = round(math.sqrt(SIGMA_HIDDEN_JITTER ** 2 + 5.0 ** 2), 2)
                cal_trust = 0.25
                method = "note_heuristic"
                detail = note_det
            elif cand_perf["torch_cases"] and cand_perf["cases"]:
                common_t = set(cand_perf["cases"]).intersection(cand_perf["torch_cases"])
                if common_t:
                    ratios_t = [
                        max(0.15, min(2.0, cand_perf["torch_cases"][c] / cand_perf["cases"][c]))
                        for c in common_t
                        if cand_perf["cases"][c] > 0
                    ]
                    geo_t = math.exp(sum(math.log(r) for r in ratios_t) / len(ratios_t))
                    implied_sc = min(ledger.frontier_top1, 58.0 * geo_t)
                    if ledger.current_score <= 0:
                        estimated_score = round(implied_sc, 2)
                    else:
                        estimated_score = round(0.65 * anchor_score + 0.35 * implied_sc, 2)
                    effective_speedup = round(estimated_score / anchor_score, 4) if anchor_score > 0 else 1.0
                    matched_cases = len(common_t)
                    sigma = round(math.sqrt(SIGMA_HIDDEN_JITTER ** 2 + 3.8 ** 2), 2)
                    method = "torch_ref_ratio"
                    detail = f"{len(common_t)} cases vs torch built-in (geo={geo_t:.2f}x)"
                else:
                    estimated_score = round(anchor_score, 2)
                    sigma = round(math.sqrt(SIGMA_HIDDEN_JITTER ** 2 + 4.0 ** 2), 2)
                    method = "anchor_fallback"
                    detail = f"no paired baseline perf; anchored at {anchor_score:.2f}"
            else:
                estimated_score = round(anchor_score, 2)
                sigma = round(math.sqrt(SIGMA_HIDDEN_JITTER ** 2 + 4.0 ** 2), 2)
                method = "anchor_fallback"
                detail = f"no perf telemetry found; anchored at {anchor_score:.2f}"

        delta_expected = round(estimated_score - ledger.current_score, 2)
        weighted_delta = round(ledger.difficulty * delta_expected, 2)
        prob_pos = round(_normal_cdf(delta_expected / sigma), 4) if sigma > 0 else (1.0 if delta_expected > 0 else 0.0)
        ucb_val = (
            round(ucb_delta_override, 2)
            if ucb_delta_override is not None
            else round(delta_expected + (0.85 + 0.55 * (1.0 - cal_trust)) * sigma, 2)
        )

        return ScoreEstimate(
            title=title,
            sha=canonical_sha,
            short_sha=short_sha,
            difficulty=ledger.difficulty,
            board_status=ledger.board_status,
            current_score=ledger.current_score,
            estimated_score=round(estimated_score, 2),
            delta_expected=delta_expected,
            weighted_delta=weighted_delta,
            confidence_sigma=round(sigma, 2),
            prob_positive=prob_pos,
            recoverable_headroom=ledger.recoverable_headroom,
            effective_speedup=round(effective_speedup, 4),
            estimation_method=method,
            baseline_sha=base_sha,
            decay_factor_gamma=ledger.decay_factor_gamma,
            raw_historical_best_score=ledger.raw_historical_best_score,
            contemporary_best_score=ledger.contemporary_best_score,
            true_restore_gain=ledger.true_restore_gain,
            matched_cases=matched_cases,
            improved_cases=improved_cases,
            regressed_cases=regressed_cases,
            detail=detail,
            ucb_delta=ucb_val,
            calibration_trust=round(cal_trust, 3),
            explore_eligible=explore_eligible,
            explore_reason=explore_reason,
            explore_evidence=explore_evidence,
            thermal_drift_us=round(thermal_drift_us, 3),
            binary_delta_pct=round(binary_delta_pct, 2) if binary_delta_pct is not None else None,
        )
