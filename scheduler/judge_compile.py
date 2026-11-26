"""Compile-only check with the judge's toolchain and flags before a candidate is uploaded (4.1 #20).

Local suites build with the device host's own toolchain. The judge may compile with another
version and its own options, so a SHA can pass every local case and still fail online. This
module compiles the candidate on the device host with the toolchain configured as the judge's:
the `npu-test.py --build-only` template path, CPU lane only (`lanes.compile_lane` budget), no
device lock and no timing. The outcome is recorded next to the SHA's results, where gate
Stage 1 reads it: a compile failure rejects with the compiler's error lines.

Deployment facts stay out of the repository; `~/.parhelion/config.json` → `"device"` holds them:
  judge_toolchain_root   toolchain install on the device host matching the judge's version
  judge_toolchain_env    environment script to source (default `<root>/set_env.sh`)
  judge_compile_flags    extra CMake configure arguments (list, or one shell-quoted string)
Without them the check is skipped and Stage 1 only adds a hint.

CLI (`python -m scheduler.judge_compile`):
  check      compile now and write the record (blocking; minutes at most)
  preflight  for the uploader: upload / wait (a check was started or is running) / hold (failed)
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .parhelion_paths import atomic_write_json, load_problem_manifest

RECORD = "judge-compile.json"
LABEL = "judge"
ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "runtime" / "npu-test.py"
CHECK_TIMEOUT_SEC = 900
HINT = "judge toolchain not configured (device.judge_toolchain_root); compile check skipped"
_FAILED_CMD = re.compile(r"\[TEMPLATE\] command failed \((\d+)\): (.*)")
_ERROR_LINE = re.compile(r"(?:^|\s)(?:fatal )?error[:\s]", re.IGNORECASE)


def _device(key: str) -> Any:
    """Deployment setting; `scheduler` may run without the CLI package on `sys.path`."""
    for module in ("opsb_core.device_env", "cli.opsb_core.device_env"):
        try:
            return __import__(module, fromlist=["device"]).device(key)
        except (ImportError, KeyError):
            continue
    return None


def settings() -> dict[str, Any]:
    root = str(_device("judge_toolchain_root") or "").strip()
    env_script = str(_device("judge_toolchain_env") or "").strip() or (root.rstrip("/") + "/set_env.sh" if root else "")
    raw = _device("judge_compile_flags")
    flags = shlex.split(raw) if isinstance(raw, str) else [str(f) for f in raw or []]
    identity = hashlib.sha256(json.dumps([env_script, flags]).encode()).hexdigest()[:16]
    return {"configured": bool(env_script), "toolchain_env": env_script, "flags": flags, "identity": identity}


def record_path(ws: Path, title: str, sha: str) -> Path:
    return Path(ws) / "results" / title / sha / RECORD


def read_record(ws: Path, title: str, sha: str, cfg: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The record for this SHA made with the currently configured toolchain and flags, else None."""
    cfg = cfg or settings()
    try:
        doc = json.loads(record_path(ws, title, sha).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("sha") != sha or doc.get("identity") != cfg["identity"]:
        return None
    return doc


def _pid_alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
        return int(pid) > 0
    except (OSError, TypeError, ValueError):
        return False


def error_summary(log: str, limit: int = 6) -> str:
    """Compiler error lines (file name and position kept, directories dropped), else the tail."""
    lines: list[str] = []
    for raw in log.splitlines():
        line = raw.strip()
        if not line or not _ERROR_LINE.search(line) or line.startswith("[TEMPLATE]"):
            continue
        line = re.sub(r"(?:/[^\s:/]+)+/([^\s:/]+:\d+)", r"\1", line)[:240]
        if line not in lines:
            lines.append(line)
        if len(lines) >= limit:
            break
    if not lines:
        lines = [ln.strip()[:240] for ln in log.splitlines() if ln.strip()][-3:]
    return " | ".join(lines)


def classify(returncode: int, log: str) -> str:
    """pass; fail (the build step failed: the candidate's fault); error (transport, fetch, configure)."""
    if returncode == 0:
        return "pass"
    failed = _FAILED_CMD.findall(log)
    return "fail" if failed and "cmake --build" in failed[-1][1] else "error"


def _problem_repo(ws: Path, title: str) -> Path | None:
    row = next((p for p in load_problem_manifest(ws) if p.get("title") == title), None)
    return Path(ws) / str(row.get("path") or title) if row else None


def run_check(ws: Path, title: str, sha: str, *, host: str | None = None,
              timeout: float = CHECK_TIMEOUT_SEC, python: str = sys.executable) -> dict[str, Any]:
    """Compile `sha` with the judge toolchain on the device host and record the outcome."""
    ws = Path(ws).expanduser().resolve()
    cfg = settings()
    base = {"version": 1, "title": title, "sha": sha, "identity": cfg["identity"],
            "toolchain_env": cfg["toolchain_env"], "flags": cfg["flags"]}
    if not cfg["configured"]:
        return {**base, "status": "skipped", "ok": True, "reason": HINT}
    repo = _problem_repo(ws, title)
    if repo is None or not all((repo / name).is_file() for name in ("CMakeLists.txt", "run.sh", "kernel.asc")):
        return _write(ws, {**base, "status": "skipped", "ok": True,
                           "reason": "only template problems (CMakeLists.txt, run.sh, kernel.asc) are checked"})
    host = host or _host()
    env = dict(os.environ, OPSB_TOOLCHAIN_ENV=cfg["toolchain_env"], OPSB_COMPILE_CHECK=LABEL,
               OPSB_CMAKE_ARGS=json.dumps(cfg["flags"]))
    cmd = [python, str(RUNNER), title, "--sha", sha, "--build-only", "--npu-host", host, "--workspace", str(ws)]
    started = time.monotonic()
    try:
        proc = subprocess.run(cmd, cwd=str(ws), env=env, capture_output=True, text=True, timeout=timeout)
        code, output = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        code, output = -1, f"compile check did not finish: {exc}"
    log_file = Path(ws) / "results" / title / sha / f"{LABEL}-build.log"
    try:
        log = log_file.read_text(encoding="utf-8", errors="replace") or output
    except OSError:
        log = output
    status = classify(code, log)
    record = {**base, "status": status, "ok": status != "fail", "exit_code": code,
              "seconds": round(time.monotonic() - started, 2), "host": host,
              "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "log": str(log_file) if log_file.is_file() else None,
              "summary": "" if status == "pass" else error_summary(log)}
    return _write(ws, record)


def _write(ws: Path, record: dict[str, Any]) -> dict[str, Any]:
    path = record_path(ws, record["title"], record["sha"])
    atomic_write_json(path, record)
    path.with_name(f".{RECORD}.running").unlink(missing_ok=True)
    return record


def _host() -> str:
    try:
        from cli.opsb_core.config import ConfigRepository  # type: ignore
        return str(ConfigRepository.load().get("npu_ssh_host") or "npu")
    except Exception:
        return "npu"


def stage1(ws: Path, title: str, sha: str) -> tuple[dict[str, Any] | None, str]:
    """Gate Stage 1 hook: (rejection fields or None, note appended to the passing reason)."""
    cfg = settings()
    if not cfg["configured"]:
        return None, "; judge compile check: not configured"
    doc = read_record(ws, title, sha, cfg)
    status = (doc or {}).get("status")
    if status == "fail":
        return {"code": "JUDGE_COMPILE_FAILED",
                "reason": "Does not compile with the judge toolchain: " + str(doc.get("summary") or "see log"),
                "metrics": {"log": doc.get("log"), "exit_code": doc.get("exit_code")}}, ""
    if status == "pass":
        return None, "; compiles with the judge toolchain"
    if status in ("error", "skipped"):
        return None, f"; judge compile check {status}: {str(doc.get('summary') or doc.get('reason') or '')[:120]}"
    return None, "; judge compile check pending (runs before upload)"


def preflight(ws: Path, title: str, sha: str, *, python: str = sys.executable) -> dict[str, Any]:
    """Decide for the uploader without blocking; start the check in the background when missing."""
    ws = Path(ws).expanduser().resolve()
    cfg = settings()
    if not cfg["configured"]:
        return {"action": "upload", "status": "unconfigured", "reason": HINT}
    doc = read_record(ws, title, sha, cfg)
    if doc and doc.get("status") in ("pass", "error", "skipped"):
        return {"action": "upload", "status": doc["status"], "reason": doc.get("summary") or doc.get("reason") or ""}
    if doc and doc.get("status") == "fail":
        return {"action": "hold", "status": "fail", "reason": doc.get("summary") or ""}
    marker = record_path(ws, title, sha).with_name(f".{RECORD}.running")
    try:
        running = json.loads(marker.read_text())
    except (OSError, ValueError):
        running = {}
    if _pid_alive(running.get("pid")) and running.get("identity") == cfg["identity"]:
        return {"action": "wait", "status": "running", "pid": running["pid"]}
    marker.parent.mkdir(parents=True, exist_ok=True)
    with (marker.parent / f"{LABEL}-compile-check.out").open("ab") as out:
        child = subprocess.Popen([python, "-m", "scheduler.judge_compile", "check", "--workspace", str(ws),
                                  "--title", title, "--sha", sha], cwd=str(ROOT), stdin=subprocess.DEVNULL,
                                 stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    atomic_write_json(marker, {"pid": child.pid, "identity": cfg["identity"], "started": time.time()})
    return {"action": "wait", "status": "started", "pid": child.pid}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scheduler.judge_compile", description=__doc__.split("\n")[0])
    parser.add_argument("action", choices=("check", "preflight", "show"))
    parser.add_argument("--workspace", "-w", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--host")
    args = parser.parse_args(argv)
    ws = Path(args.workspace).expanduser().resolve()
    if not re.fullmatch(r"[0-9a-f]{40}", args.sha):
        parser.error("--sha must be a full 40-character commit")
    if args.action == "check":
        doc = run_check(ws, args.title, args.sha, host=args.host)
    elif args.action == "preflight":
        doc = preflight(ws, args.title, args.sha)
    else:
        doc = read_record(ws, args.title, args.sha) or {"status": "none", **settings()}
    print(json.dumps(doc, ensure_ascii=False))
    return 1 if doc.get("status") == "fail" else 0


if __name__ == "__main__":
    sys.exit(main())
