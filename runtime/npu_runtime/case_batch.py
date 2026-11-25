"""Run prepared reference cases as one device reservation, then check every output on CPU."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from lanes import case_series, device_lane


class SampleProcess:
    def __init__(self, executable, cfg):
        self.executable, self.cfg = executable, cfg
        self.proc = None

    def start(self):
        self.log = tempfile.TemporaryFile()
        self.channel, child = socket.socketpair()
        try:
            self.proc = subprocess.Popen(
                [self.executable, self.cfg["device"]], stdin=subprocess.PIPE,
                stdout=self.log, stderr=self.log, pass_fds=(child.fileno(),),
                env={**os.environ, "OPSB_BATCH_FD": str(child.fileno())},
            )
        finally:
            child.close()
        self.channel.settimeout(self.cfg["timeout"])
        self.reply = self.channel.makefile("rb")

    def close(self, kill=False):
        if self.proc is None:
            return
        if kill:
            self.proc.kill()
        try:
            self.proc.stdin.close()
        except BrokenPipeError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.reply.close()
        self.channel.close()
        self.log.close()
        self.proc = None

    def execute(self, path, timed):
        if self.proc is None:
            self.start()
        try:
            self.proc.stdin.write(f"{int(timed)} {path}\n".encode())
            self.proc.stdin.flush()
            fields = self.reply.readline(128).split()
            if len(fields) == 2 and fields[0] == b"0":
                return {"status": "PASS", "us": float(fields[1])}
            detail = os.pread(self.log.fileno(), 1500, max(0, os.fstat(self.log.fileno()).st_size - 1500))
            result = {"status": "RE", "detail": detail.decode(errors="replace")}
        except TimeoutError:
            result = {"status": "TLE", "detail": "kernel execution timed out"}
        except (OSError, ValueError) as exc:
            result = {"status": "RE", "detail": str(exc)}
        self.close(kill=True)
        return result


def prepare(repo, cfg, root, update):
    plans = []
    for index, item in enumerate(cfg["selected"], 1):
        progress = dict(index=index, total=len(cfg["selected"]), id=item["id"], phase="prepare")
        update(progress)
        case = root / str(index)
        case.mkdir()
        plan = dict(progress=progress, parts=[], samples=[], timed=cfg["timing"] == "all" or
                    (cfg["timing"] == "perf" and item["perf"]))
        plans.append(plan)
        try:
            proc = subprocess.run([sys.executable, str(repo.parent / "reference_cases.py"), str(index-1), str(case)],
                                  capture_output=True, text=True, timeout=max(600, cfg["timeout"]))
            if proc.returncode:
                raise ValueError((proc.stderr or proc.stdout)[-1500:])
            plan["parts"] = json.loads((case / "parts.json").read_text())
            for part in plan["parts"]:
                base = Path(part["path"])
                count = cfg["repeats"] if plan["timed"] else 1
                part["runs"] = []
                for repeat in range(count):
                    path = base / f"run-{repeat}"
                    path.mkdir()
                    (path / "case.desc").symlink_to(base / "case.desc")
                    (path / "input").symlink_to(base / "input")
                    (path / "output").mkdir()
                    part["runs"].append(path)
            if not plan["parts"]:
                raise ValueError("Reference case produced no invocations")
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            plan["error"] = {"status": "ERROR", "detail": str(exc)}
    return plans


def execute_batch(repo, cfg, plans):
    ready = [plan for plan in plans if "error" not in plan]
    if not ready:
        return {"wait_sec": 0, "hold_sec": 0, "acquisitions": 0}
    sample = SampleProcess(repo / "build-casebench/sample", cfg)
    queued = time.monotonic()
    with device_lane(dict(cfg, case_progress=dict(ready[0]["progress"], phase="waiting"))) as update:
        started = time.monotonic()
        try:
            for plan in ready:
                for part_number, part in enumerate(plan["parts"], 1):
                    samples = []
                    for repeat, path in enumerate(part["runs"], 1):
                        update(dict(plan["progress"], phase="execute", repeat=repeat, repeats=len(part["runs"]),
                                    part=part_number, parts=len(plan["parts"])))
                        result = sample.execute(path, plan["timed"])
                        if result["status"] != "PASS":
                            plan["error"] = result
                            break
                        samples.append(result["us"])
                        if not cfg.get("reuse_session", True):
                            sample.close()
                    if "error" in plan:
                        break
                    plan["samples"].append(samples)
        finally:
            sample.close()
    return {"wait_sec": round(started - queued, 3), "hold_sec": round(time.monotonic() - started, 3),
            "acquisitions": 1}


def verify(plan, cfg, compare):
    import numpy as np
    checks = {}
    for index, part in enumerate(plan["parts"]):
        base = Path(part["path"])
        for name, tolerance in part["tolerances"].items():
            want = np.load(base / (name + ".npy"), mmap_mode="r", allow_pickle=False)
            for repeat, path in enumerate(part["runs"], 1):
                output = path / "output"
                expected = {n + ".bin" for n in part["tolerances"]}
                if {f.name for f in output.iterdir()} != expected:
                    return {"status": "WA", "detail": "missing or unexpected output files", "repeat": repeat}
                binary = output / (name + ".bin")
                check = (compare(np.fromfile(binary, dtype=want.dtype), want, tolerance, cfg.get("strict_fp32", False))
                         if binary.stat().st_size == want.nbytes else {"pass": False, "reason": "byte size"})
                checks[f"{index}/{name}"] = check
                if not check["pass"]:
                    return {"status": "WA", "outputs": checks, "repeat": repeat}
    times = [sum(group) for group in zip(*plan["samples"])] if plan["timed"] else []
    return {"status": "PASS", "outputs": checks, "device_us": [t for t in times if t > 0]}


def run_series(repo, cfg, compare, emit):
    with case_series(cfg) as update, tempfile.TemporaryDirectory(dir=repo.parent) as tmp:
        plans = prepare(repo, cfg, Path(tmp), update)
        emit("device_batch", **execute_batch(repo, cfg, plans))
        for plan in plans:
            update(dict(plan["progress"], phase="verify"))
            try:
                result = plan.get("error") or verify(plan, cfg, compare)
            except (ValueError, OSError) as exc:
                result = {"status": "ERROR", "detail": str(exc)}
            emit("case", id=plan["progress"]["id"], **result)
