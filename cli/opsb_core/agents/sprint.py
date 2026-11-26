"""Dispatch one independent worker per contest problem, seat and optimization direction.

Seat identity is ``(problem, seat name)``: the bus record carries the seat name and every seat owns
``.worktrees/sprint-<seat>`` and the branch of the same name. ``solo``/``dual``/``full`` keep the 4.2
layout; ``race`` opens N tracks on one goal and ``pipeline`` splits the work into recon, K
implementation seats, an integrator and a harness seat. An explicit sprint start is also where a
stagnant problem gets replaced: the old seats are asked for a handoff and a new seat starts in an
orthogonal direction, idempotently per stagnation event.
"""
from __future__ import annotations

import json
from pathlib import Path
from subprocess import SubprocessError
from typing import Any

from ..config import ConfigRepository
from ..drivers.registry import get_active_driver_kind
from ..services.contest_service import resolve_workspace_meta
from . import seat_plan
from .seat_plan import DEVICE_HINT, MODES, expand_seats, normalize_specs, orthogonal_direction, replace_handover
from .seat_plan import seat_of, sprint_seats, track_name

HANDOFF_DIRECTIVE = (
    "This problem has six paid attempts without a threshold gain, so the platform is replacing this seat. "
    "Write your handoff now, in at most 1500 characters, under exactly these five headings on their own lines: "
    "## Where I am / ## In the middle of / ## Uncommitted changes and paths / ## Next experiment / "
    "## Not yet in the ledger. Commit and push any work in progress, name real SHAs and paths, then stop at the "
    "next turn boundary. A replacement seat starts in the orthogonal '{direction}' direction and reads your "
    "handoff; do not start a new experiment after writing it."
)
REPLACE_TASK = ("Take over {title} from the replaced seat in the orthogonal '{direction}' direction. The two "
                "previous live seats were asked to hand off and stop; do not repeat the failed directions in "
                "your start-up handover. Change one variable, measure with parhelion_test, and hand a candidate "
                "to parhelion_submit_gate only when the digest shows a measured gain.")


def _ready(manager, driver: str) -> bool:
    diagnostics = manager._cached_doctor()["diagnostics"]
    ready = any(d.get("kind") == driver and d.get("installed") and d.get("binary_path")
                and d.get("auth_ok") for d in diagnostics)
    from .manager import claude_workers_allowed
    return ready and (driver != "claude" or claude_workers_allowed())


def _select(problems: list[dict[str, Any]], specs: list[dict[str, Any]], mode: str,
            role: str) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], list[dict[str, Any]]]:
    """``(bound specs in manifest order, unbound knowledge specs)``; unknown titles raise."""
    by_name = {p["title"].lower(): p for p in problems}
    unknown = [s["title"] for s in specs if s["title"] and s["title"].lower() not in by_name]
    if unknown:
        raise ValueError(f"Unknown problems {unknown}; choose from: {', '.join(p['title'] for p in problems)}")
    chosen: dict[str, dict[str, Any]] = {}
    unbound: list[dict[str, Any]] = []
    for spec in specs:
        if not spec["title"]:
            unbound.append({**spec, "role": spec.get("role") or "knowledge", "mode": mode})
            continue
        title = by_name[spec["title"].lower()]["title"]
        chosen[title.lower()] = {**spec, "title": title,
                                 "role": spec.get("role") or role or "worker"}
    return [(chosen[p["title"].lower()], p) for p in problems if p["title"].lower() in chosen], unbound


def _stop_seats(manager, ws: Path, names: list[str], *, preview: bool) -> list[dict[str, Any]]:
    store = manager._store(ws)
    workers = store.list_agents(limit=-1)
    out: list[dict[str, Any]] = []
    for raw in names:
        name = str(raw or "").strip()
        if not name:
            continue
        title, _, seat = name.rpartition("/")
        hits = [w for w in workers if seat_of(w) == (seat or name)
                and (not title or str(w["problem"]).lower() == title.lower())]
        if not hits:
            raise ValueError(f"Unknown seat {name!r}; name a seat as '<seat>' or '<title>/<seat>'")
        if not title and len({w["problem"] for w in hits}) > 1:
            raise ValueError(f"Seat {name!r} exists on several problems; name it as '<title>/<seat>'")
        for worker in hits:
            live = worker["status"] not in ("stopped", "error")
            slot = {"problem": worker["problem"], "seat": seat_of(worker), "agent_id": worker["agent_id"],
                    "state": "stopped" if live and not preview else "planned" if live else "already_stopped"}
            if live and not preview:
                manager.stop_agent(ws, worker["agent_id"])
            out.append(slot)
    return out


def _replace_seats(manager, ws: Path, titles: list[str], *, preview: bool, driver: str, ready: bool,
                   suspended: bool) -> list[dict[str, Any]]:
    """Ask the live seats of each title to hand off, then start one orthogonal replacement seat.

    The replacement is idempotent per stagnation event: the bus message that records it has the event
    in its key, so replaying the same receipts never replaces a problem twice while the new seat lives.
    """
    store = manager._store(ws)
    workers = store.list_agents(limit=-1)
    meta = {p["title"].lower(): p for p in resolve_workspace_meta(ws)["problems"]}
    out: list[dict[str, Any]] = []
    for raw in titles:
        title = str(raw or "").strip()
        if not title:
            continue
        problem = meta.get(title.lower())
        if problem is None:
            raise ValueError(f"Unknown problem {title!r}")
        title = problem["title"]
        live = [w for w in workers if str(w["problem"]).lower() == title.lower()
                and w["status"] not in ("stopped", "error")]
        trigger = str(store.latest_event_key(title, "stagnation") or "manual")
        base = f"seat-replace:{title.lower()}:{trigger}"
        round_no, prior = 1, None
        while True:
            key = f"{base}:{round_no}"
            row = store.message_by_event(key)
            if row is None:
                break
            prior = store.get_agent(str(row.get("agent_id") or ""))
            if prior is not None and prior["status"] not in ("stopped", "error"):
                break
            prior = None
            round_no += 1
        if prior is not None:
            out.append({"problem": title, "seat": seat_of(prior), "agent_id": prior["agent_id"],
                        "state": "existing", "handoff_to": [w["agent_id"] for w in live]})
            continue
        direction = orthogonal_direction({seat_of(w) or str(w.get("axis") or "") for w in live})
        seat = f"replace-{round_no}"
        slot = {"problem": title, "seat": seat, "state": "planned" if preview else "blocked",
                "handoff_to": [w["agent_id"] for w in live], "direction": direction, "trigger": trigger}
        if preview:
            out.append(slot)
            continue
        for worker in live:
            try:
                manager.send_message(ws, worker["agent_id"], HANDOFF_DIRECTIVE.format(direction=direction))
            except (KeyError, RuntimeError, OSError) as exc:
                slot.setdefault("warnings", []).append(f"{worker['agent_id']}: {exc}")
        if suspended or not ready:
            slot["reason"] = "driver suspended" if suspended else "Harness CLI, authentication or worker permission unavailable"
            out.append(slot)
            continue
        try:
            result = manager.spawn_agent(
                ws, driver=driver, problem=title, axis=seat, seat=seat, role_label="worker",
                task=REPLACE_TASK.format(title=title, direction=direction), track=track_name(seat),
                collab_mode="orthogonal", handover=replace_handover(ws, title, [w["agent_id"] for w in live], direction),
                _sprint=True)
        except (OSError, ValueError, RuntimeError, SubprocessError) as exc:
            slot["reason"] = str(exc)
            out.append(slot)
            continue
        if result.get("blocked"):
            slot["reason"] = result.get("reason") or "driver suspended"
            out.append(slot)
            continue
        agent = result["agent"]
        store.append_message(agent["agent_id"], "system", "seat_replace",
                             json.dumps({"title": title, "seat": seat, "direction": direction,
                                         "trigger": trigger, "replaced": [w["agent_id"] for w in live]}),
                             problem=title, event_key=f"{base}:{round_no}")
        slot.update(state="blocked" if agent["status"] == "error" else
                    "existing" if result.get("reused") else "started", agent_id=agent["agent_id"])
        workers.append(agent)
        out.append(slot)
    return out


def start_sprint(manager, workspace: Path, *, driver: str = "", preview: bool = False,
                 enable_auto_submit: bool = False, mode: str = "solo",
                 problems: list | None = None, seats: int | None = None, role: str = "",
                 adjust: bool = False, stop: list | None = None, replace: list | None = None) -> dict:
    ws = workspace.expanduser().resolve()
    mode = str(mode or "solo").strip().lower()
    if mode not in MODES:
        raise ValueError(f"Unknown sprint mode '{mode}'. Choose one of: {', '.join(MODES)}")
    role = str(role or "").strip().lower()
    if role not in ("", "worker", "knowledge"):
        raise ValueError("role must be worker or knowledge")
    driver = driver or get_active_driver_kind()
    catalogue = resolve_workspace_meta(ws)["problems"]
    if not catalogue:
        raise ValueError("No problems discovered in the selected contest")
    specs = normalize_specs(problems, mode=mode, tracks=seats)
    if not specs:
        # No selection is a whole-contest sprint; a knowledge seat on its own needs no problem.
        specs = ([{"title": "", "mode": mode, "seats": None, "role": "knowledge"}] if role == "knowledge"
                 else [{"title": p["title"], "mode": mode, "seats": seats, "role": role} for p in catalogue])
    if adjust and not specs:
        raise ValueError("adjust=true needs the problems whose seats should grow")
    rows, unbound = _select(catalogue, specs, mode, role)
    ready = _ready(manager, driver)
    assignments: list[dict[str, Any]] = []
    stopped: list[dict[str, Any]] = []
    replaced: list[dict[str, Any]] = []
    # All write calls run on the daemon. A replay after a partial launch reuses live slots.
    with manager.sprint_lock:
        config = ConfigRepository.load()
        active_workspace = config.get("active_workspace")
        selected = bool(active_workspace and Path(active_workspace).expanduser().resolve() == ws)
        if enable_auto_submit and not selected and not (adjust or stop or replace):
            raise ValueError("Select this contest in Parhelion before enabling automatic submission")
        occupied = sprint_seats(manager._store(ws).list_agents(limit=-1))
        # An explicit sprint start is the administrator confirming the provider has recovered.
        suspended = manager.driver_suspended(ws, driver)
        if suspended and not preview:
            manager.clear_driver_suspension(ws, driver)
            suspended = False
        if stop:
            stopped = _stop_seats(manager, ws, [str(s) for s in stop], preview=preview)
            occupied = sprint_seats(manager._store(ws).list_agents(limit=-1))
        if replace:
            replaced = _replace_seats(manager, ws, [str(t) for t in replace], preview=preview,
                                      driver=driver, ready=ready, suspended=suspended)
            occupied = sprint_seats(manager._store(ws).list_agents(limit=-1))
        plans: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for spec, problem in rows:
            for seat in expand_seats(spec["mode"], spec.get("seats")):
                if spec.get("role") == "knowledge":
                    seat = {"seat": "knowledge", "role": "knowledge", "task": seat_plan.TASKS["knowledge"],
                            "collab": "solo", "track": track_name("knowledge"), "mode": "knowledge"}
                plans.append((spec, {**seat, "problem": problem}))
        for _spec, plan in plans:
            title = plan["problem"]["title"]
            repo = (ws / (plan["problem"].get("path") or title)).resolve()
            seat = plan["seat"]
            slot = dict(problem=title, seat=seat, axis=seat, branch=plan["track"], role=plan["role"],
                        mode=plan["mode"])
            assignments.append(slot)
            existing = occupied.get((title, seat))
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
                    result = manager.spawn_agent(ws, driver=driver, problem=title, axis=seat, seat=seat,
                        role_label=plan["role"], task=plan["task"], track=plan["track"],
                        collab_mode=plan["collab"], _sprint=True)
                    if result.get("blocked"):
                        slot.update(state="blocked", reason=result.get("reason") or "driver suspended")
                        continue
                    agent = result["agent"]
                    occupied[(title, seat)] = agent
                    slot.update(state="blocked" if agent["status"] == "error" else
                                "existing" if result.get("reused") else "started", agent_id=agent["agent_id"])
                except (OSError, ValueError, RuntimeError, SubprocessError) as exc:
                    slot.update(state="blocked", reason=str(exc))
        for spec in unbound:
            title = ""
            seat = "knowledge"
            slot = dict(problem=title, seat=seat, axis=seat, branch=track_name(seat), role="knowledge",
                        mode="knowledge")
            assignments.append(slot)
            existing = occupied.get((ws.name, seat))
            if existing and existing["status"] not in ("stopped", "error"):
                slot.update(state="existing", agent_id=existing["agent_id"])
            elif suspended:
                slot.update(state="blocked", reason="driver suspended")
            elif not ready:
                slot.update(state="blocked", reason="Harness CLI, authentication or worker permission unavailable")
            elif preview:
                slot.update(state="planned")
            else:
                try:
                    result = manager.spawn_agent(ws, driver=driver, problem="", axis=seat, seat=seat,
                                                 role_label="knowledge", task=seat_plan.TASKS["knowledge"],
                                                 track=track_name(seat), collab_mode="solo", _sprint=True)
                    if result.get("blocked"):
                        slot.update(state="blocked", reason=result.get("reason") or "driver suspended")
                        continue
                    agent = result["agent"]
                    occupied[(ws.name, seat)] = agent
                    slot.update(state="blocked" if agent["status"] == "error" else
                                "existing" if result.get("reused") else "started", agent_id=agent["agent_id"])
                except (OSError, ValueError, RuntimeError, SubprocessError) as exc:
                    slot.update(state="blocked", reason=str(exc))
        if (enable_auto_submit and not preview and not (adjust or stop or replace)
                and any(s["state"] in ("existing", "started") for s in assignments)):
            config = ConfigRepository.load()
            if str(ws) != str(Path(config.get("active_workspace") or ".").expanduser().resolve()):
                raise ValueError("Active contest changed during startup; automatic submission was not enabled")
            config["scheduler"]["auto_consumer_enabled"] = True
            ConfigRepository.save(config)
    blocked = [s for s in assignments + replaced if s["state"] == "blocked"]
    return {"ok": not blocked, "driver": driver,
            "auto_submit_enabled": selected and bool(config["scheduler"]["auto_consumer_enabled"]),
            "preview": preview, "mode": mode, "adjust": adjust, "problems": len(rows),
            "assignments": assignments, "stopped": stopped, "replaced": replaced}


__all__ = ["DEVICE_HINT", "HANDOFF_DIRECTIVE", "MODES", "expand_seats", "seat_plan", "seat_role",
           "sprint_seats", "start_sprint", "track_name"]


def seat_role(seat: str) -> str:
    return seat_plan.seat_role(seat)
