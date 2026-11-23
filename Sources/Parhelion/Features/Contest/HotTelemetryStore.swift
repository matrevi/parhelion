import Combine
import Foundation

/// High-frequency (@MainActor) telemetry store isolating 1Hz NPU/pipeline SSE updates
/// from heavy cold contest/leaderboard/history tables in `ContestStore`.
@MainActor
final class HotTelemetryStore: ObservableObject {
    @Published var npu: NpuTelemetry?
    @Published var telemetryHistory: [TelemetrySample] = []
    @Published var pipeline: LivePipelineState?
    @Published var schedulerHot: SchedulerHotState?
    @Published var streamConnected: Bool = false
    @Published var daemonOnline: Bool = false
    @Published var probeBackend: String = "dcmi"
    @Published var streamLatencyMs: Double = 0
    @Published var dcmiSampleMs: Double = 0
    @Published var isRefreshingNpu: Bool = false

    func applyHotFrame(_ frame: HotStreamFrame, decodeMs: Double = 0) {
        npu = frame.npu
        mergeTelemetryHistory(from: frame.npu)
        pipeline = frame.pipeline
        schedulerHot = frame.schedulerHot
        daemonOnline = true
        streamConnected = frame.daemonStats.streamConnected
        probeBackend = frame.daemonStats.probeBackend
        dcmiSampleMs = frame.daemonStats.dcmiSampleMs
        streamLatencyMs = max(frame.daemonStats.dcmiSampleMs, frame.daemonStats.hotBuildMs + decodeMs)
    }

    func applyFallbackNpu(_ newNpu: NpuTelemetry) {
        npu = newNpu
        mergeTelemetryHistory(from: newNpu)
        if let backend = newNpu.probeBackend {
            probeBackend = backend
        }
        if let ms = newNpu.sampleMs ?? newNpu.probeLatencyMs {
            dcmiSampleMs = ms
            streamLatencyMs = ms
        }
    }

    func applyColdSeed(from snap: SupervisorSnapshot) {
        if pipeline == nil, let p = snap.scheduler.pipeline {
            pipeline = p
        }
        if npu == nil && snap.npu.online {
            npu = snap.npu
            mergeTelemetryHistory(from: snap.npu)
        }
    }

    func markStreamDisconnected() {
        streamConnected = false
    }

    func markDaemonOffline() {
        daemonOnline = false
        streamConnected = false
    }

    func clearForHostSwitch() {
        npu = nil
        telemetryHistory = []
        streamConnected = false
    }

    private func mergeTelemetryHistory(from newNpu: NpuTelemetry) {
        guard let incoming = newNpu.history, !incoming.isEmpty else { return }
        var byTs: [Int: TelemetrySample] = [:]
        byTs.reserveCapacity(telemetryHistory.count + incoming.count)
        for pt in telemetryHistory {
            byTs[Int(pt.ts)] = pt
        }
        for pt in incoming {
            byTs[Int(pt.ts)] = pt
        }
        let merged = byTs.values.sorted { $0.ts < $1.ts }
        telemetryHistory = Array(merged.suffix(120))
    }
}
