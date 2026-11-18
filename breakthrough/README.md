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

## Agent Receipts (`index.json#summaries`)

Every entry can be handed to subscribed agents through the bridge
([`../bridge/README.md`](../bridge/README.md)). Their ≤5-line summaries are folded back into an
**additive** top-level field — `entries` is never rewritten by this path:

```json
"summaries": [
  {
    "entry_id": "BT-20261118-LayerNormV3-af0807c",
    "agent_id": "codex-cross-review",
    "problem": "LayerNormV3",
    "entry_sha": "259fb59d…",
    "summary": "① …\n② …\n③ …",
    "lines": 3,
    "summary_path": "bridge/outbox/codex-cross-review/BT-20261118-LayerNormV3-af0807c.md",
    "collected_at": "2026-11-18T09:12:00+00:00"
  }
]
```

```bash
opsb bridge sync          # deliver new entries (idempotent, also runs inside `opsb breakthrough sync`)
opsb bridge pull <agent> --ack --json
opsb bridge publish <agent> <entry-id> --file -
opsb bridge collect       # -> index.json#summaries
opsb bridge status --json # per-agent 待总结 / 已取 / 已回执
```
