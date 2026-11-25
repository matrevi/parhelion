---
id: COM-cuda-fine-branch-anti-pattern
pillar: community
portability: anti-pattern
status: anti-pattern
bottlenecks: [scalar-bound, icache-bloat, mte2-bound, gather-bound]
regimes: [S_SMALL_CORE, M_MID_UB, L_HBM_TILED, X_EXTREME]
dtypes: [float32, float16, bfloat16, int32]
---

# [ANTI-PATTERN] Per-Element Fine Branching & Scalar GM Scatter/Gather

## 1. Why This Breaks Performance on the SoC
Porting CUDA SIMT per-thread `if/else` control flow or element-by-element GM `GetValue`/`SetValue` loops onto an Accel AI-Core executes sequentially on the **Scalar Unit** (`~88 ns/elem` stall) instead of 256B `PIPE_V` SIMD, and bloats `.text` past the 32 KB L1 I-cache limit. In addition, frequent 16-bit `SetValue` writes to GM can exhibit non-deterministic drops.

## 2. Required Fixes (Do Not Repeat)
- **Never** loop element-by-element reading/writing GM scalars inside hot paths. Stage in UB and flush via `DataCopy` / `DataCopyPad`.
- **Replace** inner-loop `if/else` predicates with hoisted `schMode` compile-time specialization (`if constexpr`) + `Compare` / `Select` / `vreducev2` vector masks (`PIPE_V`).
- **Replace** index-by-index scalar gather with `Gather` + precomputed UB offset tables, `vconv`, or `TransDataTo5HD` (`SNP-transdata-5hd-transpose`).
