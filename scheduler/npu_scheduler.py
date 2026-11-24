"""NPU Task Sequence Scheduler: Split-Phase Pipeline + 2-Queue MLFQ + WSPT + Aging.

Implements:
  1. Semantic Task Classification:
     `BUILD`, `FAST-LANE`, `PRECISION`, `PROFILER`, `STRESS`, `WAITING-DIE`
  2. 2-Queue Multi-Level Feedback Queue (Corbato / OSTEP Ch. 8):
     - `Q0` (`FAST-LANE`, <= 180s quantum): interactive smoke + targeted boundary subset
       with per-problem attained-service allotment (anti-gaming Rule 4)
     - `Q1` (`BATCH-GATE`, <= 1200s quantum): full extended suite + stress + profiler spectrum
     - Anti-Starvation Aging (Rule 5): continuous linear aging `wait_sec / tau` + hard
       promotion `Q1 -> Q0*` after `max_wait_promote_sec = 1800s`
  3. Smith's Rule / WSPT & Gittins Value-Density Priority:
     `wspt_priority = (difficulty * recoverable_headroom * cb_factor) / est_service_sec + aging_boost`
  4. Strictly Read-Only Live SSH + Local Process/Log Correlation Probe:
     Reads `/proc/locks`, `/proc/<pid>`, `ps`, and `npu-smi` on `ssh npu` WITHOUT ever
     opening or touching any `.lock` file via `flock`/`fcntl`, and correlates remote
     waiters with local `npu-test.py` / `opsb test` processes and in-flight logs.
  5. Split-Phase Execution (`schedule_test`):
     Decouples 32-core CPU compilation & pure-host `walk_model.py` checks from the
     single-die NPU lock so compile errors consume 0.0s of NPU time.
"""
from __future__ import annotations

import dataclasses
import datetime
import json
import os
import pathlib
import re
import shlex
import statistics
import subprocess
import time
from typing import Any

from .estimator import PROBLEM_DIFFICULTY, ScoreEstimator, resolve_sha
from .gate import GateConfig, SubmissionGate

NPU_TASK_TAGS: tuple[str, ...] = (
    "BUILD",
    "FAST-LANE",
    "PRECISION",
    "PROFILER",
    "STRESS",
    "WAITING-DIE",
    "BACKFILL",  # B0 lane: baseline perf-bucket pairing only (never preempts Q0/Q1)
)

#: Lexicographic ticket tiers shared with `scheduler/backfill.py` and `tools/npu-test.py`.
#: Higher wins; `prio`/aging only reorder *inside* the same tier.
LANE_TICKET_TIER: dict[str, int] = {"B0": 0, "Q1": 1, "Q0": 2, "Q0*": 2}

# Empirically measured non-queued Q1 validation durations (seconds) across 10 problems
MEDIAN_Q1_SERVICE_SEC_FALLBACK: dict[str, float] = {
    "CTCLossV2": 85.7,
    "ConcatD": 134.3,
    "SyncBatchNormGatherStats": 153.7,
    "QuantUpdateScatter": 160.8,
    "PReluGradUpdate": 249.6,
    "LayerNormV3": 298.5,
    "StridedSliceGrad": 591.3,
    "ConfusionTransposeD": 631.3,
    "MultilabelMarginLoss": 803.0,
    "PadV3Grad": 861.5,
}

VENDOR_TO_PROBLEM: dict[str, str] = {
    "concat_d": "ConcatD",
    "confusion_transpose": "ConfusionTransposeD",
    "ctc_loss_v2": "CTCLossV2",
    "layer_norm_v3": "LayerNormV3",
    "multilabel_margin_loss": "MultilabelMarginLoss",
    "pad_v3_grad": "PadV3Grad",
    "p_relu_grad_update": "PReluGradUpdate",
    "prelu_grad_update": "PReluGradUpdate",
    "quant_update_scatter": "QuantUpdateScatter",
    "strided_slice_grad": "StridedSliceGrad",
    "sync_batch_norm_gather_stats": "SyncBatchNormGatherStats",
}


@dataclasses.dataclass
class NpuTask:
    pid: int
    title: str
    sha: str | None
    short_sha: str | None
    state: str          # ACTIVE | CPU_BUILD | WAITING
    tag: str            # BUILD | FAST-LANE | PRECISION | PROFILER | STRESS | WAITING-DIE
    mlfq_level: str     # Q0 | Q0* | Q1 | CPU
    die: int | None
    elapsed_sec: int
    est_service_sec: float
    wspt_base: float
    aging_boost: float
    wspt_priority: float
    dispatch_rank: int
    starved: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class ProblemPriority:
    rank: int
    title: str
    difficulty: int
    board_status: str
    current_score: float
    active_board_sha: str | None
    decay_factor_gamma: float
    raw_historical_best_score: float
    contemporary_best_score: float
    true_restore_gain: float
    historical_best_score: float
    historical_best_sha: str | None
    frontier_top1: float
    recoverable_headroom: float
    weighted_headroom: float
    est_q0_sec: float
    est_q1_sec: float
    wspt_q0: float
    wspt_q1: float
    circuit_state: str
    in_flight_tag: str | None
    recommended_lane: str
    recommended_action: str
    calibration: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# Read-only remote probe script: NEVER opens or flocks any .lock file!
_REMOTE_READONLY_PROBE = r"""
import datetime, glob, json, os, pathlib, re, subprocess, time

t0 = time.monotonic()
KNOWN = [
    "ConcatD", "ConfusionTransposeD", "CTCLossV2", "LayerNormV3",
    "MultilabelMarginLoss", "PadV3Grad", "PReluGradUpdate",
    "QuantUpdateScatter", "StridedSliceGrad", "SyncBatchNormGatherStats",
]
VENDORS = {
    "concat_d": "ConcatD", "confusion_transpose": "ConfusionTransposeD",
    "ctc_loss_v2": "CTCLossV2", "layer_norm_v3": "LayerNormV3",
    "multilabel_margin_loss": "MultilabelMarginLoss", "pad_v3_grad": "PadV3Grad",
    "p_relu_grad_update": "PReluGradUpdate", "prelu_grad_update": "PReluGradUpdate",
    "quant_update_scatter": "QuantUpdateScatter", "strided_slice_grad": "StridedSliceGrad",
    "sync_batch_norm_gather_stats": "SyncBatchNormGatherStats",
}
SHA40 = re.compile(r"\b([0-9a-f]{40})\b")

# 1. CPU & Memory
load_1, load_5, load_15 = 0.0, 0.0, 0.0
try:
    lp = pathlib.Path("/proc/loadavg").read_text().split()
    load_1, load_5, load_15 = float(lp[0]), float(lp[1]), float(lp[2])
except Exception:
    pass

# 2. NPU-SMI (read-only)
env = os.environ.copy()
lib_dirs = [
    "/opt/driver/driver/lib64",
    "/opt/driver/driver/lib64/common",
    "/opt/driver/driver/lib64/driver",
    "/opt/toolchain/latest/latest/lib64",
]
env["LD_LIBRARY_PATH"] = ":".join(lib_dirs + ([env["LD_LIBRARY_PATH"]] if env.get("LD_LIBRARY_PATH") else []))
devices = []
try:
    smi = subprocess.run(["npu-smi", "info"], env=env, capture_output=True, text=True, timeout=2.5)
    if smi.returncode == 0:
        lines = smi.stdout.splitlines()
        i = 0
        while i < len(lines) - 1:
            l1, l2 = lines[i].strip(), lines[i + 1].strip()
            if l1.startswith("|") and l2.startswith("|") and "/" in l1 and "/" in l2:
                c1 = [x.strip() for x in l1.strip("|").split("|")]
                c2 = [x.strip() for x in l2.strip("|").split("|")]
                if len(c1) >= 3 and len(c2) >= 3:
                    p1, health, m1 = c1[0].split(), c1[1], c1[2].split()
                    p2, bus_id, m2 = c2[0].split(), c2[1], c2[2].split()
                    if len(p1) >= 2 and p1[0].isdigit() and health in ("OK", "Warning", "Error", "Alarm"):
                        slashes = re.findall(r"(\d+)\s*/\s*(\d+)", c2[2])
                        hu, ht = (int(slashes[-1][0]), int(slashes[-1][1])) if slashes else (0, 65536)
                        devices.append({
                            "npu_id": int(p1[0]),
                            "chip_id": int(p2[0]) if p2 and p2[0].isdigit() else 0,
                            "soc_name": p1[1] if p1[1].lower().startswith("accel") else "Accel" + p1[1],
                            "health": health,
                            "bus_id": bus_id,
                            "power_w": float(m1[0]) if m1 and re.match(r"^[\d.]+$", m1[0]) else None,
                            "temp_c": int(float(m1[1])) if len(m1) > 1 and re.match(r"^[\d.]+$", m1[1]) else None,
                            "aicore_pct": int(float(m2[0])) if m2 and re.match(r"^[\d.]+$", m2[0]) else 0,
                            "hbm_used_mib": hu,
                            "hbm_total_mib": ht,
                        })
                        i += 2
                        continue
            i += 1
except Exception:
    pass

# 3. Process table
procs = {}
try:
    out = subprocess.check_output(["ps", "-eo", "pid,ppid,etimes,pcpu,rss,args", "--no-headers"], text=True, timeout=2.0)
    for line in out.splitlines():
        parts = line.strip().split(None, 5)
        if len(parts) >= 6 and parts[0].isdigit():
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

children = {}
for pid, p in procs.items():
    children.setdefault(p["ppid"], []).append(pid)

def descendants(rpid):
    res, stack, seen = [], [rpid], set()
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        res.append(cur)
        stack.extend(children.get(cur, []))
    return res

# 4. Read /proc/locks matched against stat() inodes (strictly read-only, zero open() on locks!)
inodes = {}
for p in glob.glob("/srv/work*.lock"):
    try:
        st = os.stat(p)
        inodes[f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"] = os.path.basename(p)
    except OSError:
        pass

die_holders = {}
waiting_roots = set()
try:
    for line in pathlib.Path("/proc/locks").read_text().splitlines():
        for ikey, bname in inodes.items():
            if ikey in line:
                parts = line.split()
                is_blocked = len(parts) > 1 and parts[1] == "->"
                off = 1 if is_blocked else 0
                if len(parts) >= 5 + off and parts[4 + off].isdigit():
                    pid = int(parts[4 + off])
                    mode = parts[3 + off].upper()
                    m_die = re.search(r"device(\d+)\.lock$", bname)
                    if m_die:
                        d_idx = int(m_die.group(1))
                        if "WRITE" in mode and not is_blocked:
                            die_holders[d_idx] = pid
                        else:
                            waiting_roots.add(pid)
                    elif bname.endswith("validation.lock"):
                        waiting_roots.add(pid)
except Exception:
    pass

for pid, p in procs.items():
    if "bench-device" in p["cmd"] and "flock" in p["cmd"] and pid not in die_holders.values():
        waiting_roots.add(pid)

holder_trees = {}
covered_pids = set()
for d_idx, h_pid in die_holders.items():
    t = descendants(h_pid)
    holder_trees[d_idx] = t
    covered_pids.update(t)
    pp = procs.get(h_pid, {}).get("ppid", 0)
    if pp:
        covered_pids.add(pp)

waiting_roots = {pid for pid in waiting_roots if pid not in covered_pids and pid in procs}

def infer_meta(pids):
    prob, sha = None, None
    for pid in pids:
        cmd = procs.get(pid, {}).get("cmd", "")
        if not prob:
            for k in KNOWN:
                if k in cmd or k.lower() in cmd.lower():
                    prob = k
                    break
        if not prob:
            for vk, vn in VENDORS.items():
                if vk in cmd.lower():
                    prob = vn
                    break
        if not sha:
            m = SHA40.search(cmd)
            if m:
                sha = m.group(1)
            else:
                mr = re.search(r"bench-results/[^/]+/([0-9a-f]{7,40})", cmd)
                if mr:
                    sha = mr.group(1)
        if not prob or not sha:
            try:
                cwd = os.readlink(f"/proc/{pid}/cwd")
                if not prob:
                    for k in KNOWN:
                        if f"/{k}" in cwd:
                            prob = k
                            break
                if prob and not sha:
                    hf = pathlib.Path(f"/srv/work/{prob}/.git/HEAD")
                    if hf.is_file():
                        ht = hf.read_text().strip()
                        if re.fullmatch(r"[0-9a-f]{40}", ht):
                            sha = ht
            except Exception:
                pass
    return prob, sha

def classify(pids, holds_die):
    if not holds_die:
        return "WAITING-DIE", "Waiting for exclusive NPU die lock"
    joined = "\n".join(procs.get(pid, {}).get("cmd", "") for pid in pids)
    low = joined.lower()
    if "profiler" in low or "kperf" in low:
        mc = re.search(r"--(?:ref-)?worker\s+(\S+)", joined)
        return "PROFILER", (f"profiler ({mc.group(1)})" if mc else "Hardware kernel profiling (profiler)")
    if any(w in low for w in ("build.sh", "cmake", "/ccec", "compiler", "kernelc", "ninja")):
        return "BUILD", "Compiling KernelC host/kernel package"
    if "--fast" in low or "fast_lane=1" in low or "fast-lane" in low:
        return "FAST-LANE", "Q0 fast-lane smoke & targeted cases"
    if "stress" in low or "determinism" in low or "det_bad" in low:
        return "STRESS", "Multi-run determinism & stress gate"
    if "validation/perf.py" in low:
        return "BACKFILL", "B0 baseline bucket pairing (perf-only, 0s build)"
    return "PRECISION", "Extended precision & boundary suite"

raw_tasks = []
die_locks = []
max_dies = max(1, len(devices))
for d_idx in range(max_dies):
    h_pid = die_holders.get(d_idx)
    if h_pid is not None and h_pid in procs:
        tree = holder_trees.get(d_idx, [h_pid])
        prob, sha = infer_meta(tree)
        tag, det = classify(tree, True)
        el = procs[h_pid]["elapsed_sec"]
        die_locks.append({"die": d_idx, "state": "HELD", "holder_pid": h_pid, "problem": prob, "sha": sha, "tag": tag, "elapsed_sec": el})
        raw_tasks.append({"pid": h_pid, "die": d_idx, "state": "ACTIVE", "tag": tag, "problem": prob, "sha": sha, "elapsed_sec": el, "detail": det})
    else:
        die_locks.append({"die": d_idx, "state": "FREE", "holder_pid": None, "problem": None, "sha": None, "tag": None, "elapsed_sec": 0})

for w_pid in sorted(waiting_roots, key=lambda x: (-procs[x]["elapsed_sec"], x)):
    tree = descendants(w_pid)
    covered_pids.update(tree)
    prob, sha = infer_meta(tree)
    w_cmd = procs[w_pid]["cmd"]
    is_fast = ("--fast" in w_cmd or "FAST_LANE=1" in w_cmd)
    mode = "flock_waitqueue" if "flock " in w_cmd else "spin_poll_10s"
    raw_tasks.append({
        "pid": w_pid, "die": None, "state": "WAITING", "tag": "WAITING-DIE",
        "problem": prob, "sha": sha, "elapsed_sec": procs[w_pid]["elapsed_sec"],
        "fast": is_fast, "wait_mechanism": mode,
        "detail": f"Waiting for NPU die ({mode})",
    })

# 5. Detect decoupled CPU-only builds outside NPU lock (`~/.opsb/cpu_builds/*.json` + compiler/cmake/ccec)
opsb_home = pathlib.Path.home() / ".opsb"
seen_cpu_builds = set()
cpu_builds_dir = opsb_home / "cpu_builds"
if cpu_builds_dir.is_dir():
    for bf in sorted(cpu_builds_dir.glob("*.json")):
        try:
            bd = json.loads(bf.read_text())
            bpid = int(bd.get("pid") or 0)
            if bd.get("finished_at") or bpid <= 0 or bpid not in procs or bpid in covered_pids:
                continue
            bprob = bd.get("title") or bd.get("problem")
            bsha = bd.get("sha")
            el_b = int(max(0.0, time.time() - float(bd.get("started_epoch") or time.time())))
            covered_pids.add(bpid)
            seen_cpu_builds.add(f"{bprob}:{(bsha or '')[:7]}")
            raw_tasks.append({
                "pid": bpid, "die": None, "state": "CPU_BUILD", "tag": "BUILD",
                "problem": bprob, "sha": bsha, "elapsed_sec": el_b,
                "fast": True, "detail": "Phase A 32-core CPU build (0.0s NPU lock)",
            })
        except Exception:
            pass

for pid in sorted(procs):
    p = procs[pid]
    if pid in covered_pids:
        continue
    low = p["cmd"].lower()
    if any(k in low for k in ("compiler", "/ccec", "cmake")) and ("sdk" in low or "bench" in low or "tikcfw" in low):
        rpid = p["ppid"] if p["ppid"] in procs and p["ppid"] > 100 else pid
        if rpid in covered_pids:
            continue
        tree = descendants(rpid)
        covered_pids.update(tree)
        prob, sha = infer_meta(tree)
        bkey = f"{prob}:{(sha or '')[:7]}"
        if bkey not in seen_cpu_builds:
            seen_cpu_builds.add(bkey)
            raw_tasks.append({
                "pid": rpid, "die": None, "state": "CPU_BUILD", "tag": "BUILD",
                "problem": prob, "sha": sha, "elapsed_sec": procs.get(rpid, p)["elapsed_sec"],
                "fast": True, "detail": "Decoupled 32-core CPU compile (no NPU die lock held)",
            })

# Enrich active die lock AND matching ACTIVE task with `~/.opsb/die_session.json` only if pid matches holder tree
sess_file = opsb_home / "die_session.json"
if sess_file.is_file() and die_locks:
    try:
        ds = json.loads(sess_file.read_text())
        ds_die = int(ds.get("die", 0))
        ds_pid = int(ds.get("pid") or 0)
        if 0 <= ds_die < len(die_locks) and die_locks[ds_die]["state"] == "HELD":
            h_pid = die_locks[ds_die]["holder_pid"]
            tree_pids = set(holder_trees.get(ds_die, [h_pid]))
            if h_pid in procs and procs[h_pid].get("ppid"):
                tree_pids.add(procs[h_pid]["ppid"])
            if ds_pid > 0 and ds_pid in tree_pids:
                if ds.get("title") and not die_locks[ds_die].get("problem"):
                    die_locks[ds_die]["problem"] = ds["title"]
                if ds.get("sha") and not die_locks[ds_die].get("sha"):
                    die_locks[ds_die]["sha"] = ds["sha"]
                if ds.get("phase"):
                    die_locks[ds_die]["phase"] = ds["phase"]
                for rt in raw_tasks:
                    if rt.get("state") == "ACTIVE" and rt.get("die") == ds_die and rt.get("pid") == h_pid:
                        if ds.get("title") and not rt.get("problem"):
                            rt["problem"] = ds["title"]
                        if ds.get("sha") and not rt.get("sha"):
                            rt["sha"] = ds["sha"]
                        if ds.get("mode") == "B0" and rt.get("tag") != "BACKFILL":
                            rt["tag"] = "BACKFILL"
                        break
    except Exception:
        pass

die_duty_10m = None
ledger_f = opsb_home / "die_ledger.jsonl"
if ledger_f.is_file():
    try:
        now_ep = time.time()
        lrows = [json.loads(ln) for ln in ledger_f.read_text().splitlines()[-120:] if ln.strip()]
        lrows = [r for r in lrows if now_ep - float(r.get("ended_epoch") or r.get("ts") or 0) <= 600.0]
        if lrows:
            npu_s = sum(float(r.get("npu_exec_sec") or 0.0) for r in lrows)
            acl_s = sum(float(r.get("acl_host_sec") or 0.0) for r in lrows)
            wst_s = sum(float(r.get("lock_waste_sec") or 0.0) for r in lrows)
            hold_s = sum(float(r.get("hold_sec") or r.get("phase_b_die_sec") or 0.0) for r in lrows)
            denom = max(600.0, hold_s, 1.0)
            die_duty_10m = {
                "runs_10m": len(lrows),
                "avg_hold_sec": round(hold_s / len(lrows), 1),
                "slots_per_hr": round(min(240.0, 3600.0 / max(8.0, hold_s / len(lrows))), 1),
                "npu_exec_pct": round(100.0 * npu_s / denom, 2),
                "acl_host_pct": round(100.0 * acl_s / denom, 2),
                "lock_waste_pct": round(100.0 * wst_s / denom, 2),
                "free_pct": round(100.0 * max(0.0, denom - npu_s - acl_s - wst_s) / denom, 2),
            }
    except Exception:
        pass

# 7. Cooperative WSPT tickets (`/tmp/opsb-npu-tickets/*.json`) — read-only, ZERO flock
npu_tickets = []
ticket_dir = pathlib.Path('/tmp/opsb-npu-tickets')
if ticket_dir.is_dir():
    now_ep = time.time()
    for tf in sorted(ticket_dir.glob('*.json')):
        try:
            td = json.loads(tf.read_text())
        except Exception:
            continue
        try:
            tpid = int(td.get('pid') or 0)
        except Exception:
            continue
        if tpid <= 0:
            continue
        alive = tpid in procs or pathlib.Path('/proc/%d' % tpid).exists()
        try:
            tier = int(td.get('tier', 1))
        except Exception:
            tier = 1
        lane = td.get('lane') or ('B0' if tier <= 0 else ('Q0' if tier >= 2 else 'Q1'))
        try:
            created = float(td.get('created') or 0.0)
        except Exception:
            created = 0.0
        npu_tickets.append({
            'pid': tpid,
            'title': td.get('title'),
            'sha': td.get('sha'),
            'tier': tier,
            'lane': lane,
            'kind': td.get('kind'),
            'prio': round(float(td.get('prio') or 0.0), 4),
            'fast': bool(td.get('fast')),
            'created': created,
            'waiting_sec': round(max(0.0, now_ep - created), 1) if created else 0.0,
            'alive': bool(alive),
        })

devices.sort(key=lambda d: (int(d.get("npu_id", 0)), int(d.get("chip_id", 0))))
npu_tickets.sort(key=lambda t: (-int(t.get("tier", 1)), -float(t.get("prio", 0.0)), float(t.get("created", 0.0)), int(t.get("pid", 0))))

print(json.dumps({
    "sampled_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "probe_latency_ms": round((time.monotonic() - t0) * 1000, 1),
    "cpu_cores": os.cpu_count() or 32,
    "load_1m": round(load_1, 2),
    "load_5m": round(load_5, 2),
    "devices": devices,
    "die_locks": die_locks,
    "die_duty_10m": die_duty_10m,
    "npu_tickets": npu_tickets,
    "raw_tasks": raw_tasks,
}))
"""


def _parse_ps_etime(etime_str: str) -> int:
    """Parse macOS/POSIX `ps -o etime` (`[[dd-]hh:]mm:ss`) into integer seconds."""
    s = etime_str.strip()
    days = 0
    if "-" in s:
        d_part, s = s.split("-", 1)
        if d_part.isdigit():
            days = int(d_part)
    parts = [int(p) for p in s.split(":") if p.isdigit()]
    if len(parts) == 3:
        return days * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return days * 86400 + parts[0] * 60 + parts[1]
    if len(parts) == 1:
        return parts[0]
    return 0


class NpuTaskScheduler:
    """MLFQ (Q0/Q1) + WSPT Value-Density + Aging scheduler and live NPU queue inspector."""

    def __init__(
        self,
        workspace: pathlib.Path,
        estimator: ScoreEstimator | None = None,
        gate: SubmissionGate | None = None,
        config: GateConfig | None = None,
        npu_host: str = "npu",
    ) -> None:
        self.workspace = workspace.resolve()
        self.estimator = estimator or ScoreEstimator(self.workspace)
        self.config = config or GateConfig()
        self.gate = gate or SubmissionGate(self.workspace, estimator=self.estimator, config=self.config)
        self.npu_host = npu_host
        self.service_sec_map = self._learn_service_durations()

    def _learn_service_durations(self) -> dict[str, float]:
        """Learn empirical non-queued Q1 service times from local results/<Title>/*/result.json."""
        durations = dict(MEDIAN_Q1_SERVICE_SEC_FALLBACK)
        for title in self.estimator.problems_meta:
            samples: list[float] = []
            res_dir = self.workspace / "results" / title
            if not res_dir.is_dir():
                continue
            for rj in res_dir.glob("*/result.json"):
                vlog = rj.parent / "validation.log"
                if not vlog.is_file():
                    continue
                try:
                    txt = vlog.read_text(errors="replace")
                except OSError:
                    continue
                if "EXTENDED_PASSED" not in txt or "FAST_LANE=1" in txt:
                    continue
                try:
                    rj_data = json.loads(rj.read_text())
                except Exception:
                    rj_data = {}
                if isinstance(rj_data.get("phase_b_die_sec"), (int, float)) and rj_data["phase_b_die_sec"] > 0:
                    samples.append(float(rj_data["phase_b_die_sec"]))
                    continue
                if "Waiting for a free NPU die" in txt:
                    continue
                st_v = vlog.stat()
                st_r = rj.stat()
                birth = getattr(st_v, "st_birthtime", st_v.st_ctime)
                dt = st_r.st_mtime - birth
                if 10.0 <= dt <= 1800.0:
                    samples.append(dt)
            if len(samples) >= 2:
                durations[title] = round(float(statistics.median(samples)), 1)
        return durations

    def est_service_sec(self, title: str, fast: bool = False) -> float:
        full_sec = self.service_sec_map.get(title, 300.0)
        if fast:
            return round(min(90.0, max(45.0, 0.30 * full_sec)), 1)
        return round(full_sec, 1)

    def compute_wspt(
        self,
        title: str,
        fast: bool = False,
        wait_sec: int = 0,
    ) -> tuple[float, float, float, str]:
        """Compute `(wspt_base, aging_boost, wspt_priority, mlfq_level)` for a task."""
        ledger = self.estimator.get_problem_ledger(title)
        cb = self.gate.get_circuit_breaker(title)
        p_sec = self.est_service_sec(title, fast=fast)

        # Effective headroom: boost CRITICAL_RESTORE (board == 0), dampen OPEN circuit breaker
        headroom = ledger.recoverable_headroom
        if ledger.current_score <= 0.0:
            headroom = max(headroom, ledger.historical_best_mean + 25.0, 60.0)
        cb_factor = 0.25 if cb.state == "OPEN" else 1.0

        wspt_base = round((ledger.difficulty * headroom * cb_factor) / max(30.0, p_sec), 4)
        aging_boost = round(max(0, wait_sec) / max(60.0, self.config.aging_tau_sec), 4)

        if fast and wait_sec <= self.config.q0_quantum_sec:
            mlfq = "Q0"
        elif not fast and wait_sec >= 1800:
            # OSTEP Rule 5 anti-starvation promotion
            mlfq = "Q0*"
        else:
            mlfq = "Q1"

        return wspt_base, aging_boost, round(wspt_base + aging_boost, 4), mlfq

    def _scan_local_inflight(self) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """Scan local `npu-test.py` processes and unfinished `results/<Title>/<SHA>/validation.log`."""
        local_waiters: list[dict[str, Any]] = []
        inflight_sha_by_title: dict[str, str] = {}

        # 1. Check unfinished validation.log files (newest per problem)
        for title in self.estimator.problems_meta:
            res_dir = self.workspace / "results" / title
            if not res_dir.is_dir():
                continue
            candidates: list[tuple[float, str]] = []
            for sub in res_dir.iterdir():
                if not sub.is_dir() or not re.fullmatch(r"[0-9a-f]{40}", sub.name.lower()):
                    continue
                vlog = sub / "validation.log"
                rj = sub / "result.json"
                if vlog.is_file() and not rj.is_file():
                    candidates.append((vlog.stat().st_mtime, sub.name.lower()))
            if candidates:
                candidates.sort(reverse=True)
                inflight_sha_by_title[title] = candidates[0][1]

        # 2. Check local macOS `ps` for active `npu-test.py <Title>` or `opsb test <Title>` callers
        ordered_titles = sorted(self.estimator.problems_meta, key=lambda x: (-len(x), x))
        try:
            out = subprocess.check_output(["ps", "-eo", "pid,etime,args"], text=True, timeout=2.0)
            seen_titles: set[str] = set()
            for line in out.splitlines():
                if "npu-test.py" not in line and "opsb test" not in line and "schedule-test" not in line:
                    continue
                if "grep" in line or "uv run" in line:
                    continue
                parts = line.strip().split(None, 2)
                if len(parts) < 3 or not parts[0].isdigit():
                    continue
                el_sec = _parse_ps_etime(parts[1])
                cmd = parts[2]
                matched_title = next((t for t in ordered_titles if re.search(rf"\b{t}\b", cmd)), None)
                if matched_title and matched_title not in seen_titles:
                    seen_titles.add(matched_title)
                    local_waiters.append({
                        "local_pid": int(parts[0]),
                        "title": matched_title,
                        "elapsed_sec": el_sec,
                        "fast": "--fast" in cmd,
                        "sha": inflight_sha_by_title.get(matched_title),
                    })
        except Exception:
            pass

        local_waiters.sort(key=lambda lw: (-int(lw["elapsed_sec"]), str(lw["title"]), int(lw["local_pid"])))
        return local_waiters, inflight_sha_by_title

    def inspect_live_queue(self, offline: bool = False) -> dict[str, Any]:
        """Probe remote NPU server read-only (zero lock touch) and enrich with MLFQ+WSPT."""
        local_waiters, inflight_shas = self._scan_local_inflight()

        remote_data: dict[str, Any] | None = None
        ssh_error: str | None = None
        if not offline:
            try:
                proc = subprocess.run(
                    [
                        "ssh",
                        "-o",
                        "BatchMode=yes",
                        "-o",
                        "ConnectTimeout=2",
                        "-o",
                        "ControlMaster=auto",
                        "-o",
                        "ControlPath=/tmp/opsb-ssh-%r@%h:%p",
                        "-o",
                        "ControlPersist=180",
                        self.npu_host,
                        "python3 -c " + shlex.quote(_REMOTE_READONLY_PROBE),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=3.2,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    remote_data = json.loads(proc.stdout.strip().splitlines()[-1])
                else:
                    ssh_error = (proc.stderr or proc.stdout).strip()[:200] or f"ssh rc={proc.returncode}"
            except Exception as exc:
                ssh_error = str(exc)[:200]

        if remote_data is None:
            # Graceful offline synthesis from local in-flight processes
            raw_tasks = [
                {
                    "pid": lw["local_pid"],
                    "die": None,
                    "state": "WAITING",
                    "tag": "WAITING-DIE",
                    "problem": lw["title"],
                    "sha": lw["sha"],
                    "elapsed_sec": lw["elapsed_sec"],
                    "fast": lw["fast"],
                    "detail": "Local npu-test session (SSH offline/skipped)",
                }
                for lw in local_waiters
            ]
            remote_data = {
                "sampled_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "probe_latency_ms": 0.0,
                "cpu_cores": 32,
                "load_1m": None,
                "load_5m": None,
                "devices": [],
                "die_locks": [],
                "npu_tickets": [],
                "raw_tasks": raw_tasks,
                "offline": True,
                "ssh_error": ssh_error,
            }

        # Correlate remote unidentified WAITING python3 -c processes with local `npu-test.py` by elapsed_sec
        unmatched_local = list(local_waiters)
        # Remove problems already identified on remote
        known_remote_titles = {t.get("problem") for t in remote_data.get("raw_tasks", []) if t.get("problem")}
        unmatched_local = [lw for lw in unmatched_local if lw["title"] not in known_remote_titles]

        for rt in remote_data.get("raw_tasks", []):
            if not rt.get("problem") and rt.get("state") == "WAITING" and unmatched_local:
                r_el = int(rt.get("elapsed_sec", 0))
                best_lw = min(
                    unmatched_local,
                    key=lambda lw: (abs(lw["elapsed_sec"] - r_el), str(lw["title"]), int(lw["local_pid"])),
                )
                if abs(best_lw["elapsed_sec"] - r_el) <= 45:
                    rt["problem"] = best_lw["title"]
                    rt["sha"] = best_lw["sha"]
                    rt["fast"] = best_lw["fast"]
                    unmatched_local.remove(best_lw)
            if rt.get("problem") and not rt.get("sha"):
                rt["sha"] = inflight_shas.get(rt["problem"])

        # Build MLFQ + WSPT enriched NpuTask list
        enriched: list[NpuTask] = []
        starvation_warnings: list[str] = []

        for rt in remote_data.get("raw_tasks", []):
            title = rt.get("problem") or "Unknown"
            sha = resolve_sha(self.workspace, title, rt.get("sha")) if title != "Unknown" and rt.get("sha") else rt.get("sha")
            short_sha = sha[:7] if sha else None
            state = str(rt.get("state", "WAITING"))
            if state not in ("ACTIVE", "CPU_BUILD", "WAITING"):
                state = "WAITING"
            tag = str(rt.get("tag", "WAITING-DIE"))
            el_sec = max(0, int(rt.get("elapsed_sec", 0)))
            is_fast = bool(rt.get("fast", False) or tag == "FAST-LANE")

            # Enforce valid (state, tag) combinations so UI never sees contradictory badges
            if state == "CPU_BUILD":
                canonical_tag = "BUILD"
            elif state == "WAITING":
                canonical_tag = "BACKFILL" if tag == "BACKFILL" else "WAITING-DIE"
            else:  # ACTIVE
                canonical_tag = tag if (tag in NPU_TASK_TAGS and tag != "WAITING-DIE") else "PRECISION"

            if state == "CPU_BUILD":
                mlfq = "CPU"
                est_s = 35.0
                w_base, age_b, w_prio = 0.0, 0.0, 0.0
                starved = False
            elif canonical_tag == "BACKFILL":
                mlfq = "B0"
                est_s = 25.0
                w_base, age_b, w_prio = 0.0, 0.0, 0.0
                starved = False
            elif title in self.estimator.problems_meta:
                wait_for_aging = el_sec if state == "WAITING" else 0
                w_base, age_b, w_prio, mlfq = self.compute_wspt(title, fast=is_fast, wait_sec=wait_for_aging)
                # OSTEP Rule 4a demotion if active task exceeded Q0 quantum (180s)
                if state == "ACTIVE" and is_fast and el_sec > self.config.q0_quantum_sec:
                    mlfq = "Q1"
                est_s = self.est_service_sec(title, fast=(mlfq == "Q0"))
                starved = state == "WAITING" and el_sec >= 1800
                if starved:
                    starvation_warnings.append(
                        f"{title} ({short_sha or '?'}) waiting {el_sec // 60}m in {rt.get('wait_mechanism', 'poll')} "
                        f"— promoted to Q0* (priority={w_prio:.2f})"
                    )
            else:
                mlfq = "Q0" if is_fast else "Q1"
                est_s = 90.0 if is_fast else 300.0
                w_base = 0.1
                age_b = round(el_sec / max(60.0, self.config.aging_tau_sec), 4) if state == "WAITING" else 0.0
                w_prio = round(w_base + age_b, 4)
                starved = state == "WAITING" and el_sec >= 1800

            enriched.append(
                NpuTask(
                    pid=int(rt.get("pid", 0)),
                    title=title,
                    sha=sha,
                    short_sha=short_sha,
                    state=state,
                    tag=canonical_tag,
                    mlfq_level=mlfq,
                    die=rt.get("die"),
                    elapsed_sec=el_sec,
                    est_service_sec=est_s,
                    wspt_base=w_base,
                    aging_boost=age_b,
                    wspt_priority=w_prio,
                    dispatch_rank=0,
                    starved=starved,
                    detail=str(rt.get("detail", "")),
                )
            )

        # Deterministic queue order: ACTIVE -> CPU_BUILD -> Q0/Q0* -> Q1 -> B0,
        # then wspt_priority DESC, elapsed_sec DESC, title ASC, sha ASC, pid ASC.
        def _sort_key(t: NpuTask) -> tuple[int, float, int, str, str, int]:
            if t.state == "ACTIVE":
                tier = 5
            elif t.state == "CPU_BUILD":
                tier = 4
            elif t.mlfq_level in ("Q0", "Q0*"):
                tier = 3
            elif t.mlfq_level == "B0":
                tier = 1
            else:
                tier = 2
            return (-tier, -float(t.wspt_priority), -int(t.elapsed_sec), str(t.title), str(t.sha or ""), int(t.pid))

        enriched.sort(key=_sort_key)
        starvation_warnings.sort()
        w_rank = 0
        for item in enriched:
            if item.state == "WAITING":
                w_rank += 1
                item.dispatch_rank = w_rank

        return {
            "sampled_at": remote_data.get("sampled_at"),
            "probe_latency_ms": remote_data.get("probe_latency_ms"),
            "offline": bool(remote_data.get("offline", False)),
            "ssh_error": remote_data.get("ssh_error"),
            "cpu_cores": remote_data.get("cpu_cores", 32),
            "load_1m": remote_data.get("load_1m"),
            "load_5m": remote_data.get("load_5m"),
            "devices": remote_data.get("devices", []),
            "die_locks": remote_data.get("die_locks", []),
            "die_duty_10m": remote_data.get("die_duty_10m"),
            "npu_tickets": remote_data.get("npu_tickets") or [],
            "starvation_warnings": starvation_warnings,
            "tasks": [t.to_dict() for t in enriched],
        }

    def rank_problem_priorities(self, live_tasks: list[dict[str, Any]] | None = None) -> list[ProblemPriority]:
        """Rank all 10 contest problems by WSPT Value-Density across Q0 and Q1."""
        active_tag_by_title: dict[str, str] = {}
        for t in live_tasks or []:
            p = t.get("title")
            if p and p not in active_tag_by_title:
                active_tag_by_title[p] = f"{t.get('state')}:{t.get('tag')}"

        rows: list[ProblemPriority] = []
        for title in sorted(self.estimator.problems_meta):
            ledger = self.estimator.get_problem_ledger(title)
            cb = self.gate.get_circuit_breaker(title)
            q0_s = self.est_service_sec(title, fast=True)
            q1_s = self.est_service_sec(title, fast=False)
            wspt_q0, _, _, _ = self.compute_wspt(title, fast=True, wait_sec=0)
            wspt_q1, _, _, _ = self.compute_wspt(title, fast=False, wait_sec=0)
            weighted_hr = round(ledger.difficulty * ledger.recoverable_headroom, 2)

            if ledger.current_score <= 0.0:
                rec_lane = "Q1 (CRITICAL_RESTORE)"
                rec_act = (
                    f"Submit verified SHA {(ledger.historical_best_sha or '')[:7]} immediately "
                    f"to recover +{ledger.historical_best_mean:.1f} pts from 0"
                )
            elif cb.state == "OPEN":
                rec_lane = "HOLD (SWITCH_BASELINE)"
                rec_act = "Circuit OPEN (6 non-improving shots in 12h); redesign algorithm locally before NPU slot"
            elif ledger.recoverable_headroom < 15.0:
                rec_lane = "DEPRIORITIZE (NEAR_FRONTIER)"
                rec_act = f"Within {ledger.recoverable_headroom:.1f} pts of site-wide #1 ({ledger.frontier_top1:.1f}); yield NPU to high-headroom ops"
            elif q1_s >= 400.0:
                rec_lane = "Q0 -> Q1 (SPLIT_ITERATE)"
                rec_act = f"Iterate in Q0 --fast ({q0_s:.0f}s vs {q1_s:.0f}s Q1, {q1_s / q0_s:.1f}x faster); run Q1 once before submit"
            else:
                rec_lane = "Q0 / Q1 (HIGH_DENSITY)"
                rec_act = f"High WSPT value density ({wspt_q1:.2f} w-pts/s); prioritize hidden-band throughput"

            rows.append(
                ProblemPriority(
                    rank=0,
                    title=title,
                    difficulty=ledger.difficulty,
                    board_status=ledger.board_status,
                    current_score=ledger.current_score,
                    active_board_sha=ledger.active_board_sha,
                    decay_factor_gamma=ledger.decay_factor_gamma,
                    raw_historical_best_score=ledger.raw_historical_best_score,
                    contemporary_best_score=ledger.contemporary_best_score,
                    true_restore_gain=ledger.true_restore_gain,
                    historical_best_score=ledger.raw_historical_best_score,
                    historical_best_sha=ledger.historical_best_sha,
                    frontier_top1=ledger.frontier_top1,
                    recoverable_headroom=ledger.recoverable_headroom,
                    weighted_headroom=weighted_hr,
                    est_q0_sec=q0_s,
                    est_q1_sec=q1_s,
                    wspt_q0=wspt_q0,
                    wspt_q1=wspt_q1,
                    circuit_state=cb.state,
                    in_flight_tag=active_tag_by_title.get(title),
                    recommended_lane=rec_lane,
                    recommended_action=rec_act,
                )
            )

        # Rank by Q0/effective WSPT priority (with CRITICAL_RESTORE boosted natively via compute_wspt)
        rows.sort(
            key=lambda r: (
                0 if r.current_score <= 0.0 else 1,
                -float(r.wspt_q1),
                -float(r.weighted_headroom),
                str(r.title),
            )
        )
        for idx, row in enumerate(rows, 1):
            row.rank = idx
        return rows

    def schedule_test(
        self,
        title: str,
        sha: str | None = None,
        fast: bool = False,
        build_only: bool = False,
        pmu: bool = False,
        dry_run: bool = False,
        stream_stdout: bool = True,
    ) -> dict[str, Any]:
        """3-Phase Disaggregated + Cooperative MLFQ/WSPT NPU test entry point (`opsb test`).

        Phase A (`0.0s` NPU die lock):
          Per-repo lock `/srv/work-repo-<Title>.lock`, heartbeat
          `~/.opsb/cpu_builds/<Title>.json`, detached git checkout, `walk_model.py` (if present),
          and 32-core CPU `build.sh -j4` (without `-e`) + `examples/build` pre-compilation.
          Exits immediately on compile error (`lock_waste_sec = 0.0`) or `--build-only`.
        Phase B (Cooperative WSPT Ticket -> Exclusive NPU Die Execution):
          Ticket `/tmp/opsb-npu-tickets/<pid>.json`, `device{0,1}.lock`, live `~/.opsb/die_session.json`,
          in-process `PersistentArenaRunner` (`OPSB_ARENA_ACCEL=1`, ~2ms/case vs 4.15s subprocess),
          and zero redundant C++ rebuild inside `device0.lock`.
        Phase C (`0.0s` NPU die lock):
          Immediate lock release + `~/.opsb/die_ledger.jsonl` duty-cycle telemetry.
        """
        if title not in self.estimator.problems_meta:
            raise ValueError(f"Unknown problem title: {title}")

        pmeta = self.estimator.problems_meta[title]
        repo = self.workspace / pmeta.get("path", f"bench/{title}")
        canonical_sha = resolve_sha(self.workspace, title, sha)
        wspt_base, _, wspt_prio, mlfq = self.compute_wspt(title, fast=fast, wait_sec=0)
        est_sec = 25.0 if build_only else self.est_service_sec(title, fast=fast)
        walk_script_exists = (repo / "validation" / "walk_model.py").is_file()
        is_template = (
            (repo / "CMakeLists.txt").is_file()
            and (repo / "run.sh").is_file()
            and not (repo / "build.sh").is_file()
        )
        if is_template:
            plan = {
                "title": title,
                "sha": canonical_sha,
                "short_sha": canonical_sha[:7] if canonical_sha else None,
                "layout": "template",
                "fast_lane": False,
                "build_only": build_only,
                "mlfq_level": "CPU" if build_only else mlfq,
                "quantum_sec": 300 if build_only else (self.config.q0_quantum_sec if fast else 1200),
                "est_service_sec": est_sec,
                "wspt_priority": 0.0 if build_only else wspt_prio,
                "phases": [
                    {
                        "phase": "A_REMOTE_CMAKE_BUILD",
                        "holds_npu_die_lock": False,
                        "heartbeat": f"~/.opsb/cpu_builds/{title}.json",
                        "steps": [
                            "git clone/fetch + checkout the pushed SHA on the NPU host",
                            "cmake -S . -B build-npu (Compiler ASC, CMAKE_ASC_RUN_MODE=npu)",
                            "cmake --build build-npu -j<N>",
                            "python3 sample.py generate --directory build-npu",
                        ],
                    },
                    {
                        "phase": "B_DEVICE_LOCK_RUN_AND_VERIFY",
                        "holds_npu_die_lock": True,
                        "npu_host": self.npu_host,
                        "steps": [
                            "device0.lock exclusive (shared with legacy runs)",
                            "./sample <device> under the official template",
                            "profiler --task-time=l0 task duration capture",
                            "python3 sample.py verify --directory build-npu",
                        ],
                    },
                    {
                        "phase": "C_ARTIFACTS",
                        "holds_npu_die_lock": False,
                        "steps": [
                            "Immediate device0.lock release",
                            "Write results/<Title>/<SHA>/{validation.log,result.json}",
                        ],
                    },
                ],
                "dry_run": dry_run,
            }
            if dry_run:
                return plan
            import importlib.util as _ilu

            npu_test_path = pathlib.Path(__file__).resolve().parents[1] / "runtime" / "npu-test.py"
            if not npu_test_path.is_file():
                npu_test_path = self.workspace / "tools" / "npu-test.py"
            spec = _ilu.spec_from_file_location("opsb_npu_test", str(npu_test_path))
            if spec is None or spec.loader is None:
                raise RuntimeError(f"Missing 3-Phase runner at {npu_test_path}")
            mod = _ilu.module_from_spec(spec)
            spec.loader.exec_module(mod)
            res_obj = mod.run_3phase_validation(
                title=title,
                sha_override=sha,
                sync_only=False,
                fast=fast,
                build_only=build_only,
                pmu=pmu,
                npu_host=self.npu_host,
                workspace=self.workspace,
                stream_stdout=stream_stdout,
            )
            return {**plan, "result": res_obj}

        phases: list[dict[str, Any]] = [
            {
                "phase": "A_CPU_BUILD_AND_PREFILTER",
                "holds_npu_die_lock": False,
                "repo_lock": f"/srv/work-repo-{title}.lock",
                "heartbeat": f"~/.opsb/cpu_builds/{title}.json",
                "steps": (
                    ["git fetch & detached checkout", "python3 validation/walk_model.py"]
                    if walk_script_exists
                    else ["git fetch & detached checkout"]
                )
                + [
                    "32-core CPU CMake + Compiler KernelC build (build.sh -j4 without -e)",
                    "Pre-compile examples/build (0.0s NPU lock)",
                ],
            }
        ]
        if not build_only:
            phases.extend(
                [
                    {
                        "phase": "B_NPU_ARENA_DIE_EXEC",
                        "holds_npu_die_lock": True,
                        "lock_files": [
                            "/srv/work-device0.lock",
                            "/srv/work-device1.lock",
                        ],
                        "ticket_dir": "/tmp/opsb-npu-tickets",
                        "heartbeat": "~/.opsb/die_session.json",
                        "env": {
                            "FAST_LANE": "1" if fast else "0",
                            "OPSB_ARENA_ACCEL": "1",
                            "PYTHONPATH": "/home/developer/.opsb/runtime",
                        },
                        "steps": [
                            "Cooperative MLFQ + WSPT + Aging ticket admission",
                            "Prebuilt examples/run.sh (skips redundant C++ rebuild inside device0.lock)",
                            "PersistentArenaRunner single-aclInit suite (<5s)" if not fast else "Q0 fast-lane subset (<12s)",
                            "Hardware profiler Task Duration capture",
                        ],
                    },
                    {
                        "phase": "C_POST_LOCK_LEDGER",
                        "holds_npu_die_lock": False,
                        "ledger": "~/.opsb/die_ledger.jsonl",
                        "steps": [
                            "Immediate device0.lock release",
                            "Append 10m duty-cycle telemetry to ~/.opsb/die_ledger.jsonl",
                            "Write results/<Title>/<SHA>/{validation.log,result.json}",
                        ],
                    },
                ]
            )

        plan = {
            "title": title,
            "sha": canonical_sha,
            "short_sha": canonical_sha[:7] if canonical_sha else None,
            "fast_lane": fast,
            "build_only": build_only,
            "mlfq_level": "CPU" if build_only else mlfq,
            "quantum_sec": 300 if build_only else (self.config.q0_quantum_sec if fast else 1200),
            "est_service_sec": est_sec,
            "wspt_priority": 0.0 if build_only else wspt_prio,
            "phases": phases,
            "dry_run": dry_run,
        }

        if dry_run:
            return plan

        import importlib.util

        npu_test_path = pathlib.Path(__file__).resolve().parents[1] / "runtime" / "npu-test.py"
        if not npu_test_path.is_file():
            npu_test_path = self.workspace / "tools" / "npu-test.py"
        spec = importlib.util.spec_from_file_location("opsb_npu_test", str(npu_test_path))
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Missing 3-Phase runner at {npu_test_path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        res_obj = mod.run_3phase_validation(
            title=title,
            sha_override=sha,
            sync_only=False,
            fast=fast,
            build_only=build_only,
            pmu=pmu,
            npu_host=self.npu_host,
            workspace=self.workspace,
            stream_stdout=stream_stdout,
        )
        return {**plan, "result": res_obj}
