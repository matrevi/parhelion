"""CLI commands: `opsb kb {query, list, show, record, feedback, history, rollback, project, seed}`."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ..services.triad_kb_service import (
    ensure_triad_kb_seeded,
    format_prompt_cache_digest,
    get_kb_entry,
    get_kb_evolution_history,
    list_kb_entries,
    migrate_hardware_entries_from_community,
    project_readonly_skills,
    query_kb,
    record_kb_entry,
    record_kb_feedback,
    revert_event,
    rollback_last,
)


def _flatten_csv_args(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for item in values or []:
        for part in str(item).split(","):
            tok = part.strip()
            if tok and tok not in out:
                out.append(tok)
    return out


def _print_error(msg: str, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False, indent=2))
    else:
        print(f"Error: {msg}", file=sys.stderr)
    return 1


def _handle_query(args: argparse.Namespace) -> int:
    as_json = bool(getattr(args, "json", False))
    res = query_kb(
        bottlenecks=_flatten_csv_args(getattr(args, "bottleneck", None)) or None,
        regime=getattr(args, "regime", None),
        dtype=getattr(args, "dtype", None),
        pillar=getattr(args, "pillar", None),
        text=getattr(args, "text", "") or "",
        include_anti_patterns=not bool(getattr(args, "no_anti_patterns", False)),
        limit=int(getattr(args, "limit", 8) or 8),
    )
    wants_digest = bool(getattr(args, "digest", False))
    max_chars = int(getattr(args, "max_chars", 2400) or 2400)
    if wants_digest:
        digest = format_prompt_cache_digest(res, max_chars=max_chars)
        if as_json:
            payload = dict(res)
            payload["digest"] = digest
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(digest)
        return 0

    if as_json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    print(
        f"=== Triad KB Query · {res['result_count']} result(s), "
        f"{res['anti_pattern_count']} anti-pattern warning(s) ==="
    )
    if res["anti_patterns"]:
        print("  [DO NOT REPEAT · Anti-Patterns]")
        for ap in res["anti_patterns"]:
            print(f"    ! {ap['id']} (fails={ap['fails']}): {ap['title']}")
            print(f"      {ap['summary']}")
    results = res.get("results") if "results" in res else (res.get("entries") or [])
    if results:
        print("  [Ranked Knowledge Entries]")
        for it in results:
            print(
                f"    * [{it['pillar']}|{it['status']}|conf={it['confidence']:.3f}|{it['portability']}] "
                f"{it['id']} — {it['title']} (score={it['query_score']:.3f})"
            )
            print(f"      {it['summary']}")
    elif not res["anti_patterns"]:
        print("  (No matching entries found.)")
    return 0


def _handle_list(args: argparse.Namespace) -> int:
    as_json = bool(getattr(args, "json", False))
    res = list_kb_entries(
        pillar=getattr(args, "pillar", "all") or "all",
        status=getattr(args, "status", None),
        portability=getattr(args, "portability", None),
    )
    if as_json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    print(f"=== Triad KB ({res['pillar']}) · {res['count']} entries · {res['parhelion_home']} ===")
    for e in res["entries"]:
        print(
            f"  [{e['pillar']:<12}] {e['id']:<38} {e['status']:<12} "
            f"conf={e['confidence']:.3f} (w={e['wins']}/f={e['fails']}) [{e['portability']}]"
        )
        print(f"      {e['title']}")
    return 0


def _handle_show(args: argparse.Namespace) -> int:
    as_json = bool(getattr(args, "json", False))
    entry = get_kb_entry(args.entry_id)
    if entry is None:
        return _print_error(f"No KB entry matching '{args.entry_id}'.", as_json)
    if as_json:
        print(json.dumps({"ok": True, "entry": entry}, ensure_ascii=False, indent=2))
        return 0
    print(
        f"# {entry['id']} [{entry['pillar']} · {entry['status']} · conf={entry['confidence']:.3f} · {entry['portability']}]"
    )
    print(f"Title      : {entry['title']}")
    print(f"Summary    : {entry['summary']}")
    print(f"Bottlenecks: {', '.join(entry.get('bottlenecks') or [])}")
    print(f"Regimes    : {', '.join(entry.get('regimes') or [])}")
    print(f"Dtypes     : {', '.join(entry.get('dtypes') or [])}")
    if entry.get("doc_path"):
        print(f"Doc        : {entry['doc_path']}")
    if entry.get("content"):
        print("\n" + entry["content"].rstrip())
    return 0


def _handle_record(args: argparse.Namespace) -> int:
    as_json = bool(getattr(args, "json", False))
    ev: dict[str, Any] = {
        "workspace": getattr(args, "workspace", "") or "",
        "problem": args.problem,
        "sha": args.sha,
        "verdict": args.verdict,
        "note": getattr(args, "note", "") or "",
    }
    if getattr(args, "delta_raw", None) is not None:
        ev["delta_raw"] = float(args.delta_raw)
    if getattr(args, "delta_us", None) is not None:
        ev["delta_us"] = float(args.delta_us)
    if getattr(args, "speedup", None) is not None:
        ev["speedup"] = float(args.speedup)

    payload: dict[str, Any] = {
        "title": args.title,
        "summary": args.summary,
        "bottlenecks": _flatten_csv_args(getattr(args, "bottleneck", None)),
        "regimes": _flatten_csv_args(getattr(args, "regime", None)),
        "dtypes": _flatten_csv_args(getattr(args, "dtype", None)),
        "tags": _flatten_csv_args(getattr(args, "tag", None)),
        "related_snippets": _flatten_csv_args(getattr(args, "related_snippet", None)),
        "evidence": ev,
    }
    if getattr(args, "id", None):
        payload["id"] = args.id
    if getattr(args, "portability", None):
        payload["portability"] = args.portability

    code_file = getattr(args, "code_file", None)
    if code_file:
        cf = Path(code_file).expanduser()
        if not cf.is_file():
            return _print_error(f"Code/doc file not found: {cf}", as_json)
        txt = cf.read_text(encoding="utf-8", errors="replace")
        if args.pillar == "snippets":
            payload["code"] = txt
        else:
            payload["markdown"] = txt

    res = record_kb_entry(args.pillar, payload)
    if as_json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    ent = res["entry"]
    print(
        f"Recorded [{ent['pillar']}] {ent['id']} ({ent['status']}, conf={ent['confidence']:.3f}, doc={ent['doc_path']})"
    )
    return 0


def _handle_feedback(args: argparse.Namespace) -> int:
    as_json = bool(getattr(args, "json", False))
    ev: dict[str, Any] = {
        "workspace": getattr(args, "workspace", "") or "",
        "problem": args.problem,
        "sha": args.sha,
        "verdict": args.verdict,
        "note": getattr(args, "note", "") or "",
    }
    if getattr(args, "delta_raw", None) is not None:
        ev["delta_raw"] = float(args.delta_raw)
    if getattr(args, "delta_us", None) is not None:
        ev["delta_us"] = float(args.delta_us)
    if getattr(args, "speedup", None) is not None:
        ev["speedup"] = float(args.speedup)

    snip_code: str | None = None
    snip_file = getattr(args, "promote_snippet_file", None)
    if snip_file:
        sf = Path(snip_file).expanduser()
        if not sf.is_file():
            return _print_error(f"Snippet file not found: {sf}", as_json)
        snip_code = sf.read_text(encoding="utf-8", errors="replace")

    res = record_kb_feedback(
        entry_id=args.entry_id,
        verdict=args.verdict,
        evidence=ev,
        promote_snippet_code=snip_code,
    )
    if as_json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    ent = res["entry"]
    print(
        f"Updated {ent['id']}: status={ent['status']} ({res['transition']}), "
        f"conf={ent['confidence']:.3f} (w={ent['wins']}/f={ent['fails']})"
    )
    if res.get("created_snippet_id"):
        print(f"  Promoted linked snippet: {res['created_snippet_id']}")
    return 0


def _handle_history(args: argparse.Namespace) -> int:
    as_json = bool(getattr(args, "json", False))
    res = get_kb_evolution_history(limit=int(getattr(args, "limit", 20) or 20))
    if as_json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    print(f"=== KB Evolution Audit Log ({res['total_events']} total) · {res['log_path']} ===")
    if not res["events"]:
        print("  (No evolution events recorded yet.)")
        return 0
    for ev in res["events"]:
        rev = " [REVERTED]" if ev.get("reverted") else ""
        trans = f" ({ev['transition']})" if ev.get("transition") and ev.get("transition") != "unchanged" else ""
        print(
            f"  {str(ev.get('timestamp', ''))[:19]}  {ev.get('event_id', ''):<24}  "
            f"{ev.get('action', ''):<8}  {ev.get('entry_id', ''):<34}{trans}{rev}"
        )
    return 0


def _handle_rollback(args: argparse.Namespace) -> int:
    as_json = bool(getattr(args, "json", False))
    ev_id = getattr(args, "event_id", None)
    res = revert_event(ev_id) if ev_id else rollback_last()
    if as_json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    print(f"Reverted {res['reverted_event_id']} ({res['pillar']}/{res['entry_id']})")
    return 0


def _handle_project(args: argparse.Namespace) -> int:
    as_json = bool(getattr(args, "json", False))
    res = project_readonly_skills(
        workspace=Path(args.workspace),
        target_rel=getattr(args, "target_rel", ".agents/skills") or ".agents/skills",
    )
    if as_json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    print(
        f"Projected {res['file_count']} read-only ({res['file_mode']}) KB skill files into {res['target_dir']} "
        f"(.claude/skills: {res['claude_skills_symlink']})"
    )
    return 0


def _handle_seed(args: argparse.Namespace) -> int:
    as_json = bool(getattr(args, "json", False))
    tpl = Path(args.templates_root) if getattr(args, "templates_root", None) else None
    res = ensure_triad_kb_seeded(templates_root=tpl)
    if as_json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    print(
        f"Seeded 5-Pillar KB at {res['parhelion_home']}: seeded={res['seeded_pillars']}, "
        f"copied={res['copied_count']} file(s)."
    )
    return 0


def _handle_migrate_hardware(args: argparse.Namespace) -> int:
    as_json = bool(getattr(args, "json", False))
    dry_run = bool(getattr(args, "dry_run", False))
    res = migrate_hardware_entries_from_community(dry_run=dry_run)
    if as_json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    if dry_run:
        print(
            f"[dry-run] Would migrate {res['to_move_count']} hardware entries out of community "
            f"(remaining community={res['remaining_community_count']})."
        )
    else:
        print(
            f"Migrated {res['migrated_count']} hardware entries to {res['parhelion_home']}/hardware "
            f"(hardware={res['hardware_total']}, community={res['community_total']})."
        )
    return 0


def run_kb(args: argparse.Namespace) -> int:
    as_json = bool(getattr(args, "json", False))
    cmd = getattr(args, "kb_cmd", "list") or "list"
    if cmd in ("sync-bugs","annotate-bug"):
        from ..config import ConfigRepository
        from ..knowledge.bugs import sync_bugs, annotate_bug
        if cmd == "sync-bugs":
            ws = ConfigRepository.resolve_workspace(args.workspace, ConfigRepository.load())
            result = sync_bugs(ws)
        else:
            try:
                result = annotate_bug(args.entry_id,args.lesson)
            except ValueError as exc:
                return _print_error(str(exc),as_json)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    handlers = {
        "query": _handle_query,
        "list": _handle_list,
        "show": _handle_show,
        "record": _handle_record,
        "feedback": _handle_feedback,
        "history": _handle_history,
        "rollback": _handle_rollback,
        "project": _handle_project,
        "seed": _handle_seed,
        "migrate-hardware": _handle_migrate_hardware,
    }
    handler = handlers.get(cmd)
    if handler is None:
        return _print_error(f"Unknown kb subcommand: {cmd}", as_json)
    try:
        return handler(args)
    except ValueError as exc:
        return _print_error(str(exc), as_json)


def register_kb_subcommands(sub: argparse._SubParsersAction) -> None:
    """Register `opsb kb` subcommands onto the main `opsb` subparsers."""
    s_kb = sub.add_parser("kb", help="Self-evolving 5-Pillar Knowledge Base (breakthrough / snippets / hardware / community)")
    kb_sub = s_kb.add_subparsers(dest="kb_cmd")
    sync = kb_sub.add_parser("sync-bugs", help="Reconcile incident cards with this contest's judge ledger")
    sync.add_argument("--workspace","-w")
    sync.set_defaults(func=run_kb)
    note = kb_sub.add_parser("annotate-bug", help="Update a reproduction or prevention note")
    note.add_argument("entry_id")
    note.add_argument("--lesson",required=True)
    note.set_defaults(func=run_kb)
    s_kb.add_argument("--pillar", "-p", choices=["breakthrough", "snippets", "hardware", "community", "bugs", "all"], default="all")
    s_kb.add_argument("--status", "-s", choices=["verified", "unverified", "deprecated", "anti-pattern", "all"], default="all")
    s_kb.add_argument("--portability", choices=["native-accel", "portable", "remap-to-accel", "anti-pattern"], default=None)
    s_kb.add_argument("--json", action="store_true")
    s_kb.set_defaults(func=run_kb, kb_cmd="list")

    k_q = kb_sub.add_parser("query", help="Query 5-Pillar KB across all five pillars by hardware symptom")
    k_q.add_argument("--bottleneck", "-b", action="append", help="Bottleneck tag (repeatable or comma-separated)")
    k_q.add_argument("--regime", "-r", help="Regime (PUBLIC, S_SMALL_CORE, M_MID_UB, L_HBM_TILED, X_EXTREME)")
    k_q.add_argument("--dtype", "-d", help="Data type (float32, float16, bfloat16, int8, int32)")
    k_q.add_argument("--pillar", "-p", choices=["breakthrough", "snippets", "hardware", "community", "bugs", "all"], default=argparse.SUPPRESS)
    k_q.add_argument("--text", "-q", default="", help="Free-text search terms")
    k_q.add_argument("--limit", type=int, default=8)
    k_q.add_argument("--no-anti-patterns", action="store_true")
    k_q.add_argument("--digest", action="store_true", help="Format prompt-cache-safe Markdown digest")
    k_q.add_argument("--max-chars", type=int, default=2400)
    k_q.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    k_q.set_defaults(func=run_kb)

    k_ls = kb_sub.add_parser("list", help="List 5-Pillar KB entries")
    k_ls.add_argument("--pillar", "-p", choices=["breakthrough", "snippets", "hardware", "community", "bugs", "all"], default=argparse.SUPPRESS)
    k_ls.add_argument("--status", "-s", choices=["verified", "unverified", "deprecated", "anti-pattern", "all"], default=argparse.SUPPRESS)
    k_ls.add_argument("--portability", choices=["native-accel", "portable", "remap-to-accel", "anti-pattern"], default=argparse.SUPPRESS)
    k_ls.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    k_ls.set_defaults(func=run_kb)

    k_sh = kb_sub.add_parser("show", help="Show full KB entry metadata and code/Markdown content")
    k_sh.add_argument("entry_id")
    k_sh.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    k_sh.set_defaults(func=run_kb)

    k_rc = kb_sub.add_parser("record", help="Record an evidence-gated snippet, hardware law, or community card")
    k_rc.add_argument("--pillar", required=True, choices=["snippets", "hardware", "community"])
    k_rc.add_argument("--title", required=True)
    k_rc.add_argument("--summary", required=True)
    k_rc.add_argument("--problem", required=True)
    k_rc.add_argument("--sha", required=True)
    k_rc.add_argument("--verdict", required=True, choices=["win", "fail", "anti-pattern"])
    k_rc.add_argument("--bottleneck", action="append", help="Repeatable or comma-separated bottleneck tag")
    k_rc.add_argument("--regime", action="append", help="Repeatable or comma-separated regime")
    k_rc.add_argument("--dtype", action="append", help="Repeatable or comma-separated dtype")
    k_rc.add_argument("--tag", action="append")
    k_rc.add_argument("--related-snippet", action="append")
    k_rc.add_argument("--portability", choices=["native-accel", "portable", "remap-to-accel", "anti-pattern"])
    k_rc.add_argument("--code-file", help="Path to .h code or .md doc body")
    k_rc.add_argument("--delta-raw", type=float)
    k_rc.add_argument("--delta-us", type=float)
    k_rc.add_argument("--speedup", type=float)
    k_rc.add_argument("--note", default="")
    k_rc.add_argument("--workspace", default="")
    k_rc.add_argument("--id", help="Optional explicit entry ID")
    k_rc.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    k_rc.set_defaults(func=run_kb)

    k_fb = kb_sub.add_parser("feedback", help="Record evidence-gated win/fail/anti-pattern feedback on an entry")
    k_fb.add_argument("entry_id")
    k_fb.add_argument("--verdict", required=True, choices=["win", "fail", "anti-pattern"])
    k_fb.add_argument("--problem", required=True)
    k_fb.add_argument("--sha", required=True)
    k_fb.add_argument("--delta-raw", type=float)
    k_fb.add_argument("--delta-us", type=float)
    k_fb.add_argument("--speedup", type=float)
    k_fb.add_argument("--note", default="")
    k_fb.add_argument("--workspace", default="")
    k_fb.add_argument("--promote-snippet-file", help="Optional .h code file to create/link on verified win")
    k_fb.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    k_fb.set_defaults(func=run_kb)

    k_hi = kb_sub.add_parser("history", help="Inspect KB evolution audit log")
    k_hi.add_argument("--limit", type=int, default=20)
    k_hi.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    k_hi.set_defaults(func=run_kb)

    k_rb = kb_sub.add_parser("rollback", help="Revert latest or specific KB evolution event")
    k_rb.add_argument("--event-id", default=None)
    k_rb.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    k_rb.set_defaults(func=run_kb)

    k_pr = kb_sub.add_parser("project", help="Project read-only (0400) KB skills into a workspace")
    k_pr.add_argument("workspace")
    k_pr.add_argument("--target-rel", default=".agents/skills")
    k_pr.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    k_pr.set_defaults(func=run_kb)

    k_sd = kb_sub.add_parser("seed", help="Non-destructively seed 5-Pillar KB snippets, hardware & community templates")
    k_sd.add_argument("--templates-root", default=None)
    k_sd.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    k_sd.set_defaults(func=run_kb)

    k_mh = kb_sub.add_parser("migrate-hardware", help="Idempotently extract hardware-discovered laws from community/ into hardware/")
    k_mh.add_argument("--dry-run", action="store_true", help="Preview classification without mutating files")
    k_mh.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    k_mh.set_defaults(func=run_kb)
