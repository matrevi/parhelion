"""Kernel task durations from the device profiler for the broker's timed executions (4.1 route #1).

Event timing brackets the launch as well as the kernel, so a 2 us kernel reads 5 or 25 us
depending on the host. The profiler reports the device task duration the judge measures.

The broker starts the persistent sample process under the profiler, which passes stdin, the
batch reply descriptor and the environment through to it, so the execution protocol is
unchanged. Each execution's host wall-clock window is recorded. While the device lock is held
only collection runs: closing the sample ends the application, and the lock is released as soon
as the profiler log shows that the device phase is over. Export, parsing and the assignment of
kernel tasks to execution windows run afterwards (Phase C), off the lock.

A kernel task belongs to the execution whose window holds its start time; an execution with
no task, or a profile that cannot be read, leaves that case on event timing. Stdlib only: the
file ships next to the broker script (`device_broker.ensure_broker(..., companions=...)`).
"""
import csv
import os
import shutil
import signal
import subprocess
import tempfile
import time
from bisect import bisect_right
from pathlib import Path

#: The device phase is over (the application exited) once the profiler logs one of these.
RELEASE_MARKERS = ("Start export data", "Start analyzing data", "Analysis data in", "Profiling finished.",
                   "Process profiling data complete.")
RELEASE_WAIT_SEC = 60.0
COLLECT_WAIT_SEC = 300.0
KERNEL_TAGS = ("KERNEL", "AI_CORE", "AICORE", "AIV", "AIC", "MIX", "VECTOR")


def resolve(binary):
    """Absolute path of the profiler executable, or None when this host has none."""
    if binary and os.path.isabs(binary):
        return binary if os.access(binary, os.X_OK) else None
    return shutil.which(binary or "profiler")


def command(binary, acl_flag, out_dir, argv):
    """Task-time collection only: no AI Core counters, no AI CPU, no runtime or ACL API traces."""
    return [binary, f"--output={out_dir}", "--task-time=l0", "--ai-core=off", "--aicpu=off",
            f"{acl_flag or '--acl'}=off", "--runtime-api=off", "--application=" + " ".join(argv)]


def _number(text):
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return None


def parse_tasks(out_dir):
    """Kernel tasks ``(start_us, duration_us)`` sorted by start, from the exported summaries.

    ``task_time_*.csv`` lists every device task with its type; ``op_summary_*.csv`` is the
    fallback for exports without it. Only one kind is read, so a task is never counted twice.
    """
    root = Path(out_dir)
    for pattern, type_keys, start_keys, dur_keys in (
            ("task_time_*.csv", ("kernel_type",), ("task_start(us)",), ("task_time(us)",)),
            ("op_summary_*.csv", ("Task Type",), ("Task Start Time(us)",), ("Task Duration(us)",))):
        files = sorted(root.rglob(pattern))
        if not files:
            continue
        tasks = []
        for path in files:
            with path.open(newline="", encoding="utf-8", errors="replace") as fh:
                for record in csv.DictReader(fh):
                    row = {str(k).strip(): v for k, v in (record or {}).items() if k}
                    kind = str(next((row[k] for k in type_keys if k in row), "") or "").strip().upper()
                    if kind and kind != "N/A" and not any(tag in kind for tag in KERNEL_TAGS):
                        continue  # profiling markers, placeholders, memcpy and event tasks
                    start = next((_number(row[k]) for k in start_keys if k in row), None)
                    duration = next((_number(row[k]) for k in dur_keys if k in row), None)
                    if start is not None and duration is not None and duration > 0:
                        tasks.append((start, duration))
        return sorted(tasks)
    return []


def assign(tasks, windows):
    """``{path: summed task us}`` for execution windows ``[(path, start_us, end_us)]``.

    Windows come from the host clock and the profiler reports host-aligned timestamps; a task
    starting inside a window belongs to that execution. A window without a task (an empty case
    launches no kernel) stays unattributed. There is deliberately no order-based fallback: the
    number of tasks per execution varies between cases, and a shifted assignment is worse than
    event timing.
    """
    ordered = sorted(windows, key=lambda w: w[1])
    starts = [w[1] for w in ordered]
    found = {}
    for start, duration in tasks:
        i = bisect_right(starts, start) - 1
        if i >= 0 and start <= ordered[i][2]:
            found[ordered[i][0]] = found.get(ordered[i][0], 0.0) + duration
    return found


def _kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # the profiler and the application under it
    except OSError:
        pass


def sample_class(base):
    """A subclass of the broker's SampleProcess that runs the sample under the profiler.

    Every start of the sample is one profiler run with its own output directory and execution
    windows. A run that had to be killed (TLE, RE, cancel) exports nothing and is not trusted.
    """

    class ProfiledSample(base):
        def __init__(self, executable, cfg, capture):
            super().__init__(executable, cfg)
            self.capture = capture
            self.runs = []

        def start(self):
            import socket
            self.log = tempfile.TemporaryFile()
            self.channel, child = socket.socketpair()
            run = {"out": self.capture.new_output(), "windows": [], "killed": False, "proc": None,
                   "log": self.log, "release_sec": None}
            argv = command(self.capture.binary, self.capture.acl_flag, run["out"],
                           [self.executable, str(self.cfg["device"])])
            try:
                self.proc = subprocess.Popen(
                    argv, stdin=subprocess.PIPE, stdout=self.log, stderr=self.log,
                    pass_fds=(child.fileno(),), start_new_session=True,
                    env={**os.environ, "OPSB_BATCH_FD": str(child.fileno())},
                )
            finally:
                child.close()
            run["proc"] = self.proc
            self.runs.append(run)
            self.reply = self.channel.makefile("rb")

        def kill(self):
            proc = self.proc
            if proc is not None and proc.poll() is None:
                _kill_group(proc)

        def execute(self, path, timed, deadline):
            began = time.time()
            result = super().execute(path, timed, deadline)
            if result["status"] == "PASS" and self.runs:
                self.runs[-1]["windows"].append((str(path), began * 1e6, time.time() * 1e6))
            return result

        def close(self, kill=False):
            """End the application; return once its device phase is over. Export keeps running."""
            proc = self.proc
            if proc is None:
                return
            self.proc = None
            run = self.runs[-1]
            if kill or proc.poll() not in (None, 0):
                run["killed"] = True
                _kill_group(proc)
            try:
                proc.stdin.close()
            except OSError:
                pass
            began = time.monotonic()
            while not run["killed"] and time.monotonic() - began < RELEASE_WAIT_SEC:
                if proc.poll() is not None or any(m in _tail(run["log"]) for m in RELEASE_MARKERS):
                    break
                time.sleep(0.02)
            run["release_sec"] = round(time.monotonic() - began, 3)
            self.reply.close()
            self.channel.close()

        def finish(self, timeout=COLLECT_WAIT_SEC):
            """Wait for every run's export; return ``({path: kernel us}, [detail per run])``."""
            found, notes = {}, []
            for run in self.runs:
                proc = run["proc"]
                try:
                    proc.wait(timeout=0 if run["killed"] else timeout)
                except subprocess.TimeoutExpired:
                    _kill_group(proc)
                    proc.wait()
                    run["killed"] = True
                    notes.append(f"profiler export exceeded {timeout:.0f}s")
                    continue
                finally:
                    text = _tail(run["log"])
                    run["log"].close()
                if run["killed"]:
                    notes.append("profiler run interrupted")
                    continue
                if not run["windows"]:
                    continue
                try:
                    tasks = parse_tasks(run["out"])
                except (OSError, ValueError, csv.Error) as exc:
                    notes.append(f"unreadable profile: {exc!r}")
                    continue
                if not tasks:
                    notes.append("no kernel tasks: " + " ".join(text.split())[-160:])
                    continue
                mapping = assign(tasks, run["windows"])
                found.update(mapping)
                notes.append(f"{len(tasks)} tasks, {len(mapping)} of {len(run['windows'])} executions attributed")
            return found, notes

    return ProfiledSample


def _tail(log, limit=1 << 16):
    try:
        size = os.fstat(log.fileno()).st_size
        return os.pread(log.fileno(), min(size, limit), max(0, size - limit)).decode(errors="replace")
    except (OSError, ValueError):
        return ""


class Capture:
    """Profiler settings and outputs of one broker segment."""

    def __init__(self, settings, root=None):
        self.binary = resolve(settings.get("bin"))
        self.acl_flag = settings.get("acl_flag") or "--acl"
        self.root = Path(root or tempfile.gettempdir())
        self.dir = None
        self.count = 0
        self.samples = []

    @property
    def available(self):
        return self.binary is not None

    def sample(self, base, executable, cfg):
        sample = sample_class(base)(executable, cfg, self)
        self.samples.append(sample)
        return sample

    def new_output(self):
        if self.dir is None:
            self.root.mkdir(parents=True, exist_ok=True)
            self.dir = Path(tempfile.mkdtemp(prefix="opsb-prof-", dir=self.root))
        self.count += 1
        return self.dir / f"run-{self.count}"

    def release_seconds(self):
        return round(sum(r["release_sec"] or 0.0 for s in self.samples for r in s.runs), 3)

    def collect(self, case_paths):
        """Per-case profiler samples after export: ``{index: {"samples", "cold"} or {"error"}}``.

        ``case_paths`` maps a case index to its parts' ``(warm paths, run paths)``; like the
        event samples, the r-th sample of a case is the sum over its parts' r-th runs.
        """
        found, notes = {}, []
        for sample in self.samples:
            mapping, detail = sample.finish()
            found.update(mapping)
            notes.extend(detail)
        if self.dir is not None:
            shutil.rmtree(self.dir, ignore_errors=True)
        out = {}
        for index, parts in case_paths.items():
            paths = [str(p) for warm, runs in parts for p in list(warm) + list(runs)]
            if not paths or any(p not in found for p in paths):
                out[index] = {"error": ("; ".join(notes) or "no profile")[-300:]}
                continue
            runs = [[found[str(p)] for p in r] for _, r in parts]
            warm = [[found[str(p)] for p in w] for w, _ in parts]
            out[index] = {"samples": [round(sum(g), 3) for g in zip(*runs)],
                          "cold": [round(sum(g), 3) for g in zip(*warm)]}
        return out, notes


def _sum_parts(groups):
    """The r-th sample of a case is the sum of the r-th sample of every part."""
    return [sum(group) for group in zip(*groups)]


def timing_fields(plan):
    """Event and profiler samples of a timed plan, and which of them the run's timing uses.

    The profiler is authoritative when it attributed a task to every timed execution of the
    case; otherwise the case keeps event timing. The two are never mixed within a case.
    """
    if not plan["timed"]:
        return {"device_us": [], "cold_us": [], "timing_source": None}
    event = [t for t in _sum_parts(plan["samples"]) if t > 0]
    event_cold = [t for t in _sum_parts(plan.get("cold") or []) if t > 0]
    profile = plan.get("profile") or {}
    prof = [t for t in profile.get("samples") or [] if t > 0]
    prof_cold = [t for t in profile.get("cold") or [] if t > 0]
    use = bool(prof) and len(prof) == len(event)
    # Kernel tasks run inside the event bracket; a larger task total means a misattribution.
    above = use and any(p > e * 1.05 + 1.0 for p, e in zip(prof, event))
    use = use and not above
    out = {"timing_source": "profiler" if use else "event", "event_us": event, "event_cold_us": event_cold,
           "device_us": prof if use else event, "cold_us": prof_cold if use else event_cold}
    if prof:
        out.update(profiler_us=prof, profiler_cold_us=prof_cold)
    if above:
        out["profiler_error"] = "profiler task time above the event reading; kept event timing"
    elif plan.get("profile_error"):
        out["profiler_error"] = plan["profile_error"][:300]
    return out
