"""Reader for the scheduler's automatic perf-bucket backfill status.

The auto-backfill scheduler lives in `scheduler/` and is owned by another workstream.  It
exposes exactly one contract:

    python -m scheduler.backfill_status --json
    -> {"unpaired": [...], "queued": [...], "running": ..., "last_run": "...",
        "eta_sec": N, "ledger_sha": "..."}

This module never imports scheduler code: it shells out, validates the shape, and degrades
to an explicit `available: false` report so the macOS UI can render something honest instead
of crashing or showing a blank banner while that module does not exist yet.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from ..config import CACHE_DIR, SUPERVISOR_ROOT, read_json, resolve_python_exe, utc_now

BACKFILL_MODULE = "scheduler.backfill_status"
_STATUS_KEYS = ("unpaired", "queued", "running", "last_run", "eta_sec", "ledger_sha")
# `opsb status` is on every operator agent's hot path: the probe is hard-capped at 200 ms and
# cached on the ledger sha, so a steady state costs zero subprocesses.
STATUS_BUDGET_SEC = 0.2
CACHE_PATH = CACHE_DIR / ".cache_backfill_status.json"
CACHE_TTL_SEC = 20.0
EMPTY_SHA = hashlib.sha256(b"").hexdigest()


def backfill_module_path() -> Path:
    return SUPERVISOR_ROOT / "scheduler" / "backfill_status.py"


def _norm_items(rows: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, (list, tuple, set)):
        return out
    for row in rows:
        if isinstance(row, str):
            out.append({"label": row, "problem": "", "bucket_id": row, "stratum": "", "detail": ""})
            continue
        if not isinstance(row, dict):
            continue
        bucket = str(row.get("bucket_id") or row.get("bucket") or row.get("id") or row.get("name") or "")
        problem = str(row.get("problem") or row.get("title") or row.get("operator") or "")
        label = str(row.get("label") or bucket or problem or "")
        out.append(
            {
                "label": label,
                "problem": problem,
                "bucket_id": bucket,
                "stratum": str(row.get("stratum") or ""),
                "detail": str(row.get("detail") or row.get("reason") or row.get("status") or ""),
            }
        )
    return out


def _norm_running(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {
            "active": bool(value.get("active", True)),
            "problem": str(value.get("problem") or value.get("title") or ""),
            "bucket_id": str(value.get("bucket_id") or value.get("id") or ""),
            "started_at": str(value.get("started_at") or value.get("started") or ""),
            "detail": str(value.get("detail") or value.get("phase") or ""),
        }
    return {"active": bool(value), "problem": "", "bucket_id": "", "started_at": "", "detail": ""}


def _degraded(reason: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "available": False,
        "reason": reason,
        "source": BACKFILL_MODULE,
        "unpaired": [],
        "unpaired_count": 0,
        "queued": [],
        "queued_count": 0,
        "running": {"active": False, "problem": "", "bucket_id": "", "started_at": "", "detail": ""},
        "last_run": None,
        "eta_sec": None,
        "ledger_sha": None,
        "fetched_at": utc_now().isoformat(),
    }
    payload.update(extra)
    return payload


def _unwrap(obj: Any) -> dict[str, Any] | None:
    if not isinstance(obj, dict):
        return None
    if any(k in obj for k in _STATUS_KEYS):
        return obj
    for key in ("data", "status", "result", "payload"):
        inner = obj.get(key)
        if isinstance(inner, dict) and any(k in inner for k in _STATUS_KEYS):
            return inner
    # A dict without any known key is still better than nothing when it has *some* content.
    return obj if obj else None


def _spawn_status_module(
    workspace: Path | None,
    *,
    timeout: float,
    offline: bool,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Run `python -m scheduler.backfill_status --json` once; never raises."""
    mod = backfill_module_path()
    meta: dict[str, Any] = {"module_path": str(mod), "offline": offline}
    if not mod.is_file():
        meta["reason"] = "module_missing"
        return None, meta

    py_exe = resolve_python_exe(Path(workspace) if workspace else None)
    argv = [py_exe, "-m", BACKFILL_MODULE, "--json"]
    if offline:
        argv.append("--offline")
    if workspace:
        argv += ["--workspace", str(workspace)]
    try:
        proc = subprocess.run(
            argv,
            cwd=str(SUPERVISOR_ROOT),
            capture_output=True,
            text=True,
            timeout=max(0.05, float(timeout)),
        )
    except subprocess.TimeoutExpired:
        meta["reason"] = "timeout"
        meta["timeout_sec"] = timeout
        return None, meta
    except Exception as exc:  # pragma: no cover - defensive
        meta["reason"] = "spawn_failed"
        meta["error"] = str(exc)[:300]
        return None, meta

    if proc.returncode != 0:
        meta["reason"] = "nonzero_exit"
        meta["returncode"] = proc.returncode
        meta["stderr"] = (proc.stderr or "").strip()[:400]
        meta["stdout"] = (proc.stdout or "").strip()[:200]
        return None, meta

    raw = (proc.stdout or "").strip()
    if not raw:
        meta["reason"] = "empty_output"
        meta["stderr"] = (proc.stderr or "").strip()[:300]
        return None, meta
    parsed: Any = None
    for candidate in (raw, raw.splitlines()[-1] if raw.splitlines() else ""):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
            break
        except Exception:
            continue
    body = _unwrap(parsed)
    if body is None:
        meta["reason"] = "bad_json"
        meta["stdout"] = raw[:300]
        return None, meta
    meta["reason"] = ""
    return body, meta


def read_backfill_status(workspace: Path | None = None, *, timeout: float = 8.0, offline: bool = False) -> dict[str, Any]:
    """Rich report (interactive: `opsb bench backfill-status`); degrades to `available: false`."""
    mod = backfill_module_path()
    body, meta = _spawn_status_module(workspace, timeout=timeout, offline=offline)
    if body is None:
        reason = str(meta.get("reason") or "unavailable")
        extra: dict[str, Any] = {"module_path": str(mod), "offline": offline}
        if reason == "module_missing":
            extra["hint"] = (
                "scheduler.backfill_status is not installed yet; the banner falls back to "
                "`calibration.unpaired_bucket_count` + the manual backfill button."
            )
        for key in ("returncode", "stderr", "stdout", "error", "timeout_sec"):
            if key in meta:
                extra[key] = meta[key]
        return _degraded(reason, **extra)

    unpaired = _norm_items(body.get("unpaired"))
    queued = _norm_items(body.get("queued"))
    unpaired_count = body.get("unpaired_count")
    if not isinstance(unpaired_count, (int, float)):
        unpaired_count = len(unpaired) if unpaired else int(body.get("unpaired") or 0) if isinstance(body.get("unpaired"), (int, float)) else 0
    queued_count = body.get("queued_count")
    if not isinstance(queued_count, (int, float)):
        queued_count = len(queued) if queued else int(body.get("queued") or 0) if isinstance(body.get("queued"), (int, float)) else 0
    eta = body.get("eta_sec")
    try:
        eta_sec = None if eta is None else float(eta)
    except (TypeError, ValueError):
        eta_sec = None

    return {
        "available": True,
        "reason": "",
        "source": BACKFILL_MODULE,
        "module_path": str(meta.get("module_path") or mod),
        "offline": offline,
        "generated_at": body.get("generated_at"),
        "unpaired": unpaired,
        "unpaired_count": int(unpaired_count),
        "queued": queued,
        "queued_count": int(queued_count),
        "running": _norm_running(body.get("running")),
        "last_run": body.get("last_run"),
        "eta_sec": eta_sec,
        "ledger_sha": body.get("ledger_sha"),
        "fetched_at": utc_now().isoformat(),
    }


def filter_backfill_status(report: dict[str, Any], problem: str | None) -> dict[str, Any]:
    """Narrow a global report to one operator without losing the global counts."""
    if not problem or not report.get("available"):
        return report
    target = problem.strip().lower()

    def _keep(item: dict[str, str]) -> bool:
        hay = " ".join([item.get("problem", ""), item.get("label", ""), item.get("bucket_id", "")]).lower()
        return target in hay

    narrowed = dict(report)
    all_unpaired = report.get("unpaired") or []
    all_queued = report.get("queued") or []
    narrowed["unpaired_all_count"] = report.get("unpaired_count", len(all_unpaired))
    narrowed["queued_all_count"] = report.get("queued_count", len(all_queued))
    narrowed["unpaired"] = [i for i in all_unpaired if _keep(i)]
    narrowed["queued"] = [i for i in all_queued if _keep(i)]
    narrowed["unpaired_count"] = len(narrowed["unpaired"])
    narrowed["queued_count"] = len(narrowed["queued"])
    narrowed["scope_problem"] = problem
    return narrowed


# --------------------------------------------------------------------------------------
# Compact passthrough for `opsb status` (`scheduler.backfill`)
# --------------------------------------------------------------------------------------

LEDGER_NAME = "backfill_ledger.jsonl"
CONFIG_NAME = "backfill_config.json"


def compact_backfill(rich: dict[str, Any]) -> dict[str, Any]:
    """Project the rich `backfill_status` payload into the 10-field banner contract.

    Contract (additive fields allowed)::

        {lane, tier, auto_enabled, unpaired_total, unpaired_by_operator{},
         running, running_title, last_run, est_session_sec, ledger_sha}
    """
    per: dict[str, int] = {}
    for row in rich.get("unpaired") or []:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or row.get("problem") or row.get("operator") or "")
        if title:
            per[title] = per.get(title, 0) + 1
    cfg = rich.get("config") if isinstance(rich.get("config"), dict) else {}
    running = rich.get("running") if isinstance(rich.get("running"), dict) else None
    total = rich.get("unpaired_count")
    if not isinstance(total, (int, float)):
        total = sum(per.values())
    last_run = rich.get("last_run")
    if isinstance(last_run, dict):
        last_run = {
            "title": last_run.get("title"),
            "result": last_run.get("result"),
            "finished_at": last_run.get("finished_at"),
            "measured_count": last_run.get("measured_count"),
        }
    return {
        "lane": rich.get("lane") or "B0",
        "tier": rich.get("tier") if rich.get("tier") is not None else 0,
        "auto_enabled": bool(cfg.get("enabled") and cfg.get("auto_dispatch")),
        "unpaired_total": int(total),
        # `*_proxy` / parser-artifact ids can never be measured: keep them out of the queue math
        # but visible, so the banner explains why the census did not move.
        "spec_placeholder_total": int(rich.get("spec_placeholder_total") or 0),
        "spec_placeholder_by_operator": dict(rich.get("spec_placeholder_by_operator") or {}),
        "unpaired_by_operator": per,
        "running": bool(running),
        "running_title": (running or {}).get("title"),
        "last_run": last_run,
        "est_session_sec": rich.get("est_session_sec"),
        "ledger_sha": rich.get("ledger_sha"),
        # additive, useful for the banner
        "queued_count": rich.get("queued_count"),
        "eta_sec": rich.get("eta_sec"),
        "eta_wall_sec": rich.get("eta_wall_sec"),
        "generated_at": rich.get("generated_at"),
        "ledger_path": rich.get("ledger_path"),
    }


def _state_paths(ledger_path: str | None, workspace: Path | None) -> list[Path]:
    out: list[Path] = []
    if ledger_path:
        out.append(Path(ledger_path))
    if workspace:
        ws = Path(workspace)
        out.append(ws / "ops-supervisor" / "scheduler" / ".state" / LEDGER_NAME)
        out.append(ws / "scheduler" / ".state" / LEDGER_NAME)
    out.append(SUPERVISOR_ROOT / "scheduler" / ".state" / LEDGER_NAME)
    return out


def _ledger_fingerprint(ledger_path: str | None, workspace: Path | None) -> str:
    """Cheap change key: ledger sha + config mtime (both change on any lane activity)."""
    path: Path | None = None
    for candidate in _state_paths(ledger_path, workspace):
        if candidate.is_file():
            path = candidate
            break
    if path is None:
        path = _state_paths(ledger_path, workspace)[0]
    try:
        ledger_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        ledger_sha = EMPTY_SHA
    cfg_path = path.parent / CONFIG_NAME
    try:
        cfg_stamp = str(cfg_path.stat().st_mtime_ns)
    except OSError:
        cfg_stamp = "0"
    return f"{ledger_sha}|{cfg_stamp}"


def _write_cache(payload: dict[str, Any]) -> None:
    try:
        CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, CACHE_PATH)
    except Exception:
        pass


def probe_backfill_compact(
    workspace: Path | None = None,
    *,
    budget_sec: float = STATUS_BUDGET_SEC,
    force: bool = False,
) -> dict[str, Any] | None:
    """`scheduler.backfill` for `opsb status`: bounded at `budget_sec`, cached on the ledger sha.

    Returns ``None`` — never an exception, never a slow call — when the module is missing,
    slower than the budget, exits non-zero, or emits something unparsable.
    """
    cached = read_json(CACHE_PATH, None)
    if isinstance(cached, dict) and not force:
        compact = cached.get("compact")
        if isinstance(compact, dict):
            age = time.time() - float(cached.get("ts") or 0.0)
            fp = _ledger_fingerprint(str(cached.get("ledger_path") or ""), workspace)
            if cached.get("ledger_fp") == fp and 0 <= age <= CACHE_TTL_SEC:
                hit = dict(compact)
                hit["cached"] = True
                hit["cache_age_sec"] = round(age, 1)
                return hit
    rich, meta = _spawn_status_module(workspace, timeout=budget_sec, offline=True)
    if rich is None:
        return None
    compact = compact_backfill(rich)
    _write_cache(
        {
            "ts": time.time(),
            "ledger_fp": _ledger_fingerprint(rich.get("ledger_path"), workspace),
            "ledger_path": str(rich.get("ledger_path") or ""),
            "compact": compact,
        }
    )
    out = dict(compact)
    out["cached"] = False
    return out
