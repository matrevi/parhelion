"""Multiplexed SSH NPU telemetry client & Mac-side waiting task correlator."""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

PROBE_PATH = Path(__file__).resolve().parents[1] / "probe" / "npu_probe.py"
SSH_CONTROL_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=4",
    "-o", "ControlMaster=auto",
    "-o", "ControlPersist=120",
    "-o", "ControlPath=/tmp/opsb-ssh-%C",
]


def _parse_etime_sec(etime: str) -> int:
    s = etime.strip()
    days = 0
    if "-" in s:
        d_part, s = s.split("-", 1)
        days = int(d_part) if d_part.isdigit() else 0
    parts = [int(p) for p in s.split(":") if p.isdigit()]
    if len(parts) == 3:
        return days * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return days * 86400 + parts[0] * 60 + parts[1]
    return parts[0] if parts else 0


def read_mac_npu_waiters() -> list[dict[str, Any]]:
    waiters: list[dict[str, Any]] = []
    try:
        out = subprocess.check_output(["ps", "-eo", "pid,etime,command"], text=True, timeout=1.2)
        seen: set[str] = set()
        for line in out.splitlines():
            if ("npu-test.py" not in line and "opsb test" not in line) or "grep" in line or "uv run" in line:
                continue
            parts = line.strip().split(None, 2)
            if len(parts) < 3 or not parts[0].isdigit():
                continue
            m = re.search(r"(?:npu-test\.py|opsb\s+test)\s+([A-Za-z0-9_]+)", parts[2])
            if m and m.group(1) not in seen:
                prob = m.group(1)
                seen.add(prob)
                waiters.append(
                    {
                        "local_pid": int(parts[0]),
                        "problem": prob,
                        "elapsed_sec": _parse_etime_sec(parts[1]),
                        "fast": "--fast" in parts[2],
                    }
                )
    except Exception:
        pass
    return waiters


def poll_npu_telemetry(host: str, problems_by_title: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    remote_cmd = "if [ -f ~/.opsb/npu_probe.py ]; then python3 ~/.opsb/npu_probe.py; else python3 -; fi"
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            ["ssh", *SSH_CONTROL_OPTS, host, remote_cmd],
            input=PROBE_PATH.read_text(encoding="utf-8"),
            capture_output=True,
            text=True,
            timeout=6.5,
        )
        rtt_ms = round((time.monotonic() - t0) * 1000, 1)
        if proc.returncode != 0:
            return {
                "online": False,
                "ssh_host": host,
                "rtt_ms": rtt_ms,
                "error": (proc.stderr or proc.stdout).strip()[:220] or f"SSH exit {proc.returncode}",
                "die_locks": [],
                "tasks": [],
            }
        data = json.loads(proc.stdout.strip().splitlines()[-1])
        data["online"] = True
        data["ssh_host"] = host
        data["rtt_ms"] = rtt_ms
    except Exception as exc:
        return {
            "online": False,
            "ssh_host": host,
            "rtt_ms": round((time.monotonic() - t0) * 1000, 1),
            "error": str(exc)[:220],
            "die_locks": [],
            "tasks": [],
        }

    p_lookup = problems_by_title or {}
    mac_waiters = read_mac_npu_waiters()
    active_probs = {t.get("problem") for t in data.get("tasks", []) if t.get("problem")}
    unmatched_mac = sorted([w for w in mac_waiters if w["problem"] not in active_probs], key=lambda x: x["elapsed_sec"], reverse=True)
    unassigned = sorted([t for t in data.get("tasks", []) if not t.get("problem") and t.get("stage") == "WAITING-DIE"], key=lambda x: x.get("elapsed_sec", 0), reverse=True)

    used_idx: set[int] = set()
    for wt in unassigned:
        best_i, best_d = None, 999999
        for idx, mw in enumerate(unmatched_mac):
            if idx not in used_idx:
                d = abs(wt.get("elapsed_sec", 0) - mw["elapsed_sec"])
                if d < best_d and d <= 180:
                    best_d, best_i = d, idx
        if best_i is not None:
            used_idx.add(best_i)
            mw = unmatched_mac[best_i]
            wt["problem"] = mw["problem"]
            if mw.get("fast"):
                wt["detail"] = "Waiting die (Fast-lane queued)"

    for t in data.get("tasks", []):
        p_meta = p_lookup.get(t.get("problem") or "", {})
        sha = t.get("sha") or p_meta.get("head_sha") or ""
        t["sha"] = sha or None
        t["short_sha"] = sha[:7] if sha else "—"
        t["difficulty"] = p_meta.get("difficulty")
        t["wspt_priority"] = p_meta.get("wspt_priority", 0.0)
        t["wspt_tag"] = p_meta.get("wspt_tag", "")

    for lk in data.get("die_locks", []):
        sha = lk.get("sha") or p_lookup.get(lk.get("problem") or "", {}).get("head_sha") or ""
        lk["sha"] = sha or None
        lk["short_sha"] = sha[:7] if sha else None

    return data


def deploy_probe(host: str) -> dict[str, Any]:
    install_sh = "mkdir -p ~/.opsb && cat > ~/.opsb/npu_probe.py && chmod 755 ~/.opsb/npu_probe.py && python3 ~/.opsb/npu_probe.py"
    proc = subprocess.run(
        ["ssh", *SSH_CONTROL_OPTS, host, install_sh],
        input=PROBE_PATH.read_text(encoding="utf-8"),
        capture_output=True,
        text=True,
        timeout=12.0,
    )
    if proc.returncode != 0:
        return {"ok": False, "host": host, "error": (proc.stderr or proc.stdout).strip()[:240]}
    snap = json.loads(proc.stdout.strip().splitlines()[-1])
    return {"ok": True, "host": host, "installed_path": "~/.opsb/npu_probe.py", "probe": snap}


def test_ssh_connectivity(host: str) -> dict[str, Any]:
    t0 = time.monotonic()
    proc = subprocess.run(
        ["ssh", *SSH_CONTROL_OPTS, host, "uname -sr && nproc"],
        capture_output=True,
        text=True,
        timeout=6.0,
    )
    ms = round((time.monotonic() - t0) * 1000, 1)
    ok = proc.returncode == 0
    info = proc.stdout.strip().replace("\n", " · ") if ok else (proc.stderr or proc.stdout).strip()[:200]
    return {"ok": ok, "host": host, "latency_ms": ms, "info": info}


def boost_npu_ticket(host: str, pid: int, problem: str = "", sha: str = "") -> dict[str, Any]:
    remote_py = (
        "import json, os, pathlib, stat\n"
        "tdir = pathlib.Path('/tmp/opsb-npu-tickets')\n"
        "tdir.mkdir(parents=True, exist_ok=True)\n"
        f"tp = tdir / '{pid}.json'\n"
        "cur = {}\n"
        "if tp.is_file():\n"
        "    try:\n"
        "        os.chmod(tp, 0o644)\n"
        "        cur = json.loads(tp.read_text())\n"
        "    except Exception: pass\n"
        f"cur.update({{'pid': {pid}, 'title': cur.get('title') or {problem!r}, 'sha': cur.get('sha') or {sha!r}, 'tier': 99, 'prio': 9999.0, 'fast': True, 'created': 1.0}})\n"
        "tp.write_text(json.dumps(cur))\n"
        "os.chmod(tp, 0o444)\n"
        "print(json.dumps({'ok': True, 'pid': cur['pid'], 'boosted': True}))\n"
    )
    proc = subprocess.run(["ssh", *SSH_CONTROL_OPTS, host, "python3", "-c", remote_py], capture_output=True, text=True, timeout=6.0)
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or proc.stdout).strip()[:200]}
    return {"ok": True, "pid": pid, "boosted": True}


def cancel_npu_task(host: str, pid: int) -> dict[str, Any]:
    remote_py = (
        "import json, os, pathlib, signal\n"
        f"pid = {pid}\n"
        "tp = pathlib.Path(f'/tmp/opsb-npu-tickets/{pid}.json')\n"
        "try:\n"
        "    if tp.exists(): os.chmod(tp, 0o644); tp.unlink(missing_ok=True)\n"
        "except Exception: pass\n"
        "killed = False\n"
        "for sig in (signal.SIGTERM,):\n"
        "    try:\n"
        "        pgid = os.getpgid(pid)\n"
        "        if pgid > 1: os.killpg(pgid, sig)\n"
        "        else: os.kill(pid, sig)\n"
        "        killed = True\n"
        "    except Exception:\n"
        "        try: os.kill(pid, sig); killed = True\n"
        "        except Exception: pass\n"
        "print(json.dumps({'ok': True, 'pid': pid, 'killed': killed}))\n"
    )
    proc = subprocess.run(["ssh", *SSH_CONTROL_OPTS, host, "python3", "-c", remote_py], capture_output=True, text=True, timeout=6.0)
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or proc.stdout).strip()[:200]}
    return {"ok": True, "pid": pid}
