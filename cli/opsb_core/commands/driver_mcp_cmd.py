"""CLI subcommands for Unified Driver Runtime (`opsb driver ...`) and Local MCP Server (`opsb mcp ...`)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .. import config as _cfg
from ..drivers.base import load_parhelion_config
from ..drivers.registry import (
    VALID_DRIVERS,
    doctor_drivers,
    get_driver,
    launch_problem_session,
    list_all_sessions,
    list_drivers,
    select_active_driver,
    select_worker_model,
    WORKER_MODELS,
)
from ..mcp.server import serve_stdio
from ..mcp.tools import call_tool, list_tool_schemas


def _resolve_ws(explicit: str | None) -> Path:
    cfg = load_parhelion_config()
    return _cfg.ConfigRepository.resolve_workspace(explicit, cfg)


def _emit_error(msg: str, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
    else:
        print(msg, file=sys.stderr)
    return 1


def run_driver(args: argparse.Namespace) -> int:
    sub = getattr(args, "driver_cmd", "list") or "list"
    as_json = bool(getattr(args, "json", False))

    try:
        if sub == "list":
            res = list_drivers()
            if as_json:
                print(json.dumps(res, ensure_ascii=False, indent=2))
                return 0
            print(f"=== Parhelion Harness Drivers (active: {res['active_driver']}) ===")
            for d in res["drivers"]:
                mark = "*" if d["active"] else " "
                print(f"  {mark} {d['kind']:<12} {d['display_name']:<18} protocol={d['protocol']}")
            return 0

        if sub == "doctor":
            res = doctor_drivers(getattr(args, "driver", None))
            if as_json:
                print(json.dumps(res, ensure_ascii=False, indent=2))
                return 0
            print(f"=== Driver Doctor (active: {res['active_driver']}) ===")
            for diag in res["diagnostics"]:
                mark = "*" if diag.get("active") else " "
                inst = "OK" if diag["installed"] else "MISSING"
                auth = "AUTH" if diag["auth_ok"] else "NOAUTH"
                ver = diag["version"] or "—"
                gui_s = (
                    f" gui={diag.get('gui_version') or 'app'} ({diag.get('url_scheme') or 'app'}://)"
                    if diag.get("gui_installed")
                    else ""
                )
                print(
                    f"  {mark} {diag['kind']:<12} [{inst:<7}/{auth:<6}]  "
                    f"proto={diag['protocol']:<15} ver={ver}{gui_s}  ({diag['detail']})"
                )
            return 0

        if sub == "model":
            res = select_worker_model(args.kind, args.model)
            print(json.dumps(res, ensure_ascii=False) if as_json else res["worker_model"])
            return 0

        if sub == "select":
            res = select_active_driver(args.kind)
            if as_json:
                print(json.dumps(res, ensure_ascii=False))
            else:
                print(f"Active driver set to: {res['active_driver']}")
            return 0

        if sub == "sync":
            ws = _resolve_ws(getattr(args, "workspace", None))
            drv = get_driver(getattr(args, "driver", None))
            res = drv.sync_workspace(ws, mcp_url=getattr(args, "mcp_url", None))
            if as_json:
                print(json.dumps(res, ensure_ascii=False, indent=2))
            else:
                print(
                    f"Synced workspace for driver '{drv.kind}': skills={res.get('skills_dir')} · "
                    f"mcp={res.get('mcp_config_path')}"
                )
            return 0

        if sub == "launch":
            ws = _resolve_ws(getattr(args, "workspace", None))
            res = launch_problem_session(
                workspace=ws,
                problem=args.problem,
                driver_kind=getattr(args, "driver", None),
                mode=getattr(args, "mode", "headless") or "headless",
                track_name=getattr(args, "track", "") or "",
                goal=getattr(args, "goal", "") or "",
                resume_id=getattr(args, "resume", None),
                use_main=bool(getattr(args, "use_main", False)),
                dry_run=bool(getattr(args, "dry_run", False)),
                preseed=bool(getattr(args, "preseed", False)),
            )
            if as_json:
                print(json.dumps(res, ensure_ascii=False, indent=2))
            else:
                sess = res["session"]
                extra = []
                if res.get("deep_link"):
                    extra.append(f"  Deep Link: {res['deep_link']}")
                if res.get("brief_path"):
                    extra.append(f"  Brief    : {res['brief_path']} (clipboard={res.get('clipboard_copied')})")
                extra_s = ("\n" + "\n".join(extra)) if extra else ""
                print(
                    f"Session {sess['session_id']} [{sess['driver']} · {res['launch_mode']} · {sess['status']}]\n"
                    f"  Worktree : {sess['worktree']}\n"
                    f"  Native ID: {sess['native_session_id']}\n"
                    f"  Command  : {' '.join(res.get('command') or [])}{extra_s}"
                )
            return 0

        if sub == "sessions":
            ws = _resolve_ws(getattr(args, "workspace", None))
            include_gui = bool(getattr(args, "include_gui", True))
            recs = list_all_sessions(
                ws,
                problem=getattr(args, "problem", None),
                driver=getattr(args, "driver", None),
                include_gui=include_gui,
            )
            payload = {"workspace": str(ws), "include_gui": include_gui, "sessions": recs}
            if as_json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
                return 0
            print(f"=== Driver Sessions ({ws}) · {len(recs)} record(s) ===")
            for r in recs:
                steps_s = f" steps={r.get('observed_steps')}" if r.get("observed_steps") else ""
                dl_s = f" [{r.get('deep_link')}]" if r.get("deep_link") else ""
                print(
                    f"  {str(r.get('session_id')):<28} {str(r.get('driver')):<12} {str(r.get('problem')):<22} "
                    f"{str(r.get('status')):<10} pid={r.get('pid') or '—'}{steps_s}  {r.get('worktree')}{dl_s}"
                )
            return 0

        return _emit_error(f"Unknown driver subcommand: {sub}", as_json)
    except Exception as exc:
        return _emit_error(str(exc), as_json)


def run_mcp(args: argparse.Namespace) -> int:
    sub = getattr(args, "mcp_cmd", "list-tools") or "list-tools"
    as_json = bool(getattr(args, "json", False))

    if sub == "serve":
        ws = _resolve_ws(getattr(args, "workspace", None))
        role = getattr(args, "role", "orchestrator") or "orchestrator"
        agent_id = getattr(args, "agent_id", None)
        return serve_stdio(workspace=ws, role=role, agent_id=agent_id)

    if sub == "list-tools":
        role = getattr(args, "role", "orchestrator") or "orchestrator"
        tools = list_tool_schemas(role=role)
        if as_json:
            print(json.dumps({"role": role, "tools": tools}, ensure_ascii=False, indent=2))
            return 0
        print(f"=== Parhelion MCP Tools ({len(tools)}, role={role}) ===")
        for t in tools:
            req = ", ".join((t.get("inputSchema") or {}).get("required") or [])
            req_s = f" (required: {req})" if req else ""
            print(f"  - {t['name']}: {t['description']}{req_s}")
        return 0

    if sub == "call":
        ws = _resolve_ws(getattr(args, "workspace", None))
        role = getattr(args, "role", "orchestrator") or "orchestrator"
        agent_id = getattr(args, "agent_id", None)
        raw_args = getattr(args, "args_json", None) or "{}"
        try:
            parsed_args: dict[str, Any] = json.loads(raw_args)
            if not isinstance(parsed_args, dict):
                raise ValueError("--args-json must decode to a JSON object")
        except Exception as exc:
            print(json.dumps({"ok": False, "error": f"Invalid --args-json: {exc}"}, ensure_ascii=False))
            return 1
        try:
            res = call_tool(args.tool_name, parsed_args, ws, role=role, agent_id=agent_id)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        except Exception as exc:
            print(json.dumps({"ok": False, "tool": args.tool_name, "error": str(exc)}, ensure_ascii=False))
            return 1

    print(json.dumps({"ok": False, "error": f"Unknown mcp subcommand: {sub}"}), file=sys.stderr)
    return 1


def register_driver_and_mcp_subcommands(sub: argparse._SubParsersAction) -> None:
    """Register `opsb driver ...` and `opsb mcp ...` subcommands onto the root CLI parser."""
    s_drv = sub.add_parser("driver", help="Manage unified Harness Driver runtime & sessions")
    s_drv.add_argument("--json", action="store_true")
    s_drv.set_defaults(func=run_driver, driver_cmd="list")
    drv_sub = s_drv.add_subparsers(dest="driver_cmd")

    d_ls = drv_sub.add_parser("list", help="List supported Harness Drivers and active selection")
    d_ls.add_argument("--json", action="store_true")
    d_ls.set_defaults(func=run_driver)

    d_doc = drv_sub.add_parser("doctor", help="Inspect installed binaries, protocols, and auth readiness")
    d_doc.add_argument("--driver", choices=VALID_DRIVERS, default=None)
    d_doc.add_argument("--json", action="store_true")
    d_doc.set_defaults(func=run_driver)

    d_sel = drv_sub.add_parser("select", help="Set active Harness Driver in ~/.parhelion/config.json")
    d_sel.add_argument("kind", choices=VALID_DRIVERS)
    d_sel.add_argument("--json", action="store_true")
    d_sel.set_defaults(func=run_driver)

    d_model = drv_sub.add_parser("model", help="Set the explicit model for new managed workers")
    d_model.add_argument("kind", choices=WORKER_MODELS)
    d_model.add_argument("model")
    d_model.add_argument("--json", action="store_true")
    d_model.set_defaults(func=run_driver)

    d_sy = drv_sub.add_parser("sync", help="Project .agents/skills and isolated MCP config into workspace")
    d_sy.add_argument("--driver", choices=VALID_DRIVERS, default=None)
    d_sy.add_argument("--workspace", "-w")
    d_sy.add_argument("--mcp-url", default=None)
    d_sy.add_argument("--json", action="store_true")
    d_sy.set_defaults(func=run_driver)

    d_la = drv_sub.add_parser("launch", help="Prepare worktree + <3KB brief and launch a Harness session")
    d_la.add_argument("problem")
    d_la.add_argument("--driver", choices=VALID_DRIVERS, default=None)
    d_la.add_argument("--mode", choices=["headless", "gui", "dry-run", "background"], default="headless")
    d_la.add_argument("--track", default="")
    d_la.add_argument("--goal", default="")
    d_la.add_argument("--resume", default=None)
    d_la.add_argument("--use-main", action="store_true")
    d_la.add_argument("--dry-run", action="store_true")
    d_la.add_argument("--preseed", action="store_true")
    d_la.add_argument("--workspace", "-w")
    d_la.add_argument("--json", action="store_true")
    d_la.set_defaults(func=run_driver)

    d_se = drv_sub.add_parser("sessions", help="List persisted & live GUI Harness Driver sessions for workspace")
    d_se.add_argument("--problem", "-p", default=None)
    d_se.add_argument("--driver", choices=VALID_DRIVERS, default=None)
    d_se.add_argument("--include-gui", dest="include_gui", action="store_true", default=True)
    d_se.add_argument("--no-gui", dest="include_gui", action="store_false")
    d_se.add_argument("--workspace", "-w")
    d_se.add_argument("--json", action="store_true")
    d_se.set_defaults(func=run_driver)

    s_mcp = sub.add_parser("mcp", help="Parhelion local MCP JSON-RPC 2.0 server & tool caller")
    s_mcp.add_argument("--json", action="store_true")
    s_mcp.set_defaults(func=run_mcp, mcp_cmd="list-tools")
    mcp_sub = s_mcp.add_subparsers(dest="mcp_cmd")

    m_srv = mcp_sub.add_parser("serve", help="Run line-delimited JSON-RPC 2.0 MCP server on stdio")
    m_srv.add_argument("--workspace", "-w")
    m_srv.add_argument("--role", choices=["orchestrator", "worker", "recon", "knowledge"], default="orchestrator")
    m_srv.add_argument("--agent-id", default=None)
    m_srv.set_defaults(func=run_mcp)

    m_ls = mcp_sub.add_parser("list-tools", help="List all Parhelion MCP tool schemas")
    m_ls.add_argument("--role", choices=["orchestrator", "worker", "recon", "knowledge"], default="orchestrator")
    m_ls.add_argument("--json", action="store_true")
    m_ls.set_defaults(func=run_mcp)

    m_ca = mcp_sub.add_parser("call", help="Invoke a Parhelion MCP tool directly from CLI")
    m_ca.add_argument("tool_name")
    m_ca.add_argument("--args-json", default="{}")
    m_ca.add_argument("--role", choices=["orchestrator", "worker", "recon", "knowledge"], default="orchestrator")
    m_ca.add_argument("--agent-id", default=None)
    m_ca.add_argument("--workspace", "-w")
    m_ca.add_argument("--json", action="store_true")
    m_ca.set_defaults(func=run_mcp)
