"""Canonical receipt fields (4.1 #8): one reader, normalization, backfill that never duplicates."""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
for path in (str(ROOT), str(ROOT / "cli")):
    if path not in sys.path:
        sys.path.insert(0, path)

from opsb_core.services.judge_score_fetch import extract_case_results, reconstruct_timing_score  # noqa: E402
from opsb_core.services.sync_reconciler import collapse_reconciled_outcomes  # noqa: E402
from scheduler import receipt_backfill, receipts  # noqa: E402

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "receipts_real_shape.jsonl"


def _rows():
    return [json.loads(line) for line in FIXTURE.read_text().splitlines()]


def test_official_score_covers_every_receipt_kind():
    board = {"status": "Pass", "submission_id": "s1", "score": 24.86}
    below = {"status": "Pass", "submission_id": "s2", "score": None, "timing_score": {"score": 24.74}}
    wrong = {"status": "Wrong Answer", "submission_id": "s3", "score": 0}
    crash = {"status": "Runtime Error", "submission_id": "s4", "score": 0.0}
    slow = {"status": "Time Limit Exceeded", "submission_id": "s5"}
    assert [receipts.official_score(r) for r in (board, below, wrong, crash, slow)] == [24.86, 24.74, 0.0, 0.0, 0.0]
    assert [receipts.beat_board(r) for r in (board, below, wrong)] == [True, False, False]
    # Not judged, or judged with no score anywhere yet: unknown, never a silent 0.
    assert receipts.official_score({"status": "rejected", "detail": "gate"}) is None
    assert receipts.official_score({"status": "Pass", "submission_id": "s6", "score": None}) is None
    assert receipts.beat_board({"status": "Pass", "submission_id": "s6", "score": None}) is None


def test_real_shaped_rows_normalize_without_losing_legacy_fields():
    timing, wa_poll, wa_history = _rows()
    norm = receipts.normalize(timing)
    assert {k: norm[k] for k in timing} == timing  # legacy fields untouched
    assert norm["official_score"] == 24.74 and norm["beat_board"] is False and norm["receipt_schema"] == 1
    assert list(norm["time_by_case"]) == [f"tc{i:02d}" for i in range(1, 16)]
    assert norm["case_results"][14] == {"testcase_id": "tc15", "status": "Pass",
                                        "time_us": timing["timing_score"]["time_by_case"]["tc15"],
                                        "tbest_us": timing["timing_score"]["tbest_by_case"]["tc15"]}
    assert norm["tbest_observed_at"] == timing["finished_at"]
    # The judge's formula over the stored times reproduces the stored score.
    assert receipts.judge_score(norm["time_by_case"], norm["tbest_by_case"]) == 24.74
    for row in (wa_poll, wa_history):
        out = receipts.normalize(row)
        assert out["official_score"] == 0.0 and out["beat_board"] is False
        assert "case_results" not in out and "time_by_case" not in out  # nothing on disk names the failing case


def test_judge_score_matches_the_replay_used_on_receipts():
    cases = [{"_id": key, "type": "default", "tbest": tbest} for key, tbest in (("a", 2.0), ("b", 10.0), ("z", None))]
    times = [{"testcase_id": "a", "testcase_status": "Pass", "time": 3.1},
             {"testcase_id": "b", "testcase_status": "Pass", "time": 10.0},
             {"testcase_id": "z", "testcase_status": "Pass", "time": None, "score": 0}]
    detail = {"problem_id": "p", "valid": True, "status": "Pass", "result": times, "judge_revision": 0}
    rule = {"_id": "p", "score_mode": 1, "ranking_submission_mode": "best", "use_baseline": False,
            "judge_revision": 0, "testcases": ["a", "b", "z"]}
    ranking = {"testcases": cases, "total_testcase_count": 3,
               "rows": [{"status": "Pass", "score": 0, "result": times}]}
    expected = receipts.judge_score({"a": 3.1, "b": 10.0, "z": None}, {"a": 2.0, "b": 10.0, "z": None})
    ranking["rows"][0]["score"] = expected
    replay = reconstruct_timing_score(detail, rule, ranking)
    assert replay is not None and replay["score"] == expected
    assert receipts.case_points(1.5, 2.0) == 100.0  # faster than tbest scores the maximum


def test_detail_rows_keep_failing_cases():
    detail = {"result": [{"testcase_id": "a", "testcase_status": "Pass", "time": 2.5, "best_time": 2.0},
                         {"testcase_id": "b", "testcase_status": "Wrong Answer", "time": None},
                         {"testcase_status": "Pass"}]}
    results = extract_case_results(detail)
    assert results == [{"testcase_id": "a", "status": "Pass", "time_us": 2.5, "tbest_us": 2.0},
                       {"testcase_id": "b", "status": "Wrong Answer", "time_us": None, "tbest_us": None}]
    row = receipts.normalize({"status": "Wrong Answer", "submission_id": "s", "score": 0, "case_results": results})
    assert row["time_by_case"] == {"a": 2.5, "b": None} and row["official_score"] == 0.0


def test_every_dedupe_prefers_the_normalized_row():
    plain = {"title": "Op", "status": "Pass", "submission_id": "s1", "score": None,
             "timing_score": {"score": 20.0, "time_by_case": {"a": 2.0}, "tbest_by_case": {"a": 1.0}},
             "finished_at": "2026-11-26T01:00:00+00:00"}
    norm = {**receipts.normalize(plain), "source": receipts.BACKFILL_SOURCE, "backfilled_from_source": None}
    for rows in ([plain, norm], [norm, plain]):
        assert receipts.canonical(rows) == [norm]
        assert collapse_reconciled_outcomes(rows) == [norm]
    # A backfill of a judge-reconcile row does not outrank the original journal row.
    journal = {"title": "Op", "status": "Pass", "submission_id": "s2", "score": 10.0, "sha": "a" * 40}
    reconcile = {"title": "Op", "status": "Pass", "submission_id": "s2", "score": 9.9, "sha": "",
                 "source": "judge-reconcile"}
    upgraded = {**receipts.normalize(reconcile), "source": receipts.BACKFILL_SOURCE,
                "backfilled_from_source": "judge-reconcile"}
    assert receipts.canonical([journal, reconcile, upgraded]) == [journal]


def _workspace(tmp_path):
    ws = tmp_path / "ws"
    (ws / "results" / "Op").mkdir(parents=True)
    rows = _rows()
    receipt_dir = ws / "results" / "Op" / ("c" * 40)
    receipt_dir.mkdir()
    (receipt_dir / "submission-receipt.json").write_text(json.dumps(
        {"submission_id": "sub-wa-history", "validated_commit": "c" * 40}))
    (ws / "results" / "submit-queue-outcome.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return ws


def test_backfill_lists_then_appends_once(tmp_path):
    ws = _workspace(tmp_path)
    ledger = ws / "results" / "submit-queue-outcome.jsonl"
    before = ledger.read_text()
    listed = receipt_backfill.run(ws)
    assert ledger.read_text() == before  # listing writes nothing
    assert listed["receipts"] == 3 and listed["append"] == 2
    adds = {row["submission_id"]: row["adds"] for row in listed["appended"]}
    assert adds == {"sub-timing": ["official_score", "time_by_case"], "sub-wa-history": ["sha"]}
    assert listed["missing_counts"] == {"failing case": 2, "per-case times": 2}

    applied = receipt_backfill.run(ws, apply=True)
    assert applied["applied"] == 2
    rows = receipts.read_rows(ledger)
    assert len(rows) == 5
    new = rows[-2]
    assert new["source"] == receipts.BACKFILL_SOURCE and new["official_score"] == 24.74
    assert new["finished_at"] == _rows()[0]["finished_at"] and new["backfilled_at"]
    assert receipts.canonical(rows)[0] is not None and len(receipts.load(ws)) == 3
    assert [r["sha"] for r in receipts.load(ws) if r["submission_id"] == "sub-wa-history"] == ["c" * 40]

    # Replaying the same receipts appends nothing.
    again = receipt_backfill.run(ws, apply=True)
    assert again["applied"] == 0 and len(receipts.read_rows(ledger)) == 5


def test_estimator_dedupe_picks_the_backfilled_row(tmp_path):
    from scheduler.estimator import ScoreEstimator

    ws = _workspace(tmp_path)
    receipt_backfill.run(ws, apply=True)
    est = ScoreEstimator.__new__(ScoreEstimator)
    est.workspace = ws
    loaded = est._load_outcomes()
    picked = {r["submission_id"]: r for r in loaded}
    assert picked["sub-timing"]["receipt_schema"] == 1
    assert picked["sub-wa-history"]["source"] == receipts.BACKFILL_SOURCE
    # Backfill rows sit where the rows they rebuilt were: replays read the journal in order.
    order = ["sub-timing", "sub-wa-poll", "sub-wa-history"]
    assert [r["submission_id"] for r in loaded] == order
    raw = receipts.read_rows(ws / "results" / "submit-queue-outcome.jsonl")
    assert [r["submission_id"] for r in receipts.canonical(raw)] == order
    assert [r["submission_id"] for r in collapse_reconciled_outcomes(raw)] == order


def test_breaker_counts_failed_verdicts_not_passes_below_the_board(tmp_path):
    import datetime
    import types
    from unittest import mock

    from scheduler.gate import SubmissionGate

    gate = SubmissionGate(tmp_path)
    now = datetime.datetime(2026, 11, 26, 6, 0, tzinfo=datetime.timezone.utc)
    stamp = lambda minute: f"2026-11-26T05:{minute:02d}:00+00:00"  # noqa: E731
    gate.estimator.outcomes = [
        {"title": "Op", "sha": "a" * 40, "status": "Pass", "submission_id": "s1", "score": 20.0, "submitted_at": stamp(0)},
        {"title": "Op", "sha": "b" * 40, "status": "Wrong Answer", "submission_id": "s2", "score": 0, "submitted_at": stamp(10)},
        {"title": "Op", "sha": "c" * 40, "status": "Pass", "submission_id": "s3", "score": None,
         "timing_score": {"score": 19.5}, "submitted_at": stamp(20)},
        {"title": "Op", "sha": "d" * 40, "status": "Wrong Answer", "submission_id": "s4", "score": 0, "submitted_at": stamp(30)},
    ]
    ledger = types.SimpleNamespace(decay_factor_gamma=1.0, current_score=20.0, active_judged_at=stamp(0))
    with mock.patch.object(gate.estimator, "get_problem_ledger", return_value=ledger):
        breaker = gate.get_circuit_breaker("Op", as_of=now)
    # Before 4.1 the unscored Pass read as a zero, so WA, Pass, WA was "3 consecutive zeroing failures".
    assert breaker.state == "CLOSED", breaker.reason
