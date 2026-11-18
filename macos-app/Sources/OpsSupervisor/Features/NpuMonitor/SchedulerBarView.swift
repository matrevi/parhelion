import SwiftUI

struct SchedulerBarView: View {
    @EnvironmentObject var store: ContestStore
    let scheduler: SchedulerState

    @StateObject private var ticker = CooldownTicker()
    @UIState private var reserveVal: Double = 12
    @UIState private var noiseVal: Double = 2.7
    @UIState private var initialized: Bool = false

    var body: some View {
        let cd = ticker.remainingSec
        let gap = max(1, scheduler.rateLimitGapSec)

        HStack(spacing: 14) {
            Toggle(
                isOn: Binding(
                    get: { scheduler.autoConsumerEnabled },
                    set: { v in Task { await store.updateScheduler(autoConsumer: v) } }
                )
            ) {
                HStack(spacing: 5) {
                    Image(systemName: scheduler.autoConsumerEnabled ? "bolt.horizontal.circle.fill" : "pause.circle")
                        .foregroundStyle(scheduler.autoConsumerEnabled ? .green : .secondary)
                    Text("Auto-Consumer")
                        .font(.system(size: 12, weight: .semibold))
                }
            }
            .toggleStyle(.switch)
            .controlSize(.small)

            Divider().frame(height: 22)

            SemanticPill(
                text: cd == 0 ? "RATE READY" : "COOLING \(cd)s / \(gap)s",
                icon: cd == 0 ? "checkmark.bolt.fill" : "timer",
                compact: true
            )

            Divider().frame(height: 22)

            HStack(spacing: 8) {
                VStack(alignment: .leading, spacing: 1.5) {
                    Text("Quota: \(scheduler.usedToday24h)/\(scheduler.dailyQuotaLimit) · Avail \(scheduler.availableNormalShots)")
                        .font(.system(size: 10.5, weight: .semibold, design: .monospaced))
                    Text("Closing Reserve: \(Int(reserveVal)) shots")
                        .font(.system(size: 9.5))
                        .foregroundStyle(.secondary)
                }
                Slider(value: $reserveVal, in: 0...25, step: 1) { editing in
                    if !editing {
                        Task { await store.updateScheduler(closingReserve: Int(reserveVal)) }
                    }
                }
                .frame(width: 95)
            }

            Divider().frame(height: 22)

            HStack(spacing: 6) {
                Text(String(format: "Noise Δ ≥ %.1f", noiseVal))
                    .font(.system(size: 11, weight: .semibold, design: .monospaced))
                Stepper("", value: $noiseVal, in: 0.5...8.0, step: 0.2)
                    .labelsHidden()
                    .controlSize(.small)
                    .onChange(of: noiseVal) { _, nv in
                        Task { await store.updateScheduler(noiseFloor: nv) }
                    }
            }

            Spacer()

            Button {
                Task { await store.drainQueueOnce() }
            } label: {
                Label("Drain Next", systemImage: "paperplane.fill")
                    .font(.system(size: 11.5, weight: .semibold))
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.small)
            .disabled(scheduler.queueLockHeld)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
        .background(Color.primary.opacity(0.025))
        .onAppear {
            ticker.sync(scheduler.cooldownRemainingSec)
            let storeRef = store
            ticker.onZeroReached = { [weak storeRef] in
                Task { @MainActor in
                    guard let storeRef, storeRef.snapshot?.scheduler.autoConsumerEnabled == true else { return }
                    await storeRef.drainQueueOnce()
                }
            }
            if !initialized {
                reserveVal = Double(scheduler.closingReserve)
                noiseVal = scheduler.noiseFloorDelta
                initialized = true
            }
        }
        .onChange(of: scheduler.cooldownRemainingSec) { _, newSec in
            ticker.sync(newSec)
        }
    }
}
