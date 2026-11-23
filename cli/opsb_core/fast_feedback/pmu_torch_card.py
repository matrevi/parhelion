"""PMU pipe utilization & `ours/torch` ratio diagnostic card (`build_pmu_torch_card`)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import read_json
from .cold3_probe import evaluate_cold3_probe
from .common import _bench_specs_dir, _load_run_artifacts, _parse_validation_perf_safe
from .elf_guard import evaluate_elf_guard
from .pmu_extract import _extract_pmu_and_torch


def _classify_bottleneck(pmu: dict[str, Any], cold3: dict[str, Any], bucket_rows: list[dict[str, Any]]) -> str:
    bank = pmu.get("bank_conflict_ratio")
    if isinstance(bank, (int, float)) and bank >= 0.15:
        return "bank-conflict"

    pipes = {
        "mte2-bound": float(pmu["aiv_mte2_ratio"]) if isinstance(pmu.get("aiv_mte2_ratio"), (int, float)) else None,
        "mte3-bound": float(pmu["aiv_mte3_ratio"]) if isinstance(pmu.get("aiv_mte3_ratio"), (int, float)) else None,
        "vec-bound": float(pmu["aiv_vec_ratio"]) if isinstance(pmu.get("aiv_vec_ratio"), (int, float)) else None,
        "scalar-bound": float(pmu["aiv_scalar_ratio"]) if isinstance(pmu.get("aiv_scalar_ratio"), (int, float)) else None,
    }
    active = {k: v for k, v in pipes.items() if v is not None}
    cold_tax = cold3.get("cold_tax_us")
    has_cold_issue = bool(cold3.get("bimodal_spike_detected")) or (isinstance(cold_tax, (int, float)) and cold_tax >= 1.5)

    if not active:
        if has_cold_issue:
            return "cold-launch-tax"
        if bucket_rows and all((b.get("ours_us") or 0) <= 6.0 for b in bucket_rows) and any((b.get("ratio_vs_torch") or 0) > 1.15 for b in bucket_rows):
            return "cold-launch-tax"
        return "unknown"

    top_name, top_val = max(active.items(), key=lambda kv: kv[1])
    if has_cold_issue and top_val < 0.25:
        return "cold-launch-tax"
    if pipes["scalar-bound"] is not None and pipes["scalar-bound"] >= 0.35:
        other_max = max((v for k, v in active.items() if k != "scalar-bound"), default=0.0)
        if pipes["scalar-bound"] >= other_max or (pipes["scalar-bound"] >= 0.45 and (pipes["vec-bound"] or 0.0) < 0.25):
            return "scalar-bound"
    if top_val >= 0.35:
        return top_name
    sorted_vals = sorted(active.values(), reverse=True)
    second_val = sorted_vals[1] if len(sorted_vals) > 1 else 0.0
    if top_val >= 0.22 and top_val >= max(0.22, second_val * 1.4):
        return top_name
    return "balanced"


def _load_perf_spec_buckets(workspace: Path, title: str, repo_dir: Path) -> dict[str, dict[str, Any]]:
    ws = workspace.expanduser().resolve()
    out: dict[str, dict[str, Any]] = {}
    for path in (_bench_specs_dir(ws) / title / "cases_perf.json", repo_dir / "bench" / "cases_perf.json"):
        doc = read_json(path, None)
        if not isinstance(doc, dict):
            continue
        for key in ("public_anchors", "hidden_buckets", "buckets"):
            for item in doc.get(key) or []:
                if isinstance(item, dict) and item.get("id"):
                    out.setdefault(str(item["id"]), dict(item))
    return out


def _render_markdown_card(
    title: str,
    sha: str,
    bottleneck: str,
    pmu_summary: dict[str, Any],
    top_buckets: list[dict[str, Any]],
    elf_verdict: dict[str, Any],
    cold3_verdict: dict[str, Any],
    suggested_tags: list[str],
) -> str:
    sha7 = sha[:7] if sha else "none"
    em, c3m = elf_verdict["metrics"], cold3_verdict["metrics"]
    text_kb = f"{em['kernel_o_max_text_bytes'] / 1024:.1f}KB" if em.get("kernel_o_max_text_bytes") is not None else "—"
    obj_kb = f"{em['kernel_o_max_bytes'] / 1024:.1f}KB" if em.get("kernel_o_max_bytes") is not None else "—"
    tmpl_s = str(em["template_instantiation_count"]) if em.get("template_instantiation_count") is not None else "—"
    c1 = f"{c3m['first_iter_us']:.2f}us" if c3m.get("first_iter_us") is not None else "—"
    c3 = f"{c3m['cold_3run_mean_us']:.2f}us" if c3m.get("cold_3run_mean_us") is not None else "—"
    cw = f"{c3m['warm_median_us']:.2f}us" if c3m.get("warm_median_us") is not None else "—"
    _fr = lambda v: f"{v:.3f}" if isinstance(v, (int, float)) else "—"

    pmu_line = (
        f"- **PMU**: vec={_fr(pmu_summary['aiv_vec_ratio'])} | mte2={_fr(pmu_summary['aiv_mte2_ratio'])} | mte3={_fr(pmu_summary['aiv_mte3_ratio'])} | scalar={_fr(pmu_summary['aiv_scalar_ratio'])} | bank={_fr(pmu_summary['bank_conflict_ratio'])}"
        if pmu_summary.get("pmu_available")
        else f"- **PMU**: unavailable ({pmu_summary.get('pmu_mode', 'l0_timing_only')} — run `opsb test {title} --pmu` / `OPSB_PMU=1` for L1 PipeUtilization)"
    )
    lines: list[str] = [
        f"### PMU & Torch Card: {title} ({sha7})",
        f"- **Bottleneck**: `{bottleneck}` | **ELF**: `{elf_verdict['status']}` (.text={text_kb}, .o={obj_kb}, tmpl={tmpl_s}) | **Cold3**: `{cold3_verdict['status']}` (1st={c1}, 3run={c3}, warm={cw})",
        pmu_line,
    ]
    if top_buckets:
        lines.extend(["| Bucket | Stratum | Ours | Torch | Ours/Torch | w_hat | Headroom |", "|---|---|---:|---:|---:|---:|---:|"])
        for row in top_buckets[:8]:
            t_s = f"{row['torch_us']:.2f}us" if row["torch_us"] is not None else "—"
            r_s = f"{row['ratio_vs_torch']:.2f}x" if row["ratio_vs_torch"] is not None else "—"
            w_s = f"{row['w_hat']:.3f}" if row["w_hat"] is not None else "—"
            lines.append(f"| `{row['bucket']}` | {row['stratum']} | {row['ours_us']:.2f}us | {t_s} | {r_s} | {w_s} | {row['headroom_score']:.4f} |")
    else:
        lines.append("- No per-bucket profiler timings found yet in validation logs.")

    for al in [*elf_verdict.get("alerts", []), *cold3_verdict.get("alerts", [])][:3]:
        if len(lines) >= 24:
            break
        lines.append(f"- **[{al['severity'].upper()}] {al['code']}**: {al['message']}")
    if suggested_tags and len(lines) < 25:
        lines.append(f"- **KB Tags**: {', '.join(suggested_tags[:6])}")
    return "\n".join(lines[:25])


def build_pmu_torch_card(workspace: Path, title: str, sha: str | None = None, top_n: int = 5) -> dict[str, Any]:
    """Produce a concise machine + markdown card combining `ours/torch` ratios with PMU counters."""
    ws = Path(workspace).expanduser().resolve()
    art = _load_run_artifacts(ws, title, sha=sha, require_passing=True, allow_build_logs=False)
    canonical_title, resolved_sha, repo_dir, log_text = art["title"], art["sha"], art["repo_dir"], art["log_text"]

    parsed = _parse_validation_perf_safe(log_text)
    extra_ours, extra_torch, pmu_summary = _extract_pmu_and_torch(log_text)

    ours_map = {str(k): float(v) for k, v in (parsed.get("profiler_cases") or {}).items() if isinstance(v, (int, float)) and v > 0}
    for k, v in extra_ours.items():
        ours_map.setdefault(k, v)
    torch_map = {str(k): float(v) for k, v in (parsed.get("torch_cases") or {}).items() if isinstance(v, (int, float)) and v > 0}
    for k, v in extra_torch.items():
        torch_map.setdefault(k, v)

    spec_buckets = _load_perf_spec_buckets(ws, canonical_title, repo_dir)
    cal_doc = read_json(ws / "results" / canonical_title / "calibration.json", {})
    cal_buckets = {str(cb["id"]): cb for cb in (cal_doc.get("buckets") or []) if isinstance(cb, dict) and cb.get("id")} if isinstance(cal_doc, dict) else {}
    cal_corrs = cal_doc.get("bucket_correlations") if isinstance(cal_doc, dict) and isinstance(cal_doc.get("bucket_correlations"), dict) else {}
    rj = art["result_json"] if isinstance(art.get("result_json"), dict) else {}
    regime_s = str(rj.get("profiler_regime") or "")
    if pmu_summary.get("pmu_available"):
        pmu_summary["pmu_mode"] = "l1_pipe_utilization"
    elif "l0" in regime_s or "Lock-free verify + profiler parse" in log_text or pmu_summary.get("zero_dummy_samples", 0) > 0:
        pmu_summary["pmu_mode"] = "l0_timing_only"
    else:
        pmu_summary["pmu_mode"] = "unavailable"

    bucket_rows: list[dict[str, Any]] = []
    for bid, ours_us in sorted(ours_map.items()):
        sb, cb = spec_buckets.get(bid, {}), cal_buckets.get(bid, {})
        torch_us = torch_map.get(bid) or sb.get("tau_torch_us") or sb.get("torch_ref_us") or sb.get("torch_us") or sb.get("tbest_us")
        torch_f = float(torch_us) if isinstance(torch_us, (int, float)) and float(torch_us) > 0 else None
        ratio = round(ours_us / torch_f, 4) if torch_f else None
        w_hat_raw = cb.get("learned_w")
        w_hat = float(w_hat_raw) if isinstance(w_hat_raw, (int, float)) and float(w_hat_raw) > 0 else None
        corr_raw = cb.get("empirical_corr")
        if corr_raw is None:
            corr_raw = cb.get("corr") if cb.get("corr") is not None else cal_corrs.get(bid)
        corr = float(corr_raw) if isinstance(corr_raw, (int, float)) else None
        if ratio is not None:
            raw_hs = w_hat * max(0.0, ratio - 0.8) if w_hat is not None else ratio
            if corr is not None and corr < 0.0:
                headroom_score = round(raw_hs * max(0.0, 1.0 + corr), 6) if corr > -0.25 else 0.0
            else:
                headroom_score = round(raw_hs, 6)
        else:
            headroom_score = round(0.05 * w_hat, 6) if (w_hat is not None and corr is not None and corr >= 0.0) else 0.0
        priority_tier = (
            3 if (ratio is not None and ratio > 0.8 and corr is not None and corr > 0.0)
            else (2 if (ratio is not None and ratio > 0.8 and corr is None)
                  else (1 if (ratio is not None and (corr is None or corr >= 0.0)) else 0))
        )
        bucket_rows.append({
            "bucket": bid,
            "stratum": str(cb.get("stratum") or sb.get("stratum") or "M_MID_UB"),
            "ours_us": round(ours_us, 3),
            "torch_us": round(torch_f, 3) if torch_f is not None else None,
            "ratio_vs_torch": ratio,
            "w_hat": round(w_hat, 5) if w_hat is not None else None,
            "corr": round(corr, 4) if corr is not None else None,
            "headroom_score": headroom_score,
            "_priority_tier": priority_tier,
            "pmu": pmu_summary["per_case"].get(bid, {}),
        })

    bucket_rows.sort(
        key=lambda r: (
            int(r.get("_priority_tier", 0)),
            float(r["headroom_score"]),
            float(r["ratio_vs_torch"] or 0.0),
            float(r["ours_us"]),
        ),
        reverse=True,
    )
    for r in bucket_rows:
        r.pop("_priority_tier", None)
    top_buckets = bucket_rows[: max(1, int(top_n))]

    elf_verdict = evaluate_elf_guard(ws, canonical_title, sha=resolved_sha or sha)
    cold3_verdict = evaluate_cold3_probe(ws, canonical_title, sha=resolved_sha or sha)
    bottleneck = _classify_bottleneck(pmu_summary, cold3_verdict, bucket_rows)

    suggested_tags: list[str] = [bottleneck] if bottleneck not in ("unknown", "balanced") else []
    for al in [*elf_verdict.get("alerts", []), *cold3_verdict.get("alerts", [])]:
        for t in al.get("suggested_kb_tags") or []:
            if t not in suggested_tags:
                suggested_tags.append(t)

    markdown_card = _render_markdown_card(canonical_title, resolved_sha, bottleneck, pmu_summary, top_buckets, elf_verdict, cold3_verdict, suggested_tags)
    return {
        "title": canonical_title,
        "sha": resolved_sha,
        "sha_source": art.get("sha_source", "none"),
        "runtime_soc": parsed.get("runtime_soc"),
        "dominant_bottleneck": bottleneck,
        "pmu_available": bool(pmu_summary.get("pmu_available")),
        "pmu": pmu_summary,
        "prof_summary": parsed.get("prof_summary") or {},
        "buckets": bucket_rows,
        "top_buckets": top_buckets,
        "elf_guard": {"status": elf_verdict["status"], "metrics": elf_verdict["metrics"], "alerts": elf_verdict["alerts"]},
        "cold3": {"status": cold3_verdict["status"], "metrics": cold3_verdict["metrics"], "alerts": cold3_verdict["alerts"]},
        "suggested_kb_tags": suggested_tags,
        "markdown_card": markdown_card,
    }
