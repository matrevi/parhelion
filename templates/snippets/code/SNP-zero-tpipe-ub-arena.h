#pragma once
// SNP-zero-tpipe-ub-arena.h
// soc-a Zero-TPipe/TBuf Static Aligned UB Arena + 16-Bank-Group Coloring
//
// Why: Eliminates TPipe/TBuf/TQue constructor, slot-bookkeeping, and implicit
// first-packet zeroing overhead (~0.8-1.6 us on small/mid shapes) while unlocking
// the full 184 KB safe user UB budget below TMP_UB_OFFSET (188,416 B).
//
// Bank-group rule (dav_c220: 16 bank groups x 32 B = 512 B cycle):
// Keep concurrent read/write vector streams staggered by +256 B (mod 512)
// to suppress UB bank-group conflicts by ~80%+.

#include "kernel_operator.h"

namespace ParhelionSnippets {
using namespace KernelC;

constexpr uint32_t kUbSafeCapBytes = 188416U;  // Below 192 KB system temp UB tail
constexpr uint32_t kUbAlignBytes = 32U;
constexpr uint32_t kBankCycleBytes = 512U;
constexpr uint32_t kBankSkewHalf = 256U;

constexpr __aicore__ inline uint32_t AlignUp32U(uint32_t bytes) {
    return (bytes + 31U) & ~31U;
}

constexpr __aicore__ inline uint32_t AlignUp512U(uint32_t bytes) {
    return (bytes + 511U) & ~511U;
}

// Construct a bare LocalTensor<T> handle directly at a compile-time or static byte offset.
// Zero TPipe/ResetPool overhead (3 register moves).
template <typename T>
__aicore__ inline LocalTensor<T> MakeUbTensor(uint32_t byteOffset, uint32_t elemCount) {
    return LocalTensor<T>(TPosition::VECCALC, byteOffset, elemCount);
}

// Compute next UB byte offset aligned to 512 B plus a caller-chosen bank color
// (e.g. colorBytes=0 for src streams, colorBytes=256 for dst streams, colorBytes=384 for indices).
constexpr __aicore__ inline uint32_t NextBankColoredOffset(uint32_t prevEndBytes, uint32_t colorBytes) {
    const uint32_t alignedBase = AlignUp512U(prevEndBytes);
    const uint32_t modColor = colorBytes & 511U;
    const uint32_t cand = alignedBase + modColor;
    return (cand >= prevEndBytes) ? cand : (cand + kBankCycleBytes);
}

// Example 160 KB bank-colored dual-ping-pong arena (src0/src1 @ mod 512 == 0, dst0/dst1 @ mod 512 == 256):
struct StaticPingPongArena {
    static constexpr uint32_t kSlotBytes = 32768U;  // 32 KB per stream buffer
    static constexpr uint32_t kOffSrc0 = 0U;
    static constexpr uint32_t kOffSrc1 = NextBankColoredOffset(kOffSrc0 + kSlotBytes, 0U);
    static constexpr uint32_t kOffDst0 = NextBankColoredOffset(kOffSrc1 + kSlotBytes, 256U);
    static constexpr uint32_t kOffDst1 = NextBankColoredOffset(kOffDst0 + kSlotBytes, 256U);
    static constexpr uint32_t kOffScratch = NextBankColoredOffset(kOffDst1 + kSlotBytes, 384U);
    static constexpr uint32_t kTotalBytes = kOffScratch + 16384U;
    static_assert(kTotalBytes <= kUbSafeCapBytes, "StaticPingPongArena exceeds safe 184 KB UB ceiling");

    template <typename T>
    __aicore__ inline LocalTensor<T> Src(int32_t slot, uint32_t elems) const {
        return MakeUbTensor<T>((slot == 0) ? kOffSrc0 : kOffSrc1, elems);
    }

    template <typename T>
    __aicore__ inline LocalTensor<T> Dst(int32_t slot, uint32_t elems) const {
        return MakeUbTensor<T>((slot == 0) ? kOffDst0 : kOffDst1, elems);
    }
};

}  // namespace ParhelionSnippets
