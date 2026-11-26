"""4.4 P30: the stuck classes from ledger-shaped rows, their thresholds, and the class in the hot state.

Pure rows and temporary workspaces only; no daemon, device, model or real contest data.
"""
from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

import pytest

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core import config as _cfg  # noqa: E402
from opsb_core.guidance import settings, stuck  # noqa: E402

T0 = datetime.datetime(2026, 11, 26, 21, 0, tzinfo=datetime.timezone.utc)
S = settings({})


def at(minutes: float) -> str:
    return (T0 + datetime.timedelta(minutes=minutes)).isoformat()


def exp(i: int, minutes: float, outcome: str, *, speedup: float | None = None, level: str = "standard",
        fail: list[str] | None = None, timed: list[str] | None = None, base: str = "b" * 40) -> dict:
    row = {"id": f"L-{i:04d}", "type": "experiment", "ts": at(minutes), "outcome": outcome, "sha": f"{i:07d}",
           "base_sha": base, "test_run_id": f"r{i}", "source": "platform",
           "actual_outcome": {"level": level, "speedup": speedup}}
    if fail:
        row["scope"] = {"cases": fail}
        row["why"] = f"case {fail[0]} WA: mismatch"
    if timed:
        row["per_case_us"] = {c: 10.0 for c in timed}
    return row


def test_build_loop_three_in_a_row_or_the_same_error_twice():
    rows = [exp(1, 0, "better", speedup=1.1, timed=["01"]), *(exp(i, i, "compile_fail") for i in (2, 3, 4))]
    hit = stuck.classify(rows, now=T0 + datetime.timedelta(minutes=5), settings=S)
    assert hit["class"] == "build_loop" and hit["evidence"]["runs"] == 3
    assert hit["evidence"]["last_ok_sha"] == "0000001"

    two = rows[:3]
    sig = stuck.compile_signature("/x/y/kernel.asc:12:4: error: no member named 'Foo' in 'Bar'")
    assert sig and "kernel.asc:#:#" in sig
    same = {"r2": sig, "r3": stuck.compile_signature("/a/kernel.asc:99:1: error: no member named 'Foo' in 'Bar'")}
    ev = stuck.classify(two, now=T0, errors=same, settings=S)["evidence"]
    assert ev["same_error"] is True and ev["signature"] == sig.split(" ")[0] and "no member" in ev["error"]
    other = {"r2": sig, "r3": stuck.compile_signature("kernel.asc:3:1: error: expected ';'")}
    assert stuck.classify(two, now=T0, errors=other, settings=S) is None
    assert stuck.classify([*rows, exp(5, 6, "neutral", speedup=1.0)], now=T0, settings=S) is None


def test_wa_loop_counts_runs_that_include_the_case_until_one_passes_it():
    rows = [exp(1, 0, "WA", fail=["05"]), exp(2, 1, "neutral", speedup=1.0, timed=["01"]),  # 05 not run
            exp(3, 2, "WA", fail=["05"]), exp(4, 3, "compile_fail"), exp(5, 4, "WA", fail=["02", "05"])]
    hit = stuck.classify(rows, now=T0 + datetime.timedelta(minutes=5), settings=S)
    assert hit["class"] == "wa_loop" and hit["evidence"]["case"] == "05" and hit["evidence"]["runs"] == 3
    passed = rows[:2] + [exp(9, 1.5, "neutral", speedup=1.0, timed=["05"])] + rows[2:]
    assert stuck.classify(passed, now=T0, settings=S) is None  # a pass of 05 in between breaks the run
    assert stuck.classify(rows, now=T0, settings={**S, "wa_loop_runs": 4}) is None


def test_plateau_noise_band_and_minutes_without_a_new_local_best():
    rows = [exp(1, 0, "better", speedup=1.20, timed=["01"])]
    rows += [exp(i, i, "neutral", speedup=1.20 + (0.01 if i % 2 else -0.01), timed=["01"]) for i in range(2, 10)]
    hit = stuck.classify(rows, now=T0 + datetime.timedelta(minutes=10), settings=S)
    assert hit["class"] == "plateau" and hit["evidence"]["rule"] == "noise_band"
    assert stuck.classify(rows[:8], now=T0 + datetime.timedelta(minutes=10), settings=S) is None

    slow = [exp(1, 0, "better", speedup=1.2), exp(2, 20, "worse", speedup=1.05), exp(3, 39, "worse", speedup=1.1)]
    assert stuck.classify(slow, now=T0 + datetime.timedelta(minutes=39), settings=S) is None
    hit = stuck.classify(slow, now=T0 + datetime.timedelta(minutes=41), settings=S)
    assert hit["class"] == "plateau" and hit["evidence"]["rule"] == "minutes"
    better = slow + [exp(4, 40, "better", speedup=1.3)]
    assert stuck.classify(better, now=T0 + datetime.timedelta(minutes=41), settings=S) is None
    # a new baseline (the board moved) starts a new local best
    moved = slow + [exp(4, 40, "neutral", speedup=1.0, base="c" * 40)]
    assert stuck.classify(moved, now=T0 + datetime.timedelta(minutes=41), settings=S) is None


def _gap(ratios: list[float]) -> dict:
    from opsb_core.guidance import gap
    rows = [{"case": f"#{i:02d}", "ours_us": 10.0 * r, "tbest_us": 10.0} for i, r in enumerate(ratios, start=1)]
    return gap.triage(rows, 4, S)


def test_scale_mismatch_needs_outliers_on_top_and_only_small_changes():
    rows = [exp(i, i, "neutral", speedup=1.0 + 0.02 * (i % 2)) for i in range(1, 4)]
    hit = stuck.classify(rows, now=T0 + datetime.timedelta(minutes=5), gap=_gap([400, 2, 3]), settings=S)
    assert hit["class"] == "scale_mismatch" and hit["evidence"]["worst"] == "#01"
    assert stuck.classify(rows, now=T0, gap=_gap([20, 2, 3]), settings=S) is None  # no case >= 50x
    big = rows[:2] + [exp(3, 3, "better", speedup=1.4)]
    assert stuck.classify(big, now=T0, gap=_gap([400, 2, 3]), settings=S) is None
    assert stuck.classify(rows, now=T0, gap=_gap([400, 2, 3]), settings={**S, "scale_ratio": 500}) is None


def test_no_candidate_after_an_hour_and_a_gate_request_resets_it():
    start = T0
    rows = [exp(1, 5, "WA", fail=["05"]), exp(2, 30, "compile_fail")]
    assert stuck.classify(rows, now=T0 + datetime.timedelta(minutes=59), start=start, settings=S) is None
    hit = stuck.classify(rows, now=T0 + datetime.timedelta(minutes=61), start=start, settings=S)
    assert hit["class"] == "no_candidate"
    gate = [{"type": "hypothesis", "ts": at(40), "ekey": "gate:abc"}]
    assert stuck.classify(rows, now=T0 + datetime.timedelta(minutes=61), start=start, gates=gate, settings=S) is None
    smoke = rows + [exp(3, 50, "better", speedup=1.1, level="smoke")]  # smoke is not a candidate
    assert stuck.classify(smoke, now=T0 + datetime.timedelta(minutes=61), start=start, settings=S)["class"] == "no_candidate"


def test_partial_config_section_keeps_the_other_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("PARHELION_HOME", str(tmp_path / "ph"))
    s = settings({"guidance": {"wa_loop_runs": 5}})
    assert s["wa_loop_runs"] == 5 and s["plateau_runs"] == 8 and s["no_candidate_minutes"] == 60
    assert _cfg.DEFAULT_CONFIG["guidance"]["scale_ratio"] == 50


def test_class_shows_in_the_agent_mesh_hot_state(tmp_path, monkeypatch):
    """The daemon tick records the class and the hot state carries it on the worker row."""
    from opsb_core.agents.manager import AgentMeshManager
    from opsb_core.guidance import monitor
    from opsb_core.ledger import store as lstore

    monkeypatch.setenv("PARHELION_HOME", str(tmp_path / "ph"))
    ws = (tmp_path / "contest").resolve()
    ws.mkdir()
    (ws / "opsb.workspace.json").write_text(json.dumps({"problems": [{"title": "Op", "path": "Op"}]}))
    mgr = AgentMeshManager()
    mgr._cached_doctor = lambda: {"active_driver": "codex", "diagnostics": []}
    mgr._observed_driver_sessions = lambda w, d: ({}, [])
    st = mgr._store(ws)
    st.create_agent({"agent_id": "dee-aaaaaa", "driver": "deepseek", "problem": "Op", "worktree": str(ws)})
    led = lstore.Ledger(ws, "Op", "dee-aaaaaa")
    for i in range(3):
        led.append("experiment", {"claim": f"r{i} WA", "outcome": "WA", "scope": {"cases": ["05"]},
                                  "actual_outcome": {"level": "standard"}, "test_run_id": f"r{i}"})
    monkeypatch.setattr(monitor, "settings", lambda: {**S, "cards": False})
    assert monitor.tick(mgr, ws, force=True)["seats"][0]["class"] == "wa_loop"
    row = next(r for r in mgr.build_hot_mesh(ws)["workers"] if r["agent_id"] == "dee-aaaaaa")
    assert row["stuck"]["class"] == "wa_loop" and "case 05" in row["stuck"]["signal"]
    assert row["guide"] is None  # cards off: detection only, nothing pushed
