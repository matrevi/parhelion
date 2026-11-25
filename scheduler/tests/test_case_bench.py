import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from scheduler import case_bench as bench
from scheduler.case_feedback import evolve, regression_evidence


@pytest.fixture
def suite(tmp_path, monkeypatch):
    monkeypatch.setenv("PARHELION_HOME", str(tmp_path / "home"))
    repo = tmp_path / "Op"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", repo], check=True)
    (repo / "reference.py").write_text(
        "CASES = [{'id':'01','seed':1,'params':{'n':2}},"
        "{'id':'02','seed':2,'params':{'n':4}}]\nPERF_CASES={'02'}"
    )
    for name in ("main.asc", "sample.py"):
        (repo / name).write_text("fixture")
    subprocess.run(["git", "-C", repo, "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            repo,
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    return tmp_path, repo, bench.committed(repo, "HEAD")


def test_join_only_overlapping_runs(tmp_path, monkeypatch):
    blocked = threading.Event()
    flock = bench.fcntl.flock

    def observe(fd, flags):
        try:
            return flock(fd, flags)
        except BlockingIOError:
            blocked.set()
            raise

    monkeypatch.setattr(bench.fcntl, "flock", observe)
    owner, duplicate, repeat = (c * 32 for c in "abc")
    def join():
        with bench.active_bench(tmp_path, "settings", duplicate) as result:
            return result

    with ThreadPoolExecutor() as pool:
        with bench.active_bench(tmp_path, "settings", owner) as first:
            assert first is None
            future = pool.submit(join)
            assert blocked.wait(5)
            bench.atomic_write_json(tmp_path / "bench-runs" / owner / "report.json", {"run_id": owner, "exit_code": 2})
        assert future.result(timeout=5) == {"run_id": owner, "exit_code": 2, "joined": True}
    with bench.active_bench(tmp_path, "settings", repeat) as fresh:
        assert fresh is None


def test_cached_evidence_and_worker_admission(tmp_path):
    folder = tmp_path / "Op" / "sha1"
    other = folder.with_name("sha2")
    owner, next_run = "a" * 32, "b" * 32
    with bench.active_bench(folder, "settings", owner, limit_experiments=True):
        with bench.active_bench(other, "different", next_run, limit_experiments=True) as busy:
            assert busy["deferred"] and busy["active_run"] == f"sha1/{owner}"
        assert not (other / "bench-runs" / next_run).exists()
        bench.atomic_write_json(folder / "bench-runs" / owner / "report.json", {
            "run_id": owner, "built": True, "finished_at": bench.now(), "exit_code": 0,
            "summary": {"verified": True}})
    with bench.active_bench(folder, "settings", next_run) as cached:
        assert cached["reused"] and cached["run_id"] == owner
    with bench.active_bench(folder, "settings", next_run, remeasure=True) as fresh:
        assert fresh is None


def test_ablation_selection(suite):
    ws, repo, sha = suite
    profile = {
        "version": 1,
        "reuse_session": False,
        "variants": [{"id": "local/v", "base": "01", "seed": 9}],
        "buckets": {"mixed": ["01", "local/v"], "pair": ["01", "02"]},
    }
    path = ws / "trial.json"
    path.write_text(json.dumps(profile))
    plan = bench.run(
        ws,
        "Op",
        repo,
        sha,
        "npu",
        profile_path=path,
        bucket="mixed",
        official_only=True,
        dry_run=True,
    )
    assert plan["selected"] == ["01"] and not plan["formal"]
    assert plan["settings"]["timeout"] == 15
    path.write_text(json.dumps({**profile, "case_timeout": 300}))
    probe = bench.run(ws, "Op", repo, sha, "npu", profile_path=path, dry_run=True)
    assert not probe["formal"] and len(probe["selected"]) <= 2 and probe["settings"]["timeout"] == 8
    assert bench.run(ws, "Op", repo, sha, "npu", suite="all", profile_path=path, dry_run=True)["settings"]["timeout"] == 60
    assert plan["settings"]["reuse_session"] is False
    assert len(plan["catalog"]) == 3
    assert set(plan["catalog"][0]["buckets"]) == {"official", "pair", "mixed"}
    assert not bench.run(ws, "Op", repo, sha, "npu", timing="perf", dry_run=True)[
        "formal"
    ]
    assert bench.run(ws, "Op", repo, sha, "npu", dry_run=True)["formal"]
    with pytest.raises(ValueError, match="same input"):
        bench.catalog(
            repo, sha, {"variants": [{"id": "local/v", "base": "01", "seed": 1}]}
        )


def test_escape_proof_is_scoped_to_failed_submission(suite):
    from unittest.mock import Mock
    from scheduler.calibration import BenchmarkCalibrator

    ws, _, sha = suite
    estimator = Mock()
    earlier = {"status": "Wrong Answer", "submission_id": "first", "finished_at": "2026-01-01T00:00:00Z"}
    latest = {**earlier, "submission_id": "second", "finished_at": "2026-01-02T00:00:00Z"}
    estimator._filtered_outcomes.return_value = [latest, earlier]
    proof = {f"Op:{sha}": {"bad_sha": sha, "submission_ids": ["second"]}}
    cal = BenchmarkCalibrator(ws, estimator=estimator, verified_escapes=proof)
    assert not cal.inspect_escape_state("Op")["escape_lock"]
    newer = {**earlier, "submission_id": "third", "finished_at": "2026-01-03T00:00:00Z"}
    estimator._filtered_outcomes.return_value = [newer, latest, earlier]
    fresh = BenchmarkCalibrator(ws, estimator=estimator, verified_escapes=proof)
    state = fresh.inspect_escape_state("Op")
    assert state["escape_lock"] and state["escape_submission_id"] == "third"


def completed(suite):
    ws, repo, sha = suite
    plan = bench.run(ws, "Op", repo, sha, "npu", dry_run=True)
    rows = [{**c, "status": "PASS", "device_us": [10, 12]} for c in plan["catalog"]]
    return {
        **plan,
        "built": True,
        "verified": True,
        "exit_code": 0,
        "cases": rows,
        "summary": bench.summarize(rows),
        "environment": {"host": "npu", "sdk": "9.1.0"},
    }


def test_formal_evidence_requires_all_cases(suite):
    ws, repo, sha = suite
    doc = completed(suite)
    folder = ws / "results" / "Op" / sha
    report = folder / "bench-runs" / doc["run_id"] / "report.json"
    report.parent.mkdir(parents=True)
    report.write_text(json.dumps(doc))
    assert bench.formal_evidence(folder, repo, doc)
    assert not bench.formal_evidence(folder, repo, {"verified": True, "sha": sha})
    for override in (
        {"formal": False},
        {"cases": doc["cases"][:1]},
        {"sha": "different"},
    ):
        report.write_text(json.dumps({**doc, **override}))
        assert not bench.formal_evidence(folder, repo, doc)


def test_comparison_respects_inputs_and_environment(suite):
    left = completed(suite)
    right = {
        **left,
        "cases": [
            {**left["cases"][0], "device_us": [5, 6]},
            {**left["cases"][1], "status": "NOT_RUN"},
        ],
    }
    result = bench.compare_runs(left, right)
    assert result["paired_timings"] == 1 and result["cases"][0]["speedup"] == 2
    assert result["buckets"]["official"]["paired"] == 1
    assert bench.compare_runs(left, {**right, "engine": "other"})["paired_timings"] == 0


def test_evolution_and_escape_use_observations(suite):
    ws, repo, sha = suite
    good = completed(suite)
    folder = ws / "results" / "Op" / sha
    bad = {
        **good,
        "cases": [{**good["cases"][0], "status": "WA"}, good["cases"][1]],
        "formal": False,
    }
    path = folder / "bench-runs" / bad["run_id"] / "report.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(bad))
    row = {
        "title": "Op",
        "sha": sha,
        "status": "Wrong Answer",
        "submission_id": "judge1",
    }
    trial = evolve(ws, "Op", repo, [row, row], True)
    assert trial["receipts"] == 1 and trial["matching_cases"] == ["01"]
    assert evolve(ws, "Op", repo, [row], True)["version"] == trial["version"]
    assert bench.load_profile(ws, "Op") == {}
    assert regression_evidence(folder, good)["case_ids"] == ["01"]
    assert not regression_evidence(folder, {**good, "engine": "other"})


# ---- 4.0 unified suite: levels, warm timing, digest -------------------------------

FAKE_SSH = '''
import json, os, sys, time
cfg = json.loads(sys.stdin.buffer.readline())
sys.stdin.buffer.read()
out = os.environ.get("FAKE_CFG_OUT")
if out:
    with open(out, "a") as fh:
        fh.write(json.dumps({k: v for k, v in cfg.items() if k not in ("engine", "lanes", "case_batch", "device_broker")}) + "\\n")
time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
status = json.loads(os.environ.get("FAKE_STATUS", "{}"))
def emit(event, **fields):
    print("OPSB_CASE " + json.dumps(dict(event=event, **fields)), flush=True)
emit("environment", host="fake", sdk="1.0")
emit("built")
emit("device_batch", wait_sec=0.1, hold_sec=0.2, acquisitions=1, broker=True, segments=1)
code = 0
for item in cfg["selected"]:
    state = status.get(item["id"], "PASS")
    timed = item["timed"] and state == "PASS"
    warm = [10.0 + r for r in range(cfg["repeats"])] if timed else []
    cold = [550.0 + k for k in range(cfg["warmup"])] if timed else []
    detail = {} if state == "PASS" else {"detail": "mismatch in y"}
    emit("case", id=item["id"], status=state, device_us=warm, cold_us=cold, **detail)
    code = code or (state != "PASS")
sys.exit(code)
'''


@pytest.fixture
def device(suite, monkeypatch):
    import sys

    ws, repo, sha = suite
    bindir = ws / "fakebin"
    bindir.mkdir()
    script = bindir / "ssh"
    script.write_text("#!" + sys.executable + "\n" + FAKE_SSH)
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{bench.os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CFG_OUT", str(ws / "cfg.jsonl"))
    monkeypatch.delenv("PARHELION_MCP_ROLE", raising=False)
    return ws, repo, sha


def sent(ws):
    return [json.loads(line) for line in (ws / "cfg.jsonl").read_text().splitlines()]


def write_suite(ws, **fields):
    from scheduler import suite as suites

    path = suites.suite_path(ws, "Op")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 2, **fields}))


def test_standard_run_announces_and_reports_warm_timing(device, capsys):
    ws, repo, sha = device
    res = bench.run(ws, "Op", repo, sha, "npu", stream=False)
    out = capsys.readouterr().out.splitlines()
    started = [line for line in out if line.startswith("OPSB_RUN_STARTED ")]
    assert len(started) == 1
    announced = json.loads(started[0].split(" ", 1)[1])
    assert announced == {"run_id": res["run_id"], "level": "standard", "selected": 2,
                         "dir": str((ws / "results" / "Op" / sha / "bench-runs" / res["run_id"]).resolve())}
    assert res["exit_code"] == 0 and res["formal"] and res["level"] == "standard"
    assert res["timing_source"] == "event_warm" and res["timing_score_eligible"] is True
    row = next(c for c in res["cases"] if c["id"] == "02")
    assert row["min_us"] == 10.0 and row["median_us"] == 12.0 and row["cold_us"] == 550.0 and row["samples"] == 5
    assert not next(c for c in res["cases"] if c["id"] == "01")["timed"]
    cfg = sent(ws)[0]
    assert cfg["timing"] == "perf" and cfg["warmup"] == 2 and cfg["repeats"] == 5 and cfg["tier"] == 1
    assert cfg["job_timeout"] == 15 * 2 and [c["timed"] for c in cfg["selected"]] == [False, True]
    line = bench.result_line(res)
    for key in bench.RESULT_KEYS:
        assert key in line
    assert line["report"] == str(ws / "results" / "Op" / sha / "bench-runs" / res["run_id"] / "report.json")
    assert len(json.dumps(line["digest"], ensure_ascii=False).encode()) <= 2048
    stored = json.loads((ws / "results" / "Op" / sha / "bench-runs" / res["run_id"] / "report.json").read_text())
    assert stored["digest"] == res["digest"] and stored["level"] == "standard" and stored["suite_version"] == 0
    digest = res["digest"]
    assert set(digest) == {"level", "suite_version", "formal", "summary", "failures", "timing",
                           "public", "baseline", "hint"}
    assert digest["timing"]["cases"] == {"02": {"min_us": 10.0, "median_us": 12.0, "cold_us": 550.0}}
    assert digest["public"] is None and digest["baseline"] is None


def test_zero_warmup_is_not_score_eligible(device):
    ws, repo, sha = device
    write_suite(ws, warmup=0)
    res = bench.run(ws, "Op", repo, sha, "npu", stream=False)
    assert res["timing_score_eligible"] is False and "warmup" in res["digest"]["hint"]
    assert next(c for c in res["cases"] if c["id"] == "02")["cold_us"] is None


def test_smoke_and_final_protocols(device):
    ws, repo, sha = device
    write_suite(ws, case_timeout=30, variants=[{"id": "local/v", "base": "01", "seed": 9}])
    smoke = bench.run(ws, "Op", repo, sha, "npu", level="smoke", stream=False)
    assert smoke["level"] == "smoke" and not smoke["formal"] and smoke["timing_score_eligible"]
    final = bench.run(ws, "Op", repo, sha, "npu", level="final", stream=False)
    assert final["formal"] and final["selected"] == ["01", "02", "local/v"]
    smoke_cfg, final_cfg = sent(ws)
    assert (smoke_cfg["warmup"], smoke_cfg["repeats"], smoke_cfg["timeout"], smoke_cfg["tier"]) == (1, 3, 8, 2)
    assert (final_cfg["repeats"], final_cfg["timeout"], final_cfg["tier"]) == (7, 30, 1)
    assert final_cfg["job_timeout"] == 90


def test_digest_reports_public_efficiency_and_baseline(device):
    ws, repo, sha = device
    base_sha = "f" * 40
    ledger = ws / "results" / "submit-queue-outcome.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"title": "Op", "sha": base_sha, "status": "Pass", "score": 80.0, "submission_id": "s1",
         "public_tbest_us": [9.0, 5.0]},
        {"title": "Op", "sha": "e" * 40, "status": "Pass", "score": 70.0, "submission_id": "s0"},
        {"title": "Other", "sha": "d" * 40, "status": "Pass", "score": 99.0, "public_tbest_us": [1.0]},
    ]
    ledger.write_text("".join(json.dumps(r) + "\n" for r in rows))
    first = bench.run(ws, "Op", repo, sha, "npu", stream=False)
    assert first["digest"]["public"] == {"efficiency": 0.5, "cases": {"02": 0.5}, "tbest_from": "s1"}
    assert first["digest"]["baseline"] is None and "ffffff" in first["digest"]["hint"]
    prior = ws / "results" / "Op" / base_sha / "bench-runs" / ("1" * 32) / "report.json"
    prior.parent.mkdir(parents=True)
    prior.write_text(json.dumps({"run_id": "1" * 32, "sha": base_sha, "suite_version": 0,
                                 "timing_score_eligible": True, "finished_at": bench.now(),
                                 "cases": [{"id": "02", "min_us": 20.0}]}))
    second = bench.run(ws, "Op", repo, sha, "npu", stream=False, remeasure=True)
    baseline = second["digest"]["baseline"]
    assert baseline == {"sha": "fffffff", "run_id": "1" * 32, "geomean_speedup": 2.0, "cases": {"02": 2.0}}


def test_failures_reach_the_digest(device, monkeypatch):
    ws, repo, sha = device
    monkeypatch.setenv("FAKE_STATUS", json.dumps({"01": "WA"}))
    res = bench.run(ws, "Op", repo, sha, "npu", stream=False)
    assert res["exit_code"] == 2
    assert res["digest"]["failures"] == [{"id": "01", "status": "WA", "detail": "mismatch in y"}]
    assert "01" in res["digest"]["hint"]


def test_digest_stays_under_two_kilobytes():
    doc = {
        "level": "standard", "suite_version": 3, "formal": True,
        "summary": {"total": 60, "passed": 50, "counts": {"PASS": 50, "WA": 10}},
        "failures": [{"id": f"local/case-{i}", "status": "WA", "detail": "x" * 200} for i in range(5)],
        "timing": {"eligible": True, "source": "event_warm", "warmup": 2, "repeats": 5,
                   "cases": {f"local/case-{i:02}": {"min_us": 1234.567, "median_us": 1240.5, "cold_us": 5580.25}
                             for i in range(20)}},
        "public": {"efficiency": 0.8, "cases": {f"{i:02}": 0.812 for i in range(10)}, "tbest_from": "s" * 30},
        "baseline": {"sha": "abcdef1", "run_id": "a" * 32, "geomean_speedup": 1.07,
                     "cases": {f"{i:02}": 1.123 for i in range(10)}},
        "hint": "h" * 200,
    }
    small = bench.shrink_digest(doc)
    assert len(json.dumps(small, ensure_ascii=False, separators=(",", ":")).encode()) <= 2048
    assert set(small) == {"level", "suite_version", "formal", "summary", "failures", "timing", "public",
                          "baseline", "hint"}


def test_worker_custom_options_are_rejected(device, monkeypatch, capsys):
    from scheduler import cli_bridge

    ws, repo, sha = device
    monkeypatch.setenv("PARHELION_MCP_ROLE", "worker")
    code = cli_bridge.main(["schedule-test", "--title", "Op", "--profile", str(ws / "p.json"),
                            "--workspace", str(ws)])
    assert code == 2
    assert "Workers select a level (smoke|standard|final); the platform owns the suite" in capsys.readouterr().err
    with pytest.raises(ValueError, match="platform owns the suite"):
        bench.run(ws, "Op", repo, sha, "npu", timing="all", worker_request=True, dry_run=True)
    assert bench.run(ws, "Op", repo, sha, "npu", level="smoke", ids=["02"], worker_request=True,
                     dry_run=True)["selection"] == "explicit"


def test_standard_evidence_ignores_final_tier_variants(device):
    ws, repo, sha = device
    write_suite(ws, variants=[{"id": "local/v", "base": "01", "seed": 9, "tier": "final"}])
    res = bench.run(ws, "Op", repo, sha, "npu", stream=False)
    assert res["selected"] == ["01", "02"]
    assert next(c for c in res["cases"] if c["id"] == "local/v")["status"] == "NOT_RUN"
    folder = ws / "results" / "Op" / sha
    record = json.loads((folder / "result.json").read_text())
    assert bench.formal_evidence(folder, repo, record)
    write_suite(ws, variants=[{"id": "local/v", "base": "01", "seed": 9, "tier": "standard"}])
    assert not bench.formal_evidence(folder, repo, record)


def test_level_is_part_of_the_dedup_key(device):
    ws, repo, sha = device
    standard = bench.run(ws, "Op", repo, sha, "npu", stream=False)
    smoke = bench.run(ws, "Op", repo, sha, "npu", level="smoke", stream=False)
    assert smoke["run_id"] != standard["run_id"] and not smoke.get("reused")
    again = bench.run(ws, "Op", repo, sha, "npu", suite="all", stream=False)
    assert again["reused"] and again["run_id"] == standard["run_id"]
    assert again["report"].endswith(standard["run_id"] + "/report.json")


def test_identical_request_joins_the_running_bench(device, monkeypatch, capsys):
    ws, repo, sha = device
    monkeypatch.setenv("FAKE_SLEEP", "1.5")
    runs = ws / "results" / "Op" / sha / "bench-runs"
    with ThreadPoolExecutor() as pool:
        first = pool.submit(bench.run, ws, "Op", repo, sha, "npu", stream=False)
        for _ in range(100):
            if any(runs.glob("*/plan.json")):
                break
            threading.Event().wait(0.05)
        second = pool.submit(bench.run, ws, "Op", repo, sha, "npu", stream=False)
        owner, joined = first.result(timeout=30), second.result(timeout=30)
    assert joined["joined"] and joined["run_id"] == owner["run_id"]
    assert len(sent(ws)) == 1
    started = [line for line in capsys.readouterr().out.splitlines() if line.startswith("OPSB_RUN_STARTED ")]
    assert len(started) == 2 and all(owner["run_id"] in line for line in started)
