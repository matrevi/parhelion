"""CLI subcommands for Parhelion v3 Fast-Feedback Tools (`opsb bench elf|cold3|pmu|scaffold`)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ..config import ConfigRepository
from ..services.contest_service import resolve_workspace_meta
from ..services.fast_feedback_service import (
    VALID_OPERATOR_FAMILIES,
    build_pmu_torch_card,
    evaluate_cold3_probe,
    evaluate_elf_guard,
    scaffold_bench_specs,
)


def _list_workspace_problems(ws: Path) -> list[str]:
    meta = resolve_workspace_meta(ws)
    titles = [str(p["title"]) for p in (meta.get("problems") or []) if isinstance(p, dict) and p.get("title")]
    if titles:
        return titles
    res_dir = ws / "results"
    if res_dir.is_dir():
        return sorted(
            d.name for d in res_dir.iterdir()
            if d.is_dir() and d.name != "scoring-evidence" and not d.name.startswith(".")
        )
    return []


def _format_elf_line(res: dict[str, Any]) -> list[str]:
    m = res.get("metrics") or {}
    txt_kb = f"{m['kernel_o_max_text_bytes'] / 1024:.1f}KB" if m.get("kernel_o_max_text_bytes") is not None else "—"
    obj_kb = f"{m['kernel_o_max_bytes'] / 1024:.1f}KB" if m.get("kernel_o_max_bytes") is not None else "—"
    bsec = f"{m['phase_a_cpu_sec']:.1f}s" if m.get("phase_a_cpu_sec") is not None else "—"
    tmpl = str(m["template_instantiation_count"]) if m.get("template_instantiation_count") is not None else "—"
    sha7 = (res.get("sha") or "")[:7] or "—"
    lines = [
        f"  {res['title']:<26} [{res['status'].upper():<7}] sha={sha7}  .text={txt_kb:>7}  .o_max={obj_kb:>8}  build={bsec:>6}  tmpl={tmpl}"
    ]
    for al in res.get("alerts") or []:
        lines.append(f"    [{al['severity'].upper()}] {al['code']}: {al['message']}")
    return lines


def _format_cold3_line(res: dict[str, Any]) -> list[str]:
    m = res.get("metrics") or {}
    f1 = f"{m['first_iter_us']:.2f}us" if m.get("first_iter_us") is not None else "—"
    c3 = f"{m['cold_3run_mean_us']:.2f}us" if m.get("cold_3run_mean_us") is not None else "—"
    wm = f"{m['warm_median_us']:.2f}us" if m.get("warm_median_us") is not None else "—"
    tx = f"{m['cold_tax_us']:+.2f}us" if m.get("cold_tax_us") is not None else "—"
    sp = f"{m['cold_spread_median_us']:.2f}us" if m.get("cold_spread_median_us") is not None else "—"
    spike = "SPIKE" if m.get("bimodal_spike_detected") else "steady"
    sha7 = (res.get("sha") or "")[:7] or "—"
    lines = [
        f"  {res['title']:<26} [{res['status'].upper():<7}] sha={sha7}  1st={f1:>8}  3run={c3:>8}  warm={wm:>8}  tax={tx:>7}  spread={sp:>7}  ({spike})"
    ]
    for al in res.get("alerts") or []:
        lines.append(f"    [{al['severity'].upper()}] {al['code']}: {al['message']}")
    return lines


def run_bench_elf(args: argparse.Namespace) -> int:
    cfg = ConfigRepository.load()
    ws = ConfigRepository.resolve_workspace(getattr(args, "workspace", None), cfg)
    problem = getattr(args, "problem", None)
    sha = getattr(args, "sha", None)

    if problem:
        res = evaluate_elf_guard(ws, problem, sha=sha)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        print(f"=== ELF Guard: {res['title']} ({res['status'].upper()}) ===")
        for line in _format_elf_line(res):
            print(line)
        return 0

    titles = _list_workspace_problems(ws)
    items = [evaluate_elf_guard(ws, t, sha=sha) for t in titles]
    payload = {
        "workspace": str(ws),
        "count": len(items),
        "redline_count": sum(1 for x in items if x["status"] == "redline"),
        "warn_count": sum(1 for x in items if x["status"] == "warn"),
        "problems": items,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(f"=== Parhelion ELF Guard ({len(items)} operators, redline={payload['redline_count']}, warn={payload['warn_count']}) ===")
    for item in items:
        for line in _format_elf_line(item):
            print(line)
    return 0


def run_bench_cold3(args: argparse.Namespace) -> int:
    cfg = ConfigRepository.load()
    ws = ConfigRepository.resolve_workspace(getattr(args, "workspace", None), cfg)
    problem = getattr(args, "problem", None)
    sha = getattr(args, "sha", None)

    if problem:
        res = evaluate_cold3_probe(ws, problem, sha=sha)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        print(f"=== Cold3 Probe: {res['title']} ({res['status'].upper()}) ===")
        for line in _format_cold3_line(res):
            print(line)
        return 0

    titles = _list_workspace_problems(ws)
    items = [evaluate_cold3_probe(ws, t, sha=sha) for t in titles]
    payload = {
        "workspace": str(ws),
        "count": len(items),
        "redline_count": sum(1 for x in items if x["status"] == "redline"),
        "warn_count": sum(1 for x in items if x["status"] == "warn"),
        "problems": items,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(f"=== Parhelion Cold3 Probe ({len(items)} operators, redline={payload['redline_count']}, warn={payload['warn_count']}) ===")
    for item in items:
        for line in _format_cold3_line(item):
            print(line)
    return 0


def run_bench_pmu(args: argparse.Namespace) -> int:
    cfg = ConfigRepository.load()
    ws = ConfigRepository.resolve_workspace(getattr(args, "workspace", None), cfg)
    res = build_pmu_torch_card(
        ws,
        args.problem,
        sha=getattr(args, "sha", None),
        top_n=int(getattr(args, "top", 5) or 5),
    )
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    print(res["markdown_card"])
    return 0


def run_bench_scaffold(args: argparse.Namespace) -> int:
    cfg = ConfigRepository.load()
    ws = ConfigRepository.resolve_workspace(getattr(args, "workspace", None), cfg)
    raw_dtypes = str(getattr(args, "dtypes", "fp32,fp16,bf16") or "")
    dtypes = [x.strip() for x in raw_dtypes.split(",") if x.strip()]
    try:
        res = scaffold_bench_specs(
            ws,
            args.problem,
            operator_family=str(getattr(args, "family", "elementwise") or "elementwise"),
            dtypes=dtypes or None,
            apply=bool(getattr(args, "apply", False)),
        )
    except ValueError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    mode = "APPLIED" if res["applied"] else "DRY-RUN"
    print(f"=== Bench Scaffold [{mode}]: {res['title']} (family={res['operator_family']}) ===")
    print(f"  Target: {res['target_dir']}")
    print(f"  Strata: {', '.join(res['strata_covered'])} | Dtypes: {', '.join(res['dtypes'])}")
    print(
        f"  Perf buckets: existing={res['existing_perf_count']}  "
        + (
            f"appended=+{len(res['appended_perf_ids'])} ({', '.join(res['appended_perf_ids'][:5])})"
            if res["applied"]
            else f"pending=+{len(res['pending_perf_ids'])}"
        )
    )
    print(
        f"  Correctness cases: existing={res['existing_correctness_count']}  "
        + (
            f"appended=+{len(res['appended_correctness_ids'])}"
            if res["applied"]
            else f"pending=+{len(res['pending_correctness_ids'])}"
        )
    )
    if not res["applied"] and (res["pending_perf_ids"] or res["pending_correctness_ids"]):
        print("  Re-run with --apply to write/append missing specs (strictly append-only).")
    return 0


def register_fast_tools_subcommands(bn_sub: argparse._SubParsersAction[Any]) -> None:
    """Register `elf`, `cold3`, `pmu`, and `scaffold` subcommands under `opsb bench`."""
    sp_elf = bn_sub.add_parser("elf", help="Inspect .o/.text I-Cache bloat, compile time, and template instantiations")
    sp_elf.add_argument("problem", nargs="?")
    sp_elf.add_argument("--sha", default=None)
    sp_elf.add_argument("--workspace", "-w", default=None)
    sp_elf.add_argument("--json", action="store_true")
    sp_elf.set_defaults(func=run_bench_elf)

    sp_c3 = bn_sub.add_parser("cold3", help="Analyze un-warmed 3-run cold-start latency and 10-core DVFS spikes")
    sp_c3.add_argument("problem", nargs="?")
    sp_c3.add_argument("--sha", default=None)
    sp_c3.add_argument("--workspace", "-w", default=None)
    sp_c3.add_argument("--json", action="store_true")
    sp_c3.set_defaults(func=run_bench_cold3)

    sp_pmu = bn_sub.add_parser("pmu", help="Build compact ours/torch + PMU bottleneck diagnostic card")
    sp_pmu.add_argument("problem")
    sp_pmu.add_argument("--sha", default=None)
    sp_pmu.add_argument("--top", type=int, default=5)
    sp_pmu.add_argument("--workspace", "-w", default=None)
    sp_pmu.add_argument("--json", action="store_true")
    sp_pmu.set_defaults(func=run_bench_pmu)

    sp_sc = bn_sub.add_parser("scaffold", help="Generate append-only stratified bench specs in ~/.parhelion/workspaces/")
    sp_sc.add_argument("problem")
    sp_sc.add_argument("--family", choices=list(VALID_OPERATOR_FAMILIES), default="elementwise")
    sp_sc.add_argument("--dtypes", default="fp32,fp16,bf16")
    sp_sc.add_argument("--apply", action="store_true", help="Write/append missing specs (default is dry-run)")
    sp_sc.add_argument("--workspace", "-w", default=None)
    sp_sc.add_argument("--json", action="store_true")
    sp_sc.set_defaults(func=run_bench_scaffold)
