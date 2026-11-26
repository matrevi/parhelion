"""4.4 P32: guide cards — size, one active card per seat, delivery on test_wait and the idle inbox,
outcomes (adopted, declined, next local best) and the one-line seed entry.

Temporary workspaces only; no daemon, device or model.
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

from opsb_core.guidance import cards, gap, monitor, settings  # noqa: E402
from opsb_core.ledger import store as lstore  # noqa: E402

S = settings({})
AGENT = "dee-aaaaaa"
NOW = datetime.datetime(2026, 11, 26, 22, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("PARHELION_HOME", str(tmp_path / "ph"))
    root = (tmp_path / "contest").resolve()
    root.mkdir()
    (root / "opsb.workspace.json").write_text(json.dumps({"problems": [{"title": "Op", "path": "Op"}]}))
    return root


def _triage() -> dict:
    rows = [{"case": f"#{i:02d}", "ours_us": 10.0 * r, "tbest_us": 10.0} for i, r in enumerate([400, 300, 8, 2.1], 1)]
    return gap.triage(rows, 5, S)


STUCK = {
    "build_loop": {"signal": "3 builds in a row failed", "evidence": {"error": "kernel.asc:#:#: error: x" * 9,
                                                                       "last_ok_sha": "abcdef1234"}},
    "wa_loop": {"signal": "case 05 failed (WA/RE) in 7 runs in a row",
                "evidence": {"case": "05", "detail": "case 05 WA: missing or unexpected output files"}},
    "scale_mismatch": {"signal": "2 of the top 3 gap cases are >= 50x tbest", "evidence": {"worst": "#01"}},
    "plateau": {"signal": "last 8 timed runs within ±3% of the local best 1.050x", "evidence": {"best": 1.05}},
    "no_candidate": {"signal": "61 min without a submittable candidate", "evidence": {}},
}


@pytest.mark.parametrize("cls", sorted(STUCK))
def test_every_card_is_one_experiment_within_800_bytes(cls):
    lead = {"ref": f"{AGENT}/L-0007", "claim": "the scalar re-interleave is a third of case 04 " * 4}
    text, meta = cards.build(cls, "FFT3DBatchComplex", STUCK[cls], _triage(), lead=lead,
                             kb=["prior-art.md#2 (Radial, online_receipt)"])
    assert len(text.encode("utf-8")) <= 800
    assert text.startswith(f"[guide G-? · {cls} · FFT3DBatchComplex]")
    for word in ("Hypothesis", "Change one thing", "Judge", "Sources"):
        assert word in text
    assert ".parhelion/dossier/index.md" in text and "G-?" in text


def test_scale_card_names_the_points_and_a_probe_and_wa_card_spots_a_silent_case():
    text, meta = cards.build("scale_mismatch", "Op", STUCK["scale_mismatch"], _triage())
    assert "algorithm tier" in text and "100x = +" in text and "Probe first" in text
    assert meta["targets"]["cases"][:2] == ["#01", "#02"]
    wa, _ = cards.build("wa_loop", "Op", STUCK["wa_loop"], None)
    assert "produces no output" in wa and 'cases=["05"]' in wa
    plateau, _ = cards.build("plateau", "Op", STUCK["plateau"], _triage(),
                             lead={"ref": "w/L-0003", "claim": "fuse the passes with FreeFormApiCall<X>"})
    # the agent's own lead is cited by ref; its free-form text (which may name any API) is never quoted
    assert "Untested lead of yours: w/L-0003 (findings.md)." in plateau and "FreeFormApiCall" not in plateau
    build, _ = cards.build("build_loop", "Op", {"signal": "2 builds in a row failed with the same compiler error",
                                                "evidence": {"same_error": True, "signature": "abc123",
                                                             "error": "kernel.asc:#:#: error: no member SomeApi"}},
                           None)
    assert "signature abc123" in build and "SomeApi" not in build


def test_one_active_card_per_seat_supersede_and_repeat_window(ws):
    first = cards.issue(ws, AGENT, "Op", "plateau", "[guide G-? · plateau · Op] x", {}, repeat_sec=1800)
    assert first["card_id"] == "G-1" and "[guide G-1 " in first["text"]
    assert cards.issue(ws, AGENT, "Op", "plateau", "again", {}, repeat_sec=1800) is None
    second = cards.issue(ws, AGENT, "Op", "scale_mismatch", "[guide G-? · s] y", {}, repeat_sec=1800)
    assert second["card_id"] == "G-2"
    seat = cards.read_seats(ws)[AGENT]
    assert seat["card"]["card_id"] == "G-2"
    hist = {c["card_id"]: c for c in cards.history(ws, "Op")}
    assert hist["G-1"]["status"] == "superseded" and hist["G-2"]["status"] == "issued"
    # plateau again inside 30 minutes: not repeated although G-2 replaced G-1
    assert cards.issue(ws, AGENT, "Op", "plateau", "third", {}, repeat_sec=1800) is None
    assert cards.issue(ws, "dee-bbbbbb", "Op", "plateau", "[guide G-? · p] z", {}, repeat_sec=1800)["card_id"] == "G-3"


def test_test_wait_reply_carries_the_card_once_and_only_for_its_worker(ws, monkeypatch):
    from opsb_core.mcp import handlers_agents, handlers_bench
    cards.issue(ws, AGENT, "Op", "wa_loop", "[guide G-? · wa_loop · Op] run case 05", {}, repeat_sec=0)
    monkeypatch.setattr(handlers_agents, "daemon_hub", lambda w: None)
    monkeypatch.setattr(handlers_agents, "_proxy_daemon", lambda *a, **k: {"request_id": "r", "status": "finished"})
    rid = "0" * 32
    orch = handlers_bench.tool_parhelion_test_wait({"request_id": rid}, ws)
    assert "guide" not in orch
    other = handlers_bench.tool_parhelion_test_wait(
        {"request_id": rid, "_worker_request": True, "_caller_agent_id": "dee-bbbbbb"}, ws)
    assert "guide" not in other
    mine = handlers_bench.tool_parhelion_test_wait(
        {"request_id": rid, "_worker_request": True, "_caller_agent_id": AGENT}, ws)
    assert mine["guide"]["id"] == "G-1" and "run case 05" in mine["guide"]["text"]
    assert mine["guide"]["dossier"] == ".parhelion/dossier/index.md"
    again = handlers_bench.tool_parhelion_test_wait(
        {"request_id": rid, "_worker_request": True, "_caller_agent_id": AGENT}, ws)
    assert "guide" not in again
    card = cards.read_seats(ws)[AGENT]["card"]
    assert card["status"] == "delivered" and card["via"] == "test_wait"


class _Mgr:
    def __init__(self, ws):
        from opsb_core.agents.bus_store import AgentBusStore
        self.store = AgentBusStore(ws)
        self.woken: list[str] = []

    def _store(self, ws):
        return self.store

    def _enqueue_to_worker(self, ws, rec, message, **kw):
        self.woken.append(message)
        return {"ok": True}


def _wa_rows(ws, n=3):
    led = lstore.Ledger(ws, "Op", AGENT)
    for i in range(n):
        led.append("experiment", {"claim": f"r{i}", "outcome": "WA", "scope": {"cases": ["05"]},
                                  "why": "case 05 WA: mismatch", "actual_outcome": {"level": "standard"},
                                  "test_run_id": f"r{i}"})
    return led


def test_tick_issues_on_a_new_class_and_delivers_through_the_inbox_when_idle(ws, monkeypatch):
    mgr = _Mgr(ws)
    mgr.store.create_agent({"agent_id": AGENT, "driver": "deepseek", "problem": "Op", "worktree": str(ws)})
    monkeypatch.setattr(monitor, "settings", lambda: dict(S))
    monkeypatch.setattr(monitor, "kb_entries", lambda: [])
    _wa_rows(ws)
    mgr.store.update_agent(AGENT, status="working")
    monitor.tick(mgr, ws, force=True)
    card = cards.read_seats(ws)[AGENT]["card"]
    assert card["class"] == "wa_loop" and card["status"] == "issued" and not mgr.woken  # busy: wait for test_wait
    mgr.store.update_agent(AGENT, status="idle")
    monitor.tick(mgr, ws, force=True)
    assert len(mgr.woken) == 1 and "[guide G-1 · wa_loop · Op]" in mgr.woken[0]
    msgs = [m for m in mgr.store.list_messages(limit=20) if m["kind"] == "guide"]
    assert len(msgs) == 1 and msgs[0]["agent_id"] == AGENT
    assert cards.read_seats(ws)[AGENT]["card"]["via"] == "inbox"
    monitor.tick(mgr, ws, force=True)
    assert len(mgr.woken) == 1  # one card, one delivery


def test_outcomes_adopted_declined_and_the_next_local_best(ws, monkeypatch):
    mgr = _Mgr(ws)
    mgr.store.create_agent({"agent_id": AGENT, "driver": "deepseek", "problem": "Op", "worktree": str(ws)})
    mgr.store.update_agent(AGENT, status="idle")
    monkeypatch.setattr(monitor, "settings", lambda: dict(S))
    monkeypatch.setattr(monitor, "kb_entries", lambda: [])
    led = _wa_rows(ws)
    monitor.tick(mgr, ws, force=True)
    assert cards.read_seats(ws)[AGENT]["card"]["status"] == "delivered"
    led.append("insight", {"claim": "G-1: case 05 also fails on the board SHA, environment"}, source="worker")
    led.append("experiment", {"claim": "fix", "outcome": "better", "base_sha": "b" * 40,
                              "actual_outcome": {"level": "standard", "speedup": 1.2}, "per_case_us": {"05": 3.0}})
    monitor.tick(mgr, ws, force=True)
    card = cards.read_seats(ws)[AGENT]["card"]
    assert card["status"] == "adopted" and card["adopted_by"].endswith("L-0004")
    assert card["next_best"]["speedup"] == 1.2
    events = [json.loads(l) for l in (cards.root(ws) / "cards.jsonl").read_text().splitlines()]
    assert [e["event"] for e in events] == ["issued", "delivered", "status", "next_best"]
    declined = {"card_id": "G-9", "delivered_at": "2026-01-01T00:00:00+00:00"}
    assert cards.adoption(declined, [{"ts": "2026-01-02", "source": "worker", "claim": "skip G-9, not relevant",
                                      "id": "L-1"}])[0] == "declined"
    assert cards.adoption(declined, [{"ts": "2026-01-02", "source": "platform", "claim": "G-9", "id": "L-2"}]) is None


def test_seeds_keep_only_the_card_id_and_status(ws, tmp_path):
    from opsb_core.ledger import handoff, seed
    cards.issue(ws, AGENT, "Op", "plateau", "[guide G-? · plateau · Op] SECRET-BODY", {}, repeat_sec=0)
    line = seed.guide_line(ws, AGENT)
    assert line == "guide card G-1 (plateau): issued; dossier .parhelion/dossier/index.md"
    text = seed.assemble(rules=["r"], card="CARD\n" + line, ledger="", narrative="", tail="", budget=6000)
    assert "G-1" in text
    full = seed.build(ws, "Op", AGENT, [], card="STATE", rules=["r"], budget=6000)
    assert line in full and "SECRET-BODY" not in full
    session = handoff.HandoffSession(ws, "Op", AGENT, tmp_path)
    core = session.ledger_seed("last result")
    assert line in core.text and "SECRET-BODY" not in core.text
