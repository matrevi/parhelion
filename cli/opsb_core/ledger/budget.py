"""Failure fingerprints, the three-attempt budget and the plateau rule (research R5, R8).

A fingerprint is the stable identity of one attempt: its case set, its bottleneck class, its diff class
and its outcome. Because it is recomputed from the append-only ledger on disk, the count survives a
restart, a new session and a renamed hypothesis: renaming a hypothesis does not buy a fourth attempt.

Nothing here blocks a call. ``warnings`` returns notes the tool handlers attach to their reply, naming
the ledger ids to read and the way out: change the bottleneck class, or give a ``revisit_if`` reason.

- ``ATTEMPT_BUDGET``  the same fingerprint, or the same bottleneck class, recorded three times;
- ``PLATEAU``         the last ``PLATEAU_RUN`` experiments without a single ``better`` outcome.

The bottleneck class is the entry's ``scope.bucket`` (the platform's analysis dimension) or
``scope.bottleneck`` when a worker names one, falling back to a small keyword vocabulary of the claim.
The diff class comes from the change summary. Neither is a model judgement of correctness: the classes
only group attempts for counting, and every warning names the entries it counted.
"""
from __future__ import annotations

import hashlib
from typing import Any, Iterable

from . import model, view

ATTEMPT_BUDGET = 3
PLATEAU_RUN = 5
MAX_WARNINGS = 3
POSITIVE = ("better",)
CLASSES = (
    ("memory", ("dma", "bandwidth", "hbm", "l2", "l1", "cache", "memory", "load", "store", "buffer")),
    ("compute", ("compute", "cube", "mac", "flop", "alu", "vector", "simd", "fma")),
    ("sync", ("sync", "barrier", "pipe", "lock", "stall", "serial")),
    ("occupancy", ("occupancy", "block", "thread", "warp", "core", "tile")),
    ("layout", ("layout", "stride", "transpose", "padding", "pad", "align", "shape", "broadcast")),
    ("precision", ("precision", "cast", "fp16", "fp32", "bf16", "round", "quant")),
    ("launch", ("launch", "overhead", "host", "kernel count", "dispatch")),
)
DIFF_CLASSES = (
    ("tiling", ("tile", "block", "chunk", "split")),
    ("buffering", ("buffer", "double", "pipeline", "prefetch", "async")),
    ("vectorize", ("vector", "simd", "unroll", "pack", "wide")),
    ("layout", ("layout", "stride", "transpose", "pad", "align")),
    ("reduction", ("reduce", "reduction", "accumulate", "split-k", "atomic")),
    ("precision", ("cast", "precision", "fp16", "fp32", "bf16", "quant")),
    ("fusion", ("fuse", "fusion", "inline", "merge")),
)


def _text(row: dict[str, Any], *keys: str) -> str:
    return " ".join(str(row.get(k) or "") for k in keys).lower()


def _class_of(text: str, table: Iterable[tuple[str, tuple[str, ...]]], default: str) -> str:
    for name, words in table:
        if any(word in text for word in words):
            return name
    return default


def case_set(row: dict[str, Any]) -> tuple[str, ...]:
    """The cases an attempt is about: its scope, its failing cases, its measurement keys."""
    scope = row.get("scope") if isinstance(row.get("scope"), dict) else {}
    cases = list(scope.get("cases") or []) or list(row.get("cases") or [])
    if not cases and isinstance(row.get("per_case_us"), dict):
        cases = list(row["per_case_us"])
    return tuple(sorted({str(c) for c in cases}))


def bottleneck_of(row: dict[str, Any]) -> str:
    scope = row.get("scope") if isinstance(row.get("scope"), dict) else {}
    named = str(scope.get("bottleneck") or scope.get("bucket") or "").strip().lower()
    return named or _class_of(_text(row, "claim", "why", "why_failed", "approach", "lesson"), CLASSES, "other")


def diff_class_of(row: dict[str, Any]) -> str:
    scope = row.get("scope") if isinstance(row.get("scope"), dict) else {}
    named = str(scope.get("change") or "").strip().lower()
    return named or _class_of(_text(row, "change_summary", "claim", "approach", "lesson"), DIFF_CLASSES, "other")


def outcome_of(row: dict[str, Any]) -> str:
    if row.get("type") == "failure":
        return "failed"
    if row.get("type") == "hypothesis":
        return str(row.get("status") or "")
    return str(row.get("outcome") or row.get("status") or "")


def fingerprint(row: dict[str, Any]) -> str:
    """Stable short id of an attempt: case set, bottleneck class, diff class, outcome."""
    raw = "|".join([",".join(case_set(row)), bottleneck_of(row), diff_class_of(row), outcome_of(row)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def attempts(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every recorded attempt (experiments and failures, newest last) with its classes."""
    out = []
    for row in view.current(rows):
        if row.get("type") not in ("experiment", "failure"):
            continue
        out.append({"id": row.get("id"), "worker": row.get("worker"), "ref": view.ref(row),
                    "fingerprint": fingerprint(row), "bottleneck": bottleneck_of(row),
                    "diff_class": diff_class_of(row), "outcome": outcome_of(row),
                    "cases": list(case_set(row)), "ts": row.get("ts")})
    out.sort(key=lambda a: (str(a.get("ts") or ""), int(model.seq_of(a.get("id")))))
    return out


def hypothesis_attempts(rows: Iterable[dict[str, Any]], hypothesis_id: str) -> list[dict[str, Any]]:
    """The experiments and failures that cite ``hypothesis_id`` or one of the ids it replaced."""
    rows = list(rows)
    head = view.head(rows, hypothesis_id)
    if head is None:
        return []
    chain = view.chain_ids(rows, head)
    out = []
    for row in rows:
        if row.get("type") not in ("experiment", "failure"):
            continue
        cited = {str(c) for c in (row.get("links") or [])}
        if row.get("hypothesis_id"):
            cited.add(str(row["hypothesis_id"]).split("/")[-1])
        if cited & chain:
            out.append(row)
    return out


def warnings(rows: Iterable[dict[str, Any]], *, limit: int = MAX_WARNINGS) -> list[dict[str, Any]]:
    """Notes to attach to a tool reply: repeated dead ends and a plateau of non-improving experiments."""
    rows = list(rows)
    found: list[dict[str, Any]] = []
    every = attempts(rows)
    by_fp: dict[str, list[dict[str, Any]]] = {}
    by_bottleneck: dict[str, list[dict[str, Any]]] = {}
    for item in every:
        by_fp.setdefault(item["fingerprint"], []).append(item)
        by_bottleneck.setdefault(item["bottleneck"], []).append(item)
    for fp, items in sorted(by_fp.items(), key=lambda kv: -len(kv[1])):
        if len(items) < ATTEMPT_BUDGET:
            continue
        last = items[-1]
        found.append({
            "kind": "attempt_budget",
            "message": (f"{len(items)} attempts share one fingerprint ({last['bottleneck']} / "
                        f"{last['diff_class']} / {last['outcome']}); change the bottleneck class or give a "
                        f"revisit_if reason before repeating it"),
            "ids": [i["ref"] for i in items[-5:]],
        })
        break
    repeated = {items[-1]["bottleneck"] for items in by_fp.values() if len(items) >= ATTEMPT_BUDGET}
    for name, items in sorted(by_bottleneck.items(), key=lambda kv: -len(kv[1])):
        if name == "other" or name in repeated or len(items) < ATTEMPT_BUDGET:
            continue
        found.append({
            "kind": "bottleneck_budget",
            "message": (f"{len(items)} attempts on bottleneck '{name}'; the budget is {ATTEMPT_BUDGET}, so "
                        f"change the bottleneck class or justify a revisit"),
            "ids": [i["ref"] for i in items[-5:]],
        })
        break
    plateau = plateau_run(rows)
    if plateau >= PLATEAU_RUN:
        found.append({
            "kind": "plateau",
            "message": (f"{plateau} experiments without an improvement; change the bottleneck or the method "
                        f"class instead of refining this one"),
            "ids": [i["ref"] for i in every[-5:]],
        })
    return found[:limit]


def plateau_run(rows: Iterable[dict[str, Any]]) -> int:
    """How many of the newest experiments, counting back, failed to improve (research R8: 5 is a plateau)."""
    exps = [r for r in view.current(rows) if r.get("type") == "experiment"]
    exps.sort(key=view.order_key)
    run = 0
    for row in reversed(exps):
        if outcome_of(row) in POSITIVE:
            break
        run += 1
    return run
