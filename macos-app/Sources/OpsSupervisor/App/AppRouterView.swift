import SwiftUI

enum NavigationRoute: String, CaseIterable, Identifiable {
    case contest = "Contest & Operators"
    case npuMonitor = "NPU & Task Queues"
    case breakthroughs = "Breakthroughs"
    case workspaces = "Workspaces"

    var id: String { rawValue }

    var caption: String {
        switch self {
        case .contest: return "Leaderboard · 50-Case Ratios"
        case .npuMonitor: return "Telemetry · Queue · Breakthrough"
        case .breakthroughs: return "Structured Playbook · Δ Filter"
        case .workspaces: return "Projects · Clean Scaffolder"
        }
    }

    var symbol: String {
        switch self {
        case .contest: return "chart.bar.xaxis"
        case .npuMonitor: return "cpu.fill"
        case .breakthroughs: return "sparkles.rectangle.stack.fill"
        case .workspaces: return "folder.badge.gearshape"
        }
    }
}

struct AppRouterView: View {
    @EnvironmentObject var store: ContestStore
    @UIState private var route: NavigationRoute = .contest
    @UIState private var showSettings: Bool = false

    var body: some View {
        NavigationSplitView {
            sidebar
                .navigationSplitViewColumnWidth(min: 220, ideal: 242, max: 275)
        } detail: {
            VStack(spacing: 0) {
                if let banner = store.bannerMessage {
                    statusStrip(text: banner, isError: false) { store.bannerMessage = nil }
                } else if let err = store.errorMessage {
                    statusStrip(text: err, isError: true) { store.errorMessage = nil }
                }

                if store.snapshot != nil {
                    switch route {
                    case .contest: ContestOverviewView()
                    case .npuMonitor: NpuMonitorView()
                    case .breakthroughs: BreakthroughLibraryView()
                    case .workspaces: WorkspaceManagerView()
                    }
                } else {
                    emptyLoadingState
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .toolbar {
            ToolbarItemGroup(placement: .automatic) {
                Picker("Poll", selection: $store.pollIntervalSec) {
                    Text("2s").tag(2)
                    Text("5s").tag(5)
                    Text("10s").tag(10)
                    Text("30s").tag(30)
                    Text("Manual").tag(0)
                }
                .pickerStyle(.menu)
                .frame(width: 90)
                .onChange(of: store.pollIntervalSec) { _, _ in
                    store.reschedulePolling()
                }

                Button {
                    Task { await store.refreshAll() }
                } label: {
                    if store.isRefreshingNpu || store.isRefreshingLocal {
                        ProgressView().controlSize(.small)
                    } else {
                        Label("Refresh", systemImage: "arrow.clockwise")
                    }
                }
                .help("Refresh local state & SSH NPU telemetry (⌘R)")

                Button {
                    showSettings = true
                } label: {
                    Label("Settings", systemImage: "gearshape")
                }
                .help("Settings (⌘,)")
            }
        }
        .sheet(isPresented: $showSettings) {
            VStack(spacing: 0) {
                HStack {
                    Label("Parhelion Settings", systemImage: "slider.horizontal.3")
                        .font(.headline)
                    Spacer()
                    Button("Done") { showSettings = false }
                        .keyboardShortcut(.defaultAction)
                }
                .padding(.horizontal, 18)
                .padding(.vertical, 12)
                Divider()
                SettingsRootView()
                    .environmentObject(store)
            }
            .frame(width: 700, height: 540)
        }
    }

    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 12) {
            workspaceSwitcher
                .padding(.horizontal, 12)
                .padding(.top, 10)

            List(NavigationRoute.allCases, selection: $route) { item in
                HStack(spacing: 10) {
                    Image(systemName: item.symbol)
                        .font(.system(size: 13.5, weight: .semibold))
                        .frame(width: 20)
                        .foregroundStyle(route == item ? Color.accentColor : .secondary)
                    VStack(alignment: .leading, spacing: 1.5) {
                        Text(item.rawValue)
                            .font(.system(size: 12.5, weight: .semibold))
                        Text(item.caption)
                            .font(.system(size: 10))
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    routeBadge(item)
                }
                .padding(.vertical, 3.5)
                .tag(item)
            }
            .listStyle(.sidebar)

            Spacer(minLength: 0)
            sidebarFooter
        }
    }

    private var workspaceSwitcher: some View {
        Menu {
            ForEach(store.workspaces) { entry in
                Button {
                    Task { await store.switchWorkspace(entry.workspace) }
                } label: {
                    HStack {
                        Text(URL(fileURLWithPath: entry.workspace).lastPathComponent)
                        if entry.workspace == store.selectedWorkspace {
                            Image(systemName: "checkmark")
                        }
                    }
                }
            }
            Divider()
            Button("Manage Workspaces…") { route = .workspaces }
        } label: {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text(URL(fileURLWithPath: store.selectedWorkspace).lastPathComponent)
                        .font(.system(size: 13, weight: .semibold))
                        .lineLimit(1)
                    Text(store.selectedWorkspace)
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
                Spacer(minLength: 4)
                Image(systemName: "chevron.up.chevron.down")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(.secondary)
            }
            .padding(8)
            .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Color.primary.opacity(0.05)))
        }
        .buttonStyle(.plain)
    }

    @ViewBuilder
    private func routeBadge(_ item: NavigationRoute) -> some View {
        if let snap = store.snapshot {
            switch item {
            case .contest:
                Text(String(format: "%.0f", snap.contest.weightedScore))
                    .font(.system(size: 10, weight: .bold, design: .monospaced))
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(Capsule().fill(Color.accentColor.opacity(0.14)))
            case .npuMonitor:
                if !snap.npu.tasks.isEmpty {
                    Text("\(snap.npu.tasks.count)")
                        .font(.system(size: 10, weight: .bold, design: .monospaced))
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(Capsule().fill(Color.orange.opacity(0.16)))
                }
            case .breakthroughs:
                let cnt = store.breakthroughEntries.filter { $0.deltaRaw >= store.breakthroughThreshold }.count
                if cnt > 0 {
                    Text("\(cnt)")
                        .font(.system(size: 10, weight: .bold, design: .monospaced))
                        .foregroundStyle(Color(red: 0.88, green: 0.46, blue: 0.08))
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(Capsule().fill(Color(red: 0.92, green: 0.52, blue: 0.10).opacity(0.16)))
                }
            case .workspaces:
                EmptyView()
            }
        }
    }

    @ViewBuilder
    private var sidebarFooter: some View {
        if let snap = store.snapshot {
            VStack(alignment: .leading, spacing: 8) {
                Divider()
                HStack(spacing: 7) {
                    Circle()
                        .fill(snap.npu.online ? Color.green : (store.isRefreshingNpu ? Color.blue : Color.orange))
                        .frame(width: 7.5, height: 7.5)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(snap.npu.online
                             ? "\(snap.npu.sshHost) · \(snap.npu.primaryNpu?.socName ?? "soc-a")"
                             : (store.isRefreshingNpu ? "Probing \(snap.npu.sshHost)…" : "\(snap.npu.sshHost) · Standby"))
                            .font(.system(size: 11, weight: .semibold))
                        if let lk = snap.npu.dieLocks.first {
                            Text(lk.state == "HELD" ? "Die0: \(lk.problem ?? "Busy")" : "Die0: Free")
                                .font(.system(size: 10, design: .monospaced))
                                .foregroundStyle(.secondary)
                        }
                    }
                    Spacer()
                    if let temp = snap.npu.primaryNpu?.tempC {
                        Text("\(temp)°C")
                            .font(.system(size: 10.5, weight: .semibold, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }
                }

                HStack(spacing: 6) {
                    SemanticPill(text: snap.auth.judge.loggedIn ? "SDK ✓" : "SDK !", compact: true)
                    SemanticPill(text: snap.auth.gitcode.loggedIn ? "GitCode ✓" : "GitCode !", compact: true)
                    Spacer()
                    Button { showSettings = true } label: {
                        Image(systemName: "gearshape.fill")
                            .font(.system(size: 12))
                            .foregroundStyle(.secondary)
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.horizontal, 14)
            .padding(.bottom, 12)
        }
    }

    private var emptyLoadingState: some View {
        VStack(spacing: 12) {
            if let err = store.errorMessage {
                Image(systemName: "exclamationmark.triangle")
                    .font(.system(size: 28))
                    .foregroundStyle(.orange)
                Text("Failed to load workspace telemetry")
                    .font(.headline)
                Text(err)
                    .font(.system(size: 12, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: 460)
                Button("Retry Now") {
                    Task { await store.refreshAll() }
                }
                .buttonStyle(.borderedProminent)
            } else {
                ProgressView()
                Text("Loading local workspace snapshot…")
                    .font(.system(size: 12.5))
                    .foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func statusStrip(text: String, isError: Bool, onDismiss: @escaping () -> Void) -> some View {
        HStack(spacing: 8) {
            Image(systemName: isError ? "exclamationmark.triangle.fill" : "checkmark.circle.fill")
                .foregroundStyle(isError ? .orange : .green)
            Text(text)
                .font(.system(size: 12, weight: .medium))
                .lineLimit(1)
            Spacer()
            Button(action: onDismiss) {
                Image(systemName: "xmark")
                    .font(.system(size: 10, weight: .bold))
            }
            .buttonStyle(.plain)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 7)
        .background((isError ? Color.orange : Color.green).opacity(0.11))
    }
}
