import SwiftUI

struct NpuMonitorView: View {
    @EnvironmentObject var store: ContestStore

    var body: some View {
        if let snap = store.snapshot {
            VStack(spacing: 0) {
                hardwareHeader(npu: snap.npu)
                Divider()
                SchedulerBarView(scheduler: snap.scheduler)
                Divider()

                HSplitView {
                    NpuServerQueueView(npu: snap.npu)
                        .frame(minWidth: 430, idealWidth: 530)

                    SubmissionQueueView(items: snap.submissionQueue)
                        .frame(minWidth: 500, idealWidth: 650)
                }
            }
        }
    }

    private func hardwareHeader(npu: NpuTelemetry) -> some View {
        let dev = npu.primaryNpu
        let hbmFrac = Double(dev?.hbmUsedMib ?? 0) / Double(max(1, dev?.hbmTotalMib ?? 65536))
        let ramFrac = (npu.memory?.usedGib ?? 0) / max(1.0, npu.memory?.totalGib ?? 245.6)
        let cpuFrac = (npu.cpu?.load1m ?? 0) / Double(max(1, npu.cpu?.cores ?? 32))
        let die0 = npu.dieLocks.first

        return VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Circle()
                    .fill(npu.online ? Color.green : (store.isRefreshingNpu ? Color.blue : Color.orange))
                    .frame(width: 8.5, height: 8.5)
                Text(npu.online
                     ? "\(dev?.socName ?? "soc-a") · SDK \(npu.sdkVersion ?? "9.1.0")"
                     : (store.isRefreshingNpu ? "Polling SSH \(npu.sshHost)…" : "NPU (\(npu.sshHost)) Standby"))
                    .font(.system(size: 14, weight: .bold))

                if npu.online {
                    SemanticPill(text: dev?.health ?? "OK", compact: true)
                    if let p = dev?.powerW, let t = dev?.tempC {
                        SemanticPill(text: String(format: "%.1f W · %d°C", p, t), icon: "bolt.fill", compact: true)
                    }
                    if let ms = npu.probeLatencyMs {
                        Text(String(format: "%@ · probe %.0fms", npu.sshHost, ms))
                            .font(.system(size: 11, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }
                } else if let err = npu.error {
                    Text(err).font(.system(size: 11)).foregroundStyle(.orange).lineLimit(1)
                }

                Spacer()

                if let lk = die0 {
                    HStack(spacing: 6) {
                        Image(systemName: lk.state == "HELD" ? "lock.fill" : "lock.open.fill")
                            .font(.system(size: 11))
                            .foregroundStyle(lk.state == "HELD" ? .orange : .green)
                        Text(lk.state == "HELD"
                             ? "device0.lock: HELD by \(lk.problem ?? "Task") (\(Formatters.duration(lk.elapsedSec)))"
                             : "device0.lock: FREE")
                            .font(.system(size: 11.5, weight: .semibold, design: .monospaced))
                    }
                    .padding(.horizontal, 10)
                    .padding(.vertical, 5)
                    .background(Capsule().fill((lk.state == "HELD" ? Color.orange : Color.green).opacity(0.13)))
                }
            }

            HStack(spacing: 10) {
                GaugeTile(label: "NPU AICore", valueText: "\(dev?.aicorePct ?? 0)%", trailingText: dev?.busId ?? "NPU", fraction: Double(dev?.aicorePct ?? 0) / 100.0, tint: .cyan, icon: "cpu.fill")
                GaugeTile(label: "HBM Memory", valueText: "\(dev?.hbmUsedMib ?? 0) / \(dev?.hbmTotalMib ?? 65536) MiB", trailingText: String(format: "%.1f%%", hbmFrac * 100), fraction: hbmFrac, tint: .purple, icon: "memorychip.fill")
                GaugeTile(label: "Host CPU (\(npu.cpu?.cores ?? 32)c)", valueText: String(format: "Load %.2f / %.2f", npu.cpu?.load1m ?? 0, npu.cpu?.load5m ?? 0), trailingText: "\(npu.cpu?.runningThreads ?? 0) active", fraction: cpuFrac, tint: .blue, icon: "waveform.path.ecg")
                GaugeTile(label: "System RAM", valueText: String(format: "%.1f / %.1f GiB", npu.memory?.usedGib ?? 0, npu.memory?.totalGib ?? 245.6), trailingText: String(format: "%.1f%%", ramFrac * 100), fraction: ramFrac, tint: .teal, icon: "internaldrive.fill")
            }
        }
        .padding(12)
        .background(Color(nsColor: .windowBackgroundColor))
    }
}
