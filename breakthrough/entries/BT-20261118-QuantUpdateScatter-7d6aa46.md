---
id: BT-20261118-QuantUpdateScatter-7d6aa46
problem: QuantUpdateScatter
difficulty: 2
sha: 7d6aa463fe3c521820be68354f66411abfbc7454
baseline_sha: d524473
timestamp: "2026-11-18T08:38:07.390502+00:00"
score_before: 49.04
score_after: 64.30
delta_raw: 15.26
delta_weighted: 30.52
status: verified
category: tiling-regime
tags: [tiling-regime]
title: "Preserve repo cwd across Phase-B bash -c and batch 14-stratum profiler into one session"
---

# QuantUpdateScatter · `+15.26` pts (`49.04 -> 64.30`)

## 1. Context & Bottleneck
Prior validated baseline plateaued at 49.04 pts before 7d6aa46.

## 2. Core Breakthrough Mechanisms
1. E9: SCH_MODE_0 zero-TQue fast kernel
2. FusedGroupPass
3. dynamic colTile/copyStep: c128_r256 7.92->5.26us (1.51x), kv_decode_512 9.88->7.16us (1.38x), rows_big_c8 6.34->5.46us, sample 6.94->6.06us, c1024_r1024 7.18->6.58us, 116/116 green

## 3. Precision & Synchronization Invariants
Passed extended NPU precision & determinism gates + online 50-case evaluation.

## 4. Reusable KernelC Pattern
> **Rule (`tiling-regime`)**: Apply tiling-regime optimization (tiling-regime) with paired NPU verification.
