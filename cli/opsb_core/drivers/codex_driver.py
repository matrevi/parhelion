"""Codex Harness Driver (`codex://` Desktop GUI, `.codex/config.toml` sync, `state_5.sqlite` observer)."""
from __future__ import annotations

import datetime
import os
import urllib.parse
from pathlib import Path
from typing import Any

from .base import (
    BaseDriver,
    DriverDiagnostic,
    SessionRecord,
    probe_binary_version,
    resolve_parhelion_home,
    resolve_workspace_driver_dir,
    which_binary,
)
from .codex_appserver import (
    CodexAppServerClient,
    ensure_codex_project_trusted,
    preseed_codex_thread,
    render_codex_config_toml,
    run_codex_headless_turn,
    upsert_workspace_codex_toml,
)
from .macos_gui import (
    compact_prompt_for_url,
    ensure_workspace_skills,
    infer_problem_from_worktree,
    inspect_macos_app_bundle,
    launch_macos_deep_link,
    open_sqlite_readonly_wal,
    stage_brief_and_clipboard,
    write_mcp_json_file,
)


def _resolve_codex_home(codex_home: Path | None = None) -> Path:
    if codex_home is not None:
        return codex_home.expanduser()
    env_home = os.environ.get("CODEX_HOME")
    return Path(env_home).expanduser() if env_home else (Path.home() / ".codex")


def _ms_to_iso(ms: Any) -> str:
    try:
        val = float(ms)
        if val <= 0:
            return ""
        return datetime.datetime.fromtimestamp(val / 1000.0 if val > 1e11 else val, tz=datetime.timezone.utc).isoformat()
    except Exception:
        return ""


class CodexDriver(BaseDriver):
    """Unified Codex driver (`codex://` Desktop GUI + `codex app-server --listen stdio://`)."""

    kind = "codex"
    display_name = "Codex"
    protocol = "app-server-rpc"
    gui_apps = ("Codex", "ChatGPT")

    def _gui_candidates(self) -> list[Path]:
        home = Path.home()
        return [Path("/Applications/Codex.app"), Path("/Applications/ChatGPT.app"), home / "Applications/Codex.app", home / "Applications/ChatGPT.app"]

    def discover_gui(self) -> dict[str, Any]:
        return inspect_macos_app_bundle(self._gui_candidates(), expected_bundle_ids={"com.openai.codex"})

    def _find_binary(self, gui_app_path: str = "") -> str:
        if found := which_binary(("codex",)):
            return found
        roots = [Path(gui_app_path)] if gui_app_path else [p for p in self._gui_candidates() if p.exists()]
        for root in roots:
            for rel in ("Contents/Resources/codex-cli/bin/codex", "Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"):
                cand = root / rel
                if cand.is_file() and os.access(cand, os.X_OK):
                    return str(cand)
        return ""

    def _codex_home_dir(self, workspace: Path) -> Path:
        return resolve_workspace_driver_dir(workspace, self.kind)

    def _lookup_project_id(self, real_ws: Path, c_home: Path) -> str:
        state_db = c_home / "state_5.sqlite"
        if not state_db.is_file():
            return ""
        real_str = str(real_ws)
        try:
            conn = open_sqlite_readonly_wal(state_db)
            try:
                for pid, root_p in conn.execute("SELECT p.id, r.path FROM projects p LEFT JOIN project_roots r ON p.id = r.project_id").fetchall():
                    if root_p and os.path.realpath(str(root_p)) == real_str:
                        return str(pid or "")
            finally:
                conn.close()
        except Exception:
            return ""
        return ""

    def discover(self) -> DriverDiagnostic:
        gui = self.discover_gui()
        bin_path = self._find_binary(gui.get("gui_app_path", ""))
        installed = bool(bin_path) or bool(gui["gui_installed"])
        version = probe_binary_version(bin_path) if bin_path else ""
        c_home = _resolve_codex_home()
        has_env = bool(os.environ.get("OPENAI_API_KEY") or os.environ.get("CODEX_API_KEY"))
        auth_ok = installed and (has_env or (c_home / "auth.json").is_file() or (c_home / "state_5.sqlite").is_file())
        if not installed:
            detail = "codex binary and Codex GUI app not found"
        elif gui["gui_installed"] and bin_path:
            detail = "ready (Codex GUI + codex CLI)"
        elif gui["gui_installed"]:
            detail = "ready (Codex GUI; CLI binary unavailable)"
        elif not auth_ok:
            detail = "installed; missing ~/.codex/auth.json or OPENAI_API_KEY"
        else:
            detail = "ready (codex app-server stdio://)"
        return DriverDiagnostic(
            kind=self.kind, display_name=self.display_name, installed=installed, binary_path=bin_path,
            version=version, auth_ok=auth_ok, protocol=self.protocol, detail=detail,
            gui_installed=bool(gui["gui_installed"]), gui_app_path=str(gui["gui_app_path"]),
            gui_bundle_id=str(gui["gui_bundle_id"]), gui_version=str(gui["gui_version"]), url_scheme=str(gui["url_scheme"]),
        )

    def sync_workspace(self, workspace: Path, mcp_url: str | None = None, *, codex_home: Path | None = None) -> dict[str, Any]:
        real_ws = Path(os.path.realpath(workspace.expanduser()))
        skills_res = ensure_workspace_skills(real_ws)
        if not (agents_md := real_ws / "AGENTS.md").exists():
            agents_md.write_text(f"# {real_ws.name}\n\nManaged by Parhelion.\n", encoding="utf-8")

        ws_codex_toml = real_ws / ".codex" / "config.toml"
        upsert_workspace_codex_toml(ws_codex_toml, real_ws, mcp_url=mcp_url)
        target_codex_home = _resolve_codex_home(codex_home)
        trusted_updated = ensure_codex_project_trusted(target_codex_home / "config.toml", str(real_ws))

        isolated_home = self._codex_home_dir(real_ws)
        isolated_home.mkdir(parents=True, exist_ok=True)
        isolated_toml = isolated_home / "config.toml"
        tmp = isolated_toml.with_suffix(".tmp")
        tmp.write_text(render_codex_config_toml(real_ws, mcp_url=mcp_url), encoding="utf-8")
        os.replace(tmp, isolated_toml)

        global_auth, isolated_auth = target_codex_home / "auth.json", isolated_home / "auth.json"
        if global_auth.is_file() and not isolated_auth.exists() and not isolated_auth.is_symlink():
            try:
                isolated_auth.symlink_to(global_auth)
            except OSError:
                pass
        mcp_json_path = write_mcp_json_file(isolated_home / "mcp_config.json", real_ws, mcp_url=mcp_url)
        return {
            "ok": True, "driver": self.kind, "workspace": str(real_ws),
            "project_id": self._lookup_project_id(real_ws, target_codex_home),
            "skills_dir": skills_res["skills_dir"], "projected_skills": skills_res["projected"],
            "workspace_codex_toml": str(ws_codex_toml), "trusted_config_toml": str(target_codex_home / "config.toml"),
            "trust_added": trusted_updated, "codex_home": str(isolated_home),
            "codex_config_toml": str(isolated_toml), "mcp_config_path": str(mcp_json_path),
        }

    def build_launch_command(
        self, worktree: Path, prompt: str, session_id: str, resume_id: str | None = None, mcp_config_path: Path | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        codex_home = mcp_config_path.parent if mcp_config_path is not None else self._codex_home_dir(worktree)
        env = {
            "CODEX_HOME": str(codex_home), "PARHELION_HOME": str(resolve_parhelion_home()),
            "PARHELION_SESSION_ID": session_id, "PARHELION_WORKTREE": str(worktree.expanduser().resolve()),
        }
        if resume_id:
            env["PARHELION_RESUME_ID"] = resume_id
        return [self._find_binary() or "codex", "app-server", "--listen", "stdio://"], env

    def build_exec_command(self, worktree: Path, prompt: str, session_id: str) -> tuple[list[str], dict[str, str]]:
        """Build one-shot `codex exec` command for non-interactive runs."""
        cmd = [self._find_binary() or "codex", "exec", "--cd", str(worktree.expanduser().resolve()), "--json", prompt]
        return cmd, {"CODEX_HOME": str(self._codex_home_dir(worktree)), "PARHELION_HOME": str(resolve_parhelion_home()), "PARHELION_SESSION_ID": session_id}

    def build_gui_deep_link(self, worktree: Path, prompt: str = "", project_id: str | None = None, thread_id: str | None = None) -> str:
        if thread_id:
            url = f"codex://threads/{urllib.parse.quote(thread_id, safe='')}"
            return f"{url}?prompt={urllib.parse.quote(prompt, safe='')}" if prompt else url
        real_wt = str(Path(os.path.realpath(worktree.expanduser())))
        url = f"codex://threads/new?path={urllib.parse.quote(real_wt, safe='')}&mode=codex"
        if project_id:
            url += f"&projectId={urllib.parse.quote(project_id, safe='')}"
        if prompt:
            url += f"&prompt={urllib.parse.quote(prompt, safe='')}"
        return url

    def _preseed_codex_thread(self, bin_path: str, real_wt: Path, name: str, c_home: Path) -> str:
        return preseed_codex_thread(bin_path, real_wt, name, c_home)

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

        diag = self.discover()
        thread_id, launch_mode = resume_id, "gui"
        if not resume_id and preseed_thread and not dry_run and diag.binary_path:
            if pre_tid := self._preseed_codex_thread(diag.binary_path, wt, f"Parhelion · {problem}", _resolve_codex_home()):
                thread_id, launch_mode = pre_tid, "gui-preseed"

        base_url = self.build_gui_deep_link(wt, prompt="", project_id=project_id or None, thread_id=thread_id)
        url = self.build_gui_deep_link(wt, prompt=compact_prompt_for_url(brief, brief_path, len(base_url), 6000), project_id=project_id or None, thread_id=thread_id)
        session_id = self._new_session_id()
        if dry_run:
            status = "idle"
        else:
            if not diag.gui_installed and not diag.installed:
                raise RuntimeError(f"Codex GUI / CLI not installed ({diag.detail})")
            launch_macos_deep_link(url, diag.gui_app_path, [str(wt)], "Codex")
            status = "running"
        return self._finalize_gui_launch(
            ws, wt, problem, brief, session_id=session_id, native_id=thread_id or session_id,
            status=status, launch_mode=launch_mode, url=url, project_id=project_id, brief_path=brief_path,
            copied=copied, dry_run=dry_run, mcp_config_path=sync_res.get("mcp_config_path"), sync_res=sync_res,
        )

    def observe_gui_sessions(self, workspace: Path | None = None, limit: int = 20, *, codex_home: Path | None = None) -> list[dict[str, Any]]:
        c_home = _resolve_codex_home(codex_home)
        state_db = c_home / "state_5.sqlite"
        if not state_db.is_file():
            return []
        real_ws = Path(os.path.realpath(workspace.expanduser())) if workspace is not None else None
        hist_db = c_home / "thread_history_1.sqlite"
        conn_state = open_sqlite_readonly_wal(state_db)
        conn_hist = open_sqlite_readonly_wal(hist_db) if hist_db.is_file() else None
        out: list[dict[str, Any]] = []
        try:
            cols = {str(r[1]) for r in conn_state.execute("PRAGMA table_info(threads);").fetchall()}
            if not cols:
                return []
            title_expr = "COALESCE(name, substr(title, 1, 80))" if {"name", "title"} <= cols else ("name" if "name" in cols else ("title" if "title" in cols else "id"))
            upd_col = "updated_at_ms" if "updated_at_ms" in cols else ("updated_at" if "updated_at" in cols else "0")
            arch_filter = "WHERE archived = 0" if "archived" in cols else ""
            sel = [
                "id", f"{title_expr} AS display_title",
                *(f"{c}" if c in cols else f"{d} AS {c}" for c, d in (
                    ("cwd", "''"), ("project_id", "''"), ("source", "''"), ("model", "''"),
                    ("tokens_used", "0"), ("git_branch", "''"), ("git_sha", "''"),
                )),
                f"{upd_col} AS updated_at_ms", "rollout_path" if "rollout_path" in cols else "'' AS rollout_path",
            ]
            rows = conn_state.execute(
                f"SELECT {', '.join(sel)} FROM threads {arch_filter} ORDER BY {upd_col} DESC LIMIT ?",
                (max(1, int(limit)) * 3 if real_ws else max(1, int(limit)),),
            ).fetchall()
            hist_cols: set[str] = set()
            if conn_hist is not None:
                try:
                    hist_cols = {str(r[1]) for r in conn_hist.execute("PRAGMA table_info(thread_turns);").fetchall()}
                except Exception:
                    hist_cols = set()

            for tid, disp_title, cwd, pid, source, model, tokens_used, git_branch, git_sha, upd_ms, rollout_path in rows:
                cwd_str = str(cwd or "")
                if real_ws is not None:
                    if not cwd_str:
                        continue
                    try:
                        real_cwd = Path(os.path.realpath(cwd_str))
                    except OSError:
                        continue
                    if real_cwd != real_ws and real_ws not in real_cwd.parents:
                        continue
                turn_status, turn_count = "", 0
                if conn_hist is not None and "status" in hist_cols:
                    try:
                        ord_col = "rollout_ordinal" if "rollout_ordinal" in hist_cols else "rowid"
                        if t_row := conn_hist.execute(f"SELECT status FROM thread_turns WHERE thread_id = ? ORDER BY {ord_col} DESC LIMIT 1", (tid,)).fetchone():
                            turn_status = str(t_row[0] or "")
                        if c_row := conn_hist.execute("SELECT COUNT(*) FROM thread_turns WHERE thread_id = ?", (tid,)).fetchone():
                            turn_count = int(c_row[0] or 0)
                    except Exception:
                        pass
                low_t = turn_status.lower()
                norm_status = "running" if low_t in ("inprogress", "in_progress", "running") else ("failed" if low_t in ("failed", "error") else "idle")
                updated_iso = _ms_to_iso(upd_ms)
                out.append({
                    "session_id": f"codex-gui-{str(tid)[:12]}", "driver": self.kind,
                    "workspace": str(real_ws) if real_ws else cwd_str,
                    "problem": infer_problem_from_worktree(cwd_str, real_ws), "worktree": cwd_str,
                    "native_session_id": str(tid), "status": norm_status, "pid": None,
                    "created_at": updated_iso, "updated_at": updated_iso, "brief_preview": str(disp_title or "")[:180],
                    "launch_mode": "gui", "deep_link": f"codex://threads/{tid}", "project_id": str(pid or ""),
                    "brief_path": "", "observed_steps": turn_count, "tokens_used": int(tokens_used or 0),
                    "model": str(model or ""), "git_branch": str(git_branch or ""), "git_sha": str(git_sha or ""),
                    "thread_source": str(source or ""), "rollout_path": str(rollout_path or ""), "source": "gui-observer",
                })
                if len(out) >= limit:
                    break
        finally:
            conn_state.close()
            if conn_hist is not None:
                conn_hist.close()
        return out

    def _execute_headless_turn(
        self, workspace: Path, worktree: Path, brief: str, session_id: str, native_session_id: str,
        resume_id: str | None, cmd: list[str], env: dict[str, str], log_path: Path,
    ) -> int:
        return run_codex_headless_turn(workspace, worktree, brief, session_id, native_session_id, resume_id, cmd, env, log_path)
