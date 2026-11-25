#pragma once
// SNP-transdata-5hd-transpose.h
// SoC Hardware 16x16 SIMD Transpose / Interleave via TransDataTo5HD<uint16_t>
//
// Why: Replaces scalar element loops, slow Gather, or serial row reductions (such as
// column-wise bf16 accumulation or N-way narrow tensor interleaving) with fixed-function
// PIPE_V 16x16 matrix transpose hardware (up to ~9x speedup on 1024x1024 bf16).
//
// Rules:
// - Each row in srcRows[16] must be 32B-aligned in UB.
// - When repeats == 1, TransDataTo5HDParams dstRepStride & srcRepStride MUST be 0.
// - Insert PipeBarrier<PIPE_V>() after TransDataTo5HD before reading transposed UB slots.

#include "kernel_operator.h"

namespace ParhelionSnippets {
using namespace KernelC;

// Transpose a 16-row x (cols16) block of 16-bit elements (fp16/bf16/u16) into
// 16-lane interleaved columns using TransDataTo5HD<uint16_t>.
// srcRowStride16: element stride (>= cols16, multiple of 16) between consecutive src rows.
// dstColStride16: element stride (>= 16, multiple of 16) between consecutive transposed columns.
__aicore__ inline void Transpose16xColsU16(const LocalTensor<uint16_t>& dstUb,
                                           const LocalTensor<uint16_t>& srcUb,
                                           int32_t rowsValid,
                                           int32_t cols16,
                                           int32_t srcRowStride16,
                                           int32_t dstColStride16) {
    const int32_t repeats = (cols16 + 15) / 16;
    const uint16_t dstRS = (repeats == 1) ? 0U : static_cast<uint16_t>(dstColStride16);
    const uint16_t srcRS = (repeats == 1) ? 0U : 1U;
    const TransDataTo5HDParams params(false, false, static_cast<uint8_t>(repeats), dstRS, srcRS);

    uint64_t srcList[16];
    uint64_t dstList[16];
    for (int32_t r = 0; r < 16; ++r) {
        const int32_t safeR = (r < rowsValid) ? r : 0;
        srcList[r] = reinterpret_cast<uint64_t>((__ubuf__ uint16_t*)srcUb[safeR * srcRowStride16].GetPhyAddr());
        dstList[r] = reinterpret_cast<uint64_t>((__ubuf__ uint16_t*)dstUb[r * 16].GetPhyAddr());
    }

    TransDataTo5HD<uint16_t>(dstList, srcList, params);
    PipeBarrier<PIPE_V>();
}

}  // namespace ParhelionSnippets
