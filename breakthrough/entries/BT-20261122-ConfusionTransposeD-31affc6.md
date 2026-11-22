---
id: BT-20261122-ConfusionTransposeD-31affc6
problem: ConfusionTransposeD
difficulty: 3
sha: 31affc6e0efa33024e7c9f8398b2240414f01807
baseline_sha: d8eb6da
timestamp: "2026-11-22T13:29:53.164454+00:00"
score_before: 56.40
score_after: 65.72
delta_raw: 9.32
delta_weighted: 27.96
status: verified
category: tiling-regime
tags: [tiling-regime]
title: "perf: divisor-of-40 block ladder (1/10/20/40) + zero-Cast u32 TinyLut (<=240w) + 20-block <=64KB mid-UB"
---

# ConfusionTransposeD · `+9.32` pts (`56.40 -> 65.72`)

## 1. Context & Bottleneck
Prior baseline `d8eb6da` plateaued at 56.40 pts; `31affc6` lifted online score to 65.72 pts (+9.32 raw / +27.96 weighted). Top paired hardware speedups: perm_5d 5.4->3.8us (1.42x), rev_3d_16 3.2->2.3us (1.37x), f32_128x128 3.3->2.5us (1.35x).

## 2. Core Breakthrough Mechanisms
1. [main] divisor-of-40 ladder (1/10/20/40)
2. zero-Cast u32 TinyLut (<=240w)
3. SMALL_VEC (<=8KB): sum_med 120.04->108.32us (vs 133.44 ccdc5cb), legacy12_med 39.98->35.62us, ref_2x2x2 1.58->1.22us, ref_4d_t 1.62->1.32us, ref_3d_t 1.60->1.22us, f32_128x128 3.34->2.48us, perm_5d 5.44->3.84us
4. 160/160 bit-exact

## 3. Precision & Synchronization Invariants
Verified locally (EXTENDED_PASSED 160 cases, exit_code=0) and passed online 50-case Judge evaluation at 65.72 pts.

## 4. Reusable KernelC Pattern
> **Rule (`tiling-regime`)**: When optimizing `tiling-regime` bottlenecks (tiling-regime), isolate hot regimes via dedicated `schMode`/static UB layout and verify paired `profiler` Task Duration across S/M/L/X strata.

## 5. Measured Hardware Speedups (`profiler`)
| Bucket | Baseline (µs) | Optimized (µs) | Speedup |
|---|---:|---:|---:|
| `perm_5d` | 5.44 | **3.84** | **1.42x** |
| `rev_3d_16` | 3.20 | **2.34** | **1.37x** |
| `f32_128x128` | 3.34 | **2.48** | **1.35x** |
| `cyclic_3d_a` | 1.58 | **1.20** | **1.32x** |
| `ref_3d_t` | 1.60 | **1.22** | **1.31x** |
| `f32_2d_tail` | 1.64 | **1.26** | **1.30x** |
| `ref_2x2x2` | 1.58 | **1.22** | **1.30x** |
| `ref_2d_t` | 1.60 | **1.26** | **1.27x** |

## 6. Touched Kernel & Host Files
```text
op_host/confusion_transpose_d_tiling.cpp | 59 ++++++++++++++++++++++++--------
op_kernel/confusion_transpose_d.h        | 32 ++++++-----------
2 files changed, 55 insertions(+), 36 deletions(-)
```
