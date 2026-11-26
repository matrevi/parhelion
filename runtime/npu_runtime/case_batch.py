"""Run prepared reference cases as one device reservation, then check every output on CPU."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from collections import deque
from pathlib import Path

from device_broker import SampleProcess, ensure_broker
from lanes import case_series, device_lane


def _timed(cfg, item):
    if item.get("timed") is not None:
        return bool(item["timed"])
    timing = cfg.get("timing", "perf")
    return timing == "all" or (timing == "perf" and bool(item.get("perf")))


def _workdir(base, name):
    path = base / name
    path.mkdir()
    (path / "case.desc").symlink_to(base / "case.desc")
    (path / "input").symlink_to(base / "input")
    (path / "output").mkdir()
    return path


def _sum_parts(groups):
    """The r-th sample of a case is the sum of the r-th sample of every part."""
    return [sum(group) for group in zip(*groups)]


# Inputs and goldens depend only on the oracle side of the checkout, the generated I/O schema
# and the case item, never on the kernel. They are cached on the device host so that each
# candidate of a problem pays for reference generation once.
CACHE_FORMAT = 1
KERNEL_SUFFIXES = {".asc", ".h", ".hpp", ".hh", ".c", ".cc", ".cpp", ".cxx", ".cu", ".cmake"}
CACHE_BYTES_PER_PROBLEM = int(os.environ.get("OPSB_CASE_CACHE_BYTES") or 8 << 30)
CACHE_MAX_AGE_SEC = 7 * 86400


def _link_or_copy(src, dst):
    """Hard-link (the cache sits on the run directory's filesystem); copy across filesystems."""
    try:
        os.link(src, dst)
    except OSError:
        import shutil
        shutil.copy2(src, dst)


def _oracle_digest(repo):
    """Digest every checkout file that case generation can read; kernel sources are left out."""
    import hashlib
    h = hashlib.sha256()
    files = []
    for top, dirs, names in os.walk(repo):
        dirs[:] = [d for d in dirs if not (d.startswith("build") or d in ("__pycache__", ".git"))]
        files.extend(Path(top) / name for name in names)
    for path in sorted(files):
        if path.suffix.lower() in KERNEL_SUFFIXES or path.name == "CMakeLists.txt" or path.suffix == ".pyc":
            continue
        h.update(str(path.relative_to(repo)).encode() + b"\\0" + path.read_bytes() + b"\\0")
    return h.hexdigest()


class CaseCache:
    def __init__(self, repo, cfg):
        import hashlib
        from importlib import metadata
        self.enabled = cfg.get("case_cache", True) and os.environ.get("OPSB_CASE_CACHE", "1") != "0"
        title = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(cfg.get("title") or "problem"))
        # Next to the run's scratch directory, so cached files can be hard-linked rather than copied.
        self.root = Path(os.environ.get("OPSB_CASE_CACHE_DIR") or repo.parent.parent / "opsb-case-cache") / title
        try:
            numpy_version = metadata.version("numpy")
        except metadata.PackageNotFoundError:
            numpy_version = "none"
        engine = (repo.parent / "reference_cases.py").read_bytes()
        schema = (repo.parent / "schema.json").read_bytes()
        base = [CACHE_FORMAT, sys.version.split()[0], numpy_version, _oracle_digest(repo),
                hashlib.sha256(engine).hexdigest(), hashlib.sha256(schema).hexdigest()]
        self.base = json.dumps(base)

    def key(self, item):
        import hashlib
        return hashlib.sha256((self.base + json.dumps(item, sort_keys=True)).encode()).hexdigest()[:40]

    def restore(self, item, case):
        """Copy a cached case into ``case``; return its parts with absolute paths, or None."""
        import shutil
        if not self.enabled:
            return None
        entry = self.root / self.key(item)
        if not (entry / ".complete").is_file():
            return None
        try:
            shutil.copytree(entry, case, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".complete"),
                            copy_function=_link_or_copy)
            parts = json.loads((case / "parts.json").read_text())
            for part in parts:
                part["path"] = str(case / part["path"])
            os.utime(entry)
            return parts
        except (OSError, ValueError, KeyError):
            return None

    def store(self, item, case, parts):
        import shutil
        if not self.enabled:
            return
        entry = self.root / self.key(item)
        if entry.exists():
            return
        temp = self.root / f".{entry.name}.{os.getpid()}.tmp"
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            shutil.copytree(case, temp, copy_function=_link_or_copy)
            relative = [dict(part, path=str(Path(part["path"]).relative_to(case))) for part in parts]
            (temp / "parts.json").unlink()
            (temp / "parts.json").write_text(json.dumps(relative))
            size = 0
            for top, _, names in os.walk(temp):
                for name in names:
                    path = Path(top) / name
                    # Shared by every run that restores it: inputs and goldens are read-only.
                    path.chmod(0o444)
                    size += path.stat().st_size
            (temp / ".complete").write_text(json.dumps({"bytes": size}))
            temp.rename(entry)
        except (OSError, ValueError):
            shutil.rmtree(temp, ignore_errors=True)

    def prune(self):
        """Keep the most recently used entries of this problem within the byte budget and age limit."""
        import shutil
        if not self.enabled or not self.root.is_dir():
            return
        now, kept = time.time(), 0
        entries = sorted((p for p in self.root.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
        for entry in entries:
            age = now - entry.stat().st_mtime
            if entry.name.startswith("."):
                if age > 3600:
                    shutil.rmtree(entry, ignore_errors=True)
                continue
            try:
                kept += int(json.loads((entry / ".complete").read_text())["bytes"])
            except (OSError, ValueError, KeyError, TypeError):
                kept += 0 if age < 3600 else CACHE_BYTES_PER_PROBLEM
            if kept > CACHE_BYTES_PER_PROBLEM or age > CACHE_MAX_AGE_SEC:
                shutil.rmtree(entry, ignore_errors=True)


def _generate(repo, cfg, index, case, threads):
    """Run the oracle for one case in a fresh process with a bounded BLAS/OpenMP pool."""
    env = dict(os.environ)
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        env[name] = str(threads)
    proc = subprocess.run([sys.executable, str(repo.parent / "reference_cases.py"), str(index), str(case)],
                          capture_output=True, text=True, timeout=max(600, cfg["timeout"]), env=env)
    if proc.returncode:
        raise ValueError((proc.stderr or proc.stdout)[-1500:])
    return json.loads((case / "parts.json").read_text())


def prepare(repo, cfg, root, update):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from lanes import cpu_share
    selected = cfg["selected"]
    total = len(selected)
    plans, missing = [], []
    try:
        cache = CaseCache(repo, cfg)
    except (OSError, ValueError) as exc:
        print(f"case cache disabled: {exc!r}", file=sys.stderr, flush=True)
        cache = None
    for index, item in enumerate(selected, 1):
        progress = dict(index=index, total=total, id=item["id"], phase="prepare")
        case = root / str(index)
        case.mkdir()
        plan = dict(progress=progress, parts=[], samples=[], cold=[], timed=_timed(cfg, item), case=case)
        plans.append(plan)
        parts = cache.restore(item, case) if cache else None
        if parts:
            plan["parts"], plan["cached"] = parts, True
        else:
            missing.append(plan)
    done = total - len(missing)
    if missing:
        update(dict(missing[0]["progress"], index=done, cached=done))
    if missing:
        cores = cpu_share(cfg, 2 * len(missing))
        workers = max(1, min(len(missing), cores // 2))
        threads = max(1, cores // workers)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_generate, repo, cfg, plan["progress"]["index"] - 1, plan["case"], threads): plan
                       for plan in missing}
            for future in as_completed(futures):
                plan = futures[future]
                item = selected[plan["progress"]["index"] - 1]
                try:
                    plan["parts"] = future.result()
                    if not plan["parts"]:
                        raise ValueError("Reference case produced no invocations")
                    if cache:
                        cache.store(item, plan["case"], plan["parts"])
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    plan["error"] = {"status": "ERROR", "detail": str(exc)}
                done += 1
                update(dict(plan["progress"], index=done, cached=total - len(missing)))
    for plan in plans:
        plan.pop("case", None)
        if "error" in plan:
            continue
        count = cfg["repeats"] if plan["timed"] else 1
        warmup = max(0, int(cfg.get("warmup") or 0)) if plan["timed"] else 0
        for part in plan["parts"]:
            base = Path(part["path"])
            part["warm"] = [_workdir(base, f"warm-{k}") for k in range(warmup)]
            part["runs"] = [_workdir(base, f"run-{r}") for r in range(count)]
    if cache:
        try:
            cache.prune()
        except OSError:
            pass
    return plans


def execute_case(sample, cfg, plan, update):
    """Fallback lane execution: warm paths run first and only feed ``cold``."""
    deadline = time.monotonic() + cfg["timeout"]
    progress = dict(plan["progress"], phase="execute", started_epoch=time.time(), timeout_sec=cfg["timeout"])
    for index, part in enumerate(plan["parts"], 1):
        for stage, paths, sink in (("warm", part.get("warm") or [], plan.setdefault("cold", [])),
                                   ("run", part["runs"], plan["samples"])):
            samples = []
            for repeat, path in enumerate(paths, 1):
                update(dict(progress, part=index, parts=len(plan["parts"]), repeat=repeat, repeats=len(paths),
                            stage=stage))
                result = sample.execute(path, plan["timed"], deadline)
                if result["status"] != "PASS":
                    if result["status"] == "TLE":
                        result = dict(result, detail=result["detail"] + " (broker unavailable)")
                    plan["error"] = result
                    return
                samples.append(result["us"])
                if not cfg.get("reuse_session", True):
                    sample.close()
            sink.append(samples)


def _execute_lane(repo, cfg, ready):
    """3.6.2 path: this run process takes its own ticket and the device flock."""
    ready = deque(ready)
    sample = SampleProcess(repo / "build-casebench/sample", cfg)
    wait = hold = 0.0
    acquisitions = 0
    while ready:
        queued = time.monotonic()
        with device_lane(dict(cfg, case_progress=dict(ready[0]["progress"], phase="waiting"))) as update:
            started = time.monotonic()
            wait += started - queued
            acquisitions += 1
            try:
                while ready:
                    plan = ready.popleft()
                    execute_case(sample, cfg, plan, update)
                    if plan.get("error"):
                        for pending in ready:
                            pending["error"] = {"status": "NOT_RUN", "detail": "Earlier device execution failed"}
                        ready.clear()
                        break
            finally:
                sample.close()
                hold += time.monotonic() - started
    return {"wait_sec": wait, "hold_sec": hold, "acquisitions": acquisitions}


def broker_job(repo, cfg, ready):
    """The broker submission for every ready plan (protocol in device_broker)."""
    tier = cfg.get("tier")
    return {
        "run_id": cfg.get("run_id"), "title": cfg.get("title"), "sha": cfg.get("sha"),
        "client_pid": os.getpid(), "tier": int(tier if tier is not None else 2 if cfg.get("fast") else 1),
        "prio": float(cfg.get("wspt_base", 1.0)), "aging_tau": float(cfg.get("aging_tau", 600)),
        "created": time.time(), "fast": bool(cfg.get("fast")),
        "executable": str(repo / "build-casebench/sample"), "device": str(cfg["device"]),
        "reuse_session": bool(cfg.get("reuse_session", True)), "case_timeout": cfg["timeout"],
        "job_timeout": cfg.get("job_timeout") or min(1800, cfg["timeout"] * len(ready)),
        "cases": [{"index": plan["progress"]["index"], "id": plan["progress"]["id"], "timed": bool(plan["timed"]),
                   "warmup": len(plan["parts"][0].get("warm") or []) if plan["parts"] else 0,
                   "parts": [{"warm": [str(p) for p in part.get("warm") or []],
                              "runs": [str(p) for p in part["runs"]]} for part in plan["parts"]]}
                  for plan in ready],
    }


def _execute_broker(path, repo, cfg, ready, update):
    """Submit every ready plan as one job; return (done event or None, indices with a result)."""
    by_index = {plan["progress"]["index"]: plan for plan in ready}
    reported = set()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.connect(path)
            conn.sendall((json.dumps({"op": "submit", "job": broker_job(repo, cfg, ready)}) + "\n").encode())
            with conn.makefile("rb") as reader:
                for line in reader:
                    msg = json.loads(line)
                    kind = msg.get("event")
                    if kind == "queued":
                        head = next(p for p in ready if p["progress"]["index"] not in reported)
                        update(dict(head["progress"], phase="waiting", position=msg.get("position")))
                    elif kind == "progress":
                        plan = by_index[msg["index"]]
                        update(dict(plan["progress"], phase="execute", part=msg["part"], parts=msg["parts"],
                                    repeat=msg["repeat"], repeats=msg["repeats"], stage=msg["phase"],
                                    timeout_sec=cfg["timeout"]))
                    elif kind == "case":
                        plan = by_index[msg["index"]]
                        reported.add(msg["index"])
                        if msg["status"] == "PASS":
                            plan["samples"], plan["cold"] = [msg.get("samples") or []], [msg.get("cold") or []]
                        else:
                            plan["error"] = {"status": msg["status"], "detail": msg.get("detail", "")}
                    elif kind == "done":
                        return msg, reported
                    elif kind == "error":
                        print("device broker: " + str(msg.get("detail")), file=sys.stderr, flush=True)
                        break
    except (OSError, ValueError, KeyError, TypeError, StopIteration) as exc:
        print(f"device broker unavailable: {exc!r}", file=sys.stderr, flush=True)
    return None, reported


def execute_batch(repo, cfg, plans, update=None):
    update = update or (lambda progress: None)
    ready = [plan for plan in plans if "error" not in plan]
    path = ensure_broker(cfg, Path(__file__).with_name("device_broker.py").read_text(encoding="utf-8"))
    result = {"wait_sec": 0.0, "hold_sec": 0.0, "segments": 0, "broker": path is not None}
    if path is not None and ready:
        done, reported = _execute_broker(path, repo, cfg, ready, update)
        ready = [plan for plan in ready if plan["progress"]["index"] not in reported]
        if done is not None:
            result.update(wait_sec=done.get("wait_sec", 0.0), hold_sec=done.get("hold_sec", 0.0),
                          segments=done.get("segments", 0))
    if ready:
        # Broker unavailable, or it went away mid-job: finish the unreported plans on the 3.6.2 lane.
        lane = _execute_lane(repo, cfg, ready)
        result.update(broker=False, wait_sec=result["wait_sec"] + lane["wait_sec"],
                      hold_sec=result["hold_sec"] + lane["hold_sec"],
                      segments=result["segments"] + lane["acquisitions"])
    return {"wait_sec": round(result["wait_sec"], 3), "hold_sec": round(result["hold_sec"], 3),
            "acquisitions": result["segments"], "segments": result["segments"], "broker": result["broker"]}


def verify(plan, cfg, compare):
    import numpy as np
    checks = {}
    for index, part in enumerate(plan["parts"]):
        base = Path(part["path"])
        expected = {name + ".bin" for name in part["tolerances"]}
        for repeat, path in enumerate(part["runs"], 1):
            if {f.name for f in (path / "output").iterdir()} != expected:
                return {"status": "WA", "detail": "missing or unexpected output files", "repeat": repeat}
        for name, tolerance in part["tolerances"].items():
            want = np.load(base / (name + ".npy"), mmap_mode="r", allow_pickle=False)
            for repeat, path in enumerate(part["runs"], 1):
                output = path / "output"
                binary = output / (name + ".bin")
                check = (compare(np.fromfile(binary, dtype=want.dtype), want, tolerance, cfg.get("strict_fp32", False))
                         if binary.stat().st_size == want.nbytes else {"pass": False, "reason": "byte size"})
                checks[f"{index}/{name}"] = check
                if not check["pass"]:
                    return {"status": "WA", "outputs": checks, "repeat": repeat}
    times = _sum_parts(plan["samples"]) if plan["timed"] else []
    cold = _sum_parts(plan.get("cold") or []) if plan["timed"] else []
    return {"status": "PASS", "outputs": checks, "device_us": [t for t in times if t > 0],
            "cold_us": [t for t in cold if t > 0]}


def run_series(repo, cfg, compare, emit):
    with case_series(cfg) as update, tempfile.TemporaryDirectory(dir=repo.parent) as tmp:
        started = time.monotonic()
        plans = prepare(repo, cfg, Path(tmp), update)
        emit("prepared", seconds=round(time.monotonic() - started, 3), cases=len(plans),
             cached=sum(1 for plan in plans if plan.get("cached")), errors=sum(1 for plan in plans if "error" in plan))
        emit("device_batch", **execute_batch(repo, cfg, plans, update))
        for plan in plans:
            update(dict(plan["progress"], phase="verify"))
            try:
                result = plan.get("error") or verify(plan, cfg, compare)
            except (ValueError, OSError) as exc:
                result = {"status": "ERROR", "detail": str(exc)}
            emit("case", id=plan["progress"]["id"], **dict({"cold_us": []}, **result))
