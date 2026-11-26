"""Compaction replay benchmark (docs/v4.2/00-PLAN.md section 5.1): cut, probe, seed, score."""
from __future__ import annotations

import statistics
from pathlib import Path
from typing import Any

from . import probes as _probes
from . import score as _score
from . import seeds as _seeds
from .loader import AgentLog, load_agent

DEFAULT_SCHEMES = ("current", "current_plus_ledger", "full_history")


def run(paths: list[Path], *, schemes: list[str] | None = None, since: str | None = None,
        until: str | None = None, judge: str = "keys", with_details: bool = False) -> dict[str, Any]:
    names = list(schemes or DEFAULT_SCHEMES)
    builders = {n: _seeds.get(n) for n in names}  # fail fast on an unknown scheme
    logs: list[AgentLog] = [g for g in (load_agent(p, since=since, until=until) for p in paths) if g.turns]
    probes_by_cut: dict[tuple[str, int], list[_probes.Probe]] = {}
    seeds_by_cut: dict[tuple[str, int], dict[str, str]] = {}
    sizes: dict[str, list[int]] = {n: [] for n in names}
    cuts = []
    for log in logs:
        for cut in log.cuts:
            key = (log.agent_id, cut.index)
            probes_by_cut[key] = _probes.generate(log, cut)
            ctx = _seeds.CutContext(log, cut)
            seeds_by_cut[key] = {n: b(ctx) for n, b in builders.items()}
            for n in names:
                sizes[n].append(len(seeds_by_cut[key][n]))
            cuts.append({"agent_id": log.agent_id, "cut": cut.index, "ts": cut.ts, "handoff_chars": len(cut.handoff),
                         "logged_seed": bool(cut.seed), "probes": len(probes_by_cut[key])})
    scored = _score.score(probes_by_cut, seeds_by_cut, names, judge=judge)
    by_type: dict[str, int] = {}
    for plist in probes_by_cut.values():
        for p in plist:
            by_type[p.type] = by_type.get(p.type, 0) + 1
    out: dict[str, Any] = {
        "workers": len(logs), "workers_with_cuts": sum(1 for g in logs if g.cuts), "cuts": cuts,
        "probes": {"total": sum(by_type.values()), "by_type": dict(sorted(by_type.items()))},
        "seed_chars": {n: {"median": statistics.median(v) if v else None, "max": max(v, default=None)}
                       for n, v in sizes.items()},
        "schemes": scored["schemes"], "judge": judge,
    }
    if with_details:
        out["details"] = scored["details"]
        out["probe_list"] = [p.as_dict() for plist in probes_by_cut.values() for p in plist]
    return out
