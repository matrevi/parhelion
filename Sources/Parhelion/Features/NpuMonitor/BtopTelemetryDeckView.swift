import SwiftUI

struct BtopTelemetryDeckView: View {
    @EnvironmentObject var store: ContestStore
    @EnvironmentObject var hotStore: HotTelemetryStore
    let npu: NpuTelemetry

    /// The device the deck shows: the row picked in the device roster, else the primary host.
    private var device: DeviceHostState { hotStore.selectedDevice }
    private var multiHost: Bool { hotStore.deviceHosts.count > 1 }

    private var liveNpu: NpuTelemetry {
        if device.primary { return hotStore.npu ?? npu }
        return device.npu ?? .offline(host: device.host, error: device.lastError)
    }

    private var samples: [TelemetrySample] {
        if !device.history.isEmpty { return device.history }
        return liveNpu.history ?? []
    }

    /// Panel titles name the shown host once there is more than one.
    private func hostSuffix() -> String { multiHost ? "@\(device.host)" : "" }

    var body: some View {
        VStack(spacing: 8) {
            topStatusBar
            HStack(alignment: .top, spacing: 8) {
                npuWaveAndDutyPanel
                    .frame(maxWidth: .infinity)

                cpuPanel
                    .frame(maxWidth: .infinity)

                deviceRosterPanel
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
        let curNpu = liveNpu
        let dev = curNpu.primaryNpu
        let die0 = curNpu.dieLocks.first
        let slots = curNpu.slotsPerHr ?? 0
        let avgHold = curNpu.avgDieHoldSec ?? 0
        let wastePct = curNpu.dieDuty10m?.lockWastePct ?? 0
        let dieLabel = multiHost ? "\(device.host) DIE0" : "DIE0"

        return HStack(spacing: 8) {
            Circle()
                .fill(curNpu.online ? BtopPalette.emerald : BtopPalette.amber)
                .frame(width: 8, height: 8)
                .shadow(color: (curNpu.online ? BtopPalette.emerald : BtopPalette.amber).opacity(0.8), radius: 4)

            Text(curNpu.online ? "\(dev?.socName ?? "soc-a")" : "NPU (\(curNpu.sshHost))")
                .font(.system(size: 13, weight: .heavy, design: .monospaced))
                .foregroundStyle(BtopPalette.textPrimary)

            Text("SDK \(curNpu.sdkVersion ?? "9.1.0")")
                .font(.system(size: 10, weight: .bold, design: .monospaced))
                .foregroundStyle(BtopPalette.cyan)
                .padding(.horizontal, 6)
                .padding(.vertical, 1.5)
                .background(Capsule().fill(BtopPalette.cyan.opacity(0.15)))

            if device.streamConnected {
                let hubMs = device.primary ? hotStore.dcmiSampleMs : 0
                let latMs = hubMs > 0 ? hubMs : (curNpu.sampleMs ?? curNpu.probeLatencyMs ?? 0)
                let backend = device.primary ? hotStore.probeBackend : (curNpu.probeBackend ?? "dcmi")
                Text(String(format: "· %@ %@ %.1fms", curNpu.sshHost, backend.uppercased(), latMs))
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(BtopPalette.emerald)
            } else if let ms = curNpu.sampleMs ?? curNpu.probeLatencyMs {
                Text(String(format: "· %@ %.0fms", curNpu.sshHost, ms))
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(BtopPalette.textDim)
            }

            Spacer()

            if multiHost {
                let hosts = hotStore.deviceHosts
                let busy = hosts.filter { $0.dieHeld }.count
                Text(store.tr("\(hosts.count) 设备 · \(busy) 占用", "\(hosts.count) devices · \(busy) busy"))
                    .font(.system(size: 10, weight: .semibold, design: .monospaced))
                    .foregroundStyle(BtopPalette.violet)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 3)
                    .background(Capsule().fill(BtopPalette.violet.opacity(0.14)))
            }

            if curNpu.online && slots > 0 {
                Text(store.tr(
                    String(format: "理论 %.0f 次/h · 均持锁 %.2fs · 锁浪费 %.0f%%", slots, avgHold, wastePct),
                    String(format: "Capacity %.0f/h · Mean hold %.2fs · Waste %.0f%%", slots, avgHold, wastePct)
                ))
                .font(.system(size: 10, weight: .semibold, design: .monospaced))
                .foregroundStyle(BtopPalette.sky)
                .padding(.horizontal, 8)
                .padding(.vertical, 3)
                .background(Capsule().fill(BtopPalette.sky.opacity(0.13)))
            }

            if let lk = die0 {
                let held = lk.state == "HELD"
                let duration = lk.elapsedSec.map { (lk.stage == "UNREGISTERED" ? "≥" : "") + Formatters.duration($0) } ?? store.tr("持锁起点未记录", "Start unrecorded")
                let c = held ? BtopPalette.amber : BtopPalette.emerald
                HStack(spacing: 5) {
                    Image(systemName: held ? "lock.fill" : "lock.open.fill")
                        .font(.system(size: 10, weight: .bold))
                    Text(held
                         ? store.tr("\(dieLabel) 占用 · \(lk.problem ?? "任务") · \(duration)", "\(dieLabel) HELD · \(lk.problem ?? "Task") · \(duration)")
                         : store.tr("\(dieLabel) 空闲 · 就绪", "\(dieLabel) FREE · READY"))
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
        let dev = liveNpu.primaryNpu
        let vecCoreVals = samples.map { max($0.aivectorPct ?? 0, $0.aicorePct) }
        let bwVals = samples.map { $0.hbmBwPct ?? 0 }
        let pwrScaled = samples.map { min(100.0, max(0.0, ($0.powerW / 220.0) * 100.0)) }

        let curVec = max(dev?.aivectorPct ?? 0, dev?.aicorePct ?? Int(vecCoreVals.last ?? 0))
        let curBw = dev?.hbmBwPct ?? Int(bwVals.last ?? 0)
        let curPwr = dev?.powerW ?? (samples.last?.powerW ?? 0)
        let curTemp = dev?.tempC ?? (samples.last?.tempC ?? 0)
        let ctrlPct = dev?.ctrlcpuPct ?? 0

        return BtopBox(keyIndex: "1", title: store.tr("NPU 向量/核心与功耗", "NPU Vector, Core & Power"), accent: BtopPalette.cyan, titleSuffix: hostSuffix()) {
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
        let duty = liveNpu.dieDuty10m ?? DieDuty10m(npuExecPct: 0, aclHostPct: 0, lockWastePct: 0, freePct: 100)
        return VStack(alignment: .leading, spacing: 3) {
            dutyStrip(duty, height: 5.5)

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
        let curNpu = liveNpu
        let cpuPct = curNpu.cpu?.usagePct ?? (samples.last?.cpuPct ?? 0)
        let cpuSeries = samples.map { $0.cpuPct }
        let l1 = curNpu.cpu?.load1m ?? 0
        let nCores = curNpu.cpu?.cores ?? 32
        let lfCount = curNpu.parallelCpuBuilds?.count ?? curNpu.tasks.filter { $0.state == "CPU_BUILD" }.count

        return BtopBox(keyIndex: "2", title: store.tr("主机 CPU · \(nCores) 核", "CPU · \(nCores) Cores"), accent: BtopPalette.emerald, titleSuffix: hostSuffix()) {
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

                BtopCoreMatrixGrid(perCorePct: curNpu.cpu?.perCorePct ?? [])
            }
        }
    }

    // MARK: - Panel 3: Device roster (one row per device host, click to show it in panels 1-2)

    private var deviceRosterPanel: some View {
        let hosts = hotStore.deviceHosts
        let busy = hosts.filter { $0.dieHeld }.count
        return BtopBox(keyIndex: "3", title: store.tr("NPU 设备 · \(hosts.count)", "NPU Devices · \(hosts.count)"), accent: BtopPalette.violet) {
            Text(store.tr("\(busy) 占用 · \(hosts.count - busy) 空闲", "\(busy) busy · \(hosts.count - busy) free"))
                .font(.system(size: 9.5, design: .monospaced))
                .foregroundStyle(BtopPalette.textDim)
        } content: {
            ScrollView(.vertical, showsIndicators: hosts.count > 4) {
                VStack(spacing: 4) {
                    ForEach(hosts) { dev in
                        deviceRow(dev)
                    }
                    if hosts.count == 1 {
                        Text(store.tr("在「设置 › NPU SSH」勾选「设备池」可加入更多设备",
                                      "Add devices under Settings › NPU SSH › Device pool"))
                            .font(.system(size: 9, design: .monospaced))
                            .foregroundStyle(BtopPalette.textDim)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.top, 4)
                    }
                }
            }
        }
    }

    private func deviceRow(_ dev: DeviceHostState) -> some View {
        let tele = dev.npu
        let chip = tele?.primaryNpu
        let online = (tele?.online ?? false) && dev.streamConnected
        let lock = tele?.dieLocks.first
        let held = lock?.state == "HELD"
        let waiting = tele?.tasks.filter { $0.state == "WAITING" }.count ?? 0
        let building = tele?.tasks.filter { $0.state == "CPU_BUILD" }.count ?? 0
        let vec = max(chip?.aivectorPct ?? 0, chip?.aicorePct ?? 0)
        let temp = chip?.tempC ?? 0
        let selected = dev.host == device.host
        let tint = !online ? BtopPalette.coral : (held ? BtopPalette.amber : BtopPalette.emerald)
        let holdText = lock?.elapsedSec.map { Formatters.duration($0) } ?? ""

        return Button {
            hotStore.selectHost(dev.host)
        } label: {
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 5) {
                    Circle().fill(tint).frame(width: 6, height: 6)
                        .shadow(color: tint.opacity(0.7), radius: 3)
                    Text(dev.host)
                        .font(.system(size: 10.5, weight: .heavy, design: .monospaced))
                        .foregroundStyle(BtopPalette.textPrimary)
                    if dev.primary && hotStore.deviceHosts.count > 1 {
                        Text(store.tr("主", "MAIN"))
                            .font(.system(size: 8, weight: .bold, design: .monospaced))
                            .foregroundStyle(BtopPalette.sky)
                            .padding(.horizontal, 4)
                            .background(Capsule().fill(BtopPalette.sky.opacity(0.15)))
                    }
                    Spacer(minLength: 2)
                    if online {
                        Text("\(temp)°C")
                            .foregroundStyle(temp >= 65 ? BtopPalette.amber : BtopPalette.textDim)
                        Text(String(format: "%.0fW", chip?.powerW ?? 0))
                            .foregroundStyle(BtopPalette.textDim)
                        Text("\(vec)%")
                            .foregroundStyle(BtopPalette.cyan)
                    } else {
                        Text(store.tr("离线", "OFFLINE"))
                            .foregroundStyle(BtopPalette.coral)
                    }
                }
                .font(.system(size: 9.5, weight: .semibold, design: .monospaced))

                HStack(spacing: 4) {
                    Image(systemName: held ? "lock.fill" : "lock.open.fill")
                        .font(.system(size: 8, weight: .bold))
                    Text(held ? "\(lock?.problem ?? store.tr("任务", "task")) · \(holdText)" : store.tr("空闲 · 就绪", "free · ready"))
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Spacer(minLength: 2)
                    Text(store.tr("等 \(waiting) · 编 \(building)", "W \(waiting) · B \(building)"))
                        .foregroundStyle(waiting > 0 ? BtopPalette.amber : BtopPalette.textDim)
                }
                .font(.system(size: 9, weight: .semibold, design: .monospaced))
                .foregroundStyle(held ? BtopPalette.amber : BtopPalette.textDim)

                dutyStrip(tele?.dieDuty10m ?? DieDuty10m(npuExecPct: 0, aclHostPct: 0, lockWastePct: 0, freePct: 100), height: 3)
            }
            .padding(.horizontal, 7)
            .padding(.vertical, 5)
            .background(
                RoundedRectangle(cornerRadius: 6, style: .continuous)
                    .fill(selected ? BtopPalette.violet.opacity(0.14) : Color.white.opacity(0.03))
                    .overlay(
                        RoundedRectangle(cornerRadius: 6, style: .continuous)
                            .strokeBorder(selected ? BtopPalette.violet.opacity(0.75) : BtopPalette.border.opacity(0.6),
                                          lineWidth: selected ? 1.2 : 0.8)
                    )
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help(online
              ? store.tr("点击在左侧面板查看 \(dev.host)；条形为 10 分钟占空（核执行 / ACL / 锁浪费 / 空闲）",
                         "Show \(dev.host) in the panels on the left; the bar is the 10-minute duty (exec / ACL / waste / free)")
              : (dev.lastError ?? store.tr("\(dev.host) 遥测流未连接", "\(dev.host) telemetry stream not connected")))
    }

    /// The 10-minute duty split: kernel execution, ACL host work, lock waste, free.
    private func dutyStrip(_ duty: DieDuty10m, height: CGFloat) -> some View {
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
        .frame(height: height)
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
