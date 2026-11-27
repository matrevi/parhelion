"""Unified Driver Registry, session launch/observation orchestration, and detached worker entrypoint."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .antigravity_driver import AntigravityDriver
from .base import (
    ORCHESTRATOR_PROBLEM,
    BaseDriver,
    DriverDiagnostic,
    SessionRecord,
    load_parhelion_config,
    save_parhelion_config,
)
from .brief import (
    MAX_BRIEF_BYTES,
    compose_contest_subagent_brief,
    compose_session_brief,
    prepare_session_worktree,
    resolve_problem_repo,
)
from .claude_driver import ClaudeDriver
from .codex_driver import CodexDriver
from .ext_drivers import DeepSeekDriver, GeminiDriver
from .pi_driver import PiDriver
from .session_store import list_sessions, update_session

DRIVER_CLASSES: dict[str, type[BaseDriver]] = {
    "antigravity": AntigravityDriver,
    "codex": CodexDriver,
    "claude": ClaudeDriver,
    "pi": PiDriver,
    "gemini": GeminiDriver,
    "deepseek": DeepSeekDriver,
}

VALID_DRIVERS: tuple[str, ...] = tuple(DRIVER_CLASSES.keys())
DEFAULT_ACTIVE_DRIVER = "antigravity"
WORKER_MODELS = {"deepseek": "DeepSeek V4.1 Flash", "codex": "gpt-6.2-astra", "antigravity": "Neon High"}


def worker_model(kind: str) -> str | None:
    if kind not in WORKER_MODELS:
        return None
    return (load_parhelion_config().get("worker_models") or {}).get(kind) or WORKER_MODELS[kind]


def select_worker_model(kind: str, model: str) -> dict[str, Any]:
    model = model.strip()
    if kind not in WORKER_MODELS or not model or len(model) > 200 or not model.isprintable():
        raise ValueError("Invalid worker driver or model")
    cfg = load_parhelion_config()
    cfg["worker_models"] = {**(cfg.get("worker_models") or {}), kind: model}
    save_parhelion_config(cfg)
    return {"ok": True, "driver": kind, "worker_model": model}


def get_active_driver_kind(cfg: dict[str, Any] | None = None) -> str:
    loaded = cfg if cfg is not None else load_parhelion_config()
    raw = str(loaded.get("active_driver") or DEFAULT_ACTIVE_DRIVER).strip().lower()
    return raw if raw in DRIVER_CLASSES else DEFAULT_ACTIVE_DRIVER


def get_driver(kind: str | None = None) -> BaseDriver:
    resolved = (kind or get_active_driver_kind()).strip().lower()
    cls = DRIVER_CLASSES.get(resolved)
    if cls is None:
        raise ValueError(f"Unknown driver '{kind}'. Supported: {', '.join(VALID_DRIVERS)}")
    return cls()


def list_drivers() -> dict[str, Any]:
    active = get_active_driver_kind()
    items = [
        {"kind": cls.kind, "display_name": cls.display_name, "protocol": cls.protocol, "active": k == active}
        for k, cls in DRIVER_CLASSES.items()
    ]
    return {"active_driver": active, "drivers": items}


def select_active_driver(kind: str) -> dict[str, Any]:
    norm = kind.strip().lower()
    if norm not in DRIVER_CLASSES:
        raise ValueError(f"Unknown driver '{kind}'. Supported: {', '.join(VALID_DRIVERS)}")
    cfg = load_parhelion_config()
    cfg["active_driver"] = norm
    save_parhelion_config(cfg)
    return {"ok": True, "active_driver": norm}


def doctor_drivers(kind: str | None = None) -> dict[str, Any]:
    from .availability import available
    active = get_active_driver_kind()
    ticked = available()
    targets = [kind.strip().lower()] if kind else list(VALID_DRIVERS)
    diagnostics: list[dict[str, Any]] = []
    for k in targets:
        diag: DriverDiagnostic = get_driver(k).discover()
        row = diag.to_dict()
        row["active"] = (k == active)
        row["worker_model"] = worker_model(k)
        row["available"] = k in ticked  # 4.4 P34: ticked for workers and scouts in Settings > Drivers
        diagnostics.append(row)
    return {"active_driver": active, "diagnostics": diagnostics, "available_drivers": ticked}


def sync_driver_workspace(
    workspace: Path,
    driver_kind: str | None = None,
    mcp_url: str | None = None,
) -> dict[str, Any]:
    """Sync MCP configs, skills, and project registration for the active or specified driver."""
    drv = get_driver(driver_kind)
    return drv.sync_workspace(workspace.expanduser().resolve(), mcp_url=mcp_url)


def launch_problem_session(
    workspace: Path,
    problem: str,
    *,
    driver_kind: str | None = None,
    mode: str = "background",
    track_name: str = "",
    goal: str = "",
    resume_id: str | None = None,
    use_main: bool = False,
    dry_run: bool = False,
    preseed: bool = False,
) -> dict[str, Any]:
    ws = workspace.expanduser().resolve()
    drv = get_driver(driver_kind)
    prob_key = str(problem or "").strip()
    if prob_key.upper() in ("ALL", ORCHESTRATOR_PROBLEM, "ALL_OPS", "CONTEST", "__ALL__"):
        title = ORCHESTRATOR_PROBLEM
        worktree = ws
        brief = compose_contest_subagent_brief(ws, goal=goal)
    else:
        title, _, _ = resolve_problem_repo(ws, prob_key)
        worktree = prepare_session_worktree(ws, title, track_name=track_name, use_main=use_main)
        brief = compose_session_brief(ws, title, goal=goal, worktree=worktree,
                                      branch=track_name or worktree.name, include_startup_pack=True,
                                      driver=drv.kind)
    record: SessionRecord = drv.launch_session(
        workspace=ws, problem=title, worktree=worktree, brief=brief,
        mode=mode, resume_id=resume_id, dry_run=dry_run, preseed_thread=preseed,
    )
    meta = dict(drv.last_launch_meta)
    return {
        "ok": True,
        "session": record.to_dict(),
        "mode": meta.get("mode", mode),
        "launch_mode": meta.get("launch_mode", record.launch_mode),
        "dry_run": bool(meta.get("dry_run", dry_run or mode == "dry-run")),
        "command": meta.get("command", []),
        "env_overrides": meta.get("env", {}),
        "mcp_config_path": meta.get("mcp_config_path"),
        "log_path": meta.get("log_path"),
        "worktree": str(worktree),
        "deep_link": meta.get("deep_link", record.deep_link),
        "project_id": meta.get("project_id", record.project_id),
        "brief_path": meta.get("brief_path", record.brief_path),
        "clipboard_copied": bool(meta.get("clipboard_copied", False)),
        "dsh_seed": meta.get("dsh_seed"),
        "dsh_workspace": meta.get("dsh_workspace"),
        "brief": brief,
    }


launch_driver_session = launch_problem_session


def list_all_sessions(
    workspace: Path,
    *,
    problem: str | None = None,
    driver: str | None = None,
    status: str | None = None,
    include_gui: bool = True,
    include_drafts: bool = False,
    limit: int = 40,
) -> list[dict[str, Any]]:
    """Merge persisted Parhelion sessions with live read-only GUI observer sessions."""
    ws = workspace.expanduser().resolve()
    tracked = list_sessions(ws, problem=problem, driver=driver, status=status)
    merged: list[dict[str, Any]] = []
    by_native: dict[tuple[str, str], dict[str, Any]] = {}

    for rec in tracked:
        is_unsent_gui_draft = rec.status == "draft" or (
            rec.launch_mode in ("gui", "gui-preseed") and rec.pid is None and rec.observed_steps == 0 and rec.status == "running"
        )
        if not include_drafts and is_unsent_gui_draft and (status or "").lower() != "draft":
            continue
        item = {**rec.to_dict(), "source": "parhelion"}
        merged.append(item)
        if rec.native_session_id:
            by_native[(rec.driver.lower(), rec.native_session_id)] = item

    if include_gui:
        target_kinds = [driver.strip().lower()] if driver else list(VALID_DRIVERS)
        for kind in target_kinds:
            try:
                gui_rows = get_driver(kind).observe_gui_sessions(workspace=ws, limit=limit)
            except Exception:
                gui_rows = []
            for g in gui_rows:
                if problem and str(g.get("problem") or "").lower() != problem.lower():
                    continue
                if status and str(g.get("status") or "").lower() != status.lower():
                    continue
                key = (str(g.get("driver") or kind).lower(), str(g.get("native_session_id") or ""))
                existing = by_native.get(key) if key[1] else None
                if existing is not None:
                    existing["status"] = g.get("status") or existing["status"]
                    existing["observed_steps"] = max(int(existing.get("observed_steps") or 0), int(g.get("observed_steps") or 0))
                    existing["tokens_used"] = max(int(existing.get("tokens_used") or 0), int(g.get("tokens_used") or 0))
                    if g.get("updated_at") and str(g["updated_at"]).replace(" ", "T", 1) > str(existing.get("updated_at") or "").replace(" ", "T", 1):
                        existing["updated_at"] = g["updated_at"]
                    if g.get("deep_link") and not existing.get("deep_link"):
                        existing["deep_link"] = g["deep_link"]
                    existing["gui_live"] = True
                else:
                    merged.append(g)

    def _sort_ts(row: dict[str, Any]) -> str:
        return str(row.get("updated_at") or row.get("created_at") or "").strip().replace(" ", "T", 1)

    merged.sort(key=_sort_ts, reverse=True)
    cap = max(1, int(limit))
    if driver is None and len(merged) > cap:
        present_drivers = sorted({str(r.get("driver") or "").lower() for r in merged})
        if len(present_drivers) > 1:
            per_driver_quota = max(4, min(10, cap // len(present_drivers)))
            selected: list[dict[str, Any]] = []
            selected_idx: set[int] = set()
            counts: dict[str, int] = {}
            for idx, item in enumerate(merged):
                dk = str(item.get("driver") or "").lower()
                if counts.get(dk, 0) < per_driver_quota:
                    selected.append(item)
                    selected_idx.add(idx)
                    counts[dk] = counts.get(dk, 0) + 1
            for idx, item in enumerate(merged):
                if len(selected) >= cap:
                    break
                if idx not in selected_idx:
                    selected.append(item)
                    selected_idx.add(idx)
            selected.sort(key=_sort_ts, reverse=True)
            return selected[:cap]
    return merged[:cap]


def _live_seat_count(ws: Path) -> int:
    """Sprint seats that are not stopped; a seat waiting for a follow-up after a restart counts."""
    from ..agents.bus_store import AgentBusStore

    try:
        rows = AgentBusStore(ws).list_agents(limit=-1)
    except Exception:
        return 0
    return sum(1 for r in rows if r.get("role_label") != "orchestrator" and r.get("status") != "stopped")


def _run_worker_job(job_path: Path) -> int:
    job = json.loads(job_path.read_text(encoding="utf-8"))
    drv = get_driver(job["driver"])
    ws, wt = Path(job["workspace"]), Path(job["worktree"])
    session_id, native_id, resume_id = str(job["session_id"]), str(job["native_session_id"]), job.get("resume_id")
    cmd = [str(x) for x in job["cmd"]]
    env = {str(k): str(v) for k, v in (job.get("env") or {}).items()}
    log_path = Path(job["log_path"])
    if job.get("problem") == ORCHESTRATOR_PROBLEM:
        # The contest orchestrator keeps its turn going while the sprint has seats (ACP drivers).
        drv.live_seats = lambda: _live_seat_count(ws)

    try:
        rc = drv._execute_headless_turn(
            workspace=ws, worktree=wt, brief=str(job["brief"]), session_id=session_id,
            native_session_id=native_id, resume_id=resume_id, cmd=cmd, env=env, log_path=log_path,
        )
    except Exception as exc:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as lf:
            lf.write(json.dumps({"event": "worker_error", "error": str(exc)}) + "\n")
        update_session(ws, session_id, status="failed", clear_pid=True)
        return 1

    update_session(ws, session_id, status=("completed" if rc == 0 else "failed"), clear_pid=True)
    return int(rc)


def _main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="opsb_core.drivers.registry")
    p.add_argument("--exec-job", required=True, help="Path to detached driver job spec JSON")
    args = p.parse_args(argv)
    return _run_worker_job(Path(args.exec_job))


if __name__ == "__main__":
    raise SystemExit(_main())
