import AppKit
import SwiftUI

struct DriverSessionsPopoverView: View {
    @EnvironmentObject var store: ContestStore
    @UIState private var selectedDriverFilter: String = "all"

    private var lang: AppLanguage { store.lang }

    private var availableDrivers: [String] {
        let found = Set(store.driverSessions.map { $0.driver.lowercased() })
        return ["all"] + found.sorted()
    }

    private var effectiveDriverFilter: String {
        availableDrivers.contains(selectedDriverFilter) ? selectedDriverFilter : "all"
    }

    private func driverCount(_ drv: String) -> Int {
        drv == "all"
            ? store.driverSessions.count
            : store.driverSessions.filter { $0.driver.lowercased() == drv }.count
    }

    private var visibleSessions: [DriverSessionModel] {
        let filtered = effectiveDriverFilter == "all"
            ? store.driverSessions
            : store.driverSessions.filter { $0.driver.lowercased() == effectiveDriverFilter }
        return Array(filtered.prefix(25))
    }

    private func supportsDirectThreadJump(_ session: DriverSessionModel) -> Bool {
        let dl = session.deepLink.lowercased()
        return dl.hasPrefix("codex://threads/") && !dl.hasPrefix("codex://threads/new")
    }

    private func openSession(_ session: DriverSessionModel) {
        let direct = supportsDirectThreadJump(session)
        if !direct && !session.displayTitle.isEmpty {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(session.displayTitle, forType: .string)
        }
        SystemNavigator.openURL(session.deepLink)
        if !direct {
            let shortcut = session.driver.lowercased() == "deepseek" ? "⌘K 粘贴" : "搜索粘贴"
            store.bannerMessage = lang.tr(
                "已唤起 \(session.driver)（桌面端暂不支持直达指定对话深链，已复制标题「\(session.displayTitle)」方便\(shortcut)定位）",
                "Focused \(session.driver) (app lacks per-thread deep link; copied title \"\(session.displayTitle)\" to clipboard)"
            )
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Image(systemName: "bubble.left.and.text.bubble.right.fill")
                    .foregroundStyle(Color.accentColor)
                Text(lang.tr("活跃调优会话 (GUI + CLI)", "Active Harness Sessions"))
                    .font(.system(size: 13, weight: .bold))
                SemanticPill(text: store.activeDriver.uppercased(), compact: true)
                Spacer()
                Button {
                    Task { await store.syncActiveDriverWorkspace() }
                } label: {
                    Label(lang.tr("同步", "Sync"), systemImage: "arrow.triangle.2.circlepath")
                        .font(.system(size: 10.5))
                }
                .buttonStyle(.bordered)
                .controlSize(.mini)
                .disabled(store.isSyncingDriver)

                Button {
                    Task { await store.reloadDriverSessions() }
                } label: {
                    if store.isLoadingSessions {
                        ProgressView().controlSize(.small)
                    } else {
                        Image(systemName: "arrow.clockwise")
                            .font(.system(size: 11))
                    }
                }
                .buttonStyle(.bordered)
                .controlSize(.mini)
            }

            Divider()

            if availableDrivers.count > 2 {
                HStack(spacing: 5) {
                    ForEach(availableDrivers, id: \.self) { drv in
                        let active = effectiveDriverFilter == drv
                        let count = driverCount(drv)
                        let label = drv == "all"
                            ? "\(lang.tr("全部", "All")) (\(count))"
                            : "\(drv) (\(count))"
                        Button(label) {
                            selectedDriverFilter = drv
                        }
                        .buttonStyle(.bordered)
                        .controlSize(.mini)
                        .tint(active ? .accentColor : .secondary)
                    }
                    Spacer()
                }
            }

            if visibleSessions.isEmpty {
                VStack(spacing: 6) {
                    Text(lang.tr("当前工作区暂无观测到的会话", "No active or recent sessions found for workspace"))
                        .font(.system(size: 11.5))
                        .foregroundStyle(.secondary)
                    Text(lang.tr("点击任意算子行的「新开对话 / Launch」即可通过 Deep-Link 拉起 GUI 会话。",
                                 "Click Launch on any operator to open a deep-linked GUI session."))
                        .font(.system(size: 10.5))
                        .foregroundStyle(.tertiary)
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 24)
            } else {
                ScrollView {
                    LazyVStack(spacing: 7) {
                        ForEach(visibleSessions) { session in
                            sessionRow(session)
                        }
                    }
                }
                .frame(maxHeight: 340)
            }
        }
        .padding(14)
        .frame(width: 470)
        .onAppear {
            Task { await store.reloadDriverSessions() }
        }
    }

    private func sessionRow(_ session: DriverSessionModel) -> some View {
        let isRunning = session.status.lowercased() == "running"
        return SurfaceCard(highlighted: isRunning, highlightColor: .green, padding: 9) {
            VStack(alignment: .leading, spacing: 5) {
                HStack(spacing: 6) {
                    SemanticPill(text: session.status.uppercased(), compact: true)
                    Text(session.driver)
                        .font(.system(size: 10.5, weight: .bold, design: .monospaced))
                        .foregroundStyle(.secondary)
                    Text(session.displayTitle)
                        .font(.system(size: 12, weight: .semibold))
                        .lineLimit(1)
                    Spacer()
                    Text(session.formattedTimestamp)
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.tertiary)
                }

                HStack(spacing: 8) {
                    Text(session.problem.isEmpty ? session.shortWorktree : "\(session.problem) · \(session.shortWorktree)")
                        .font(.system(size: 10.5, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                    if session.observedSteps > 0 {
                        Text("\(session.observedSteps) steps")
                            .font(.system(size: 10, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }
                    if session.tokensUsed > 0 {
                        Text("\(session.tokensUsed) tok")
                            .font(.system(size: 10, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }
                    if let tool = session.lastTool, !tool.isEmpty {
                        Text("· \(tool)")
                            .font(.system(size: 10, design: .monospaced))
                            .foregroundStyle(.tertiary)
                            .lineLimit(1)
                    }
                    Spacer()

                    if !session.worktree.isEmpty {
                        Button {
                            SystemNavigator.revealInFinder(session.worktree)
                        } label: {
                            Image(systemName: "folder")
                                .font(.system(size: 10))
                        }
                        .buttonStyle(.bordered)
                        .controlSize(.mini)
                        .help(session.worktree)
                    }

                    if !session.deepLink.isEmpty {
                        let direct = supportsDirectThreadJump(session)
                        Button {
                            openSession(session)
                        } label: {
                            Label(
                                direct ? lang.tr("直达对话", "Open") : lang.tr("唤起+复制", "Focus"),
                                systemImage: direct ? "arrow.up.forward.app.fill" : "macwindow"
                            )
                            .font(.system(size: 10.5, weight: .semibold))
                        }
                        .buttonStyle(.borderedProminent)
                        .controlSize(.mini)
                        .help(direct
                            ? session.deepLink
                            : lang.tr(
                                "\(session.deepLink)（该桌面端不支持直达具体对话 ID，点击将唤起应用并复制会话标题）",
                                "\(session.deepLink) (app lacks thread deep link; focuses app & copies title)"
                            ))
                    }
                }
            }
        }
    }
}
