"""Platform-owned test suite per problem: case tiers, timing marks and the three levels.

The suite is the measuring stick. Workers choose a level (smoke, standard, final);
they never assemble profiles, timing modes or repeat counts themselves. Cases are the
smallest unit; buckets are labels on cases. A legacy `case-profile.json` (version 1)
is converted in memory with `suite_version` 0 and is never rewritten implicitly.
"""

from __future__ import annotations

import datetime
import fcntl
import json
import os
import re
from pathlib import Path

from .parhelion_paths import atomic_write_json, workspace_bench_specs_dir

LEVELS = ("smoke", "standard", "final")
LEGACY_SUITES = {"adaptive": "smoke", "all": "standard"}
TIERS = {"smoke": 2, "standard": 1, "final": 1}
VARIANT_TIERS = ("standard", "final")
WORKER_FORBIDDEN = ("profile", "experiment", "timing", "repeats", "runner", "bucket")
WORKER_MESSAGE = "Workers select a level (smoke|standard|final); the platform owns the suite"
SUITE_FIELDS = {
    "version", "suite_version", "updated_at", "strict_fp32", "warmup", "repeats",
    "case_timeout", "reuse_session", "variants", "buckets", "timed", "weights", "timing_scope",
}
TIMING_SCOPES = ("official", "perf")
VARIANT_FIELDS = {"id", "base", "params", "seed", "name", "perf", "tier"}
PROFILE_FIELDS = ("version", "variants", "buckets", "timing", "repeats", "case_timeout", "strict_fp32", "reuse_session")
DEFAULTS = {"warmup": 2, "repeats": 5, "case_timeout": 15, "reuse_session": True}
RANGES = {"warmup": (0, 10), "repeats": (1, 50), "case_timeout": (1, 600)}


def is_worker() -> bool:
    return os.getenv("PARHELION_MCP_ROLE") == "worker"


def suite_path(ws, title) -> Path:
    return workspace_bench_specs_dir(ws) / title / "suite.json"


def legacy_path(ws, title) -> Path:
    return workspace_bench_specs_dir(ws) / title / "case-profile.json"


def _read(path: Path) -> dict:
    doc = json.loads(Path(path).read_text())
    if not isinstance(doc, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return doc


def _strict_default(ws) -> bool:
    try:
        value = _read(Path(ws) / "opsb.workspace.json").get("bench_strict_fp32", False)
    except (OSError, ValueError):
        return False
    if not isinstance(value, bool):
        raise ValueError("strict_fp32 must be true or false")
    return value


def _integer(doc, field):
    value = doc.get(field, DEFAULTS[field])
    low, high = RANGES[field]
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"suite.{field} must be an integer in {low}..{high}")
    return value


def _id_list(value, field):
    if not isinstance(value, list) or not all(isinstance(k, str) and k for k in value):
        raise ValueError(f"suite.{field} must be a list of case IDs")
    return list(dict.fromkeys(value))


def normalize_variant(variant, index=0) -> dict:
    where = f"suite.variants[{index}]"
    if not isinstance(variant, dict):
        raise ValueError(f"{where} must be an object")
    unknown = set(variant) - VARIANT_FIELDS
    if unknown:
        raise ValueError(f"{where}.{sorted(unknown)[0]} is not a variant field")
    key = variant.get("id")
    if not isinstance(key, str) or not re.fullmatch(r"local/[\w.-]+", key):
        raise ValueError(f"{where}.id must look like local/<name>")
    if not isinstance(variant.get("base"), str) or not variant["base"]:
        raise ValueError(f"{where}.base must name an official case")
    if "params" in variant and not isinstance(variant["params"], dict):
        raise ValueError(f"{where}.params must be an object")
    if "seed" in variant and type(variant["seed"]) is not int:
        raise ValueError(f"{where}.seed must be an integer")
    if "name" in variant and not isinstance(variant["name"], str):
        raise ValueError(f"{where}.name must be a string")
    if "perf" in variant and not isinstance(variant["perf"], bool):
        raise ValueError(f"{where}.perf must be true or false")
    tier = variant.get("tier", "final")
    if tier not in VARIANT_TIERS:
        raise ValueError(f"{where}.tier must be standard or final")
    return {**variant, "tier": tier}


def normalize(doc, *, strict_default=False) -> dict:
    """Validate a version 2 suite document and fill every field."""
    if not isinstance(doc, dict):
        raise ValueError("suite must be a JSON object")
    unknown = set(doc) - SUITE_FIELDS
    if unknown:
        raise ValueError(f"suite.{sorted(unknown)[0]} is not a suite field")
    if doc.get("version") != 2:
        raise ValueError("suite.version must be 2")
    version = doc.get("suite_version", 0)
    if type(version) is not int or version < 0:
        raise ValueError("suite.suite_version must be a non-negative integer")
    updated = doc.get("updated_at")
    if updated is not None and not isinstance(updated, str):
        raise ValueError("suite.updated_at must be an ISO-8601 string")
    strict = doc.get("strict_fp32", strict_default)
    if not isinstance(strict, bool):
        raise ValueError("suite.strict_fp32 must be true or false")
    reuse = doc.get("reuse_session", True)
    if not isinstance(reuse, bool):
        raise ValueError("suite.reuse_session must be true or false")
    variants = doc.get("variants", [])
    if not isinstance(variants, list):
        raise ValueError("suite.variants must be a list")
    variants = [normalize_variant(v, i) for i, v in enumerate(variants)]
    if len({v["id"] for v in variants}) != len(variants):
        raise ValueError("suite.variants contains a duplicate id")
    buckets = doc.get("buckets", {})
    if not isinstance(buckets, dict) or not all(isinstance(k, str) and k for k in buckets):
        raise ValueError("suite.buckets must map labels to case ID lists")
    buckets = {label: _id_list(ids, f"buckets.{label}") for label, ids in buckets.items()}
    weights = doc.get("weights", {})
    if not isinstance(weights, dict) or not all(
        isinstance(k, str) and type(w) in (int, float) and w >= 0 for k, w in weights.items()
    ):
        raise ValueError("suite.weights must map case IDs to non-negative numbers")
    return {
        "version": 2,
        "suite_version": version,
        "updated_at": updated,
        "strict_fp32": strict,
        "warmup": _integer(doc, "warmup"),
        "repeats": _integer(doc, "repeats"),
        "case_timeout": _integer(doc, "case_timeout"),
        "reuse_session": reuse,
        "variants": variants,
        "buckets": buckets,
        "timed": _id_list(doc.get("timed", []), "timed"),
        "weights": {k: float(w) for k, w in weights.items()},
        "timing_scope": _timing_scope(doc),
    }


def from_profile(profile, *, strict_default=False) -> dict:
    """Convert a legacy version 1 bench profile; its numeric limits are clamped to the suite ranges."""
    if not isinstance(profile, dict) or profile.get("version", 1) != 1:
        raise ValueError("Unsupported bench profile version")
    unknown = set(profile) - set(PROFILE_FIELDS)
    if unknown:
        raise ValueError(f"profile.{sorted(unknown)[0]} is not a profile field")
    doc = {"version": 2, "suite_version": 0}
    for key in ("strict_fp32", "reuse_session", "variants", "buckets"):
        if key in profile:
            doc[key] = profile[key]
    for key in ("repeats", "case_timeout"):
        if key in profile:
            value = profile[key]
            low, high = RANGES[key]
            doc[key] = min(max(value, low), high) if type(value) is int else value
    return normalize(doc, strict_default=strict_default)


def load_document(doc, *, strict_default=False) -> dict:
    """Normalize either suite format: version 2 directly, version 1 through `from_profile`."""
    if isinstance(doc, dict) and doc.get("version") == 2:
        return normalize(doc, strict_default=strict_default)
    return from_profile(doc, strict_default=strict_default)


def load_suite(ws, title, path=None) -> dict:
    """Return the normalized suite: an explicit file, `suite.json`, the legacy profile, or the empty suite."""
    strict = _strict_default(ws)
    if path:
        return load_document(_read(Path(path)), strict_default=strict)
    current = suite_path(ws, title)
    if current.exists():
        return normalize(_read(current), strict_default=strict)
    legacy = legacy_path(ws, title)
    if legacy.exists():
        return from_profile(_read(legacy), strict_default=strict)
    return normalize({"version": 2}, strict_default=strict)


def to_profile(suite) -> dict:
    """Project the suite onto the profile shape accepted by `case_bench.catalog`."""
    return {
        "version": 1,
        "variants": [dict(v) for v in suite["variants"]],
        "buckets": {k: list(v) for k, v in suite["buckets"].items()},
        "timing": "perf",
        "repeats": suite["repeats"],
        "case_timeout": suite["case_timeout"],
        "strict_fp32": suite["strict_fp32"],
        "reuse_session": suite["reuse_session"],
    }


def requested_level(level=None, suite=None):
    """Resolve `level`, falling back to the deprecated `suite` values; None when neither is set."""
    for value in (level, suite):
        if value is None:
            continue
        value = LEGACY_SUITES.get(value, value)
        if value not in LEVELS:
            raise ValueError(f"Unknown level: {value}")
        return value
    return None


def _timing_scope(doc):
    scope = doc.get("timing_scope", "official")
    if scope not in TIMING_SCOPES:
        raise ValueError("suite.timing_scope must be official or perf")
    return scope


def timed_ids(cases, suite) -> set:
    """Cases whose device time is measured.

    The judge times and scores every official case, so by default every official case is
    timed (scope ``official``), together with performance-marked cases and the suite's
    explicit list. Scope ``perf`` restricts timing to the marked and listed cases; with
    nothing marked it still falls back to every official case.
    """
    known = {c["id"] for c in cases}
    official = {c["id"] for c in cases if c["source"] == "official"}
    timed = {c["id"] for c in cases if c.get("perf")} | (set(suite.get("timed", [])) & known)
    if suite.get("timing_scope", "official") == "official":
        return timed | official
    return timed or official


def in_level(case, level) -> bool:
    if level == "final":
        return True
    return case["source"] == "official" or case.get("tier", "final") == "standard"


def select(cases, suite, level, ids=None, folder=None):
    """Return `(selected, selection_label, formal)` for a level or an explicit ID list."""
    if ids:
        unknown = set(ids) - {c["id"] for c in cases}
        if unknown:
            raise ValueError("Unknown case ID for this commit")
        return [c for c in cases if c["id"] in ids], "explicit", False
    if level == "smoke":
        from .case_bench import adaptive_cases

        selected, label = adaptive_cases(folder, cases, timed_ids(cases, suite))
        return selected, label, False
    if level not in ("standard", "final"):
        raise ValueError(f"Unknown level: {level}")
    return [c for c in cases if in_level(c, level)], level, True


def protocol(suite, level) -> dict:
    """Timing protocol and device priority of a level."""
    if level == "smoke":
        return {"warmup": 1, "repeats": 3, "timeout": min(suite["case_timeout"], 8), "tier": TIERS[level]}
    if level not in ("standard", "final"):
        raise ValueError(f"Unknown level: {level}")
    repeats = max(suite["repeats"], 7) if level == "final" else suite["repeats"]
    return {"warmup": suite["warmup"], "repeats": repeats, "timeout": suite["case_timeout"], "tier": TIERS[level]}


def write_suite(ws, title, suite) -> dict:
    """Validate and atomically write `suite.json`; the written version is one above the newest seen."""
    path = suite_path(ws, title)
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / ".suite.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        doc = normalize(suite, strict_default=bool(suite.get("strict_fp32", False)))
        current = 0
        if path.exists():
            try:
                current = int(_read(path).get("suite_version", 0))
            except (OSError, ValueError, TypeError):
                current = 0
        doc["suite_version"] = max(doc["suite_version"], current) + 1
        doc["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        atomic_write_json(path, doc)
    return doc


def describe(ws, title, repo=None, rev="HEAD") -> dict:
    """The normalized suite with each case's level membership and timing mark."""
    suite = load_suite(ws, title)
    doc = {
        "title": title,
        "path": str(suite_path(ws, title)),
        "source": "suite" if suite_path(ws, title).exists() else
                  "legacy" if legacy_path(ws, title).exists() else "empty",
        "suite": suite,
        "cases": None,
    }
    if repo is None:
        return doc
    from .case_bench import catalog, committed

    _, cases = catalog(repo, committed(repo, rev), to_profile(suite))
    timed = timed_ids(cases, suite)
    doc["cases"] = [
        {
            "id": c["id"],
            "source": c["source"],
            "levels": [lv for lv in ("standard", "final") if in_level(c, lv)],
            "timed": c["id"] in timed,
            "weight": suite["weights"].get(c["id"], 1.0 if c["source"] == "official" else 0.0),
            "buckets": c["buckets"],
        }
        for c in cases
    ]
    return doc


def migrate(ws, title) -> dict:
    """Write `suite.json` (version 1) from the legacy profile; the legacy file is left untouched."""
    if suite_path(ws, title).exists():
        raise ValueError(f"{suite_path(ws, title)} already exists")
    legacy = legacy_path(ws, title)
    suite = from_profile(_read(legacy), strict_default=_strict_default(ws)) if legacy.exists() else \
        normalize({"version": 2}, strict_default=_strict_default(ws))
    return write_suite(ws, title, {**suite, "suite_version": 0})


def add_variant(ws, title, variant, repo=None, rev="HEAD") -> dict:
    """Append one validated local variant and bump the suite version."""
    suite = load_suite(ws, title)
    entry = normalize_variant(variant, len(suite["variants"]))
    if any(v["id"] == entry["id"] for v in suite["variants"]):
        raise ValueError(f"Variant {entry['id']} already exists")
    trial = {**suite, "variants": [*suite["variants"], entry]}
    if repo is not None:
        from .case_bench import catalog, committed

        catalog(repo, committed(repo, rev), to_profile(trial))
    return write_suite(ws, title, trial)


def manage(ws, title, op, variant_json=None, repo=None) -> dict:
    """Entry point of `opsb bench suite <show|migrate|add-variant>`."""
    if is_worker():
        raise PermissionError("Workers cannot edit or inspect the suite; the platform owns it")
    if op == "show":
        return describe(ws, title, repo)
    if op == "migrate":
        return {"title": title, "path": str(suite_path(ws, title)), "suite": migrate(ws, title)}
    if op == "add-variant":
        if not variant_json:
            raise ValueError("add-variant requires --json '<variant>'")
        try:
            variant = json.loads(variant_json)
        except ValueError as exc:
            raise ValueError(f"--json is not valid JSON: {exc}") from exc
        return {"title": title, "path": str(suite_path(ws, title)),
                "suite": add_variant(ws, title, variant, repo)}
    raise ValueError(f"Unknown suite operation: {op}")
