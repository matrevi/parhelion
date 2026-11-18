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
                npuWaveAndDutyPanel
                    .frame(maxWidth: .infinity)

                cpuPanel
                    .frame(maxWidth: .infinity)

                memoryHierarchyPanel
                    .frame(width: 286)
            }
            .frame(height: 224)
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

    private var topStatusBar: some View {
        let dev = npu.primaryNpu
        let die0 = npu.dieLocks.first
        let slots = npu.slotsPerHr ?? 0
        let avgHold = Int(npu.avgDieHoldSec ?? 0)
        let wastePct = npu.dieDuty10m?.lockWastePct ?? 0

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

            if let ms = npu.probeLatencyMs {
                Text(String("· \(npu.sshHost) \(Int(ms))ms"))
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(BtopPalette.textDim)
            }

            Spacer()

            if npu.online && slots > 0 {
                Text(store.tr(
                    String(format: "吞吐 %.1f 轮/h · 均持锁 %ds · 锁浪费 %.0f%%", slots, avgHold, wastePct),
                    String(format: "%.1f slots/h · Hold %ds · Waste %.0f%%", slots, avgHold, wastePct)
                ))
                .font(.system(size: 10, weight: .semibold, design: .monospaced))
                .foregroundStyle(BtopPalette.sky)
                .padding(.horizontal, 8)
                .padding(.vertical, 3)
                .background(Capsule().fill(BtopPalette.sky.opacity(0.13)))
            }

            if let lk = die0 {
                let held = lk.state == "HELD"
                let c = held ? BtopPalette.amber : BtopPalette.emerald
                HStack(spacing: 5) {
                    Image(systemName: held ? "lock.fill" : "lock.open.fill")
                        .font(.system(size: 10, weight: .bold))
                    Text(held
                         ? store.tr("DIE0 占用 · \(lk.problem ?? "任务") · \(Formatters.duration(lk.elapsedSec))", "DIE0 HELD · \(lk.problem ?? "Task") · \(Formatters.duration(lk.elapsedSec))")
                         : store.tr("DIE0 空闲 · 就绪", "DIE0 FREE · READY"))
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

    // MARK: - Panel 1: NPU Vector/Core + HBM BW + Power + 10m Die Duty Bar

    private var npuWaveAndDutyPanel: some View {
        let dev = npu.primaryNpu
        let vecCoreVals = samples.map { max($0.aivectorPct ?? 0, $0.aicorePct) }
        let bwVals = samples.map { $0.hbmBwPct ?? 0 }
        let pwrScaled = samples.map { min(100.0, max(0.0, ($0.powerW / 220.0) * 100.0)) }

        let curVec = max(dev?.aivectorPct ?? 0, dev?.aicorePct ?? Int(vecCoreVals.last ?? 0))
        let curBw = dev?.hbmBwPct ?? Int(bwVals.last ?? 0)
        let curPwr = dev?.powerW ?? (samples.last?.powerW ?? 0)
        let curTemp = dev?.tempC ?? (samples.last?.tempC ?? 0)
        let ctrlPct = dev?.ctrlcpuPct ?? 0

        return BtopBox(keyIndex: "1", title: store.tr("NPU 向量/核心与功耗", "NPU Vector, Core & Power"), accent: BtopPalette.cyan) {
            HStack(spacing: 6) {
                if ctrlPct > 0 {
                    Text("Ctrl \(ctrlPct)%")
                        .font(.system(size: 9.5, design: .monospaced))
                        .foregroundStyle(BtopPalette.textDim)
                }
                Text("\(curTemp)°C")
                    .font(.system(size: 10, weight: .bold, design: .monospaced))
                    .foregroundStyle(curTemp >= 65 ? BtopPalette.amber : BtopPalette.emerald)
            }
        } content: {
            VStack(spacing: 6) {
                BtopWaveChart(
                    traces: [
                        TelemetryTrace(
                            id: "aivec",
                            label: "AIVec",
                            currentText: "\(curVec)%",
                            values: vecCoreVals.isEmpty ? [Double(curVec)] : vecCoreVals,
                            color: BtopPalette.cyan,
                            showBrailleColumns: true
                        ),
                        TelemetryTrace(
                            id: "hbmbw",
                            label: store.tr("带宽", "BW"),
                            currentText: "\(curBw)%",
                            values: bwVals.isEmpty ? [Double(curBw)] : bwVals,
                            color: BtopPalette.violet,
                            showBrailleColumns: false
                        ),
                        TelemetryTrace(
                            id: "power",
                            label: store.tr("功耗", "Power"),
                            currentText: String(format: "%.1fW", curPwr),
                            values: pwrScaled.isEmpty ? [min(100, curPwr / 2.2)] : pwrScaled,
                            color: BtopPalette.amber,
                            showBrailleColumns: false
                        )
                    ],
                    timestamps: samples.map { $0.ts }
                )

                dieDuty10mBar
            }
        }
    }

    private var dieDuty10mBar: some View {
        let duty = npu.dieDuty10m ?? DieDuty10m(npuExecPct: 0, aclHostPct: 0, lockWastePct: 0, freePct: 100)
        return VStack(alignment: .leading, spacing: 3) {
            GeometryReader { geo in
                let w = max(1, geo.size.width)
                HStack(spacing: 1) {
                    Rectangle().fill(BtopPalette.cyan).frame(width: max(duty.npuExecPct > 0 ? 3 : 0, w * (duty.npuExecPct / 100)))
                    Rectangle().fill(BtopPalette.amber).frame(width: max(0, w * (duty.aclHostPct / 100)))
                    Rectangle().fill(BtopPalette.coral).frame(width: max(0, w * (duty.lockWastePct / 100)))
                    Rectangle().fill(BtopPalette.emerald.opacity(0.28)).frame(maxWidth: .infinity)
                }
                .clipShape(Capsule())
            }
            .frame(height: 5.5)

            HStack(spacing: 7) {
                Text(store.tr("10m 占空:", "10m Duty:"))
                    .font(.system(size: 8.5, weight: .bold, design: .monospaced))
                    .foregroundStyle(BtopPalette.textDim)
                legendDot(String(format: "%@ %.1f%%", store.tr("核执行", "Exec"), duty.npuExecPct), BtopPalette.cyan)
                legendDot(String(format: "ACL %.0f%%", duty.aclHostPct), BtopPalette.amber)
                legendDot(String(format: "%@ %.0f%%", store.tr("锁浪费", "Waste"), duty.lockWastePct), BtopPalette.coral)
                Spacer()
                Text(String(format: "%@ %.0f%%", store.tr("空闲", "Free"), duty.freePct))
                    .font(.system(size: 8.5, design: .monospaced))
                    .foregroundStyle(BtopPalette.emerald)
            }
        }
    }

    // MARK: - Panel 2: Host CPU % Waveform + Lock-Free Builds + 32-Core Matrix

    private var cpuPanel: some View {
        let cpuPct = npu.cpu?.usagePct ?? (samples.last?.cpuPct ?? 0)
        let cpuSeries = samples.map { $0.cpuPct }
        let l1 = npu.cpu?.load1m ?? 0
        let nCores = npu.cpu?.cores ?? 32
        let lfCount = npu.parallelCpuBuilds?.count ?? npu.tasks.filter { $0.state == "CPU_BUILD" }.count

        return BtopBox(keyIndex: "2", title: store.tr("主机 CPU · \(nCores) 核", "CPU · \(nCores) Cores"), accent: BtopPalette.emerald) {
            HStack(spacing: 6) {
                Text(store.tr("无锁编译: \(lfCount)", "Lock-Free: \(lfCount)"))
                    .font(.system(size: 9, weight: .bold, design: .monospaced))
                    .foregroundStyle(lfCount > 0 ? BtopPalette.cyan : BtopPalette.textDim)
                Text(String(format: "%@ %.2f", store.tr("负载", "LAV"), l1))
                    .font(.system(size: 9.5, weight: .semibold, design: .monospaced))
                    .foregroundStyle(BtopPalette.textDim)
            }
        } content: {
            VStack(spacing: 6) {
                BtopWaveChart(
                    traces: [
                        TelemetryTrace(
                            id: "cpu",
                            label: store.tr("总占用", "Total"),
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

        return BtopBox(keyIndex: "3", title: store.tr("显存与内存", "Memory"), accent: BtopPalette.violet) {
            Text("HBM + DDR")
                .font(.system(size: 9.5, design: .monospaced))
                .foregroundStyle(BtopPalette.textDim)
        } content: {
            VStack(alignment: .leading, spacing: 10) {
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
                        legendDot("\(store.tr("已用", "Used")) \(String(format: "%.1fG", ramUsed))", BtopPalette.sky)
                        legendDot("\(store.tr("缓存", "Cache")) \(String(format: "%.0fG", ramCached))", BtopPalette.indigo)
                        Spacer()
                        Text(String(format: "%@ %.0fG", store.tr("空闲", "Free"), max(0, ramTot - ramUsed - ramCached)))
                            .font(.system(size: 8.5, design: .monospaced))
                            .foregroundStyle(BtopPalette.textDim)
                    }
                }

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
