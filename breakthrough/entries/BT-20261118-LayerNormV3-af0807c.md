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
   Transposes 16 rows in UB via hardware `TransDataTo5HD` so column steps become contiguous vertical SIMD lanes — allowing `PIPE_V` vector `Add` + bf16 rounding to execute **exact step-by-step sequential accumulation across 16 rows in parallel**.
2. **Batched Multi-Row `ReduceSum` (`ReduceSumMultiN`)**:
   Packs multiple rows into a single vectorized reduction pass instead of paying the `~240`-cycle `ReduceSum` pipeline startup per row.
3. **Native FP16 Path & Fused Parameter Broadcast (`b1/b2`)**:
   Computes fp16 tiles natively without redundant widening rounds and fuses trailing/row-invariant `gamma`/`beta` broadcast setup.
4. **Single-`TBuf` Arena with Double-Buffered Ping-Pong**:
   Replaces multiple `TQue` descriptors with a single `TBuf` arena partitioned on the host, overlapping `MTE2` tile prefetch with `PIPE_V` normalization and `MTE3` writeback.

## 3. Precision & Synchronization Invariants
- Preserves exact sequential bf16 rounding order (`2080 == 2080` bit-identical on `(5, 4097)`), passing all 75 extended multi-dtype/broadcast cases with 3-run SHA-256 determinism.
- Explicit `V -> MTE2` staging barrier prevents ping-pong DMA overwrite before reduction completes; workspace kept at `1024 B` (avoiding `0 B` online `cpu_timeout`).

## 4. Reusable KernelC Pattern
> **Rule (`simd-transpose`)**: Whenever a golden reference mandates **non-associative sequential rounding** along a reduction axis (preventing intra-row tree reduction), transpose `16` independent rows via `TransDataTo5HD` and advance column-by-column with vector SIMD instructions across rows simultaneously.
