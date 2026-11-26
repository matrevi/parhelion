"""Unified CLI parser & dispatcher for `opsb`."""
from __future__ import annotations

import argparse

from .commands.admin_cmd import run_auth, run_collab, run_skills, run_ssh, run_workspace
from .commands.bench_cmd import run_bench
from .commands.close_cmd import register_close_subcommands
from .commands.breakthrough_cmd import run_breakthrough, run_queue_list
from .commands.driver_mcp_cmd import register_driver_and_mcp_subcommands
from .commands.doctor_cmd import register_doctor_subcommands
from .commands.kb_cmd import register_kb_subcommands
from .commands.npu_cmd import run_npu
from .commands.queue_cmd import run_queue, run_submit, run_test
from .commands.replay_cmd import register_replay_subcommands
from .commands.status_cmd import run_history, run_status
from .commands.tools_cmd import register_fast_tools_subcommands
from .daemon.server import run_daemon


def run_bench_suite(args: argparse.Namespace) -> int:
    """Forward `opsb bench suite` to `scheduler.cli_bridge suite`, which refuses workers."""
    import subprocess

    from .config import CLI_DIR, ConfigRepository, resolve_python_exe

    ws = ConfigRepository.resolve_workspace(args.workspace, ConfigRepository.load())
    cmd = [resolve_python_exe(ws), "-m", "scheduler.cli_bridge", "suite", args.suite_action,
           "--workspace", str(ws), "--title", args.problem]
    if args.variant_json is not None:
        cmd += ["--json", args.variant_json]
    return subprocess.call(cmd, cwd=str(CLI_DIR.parent))


def run_bench_judge_align(args: argparse.Namespace) -> int:
    """Forward `opsb bench case-map` / `opsb bench receipts` to `scheduler.cli_bridge`."""
    import subprocess

    from .config import CLI_DIR, ConfigRepository, resolve_python_exe

    ws = ConfigRepository.resolve_workspace(args.workspace, ConfigRepository.load())
    if args.bench_cmd == "case-map":
        cmd = ["case-map", "--title", args.problem] + (["--rebuild"] if args.rebuild else [])
    elif args.bench_cmd == "wa-repro":
        cmd = ["wa-repro", "--title", args.problem, *(["--receipt", args.receipt] if args.receipt else []),
               *(["--like", args.like] if args.like else []), *(["--apply"] if args.apply else [])]
    else:
        cmd = ["receipts", args.receipts_action] + (["--apply"] if args.apply else [])
    full = [resolve_python_exe(ws), "-m", "scheduler.cli_bridge", *cmd, "--workspace", str(ws)]
    if args.json:
        return subprocess.call(full, cwd=str(CLI_DIR.parent))
    proc = subprocess.run(full, cwd=str(CLI_DIR.parent), capture_output=True, text=True)
    if proc.returncode != 0:
        print((proc.stderr or proc.stdout).strip())
        return proc.returncode
    import json as _json

    doc = _json.loads(proc.stdout)
    if args.bench_cmd == "wa-repro":
        for item in doc["receipts"]:
            print(f"{item['sha']} {item['status']} {item['submission_id']}: {item['state']} · "
                  f"{len(item.get('variants') or [])} variants · {item.get('next_step') or item.get('reason') or ''}")
    elif args.bench_cmd == "case-map":
        for item in doc["maps"]:
            pairs = item.get("pairs") or []
            mapped = sum(1 for p in pairs if p.get("judge_mapped"))
            print(f"{item['title']}: {item['method']} ({item['confidence']}), {mapped}/{len(item.get('local_ids') or [])} "
                  f"local cases timed against the judge, judge roster {item.get('judge_count')}")
            for p in pairs:
                if p.get("reason"):
                    print(f"  {p['local']} -> {p['judge']}: {p['reason']}")
    else:
        verb = "appended" if args.apply else "would append"
        print(f"{doc['receipts']} receipts, {verb} {doc['append']} normalized rows; missing: {doc['missing_counts']}")
        for row in doc["appended"]:
            print(f"  {row['title']} {row['sha']} {row['status']} {row['official_score']}: {', '.join(row['adds'])}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="opsb", description="Parhelion · Unified KernelC Contest & NPU CLI")
    p.add_argument("--lang", choices=["zh", "en"], help="Output language (zh / en)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s_st = sub.add_parser("status", help="Contest scores, NPU telemetry, and dual queues")
    s_st.add_argument("--workspace", "-w")
    s_st.add_argument("--offline-npu", action="store_true")
    s_st.add_argument("--compact", action="store_true")
    s_st.add_argument("--no-backfill", action="store_true", help="Skip the B0 auto-backfill probe")
    s_st.add_argument("--lang", choices=["zh", "en"])
    s_st.add_argument("--json", action="store_true")
    s_st.set_defaults(func=run_status)

    s_hi = sub.add_parser("history", help="Inspect problem score history and per-case ratios")
    s_hi.add_argument("problem", nargs="?")
    s_hi.add_argument("--workspace", "-w")
    s_hi.add_argument("--json", action="store_true")
    s_hi.set_defaults(func=run_history)

    s_npu = sub.add_parser("npu", help="Remote NPU telemetry & probe management")
    npu_sub = s_npu.add_subparsers(dest="npu_cmd")
    s_npu.add_argument("--host")
    s_npu.add_argument("--workspace", "-w")
    s_npu.add_argument("--json", action="store_true")
    s_npu.set_defaults(func=run_npu, npu_cmd="status")
    for name in ("status", "agent-install", "test-ssh"):
        sp = npu_sub.add_parser(name)
        sp.add_argument("--host")
        sp.add_argument("--workspace", "-w")
        sp.add_argument("--json", action="store_true")
        sp.set_defaults(func=run_npu)
    sp_nb = npu_sub.add_parser("boost", help="Boost an NPU waiting ticket to top priority")
    sp_nb.add_argument("pid", type=int)
    sp_nb.add_argument("--problem", default="")
    sp_nb.add_argument("--sha", default="")
    sp_nb.add_argument("--host")
    sp_nb.add_argument("--json", action="store_true")
    sp_nb.set_defaults(func=run_npu)
    sp_nc = npu_sub.add_parser("cancel", help="Cancel a waiting or building NPU task")
    sp_nc.add_argument("pid", type=int)
    sp_nc.add_argument("--host")
    sp_nc.add_argument("--json", action="store_true")
    sp_nc.set_defaults(func=run_npu)

    s_ws = sub.add_parser("workspace", help="Manage clean AGENTS.md-first contest workspaces")
    ws_sub = s_ws.add_subparsers(dest="ws_cmd", required=True)
    w_ls = ws_sub.add_parser("list")
    w_ls.add_argument("--json", action="store_true")
    w_ls.set_defaults(func=run_workspace)
    w_sel = ws_sub.add_parser("select")
    w_sel.add_argument("path")
    w_sel.add_argument("--json", action="store_true")
    w_sel.set_defaults(func=run_workspace)
    w_init = ws_sub.add_parser("init")
    w_init.add_argument("target_dir")
    w_init.add_argument("--title", default="")
    w_init.add_argument("--contest-id", default="")
    w_init.add_argument("--contest-url", default="")
    w_init.add_argument("--git-owner", default="winbeau")
    w_init.add_argument("--repo-prefix", default="accel-ops")
    w_init.add_argument("--npu-host", default="npu")
    w_init.add_argument("--problems", default="")
    w_init.add_argument("--activate", action="store_true")
    w_init.add_argument("--no-register", action="store_true")
    w_init.add_argument("--no-repos", action="store_true", help="Skip official template fetch / private repo creation")
    w_init.add_argument("--json", action="store_true")
    w_init.set_defaults(func=run_workspace)
    w_sync = ws_sub.add_parser("sync-problems", help="Scrape a Judge contest and refresh the workspace problem list")
    w_sync.add_argument("target_dir", nargs="?", default="")
    w_sync.add_argument("--workspace", "-w", default="")
    w_sync.add_argument("--contest-url", default="")
    w_sync.add_argument("--contest-id", default="")
    w_sync.add_argument("--no-repos", action="store_true", help="Skip official template fetch / private repo creation")
    w_sync.add_argument("--json", action="store_true")
    w_sync.set_defaults(func=run_workspace)
    w_ga = ws_sub.add_parser("gen-agents")
    w_ga.add_argument("target_dir")
    w_ga.add_argument("--json", action="store_true")
    w_ga.set_defaults(func=run_workspace)
    w_rules = ws_sub.add_parser("rules", help="Show each problem's judge rule snapshot, or re-read it from Judge")
    w_rules.add_argument("target_dir", nargs="?", default="")
    w_rules.add_argument("--workspace", "-w", default="")
    w_rules.add_argument("--refresh", action="store_true",
                         help="Read the rule endpoint now with the platform's own login state")
    w_rules.add_argument("--json", action="store_true")
    w_rules.set_defaults(func=run_workspace)

    s_co = sub.add_parser("collab", help="Batch repository collaborator invite")
    co_sub = s_co.add_subparsers(dest="co_cmd", required=True)
    c_inv = co_sub.add_parser("invite")
    c_inv.add_argument("username")
    c_inv.add_argument("--workspace", "-w")
    c_inv.add_argument("--owner", default="")
    c_inv.add_argument("--permission", choices=["push", "admin", "pull"], default="push")
    c_inv.add_argument("--dry-run", action="store_true")
    c_inv.add_argument("--json", action="store_true")
    c_inv.set_defaults(func=run_collab)

    s_sub = sub.add_parser("submit", help="Evaluate 4-Stage Gate and enqueue commit")
    s_sub.add_argument("problem")
    s_sub.add_argument("sha", nargs="?")
    s_sub.add_argument("--note", default="")
    s_sub.add_argument("--now", action="store_true", help="Dispatch rate-paced submission worker immediately")
    s_sub.add_argument("--force", action="store_true")
    s_sub.add_argument("--intent", default="exploit", choices=["exploit", "probe", "restore"],
                       help="4.3 submit intent: exploit (default), probe (speculative, undo+budget), restore")
    s_sub.add_argument("--workspace", "-w")
    s_sub.add_argument("--json", action="store_true")
    s_sub.set_defaults(func=run_submit)

    s_q = sub.add_parser("queue", help="Submission queue & gate actions")
    q_sub = s_q.add_subparsers(dest="q_cmd", required=True)
    q_ls = q_sub.add_parser("list", help="List and filter submissions (supports --breakthrough)")
    q_ls.add_argument("--breakthrough", "-b", action="store_true", help="Filter only breakthrough jumps (Δ >= threshold)")
    q_ls.add_argument("--min-delta", type=float, help="Override breakthrough Δ threshold (pts)")
    q_ls.add_argument("--status", choices=["all", "pending", "completed"], default="all")
    q_ls.add_argument("--problem", "-p")
    q_ls.add_argument("--workspace", "-w")
    q_ls.add_argument("--json", action="store_true")
    q_ls.set_defaults(func=run_queue_list)
    q_cfg = q_sub.add_parser("config")
    q_cfg.add_argument("--auto-consumer")
    q_cfg.add_argument("--closing-reserve", type=int)
    q_cfg.add_argument("--noise-floor", type=float)
    q_cfg.add_argument("--breakthrough-threshold", type=float)
    q_cfg.add_argument("--gate", action="append", metavar="NAME=VALUE",
                       help="4.2.1 gate switch: board_mode_from_rules, tle_arms_escape_lock, circuit_blocks, "
                            "noise_floor_from_judge_sigma, approve_low_gain_positive, low_gain_min_prob")
    q_cfg.add_argument("--lang", choices=["zh", "en"])
    q_cfg.set_defaults(func=run_queue)
    q_app = q_sub.add_parser("approve")
    q_app.add_argument("problem")
    q_app.add_argument("sha")
    q_app.add_argument("--note", default="")
    q_app.add_argument("--workspace", "-w")
    q_app.set_defaults(func=run_queue)
    q_sk = q_sub.add_parser("skip")
    q_sk.add_argument("problem")
    q_sk.add_argument("sha")
    q_sk.add_argument("--reason", default="")
    q_sk.add_argument("--workspace", "-w")
    q_sk.set_defaults(func=run_queue)
    q_re = q_sub.add_parser("restore")
    q_re.add_argument("problem")
    q_re.add_argument("--workspace", "-w")
    q_re.set_defaults(func=run_queue)
    q_sn = q_sub.add_parser("submit-now")
    q_sn.add_argument("problem")
    q_sn.add_argument("sha")
    q_sn.add_argument("--note", default="")
    q_sn.add_argument("--account-id", default=None)
    q_sn.add_argument("--force", action="store_true")
    q_sn.add_argument("--workspace", "-w")
    q_sn.set_defaults(func=run_queue)
    q_wk = q_sub.add_parser("_worker-submit-one")
    q_wk.add_argument("problem")
    q_wk.add_argument("sha")
    q_wk.add_argument("--note", default="")
    q_wk.add_argument("--account-id", default="primary")
    q_wk.add_argument("--force", action="store_true")
    q_wk.add_argument("--workspace", "-w")
    q_wk.set_defaults(func=run_queue)
    q_dr = q_sub.add_parser("drain")
    q_dr.add_argument("--once", action="store_true")
    q_dr.add_argument("--workspace", "-w")
    q_dr.add_argument("--json", action="store_true")
    q_dr.set_defaults(func=run_queue)

    s_ts = sub.add_parser("test", help="3-Phase NPU pipeline (--build-only / --fast Q0 / full Q1)")
    s_ts.add_argument("problem")
    s_ts.add_argument("--sha", default=None)
    s_ts.add_argument("--fast", action="store_true", help="Q0 fast-lane (~10s die lock)")
    s_ts.add_argument("--build-only", action="store_true", help="Phase A lock-free 32-core CPU build (0s die lock)")
    s_ts.add_argument("--pmu", action="store_true", help="Enable L1 PipeUtilization AI-Core PMU counters (slower profiler)")
    s_ts.add_argument("--case", action="append", dest="case_ids", help="Reference or local case ID (repeatable)")
    s_ts.add_argument("--bucket", help="Select a named case group")
    s_ts.add_argument("--profile", help="Use a JSON experiment profile")
    s_ts.add_argument("--experiment", help="Label an isolated run")
    s_ts.add_argument("--remeasure", action="store_true", help="Repeat a completed reference measurement")
    s_ts.add_argument("--compare", metavar="BASE",
                      help="A/B paired run against a baseline commit (or 'board'): both builds alternate in one device job")
    s_ts.add_argument("--cold", action="store_true",
                      help="Run every case in a fresh device session (confirms implausible timings)")
    s_ts.add_argument("--level", choices=["smoke", "standard", "final"],
                      help="Suite level: smoke self-check, standard evidence (default) or final candidate run")
    s_ts.add_argument("--suite", choices=["adaptive", "all"],
                      help="Deprecated; use --level (adaptive = smoke, all = standard)")
    s_ts.add_argument("--runner", help="Use an experimental Python runner implementing the case event protocol")
    s_ts.add_argument("--official-only", action="store_true", help="Omit local variants for an ablation")
    s_ts.add_argument("--timing", choices=["off","perf","all"])
    s_ts.add_argument("--repeats", type=int, help="Independent device timing samples")
    s_ts.add_argument("--dry-run", action="store_true")
    s_ts.add_argument("--sync-only", action="store_true")
    s_ts.add_argument("--workspace", "-w")
    s_ts.set_defaults(func=run_test)

    s_bn = sub.add_parser("bench", help="Stratified benchmark suite & closed-loop score-gap calibration")
    bn_sub = s_bn.add_subparsers(dest="bench_cmd")
    for name, help_text in (("cases", "Inspect the case catalog and stored evidence"),
                            ("compare", "Compare two immutable case-bench run IDs")):
        inspector = bn_sub.add_parser(name, help=help_text)
        inspector.add_argument("problem")
        inspector.add_argument("--workspace", "-w")
        inspector.add_argument("--json", action="store_true")
        if name == "compare":
            inspector.add_argument("before")
            inspector.add_argument("after")
        else:
            inspector.add_argument("--sha", default="HEAD")
            inspector.add_argument("--profile")
        inspector.set_defaults(func=run_bench)
    s_bn.add_argument("--workspace", "-w")
    s_bn.add_argument("--json", action="store_true")
    s_bn.set_defaults(func=run_bench, bench_cmd="status", problem=None)

    bn_su = bn_sub.add_parser("suite", help="Show, migrate or extend the platform-owned test suite")
    bn_su.add_argument("suite_action", choices=["show", "migrate", "add-variant"])
    bn_su.add_argument("problem")
    bn_su.add_argument("--json", dest="variant_json", help="Variant object for add-variant")
    bn_su.add_argument("--workspace", "-w")
    bn_su.set_defaults(func=run_bench_suite)

    bn_cm = bn_sub.add_parser("case-map", help="Map local official cases to judge testcases")
    bn_cm.add_argument("problem")
    bn_cm.add_argument("--rebuild", action="store_true", help="Rebuild and store results/<problem>/.case-map.json")
    bn_cm.add_argument("--json", action="store_true")
    bn_cm.add_argument("--workspace", "-w")
    bn_cm.set_defaults(func=run_bench_judge_align)

    bn_wa = bn_sub.add_parser("wa-repro", help="Multi-seed, tie-rich local reproducers for WA/RE receipts")
    bn_wa.add_argument("problem")
    bn_wa.add_argument("--receipt", help="One submission ID (default: every WA/RE receipt of the problem)")
    bn_wa.add_argument("--like", help='Failure shape when the receipt names no case, e.g. {"shape":[2048,2048],"mode":0}')
    bn_wa.add_argument("--apply", action="store_true", help="Write the variants into the suite (default: list)")
    bn_wa.add_argument("--json", action="store_true")
    bn_wa.add_argument("--workspace", "-w")
    bn_wa.set_defaults(func=run_bench_judge_align)

    bn_rc = bn_sub.add_parser("receipts", help="Normalize judge receipt rows from data on disk")
    bn_rc.add_argument("receipts_action", choices=["backfill"])
    bn_rc.add_argument("--apply", action="store_true", help="Append the rows (default: list only)")
    bn_rc.add_argument("--json", action="store_true")
    bn_rc.add_argument("--workspace", "-w")
    bn_rc.set_defaults(func=run_bench_judge_align)

    bn_st = bn_sub.add_parser("status", help="Show 5-stratum learned weights, efficiency & headroom")
    bn_st.add_argument("problem", nargs="?")
    bn_st.add_argument("--workspace", "-w")
    bn_st.add_argument("--json", action="store_true")
    bn_st.set_defaults(func=run_bench)

    register_fast_tools_subcommands(bn_sub)

    bn_in = bn_sub.add_parser("init", help="Scaffold stratified bench/ & lock SHA-256 oracle")
    bn_in.add_argument("problem")
    bn_in.add_argument("--workspace", "-w")
    bn_in.add_argument("--json", action="store_true")
    bn_in.set_defaults(func=run_bench)

    bn_ca = bn_sub.add_parser("calibrate", help="Run Simplex MAP weight calibration against online scores")
    bn_ca.add_argument("problem", nargs="?")
    bn_ca.add_argument("--workspace", "-w")
    bn_ca.add_argument("--json", action="store_true")
    bn_ca.set_defaults(func=run_bench)

    bn_dg = bn_sub.add_parser("diagnose", help="Diagnose residual gaps, bucket correlations, and bench blind spots")
    bn_dg.add_argument("problem", nargs="?")
    bn_dg.add_argument("--sha", default=None)
    bn_dg.add_argument("--workspace", "-w")
    bn_dg.add_argument("--json", action="store_true")
    bn_dg.set_defaults(func=run_bench)

    bn_ev = bn_sub.add_parser("evolve", help="Append-only bench evolution driven by online residual feedback")
    bn_ev.add_argument("problem", nargs="?")
    bn_ev.add_argument("--dry-run", action="store_true", help="Preview promotions without editing cases_perf.json")
    bn_ev.add_argument("--min-pairs", type=int, default=2)
    bn_ev.add_argument("--min-corr", type=float, default=0.15)
    bn_ev.add_argument("--workspace", "-w")
    bn_ev.add_argument("--json", action="store_true")
    bn_ev.set_defaults(func=run_bench)

    bn_rb = bn_sub.add_parser("refresh-board", help="Refresh live tbest(t), frontier & board scores (auto when >=24h stale) and recalibrate")
    bn_rb.add_argument("--force", action="store_true", help="Refresh even if <24h old")
    bn_rb.add_argument("--max-age-hours", type=float, default=24.0)
    bn_rb.add_argument("--no-calibrate", action="store_true")
    bn_rb.add_argument("--workspace", "-w")
    bn_rb.add_argument("--json", action="store_true")
    bn_rb.set_defaults(func=run_bench)

    bn_bs = bn_sub.add_parser("backfill-status", help="Auto-backfill scheduler progress (degrades cleanly)")
    bn_bs.add_argument("problem", nargs="?")
    bn_bs.add_argument("--workspace", "-w")
    bn_bs.add_argument("--json", action="store_true")
    bn_bs.set_defaults(func=run_bench)

    bn_bf = bn_sub.add_parser("backfill", help="Backfill missing perf buckets on NPU (~20s)")
    bn_bf.add_argument("problem")
    bn_bf.add_argument("--sha", default=None)
    bn_bf.add_argument("--workspace", "-w")
    bn_bf.add_argument("--json", action="store_true")
    bn_bf.set_defaults(func=run_bench)

    bn_ve = bn_sub.add_parser("verify-escape", help="Verify escape reproducer fails on --bad and passes on --good")
    bn_ve.add_argument("problem")
    bn_ve.add_argument("--bad", required=True)
    bn_ve.add_argument("--good", required=True)
    bn_ve.add_argument("--review", help="Request a scoped review trial when the official WA stays locally green")
    bn_ve.add_argument("--workspace", "-w")
    bn_ve.add_argument("--json", action="store_true")
    bn_ve.set_defaults(func=run_bench)

    s_au = sub.add_parser("auth", help="Shared Judge & GitCode authentication")
    au_sub = s_au.add_subparsers(dest="auth_cmd", required=True)
    for name in ("status", "judge-captcha", "judge-logout", "judge-accounts", "gitcode-login"):
        ap = au_sub.add_parser(name)
        ap.add_argument("--json", action="store_true")
        ap.set_defaults(func=run_auth)
    a_cjl = au_sub.add_parser("judge-login")
    a_cjl.add_argument("--account", required=True)
    a_cjl.add_argument("--login-type", default="auto")
    a_cjl.add_argument("--captcha-id", default="")
    a_cjl.add_argument("--captcha-code", default="")
    a_cjl.add_argument("--account-id", default="primary")
    a_cjl.add_argument("--label", default="")
    a_cjl.add_argument("--json", action="store_true")
    a_cjl.set_defaults(func=run_auth)
    a_cji = au_sub.add_parser("judge-import")
    a_cji.add_argument("session_file")
    a_cji.add_argument("--label", default="")
    a_cji.add_argument("--json", action="store_true")
    a_cji.set_defaults(func=run_auth)
    a_cjt = au_sub.add_parser("judge-toggle")
    a_cjt.add_argument("account_id")
    a_cjt.add_argument("--enabled", required=True)
    a_cjt.add_argument("--json", action="store_true")
    a_cjt.set_defaults(func=run_auth)
    captain = au_sub.add_parser("judge-captain", help="Select this contest's ranking account")
    captain.add_argument("account_id")
    captain.add_argument("--workspace", required=True)
    captain.add_argument("--json", action="store_true")
    captain.set_defaults(func=run_auth)
    a_cjr = au_sub.add_parser("judge-remove")
    a_cjr.add_argument("account_id")
    a_cjr.add_argument("--json", action="store_true")
    a_cjr.set_defaults(func=run_auth)

    s_sh = sub.add_parser("ssh", help="Manage NPU SSH host aliases")
    sh_sub = s_sh.add_subparsers(dest="ssh_cmd", required=True)
    sh_sub.add_parser("list").set_defaults(func=run_ssh)
    sh_sel = sh_sub.add_parser("select")
    sh_sel.add_argument("alias")
    sh_sel.set_defaults(func=run_ssh)
    sh_pool = sh_sub.add_parser("pool", help="Show or change the device pool (npu_ssh_hosts); the primary stays in it")
    sh_pool.add_argument("--add", action="append", default=[], metavar="ALIAS")
    sh_pool.add_argument("--remove", action="append", default=[], metavar="ALIAS")
    sh_pool.add_argument("--set", default=None, metavar="A,B", help="Replace the whole pool, in pin order")
    sh_pool.set_defaults(func=run_ssh)
    sh_sv = sh_sub.add_parser("save")
    sh_sv.add_argument("--alias", required=True)
    sh_sv.add_argument("--hostname", required=True)
    sh_sv.add_argument("--port", type=int, default=22)
    sh_sv.add_argument("--user", default="developer")
    sh_sv.add_argument("--identity-file", default="")
    sh_sv.add_argument("--activate", action="store_true")
    sh_sv.add_argument("--write-ssh-config", action="store_true")
    sh_sv.set_defaults(func=run_ssh)

    s_bt = sub.add_parser("breakthrough", help="Structured KernelC breakthrough playbook & Δ filter")
    bt_sub = s_bt.add_subparsers(dest="bt_cmd")
    s_bt.add_argument("--min-delta", type=float, help="Minimum raw score jump Δ (pts)")
    s_bt.add_argument("--problem", "-p")
    s_bt.add_argument("--category", "-c")
    s_bt.add_argument("--include-candidates", action="store_true")
    s_bt.add_argument("--workspace", "-w")
    s_bt.add_argument("--json", action="store_true")
    s_bt.set_defaults(func=run_breakthrough, bt_cmd="list")

    bt_ls = bt_sub.add_parser("list", help="List structured breakthroughs above threshold")
    bt_ls.add_argument("--min-delta", type=float)
    bt_ls.add_argument("--problem", "-p")
    bt_ls.add_argument("--category", "-c")
    bt_ls.add_argument("--include-candidates", action="store_true")
    bt_ls.add_argument("--workspace", "-w")
    bt_ls.add_argument("--json", action="store_true")
    bt_ls.set_defaults(func=run_breakthrough)

    bt_sh = bt_sub.add_parser("show", help="Show full structured breakthrough document")
    bt_sh.add_argument("query", help="Breakthrough ID, commit SHA prefix, or problem title")
    bt_sh.add_argument("--workspace", "-w")
    bt_sh.add_argument("--json", action="store_true")
    bt_sh.set_defaults(func=run_breakthrough)

    bt_th = bt_sub.add_parser("threshold", help="Get or set shared breakthrough Δ threshold")
    bt_th.add_argument("value", nargs="?", type=float)
    bt_th.add_argument("--workspace", "-w")
    bt_th.add_argument("--json", action="store_true")
    bt_th.set_defaults(func=run_breakthrough)

    bt_sy = bt_sub.add_parser("sync", help="Auto-sync verified high-Δ submissions into breakthrough/")
    bt_sy.add_argument("--min-delta", type=float)
    bt_sy.add_argument("--workspace", "-w")
    bt_sy.add_argument("--json", action="store_true")
    bt_sy.set_defaults(func=run_breakthrough)

    bt_ad = bt_sub.add_parser("add", help="Record or enrich a structured breakthrough entry")
    bt_ad.add_argument("problem")
    bt_ad.add_argument("sha")
    bt_ad.add_argument("--title", required=True)
    bt_ad.add_argument("--category", default="tiling-regime")
    bt_ad.add_argument("--bottleneck", default="")
    bt_ad.add_argument("--mechanism", action="append", help="Repeatable mechanism bullet")
    bt_ad.add_argument("--precision-guard", default="")
    bt_ad.add_argument("--rule", default="")
    bt_ad.add_argument("--tags", default="")
    bt_ad.add_argument("--score-before", type=float)
    bt_ad.add_argument("--score-after", type=float)
    bt_ad.add_argument("--workspace", "-w")
    bt_ad.add_argument("--json", action="store_true")
    bt_ad.set_defaults(func=run_breakthrough)

    s_sk = sub.add_parser("skills", help="Optional fallback skills for dsh & codex")
    sk_sub = s_sk.add_subparsers(dest="skills_cmd", required=True)
    sk_st = sk_sub.add_parser("status")
    sk_st.add_argument("--json", action="store_true")
    sk_st.set_defaults(func=run_skills)
    sk_in = sk_sub.add_parser("install")
    sk_in.add_argument("--targets", default="dsh,codex")
    sk_in.add_argument("--json", action="store_true")
    sk_in.set_defaults(func=run_skills)

    s_dm = sub.add_parser("daemon", help="Local persistent HTTP/SSE telemetry & state cache daemon")
    dm_sub = s_dm.add_subparsers(dest="daemon_cmd", required=True)
    dm_start = dm_sub.add_parser("start", help="Start the local Parhelion daemon")
    dm_start.add_argument("--background", action="store_true")
    dm_start.add_argument("--port", type=int, default=18910)
    dm_start.add_argument("--workspace", "-w")
    dm_start.add_argument("--host")
    dm_start.add_argument("--json", action="store_true")
    dm_start.set_defaults(func=run_daemon)

    dm_stop = dm_sub.add_parser("stop", help="Stop the running Parhelion daemon")
    dm_stop.add_argument("--json", action="store_true")
    dm_stop.set_defaults(func=run_daemon)

    dm_st = dm_sub.add_parser("status", help="Inspect running Parhelion daemon health")
    dm_st.add_argument("--json", action="store_true")
    dm_st.set_defaults(func=run_daemon)

    dm_rl = dm_sub.add_parser("reload", help="Hot-reload the daemon's scheduling modules without a restart")
    dm_rl.add_argument("--json", action="store_true")
    dm_rl.set_defaults(func=run_daemon)

    register_kb_subcommands(sub)
    register_replay_subcommands(sub)
    register_close_subcommands(sub)

    register_doctor_subcommands(sub)
    register_driver_and_mcp_subcommands(sub)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
