---
id: BT-20261122-StridedSliceGrad-ba4a357
problem: StridedSliceGrad
difficulty: 3
sha: ba4a357b94d9f4724097a2e753d7367140d221a6
baseline_sha: 3267c09
timestamp: "2026-11-22T17:47:48.454315+00:00"
score_before: 67.63
score_after: 71.57
delta_raw: 3.94
delta_weighted: 11.82
status: verified
category: tiling-regime
tags: [Gather]
title: "fix(exp-b v14-fix): zero RunSmallStridedCold tail after BuildBatchGatherTable (fixes 64-elem kInfOff WA)"
---

# StridedSliceGrad · `+3.94` pts (`67.63 -> 71.57`)

## 1. Context & Bottleneck
Prior baseline `3267c09` plateaued at 67.63 pts; `ba4a357` lifted online score to 71.57 pts (+3.94 raw / +11.82 weighted). Top paired hardware speedups: r1_partial 4.9->2.5us (1.95x), tiny_r2 4.0->2.5us (1.60x), full_r2_huge 23.6->23.8us (0.99x).

## 2. Core Breakthrough Mechanisms
1. [exp-b v14-fix] Escape-unlocked fix for RunSmallStridedCold 64-elem kInfOff tail WA (373/373 PASS incl all 16-bit finite/subnormal/Inf sweep)
2. keeps v14 hidden-regime speedups

## 3. Precision & Synchronization Invariants
Verified locally (EXTENDED_PASSED 373 cases, exit_code=0) and passed online 50-case Judge evaluation at 71.57 pts.

## 4. Reusable KernelC Pattern
> **Rule (`tiling-regime`)**: When optimizing `tiling-regime` bottlenecks (Gather), isolate hot regimes via dedicated `schMode`/static UB layout and verify paired `profiler` Task Duration across S/M/L/X strata.

## 5. Measured Hardware Speedups (`profiler`)
| Bucket | Baseline (µs) | Optimized (µs) | Speedup |
|---|---:|---:|---:|
| `r1_partial` | 4.88 | **2.50** | **1.95x** |
| `tiny_r2` | 4.02 | **2.52** | **1.60x** |
| `full_r2_huge` | 23.58 | **23.80** | **0.99x** |
| `r3_inner_full` | 7.38 | **7.52** | **0.98x** |
| `r2_neg_step` | 2.48 | **2.54** | **0.98x** |
| `r3_mid_slice` | 7.52 | **7.82** | **0.96x** |
| `full_r2_big` | 4.74 | **4.94** | **0.96x** |
| `oddQ_step2` | 5.14 | **5.38** | **0.96x** |

## 6. Touched Kernel & Host Files
```text
op_host/strided_slice_grad_tiling.cpp |  27 +-
op_kernel/strided_slice_grad.h        | 644 ++++++++++++++++++++++------------
2 files changed, 433 insertions(+), 238 deletions(-)
```
