"""DeepSeek Harness (`dsh`) Unified Driver (Desktop GUI `dsh://open` + bundled CLI / ACP stdio + JSON/flock observer)."""
from __future__ import annotations

import datetime
import json
import os
import re
from pathlib import Path
from typing import Any

from .. import config as _cfg
from .antigravity_acp import build_acp_mcp_servers, run_acp_headless_turn
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
    ensure_workspace_skills,
    infer_problem_from_worktree,
    inspect_macos_app_bundle,
    is_flock_held,
    launch_macos_deep_link,
    stage_brief_and_clipboard,
    write_mcp_json_file,
)


def _resolve_dsh_home(dsh_home: Path | None = None) -> Path:
    if dsh_home is not None:
        return dsh_home.expanduser()
    env_home = os.environ.get("DSH_HOME")
    return Path(env_home).expanduser() if env_home else (Path.home() / ".dsh")


def _dsh_project_key(cwd: str) -> str:
    """Encode cwd using DSH `dsh-session-persistence-jsonl` `projectKey` format (`--<readable>--`)."""
    s = re.sub(r"[/\\:]+", "-", str(cwd or ""))
    out_chars: list[str] = []
    for ch in s:
        if ch.isascii() and (ch.isalnum() or ch in "._-"):
            out_chars.append(ch)
        else:
            out_chars.append(f"~{ord(ch):04X}")
    readable = "".join(out_chars).lstrip("-")[:251]
    return f"--{readable}--"


def _ms_to_iso(val: Any) -> str:
    if val is None or val == "":
        return ""
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return ""
        try:
            num = float(s)
        except ValueError:
            return s
    else:
        try:
            num = float(val)
        except (TypeError, ValueError):
            return ""
    if num <= 0:
        return ""
    if num > 1e12:
        num /= 1000.0
    return datetime.datetime.fromtimestamp(num, tz=datetime.timezone.utc).isoformat()


def _read_json_safe(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").strip()
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


class DeepSeekDriver(BaseDriver):
    """Unified DeepSeek Harness driver (`DeepSeek Harness.app` GUI + bundled `dsh` CLI / ACP stdio)."""

    kind = "deepseek"
    display_name = "DeepSeek Harness"
    protocol = "acp-stdio"
    gui_apps = ("DeepSeek Harness",)

    def _gui_candidates(self) -> list[Path]:
        return [
            Path("/Applications/DeepSeek Harness.app"),
            Path.home() / "Applications" / "DeepSeek Harness.app",
        ]

    def discover_gui(self) -> dict[str, Any]:
        return inspect_macos_app_bundle(
            self._gui_candidates(),
            expected_bundle_ids={"com.deepseek.dsh"},
        )

    def _find_binary(self) -> str:
        if found := which_binary(("dsh", "deepseek-harness")):
            return found
        for app_dir in self._gui_candidates():
            bundled = app_dir / "Contents" / "Resources" / "runtime" / "cli" / "bin" / "dsh"
            if bundled.is_file() and os.access(bundled, os.X_OK):
                return str(bundled)
        return ""

    def discover(self) -> DriverDiagnostic:
        gui = self.discover_gui()
        bin_path = self._find_binary()
        installed = bool(bin_path) or bool(gui["gui_installed"])
        version = probe_binary_version(bin_path) if bin_path else str(gui.get("gui_version") or "")
        dsh_home = _resolve_dsh_home()
        has_env = bool(os.environ.get("DEEPSEEK_API_KEY"))
        has_local_auth = (
            (dsh_home / ".credentials.yaml").is_file()
            or (dsh_home / "profiles" / "desktop").is_dir()
            or dsh_home.is_dir()
        )
        auth_ok = installed and (has_env or has_local_auth)

        if not installed:
            detail = "DeepSeek Harness.app and dsh CLI not found"
        elif gui["gui_installed"] and bin_path:
            detail = "ready (DeepSeek Harness Desktop + bundled dsh --profile acp)"
        elif gui["gui_installed"]:
            detail = "ready (DeepSeek Harness Desktop GUI; dsh CLI not found)"
        elif not auth_ok:
            detail = "installed; missing DEEPSEEK_API_KEY or ~/.dsh"
        else:
            detail = "ready (dsh --profile acp)"

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
            url_scheme=str(gui["url_scheme"] or ("dsh" if gui["gui_installed"] else "")),
        )

    def sync_workspace(self, workspace: Path, mcp_url: str | None = None) -> dict[str, Any]:
        real_ws = Path(os.path.realpath(workspace.expanduser()))
        # DSH natively discovers `<projectRoot>/.agents/skills` (Rank 200) and `AGENTS.md`.
        skills_res = ensure_workspace_skills(real_ws)
        if not (agents_md := real_ws / "AGENTS.md").exists():
            agents_md.write_text(f"# {real_ws.name}\n\nManaged by Parhelion.\n", encoding="utf-8")

        drv_dir = resolve_workspace_driver_dir(real_ws, self.kind)
        drv_dir.mkdir(parents=True, exist_ok=True)
        mcp_path = write_mcp_json_file(drv_dir / "mcp_config.json", real_ws, mcp_url=mcp_url)
        acp_servers = build_acp_mcp_servers(real_ws, mcp_url=mcp_url)
        meta_path = drv_dir / "workspace.json"
        meta_path.write_text(
            json.dumps(
                {
                    "workspace": str(real_ws),
                    "driver": self.kind,
                    "protocol": self.protocol,
                    "skills_dir": skills_res["skills_dir"],
                    "mcpServers": acp_servers,
                    "updated_at": _cfg.utc_now().isoformat(),
                },
                indent=2,
                ensure_ascii=False,
            ) + "\n",
            encoding="utf-8",
        )
        return {
            "ok": True,
            "driver": self.kind,
            "workspace": str(real_ws),
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
        bin_path = self._find_binary() or "dsh"
        wt = worktree.expanduser().resolve()
        cfg_path = mcp_config_path or self._default_mcp_config_path(wt)
        cmd = [bin_path, "--profile", "acp"]
        env = {
            "PARHELION_HOME": str(resolve_parhelion_home()),
            "PARHELION_MCP_CONFIG": str(cfg_path),
            "PARHELION_SESSION_ID": session_id,
            "PARHELION_WORKTREE": str(wt),
        }
        if resume_id:
            env["PARHELION_RESUME_ID"] = resume_id
        return cmd, env

    def build_gui_deep_link(
        self,
        worktree: Path,
        prompt: str = "",
        project_id: str | None = None,
        thread_id: str | None = None,
    ) -> str:
        # Verified in DeepSeek Harness 0.2.0-rc.2 `lib/main.js`: `open-url` strictly matches
        # `dsh://open` or `dsh://open/` and ignores any query or path parameters.
        return "dsh://open"

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
    ) -> SessionRecord:
        if mode != "gui":
            return super().launch_session(
                workspace, problem, worktree, brief,
                mode=mode, resume_id=resume_id, dry_run=dry_run, preseed_thread=preseed_thread,
            )

        ws = Path(os.path.realpath(workspace.expanduser()))
        wt = Path(os.path.realpath(worktree.expanduser()))
        sync_res = self.sync_workspace(ws)
        brief_path, copied = stage_brief_and_clipboard(wt, brief, copy_clipboard=(not dry_run))
        url = self.build_gui_deep_link(wt, prompt="", thread_id=resume_id)
        session_id = self._new_session_id()
        diag = self.discover()
        if dry_run:
            status = "idle"
        else:
            if not diag.gui_installed and not diag.installed:
                raise RuntimeError(f"DeepSeek Harness Desktop / dsh CLI not installed ({diag.detail})")
            launch_macos_deep_link(url, diag.gui_app_path, [], "DeepSeek Harness")
            status = "draft"
        return self._finalize_gui_launch(
            ws, wt, problem, brief,
            session_id=session_id, native_id=resume_id or session_id,
            status=status, launch_mode="gui", url=url, project_id="",
            brief_path=brief_path, copied=copied, dry_run=dry_run,
            mcp_config_path=sync_res.get("mcp_config_path"), sync_res=sync_res,
        )

    def observe_gui_sessions(
        self,
        workspace: Path | None = None,
        limit: int = 20,
        *,
        dsh_home: Path | None = None,
    ) -> list[dict[str, Any]]:
        """Observe DSH Desktop sessions via `workspace.json` + `session_projcache` + POSIX `flock`."""
        home = _resolve_dsh_home(dsh_home)
        proj_dir = home / "storages" / "session_projcache" / "sessions"
        if not proj_dir.is_dir():
            return []

        ws_doc = _read_json_safe(home / "storages" / "workspace.json")
        global_blk = ws_doc.get("global") if isinstance(ws_doc.get("global"), dict) else {}
        archived_ids = {str(x) for x in (global_blk.get("archivedSessionIds") or [])}
        tables = ws_doc.get("tables") if isinstance(ws_doc.get("tables"), dict) else {}
        ws_table = tables.get("workspaces") if isinstance(tables.get("workspaces"), dict) else {}

        sid_to_ws: dict[str, tuple[str, str]] = {}
        for ws_id, wobj in ws_table.items():
            if not isinstance(wobj, dict):
                continue
            wpath = str(wobj.get("path") or "").strip()
            for sid in wobj.get("sessionIds") or []:
                sid_to_ws[str(sid)] = (str(ws_id), wpath)

        real_ws = Path(os.path.realpath(workspace.expanduser())) if workspace is not None else None
        candidates: list[tuple[float, Path]] = []
        for sf in proj_dir.glob("*.json"):
            if sf.stem in archived_ids:
                continue
            try:
                candidates.append((sf.stat().st_mtime, sf))
            except OSError:
                continue
        candidates.sort(key=lambda item: item[0], reverse=True)

        out: list[dict[str, Any]] = []
        scan_cap = max(60, int(limit) * 6) if real_ws else max(20, int(limit) * 2)
        for mtime, sf in candidates[:scan_cap]:
            doc = _read_json_safe(sf)
            rec = doc.get("record") if isinstance(doc.get("record"), dict) else doc
            ident = rec.get("identity") if isinstance(rec.get("identity"), dict) else {}
            rows = rec.get("rows") if isinstance(rec.get("rows"), dict) else {}
            sid = sf.stem

            ws_id, ws_path = sid_to_ws.get(sid, ("", ""))
            cwd_str = str(ident.get("cwd") or ws_path or "").strip()
            if real_ws is not None:
                if not cwd_str:
                    continue
                try:
                    real_cwd = Path(os.path.realpath(cwd_str))
                except OSError:
                    continue
                if real_cwd != real_ws and real_ws not in real_cwd.parents:
                    continue

            list_meta = (rows.get("sessionListMetadata") or {}).get("val") if isinstance(rows.get("sessionListMetadata"), dict) else {}
            if isinstance(list_meta, dict) and list_meta.get("blank") is True:
                continue

            title_val = (rows.get("title") or {}).get("val") if isinstance(rows.get("title"), dict) else ""
            title_input = (rows.get("titleInput") or {}).get("val") if isinstance(rows.get("titleInput"), dict) else {}
            first_prompt = (title_input.get("first") or {}).get("text") if isinstance(title_input, dict) and isinstance(title_input.get("first"), dict) else ""
            display_title = str(title_val or first_prompt or sid).strip()

            tb = (rows.get("turnBoundary") or {}).get("val") if isinstance(rows.get("turnBoundary"), dict) else {}
            stats = (rows.get("sessionStats") or {}).get("val") if isinstance(rows.get("sessionStats"), dict) else {}
            tok_totals = ((rows.get("tokenUsage") or {}).get("val") or {}).get("totals") if isinstance(rows.get("tokenUsage"), dict) else {}

            lock_path = home / "sessions" / _dsh_project_key(cwd_str) / sid / "session.lock"
            lock_held = is_flock_held(lock_path) if cwd_str else False
            has_open_turn = isinstance(tb, dict) and tb.get("openTurnStartSeq") is not None
            has_open_step = isinstance(stats, dict) and (stats.get("openStep") is not None or bool(stats.get("pendingCalls")))
            norm_status = "running" if (lock_held and (has_open_turn or has_open_step)) else "idle"

            steps = int(stats.get("steps") or stats.get("turns") or 0) if isinstance(stats, dict) else 0
            tokens = 0
            if isinstance(tok_totals, dict):
                tokens = int(
                    (tok_totals.get("uncachedInputTokens") or 0)
                    + (tok_totals.get("outputTokens") or 0)
                    + (tok_totals.get("cacheReadTokens") or 0)
                )

            created_iso = _ms_to_iso(ident.get("createdAt"))
            last_prompt_ms = list_meta.get("lastPromptAt") if isinstance(list_meta, dict) else None
            updated_iso = _ms_to_iso(last_prompt_ms) or _ms_to_iso(mtime) or created_iso

            out.append({
                "session_id": f"dsh-gui-{sid[:12]}",
                "driver": self.kind,
                "workspace": str(real_ws) if real_ws else cwd_str,
                "problem": infer_problem_from_worktree(cwd_str, real_ws),
                "worktree": cwd_str,
                "native_session_id": sid,
                "status": norm_status,
                "pid": None,
                "created_at": created_iso or updated_iso,
                "updated_at": updated_iso,
                "brief_preview": display_title[:180],
                "launch_mode": "gui",
                "deep_link": "dsh://open",
                "project_id": ws_id,
                "brief_path": "",
                "observed_steps": steps,
                "tokens_used": tokens,
                "presence_lock_held": lock_held,
                "source": "gui-observer",
            })
        out.sort(key=lambda r: str(r.get("updated_at") or r.get("created_at") or ""), reverse=True)
        return out[: max(1, int(limit))]

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
        return run_acp_headless_turn(
            workspace, worktree, brief, session_id, native_session_id, resume_id, cmd, env, log_path
        )
