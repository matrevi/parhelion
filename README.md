# Parhelion

Native macOS SwiftUI telemetry & Bayesian submission supervisor + modular `opsb` CLI for heterogeneous NPU kernel engineering.

## Architecture

Parhelion is a standalone, workspace-agnostic macOS application. All user configuration, credentials, per-workspace benchmark specs, scheduler runtime state, and cross-contest breakthroughs live cleanly under `~/.parhelion/` (`PARHELION_HOME`), never inside the application repository or `.app` bundle.

```text
parhelion/
├── Package.swift                      # SwiftPM macOS 14+ App (`Parhelion`)
├── Sources/Parhelion/                 # Native SwiftUI UI (App, Core, Domain, DesignSystem, Features)
├── scripts/
│   ├── generate_icon.swift            # CoreGraphics vector AppIcon generator
│   ├── bundle_app.sh                  # Release build + codesigned `/Applications/Parhelion.app` bundler
│   └── build_dmg.sh                   # Compressed UDZO `.dmg` + SHA-256 release packager
├── cli/
│   ├── opsb                           # Thin executable entrypoint (`~/.local/bin/opsb`)
│   └── opsb_core/                     # Layered Python CLI + local HTTP/SSE telemetry daemon
├── scheduler/                         # Paired-kperf Simplex-MAP estimator, 4-stage gate & WSPT scheduler
├── runtime/                           # Generic 3-phase NPU runner (`npu-test.py`, `npu_runtime/`) & judge client
├── templates/
│   └── breakthrough/schema.json       # Empty schema template (zero contest data)
├── skills/
│   └── kernelc-deep-reference/        # Optional generic KernelC reference skill
└── docs/
    ├── SCHEDULER_DESIGN.md
    ├── BENCH_EVOLUTION_DESIGN.md
    └── REALTIME_TELEMETRY_PLAN.md
```

## Runtime Data Layout (`~/.parhelion/`)

```text
~/.parhelion/
├── config.json                        # Registered workspaces, active workspace, SSH hosts & gate config
├── cj_accounts.json                   # Multi-account judge credentials (0600)
├── daemon.json / daemon.lock          # Localhost HTTP/SSE daemon endpoint & bearer token
├── cache/                             # TTL & code-fingerprint caches
├── logs/                              # Daemon & submission worker logs
├── sessions/                          # Auth sessions
├── breakthrough/                      # Cross-contest shared structured KernelC playbook
│   ├── schema.json
│   ├── index.json
│   └── entries/*.md
└── workspaces/
    └── <slug>-<sha1[:8]>/             # Per-workspace isolated specs & scheduler state
        ├── meta.json
        ├── bench-specs/               # Per-problem cases_*.json & bench_feedback.json
        └── state/                     # scheduler_state.json, backfill_ledger.jsonl, verified_escapes.json, agent_bus.db
```

## Multi-Harness Agent Mesh (v3.2)

The fifth sidebar tab「Agent 协作流 / Agent Mesh」renders the whole collaboration topology on one canvas:
the active Harness as orchestrator in the centre, its native subagents on dashed branches, and
cross-Harness external workers on solid bidirectional Bézier lanes (pulses travel on the arcs).

- **Bus & lifecycle** — `opsb daemon` owns an `AgentMeshManager`; agent state and the monotonic message
  log live in a stdlib SQLite WAL file at `~/.parhelion/workspaces/<id>/state/agent_bus.db`.
  Every worker runs as its own process group (`-pgid` teardown) in an isolated git worktree
  (`readonly` / `shared` modes available).
- **HTTP control plane** — `GET /v1/agents/pool`, `POST /v1/agents/spawn|wait|directive|cleanup`,
  `POST /v1/agents/<id>/send|stop|report`, `GET /v1/agents/<id>/messages|inbox`; all bearer-token guarded.
- **Role-scoped MCP** — orchestrator sees 15 `parhelion_*` tools, a worker sees 11
  (`parhelion_agent_report` / `parhelion_agent_inbox`, no orchestration tools).
  `parhelion_agent_spawn` rejects `driver == active_driver` (homogeneous subagents stay native),
  and `parhelion_agent_wait` defaults to 25 s with a hard 45 s ceiling.
- **Zero-poll UI** — the compact `agent_mesh` projection rides the existing 1 Hz `/v1/stream/hot` SSE frame;
  full message history is fetched only when a node is selected.

## Contest Problem Scraping

A contest link (or bare contest name / Judge object id) resolves to the real problem set through
three read-only public endpoints, so a new workspace never starts from placeholder operators:

```bash
opsb workspace init ~/Competitions/<contest> --contest-url https://judge.invalid/public/<contest> --activate
opsb workspace sync-problems ~/Competitions/<contest>        # refresh an existing workspace
```

`/api/groups/public` → `/api/contests/group/<group_id>` → `/api/problems/contest/<contest_id>`
yields titles, slugs, difficulty tiers, kernel pattern and judge ids; existing repo slugs and paths
are preserved on re-sync, and removed problems are reported (their directories are never deleted).

## Build, Install & Release DMG

```bash
chmod +x scripts/bundle_app.sh scripts/build_dmg.sh
./scripts/bundle_app.sh --install      # Build & install /Applications/Parhelion.app + ~/.local/bin/opsb
./scripts/build_dmg.sh                 # Package dist/Parhelion-v<ver>-<arch>.dmg + .sha256
```

This builds `Parhelion.app`, installs it to `/Applications/Parhelion.app`, and symlinks `~/.local/bin/opsb`.
