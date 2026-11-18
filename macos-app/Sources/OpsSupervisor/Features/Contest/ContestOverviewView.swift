import SwiftUI

struct ContestOverviewView: View {
    @EnvironmentObject var store: ContestStore
    @UIState private var showInvitePopover: Bool = false

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
                        .frame(minWidth: 530, idealWidth: 650)

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
                    Label("Judge", systemImage: "safari")
                        .font(.system(size: 12, weight: .medium))
                }
                .buttonStyle(.bordered)

                Button {
                    SystemNavigator.openURL(snap.contest.gitcodeUrl)
                } label: {
                    Label("GitCode Repos", systemImage: "chevron.left.forwardslash.chevron.right")
                        .font(.system(size: 12, weight: .medium))
                }
                .buttonStyle(.bordered)

                Button {
                    showInvitePopover.toggle()
                } label: {
                    Label("Invite Collaborator", systemImage: "person.badge.plus")
                        .font(.system(size: 12, weight: .semibold))
                }
                .buttonStyle(.borderedProminent)
                .popover(isPresented: $showInvitePopover, arrowEdge: .bottom) {
                    CollaboratorPopoverView(problemCount: snap.contest.problemCount)
                        .environmentObject(store)
                }
            }

            HStack(spacing: 10) {
                MetricTile(
                    title: "Weighted Total",
                    value: String(format: "%.2f / %d", snap.contest.weightedScore, snap.contest.maxWeightedScore),
                    detail: String(format: "%.1f%% of ceiling", (snap.contest.weightedScore / Double(max(1, snap.contest.maxWeightedScore))) * 100),
                    icon: "scalemass.fill",
                    accent: .accentColor
                )
                MetricTile(
                    title: "Raw Sum",
                    value: String(format: "%.2f / %d", snap.contest.rawScore, snap.contest.maxRawScore),
                    detail: String(format: "Mean %.2f / 100", snap.contest.rawScore / Double(max(1, snap.contest.problemCount))),
                    icon: "sum",
                    accent: .teal
                )
                MetricTile(
                    title: "Passing Operators",
                    value: "\(snap.contest.passingCount) / \(snap.contest.problemCount)",
                    detail: snap.contest.passingCount < snap.contest.problemCount ? "Restore ready" : "All green on board",
                    icon: "checkmark.seal.fill",
                    accent: snap.contest.passingCount == snap.contest.problemCount ? .green : .orange
                )
                MetricTile(
                    title: "Closing Potential",
                    value: String(format: "%.2f", snap.contest.closingPotentialWeighted),
                    detail: String(format: "+%.2f weighted recoverable", max(0, snap.contest.closingPotentialWeighted - snap.contest.weightedScore)),
                    icon: "sparkles",
                    accent: .purple
                )
            }
        }
        .padding(14)
        .background(Color(nsColor: .windowBackgroundColor))
    }
}
