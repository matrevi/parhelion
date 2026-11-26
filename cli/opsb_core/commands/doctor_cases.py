"""`opsb doctor --cases [--remote]` and `opsb doctor clean --purge-cases`: the prepared-case cache.

Reference runs cache inputs and goldens on the device host (`runtime/npu_runtime/case_cache.py`).
The report runs that same module with a short footer, on the device host over SSH with
`--remote`, otherwise on this machine, so the budget the runs enforce and the numbers shown
here share one definition. The report is read-only; `doctor clean --apply` never touches the
cache. Only `doctor clean --purge-cases --apply` deletes entries (statistics are kept).
"""
from __future__ import annotations

import datetime
import json
import posixpath
import subprocess
import sys
from typing import Any

from ..config import SUPERVISOR_ROOT
from ..device_env import device_text

CASE_CACHE_PY = SUPERVISOR_ROOT / "runtime" / "npu_runtime" / "case_cache.py"
CASES_PREFIX = "OPSB_DOCTOR_CASES "
DEFAULT_RECENT = 20
CASES_TIMEOUT_SEC = 120

_FOOTER = r'''
_TOP = find_root(CONFIGURED_DIR)
_BUDGET = total_budget({"case_cache_bytes": CONFIGURED_BYTES})
_PURGED = None
if PURGE:
    _removed, _freed = purge(_TOP)
    record(_TOP, purged=_removed, freed=_freed)
    _PURGED = {"entries": _removed, "bytes": _freed}
print(OUTPUT_PREFIX + json.dumps(dict(report(_TOP, _BUDGET, RECENT), purged=_PURGED)))
'''


def cases_script(*, purge: bool = False, recent: int = DEFAULT_RECENT, configured_bytes: str | None = None,
                 configured_dir: str | None = None) -> str:
    """The cache module's source plus a footer; its purge branch runs only when `purge` is set."""
    configured = device_text("case_cache_bytes") if configured_bytes is None else configured_bytes
    head = (f"CONFIGURED_BYTES = {(configured or None)!r}\n"
            f"CONFIGURED_DIR = {(configured_dir or None)!r}\n"
            f"PURGE = {bool(purge)!r}\n"
            f"RECENT = {max(0, int(recent))}\nOUTPUT_PREFIX = {CASES_PREFIX!r}\n")
    return CASE_CACHE_PY.read_text(encoding="utf-8") + "\n" + head + _FOOTER


def scan_cases(host: str | None, *, purge: bool = False, recent: int = DEFAULT_RECENT,
               configured_dir: str | None = None,
               env: dict[str, str] | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """Run the cache report on `host` (None: this machine); return (report, error)."""
    from .doctor_cmd import remote_command

    cmd = remote_command(host) if host else [sys.executable, "-"]
    where = f"ssh {host}" if host else "local"
    try:
        proc = subprocess.run(cmd, input=cases_script(purge=purge, recent=recent, configured_dir=configured_dir),
                              capture_output=True, text=True, timeout=CASES_TIMEOUT_SEC, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"{where}: {exc}"
    for line in reversed((proc.stdout or "").splitlines()):
        if line.startswith(CASES_PREFIX):
            try:
                doc = json.loads(line[len(CASES_PREFIX):])
            except ValueError:
                break
            doc["host"] = host or "local"
            return doc, None if proc.returncode == 0 else f"{where}: exit {proc.returncode}"
    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
    return None, f"{where}: exit {proc.returncode}" + (f": {detail[-1][:200]}" if detail else "")


def _size(n: Any) -> str:
    value = float(n or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TiB"


def _when(epoch: Any, now: float) -> str:
    if not isinstance(epoch, (int, float)):
        return "-"
    stamp = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    hours = max(0.0, (now - epoch) / 3600.0)
    return f"{stamp} ({hours:.1f} h ago)" if hours < 48 else f"{stamp} ({hours / 24:.1f} d ago)"


def print_cases(doc: dict[str, Any]) -> None:
    now = float(doc.get("now") or 0)
    print(f"Case cache on {doc.get('host')}: {doc.get('root')}" + ("" if doc.get("exists") else " (absent)"))
    print(f"  Total {_size(doc.get('total_bytes'))} of {_size(doc.get('budget_bytes'))} global budget "
          f"({_size(doc.get('per_problem_budget_bytes'))} per problem, "
          f"{int(doc.get('max_age_sec') or 0) // 86400}-day age limit), {doc.get('entries', 0)} entries")
    print(f"  Oldest entry last used {_when(doc.get('oldest_used'), now)}")
    recent = doc.get("recent") or {}
    rate = recent.get("hit_rate")
    print(f"  Last {recent.get('runs', 0)} preparations: {recent.get('hits', 0)} hits / "
          f"{recent.get('misses', 0)} misses" + (f" ({rate * 100:.1f}% hit rate)" if rate is not None else "")
          + f", {recent.get('evicted', 0)} evicted")
    if doc.get("purged"):
        print(f"  Purged {doc['purged']['entries']} entries ({_size(doc['purged']['bytes'])})")
    problems = doc.get("problems") or []
    if problems:
        print(f"  {'Problem':<30} {'Entries':>7} {'Size':>10}  Oldest use")
    for row in problems:
        print(f"  {row['title']:<30} {row['entries']:>7} {_size(row['bytes']):>10}  {_when(row.get('oldest_used'), now)}")


def _host(cfg: dict[str, Any], remote: bool) -> str | None:
    return str(cfg.get("npu_ssh_host") or "npu") if remote else None


def _configured_dir(host: str | None) -> str | None:
    """The device host's cache directory from `device.*`, so only --remote can use it.

    `case_cache_dir` names it; with only `case_tmp_dir` set, runs keep the cache next to their
    scratch (`<case_tmp_dir>/opsb-case-cache`, see CaseCache.top), so the report looks there.
    """
    if not host:
        return None
    cache = device_text("case_cache_dir")
    if cache:
        return cache
    scratch = device_text("case_tmp_dir")
    return posixpath.join(scratch, "opsb-case-cache") if scratch else None


def run_cases(args: Any, cfg: dict[str, Any]) -> int:
    """`opsb doctor --cases`: read-only report."""
    host = _host(cfg, bool(getattr(args, "remote", False)))
    doc, error = scan_cases(host, recent=int(getattr(args, "recent", None) or DEFAULT_RECENT),
                            configured_dir=_configured_dir(host))
    if getattr(args, "json", False):
        print(json.dumps({"cases": doc, "error": error}, ensure_ascii=False, indent=2))
    elif doc is not None:
        print_cases(doc)
    if error:
        print(f"error: {error}", file=sys.stderr)
    return 1 if error else 0


def purge_section(cfg: dict[str, Any], remote: bool, apply: bool) -> dict[str, Any]:
    """`doctor clean --purge-cases`: the report, and with `apply` the purge itself."""
    host = _host(cfg, remote)
    doc, error = scan_cases(host, purge=apply, configured_dir=_configured_dir(host))
    section: dict[str, Any] = {"report": doc, "applied": bool(apply and doc and doc.get("purged") is not None)}
    if error:
        section["error"] = error
    return section


def print_purge(section: dict[str, Any]) -> None:
    doc = section.get("report") or {}
    print("\nCase cache purge:")
    if section.get("error"):
        print(f"  error: {section['error']}")
    if not doc:
        return
    if section.get("applied"):
        purged = doc.get("purged") or {}
        print(f"  deleted {purged.get('entries', 0)} entries ({_size(purged.get('bytes'))}) under {doc.get('root')}")
    else:
        print(f"  would delete {doc.get('entries', 0)} entries ({_size(doc.get('total_bytes'))}) under "
              f"{doc.get('root')} on {doc.get('host')} (pass --apply)")


def register_cases_flags(doctor: Any, clean: Any) -> None:
    doctor.add_argument("--cases", action="store_true",
                        help="Show the prepared-case cache: use per problem, budget, hit rate (read-only)")
    doctor.add_argument("--remote", action="store_true", help="With --cases, inspect the device host")
    doctor.add_argument("--recent", type=int, default=DEFAULT_RECENT, metavar="N",
                        help=f"With --cases, hit rate over the last N preparations (default {DEFAULT_RECENT})")
    doctor.add_argument("--json", action="store_true")
    clean.add_argument("--purge-cases", action="store_true",
                       help="Also delete the prepared-case cache (with --remote: on the device host; --apply only)")
