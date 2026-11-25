---
id: HW-compiler-abi-7arg-unroll-guard
pillar: hardware
portability: anti-pattern
status: anti-pattern
confidence: 0.167
bottlenecks: [icache-bloat, scalar-bound, cold-launch-tax]
regimes: [PUBLIC, S_SMALL_CORE, M_MID_UB, L_HBM_TILED]
dtypes: [float32, float16, bfloat16, int32]
---

# [ANTI-PATTERN] Kernel Compiler (`arch-v1`) & ABI Redlines

## 1. Inner-Loop `noinline` & Short-Loop `unroll(disable)`
- **NEVER** call a `noinline` helper inside a hot inner tile loop.
- **NEVER** place `#pragma clang loop unroll(disable)` on `rank <= 8` short loops (online-verified `-7.24 pts` drop on `StridedSliceGrad a690e69`).

## 2. >7–8 Scalar Argument HBM Stack Spill Cliff
- **NEVER** pass more than **7–8 scalar arguments** (or `const Struct&` / `LocalTensor&` by reference) into a `noinline` function.
- Excess arguments inevitably spill onto the **HBM stack**, adding microseconds of latency per call (online/profiler verified across `CTCLossV2`, `ConcatD`, `PadV3Grad`, and `StridedSliceGrad`). Pack hot parameters into compact scalar registers or `direct __gm__` pointers.
