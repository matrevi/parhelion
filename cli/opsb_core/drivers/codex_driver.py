"""Codex Harness Driver (`codex://` Desktop GUI, `codex app-server`, WAL observer, and `.codex` sync)."""
from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import tomllib
import urllib.parse
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
    open_sqlite_readonly_wal,
    probe_binary_version,
    resolve_parhelion_home,
    resolve_session_logs_dir,
    resolve_workspace_driver_dir,
    stage_brief_and_clipboard,
    which_binary,
    write_mcp_json_file,
)


class CodexAppServerClient:
    """Native `codex app-server --listen stdio://` JSON-RPC 2.0 client."""

    def __init__(self, stdin_stream: IO[str], stdout_stream: IO[str], log_stream: IO[str] | None = None) -> None:
        self._stdin = stdin_stream
        self._stdout = stdout_stream
        self._log = log_stream
        self._next_id = 1
        self.events: list[dict[str, Any]] = []

    def _log_msg(self, direction: str, msg: Any) -> None:
        if self._log is None:
            return
        self._log.write(json.dumps({"ts": _cfg.utc_now().isoformat(), "dir": direction, "msg": msg}, ensure_ascii=False) + "\n")
        self._log.flush()

    def _write_msg(self, msg: dict[str, Any]) -> None:
        self._log_msg("out", msg)
        self._stdin.write(json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n")
        self._stdin.flush()

    def _read_until_id(self, req_id: int) -> dict[str, Any]:
        while True:
            raw = self._stdout.readline()
            if raw == "":
                raise RuntimeError(f"Codex app-server stdout closed before id={req_id}")
            s = raw.strip()
            if not s:
                continue
            try:
                msg = json.loads(s)
            except json.JSONDecodeError:
                continue
            self._log_msg("in", msg)
            if isinstance(msg, dict) and msg.get("id") == req_id:
                return msg
            if isinstance(msg, dict):
                self.events.append(msg)

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        req_id = self._next_id
        self._next_id += 1
        self._write_msg({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        return self._read_until_id(req_id)

    def initialize(self) -> dict[str, Any]:
        resp = self.request(
            "initialize",
            {"clientInfo": {"name": "parhelion", "title": "Parhelion", "version": "3.0.0"}},
        )
        if "error" in resp:
            raise RuntimeError(f"Codex initialize failed: {resp['error']}")
        self._write_msg({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        return resp.get("result") or {}

    def open_thread(self, cwd: Path, resume_id: str | None = None, *, persist_extended_history: bool = False) -> str:
        if resume_id:
            resp = self.request("thread/resume", {"threadId": resume_id, "cwd": str(cwd)})
            res = resp.get("result") if isinstance(resp.get("result"), dict) else None
            if "error" not in resp and res is not None:
                thread = res.get("thread") if isinstance(res.get("thread"), dict) else {}
                tid = thread.get("id") or res.get("threadId") or resume_id
                return str(tid)
        params: dict[str, Any] = {"cwd": str(cwd)}
        if persist_extended_history:
            params["persistExtendedHistory"] = True
        resp = self.request("thread/start", params)
        if "error" in resp:
            raise RuntimeError(f"Codex thread/start failed: {resp['error']}")
        res = resp.get("result") or {}
        thread = res.get("thread") if isinstance(res.get("thread"), dict) else {}
        return str(thread.get("id") or res.get("threadId") or "")

    def set_thread_name(self, thread_id: str, name: str) -> dict[str, Any]:
        resp = self.request("thread/name/set", {"threadId": thread_id, "name": name})
        if "error" in resp:
            raise RuntimeError(f"Codex thread/name/set failed: {resp['error']}")
        return resp.get("result") or {}

    def start_turn(self, thread_id: str, prompt_text: str) -> dict[str, Any]:
        resp = self.request(
            "turn/start",
            {"threadId": thread_id, "input": [{"type": "text", "text": prompt_text}]},
        )
        if "error" in resp:
            raise RuntimeError(f"Codex turn/start failed: {resp['error']}")
        while True:
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
            self._log_msg("in", msg)
            if isinstance(msg, dict):
                self.events.append(msg)
                if msg.get("method") in ("turn/completed", "turn/failed"):
                    break
        return resp.get("result") or {}


def _toml_quote(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def _strip_toml_sections(text: str, section_headers: set[str]) -> str:
    """Remove exact `[header]` sections (preserving all other TOML lines & comments)."""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    skipping = False
    for ln in lines:
        stripped = ln.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if stripped in section_headers:
                skipping = True
                continue
            skipping = False
        if not skipping:
            out.append(ln)
    return "".join(out)


def render_codex_config_toml(workspace: Path, mcp_url: str | None = None) -> str:
    if mcp_url:
        lines = [
            "# Managed by Parhelion",
            "[mcp_servers.parhelion]",
            f"url = {_toml_quote(mcp_url)}",
        ]
    else:
        cmd, args, env = build_parhelion_mcp_command(workspace)
        args_toml = ", ".join(_toml_quote(a) for a in args)
        lines = [
            "# Managed by Parhelion",
            "[mcp_servers.parhelion]",
            f"command = {_toml_quote(cmd)}",
            f"args = [{args_toml}]",
            "[mcp_servers.parhelion.env]",
            *(f"{k} = {_toml_quote(v)}" for k, v in env.items()),
        ]
    return "\n".join(lines) + "\n"


def _upsert_workspace_codex_toml(ws_toml_path: Path, workspace: Path, mcp_url: str | None = None) -> None:
    ws_toml_path.parent.mkdir(parents=True, exist_ok=True)
    existing = ws_toml_path.read_text(encoding="utf-8") if ws_toml_path.is_file() else ""
    base = _strip_toml_sections(existing, {"[mcp_servers.parhelion]", "[mcp_servers.parhelion.env]"})
    if base and not base.endswith("\n"):
        base += "\n"
    block = render_codex_config_toml(workspace, mcp_url=mcp_url)
    merged = (base.rstrip() + "\n\n" + block) if base.strip() else block
    tomllib.loads(merged)
    tmp = ws_toml_path.with_suffix(".tmp")
    tmp.write_text(merged, encoding="utf-8")
    os.replace(tmp, ws_toml_path)


def _ensure_codex_project_trusted(config_toml_path: Path, real_ws: str) -> bool:
    """Idempotently add `[projects."<real_ws>"] trust_level = "trusted"` without clobbering existing TOML."""
    config_toml_path.parent.mkdir(parents=True, exist_ok=True)
    existing = config_toml_path.read_text(encoding="utf-8") if config_toml_path.is_file() else ""
    if existing.strip():
        try:
            parsed = tomllib.loads(existing)
            projects = parsed.get("projects")
            if isinstance(projects, dict):
                cur = projects.get(real_ws)
                if isinstance(cur, dict) and cur.get("trust_level") == "trusted":
                    return False
        except tomllib.TOMLDecodeError:
            pass

    header = f"[projects.{_toml_quote(real_ws)}]"
    cleaned = _strip_toml_sections(existing, {header})
    snippet = f"{header}\ntrust_level = \"trusted\"\n"
    new_text = (cleaned.rstrip() + "\n\n" + snippet) if cleaned.strip() else snippet
    tomllib.loads(new_text)
    tmp = config_toml_path.with_suffix(".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, config_toml_path)
    return True


def _resolve_codex_home(codex_home: Path | None = None) -> Path:
    if codex_home is not None:
        return codex_home.expanduser()
    env_home = os.environ.get("CODEX_HOME")
    if env_home:
        return Path(env_home).expanduser()
    return Path.home() / ".codex"


def _ms_to_iso(ms: Any) -> str:
    try:
        val = float(ms)
        if val <= 0:
            return ""
        sec = val / 1000.0 if val > 1e11 else val
        return datetime.datetime.fromtimestamp(sec, tz=datetime.timezone.utc).isoformat()
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
        return [
            Path("/Applications/Codex.app"),
            Path("/Applications/ChatGPT.app"),
            home / "Applications" / "Codex.app",
            home / "Applications" / "ChatGPT.app",
        ]

    def discover_gui(self) -> dict[str, Any]:
        return inspect_macos_app_bundle(self._gui_candidates(), expected_bundle_ids={"com.openai.codex"})

    def _find_binary(self, gui_app_path: str = "") -> str:
        found = which_binary(("codex",))
        if found:
            return found
        gui_roots = [Path(gui_app_path)] if gui_app_path else [p for p in self._gui_candidates() if p.exists()]
        for root in gui_roots:
            for rel in (
                Path("Contents/Resources/codex-cli/bin/codex"),
                Path("Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"),
            ):
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
                cur = conn.execute(
                    "SELECT p.id, r.path FROM projects p LEFT JOIN project_roots r ON p.id = r.project_id"
                )
                for pid, root_p in cur.fetchall():
                    if not root_p:
                        continue
                    try:
                        if os.path.realpath(str(root_p)) == real_str:
                            return str(pid or "")
                    except OSError:
                        continue
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
        auth_file = c_home / "auth.json"
        state_db = c_home / "state_5.sqlite"
        has_env = bool(os.environ.get("OPENAI_API_KEY") or os.environ.get("CODEX_API_KEY"))
        auth_ok = installed and (has_env or auth_file.is_file() or state_db.is_file())
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

    def sync_workspace(
        self,
        workspace: Path,
        mcp_url: str | None = None,
        *,
        codex_home: Path | None = None,
    ) -> dict[str, Any]:
        real_ws = Path(os.path.realpath(workspace.expanduser()))
        skills_res = ensure_workspace_skills(real_ws)

        agents_md = real_ws / "AGENTS.md"
        if not agents_md.exists():
            agents_md.write_text(f"# {real_ws.name}\n\nManaged by Parhelion.\n", encoding="utf-8")

        # Workspace-scoped `.codex/config.toml` for Codex Desktop & CLI
        ws_codex_toml = real_ws / ".codex" / "config.toml"
        _upsert_workspace_codex_toml(ws_codex_toml, real_ws, mcp_url=mcp_url)

        # Trust canonical workspace path in target CODEX_HOME/config.toml (respects override in tests)
        target_codex_home = _resolve_codex_home(codex_home)
        trusted_updated = _ensure_codex_project_trusted(target_codex_home / "config.toml", str(real_ws))

        # Isolated per-workspace CODEX_HOME under PARHELION_HOME for headless app-server runs
        isolated_home = self._codex_home_dir(real_ws)
        isolated_home.mkdir(parents=True, exist_ok=True)
        isolated_toml = isolated_home / "config.toml"
        tmp = isolated_toml.with_suffix(".tmp")
        tmp.write_text(render_codex_config_toml(real_ws, mcp_url=mcp_url), encoding="utf-8")
        os.replace(tmp, isolated_toml)

        global_auth = target_codex_home / "auth.json"
        isolated_auth = isolated_home / "auth.json"
        if global_auth.is_file() and not isolated_auth.exists() and not isolated_auth.is_symlink():
            try:
                isolated_auth.symlink_to(global_auth)
            except OSError:
                pass

        mcp_json_path = isolated_home / "mcp_config.json"
        write_mcp_json_file(mcp_json_path, real_ws, mcp_url=mcp_url)
        project_id = self._lookup_project_id(real_ws, target_codex_home)

        return {
            "ok": True,
            "driver": self.kind,
            "workspace": str(real_ws),
            "project_id": project_id,
            "skills_dir": skills_res["skills_dir"],
            "projected_skills": skills_res["projected"],
            "workspace_codex_toml": str(ws_codex_toml),
            "trusted_config_toml": str(target_codex_home / "config.toml"),
            "trust_added": trusted_updated,
            "codex_home": str(isolated_home),
            "codex_config_toml": str(isolated_toml),
            "mcp_config_path": str(mcp_json_path),
        }

    def build_launch_command(
        self,
        worktree: Path,
        prompt: str,
        session_id: str,
        resume_id: str | None = None,
        mcp_config_path: Path | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        bin_path = self._find_binary() or "codex"
        codex_home = mcp_config_path.parent if mcp_config_path is not None else self._codex_home_dir(worktree)
        cmd = [bin_path, "app-server", "--listen", "stdio://"]
        env = {
            "CODEX_HOME": str(codex_home),
            "PARHELION_HOME": str(resolve_parhelion_home()),
            "PARHELION_SESSION_ID": session_id,
            "PARHELION_WORKTREE": str(worktree.expanduser().resolve()),
        }
        if resume_id:
            env["PARHELION_RESUME_ID"] = resume_id
        return cmd, env

    def build_exec_command(self, worktree: Path, prompt: str, session_id: str) -> tuple[list[str], dict[str, str]]:
        """Build one-shot `codex exec` command for non-interactive runs."""
        bin_path = self._find_binary() or "codex"
        cmd = [bin_path, "exec", "--cd", str(worktree.expanduser().resolve()), "--json", prompt]
        env = {
            "CODEX_HOME": str(self._codex_home_dir(worktree)),
            "PARHELION_HOME": str(resolve_parhelion_home()),
            "PARHELION_SESSION_ID": session_id,
        }
        return cmd, env

    def build_gui_deep_link(
        self,
        worktree: Path,
        prompt: str = "",
        project_id: str | None = None,
        thread_id: str | None = None,
    ) -> str:
        if thread_id:
            url = f"codex://threads/{urllib.parse.quote(thread_id, safe='')}"
            if prompt:
                url += f"?prompt={urllib.parse.quote(prompt, safe='')}"
            return url
        real_wt = str(Path(os.path.realpath(worktree.expanduser())))
        url = f"codex://threads/new?path={urllib.parse.quote(real_wt, safe='')}&mode=codex"
        if project_id:
            url += f"&projectId={urllib.parse.quote(project_id, safe='')}"
        if prompt:
            url += f"&prompt={urllib.parse.quote(prompt, safe='')}"
        return url

    def _preseed_codex_thread(self, bin_path: str, real_wt: Path, name: str, c_home: Path) -> str:
        env = {**os.environ, "CODEX_HOME": str(c_home)}
        proc = subprocess.Popen(
            [bin_path, "app-server", "--listen", "stdio://"],
            cwd=str(real_wt),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        assert proc.stdin is not None and proc.stdout is not None
        client = CodexAppServerClient(proc.stdin, proc.stdout)
        try:
            client.initialize()
            tid = client.open_thread(real_wt, persist_extended_history=True)
            if tid and name:
                try:
                    client.set_thread_name(tid, name)
                except Exception:
                    pass
            return tid
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()

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
        brief_path, copied = stage_brief_and_clipboard(wt, brief, copy_clipboard=(not dry_run))

        diag = self.discover()
        thread_id = resume_id
        launch_mode = "gui"
        if not resume_id and preseed_thread and not dry_run and diag.binary_path:
            c_home = _resolve_codex_home()
            pre_tid = self._preseed_codex_thread(diag.binary_path, wt, f"Parhelion · {problem}", c_home)
            if pre_tid:
                thread_id = pre_tid
                launch_mode = "gui-preseed"

        base_url = self.build_gui_deep_link(wt, prompt="", project_id=project_id or None, thread_id=thread_id)
        compact_prompt = compact_prompt_for_url(brief, brief_path, len(base_url), max_url_len=6000)
        url = self.build_gui_deep_link(wt, prompt=compact_prompt, project_id=project_id or None, thread_id=thread_id)

        session_id = self._new_session_id()
        native_id = thread_id or session_id
        now_iso = _cfg.utc_now().isoformat()
        cmd = ["open", url]

        if dry_run:
            status = "idle"
        else:
            if not diag.gui_installed and not diag.installed:
                raise RuntimeError(f"Codex GUI / CLI not installed ({diag.detail})")
            proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False)
            if proc.returncode != 0:
                if diag.gui_app_path:
                    subprocess.run(["open", "-a", diag.gui_app_path, str(wt)], check=True)
                else:
                    raise RuntimeError(f"Failed to launch Codex GUI URL: {(proc.stderr or proc.stdout).strip()}")
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
            launch_mode=launch_mode,
            deep_link=url,
            project_id=project_id,
            brief_path=str(brief_path),
        )
        save_session(ws, record)
        self.last_launch_meta = {
            "mode": "gui",
            "launch_mode": launch_mode,
            "dry_run": dry_run,
            "command": cmd,
            "env": {},
            "mcp_config_path": sync_res.get("mcp_config_path"),
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
        codex_home: Path | None = None,
    ) -> list[dict[str, Any]]:
        c_home = _resolve_codex_home(codex_home)
        state_db = c_home / "state_5.sqlite"
        if not state_db.is_file():
            return []

        real_ws: Path | None = Path(os.path.realpath(workspace.expanduser())) if workspace is not None else None
        hist_db = c_home / "thread_history_1.sqlite"

        out: list[dict[str, Any]] = []
        conn_state = open_sqlite_readonly_wal(state_db)
        conn_hist = open_sqlite_readonly_wal(hist_db) if hist_db.is_file() else None
        try:
            cols_info = conn_state.execute("PRAGMA table_info(threads);").fetchall()
            col_names = {str(row[1]) for row in cols_info}
            if not col_names:
                return []

            title_expr = "COALESCE(name, substr(title, 1, 80))" if ("name" in col_names and "title" in col_names) else (
                "name" if "name" in col_names else ("title" if "title" in col_names else "id")
            )
            upd_col = "updated_at_ms" if "updated_at_ms" in col_names else ("updated_at" if "updated_at" in col_names else "0")
            arch_filter = "WHERE archived = 0" if "archived" in col_names else ""
            sel_cols = [
                "id",
                f"{title_expr} AS display_title",
                "cwd" if "cwd" in col_names else "'' AS cwd",
                "project_id" if "project_id" in col_names else "'' AS project_id",
                "source" if "source" in col_names else "'' AS source",
                "model" if "model" in col_names else "'' AS model",
                "tokens_used" if "tokens_used" in col_names else "0 AS tokens_used",
                "git_branch" if "git_branch" in col_names else "'' AS git_branch",
                "git_sha" if "git_sha" in col_names else "'' AS git_sha",
                f"{upd_col} AS updated_at_ms",
                "rollout_path" if "rollout_path" in col_names else "'' AS rollout_path",
            ]
            sql = f"SELECT {', '.join(sel_cols)} FROM threads {arch_filter} ORDER BY {upd_col} DESC LIMIT ?"
            rows = conn_state.execute(sql, (max(1, int(limit)) * 3 if real_ws else max(1, int(limit)),)).fetchall()

            hist_cols: set[str] = set()
            if conn_hist is not None:
                try:
                    hist_cols = {str(r[1]) for r in conn_hist.execute("PRAGMA table_info(thread_turns);").fetchall()}
                except Exception:
                    hist_cols = set()

            for row in rows:
                tid, disp_title, cwd, pid, source, model, tokens_used, git_branch, git_sha, upd_ms, rollout_path = row
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

                turn_status = ""
                turn_count = 0
                if conn_hist is not None and hist_cols and "status" in hist_cols:
                    try:
                        order_col = "rollout_ordinal" if "rollout_ordinal" in hist_cols else "rowid"
                        t_row = conn_hist.execute(
                            f"SELECT status FROM thread_turns WHERE thread_id = ? ORDER BY {order_col} DESC LIMIT 1",
                            (tid,),
                        ).fetchone()
                        if t_row:
                            turn_status = str(t_row[0] or "")
                        c_row = conn_hist.execute(
                            "SELECT COUNT(*) FROM thread_turns WHERE thread_id = ?",
                            (tid,),
                        ).fetchone()
                        if c_row:
                            turn_count = int(c_row[0] or 0)
                    except Exception:
                        pass

                low_t = turn_status.lower()
                if low_t in ("inprogress", "in_progress", "running"):
                    norm_status = "running"
                elif low_t in ("failed", "error"):
                    norm_status = "failed"
                else:
                    norm_status = "idle"

                updated_iso = _ms_to_iso(upd_ms)
                problem_name = Path(cwd_str).name if cwd_str else ""
                if real_ws is not None and cwd_str:
                    try:
                        rel_parts = Path(os.path.realpath(cwd_str)).relative_to(real_ws).parts
                        if len(rel_parts) >= 2 and rel_parts[0] == "bench":
                            problem_name = rel_parts[1]
                        elif rel_parts and rel_parts[0] not in (".worktrees",):
                            problem_name = rel_parts[0]
                    except Exception:
                        pass

                out.append(
                    {
                        "session_id": f"codex-gui-{str(tid)[:12]}",
                        "driver": self.kind,
                        "workspace": str(real_ws) if real_ws else cwd_str,
                        "problem": problem_name,
                        "worktree": cwd_str,
                        "native_session_id": str(tid),
                        "status": norm_status,
                        "pid": None,
                        "created_at": updated_iso,
                        "updated_at": updated_iso,
                        "brief_preview": str(disp_title or "")[:180],
                        "launch_mode": "gui",
                        "deep_link": f"codex://threads/{tid}",
                        "project_id": str(pid or ""),
                        "brief_path": "",
                        "observed_steps": turn_count,
                        "tokens_used": int(tokens_used or 0),
                        "model": str(model or ""),
                        "git_branch": str(git_branch or ""),
                        "git_sha": str(git_sha or ""),
                        "thread_source": str(source or ""),
                        "rollout_path": str(rollout_path or ""),
                        "source": "gui-observer",
                    }
                )
                if len(out) >= limit:
                    break
        finally:
            conn_state.close()
            if conn_hist is not None:
                conn_hist.close()
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
            client = CodexAppServerClient(proc.stdin, proc.stdout, log_stream=lf)
            try:
                client.initialize()
                thread_id = client.open_thread(worktree, resume_id=resume_id)
                if thread_id:
                    update_session(workspace, session_id, native_session_id=thread_id)
                client.start_turn(thread_id or native_session_id, brief)
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
