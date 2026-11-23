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
│   └── bundle_app.sh                  # Release build + codesigned `/Applications/Parhelion.app` bundler
├── cli/
│   ├── opsb                           # Thin executable entrypoint (`~/.local/bin/opsb`)
│   └── opsb_core/                     # Layered Python CLI + local HTTP/SSE telemetry daemon
├── scheduler/                         # Paired-kperf Simplex-MAP estimator, 4-stage gate & WSPT scheduler
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
        └── state/                     # scheduler_state.json, backfill_ledger.jsonl, verified_escapes.json
```

## Build & Install

```bash
chmod +x scripts/bundle_app.sh
./scripts/bundle_app.sh --install
```

This builds `Parhelion.app`, installs it to `/Applications/Parhelion.app`, and symlinks `~/.local/bin/opsb`.
