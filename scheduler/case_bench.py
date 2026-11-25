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

from .parhelion_paths import (
    atomic_write_json,
    atomic_write_text,
    load_problem_manifest,
    workspace_bench_specs_dir,
)

RUNNER = Path(__file__).resolve().parents[1] / "runtime/npu_runtime/reference_cases.py"
STATUSES = {"PASS", "WA", "RE", "TLE", "ERROR", "SKIP", "NOT_RUN"}


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
            or set(variant) - {"id", "base", "params", "seed", "name", "perf"}
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
        row["key"] = digest(
            [oracle, row["base"], row.get("params", {}), row.get("seed")]
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
with tempfile.TemporaryDirectory(prefix='opsb-case-') as temp:
    root = pathlib.Path(temp)
    repo = root/'repo'
    repo.mkdir()
    with tarfile.open(fileobj=sys.stdin.buffer,mode='r|gz') as archive:
        for member in archive:
            dest = (repo/member.name).resolve()
            if not dest.is_relative_to(repo) or not (member.isdir() or member.isfile()):
                raise ValueError('Unsupported archive member: '+member.name)
            archive.extract(member,repo)
    engine = root/'reference_cases.py'
    engine.write_text(cfg.pop('engine'))
    (root/'lanes.py').write_text(cfg.pop('lanes'))
    (root/'case_batch.py').write_text(cfg.pop('case_batch'))
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


def adaptive_cases(folder, cases):
    """Retest a known failure first; otherwise probe one anchor and one performance case."""
    by_id = {case["id"]: case for case in cases}
    # Profiles can change the input behind a local ID. Only reuse failure identity
    # when the catalog also matches, and let a later pass supersede that failure.
    seen = set()
    reports = sorted((folder / "bench-runs").glob("*/report.json"),
                     key=lambda p: p.stat().st_mtime_ns, reverse=True)
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
    anchor = next((c for c in cases if c["source"] == "official" and not c["perf"]), cases[0])
    perf = next((c for c in cases if c["perf"] and c["id"] != anchor["id"]), None)
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
    dry_run=False,
    stream=True,
):
    sha = committed(repo, rev or "HEAD")
    profile = load_profile(ws, title, profile_path)
    oracle, cases = catalog(repo, sha, profile)
    if ids and set(ids) - {r["id"] for r in cases}:
        raise ValueError("Unknown case ID for this commit")
    if bucket and not any(bucket in r["buckets"] for r in cases):
        raise ValueError(f"Unknown bucket: {bucket}")
    selected = [
        r
        for r in cases
        if (not ids or r["id"] in ids)
        and (not bucket or bucket in r["buckets"])
        and (not official_only or r["source"] == "official")
    ]
    if not selected:
        raise ValueError("Empty case selection")
    folder = ws / "results" / title / sha
    adaptive = (suite == "adaptive" or (suite is None and (worker_request or
                any(v is not None for v in (experiment, timing, repeats, profile_path))))) and not (ids or bucket or official_only)
    selection = "explicit" if ids or bucket or official_only else "all"
    if adaptive:
        selected, selection = adaptive_cases(folder, selected)
    formal = not adaptive and all(
        v is None
        for v in (
            ids,
            bucket,
            profile_path,
            experiment,
            timing,
            repeats,
            official_only,
            runner,
        )
    )
    timing = timing if timing is not None else profile.get("timing", "perf")
    repeat_count = repeats if repeats is not None else (1 if adaptive else profile.get("repeats", 3))
    timeout = profile.get("case_timeout", 15)
    if (
        timing not in ("off", "perf", "all")
        or not 1 <= repeat_count <= 100
        or type(timeout) is not int or not 1 <= timeout <= 7200
    ):
        raise ValueError("Invalid timing, repeats or case_timeout")
    reuse_session = profile.get("reuse_session", True)
    if not isinstance(reuse_session, bool):
        raise ValueError("reuse_session must be true or false")
    cfg = {
        "reuse_session": reuse_session,
        "title": title,
        "sha": sha,
        "jobs": os.getenv("OPSB_BUILD_JOBS"),
        "wspt_base": wspt_base,
        "aging_tau": aging_tau,
        "selected": selected,
        "strict_fp32": profile.get("strict_fp32", False),
        "timing": timing,
        "repeats": repeat_count,
        "timeout": min(timeout, 8 if adaptive else 60),
        "arch": os.getenv("NPU_ARCH", "arch-v1"),
        "device": os.getenv("OPSB_DEVICE_ID", "0"),
        "lock": "/srv/work-device0.lock",
    }
    engine = (Path(runner) if runner else RUNNER).read_text()
    modules = {name: RUNNER.with_name(name + ".py").read_text() for name in ("lanes", "case_batch")}
    run_id = uuid.uuid4().hex
    cfg["run_id"] = run_id
    dest = folder / "bench-runs" / run_id
    report = {
        "version": 1,
        "scheme": "custom" if runner else "reference",
        "title": title,
        "sha": sha,
        "run_id": run_id,
        "experiment": experiment,
        "formal": formal,
        "oracle": oracle,
        "engine": digest([engine, modules]),
        "profile": profile,
        "catalog": cases,
        "selected": [c["id"] for c in selected],
        "selection": selection,
        "started_at": now(),
        "host": host,
        "settings": {k: v for k, v in cfg.items() if k != "selected"},
    }
    if dry_run:
        return {**report, "dry_run": True, "exit_code": 0}
    key = digest([host, engine, profile, formal, oracle,
                  {k: v for k, v in cfg.items() if k not in ("run_id", "wspt_base", "aging_tau")},
                  os.getenv("OPSB_TOOLCHAIN_ENV"), modules])
    with active_bench(folder, key, run_id, remeasure=remeasure,
                      limit_experiments=worker_request and not formal) as shared:
        if shared is not None:
            return shared
        return _execute_run(repo, cfg, report, engine, modules, runner, dest, stream)


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
                "OPSB_TOOLCHAIN_ENV", "/opt/toolchain/latest/set_env.sh"
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
                subprocess.run(
                    ["git", "-C", repo, "archive", "--format=tar.gz", sha],
                    stdout=payload,
                    check=True,
                )
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
                            samples = msg.get("device_us", [])
                            if not isinstance(samples, list) or any(
                                type(n) not in (int, float)
                                or not math.isfinite(n)
                                or n <= 0
                                for n in samples
                            ):
                                raise ValueError(
                                    f"Invalid device timing for case {key}"
                                )
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
        results = list(rows.values())
        summary = summarize([rows[key] for key in report["selected"]])
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
        event("finish", exit_code=report["exit_code"], summary=summary)
        # Process-scoped ACL samples include setup overhead. Keep them in the report
        # for diagnostics; exporting them as kernel times contaminates score calibration.
        report["timing_source"] = "reference_acl_event"
        report["timing_score_eligible"] = False
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
                r["id"] for r in results if r["status"] not in ("PASS", "NOT_RUN")
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
    return report


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
        if (
            same_env
            and a["status"] == b["status"] == "PASS"
            and a.get("device_us")
            and b.get("device_us")
        ):
            row.update(
                before_us=statistics.median(a["device_us"]),
                after_us=statistics.median(b["device_us"]),
            )
            row["speedup"] = row["before_us"] / row["after_us"]
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
        "timing_source": "reference_acl_event",
        "timing_score_eligible": False,
        "cases": paired,
        "buckets": groups,
        "selected_before": len(left["selected"]),
        "selected_after": len(right["selected"]),
        "settings_before": left["settings"],
        "settings_after": right["settings"],
        "paired_timings": sum("speedup" in r for r in paired),
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
        profile = load_profile(folder.parents[2], folder.parent.name)
        oracle, cases = catalog(repo, folder.name, profile)
        required = {c["id"]: c["key"] for c in cases}
        rows = report["cases"]
        if (
            report["oracle"] == oracle
            and bool(report.get("settings", {}).get("strict_fp32")) == bool(profile.get("strict_fp32"))
            and len(rows) == len(required)
            and {r["id"]: r["key"] for r in rows if r["status"] == "PASS"} == required
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
