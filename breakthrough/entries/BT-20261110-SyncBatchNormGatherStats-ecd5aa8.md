---
id: BT-20261110-SyncBatchNormGatherStats-ecd5aa8
problem: SyncBatchNormGatherStats
difficulty: 2
sha: ecd5aa84543125e0c9a8f1cfd458631ffe467f86
baseline_sha: 8caa859
timestamp: "2026-11-10T00:25:00Z"
score_before: 33.61
score_after: 55.10
delta_raw: 21.49
delta_weighted: 42.98
status: verified
category: vectorization
tags: [Dekker-TwoSum, error-compensation, PipeBarrier, TQue-depth4, DMA-overlap, workspace-1KB, dispatch-floor]
title: "Vectorized Dekker/TwoSum Variance + Depth-4 TQue DMA Overlap"
---

# SyncBatchNormGatherStats · `+21.49` pts (`33.61 -> 55.10`, extended to `57.45` via `544439e`)

> **Code Provenance**: Prerequisite `8caa859` lowered template workspace `16 MB -> 1024 B` (`6.69 -> 33.61`); **`ecd5aa8`** (`d59b513` + `f1c95c4` + `ecd5aa8`) vectorized Dekker/TwoSum variance and raised `TQue` depth `1 -> 4` (**`33.61 -> 55.10`**, **`+21.49` raw / `+42.98` weighted**, sub `1244679`, rank 36). Follow-up **`544439e`** (`20d0000` + `84a80bc` + `66f9548`) tuned `SetBlockDim` to `clamp(ceil(C/1024), 4, AIV)` and 32B `DataCopy`, lifting online peak to **`57.45`** (`+2.35`, sub `6af2a1b96bd0f9be37f03bcf`, rank 33).

## 1. Context & Bottleneck
1. **16 MB workspace platform overhead (`f8c6803 -> 8caa859`)**: 99% of online time (`650–1798 us` vs `4–30 us` local kernel) was per-call platform overhead proportional to the template default `GetWorkspaceSizes(1)[0] = 16 MB`, even though the kernel never dereferenced workspace and used zero KFC high-level APIs.
2. **Scalar Dekker/TwoSum variance (`8caa859 -> ecd5aa8`)**: per-element `GetValue/SetValue` compensation cost `~88 ns/element` (`f32_c8192`: `29.161 us` vs `7.300 us` sample), while depth-1 `TQue` serialized all 8 `DataCopyPad` streams.
3. **Linear per-block dispatch floor (`ecd5aa8 -> 544439e`)**: empty-kernel `SBEARLY=1` sweeps proved **`T_dispatch ≈ 1.8 us + 0.12 us * blocks`** (`1 blk = 1.82 us`, `32 blk = 5.66 us`, `48 blk = 6.88 us`). Launching 32 blocks at `C=8192` paid `5.66 us` of pure dispatch floor for `0.8 us` of real work, whereas `1`-block launches hit scheduling instability (`6.3–6.6 us` med vs `2.58 us` at 4 blocks).

## 2. Core Breakthrough Mechanisms
1. **Minimal Non-Zero Workspace (`1024 B`)**:
   - Verified zero workspace dereference and zero KFC APIs (`ReduceSum`/`Matmul`/`Broadcast`); lowering `16 MB -> 1024 B` collapsed online time from `650–1798 us` to `5.2–5.4 us` (non-zero `1024 B` avoids `0 B` `cpu_timeout`).
2. **Bit-Exact Vectorized `VarianceTile` (Dekker Split + TwoSum)**:
   - Vectorizes Dekker split (`4097.0f * x`), TwoSum, and `factor.lo/complement.lo` error compensation into **39 element-wise `Mul/Sub/Add/Muls`** ops with hoisted scalar constants (`q, qlo, factor hi/lo`) and identical operand ordering, matching scalar `AccurateVariance` bit-for-bit across 240K-element replay.
3. **Depth-4 `TQue` DMA Batching (`106 KB < 192 KB` UB)**:
   - Issues all 4 input `DataCopyPad`s before consuming any and enqueues all 4 outputs before writing back, overlapping 8 DMA latencies.
4. **Dispatch-Optimal `SetBlockDim` + Vectorized `count==1` + 32B `DataCopy` (`544439e`)**:
   - `cores = clamp(ceil(total / 1024), 4, AIV)` (`~32 KB/block` matching single-core DMA saturation, floor `4` eliminating 1-block jitter).
   - Vectorized `count == 1` branch (`Muls/Muls/Add`) and 32B-rounded plain `DataCopy` (`C=65536`: `11.24 -> 9.76 us`, hitting the `724 GB/s` single-die HBM wall).

## 3. Measured Speedups (Exclusive Die `profiler`)
- `8caa859 -> ecd5aa8` (med): `sample (C=6)` `7.300 -> 5.470 us` (**1.34x**); `f32_c8192` `29.161 -> 6.640 us` (**4.39x**).
- `ecd5aa8 -> 544439e` (min / med): `C=64` `4.18/6.62 -> 2.44/2.58 us` (**2.57x** med); `C=256` `4.18/6.32 -> 2.66/2.84 us` (**2.23x**); `C=8192` `6.44/6.58 -> 4.52/4.64 us` (**1.43x**); `C=16384` `8.32 -> 5.88 us` (**1.42x**); `f16 C=8192` `7.20 -> 4.48 us` (**1.61x**).
- **Online**: `6.69 -> 33.61 -> 55.10 ->` **`57.45`** (5 public cases `1798 -> 5.13–5.40 us`).

## 4. Precision, Synchronization & Falsified Negative Lessons (`033804e`)
- **Mandatory `PipeBarrier<PIPE_V>()` regardless of buffer aliasing**: giving all 10 variance intermediates non-aliasing `TBuf`s and deleting the 39 `PipeBarrier<PIPE_V>()`s (`d4f0a07`, reverted) sped up `1.08–1.15x` **but broke `new_variance` `1024/1024` at `C=1024`** — the `PIPE_V` scheduler reorders dependent UB instructions even on distinct buffers.
- **Falsified DMA assumptions (`033804e`)**: single-core MTE2 throughput (`~15 GB/s`, `48 × 15 ≈ 720 GB/s` HBM ceiling) is independent of both cross-tile depth-2 pipelining (`4.52 -> 4.60 us` at `C=8192`) and doubling transaction size `4 KB -> 8 KB`.
- `48/48` extended cases green on `ecd5aa8` and `544439e`.

## 5. Reusable KernelC Pattern
> **Rule (`vectorization`)**: Eliminate template `16 MB` workspace down to `1024 B` whenever the kernel never dereferences workspace or calls KFC APIs; vectorize Dekker/TwoSum compensated arithmetic preserving exact operand order and mandatory `PipeBarrier<PIPE_V>()`; and size `SetBlockDim` against the `~0.12 us/block` dispatch floor (`~32 KB/block`, `>= 4`-block stability floor when 1-block jitters).
