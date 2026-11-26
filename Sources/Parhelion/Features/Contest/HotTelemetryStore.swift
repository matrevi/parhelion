import Combine
import Foundation

/// One device host's live telemetry as the deck and lanes show it (4.3.2).
struct DeviceHostState: Identifiable {
    let host: String
    let primary: Bool
    var streamConnected: Bool
    var lastError: String?
    var npu: NpuTelemetry?
    var history: [TelemetrySample]
    var id: String { host }

    var dieHeld: Bool { npu?.dieLocks.contains { $0.state == "HELD" } ?? false }
}

/// High-frequency (@MainActor) telemetry store isolating 1Hz NPU/pipeline SSE updates
/// from heavy cold contest/leaderboard/history tables in `ContestStore`.
@MainActor
final class HotTelemetryStore: ObservableObject {
    @Published var npu: NpuTelemetry?
    @Published var telemetryHistory: [TelemetrySample] = []
    @Published var pipeline: LivePipelineState?
    @Published var schedulerHot: SchedulerHotState?
    @Published var agentMesh: AgentMeshHotState?
    @Published var testRuns: [TestRunEntry] = []
    @Published var streamConnected: Bool = false
    @Published var daemonOnline: Bool = false
    @Published var probeBackend: String = "dcmi"
    @Published var streamLatencyMs: Double = 0
    @Published var dcmiSampleMs: Double = 0
    @Published var isRefreshingNpu: Bool = false
    /// Further device hosts of the pool (`npu_hosts` minus the primary), in pool order.
    @Published var extraHosts: [DeviceHostState] = []
    @Published var primaryHostName: String?
    /// Host the deck shows; `nil` means the primary.
    @Published var selectedHost: String?

    /// Every device host, the primary first; a daemon without `npu_hosts` yields the primary alone.
    var deviceHosts: [DeviceHostState] {
        let primary = DeviceHostState(
            host: primaryHostName ?? npu?.sshHost ?? "npu",
            primary: true,
            streamConnected: streamConnected,
            lastError: npu?.error,
            npu: npu,
            history: telemetryHistory
        )
        return [primary] + extraHosts
    }

    var selectedDevice: DeviceHostState {
        let hosts = deviceHosts
        return hosts.first { $0.host == selectedHost } ?? hosts[0]
    }

    /// Tasks of every host, each tagged with its host, so the lanes can show all devices at once.
    var allDeviceTasks: [NpuTaskItem] {
        deviceHosts.flatMap { dev in
            (dev.npu?.tasks ?? []).map { task in
                var tagged = task
                tagged.host = dev.host
                return tagged
            }
        }
    }

    func applyHotFrame(_ frame: HotStreamFrame, decodeMs: Double = 0) {
        npu = frame.npu
        mergeTelemetryHistory(from: frame.npu)
        pipeline = frame.pipeline
        schedulerHot = frame.schedulerHot
        if let mesh = frame.agentMesh {
            agentMesh = mesh
        }
        if let runs = frame.testRuns, runs != testRuns {
            testRuns = runs
        }
        applyDeviceHosts(frame.npuHosts)
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

    /// Cold snapshots only *seed* hot state; they never override a live hot frame. While the
    /// daemon is offline there is no hot source, so the cold pipeline must keep flowing in
    /// (otherwise the first seeded pipeline would freeze forever). Cold `npu` is a placeholder
    /// (`online == false`) and is never allowed to replace hot NPU data.
    func applyColdSeed(from snap: SupervisorSnapshot) {
        if let p = snap.scheduler.pipeline, pipeline == nil || !daemonOnline {
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
        schedulerHot = nil
        agentMesh = nil
    }

    /// Workspace switch: drop the previous workspace's submit pipeline / scheduler hot state.
    func resetPipelineState() {
        pipeline = nil
        schedulerHot = nil
        agentMesh = nil
    }

    func clearForHostSwitch() {
        npu = nil
        telemetryHistory = []
        streamConnected = false
        extraHosts = []
        primaryHostName = nil
        selectedHost = nil
    }

    func selectHost(_ host: String) {
        selectedHost = (host == deviceHosts.first?.host) ? nil : host
    }

    private func applyDeviceHosts(_ rows: [DeviceHostFrame]?) {
        guard let rows, !rows.isEmpty else {
            if !extraHosts.isEmpty { extraHosts = [] }
            primaryHostName = nil
            return
        }
        primaryHostName = rows.first { $0.primary }?.host
        var next: [DeviceHostState] = []
        for row in rows where !row.primary {
            let prev = extraHosts.first { $0.host == row.host }
            let telemetry = row.npu ?? prev?.npu
            next.append(DeviceHostState(
                host: row.host,
                primary: false,
                streamConnected: row.streamConnected,
                lastError: row.lastError,
                npu: telemetry,
                history: Self.mergedHistory(prev?.history ?? [], row.npu?.history)
            ))
        }
        extraHosts = next
        if let sel = selectedHost, !deviceHosts.contains(where: { $0.host == sel }) {
            selectedHost = nil
        }
    }

    private func mergeTelemetryHistory(from newNpu: NpuTelemetry) {
        guard let incoming = newNpu.history, !incoming.isEmpty else { return }
        telemetryHistory = Self.mergedHistory(telemetryHistory, incoming)
    }

    /// Keeps the newest 120 one-second points, de-duplicated by second.
    private static func mergedHistory(_ existing: [TelemetrySample], _ incoming: [TelemetrySample]?) -> [TelemetrySample] {
        guard let incoming, !incoming.isEmpty else { return existing }
        var byTs: [Int: TelemetrySample] = [:]
        byTs.reserveCapacity(existing.count + incoming.count)
        for pt in existing {
            byTs[Int(pt.ts)] = pt
        }
        for pt in incoming {
            byTs[Int(pt.ts)] = pt
        }
        let merged = byTs.values.sorted { $0.ts < $1.ts }
        return Array(merged.suffix(120))
    }
}
