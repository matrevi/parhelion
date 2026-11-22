---
id: BT-20261122-PReluGradUpdate-1baa854
problem: PReluGradUpdate
difficulty: 1
sha: 1baa85444835a605f124c7df155ddbd81649a8bb
baseline_sha: 29a557a
timestamp: "2026-11-22T13:44:09.533406+00:00"
score_before: 65.74
score_after: 71.55
delta_raw: 5.81
delta_weighted: 5.81
status: verified
category: tiling-regime
tags: [bf16, Gather]
title: "[exp-b] V1.2: fix bf16 vconv/vsel RAW pipe_barrier + gate ROW_2D_PAD B to inner>=128 + enlarge GATHER tile 384"
---

# PReluGradUpdate · `+5.81` pts (`65.74 -> 71.55`)

## 1. Context & Bottleneck
Prior baseline `29a557a` plateaued at 65.74 pts; `1baa854` lifted online score to 71.55 pts (+5.81 raw / +5.81 weighted). Top paired hardware speedups: sample_4_6_8 5.0->2.1us (2.40x), tiny_1_1 2.1->1.5us (1.44x), 4d_64_1024_3_3_f16 10.6->7.4us (1.43x).

## 2. Core Breakthrough Mechanisms
1. [exp-b] V1.2: fix bf16 vconv/vsel RAW pipe_barrier
2. gate ROW_2D_PAD B to inner>=128
3. enlarge GATHER tile 3840/6912

## 3. Precision & Synchronization Invariants
Verified locally (EXTENDED_PASSED 138 cases, exit_code=0) and passed online 50-case Judge evaluation at 71.55 pts.

## 4. Reusable KernelC Pattern
> **Rule (`tiling-regime`)**: When optimizing `tiling-regime` bottlenecks (bf16, Gather), isolate hot regimes via dedicated `schMode`/static UB layout and verify paired `profiler` Task Duration across S/M/L/X strata.

## 5. Measured Hardware Speedups (`profiler`)
| Bucket | Baseline (µs) | Optimized (µs) | Speedup |
|---|---:|---:|---:|
| `sample_4_6_8` | 5.00 | **2.08** | **2.40x** |
| `tiny_1_1` | 2.10 | **1.46** | **1.44x** |
| `4d_64_1024_3_3_f16` | 10.56 | **7.36** | **1.43x** |
| `sample_4_6_8_bf16` | 2.42 | **1.80** | **1.34x** |
| `high_c16384_proxy` | 7.96 | **6.40** | **1.24x** |
| `sample_4_6_8_f16` | 2.32 | **2.08** | **1.12x** |
| `4d_4_8192_7_7_f32` | 12.78 | **11.62** | **1.10x** |
| `4d_16_64_32_32` | 7.40 | **6.92** | **1.07x** |

## 6. Touched Kernel & Host Files
```text
op_host/p_relu_grad_update_tiling.cpp      |  551 +++----
op_kernel/p_relu_grad_update.cpp           |   15 +-
op_kernel/p_relu_grad_update.h             | 2353 ++++++++++++++++++----------
op_kernel/p_relu_grad_update_tiling_data.h |  100 +-
op_kernel/p_relu_grad_update_tiling_key.h  |    7 +-
5 files changed, 1765 insertions(+), 1261 deletions(-)
```
