---
id: COM-cuda-grid-stride-to-divisor-cores
pillar: community
portability: remap-to-accel
status: unverified
bottlenecks: [cold-launch-tax, port-contention, dispatch-floor]
regimes: [S_SMALL_CORE, M_MID_UB, L_HBM_TILED]
dtypes: [float32, float16, bfloat16, int32]
---

# Remap CUDA Grid-Stride Occupancy -> soc-a `1/4/8/20/40` Divisor Ladder

## 1. Why Direct Porting Fails
CUDA kernels saturate dozens of SMs by launching thousands of lightweight threadblocks (`gridDim = 4 * numSMs`). On soc-a (20 AI Cores / 40 Vector AIV cores), launching all 40 blocks pays **`~0.12 us` per launched block** pure dispatch floor plus HBM/L2 port contention on small/mid payloads (`<= 2 MB`).

## 2. soc-a Remapping Rules
1. **Follow 40's Divisor Ladder (`1 / 4 / 8 / 20 / 40`)**:
   - **Tiny (`<= 16 KB`)**: `1` or `4` blocks (`S_SMALL_CORE`) to eliminate multi-block dispatch tax.
   - **Mid (`64 KB - 2 MB`)**: `20` half-core blocks (`M_MID_UB`) to halve HBM/L2 port contention while saturating vector lanes.
   - **Large (`>= 4 MB`)**: `40` full AIV blocks (`L_HBM_TILED`) for 724 GB/s HBM roofline.
2. **Avoid Non-Divisor Cliffs (e.g. `32` blocks)** and beware `10`-core bimodal DVFS traps (`COM-10core-dvfs-bimodal-trap`).
3. Combine with `SNP-direct-gm-tiling-64b` so H2D tiling copy does not negate small-core dispatch savings.
