"""Markdown protocol & spec templates v2 (3-Phase NPU pipeline + Stratified Bench + Closed-Loop Calibration)."""

WORKSPACE_AGENTS_MD = """# {contest_title} · Agent Protocol (`AGENTS.md`-First · 3-Phase NPU Pipeline)

This workspace uses a **zero-noise directory layout** powered by **`opsb`** and **`Parhelion.app`** (`~/.parhelion/`):
- **Phase A (32-Core Lock-Free CPU Build, `0s` NPU lock)**: Up to `8–10` operators compile concurrently without touching `device0.lock`.
- **Phase B (`PersistentArenaRunner` + Single-Session `profiler`, `~13–24s` NPU lock)**: Sub-minute WSPT `Q0`/`Q1` ticket execution on Die 0.
- **Phase C (Lock-Free Golden Diff & Simplex MAP Calibration, `0s` NPU lock)**: Out-of-tree `results/<Op>/calibration.json` updates automatically on every online evaluation.

## Quick CLI Reference (`opsb`)

```bash
opsb status                                   # Contest scores (with tbest γ & Model MAE), NPU duty & 3-lane queues
opsb test <Op> --build-only                   # Phase A only: 0s NPU lock, fast -j4 compile check
opsb test <Op> --fast                         # Q0 fast-lane (~10s lock): sample + smoke + quick timing
opsb test <Op>                                # Q1 full gate (~18s lock): adversarial arena + full-spectrum profiler
opsb bench status <Op>                        # 5-stratum learned weights w_hat, efficiency r_hat & headroom pts
opsb bench diagnose <Op>                      # Diagnose predicted-vs-actual score residuals & uncovered branches
opsb submit <Op> <SHA> --note "<summary>"     # 4-Stage Gate check & enqueue (NEVER run custom submit scripts)
opsb breakthrough list                        # Inspect verified cross-operator KernelC breakthroughs
```

## 3 Benchmark Integrity Rules

1. **Frozen SHA-256 Oracle**: `bench/reference.py` is SHA-256 locked in `opsb.workspace.json`; never weaken tolerances or alter golden logic.
2. **Append-Only Perf Buckets**: `bench/cases_perf.json` (`PUBLIC`, `S_SMALL_CORE`, `M_MID_UB`, `L_HBM_TILED`, `X_EXTREME`) is append-only so cross-commit ratios stay paired.
3. **Escape-Repro Lock**: Any online `WA / TLE / RE` after passing local checks activates `ESCAPE_LOCK` until a reproducer is added to `bench/cases_correctness.json` and verified via `opsb bench verify-escape <Op> --bad <badSHA> --good <newSHA>`.
"""

PROBLEM_AGENTS_MD = """# {problem} · Operator Agent Guide

- **Operator**: `{problem}` (Tier weight: `{difficulty}`)
- **Remote Repo**: `https://gitcode.com/{git_owner}/{repo_slug}.git` (private)
- **Editable Scope**: `op_host/`, `op_kernel/`, `bench/cases_correctness.json`, `bench/cases_perf.json`, `DESIGN.md`, `SCORE_HISTORY.md` (`bench/reference.py` is SHA-256 locked)

## 3-Step Iteration Loop (Zero Custom Scripts)

1. Check `opsb breakthrough list` and `opsb bench status {problem}` to target the highest-headroom stratum (`w_hat * (1 - r_hat)`).
2. Commit and push to `origin/main`, then validate in 3 fast steps on `{npu_host}`:
   - `opsb test {problem} --build-only` (`0s` NPU lock — verify `-j4` compile)
   - `opsb test {problem} --fast` (`~10s` `Q0` lock — quick verification)
   - `opsb test {problem}` (`~18s` `Q1` lock — full `6x6` adversarial correctness + single-session `profiler`)
3. Enqueue passing commits exclusively via `opsb submit {problem} $(git rev-parse HEAD) --note "<stratum speedup>"`.

## Calibrated Stratum Score Ledger

| Stratum | Prior `w0` | Learned `w_hat` | Efficiency `r_hat` | Headroom |
|---|---:|---:|---:|---:|
| `PUBLIC (P1..P5)` | `0.100` | `0.100` | `—` |Run `opsb bench status {problem}`|
| `S_SMALL_CORE` | `0.270` | `0.270` | `—` |Prologue / SetBlockDim floor|
| `M_MID_UB` | `0.315` | `0.315` | `—` |All dtypes (fp32/fp16/bf16/i8)|
| `L_HBM_TILED` | `0.180` | `0.180` | `—` |Ping-pong & 724 GB/s HBM roof|
| `X_EXTREME` | `0.135` | `0.135` | `—` |Odd/prime alignment & edge attrs|
"""

PROBLEM_SPEC_MD = """# {problem} · Problem Specification

- **Problem ID**: `{problem_id}` · **Tier**: `{difficulty}` · **Target**: `soc-a` (`SDK {sdk_version}`)

## Mathematical Semantics & Tolerance Contract
- `float32`: `atol = 1e-4, rtol = 1e-4`
- `float16 / bfloat16`: `atol = 1e-3, rtol = 1e-3`
- `integer`: exact (`0` mismatch)
"""

DESIGN_MD = """# {problem} · Architecture & Tiling Design

## 1. Roofline & Bottleneck Notes (Accel soc-a)
- Vector cores: up to `40` AIV (`192 KB` UB per AIV, `32 B` DMA alignment).

## 2. Stratified Experiment Ledger (`profiler` Single-Session `Task Duration(us)`)
| Commit | Hypothesis | `S_SMALL` (`µs`) | `M_MID` (`µs`) | `L_HBM` (`µs`) | `X_EXT` (`µs`) | Gate |
|---|---|---|---|---|---|---|
"""

SCORE_HISTORY_MD = """# {problem} · Online Score & Residual History

| UTC Time | Commit | Predicted | Actual (`/100`) | Residual Gap | Note |
|---|---|---:|---:|---:|---|
"""
