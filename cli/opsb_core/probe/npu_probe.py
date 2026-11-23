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


def _parse_proc_stat() -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    try:
        for line in pathlib.Path("/proc/stat").read_text().splitlines():
            if not line.startswith("cpu"):
                continue
            parts = line.split()
            name = parts[0]
            nums = [int(x) for x in parts[1:] if x.isdigit()]
            if len(nums) >= 4:
                idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
                total = sum(nums)
                out[name] = (idle, total)
    except Exception:
        pass
    return out


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

    # Compute overall & per-core % from /proc/stat delta
    state_dir = pathlib.Path.home() / ".opsb"
    prev_file = state_dir / "cpu_stat_prev.json"
    cur_stat = _parse_proc_stat()
    prev_stat: dict[str, list[int]] = {}
    if prev_file.is_file():
        try:
            obj = json.loads(prev_file.read_text())
            if time.time() - float(obj.get("ts", 0)) < 120:
                prev_stat = obj.get("stat") or {}
        except Exception:
            pass
    if not prev_stat:
        time.sleep(0.05)
        prev_stat = {k: [v[0], v[1]] for k, v in cur_stat.items()}
        cur_stat = _parse_proc_stat()

    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        prev_file.write_text(json.dumps({"ts": time.time(), "stat": {k: [v[0], v[1]] for k, v in cur_stat.items()}}))
    except Exception:
        pass

    def calc_pct(key: str) -> float:
        if key not in cur_stat or key not in prev_stat:
            return 0.0
        c_idle, c_tot = cur_stat[key]
        p_idle, p_tot = prev_stat[key][0], prev_stat[key][1]
        dt = c_tot - p_tot
        if dt <= 0:
            return 0.0
        return round(max(0.0, min(100.0, (1.0 - (c_idle - p_idle) / dt) * 100.0)), 1)

    usage_pct = calc_pct("cpu")
    per_core = [calc_pct(f"cpu{i}") for i in range(cores)]

    return {
        "cores": cores,
        "load_1m": round(load_1, 2),
        "load_5m": round(load_5, 2),
        "load_15m": round(load_15, 2),
        "running_threads": running_procs,
        "total_threads": total_procs,
        "usage_pct": usage_pct,
        "per_core_pct": per_core,
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
    cached_kib = info.get("Buffers", 0) + info.get("Cached", 0) + info.get("SReclaimable", 0)
    used_kib = max(0, total_kib - avail_kib)
    return {
        "total_mib": round(total_kib / 1024),
        "used_mib": round(used_kib / 1024),
        "available_mib": round(avail_kib / 1024),
        "total_gib": round(total_kib / (1024 * 1024), 1),
        "used_gib": round(used_kib / (1024 * 1024), 1),
        "cached_gib": round(cached_kib / (1024 * 1024), 1),
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
    id_cache = pathlib.Path.home() / ".opsb" / "npu_id_hint"
    hint_id = id_cache.read_text().strip() if id_cache.is_file() else "5"
    try:
        p_info = subprocess.Popen(["npu-smi", "info"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        p_usage = subprocess.Popen(
            ["npu-smi", "info", "-t", "usages", "-i", hint_id],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        out, err = p_info.communicate(timeout=2.5)
        u_out, _ = p_usage.communicate(timeout=1.8)
        u_rc = p_usage.returncode
        if p_info.returncode != 0:
            result["error"] = (err or out).strip()[:240]
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
                            "aivector_pct": 0,
                            "hbm_bw_pct": 0,
                            "ctrlcpu_pct": 0,
                            "hbm_used_mib": hbm_used,
                            "hbm_total_mib": hbm_total,
                        }
                    )
                    i += 2
                    continue
        i += 1

    # Enrich each NPU device with `npu-smi info -t usages -i <npu_id>` (overlapped when hint_id matches)
    for dev in result["devices"]:
        try:
            dev_id_str = str(dev["npu_id"])
            if dev_id_str != hint_id:
                id_cache.parent.mkdir(parents=True, exist_ok=True)
                id_cache.write_text(dev_id_str)
                u_proc = subprocess.run(
                    ["npu-smi", "info", "-t", "usages", "-i", dev_id_str],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=1.5,
                )
                cur_u_out = u_proc.stdout if u_proc.returncode == 0 else ""
            else:
                if not id_cache.is_file():
                    id_cache.parent.mkdir(parents=True, exist_ok=True)
                    id_cache.write_text(dev_id_str)
                cur_u_out = u_out if u_rc == 0 else ""
            for uline in cur_u_out.splitlines():
                if ":" not in uline:
                    continue
                k, v = [s.strip() for s in uline.split(":", 1)]
                if not v.isdigit():
                    continue
                ival = int(v)
                kl = k.lower()
                if "aivector usage rate" in kl:
                    dev["aivector_pct"] = ival
                elif "aicore usage rate" in kl:
                    dev["aicore_pct"] = max(dev["aicore_pct"], ival)
                elif "hbm bandwidth usage rate" in kl:
                    dev["hbm_bw_pct"] = ival
                elif "ctrlcpu usage rate" in kl:
                    dev["ctrlcpu_pct"] = ival
        except Exception:
            pass

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


def _is_driver_wrapper(cmd: str) -> bool:
    """Return True if cmd is an inline `python3 -c` driver script (npu-test.py or backfill.py).
    Scanning its literal source text would match `CONCATD_FAST`, `profiler_launches`, `device0.lock`, etc.
    """
    return "python3 -c" in cmd and (
        "bench-repo-" in cmd
        or "PHASE A:" in cmd
        or "CONCATD_FAST" in cmd
        or "BACKFILL_RESULT " in cmd
        or ".opsb_backfill_build.json" in cmd
    )


def infer_problem_and_sha(pids: list[int], procs: dict[int, dict[str, Any]]) -> tuple[str | None, str | None]:
    problem: str | None = None
    sha: str | None = None
    for pid in pids:
        raw_cmd = procs.get(pid, {}).get("cmd", "")
        cmd = "" if _is_driver_wrapper(raw_cmd) else raw_cmd
        if cmd and not problem:
            for p_name in KNOWN_PROBLEMS:
                if p_name in cmd or p_name.lower() in cmd.lower():
                    problem = p_name
                    break
        if cmd and not problem:
            for vendor_key, p_name in VENDOR_TO_PROBLEM.items():
                if vendor_key in cmd.lower():
                    problem = p_name
                    break
        if cmd and not sha:
            m40 = SHA_RE.search(cmd)
            if m40:
                sha = m40.group(1)
            else:
                # Look for results/<Problem>/<sha> or --out .../<sha>/ or explicit 7-char sha token
                m_res = re.search(r"bench-results/[^/]+/([0-9a-f]{7,40})", cmd)
                if m_res:
                    sha = m_res.group(1)
        # Also inspect /proc/<pid>/cwd safely (skip driver wrapper whose cwd is home dir)
        if not _is_driver_wrapper(raw_cmd) and (not problem or not sha):
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


def classify_stage(
    pids: list[int],
    procs: dict[int, dict[str, Any]],
    holds_die: bool,
    substage_hint: str | None = None,
) -> tuple[str, str]:
    if not holds_die:
        return "WAITING-DIE", "Waiting for exclusive NPU die lock"
    non_driver = [
        procs.get(pid, {}).get("cmd", "")
        for pid in pids
        if not _is_driver_wrapper(procs.get(pid, {}).get("cmd", ""))
    ]
    joined = "\n".join(non_driver)
    low = joined.lower()
    if "profiler" in low or "kperf.py" in low or substage_hint == "profiler":
        m_case = re.search(r"--(?:ref-)?worker\s+(\S+)", joined)
        detail = f"profiler ({m_case.group(1)})" if m_case else "Hardware kernel profiling (profiler)"
        return "PROFILER", detail
    if any(k in low for k in ("build.sh", "cmake", "/ccec", "compiler", "kernelc", "ninja", "g++")):
        return "BUILD", "Compiling KernelC host/kernel package"
    if "--fast" in low or "fast-lane" in low or "fast_lane" in low:
        return "FAST-LANE", "Fast-lane smoke & public cases"
    if "stress" in low or "determinism" in low:
        return "STRESS", "Multi-run determinism & stress gate"
    if "extended.py" in low or "verify" in low or "run-npu.sh" in low or "pytest" in low:
        return "PRECISION", "Extended precision & boundary suite"
    return "PRECISION", "Active NPU validation task"


def build_snapshot() -> dict[str, Any]:
    t0 = time.monotonic()
    now_epoch = time.time()
    opsb_home = pathlib.Path.home() / ".opsb"
    cpu = read_cpu_and_load()
    mem = read_memory()
    sdk_ver = read_sdk_version()
    npu = read_npu_smi()
    procs = read_processes()
    locks = read_locks()

    # Load authoritative Phase-A CPU build heartbeats & Phase-B die session upfront
    active_cpu_build_pids: set[int] = set()
    cpu_builds_by_pid: dict[int, dict[str, Any]] = {}
    cpu_builds_dir = opsb_home / "cpu_builds"
    if cpu_builds_dir.is_dir():
        for bf in sorted(cpu_builds_dir.glob("*.json")):
            try:
                bd = json.loads(bf.read_text())
                b_pid = int(bd.get("pid") or 0)
                if bd.get("finished_at") or (b_pid and b_pid not in procs):
                    continue
                if b_pid:
                    active_cpu_build_pids.add(b_pid)
                    cpu_builds_by_pid[b_pid] = bd
            except Exception:
                continue

    die_session: dict[str, Any] = {}
    sess_file = opsb_home / "die_session.json"
    if sess_file.is_file():
        try:
            die_session = json.loads(sess_file.read_text()) or {}
        except Exception:
            die_session = {}

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
                # Prefer earliest ancestor holding the lock if child inherited fd
                prev = die_holders.get(die_idx)
                if prev is None or procs.get(pid, {}).get("elapsed_sec", 0) > procs.get(prev, {}).get("elapsed_sec", 0):
                    die_holders[die_idx] = pid
            else:
                waiting_roots.add(pid)
        elif path.endswith("validation.lock"):
            waiting_roots.add(pid)

    # Also detect `flock -w ... device0.lock` shell processes (never match inline python3 -c driver text)
    for pid, p in procs.items():
        cmd = p["cmd"]
        if _is_driver_wrapper(cmd):
            continue
        if "bench-device" in cmd and "flock" in cmd:
            if pid not in die_holders.values():
                waiting_roots.add(pid)

    # Remove any root pid from waiting_roots if it (or an ancestor/descendant) already holds a die lock or is still in Phase A CPU build
    holder_trees: dict[int, list[int]] = {}
    all_holder_pids: set[int] = set()
    for die_idx, h_pid in die_holders.items():
        tree = get_descendants(h_pid, procs)
        holder_trees[die_idx] = tree
        all_holder_pids.update(tree)
        ppid = procs.get(h_pid, {}).get("ppid", 0)
        if ppid:
            all_holder_pids.add(ppid)

    waiting_roots = {
        pid for pid in waiting_roots
        if pid not in all_holder_pids and pid not in active_cpu_build_pids
    }

    tasks: list[dict[str, Any]] = []
    die_locks_info: list[dict[str, Any]] = []

    # Check die 0 (and die 1 if present)
    max_dies = max(1, len(npu.get("devices", [])))
    for die_idx in range(max_dies):
        h_pid = die_holders.get(die_idx)
        if h_pid is not None and h_pid in procs:
            tree = holder_trees.get(die_idx, [h_pid])
            prob, sha = infer_problem_and_sha(tree, procs)
            sess_matches = bool(
                die_session
                and int(die_session.get("die", 0)) == die_idx
                and (int(die_session.get("pid") or 0) in tree or int(die_session.get("pid") or 0) == h_pid)
            )
            substage_hint = str(die_session.get("substage") or "") if sess_matches else None
            sess_mode = str(die_session.get("mode") or "").upper() if sess_matches else ""
            if sess_matches:
                prob = die_session.get("title") or prob
                sha = die_session.get("sha") or sha
            is_backfill = (
                sess_mode == "B0"
                or substage_hint == "backfill_perf"
                or any("/.backfill/" in procs.get(cpid, {}).get("cmd", "") for cpid in tree)
            )
            if is_backfill:
                stage, detail = "BACKFILL", "Baseline perf-bucket backfill (B0)"
            else:
                stage, detail = classify_stage(tree, procs, holds_die=True, substage_hint=substage_hint)
            # Measure ONLY actual Die 0 hold duration (from die_session.started_epoch), NEVER Phase A + queue wait!
            if sess_matches and float(die_session.get("started_epoch") or 0) > 0:
                elapsed = int(max(0, round(now_epoch - float(die_session["started_epoch"]))))
            else:
                # Fallback to child bash/python elapsed rather than Phase-A root wrapper
                child_el = [
                    procs[cpid]["elapsed_sec"]
                    for cpid in tree
                    if cpid != h_pid and cpid in procs and not _is_driver_wrapper(procs[cpid].get("cmd", ""))
                ]
                elapsed = max(child_el) if child_el else procs[h_pid]["elapsed_sec"]
            lock_entry: dict[str, Any] = {
                "die": die_idx,
                "lock_path": f"/srv/work-device{die_idx}.lock",
                "state": "HELD",
                "holder_pid": h_pid,
                "problem": prob or "Unknown",
                "sha": sha,
                "stage": stage,
                "elapsed_sec": elapsed,
            }
            if substage_hint:
                lock_entry["substage"] = substage_hint
            die_locks_info.append(lock_entry)
            tasks.append(
                {
                    "pid": h_pid,
                    "die": die_idx,
                    "state": "ACTIVE",
                    "stage": stage,
                    "lane": "B0" if is_backfill else (sess_mode or "Q1"),
                    "is_backfill": is_backfill,
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

    # Read cooperative WSPT tickets `/tmp/opsb-npu-tickets/*.json` for priority & boost state
    tickets_by_pid: dict[int, dict[str, Any]] = {}
    ticket_dir = pathlib.Path("/tmp/opsb-npu-tickets")
    if ticket_dir.is_dir():
        for tf in ticket_dir.glob("*.json"):
            try:
                td = json.loads(tf.read_text())
                tpid = int(td.get("pid") or 0)
                if tpid in procs and tpid not in active_cpu_build_pids:
                    tickets_by_pid[tpid] = td
                    if tpid not in all_holder_pids:
                        waiting_roots.add(tpid)
            except Exception:
                continue

    # Add waiting tasks sorted by WSPT ticket priority (tier, prio, wait_sec)
    waiting_tasks_list: list[dict[str, Any]] = []
    sorted_waiters = sorted(
        waiting_roots,
        key=lambda x: (
            int(tickets_by_pid.get(x, {}).get("tier") or 1),
            float(tickets_by_pid.get(x, {}).get("prio") or 0.0),
            -float(tickets_by_pid.get(x, {}).get("created") or now_epoch),
        ),
        reverse=True,
    )
    for w_pid in sorted_waiters:
        if w_pid not in procs or w_pid in active_cpu_build_pids:
            continue
        tree = get_descendants(w_pid, procs)
        tk = tickets_by_pid.get(w_pid, {})
        prob, sha = infer_problem_and_sha(tree, procs)
        # Authoritative ticket title/sha always takes precedence
        prob = tk.get("title") or prob
        sha = tk.get("sha") or sha
        if not prob:
            # Skip orphan wrapper pids without a ticket yet (e.g. mid-git-fetch before cpu_hb)
            continue
        is_backfill = (
            str(tk.get("lane") or "").upper() == "B0"
            or str(tk.get("kind") or "").upper() == "BACKFILL"
            or ("tier" in tk and int(tk.get("tier") or 0) == 0)
            or any("/.backfill/" in procs.get(cpid, {}).get("cmd", "") for cpid in tree)
        )
        if is_backfill:
            stage, detail = "BACKFILL-WAIT", "Waiting idle Die 0 for perf-bucket backfill (B0 · tier 0)"
            is_boosted = False
            lane_str = "B0"
        else:
            stage, detail = classify_stage(tree, procs, holds_die=False)
            is_boosted = int(tk.get("tier") or 0) >= 50
            if is_boosted:
                detail = "Priority boosted by Parhelion"
            lane_str = "Q0" if tk.get("fast") else "Q1"
        tk_created = float(tk.get("created") or 0)
        wait_elapsed = int(max(0, round(now_epoch - tk_created))) if tk_created > 0 else procs[w_pid]["elapsed_sec"]
        wt_item = {
            "pid": w_pid,
            "die": None,
            "state": "WAITING",
            "stage": stage,
            "lane": lane_str,
            "is_backfill": is_backfill,
            "problem": prob,
            "sha": sha,
            "elapsed_sec": wait_elapsed,
            "detail": detail,
            "boosted": is_boosted,
            "subprocesses": len(tree),
        }
        waiting_tasks_list.append(wt_item)
        tasks.append(wt_item)

    # Detect lock-free 32-core CPU builds (`~/.opsb/cpu_builds/*.json` + compiler procs outside die lock)
    parallel_cpu_builds: list[dict[str, Any]] = []
    seen_build_keys: set[str] = set()
    for b_pid, bd in cpu_builds_by_pid.items():
        b_title = bd.get("title") or bd.get("problem") or "Operator"
        b_sha = str(bd.get("sha") or "")
        st_ep = float(bd.get("started_epoch") or 0)
        el_s = int(now_epoch - st_ep) if st_ep > 0 else procs.get(b_pid, {}).get("elapsed_sec", 0)
        b_is_bf = str(bd.get("lane") or "").upper() == "B0" or str(bd.get("kind") or "").upper() == "BACKFILL"
        b_key = f"{b_title}:{b_sha[:7]}:{'B0' if b_is_bf else 'OP'}"
        if b_key not in seen_build_keys:
            seen_build_keys.add(b_key)
            parallel_cpu_builds.append(
                {
                    "pid": b_pid,
                    "title": b_title,
                    "sha": b_sha,
                    "short_sha": b_sha[:7] or "—",
                    "elapsed_sec": max(0, el_s),
                    "lane": "B0" if b_is_bf else "Q1",
                    "is_backfill": b_is_bf,
                }
            )

    for pid, p in procs.items():
        if pid in all_holder_pids or pid in waiting_roots or _is_driver_wrapper(p["cmd"]):
            continue
        cmd_l = p["cmd"].lower()
        if any(k in cmd_l for k in ("build.sh", "/ccec", "compiler")) and "grep" not in cmd_l:
            prob, sha = infer_problem_and_sha([pid], procs)
            b_is_bf = "/.backfill/" in p["cmd"]
            if not b_is_bf:
                try:
                    if "/.backfill/" in os.readlink(f"/proc/{pid}/cwd"):
                        b_is_bf = True
                except Exception:
                    pass
            if prob:
                b_key = f"{prob}:{(sha or '')[:7]}:{'B0' if b_is_bf else 'OP'}"
                if b_key not in seen_build_keys:
                    seen_build_keys.add(b_key)
                    parallel_cpu_builds.append(
                        {
                            "pid": pid,
                            "title": prob,
                            "sha": sha or "",
                            "short_sha": (sha or "")[:7] or "—",
                            "elapsed_sec": p["elapsed_sec"],
                            "lane": "B0" if b_is_bf else "Q1",
                            "is_backfill": b_is_bf,
                        }
                    )

    for cb in parallel_cpu_builds:
        cb_bf = bool(cb.get("is_backfill"))
        tasks.append(
            {
                "pid": cb["pid"],
                "die": None,
                "state": "CPU_BUILD",
                "stage": "BACKFILL-BUILD" if cb_bf else "CPU-BUILD",
                "lane": "B0" if cb_bf else "Q1",
                "is_backfill": cb_bf,
                "problem": cb["title"],
                "sha": cb["sha"] or None,
                "elapsed_sec": cb["elapsed_sec"],
                "detail": (
                    "Phase A baseline worktree build for perf-bucket backfill (B0)"
                    if cb_bf
                    else "Phase A lock-free 32-core CPU build (0s NPU lock)"
                ),
                "subprocesses": 4,
            }
        )

    primary_dev = npu["devices"][0] if npu["devices"] else {
        "npu_id": 0,
        "chip_id": 0,
        "soc_name": "soc-a",
        "health": "Unknown",
        "bus_id": "",
        "power_w": None,
        "temp_c": None,
        "aicore_pct": 0,
        "aivector_pct": 0,
        "hbm_bw_pct": 0,
        "ctrlcpu_pct": 0,
        "hbm_used_mib": 0,
        "hbm_total_mib": 65536,
    }

    now_ts = round(time.time(), 1)
    hbm_pct = round((primary_dev.get("hbm_used_mib", 0) / max(1, primary_dev.get("hbm_total_mib", 65536))) * 100.0, 2)
    ram_pct = round((mem.get("used_mib", 0) / max(1, mem.get("total_mib", 1))) * 100.0, 2)
    sample_pt = {
        "ts": now_ts,
        "aicore_pct": float(primary_dev.get("aicore_pct") or 0.0),
        "aivector_pct": float(primary_dev.get("aivector_pct") or 0.0),
        "hbm_bw_pct": float(primary_dev.get("hbm_bw_pct") or 0.0),
        "power_w": float(primary_dev.get("power_w") or 0.0),
        "temp_c": int(primary_dev.get("temp_c") or 0),
        "hbm_pct": hbm_pct,
        "cpu_pct": float(cpu.get("usage_pct") or 0.0),
        "ram_pct": ram_pct,
        "die_held": 1 if (die_locks_info and die_locks_info[0]["state"] == "HELD") else 0,
        "die_stage": die_locks_info[0].get("stage") if die_locks_info else None,
    }

    ring_file = opsb_home / "telemetry_ring.json"
    history: list[dict[str, Any]] = []
    try:
        if ring_file.is_file():
            loaded = json.loads(ring_file.read_text())
            if isinstance(loaded, list):
                history = [p for p in loaded if now_ts - float(p.get("ts", 0)) <= 1800]
        if not history or now_ts - float(history[-1].get("ts", 0)) >= 1.5:
            history.append(sample_pt)
        history = history[-90:]
        ring_file.parent.mkdir(parents=True, exist_ok=True)
        ring_file.write_text(json.dumps(history, separators=(",", ":")))
    except Exception:
        history = [sample_pt]

    # Compute 10m Die Duty (`~/.opsb/die_ledger.jsonl` primary, live ring fallback)
    window_sec = 600.0
    ledger_file = opsb_home / "die_ledger.jsonl"
    ledger_rows: list[dict[str, Any]] = []
    if ledger_file.is_file():
        try:
            for line in ledger_file.read_text().splitlines()[-120:]:
                if not line.strip():
                    continue
                row = json.loads(line)
                end_ep = float(row.get("ended_epoch") or row.get("ts") or 0)
                if end_ep > 0 and (now_ts - end_ep) <= window_sec:
                    ledger_rows.append(row)
        except Exception:
            ledger_rows = []

    if ledger_rows:
        npu_s = sum(float(r.get("npu_exec_sec") or 0.0) for r in ledger_rows)
        acl_s = sum(float(r.get("acl_host_sec") or 0.0) for r in ledger_rows)
        waste_s = sum(float(r.get("lock_waste_sec") or 0.0) for r in ledger_rows)
        tot_hold = sum(float(r.get("hold_sec") or (npu_s + acl_s + waste_s)) for r in ledger_rows)
        denom = max(window_sec, tot_hold, 1.0)
        free_s = max(0.0, denom - (npu_s + acl_s + waste_s))
        die_duty_10m = {
            "npu_exec_pct": round(100.0 * npu_s / denom, 2),
            "acl_host_pct": round(100.0 * acl_s / denom, 2),
            "lock_waste_pct": round(100.0 * waste_s / denom, 2),
            "free_pct": round(100.0 * free_s / denom, 2),
        }
        avg_hold_sec = round(tot_hold / len(ledger_rows), 1)
        slots_per_hr = round(min(240.0, 3600.0 / max(8.0, avg_hold_sec)), 1)
    else:
        win_pts = [p for p in history if (now_ts - float(p.get("ts", 0))) <= window_sec] or [sample_pt]
        n_pts = max(1, len(win_pts))
        held_pts = [p for p in win_pts if p.get("die_held", (1 if die_locks_info and die_locks_info[0]["state"] == "HELD" else 0))]
        held_frac = len(held_pts) / n_pts
        build_in_lock = sum(1 for p in held_pts if p.get("die_stage") == "BUILD") / n_pts
        mean_vec = sum(max(float(p.get("aivector_pct") or 0.0), float(p.get("aicore_pct") or 0.0)) for p in win_pts) / n_pts
        npu_exec_p = round(min(held_frac * 100.0, max(mean_vec, 0.4 if held_frac > 0 else 0.0)), 2)
        waste_p = round(build_in_lock * 100.0, 2)
        acl_p = round(max(0.0, held_frac * 100.0 - npu_exec_p - waste_p), 2)
        free_p = round(max(0.0, 100.0 - npu_exec_p - acl_p - waste_p), 2)
        die_duty_10m = {
            "npu_exec_pct": npu_exec_p,
            "acl_host_pct": acl_p,
            "lock_waste_pct": waste_p,
            "free_pct": free_p,
        }
        cur_hold = float(die_locks_info[0].get("elapsed_sec") or 0) if (die_locks_info and die_locks_info[0]["state"] == "HELD") else 20.0
        avg_hold_sec = round(max(18.0, cur_hold), 1)
        slots_per_hr = round(3600.0 / max(12.0, avg_hold_sec), 1)

    for idx_w, wt in enumerate(waiting_tasks_list):
        wt["estimated_wait_sec"] = int(round((idx_w + 1) * min(avg_hold_sec, 180.0)))

    probe_ms = round((time.monotonic() - t0) * 1000, 1)

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
        "history": history,
        "die_duty_10m": die_duty_10m,
        "slots_per_hr": slots_per_hr,
        "avg_die_hold_sec": avg_hold_sec,
        "parallel_cpu_builds": parallel_cpu_builds,
        "die_locks": die_locks_info,
        "tasks": tasks,
    }


if __name__ == "__main__":
    print(json.dumps(build_snapshot(), ensure_ascii=False))
