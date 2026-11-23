"""Antigravity Harness Driver (`antigravity://` Desktop GUI, `agy acp` stdio, WAL+flock observer)."""
from __future__ import annotations

import json
import os
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

from .. import config as _cfg
from .antigravity_acp import AcpSessionClient, build_acp_mcp_servers, run_acp_headless_turn
from .base import (
    BaseDriver,
    DriverDiagnostic,
    SessionRecord,
    probe_binary_version,
    resolve_parhelion_home,
    resolve_workspace_driver_dir,
    which_binary,
)
from .macos_gui import (
    build_parhelion_mcp_command,
    compact_prompt_for_url,
    ensure_workspace_skills,
    infer_problem_from_worktree,
    inspect_macos_app_bundle,
    is_flock_held,
    launch_macos_deep_link,
    open_sqlite_readonly_wal,
    stage_brief_and_clipboard,
    write_mcp_json_file,
)


def _resolve_gemini_home(gemini_home: Path | None = None) -> Path:
    if gemini_home is not None:
        return gemini_home.expanduser()
    env_home = os.environ.get("GEMINI_HOME")
    return Path(env_home).expanduser() if env_home else (Path.home() / ".gemini")


def _uri_to_realpath(uri_or_path: str) -> str:
    s = str(uri_or_path or "").strip()
    if not s:
        return ""
    raw_path = urllib.parse.unquote(urllib.parse.urlparse(s).path) if s.startswith("file://") else s
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
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _tail_jsonl(path: Path, max_lines: int = 5) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 16384), os.SEEK_SET)
            chunk = fh.read().decode("utf-8", errors="replace")
        rows: list[dict[str, Any]] = []
        for ln in chunk.splitlines()[-max_lines:]:
            if not (s := ln.strip()):
                continue
            try:
                if isinstance(obj := json.loads(s), dict):
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
        return [Path("/Applications/Antigravity.app"), Path.home() / "Applications" / "Antigravity.app"]

    def discover_gui(self) -> dict[str, Any]:
        return inspect_macos_app_bundle(self._gui_candidates(), expected_bundle_ids={"com.google.antigravity"})

    def _find_binary(self) -> str:
        if found := which_binary(("agy", "antigravity")):
            return found
        home = Path.home()
        for cand in (home / ".local/bin/agy", home / ".antigravity/bin/agy", Path("/Applications/Antigravity.app/Contents/Resources/app/bin/agy")):
            if cand.is_file() and os.access(cand, os.X_OK):
                return str(cand)
        return ""

    def discover(self) -> DriverDiagnostic:
        gui = self.discover_gui()
        bin_path = self._find_binary()
        installed = bool(bin_path) or bool(gui["gui_installed"])
        version = probe_binary_version(bin_path) if bin_path else ""
        home, g_home = Path.home(), _resolve_gemini_home()
        has_env = bool(os.environ.get("ANTIGRAVITY_API_KEY") or os.environ.get("GEMINI_API_KEY"))
        auth_ok = installed and (has_env or any(p.exists() for p in (g_home / "antigravity", home / ".antigravity", home / ".config/Antigravity")))
        if not installed:
            detail = "agy / antigravity binary and Antigravity.app not found"
        elif gui["gui_installed"] and not bin_path:
            detail = "ready (Antigravity GUI; agy CLI not on PATH)"
        elif gui["gui_installed"]:
            detail = "ready (Antigravity GUI + agy acp)"
        elif not auth_ok:
            detail = "installed; local Antigravity auth state directory not detected"
        else:
            detail = "ready (agy acp stdio)"
        return DriverDiagnostic(
            kind=self.kind, display_name=self.display_name, installed=installed, binary_path=bin_path,
            version=version, auth_ok=auth_ok, protocol=self.protocol, detail=detail,
            gui_installed=bool(gui["gui_installed"]), gui_app_path=str(gui["gui_app_path"]),
            gui_bundle_id=str(gui["gui_bundle_id"]), gui_version=str(gui["gui_version"]), url_scheme=str(gui["url_scheme"]),
        )

    def _register_project_idempotent(self, real_ws: Path, g_home: Path) -> tuple[str, Path, bool]:
        projects_dir = g_home / "config" / "projects"
        projects_dir.mkdir(parents=True, exist_ok=True)
        real_str = str(real_ws)
        for proj_file in sorted(projects_dir.glob("*.json")):
            doc = _read_json_tolerant(proj_file)
            if doc and real_str in _extract_project_paths(doc):
                return str(doc.get("id") or proj_file.stem), proj_file, False

        proj_id = str(uuid.uuid4())
        proj_file = projects_dir / f"{proj_id}.json"
        folder_uri = real_ws.as_uri()
        resource: dict[str, Any] = {"gitFolder": {"folderUri": folder_uri, "defaultBranch": "main"}} if (real_ws / ".git").exists() else {"folderUri": folder_uri}
        payload = {
            "id": proj_id, "name": real_ws.name, "projectResources": {"resources": [resource]},
            "permissionGrants": {"v2Migrated": True}, "settings": {}, "isWorkspaceOnly": False,
        }
        tmp = proj_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o644)
        os.replace(tmp, proj_file)
        return proj_id, proj_file, True

    def sync_workspace(
        self, workspace: Path, mcp_url: str | None = None, *, gemini_home: Path | None = None, register_global_mcp: bool = False,
    ) -> dict[str, Any]:
        real_ws = Path(os.path.realpath(workspace.expanduser()))
        skills_res = ensure_workspace_skills(real_ws)
        if not (agents_md := real_ws / "AGENTS.md").exists():
            agents_md.write_text(f"# {real_ws.name}\n\nManaged by Parhelion.\n", encoding="utf-8")

        g_home = _resolve_gemini_home(gemini_home)
        project_id, project_file, created_new = self._register_project_idempotent(real_ws, g_home)

        plugin_dir = real_ws / ".agents" / "plugins" / "parhelion"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        plugin_json = plugin_dir / "plugin.json"
        tmp_pj = plugin_json.with_suffix(".tmp")
        tmp_pj.write_text(json.dumps({"name": "parhelion"}, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp_pj, plugin_json)
        plugin_mcp = write_mcp_json_file(plugin_dir / "mcp_config.json", real_ws, mcp_url=mcp_url)

        if register_global_mcp:
            gm_path = g_home / "config" / "mcp_config.json"
            gm_path.parent.mkdir(parents=True, exist_ok=True)
            existing = _read_json_tolerant(gm_path)
            servers = existing.get("mcpServers") if isinstance(existing.get("mcpServers"), dict) else {}
            if mcp_url:
                servers["parhelion"] = {"url": mcp_url}
            else:
                cmd, args, env = build_parhelion_mcp_command(real_ws)
                servers["parhelion"] = {"command": cmd, "args": args, "env": env}
            existing["mcpServers"] = servers
            tmp_gm = gm_path.with_suffix(".tmp")
            tmp_gm.write_text(json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(tmp_gm, gm_path)

        drv_dir = resolve_workspace_driver_dir(real_ws, self.kind)
        drv_dir.mkdir(parents=True, exist_ok=True)
        mcp_path = write_mcp_json_file(drv_dir / "mcp_config.json", real_ws, mcp_url=mcp_url)
        acp_servers = build_acp_mcp_servers(real_ws, mcp_url=mcp_url)
        meta_path = drv_dir / "workspace.json"
        meta_path.write_text(json.dumps({
            "workspace": str(real_ws), "project_id": project_id, "driver": self.kind, "protocol": self.protocol,
            "skills_dir": skills_res["skills_dir"], "mcpServers": acp_servers, "updated_at": _cfg.utc_now().isoformat(),
        }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return {
            "ok": True, "driver": self.kind, "workspace": str(real_ws), "project_id": project_id,
            "project_file": str(project_file), "created_new_project": created_new, "plugin_dir": str(plugin_dir),
            "plugin_mcp_config": str(plugin_mcp), "skills_dir": skills_res["skills_dir"],
            "projected_skills": skills_res["projected"], "mcp_config_path": str(mcp_path),
            "workspace_meta_path": str(meta_path), "acp_mcp_servers": acp_servers,
        }

    def build_launch_command(
        self, worktree: Path, prompt: str, session_id: str, resume_id: str | None = None, mcp_config_path: Path | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        env = {
            "PARHELION_HOME": str(resolve_parhelion_home()),
            "PARHELION_SESSION_ID": session_id,
            "PARHELION_WORKTREE": str(worktree.expanduser().resolve()),
        }
        if resume_id:
            env["PARHELION_RESUME_ID"] = resume_id
        if mcp_config_path:
            env["PARHELION_MCP_CONFIG"] = str(mcp_config_path)
        return [self._find_binary() or "agy", "acp"], env

    def build_gui_deep_link(self, worktree: Path, prompt: str = "", project_id: str | None = None, thread_id: str | None = None) -> str:
        params = [*([("project", project_id)] if project_id else []), *([("prompt", prompt)] if prompt else [])]
        return f"antigravity://open?{urllib.parse.urlencode(params, quote_via=urllib.parse.quote)}" if params else "antigravity://open"

    def launch_session(
        self, workspace: Path, problem: str, worktree: Path, brief: str, mode: str = "background",
        resume_id: str | None = None, dry_run: bool = False, preseed_thread: bool = False,
    ) -> SessionRecord:
        if mode != "gui":
            return super().launch_session(workspace, problem, worktree, brief, mode=mode, resume_id=resume_id, dry_run=dry_run, preseed_thread=preseed_thread)

        ws, wt = Path(os.path.realpath(workspace.expanduser())), Path(os.path.realpath(worktree.expanduser()))
        sync_res = self.sync_workspace(ws)
        project_id = str(sync_res.get("project_id") or "")
        brief_path, copied = stage_brief_and_clipboard(wt, brief, copy_clipboard=(not dry_run))
        base_url = self.build_gui_deep_link(wt, prompt="", project_id=project_id or None)
        url = self.build_gui_deep_link(wt, prompt=compact_prompt_for_url(brief, brief_path, len(base_url), 6000), project_id=project_id or None)

        session_id = self._new_session_id()
        diag = self.discover()
        if dry_run:
            status = "idle"
        else:
            if not diag.gui_installed and not diag.installed:
                raise RuntimeError(f"Antigravity GUI / agy CLI not installed ({diag.detail})")
            launch_macos_deep_link(url, diag.gui_app_path, [], "Antigravity")
            status = "running"
        return self._finalize_gui_launch(
            ws, wt, problem, brief, session_id=session_id, native_id=resume_id or session_id,
            status=status, launch_mode="gui", url=url, project_id=project_id, brief_path=brief_path,
            copied=copied, dry_run=dry_run, mcp_config_path=sync_res.get("plugin_mcp_config") or sync_res.get("mcp_config_path"),
            sync_res=sync_res,
        )

    def observe_gui_sessions(self, workspace: Path | None = None, limit: int = 20, *, gemini_home: Path | None = None) -> list[dict[str, Any]]:
        g_home = _resolve_gemini_home(gemini_home)
        db_path = g_home / "antigravity" / "conversation_summaries.db"
        if not db_path.is_file():
            return []
        projects_map: dict[str, dict[str, Any]] = {}
        if (pdir := g_home / "config" / "projects").is_dir():
            for pf in pdir.glob("*.json"):
                if doc := _read_json_tolerant(pf):
                    projects_map[str(doc.get("id") or pf.stem)] = {"name": doc.get("name", ""), "paths": _extract_project_paths(doc)}

        real_ws = Path(os.path.realpath(workspace.expanduser())) if workspace is not None else None
        conn = open_sqlite_readonly_wal(db_path)
        try:
            cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(conversation_summaries);").fetchall()}
            if not cols:
                return []
            sel = [
                f"{c}" if c in cols else default
                for c, default in (
                    ("conversation_id", "''"), ("title", "''"), ("preview", "''"), ("step_count", "0"),
                    ("status", "''"), ("project_id", "''"), ("workspace_uris", "'[]'"), ("not_fully_idle", "0"),
                    ("killed", "0"), ("last_modified_time", "''"), ("last_user_input_time", "''"),
                    ("parent_conversation_id", "''"), ("agent_name", "''"),
                )
            ]
            order_col = "last_modified_time" if "last_modified_time" in cols else "rowid"
            rows = conn.execute(
                f"SELECT {', '.join(sel)} FROM conversation_summaries ORDER BY {order_col} DESC LIMIT ?",
                (max(1, int(limit)) * 3 if real_ws else max(1, int(limit)),),
            ).fetchall()
        finally:
            conn.close()

        out: list[dict[str, Any]] = []
        for cid, title, preview, step_count, raw_status, proj_id, ws_uris_raw, not_fully_idle, killed, last_mod, last_input, parent_cid, agent_name in rows:
            cid_s, proj_id_s = str(cid or ""), str(proj_id or "")
            try:
                parsed = json.loads(ws_uris_raw) if isinstance(ws_uris_raw, str) and ws_uris_raw.strip() else []
                uri_paths = [_uri_to_realpath(str(u)) for u in parsed if u] if isinstance(parsed, list) else []
            except Exception:
                uri_paths = []
            proj_paths = projects_map.get(proj_id_s, {}).get("paths", [])
            all_paths = [p for p in (*uri_paths, *proj_paths) if p]
            if real_ws is not None and not any(Path(cp) == real_ws or real_ws in Path(cp).parents for cp in all_paths):
                continue

            lock_held = is_flock_held(g_home / "antigravity" / "presence" / f"{cid_s}.lock")
            norm_status = "failed" if bool(killed) else ("running" if (str(raw_status or "") == "CASCADE_RUN_STATUS_RUNNING" or (lock_held and bool(not_fully_idle))) else "idle")
            tail_rows = _tail_jsonl(g_home / "antigravity" / "brain" / cid_s / ".system_generated" / "logs" / "transcript.jsonl", 5)
            last_tool = next(
                (str(tcs[-1].get("name") or "") for tr in reversed(tail_rows) if isinstance(tcs := tr.get("tool_calls"), list) and tcs and isinstance(tcs[-1], dict)),
                "",
            )
            wt_str = uri_paths[0] if uri_paths else (proj_paths[0] if proj_paths else "")
            out.append({
                "session_id": f"agy-gui-{cid_s[:12]}", "driver": self.kind,
                "workspace": str(real_ws) if real_ws else wt_str,
                "problem": infer_problem_from_worktree(wt_str, real_ws), "worktree": wt_str,
                "native_session_id": cid_s, "status": norm_status, "pid": None,
                "created_at": str(last_input or last_mod or ""), "updated_at": str(last_mod or ""),
                "brief_preview": str(title or preview or "")[:180], "launch_mode": "gui",
                "deep_link": f"antigravity://open?project={urllib.parse.quote(proj_id_s, safe='')}" if proj_id_s else "antigravity://open",
                "project_id": proj_id_s, "brief_path": "", "observed_steps": int(step_count or 0),
                "tokens_used": 0, "killed": bool(killed), "presence_lock_held": lock_held,
                "agent_name": str(agent_name or ""), "parent_conversation_id": str(parent_cid or ""),
                "last_tool": last_tool, "source": "gui-observer",
            })
            if len(out) >= limit:
                break
        return out

    def _execute_headless_turn(
        self, workspace: Path, worktree: Path, brief: str, session_id: str, native_session_id: str,
        resume_id: str | None, cmd: list[str], env: dict[str, str], log_path: Path,
    ) -> int:
        return run_acp_headless_turn(workspace, worktree, brief, session_id, native_session_id, resume_id, cmd, env, log_path)
