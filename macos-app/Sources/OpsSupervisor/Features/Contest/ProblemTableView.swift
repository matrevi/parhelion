import SwiftUI

enum ProblemFilter: String, CaseIterable, Identifiable {
    case all = "全部"
    case highWeight = "^^^ 高难题"
    case restoreGap = "可恢复差值"

    var id: String { rawValue }
}

struct ProblemTableView: View {
    @EnvironmentObject var store: ContestStore
    let problems: [ProblemItem]

    @UIState private var filter: ProblemFilter = .all
    @UIState private var sortByROI: Bool = false

    private var displayed: [ProblemItem] {
        var list = problems.filter { p in
            switch filter {
            case .all: return true
            case .highWeight: return p.difficulty == 3
            case .restoreGap: return p.bestScore - p.currentScore >= 1.0 || p.currentScore == 0
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
                    ForEach(ProblemFilter.allCases) { f in Text(f.rawValue).tag(f) }
                }
                .pickerStyle(.segmented)
                .frame(maxWidth: 290)

                Spacer()

                Toggle(isOn: $sortByROI) {
                    Label("Sort by WSPT ROI", systemImage: "arrow.up.right.circle")
                        .font(.system(size: 11, weight: .medium))
                }
                .toggleStyle(.button)
                .controlSize(.small)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)

            Divider()

            HStack(spacing: 8) {
                Text("级别").frame(width: 34, alignment: .leading)
                Text("Operator").frame(maxWidth: .infinity, alignment: .leading)
                Text("Status").frame(width: 88, alignment: .center)
                Text("Current").frame(width: 60, alignment: .trailing)
                Text("Best").frame(width: 60, alignment: .trailing)
                Text("Rank").frame(width: 52, alignment: .trailing)
                Text("Pub µs").frame(width: 58, alignment: .trailing)
                Text("Priority").frame(width: 120, alignment: .trailing)
            }
            .font(.system(size: 10.5, weight: .semibold))
            .foregroundStyle(.secondary)
            .padding(.horizontal, 14)
            .padding(.vertical, 6)
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

        return HStack(spacing: 8) {
            DifficultyBadge(difficulty: p.difficulty)
                .frame(width: 34, alignment: .leading)

            VStack(alignment: .leading, spacing: 2) {
                Text(p.title)
                    .font(.system(size: 12.5, weight: .semibold))
                    .lineLimit(1)
                HStack(spacing: 5) {
                    if !p.headSha.isEmpty {
                        Text(String(p.headSha.prefix(7)))
                            .font(.system(size: 10, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }
                    if let age = p.headAgeMin {
                        Text("· \(age < 60 ? "\(age)m" : "\(age/60)h") ago")
                            .font(.system(size: 10))
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            SemanticPill(text: p.status, compact: true)
                .frame(width: 88, alignment: .center)

            Text(String(format: "%.2f", p.currentScore))
                .font(.system(size: 12.5, weight: .bold, design: .monospaced))
                .foregroundStyle(p.currentScore == 0 ? .red : .primary)
                .frame(width: 60, alignment: .trailing)

            VStack(alignment: .trailing, spacing: 1) {
                Text(String(format: "%.2f", p.bestScore))
                    .font(.system(size: 11.5, weight: .medium, design: .monospaced))
                if gap >= 0.5 {
                    Text(String(format: "-%.1f", gap))
                        .font(.system(size: 9.5, weight: .semibold, design: .monospaced))
                        .foregroundStyle(.orange)
                }
            }
            .frame(width: 60, alignment: .trailing)

            Text(p.rank.map { "#\($0)" } ?? "—")
                .font(.system(size: 11.5, design: .monospaced))
                .foregroundStyle(.secondary)
                .frame(width: 52, alignment: .trailing)

            Text(p.publicMeanUs.map { String(format: "%.2f", $0) } ?? "—")
                .font(.system(size: 11.5, design: .monospaced))
                .foregroundStyle(.secondary)
                .frame(width: 58, alignment: .trailing)

            SemanticPill(text: p.wsptTag, compact: true)
                .frame(width: 120, alignment: .trailing)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
        .background(
            RoundedRectangle(cornerRadius: 7, style: .continuous)
                .fill(selected ? Color.accentColor.opacity(0.13) : Color.clear)
                .padding(.horizontal, 6)
        )
    }
}
