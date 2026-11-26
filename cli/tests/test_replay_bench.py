"""4.2 A4 and section 3: compaction cuts, the four probes, seed schemes, scoring, sprint metrics, CLI.

The mesh log is synthetic: the real field structure with made-up text.
"""
from __future__ import annotations

import contextlib
import datetime
import io
import json
import os
import sys
from pathlib import Path
from unittest import mock

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core.replay import bench, loader, probes, score, seeds, sprint_metrics  # noqa: E402

T0 = datetime.datetime(2026, 1, 2, 3, 0, 0, tzinfo=datetime.timezone.utc)
CWD = "/w/tree"
COMMIT_MSG = "Tile the inner loop by 64 with double buffering for the radix kernel"
HANDOFF = ("Goal: speed up the radix kernel.\n"
           "- abc1234 standard 15/15 but slower (geomean 0.91x), gate held it for low gain.\n"
           "Next experiment: shrink the twiddle table.")


class Mesh:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.rid = 0
        self.t = T0
        self.sid = ""
        self.cid = 0

    def _ts(self) -> str:
        self.t += datetime.timedelta(seconds=10)
        return self.t.isoformat()

    def _out(self, method: str, params: dict) -> int:
        self.rid += 1
        self.rows.append({"ts": self._ts(), "dir": "out", "msg": {"jsonrpc": "2.0", "id": self.rid,
                                                                   "method": method, "params": params}})
        return self.rid

    def _in(self, msg: dict) -> None:
        self.rows.append({"ts": self._ts(), "dir": "in", "msg": {"jsonrpc": "2.0", **msg}})

    def session(self, sid: str) -> None:
        rid = self._out("session/new", {"cwd": CWD, "mcpServers": []})
        self._in({"id": rid, "result": {"sessionId": sid}})
        self.sid = sid

    def _update(self, upd: dict) -> None:
        self._in({"method": "session/update", "params": {"sessionId": self.sid, "update": upd}})

    def turn(self, prompt: str, *, reply: str = "", tools: list[tuple[str, dict, str]] = (), usage: list[int] = ()) -> None:
        rid = self._out("session/prompt", {"sessionId": self.sid, "prompt": [{"type": "text", "text": prompt}]})
        for used in usage[:1]:
            self._update({"sessionUpdate": "usage_update", "used": used, "size": 1_000_000})
        for title, raw, result in tools:
            self.cid += 1
            cid = f"call_{self.cid}"
            self._update({"sessionUpdate": "tool_call", "toolCallId": cid, "title": title, "kind": "other",
                          "status": "in_progress", "rawInput": raw})
            self._update({"sessionUpdate": "tool_call_update", "toolCallId": cid, "status": "completed",
                          "content": [{"type": "content", "content": {"type": "text", "text": result}}]})
        for used in usage[1:]:
            self._update({"sessionUpdate": "usage_update", "used": used, "size": 1_000_000})
        if reply:
            self._update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": reply}})
        self._in({"id": rid, "result": {"stopReason": "end_turn"}})


def bash(cmd: str) -> tuple[str, dict, str]:
    return ("bash", {"command": cmd, "description": "run"}, "output line\n" * 3)


def build_log(path: Path) -> Path:
    m = Mesh()
    m.session("sess-a")
    status = ("mcp__parhelion__parhelion_status", {"problem": "P1"}, '{"ok": true}')
    m.turn("# P1 brief", usage=[10_000, 30_000], tools=[
        ("read", {"file_path": f"{CWD}/src/kernel.cpp"}, "int main() {}\n"),
        bash(f"cd {CWD} && sed -n 1,40p src/kernel.cpp"),
        bash(f"cd {CWD} && git log --oneline | head -5"),
        bash("python3 -c \"import json; print(json.load(open('report.json'))['cases'])\""),
        bash("opsb queue list --json"),
        status,
    ])
    digest = {"summary": {"total": 15, "passed": 15, "counts": {"PASS": 15}},
              "baseline": {"sha": "1111111", "geomean_speedup": 0.91}}
    m.turn("Continue.", usage=[40_000], reply=f"- {COMMIT_MSG} came out slower than the board SHA.", tools=[
        ("bash", {"command": f"cd {CWD} && git commit -am \"{COMMIT_MSG}\""},
         f"[sprint abc1234] {COMMIT_MSG}\n 1 file changed"),
        ("mcp__parhelion__parhelion_test", {"problem": "P1", "sha": "abc1234", "level": "standard"},
         json.dumps({"sha7": "abc1234", "level": "standard", "status": "queued"})),
        ("mcp__parhelion__parhelion_test_wait", {"request_id": "r1"},
         json.dumps({"sha7": "abc1234", "level": "standard", "status": "finished", "digest": digest})),
        ("mcp__parhelion__parhelion_submit_gate", {"problem": "P1", "sha": "abc1234", "note": COMMIT_MSG},
         json.dumps({"short_sha": "abc1234", "status_tag": "HOLD_LOW_GAIN", "gate_reason": "below noise floor",
                     "estimate": {"delta_expected": -0.4}})),
    ])
    m.turn("Write a short handoff: goal, constraints, verified SHAs and evidence, open hypotheses, next experiment.",
           reply=HANDOFF)
    m.session("sess-b")
    m.turn("Preserve this checkpoint and await the next task.\n" + HANDOFF + "\nLast reported result:\nstage",
           reply="Ready.")
    m.turn("[dee-x → dee-aaaaaa] peer note", usage=[12_000], tools=[
        ("read", {"file_path": f"{CWD}/src/kernel.cpp"}, "int main() {}\n"),
        ("mcp__parhelion__parhelion_test", {"problem": "P1", "sha": "abc1234", "level": "standard"},
         json.dumps({"sha7": "abc1234", "level": "standard", "status": "queued"})),
        ("bash", {"command": f"cd {CWD} && git commit -am \"Retry tiling the inner loop by 64 with double buffering radix\""},
         "[sprint def5678] Retry tiling the inner loop by 64 with double buffering radix"),
    ])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in m.rows), encoding="utf-8")
    return path


def test_loader_rebuilds_turns_and_cuts(tmp_path: Path) -> None:
    log = loader.load_agent(build_log(tmp_path / "mesh-dee-aaaaaa.jsonl"))
    assert log.agent_id == "dee-aaaaaa"
    assert [t.kind for t in log.turns] == ["brief", "followup", "handoff", "seed", "peer"]
    assert [s[0] for s in log.sessions] == ["sess-a", "sess-b"]
    assert len(log.cuts) == 1
    cut = log.cuts[0]
    assert (cut.handoff_turn, cut.seed_turn, cut.after_end) == (2, 3, 5)
    assert cut.handoff == HANDOFF and cut.seed.startswith(loader.SEED_PREFIX)
    calls = log.tool_calls()
    assert calls[5].name == "parhelion_status" and calls[5].is_platform
    assert [t.index for t in loader.after_turns(log, cut)] == [4]


def test_four_probe_types_with_sources(tmp_path: Path) -> None:
    log = loader.load_agent(build_log(tmp_path / "mesh-dee-aaaaaa.jsonl"))
    plist = probes.generate(log, log.cuts[0])
    by_type = {p.type: p for p in plist}
    assert set(by_type) == {"idea_repeat", "refuted_retry", "file_reread", "retest"}
    assert by_type["file_reread"].keys == ["kernel.cpp"]
    assert by_type["retest"].keys == ["abc1234"] and by_type["retest"].where["level"] == "standard"
    assert by_type["refuted_retry"].keys == ["abc1234"] and by_type["refuted_retry"].before["why"] in ("slower", "no_gain")
    assert len(by_type["idea_repeat"].keys) == 3
    for p in plist:
        assert p.where.get("seq") and p.before.get("seq")
    assert [p.as_dict() for p in plist] == [p.as_dict() for p in probes.generate(log, log.cuts[0])]


def test_seed_schemes_and_registration(tmp_path: Path) -> None:
    log = loader.load_agent(build_log(tmp_path / "mesh-dee-aaaaaa.jsonl"))
    ctx = seeds.CutContext(log, log.cuts[0])
    assert seeds.get("current")(ctx) == log.cuts[0].seed
    ledger = seeds.get("current_plus_ledger")(ctx)
    assert ledger.startswith(log.cuts[0].seed) and "gate abc1234: HOLD_LOW_GAIN" in ledger
    assert "test abc1234 standard: 15/15" in ledger and "commit abc1234" in ledger
    assert "kernel.cpp" in seeds.get("full_history")(ctx)
    rebuilt = seeds.build_current(seeds.CutContext(log, loader.Cut(0, 2, None, "", HANDOFF, "")))
    assert rebuilt.startswith(loader.SEED_PREFIX) and HANDOFF in rebuilt and seeds.CHECKPOINT_TAIL in rebuilt
    seeds.register("full_c", lambda c: "custom seed")
    try:
        assert "full_c" in seeds.names() and seeds.get("full_c")(ctx) == "custom seed"
    finally:
        seeds._REGISTRY.pop("full_c", None)


def test_scoring_is_deterministic_and_context_aware(tmp_path: Path) -> None:
    path = build_log(tmp_path / "mesh-dee-aaaaaa.jsonl")
    a = bench.run([path])
    b = bench.run([path])
    assert a == b
    cur, full = a["schemes"]["current"], a["schemes"]["full_history"]
    assert full["overall"]["coverage"] == 1.0
    assert cur["by_type"]["retest"]["coverage"] == 1.0        # "abc1234 standard 15/15" on one line
    assert cur["by_type"]["refuted_retry"]["coverage"] == 1.0  # "abc1234 ... slower" on one line
    assert cur["by_type"]["file_reread"]["coverage"] == 0.0    # the handoff never names kernel.cpp
    lo, hi = cur["by_worker"]["dee-aaaaaa"]["ci95"]
    assert 0.0 <= lo <= cur["overall"]["coverage"] <= hi <= 1.0
    assert score.bootstrap_ci([1, 0, 1, 1, 0]) == score.bootstrap_ci([1, 0, 1, 1, 0])
    sha_only = probes.Probe("p", "retest", ["abc1234"], 1, "a", 0, {}, {}, probes.RESULT_CONTEXT)
    assert not score.covered(sha_only, "we touched abc1234 earlier")
    assert score.covered(sha_only, "abc1234 final 16/16 pass")
    try:
        bench.run([path], judge="llm")
    except ValueError as exc:
        assert "disabled" in str(exc)
    else:
        raise AssertionError("LLM judge must stay off")


def test_sprint_metrics_definitions(tmp_path: Path) -> None:
    res = sprint_metrics.compute([build_log(tmp_path / "mesh-dee-aaaaaa.jsonl")])
    assert res["window"]["workers"] == 1
    ctx = res["context"]
    assert ctx["peak_max"] == 40_000 and ctx["compactions"] == 1
    assert ctx["per_agent"]["dee-aaaaaa"]["startup"] == 10_000
    cd = res["cd_shell"]
    assert cd["count"] == 4 and cd["by_program"]["sed"]["count"] == 1 and cd["by_program"]["git"]["count"] == 3
    assert res["inline_python"] == {"count": 1, "reads_json": 1, "parses_report": 1,
                                    "per_hour": res["inline_python"]["per_hour"]}
    assert res["cli_bypass"]["opsb queue list"] == 1
    rep = res["repeats"]
    assert (rep["reads"], rep["reads_after_cut"], rep["rereads"]) == (3, 1, 1)
    assert (rep["tests"], rep["tests_after_cut"], rep["retests"]) == (2, 1, 1)
    assert res["tools"]["bash"]["calls"] == 6
    polls = res["worker_polls"]
    assert (polls["test_wait"], polls["status"], polls["wait"], polls["poll_total"]) == (1, 1, 0, 2)
    # A bound without an offset is UTC, like the log rows (not the machine's local time).
    early = sprint_metrics.compute([tmp_path / "mesh-dee-aaaaaa.jsonl"], until="2026-01-02T03:01:00")
    assert 0 < early["window"]["hours"] < 0.02 and early["context"]["compactions"] == 0


def test_replay_cli_reads_logs_only(tmp_path: Path) -> None:
    from opsb_core.cli_app import main
    path = build_log(tmp_path / "logs" / "mesh-dee-aaaaaa.jsonl")
    before = path.read_bytes()
    ws = tmp_path / "ws"
    ws.mkdir()
    env = {"PARHELION_HOME": str(tmp_path / "home"), "DSH_HOME": str(tmp_path / "dsh")}
    with mock.patch.dict(os.environ, env):
        for argv in (["replay", "bench", "--sessions", str(path), "--json"],
                     ["replay", "metrics", "--sessions", str(tmp_path / "logs" / "mesh-*.jsonl"),
                      "--workspace", str(ws), "--json"]):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                assert main(argv) == 0
            doc = json.loads(out.getvalue())
            assert doc
        assert set(doc) >= {"window", "context", "cd_shell", "repeats", "cache", "cost", "orchestrator"}
        assert doc["cache"]["dee-aaaaaa"]["status"] == "unavailable"
    assert path.read_bytes() == before
