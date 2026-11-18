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
tags: [cross-core-reduction, sentinel-handshake, PipeBarrier, Sort32, determinism]
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

## 3. Precision & Synchronization Invariants
- Verified across **2,860 consecutive NPU launches** (including 600 launches each on race-trigger shapes `C = 9, 13, 16`) with zero bit-flips and zero hangs.
- Deferred restoration of `is_target[0 .. stageWords]` ensures output tensor `is_target` remains exact after core-0 reduction finishes.

## 4. Reusable KernelC Pattern
> **Rule (`deterministic-sync`)**: Avoid hardware `SetAtomicAdd` when online tests require strict multi-dtype reproducibility or small scalar outputs (`y[0]`); instead, pass per-core `fp32` partials via cache-coherent GM slots guarded by an out-of-domain bit pattern (`0x7FC00000` NaN) + `DataCacheCleanAndInvalid`, and reduce in fixed core order on block 0.
