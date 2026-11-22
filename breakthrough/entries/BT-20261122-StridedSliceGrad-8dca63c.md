---
id: BT-20261122-StridedSliceGrad-8dca63c
problem: StridedSliceGrad
difficulty: 3
sha: 8dca63c50c0974985a6f9471ddb42d016f505e14
baseline_sha: 9585ce5
timestamp: "2026-11-22T13:29:02.162911+00:00"
score_before: 56.83
score_after: 61.75
delta_raw: 4.92
delta_weighted: 14.76
status: verified
category: tiling-regime
tags: [bf16, fp16]
title: "v5: noinline RunEngine2 (zero Odo stack in Engine 0/1), Super-Row 2.1C/2.1B batching (all outer strides + 16K "
---

# StridedSliceGrad · `+4.92` pts (`56.83 -> 61.75`)

## 1. Context & Bottleneck
Prior baseline `9585ce5` plateaued at 56.83 pts; `8dca63c` lifted online score to 61.75 pts (+4.92 raw / +14.76 weighted). Top paired hardware speedups: r1_partial 5.2->3.0us (1.72x), tiny_r2 4.5->2.9us (1.56x), full_r2_big 8.0->7.4us (1.08x).

## 2. Core Breakthrough Mechanisms
1. v5: noinline RunEngine2 (zero Odo stack in Engine 0/1), Super-Row 2.1C/2.1B batching (all outer strides
2. 16K fp16/bf16 cap), universal interior BuildStridedRow (odd Q), balanced Regime 2.2
3. cached chunk tables

## 3. Precision & Synchronization Invariants
Verified locally (EXTENDED_PASSED 243 cases, exit_code=0) and passed online 50-case Judge evaluation at 61.75 pts.

## 4. Reusable KernelC Pattern
> **Rule (`tiling-regime`)**: When optimizing `tiling-regime` bottlenecks (bf16, fp16), isolate hot regimes via dedicated `schMode`/static UB layout and verify paired `profiler` Task Duration across S/M/L/X strata.

## 5. Measured Hardware Speedups (`profiler`)
| Bucket | Baseline (µs) | Optimized (µs) | Speedup |
|---|---:|---:|---:|
| `r1_partial` | 5.22 | **3.04** | **1.72x** |
| `tiny_r2` | 4.48 | **2.88** | **1.56x** |
| `full_r2_big` | 7.96 | **7.40** | **1.08x** |
| `fp16_mid` | 6.92 | **6.58** | **1.05x** |
| `r1_step3` | 7.76 | **7.52** | **1.03x** |
| `full_r4` | 6.14 | **6.04** | **1.02x** |
| `r2_lastdim_half` | 7.48 | **7.36** | **1.02x** |
| `r2_neg_step` | 2.94 | **2.92** | **1.01x** |

## 6. Touched Kernel & Host Files
```text
op_kernel/strided_slice_grad.h | 747 ++++++++++++++++++++++-------------------
1 file changed, 405 insertions(+), 342 deletions(-)
```
