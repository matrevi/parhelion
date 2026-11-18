# Parhelion

Native macOS SwiftUI telemetry & Bayesian submission supervisor + modular `opsb` CLI for heterogeneous NPU kernel engineering.

## Architecture

Parhelion separates UI presentation, IPC transport, and scheduling/gate logic into small, single-responsibility modules:

```text
parhelion/
├── macos-app/                         # SwiftPM macOS 14+ App (MVVM + Repository)
│   ├── Package.swift
│   ├── scripts/
│   │   ├── generate_icon.swift        # CoreGraphics vector AppIcon generator
│   │   └── bundle_app.sh              # Release build + codesigned .app bundler
│   └── Sources/OpsSupervisor/
│       ├── App/                       # App entry, window commands & NavigationSplitView router
│       ├── Core/                      # Concurrent deadlock-free ProcessClient, OpsRepository, UIState
│       ├── Domain/                    # Codable domain models (Contest, Queue, NPU, Settings)
│       ├── DesignSystem/              # SurfaceCard, MetricTile, GaugeTile, SemanticPill
│       └── Features/
│           ├── Contest/               # ContestStore, Operator table, 50-case Inspector, Collaborator invite
│           ├── NpuMonitor/            # Live NPU header, interactive SchedulerBar, Server & Submit queues
│           ├── Breakthroughs/         # Configurable Δ filter & structured KernelC optimization playbook UI
│           ├── Bridge/                # Experience hand-off console (待总结 / 已取 / 已回执 per agent)
│           ├── Workspaces/            # Workspace switcher & clean AGENTS.md-first wizard
│           └── Settings/              # SSH config, shared Judge/GitCode auth, dsh/codex toolchain
├── breakthrough/                      # Structured KernelC optimization playbook (schema.json + index.json + entries/*.md)
├── bridge/                            # Agent-agnostic breakthrough hand-off (agents.json + inbox/ + outbox/ + ledger.jsonl)
├── cli/
│   ├── opsb                           # Thin executable entrypoint (~/.local/bin/opsb)
│   └── opsb_core/                     # Layered Python package
│       ├── config.py                  # ConfigRepository & mtime/TTL caches
│       ├── ipc_bridge.py              # Adapter to scheduler.cli_bridge
│       ├── probe/npu_probe.py         # <0.5s read-only remote NPU & flock probe
│       ├── services/                  # Contest, Queue, Multiplexed-SSH NPU, Auth, SSH, Skills, Scaffolder
│       └── commands/                  # Modular CLI subcommand routers
├── scheduler/                         # Paired-kperf Bayesian estimator, 4-stage gate & WSPT NPU scheduler
└── skills/
    └── kernelc-deep-reference/        # Optional fallback reference skill (dsh & codex only)
```

## Quick Start

```bash
# Build & package native macOS app -> dist/OpsSupervisor.app
bash macos-app/scripts/bundle_app.sh
open dist/OpsSupervisor.app

# Install CLI symlink & deploy read-only probe to NPU host
ln -sf "$(pwd)/cli/opsb" ~/.local/bin/opsb
opsb npu agent-install

# Query status, operator history & structured breakthroughs
opsb status                     # includes scheduler.backfill (B0 auto-backfill lane, 200 ms cap)
opsb status --no-backfill       # opt out of the backfill probe entirely
opsb bench backfill-status      # rich B0 lane view (unpaired buckets, queue, running, ETA)
opsb history <Operator>
opsb queue list --breakthrough --min-delta 5.0
opsb breakthrough list
opsb breakthrough show <ID|Operator>

# Hand a breakthrough to subscribed agents (idempotent; auto-runs on every sync)
opsb bridge status                      # 待总结 / 已取 / 已回执 per agent
opsb bridge sync                        # route new entries into bridge/inbox/<agent>/
opsb bridge pull <agent_id> --ack --json
opsb bridge publish <agent_id> <entry_id> --file -   # <=5 line receipt -> outbox/
opsb bridge collect                     # fold receipts into breakthrough/index.json#summaries
```

## Breakthrough hand-off bridge

`opsb breakthrough sync` (and the app's 「同步」 button, `opsb breakthrough add`, and the
auto-archiver after an online `Pass`) automatically routes each new structured entry to the
agents subscribed in [`bridge/agents.json`](bridge/agents.json), keyed by `scope.paths`
prefix / `scope.tags` intersection / an explicit `notify: [...]` in the entry. Delivery is
deduplicated on the entry document's SHA-256, so re-syncing never re-delivers.

Three integration modes — directory watching, `opsb bridge pull --json`, and `exec`/`http`
push hooks — with copy-paste examples live in [`bridge/README.md`](bridge/README.md).
