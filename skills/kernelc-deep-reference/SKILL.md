---
name: kernelc-deep-reference
description: Optional fallback reference for KernelC soc-a hardware pitfalls (DataCopyPad alignment, PipeBarrier hazards, fp32 accumulation, profiler device timing, and 50-case tbest/time contest optimization). Normal workflow uses AGENTS.md + `opsb` CLI directly.
---

# KernelC soc-a Contest Optimization · Deep Fallback Reference

> **Architecture Note**: For standard problem reading, NPU testing, and gated submission, follow the workspace's **`AGENTS.md`** and run `opsb test` / `opsb submit` / `opsb history` directly. Load this skill **only** when debugging subtle Accel soc-a (`soc-a`) hardware/ISA behavior or breaking through a roofline plateau.

## 1. Hardware Ground Truth (Accel soc-a · SDK 9.1.0)
- **Compute Units**: Up to `20` AI Cores (`40` Vector AIV cores in `KERNEL_TYPE_AIV_ONLY`), `192 KB` Unified Buffer (UB) per AIV.
- **32-Byte Alignment Rule**: All UB addresses and standard `DataCopy` lengths must be multiples of `32 B` (`8 × fp32` or `16 × fp16/bf16`). Always use `DataCopyExtParams` + `DataCopyPad` for unaligned GM tails, and place any sentinel/scratch buffers at least `32 B` past the padded write region so `DataCopyPad` tail padding never clobbers sentinels.
- **Launch & Prologue Floor**: Bare device kernel dispatch is `~3.4–4.2 µs`, plus `~65–70 ns` per launched block. On small shapes (`< 64 KB`), shrink `blockDim` to `1..4` and replace tiny GM descriptor DMAs with packed prologue scalar loads.

## 2. Contest Score Math & Shape-Spectrum Strategy
- **Formula**: `score = 100 * mean_{i=1..50}(tbest_i / time_i)` (`5` public cases worth `<= 10` pts + `45` hidden cases worth `<= 90` pts).
- **Zero-Tolerance Failure Cost**: A single `Wrong Answer`, `Runtime Error`, or `Time Limit Exceeded` on any of the `45` hidden cases zeroes the **entire** problem (`0.00` pts), and leaderboard mode is `latest`.
- **Whole-Spectrum Validation**: Never overfit to the `5` small public cases (`~2.6–7.5` pts total). Benchmark across small (`< 16 KB`), mid (`64 KB – 2 MB`), and large (`4 MB – 64 MB`) + odd/prime/empty shapes, and verify multi-run bit-identical determinism (`0` drift across repeated launches).

## 3. Key Pipeline & Precision Idioms
1. **FP16/BF16 Precision**: Upcast `fp16`/`bf16` inputs via `Cast(..., RoundMode::CAST_NONE)` to `fp32` for reductions (`LayerNorm`, `BatchNorm`, `Loss`, `PReLU` grad sum), unless golden explicitly mandates sequential per-element rounding.
2. **In-Chain Hazards**: When reusing UB buffers across recurrence steps or `Gather` chains, insert explicit `PipeBarrier<PIPE_V>()` where producer/consumer vector instructions overlap sub-tensors; verify under stress (`>= 50` back-to-back launches on dirty GM buffers).
3. **Device Timing Only**: Measure `Task Duration(us)` from `profiler` (`op_summary*.csv`), never Python host `time.perf_counter()`.
