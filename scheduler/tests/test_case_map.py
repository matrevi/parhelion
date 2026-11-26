"""Local <-> judge case map (4.1 #5) and per-case receipt prediction (4.1 #7) on synthetic data."""
from __future__ import annotations

import datetime
import json
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[2]
for path in (str(ROOT), str(ROOT / "cli")):
    if path not in sys.path:
        sys.path.insert(0, path)

from scheduler import case_bench, case_map, receipt_predict, receipts  # noqa: E402

UTC = datetime.timezone.utc
SHAS = {name: name * 40 for name in "abcd"}


def _run(ws, title, sha, run, minima, finished, **extra):
    path = ws / "results" / title / sha / "bench-runs" / run / "report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "formal": True, "verified": True, "exit_code": 0, "sha": sha, "timing_score_eligible": True,
        "suite_version": 0, "finished_at": finished, "run_id": run,
        "cases": [{"id": cid, "source": "official", "status": "PASS", "min_us": us} for cid, us in minima.items()],
        **extra}))


def _receipt(title, sha, sid, at, times, tbest, score=None):
    return {"title": title, "sha": sha, "status": "Pass", "submission_id": sid, "score": score,
            "submitted_at": at, "finished_at": at,
            "timing_score": {"score": receipts.judge_score(times, tbest), "time_by_case": times,
                             "tbest_by_case": tbest}}


def _ledger(ws, rows):
    (ws / "results" / "submit-queue-outcome.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


TBEST = {"j1": 2.0, "j2": 10.0, "j3": 400.0, "j4": None}


def _roster_ws(tmp_path):
    """Four local cases, four judge cases; local 03 is the judge's j3 at 100x the size."""
    ws = tmp_path / "ws"
    _run(ws, "Op", SHAS["a"], "r1", {"01": 5.0, "02": 60.0, "03": 40000.0, "04": 1.5}, "2026-11-26T01:00:00+00:00")
    _run(ws, "Op", SHAS["b"], "r2", {"01": 5.0, "02": 30.0, "03": 40000.0, "04": 1.5}, "2026-11-26T01:10:00+00:00")
    _ledger(ws, [
        _receipt("Op", SHAS["a"], "s1", "2026-11-26T02:00:00+00:00", {"j1": 3.0, "j2": 55.0, "j3": 400.0, "j4": None},
                 TBEST, score=40.0),
        _receipt("Op", SHAS["b"], "s2", "2026-11-26T03:00:00+00:00", {"j1": 3.0, "j2": 28.0, "j3": 400.0, "j4": None},
                 TBEST),
    ])
    return ws


def test_roster_pairs_by_position_and_flags_other_workloads(tmp_path):
    ws = _roster_ws(tmp_path)
    doc = case_map.build(ws, "Op")
    assert doc["method"] == "roster" and doc["confidence"] == "high"
    assert doc["judge_ids"] == ["j1", "j2", "j3", "j4"] and doc["judge_untimed"] == ["j4"]
    pairs = {p["local"]: p for p in doc["pairs"]}
    assert [pairs[c]["judge"] for c in ("01", "02", "03", "04")] == ["j1", "j2", "j3", "j4"]
    assert pairs["02"]["judge_mapped"] and not pairs["02"]["scale_mismatch"]
    assert not pairs["03"]["judge_mapped"] and pairs["03"]["scale_mismatch"]  # x100 at 400 us, only 2 SHAs
    assert not pairs["04"]["judge_mapped"]  # the judge does not time j4
    assert doc["unmapped_local"] == ["03", "04"] and doc["unmapped_judge"] == ["j3", "j4"]
    assert case_map.mapped_pairs(doc) == {"01": "j1", "02": "j2"}


def test_map_is_stored_and_only_rewritten_on_new_evidence(tmp_path):
    ws = _roster_ws(tmp_path)
    first = case_map.current(ws, "Op")
    path = case_map.map_path(ws, "Op")
    stamp = path.stat().st_mtime_ns
    assert case_map.current(ws, "Op")["built_at"] == first["built_at"] and path.stat().st_mtime_ns == stamp
    _run(ws, "Op", SHAS["c"], "r3", {"01": 5.0, "02": 20.0, "03": 40000.0, "04": 1.5}, "2026-11-26T03:30:00+00:00")
    rows = receipts.read_rows(ws / "results" / "submit-queue-outcome.jsonl")
    rows.append(_receipt("Op", SHAS["c"], "s3", "2026-11-26T04:00:00+00:00",
                         {"j1": 3.0, "j2": 19.0, "j3": 400.0, "j4": None}, TBEST))
    _ledger(ws, rows)
    changed = case_map.current(ws, "Op")
    assert len(changed["evidence"]) == 3 and json.loads(path.read_text())["built_at"] == changed["built_at"]


def test_assignment_needs_two_agreeing_submissions(tmp_path):
    ws = tmp_path / "ws"
    local = {"01": 10.0, "02": 100.0, "03": 1000.0}
    judge_times = {"j1": 9.0, "j2": 1.0, "j3": 95.0, "j4": 900.0}
    tbest = {k: v / 2 for k, v in judge_times.items()}
    _run(ws, "Op", SHAS["a"], "r1", local, "2026-11-26T01:00:00+00:00")
    _ledger(ws, [_receipt("Op", SHAS["a"], "s1", "2026-11-26T02:00:00+00:00", judge_times, tbest)])
    one = case_map.build(ws, "Op")
    assert one["method"] == "unmapped" and one["pairs"] == [] and one["assignment_votes"] == [
        {"pairs": 3, "submissions": 1}]
    assert not case_map.usable(one)
    _run(ws, "Op", SHAS["b"], "r2", {k: v * 0.8 for k, v in local.items()}, "2026-11-26T02:30:00+00:00")
    rows = receipts.read_rows(ws / "results" / "submit-queue-outcome.jsonl")
    rows.append(_receipt("Op", SHAS["b"], "s2", "2026-11-26T03:00:00+00:00",
                         {k: v * 0.8 for k, v in judge_times.items()}, tbest))
    _ledger(ws, rows)
    two = case_map.build(ws, "Op")
    assert two["method"] == "assignment" and two["confidence"] == "confident"
    assert {p["local"]: p["judge"] for p in two["pairs"]} == {"01": "j1", "02": "j3", "03": "j4"}
    assert two["unmapped_judge"] == ["j2"]


def test_unmapped_without_any_judge_roster(tmp_path):
    ws = tmp_path / "ws"
    _run(ws, "Op", SHAS["a"], "r1", {"01": 10.0}, "2026-11-26T01:00:00+00:00")
    _ledger(ws, [{"title": "Op", "sha": SHAS["a"], "status": "Pass", "submission_id": "s1", "score": 5.0,
                  "public_times_us": [9.0], "public_tbest_us": [4.0]}])
    doc = case_map.build(ws, "Op")
    assert doc["method"] == "unmapped" and doc["judge_ids"] == [] and doc["unmapped_local"] == ["01"]


def test_prediction_follows_the_judge_formula(tmp_path):
    ws = _roster_ws(tmp_path)
    _run(ws, "Op", SHAS["c"], "r3", {"01": 5.0, "02": 15.0, "03": 20000.0, "04": 1.5}, "2026-11-26T03:30:00+00:00")
    rp = receipt_predict.predict(ws, "Op", SHAS["c"], [SHAS["b"]])
    assert rp["method"] == receipt_predict.METHOD and rp["base_sha"] == SHAS["b"] and rp["base_role"] == "board"
    # Mapped j1 (no change) and j2 (2x faster locally); j3 is another workload so keeps T_b; j4 untimed.
    assert rp["matched"] == 2 and rp["timed"] == 3 and rp["coverage"] == round(2 / 3, 3)
    assert rp["offset_us"] == 2.0  # local 5.0 - judged 3.0 on the sub-50 us case
    # 2.15x locally on a 28 us judge case, shrunk for the event-timer resolution (weight 28 / 38).
    ratio = ((30.0 - 2.0) / (15.0 - 2.0)) ** (28.0 / (28.0 + receipt_predict.NOISE_US))
    expected = receipts.judge_score({"j1": 3.0, "j2": 28.0 / ratio, "j3": 400.0, "j4": None}, TBEST)
    assert rp["estimated_score"] == expected
    assert rp["base_score"] == receipts.judge_score({"j1": 3.0, "j2": 28.0, "j3": 400.0, "j4": None}, TBEST)
    assert rp["refresh_tbest"] == [] and rp["gain"] > 0 and rp["improved"] == ["j2"]
    # Without a judged base or with an unusable map there is no prediction.
    assert receipt_predict.predict(ws, "Op", SHAS["c"], [None]) is not None  # falls back to another judged SHA
    assert receipt_predict.predict(ws, "Op", SHAS["c"], [None], allow_other=False) is None  # the gate's rule
    assert receipt_predict.predict(ws, "Op", SHAS["d"], [SHAS["b"]]) is None  # no local runs for d


def test_slow_mode_reading_of_the_base_is_replaced():
    pairs = [{"local": c, "judge": j} for c, j in (("01", "a"), ("02", "b"), ("03", "c"), ("04", "d"))]
    judged = {"a": 2.0, "b": 2.0, "c": 4.0, "d": 3.0}
    local = {"01": 3.0, "02": 25.0, "03": 6.0, "04": 4.5}  # 02 read 12x its judged time, the rest 1.5x
    fixed = receipt_predict.despike(pairs, local, judged)
    assert fixed["02"] == 3.0 and {k: fixed[k] for k in ("01", "03", "04")} == {"01": 3.0, "03": 6.0, "04": 4.5}


def test_prediction_replays_as_of_a_submission(tmp_path):
    ws = _roster_ws(tmp_path)
    at = datetime.datetime(2026, 11, 26, 2, 30, tzinfo=UTC)  # after s1, before s2's receipt
    rp = receipt_predict.predict(ws, "Op", SHAS["b"], [SHAS["a"]], as_of=at)
    assert rp["base_sha"] == SHAS["a"] and rp["tbest_source"] == "s1"
    actual = receipts.official_score(receipts.load(ws, "Op")[1])
    assert abs(rp["estimated_score"] - actual) < 3.0


def test_estimator_uses_receipt_prediction_and_keys_residuals_by_method(tmp_path):
    from scheduler.estimator import CASE_BENCH_METHOD, ScoreEstimator

    ws = _roster_ws(tmp_path)
    est = ScoreEstimator.__new__(ScoreEstimator)
    est.workspace = ws
    est.outcomes = est._load_outcomes()
    ledger = types.SimpleNamespace(active_board_sha=SHAS["a"], historical_best_sha=SHAS["a"], current_score=40.0,
                                   public_score=4.0, hidden_score=36.0, frontier_top1=90.0)
    est.get_problem_ledger = lambda title, as_of=None, **_: ledger
    rp = est._receipt_projection("Op", SHAS["b"], ledger, as_of=datetime.datetime(2026, 11, 26, 2, 30, tzinfo=UTC))
    assert rp is not None and rp["base_sha"] == SHAS["a"]
    out = est._receipt_residuals(receipt_predict.METHOD)
    assert [round(r["residual"], 2) for r in out] == [
        round(receipts.official_score(receipts.load(ws, "Op")[1]) - rp["estimated_score"], 2)]
    cache = json.loads((ws / "results" / ".case-bench-residuals.json").read_text())
    assert any(k.startswith(f"{receipt_predict.METHOD}:Op:") for k in cache)
    assert all(k.startswith(f"{receipt_predict.METHOD}:") for k in cache)  # no legacy key was computed
    assert est.prediction_confidence("Op", method=receipt_predict.METHOD)["n_eff"] == 1.0
    assert CASE_BENCH_METHOD != receipt_predict.METHOD


def test_case_bench_offset_and_efficiency_use_the_map(tmp_path):
    ws = _roster_ws(tmp_path)
    doc = case_map.build(ws, "Op")
    rows = {cid: {"id": cid, "source": "official", "min_us": us}
            for cid, us in {"01": 5.0, "02": 30.0, "03": 40000.0, "04": 1.5}.items()}
    ledger = receipts.load(ws)
    offset, sid = case_bench.timing_offset(ledger, "Op", SHAS["b"], rows, sorted(rows), doc=doc)
    assert (offset, sid) == (2.0, "s2")  # 01 (5-3) and 02 (30-28); 03 is another workload
    est = case_bench.public_estimate(ledger, "Op", rows, SHAS["b"], doc=doc)
    assert set(est["cases"]) == {"01", "02"} and est["case_map"] == "roster" and est["tbest_from"] == "s2"
