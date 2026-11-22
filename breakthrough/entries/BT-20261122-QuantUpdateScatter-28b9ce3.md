---
id: BT-20261122-QuantUpdateScatter-28b9ce3
problem: QuantUpdateScatter
difficulty: 2
sha: 28b9ce34802f7842357d84f84e48a90e21b7575e
baseline_sha: e29b285
timestamp: "2026-11-22T14:54:35.608898+00:00"
score_before: 67.84
score_after: 85.89
delta_raw: 18.05
delta_weighted: 36.10
status: verified
category: tiling-regime
tags: [tiling-regime]
title: "E19.2: replace lambda with __aicore__ ChunkCursor struct"
---

# QuantUpdateScatter · `+18.05` pts (`67.84 -> 85.89`)

## 1. Context & Bottleneck
Prior baseline `e29b285` plateaued at 67.84 pts; `28b9ce3` lifted online score to 85.89 pts (+18.05 raw / +36.10 weighted). Top paired hardware speedups: odd_c129_i8_proxy 6.2->2.7us (2.32x), sample 2.5->1.7us (1.45x), c1024_r1024 6.4->4.8us (1.31x).

## 2. Core Breakthrough Mechanisms
1. E19.2: replace lambda with __aicore__ ChunkCursor struct

## 3. Precision & Synchronization Invariants
Verified locally (EXTENDED_PASSED 144 cases, exit_code=0) and passed online 50-case Judge evaluation at 85.89 pts.

## 4. Reusable KernelC Pattern
> **Rule (`tiling-regime`)**: When optimizing `tiling-regime` bottlenecks (tiling-regime), isolate hot regimes via dedicated `schMode`/static UB layout and verify paired `profiler` Task Duration across S/M/L/X strata.

## 5. Measured Hardware Speedups (`profiler`)
| Bucket | Baseline (µs) | Optimized (µs) | Speedup |
|---|---:|---:|---:|
| `odd_c129_i8_proxy` | 6.22 | **2.68** | **2.32x** |
| `sample` | 2.50 | **1.72** | **1.45x** |
| `c1024_r1024` | 6.36 | **4.84** | **1.31x** |
| `c128_r256` | 4.56 | **4.06** | **1.12x** |
| `single_row_decode_proxy` | 8.34 | **7.56** | **1.10x** |
| `rows_big_c8` | 2.80 | **2.60** | **1.08x** |
| `kv_decode_512` | 6.00 | **5.58** | **1.08x** |
| `large_ele_c128` | 6.82 | **6.42** | **1.06x** |

## 6. Touched Kernel & Host Files
```text
op_host/quant_update_scatter_tiling.cpp      |   36 +-
op_kernel/quant_update_scatter.cpp           |   25 +-
op_kernel/quant_update_scatter.h             | 2355 +++++++++++++-------------
op_kernel/quant_update_scatter_tiling_data.h |   14 +-
op_kernel/quant_update_scatter_tiling_key.h  |   14 +-
5 files changed, 1237 insertions(+), 1207 deletions(-)
```
