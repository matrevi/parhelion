#!/usr/bin/env python3
"""Lightweight, strictly read-only NPU server telemetry & task queue probe.

Deployed to `~/.opsb/npu_probe.py` on the remote Accel NPU host.
IMPORTANT: Never acquires or touches any `.lock` file via flock/fcntl.
Uses read-only `/proc`, `lslocks`, `ps`, and `npu-smi info` in <0.4s.
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
import subprocess
import time
from typing import Any

KNOWN_PROBLEMS = [
    "ConcatD",
    "ConfusionTransposeD",
    "CTCLossV2",
    "LayerNormV3",
    "MultilabelMarginLoss",
    "PadV3Grad",
    "PReluGradUpdate",
    "QuantUpdateScatter",
    "StridedSliceGrad",
    "SyncBatchNormGatherStats",
]

VENDOR_TO_PROBLEM = {
    "concat_d": "ConcatD",
    "confusion_transpose": "ConfusionTransposeD",
    "ctc_loss_v2": "CTCLossV2",
    "layer_norm_v3": "LayerNormV3",
    "multilabel_margin_loss": "MultilabelMarginLoss",
    "pad_v3_grad": "PadV3Grad",
    "prelu_grad_update": "PReluGradUpdate",
    "quant_update_scatter": "QuantUpdateScatter",
    "strided_slice_grad": "StridedSliceGrad",
    "sync_batch_norm_gather_stats": "SyncBatchNormGatherStats",
}

SHA_RE = re.compile(r"\b([0-9a-f]{40})\b")
SHORT_SHA_RE = re.compile(r"\b([0-9a-f]{7,12})\b")


def read_cpu_and_load() -> dict[str, Any]:
    cores = os.cpu_count() or 1
    load_1, load_5, load_15 = 0.0, 0.0, 0.0
    running_procs, total_procs = 0, 0
    try:
        parts = pathlib.Path("/proc/loadavg").read_text().split()
        load_1, load_5, load_15 = float(parts[0]), float(parts[1]), float(parts[2])
        if "/" in parts[3]:
            r_s, t_s = parts[3].split("/", 1)
            running_procs, total_procs = int(r_s), int(t_s)
    except Exception:
        pass
    return {
        "cores": cores,
        "load_1m": round(load_1, 2),
        "load_5m": round(load_5, 2),
        "load_15m": round(load_15, 2),
        "running_threads": running_procs,
        "total_threads": total_procs,
    }


def read_memory() -> dict[str, Any]:
    info: dict[str, int] = {}
    try:
        for line in pathlib.Path("/proc/meminfo").read_text().splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            num = v.strip().split()[0]
            if num.isdigit():
                info[k.strip()] = int(num)  # KiB
    except Exception:
        pass
    total_kib = info.get("MemTotal", 0)
    avail_kib = info.get("MemAvailable", info.get("MemFree", 0))
    used_kib = max(0, total_kib - avail_kib)
    return {
        "total_mib": round(total_kib / 1024),
        "used_mib": round(used_kib / 1024),
        "available_mib": round(avail_kib / 1024),
        "total_gib": round(total_kib / (1024 * 1024), 1),
        "used_gib": round(used_kib / (1024 * 1024), 1),
    }


def read_sdk_version() -> str:
    candidates = [
        pathlib.Path("/opt/toolchain/latest/latest/aarch64-linux/toolkit_install.info"),
        pathlib.Path("/opt/driver//opt/toolchain/latest/aarch64-linux/toolkit_install.info"),
    ]
    for p in candidates:
        try:
            if p.is_file():
                for line in p.read_text().splitlines():
                    if line.startswith("version="):
                        return line.split("=", 1)[1].strip()
        except Exception:
            continue
    return "9.1.0"


def read_npu_smi() -> dict[str, Any]:
    env = os.environ.copy()
    lib_paths = [
        "/opt/driver/driver/lib64",
        "/opt/driver/driver/lib64/common",
        "/opt/driver/driver/lib64/driver",
        "/opt/toolchain/latest/latest/lib64",
    ]
    existing = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = ":".join(lib_paths + ([existing] if existing else []))

    result: dict[str, Any] = {
        "available": False,
        "smi_version": "",
        "devices": [],
        "npu_processes": [],
        "error": None,
    }
    try:
        proc = subprocess.run(
            ["npu-smi", "info"],
            env=env,
            capture_output=True,
            text=True,
            timeout=2.5,
        )
        out = proc.stdout
        if proc.returncode != 0:
            result["error"] = (proc.stderr or out).strip()[:240]
            return result
    except Exception as exc:
        result["error"] = str(exc)[:240]
        return result

    result["available"] = True
    lines = out.splitlines()
    for line in lines:
        m_ver = re.search(r"npu-smi\s+([\d.]+)", line)
        if m_ver:
            result["smi_version"] = m_ver.group(1)
            break

    # Parse two-line device blocks inside npu-smi info table:
    # Line 1: | 5     soc-a               | OK            | 88.6        36                0    / 0             |
    # Line 2: | 0                         | 0000:0A:00.0  | 0           0    / 0          3427 / 65536         |
    i = 0
    while i < len(lines) - 1:
        l1 = lines[i].strip()
        l2 = lines[i + 1].strip()
        if l1.startswith("|") and l2.startswith("|") and "/" in l1 and "/" in l2:
            cols1 = [c.strip() for c in l1.strip("|").split("|")]
            cols2 = [c.strip() for c in l2.strip("|").split("|")]
            if len(cols1) >= 3 and len(cols2) >= 3:
                p1 = cols1[0].split()
                health = cols1[1]
                m1 = cols1[2].split()
                p2 = cols2[0].split()
                bus_id = cols2[1]
                m2 = cols2[2].split()
                if len(p1) >= 2 and p1[0].isdigit() and health in ("OK", "Warning", "Error", "Alarm"):
                    npu_id = int(p1[0])
                    raw_name = p1[1]
                    soc_name = raw_name if raw_name.lower().startswith("accel") else f"Accel{raw_name}"
                    power_w = float(m1[0]) if m1 and re.match(r"^[\d.]+$", m1[0]) else None
                    temp_c = int(float(m1[1])) if len(m1) > 1 and re.match(r"^[\d.]+$", m1[1]) else None
                    chip_id = int(p2[0]) if p2 and p2[0].isdigit() else 0
                    aicore_pct = int(float(m2[0])) if m2 and re.match(r"^[\d.]+$", m2[0]) else 0
                    # Extract HBM used / total from end of cols2[2], e.g. "0 0 / 0 3427 / 65536"
                    slashes = re.findall(r"(\d+)\s*/\s*(\d+)", cols2[2])
                    hbm_used, hbm_total = (int(slashes[-1][0]), int(slashes[-1][1])) if slashes else (0, 65536)
                    result["devices"].append(
                        {
                            "npu_id": npu_id,
                            "chip_id": chip_id,
                            "soc_name": soc_name,
                            "health": health,
                            "bus_id": bus_id,
                            "power_w": power_w,
                            "temp_c": temp_c,
                            "aicore_pct": aicore_pct,
                            "hbm_used_mib": hbm_used,
                            "hbm_total_mib": hbm_total,
                        }
                    )
                    i += 2
                    continue
        i += 1

    # Parse active NPU chip processes table at bottom
    in_proc_table = False
    for line in lines:
        if "Process id" in line and "Process name" in line:
            in_proc_table = True
            continue
        if in_proc_table and line.strip().startswith("|"):
            if "No running processes" in line:
                break
            cols = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cols) >= 4 and cols[1].isdigit():
                result["npu_processes"].append(
                    {
                        "npu_chip": cols[0],
                        "pid": int(cols[1]),
                        "name": cols[2],
                        "memory_mib": int(cols[3]) if cols[3].isdigit() else 0,
                    }
                )
    return result


def read_processes() -> dict[int, dict[str, Any]]:
    procs: dict[int, dict[str, Any]] = {}
    try:
        out = subprocess.check_output(
            ["ps", "-eo", "pid,ppid,etimes,pcpu,rss,args", "--no-headers"],
            text=True,
            timeout=2.0,
        )
        for line in out.splitlines():
            parts = line.strip().split(None, 5)
            if len(parts) < 6 or not parts[0].isdigit():
                continue
            pid = int(parts[0])
            procs[pid] = {
                "pid": pid,
                "ppid": int(parts[1]) if parts[1].isdigit() else 0,
                "elapsed_sec": int(parts[2]) if parts[2].isdigit() else 0,
                "cpu_pct": float(parts[3]) if re.match(r"^[\d.]+$", parts[3]) else 0.0,
                "rss_kib": int(parts[4]) if parts[4].isdigit() else 0,
                "cmd": parts[5],
            }
    except Exception:
        pass
    return procs


def read_locks() -> list[dict[str, Any]]:
    locks: list[dict[str, Any]] = []
    try:
        out = subprocess.check_output(
            ["lslocks", "-n", "-o", "PID,COMMAND,TYPE,MODE,PATH"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
        )
        for line in out.splitlines():
            if "bench" not in line and "opsb" not in line:
                continue
            parts = line.strip().split(None, 4)
            if len(parts) >= 5 and parts[0].isdigit():
                locks.append(
                    {
                        "pid": int(parts[0]),
                        "command": parts[1],
                        "type": parts[2],
                        "mode": parts[3],
                        "path": parts[4],
                    }
                )
    except Exception:
        pass
    return locks


def get_descendants(root_pid: int, procs: dict[int, dict[str, Any]]) -> list[int]:
    children_map: dict[int, list[int]] = {}
    for pid, p in procs.items():
        children_map.setdefault(p["ppid"], []).append(pid)
    result: list[int] = []
    stack = [root_pid]
    seen = set()
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        result.append(cur)
        stack.extend(children_map.get(cur, []))
    return result


def infer_problem_and_sha(pids: list[int], procs: dict[int, dict[str, Any]]) -> tuple[str | None, str | None]:
    problem: str | None = None
    sha: str | None = None
    for pid in pids:
        cmd = procs.get(pid, {}).get("cmd", "")
        if not problem:
            for p_name in KNOWN_PROBLEMS:
                if p_name in cmd or p_name.lower() in cmd.lower():
                    problem = p_name
                    break
        if not problem:
            for vendor_key, p_name in VENDOR_TO_PROBLEM.items():
                if vendor_key in cmd.lower():
                    problem = p_name
                    break
        if not sha:
            m40 = SHA_RE.search(cmd)
            if m40:
                sha = m40.group(1)
            else:
                # Look for results/<Problem>/<sha> or --out .../<sha>/ or explicit 7-char sha token
                m_res = re.search(r"bench-results/[^/]+/([0-9a-f]{7,40})", cmd)
                if m_res:
                    sha = m_res.group(1)
        # Also inspect /proc/<pid>/cwd safely
        if not problem or not sha:
            try:
                cwd = os.readlink(f"/proc/{pid}/cwd")
                if not problem:
                    for p_name in KNOWN_PROBLEMS:
                        if f"/{p_name}" in cwd:
                            problem = p_name
                            break
                if problem and not sha:
                    head_file = pathlib.Path(f"/srv/work/{problem}/.git/HEAD")
                    if head_file.is_file():
                        htxt = head_file.read_text().strip()
                        if re.match(r"^[0-9a-f]{40}$", htxt):
                            sha = htxt
            except Exception:
                pass
    return problem, sha


def classify_stage(pids: list[int], procs: dict[int, dict[str, Any]], holds_die: bool) -> tuple[str, str]:
    if not holds_die:
        return "WAITING-DIE", "Waiting for exclusive NPU die lock"
    joined = "\n".join(procs.get(pid, {}).get("cmd", "") for pid in pids)
    low = joined.lower()
    if "profiler" in low or "kperf.py" in low:
        # Extract current worker/case if visible
        m_case = re.search(r"--(?:ref-)?worker\s+(\S+)", joined)
        detail = f"profiler ({m_case.group(1)})" if m_case else "Hardware kernel profiling (profiler)"
        return "PROFILER", detail
    if any(k in low for k in ("build.sh", "cmake", "/ccec", "compiler", "kernelc", "ninja", "g++")):
        return "BUILD", "Compiling KernelC host/kernel package"
    if "--fast" in low or "fast-lane" in low or "fast_lane" in low:
        return "FAST-LANE", "Fast-lane smoke & публичные cases"
    if "stress" in low or "determinism" in low:
        return "STRESS", "Multi-run determinism & stress gate"
    if "extended.py" in low or "verify" in low or "run-npu.sh" in low or "pytest" in low:
        return "PRECISION", "Extended precision & boundary suite"
    return "PRECISION", "Active NPU validation task"


def build_snapshot() -> dict[str, Any]:
    t0 = time.monotonic()
    cpu = read_cpu_and_load()
    mem = read_memory()
    sdk_ver = read_sdk_version()
    npu = read_npu_smi()
    procs = read_processes()
    locks = read_locks()

    die_holders: dict[int, int] = {}  # die_idx -> root lock holder pid
    waiting_roots: set[int] = set()

    for lk in locks:
        path = lk["path"]
        mode = lk["mode"].upper()
        pid = lk["pid"]
        m_die = re.search(r"device(\d+)\.lock$", path)
        if m_die:
            die_idx = int(m_die.group(1))
            if "WRITE" in mode:
                die_holders[die_idx] = pid
            else:
                waiting_roots.add(pid)
        elif path.endswith("validation.lock"):
            waiting_roots.add(pid)

    # Also detect `flock -w ... device0.lock` processes that might be waiting in flock syscall
    for pid, p in procs.items():
        cmd = p["cmd"]
        if "bench-device" in cmd and "flock" in cmd:
            if pid not in die_holders.values():
                waiting_roots.add(pid)

    # Remove any root pid from waiting_roots if it (or an ancestor/descendant) already holds a die lock
    holder_trees: dict[int, list[int]] = {}
    all_holder_pids: set[int] = set()
    for die_idx, h_pid in die_holders.items():
        tree = get_descendants(h_pid, procs)
        holder_trees[die_idx] = tree
        all_holder_pids.update(tree)
        ppid = procs.get(h_pid, {}).get("ppid", 0)
        if ppid:
            all_holder_pids.add(ppid)

    waiting_roots = {pid for pid in waiting_roots if pid not in all_holder_pids}

    tasks: list[dict[str, Any]] = []
    die_locks_info: list[dict[str, Any]] = []

    # Check die 0 (and die 1 if present)
    max_dies = max(1, len(npu.get("devices", [])))
    for die_idx in range(max_dies):
        h_pid = die_holders.get(die_idx)
        if h_pid is not None and h_pid in procs:
            tree = holder_trees.get(die_idx, [h_pid])
            prob, sha = infer_problem_and_sha(tree, procs)
            stage, detail = classify_stage(tree, procs, holds_die=True)
            elapsed = procs[h_pid]["elapsed_sec"]
            die_locks_info.append(
                {
                    "die": die_idx,
                    "lock_path": f"/srv/work-device{die_idx}.lock",
                    "state": "HELD",
                    "holder_pid": h_pid,
                    "problem": prob or "Unknown",
                    "sha": sha,
                    "stage": stage,
                    "elapsed_sec": elapsed,
                }
            )
            tasks.append(
                {
                    "pid": h_pid,
                    "die": die_idx,
                    "state": "ACTIVE",
                    "stage": stage,
                    "problem": prob,
                    "sha": sha,
                    "elapsed_sec": elapsed,
                    "detail": detail,
                    "subprocesses": len(tree),
                }
            )
        else:
            die_locks_info.append(
                {
                    "die": die_idx,
                    "lock_path": f"/srv/work-device{die_idx}.lock",
                    "state": "FREE",
                    "holder_pid": None,
                    "problem": None,
                    "sha": None,
                    "stage": None,
                    "elapsed_sec": 0,
                }
            )

    # Add waiting tasks sorted by longest wait first (FIFO/aging visibility)
    for w_pid in sorted(waiting_roots, key=lambda x: procs.get(x, {}).get("elapsed_sec", 0), reverse=True):
        if w_pid not in procs:
            continue
        tree = get_descendants(w_pid, procs)
        prob, sha = infer_problem_and_sha(tree, procs)
        stage, detail = classify_stage(tree, procs, holds_die=False)
        tasks.append(
            {
                "pid": w_pid,
                "die": None,
                "state": "WAITING",
                "stage": stage,
                "problem": prob,
                "sha": sha,
                "elapsed_sec": procs[w_pid]["elapsed_sec"],
                "detail": detail,
                "subprocesses": len(tree),
            }
        )

    probe_ms = round((time.monotonic() - t0) * 1000, 1)
    primary_dev = npu["devices"][0] if npu["devices"] else {
        "npu_id": 0,
        "chip_id": 0,
        "soc_name": "soc-a",
        "health": "Unknown",
        "bus_id": "",
        "power_w": None,
        "temp_c": None,
        "aicore_pct": 0,
        "hbm_used_mib": 0,
        "hbm_total_mib": 65536,
    }

    return {
        "sampled_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "probe_latency_ms": probe_ms,
        "hostname": os.uname().nodename,
        "arch": os.uname().machine,
        "sdk_version": sdk_ver,
        "smi_version": npu.get("smi_version", ""),
        "primary_npu": primary_dev,
        "npu_devices": npu.get("devices", []),
        "npu_processes": npu.get("npu_processes", []),
        "cpu": cpu,
        "memory": mem,
        "die_locks": die_locks_info,
        "tasks": tasks,
    }


if __name__ == "__main__":
    print(json.dumps(build_snapshot(), ensure_ascii=False))
