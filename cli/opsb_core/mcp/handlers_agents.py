"""MCP handlers for Multi-Harness Agent Mesh orchestration & worker messaging (daemon-proxied)."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ..agents.event_hub import MAX_BLOCK_SEC, EventHub, normalize_sources
from ..agents.manager import DEFAULT_WAIT_SEC, MAX_WAIT_SEC, AgentMeshManager

_LOCAL_MANAGER: AgentMeshManager | None = None
_ACTIVE_DAEMON_MANAGER: AgentMeshManager | None = None


def bind_daemon_agent_manager(manager: AgentMeshManager | None) -> None:
    """Bind the in-process daemon AgentMeshManager so HTTP `/v1/mcp` avoids self-loopback.

    The daemon's manager owns the event hub, so its legacy waits block on hub events.
    """
    global _ACTIVE_DAEMON_MANAGER
    _ACTIVE_DAEMON_MANAGER = manager
    if manager is not None:
        manager.hub_enabled = True


def daemon_hub(workspace: Path) -> EventHub | None:
    """The event hub when this code runs inside the daemon; None in a stdio or CLI process."""
    return _ACTIVE_DAEMON_MANAGER.event_hub(workspace) if _ACTIVE_DAEMON_MANAGER is not None else None


def _get_local_manager() -> AgentMeshManager:
    global _LOCAL_MANAGER
    if _LOCAL_MANAGER is None:
        _LOCAL_MANAGER = AgentMeshManager()
    return _LOCAL_MANAGER


def _proxy_daemon(
    method: str,
    path: str,
    workspace: Path,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = 12.0,
    missing_ok: bool = False,
) -> dict[str, Any] | None:
    """If `opsb daemon` is running (and we are NOT inside it), forward the bus call to daemon HTTP.

    ``missing_ok``: a 404 means the running daemon predates the route; return None so the caller
    falls back to its local path instead of failing.
    """
    if _ACTIVE_DAEMON_MANAGER is not None or os.environ.get("PARHELION_DISABLE_DAEMON_PROXY") == "1":
        return None
    try:
        from ..daemon.server import probe_daemon_health, read_daemon_state

        st = read_daemon_state()
        if not st or not probe_daemon_health(st, timeout=0.6):
            return None
        port = int(st["port"])
        token = str(st["token"])
    except Exception:
        return None

    ws_q = urllib.parse.urlencode({"workspace": str(workspace.expanduser().resolve())})
    sep = "&" if "?" in path else "?"
    url = f"http://127.0.0.1:{port}{path}{sep}{ws_q}"
    data = json.dumps({**(payload or {}), "workspace": str(workspace.expanduser().resolve())}).encode("utf-8") if method == "POST" else None
    headers = {"X-Opsb-Token": token}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if missing_ok and exc.code == 404:
            return None
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            err_obj = json.loads(raw)
            msg = str(err_obj.get("error") or raw)
        except Exception:
            msg = raw or str(exc)
        raise RuntimeError(msg) from None


def _mgr() -> AgentMeshManager:
    return _ACTIVE_DAEMON_MANAGER or _get_local_manager()


def tool_parhelion_harness_pool(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    if proxied := _proxy_daemon("GET", "/v1/agents/pool", workspace):
        return proxied
    return _mgr().get_harness_pool(workspace)


def tool_parhelion_sprint_start(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    from ..agents.sprint import MODES, start_sprint
    mode = str(args.get("mode") or "solo").strip().lower()
    if mode not in MODES:
        raise ValueError(f"mode must be one of: {', '.join(MODES)}")
    body = {"driver": args.get("driver") or "", "preview": args.get("preview") is True,
            "enable_auto_submit": args.get("enable_auto_submit") is True, "mode": mode}
    problems = args.get("problems")
    if problems is not None:
        if not isinstance(problems, list) or not all(isinstance(p, str) and p.strip() for p in problems):
            raise ValueError("problems must be a list of problem titles")
        body["problems"] = problems
    if _ACTIVE_DAEMON_MANAGER is not None:
        return start_sprint(_ACTIVE_DAEMON_MANAGER, workspace, **body)
    result = _proxy_daemon("POST", "/v1/agents/sprint", workspace, body, timeout=180)
    if result is None:
        raise RuntimeError("Start the Parhelion daemon for this workspace before starting a sprint")
    return result


def tool_parhelion_agent_spawn(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    body = {
        "driver": str(args.get("driver") or ""),
        "task": str(args.get("task") or ""),
        "role_label": str(args.get("role_label") or "worker"),
        "problem": str(args.get("problem") or ""),
        "collab_mode": str(args.get("collab_mode") or "solo"),
        "axis": str(args.get("axis") or ""),
        "worktree_mode": str(args.get("worktree_mode") or "isolated"),
        "track": str(args.get("track") or ""),
    }
    if proxied := _proxy_daemon("POST", "/v1/agents/spawn", workspace, body, timeout=20.0):
        return proxied
    return _mgr().spawn_agent(workspace, **body)


def tool_parhelion_agent_send(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    agent_id = str(args.get("agent_id") or "").strip()
    if not agent_id:
        raise ValueError("agent_id is required")
    body = {
        "message": str(args.get("message") or ""),
        "delivery": str(args.get("delivery") or "queue"),
    }
    if proxied := _proxy_daemon("POST", f"/v1/agents/{urllib.parse.quote(agent_id, safe='')}/send", workspace, body):
        return proxied
    return _mgr().send_message(workspace, agent_id, body["message"], delivery=body["delivery"])


def tool_parhelion_agent_wait(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    since_seq = int(args.get("since_seq") or 0)
    agent_ids = args.get("agent_ids") if isinstance(args.get("agent_ids"), list) else None
    problem = str(args.get("problem") or "").strip() or None
    strategic_only = bool(args.get("strategic_only", True))
    raw_timeout = float(args.get("timeout_sec") if args.get("timeout_sec") is not None else DEFAULT_WAIT_SEC)
    capped = max(0.5, min(MAX_WAIT_SEC, raw_timeout))
    body = {"since_seq": since_seq, "agent_ids": agent_ids, "problem": problem, "strategic_only": strategic_only, "timeout_sec": capped}
    if proxied := _proxy_daemon("POST", "/v1/agents/wait", workspace, body, timeout=capped + 6.0):
        return proxied
    return _mgr().wait_events(workspace, since_seq=since_seq, agent_ids=agent_ids, problem=problem, strategic_only=strategic_only, timeout_sec=capped)


def wait_body(args: dict[str, Any]) -> dict[str, Any]:
    """Validated ``EventHub.wait`` keyword arguments from MCP arguments (or a daemon route body)."""
    timeout = args.get("timeout")
    ids = args.get("agent_ids")
    return {
        "since": str(args.get("since") or "").strip() or None,
        "until": list(normalize_sources(args.get("until"))),
        "timeout": max(0.0, min(MAX_BLOCK_SEC, float(timeout))) if timeout is not None else None,
        "agent_ids": [str(x) for x in ids] if isinstance(ids, list) else None,
        "problem": str(args.get("problem") or "").strip() or None,
        "strategic_only": args.get("strategic_only") is True,
    }


def tool_parhelion_wait(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    body = wait_body(args)
    if args.get("_worker_request"):
        agent_id = str(args.get("_caller_agent_id") or "").strip()
        if not agent_id:
            raise ValueError("worker agent_id context missing")
        scoped = {k: body[k] for k in ("since", "until", "timeout")}  # a worker's scope is fixed
        return _mgr().event_hub(workspace).wait(role="worker", agent_id=agent_id, **scoped)
    block = body["timeout"] if body["timeout"] is not None else MAX_BLOCK_SEC
    if proxied := _proxy_daemon("POST", "/v1/events/wait", workspace, body, timeout=block + 6.0, missing_ok=True):
        return proxied
    return _mgr().event_hub(workspace).wait(role="orchestrator", **body)


def tool_parhelion_agent_messages(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    agent_id = str(args.get("agent_id") or "").strip() or None
    problem = str(args.get("problem") or "").strip() or None
    since_seq = int(args.get("since_seq") or 0)
    limit = int(args.get("limit") or 60)
    ack = bool(args.get("ack", True))
    q_dict: dict[str, Any] = {"since_seq": since_seq, "limit": limit, "ack": "1" if ack else "0", "audience": "orchestrator"}
    if problem:
        q_dict["problem"] = problem
    q = urllib.parse.urlencode(q_dict)
    path = f"/v1/agents/{urllib.parse.quote(agent_id, safe='')}/messages?{q}" if agent_id else f"/v1/agents/messages?{q}"
    if proxied := _proxy_daemon("GET", path, workspace):
        return proxied
    return _mgr().read_messages(workspace, agent_id=agent_id, problem=problem, since_seq=since_seq, limit=limit, ack=ack,
                                include_squad=False)


def tool_parhelion_agent_stop(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    agent_id = str(args.get("agent_id") or "").strip()
    if not agent_id:
        raise ValueError("agent_id is required")
    body = {"cleanup_worktree": bool(args.get("cleanup_worktree", False))}
    if proxied := _proxy_daemon("POST", f"/v1/agents/{urllib.parse.quote(agent_id, safe='')}/stop", workspace, body):
        return proxied
    return _mgr().stop_agent(workspace, agent_id, cleanup_worktree=body["cleanup_worktree"])


def tool_parhelion_agent_report(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    agent_id = str(args.get("_caller_agent_id") or args.get("agent_id") or os.environ.get("PARHELION_AGENT_ID") or "").strip()
    if not agent_id:
        raise ValueError("worker agent_id context missing")
    streak = int(args["no_gain_streak"]) if args.get("no_gain_streak") is not None else None
    body = {
        "kind": str(args.get("kind") or "progress"),
        "summary": str(args.get("summary") or ""),
        "no_gain_streak": streak,
        "artifacts": args.get("artifacts") if isinstance(args.get("artifacts"), list) else [],
    }
    if proxied := _proxy_daemon("POST", f"/v1/agents/{urllib.parse.quote(agent_id, safe='')}/report", workspace, body):
        return proxied
    return _mgr().worker_report(
        workspace, agent_id, kind=body["kind"], summary=body["summary"],
        no_gain_streak=streak, artifacts=body["artifacts"],
    )


def tool_parhelion_agent_inbox(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    agent_id = str(args.get("_caller_agent_id") or args.get("agent_id") or os.environ.get("PARHELION_AGENT_ID") or "").strip()
    if not agent_id:
        raise ValueError("worker agent_id context missing")
    since_seq = int(args.get("since_seq") or 0)
    ack_seq = int(args["ack_seq"]) if args.get("ack_seq") is not None else None
    qs_dict: dict[str, Any] = {"since_seq": since_seq}
    if ack_seq is not None:
        qs_dict["ack_seq"] = ack_seq
    path = f"/v1/agents/{urllib.parse.quote(agent_id, safe='')}/inbox?{urllib.parse.urlencode(qs_dict)}"
    if proxied := _proxy_daemon("GET", path, workspace):
        return proxied
    return _mgr().worker_inbox(workspace, agent_id, since_seq=since_seq, ack_seq=ack_seq)


def squad_body(args: dict[str, Any]) -> dict[str, Any]:
    """The envelope fields of one ``parhelion_squad`` call (shared by the MCP handler and the route)."""
    body = {k: str(args.get(k) or "").strip()
            for k in ("recipient", "to", "message", "request_id", "claim", "evidence", "ask")}
    refs = args.get("refs")
    if isinstance(refs, list):
        body["refs"] = [str(ref).strip() for ref in refs if str(ref).strip()]
    elif isinstance(refs, str) and refs.strip():
        body["refs"] = refs
    return body


def tool_parhelion_squad(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    sender = args.get("_caller_agent_id")
    if not sender:
        raise ValueError("A bound worker identity is required")
    body = squad_body(args)
    if proxied := _proxy_daemon("POST", f"/v1/agents/{urllib.parse.quote(sender, safe='')}/squad", workspace, body):
        return proxied
    return _mgr().squad(workspace, sender, **body)
