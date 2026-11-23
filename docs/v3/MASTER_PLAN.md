# Parhelion v3 Master Architecture & Implementation Plan

> **Positioning**: Parhelion is a standalone, workspace-agnostic **Dashboard + Fast-Feedback Toolbox + MCP Platform + Self-Evolving Triad Knowledge Base + Single-Driver Launcher** (`~/Developer/personal/parhelion`, `/Applications/Parhelion.app`, `~/.parhelion/`). Heavy kernel reasoning and code editing stay in the user's chosen external Harness (`Antigravity`, `Codex`, `Claude Code`, `Pi`, `Gemini CLI`, `DeepSeek Harness`).

---

## 1. Global 4-Layer Architecture

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│ Layer 4 · Parhelion macOS SwiftUI App (/Applications/Parhelion.app)        │
│   Live <5ms NPU Telemetry · 4-Stage Gate · ELF/Cold3 Redline Badges        │
│   Triad Knowledge Hub (Breakthrough / Snippets / Community) · Driver Panel │
└───────────────────────────────┬─────────────────────────────────────────────┘
                                │ Localhost HTTP/SSE (:18910) + opsb CLI
┌───────────────────────────────▼─────────────────────────────────────────────┐
│ Layer 3 · Parhelion Control Plane & MCP Platform (`opsb daemon` + `opsb`)  │
│   ├── Unified Driver Runtime (`antigravity` / `codex` / `claude` / `pi` …) │
│   │     sync_workspace(ws) · launch_session(worktree, brief) · resume      │
│   ├── Local MCP Server (`opsb mcp serve` stdio + `/v1/mcp` loopback front) │
│   └── 4 Fast-Feedback Tools (`elf_guard`, `cold3`, `pmu_card`, `scaffold`) │
└───────────────────────────────┬─────────────────────────────────────────────┘
                                │ Evidence-Gated Read / Write / Promote
┌───────────────────────────────▼─────────────────────────────────────────────┐
│ Layer 2 · Self-Evolving Triad Knowledge Base (`~/.parhelion/`)             │
│   ├── breakthrough/   Verified contest wins + escape postmortems           │
│   ├── snippets/       Copy-ready KernelC kernel building blocks (.h)       │
│   └── community/      Layered GPU/CUDA→Accel cards (portable/remap/ban)   │
└───────────────────────────────┬─────────────────────────────────────────────┘
                                │ Per-Workspace Isolation
┌───────────────────────────────▼─────────────────────────────────────────────┐
│ Layer 1 · Workspace Runtime & NPU Execution (`~/.parhelion/workspaces/<id>`)│
│   bench-specs/ · state/ · driver_sessions.json · runtime/{npu-test,judge}  │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Design Principles Adopted from Linger (`driver-runtime`) & Simplifications
- **Reuse**:
  1. Single tiny `Driver` interface (`discover`, `sync_workspace`, `launch_session`, `resume_session`) backed by native stdio protocols (`agy acp`, `codex app-server`, `claude -p stream-json`, `pi --mode rpc`) rather than brittle GUI automation.
  2. **CLI-first + Focused MCP**: Keep bulk/admin flows in `opsb`, expose only ~9 high-signal tools via MCP so Harness context stays lean.
  3. **Prompt-Cache-Safe Knowledge Injection**: Project static skills/rules read-only (`0400`) to `.agents/skills`, and inject dynamic KB digests + PMU bottleneck cards into the **first user message** of a session (never mutating the system prompt).
  4. **Evidence-Gated Knowledge Evolution**: Harnesses can propose/promote/demote KB entries via MCP, but every write must attach verifiable `SHA` + local `profiler` / online score evidence.
- **Explicitly Omitted** (avoiding cloud multi-tenant bloat):
  - No remote WebSocket gateway, no cross-machine ledger replay, no 8-slot resident process parking pool (`residentTable` — operator tuning is long-running, fresh spawn + native `resume` is simpler and safer), and no Cua/Playwright desktop UI scraping.

---

## 2. Detail Specifications Index

| Track | Branch / Worktree | Owner Scope | Detail Spec |
|---|---|---|---|
| **Track 1** | `v3/fast-tools` | `elf_guard`, `cold3_probe`, `pmu_torch_card`, `bench_scaffold` | [01-fast-feedback-tools.md](01-fast-feedback-tools.md) |
| **Track 2** | `v3/triad-kb` | `~/.parhelion/{breakthrough,snippets,community}`, evidence-gated evolution, `opsb kb`, seeded KernelC snippets & community cards | [02-triad-knowledge-base.md](02-triad-knowledge-base.md) |
| **Track 3** | `v3/driver-mcp` | Unified single-Driver runtime (`antigravity`/`codex`/`claude`/`pi`/`gemini`/`deepseek`) + Parhelion MCP server (`stdio` + loopback HTTP) | [03-driver-runtime-and-mcp.md](03-driver-runtime-and-mcp.md) |
| **Track 4** | `v3/macos-ui` | SwiftUI Triad Knowledge Hub, Driver control & one-click Launch Session, ELF/Cold-3 redline badges, `.app` release build | [04-parhelion-ui-integration.md](04-parhelion-ui-integration.md) |

---

## 3. Zero-Conflict File Ownership Matrix & Execution DAG

### Wave 1 (3 Parallel Hydrogen Workers in Isolated Git Worktrees)
Each worker works ONLY inside its dedicated worktree under `.worktrees/` and touches ONLY its owned files:
- **`v3-fast-tools`** (`.worktrees/v3-fast-tools`, branch `v3/fast-tools`):
  - `cli/opsb_core/services/fast_feedback_service.py` (new)
  - `cli/opsb_core/commands/tools_cmd.py` (new, exports `register_fast_tools_subcommands(bn_sub)`)
  - `runtime/npu-test.py` (additive `--cold3` + template instantiation / ELF redline summary)
  - `cli/tests/test_fast_feedback.py` (new)
- **`v3-triad-kb`** (`.worktrees/v3-triad-kb`, branch `v3/triad-kb`):
  - `cli/opsb_core/services/triad_kb_service.py` (new)
  - `cli/opsb_core/commands/kb_cmd.py` (new, exports `register_kb_subcommands(sub)`)
  - `templates/snippets/*` & `templates/community/*` (new)
  - `cli/tests/test_triad_kb.py` (new)
- **`v3-driver-mcp`** (`.worktrees/v3-driver-mcp`, branch `v3/driver-mcp`):
  - `cli/opsb_core/drivers/*` (new package)
  - `cli/opsb_core/mcp/*` (new package)
  - `cli/opsb_core/commands/driver_mcp_cmd.py` (new, exports `register_driver_and_mcp_subcommands(sub)`)
  - `cli/tests/test_driver_runtime.py` & `cli/tests/test_mcp_server.py` (new)

### Integration Gate (Coordinator)
1. Merge `v3/fast-tools`, `v3/triad-kb`, `v3/driver-mcp` into `main` (clean 0-conflict merges).
2. Wire the 3 `register_*` hooks into `cli/opsb_core/cli_app.py`, mount `/v1/mcp` JSON-RPC endpoint on `cli/opsb_core/daemon/server.py`, and add `snippets`/`community` path helpers in `cli/opsb_core/config.py`.
3. Seed `~/.parhelion/{snippets,community}` non-destructively from `templates/`.
4. Run `~/.parhelion/venv/bin/python -m pytest cli/tests scheduler/tests -q` — must pass 100%.

### Wave 2 (Hydrogen UI Worker after Integration Gate)
- **`v3-macos-ui`** (`.worktrees/v3-macos-ui`, branch `v3/macos-ui`):
  - `Sources/Parhelion/**`
  - Builds release `Parhelion` via `swift build -c release`, merges cleanly into `main`.

### Final Release Verification (Coordinator)
- Remove all `.worktrees/`, run full `pytest`, run `./scripts/bundle_app.sh --install`, restart `opsb daemon` + `/Applications/Parhelion.app`, smoke-test `opsb bench elf|pmu`, `opsb kb query`, `opsb driver doctor`, `opsb mcp list-tools`, and verify zero personal hardcodes or contest data in repo.
