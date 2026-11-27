"""4.2 C #3 #4 (P06): ``parhelion_note``, ``parhelion_ledger``, ``parhelion_recall``, retest interception,
near-duplicate filtering, failure fingerprints and the tool registration contract.

Everything runs on temporary workspaces and fake records; no model API, daemon, device or real contest data.
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

from opsb_core.ledger import auto, budget, model, note, recall, retest, store, view  # noqa: E402
from opsb_core.mcp import handlers_bench, handlers_ledger  # noqa: E402
from opsb_core.mcp.schemas import TOOL_SCHEMAS  # noqa: E402
from opsb_core.mcp.tools import (  # noqa: E402
    TOOL_DEFINITIONS,
    TOOL_HANDLERS,
    call_tool,
    is_tool_allowed_for_role,
    list_tool_schemas,
)
from opsb_core.services import test_runs  # noqa: E402

WORKER = "dee-aaaaaa"
_TILED = """int main() {{
  // tile 64: a block of statements that the two candidate commits share verbatim
  int acc = 0;
  for (int i = 0; i < 64; ++i) {{
    acc += i * {};
  }}
  for (int j = 0; j < 64; ++j) {{
    acc ^= j;
  }}
  return acc;
}}
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, name: str, text: str, message: str) -> str:
    (repo / name).write_text(text)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def contest(tmp_path):
    """A contest workspace with one problem repository and three commits (two nearly equal)."""
    ws = (tmp_path / "contest").resolve()
    repo = ws / "Op00"
    (repo / "src").mkdir(parents=True)
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "T"]):
        _git(repo, *args)
    base = _commit(repo, "src/kernel.cpp", "int main() {\n  return 0;\n}\n", "baseline kernel")
    tile = _commit(repo, "src/kernel.cpp", _TILED.format(1), "Tile the inner loop by 64")
    _git(repo, "checkout", "-q", "-b", "alt", base)
    near = _commit(repo, "src/kernel.cpp", _TILED.format(2), "Tile the inner loop by 64 with another tail value")
    _git(repo, "checkout", "-q", "master")
    (ws / "opsb.workspace.json").write_text(json.dumps({
        "layout": "clean-agents-first", "problems": [{"title": "Op00", "path": "Op00", "difficulty": 2}]}))
    (ws / "AGENTS.md").write_text("# Contest\n\n## Scoring workflow\nUse the gate.\n\n## Rules\nNever ssh; one device job at a time.\n")
    return {"ws": ws, "repo": repo, "base": base, "tile": tile, "near": near}


def _note(args: dict, ws: Path) -> dict:
    return call_tool("parhelion_note", {"problem": "Op00", **args}, ws, role="worker", agent_id=WORKER)


# ---------------------------------------------------------------- #3 parhelion_note

def test_note_writes_a_typed_entry_with_an_id(contest):
    ws = contest["ws"]
    out = _note({"type": "insight", "claim": "case 05 is the tail tile", "scope": {"cases": ["05"]},
                 "actionable_next": "swap the tile order", "est_gain": {"case05": "-15%"}}, ws)
    assert out["ok"] and out["id"] == "L-0001" and out["confidence"] == "inferred"
    row = store.Ledger(ws, "Op00", WORKER).rows()[0]
    assert row["type"] == "insight" and row["source"] == "worker" and row["scope"] == {"cases": ["05"]}
    assert row["claim"] == "case 05 is the tail tile" and row["actionable_next"] == "swap the tile order"


@pytest.mark.parametrize("field,value", [("outcome", "better"), ("per_case_us", {"01": 1.0}), ("source", "platform"),
                                         ("receipt_id", "s1"), ("test_run_id", "r1"), ("median_us", {"01": 1.0}),
                                         ("truncated", ["claim"])])
def test_note_rejects_platform_fields(contest, field, value):
    ws = contest["ws"]
    out = _note({"type": "experiment", "claim": "x", field: value}, ws)
    assert out["ok"] is False and field in out["error"] and out["written"] is False
    assert store.Ledger(ws, "Op00", WORKER).rows() == []


def test_note_validates_type_claim_and_priority(contest):
    ws = contest["ws"]
    missing_type = _note({"claim": "x"}, ws)
    assert missing_type["ok"] is False and "type is required" in missing_type["error"]
    unknown = _note({"type": "insight", "claim": "x", "colour": "blue"}, ws)
    assert unknown["ok"] is False and "colour" in unknown["error"]
    out = _note({"type": "insight", "claim": "   "}, ws)
    assert out["ok"] is False and "claim is required" in out["error"]
    out = _note({"type": "hypothesis", "claim": "tile helps", "priority": 9}, ws)
    assert out["ok"] is False and "priority" in out["error"]
    out = _note({"type": "hypothesis", "claim": "tile helps", "status": "vibing"}, ws)
    assert out["ok"] is False and "status" in out["error"]


def test_note_status_change_is_a_new_row_that_supersedes(contest):
    ws = contest["ws"]
    hyp = _note({"type": "hypothesis", "claim": "tile 64 helps case 05", "priority": 1}, ws)
    moved = _note({"type": "hypothesis", "id": hyp["id"], "status": "refuted",
                   "resolution_note": "no change within the noise band"}, ws)
    assert moved["id"] != hyp["id"] and moved["status"] == "refuted"
    rows = store.Ledger(ws, "Op00", WORKER).rows()
    assert rows[-1]["supersedes"] == [hyp["id"]] and rows[-1]["claim"] == "tile 64 helps case 05"
    assert [r["id"] for r in view.current(rows)] == [moved["id"]]
    assert view.head(rows, hyp["id"])["id"] == moved["id"]  # the old id still resolves
    assert _note({"type": "hypothesis", "id": "L-9999", "status": "open"}, ws)["ok"] is False


def test_a_model_confidence_is_capped_at_inferred(contest):
    ws = contest["ws"]
    out = _note({"type": "insight", "claim": "x", "confidence": "measured"}, ws)
    assert out["ok"] is True and out["confidence"] == "inferred"
    via_api = note.write(store.Ledger(ws, "Op00", WORKER), {"type": "insight", "claim": "y",
                                                             "confidence": "measured"}, source="worker")
    assert via_api["confidence"] == "inferred"
    platform = store.Ledger(ws, "Op00", WORKER).append(
        "measurement", {"claim": "smoke run", "key": "test.min_us", "value": 9.0, "test_level": "smoke"})
    assert platform["confidence"] == "measured"


def test_unverified_confidence_belongs_to_peer_rows_only(contest):
    ws = contest["ws"]
    led = store.Ledger(ws, "Op00", WORKER)
    own = note.write(led, {"type": "insight", "claim": "mine", "confidence": "unverified"}, source="worker")
    assert own["confidence"] == "speculative"
    peer = note.write(led, {"type": "insight", "claim": "theirs", "confidence": "unverified"}, source="peer")
    assert peer["confidence"] == "unverified" and peer["source"] == "peer"


def test_note_cannot_touch_a_measurement(contest):
    ws = contest["ws"]
    led = store.Ledger(ws, "Op00", WORKER)
    measured = led.append("measurement", {"key": "test.min_us", "value": 9.0, "claim": "test", "test_level": "smoke"})
    out = _note({"type": "measurement", "id": measured["id"], "claim": "rewritten"}, ws)
    assert out["ok"] is False and "platform only" in out["error"]


def test_worker_cannot_use_another_problem(contest):
    ws = contest["ws"]
    with pytest.raises(PermissionError):
        # no bus record for this worker: the problem it names is taken as its own only when known,
        # so an orchestrator-supplied name is never silently accepted for another problem
        handlers_ledger.problem_of(ws, WORKER) or (_ for _ in ()).throw(PermissionError("no bus"))
    (ws / "state").mkdir(exist_ok=True)
    from opsb_core.agents.bus_store import AgentBusStore
    bus = AgentBusStore(ws)
    bus.create_agent({"agent_id": WORKER, "driver": "deepseek", "problem": "Op00", "worktree_mode": "readonly"})
    assert handlers_ledger.problem_of(ws, WORKER) == "Op00"
    with pytest.raises(PermissionError, match="staffed on Op00"):
        call_tool("parhelion_note", {"problem": "Other", "type": "insight", "claim": "x"}, ws,
                  role="worker", agent_id=WORKER)



def test_a_bound_worker_writes_its_own_ledger_without_naming_the_problem(contest):
    """4.3.1: the first sprint's workers called parhelion_note without ``problem`` and got
    "the problem of this call is unknown" although the bus knew their problem."""
    ws = contest["ws"]
    (ws / "state").mkdir(exist_ok=True)
    from opsb_core.agents.bus_store import AgentBusStore
    AgentBusStore(ws).create_agent({"agent_id": WORKER, "driver": "deepseek", "problem": "Op00",
                                    "worktree_mode": "readonly"})
    out = call_tool("parhelion_note", {"type": "insight", "claim": "case 05 is the tail tile"}, ws,
                    role="worker", agent_id=WORKER)
    assert out["ok"] and out["problem"] == "Op00"
    assert store.Ledger(ws, "Op00", WORKER).rows()[0]["claim"] == "case 05 is the tail tile"

# ---------------------------------------------------------------- #3 gate hypothesis

def test_gate_request_without_hypothesis_is_generated_and_named(contest):
    ws, sha = contest["ws"], contest["tile"]
    link = auto.on_gate_link(ws, "Op00", sha, WORKER, note="Tile the inner loop", enqueued=True,
                             estimate={"delta_expected": 2.0, "confidence_sigma": 0.8})
    assert link["auto"] is True and link["id"] == "L-0001" and link["id"] in link["note"]
    row = store.Ledger(ws, "Op00", WORKER).rows()[0]
    assert row["type"] == "hypothesis" and row["status"] == "queued" and row["claim"] == "Tile the inner loop"
    again = auto.on_gate_link(ws, "Op00", sha, WORKER, note="again", enqueued=True)
    assert again["auto"] is False and again["id"] == "L-0001"  # one hypothesis per SHA


def test_gate_request_names_and_updates_the_given_hypothesis(contest):
    ws, sha = contest["ws"], contest["tile"]
    hyp = _note({"type": "hypothesis", "claim": "tile helps", "priority": 1}, ws)
    link = auto.on_gate_link(ws, "Op00", sha, WORKER, hypothesis_id=hyp["id"], enqueued=True,
                             predicted_effect={"delta_mu": 1.5, "delta_sigma": 0.5})
    assert link["auto"] is False and link["id"] != hyp["id"]  # the update is a new row
    rows = store.Ledger(ws, "Op00", WORKER).rows()
    assert view.head(rows, hyp["id"])["id"] == link["id"]
    current = view.current(rows)
    (row,) = [r for r in current if r["type"] == "hypothesis"]
    assert row["predicted_effect"] == {"delta_mu": 1.5, "delta_sigma": 0.5} and row["status"] == "queued"
    assert row["sha"] == sha and row["supersedes"] == [hyp["id"]]


def test_gate_link_never_refuses_a_submission(contest):
    ws, sha = contest["ws"], contest["tile"]
    link = auto.on_gate_link(ws, "Op00", sha, WORKER, hypothesis_id="L-0404", note="cand", enqueued=True)
    assert "L-0404" in link["note"] and link["id"]  # unknown id: the generated one is used instead
    created = [r for r in store.Ledger(ws, "Op00", WORKER).rows() if r["type"] == "hypothesis"]
    assert created and created[0]["id"] == link["id"]


@pytest.mark.parametrize("entry", [
    {"type": "insight", "claim": "case 05 is bound by the matrix unit"},
    {"type": "constraint", "claim": "inputs stay under 4 MiB"},
])
def test_gate_link_with_a_non_hypothesis_id_generates_one(contest, entry):
    """4.4.7: a worker that names an insight or constraint (a card's lead ref) as its hypothesis lost the link.

    The platform superseded that row with hypothesis fields, the ledger refused them, and the guard
    swallowed the error: no hypothesis was recorded for the SHA and the reply said nothing.
    """
    ws, sha = contest["ws"], contest["tile"]
    lead = _note(entry, ws)
    link = auto.on_gate_link(ws, "Op00", sha, WORKER, hypothesis_id=lead["id"], note="cand", enqueued=True,
                             predicted_effect={"delta_mu": 1.0, "delta_sigma": 0.5})
    assert link and link["id"] != lead["id"] and lead["id"] in link["note"] and entry["type"] in link["note"]
    rows = store.Ledger(ws, "Op00", WORKER).rows()
    assert view.head(rows, lead["id"])["type"] == entry["type"]  # the cited row is left as it was
    (hyp,) = [r for r in rows if r["type"] == "hypothesis"]
    assert hyp["id"] == link["id"] and hyp["sha"] == sha


def test_submit_gate_handler_reports_the_auto_generated_id(contest):
    ws, sha = contest["ws"], contest["tile"]
    verdict = {"approved": True, "priority_score": 1.0, "stages": [
        {"stage": "SAFETY", "passed": True, "reason": ""}, {"stage": "NOISE_FLOOR", "passed": True, "reason": ""},
        {"stage": "CIRCUIT_BREAKER", "passed": True, "reason": ""}, {"stage": "QUOTA_RESERVE", "passed": True, "reason": ""}],
        "estimate": {"estimated_score": 75.0, "current_score": 72.0, "delta_expected": 3.0, "confidence_sigma": 1.0}}
    args = {"problem": "Op00", "sha": sha, "note": "Tile by 64", "enqueue": True}
    with mock.patch.object(handlers_bench, "invoke_bridge", return_value={"enqueued": True, "verdict": verdict}):
        out = call_tool("parhelion_submit_gate", args, ws, role="worker", agent_id=WORKER)
    assert out["enqueued"] is True and out["ledger"]["auto"] is True
    assert out["ledger"]["id"] in out["ledger"]["note"] and out["ledger"]["id"].startswith("L-")


# ---------------------------------------------------------------- #4 ledger views and recall

def _seed_ledger(ws: Path) -> dict[str, str]:
    led = store.Ledger(ws, "Op00", WORKER)
    out = {}
    out["hyp"] = led.append("hypothesis", {"claim": "tile 64 helps case 05", "priority": 1,
                                            "scope": {"cases": ["05"]},
                                            "predicted_effect": {"delta_mu": 1.2, "delta_sigma": 0.4}},
                            source="worker")["id"]
    out["quiet"] = led.append("hypothesis", {"claim": "vectorise the copy", "priority": 4},
                              source="worker")["id"]
    out["ins"] = led.append("insight", {"claim": "cases 01-04 are within 5% of tbest", "cases": ["01", "02"],
                                         "actionable_next": "leave them alone"}, source="worker")["id"]
    out["fail"] = led.append("failure", {"claim": "double buffer did not help", "approach": "double-buffer A",
                                          "why_failed": "the kernel is DMA-bound on B",
                                          "retry_conditions": "if B becomes L1 resident",
                                          "scope": {"cases": ["03"]}}, source="worker")["id"]
    led.append("experiment", {"claim": "abc1234 worse on case 03", "sha": "abc1234", "outcome": "worse",
                              "hypothesis_id": out["hyp"], "change_summary": "double buffer A",
                              "scope": {"cases": ["03"]}}, source="platform")
    return out


def test_ledger_views_one_line_per_entry_with_ids(contest):
    ws = contest["ws"]
    ids = _seed_ledger(ws)
    open_view = call_tool("parhelion_ledger", {"problem": "Op00", "view": "open"}, ws, role="worker", agent_id=WORKER)
    assert open_view["matched"] == 2 and open_view["lines"][0].startswith(f"{ids['hyp']} hypothesis")
    assert ids["quiet"] in open_view["lines"][1]  # priority 1 first
    failures = call_tool("parhelion_ledger", {"problem": "Op00", "view": "failures"}, ws, role="worker",
                         agent_id=WORKER)
    assert any(line.startswith(ids["fail"]) for line in failures["lines"])
    unexploited = call_tool("parhelion_ledger", {"problem": "Op00", "view": "unexploited"}, ws, role="worker",
                           agent_id=WORKER)
    assert [line.split()[0] for line in unexploited["lines"]] == [ids["ins"]]
    case_view = call_tool("parhelion_ledger", {"problem": "Op00", "view": "case:03"}, ws, role="worker",
                          agent_id=WORKER)
    assert ids["fail"] in " ".join(case_view["lines"])
    since = call_tool("parhelion_ledger", {"problem": "Op00", "view": f"since:{ids['hyp']}"}, ws, role="worker",
                      agent_id=WORKER)
    assert since["matched"] >= 1 and ids["hyp"] not in " ".join(since["lines"])
    bad = call_tool("parhelion_ledger", {"problem": "Op00", "view": "everything"}, ws, role="worker", agent_id=WORKER)
    assert bad["ok"] is False and "view must be" in bad["error"]
    assert handlers_ledger.row_line({"id": "L-0001", "type": "measurement", "value": 9.2, "unit": "us",
                                     "key": "test.min_us", "claim": "smoke"}, WORKER).startswith("L-0001 measurement")


def test_orchestrator_reads_a_workers_ledger(contest):
    ws = contest["ws"]
    ids = _seed_ledger(ws)
    out = call_tool("parhelion_ledger", {"problem": "Op00", "worker": WORKER, "view": "open"}, ws,
                    role="orchestrator")
    assert ids["hyp"] in " ".join(out["lines"]) and out["worker"] == WORKER


def test_recall_returns_snippets_with_their_source(contest):
    ws = contest["ws"]
    ids = _seed_ledger(ws)
    hits = call_tool("parhelion_recall", {"problem": "Op00", "query": "double buffer DMA"}, ws,
                     role="worker", agent_id=WORKER)
    assert hits["ok"] and hits["count"] >= 1
    sources = [h["source"] for h in hits["results"]]
    assert any(p.endswith(".jsonl#1") or ".jsonl#" in p for p in sources)
    assert any("DMA" in h["text"] or "dma" in h["text"].lower() for h in hits["results"])
    ledger_hits = [h for h in hits["results"] if h["kind"] == "ledger"]
    assert ledger_hits and ledger_hits[0]["source"].startswith(str(ws / "results" / "ledger"))
    assert ids["fail"] in ledger_hits[0]["text"]


def test_recall_indexes_handoffs_and_session_logs_read_only(contest, monkeypatch, tmp_path):
    from opsb_core.drivers.base import resolve_session_logs_dir, resolve_workspace_state_dir
    ws = contest["ws"]
    _seed_ledger(ws)
    state = resolve_workspace_state_dir(ws)
    (state / "handoffs").mkdir(parents=True, exist_ok=True)
    (state / "handoffs" / f"{WORKER}.jsonl").write_text(json.dumps(
        {"ts": "2026-01-01T00:00:00Z", "kind": "acp", "status": "ok",
         "narrative_final": "## Next experiment\nRetry the B-side prefetch, verdict L-0001."}) + "\n")
    logs = resolve_session_logs_dir()
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / f"mesh-{WORKER}.jsonl"
    row = {"ts": "2026-01-01T00:00:00Z", "msg": {"method": "session/prompt",
                                                 "params": {"prompt": "the prefetch of B looked free"}}}
    log.write_text(json.dumps(row) + "\n")
    before = {p: p.read_bytes() for p in (state / "handoffs" / f"{WORKER}.jsonl", log)}
    hits = recall.search(ws, "Op00", WORKER, "prefetch", limit=5)
    assert {h["kind"] for h in hits} >= {"handoff", "session"}
    assert all(h["source"].split("#")[0] in {str(p) for p in before} or "ledger" in h["source"] for h in hits)
    assert {p: p.read_bytes() for p in before} == before  # the sources were not touched
    cache = recall.cache_dir(ws)
    assert cache.is_dir() and any(cache.iterdir())
    assert all(not str(p).startswith(str(logs)) for p in cache.rglob("*"))
    assert recall.search(ws, "Op00", WORKER, "  ") == []
    with pytest.raises(ValueError, match="query"):
        call_tool("parhelion_recall", {"problem": "Op00", "query": " "}, ws, role="worker", agent_id=WORKER)


# ---------------------------------------------------------------- retest interception and near duplicates

def _formal(ws: Path, sha: str, *, rid: str = "ab" * 16, level: str = "standard", status: str = "finished",
            counts: dict | None = None, version: int = 2) -> dict:
    record = {"request_id": rid, "problem": "Op00", "sha": sha, "level": level, "status": status,
              "exit_code": 0, "cases": [], "finished_at": "2026-01-01T00:00:00+00:00",
              "digest": {"formal": True, "suite_version": version,
                         "summary": {"total": 2, "passed": 2, "counts": counts or {"PASS": 2}}}}
    test_runs._write(ws, record)
    return record


def test_retest_of_a_formal_run_returns_it_without_queueing(contest):
    ws, sha = contest["ws"], contest["tile"]
    _formal(ws, sha)
    store.Ledger(ws, "Op00", WORKER).append("experiment", {"claim": "tile 64 better", "sha": sha,
                                                            "outcome": "better", "test_run_id": "r" * 32},
                                             ekey=f"test:{'ab' * 16}:experiment")
    out = call_tool("parhelion_test", {"problem": "Op00", "sha": sha, "level": "standard"}, ws,
                    role="worker", agent_id=WORKER)
    assert out["intercepted"] is True and out["request_id"] == "ab" * 16
    assert out["ledger_ids"] and out["ledger_ids"][0].endswith("L-0001")
    assert not list(test_runs.registry_dir(ws).glob("*.log"))  # nothing was launched
    forced = retest.intercept(ws, "Op00", sha, "standard", force_rerun=True, reason="cold start rerun")
    assert forced is None  # a written reason is the documented way through
    assert retest.intercept(ws, "Op00", sha, "final") is None  # another level is new evidence
    _formal(ws, contest["base"], rid="cd" * 16, counts={"WA": 1})
    assert retest.intercept(ws, "Op00", contest["base"], "standard") is None  # a failing run is not reusable
    _formal(ws, contest["near"], rid="ef" * 16, version=4)
    assert retest.intercept(ws, "Op00", contest["near"], "standard") is None  # same suite version only


def test_near_duplicate_diff_is_reported_with_its_ledger_id(contest):
    ws, repo = contest["ws"], contest["repo"]
    led = store.Ledger(ws, "Op00", WORKER)
    led.append("experiment", {"claim": "tile 64", "sha": contest["tile"], "outcome": "worse",
                              "diff_hash": "", "change_summary": "tile 64"}, source="platform")
    rows = led.rows()
    found = retest.near_duplicate(ws, "Op00", contest["near"], rows, WORKER)
    assert found is not None and found["id"].endswith("L-0001") and found["kind"] in ("similar_diff", "same_diff")
    assert found["similarity"] >= retest.SIMILARITY
    out = call_tool("parhelion_test", {"problem": "Op00", "sha": contest["near"]}, ws, role="worker",
                    agent_id=WORKER)
    assert out["intercepted"] is True and out["near_duplicate"]["id"].endswith("L-0001")
    assert "near_duplicate" in out and "difference" in out["note"]
    assert retest.near_duplicate(ws, "Op00", contest["base"], rows, WORKER) is None  # a different change
    assert retest.similarity("", "x") == 0.0
    assert retest.normalize_patch("@@ -1 +1 @@\n-old line\n+new line\n") == ["-old line", "+new line"]


def test_a_revert_is_not_a_near_duplicate_of_the_change_it_undoes():
    """The +/- marker is part of a line: undoing a change is a different experiment."""
    change = "@@ -1,3 +1,3 @@\n keep = 1\n-x = 1\n+x = 2\n keep = 2\n"
    revert = "@@ -1,3 +1,3 @@\n keep = 1\n-x = 2\n+x = 1\n keep = 2\n"
    assert retest.similarity(change, change) == 1.0
    assert retest.similarity(change, revert) < retest.SIMILARITY
    single = "@@ -10,7 +10,7 @@\n" + "".join(f" keep_{i} = {i}\n" for i in range(6)) + "-TILE = 64\n+TILE = {}\n"
    assert retest.similarity(single.format(128), single.format(256)) < retest.SIMILARITY  # one-value sweep


def test_near_duplicate_lets_a_reasoned_rerun_through(contest):
    ws = contest["ws"]
    led = store.Ledger(ws, "Op00", WORKER)
    led.append("experiment", {"claim": "tile 64", "sha": contest["tile"], "outcome": "worse"}, source="platform")
    with mock.patch.object(handlers_bench.test_runs, "submit", return_value={"request_id": "n" * 32,
                                                                            "status": "queued"}) as submit:
        out = call_tool("parhelion_test", {"problem": "Op00", "sha": contest["near"],
                                            "reason": "the second return value changes the tail case"}, ws,
                        role="worker", agent_id=WORKER)
    assert submit.called and out["request_id"] == "n" * 32 and "intercepted" not in out


# ---------------------------------------------------------------- R5 fingerprints, budget and plateau

def test_attempt_fingerprint_is_stable_across_renames(contest):
    ws = contest["ws"]
    led = store.Ledger(ws, "Op00", WORKER)
    for i in range(budget.ATTEMPT_BUDGET):
        led.append("failure", {"claim": f"attempt {i}: tiling the K loop", "approach": "tile K",
                                "why_failed": "memory bandwidth bound on the B tile",
                                "scope": {"cases": ["03"], "bucket": "memory"}}, source="worker")
    rows = led.rows()
    prints = {budget.fingerprint(r) for r in rows}
    assert len(prints) == 1  # same cases, bucket, diff class and outcome whatever the claim says
    notes = budget.warnings(rows)
    assert notes and notes[0]["kind"] in ("attempt_budget", "bottleneck_budget")
    assert all(i.split("/")[-1].startswith("L-") for i in notes[0]["ids"])
    assert budget.case_set(rows[0]) == ("03",) and budget.bottleneck_of(rows[0]) == "memory"
    renamed = store.Ledger(ws, "Op00", "dee-bbbbbb")
    renamed.append("failure", {"claim": "the K tile again", "approach": "tile K",
                                "why_failed": "memory bandwidth bound", "scope": {"cases": ["03"],
                                                                                  "bucket": "memory"}}, source="worker")
    assert budget.warnings(rows + renamed.rows())[0]["kind"] in ("attempt_budget", "bottleneck_budget")


def test_plateau_after_five_non_improving_experiments(contest):
    ws = contest["ws"]
    led = store.Ledger(ws, "Op00", WORKER)
    for i in range(budget.PLATEAU_RUN):
        led.append("experiment", {"claim": f"try {i}", "sha": f"{i:07d}", "outcome": "worse",
                                  "scope": {"cases": ["01"], "bucket": "compute"}}, source="platform")
    rows = led.rows()
    assert budget.plateau_run(rows) == budget.PLATEAU_RUN
    kinds = [w["kind"] for w in budget.warnings(rows)]
    assert "plateau" in kinds
    led.append("experiment", {"claim": "it worked", "sha": "f" * 7, "outcome": "better"}, source="platform")
    assert budget.plateau_run(led.rows()) == 0


def test_test_tool_carries_the_budget_warning(contest):
    ws = contest["ws"]
    led = store.Ledger(ws, "Op00", WORKER)
    for i in range(budget.ATTEMPT_BUDGET):
        led.append("experiment", {"claim": f"try {i}", "sha": contest["tile"], "outcome": "worse",
                                  "scope": {"cases": ["03"], "bucket": "memory"}}, source="platform")
    with mock.patch.object(handlers_bench.test_runs, "submit", return_value={"request_id": "q" * 32,
                                                                            "status": "queued"}):
        out = call_tool("parhelion_test", {"problem": "Op00", "sha": contest["tile"], "cases": ["03"],
                                            "force_rerun": True, "reason": "measure the new tile size"}, ws,
                        role="worker", agent_id=WORKER)
    assert out["request_id"] == "q" * 32 and out["warnings"]
    assert {w["kind"] for w in out["warnings"]} & {"attempt_budget", "bottleneck_budget", "plateau"}


# ---------------------------------------------------------------- registration contract

def test_new_tools_are_registered_in_schema_handler_and_roles():
    names = ("parhelion_note", "parhelion_ledger", "parhelion_recall")
    schema_names = [s["name"] for s in TOOL_SCHEMAS]
    assert len(schema_names) == len(set(schema_names))
    for name in names:
        assert name in schema_names and name in TOOL_HANDLERS
        assert {t["name"]: t for t in TOOL_DEFINITIONS}[name]["handler"] is TOOL_HANDLERS[name]
        assert is_tool_allowed_for_role(name, "worker") and is_tool_allowed_for_role(name, "orchestrator")
        assert name in {t["name"] for t in list_tool_schemas(role="worker")}
    schema = {s["name"]: s for s in TOOL_SCHEMAS}
    assert schema["parhelion_note"]["inputSchema"]["required"] == ["type"]
    assert "hypothesis_id" in schema["parhelion_submit_gate"]["inputSchema"]["properties"]
    assert "predicted_effect" in schema["parhelion_submit_gate"]["inputSchema"]["properties"]
    assert "force_rerun" in schema["parhelion_test"]["inputSchema"]["properties"]
    assert "detail" in schema["parhelion_status"]["inputSchema"]["properties"]


def test_tool_arguments_match_the_handlers(contest):
    """Every property the schema promises is passed straight through to the handler's argument key."""
    ws = contest["ws"]
    schema = {s["name"]: s for s in TOOL_SCHEMAS}
    props = set(schema["parhelion_note"]["inputSchema"]["properties"])
    assert props <= set(handlers_ledger.FIELD_KEYS) | {"type", "id", "problem", "worker"}
    assert {"view", "problem", "worker", "limit", "offset", "cursor"} == set(
        schema["parhelion_ledger"]["inputSchema"]["properties"])
    assert {"query", "limit", "problem", "worker", "refresh"} == set(
        schema["parhelion_recall"]["inputSchema"]["properties"])
    out = call_tool("parhelion_ledger", {"problem": "Op00", "worker": WORKER, "limit": 5}, ws)
    assert out["ok"] is True


def test_ledger_schema_enum_matches_the_model():
    schema = {s["name"]: s for s in TOOL_SCHEMAS}
    assert set(schema["parhelion_note"]["inputSchema"]["properties"]["type"]["enum"]) <= set(model.TYPES)
    assert set(schema["parhelion_note"]["inputSchema"]["properties"]["status"]["enum"]) <= set(model.HYPOTHESIS_STATUS)
    assert set(schema["parhelion_note"]["inputSchema"]["properties"]["confidence"]["enum"]) == {"inferred", "speculative"}
