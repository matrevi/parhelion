"""Antigravity Harness Driver (`antigravity://` Desktop GUI, `agy acp` stdio, WAL+flock observer)."""
from __future__ import annotations

import json
import os
import select
import subprocess
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import IO, Any

from .. import config as _cfg
from .base import (
    BaseDriver,
    DriverDiagnostic,
    build_parhelion_mcp_command,
    compact_prompt_for_url,
    ensure_workspace_skills,
    inspect_macos_app_bundle,
    is_flock_held,
    open_sqlite_readonly_wal,
    probe_binary_version,
    resolve_parhelion_home,
    resolve_session_logs_dir,
    resolve_workspace_driver_dir,
    stage_brief_and_clipboard,
    which_binary,
    write_mcp_json_file,
)


class AcpSessionClient:
    """Native ACP JSON-RPC 2.0 client over line-delimited stdio."""

    def __init__(
        self,
        stdin_stream: IO[str],
        stdout_stream: IO[str],
        log_stream: IO[str] | None = None,
        quiet_window_sec: float = 0.25,
    ) -> None:
        self._stdin = stdin_stream
        self._stdout = stdout_stream
        self._log = log_stream
        self.quiet_window_sec = quiet_window_sec
        self._next_id = 1
        self.notifications: list[dict[str, Any]] = []
        self.open_tool_calls: set[str] = set()

    def _log_line(self, direction: str, payload: Any) -> None:
        if self._log is None:
            return
        row = {"ts": _cfg.utc_now().isoformat(), "dir": direction, "msg": payload}
        self._log.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._log.flush()

    def _write_msg(self, msg: dict[str, Any]) -> None:
        line = json.dumps(msg, ensure_ascii=False, separators=(",", ":"))
        self._log_line("out", msg)
        self._stdin.write(line + "\n")
        self._stdin.flush()

    def _track_notification(self, msg: dict[str, Any]) -> None:
        self.notifications.append(msg)
        if msg.get("method") != "session/update":
            return
        params = msg.get("params") or {}
        update = params.get("update") if isinstance(params.get("update"), dict) else params
        if not isinstance(update, dict):
            return
        u_type = str(update.get("sessionUpdate") or update.get("type") or "")
        tc_id = str(update.get("toolCallId") or update.get("id") or "")
        status = str(update.get("status") or "").lower()
        if u_type == "tool_call" and tc_id and status in ("", "pending", "in_progress", "running"):
            self.open_tool_calls.add(tc_id)
        elif u_type in ("tool_call_update", "tool_call") and tc_id:
            if status in ("completed", "failed", "error", "cancelled", "done"):
                self.open_tool_calls.discard(tc_id)
            elif status in ("pending", "in_progress", "running"):
                self.open_tool_calls.add(tc_id)

    def _read_until_id(self, target_id: int) -> dict[str, Any]:
        while True:
            raw = self._stdout.readline()
            if raw == "":
                raise RuntimeError(f"ACP stdout closed before response id={target_id}")
            s = raw.strip()
            if not s:
                continue
            try:
                msg = json.loads(s)
            except json.JSONDecodeError:
                continue
            self._log_line("in", msg)
            if isinstance(msg, dict) and msg.get("id") == target_id:
                return msg
            if isinstance(msg, dict) and "method" in msg:
                self._track_notification(msg)

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        req_id = self._next_id
        self._next_id += 1
        self._write_msg({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        return self._read_until_id(req_id)

    def initialize(self) -> dict[str, Any]:
        resp = self.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True}},
                "clientInfo": {"name": "parhelion", "version": "3.0.0"},
            },
        )
        if "error" in resp:
            raise RuntimeError(f"ACP initialize failed: {resp['error']}")
        return resp.get("result") or {}

    def open_session(
        self,
        cwd: Path,
        mcp_servers: list[dict[str, Any]],
        system_append: str = "",
        resume_id: str | None = None,
    ) -> tuple[str, bool]:
        """Open ACP session via `session/load` (checking `replayedPrompts > 0`) or `session/new`."""
        meta_block: dict[str, Any] = {}
        if system_append:
            meta_block = {"antigravity": {"systemPrompt": {"append": system_append}}}

        if resume_id:
            load_params: dict[str, Any] = {
                "sessionId": resume_id,
                "cwd": str(cwd),
                "mcpServers": mcp_servers,
            }
            if meta_block:
                load_params["_meta"] = meta_block
            resp = self.request("session/load", load_params)
            res = resp.get("result") if isinstance(resp.get("result"), dict) else None
            replayed = int((res or {}).get("replayedPrompts") or 0)
            if "error" not in resp and res is not None and replayed > 0:
                return str(res.get("sessionId") or resume_id), True

        new_params: dict[str, Any] = {
            "cwd": str(cwd),
            "mcpServers": mcp_servers,
        }
        if meta_block:
            new_params["_meta"] = meta_block
        resp = self.request("session/new", new_params)
        if "error" in resp:
            raise RuntimeError(f"ACP session/new failed: {resp['error']}")
        res = resp.get("result") or {}
        return str(res.get("sessionId") or ""), False

    def drain_quiet_window(self) -> None:
        """Wait bounded quiet window if async tool calls are still marked open."""
        if not self.open_tool_calls or self.quiet_window_sec <= 0:
            return
        try:
            fd = self._stdout.fileno()
        except Exception:
            return
        deadline = time.monotonic() + self.quiet_window_sec
        while self.open_tool_calls and time.monotonic() < deadline:
            rem = max(0.01, deadline - time.monotonic())
            ready, _, _ = select.select([fd], [], [], rem)
            if not ready:
                break
            raw = self._stdout.readline()
            if raw == "":
                break
            s = raw.strip()
            if not s:
                continue
            try:
                msg = json.loads(s)
            except json.JSONDecodeError:
                continue
            self._log_line("in", msg)
            if isinstance(msg, dict) and "method" in msg:
                self._track_notification(msg)

    def send_prompt(self, session_id: str, prompt_text: str) -> dict[str, Any]:
        resp = self.request(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": prompt_text}],
            },
        )
        self.drain_quiet_window()
        if "error" in resp:
            raise RuntimeError(f"ACP session/prompt failed: {resp['error']}")
        return resp.get("result") or {}


def build_acp_mcp_servers(workspace: Path, mcp_url: str | None = None) -> list[dict[str, Any]]:
    if mcp_url:
        return [{"name": "parhelion", "type": "http", "url": mcp_url}]
    cmd, args, env = build_parhelion_mcp_command(workspace)
    return [
        {
            "name": "parhelion",
            "command": cmd,
            "args": args,
            "env": [{"name": k, "value": v} for k, v in env.items()],
        }
    ]


def _resolve_gemini_home(gemini_home: Path | None = None) -> Path:
    if gemini_home is not None:
        return gemini_home.expanduser()
    env_home = os.environ.get("GEMINI_HOME")
    if env_home:
        return Path(env_home).expanduser()
    return Path.home() / ".gemini"


def _uri_to_realpath(uri_or_path: str) -> str:
    s = str(uri_or_path or "").strip()
    if not s:
        return ""
    if s.startswith("file://"):
        parsed = urllib.parse.urlparse(s)
        raw_path = urllib.parse.unquote(parsed.path)
    else:
        raw_path = s
    try:
        return os.path.realpath(raw_path)
    except OSError:
        return raw_path


def _extract_project_paths(proj_doc: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    res_block = proj_doc.get("projectResources")
    resources = res_block.get("resources") if isinstance(res_block, dict) else []
    for item in resources or []:
        if not isinstance(item, dict):
            continue
        if item.get("folderUri"):
            paths.append(_uri_to_realpath(str(item["folderUri"])))
        gf = item.get("gitFolder")
        if isinstance(gf, dict) and gf.get("folderUri"):
            paths.append(_uri_to_realpath(str(gf["folderUri"])))
    return [p for p in paths if p]


def _read_json_tolerant(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _tail_jsonl(path: Path, max_lines: int = 5) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 16384), os.SEEK_SET)
            chunk = fh.read().decode("utf-8", errors="replace")
        rows: list[dict[str, Any]] = []
        for ln in chunk.splitlines()[-max_lines:]:
            s = ln.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    rows.append(obj)
            except Exception:
                continue
        return rows
    except Exception:
        return []


class AntigravityDriver(BaseDriver):
    """Unified Antigravity driver (`antigravity://` Desktop GUI + `agy acp` stdio)."""

    kind = "antigravity"
    display_name = "Antigravity"
    protocol = "acp-stdio"
    gui_apps = ("Antigravity",)

    def _gui_candidates(self) -> list[Path]:
        home = Path.home()
        return [
            Path("/Applications/Antigravity.app"),
            home / "Applications" / "Antigravity.app",
        ]

    def discover_gui(self) -> dict[str, Any]:
        return inspect_macos_app_bundle(self._gui_candidates(), expected_bundle_ids={"com.google.antigravity"})

    def _find_binary(self) -> str:
        found = which_binary(("agy", "antigravity"))
        if found:
            return found
        home = Path.home()
        for cand in (
            home / ".local" / "bin" / "agy",
            home / ".antigravity" / "bin" / "agy",
            Path("/Applications/Antigravity.app/Contents/Resources/app/bin/agy"),
        ):
            if cand.is_file() and os.access(cand, os.X_OK):
                return str(cand)
        return ""

    def discover(self) -> DriverDiagnostic:
        gui = self.discover_gui()
        bin_path = self._find_binary()
        installed = bool(bin_path) or bool(gui["gui_installed"])
        version = probe_binary_version(bin_path) if bin_path else ""
        home = Path.home()
        g_home = _resolve_gemini_home()
        auth_markers = (
            g_home / "antigravity",
            home / ".antigravity",
            home / ".config" / "Antigravity",
        )
        has_env_auth = bool(os.environ.get("ANTIGRAVITY_API_KEY") or os.environ.get("GEMINI_API_KEY"))
        auth_ok = installed and (has_env_auth or any(p.exists() for p in auth_markers))
        if not installed:
            detail = "agy / antigravity binary and Antigravity.app not found"
        elif gui["gui_installed"] and not bin_path:
            detail = "ready (Antigravity GUI; agy CLI not on PATH)"
        elif gui["gui_installed"] and bin_path:
            detail = "ready (Antigravity GUI + agy acp)"
        elif not auth_ok:
            detail = "installed; local Antigravity auth state directory not detected"
        else:
            detail = "ready (agy acp stdio)"
        return DriverDiagnostic(
            kind=self.kind,
            display_name=self.display_name,
            installed=installed,
            binary_path=bin_path,
            version=version,
            auth_ok=auth_ok,
            protocol=self.protocol,
            detail=detail,
            gui_installed=bool(gui["gui_installed"]),
            gui_app_path=str(gui["gui_app_path"]),
            gui_bundle_id=str(gui["gui_bundle_id"]),
            gui_version=str(gui["gui_version"]),
            url_scheme=str(gui["url_scheme"]),
        )

    def _register_project_idempotent(self, real_ws: Path, g_home: Path) -> tuple[str, Path, bool]:
        projects_dir = g_home / "config" / "projects"
        projects_dir.mkdir(parents=True, exist_ok=True)
        real_str = str(real_ws)
        folder_uri = real_ws.as_uri()

        for proj_file in sorted(projects_dir.glob("*.json")):
            doc = _read_json_tolerant(proj_file)
            if not doc:
                continue
            if real_str in _extract_project_paths(doc):
                proj_id = str(doc.get("id") or proj_file.stem)
                return proj_id, proj_file, False

        proj_id = str(uuid.uuid4())
        proj_file = projects_dir / f"{proj_id}.json"
        if (real_ws / ".git").exists():
            resource: dict[str, Any] = {"gitFolder": {"folderUri": folder_uri, "defaultBranch": "main"}}
        else:
            resource = {"folderUri": folder_uri}
        payload = {
            "id": proj_id,
            "name": real_ws.name,
            "projectResources": {"resources": [resource]},
            "permissionGrants": {"v2Migrated": True},
            "settings": {},
            "isWorkspaceOnly": False,
        }
        tmp = proj_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o644)
        os.replace(tmp, proj_file)
        return proj_id, proj_file, True

    def sync_workspace(
        self,
        workspace: Path,
        mcp_url: str | None = None,
        *,
        gemini_home: Path | None = None,
        register_global_mcp: bool = False,
    ) -> dict[str, Any]:
        real_ws = Path(os.path.realpath(workspace.expanduser()))
        skills_res = ensure_workspace_skills(real_ws)

        agents_md = real_ws / "AGENTS.md"
        if not agents_md.exists():
            agents_md.write_text(f"# {real_ws.name}\n\nManaged by Parhelion.\n", encoding="utf-8")

        g_home = _resolve_gemini_home(gemini_home)
        project_id, project_file, created_new = self._register_project_idempotent(real_ws, g_home)

        # Workspace-scoped Antigravity plugin MCP layout: `<ws>/.agents/plugins/parhelion/`
        plugin_dir = real_ws / ".agents" / "plugins" / "parhelion"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        plugin_json = plugin_dir / "plugin.json"
        tmp_pj = plugin_json.with_suffix(".tmp")
        tmp_pj.write_text(json.dumps({"name": "parhelion"}, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp_pj, plugin_json)

        plugin_mcp = plugin_dir / "mcp_config.json"
        write_mcp_json_file(plugin_mcp, real_ws, mcp_url=mcp_url)

        if register_global_mcp:
            global_mcp_path = g_home / "config" / "mcp_config.json"
            global_mcp_path.parent.mkdir(parents=True, exist_ok=True)
            existing_global = _read_json_tolerant(global_mcp_path)
            servers = existing_global.get("mcpServers")
            if not isinstance(servers, dict):
                servers = {}
            if mcp_url:
                servers["parhelion"] = {"url": mcp_url}
            else:
                cmd, args, env = build_parhelion_mcp_command(real_ws)
                servers["parhelion"] = {"command": cmd, "args": args, "env": env}
            existing_global["mcpServers"] = servers
            tmp_gm = global_mcp_path.with_suffix(".tmp")
            tmp_gm.write_text(json.dumps(existing_global, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(tmp_gm, global_mcp_path)

        # Keep isolated driver dir updated for headless `agy acp`
        drv_dir = resolve_workspace_driver_dir(real_ws, self.kind)
        drv_dir.mkdir(parents=True, exist_ok=True)
        mcp_path = drv_dir / "mcp_config.json"
        write_mcp_json_file(mcp_path, real_ws, mcp_url=mcp_url)
        acp_servers = build_acp_mcp_servers(real_ws, mcp_url=mcp_url)
        ws_meta = {
            "workspace": str(real_ws),
            "project_id": project_id,
            "driver": self.kind,
            "protocol": self.protocol,
            "skills_dir": skills_res["skills_dir"],
            "mcpServers": acp_servers,
            "updated_at": _cfg.utc_now().isoformat(),
        }
        meta_path = drv_dir / "workspace.json"
        meta_path.write_text(json.dumps(ws_meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        return {
            "ok": True,
            "driver": self.kind,
            "workspace": str(real_ws),
            "project_id": project_id,
            "project_file": str(project_file),
            "created_new_project": created_new,
            "plugin_dir": str(plugin_dir),
            "plugin_mcp_config": str(plugin_mcp),
            "skills_dir": skills_res["skills_dir"],
            "projected_skills": skills_res["projected"],
            "mcp_config_path": str(mcp_path),
            "workspace_meta_path": str(meta_path),
            "acp_mcp_servers": acp_servers,
        }

    def build_launch_command(
        self,
        worktree: Path,
        prompt: str,
        session_id: str,
        resume_id: str | None = None,
        mcp_config_path: Path | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        bin_path = self._find_binary() or "agy"
        cmd = [bin_path, "acp"]
        env: dict[str, str] = {
            "PARHELION_HOME": str(resolve_parhelion_home()),
            "PARHELION_SESSION_ID": session_id,
            "PARHELION_WORKTREE": str(worktree.expanduser().resolve()),
        }
        if resume_id:
            env["PARHELION_RESUME_ID"] = resume_id
        if mcp_config_path:
            env["PARHELION_MCP_CONFIG"] = str(mcp_config_path)
        return cmd, env

    def build_gui_deep_link(
        self,
        worktree: Path,
        prompt: str = "",
        project_id: str | None = None,
        thread_id: str | None = None,
    ) -> str:
        params: list[tuple[str, str]] = []
        if project_id:
            params.append(("project", project_id))
        if prompt:
            params.append(("prompt", prompt))
        if not params:
            return "antigravity://open"
        qs = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        return f"antigravity://open?{qs}"

    def launch_session(
        self,
        workspace: Path,
        problem: str,
        worktree: Path,
        brief: str,
        mode: str = "background",
        resume_id: str | None = None,
        dry_run: bool = False,
        preseed_thread: bool = False,
    ) -> Any:
        if mode != "gui":
            return super().launch_session(
                workspace=workspace,
                problem=problem,
                worktree=worktree,
                brief=brief,
                mode=mode,
                resume_id=resume_id,
                dry_run=dry_run,
                preseed_thread=preseed_thread,
            )

        from .base import SessionRecord
        from .session_store import save_session

        ws = Path(os.path.realpath(workspace.expanduser()))
        wt = Path(os.path.realpath(worktree.expanduser()))
        sync_res = self.sync_workspace(ws)
        project_id = str(sync_res.get("project_id") or "")

        # Always persist `<worktree>/.parhelion/brief.md` + clipboard (when not dry-run) as Mendel deep-link fallback
        brief_path, copied = stage_brief_and_clipboard(wt, brief, copy_clipboard=(not dry_run))

        base_url = self.build_gui_deep_link(wt, prompt="", project_id=project_id or None)
        compact_prompt = compact_prompt_for_url(brief, brief_path, len(base_url), max_url_len=6000)
        url = self.build_gui_deep_link(wt, prompt=compact_prompt, project_id=project_id or None)

        session_id = self._new_session_id()
        native_id = resume_id or session_id
        now_iso = _cfg.utc_now().isoformat()
        cmd = ["open", url]

        diag = self.discover()
        if dry_run:
            status = "idle"
        else:
            if not diag.gui_installed and not diag.installed:
                raise RuntimeError(f"Antigravity GUI / agy CLI not installed ({diag.detail})")
            proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False)
            if proc.returncode != 0:
                if diag.gui_app_path:
                    subprocess.run(["open", "-a", diag.gui_app_path], check=True)
                else:
                    raise RuntimeError(f"Failed to launch Antigravity GUI URL: {(proc.stderr or proc.stdout).strip()}")
            status = "running"

        record = SessionRecord(
            session_id=session_id,
            driver=self.kind,
            workspace=str(ws),
            problem=problem,
            worktree=str(wt),
            native_session_id=native_id,
            status=status,
            pid=None,
            created_at=now_iso,
            updated_at=now_iso,
            brief_preview=brief.strip().replace("\n", " ")[:180],
            launch_mode="gui",
            deep_link=url,
            project_id=project_id,
            brief_path=str(brief_path),
        )
        save_session(ws, record)
        self.last_launch_meta = {
            "mode": "gui",
            "launch_mode": "gui",
            "dry_run": dry_run,
            "command": cmd,
            "env": {},
            "mcp_config_path": sync_res.get("plugin_mcp_config") or sync_res.get("mcp_config_path"),
            "log_path": str(resolve_session_logs_dir() / f"{session_id}.jsonl"),
            "worktree": str(wt),
            "deep_link": url,
            "project_id": project_id,
            "brief_path": str(brief_path),
            "clipboard_copied": copied,
            "sync": sync_res,
        }
        return record

    def observe_gui_sessions(
        self,
        workspace: Path | None = None,
        limit: int = 20,
        *,
        gemini_home: Path | None = None,
    ) -> list[dict[str, Any]]:
        g_home = _resolve_gemini_home(gemini_home)
        db_path = g_home / "antigravity" / "conversation_summaries.db"
        if not db_path.is_file():
            return []

        projects_map: dict[str, dict[str, Any]] = {}
        projects_dir = g_home / "config" / "projects"
        if projects_dir.is_dir():
            for pf in projects_dir.glob("*.json"):
                doc = _read_json_tolerant(pf)
                if doc:
                    pid = str(doc.get("id") or pf.stem)
                    projects_map[pid] = {"name": doc.get("name", ""), "paths": _extract_project_paths(doc)}

        real_ws: Path | None = Path(os.path.realpath(workspace.expanduser())) if workspace is not None else None
        out: list[dict[str, Any]] = []
        conn = open_sqlite_readonly_wal(db_path)
        try:
            col_names = {str(r[1]) for r in conn.execute("PRAGMA table_info(conversation_summaries);").fetchall()}
            if not col_names:
                return []
            sel = [
                "conversation_id" if "conversation_id" in col_names else "''",
                "title" if "title" in col_names else "''",
                "preview" if "preview" in col_names else "''",
                "step_count" if "step_count" in col_names else "0",
                "status" if "status" in col_names else "''",
                "project_id" if "project_id" in col_names else "''",
                "workspace_uris" if "workspace_uris" in col_names else "'[]'",
                "not_fully_idle" if "not_fully_idle" in col_names else "0",
                "killed" if "killed" in col_names else "0",
                "last_modified_time" if "last_modified_time" in col_names else "''",
                "last_user_input_time" if "last_user_input_time" in col_names else "''",
                "parent_conversation_id" if "parent_conversation_id" in col_names else "''",
                "agent_name" if "agent_name" in col_names else "''",
            ]
            order_col = "last_modified_time" if "last_modified_time" in col_names else "rowid"
            rows = conn.execute(
                f"SELECT {', '.join(sel)} FROM conversation_summaries ORDER BY {order_col} DESC LIMIT ?",
                (max(1, int(limit)) * 3 if real_ws else max(1, int(limit)),),
            ).fetchall()
        finally:
            conn.close()

        for row in rows:
            (
                cid,
                title,
                preview,
                step_count,
                raw_status,
                proj_id,
                ws_uris_raw,
                not_fully_idle,
                killed,
                last_mod,
                last_input,
                parent_cid,
                agent_name,
            ) = row
            cid_s = str(cid or "")
            proj_id_s = str(proj_id or "")
            uri_paths: list[str] = []
            try:
                parsed_uris = json.loads(ws_uris_raw) if isinstance(ws_uris_raw, str) and ws_uris_raw.strip() else []
                if isinstance(parsed_uris, list):
                    uri_paths = [_uri_to_realpath(str(u)) for u in parsed_uris if u]
            except Exception:
                uri_paths = []
            proj_paths = projects_map.get(proj_id_s, {}).get("paths", [])
            all_paths = [p for p in [*uri_paths, *proj_paths] if p]

            if real_ws is not None:
                matched = False
                for cp in all_paths:
                    try:
                        p_obj = Path(cp)
                        if p_obj == real_ws or real_ws in p_obj.parents:
                            matched = True
                            break
                    except OSError:
                        continue
                if not matched:
                    continue

            lock_path = g_home / "antigravity" / "presence" / f"{cid_s}.lock"
            lock_held = is_flock_held(lock_path)

            if bool(killed):
                norm_status = "failed"
            elif str(raw_status or "") == "CASCADE_RUN_STATUS_RUNNING" or (lock_held and bool(not_fully_idle)):
                norm_status = "running"
            else:
                norm_status = "idle"

            transcript_path = g_home / "antigravity" / "brain" / cid_s / ".system_generated" / "logs" / "transcript.jsonl"
            tail_rows = _tail_jsonl(transcript_path, max_lines=5)
            last_tool = ""
            for tr in reversed(tail_rows):
                tcs = tr.get("tool_calls") or []
                if isinstance(tcs, list) and tcs and isinstance(tcs[-1], dict):
                    last_tool = str(tcs[-1].get("name") or "")
                    break

            worktree_str = uri_paths[0] if uri_paths else (proj_paths[0] if proj_paths else "")
            problem_name = Path(worktree_str).name if worktree_str else ""
            if real_ws is not None and worktree_str:
                try:
                    rel_parts = Path(worktree_str).relative_to(real_ws).parts
                    if len(rel_parts) >= 2 and rel_parts[0] == "bench":
                        problem_name = rel_parts[1]
                    elif rel_parts and rel_parts[0] not in (".worktrees",):
                        problem_name = rel_parts[0]
                except Exception:
                    pass

            out.append(
                {
                    "session_id": f"agy-gui-{cid_s[:12]}",
                    "driver": self.kind,
                    "workspace": str(real_ws) if real_ws else worktree_str,
                    "problem": problem_name,
                    "worktree": worktree_str,
                    "native_session_id": cid_s,
                    "status": norm_status,
                    "pid": None,
                    "created_at": str(last_input or last_mod or ""),
                    "updated_at": str(last_mod or ""),
                    "brief_preview": str(title or preview or "")[:180],
                    "launch_mode": "gui",
                    "deep_link": f"antigravity://open?project={urllib.parse.quote(proj_id_s, safe='')}" if proj_id_s else "antigravity://open",
                    "project_id": proj_id_s,
                    "brief_path": "",
                    "observed_steps": int(step_count or 0),
                    "tokens_used": 0,
                    "killed": bool(killed),
                    "presence_lock_held": lock_held,
                    "agent_name": str(agent_name or ""),
                    "parent_conversation_id": str(parent_cid or ""),
                    "last_tool": last_tool,
                    "source": "gui-observer",
                }
            )
            if len(out) >= limit:
                break
        return out

    def _execute_headless_turn(
        self,
        workspace: Path,
        worktree: Path,
        brief: str,
        session_id: str,
        native_session_id: str,
        resume_id: str | None,
        cmd: list[str],
        env: dict[str, str],
        log_path: Path,
    ) -> int:
        from .session_store import update_session

        log_path.parent.mkdir(parents=True, exist_ok=True)
        merged_env = {**os.environ, **env}
        mcp_servers = build_acp_mcp_servers(workspace)
        with log_path.open("a", encoding="utf-8") as lf:
            proc = subprocess.Popen(
                cmd,
                cwd=str(worktree),
                env=merged_env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            assert proc.stdin is not None and proc.stdout is not None
            client = AcpSessionClient(proc.stdin, proc.stdout, log_stream=lf)
            try:
                client.initialize()
                active_id, _ = client.open_session(
                    cwd=worktree,
                    mcp_servers=mcp_servers,
                    resume_id=resume_id,
                )
                if active_id:
                    update_session(workspace, session_id, native_session_id=active_id)
                client.send_prompt(active_id or native_session_id, brief)
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
                rc = proc.wait(timeout=10)
                if proc.stderr is not None:
                    err_txt = proc.stderr.read()
                    if err_txt:
                        lf.write(json.dumps({"event": "stderr", "text": err_txt}) + "\n")
            return int(rc)
