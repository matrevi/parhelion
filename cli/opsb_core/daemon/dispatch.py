"""Submission dispatch decision of the daemon's queue consumer.

Pure logic over one queue snapshot. The daemon passes in the bridge call and the uploader, and
the only state is the caller's backoff map, so `hot_reload` can swap this module between
dispatch ticks without a restart (and without interrupting workers).
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable


def judge_preflight(workspace: Path, title: str, sha: str) -> dict[str, Any]:
    """Compile check with the judge toolchain before an upload (scheduler/judge_compile.py).

    Unconfigured is answered here without a subprocess; otherwise a short-lived bridge process
    decides (and starts the check in the background), so its logic is always the current file.
    A broken check never blocks an upload; only a recorded compile failure holds one.
    """
    from ..config import SUPERVISOR_ROOT, resolve_python_exe
    from ..device_env import device_text

    if not (device_text("judge_toolchain_root") or device_text("judge_toolchain_env")):
        return {"action": "upload", "status": "unconfigured"}
    cmd = [resolve_python_exe(workspace), "-m", "scheduler.judge_compile", "preflight",
           "--workspace", str(workspace), "--title", title, "--sha", sha]
    try:
        proc = subprocess.run(cmd, cwd=str(SUPERVISOR_ROOT), capture_output=True, text=True, timeout=20)
        lines = (proc.stdout or "").strip().splitlines()
        reply = json.loads(lines[-1]) if lines else {"error": (proc.stderr or "")[-300:]}
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        reply = {"error": str(exc)}
    if isinstance(reply, dict) and reply.get("action") in ("upload", "wait", "hold"):
        return reply
    print(f"judge compile preflight failed for {title} {sha[:7]}: {reply}", file=sys.stderr, flush=True)
    return {"action": "upload", "status": "error"}


def dispatch_next(runtime: Any, workspace: Path, snapshot: dict, backoff: dict, *,
                  bridge: Callable[..., Any], launch: Callable[..., Any], cli: Path,
                  preflight: Callable[..., dict[str, Any]] | None = None) -> None:
    """Promote one requested candidate or upload one approved pending item, then stop."""
    now = time.monotonic()
    queue = snapshot.get("submission_queue", [])
    for item in queue:
        if not item.get("gates", {}).get("all_pass"):
            continue
        promote = item.get("kind") == "candidate" and item.get("requested_submission")
        if item.get("kind") != "pending" and not promote:
            continue
        title, sha = item["title"], item["sha"]
        key = (workspace, title, sha)
        if backoff.get(key, 0) > now:
            continue
        if promote:
            args = ["--title", title, "--sha", sha, "--note", item.get("request_note") or ""]
            if item.get("agent_id"):
                args.extend(["--agent-id", item["agent_id"]])
            if item.get("speedup") is not None:
                args.extend(["--speedup", str(item["speedup"])])
            reply = bridge(workspace, "submit", args) or {}
            if reply.get("enqueued"):
                runtime.state_cache.invalidate(workspace)
                runtime.notify_subscribers()
                return
            backoff[key] = now + 5
            continue
        check = (preflight or judge_preflight)(workspace, title, sha)
        if check.get("action") == "wait":  # compile check with the judge toolchain still running
            backoff[key] = now + 10
            continue
        if check.get("action") == "hold":  # does not compile with the judge toolchain
            if backoff.get(("judge_compile_hold", key)) is None:
                runtime.state_cache.invalidate(workspace)  # let Stage 1 show the compile error
                runtime.notify_subscribers()
            backoff[key] = backoff[("judge_compile_hold", key)] = now + 300
            continue
        result = launch(cli, workspace, title, sha, item.get("note") or "", enforce_operator_mutex=True)
        if result.get("ok"):
            backoff[key] = now + 10
            runtime.state_cache.invalidate(workspace)
            runtime.notify_subscribers()
            return
        if result.get("operator_inflight") or result.get("duplicate_inflight") or result.get("duplicate_terminal"):
            backoff[key] = now + 5
            continue
        return
