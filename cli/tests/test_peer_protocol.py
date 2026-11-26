"""Acceptance tests for the 4.2 F peer protocol.

Covered: envelope validation, the legacy ``message`` parameter, the 1200-character attachment, the
repeated-quote strip, the recipient's ``source=peer`` ledger row, merged delivery at the turn
boundary with exactly one hub event, immediate administrator/judge delivery, the persisted
``result`` message, and the MCP handler / daemon route carrying the same envelope.

A spawn turn ends right after the worker turns idle, and its turn-end hook may already deliver a
message the test just queued. Delivery assertions therefore read the bus after an explicit flush
instead of assuming a message is still pending.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core.agents import peer  # noqa: E402
from opsb_core.agents.adapters import BaseWorkerAdapter  # noqa: E402
from opsb_core.agents.event_hub import EventHub  # noqa: E402
from opsb_core.agents.manager import AgentMeshManager  # noqa: E402
from opsb_core.ledger import store as ledger_store  # noqa: E402


class FakeAdapter(BaseWorkerAdapter):
    """Deterministic worker; ``gate`` holds a turn open so a peer batch can pile up behind it."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.turns: list[str] = []
        self.gate: threading.Event | None = None
        self.reply = ""

    def send_turn(self, prompt, on_event=None):
        self.turns.append(prompt)
        gate = self.gate
        if gate is not None:
            gate.wait(5.0)
        return self.reply or f"turn {len(self.turns)} done"

    def stop(self):
        pass


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PARHELION_HOME", str(tmp_path / "phome"))
    monkeypatch.setenv("PARHELION_DISABLE_DAEMON_PROXY", "1")
    # Peer delivery is driven by the test at explicit boundaries unless it asks for the timer.
    monkeypatch.setattr("opsb_core.agents.manager.PEER_COALESCE_SEC", 30.0)
    ws = (tmp_path / "contest").resolve()
    ws.mkdir()
    adapters: dict[str, FakeAdapter] = {}

    def factory(workspace, worktree, driver, agent_id, log_path):
        adapter = FakeAdapter(workspace, worktree, driver, agent_id, log_path)
        adapters[agent_id] = adapter
        return adapter

    mgr = AgentMeshManager(adapter_factory=factory)
    monkeypatch.setattr(mgr, "_cached_doctor", lambda: {"diagnostics": [
        dict(kind="deepseek", auth_ok=True, binary_path="dsh", installed=True)]})
    yield ws, mgr, adapters
    mgr.stop_all()


def _spawn(mgr, ws, *, problem="Demo", axis="", role="worker"):
    res = mgr.spawn_agent(ws, driver="deepseek", task="work", problem=problem, axis=axis, role_label=role,
                          worktree_mode="readonly", _sprint=True)
    agent_id = res["agent"]["agent_id"]
    assert _wait(lambda: mgr._store(ws).get_agent(agent_id)["status"] == "idle")
    return agent_id


def _messages(ws, mgr, agent_id, **kwargs):
    return mgr._store(ws).list_messages(agent_id=agent_id, limit=200, **kwargs)


def _peer_rows(ws, mgr, agent_id):
    return [row for row in _messages(ws, mgr, agent_id, direction="squad_to_worker") if row["kind"] == "peer"]


def _deliver(ws, mgr, agent_id, expect=1):
    """Flush the recipient's queue and return the newest peer body (a boundary may have delivered it)."""
    mgr._flush_peer(ws, agent_id)
    assert _wait(lambda: len(_peer_rows(ws, mgr, agent_id)) >= expect), "the peer batch reached the bus"
    return _peer_rows(ws, mgr, agent_id)[-1]["body"]


def _pending(ws, agent_id):
    path = peer.pending_path(ws, agent_id)
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _ledger_rows(ws, worker, problem="Demo"):
    return ledger_store.read_rows(ledger_store.problem_dir(ws, problem) / f"{worker}.jsonl")


def _attachment_of(body):
    return Path(body.split("full: ", 1)[1].split(" (", 1)[0])


# 1. envelope ---------------------------------------------------------------------------------

def test_envelope_validation_aliases_listing_and_request_id(env):
    ws, mgr, _ = env
    sender = _spawn(mgr, ws, axis="algorithm")
    mate = _spawn(mgr, ws, axis="dataflow")

    with pytest.raises(ValueError, match="280"):
        mgr.squad(ws, sender, recipient=mate, claim="x" * 281, evidence={"class": "local", "ref": "run-p14"})
    with pytest.raises(ValueError, match="ledger ids"):
        mgr.squad(ws, sender, recipient=mate, claim="x", refs=["L-1", "not-a-ref"], evidence={"class": "local", "ref": "run-p14"})
    with pytest.raises(ValueError, match="claim or message"):
        mgr.squad(ws, sender, recipient=mate, evidence={"class": "local", "ref": "run-p14"})
    with pytest.raises(ValueError, match="evidence ref is limited"):
        mgr.squad(ws, sender, recipient=mate, claim="x", evidence={"class": "local", "ref": "q" * 301})

    assert {p["agent_id"] for p in mgr.squad(ws, sender)["peers"]} == {mate}

    out = mgr.squad(ws, sender, to=mate, claim="the small bucket repeats once",
                    refs=["L-0001", "platform/L-0002"], evidence={"class": "local", "ref": "results/Demo/report.json#bucket"},
                    ask="repeat?")
    assert out["ok"] and out["recipient"] == mate and not out["duplicate"] and out["msg_id"]
    body = _deliver(ws, mgr, mate)
    assert f"from={sender}" in body and "refs=L-0001,platform/L-0002" in body
    assert "evidence=local:results/Demo/report.json#bucket" in body  # 4.3 P27: class:ref
    assert "claim: the small bucket repeats once" in body and "ask: repeat?" in body

    again = mgr.squad(ws, sender, to=mate, claim="retry", request_id="r-1", evidence={"class": "local", "ref": "run-p14"})
    assert not again["duplicate"]
    retried = mgr.squad(ws, sender, to=mate, claim="retry", request_id="r-1", evidence={"class": "local", "ref": "run-p14"})
    assert retried["duplicate"] and retried["recipient"] == mate
    _deliver(ws, mgr, mate, expect=2)
    assert len(_peer_rows(ws, mgr, mate)) == 2  # the retry is not delivered twice


# 2. legacy parameter and the attachment ------------------------------------------------------

def test_legacy_message_short_is_the_claim_and_long_becomes_an_attachment(env):
    ws, mgr, _ = env
    sender = _spawn(mgr, ws, axis="algorithm")
    mate = _spawn(mgr, ws, axis="dataflow")

    mgr.squad(ws, sender, recipient=mate, message="repeat bucket small", evidence={"class": "local", "ref": "run-p14"})
    short = _deliver(ws, mgr, mate)
    assert "claim: repeat bucket small" in short and "text:" not in short and "full:" not in short

    marker = "middle-of-the-report"
    body = "".join(f"line {i:03d} of the experiment record\n" for i in range(80)) + marker + "\n" + "tail " * 40
    assert len(body) > peer.ATTACH_MAX
    out = mgr.squad(ws, sender, recipient=mate, message=body, evidence={"class": "local", "ref": "run-p14"})
    assert out["hint"]  # the sender is told to write a claim next time
    long_body = _deliver(ws, mgr, mate, expect=2)
    attachment = _attachment_of(long_body)
    assert attachment.is_file() and attachment.parent.name == "peer"
    stored = attachment.read_text(encoding="utf-8")
    assert body.strip() in stored and marker in stored
    assert "claim: " in long_body and marker not in long_body and len(long_body) < 1200


# 3. repeated quote ---------------------------------------------------------------------------

def test_repeated_quote_is_stripped_while_refs_survive(env):
    ws, mgr, _ = env
    sender = _spawn(mgr, ws, axis="algorithm")
    mate = _spawn(mgr, ws, axis="dataflow")
    paragraphs = "\n".join(f"evidence row {i}: the small bucket is stable" for i in range(8))

    mgr.squad(ws, sender, recipient=mate, claim="first copy", message=paragraphs, evidence={"class": "local", "ref": "run-p14"})
    assert "evidence row 0" in _deliver(ws, mgr, mate)

    mgr.squad(ws, sender, recipient=mate, claim="second copy, cite it", message=paragraphs, refs=["L-0007"], evidence={"class": "local", "ref": "run-p14"})
    _deliver(ws, mgr, mate, expect=2)
    second = next(row["body"] for row in _peer_rows(ws, mgr, mate) if "second copy" in row["body"])
    assert "evidence row 0" not in second and "quote: duplicate" in second
    assert "L-0007" in second


# 4. recipient ledger -------------------------------------------------------------------------

def test_recipient_ledger_gets_a_peer_unverified_row(env):
    ws, mgr, _ = env
    sender = _spawn(mgr, ws, axis="algorithm")
    mate = _spawn(mgr, ws, axis="dataflow")

    out = mgr.squad(ws, sender, recipient=mate, claim="the repeat bucket is stable on small shapes",
                    refs=["L-0003"], evidence={"class": "local", "ref": "results/Demo/run/report.json"})
    row = _ledger_rows(ws, mate)[-1]
    assert row["type"] == "insight" and row["source"] == "peer" and row["confidence"] == "unverified"
    assert "repeat bucket is stable" in row["claim"] and row["links"] == ["L-0003"]
    assert sender in row["why"] and row["evidence"] == ["results/Demo/run/report.json"]
    assert out["ledger_id"] == row["id"]


# 5. merged delivery --------------------------------------------------------------------------

def test_peer_messages_merge_at_the_turn_boundary_into_one_hub_event(env):
    ws, mgr, adapters = env
    sender = _spawn(mgr, ws, axis="algorithm")
    mate = _spawn(mgr, ws, axis="dataflow")
    gate = threading.Event()
    adapters[mate].gate = gate
    mgr.send_message(ws, mate, "hold this turn")  # orch_to_worker: immediate, never merged
    assert _wait(lambda: mgr._runners[mate].busy)
    hub = EventHub(ws, mgr._store(ws), background=False)  # primed after the follow-up row

    for index in range(3):
        mgr.squad(ws, sender, recipient=mate, claim=f"finding number {index}", evidence={"class": "local", "ref": "run-p14"})
    assert len(_pending(ws, mate)) == 3
    assert _peer_rows(ws, mgr, mate) == []
    hub.scan_now()
    assert not [ev for ev in hub._events if ev.source == "inbox" and ev.audience == "worker"]

    adapters[mate].gate = None
    gate.set()
    assert _wait(lambda: len(_peer_rows(ws, mgr, mate)) == 1)
    row = _peer_rows(ws, mgr, mate)[0]
    assert row["body"].startswith("[peer batch ×3]")
    assert all(f"finding number {index}" in row["body"] for index in range(3))
    assert _pending(ws, mate) == []

    hub.scan_now()
    events = [ev for ev in hub._events if ev.source == "inbox" and ev.audience == "worker"
              and ev.agent_id == mate]
    assert len(events) == 1  # the merged row is the only peer wake
    assert _wait(lambda: sum("[peer batch" in prompt for prompt in adapters[mate].turns) == 1)


def test_an_idle_recipient_gets_one_coalesced_wake_for_a_burst(env, monkeypatch):
    ws, mgr, adapters = env
    monkeypatch.setattr("opsb_core.agents.manager.PEER_COALESCE_SEC", 0.05)
    sender = _spawn(mgr, ws, axis="algorithm")
    mate = _spawn(mgr, ws, axis="dataflow")

    for index in range(2):
        mgr.squad(ws, sender, recipient=mate, claim=f"idle finding {index}", evidence={"class": "local", "ref": "run-p14"})
    assert _wait(lambda: len(_peer_rows(ws, mgr, mate)) == 1)
    assert _wait(lambda: sum("[peer batch" in prompt for prompt in adapters[mate].turns) == 1)
    assert "idle finding 0" in _peer_rows(ws, mgr, mate)[0]["body"]


# 6. immediate paths --------------------------------------------------------------------------

def test_admin_directive_and_judge_feedback_do_not_join_the_peer_batch(env):
    ws, mgr, adapters = env
    sender = _spawn(mgr, ws, axis="algorithm")
    mate = _spawn(mgr, ws, axis="dataflow")
    gate = threading.Event()
    adapters[mate].gate = gate
    mgr.send_message(ws, mate, "hold this turn")
    assert _wait(lambda: mgr._runners[mate].busy)
    for index in range(2):
        mgr.squad(ws, sender, recipient=mate, claim=f"peer finding {index}", evidence={"class": "local", "ref": "run-p14"})

    mgr.post_admin_directive(ws, "switch the axis now", problem="Demo", broadcast_workers=True)
    immediate = [row for row in _messages(ws, mgr, mate, direction="orch_to_worker")
                 if "switch the axis now" in row["body"]]
    assert immediate, "an administrator directive is a separate immediate row"
    assert len(_pending(ws, mate)) == 2  # the peer batch still waits for the boundary

    results = ws / "results"
    results.mkdir(exist_ok=True)
    sha, queued_at = "a" * 40, "2026-11-01T00:00:00Z"
    (results / "submit-queue.jsonl").write_text(json.dumps({
        "title": "Demo", "sha": sha, "queued_at": queued_at, "agent_id": mate}) + "\n", encoding="utf-8")
    (results / "submit-queue-outcome.jsonl").write_text(json.dumps({
        "title": "Demo", "sha": sha, "queued_at": queued_at, "submission_id": "s-p14", "status": "Pass",
        "score": 40, "submitted_at": "2026-11-01T00:00:04Z"}) + "\n", encoding="utf-8")
    mgr.deliver_judgements(ws, 5)
    judged = [row for row in _messages(ws, mgr, mate, direction="orch_to_worker")
              if row["kind"] == "judge_result"]
    assert judged, "judge feedback is a separate immediate row"

    adapters[mate].gate = None
    gate.set()
    assert _wait(lambda: len(_peer_rows(ws, mgr, mate)) == 1)
    body = _peer_rows(ws, mgr, mate)[0]["body"]
    assert "peer finding 0" in body and "peer finding 1" in body
    assert "switch the axis now" not in body and "judge_result" not in body


# 7. result messages --------------------------------------------------------------------------

def test_stage_result_is_persisted_with_a_claim_and_a_ledger_decision(env):
    ws, mgr, _ = env
    mate = _spawn(mgr, ws, axis="dataflow")
    body = "\n".join(f"result line {i}: the hypothesis held on small shapes" for i in range(70))
    assert len(body) > peer.ATTACH_MAX

    out = mgr.worker_report(ws, mate, kind="result", summary=body)
    path = Path(out["attachment"])
    assert path.is_file() and path.parent.name == "peer" and path.suffix == ".md"
    assert body.strip() in path.read_text(encoding="utf-8")

    row = [item for item in _messages(ws, mgr, mate, direction="worker_to_orch") if item["kind"] == "result"][-1]
    assert row["body"].startswith("[result ") and "claim:" in row["body"] and str(path) in row["body"]
    assert len(row["body"]) < 700 and "result line 40" not in row["body"]

    decision = _ledger_rows(ws, mate)[-1]
    assert decision["type"] == "decision" and decision["evidence"] == [str(path)]
    assert decision["claim"] and out["ledger_id"] == decision["id"]

    short = mgr.worker_report(ws, mate, kind="result", summary="checks passed")
    assert Path(short["attachment"]).is_file()
    latest = [item for item in _messages(ws, mgr, mate, direction="worker_to_orch") if item["kind"] == "result"][-1]
    assert latest["body"] == "checks passed"


def test_a_long_turn_reply_is_attached_by_the_runner(env):
    ws, mgr, adapters = env
    mate = _spawn(mgr, ws, axis="dataflow")
    adapters[mate].reply = "\n".join(f"turn row {i}: measured the repeat bucket" for i in range(60))
    mgr.send_message(ws, mate, "next step")

    def attached():
        return [row for row in _messages(ws, mgr, mate, direction="worker_to_orch")
                if row["kind"] == "result" and row["body"].startswith("[result ")]

    assert _wait(attached)
    row = attached()[-1]
    path = _attachment_of(row["body"])
    assert path.is_file() and "turn row 59" in path.read_text(encoding="utf-8")
    assert "turn row 30" not in row["body"]


# 8. schema, handler and daemon route carry the same envelope ---------------------------------

def test_the_mcp_tool_and_the_daemon_route_forward_the_envelope(env):
    from opsb_core.daemon.agent_routes import handle_agent_post
    from opsb_core.mcp.handlers_agents import bind_daemon_agent_manager, squad_body
    from opsb_core.mcp.tools import call_tool

    ws, mgr, _ = env
    sender = _spawn(mgr, ws, axis="algorithm")
    mate = _spawn(mgr, ws, axis="dataflow")

    body = squad_body({"to": "dee-1", "claim": "claim", "refs": ["L-0001"],
                       "evidence": {"class": "paired", "ref": "results/x#a"}, "ask": "why"})
    assert body == {"recipient": "", "to": "dee-1", "message": "", "request_id": "", "claim": "claim",
                    "evidence": {"class": "paired", "ref": "results/x#a"}, "ask": "why", "refs": ["L-0001"]}

    bind_daemon_agent_manager(mgr)
    try:
        out = call_tool("parhelion_squad", {"claim": "through the MCP handler", "refs": ["L-0009"], "to": mate,
                                            "evidence": {"class": "local", "ref": "run-p14"}},
                        ws, role="worker", agent_id=sender)
    finally:
        bind_daemon_agent_manager(None)
    assert out["recipient"] == mate
    delivered = _deliver(ws, mgr, mate)
    assert "claim: through the MCP handler" in delivered and "L-0009" in delivered

    seen: dict = {}

    class Mesh:
        def squad(self, workspace, agent_id, **kwargs):
            seen.update(kwargs)
            return {"ok": True}

    class Runtime:
        agent_mesh = Mesh()

    status, routed = handle_agent_post(f"/v1/agents/{sender}/squad",
                                       {"to": mate, "claim": "through the route", "refs": ["L-0004"],
                                        "evidence": {"class": "local", "ref": "run-p14"}}, ws, Runtime())
    assert status == 200 and routed["ok"]
    assert seen["to"] == mate and seen["claim"] == "through the route" and seen["refs"] == ["L-0004"]
