"""Versioned reference cases, benchmark profiles and immutable per-run evidence."""

from __future__ import annotations

import argparse
import ast
import datetime
import fcntl
import hashlib
import json
import math
import os
import re
import shlex
import statistics
import subprocess
import sys
import tempfile
import uuid
from collections import Counter
from contextlib import ExitStack, contextmanager
from pathlib import Path

from . import case_map, harness_check, hidden_final, pairing, plausibility, receipts, verdicts
from . import suite as suites
from . import paired_timing, timing
from .parhelion_paths import (
    atomic_write_json,
    atomic_write_text,
    load_problem_manifest,
    workspace_bench_specs_dir,
)

RUNNER = Path(__file__).resolve().parents[1] / "runtime/npu_runtime/reference_cases.py"
TIMING_LISTS = ("event_us", "event_cold_us", "profiler_us", "profiler_cold_us")
STATUSES = {"PASS", "WA", "RE", "TLE", "ERROR", "SKIP", "NOT_RUN"}


def _dev(key: str, default: str = "") -> str:
    """Deployment setting from `~/.parhelion/config.json` ("device").

    `scheduler` may be imported without the CLI package on `sys.path`, so the
    accessor is imported lazily and a missing CLI falls back to the neutral default.
    """
    try:
        from cli.opsb_core.device_env import device_text
        return device_text(key) or default
    except Exception:
        return default


def profiler_settings(level):
    """Device-profiler task timing for this level (device.profiler_levels), else None."""
    levels = {x.strip() for x in (_dev("profiler_levels") or "standard,final").split(",") if x.strip()}
    if level not in levels:
        return None
    return {"bin": _dev("profiler_bin") or "profiler", "acl_flag": _dev("profiler_acl_flag") or "--acl"}


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def committed(repo, rev, name=None):
    args = (
        ["show", f"{rev}:{name}"]
        if name
        else ["rev-parse", "--verify", "--end-of-options", rev + "^{commit}"]
    )
    return subprocess.check_output(
        ["git", "-C", repo, *args], text=True, stderr=subprocess.DEVNULL
    ).strip()


def has_reference(repo, rev=None):
    if rev:
        try:
            return bool(
                re.search(
                    r"^CASES\s*=", committed(repo, rev, "reference.py"), re.MULTILINE
                )
            )
        except subprocess.CalledProcessError:
            return False
    path = repo / "reference.py"
    return (
        (repo / "main.asc").is_file()
        and path.is_file()
        and bool(
            re.search(r"^CASES\s*=", path.read_text(errors="replace"), re.MULTILINE)
        )
    )


def catalog(repo, sha, profile):
    ref, sample, main = [
        committed(repo, sha, n) for n in ("reference.py", "sample.py", "main.asc")
    ]
    oracle = digest([ref, sample, main])
    tree = ast.parse(ref)
    definitions = {
        t.id: n.value
        for n in tree.body
        if isinstance(n, ast.Assign)
        for t in n.targets
        if isinstance(t, ast.Name)
    }
    perf = (
        ast.literal_eval(definitions["PERF_CASES"])
        if "PERF_CASES" in definitions
        else []
    )
    single = (
        ast.literal_eval(definitions["PERF_CASE_ID"])
        if "PERF_CASE_ID" in definitions
        else None
    )
    rows = []
    for node in definitions["CASES"].elts:
        fields = {k.value: v for k, v in zip(node.keys, node.values)}
        key = ast.literal_eval(fields["id"])
        if not isinstance(key, str) or not key or key.startswith("local/"):
            raise ValueError("Unsupported reference case ID")
        row = {
            "id": key,
            "base": key,
            "name": ast.literal_eval(fields.get("name", fields["id"])),
            "source": "official",
            "perf": key in perf or key == single,
        }
        for field in ("seed", "params", "dtype"):
            if field in fields:
                try:
                    row[field] = ast.literal_eval(fields[field])
                except (ValueError, TypeError):
                    pass  # Computed defaults are pinned by the oracle digest.
        row["perf"] |= "性能" in row["name"] or str(
            row.get("params", {}).get("mode", "")
        ).startswith("perf")
        rows.append(row)
    by_id = {c["id"]: c for c in rows}
    if not rows or len(by_id) != len(rows):
        raise ValueError("Empty or duplicate official case IDs")
    if (
        set(profile)
        - {"version", "variants", "buckets", "timing", "repeats", "case_timeout", "strict_fp32", "reuse_session"}
        or profile.get("version", 1) != 1
    ):
        raise ValueError("Unsupported bench profile fields or version")
    for variant in profile.get("variants", []):
        key = variant["id"]
        if (
            not re.fullmatch(r"local/[\w.-]+", key)
            or key in by_id
            or variant["base"] not in by_id
            or by_id[variant["base"]]["source"] != "official"
            or set(variant) - suites.VARIANT_FIELDS
            or variant.get("tier", "final") not in suites.VARIANT_TIERS
        ):
            raise ValueError(f"Invalid local variant: {key}")
        row = {**by_id[variant["base"]], **variant, "source": "local"}
        row["params"] = {
            **by_id[variant["base"]].get("params", {}),
            **variant.get("params", {}),
        }
        rows.append(row)
        by_id[key] = row
    buckets = profile.get("buckets", {})
    for label, ids in buckets.items():
        if not label or not isinstance(ids, list) or set(ids) - by_id.keys():
            raise ValueError(f"Unknown case or invalid membership in bucket {label}")
    for row in rows:
        # A platform value transform (scheduler.hidden_final) changes the input; official keys
        # and every earlier key stay as they were.
        row["key"] = digest(
            [oracle, row["base"], row.get("params", {}), row.get("seed")]
            + ([row["transform"]] if row.get("transform") else [])
        )
        labels = [row["source"]]
        if row["perf"]:
            labels.append("performance")
        row["buckets"] = sorted(
            set(labels + [b for b, ids in buckets.items() if row["id"] in ids])
        )
    if len({c["key"] for c in rows}) != len(rows):
        raise ValueError(
            "Two case IDs describe the same input; use overlapping buckets instead"
        )
    return oracle, rows


def load_profile(ws, title, path=None):
    """Catalog profile of the platform suite, of an explicit file, or of the legacy profile."""
    if not path and suites.suite_path(ws, title).exists():
        return suites.to_profile(suites.load_suite(ws, title))
    if path and read_doc(Path(path)).get("version") == 2:
        return suites.to_profile(suites.load_suite(ws, title, path))
    file = (
        Path(path)
        if path
        else workspace_bench_specs_dir(ws) / title / "case-profile.json"
    )
    profile = json.loads(file.read_text()) if path or file.exists() else {}
    strict = profile.get("strict_fp32", read_doc(ws / "opsb.workspace.json").get("bench_strict_fp32", False))
    if not isinstance(strict, bool):
        raise ValueError("strict_fp32 must be true or false")
    if strict:
        profile["strict_fp32"] = True
    return profile


# The source archive contains exactly the named commit. The remote process owns and
# removes its scratch directory; even concurrent runs of the same SHA never share files.
BOOTSTRAP = """
import json, pathlib, subprocess, sys, tarfile, tempfile
cfg = json.loads(sys.stdin.buffer.readline())
tmp_parent = cfg.get('case_tmp_dir') or None
if tmp_parent:
    pathlib.Path(tmp_parent).mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix='opsb-case-', dir=tmp_parent) as temp:
    root = pathlib.Path(temp).resolve()
    repo = root/'repo'
    repo.mkdir()
    # A paired run ships both commits, under cand/ (the candidate) and base/ (the baseline).
    trees = {'cand/': repo, 'base/': root/'base'} if cfg.get('compare') else {'': repo}
    with tarfile.open(fileobj=sys.stdin.buffer,mode='r|gz') as archive:
        for member in archive:
            prefix = next((p for p in trees if member.name.startswith(p) or member.name == p.rstrip('/')), None)
            if prefix is None:
                raise ValueError('Unsupported archive member: '+member.name)
            target = trees[prefix]
            target.mkdir(exist_ok=True)
            member.name = member.name[len(prefix):]
            if not member.name:
                continue  # the prefix directory itself
            dest = (target/member.name).resolve()
            if not dest.is_relative_to(target) or not (member.isdir() or member.isfile()):
                raise ValueError('Unsupported archive member: '+member.name)
            archive.extract(member,target)
    engine = root/'reference_cases.py'
    engine.write_text(cfg.pop('engine'))
    for name in ('lanes','case_batch','case_cache','device_broker','profiler_capture','case_pairs','early_abort'):
        source = cfg.pop(name, None)
        if source is not None:
            (root/(name+'.py')).write_text(source)
    (root/'config.json').write_text(json.dumps(cfg))
    sys.exit(subprocess.call([sys.executable,'-u',str(engine)],cwd=repo))
"""


def summarize(rows):
    counts = Counter(r["status"] for r in rows)
    return {
        "total": len(rows),
        "passed": counts["PASS"],
        "counts": dict(counts),
        "verified": bool(rows) and counts["PASS"] == len(rows),
    }


@contextmanager
def active_bench(folder, key, run_id, *, remeasure=False, limit_experiments=False):
    """Share identical evidence and admit one worker experiment per problem at a time."""
    runs = folder / "bench-runs"
    runs.mkdir(parents=True, exist_ok=True)
    with ExitStack() as lifetime:
        with (runs / (".active-" + key)).open("a+") as admission:
            fcntl.flock(admission, fcntl.LOCK_EX)
            admission.seek(0)
            prior = admission.read().strip()
            waiter = None
            if re.fullmatch(r"[a-f0-9]{32}", prior) and (runs / prior).is_dir():
                candidate = (runs / prior / ".owner").open("a")
                try:
                    fcntl.flock(candidate, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    waiter = candidate
                else:
                    candidate.close()
                    report = read_doc(runs / prior / "report.json")
                    if not remeasure and reusable_report(report, prior, folder):
                        yield {**report, "reused": True}
                        return
            if waiter is None:
                if limit_experiments:
                    seat = lifetime.enter_context((folder.parent / ".worker-experiment.lock").open("a+"))
                    try:
                        fcntl.flock(seat, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        seat.seek(0)
                        yield {"deferred": True, "exit_code": 75, "active_run": seat.read().strip(),
                               "reason": "An experiment is already admitted for this problem. Use its result before scheduling another."}
                        return
                    seat.seek(0)
                    seat.truncate()
                    seat.write(f"{folder.name}/{run_id}")
                    seat.flush()
                dest = runs / run_id
                dest.mkdir()
                owner = lifetime.enter_context((dest / ".owner").open("a"))
                fcntl.flock(owner, fcntl.LOCK_EX)
                admission.seek(0)
                admission.truncate()
                admission.write(run_id)
                admission.flush()
        if waiter is None:
            yield None
        else:
            with waiter:
                fcntl.flock(waiter, fcntl.LOCK_SH)
                report = read_doc(runs / prior / "report.json")
                if not report:
                    raise RuntimeError(f"Shared bench {prior} ended without a report. Retry validation.")
                yield {**report, "joined": True}


def reusable_report(report, run_id, folder):
    if (report.get("run_id") != run_id or report.get("exit_code") != 0 or
            not report.get("summary", {}).get("verified") or not report.get("built")):
        return False
    if report.get("formal") and read_doc(folder / "result.json").get("run_id") != run_id:
        return False
    try:
        finished = datetime.datetime.fromisoformat(report["finished_at"])
        age = (datetime.datetime.now(datetime.timezone.utc) - finished).total_seconds()
    except (KeyError, TypeError, ValueError):
        return False
    return 0 <= age <= 1800


def adaptive_cases(folder, cases, timed=None):
    """Retest a known failure first; otherwise probe one correctness anchor and one timed case."""
    by_id = {case["id"]: case for case in cases}
    timed = {c["id"] for c in cases if c["perf"]} if timed is None else set(timed)
    # Profiles can change the input behind a local ID. Only reuse failure identity
    # when the catalog also matches, and let a later pass supersede that failure.
    seen = set()
    reports = sorted((folder / "bench-runs").glob("*/report.json"),
                     key=lambda p: p.stat().st_mtime_ns, reverse=True) if folder else []
    for path in reports:
        report = read_doc(path)
        keys = {c["id"]: c.get("key") for c in report.get("catalog", [])}
        for result in report.get("cases", []):
            cid = result.get("id")
            if cid not in by_id or cid in seen or keys.get(cid) != by_id[cid]["key"]:
                continue
            if result.get("status") in ("TLE", "WA", "RE"):
                return [by_id[cid]], "retest_failure"
            if result.get("status") == "PASS":
                seen.add(cid)
    anchor = next((c for c in cases if c["source"] == "official" and c["id"] not in timed),
                  next((c for c in cases if c["source"] == "official"), cases[0]))
    # Prefer a performance-marked case for the timed probe; any other timed case otherwise.
    perf = next((c for c in cases if c.get("perf") and c["id"] in timed and c["id"] != anchor["id"]),
                next((c for c in cases if c["id"] in timed and c["id"] != anchor["id"]), None))
    return [anchor] + ([perf] if perf else []), "anchor_and_performance"


def run(
    ws,
    title,
    repo,
    rev,
    host,
    *,
    ids=None,
    bucket=None,
    profile_path=None,
    experiment=None,
    timing=None,
    repeats=None,
    official_only=None,
    wspt_base=1.0,
    aging_tau=600.0,
    runner=None,
    remeasure=False,
    worker_request=False,
    suite=None,
    level=None,
    dry_run=False,
    stream=True,
    compare=None,
    cold=False,
):
    """Run one level of the platform suite (or a custom experiment) as one device job.

    ``compare`` names a baseline commit: the run becomes an A/B paired experiment in which the
    two builds alternate within the job (runtime/npu_runtime/case_pairs.py, scheduler.paired_timing).

    `level` is smoke, standard or final; None means standard, except that a custom
    experiment without a level keeps the legacy targeted probe. The deprecated `suite`
    maps adaptive to smoke and all to standard, and also accepts the level names.
    `cold` runs every case in a fresh device session; a formal run of a SHA with an
    unconfirmed implausible reading (scheduler.plausibility) runs cold on its own.
    """
    sha = committed(repo, rev or "HEAD")
    requested = suites.requested_level(level, suite)
    custom = any(v is not None for v in (profile_path, experiment, timing, repeats, runner, bucket, official_only))
    if custom and worker_request:
        raise ValueError(suites.WORKER_MESSAGE)
    spec = suites.load_suite(ws, title, profile_path)
    profile = suites.to_profile(spec)
    oracle, cases = catalog(repo, sha, profile)
    if compare == "board":
        from .receipt_predict import board_sha
        compare = board_sha(ws, title)
        if not compare:
            raise ValueError("No judged board SHA to compare against yet")
    base_sha = committed(repo, compare) if compare else None
    if base_sha == sha:
        raise ValueError("compare needs a baseline commit other than the candidate")
    if base_sha and catalog(repo, base_sha, profile)[0] != oracle:
        raise ValueError("A paired run needs the same frozen oracle in both commits")
    if ids and set(ids) - {r["id"] for r in cases}:
        raise ValueError("Unknown case ID for this commit")
    if bucket and not any(bucket in r["buckets"] for r in cases):
        raise ValueError(f"Unknown bucket: {bucket}")
    folder = ws / "results" / title / sha
    base = requested or ("smoke" if custom else "standard")
    run_id = uuid.uuid4().hex
    hidden = []
    if base == "final" and not custom and not ids:
        # Unpublished inputs for this run only (scheduler.hidden_final); suite.json is untouched.
        spec_hidden, hidden = hidden_final.derive(spec, cases, run_id, hidden_final.recent_reports(ws, title))
        if hidden:
            oracle, cases = catalog(repo, sha, suites.to_profile(spec_hidden))
    protected = harness_check.protected_changes(repo, sha)
    if ids or bucket or official_only:
        selected = [
            r
            for r in cases
            if (not ids or r["id"] in ids)
            and (not bucket or bucket in r["buckets"])
            and (not official_only or r["source"] == "official")
        ]
        selection, formal = "explicit", False
    else:
        selected, selection, formal = suites.select(cases, spec, base, folder=folder)
    if not selected:
        raise ValueError("Empty case selection")
    formal = formal and not custom and not base_sha and not protected  # paired evidence stays apart
    cold = bool(cold) or (formal and plausibility.pending(list(case_map._reports(ws, title, sha))))
    probe = selection not in ("explicit", "standard", "final")
    # An explicit case list measures like standard; it keeps the requested level's device tier.
    proto = suites.protocol(spec, "standard" if selection == "explicit" else base)
    proto["tier"] = suites.TIERS[base]
    if custom:
        if timing is None:
            timing = read_doc(Path(profile_path)).get("timing", "perf") if profile_path else "perf"
        proto["repeats"] = repeats if repeats is not None else proto["repeats"]
        proto["timeout"] = min(spec["case_timeout"], 8 if probe else 60)
        if (timing not in ("off", "perf", "all") or type(proto["repeats"]) is not int
                or not 1 <= proto["repeats"] <= 100):
            raise ValueError("Invalid timing, repeats or case_timeout")
    if timing == "off":
        timed = set()
    elif timing == "all":
        timed = {c["id"] for c in cases}
    else:
        timed = suites.timed_ids(cases, spec)
    level_label = "custom" if custom else base
    cfg = {
        "reuse_session": spec["reuse_session"] and not cold,
        "title": title,
        "sha": sha,
        "jobs": os.getenv("OPSB_BUILD_JOBS"),
        "wspt_base": wspt_base,
        "aging_tau": aging_tau,
        "selected": [{**c, "timed": c["id"] in timed} for c in selected],
        "strict_fp32": spec["strict_fp32"],
        # Each selected case carries its own `timed` flag; the device side reads it first.
        "timing": "perf",
        "warmup": proto["warmup"],
        "repeats": proto["repeats"],
        "timeout": proto["timeout"],
        "tier": proto["tier"],
        "job_timeout": min(proto["timeout"] * len(selected), 1800),
        "arch": os.getenv("NPU_ARCH", _dev("arch")),
        "device": os.getenv("OPSB_DEVICE_ID", "0"),
        "lock": _dev("work_root") + "/" + _dev("work_prefix") + "-device0.lock",
        "case_cache_bytes": _dev("case_cache_bytes") or None,  # global LRU budget on the device host
        # Device-host directories for the case cache and its scratch; empty keeps the runtime
        # defaults. They must share a filesystem for hard links (case_cache copies otherwise).
        "case_cache_dir": _dev("case_cache_dir") or None,
        "case_tmp_dir": _dev("case_tmp_dir") or None,
        "profiler": profiler_settings(base),
        "merge_batches": _dev("merge_batches") != "off",  # share one lock hold with other problems
    }
    if base == "standard" and not custom and not base_sha:
        cfg["early_abort"] = early_abort_config(ws, title, sha, cases)
    if base_sha:
        cfg["compare"] = base_sha  # rounds = repeats; each round runs every case once per arm
        # The device profiles one process at a time: with both arms' sessions alive only the
        # first would be profiled (P10-timing.md), so a paired run compares event timings.
        cfg["profiler"] = None
        experiment = experiment or f"paired:{base_sha[:7]}"
    engine = (Path(runner) if runner else RUNNER).read_text()
    modules = {name: RUNNER.with_name(name + ".py").read_text()
               for name in ("lanes", "case_batch", "case_cache", "early_abort")}
    for optional in ("device_broker", "profiler_capture", "case_pairs"):
        if RUNNER.with_name(optional + ".py").is_file():
            modules[optional] = RUNNER.with_name(optional + ".py").read_text()
    cfg["run_id"] = run_id
    dest = folder / "bench-runs" / run_id
    report = {
        # 2: per-case timing_source with event_* and profiler_* samples and minima (4.1 route #1).
        "version": 2,
        "scheme": "custom" if runner else "reference",
        "title": title,
        "sha": sha,
        "run_id": run_id,
        "experiment": experiment,
        "level": level_label,
        "suite_version": spec["suite_version"],
        "pairing_key": pairing.official_key(cases) or f"suite-v{spec['suite_version']}",
        "formal": formal,
        "oracle": oracle,
        "engine": digest([engine, modules]),
        "profile": profile,
        "weights": spec["weights"],
        "catalog": cases,
        "selected": [c["id"] for c in selected],
        "timed": [c["id"] for c in selected if c["id"] in timed],
        "selection": selection,
        "started_at": now(),
        "host": host,
        "settings": {k: v for k, v in cfg.items() if k != "selected"},
    }
    if base_sha:
        report["compare"] = {"base_sha": base_sha, "rounds": cfg["repeats"]}
    if hidden:
        report["hidden_variants"] = len(hidden)
    if protected:
        report["harness"] = {"protected_changed": protected, "detail": harness_check.describe(protected)}
    if dry_run:
        return {**report, "dry_run": True, "exit_code": 0}
    # Identical requests share one measurement; this run's hidden variants do not make it unique.
    shared_cfg = {**cfg, "selected": [c for c in cfg["selected"] if not hidden_final.is_hidden(c)]}
    key = digest([host, engine, profile, formal, oracle,
                  {k: v for k, v in shared_cfg.items() if k not in ("run_id", "wspt_base", "aging_tau")},
                  os.getenv("OPSB_TOOLCHAIN_ENV"), modules, level_label, base, spec["suite_version"]])
    with active_bench(folder, key, run_id, remeasure=remeasure,
                      limit_experiments=worker_request and not formal) as shared:
        if shared is None:
            return _execute_run(repo, cfg, report, engine, modules, runner, dest, stream)
    if shared.get("deferred"):
        return {"run_id": None, "level": level_label, "formal": formal, "summary": None,
                "digest": None, "report": None, **shared}
    path = folder / "bench-runs" / shared["run_id"] / "report.json"
    announce(shared["run_id"], path.parent, shared.get("level", level_label), len(shared.get("selected", [])))
    return {**shared, "report": str(path)}


def write_archive(out, repo, sha, base_sha=None):
    """``git archive`` of ``sha``; a paired run also carries ``base_sha``, both under a prefix."""
    if not base_sha:
        subprocess.run(["git", "-C", repo, "archive", "--format=tar.gz", sha], stdout=out, check=True)
        return
    import tarfile
    with tarfile.open(fileobj=out, mode="w|gz") as merged:
        for prefix, rev in (("cand/", sha), ("base/", base_sha)):
            tree = subprocess.run(["git", "-C", repo, "archive", "--format=tar", f"--prefix={prefix}", rev],
                                  stdout=subprocess.PIPE, check=True).stdout
            with tarfile.open(fileobj=__import__("io").BytesIO(tree), mode="r:") as part:
                for member in part:
                    merged.addfile(member, part.extractfile(member) if member.isfile() else None)


def announce(run_id, folder, level, selected):
    """The `OPSB_RUN_STARTED` line of the `opsb test` stdout protocol."""
    print("OPSB_RUN_STARTED " + json.dumps(
        {"run_id": run_id, "dir": str(Path(folder).resolve()), "level": level, "selected": selected}),
        flush=True)


def _execute_run(repo, cfg, report, engine, modules, runner, dest, stream):
    folder = dest.parent.parent
    run_id, sha, host = report["run_id"], report["sha"], report["host"]
    cases, formal, experiment = report["catalog"], report["formal"], report["experiment"]
    if runner:
        (dest / "runner.py").write_text(engine)
    rows = {r["id"]: {**r, "status": "NOT_RUN"} for r in cases}
    built, completed = False, set()
    rc = 1
    atomic_write_json(dest / "plan.json", report)
    announce(run_id, dest, report.get("level"), len(report["selected"]))
    with (
        (dest / "validation.log").open("w") as log,
        (dest / "events.jsonl").open("w") as events,
    ):

        def event(kind, **fields):
            events.write(
                json.dumps(
                    dict(at=now(), run_id=run_id, event=kind, **fields), allow_nan=False
                )
                + "\n"
            )
            events.flush()

        event("launch")
        try:
            env_script = os.getenv(
                "OPSB_TOOLCHAIN_ENV", _dev("toolchain_dir") + "/set_env.sh"
            )
            shell = (
                "source "
                + shlex.quote(env_script)
                + " >/dev/null && exec python3 -u -c "
                + shlex.quote(BOOTSTRAP)
            )
            with tempfile.TemporaryFile() as payload:
                payload.write((json.dumps({**cfg, "engine": engine, **modules}) + "\n").encode())
                payload.flush()
                write_archive(payload, repo, sha, cfg.get("compare"))
                payload.seek(0)
                with subprocess.Popen(
                    [
                        "ssh",
                        "-o",
                        "BatchMode=yes",
                        "-o",
                        "ConnectTimeout=15",
                        "--",
                        host,
                        "bash -lc " + shlex.quote(shell),
                    ],
                    stdin=payload,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    errors="replace",
                ) as proc:
                    for line in proc.stdout:
                        log.write(line)
                        log.flush()
                        if stream:
                            print(line, end="", flush=True)
                        if not line.startswith("OPSB_CASE "):
                            continue
                        msg = json.loads(line[len("OPSB_CASE ") :])
                        kind = msg.pop("event")
                        event(kind, **msg)
                        if kind == "case":
                            key = msg.pop("id")
                            if (
                                key in completed
                                or key not in report["selected"]
                                or msg.get("status") not in STATUSES
                            ):
                                raise ValueError("Unexpected case result")
                            # `device_us` holds warm samples only; `cold_us` holds the discarded warmups.
                            cold = msg.pop("cold_us", None) or []
                            for samples in (msg.get("device_us", []), cold,
                                            *(msg.get(f) or [] for f in TIMING_LISTS)):
                                if not isinstance(samples, list) or any(
                                    type(n) not in (int, float)
                                    or not math.isfinite(n)
                                    or n <= 0
                                    for n in samples
                                ):
                                    raise ValueError(
                                        f"Invalid device timing for case {key}"
                                    )
                            rows[key]["cold_samples_us"] = cold
                            completed.add(key)
                            rows[key].update(
                                {
                                    k: v
                                    for k, v in msg.items()
                                    if k not in rows[key] or k == "status"
                                }
                            )
                        elif kind == "environment":
                            report["environment"] = msg
                        elif kind == "device_batch":
                            report["device_batch"] = msg
                        elif kind == "built":
                            built = True
                    rc = proc.wait()
        except (
            OSError,
            KeyError,
            TypeError,
            AttributeError,
            ValueError,
            subprocess.SubprocessError,
        ) as exc:
            report["error"] = str(exc)
            event("error", detail=str(exc))
        hidden_final.settle(rows)
        results = list(rows.values())
        # Advisory hidden variants are reported apart and never decide verification.
        summary = summarize(hidden_final.blocking(rows, report["selected"]))
        ok = rc == 0 and built and summary["verified"]
        report.update(
            cases=results,
            summary=summary,
            built=built,
            remote_exit=rc,
            finished_at=now(),
            exit_code=0 if ok else 2 if built else 1,
            verified=formal and ok,
            official=summarize([r for r in results if r["source"] == "official"]),
        )
        hidden = hidden_final.summary(rows, report["selected"])
        if hidden:
            report["hidden_final"] = hidden
        event("finish", exit_code=report["exit_code"], summary=summary)
        # Warmup executions absorb kernel load and session setup; only warm samples count.
        reasons = warm_timing(rows, report, cfg)
        report["timing_source"] = timing.run_source(rows, report)
        if (report.get("device_batch") or {}).get("aborted_early"):
            report["aborted_early"] = report["device_batch"]["aborted_early"]
        if report.get("compare"):
            pair_statistics(folder.parents[2], report, rows)
        report["timing_score_eligible"] = not reasons
        # Readings faster than physics or history allow wait for a cold confirmation.
        report["implausible"] = plausibility.check(
            report, rows, list(case_map._reports(folder.parents[2], report["title"])), plausibility.peak_bandwidth())
        report["digest"] = build_digest(folder.parents[2], report, rows, reasons)
        env = report.get("environment", {})
        if formal and ok and env.get("soc"):
            print("RUNTIME_SOC=" + env["soc"], file=log)
    atomic_write_json(dest / "report.json", report)
    # The UI follows the most recently finished run. The gate follows its own formal pointer.
    with (folder / ".bench-publish.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = read_doc(folder / "latest-bench.json")
        if report["finished_at"] >= current.get("finished_at", ""):
            failures = [
                r["id"] for r in results
                if r["status"] not in ("PASS", "NOT_RUN") and not hidden_final.advisory(r)
            ]
            atomic_write_json(
                folder / "latest-bench.json",
                {
                    "run_id": run_id,
                    "formal": formal,
                    "finished_at": report["finished_at"],
                    "experiment": experiment,
                    "passed": summary["passed"],
                    "selected": summary["total"],
                    "total": len(cases),
                    "failures": failures,
                    "report": str(dest / "report.json"),
                },
            )
        if formal and report["finished_at"] >= read_doc(folder / "result.json").get(
            "finished_at", ""
        ):
            atomic_write_text(
                folder / "validation.log", (dest / "validation.log").read_text()
            )
            record = {
                k: report[k]
                for k in (
                    "sha",
                    "title",
                    "exit_code",
                    "finished_at",
                    "verified",
                    "run_id",
                    "oracle",
                    "official",
                    "summary",
                    "built",
                )
            }
            atomic_write_json(
                folder / "result.json",
                {
                    **record,
                    "layout": "template",
                    "scheme": "reference",
                    "fast_lane": False,
                },
            )
    return {**report, "report": str(dest / "report.json")}


def pair_statistics(ws, report, rows):
    """Per-case paired ratio (baseline over candidate) and interval of a paired run.

    Both arms read the same timer: the profiler when it covered every round of both arms,
    else the event timer with the case map's launch overhead removed first.
    """
    try:
        doc = case_map.current(ws, report["title"])
        offset = float(doc.get("offset_us") or 0.0) if case_map.usable(doc) else 0.0
    except (OSError, ValueError, KeyError, TypeError):
        offset = 0.0
    used = set()
    for key in report["selected"]:
        row = rows[key]
        chosen = paired_timing.case_pairs(row)
        if chosen is None:
            continue
        source, a, b = chosen
        shift = 0.0 if source == "profiler" else offset
        ci = paired_timing.interval(a, b, offset_us=shift)
        if ci:
            row["pair"] = dict(ci, source=source, offset_us=shift)
            boot = paired_timing.bootstrap(a, b, offset_us=shift)
            if boot:
                row["pair_bootstrap"] = {k: boot[k] for k in ("lo", "hi")}
            used.add(source)
    report["compare"]["source"] = used.pop() if len(used) == 1 else ("mixed" if used else None)
    ratios = [r["pair"]["ratio"] for r in rows.values() if r.get("pair") and r.get("source") == "official"]
    report["compare"]["geomean_ratio"] = (round(math.exp(sum(math.log(x) for x in ratios) / len(ratios)), 4)
                                          if ratios else None)


def warm_timing(rows, report, cfg):
    """Write warm statistics on every timed case; return why the run is not score-eligible."""
    timed = set(report.get("timed", []))
    reasons = []
    if cfg.get("warmup", 0) < 1:
        reasons.append("warmup is 0, so samples include the cold start")
    if cfg.get("repeats", 0) < 3:
        reasons.append("fewer than 3 warm repeats")
    if not timed:
        reasons.append("no timed case selected")
    for key in report["selected"]:
        row = rows[key]
        row["timed"] = key in timed
        if key not in timed:
            continue
        samples = row.get("device_us") or []
        cold = row.get("cold_samples_us") or []
        row.update(
            min_us=min(samples) if samples else None,
            median_us=statistics.median(samples) if samples else None,
            cold_us=cold[0] if cold else None,
            samples=len(samples),
            timing_source=row.get("timing_source") or "event",
        )
        timing.source_minima(row)
        if row["status"] != "PASS" or not samples:
            reasons.append(f"timed case {key} has no warm samples ({row['status']})")
    return reasons


def _ledger(ws):
    """Receipt rows, one per submission (scheduler.receipts)."""
    return receipts.load(ws)


def _row_sha(row):
    return row.get("canonical_sha") or row.get("sha") or ""


def _positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _judge_view(row, doc, field):
    """Judge times (``field="times"``) or tbest of one ledger row keyed by the case map's judge keys."""
    named = receipts.case_times(row) if field == "times" else receipts.case_tbest(row)
    if named:
        return {k: v for k, v in named.items() if _positive(v)}
    public, tbest = receipts.public_times(row)
    if field == "times" and not receipts.is_pass(row):
        return {}  # a failed run's positional list can skip more than the untimed cases
    timed = [k for k in doc["judge_ids"] if k not in set(doc.get("judge_untimed") or [])]
    return {k: v for k, v in zip(timed, public if field == "times" else tbest) if _positive(v)}


def _timing_pairs(doc):
    """Mapped ``(local_id, judge_key)`` pairs fit for timing: same workload size on both sides."""
    return [(p["local"], p["judge"]) for p in (doc or {}).get("pairs") or []
            if p.get("judge_mapped") and not p.get("scale_mismatch")]


def timing_offset(ledger, title, sha, rows, official, cache=None, doc=None):
    """Additive per-launch overhead of the local event timer, in microseconds.

    Device events bracket the launch as well as the kernel, so tiny kernels read a few
    microseconds slow while long ones agree with the judge. When the judge has timed this
    exact SHA, the median of (local warm minimum - judged time) is that overhead. With a usable
    case map (``doc``, scheduler.case_map) the pairs come from the map and skip judge cases that
    are a different workload; without one, the public cases pair by position as before. It is
    cached per workspace so later SHAs reuse the last calibration.
    Returns ``(offset_us, submission_id)`` or ``(None, None)``.
    """
    calibration, gaps = None, []
    for row in ledger:
        if row.get("title") != title or _row_sha(row) != sha:
            continue
        if doc is not None and _judge_view(row, doc, "times"):
            calibration = row
        elif doc is None and isinstance(row.get("public_times_us"), list) and row["public_times_us"]:
            calibration = row
    if calibration is not None and doc is not None:
        judged = _judge_view(calibration, doc, "times")
        # The overhead belongs to the event timer: profiler readings never calibrate it.
        gaps = [timing.case_value(rows[lid], "event") - judged[key] for lid, key in _timing_pairs(doc)
                if lid in rows and key in judged and timing.case_value(rows[lid], "event")]
    elif calibration is not None:
        gaps = [timing.case_value(rows[cid], "event") - judged
                for cid, judged in zip(official, calibration["public_times_us"])
                if _positive(judged) and cid in rows and timing.case_value(rows[cid], "event")]
    if gaps:
        offset = round(max(0.0, statistics.median(gaps)), 3)
        if cache is not None:
            try:
                atomic_write_json(cache, {"offset_us": offset, "from": calibration.get("submission_id"),
                                          "title": title, "sha": sha, "at": now()})
            except OSError:
                pass
        return offset, calibration.get("submission_id")
    stored = read_doc(cache) if cache is not None else {}
    if _positive(stored.get("offset_us")) or stored.get("offset_us") == 0:
        return float(stored["offset_us"]), stored.get("from")
    return None, None


def public_estimate(ledger, title, rows, sha=None, cache=None, doc=None):
    """Ratio of the latest official tbest to the local warm minimum per judge-timed case.

    With a usable case map (``doc``) every mapped case with a known tbest counts and cases the
    judge runs as a different workload are skipped; without one, the five public cases pair by
    position. ``efficiency`` subtracts the calibrated timer overhead when one is known; the
    uncorrected value stays in ``efficiency_raw``.
    """
    latest = None
    for row in ledger:
        if row.get("title") != title:
            continue
        if (doc is not None and _judge_view(row, doc, "tbest")) or (
                doc is None and isinstance(row.get("public_tbest_us"), list) and row["public_tbest_us"]):
            latest = row
    if latest is None:
        return None
    official = sorted(r["id"] for r in rows.values() if r["source"] == "official")
    offset, offset_from = timing_offset(ledger, title, sha, rows, official, cache, doc) if sha else (None, None)
    if doc is not None:
        tbest_by_key = _judge_view(latest, doc, "tbest")
        pairs = [(lid, tbest_by_key[key]) for lid, key in _timing_pairs(doc) if key in tbest_by_key and lid in rows]
    else:
        pairs = list(zip(official, latest["public_tbest_us"]))
    raw, ratios = {}, {}
    for cid, tbest in pairs:
        mine = rows[cid].get("min_us")
        if _positive(tbest) and _positive(mine):
            raw[cid] = round(min(tbest / mine, 1.5), 3)
            event = (rows[cid].get("timing_source") or "event") == "event"
            kernel = max(mine - offset, 0.1 * mine) if offset is not None and event else mine
            ratios[cid] = round(min(tbest / kernel, 1.5), 3)
    mean = lambda values: round(sum(values) / len(values), 3) if values else None
    out = {
        "efficiency": mean(list(ratios.values())),
        "efficiency_raw": mean(list(raw.values())),
        "offset_us": offset,
        "offset_from": offset_from,
        "cases": dict(sorted(ratios.items(), key=lambda kv: kv[1])[:10]) or None,
        "tbest_from": latest.get("submission_id"),
    }
    if doc is not None:
        out["case_map"] = doc.get("method")
    return out


EARLY_ABORT = {"enabled": True, "factor": 1.25, "cases": 3, "min_us": 200.0}


def best_pass_sha(ledger, title, sha):
    """The judged Pass SHA with the best official score, other than ``sha``."""
    best = None
    for row in ledger:
        if row.get("title") != title or row.get("status") != "Pass" or _row_sha(row) in ("", sha):
            continue
        score = row.get("score")
        if type(score) in (int, float) and (best is None or score > best[0]):
            best = (score, _row_sha(row))
    return best[1] if best else None


def early_abort_config(ws, title, sha, cases):
    """R9 thresholds for a standard run: event minima of the best Pass SHA on the same inputs.

    ``scheduler.early_abort`` in config.json overrides the defaults; ``{"enabled": false}``
    turns it off. None when there is no measured baseline.
    """
    from .parhelion_paths import read_parhelion_scheduler_config
    if os.environ.get("OPSB_EARLY_ABORT") == "off":  # measurement runs that must time every case
        return None
    try:
        settings = {**EARLY_ABORT, **(read_parhelion_scheduler_config().get("early_abort") or {})}
        base_sha = best_pass_sha(_ledger(ws), title, sha) if settings.get("enabled") else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    if not base_sha:
        return None
    key = pairing.official_key(cases)
    docs = [read_doc(path) for path in (ws / "results" / title / base_sha).glob("bench-runs/*/report.json")]
    docs = [d for d in docs if d.get("timing_score_eligible") is True and d.get("formal") is True
            and (key is None or pairing.pairing_key(d) == key)]
    baseline = timing.best_minima(docs, "event")
    if not baseline:
        return None
    return {"baseline_us": baseline, "base_sha": base_sha, "factor": float(settings["factor"]),
            "cases": int(settings["cases"]), "min_us": float(settings["min_us"])}


def baseline_comparison(ws, ledger, report, rows):
    """Geometric-mean warm speedup against the best official Pass SHA; returns (baseline, missing_sha)."""
    title, sha = report["title"], report["sha"]
    best = None
    for row in ledger:
        if row.get("title") != title or row.get("status") != "Pass" or _row_sha(row) in ("", sha):
            continue
        score = row.get("score")
        if type(score) in (int, float) and (best is None or score > best[0]):
            best = (score, _row_sha(row))
    if best is None:
        return None, None
    base_sha = best[1]
    candidates = []
    for path in (ws / "results" / title / base_sha).glob("bench-runs/*/report.json"):
        doc = read_doc(path)
        if (pairing.same_inputs(doc, report)
                and doc.get("timing_score_eligible") is True and doc.get("sha") == base_sha):
            candidates.append(doc)
    if not candidates:
        return None, base_sha
    base = max(candidates, key=lambda d: d.get("finished_at") or "")
    # Best warm minimum per case over every eligible baseline run, so one run caught in a
    # slow timer mode does not inflate the apparent speedup.
    before, before_rows = {}, {}
    for doc in candidates:
        for c in doc.get("cases", []):
            row = rows.get(c.get("id")) or {}
            value = timing.doc_value(doc, c, row.get("timing_source") or "event")  # same timer as this run
            if value is not None and value < before.get(c["id"], math.inf):
                before[c["id"]] = value
                before_rows[c["id"]] = (c, doc.get("timing_source"))
    speedups, per_case = {}, {}
    for key, row in rows.items():
        if row.get("min_us") and before.get(key):
            speedups[key] = before[key] / row["min_us"]
            # Three-state verdicts against each case's noise band (scheduler.verdicts).
            call = verdicts.paired(before_rows[key][0], row, before_source=before_rows[key][1],
                                   after_source=report.get("timing_source"))
            if call:
                per_case[key] = call
    geomean = math.exp(sum(math.log(v) for v in speedups.values()) / len(speedups)) if speedups else None
    ranked = sorted(speedups.items(), key=lambda kv: abs(math.log(kv[1])), reverse=True)[:10]
    return {
        "sha": base_sha[:7],
        "run_id": base.get("run_id"),
        "geomean_speedup": round(geomean, 4) if geomean else None,
        "verdicts": verdicts.summarize(per_case),
        "cases": {k: round(v, 3) for k, v in ranked} or None,
    }, None


DIGEST_LIMIT = 2048


def failure_detail(row):
    """The device detail, or the first failing output check of a wrong answer."""
    if row.get("detail"):
        return str(row["detail"])
    for name, check in (row.get("outputs") or {}).items():
        if isinstance(check, dict) and not check.get("pass", True):
            reason = check.get("reason") or {k: v for k, v in check.items() if k != "pass"}
            return f"{name}: {reason}"
    return ""


NOISY_RATIO = 1.5


def best_known_minima(ws, report, rows=None):
    """Per-case best warm minimum over earlier eligible runs of the same SHA and official inputs,
    read in the timing source each case used in this run."""
    best = {}
    rows = rows or {}
    folder = ws / "results" / report["title"] / report["sha"] / "bench-runs"
    for path in folder.glob("*/report.json"):
        doc = read_doc(path)
        if (doc.get("run_id") == report.get("run_id") or doc.get("timing_score_eligible") is not True
                or not pairing.same_inputs(doc, report)):
            continue
        for c in doc.get("cases", []):
            value = timing.doc_value(doc, c, (rows.get(c.get("id")) or {}).get("timing_source") or "event")
            if value is not None:
                best[c["id"]] = min(best.get(c["id"], value), value)
    return best


def timer_drift(rows, history):
    """Median ratio of this run's warm minima to the best earlier minima, or None."""
    ratios = sorted(r["min_us"] / history[k] for k, r in rows.items()
                    if _positive(r.get("min_us")) and k in history)
    return round(statistics.median(ratios), 3) if ratios else None


def build_digest(ws, report, rows, reasons=()):
    """One-level summary of a run for tool replies and inboxes; at most 2KB serialized."""
    summary = report.get("summary") or {}
    weights = report.get("weights") or {}
    selected = [rows[k] for k in report.get("selected", []) if k in rows]
    failures = sorted(
        (r for r in selected if r["status"] not in ("PASS", "NOT_RUN") and not hidden_final.advisory(r)),
        key=lambda r: -weights.get(r["id"], 1.0 if r["source"] == "official" else 0.0),
    )[:5]
    timed = sorted((r for r in selected if r.get("min_us")), key=lambda r: -r["min_us"])[:20]
    ledger = _ledger(ws)
    history = best_known_minima(ws, report, rows)
    drift = timer_drift(rows, history)
    noisy = drift is not None and drift > NOISY_RATIO
    # Comparisons use the best warm minimum this SHA has shown, including this run.
    best_rows = {k: ({**r, "min_us": min(r["min_us"], history.get(k, r["min_us"]))}
                     if _positive(r.get("min_us")) else r) for k, r in rows.items()}
    try:
        doc = case_map.current(ws, report["title"])
    except (OSError, ValueError, KeyError, TypeError):
        doc = None
    public = public_estimate(ledger, report["title"], best_rows, report.get("sha"),
                             cache=ws / "results" / ".timing-offset.json",
                             doc=doc if case_map.usable(doc) else None)
    baseline, missing = baseline_comparison(ws, ledger, report, best_rows)
    hints = list(reasons[:2])
    if report.get("aborted_early"):
        hints.insert(0, "aborted early: slower than " + str(report["aborted_early"].get("base_sha", ""))[:7]
                     + " on the first timed cases; the rest was not measured")
    if (report.get("harness") or {}).get("protected_changed"):
        hints.append("restore " + ", ".join(report["harness"]["protected_changed"])
                     + ": only the kernel may change; this run is not formal evidence")
    if noisy:
        hints.append(f"timer read x{drift} slower than this SHA's best run; comparisons use the best run")
    if failures:
        shown = hidden_final.redact(failures[0])["id"] if hidden_final.is_hidden(failures[0]) else failures[0]["id"]
        hints.append(f"fix {shown} ({failures[0]['status']}) before timing work")
    if report.get("implausible"):
        hints.append(f"implausible timing on {', '.join(report['implausible'][:3])}: not gate evidence until "
                     "the next formal run of this SHA (it runs cold) agrees within 10%")
    if missing:
        hints.append(f"run standard on {missing[:7]} first to enable baseline comparison")
    elif baseline and baseline.get("verdicts"):
        calls = baseline["verdicts"]
        hints.append(f"vs {baseline['sha']}: {len(calls['improved'])} improved, {len(calls['regressed'])} regressed "
                     f"beyond noise" + (f", {len(calls['noisy'])} noisy" if calls["noisy"] else ""))
    doc = {
        "level": report.get("level"),
        "suite_version": report.get("suite_version"),
        "formal": report.get("formal"),
        "summary": {k: summary.get(k) for k in ("total", "passed", "counts")},
        "failures": [hidden_final.redact(r) if hidden_final.is_hidden(r) else
                     {"id": r["id"], "status": r["status"], "detail": failure_detail(r)[:200]} for r in failures],
        "timing": {
            "eligible": not reasons,
            "source": report.get("timing_source", "event_warm"),
            "drift": drift,
            "noisy": noisy,
            "implausible": report.get("implausible") or None,
            "warmup": report.get("settings", {}).get("warmup"),
            "repeats": report.get("settings", {}).get("repeats"),
            "cases": {r["id"]: {"min_us": round(r["min_us"], 3),
                                "median_us": round(r["median_us"], 3),
                                "cold_us": None if r.get("cold_us") is None else round(r["cold_us"], 3)}
                      for r in timed} or None,
        },
        "public": public,
        "baseline": baseline,
        "hint": "; ".join(hints)[:200],
    }
    if report.get("compare"):
        doc["paired"] = paired_digest(report, rows)
    if report.get("hidden_final"):
        doc["hidden_final"] = report["hidden_final"]  # class and base case only (scheduler.hidden_final)
    return shrink_digest(doc)


def paired_digest(report, rows):
    """Paired-run summary: baseline, timer, geometric-mean ratio and the cases that moved most."""
    compare = report.get("compare")
    if not compare:
        return None
    moved = sorted((r for r in rows.values() if r.get("pair")), key=lambda r: -abs(math.log(r["pair"]["ratio"])))
    return {"base": compare["base_sha"][:7], "source": compare.get("source"),
            "geomean_ratio": compare.get("geomean_ratio"),
            "cases": {r["id"]: [r["pair"]["ratio"], r["pair"]["lo"], r["pair"]["hi"]] for r in moved[:8]} or None}


def _size(doc):
    return len(json.dumps(doc, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode())


def shrink_digest(doc, limit=DIGEST_LIMIT):
    """Drop the least informative detail first until the digest fits."""
    for failure in doc["failures"] if _size(doc) > limit else []:
        failure["detail"] = failure["detail"][:60]
    for part in (("timing", "cases"), ("public", "cases"), ("baseline", "cases"), ("paired", "cases")):
        holder = doc.get(part[0])
        while _size(doc) > limit and holder and holder.get(part[1]):
            entries = holder[part[1]]
            entries.pop(next(reversed(entries)))
            if not entries:
                holder[part[1]] = None
    while _size(doc) > limit and len(doc["failures"]) > 1:
        doc["failures"].pop()
    if _size(doc) > limit:
        doc["hint"] = doc["hint"][:40]
    return doc


RESULT_KEYS = ("run_id", "exit_code", "level", "formal", "summary", "digest", "report")
RESULT_OPTIONAL = ("title", "sha", "suite_version", "verified", "built", "selection", "selected",
                   "timing_score_eligible", "finished_at", "reused", "joined", "deferred", "active_run",
                   "reason", "error")


def result_line(result):
    """Compact final stdout object of `opsb test`; other result shapes pass through unchanged."""
    if not isinstance(result, dict) or not ("oracle" in result or result.get("deferred")):
        return result
    line = {k: result.get(k) for k in RESULT_KEYS}
    line.update({k: result[k] for k in RESULT_OPTIONAL if k in result})
    return line


def _warm_us(row, source="event"):
    """Warm minimum of a case in one timer; the median of its device samples for old reports."""
    value = timing.case_value(row, source)
    if value is not None:
        return value
    if source == "event" and not row.get("min_us") and row.get("device_us"):
        return statistics.median(row["device_us"])
    return None


def compare_runs(left, right):
    if left["title"] != right["title"]:
        raise ValueError("Choose reports for the same problem")
    same_env = (
        left.get("environment") == right.get("environment")
        and bool(left.get("environment"))
        and left["host"] == right["host"]
        and left["engine"] == right["engine"]
        and left["settings"]["arch"] == right["settings"]["arch"]
        and left["settings"]["device"] == right["settings"]["device"]
    )
    before = {c["key"]: c for c in left["cases"]}
    paired = []
    for b in right["cases"]:
        a = before.get(b["key"])
        if not a or "NOT_RUN" in (a["status"], b["status"]):
            continue
        row = {
            "id": b["id"],
            "before": a["status"],
            "after": b["status"],
            "buckets": b["buckets"],
        }
        source = "profiler" if _warm_us(a, "profiler") and _warm_us(b, "profiler") else "event"
        before_us, after_us = _warm_us(a, source), _warm_us(b, source)
        row["timing_source"] = source
        if same_env and a["status"] == b["status"] == "PASS" and before_us and after_us:
            row.update(before_us=before_us, after_us=after_us)
            row["speedup"] = row["before_us"] / row["after_us"]
            call = verdicts.paired({**a, "min_us": before_us}, {**b, "min_us": after_us},
                                   before_source=left.get("timing_source"), after_source=right.get("timing_source"))
            if call:
                row.update(verdict=call["verdict"], band=call["band"], noisy=call["noisy"])
        paired.append(row)
    groups = {}
    for label in sorted({label for r in paired for label in r["buckets"]}):
        group = [r for r in paired if label in r["buckets"]]
        groups[label] = {
            "paired": len(group),
            "before_pass": sum(r["before"] == "PASS" for r in group),
            "after_pass": sum(r["after"] == "PASS" for r in group),
        }
    return {
        "before": left["run_id"],
        "after": right["run_id"],
        "same_environment": same_env,
        "timing_source": right.get("timing_source", "reference_acl_event"),
        "timing_score_eligible": bool(same_env and left.get("timing_score_eligible")
                                      and right.get("timing_score_eligible")),
        "cases": paired,
        "buckets": groups,
        "selected_before": len(left["selected"]),
        "selected_after": len(right["selected"]),
        "settings_before": left["settings"],
        "settings_after": right["settings"],
        "paired_timings": sum("speedup" in r for r in paired),
        "verdicts": verdicts.summarize({r["id"]: r for r in paired if "verdict" in r}),
    }


def read_doc(path):
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def formal_evidence(folder, repo, record):
    """Bind the published pointer to a complete execution and the current required case set."""
    rid = record.get("run_id")
    if (
        record.get("scheme") != "reference"
        or not isinstance(rid, str)
        or not re.fullmatch("[0-9a-f]{32}", rid)
    ):
        return {}
    report = read_doc(folder / "bench-runs" / rid / "report.json")
    if not (
        report.get("formal") is True
        and report.get("verified") is True
        and report.get("exit_code") == 0
        and report.get("run_id") == rid
        and report.get("sha") == folder.name == record.get("sha")
        and report.get("title") == folder.parent.name
        and report.get("scheme") == "reference"
    ):
        return {}
    try:
        # The required set is what `standard` selects under the current suite; final-tier
        # variants are optional, and unselected catalog rows appear as NOT_RUN.
        spec = suites.load_suite(folder.parents[2], folder.parent.name)
        oracle, cases = catalog(repo, folder.name, suites.to_profile(spec))
        standard, _, _ = suites.select(cases, spec, "standard")
        required = {c["id"]: c["key"] for c in standard}
        rows = report["cases"]
        if (
            report["oracle"] == oracle
            and bool(report.get("settings", {}).get("strict_fp32")) == bool(spec["strict_fp32"])
            and {r["id"]: r["key"] for r in rows if r["id"] in required and r["status"] == "PASS"} == required
        ):
            return report
    except (KeyError, OSError, TypeError, ValueError, subprocess.CalledProcessError):
        pass
    return {}


def problem_repo(ws, title):
    for row in load_problem_manifest(ws):
        if row["title"] == title:
            return ws / row["path"]
    raise ValueError(f"Unknown problem: {title}")


def list_cases(ws, title, repo, rev, profile_path=None):
    sha = committed(repo, rev)
    if has_reference(repo, sha):
        profile = load_profile(ws, title, profile_path)
        oracle, cases = catalog(repo, sha, profile)
        folder = ws / "results" / title / sha / "bench-runs"
        runs = []
        for path in folder.glob("*/plan.json"):
            doc = read_doc(path.parent / "report.json") or read_doc(path)
            runs.append(
                {
                    k: doc.get(k)
                    for k in (
                        "run_id",
                        "experiment",
                        "started_at",
                        "finished_at",
                        "formal",
                        "summary",
                    )
                }
            )
        return {
            "scheme": "reference",
            "measurement_notes": read_doc(ws / "opsb.workspace.json").get("bench_observations", {}).get(title, []),
            "sha": sha,
            "oracle": oracle,
            "cases": cases,
            "runs": sorted(runs, key=lambda r: r["started_at"] or "", reverse=True),
        }
    from .calibration import BenchmarkCalibrator

    corr, perf = BenchmarkCalibrator(ws)._spec_paths(title)
    return {
        "scheme": "extended",
        "sha": sha,
        "correctness": read_doc(corr),
        "performance": read_doc(perf),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["cases", "compare"])
    parser.add_argument("workspace", type=Path)
    parser.add_argument("problem")
    parser.add_argument("runs", nargs="*")
    parser.add_argument("--sha", default="HEAD")
    parser.add_argument("--profile")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    ws, title = args.workspace.resolve(), args.problem
    try:
        repo = problem_repo(ws, title)
        if args.action == "cases":
            doc = list_cases(ws, title, repo, args.sha, args.profile)
        else:
            if len(args.runs) != 2:
                raise ValueError("Expected two run IDs")
            reports = []
            for rid in args.runs:
                if not re.fullmatch("[0-9a-f]{32}", rid):
                    raise ValueError("Invalid run ID")
                matches = list(
                    (ws / "results" / title).glob(
                        "*/bench-runs/" + rid + "/report.json"
                    )
                )
                if len(matches) != 1:
                    raise ValueError(f"No unique completed report for {rid}")
                reports.append(read_doc(matches[0]))
            doc = compare_runs(*reports)
        print(json.dumps(doc, ensure_ascii=False, indent=None if args.json else 2))
        return 0
    except (KeyError, ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
