"""Top-K candidate archive per problem (research R12, agent-optimization-practice.md).

Serial refinement stays the default (one worker per problem). Beside it the platform keeps the
K best candidate SHAs with their per-case records, so a dead-end experiment never replaces the
incumbent: a candidate enters only when it passed Stage 1 and its gate-predicted score beats the
weakest of K, and it must differ from every incumbent. Two candidates are the same idea when their
best warm minima agree within DUPLICATE_BAND on DUPLICATE_SHARE of the common official cases; the
better-predicted one stays. The archive lives in ``results/<title>/.candidate-archive.json`` and is
written only when it changes.
"""
from __future__ import annotations

import datetime
import json
import pathlib
from typing import Any

from . import case_map
from .parhelion_paths import atomic_write_json

K = 3
FILE = ".candidate-archive.json"
DUPLICATE_BAND = 0.03
DUPLICATE_SHARE = 0.9


def path(ws: pathlib.Path, title: str) -> pathlib.Path:
    return ws / "results" / title / FILE


def load(ws: pathlib.Path, title: str) -> list[dict[str, Any]]:
    try:
        doc = json.loads(path(ws, title).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [c for c in doc.get("candidates") or [] if isinstance(c, dict)] if isinstance(doc, dict) else []


def same_idea(a: dict[str, float], b: dict[str, float]) -> bool:
    common = [k for k in a if k in b and a[k] > 0 and b[k] > 0]
    if not common:
        return False
    close = sum(1 for k in common if abs(a[k] / b[k] - 1.0) <= DUPLICATE_BAND)
    return close >= DUPLICATE_SHARE * len(common)


def admit(archive: list[dict[str, Any]], entry: dict[str, Any]) -> list[dict[str, Any]]:
    """The archive after offering ``entry`` (pure)."""
    others = [c for c in archive if c["sha"] != entry["sha"]]
    twins = [c for c in others if same_idea(c.get("minima") or {}, entry["minima"])]
    if any(c["predicted"] >= entry["predicted"] for c in twins):
        return sorted(others + [c for c in archive if c["sha"] == entry["sha"]], key=lambda c: -c["predicted"])
    kept = [c for c in others if c not in twins] + [entry]
    return sorted(kept, key=lambda c: -c["predicted"])[:K]


def offer(ws: pathlib.Path, title: str, verdict: dict[str, Any]) -> list[dict[str, Any]]:
    """Offer one gate verdict; returns the (possibly unchanged) archive."""
    archive = load(ws, title)
    stages = {s.get("stage"): s for s in verdict.get("stages") or [] if isinstance(s, dict)}
    est = verdict.get("estimate") if isinstance(verdict.get("estimate"), dict) else {}
    predicted = est.get("estimated_score")
    sha = str(verdict.get("sha") or "")
    if not (stages.get("SAFETY", {}).get("passed") and isinstance(predicted, (int, float)) and len(sha) == 40):
        return archive  # failed or unmeasured runs never enter
    key, _ = case_map.local_catalog(ws, title)
    minima = case_map.local_minima(ws, title, sha, key, strict=True)[0]
    if not minima:
        return archive
    entry = {"sha": sha, "predicted": round(float(predicted), 2), "method": est.get("estimation_method"),
             "baseline_sha": est.get("baseline_sha"), "minima": {k: round(v, 3) for k, v in sorted(minima.items())},
             "evaluated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    updated = admit(archive, entry)
    strip = lambda rows: [{k: v for k, v in c.items() if k != "evaluated_at"} for c in rows]  # noqa: E731
    if strip(updated) != strip(archive) and path(ws, title).parent.is_dir():
        atomic_write_json(path(ws, title), {"title": title, "k": K, "candidates": updated})
    return updated


def summary(ws: pathlib.Path, titles: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Compact archive per problem for the status payload (no per-case records)."""
    out = {}
    for title in titles:
        rows = load(ws, title)
        if rows:
            out[title] = [{"rank": i + 1, "sha": c["sha"], "short_sha": c["sha"][:7], "predicted": c["predicted"],
                           "method": c.get("method"), "cases": len(c.get("minima") or {})} for i, c in enumerate(rows)]
    return out
