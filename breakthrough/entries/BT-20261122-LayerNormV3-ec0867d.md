---
id: BT-20261122-LayerNormV3-ec0867d
problem: LayerNormV3
difficulty: 2
sha: ec0867dd795ba4e3d45cf135413ec771c8bcdbf1
baseline_sha: e21fdce
timestamp: "2026-11-22T12:30:12.166267+00:00"
score_before: 49.49
score_after: 65.45
delta_raw: 15.96
delta_weighted: 31.92
status: verified
category: tiling-regime
tags: [ReduceSum]
title: "v5.1: fix BlockReduceSum (repeatTime, mask) parameter order"
---

# LayerNormV3 · `+15.96` pts (`49.49 -> 65.45`)

## 1. Context & Bottleneck
Prior validated baseline plateaued at 49.49 pts before ec0867d.

## 2. Core Breakthrough Mechanisms
1. Simplex-MAP (8/14 buckets vs e21fdce, beta=1.104, d_hid=
2. 7.42, d_pub=
3. 0.67, improved=7, regressed=1)
4. All 4 stages passed

## 3. Precision & Synchronization Invariants
Passed extended NPU precision & determinism gates + online 50-case evaluation.

## 4. Reusable KernelC Pattern
> **Rule (`tiling-regime`)**: Apply tiling-regime optimization (ReduceSum) with paired NPU verification.
