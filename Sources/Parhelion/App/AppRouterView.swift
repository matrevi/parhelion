import SwiftUI

enum NavigationRoute: String, CaseIterable, Identifiable {
    case contest
    case npuMonitor
    case breakthroughs
    case workspaces

    var id: String { rawValue }

    func title(_ lang: AppLanguage) -> String {
        switch self {
        case .contest: return lang.tr("赛题与榜单", "Contest & Operators")
        case .npuMonitor: return lang.tr("NPU 遥测与队列", "NPU & Task Queues")
        case .breakthroughs: return lang.tr("三元知识库", "Knowledge Hub")
        case .workspaces: return lang.tr("工作区管理", "Workspaces")
        }
    }

    func caption(_ lang: AppLanguage) -> String {
        switch self {
        case .contest: return lang.tr("实时总分 · 50 例对标", "Leaderboard · 50-Case Ratios")
        case .npuMonitor: return lang.tr("示波遥测 · 提交与门禁", "Telemetry · Queue · Gate")
        case .breakthroughs: return lang.tr("突破 · 积木 · 社区 · 进化", "Breakthrough · Snippets · Community")
        case .workspaces: return lang.tr("项目切换 · 零噪声脚手架", "Projects · Clean Scaffolder")
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

    private var lang: AppLanguage { store.lang }

    var body: some View {
        NavigationSplitView {
            sidebar
                .navigationSplitViewColumnWidth(min: 220, ideal: 244, max: 275)
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
                } else if store.workspacesLoaded && store.selectedWorkspace.isEmpty {
                    WorkspaceManagerView()
                } else {
                    emptyLoadingState
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .toolbar {
            ToolbarItemGroup(placement: .automatic) {
                Picker("", selection: Binding(
                    get: { store.lang },
                    set: { store.setLanguage($0) }
                )) {
                    Text("中文").tag(AppLanguage.zh)
                    Text("EN").tag(AppLanguage.en)
                }
                .pickerStyle(.segmented)
                .controlSize(.small)
                .frame(width: 92)
                .help(lang.tr("切换界面语言 (中文 / English)", "Switch UI Language (中文 / English)"))

                Picker(lang.tr("轮询", "Poll"), selection: $store.pollIntervalSec) {
                    Text("0.5s").tag(0.5)
                    Text("1s").tag(1.0)
                    Text("2s").tag(2.0)
                    Text("5s").tag(5.0)
                    Text("10s").tag(10.0)
                    Text("30s").tag(30.0)
                    Text(lang.tr("手动", "Manual")).tag(0.0)
                }
                .pickerStyle(.menu)
                .frame(width: 92)
                .onChange(of: store.pollIntervalSec) { _, _ in
                    store.reschedulePolling()
                }

                ToolbarRefreshButton(lang: lang) {
                    Task { await store.refreshAll() }
                }

                Button {
                    showSettings = true
                } label: {
                    Label(lang.tr("设置", "Settings"), systemImage: "gearshape")
                }
                .help(lang.tr("系统设置 (⌘,)", "Settings (⌘,)"))
            }
        }
        .sheet(isPresented: $showSettings) {
            VStack(spacing: 0) {
                HStack {
                    Label(lang.tr("Parhelion 系统设置", "Parhelion Settings"), systemImage: "slider.horizontal.3")
                        .font(.headline)
                    Spacer()
                    Picker("", selection: Binding(
                        get: { store.lang },
                        set: { store.setLanguage($0) }
                    )) {
                        Text("中文").tag(AppLanguage.zh)
                        Text("English").tag(AppLanguage.en)
                    }
                    .pickerStyle(.segmented)
                    .controlSize(.small)
                    .frame(width: 120)
                    Button(lang.tr("完成", "Done")) { showSettings = false }
                        .keyboardShortcut(.defaultAction)
                }
                .padding(.horizontal, 18)
                .padding(.vertical, 12)
                Divider()
                SettingsRootView()
                    .environmentObject(store)
            }
            .frame(width: 710, height: 540)
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
                        Text(item.title(lang))
                            .font(.system(size: 12.5, weight: .semibold))
                        Text(item.caption(lang))
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
            Button(lang.tr("管理工作区…", "Manage Workspaces…")) { route = .workspaces }
        } label: {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text(store.selectedWorkspace.isEmpty
                         ? lang.tr("未绑定工作区", "No Workspace")
                         : URL(fileURLWithPath: store.selectedWorkspace).lastPathComponent)
                        .font(.system(size: 13, weight: .semibold))
                        .lineLimit(1)
                    Text(store.selectedWorkspace.isEmpty
                         ? lang.tr("请创建或选择竞赛工作区", "Create or select a workspace")
                         : store.selectedWorkspace)
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
                NpuRouteBadgeView(fallbackCount: snap.npu.tasks.count)
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
            SidebarNpuFooterView(
                lang: lang,
                fallbackNpu: snap.npu,
                judgeLoggedIn: snap.auth.judge.loggedIn,
                gitcodeLoggedIn: snap.auth.gitcode.loggedIn,
                onOpenSettings: { showSettings = true }
            )
        }
    }

    private var emptyLoadingState: some View {
        VStack(spacing: 12) {
            if let err = store.errorMessage {
                Image(systemName: "exclamationmark.triangle")
                    .font(.system(size: 28))
                    .foregroundStyle(.orange)
                Text(lang.tr("加载工作区状态失败", "Failed to load workspace telemetry"))
                    .font(.headline)
                Text(err)
                    .font(.system(size: 12, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: 460)
                Button(lang.tr("立即重试", "Retry Now")) {
                    Task { await store.refreshAll() }
                }
                .buttonStyle(.borderedProminent)
            } else {
                ProgressView()
                Text(lang.tr("正在加载本地工作区快照…", "Loading local workspace snapshot…"))
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

private struct ToolbarRefreshButton: View {
    @EnvironmentObject var store: ContestStore
    @EnvironmentObject var hotStore: HotTelemetryStore
    let lang: AppLanguage
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            if hotStore.isRefreshingNpu || store.isRefreshingLocal {
                ProgressView().controlSize(.small)
            } else {
                HStack(spacing: 4) {
                    if hotStore.streamConnected {
                        Circle()
                            .fill(Color.green)
                            .frame(width: 6, height: 6)
                    }
                    Label(lang.tr("刷新", "Refresh"), systemImage: "arrow.clockwise")
                }
            }
        }
        .help(lang.tr("刷新本地状态与远程 NPU 遥测 (⌘R)", "Refresh local state & SSH NPU telemetry (⌘R)"))
    }
}

private struct NpuRouteBadgeView: View {
    @EnvironmentObject var hotStore: HotTelemetryStore
    let fallbackCount: Int

    var body: some View {
        let count = hotStore.npu?.tasks.count ?? fallbackCount
        if count > 0 {
            Text("\(count)")
                .font(.system(size: 10, weight: .bold, design: .monospaced))
                .padding(.horizontal, 6)
                .padding(.vertical, 2)
                .background(Capsule().fill(Color.orange.opacity(0.16)))
        }
    }
}

private struct SidebarNpuFooterView: View {
    @EnvironmentObject var hotStore: HotTelemetryStore
    let lang: AppLanguage
    let fallbackNpu: NpuTelemetry
    let judgeLoggedIn: Bool
    let gitcodeLoggedIn: Bool
    let onOpenSettings: () -> Void

    var body: some View {
        let npu = hotStore.npu ?? fallbackNpu
        VStack(alignment: .leading, spacing: 8) {
            Divider()
            HStack(spacing: 7) {
                Circle()
                    .fill(npu.online ? Color.green : (hotStore.isRefreshingNpu ? Color.blue : Color.orange))
                    .frame(width: 7.5, height: 7.5)
                VStack(alignment: .leading, spacing: 1) {
                    Text(npu.online
                         ? "\(npu.sshHost) · \(npu.primaryNpu?.socName ?? "soc-a")"
                         : (hotStore.isRefreshingNpu
                            ? lang.tr("正在探测 \(npu.sshHost)…", "Probing \(npu.sshHost)…")
                            : "\(npu.sshHost) · \(lang.tr("待机", "Standby"))"))
                        .font(.system(size: 11, weight: .semibold))
                    if let lk = npu.dieLocks.first {
                        Text(lk.state == "HELD"
                             ? "Die0: \(lk.problem ?? lang.tr("占用中", "Busy"))"
                             : lang.tr("Die0: 空闲", "Die0: Free"))
                            .font(.system(size: 10, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }
                }
                Spacer()
                if let temp = npu.primaryNpu?.tempC {
                    Text("\(temp)°C")
                        .font(.system(size: 10.5, weight: .semibold, design: .monospaced))
                        .foregroundStyle(.secondary)
                }
            }

            HStack(spacing: 6) {
                SemanticPill(text: judgeLoggedIn ? "SDK ✓" : "SDK !", compact: true)
                SemanticPill(text: gitcodeLoggedIn ? "GitCode ✓" : "GitCode !", compact: true)
                Spacer()
                Button(action: onOpenSettings) {
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
