---
id: BT-20261122-ConfusionTransposeD-d8eb6da
problem: ConfusionTransposeD
difficulty: 3
sha: d8eb6daa1044f11d49ccfe93c4209d501b32f221
baseline_sha: 4b4303f
timestamp: "2026-11-22T13:06:44.595630+00:00"
score_before: 50.11
score_after: 56.40
delta_raw: 6.29
delta_weighted: 18.87
status: verified
category: tiling-regime
tags: [tiling-regime]
title: "perf: cap SMALL_VEC at <=8KB/2048 words (restore 16-core rev_3d_16) + test half-chip 20-block dispatch on TINY"
---

# ConfusionTransposeD · `+6.29` pts (`50.11 -> 56.40`)

## 1. Context & Bottleneck
Prior baseline `4b4303f` plateaued at 50.11 pts; `d8eb6da` lifted online score to 56.40 pts (+6.29 raw / +18.87 weighted). Top paired hardware speedups: f32_2d_tail 2.4->1.6us (1.48x), ref_2d_t 2.4->1.6us (1.47x), ref_2x2x2 2.3->1.6us (1.46x).

## 2. Core Breakthrough Mechanisms
1. perf: cap SMALL_VEC at <=8KB/2048 words (restore 16-core rev_3d_16)
2. test half-chip 20-block dispatch on TINY_LUT/SMALL_VEC

## 3. Precision & Synchronization Invariants
Verified locally (EXTENDED_PASSED 160 cases, exit_code=0) and passed online 50-case Judge evaluation at 56.40 pts.

## 4. Reusable KernelC Pattern
> **Rule (`tiling-regime`)**: When optimizing `tiling-regime` bottlenecks (tiling-regime), isolate hot regimes via dedicated `schMode`/static UB layout and verify paired `profiler` Task Duration across S/M/L/X strata.

## 5. Measured Hardware Speedups (`profiler`)
| Bucket | Baseline (µs) | Optimized (µs) | Speedup |
|---|---:|---:|---:|
| `f32_2d_tail` | 2.42 | **1.64** | **1.48x** |
| `ref_2d_t` | 2.36 | **1.60** | **1.47x** |
| `ref_2x2x2` | 2.30 | **1.58** | **1.46x** |
| `ref_3d_t` | 2.32 | **1.60** | **1.45x** |
| `ref_4d_t` | 2.34 | **1.62** | **1.44x** |
| `ref_4d_f` | 2.34 | **1.62** | **1.44x** |
| `f16_2d_tail` | 2.28 | **1.58** | **1.44x** |
| `ref_16x16` | 2.36 | **1.64** | **1.44x** |

## 6. Touched Kernel & Host Files
```text
op_host/confusion_transpose_d_tiling.cpp | 8 ++++----
1 file changed, 4 insertions(+), 4 deletions(-)
```
