import SwiftUI

struct TriadConfidenceBadge: View {
    let entry: TriadKBEntry

    private var tint: Color {
        if entry.isAntiPattern { return Color(red: 0.84, green: 0.23, blue: 0.26) }
        if entry.confidence >= 0.75 { return Color(red: 0.10, green: 0.72, blue: 0.43) }
        if entry.confidence >= 0.55 { return Color(red: 0.20, green: 0.56, blue: 0.96) }
        return .orange
    }

    var body: some View {
        HStack(spacing: 4) {
            Text("\(entry.confidencePercent)%")
                .font(.system(size: 10.5, weight: .bold, design: .monospaced))
            Text("W\(entry.wins)/F\(entry.fails)")
                .font(.system(size: 9.5, weight: .medium, design: .monospaced))
                .opacity(0.85)
        }
        .foregroundStyle(tint)
        .padding(.horizontal, 7)
        .padding(.vertical, 2.5)
        .background(Capsule(style: .continuous).fill(tint.opacity(0.14)))
    }
}

struct TriadKBListPane: View {
    @EnvironmentObject var store: ContestStore
    let entries: [TriadKBEntry]
    @Binding var selectedEntryId: String?
    let emptyHint: String

    var body: some View {
        ScrollView {
            LazyVStack(spacing: 8) {
                if entries.isEmpty {
                    VStack(spacing: 8) {
                        Image(systemName: "tray")
                            .font(.system(size: 24))
                            .foregroundStyle(.secondary)
                        Text(emptyHint)
                            .font(.system(size: 12))
                            .foregroundStyle(.secondary)
                    }
                    .padding(.vertical, 48)
                } else {
                    ForEach(entries) { item in
                        let isSelected = selectedEntryId == item.id
                            || (selectedEntryId == nil && entries.first?.id == item.id)
                        Button {
                            selectedEntryId = item.id
                        } label: {
                            rowCard(item, selected: isSelected)
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
            .padding(10)
        }
    }

    private func rowCard(_ entry: TriadKBEntry, selected: Bool) -> some View {
        let highlight: Color = entry.isAntiPattern
            ? Color(red: 0.84, green: 0.23, blue: 0.26)
            : (entry.pillar == "snippets"
                ? Color(red: 0.12, green: 0.68, blue: 0.62)
                : (entry.pillar == "hardware" ? Color(red: 0.45, green: 0.34, blue: 0.88) : Color.accentColor))
        return SurfaceCard(highlighted: selected, highlightColor: highlight, padding: 10) {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 6) {
                    SemanticPill(text: entry.portability, compact: true)
                    SemanticPill(text: entry.status, compact: true)
                    Spacer()
                    TriadConfidenceBadge(entry: entry)
                }
                Text(entry.title)
                    .font(.system(size: 12.5, weight: .bold))
                    .lineLimit(2)
                    .multilineTextAlignment(.leading)
                Text(entry.summary)
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                    .multilineTextAlignment(.leading)
                HStack(spacing: 5) {
                    Text(entry.id)
                        .font(.system(size: 9.5, design: .monospaced))
                        .foregroundStyle(.tertiary)
                    Spacer()
                    ForEach(entry.bottlenecks.prefix(2), id: \.self) { bn in
                        Text(bn)
                            .font(.system(size: 9.5, weight: .medium, design: .monospaced))
                            .foregroundStyle(.orange)
                            .padding(.horizontal, 5)
                            .padding(.vertical, 1.5)
                            .background(Capsule().fill(Color.orange.opacity(0.12)))
                    }
                }
            }
        }
    }
}

struct BreakthroughListPane: View {
    @EnvironmentObject var store: ContestStore
    let entries: [BreakthroughEntry]
    let triadById: [String: TriadKBEntry]
    @Binding var selectedId: String?
    let activeId: String?

    private var lang: AppLanguage { store.lang }

    var body: some View {
        ScrollView {
            LazyVStack(spacing: 8) {
                if entries.isEmpty {
                    Text(lang.tr(
                        String(format: "当前阈值 (Δ ≥ %.1f) 下无匹配突破档案", store.breakthroughThreshold),
                        String(format: "No breakthrough entries at Δ ≥ %.1f", store.breakthroughThreshold)
                    ))
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                    .padding(.vertical, 48)
                } else {
                    ForEach(entries) { entry in
                        Button { selectedId = entry.id } label: {
                            rowCard(entry, selected: activeId == entry.id)
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
            .padding(10)
        }
    }

    private func rowCard(_ entry: BreakthroughEntry, selected: Bool) -> some View {
        SurfaceCard(highlighted: selected, highlightColor: Color(red: 0.88, green: 0.46, blue: 0.08), padding: 10) {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 6) {
                    DifficultyBadge(difficulty: entry.difficulty)
                    Text(entry.problem).font(.system(size: 12.5, weight: .bold))
                    Text(entry.shortSha).font(.system(size: 10.5, design: .monospaced)).foregroundStyle(.secondary)
                    Spacer()
                    Text(String(format: "%+.2f", entry.deltaRaw))
                        .font(.system(size: 12, weight: .heavy, design: .monospaced))
                        .foregroundStyle(Color(red: 0.10, green: 0.72, blue: 0.42))
                }
                Text(entry.title).font(.system(size: 11.5, weight: .medium)).lineLimit(2).multilineTextAlignment(.leading)
                HStack(spacing: 6) {
                    SemanticPill(text: lang.categoryName(entry.category), compact: true)
                    if let kb = triadById[entry.id] {
                        TriadConfidenceBadge(entry: kb)
                    }
                    Spacer()
                    Text(Formatters.shortTimestamp(entry.timestamp))
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.tertiary)
                }
            }
        }
    }
}

struct TriadEvolutionLogView: View {
    @EnvironmentObject var store: ContestStore
    @UIState private var pendingRevertEvent: KBEvolutionEvent?

    private var lang: AppLanguage { store.lang }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                Label(
                    lang.tr(
                        "四元知识库进化审计日志 (\(store.triadEvolutionEvents.count) 条)",
                        "4-Pillar KB Evolution Audit Log (\(store.triadEvolutionEvents.count) event(s))"
                    ),
                    systemImage: "clock.arrow.2.circlepath"
                )
                .font(.system(size: 12.5, weight: .bold))

                if !store.triadEvolutionLogPath.isEmpty {
                    Text(store.triadEvolutionLogPath)
                        .font(.system(size: 10.5, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
                Spacer()
                Button {
                    Task { await store.reloadTriadHistory() }
                } label: {
                    Label(lang.tr("刷新日志", "Refresh"), systemImage: "arrow.clockwise")
                        .font(.system(size: 11))
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 9)
            .background(Color.primary.opacity(0.02))

            Divider()

            if store.triadEvolutionEvents.isEmpty {
                VStack(spacing: 10) {
                    Image(systemName: "waveform.path.ecg")
                        .font(.system(size: 28))
                        .foregroundStyle(.secondary)
                    Text(lang.tr("尚未记录任何知识库进化事件", "No KB evolution events recorded yet."))
                        .font(.system(size: 13, weight: .medium))
                        .foregroundStyle(.secondary)
                    Text(lang.tr("对突破/积木/硬件/社区卡片提交 Win 或 Fail 实证后，系统将在此记录自动晋升/降级与回滚链。",
                                 "Submit Win/Fail evidence on any entry to track auto-promotions, demotions, and rollbacks here."))
                        .font(.system(size: 11.5))
                        .foregroundStyle(.tertiary)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ScrollView {
                    LazyVStack(spacing: 8) {
                        ForEach(store.triadEvolutionEvents) { ev in
                            eventCard(ev)
                        }
                    }
                    .padding(14)
                }
            }
        }
        .alert(
            lang.tr("回滚知识库变更？", "Rollback KB Evolution Event?"),
            isPresented: Binding(
                get: { pendingRevertEvent != nil },
                set: { if !$0 { pendingRevertEvent = nil } }
            )
        ) {
            Button(lang.tr("取消", "Cancel"), role: .cancel) { pendingRevertEvent = nil }
            Button(lang.tr("确认回滚", "Revert"), role: .destructive) {
                if let target = pendingRevertEvent {
                    pendingRevertEvent = nil
                    Task { await store.rollbackTriadEvent(eventId: target.eventId) }
                }
            }
        } message: {
            if let target = pendingRevertEvent {
                Text("\(target.eventId) · \(target.entryId)")
            }
        }
    }

    private func eventCard(_ ev: KBEvolutionEvent) -> some View {
        let isReverted = ev.reverted == true
        return SurfaceCard(padding: 11) {
            HStack(alignment: .top, spacing: 10) {
                VStack(alignment: .leading, spacing: 5) {
                    HStack(spacing: 6) {
                        SemanticPill(text: ev.action.uppercased(), compact: true)
                        SemanticPill(text: ev.pillar, compact: true)
                        if let trans = ev.transition, !trans.isEmpty, trans != "unchanged" {
                            SemanticPill(text: trans, compact: true)
                        }
                        if isReverted {
                            SemanticPill(text: "REVERTED", compact: true)
                        }
                        Text(ev.entryId)
                            .font(.system(size: 12, weight: .bold, design: .monospaced))
                        Spacer()
                        Text(Formatters.shortTimestamp(ev.timestamp))
                            .font(.system(size: 10.5, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }

                    if let evid = ev.evidence {
                        HStack(spacing: 8) {
                            Text("\(evid.problem) (\(String(evid.sha.prefix(7))))")
                                .font(.system(size: 11, weight: .semibold, design: .monospaced))
                            SemanticPill(text: evid.verdict.uppercased(), compact: true)
                            if let dr = evid.deltaRaw {
                                Text(String(format: "%+.2f pts", dr))
                                    .font(.system(size: 10.5, weight: .bold, design: .monospaced))
                                    .foregroundStyle(dr >= 0 ? .green : .orange)
                            }
                            if let note = evid.note, !note.isEmpty {
                                Text(note)
                                    .font(.system(size: 11))
                                    .foregroundStyle(.secondary)
                                    .lineLimit(1)
                            }
                        }
                    }

                    HStack(spacing: 8) {
                        Text(ev.eventId)
                            .font(.system(size: 10, design: .monospaced))
                            .foregroundStyle(.tertiary)
                        if let cs = ev.createdSnippetId, !cs.isEmpty {
                            Text("promoted → \(cs)")
                                .font(.system(size: 10, weight: .semibold, design: .monospaced))
                                .foregroundStyle(.teal)
                        }
                    }
                }

                if ev.action != "revert" && !isReverted {
                    Button(lang.tr("回滚", "Revert")) {
                        pendingRevertEvent = ev
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                }
            }
        }
    }
}
