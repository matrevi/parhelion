# Track 3b · macOS GUI Runtime Research, Hands-On Probe & Integration Blueprint

> **Companion to**: `docs/v3/03-driver-runtime-and-mcp.md`
> **Targets**: **Antigravity Desktop (2.0)** (`/Applications/Antigravity.app`) & **Codex Desktop** (`/Applications/ChatGPT.app`, `com.openai.codex`)
> **Verification Status**: Empirically verified on macOS (Apple Silicon) against installed app bundles, official docs, isolated `/tmp/parhelion-gui-probe` CLI/RPC harnesses, and live read-only SQLite/flock observers. Zero user sessions or active configs were mutated.

---

## 1. Executive Summary

Both **Codex Desktop** and **Antigravity Desktop (2.0)** expose **native macOS URL schemes, declarative workspace/MCP registries, and fast read-only local SQLite + `flock` telemetry** — eliminating the need for brittle GUI screen-scraping, AppleScript keystroke injection, or Accessibility permissions.

| Capability | **Codex GUI** (`ChatGPT.app` / `com.openai.codex`) | **Antigravity GUI** (`Antigravity.app` / `com.google.antigravity`) |
|---|---|---|
| **Installed App Bundle** | `/Applications/ChatGPT.app` (`CFBundleIdentifier=com.openai.codex`, `CFBundleAlternateNames=["Codex"]`, v`26.1117.41037`). Also check `/Applications/Codex.app`. | `/Applications/Antigravity.app` (`CFBundleIdentifier=com.google.antigravity`, v`2.34.0`, Antigravity 2.0 Electron Hub). |
| **Bundled / Companion CLI** | **Bundled inside app**: `/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex` (`codex-cli 0.171.0-alpha.13`) — works even when `codex` is absent from `$PATH`! | `agy` CLI is separate (not bundled in `Antigravity.app`, currently absent from `$PATH`; official install via `antigravity.google/cli/install.sh`). |
| **Registered URL Scheme** | `codex://` (`CFBundleURLTypes`) | `antigravity://` (`CFBundleURLTypes`) |
| **Native "New Chat + Workspace + Pre-Filled Prompt" Deep Link** | **`codex://threads/new?path=<realpath>&prompt=<urlencoded>&mode=codex[&projectId=<id>]`** (auto-registers project via `createProjectForRoot`, selects it, opens `/`, focuses & prefills composer!). | **`antigravity://open?project=<project_uuid>&prompt=<urlencoded>`** (navigates to Home for `<project_uuid>`, focuses & prefills Home composer; gated by Mendel `enableDeepLinkHandler` flag — always pair with `.parhelion/brief.md` + `pbcopy` fallback!). |
| **Native "Open Existing Thread" Deep Link** | **`codex://threads/<thread_uuid>[?prompt=<urlencoded>]`** (works for threads created by GUI *or* `codex app-server` / `codex exec` sharing `~/.codex`!). | No conversation-ID deep link in v2.34.0 renderer (`antigravity://` handles `open`, `oauth-success`, `plugin/*`, `customizations/marketplaces/*`, `teleport/ai_studio`). |
| **Workspace / Project Registration** | Automatic via `codex://threads/new?path=...` (`state_5.sqlite` `projects` + `project_roots`) + `[projects."<realpath>"] trust_level = "trusted"` in `~/.codex/config.toml`. | Declarative JSON file `~/.gemini/config/projects/<uuid>.json` (`folderUri` / `gitFolder` with `file://<realpath>`). |
| **Workspace-Scoped MCP & Skills Injection** | `<ws>/.codex/config.toml` (`[mcp_servers.parhelion]`) + `<ws>/AGENTS.md` + `<ws>/.agents/skills/*/SKILL.md` (**hands-on verified** once `realpath(ws)` is trusted!). | `<ws>/.agents/plugins/parhelion/{plugin.json, mcp_config.json}` + `<ws>/AGENTS.md` + `<ws>/.agents/skills/*/SKILL.md` (+ optional global `~/.gemini/config/mcp_config.json`). |
| **Live Read-Only Session Observation** | `state_5.sqlite` + `thread_history_1.sqlite` (`file:...?mode=ro`, WAL-aware) + `sessions/**/*.jsonl`. **Measured: `6.1 ms`**. | `conversation_summaries.db` (`file:...?mode=ro`, WAL-aware) + non-blocking `flock` on `presence/<id>.lock` + `brain/<id>/.system_generated/logs/transcript.jsonl`. **Measured: `1.7 ms`**. |

---

## 2. Installed Apps, Binaries & State Inventory (Verified on macOS)

> **Portability rule**: All paths in production code MUST use `Path.home()` / `os.environ.get("CODEX_HOME")` / dynamic bundle probing — **zero** hardcoded usernames.

### 2.1 Antigravity Desktop (2.0)

* **App Bundle**: `/Applications/Antigravity.app` (also check `~/Applications/Antigravity.app`)
  * `CFBundleIdentifier`: `com.google.antigravity`
  * `CFBundleShortVersionString`: `2.34.0`
  * `CFBundleURLTypes`: `["antigravity"]`
  * `CFBundleDocumentTypes`: **None** — `open -a Antigravity <dir>` does **not** register or open folders.
  * `Contents/MacOS/Antigravity` CLI switches (verified in `app.asar` `dist/main.js`): accepts only `antigravity://...`, `--ssh-host=<host>`, `--wsl-distro=<distro>`, `--remote-debugging-port`. No `--project` or `--prompt` CLI flags.
  * Bundled helper runtime: `Contents/Resources/bin/language_server` (embedded Hub web UI + Go backend; do **not** probe its private loopback ports/tokens).
* **Companion CLI (`agy`)**: Not bundled inside `Antigravity.app`. Discovery should check `shutil.which("agy")`, `~/.local/bin/agy`, `~/.antigravity/bin/agy`. When present, `agy` supports `agy acp` (headless JSON-RPC), `agy -p "<prompt>" --project=<id> --output-format json|stream-json`, and `agy --project=<id> -i "<prompt>"` (interactive TUI).
* **Config & State Files** (all under `~/.gemini/` & `~/Library/Application Support/Antigravity/`):
  * `~/.gemini/config/config.json` — global `userSettings` (permissions preset, theme, sidecars).
  * `~/.gemini/config/mcp_config.json` — global MCP servers (`{"mcpServers": {...}}`; supports `stdio` via `command`/`args`/`env` and Streamable HTTP via `url`; legacy SSE is unsupported).
  * `~/.gemini/config/projects/<project_uuid>.json` — **Authoritative project registry** (one JSON file per project).
  * `~/Library/Application Support/Antigravity/app_storage.json` — Electron `StorageManager` key-value store (contains `lastCreatedProjectId`, `new-convo-last-selected-project`, `projectsOrder`, window layouts). **Read-only for Parhelion** — Electron owns write serialization and would race/overwrite external edits.
  * `~/.gemini/antigravity/conversation_summaries.db` — SQLite WAL database indexing all conversations (`conversation_summaries` table + FTS5).
  * `~/.gemini/antigravity/presence/<conversation_id>.lock` — advisory `flock` lock held exclusively while a conversation is actively live.
  * `~/.gemini/antigravity/brain/<conversation_id>/.system_generated/logs/transcript.jsonl` — compact append-only JSONL step/tool trajectory.

### 2.2 Codex Desktop (`ChatGPT.app`)

* **App Bundle**: `/Applications/ChatGPT.app` (also check `/Applications/Codex.app` and `~/Applications/{Codex,ChatGPT}.app`, verifying `Info.plist` `CFBundleIdentifier == "com.openai.codex"`).
  * `CFBundleIdentifier`: `com.openai.codex`
  * `CFBundleDisplayName`: `ChatGPT` (`CFBundleAlternateNames`: `["Codex"]`, `MDItemKeywords`: `"Codex"`)
  * `CFBundleShortVersionString`: `26.1117.41037` (Chromium/Owl Electron runtime; `UserDataDirectoryName=Codex` → `~/Library/Application Support/Codex`)
  * `CFBundleURLTypes`: `["codex", "http", "https"]`
  * `CFBundleDocumentTypes`: accepts `public.folder`, `.skill` (`com.openai.codex.skill`), CSV/XLSX/DOCX/PPTX.
  * CLI flags accepted by GUI process (verified in `app.asar` `P(e)`): `--open-project <path>` / `--open-project=<path>` (maps to `{kind: "newThread", path}`), plus any `codex://...` URL argument.
* **Bundled CLI Binary**:
  * Wrapper: `<AppBundle>/Contents/Resources/codex-cli/bin/codex`
  * Mach-O executable: `<AppBundle>/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex` (`codex-cli 0.171.0-alpha.13`)
  * Key verified subcommands: `codex app [PATH]`, `codex app-server --listen stdio://`, `codex exec`, `codex mcp add|list|remove`, `codex queue --thread <id> --message <text> --resume`, `codex resume`.
  * **Important discovery fix**: On this Mac, `codex` is **not** on default `$PATH`, yet the full `codex-cli 0.171.0-alpha.13` binary is bundled right inside `/Applications/ChatGPT.app`. `codex_driver.py` MUST check bundled app paths after `shutil.which("codex")`!
* **Config & State Files** (under `$CODEX_HOME`, default `~/.codex/`):
  * `~/.codex/config.toml` — global `[mcp_servers.*]` and `[projects."<abs_realpath>"] trust_level = "trusted"`.
  * `<ws>/.codex/config.toml` — workspace-scoped overrides (`[mcp_servers.*]`, sandbox, hooks, model), loaded automatically when `os.path.realpath(ws)` is trusted in `~/.codex/config.toml`.
  * `~/.codex/.codex-global-state.json` — Electron UI state (`local-projects`, `project-order`, `electron-owner-value:selected-project:*`, `thread-project-assignments`). **Read-only for Parhelion** — Electron owns mutations.
  * `~/.codex/state_5.sqlite` — SQLite WAL database (`projects`, `project_roots`, `threads`, `thread_spawn_edges`, `thread_read_receipts`).
  * `~/.codex/thread_history_1.sqlite` — SQLite WAL database (`thread_turns`, `thread_items`, `thread_history_projection_state`).
  * `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<thread_id>.jsonl` & `~/.codex/session_index.jsonl` — rollout event logs (`session_meta`, `event_msg`, `response_item`).
  * `~/.codex/thread-writer-locks/<thread_id>.lock` — `flock` held while `app-server` keeps the thread loaded in memory.

---

## 3. Verified Mechanisms & Hands-On Probe Results

### 3.1 Codex GUI — Verified Mechanisms

#### A. Workspace Sync, Trust & Workspace-Scoped MCP Injection (Hands-On Verified)

Tested in `/tmp/parhelion-gui-probe/codex-ws` with isolated `CODEX_HOME=/tmp/parhelion-gui-probe/codex-home` using the bundled `codex` binary:

1. **Workspace-scoped MCP (`<ws>/.codex/config.toml`)**:
   ```toml
   [mcp_servers.parhelion]
   command = "<abs_path_to_opsb>"
   args = ["mcp", "serve", "--workspace", "<realpath_ws>"]
   ```
   *(Or Streamable HTTP when `opsb daemon` loopback `mcp_url` is active: `url = "http://127.0.0.1:<port>/v1/mcp"`.)*
2. **Project Trust (`~/.codex/config.toml`)**:
   ```toml
   [projects."<os.path.realpath(ws)>"]
   trust_level = "trusted"
   ```
   > **Empirical Gotcha (macOS Symlinks)**: During our probe, trusting `/tmp/parhelion-gui-probe/codex-ws` did **not** unlock `<ws>/.codex/config.toml` in `codex mcp list` because macOS resolves `/tmp` → `/private/tmp`. Once canonicalized to `os.path.realpath(ws)` (`/private/tmp/parhelion-gui-probe/codex-ws`), `codex mcp list` immediately listed both global `parhelion` and repo-scoped `parhelion_ws`, and `codex app-server` `config/read(cwd=real_ws)` merged both cleanly.
3. **Repo Skills & Rules Discovery**:
   * `codex app-server` `skills/list(cwds=[real_ws])` immediately returned `<ws>/.agents/skills/parhelion-probe/SKILL.md` with `"scope": "repo"`.
   * `<ws>/AGENTS.md` (and optional `AGENTS.override.md`) is natively loaded by Codex CLI & Desktop.

#### B. Launching & Opening a Conversation in Codex GUI (Verified in `ChatGPT.app` Router + Isolated `app-server` Probe)

Two complementary, 100% native paths exist:

##### Path 1 (Default Interactive GUI): `codex://threads/new` Deep Link

Verified directly in `ChatGPT.app/Contents/Resources/app.asar` (`G(e)` → `H(e)` → `case "newThread"`):

```text
codex://threads/new?path=<urlencoded_realpath_worktree>&prompt=<urlencoded_brief>&mode=codex[&projectId=<uuid>]
```

* Accepted query parameters (validated by `H(e)` & `K(e)`):
  * `path`: absolute existing directory path (canonicalized with `os.path.realpath`).
  * `projectId`: optional existing local project UUID (looked up read-only from `~/.codex/state_5.sqlite` `projects JOIN project_roots`; bypasses `createProjectForRoot` confirmation when already registered).
  * `prompt`: URL-encoded text inserted into the composer (`prefillPrompt`) with `focusComposerNonce: Date.now()` — **ready for user review + Enter, never auto-submits unexpectedly**.
  * `mode`: enum validated against `["chat", "work", "codex"]` — always pass `mode=codex` for coding/operator work.
* Exact main-process execution sequence inside `ChatGPT.app`:
  1. Brings primary Codex window to front (or launches app).
  2. Resolves `projectId` or checks `stat(path).isDirectory()`.
  3. If no local project owns `path`, invokes `projectsManager.createProjectForRoot(path)` (registers project in `state_5.sqlite` + `.codex-global-state.json` and broadcasts `workspace-root-options-updated` & `global-state-updated` to all windows).
  4. Sets `SELECTED_PROJECT` to `{type: "local", projectId}` in the sidebar.
  5. Navigates renderer to `/` with `{codexAppMode: "codex", focusComposerNonce, prefillPrompt, project}`.
* **Invocation**: `subprocess.run(["open", url], check=True)` — zero screen scraping, zero Accessibility permissions.
* **URL Length Guard**: If `len(url) > 6000` (e.g., long brief), write full brief to `<worktree>/.parhelion/brief.md`, copy full brief via `pbcopy`, and pass compact `prompt=Read .parhelion/brief.md and begin.` in the URL.

##### Path 2 (Pre-Seeded Named Thread / Headless-to-GUI Handoff): `codex app-server` + `codex://threads/<id>`

**Hands-on verified** in isolated `CODEX_HOME`:

1. Start short-lived `codex app-server --listen stdio://` against the user's shared `~/.codex` (default `CODEX_HOME`).
2. JSON-RPC handshake (`initialize` → `notifications/initialized`):
   * `thread/start` with `{"cwd": real_worktree, "persistExtendedHistory": true}` → returns `thread.id` (UUIDv7) with zero LLM API calls!
   * `thread/name/set` with `{"threadId": tid, "name": f"Parhelion · {problem} ({track})"}` → immediately persists row into `state_5.sqlite` (`threads` table) and creates `sessions/YYYY/MM/DD/rollout-...-<tid>.jsonl` (**verified via WAL-aware `mode=ro` query**!).
   * *(Optional autonomous mode)*: call `turn/start` to start executing immediately, OR skip `turn/start` to hand off cleanly to the GUI composer.
3. Open that exact thread directly in Codex Desktop:
   ```text
   codex://threads/<thread_uuid>?prompt=<urlencoded_brief>
   ```
   Verified in `case "localConversation"`: `codex://threads/<uuid>` supports `prompt` (`prefillPrompt` + `focusComposerNonce`), `view=review` (`diffFilter=branch|last-turn`), and `hostId`.
4. **Follow-up nudges to an existing thread**:
   * `codex queue --thread <thread_uuid> --message "<text>" --resume` (or `codex://threads/wake/<thread_uuid>`).

---

### 3.2 Antigravity GUI — Verified Mechanisms

#### A. Workspace / Project Registration & Customizations (`sync_workspace`)

1. **Declarative Project Registration (`~/.gemini/config/projects/<uuid>.json`)**:
   Verified across all 5 live projects on this Mac. Schema:
   ```json
   {
     "id": "<uuid-v4>",
     "name": "<workspace-display-name>",
     "projectResources": {
       "resources": [
         {
           "gitFolder": {
             "folderUri": "file:///abs/realpath/to/repo",
             "defaultBranch": "main"
           }
         }
       ]
     },
     "permissionGrants": {
       "v2Migrated": true
     },
     "settings": {},
     "isWorkspaceOnly": false
   }
   ```
   *(For non-Git folders, use `{"folderUri": "file:///abs/realpath"}` inside `resources`.)*
   * **Idempotency**: Scan existing `~/.gemini/config/projects/*.json`; if any resource's `folderUri` (or `gitFolder.folderUri`) matches `Path(ws).resolve().as_uri()`, reuse its existing `id` instead of creating a duplicate!
   * **Atomic write**: Write to `.tmp` in the same directory and `os.replace()` with `0o644` permissions.
   * **Live-reload caveat**: `language_server` caches loaded projects in memory; an already-registered project (such as `ops-bench`) is immediately addressable by UUID, whereas a brand-new JSON file written while `Antigravity.app` is running may require a window reload or app restart before appearing in the sidebar.
2. **Workspace-Scoped MCP, Rules & Skills (Zero Global Pollution)**:
   Verified from official Antigravity customization docs (`agy-customizations/docs/{plugins,mcp_servers,rules}.md`):
   * **Rules**: `<ws>/AGENTS.md` (hierarchical walk-up discovery; 24 KB per-file cap; preserve existing user rules!).
   * **Skills**: `<ws>/.agents/skills/<name>/SKILL.md` (progressive disclosure).
   * **Workspace Plugin MCP** (`<ws>/.agents/plugins/parhelion/`):
     ```text
     <ws>/.agents/plugins/parhelion/
     ├── plugin.json       # {"name": "parhelion"}
     └── mcp_config.json   # {"mcpServers": {"parhelion": ...}}
     ```
     * With daemon HTTP URL: `{"mcpServers": {"parhelion": {"url": "http://127.0.0.1:<port>/v1/mcp"}}}` (Streamable HTTP).
     * With stdio fallback: `{"mcpServers": {"parhelion": {"command": "<abs_path_to_opsb>", "args": ["mcp", "serve", "--workspace", "<realpath_ws>"]}}}`.
   * **Optional Global MCP** (`~/.gemini/config/mcp_config.json`): Only add/update the `"parhelion"` key when `register_global_mcp=True`, preserving all existing keys and handling empty (`0`-byte) files safely (observed: `~/.gemini/config/mcp_config.json` is currently `0` bytes on this Mac!).

#### B. Launching & Opening a New Conversation in Antigravity GUI (Verified in `Antigravity.app` v2.34.0)

1. **Native Deep Link (`antigravity://open`)**:
   Verified directly in `Antigravity.app` `dist/main.js` (`handleDeepLink`) and Hub UI `main.js` (`tec()` / `z0()` / `HR()` / `y0()`):
   ```text
   antigravity://open?project=<project_uuid>&prompt=<urlencoded_brief>
   ```
   * `dist/main.js` restores/focuses the local Hub window (or opens one) and dispatches the URL over IPC `'deep-link'` (or stores it in `'deep-link:get-stored'` during cold startup).
   * Hub `tec()` accepts `hostname in {"", "open"}`, extracts `project` (exact UUID from `~/.gemini/config/projects/<uuid>.json`) and `prompt`, strips invisible/control Unicode characters, switches Home to `{kind: "newConversation", projectId}`, and inserts `prompt` into the Home composer (`draftKey: "__home__"`, `appendToExisting: true`) and focuses it.
2. **Critical Caveats & Mandatory Defensive Handoff**:
   * **Feature-flag gate**: `tec()` checks `enableDeepLinkHandler` (`default: false` if not flipped on for the user's account via Mendel). If disabled, `open "antigravity://open?..."` still activates and focuses `Antigravity.app`, but the renderer ignores the query params.
   * **Append semantics**: `appendToExisting: true` appends to any unsent Home composer draft.
   * **Mandatory Parhelion Safety Pattern**:
     1. Always persist full brief to `<worktree>/.parhelion/brief.md`.
     2. Always copy full brief to macOS clipboard via `pbcopy` (so even when `enableDeepLinkHandler` is disabled, the user simply presses `Cmd+V` into the focused window — zero friction, zero screen scraping!).
     3. Enforce the same `6000`-char URL length cap (passing a compact `Read .parhelion/brief.md ...` prompt when exceeded).
     4. Invoke `subprocess.run(["open", url], check=True)` (falls back to `open -a Antigravity` if URL launch fails).
3. **Optional `agy` CLI Surface (When Installed)**:
   * Headless ACP: `agy acp` (Track 3 existing protocol).
   * Headless one-shot: `agy -p "<brief>" --project=<project_id> --output-format stream-json`.
   * Interactive TUI: `agy --project=<project_id> -i "<brief>"`.
   * Note: `agy` CLI stores CLI-initiated threads in its CLI session store (separate from the App's `conversation_summaries.db`, though `/resume` → **Other** tab can import App conversations into CLI). Therefore, for true **Antigravity GUI** sessions, `antigravity://open?...` + `~/.gemini/config/projects/<uuid>.json` is the right primary path.

---

## 4. Verified Live Read-Only Session Observation (Benchmarked)

Both observers were hands-on prototyped and benchmarked against the live databases on this Mac using **pure Python 3 stdlib** (`sqlite3`, `fcntl`, `json`, `glob`).

### 4.1 Critical SQLite WAL Mode Rule (Empirically Verified!)

Both `conversation_summaries.db` and `state_5.sqlite` / `thread_history_1.sqlite` operate in **WAL mode**:
* **ALWAYS open with `sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)` + `PRAGMA query_only = ON;`**.
* **NEVER default to `&immutable=1`**: In our isolated Codex probe, `immutable=1` skipped the uncheckpointed `-wal` file and saw **0 tables / 0 threads**, whereas `mode=ro` immediately read the newly created thread in `<1 ms` without blocking writers!
* Only fall back to `mode=ro&immutable=1` if `mode=ro` raises `sqlite3.OperationalError` (e.g., inside a read-only sandbox where SQLite sdkot mmap `-shm`).

### 4.2 Antigravity GUI Observer (`1.7 ms` measured)

1. **Projects map**: Parse `~/.gemini/config/projects/*.json` → `{project_id: {"name", "paths"}}`.
2. **Active lock probe (`flock`)**: For each `~/.gemini/antigravity/presence/<conversation_id>.lock`, open `os.O_RDONLY` and try `fcntl.flock(fd, LOCK_EX | LOCK_NB)`:
   * `BlockingIOError` ⇒ lock is **actively held** by a running/open Antigravity conversation (empirically matched 2/42 locks = the exact 2 live agents running on this Mac!).
   * Otherwise immediately `LOCK_UN` and close (zero mutation).
3. **Summary query (`conversation_summaries.db`)**:
   ```sql
   SELECT conversation_id, title, preview, step_count, status, project_id,
          workspace_uris, not_fully_idle, killed, last_modified_time,
          last_user_input_time, parent_conversation_id, agent_name
   FROM conversation_summaries
   ORDER BY last_modified_time DESC
   LIMIT ?
   ```
4. **Workspace filtering & status normalization**:
   * Parse `workspace_uris` JSON array (`file://...` → `unquote(urlparse(u).path)` → `os.path.realpath`). Match workspace root OR any child path (e.g., `.worktrees/*`).
   * Normalized status:
     * `"running"` if `status == "CASCADE_RUN_STATUS_RUNNING"` or `(presence_lock_held and bool(not_fully_idle))`
     * `"failed"` / `"killed"` if `bool(killed)`
     * `"idle"` otherwise
5. **Optional fine-grained activity**: Tail last 5 lines of `~/.gemini/antigravity/brain/<conversation_id>/.system_generated/logs/transcript.jsonl` to surface latest `step_index`, `type`, `status`, and `tool_calls[].name` on the Parhelion dashboard.

### 4.3 Codex GUI Observer (`6.1 ms` measured)

1. **Projects + Threads query (`~/.codex/state_5.sqlite`)**:
   ```sql
   SELECT p.id, p.name, r.path
   FROM projects p LEFT JOIN project_roots r ON p.id = r.project_id
   ```
   ```sql
   SELECT id, COALESCE(name, substr(title, 1, 80)), cwd, project_id, source,
          model, tokens_used, git_branch, git_sha, updated_at_ms, archived, rollout_path
   FROM threads
   WHERE archived = 0
   ORDER BY updated_at_ms DESC
   LIMIT ?
   ```
2. **Latest turn status (`~/.codex/thread_history_1.sqlite`)**:
   ```sql
   SELECT status, started_at, completed_at, duration_ms, error_json
   FROM thread_turns
   WHERE thread_id = ?
   ORDER BY rollout_ordinal DESC
   LIMIT 1
   ```
   * Normalized status:
     * `"running"` if `turn_status in ("inProgress", "running")`
     * `"failed"` if `turn_status in ("failed", "error")`
     * `"idle"` (or `"completed"`) if `turn_status == "completed"` or no active turn
   * *(Note on `thread-writer-locks/*.lock`)*: Empirically, Codex Desktop's persistent `app-server` holds `flock` on recently loaded threads (9/9 locks held while idle), so `thread_turns.status` + `rollout_path` tail (`task_started` vs `task_complete`/`turn_aborted`) is the authoritative running-vs-idle signal, while `flock` indicates "currently loaded in Codex Desktop memory".
3. **Workspace filtering & 1-click GUI deep link**:
   * Match `os.path.realpath(cwd)` equal to or nested under `os.path.realpath(workspace)` (captures both main workspace and `<problem>/.worktrees/<track>`).
   * Attach `deep_link = f"codex://threads/{thread_id}"` for instant 1-click focus from Parhelion UI.

---

## 5. Exact API Design for `cli/opsb_core/drivers/`

Designed to extend `docs/v3/03-driver-runtime-and-mcp.md` with **100% backward compatibility** for Track 3's existing dataclasses, CLI commands, and unit tests.

### 5.1 Dataclass & `BaseDriver` Extensions (`base.py`)

```python
@dataclass
class DriverDiagnostic:
    kind: str
    display_name: str
    installed: bool
    binary_path: str
    version: str
    auth_ok: bool
    protocol: str
    detail: str
    # --- v3b GUI extensions (backward-compatible defaults) ---
    gui_installed: bool = False
    gui_app_path: str = ""
    gui_bundle_id: str = ""
    gui_version: str = ""
    url_scheme: str = ""

@dataclass
class SessionRecord:
    session_id: str
    driver: str
    workspace: str
    problem: str
    worktree: str
    native_session_id: str
    status: str
    pid: int | None
    created_at: str
    updated_at: str
    brief_preview: str
    # --- v3b GUI extensions (backward-compatible defaults) ---
    launch_mode: str = "headless"   # "headless" | "gui" | "gui-preseed" | "dry-run"
    deep_link: str = ""             # e.g. "codex://threads/<id>" or "antigravity://open?..."
    project_id: str = ""            # Antigravity project UUID or Codex project UUID
    brief_path: str = ""            # "<worktree>/.parhelion/brief.md"
    observed_steps: int = 0
    tokens_used: int = 0
```

Add clean optional methods on `BaseDriver`:

```python
class BaseDriver(ABC):
    # ... existing methods: discover, sync_workspace, build_launch_command, launch_session ...

    def discover_gui(self) -> dict[str, Any]:
        """Inspect installed macOS GUI bundle via Info.plist (read-only)."""
        return {"gui_installed": False, "gui_app_path": "", "gui_bundle_id": "", "gui_version": "", "url_scheme": ""}

    def build_gui_deep_link(
        self,
        worktree: Path,
        prompt: str = "",
        project_id: str | None = None,
        thread_id: str | None = None,
    ) -> str:
        """Build native URL scheme deep link (codex:// or antigravity://)."""
        return ""

    def observe_gui_sessions(
        self,
        workspace: Path | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Read-only observation of live & recent GUI sessions from native local stores."""
        return []
```

### 5.2 Shared Helpers (`gui_utils.py` or inside `base.py`)

* `inspect_macos_app_bundle(candidates: list[Path], expected_bundle_ids: set[str] | None = None) -> dict`
  * Uses Python stdlib `plistlib.load()` (zero `plutil` subprocess overhead!) to extract `CFBundleIdentifier`, `CFBundleShortVersionString`, and `CFBundleURLTypes[0].CFBundleURLSchemes[0]`.
* `open_sqlite_readonly_wal(db_path: Path, timeout: float = 1.0) -> sqlite3.Connection`
  * Opens `file:{db_path}?mode=ro` + `PRAGMA query_only = ON;`, with graceful fallback to `mode=ro&immutable=1` on `sqlite3.OperationalError`.
* `is_flock_held(lock_path: Path) -> bool`
  * Non-blocking `fcntl.flock(fd, LOCK_EX | LOCK_NB)` check; immediately unlocks if acquired.
* `stage_brief_and_clipboard(worktree: Path, brief: str, copy_clipboard: bool = True) -> tuple[Path, bool]`
  * Writes `<worktree>/.parhelion/brief.md` atomically and copies to clipboard via `pbcopy` (skipped in `dry-run` or non-macOS test environments).
* `compact_prompt_for_url(brief: str, brief_path: Path, base_url_len: int, max_url_len: int = 6000) -> str`
  * URL-encodes `brief` directly when total URL stays `<= max_url_len`; otherwise returns compact pointer `f"Read {brief_path} for the full Parhelion brief and execute."`.

### 5.3 Wiring into `codex_driver.py`

1. **`discover()` / `discover_gui()`**:
   * App search order: `/Applications/Codex.app`, `/Applications/ChatGPT.app`, `~/Applications/Codex.app`, `~/Applications/ChatGPT.app` — accept only when `CFBundleIdentifier == "com.openai.codex"`.
   * Binary search order: `shutil.which("codex")` → `<gui_app_path>/Contents/Resources/codex-cli/bin/codex` → `<gui_app_path>/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex`.
   * `installed = bool(binary_path) or gui_installed`.
2. **`sync_workspace(workspace, mcp_url=None, codex_home=None)`**:
   * Canonicalize `real_ws = workspace.resolve()`.
   * Write/merge `<real_ws>/.codex/config.toml` with `[mcp_servers.parhelion]` (HTTP `url` if `mcp_url`, else stdio `opsb mcp serve --workspace <real_ws>`).
   * Ensure `[projects."<real_ws>"]\ntrust_level = "trusted"` in `(codex_home or Path.home() / ".codex") / "config.toml"` (idempotent, preserves all existing TOML tables; skips mutating user's real `~/.codex` when `codex_home` override is passed in tests!).
   * Ensure `<real_ws>/.agents/skills/` and `<real_ws>/AGENTS.md` exist (never overwrite user content).
   * Look up existing `project_id` read-only from `state_5.sqlite` (`project_roots` matching `real_ws`) and return it in the sync dict.
3. **`build_gui_deep_link(worktree, prompt="", project_id=None, thread_id=None)`**:
   * If `thread_id`: `codex://threads/{thread_id}` (+ `?prompt=...` if `prompt`).
   * Else: `codex://threads/new?path={quote(str(worktree.resolve()), safe='')}&mode=codex` (+ `&projectId={project_id}` if known, + `&prompt=...` if `prompt`).
4. **`launch_session(..., mode="gui", resume_id=None, preseed_thread=False)`**:
   * Calls `sync_workspace(workspace)` and `stage_brief_and_clipboard(worktree, brief, copy_clipboard=(mode != "dry-run"))`.
   * If `resume_id`: opens `build_gui_deep_link(worktree, prompt=compact_prompt, thread_id=resume_id)`.
   * Else if `preseed_thread` and `binary_path`: runs short-lived `codex app-server --listen stdio://` (`thread/start(cwd=str(worktree.resolve()), persistExtendedHistory=True)` → `thread/name/set`) against shared `CODEX_HOME`, captures `native_session_id = thread_id`, and opens `codex://threads/{thread_id}?prompt=...`.
   * Else (default `mode="gui"`): opens `codex://threads/new?path=...&mode=codex&prompt=...` via `["open", url]`.
   * Persists `SessionRecord(launch_mode="gui", deep_link=url, project_id=project_id, brief_path=str(brief_path), ...)`.
5. **`observe_gui_sessions(workspace=None, limit=20)`**:
   * Implements §4.3 (`state_5.sqlite` + `thread_history_1.sqlite` WAL-aware read-only observer).

### 5.4 Wiring into `antigravity_driver.py`

1. **`discover()` / `discover_gui()`**:
   * App search order: `/Applications/Antigravity.app`, `~/Applications/Antigravity.app` — verify `CFBundleIdentifier == "com.google.antigravity"`.
   * Binary search order: `shutil.which("agy")`, `shutil.which("antigravity")`, `~/.local/bin/agy`.
   * `installed = bool(binary_path) or gui_installed` (CRITICAL: previously if `agy` was not installed, `AntigravityDriver.discover()` reported `installed=False` even though `Antigravity.app` 2.34.0 is installed and running!).
2. **`sync_workspace(workspace, mcp_url=None, gemini_home=None, register_global_mcp=False)`**:
   * Canonicalize `real_ws = workspace.resolve()`, `folder_uri = real_ws.as_uri()`.
   * Idempotently find or create `(gemini_home or Path.home() / ".gemini") / "config/projects/<uuid>.json"` with `gitFolder` (if `(real_ws / ".git").exists()`) or `folderUri`. Return `project_id` and `created_new_project: bool`.
   * Write workspace-scoped plugin `<real_ws>/.agents/plugins/parhelion/{plugin.json, mcp_config.json}` pointing to `mcp_url` (Streamable HTTP) or stdio `opsb mcp serve --workspace <real_ws>`.
   * Ensure `<real_ws>/.agents/skills/` and `<real_ws>/AGENTS.md` exist.
   * If `register_global_mcp=True`, merge `"parhelion"` into `~/.gemini/config/mcp_config.json` (treating 0-byte file as `{}`).
3. **`build_gui_deep_link(worktree, prompt="", project_id=None, thread_id=None)`**:
   * Builds `antigravity://open?project={project_id}&prompt={urlencoded_prompt}` (omitting `project` if unset).
4. **`launch_session(..., mode="gui", resume_id=None)`**:
   * Calls `sync_workspace(workspace)` to obtain `project_id`.
   * Calls `stage_brief_and_clipboard(worktree, brief, copy_clipboard=(mode != "dry-run"))` — **always** writes `.parhelion/brief.md` AND copies to clipboard as zero-friction fallback in case `enableDeepLinkHandler` is disabled on the user's account.
   * Builds `url = build_gui_deep_link(worktree, prompt=compact_prompt, project_id=project_id)` and invokes `["open", url]` (with fallback `["open", "-a", gui_app_path]` if `open url` exits non-zero).
   * Persists `SessionRecord(launch_mode="gui", deep_link=url, project_id=project_id, brief_path=str(brief_path), ...)`.
5. **`observe_gui_sessions(workspace=None, limit=20)`**:
   * Implements §4.2 (`conversation_summaries.db` WAL-aware read-only + `presence/*.lock` `flock` + optional `brain/*/transcript.jsonl` tail).

### 5.5 CLI & Dashboard Integration (`driver_mcp_cmd.py` & `Parhelion.app`)

* `opsb driver doctor --json`: surfaces both CLI (`installed`, `binary_path`, `version`) and GUI (`gui_installed`, `gui_app_path`, `gui_version`, `url_scheme`) status cleanly.
* `opsb driver launch <problem> --mode gui [--preseed] [--dry-run] [--json]`: runs `sync_workspace` → `prepare_session_worktree` → `compose_session_brief` → native URL scheme launch → prints `{deep_link, project_id, brief_path, clipboard_copied}` (in `--dry-run`, skips `open` and `pbcopy` and returns the exact deep link & commands for automated testing!).
* `opsb driver sessions [--include-gui] [--workspace W] [--json]`: merges Parhelion-tracked `driver_sessions.json` with live `observe_gui_sessions(workspace)` records (deduplicated by `native_session_id`), enabling `Parhelion.app` to display real-time GUI agent activity (`running` / `idle` / `killed`, step count, token usage, last modified time) and a 1-click **Open in Codex / Antigravity** button.

---

## 6. Verification Checklist for Track 3 Follow-Up Implementation

1. **Unit tests (hermetic, temp `HOME` / `CODEX_HOME` / `GEMINI_HOME`, zero real GUI popup)**:
   * `discover_gui()` parses mock `Info.plist` for both `Antigravity.app` (`com.google.antigravity`) and `ChatGPT.app` (`com.openai.codex`) and falls back to bundled `codex-cli/bin/codex`.
   * `CodexDriver.sync_workspace()` writes `<ws>/.codex/config.toml` and canonicalizes `[projects."<realpath>"] trust_level = "trusted"` without clobbering existing TOML entries.
   * `AntigravityDriver.sync_workspace()` idempotently reuses an existing project UUID matching `folderUri`, creates valid JSON when missing, and creates `<ws>/.agents/plugins/parhelion/{plugin.json, mcp_config.json}`.
   * `build_gui_deep_link()` properly URL-encodes spaces/UTF-8, enforces the `6000`-char pointer fallback, and supports both new-thread and existing-thread Codex URLs.
   * `observe_gui_sessions()` reads temp WAL-mode SQLite fixtures for both `conversation_summaries.db` (+ simulated held `flock`) and `state_5.sqlite` + `thread_history_1.sqlite` without creating `-wal`/`-shm` mutations.
2. **Manual smoke test on macOS (user-initiated)**:
   * `opsb driver doctor --json` shows `gui_installed: true` for both `antigravity` and `codex` even with `agy`/`codex` absent from `$PATH`.
   * `opsb driver launch PadV3Grad --driver codex --mode gui --dry-run --json` prints a valid `codex://threads/new?path=...&mode=codex&prompt=...` URL.
   * `opsb driver sessions --include-gui --json` returns live Antigravity & Codex sessions in `<10 ms`.
