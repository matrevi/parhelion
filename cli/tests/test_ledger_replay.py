"""4.2 C (P05): the ``ledger_core`` replay seed rebuilds the ledger from logged events with the platform's code."""
from __future__ import annotations

import json
import sys
from pathlib import Path

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core.ledger import checkpoint, view  # noqa: E402
from opsb_core.replay import bench, ledger_seed, loader, seeds  # noqa: E402
from opsb_core.replay.loader import AgentLog, Cut, ToolCall, Turn  # noqa: E402
from test_replay_bench import build_log  # noqa: E402

SHA_A, SHA_B = "a1b2c3d" + "0" * 33, "b2c3d4e" + "0" * 33
DIGEST = {"level": "standard", "suite_version": 3, "summary": {"total": 15, "passed": 15, "counts": {"PASS": 15}},
          "timing": {"drift": 1.0, "noisy": False, "repeats": 5,
                     "cases": {"01": {"min_us": 9.2, "median_us": 9.4}}},
          "baseline": {"sha": "1111111", "geomean_speedup": 0.91, "cases": {"01": 0.91}}}
CARD = "STATE Op00 @ 10:00\nbest: 72.50 a1b2c3d\nrules: device work only via parhelion_test."
HANDOFF = "Goal: speed up Op00.\nNext experiment: shrink the twiddle table."
STAGE = "Long stage report. " * 400 + "FINAL: abandon tiling, try the twiddle table next."


def _call(seq, name, args, body, ts="2026-01-02T03:00:00+00:00"):
    return ToolCall(seq, ts, f"c{seq}", name, args, "completed", json.dumps(body))


def _log() -> AgentLog:
    gate_a = _call(1, "parhelion_submit_gate", {"problem": "Op00", "sha": SHA_A[:7], "enqueue": True,
                                                "note": "Tile the inner loop by 64 with double buffering"},
                   {"sha": SHA_A, "enqueued": True, "status_tag": "APPROVED",
                    "estimate": {"delta_expected": 3.0, "confidence_sigma": 1.0}})
    test_a = _call(2, "parhelion_test_wait", {"request_id": "req1"},
                   {"request_id": "req1", "problem": "Op00", "sha7": SHA_A[:7], "level": "standard",
                    "status": "finished", "digest": DIGEST})
    preview = _call(3, "parhelion_submit_gate", {"problem": "Op00", "sha": SHA_B[:7], "note": "just a preview"},
                    {"sha": SHA_B, "enqueued": False, "estimate": {"delta_expected": 1.0, "confidence_sigma": 1.0}})
    gate_b = _call(4, "parhelion_submit_gate", {"problem": "Op00", "sha": SHA_B[:7], "enqueue": True,
                                                "note": "Shrink the twiddle table for the radix kernel"},
                   {"sha": SHA_B, "enqueued": True, "estimate": {"delta_expected": 2.0, "confidence_sigma": 1.0}})
    report = _call(5, "parhelion_agent_report", {"kind": "result", "summary": STAGE}, {"ok": True})
    turns = [
        Turn(0, 1, "2026-01-02T03:00:00+00:00", "s1", "# Op00 brief", "brief", "intermediate reply", tools=[gate_a, test_a]),
        Turn(1, 2, "2026-01-02T03:10:00+00:00", "s1", "Op00 a1b2c3d · Pass · score 72.5 · submission sub1\nnext steps",
             "followup", "ok", tools=[preview, gate_b, report]),
        Turn(2, 3, "2026-01-02T03:20:00+00:00", "s1", "Write a structured handoff ...", "handoff", HANDOFF),
        Turn(3, 4, "2026-01-02T03:21:00+00:00", "s2", "Preserve this checkpoint ...", "seed", "Ready."),
    ]
    seed = f"{loader.SEED_PREFIX} and await the next task.\n{HANDOFF}\nLast reported result:\nstage\n{CARD}\n{seeds.CHECKPOINT_TAIL}"
    cut = Cut(0, 2, 3, "2026-01-02T03:20:00+00:00", HANDOFF, seed, after_end=4)
    return AgentLog("dee-aaaaaa", "mesh-dee-aaaaaa.jsonl", turns, [], [], [cut])


def test_ledger_core_is_registered_and_rebuilds_the_platform_ledger():
    assert "ledger_core" in seeds.names()
    log = _log()
    led = ledger_seed.reconstruct(log.turns)
    rows = view.current(led.rows())
    hyps = {r["sha"][:7]: r for r in rows if r["type"] == "hypothesis"}
    assert set(hyps) == {SHA_A[:7], SHA_B[:7]}  # the preview call (no enqueue) made no hypothesis
    assert hyps[SHA_A[:7]]["status"] == "confirmed" and hyps[SHA_B[:7]]["status"] == "queued"
    assert hyps[SHA_B[:7]]["claim"].startswith("Shrink the twiddle table")
    (exp,) = [r for r in rows if r["type"] == "experiment"]
    assert exp["outcome"] == "worse" and exp["receipt_id"] == "sub1" and exp["test_run_id"] == "req1"
    assert [r["value"] for r in rows if r.get("receipt_id") == "sub1" and r["type"] == "measurement"] == [72.5]
    # the same events replayed give the same ledger rows apart from nothing: ids and keys are deterministic
    assert [(r["id"], r.get("ekey")) for r in led.rows()] == [(r["id"], r.get("ekey")) for r in
                                                              ledger_seed.reconstruct(log.turns).rows()]


def test_ledger_core_seed_has_handoff_summary_card_and_the_stage_tail():
    log = _log()
    seed = seeds.get("ledger_core")(seeds.CutContext(log, log.cuts[0]))
    assert seed.startswith(f"{loader.SEED_PREFIX} and await the next task.\n{HANDOFF}\nLEDGER SUMMARY")
    assert "Open hypotheses (1):" in seed and "Shrink the twiddle table for the radix kernel" in seed
    assert "worse b" not in seed and "worse a1b2c3d" in seed  # the dead end names its SHA and outcome
    assert "Last reported result:\nSTATE Op00 @ 10:00" in seed and CARD.splitlines()[-1] in seed
    assert "FINAL: abandon tiling, try the twiddle table next." in seed and seed.endswith(seeds.CHECKPOINT_TAIL)
    assert len(seed) < len(HANDOFF) + 3000 + checkpoint.SEED_BUDGET_DEFAULT + 100
    assert ledger_seed._card(log.cuts[0].seed) == CARD
    assert ledger_seed._card("no card here") == ""
    no_report = AgentLog("x", "p", [t for t in log.turns], [], [], log.cuts)
    no_report.turns[1].tools = [c for c in no_report.turns[1].tools if c.name != "parhelion_agent_report"]
    assert ledger_seed._stage(no_report.turns[:2]) == "ok"  # no stage report: the last reply tail


def test_bench_runs_the_scheme_next_to_current(tmp_path):
    path = build_log(tmp_path / "mesh-dee-aaaaaa.jsonl")
    res = bench.run([path], schemes=["current", "ledger_core", "full_history"])
    cur, core = res["schemes"]["current"], res["schemes"]["ledger_core"]
    assert cur["overall"]["probes"] == core["overall"]["probes"] > 0
    assert core["overall"]["ci95"] and set(core["by_type"]) == set(cur["by_type"])
    assert res["seed_chars"]["ledger_core"]["median"] > 0


def test_loader_recognizes_both_handoff_prompts():
    assert loader.classify_prompt("Write a short handoff: goal, constraints", 3) == "handoff"
    assert loader.classify_prompt("Write a structured handoff for the next session", 3) == "handoff"
    assert loader.classify_prompt("Your handoff is missing the heading(s): Next experiment", 3) == "followup"
