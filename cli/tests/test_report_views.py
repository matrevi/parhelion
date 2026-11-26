"""Tests of the ``parhelion_report`` views (P12): fixtures, fixed columns, budgets, roles, no network."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core import config as _cfg  # noqa: E402
from opsb_core.mcp import handlers_report  # noqa: E402
from opsb_core.mcp.tools import call_tool, list_tool_schemas  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "p12_report"
DEVICE_VALUES = ("soc-a", "arch-v1", "sdk-x", "npu")


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _commit(repo: Path, text: str, message: str) -> str:
    (repo / "kernel.asc").write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", message], check=True)
    return _git(repo, "rev-parse", "HEAD")


def build_fixture(tmp_path: Path) -> tuple[Path, dict[str, tuple[str, str]]]:
    """Copy the de-identified fixture, give each problem git history, and name the runs after it."""
    ws = tmp_path / "ws"
    shutil.copytree(FIXTURE, ws)
    shas: dict[str, tuple[str, str]] = {}
    for repo in (ws / "Op", ws / "Op2"):
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        _git(repo, "config", "user.email", "fixture@example.invalid")
        _git(repo, "config", "user.name", "Fixture")
        base = _commit(repo, "base\n", "base")
        cand = _commit(repo, "candidate\n", "candidate")
        old_base, old_cand = ("sha-base", "sha-cand") if repo.name == "Op" else ("sha-b2", "sha-c2")
        root = ws / "results" / repo.name
        (root / old_base).rename(root / base)
        (root / old_cand).rename(root / cand)
        for path in root.glob("*/bench-runs/*/report.json"):
            doc = json.loads(path.read_text(encoding="utf-8"))
            doc["sha"] = base if doc["sha"] == old_base else cand if doc["sha"] == old_cand else doc["sha"]
            if isinstance(doc.get("compare"), dict) and doc["compare"].get("base_sha") == old_base:
                doc["compare"]["base_sha"] = base
            path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        receipts = ws / "results" / "submit-queue-outcome.jsonl"
        rows = [json.loads(line) for line in receipts.read_text().splitlines() if line.strip()]
        for row in rows:
            row["sha"] = base if row.get("sha") == old_base else cand if row.get("sha") == old_cand else row.get("sha")
        receipts.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
        shas[repo.name] = (base, cand)
    return ws, shas


@pytest.fixture()
def contest(tmp_path: Path) -> tuple[Path, dict[str, tuple[str, str]]]:
    return build_fixture(tmp_path)


def _report(ws: Path, **args) -> dict:
    return call_tool("parhelion_report", args, ws)


def _text(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _write_wa_manifest(ws: Path, receipt: str, local: str, judge: str) -> None:
    manifest = _cfg.workspace_data_dir(ws) / "bench-specs" / "Op" / "wa-repro.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"receipts": {receipt: {
        "status": "Wrong Answer", "bases": [{"local": local, "judge": judge, "mapped": True}],
        "variants": ["local/wa-fixture-00"], "failing": [judge]}}}), encoding="utf-8")


def test_per_case_fixed_columns_both_shapes_and_budget(contest) -> None:
    ws, shas = contest
    base, cand = shas["Op"]
    out = _report(ws, view="per_case", problem="Op")
    assert out["view"] == "per_case" and out["problem"] == "Op"
    assert out["sha"] == cand[:7] and out["base"] == base[:7] and out["head"] == cand[:7]
    assert list(out["columns"]) == list(handlers_report.PER_CASE_COLUMNS)
    assert len(_text(out).encode("utf-8")) <= handlers_report.BIG
    rows = [dict(zip(out["columns"], row)) for row in out["cases"]]
    assert [r["case"] for r in rows] == ["L01", "L02", "L03"]  # judge case -> local label from the map
    assert all(r["mapped"] is True and r["judge"].startswith("#") for r in rows)
    assert [r["passed"] for r in rows] == [True, False, True]
    assert [r["mismatch_n"] for r in rows] == [None, 1, None]
    assert [r["timer"] for r in rows] == ["event", "event", "event"]
    # The suite-level SHA and the legacy single bench-run are both read (the base has no min_us).
    assert [r["speedup"] for r in rows] == [1.1, 0.9756, 1.03]
    assert out["speedup_confidence"] == "inferred"
    assert {r["verdict"] for r in rows} <= {"improved", "indistinguishable", "regressed"}
    assert rows[1]["verdict"] == "regressed" and rows[0]["verdict"] == "improved"
    assert all(isinstance(r["band_pct"], float) for r in rows)
    assert rows[0]["cal"] == "noisy" and rows[1]["cal"] is None  # calibration labels from the stored records
    assert all(r["strict_fp32"] is True and r["run"] == "candrun" for r in rows)
    assert rows[0]["gap_pct"] == 3.57
    for value in DEVICE_VALUES:
        assert value not in _text(out)


def test_per_case_wa_repro_marker(contest) -> None:
    ws, _ = contest
    _write_wa_manifest(ws, "sub-wa", local="02", judge="#02")
    out = _report(ws, view="per_case", problem="Op")
    rows = [dict(zip(out["columns"], row)) for row in out["cases"]]
    assert out["wa_state"] == "pending" and out["wa_receipt"]
    assert [r["wa"] for r in rows] == ["", "pending", ""]


def test_diff_prefers_the_stored_paired_report(contest) -> None:
    ws, shas = contest
    base, cand = shas["Op"]
    out = _report(ws, view="diff", problem="Op")
    assert out["source"] == "paired-report" and out["sha"] == cand[:7] and out["base"] == base[:7]
    assert out["geomean"] == 1.0183
    rows = [dict(zip(out["columns"], row)) for row in out["cases"]]
    assert [r["case"] for r in rows] == ["01", "02", "03"]
    assert rows[0]["ratio"] == 1.3262 and rows[0]["lo"] == 0.4793 and rows[0]["hi"] == 3.6695
    assert rows[0]["bootstrap"] == [0.6996, 2.5141] and rows[0]["method"] == "log_t"
    assert rows[1]["verdict"] == "improved" and rows[2]["verdict"] == "regressed"
    assert out["git"]["stat"] and out["git"]["stat"][0].startswith("kernel.asc")
    assert len(_text(out).encode("utf-8")) <= handlers_report.BIG


def test_diff_separate_runs_use_welch_interval_and_legacy_minima(contest) -> None:
    ws, shas = contest
    base, cand = shas["Op2"]
    out = _report(ws, view="diff", problem="Op2", sha=cand, base=base)
    assert out["source"] == "separate-runs" and out["geomean"] is None
    rows = [dict(zip(out["columns"], row)) for row in out["cases"]]
    assert [r["case"] for r in rows] == ["01", "02"]
    assert rows[0]["ratio"] == 1.2 and rows[0]["method"] == "welch_log" and rows[0]["timer"] == "event"
    assert rows[0]["verdict"] == "improved" and rows[0]["lo"] > 1.0
    assert len(out["git"]["stat"]) == 2


def test_history_canonical_receipts_and_per_case_ratios(contest) -> None:
    ws, shas = contest
    base, cand = shas["Op"]
    out = _report(ws, view="history", problem="Op")
    problem = out["problems"][0]
    assert problem["title"] == "Op" and problem["judged"] == 3 and problem["rows"] == 4
    assert problem["best_judged"] == 15.4 and problem["latest"]["status"] == "Wrong Answer"
    receipts = {row["sid"]: row for row in out["receipts"]}
    assert set(receipts) == {"sub-base", "sub-cand", "sub-wa", ""}
    assert receipts["sub-cand"]["beat_board"] is False and receipts["sub-base"]["beat_board"] is True
    assert receipts["sub-cand"]["score"] == 15.4 and receipts["sub-cand"]["sha"] == cand[:7]
    assert receipts["sub-cand"]["cases"][0] == ["#01", 290.0, 280.0, 0.966]
    assert receipts["sub-cand"]["mean_ratio"] == 0.974
    assert receipts["sub-wa"]["status"] == "Wrong Answer" and receipts["sub-wa"]["score"] == 0.0
    assert receipts[""]["status"] == "rejected" and receipts[""]["score"] is None
    assert receipts["sub-base"]["sha"] == base[:7]


def _queue_snapshot(ws: Path) -> tuple[dict, dict]:
    inflight = {"id": "live-Op-abc1234", "kind": "inflight", "title": "Op", "sha": "abc1234def5678", "short_sha": "abc1234",
                "badge": "JUDGING", "raw_status": "judging", "submission_id": "sub-inflight", "queued_at": "2026-11-20T11:00:00",
                "current_score": 15.4, "actual_score": None, "delta_score": None, "breakthrough_delta": None,
                "account_id": "acct-secret", "account_label": "acct-label-secret", "remote_judge_id": "judge-1",
                "can_approve": False, "can_skip": False, "can_restore": False,
                "gates": {"evaluated": True, "safety_ok": True, "noise_ok": True, "circuit_ok": True, "all_pass": True,
                          "dispatch_ok": True, "delta_expected": 0.0, "wspt_score": 0.0}}
    snap = {"sampled_at": "2026-11-20T12:00:00+00:00",
            "contest": {"workspace": str(ws), "title": "P12 fixture"},
            "scheduler": {"breakthrough_threshold": 5.0, "pipeline": {"active_submissions": [{"title": "Op"}]}},
            "problems": [{"title": "Op", "difficulty": 2, "current_score": 15.4, "best_score": 15.4, "board_listed": True,
                          "board_submission_id": "sub-cand", "board_score": 15.4, "calibration": {}}],
            "submission_queue": [inflight]}
    stages = [{"stage": name, "passed": True} for name in ("SAFETY", "NOISE_FLOOR", "CIRCUIT_BREAKER", "QUOTA_RESERVE")]
    bridge = {"generated_at": "2026-11-20T12:00:00+00:00", "submit_queue": [], "candidate_archive": {},
              "unsubmitted_candidates": [{"title": "Op", "sha": "fedcba987654", "status_tag": "READY",
                                          "estimate": {"estimated_score": 20.0, "current_score": 15.4, "delta_expected": 4.6},
                                          "stages": stages, "priority_score": 9.2}]}
    return snap, bridge


def test_queue_merges_gate_view_and_drops_account_fields(contest, monkeypatch) -> None:
    ws, _ = contest
    snap, bridge = _queue_snapshot(ws)
    monkeypatch.setattr("opsb_core.services.queue_service.compute_workspace_snapshot", lambda workspace, cfg: snap)
    monkeypatch.setattr("opsb_core.ipc_bridge.invoke_bridge", lambda *args, **kwargs: bridge)
    out = _report(ws, view="queue", problem="Op")
    text = _text(out)
    assert out["count"] == 2 and out["by_kind"] == {"inflight": 1, "candidate": 1}
    assert out["active_submissions"] == 1 and out["threshold"] == 5.0
    for forbidden in ("account_id", "account_label", "acct-secret", "acct-label-secret", "remote_judge_id"):
        assert forbidden not in text
    candidate = next(item for item in out["items"] if item["kind"] == "candidate")
    assert candidate["badge"] == "READY" and candidate["gates"]["evaluated"] is True
    assert candidate["gates"]["safety_ok"] is True and candidate["gates"]["delta_expected"] == 4.6
    assert candidate["next_step"] == "" or isinstance(candidate["next_step"], str)


def test_board_reads_caches_only(contest, monkeypatch) -> None:
    ws, _ = contest

    def no_network(*args, **kwargs):
        raise AssertionError("the board view must not touch the network")

    monkeypatch.setattr("urllib.request.urlopen", no_network)
    out = _report(ws, view="board", problem="Op")
    text = _text(out)
    problem = out["problems"][0]
    assert problem["score"] == 15.4 and problem["rank"] == 57 and problem["total_teams"] == 91
    assert problem["frontier"] == {"top1": 88.0, "top5": 70.0}
    assert problem["observed_at"] == "2026-11-20T12:00:00"
    assert [case["tbest_us"] for case in problem["tbest"]] == [1.2, 2.3, 3.4]
    assert out["team"] == {"rank": 57, "score": 44.5, "total": 91, "state": None, "fetched_at": "2026-11-20T12:00:00"}
    for forbidden in ("Fixture Team", "acct-fixture", "user-fixture", "team-fixture"):
        assert forbidden not in text
    every = _report(ws, view="board")
    assert len(every["problems"]) == 2 and "tbest" not in every["problems"][0]


def test_bottleneck_card_kb_fixes_and_raw_paths(contest, monkeypatch) -> None:
    ws, shas = contest
    base, cand = shas["Op"]
    card = {"title": "Op", "sha": cand, "dominant_bottleneck": "mte2-bound", "pmu_available": True,
            "runtime_soc": "soc-a",
            "pmu": {"aiv_vec_ratio": 0.1, "aiv_mte2_ratio": 0.62, "aiv_mte3_ratio": 0.2, "aiv_scalar_ratio": 0.05,
                    "bank_conflict_ratio": 0.0},
            "top_buckets": [{"bucket": f"b{i}", "ours_us": 10.0 + i, "torch_us": 5.0, "ratio_vs_torch": 2.0,
                             "headroom_score": 0.5} for i in range(4)]}
    monkeypatch.setattr("opsb_core.fast_feedback.pmu_torch_card.build_pmu_torch_card",
                        lambda workspace, title, sha=None, top_n=5: card)
    monkeypatch.setattr("opsb_core.knowledge.query.query_kb",
                        lambda **kwargs: {"results": [{"id": f"K{i}", "title": f"fix {i}", "confidence": 0.7,
                                                       "summary": "use a wider copy"} for i in range(3)]})
    out = _report(ws, view="bottleneck", problem="Op")
    text = _text(out)
    assert out["category"] == "mte2-bound" and out["pmu_available"] is True
    assert out["limited_resource_pct"] == 62.0 and out["pmu"]["aiv_mte2_ratio"] == 62.0
    assert len(out["top_metrics"]) == 3 and out["top_metrics"][0]["bucket"] == "b0"
    assert out["fix_columns"] == ["id", "title", "confidence", "summary"]
    assert [fix[0] for fix in out["fixes"]] == ["K0", "K1", "K2"]
    assert out["raw"]["report"].endswith("run-cand/report.json")
    assert out["raw"]["run_dir"].endswith(cand) and out["raw"]["log"] is None
    assert "runtime_soc" not in text and "soc-a" not in text


def test_env_is_small_and_never_carries_credentials(contest, monkeypatch, tmp_path) -> None:
    ws, _ = contest
    state = tmp_path / "daemon.json"
    state.write_text(json.dumps({"pid": os.getpid(), "token": "secret-token", "port": 19999,
                                 "started_at": "2026-11-20T09:00:00+00:00"}), encoding="utf-8")
    monkeypatch.setattr(_cfg, "DAEMON_STATE_PATH", state)
    out = _report(ws, view="env")
    text = _text(out)
    assert len(text.encode("utf-8")) <= handlers_report.ENV_BYTES
    assert out["view"] == "env" and out["platform"]["version"]
    assert out["workspace"]["root"] == str(ws.resolve()) and out["workspace"]["results"].endswith("results")
    assert out["daemon"]["pid"] == os.getpid() and out["daemon"]["started_at"] == "2026-11-20T09:00:00"
    assert "secret-token" not in text and "port" not in text and "token" not in text
    monkeypatch.setattr(_cfg, "DAEMON_STATE_PATH", tmp_path / "missing.json")
    assert _report(ws, view="env")["daemon"]["running"] is False


def _bind_worker(ws: Path, agent_id: str, problem: str) -> None:
    from opsb_core.agents.bus_store import agent_bus_db_path

    db = agent_bus_db_path(ws)
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS agents (agent_id TEXT PRIMARY KEY, problem TEXT NOT NULL DEFAULT '')")
        conn.execute("INSERT OR REPLACE INTO agents (agent_id, problem) VALUES (?, ?)", (agent_id, problem))
        conn.commit()
    finally:
        conn.close()


def test_worker_reads_only_its_own_problem(contest) -> None:
    ws, _ = contest
    _bind_worker(ws, "worker-1", "Op")
    own = call_tool("parhelion_report", {"view": "per_case"}, ws, role="worker", agent_id="worker-1")
    assert own["problem"] == "Op"
    with pytest.raises(PermissionError, match="staffed on Op"):
        call_tool("parhelion_report", {"view": "per_case", "problem": "Op2"}, ws, role="worker", agent_id="worker-1")
    # board and env stay open to workers, still scoped to their own problem
    board = call_tool("parhelion_report", {"view": "board"}, ws, role="worker", agent_id="worker-1")
    assert [row["title"] for row in board["problems"]] == ["Op"]
    assert call_tool("parhelion_report", {"view": "env"}, ws, role="worker", agent_id="worker-1")["view"] == "env"
    with pytest.raises(PermissionError):
        call_tool("parhelion_report", {"view": "board", "problem": "Op2"}, ws, role="worker", agent_id="worker-1")
    with pytest.raises(PermissionError):
        call_tool("parhelion_report", {"view": "env"}, ws, role="worker", agent_id="unbound-worker")


def test_report_tool_is_registered_for_both_roles() -> None:
    orchestrator = {tool["name"]: tool for tool in list_tool_schemas("orchestrator")}
    worker = {tool["name"]: tool for tool in list_tool_schemas("worker")}
    assert "parhelion_report" in orchestrator and "parhelion_report" in worker
    assert orchestrator["parhelion_report"]["inputSchema"]["required"] == ["view"]
    views = orchestrator["parhelion_report"]["inputSchema"]["properties"]["view"]["enum"]
    assert views == list(handlers_report.VIEWS)
    for view in views:
        assert view in orchestrator["parhelion_report"]["description"]


def test_trim_enforces_the_byte_budget() -> None:
    doc = {"cases": [["case-%02d" % i, i, i, i, i, i, i, False, "j", True, "l", "event", True, None, None, True,
                      None, "", "run"] for i in range(200)]}
    handlers_report._trim(doc, "cases", handlers_report.BIG)
    assert handlers_report._bytes(doc) <= handlers_report.BIG
    assert doc["truncated"] is True and doc["shown"] == len(doc["cases"]) < 200
    with pytest.raises(ValueError, match="view must be one of"):
        call_tool("parhelion_report", {"view": "nope"}, Path("/tmp"))
