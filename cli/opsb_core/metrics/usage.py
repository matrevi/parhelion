"""Per-model-call token usage in one canonical record, parsed from each driver's own logs.

Record: ``{agent_id, driver, model, ts, input_miss, cache_read, cache_write, output, call_seq,
session_id, system_fp, tools_fp, session_start, rewrite}`` (``shape.classify`` adds the miss
fields). Sources, all opened read-only:

- DSH (``deepseek``): the mesh ACP log only has ``usage_update {used, size}``, so the record
  comes from the native ``~/.dsh/sessions/<key>/<sessionId>/session.v4.jsonl.zstd`` located
  through the ``session/new`` results in the mesh log. ``assistant/message`` carries
  ``usage {inputTokens (uncached), cacheReadTokens, cacheWriteTokens, outputTokens}``;
  ``system/message`` and ``request/header`` give the system and tool fingerprints, and a
  non-append ``surfaceOp`` is a declared rewrite.
- Codex app-server: ``thread/tokenUsage/updated`` ``tokenUsage.last`` where ``inputTokens``
  includes ``cachedInputTokens``; ``thread/compact/start`` or ``thread/compacted`` marks a rewrite.
- Claude stream-json: ``assistant.message.usage`` (``input_tokens`` uncached,
  ``cache_read_input_tokens``, ``cache_creation_input_tokens``), one record per message id;
  ``system/init`` gives model and tool names, ``system/compact_boundary`` a rewrite.
- Antigravity ACP and the per-turn CLI drivers report no cache fields: ``unavailable``.
"""
from __future__ import annotations

import datetime
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterable

from . import shape

DRIVER_PREFIXES = {"dee": "deepseek", "ant": "antigravity", "cod": "codex", "cla": "claude", "gem": "gemini", "pi-": "pi"}


def driver_for_agent(agent_id: str) -> str:
    return DRIVER_PREFIXES.get(str(agent_id or "")[:3], "")


def iso_from_ms(ms: Any) -> str | None:
    if not isinstance(ms, (int, float)):
        return None
    return datetime.datetime.fromtimestamp(ms / 1000.0, tz=datetime.timezone.utc).isoformat()


def content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return content_text(content.get("text") if "text" in content else content.get("content"))
    if isinstance(content, list):
        return "".join(content_text(c) for c in content)
    return ""


def decompress_zstd(data: bytes) -> bytes | None:
    """Decompress concatenated zstd frames (DSH appends one frame per write)."""
    if not data:
        return b""
    try:
        from compression import zstd  # Python 3.14+
        return zstd.decompress(data)
    except ImportError:
        pass
    except Exception:
        return None
    exe = shutil.which("zstd")
    if not exe:
        return None
    try:
        proc = subprocess.run([exe, "-dcq"], input=data, capture_output=True, timeout=60)
    except Exception:
        return None
    return proc.stdout if proc.returncode == 0 else None


def dsh_session_path(cwd: str, session_id: str, dsh_home: Path | None = None) -> Path:
    from ..drivers.dsh_driver import _dsh_project_key, _resolve_dsh_home
    home = Path(dsh_home) if dsh_home else _resolve_dsh_home()
    return home / "sessions" / _dsh_project_key(cwd) / session_id / "session.v4.jsonl.zstd"


def _record(agent_id: str, driver: str, st: dict[str, Any], **fields: Any) -> dict[str, Any]:
    rec = {"agent_id": agent_id, "driver": driver, "model": st.get("model") or "", "call_seq": st["call_seq"],
           "session_id": st.get("session") or "", "system_fp": st.get("system_fp"), "tools_fp": st.get("tools_fp"),
           "session_start": not st.get("started"), "rewrite": bool(st.get("rewrite"))}
    rec.update(fields)
    st["call_seq"] += 1
    st["started"] = True
    st["rewrite"] = False
    return rec


class DshParser:
    """Incremental parser of one DSH native session's events; ``state`` is JSON-serializable."""

    def __init__(self, agent_id: str, session_id: str, state: dict[str, Any] | None = None, call_seq: int = 0) -> None:
        self.agent_id = agent_id
        self.state = state or {"session": session_id, "system_fp": None, "tools_fp": None, "rewrite": False,
                               "started": False, "call_seq": call_seq, "model": "", "system_msgs": []}

    def _system(self, ev: dict[str, Any], data: dict[str, Any], op: Any) -> None:
        """Track every system message by seq; a ``replace`` op swaps (or, when empty, drops) a range."""
        msgs = self.state.setdefault("system_msgs", [])
        text = content_text((data.get("message") or {}).get("content"))
        if isinstance(op, dict) and op.get("op") == "replace":
            lo, hi = int(op.get("startSeq") or 0), int(op.get("endSeq") or 0)
            msgs[:] = [m for m in msgs if not lo <= m[0] <= hi]
            if text:
                msgs.append([lo, shape.fingerprint(text)])
        else:
            msgs.append([int(ev.get("seq") or 0), shape.fingerprint(text)])
        msgs.sort(key=lambda m: m[0])
        self.state["system_fp"] = shape.fingerprint([m[1] for m in msgs])

    def feed(self, events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        st, out = self.state, []
        for ev in events:
            if not isinstance(ev, dict):
                continue
            kind = ev.get("type")
            data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
            op = ev.get("surfaceOp")
            if kind == "system/message":
                self._system(ev, data, op)
            elif kind == "request/header":
                header = data.get("header") if isinstance(data.get("header"), dict) else {}
                st["tools_fp"] = shape.tools_fingerprint(header.get("tools")) or st["tools_fp"]
                st["model"] = str((header.get("config") or {}).get("model") or st["model"])
            elif isinstance(op, dict):
                st["rewrite"] = True  # a non-append surface op rewrote history
            if kind != "assistant/message":
                continue
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
            if usage is None:
                continue
            src = ((data.get("message") or {}).get("source") or {})
            st["model"] = str(src.get("model") or st["model"])
            out.append(_record(self.agent_id, "deepseek", st, ts=iso_from_ms(ev.get("time")),
                               input_miss=int(usage.get("inputTokens") or 0),
                               cache_read=int(usage.get("cacheReadTokens") or 0),
                               cache_write=int(usage.get("cacheWriteTokens") or 0),
                               output=int(usage.get("outputTokens") or 0)))
        return out


def dsh_events(raw: bytes) -> list[dict[str, Any]]:
    out = []
    for line in raw.splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


class MeshSessions:
    """Collect ``(sessionId, cwd, ts)`` from ``session/new`` request/result pairs of an ACP mesh log."""

    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self.state = state or {"pending": {}, "sessions": []}

    def feed(self, row: dict[str, Any]) -> None:
        msg = row.get("msg") if isinstance(row.get("msg"), dict) else {}
        if msg.get("method") in ("session/new", "session/load") and row.get("dir") == "out":
            self.state["pending"][str(msg.get("id"))] = str((msg.get("params") or {}).get("cwd") or "")
        elif "result" in msg and str(msg.get("id")) in self.state["pending"]:
            cwd = self.state["pending"].pop(str(msg.get("id")))
            sid = (msg.get("result") or {}).get("sessionId") if isinstance(msg.get("result"), dict) else None
            if sid and sid not in [s[0] for s in self.state["sessions"]]:
                self.state["sessions"].append([str(sid), cwd, str(row.get("ts") or "")])


class CodexParser:
    """Incremental parser of a Codex app-server mesh log (``{ts, dir, msg}`` rows)."""

    def __init__(self, agent_id: str, state: dict[str, Any] | None = None) -> None:
        self.agent_id = agent_id
        self.state = state or {"session": "", "system_fp": None, "tools_fp": None, "rewrite": False, "started": False,
                               "call_seq": 0, "model": "", "last_total": None, "pending_start": {}}

    def feed(self, rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        st, out = self.state, []
        for row in rows:
            msg = row.get("msg") if isinstance(row, dict) and isinstance(row.get("msg"), dict) else {}
            method = str(msg.get("method") or "")
            params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
            if method == "thread/start" and row.get("dir") == "out":
                st["pending_start"][str(msg.get("id"))] = True
            elif "result" in msg and st["pending_start"].pop(str(msg.get("id")), None):
                res = msg.get("result") if isinstance(msg.get("result"), dict) else {}
                st["model"] = str(res.get("model") or st["model"])
                st["session"] = str((res.get("thread") or {}).get("id") or res.get("threadId") or st["session"])
                st["started"] = False
            elif method in ("thread/compact/start", "thread/compacted"):
                st["rewrite"] = True
            elif method in ("item/started", "item/completed"):
                item = params.get("item") if isinstance(params.get("item"), dict) else {}
                if str(item.get("type") or "").lower() in ("contextcompaction", "compacted"):
                    st["rewrite"] = True
            elif method == "thread/tokenUsage/updated":
                tu = params.get("tokenUsage") if isinstance(params.get("tokenUsage"), dict) else {}
                total = (tu.get("total") or {}).get("totalTokens")
                last = tu.get("last") if isinstance(tu.get("last"), dict) else {}
                if not last or (total is not None and total == st["last_total"]):
                    continue
                st["last_total"] = total
                inp = int(last.get("inputTokens") or 0)
                cached = int(last.get("cachedInputTokens") or 0)
                write = int(last.get("cacheWriteInputTokens") or 0)
                out.append(_record(self.agent_id, "codex", st, ts=row.get("ts"), input_miss=max(0, inp - cached - write),
                                   cache_read=cached, cache_write=write, output=int(last.get("outputTokens") or 0)))
        return out


class ClaudeParser:
    """Incremental parser of raw Claude stream-json events; one record per assistant message id."""

    def __init__(self, agent_id: str, state: dict[str, Any] | None = None) -> None:
        self.agent_id = agent_id
        self.state = state or {"session": "", "system_fp": None, "tools_fp": None, "rewrite": False, "started": False,
                               "call_seq": 0, "model": "", "pending": None}

    def _flush(self, out: list[dict[str, Any]]) -> None:
        pend = self.state.get("pending")
        if pend:
            out.append(_record(self.agent_id, "claude", self.state, **pend["fields"]))
            self.state["pending"] = None

    def feed(self, events: Iterable[dict[str, Any]], *, final: bool = False) -> list[dict[str, Any]]:
        st, out = self.state, []
        for ev in events:
            if not isinstance(ev, dict):
                continue
            kind, sub = ev.get("type"), ev.get("subtype")
            if kind == "system" and sub == "init":
                self._flush(out)
                sid = str(ev.get("session_id") or "")
                if sid and sid != st["session"]:
                    st["session"], st["started"] = sid, False
                st["model"] = str(ev.get("model") or st["model"])
                names = sorted(str(t) for t in ev.get("tools") or [])
                st["tools_fp"] = shape.fingerprint(names) if names else st["tools_fp"]
            elif kind == "system" and sub == "compact_boundary":
                self._flush(out)
                st["rewrite"] = True
            elif kind == "assistant":
                msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
                usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else None
                if usage is None:
                    continue
                mid = str(msg.get("id") or "")
                fields = {"ts": ev.get("timestamp"), "input_miss": int(usage.get("input_tokens") or 0),
                          "cache_read": int(usage.get("cache_read_input_tokens") or 0),
                          "cache_write": int(usage.get("cache_creation_input_tokens") or 0),
                          "output": int(usage.get("output_tokens") or 0)}
                st["model"] = str(msg.get("model") or st["model"])
                pend = st.get("pending")
                if pend and pend["id"] == mid:
                    pend["fields"]["output"] = max(pend["fields"]["output"], fields["output"])
                    continue
                self._flush(out)
                st["pending"] = {"id": mid, "fields": fields}
            elif kind == "result":
                self._flush(out)
        if final:
            self._flush(out)
        return out


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    except OSError:
        pass
    return rows


def unavailable(agent_id: str, driver: str, reason: str) -> dict[str, Any]:
    return {"agent_id": agent_id, "driver": driver, "status": "unavailable", "reason": reason, "calls": []}


def dsh_file_calls(path: Path, agent_id: str, *, call_seq: int = 0) -> list[dict[str, Any]] | None:
    try:
        raw = decompress_zstd(path.read_bytes())
    except OSError:
        return None
    if raw is None:
        return None
    return DshParser(agent_id, path.parent.name, call_seq=call_seq).feed(dsh_events(raw))


def agent_usage(log_path: Path, agent_id: str | None = None, driver: str | None = None,
                *, dsh_home: Path | None = None, ttl_sec: float | None = None) -> dict[str, Any]:
    """Parse one worker's whole usage history and classify misses (non-incremental)."""
    log_path = Path(log_path)
    aid = agent_id or log_path.stem.removeprefix("mesh-")
    drv = (driver or driver_for_agent(aid)).lower()
    rows = read_jsonl(log_path)
    calls: list[dict[str, Any]] = []
    if drv == "deepseek":
        tracker = MeshSessions()
        for row in rows:
            tracker.feed(row)
        missing = 0
        for sid, cwd, _ in tracker.state["sessions"]:
            got = dsh_file_calls(dsh_session_path(cwd, sid, dsh_home), aid, call_seq=len(calls))
            if got is None:
                missing += 1
                continue
            calls.extend(got)
        if not calls:
            return unavailable(aid, drv, "no DSH native session log found" if missing or not tracker.state["sessions"]
                               else "DSH sessions carry no usage")
    elif drv == "codex":
        calls = CodexParser(aid).feed(rows)
    elif drv == "claude":
        calls = ClaudeParser(aid).feed(rows, final=True)
    else:
        return unavailable(aid, drv or "unknown", "driver logs carry no cache fields")
    if not calls:
        return unavailable(aid, drv, "no usage records in the log")
    shape.classify(calls, ttl_sec=ttl_sec)
    return {"agent_id": aid, "driver": drv, "status": "ok", "reason": "", "calls": calls}
