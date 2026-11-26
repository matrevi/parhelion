"""4.4 P30 replay: ``opsb replay stuck`` finds the stuck episode of a synthetic sprint, read only, and
counts tool calls inside it from a mesh log (aggregates only).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core.replay import stuck_replay  # noqa: E402


def _row(i: int, minute: int, **fields) -> dict:
    return {"id": f"L-{i:04d}", "ts": f"2026-11-26T21:{minute:02d}:00+00:00", "worker": "dee-aaaaaa",
            "problem": "Op", "source": "platform", "confidence": "measured", **fields}


def _mesh(path: Path, calls: list[tuple[int, str]]) -> Path:
    lines = [{"ts": "2026-11-26T21:00:00Z", "msg": {"method": "session/prompt",
                                                     "params": {"sessionId": "s", "prompt": [{"type": "text", "text": "go"}]}}}]
    for i, (minute, title) in enumerate(calls):
        lines.append({"ts": f"2026-11-26T21:{minute:02d}:30Z", "msg": {"method": "session/update", "params": {
            "update": {"sessionUpdate": "tool_call", "toolCallId": f"c{i}", "title": title, "rawInput": {}}}}})
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return path


def test_replay_finds_the_wa_loop_and_counts_calls_inside_it(tmp_path, monkeypatch):
    monkeypatch.setenv("PARHELION_HOME", str(tmp_path / "ph"))
    ws = tmp_path / "contest"
    led = ws / "results" / "ledger" / "Op"
    led.mkdir(parents=True)
    (ws / "opsb.workspace.json").write_text(json.dumps({"problems": [{"title": "Op", "path": "Op"}]}))
    rows = [_row(1, 0, type="experiment", outcome="better", base_sha="b" * 40, per_case_us={"05": 9.0},
                 actual_outcome={"level": "standard", "speedup": 1.1})]
    rows += [_row(i, 5 * i, type="experiment", outcome="WA", scope={"cases": ["05"]}, why="case 05 WA",
                  actual_outcome={"level": "standard"}) for i in range(2, 6)]
    (led / "dee-aaaaaa.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    before = sorted(p.name for p in ws.rglob("*"))
    logs = tmp_path / "logs"
    logs.mkdir()
    _mesh(logs / "mesh-dee-aaaaaa.jsonl", [(21, "parhelion_test_wait"), (22, "parhelion_kb_query"), (23, "Bash")])
    doc = stuck_replay.run(ws, since="2026-11-26T21:00Z", until="2026-11-26T21:40Z", step=5, sessions=logs)
    seat = doc["problems"]["Op"]["dee-aaaaaa"]
    episodes = seat["episodes"]
    assert [e["class"] for e in episodes] == ["wa_loop"] and episodes[0]["from"].startswith("2026-11-26T21:20")
    assert seat["new_bests"] == ["21:00"] and seat["outcomes"] == {"better": 1, "WA": 4}
    calls = episodes[0]["calls"]
    assert calls == {"total": 3, "kb_query": 1, "test_wait": 1, "first_test_wait_min": 1.5}
    assert doc["sessions"]["kb_query"] == 1 and "wa_loop" in stuck_replay.render(doc)
    assert sorted(p.name for p in ws.rglob("*")) == before  # read only
