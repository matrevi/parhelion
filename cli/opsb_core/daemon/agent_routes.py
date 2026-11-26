"""HTTP route handlers for `/v1/agents/*` and role-scoped `/v1/mcp/*` inside `opsb daemon`."""
from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import Any


def handle_agent_get(path: str, q: dict[str, str], ws: Path, runtime: Any) -> tuple[int, Any] | None:
    if path == "/v1/agents":
        return 200, {"ok": True, "agent_mesh": runtime.agent_mesh.build_hot_mesh(ws)}

    if path == "/v1/agents/pool":
        return 200, runtime.agent_mesh.get_harness_pool(ws)

    if path == "/v1/agents/messages":
        since_seq = int(q.get("since_seq") or 0)
        limit = int(q.get("limit") or 60)
        ack = q.get("ack", "1") not in ("0", "false", "no")
        prob = q.get("problem") or None
        return 200, runtime.agent_mesh.read_messages(ws, agent_id=None, problem=prob, since_seq=since_seq, limit=limit, ack=ack,
                                                    include_squad=q.get("audience") != "orchestrator")

    if path.startswith("/v1/agents/"):
        parts = [urllib.parse.unquote(p) for p in path[len("/v1/agents/"):].split("/") if p]
        if len(parts) == 2 and parts[1] == "messages":
            since_seq = int(q.get("since_seq") or 0)
            limit = int(q.get("limit") or 50)
            ack = q.get("ack", "1") not in ("0", "false", "no")
            return 200, runtime.agent_mesh.read_messages(ws, agent_id=parts[0], since_seq=since_seq, limit=limit, ack=ack,
                                                        include_squad=q.get("audience") != "orchestrator")
        if len(parts) == 2 and parts[1] == "inbox":
            since_seq = int(q.get("since_seq") or 0)
            ack_seq = int(q["ack_seq"]) if q.get("ack_seq") is not None else None
            return 200, runtime.agent_mesh.worker_inbox(ws, parts[0], since_seq=since_seq, ack_seq=ack_seq)

    return None


def handle_agent_post(path: str, body_obj: dict[str, Any], ws: Path, runtime: Any) -> tuple[int, Any] | None:
    try:
        if path == "/v1/events/wait":
            # Full-token callers only (the orchestrator); workers wait through /v1/mcp/worker/<id>.
            from ..mcp.handlers_agents import wait_body
            return 200, runtime.agent_mesh.event_hub(ws).wait(role="orchestrator", **wait_body(body_obj))

        if path == "/v1/events/test_wait":
            from ..services import test_runs
            request_id = str(body_obj.get("request_id") or "").strip()
            if not request_id:
                raise ValueError("'request_id' is required")
            hub = runtime.agent_mesh.event_hub(ws)
            return 200, test_runs.wait(ws, request_id, body_obj.get("wait_sec", 30),
                                       block=hub.test_blocker(request_id), full=body_obj.get("full") is True)

        if path == "/v1/agents/sprint":
            from ..agents.sprint import start_sprint
            return 200, start_sprint(runtime.agent_mesh, ws, driver=body_obj.get("driver") or "",
                                     preview=body_obj.get("preview") is True,
                                     enable_auto_submit=body_obj.get("enable_auto_submit") is True,
                                     mode=str(body_obj.get("mode") or "solo"),
                                     problems=body_obj.get("problems") if isinstance(body_obj.get("problems"), list) else None,
                                     seats=body_obj.get("seats"), role=str(body_obj.get("role") or ""),
                                     adjust=body_obj.get("adjust") is True,
                                     stop=body_obj.get("stop") if isinstance(body_obj.get("stop"), list) else None,
                                     replace=body_obj.get("replace") if isinstance(body_obj.get("replace"), list) else None)

        if path == "/v1/agents/spawn":
            res = runtime.agent_mesh.spawn_agent(
                ws,
                driver=str(body_obj.get("driver") or ""),
                task=str(body_obj.get("task") or ""),
                role_label=str(body_obj.get("role_label") or "worker"),
                problem=str(body_obj.get("problem") or ""),
                collab_mode=str(body_obj.get("collab_mode") or "solo"),
                axis=str(body_obj.get("axis") or ""),
                worktree_mode=str(body_obj.get("worktree_mode") or "isolated"),
                track=str(body_obj.get("track") or ""),
            )
            return 200, res

        if path == "/v1/agents/wait":
            res = runtime.agent_mesh.wait_events(
                ws,
                since_seq=int(body_obj.get("since_seq") or 0),
                agent_ids=body_obj.get("agent_ids") if isinstance(body_obj.get("agent_ids"), list) else None,
                problem=str(body_obj.get("problem") or "").strip() or None,
                strategic_only=bool(body_obj.get("strategic_only", True)),
                timeout_sec=float(body_obj.get("timeout_sec") if body_obj.get("timeout_sec") is not None else 25.0),
            )
            return 200, res

        if path == "/v1/agents/directive":
            res = runtime.agent_mesh.post_admin_directive(
                ws,
                str(body_obj.get("body") or body_obj.get("message") or ""),
                problem=str(body_obj.get("problem") or ""),
                broadcast_workers=bool(body_obj.get("broadcast_workers", False)),
            )
            return 200, res

        if path == "/v1/agents/cleanup":
            return 200, runtime.agent_mesh.cleanup_stopped(ws)

        if path.startswith("/v1/agents/"):
            parts = [urllib.parse.unquote(p) for p in path[len("/v1/agents/"):].split("/") if p]
            if len(parts) == 2:
                aid, action = parts
                if action == "squad":
                    from ..mcp.handlers_agents import squad_body
                    return 200, runtime.agent_mesh.squad(ws, aid, **squad_body(body_obj))
                if action == "delete":
                    return 200, runtime.agent_mesh.cleanup_stopped(ws, aid)
                if action == "send":
                    res = runtime.agent_mesh.send_message(
                        ws, aid, str(body_obj.get("message") or ""),
                        delivery=str(body_obj.get("delivery") or "queue"),
                    )
                    return 200, res
                if action == "stop":
                    res = runtime.agent_mesh.stop_agent(
                        ws, aid, cleanup_worktree=bool(body_obj.get("cleanup_worktree", False)),
                    )
                    return 200, res
                if action == "report":
                    streak = int(body_obj["no_gain_streak"]) if body_obj.get("no_gain_streak") is not None else None
                    res = runtime.agent_mesh.worker_report(
                        ws, aid,
                        kind=str(body_obj.get("kind") or "progress"),
                        summary=str(body_obj.get("summary") or ""),
                        no_gain_streak=streak,
                        artifacts=body_obj.get("artifacts") if isinstance(body_obj.get("artifacts"), list) else None,
                    )
                    return 200, res
    except KeyError as exc:
        return 404, {"ok": False, "error": str(exc)}
    except ValueError as exc:
        return 400, {"ok": False, "error": str(exc)}
    except Exception as exc:
        return 500, {"ok": False, "error": str(exc)}

    return None
