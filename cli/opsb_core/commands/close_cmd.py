"""CLI: ``opsb close-sweep`` — the closing reconciliation of a contest (4.3 P25).

The command forwards to ``scheduler.cli_bridge close-sweep`` and never writes by itself: the three
lists are read-only, and ``--enqueue`` hands each restore row to the normal gate with
``intent=restore`` (approval, quota and the administrator's auto-submit switch still decide
dispatch). Nothing is merged, no branch is touched and no restore is forced.
"""
from __future__ import annotations

import argparse
import json
import sys


def _workspace(arg: str | None):
    from ..config import ConfigRepository

    return ConfigRepository.resolve_workspace(arg, ConfigRepository.load())


def _row(line: list) -> str:
    return "  " + "  ".join(str(item) for item in line)


def _print_text(doc: dict) -> None:
    totals = doc.get("totals") or {}
    window = doc.get("closing") or {}
    print(f"Close sweep · {doc.get('as_of', '')[:16]}Z · board mode {doc.get('board_mode') or {}}")
    end = window.get("contest_end")
    print(f"Closing window: {'OPEN' if window.get('in_window') else 'closed'} "
          f"({window.get('window_min')} min before {end[:16] + 'Z' if end else 'no contest end'}, "
          f"restore x{window.get('restore_boost')})")
    print(f"\nNeeds restore ({totals.get('restore')}, {totals.get('restore_gap')} pts / "
          f"{totals.get('restore_weighted_gap')} weighted, {totals.get('restore_beyond_sigma')} beyond sigma):")
    for r in doc.get("restore") or []:
        flag = "CRITICAL " if r.get("critical") else ""
        print(_row([r["title"], f"{flag}{r['best_sha']} {r['best_score']:.2f} -> "
                    f"{r['board_sha'] or '-'} {r['board_score']:.2f}", f"gap {r['gap']:+.2f}",
                    f"w {r['weighted_gap']:.2f}", f"sigma {r['sigma_hat']:.2f}({r['sigma_scope']})"]))
    print(f"\nNeeds merge into main ({totals.get('merge')}), branches only — nothing is merged here:")
    for r in doc.get("merge") or []:
        print(_row([r["title"], f"{r['best_sha']} {r['best_score']:.2f}",
                    f"main {r['main_sha'] or '-'}", ", ".join(r.get("branches") or []) or r.get("git_error", "")]))
    print(f"\nNeeds best/* refresh ({totals.get('tag')}):")
    for r in doc.get("tag") or []:
        print(_row([r["title"], r.get("tag") or "(none)", f"-> {r['target']}"]))
    notice = doc.get("window_notice") or {}
    if notice.get("event_key"):
        print(f"\nWindow notice: {'sent' if notice.get('sent') else notice.get('reason')}")
    queued = doc.get("enqueued")
    if queued is not None:
        print(f"\nEnqueued through the gate ({sum(1 for q in queued if q.get('enqueued'))}/{len(queued)}):")
        for q in queued:
            print(_row([q["title"], q["sha"], q.get("status_tag") or q.get("error") or "",
                        f"priority {q.get('priority_score')}" if q.get("enqueued") else ""]))


def run_close_sweep(args: argparse.Namespace) -> int:
    """Read the closing reconciliation; ``--enqueue`` prices each restore through the gate."""
    from ..ipc_bridge import invoke_bridge

    ws = _workspace(args.workspace)
    doc = invoke_bridge(ws, "close-sweep", (["--enqueue"] if args.enqueue else []))
    if not isinstance(doc, dict) or not doc.get("view"):
        print("close-sweep failed: the scheduler bridge returned no document", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(doc, indent=2, ensure_ascii=False, allow_nan=False))
    else:
        _print_text(doc)
    return 0


def register_close_subcommands(sub) -> None:
    """Register ``opsb close-sweep`` (the only P25 entry point; the view is ``parhelion_report``)."""
    p = sub.add_parser("close-sweep", help="Closing reconciliation: restore, merge and best/* lists")
    p.add_argument("--workspace", "-w", default=None)
    p.add_argument("--enqueue", action="store_true",
                   help="Queue each restore through the gate with intent=restore (no force, no approve)")
    p.add_argument("--json", action="store_true", help="Print the reconciliation document as JSON")
    p.set_defaults(func=run_close_sweep)
