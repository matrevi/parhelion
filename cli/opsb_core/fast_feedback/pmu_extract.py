"""Markdown-table & profiler PMU log extraction helpers for `pmu_torch_card`."""
from __future__ import annotations

import json
import re
from typing import Any

from .common import _to_ratio

_PMU_ALIAS_SPECS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("aiv_vec_ratio", "vec_ratio", "pipe_v", "PIPE_V"), "aiv_vec_ratio"),
    (("aiv_mte2_ratio", "mte2_ratio", "MTE2"), "aiv_mte2_ratio"),
    (("aiv_mte3_ratio", "mte3_ratio", "MTE3"), "aiv_mte3_ratio"),
    (("aiv_scalar_ratio", "scalar_ratio", "aiv_scalar_time_ratio", "Scalar"), "aiv_scalar_ratio"),
    (
        (
            "aiv_ub_bank_conflict", "bank_conflict_ratio", "bank_conflict", "BankConflict",
            "ResourceConflictRatio", "resource_conflict_ratio",
            "vec_bankgroup_conflict_ratio", "vec_bank_conflict_ratio",
        ),
        "bank_conflict_ratio",
    ),
)

_INLINE_PMU_PATS: tuple[tuple[str, str], ...] = (
    (r"\b(?:aiv_vec_ratio|vec_ratio|pipe_v|PIPE_V)=([\d.]+%?)", "aiv_vec_ratio"),
    (r"\b(?:aiv_mte2_ratio|mte2_ratio|MTE2)=([\d.]+%?)", "aiv_mte2_ratio"),
    (r"\b(?:aiv_mte3_ratio|mte3_ratio|MTE3)=([\d.]+%?)", "aiv_mte3_ratio"),
    (r"\b(?:aiv_scalar_ratio|scalar_ratio|Scalar)=([\d.]+%?)", "aiv_scalar_ratio"),
    (r"\b(?:aiv_ub_bank_conflict|bank_conflict_ratio|bank_conflict|ResourceConflictRatio|BankConflict)=([\d.]+%?)", "bank_conflict_ratio"),
)

_PIPE_KEYS: tuple[str, ...] = ("aiv_vec_ratio", "aiv_mte2_ratio", "aiv_mte3_ratio", "aiv_scalar_ratio")


def _extract_pmu_and_torch(log_text: str) -> tuple[dict[str, float], dict[str, float], dict[str, Any]]:
    """Extract markdown-table timings/torch refs + per-case & aggregate PMU pipe ratios."""
    extra_ours: dict[str, float] = {}
    extra_torch: dict[str, float] = {}
    per_case_pmu: dict[str, dict[str, float]] = {}
    buckets: dict[str, list[float]] = {dest: [] for _, dest in _PMU_ALIAS_SPECS}
    zero_dummy_samples = 0

    def _ingest_pmu_dict(d: dict[str, Any], cname: str = "") -> None:
        nonlocal zero_dummy_samples
        if not isinstance(d, dict):
            return
        sub_dicts = [d, *(d[k] for k in ("pipe", "pmu", "metrics", "PipeUtilization") if isinstance(d.get(k), dict))]
        rec: dict[str, float] = {}
        for sd in sub_dicts:
            for aliases, dest in _PMU_ALIAS_SPECS:
                if dest in rec:
                    continue
                for alias in aliases:
                    if alias in sd:
                        rv = _to_ratio(sd[alias])
                        if rv is not None and rv >= 0.0:
                            rec[dest] = rv
                            break
        if not rec:
            return
        pipe_vals = [rec[k] for k in _PIPE_KEYS if k in rec]
        if pipe_vals and all(v == 0.0 for v in pipe_vals) and rec.get("bank_conflict_ratio", 0.0) <= 0.0:
            zero_dummy_samples += 1
            return
        for dest, rv in rec.items():
            buckets[dest].append(rv)
        if cname:
            per_case_pmu[cname] = rec

    md_cols: list[str] | None = None
    for raw in log_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("|") and line.endswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(re.fullmatch(r":?-+:?", c) for c in cells if c):
                continue
            low_cells = [c.lower() for c in cells]
            if "case" in low_cells and any(k in low_cells for k in ("med_us", "ours_us", "median_us", "torch_med", "torch_us")):
                md_cols = low_cells
                continue
            if md_cols and len(cells) >= len(md_cols):
                row_map = dict(zip(md_cols, cells))
                cname = row_map.get("case", "")
                if cname and ":" not in cname and not cname.startswith("__"):
                    for keys, dest_map in (
                        (("med_us", "ours_us", "median_us", "custom_us"), extra_ours),
                        (("torch_med", "torch_us", "tau_torch_us", "torch_min"), extra_torch),
                    ):
                        for k in keys:
                            if k in row_map:
                                try:
                                    v = float(row_map[k].rstrip("us"))
                                    if v > 0:
                                        dest_map.setdefault(cname, v)
                                        break
                                except ValueError:
                                    pass
                continue
        else:
            md_cols = None

        for prefix in ("PERF ", "KPERF_JSON ", "KERNEL_JSON ", "KERNEL_PERF ", "PROF_SUMMARY ", "PMU_JSON ", "PROFILER_PMU "):
            if line.startswith(prefix):
                try:
                    payload = json.loads(line[len(prefix):].strip())
                except ValueError:
                    break
                items = payload if isinstance(payload, list) else ([payload] if isinstance(payload, dict) else [])
                for it in items:
                    if not isinstance(it, dict) or it.get("mode") == "torch" or it.get("source") == "torch":
                        continue
                    cname = str(it.get("case") or it.get("label") or "")
                    for tk in ("torch_us", "tau_torch_us"):
                        if isinstance(it.get(tk), (int, float)) and float(it[tk]) > 0 and cname:
                            extra_torch.setdefault(cname, float(it[tk]))
                    _ingest_pmu_dict(it, cname)
                    if isinstance(it.get("ops"), dict):
                        for op_val in it["ops"].values():
                            if isinstance(op_val, dict):
                                _ingest_pmu_dict(op_val, cname)
                break
        else:
            m_case = re.search(r"\bcase=(\S+)", line)
            cname = m_case.group(1) if m_case else ""
            m_t = re.search(r"\b(?:torch_us|tau_torch_us|torch)=([\d.]+)", line)
            if m_t and cname:
                try:
                    tv = float(m_t.group(1))
                    if tv > 0:
                        extra_torch.setdefault(cname, tv)
                except ValueError:
                    pass
            pmu_inline = {key: m.group(1) for pat, key in _INLINE_PMU_PATS if (m := re.search(pat, line))}
            if pmu_inline:
                _ingest_pmu_dict(pmu_inline, cname)

    def _mean(vals: list[float]) -> float | None:
        return round(sum(vals) / len(vals), 4) if vals else None

    bank_mean = _mean(buckets["bank_conflict_ratio"])
    pipe_means = [v for k in _PIPE_KEYS if (v := _mean(buckets[k])) is not None]
    pmu_available = bool(
        (pipe_means and any(v > 0.0 for v in pipe_means))
        or (isinstance(bank_mean, (int, float)) and bank_mean > 0.0)
    )
    pmu_summary = {
        "pmu_available": pmu_available,
        "aiv_vec_ratio": _mean(buckets["aiv_vec_ratio"]) if pmu_available else None,
        "aiv_mte2_ratio": _mean(buckets["aiv_mte2_ratio"]) if pmu_available else None,
        "aiv_mte3_ratio": _mean(buckets["aiv_mte3_ratio"]) if pmu_available else None,
        "aiv_scalar_ratio": _mean(buckets["aiv_scalar_ratio"]) if pmu_available else None,
        "bank_conflict_ratio": bank_mean if pmu_available else None,
        "aiv_ub_bank_conflict": bank_mean if pmu_available else None,
        "samples": max((len(v) for v in buckets.values()), default=0) if pmu_available else 0,
        "zero_dummy_samples": zero_dummy_samples,
        "per_case": per_case_pmu if pmu_available else {},
    }
    return extra_ours, extra_torch, pmu_summary
