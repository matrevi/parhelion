---
id: BT-20261122-StridedSliceGrad-306c457
problem: StridedSliceGrad
difficulty: 3
sha: 306c457a2e010c2e2f2a1888e86f71533a3a20e6
baseline_sha: dd7de48
timestamp: "2026-11-22T14:18:58.852773+00:00"
score_before: 62.99
score_after: 67.63
delta_raw: 4.64
delta_weighted: 13.92
status: verified
category: tiling-regime
tags: [tiling-regime]
title: "v8a: fix PIPE_ALL global namespace in strided_slice_grad.cpp"
---

# StridedSliceGrad · `+4.64` pts (`62.99 -> 67.63`)

## 1. Context & Bottleneck
Prior baseline `dd7de48` plateaued at 62.99 pts; `306c457` lifted online score to 67.63 pts (+4.64 raw / +13.92 weighted). Top paired hardware speedups: r2_shrink 3.6->3.0us (1.22x), r2_unaligned 3.4->2.9us (1.20x), r2_neg_step 4.0->3.3us (1.19x).

## 2. Core Breakthrough Mechanisms
1. v8a: fix PIPE_ALL global namespace in strided_slice_grad.cpp

## 3. Precision & Synchronization Invariants
Verified locally (EXTENDED_PASSED 243 cases, exit_code=0) and passed online 50-case Judge evaluation at 67.63 pts.

## 4. Reusable KernelC Pattern
> **Rule (`tiling-regime`)**: When optimizing `tiling-regime` bottlenecks (tiling-regime), isolate hot regimes via dedicated `schMode`/static UB layout and verify paired `profiler` Task Duration across S/M/L/X strata.

## 5. Measured Hardware Speedups (`profiler`)
| Bucket | Baseline (µs) | Optimized (µs) | Speedup |
|---|---:|---:|---:|
| `r2_shrink` | 3.62 | **2.96** | **1.22x** |
| `r2_unaligned` | 3.44 | **2.86** | **1.20x** |
| `r2_neg_step` | 3.98 | **3.34** | **1.19x** |
| `r2_lastdim_step2` | 6.64 | **5.86** | **1.13x** |
| `r1_partial` | 5.14 | **4.76** | **1.08x** |
| `r1_step3` | 5.44 | **5.08** | **1.07x** |
| `r4_ellipsis` | 5.28 | **5.08** | **1.04x** |
| `tiny_r2` | 4.62 | **4.48** | **1.03x** |

## 6. Touched Kernel & Host Files
```text
op_host/strided_slice_grad_tiling.cpp     |   53 +-
op_kernel/strided_slice_grad.cpp          |   26 +-
op_kernel/strided_slice_grad.h            | 3060 +++++++++++++++--------------
op_kernel/strided_slice_grad_tiling_key.h |    8 +-
4 files changed, 1648 insertions(+), 1499 deletions(-)
```
