---
id: COM-vectorized-float4-to-32b-burst
pillar: community
portability: remap-to-accel
status: unverified
bottlenecks: [mte2-bound, mte3-bound, unaligned-tail]
regimes: [S_SMALL_CORE, M_MID_UB, L_HBM_TILED, X_EXTREME]
dtypes: [float32, float16, bfloat16, int8]
---

# Remap CUDA `float4` Coalescing -> KernelC 32B/512B DMA Bursts + `noinline DataCopyPad`

## 1. Why Direct Porting Fails
In CUDA, each thread issues a 16B `float4` load/store coalesced across a 32-thread warp into 128B cachelines. On soc-a, MTE2/MTE3 are asynchronous block DMA engines requiring **32-byte alignment** (`8 x fp32` / `16 x fp16`) for fast `DataCopy`, preferring `>= 512B` contiguous HBM bursts.

## 2. soc-a Remapping Rules
1. **Contiguous Fast Path**: Batch multi-row/contiguous payloads into `DataCopy` chunks aligned to `32B` (`>= 512B` per burst when possible).
2. **Unaligned Tails (`X_EXTREME`)**: Use `DataCopyExtParams` + `DataCopyPad` strictly inside a `static __attribute__((noinline))` cold helper (`SNP-noinline-cold-pad`) so `DataCopyPad` template bloat never overflows the 32 KB I-cache.
3. **Stride Unit Trap**: Remember UB stride in `DataCopyExtParams` is in **32B data blocks**, whereas GM stride is in **bytes** (`dstStride=0` on non-32B-multiple rows silently misaligns!).
4. **Sentinel Safety**: Place any UB sentinel/scratch at least `32B` past a `DataCopyPad` target buffer so tail padding never clobbers sentinels.
