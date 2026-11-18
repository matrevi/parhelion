import SwiftUI

enum QueueFilterTab: String, CaseIterable, Identifiable {
    case all = "All"
    case actionable = "Actionable"
    case completed = "Passed"
    case held = "Held / Rejected"

    var id: String { rawValue }
}

struct SubmissionQueueView: View {
    @EnvironmentObject var store: ContestStore
    let items: [SubmissionQueueItem]
    @UIState private var filter: QueueFilterTab = .all

    private var filtered: [SubmissionQueueItem] {
        items.filter { item in
            switch filter {
            case .all: return true
            case .actionable: return item.kind != "completed"
            case .completed: return item.kind == "completed" && item.badge.contains("PASS")
            case .held: return item.badge.contains("HOLD") || item.badge.contains("REJECTED") || item.badge.contains("CIRCUIT")
            }
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Label("Submission Queue & 4-Stage Gate", systemImage: "tray.full.fill")
                    .font(.system(size: 12.5, weight: .bold))
                Spacer()
                Picker("", selection: $filter) {
                    ForEach(QueueFilterTab.allCases) { f in Text(f.rawValue).tag(f) }
                }
                .pickerStyle(.segmented)
                .controlSize(.small)
                .frame(width: 310)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 9)

            Divider()

            ScrollView {
                LazyVStack(spacing: 8) {
                    ForEach(filtered) { item in
                        card(item)
                    }
                }
                .padding(10)
            }
        }
    }

    private func card(_ item: SubmissionQueueItem) -> some View {
        let isActionable = item.kind != "completed"
        let isCrit = item.badge == "CRITICAL RESTORE"

        return SurfaceCard(
            highlighted: isActionable,
            highlightColor: isCrit ? .red : .accentColor,
            padding: 10
        ) {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 7) {
                    DifficultyBadge(difficulty: item.difficulty)
                    Text(item.title).font(.system(size: 13, weight: .bold))
                    Text(item.shortSha).font(.system(size: 11, design: .monospaced)).foregroundStyle(.secondary)
                    Text("· \(Formatters.shortTimestamp(item.finishedAt ?? item.queuedAt))")
                        .font(.system(size: 10.5, design: .monospaced))
                        .foregroundStyle(.secondary)
                    Spacer()
                    SemanticPill(text: item.badge)
                }

                HStack(spacing: 10) {
                    scoreCell("目前分数 (Current)", String(format: "%.2f", item.currentScore), item.currentScore == 0 ? .red : .primary)
                    scoreCell("预计分数 (Estimated)", item.estimatedScore.map { String(format: "~%.2f", $0) } ?? "—", .blue)
                    HStack(spacing: 4) {
                        scoreCell(
                            "实际得分 (Actual)",
                            item.actualScore.map { String(format: "%.2f", $0) } ?? (item.rawStatus == "skipped" ? "Skipped" : "Pending"),
                            item.actualScore.map { $0 > 0 ? Color.primary : Color.red } ?? .secondary
                        )
                        if let d = item.deltaScore {
                            Text(String(format: "%+.2f", d))
                                .font(.system(size: 11, weight: .bold, design: .monospaced))
                                .foregroundStyle(d >= 0 ? .green : .orange)
                        }
                    }
                }
                .padding(.vertical, 5)
                .padding(.horizontal, 8)
                .background(RoundedRectangle(cornerRadius: 6).fill(Color.primary.opacity(0.03)))

                if !item.note.isEmpty {
                    Text(item.note).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(2)
                }

                HStack(spacing: 5) {
                    GateCheckPill(label: "G1 Safety", ok: item.gates.safetyOk)
                    GateCheckPill(label: "G2 Noise", ok: item.gates.noiseOk)
                    GateCheckPill(label: "G3 Circuit", ok: item.gates.circuitOk)
                    GateCheckPill(label: "G4 Quota", ok: item.gates.quotaOk)

                    Spacer()

                    if item.canRestore {
                        Button("Restore Best SHA") {
                            Task { await store.restoreBest(problem: item.title) }
                        }
                        .buttonStyle(.borderedProminent)
                        .tint(.red)
                        .controlSize(.mini)
                    } else if item.canApprove {
                        Button("Approve / Force Submit") {
                            Task { await store.approveCandidate(problem: item.title, sha: item.sha, note: item.note) }
                        }
                        .buttonStyle(.borderedProminent)
                        .controlSize(.mini)
                    }

                    if item.canSkip {
                        Button("Skip") {
                            Task { await store.skipCandidate(problem: item.title, sha: item.sha) }
                        }
                        .buttonStyle(.bordered)
                        .controlSize(.mini)
                    }
                }
            }
        }
    }

    private func scoreCell(_ label: String, _ val: String, _ color: Color) -> some View {
        VStack(alignment: .leading, spacing: 1.5) {
            Text(label).font(.system(size: 9.5)).foregroundStyle(.secondary)
            Text(val).font(.system(size: 12.5, weight: .bold, design: .monospaced)).foregroundStyle(color)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}
