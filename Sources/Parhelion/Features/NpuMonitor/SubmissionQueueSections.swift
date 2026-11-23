import SwiftUI

struct KeyedQueueItem: Identifiable {
    let id: String
    let item: SubmissionQueueItem
}

func deduplicatedBySubmissionKey(_ list: [SubmissionQueueItem]) -> [KeyedQueueItem] {
    var seen = Set<String>()
    var out: [KeyedQueueItem] = []
    for item in list {
        let key = SubmissionKey.of(item)
        if seen.insert(key).inserted {
            out.append(KeyedQueueItem(id: key, item: item))
        }
    }
    return out
}

func deduplicatedByItemId(_ list: [SubmissionQueueItem]) -> [KeyedQueueItem] {
    var seen = Set<String>()
    var out: [KeyedQueueItem] = []
    for item in list {
        let key = item.kind == "completed" ? item.id : SubmissionKey.of(item)
        if seen.insert(key).inserted {
            out.append(KeyedQueueItem(id: key, item: item))
        }
    }
    return out
}

/// Isolated hot-subscribed header so tab counts stay consistent with live inflight filtering
/// without forcing the whole `SubmissionQueueView` to re-render at 1Hz.
struct SubmissionQueueHeaderTabs: View {
    @EnvironmentObject var store: ContestStore
    @EnvironmentObject var hotStore: HotTelemetryStore
    @Binding var selectedTab: QueueViewTab
    let waitingItems: [SubmissionQueueItem]
    let heldItems: [SubmissionQueueItem]
    let breakthroughCount: Int
    let completedCount: Int
    let fallbackPipeline: LivePipelineState?

    private var lang: AppLanguage { store.lang }

    var body: some View {
        let pipe = hotStore.pipeline ?? fallbackPipeline
        let activeKeys = store.activeDeckKeys(pipe)
        let visibleWaitingCount = deduplicatedBySubmissionKey(waitingItems)
            .filter { !activeKeys.contains($0.id) }.count
        let visibleHeldCount = deduplicatedBySubmissionKey(heldItems)
            .filter { !activeKeys.contains($0.id) }.count

        HStack {
            Label(lang.tr("提交与判题队列", "Submission & Judge Queue"), systemImage: "paperplane.circle.fill")
                .font(.system(size: 13, weight: .bold))
            Spacer()
            Picker("", selection: $selectedTab) {
                Text(lang.tr("待提交 (\(visibleWaitingCount))", "Queue (\(visibleWaitingCount))")).tag(QueueViewTab.waiting)
                Text(lang.tr("突破 (\(breakthroughCount))", "Breakthrough (\(breakthroughCount))")).tag(QueueViewTab.breakthrough)
                Text(lang.tr("历史 (\(completedCount))", "History (\(completedCount))")).tag(QueueViewTab.history)
                Text(lang.tr("暂缓 (\(visibleHeldCount))", "Held (\(visibleHeldCount))")).tag(QueueViewTab.held)
            }
            .pickerStyle(.segmented)
            .controlSize(.small)
            .frame(width: 365)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 9)
    }
}

/// Isolated hot-subscribed section for active inflight submissions & waiting queue cards.
struct WaitingQueueContentView: View {
    @EnvironmentObject var store: ContestStore
    @EnvironmentObject var hotStore: HotTelemetryStore
    @Namespace private var queueNamespace
    let waitingItems: [SubmissionQueueItem]
    let heldItems: [SubmissionQueueItem]
    let coldInflightItems: [SubmissionQueueItem]
    let fallbackPipeline: LivePipelineState?
    @ObservedObject var clock: WallClockTicker
    let emptyText: String
    let onOpenHeldTab: () -> Void

    private var livePipeline: LivePipelineState? {
        hotStore.pipeline ?? fallbackPipeline
    }

    var body: some View {
        let pipe = livePipeline
        let activeKeys = store.activeDeckKeys(pipe)
        let visibleWaiting = deduplicatedBySubmissionKey(waitingItems)
            .filter { !activeKeys.contains($0.id) }
        let visibleHeldCount = deduplicatedBySubmissionKey(heldItems)
            .filter { !activeKeys.contains($0.id) }.count
        let hasActiveDeck = !activeKeys.isEmpty || !coldInflightItems.isEmpty || (pipe?.isBusy ?? false)
        let waitingIds = visibleWaiting.map(\.id)

        VStack(spacing: 9) {
            ActiveSubmissionsDeckView(
                fallbackPipeline: fallbackPipeline,
                coldInflightItems: coldInflightItems,
                clock: clock,
                namespace: queueNamespace
            )

            if visibleWaiting.isEmpty && !hasActiveDeck {
                VStack(spacing: 10) {
                    Image(systemName: "tray")
                        .font(.system(size: 24))
                        .foregroundStyle(.secondary)
                    Text(emptyText)
                        .font(.system(size: 12.5))
                        .foregroundStyle(.secondary)
                    if visibleHeldCount > 0 {
                        Button(store.tr("查看 \(visibleHeldCount) 个门禁暂缓候选", "View \(visibleHeldCount) held candidate(s)")) {
                            onOpenHeldTab()
                        }
                        .buttonStyle(.bordered)
                        .controlSize(.small)
                    }
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 36)
                .transition(.opacity)
            } else {
                ForEach(Array(visibleWaiting.enumerated()), id: \.element.id) { idx, entry in
                    CandidateSubmissionCardView(item: entry.item, rankIndex: idx + 1, clock: clock)
                        .matchedGeometryEffect(id: entry.id, in: queueNamespace)
                        .transition(.opacity)
                }
            }
        }
        .animation(.snappy(duration: 0.28, extraBounce: 0.0), value: waitingIds)
    }
}

/// Isolated hot-subscribed section for Held tab so manual Jump Queue glides into the active deck.
struct HeldQueueContentView: View {
    @EnvironmentObject var store: ContestStore
    @EnvironmentObject var hotStore: HotTelemetryStore
    @Namespace private var heldNamespace
    let heldItems: [SubmissionQueueItem]
    let coldInflightItems: [SubmissionQueueItem]
    let fallbackPipeline: LivePipelineState?
    @ObservedObject var clock: WallClockTicker
    let emptyText: String

    private var livePipeline: LivePipelineState? {
        hotStore.pipeline ?? fallbackPipeline
    }

    var body: some View {
        let pipe = livePipeline
        let activeKeys = store.activeDeckKeys(pipe)
        let visibleHeld = deduplicatedBySubmissionKey(heldItems)
            .filter { !activeKeys.contains($0.id) }
        let heldIds = visibleHeld.map(\.id)

        VStack(spacing: 9) {
            ActiveSubmissionsDeckView(
                fallbackPipeline: fallbackPipeline,
                coldInflightItems: coldInflightItems,
                clock: clock,
                namespace: heldNamespace
            )

            if visibleHeld.isEmpty {
                VStack(spacing: 8) {
                    Image(systemName: "tray")
                        .font(.system(size: 24))
                        .foregroundStyle(.secondary)
                    Text(emptyText)
                        .font(.system(size: 12.5))
                        .foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 40)
            } else {
                ForEach(Array(visibleHeld.enumerated()), id: \.element.id) { idx, entry in
                    CandidateSubmissionCardView(item: entry.item, rankIndex: idx + 1, clock: clock)
                        .matchedGeometryEffect(id: entry.id, in: heldNamespace)
                        .transition(.opacity)
                }
            }
        }
        .animation(.snappy(duration: 0.28, extraBounce: 0.0), value: heldIds)
    }
}

/// Isolated hot-subscribed section for History tab: renders active judging cards above completed outcomes.
struct HistoryQueueContentView: View {
    @EnvironmentObject var store: ContestStore
    @EnvironmentObject var hotStore: HotTelemetryStore
    let completedItems: [SubmissionQueueItem]
    let coldInflightItems: [SubmissionQueueItem]
    let fallbackPipeline: LivePipelineState?
    @ObservedObject var clock: WallClockTicker
    let emptyText: String
    let matchedExperience: (SubmissionQueueItem) -> BreakthroughEntry?
    let onInspectExperience: (BreakthroughEntry) -> Void

    private var livePipeline: LivePipelineState? {
        hotStore.pipeline ?? fallbackPipeline
    }

    var body: some View {
        let pipe = livePipeline
        let activeIds = store.activeDeckKeys(pipe)
        let hasActives = !activeIds.isEmpty || !coldInflightItems.isEmpty || (pipe?.isBusy ?? false)
        let keyedCompleted = deduplicatedByItemId(completedItems)
        let layoutIds = activeIds.sorted() + keyedCompleted.prefix(12).map(\.id)

        VStack(spacing: 9) {
            ActiveSubmissionsDeckView(
                fallbackPipeline: fallbackPipeline,
                coldInflightItems: coldInflightItems,
                clock: clock
            )

            if keyedCompleted.isEmpty && !hasActives {
                VStack(spacing: 8) {
                    Image(systemName: "tray")
                        .font(.system(size: 24))
                        .foregroundStyle(.secondary)
                    Text(emptyText)
                        .font(.system(size: 12.5))
                        .foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 40)
            } else {
                LazyVStack(spacing: 9) {
                    ForEach(keyedCompleted) { entry in
                        CompletedSubmissionCardView(
                            item: entry.item,
                            experience: matchedExperience(entry.item),
                            onInspectExperience: onInspectExperience
                        )
                        .transition(.opacity)
                    }
                }
            }
        }
        .animation(.snappy(duration: 0.28, extraBounce: 0.0), value: layoutIds)
    }
}
