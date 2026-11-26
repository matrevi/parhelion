"""4.3 P24: judge-noise ledger rows (same-source verdict flips and unreproduced verdicts).

Temporary workspaces only; the WA/RE manifest and the formal runs are fixtures.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core.ledger import auto, store  # noqa: E402

WORKER = "dee-aaaaaa"
BAD, OTHER = "b" * 40, "c" * 40
SID = "6b07b8de85ad43bbe0ff92a5"
VARIANT_IDS = ["local/wa-e0ff92a5-00", "local/wa-e0ff92a5-01"]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, name: str, text: str, message: str) -> str:
    (repo / name).write_text(text)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def contest(tmp_path):
    ws = (tmp_path / "contest").resolve()
    repo = ws / "Op00"
    repo.mkdir(parents=True)
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "T"]):
        _git(repo, *args)
    first = _commit(repo, "kernel.cpp", "int main() { return 0; }\n", "baseline kernel")
    second = _commit(repo, "kernel.cpp", "int main() { return 1; }\n", "Tile the inner loop by 64")
    (ws / "opsb.workspace.json").write_text(json.dumps({
        "layout": "clean-agents-first", "problems": [{"title": "Op00", "path": "Op00", "difficulty": 2}]}))
    return {"ws": ws, "repo": repo, "first": first, "second": second}


def _receipt(sid: str, sha: str, status: str = "Pass", score=72.0, **over) -> dict:
    return {"submission_id": sid, "title": "Op00", "sha": sha, "status": status,
            "score": score if status == "Pass" else 0,
            "submitted_at": "2026-01-02T03:00:00+00:00", "finished_at": "2026-01-02T03:05:00+00:00", **over}


def _journal(ws: Path, rows: list[dict]) -> None:
    path = ws / "results" / "submit-queue-outcome.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _record(sha: str, rid: str = "a" * 32) -> dict:
    return {"request_id": rid, "problem": "Op00", "sha": sha, "level": "final", "status": "finished",
            "digest": {"level": "final", "summary": {"total": 1, "passed": 1, "counts": {"PASS": 1}},
                       "timing": {"cases": {"01": {"min_us": 9.2}}}, "failures": []},
            "agent_id": WORKER, "error": None}


def _noise_rows(ws: Path) -> list[dict]:
    return [row for row in store.Ledger(ws, "Op00", auto.PLATFORM_WORKER).rows()
            if row.get("judge_noise")]


def test_a_same_source_verdict_flip_is_one_idempotent_row(contest):
    ws, sha = contest["ws"], contest["second"]
    rows = [_receipt("s1", sha), _receipt("s2", sha, status="Wrong Answer")]
    _journal(ws, rows)
    auto.on_receipt(ws, "Op00", rows[1], WORKER)
    noise = _noise_rows(ws)
    assert len(noise) == 1
    row = noise[0]
    assert row["type"] == "measurement" and row["source"] == "platform"
    assert row["judge_noise"]["kind"] == "flip" and row["judge_noise"]["from"] == "Pass"
    assert row["judge_noise"]["to"] == "Wrong Answer" and row["receipt_id"] == "s2"
    assert row["ekey"] == "judge-noise:s2:flip" and row["key"] == "judge.noise.flip"
    path = ws / "results/ledger/Op00" / f"{auto.PLATFORM_WORKER}.jsonl"
    before = path.read_bytes()
    for _ in range(3):  # replayed batch, a daemon restart, a reconciler pass
        auto.on_judge_noise(ws, "Op00")
        auto.on_receipt(ws, "Op00", rows[1], WORKER)
    assert path.read_bytes() == before


def test_the_flip_back_is_its_own_row(contest):
    ws, sha = contest["ws"], contest["second"]
    rows = [_receipt("s1", sha), _receipt("s2", sha, status="Wrong Answer"),
            _receipt("s3", sha, submitted_at="2026-01-02T05:00:00+00:00")]
    _journal(ws, rows)
    auto.on_judge_noise(ws, "Op00")
    noise = sorted(_noise_rows(ws), key=lambda row: row["receipt_id"])
    assert [row["judge_noise"]["kind"] for row in noise] == ["flip", "flip"]
    assert [row["judge_noise"]["to"] for row in noise] == ["Wrong Answer", "Pass"]


def _manifest_and_runs(ws: Path) -> str:
    from scheduler import wa_repro

    path = wa_repro.manifest_path(ws, "Op00")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"receipts": {SID: {
        "sha": BAD, "status": "Wrong Answer", "variants": VARIANT_IDS, "failing": [], "reason": ""}}}))
    for run, finished in (("r1", "2026-01-02T04:00:00+00:00"), ("r2", "2026-01-02T05:00:00+00:00")):
        folder = ws / "results" / "Op00" / BAD / "bench-runs" / run
        folder.mkdir(parents=True)
        (folder / "report.json").write_text(json.dumps({
            "title": "Op00", "sha": BAD, "run_id": run, "formal": True, "level": "final",
            "finished_at": finished, "settings": {"reuse_session": False},
            "cases": [{"id": cid, "status": "PASS", "key": f"key-{cid}"} for cid in VARIANT_IDS]}))
    return SID


def test_an_unreproduced_verdict_is_one_idempotent_row(contest):
    ws = contest["ws"]
    _manifest_and_runs(ws)
    auto.on_test_settled(ws, _record(contest["second"]))
    noise = _noise_rows(ws)
    assert len(noise) == 1
    row = noise[0]
    assert row["judge_noise"]["kind"] == "unreproduced" and row["judge_noise"]["runs"] == 2
    assert row["value"] == 2 and row["unit"] == "count" and row["receipt_id"] == SID
    assert row["ekey"] == f"judge-noise:{SID}:unreproduced" and "judge-side noise" in row["why"]
    path = ws / "results/ledger/Op00" / f"{auto.PLATFORM_WORKER}.jsonl"
    before = path.read_bytes()
    for _ in range(3):
        auto.on_judge_noise(ws, "Op00")
        auto.on_test_settled(ws, _record(contest["second"], rid="f" * 32))
    assert path.read_bytes() == before


def test_one_formal_run_does_not_make_it_unreproduced(contest):
    ws = contest["ws"]
    _manifest_and_runs(ws)
    (ws / "results/Op00" / BAD / "bench-runs" / "r2" / "report.json").unlink()
    auto.on_judge_noise(ws, "Op00")
    assert _noise_rows(ws) == []


def test_the_rules_view_shows_the_flip_count(contest):
    from opsb_core.mcp import report_views

    ws, sha = contest["ws"], contest["second"]
    (ws / "results" / "Op00").mkdir(parents=True, exist_ok=True)
    (ws / "results" / "Op00" / ".judge-rule.json").write_text(json.dumps({
        "schema_version": 1, "ranking_submission_mode": "latest", "score_mode": 1, "iterations": 5,
        "fetched_at": "2026-01-02T00:00:00+00:00"}))
    _journal(ws, [_receipt("s1", sha), _receipt("s2", sha, status="Wrong Answer")])
    out = report_views._rules(ws, "Op00", {}, "orchestrator")
    assert out["flips"] == 1 and out["view"] == "rules"


def test_the_submit_gate_reply_carries_the_health_view(contest):
    from opsb_core.mcp import handlers_bench

    ws, sha = contest["ws"], contest["second"]
    health = {"checked": True, "metrics": {"compile_sec": 12.5}, "warnings": [{"code": "COMPILE_TIME_WARN"}],
              "time_risk": {"risk": False}, "status": "warn"}
    verdict = {"approved": True, "priority_score": 1.0, "status_tag": "READY",
               "stages": [{"stage": "SAFETY", "passed": True, "reason": "", "code": "PASS",
                           "metrics": {"health": health}},
                          {"stage": "NOISE_FLOOR", "passed": True, "reason": "", "code": "PASS", "metrics": {}},
                          {"stage": "CIRCUIT_BREAKER", "passed": True, "reason": "", "code": "PASS", "metrics": {}},
                          {"stage": "QUOTA_RESERVE", "passed": True, "reason": "", "code": "PASS", "metrics": {}}],
               "estimate": {"estimated_score": 75.0, "current_score": 72.0, "delta_expected": 3.0}}
    with mock.patch.object(handlers_bench, "invoke_bridge", return_value=verdict):
        out = handlers_bench.tool_parhelion_submit_gate({"problem": "Op00", "sha": sha}, ws)
    assert out["health"] == health and out["stages"][0]["metrics"]["health"] == health
