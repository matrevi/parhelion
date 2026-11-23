---
id: HW-16kb-aiv-l1i-hot-packing
pillar: hardware
portability: native-accel
status: verified
confidence: 0.875
bottlenecks: [icache-bloat, cold-launch-tax]
regimes: [PUBLIC, S_SMALL_CORE, M_MID_UB]
dtypes: [float32, float16, bfloat16, int32, int8]
---

# Accel soc-a 16 KB AIV L1I Iron Law & Multi-TU Hybrid Linker Layout

## 1. 16 KB AIV L1I Packing Law
On Accel soc-a (`arch-v1`), keeping the hottest kernel entries (`_0.._2` / `_0.._3`) packed contiguously inside **`0x0000..0x4000` (16 KB AIV L1 I-Cache)** while minimizing total `.text` and `.o_max` delivers consistent online gains (verified across `MultilabelMarginLoss`, `CTCLossV2`, `ConfusionTransposeD`, `PadV3Grad`, and `StridedSliceGrad`).

## 2. Multi-TU Hybrid Linker Layout
- **Cross-`schMode` shared helpers**: declare `inline __attribute__((noinline))` so the linker deduplicates via **WEAK COMDAT**, saving **5–10 KB** of `.text`/`.o` bloat without breaking L1I packing.
- **Single-`schMode` hot helpers**: declare `static __attribute__((noinline))` so the helper stays immediately adjacent to its caller in `.text` with zero cross-section jump penalty.
