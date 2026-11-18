import SwiftUI

struct ProblemInspectorView: View {
    @EnvironmentObject var store: ContestStore
    let problem: ProblemItem
    let gitOwner: String
    @UIState private var expandedGroups: Set<Int> = []

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 13) {
                headerRow

                if problem.bestScore > problem.currentScore + 0.2 && !problem.bestSha.isEmpty {
                    restoreCallout
                }

                accountingCard
                publicRatioCard
                all50CasesCard
                timelineCard
            }
            .padding(14)
        }
        .background(Color(nsColor: .windowBackgroundColor))
    }

    private var headerRow: some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 8) {
                    Text(problem.title).font(.system(size: 16, weight: .bold))
                    DifficultyBadge(difficulty: problem.difficulty)
                    SemanticPill(text: problem.wsptTag, compact: true)
                }
                Text("\(gitOwner)/\(problem.repoSlug) · Rank \(problem.rank.map { "#\($0)" } ?? "—") / \(problem.totalTeams ?? 0)")
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
            Spacer()
            HStack(spacing: 6) {
                Button {
                    SystemNavigator.openURL("https://judge.invalid/problems/\(problem.problemId)")
                } label: { Image(systemName: "globe") }
                .help("Open on Judge")

                Button {
                    SystemNavigator.openURL("https://gitcode.com/\(gitOwner)/\(problem.repoSlug)")
                } label: { Image(systemName: "arrow.up.forward.square") }
                .help("Open GitCode Repository")
            }
        }
    }

    private var restoreCallout: some View {
        HStack(spacing: 10) {
            Image(systemName: "clock.arrow.circlepath").foregroundStyle(.orange)
            VStack(alignment: .leading, spacing: 1) {
                Text(String(format: "Best Validated: %.2f (%@)", problem.bestScore, String(problem.bestSha.prefix(7))))
                    .font(.system(size: 11.5, weight: .semibold))
                Text(String(format: "Recoverable +%.2f over current %.2f", problem.bestScore - problem.currentScore, problem.currentScore))
                    .font(.system(size: 10.5))
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Button("Queue Restore") {
                Task { await store.restoreBest(problem: problem.title) }
            }
            .buttonStyle(.borderedProminent)
            .tint(.orange)
            .controlSize(.small)
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.orange.opacity(0.10)))
    }

    private var accountingCard: some View {
        SurfaceCard(padding: 11) {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Text("SCORE ACCOUNTING").font(.system(size: 10, weight: .bold)).foregroundStyle(.secondary)
                    Spacer()
                    if problem.leaderScore > 0 {
                        Text(String(format: "#1 %@: %.2f", problem.leaderNickname, problem.leaderScore))
                            .font(.system(size: 10.5, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }
                }
                HStack(spacing: 12) {
                    miniStat("Public (5)", String(format: "%.2f / 10", problem.publicPts), .blue)
                    miniStat("Hidden (45)", String(format: "%.2f / 90", problem.hiddenPts), .purple)
                    miniStat("Hidden r̄_h", String(format: "%.1f%%", problem.impliedHiddenRatio * 100), .teal)
                }
            }
        }
    }

    private var publicRatioCard: some View {
        SurfaceCard(padding: 11) {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Text("PUBLIC CASES · OURS vs BESTCASE (tbest)").font(.system(size: 10, weight: .bold)).foregroundStyle(.secondary)
                    Spacer()
                    Text("r = tbest / ours").font(.system(size: 10, design: .monospaced)).foregroundStyle(.secondary)
                }
                if problem.publicTbestUs.isEmpty {
                    Text("No public timings recorded.").font(.system(size: 11)).foregroundStyle(.secondary)
                } else {
                    ForEach(0..<problem.publicTbestUs.count, id: \.self) { i in
                        let tb = problem.publicTbestUs[i]
                        let ours = i < problem.publicTimesUs.count ? problem.publicTimesUs[i] : nil
                        let r = (ours != nil && ours! > 0) ? min(1.0, tb / ours!) : 0.0
                        ratioBar(index: i + 1, tbest: tb, ours: ours, ratio: r)
                    }
                }
            }
        }
    }

    private var all50CasesCard: some View {
        SurfaceCard(padding: 11) {
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Text("ALL 50 TESTCASES · FOLDABLE BREAKDOWN").font(.system(size: 10, weight: .bold)).foregroundStyle(.secondary)
                    Spacer()
                    Button(expandedGroups.isEmpty ? "Expand All" : "Fold All") {
                        expandedGroups = expandedGroups.isEmpty ? Set(0..<5) : []
                    }
                    .buttonStyle(.link)
                    .font(.system(size: 10.5))
                }
                ForEach(0..<5, id: \.self) { g in
                    let s = g * 10
                    let e = min(problem.testcases.count, s + 10)
                    if s < e {
                        let slice = Array(problem.testcases[s..<e])
                        let pubCnt = slice.filter { !$0.isHidden }.count
                        DisclosureGroup(
                            isExpanded: Binding(
                                get: { expandedGroups.contains(g) },
                                set: { v in if v { expandedGroups.insert(g) } else { expandedGroups.remove(g) } }
                            )
                        ) {
                            VStack(spacing: 3) {
                                ForEach(slice) { tc in
                                    HStack(spacing: 8) {
                                        Text(String(format: "#%02d", tc.index))
                                            .font(.system(size: 10.5, weight: .semibold, design: .monospaced))
                                            .frame(width: 28, alignment: .leading)
                                        SemanticPill(text: tc.isHidden ? "Hidden" : "Public", compact: true)
                                        Spacer()
                                        Text(tc.tbestUs.map { String(format: "tbest %.2f µs", $0) } ?? String(format: "Masked · r̄_h %.2f", problem.impliedHiddenRatio))
                                            .font(.system(size: 10, design: .monospaced))
                                            .foregroundStyle(tc.tbestUs != nil ? .primary : .secondary)
                                        if let o = tc.oursUs {
                                            Text(String(format: "ours %.2f µs", o))
                                                .font(.system(size: 10.5, weight: .medium, design: .monospaced))
                                                .foregroundStyle(Color.accentColor)
                                                .frame(width: 82, alignment: .trailing)
                                        }
                                    }
                                    .padding(.vertical, 2)
                                }
                            }
                            .padding(.top, 4)
                        } label: {
                            HStack {
                                Text(String(format: "Cases #%02d – #%02d", s + 1, e))
                                    .font(.system(size: 11.5, weight: .medium, design: .monospaced))
                                Spacer()
                                Text(pubCnt > 0 ? "\(pubCnt) Public · \(slice.count - pubCnt) Hidden" : "10 Hidden Cases")
                                    .font(.system(size: 10.5))
                                    .foregroundStyle(.secondary)
                            }
                        }
                        .padding(.horizontal, 8)
                        .padding(.vertical, 4)
                        .background(RoundedRectangle(cornerRadius: 6).fill(Color.primary.opacity(0.03)))
                    }
                }
            }
        }
    }

    private var timelineCard: some View {
        SurfaceCard(padding: 11) {
            VStack(alignment: .leading, spacing: 8) {
                Text("SUBMISSION TIMELINE (\(problem.history.count))")
                    .font(.system(size: 10, weight: .bold))
                    .foregroundStyle(.secondary)
                ForEach(problem.history.prefix(15)) { h in
                    HStack(alignment: .top, spacing: 8) {
                        VStack(alignment: .leading, spacing: 2) {
                            HStack(spacing: 6) {
                                Text(String(h.sha.prefix(7))).font(.system(size: 11, weight: .semibold, design: .monospaced))
                                SemanticPill(text: h.status, compact: true)
                                Text(Formatters.shortTimestamp(h.at)).font(.system(size: 10, design: .monospaced)).foregroundStyle(.secondary)
                            }
                            if !h.note.isEmpty {
                                Text(h.note).font(.system(size: 10.5)).foregroundStyle(.secondary).lineLimit(2)
                            }
                        }
                        Spacer()
                        VStack(alignment: .trailing, spacing: 2) {
                            if let sc = h.score {
                                Text(String(format: "%.2f", sc)).font(.system(size: 12, weight: .bold, design: .monospaced))
                            }
                            if let d = h.delta {
                                Text(String(format: "%+.2f", d))
                                    .font(.system(size: 10, weight: .semibold, design: .monospaced))
                                    .foregroundStyle(d >= 0 ? .green : .orange)
                            }
                        }
                    }
                    .padding(.vertical, 3)
                    Divider()
                }
            }
        }
    }

    private func miniStat(_ label: String, _ val: String, _ color: Color) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label).font(.system(size: 10)).foregroundStyle(.secondary)
            Text(val).font(.system(size: 12.5, weight: .bold, design: .monospaced)).foregroundStyle(color)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func ratioBar(index: Int, tbest: Double, ours: Double?, ratio: Double) -> some View {
        VStack(spacing: 4) {
            HStack(spacing: 6) {
                Text(String(format: "Case #%02d", index)).font(.system(size: 11, weight: .semibold, design: .monospaced))
                Spacer()
                Text(String(format: "tbest %.2f µs", tbest)).font(.system(size: 10.5, design: .monospaced)).foregroundStyle(.secondary)
                Text("vs").font(.system(size: 10)).foregroundStyle(.secondary)
                Text(ours.map { String(format: "ours %.2f µs", $0) } ?? "ours —").font(.system(size: 10.5, weight: .semibold, design: .monospaced))
                Text(String(format: "%.0f%%", ratio * 100))
                    .font(.system(size: 10.5, weight: .bold, design: .monospaced))
                    .foregroundStyle(ratio >= 0.7 ? .green : .accentColor)
                    .frame(width: 38, alignment: .trailing)
            }
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.primary.opacity(0.07))
                    Capsule().fill((ratio >= 0.7 ? Color.green : Color.accentColor).gradient).frame(width: max(4, geo.size.width * ratio))
                }
            }
            .frame(height: 5.5)
        }
    }
}
