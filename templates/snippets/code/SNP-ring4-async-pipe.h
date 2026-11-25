#pragma once
// SNP-ring4-async-pipe.h
// SoC 4-Slot Ring Buffer (MTE2 <-> PIPE_V <-> MTE3) Explicit SetFlag/WaitFlag Overlap
//
// Why: Depth-2 ping-pong often stalls when HBM latency exceeds one compute/store slice.
// A 4-slot ring with bank-staggered UB buffers keeps MTE2 prefetch, PIPE_V compute,
// and MTE3 writeback deeply overlapped without TPipe/TQue queue overhead.
//
// Safety invariant: Every HardEvent initialized/set MUST be waited an equal number of
// times before kernel return to prevent AI-Core hardware lockups.

#include "kernel_operator.h"

namespace ParhelionSnippets {
using namespace KernelC;

template <typename T, uint32_t kSlotBytes = 16384U>
__aicore__ inline void RunRing4VecPipeline(const GlobalTensor<T>& gmDst,
                                           const GlobalTensor<T>& gmSrc,
                                           int64_t totalElems,
                                           uint32_t inBaseByteOff,
                                           uint32_t outBaseByteOff) {
    constexpr uint32_t kStridedSlotBytes = (kSlotBytes + 511U) & ~511U;  // 512B cycle aligned
    constexpr int32_t kSlots = 4;
    const int64_t maxSlotElems = static_cast<int64_t>(kSlotBytes / sizeof(T));
    const event_t kEv[kSlots] = {EVENT_ID0, EVENT_ID1, EVENT_ID2, EVENT_ID3};

    int64_t offset = 0;
    int32_t step = 0;
    int64_t pendingOffset[kSlots] = {0, 0, 0, 0};
    uint32_t pendingElems[kSlots] = {0, 0, 0, 0};
    bool slotBusy[kSlots] = {false, false, false, false};

    // Prime and stream through 4 staggered UB slots
    while (offset < totalElems) {
        const int32_t s = step & 3;
        if (slotBusy[s]) {
            WaitFlag<HardEvent::MTE3_MTE2>(kEv[s]);
            slotBusy[s] = false;
        }
        int64_t n = totalElems - offset;
        if (n > maxSlotElems) n = maxSlotElems;
        const uint32_t n32 = static_cast<uint32_t>(n);

        // Bank-skewed input & output slots: in @ mod 512 == 0, out @ mod 512 == 256
        LocalTensor<T> ubIn(TPosition::VECCALC, inBaseByteOff + static_cast<uint32_t>(s) * kStridedSlotBytes, n32);
        LocalTensor<T> ubOut(TPosition::VECCALC, outBaseByteOff + static_cast<uint32_t>(s) * kStridedSlotBytes + 256U, n32);

        DataCopy(ubIn, gmSrc[offset], n32);
        SetFlag<HardEvent::MTE2_V>(kEv[s]);
        WaitFlag<HardEvent::MTE2_V>(kEv[s]);

        // Vector stage on ubIn -> ubOut (example: Muls/Add/Cast/Reduce)
        Adds(ubOut, ubIn, static_cast<T>(0), n32);
        SetFlag<HardEvent::V_MTE3>(kEv[s]);
        WaitFlag<HardEvent::V_MTE3>(kEv[s]);

        DataCopy(gmDst[offset], ubOut, n32);
        SetFlag<HardEvent::MTE3_MTE2>(kEv[s]);

        pendingOffset[s] = offset;
        pendingElems[s] = n32;
        slotBusy[s] = true;
        (void)pendingOffset;
        (void)pendingElems;
        offset += n;
        ++step;
    }

    // Drain all in-flight MTE3 stores cleanly before returning
    for (int32_t s = 0; s < kSlots; ++s) {
        if (slotBusy[s]) {
            WaitFlag<HardEvent::MTE3_MTE2>(kEv[s]);
        }
    }
}

}  // namespace ParhelionSnippets
