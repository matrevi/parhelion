import SwiftUI

struct ContestOverviewView: View {
    @EnvironmentObject var store: ContestStore
    @UIState private var showInvitePopover: Bool = false

    private var lang: AppLanguage { store.lang }

    private var selectedProblem: ProblemItem? {
        guard let probs = store.snapshot?.problems else { return nil }
        return probs.first(where: { $0.title == store.selectedProblemTitle }) ?? probs.first
    }

    var body: some View {
        if let snap = store.snapshot {
            VStack(spacing: 0) {
                headerSection(snap: snap)
                Divider()

                HSplitView {
                    ProblemTableView(problems: snap.problems)
                        .frame(minWidth: 700, idealWidth: 760)

                    if let sel = selectedProblem {
                        ProblemInspectorView(problem: sel, gitOwner: snap.contest.gitOwner)
                            .frame(minWidth: 380, idealWidth: 460)
                    }
                }
            }
        }
    }

    private func headerSection(snap: SupervisorSnapshot) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .center, spacing: 10) {
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 8) {
                        Text(snap.contest.title)
                            .font(.system(size: 16, weight: .bold))
                            .lineLimit(1)
                        if !snap.contest.teamName.isEmpty {
                            SemanticPill(text: snap.contest.teamName, icon: "person.2.fill", compact: true)
                        }
                    }
                    Text("\(snap.contest.workspace) · GitCode: \(snap.contest.gitOwner)")
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }

                Spacer()

                Button {
                    SystemNavigator.openURL(snap.contest.contestUrl)
                } label: {
                    Label(lang.tr("判题平台", "Judge"), systemImage: "safari")
                        .font(.system(size: 12, weight: .medium))
                }
                .buttonStyle(.bordered)

                Button {
                    SystemNavigator.openURL(snap.contest.gitcodeUrl)
                } label: {
                    Label(lang.tr("GitCode 仓库", "GitCode Repos"), systemImage: "chevron.left.forwardslash.chevron.right")
                        .font(.system(size: 12, weight: .medium))
                }
                .buttonStyle(.bordered)

                Button {
                    showInvitePopover.toggle()
                } label: {
                    Label(lang.tr("邀请协作者", "Invite Collaborator"), systemImage: "person.badge.plus")
                        .font(.system(size: 12, weight: .semibold))
                }
                .buttonStyle(.borderedProminent)
                .popover(isPresented: $showInvitePopover, arrowEdge: .bottom) {
                    CollaboratorPopoverView(problemCount: snap.contest.problemCount)
                        .environmentObject(store)
                }
            }

            HStack(spacing: 10) {
                let pct = (snap.contest.weightedScore / Double(max(1, snap.contest.maxWeightedScore))) * 100
                let meanRaw = snap.contest.rawScore / Double(max(1, snap.contest.problemCount))
                let recov = max(0, snap.contest.closingPotentialWeighted - snap.contest.weightedScore)
                let maeText = snap.contest.modelMaePts.map { String(format: " · MAE ±%.1f", $0) } ?? ""

                MetricTile(
                    title: lang.tr("大赛加权总分", "Weighted Total"),
                    value: String(format: "%.2f / %d", snap.contest.weightedScore, snap.contest.maxWeightedScore),
                    detail: lang.tr(String(format: "满分占比 %.1f%%", pct), String(format: "%.1f%% of ceiling", pct)),
                    icon: "scalemass.fill",
                    accent: .accentColor
                )
                MetricTile(
                    title: lang.tr("单题原始总分", "Raw Sum"),
                    value: String(format: "%.2f / %d", snap.contest.rawScore, snap.contest.maxRawScore),
                    detail: lang.tr(String(format: "单题均分 %.2f / 100", meanRaw), String(format: "Mean %.2f / 100", meanRaw)),
                    icon: "sum",
                    accent: .teal
                )
                MetricTile(
                    title: lang.tr("通过算子数", "Passing Operators"),
                    value: "\(snap.contest.passingCount) / \(snap.contest.problemCount)",
                    detail: snap.contest.passingCount < snap.contest.problemCount
                        ? lang.tr("存在可恢复算子", "Restore ready")
                        : lang.tr("全算子在榜通过", "All green on board"),
                    icon: "checkmark.seal.fill",
                    accent: snap.contest.passingCount == snap.contest.problemCount ? .green : .orange
                )
                let bestRank = snap.problems.compactMap { $0.rank }.min()
                let teams = snap.problems.compactMap { $0.totalTeams }.max()
                MetricTile(
                    title: lang.tr("最好单题名次", "Best Placement"),
                    value: bestRank.map { "#\($0)" } ?? "—",
                    detail: teams.map { lang.tr("共 \($0) 队 · 平台仅开放单题榜", "\($0) teams · per-problem boards only") }
                        ?? lang.tr("单题榜位次", "Per-problem placement"),
                    icon: "trophy.fill",
                    accent: .orange
                )
                MetricTile(
                    title: lang.tr("同代可恢复总分", "Closing Potential"),
                    value: String(format: "%.2f", snap.contest.closingPotentialWeighted),
                    detail: lang.tr(
                        String(format: "可追回 +%.2f%@", recov, maeText),
                        String(format: "+%.2f recov%@", recov, maeText)
                    ),
                    icon: "sparkles",
                    accent: .purple
                )
            }
        }
        .padding(14)
        .background(Color(nsColor: .windowBackgroundColor))
    }
}
