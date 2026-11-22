---
id: BT-20261118-LayerNormV3-af0807c
problem: LayerNormV3
difficulty: 2
sha: af0807c2c6001fdcfd870ebb05b8262864556033
baseline_sha: bf2beb9
timestamp: "2026-11-18T06:05:19Z"
score_before: 27.13
score_after: 43.66
delta_raw: 16.53
delta_weighted: 33.06
status: verified
category: simd-transpose
tags: [bf16, fp16, TransDataTo5HD, ReduceSum, ping-pong, single-TBuf]
title: "BF16 TransDataTo5HD Vertical SIMD Accumulate + Native FP16 + Single-TBuf Ping-Pong"
---

# LayerNormV3 · `+16.53` pts (`27.13 -> 43.66`)

## 1. Context & Bottleneck
`LayerNormV3` must match the platform's per-dtype numpy golden reference bit-for-bit:
- `float32`: exact fp32 reduction.
- `float16`: fp16 `.sum()` before division + intermediate fp16 rounding points.
- `bfloat16`: **element-by-element sequential bf16 accumulation** (where once the running sum exceeds `256`, the bf16 ULP exceeds `1.0`, causing `(5, 4097)` variance sum to saturate at `2080` instead of `4208`).

In the `v1` baseline (`bf2beb9`, `27.13` pts), reproducing sequential bf16 rounding forced scalar/serial per-row accumulation, while per-row `ReduceSum` (`~240` cycles each) and 9 serial `MTE2 <-> V <-> MTE3` round trips stalled small and medium tiles.

## 2. Core Breakthrough Mechanisms
1. **BF16 `TransDataTo5HD` Vertical SIMD Accumulation (`~9x` speedup)**:
   Transposes 16–32 rows in UB via hardware `TransDataTo5HD<uint16_t>` (`set_va_reg_sb` + `scatter_vnchwconv_b32`, zero workspace) so column steps become contiguous vertical SIMD lanes — allowing `PIPE_V` vector `Add` + bf16 rounding to execute **exact step-by-step sequential accumulation across 16–32 rows in parallel** (`bf16_1024x1024`: `936.86 -> 103.54 us`, **9.05x**; **19.0x** vs `bf2beb9`'s `1969.5 us`).
2. **Batched Multi-Row `ReduceSum` (`ReduceSumMultiN`) & `resident == 2` Wide-Row Path**:
   Packs multiple rows into a single vectorized reduction pass instead of paying the `~240`-cycle `ReduceSum` pipeline startup per row; keeps wide rows (`cols <= 32K fp32 / 64K 16-bit`) single-read UB-resident.
3. **Native 128-Wide FP16 Path & Fused Parameter Broadcast (`b1/b2`)**:
   Computes fp16 tiles natively without redundant widening rounds (`f16_4096x1024`: `96.68 -> 51.00 us`, **1.90x**, beating built-in `torch` `52.44 us`) and fuses trailing/row-invariant `gamma`/`beta` broadcast setup.
4. **Single-`TBuf` Arena with 256 B Bank-Conflict Skew & Double-Buffered Ping-Pong**:
   Replaces multiple `TQue` descriptors with a single `TBuf` arena partitioned on the host with 256 B bank-conflict skew, overlapping `MTE2` tile prefetch with `PIPE_V` normalization and `MTE3` writeback (`f32_64x8192`: `27.28 -> 20.96 us`; `f32_1024x1024`: `21.20 -> 17.54 us`; online 5 public cases `11.7/11.8/23.3/11.8/23.6 -> 6.2/6.2/12.4/6.0/12.3 us`).

## 3. Precision, Synchronization & Negative-Result Invariants
- Preserves exact sequential bf16 rounding order (`2080 == 2080` bit-identical on `(5, 4097)`) plus exact IEEE-754 RNE scalar `RoundHalf` + 64-bit Tuckerman `SqrtRNE` (fixing `chunk-float16-(3,12000)` `357/36000` mismatch from AIV `vsqrt` 1-ULP midpoint flips); 75/75 extended cases 3-run SHA-256 deterministic.
- Explicit `V -> MTE2` staging barrier prevents ping-pong DMA overwrite; workspace kept at `1024 B` (note: in pure AIV kernels `SyncAll()` traps with ACL `507035` because `set_ffts_base_addr` is mix-kernel only, and kernel `workspace` arg is offset by `+16 MB` `RESERVED_WORKSPACE` — use global `::GetSysWorkSpacePtr()` if ever reading workspace).
- **Negative lesson (`64c85cf`, `43.66 -> 42.04`)**: replacing horizontal `vadd` folding + 1 `WholeReduceSum` with 2-step `WholeReduceSum` (`vcadd`) regressed `512/1024`-col shapes by `6%–11%` (`aiv_vec_ratio +18%–34%`), and unconditional `statBlock = 1` broke 32B GM alignment on large-row `m`/`r` writes — gate `statBlock = 1` strictly to low-row regimes (`rows < coreNum * (32 / sizeof(T))`).

## 4. Reusable KernelC Pattern
> **Rule (`simd-transpose`)**: Whenever a golden reference mandates **non-associative sequential rounding** along a reduction axis (preventing intra-row tree reduction), transpose `16` independent rows via `TransDataTo5HD` and advance column-by-column with vector SIMD instructions across rows simultaneously.
