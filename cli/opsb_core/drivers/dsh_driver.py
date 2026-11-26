"""DeepSeek Harness (`dsh`) Unified Driver (Desktop GUI `dsh://open` + bundled CLI / ACP stdio + JSON/flock observer)."""
from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from .. import config as _cfg
from .. import worker_sandbox
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


def _zstd_read(path: Path) -> bytes | None:
    """Decompress a zstd file via the `zstd` CLI (DSH appends one frame per write)."""
    zstd_bin = shutil.which("zstd")
    if not zstd_bin:
        return None
    try:
        proc = subprocess.run([zstd_bin, "-dc", str(path)], capture_output=True, timeout=30)
    except Exception:
        return None
    return proc.stdout if proc.returncode == 0 else None


def _zstd_frame(data: bytes) -> bytes | None:
    zstd_bin = shutil.which("zstd")
    if not zstd_bin:
        return None
    try:
        proc = subprocess.run([zstd_bin, "-q", "-c"], input=data, capture_output=True, timeout=30)
    except Exception:
        return None
    return proc.stdout if proc.returncode == 0 else None


def _dsh_workspace_store(dsh_home: Path) -> Path:
    return dsh_home / "storages" / "workspace.json"


def _dsh_session_log(dsh_home: Path, cwd: str, session_id: str) -> Path:
    return dsh_home / "sessions" / _dsh_project_key(cwd) / session_id / "session.v4.jsonl.zstd"


def _dsh_desktop_running() -> bool:
    """True when the Desktop app is already up (its in-memory store needs a page reload)."""
    try:
        proc = subprocess.run(
            ["pgrep", "-f", "Contents/MacOS/DeepSeek Harness"],
            capture_output=True, timeout=5,
        )
    except Exception:
        return False
    return proc.returncode == 0


def _quit_dsh_desktop(timeout: float = 15.0) -> bool:
    """Gracefully quit DeepSeek Harness so its storage unit is flushed before we edit it.

    DSH keeps the workspace unit authoritative in memory and rewrites the whole file on any
    change, so an external write is only safe while the app is down. Every open session is
    already persisted; the app restores them on the next launch.
    """
    if not _dsh_desktop_running():
        return True
    try:
        subprocess.run(
            ["osascript", "-e", 'tell application "DeepSeek Harness" to quit'],
            capture_output=True, timeout=20,
        )
    except Exception:
        pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _dsh_desktop_running():
            return True
        time.sleep(0.3)
    try:
        subprocess.run(["pkill", "-f", "Contents/MacOS/DeepSeek Harness"], capture_output=True, timeout=10)
    except Exception:
        pass
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if not _dsh_desktop_running():
            return True
        time.sleep(0.2)
    return False






DSH_SUBAGENT_CAP = 20


def ensure_subagent_capacity(
    profile: str = "desktop",
    minimum: int = DSH_SUBAGENT_CAP,
    dsh_home: Path | None = None,
) -> dict[str, Any]:
    """Pin DSH's subagent residency cap in `<profile>/cordis.patch.yml`.

    The shipped default is 8 (and this machine's desktop profile had been pinned to 5), far
    below a 14-problem sprint, so spawn requests were rejected at residency and had to be
    retried by hand. The value is read when a profile loads, so this only takes effect on
    the next DSH start; callers must not restart DSH on the user's behalf.
    """
    home = _resolve_dsh_home(dsh_home)
    path = home / "profiles" / profile / "cordis.patch.yml"
    result: dict[str, Any] = {"path": str(path), "changed": False, "value": None, "exists": path.is_file()}
    if not path.is_file():
        return result
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.strip() == "- id: subagent"), None)
    if start is None:
        block = ["- id: subagent", "  name: '@deepseek-ai/dsh-subagent'", "  config:",
                 f"    maxActiveSubagents: {minimum}"]
        # A profile that only holds the loader's `[]` placeholder must be replaced, not
        # appended to (two top-level documents would fail to parse).
        kept = [ln for ln in lines if ln.strip() not in ("[]", "")]
        text = "\n".join(kept + ([""] if kept else []) + block) + "\n"
        path.write_text(text, encoding="utf-8")
        result.update(changed=True, value=minimum)
        return result

    end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("- id:")), len(lines))
    current = None
    value_idx = None
    for i in range(start, end):
        stripped = lines[i].strip()
        if stripped.startswith("maxActiveSubagents:"):
            value_idx = i
            try:
                current = int(stripped.split(":", 1)[1].strip())
            except ValueError:
                current = None
            break
    if value_idx is None:
        for i in range(start, end):
            if lines[i].strip() == "config:":
                value_idx = i
                lines.insert(i + 1, f"    maxActiveSubagents: {minimum}")
                break
        else:
            lines.insert(end, f"  config:\n    maxActiveSubagents: {minimum}")
            value_idx = end
        result.update(changed=True, value=minimum)
    elif current is None or current < minimum:
        indent = lines[value_idx][: len(lines[value_idx]) - len(lines[value_idx].lstrip())]
        lines[value_idx] = f"{indent}maxActiveSubagents: {minimum}"
        result.update(changed=True, value=minimum)
    else:
        result["value"] = current
    if result["changed"]:
        (home / "profiles" / profile / "cordis.patch.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result


PARHELION_MCP_PLUGIN = Path(__file__).with_name("dsh_parhelion_mcp.mjs")
PARHELION_MCP_PLUGIN_ID = "global-parhelion-mcp"
PARHELION_MCP_BEGIN = "# >>> parhelion-mcp (managed by opsb, rewritten on launch)"
PARHELION_MCP_END = "# <<< parhelion-mcp"


def ensure_parhelion_mcp_plugin(
    profile: str = "desktop",
    dsh_home: Path | None = None,
    command: str | None = None,
    opsb_script: str | None = None,
) -> dict[str, Any]:
    """Install the per-Agent Parhelion MCP mount into a DSH profile.

    DSH Desktop conversations only see MCP servers that the profile mounts; registering a
    contest folder as a DSH workspace does not give its conversations the `parhelion_*`
    tools. The plugin mounts the orchestrator server for any Agent whose cwd is inside a
    contest workspace. Like ensure_subagent_capacity, the profile is read at load time, so
    a change only takes effect on the next DSH start; callers must not restart DSH.
    """
    home = _resolve_dsh_home(dsh_home)
    profile_dir = home / "profiles" / profile
    patch = profile_dir / "cordis.patch.yml"
    result: dict[str, Any] = {"path": str(patch), "changed": False, "exists": patch.is_file()}
    if not patch.is_file():
        return result
    plugin_target = profile_dir / f"{PARHELION_MCP_PLUGIN_ID}.mjs"
    source = PARHELION_MCP_PLUGIN.read_text(encoding="utf-8")
    if not plugin_target.is_file() or plugin_target.read_text(encoding="utf-8") != source:
        plugin_target.write_text(source, encoding="utf-8")
        result["changed"] = True
    cmd = command or _cfg.resolve_python_exe(None)
    script = opsb_script or str(_cfg.CLI_DIR / "opsb")
    block = [
        PARHELION_MCP_BEGIN,
        "# Parhelion contest tools for Agents whose folder holds opsb.workspace.json.",
        "- insert:",
        f"    - id: {PARHELION_MCP_PLUGIN_ID}",
        f"      name: ./{PARHELION_MCP_PLUGIN_ID}.mjs",
        "      config:",
        f"        command: {json.dumps(str(cmd))}",
        f"        opsb: {json.dumps(script)}",
        PARHELION_MCP_END,
    ]
    lines = patch.read_text(encoding="utf-8").splitlines()
    if PARHELION_MCP_BEGIN in lines and PARHELION_MCP_END in lines:
        begin, end = lines.index(PARHELION_MCP_BEGIN), lines.index(PARHELION_MCP_END) + 1
        if lines[begin:end] != block:
            lines[begin:end] = block
            result["changed"] = True
    else:
        kept = [ln for ln in lines if ln.strip() != "[]"]
        lines = kept + ([""] if kept and kept[-1].strip() else []) + block
        result["changed"] = True
    if result["changed"]:
        patch.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result["plugin"] = str(plugin_target)
    return result


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
            "DSH_PERMISSION_MODE": "danger-full-access",
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
        # `dsh://open` or `dsh://open/` and ignores any query or path parameters, so the folder
        # and the prompt have to be staged inside DSH's own storage instead of the deep link.
        return "dsh://open"

    def ensure_dsh_workspace(self, path: Path, session_id: str | None = None, *, dsh_home: Path | None = None) -> dict[str, Any]:
        """Idempotently register `path` as a DSH Desktop workspace and optionally link a session.

        DSH only adds workspaces through its own UI, so a launcher has to write the same
        `storages/workspace.json` unit (`workspace/v2`). The write is atomic, backed up once and
        skipped entirely for unknown schema versions.
        """
        home = dsh_home or _resolve_dsh_home()
        store = _dsh_workspace_store(home)
        doc = _read_json_safe(store)
        unit = doc.get("unit") if isinstance(doc.get("unit"), dict) else {}
        tables = doc.get("tables") if isinstance(doc.get("tables"), dict) else {}
        workspaces = tables.get("workspaces") if isinstance(tables.get("workspaces"), dict) else None
        if unit.get("name") != "workspace" or int(unit.get("version") or 0) != 2 or workspaces is None:
            return {"ok": False, "reason": f"unsupported DSH workspace store at {store}"}

        real = Path(os.path.realpath(path.expanduser()))
        ws_id = ""
        for wid, obj in workspaces.items():
            if isinstance(obj, dict) and str(obj.get("path") or "").strip():
                if os.path.realpath(str(obj.get("path"))) == str(real):
                    ws_id = str(wid)
                    break
        created = False
        if not ws_id:
            ws_id = str(uuid.uuid4())
            workspaces[ws_id] = {"path": str(real), "title": real.name, "sessionIds": []}
            global_blk = doc.setdefault("global", {})
            ids = global_blk.setdefault("workspaceIds", [])
            if ws_id not in ids:
                ids.append(ws_id)
            created = True

        entry = workspaces[ws_id]
        sessions = entry.setdefault("sessionIds", [])
        linked = False
        if session_id and session_id not in sessions:
            sessions.append(session_id)
            linked = True

        if created or linked:
            backup = store.with_name(store.name + ".parhelion.bak")
            if not backup.exists():
                try:
                    backup.write_bytes(store.read_bytes())
                except OSError:
                    pass
            tmp = store.with_name(store.name + ".parhelion.tmp")
            tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            os.replace(tmp, store)
        return {
            "ok": True,
            "workspace_id": ws_id,
            "path": str(real),
            "created": created,
            "session_linked": linked,
        }

    def preseed_dsh_conversation(self, worktree: Path, brief: str, *, dsh_home: Path | None = None) -> dict[str, Any]:
        """Create a real DSH session for `worktree` and queue the brief as its first user message.

        `session/new` (ACP) makes DSH persist a schema-correct session log; the brief is then
        appended as one extra zstd frame carrying an `agent/inbox/spliced` event, exactly the
        event DSH itself emits when it queues user input for the next turn.
        """
        bin_path = self._find_binary()
        if not bin_path:
            return {"ok": False, "reason": "dsh CLI not found"}
        wt = Path(os.path.realpath(worktree.expanduser()))
        home = dsh_home or _resolve_dsh_home()
        env = worker_sandbox.worker_env(os.environ, {
            "DSH_PERMISSION_MODE": "danger-full-access",
            "PARHELION_HOME": str(resolve_parhelion_home()),
        })
        try:
            proc = subprocess.Popen(
                [bin_path, "--profile", "acp"], cwd=str(wt), env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            )
        except Exception as exc:
            return {"ok": False, "reason": f"cannot start dsh --profile acp: {exc}"}

        from .antigravity_acp import AcpSessionClient

        session_id = ""
        try:
            assert proc.stdin is not None and proc.stdout is not None
            client = AcpSessionClient(proc.stdin, proc.stdout)
            client.initialize()
            session_id, _ = client.open_session(cwd=wt, mcp_servers=[])
        except Exception as exc:
            return {"ok": False, "reason": f"acp session/new failed: {exc}"}
        finally:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()

        if not session_id:
            return {"ok": False, "reason": "acp session/new returned no session id"}
        log_path = _dsh_session_log(home, str(wt), session_id)
        if not log_path.is_file():
            return {"ok": False, "session_id": session_id, "reason": f"session log not found at {log_path}"}
        queued = self._queue_dsh_prompt(log_path, brief)
        return {
            "ok": queued,
            "session_id": session_id,
            "session_path": str(log_path),
            "brief_queued": queued,
            "reason": "" if queued else "zstd CLI unavailable or session log unreadable",
        }

    @staticmethod
    def _queue_dsh_prompt(log_path: Path, brief: str) -> bool:
        text = brief.strip()
        if not text:
            return False
        raw = _zstd_read(log_path)
        if raw is None:
            return False
        last_seq = -1
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except Exception:
                continue
            seq = event.get("seq") if isinstance(event, dict) else None
            if isinstance(seq, int) and seq > last_seq:
                last_seq = seq
        message = {
            "type": "agent/inbox/spliced",
            "seq": last_seq + 1,
            "time": int(time.time() * 1000),
            "data": {
                "target": "next-turn",
                "start": 0,
                "inserted": [{
                    "content": [{"type": "text", "text": text}],
                    "source": {"kind": "user"},
                    "role": "user",
                    "id": str(uuid.uuid4()),
                }],
            },
        }
        frame = _zstd_frame((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        if not frame:
            return False
        try:
            with log_path.open("ab") as fh:
                fh.write(frame)
        except OSError:
            return False
        return True

    def dsh_workspace_exists(self, path: Path, *, dsh_home: Path | None = None) -> bool:
        """Read-only check: is this folder already a DSH workspace?"""
        home = dsh_home or _resolve_dsh_home()
        doc = _read_json_safe(_dsh_workspace_store(home))
        tables = doc.get("tables") if isinstance(doc.get("tables"), dict) else {}
        workspaces = tables.get("workspaces") if isinstance(tables.get("workspaces"), dict) else {}
        real = str(Path(os.path.realpath(path.expanduser())))
        return any(
            isinstance(obj, dict) and os.path.realpath(str(obj.get("path") or "")) == real
            for obj in workspaces.values()
        )

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
        subagent_cap: dict[str, Any] = {}
        parhelion_mcp: dict[str, Any] = {}
        if not dry_run:
            try:
                subagent_cap = ensure_subagent_capacity()
            except Exception:
                subagent_cap = {}
            try:
                parhelion_mcp = ensure_parhelion_mcp_plugin()
            except Exception as exc:  # noqa: BLE001 - registration must not fail the launch
                parhelion_mcp = {"error": str(exc)}
        sync_res = self.sync_workspace(ws)
        brief_path, copied = stage_brief_and_clipboard(wt, brief, copy_clipboard=(not dry_run))
        url = self.build_gui_deep_link(wt, prompt="", thread_id=resume_id)
        session_id = self._new_session_id()
        diag = self.discover()
        seed: dict[str, Any] = {"ok": False, "reason": "dry-run"}
        workspace_reg: dict[str, Any] = {"ok": False, "reason": "dry-run"}
        native_id = resume_id or session_id
        if dry_run:
            status = "idle"
        else:
            if not diag.gui_installed and not diag.installed:
                raise RuntimeError(f"DeepSeek Harness Desktop / dsh CLI not installed ({diag.detail})")
            # DSH workspaces are folders: register the contest directory itself (not the
            # per-problem worktree) so the sidebar entry matches the workspace the user added.
            running = _dsh_desktop_running()
            # A brand-new workspace can only appear after DSH reloads its store from disk, and
            # the store is in-memory-authoritative while the app runs. Quit it once for the
            # first launch of a folder, seed, and let the deep link bring it back.
            missing = not self.dsh_workspace_exists(ws)
            restarted = _quit_dsh_desktop() if (running and missing) else False
            seed = self.preseed_dsh_conversation(ws, brief) if not resume_id else {"ok": False, "reason": "resume"}
            workspace_reg = self.ensure_dsh_workspace(ws, session_id=seed.get("session_id") or resume_id)
            if seed.get("session_id"):
                native_id = str(seed["session_id"])
            workspace_reg["restarted"] = restarted
            if subagent_cap:
                workspace_reg["subagent_capacity"] = subagent_cap
            if parhelion_mcp:
                workspace_reg["parhelion_mcp"] = parhelion_mcp
            workspace_reg["needs_reload"] = bool(
                not restarted and (running or _dsh_desktop_running())
                and (workspace_reg.get("created") or workspace_reg.get("session_linked")
                     or parhelion_mcp.get("changed"))
            )
            launch_macos_deep_link(url, diag.gui_app_path, [], "DeepSeek Harness")
            status = "draft"
        record = self._finalize_gui_launch(
            ws, wt, problem, brief,
            session_id=session_id, native_id=native_id,
            status=status, launch_mode="gui", url=url,
            project_id=str(workspace_reg.get("workspace_id") or ""),
            brief_path=brief_path, copied=copied, dry_run=dry_run,
            mcp_config_path=sync_res.get("mcp_config_path"), sync_res=sync_res,
        )
        self.last_launch_meta["dsh_seed"] = seed
        self.last_launch_meta["dsh_workspace"] = workspace_reg
        return record

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
