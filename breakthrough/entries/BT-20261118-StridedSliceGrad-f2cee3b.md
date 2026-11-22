---
id: BT-20261118-StridedSliceGrad-f2cee3b
problem: StridedSliceGrad
difficulty: 3
sha: f2cee3b1691a24a83789854b7c2403e054d55f4d
baseline_sha: edc7d7b
timestamp: "2026-11-18T09:48:00.659250+00:00"
score_before: 46.06
score_after: 52.54
delta_raw: 6.48
delta_weighted: 19.44
status: verified
category: dma-coalescing
tags: [dim-collapsing, 4x32KB-ring, SkipStepRows, multi-row-Gather, element-vs-traffic-cores, sentinel-32B-padding]
title: "Canonical Dim Collapsing + 4x32KB Flat Ring + SkipStepRows Multi-Row Gather + Element-Count Core Sizing"
---

# StridedSliceGrad · `+6.48` pts (`46.06 -> 52.54`, peak `56.33`)

> **Code Provenance**: Core v2 kernel rewrite landed in `a933351` (on `b08510f`, restoring `1024 B` workspace); submitted SHA `f2cee3b` switched host `blockDim` from traffic-bytes to element-count sizing above 4 KB and fixed two malformed extended test cases (`163.04 -> 134.38 us` 13-level min sum). Online `f2cee3b` scored `52.54` -> `56.33` -> `52.29` (after rolling back `da1ba4d`).

## 1. Context & Bottleneck
At `edc7d7b` (`46.06` pts) / `c0630a2` (`47.06` pts):
1. **Serialized 1D GM round trips**: `full_r2_huge` `(4096,4096)` ran `81.02 us` min / `103.78 us` med (`~11 GB/s/core`) because `MTE2` and `MTE3` ran sequentially instead of overlapping.
2. **Row-walk scalar overhead on multi-axis slices**: `r3_mid_slice` `(64,128,256) [8:40,:,16:240]` ran `38.10 / 38.68 us` with `aiv_scalar_time = 21.8 us` (205 rows/core × ~210 cycles walking zero/valid rows + odometer + 3 MTE3 descriptors/row).
3. **Traffic-byte vs element-count core-sizing trap (`a933351`)**: sizing `cores = ceil(trafficBytes / 24 KB)` gave scatter-heavy `r5_mixed` `(4,6,8,10,12)` (23,040 outputs, 5.4 KB `dy`) only 4 blocks (480 rows/block), spiking `aiv_scalar_time` to `26.36 us` (`40.02 us` total vs `17.48 us` baseline).

## 2. Core Breakthrough Mechanisms
1. **Exact Device-Side `CollapseDims`**:
   - Canonicalizes `len[d] == 1 -> stp[d] = 1`, drops `dim[d] == 1`, and merges right-to-left any `(d, d+1)` where `stp[d] == 1` and `d+1` is a full unit-stride cover (`bgn == 0 && stp == 1 && len == dim`). Collapses outer/whole slices to **1D unit-stride** and `(64,128,256) [8:40,:,16:240]` directly to **2D**.
2. **Single Static `182.5 KB` `TBuf` Pool (`kPoolBytes = 186,880 B`)**:
   - Allocated once with 512B-aligned compile-time `GetWithOffset` partitions (`b/e/s`, `16 KB` `zero`, 4×`33,792 B` `src[0/1]`/`dst[0/1]`, `offInt/offU32`), avoiding repeated `InitBuffer` cost and UB bank aliasing.
3. **Four Specialized Execution Engines**:
   - **Single-Core Whole-Tensor UB Burst (`nblk == 1 && total <= 8192 && dyTotal <= 8192`)**: issues `begin/end/strides` + all of `dy` in one `MTE2` burst overlapped with `Duplicate(dst[0], 0)`, scatters in UB, and flushes in **one `MTE3` `DataCopyPad`**.
   - **Engine 1 — Flat 1D Unit-Stride (`cr == 1 && stp[0] == 1`)**: 512B-aligned core partitioning streaming through a **4 × 32 KB (128 KB) `MTE2 <-> MTE3` hardware ring** (`flatBuf[0..3]`, events `0..3`, zero scalar waits inside loop) flanked by bulk `16 KB` `ZeroSpan` writes.
   - **Engine 2.1 — `SkipStepRows` + Double-Buffered Multi-Row Vector `Gather`**: jumps maximal provably-zero outer spans in `O(m)` arithmetic (coalesced into bulk `16 KB` `ZeroSpan`s, cutting `r3_mid_slice` `aiv_scalar` `21.8 -> 3.27 us`), packs small `Q < 128` rows into super-tiles, and batches up to `8192 / Q` consecutive forward/reversed rows into **1 MTE2 load + 1 `Gather` + 1 MTE3 store** via 4-event ping-pong. Offset tables use scalar seeding + `O(log2)` `Adds` doubling + `Mins(..., sentBytes)` clamping to a zero sentinel.
4. **Element-Count `blockDim` Sizing (`f2cee3b`)**:
   - `trafficBytes <= 4096` -> `1 block` (unlocks single-burst UB assembly); above 4 KB -> `clamp(ceil(total / 256), 2, maxCores)`, matching scatter/odometer work across high-rank shapes while remaining monotone for copies.

## 3. Measured Speedups (`c0630a2` / `edc7d7b` -> `f2cee3b`, Local `profiler` Min / Med)
- **13-level min sum**: `221.5 us (c0630a2) -> 163.04 us (a933351) ->` **`134.38 us (f2cee3b)`** (**1.65x** overall).
- `full_r2_huge (4096,4096)`: `81.02/103.78 ->` **`28.56/29.64 us`** (**2.82x** min / **3.50x** med; `mte2` & `mte3` fully overlapped).
- `r3_mid_slice (64,128,256)`: `38.10/38.68 ->` **`16.36/16.66 us`** (**2.33x**).
- `r5_mixed (4,6,8,10,12)`: `40.02 (a933351) / 17.48 (c0630a2) ->` **`13.02/13.66 us`** (**3.07x** vs traffic sizing; `aiv_scalar 26.36 -> 4.61 us`).
- `tiny_r2 (3,7)`: `4.64/7.32 -> 2.66/5.00 us` (**1.74x** min); `sample_r4`: `7.58 -> 5.22 us` (**1.45x** min).
- **Online**: `46.06 ->` **`52.54` / `56.33` / `52.29`** (`L_HBM_TILED` calibrated `r: 45.3% -> 77.4%`, `X_EXTREME 41.8% -> 54.7%`).

## 4. Precision, Sync & Negative-Result Invariants
- Raw bit-pattern moves (`uint16_t` for fp16/bf16, `uint32_t` for fp32); `0/240` sample, **`195/195` isolated per-process extended cases green**.
- **Sentinel 32B-tail guard**: `kSentElem = 8224` (`>= kGatherCap + 32`) keeps the zero sentinel outside `DataCopyPad`'s 32B-granular UB tail writes (placing sentinel inside the tail corrupted strided outputs).
- Explicit per-slot `HardEvent`s for all `MTE3 -> MTE2` and `MTE3 -> V` buffer reuses (replacing with `PipeBarrier<PIPE_MTE3>` caused non-deterministic corruption on `1024x1024`). `1024 B` non-zero workspace retained.
- **Pitched-store trap (`da1ba4d` rollback)**: replacing multi-row Gather with pitched `DataCopyPad` (`dstStride = (Q - n) * sizeof(T)`) regressed `r3_mid_slice` (`16.66 -> 18.72 us`, `aiv_mte3 2.23 -> 5.13 us`) and **silently left intra-row head/tail gaps unwritten** (hidden only because test buffers were pre-zeroed — always poison output buffers with NaN/int-min in correctness harnesses).

## 5. Reusable KernelC Pattern
> **Rule (`dma-coalescing`)**: Collapse full unit-stride trailing dims before dispatch, skip outer zero spans in `O(m)` into bulk `ZeroSpan`s, stream 1D slices via a 512B-aligned 4×32 KB ring, batch multi-row scatters with `O(log2)` `Gather` (sentinel offset `>= cap + 32`), and size `blockDim` by output element count (not traffic bytes) whenever scalar scatter/odometer work dominates; never use pitched stores over many rows (`>16` rows/core or `gap < burst`) without explicitly zeroing intra-row head/tail gaps.
