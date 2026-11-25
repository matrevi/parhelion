---
id: COM-diagonal-l2-swizzle
pillar: community
portability: portable
status: verified
bottlenecks: [mte2-bound, mte3-bound, l2-partition-camping, transpose-bound]
regimes: [L_HBM_TILED]
dtypes: [float32, float16, bfloat16]
---

# Diagonal L2 / HBM Tile Swizzle (CUDA -> KernelC)

## 1. Core Technique (Portable Algorithmic Idea)
Linear raster assignment of 2D matrix/tensor tiles causes concurrent AI-Cores to hit the same HBM channel / L2 cache partition stride simultaneously (partition camping), saturating one memory controller while leaving others idle. Mapping linear tile index `t` via diagonal swizzle (`group = t / groupArea`, diagonal wrap within a `swizzleWidth` column strip) spreads concurrent block traffic evenly across L2 slices and HBM banks.

## 2. SoC Verification Notes
- Verified on `L_HBM_TILED` 2D transpose & wide tensor streaming (`ConfusionTransposeD` `a7086b0` / `3938863`).
- Keep `swizzleWidth` small (`4` or `8` tile columns) so adjacent tiles still share row/column L2 cache locality without aliasing bank sets.
- Combine with 40-core dispatch only when total payload is large (`>= 4 MB`); prefer 20 half-core for `64 KB - 2 MB` to avoid HBM port thrashing.
