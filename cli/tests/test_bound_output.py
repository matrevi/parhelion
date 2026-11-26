"""Tests of the one MCP output bound (P13): cap, paging, spill, read-back, formats and retention."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import time
from pathlib import Path

import pytest

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))
SUP_ROOT = CLI_DIR.parent
if str(SUP_ROOT) not in sys.path:
    sys.path.insert(0, str(SUP_ROOT))

from opsb_core.commands.doctor_cmd import register_doctor_subcommands, run_doctor  # noqa: E402
from opsb_core.ledger.store import Ledger  # noqa: E402
from opsb_core.mcp import bound, handlers_agents, handlers_kb, server  # noqa: E402
from opsb_core.mcp.handlers_bench import tool_parhelion_status  # noqa: E402
from opsb_core.mcp.spill_read import tool_parhelion_spill_read  # noqa: E402
from opsb_core.mcp.tools import call_tool  # noqa: E402
from opsb_core.services import test_runs  # noqa: E402


def _compact(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _call(ws: Path, name: str, arguments: dict, *, role: str = "orchestrator",
          agent_id: str | None = None) -> dict:
    """One ``tools/call`` through the server: parse the bounded text back into an object."""
    response = server.handle_jsonrpc_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
        ws, role=role, agent_id=agent_id)
    return response["result"]


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


@pytest.fixture()
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / "results").mkdir(parents=True)
    return root


def _spill_files(ws: Path) -> list[Path]:
    root = bound.spill_root(ws)
    return sorted(p for p in root.rglob("*.json")) if root.is_dir() else []


# ---------------------------------------------------------------- small results and the cap

def test_small_result_is_returned_byte_for_byte(ws: Path) -> None:
    payload = {"ok": True, "rows": [["a", 1], ["b", 2]], "note": "é"}
    text, meta = bound.bound_output("parhelion_status", payload, ws)
    assert text == _compact(payload)
    assert meta == {"spilled": False, "bound_bytes": len(text.encode("utf-8"))}
    assert _spill_files(ws) == []


def test_server_returns_a_small_result_through_the_bound(ws: Path, monkeypatch) -> None:
    monkeypatch.setattr(server, "call_tool", lambda *args, **kwargs: {"ok": True, "value": 7})
    result = _call(ws, "parhelion_status", {})
    assert result["isError"] is False
    assert result["content"][0]["text"] == '{"ok":true,"value":7}'
    assert _spill_files(ws) == []


def test_oversize_result_spills_path_head_tail_and_total(ws: Path) -> None:
    full = {"view": "per_case", "sha": "abc1234", "columns": ["case", "us"], "short": [1, 2],
            "cases": [[f"c{i:03d}", i * 3] for i in range(300)], "note": "end-of-payload"}
    text, meta = bound.bound_output("parhelion_report", full, ws, agent_id="worker-1", cap=1200)
    envelope = json.loads(text)
    assert envelope["spilled"] is True and meta["spilled"] is True
    assert meta["bound_bytes"] == len(_compact(full).encode("utf-8"))
    path = Path(envelope["path"])
    assert path.parent == bound.spill_root(ws) / "worker-1"
    assert path.name.endswith("-parhelion_report.json")
    assert path.read_text(encoding="utf-8") == _compact(full)
    assert envelope["total_bytes"] == path.stat().st_size == meta["bound_bytes"]
    head = envelope["head"]
    assert head["view"] == "per_case" and head["sha"] == "abc1234" and head["short"] == [1, 2]
    assert head["columns"] == ["case", "us"] and head["note"] == "end-of-payload"
    assert head["cases"] and len(head["cases"]) < 300
    assert len(_compact(head).encode("utf-8")) <= 1200
    assert envelope["tail"].endswith('"note":"end-of-payload"}')
    assert 0 < len(envelope["tail"].encode("utf-8")) <= bound.TAIL_BYTES + 8


def test_failure_spill_keeps_the_tail_with_the_cause(ws: Path) -> None:
    payload = {"ok": False, "tool": "parhelion_report", "error": "trace " * 1200 + "FINAL-CAUSE"}
    envelope = json.loads(bound.bound_output("parhelion_report", payload, ws, agent_id="w", cap=900)[0])
    assert envelope["spilled"] is True and envelope["head"]["ok"] is False
    assert "FINAL-CAUSE" in envelope["tail"]


def test_server_error_reply_is_spilled_with_its_tail(ws: Path, monkeypatch) -> None:
    monkeypatch.setattr(bound, "output_cap", lambda: 600)

    def boom(*args, **kwargs):
        raise RuntimeError("start " + "x" * 4000 + " boom-tail")

    monkeypatch.setattr(server, "call_tool", boom)
    result = _call(ws, "parhelion_status", {}, role="worker", agent_id="w-1")
    assert result["isError"] is True
    envelope = _payload(result)
    assert envelope["spilled"] is True and Path(envelope["path"]).parent.name == "w-1"
    assert "boom-tail" in envelope["tail"]


def test_a_failed_spill_still_returns_a_bounded_reply(ws: Path, monkeypatch) -> None:
    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(bound, "write_spill", broken)
    text, meta = bound.bound_output("parhelion_report", {"cases": ["x" * 4000]}, ws, cap=500)
    envelope = json.loads(text)
    assert envelope["spilled"] is False and envelope["truncated"] is True
    assert "disk full" in envelope["spill_error"] and envelope["tail"]
    assert meta == {"spilled": False, "bound_bytes": len(_compact({"cases": ["x" * 4000]}).encode())}


def test_spill_read_window_is_never_spilled_again(ws: Path) -> None:
    payload = {"rows": [[str(i), "z" * 60] for i in range(200)]}
    envelope = json.loads(bound.bound_output("parhelion_report", payload, ws, agent_id="w", cap=800)[0])
    before = _spill_files(ws)
    window = tool_parhelion_spill_read({"path": envelope["path"], "limit": 900}, ws)
    text, meta = bound.bound_output("parhelion_spill_read", window, ws, cap=300)
    assert text == _compact(window) and meta["spilled"] is False
    assert _spill_files(ws) == before


# ---------------------------------------------------------------- paging before spilling

def _seed_insights(ws: Path, count: int = 35) -> list[str]:
    ledger = Ledger(ws, "Op", "platform")
    return [ledger.append("insight", {"claim": f"insight {i:02d} " + "y" * 40}, source="platform")["id"]
            for i in range(count)]


def test_ledger_pages_before_it_spills(ws: Path, monkeypatch) -> None:
    ids = _seed_insights(ws, 35)
    monkeypatch.setattr(bound, "output_cap", lambda: 700)
    first = _payload(_call(ws, "parhelion_ledger", {"problem": "Op", "view": "unexploited"}))
    assert "spilled" not in first and first["page"] == {"offset": 0, "limit": len(first["lines"]), "total": 35}
    assert 0 < len(first["lines"]) < 35 and first["next_cursor"]
    seen: list[str] = list(first["lines"])
    cursor = first["next_cursor"]
    for _ in range(40):
        if not cursor:
            break
        page = _payload(_call(ws, "parhelion_ledger",
                              {"problem": "Op", "view": "unexploited", "cursor": cursor}))
        seen.extend(page["lines"])
        cursor = page.get("next_cursor")
    assert len(seen) == len(set(seen)) == 35
    assert all(any(line.startswith(entry_id) for line in seen) for entry_id in ids)
    assert _spill_files(ws) == []


def test_ledger_offset_and_cursor_agree(ws: Path) -> None:
    _seed_insights(ws, 25)
    page = call_tool("parhelion_ledger", {"problem": "Op", "view": "unexploited", "offset": 10, "limit": 5}, ws)
    assert page["page"]["offset"] == 10 and page["page"]["limit"] == 5 and page["shown"] == 5
    assert page["next_cursor"] == "o15"
    again = call_tool("parhelion_ledger",
                      {"problem": "Op", "view": "unexploited", "offset": 99, "limit": 5, "cursor": "o15"}, ws)
    assert again["lines"] == page["lines"] or again["page"]["offset"] == 15
    with pytest.raises(ValueError, match="cursor"):
        call_tool("parhelion_ledger", {"problem": "Op", "view": "unexploited", "cursor": "nope"}, ws)


def test_agent_messages_pages_with_a_cursor(ws: Path, monkeypatch) -> None:
    rows = [{"seq": i, "body": f"m{i}"} for i in range(1, 122)]
    acked: list[int] = []

    class Stub:
        def read_messages(self, workspace, *, agent_id=None, problem=None, since_seq=0, limit=60,
                          ack=True, include_squad=False):
            if ack and rows[:limit]:  # the real read acknowledges every row it returns
                acked.append(max(row["seq"] for row in rows[:limit]))
            return {"ok": True, "agent": None, "problem": problem or "", "diff_stat": "",
                    "latest_seq": 121, "cursor_reset": False, "messages": rows[:limit]}

    monkeypatch.setattr(handlers_agents, "_ACTIVE_DAEMON_MANAGER", None)
    monkeypatch.setattr(handlers_agents, "_get_local_manager", lambda: Stub())
    monkeypatch.setattr(handlers_agents, "_proxy_daemon", lambda *args, **kwargs: None)

    def walk(total_rows: int, limit: int) -> tuple[list[int], int]:
        del rows[total_rows:]
        seen: list[int] = []
        cursor, calls = None, 0
        while calls < 20:
            arguments: dict = {"limit": limit}
            if cursor:
                arguments["cursor"] = cursor
            out = call_tool("parhelion_agent_messages", arguments, ws)
            calls += 1
            seen.extend(row["seq"] for row in out["messages"])
            cursor = out.get("next_cursor")
            if not cursor:
                return seen, calls
        raise AssertionError("next_cursor never ended")

    seen, calls = walk(121, 50)
    assert seen == list(range(1, 122)) and calls == 3
    # The first page acknowledges exactly the rows it shows, never a look-ahead row.
    assert acked == [50]
    rows[:] = [{"seq": i, "body": f"m{i}"} for i in range(1, 101)]
    seen, calls = walk(100, 50)  # an exact multiple ends on one empty page
    assert seen == list(range(1, 101)) and calls == 3


def test_kb_query_formats_and_cursor_pages(ws: Path, monkeypatch) -> None:
    entries = [{"id": f"HW-{i:02d}", "pillar": "hardware", "title": f"t{i}", "confidence": 0.9,
                "summary": "s" * 900} for i in range(10)]

    def fake_full(args, workspace):
        limit = int(args.get("limit") or 3)
        rows = entries[:limit]
        return {"results": list(rows), "entries": list(rows), "anti_patterns": [], "result_count": len(rows),
                "total_matched": len(rows), "source": "fixture"}

    monkeypatch.setattr(handlers_kb, "_kb_query_full", fake_full)
    brief = call_tool("parhelion_kb_query", {}, ws)
    assert [len(entry["summary"]) for entry in brief["results"]] == [600, 600, 600]
    assert "entries" not in brief  # 4.2 J-1: the mirrored list no longer doubles the reply
    full = call_tool("parhelion_kb_query", {"format": "full"}, ws)
    assert len(full["results"]) == handlers_kb.KB_FULL_DEFAULT_LIMIT
    assert full["results"][0]["summary"] == "s" * 900
    assert call_tool("parhelion_kb_query", {"full": True}, ws)["results"] == full["results"]
    seen: list[str] = []
    cursor, calls = None, 0
    for _ in range(10):
        arguments: dict = {"format": "full", "limit": 4}
        if cursor:
            arguments["cursor"] = cursor
        out = call_tool("parhelion_kb_query", arguments, ws)
        calls += 1
        seen.extend(entry["id"] for entry in out["results"])
        assert "entries" not in out
        cursor = out.get("next_cursor")
        if not cursor:
            break
    assert seen == [f"HW-{i:02d}" for i in range(10)]
    assert cursor is None and calls == 3  # 4 + 4 + 2, and the walk ends
    with pytest.raises(ValueError, match="format must be brief or full"):
        call_tool("parhelion_kb_query", {"format": "verbose"}, ws)


def test_report_queue_and_history_page_with_a_cursor(ws: Path, monkeypatch) -> None:
    queue = [{"id": f"cand-{i:02d}", "kind": "candidate", "title": "Op", "sha": f"{i:040x}", "badge": "READY",
              "raw_status": "ready", "gates": {}, "note": "", "detail": "", "next_step": "",
              "account_id": "acct-secret"} for i in range(45)]
    snap = {"sampled_at": "2026-11-26T00:00:00+00:00", "contest": {"workspace": str(ws)},
            "scheduler": {"breakthrough_threshold": 5.0, "pipeline": {"active_submissions": []}},
            "problems": [], "submission_queue": queue}
    monkeypatch.setattr("opsb_core.services.queue_service.compute_workspace_snapshot",
                        lambda workspace, cfg: snap)
    monkeypatch.setattr("opsb_core.ipc_bridge.invoke_bridge",
                        lambda *args, **kwargs: {"submit_queue": [], "candidate_archive": {},
                                                 "unsubmitted_candidates": []})
    seen: list[str] = []
    cursor = None
    for _ in range(10):
        arguments: dict = {"view": "queue", "problem": "Op", "limit": 20}
        if cursor:
            arguments["cursor"] = cursor
        out = call_tool("parhelion_report", arguments, ws)
        assert "acct-secret" not in _compact(out)
        assert out["page"]["total"] == 45
        seen.extend(item["id"] for item in out["items"])
        cursor = out.get("next_cursor")
        if not cursor:
            break
    assert seen == [f"cand-{i:02d}" for i in range(45)]

    receipts = ws / "results" / "submit-queue-outcome.jsonl"
    receipts.write_text("".join(json.dumps({
        "title": "Op", "submission_id": f"sub-{i:02d}", "sha": f"{i:040x}", "status": "Pass", "score": 10.0 + i,
        "submitted_at": f"2026-11-20T{i:02d}:00:00Z"}) + "\n" for i in range(25)), encoding="utf-8")
    seen = []
    cursor = None
    for _ in range(10):
        arguments = {"view": "history", "problem": "Op", "limit": 10}
        if cursor:
            arguments["cursor"] = cursor
        out = call_tool("parhelion_report", arguments, ws)
        seen.extend(receipt["sid"] for receipt in out["receipts"])
        cursor = out.get("next_cursor")
        if not cursor:
            break
    assert seen == [f"sub-{i:02d}" for i in reversed(range(25))]


# ---------------------------------------------------------------- read-back permissions

def test_spill_read_is_scoped_to_the_workspace_and_the_agent(ws: Path) -> None:
    payload = {"rows": [[str(i), "z" * 40] for i in range(120)]}
    envelope = json.loads(bound.bound_output("parhelion_report", payload, ws, agent_id="worker-1", cap=700)[0])
    path = envelope["path"]
    own = tool_parhelion_spill_read({"_worker_request": True, "_caller_agent_id": "worker-1", "path": path}, ws)
    assert own["total_bytes"] == envelope["total_bytes"] and own["offset"] == 0
    chunks, offset = [], 0
    for _ in range(20):
        window = tool_parhelion_spill_read({"path": path, "offset": offset, "limit": 700}, ws)
        chunks.append(window["text"])
        if window["next_offset"] is None:
            break
        offset = window["next_offset"]
    assert "".join(chunks) == Path(path).read_text(encoding="utf-8")
    with pytest.raises(PermissionError, match="only its own"):
        tool_parhelion_spill_read({"_worker_request": True, "_caller_agent_id": "worker-2", "path": path}, ws)
    relative = str(Path(path).relative_to(bound.spill_root(ws)))
    with pytest.raises(PermissionError):
        tool_parhelion_spill_read({"_worker_request": True, "_caller_agent_id": "worker-2", "path": relative}, ws)
    outside = ws / "results" / "report.json"
    outside.write_text("{}", encoding="utf-8")
    with pytest.raises(PermissionError, match="spill directory"):
        tool_parhelion_spill_read({"path": str(outside)}, ws)
    with pytest.raises(ValueError, match="no spilled result"):
        tool_parhelion_spill_read({"path": str(bound.spill_root(ws) / "missing.json")}, ws)
    with pytest.raises(PermissionError, match="identity"):
        tool_parhelion_spill_read({"_worker_request": True, "path": path}, ws)


# ---------------------------------------------------------------- brief and full formats

def test_status_brief_and_full_keep_the_rev_semantics(ws: Path) -> None:
    (ws / "bench" / "Op").mkdir(parents=True)
    (ws / "bench" / "problems.json").write_text(
        json.dumps([{"title": "Op", "path": "bench/Op", "difficulty": 2}]), encoding="utf-8")
    (ws / "results" / "score-snapshot.json").write_text(
        json.dumps({"Op": {"score": 10.0, "status": "Pass", "rank": 3}}), encoding="utf-8")
    (ws / "results" / "submit-queue.jsonl").write_text("", encoding="utf-8")
    (ws / "results" / "submit-queue-outcome.jsonl").write_text("", encoding="utf-8")
    brief = call_tool("parhelion_status", {}, ws)
    full = call_tool("parhelion_status", {"format": "full"}, ws)
    verbose = call_tool("parhelion_status", {"verbose": True}, ws)
    assert isinstance(brief["problems"], dict) and brief["problems"]["columns"][0] == "title"
    assert isinstance(full["problems"], list) and full["problems"][0]["title"] == "Op"
    assert full["rev"] != brief["rev"]
    assert {k: v for k, v in full.items() if k not in ("rev", "sampled_at")} == \
           {k: v for k, v in verbose.items() if k not in ("rev", "sampled_at")}
    same = call_tool("parhelion_status", {"format": "brief"}, ws)
    assert same["rev"] == call_tool("parhelion_status", {"format": "brief"}, ws)["rev"]
    assert {k: v for k, v in same.items() if k not in ("rev", "sampled_at")} == \
           {k: v for k, v in brief.items() if k not in ("rev", "sampled_at")}
    assert call_tool("parhelion_status", {"if_rev": brief["rev"]}, ws) == {"not_modified": True, "rev": brief["rev"]}
    assert "not_modified" not in call_tool("parhelion_status", {"format": "full", "if_rev": brief["rev"]}, ws)
    with pytest.raises(ValueError, match="format must be brief or full"):
        tool_parhelion_status({"format": "verbose"}, ws)


def test_test_wait_brief_and_full(ws: Path, monkeypatch) -> None:
    record = {"request_id": "r" * 32, "problem": "Op", "sha": "a" * 40, "level": "standard",
              "status": "finished", "exit_code": 0, "error": "E" * 2000,
              "digest": {"summary": {"passed": 3, "total": 3},
                         "timing": {"cases": {f"c{i:02d}": {"min_us": i} for i in range(30)}}}}
    brief = test_runs.public_view(record)
    full = test_runs.public_view(record, full=True)
    assert len(full["digest"]["timing"]["cases"]) == 30
    assert brief["error"] == "E" * test_runs.ERROR_LIMIT and full["error"] == "E" * 2000
    assert len(_compact(brief).encode("utf-8")) <= test_runs.VIEW_LIMIT
    seen: list[bool] = []

    def fake_wait(workspace, request_id, wait_sec=30, block=None, full=False):
        seen.append(full)
        return {"request_id": request_id, "status": "finished", "full": full}

    monkeypatch.setattr(test_runs, "wait", fake_wait)
    monkeypatch.setattr(handlers_agents, "_proxy_daemon", lambda *args, **kwargs: None)
    assert call_tool("parhelion_test_wait", {"request_id": "r" * 32}, ws)["full"] is False
    assert call_tool("parhelion_test_wait", {"request_id": "r" * 32, "format": "full"}, ws)["full"] is True
    assert seen == [False, True]
    with pytest.raises(ValueError, match="format"):
        call_tool("parhelion_test_wait", {"request_id": "r" * 32, "format": "long"}, ws)


def test_test_result_message_brief_and_full(ws: Path) -> None:
    record = {"request_id": "r" * 32, "problem": "Op", "sha": "a" * 40, "level": "standard",
              "status": "finished", "exit_code": 0, "agent_id": "worker-1", "result_format": "brief",
              "digest": {"summary": {"passed": 3, "total": 3}, "timing": {"cases": {"01": {"min_us": 5}}}}}
    assert test_runs.result_message(record, ws) == test_runs.compact_line(record)
    record["result_format"] = "full"
    full = test_runs.result_message(record, ws)
    line, _, body = full.partition("\n")
    assert line == test_runs.compact_line(record) and json.loads(body)["digest"]["timing"]["cases"]
    record["digest"] = {"timing": {"cases": {f"c{i:03d}": {"min_us": i} for i in range(400)}}}
    big = test_runs.result_message(record, ws)
    envelope = json.loads(big.partition("\n")[2])
    assert envelope["spilled"] is True and Path(envelope["path"]).parent.name == "worker-1"
    assert _spill_files(ws) == [Path(envelope["path"])]


# ---------------------------------------------------------------- metering and retention

def test_meter_records_spilled_and_bound_bytes(ws: Path, monkeypatch) -> None:
    from opsb_core.metrics import mcp_meter

    monkeypatch.setattr(bound, "output_cap", lambda: 400)
    monkeypatch.setattr(server, "call_tool", lambda *args, **kwargs: {"rows": ["x" * 5000]})
    _call(ws, "parhelion_report", {}, role="worker", agent_id="worker-1")
    rows = mcp_meter.read_rows(ws)
    assert len(rows) == 1
    row = rows[0]
    assert row["spilled"] is True and row["tool"] == "parhelion_report"
    assert row["bound_bytes"] > row["result_bytes"] > 0
    assert json.loads(server.handle_jsonrpc_message(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "parhelion_report", "arguments": {}}},
        ws, role="worker", agent_id="worker-1")["result"]["content"][0]["text"])["spilled"] is True


def test_reap_drops_old_files_and_the_size_budget(ws: Path) -> None:
    directory = bound.agent_spill_dir(ws, "worker-1")
    directory.mkdir(parents=True)
    old = directory / "old.json"
    old.write_text("{}", encoding="utf-8")
    os.utime(old, (time.time() - 10 * 86400,) * 2)
    recent = [directory / f"recent-{i}.json" for i in range(3)]
    for path in recent:
        path.write_text("x" * 900, encoding="utf-8")
    assert sorted(bound.reap(ws, keep_days=7, max_bytes=1000)) == sorted(
        str(path) for path in [old, recent[0], recent[1]])
    assert recent[2].is_file() and not old.is_file()


def _parse_doctor(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="opsb")
    register_doctor_subcommands(parser.add_subparsers(dest="command"))
    return parser.parse_args(argv)


def _run_doctor(argv: list[str]) -> tuple[int, dict]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = run_doctor(_parse_doctor(argv))
    return code, json.loads(buffer.getvalue())


def test_doctor_lists_spill_and_peer_retention_then_purges_on_request(tmp_path: Path, monkeypatch) -> None:
    from opsb_core import config as _cfg

    monkeypatch.setenv("PARHELION_HOME", str(tmp_path))
    monkeypatch.setenv("OPSB_HOME", str(tmp_path))
    contest = tmp_path / "contest"
    contest.mkdir()
    (tmp_path / "config.json").write_text(
        json.dumps({"workspaces": [str(contest)], "active_workspace": str(contest)}), encoding="utf-8")
    data_dir = tmp_path / "workspaces" / _cfg.workspace_id(contest)
    data_dir.mkdir(parents=True)
    (data_dir / "meta.json").write_text(json.dumps({"workspace": str(contest)}), encoding="utf-8")
    state = data_dir / "state"
    spill = state / "spill" / "worker-1"
    peer = state / "peer" / "quotes"
    spill.mkdir(parents=True)
    peer.mkdir(parents=True)
    old_spill = spill / "old.json"
    old_spill.write_text("{}", encoding="utf-8")
    old_peer = peer / "worker-1.jsonl"
    old_peer.write_text("{}\n", encoding="utf-8")
    for path in (old_spill, old_peer):
        os.utime(path, (time.time() - 15 * 86400,) * 2)
    fresh = state / "peer" / "fresh.md"
    fresh.write_text("keep\n", encoding="utf-8")
    code, report = _run_doctor(["doctor", "clean", "--json"])
    assert code == 0
    hits = {Path(hit["path"]): hit for hit in report["retention"]}
    assert set(hits) == {old_spill, old_peer}
    assert all(hit["applied"] is False and hit["reason"].startswith("older than") for hit in hits.values())
    assert old_spill.is_file() and old_peer.is_file() and fresh.is_file()
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        assert run_doctor(_parse_doctor(["doctor", "clean"])) == 0
    assert "Spill and peer retention (2," in buffer.getvalue() and "would delete" in buffer.getvalue()
    code, report = _run_doctor(["doctor", "clean", "--apply", "--purge-retention", "--json"])
    assert code == 0 and all(hit["applied"] for hit in report["retention"])
    assert not old_spill.exists() and not old_peer.exists() and fresh.is_file()


def test_retention_settings_come_from_the_mcp_section(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    (home / "workspaces" / "contest-0000" / "state" / "spill").mkdir(parents=True)
    (home / "config.json").write_text(json.dumps({"mcp": {"output_cap_bytes": 1234, "spill_keep_days": 3,
                                                          "spill_max_mb": 7}}), encoding="utf-8")
    monkeypatch.setenv("PARHELION_HOME", str(home))
    monkeypatch.setenv("OPSB_HOME", str(home))
    assert bound.output_cap() == 1234
    assert bound.spill_keep_days() == 3 and bound.spill_max_bytes() == 7 * 1024 * 1024


def test_a_failing_bound_still_returns_the_tool_result(ws: Path, monkeypatch) -> None:
    def broken(*args, **kwargs):
        raise RuntimeError("bound exploded")

    monkeypatch.setattr(server, "bound_output", broken)
    result = _call(ws, "parhelion_report", {"view": "env"})
    assert result["isError"] is False and _payload(result)["view"] == "env"
