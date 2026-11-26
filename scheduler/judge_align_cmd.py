"""Bridge commands ``case-map``, ``receipts`` and ``wa-repro`` (``opsb bench ...``)."""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

from . import case_map, receipt_backfill, receipts, wa_repro
from .parhelion_paths import load_problem_manifest
from .suite import is_worker


def add_parsers(sub, common) -> None:
    p_cm = sub.add_parser("case-map", parents=[common], help="Local official case <-> judge testcase map")
    p_cm.add_argument("--title", default=None, help="Operator problem title (default: every problem)")
    p_cm.add_argument("--rebuild", action="store_true", help="Rebuild and rewrite the stored map")
    p_rc = sub.add_parser("receipts", parents=[common], help="Normalize judge receipt rows")
    p_rc.add_argument("op", choices=["backfill"])
    p_rc.add_argument("--apply", action="store_true", help="Append the backfill rows (default: list only)")
    p_wa = sub.add_parser("wa-repro", parents=[common], help="Local reproducers for WA/RE receipts")
    p_wa.add_argument("--title", required=True, help="Operator problem title")
    p_wa.add_argument("--receipt", default=None, help="One submission ID (default: every WA/RE receipt)")
    p_wa.add_argument("--like", default=None, help='Failure shape when the receipt names no case, e.g. {"shape":[2048,2048],"mode":0}')
    p_wa.add_argument("--apply", action="store_true", help="Write the variants (default: list the plan)")


def run(args: argparse.Namespace) -> int:
    ws = pathlib.Path(args.workspace).resolve()
    if args.command == "wa-repro":
        return _wa_repro(ws, args)
    if args.command == "receipts":
        if args.apply and is_worker():
            print("Workers cannot rewrite the receipt journal", file=sys.stderr)
            return 2
        payload = {"command": "receipts backfill", "apply": bool(args.apply),
                   **receipt_backfill.run(ws, apply=bool(args.apply))}
    else:
        titles = [args.title] if args.title else [p["title"] for p in load_problem_manifest(ws) if p.get("title")]
        maps = []
        for title in titles:
            # Listing never writes; --rebuild rewrites the stored map.
            doc = case_map.current(ws, title, rebuild=bool(args.rebuild), write=bool(args.rebuild))
            maps.append(doc if args.title else case_map.summary(doc))
        payload = {"command": "case-map", "rebuilt": bool(args.rebuild), "maps": maps}
    print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


def _wa_repro(ws: pathlib.Path, args: argparse.Namespace) -> int:
    if args.apply and is_worker():
        print("Workers cannot edit the suite; the platform owns it", file=sys.stderr)
        return 2
    like = json.loads(args.like) if args.like else None
    if like is not None and not isinstance(like, dict):
        print("--like must be a JSON object", file=sys.stderr)
        return 2
    rows = [r for r in receipts.load(ws, args.title) if r.get("status") in wa_repro.FAILED
            and (args.receipt is None or r.get("submission_id") == args.receipt)]
    repo = None
    if args.apply:
        from .case_bench import has_reference, problem_repo

        try:
            repo = problem_repo(ws, args.title)
            repo = repo if has_reference(repo, "HEAD") else None
        except (ValueError, OSError):
            repo = None  # variants are still validated by the suite schema, not against the catalog
    out = []
    for row in rows:
        sid = str(row["submission_id"])
        if args.apply:
            result = wa_repro.apply(ws, args.title, row, like=like, repo=repo)
            entry = result.get("entry") or {}
            item = {"submission_id": sid, "applied": result.get("applied"), "added": len(result.get("added") or []),
                    "reason": result.get("reason") or entry.get("reason"), "variants": entry.get("variants")}
        elif (wa_repro.load_manifest(ws, args.title)["receipts"].get(sid) or {}).get("variants"):
            entry = wa_repro.load_manifest(ws, args.title)["receipts"][sid]
            item = {"submission_id": sid, "planned": True, "bases": entry.get("bases"), "like": entry.get("like"),
                    "reason": entry.get("reason"), "variants": entry["variants"]}
        else:
            plan = wa_repro.plan_variants(ws, args.title, row, like)
            item = {"submission_id": sid, "planned": False, "bases": plan["bases"], "reason": plan["reason"],
                    "variants": [v["id"] for v in plan["variants"] + plan["ties"]]}
        state = wa_repro.status(ws, args.title, sid, row.get("sha"))
        item.update(sha=str(row.get("sha") or "")[:7], status=row.get("status"), state=state["state"],
                    next_step=wa_repro.next_step(state), reproduced_run=state.get("reproduced_run"))
        out.append(item)
    payload = {"command": "wa-repro", "title": args.title, "apply": bool(args.apply), "receipts": out}
    print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    return 0
