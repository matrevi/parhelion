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
