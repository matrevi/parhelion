#pragma once
// SNP-direct-gm-tiling-64b.h
// soc-a Single-Cacheline 64B Direct-__gm__ Tiling + Workspace Floor Guard
//
// Why:
// 1. Replaces GET_TILING_DATA_WITH_STRUCT (which H2D-copies 184B-1704B and copies the full
//    struct onto AI-Core stack) with direct const __gm__ dereference of hot fields in
//    the first 64B cacheline.
// 2. Host side trims payload via c->GetRawTilingData()->SetDataSize(actualBytes) (64B for
//    compact regimes) and sets GetWorkspaceSizes(1)[0] = 1024 (avoiding default 16 MB penalty!).

#include <cstdint>

#if defined(__CCE_KT_TEST__) || defined(__DAV_C220__) || defined(__KERNELC__)
#include "kernel_operator.h"
#endif

namespace ParhelionSnippets {

// Pack all hot scalars required by small/mid regimes into the first 64 bytes.
struct alignas(64) CompactHotTiling64B {
    int64_t totalElems;     // [0..7]
    int64_t rows;           // [8..15]
    int64_t cols;           // [16..23]
    int32_t blockMode;      // [24..27]
    int32_t schMode;        // [28..31]
    uint32_t tileElems;     // [32..35]
    uint32_t activeCores;   // [36..39]
    uint64_t flags;         // [40..47]
    uint64_t reserved[2];   // [48..63] -> exact 64B hot cacheline
    // Optional cold-path arrays (e.g. multi-dim shapes/strides) go AFTER offset 64.
};

static_assert(sizeof(CompactHotTiling64B) == 64U, "Hot tiling header must fit in 64 bytes");

#if defined(__CCE_KT_TEST__) || defined(__DAV_C220__) || defined(__KERNELC__)
// Kernel-side zero-stack-copy direct __gm__ tiling accessor.
template <typename TilingT>
__aicore__ inline const __gm__ TilingT* DirectGmTiling(GM_ADDR tiling) {
    return reinterpret_cast<const __gm__ TilingT* __restrict__>(tiling);
}

// Compute even block split [beg, end) across activeCores for aligned chunk granularity.
__aicore__ inline void SplitAlignedRange(int64_t totalUnits, int64_t blkIdx, int64_t activeCores,
                                         int64_t alignUnits, int64_t& begOut, int64_t& endOut) {
    if (totalUnits <= 0 || blkIdx >= activeCores) {
        begOut = 0;
        endOut = 0;
        return;
    }
    const int64_t chunks = (totalUnits + alignUnits - 1) / alignUnits;
    int64_t c0 = (chunks * blkIdx / activeCores) * alignUnits;
    int64_t c1 = (chunks * (blkIdx + 1) / activeCores) * alignUnits;
    if (c0 > totalUnits) c0 = totalUnits;
    if (c1 > totalUnits) c1 = totalUnits;
    begOut = c0;
    endOut = c1;
}
#endif

// Host-side helper pattern (use inside op_host DoTiling):
//   size_t actualBytes = useCompact ? 64u : ((usedBytes + 63u) & ~63u);
//   c->GetRawTilingData()->SetDataSize(actualBytes);
//   c->GetWorkspaceSizes(1)[0] = 1024;  // Never leave default 16 MB GetLibApiWorkSpaceSize()!

}  // namespace ParhelionSnippets
