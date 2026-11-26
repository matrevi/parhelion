"""4.3 P22: the judge rule snapshot, its board-mode priority, latest-mode replay and the rules view.

The rule endpoint is the only source of a problem's board mode, and the reconciler already reads it;
these tests pin what reaches disk (a whitelist and nothing personal), when it is rewritten, how the
estimator prefers it over the scraped evidence, how the replayed score behaves under ``latest``, and
what the ``rules`` report view answers inside its byte budget.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

CLI_DIR = Path(__file__).resolve().parents[1]
ROOT = CLI_DIR.parent
for path in (str(CLI_DIR), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from opsb_core.mcp.tools import call_tool  # noqa: E402
from opsb_core.mcp import handlers_report  # noqa: E402
from opsb_core.services import judge_rules  # noqa: E402
from opsb_core.services.judge_score_fetch import reconstruct_timing_score  # noqa: E402
from opsb_core.services.sync_reconciler import _best_score_bound  # noqa: E402
from scheduler import receipts  # noqa: E402

# A rule payload shaped like the live endpoint: the fields we keep, plus the ones we must drop.
RULE = {
    "_id": "pid-1",
    "title": "Op",
    "desc": "problem statement that must never reach disk",
    "code_template": "kernel source that must never reach disk",
    "user_id": "author-account-must-not-leak",
    "contest_id": "contest-1",
    "score_mode": 1,
    "use_baseline": False,
    "ranking_submission_mode": "Latest",
    "iterations": 5,
    "judge_revision": 7,
    "ranking_testcase_count": None,
    "testcases": [
        {"testcase_id": {"_id": "#01", "type": "default", "baseline": None}},
        {"testcase_id": {"_id": "#02", "type": "default", "baseline": None}},
        {"testcase_id": {"_id": "#03", "type": "sample", "baseline": None}},
    ],
}

FETCHED = "2026-11-26T00:00:00+00:00"


def _text(doc) -> str:
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":"))


def _workspace(tmp_path: Path, problems: list[dict]) -> Path:
    ws = tmp_path / "ws"
    (ws / "results").mkdir(parents=True)
    rows = [{"slug": str(p["title"]).lower(), "repo_slug": f"r-{str(p['title']).lower()}", **p}
            for p in problems]
    (ws / "opsb.workspace.json").write_text(json.dumps({
        "schema_version": 2, "layout": "clean-agents-first", "title": "P22 fixture", "contest_id": "p22",
        "problems": rows,
    }), encoding="utf-8")
    return ws


# ---------------------------------------------------------------- the snapshot

def test_normalize_keeps_only_the_whitelist(tmp_path: Path) -> None:
    snapshot = judge_rules.normalize(RULE, fetched_at=FETCHED)
    assert snapshot == {
        "schema_version": 1, "problem_id": "pid-1", "score_mode": 1, "use_baseline": False,
        "ranking_submission_mode": "latest", "iterations": 5, "judge_revision": 7,
        "testcases": {"total": 3, "by_type": {"default": 2, "sample": 1}}, "fetched_at": FETCHED,
    }
    body = _text(snapshot)
    for leaked in ("author-account-must-not-leak", "problem statement", "kernel source", "cookie", "user"):
        assert leaked not in body
    assert "ranking_testcase_count" not in snapshot  # null in the rule means "not published"


def test_normalize_counts_visibility_only_when_every_case_declares_it() -> None:
    rule = dict(RULE, testcases=[
        {"testcase_id": {"_id": "#01", "type": "default"}, "hidden": False},
        {"testcase_id": {"_id": "#02", "type": "default"}, "hidden": True},
    ])
    assert judge_rules.normalize(rule, fetched_at=FETCHED)["testcases"] == {
        "total": 2, "by_type": {"default": 2}, "public": 1, "hidden": 1}
    partial = dict(RULE, testcases=[
        {"testcase_id": {"_id": "#01", "type": "default"}, "hidden": False},
        {"testcase_id": {"_id": "#02", "type": "default"}},
    ])
    assert judge_rules.normalize(partial, fetched_at=FETCHED)["testcases"] == {
        "total": 2, "by_type": {"default": 2}}


def test_normalize_records_a_time_limit_only_when_the_rule_has_one() -> None:
    assert "time_limit" not in judge_rules.normalize(RULE, fetched_at=FETCHED)
    rule = dict(RULE, time_limit_ms=4000)
    assert judge_rules.normalize(rule, fetched_at=FETCHED)["time_limit"] == {
        "field": "time_limit_ms", "value": 4000}


def test_normalize_rejects_a_payload_that_is_not_a_rule() -> None:
    assert judge_rules.normalize({}, fetched_at=FETCHED) is None
    assert judge_rules.normalize({"user_id": "x", "desc": "y"}, fetched_at=FETCHED) is None
    assert judge_rules.normalize("nonsense", fetched_at=FETCHED) is None


def test_save_writes_only_when_the_content_changes(tmp_path: Path) -> None:
    ws = _workspace(tmp_path, [{"title": "Op", "path": "Op", "problem_id": "pid-1"}])
    first = judge_rules.save(ws, "Op", RULE, fetched_at=FETCHED)
    assert first["saved"] is True and judge_rules.path_for(ws, "Op").is_file()
    stored = judge_rules.load(ws, "Op")
    assert stored["fetched_at"] == FETCHED and stored["ranking_submission_mode"] == "latest"
    # The same content read again is a no-op, even though the read happened later.
    again = judge_rules.save(ws, "Op", RULE, fetched_at="2026-11-27T00:00:00+00:00")
    assert again["saved"] is False and judge_rules.load(ws, "Op")["fetched_at"] == FETCHED
    changed = judge_rules.save(ws, "Op", dict(RULE, ranking_submission_mode="best"),
                               fetched_at="2026-11-27T00:00:00+00:00")
    assert changed["saved"] is True
    assert judge_rules.load(ws, "Op")["ranking_submission_mode"] == "best"
    assert judge_rules.board_modes(ws) == {"Op": "best"}


def test_refresh_reports_the_problems_it_could_not_read(tmp_path: Path, monkeypatch) -> None:
    ws = _workspace(tmp_path, [{"title": "Op", "path": "Op", "problem_id": "pid-1"},
                               {"title": "Op2", "path": "Op2"}])
    monkeypatch.setattr("opsb_core.services.orphan_receipts.resolve_sessions_for_account", lambda _a: [])
    out = judge_rules.refresh(ws)
    assert out["ok"] is True and out["logged_in"] is False
    assert out["missing"] == ["Op", "Op2"] and out["snapshots"] == []
    assert not judge_rules.path_for(ws, "Op").exists()  # a missing login writes nothing


def test_refresh_saves_with_the_platform_session_and_stays_idempotent(tmp_path: Path, monkeypatch) -> None:
    ws = _workspace(tmp_path, [{"title": "Op", "path": "Op", "problem_id": "pid-1"}])
    monkeypatch.setattr("opsb_core.services.orphan_receipts.resolve_sessions_for_account",
                        lambda _a: [("https://judge.invalid", "cookie-value-must-not-leak", "uid")])
    calls: list[str] = []

    def fetch(base: str, pid: str):
        calls.append(pid)
        return dict(RULE, _id=pid)

    out = judge_rules.refresh(ws, fetch=fetch)
    assert out["logged_in"] is True and calls == ["pid-1"]
    assert out["snapshots"][0] == {"title": "Op", "saved": True, "mode": "latest", "score_mode": 1,
                                   "cases": 3, "fetched_at": out["snapshots"][0]["fetched_at"]}
    assert judge_rules.refresh(ws, fetch=fetch)["snapshots"][0]["saved"] is False
    assert "cookie-value-must-not-leak" not in _text(judge_rules.load(ws, "Op"))


def test_refresh_keeps_going_after_one_unreadable_rule(tmp_path: Path, monkeypatch) -> None:
    ws = _workspace(tmp_path, [{"title": "Op", "path": "Op", "problem_id": "pid-1"},
                               {"title": "Op2", "path": "Op2", "problem_id": "pid-2"}])
    monkeypatch.setattr("opsb_core.services.orphan_receipts.resolve_sessions_for_account",
                        lambda _a: [("https://judge.invalid", "c", "uid")])

    def fetch(base: str, pid: str):
        if pid == "pid-1":
            raise OSError("HTTP 500")
        return dict(RULE, _id=pid)

    out = judge_rules.refresh(ws, fetch=fetch)
    assert [row["title"] for row in out["snapshots"]] == ["Op2"]
    assert out["errors"] == [{"title": "Op", "error": "OSError"}]


# ---------------------------------------------------------------- board-mode priority

def test_estimator_prefers_the_snapshot_over_the_evidence_and_the_manifest(tmp_path: Path) -> None:
    from scheduler.estimator import ScoreEstimator

    ws = _workspace(tmp_path, [
        {"title": "Op", "path": "Op", "problem_id": "pid-1"},          # snapshot says best
        {"title": "Op2", "path": "Op2", "problem_id": "pid-2"},         # only the evidence names it
        {"title": "Op3", "path": "Op3", "problem_id": "pid-3",
         "ranking_submission_mode": "best"},                            # only the manifest names it
    ])
    judge_rules.save(ws, "Op", dict(RULE, ranking_submission_mode="best"), fetched_at=FETCHED)
    evidence = ws / "results" / "scoring-evidence" / "problems-all.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text(json.dumps({
        "Op": {"title": "Op", "ranking_submission_mode": "latest"},     # loses to the snapshot
        "Op2": {"title": "Op2", "ranking_submission_mode": "latest"},   # beats the manifest
        "Op3": {"title": "Op3"},                                       # says nothing
    }), encoding="utf-8")
    meta = ScoreEstimator(ws).problems_meta
    assert meta["Op"]["ranking_submission_mode"] == "best"
    assert meta["Op2"]["ranking_submission_mode"] == "latest"
    assert meta["Op3"]["ranking_submission_mode"] == "best"
    # The manifest is untouched by the snapshot: the two-problem roster is still what it lists.
    assert set(meta) == {"Op", "Op2", "Op3"}


def test_estimator_reads_a_snapshot_when_there_is_no_manifest(tmp_path: Path) -> None:
    from scheduler.estimator import ScoreEstimator

    ws = tmp_path / "ws"
    (ws / "results").mkdir(parents=True)
    judge_rules.save(ws, "Op", dict(RULE, ranking_submission_mode="latest"), fetched_at=FETCHED)
    assert ScoreEstimator(ws).problems_meta["Op"]["ranking_submission_mode"] == "latest"


# ---------------------------------------------------------------- latest-mode replay

def _replay_fixture(mode: str = "latest") -> tuple[dict, dict, dict]:
    """One submission whose stored times and tbest reproduce its own judge score exactly."""
    times = {"#01": 290.0, "#02": 495.0, "#03": 690.0}
    tbest = {"#01": 280.0, "#02": 480.0, "#03": 680.0}
    score = receipts.judge_score(times, tbest)
    assert score is not None
    result = [{"testcase_id": key, "testcase_status": "Pass", "time": times[key], "score": 0}
              for key in times]
    rule = {"_id": "pid-1", "score_mode": 1, "use_baseline": False, "ranking_submission_mode": mode,
            "judge_revision": 7, "ranking_testcase_count": None,
            "testcases": [{"testcase_id": {"_id": key, "type": "default"}} for key in times]}
    ranking = {"testcases": [{"_id": key, "type": "default", "tbest": tbest[key]} for key in times],
               "total_testcase_count": len(times),
               "rows": [{"status": "Pass", "score": score, "result": result}]}
    detail = {"problem_id": "pid-1", "status": "Pass", "valid": True, "judge_revision": 7,
              "result": result}
    return detail, rule, ranking


def test_reconstruct_replays_latest_against_the_judge_score() -> None:
    detail, rule, ranking = _replay_fixture("latest")
    out = reconstruct_timing_score(detail, rule, ranking)
    assert out is not None and out["score"] == receipts.judge_score(
        {"#01": 290.0, "#02": 495.0, "#03": 690.0}, {"#01": 280.0, "#02": 480.0, "#03": 680.0})
    assert out["formula"] == "judge_log1p5_mean" and out["case_count"] == 3


def test_reconstruct_latest_can_be_switched_off(monkeypatch) -> None:
    detail, rule, ranking = _replay_fixture("latest")
    monkeypatch.setattr(judge_rules, "reconstruct_latest_enabled", lambda cfg=None: False)
    assert reconstruct_timing_score(detail, rule, ranking) is None
    rule["ranking_submission_mode"] = "best"  # the 4.2.1 path is untouched by the switch
    assert (reconstruct_timing_score(detail, rule, ranking) or {}).get("score") is not None


def test_reconstruct_latest_keeps_every_other_precondition(monkeypatch) -> None:
    detail, rule, ranking = _replay_fixture("latest")
    for name, mutate in (
        ("score_mode", lambda r: r.update(score_mode=2)),
        ("use_baseline", lambda r: r.update(use_baseline=True)),
        ("judge_revision", lambda r: r.update(judge_revision=6)),
        ("problem", lambda r: r.update(_id="other")),
    ):
        broken = dict(rule)
        mutate(broken)
        assert reconstruct_timing_score(detail, broken, ranking) is None, name
    assert reconstruct_timing_score(dict(detail, valid=False), rule, ranking) is None
    assert reconstruct_timing_score(detail, rule, {"testcases": [], "total_testcase_count": 3}) is None


def test_reconstruct_latest_switch_reads_the_config() -> None:
    assert judge_rules.reconstruct_latest_enabled({}) is True
    assert judge_rules.reconstruct_latest_enabled({"judge": {}}) is True
    assert judge_rules.reconstruct_latest_enabled({"judge": {"reconstruct_latest": True}}) is True
    assert judge_rules.reconstruct_latest_enabled({"judge": {"reconstruct_latest": False}}) is False
    assert judge_rules.reconstruct_latest_enabled({"judge": "not-a-block"}) is True


def test_best_score_bound_stays_best_only() -> None:
    detail = {"_id": "sid-2", "problem_id": "pid-1", "valid": True, "judge_revision": 7,
              "user_id": "me"}
    board = {"submission_id": "sid-1", "score": 20.0, "user_id": "me"}
    latest = {"_id": "pid-1", "ranking_submission_mode": "latest", "judge_revision": 7}
    best = dict(latest, ranking_submission_mode="best")
    assert _best_score_bound(detail, "sid-2", "pid-1", board, latest) is None
    assert _best_score_bound(detail, "sid-2", "pid-1", board, best) == {
        "score": 20.0, "board_submission_id": "sid-1"}


# ---------------------------------------------------------------- the rules view

def _receipt_workspace(tmp_path: Path, rows: list[dict], board_sid: str = "") -> Path:
    ws = _workspace(tmp_path, [{"title": "Op", "path": "Op", "problem_id": "pid-1"},
                               {"title": "Op2", "path": "Op2", "problem_id": "pid-2"}])
    journal = ws / "results" / "submit-queue-outcome.jsonl"
    journal.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    if board_sid:
        (ws / "results" / "score-snapshot.json").write_text(
            json.dumps({"Op": {"submission_id": board_sid, "status": "Pass", "score": 15.0}}),
            encoding="utf-8")
    return ws


def _pass_row(sid: str, score: float, times: dict[str, float], tbest: dict[str, float],
              public: list[float]) -> dict:
    return {"title": "Op", "sha": sid.replace("sub", "sha")[:12], "submission_id": sid, "status": "Pass",
            "score": score, "submitted_at": "2026-11-20T10:05:00+00:00",
            "case_results": [{"testcase_id": key, "status": "Pass", "time_us": times[key],
                              "tbest_us": tbest[key]} for key in times],
            "tbest_observed_at": "2026-11-20T10:06:00+00:00", "public_times_us": public}


def test_rules_view_reports_the_split_only_where_the_identity_holds(tmp_path: Path) -> None:
    times = {"#01": 290.0, "#02": 495.0, "#03": 690.0}
    tbest = {"#01": 280.0, "#02": 480.0, "#03": 680.0}
    score = receipts.judge_score(times, tbest)
    good = _pass_row("sub-good", score, times, tbest, [290.0, 495.0])
    drifted = _pass_row("sub-drift", 40.0, times, tbest, [290.0, 495.0])  # score from another tbest
    ws = _receipt_workspace(tmp_path, [drifted, good], board_sid="sub-good")
    judge_rules.save(ws, "Op", RULE, fetched_at=FETCHED)
    out = call_tool("parhelion_report", {"view": "rules", "problem": "Op"}, ws)
    assert out["view"] == "rules" and out["problem"] == "Op"
    assert out["rule"]["ranking_submission_mode"] == "latest" and out["rule"]["testcases"]["total"] == 3
    assert out["formula"].startswith("case = 100/")
    assert out["split"]["verified"] == {"rows": 1, "public_cases": 2, "max_err": 0.0}
    assert out["split"]["best"]["score"] == score and out["split"]["board"]["score"] == score
    assert out["split"]["best"]["public"] + out["split"]["best"]["hidden"] == score
    assert out["split"]["best"]["public"] > 0 and out["split"]["best"]["hidden"] > 0


def test_rules_view_omits_the_split_when_nothing_verifies(tmp_path: Path) -> None:
    times = {"#01": 290.0, "#02": 495.0, "#03": 690.0}
    tbest = {"#01": 280.0, "#02": 480.0, "#03": 680.0}
    ws = _receipt_workspace(tmp_path, [_pass_row("sub-drift", 40.0, times, tbest, [290.0, 495.0])])
    judge_rules.save(ws, "Op", RULE, fetched_at=FETCHED)
    out = call_tool("parhelion_report", {"view": "rules", "problem": "Op"}, ws)
    assert "split" not in out
    assert out["tbest"] == {"cases": 3, "observed_at": "2026-11-20T10:06:00",
                            "changed_at": None, "changed_cases": 0}


def test_rules_view_stays_inside_its_budget_and_names_the_missing_snapshot(tmp_path: Path) -> None:
    ws = _receipt_workspace(tmp_path, [])
    out = call_tool("parhelion_report", {"view": "rules", "problem": "Op"}, ws)
    assert "reason" in out and "split" not in out
    assert len(_text(out).encode("utf-8")) <= handlers_report.RULES_BYTES <= 2048
    judge_rules.save(ws, "Op", RULE, fetched_at=FETCHED)
    table = call_tool("parhelion_report", {"view": "rules"}, ws)
    assert table["view"] == "rules" and table["columns"][0] == "title"
    assert [row[0] for row in table["problems"]] == ["Op"]
    assert len(_text(table).encode("utf-8")) <= handlers_report.RULES_BYTES


def test_rules_view_serves_a_worker_only_its_own_problem(tmp_path: Path) -> None:
    import sqlite3

    from opsb_core.agents.bus_store import agent_bus_db_path

    ws = _receipt_workspace(tmp_path, [])
    for title, pid in (("Op", "pid-1"), ("Op2", "pid-2")):
        judge_rules.save(ws, title, dict(RULE, _id=pid), fetched_at=FETCHED)
    db = agent_bus_db_path(ws)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS agents (agent_id TEXT PRIMARY KEY, "
                     "problem TEXT NOT NULL DEFAULT '')")
        conn.execute("INSERT OR REPLACE INTO agents (agent_id, problem) VALUES ('w1', 'Op')")
        conn.commit()
    finally:
        conn.close()
    own = call_tool("parhelion_report", {"view": "rules"}, ws, role="worker", agent_id="w1")
    assert own["problem"] == "Op" and own["rule"]["ranking_submission_mode"] == "latest"
    with pytest.raises(PermissionError, match="staffed on Op"):
        call_tool("parhelion_report", {"view": "rules", "problem": "Op2"}, ws, role="worker", agent_id="w1")


# ---------------------------------------------------------------- wiring

def test_reconciler_snapshot_write_cannot_disturb_reconciliation(tmp_path: Path, monkeypatch) -> None:
    from opsb_core.services import sync_reconciler

    ws = _workspace(tmp_path, [{"title": "Op", "path": "Op", "problem_id": "pid-1"}])
    sync_reconciler._save_rule(ws, "Op", RULE)
    assert judge_rules.load(ws, "Op")["ranking_submission_mode"] == "latest"

    def explode(*_args, **_kwargs):
        raise OSError("read-only results directory")

    monkeypatch.setattr(judge_rules, "save", explode)
    sync_reconciler._save_rule(ws, "Op", RULE)  # a failed snapshot is not a failed reconcile


def test_status_brief_carries_one_judge_line(tmp_path: Path) -> None:
    """The brief line lives in judge_rules; handlers_bench only inserts it (4.3 P22)."""
    from opsb_core.mcp.handlers_bench import _status_payload

    ws = _workspace(tmp_path, [{"title": "Op", "path": "Op", "problem_id": "pid-1"},
                               {"title": "Op2", "path": "Op2", "problem_id": "pid-2"}])
    assert judge_rules.status_line(ws, []) == {}
    # A workspace without a snapshot says so, so the reader knows to run `opsb workspace rules`.
    assert judge_rules.status_line(ws, [{"title": "Op"}]) == {"board_mode": "unknown"}
    judge_rules.save(ws, "Op", dict(RULE, ranking_submission_mode="latest"), fetched_at=FETCHED)
    judge_rules.save(ws, "Op2", dict(RULE, _id="pid-2", ranking_submission_mode="best"), fetched_at=FETCHED)
    assert judge_rules.status_line(ws, [{"title": "Op"}]) == {"board_mode": "latest"}
    tally = judge_rules.status_line(ws, [{"title": "Op"}, {"title": "Op2"}])
    assert tally["board_modes"] == {"best": 1, "latest": 1}
    # The handler is a one-line access point: the same line, no logic of its own.
    payload = _status_payload({"problem": "Op"}, ws)
    assert payload["judge"] == judge_rules.status_line(
        ws, [{"title": row[0]} for row in payload["problems"]["rows"]])
