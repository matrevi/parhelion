"""Asynchronous test-run registry: submit `opsb test` in the background, poll, wait, cancel, deliver.

Records live at ``resolve_workspace_state_dir(ws) / "test_runs" / "<request_id>.json"``. The orchestration
package reads finished records only through ``list_undelivered``, ``mark_delivered`` and ``compact_line``.
"""
from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

from .. import config as _cfg
from ..drivers.base import resolve_workspace_state_dir
from .contest_service import resolve_workspace_meta

LEVELS = ("smoke", "standard", "final")
ACTIVE = ("queued", "running")
TERMINAL = ("finished", "failed", "cancelled")
REUSE_WINDOW_SEC = 30 * 60
WAIT_MAX_SEC = 45
GC_INTERVAL_SEC = 3600
POLL_SEC = 1.0
VIEW_LIMIT = 2500
ERROR_LIMIT = 600
LINE_LIMIT = 600
STARTED_PREFIX = "OPSB_RUN_STARTED "
CMD_OVERRIDE_ENV = "OPSB_TEST_CMD_OVERRIDE"
_FINAL_KEYS = ("run_id", "exit_code", "digest", "report", "reused", "joined", "deferred", "reason")


# ---------------------------------------------------------------- problem / revision resolution

def _find_problem(problems: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    q = query.strip().lower()
    return next((p for p in problems if str(p.get("title", "")).lower() == q), None)


def _recent_commits(repo: Path, count: int = 5) -> list[str]:
    try:
        out = subprocess.run(["git", "-C", str(repo), "log", f"-{count}", "--format=%h %s"],
                             capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return []
    return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()] if out.returncode == 0 else []


def _problem_commit(workspace: Path, problem: str, revision: str) -> tuple[str, str]:
    """Resolve a problem title and revision to (canonical title, full commit SHA)."""
    prob = _find_problem(resolve_workspace_meta(workspace).get("problems") or [], problem)
    if not prob:
        raise ValueError(f"Unknown problem: {problem}")
    repo = workspace / prob["path"]
    if not (repo / ".git").exists():
        raise ValueError(f"No local problem repository for {problem}")
    ref = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--end-of-options",
                          revision + "^{commit}"], capture_output=True, text=True, timeout=3)
    if ref.returncode:
        recent = _recent_commits(repo)
        listing = "; ".join(recent) if recent else "no commits found"
        raise ValueError(f"Cannot resolve {revision} for {problem}. Pick one of the recent commits "
                         f"(sha7 subject): {listing}")
    return prob["title"], ref.stdout.strip()


def _compare_ref(workspace: Path, problem: str, compare: str) -> str:
    """Baseline of an A/B paired run: ``board`` (resolved by the run) or a commit of the problem."""
    text = str(compare).strip()
    return "board" if text.lower() == "board" else _problem_commit(workspace, problem, text)[1]


# ---------------------------------------------------------------- registry storage

def registry_dir(ws: Path | str) -> Path:
    return resolve_workspace_state_dir(ws) / "test_runs"


def _record_path(ws: Path | str, request_id: str) -> Path:
    if len(request_id) != 32 or any(c not in "0123456789abcdef" for c in request_id):
        raise ValueError(f"Invalid request_id: {request_id!r}")
    return registry_dir(ws) / f"{request_id}.json"


@contextlib.contextmanager
def _flock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _read(ws: Path | str, request_id: str) -> dict[str, Any] | None:
    try:
        data = json.loads(_record_path(ws, request_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write(ws: Path | str, record: dict[str, Any]) -> None:
    path = _record_path(ws, record["request_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _update(ws: Path | str, request_id: str, **fields: Any) -> dict[str, Any]:
    with _flock(_record_path(ws, request_id).with_suffix(".lock")):
        record = _read(ws, request_id)
        if record is None:
            raise ValueError(f"Unknown test request: {request_id}")
        record.update(fields)
        _write(ws, record)
        return record


def _load(ws: Path | str, request_id: str) -> dict[str, Any]:
    record = _read(ws, request_id)
    if record is None:
        raise ValueError(f"Unknown test request: {request_id}")
    return record


def _all_records(ws: Path | str) -> list[dict[str, Any]]:
    root = registry_dir(ws)
    if not root.is_dir():
        return []
    out = []
    for path in root.glob("*.json"):
        rec = _read(ws, path.stem) if len(path.stem) == 32 else None
        if rec is not None:
            out.append(rec)
    out.sort(key=lambda r: str(r.get("created_at") or ""))
    return out


def _now_iso() -> str:
    return _cfg.utc_now().isoformat()


def _age_sec(iso: Any) -> float | None:
    try:
        ts = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.timezone.utc)
    return (_cfg.utc_now() - ts).total_seconds()


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ---------------------------------------------------------------- submission

def _dedup_key(rec: dict[str, Any]) -> tuple[Any, ...]:
    return (rec.get("problem"), rec.get("sha"), rec.get("level"), tuple(sorted(rec.get("cases") or [])),
            bool(rec.get("build_only")), rec.get("compare") or None)


def _base_command(ws: Path) -> list[str]:
    override = os.environ.get(CMD_OVERRIDE_ENV, "").strip()
    if override:
        return [override]
    return [_cfg.resolve_python_exe(ws), str(_cfg.CLI_DIR / "opsb")]


def build_command(ws: Path, title: str, sha: str, level: str, cases: list[str], remeasure: bool,
                  build_only: bool, compare: str | None = None) -> list[str]:
    cmd = [*_base_command(ws), "test", title, "--workspace", str(ws), "--sha", sha, "--level", level]
    if compare:
        cmd.extend(["--compare", compare])
    for case_id in cases:
        cmd.extend(["--case", case_id])
    if remeasure:
        cmd.append("--remeasure")
    if build_only:
        cmd.append("--build-only")
    return cmd


def _normalize_cases(cases: Any) -> list[str]:
    if cases is None:
        return []
    if not isinstance(cases, list) or not all(isinstance(c, str) and c.strip() for c in cases):
        raise ValueError("cases must be a list of nonempty case IDs")
    return [c.strip() for c in cases]


def submit(ws: Path, *, problem: str, sha: str | None = None, level: str | None = "standard",
           cases: list[str] | None = None, remeasure: bool = False, build_only: bool = False,
           agent_id: str | None = None, role: str = "orchestrator", compare: str | None = None) -> dict[str, Any]:
    """Start (or join / reuse) a background `opsb test` run and return its public view immediately."""
    ws = Path(ws).expanduser().resolve()
    req_level = str(level or "standard").strip().lower()
    if req_level not in LEVELS:
        raise ValueError(f"level must be one of {'|'.join(LEVELS)}")
    case_ids = _normalize_cases(cases)
    title, full_sha = _problem_commit(ws, problem, str(sha or "HEAD"))
    rec_level = "build" if build_only else req_level
    gc(ws)

    compare = _compare_ref(ws, title, compare) if compare and not build_only else None
    candidate = {"problem": title, "sha": full_sha, "level": rec_level, "cases": case_ids, "build_only": bool(build_only),
                 "compare": compare}
    key = _dedup_key(candidate)
    with _flock(registry_dir(ws) / ".submit.lock"):
        for rec in reversed(_all_records(ws)):
            if _dedup_key(rec) != key:
                continue
            if rec.get("status") in ACTIVE and _pid_alive(rec.get("pid")):
                return {**public_view(rec), "joined": True}
            if (not remeasure and rec.get("status") == "finished" and rec.get("exit_code") == 0
                    and (age := _age_sec(rec.get("finished_at"))) is not None and age <= REUSE_WINDOW_SEC):
                return {**public_view(rec), "reused": True}

        request_id = uuid.uuid4().hex
        log_path = registry_dir(ws) / f"{request_id}.log"
        record: dict[str, Any] = {
            "request_id": request_id, **candidate, "remeasure": bool(remeasure),
            "agent_id": agent_id or None, "role": role, "created_at": _now_iso(), "pid": None,
            "log": str(log_path), "status": "queued", "run_id": None, "exit_code": None, "digest": None,
            "report": None, "finished_at": None, "observed": False, "delivered": False,
            "progress": None, "joined": None, "reused": None, "deferred": None, "reason": None, "error": None,
        }
        _write(ws, record)
        cmd = build_command(ws, title, full_sha, req_level, case_ids, bool(remeasure), bool(build_only), compare)
        env = dict(os.environ)
        if role == "worker":
            env["PARHELION_MCP_ROLE"] = "worker"
        try:
            with log_path.open("wb") as log_fh:
                proc = subprocess.Popen(cmd, cwd=str(ws), stdin=subprocess.DEVNULL, stdout=log_fh,
                                        stderr=subprocess.STDOUT, start_new_session=True, env=env)
        except OSError as exc:
            record = _update(ws, request_id, status="failed", finished_at=_now_iso(),
                             error=f"Cannot launch test command: {exc}"[:ERROR_LIMIT])
            return public_view(record)
        record = _update(ws, request_id, pid=proc.pid, status="running")

    def _reap() -> None:
        returncode = proc.wait()
        try:
            _finalize(ws, record, returncode=returncode)
        except Exception:
            pass

    threading.Thread(target=_reap, name=f"test-run-{request_id[:8]}", daemon=True).start()
    return public_view(record)


# ---------------------------------------------------------------- completion / progress

def _log_text(record: dict[str, Any], limit: int = 512 * 1024) -> str:
    path = Path(str(record.get("log") or ""))
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - limit))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _final_json(text: str) -> dict[str, Any] | None:
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _finalize(ws: Path | str, record: dict[str, Any], *, returncode: int | None = None) -> dict[str, Any]:
    """Settle a run from its log: last JSON line wins; exit 0/2 is finished, anything else failed."""
    request_id = record["request_id"]
    with _flock(_record_path(ws, request_id).with_suffix(".lock")):
        current = _read(ws, request_id) or dict(record)
        if current.get("status") in TERMINAL:
            return current
        text = _log_text(current)
        final = _final_json(text)
        fields: dict[str, Any] = {"finished_at": _now_iso()}
        if final is not None:
            fields.update({k: final[k] for k in _FINAL_KEYS if k in final})
        exit_code = fields.get("exit_code", returncode)
        fields["exit_code"] = exit_code
        if current.get("cancel_requested"):
            fields["status"] = "cancelled"
        elif final is not None and exit_code in (0, 2):
            fields["status"] = "finished"
        else:
            fields["status"] = "failed"
            fields["error"] = text[-1500:] or f"test command exited with {returncode}"
        current.update(fields)
        _write(ws, current)
        return current


def _progress(record: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    started = None
    for line in reversed(_log_text(record).splitlines()):
        if line.startswith(STARTED_PREFIX):
            try:
                started = json.loads(line[len(STARTED_PREFIX):])
            except ValueError:
                started = None
            if isinstance(started, dict):
                break
            started = None
    if started is None:
        return {"phase": "starting", "cases_done": 0, "selected": None, "wait_sec": None}, None
    progress: dict[str, Any] = {"phase": "started", "cases_done": 0, "selected": started.get("selected"), "wait_sec": None}
    events = Path(str(started.get("dir") or "")) / "events.jsonl"
    try:
        lines = events.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        progress["phase"] = ev.get("event") or progress["phase"]
        if ev.get("event") == "case":
            progress["cases_done"] += 1
        elif ev.get("event") == "device_batch" and ev.get("wait_sec") is not None:
            progress["wait_sec"] = ev.get("wait_sec")
    run_id = started.get("run_id")
    return progress, str(run_id) if run_id else None


def refresh(ws: Path | str, request_id: str) -> dict[str, Any]:
    record = _load(ws, request_id)
    if record.get("status") == "queued" and not record.get("pid") and (_age_sec(record.get("created_at")) or 0) > 60:
        return _update(ws, request_id, status="failed", finished_at=_now_iso(),
                       error="Test command was never launched")
    if record.get("status") != "running":
        return record
    if not _pid_alive(record.get("pid")):
        return _finalize(ws, record)
    progress, run_id = _progress(record)
    fields: dict[str, Any] = {"progress": progress}
    if run_id and not record.get("run_id"):
        fields["run_id"] = run_id
    with _flock(_record_path(ws, request_id).with_suffix(".lock")):
        current = _read(ws, request_id) or record
        if current.get("status") == "running":
            current.update(fields)
            _write(ws, current)
        return current


# ---------------------------------------------------------------- views

def _shrink_digest(digest: Any) -> Any:
    if not isinstance(digest, dict):
        return digest
    out = dict(digest)
    for section in ("timing", "public", "baseline"):
        if isinstance(out.get(section), dict) and isinstance(out[section].get("cases"), dict):
            out[section] = {**out[section], "cases": dict(list(out[section]["cases"].items())[:3])}
    if isinstance(out.get("failures"), list):
        out["failures"] = out["failures"][:2]
    return out


def public_view(record: dict[str, Any]) -> dict[str, Any]:
    """Caller-facing projection of a registry record, at most ~2.5KB serialized."""
    error = record.get("error")
    view: dict[str, Any] = {
        "request_id": record.get("request_id"), "problem": record.get("problem"),
        "sha7": str(record.get("sha") or "")[:7], "level": record.get("level"), "status": record.get("status"),
        "run_id": record.get("run_id"), "exit_code": record.get("exit_code"), "progress": record.get("progress"),
        "digest": record.get("digest"), "report": record.get("report"), "joined": record.get("joined"),
        "reused": record.get("reused"), "reason": record.get("reason"),
        "error": str(error)[-ERROR_LIMIT:] if error else None,
    }

    def size() -> int:
        return len(json.dumps(view, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    if size() > VIEW_LIMIT:
        view["digest"] = _shrink_digest(view["digest"])
    if size() > VIEW_LIMIT:
        digest = view["digest"] if isinstance(view["digest"], dict) else {}
        view["digest"] = {"summary": digest.get("summary"), "truncated": True}
    if size() > VIEW_LIMIT and view["error"]:
        view["error"] = view["error"][-200:]
    if size() > VIEW_LIMIT and isinstance(view["reason"], str):
        view["reason"] = view["reason"][:200]
    return view


def wait(ws: Path | str, request_id: str, wait_sec: float = 30) -> dict[str, Any]:
    """Poll a run for at most `wait_sec` (clamped to [1, 45]) and return its public view."""
    try:
        budget = float(wait_sec)
    except (TypeError, ValueError):
        budget = 30.0
    budget = min(float(WAIT_MAX_SEC), max(1.0, budget))
    deadline = time.monotonic() + budget
    while True:
        record = refresh(ws, request_id)
        if record.get("status") in TERMINAL:
            if not record.get("observed"):
                record = _update(ws, request_id, observed=True)
            return public_view(record)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return public_view(record)
        time.sleep(min(POLL_SEC, remaining))


def cancel(ws: Path | str, request_id: str) -> dict[str, Any]:
    """SIGTERM the run's process group, SIGKILL after 5 s, and record it as cancelled."""
    record = _load(ws, request_id)
    if record.get("status") in TERMINAL:
        return public_view(record)
    record = _update(ws, request_id, cancel_requested=True)
    pid = record.get("pid")
    if isinstance(pid, int) and pid > 0 and _group_alive(pid):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGTERM)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and _group_alive(pid):
            time.sleep(0.05)
        if _group_alive(pid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pid, signal.SIGKILL)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and _group_alive(pid):
                time.sleep(0.05)
    record = _update(ws, request_id, status="cancelled", finished_at=record.get("finished_at") or _now_iso())
    return public_view(record)


# ---------------------------------------------------------------- delivery (consumed by orchestration)

def list_undelivered(ws: Path | str) -> list[dict[str, Any]]:
    """Ended records with an owning agent that have not been delivered yet (stale runs are settled first)."""
    out = []
    for rec in _all_records(ws):
        if not rec.get("agent_id") or rec.get("delivered"):
            continue
        if rec.get("status") == "running" and not _pid_alive(rec.get("pid")):
            rec = _finalize(ws, rec)
        if rec.get("status") in TERMINAL:
            out.append(rec)
    out.sort(key=lambda r: str(r.get("finished_at") or ""))
    return out


def mark_delivered(ws: Path | str, request_id: str) -> None:
    _update(ws, request_id, delivered=True)


def _fmt_num(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)


def compact_line(record: dict[str, Any]) -> str:
    """One-line (<=600 chars) summary: problem sha7 level status passed/total [· slow case] [· baseline]."""
    digest = record.get("digest") if isinstance(record.get("digest"), dict) else {}
    summary = digest.get("summary") if isinstance(digest.get("summary"), dict) else {}
    passed = summary.get("passed", "?")
    total = summary.get("total", "?")
    line = (f"{record.get('problem')} {str(record.get('sha') or '')[:7]} {record.get('level')} "
            f"{record.get('status')} {passed}/{total}")
    timing = digest.get("timing") if isinstance(digest.get("timing"), dict) else {}
    cases = timing.get("cases") if isinstance(timing.get("cases"), dict) else {}
    slow = [(cid, c.get("min_us")) for cid, c in cases.items()
            if isinstance(c, dict) and isinstance(c.get("min_us"), (int, float))]
    if slow:
        cid, min_us = max(slow, key=lambda item: item[1])
        line += f" · 慢例 {cid} {_fmt_num(min_us)}µs"
    baseline = digest.get("baseline") if isinstance(digest.get("baseline"), dict) else {}
    if isinstance(baseline.get("geomean_speedup"), (int, float)):
        line += f" · 相对基线 ×{_fmt_num(baseline['geomean_speedup'])}"
    if record.get("status") != "finished":
        error_tail = str(record.get("error") or "").strip().splitlines()
        why = str(record.get("reason") or (error_tail[-1] if error_tail else "")).strip()
        if why:
            line += f" · {why}"
    return line[:LINE_LIMIT]


# ---------------------------------------------------------------- housekeeping

def gc(ws: Path | str, keep_days: float = 3) -> int:
    """Delete ended records (and logs/locks) older than `keep_days`; runs at most once per hour."""
    root = registry_dir(ws)
    marker = root / ".gc"
    try:
        if marker.exists() and time.time() - marker.stat().st_mtime < GC_INTERVAL_SEC:
            return 0
        root.mkdir(parents=True, exist_ok=True)
        marker.touch()
    except OSError:
        return 0
    removed = 0
    for rec in _all_records(ws):
        age = _age_sec(rec.get("finished_at"))
        if rec.get("status") not in TERMINAL or age is None or age < keep_days * 86400:
            continue
        rid = rec["request_id"]
        for suffix in (".json", ".log", ".lock"):
            with contextlib.suppress(OSError):
                (root / f"{rid}{suffix}").unlink()
        removed += 1
    return removed


__all__ = [
    "LEVELS",
    "build_command",
    "cancel",
    "compact_line",
    "gc",
    "list_undelivered",
    "mark_delivered",
    "public_view",
    "refresh",
    "registry_dir",
    "submit",
    "wait",
]
