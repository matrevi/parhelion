"""Bridge commands ``case-map`` and ``receipts`` (``opsb bench case-map`` / ``opsb bench receipts``)."""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

from . import case_map, receipt_backfill
from .parhelion_paths import load_problem_manifest
from .suite import is_worker


def add_parsers(sub, common) -> None:
    p_cm = sub.add_parser("case-map", parents=[common], help="Local official case <-> judge testcase map")
    p_cm.add_argument("--title", default=None, help="Operator problem title (default: every problem)")
    p_cm.add_argument("--rebuild", action="store_true", help="Rebuild and rewrite the stored map")
    p_rc = sub.add_parser("receipts", parents=[common], help="Normalize judge receipt rows")
    p_rc.add_argument("op", choices=["backfill"])
    p_rc.add_argument("--apply", action="store_true", help="Append the backfill rows (default: list only)")


def run(args: argparse.Namespace) -> int:
    ws = pathlib.Path(args.workspace).resolve()
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
