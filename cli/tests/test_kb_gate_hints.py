"""P28 scope 4: the submit gate reply carries deterministic anti-pattern/bug hints.

The hints are advisory: these tests pin relevance, the two-line/600-byte budget and the fact that
nothing about the gate verdict or the bridge call changes when hints are attached.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core.knowledge.hints import (  # noqa: E402
    MAX_HINTS, MAX_HINT_BYTES, gate_hints, select_gate_hints,
)
from opsb_core.mcp import handlers_bench  # noqa: E402

PILLARS = ("breakthrough", "snippets", "hardware", "community", "bugs")


def _write_indexes(home: Path, entries: dict[str, list[dict]]) -> Path:
    for pillar in PILLARS:
        rows = entries.get(pillar) or []
        path = home / pillar / "index.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 1, "entries": rows}, ensure_ascii=False), encoding="utf-8")
    return home


def _anti(entry_id: str, title: str, summary: str, *, pillar: str = "hardware", tags: list[str] | None = None) -> dict:
    return {"id": entry_id, "pillar": pillar, "title": title, "summary": summary, "category": "hardware-law",
            "tags": tags or ["anti-pattern"], "bottlenecks": [], "status": "anti-pattern",
            "portability": "anti-pattern", "evidence": [], "doc_path": f"entries/{entry_id}.md"}


def _bug(entry_id: str, problem: str, *, category: str = "WA", lesson: str = "", summary: str = "") -> dict:
    return {"id": entry_id, "pillar": "bugs", "problem": problem, "title": f"{problem} · {category}",
            "category": category, "summary": summary or f"{problem} local pass before judge failure",
            "lesson": lesson, "fix": {}, "status": "unverified", "tags": [problem, category]}


@pytest.fixture()
def kb_home(tmp_path: Path) -> Path:
    return _write_indexes(tmp_path / "home", {
        "hardware": [
            _anti("HW-noinline-hot-loop", "NEVER call noinline inside hot loops",
                  "Noinline inside the hot loop spills 12 scalars to HBM and loses 6 points.",
                  tags=["anti-pattern", "noinline", "spill"]),
            _anti("HW-core-ladder", "Avoid the ten-core dispatch ladder", "Bimodal latency on ten cores.",
                  tags=["anti-pattern", "cores"]),
        ],
        "community": [
            _anti("COM-unrelated", "Unrelated community dead end", "Do not use this vector layout.",
                  pillar="community", tags=["anti-pattern", "layout"]),
        ],
        "bugs": [
            _bug("BUG-testop-1", "TestOp", category="WA", lesson="Judge case 15 has a 4-ulp precision miss."),
            _bug("BUG-otherop-1", "OtherOp", category="TLE", lesson="Compile budget blew up on template instantiations."),
        ],
    })


def test_hints_rank_the_same_problem_before_keyword_matches(kb_home: Path) -> None:
    same = select_gate_hints(problem="TestOp", subject="", note="", parhelion_home=kb_home)
    assert len(same) == 1 and same[0].startswith("!BUG-testop-1 ")
    assert "precision" in same[0]

    keyword = select_gate_hints(problem="ThirdOp", subject="stop the noinline spill", note="", parhelion_home=kb_home)
    assert [line.split(" ")[0] for line in keyword] == ["!HW-noinline-hot-loop"]
    assert "hot loops" in keyword[0]

    # Same problem wins the first slot; a keyword match fills the second.
    both = select_gate_hints(problem="TestOp", subject="noinline spill", note="", parhelion_home=kb_home)
    assert both[0].startswith("!BUG-testop-1 ")
    assert both[1].startswith("!HW-noinline-hot-loop ")


def test_hints_reject_unrelated_candidates(kb_home: Path) -> None:
    assert select_gate_hints(problem="NoSuchProblem", subject="restore the budget", note="", parhelion_home=kb_home) == []
    # One shared keyword is not a mechanism match.
    assert select_gate_hints(problem="NoSuchProblem", subject="noinline", note="", parhelion_home=kb_home) == []
    # The submission note is part of the query text, exactly like the subject.
    assert select_gate_hints(problem="NoSuchProblem", subject="", note="noinline spill", parhelion_home=kb_home)


def test_hints_stay_within_two_lines_and_600_bytes(tmp_path: Path) -> None:
    long_summary = "spill " * 400
    entries = [
        _anti(f"HW-long-{i}", f"Noinline hot loop rule {i}", long_summary, tags=["noinline", "spill", "hot-loop"])
        for i in range(6)
    ]
    home = _write_indexes(tmp_path / "long", {"hardware": entries})
    lines = select_gate_hints(problem="Op", subject="noinline spill hot loop", note="", parhelion_home=home)
    assert len(lines) <= MAX_HINTS
    assert sum(len(line.encode("utf-8")) for line in lines) + len(lines) - 1 <= MAX_HINT_BYTES
    for line in lines:
        assert "\n" not in line and line.strip() == line
        assert len(line.encode("utf-8")) <= MAX_HINT_BYTES // MAX_HINTS
    tight = select_gate_hints(problem="Op", subject="noinline spill hot loop", note="", parhelion_home=home, max_bytes=220)
    assert sum(len(line.encode("utf-8")) for line in tight) + max(0, len(tight) - 1) <= 220


def test_hints_are_deterministic(kb_home: Path) -> None:
    first = select_gate_hints(problem="TestOp", subject="noinline spill", note="", parhelion_home=kb_home)
    second = select_gate_hints(problem="TestOp", subject="noinline spill", note="", parhelion_home=kb_home)
    assert first == second


def test_gate_hints_never_raise_without_a_kb(tmp_path: Path) -> None:
    assert gate_hints(tmp_path, "TestOp", "a" * 40, "noinline spill", parhelion_home=tmp_path / "missing") == []


def _fake_bridge(seen: list[list[str]]):
    def bridge(workspace, action, options, **kwargs):
        seen.append([action, *options])
        return {"stages": [{"stage": "SAFETY", "passed": True, "code": "PASS", "reason": "ok", "metrics": {}},
                           {"stage": "NOISE_FLOOR", "passed": True, "code": "PASS", "reason": "ok", "metrics": {}},
                           {"stage": "CIRCUIT_BREAKER", "passed": True, "code": "CIRCUIT_CLOSED", "reason": "ok", "metrics": {}},
                           {"stage": "QUOTA_RESERVE", "passed": True, "code": "QUOTA_OK", "reason": "ok", "metrics": {}}],
                "approved": True, "status_tag": "APPROVE", "priority_score": 7.5, "intent": "exploit",
                "estimate": {"estimated_score": 51.0, "current_score": 50.0, "delta_expected": 1.0,
                             "detail": "", "estimation_method": "case_bench_paired"},
                "enqueued": True}
    return bridge


VERDICT_KEYS = ("stages", "gates", "approved", "badge", "next_step", "estimated_score", "current_score",
                "delta_expected", "wspt_priority", "reasons", "enqueued", "dry_run", "status_tag",
                "priority_score", "intent")


def test_gate_reply_appends_hints_without_touching_the_verdict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(handlers_bench, "invoke_bridge", _fake_bridge(seen))
    monkeypatch.setattr(handlers_bench, "_problem_commit", lambda ws, problem, sha: ("TestOp", "b" * 40))
    import opsb_core.knowledge.hints as hints_mod

    monkeypatch.setattr(hints_mod, "gate_hints",
                        lambda *a, **k: ["!HW-noinline-hot-loop [anti-pattern|hardware-law]: never noinline"])
    with_hints = handlers_bench.tool_parhelion_submit_gate({"problem": "TestOp", "sha": "HEAD"}, tmp_path)
    assert with_hints["knowledge_hints"] == ["!HW-noinline-hot-loop [anti-pattern|hardware-law]: never noinline"]
    assert len(with_hints["knowledge_hints"][0].encode("utf-8")) <= MAX_HINT_BYTES

    monkeypatch.setattr(hints_mod, "gate_hints", lambda *a, **k: [])
    without = handlers_bench.tool_parhelion_submit_gate({"problem": "TestOp", "sha": "HEAD"}, tmp_path)
    assert "knowledge_hints" not in without
    # The verdict is byte-for-byte identical and the bridge saw the same arguments both times.
    assert {k: with_hints.get(k) for k in VERDICT_KEYS} == {k: without.get(k) for k in VERDICT_KEYS}
    assert seen[0] == seen[1]
    assert seen[0][0] == "evaluate"


def test_enqueue_also_carries_the_hints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(handlers_bench, "invoke_bridge",
                        lambda ws, action, options, **kw: {"enqueued": True, "verdict": _fake_bridge(seen)(ws, action, options)})
    monkeypatch.setattr(handlers_bench, "_problem_commit", lambda ws, problem, sha: ("TestOp", "b" * 40))
    monkeypatch.setattr(handlers_bench, "has_pending_sync_error_for_commit", lambda rows, title, sha: False)
    import opsb_core.knowledge.hints as hints_mod

    monkeypatch.setattr(hints_mod, "gate_hints", lambda *a, **k: ["!BUG-testop-1 [bugs|WA]: judge case 15 precision"])
    result = handlers_bench.tool_parhelion_submit_gate({"problem": "TestOp", "sha": "HEAD", "enqueue": True}, tmp_path)
    assert result["enqueued"] is True and result["dry_run"] is False
    assert result["knowledge_hints"] == ["!BUG-testop-1 [bugs|WA]: judge case 15 precision"]
