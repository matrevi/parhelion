---
id: BT-20261122-LayerNormV3-e21fdce
problem: LayerNormV3
difficulty: 2
sha: e21fdced97de88ee371858c22847641773c17134
baseline_sha: 64c85cf
timestamp: "2026-11-22T12:13:52.015241+00:00"
score_before: 42.04
score_after: 49.49
delta_raw: 7.45
delta_weighted: 14.90
status: verified
category: async-pipeline
tags: [Brcb, pure-PIPE_V]
title: "v4: pure-PIPE_V zero-scalar stats + Brcb stride-0 broadcast + binary-tree Add reduce + async g/b overlap + SCH"
---

# LayerNormV3 · `+7.45` pts (`42.04 -> 49.49`)

## 1. Context & Bottleneck
Prior baseline `64c85cf` plateaued at 42.04 pts; `e21fdce` lifted online score to 49.49 pts (+7.45 raw / +14.90 weighted). Top paired hardware speedups: f16_4096x1024 56.5->32.6us (1.73x), f32_1024x1024 18.7->15.9us (1.17x), f32_256x512 11.9->10.5us (1.14x).

## 2. Core Breakthrough Mechanisms
1. Simplex-MAP (8/14 buckets vs 64c85cf, beta=1.170, d_hid=
2. 2.93, d_pub=
3. 0.14, improved=7, regressed=0)
4. All 4 stages passed (weighted E[delta]=
5. 6.14 pts)

## 3. Precision & Synchronization Invariants
Verified locally (EXTENDED_PASSED 79 cases, exit_code=0) and passed online 50-case Judge evaluation at 49.49 pts.

## 4. Reusable KernelC Pattern
> **Rule (`async-pipeline`)**: When optimizing `async-pipeline` bottlenecks (Brcb, pure-PIPE_V), isolate hot regimes via dedicated `schMode`/static UB layout and verify paired `profiler` Task Duration across S/M/L/X strata.

## 5. Measured Hardware Speedups (`profiler`)
| Bucket | Baseline (µs) | Optimized (µs) | Speedup |
|---|---:|---:|---:|
| `f16_4096x1024` | 56.50 | **32.64** | **1.73x** |
| `f32_1024x1024` | 18.66 | **15.94** | **1.17x** |
| `f32_256x512` | 11.90 | **10.48** | **1.14x** |
| `f32_2x20000` | 19.02 | **16.96** | **1.12x** |
| `bf16_1024x1024` | 105.00 | **98.80** | **1.06x** |
| `f32_64x8192` | 12.54 | **12.12** | **1.03x** |
| `f16_512x512` | 14.18 | **14.66** | **0.97x** |
| `f32_4x6x8` | 6.38 | **6.88** | **0.93x** |

## 6. Touched Kernel & Host Files
```text
op_host/layer_norm_v3_tiling.cpp      |   40 +-
op_kernel/layer_norm_v3.cpp           |    2 +-
op_kernel/layer_norm_v3.h             | 1271 +++++++++++++++++++++++----------
op_kernel/layer_norm_v3_tiling_data.h |    3 +-
4 files changed, 917 insertions(+), 399 deletions(-)
```
