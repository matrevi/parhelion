"""Standalone observability entry for the B0 backfill lane.

    python -m scheduler.backfill_status --json
    python -m scheduler.backfill_status --json --offline        # no SSH probe (CI / replay)
    python -m scheduler.backfill_status --json --dispatch      # admit + run ONE session
    python -m scheduler.backfill_status --json --dispatch --force --title SyncBatchNormGatherStats

Contract (client / `OpsSupervisor.app` reads this; additive fields may appear):

    {
      "unpaired[]":  {title, bucket, stratum, dtype, baseline_sha, baseline_short,
                      modality, runtime_soc, headroom_pts, key},
      "queued[]":    {job_id, title, baseline_sha, buckets[], bucket_count, headroom_pts,
                      est_sec, attempts{}, waiting_sec, lane:"B0", tier:0},
      "running":     null | {job_id, title, buckets[], started_at, elapsed_sec, pid, lane, tier},
      "last_run":    null | {title, baseline_short, finished_at, result, duration_sec,
                             measured_count, log},
      "eta_sec":     float,     # sum of learned session estimates over `queued[]`
      "ledger_sha":  "<sha256 of the append-only ledger>"   # client cache key
    }

`--dispatch` is the *only* mutating path; it still refuses to take the die while any
operator ticket (`tier >= 1`) waits or any die lock is held, even with `--force`
(`--force` bypasses the auto-throttle only, never the operator priority).
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

from .backfill import LANE_BACKFILL, BackfillScheduler, offline_npu_sentinel

DEFAULT_WORKSPACE = pathlib.Path("/Users/winbeau/Competitions/ops-bench")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m scheduler.backfill_status",
        description="B0 backfill lane status: unpaired baseline buckets, queue, running, ledger",
    )
    p.add_argument("--workspace", type=pathlib.Path, default=DEFAULT_WORKSPACE, help="Contest workspace root")
    p.add_argument("--state-dir", type=pathlib.Path, default=None, help="Scheduler state directory override")
    p.add_argument("--npu-host", default="npu", help="SSH host alias for the Accel NPU server")
    p.add_argument("--json", action="store_true", help="Emit the machine-readable JSON contract")
    p.add_argument("--offline", action="store_true", help="Skip the live (read-only) NPU probe")
    p.add_argument("--title", default=None, help="Restrict queue/dispatch to a single operator")
    p.add_argument("--dispatch", action="store_true", help="Admit and run at most one B0 session")
    p.add_argument("--force", action="store_true", help="Bypass AUTO throttle (manual override; never preempts)")
    p.add_argument("--dry-run", action="store_true", help="With --dispatch: print the acquired plan only")
    p.add_argument("--no-hydrate", action="store_true", help="Skip the unpaired/queue scan (ledger view only)")
    return p


def build_scheduler(args: argparse.Namespace) -> BackfillScheduler:
    return BackfillScheduler(
        pathlib.Path(args.workspace),
        state_dir=pathlib.Path(args.state_dir) if args.state_dir else None,
        npu_host=args.npu_host,
    )


def _probe(sched: BackfillScheduler, offline: bool) -> dict[str, Any] | None:
    if offline:
        return offline_npu_sentinel()
    try:
        return sched.npu_scheduler.inspect_live_queue()
    except Exception as exc:  # telemetry is best-effort; the plan is still printable
        return offline_npu_sentinel(str(exc)[:160])


def _human(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    up = payload.get("unpaired") or []
    per: dict[str, list[str]] = {}
    for row in up:
        per.setdefault(str(row["title"]), []).append(str(row["bucket"]))
    lines.append(
        f"B0 backfill lane · unpaired {payload.get('unpaired_count', 0)} bucket(s) "
        f"across {len(per)} operator(s) · queued {payload.get('queued_count', 0)} session(s) "
        f"· eta {payload.get('eta_sec', 0)}s"
    )
    for title in sorted(per):
        lines.append(f"  {title:<26} {len(per[title]):2d} unpaired  e.g. {', '.join(per[title][:3])}")
    run = payload.get("running")
    lines.append(f"  running: {run.get('title') if run else '—'}")
    last = payload.get("last_run")
    lines.append(f"  last_run: {last.get('title') if last else '—'} {(last or {}).get('result') or ''}".rstrip())
    lines.append(f"  ledger_sha: {payload.get('ledger_sha')}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    sched = build_scheduler(args)

    if args.dispatch:
        npu = _probe(sched, args.offline)
        out = sched.dispatch(
            force=bool(args.force),
            dry_run=bool(args.dry_run),
            title=args.title,
            npu=npu,
        )
        payload = sched.status(hydrate=True, npu=npu)
        payload["dispatch"] = out
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(_human(payload))
            d = out
            print(
                f"  dispatch: dispatched={d.get('dispatched')} reason={d.get('reason')} "
                f"blocked_by={d.get('blocked_by')} result={d.get('result')}"
            )
        return 0 if not out.get("blocked_by") or args.dry_run else 0

    npu = _probe(sched, args.offline)
    payload = sched.status(hydrate=not args.no_hydrate, npu=npu)
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(_human(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
