"""4.3 P23 wiring: the `intent` argument reaches the gate, the bridge and the tool table."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core.mcp import list_tool_schemas  # noqa: E402
from opsb_core.mcp import handlers_bench  # noqa: E402


def test_submit_gate_schema_carries_the_intent_enum() -> None:
    schemas = {t["name"]: t for t in list_tool_schemas(role="worker")}
    gate = schemas["parhelion_submit_gate"]
    intent = gate["inputSchema"]["properties"]["intent"]
    assert intent == {"type": "string", "enum": ["exploit", "probe", "restore"]}
    assert "force" in gate["inputSchema"]["properties"]  # the 4.2.1 bypass stays declared
    assert gate["inputSchema"]["required"] == ["problem"]


def test_submit_gate_passes_the_intent_to_the_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_bridge(workspace, action, options, **kwargs):
        seen.append([action, *options])
        return {"stages": [{"stage": "SAFETY", "passed": True, "code": "PASS", "reason": "ok", "metrics": {}},
                           {"stage": "NOISE_FLOOR", "passed": True, "code": "PROBE_INTENT", "reason": "probe",
                            "metrics": {"next_step": "roll back with x"}},
                           {"stage": "CIRCUIT_BREAKER", "passed": True, "code": "CIRCUIT_CLOSED", "reason": "ok",
                            "metrics": {}},
                           {"stage": "QUOTA_RESERVE", "passed": True, "code": "QUOTA_OK", "reason": "ok",
                            "metrics": {}}],
                "approved": True, "status_tag": "PROBE", "priority_score": 1.0,
                "intent": "probe", "requested_intent": "probe", "intent_code": "PROBE_INTENT",
                "intent_reason": "probe", "undo": {"kind": "best_board_keeps_its_score"},
                "estimate": {"estimated_score": 51.0, "current_score": 50.0, "delta_expected": 1.0,
                             "detail": "", "estimation_method": "case_bench_paired"}}

    monkeypatch.setattr(handlers_bench, "invoke_bridge", fake_bridge)
    monkeypatch.setattr(handlers_bench, "_problem_commit", lambda ws, problem, sha: ("Op", "a" * 40))
    result = handlers_bench.tool_parhelion_submit_gate(
        {"problem": "Op", "sha": "HEAD", "intent": "probe", "enqueue": False}, tmp_path)
    assert seen[0][0] == "evaluate"
    assert seen[0][-2:] == ["--intent", "probe"]
    assert result["intent"] == "probe"
    assert result["next_step"] == "roll back with x"
    assert result["badge"] == "PROBE"


def test_an_unknown_intent_is_rejected_before_the_gate(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="intent must be one of"):
        handlers_bench.tool_parhelion_submit_gate(
            {"problem": "Op", "sha": "HEAD", "intent": "yolo"}, tmp_path)


def test_opsb_submit_parser_accepts_the_intent() -> None:
    from opsb_core.cli_app import build_parser

    parser = build_parser()
    args = parser.parse_args(["submit", "Op", "abc1234", "--intent", "restore"])
    assert args.intent == "restore"
    assert parser.parse_args(["submit", "Op"]).intent == "exploit"
    with pytest.raises(SystemExit):
        parser.parse_args(["submit", "Op", "--intent", "yolo"])


def test_gate_switches_expose_every_p23_switch() -> None:
    from scheduler.gate import GATE_SWITCHES, GateConfig

    for name in ("submit_intents", "opening_until_first_score", "opening_max_probes",
                 "probe_per_window", "probe_window_hours", "probe_min_prob"):
        assert name in GATE_SWITCHES
        assert hasattr(GateConfig(), name)
    config = GateConfig()
    assert config.set_switch("probe_per_window=4") == ("probe_per_window", 4)
    assert isinstance(config.probe_per_window, int)  # an int switch stays an int
    assert config.set_switch("probe_window_hours=24") == ("probe_window_hours", 24.0)
