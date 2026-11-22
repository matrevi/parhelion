---
id: BT-20261122-ConfusionTransposeD-3938863
problem: ConfusionTransposeD
difficulty: 3
sha: 39388639a651675c7a956740b166a739fa5f1433
baseline_sha: 31affc6
timestamp: "2026-11-22T14:14:08.221718+00:00"
score_before: 65.72
score_after: 74.11
delta_raw: 8.39
delta_weighted: 25.17
status: verified
category: dma-coalescing
tags: [Gather]
title: "perf(exp-b): V3 add SCH_WHOLE_UB 1D-DMA gather, q4Blocks warm LUT, 20b small/mid, 64x64 i64"
---

# ConfusionTransposeD · `+8.39` pts (`65.72 -> 74.11`)

## 1. Context & Bottleneck
Prior baseline `31affc6` plateaued at 65.72 pts; `3938863` lifted online score to 74.11 pts (+8.39 raw / +25.17 weighted). Top paired hardware speedups: perm_5d 3.8->1.6us (2.40x), attn_small_span 4.0->2.5us (1.59x), attn_0213_bf16 4.4->2.9us (1.52x).

## 2. Core Breakthrough Mechanisms
1. perf(exp-b): V3 add SCH_WHOLE_UB 1D-DMA gather, q4Blocks warm LUT, 20b small/mid, 64x64 i64

## 3. Precision & Synchronization Invariants
Verified locally (EXTENDED_PASSED 160 cases, exit_code=0) and passed online 50-case Judge evaluation at 74.11 pts.

## 4. Reusable KernelC Pattern
> **Rule (`dma-coalescing`)**: When optimizing `dma-coalescing` bottlenecks (Gather), isolate hot regimes via dedicated `schMode`/static UB layout and verify paired `profiler` Task Duration across S/M/L/X strata.

## 5. Measured Hardware Speedups (`profiler`)
| Bucket | Baseline (µs) | Optimized (µs) | Speedup |
|---|---:|---:|---:|
| `perm_5d` | 3.84 | **1.60** | **2.40x** |
| `attn_small_span` | 3.98 | **2.50** | **1.59x** |
| `attn_0213_bf16` | 4.42 | **2.90** | **1.52x** |
| `f32_256x256` | 3.82 | **2.62** | **1.46x** |
| `attn_0213_f16` | 4.74 | **3.36** | **1.41x** |
| `nhwc_to_nchw` | 4.10 | **3.18** | **1.29x** |
| `u8_nchw_nhwc` | 5.22 | **4.12** | **1.27x** |
| `f16_2d_tail` | 1.46 | **1.18** | **1.24x** |

## 6. Touched Kernel & Host Files
```text
op_host/confusion_transpose_d_tiling.cpp      |  278 ++--
op_kernel/confusion_transpose_d.h             | 1774 +++++++++++++++----------
op_kernel/confusion_transpose_d_tiling_data.h |    6 +-
op_kernel/confusion_transpose_d_tiling_key.h  |   24 +-
4 files changed, 1207 insertions(+), 875 deletions(-)
```
