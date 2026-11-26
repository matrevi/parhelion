"""4.2 R1-R3: timing ownership, hidden final variants, plausibility, calibration and verdicts."""
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

from scheduler import (case_bench as bench, case_calibration, case_map, harness_check, hidden_final,
                       plausibility, predict_interval, verdicts)
from scheduler import suite as suites
from scheduler.tests.test_case_bench import FAKE_SSH

# The real runner exits 0 after reporting a failed case; the verdict travels in the case events.
REMOTE = FAKE_SSH.replace("sys.exit(code)", "sys.exit(0)")

RUNTIME = Path(__file__).resolve().parents[2] / "runtime" / "npu_runtime"


def _git(repo, *args):
    subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                   check=True, capture_output=True)


@pytest.fixture
def problem(tmp_path, monkeypatch):
    monkeypatch.setenv("PARHELION_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("PARHELION_MCP_ROLE", raising=False)
    repo = tmp_path / "Op"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", repo], check=True)
    (repo / "reference.py").write_text(
        "CASES = [{'id':'01','seed':1,'dtype':'float32','params':{'shape':[64,9]}},"
        "{'id':'02','seed':2,'dtype':'float32','params':{'n':4}},"
        "{'id':'03','seed':3,'params':{'values':[1.0]}}]\n")
    for name in ("main.asc", "sample.py"):
        (repo / name).write_text("template")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "template")
    return tmp_path, repo, bench.committed(repo, "HEAD")


@pytest.fixture
def device(problem, monkeypatch):
    ws, repo, sha = problem
    bindir = ws / "fakebin"
    bindir.mkdir()
    script = bindir / "ssh"
    script.write_text("#!" + sys.executable + "\n" + REMOTE)
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{bench.os.environ['PATH']}")
    return problem


# --- R1 item 1: timing ownership -------------------------------------------------------------

TEMPLATE = """#include "kernel.asc"
int main(int argc, char** argv)
{
        Group group_0({Tensor{{4}, 0, 16, "x.bin"}}, false, true);
        Group group_1({Tensor{{4}, 0, 16, "y.bin"}}, false, false);
        float attr_0 = 1.0;
        Session session(device);
        session.start();
        run_kernel(group_0, group_1, attr_0);
        check(aclrtSynchronizeStream(session.stream));
        return 0;
}
"""


def test_platform_events_bracket_the_kernel_launch():
    sys.path.insert(0, str(RUNTIME))
    try:
        import reference_cases
    finally:
        sys.path.remove(str(RUNTIME))
    code, groups, attrs = reference_cases.generate(TEMPLATE, "reference.impl(x, alpha=1.0)\n")
    case = code[code.index("int case_main("):]
    order = [case.index(s) for s in ("aclrtRecordEvent(begin", "run_kernel(", "aclrtRecordEvent(end",
                                     "aclrtSynchronizeStream(session.stream)", "aclrtEventElapsedTime")]
    assert order == sorted(order)
    assert "sample_us = ms * 1000.0f" in case and groups[0][0] == "x" and attrs == [("alpha", "float")]


def test_changed_protected_file_is_never_formal_evidence(problem):
    ws, repo, sha = problem
    assert harness_check.protected_changes(repo, sha) == []
    assert bench.run(ws, "Op", repo, sha, "npu", dry_run=True)["formal"]
    (repo / "main.asc").write_text("template with a moved timer")
    (repo / "kernel.asc").write_text("kernel")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "edit")
    edited = bench.committed(repo, "HEAD")
    assert harness_check.protected_changes(repo, edited) == ["main.asc"]
    plan = bench.run(ws, "Op", repo, edited, "npu", dry_run=True)
    assert not plan["formal"] and plan["harness"]["protected_changed"] == ["main.asc"]


# --- R1 item 2: hidden final variants ---------------------------------------------------------

def _official(repo, sha):
    return [c for c in bench.catalog(repo, sha, {})[1] if c["source"] == "official"]


def test_hidden_variants_are_deterministic_per_run(problem):
    _, repo, sha = problem
    official = _official(repo, sha)
    first = hidden_final.plan(official, "a" * 32)
    assert first == hidden_final.plan(official, "a" * 32)
    other = hidden_final.plan(official, "b" * 32)
    assert [v.get("seed") for v in first if v["hidden_class"] == "seed"] != \
        [v.get("seed") for v in other if v["hidden_class"] == "seed"]
    kinds = {v["hidden_class"] for v in first}
    assert kinds == {"seed", "scale", "shape"} and all(v["origin"] == "hidden-final" for v in first)
    assert all(v["base"] != "03" for v in first)  # explicit values are never re-seeded or scaled
    assert {v["transform"]["scale"] for v in first if v["hidden_class"] == "scale"} == {3.0, 0.01, -1.0}
    for v in (v for v in first if v["hidden_class"] == "shape"):
        changed = [n for n, o in zip(v["params"]["shape"], [64, 9]) if n != o]
        assert len(changed) == 1 and changed[0] % 2 == 1 and changed[0] < max(64, 9)


def test_hidden_variants_enter_final_only_and_never_the_suite(problem):
    ws, repo, sha = problem
    standard = bench.run(ws, "Op", repo, sha, "npu", level="standard", dry_run=True)
    final = bench.run(ws, "Op", repo, sha, "npu", level="final", dry_run=True)
    assert not any(c.startswith("local/hf-") for c in standard["selected"])
    hidden = [c for c in final["catalog"] if c.get("origin") == "hidden-final"]
    assert hidden and {c["id"] for c in hidden} <= set(final["selected"])
    assert all(not c["perf"] for c in hidden) and len({c["key"] for c in final["catalog"]}) == len(final["catalog"])
    assert final["pairing_key"] == standard["pairing_key"] and final["suite_version"] == standard["suite_version"]
    assert not suites.suite_path(ws, "Op").exists()
    assert all(v.get("origin") != "hidden-final" for v in final["profile"]["variants"])


def test_hidden_failures_block_only_for_seed_variants(device, monkeypatch):
    ws, repo, sha = device
    current = {"hex": "c" * 32}
    monkeypatch.setattr(bench.uuid, "uuid4", lambda: type("U", (), dict(current))())

    def classes():
        planned = bench.run(ws, "Op", repo, sha, "npu", level="final", dry_run=True)
        found = {}
        for c in planned["catalog"]:
            found.setdefault(c.get("hidden_class"), c["id"])
        return planned, found

    planned, by_class = classes()
    monkeypatch.setenv("FAKE_STATUS", json.dumps({by_class["scale"]: "WA"}))
    run = bench.run(ws, "Op", repo, sha, "npu", level="final", stream=False)
    assert run["exit_code"] == 0 and run["verified"]
    assert run["digest"]["hidden_final"]["failed"] == [
        {"class": "scale", "base": next(c["base"] for c in planned["catalog"] if c["id"] == by_class["scale"]),
         "status": "WA", "blocking": False}]
    assert run["digest"]["failures"] == []
    current["hex"] = "d" * 32
    _, by_class = classes()
    monkeypatch.setenv("FAKE_STATUS", json.dumps({by_class["seed"]: "WA"}))
    blocked = bench.run(ws, "Op", repo, sha, "npu", level="final", stream=False, remeasure=True)
    assert blocked["exit_code"] == 2 and not blocked["verified"]
    shown = blocked["digest"]["failures"][0]
    assert shown["id"].startswith("hidden-final:seed:") and "seed" not in shown["detail"].replace("seed variant", "")
    assert by_class["seed"] not in json.dumps(blocked["digest"])


def test_oracle_rejection_of_a_hidden_variant_is_a_skip():
    rows = {"01": {"id": "01", "status": "PASS"},
            "local/hf-x-00": {"id": "local/hf-x-00", "origin": "hidden-final", "hidden_class": "seed",
                              "status": "ERROR", "phase": "prepare", "base": "01"}}
    hidden_final.settle(rows)
    assert rows["local/hf-x-00"]["status"] == "SKIP" and hidden_final.advisory(rows["local/hf-x-00"])
    assert bench.summarize(hidden_final.blocking(rows, list(rows)))["verified"]
    assert hidden_final.summary(rows, list(rows))["skipped"] == 1


def test_runner_scales_only_floating_inputs():
    np = pytest.importorskip("numpy")
    sys.path.insert(0, str(RUNTIME))
    try:
        import reference_cases
    finally:
        sys.path.remove(str(RUNTIME))
    build = reference_cases.scaled_builder(
        lambda cid: ({"x": np.ones(3, np.float32), "idx": np.arange(3, dtype=np.int32)}, {"k": 1}), -1.0)
    inputs, values = build("01")
    assert inputs["x"].dtype == np.float32 and inputs["x"].tolist() == [-1.0] * 3
    assert inputs["idx"].tolist() == [0, 1, 2] and values == {"k": 1}


# --- R1 item 3: physical plausibility ---------------------------------------------------------

def _doc(run, sha, cases, finished, cold=False, eligible=True):
    return {"run_id": run, "sha": sha, "formal": True, "verified": True, "exit_code": 0,
            "timing_score_eligible": eligible, "finished_at": finished, "pairing_key": "pk",
            "settings": {"reuse_session": not cold},
            "cases": [{"id": cid, "key": "k" + cid, "source": "official", "status": "PASS", "min_us": us,
                       **extra} for cid, us, extra in cases]}


def test_floor_and_ratio_marks():
    history = [_doc("r0", "a", [("01", 36.0, {}), ("02", 30.0, {})], "2026-01-01"),
               _doc("r1", "b", [("01", 30.0, {}), ("02", 30.0, {})], "2026-01-02")]
    report = {"run_id": "r2", "selected": ["01", "02", "03"]}
    rows = {"01": {"id": "01", "key": "k01", "status": "PASS", "min_us": 11.0},
            "02": {"id": "02", "key": "k02", "status": "PASS", "min_us": 9.5},
            "03": {"id": "03", "key": "k03", "status": "PASS", "min_us": 500.0, "bytes": 10 ** 6}}
    flagged = plausibility.check(report, rows, history, bandwidth=1e9)
    assert flagged == ["01", "02", "03"]
    assert "largest earlier jump" in rows["01"]["implausible"]["reasons"][0]  # 2.73x > 2 x 1.2x
    assert "3x" in rows["02"]["implausible"]["reasons"][0]                     # 3.16x > 3x
    assert rows["03"]["implausible"]["floor_us"] == 1000.0
    calm = {"02": {"id": "02", "key": "k02", "status": "PASS", "min_us": 11.0}}
    assert plausibility.check({"run_id": "r3", "selected": ["02"]}, calm, history) == []  # 2.7x, no jump history


def test_flag_clears_only_after_a_cold_rerun_agrees(tmp_path):
    flagged = {"implausible": {"reasons": ["x"]}}
    warm = _doc("w", "s", [("01", 9.0, flagged)], "2026-01-01")
    assert plausibility.unconfirmed([warm]) == {"w": {"01"}}
    assert plausibility.unconfirmed([warm, _doc("w2", "s", [("01", 9.1, {})], "2026-01-02")]) == {"w": {"01"}}
    assert plausibility.unconfirmed([warm, _doc("c", "s", [("01", 10.5, {})], "2026-01-02", cold=True)]) == \
        {"w": {"01"}}
    assert plausibility.unconfirmed([warm, _doc("c", "s", [("01", 9.5, {})], "2026-01-02", cold=True)]) == {}
    run_dir = tmp_path / "results" / "Op" / "s" / "bench-runs"
    for doc in (warm, _doc("other", "s", [("02", 20.0, {})], "2026-01-01")):
        (run_dir / doc["run_id"]).mkdir(parents=True)
        (run_dir / doc["run_id"] / "report.json").write_text(json.dumps(doc))
    assert case_map.local_minima(tmp_path, "Op", "s", None)[0] == {"02": 20.0}
    cold = _doc("cold", "s", [("01", 9.4, {})], "2026-01-03", cold=True)
    (run_dir / "cold").mkdir()
    (run_dir / "cold" / "report.json").write_text(json.dumps(cold))
    assert case_map.local_minima(tmp_path, "Op", "s", None)[0] == {"01": 9.0, "02": 20.0}


def test_pending_flag_makes_the_next_formal_run_cold(problem):
    ws, repo, sha = problem
    assert bench.run(ws, "Op", repo, sha, "npu", dry_run=True)["settings"]["reuse_session"] is True
    assert bench.run(ws, "Op", repo, sha, "npu", dry_run=True, cold=True)["settings"]["reuse_session"] is False
    doc = _doc("f" * 32, sha, [("01", 1.0, {"implausible": {"reasons": ["x"]}})], "2026-01-01")
    path = ws / "results" / "Op" / sha / "bench-runs" / doc["run_id"] / "report.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(doc))
    assert bench.run(ws, "Op", repo, sha, "npu", dry_run=True)["settings"]["reuse_session"] is False


# --- R2: calibration table --------------------------------------------------------------------

def _entry(local, judge, band=0.01):
    return {"local": local, "judge_delta": judge, "band": band}


def test_labels_follow_agreement_and_regressions():
    trusted = [_entry(0.2, 0.18), _entry(0.1, 0.12), _entry(-0.1, -0.08), _entry(0.3, 0.25)]
    divergent = [_entry(0.2, -0.1), _entry(0.15, -0.05), _entry(0.1, 0.12)]
    noisy = [_entry(0.2, 0.18), _entry(0.005, 0.2)]
    st = case_calibration.stats(trusted)
    assert case_calibration.label(st) == "trusted" and st["sign_agreement"] == 1.0 and st["rank_corr"] == 1.0
    st = case_calibration.stats(divergent)
    assert case_calibration.label(st) == "divergent" and st["regression_rate"] == pytest.approx(2 / 3, abs=1e-3)
    assert case_calibration.label(case_calibration.stats(noisy)) == "noisy"


def _calibration_ws(ws):
    rows = [
        {"title": "Op", "sha": "a" * 40, "submission_id": "s1", "status": "Pass", "score": 50.0,
         "submitted_at": "2026-01-02T00:00:00+00:00",
         "time_by_case": {"t1": 100.0, "t2": 200.0}, "tbest_by_case": {"t1": 50.0, "t2": 100.0}},
        {"title": "Op", "sha": "b" * 40, "submission_id": "s2", "status": "Pass", "score": 60.0,
         "submitted_at": "2026-01-05T00:00:00+00:00",
         "time_by_case": {"t1": 50.0, "t2": 200.0}, "tbest_by_case": {"t1": 50.0, "t2": 100.0}},
    ]
    ledger = ws / "results" / "submit-queue-outcome.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("".join(json.dumps(r) + "\n" for r in rows))
    for sha, run, finished, first in (("a" * 40, "1" * 32, "2026-01-01T00:00:00+00:00", 100.0),
                                      ("b" * 40, "2" * 32, "2026-01-04T00:00:00+00:00", 50.0)):
        doc = _doc(run, sha, [("01", first, {"device_us": [first, first * 1.01]}),
                              ("02", 200.0, {"device_us": [200.0, 201.0]})], finished)
        path = ws / "results" / "Op" / sha / "bench-runs" / run / "report.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(doc))


def test_backfill_pairs_each_receipt_once(tmp_path):
    _calibration_ws(tmp_path)
    records = {r["submission_id"]: r for r in case_calibration.refresh(tmp_path, "Op")}
    assert records["s1"]["baseline"] is None and not records["s1"]["cases"]
    pair = records["s2"]
    assert pair["baseline"] == {"sha": "a" * 40, "submission_id": "s1", "role": "board"}
    assert pair["cases"]["01"]["local"] == pytest.approx(math.log(2), abs=1e-3)
    assert pair["cases"]["01"]["judge_delta"] == pytest.approx(math.log(2), abs=1e-3)
    assert pair["cases"]["02"]["judge"] == "t2" and pair["cases"]["02"]["band"] is not None
    file = case_calibration.path(tmp_path, "Op")
    lines = file.read_text().splitlines()
    case_calibration.refresh(tmp_path, "Op")
    assert file.read_text().splitlines() == lines and len(lines) == 2  # replay appends nothing
    file.write_text(file.read_text() + json.dumps({**pair, "cases": {}}) + "\n")
    assert case_calibration.load(tmp_path, "Op")["s2"]["cases"] == {}  # the last line of an ID wins
    table = case_calibration.table(tmp_path, "Op", records=list(records.values()))
    assert table["paired_receipts"] == 1 and set(table["cases"]) == {"01", "02"}


def test_interval_treats_divergent_cases_as_unknown():
    rp = {"cases": {"t1": {"t_base": 100.0, "t_pred": 50.0, "ratio": 2.0, "tbest": 30.0, "local": "01"},
                    "t2": {"t_base": 200.0, "t_pred": 200.0, "ratio": 1.0, "tbest": 100.0, "local": "02"}}}
    calm = {"cases": {"01": {"label": "trusted", "discrepancy": 0.02}, "02": {"label": "trusted", "discrepancy": 0.02}},
            "labels": {"trusted": ["01", "02"], "noisy": [], "divergent": []}}
    wild = {"cases": {**calm["cases"], "01": {"label": "divergent", "discrepancy": 0.1}},
            "labels": {"trusted": ["02"], "noisy": [], "divergent": ["01"]}}
    narrow, wide = predict_interval.per_case(rp, calm), predict_interval.per_case(rp, wild)
    assert narrow["divergent"] == [] and wide["divergent"] == ["01"] and wide["unknown_cases"] == 1
    assert wide["low"] < narrow["low"] <= narrow["high"] < wide["high"]
    # Unknown is not zero: the divergent case still scores between its baseline and better.
    assert wide["low"] > 0
    fallback = predict_interval.gaussian(70.0, 2.0, wild)
    assert fallback["method"] == "sigma" and fallback["divergent"] == ["01"]
    assert fallback["high"] - fallback["low"] > 2 * predict_interval.Z * 2.0


# --- R3: three-state verdicts -----------------------------------------------------------------

def test_three_state_verdicts_and_noisy_flag():
    tight = [100.0, 100.5, 100.2]
    before = {"min_us": 100.0, "device_us": tight}
    assert verdicts.paired(before, {"min_us": 90.0, "device_us": [90.0, 90.3, 90.1]})["verdict"] == "improved"
    assert verdicts.paired(before, {"min_us": 99.5, "device_us": [99.5, 99.8]})["verdict"] == "indistinguishable"
    assert verdicts.paired(before, {"min_us": 110.0, "device_us": [110.0, 110.4]})["verdict"] == "regressed"
    shaky = verdicts.paired(before, {"min_us": 80.0, "device_us": [80.0, 90.0, 100.0]})
    assert shaky["noisy"] and verdicts.summarize({"x": shaky})["improved"] == []
    assert verdicts.paired(before, {"min_us": 90.0}) ["verdict"] == "indistinguishable"  # no band, no claim
    profiled = {"min_us": 90.0, "device_us": [90.0, 90.1], "interval_us": [80.0, 100.0]}
    assert verdicts.paired(before, profiled, after_source="profiler")["verdict"] == "indistinguishable"
    assert verdicts.outcome({"improved": ["a"], "regressed": []}) == "improved"
    assert verdicts.outcome({"improved": ["a"], "regressed": ["b"]}) == "regressed"


def test_compare_runs_reports_verdicts():
    env = {"environment": {"host": "h"}, "host": "h", "engine": "e", "settings": {"arch": "x", "device": "0"},
           "title": "Op", "selected": ["01"]}
    left = {**env, "run_id": "l", "cases": [{"id": "01", "key": "k", "status": "PASS", "buckets": [],
                                             "min_us": 100.0, "device_us": [100.0, 100.2]}]}
    right = {**env, "run_id": "r", "cases": [{"id": "01", "key": "k", "status": "PASS", "buckets": [],
                                              "min_us": 80.0, "device_us": [80.0, 80.1]}]}
    out = bench.compare_runs(left, right)
    assert out["cases"][0]["verdict"] == "improved" and out["verdicts"]["improved"] == ["01"]
