"""Rebuild turns -> tool calls -> results from an ACP mesh log and cut it at compaction points.

A mesh log (``~/.parhelion/logs/sessions/mesh-<id>.jsonl``, rows ``{ts, dir, msg}``) is read
once, read-only. Every ``session/prompt`` opens a turn; the following ``session/update``
notifications fill it (message and thought chunks, tool calls and their results, context
usage). A compaction is the platform's handoff turn (prompt starting ``HANDOFF_PREFIX``,
``agents/adapters.py``) followed by the seed prompt (``SEED_PREFIX``) in a new session.
"""
from __future__ import annotations

import datetime
import fnmatch
import glob
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HANDOFF_PREFIX = "Write a short handoff"
SEED_PREFIX = "Preserve this checkpoint"
PLATFORM_TOOL_PREFIX = "parhelion_"
PEER_RE = re.compile(r"^\[[\w.-]+ → [\w.-]+\]")


@dataclass
class ToolCall:
    seq: int
    ts: str
    call_id: str
    name: str
    args: dict[str, Any]
    status: str = ""
    result: str = ""

    @property
    def result_bytes(self) -> int:
        return len(self.result.encode("utf-8", "replace"))

    @property
    def is_platform(self) -> bool:
        return self.name.startswith(PLATFORM_TOOL_PREFIX)


@dataclass
class Turn:
    index: int
    seq: int
    ts: str
    session_id: str
    prompt: str
    kind: str
    reply: str = ""
    thought: str = ""
    tools: list[ToolCall] = field(default_factory=list)
    end_ts: str = ""


@dataclass
class Cut:
    """A compaction point: turns ``[0, handoff_turn)`` precede it, ``(seed_turn, next cut)`` follow."""
    index: int
    handoff_turn: int
    seed_turn: int | None
    ts: str
    handoff: str
    seed: str
    after_end: int = 0


@dataclass
class AgentLog:
    agent_id: str
    path: str
    turns: list[Turn]
    usage: list[tuple[int, str, int, int]]
    sessions: list[tuple[str, str, str]]
    cuts: list[Cut]

    def tool_calls(self, start: int = 0, end: int | None = None) -> list[ToolCall]:
        return [c for t in self.turns[start:end] for c in t.tools]

    @property
    def first_ts(self) -> str:
        return self.turns[0].ts if self.turns else ""

    @property
    def last_ts(self) -> str:
        return (self.turns[-1].end_ts or self.turns[-1].ts) if self.turns else ""


def normalize_tool(title: Any) -> str:
    name = str(title or "").strip().split(" ")[0]
    for prefix in ("mcp__parhelion__", "mcp_parhelion_", "parhelion__"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def classify_prompt(text: str, index: int) -> str:
    head = text.lstrip()
    if head.startswith(HANDOFF_PREFIX):
        return "handoff"
    if head.startswith(SEED_PREFIX):
        return "seed"
    if PEER_RE.match(head):
        return "peer"
    if head.startswith("[Admin Directive]"):
        return "directive"
    return "brief" if index == 0 else "followup"


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if "text" in content:
            return str(content.get("text") or "")
        return _text_of(content.get("content"))
    if isinstance(content, list):
        return "".join(_text_of(c) for c in content)
    return ""


def _in_window(ts: str, since: str | None, until: str | None) -> bool:
    return not ((since and ts and ts < since) or (until and ts and ts > until))


def load_agent(path: Path | str, *, since: str | None = None, until: str | None = None) -> AgentLog:
    """Parse one mesh log; ``since``/``until`` (ISO UTC prefixes) drop rows outside the window."""
    path = Path(path)
    agent_id = path.stem.removeprefix("mesh-")
    turns: list[Turn] = []
    usage: list[tuple[int, str, int, int]] = []
    sessions: list[tuple[str, str, str]] = []
    pending_new: dict[Any, str] = {}
    calls: dict[str, ToolCall] = {}
    seq = 0
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            msg = row.get("msg") if isinstance(row, dict) else None
            if not isinstance(msg, dict):
                continue
            ts = str(row.get("ts") or "")
            if not _in_window(ts, since, until):
                continue
            seq += 1
            method = msg.get("method")
            params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
            if method == "session/new":
                pending_new[msg.get("id")] = str(params.get("cwd") or "")
            elif "result" in msg and msg.get("id") in pending_new:
                res = msg.get("result") if isinstance(msg.get("result"), dict) else {}
                cwd = pending_new.pop(msg.get("id"))
                if res.get("sessionId"):
                    sessions.append((str(res["sessionId"]), cwd, ts))
            elif method == "session/prompt":
                text = _text_of(params.get("prompt"))
                turns.append(Turn(len(turns), seq, ts, str(params.get("sessionId") or ""), text,
                                  classify_prompt(text, len(turns))))
            elif "result" in msg and isinstance(msg.get("result"), dict) and "stopReason" in msg["result"]:
                if turns:
                    turns[-1].end_ts = ts
            elif method == "session/update" and turns:
                upd = params.get("update") if isinstance(params.get("update"), dict) else {}
                kind = upd.get("sessionUpdate")
                turn = turns[-1]
                if kind == "agent_message_chunk":
                    turn.reply += _text_of(upd.get("content"))
                elif kind == "agent_thought_chunk":
                    turn.thought += _text_of(upd.get("content"))
                elif kind == "usage_update":
                    usage.append((seq, ts, int(upd.get("used") or 0), int(upd.get("size") or 0)))
                elif kind in ("tool_call", "tool_call_update"):
                    cid = str(upd.get("toolCallId") or "")
                    call = calls.get(cid)
                    if call is None:
                        call = ToolCall(seq, ts, cid, normalize_tool(upd.get("title")), {})
                        calls[cid] = call
                        turn.tools.append(call)
                    if isinstance(upd.get("rawInput"), dict) and not call.args:
                        call.args = upd["rawInput"]
                    if upd.get("title") and not call.name:
                        call.name = normalize_tool(upd.get("title"))
                    call.status = str(upd.get("status") or call.status)
                    if kind == "tool_call_update" and upd.get("content") is not None:
                        call.result += _text_of(upd.get("content"))
    return AgentLog(agent_id, str(path), turns, usage, sessions, _find_cuts(turns))


def _find_cuts(turns: list[Turn]) -> list[Cut]:
    cuts: list[Cut] = []
    for t in turns:
        if t.kind != "handoff":
            continue
        seed_turn = next((u.index for u in turns[t.index + 1:t.index + 3] if u.kind == "seed"), None)
        seed = turns[seed_turn].prompt if seed_turn is not None else ""
        cuts.append(Cut(len(cuts), t.index, seed_turn, t.ts, t.reply.strip(), seed))
    for i, c in enumerate(cuts):
        c.after_end = cuts[i + 1].handoff_turn if i + 1 < len(cuts) else len(turns)
    return cuts


def after_turns(log: AgentLog, cut: Cut) -> list[Turn]:
    start = (cut.seed_turn if cut.seed_turn is not None else cut.handoff_turn) + 1
    return log.turns[start:cut.after_end]


def before_turns(log: AgentLog, cut: Cut) -> list[Turn]:
    return log.turns[:cut.handoff_turn]


def expand_sessions(pattern: str | None = None, logs_dir: Path | None = None) -> list[Path]:
    """Mesh logs matching ``pattern``: comma-separated globs, absolute or relative to the logs dir."""
    from ..drivers.base import resolve_session_logs_dir
    base = Path(logs_dir) if logs_dir else resolve_session_logs_dir()
    found: set[str] = set()
    for part in (pattern or "mesh-*.jsonl").split(","):
        pat = os.path.expanduser(part.strip())
        if not pat:
            continue
        if not os.path.isabs(pat):
            pat = str(base / pat)
        found.update(p for p in glob.glob(pat) if fnmatch.fnmatch(os.path.basename(p), "mesh-*.jsonl"))
    return [Path(p) for p in sorted(found)]


def epoch(ts: str) -> float | None:
    """Seconds since the epoch; a timestamp without an offset is UTC (like the log rows)."""
    try:
        dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)).timestamp()
