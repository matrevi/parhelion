---
id: COM-warp-shuffle-to-5hd-ub
pillar: community
portability: remap-to-accel
status: unverified
bottlenecks: [vec-bound, bank-conflict, scalar-bound, transpose-bound]
regimes: [S_SMALL_CORE, M_MID_UB]
dtypes: [float32, float16, bfloat16]
---

# Remap CUDA Warp Shuffle & SMEM Padding -> KernelC 256B Reduce + 5HD + 48-Bank Coloring

## 1. Why Direct Porting Fails
CUDA kernels rely on 32-thread SIMT warp shuffles (`__shfl_xor_sync`) and 32-bank shared memory `+1 float` skewing. The SoC's AI-Core Vector (`PIPE_V`) operates on 256B vector registers (`64 x fp32` or `128 x fp16/bf16`) backed by Unified Buffer (UB) organized as **48 Banks / 16 Bank Groups (32B x 16 = 512B cycle)**.

## 2. SoC Remapping Rules
1. **Warp Reduction -> `WholeReduceSum` / `BlockReduceSum`**:
   - Replace warp tree shuffles with `WholeReduceSum` / `BlockReduceSum` (256B single-instruction SIMD reduction) or Level-0 `Add<T, false>` folding across 64-element chunks.
2. **Column / Narrow Reduction -> `TransDataTo5HD<uint16_t>`**:
   - Transpose 16 rows x 16 cols in UB (`SNP-transdata-5hd-transpose`) so column reductions become contiguous 256B vector adds (`~9x` faster than scalar column accumulation).
3. **SMEM `+4B` Padding -> UB `+256B` (512B-Cycle) Bank Coloring**:
   - Do NOT pad rows by 4 bytes (breaks 32B DMA alignment!). Instead stagger concurrent read/write UB buffers by `+256B mod 512B` (`SNP-zero-tpipe-ub-arena`).
