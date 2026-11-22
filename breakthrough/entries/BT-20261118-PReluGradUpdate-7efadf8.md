---
id: BT-20261118-PReluGradUpdate-7efadf8
problem: PReluGradUpdate
difficulty: 1
sha: 7efadf8d437042d39fc1ce49e0e25e61c35fa591
baseline_sha: 33363f0
timestamp: "2026-11-18T10:00:00Z"
score_before: 55.76
score_after: 62.44
delta_raw: 6.68
delta_weighted: 6.68
status: verified
category: tiling-regime
tags: [schMode, 48-bank-coloring, TMP_UB_OFFSET, splitCore, Gather, BUILD_GROW, ping-pong, scalar-GM-dedup]
title: "6-schMode Static 48-Bank UB Arena + Bimodal Subcore Warmth + Deduplicated BUILD_GROW GM Reads"
---

# PReluGradUpdate · `+6.68` pts cumulative (`55.76 -> 57.81 -> 62.44`)

> **Code Provenance**: Built across `55d2f17` (V2) -> `44ba022` (UB reserve & Gather zero-init) -> `2a29531` (V3 bimodal core split, online `55.76 -> 57.81`, `+2.05`, sub `6afd557585ad43bbe0ac0c13`) -> **`7efadf8`** (V3.1 `BUILD_GROW` scalar GM read deduplication, online **`57.81 -> 62.44`**, `+4.63` single-step / `+6.68` over `33363f0`, sub `6afd76b585ad43bbe0eeb41b`, rank 74).

## 1. Context & Bottleneck
After `33363f0` (native `fp16` 128-lane tile body, `56.84` Nov 10 / `55.76` Nov 18), `profiler` `PipeUtilization` proved `PReluGradUpdate` was **not** bandwidth- or tile-count-bound (`aiv_scalar_ratio = 0.348` fp32 / `0.437` fp16 > `mte2 0.340` > `vec 0.162`):
1. `TQue` bookkeeping, runtime mode branches, and 64-bit divmod bloated scalar/icache overhead.
2. Small/unaligned `inner` with large `period = inner * wcount` hit a scalar GM-read + `PipeBarrier<PIPE_ALL>()` cliff (`4d_64_1024_3_3_f32`: `53.02 us`; `4d_4_8192_7_7_f32`: `40.54 us`).
3. Medium shapes (`2..39` blocks) rotated across cold AIV subcores on STARS/FFTS (`4d_16_64_8_8_f32`: `11.70 us`).
4. **40-core prologue GM amplification (`2a29531 -> 7efadf8`)**: moving `> 16 KB` shapes to 40 blocks caused all 40 cores on `BUILD_GROW` (`inner==1`, odd `wcount`) to simultaneously issue `base = lcm(wcount, alignE)` (up to 512) redundant scalar GM reads (`ReadWeightF32`), blowing `2d_4200_3` `dev_us` from `12.61 -> 29.23 us` until `7efadf8` deduplicated GM loads into UB.

## 2. Core Breakthrough Mechanisms
1. **6 Compile-Time `schMode` Kernels (`0..5`: `SCALAR`, `LARGE_INNER`, `PAT_ONCE`, `WINDOW`, `GATHER`, `MASKED_TILE`)**:
   - Compiled via `TPL_ARGS_DECL`, eliminating dead runtime branches and shrinking per-mode I-cache footprint ~5x.
2. **Host-Precomputed 48-Bank Conflict-Free Static UB Arena (`PopulateUbLayout`)**:
   - Single `TBuf<VECCALC>` strictly below `184 KB`, coloring 8 ping-pong I/O buffers at distinct `mod 1536 B` residues (`0, 128, 256, 384, 512, 640, 1280, 1408`) and pattern buffers at `768/896`; zero device modulo/alignment math; `bf16` reduced from 4 -> 2 fp32 scratch buffers (`tileCap 4096 -> 6144`, `fp16` up to `8192`).
3. **Manual `(EVENT_ID0, EVENT_ID1)` Ping-Pong + Single-Tile Straight-Line Path + Level-0 Whole-Repeat `Mul`/`Muls`**:
   - `MODE_LARGE_INNER` uses direct per-channel `Muls` with zero pattern buffer; `MODE_GATHER` builds byte-offset table once via `DupMaskedF32` + `Cast` + exponential `Adds` and expands `MTE2`-streamed `kCh` channel weights via `Gather`.
4. **Bimodal Subcore I-Cache Warmth (`splitCore`)**:
   - `<= 16 KB -> 1 block`, `> 16 KB -> 40 blocks` balanced via `blockFactorHi/Lo` (`512 B`-aligned on large tensors) so launch 0 warms all 40 AIV I-caches simultaneously.
5. **Deduplicated `BUILD_GROW` Scalar GM Reads (`7efadf8`)**:
   - Reads the `wc` unique channel weights from GM **once** into `pat32[0..wc-1]`, then replicates `wc..base-1` purely inside UB before exponential `ReplicateOnPipeV` (`Muls(..., 1.0f)`), cutting scalar GM traffic by `alignE / gcd(wcount, alignE)` (**8x** odd fp32 / **16x** odd fp16/bf16) across all 40 cores.

## 3. Measured Speedups (`33363f0` -> `2a29531` -> `7efadf8`, `profiler` / `aclrtEvent`)
- `4d_64_1024_3_3_f32`: `53.02 -> 9.82 us` (**5.40x**); `f16`: `51.70 -> 10.94 us` (**4.73x**)
- `4d_4_8192_7_7_f32`: `40.54 -> 13.52 us` (**3.00x**)
- `4d_16_64_8_8_f32`: `11.70 -> 5.88 us` (**1.99x**); `2d_256_64_f32`: `9.80 -> 5.66 us` (**1.73x**)
- `4d_8_64_64_64` (`f16/f32/bf16`): `13.50 -> 8.06 us` (**1.67x**), `16.72 -> 10.92 us` (**1.53x**), `18.94 -> 12.38 us` (**1.53x**)
- `4d_16_64_32_32` (1M, `f32/f16/bf16`): `12.58 -> 8.96 us` (`1.87 TB/s`, min `8.68 us`), `9.94 -> 6.78 us` (**1.47x**), `13.30 -> 9.28 us` (**1.43x**)
- `2d_4200_3` (`7efadf8` fix): `aclrtEvent` `29.23 -> 12.90 us` (**2.27x**), `profiler 9.94 -> 6.64 us` (**1.50x**).
- **Online**: `55.76 -> 57.81 ->` **`62.44`** (**`+6.68` cumulative**).

## 4. Precision & Synchronization Invariants
- **`dav_c220` `TMP_UB_OFFSET = ubSize - 8 KB` (`184 KB`)**: hardware `Select` uses internal scratch at `TMP_UB_OFFSET`; static UB arenas **must** reserve `>= 10 KB` (`UB_RESERVE = 10*1024`) or `Select` silently corrupts top-of-UB user tensors.
- Zero-initialize `wBuf[0..1]` before `Gather` + `V_MTE2` sync so padding lanes past `kCh` never gather UB NaN/garbage.
- `fp16` vector ops silently drop tails unless count is rounded up to 128 (`+192` slack in tile buffers); `bf16` keeps fp32 compute detour (`Compare/Select/Muls` lack bf16 overloads).
- `93/93` isolated extended cases green (`fp32/fp16/bf16`, all 6 `schMode`s, `features==0` edge).

## 5. Reusable KernelC Pattern
> **Rule (`tiling-regime`)**: Specialize broadcast regimes into compile-time `TILING_KEY` kernels over a host-colored 48-bank static UB arena strictly below `ubSize - 10 KB` (`TMP_UB_OFFSET`), use bimodal `1`-or-`all-AIV` dispatch to keep subcore I-caches warm, and strictly deduplicate per-core prologue scalar GM reads into UB before replicating across 40 cores.
