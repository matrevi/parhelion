---
id: BT-20261110-MultilabelMarginLoss-59f8065
problem: MultilabelMarginLoss
difficulty: 2
sha: 59f80657c0fc0dcf1fbd1e735cf36b2c34651bb3
baseline_sha: 495ca5c
timestamp: "2026-11-10T04:17:12Z"
score_before: 43.96
score_after: 56.64
delta_raw: 12.68
delta_weighted: 25.36
status: verified
category: deterministic-sync
tags: [cross-core-reduction, sentinel-handshake, PipeBarrier, dedup-histogram, zero-workspace, determinism]
title: "NaN-Sentinel Cross-Core Deterministic Reduction + Lean Deduplicated PIPE_V Margin Loop"
---

# MultilabelMarginLoss · `+12.68` pts (`43.96 -> 56.64`)

## 1. Context & Bottleneck
When `reduction` is `mean` or `sum`, all active AIV blocks must reduce their partial per-row losses into scalar output `y[0]`. Earlier multi-core attempts using atomic float adds or unsynchronized scratch buffers suffered either non-deterministic rounding drift, cross-launch stale workspace state (`Time Limit Exceeded`), or `Runtime Error` on unaligned `is_target` scratch tails (`classes = 9, 13, 16` with high row counts). Single-core fallback (`495ca5c`, `43.96` pts) was safe but left 39 AIV cores idle on `mean`/`sum`.

## 2. Core Breakthrough Mechanisms
1. **Zero-Workspace NaN-Sentinel + Gate-Word Cross-Core Handshake**:
   Uses `NaN` (`0x7FC00000`, impossible as a valid non-negative hinge loss sum) + explicit gate-word spin/barrier protocol with `DataCacheCleanAndInvalid` to coordinate all AIV cores deterministically without relying on uninitialized GM workspace.
2. **Fixed-Order Core-0 FP32 Accumulation**:
   Core 0 sums per-core partial `fp32` scalars in fixed core-index order `0 .. activeCores-1` and issues a single `sizeof(T)` write to `y[0]`, guaranteeing **100% bit-identical output across 2,860 stress launches**.
3. **Deduplicated Target Histogram + Single-`Select` Vector Loop**:
   Accumulates repeated target labels before the class scan (`cnt_l * max(0, 1 + x_j - x_l)`), driving `PIPE_V` with tight `Adds -> Maxs -> Muls -> Add` instructions and a single final `Select` + `ReduceSum` per row.

## 3. Measured Speedups, Invariants & Negative Lessons
- **Measured speedups (`495ca5c` -> `59f8065`)**: `32x1024 mean`: `3178 -> 108.36 us` (**29.3x**); `256x32 mean`: `705 -> 27.78 us` (**25.4x**); `64x64 mean`: `325 -> 17.98 us` (**18.1x**).
- **Online-zeroing post-mortems prevented**: (a) `f77146d` TLE — 32B-granular `SetAtomicAdd` into 4 B `y[0]` caused a 28 B OOB write masked locally by 512 B `aclrtMalloc` slack; (b) `af84e85` RE — fast worker cores on small `C` (`0.3–0.5 us`) published before core 0's NaN sentinel write landed (`~0.3 us`), clobbering the slot. Explicit gate-word handshake eliminated both (**2,860 consecutive NPU launches**, including 600 launches each on race shapes `C = 9, 13, 16`, 0 bit-flips).
- Deferred restoration of `is_target[0 .. stageWords]` ensures output tensor `is_target` remains exact after core-0 reduction finishes.
- **Negative lesson (`f8c1f9c` vs `2d0af18` hybrid)**: compiling `O(C log C)` hardware `Sort32`/`MrgSort` into the same binary as small-`C` `O(nd*C)` paths and triggering `Sort32` at `C <= 128` regressed small/medium shapes by **1.4x–2.5x** (`k_32x10_none 6.18 -> 15.38 us`, `k_64x64_none 15.36 -> 30.62 us`). Isolating `KernelSort` into a separate `SCH_MODE_1` `TILING_KEY` gated to `C >= 512` & `C * min(C,L) >= 131072` (`2d0af18`) fully preserved small-`C` speed while accelerating `8x4096` **8.5x** (`688 -> 81 us`) and `8192` **23.5x** (`3805 -> 162 us`).

## 4. Reusable KernelC Pattern
> **Rule (`deterministic-sync`)**: Avoid hardware `SetAtomicAdd` when online tests require strict multi-dtype reproducibility or small scalar outputs (`y[0]`); instead, pass per-core `fp32` partials via cache-coherent GM slots guarded by an out-of-domain bit pattern (`0x7FC00000` NaN) + `DataCacheCleanAndInvalid`, and reduce in fixed core order on block 0.
