"""CLI: ``opsb replay {bench, metrics, gate, stuck}`` over session logs and contest data (read-only)."""
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
              f"test_wait {o['test_wait']}, agent_send {o['agent_send']}, status {o['status']}, wait {o['wait']}; "
              f"squad calls {o.get('squad_calls', 0)}")
    wp = r.get("worker_polls") or {}
    if wp:
        print(f"Worker polls {wp['poll_total']} ({wp['poll_per_hour']}/h): test_wait {wp['test_wait']}, "
              f"status {wp['status']}, agent_inbox {wp['agent_inbox']}, wait {wp['wait']}")
        for sid, u in o.get("usage", {}).items():
            print(f"  orchestrator {sid[:16]}: hit {u['cache_hit_pct']}% miss {u['input_miss']} read {u['cache_read']} "
                  f"last miss {u['last_miss_reason'] or '-'}")
    cm = r.get("comms") or {}
    if cm:
        ev = ", ".join(f"{k} {v}" for k, v in (cm.get("evidence") or {}).items())
        print(f"Peer messages {cm['squad_messages']} ({cm['per_hour']}/h): broadcasts {cm['broadcasts']}; "
              f"evidence {ev}")
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


def run_replay_gate(args: argparse.Namespace) -> int:
    """``opsb replay gate``: judge every receipt again as of its own timestamp, on a copy."""
    from ..replay import gate_replay
    ws = _workspace(args.workspace)
    if ws is None:
        print(json.dumps({"ok": False, "error": "workspace not found"}) if args.json
              else f"Error: workspace {args.workspace!r} not found")
        return 2
    try:
        result = gate_replay.run(ws, manifest=args.manifest, board_mode=args.board_mode,
                                 gate_switches=args.gate, since=args.since, until=args.until,
                                 groups=[g.strip() for g in str(args.groups or "").split(",") if g.strip()],
                                 jobs=args.jobs, keep_copy=args.keep_copy, detail=args.detail,
                                 intents=args.intents, ranking=args.ranking)
    except (ValueError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}) if args.json else f"Error: {exc}")
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        _print_gate(result)
    return 0


def run_replay_stuck(args: argparse.Namespace) -> int:
    """``opsb replay stuck``: the 4.4 P30 detector over a past sprint's ledgers (read only)."""
    from ..replay import stuck_replay
    ws = _workspace(args.workspace)
    if ws is None:
        print(json.dumps({"ok": False, "error": "workspace not found"}) if args.json
              else f"Error: workspace {args.workspace!r} not found")
        return 2
    problems = [p.strip() for p in str(args.problems or "").split(",") if p.strip()] or None
    result = stuck_replay.run(ws, since=args.since, until=args.until, step=float(args.step), problems=problems,
                              sessions=args.sessions)
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else stuck_replay.render(result))
    return 0


def _print_gate(r: dict[str, Any]) -> None:
    copy, shots = r["copy"], r["shots"]
    print(f"{r['workspace']}: {shots['replayed']} of {shots['canonical_rows']} journal rows replayed "
          f"({shots['judged']} judged; {shots['not_judged']} not judged, {shots['no_sha']} without a sha, "
          f"{shots['no_time']} without a timestamp)")
    print(f"copy {copy['copy_sec']}s, manifest {copy['manifest']} ({copy['problems']} problems), "
          f"{copy['repos']} repositories cloned, window {r['window']['since']}..{r['window']['until']}")
    for name, group in r["groups"].items():
        gate, out, cred = group["gate"], group["outcome"], group["credibility"]
        print(f"\n[{name}] board {group['board_mode']}  switches "
              f"{group['switches'] or 'all 4.2.1 defaults'}")
        print(f"  gate     blocked {gate['blocked']}/{gate['shots']} ({gate['blocked_rate'] * 100:.1f}%): "
              + (", ".join(f"{k} {v}" for k, v in gate["by_stage"].items()) or "none")
              + f"; next step {gate['blocked_with_next_step']}/{gate['blocked']}")
        print(f"           new highs {len(gate['blocked_new_highs'])}, restores {len(gate['blocked_restores'])}, "
              f"frozen {gate['frozen']['total']} (max streak {gate['frozen']['max_streak']}), "
              f"restore {gate['restore']['restore_approved']}/{gate['restore']['restore_attempts']} "
              f"of {gate['restore']['zero_events']} zeroed boards")
        print(f"  outcome  new high {out['new_high_rate'] * 100:.1f}% ({out['new_highs']}), "
              f"tie {out['tie_rate'] * 100:.1f}%, regression {out['regression_rate'] * 100:.1f}%, "
              f"hard failure {out['hard_fail_rate'] * 100:.1f}% ({out['hard_failures']})")
        print(f"           points {out['points']} ({out['points_per_judgement']}/judgement), "
              f"same source {out['same_source_rate'] * 100:.1f}%, "
              f"recovery {out['recovery_median_min']} min, closing gap {out['closing_gap']}")
        print(f"  S2       approximate: {cred['s2_paired_shots']}/{out['judged']} shots carry paired local "
              f"evidence ({cred['s2_paired_rate'] * 100:.1f}%)")
        if group.get("intents"):
            _print_intents(group["intents"])
        if group.get("ranking"):
            _print_ranking(group["ranking"])
    print(f"\ntotal {r['elapsed_sec']}s")


def _print_intents(block: dict[str, Any]) -> None:
    """4.3 P23: G4, the hypothetical probe series and the opening window of one switch group."""
    restore, probe, opening = block["restore"], block["probe"], block["opening"]
    rate = "n/a" if restore["approve_rate"] is None else f"{restore['approve_rate'] * 100:.0f}%"
    print(f"  G4       restore {restore['approved']}/{restore['events']} approved ({rate}); "
          f"board open {restore['by_reason']['board_open']}, after a failure {restore['by_reason']['after_failure']}")
    print(f"  probe    held shots re-judged as intent=probe: {probe['approved']}/{probe['evaluated']} approved, "
          f"{probe['approved_new_highs']} new highs, gain {probe['gain']:+.2f} / loss {probe['loss']:+.2f} "
          f"(net {probe['net']:+.2f})")
    scored = [(t, v) for t, v in opening["per_problem"].items() if v["scored"]]
    unscored = [t for t, v in opening["per_problem"].items() if not v["scored"]]
    print(f"  opening  window <= {opening['max_probes']}: first score at shot "
          + (", ".join(f"{t} #{v['first_score_shot']}" for t, v in scored) or "none")
          + (f"; never scored within the window: {', '.join(unscored)}" if unscored else ""))


def _interval(cell: dict[str, Any]) -> str:
    """``value [lo, hi]``; a metric with no data prints as such rather than as zero."""
    if cell.get("value") is None:
        return "n/a"
    lo, hi = cell.get("ci95") or [None, None]
    mark = " unstable" if cell.get("unstable") else ""
    return f"{cell['value']:+.3f}" + (f" [{lo:+.3f}, {hi:+.3f}]" if lo is not None else "") + mark


def _print_ranking(block: dict[str, Any]) -> None:
    """4.3 P29: does priority order match the gain the judge actually paid?"""
    shots, err = block["shots"], block["error"]
    print(f"  ranking  {shots['ranked']}/{shots['total']} shots ranked ({shots['blocked']} blocked, "
          f"{shots['unpriced']} unscored, {shots['hard_failures']} hard failures, {shots['problems']} problems)")
    print(f"           spearman priority {_interval(block['spearman'])}, predicted "
          f"{_interval(block['spearman_predicted'])}; per moment {_interval(block['spearman_moment'])} "
          f"({block['spearman_moment']['moments']} moments)")
    print(f"           top-1 hit {_interval(block['top1'])} ({block['top1']['hits']}/{block['top1']['n']})")
    print(f"           gain error median {err['median']} mean {err['mean']} over {err['n']}: "
          + ", ".join(f"{b['band']} {b['mean']:+.2f} (n={b['n']})" for b in err["bands"]))
    every = block["counterfactual"]
    print(f"           counterfactual (every judged shot) spearman {_interval(every['spearman'])}, "
          f"per moment {_interval(every['spearman_moment'])}, top-1 {_interval(every['top1'])} "
          f"(n={every['spearman']['n']}, {every['top1']['n']} moments)")


def register_replay_subcommands(sub: Any) -> None:
    s_rp = sub.add_parser("replay", help="Replay session logs: sprint metrics and the compaction benchmark")
    rp_sub = s_rp.add_subparsers(dest="replay_cmd", required=True)
    g = rp_sub.add_parser("gate", help="Replay judge receipts through the submission gate on a workspace copy")
    g.add_argument("--workspace", "-w", required=True, help="Contest workspace directory (read only)")
    g.add_argument("--board-mode", choices=("best", "latest"),
                   help="Force every problem to this board type (default: each problem's rule, else best)")
    g.add_argument("--gate", action="append", metavar="NAME=VALUE",
                   help="GateConfig switch for this replay only, as `opsb queue config --gate`")
    g.add_argument("--since", help="ISO-8601 lower bound on submitted_at")
    g.add_argument("--until", help="ISO-8601 upper bound on submitted_at")
    g.add_argument("--manifest", help="Problem roster JSON when the workspace has no opsb.workspace.json")
    g.add_argument("--groups", default="default,420",
                   help="Switch groups to compare: default (4.2.1), 420 (4.2.0), sigma (constant jitter), ranking (P29 v2)")
    g.add_argument("--detail", action="store_true", help="Include every replayed shot in the JSON output")
    g.add_argument("--jobs", type=int, default=6, help="Problems replayed in parallel (default 6)")
    g.add_argument("--keep-copy", action="store_true", help="Keep the temporary copy and print its path")
    g.add_argument("--intents", action="store_true",
                   help="4.3 P23 block: re-judge as intent=probe/restore and report G4, probe and opening window")
    g.add_argument("--ranking", action="store_true",
                   help="4.3 P29 block: Spearman, top-1 hit rate and the gain error, with bootstrap CIs")
    g.add_argument("--json", action="store_true")
    g.set_defaults(func=run_replay_gate)
    st = rp_sub.add_parser("stuck", help="Run the stuck detector over a past sprint's ledgers (read only)")
    st.add_argument("--workspace", "-w", required=True, help="Contest workspace directory (read only)")
    st.add_argument("--since", help="ISO-8601 lower bound, e.g. 2026-11-26T21:17Z")
    st.add_argument("--until", help="ISO-8601 upper bound")
    st.add_argument("--step", default=5, type=float, help="Minutes between evaluations (default 5)")
    st.add_argument("--problems", help="Comma-separated problem titles (default: every problem)")
    st.add_argument("--sessions", help="Session logs directory: add aggregate tool-call counts per episode")
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=run_replay_stuck)
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
