"""Join judge receipts to immutable runs and derive reproducible experiment profiles."""

from __future__ import annotations

import fcntl
import hashlib
import re
import subprocess
import tempfile
from pathlib import Path

from .case_bench import catalog, committed, digest, load_profile, now, read_doc
from .parhelion_paths import atomic_write_json, workspace_bench_specs_dir

FAILURES = {"Wrong Answer": "WA", "Runtime Error": "RE", "Time Limit Exceeded": "TLE"}


def trial_path(folder, submission_id):
    return folder / ("escape-trial-" + hashlib.sha256(submission_id.encode()).hexdigest() + ".json")


def _jsonl(path):
    import json
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return rows


def trial_spent(folder, good_sha):
    """Whether the repair bound to a receipt was submitted or is waiting in the queue.

    ``folder`` is ``results/<title>/<bad_sha>``. A judge submission ID, or a queued row with no
    outcome yet, spends the trial; a row the gate rejected or an operator skipped does not.
    """
    results, title = folder.parent.parent, folder.parent.name
    match = lambda row: row.get("title") == title and str(row.get("sha") or "").lower() == good_sha.lower()
    outcomes = [row for row in _jsonl(results / "submit-queue-outcome.jsonl") if match(row)]
    if any(row.get("submission_id") for row in outcomes):
        return True
    finished = {row.get("queued_at") for row in outcomes}
    return any(match(row) and row.get("queued_at") not in finished
               for row in _jsonl(results / "submit-queue.jsonl"))


def authorize_review_trial(repo, folder, bad, good, state, reason):
    sid = state.get("escape_submission_id")
    if not (sid and state.get("escape_lock") and state.get("escape_status") == "Wrong Answer"
            and state.get("escape_sha") == folder.name and bad
            and bad["engine"] == good["engine"] and bad["oracle"] == good["oracle"]
            and bad.get("environment") and bad["environment"] == good["environment"]
            and bad["settings"].get("strict_fp32") and good["settings"].get("strict_fp32")):
        return {"ok": False, "reason": "Need the active judge WA and matching full strict runs on both commits"}
    diff = subprocess.run(["git", "-C", str(repo), "diff", "--quiet", folder.name, good["sha"],
                           "--", "kernel.asc", "op_host", "op_kernel"], check=False)
    if diff.returncode != 1:
        return {"ok": False, "reason": "No implementation change between reviewed commits"}
    path = trial_path(folder, sid)
    with (folder / ".escape-trial.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # One reviewed trial per WA receipt: the binding can move to a better repair only while
        # the bound one has not been submitted or queued.
        bound = read_doc(path).get("good_sha") if path.exists() else None
        if bound and bound != good["sha"] and trial_spent(folder, bound):
            return {"ok": False, "reason": "Another repair commit is already bound to this receipt and was submitted"}
        record = {"bad_sha": folder.name, "good_sha": good["sha"], "submission_id": sid,
                  "good_run": good["run_id"], "bad_run": bad["run_id"], "review": reason.strip()}
        if bound and bound != good["sha"]:
            record["replaces"] = bound
        atomic_write_json(path, record)
    return {"ok": True, "cleared": False, "review_trial": True, **record}


def match_escape_submission(repo, bad_sha, problem_id, row):
    """Bind a historical verdict to the submitted source, even after a source-identical revert."""
    from cli.opsb_core.services.auth_service import list_judge_accounts
    from runtime.judge.ict_cli import ICTClient
    from runtime.judge.project_files import extract_package, template_files

    sid = str(row["submission_id"])
    accounts = list_judge_accounts()
    aid = row.get("account_id") or "primary"
    account = next((a for a in accounts if a["id"] == aid), {})
    client = ICTClient(session_file=account.get("session_file") or None)
    if not client.load_session():
        raise ValueError("Sign in to read the failed submission's source")
    detail = client.get_submission(sid)
    if (detail.get("_id") != sid or detail.get("problem_id") != problem_id
            or detail.get("status") != row["status"]):
        raise ValueError("Judge submission identity or verdict differs from history")
    template = client.get_template(problem_id)
    archive = subprocess.check_output(["git", "-C", str(repo), "archive", "--format=zip", bad_sha])
    with tempfile.TemporaryDirectory(prefix="opsb-escape-") as tmp:
        root = extract_package(archive, tmp, {"_id": problem_id}, template)
        payload = client.prepare_submission(problem_id, root)
        if "files" not in payload:
            fields = ("host_cpp", "kernel_cpp", "tiling_h", "tiling_key_h")
            return (bool(payload.get("kernel_cpp")) and
                    all(isinstance(detail.get(k), str) and detail[k] == payload[k] for k in fields))
        expected = {f["path"]: f["content"] for f in payload["files"]}
        official = template_files(template)
        remote = template_files({"files": detail.get("files")})
        if not expected or not expected.keys() <= remote.keys():
            return False
        for name, item in remote.items():
            if name in expected:
                content = expected[name]
            elif name in official and not official[name]["editable"]:
                content = (Path(root) / name).read_text(encoding="utf-8-sig")
            else:
                return False
            if content.replace("\r\n", "\n") != item["content"].replace("\r\n", "\n"):
                return False
    return True


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
            or bool(bad.get("settings", {}).get("strict_fp32")) != bool(good.get("settings", {}).get("strict_fp32"))
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
