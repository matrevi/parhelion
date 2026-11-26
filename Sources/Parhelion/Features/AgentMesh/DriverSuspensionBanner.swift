import SwiftUI

/// Drivers paused by a provider fault (driver, reason, since, affected workers) with the
/// administrator's explicit "Resume" action. Shown on the collaboration page and in the
/// driver diagnostics of Settings; nothing is drawn while no driver is suspended.
struct DriverSuspensionBanner: View {
    @EnvironmentObject var store: ContestStore
    @EnvironmentObject var hotStore: HotTelemetryStore
    @State private var resuming: Set<String> = []
    /// Restrict to one driver (Settings rows); nil lists every suspension.
    var driver: String? = nil

    private var lang: AppLanguage { store.lang }

    private var rows: [DriverSuspension] {
        let all = hotStore.agentMesh?.suspensions ?? []
        guard let driver else { return all }
        return all.filter { $0.driver == driver.lowercased() }
    }

    var body: some View {
        if !rows.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                ForEach(rows) { row in
                    HStack(alignment: .top, spacing: 8) {
                        Image(systemName: "pause.circle.fill").foregroundStyle(.orange)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(lang.tr("驱动 \(row.driver) 已暂停", "Driver \(row.driver) suspended"))
                                .font(.system(size: 11, weight: .semibold))
                            Text(row.reason).font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(2)
                            Text(lang.tr("自 ", "since ") + row.since
                                 + (row.workers.isEmpty ? "" : lang.tr(" · 受影响：", " · affected: ")
                                    + row.workers.joined(separator: ", ")))
                                .font(.system(size: 9, design: .monospaced)).foregroundStyle(.secondary)
                                .lineLimit(2)
                        }
                        Spacer()
                        Button(lang.tr("解除暂停", "Resume")) {
                            Task { await resume(row.driver) }
                        }
                        .controlSize(.small)
                        .disabled(resuming.contains(row.driver))
                        .help(lang.tr("确认供应商已恢复后解除；之后可重新派发 worker",
                                      "Release after the provider recovered; workers can then be dispatched again"))
                    }
                }
            }
            .padding(8)
            .background(RoundedRectangle(cornerRadius: 8).fill(Color.orange.opacity(0.10)))
        }
    }

    private func resume(_ driver: String) async {
        resuming.insert(driver)
        defer { resuming.remove(driver) }
        do {
            try await store.daemonClient.resumeDriver(
                workspace: store.selectedWorkspace, host: store.activeSSHHost, driver: driver
            )
            if let mesh = try? await store.daemonClient.fetchAgentMesh(
                workspace: store.selectedWorkspace, host: store.activeSSHHost
            ) {
                hotStore.agentMesh = mesh
            }
        } catch {
            store.errorMessage = error.localizedDescription
        }
    }
}
