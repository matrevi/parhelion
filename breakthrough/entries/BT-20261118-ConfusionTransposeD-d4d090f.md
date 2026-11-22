---
id: BT-20261118-ConfusionTransposeD-d4d090f
problem: ConfusionTransposeD
difficulty: 3
sha: d4d090fbd3e4218f0e9e89f1b8c6cdadca06ce1b
baseline_sha: 7b15ba5
timestamp: "2026-11-18T08:35:28.535009+00:00"
score_before: 42.83
score_after: 48.83
delta_raw: 6.00
delta_weighted: 18.00
status: verified
category: simd-transpose
tags: [Gather, canonical-coalescing, int8-widen-cast, double-buffer, Level0-normal-mode, vector-doubling, zero-workspace]
title: "Canonical Axis Coalescing + Universal Double-Buffered Level-0 Gather Transpose (All 11 Dtypes incl. 1B)"
---

# ConfusionTransposeD · `+6.00` pts (`42.83 -> 48.83`, extended to `50.11` via `ea0d191`/`6606085`)

> **Code Provenance**: Core kernel & tiling breakthrough landed in `9a04e48`; submitted SHA `d4d090f` fixed C++11 `CeilDiv` overload & unused parameter in `op_host/confusion_transpose_d_tiling.cpp`. Follow-up `ea0d191` (submitted via `6606085`) added 32B-chunk vector doubling for `Gather` offset tables, lifting online peak further to **`50.11`** (`+7.28` raw over `7b15ba5`).

## 1. Context & Bottleneck
At `7b15ba5` (`42.83` pts, contemporary `39.91`):
1. **1-byte dtypes had no vector transpose path**: `i8_512x512` `(512,512) int8` fell back to 262,144 1-byte scalar runs taking **`513.01 us` median (`498.43 us` min)**.
2. **`TransDataTo5HD` ~6 us fixed latency & single-buffered 16-row tiles**: `f32_1024x1024` took `28.48 us`, `f32_256x256` `9.38 us`, and `f32_128x128` `6.94 us` because `7b15ba5` used single-buffered 16-row tiles with either `TransDataTo5HD` (~6 us/issue) or 16-wide 1-row-per-call `vgather` toggling `SetMaskCount/SetMaskNorm`.
3. **Incomplete axis folding & UB-chunk truncation**: `BuildPlan` stopped folding at UB chunk boundaries, missing multi-axis block permutations (`NCHW <-> NHWC`, cyclic 3D `[1,2,0]/[2,0,1]`, `[2,3,0,1]`) and truncating spans `> 48 KB`.

## 2. Core Breakthrough Mechanisms
1. **Full Canonical Adjacent-Axis Coalescing (`BuildPlan`)**:
   - Drops size-1 axes and merges every adjacent pair `(k-1, i)` with `cs[k-1] == os[i] * od[i]` across all axes **without UB-chunk truncation**, collapsing NCHW<->NHWC, cyclic 3D, and 4D block swaps into canonical 2D or batched-2D transposes; splits spans `> 48 KB` into `48 KB` sub-segments distributed across all 40 cores.
2. **Universal Double-Buffered 2D In-UB Normal-Mode Level-0 `Gather` Transpose (All 11 Dtypes)**:
   - Identifies source-contiguous axis `p` (`strides[p] == span`) and dest-contiguous axis `rank - 1`, treating remaining `nb` axes as outer batches.
   - **Even `span` (`TransposeEvenRun<uint32_t|uint16_t>`)**: uses Normal-mode Level-0 `Gather` (`64 u32` or `128 u16` words/repeat, up to 255 repeats = 64 KB/call) — **zero `SetMaskCount/SetMaskNorm` toggling** and **zero `TransDataTo5HD` 6 us floor**.
   - **Odd / 1-byte `span` (`TransposeOddRun`, e.g. `int8`/`uint8`)**: bit-exact widening/narrowing pipeline via Level-0 Normal-mode `Cast<half, uint8_t, true>(CAST_NONE, {1,1,8,4})` -> `VecGather<uint16_t>` -> `Cast<uint8_t, half, true>(CAST_NONE, {1,1,4,8})`.
3. **Single-Descriptor 2D GM<->UB DMA + 4-Event Ping-Pong + Wave-Guarded Tile Sizing**:
   - Pads UB dst row pitch to `AlignUp32(bTile * span)` so `ubSrcGapBlocks` is an exact 32B block stride — each tile executes **1** 2D `DataCopy`/`DataCopyPad` read and **1** 2D write pipelined over 2 source slots (`EVENT_ID0/1`) + 2 dest slots (`EVENT_ID2/3`).
   - Targets `MAX_EVEN_TILE_BYTES = 16 KB` / `MAX_ODD_TILE_BYTES = 8 KB` and shrinks tiles toward `targetTiles = min(coreNum, max(4, ceil(totalBytes/4KB)))` only when `nextWaves * nextA * nextB < curWaves * aTile * bTile`. `WS_SYS_SIZE = 0 U` with lazy 2-cacheline scalar `plan.GetValue()` header reads.
4. **Follow-up `ea0d191` / `6606085` (`48.83 -> 50.11`) — 32B-Chunk Vector Doubling for `Gather` Offset Tables**:
   - Seeds only `baseWords = lcm(wordsPerSpan, 8)` scalar entries (`8–16` words instead of `64–192`), issues one `S_V`, and grows row 0 in `O(log2)` steps via Level-0 `Adds<int32_t, true>` at 32B-aligned UB destinations (`-4.12 us` across 19 touched cases: `attn_0213_bf16 8.19 -> 7.31 us`, `nchw_to_nhwc 11.00 -> 10.29 us`).

## 3. Measured Speedups (`7b15ba5` -> `d4d090f` -> `6606085`, Local `profiler` Median)
- `i8_512x512 (512,512 int8)`: `513.01 -> 8.78 -> 8.54 us` (**60.1x**, 1.74x faster than torch `14.90 us`)
- `f32_1024x1024`: `28.48 -> 15.46 -> 15.26 us` (**1.87x**)
- `f32_128x128`: `6.94 -> 3.79 us` (**1.83x**); `f32_256x256`: `9.38 -> 6.09 -> 5.88 us` (**1.60x**, 2.59x vs torch)
- Full 32-case spectrum: `sum_med = 205.10 us (d4d090f) -> 200.11 us (6606085)`, **`vs_torch_med = 254.5%` (2.54x built-in torch)**.
- **Online**: `42.83 ->` **`48.83`** (`d4d090f`, `+6.00` / `+18.00w`) -> **`50.11`** (`6606085`, `+7.28` / `+21.84w` cumulative).

## 4. Precision, Sync & Anti-Pattern Invariants
- Bit-exact across all 11 dtypes (`0/120` sample, **`160/160` extended cases**, `PLAN_CHECK_OK 556–1356 cases`, offset-doubling verified over `29,667` parameter combinations).
- `srcRowsAlloc = max(bTile, ceil(ubDstPitchBytes / span))` prevents out-of-bounds UB `Gather` reads on partial tail tiles where `ubDstPitchBytes` rounds up to 32B; `hasGathered` gates `V_S` (`EVENT_ID5`) before rebuilding `offsets32` on tail geometry changes; `srcRowPitch/dstColPitch <= 0x7FFFFFFF` protects `uint32_t` `DataCopyExtParams` strides.
- **Hot-cacheline MTE2 anti-pattern (`52f8066` falsified, `48.83 -> 47.56`)**: replacing 8 cached GM scalar `plan.GetValue()` header loads with a 128 B MTE2 `DataCopy` across 32–40 cores caused all-core L2/HBM cacheline contention (`+2.13 us` mean at 32–40 blocks, `sum_med 200.11 -> 229.69 us`). Keep small `<= 128 B` broadcast headers on scalar `GetValue()`.

## 5. Reusable KernelC Pattern
> **Rule (`simd-transpose`)**: Coalesce all adjacent source-contiguous axes without UB-chunk truncation, replace fixed-latency `TransDataTo5HD` with single-descriptor 2D double-buffered Normal-mode Level-0 `Gather<u32|u16>` (widening 1B/odd dtypes bit-exactly via `Cast<half, uint8_t>` -> `Gather<u16>` -> `Cast<uint8_t, half>`), build periodic offset tables via `O(log2)` Level-0 `Adds<int32_t, true>` from `lcm(period, 8)` seeds, and never move tiny (`<= 128 B`) broadcast headers onto all-core MTE2 `DataCopy`.
