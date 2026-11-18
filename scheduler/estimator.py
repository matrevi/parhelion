"""Expected Score Estimator & Empirical Frontier Ledger.

Models the contest's harmonic efficiency objective:
    score_p = 100 * (1/50) * sum_{i=1..50} (tbest_i / time_i)
            = S_{p,vis} (<= 10) + S_{p,hid} (<= 90)
    contest_total = sum_p (difficulty_p * score_p)   [max 2300]

Incorporates:
  - Exact public-vs-hidden decomposition from `score-snapshot.json` and `tbest`
  - Site-wide top-1 / top-5 frontier headroom from `scoring-evidence/board-summary.json`
  - Universal multi-format `profiler` / `kperf` device-duration log parser (ignoring
    <8.5 us host-enqueue artifacts on `exec` wall-clock)
  - Trimmed geometric spectrum speedup with saturation and regression penalty
  - Empirical Bayes shrinkage for historical same-SHA restores under hidden
    re-timing jitter (sigma = 2.70 pts) and tbest drift (-0.08 pts/hr)
"""
from __future__ import annotations

import dataclasses
import datetime
import json
import math
import pathlib
import re
import subprocess
from typing import Any

# Measured platform constants (from results/scoring-rules.md)
SIGMA_HIDDEN_JITTER: float = 2.70      # pts per re-run on identical source
TBEST_DRIFT_PTS_PER_HR: float = -0.08  # average site-wide hidden tbest decay

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

# Verified closing-checklist fallback SHAs (from results/agent-supervision.md & STATUS)
VERIFIED_BEST_FALLBACK: dict[str, tuple[str, float]] = {
    "ConcatD": ("899a2dc6a824a519c73b0686c69ee9b9f916fb12", 52.87),
    "ConfusionTransposeD": ("fa81ef9b64c2e6abf66f0115f7c1be37b45dd3ce", 43.55),
    "CTCLossV2": ("02c2048c37efc124a0ae221ae0bb03af874ec93e", 37.18),
    "LayerNormV3": ("bf2beb9a968a5b1e94374394903594ee916b1021", 27.13),
    "MultilabelMarginLoss": ("59f80657c0fc0dcf1fbd1e735cf36b2c34651bb3", 56.64),
    "PadV3Grad": ("0fc367de124f4cd43c729b34437daa67a0bc2069", 30.38),
    "PReluGradUpdate": ("33363f0417074ae62c0a5705a6f48539958e5eae", 56.84),
    "QuantUpdateScatter": ("956ecc93c03d7ae8c5e5a9520c4d57f858303b31", 47.15),
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
    historical_best_score: float
    historical_best_mean: float
    historical_best_sha: str | None
    restore_gap: float
    active_board_sha: str | None

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
    matched_cases: int
    improved_cases: int
    regressed_cases: list[str]
    detail: str

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
    if not sha_prefix:
        repo = workspace / "bench" / title
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
    repo = workspace / "bench" / title
    if (repo / ".git").exists():
        res = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", cand],
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


def parse_validation_perf(log_text: str) -> dict[str, Any]:
    """Extract normalized device-side kernel timings (us) across all 10 repo formats.

    Strictly prefers `profiler` Task Duration telemetry (`MS_PROF`, `KPERF_JSON`,
    `KPERF`, `KERNEL_JSON`, `KERNEL_PERF`, `PERF`, `PROF_SUMMARY`, `dev_us`) over
    host `exec` wall-clock, and filters out `exec < 9.0 us` host-enqueue floors.
    """
    profiler_cases: dict[str, float] = {}
    torch_cases: dict[str, float] = {}
    exec_cases: dict[str, float] = {}
    vs_torch_pct: float | None = None
    source_kind: str = "none"

    for raw_line in log_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # 1. MS_PROF key-value lines (e.g. ConcatD 32-case spectrum)
        m_ms = _MS_PROF_RE.match(line)
        if m_ms:
            profiler_cases[m_ms.group("case")] = float(m_ms.group("med"))
            source_kind = "profiler_spectrum"
            continue

        m_sp = _SPECTRUM_RE.match(line)
        if m_sp:
            vs_torch_pct = float(m_sp.group("vmed"))
            continue

        # 2. KPERF tabular lines (e.g. MultilabelMarginLoss)
        m_kp = _KPERF_TXT_RE.match(line)
        if m_kp:
            profiler_cases[m_kp.group("case")] = float(m_kp.group("med"))
            if source_kind == "none":
                source_kind = "kperf_table"
            continue

        # 3. JSON-prefixed telemetry lines
        for prefix, tag in (
            ("KPERF_JSON ", "kperf_json"),
            ("KERNEL_JSON ", "kernel_json"),
            ("KERNEL_PERF ", "kernel_perf"),
            ("PERF ", "perf_ops"),
            ("PROF_SUMMARY ", "prof_summary"),
            ("PERF_JSON ", "perf_json"),
        ):
            if line.startswith(prefix):
                payload_str = line[len(prefix):].strip()
                try:
                    payload = json.loads(payload_str)
                except ValueError:
                    break

                if tag == "kperf_json" and isinstance(payload, list):
                    for item in payload:
                        if not isinstance(item, dict):
                            continue
                        cname = str(item.get("case") or item.get("label") or "")
                        med = item.get("median_us")
                        if cname and isinstance(med, (int, float)) and med > 0:
                            if item.get("mode") == "torch":
                                torch_cases[cname] = float(med)
                            else:
                                profiler_cases[cname] = float(med)
                                source_kind = tag
                elif tag == "kernel_json" and isinstance(payload, dict):
                    cname = str(payload.get("case") or payload.get("label") or "")
                    med = payload.get("median_us")
                    if cname and isinstance(med, (int, float)) and med > 0:
                        if payload.get("mode") == "torch":
                            torch_cases[cname] = float(med)
                        else:
                            profiler_cases[cname] = float(med)
                            source_kind = tag
                elif tag == "kernel_perf" and isinstance(payload, dict):
                    cname = str(payload.get("case") or "")
                    med = payload.get("kernel_median_us")
                    if cname and isinstance(med, (int, float)) and med > 0:
                        if payload.get("source") == "torch":
                            torch_cases[cname] = float(med)
                        else:
                            profiler_cases[cname] = float(med)
                            source_kind = tag
                elif tag == "perf_ops" and isinstance(payload, dict):
                    cname = str(payload.get("case") or "")
                    ops = payload.get("ops") or {}
                    if cname and isinstance(ops, dict) and ops:
                        first_op = next(iter(ops.values()))
                        med = first_op.get("median_us") if isinstance(first_op, dict) else None
                        if isinstance(med, (int, float)) and med > 0:
                            if payload.get("mode") == "torch":
                                torch_cases[cname] = float(med)
                            else:
                                profiler_cases[cname] = float(med)
                                source_kind = tag
                elif tag == "prof_summary" and isinstance(payload, dict):
                    cmed = payload.get("custom_median_us")
                    tmed = payload.get("torchref_median_us")
                    if isinstance(cmed, (int, float)) and cmed > 0:
                        profiler_cases.setdefault("__prof_summary__", float(cmed))
                        if source_kind == "none":
                            source_kind = tag
                    if isinstance(tmed, (int, float)) and tmed > 0:
                        torch_cases.setdefault("__prof_summary__", float(tmed))
                elif tag == "perf_json" and isinstance(payload, list):
                    for item in payload:
                        if not isinstance(item, dict):
                            continue
                        cname = str(item.get("case") or item.get("label") or "")
                        dev_us = item.get("dev_us")
                        ex_us = item.get("exec_us")
                        if cname and isinstance(dev_us, (int, float)) and dev_us > 0:
                            profiler_cases.setdefault(cname, float(dev_us))
                            if source_kind == "none":
                                source_kind = "dev_us"
                        elif cname and isinstance(ex_us, (int, float)) and ex_us >= 150.0:
                            exec_cases[cname] = float(ex_us)
                break
        else:
            # 4. Standalone JSON lines inside KPERF_BEGIN..KPERF_END or PERF dumps
            if line.startswith("{") and line.endswith("}") and ("median_us" in line or "dev_us" in line):
                try:
                    item = json.loads(line)
                    cname = str(item.get("case") or item.get("label") or "")
                    if cname:
                        if isinstance(item.get("median_us"), (int, float)) and item["median_us"] > 0:
                            if item.get("mode") == "torch":
                                torch_cases[cname] = float(item["median_us"])
                            else:
                                profiler_cases.setdefault(cname, float(item["median_us"]))
                                if source_kind == "none":
                                    source_kind = "inline_json"
                        elif isinstance(item.get("dev_us"), (int, float)) and item["dev_us"] > 0:
                            profiler_cases.setdefault(cname, float(item["dev_us"]))
                            if source_kind == "none":
                                source_kind = "dev_us"
                except ValueError:
                    pass
                continue

            # 5. Tabular exec= lines: host Python enqueue jitter reaches 25-105 us under CPU load,
            # so only >= 150 us compute-bound cases carry reliable non-profiler kernel signal.
            m_ex = _EXEC_ROW_RE.match(line)
            if m_ex:
                val = float(m_ex.group("exec"))
                if val >= 150.0:
                    exec_cases[m_ex.group("case")] = val

    chosen = profiler_cases if profiler_cases else exec_cases
    if not profiler_cases and exec_cases:
        source_kind = "exec_above_floor"

    return {
        "cases": chosen,
        "torch_cases": torch_cases,
        "vs_torch_pct": vs_torch_pct,
        "source": source_kind,
        "is_profiler": bool(profiler_cases),
    }


def _extract_note_speedup(note: str) -> tuple[float | None, int, str]:
    """Conservative parser for commit/queue notes when logs use incompatible schemas.

    Parses explicit `A->B us`, `N.NNx`, and `+/-P%` tokens, including flat/regressing
    mentions (`anchors 1.0x`, `big flat`, `+15%`), and applies coverage shrinkage
    so single-band micro-optimizations sdkot masquerade as whole-spectrum gains.
    """
    if not note:
        return None, 0, ""

    ratios: list[float] = []

    # 1. Explicit arrow timings e.g. `222->9.1us`, `6.80->3.46us`, `3805->892`
    used_spans: list[tuple[int, int]] = []
    for m in re.finditer(r"(?<![A-Za-z0-9])(\d+(?:\.\d+)?)\s*->\s*(\d+(?:\.\d+)?)\s*(?:us)?", note):
        # Ignore block/tile/run count changes like `40->16 blocks`, `4096->6144`, `160->45 runs`
        tail = note[m.end(): m.end() + 14].lower()
        if re.match(r"^\s*(?:blocks?|cores?|runs?|tiles?|lanes?|elems?|b|kb)\b", tail):
            continue
        before, after = float(m.group(1)), float(m.group(2))
        if before >= 200 and after >= 200 and "us" not in m.group(0).lower() and before < after:
            # Tile size increase e.g. 4096->6144
            continue
        if before > 0 and after > 0:
            ratios.append(max(0.25, min(3.5, before / after)))
            used_spans.append((m.start(), m.end()))

    # 2. Explicit multipliers e.g. `1.61x`, `0.98x`, `1.0x` (skipping `~27x/48x vs scalar` if incremental `vs prev` exists)
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

    # 3. Percentage deltas e.g. `-5.5%`, `-34%`, `+15%`, `-2~4%`
    for m in re.finditer(r"([+-])\s*(\d+(?:\.\d+)?)\s*(?:~\s*(\d+(?:\.\d+)?))?\s*%", note):
        sign = m.group(1)
        v1 = float(m.group(2))
        v2 = float(m.group(3)) if m.group(3) else v1
        pct = 0.5 * (v1 + v2)
        if 0.0 < pct < 95.0:
            if sign == "-":
                # Latency reduction of pct% => 1 / (1 - pct/100)
                ratios.append(max(0.25, min(3.5, 1.0 / (1.0 - pct / 100.0))))
            else:
                # Latency increase (+15%) => regression 1 / (1 + pct/100)
                ratios.append(max(0.25, min(3.5, 1.0 / (1.0 + pct / 100.0))))

    # 4. Anchor / flat qualifiers ("anchors 1.0x", "big shapes flat", "big flat")
    low = note.lower()
    if "flat" in low or "anchors 1.0x" in low:
        # Weight the un-touched anchor bands (typically >=50% of the 50-case distribution)
        ratios.extend([1.0, 1.0, 1.0])

    if "0 gain" in low or "<=3%" in low:
        return 1.01, 1, "note reports negligible (<=3% / ~0) spectrum gain"

    if not ratios:
        return None, 0, ""

    geo = math.exp(sum(math.log(r) for r in ratios) / len(ratios))
    # Coverage shrinkage: a 1-2 band note only touches a slice of the 45 hidden cases
    coverage = min(0.75, 0.28 + 0.10 * len(ratios))
    k_eff = 1.0 + coverage * (geo - 1.0)
    return round(k_eff, 4), len(ratios), f"note_parsed({len(ratios)} signals, raw_geo={geo:.2f}x, shrunk={k_eff:.3f}x)"


def _project_score_from_speedup(
    current_score: float,
    public_score: float,
    hidden_score: float,
    k_eff: float,
    frontier_top1: float,
) -> float:
    """Map effective whole-spectrum speedup k_eff to expected 50-case score.

    Uses smooth concave per-case saturation r'(k) = (r * k) / (1 + 0.35 * r * max(0, k - 1))
    derived from scoring-rules.md §3.1 so low-r problems (r ~ 0.25) gain proportionally
    while high-r problems (r ~ 0.55+) respect hardware/tbest diminishing returns.
    """
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

    # Public cases (3.4-6.0 us) sit closer to the 1-block dispatch floor, so they move ~40% as much as hidden cases
    k_vis = 1.0 + 0.40 * (k_eff - 1.0)
    new_vis = 10.0 * _sat(r_vis, k_vis)
    new_hid = 90.0 * _sat(r_hid, k_eff)
    projected = new_vis + new_hid

    # Soft cap near empirical frontier + 3 pts
    hard_cap = min(98.0, max(frontier_top1 + 3.0, current_score + 35.0))
    return round(max(0.0, min(hard_cap, projected)), 2)


class ScoreEstimator:
    """Computes problem ledgers, recoverable headroom, and candidate score estimates."""

    def __init__(self, workspace: pathlib.Path) -> None:
        self.workspace = workspace.resolve()
        self.problems_meta = self._load_problems()
        self.public_tbest = self._load_public_tbest()
        self.frontier = self._load_frontier()
        self.outcomes = self._load_outcomes()
        self.snapshot = self._load_snapshot()

    def _load_problems(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        p_file = self.workspace / "bench" / "problems.json"
        if p_file.is_file():
            try:
                for item in json.loads(p_file.read_text()):
                    title = item["title"]
                    out[title] = dict(item)
                    out[title].setdefault("difficulty", PROBLEM_DIFFICULTY.get(title, 2))
            except Exception:
                pass
        # Enrich difficulty from scoring-evidence/problems-all.json if available
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
                rec["canonical_sha"] = resolve_sha(self.workspace, rec.get("title", ""), rec.get("sha"))
                records.append(rec)
            except ValueError:
                continue
        return records

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

        # Historical passing draws grouped by canonical SHA
        by_sha: dict[str, list[float]] = {}
        active_board_sha: str | None = None
        replayed_score: float = 0.0
        replayed_status: str = "no-submission"

        for r in rows:
            st = str(r.get("status", ""))
            if st in ("rejected", "skipped", "duplicate"):
                continue
            csha = r.get("canonical_sha") or r.get("sha")
            sc = float(r["score"]) if isinstance(r.get("score"), (int, float)) else 0.0
            active_board_sha = csha
            replayed_status = st
            replayed_score = sc if st == "Pass" else 0.0
            if st == "Pass" and sc > 0:
                by_sha.setdefault(csha, []).append(sc)

        # Determine live vs historical board state
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

            # Also map snapshot submission_id via results/<Title>/submissions.json if present
            sub_json = self.workspace / "results" / title / "submissions.json"
            snap_id = snap_row.get("submission_id")
            if snap_id and sub_json.is_file():
                try:
                    for srec in json.loads(sub_json.read_text()):
                        if srec.get("ID") == snap_id and srec.get("validated_commit"):
                            active_board_sha = str(srec["validated_commit"]).lower()
                            break
                except Exception:
                    pass

        # Historical best SHA & multi-draw mean
        best_sha: str | None = None
        best_peak: float = 0.0
        best_mean: float = 0.0
        for sha_k, draws in by_sha.items():
            mx = max(draws)
            mn = sum(draws) / len(draws)
            if mx > best_peak:
                best_peak = mx
                best_mean = mn
                best_sha = sha_k

        if best_sha is None and as_of is None and title in VERIFIED_BEST_FALLBACK:
            fb_sha, fb_sc = VERIFIED_BEST_FALLBACK[title]
            if (self.workspace / "results" / title / fb_sha / "result.json").is_file():
                best_sha = fb_sha
                best_peak = fb_sc
                best_mean = fb_sc

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
            # Proportional fallback split (public ~12% of score on average across 10 problems)
            public_score = round(min(9.0, current_score * 0.13), 2)
            hidden_score = round(max(0.0, current_score - public_score), 2)
            public_r_list = [round(public_score / 10.0, 4)] * 5
        else:
            public_score = 0.0
            hidden_score = 0.0
            public_r_list = [0.0] * 5

        implied_hidden_r = round(hidden_score / 90.0, 4) if hidden_score > 0 else 0.0
        target_score = round(current_score + 20.0 if current_score >= 40.0 else 60.0, 1)

        # Frontier-grounded recoverable headroom
        effective_ceiling = max(top1, best_peak, current_score)
        recoverable_headroom = round(max(0.0, effective_ceiling - current_score), 2)
        theoretical_headroom = round(max(0.0, 100.0 - current_score), 2)
        restore_gap = round(max(0.0, best_mean - current_score), 2)

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
            historical_best_score=round(best_peak, 2),
            historical_best_mean=round(best_mean, 2),
            historical_best_sha=best_sha,
            restore_gap=restore_gap,
            active_board_sha=active_board_sha,
        )

    def _load_perf_for_sha(self, title: str, sha: str | None) -> dict[str, Any]:
        if not sha:
            return {"cases": {}, "torch_cases": {}, "vs_torch_pct": None, "source": "none", "is_profiler": False}
        vlog = self.workspace / "results" / title / sha / "validation.log"
        if not vlog.is_file():
            return {"cases": {}, "torch_cases": {}, "vs_torch_pct": None, "source": "none", "is_profiler": False}
        return parse_validation_perf(vlog.read_text(errors="replace"))

    def estimate(
        self,
        title: str,
        sha: str | None = None,
        speedup: float | None = None,
        note: str = "",
        as_of: datetime.datetime | None = None,
        override_board_score: float | None = None,
        override_board_status: str | None = None,
    ) -> ScoreEstimate:
        """Estimate online score, expected delta, and uncertainty sigma for (title, sha)."""
        canonical_sha = resolve_sha(self.workspace, title, sha)
        short_sha = canonical_sha[:7] if canonical_sha else "unknown"
        ledger = self.get_problem_ledger(
            title,
            as_of=as_of,
            override_board_score=override_board_score,
            override_board_status=override_board_status,
        )

        rows = self._filtered_outcomes(title, as_of=as_of)
        prior_same_sha_scores: list[tuple[datetime.datetime | None, float]] = []
        for r in rows:
            if r.get("status") == "Pass" and isinstance(r.get("score"), (int, float)):
                if (r.get("canonical_sha") or r.get("sha")) == canonical_sha:
                    ts = _parse_iso(r.get("submitted_at") or r.get("finished_at"))
                    prior_same_sha_scores.append((ts, float(r["score"])))

        cand_perf = self._load_perf_for_sha(title, canonical_sha)
        base_sha = ledger.active_board_sha or ledger.historical_best_sha
        base_perf = self._load_perf_for_sha(title, base_sha) if base_sha and base_sha != canonical_sha else {
            "cases": {},
            "torch_cases": {},
            "vs_torch_pct": None,
            "source": "none",
            "is_profiler": False,
        }

        matched_cases = 0
        improved_cases = 0
        regressed_cases: list[str] = []
        effective_speedup = 1.0
        method = "baseline_prior"
        detail = ""

        # Reference anchor when current board is 0 / no-submission
        anchor_score = ledger.current_score
        anchor_pub = ledger.public_score
        anchor_hid = ledger.hidden_score
        if anchor_score <= 0.0:
            if ledger.historical_best_mean > 0:
                anchor_score = ledger.historical_best_mean
            elif title in VERIFIED_BEST_FALLBACK and as_of is None:
                anchor_score = VERIFIED_BEST_FALLBACK[title][1]
            else:
                # First-time submission prior (~52% of frontier top-5)
                anchor_score = round(0.55 * ledger.frontier_top5, 2)
            anchor_pub = round(min(9.0, anchor_score * 0.13), 2)
            anchor_hid = round(max(0.0, anchor_score - anchor_pub), 2)

        ref_now = as_of or datetime.datetime.now(datetime.timezone.utc)

        # 1. Explicit caller speedup override (--speedup k)
        if speedup is not None and speedup > 0:
            effective_speedup = float(speedup)
            estimated_score = _project_score_from_speedup(
                anchor_score, anchor_pub, anchor_hid, effective_speedup, ledger.frontier_top1
            )
            sigma = math.sqrt(SIGMA_HIDDEN_JITTER ** 2 + 2.0 ** 2)
            method = "explicit_speedup"
            detail = f"explicit speedup={effective_speedup:.3f}x over anchor={anchor_score:.2f}"

        # 2. Historical same-SHA restore (Empirical Bayes over prior online passes)
        elif prior_same_sha_scores:
            m = len(prior_same_sha_scores)
            mean_prior = sum(s for _, s in prior_same_sha_scores) / m
            last_ts = max((t for t, _ in prior_same_sha_scores if t is not None), default=None)
            hrs = max(0.0, (ref_now - last_ts).total_seconds() / 3600.0) if last_ts else 0.0
            # Active contest tbest drift capped at 12 active hours (-0.96 pts max)
            drift = max(-1.20, TBEST_DRIFT_PTS_PER_HR * min(hrs, 12.0))
            estimated_score = round(max(0.0, mean_prior + drift), 2)
            effective_speedup = round(estimated_score / anchor_score, 4) if anchor_score > 0 else 1.0
            sigma = round(SIGMA_HIDDEN_JITTER * math.sqrt(1.0 + 1.0 / m), 2)
            method = "historical_sha_empirical_bayes"
            draws_str = "/".join(f"{s:.2f}" for _, s in prior_same_sha_scores)
            detail = f"same-SHA {m} prior draw(s) [{draws_str}], mean={mean_prior:.2f}, drift={drift:+.2f}"

        # 3. Paired validation log benchmark A/B (profiler / kperf overlapping cases)
        elif cand_perf["cases"] and base_perf["cases"]:
            common = sorted(set(cand_perf["cases"]).intersection(base_perf["cases"]))
            if common:
                matched_cases = len(common)
                log_ratios: list[float] = []
                for cname in common:
                    t_b = base_perf["cases"][cname]
                    t_c = cand_perf["cases"][cname]
                    if t_b <= 0 or t_c <= 0:
                        continue
                    ratio = t_b / t_c
                    if ratio > 1.03:
                        improved_cases += 1
                    elif ratio < 0.95:
                        regressed_cases.append(f"{cname}({ratio:.2f}x)")
                    log_ratios.append(math.log(max(0.25, min(3.5, ratio))))

                raw_geo = math.exp(sum(log_ratios) / len(log_ratios)) if log_ratios else 1.0
                # Coverage shrinkage if fewer than 8 cases, plus penalty for small/mid band regressions
                coverage = min(1.0, matched_cases / 8.0)
                reg_penalty = 0.03 * len(regressed_cases)
                effective_speedup = max(0.30, 1.0 + coverage * (raw_geo - 1.0) - reg_penalty)
                estimated_score = _project_score_from_speedup(
                    anchor_score, anchor_pub, anchor_hid, effective_speedup, ledger.frontier_top1
                )
                model_err = 1.5 if (cand_perf["is_profiler"] and matched_cases >= 10) else 2.5
                sigma = round(math.sqrt(SIGMA_HIDDEN_JITTER ** 2 + model_err ** 2), 2)
                method = f"paired_{cand_perf['source']}"
                detail = (
                    f"{matched_cases} paired cases vs {base_sha[:7]}: raw_geo={raw_geo:.3f}x, "
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
                sigma = round(math.sqrt(SIGMA_HIDDEN_JITTER ** 2 + 3.5 ** 2), 2)
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
                sigma = round(math.sqrt(SIGMA_HIDDEN_JITTER ** 2 + 3.5 ** 2), 2)
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
                    # Torch built-in typically scores ~55-68% of site-wide specialist tbest
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
            matched_cases=matched_cases,
            improved_cases=improved_cases,
            regressed_cases=regressed_cases,
            detail=detail,
        )
