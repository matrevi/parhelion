"""CLI command: `opsb doctor clean [--apply] [--remote] [--json]`.

Finds state left behind by test runs and dead device tickets:

* local: `PARHELION_HOME/workspaces/*` directories that are test pollution — names
  starting with `bus.db-`, a `meta.json` whose `workspace` lies under `/pytest-of-`, or a
  `ws-*` directory whose recorded workspace no longer exists. Directories belonging to a
  workspace registered in `config.json` (`workspaces`, `active_workspace`) are never
  touched. `--apply` moves the hits to `PARHELION_HOME/trash/<UTC timestamp>/`; nothing
  is deleted.
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
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..config import ConfigRepository, workspace_id
from ..device_env import parhelion_home

REMOTE_TICKET_DIR = "/tmp/opsb-npu-tickets"
REMOTE_TIMEOUT_SEC = 30

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
    if report.get("trash"):
        print(f"  trash: {report['trash']}")
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
        print("usage: opsb doctor clean [--apply] [--remote] [--json]", file=sys.stderr)
        return 2
    apply = bool(getattr(args, "apply", False))
    home = parhelion_home()
    cfg = ConfigRepository.load()
    local = scan_local(home, cfg)
    trash = apply_local(home, local) if apply else None
    report: dict[str, Any] = {"local": local, "remote": [], "applied": apply}
    if trash is not None:
        report["trash"] = str(trash)
    rc = 0
    if getattr(args, "remote", False):
        host = str(cfg.get("npu_ssh_host") or "npu")
        remote, error = scan_remote(host, apply)
        report["remote"] = remote
        report["remote_checked"] = True
        if error:
            report["remote_error"] = error
            rc = 1
    if any(h.get("error") for h in local):
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
    clean = d_sub.add_parser("clean", help="List (or with --apply, move to trash) test-polluted state")
    clean.add_argument("--apply", action="store_true", help="Act instead of listing; local hits move to trash")
    clean.add_argument("--remote", action="store_true", help="Also check dead tickets on the device host")
    clean.add_argument("--json", action="store_true")
    clean.set_defaults(func=run_doctor)
