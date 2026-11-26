"""4.3 P24: the S1 mechanical health check, its gate wiring and the judge time-risk hold.

Everything runs on temporary workspaces. The judge's own numbers are fixtures: the repository keeps
only neutral defaults, deployment values live in ``config.json``.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import types
from unittest import mock

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
for path in (str(ROOT), str(ROOT / "cli")):
    if path not in sys.path:
        sys.path.insert(0, path)

from scheduler import health  # noqa: E402
from scheduler.gate import GateConfig, StageResult, SubmissionGate  # noqa: E402

BASE, FIX, BAD = "a" * 40, "f" * 40, "b" * 40


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """An isolated ``~/.parhelion`` so ``config.json`` (the ``judge`` block) is under the test."""
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("PARHELION_HOME", str(root))
    return root


@pytest.fixture()
def ws(tmp_path, home):
    root = tmp_path / "ws"
    (root / "results" / "Op").mkdir(parents=True)
    return root


def _record(ws, sha, **fields):
    folder = ws / "results" / "Op" / sha
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "result.json").write_text(json.dumps({"sha": sha, "exit_code": 0, **fields}))


def _run_report(ws, sha, run, build=None, *, formal=True, level="final", reuse=None, finished="2026-11-26T03:00:00+00:00"):
    path = ws / "results" / "Op" / sha / "bench-runs" / run / "report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"sha": sha, "title": "Op", "run_id": run, "formal": formal, "level": level, "verified": True,
           "finished_at": finished, "cases": []}
    if build is not None:
        doc["build_metrics"] = [{"tree": "checkout", "candidate": True, **build}]
    if reuse is not None:
        doc["settings"] = {"reuse_session": reuse}
    path.write_text(json.dumps(doc))


# ---- the check itself ------------------------------------------------------------------------------

def test_missing_data_is_skipped_not_held(ws):
    view = health.check(ws, "Op", FIX, BASE)
    assert view["checked"] is False and view["warnings"] == [] and view["time_risk"] == {}
    assert "no formal run record" in view["skipped"]


def test_the_formal_run_report_is_the_primary_source(ws):
    _record(ws, BASE, phase_a_cpu_sec=40.0, template_instantiations=10, kernel_o_max_bytes=50_000)
    _record(ws, FIX, kernel_o_sum_bytes=900_000)  # result.json only carries what the report lacks
    _run_report(ws, FIX, "r1", {"compile_sec": 200.0, "template_instantiations": 60,
                                "kernel_o_max_bytes": 120_000}, reuse=False)
    view = health.check(ws, "Op", FIX, BASE)
    assert view["checked"] is True and view["run_id"] == "r1" and view["sources"][0] == "bench-run"
    assert view["metrics"] == {"compile_sec": 200.0, "template_instantiations": 60.0, "kernel_max_bytes": 120_000.0}
    assert view["board_metrics"]["compile_sec"] == 40.0 and view["session_reuse"] is False
    assert view["growth_pct"] == {"compile_sec": 400.0, "template_instantiations": 500.0,
                                  "kernel_max_bytes": 140.0, "cold_ratio": None}
    codes = {warning["code"] for warning in view["warnings"]}
    assert {"COMPILE_TIME_TLE_RISK", "TEMPLATE_COUNT_WARN", "ELF_OBJECT_WARN", "HEALTH_METRIC_GROWTH"} <= codes
    assert view["status"] == "redline"


def test_an_older_report_format_is_read_at_the_top_level(ws):
    _run_report(ws, FIX, "r1", None)
    path = ws / "results" / "Op" / FIX / "bench-runs" / "r1" / "report.json"
    doc = json.loads(path.read_text())
    doc.update(phase_a_cpu_sec=12.5, template_instantiations=4, kernel_o_max_bytes=2048)
    path.write_text(json.dumps(doc))
    view = health.check(ws, "Op", FIX)
    assert view["metrics"]["compile_sec"] == 12.5 and view["status"] == "ok"


def test_only_formal_runs_are_read(ws):
    _run_report(ws, FIX, "r1", {"compile_sec": 12.0}, formal=False)
    _record(ws, FIX)
    assert health.check(ws, "Op", FIX)["checked"] is False


def test_cold_ratio_comes_from_a_cold3_result(ws):
    _record(ws, FIX, phase_a_cpu_sec=10.0,
            cold3_stats={"first_iter_us": 10.0, "warm_median_us": 2.0, "per_case": {"01": [10.0, 2.0, 2.0]}})
    _record(ws, BASE, phase_a_cpu_sec=10.0,
            cold3_stats={"first_iter_us": 3.0, "warm_median_us": 2.0, "per_case": {"01": [3.0, 2.0, 2.0]}})
    view = health.check(ws, "Op", FIX, BASE)
    assert view["metrics"]["cold_ratio"] == 5.0 and "cold3" in view["sources"]
    assert view["growth_pct"]["cold_ratio"] == 233.3
    assert any(w["metric"] == "cold_ratio" for w in view["warnings"])


# ---- the judge time limit --------------------------------------------------------------------------

def _snapshot(ws, title="Op", **fields):
    path = ws / "results" / title / ".judge-rule.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 1, **fields}))


def _config(home, **judge):
    (home / "config.json").write_text(json.dumps({"judge": judge}))


def test_the_rule_snapshot_supplies_the_limit_and_the_iterations(home, ws):
    _record(ws, FIX, phase_a_cpu_sec=340.0)
    _snapshot(ws, iterations=5, time_limit={"field": "time_limit_ms", "value": 420_000})
    risk = health.check(ws, "Op", FIX)["time_risk"]
    assert risk["limit_sec"] == 420.0 and risk["limit_source"] == "rule-snapshot"
    assert risk["iterations"] == 5 and risk["iterations_source"] == "rule-snapshot"
    assert risk["risk"] is True and risk["required_sec"] == 340.0
    assert "340.0s" in risk["detail"]


def test_the_config_is_the_fallback_and_an_absent_limit_skips(home, ws):
    _record(ws, FIX, phase_a_cpu_sec=400.0)
    _config(home, time_limit_sec=420)
    risk = health.check(ws, "Op", FIX)["time_risk"]
    assert risk["limit_source"] == "config" and risk["risk"] is True
    _config(home)
    risk = health.check(ws, "Op", FIX)["time_risk"]
    assert risk["risk"] is None and "judge.time_limit_sec is unset" in risk["skipped"]
    _snapshot(ws)  # a snapshot without a limit falls back too
    assert health.check(ws, "Op", FIX)["time_risk"]["risk"] is None


def test_the_compile_factor_scales_the_compile_term(home, ws):
    _record(ws, FIX, phase_a_cpu_sec=200.0)
    _config(home, time_limit_sec=420)
    assert health.check(ws, "Op", FIX)["time_risk"]["risk"] is False
    scaled = health.check(ws, "Op", FIX, compile_factor=1.7)["time_risk"]
    assert scaled["risk"] is True and scaled["required_sec"] == 340.0


def test_no_compile_record_skips_the_risk(home, ws):
    _record(ws, FIX, template_instantiations=5)
    _config(home, time_limit_sec=420)
    risk = health.check(ws, "Op", FIX)["time_risk"]
    assert risk["risk"] is None and "no compile seconds" in risk["skipped"]


# ---- the snapshot helpers, the remote runner and the case-bench record -----------------------------

def test_judge_rules_reads_seconds_and_iterations(ws):
    from cli.opsb_core.services import judge_rules

    _snapshot(ws, iterations="5", time_limit={"field": "time_limit_ms", "value": 420_000})
    assert judge_rules.time_limit_sec(ws, "Op") == 420.0
    assert judge_rules.iterations(ws, "Op") == 5
    _snapshot(ws, time_limit={"field": "time_limit_sec", "value": 1.5})
    assert judge_rules.time_limit_sec(ws, "Op") == 1.5
    _snapshot(ws, time_limit={"field": "limits", "value": {"seconds": 2}})
    assert judge_rules.time_limit_sec(ws, "Op") == 2.0
    _snapshot(ws)
    assert judge_rules.time_limit_sec(ws, "Op") is None and judge_rules.iterations(ws, "Op") is None


def test_the_runner_counts_template_instantiations_and_object_bytes(tmp_path):
    spec = importlib.util.spec_from_file_location("p24_runner", ROOT / "runtime/npu_runtime/reference_cases.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    repo = tmp_path / "repo"
    (repo / "op_kernel").mkdir(parents=True)
    (repo / "op_kernel" / "tile.h").write_text(
        "TILING_KEY_IS(1001);\nTILING_KEY_IS(1002);\n"
        "template <typename T> __aicore__ void kernel(T* x) {}\n"
        "template void kernel<float>(float*);\n", encoding="utf-8")
    (repo / "build-cached").mkdir()
    (repo / "build-cached" / "generated.h").write_text("TILING_KEY_IS(1); TILING_KEY_IS(2); TILING_KEY_IS(3);\n")
    build = repo / "build-casebench" / "CMakeFiles"
    build.mkdir(parents=True)
    (build / "kernel.o").write_bytes(b"x" * 1234)
    (build / "other.o").write_bytes(b"x" * 66)
    assert runner.template_instantiations(repo) == 3  # 2 tiling keys + 1 explicit instantiation
    assert runner.object_bytes(repo) == {"kernel_o_count": 2, "kernel_o_sum_bytes": 1300,
                                        "kernel_o_max_bytes": 1234}


def test_case_bench_maps_the_candidate_build_metrics():
    from scheduler import case_bench

    report = {"build_metrics": [
        {"tree": "base", "candidate": False, "compile_sec": 1.0, "template_instantiations": 1},
        {"tree": "cand", "candidate": True, "compile_sec": 12.5, "template_instantiations": 7,
         "kernel_o_count": 3, "kernel_o_sum_bytes": 4096, "kernel_o_max_bytes": 2048},
    ]}
    assert case_bench.build_record(report) == {
        "phase_a_cpu_sec": 12.5, "template_instantiations": 7, "kernel_o_count": 3,
        "kernel_o_sum_bytes": 4096, "kernel_o_max_bytes": 2048}
    assert case_bench.build_record({}) == {}


# ---- the gate wiring -------------------------------------------------------------------------------

def _stage(code="PASS", passed=True, metrics=None, reason="ok"):
    return StageResult(stage="SAFETY", passed=passed, code=code, reason=reason, metrics=metrics or {})


def _health_view(warnings=(), risk=None):
    return {"checked": True, "metrics": {"compile_sec": 200.0}, "board_metrics": {}, "growth_pct": {},
            "warnings": list(warnings), "elf_alerts": [], "skipped": "",
            "time_risk": risk or {"risk": None, "skipped": "not evaluated"}, "status": "warn" if warnings else "ok"}


def _evaluate(*, s1=None, health_view=None, health_checks=True, **config):
    """One ``_evaluate_impl`` over a fixed estimate, board and slot; S1 and health are injected."""
    from scheduler.tests.test_gate_confidence import _estimate

    with tempfile.TemporaryDirectory() as td:
        gate = SubmissionGate(pathlib.Path(td))
        gate.config.health_checks = health_checks
        for key, value in config.items():
            setattr(gate.config, key, value)
        ledger = types.SimpleNamespace(confirmed_board_zero=False, historical_best_sha=BASE,
                                       contemporary_best_score=10.0, raw_historical_best_score=10.0,
                                       true_restore_gain=0.0, active_board_sha=BASE,
                                       active_judged_score=10.0, active_judged_at=None,
                                       historical_best_score=10.0, historical_best_mean=10.0)
        quota = types.SimpleNamespace(remaining_today=140, discretionary_remaining=138, rate_wait_sec=0,
                                      used_today=2, daily_limit=150, reserved_for_closing=2,
                                      needed_closing_restores=0, to_dict=lambda: {})
        breaker = types.SimpleNamespace(state="CLOSED", reason="closed", to_dict=lambda: {})
        patches = [mock.patch.object(gate.estimator, "estimate", return_value=_estimate(4.0, 0.5)),
                   mock.patch.object(gate.estimator, "get_problem_ledger", return_value=ledger),
                   mock.patch.object(gate, "get_circuit_breaker", return_value=breaker),
                   mock.patch.object(gate, "get_quota_status", return_value=quota),
                   mock.patch.object(gate, "probe_allowance", return_value={"allowed": False}),
                   mock.patch.object(gate, "evaluate_safety", return_value=s1 or _stage()),
                   mock.patch.object(gate, "_find_override", return_value={}),
                   mock.patch("scheduler.gate.resolve_sha", return_value=BASE)]
        if health_view is not None:
            patches.append(mock.patch("scheduler.health.check", return_value=health_view))
        for patcher in patches:
            patcher.start()
        try:
            return gate._evaluate_impl("Op", BASE)
        finally:
            for patcher in reversed(patches):
                patcher.stop()


def _warning():
    return {"code": "COMPILE_TIME_TLE_RISK", "severity": "redline", "metric": "phase_a_cpu_sec",
            "value": 200.0, "reason": "build 200s"}


def test_health_lands_in_s1_metrics_and_scales_the_priority():
    base = _evaluate(health_view=_health_view())
    warned = _evaluate(health_view=_health_view([_warning()]))
    assert base.stages[0].metrics["health"]["checked"] is True
    assert warned.approved and warned.priority_score == pytest.approx(base.priority_score * 0.8, rel=1e-3)
    off = _evaluate(health_view=_health_view([_warning()]), health_checks=False)
    assert "health" not in off.stages[0].metrics and off.priority_score == pytest.approx(base.priority_score)


def test_a_judge_time_risk_holds_the_submission():
    risk = {"risk": True, "detail": "compile 340.0s x 1.00 + run 0.000s = 340.00s vs 0.80 x 420.0s limit"}
    held = _evaluate(health_view=_health_view(risk=risk))
    assert held.approved is False and held.status_tag == "REJECTED_GATE"
    s1 = held.stages[0]
    assert s1.code == "JUDGE_TIME_RISK" and s1.metrics["judge_time_risk"]["risk"] is True
    assert "reduce instantiations or compile volume" in s1.reason
    assert "or send it as intent=probe" in s1.reason
    # The stage code, the next step and the badge reach the queue card and the submit-gate reply.
    from opsb_core.services import gate_view
    verdict = held.to_dict()
    fields = gate_view.card_fields(verdict)
    assert fields["badge"] == "HOLD: JUDGE TIME" and fields["gates"]["safety_ok"] is False
    assert "reduce instantiations" in verdict["stages"][0]["metrics"]["next_step"]
    assert fields["health"]["time_risk"]["risk"] is True


def test_the_hold_switch_leaves_a_hint_only():
    risk = {"risk": True, "detail": "compile 340.0s x 1.00 + run 0.000s = 340.00s vs 0.80 x 420.0s limit"}
    passed = _evaluate(health_view=_health_view(risk=risk), health_time_risk_holds=False)
    assert passed.approved and passed.stages[0].code == "PASS"
    assert "judge time risk" in passed.stages[0].reason
    assert passed.stages[0].metrics["health"]["time_risk"]["risk"] is True


def test_a_released_escape_lock_is_explore_at_half_priority():
    ready = _evaluate()
    release = _stage(code="ESCAPE_UNREPRODUCED",
                     reason=f"ESCAPE LOCK released for {BASE[:7]}: {BAD[:7]} failed online (Runtime Error, "
                            "submission s-re) but the failure did not reproduce in 2 formal final runs",
                     metrics={"next_step": "not reproduced", "bad_sha": BAD})
    released = _evaluate(s1=release, health_view=_health_view())
    assert released.approved and released.status_tag == "EXPLORE"
    assert "[S1 escape released]" in released.gate_reason and "bbbbbbb" in released.gate_reason
    assert released.priority_score == pytest.approx(ready.priority_score * 0.5, rel=1e-3)


def test_gate_switches_and_their_defaults():
    cfg = GateConfig()
    assert cfg.escape_unreproduced_releases and cfg.health_checks and cfg.health_time_risk_holds
    assert cfg.health_penalty == 0.8 and cfg.health_time_margin == 0.8 and cfg.judge_compile_factor == 1.0
    assert cfg.unrepro_runs == 2
    from scheduler.gate import GATE_SWITCHES
    for name in ("escape_unreproduced_releases", "health_checks", "health_penalty",
                 "health_time_risk_holds", "health_time_margin", "judge_compile_factor"):
        assert name in GATE_SWITCHES
    assert cfg.set_switch("health_penalty=0.5") == ("health_penalty", 0.5)
    assert cfg.set_switch("judge_compile_factor=1.5") == ("judge_compile_factor", 1.5)  # above 1 allowed
    assert cfg.set_switch("health_time_margin=2") == ("health_time_margin", 1.0)  # a share is clamped
    assert cfg.set_switch("health_checks=off") == ("health_checks", False)


def test_an_approved_probe_is_not_held_by_the_judge_time_risk():
    # D3 holds an exploit; an approved intent=probe (P23) is how such a shot is sent anyway.
    risk = {"risk": True, "detail": "compile 340.0s x 1.00 + run 0.000s = 340.00s vs 0.80 x 420.0s limit"}
    with tempfile.TemporaryDirectory() as td:
        gate = SubmissionGate(pathlib.Path(td))
        with mock.patch("scheduler.health.check", return_value=_health_view(risk=risk)):
            exploit = gate._apply_health("Op", BASE, BASE, _stage())
            probe = gate._apply_health("Op", BASE, BASE, _stage(), probe=True)
    assert exploit.passed is False and exploit.code == "JUDGE_TIME_RISK"
    assert probe.passed is True and probe.code == "PASS"
    assert "judge time risk" in probe.reason and probe.metrics["health"]["time_risk"]["risk"] is True
