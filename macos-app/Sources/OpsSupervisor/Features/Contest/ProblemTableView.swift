import SwiftUI

enum ProblemFilter: String, CaseIterable, Identifiable {
    case all
    case highWeight
    case restoreGap

    var id: String { rawValue }

    func label(_ lang: AppLanguage) -> String {
        switch self {
        case .all: return lang.tr("全部", "All")
        case .highWeight: return lang.tr("3级高难", "Tier-3")
        case .restoreGap: return lang.tr("可恢复差值", "Recoverable")
        }
    }
}

struct ProblemTableView: View {
    @EnvironmentObject var store: ContestStore
    let problems: [ProblemItem]

    @UIState private var filter: ProblemFilter = .all
    @UIState private var sortByROI: Bool = false

    private var lang: AppLanguage { store.lang }

    private var displayed: [ProblemItem] {
        var list = problems.filter { p in
            switch filter {
            case .all: return true
            case .highWeight: return p.difficulty == 3
            case .restoreGap: return p.bestScore - p.currentScore >= 2.5 || p.currentScore == 0
            }
        }
        if sortByROI {
            list.sort { $0.wsptPriority > $1.wsptPriority }
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

            HStack(spacing: 6) {
                Text(lang.tr("级别", "Tier")).frame(width: 36, alignment: .leading)
                Text(lang.tr("算子", "Operator")).frame(minWidth: 142, maxWidth: .infinity, alignment: .leading)
                Text(lang.tr("状态", "Status")).frame(width: 92, alignment: .center)
                Text(lang.tr("当前分", "Current")).frame(width: 66, alignment: .trailing)
                Text(lang.tr("同代最优", "Equiv Best")).frame(width: 74, alignment: .trailing)
                Text(lang.tr("排名", "Rank")).frame(width: 58, alignment: .trailing)
                Text(lang.tr("调度建议", "Priority")).frame(width: 116, alignment: .trailing)
            }
            .font(.system(size: 11.5, weight: .semibold))
            .foregroundStyle(.secondary)
            .padding(.horizontal, 16)
            .padding(.vertical, 8)
            .background(Color.primary.opacity(0.025))

            Divider()

            ScrollView {
                LazyVStack(spacing: 1) {
                    ForEach(displayed) { p in
                        row(p)
                            .contentShape(Rectangle())
                            .onTapGesture { store.selectedProblemTitle = p.title }
                    }
                }
                .padding(.vertical, 4)
            }
        }
    }

    private func row(_ p: ProblemItem) -> some View {
        let selected = p.title == store.selectedProblemTitle
        let gap = p.bestScore - p.currentScore

        return HStack(spacing: 6) {
            DifficultyBadge(difficulty: p.difficulty)
                .frame(width: 36, alignment: .leading)

            VStack(alignment: .leading, spacing: 2) {
                Text(p.title)
                    .font(.system(size: 13.5, weight: .semibold))
                    .lineLimit(1)
                HStack(spacing: 5) {
                    if !p.headSha.isEmpty {
                        Text(String(p.headSha.prefix(7)))
                            .font(.system(size: 11, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }
                    if let age = p.headAgeMin {
                        let unit = age < 60 ? "\(age)m" : "\(age / 60)h"
                        Text(lang.tr("· \(unit)前", "· \(unit) ago"))
                            .font(.system(size: 11))
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .frame(minWidth: 142, maxWidth: .infinity, alignment: .leading)

            SemanticPill(text: lang.evalStatus(p.status), compact: true)
                .frame(width: 92, alignment: .center)

            Text(String(format: "%.2f", p.currentScore))
                .font(.system(size: 13.5, weight: .bold, design: .monospaced))
                .foregroundStyle(p.currentScore == 0 ? .red : .primary)
                .frame(width: 66, alignment: .trailing)

            VStack(alignment: .trailing, spacing: 1) {
                Text(String(format: "%.2f", p.bestScore))
                    .font(.system(size: 12.5, weight: .medium, design: .monospaced))
                if gap >= 2.5 {
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
                .frame(width: 116, alignment: .trailing)
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
