"""CLI command: `opsb doctor clean [--apply] [--remote] [--agents] [--logs DAYS] [--purge-trash] [--json]`.

Finds state left behind by test runs, finished agents, expired transcripts and dead
device tickets:

* local: `PARHELION_HOME/workspaces/*` directories that are test pollution — names
  starting with `bus.db-`, a `meta.json` whose `workspace` lies under `/pytest-of-`, or a
  `ws-*` directory whose recorded workspace no longer exists. Directories belonging to a
  workspace registered in `config.json` (`workspaces`, `active_workspace`) are never
  touched. `--apply` moves the hits to `PARHELION_HOME/trash/<UTC timestamp>/`; nothing
  is deleted.
* agents (`--agents`): per-agent sandboxes under `workspaces/<id>/agents/<agent_id>` whose
  agent is finished — the same predicate `AgentBusStore.cleanup_stopped` uses (`stopped`
  or `error`, and neither `pid` nor `pgid` left behind) — plus sandboxes the store has no
  record of at all. A registered workspace's *state* is still never moved; only these
  sandboxes, and only when the store proves the agent is gone. `--apply` moves them into
  the same trash batch.
* logs (`--logs DAYS`, `--logs-max-mb MB`): driver session transcripts under
  `PARHELION_HOME/logs/sessions` beyond the age window (default 14 days) or the total
  budget (default 100 MB, newest kept first) — a busy day of agent sessions can outgrow
  any age window. These are platform-written telemetry, the same class as the remote dead
  tickets below, so `--apply` removes them instead of parking them in trash.
* trash (`--purge-trash`): empties `PARHELION_HOME/trash/`. This is the only operation
  that deletes data and it is opt-in, after the operator reviewed `trash/`.
* remote (`--remote`): admission tickets in `/tmp/opsb-npu-tickets/*.json` on the device
  host whose `pid` no longer exists. `--apply` deletes only those ticket files, never a
  lock file.

Without `--apply` the command only reports. The integrator registers it in `cli_app.py`
via `register_doctor_subcommands(sub)`.
"""
from __future__ import annotations

import argparse
import datetime
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ..config import ConfigRepository, workspace_id
from ..device_env import parhelion_home

REMOTE_TICKET_DIR = "/tmp/opsb-npu-tickets"
REMOTE_TIMEOUT_SEC = 30

#: Default retention for driver session transcripts under `PARHELION_HOME/logs/sessions`.
DEFAULT_LOG_KEEP_DAYS = 14
#: Total budget for those transcripts, newest first; a busy day can outgrow any age window.
DEFAULT_LOG_MAX_MB = 100

#: `AgentBusStore.cleanup_stopped` treats these statuses as finished when no pid remains.
_AGENT_SQL = "SELECT agent_id, status, pid, pgid FROM agents"
_AGENT_FINISHED = ("stopped", "error")

_REMOTE_LIST = r'''
import glob, json, os
hits = []
for path in sorted(glob.glob(TICKET_DIR + "/*.json")):
    try:
        with open(path) as fh:
            pid = int(json.load(fh)["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        continue
    if pid <= 0:
        continue
    try:
        os.kill(pid, 0)
        continue
    except ProcessLookupError:
        pass
    except OSError:
        continue
    hits.append({"path": path, "pid": pid, "applied": False})
'''

_REMOTE_APPLY = r'''
for hit in hits:
    try:
        os.unlink(hit["path"])
        hit["applied"] = True
    except FileNotFoundError:
        pass
    except OSError as exc:
        hit["error"] = str(exc)
'''

_REMOTE_EMIT = r'''
print("OPSB_DOCTOR " + json.dumps(hits))
'''


def remote_ticket_script(apply: bool, ticket_dir: str = REMOTE_TICKET_DIR) -> str:
    """Python run on the device host; the delete fragment is only present when `apply`."""
    head = f"TICKET_DIR = {json.dumps(ticket_dir)}\n"
    return head + _REMOTE_LIST + (_REMOTE_APPLY if apply else "") + _REMOTE_EMIT


def _utc_stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _registered(cfg: dict[str, Any]) -> tuple[set[str], set[str]]:
    """Return (data-dir names, resolved workspace paths) of registered workspaces."""
    raw = [str(w) for w in (cfg.get("workspaces") or []) if str(w).strip()]
    active = str(cfg.get("active_workspace") or "").strip()
    if active:
        raw.append(active)
    names: set[str] = set()
    paths: set[str] = set()
    for item in raw:
        try:
            resolved = Path(item).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        paths.add(str(resolved))
        names.add(workspace_id(resolved))
    return names, paths


def _meta_workspace(directory: Path) -> str | None:
    try:
        meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = meta.get("workspace") if isinstance(meta, dict) else None
    return str(value) if isinstance(value, str) and value.strip() else None


def _pollution_reason(directory: Path) -> str | None:
    name = directory.name
    if name.startswith("bus.db-"):
        return "bus.db test residue"
    ws = _meta_workspace(directory)
    if ws and "/pytest-of-" in ws:
        return "pytest workspace"
    if name.startswith("ws-") and ws and not Path(ws).exists():
        return "workspace path missing"
    if not (directory / "meta.json").is_file():
        # Every data directory created for a real workspace records its path.
        return "no workspace metadata"
    if ws and ".worktrees" in Path(ws).parts and not any(
            p.is_file() and p.name != "meta.json" for p in directory.rglob("*")):
        # 3.x keyed state by worker worktree paths; empty leftovers carry no data.
        return "empty worktree state"
    return None


def scan_local(home: Path, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    root = home / "workspaces"
    if not root.is_dir():
        return []
    reg_names, reg_paths = _registered(cfg)
    hits: list[dict[str, Any]] = []
    for directory in sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink()):
        if directory.name in reg_names:
            continue
        ws = _meta_workspace(directory)
        if ws:
            try:
                if str(Path(ws).expanduser().resolve()) in reg_paths:
                    continue
            except (OSError, RuntimeError):
                pass
        reason = _pollution_reason(directory)
        if reason:
            hits.append({"path": str(directory), "name": directory.name, "reason": reason,
                         "workspace": ws, "applied": False})
    return hits


def apply_local(home: Path, hits: list[dict[str, Any]]) -> Path | None:
    """Move every hit into `home/trash/<UTC stamp>/`; return that trash directory."""
    if not hits:
        return None
    trash = home / "trash" / _utc_stamp()
    trash.mkdir(parents=True, exist_ok=True)
    for hit in hits:
        src = Path(hit["path"])
        dst = trash / src.name
        try:
            shutil.move(str(src), str(dst))
            hit["applied"] = True
            hit["moved_to"] = str(dst)
        except OSError as exc:
            hit["error"] = str(exc)
    return trash


def _agent_rows(db_path: Path) -> dict[str, dict[str, Any]] | None:
    """`agent_id -> {status, pid, pgid}`; `None` when the store cannot be read."""
    if not db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(_AGENT_SQL).fetchall()
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return None
    return {str(r[0]): {"status": str(r[1] or ""), "pid": r[2], "pgid": r[3]} for r in rows}


def _finished(record: dict[str, Any] | None) -> bool:
    if not record:
        return False
    return (record["status"] in _AGENT_FINISHED
            and record["pid"] is None and record["pgid"] is None)


def scan_agents(home: Path, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-agent sandboxes that a finished (or forgotten) agent left behind."""
    root = home / "workspaces"
    if not root.is_dir():
        return []
    reg_names, _ = _registered(cfg)
    hits: list[dict[str, Any]] = []
    for data_dir in sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink()):
        if data_dir.name in reg_names:
            # A registered workspace's own state stays untouched; its agents/ sandboxes are
            # judged individually below, exactly like any other data directory.
            pass
        agents_dir = data_dir / "agents"
        if not agents_dir.is_dir():
            continue
        rows = _agent_rows(data_dir / "state" / "agent_bus.db")
        for sandbox in sorted(p for p in agents_dir.iterdir() if p.is_dir() and not p.is_symlink()):
            record = rows.get(sandbox.name) if rows is not None else None
            if rows is None:
                hits.append({"path": str(sandbox), "agent_id": sandbox.name,
                             "workspace_dir": data_dir.name, "status": "",
                             "reason": "agent store unreadable", "reclaimable": False,
                             "applied": False})
                continue
            if _finished(record):
                reason, reclaimable = f"agent {record['status']}", True
            elif record is None:
                reason, reclaimable = "no agent record", True
            else:
                continue
            hits.append({"path": str(sandbox), "agent_id": sandbox.name,
                         "workspace_dir": data_dir.name,
                         "status": (record or {}).get("status", ""),
                         "reason": reason, "reclaimable": reclaimable, "applied": False})
    return hits


def apply_agents(home: Path, hits: list[dict[str, Any]], trash: Path | None) -> Path | None:
    """Move reclaimable sandboxes into `trash/<UTC stamp>/agents/<workspace dir>/`."""
    targets = [h for h in hits if h.get("reclaimable")]
    if not targets:
        return trash
    if trash is None:
        trash = home / "trash" / _utc_stamp()
    for hit in targets:
        src = Path(hit["path"])
        dst = trash / "agents" / hit["workspace_dir"] / src.name
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            hit["applied"] = True
            hit["moved_to"] = str(dst)
        except OSError as exc:
            hit["error"] = str(exc)
    return trash


def scan_logs(home: Path, keep_days: int, max_mb: int = DEFAULT_LOG_MAX_MB) -> list[dict[str, Any]]:
    """Driver session transcripts beyond the age window or the total size budget."""
    root = home / "logs" / "sessions"
    if not root.is_dir():
        return []
    now = time.time()
    cutoff = now - max(0, keep_days) * 86400
    entries: list[tuple[Path, float, int]] = []
    for path in sorted(p for p in root.iterdir() if p.is_file() and not p.is_symlink()):
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append((path, stat.st_mtime, stat.st_size))

    expired: dict[Path, str] = {p: f"older than {keep_days}d" for p, mtime, _ in entries if mtime <= cutoff}
    if max_mb > 0:
        budget = max_mb * 1024 * 1024
        used = 0
        for path, _, size in sorted((e for e in entries if e[0] not in expired),
                                    key=lambda e: e[1], reverse=True):
            used += size
            if used > budget:
                expired[path] = f"over {max_mb} MB budget"

    hits: list[dict[str, Any]] = []
    for path, mtime, size in entries:
        reason = expired.get(path)
        if not reason:
            continue
        hits.append({"path": str(path), "name": path.name, "reason": reason,
                     "age_days": round((now - mtime) / 86400.0, 1),
                     "size_bytes": size, "applied": False})
    return hits


def apply_logs(hits: list[dict[str, Any]]) -> None:
    """Remove expired transcripts; they are regenerable platform telemetry."""
    for hit in hits:
        try:
            Path(hit["path"]).unlink()
            hit["applied"] = True
        except FileNotFoundError:
            pass
        except OSError as exc:
            hit["error"] = str(exc)


def scan_trash(home: Path) -> list[dict[str, Any]]:
    root = home / "trash"
    if not root.is_dir():
        return []
    hits: list[dict[str, Any]] = []
    for entry in sorted(p for p in root.iterdir() if not p.is_symlink()):
        size = 0
        if entry.is_dir():
            size = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
        elif entry.is_file():
            size = entry.stat().st_size
        hits.append({"path": str(entry), "name": entry.name, "size_bytes": size,
                     "applied": False})
    return hits


def apply_trash(hits: list[dict[str, Any]]) -> None:
    for hit in hits:
        try:
            path = Path(hit["path"])
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
            hit["applied"] = True
        except OSError as exc:
            hit["error"] = str(exc)


def remote_command(host: str) -> list[str]:
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, "python3", "-"]


def scan_remote(host: str, apply: bool) -> tuple[list[dict[str, Any]], str | None]:
    """Run the ticket script on `host`; return (hits, error)."""
    try:
        proc = subprocess.run(
            remote_command(host), input=remote_ticket_script(apply), capture_output=True,
            text=True, timeout=REMOTE_TIMEOUT_SEC,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], f"ssh {host}: {exc}"
    for line in reversed((proc.stdout or "").splitlines()):
        if line.startswith("OPSB_DOCTOR "):
            try:
                rows = json.loads(line[len("OPSB_DOCTOR "):])
            except ValueError:
                break
            hits = [dict(r, host=host) for r in rows if isinstance(r, dict)]
            return hits, None if proc.returncode == 0 else f"ssh {host}: exit {proc.returncode}"
    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
    return [], f"ssh {host}: exit {proc.returncode}" + (f": {detail[-1][:200]}" if detail else "")


def _print_table(report: dict[str, Any]) -> None:
    mode = "applied" if report["applied"] else "dry-run (pass --apply to act)"
    print(f"opsb doctor clean — {mode}")
    print(f"\nLocal workspaces ({len(report['local'])}):")
    if not report["local"]:
        print("  (none)")
    for hit in report["local"]:
        state = "moved" if hit.get("applied") else ("error: " + hit["error"] if hit.get("error") else "would move")
        print(f"  {hit['name']:<40} {hit['reason']:<24} {state}")
    if "agents" in report:
        print(f"\nAgent sandboxes ({len(report['agents'])}):")
        if not report["agents"]:
            print("  (none)")
        for hit in report["agents"]:
            state = ("moved" if hit.get("applied")
                     else ("error: " + hit["error"] if hit.get("error")
                           else ("would move" if hit.get("reclaimable") else "kept")))
            print(f"  {hit['workspace_dir']}/{hit['agent_id']:<24} {hit['reason']:<24} {state}")
    if "logs" in report:
        total = sum(h.get("size_bytes") or 0 for h in report["logs"])
        print(f"\nSession logs ({len(report['logs'])}, {total / 1048576:.1f} MB):")
        if not report["logs"]:
            print("  (none)")
        for hit in report["logs"]:
            state = "removed" if hit.get("applied") else ("error: " + hit["error"] if hit.get("error") else "would remove")
            print(f"  {hit['name']:<40} {hit['reason']:<22} {hit['age_days']:>6}d  {state}")
    if "trash" in report:
        print(f"  trash: {report['trash']}")
    if "trash_purge" in report:
        total = sum(h.get("size_bytes") or 0 for h in report["trash_purge"])
        print(f"\nTrash ({len(report['trash_purge'])} batches, {total / 1048576:.1f} MB):")
        if not report["trash_purge"]:
            print("  (none)")
        for hit in report["trash_purge"]:
            state = "deleted" if hit.get("applied") else ("error: " + hit["error"] if hit.get("error") else "would delete")
            print(f"  {hit['name']:<40} {hit['size_bytes'] / 1048576:>8.1f} MB  {state}")
    if report.get("remote_checked"):
        print(f"\nRemote tickets ({len(report['remote'])}):")
        if report.get("remote_error"):
            print(f"  error: {report['remote_error']}")
        elif not report["remote"]:
            print("  (none)")
        for hit in report["remote"]:
            state = "deleted" if hit.get("applied") else ("error: " + hit["error"] if hit.get("error") else "would delete")
            print(f"  {hit['path']:<52} pid={hit['pid']:<8} {state}")


def run_doctor(args: argparse.Namespace) -> int:
    action = getattr(args, "doctor_action", None)
    if action != "clean":
        print("usage: opsb doctor clean [--apply] [--remote] [--agents] [--logs DAYS] "
              "[--purge-trash] [--json]", file=sys.stderr)
        return 2
    apply = bool(getattr(args, "apply", False))
    home = parhelion_home()
    cfg = ConfigRepository.load()
    local = scan_local(home, cfg)
    trash = apply_local(home, local) if apply else None
    report: dict[str, Any] = {"local": local, "remote": [], "applied": apply}
    rc = 0
    if getattr(args, "agents", False):
        agents = scan_agents(home, cfg)
        if apply:
            trash = apply_agents(home, agents, trash)
        report["agents"] = agents
    if getattr(args, "logs", None) is not None:
        logs = scan_logs(home, int(args.logs), int(getattr(args, "logs_max_mb", DEFAULT_LOG_MAX_MB)))
        if apply:
            apply_logs(logs)
        report["logs"] = logs
    if getattr(args, "purge_trash", False):
        batches = scan_trash(home)
        if apply:
            apply_trash(batches)
        report["trash_purge"] = batches
    if trash is not None:
        report["trash"] = str(trash)
    if getattr(args, "remote", False):
        host = str(cfg.get("npu_ssh_host") or "npu")
        remote, error = scan_remote(host, apply)
        report["remote"] = remote
        report["remote_checked"] = True
        if error:
            report["remote_error"] = error
            rc = 1
    for key in ("local", "agents", "logs", "trash_purge", "remote"):
        if any(h.get("error") for h in report.get(key) or []):
            rc = 1
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_table(report)
    return rc


def register_doctor_subcommands(sub: argparse._SubParsersAction[Any]) -> None:
    doctor = sub.add_parser("doctor", help="Diagnose and clean Parhelion state")
    doctor.set_defaults(func=run_doctor, doctor_action=None)
    d_sub = doctor.add_subparsers(dest="doctor_action")
    clean = d_sub.add_parser(
        "clean",
        help="List (or with --apply, move to trash / prune) leftover test, agent and log state",
    )
    clean.add_argument("--apply", action="store_true", help="Act instead of listing; local hits move to trash")
    clean.add_argument("--remote", action="store_true", help="Also check dead tickets on the device host")
    clean.add_argument("--agents", action="store_true",
                       help="Also reclaim per-agent sandboxes of finished agents")
    clean.add_argument("--logs", type=int, nargs="?", const=DEFAULT_LOG_KEEP_DAYS,
                       metavar="DAYS",
                       help=f"Also prune driver session logs older than DAYS (default {DEFAULT_LOG_KEEP_DAYS})")
    clean.add_argument("--logs-max-mb", type=int, default=DEFAULT_LOG_MAX_MB, metavar="MB",
                       help=f"Total budget for session logs, newest kept first (default {DEFAULT_LOG_MAX_MB}, 0 disables)")
    clean.add_argument("--purge-trash", action="store_true",
                       help="Also empty PARHELION_HOME/trash (destructive; --apply only)")
    clean.add_argument("--json", action="store_true")
    clean.set_defaults(func=run_doctor)
