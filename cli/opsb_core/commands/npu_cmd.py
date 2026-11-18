"""CLI commands: `opsb npu status | agent-install | test-ssh`."""
from __future__ import annotations

import argparse
import json
import sys

from ..config import ConfigRepository
from ..services.npu_service import deploy_probe, poll_npu_telemetry, test_ssh_connectivity
from ..services.queue_service import compute_workspace_snapshot


def run_npu(args: argparse.Namespace) -> int:
    cfg = ConfigRepository.load()
    ws = ConfigRepository.resolve_workspace(getattr(args, "workspace", None), cfg)
    host = getattr(args, "host", None) or cfg.get("npu_ssh_host", "npu")
    as_json = getattr(args, "json", False)

    if args.npu_cmd == "agent-install":
        res = deploy_probe(host)
        if as_json:
            print(json.dumps(res, ensure_ascii=False))
        elif res["ok"]:
            print(f"Deployed ~/.opsb/npu_probe.py to {host} (probe latency: {res['probe'].get('probe_latency_ms')} ms)")
        else:
            print(f"Failed to deploy probe to {host}: {res.get('error')}", file=sys.stderr)
        return 0 if res["ok"] else 1

    if args.npu_cmd == "test-ssh":
        res = test_ssh_connectivity(host)
        print(json.dumps(res, ensure_ascii=False) if as_json else f"{host}: {'OK' if res['ok'] else 'FAIL'} ({res['latency_ms']} ms) {res['info']}")
        return 0 if res["ok"] else 1

    snap = compute_workspace_snapshot(ws, cfg)
    probs_map = {p["title"]: p for p in snap["problems"]}
    data = poll_npu_telemetry(host, probs_map)
    if as_json:
        print(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
        return 0

    if not data.get("online"):
        print(f"NPU {host} OFFLINE: {data.get('error')}", file=sys.stderr)
        return 1
    dev = data["primary_npu"]
    print(f"Host: {host} ({data['hostname']}) · {dev['soc_name']} · SDK {data['sdk_version']} · RTT {data['rtt_ms']}ms (probe {data['probe_latency_ms']}ms)")
    print(f"Hardware: AICore {dev['aicore_pct']}% | HBM {dev['hbm_used_mib']}/{dev['hbm_total_mib']} MiB | {dev['power_w']}W | {dev['temp_c']}°C")
    for t in data.get("tasks", []):
        print(f"  [{t['stage']:<11}] PID={t['pid']:<7} {str(t.get('problem') or '—'):<24} {t.get('short_sha', '—')}  {t['elapsed_sec']}s  {t['detail']}")
    return 0
