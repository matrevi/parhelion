import SwiftUI

struct BtopTelemetryDeckView: View {
    @EnvironmentObject var store: ContestStore
    let npu: NpuTelemetry

    private var samples: [TelemetrySample] {
        if !store.telemetryHistory.isEmpty { return store.telemetryHistory }
        return npu.history ?? []
    }

    var body: some View {
        VStack(spacing: 8) {
            topStatusBar
            HStack(alignment: .top, spacing: 8) {
                npuWavePanel
                    .frame(maxWidth: .infinity)

                cpuPanel
                    .frame(maxWidth: .infinity)

                memoryHierarchyPanel
                    .frame(width: 286)
            }
            .frame(height: 216)
        }
        .padding(10)
        .background(
            RoundedRectangle(cornerRadius: 11, style: .continuous)
                .fill(
                    LinearGradient(
                        colors: [BtopPalette.deckBgTop, BtopPalette.deckBgBottom],
                        startPoint: .top,
                        endPoint: .bottom
                    )
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 11, style: .continuous)
                        .strokeBorder(BtopPalette.border, lineWidth: 1)
                )
        )
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .background(Color(nsColor: .windowBackgroundColor))
    }

    // MARK: - Top Console Bar

    private var topStatusBar: some View {
        let dev = npu.primaryNpu
        let die0 = npu.dieLocks.first

        return HStack(spacing: 8) {
            Circle()
                .fill(npu.online ? BtopPalette.emerald : BtopPalette.amber)
                .frame(width: 8, height: 8)
                .shadow(color: (npu.online ? BtopPalette.emerald : BtopPalette.amber).opacity(0.8), radius: 4)

            Text(npu.online ? "\(dev?.socName ?? "soc-a")" : "NPU (\(npu.sshHost))")
                .font(.system(size: 13, weight: .heavy, design: .monospaced))
                .foregroundStyle(BtopPalette.textPrimary)

            Text("SDK \(npu.sdkVersion ?? "9.1.0")")
                .font(.system(size: 10, weight: .bold, design: .monospaced))
                .foregroundStyle(BtopPalette.cyan)
                .padding(.horizontal, 6)
                .padding(.vertical, 1.5)
                .background(Capsule().fill(BtopPalette.cyan.opacity(0.15)))

            if let bus = dev?.busId, !bus.isEmpty {
                Text(bus)
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(BtopPalette.textDim)
            }

            if let ms = npu.probeLatencyMs {
                Text("· \(npu.sshHost) \(Int(ms))ms · \(samples.count) pts")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(BtopPalette.textDim)
            }

            Spacer()

            if let lk = die0 {
                let held = lk.state == "HELD"
                let c = held ? BtopPalette.amber : BtopPalette.emerald
                HStack(spacing: 5) {
                    Image(systemName: held ? "lock.fill" : "lock.open.fill")
                        .font(.system(size: 10, weight: .bold))
                    Text(held
                         ? "DIE0 HELD · \(lk.problem ?? "Task") · \(Formatters.duration(lk.elapsedSec))"
                         : "DIE0 FREE · READY")
                        .font(.system(size: 10.5, weight: .bold, design: .monospaced))
                }
                .foregroundStyle(c)
                .padding(.horizontal, 9)
                .padding(.vertical, 3.5)
                .background(
                    Capsule()
                        .fill(c.opacity(0.14))
                        .overlay(Capsule().strokeBorder(c.opacity(0.4), lineWidth: 0.8))
                )
            }
        }
        .padding(.horizontal, 4)
    }

    // MARK: - Panel 1: NPU AICore % + Board Power W Dual Waveform

    private var npuWavePanel: some View {
        let dev = npu.primaryNpu
        let aiVals = samples.map { $0.aicorePct }
        // Scale Power (0..220W -> 0..100%) so 88W–160W swings render clearly alongside AICore %
        let pwrScaled = samples.map { min(100.0, max(0.0, ($0.powerW / 220.0) * 100.0)) }
        let curAi = dev?.aicorePct ?? Int(aiVals.last ?? 0)
        let curPwr = dev?.powerW ?? (samples.last?.powerW ?? 0)
        let curTemp = dev?.tempC ?? (samples.last?.tempC ?? 0)

        return BtopBox(keyIndex: "1", title: "NPU Core & Power", accent: BtopPalette.cyan) {
            HStack(spacing: 6) {
                Text("\(curTemp)°C")
                    .font(.system(size: 10, weight: .bold, design: .monospaced))
                    .foregroundStyle(curTemp >= 65 ? BtopPalette.amber : BtopPalette.emerald)
            }
        } content: {
            BtopWaveChart(
                traces: [
                    TelemetryTrace(
                        id: "aicore",
                        label: "AICore",
                        currentText: "\(curAi)%",
                        values: aiVals.isEmpty ? [Double(curAi)] : aiVals,
                        color: BtopPalette.cyan,
                        showBrailleColumns: true
                    ),
                    TelemetryTrace(
                        id: "power",
                        label: "Power",
                        currentText: String(format: "%.1fW", curPwr),
                        values: pwrScaled.isEmpty ? [min(100, curPwr / 2.2)] : pwrScaled,
                        color: BtopPalette.amber,
                        showBrailleColumns: false
                    )
                ],
                timestamps: samples.map { $0.ts }
            )
        }
    }

    // MARK: - Panel 2: Host CPU % Waveform + 32-Core Matrix

    private var cpuPanel: some View {
        let cpuPct = npu.cpu?.usagePct ?? (samples.last?.cpuPct ?? 0)
        let cpuSeries = samples.map { $0.cpuPct }
        let l1 = npu.cpu?.load1m ?? 0
        let l5 = npu.cpu?.load5m ?? 0

        return BtopBox(keyIndex: "2", title: "CPU · \(npu.cpu?.cores ?? 32) Cores", accent: BtopPalette.emerald) {
            Text(String(format: "LAV %.2f %.2f", l1, l5))
                .font(.system(size: 9.5, weight: .semibold, design: .monospaced))
                .foregroundStyle(BtopPalette.textDim)
        } content: {
            VStack(spacing: 6) {
                BtopWaveChart(
                    traces: [
                        TelemetryTrace(
                            id: "cpu",
                            label: "Total",
                            currentText: String(format: "%.1f%%", cpuPct),
                            values: cpuSeries.isEmpty ? [cpuPct] : cpuSeries,
                            color: BtopPalette.emerald,
                            showBrailleColumns: true
                        )
                    ],
                    timestamps: samples.map { $0.ts }
                )

                BtopCoreMatrixGrid(perCorePct: npu.cpu?.perCorePct ?? [])
            }
        }
    }

    // MARK: - Panel 3: HBM2e & System RAM Segmented Meters

    private var memoryHierarchyPanel: some View {
        let dev = npu.primaryNpu
        let hbmUsed = dev?.hbmUsedMib ?? 0
        let hbmTot = max(1, dev?.hbmTotalMib ?? 65536)
        let hbmFrac = Double(hbmUsed) / Double(hbmTot)

        let ramTot = max(1.0, npu.memory?.totalGib ?? 245.6)
        let ramUsed = npu.memory?.usedGib ?? 0
        let ramCached = npu.memory?.cachedGib ?? 0
        let ramUsedFrac = ramUsed / ramTot
        let ramCacheFrac = ramCached / ramTot

        return BtopBox(keyIndex: "3", title: "Memory", accent: BtopPalette.violet) {
            Text("HBM + DDR")
                .font(.system(size: 9.5, design: .monospaced))
                .foregroundStyle(BtopPalette.textDim)
        } content: {
            VStack(alignment: .leading, spacing: 10) {
                // HBM2e Sub-block
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Text("HBM2e")
                            .font(.system(size: 10, weight: .bold, design: .monospaced))
                            .foregroundStyle(BtopPalette.violet)
                        Spacer()
                        Text(String(format: "%.1f / %.0f GiB (%.1f%%)", Double(hbmUsed) / 1024.0, Double(hbmTot) / 1024.0, hbmFrac * 100))
                            .font(.system(size: 9.5, weight: .semibold, design: .monospaced))
                            .foregroundStyle(BtopPalette.textPrimary)
                    }
                    BtopSegmentedBar(primaryFraction: hbmFrac, primaryColor: BtopPalette.violet)
                }

                Divider().overlay(BtopPalette.border.opacity(0.6))

                // Host DDR RAM Sub-block (Used + OS Page Cache)
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Text("DRAM")
                            .font(.system(size: 10, weight: .bold, design: .monospaced))
                            .foregroundStyle(BtopPalette.sky)
                        Spacer()
                        Text(String(format: "%.1f / %.0f GiB", ramUsed, ramTot))
                            .font(.system(size: 9.5, weight: .semibold, design: .monospaced))
                            .foregroundStyle(BtopPalette.textPrimary)
                    }
                    BtopSegmentedBar(
                        primaryFraction: ramUsedFrac,
                        secondaryFraction: ramCacheFrac,
                        primaryColor: BtopPalette.sky,
                        secondaryColor: BtopPalette.indigo
                    )
                    HStack(spacing: 8) {
                        legendDot("Used \(String(format: "%.1fG", ramUsed))", BtopPalette.sky)
                        legendDot("Cache \(String(format: "%.0fG", ramCached))", BtopPalette.indigo)
                        Spacer()
                        Text(String(format: "Free %.0fG", max(0, ramTot - ramUsed - ramCached)))
                            .font(.system(size: 8.5, design: .monospaced))
                            .foregroundStyle(BtopPalette.textDim)
                    }
                }

                // Mini HBM + DRAM utilization trend
                BtopWaveChart(
                    traces: [
                        TelemetryTrace(
                            id: "hbm",
                            label: "HBM",
                            currentText: String(format: "%.1f%%", hbmFrac * 100),
                            values: samples.isEmpty ? [hbmFrac * 100] : samples.map { $0.hbmPct },
                            color: BtopPalette.violet,
                            showBrailleColumns: false
                        ),
                        TelemetryTrace(
                            id: "ram",
                            label: "RAM",
                            currentText: String(format: "%.1f%%", ramUsedFrac * 100),
                            values: samples.isEmpty ? [ramUsedFrac * 100] : samples.map { $0.ramPct },
                            color: BtopPalette.sky,
                            showBrailleColumns: false
                        )
                    ],
                    timestamps: samples.map { $0.ts },
                    yTopLabel: "100",
                    yMidLabel: "50"
                )
            }
            .padding(.top, 2)
        }
    }

    private func legendDot(_ text: String, _ color: Color) -> some View {
        HStack(spacing: 3) {
            RoundedRectangle(cornerRadius: 1.5).fill(color).frame(width: 5, height: 5)
            Text(text)
                .font(.system(size: 8.5, weight: .medium, design: .monospaced))
                .foregroundStyle(BtopPalette.textDim)
        }
    }
}
