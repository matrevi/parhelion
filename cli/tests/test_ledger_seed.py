"""4.2 C #5 #7 and 4.1 #13 #14 (P06): the code-assembled seed, its budget, the compaction boundaries
and the two state cards.

Temporary workspaces only; no model API, daemon, device or real contest data.
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

from opsb_core.agents import compact_window  # noqa: E402
from opsb_core.agents.manager import compact_threshold  # noqa: E402
from opsb_core.ledger import checkpoint, digest, handoff, seed, store, view  # noqa: E402
from opsb_core.services import state_card, state_card_ext  # noqa: E402

WORKER = "dee-aaaaaa"
HEADINGS = "\n".join(f"## {h}\nsome line about the work" for h in handoff.HEADINGS)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def contest(tmp_path):
    ws = (tmp_path / "contest").resolve()
    repo = ws / "Op00"
    (repo / "src").mkdir(parents=True)
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "T"]):
        _git(repo, *args)
    (repo / "src" / "kernel.cpp").write_text("int main() { return 0; }\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "baseline kernel")
    sha = _git(repo, "rev-parse", "HEAD")
    (ws / "opsb.workspace.json").write_text(json.dumps({
        "layout": "clean-agents-first", "problems": [{"title": "Op00", "path": "Op00", "difficulty": 2}]}))
    (ws / "AGENTS.md").write_text(
        "# Contest guide\n\n## Scoring workflow\nUse the gate.\n\n## Rules\n- Never ssh to the device host.\n"
        "- One device job at a time.\n")
    return {"ws": ws, "repo": repo, "sha": sha}


def _rows(ws: Path) -> list[dict]:
    led = store.Ledger(ws, "Op00", WORKER)
    led.append("hypothesis", {"claim": "tile 64 helps case 05", "priority": 1, "scope": {"cases": ["05"]},
                              "predicted_effect": {"delta_mu": 1.2, "delta_sigma": 0.4}}, source="worker")
    for i in range(2):
        led.append("hypothesis", {"claim": f"minor idea {i}", "priority": 4}, source="worker")
    led.append("insight", {"claim": "cases 01-04 are within 5% of tbest", "cases": ["01"],
                           "actionable_next": "leave them alone"}, source="worker")
    for i in range(3):
        led.append("failure", {"claim": f"dead end {i}", "approach": f"double-buffer {i}",
                               "why_failed": "the kernel is DMA-bound on B",
                               "scope": {"cases": ["03"]}}, source="worker")
    led.append("measurement", {"claim": "smoke run", "key": "test.min_us", "value": 9.2, "unit": "us",
                               "test_level": "smoke", "per_case_us": {"01": 9.2, "02": 4.1},
                               "noise_band": {"drift": 1.01, "noisy": False}}, source="platform")
    return led.rows()


# ---------------------------------------------------------------- #5 the seed

def test_seed_sections_come_in_order_and_end_with_the_action(contest):
    ws = contest["ws"]
    rows = _rows(ws)
    text = seed.build(ws, "Op00", WORKER, rows, worktree=contest["repo"])
    order = [text.index(h) for h in (seed.HEADERS["rules"], seed.HEADERS["state"], seed.HEADERS["ledger"])]
    assert order == sorted(order)
    assert text.rstrip().endswith(seed.ACTION)
    assert "Never ssh to the device host." in text  # the contest rules section verbatim
    assert state_card.RULES_LINE in text  # the platform rules line
    assert "L-0001 [open p1]" in text and "L-0004 cases 01" in text
    assert "Recent dead ends" in text and "parhelion_ledger(view=failures)" not in text.split("Recent dead ends")[0]
    assert "double-buffer 2" in text  # newest dead end first
    assert "Latest test" in text and "noise" in text
    assert "git status" not in text  # a clean worktree prints nothing
    assert "Take the highest-priority open hypothesis" in text


def test_worktree_block_names_the_edited_files_verbatim(contest):
    repo = contest["repo"]
    (repo / "src" / "kernel.cpp").write_text("int main() { return 1; }\n")
    block = seed.worktree_state(repo)
    assert "git status:" in block and "git diff --stat:" in block
    assert "edited: src/kernel.cpp" in block
    (repo / "src" / "notes with a space.md").write_text("x\n")
    assert '"src/notes with a space.md"' in seed.worktree_state(repo)
    assert seed.worktree_state("/nonexistent/path") == ""


def test_rules_block_is_quoted_verbatim(contest):
    ws = contest["ws"]
    (ws / "AGENTS.md").write_text("# Contest\n\n## Rules\n- One device job at a time.\n- Never ssh.\n")
    (rules,) = seed.rules_text(ws)
    assert rules == "## Rules\n- One device job at a time.\n- Never ssh."
    assert seed.rules_text(ws / "missing") == []


def test_seed_keeps_open_ids_and_rules_when_the_budget_is_tiny(contest):
    ws = contest["ws"]
    rows = _rows(ws)
    full = seed.build(ws, "Op00", WORKER, rows, worktree=contest["repo"], budget=8000)
    tight = seed.build(ws, "Op00", WORKER, rows, worktree=contest["repo"], budget=1200)
    assert len(full) <= 8000 and len(tight) <= 1200 and len(tight) < len(full)
    for ref in (r["id"] for r in view.current(rows) if r["type"] in ("hypothesis", "insight")):
        assert ref in tight  # an open id is never dropped
    assert "Never ssh to the device host." in tight  # the fixed rules stay
    assert seed.ACTION in tight


def test_seed_truncation_order_is_tail_then_failures(contest):
    ws = contest["ws"]
    rows = _rows(ws)
    led = store.Ledger(ws, "Op00", WORKER)
    for i in range(40):  # plenty of dead ends so the failure list is the second thing to go
        led.append("failure", {"claim": f"dead end {i}", "approach": f"approach {i}", "why_failed": "no gain",
                               "scope": {"cases": ["03"], "bucket": "memory"}}, source="worker")
    rows = led.rows()
    tail = "LAST ROUND MARKER " * 40
    want_tail = seed.assemble(rules=["rule one"], card="STATE card", ledger="LEDGER", narrative="narrative",
                              tail=tail, worktree="git status:\n M src/kernel.cpp", budget=4000)
    assert want_tail.rstrip().endswith(seed.ACTION)
    assert "M src/kernel.cpp" in want_tail
    tight = seed.assemble(rules=["rule one"], card="STATE card", ledger="LEDGER", narrative="narrative",
                          tail=tail, worktree="git status:\n M src/kernel.cpp", budget=420)
    assert "LAST ROUND MARKER" not in tight  # the raw tail goes first
    assert "rule one" in tight and "STATE card" in tight and seed.ACTION in tight
    assert len(tight) <= 420
    assert len(want_tail) <= 4000


def test_seed_digest_falls_back_to_ids(contest):
    ws = contest["ws"]
    rows = _rows(ws)
    block = digest.render(rows, own=WORKER, max_chars=10_000)
    assert "tile 64 helps case 05" in block
    bare = digest.render(rows, own=WORKER, max_chars=60)
    assert "tile 64 helps case 05" not in bare
    for ref in (r["id"] for r in view.current(rows) if r["type"] in ("hypothesis", "insight")):
        assert ref in bare
    assert digest.ids_missing(block, rows, WORKER) == []
    assert digest.ids_missing("nothing", rows, WORKER)


def test_seed_caps_the_live_items_at_the_r4_budget(contest):
    ws = contest["ws"]
    led = store.Ledger(ws, "Op00", WORKER)
    for i in range(30):
        led.append("hypothesis", {"claim": f"live hypothesis {i} with a reasonably long claim text",
                                  "priority": 1 + i % 5}, source="worker")
    rows = led.rows()
    block = digest.render(rows, own=WORKER, max_chars=10_000)
    detailed = [ln for ln in block.splitlines() if ln.startswith("- ") and "claim text" in ln]
    assert len(detailed) <= digest.LIVE_DETAIL_MAX
    assert block.count("- L-") >= 30  # every id still appears, most as a bare id


def test_rules_block_is_pinned_as_constraints_once(contest):
    ws = contest["ws"]
    led = store.Ledger(ws, "Op00", WORKER)
    rules = seed.rules_text(ws) + seed.admin_directives(ws, WORKER) + [seed.platform_rules()]
    seed.pin(led, rules)
    seed.pin(led, rules)
    pinned = [r for r in led.rows() if r["type"] == "constraint"]
    assert len(pinned) == len(rules) and all(r["text"] for r in pinned)
    assert any("Never ssh" in r["text"] for r in pinned)


def test_seed_scheme_selection_and_defaults(tmp_path):
    assert seed.scheme({}) == seed.CORE
    assert seed.scheme({"agents": {"seed_scheme": "ledger_full"}}) == seed.FULL
    assert seed.scheme({"agents": {"seed_scheme": "legacy"}}) == seed.LEGACY
    assert seed.scheme({"agents": {"seed_scheme": "bogus"}}) == seed.DEFAULT_SCHEME
    assert seed.DEFAULT_SCHEME == seed.CORE  # the P06 replay tied current, so the default did not move


@pytest.mark.parametrize("scheme,expect_summary", [(seed.LEGACY, False), (seed.CORE, True), (seed.FULL, False)])
def test_handoff_seed_follows_the_scheme(contest, scheme, expect_summary):
    ws, repo = contest["ws"], contest["repo"]
    _rows(ws)
    session = handoff.HandoffSession(ws, "Op00", WORKER, repo)
    with mock.patch("opsb_core.ledger.seed.scheme", return_value=scheme):
        result = session.build_seed(HEADINGS, "Last reported result: n/m passed", kind="acp")
    assert result.status == "ok"
    lines = result.text.splitlines()
    assert lines[0].startswith("Preserve this checkpoint")
    assert ("LEDGER SUMMARY" in result.text) is expect_summary
    if scheme == seed.FULL:
        assert result.text.index(seed.HEADERS["state"]) < result.text.index(seed.HEADERS["ledger"])
        assert result.text.rstrip().endswith(seed.ACTION)
        assert "L-0001" in result.text
        assert not any("Do not re-read files you already summarized" in ln for ln in lines)
    if scheme == seed.LEGACY:
        assert "LEDGER SUMMARY" not in result.text and seed.ACTION not in result.text


def test_codex_ledger_seed_follows_the_scheme(contest):
    ws, repo = contest["ws"], contest["repo"]
    _rows(ws)
    session = handoff.HandoffSession(ws, "Op00", WORKER, repo)
    with mock.patch("opsb_core.ledger.seed.scheme", return_value=seed.FULL):
        full = session.ledger_seed("stage result", kind="codex")
    assert seed.HEADERS["ledger"] in full.text and "L-0001" in full.text
    with mock.patch("opsb_core.ledger.seed.scheme", return_value=seed.LEGACY):
        legacy = session.ledger_seed("stage result", kind="codex")
    assert legacy.text == "Preserve this checkpoint and await the next task.\nLast reported result:\nstage result"


def test_full_seed_records_its_scheme_and_size(contest):
    ws, repo = contest["ws"], contest["repo"]
    _rows(ws)
    session = handoff.HandoffSession(ws, "Op00", WORKER, repo)
    with mock.patch("opsb_core.ledger.seed.scheme", return_value=seed.FULL):
        session.build_seed(HEADINGS, "checkpoint", kind="acp")
    row = json.loads(session.log_path.read_text().splitlines()[-1])
    assert row["findings"]["seed_scheme"] == seed.FULL and row["seed_chars"] > 200


# ---------------------------------------------------------------- #7 compaction boundaries

def test_compaction_needs_a_boundary_above_the_floor():
    cfg = {"agents": {"compact_tokens": 100_000}}
    threshold = compact_threshold(1_000_000, cfg)
    assert threshold == 100_000
    floor = threshold * compact_window.floor_percent(cfg) // 100
    assert floor == 40_000
    assert compact_window.should_compact(39_999, 1_000_000, boundary="judged", cfg=cfg)[0] is False
    ok, reason = compact_window.should_compact(40_000, 1_000_000, boundary="judged", cfg=cfg)
    assert ok and reason == "judged"
    assert compact_window.should_compact(50_000, 1_000_000, boundary="", cfg=cfg) == (False, "no_boundary")


@pytest.mark.parametrize("boundary", list(compact_window.BOUNDARIES))
def test_each_boundary_triggers_above_the_floor(boundary):
    cfg = {"agents": {"compact_floor_percent": 40}}
    assert compact_window.should_compact(130_000, 1_000_000, boundary=boundary, cfg=cfg)[0] is True


def test_hard_cap_still_compacts_without_a_boundary():
    cfg = {}
    assert compact_window.should_compact(120_000, 200_000, boundary="", cfg=cfg) == (True, "hard_cap")
    assert compact_window.should_compact(1_000, 200_000, boundary="", cfg=cfg, percent=70) == (True, "hard_cap")


def test_no_compaction_while_a_tool_call_runs():
    cfg = {}
    assert compact_window.should_compact(500_000, 1_000_000, boundary="judged", cfg=cfg,
                                         tool_in_flight=True) == (False, "tool_in_flight")
    assert compact_window.should_compact(500_000, 1_000_000, boundary="test_terminal", cfg=cfg,
                                         tool_in_flight=True)[0] is False


def test_floor_percent_is_configurable_and_clamped():
    assert compact_window.floor_percent({}) == 40
    assert compact_window.floor_percent({"agents": {"compact_floor_percent": 55}}) == 55
    assert compact_window.floor_percent({"agents": {"compact_floor_percent": 1}}) == compact_window.FLOOR_MIN
    assert compact_window.floor_percent({"agents": {"compact_floor_percent": 500}}) == compact_window.FLOOR_MAX
    assert compact_window.floor_percent({"agents": {"compact_floor_percent": "lots"}}) == 40


def test_runner_compacts_on_a_delivered_test_result(tmp_path, monkeypatch):
    """A test terminal boundary compacts well below the threshold, while a bare turn end does not."""
    monkeypatch.setenv("PARHELION_HOME", str(tmp_path / "phome"))
    runner = _fake_runner(tmp_path, used=45_000, window=1_000_000)
    try:
        with mock.patch("opsb_core.config.ConfigRepository.load",
                        return_value={"agents": {"compact_tokens": 100_000}}):
            assert runner._should_compact() is False  # 45k >= floor, but no boundary yet
            runner.note_boundary("test_terminal")
            assert runner._should_compact() is True and runner.compact_reason == "test_terminal"
            assert runner._should_compact() is False  # the boundary is consumed once
    finally:
        runner.stop()


def _fake_runner(tmp_path, *, used: int, window: int):
    from opsb_core.agents.bus_store import AgentBusStore
    from opsb_core.agents.manager import _WorkerRunner
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    bus = AgentBusStore(ws)
    agent = f"dee-{used}"
    bus.create_agent({"agent_id": agent, "driver": "deepseek", "problem": "Demo", "worktree_mode": "readonly"})
    adapter = _QuietAdapter(ws, ws, "deepseek", agent, tmp_path / "log.jsonl")
    runner = _WorkerRunner(agent, bus, adapter, lambda: None)
    runner._context_tokens, runner._context_window = used, window
    return runner


class _QuietAdapter:
    def __init__(self, workspace, worktree, kind, agent_id, log_path):
        self.workspace, self.worktree, self.driver_kind = workspace, worktree, kind
        self.agent_id, self.log_path = agent_id, log_path
        self.on_spawn = None
        self.native_session_id = ""

    def stop(self) -> None:
        pass


# ---------------------------------------------------------------- #13 #14 the two state cards

def test_simple_state_card_keeps_its_2000_byte_ceiling(contest):
    ws = contest["ws"]
    card = state_card.build(ws, "Op00", WORKER)
    assert len(card.encode("utf-8")) <= state_card.MAX_CARD_BYTES
    assert card.startswith("STATE Op00") and card.rstrip().endswith(state_card.RULES_LINE)


def test_extended_state_card_reports_the_worktree_and_stays_bounded(contest, tmp_path, monkeypatch):
    ws = contest["ws"]
    card = state_card.build(ws, "Op00", WORKER, extended=True)
    assert card.startswith("EXT Op00")
    assert len(card.encode("utf-8")) <= state_card_ext.MAX_EXT_BYTES
    assert card.rstrip().endswith(state_card.RULES_LINE)
    doc = state_card_ext.data(ws, "Op00", WORKER)
    assert set(doc) >= {"title", "worktree", "head_sha", "cases", "targets", "receipts"}
    assert doc["head_sha"] == contest["sha"]
    assert state_card_ext.render({"title": "T", "cases": [], "targets": [], "receipts": []}).startswith("EXT T")


def test_extended_card_gap_and_targets_from_a_receipt(contest):
    ws = contest["ws"]
    (ws / "results").mkdir(exist_ok=True)
    row = {"submission_id": "s1", "title": "Op00", "sha": contest["sha"], "status": "Pass", "score": 61.0,
           "submitted_at": "2026-01-02T03:00:00+00:00", "finished_at": "2026-01-02T03:05:00+00:00",
           "tbest_observed_at": "2026-01-02T02:00:00+00:00",
           "case_results": [{"testcase_id": "01", "status": "Pass", "time_us": 12.0, "tbest_us": 10.0},
                            {"testcase_id": "02", "status": "Pass", "time_us": 4.0, "tbest_us": 4.0}]}
    (ws / "results" / "submit-queue-outcome.jsonl").write_text(json.dumps(row) + "\n")
    doc = state_card_ext.data(ws, "Op00", WORKER)
    assert [c["case"] for c in doc["cases"]][:2] == ["#01", "#02"]
    assert doc["cases"][0]["gap_pct"] == pytest.approx(0.2)
    # the judge's own points formula: below the 100 of a case already at tbest, and above zero
    assert 0 < doc["cases"][0]["points"] < doc["cases"][1]["points"] == pytest.approx(100.0)
    assert doc["targets"][0]["case"] == "#01" and doc["targets"][0]["gap_pct"] == pytest.approx(0.2)
    assert doc["receipts"][0]["sha"] == contest["sha"][:7] and doc["receipts"][0]["score"] == 61.0
    assert "case gap ours/tbest" in state_card_ext.render(doc)


def test_status_detail_adds_the_per_case_table(contest):
    from opsb_core.mcp import handlers_bench
    ws = contest["ws"]
    snap = {"sampled_at": "now", "contest": {"title": "C", "workspace": str(ws)},
            "scheduler": {}, "problems": [{"title": "Op00", "status": "Pass", "current_score": 1.0}],
            "submission_queue": []}
    with mock.patch.object(handlers_bench, "compute_workspace_snapshot", return_value=snap), \
         mock.patch.object(handlers_bench, "invoke_bridge", return_value={}):
        plain = handlers_bench.tool_parhelion_status({"problem": "Op00"}, ws)
        detail = handlers_bench.tool_parhelion_status({"problem": "Op00", "detail": True}, ws)
    assert "case_detail" not in plain and plain["rev"]
    (item,) = detail["case_detail"]
    assert item["title"] == "Op00" and item["card"].startswith("EXT Op00")
    assert item["columns"][:2] == ["case", "speedup"] and isinstance(item["rows"], list)
    assert detail["rev"] != plain["rev"]  # the detail is part of the revision


def test_checkpoint_still_budgets_the_card_and_the_tail():
    text = checkpoint.build("STATE card", "stage " * 500, budget=1200)
    assert len(text) <= 1200 and text.startswith("STATE card")
    assert text.rstrip().endswith(checkpoint.CHECKPOINT_TAIL)
