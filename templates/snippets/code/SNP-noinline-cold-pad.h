#pragma once
// SNP-noinline-cold-pad.h
// soc-a I-Cache Diet (<8 KB Hot .text) + Deduplicated noinline DataCopyPad
//
// Why: soc-a has a 32 KB L1 I-Cache and Judge scores 3 cold runs.
// Expanding DataCopyPad / fallback branches inline across multiple schModes bloats
// .o size (up to 172 KB) and adds +1.5 to +2.8 us cold I-Cache miss tax (plus
// 420s compile TLE risk). Isolating unaligned/fallback paths into static
// __attribute__((noinline)) helpers keeps fast aligned DataCopy hot loops lean.
//
// Reminder: DataCopyPad UB stride unit is 32B blocks, GM stride unit is BYTES!

#include "kernel_operator.h"

namespace ParhelionSnippets {
using namespace KernelC;

template <typename T>
static __attribute__((noinline)) __aicore__ void Load1DPadCold(
    const LocalTensor<T>& ub, const GlobalTensor<T>& gm, int64_t gmElemOff, uint32_t bytes) {
    DataCopyPadExtParams<T> padParams{false, 0, 0, static_cast<T>(0)};
    DataCopyPad(ub, gm[gmElemOff], DataCopyExtParams{1, bytes, 0, 0, 0}, padParams);
}

template <typename T>
static __attribute__((noinline)) __aicore__ void Store1DPadCold(
    const GlobalTensor<T>& gm, const LocalTensor<T>& ub, int64_t gmElemOff, uint32_t bytes) {
    DataCopyPad(gm[gmElemOff], ub, DataCopyExtParams{1, bytes, 0, 0, 0});
}

// Hot aligned fast-path stays inline; unaligned edge tails jump to deduplicated cold helper.
template <typename T>
__aicore__ inline void Load1DFastOrColdPad(
    const LocalTensor<T>& ub, const GlobalTensor<T>& gm, int64_t gmElemOff, int64_t count) {
    const uint32_t bytes = static_cast<uint32_t>(count * static_cast<int64_t>(sizeof(T)));
    const bool aligned = ((bytes & 31U) == 0U) &&
                         (((gmElemOff * static_cast<int64_t>(sizeof(T))) & 31LL) == 0LL);
    if (aligned) {
        DataCopy(ub, gm[gmElemOff], static_cast<uint32_t>(count));
    } else {
        Load1DPadCold<T>(ub, gm, gmElemOff, bytes);
    }
}

template <typename T>
__aicore__ inline void Store1DFastOrColdPad(
    const GlobalTensor<T>& gm, const LocalTensor<T>& ub, int64_t gmElemOff, int64_t count) {
    const uint32_t bytes = static_cast<uint32_t>(count * static_cast<int64_t>(sizeof(T)));
    const bool aligned = ((bytes & 31U) == 0U) &&
                         (((gmElemOff * static_cast<int64_t>(sizeof(T))) & 31LL) == 0LL);
    if (aligned) {
        DataCopy(gm[gmElemOff], ub, static_cast<uint32_t>(count));
    } else {
        Store1DPadCold<T>(gm, ub, gmElemOff, bytes);
    }
}

}  // namespace ParhelionSnippets
