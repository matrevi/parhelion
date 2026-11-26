import SwiftUI

enum QueueViewTab: String, CaseIterable, Identifiable {
    case waiting
    case breakthrough
    case history
    case held

    var id: String { rawValue }
}

struct SubmissionQueueView: View {
    @EnvironmentObject var store: ContestStore
    let items: [SubmissionQueueItem]

    @StateObject private var clock = WallClockTicker()
    @UIState private var selectedTab: QueueViewTab = .waiting
    @UIState private var btSortByTime: Bool = false
    @UIState private var inspectedExperience: BreakthroughEntry? = nil

    private var lang: AppLanguage { store.lang }
    private var pipeline: LivePipelineState? { store.snapshot?.scheduler.pipeline }
    private var btThreshold: Double { store.breakthroughThreshold }

    private func isHeldItem(_ item: SubmissionQueueItem) -> Bool {
        let u = item.badge.uppercased()
        if u.contains("CRITICAL") { return false }
        return u.contains("HOLD")
            || u.contains("CIRCUIT")
            || u.contains("ESCAPE")
            || u.contains("REJECTED")
            || !item.gates.allPass
    }

    private var coldInflightItems: [SubmissionQueueItem] {
        items.filter { $0.kind == "inflight" }
    }

    private var waitingItems: [SubmissionQueueItem] {
        items.filter {
            $0.kind != "completed" && $0.kind != "inflight" && !isHeldItem($0)
        }
    }

    private var heldItems: [SubmissionQueueItem] {
        items.filter {
            $0.kind != "completed" && $0.kind != "inflight" && isHeldItem($0)
        }
    }

    private var completedItems: [SubmissionQueueItem] {
        items.filter { $0.kind == "completed" }
    }

    private var breakthroughItems: [SubmissionQueueItem] {
        items
            .filter { $0.kind == "completed" && $0.rawStatus == "Pass" && ($0.breakthroughDelta ?? -999.0) >= btThreshold }
            .sorted { a, b in
                if btSortByTime {
                    let ta = a.finishedAt ?? a.queuedAt
                    let tb = b.finishedAt ?? b.queuedAt
                    if ta != tb { return ta > tb }
                    let da = a.breakthroughDelta ?? 0
                    let db = b.breakthroughDelta ?? 0
                    if da != db { return da > db }
                    return a.id < b.id
                }
                let aPend = a.kind != "completed"
                let bPend = b.kind != "completed"
                if aPend != bPend { return aPend && !bPend }
                let da = a.breakthroughDelta ?? 0
                let db = b.breakthroughDelta ?? 0
                if da != db { return da > db }
                return a.id < b.id
            }
    }

    private func matchedExperience(for item: SubmissionQueueItem) -> BreakthroughEntry? {
        if let eid = item.experienceId, let e = store.breakthroughEntries.first(where: { $0.id == eid }) {
            return e
        }
        return store.breakthroughEntries.first { $0.problem == item.title && $0.shortSha == item.shortSha }
    }

    var body: some View {
        VStack(spacing: 0) {
            SubmissionQueueHeaderTabs(
                selectedTab: $selectedTab,
                waitingItems: waitingItems,
                heldItems: heldItems,
                breakthroughCount: breakthroughItems.count,
                completedCount: completedItems.count,
                fallbackPipeline: pipeline
            )
            Divider()
            if selectedTab == .breakthrough {
                breakthroughThresholdBar
                Divider()
            }

            ScrollView {
                switch selectedTab {
                case .waiting:
                    // VStack (not LazyVStack) lets matchedGeometryEffect glide candidate → active.
                    WaitingQueueContentView(
                        waitingItems: waitingItems,
                        heldItems: heldItems,
                        coldInflightItems: coldInflightItems,
                        fallbackPipeline: pipeline,
                        clock: clock,
                        emptyText: lang.tr("当前没有待提交的候选版本", "No pending candidates in queue"),
                        onOpenHeldTab: { selectedTab = .held }
                    )
                    .padding(10)
                case .held:
                    HeldQueueContentView(
                        heldItems: heldItems,
                        coldInflightItems: coldInflightItems,
                        fallbackPipeline: pipeline,
                        clock: clock,
                        emptyText: lang.tr("没有被防抖门禁暂缓的候选版本", "No candidates currently held by noise gate")
                    )
                    .padding(10)
                case .breakthrough:
                    LazyVStack(spacing: 9) {
                        let keyed = deduplicatedByItemId(breakthroughItems)
                        if keyed.isEmpty {
                            emptyHint(lang.tr(
                                String(format: "当前阈值 (Δ ≥ %.1f pts) 下暂无突破提交，可调低阈值查看", btThreshold),
                                String(format: "No breakthrough submissions at Δ ≥ %.1f pts", btThreshold)
                            ))
                        } else {
                            ForEach(Array(keyed.enumerated()), id: \.element.id) { idx, entry in
                                if entry.item.kind == "completed" {
                                    CompletedSubmissionCardView(
                                        item: entry.item,
                                        experience: matchedExperience(for: entry.item),
                                        onInspectExperience: { inspectedExperience = $0 }
                                    )
                                } else {
                                    CandidateSubmissionCardView(item: entry.item, rankIndex: idx + 1, clock: clock)
                                }
                            }
                        }
                    }
                    .padding(10)
                case .history:
                    HistoryQueueContentView(
                        completedItems: completedItems,
                        coldInflightItems: coldInflightItems,
                        fallbackPipeline: pipeline,
                        clock: clock,
                        emptyText: lang.tr("暂无历史评测记录", "No historical evaluations yet"),
                        matchedExperience: matchedExperience(for:),
                        onInspectExperience: { inspectedExperience = $0 }
                    )
                    .padding(10)
                }
            }
        }
        .sheet(item: $inspectedExperience) { entry in
            VStack(spacing: 0) {
                HStack {
                    Label(
                        lang.tr("结构化突破档案 · \(entry.id)", "Breakthrough Playbook · \(entry.id)"),
                        systemImage: "sparkles.rectangle.stack.fill"
                    )
                    .font(.system(size: 13.5, weight: .bold))
                    Spacer()
                    Button(lang.tr("关闭", "Close")) { inspectedExperience = nil }
                        .keyboardShortcut(.defaultAction)
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 11)
                Divider()
                BreakthroughDetailView(entry: entry)
                    .environmentObject(store)
            }
            .frame(width: 680, height: 520)
        }
    }

    private var breakthroughThresholdBar: some View {
        HStack(spacing: 8) {
            Image(systemName: "flame.fill")
                .font(.system(size: 11.5))
                .foregroundStyle(Color(red: 0.88, green: 0.46, blue: 0.08))
            Text(lang.tr("突破筛选阈值:", "Breakthrough Δ:"))
                .font(.system(size: 11.5, weight: .medium))
                .foregroundStyle(.secondary)
            Stepper(
                value: Binding(
                    get: { btThreshold },
                    set: { nv in Task { await store.updateScheduler(breakthroughThreshold: nv) } }
                ),
                in: 1.0...25.0,
                step: 1.0
            ) {
                Text(String(format: "Δ ≥ %.1f", btThreshold))
                    .font(.system(size: 11.5, weight: .bold, design: .monospaced))
                    .foregroundStyle(Color(red: 0.88, green: 0.46, blue: 0.08))
            }
            .controlSize(.small)

            ForEach([3.0, 5.0, 10.0, 15.0], id: \.self) { preset in
                Button(String(format: "+%.0f", preset)) {
                    Task { await store.updateScheduler(breakthroughThreshold: preset) }
                }
                .buttonStyle(.bordered)
                .controlSize(.mini)
                .tint(abs(btThreshold - preset) < 0.1 ? .orange : .secondary)
            }

            Spacer()

            Picker("", selection: $btSortByTime) {
                Text(lang.tr("按突破分", "By Δ Score")).tag(false)
                Text(lang.tr("按最新时间", "Newest First")).tag(true)
            }
            .pickerStyle(.segmented)
            .controlSize(.mini)
            .frame(width: 150)

            Button {
                Task { await store.syncBreakthroughs() }
            } label: {
                Label(lang.tr("归档至经验库", "Sync Playbook"), systemImage: "arrow.down.doc.fill")
                    .font(.system(size: 11))
            }
            .buttonStyle(.bordered)
            .controlSize(.mini)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 7)
        .background(Color(red: 0.92, green: 0.56, blue: 0.10).opacity(0.07))
    }

    private func emptyHint(_ text: String) -> some View {
        VStack(spacing: 8) {
            Image(systemName: "tray")
                .font(.system(size: 24))
                .foregroundStyle(.secondary)
            Text(text)
                .font(.system(size: 12.5))
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 40)
    }
}
