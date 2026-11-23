# Track 3 Spec · Unified Single-Driver Runtime & Local MCP Platform

> **Branch**: `v3/driver-mcp` | **Worktree**: `.worktrees/v3-driver-mcp`
> **Owned Files**: `cli/opsb_core/drivers/*`, `cli/opsb_core/mcp/*`, `cli/opsb_core/commands/driver_mcp_cmd.py`, `cli/tests/test_driver_runtime.py`, `cli/tests/test_mcp_server.py`

---

## 1. Goals
1. **Single Unified Driver Abstraction** (directly distilled from Linger's `local-daemon`, without cloud gateway, 8-slot `residentTable`, or Cua GUI scraping):
   - User configures ONE active Harness Driver in `~/.parhelion/config.json` (switchable anytime: `antigravity`, `codex`, `claude`, `pi`, `gemini`, `deepseek`).
   - Creating/binding a workspace in Parhelion automatically runs `driver.sync_workspace(ws)` (registers `.agents/skills`, MCP config, and workspace rules).
   - Clicking "Launch Session" (or running `opsb driver launch <Title>`) prepares an isolated worktree, injects a prompt-cache-safe initial user brief (PMU/residual bottleneck card + Triad KB digest + gate rules), and opens a new session via the Driver's native protocol (or clean GUI/terminal opener).
2. **Focused Local MCP Server (`opsb mcp serve` + loopback HTTP handler)**:
   - Implements standard **MCP JSON-RPC 2.0** (`initialize`, `notifications/initialized`, `tools/list`, `tools/call`) over both `stdio` (for `agy acp`, `codex`, `claude`, `pi`) and localhost HTTP (`/v1/mcp` on `opsb daemon`, reusable as a stable loopback `mcpFront`).
   - Exposes ~9 focused high-signal tools — keeping bulk operations in `opsb` CLI to avoid schema bloat.

---

## 2. Driver Runtime Package (`cli/opsb_core/drivers/`)

### 2.1 Core Protocol (`base.py`)
```python
@dataclass
class DriverDiagnostic:
    kind: str                  # "antigravity" | "codex" | "claude" | "pi" | "gemini" | "deepseek"
    display_name: str
    installed: bool
    binary_path: str
    version: str
    auth_ok: bool
    protocol: str              # "acp-stdio" | "app-server-rpc" | "stream-json" | "pi-rpc" | "cli-exec"
    detail: str

@dataclass
class SessionRecord:
    session_id: str
    driver: str
    workspace: str
    problem: str
    worktree: str
    native_session_id: str     # e.g. Codex threadId, Claude UUID, Antigravity sessionId, Pi sessionId
    status: str                # "running" | "idle" | "completed" | "failed"
    pid: int | None
    created_at: str
    updated_at: str
    brief_preview: str

class BaseDriver(ABC):
    kind: str
    display_name: str
    protocol: str

    def discover(self) -> DriverDiagnostic: ...
    def sync_workspace(self, workspace: Path, mcp_url: str | None = None) -> dict: ...
    def build_launch_command(self, worktree: Path, prompt: str, session_id: str, resume_id: str | None = None, mcp_config_path: Path | None = None) -> tuple[list[str], dict[str, str]]: ...
    def launch_session(self, workspace: Path, problem: str, worktree: Path, brief: str, mode: str = "background", resume_id: str | None = None) -> SessionRecord: ...
```

### 2.2 Concrete Drivers (Linger-Verified Native Protocols)
1. **`antigravity_driver.py`** (`kind="antigravity"`, binary `agy` or `antigravity`):
   - Protocol: `agy acp` (ACP JSON-RPC 2.0: `initialize` → `session/new` or `session/load` with `mcpServers` + `_meta.antigravity.systemPrompt.append` → `session/prompt`; handles Linger's known ACP gotchas: check `replayedPrompts > 0` on `session/load` fallback, quiet-window wait while async tool calls open).
   - `sync_workspace`: Ensures `<ws>/.agents/skills` & registers workspace metadata; supports `mode="gui"` (opens workspace in Antigravity app + copies brief to clipboard/prompt handoff) OR `mode="headless"` (`agy acp` subprocess with live NDJSON event log under `~/.parhelion/logs/sessions/<id>.jsonl`).
2. **`codex_driver.py`** (`kind="codex"`, binary `codex`):
   - Protocol: `codex app-server --listen stdio://` (JSON-RPC 2.0 `initialize` → `thread/start|resume` → `turn/start`) or `codex exec` for one-shot; supports isolated per-workspace `CODEX_HOME` projection (under `~/.parhelion/workspaces/<ws-id>/drivers/codex/`) writing `[mcp_servers.parhelion]` cleanly without mutating `~/.codex/config.toml`.
   - Supports `mode="gui"` (launches Codex Desktop / Terminal at `worktree`) and `mode="headless"`.
3. **`claude_driver.py`** (`kind="claude"`, binary `claude`):
   - Protocol: `claude -p --input-format stream-json --output-format stream-json --verbose --permission-mode acceptEdits --strict-mcp-config --mcp-config <tmp>` with `--session-id <uuid>` / `--resume <uuid>` and `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` hygiene option.
   - `sync_workspace`: Symlinks `<ws>/.claude/skills -> ../.agents/skills`.
4. **`pi_driver.py`** (`kind="pi"`, binary `pi`):
   - Protocol: `pi --mode rpc --no-context-files --session-dir <dir> --session-id <id> --skill <ws>/.agents/skills` with managed MCP config env `PARHELION_MCP_CONFIG`.
5. **`ext_drivers.py`** (`kind="gemini"` binary `gemini`, `kind="deepseek"` binary `dsh` / `deepseek-harness`):
   - Clean CLI adapters supporting `discover()`, `sync_workspace()`, and `launch_session()` (`gemini -p` / `dsh` interactive or headless).

### 2.3 Session Store & Brief Builder (`session_store.py`, `registry.py`)
- Persists sessions in `~/.parhelion/workspaces/<ws-id>/state/driver_sessions.json` (locked atomic JSON).
- `prepare_session_worktree(workspace, problem, track_name)`: creates or reuses clean git worktree `<problem_repo>/.worktrees/<track>` safely (no-op if `use_main=True`).
- `compose_session_brief(workspace, problem, goal="")`: lazily combines live score/rank (`contest_service`), PMU/ELF/Cold3 card (`fast_feedback_service` if available), and prompt-cache-safe Triad KB digest (`triad_kb_service` if available) into a crisp `< 3 KB` first-user-message brief!

---

## 3. Local MCP Server (`cli/opsb_core/mcp/`)

### 3.1 Protocol (`server.py`)
- Pure Python 3 stdlib JSON-RPC 2.0 MCP implementation (`protocolVersion: "2024-11-05"`):
  - `initialize` → returns `serverInfo: {"name": "parhelion-mcp", "version": "3.0.0"}`, `capabilities: {"tools": {}}`
  - `tools/list` → returns schema definitions for all registered tools
  - `tools/call` → validates args, dispatches to tool handler, returns `{"content": [{"type": "text", "text": json_str}], "isError": bool}`
- Supports:
  1. **Stdio transport**: `opsb mcp serve [--workspace W]` (reads line-delimited JSON-RPC from stdin, writes to stdout, logs errors strictly to stderr).
  2. **In-process HTTP handler**: `handle_mcp_http_request(body_bytes, workspace)` ready to be mounted at `POST /v1/mcp` inside `opsb daemon` (stable loopback `mcpFront`).

### 3.2 Focused MCP Toolset (`tools.py` — 9 High-Signal Tools)
All handlers lazily import sibling services so Track 3 works cleanly both standalone and after Track 1/2 merge:
1. `parhelion_status` — Live contest scores, active NPU state, quota & queue summary.
2. `parhelion_bench_diagnose` — `+corr` headroom strata, residual diagnostics & calibration trust.
3. `parhelion_pmu_card` — `ours/torch` ratios + PMU bottleneck card (`fast_feedback_service`).
4. `parhelion_elf_cold3_check` — ELF `.o/.text`/template redlines & 3-run cold-start spike check.
5. `parhelion_kb_query` — Query Triad KB (`breakthrough` + `snippets` + `community` + anti-patterns) by bottleneck/regime/dtype.
6. `parhelion_kb_record` — Propose new verified snippet / community card / anti-pattern (evidence-gated!).
7. `parhelion_kb_feedback` — Record `win`/`fail`/`anti-pattern` feedback on an existing KB entry (triggers auto-promotion/demotion!).
8. `parhelion_test_plan` — Dry-run WSPT NPU test plan (`--build-only` / `--fast` / full) or execute `opsb test`.
9. `parhelion_submit_gate` — Evaluate 4-Stage Gate & enqueue validated commit (`opsb submit`).

---

## 4. CLI Subcommands (`cli/opsb_core/commands/driver_mcp_cmd.py`)
Export `register_driver_and_mcp_subcommands(sub)`:
- `opsb driver list [--json]`
- `opsb driver doctor [--driver KIND] [--json]`
- `opsb driver select <kind> [--json]` (persists `active_driver` in `~/.parhelion/config.json`)
- `opsb driver sync [--driver KIND] [--workspace W] [--json]`
- `opsb driver launch <problem> [--driver KIND] [--mode headless|gui|dry-run] [--track NAME] [--goal TEXT] [--resume SESSION_ID] [--workspace W] [--json]`
- `opsb driver sessions [--workspace W] [--json]`
- `opsb mcp serve [--workspace W]`
- `opsb mcp list-tools [--json]`
- `opsb mcp call <tool_name> [--args-json JSON] [--workspace W]`

---

## 5. Verification & Tests
- `cli/tests/test_driver_runtime.py`: Test `discover()` mocking, `sync_workspace()` isolated `CODEX_HOME` & `.claude/skills` symlink, `build_launch_command()` for all 6 drivers, `launch_session(mode="dry-run")`, and `driver_sessions.json` persistence under temp `PARHELION_HOME`.
- `cli/tests/test_mcp_server.py`: Test full JSON-RPC 2.0 handshake (`initialize` → `tools/list` → `tools/call`), error handling for unknown tools / invalid JSON, and evidence-gate enforcement through `parhelion_kb_record`.
- Run `~/.parhelion/venv/bin/python -m pytest cli/tests/test_driver_runtime.py cli/tests/test_mcp_server.py -q` — 100% pass.
