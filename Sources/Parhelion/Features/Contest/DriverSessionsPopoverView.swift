import SwiftUI

struct DriverSessionsPopoverView: View {
    @EnvironmentObject var store: ContestStore

    private var lang: AppLanguage { store.lang }

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

            if store.driverSessions.isEmpty {
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
                        ForEach(store.driverSessions.prefix(25)) { session in
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
                        Button {
                            SystemNavigator.openURL(session.deepLink)
                        } label: {
                            Label(lang.tr("打开", "Open"), systemImage: "arrow.up.forward.app.fill")
                                .font(.system(size: 10.5, weight: .semibold))
                        }
                        .buttonStyle(.borderedProminent)
                        .controlSize(.mini)
                        .help(session.deepLink)
                    }
                }
            }
        }
    }
}
