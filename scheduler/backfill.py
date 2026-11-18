"""B0 Backfill Lane — automatic, idempotent, die-deferential baseline bucket pairing.

Problem
-------
`bench-specs/<Title>/cases_perf.json` is an *append-only* stratified spectrum
(`PUBLIC` + `S_SMALL_CORE` / `M_MID_UB` / `L_HBM_TILED` / `X_EXTREME`).  Whenever the
spectrum grows a new bucket, the pinned baseline commit (`ledger.active_board_sha`) has
no paired hardware `profiler` / `exec=` timing for it, so `BenchmarkCalibrator` reports
`unpaired_bucket_count > 0` and `OpsSupervisor.app` shows the orange
“N perf bucket(s) missing baseline pairing → Backfill (~20s)” banner.  Today a human
must notice that banner and click it; nothing keeps the ledger warm.

This module turns that manual button into the **third, lowest, non-preemptible lane**
of the NPU MLFQ:

    tier 2 = Q0  FAST-LANE   (interactive probe, `opsb test --fast`)
    tier 1 = Q1  BATCH-GATE  (full validation, `opsb test`)
    tier 0 = B0  BACKFILL    (this module — baseline bucket pairing only)

State machine (per dedup key `(title, bucket_id, baseline_sha)`)::

    UNPAIRED ──enqueue──▶ QUEUED ──admit──▶ RUNNING ──ok──────────▶ PAIRED
                            ▲                 │
                            │                 ├─no_evidence / error ─▶ COOLDOWN(backoff) ┐
                            │                 ├─die_busy / head_mismatch ─▶ QUEUED(0 cost) ┤
                            │                 └─pid dead (crash) ────────▶ ABANDONED ─────┘
                            └───────────────────────────────────────────────────────────┘

`PAIRED` is terminal: the bucket now has a paired timing in the timings store, so
detection stops reporting it, and the ledger keeps a success record for the key —
“已补过的永不重跑” is enforced *twice* (content-addressed timings store + ledger).

Invariants (proved in `SCHEDULER_DESIGN.md` §6)
----------------------------------------------
1. **Never preempts an operator task.** The remote backfill process registers a
   `tier=0` ticket in `/tmp/opsb-npu-tickets/`, while `tools/npu-test.py` registers
   `tier=2` (Q0) or `tier=1` (Q1).  Ticket admission is lexicographic on
   `(tier, prio, -created)` **descending**, so any waiting operator ticket outranks a
   backfill ticket unconditionally — `prio`/aging can only reorder *inside* a tier.
   On top of that the die itself is a kernel `flock(LOCK_EX)`: B0 only ever attempts
   `LOCK_EX | LOCK_NB` when it observes no operator ticket, so it sdkot even queue
   ahead of an operator in the VFS waitqueue.
2. **Read-only w.r.t. operator artifacts.** No `git checkout`, no `build.sh`, no
   `results/` write, no repo lock: the session is a *Phase-B-only* run against the
   already-built package of the baseline commit, after verifying that the server
   working tree is that commit or a **C++-identical** one (empty `op_host/` +
   `op_kernel/` diff).  Otherwise it aborts **before** touching the die
   (`server_head_mismatch`) instead of checking out / rebuilding the operator tree.
3. **Idempotent + resumable.** The ledger is append-only JSONL
   (`started` → terminal record) guarded by `flock`; a `started` record whose pid is
   dead is `ABANDONED` (retryable), one whose pid is alive is `running` and blocks a
   second acquisition of the same key (single-flight).  Re-running any entry point any
   number of times converges to the same plan.
4. **No daemon, no poller.** Scheduling is *event driven*: the planner is a pure
   function of `(bench-specs, results/, ledger, live NPU probe)`, and the dispatcher is
   invoked only from invocations that already exist (`opsb test` post-release hook,
   `opsb bench backfill`, `python -m scheduler.backfill_status --dispatch`).
"""
from __future__ import annotations

import dataclasses
import datetime
import fcntl
import hashlib
import json
import pathlib
import re
import shlex
import statistics
import subprocess
import time
from typing import Any, Callable

from .estimator import _parse_iso, parse_validation_perf

# --------------------------------------------------------------------------------------
# Lane / ticket constants — shared vocabulary with `npu_scheduler.py` and `tools/npu-test.py`
# --------------------------------------------------------------------------------------
LANE_BACKFILL = "B0"
LANE_Q1 = "Q1"
LANE_Q0 = "Q0"

#: Lexicographic ticket tier.  Higher wins; `prio` only orders *inside* a tier.
TICKET_TIER: dict[str, int] = {
    LANE_BACKFILL: 0,   # backfill: strictly below every operator lane
    LANE_Q1: 1,         # full validation (`opsb test`)
    LANE_Q0: 2,         # fast lane (`opsb test --fast`)  [also matches `Q0*` promotion]
    "Q0*": 2,
}

BACKFILL_TAG = "BACKFILL"
DIE_LOCK_ROOT = "/home/developer/work"
TICKET_DIR = "/tmp/opsb-npu-tickets"

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "auto_dispatch": True,
    # --- auto throttle (bypassed by `force=True`, i.e. the UI button) ---
    "min_interval_sec": 900.0,
    "max_sessions_per_hour": 4,
    "max_buckets_per_session": 32,
    "max_runs_per_session": 6,
    "backoff_sec": 3600.0,
    "max_attempts_per_key": 3,
    # --- session shape ---
    "est_session_sec": 22.0,
    "est_build_sec": 55.0,
    "build_timeout_sec": 300.0,
    "build_cache_days": 7.0,
    "min_free_gb": 5.0,
    "session_timeout_sec": 180.0,
    "stale_inflight_sec": 900.0,
    "auto_max_wait_sec": 0.0,
    "force_max_wait_sec": 45.0,
}


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _now_epoch() -> float:
    return time.time()


def offline_npu_sentinel(reason: str = "offline_requested") -> dict[str, Any]:
    """Fail-closed telemetry stub: the lane may plan, but it may not take the die."""
    return {
        "offline": True,
        "ssh_error": reason,
        "die_locks": [],
        "tasks": [],
        "npu_tickets": [],
        "sampled_at": None,
    }


# --------------------------------------------------------------------------------------
# Ticket ordering — the priority proof, as executable code
# --------------------------------------------------------------------------------------
def ticket_rank(ticket: dict[str, Any]) -> tuple[int, float, float]:
    """Admission rank used by `tools/npu-test.py::am_top_ticket`.

    `(tier, prio, -created)` descending — the *only* comparison in the protocol.
    A `B0` ticket is `(0, …)`; every operator ticket is `(1|2, …)`, hence
    `ticket_rank(operator) > ticket_rank(backfill)` regardless of prio/aging.
    """
    return (
        int(ticket.get("tier", 1)),
        float(ticket.get("prio", 0.0)),
        -float(ticket.get("created", 0.0)),
    )


def operator_tickets_waiting(tickets: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Tickets that would outrank a B0 ticket: any live `tier >= 1` entry."""
    out: list[dict[str, Any]] = []
    for t in tickets or []:
        try:
            if int(t.get("tier", 1)) >= 1:
                out.append(t)
        except (TypeError, ValueError):
            continue
    return out


# --------------------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------------------
@dataclasses.dataclass
class BackfillConfig:
    enabled: bool = True
    auto_dispatch: bool = True
    min_interval_sec: float = 900.0
    max_sessions_per_hour: int = 4
    max_buckets_per_session: int = 32
    max_runs_per_session: int = 6
    backoff_sec: float = 3600.0
    max_attempts_per_key: int = 3
    est_session_sec: float = 22.0
    est_build_sec: float = 55.0
    build_timeout_sec: float = 300.0
    build_cache_days: float = 7.0
    min_free_gb: float = 5.0
    session_timeout_sec: float = 180.0
    stale_inflight_sec: float = 900.0
    auto_max_wait_sec: float = 0.0
    force_max_wait_sec: float = 45.0

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "BackfillConfig":
        valid = {f.name for f in dataclasses.fields(cls)}
        filtered = {k: v for k, v in (data or {}).items() if k in valid and v is not None}
        return cls(**filtered)


# --------------------------------------------------------------------------------------
# Append-only ledger
# --------------------------------------------------------------------------------------
class BackfillLedger:
    """Append-only JSONL ledger of backfill sessions (`started` + one terminal record)."""

    def __init__(self, path: pathlib.Path) -> None:
        self.path = path

    # -- read -------------------------------------------------------------------
    def records(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        rows: list[dict[str, Any]] = []
        try:
            text = self.path.read_text(errors="replace")
        except OSError:
            return []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue  # torn tail line: append-only tolerates it
            if isinstance(obj, dict):
                rows.append(obj)
        return rows

    def sha256(self) -> str:
        try:
            return hashlib.sha256(self.path.read_bytes()).hexdigest()
        except OSError:
            return hashlib.sha256(b"").hexdigest()

    # -- write ------------------------------------------------------------------
    def append(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"v": 1, **record}, ensure_ascii=False, sort_keys=True) + "\n"
        with self.path.open("a", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.write(payload)
                fh.flush()
                try:
                    import os

                    os.fsync(fh.fileno())
                except OSError:
                    pass
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    # -- joins ------------------------------------------------------------------
    def terminal_by_session(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for rec in self.records():
            if rec.get("event") in ("finished", "aborted") and rec.get("session_key"):
                out[str(rec["session_key"])] = rec
        return out

    def started_by_session(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for rec in self.records():
            if rec.get("event") == "started" and rec.get("session_key"):
                out[str(rec["session_key"])] = rec
        return out

    def successful_bucket_keys(self) -> set[str]:
        """`(title, bucket, baseline_sha)` keys with a terminal `ok` measurement."""
        done: set[str] = set()
        for rec in self.records():
            if rec.get("event") != "finished" or rec.get("result") != "ok":
                continue
            for k in rec.get("bucket_keys") or []:
                done.add(str(k))
        return done

    def attempts_by_bucket_key(self) -> dict[str, list[float]]:
        """Per-key attempt timestamps, for bounded retries + exponential backoff."""
        out: dict[str, list[float]] = {}
        for rec in self.records():
            if rec.get("event") not in ("finished", "aborted"):
                continue
            ts = float(rec.get("ended_epoch") or rec.get("started_epoch") or 0.0)
            for k in rec.get("bucket_keys") or []:
                out.setdefault(str(k), []).append(ts)
        for v in out.values():
            v.sort()
        return out


# --------------------------------------------------------------------------------------
# Session planning helpers (pure functions — unit tested without any NPU access)
# --------------------------------------------------------------------------------------
def bucket_key(title: str, bucket: str, baseline_sha: str | None) -> str:
    """Dedup key: `(operator, bucket, baseline_sha)`.  A new baseline re-opens pairing."""
    return f"{title}|{bucket}|{(baseline_sha or '')[:12]}"


def session_key_for(title: str, baseline_sha: str | None, buckets: list[str]) -> str:
    """Content-addressed session id: same operator + same bucket set ⇒ same id."""
    blob = f"{title}|{(baseline_sha or '')[:12]}|{','.join(sorted(buckets))}"
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


_PERF_FLAG_PRIORITY: tuple[str, ...] = ("--cases", "--filter", "--case-filter", "--case")

#: Repos with `validation/kperf.py` emit hardware `profiler` `KPERF <case> … median=…us`
#: rows the estimator parses directly; that is the exact modality the baseline logs used,
#: so kperf always outranks `perf.py` (whose `exec p50=` table is not a paired timing).
KPERF_TOOL = "kperf"
PERF_TOOL = "perf"


def build_perf_plan(perf_source: str, buckets: list[str], max_runs: int = 6) -> dict[str, Any]:
    """Derive the `validation/perf.py` invocation for a bucket set (pure, offline).

    Case-selection surfaces differ per operator repo:
      * `--cases <a,b,c>`  → one run with the comma list (best: one session, one warmup)
      * `--filter <k>`     → substring filter only, so run the *whole* spectrum once
      * `--case <one>`     → one run per bucket (bounded by `max_runs`)
    """
    flag = next((f for f in _PERF_FLAG_PRIORITY if f"'{f}'" in perf_source or f'"{f}"' in perf_source), None)
    if not buckets:
        return {"flag": flag, "mode": "empty", "runs": []}
    if flag == "--cases":
        return {"flag": flag, "mode": "cases_batch", "runs": [["--cases", ",".join(buckets)]]}
    if flag in ("--filter", "--case-filter"):
        return {"flag": flag, "mode": "full_spectrum", "runs": [[]]}
    if flag == "--case":
        runs = [["--case", b] for b in buckets[: max(1, max_runs)]]
        return {"flag": flag, "mode": "per_bucket", "runs": runs}
    return {"flag": None, "mode": "full_spectrum", "runs": [[]]}


def build_measurement_plan(
    repo_dir: pathlib.Path,
    buckets: list[str],
    max_runs: int = 6,
) -> dict[str, Any]:
    """Choose `kperf.py` vs `perf.py` and the case-selection flags for a bucket set.

    kperf (hardware Task Duration) wins when present because the paired baseline timing
    must be the same physical quantity the calibration was fitted on.
    """
    if not buckets:
        return {"tool": PERF_TOOL, "flag": None, "mode": "empty", "runs": [], "supports_device": False}
    kperf = repo_dir / "validation" / "kperf.py"
    if kperf.is_file():
        src = kperf.read_text(errors="replace")
        flag = next((f for f in _PERF_FLAG_PRIORITY if f"'{f}'" in src or f'"{f}"' in src), None)
        base = {"tool": KPERF_TOOL, "supports_device": "--device" in src, "uses_root": "--root" in src}
        if flag in ("--cases", "--case"):
            return {**base, "flag": flag, "mode": "kperf_batch", "runs": [[flag, ",".join(buckets)]]}
        if flag == "--filter":
            return {**base, "flag": flag, "mode": "kperf_filter", "runs": [[flag, ",".join(buckets)]]}
        return {**base, "flag": None, "mode": "kperf_full", "runs": [[]]}
    perf = repo_dir / "validation" / "perf.py"
    src = perf.read_text(errors="replace") if perf.is_file() else ""
    plan = build_perf_plan(src, buckets, max_runs=max_runs)
    return {"tool": PERF_TOOL, **plan, "supports_device": "--device" in src, "uses_root": "--root" in src}


def coalesce_sessions(
    unpaired: list[dict[str, Any]],
    *,
    max_buckets: int = 32,
    skip_bucket_keys: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Group unpaired buckets into **one session per operator** (never one per bucket).

    Buckets are ordered by `headroom_pts` descending then id (deterministic), and
    truncated to `max_buckets`; operators are ordered by total headroom descending.
    """
    skip = skip_bucket_keys or set()
    by_title: dict[str, list[dict[str, Any]]] = {}
    for row in unpaired:
        if str(row.get("key")) in skip:
            continue
        by_title.setdefault(str(row["title"]), []).append(row)

    sessions: list[dict[str, Any]] = []
    for title, rows in by_title.items():
        rows.sort(key=lambda r: (-float(r.get("headroom_pts") or 0.0), str(r.get("bucket"))))
        chosen = rows[:max_buckets]
        baseline_sha = chosen[0].get("baseline_sha")
        buckets = [str(r["bucket"]) for r in chosen]
        sessions.append(
            {
                "title": title,
                "baseline_sha": baseline_sha,
                "baseline_short": (baseline_sha or "")[:7] or None,
                "buckets": buckets,
                "bucket_keys": [bucket_key(title, b, baseline_sha) for b in buckets],
                "bucket_count": len(buckets),
                "headroom_pts": round(sum(float(r.get("headroom_pts") or 0.0) for r in chosen), 2),
                "deferred_buckets": [str(r["bucket"]) for r in rows[max_buckets:]],
                "session_key": session_key_for(title, baseline_sha, buckets),
            }
        )
    sessions.sort(key=lambda s: (-float(s["headroom_pts"]), s["title"]))
    return sessions


# --------------------------------------------------------------------------------------
# Remote Phase-B-only session (tier-0 ticket; verifies HEAD *before* touching the die)
# --------------------------------------------------------------------------------------
REMOTE_BACKFILL = r'''
import datetime, fcntl, glob, json, os, pathlib, re, shutil, subprocess, sys, time

cfg = json.load(sys.stdin)
title = cfg['title']; sha = cfg['sha']; buckets = list(cfg.get('buckets') or [])
wait_sec = float(cfg.get('wait_sec') or 0.0)
build_timeout = float(cfg.get('build_timeout_sec') or 300.0)
perf_timeout = float(cfg.get('perf_timeout_sec') or 180.0)
cache_days = float(cfg.get('cache_days') or 7.0)
min_free_gb = float(cfg.get('min_free_gb') or 5.0)
force_rebuild = bool(cfg.get('force_rebuild'))
wt_root = pathlib.Path(cfg.get('worktree_root') or '/home/developer/work/.backfill')
repo = pathlib.Path(cfg.get('repo') or ('/srv/work/' + title))
wt = wt_root / ('%s-%s' % (title, sha[:8]))
marker = wt / '.opsb_backfill_build.json'
opsb_home = pathlib.Path.home() / '.opsb'
outdir = opsb_home / 'backfill' / title / sha
run_id = 'bf-%d-%s' % (int(time.time()), re.sub(r'[^A-Za-z0-9]+', '', title)[:12])
log_path = outdir / (run_id + '.log')
res = {'ok': False, 'acquired': False, 'title': title, 'sha': sha, 'run_id': run_id,
       'log': str(log_path), 'lane': 'B0', 'tier': 0, 'reason': None, 'runs': [],
       'lock_wait_sec': 0.0, 'duration_sec': 0.0, 'die': None,
       'worktree': str(wt), 'wt_method': None, 'wt_reused': False,
       'built': False, 'build_sec': 0.0, 'build_cached': False, 'perf_sec': 0.0,
       'phase_a_sec': 0.0}


def emit(obj):
    print('BACKFILL_RESULT ' + json.dumps(obj), flush=True)


def sh(argv, cwd=None, timeout=60.0, env=None, shell=False):
    try:
        p = subprocess.run(argv, cwd=cwd, env=env, shell=shell, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or ''), (p.stderr or '')
    except subprocess.TimeoutExpired:
        return 124, '', 'timeout'
    except Exception as exc:
        return 125, '', str(exc)


def git(*args, timeout=120.0):
    return sh(['git', '-C', str(repo)] + list(args), timeout=timeout)


def free_gb(path):
    try:
        st = os.statvfs(str(path if pathlib.Path(path).exists() else pathlib.Path(path).parent))
        return st.f_bavail * st.f_frsize / (1024.0 ** 3)
    except Exception:
        return None


# ---------------- PRE-FLIGHT: zero lock, zero side effects on the operator tree ----------
if not (repo / '.git').exists() and not (repo / 'build.sh').is_file():
    res.update(reason='repo_missing'); emit(res); sys.exit(0)
rc, _, _ = git('cat-file', '-e', sha + '^{commit}', timeout=30.0)
if rc != 0:
    res.update(reason='baseline_sha_missing'); emit(res); sys.exit(0)
avail = free_gb(wt_root if wt_root.exists() else '/home/developer/work')
res['disk_free_gb'] = round(avail, 1) if avail is not None else None
if avail is not None and avail < min_free_gb:
    res.update(reason='disk_low'); emit(res); sys.exit(0)

# Reap expired build caches (bounded, never touches the operator worktree itself)
wt_root.mkdir(parents=True, exist_ok=True)
now_ep = time.time()
for old in sorted(wt_root.iterdir()):
    try:
        age_d = (now_ep - old.stat().st_mtime) / 86400.0
    except Exception:
        continue
    if old == wt or age_d <= cache_days:
        continue
    try:
        sh(['git', '-C', str(repo), 'worktree', 'remove', '--force', str(old)], timeout=120.0)
    except Exception:
        pass
    if old.exists():
        shutil.rmtree(old, ignore_errors=True)
    res.setdefault('reaped', []).append(old.name)
git('worktree', 'prune', timeout=60.0)


def marker_read():
    try:
        m = json.loads(marker.read_text())
        return m if isinstance(m, dict) else None
    except Exception:
        return None


def pkg_present():
    return bool(glob.glob(str(wt / 'build/packages/vendors/*_custom/op_api/lib/libcust_opapi.so')))


def materialize():
    """Detached `git worktree` of the baseline commit; `git archive` fallback. Never the repo tree."""
    if wt.exists():
        m = marker_read()
        if m and m.get('sha') == sha:
            res['wt_reused'] = True
            res['wt_method'] = m.get('wt_method') or 'worktree'
            return True
        sh(['git', '-C', str(repo), 'worktree', 'remove', '--force', str(wt)], timeout=120.0)
        if wt.exists():
            shutil.rmtree(wt, ignore_errors=True)
    rc, _o, err = git('worktree', 'add', '--detach', str(wt), sha, timeout=180.0)
    if rc == 0 and wt.exists():
        res['wt_method'] = 'worktree'
        return True
    res['worktree_error'] = (err or '')[-300:]
    # Fallback: materialize the same commit without needing worktree support (build.sh is
    # git-independent, so a detached export is buildable and measurable all the same).
    try:
        wt.mkdir(parents=True, exist_ok=True)
        tar = subprocess.Popen(['git', '-C', str(repo), 'archive', '--format=tar', sha],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        ext = subprocess.Popen(['tar', '-x', '-C', str(wt)], stdin=tar.stdout,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        tar.stdout.close()
        ext.communicate(timeout=180.0)
        tar.wait(timeout=60.0)
        if ext.returncode == 0 and (wt / 'build.sh').is_file():
            res['wt_method'] = 'git_archive'
            return True
    except Exception as exc:
        res['archive_error'] = str(exc)[:200]
    shutil.rmtree(wt, ignore_errors=True)
    res.update(reason='worktree_unavailable', blocked_by='worktree_unavailable')
    return False


def cleanup_worktree():
    if cache_days <= 0:
        sh(['git', '-C', str(repo), 'worktree', 'remove', '--force', str(wt)], timeout=120.0)
        if wt.exists():
            shutil.rmtree(wt, ignore_errors=True)
        git('worktree', 'prune', timeout=60.0)


def drop_worktree():
    sh(['git', '-C', str(repo), 'worktree', 'remove', '--force', str(wt)], timeout=120.0)
    if wt.exists():
        shutil.rmtree(wt, ignore_errors=True)
    git('worktree', 'prune', timeout=60.0)


# ---------------- PHASE A (0.0s die lock): build the baseline commit in the worktree -------
phase_a_t0 = time.time()
if not materialize():
    emit(res); sys.exit(0)
res['wt_sha'] = (sh(['git', '-C', str(wt), 'rev-parse', 'HEAD'], timeout=30.0)[1] or '').strip()[:40] or None

env = dict(os.environ)
env['GIT_TERMINAL_PROMPT'] = '0'
env['OPSB_BACKFILL'] = '1'
env['OPSB_BACKFILL_BUCKETS'] = ','.join(buckets)
set_env = '/opt/toolchain/latest/set_env.sh'
soc = None
try:
    with open(set_env) as fh:
        pass
    rc, out, _err = sh(['bash', '-c', 'source %s >/dev/null 2>&1; python3 - <<SOC\n'
                        'import ctypes\n'
                        "acl = ctypes.CDLL('libruntime.so')\n"
                        'assert acl.aclInit(None) == 0\n'
                        'acl.aclrtGetSocName.restype = ctypes.c_char_p\n'
                        'print(acl.aclrtGetSocName().decode().lower())\n'
                        'assert acl.aclFinalize() == 0\n'
                        'SOC' % set_env], timeout=60.0)
    soc = (out or '').strip().splitlines()[-1].strip() if rc == 0 and (out or '').strip() else None
except Exception:
    soc = None
res['runtime_soc'] = soc
target = 'soc-b' if (soc or '').startswith('soc-b') else 'soc-a'

# The Accel toolkit (`profiler`, `libruntime.so`, LD_LIBRARY_PATH) only exists inside the
# sourced environment; the measuring harness is a direct child, so import that env once.
try:
    rc, out, _err = sh(['bash', '-c',
                        'source %s >/dev/null 2>&1; python3 -c '
                        '"import json,os;print(json.dumps(dict(os.environ)))"' % set_env], timeout=60.0)
    if rc == 0 and (out or '').strip():
        env.update({str(k): str(v) for k, v in json.loads(out.strip().splitlines()[-1]).items()})
        res['toolkit_env'] = True
except Exception as exc:
    res['toolkit_env'] = 'error: %s' % str(exc)[:120]

m = marker_read()
cache_valid = bool(
    m and m.get('sha') == sha and m.get('rc') == 0 and not force_rebuild
    and pkg_present() and (now_ep - float(m.get('at') or 0.0)) / 86400.0 <= cache_days
)
if cache_valid:
    res.update(built=False, build_cached=True, build_sec=round(float(m.get('build_sec') or 0.0), 1))
else:
    b_t0 = time.time()
    build_cmd = (
        'set -o pipefail; source %s >/dev/null 2>&1; '
        'sed "s/COMPUTE_UNIT=\\"soc-a\\"/COMPUTE_UNIT=\\"%s\\"/" build.sh | bash -s -- -j4'
        % (set_env, target)
    )
    rc, out, err = sh(['bash', '-c', build_cmd], cwd=str(wt), timeout=build_timeout)
    b_sec = round(time.time() - b_t0, 1)
    res.update(built=True, build_sec=b_sec, build_rc=rc)
    try:
        (wt / 'build.log').write_text((out or '')[-200000:] + '\n' + (err or '')[-20000:])
    except Exception:
        pass
    if rc != 0 or not pkg_present():
        res.update(reason='build_failed', build_tail=((err or out or '')[-400:]))
        drop_worktree()  # never poison the cache with a half-built tree
        res['phase_a_sec'] = round(time.time() - phase_a_t0, 1)
        emit(res); sys.exit(0)
    try:
        marker.write_text(json.dumps({'sha': sha, 'soc': soc, 'target': target, 'rc': 0,
                                      'build_sec': b_sec, 'at': time.time(),
                                      'wt_method': res.get('wt_method')}, indent=1))
    except Exception:
        pass
res['phase_a_sec'] = round(time.time() - phase_a_t0, 1)

# ---------------- PHASE B (die lock): measure the missing buckets --------------------------
outdir.mkdir(parents=True, exist_ok=True)
tool = cfg.get('tool') or 'perf'          # 'kperf' | 'perf'
plan_runs = cfg.get('perf_runs')
plan_mode = cfg.get('perf_mode') or 'full_spectrum'
if not isinstance(plan_runs, list) or not plan_runs:
    plan_runs, plan_mode = ([[]], 'full_spectrum')
tool_name = '%s.py' % tool
uses_root = bool(cfg.get('uses_root'))
# The bucket shapes may postdate the baseline commit, so its own `validation/` harness can
# lack them.  Run the *current* harness against the *baseline* build via `--root` (the same
# kernel, today's case table); repositories without `--root` get the current harness copied
# into the disposable worktree instead.  The operator tree is never modified either way.
src_tool = repo / 'validation' / tool_name
wt_tool = wt / 'validation' / tool_name
tool_path = None
res['harness_source'] = 'worktree'


def ids_known(path, ids):
    try:
        txt = path.read_text(errors='replace')
    except Exception:
        return False
    return all(i in txt for i in ids) if ids else True


if uses_root and src_tool.is_file():
    tool_path = src_tool
    res['harness_source'] = 'repo_with_root'
else:
    # The baseline harness may predate the bucket shapes (e.g. kperf `k_8x4096_*` was added
    # after the pinned commit).  Inject today's harness into the disposable worktree so the
    # *baseline build* is measured with a case table that actually contains the ids.
    if src_tool.is_file() and not ids_known(wt_tool, buckets) and ids_known(src_tool, buckets):
        try:
            wt_tool.parent.mkdir(parents=True, exist_ok=True)
            wt_tool.write_bytes(src_tool.read_bytes())
            res['harness_source'] = 'injected_current'
        except Exception:
            res['harness_inject_error'] = 'copy failed'
    tool_path = wt_tool if wt_tool.is_file() else src_tool
if tool_path is None or not tool_path.is_file():
    res.update(reason='no_measurement_tool', tool=str(tool_path))
    cleanup_worktree()
    emit(res); sys.exit(0)
supports_device = '--device' in tool_path.read_text(errors='replace')

# B0 ticket: tier 0 => strictly below Q1(1) / Q0(2); the die keeps its operator priority.
ticket_dir = pathlib.Path('/tmp/opsb-npu-tickets')
ticket_dir.mkdir(parents=True, exist_ok=True)
my_pid = os.getpid()
ticket_path = ticket_dir / ('%d.json' % my_pid)
created = time.time()


def write_ticket():
    try:
        ticket_path.write_text(json.dumps({
            'pid': my_pid, 'title': title, 'sha': sha, 'tier': 0, 'prio': 0.0,
            'fast': False, 'lane': 'B0', 'kind': 'BACKFILL', 'created': created,
        }))
    except Exception:
        pass


def am_top_ticket():
    write_ticket()
    try:
        mine = json.loads(ticket_path.read_text())
    except Exception:
        return True
    for tf in ticket_dir.glob('*.json'):
        try:
            other = json.loads(tf.read_text())
            opid = int(other['pid'])
            if opid == my_pid:
                continue
            if not pathlib.Path('/proc/%d' % opid).exists():
                tf.unlink(missing_ok=True)
                continue
            if (int(other.get('tier', 1)), float(other.get('prio', 0.0)), -float(other.get('created', 0.0))) > \
               (int(mine.get('tier', 0)), float(mine.get('prio', 0.0)), -float(mine.get('created', 0.0))):
                return False
        except Exception:
            continue
    return True


devices = int(os.environ.get('TASK_DEVICES', '1'))
slot = None
deadline = time.time() + max(0.0, wait_sec)
wait_t0 = time.time()
while slot is None:
    if am_top_ticket():
        for index in range(devices):
            try:
                handle = (pathlib.Path('/home/developer/work') / ('bench-device%d.lock' % index)).open('a')
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                slot = (index, handle)
                break
            except BlockingIOError:
                try:
                    handle.close()
                except Exception:
                    pass
            except Exception:
                break
    if slot is None and time.time() >= deadline:
        break
    if slot is None:
        time.sleep(2.0)

if slot is None:
    ticket_path.unlink(missing_ok=True)
    cleanup_worktree()
    res.update(reason='die_busy', lock_wait_sec=round(time.time() - wait_t0, 2))
    res['phase_a_sec'] = round(time.time() - phase_a_t0, 1)
    emit(res); sys.exit(0)

die_idx, die_handle = slot
die_t0 = time.time()
res.update(acquired=True, die=die_idx, lock_wait_sec=round(die_t0 - wait_t0, 2))
sess_file = opsb_home / 'die_session.json'
try:
    sess_file.write_text(json.dumps({
        'die': die_idx, 'pid': my_pid, 'title': title, 'sha': sha, 'mode': 'B0',
        'phase': 'backfill_perf', 'substage': 'backfill_perf',
        'started_epoch': round(die_t0, 3),
        'started_ts': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }))
except Exception:
    pass

env['VISIBLE_DEVICES'] = str(die_idx)
TIMING_PREFIX = ('MS_PROF ', 'KPERF ', 'KPERF_JSON ', 'KERNEL_JSON ', 'KERNEL_PERF ',
                 'PERF ', 'PERF_JSON ', 'PROF_SUMMARY ', 'SPECTRUM_SUMMARY ', 'RUNTIME_SOC=')
timing_lines = []
out_tail = ''
rc_last = None
try:
    with log_path.open('w', encoding='utf-8') as log_f:
        for run_args in plan_runs:
            argv = ['python3', str(tool_path)] + [str(a) for a in run_args]
            if uses_root and str(tool_path).startswith(str(repo)):
                argv += ['--root', str(wt)]
            if supports_device:
                # `VISIBLE_DEVICES` already masks us onto this die, so the
                # logical index the tool must open is always 0.
                argv += ['--device', '0']
            log_f.write('### ' + ' '.join(argv) + '\n'); log_f.flush()
            p = subprocess.Popen(argv, cwd=str(wt), env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True)
            try:
                for line in p.stdout:
                    log_f.write(line); log_f.flush()
                    out_tail = (out_tail + line)[-2000:]
                    if line.strip().startswith(TIMING_PREFIX) or 'exec=' in line:
                        timing_lines.append(line.rstrip('\n'))
                rc_last = p.wait(timeout=max(5.0, perf_timeout))
            except subprocess.TimeoutExpired:
                p.kill(); rc_last = -9
                log_f.write('### TIMEOUT after %.1fs\n' % perf_timeout); log_f.flush()
            res['runs'].append({'argv': argv[1:], 'rc': rc_last})
            if rc_last != 0:
                break
finally:
    res['perf_sec'] = round(time.time() - die_t0, 2)
    try:
        sess_file.unlink(missing_ok=True)
    except Exception:
        pass
    try:
        fcntl.flock(die_handle, fcntl.LOCK_UN)
        die_handle.close()
    except Exception:
        pass
    ticket_path.unlink(missing_ok=True)

res['timing_lines'] = timing_lines
res['ok'] = bool(timing_lines)
if not res['ok']:
    if rc_last != 0 and 'no case selected' in (out_tail or ''):
        res['reason'] = 'case_not_in_harness'
    else:
        res['reason'] = 'no_timing_evidence' if (rc_last == 0) else 'measurement_rc_nonzero'
    # a validly built tree stays as the build cache even when the measurement failed;
    # a tree without a green marker (or a failed build path) is dropped above already
    _m = marker_read()
    if not (_m and _m.get('sha') == sha and _m.get('rc') == 0 and pkg_present()):
        drop_worktree()
    else:
        cleanup_worktree()
else:
    cleanup_worktree()
res['duration_sec'] = round(res.get('phase_a_sec', 0.0) + res.get('perf_sec', 0.0), 1)

event = {
    'ts': round(time.time(), 3), 'event': 'finished', 'lane': 'B0', 'tier': 0,
    'title': title, 'sha': sha, 'run_id': run_id, 'die': die_idx,
    'tool': tool, 'wt_method': res.get('wt_method'), 'built': res.get('built'),
    'build_sec': res.get('build_sec'), 'perf_sec': res.get('perf_sec'),
    'lock_wait_sec': res.get('lock_wait_sec'), 'rc': rc_last,
    'timing_lines': len(timing_lines), 'log': str(log_path),
}
try:
    with (opsb_home / 'backfill_sessions.jsonl').open('a', encoding='utf-8') as lf:
        lf.write(json.dumps(event) + '\n')
except Exception:
    pass
emit(res)
'''


# --------------------------------------------------------------------------------------
# Scheduler
# --------------------------------------------------------------------------------------
class BackfillScheduler:
    """B0 lane: detect unpaired buckets, coalesce sessions, admit, dispatch, ledger."""

    def __init__(
        self,
        workspace: pathlib.Path,
        state_dir: pathlib.Path | None = None,
        estimator: Any | None = None,
        calibrator: Any | None = None,
        npu_scheduler: Any | None = None,
        npu_host: str = "npu",
        ledger_path: pathlib.Path | None = None,
        clock: Callable[[], float] = _now_epoch,
        runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.workspace = pathlib.Path(workspace).resolve()
        self.state_dir = (pathlib.Path(state_dir) if state_dir else self.workspace / "ops-supervisor" / "scheduler" / ".state").resolve()
        self.npu_host = npu_host
        self.clock = clock
        self._estimator = estimator
        self._calibrator = calibrator
        self._npu_scheduler = npu_scheduler
        self.ledger = BackfillLedger(ledger_path or (self.state_dir / "backfill_ledger.jsonl"))
        self.timings_path = self.state_dir / "backfill_timings.json"
        self.config_path = self.state_dir / "backfill_config.json"
        self._timings_doc: dict[str, Any] | None = None
        self._timings_stamp: tuple[int, int] | None = None
        self.runner = runner or self._run_remote_dispatch

    # -- lazy collaborators (avoid heavy imports when only planning) --------------
    @property
    def estimator(self) -> Any:
        if self._estimator is None:
            from .estimator import ScoreEstimator

            self._estimator = ScoreEstimator(self.workspace)
        return self._estimator

    @property
    def calibrator(self) -> Any:
        if self._calibrator is None:
            from .calibration import BenchmarkCalibrator

            self._calibrator = BenchmarkCalibrator(self.workspace, estimator=self.estimator)
        return self._calibrator

    @property
    def npu_scheduler(self) -> Any:
        if self._npu_scheduler is None:
            from .npu_scheduler import NpuTaskScheduler

            self._npu_scheduler = NpuTaskScheduler(self.workspace, npu_host=self.npu_host)
        return self._npu_scheduler

    # -- config ------------------------------------------------------------------
    def config(self) -> BackfillConfig:
        if self.config_path.is_file():
            try:
                return BackfillConfig.from_dict(json.loads(self.config_path.read_text()))
            except (ValueError, TypeError):
                pass
        return BackfillConfig()

    def save_config(self, **kwargs: Any) -> BackfillConfig:
        cfg = self.config()
        for k, v in kwargs.items():
            if v is not None and hasattr(cfg, k):
                setattr(cfg, k, v)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(cfg.to_dict(), indent=2) + "\n")
        return cfg

    # -- timings store (consumed by `calibration._extract_bucket_timings`) -------
    def _load_timings_doc(self) -> dict[str, Any]:
        try:
            st = self.timings_path.stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        if stamp is not None and self._timings_stamp == stamp and self._timings_doc is not None:
            return self._timings_doc
        if not self.timings_path.is_file():
            self._timings_doc, self._timings_stamp = {"version": 1, "entries": {}}, None
            return self._timings_doc
        try:
            doc = json.loads(self.timings_path.read_text())
        except ValueError:
            doc = {"version": 1, "entries": {}}
        if not isinstance(doc.get("entries"), dict):
            doc["entries"] = {}
        self._timings_doc, self._timings_stamp = doc, stamp
        return doc

    def store_timings(
        self,
        title: str,
        sha: str,
        cases: dict[str, float],
        *,
        runtime_soc: str | None = None,
        modality: str = "profiler",
        source: str = "backfill",
    ) -> dict[str, Any]:
        """Merge measured bucket timings into `<state>/backfill_timings.json` (idempotent)."""
        doc = self._load_timings_doc()
        key = f"{title}|{sha}"
        entry = doc["entries"].get(key) or {"title": title, "sha": sha, "cases": {}}
        merged = dict(entry.get("cases") or {})
        added = 0
        for bid, us in cases.items():
            if not isinstance(us, (int, float)) or us <= 0:
                continue
            if bid not in merged:
                added += 1
            merged[bid] = round(float(us), 4)
        entry.update(
            {
                "title": title,
                "sha": sha,
                "cases": merged,
                "source": source,
                "modality": modality,
                "runtime_soc": runtime_soc or entry.get("runtime_soc"),
                "updated_at": _utc_now(),
            }
        )
        doc["entries"][key] = entry
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.timings_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
        tmp.replace(self.timings_path)
        return {"added": added, "total": len(merged)}

    def timings_for(self, title: str, sha: str | None) -> dict[str, Any] | None:
        """Return `{cases: {bucket: us}, runtime_soc}` for a pinned baseline, else `None`."""
        if not sha:
            return None
        entry = (self._load_timings_doc().get("entries") or {}).get(f"{title}|{sha}")
        if not entry or not entry.get("cases"):
            return None
        return {
            "cases": dict(entry["cases"]),
            "runtime_soc": entry.get("runtime_soc"),
            "modality": entry.get("modality") or "profiler",
        }

    # -- detection ---------------------------------------------------------------
    def scan_unpaired(self) -> list[dict[str, Any]]:
        """Authoritative unpaired-bucket census across all operators (calibration code path)."""
        rows: list[dict[str, Any]] = []
        for title in sorted(self.estimator.problems_meta):
            try:
                census = self.calibrator.unpaired_buckets(title)
            except Exception:
                continue
            headroom_by_bucket: dict[str, float] = {}
            try:
                cal = self.calibrator.load_or_calibrate(title)
                for b in cal.get("buckets") or []:
                    headroom_by_bucket[str(b.get("id"))] = float(b.get("headroom_pts") or 0.0)
            except Exception:
                pass
            for b in census.get("unpaired_details") or []:
                bid = str(b["id"])
                rows.append(
                    {
                        "title": title,
                        "bucket": bid,
                        "stratum": b.get("stratum"),
                        "dtype": b.get("dtype"),
                        "baseline_sha": census.get("baseline_sha"),
                        "baseline_short": census.get("baseline_short"),
                        "modality": census.get("modality"),
                        "runtime_soc": census.get("runtime_soc"),
                        "description": b.get("description") or "",
                        "headroom_pts": round(headroom_by_bucket.get(bid, 0.0), 2),
                        "key": bucket_key(title, bid, census.get("baseline_sha")),
                    }
                )
        rows.sort(key=lambda r: (r["title"], -float(r["headroom_pts"]), r["bucket"]))
        return rows

    # -- planning ----------------------------------------------------------------
    def _blocked_reason_for(
        self,
        key: str,
        att: dict[str, list[float]],
        cfg: BackfillConfig,
        now: float,
        force: bool = False,
    ) -> str | None:
        hist = att.get(key) or []
        if key in self._deterministic_failures():
            return "case_not_in_harness"
        if len(hist) >= cfg.max_attempts_per_key:
            return "max_attempts_exhausted"
        if hist and not force:  # `force` (manual “立即补测”) bypasses the auto backoff too
            gap = now - hist[-1]
            if gap < cfg.backoff_sec:
                return "backoff"
        return None

    def _deterministic_failures(self) -> set[str]:
        """Keys whose failure sdkot change by retrying (spec/harness case-table mismatch)."""
        out: set[str] = set()
        for rec in self.ledger.records():
            if rec.get("event") not in ("finished", "aborted"):
                continue
            if rec.get("result") == "case_not_in_harness":
                for k in rec.get("bucket_keys") or []:
                    out.add(str(k))
            # harness ran to completion but emitted no row for this id -> the spec names a
            # case the operator does not implement; retrying would just rebuild for nothing.
            for k in rec.get("unmeasured_keys") or []:
                out.add(str(k))
        return out

    def unmeasurable(self) -> list[dict[str, Any]]:
        """Buckets blocked by a deterministic mismatch rather than by scheduling."""
        agg: dict[str, dict[str, Any]] = {}
        for rec in self.ledger.records():
            if rec.get("event") not in ("finished", "aborted"):
                continue
            reason = None
            ids: list[str] = []
            if rec.get("result") == "case_not_in_harness":
                reason, ids = "case_not_in_harness", [str(b) for b in rec.get("buckets") or []]
            elif rec.get("unmeasured_buckets"):
                reason, ids = "case_not_in_harness", [str(b) for b in rec["unmeasured_buckets"]]
            if not reason:
                continue
            row = agg.setdefault(
                str(rec.get("title")),
                {"title": rec.get("title"), "reason": reason,
                 "baseline_short": (rec.get("baseline_sha") or "")[:7], "buckets": []},
            )
            for b in ids:
                if b not in row["buckets"]:
                    row["buckets"].append(b)
        return list(agg.values())

    def plan(self, now: float | None = None, force: bool = False) -> list[dict[str, Any]]:
        """Unpaired → coalesced sessions, with attempt/backoff bookkeeping attached."""
        t_now = self.clock() if now is None else now
        cfg = self.config()
        att = self.ledger.attempts_by_bucket_key()
        done = self.ledger.successful_bucket_keys()
        unpaired = self.scan_unpaired()

        ready: list[dict[str, Any]] = []
        blocked: dict[str, list[str]] = {}
        for row in unpaired:
            key = str(row["key"])
            if key in done:
                continue  # already paired once — never re-run
            reason = self._blocked_reason_for(key, att, cfg, t_now, force=force)
            if reason:
                blocked.setdefault(reason, []).append(key)
                continue
            ready.append(row)

        sessions = coalesce_sessions(ready, max_buckets=cfg.max_buckets_per_session)
        last_fail: dict[str, dict[str, Any]] = {}
        for rec in self.ledger.records():
            if rec.get("event") in ("finished", "aborted") and rec.get("result") != "ok" and rec.get("session_key"):
                last_fail[str(rec["session_key"])] = {
                    "result": rec.get("result"),
                    "at": _iso_from_epoch(rec.get("ended_epoch")),
                }
        out: list[dict[str, Any]] = []
        for s in sessions:
            first_seen = min((att.get(k, [t_now])[0] for k in s["bucket_keys"]), default=t_now)
            attempts = {k: len(att.get(k) or []) for k in s["bucket_keys"]}
            out.append(
                {
                    **s,
                    "job_id": s["session_key"],
                    "lane": LANE_BACKFILL,
                    "tier": TICKET_TIER[LANE_BACKFILL],
                    "est_sec": round(self.est_session_sec(), 1),
                    "est_build_sec": round(0.0 if self.build_cache_hit(s) else cfg.est_build_sec, 1),
                    "budget_sec": round(self.est_session_sec() + (0.0 if self.build_cache_hit(s) else cfg.est_build_sec), 1),
                    "attempts": attempts,
                    "attempts_total": max(attempts.values()) if attempts else 0,
                    "last_failure": last_fail.get(s["session_key"]),
                    "first_seen_epoch": round(first_seen, 3),
                    "waiting_sec": round(max(0.0, t_now - first_seen), 1),
                }
            )
        return out

    def est_session_sec(self) -> float:
        """Learned median lock/wall time of successful sessions (fallback: config)."""
        cfg = self.config()
        samples: list[float] = []
        for rec in self.ledger.records():
            if rec.get("event") == "finished" and rec.get("result") == "ok":
                d = rec.get("duration_sec")
                if isinstance(d, (int, float)) and 0.5 <= float(d) <= 600.0:
                    samples.append(float(d))
        if len(samples) >= 2:
            return round(float(statistics.median(samples)), 1)
        return float(cfg.est_session_sec or 22.0)

    # -- admission ---------------------------------------------------------------
    def admission(self, force: bool = False, npu: dict[str, Any] | None = None) -> dict[str, Any]:
        """May the B0 lane take the die *right now*?  Returns the proof, not just a bool."""
        cfg = self.config()
        t_now = self.clock()
        reasons: list[str] = []
        details: dict[str, Any] = {}

        if npu is None:
            try:
                npu = self.npu_scheduler.inspect_live_queue()
            except Exception as exc:  # telemetry failure ⇒ fail closed
                npu = {"offline": True, "ssh_error": str(exc)[:120], "die_locks": [], "tasks": []}
        details["npu_offline"] = bool(npu.get("offline"))
        details["ssh_error"] = npu.get("ssh_error")
        details["sampled_at"] = npu.get("sampled_at")

        online = (not bool(npu.get("offline"))) and not npu.get("ssh_error")
        if not online:
            reasons.append("npu_telemetry_offline")
        else:
            held = [l for l in (npu.get("die_locks") or []) if str(l.get("state", "")).upper() == "HELD"]
            details["die_locks"] = npu.get("die_locks") or []
            if held:
                reasons.append("die_lock_held")
            tickets = npu.get("npu_tickets")
            waiting = operator_tickets_waiting(tickets)
            details["operator_tickets_waiting"] = len(waiting)
            details["operator_ticket_titles"] = [t.get("title") for t in waiting[:6]]
            if waiting:
                reasons.append("operator_ticket_waiting")
            busy_tasks = [
                t
                for t in (npu.get("tasks") or [])
                if str(t.get("tag")) in ("PROFILER", "PRECISION", "FAST-LANE", "STRESS", "BUILD")
            ]
            details["operator_sessions_active"] = len(busy_tasks)
            if busy_tasks:
                reasons.append("operator_session_active")

        # throttle (bypassed by force = the UI button “立即补测”)
        inflight = self.inflight_running()
        details["backfill_running"] = bool(inflight)
        if inflight:
            reasons.append("backfill_inflight")
        if not force:
            if not cfg.enabled:
                reasons.append("lane_disabled")
            last_start = max(
                [float(r.get("started_epoch") or 0.0) for r in self.ledger.records() if r.get("event") == "started"],
                default=0.0,
            )
            gap = t_now - last_start if last_start else float("inf")
            details["since_last_start_sec"] = round(gap, 1) if last_start else None
            if last_start and gap < cfg.min_interval_sec:
                reasons.append("throttle_min_interval")
            hour_ago = t_now - 3600.0
            recent = [
                r
                for r in self.ledger.records()
                if r.get("event") == "started" and float(r.get("started_epoch") or 0.0) >= hour_ago
            ]
            details["sessions_last_hour"] = len(recent)
            if len(recent) >= cfg.max_sessions_per_hour:
                reasons.append("throttle_hourly_cap")

        return {
            "allowed": not reasons,
            "reasons": reasons,
            "lane": LANE_BACKFILL,
            "tier": TICKET_TIER[LANE_BACKFILL],
            "force": bool(force),
            "checked_at": _utc_now(),
            "details": details,
            "max_wait_sec": float(cfg.force_max_wait_sec if force else cfg.auto_max_wait_sec),
        }

    # -- in-flight ---------------------------------------------------------------
    def inflight_running(self) -> dict[str, Any] | None:
        """Latest `started` record without a terminal record, if its pid is still alive."""
        terminal = self.ledger.terminal_by_session()
        rows = [r for r in self.ledger.records() if r.get("event") == "started"]
        for rec in reversed(rows):
            if str(rec.get("session_key") or "") in terminal:
                continue
            pid = rec.get("pid")
            if isinstance(pid, int) and _pid_alive(pid):
                return rec
        return None

    def stale_inflight(self) -> list[dict[str, Any]]:
        """`started` records with a dead pid and no terminal record (crash recovery)."""
        terminal = self.ledger.terminal_by_session()
        out: list[dict[str, Any]] = []
        for rec in self.ledger.records():
            if rec.get("event") != "started":
                continue
            sk = str(rec.get("session_key") or "")
            if sk in terminal:
                continue
            pid = rec.get("pid")
            if isinstance(pid, int) and not _pid_alive(pid):
                out.append(rec)
        return out

    # -- dispatch ----------------------------------------------------------------
    def dispatch(
        self,
        *,
        force: bool = False,
        dry_run: bool = True,
        title: str | None = None,
        max_sessions: int = 1,
        npu: dict[str, Any] | None = None,
        allow_offline: bool = False,
    ) -> dict[str, Any]:
        """Admit → start → (optionally execute) one B0 session.  Default is `dry_run`."""
        cfg = self.config()
        jobs = self.plan(force=force)
        if title:
            jobs = [j for j in jobs if j["title"] == title]
        if not jobs:
            return {
                "action": "backfill",
                "dispatched": False,
                "dry_run": bool(dry_run),
                "reason": "queue_empty",
                "queued": [],
                "admission": None,
            }

        adm = self.admission(force=force, npu=npu)
        job = jobs[0]
        runner_plan = self._build_runner_cfg(job, adm)
        if dry_run:
            # `--dry-run` always returns the plan *and* the admission proof, never executes
            return {
                "action": "backfill",
                "dispatched": False,
                "dry_run": True,
                "reason": "dry_run" if adm["allowed"] else "dry_run_admission_blocked",
                "blocked_by": adm["reasons"],
                "plan": runner_plan,
                "session": job,
                "admission": adm,
                "queued": jobs[1:],
            }
        if not adm["allowed"]:
            return {
                "action": "backfill",
                "dispatched": False,
                "dry_run": False,
                "reason": "admission_blocked",
                "blocked_by": adm["reasons"],
                "admission": adm,
                "queued": jobs,
            }

        started_epoch = self.clock()
        base = {
            "session_key": job["session_key"],
            "title": job["title"],
            "baseline_sha": job["baseline_sha"],
            "buckets": job["buckets"],
            "bucket_keys": job["bucket_keys"],
            "lane": LANE_BACKFILL,
            "tier": TICKET_TIER[LANE_BACKFILL],
            "host": self.npu_host,
            "pid": None,
        }
        import os as _os

        base["pid"] = _os.getpid()
        self.ledger.append({"event": "started", "started_epoch": round(started_epoch, 3), "started_at": _utc_now(), **base})

        try:
            res = self.runner(runner_plan)
        except Exception as exc:  # never let a transport failure break the caller
            res = {"ok": False, "reason": f"runner_exception: {exc}"}

        ended_epoch = self.clock()
        parsed = parse_validation_perf("\n".join(res.get("timing_lines") or []))
        soc = parsed.get("runtime_soc") or res.get("runtime_soc")
        # Modality ladder mirrors `BenchmarkCalibrator._extract_bucket_timings`:
        # hardware profiler wins; host-enqueue proxies are only usable after floor removal.
        pool: dict[str, float] = {}
        modality = "none"
        if parsed.get("profiler_cases"):
            pool = dict(parsed["profiler_cases"])
            modality = "profiler"
        elif parsed.get("same_soc_exec_cases"):
            ovh = 7.2 if soc == "soc-a" else 3.2
            pool = {k: max(3.6, v - ovh) for k, v in parsed["same_soc_exec_cases"].items()}
            modality = "exec_normalized"
        elif parsed.get("dev_event_cases"):
            ovh = 5.2 if soc == "soc-a" else 1.8
            pool = {k: max(3.6, v - ovh) for k, v in parsed["dev_event_cases"].items()}
            modality = "dev_normalized"
        measured = {b: pool[b] for b in job["buckets"] if b in pool and pool[b] > 0}

        if measured:
            self.store_timings(
                job["title"],
                str(job["baseline_sha"]),
                measured,
                runtime_soc=soc,
                modality=modality,
            )
            # Re-fit the affected operator so `results/<Title>/calibration.json` (and thus the
            # app banner via `status.backfill`) reflects the fresh pairings immediately.
            try:
                self.calibrator.calibrate_problem(job["title"], persist=True)
            except Exception:
                pass

        result = "ok" if measured else str(res.get("reason") or "no_timing_evidence")
        terminal = {
            "event": "finished",
            "result": result,
            "started_epoch": round(started_epoch, 3),
            "ended_epoch": round(ended_epoch, 3),
            "duration_sec": round(max(0.0, ended_epoch - started_epoch), 2),
            "measured": {k: round(float(v), 3) for k, v in measured.items()},
            "measured_count": len(measured),
            "requested_count": len(job["buckets"]),
            "unmeasured_buckets": [] if not measured else [b for b in job["buckets"] if b not in measured],
            "unmeasured_keys": [] if not measured else [
                bucket_key(job["title"], b, job["baseline_sha"])
                for b in job["buckets"]
                if b not in measured
            ],
            "runtime_soc": soc,
            "modality": modality,
            "tool": (runner_plan or {}).get("tool"),
            "worktree": res.get("worktree"),
            "wt_method": res.get("wt_method"),
            "built": res.get("built"),
            "build_ok": bool(res.get("built") or res.get("build_cached")) and res.get("build_rc") in (0, None),
            "build_sec": res.get("build_sec"),
            "build_cached": res.get("build_cached"),
            "perf_sec": res.get("perf_sec"),
            "harness_source": res.get("harness_source"),
            "log": res.get("log"),
            "die": res.get("die"),
            "lock_wait_sec": res.get("lock_wait_sec"),
            "head_used": res.get("head_used"),
            "cpp_identical_head": res.get("cpp_identical_head"),
            "accepted": bool(res.get("ok")),
        }
        self.ledger.append({**base, **terminal})

        return {
            "action": "backfill",
            "dispatched": True,
            "dry_run": False,
            "result": result,
            "session": job,
            "measured": {k: round(float(v), 3) for k, v in measured.items()},
            "admission": adm,
            "remote": {k: v for k, v in res.items() if k != "timing_lines"},
        }

    # -- status ------------------------------------------------------------------
    def status(self, *, hydrate: bool = True, npu: dict[str, Any] | None = None) -> dict[str, Any]:
        """The `python -m scheduler.backfill_status --json` contract."""
        cfg = self.config()
        t_now = self.clock()
        jobs = self.plan(now=t_now) if hydrate else []
        unpaired = self.scan_unpaired() if hydrate else []
        inflight = self.inflight_running()
        stale = self.stale_inflight()

        last_run = None
        for rec in reversed(self.ledger.records()):
            if rec.get("event") == "finished":
                last_run = {
                    "title": rec.get("title"),
                    "baseline_short": (rec.get("baseline_sha") or "")[:7] or None,
                    "finished_at": _iso_from_epoch(rec.get("ended_epoch")),
                    "result": rec.get("result"),
                    "duration_sec": rec.get("duration_sec"),
                    "measured_count": rec.get("measured_count"),
                    "requested_count": rec.get("requested_count"),
                    "log": rec.get("log"),
                }
                break

        est = self.est_session_sec()
        eta_lock = sum(float(j["est_sec"]) for j in jobs)
        eta = sum(float(j.get("budget_sec") or j["est_sec"]) for j in jobs)
        # `eta_wall_sec` also pays the auto-throttle gap between consecutive sessions
        eta_wall = eta + max(0.0, cfg.min_interval_sec) * max(0, len(jobs) - 1)

        running = None
        if inflight:
            running = {
                "job_id": inflight.get("session_key"),
                "title": inflight.get("title"),
                "baseline_short": (inflight.get("baseline_sha") or "")[:7] or None,
                "buckets": inflight.get("buckets") or [],
                "started_at": inflight.get("started_at"),
                "elapsed_sec": round(max(0.0, t_now - float(inflight.get("started_epoch") or t_now)), 1),
                "pid": inflight.get("pid"),
                "lane": LANE_BACKFILL,
                "tier": TICKET_TIER[LANE_BACKFILL],
            }

        return {
            "generated_at": _utc_now(),
            "lane": LANE_BACKFILL,
            "tier": TICKET_TIER[LANE_BACKFILL],
            "derived_tiers": dict(TICKET_TIER),
            "unpaired": unpaired,
            "unpaired_count": len(unpaired),
            "unpaired_operators": sorted({r["title"] for r in unpaired}),
            "queued": jobs,
            "queued_count": len(jobs),
            "unmeasurable": self.unmeasurable(),
            "running": running,
            "stale_inflight": [
                {"job_id": r.get("session_key"), "title": r.get("title"), "pid": r.get("pid"), "started_at": r.get("started_at")}
                for r in stale
            ],
            "last_run": last_run,
            "eta_sec": round(eta, 1),
            "eta_lock_sec": round(eta_lock, 1),
            "eta_build_sec": round(max(0.0, eta - eta_lock), 1),
            "eta_wall_sec": round(eta_wall, 1),
            "est_session_sec": est,
            "ledger_sha": self.ledger.sha256(),
            "ledger_records": len(self.ledger.records()),
            "ledger_path": str(self.ledger.path),
            "paired_via_backfill": len(self._load_timings_doc().get("entries") or {}),
            "admission": (self.admission(force=False, npu=npu) if npu is not None else None),
            "config": cfg.to_dict(),
        }

    # -- cheap summary for `cli_bridge.status` (no extra scanning) ---------------
    def summary(self, cal_docs: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
        """Zero-cost banner payload derived from already-loaded calibration docs."""
        per: dict[str, int] = {}
        for title, doc in (cal_docs or {}).items():
            n = int(doc.get("unpaired_bucket_count") or 0)
            if n > 0:
                per[title] = n
        inflight = self.inflight_running()
        last = None
        for rec in reversed(self.ledger.records()):
            if rec.get("event") == "finished":
                last = {
                    "title": rec.get("title"),
                    "result": rec.get("result"),
                    "finished_at": _iso_from_epoch(rec.get("ended_epoch")),
                    "measured_count": rec.get("measured_count"),
                }
                break
        cfg = self.config()
        return {
            "lane": LANE_BACKFILL,
            "tier": TICKET_TIER[LANE_BACKFILL],
            "auto_enabled": bool(cfg.enabled and cfg.auto_dispatch),
            "unpaired_by_operator": per,
            "unpaired_total": sum(per.values()),
            "running": bool(inflight),
            "running_title": (inflight or {}).get("title"),
            "last_run": last,
            "est_session_sec": self.est_session_sec(),
            "ledger_sha": self.ledger.sha256(),
        }

    # -- remote transport --------------------------------------------------------
    def _build_runner_cfg(self, job: dict[str, Any], adm: dict[str, Any]) -> dict[str, Any]:
        cfg = self.config()
        repo_dir = self.workspace / "bench" / str(job["title"])
        plan = build_measurement_plan(
            repo_dir,
            [str(b) for b in job["buckets"]],
            max_runs=int(cfg.max_runs_per_session),
        )
        return {
            "title": job["title"],
            "sha": job["baseline_sha"],
            "buckets": job["buckets"],
            "wait_sec": float(adm.get("max_wait_sec") or 0.0),
            "perf_timeout_sec": float(cfg.session_timeout_sec),
            "build_timeout_sec": float(cfg.build_timeout_sec),
            "cache_days": float(cfg.build_cache_days),
            "min_free_gb": float(cfg.min_free_gb),
            "max_runs": int(cfg.max_runs_per_session),
            "force": bool(adm.get("force")),
            "tool": plan["tool"],
            "uses_root": bool(plan.get("uses_root")),
            "perf_flag": plan.get("flag"),
            "perf_mode": plan.get("mode"),
            "perf_runs": plan.get("runs") or [[]],
            "bucket_count": len(job["buckets"]),
            "session_key": job["session_key"],
            "budget_sec": round(self.est_session_sec() + (0.0 if self.build_cache_hit(job) else cfg.est_build_sec), 1),
        }

    def build_cache_hit(self, job: dict[str, Any]) -> bool:
        """True when `(operator, baseline_sha)` already has a warm server build cache.

        A *successful* session keeps its worktree (the cache); failed sessions drop it, so
        only `result == "ok"` records within `build_cache_days` count as warm.
        """
        cfg = self.config()
        cutoff = self.clock() - max(0.0, cfg.build_cache_days) * 86400.0
        for rec in reversed(self.ledger.records()):
            if rec.get("event") != "finished" or not rec.get("build_ok"):
                continue
            if rec.get("title") != job.get("title"):
                continue
            if (rec.get("baseline_sha") or "")[:12] != (job.get("baseline_sha") or "")[:12]:
                continue
            if float(rec.get("ended_epoch") or 0.0) >= cutoff:
                return True
        return False

    def _run_remote_dispatch(self, cfg: dict[str, Any]) -> dict[str, Any]:
        """Execute one B0 session over SSH; returns the remote JSON verdict."""
        cmd = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=4",
            "-o",
            "ControlMaster=auto",
            "-o",
            "ControlPath=/tmp/opsb-ssh-%C",
            "-o",
            "ControlPersist=120",
            self.npu_host,
            "python3 -c " + shlex.quote(REMOTE_BACKFILL),
        ]
        timeout = (
            float(cfg.get("wait_sec") or 0.0)
            + float(cfg.get("build_timeout_sec") or 300.0)
            + float(cfg.get("perf_timeout_sec") or 180.0)
            + 120.0
        )
        try:
            proc = subprocess.run(cmd, input=json.dumps(cfg), capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"ok": False, "reason": "ssh_timeout"}
        for line in reversed((proc.stdout or "").splitlines()):
            if line.startswith("BACKFILL_RESULT "):
                try:
                    return json.loads(line[len("BACKFILL_RESULT ") :])
                except ValueError:
                    continue
        return {
            "ok": False,
            "reason": f"remote_protocol_error rc={proc.returncode}",
            "stderr": (proc.stderr or "")[-300:],
        }


# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------
def _pid_alive(pid: int) -> bool:
    try:
        import os

        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def _iso_from_epoch(epoch: Any) -> str | None:
    if not isinstance(epoch, (int, float)) or epoch <= 0:
        return None
    return datetime.datetime.fromtimestamp(float(epoch), datetime.timezone.utc).isoformat()


def _is_offline(npu: dict[str, Any] | None) -> bool:
    if not npu:
        return False
    return bool(npu.get("offline")) or bool(npu.get("ssh_error"))
