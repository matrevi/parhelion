"""Dispatch one independent worker per contest problem and optimization direction."""
from __future__ import annotations

from pathlib import Path
from subprocess import SubprocessError

from ..config import ConfigRepository
from ..drivers.registry import get_active_driver_kind
from ..services.contest_service import resolve_workspace_meta


DIRECTIONS = {
    "algorithm": "Explore algorithmic and numerical changes. Compare against the passing baseline.",
    "dataflow": "Explore tiling, memory traffic and kernel launch costs on measured buckets.",
    "calibration": "Use official receipts to improve this problem's local bench and cases. Compare baseline and candidate using the same experimental profile. Share reproduced gaps, run IDs and coverage changes with your squad.",
}


def start_sprint(manager, workspace: Path, *, driver: str = "", preview: bool = False,
                 enable_auto_submit: bool = False) -> dict:
    ws = workspace.expanduser().resolve()
    driver = driver or get_active_driver_kind()
    problems = resolve_workspace_meta(ws)["problems"]
    if not problems:
        raise ValueError("No problems discovered in the selected contest")
    diagnostics = manager._cached_doctor()["diagnostics"]
    ready = any(d.get("kind") == driver and d.get("installed") and d.get("binary_path")
                and d.get("auth_ok") for d in diagnostics)
    from .manager import claude_workers_allowed
    ready = ready and (driver != "claude" or claude_workers_allowed())
    assignments = []
    # All write calls run on the daemon. A replay after a partial launch reuses live slots.
    with manager.sprint_lock:
        config = ConfigRepository.load()
        active_workspace = config.get("active_workspace")
        selected = bool(active_workspace and Path(active_workspace).expanduser().resolve() == ws)
        if enable_auto_submit and not selected:
            raise ValueError("Select this contest in Parhelion before enabling automatic submission")
        workers = [] if preview else manager._store(ws).list_agents(include_stopped=False, limit=-1)
        occupied = {(w["problem"], w["branch"]): w for w in workers}
        for problem in problems:
            title = problem["title"]
            repo = (ws / (problem.get("path") or title)).resolve()
            for axis, task in DIRECTIONS.items():
                track = "sprint-" + axis
                slot = dict(problem=title, axis=axis, branch=track)
                assignments.append(slot)
                existing = occupied.get((title, track))
                if existing:
                    slot.update(state="existing", agent_id=existing["agent_id"])
                elif not ready:
                    slot.update(state="blocked", reason="Harness CLI, authentication or worker permission unavailable")
                elif not repo.is_relative_to(ws) or not (repo / ".git").exists():
                    slot.update(state="blocked", reason="Problem repository unavailable")
                elif preview:
                    slot.update(state="planned")
                else:
                    try:
                        result = manager.spawn_agent(ws, driver=driver, problem=title, axis=axis,
                            role_label="bench-author" if axis == "calibration" else "worker",
                            task=task + " Run device work through parhelion_test_plan.", track=track,
                            collab_mode="orthogonal", _sprint=True)
                        agent = result["agent"]
                        occupied[(title, track)] = agent
                        slot.update(state="started", agent_id=agent["agent_id"])
                    except (OSError, ValueError, RuntimeError, SubprocessError) as exc:
                        slot.update(state="blocked", reason=str(exc))
        if enable_auto_submit and not preview and any(s["state"] in ("existing", "started") for s in assignments):
            config = ConfigRepository.load()
            if str(ws) != str(Path(config.get("active_workspace") or ".").expanduser().resolve()):
                raise ValueError("Active contest changed during startup; automatic submission was not enabled")
            config["scheduler"]["auto_consumer_enabled"] = True
            ConfigRepository.save(config)
    return {"ok": all(s["state"] != "blocked" for s in assignments), "driver": driver,
            "auto_submit_enabled": selected and bool(config["scheduler"]["auto_consumer_enabled"]),
            "preview": preview, "problems": len(problems), "assignments": assignments}
