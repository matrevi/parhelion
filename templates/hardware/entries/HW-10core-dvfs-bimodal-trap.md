---
id: HW-10core-dvfs-bimodal-trap
legacy_id: COM-10core-dvfs-bimodal-trap
pillar: hardware
portability: anti-pattern
status: anti-pattern
bottlenecks: [cold-launch-tax, dvfs-10core-spike, port-contention]
regimes: [S_SMALL_CORE, M_MID_UB]
dtypes: [float32, float16, bfloat16]
---

# [ANTI-PATTERN] SoC 10-Core (`SetBlockDim(10)`) Cold DVFS Bimodal Spike Trap

## 1. Observed Hardware Failure Mechanism
On the SoC, launching `10` AIV blocks triggers a reproducible **`~9 us` bimodal DVFS down-clocking latency spike** on the first 1–2 cold iterations (`run1 / run3 >= 1.45`). Standard warmed `profiler` medians (after 10+ warmup calls) completely mask this spike, causing unexpected regression under Judge's un-warmed **3-run evaluation protocol**.

## 2. Verified Silicon Evidence & Mitigation
- **Observed**: `SyncBatchNormGatherStats V27 (73046fe)` stagnated under `10`-core mode; switching away from `10`-core (`V38 9c943d3`) dropped latency `5.26 -> 3.96 us` and lifted online score `72.26 -> 72.95`.
- **Action**: Use `opsb bench cold3 <Problem>` (`cold3_probe`) to verify un-warmed 3-run `first_iter_us` and `run1/run3` stability, and pick `blockDim` strictly from `{1, 4, 8, 20, 40}` instead of `10`.
