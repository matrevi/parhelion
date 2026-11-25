---
id: COM-online-welford-softmax
pillar: community
portability: portable
status: unverified
bottlenecks: [mte2-bound, ub-budget-bloat, vec-bound]
regimes: [M_MID_UB, L_HBM_TILED]
dtypes: [float32, float16, bfloat16]
---

# Online Welford / Softmax Single-Pass Reduction (CUDA -> KernelC)

## 1. Core Technique (Portable Algorithmic Idea)
Instead of reading a row from HBM twice (Pass 1: global max/mean, Pass 2: exp/variance + normalization), maintain running statistics `(m_new, d_new)` over UB tiles in a single streaming pass, cutting HBM read traffic (`MTE2`) by up to `50%` and eliminating intermediate workspace buffers when rows exceed 184 KB UB.

## 2. SoC Adaptation Notes
1. **Recurrence Hazard**: Insert explicit `PipeBarrier<PIPE_V>()` between chained `Max` / `Sub` / `Exp` / `Add` updates on the running accumulator tensor so vector pipeline hazards never corrupt state.
2. **Precision Contract Guard**: Upcast `float16` / `bfloat16` inputs to `float32` via `Cast(..., RoundMode::CAST_NONE)` (chunked `<= 64` or `<= 128` elements per repeat) UNLESS the problem golden strictly requires sequential `bfloat16` element-order rounding.
3. **UB Layout**: Combine with `SNP-zero-tpipe-ub-arena` and `SNP-ring4-async-pipe` so HBM loads overlap cleanly with `PIPE_V` reduction.
