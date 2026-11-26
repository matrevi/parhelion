"""CLI: ``opsb replay {bench, metrics}`` over session logs (read-only)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _workspace(arg: str | None) -> Path | None:
    from ..config import ConfigRepository
    try:
        ws = ConfigRepository.resolve_workspace(arg, ConfigRepository.load())
    except Exception:
        return None
    return ws if ws and ws.is_dir() else None


def _paths(args: argparse.Namespace) -> list[Path]:
    from ..replay.loader import expand_sessions
    return expand_sessions(args.sessions)


def _orchestrator_paths(pattern: str | None) -> list[Path] | None:
    if not pattern:
        return None
    import glob
    import os
    return [Path(p) for p in sorted(glob.glob(os.path.expanduser(pattern)))]


def _usage_section(paths: list[Path], since: str | None, until: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Per-worker cache summary in the window, and the raw parse (reused by the cost section)."""
    from ..metrics import shape, usage
    per_agent: dict[str, Any] = {}
    raw: dict[str, Any] = {}
    for p in paths:
        res = usage.agent_usage(p)
        raw[res["agent_id"]] = res
        calls = [c for c in res["calls"] if not ((since and str(c.get("ts") or "") < since)
                                                  or (until and str(c.get("ts") or "") > until))]
        per_agent[res["agent_id"]] = {"driver": res["driver"], "status": res["status"], "reason": res["reason"],
                                      **_summary(shape.summarize(calls))}
    return per_agent, raw


def _summary(s: dict[str, Any]) -> dict[str, Any]:
    return {"calls": s["calls"], "cache_hit_pct": s["cache_hit_pct"], "input_miss": s["input_miss"],
            "cache_read": s["cache_read"], "cache_write": s["cache_write"], "output": s["output"],
            "miss_events": s["miss_events"], "reasons": {k: v for k, v in s["reasons"].items() if v},
            "last_miss_reason": s["last_miss_reason"], "model": s["model"]}


def run_replay_metrics(args: argparse.Namespace) -> int:
    from ..metrics import mcp_meter
    from ..replay import cost, sprint_metrics
    from ..replay.loader import load_agent
    paths = _paths(args)
    if not paths:
        print(json.dumps({"ok": False, "error": "no mesh session logs matched"}) if args.json
              else "No mesh session logs matched.", file=None if args.json else sys.stderr)
        return 1
    ws = _workspace(args.workspace)
    logs = [load_agent(p, since=args.since, until=args.until) for p in paths]
    result = sprint_metrics.compute(paths, workspace=ws, since=args.since, until=args.until,
                                    orchestrator_paths=_orchestrator_paths(args.orchestrator), logs=logs)
    since, until = result["window"]["since"], result["window"]["until"]
    result["cache"], raw = _usage_section(paths, since, until)
    if ws is not None:
        if not args.no_cost:
            result["cost"] = cost.compute([g for g in logs if g.turns], ws, since, until, usage_by_agent=raw)
        rows = mcp_meter.read_rows(ws)
        if rows:
            result["mcp_calls"] = mcp_meter.summarize(rows)
    result["workspace"] = str(ws) if ws else None
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        _print_metrics(result)
    return 0


def _print_metrics(r: dict[str, Any]) -> None:
    w, c, rep = r["window"], r["context"], r["repeats"]
    print(f"Window {w['since']} .. {w['until']} ({w['hours']} h), {w['workers']} worker logs")
    print(f"Context peak median {c['peak_median']} max {c['peak_max']}; compactions {c['compactions']}; "
          f"startup median {c['startup_median']} max {c['startup_max']}")
    cd = r["cd_shell"]
    print(f"cd shell calls {cd['count']} ({cd['per_hour']}/h), {cd['bytes']} bytes: " +
          ", ".join(f"{k} {v['bytes']}" for k, v in cd["by_program"].items()))
    py = r["inline_python"]
    print(f"Inline Python {py['count']} ({py['per_hour']}/h): json {py['reads_json']}, report.json {py['parses_report']}")
    print("CLI bypass: " + ", ".join(f"{k} {v}" for k, v in r["cli_bypass"].items()))
    if r.get("orchestrator"):
        o = r["orchestrator"]
        print(f"Orchestrator polls {o['poll_total']} ({o['poll_per_hour']}/h): agent_wait {o['agent_wait']}, "
              f"test_wait {o['test_wait']}, agent_send {o['agent_send']}, status {o['status']}")
        for sid, u in o.get("usage", {}).items():
            print(f"  orchestrator {sid[:16]}: hit {u['cache_hit_pct']}% miss {u['input_miss']} read {u['cache_read']} "
                  f"last miss {u['last_miss_reason'] or '-'}")
    print(f"Re-reads {rep['rereads']}/{rep['reads']} ({rep['reread_ratio_all']}), after cut {rep['reread_ratio_after_cut']}; "
          f"re-tests {rep['retests']}/{rep['tests']}")
    for aid, u in (r.get("cache") or {}).items():
        print(f"  {aid}: {u['status']} hit {u.get('cache_hit_pct')}% miss {u.get('input_miss')} "
              f"read {u.get('cache_read')} last miss {u.get('last_miss_reason') or '-'}")


def run_replay_bench(args: argparse.Namespace) -> int:
    from ..replay import bench
    paths = _paths(args)
    schemes = [s.strip() for s in str(args.schemes or "").split(",") if s.strip()] or None
    try:
        result = bench.run(paths, schemes=schemes, since=args.since, until=args.until,
                           judge="llm" if args.llm_judge else "keys", with_details=args.details)
    except (KeyError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}) if args.json else f"Error: {exc}")
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    print(f"{result['workers']} worker logs, {len(result['cuts'])} cuts, {result['probes']['total']} probes "
          f"{result['probes']['by_type']}")
    for name, s in result["schemes"].items():
        o = s["overall"]
        print(f"  {name}: coverage {o['coverage']} CI95 {o['ci95']} ({o['covered']}/{o['probes']}); "
              f"seed chars median {result['seed_chars'][name]['median']}")
    return 0


def register_replay_subcommands(sub: Any) -> None:
    s_rp = sub.add_parser("replay", help="Replay session logs: sprint metrics and the compaction benchmark")
    rp_sub = s_rp.add_subparsers(dest="replay_cmd", required=True)
    for name, func, help_text in (
        ("bench", run_replay_bench, "Score seed schemes against probes cut from compaction points"),
        ("metrics", run_replay_metrics, "Recompute sprint metrics, cache hit rates and cost efficiency"),
    ):
        p = rp_sub.add_parser(name, help=help_text)
        p.add_argument("--sessions", help="Glob of mesh logs (default: mesh-*.jsonl in the session logs dir)")
        p.add_argument("--since", help="ISO UTC lower bound, e.g. 2026-11-26T04:15")
        p.add_argument("--until", help="ISO UTC upper bound")
        p.add_argument("--json", action="store_true")
        p.set_defaults(func=func)
        if name == "bench":
            p.add_argument("--schemes", help="Comma-separated seed schemes (default: current,current_plus_ledger,full_history)")
            p.add_argument("--details", action="store_true", help="Include every probe and verdict")
            p.add_argument("--llm-judge", action="store_true", help=argparse.SUPPRESS)
        else:
            p.add_argument("--workspace", "-w")
            p.add_argument("--orchestrator", help="Glob of orchestrator DSH session files (default: auto-detect)")
            p.add_argument("--no-cost", action="store_true", help="Skip the cost-efficiency section")
