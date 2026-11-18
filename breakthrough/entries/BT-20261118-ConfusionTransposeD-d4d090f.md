---
id: BT-20261118-ConfusionTransposeD-d4d090f
problem: ConfusionTransposeD
difficulty: 3
sha: d4d090fbd3e4218f0e9e89f1b8c6cdadca06ce1b
baseline_sha: 7b15ba5
timestamp: "2026-11-18T08:35:28.535009+00:00"
score_before: 42.83
score_after: 48.83
delta_raw: 6.00
delta_weighted: 18.00
status: verified
category: tiling-regime
tags: [tiling-regime]
title: "Fix C++11 CeilDiv overload and unused param in tiling"
---

# ConfusionTransposeD · `+6.00` pts (`42.83 -> 48.83`)

## 1. Context & Bottleneck
Prior validated baseline plateaued at 42.83 pts before d4d090f.

## 2. Core Breakthrough Mechanisms
1. Simplex-MAP (12/17 buckets vs 7b15ba5, beta=0.736, d_hid=
2. 19.73, d_pub=
3. 1.39, improved=12, regressed=0)
4. All 4 stages passed

## 3. Precision & Synchronization Invariants
Passed extended NPU precision & determinism gates + online 50-case evaluation.

## 4. Reusable KernelC Pattern
> **Rule (`tiling-regime`)**: Apply tiling-regime optimization (tiling-regime) with paired NPU verification.
