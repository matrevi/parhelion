# Track 2 Spec · Self-Evolving Triad Knowledge Base (`breakthrough` + `snippets` + `community`)

> **Branch**: `v3/triad-kb` | **Worktree**: `.worktrees/v3-triad-kb`
> **Owned Files**: `cli/opsb_core/services/triad_kb_service.py`, `cli/opsb_core/commands/kb_cmd.py`, `templates/snippets/*`, `templates/community/*`, `cli/tests/test_triad_kb.py`

---

## 1. Goals
Transform `~/.parhelion/` into a **bidirectional, evidence-gated Triad Knowledge Base** that Harnesses can both **query by hardware symptom** and **evolve with verified wins or negative results**:

```text
~/.parhelion/
├── breakthrough/          # Pillar 1: Verified contest wins & escape postmortems (existing)
├── snippets/              # Pillar 2: Copy-ready minimal KernelC kernel building blocks (.h + index.json)
└── community/             # Pillar 3: Layered GPU/CUDA/Accel community techniques (index.json + entries/*.md)
```

---

## 2. Unified Knowledge Schema & Evolution Lifecycle

Every entry across all 3 pillars exposes normalized metadata when queried:
```json
{
  "id": "SNP-zero-tpipe-ub-arena | COM-cuda-online-softmax | BT-20261122-...",
  "pillar": "breakthrough | snippets | community",
  "title": "...",
  "summary": "...",
  "bottlenecks": ["cold-launch-tax", "mte2-bound", "bank-conflict", "scalar-bound", "icache-bloat"],
  "regimes": ["S_SMALL_CORE", "M_MID_UB", "L_HBM_TILED", "X_EXTREME"],
  "dtypes": ["float32", "float16", "bfloat16"],
  "portability": "native-accel | portable | remap-to-accel | anti-pattern",
  "status": "verified | unverified | deprecated | anti-pattern",
  "confidence": 0.85,
  "wins": 4,
  "fails": 0,
  "evidence": [{"workspace": "...", "problem": "...", "sha": "...", "delta_raw": 7.9, "delta_us": -1.8, "verdict": "win|fail", "recorded_at": "..."}],
  "related_snippets": ["SNP-zero-tpipe-ub-arena"],
  "doc_path": "..."
}
```

### Mandatory Evidence-Gated Evolution Rules
1. **No Hallucinated Writes**: `record_feedback()` / `promote_entry()` REQUIRES an `evidence` payload containing `problem`, `sha` (7–40 hex), `verdict` (`win` | `fail`), and at least one measurable metric (`delta_raw`, `delta_us`, or `note` citing `profiler`/`online`). Unbacked writes raise `ValueError`.
2. **Confidence Formula (Bayesian Beta Mean)**:
   - Prior: `alpha0 = 2.0, beta0 = 1.0` for `native-accel`/`verified`; `alpha0 = 1.0, beta0 = 1.0` for `unverified` community entries.
   - `confidence = round((alpha0 + wins) / (alpha0 + beta0 + wins + fails), 3)`.
3. **Automatic State Transitions**:
   - `community` entry (`status="unverified"`) receiving a verified `win` with `delta_raw >= 1.5` or `speedup >= 1.08` automatically promotes to `status="verified"` (and optionally creates/links a new `snippets/` header!).
   - Any entry accumulating `fails >= 3` and `wins == 0` (or explicit `verdict="anti-pattern"` with evidence) automatically demotes to `status="anti-pattern"` and surfaces as a red **WARNING / DO-NOT-REPEAT** item when future Harnesses query the same bottleneck!
4. **Atomic Persistence + Evolution Audit Log**:
   - All writes use temp-file + `os.replace()` with `fcntl.flock` lock.
   - Every mutation appends an audit row to `~/.parhelion/kb-evolution.jsonl` so Parhelion UI can show the live Knowledge Evolution timeline and support rollback (`rollback_last` / `revert_event`).

---

## 3. Service API (`cli/opsb_core/services/triad_kb_service.py`)

1. `ensure_triad_kb_seeded(parhelion_home: Path | None = None, templates_root: Path | None = None) -> dict`
   - Non-destructively seeds `~/.parhelion/snippets/` and `~/.parhelion/community/` from `SUPERVISOR_ROOT / "templates" / {snippets,community}` if missing. Never overwrites user-evolved entries.
2. `query_kb(bottlenecks: list[str] | None = None, regime: str | None = None, dtype: str | None = None, pillar: str | None = None, text: str = "", include_anti_patterns: bool = True, limit: int = 8) -> dict`
   - Scores entries across all 3 pillars by tag overlap × `confidence` (weighted boost for `verified`, separate `anti_patterns` warning bucket so Harnesses always see known traps first!).
3. `format_prompt_cache_digest(query_result: dict, max_chars: int = 2400) -> str`
   - Formats a tight Markdown briefing block intended for injection into the **first user message** of a new Harness session (preserving system prompt KV cache, per Linger's pattern).
4. `record_kb_entry(pillar: str, payload: dict) -> dict`
   - Adds a new `snippets` or `community` entry (or negative-result anti-pattern) with required evidence validation; writes code file (`snippets/code/<id>.h`) or Markdown doc (`community/entries/<id>.md`) + updates `index.json` + appends `kb-evolution.jsonl`.
5. `record_kb_feedback(entry_id: str, verdict: str, evidence: dict, promote_snippet_code: str | None = None) -> dict`
   - Updates `wins`/`fails`, recomputes `confidence`, runs promotion/demotion rules, optionally creates linked snippet, and logs to `kb-evolution.jsonl`.
6. `project_readonly_skills(workspace: Path, target_rel: str = ".agents/skills") -> dict`
   - Projects canonical read-only (`0400`) skill/KB index files into `<workspace>/.agents/skills/parhelion-kb/` and symlinks `.claude/skills` if needed (following Linger's single canonical skills layout).

---

## 4. Seeded Initial Knowledge Assets (`templates/`)

### 4.1 `templates/snippets/` (5 Battle-Tested KernelC SoC Building Blocks)
1. `SNP-zero-tpipe-ub-arena.h` — Zero-`TPipe`/`TBuf` static aligned UB Arena (`LocalTensor` direct overlay, full 192KB budget, zero ctor/zero-init tax).
2. `SNP-direct-gm-tiling-64b.h` — 64-byte single-cacheline `struct` + `SetDataSize(actualBytes)` + `const __gm__` direct dereference (`GET_TILING_DATA` replacement).
3. `SNP-ring4-async-pipe.h` — 4-slot Ring Buffer `MTE2 ⇄ PIPE_V ⇄ MTE3` explicit `SetFlag/WaitFlag` double/quad overlap template.
4. `SNP-transdata-5hd-transpose.h` — `TransDataTo5HD<uint16_t>` 16×16 SIMD matrix transpose skeleton replacing scalar/gather loops.
5. `SNP-noinline-cold-pad.h` — `__attribute__((noinline))` cold-path `DataCopyPad` + micro-entry dispatcher keeping hot `.text` under 8 KB (32 KB I-Cache safe).

### 4.2 `templates/community/` (Curated GPU/CUDA → Accel Layered Cards)
At least 6 high-value cards strictly tagged with `portability`:
- `COM-online-welford-softmax` (`portable` — single-pass online reduction, eliminates extra GM pass)
- `COM-diagonal-l2-swizzle` (`portable` — diagonal tile swizzle avoiding L2/HBM bank partition camping)
- `COM-warp-shuffle-to-5hd-ub` (`remap-to-accel` — translating CUDA warp-shuffle/SMEM reductions to 256B `WholeReduce`/`BlockReduce` + 48-Bank coloring)
- `COM-vectorized-float4-to-32b-burst` (`remap-to-accel` — CUDA `float4` coalescing mapped to Accel 32B-aligned contiguous MTE2/MTE3 bursts)
- `COM-cuda-fine-branch-anti-pattern` (`anti-pattern` — warning against per-thread fine branching / uncoalesced scalar gather on AI Core)
- `COM-10core-dvfs-bimodal-trap` (`anti-pattern` — SoC 10-core first-2-run DVFS spike trap; prefer `1/20/40` divisor ladder)

---

## 5. CLI Subcommands (`cli/opsb_core/commands/kb_cmd.py`)
Export `register_kb_subcommands(sub)` registering `opsb kb`:
- `opsb kb query [--bottleneck TAG] [--regime R] [--dtype D] [--pillar P] [--text Q] [--digest] [--json]`
- `opsb kb list [--pillar breakthrough|snippets|community|all] [--status S] [--json]`
- `opsb kb show <entry_id> [--json]`
- `opsb kb record --pillar snippets|community --title T --summary S --problem P --sha SHA --verdict win|fail [--bottleneck B] [--portability P] [--code-file F] [--delta-raw X] [--json]`
- `opsb kb feedback <entry_id> --verdict win|fail|anti-pattern --problem P --sha SHA [--delta-raw X] [--note N] [--json]`
- `opsb kb history [--limit N] [--json]`

---

## 6. Verification & Tests (`cli/tests/test_triad_kb.py`)
- Test seeding into isolated `PARHELION_HOME` (non-destructive on re-run).
- Test `query_kb` ranking across all 3 pillars + anti-pattern warning surfacing + `format_prompt_cache_digest`.
- Test evidence gate rejects unbacked writes (`ValueError`), accepts backed wins, promotes `unverified` community card → `verified`, and demotes repeated failures → `anti-pattern`.
- Test `kb-evolution.jsonl` audit logging.
- Run `~/.parhelion/venv/bin/python -m pytest cli/tests/test_triad_kb.py -q` — 100% pass.
