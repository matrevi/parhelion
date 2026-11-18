"""CLI commands: `opsb bridge {status,sync,pull,publish,collect,push,watch}`."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..config import ConfigRepository
from ..services.bridge_service import (
    bridge_status,
    collect_summaries,
    publish_receipt,
    pull_items,
    push_deliveries,
    sync_deliveries,
    watch,
)


def _read_stdin_or_file(file_arg: str | None) -> str:
    if file_arg and file_arg not in ("-", "stdin"):
        return Path(file_arg).expanduser().read_text(encoding="utf-8", errors="replace")
    if sys.stdin is not None and not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit(
        "opsb bridge publish: nothing to read (stdin is a terminal). "
        "Pass `--file <path>` or pipe the summary: `... | opsb bridge publish <agent> <entry> --file -`"
    )


def run_bridge(args: argparse.Namespace) -> int:
    cfg = ConfigRepository.load()
    ws = ConfigRepository.resolve_workspace(getattr(args, "workspace", None), cfg)
    sub = getattr(args, "bridge_cmd", None) or "status"
    as_json = bool(getattr(args, "json", False))
    bridge_dir = getattr(args, "bridge_dir", None)

    if sub == "sync":
        res = sync_deliveries(
            ws,
            bridge_dir=bridge_dir,
            min_delta=getattr(args, "min_delta", None),
            agent_ids=getattr(args, "agent", None),
            dry_run=bool(getattr(args, "dry_run", False)),
        )
        if as_json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0 if res.get("ok") else 1
        tag = " (dry-run)" if res.get("dry_run") else ""
        print(
            f"Bridge sync{tag}: +{res['delivered_count']} delivered, "
            f"{res['skipped_count']} already delivered, {res['error_count']} error(s) · "
            f"{res['entries_scanned']} entries × {res['agents']} agent(s)"
        )
        for item in res.get("delivered", []):
            print(f"  → {item['agent_id']:<22} {item['sha8']}-{item['entry_id']}.md")
        for err in res.get("errors", []):
            print(f"  ! {err.get('agent_id')} {err.get('entry_id')}: {err.get('error')}", file=sys.stderr)
        print(f"  dir: {res['bridge_dir']}")
        return 0 if res.get("ok") else 1

    if sub == "pull":
        res = pull_items(
            ws,
            args.agent_id,
            ack=bool(getattr(args, "ack", False)),
            bridge_dir=bridge_dir,
            entry_ids=getattr(args, "entry", None),
        )
        if as_json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0 if res.get("ok") else 1
        if not res.get("ok"):
            print(res.get("error"), file=sys.stderr)
            known = res.get("known_agents") or []
            if known:
                print("known agents: " + ", ".join(known), file=sys.stderr)
            return 1
        print(f"=== bridge pull · {res['agent_id']} ({res.get('display')}) · {res['count']} item(s){' · acked' if res.get('acked') else ''} ===")
        if not res["items"]:
            print("  (nothing pending — run `opsb bridge sync` or check the outbox)")
        for item in res["items"]:
            print("-" * 92)
            print(f"[{item['state']}] {item['entry_id']}  Δ{item.get('delta_raw') or 0:+.2f}  {item.get('problem') or ''}")
            print(item["note"])
        return 0

    if sub == "publish":
        body = _read_stdin_or_file(getattr(args, "file", None))
        res = publish_receipt(ws, args.agent_id, args.entry_id, body, bridge_dir=bridge_dir)
        if as_json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0 if res.get("ok") else 1
        if not res.get("ok"):
            print(res.get("error"), file=sys.stderr)
            if res.get("hint"):
                print(res["hint"], file=sys.stderr)
            return 1
        print(f"Receipt saved: {res['outbox']} ({res['lines']} line(s)) — run `opsb bridge collect` to fold it into index.json")
        return 0

    if sub == "collect":
        res = collect_summaries(ws, bridge_dir=bridge_dir, agent_ids=getattr(args, "agent", None))
        if as_json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0 if res.get("ok") else 1
        print(
            f"Bridge collect: +{res['collected_count']} summary(ies) → {res['index_path']} "
            f"({res['skipped_count']} unchanged, {res['orphaned_count']} orphaned, {res['total_summaries']} total)"
        )
        for row in res.get("collected", []):
            print(f"  ✓ {row['agent_id']}/{row['entry_id']} · {row['lines']} line(s)")
        for row in res.get("orphaned", []):
            print(f"  ? orphaned receipt (unknown entry): {row['agent_id']}/{row['entry_id']}", file=sys.stderr)
        return 0

    if sub == "push":
        res = push_deliveries(
            ws,
            bridge_dir=bridge_dir,
            agent_ids=[args.agent_id] if getattr(args, "agent_id", None) else None,
            entry_id=getattr(args, "entry", None),
            dry_run=bool(getattr(args, "dry_run", False)),
        )
        if as_json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0 if res.get("ok") else 1
        print(
            f"Bridge push: {res['pushed_count']} pushed, {res['failed_count']} failed "
            f"({res['considered']} exec/http candidate(s))"
        )
        for row in res.get("pushed", []):
            print(f"  ✓ {row['agent_id']}/{row['entry_id']} [{row['mode']}] {row.get('detail', '')}")
        for row in res.get("failed", []):
            print(f"  ! {row['agent_id']}/{row['entry_id']} [{row['mode']}] {row.get('error')}", file=sys.stderr)
        if res["considered"] == 0:
            print("  (no exec/http deliveries pending; file-transport agents need no push)")
        return 0 if res.get("ok") else 1

    if sub == "watch":
        def _tick(tick: dict) -> None:
            s, p = tick["sync"], tick["push"]
            if as_json:
                print(json.dumps(tick, ensure_ascii=False), flush=True)
            else:
                print(
                    f"[{tick['ts'][:19]}] sync +{s.get('delivered_count', 0)} "
                    f"(skip {s.get('skipped_count', 0)}, err {s.get('error_count', 0)}) · "
                    f"push {p.get('pushed_count', 0)}/{p.get('failed_count', 0)}",
                    flush=True,
                )

        if not as_json:
            print(
                f"bridge watch · {ws} · interval {max(2.0, float(getattr(args, 'interval', 20))):.0f}s · "
                f"escalate={'off' if getattr(args, 'no_escalate', False) else 'on'} (Ctrl-C to stop)"
            )
        try:
            return watch(
                ws,
                interval=getattr(args, "interval", 20),
                once=bool(getattr(args, "once", False)),
                escalate=not bool(getattr(args, "no_escalate", False)),
                bridge_dir=bridge_dir,
                on_tick=_tick,
            )
        except KeyboardInterrupt:
            print("\nbridge watch stopped", file=sys.stderr)
            return 0

    # Default: `opsb bridge status`
    res = bridge_status(ws, bridge_dir=bridge_dir, agent_id=getattr(args, "agent_id", None))
    if as_json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    totals = res["totals"]
    print(f"=== Bridge · {res['bridge_dir']} · {res['agent_count']} subscriber(s) ===")
    print(
        f"  totals: 待总结 {totals['pending']} · 已取 {totals['pulled']} · 已回执 {totals['acked']} "
        f"| index summaries {res['summaries_count']} | ledger {res['ledger_records']} record(s)"
    )
    if not res["agents"]:
        print("  (no subscribers — edit bridge/agents.json, see bridge/README.md)")
        return 0
    for agent in res["agents"]:
        counts = agent["counts"]
        mode = (agent.get("transport") or {}).get("mode")
        disabled = "" if agent.get("enabled", True) else " [disabled]"
        print(
            f"  {agent['agent_id']:<24} {agent.get('kind'):<12} {mode:<5} "
            f"待总结 {counts['pending']} · 已取 {counts['pulled']} · 已回执 {counts['acked']}{disabled}"
        )
        for item in agent["items"]:
            mark = {"pending": "·", "pulled": "→", "acked": "✓"}.get(item["state"], "?")
            print(
                f"      {mark} {item['entry_id']:<40} {item['state']:<8} "
                f"Δ{float(item.get('delta_raw') or 0):+6.2f} [{item['sha8']}]"
            )
    return 0
