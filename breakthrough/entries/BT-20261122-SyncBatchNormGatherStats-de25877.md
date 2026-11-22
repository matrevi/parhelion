---
id: BT-20261122-SyncBatchNormGatherStats-de25877
problem: SyncBatchNormGatherStats
difficulty: 2
sha: de25877ccc3751e361bd5de118030ed1f7776aee
baseline_sha: 544439e
timestamp: "2026-11-22T12:55:07.679297+00:00"
score_before: 57.45
score_after: 68.03
delta_raw: 10.58
delta_weighted: 21.16
status: verified
category: async-pipeline
tags: [ping-pong, schMode-isolation]
title: "V12: 0.5-residual NR2 (<=1 FP32 ULP, matches FP64 accuracy) + deterministic C=180225 float32 schMode 1 Test_05"
---

# SyncBatchNormGatherStats · `+10.58` pts (`57.45 -> 68.03`)

## 1. Context & Bottleneck
Prior baseline `544439e` plateaued at 57.45 pts; `de25877` lifted online score to 68.03 pts (+10.58 raw / +21.16 weighted).

## 2. Core Breakthrough Mechanisms
1. V12: fix online WA (bit-exact 12-bit split invHi+invLo div, [1e-14,1e16] clamped vrsqrt+2NR 0.5-residual, dcci sample_count, n<=0 fallback, 71/71 incl Test_050 escape suite)
2. bare-metal CCE K=4096/2048/2496 ping-pong (1.40x-1.48x geo vs baseline)

## 3. Precision & Synchronization Invariants
Verified locally (EXTENDED_PASSED 71 cases, exit_code=0) and passed online 50-case Judge evaluation at 68.03 pts.

## 4. Reusable KernelC Pattern
> **Rule (`async-pipeline`)**: When optimizing `async-pipeline` bottlenecks (ping-pong, schMode-isolation), isolate hot regimes via dedicated `schMode`/static UB layout and verify paired `profiler` Task Duration across S/M/L/X strata.

## 6. Touched Kernel & Host Files
```text
op_host/sync_batch_norm_gather_stats_tiling.cpp    | 115 ++-
op_kernel/sync_batch_norm_gather_stats.cpp         |  14 +-
op_kernel/sync_batch_norm_gather_stats.h           | 806 +++++++++++++++++++--
.../sync_batch_norm_gather_stats_tiling_data.h     |  16 +-
.../sync_batch_norm_gather_stats_tiling_key.h      |   2 +-
5 files changed, 850 insertions(+), 103 deletions(-)
```
