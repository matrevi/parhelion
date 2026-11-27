"""Detect a worker that reaches into the platform's implementation or credentials (4.2 K).

A worker's tool-call **input** is read from either event shape — ACP ``session/update``
``tool_call``/``tool_call_update`` rows (``rawInput``) and DSH native session ``tool/call``
rows (``data.arguments``, a JSON string) — and classified into one of four classes: reading
platform state, importing platform code, opening a judge credential store, or calling a
team-session helper. ``boundary_scan`` walks recorded streams with the same classifier.

A read is only a hit when it reaches *outside* the worker's own worktree: ``state/test_runs``
inside the worktree is the worker's own directory and stays legal, so relative references
are not flagged and a path that resolves under the worktree is dropped before matching.

Detection never stops a worker. A hit writes one ``platform`` message to the orchestrator and
one to the administrator (once per worker and class, idempotent on the bus ``event_key``), one
ledger ``constraint`` (idempotent on its ``ekey``) and the worker's hot-mesh flag; whether to
stop the worker stays the administrator's decision. Only the worker, the time and the class
travel with a hit — never the command, the path it opened or anything read from it.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

from . import config as _cfg
from .worker_sandbox import parhelion_home, platform_source_dirs

#: Most severe first: a command that both imports platform code and reads sessions is one
#: ``team_sessions`` hit, so the alert names the boundary that actually broke.
CATEGORY_ORDER = ("team_sessions", "judge_credentials", "platform_state", "platform_import")
CATEGORY_LABELS = {
    "team_sessions": "team session helper (_all_team_sessions)",
    "judge_credentials": "judge credential store",
    "platform_state": "platform state (test_runs/agent_bus/hardware)",
    "platform_import": "platform source import",
}

SHELL_TOOLS = frozenset({"bash", "shell", "sh", "exec", "exec_command", "run_shell", "terminal", "command",
                          "commandexecution", "command_execution", "local_shell", "shell_call",
                          "execcommand", "run_shell_command"})
FILE_TOOLS = frozenset({"read", "read_file", "read_many_files", "write", "write_file", "edit", "multi_edit",
                        "notebook_edit", "str_replace", "apply_patch", "applypatch", "filechange", "file_change",
                        "fileread", "filewrite", "grep", "search_file_content", "glob",
                        "list_dir", "ls", "view", "python", "run_python"})
SHELL_KEYS = ("command", "cmd", "script", "code", "shell_command")
FILE_KEYS = ("path", "file_path", "filePath", "notebook_path", "target_file", "glob", "pattern", "query")

PATH_RE = re.compile(
    r"(?:~|\$HOME|\$\{HOME\}|\$PARHELION_HOME|\$\{PARHELION_HOME\}|\$OPSB_HOME|\$\{OPSB_HOME\})?"
    r"/[^\s'\";|&()<>`,]+")
TEAM_RE = re.compile(r"\b_all_team_sessions\b")
CRED_BARE_RE = re.compile(
    r"cj_accounts|\.cache_remote_judge|\bJUDGE_SESSION_FILE\b|~/\.judge(?:/|\b)|\$HOME/\.judge(?:/|\b)"
    r"|/\.judge/(?:session|sessions)", re.I)
STATE_BARE_RE = re.compile(r"\bagent_bus\.db\b|/\.parhelion/(?:workspaces/[^/\s]+/state|hardware|cache)/", re.I)
IMPORT_RE = re.compile(r"\b(?:from|import)\s+opsb_core\b|\bopsb_core\.(?:cli_app|drivers|mcp|services|scheduler)\b"
                       r"|\bfrom\s+scheduler\b|\bimport\s+scheduler\b")
SYS_PATH_RE = re.compile(r"sys\.path", re.I)
SYS_PATH_ROOT_RE = re.compile(r"opsb|/cli\b|['\"]cli['\"]|parhelion", re.I)


def _under(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _resolved(raw: str, *, parhelion: Path, home: Path) -> Path | None:
    """Absolute form of one path token, or None for a relative one.

    ``$PARHELION_HOME``-style references are expanded from the *platform* home rather than the
    current environment, and a relative token is never resolved against the process working
    directory: a scan must not turn ``$PARHELION_HOME/state/...`` into a path under the
    platform checkout it happens to run in.
    """
    text = str(raw or "")
    for token, value in (("${PARHELION_HOME}", parhelion), ("$PARHELION_HOME", parhelion),
                        ("${OPSB_HOME}", parhelion), ("$OPSB_HOME", parhelion),
                        ("${HOME}", home), ("$HOME", home), ("~", home)):
        if text.startswith(token):
            text = str(value) + text[len(token):]
            break
    if not text.startswith("/"):
        return None
    try:
        return Path(os.path.normpath(text))
    except (OSError, ValueError):
        return None


def short_tool(name: Any) -> str:
    text = str(name or "").strip()
    if "__" in text:
        text = text.split("__")[-1]
    return text.removeprefix("parhelion_")


def _text_of(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(_text_of(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return " ".join(_text_of(v) for v in value)
    return value if isinstance(value, str) else ""


def tool_text(name: Any, args: Any) -> str:
    """The part of a tool call that can reach the filesystem, or ``""`` for a platform tool."""
    short = short_tool(name)
    if short in SHELL_TOOLS:
        keys = SHELL_KEYS
    elif short in FILE_TOOLS:
        keys = FILE_KEYS
    else:
        return ""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return args if short in SHELL_TOOLS else ""
    if not isinstance(args, dict):
        return ""
    return "\n".join(_text_of(args[key]) for key in keys if key in args)


def _category_of_path(path: Path, ph: Path, home: Path) -> str:
    if path.name == "opsb":  # the CLI launcher a worker is told to fall back to, not platform source
        return ""
    if path.name == "cj_accounts.json" or ".cache_remote_judge" in path.name:
        return "judge_credentials"
    if _under(path, home / ".judge") or _under(path, ph / "sessions"):
        return "judge_credentials"
    if path.name == "agent_bus.db":
        return "platform_state"
    parts = path.parts
    if "workspaces" in parts and _under(path, ph / "workspaces"):
        after = parts[parts.index("workspaces"):]
        if len(after) > 2 and after[2] == "state":
            return "platform_state"
    if _under(path, ph / "hardware") or _under(path, ph / "cache"):
        return "platform_state"
    if any(_under(path, root) for root in platform_source_dirs()):
        return "platform_import"
    return ""


def _own_spill(path: Path, agent_id: str | None) -> bool:
    """``<state>/spill/<agent_id>/...``: the worker's own spilled tool results (4.2 E), handed to it by path."""
    if not agent_id:
        return False
    parts = path.parts
    return any(parts[i:i + 3] == ("state", "spill", agent_id) for i in range(len(parts) - 2))


def classify_text(text: Any, *, worktree: Path | str | None = None, parhelion: Path | str | None = None,
                  home: Path | str | None = None, agent_id: str | None = None) -> list[str]:
    """Boundary classes matched by one tool input, most severe first.

    ``agent_id`` (4.4.4) makes the worker's own spill directory legal like its worktree: the MCP output
    bound writes a large reply there and gives the worker the path.
    """
    body = str(text or "")
    if not body.strip():
        return []
    ph = Path(parhelion).resolve() if parhelion is not None else parhelion_home()
    user_home = Path(home).expanduser().resolve() if home is not None else Path.home()
    wt = Path(worktree).expanduser().resolve() if worktree else None
    found: set[str] = set()
    residue = body
    if TEAM_RE.search(body):
        found.add("team_sessions")
    for raw in PATH_RE.findall(body):
        resolved = _resolved(raw, parhelion=ph, home=user_home)
        if resolved is None:
            continue
        if (wt is not None and _under(resolved, wt)) or _own_spill(resolved, agent_id):
            residue = residue.replace(raw, " ")  # the worker's own tree or spill: legal, not a bare token either
            continue
        category = _category_of_path(resolved, ph, user_home)
        if category:
            found.add(category)
    if CRED_BARE_RE.search(residue):
        found.add("judge_credentials")
    if STATE_BARE_RE.search(residue):
        found.add("platform_state")
    if IMPORT_RE.search(body) or (SYS_PATH_RE.search(body) and SYS_PATH_ROOT_RE.search(body)):
        found.add("platform_import")
    return [c for c in CATEGORY_ORDER if c in found]


def classify_tool(name: Any, args: Any, *, worktree: Path | str | None = None,
                  parhelion: Path | str | None = None, home: Path | str | None = None,
                  agent_id: str | None = None) -> list[str]:
    """Boundary classes of one tool call; empty for a platform tool or a worktree-local read."""
    return classify_text(tool_text(name, args), worktree=worktree, parhelion=parhelion, home=home,
                         agent_id=agent_id)


def classify_acp_update(update: Any, *, worktree: Path | str | None = None,
                        parhelion: Path | str | None = None, home: Path | str | None = None,
                        agent_id: str | None = None) -> list[str]:
    """One ACP ``session/update`` row: ``tool_call``/``tool_call_update`` with ``rawInput``."""
    if not isinstance(update, dict):
        return []
    if str(update.get("sessionUpdate") or "") not in ("tool_call", "tool_call_update"):
        return []
    name = update.get("title") or update.get("name") or update.get("toolName") or ""
    raw = update.get("rawInput")
    return classify_tool(name, raw, worktree=worktree, parhelion=parhelion, home=home, agent_id=agent_id)


def classify_dsh_event(event: Any, *, worktree: Path | str | None = None,
                       parhelion: Path | str | None = None, home: Path | str | None = None,
                       agent_id: str | None = None) -> list[str]:
    """One DSH native session event: ``tool/call`` with ``data.name``/``data.arguments``."""
    if not isinstance(event, dict) or str(event.get("type") or "") != "tool/call":
        return []
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    return classify_tool(data.get("name"), data.get("arguments"), worktree=worktree,
                         parhelion=parhelion, home=home, agent_id=agent_id)


def _alert_body(worker: str, category: str, ts: str) -> str:
    return (f"[Worker boundary] {worker} 触到平台边界（类别 {category}：{CATEGORY_LABELS[category]}，"
            f"时间 {ts[:19]}Z）。平台数据请走 parhelion_report 的 board/history/per_case/env 视图；"
            f"是否停止该 worker 由管理员决定，平台不自动停止。")


def mark(store: Any, agent_id: str, categories: Iterable[str]) -> list[str]:
    """Union ``categories`` into the worker's hot-mesh boundary flag and return the new list."""
    record = store.get_agent(agent_id) or {}
    flags = flags_of(record)
    for category in categories:
        if category not in flags:
            flags.append(category)
    flags = [c for c in CATEGORY_ORDER if c in flags]
    if flags != flags_of(record):
        try:
            store.update_agent(agent_id, boundary_flags=json.dumps(flags))
        except Exception:
            return flags_of(record)
    return flags


def flags_of(record: dict[str, Any] | None) -> list[str]:
    """Hot-mesh boundary classes of one agent row (older rows have no flag)."""
    raw = (record or {}).get("boundary_flags")
    if isinstance(raw, list):
        values = [str(v) for v in raw]
    else:
        try:
            values = [str(v) for v in json.loads(str(raw or "[]"))]
        except ValueError:
            values = []
    return [c for c in CATEGORY_ORDER if c in values]


def report(store: Any, workspace: Path | str, agent_id: str, categories: Any, *,
           problem: str = "", ts: str = "") -> dict[str, Any]:
    """Alert both the orchestrator and the administrator, write the ledger row, mark the page.

    Idempotent per ``(agent_id, category)``: the bus rows carry ``event_key`` and the ledger row
    its ``ekey``, so a repeated hit (or a replayed one) writes nothing new.
    """
    wanted = [categories] if isinstance(categories, str) else list(categories or [])
    cats = [c for c in CATEGORY_ORDER if c in {str(c) for c in wanted}]
    if not cats:
        return {"ok": False, "reason": "unknown category", "categories": []}
    record = store.get_agent(agent_id) or {}
    title = str(problem or record.get("problem") or "")
    stamp = str(ts or _cfg.utc_now().isoformat())
    out: dict[str, Any] = {"ok": True, "agent_id": agent_id, "categories": cats, "alerts": [], "ledger": []}
    for category in cats:
        body = _alert_body(agent_id, category, stamp)
        for direction, recipient, key in (("worker_to_orch", agent_id, f"boundary:{agent_id}:{category}"),
                                          ("system", "admin", f"boundary:admin:{agent_id}:{category}")):
            try:
                msg = store.append_message(recipient, direction, "platform", body, priority="strategic",
                                           problem=title, event_key=key)
            except Exception:
                msg = {}
            if msg:
                out["alerts"].append({"direction": direction, "seq": msg.get("seq"), "category": category})
        if title:
            try:
                from .ledger.store import Ledger

                row = Ledger(workspace, title, agent_id).append(
                    "constraint", {"claim": f"worker boundary: {category}",
                                   "text": f"{agent_id} reached the {CATEGORY_LABELS[category]} at {stamp[:19]}Z; "
                                           "platform data goes through parhelion_report (board/history/per_case/env)",
                                   "rule_source": "4.2 K worker boundary (P15)"},
                    source="platform", ekey=f"boundary:{category}")
                if not row.get("duplicate"):
                    out["ledger"].append(row.get("id"))
            except Exception:
                pass
    out["flags"] = mark(store, agent_id, cats)
    return out
