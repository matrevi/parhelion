# Parhelion · Structured Breakthrough Playbook (`breakthrough/`)

This directory stores **verified, structured KernelC operator optimization breakthroughs** — turning raw online score jumps (`ΔS >= breakthrough_threshold`) into reusable engineering patterns that humans and autonomous operator agents can query before tuning new kernels.

## Directory Layout

```text
breakthrough/
├── README.md        # Playbook overview & CLI workflow
├── schema.json      # JSON Schema (2020-12) for structured experience records
├── index.json       # Machine-readable catalog queried by `opsb` & OpsSupervisor.app
└── entries/         # Detailed structured case studies (YAML header + Markdown sections)
    ├── BT-20261118-LayerNormV3-af0807c.md
    ├── BT-20261118-CTCLossV2-0d5125c.md
    ├── BT-20261110-MultilabelMarginLoss-59f8065.md
    ├── BT-20261118-PadV3Grad-49f905f.md
    └── BT-20261110-PadV3Grad-0fc367d.md
```

## What Counts as a Breakthrough?

To prevent false positives from zero-recoveries (`WA/TLE -> 0 -> restore`) or re-submissions of already-evaluated SHAs:
1. **Novelty**: First online `Pass` evaluation of `(problem, sha)`, excluding rollback/restore re-submissions.
2. **Frontier Gain**: `delta_raw = score_after - last_valid_pass_score >= breakthrough_threshold` (default `5.0` pts on the 100-pt problem scale, configurable in both CLI and macOS App), while advancing past prior peak performance.
3. **Candidate Preview**: Queued/validated commits whose `estimated_score - current_score >= breakthrough_threshold` appear as `预计突破 (Candidate)` in the filter so operators can jump-queue (`插队提交`) high-leverage commits immediately.

## CLI Reference (`opsb`)

```bash
# List breakthrough submissions & structured experience records (uses configured threshold)
opsb breakthrough list
opsb breakthrough list --min-delta 3.0 --category vectorization
opsb queue list --breakthrough --min-delta 5.0

# Inspect a full structured experience document
opsb breakthrough show BT-20261118-LayerNormV3-af0807c
opsb breakthrough show CTCLossV2

# Read or update the shared App + CLI breakthrough threshold
opsb breakthrough threshold
opsb breakthrough threshold 5.0

# Auto-sync newly judged high-Δ submissions into `breakthrough/`
opsb breakthrough sync --min-delta 5.0

# Record or enrich a structured experience entry
opsb breakthrough add LayerNormV3 af0807c \
  --category simd-transpose \
  --title "BF16 TransDataTo5HD SIMD Accumulate + Ping-Pong Single-TBuf Arena" \
  --bottleneck "Sequential scalar/per-row bf16 accumulation stalled PIPE_V" \
  --mechanism "Transpose 16 rows via TransDataTo5HD so vertical SIMD Add reproduces exact step-by-step bf16 rounding across 16 rows simultaneously" \
  --precision-guard "Preserves numpy sequential bf16 rounding order bit-for-bit on (5, 4097)" \
  --rule "When reference requires non-associative sequential rounding per row, transpose rows into SIMD lanes rather than falling back to scalar loops"
```

## Maintenance & Distillation Workflow

- **Automatic skeleton capture**: After an online `Pass` exceeding the configured threshold (`Δ >= breakthrough_threshold`), `opsb` records a baseline entry via `opsb breakthrough sync`.
- **Subagent-driven distillation & cross-operator transfer**: Deep analysis (verifying exact kernel/tiling commits, measured `profiler` before/after numbers, precision/synchronization invariants, and reusable KernelC rules) and cross-operator propagation are handled in the background by the main agent dispatching subagents, which update `index.json` and `entries/*.md` via `opsb breakthrough add` or direct edits.
