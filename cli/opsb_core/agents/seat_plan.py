"""Seat identity and per-problem mode expansion for sprint v2 (4.3 P26).

A seat is a ``(problem, seat name)`` pair. ``sprint-<seat>`` is the worktree/branch track of that
seat, so the 4.2 names (``sprint-solo``, ``sprint-algorithm``, ...) keep the same layout. Modes expand
to a list of seat specs; ``race`` opens N tracks on one goal and ``pipeline`` splits a problem into
recon, K implementation seats, an integrator and a harness (bench-author) seat.

The failures view and the last handoff narrative of a replaced worker are rendered here, because the
replacement seat needs them in its start-up pack and nothing else in the platform does.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEVICE_HINT = " Device work: parhelion_test, then parhelion_test_wait."
#: Axis names the 4.2 layout used as ``.worktrees/sprint-<axis>``; a record without a ``seat``
#: column is still recognised through them.
LEGACY_SEATS = ("solo", "algorithm", "dataflow", "calibration")
#: Seats that may run the platform suite; recon and knowledge seats deliver documents instead.
NON_TEST_ROLES = frozenset({"recon", "knowledge"})
MAX_TRACKS = 8
DEFAULT_TRACKS = {"race": 2, "pipeline": 2}
MODES = ("solo", "dual", "full", "race", "pipeline")

TASKS = {
    "solo": ("Own this problem end to end. Pick one hypothesis from the state card, change one variable, "
             "run parhelion_test level=standard, compare against the baseline in the digest, and hand a candidate "
             "to parhelion_submit_gate only when the digest shows a measured gain. Record negative results with "
             "parhelion_agent_report."),
    "algorithm": "Explore algorithmic and numerical changes. Compare against the passing baseline.",
    "dataflow": "Explore tiling, memory traffic and kernel launch costs on measured buckets.",
    "calibration": ("Calibrate on a new candidate or official receipt. Reuse comparable baseline evidence and "
                    "measure only the cases needed to test your hypothesis. Share the run IDs and findings, "
                    "then wait for new evidence. The suite is platform-owned: propose case and bucket variants "
                    "to the orchestrator, and do not write measurement scripts outside the platform suite."),
    "race": ("You are one of {tracks} tracks racing on this same goal; a rival seat is working the same objective. "
             "Pick a mechanism your rivals are not using, change one variable per round, and keep every candidate "
             "measured on the same baseline. There is no separate winner rule: each track goes through the same "
             "gate, and the platform keeps the best gated candidate in its candidate archive (top-3)."),
    "recon": ("Read-only reconnaissance. Do not run tests and do not enqueue submissions (gate previews only). "
              "Read the problem repository, receipts, per-case and bottleneck views, then deliver one blueprint: "
              "the measured bottleneck split, 2-3 candidate mechanisms with their evidence, and which "
              "implementation seat should own each one. Send it with parhelion_squad to the implementation "
              "seats (recipient all works) and record it with parhelion_note."),
    "impl": ("Own one bottleneck split of this problem inside a pipeline: the recon blueprint and the ledger name "
             "the mechanisms, you own the ones assigned to you. Keep your branch independent, measure with "
             "parhelion_test, and hand the SHA to the integrator seat with parhelion_squad instead of submitting "
             "a competing candidate yourself."),
    "integrator": ("Merge the implementation seats' branches (sprint-impl-*) into your branch, resolve conflicts, "
                   "run parhelion_test level=final on the merged SHA, and submit one candidate through "
                   "parhelion_submit_gate. Ask the implementation seats which branch carries the measured gain "
                   "before merging, and report the merged SHA to the orchestrator."),
    "knowledge": ("Knowledge seat: run no tests and enqueue no submissions. Turn measured findings into reusable "
                  "entries with parhelion_kb_record (mechanism, trigger conditions, cost, applicable problems, "
                  "evidence), and write the supporting documents in the workspace. Deliverable: KB entries plus "
                  "the document paths, not a score."),
}

#: Worker subroles outside the plain worker table; the MCP tool filter and the brief contracts share them.
CONTRACT_ROLES = ("race", "recon", "integrator", "knowledge")


def track_name(seat: str) -> str:
    """Worktree and branch name of a seat (4.2 names are kept as ``sprint-<axis>``)."""
    return f"sprint-{seat}"


def seat_role(seat: str) -> str:
    """Role label of a seat: the axis name, except calibration/harness which are bench-author seats."""
    if seat in ("calibration", "harness"):
        return "bench-author"
    if seat.startswith("race-"):
        return "race"
    return seat if seat in ("recon", "knowledge", "integrator", "race") else "worker"


def expand_seats(mode: str, tracks: int | None = None) -> list[dict[str, Any]]:
    """The seat specs of one problem under ``mode``; ``tracks`` overrides N (race) or K (pipeline)."""
    mode = str(mode or "solo").strip().lower()
    if mode not in MODES:
        raise ValueError(f"Unknown sprint mode '{mode}'. Choose one of: {', '.join(MODES)}")
    count = DEFAULT_TRACKS.get(mode)
    if tracks is not None:
        try:
            count = int(tracks)
        except (TypeError, ValueError):
            raise ValueError("seats must be an integer") from None
        if count < 1 or count > MAX_TRACKS:
            raise ValueError(f"seats must be between 1 and {MAX_TRACKS}")
    names: list[str] = []
    if mode == "solo":
        names = ["solo"]
    elif mode == "dual":
        names = ["algorithm", "dataflow"]
    elif mode == "full":
        names = ["algorithm", "dataflow", "calibration"]
    elif mode == "race":
        names = [f"race-{i}" for i in range(1, (count or DEFAULT_TRACKS["race"]) + 1)]
    else:  # pipeline
        impl = count or DEFAULT_TRACKS["pipeline"]
        names = ["recon", *[f"impl-{i}" for i in range(1, impl + 1)], "integrator", "harness"]
    out = []
    for name in names:
        role = seat_role(name)
        task_text = TASKS["calibration"] if name == "harness" else \
            TASKS["recon"] if name == "recon" else TASKS["knowledge"] if name == "knowledge" else \
            TASKS["integrator"] if name == "integrator" else TASKS["race"] if name.startswith("race-") else \
            TASKS["impl"] if name.startswith("impl-") else TASKS[name]
        task = task_text.format(tracks=count or DEFAULT_TRACKS.get("race", 2))
        out.append({
            "seat": name, "role": role, "task": task + ("" if role in NON_TEST_ROLES else DEVICE_HINT),
            "collab": "solo" if mode == "solo" else "pipeline" if mode == "pipeline" else "orthogonal",
            "track": track_name(name), "mode": mode, "testable": role not in NON_TEST_ROLES,
        })
    return out


def seat_of(worker: dict[str, Any]) -> str:
    """Seat name of one bus record: the stored column, else the 4.2 ``.worktrees/sprint-<axis>`` name."""
    stored = str(worker.get("seat") or "").strip()
    if stored:
        return stored
    path = Path(str(worker.get("worktree") or ""))
    if path.parent.name == ".worktrees" and path.name.startswith("sprint-"):
        axis = path.name[len("sprint-"):]
        if axis in LEGACY_SEATS:
            return axis
    return ""


def sprint_seats(workers) -> dict[tuple[str, str], dict[str, Any]]:
    """``{(problem, seat): worker}`` for the live seat of every (problem, seat), newest first."""
    seats: dict[tuple[str, str], dict[str, Any]] = {}
    for worker in sorted(workers, key=lambda w: (w["status"] != "stopped", w["created_at"]), reverse=True):
        seat = seat_of(worker)
        if seat:
            seats.setdefault((worker["problem"], seat), worker)
    return seats


def normalize_specs(raw: Any, *, mode: str, tracks: int | None) -> list[dict[str, Any]]:
    """Parse ``problems`` (titles, or ``{title, mode, seats}`` objects) into per-problem specs.

    A ``{"role": "knowledge"}`` entry needs no title: it opens the knowledge seat, whose deliverable is
    a KB entry and document paths rather than a score.
    """
    specs: list[dict[str, Any]] = []
    for item in raw or []:
        if isinstance(item, str):
            if item.strip():
                specs.append({"title": item.strip(), "mode": mode, "seats": tracks, "role": ""})
            continue
        if not isinstance(item, dict):
            raise ValueError("problems must be titles or objects {title, mode, seats, role}")
        title = str(item.get("title") or "").strip()
        role = str(item.get("role") or "").strip().lower()
        if role and role not in ("worker", "knowledge"):
            raise ValueError("problem role must be worker or knowledge")
        if not title:
            if role == "knowledge":
                specs.append({"title": "", "mode": mode, "seats": None, "role": "knowledge"})
                continue
            raise ValueError("each problem object needs a title (except a knowledge seat)")
        seats = item.get("seats")
        if seats is not None and (not isinstance(seats, int) or isinstance(seats, bool)):
            raise ValueError(f"seats for {title} must be an integer")
        specs.append({"title": title, "mode": str(item.get("mode") or mode).strip().lower(),
                      "seats": seats, "role": role})
    return specs


def _clip(text: Any, limit: int) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _handoff_narrative(workspace: Path, agent_id: str) -> str:
    """The newest narrative in ``state/handoffs/<agent>.jsonl`` ("" when the worker never wrote one)."""
    from ..drivers.base import resolve_workspace_state_dir

    path = resolve_workspace_state_dir(workspace) / "handoffs" / f"{agent_id}.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("narrative_final"):
            return _clip(row["narrative_final"], 1200)
    return ""


def replace_handover(workspace: Path, title: str, old_agents: list[str], direction: str) -> str:
    """Start-up handover for a replacement seat: the failures view plus the last handoff narratives.

    The narrative is usually still being written when the seat is replaced, so the block also names the
    handoff file the old seat writes, which the new seat can read at its first turn.
    """
    from ..drivers.base import resolve_workspace_state_dir
    from ..ledger import store as ledger_store
    from ..ledger import view as ledger_view

    ws = Path(workspace).expanduser().resolve()
    lines = [f"Replacement seat for {title}: work the orthogonal direction '{direction}'. Do not repeat the "
             "failed directions below; the platform replaced the previous seat because six paid attempts "
             "produced no gain."]
    try:
        failures = ledger_view.failures(ledger_store.read_problem(ws, title))[:8]
    except Exception:
        failures = []
    if failures:
        lines.append("Failures (newest first):")
        for row in failures:
            why = f" because {_clip(row.get('why_failed'), 100)}" if row.get("why_failed") else ""
            retry = f"; retry if {_clip(row.get('retry_conditions'), 70)}" if row.get("retry_conditions") else ""
            lines.append(f"- {ledger_view.ref(row)} failed: {_clip(row.get('approach') or row.get('claim'), 110)}{why}{retry}")
    else:
        lines.append("Failures: the problem ledger has no failure entry yet; read the receipts before choosing a mechanism.")
    for agent in old_agents:
        narrative = _handoff_narrative(ws, agent)
        if narrative:
            lines.append(f"Handoff from the replaced seat ({agent}):\n{narrative}")
        path = resolve_workspace_state_dir(ws) / "handoffs" / f"{agent}.jsonl"
        lines.append(f"Read `{path}` at your first turn: the replaced seat was asked to append its handoff there.")
    return "\n".join(lines)


def orthogonal_direction(covered) -> str:
    """A direction no live seat of the problem covers (algorithm and dataflow are the usual rivals)."""
    used = {str(x) for x in covered or []}
    for axis in ("dataflow", "algorithm", "numerical"):
        if axis not in used:
            return axis
    return "numerical"
