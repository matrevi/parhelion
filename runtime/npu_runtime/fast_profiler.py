#!/usr/bin/env python3
"""Low-lock profiler launcher with Phase-C deferred export & parsing.

Inside Phase B (holding device0.lock):
  - Launches `profiler` in a detached session with minimal low-overhead flags
    (`--task-time=l0 --ai-core=off --aicpu=off <acl-flag>=off --runtime-api=off`
    by default, or `--task-time=l1 --ai-core=on --aic-metrics=PipeUtilization`
    when `--pmu` or `OPSB_PMU=1`).
  - When `OPSB_PHASE_C_QUEUE` is set, watches the profiler log until
    "Start analyzing data" appears (at which point the profiled NPU child has
    already exited and profiler has finished flushing Die 0 hardware buffers;
    only pure-CPU Python SQLite/CSV export remains).
  - Enqueues a `wait_profiler` task onto `OPSB_PHASE_C_QUEUE` and returns 0
    IMMEDIATELY so Phase B releases `device0.lock` 3.5-8s earlier.

When `OPSB_PHASE_C_QUEUE` is NOT set (manual CLI use):
  - Waits for `profiler` to finish, runs `--parse-cmd` inline, and removes `--out`.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import time


ANALYSIS_MARKERS = (
    "Start analyzing data",
    "Analysis data in",
    "Start exporting",
    "Profiling finished.",
    "Process profiling data complete.",
)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="Temporary profiler output directory")
    ap.add_argument("--app", required=True, help="Application command passed to profiler --application")
    ap.add_argument("--parse-cmd", default="", help="Shell command executed once profiler finishes exporting")
    ap.add_argument("--pmu", action="store_true", help="Enable PipeUtilization AI-Core PMU counters")
    ap.add_argument("--task-time", default="", help="Override --task-time (default l0, or l1 with --pmu)")
    ap.add_argument("--keep", action="store_true", help="Keep --out directory after parsing")
    # The profiler binary and its runtime-API tracing flag are deployment facts, read the
    # same way npu-test.py does: OPSB_DEVICE_* overrides, else neutral defaults.
    ap.add_argument("--profiler-bin", default=os.environ.get("OPSB_DEVICE_PROFILER_BIN") or "profiler",
                    help="Profiler executable (default $OPSB_DEVICE_PROFILER_BIN or 'profiler')")
    ap.add_argument("--acl-flag", default=os.environ.get("OPSB_DEVICE_PROFILER_ACL_FLAG") or "--acl",
                    help="Runtime-API tracing flag switched off (default $OPSB_DEVICE_PROFILER_ACL_FLAG or '--acl')")
    args = ap.parse_args(argv)

    out_dir = pathlib.Path(args.out)
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_file = out_dir.parent / (out_dir.name + ".profiler.log")
    try:
        log_file.unlink()
    except FileNotFoundError:
        pass

    want_pmu = bool(args.pmu or os.environ.get("OPSB_PMU") == "1")
    task_time = args.task_time or ("l1" if want_pmu else "l0")

    cmd = [
        args.profiler_bin,
        f"--output={out_dir}",
        f"--task-time={task_time}",
        f"--ai-core={'on' if want_pmu else 'off'}",
        "--aicpu=off",
        f"{args.acl_flag}=off",
        "--runtime-api=off",
        f"--application={args.app}",
    ]
    if want_pmu:
        cmd.insert(4, "--aic-mode=task-based")
        cmd.insert(5, "--aic-metrics=PipeUtilization")

    phase_c_q = os.environ.get("OPSB_PHASE_C_QUEUE", "").strip()

    with log_file.open("w", encoding="utf-8") as lfh:
        proc = subprocess.Popen(
            cmd,
            stdout=lfh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )

    if phase_c_q:
        # Wait ONLY until profiler has finished capturing + flushing Die 0 hardware buffers
        # (signaled by "Start analyzing data") or until profiler exits.
        deadline = time.monotonic() + 300.0
        saw_analysis = False
        while time.monotonic() < deadline:
            if log_file.is_file():
                try:
                    txt = log_file.read_text(encoding="utf-8", errors="replace")
                    if any(m in txt for m in ANALYSIS_MARKERS):
                        saw_analysis = True
                        break
                except OSError:
                    pass
            if proc.poll() is not None:
                break
            time.sleep(0.025)

        rec = {
            "kind": "wait_profiler",
            "pid": proc.pid,
            "prof_dir": str(out_dir),
            "log_file": str(log_file),
            "parse_cmd": args.parse_cmd,
            "cwd": os.getcwd(),
            "keep": bool(args.keep),
            "early_unlock": saw_analysis,
            "env": {
                k: v for k, v in os.environ.items()
                if k in (
                    os.environ.get("OPSB_EXTRA_OPS_ENV") or "EXTRA_OPS_PATH",
                    os.environ.get("OPSB_HOME_ENV") or "TOOLCHAIN_HOME",
                    "LD_LIBRARY_PATH",
                    "PYTHONPATH",
                )
            },
        }
        with open(phase_c_q, "a", encoding="utf-8") as qf:
            qf.write(json.dumps(rec) + "\n")
        return 0

    # Synchronous fallback when called outside opsb 3-phase runner
    rc = proc.wait()
    if args.parse_cmd:
        prc = subprocess.run(["bash", "-lc", args.parse_cmd], cwd=os.getcwd())
        if prc.returncode != 0 and rc == 0:
            rc = prc.returncode
    if not args.keep:
        shutil.rmtree(out_dir, ignore_errors=True)
        try:
            log_file.unlink()
        except OSError:
            pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
