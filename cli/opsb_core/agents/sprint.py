"""Dispatch one independent worker per contest problem and optimization direction."""
from __future__ import annotations

from pathlib import Path
from subprocess import SubprocessError

from ..config import ConfigRepository
from ..drivers.registry import get_active_driver_kind
from ..services.contest_service import resolve_workspace_meta


SOLO_TASK = ("Own this problem end to end. Pick one hypothesis from the state card, change one variable, "
             "run parhelion_test level=standard, compare against the baseline in the digest, and hand a candidate "
             "to parhelion_submit_gate only when the digest shows a measured gain. Record negative results with "
             "parhelion_agent_report.")

DIRECTIONS = {
    "solo": SOLO_TASK,
    "algorithm": "Explore algorithmic and numerical changes. Compare against the passing baseline.",
    "dataflow": "Explore tiling, memory traffic and kernel launch costs on measured buckets.",
    "calibration": "Calibrate on a new candidate or official receipt. Reuse comparable baseline evidence and measure only the cases needed to test your hypothesis. Share the run IDs and findings, then wait for new evidence.",
}

# Seats per sprint mode; solo is the default (one worker per problem).
MODES = {
    "solo": ("solo",),
    "dual": ("algorithm", "dataflow"),
    "full": ("algorithm", "dataflow", "calibration"),
}
DEVICE_HINT = " Device work: parhelion_test, then parhelion_test_wait."


def seat_role(axis: str) -> str:
    return "bench-author" if axis == "calibration" else "worker"


def sprint_seats(workers):
    seats = {}
    for worker in sorted(workers, key=lambda w: (w["status"] != "stopped", w["created_at"]), reverse=True):
        path = Path(worker["worktree"])
        if path.parent.name == ".worktrees" and path.name in {"sprint-" + axis for axis in DIRECTIONS}:
            seats.setdefault((worker["problem"], path.name), worker)
    return seats


def start_sprint(manager, workspace: Path, *, driver: str = "", preview: bool = False,
                 enable_auto_submit: bool = False, mode: str = "solo") -> dict:
    ws = workspace.expanduser().resolve()
    mode = str(mode or "solo").strip().lower()
    if mode not in MODES:
        raise ValueError(f"Unknown sprint mode '{mode}'. Choose one of: {', '.join(MODES)}")
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
        occupied = sprint_seats(manager._store(ws).list_agents(limit=-1))
        # An explicit sprint start is the administrator confirming the provider has recovered.
        suspended = manager.driver_suspended(ws, driver)
        if suspended and not preview:
            manager.clear_driver_suspension(ws, driver)
            suspended = False
        for problem in problems:
            title = problem["title"]
            repo = (ws / (problem.get("path") or title)).resolve()
            for axis in MODES[mode]:
                task = DIRECTIONS[axis]
                track = "sprint-" + axis
                slot = dict(problem=title, axis=axis, branch=track)
                assignments.append(slot)
                existing = occupied.get((title, track))
                if existing and existing["status"] in ("stopped", "error"):
                    slot.update(state="blocked", agent_id=existing["agent_id"],
                                reason=existing.get("error") or "Worker explicitly stopped")
                elif existing:
                    slot.update(state="existing", agent_id=existing["agent_id"])
                elif suspended:
                    slot.update(state="blocked", reason="driver suspended")
                elif not ready:
                    slot.update(state="blocked", reason="Harness CLI, authentication or worker permission unavailable")
                elif not repo.is_relative_to(ws) or not (repo / ".git").exists():
                    slot.update(state="blocked", reason="Problem repository unavailable")
                elif preview:
                    slot.update(state="planned")
                else:
                    try:
                        result = manager.spawn_agent(ws, driver=driver, problem=title, axis=axis,
                            role_label=seat_role(axis), task=task + DEVICE_HINT, track=track,
                            collab_mode="solo" if mode == "solo" else "orthogonal", _sprint=True)
                        if result.get("blocked"):
                            slot.update(state="blocked", reason=result.get("reason") or "driver suspended")
                            continue
                        agent = result["agent"]
                        occupied[(title, track)] = agent
                        slot.update(state="blocked" if agent["status"] == "error" else
                                    "existing" if result.get("reused") else "started", agent_id=agent["agent_id"])
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
            "preview": preview, "mode": mode, "problems": len(problems), "assignments": assignments}
