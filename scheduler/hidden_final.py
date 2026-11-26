"""Unpublished final-level input variants: inputs the worker never sees (4.2 R1 item 2).

Every formal ``final`` run derives a few extra local variants from the official cases, with
seeds and choices drawn deterministically from the run ID. They stay inside what the frozen
oracle can build:

- ``seed``: an official case with a fresh seed (its own generator, another value draw);
- ``scale``: an official case whose floating-point inputs the runner multiplies by 3, 0.01 or
  -1 before the oracle computes the expected output (``transform``, runtime reference_cases);
- ``shape``: an official case with an explicit ``shape`` and one axis set to an odd size below
  the published one.

The variants share the suite's variant validation (``scheduler.suite.extend``) and carry
``origin="hidden-final"``, but they are never written to ``suite.json``: they exist in one run's
catalog only, so the suite version and the official pairing key (scheduler.pairing) do not move.
Large bases are skipped because each fresh input is generated on the device host's CPU.

A ``seed`` variant is the oracle's own distribution, so its failure fails the run like any
other case. ``scale`` and ``shape`` variants can step outside a problem's stated input domain,
so their failures are reported (``hidden_final`` in the report and digest) without failing the
run. A variant the oracle itself cannot build is ``SKIP``: that says nothing about the kernel.
Worker-facing digests name only the class and base case, never seeds, shapes or values.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from pathlib import Path
from typing import Any

from . import suite as suites

ORIGIN = "hidden-final"
SEEDS = 4
SCALES = (3.0, 0.01, -1.0)
SHAPES = 2
MAX_BYTES = 64 << 20          # input + output bytes of a base case, when a run has measured it
MAX_ELEMENTS = 16 << 20       # parameter-product proxy for bases never measured
BLOCKING_CLASSES = frozenset({"seed"})


def _rng(run_id: str) -> random.Random:
    return random.Random(int(hashlib.sha256(f"{run_id}:hidden-final".encode()).hexdigest()[:16], 16))


def derived_seed(run_id: str, n: int) -> int:
    return int(hashlib.sha256(f"{run_id}:hidden:{n}".encode()).hexdigest()[:8], 16) & 0x7FFFFFFF


def _shape(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(type(n) is int and n > 0 for n in value)


def _elements(params: dict[str, Any]) -> int:
    if _shape(params.get("shape")):
        return math.prod(params["shape"])
    ints = [v for v in params.values() if type(v) is int and v > 0]
    return math.prod(ints) if ints else 1


def _small(case: dict[str, Any], sizes: dict[str, int]) -> bool:
    known = sizes.get(case["id"])
    if isinstance(known, int) and known > 0:
        return known <= MAX_BYTES
    return _elements(case.get("params") or {}) <= MAX_ELEMENTS


def _seedable(case: dict[str, Any]) -> bool:
    params = case.get("params") or {}
    return (case.get("source") == "official" and type(case.get("seed")) is int
            and not any(str(k).endswith("values") for k in params))


def plan(official: list[dict[str, Any]], run_id: str, sizes: dict[str, int] | None = None) -> list[dict[str, Any]]:
    """The run's hidden variants (deterministic in ``run_id``); empty when no base qualifies."""
    sizes = sizes or {}
    rng = _rng(run_id)
    bases = [c for c in official if _seedable(c) and _small(c, sizes)]
    floats = [c for c in bases if "float" in str(c.get("dtype") or "float")]
    shaped = [c for c in bases if _shape((c.get("params") or {}).get("shape"))]
    out: list[dict[str, Any]] = []

    def add(base: dict[str, Any], kind: str, **body: Any) -> None:
        out.append({"id": f"local/hf-{run_id[:8]}-{len(out):02d}", "base": base["id"], "perf": False,
                    "tier": "final", "origin": ORIGIN, "hidden_class": kind,
                    "name": f"hidden-final {kind} of {base['id']}", **body})

    for n, base in enumerate(rng.sample(bases, min(SEEDS, len(bases)))):
        seed = derived_seed(run_id, n)
        add(base, "seed", seed=seed if seed != base["seed"] else seed ^ 1)
    for factor in SCALES if floats else ():
        add(rng.choice(floats), "scale", transform={"scale": factor})
    for n, base in enumerate(rng.sample(shaped, min(SHAPES, len(shaped)))):
        shape = list(base["params"]["shape"])
        axis = max(range(len(shape)), key=lambda i: (shape[i], -i)) if rng.random() < 0.5 else len(shape) - 1
        size = shape[axis]
        if size < 3:
            continue  # nothing odd fits below this axis
        new = rng.randint(max(1, size // 2), size - 1)
        new += (1 if new + 1 < size else -1) if new % 2 == 0 else 0
        shape[axis] = new
        add(base, "shape", params={"shape": shape}, seed=derived_seed(run_id, SEEDS + n))
    return out


def _read(path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def recent_reports(ws, title: str, n: int = 3) -> list[dict[str, Any]]:
    """The newest finished runs of the problem (one per SHA pointer), cheap to read at run start."""
    pointers = [_read(p) for p in (Path(ws) / "results" / title).glob("*/latest-bench.json")]
    pointers.sort(key=lambda d: str(d.get("finished_at") or ""), reverse=True)
    return [doc for doc in (_read(Path(str(d.get("report")))) for d in pointers[:n] if d.get("report")) if doc]


def known_sizes(reports: list[dict[str, Any]]) -> dict[str, int]:
    """Measured input + output bytes per official case, newest report first."""
    sizes: dict[str, int] = {}
    for doc in sorted(reports, key=lambda d: d.get("finished_at") or "", reverse=True):
        for row in doc.get("cases") or []:
            if row.get("source") == "official" and type(row.get("bytes")) is int:
                sizes.setdefault(row["id"], row["bytes"])
    return sizes


def derive(spec: dict[str, Any], cases: list[dict[str, Any]], run_id: str,
           reports: list[dict[str, Any]] | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """``(suite with the hidden variants, variants)`` for one final run; the suite file is untouched."""
    official = [c for c in cases if c.get("source") == "official"]
    variants = plan(official, run_id, known_sizes(reports or []))
    return (suites.extend(spec, variants) if variants else spec), variants


def is_hidden(row: dict[str, Any]) -> bool:
    return row.get("origin") == ORIGIN


def advisory(row: dict[str, Any]) -> bool:
    """A hidden variant whose result is reported but never fails the run."""
    return is_hidden(row) and (row.get("hidden_class") not in BLOCKING_CLASSES or row.get("status") == "SKIP")


def settle(rows: dict[str, dict[str, Any]]) -> None:
    """A hidden variant the oracle could not build is skipped, not failed."""
    for row in rows.values():
        if is_hidden(row) and row.get("status") == "ERROR" and row.get("phase") == "prepare":
            row["status"] = "SKIP"
            row["detail"] = "the frozen oracle cannot build this hidden variant"


def blocking(rows: dict[str, dict[str, Any]], selected: list[str]) -> list[dict[str, Any]]:
    """Selected rows that decide whether the run verifies."""
    return [rows[k] for k in selected if k in rows and not advisory(rows[k])]


def summary(rows: dict[str, dict[str, Any]], selected: list[str]) -> dict[str, Any] | None:
    hidden = [rows[k] for k in selected if k in rows and is_hidden(rows[k])]
    if not hidden:
        return None
    failed = [r for r in hidden if r.get("status") not in ("PASS", "SKIP", "NOT_RUN")]
    return {
        "total": len(hidden),
        "passed": sum(r.get("status") == "PASS" for r in hidden),
        "skipped": sum(r.get("status") == "SKIP" for r in hidden),
        "failed": [{"class": r.get("hidden_class"), "base": r.get("base"), "status": r.get("status"),
                    "blocking": not advisory(r)} for r in failed],
    }


def redact(row: dict[str, Any]) -> dict[str, Any]:
    """A worker-facing failure line: class and base case only."""
    kind = row.get("hidden_class") or "hidden"
    return {"id": f"hidden-final:{kind}:{row.get('base')}", "status": row.get("status"),
            "detail": f"unpublished {kind} variant of case {row.get('base')} (inputs withheld)"}
