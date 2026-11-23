"""3-run cold-start latency & 10-core DVFS bimodal spike probe (`evaluate_cold3_probe`)."""
from __future__ import annotations

import json
import re
import statistics
from pathlib import Path
from typing import Any

from .common import DEFAULT_COLD3_THRESHOLDS, _load_fast_tools_settings, _load_run_artifacts, _parse_validation_perf_safe, _to_float_list


def _extract_cold3_samples(log_text: str, rj: dict[str, Any]) -> dict[str, Any]:
    """Parse COLD3_STATS, COLD_START_STATS, COLD3_CASE, and per-case 3-run samples."""
    cold_start_stats: dict[str, Any] = {}
    cold3_stats: dict[str, Any] = {}
    explicit_3runs: dict[str, list[float]] = {}
    first_iters: dict[str, float] = {}
    medians: dict[str, float] = {}
    mins: dict[str, float] = {}
    maxs: dict[str, float] = {}
    block_dims: dict[str, int] = {}

    def _ingest_case_item(cname: str, item: dict[str, Any]) -> None:
        if not cname or ":" in cname or cname.startswith("__"):
            return
        if item.get("mode") == "torch" or item.get("source") == "torch":
            return
        bdim_raw = item.get("block_dim") if item.get("block_dim") is not None else item.get("blockDim")
        if isinstance(bdim_raw, (int, float)) and not isinstance(bdim_raw, bool) and int(bdim_raw) > 0:
            block_dims[cname] = int(bdim_raw)
        if isinstance(item.get("ops"), dict) and item["ops"]:
            sub = next(iter(item["ops"].values()))
            if isinstance(sub, dict):
                merged = dict(sub)
                for dst_k, src_v in (
                    ("median_us", item.get("median_us")),
                    ("min_us", item.get("min_us")),
                    ("max_us", item.get("max_us")),
                    ("first_iter_us", item.get("first_iter_us") or item.get("cold_us")),
                    ("cold3_runs", item.get("cold3_runs") or item.get("runs_us") or item.get("runs")),
                ):
                    merged.setdefault(dst_k, src_v)
                item = merged

        runs = (
            _to_float_list(item.get("cold3_runs"))
            or _to_float_list(item.get("runs_us"))
            or _to_float_list(item.get("runs"))
            or _to_float_list(item.get("raw_runs"))
        )
        if not runs:
            dur = _to_float_list(item.get("durations_us")) or _to_float_list(item.get("all_us"))
            if len(dur) >= 3 and dur != sorted(dur):
                runs = dur[:3]
        if not runs and all(isinstance(item.get(k), (int, float)) for k in ("run1_us", "run2_us", "run3_us")):
            r1, r2, r3 = float(item["run1_us"]), float(item["run2_us"]), float(item["run3_us"])
            if r1 > 0 and r2 > 0 and r3 > 0:
                runs = [r1, r2, r3]
        if len(runs) >= 3:
            explicit_3runs[cname] = runs[:3]
            first_iters[cname], mins[cname], maxs[cname] = runs[0], min(runs[:3]), max(runs[:3])
            medians.setdefault(cname, statistics.median(runs[:3]))

        for val, target in (
            (item.get("first_iter_us") or item.get("cold_us"), first_iters),
            (item.get("median_us") or item.get("kernel_median_us"), medians),
            (item.get("min_us") or item.get("kernel_min_us"), mins),
            (item.get("max_us") or item.get("kernel_max_us"), maxs),
        ):
            if isinstance(val, (int, float)) and float(val) > 0:
                target[cname] = float(val)

    def _ingest_per_case_map(per_case: Any, only_missing: bool = False) -> None:
        if not isinstance(per_case, dict):
            return
        for cname, val in per_case.items():
            if only_missing and cname in explicit_3runs:
                continue
            if isinstance(val, list):
                fl = _to_float_list(val)
                if len(fl) >= 3:
                    explicit_3runs[str(cname)] = fl[:3]
            elif isinstance(val, dict):
                _ingest_case_item(str(cname), val)

    for raw in log_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(("COLD3_STATS ", "COLD3_CASE ", "COLD_START_STATS ")):
            tag, _, body = line.partition(" ")
            try:
                obj = json.loads(body.strip())
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            if tag == "COLD3_STATS":
                cold3_stats = obj
                _ingest_per_case_map(obj.get("per_case"))
            elif tag == "COLD3_CASE":
                _ingest_case_item(str(obj.get("case") or obj.get("label") or ""), obj)
            else:
                cold_start_stats = obj
            continue
        for prefix in ("KPERF_JSON ", "KERNEL_JSON ", "KERNEL_PERF ", "PERF "):
            if line.startswith(prefix):
                try:
                    payload = json.loads(line[len(prefix):].strip())
                except ValueError:
                    break
                items = payload if isinstance(payload, list) else ([payload] if isinstance(payload, dict) else [])
                for it in items:
                    if isinstance(it, dict):
                        _ingest_case_item(str(it.get("case") or it.get("label") or ""), it)
                break
        else:
            m_row = re.match(r"^MS_PROF\s+case=(\S+)(.*)$", line) or re.match(r"^KPERF\s+(\S+)(.*)$", line)
            if m_row:
                cname, rest = m_row.group(1), m_row.group(2)
                if ":" not in cname and not cname.startswith("__"):
                    for pat, target in (
                        (r"\bmedian=\s*([\d.]+)", medians),
                        (r"\bmin=\s*([\d.]+)", mins),
                        (r"\bmax=\s*([\d.]+)", maxs),
                        (r"\bfirst(?:_iter)?=\s*([\d.]+)", first_iters),
                    ):
                        m_val = re.search(pat, rest)
                        if m_val:
                            target[cname] = float(m_val.group(1))
            elif line.startswith("{") and line.endswith("}"):
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        _ingest_case_item(str(obj.get("case") or obj.get("label") or ""), obj)
                except ValueError:
                    pass

    if isinstance(rj.get("cold3_stats"), dict):
        for k, v in rj["cold3_stats"].items():
            cold3_stats.setdefault(k, v)
    _ingest_per_case_map(cold3_stats.get("per_case"), only_missing=True)

    return {
        "cold_start_stats": cold_start_stats,
        "cold3_stats": cold3_stats,
        "explicit_3runs": explicit_3runs,
        "first_iters": first_iters,
        "medians": medians,
        "mins": mins,
        "maxs": maxs,
        "block_dims": block_dims,
    }


def _first_float(*vals: Any) -> float | None:
    for v in vals:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def evaluate_cold3_probe(workspace: Path, title: str, sha: str | None = None) -> dict[str, Any]:
    """Quantify true un-warmed 3-run cold-start latency and 10-core DVFS bimodal spikes."""
    ws = Path(workspace).expanduser().resolve()
    art = _load_run_artifacts(ws, title, sha=sha, require_passing=False, allow_build_logs=False)
    rj, log_text = art["result_json"], art["log_text"]

    eff_thresh = dict(DEFAULT_COLD3_THRESHOLDS)
    cfg_c3 = _load_fast_tools_settings().get("cold3_probe")
    if isinstance(cfg_c3, dict):
        for k, v in cfg_c3.items():
            if k in eff_thresh and isinstance(v, (int, float)):
                eff_thresh[k] = float(v)

    ex = _extract_cold3_samples(log_text, rj)
    parsed_perf = _parse_validation_perf_safe(log_text)
    block_dims: dict[str, int] = dict(parsed_perf.get("profiler_block_dims") or {})
    block_dims.update(ex.get("block_dims") or {})
    c3s, css = ex["cold3_stats"], ex["cold_start_stats"]
    explicit_3runs: dict[str, list[float]] = ex["explicit_3runs"]
    first_iters, medians, mins, maxs = ex["first_iters"], ex["medians"], ex["mins"], ex["maxs"]
    spike_ratio_thr, spike_diff_thr = eff_thresh["spike_ratio"], eff_thresh["spike_diff_us"]

    spike_cases: list[dict[str, Any]] = []
    jitter_cases: list[dict[str, Any]] = []
    per_case_means: list[float] = []
    per_case_spreads: list[float] = []
    per_case_firsts: list[float] = []

    if explicit_3runs:
        for cname, runs in sorted(explicit_3runs.items()):
            r1, r2, r3 = runs[0], runs[1], runs[2]
            per_case_firsts.append(r1)
            per_case_means.append((r1 + r2 + r3) / 3.0)
            per_case_spreads.append(max(runs) - min(runs))
            if r3 > 0 and (r1 / r3) >= spike_ratio_thr and (r1 - r3) >= spike_diff_thr:
                spike_cases.append({"case": cname, "run1_us": round(r1, 3), "run2_us": round(r2, 3), "run3_us": round(r3, 3), "ratio": round(r1 / r3, 3), "diff_us": round(r1 - r3, 3), "block_dim": block_dims.get(cname)})
    else:
        for cname in sorted(set(first_iters) | set(maxs)):
            r1 = first_iters.get(cname) or maxs.get(cname)
            r3 = mins.get(cname) or medians.get(cname)
            if r1 and r3 and r3 > 0 and (r1 / r3) >= spike_ratio_thr and (r1 - r3) >= spike_diff_thr:
                r2 = medians.get(cname, r3)
                jitter_cases.append({"case": cname, "max_or_first_us": round(r1, 3), "median_us": round(r2, 3), "min_us": round(r3, 3), "ratio": round(r1 / r3, 3), "diff_us": round(r1 - r3, 3), "synthetic_order": True, "block_dim": block_dims.get(cname)})

    explicit_first_iter = _first_float(
        c3s.get("first_iter_us"),
        statistics.median(per_case_firsts) if per_case_firsts else None,
        statistics.median(first_iters.values()) if first_iters else None,
        css.get("first_iter_us"),
        rj.get("first_iter_us"),
    )
    first_iter_us = _first_float(
        explicit_first_iter,
        statistics.median(maxs.values()) if maxs else None,
    )
    has_explicit_first_iter = explicit_first_iter is not None
    warm_median_us = _first_float(
        c3s.get("warm_median_us"),
        statistics.median(medians.values()) if medians else None,
        statistics.median(min(r[1], r[2]) for r in explicit_3runs.values()) if explicit_3runs else None,
        statistics.median(mins.values()) if mins else None,
    )
    cold_3run_mean_us = _first_float(
        c3s.get("cold_3run_mean_us"),
        rj.get("cold_3run_mean_us"),
        statistics.median(per_case_means) if per_case_means else None,
        ((first_iter_us + 2.0 * warm_median_us) / 3.0) if (first_iter_us is not None and warm_median_us is not None) else None,
    )
    cold_tax_us = round(cold_3run_mean_us - warm_median_us, 3) if (cold_3run_mean_us is not None and warm_median_us is not None) else None
    spreads_fb = [maxs[c] - mins[c] for c in maxs if c in mins and maxs[c] >= mins[c]]
    cold_spread_median_us = _first_float(
        c3s.get("cold_spread_median_us"),
        statistics.median(per_case_spreads) if per_case_spreads else None,
        css.get("cold_spread_median_us"),
        rj.get("cold_spread_median_us"),
        statistics.median(spreads_fb) if spreads_fb else None,
    )

    synthetic_order = bool(not explicit_3runs and (jitter_cases or maxs or medians))
    bimodal_spike_detected = bool(explicit_3runs and (c3s.get("bimodal_spike_detected") or spike_cases))
    synthetic_jitter_detected = bool(
        not explicit_3runs
        and (
            jitter_cases
            or (
                first_iter_us is not None and warm_median_us is not None and warm_median_us > 0
                and (first_iter_us / warm_median_us) >= spike_ratio_thr
                and (first_iter_us - warm_median_us) >= spike_diff_thr
            )
        )
    )

    def _trigger_block_dim(top_case: str | None) -> int | None:
        if top_case and top_case in block_dims:
            return block_dims[top_case]
        unique_dims = set(block_dims.values())
        return next(iter(unique_dims)) if len(unique_dims) == 1 else (10 if 10 in unique_dims and not unique_dims - {10} else None)

    alerts: list[dict[str, Any]] = []
    if bimodal_spike_detected:
        top_sc = spike_cases[0] if spike_cases else None
        bdim = _trigger_block_dim(top_sc["case"] if top_sc else None)
        detail = (
            f"case={top_sc['case']} ({top_sc['run1_us']}us -> {top_sc['run3_us']}us, {top_sc['ratio']}x)"
            if top_sc else f"first_iter={first_iter_us:.2f}us vs warm={warm_median_us:.2f}us"
        )
        msg = (
            f"10-core DVFS bimodal spike detected: {detail}."
            if bdim == 10
            else f"Cold-start bimodal spike detected (block_dim={bdim if bdim is not None else 'unknown'}): {detail}."
        )
        alerts.append({
            "code": "DVFS_BIMODAL_SPIKE",
            "severity": "redline",
            **({"block_dim": bdim} if bdim is not None else {}),
            "message": msg,
            "suggested_kb_tags": (
                ["dvfs-10core-spike", "zero-tpipe", "direct-gm-tiling"]
                if (bdim == 10 or bdim is None)
                else ["zero-tpipe", "direct-gm-tiling", "elf-diet"]
            ),
        })
    elif synthetic_jitter_detected:
        top_jc = jitter_cases[0] if jitter_cases else None
        bdim = _trigger_block_dim(top_jc["case"] if top_jc else None)
        bdim_lbl = str(bdim) if bdim is not None else "unknown"
        detail = (
            f"case={top_jc['case']} (max/1st={top_jc['max_or_first_us']}us vs min={top_jc['min_us']}us, {top_jc['ratio']}x)"
            if top_jc else f"max_or_1st={first_iter_us:.2f}us vs warm={warm_median_us:.2f}us"
        )
        alerts.append({
            "code": "SPREAD_JITTER_WARN",
            "severity": "warn",
            "synthetic_order": True,
            **({"block_dim": bdim} if bdim is not None else {}),
            "message": f"Unordered max/med/min spread jitter detected (block_dim={bdim_lbl}): {detail} (no chronological Run1->Run2->Run3 cold3 data).",
            "suggested_kb_tags": ["dvfs-10core-spike", "zero-tpipe", "direct-gm-tiling"] if bdim == 10 else ["zero-tpipe", "direct-gm-tiling"],
        })

    if cold_tax_us is not None and (explicit_3runs or has_explicit_first_iter):
        if cold_tax_us >= eff_thresh["redline_cold_tax_us"]:
            alerts.append({"code": "COLD_LAUNCH_TAX_HIGH", "severity": "redline", "message": f"Cold 3-run tax +{cold_tax_us:.3f} us exceeds +{eff_thresh['redline_cold_tax_us']:.1f} us redline.", "suggested_kb_tags": ["zero-tpipe", "direct-gm-tiling", "elf-diet"]})
        elif cold_tax_us >= eff_thresh["warn_cold_tax_us"]:
            alerts.append({"code": "COLD_LAUNCH_TAX_WARN", "severity": "warn", "message": f"Cold 3-run tax +{cold_tax_us:.3f} us exceeds +{eff_thresh['warn_cold_tax_us']:.1f} us warning threshold.", "suggested_kb_tags": ["zero-tpipe", "direct-gm-tiling"]})

    rel_spread = (cold_spread_median_us / max(1.0, warm_median_us)) if (cold_spread_median_us is not None and warm_median_us is not None and warm_median_us > 0) else 1.0
    if cold_spread_median_us is not None and not bimodal_spike_detected and not synthetic_jitter_detected and rel_spread >= 0.12:
        has_10core = 10 in block_dims.values()
        spread_tags = ["dvfs-10core-spike", "half-core-1-20-40"] if has_10core else ["half-core-1-20-40", "zero-tpipe"]
        if (explicit_3runs or has_explicit_first_iter) and cold_spread_median_us >= eff_thresh["redline_spread_us"]:
            alerts.append({"code": "COLD_SPREAD_HIGH", "severity": "redline", "message": f"Cold-to-warm spread {cold_spread_median_us:.3f} us exceeds {eff_thresh['redline_spread_us']:.1f} us redline.", "suggested_kb_tags": spread_tags})
        elif cold_spread_median_us >= eff_thresh["warn_spread_us"]:
            alerts.append({"code": "COLD_SPREAD_WARN", "severity": "warn", "message": f"Cold-to-warm spread {cold_spread_median_us:.3f} us exceeds {eff_thresh['warn_spread_us']:.1f} us warning threshold.", "suggested_kb_tags": ["dvfs-10core-spike"] if has_10core else ["zero-tpipe"]})

    status = "redline" if any(a["severity"] == "redline" for a in alerts) else ("warn" if any(a["severity"] == "warn" for a in alerts) else "ok")
    metrics = {
        "first_iter_us": round(first_iter_us, 3) if first_iter_us is not None else None,
        "cold_3run_mean_us": round(cold_3run_mean_us, 3) if cold_3run_mean_us is not None else None,
        "warm_median_us": round(warm_median_us, 3) if warm_median_us is not None else None,
        "cold_tax_us": cold_tax_us,
        "cold_spread_median_us": round(cold_spread_median_us, 3) if cold_spread_median_us is not None else None,
        "bimodal_spike_detected": bimodal_spike_detected,
        "synthetic_order": synthetic_order,
        "explicit_cold3_cases": len(explicit_3runs),
        "spike_cases": spike_cases[:8],
        "jitter_cases": jitter_cases[:8],
        "block_dims": block_dims,
        "has_telemetry": any(v is not None for v in (first_iter_us, cold_3run_mean_us, warm_median_us, cold_spread_median_us)),
    }
    return {
        "title": art["title"],
        "sha": art["sha"],
        "sha_source": art.get("sha_source", "none"),
        "status": status,
        "metrics": metrics,
        "first_iter_us": metrics["first_iter_us"],
        "cold_3run_mean_us": metrics["cold_3run_mean_us"],
        "warm_median_us": metrics["warm_median_us"],
        "cold_tax_us": metrics["cold_tax_us"],
        "cold_spread_median_us": metrics["cold_spread_median_us"],
        "bimodal_spike_detected": bimodal_spike_detected,
        "synthetic_order": synthetic_order,
        "alerts": alerts,
    }
