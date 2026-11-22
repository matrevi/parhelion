---
id: BT-20261122-MultilabelMarginLoss-9f92b0a
problem: MultilabelMarginLoss
difficulty: 2
sha: 9f92b0a4702b9130ff5f01eb827e888363990330
baseline_sha: f1d06c8
timestamp: "2026-11-22T12:36:25.771504+00:00"
score_before: 56.64
score_after: 61.93
delta_raw: 5.29
delta_weighted: 10.58
status: verified
category: vectorization
tags: [ReduceSum, Brcb]
title: "fix: harden KernelTiny MTE3_S EVENT_ID1, KernelMid2D fallback stage bounds & -FLT_MAX mask"
---

# MultilabelMarginLoss · `+5.29` pts (`56.64 -> 61.93`)

## 1. Context & Bottleneck
Prior baseline `f1d06c8` plateaued at 56.64 pts; `9f92b0a` lifted online score to 61.93 pts (+5.29 raw / +10.58 weighted). Top paired hardware speedups: k_bf16_64x128_none 20.6->10.8us (1.91x), k_64x64_none 13.5->7.1us (1.90x), k_256x32_mean 21.8->12.1us (1.80x).

## 2. Core Breakthrough Mechanisms
1. 4-TILING_KEY isolation
2. dual-row 64b histogram
3. -FLT_MAX masked 2D Brcb/WholeReduceSum
4. cost-aware core partition
5. hardened sync/bounds: 4x6 7.74->4.62us, 32x10 7.06->4.92us, 97x9 10.46->6.42us, 64x64 15.66->7.14us, 64x128 25.86->10.80us, 256x32 27.78->12.12us, 32x1024 108->31.9us, 8192 3805->158.7us

## 3. Precision & Synchronization Invariants
Verified locally (EXTENDED_PASSED 132 cases, exit_code=0) and passed online 50-case Judge evaluation at 61.93 pts.

## 4. Reusable KernelC Pattern
> **Rule (`vectorization`)**: When optimizing `vectorization` bottlenecks (ReduceSum, Brcb), isolate hot regimes via dedicated `schMode`/static UB layout and verify paired `profiler` Task Duration across S/M/L/X strata.

## 5. Measured Hardware Speedups (`profiler`)
| Bucket | Baseline (µs) | Optimized (µs) | Speedup |
|---|---:|---:|---:|
| `k_bf16_64x128_none` | 20.58 | **10.80** | **1.91x** |
| `k_64x64_none` | 13.54 | **7.14** | **1.90x** |
| `k_256x32_mean` | 21.84 | **12.12** | **1.80x** |
| `k_4x6_mean` | 7.60 | **4.62** | **1.65x** |
| `k_64x64_mean` | 14.46 | **9.12** | **1.59x** |
| `k_fp16_64x64_mean` | 14.64 | **9.26** | **1.58x** |
| `k_32x10_none` | 5.72 | **4.02** | **1.42x** |
| `k_bf16_97x9_mean` | 8.50 | **6.42** | **1.32x** |

## 6. Touched Kernel & Host Files
```text
op_host/multilabel_margin_loss_tiling.cpp      |  171 ++-
op_kernel/multilabel_margin_loss.cpp           |   10 +-
op_kernel/multilabel_margin_loss.h             | 1578 +++++++++++++++---------
op_kernel/multilabel_margin_loss_tiling_data.h |   20 +-
op_kernel/multilabel_margin_loss_tiling_key.h  |   14 +-
5 files changed, 1108 insertions(+), 685 deletions(-)
```
