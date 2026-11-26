"""Coverage scoring of seeds against probes, with per-worker bootstrap confidence intervals.

A probe is covered when at least ``need`` of its key points occur in the seed (SHA prefixes
and file names as case-insensitive substrings, keywords as whole words). The optional LLM
judge stays off: this module never calls a model API.
"""
from __future__ import annotations

import random
import re
from typing import Any, Iterable

from .probes import Probe

BOOTSTRAP_ROUNDS = 1000
BOOTSTRAP_SEED = 20261126
CI_LEVEL = 0.95


def key_present(key: str, text_lower: str) -> bool:
    k = key.lower()
    if re.fullmatch(r"[a-z_][a-z0-9_]*", k):
        return re.search(rf"(?<![a-z0-9_]){re.escape(k)}(?![a-z0-9_])", text_lower) is not None
    return k in text_lower


def covered(probe: Probe, seed_lower: str) -> bool:
    if not probe.keys:
        return False
    if probe.context:
        lead = probe.keys[0]
        pats = [re.compile(p, re.I) for p in probe.context]
        return any(key_present(lead, ln) and any(p.search(ln) for p in pats)
                   for ln in seed_lower.splitlines() if lead.lower() in ln)
    hits = sum(1 for k in probe.keys if key_present(k, seed_lower))
    return hits >= min(probe.need, len(probe.keys))


def bootstrap_ci(values: list[int], *, rounds: int = BOOTSTRAP_ROUNDS, seed: int = BOOTSTRAP_SEED,
                 level: float = CI_LEVEL) -> tuple[float, float] | None:
    """Percentile interval of the mean of 0/1 ``values`` resampled with replacement."""
    if not values:
        return None
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(rounds))
    lo = means[int((1 - level) / 2 * (rounds - 1))]
    hi = means[int((1 + level) / 2 * (rounds - 1))]
    return round(lo, 4), round(hi, 4)


def _cell(values: list[int]) -> dict[str, Any]:
    n = len(values)
    return {"probes": n, "covered": sum(values), "coverage": round(sum(values) / n, 4) if n else None,
            "ci95": bootstrap_ci(values)}


def paired_ci(diffs_by_worker: dict[str, list[int]], *, rounds: int = BOOTSTRAP_ROUNDS,
              seed: int = BOOTSTRAP_SEED, level: float = CI_LEVEL) -> dict[str, Any]:
    """Mean paired difference of two schemes and a bootstrap CI resampling whole workers.

    ``diffs_by_worker`` maps a worker id to its probe-level differences (1 covered, 0 not, -1 lost).
    Workers are the resampling unit because a worker's cuts share its history, so probes are not
    independent within a worker.
    """
    groups = [values for values in diffs_by_worker.values() if values]
    flat = [v for values in groups for v in values]
    if not flat or not groups:
        return {"mean": None, "ci95": None, "probes": 0}
    rng = random.Random(seed)
    means = []
    for _ in range(rounds):
        total = 0
        count = 0
        for _ in range(len(groups)):
            picked = groups[rng.randrange(len(groups))]
            total += sum(picked)
            count += len(picked)
        means.append(total / count if count else 0.0)
    means.sort()
    lo = means[int((1 - level) / 2 * (rounds - 1))]
    hi = means[int((1 + level) / 2 * (rounds - 1))]
    return {"mean": round(sum(flat) / len(flat), 4), "ci95": [round(lo, 4), round(hi, 4)],
            "probes": len(flat), "workers": len(groups),
            "gained": sum(1 for v in flat if v > 0), "lost": sum(1 for v in flat if v < 0)}


def compare(details: list[dict[str, Any]], schemes: list[str], by_worker: dict[str, dict[str, int]] | None = None
            ) -> dict[str, Any]:
    """``{scheme: paired vs schemes[0]}`` from per-probe verdicts (``details`` of ``score``)."""
    base = schemes[0]
    verdict: dict[tuple[str, str], bool] = {(d["probe"], d["scheme"]): bool(d["covered"]) for d in details}
    workers = by_worker or {}
    out: dict[str, Any] = {}
    for scheme in schemes[1:]:
        diffs: dict[str, list[int]] = {}
        for d in details:
            if d["scheme"] != scheme:
                continue
            other = verdict.get((d["probe"], base))
            if other is None:
                continue
            delta = int(bool(d["covered"])) - int(other)
            worker = workers.get(d["probe"], "")
            diffs.setdefault(worker or "-", []).append(delta)
        out[scheme] = {"vs": base, **paired_ci(diffs)}
    return out


def by_type_diff(details: list[dict[str, Any]], schemes: list[str]) -> dict[str, Any]:
    """``{scheme: {type: [gained, lost]}}`` against ``schemes[0]``: which probes moved and which way."""
    base = schemes[0]
    verdict = {(d["probe"], d["scheme"]): (bool(d["covered"]), d.get("type") or "?") for d in details}
    out: dict[str, dict[str, list[int]]] = {}
    for scheme in schemes[1:]:
        cells: dict[str, list[int]] = {}
        for key, (covered, ptype) in verdict.items():
            if key[1] != scheme:
                continue
            other = verdict.get((key[0], base))
            if other is None:
                continue
            gain, loss = cells.setdefault(ptype, [0, 0])
            gained = covered and not other[0]
            lost = other[0] and not covered
            cells[ptype] = [gain + int(gained), loss + int(lost)]
        out[scheme] = cells
    return out


def score(probes_by_cut: dict[tuple[str, int], list[Probe]], seeds_by_cut: dict[tuple[str, int], dict[str, str]],
          schemes: Iterable[str], *, judge: str = "keys") -> dict[str, Any]:
    """``{scheme: {overall, by_type, by_worker}}`` plus per-probe verdicts (``details``)."""
    if judge != "keys":
        raise ValueError("LLM judge mode is disabled: replay scoring only uses key-point matching")
    out: dict[str, Any] = {}
    details: list[dict[str, Any]] = []
    for scheme in schemes:
        overall: list[int] = []
        by_type: dict[str, list[int]] = {}
        by_worker: dict[str, list[int]] = {}
        for key, plist in sorted(probes_by_cut.items()):
            seed_lower = (seeds_by_cut.get(key, {}).get(scheme) or "").lower()
            for p in plist:
                hit = int(covered(p, seed_lower))
                overall.append(hit)
                by_type.setdefault(p.type, []).append(hit)
                by_worker.setdefault(p.agent_id, []).append(hit)
                details.append({"scheme": scheme, "probe": p.id, "type": p.type, "covered": bool(hit)})
        out[scheme] = {"overall": _cell(overall), "by_type": {t: _cell(v) for t, v in sorted(by_type.items())},
                       "by_worker": {w: _cell(v) for w, v in sorted(by_worker.items())}}
    return {"schemes": out, "details": details}
