import SwiftUI

enum ProblemFilter: String, CaseIterable, Identifiable {
    case all
    case highWeight
    case restoreGap

    var id: String { rawValue }

    func label(_ lang: AppLanguage) -> String {
        switch self {
        case .all: return lang.tr("全部", "All")
        case .highWeight: return lang.tr("3级及以上", "Tier 3+")
        case .restoreGap: return lang.tr("可恢复差值", "Recoverable")
        }
    }
}

struct ProblemTableColumns {
    let name: CGFloat
    let verdict: CGFloat
    let priority: CGFloat
    let chat: CGFloat
    // Tier, current, board, equivalent best and rank, plus the row's horizontal insets.
    private var fields: CGFloat { name + verdict + priority + chat + 36 + 66 + 56 + 74 + 58 + 32 }
    var idealWidth: CGFloat { fields + 8 * 8 }
    var minimumWidth: CGFloat { fields + 8 * 3 }
    func spacing(for width: CGFloat) -> CGFloat { min(8, max(3, (width - fields) / 8)) }

    init(_ problems: [ProblemItem], _ lang: AppLanguage) {
        func width(_ label: String, _ size: CGFloat) -> CGFloat {
            ceil((label as NSString).size(withAttributes: [.font: NSFont.systemFont(ofSize: size, weight: .semibold)]).width)
        }
        name = max(104, problems.map { width($0.title, 13.5) + 2 }.max() ?? 0)
        verdict = max(width(lang.tr("最近判题", "Last verdict"), 11.5),
                      problems.map { width($0.latestStatus.map { lang.evalStatus($0) } ?? "—", 10) + 17 }.max() ?? 0)
        priority = max(width(lang.tr("调度建议", "Priority"), 11.5),
                       problems.map { width(lang.wsptTag($0.wsptTag), 10) + 17 }.max() ?? 0)
        chat = width(lang.tr("新开", "Launch"), 10) + 34
    }
}

struct ProblemTableView: View {
    @EnvironmentObject var store: ContestStore
    let problems: [ProblemItem]
    let columns: ProblemTableColumns

    @UIState private var filter: ProblemFilter = .all
    @UIState private var sortByROI: Bool = false

    private var lang: AppLanguage { store.lang }

    private var displayed: [ProblemItem] {
        var list = problems.filter { p in
            switch filter {
            case .all: return true
            case .highWeight: return p.difficulty >= 3
            case .restoreGap: return p.boardScore != nil && p.bestScore - p.currentScore >= 2.5
            }
        }
        if sortByROI {
            list.sort {
                if $0.wsptPriority != $1.wsptPriority { return $0.wsptPriority > $1.wsptPriority }
                return $0.title < $1.title
            }
        }
        return list
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Picker("", selection: $filter) {
                    ForEach(ProblemFilter.allCases) { f in Text(f.label(lang)).tag(f) }
                }
                .pickerStyle(.segmented)
                .frame(maxWidth: 295)

                Spacer()

                Button {
                    Task { await store.launchContestSubagentSession(mode: "gui") }
                } label: {
                    Label(lang.tr("新开对话", "New Contest Chat"), systemImage: "plus.bubble.fill")
                        .font(.system(size: 11, weight: .semibold))
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
                .disabled(store.isLaunchingSession || store.selectedWorkspace.isEmpty)
                .help(lang.tr(
                    "为当前比赛新开主控对话，协调各题并行优化",
                    "Start an orchestrator chat for the selected contest"
                ))

                Toggle(isOn: $sortByROI) {
                    Label(lang.tr("按 WSPT 收益排序", "Sort by WSPT ROI"), systemImage: "arrow.up.right.circle")
                        .font(.system(size: 11, weight: .medium))
                }
                .toggleStyle(.button)
                .controlSize(.small)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)

            Divider()

            GeometryReader { geometry in
                let width = max(geometry.size.width, columns.minimumWidth)
                let spacing = columns.spacing(for: width)
                ScrollView(.horizontal) {
                    VStack(spacing: 0) {
                        tableHeader(spacing: spacing)
                        Divider()
                        ScrollView {
                            LazyVStack(spacing: 1) {
                                ForEach(displayed) { p in
                                    row(p, spacing: spacing)
                                        .contentShape(Rectangle())
                                        .onTapGesture { store.selectedProblemTitle = p.title }
                                }
                            }
                            .padding(.vertical, 4)
                        }
                    }
                    .frame(width: width, height: geometry.size.height)
                }
            }
        }
    }

    private func tableHeader(spacing: CGFloat) -> some View {
            HStack(spacing: spacing) {
                Text(lang.tr("级别", "Tier")).frame(width: 36, alignment: .leading)
                Text(lang.tr("算子", "Operator")).frame(minWidth: columns.name, maxWidth: .infinity, alignment: .leading)
                Text(lang.tr("最近判题", "Last verdict")).frame(width: columns.verdict, alignment: .center)
                Text(lang.tr("当前分", "Current")).frame(width: 66, alignment: .trailing)
                Text(lang.tr("在榜分", "Board")).frame(width: 56, alignment: .trailing)
                Text(lang.tr("同代最优", "Equiv Best")).frame(width: 74, alignment: .trailing)
                Text(lang.tr("排名", "Rank")).frame(width: 58, alignment: .trailing)
                Text(lang.tr("调度建议", "Priority")).frame(width: columns.priority, alignment: .trailing)
                Text(lang.tr("对话", "Chat")).frame(width: columns.chat, alignment: .trailing)
            }
            .font(.system(size: 11.5, weight: .semibold))
            .foregroundStyle(.secondary)
            .padding(.horizontal, 16)
            .padding(.vertical, 8)
            .background(Color.primary.opacity(0.025))

    }

    private func row(_ p: ProblemItem, spacing: CGFloat) -> some View {
        let selected = p.title == store.selectedProblemTitle
        let gap = p.bestScore - p.currentScore

        return HStack(spacing: spacing) {
            DifficultyBadge(difficulty: p.difficulty)
                .frame(width: 36, alignment: .leading)

            VStack(alignment: .leading, spacing: 2) {
                Text(p.title)
                    .font(.system(size: 13.5, weight: .semibold))
                    .lineLimit(1)
                    .help(p.title)
                HStack(spacing: 5) {
                    if let sha = p.latestSha, !sha.isEmpty {
                        Text(String(sha.prefix(7)))
                            .font(.system(size: 11, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }
                    if let at = p.latestAt {
                        Text("· " + Formatters.shortTimestamp(at))
                            .font(.system(size: 11))
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                }
            }
            .frame(minWidth: columns.name, maxWidth: .infinity, alignment: .leading)

            SemanticPill(text: p.latestStatus.map { lang.evalStatus($0) } ?? "—", compact: true)
                .frame(width: columns.verdict, alignment: .center)

            Text(p.latestScoreText(lang))
                .font(.system(size: 13.5, weight: .bold, design: .monospaced))
                .foregroundStyle(p.latestScore == 0 ? .red : .primary)
                .frame(width: 66, alignment: .trailing)
            Text(p.boardScoreText(lang))
                .font(.system(size: 12, design: .monospaced))
                .frame(width: 56, alignment: .trailing)

            VStack(alignment: .trailing, spacing: 1) {
                Text(String(format: "%.2f", p.bestScore))
                    .font(.system(size: 12.5, weight: .medium, design: .monospaced))
                if p.boardScore != nil && gap >= 2.5 {
                    Text(String(format: "-%.1f", gap))
                        .font(.system(size: 10.5, weight: .semibold, design: .monospaced))
                        .foregroundStyle(.orange)
                }
            }
            .frame(width: 74, alignment: .trailing)
            .help(p.rawBestScore.map {
                lang.tr(
                    String(format: "同代折算最优 %.2f（原始峰值 %.2f · tbest γ=%.3f）", p.bestScore, $0, p.tbestDecayGamma ?? 1.0),
                    String(format: "Contemporary Equiv Best %.2f (Raw Peak %.2f · tbest γ=%.3f)", p.bestScore, $0, p.tbestDecayGamma ?? 1.0)
                )
            } ?? "")

            Text(p.rank.map { "#\($0)" } ?? "—")
                .font(.system(size: 12.5, design: .monospaced))
                .foregroundStyle(.secondary)
                .frame(width: 58, alignment: .trailing)

            SemanticPill(text: lang.wsptTag(p.wsptTag), compact: true)
                .frame(width: columns.priority, alignment: .trailing)

            Button {
                Task { await store.launchOperatorSession(problem: p.title, mode: "gui") }
            } label: {
                Label(lang.tr("新开", "Launch"), systemImage: "plus.bubble")
                    .font(.system(size: 10, weight: .semibold))
            }
            .buttonStyle(.bordered)
            .controlSize(.mini)
            .disabled(store.isLaunchingSession)
            .help(lang.tr("新开调优对话 (opsb driver launch \(p.title) --mode gui)", "Launch tuning session for \(p.title)"))
            .frame(width: columns.chat, alignment: .trailing)
        }
        .contextMenu {
            Button {
                Task { await store.launchOperatorSession(problem: p.title, mode: "gui") }
            } label: {
                Label(lang.tr("新开 GUI 调优对话", "Launch GUI Session"), systemImage: "plus.bubble.fill")
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 9)
        .background(
            RoundedRectangle(cornerRadius: 7, style: .continuous)
                .fill(selected ? Color.accentColor.opacity(0.13) : Color.clear)
                .padding(.horizontal, 6)
        )
    }
}
