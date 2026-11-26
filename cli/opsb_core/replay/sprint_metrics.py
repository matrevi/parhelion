"""Sprint metrics recomputed from session logs (docs/v4.2/00-PLAN.md sections 1 and 3).

Definitions (one place, so later sprints are measured the same way):

- context peak: max ``usage_update.used`` per worker log; compactions: handoff turns.
- startup context: the last ``usage_update.used`` at or before the worker's first
  ``parhelion_*`` tool call (the first sample after it when none precedes it).
- ``cd`` shell calls: ``bash`` commands whose text starts with ``cd ``; bytes are the UTF-8
  size of the logged result; grouped by the first program after the ``cd`` chain.
- inline Python: a ``bash`` command running ``python -c`` or a heredoc; it "reads JSON"
  when the script mentions ``json`` and "parses report.json" when it names ``report.json``.
- CLI bypass: shell commands invoking ``opsb queue list``, ``opsb history``,
  ``opsb bench verify-escape``.
- orchestrator polling: ``agent_wait``/``test_wait``/``agent_send``/``status`` calls in DSH
  conversations whose cwd is the workspace root (``sources.orchestrator_sessions``).
- bus: messages created in the window, by kind (count, bytes, longest).
- re-reads: a read is a ``read`` tool call or a ``cat``/``sed``/``head``/``tail``/``nl``/``grep``
  shell segment naming files; after a cut it is a re-read when any of its paths was read
  before that cut. Reported against all reads and against post-cut reads.
- re-tests: a ``parhelion_test`` after a cut whose ``(sha7, level)`` was requested before it.
- comms (4.3 P27): ``parhelion_squad`` calls by worker seats, how many were workspace broadcasts
  (``recipient``/``to`` == all) and the evidence class each message carried (``local``, ``paired``,
  ``online_receipt``, or ``legacy`` for a pre-P27 envelope with a bare string evidence). One call is
  one message: a broadcast counts once, because every recipient gets the same envelope class.
"""
from __future__ import annotations

import collections
import math
import statistics
from pathlib import Path
from typing import Any

from .. import evidence as _evidence
from ..metrics import shape
from . import actions, sources
from .loader import AgentLog, expand_sessions, epoch, load_agent


def _median(values: list[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def _p95(values: list[int]) -> int:
    vals = sorted(values)
    return int(vals[max(0, math.ceil(0.95 * len(vals)) - 1)]) if vals else 0


def window_of(logs: list[AgentLog]) -> tuple[str, str]:
    firsts = [g.first_ts for g in logs if g.first_ts]
    lasts = [g.last_ts for g in logs if g.last_ts]
    return (min(firsts) if firsts else "", max(lasts) if lasts else "")


def hours_between(since: str, until: str) -> float:
    a, b = epoch(since), epoch(until)
    return round(max(0.0, (b - a) / 3600.0), 3) if a is not None and b is not None else 0.0


def startup_context(log: AgentLog) -> int | None:
    first = next((c for c in log.tool_calls() if c.is_platform), None)
    if first is None or not log.usage:
        return None
    before = [u for u in log.usage if u[0] <= first.seq]
    return (before[-1] if before else log.usage[0])[2]


def context_metrics(logs: list[AgentLog]) -> dict[str, Any]:
    peaks = {g.agent_id: max((u[2] for u in g.usage), default=0) for g in logs}
    starts = {g.agent_id: startup_context(g) for g in logs}
    vals = [v for v in starts.values() if v is not None]
    return {
        "peak_median": _median(list(peaks.values())), "peak_max": max(peaks.values(), default=0),
        "compactions": sum(len(g.cuts) for g in logs), "startup_median": _median(vals),
        "startup_max": max(vals, default=None), "per_agent": {
            g.agent_id: {"peak": peaks[g.agent_id], "startup": starts[g.agent_id], "compactions": len(g.cuts)}
            for g in logs},
    }


def _rate(n: float, hours: float) -> float | None:
    return round(n / hours, 1) if hours > 0 else None


def shell_metrics(logs: list[AgentLog], hours: float) -> dict[str, Any]:
    cd = {"count": 0, "bytes": 0, "by_program": {g: {"count": 0, "bytes": 0} for g in actions.CD_GROUPS + ("other",)}}
    py = {"count": 0, "reads_json": 0, "parses_report": 0}
    bypass: collections.Counter[str] = collections.Counter()
    for log in logs:
        for call in log.tool_calls():
            cmd = actions.shell_command(call)
            if not cmd:
                continue
            group = actions.cd_group(cmd)
            if group is not None:
                cd["count"] += 1
                cd["bytes"] += call.result_bytes
                cd["by_program"][group]["count"] += 1
                cd["by_program"][group]["bytes"] += call.result_bytes
            info = actions.inline_python(cmd)
            if info is not None:
                py["count"] += 1
                py["reads_json"] += info["reads_json"]
                py["parses_report"] += info["parses_report"]
            bypass.update(actions.cli_bypass(cmd))
    cd["per_hour"], py["per_hour"] = _rate(cd["count"], hours), _rate(py["count"], hours)
    return {"cd_shell": cd, "inline_python": py, "cli_bypass": {k: bypass.get(k, 0) for k in actions.CLI_BYPASS}}


def repeat_metrics(logs: list[AgentLog]) -> dict[str, Any]:
    """Re-reads and re-tests after each compaction cut (see module docstring)."""
    c: collections.Counter[str] = collections.Counter()
    for log in logs:
        cwd = log.sessions[0][1] if log.sessions else ""
        seen_paths: set[str] = set()
        seen_tests: set[tuple[str, str]] = set()
        snap_paths: set[str] | None = None
        snap_tests: set[tuple[str, str]] | None = None
        for turn in log.turns:
            if turn.kind == "handoff":
                snap_paths, snap_tests = set(seen_paths), set(seen_tests)
            for call in turn.tools:
                paths = actions.read_paths(call, cwd)
                key = actions.test_key(call)
                if paths:
                    c["reads"] += 1
                    if snap_paths is not None:
                        c["reads_after_cut"] += 1
                        c["rereads"] += any(p in snap_paths for p in paths)
                if key:
                    c["tests"] += 1
                    body = actions.platform_result(call)
                    c["tests_reused"] += bool(body.get("reused") or body.get("joined"))
                    if snap_tests is not None:
                        c["tests_after_cut"] += 1
                        c["retests"] += key in snap_tests
                seen_paths.update(paths)
                if key:
                    seen_tests.add(key)
    reads, after = c["reads"], c["reads_after_cut"]
    return {
        "reads": reads, "reads_after_cut": after, "rereads": c["rereads"],
        "reread_ratio_all": round(c["rereads"] / reads, 3) if reads else None,
        "reread_ratio_after_cut": round(c["rereads"] / after, 3) if after else None,
        "tests": c["tests"], "tests_after_cut": c["tests_after_cut"], "retests": c["retests"],
        "tests_reused_or_joined": c["tests_reused"],
    }


def worker_poll_metrics(logs: list[AgentLog], hours: float) -> dict[str, Any]:
    """Worker waiting calls (``test_wait``, ``status``, ``agent_inbox``, ``wait``) in the mesh logs."""
    counts: collections.Counter[str] = collections.Counter()
    for log in logs:
        for call in log.tool_calls():
            name = call.name.removeprefix("parhelion_")
            if call.is_platform and name in sources.WORKER_POLL_TOOLS:
                counts[name] += 1
    total = sum(counts.values())
    return {**{k: counts.get(k, 0) for k in sources.WORKER_POLL_TOOLS}, "poll_total": total,
            "poll_per_hour": _rate(total, hours)}


def tool_metrics(logs: list[AgentLog], hours: float) -> dict[str, Any]:
    """Per-tool call counts and logged result bytes (the A3 baseline before live metering)."""
    sizes: dict[str, list[int]] = collections.defaultdict(list)
    for log in logs:
        for call in log.tool_calls():
            sizes[call.name or "?"].append(call.result_bytes)
    out = {}
    for name, vals in sorted(sizes.items(), key=lambda kv: -sum(kv[1])):
        out[name] = {"calls": len(vals), "per_hour": _rate(len(vals), hours), "result_bytes": sum(vals),
                     "result_bytes_mean": round(sum(vals) / len(vals), 1), "result_bytes_p95": _p95(vals)}
    return out


def comms_metrics(logs: list[AgentLog], hours: float) -> dict[str, Any]:
    """Peer messages: how many, how many were broadcasts, and the evidence class of each (4.3 P27)."""
    by_class: collections.Counter[str] = collections.Counter()
    broadcasts = sends = 0
    for log in logs:
        for call in log.tool_calls():
            if not call.is_platform or call.name.removeprefix("parhelion_") != "squad":
                continue
            args = call.args if isinstance(call.args, dict) else {}
            target = str(args.get("recipient") or args.get("to") or "").strip()
            if not target:
                continue  # a listing call is not a message
            sends += 1
            if target.lower() == "all":
                broadcasts += 1
            raw = args.get("evidence")
            if isinstance(raw, dict):
                by_class[_evidence.normalize(raw)["class"]] += 1
            else:
                by_class["legacy"] += 1
    return {"squad_messages": sends, "broadcasts": broadcasts, "per_hour": _rate(sends, hours),
            "evidence": {name: by_class.get(name, 0)
                         for name in (*_evidence.CLASSES, "legacy")}}


def orchestrator_metrics(sessions: list[dict[str, Any]], hours: float) -> dict[str, Any]:
    counts: collections.Counter[str] = collections.Counter()
    usage_by_session = {}
    for s in sessions:
        counts.update(s["tool_counts"])
        summ = shape.summarize(shape.classify(s["calls"]))
        usage_by_session[s["session_id"]] = {k: summ[k] for k in (
            "calls", "cache_hit_pct", "input_miss", "cache_read", "cache_write", "output", "miss_events",
            "last_miss_reason", "model")} | {"reasons": {k: v for k, v in summ["reasons"].items() if v}}
    polls = {k: counts.get(k, 0) for k in sources.POLL_TOOLS}
    total = sum(polls.values())
    # 4.3 P27: the DSH usage records carry tool names but not their arguments, so an orchestrator
    # broadcast is counted here as a squad call; the class and the "all" target come from mesh logs.
    return {"sessions": [Path(s["path"]).parent.name for s in sessions], **polls,
            "squad_calls": counts.get("squad", 0),
            "poll_total": total, "poll_per_hour": _rate(total, hours), "usage": usage_by_session}


def compute(paths: list[Path], *, workspace: Path | None = None, since: str | None = None,
            until: str | None = None, orchestrator_paths: list[Path] | None = None,
            logs: list[AgentLog] | None = None) -> dict[str, Any]:
    if logs is None:
        logs = [load_agent(p, since=since, until=until) for p in paths]
    logs = [g for g in logs if g.turns]
    lo, hi = window_of(logs)
    lo, hi = since or lo, until or hi
    hours = hours_between(lo, hi)
    out: dict[str, Any] = {
        "window": {"since": lo, "until": hi, "hours": hours, "workers": len(logs),
                   "sessions": [g.agent_id for g in logs]},
        "context": context_metrics(logs), **shell_metrics(logs, hours), "repeats": repeat_metrics(logs),
        "worker_polls": worker_poll_metrics(logs, hours), "tools": tool_metrics(logs, hours),
        "comms": comms_metrics(logs, hours),
    }
    if workspace is not None:
        orch = sources.orchestrator_sessions(workspace, lo, hi, paths=orchestrator_paths)
        out["orchestrator"] = orchestrator_metrics(orch, hours)
        out["bus"] = sources.bus_message_stats(workspace, lo, hi)
    return out


def compute_from_glob(pattern: str | None, **kw: Any) -> dict[str, Any]:
    return compute(expand_sessions(pattern), **kw)
