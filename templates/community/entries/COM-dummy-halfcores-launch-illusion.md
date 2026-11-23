---
id: COM-dummy-halfcores-launch-illusion
pillar: community
portability: anti-pattern
status: anti-pattern
confidence: 0.333
bottlenecks: [cold-launch-tax, dvfs-10core-spike]
regimes: [PUBLIC, S_SMALL_CORE]
dtypes: [float32, float16, bfloat16]
---

# [ANTI-PATTERN] Dummy `SetBlockDim(halfCores)` / `SetBlockDim(20)` + Early Return on Small Shapes

## 1. Observed Failure Mechanism
Never launch small 1-core shapes with dummy extra cores (`SetBlockDim(halfCores)` / `SetBlockDim(20)` combined with `if (GetBlockIdx() != 0) return;` or no-op cores):
1. It **artificially masks** local idle-die 1-core DVFS spikes in warmed/synthetic local microbenchmarks (`4.7 -> 1.56 us`) while paying real multi-core dispatch overhead online (`+0.62 us` on Public #3, matching the `~2.04 us` 20-core floor).
2. On `20`-core variants (`soc-b`), `(maxCores + 1) / 2` silently picks `10` cores — triggering the `10`-core DVFS bimodal spike trap directly!

## 2. Verified Evidence & Mitigation
- **Observed**: `StridedSliceGrad (734e097)` regressed online from `71.57 -> 59.74` (`-11.83 pts`, Public #3 `3.95 -> 4.56 us`) when dummy `SetBlockDim(halfCores)` early-return launch replaced true 1-core launch.
- **Action**: Launch only genuinely utilized cores (`1/4/8/20/40`), keep `SetDataSize(64)` compact tiling (`SNP-direct-gm-tiling-64b`), and verify real cold latency with `opsb bench cold3` instead of masking 1c DVFS jitter via idle dummy blocks.
