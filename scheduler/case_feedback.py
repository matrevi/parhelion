"""Join judge receipts to immutable runs and derive reproducible experiment profiles."""

from __future__ import annotations

import fcntl
import re

from .case_bench import catalog, committed, digest, load_profile, now, read_doc
from .parhelion_paths import atomic_write_json, workspace_bench_specs_dir

FAILURES = {"Wrong Answer": "WA", "Runtime Error": "RE", "Time Limit Exceeded": "TLE"}


def regression_evidence(folder, good):
    fixed = {r["key"] for r in good["cases"] if r["status"] == "PASS"}
    for path in sorted(folder.glob("bench-runs/*/report.json")):
        bad = read_doc(path)
        if (
            not bad.get("built")
            or bad.get("sha") != folder.name
            or bad.get("title") != good["title"]
            or bad.get("scheme") != "reference"
            or bad.get("engine") != good["engine"]
            or bad.get("oracle") != good["oracle"]
            or not bad.get("environment")
            or bad["environment"] != good["environment"]
        ):
            continue
        hits = [
            r["id"]
            for r in bad["cases"]
            if r["key"] in fixed
            and (
                r["status"] in ("WA", "RE")
                or (
                    r["status"] == "TLE"
                    and good["settings"]["timeout"] <= bad["settings"]["timeout"]
                )
            )
        ]
        if hits:
            return {
                "bad_run": bad["run_id"],
                "good_run": good["run_id"],
                "case_ids": hits,
            }
    return {}


def evolve(ws, title, repo, outcomes, apply):
    """Build an opt-in ablation profile. A receipt alone never assigns a failed case."""
    folder = workspace_bench_specs_dir(ws) / title
    head = committed(repo, "HEAD")
    profile = load_profile(ws, title)
    oracle, cases = catalog(repo, head, profile)
    by_key = {c["key"]: c for c in cases}
    seen = {}
    for row in outcomes:
        sid = row.get("submission_id")
        sha = row.get("canonical_sha") or row.get("sha", "")
        if (
            sid
            and re.fullmatch("[0-9a-f]{40}", sha or "")
            and row.get("status") in (*FAILURES, "Pass")
            and row.get("title") == title
        ):
            seen[(str(sid), sha)] = row
    observations, regressions, variants = [], set(), {}
    for (sid, sha), row in seen.items():
        local = []
        for path in sorted(
            (ws / "results" / title / sha).glob("bench-runs/*/report.json")
        ):
            report = read_doc(path)
            if (
                report.get("scheme") != "reference"
                or report.get("sha") != sha
                or report.get("oracle") != oracle
                or not report.get("built")
            ):
                continue
            hits = [
                by_key[r["key"]]["id"]
                for r in report["cases"]
                if r["key"] in by_key and r["status"] == FAILURES.get(row["status"])
            ]
            regressions.update(hits)
            local.append(
                {
                    "run_id": report["run_id"],
                    "summary": report["summary"],
                    "matching_failures": hits,
                }
            )
        obs = {
            "submission_id": sid,
            "sha": sha,
            "status": row["status"],
            "score": row.get("score"),
            "predicted_score": row.get("estimated_score", row.get("predicted_score")),
            "runs": local,
        }
        observations.append(obs)
    # Vary only inputs whose generator exposes a seed. Untested variants stay experimental.
    for case in cases:
        if (
            case["source"] != "official"
            or case["id"] not in regressions
            or not isinstance(case.get("seed"), int)
        ):
            continue
        key = "local/regression-" + case["id"]
        variants[key] = {"id": key, "base": case["id"], "seed": case["seed"] + 1}
    trial = {
        **profile,
        "version": 1,
        "variants": list(profile.get("variants", [])),
        "buckets": dict(profile.get("buckets", {})),
    }
    existing = {v["id"] for v in trial["variants"]}
    trial["variants"].extend(v for k, v in variants.items() if k not in existing)
    if regressions:
        members = set(trial["buckets"].get("observed-regressions", []))
        trial["buckets"]["observed-regressions"] = sorted(members | regressions)
    if variants:
        trial["buckets"]["regression-variants"] = sorted(
            set(trial["buckets"].get("regression-variants", [])) | set(variants)
        )
    catalog(repo, head, trial)
    observations.sort(key=lambda row: (row["submission_id"], row["sha"]))
    version = digest([head, trial, observations])
    dest = folder / "experiments" / version
    if apply:
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / ".evolution.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            dest.mkdir(parents=True, exist_ok=True)
            if not (dest / "feedback.json").exists():
                atomic_write_json(dest / "profile.json", trial)
                atomic_write_json(
                    dest / "feedback.json",
                    {
                        "generated_at": now(),
                        "oracle": oracle,
                        "head": head,
                        "observations": observations,
                    },
                )
    return {
        "title": title,
        "scheme": "reference",
        "applied": apply,
        "receipts": len(observations),
        "matching_cases": sorted(regressions),
        "proposed_variants": sorted(variants),
        "version": version,
        "profile": str(dest / "profile.json"),
        "feedback_file": str(dest / "feedback.json"),
    }
