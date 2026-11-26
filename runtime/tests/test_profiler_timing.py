"""Profiler task timing in the broker: parsing, attribution, Phase C, fallback and pairing order."""
import json
import os
import sys
import time
from pathlib import Path

import pytest

RUNTIME = Path(__file__).resolve().parents[1] / "npu_runtime"
sys.path.insert(0, str(RUNTIME))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import case_batch  # noqa: E402
import case_pairs  # noqa: E402
import profiler_capture  # noqa: E402
from test_device_broker import Client, SOURCE, base_cfg, env, make_case, make_job  # noqa: E402,F401
import device_broker  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
COMPANIONS = {"profiler_capture": (RUNTIME / "profiler_capture.py").read_text(encoding="utf-8")}

# Like the broker tests' fake sample, but it also logs one kernel task per execution
# (start time, duration = event reading - 3 us) for the fake profiler to export.
PROFILED_SAMPLE = r'''#!{python}
import os, sys, time
out = os.fdopen(int(os.environ["OPSB_BATCH_FD"]), "w", buffering=1)
tasks = os.environ["FAKE_TASKS"]
for line in sys.stdin:
    timed, path = line.rstrip("\n").split(" ", 1)
    us = 0.0
    for token in os.path.basename(os.path.dirname(path)).split("_"):
        kind, _, value = token.partition("-")
        if kind == "us":
            us = float(value)
        elif kind == "fail":
            out.write("3 0\n")
            sys.exit(3)
    if os.path.basename(path).startswith("warm"):
        us += 100
    with open(tasks, "a") as f:
        f.write(f"{{time.time() * 1e6 + 1:.3f}} {{us - 3:.3f}}\n")
    out.write(f"0 {{us}}\n")
'''

# Stands in for the device profiler: runs the application with stdin, the reply descriptor and
# the environment, logs the release marker, then (after a delay) exports a task_time CSV.
FAKE_PROFILER = r'''#!{python}
import os, subprocess, sys, time
args = dict(a.split("=", 1) for a in sys.argv[1:] if a.startswith("--") and "=" in a)
out = args["--output"]
app = args["--application"].split()
fd = int(os.environ["OPSB_BATCH_FD"])
print("[INFO] Start profiling....", flush=True)
rc = subprocess.call(app, pass_fds=(fd,))
print("[INFO] Start export data in PROF_000001.", flush=True)
time.sleep(float(os.environ.get("FAKE_EXPORT_SEC", "0.8")))
if os.environ.get("FAKE_PROFILER_EMPTY") != "1":
    target = os.path.join(out, "PROF_000001", "summary_output")
    os.makedirs(target, exist_ok=True)
    with open(os.path.join(target, "task_time_1.csv"), "w") as csv:
        csv.write("Device_id,kernel_name,kernel_type,stream_id,task_id,task_time(us),task_start(us),task_stop(us)\n")
        csv.write("0,N/A,PROFILING_ENABLE,48,1,0.000,1.000\t,1.000\t\n")
        for k, line in enumerate(open(os.environ["FAKE_TASKS"])):
            start, duration = line.split()
            csv.write(f"0,_Z1kv,KERNEL_AIVEC,47,{{k}},{{duration}},{{start}}\t,{{start}}\t\n")
print("[INFO] Profiling finished.", flush=True)
sys.exit(rc)
'''


def test_parse_prefers_task_time_and_keeps_kernels_only():
    tasks = profiler_capture.parse_tasks(FIXTURES / "profiler")
    assert tasks == [(1000100.0, 5.68), (1000300.0, 5.5), (1000310.0, 1.25), (1000500.0, 6.0)]
    assert profiler_capture.parse_tasks(FIXTURES / "profiler_op_only") == tasks  # op_summary fallback
    assert profiler_capture.parse_tasks(FIXTURES / "missing") == []


def test_tasks_belong_to_the_window_holding_their_start():
    tasks = profiler_capture.parse_tasks(FIXTURES / "profiler")
    windows = [("a", 1000050, 1000200), ("b", 1000250, 1000400), ("c", 1000450, 1000600)]
    assert profiler_capture.assign(tasks, windows) == {"a": 5.68, "b": 6.75, "c": 6.0}
    # An execution that launched no kernel stays unattributed; tasks are never shifted by count
    # (on the device an empty case made a count-based assignment read other cases' kernels).
    found = profiler_capture.assign(tasks, windows + [("empty", 1000700, 1000800)])
    assert "empty" not in found and found["c"] == 6.0
    assert profiler_capture.assign(tasks, [("x", 1, 2), ("y", 3, 4)]) == {}


def test_command_collects_task_time_only():
    argv = profiler_capture.command("/opt/prof", "--trace", "/tmp/out", ["/x/sample", "0"])
    assert argv[0] == "/opt/prof" and "--task-time=l0" in argv and "--trace=off" in argv
    assert argv[-1] == "--application=/x/sample 0"


def _profiled(env, monkeypatch, export_sec="1.5", empty=False):
    sample = env.tmp / "psample"
    sample.write_text(PROFILED_SAMPLE.format(python=sys.executable))
    sample.chmod(0o755)
    prof = env.tmp / "fake-profiler"
    prof.write_text(FAKE_PROFILER.format(python=sys.executable))
    prof.chmod(0o755)
    monkeypatch.setenv("FAKE_TASKS", str(env.tmp / "tasks.txt"))
    monkeypatch.setenv("FAKE_EXPORT_SEC", export_sec)
    monkeypatch.setenv("FAKE_PROFILER_EMPTY", "1" if empty else "0")
    path = device_broker.ensure_broker(base_cfg(env), SOURCE, companions=COMPANIONS)
    assert path == str(env.sock)
    return path, sample, {"bin": str(prof), "acl_flag": "--acl", "dir": str(env.tmp / "prof")}


def test_broker_reports_profiler_samples_after_releasing_the_lock(env, monkeypatch):
    path, sample, profiler = _profiled(env, monkeypatch)
    cases = [make_case(env, 1, "us-9", warmup=1, repeats=3, parts=2), make_case(env, 2, "us-5", repeats=2)]
    client = Client(path, make_job(env, "prof", cases, executable=str(sample), profiler=profiler))
    client.wait_for("done", timeout=20)
    kinds = [e["event"] for e in client.events if e["event"] not in ("progress", "queued")]
    # The device lock is released before the export: profile results follow "released".
    assert kinds.index("released") < kinds.index("profile") < kinds.index("done")
    ledger_rows = [json.loads(l) for l in (env.home / ".opsb/die_ledger.jsonl").read_text().splitlines()]
    assert ledger_rows[-1]["profiler_release_sec"] < ledger_rows[-1]["hold_sec"]
    profile = {e["index"]: e for e in client.of("profile")}
    assert profile[1]["samples"] == [12.0, 12.0, 12.0]  # two parts of (9 - 3) us
    assert profile[1]["cold"] == [212.0]  # warm runs read 100 us more
    assert profile[2]["samples"] == [2.0, 2.0]
    (summary,) = client.of("profiler")
    assert summary["status"] == "collected" and summary["parsed"] == 2
    assert not list((env.tmp / "prof").glob("opsb-prof-*"))  # outputs are removed after parsing
    events = [e["samples"] for e in client.of("case")]
    assert events == [[18.0, 18.0, 18.0], [5.0, 5.0]]  # event samples stay as they were


def test_unreadable_profile_keeps_event_timing(env, monkeypatch):
    path, sample, profiler = _profiled(env, monkeypatch, export_sec="0", empty=True)
    client = Client(path, make_job(env, "noprof", [make_case(env, 1, "us-7", repeats=2)],
                                   executable=str(sample), profiler=profiler))
    client.wait_for("done", timeout=20)
    (profile,) = client.of("profile")
    assert "error" in profile and "samples" not in profile
    plan = {"timed": True, "samples": [client.of("case")[0]["samples"]], "cold": [[]], "profile_error": profile["error"]}
    fields = case_batch.timing_fields(plan)
    assert fields["timing_source"] == "event" and fields["device_us"] == [7.0, 7.0]


def test_missing_profiler_is_reported_and_runs_plain(env, monkeypatch):
    path, sample, profiler = _profiled(env, monkeypatch)
    profiler["bin"] = str(env.tmp / "no-such-profiler")
    client = Client(path, make_job(env, "absent", [make_case(env, 1, "us-4")], executable=str(sample),
                                   profiler=profiler))
    client.wait_for("done", timeout=20)
    assert client.of("profiler")[0]["status"] == "unavailable"
    assert client.of("case")[0]["samples"] == [4.0] and not client.of("profile")


def test_timing_fields_never_mix_sources():
    base = {"timed": True, "samples": [[20.0, 22.0, 21.0]], "cold": [[30.0]]}
    full = dict(base, profile={"samples": [4.0, 4.5, 4.2], "cold": [6.0]})
    fields = case_batch.timing_fields(full)
    assert fields["timing_source"] == "profiler" and fields["device_us"] == [4.0, 4.5, 4.2]
    assert fields["event_us"] == [20.0, 22.0, 21.0] and fields["cold_us"] == [6.0]
    partial = dict(base, profile={"samples": [4.0, 4.5], "cold": []})  # one execution unattributed
    assert case_batch.timing_fields(partial)["device_us"] == [20.0, 22.0, 21.0]
    wrong = dict(base, profile={"samples": [4.0, 900.0, 4.2], "cold": []})  # more kernel time than the bracket
    fields = case_batch.timing_fields(wrong)
    assert fields["timing_source"] == "event" and "above the event" in fields["profiler_error"]
    assert case_batch.timing_fields(dict(base, timed=False))["timing_source"] is None


def test_pair_schedule_alternates_arms_and_rotates_cases():
    steps = case_pairs.schedule([1, 2, 3], 4)
    assert steps == case_pairs.schedule([1, 2, 3], 4)  # deterministic
    assert [a for _, _, a in steps[:6]] == ["A", "B"] * 3 and [a for _, _, a in steps[6:12]] == ["B", "A"] * 3
    firsts = [[i for r, i, _ in steps if r == k][0] for k in range(4)]
    assert firsts == [1, 2, 3, 1]
    for index in (1, 2, 3):  # every case's two arms run back to back in every round
        for r in range(4):
            spots = [k for k, s in enumerate(steps) if s[:2] == (r, index)]
            assert len(spots) == 2 and spots[1] == spots[0] + 1


def test_pair_cases_carry_executables_and_warm_only_in_round_zero(tmp_path):
    def part(name):
        root = tmp_path / name
        return {"warm": [root / "warm-0"], "runs": [root / f"run-{r}" for r in range(2)],
                "base_warm": [root / "base-warm-0"], "base_runs": [root / f"base-run-{r}" for r in range(2)]}
    plans = [{"progress": {"index": 1, "id": "01"}, "timed": True, "parts": [part("c1")]},
             {"progress": {"index": 2, "id": "02"}, "timed": False, "parts": [part("c2")]}]
    cases, mapping = case_pairs.broker_cases(plans, 2, {"A": "/a/sample", "B": "/b/sample"})
    assert [mapping[c["index"]] for c in cases] == [(1, "A", 0), (1, "B", 0), (1, "B", 1), (1, "A", 1),
                                                    (2, "A", 0), (2, "B", 0)]
    assert [c["executable"] for c in cases[:2]] == ["/a/sample", "/b/sample"]
    assert cases[1]["parts"][0]["warm"] == [str(tmp_path / "c1/base-warm-0")]
    assert cases[2]["parts"][0] == {"warm": [], "runs": [str(tmp_path / "c1/base-run-1")]}
    for number, (index, arm, r) in mapping.items():
        case_pairs.record(plans[index - 1], arm, r, {"event": "case", "status": "PASS",
                                                     "samples": [10.0 + r if arm == "A" else 20.0], "cold": []})
    view = case_pairs.arm_plan(plans[0], "B")
    assert view["samples"] == [[20.0, 20.0]] and view["rounds"] == [0, 1]
    assert [p.name for p in view["parts"][0]["runs"]] == ["base-run-0", "base-run-1"]


def test_paired_job_alternates_two_builds_through_the_broker(env, monkeypatch):
    import shutil
    import types
    from test_device_broker import FAKE_ENGINE, FAKE_SAMPLE
    repo = env.tmp / "repo"
    (repo / "build-casebench").mkdir(parents=True)
    shutil.copy(env.sample, repo / "build-casebench/sample")
    slower = env.tmp / "base" / "build-casebench" / "sample"  # the baseline build reads twice as slow
    slower.parent.mkdir(parents=True)
    slower.write_text(FAKE_SAMPLE.format(python=sys.executable).replace("us = float(value)", "us = 2 * float(value)"))
    slower.chmod(0o755)
    (env.tmp / "reference_cases.py").write_text(FAKE_ENGINE)
    monkeypatch.setenv("FAKE_BEHAVIORS", "us-4,us-6")
    root = env.tmp / "work"
    root.mkdir()
    cfg = base_cfg(env, selected=[{"id": "01", "timed": True}, {"id": "02", "timed": True}], repeats=3, warmup=1,
                   timing="perf", compare="b" * 40)
    plans = case_batch.prepare(repo, cfg, root, lambda progress: None)
    batch = case_pairs.execute(repo, cfg, plans)
    assert batch["paired_steps"] == 12 and batch["segments"] == 1
    monkeypatch.setitem(sys.modules, "numpy", types.ModuleType("numpy"))  # no tolerances: numpy unused
    first = case_pairs.result(plans[0], cfg, None)
    assert first["status"] == first["base_status"] == "PASS"
    assert first["device_us"] == [4.0, 4.0, 4.0] and first["base_device_us"] == [8.0, 8.0, 8.0]
    assert first["pairs"] == {"event": [[4.0, 8.0]] * 3}
    # Timed executions alternate builds: consecutive runs come from different processes.
    execs = [line.split() for line in env.log.read_text().splitlines() if line.startswith("exec")]
    runs = [pid for _, pid, _, path in execs if "/run-" in path or "/base-run-" in path]
    assert len({pid for _, pid, _, _ in execs}) == 2 and len(runs) == 12
    for start in range(0, 12, 4):  # A B A B, then B A B A: each round alternates, rounds flip
        block = runs[start:start + 4]
        assert all(a != b for a, b in zip(block, block[1:]))
    assert runs[3] == runs[4]  # the flip at the round boundary
