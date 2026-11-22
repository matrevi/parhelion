---
id: BT-20261118-QuantUpdateScatter-7d6aa46
problem: QuantUpdateScatter
difficulty: 2
sha: 7d6aa463fe3c521820be68354f66411abfbc7454
baseline_sha: d524473
timestamp: "2026-11-18T08:38:07.390502+00:00"
score_before: 49.04
score_after: 64.30
delta_raw: 15.26
delta_weighted: 30.52
status: verified
category: tiling-regime
tags: [tilingKey, schMode, zero-TQue, in-UB-overwrite, fused-group, dynamic-colTile, front-loaded-DMA]
title: "SCH_MODE_0 Zero-TQue Fast Kernel + FusedGroupPass In-UB Overwrite + Dynamic colTile/copyStep"
---

# QuantUpdateScatter · `+15.26` pts (`49.04 -> 64.30`)

> **Code Provenance**: Core kernel & tiling breakthrough landed in `6a89b3c` (+ LP64 `std::max<int64_t>` fix `e2c8d76`); submitted SHA `7d6aa46` added single-session 14-stratum `profiler` batching on top of identical `op_host/` & `op_kernel/`. Builds on precursor `d524473` (`44.76 -> 49.04`, depth-2 `TQue` ping-pong, gated multi-row `O(log2)` scale replication, 32B `DataCopy` fast path, and UB index cache).

## 1. Context & Bottleneck
At `d524473` (`49.04` pts), three distinct bottlenecks capped performance across small, KV-decode, and wide-channel regimes:
1. **Small/medium `TQue` + two-pass serialization**: `c128_r256` `(1,256,128)` ran `7.92 us` (vs torch `6.83 us`), `sample` `(1,2,32)` ran `6.94 us` (vs `5.06 us`), and `rows_big_c8` `(1,4096,8)` ran `6.34 us`. Every core paid `TQue<2>` init/EnQue/DeQue bookkeeping and ran `CopyPass` serially before `UpdatePass` even when each core held `<= 32 KB` copy and `<= 1` quant batch.
2. **High-batch KV-decode scatter fragmentation**: `kv_decode_512` `(512,64,128)` updating `(512,1,128)` took `9.88 us` (1.33x slower than torch `7.44 us`) because two-pass Copy+Update fragmented `var` around each window and emitted 512 tiny 128 B scatter stores.
3. **Wide-channel core starvation & fixed copy step**: shapes with `C > 4096` and few update rows (`few_rows_c65536`) generated only `16` tiles at fixed `colTile = 4096` (`16/48` cores active), while fixed `32 KB` copy bursts lost double-buffering on ~1 MB shapes (`c1024_r1024` `7.18 us`).

## 2. Core Breakthrough Mechanisms
1. **Dual-Schedule `tilingKey` Specialization (`SCH_MODE_0` Zero-`TQue` `KernelFast`, `116 KB` Static UB)**:
   - Selected when `!fusedGroup && tpr == 1 && copyBlock <= 32768 && blocks <= cores && qBlock <= mRows && qBlock * cPad <= 4096 && (qBlock <= 1 || C % 32 == 0) && liveBatches <= 1024`.
   - **Sub-path 0A (`RunSingleCoreInUB`, `vvPtr != yyPtr && cores == 1 && C % 32 == 0 && total <= 32 KB`)**: issues `var`, `scales`, `zp`, and `updates` in one back-to-back `MTE2` burst before the first `WaitFlag<MTE2_V>`, fuses input `Cast`s before a single `PipeBarrier<PIPE_V>()`, overwrites updated slices in UB via 32B-aligned `DataCopy`, and writes all of `var_out` in **one contiguous `MTE3` `Store1D`**.
   - **Sub-path 0B (`RunFrontLoaded`, multi-core)**: issues `scales + zp + updates` onto `MTE2` **before** `CopyPass` so update DMA hides behind `CopyPass`, then runs `QuantiseFused` and merged per-group stores using static compile-time event IDs with zero `TQue` tax.
2. **`FusedGroupPass` In-UB Overwrite (in `SCH_MODE_1` `KernelStream`, `172 KB` UB)**:
   - Enabled for high-batch small-group decode shapes (`tpr == 1 && C % 32 == 0 && C <= 4096 && 0 < varRows*C <= 16384 && updateRows*C <= 2048 && totalGroups >= max(64, 2*aiv) && updateRows < varRows && vvPtr != yyPtr`).
   - Streams double-buffered `32 KB` whole-group `var` stages (`stg[0/1]`, events `EV_FUSED_V/OUT=2..3`, `EV_COPY_FREE=0..1`), overlaps `qi` update loads + `Quantise` with `var` `Load1D`, overwrites updated rows directly in UB, and flushes each stage in **one 32 KB contiguous `Store1D`**.
3. **Dynamic `colTile` (`[512, 4096]`) + Dynamic `copyStep` (`8/16/32 KB`) + Front-Loaded First Tile**:
   - Shrinks `colTile` down to `512` (64-aligned) when tile count under-occupies AIV cores, with zero extra scale traffic.
   - Adapts `copyStep` (`32 KB` for `> 64 KB`, `16 KB` for `> 16 KB`, `8 KB` below) so ~1 MB shapes keep double-buffering active; overlaps first update load between `SetChunkConstIssue` and `SetChunkConstFinish`.
   - Refined `blockDim`: `1` core for small tensors (`total <= 8192 && perChunk*C <= 2048 && rows <= 128 && liveBatches <= 16`), `min(aiv, totalGroups)` for `fusedGroup`.

## 3. Measured Speedups (`d524473` -> `7d6aa46`, Local `profiler` Median)
- `c128_r256 (1,256,128)`: `7.92 -> 5.26 us` (**1.51x**, min `4.84 us`)
- `kv_decode_512 (512,64,128)`: `9.88 -> 7.16 us` (**1.38x**, beats torch `7.44 us`)
- `rows_big_c8 (1,4096,8)`: `6.34 -> 5.46 us` (**1.16x**)
- `sample (1,2,32)`: `6.94 -> 6.06 us` (**1.15x**, min `4.44 us`)
- `c1024_r1024 (1,1024,1024)`: `7.18 -> 6.58 us` (**1.09x**)
- **Online**: `49.04 -> 64.30` (**`+15.26` raw / `+30.52` weighted**, submission `6afd636985ad43bbe0c83f25`).

## 4. Precision & Synchronization Invariants
- Bit-exact `int8` (`0/64` sample, **`116/116` isolated extended cases green**), plus offline state-machine verification (`WALK_OK 2041 quant blocks + 279 copy/fused partitions + 135 numeric e2e checks` across all 4 mode sub-paths).
- `vvPtr != yyPtr` explicitly guards `RunSingleCoreInUB` and `FusedGroupPass` against in-place aliasing hazards.
- Compile-time disjoint event IDs (`EV_COPY_IN/FREE=0..1`, `EV_FUSED_V/OUT=2..3`, `EV_SC_LOAD/REFILL/UPD_OUT=4`) prevent collisions with depth-2 `TQue`s; every `Cast`/`DataCopy` <-> arithmetic boundary retains `PipeBarrier<PIPE_V>()` (removing it caused `26-27/64` wrong rows), and no `TQue` ever mixes two dtypes. `1024 B` non-zero workspace retained.

## 5. Reusable KernelC Pattern
> **Rule (`tiling-regime`)**: Isolate small/single-batch shapes onto a dedicated zero-`TQue` `SCH_MODE_0` kernel binary (in-UB overwrite + front-loaded DMA overlap), fuse small-group scatter-copies in UB (`FusedGroupPass`) to turn fragmented stores into 32 KB contiguous bursts, and dynamically shrink `colTile`/`copyStep` whenever fixed tiles under-occupy AIV cores.
