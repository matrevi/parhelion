#!/usr/bin/env python3
"""Validate a pushed commit on the compute server via the 3-Phase Disaggregated Pipeline.

Phase A (0.0s NPU die lock):
  - Holds per-problem `<remote-work-root>/<prefix>-repo-<Title>.lock` ONLY
    (all 10 problems compile in parallel across the 32 AArch64 CPU cores)
  - Emits heartbeat `~/.opsb/cpu_builds/<Title>.json` for live dashboard visibility
  - Performs detached git checkout of `<SHA>`, pure-host `walk_model.py` (if present),
    and full CMake + Compiler KernelC CPU package build (`build.sh -j4` without `-e`)
    plus `examples/build` pre-compilation
  - Exits immediately with `lock_waste_sec = 0.0` if CPU compilation fails or `--build-only`

Phase B (Cooperative WSPT + MLFQ Q0/Q1 Ticket -> Exclusive NPU Die Lock):
  - Registers priority ticket `/tmp/opsb-npu-tickets/<pid>.json`
  - Acquires `<prefix>-device{0,1}.lock` + shared `<prefix>-validation.lock`
  - Emits live substage heartbeat `~/.opsb/die_session.json`
  - Injects `OPSB_ARENA_ACCEL=1` (`PYTHONPATH=/home/developer/.opsb/runtime`) so
    `acl_runner.Runner` executes in-process via `PersistentArenaRunner` (~2ms/case vs 4.15s)
  - Skips redundant second C++ `build.sh` inside `device0.lock` while running full
    `examples/run.sh`, `extended.py`, `stress.py`, and `profiler`

Phase C (0.0s NPU die lock):
  - Releases `device0.lock` immediately, records telemetry in `~/.opsb/die_ledger.jsonl`,
    and writes `results/<Title>/<SHA>/{validation.log,result.json}`.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

def _default_workspace() -> Path:
    env_ws = os.environ.get("OPSB_WORKSPACE")
    if env_ws:
        return Path(env_ws).expanduser().resolve()
    return Path.cwd().resolve()


ROOT = _default_workspace()

# Neutral fallbacks; the real deployment facts live in ~/.parhelion/config.json ("device").
_DEVICE_DEFAULTS: dict = {
    "soc": "soc-a",
    "soc_legacy": "soc-b",
    "build_target": "soc-a",
    "build_target_legacy": "soc-b",
    "arch": "arch-v1",
    "toolchain_env": "",
    "toolchain_root": "",
    "toolchain_dir": "",
    "sdk_glob": "sdk-*",
    "driver_root": "",
    "driver_lib_dirs": [],
    "driver_libs": {},
    "dcmi_lib": "",
    "driver_version_info": "",
    "toolkit_info": "toolkit_install.info",
    "toolkit_name": "toolkit",
    "lib_runtime": "libruntime.so",
    "visible_devices_env": "VISIBLE_DEVICES",
    "extra_ops_env": "EXTRA_OPS_PATH",
    "toolchain_home_env": "TOOLCHAIN_HOME",
    "tpl_env": "TPL_SEL",
    "tpl_prefix": "TPL_",
    "profiler_bin": "profiler",
    "profiler_acl_flag": "--acl",
    "judge_base": "https://judge.invalid",
    "judge_bot": "judge-bot",
    "work_root": "/srv/work",
    "work_prefix": "bench",
    "repo_url_template": "https://git.invalid/{prefix}-{slug}.git",
    "contest_org": "",
    "contest_track": "",
}


def _device_settings() -> dict:
    """Deployment facts (SoC, arch, toolchain, remote roots) for this host.

    Stdlib-only and self-contained on purpose: this script is also copied to the
    compute host, where the CLI package may not be importable.
    """
    if os.environ.get("OPSB_DEVICE_ISOLATED") == "1":
        return dict(_DEVICE_DEFAULTS)
    merged = dict(_DEVICE_DEFAULTS)
    home_override = os.environ.get("PARHELION_HOME") or os.environ.get("OPSB_HOME")
    cfg_dir = Path(home_override).expanduser().resolve() if home_override else (Path.home() / ".parhelion")
    try:
        raw = json.loads((cfg_dir / "config.json").read_text(encoding="utf-8"))
        sect = raw.get("device") if isinstance(raw, dict) else None
        if isinstance(sect, dict):
            for key, value in sect.items():
                if key in merged and value not in (None, ""):
                    merged[key] = value
    except Exception:
        pass
    for key in list(merged):
        raw_env = os.environ.get("OPSB_DEVICE_" + key.upper())
        if raw_env:
            merged[key] = raw_env
    return merged


DEVICE = _device_settings()


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _resolve_parhelion_root() -> Path | None:
    """Locate standalone Parhelion installation (repo checkout or /Applications/Parhelion.app)."""
    env_root = os.environ.get("PARHELION_APP_ROOT")
    candidates: list[Path] = []
    if env_root:
        candidates.append(Path(env_root).expanduser().resolve())
    opsb_bin = Path.home() / ".local" / "bin" / "opsb"
    if opsb_bin.exists():
        try:
            candidates.append(opsb_bin.resolve().parents[1])
        except Exception:
            pass
    candidates.append(Path("/Applications/Parhelion.app/Contents/Resources"))
    for cand in candidates:
        if (cand / "scheduler" / "npu_scheduler.py").is_file():
            return cand
    return None


def _compute_scheduler_priority(title: str, fast: bool, workspace: Path = ROOT) -> tuple[float, str, float]:
    """Read WSPT base priority & MLFQ tier from standalone Parhelion scheduler if available."""
    sup_dir = _resolve_parhelion_root()
    if sup_dir is not None:
        try:
            if str(sup_dir) not in sys.path:
                sys.path.insert(0, str(sup_dir))
            from scheduler.npu_scheduler import NpuTaskScheduler  # type: ignore

            sched = NpuTaskScheduler(workspace)
            wspt_base, _, _, mlfq = sched.compute_wspt(title, fast=fast, wait_sec=0)
            return float(wspt_base), str(mlfq), float(sched.config.aging_tau_sec)
        except Exception:
            pass
    return (1.5 if fast else 1.0), ("Q0" if fast else "Q1"), 600.0


def evaluate_build_signature(
    repo: Path,
    branch_ref: str,
    target: str,
    sha: str,
    prev_sig: dict | None,
    is_ancestor: bool = True,
) -> tuple[dict, list[str]]:
    """Build `<repo>/build/.opsb_build_sig` payload and detect branch/header changes requiring clean rebuild."""
    opk_dir = repo / "op_kernel"
    opk_headers = sorted(
        str(p.relative_to(repo))
        for p in opk_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in (".h", ".hpp", ".cuh")
    ) if opk_dir.is_dir() else []
    cur_sig = {
        "branch_ref": branch_ref or "HEAD",
        "target": target or DEVICE["build_target"],
        "op_kernel_headers": opk_headers,
        "last_sha": sha,
    }
    reasons: list[str] = []
    if isinstance(prev_sig, dict) and prev_sig:
        if prev_sig.get("branch_ref") != cur_sig["branch_ref"]:
            reasons.append(f"branch:{prev_sig.get('branch_ref')}->{cur_sig['branch_ref']}")
        if prev_sig.get("target") != cur_sig["target"]:
            reasons.append(f"target:{prev_sig.get('target')}->{cur_sig['target']}")
        if prev_sig.get("op_kernel_headers") != cur_sig["op_kernel_headers"]:
            reasons.append("op_kernel_headers_changed")
        prev_sha = str(prev_sig.get("last_sha") or "")
        if prev_sha and prev_sha != sha and not reasons and not is_ancestor:
            reasons.append(f"non_ancestor:{prev_sha[:7]}->{sha[:7]}")
    return cur_sig, reasons


REMOTE_3PHASE = r'''
import datetime, fcntl, glob, hashlib, json, os, pathlib, re, shutil, signal, subprocess, sys, tempfile, threading, time

cfg = json.load(sys.stdin)
exec(cfg.pop('lanes_source'))
title = cfg['title']
sha = cfg['sha']
fast = bool(cfg.get('fast', False))
cold3 = bool(cfg.get('cold3', False))
pmu = bool(cfg.get('pmu', False))
branch_ref_cfg = str(cfg.get('branch_ref') or '').strip()
build_only = bool(cfg.get('build_only', False))
sync_only = bool(cfg.get('sync_only', False))
mlfq = cfg.get('mlfq') or ('Q0' if fast else 'Q1')
wspt_base = float(cfg.get('wspt_base', 1.0))
aging_tau = float(cfg.get('aging_tau', 600.0))

_dev = cfg.get('device') or {}
work_root = pathlib.Path(_dev.get('work_root') or '/srv/work')
work_prefix = _dev.get('work_prefix') or 'bench'
toolchain_env = _dev.get('toolchain_env') or ''
soc_default = _dev.get('soc') or 'soc-a'
build_target = _dev.get('build_target') or 'soc-a'
extra_ops_env = _dev.get('extra_ops_env') or 'EXTRA_OPS_PATH'
toolchain_home_env = _dev.get('toolchain_home_env') or 'TOOLCHAIN_HOME'
visible_env = _dev.get('visible_devices_env') or 'VISIBLE_DEVICES'
tpl_env = _dev.get('tpl_env') or 'TPL_SEL'
tpl_prefix = _dev.get('tpl_prefix') or 'TPL_'

root = work_root / work_prefix
root.mkdir(parents=True, exist_ok=True)
repo = root / title
opsb_home = pathlib.Path.home() / '.opsb'
cpu_builds_dir = opsb_home / 'cpu_builds'
runtime_dir = opsb_home / 'runtime'
cpu_builds_dir.mkdir(parents=True, exist_ok=True)

logdir = root.parent / (work_prefix + '-results') / title / sha
logdir.mkdir(parents=True, exist_ok=True)
log_name = (
    'sync.log' if sync_only else
    'build.log' if build_only else
    'validation-fast.log' if fast else
    'validation.log'
)
res_name = (
    'sync.json' if sync_only else
    'result-build.json' if build_only else
    'result-fast.json' if fast else
    'result.json'
)

# Per-problem repo lock: allows all 10 problems to compile in parallel on 32 CPU cores
# while preventing two overlapping runs of the SAME problem from clobbering git HEAD / build/.
repo_lock_f = (root.parent / f'{work_prefix}-repo-{title}.lock').open('a')
fcntl.flock(repo_lock_f, fcntl.LOCK_EX)

env = os.environ.copy()
env['GIT_TERMINAL_PROMPT'] = '0'
# Publish the configured environment-variable *names* so the in-process helpers
# (arena cache, profiler wait, environment probe) address the toolchain's own vars.
env['OPSB_EXTRA_OPS_ENV'] = extra_ops_env
env['OPSB_HOME_ENV'] = toolchain_home_env
env['OPSB_RUNTIME_LIB'] = _dev.get('lib_runtime') or 'libruntime.so'

# ---------------- PHASE A: 32-CORE CPU SYNC, BUILD & CASE CACHE (0.0s NPU DIE LOCK) ----------------
phase_a_t0 = time.monotonic()
cpu_hb = cpu_builds_dir / f'{title}.json'
use_arena_cache = False
arena_cache_dir = None

try:
    if not sync_only:
        try:
            cpu_hb.write_text(json.dumps({
                'pid': os.getpid(),
                'title': title,
                'sha': sha,
                'state': 'building',
                'started_epoch': round(time.time(), 3),
                'started_ts': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }))
        except Exception:
            pass

    with tempfile.TemporaryDirectory(prefix='git-auth-') as auth:
        askpass = pathlib.Path(auth) / 'askpass.py'
        askpass.write_text(
            '#!/usr/bin/env python3\nimport os,sys\n'
            'print(os.environ["TASK_GIT_USERNAME" if "username" in sys.argv[1].lower() else "TASK_GIT_PASSWORD"])\n'
        )
        askpass.chmod(0o700)
        env.update(
            GIT_ASKPASS=str(askpass),
            TASK_GIT_USERNAME=cfg.pop('username'),
            TASK_GIT_PASSWORD=cfg.pop('password'),
        )

        def run_git(*args, **kw):
            return subprocess.check_output(args, text=True, env=env, **kw).strip()

        if repo.exists() and not (repo / '.git').is_dir():
            backup = root.parent / (work_prefix + '-pre-git-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
            backup.mkdir(exist_ok=True)
            repo.rename(backup / title)
            print('Preserved previous directory:', backup / title, flush=True)
        if not repo.exists():
            subprocess.run(['git', '-c', 'credential.helper=', 'clone', cfg['url'], str(repo)], env=env, check=True)
        if run_git('git', '-C', str(repo), 'remote', 'get-url', 'origin') != cfg['url']:
            raise RuntimeError('Unexpected server origin')
        if run_git('git', '-C', str(repo), 'status', '--porcelain'):
            raise RuntimeError('Server checkout has local changes')
        subprocess.run(['git', '-C', str(repo), '-c', 'credential.helper=', 'fetch', 'origin', '--prune'], env=env, check=True)
        subprocess.run(['git', '-C', str(repo), '-c', 'credential.helper=', 'checkout', '--detach', '--force', sha], env=env, check=True)
        if run_git('git', '-C', str(repo), 'rev-parse', 'HEAD') != sha:
            raise RuntimeError('Server HEAD differs from the requested SHA')
        del env['TASK_GIT_USERNAME'], env['TASK_GIT_PASSWORD'], env['GIT_ASKPASS']

    if sync_only:
        print('SYNCHRONIZED:', title, sha, flush=True)
        sys.exit(0)

    log_f = (logdir / log_name).open('w', encoding='utf-8')

    def emit(msg):
        log_f.write(msg + '\n')
        log_f.flush()
        print(msg, flush=True)

    emit(f'Verified commit: {sha}')
    if fast:
        emit(f'FAST_LANE=1 MLFQ={mlfq}')

    # Optional pure-host pre-filters (0s NPU lock): walk_model.py & plan_check.py
    for host_check in ('walk_model.py', 'plan_check.py'):
        hc_py = repo / 'validation' / host_check
        if hc_py.is_file():
            emit(f'[PHASE-A] Running pure-host {host_check} (0s NPU lock)...')
            wp = subprocess.run(['python3', str(hc_py)], cwd=repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            log_f.write(wp.stdout)
            log_f.flush()
            print(wp.stdout, end='', flush=True)
            if wp.returncode != 0:
                emit(f'VALIDATION_FAILED rc={wp.returncode} (Phase-A {host_check} failed; 0.0s NPU lock used)')
                log_f.close()
                (logdir / res_name).write_text(json.dumps({
                    'title': title, 'sha': sha, 'exit_code': wp.returncode,
                    'phase': f'A_{host_check}', 'phase_a_cpu_sec': round(time.monotonic() - phase_a_t0, 2),
                    'phase_b_die_sec': 0.0, 'finished_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                }, indent=2) + '\n')
                sys.exit(wp.returncode)

    # Phase A CPU-only KernelC host + kernel package build + examples pre-build + sample data gen (NO -e, NO aclInit!)
    # Adaptive CPU build parallelism: dynamically scales `-j` from server core count (16c/32c/etc.)
    # and live concurrent builds in `~/.opsb/cpu_builds/*.json`, reserving ~10% (min 2 cores) for NPU host/daemon.
    ncpu = os.cpu_count() or 16
    active_builds = 1
    try:
        live_b = 0
        for hb_p in cpu_builds_dir.glob('*.json'):
            try:
                hb_d = json.loads(hb_p.read_text(encoding='utf-8'))
                hb_pid = int(hb_d.get('pid') or 0)
                if hb_pid > 0 and pathlib.Path(f'/proc/{hb_pid}').exists():
                    live_b += 1
            except Exception:
                pass
        active_builds = max(1, live_b)
    except Exception:
        active_builds = 1
    usable_cores = max(4, ncpu - max(2, ncpu // 10))
    if env.get('OPSB_BUILD_JOBS', '').isdigit():
        build_jobs = max(1, min(ncpu, int(env['OPSB_BUILD_JOBS'])))
    else:
        build_jobs = max(4, min(16, usable_cores // active_builds))
    env['OPSB_EFFECTIVE_BUILD_JOBS'] = str(build_jobs)

    # Structural build signature guard (`<repo>/build/.opsb_build_sig`):
    # Triggers `--make_clean` on branch switch, target switch, or `op_kernel/` header additions/removals,
    # while keeping incremental rebuilds fast within the same branch.
    sig_branch = branch_ref_cfg
    if not sig_branch or sig_branch == 'HEAD':
        try:
            sig_branch = subprocess.check_output(
                ['git', '-C', str(repo), 'name-rev', '--name-only', '--refs=refs/remotes/origin/*', 'HEAD'],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip() or 'HEAD'
        except Exception:
            sig_branch = 'HEAD'
    opk_dir_sig = repo / 'op_kernel'
    opk_headers_sig = sorted(
        str(p.relative_to(repo))
        for p in opk_dir_sig.rglob('*')
        if p.is_file() and p.suffix.lower() in ('.h', '.hpp', '.cuh')
    ) if opk_dir_sig.is_dir() else []
    cur_build_sig = {
        'branch_ref': sig_branch,
        'target': env.get('OPSB_BUILD_TARGET', soc_default),
        'op_kernel_headers': opk_headers_sig,
        'last_sha': sha,
    }
    sig_path = repo / 'build' / '.opsb_build_sig'
    prev_build_sig = None
    if sig_path.is_file():
        try:
            prev_build_sig = json.loads(sig_path.read_text(encoding='utf-8'))
        except Exception:
            prev_build_sig = {}
    clean_reasons = []
    if isinstance(prev_build_sig, dict) and prev_build_sig:
        if prev_build_sig.get('branch_ref') != cur_build_sig['branch_ref']:
            clean_reasons.append(f"branch:{prev_build_sig.get('branch_ref')}->{cur_build_sig['branch_ref']}")
        if prev_build_sig.get('target') != cur_build_sig['target']:
            clean_reasons.append(f"target:{prev_build_sig.get('target')}->{cur_build_sig['target']}")
        if prev_build_sig.get('op_kernel_headers') != cur_build_sig['op_kernel_headers']:
            clean_reasons.append('op_kernel_headers_changed')
        prev_sha_sig = str(prev_build_sig.get('last_sha') or '')
        if prev_sha_sig and prev_sha_sig != sha and not clean_reasons:
            rc_anc = subprocess.run(
                ['git', '-C', str(repo), 'merge-base', '--is-ancestor', prev_sha_sig, sha],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            if rc_anc != 0:
                clean_reasons.append(f"non_ancestor:{prev_sha_sig[:7]}->{sha[:7]}")
    if env.get('OPSB_MAKE_CLEAN') == '1':
        clean_reasons.append('explicit_env')
    if clean_reasons:
        env['OPSB_MAKE_CLEAN'] = '1'
        env['OPSB_CLEAN_REASON'] = ','.join(clean_reasons)
        emit(f"[PHASE-A] Auto clean-rebuild armed ({env['OPSB_CLEAN_REASON']})")

    emit(f'[PHASE-A] Building KernelC package + examples on {ncpu}-core CPU with adaptive -j{build_jobs} (active_builds={active_builds}, 0s NPU lock)...')
    phase_a_sh = r"""
set -uo pipefail
@@SRC@@
TARGET="${OPSB_BUILD_TARGET:-@@BUILD_TARGET@@}"
JOBS="${OPSB_EFFECTIVE_BUILD_JOBS:-4}"
if [ "${OPSB_MAKE_CLEAN:-0}" = "1" ]; then
  echo "[PHASE-A] Clean rebuild triggered (${OPSB_CLEAN_REASON:-signature_changed}) via --make_clean..."
  (sed "s/COMPUTE_UNIT=\"@@BUILD_TARGET@@\"/COMPUTE_UNIT=\"$TARGET\"/" build.sh | bash -s -- --make_clean) >/dev/null 2>&1 || rm -rf "$PWD/build"/* "$PWD/build_out"/*
  rm -rf "$PWD/examples/build"
fi
sed "s/COMPUTE_UNIT=\"@@BUILD_TARGET@@\"/COMPUTE_UNIT=\"$TARGET\"/" build.sh | bash -s -- -j"$JOBS"
BRC=$?
if [ "$BRC" -ne 0 ]; then
  exit "$BRC"
fi
VENDOR_DIR=$(ls -d "$PWD"/build/packages/vendors/*_custom 2>/dev/null | head -n 1 || true)
if [ -n "$VENDOR_DIR" ] && [ -d "$PWD/examples" ]; then
  export @@EXTRA@@="$VENDOR_DIR${@@EXTRA@@:+:$@@EXTRA@@}"
  export LD_LIBRARY_PATH="$VENDOR_DIR/op_api/lib:$@@HOME@@/lib64:${LD_LIBRARY_PATH:-}"
  cmake -S "$PWD/examples" -B "$PWD/examples/build" >/dev/null
  cmake --build "$PWD/examples/build" --parallel "$JOBS"
fi
if [ -f "$PWD/samples/sample.py" ]; then
  mkdir -p "$PWD/samples/build"
  python3 "$PWD/samples/sample.py" generate --directory "$PWD/samples/build" >/dev/null 2>&1 || true
fi
"""
    # Toolchain facts are deployment data, substituted here instead of being literal.
    phase_a_sh = (
        phase_a_sh
        .replace('@@SRC@@', f'source {toolchain_env}' if toolchain_env else ':')
        .replace('@@BUILD_TARGET@@', build_target)
        .replace('@@EXTRA@@', extra_ops_env)
        .replace('@@HOME@@', toolchain_home_env)
    )
    bp = subprocess.Popen(['bash', '-c', phase_a_sh], cwd=repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in bp.stdout:
        log_f.write(line)
        log_f.flush()
        print(line, end='', flush=True)
    b_rc = bp.wait()

    if b_rc != 0:
        phase_a_sec = round(time.monotonic() - phase_a_t0, 2)
        emit(f'VALIDATION_FAILED rc={b_rc} (Phase-A CPU build failed in {phase_a_sec:.1f}s; 0.0s NPU die lock wasted)')
        log_f.close()
        (logdir / res_name).write_text(json.dumps({
            'title': title, 'sha': sha, 'exit_code': b_rc,
            'phase': 'A_CPU_BUILD', 'phase_a_cpu_sec': phase_a_sec,
            'phase_b_die_sec': 0.0, 'lock_waste_sec': 0.0,
            'finished_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }, indent=2) + '\n')
        sys.exit(b_rc)

    try:
        sig_path.parent.mkdir(parents=True, exist_ok=True)
        sig_path.write_text(json.dumps(cur_build_sig, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    except Exception:
        pass

    # Phase A compiled kernel ELF `.o` telemetry (0s NPU lock; tracks 3-run cold-start bloat)
    kernel_o_count = 0
    kernel_o_max_bytes = 0
    kernel_o_sum_bytes = 0
    kernel_o_max_text_bytes = 0
    kernel_o_files = {}
    template_instantiations = 0
    template_instantiation_source = 'none'
    obj_template_syms = set()
    sym_size_map = {}
    top_symbols = []
    max_hot_symbol_bytes = 0
    try:
        o_paths = sorted(glob.glob(str(repo / 'build/packages/vendors/*_custom/op_impl/ai_core/tbe/kernel/**/*.o'), recursive=True))
        if not o_paths:
            o_paths = sorted(glob.glob(str(repo / 'build/op_kernel/**/*.o'), recursive=True))
        for op in o_paths:
            op_base = os.path.basename(op)
            try:
                sz = os.path.getsize(op)
            except OSError:
                continue
            kernel_o_count += 1
            kernel_o_sum_bytes += sz
            if sz > kernel_o_max_bytes:
                kernel_o_max_bytes = sz
            kernel_o_files[op_base] = sz
            try:
                sp = subprocess.run(['size', '-A', op], capture_output=True, text=True, timeout=2.0)
                if sp.returncode == 0:
                    for sl in sp.stdout.splitlines():
                        parts = sl.split()
                        if len(parts) >= 2 and parts[0] == '.text' and parts[1].isdigit():
                            tb = int(parts[1])
                            if tb > kernel_o_max_text_bytes:
                                kernel_o_max_text_bytes = tb
            except Exception:
                pass
            # Collect per-function symbol sizes (`nm --size-sort` first, `readelf -sW` fallback)
            got_sym_sizes = False
            try:
                nms_p = subprocess.run(['nm', '-C', '--defined-only', '--size-sort', op], capture_output=True, text=True, timeout=2.0)
                if nms_p.returncode == 0 and nms_p.stdout:
                    for nml in nms_p.stdout.splitlines():
                        cols = nml.strip().split(None, 2)
                        if len(cols) == 3 and cols[1] in ('T', 'W', 't', 'w'):
                            try:
                                sbytes = int(cols[0], 16)
                            except ValueError:
                                continue
                            sname = cols[2]
                            if sbytes > 0:
                                key = (op_base, sname)
                                sym_size_map[key] = max(sym_size_map.get(key, 0), sbytes)
                                got_sym_sizes = True
            except Exception:
                pass
            if not got_sym_sizes:
                try:
                    re_p = subprocess.run(['readelf', '-sW', op], capture_output=True, text=True, timeout=2.0)
                    if re_p.returncode == 0 and re_p.stdout:
                        for rel_l in re_p.stdout.splitlines():
                            cols = rel_l.split()
                            if len(cols) >= 8 and cols[3] == 'FUNC' and cols[6] != 'UND':
                                try:
                                    sbytes = int(cols[2], 0)
                                except ValueError:
                                    continue
                                sname = cols[-1]
                                if sbytes > 0 and sname:
                                    key = (op_base, sname)
                                    sym_size_map[key] = max(sym_size_map.get(key, 0), sbytes)
                except Exception:
                    pass
            # Count compiled template / tiling-key symbols via nm or objdump
            try:
                nm_p = subprocess.run(['nm', '-C', '--defined-only', op], capture_output=True, text=True, timeout=2.0)
                if nm_p.returncode == 0 and nm_p.stdout:
                    template_instantiation_source = 'nm'
                    for nml in nm_p.stdout.splitlines():
                        cols = nml.strip().split(None, 2)
                        if len(cols) == 3 and cols[1] in ('T', 'W', 't', 'w'):
                            sym = cols[2]
                            if '<' in sym or 'tiling' in sym.lower() or 'kernel' in sym.lower() or '_Z' in sym:
                                obj_template_syms.add(sym)
                else:
                    od_p = subprocess.run(['objdump', '-t', op], capture_output=True, text=True, timeout=2.0)
                    if od_p.returncode == 0 and od_p.stdout:
                        template_instantiation_source = 'objdump'
                        for odl in od_p.stdout.splitlines():
                            if '.text' in odl and ('_Z' in odl or 'tiling' in odl.lower() or 'kernel' in odl.lower()):
                                obj_template_syms.add(odl.strip().split()[-1])
            except Exception:
                pass

        if sym_size_map:
            top_symbols = [
                {'symbol': s[:180], 'bytes': b, 'file': f}
                for (f, s), b in sorted(sym_size_map.items(), key=lambda kv: kv[1], reverse=True)[:8]
            ]
            max_hot_symbol_bytes = top_symbols[0]['bytes']

        if obj_template_syms:
            template_instantiations = len(obj_template_syms)
        else:
            # Source scan fallback in op_kernel/
            ok_dir = repo / 'op_kernel'
            if ok_dir.is_dir():
                tk_set = set()
                macro_cnt = 0
                tmpl_cnt = 0
                for kp in sorted(ok_dir.rglob('*')):
                    if not kp.is_file() or kp.suffix.lower() not in ('.h', '.hpp', '.cpp', '.cc', '.cxx', '.c'):
                        continue
                    try:
                        ktxt = kp.read_text(encoding='utf-8', errors='replace')
                    except OSError:
                        continue
                    ktxt = re.sub(r'/\*.*?\*/', '', ktxt, flags=re.S)
                    ktxt = re.sub(r'//.*?$', '', ktxt, flags=re.M)
                    for km in re.finditer(r'\b(?:TILING_KEY_IS|REGISTER_TILING_KEY)\s*\(\s*([^)]+)\)', ktxt):
                        tk_set.add(re.sub(r'\s+', '', km.group(1)))
                        macro_cnt += 1
                    for km in re.finditer(r'\b' + re.escape(tpl_env) + r'(?:_[A-Z0-9_]+)?\s*\(\s*([^)]*)\)', ktxt):
                        tk_set.add(tpl_env + ':' + re.sub(r'\s+', '', km.group(1)))
                        macro_cnt += 1
                    macro_cnt += len(re.findall(r'\b(?:IMPL_OP|EXEC_KERNEL|DISPATCH_KERNEL|INSTANTIATE_KERNEL|' + re.escape(tpl_prefix) + r'[A-Z0-9_]*DECL|GET_TPL_TILING_KEY)\s*\(', ktxt))
                    tmpl_cnt += len(re.findall(r'\btemplate\s*<', ktxt))
                template_instantiations = max(len(tk_set), macro_cnt) if (tk_set or macro_cnt) else tmpl_cnt
                template_instantiation_source = 'source_scan'

        if kernel_o_count > 0 or template_instantiations > 0:
            emit('KERNEL_BINARY_STATS ' + json.dumps({
                'count': kernel_o_count,
                'max_bytes': kernel_o_max_bytes,
                'sum_bytes': kernel_o_sum_bytes,
                'max_text_bytes': kernel_o_max_text_bytes,
                'max_hot_symbol_bytes': max_hot_symbol_bytes,
                'top_symbols': top_symbols,
                'template_instantiations': template_instantiations,
                'template_source': template_instantiation_source,
                'files': dict(sorted(kernel_o_files.items(), key=lambda kv: kv[1], reverse=True)[:8]),
            }, sort_keys=True))
    except Exception:
        pass

    # Phase A persistent extended-suite input & golden cache (0s NPU lock; 0.00s on cache hit!)
    ext_py = repo / 'validation' / 'extended.py'
    if (not build_only) and (not fast) and ext_py.is_file() and (runtime_dir / 'arena_cache.py').is_file() and env.get('OPSB_ARENA_CACHE', '1') != '0':
        h = hashlib.sha256()
        for rel in ('validation/extended.py', 'validation/acl_runner.py', 'samples/reference.py'):
            fp = repo / rel
            if fp.is_file():
                h.update(rel.encode())
                h.update(fp.read_bytes())
        spec_hash = h.hexdigest()[:16]
        cdir = pathlib.Path('/tmp/opsb-cases') / title / spec_hash
        if not (cdir / 'DONE').is_file():
            emit(f'[PHASE-A] Pre-computing extended inputs & CPU goldens into {cdir} (0s NPU lock)...')
            rec_env = env.copy()
            rec_env['OPSB_ARENA_PHASE'] = 'record'
            rec_env['OPSB_ARENA_CACHE_DIR'] = str(cdir)
            cur_pp = rec_env.get('PYTHONPATH', '')
            rec_env['PYTHONPATH'] = f"{runtime_dir}:{cur_pp}" if cur_pp else str(runtime_dir)
            rp = subprocess.run(['python3', str(ext_py)], cwd=repo, env=rec_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            if rp.returncode != 0 or not (cdir / 'DONE').is_file():
                shutil.rmtree(cdir, ignore_errors=True)
                emit(f'[PHASE-A] Case-cache record skipped (rc={rp.returncode}); falling back to inline arena')
            else:
                emit(f'[PHASE-A] Cached extended cases ready ({spec_hash})')
        else:
            emit(f'[PHASE-A] Extended case cache HIT ({spec_hash}, 0.0s golden compute)')
        if (cdir / 'DONE').is_file():
            use_arena_cache = True
            arena_cache_dir = cdir

    phase_a_sec = round(time.monotonic() - phase_a_t0, 2)

    if build_only:
        emit(f'BUILD_ONLY_PASSED ({phase_a_sec:.1f}s CPU, 0.0s NPU lock)')
        log_f.close()
        build_res = {
            'title': title, 'sha': sha, 'exit_code': 0, 'build_only': True,
            'phase': 'A_CPU_BUILD', 'phase_a_cpu_sec': phase_a_sec,
            'phase_b_die_sec': 0.0,
            'kernel_o_count': kernel_o_count,
            'kernel_o_max_bytes': kernel_o_max_bytes,
            'kernel_o_sum_bytes': kernel_o_sum_bytes,
            'kernel_o_max_text_bytes': kernel_o_max_text_bytes,
            'max_hot_symbol_bytes': max_hot_symbol_bytes,
            'top_symbols': top_symbols,
            'template_instantiations': template_instantiations,
            'finished_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        (logdir / res_name).write_text(json.dumps(build_res, indent=2) + '\n')
        print('OPSB_PHASE_METRICS ' + json.dumps(build_res), flush=True)
        sys.exit(0)

finally:
    cpu_hb.unlink(missing_ok=True)

# ---------------- PHASE B: COOPERATIVE WSPT TICKET -> EXCLUSIVE NPU DIE EXEC ----------------
ticket_dir = pathlib.Path('/tmp/opsb-npu-tickets')
ticket_dir.mkdir(parents=True, exist_ok=True)
my_pid = os.getpid()
wait_t0 = time.time()
ticket_path = ticket_dir / f'{my_pid}.json'

def update_ticket():
    wait_s = max(0.0, time.time() - wait_t0)
    age = wait_s / max(60.0, aging_tau)
    mlfq_now = 'Q0*' if (not fast and wait_s >= 1800) else mlfq
    try:
        ticket_path.write_text(json.dumps({
            'pid': my_pid, 'title': title, 'sha': sha,
            'tier': 2 if mlfq_now in ('Q0', 'Q0*') else 1,
            'prio': round(wspt_base + age, 4),
            'fast': fast,
            'created': wait_t0,
        }))
    except Exception:
        pass

def am_top_ticket():
    update_ticket()
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
            if not pathlib.Path(f'/proc/{opid}').exists():
                tf.unlink(missing_ok=True)
                continue
            if (other['tier'], other['prio'], -other['created']) > (mine['tier'], mine['prio'], -mine['created']):
                return False
        except Exception:
            continue
    return True

legacy_lock = (root.parent / (work_prefix + '-validation.lock')).open('a')
fcntl.flock(legacy_lock, fcntl.LOCK_SH)
devices = int(os.environ.get('TASK_DEVICES', '1'))
wait_budget = int(os.environ.get('TASK_LOCK_WAIT', '3600')) * 60
deadline = time.monotonic() + wait_budget
slot = None
announced = False

try:
    while slot is None:
        if am_top_ticket():
            for index in range(devices):
                handle = (root.parent / ('%s-device%d.lock' % (work_prefix, index))).open('a')
                try:
                    if devices == 1:
                        if not wait_as_queue_head(handle, ticket_dir, am_top_ticket, deadline):
                            handle.close()
                            continue
                    else:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    slot = (index, handle)
                    break
                except BlockingIOError:
                    handle.close()
        if slot is None:
            if not announced:
                emit('Waiting for a free NPU die')
                announced = True
            if time.monotonic() > deadline:
                emit('Timed out waiting for an NPU die')
                sys.exit(3)
            time.sleep(2.5)
finally:
    ticket_path.unlink(missing_ok=True)

die_idx, die_handle = slot
die_t0 = time.monotonic()
die_started_epoch = round(time.time(), 3)
die_started_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
die_sess_file = opsb_home / 'die_session.json'
case_progress = None

def write_die_session(substage):
    try:
        temp = die_sess_file.with_suffix('.tmp-' + str(my_pid))
        temp.write_text(json.dumps({
            'die': die_idx,
            'pid': my_pid,
            'title': title,
            'sha': sha,
            'mode': 'Q0' if fast else 'Q1',
            'phase': substage,
            'substage': substage,
            'started_epoch': die_started_epoch,
            'started_ts': die_started_iso,
            'case_progress': case_progress,
        }))
        temp.replace(die_sess_file)
    except Exception:
        pass

write_die_session('acl_setup')
env[visible_env] = str(die_idx)
emit('Using NPU die %d (%s=%d)' % (die_idx, visible_env, die_idx))

# Activate PersistentArenaRunner in-process execution if deployed
arena_stats_file = pathlib.Path(tempfile.mktemp(prefix=f'opsb-arena-{title}-', suffix='.jsonl'))
if (runtime_dir / 'sitecustomize.py').is_file() and env.get('OPSB_ARENA_ACCEL', '1') != '0':
    env['OPSB_ARENA_ACCEL'] = '1'
    env['OPSB_ARENA_STATS_FILE'] = str(arena_stats_file)
    cur_pp = env.get('PYTHONPATH', '')
    env['PYTHONPATH'] = f"{runtime_dir}:{cur_pp}" if cur_pp else str(runtime_dir)

if cold3:
    env['OPSB_COLD3'] = '1'
    emit('COLD3_MODE=1')

if pmu:
    env['OPSB_PMU'] = '1'
    emit('PMU_MODE=1')

if fast:
    env['FAST_LANE'] = '1'
    env['OPSB_FAST_LANE'] = '1'
    env['CONCATD_FAST'] = '1'
    env['FAST'] = '1'
    env['PERF_REF'] = '0'
    if title == 'LayerNormV3':
        env.setdefault('PERF_CASES', 'f32_4x6x8 f32_1024x1024 bf16_1024x1024')
    elif title == 'QuantUpdateScatter':
        env.setdefault('PERF_CASES', 'sample c1024_r1024 kv_decode_512')
    elif title == 'CTCLossV2':
        env.setdefault('KPERF_CASE', 't4_c5_s1,t50_c32_s20,t100_c512_s40')
    elif title == 'StridedSliceGrad':
        env.setdefault('KPERF_CASES', 'sample_r4 full_r2_big r3_mid_slice fp16_mid')
    elif title == 'PReluGradUpdate':
        env.setdefault('PRLGU_DEV_CASES', 'sample_4_6_8,4d_16_64_32_32')

# Transform run-npu.sh in-memory for minimal Phase-B Die-0 hold:
#  1) Replace redundant SOC aclInit probe with static cached SoC (saves 2-3.5s)
#  2) Replace build.sh -j4 -e with ONLY the prebuilt sample binary (deferring sample.py verify to Phase C, saves 5-10s)
#  3) Strip plan_check.py (already passed in Phase A)
#  4) When Phase-A extended case cache is ready, replace extended.py with arena_cache.py replay
#     and defer golden/determinism verification to lock-free Phase C (saves 5-45s)
phase_c_queue = pathlib.Path(f'/tmp/opsb-phasec-{title}-{my_pid}.jsonl')
phase_c_queue.unlink(missing_ok=True)
env['OPSB_PHASE_C_QUEUE'] = str(phase_c_queue)
env['OPSB_RUNTIME_SOC'] = env.get('OPSB_RUNTIME_SOC', soc_default)
env['OPSB_RUNTIME_DIR'] = str(runtime_dir)

run_sh_path = repo / 'validation' / 'run-npu.sh'
orig_script = run_sh_path.read_text(encoding='utf-8')
exec_script = orig_script

# 1) Eliminate Python aclInit SOC probe inside Die-0 lock
exec_script = re.sub(
    r"SOC=\$\(\s*python3\s+-\s+<<'SOC_PY'.*?SOC_PY\s*\)",
    f'SOC="${{OPSB_RUNTIME_SOC:-{soc_default}}}"',
    exec_script,
    count=1,
    flags=re.S,
)

# 2) Skip redundant build.sh & cmake/torch_npu sample.py inside Die-0 lock
prebuilt_libs = glob.glob(str(repo / 'build/packages/vendors/*_custom/op_api/lib/libcust_opapi.so'))
sample_bin = repo / 'examples' / 'build' / 'sample'
sample_build_dir = repo / 'samples' / 'build'
build_needle = 'sed "s/COMPUTE_UNIT=\\"' + build_target + '\\"/COMPUTE_UNIT=\\"$TARGET\\"/" build.sh | bash -s -- -j4 -e'
if prebuilt_libs and build_needle in exec_script:
    if (not fast) and ext_py.is_file() and sample_bin.is_file() and env.get('OPSB_RUN_SAMPLE_BIN', '0') != '1':
        # Phase A already compiled & linked examples/build/sample against libcust_opapi.so,
        # and extended.py tests samples/case.json plus full coverage. Skipping a separate
        # sample process in Q1 saves 2.5-3.9s of redundant aclInit + .o load + aclrtResetDevice.
        replacement = (
            'echo "[INFO] Sample binary compiled in Phase A; skipping redundant Q1 sample process '
            '(covered by extended suite)"'
        )
    elif sample_bin.is_file() and sample_build_dir.is_dir():
        replacement = (
            'bash -c \'set -e; echo "[INFO] Running prebuilt sample binary (0s cmake/verify in lock)..."; '
            'VDIR=$(ls -d "$PWD"/build/packages/vendors/*_custom | head -n 1); '
            '@@EXTRA@@="$VDIR${@@EXTRA@@:+:$@@EXTRA@@}" '
            'LD_LIBRARY_PATH="$VDIR/op_api/lib:$@@HOME@@/lib64:${LD_LIBRARY_PATH:-}" '
            '"$PWD/examples/build/sample" "$PWD/samples/build" "${DEVICE_ID:-0}"\''
        ).replace('@@EXTRA@@', extra_ops_env).replace('@@HOME@@', toolchain_home_env)
        with phase_c_queue.open('a', encoding='utf-8') as qf:
            qf.write(json.dumps({
                'kind': 'cmd',
                'label': 'sample_verify',
                'cmd': ['python3', 'samples/sample.py', 'verify', '--directory', 'samples/build'],
                'cwd': str(repo),
                'fatal': (title != 'LayerNormV3'),
            }) + '\n')
    else:
        replacement = (
            'bash -c \'set -e; echo "[INFO] Running examples (Phase-A prebuilt, 0.0s lock compile)..."; '
            'bash examples/run.sh; echo "[INFO] Example completed successfully!"\''
        )
    exec_script = exec_script.replace(build_needle, replacement, 1)

# 3) Skip plan_check.py inside Die-0 lock (already verified in Phase A)
exec_script = re.sub(r'(?m)^\s*python3\s+validation/plan_check\.py\b.*$', 'true # plan_check done in Phase A', exec_script)

# 4) Replace extended.py with pure-NPU cached replay + Phase-C lock-free golden verify
arena_out_dir = None
if use_arena_cache and arena_cache_dir is not None and not env.get('EXTENDED_FILTER'):
    arena_out_dir = pathlib.Path(f'/tmp/opsb-out-{title}-{my_pid}')
    replay_cmd = (
        f'python3 {runtime_dir}/arena_cache.py replay '
        f'--repo "$PWD" --cache {arena_cache_dir} --out {arena_out_dir}'
    )
    new_exec = re.sub(r'\bpython3\s+validation/extended\.py\b', replay_cmd, exec_script, count=1)
    if new_exec != exec_script:
        exec_script = new_exec
        with phase_c_queue.open('a', encoding='utf-8') as qf:
            qf.write(json.dumps({
                'kind': 'cmd',
                'label': 'arena_verify',
                'cmd': [
                    'python3', str(runtime_dir / 'arena_cache.py'), 'verify',
                    '--cache', str(arena_cache_dir),
                    '--out', str(arena_out_dir),
                    '--title', title,
                ],
                'cwd': str(repo),
                'fatal': True,
            }) + '\n')

if exec_script != orig_script:
    cmd = ['bash', '-c', exec_script, str(run_sh_path)]
else:
    cmd = ['bash', 'validation/run-npu.sh']
lock_waste_sec = 0.0

cur_substage = 'acl_setup'
profiler_us_sum = 0.0
profiler_launches = 0
cold_first_samples = []
cold_spread_samples = []
cold3_per_case = {}
cold3_warm_samples = []

def _scan_perf_line(line):
    global profiler_us_sum, profiler_launches
    m_us = re.search(r'(?:median_us|kernel_median_us|median)[=:"\s]+([\d.]+)', line)
    if m_us:
        try:
            profiler_us_sum += float(m_us.group(1))
            profiler_launches += 1
        except ValueError:
            pass
    s = line.strip()
    if s.startswith('COLD3_CASE '):
        try:
            cobj = json.loads(s[len('COLD3_CASE '):].strip())
            if isinstance(cobj, dict):
                cname = str(cobj.get('case') or cobj.get('label') or '')
                runs = cobj.get('runs') or cobj.get('runs_us') or cobj.get('cold3_runs')
                if cname and isinstance(runs, list) and len(runs) >= 3:
                    vals = [float(x) for x in runs[:3]]
                    cold3_per_case[cname] = vals
                    cold_first_samples.append(vals[0])
                    cold_spread_samples.append(max(vals) - min(vals))
                    cold3_warm_samples.append(min(vals[1], vals[2]))
        except Exception:
            pass
        return
    if s.startswith(('KPERF_JSON ', 'KERNEL_JSON ', 'KERNEL_PERF ', 'PERF ')):
        try:
            payload = json.loads(s.split(' ', 1)[1].strip())
            items = payload if isinstance(payload, list) else ([payload] if isinstance(payload, dict) else [])
            for it in items:
                if not isinstance(it, dict) or it.get('mode') == 'torch' or it.get('source') == 'torch':
                    continue
                cname = str(it.get('case') or it.get('label') or '')
                if isinstance(it.get('ops'), dict) and it['ops']:
                    it = next(iter(it['ops'].values()))
                    if not isinstance(it, dict):
                        continue
                fi = it.get('first_iter_us') or it.get('cold_us')
                mx = it.get('max_us') or it.get('kernel_max_us')
                mn = it.get('min_us') or it.get('kernel_min_us')
                med = it.get('median_us') or it.get('kernel_median_us')
                runs = it.get('cold3_runs') or it.get('runs_us') or it.get('runs')
                if isinstance(runs, list) and len(runs) >= 3 and cname:
                    vals = [float(x) for x in runs[:3]]
                    cold3_per_case[cname] = vals
                    cold_first_samples.append(vals[0])
                    cold_spread_samples.append(max(vals) - min(vals))
                    cold3_warm_samples.append(float(med) if isinstance(med, (int, float)) and med > 0 else min(vals[1], vals[2]))
                else:
                    if isinstance(fi, (int, float)) and fi > 0:
                        cold_first_samples.append(float(fi))
                    elif isinstance(mx, (int, float)) and mx > 0:
                        cold_first_samples.append(float(mx))
                    if isinstance(mx, (int, float)) and isinstance(mn, (int, float)) and mx >= mn > 0:
                        cold_spread_samples.append(float(mx) - float(mn))
                    if isinstance(med, (int, float)) and med > 0:
                        cold3_warm_samples.append(float(med))
                        if cold3 and cname and cname not in cold3_per_case:
                            r1 = float(fi) if isinstance(fi, (int, float)) and fi > 0 else (float(mx) if isinstance(mx, (int, float)) and mx > 0 else float(med))
                            r3 = float(mn) if isinstance(mn, (int, float)) and mn > 0 else float(med)
                            cold3_per_case[cname] = [round(r1, 3), round(float(med), 3), round(r3, 3)]
        except Exception:
            pass
    elif s.startswith(('MS_PROF ', 'KPERF ')):
        m_cname = re.search(r'^(?:MS_PROF\s+case=|KPERF\s+)(\S+)', s)
        m_med = re.search(r'\bmedian=\s*([\d.]+)', s)
        m_mn = re.search(r'\bmin=\s*([\d.]+)', s)
        m_mx = re.search(r'\bmax=\s*([\d.]+)', s)
        if m_med:
            try:
                cold3_warm_samples.append(float(m_med.group(1)))
            except ValueError:
                pass
        if m_mx:
            try:
                mx_v = float(m_mx.group(1))
                cold_first_samples.append(mx_v)
                if m_mn:
                    mn_v = float(m_mn.group(1))
                    if mx_v >= mn_v > 0:
                        cold_spread_samples.append(mx_v - mn_v)
                    if cold3 and m_cname and m_med and m_cname.group(1) not in cold3_per_case:
                        med_v = float(m_med.group(1))
                        cold3_per_case[m_cname.group(1)] = [round(mx_v, 3), round(med_v, 3), round(mn_v, 3)]
            except ValueError:
                pass

phase_b_timeout_sec = int(env.get('OPSB_PHASE_B_TIMEOUT_SEC', '180'))
phase_b_timed_out = False
watchdog_timer = None
try:
    p = subprocess.Popen(
        cmd,
        cwd=repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )

    def _kill_hung_phase_b():
        global phase_b_timed_out
        phase_b_timed_out = True
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    watchdog_timer = threading.Timer(phase_b_timeout_sec, _kill_hung_phase_b)
    watchdog_timer.daemon = True
    watchdog_timer.start()

    for line in p.stdout:
        log_f.write(line)
        log_f.flush()
        print(line, end='', flush=True)
        low = line.lower()
        counter = re.fullmatch(r'OPSB_REPLAY_CASE (\d+)/(\d+)', line.strip())
        if counter:
            idx, total = map(int, counter.groups())
            if 0 < idx <= total:
                case_progress = dict(index=idx, total=total, phase='execute')
                cur_substage = 'npu_exec'
                write_die_session(cur_substage)
        elif line.strip() == 'OPSB_REPLAY_DONE':
            case_progress = None
            write_die_session(cur_substage)
        if ('profiler' in low or 'kperf' in low or 'kernel_json' in low or 'ms_prof' in low or 'prof_summary' in low):
            if cur_substage != 'profiler':
                cur_substage = 'profiler'
                write_die_session('profiler')
        elif ('extended' in low or 'case ' in low or 'running examples' in low or 'running prebuilt sample' in low or 'replayed ' in low or 'stress' in low):
            if cur_substage == 'acl_setup':
                cur_substage = 'npu_exec'
                write_die_session('npu_exec')
        _scan_perf_line(line)
    code = p.wait()
    if phase_b_timed_out:
        emit(f'PHASE_B_WATCHDOG_TIMEOUT: killed hung NPU kernel after {phase_b_timeout_sec}s to release Die 0')
        emit('VALIDATION_FAILED')
        code = 124
finally:
    if watchdog_timer is not None:
        watchdog_timer.cancel()
    phase_b_sec = round(time.monotonic() - die_t0, 3)
    die_sess_file.unlink(missing_ok=True)
    try:
        fcntl.flock(die_handle, fcntl.LOCK_UN)
        die_handle.close()
    except Exception:
        pass
    try:
        fcntl.flock(legacy_lock, fcntl.LOCK_UN)
        legacy_lock.close()
    except Exception:
        pass
    try:
        fcntl.flock(repo_lock_f, fcntl.LOCK_UN)
        repo_lock_f.close()
    except Exception:
        pass

# ---------------- PHASE C: LOCK-FREE GOLDEN VERIFY + PROFILER EXPORT/PARSE (0.0s NPU LOCK) ----------------
phase_c_t0 = time.monotonic()
if phase_c_queue.is_file():
    try:
        q_lines = [l.strip() for l in phase_c_queue.read_text(encoding='utf-8').splitlines() if l.strip()]
    except Exception:
        q_lines = []
    phase_c_queue.unlink(missing_ok=True)

    for raw_item in q_lines:
        try:
            item = json.loads(raw_item)
        except Exception:
            continue
        kind = item.get('kind')
        if kind == 'cmd':
            c_cwd = item.get('cwd') or str(repo)
            cp = subprocess.Popen(item['cmd'], cwd=c_cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in cp.stdout:
                log_f.write(line)
                log_f.flush()
                print(line, end='', flush=True)
                _scan_perf_line(line)
            crc = cp.wait()
            if crc != 0 and item.get('fatal', True):
                code = crc
        elif kind == 'wait_profiler':
            mpid = int(item.get('pid') or 0)
            wait_end = time.monotonic() + 240.0
            while mpid > 0 and pathlib.Path(f'/proc/{mpid}').exists() and time.monotonic() < wait_end:
                time.sleep(0.05)
            parse_cmd = item.get('parse_cmd') or ''
            if parse_cmd:
                penv = env.copy()
                penv.update(item.get('env') or {})
                pcwd = item.get('cwd') or str(repo)
                pp = subprocess.Popen(['bash', '-c', parse_cmd], cwd=pcwd, env=penv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                for line in pp.stdout:
                    log_f.write(line)
                    log_f.flush()
                    print(line, end='', flush=True)
                    _scan_perf_line(line)
                pp.wait()
            if not item.get('keep', False):
                pdir = item.get('prof_dir')
                if pdir:
                    shutil.rmtree(pdir, ignore_errors=True)
                lf = item.get('log_file')
                if lf:
                    try:
                        pathlib.Path(lf).unlink()
                    except OSError:
                        pass

phase_c_sec = round(time.monotonic() - phase_c_t0, 3)
first_iter_us = None
cold_spread_median_us = None
if cold_first_samples:
    sf = sorted(cold_first_samples)
    first_iter_us = round(sf[len(sf) // 2], 3)
if cold_spread_samples:
    ss = sorted(cold_spread_samples)
    cold_spread_median_us = round(ss[len(ss) // 2], 3)
if first_iter_us is not None or cold_spread_median_us is not None:
    emit('COLD_START_STATS ' + json.dumps({
        'cases': len(cold_first_samples),
        'first_iter_us': first_iter_us,
        'cold_spread_median_us': cold_spread_median_us,
    }, sort_keys=True))

cold_3run_mean_us = None
cold3_stats_payload = None
if cold3 or cold3_per_case:
    c3_means = sorted((v[0] + v[1] + v[2]) / 3.0 for v in cold3_per_case.values()) if cold3_per_case else []
    warm_med_us = round(sorted(cold3_warm_samples)[len(cold3_warm_samples) // 2], 3) if cold3_warm_samples else None
    if c3_means:
        cold_3run_mean_us = round(c3_means[len(c3_means) // 2], 3)
    elif first_iter_us is not None and warm_med_us is not None:
        cold_3run_mean_us = round((first_iter_us + 2.0 * warm_med_us) / 3.0, 3)
    bimodal_spike = any(
        (v[2] > 0 and (v[0] / v[2]) >= 1.45 and (v[0] - v[2]) >= 1.5)
        for v in cold3_per_case.values()
    )
    if not bimodal_spike and first_iter_us is not None and warm_med_us is not None and warm_med_us > 0:
        bimodal_spike = ((first_iter_us / warm_med_us) >= 1.45 and (first_iter_us - warm_med_us) >= 1.5)
    cold3_stats_payload = {
        'cases': len(cold3_per_case) or len(cold_first_samples),
        'first_iter_us': first_iter_us,
        'cold_3run_mean_us': cold_3run_mean_us,
        'warm_median_us': warm_med_us,
        'cold_spread_median_us': cold_spread_median_us,
        'bimodal_spike_detected': bimodal_spike,
        'per_case': cold3_per_case,
    }
    emit('COLD3_STATS ' + json.dumps(cold3_stats_payload, sort_keys=True))
emit(f'[PHASE-C] Lock-free verify + profiler parse finished in {phase_c_sec:.2f}s (Phase-B Die-0 hold was {phase_b_sec:.2f}s)')
log_f.close()

arena_inproc = 0
arena_fallback = 0
arena_acl_sec = 0.0
if arena_stats_file.is_file():
    try:
        for sline in arena_stats_file.read_text(encoding='utf-8').splitlines():
            if sline.strip():
                sobj = json.loads(sline)
                arena_inproc += int(sobj.get('cases_inproc', 0))
                arena_fallback += int(sobj.get('cases_fallback', 0))
                arena_acl_sec += float(sobj.get('acl_wall_sec', 0.0))
    except Exception:
        pass
    arena_stats_file.unlink(missing_ok=True)

# Estimate NPU active device execution window vs host/ACL setup inside Phase B
raw_kernel_sec = (profiler_us_sum * 12.0 + arena_inproc * 250.0) / 1e6
npu_exec_sec = round(min(phase_b_sec, max(0.05 if phase_b_sec > 0 else 0.0, raw_kernel_sec + 0.18 * profiler_launches)), 3)
acl_host_sec = round(max(0.0, phase_b_sec - npu_exec_sec - lock_waste_sec), 3)
end_ep = round(time.time(), 3)
end_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

try:
    with (opsb_home / 'die_ledger.jsonl').open('a', encoding='utf-8') as lfh:
        lfh.write(json.dumps({
            'ts': end_ep,
            'ended_epoch': end_ep,
            'ended_at': end_iso,
            'title': title,
            'sha': sha,
            'mode': 'Q0' if fast else 'Q1',
            'phase_a_cpu_sec': phase_a_sec,
            'phase_b_die_sec': phase_b_sec,
            'phase_c_cpu_sec': phase_c_sec,
            'hold_sec': phase_b_sec,
            'npu_exec_sec': npu_exec_sec,
            'acl_host_sec': acl_host_sec,
            'lock_waste_sec': lock_waste_sec,
            'arena_cases_inproc': arena_inproc,
            'arena_cases_fallback': arena_fallback,
            'rc': code,
        }) + '\n')
except Exception:
    pass

res_payload = {
    'title': title,
    'sha': sha,
    'exit_code': code,
    'fast_lane': fast,
    'cold3': cold3,
    'pmu': pmu,
    'phase_a_cpu_sec': phase_a_sec,
    'phase_b_die_sec': phase_b_sec,
    'phase_c_cpu_sec': phase_c_sec,
    'hold_sec': phase_b_sec,
    'npu_exec_sec': npu_exec_sec,
    'acl_host_sec': acl_host_sec,
    'arena_cases_inproc': arena_inproc,
    'kernel_o_count': kernel_o_count,
    'kernel_o_max_bytes': kernel_o_max_bytes,
    'kernel_o_sum_bytes': kernel_o_sum_bytes,
    'kernel_o_max_text_bytes': kernel_o_max_text_bytes,
    'max_hot_symbol_bytes': max_hot_symbol_bytes,
    'top_symbols': top_symbols,
    'template_instantiations': template_instantiations,
    'first_iter_us': first_iter_us,
    'cold_3run_mean_us': cold_3run_mean_us,
    'cold_spread_median_us': cold_spread_median_us,
    'cold3_stats': cold3_stats_payload,
    'finished_at': end_iso,
}
(logdir / res_name).write_text(json.dumps(res_payload, indent=2) + '\n')
print('OPSB_PHASE_METRICS ' + json.dumps(res_payload), flush=True)
sys.exit(code)
'''


def expected_origin_url(workspace: Path, problem: dict) -> str:
    """Canonical remote for a problem repo.

    `opsb.workspace.json` is authoritative when present (clean-agents-first layout:
    `git_owner` + per-problem `repo_slug`, e.g. `accel-mat-<slug>`). Workspaces without
    that manifest keep the legacy `<prefix>-<slug>` naming.
    """
    manifest_path = workspace / "opsb.workspace.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text())
            owner = manifest.get("git_owner") or "winbeau"
            for entry in manifest.get("problems") or []:
                if entry.get("title") == problem.get("title") and entry.get("repo_slug"):
                    return f"https://gitcode.com/{owner}/{entry['repo_slug']}.git"
        except Exception:
            pass
    template = DEVICE.get("repo_url_template") or "https://git.invalid/{prefix}-{slug}.git"
    return template.format(prefix=DEVICE.get("work_prefix") or "bench", slug=problem["slug"])


def load_problem_manifest(workspace: Path) -> list[dict]:
    """Clean-layout manifest first, legacy `<prefix>/problems.json` as fallback."""
    prefix = DEVICE.get("work_prefix") or "bench"
    clean = workspace / "opsb.workspace.json"
    if clean.is_file():
        try:
            data = json.loads(clean.read_text())
            rows = [r for r in (data.get("problems") or []) if isinstance(r, dict) and r.get("title")]
            if rows:
                out = []
                for r in rows:
                    row = dict(r)
                    row.setdefault("path", row.get("path") or row["title"])
                    row.setdefault("repo_slug", row.get("repo_slug") or "")
                    out.append(row)
                return out
        except Exception:
            pass
    legacy = workspace / prefix / "problems.json"
    if legacy.is_file():
        try:
            rows = json.loads(legacy.read_text())
            out = []
            for r in rows:
                row = dict(r)
                row.setdefault("path", row.get("path") or f"{prefix}/{row['title']}")
                row.setdefault("repo_slug", row.get("repo_slug") or f"{prefix}-{row.get('slug', row['title'].lower())}")
                out.append(row)
            return out
        except Exception:
            pass
    return []


REMOTE_TEMPLATE = r"""
import glob, json, os, pathlib, shlex, shutil, subprocess, sys, tempfile, time, fcntl

cfg = json.load(sys.stdin)
title, sha = cfg['title'], cfg['sha']
exec(cfg.pop('lanes_source'))
cfg['run_id'] = f"{os.getpid()}-{time.time_ns()}"
_dev = cfg.get('device') or {}
toolchain_root = _dev.get('toolchain_root') or ''
sdk_glob = _dev.get('sdk_glob') or 'sdk-*'
driver_root = _dev.get('driver_root') or ''
profiler_bin = _dev.get('profiler_bin') or 'profiler'
profiler_acl_flag = _dev.get('profiler_acl_flag') or '--acl'
extra_ops_env = _dev.get('extra_ops_env') or 'EXTRA_OPS_PATH'
toolchain_home_env = _dev.get('toolchain_home_env') or 'TOOLCHAIN_HOME'

def _parse_task_durations(out_dir):
    import csv
    rows = []
    # SDK 9.1 writes task_time_*.csv; older toolchains write op_summary_*.csv.
    candidates = sorted(pathlib.Path(out_dir).rglob('task_time_*.csv')) + \
        sorted(pathlib.Path(out_dir).rglob('op_summary_*.csv'))
    for csv_path in candidates:
        try:
            with csv_path.open(newline='', encoding='utf-8', errors='replace') as fh:
                for record in csv.DictReader(fh):
                    keys = {str(k).strip(): k for k in (record or {}) if k}
                    dur_key = next((k for k in keys if 'Task Duration' in k), '')
                    if not dur_key:
                        dur_key = next((k for k in keys if k.lower().startswith('task_time')), '')
                    if not dur_key:
                        continue
                    type_key = next((k for k in keys if k.strip() in ('Task Type', 'kernel_type')), '')
                    task_type = str(record.get(keys[type_key], '') if type_key else '').upper()
                    if task_type and not any(tag in task_type for tag in ('AICORE', 'AI_CORE', 'AIV', 'AIC', 'MIX', 'VECTOR')):
                        continue
                    try:
                        value = float(str(record[keys[dur_key]]).strip())
                    except (TypeError, ValueError):
                        continue
                    if value > 0:
                        rows.append(value)
        except OSError:
            continue
    return rows


def _profiler_path():
    shell = (f'source {accel_env} && ' if accel_env else '') + f'command -v {profiler_bin}'
    probe = subprocess.run(['bash', '-lc', shell], capture_output=True, text=True, env=env, timeout=60)
    hits = [ln.strip() for ln in (probe.stdout or '').splitlines() if ln.strip()]
    if hits:
        return hits[-1]
    for pattern in (f'{toolchain_root}/{sdk_glob}/tools/profiler/bin/{profiler_bin}',
                    f'{driver_root}/*/*/tools/profiler/bin/{profiler_bin}',
                    f'{driver_root}/*/tools/profiler/bin/{profiler_bin}'):
        found = sorted(glob.glob(pattern))
        if found:
            return found[-1]
    return shutil.which(profiler_bin) or ''


def _run_profiler(repo, build_dir, application, pmu, out_dir):
    # Profile one ./sample invocation; return Task Duration stats (us).
    profiler = _profiler_path()
    if not profiler:
        print('[TEMPLATE] profiler not found; keeping wall-clock timing', flush=True)
        return {}
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    flags = ['--task-time=l1' if pmu else '--task-time=l0',
             '--ai-core=on' if pmu else '--ai-core=off',
             '--aicpu=off', f'{profiler_acl_flag}=off', '--runtime-api=off']
    if pmu:
        flags.append('--aic-metrics=PipeUtilization')
    cmd = [profiler, f'--application={application}', f'--output={out_dir}', *flags]
    print(f'[TEMPLATE] profiler profiling: {" ".join(cmd)}', flush=True)
    shell_cmd = (f'source {accel_env} && ' if accel_env else '') + ' '.join(shlex.quote(part) for part in cmd)
    proc = subprocess.run(['bash', '-lc', shell_cmd], cwd=str(build_dir), env=env,
                          capture_output=True, text=True, timeout=600)
    tail = (proc.stdout or '') + (proc.stderr or '')
    for line in tail.splitlines()[-8:]:
        print('[TEMPLATE][profiler] ' + line, flush=True)
    durations = _parse_task_durations(out_dir)
    if not durations:
        print('[TEMPLATE] profiler produced no AI Core task durations', flush=True)
        return {}
    durations.sort()
    mid = len(durations) // 2
    median = durations[mid] if len(durations) % 2 else (durations[mid - 1] + durations[mid]) / 2.0
    return {'profiler_us_median': round(median, 3), 'profiler_us_min': round(durations[0], 3),
            'profiler_us_max': round(durations[-1], 3), 'profiler_samples': len(durations)}
root = pathlib.Path(cfg['remote_root'])
repo = root / title
build_dir = repo / 'build-npu'
accel_env = cfg.get('accel_env') or ''
soc = cfg.get('soc') or _dev.get('soc') or 'soc-a'
device_id = cfg.get('device_id', 0)
arch = cfg.get('arch') or _dev.get('arch') or 'arch-v1'

env = os.environ.copy()
env['GIT_TERMINAL_PROMPT'] = '0'
# Publish the configured environment-variable *names* so the in-process helpers
# (arena cache, profiler wait, environment probe) address the toolchain's own vars.
env['OPSB_EXTRA_OPS_ENV'] = extra_ops_env
env['OPSB_HOME_ENV'] = toolchain_home_env
env['OPSB_RUNTIME_LIB'] = _dev.get('lib_runtime') or 'libruntime.so'

def sh(cmd, cwd=None, check=True, timeout=None):
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env,
                          capture_output=True, text=True, timeout=timeout)
    for stream, sink in ((proc.stdout, sys.stdout), (proc.stderr, sys.stderr)):
        if stream:
            sink.write(stream)
            sink.flush()
    if check and proc.returncode != 0:
        print(f'[TEMPLATE] command failed ({proc.returncode}): {" ".join(cmd)}', flush=True)
        raise SystemExit(proc.returncode or 1)
    return proc

def bash(script, cwd=None, check=True, timeout=None):
    return sh(['bash', '-lc', (f'source {accel_env} && ' if accel_env else '') + script],
              cwd=cwd, check=check, timeout=timeout)

root.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix='git-auth-') as auth, (root / (title + '.lock')).open('a') as repo_lock:
    fcntl.flock(repo_lock, fcntl.LOCK_EX)
    askpass = pathlib.Path(auth) / 'askpass.py'
    askpass.write_text(
        '#!/usr/bin/env python3\nimport os,sys\n'
        'print(os.environ["TASK_GIT_USERNAME" if "username" in sys.argv[1].lower() else "TASK_GIT_PASSWORD"])\n'
    )
    askpass.chmod(0o700)
    env.update(GIT_ASKPASS=str(askpass),
               TASK_GIT_USERNAME=cfg.pop('username', ''),
               TASK_GIT_PASSWORD=cfg.pop('password', ''))

    if not (repo / '.git').is_dir():
        root.mkdir(parents=True, exist_ok=True)
        print(f'[TEMPLATE] cloning {cfg["url"]} -> {repo}', flush=True)
        sh(['git', 'clone', cfg['url'], str(repo)])
    sh(['git', 'fetch', 'origin', '--prune'], cwd=repo)
    sh(['git', 'checkout', '--detach', '--force', sha], cwd=repo)

    if build_dir.exists():
        shutil.rmtree(build_dir)
    t_a = time.monotonic()
    with compile_lane(cfg, units=1):
        print('[TEMPLATE] cmake configure', flush=True)
        bash(f'cmake -S . -B build-npu -DCMAKE_EXPORT_COMPILE_COMMANDS=ON -DCMAKE_ASC_RUN_MODE=npu -DCMAKE_ASC_ARCHITECTURES={arch}', cwd=repo)
    with compile_lane(cfg, units=compilation_units(build_dir)) as jobs:
        print(f'[TEMPLATE] cmake build (-j{jobs})', flush=True)
        bash(f'cmake --build build-npu --parallel {jobs}', cwd=repo)
    print('[TEMPLATE] generating sample data', flush=True)
    bash('python3 sample.py generate --directory build-npu', cwd=repo)
    phase_a = round(time.monotonic() - t_a, 3)

    metrics = {'layout': 'template', 'phase_a_cpu_sec': phase_a, 'soc': soc}
    if cfg.get('build_only'):
        metrics['build_only'] = True
        print('OPSB_PHASE_METRICS ' + json.dumps(metrics), flush=True)
        raise SystemExit(0)

    with device_lane(cfg):
        t_b = time.monotonic()
        bash(f'cd build-npu && ./sample {device_id}', cwd=repo)
        run_wall = round(time.monotonic() - t_b, 3)
        prof = {}
        if cfg.get('profiler', True):
            prof = _run_profiler(repo, build_dir, f'./sample {device_id}', bool(cfg.get('pmu')), build_dir / 'prof')
        verified = True
        try:
            bash('python3 sample.py verify --directory build-npu', cwd=repo)
        except SystemExit:
            verified = False

    print(f'RUNTIME_SOC={soc}')
    if prof:
        print(f'MS_PROF case={title} median={prof["profiler_us_median"]:.1f} min={prof["profiler_us_min"]:.1f}')
        metrics.update(npu_exec_sec=round(prof['profiler_us_median'] / 1e6, 6), timing_source='profiler', **prof)
    else:
        # Without hardware counters we refuse to fabricate an MS_PROF row: a wall-clock number
        # would be treated as device evidence by parse_validation_perf.
        metrics.update(npu_exec_sec=run_wall, timing_source='wall')
    print(f'[TEMPLATE] run_wall_sec={run_wall} verified={verified} timing_source={metrics["timing_source"]}')
    metrics.update(verified=verified)
    print('OPSB_PHASE_METRICS ' + json.dumps(metrics), flush=True)
    raise SystemExit(0 if verified else 2)
"""


def _remote_root_for(workspace: Path, manifest: dict) -> str:
    """Remote contest root: manifest override, else `<configured work root>/<contest>`."""
    override = str(manifest.get("npu_remote_root") or "").strip()
    if override:
        return override.rstrip("/")
    name = str(manifest.get("contest_id") or "").strip() or workspace.name
    return (DEVICE.get("work_root") or "/srv/work").rstrip("/") + f"/{name}"


def _stream_remote(script: str, cfg: dict, destination: Path, log_basename: str, stream_stdout: bool) -> tuple[int, dict]:
    """Run the remote helper over ssh, teeing its log and capturing OPSB_PHASE_METRICS."""
    remote_metrics: dict = {}
    with (destination / (log_basename + ".log")).open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", cfg["npu_host"], "python3 -c " + shlex.quote(script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(json.dumps(cfg))
        proc.stdin.close()
        for line in proc.stdout:
            if line.startswith("OPSB_PHASE_METRICS "):
                try:
                    remote_metrics = json.loads(line[len("OPSB_PHASE_METRICS "):].strip())
                except Exception:
                    pass
                continue
            log.write(line)
            log.flush()
            if stream_stdout:
                print(line, end="", flush=True)
        return proc.wait(), remote_metrics

def run_3phase_validation(
    title: str,
    sha_override: str | None = None,
    sync_only: bool = False,
    fast: bool = False,
    build_only: bool = False,
    cold3: bool = False,
    pmu: bool = False,
    npu_host: str = "npu",
    workspace: Path = ROOT,
    stream_stdout: bool = True,
) -> dict:
    problems = load_problem_manifest(workspace)
    problem = next((p for p in problems if p["title"] == title), None)
    if problem is None:
        raise ValueError(f"Unknown title: {title}")

    repo = workspace / problem["path"]
    if not sha_override and git(repo, "status", "--porcelain"):
        raise RuntimeError("Commit local changes before server validation")
    url = expected_origin_url(workspace, problem)
    if git(repo, "remote", "get-url", "origin") != url:
        raise RuntimeError("Unexpected local origin")

    if sha_override:
        sha = git(repo, "rev-parse", sha_override)
    else:
        sha = git(repo, "rev-parse", "HEAD")
        remote = git(repo, "ls-remote", "origin", "refs/heads/main").split()
        if not remote or remote[0] != sha:
            raise RuntimeError("Push local HEAD to origin/main before validation")

    fast_mode = bool(fast or os.environ.get("OPSB_FAST_LANE") == "1")
    cold3_mode = bool(cold3 or os.environ.get("OPSB_COLD3") == "1")
    pmu_mode = bool(pmu or os.environ.get("OPSB_PMU") == "1")
    if (repo/"main.asc").is_file() and (repo/"reference.py").is_file() and not (sync_only or build_only or fast_mode):
        supervisor = Path(__file__).resolve().parents[1]
        if not (supervisor/"scheduler/case_bench.py").is_file():
            supervisor = _resolve_parhelion_root()
        if supervisor and str(supervisor) not in sys.path:
            sys.path.insert(0,str(supervisor))
        from scheduler.case_bench import has_reference,run
        if has_reference(repo,sha):
            if pmu_mode:
                raise ValueError("Use opsb test --timing all for reference template device timing")
            return run(workspace,title,repo,sha,npu_host,stream=stream_stdout)
    try:
        branch_ref = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    except Exception:
        branch_ref = "HEAD"
    wspt_base, mlfq, aging_tau = _compute_scheduler_priority(problem["title"], fast=fast_mode, workspace=workspace)

    credential = subprocess.check_output(
        ["git", "credential", "fill"],
        input="url=" + url + "\n\n",
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    fields = dict(line.split("=", 1) for line in credential.splitlines() if "=" in line)
    cfg = dict(
        title=problem["title"],
        lanes_source=(Path(__file__).parent / "npu_runtime/lanes.py").read_text(),
        url=url,
        sha=sha,
        username=fields["username"],
        password=fields["password"],
        sync_only=sync_only,
        fast=fast_mode,
        cold3=cold3_mode,
        pmu=pmu_mode,
        branch_ref=branch_ref,
        build_only=build_only,
        mlfq=mlfq,
        wspt_base=wspt_base,
        aging_tau=aging_tau,
        device=DEVICE,
    )

    destination = workspace / "results" / problem["title"] / sha
    destination.mkdir(parents=True, exist_ok=True)
    log_basename = (
        "sync" if sync_only else
        "build" if build_only else
        "validation-fast" if fast_mode else
        "validation"
    )
    res_basename = (
        "sync.json" if sync_only else
        "result-build.json" if build_only else
        "result-fast.json" if fast_mode else
        "result.json"
    )

    t_start = datetime.datetime.now(datetime.timezone.utc)

    # Official-template layout (CMakeLists.txt + run.sh + kernel.asc, no build.sh): the judge
    # scaffold is its own CMake project, so the legacy build.sh/examples pipeline sdkot run.
    manifest_doc: dict = {}
    try:
        manifest_path = workspace / "opsb.workspace.json"
        if manifest_path.is_file():
            manifest_doc = json.loads(manifest_path.read_text())
    except Exception:
        manifest_doc = {}
    if (repo / "CMakeLists.txt").is_file() and (repo / "run.sh").is_file() and not (repo / "build.sh").is_file():
        template_cfg = dict(cfg)
        template_cfg.update(
            npu_host=npu_host,
            remote_root=_remote_root_for(workspace, manifest_doc),
            lock=(DEVICE.get("work_root") or "/srv/work").rstrip("/") + "/" + (DEVICE.get("work_prefix") or "bench") + "-device0.lock",
            accel_env=os.environ.get("OPSB_TOOLCHAIN_ENV") or DEVICE.get("toolchain_env") or "",
            device_id=str(os.environ.get("OPSB_DEVICE_ID", "0")),
            arch=os.environ.get("NPU_ARCH") or DEVICE.get("arch") or "arch-v1",
            soc=os.environ.get("OPSB_RUNTIME_SOC") or DEVICE.get("soc") or "soc-a",
            jobs=os.environ.get("OPSB_BUILD_JOBS"),
            build_only=build_only,
        )
        for legacy_only in ("cold3", "mlfq", "branch_ref", "sync_only"):
            template_cfg.pop(legacy_only, None)
        code, template_metrics = _stream_remote(
            REMOTE_TEMPLATE, template_cfg, destination, log_basename, stream_stdout
        )
        finished_at = datetime.datetime.now(datetime.timezone.utc)
        result = dict(
            title=problem["title"], sha=sha, exit_code=code, layout="template",
            fast_lane=False, build_only=build_only, mlfq_level="CPU" if build_only else "Q1",
            wall_sec=round((finished_at - t_start).total_seconds(), 2),
            finished_at=finished_at.isoformat(),
        )
        for k in ("phase_a_cpu_sec", "npu_exec_sec", "timing_source", "verified", "soc", "build_only"):
            if template_metrics.get(k) is not None:
                result[k] = template_metrics[k]
        (destination / res_basename).write_text(json.dumps(result, indent=2) + "\n")
        return result

    remote_metrics: dict = {}
    with (destination / (log_basename + ".log")).open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", npu_host, "python3 -c " + shlex.quote(REMOTE_3PHASE)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(json.dumps(cfg))
        proc.stdin.close()
        cfg.clear()
        for line in proc.stdout:
            if line.startswith("OPSB_PHASE_METRICS "):
                try:
                    remote_metrics = json.loads(line[len("OPSB_PHASE_METRICS "):].strip())
                except Exception:
                    pass
                continue
            log.write(line)
            log.flush()
            if stream_stdout:
                print(line, end="", flush=True)
        code = proc.wait()

    finished_at = datetime.datetime.now(datetime.timezone.utc)
    result = dict(
        title=problem["title"],
        sha=sha,
        exit_code=code,
        fast_lane=fast_mode,
        build_only=build_only,
        mlfq_level="CPU" if build_only else mlfq,
        profiler_regime=None if (build_only or sync_only) else ("fast_single_session_l1_pmu" if pmu_mode else "fast_single_session_l0"),
        pmu=pmu_mode,
        wall_sec=round((finished_at - t_start).total_seconds(), 2),
        finished_at=finished_at.isoformat(),
    )
    for k in (
        "phase_a_cpu_sec", "phase_b_die_sec", "phase_c_cpu_sec", "hold_sec",
        "npu_exec_sec", "acl_host_sec", "arena_cases_inproc",
        "kernel_o_count", "kernel_o_max_bytes", "kernel_o_sum_bytes", "kernel_o_max_text_bytes",
        "max_hot_symbol_bytes", "top_symbols",
        "template_instantiations",
        "first_iter_us", "cold_3run_mean_us", "cold_spread_median_us", "cold3", "cold3_stats",
    ):
        if k in remote_metrics and remote_metrics[k] is not None:
            result[k] = remote_metrics[k]
    (destination / res_basename).write_text(json.dumps(result, indent=2) + "\n")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("title")
    parser.add_argument("--sha", default=None, help="Explicit pushed commit SHA (defaults to HEAD)")
    parser.add_argument("--sync-only", action="store_true", help="Pull and verify SHA without building or running NPU")
    parser.add_argument("--build-only", action="store_true", help="Run Phase A 32-core CPU compile only (0.0s NPU lock)")
    parser.add_argument("--fast", action="store_true", help="Run Q0 fast-lane subset after Phase A CPU build")
    parser.add_argument("--cold3", action="store_true", help="Record explicit un-warmed 3-run cold-start timings (COLD3_STATS)")
    parser.add_argument("--pmu", action="store_true", help="Enable L1 PipeUtilization AI-Core PMU counters")
    parser.add_argument("--npu-host", default="npu", help="SSH host alias (default: npu)")
    parser.add_argument("--workspace", "-w", default=None, help="Contest workspace root (default: OPSB_WORKSPACE or cwd)")
    args = parser.parse_args(argv)
    ws = Path(args.workspace).expanduser().resolve() if args.workspace else _default_workspace()

    result = run_3phase_validation(
        title=args.title,
        sha_override=args.sha,
        sync_only=args.sync_only,
        fast=args.fast,
        build_only=args.build_only,
        cold3=args.cold3,
        pmu=args.pmu,
        npu_host=args.npu_host,
        workspace=ws,
        stream_stdout=True,
    )
    print(json.dumps(result))
    return int(result["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
