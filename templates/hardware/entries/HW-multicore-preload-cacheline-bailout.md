---
id: HW-multicore-preload-cacheline-bailout
pillar: hardware
portability: native-accel
status: verified
confidence: 0.833
bottlenecks: [sync-hazard, cold-launch-tax, dvfs-10core-spike, mte3-bound]
regimes: [PUBLIC, S_SMALL_CORE, M_MID_UB, L_HBM_TILED, X_EXTREME]
dtypes: [float32, float16, bfloat16, int32]
---

# Accel SoC Multi-Core Concurrency & Scheduling Iron Laws

## 1. Ban `PreLoad(N)`, Dummy Core Launches, & `10c`
- **NEVER** call `KernelC::PreLoad(N)`, dummy `SetBlockDim(4/halfCores)`, or `10`-core launches.
- Fix core counts strictly to **`{1, 4, 8, 20, 40}`** to avoid DVFS cold-start jitter.

## 2. 32B/64B Per-Core Slot Alignment for `cores > 16`
- When `cores > 16`, every per-core workspace reduction/handshake slot MUST be **32B/64B aligned** (`slotStride=1` caused L2 cacheline false sharing costing **`-3..-4 pts`** online on `MultilabelMarginLoss`).

## 3. Capability Bailout Must Precede Idle-Core Early Return
- In any multi-core fast-path kernel returning `bool` (`true=handled`, `false=fallback`), **NEVER** write `if (b0 >= B) return true;` before capability fallback checks.
- Otherwise idle cores return `true` early while active cores take the fallback path, resulting in **silent partial-output WA**.
