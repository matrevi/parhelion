import AppKit
import SwiftUI

struct BreakthroughLibraryView: View {
    @EnvironmentObject var store: ContestStore

    @UIState private var selectedPillar: TriadKBPillar = .breakthrough
    @UIState private var selectedPortability: TriadPortabilityFilter = .all
    @UIState private var searchText: String = ""

    // Breakthroughs pillar state
    @UIState private var selectedBreakthroughId: String? = nil
    @UIState private var selectedCategory: String = "ALL"
    @UIState private var sortByTime: Bool = false

    // Snippets / Community pillar state
    @UIState private var selectedKBId: String? = nil
    @UIState private var detailCache: [String: TriadKBEntry] = [:]
    @UIState private var loadingDetailId: String? = nil
    @UIState private var feedbackRequest: TriadKBFeedbackRequest? = nil

    private var lang: AppLanguage { store.lang }

    private var triadById: [String: TriadKBEntry] {
        Dictionary(uniqueKeysWithValues: store.triadEntries.map { ($0.id, $0) })
    }

    private var categories: [String] {
        let cats = Set(store.breakthroughEntries.map(\.category)).sorted()
        return ["ALL"] + cats
    }

    private var filteredBreakthroughs: [BreakthroughEntry] {
        let q = searchText.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        return store.breakthroughEntries
            .filter { entry in
                guard entry.deltaRaw >= store.breakthroughThreshold else { return false }
                guard selectedCategory == "ALL" || entry.category == selectedCategory else { return false }
                if selectedPortability != .all, let kb = triadById[entry.id] {
                    guard kb.portability == selectedPortability.rawValue else { return false }
                }
                if !q.isEmpty {
                    let blob = "\(entry.id) \(entry.problem) \(entry.title) \(entry.bottleneck) \(entry.tags.joined(separator: " "))".lowercased()
                    guard blob.contains(q) else { return false }
                }
                return true
            }
            .sorted { a, b in
                if sortByTime {
                    return a.timestamp != b.timestamp ? a.timestamp > b.timestamp : a.deltaRaw > b.deltaRaw
                }
                return a.deltaRaw != b.deltaRaw ? a.deltaRaw > b.deltaRaw : a.timestamp > b.timestamp
            }
    }

    private var activeBreakthrough: BreakthroughEntry? {
        if let id = selectedBreakthroughId, let found = filteredBreakthroughs.first(where: { $0.id == id }) {
            return found
        }
        return filteredBreakthroughs.first
    }

    private var filteredTriadEntries: [TriadKBEntry] {
        let targetPillar = selectedPillar.rawValue
        let q = searchText.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        return store.triadEntries.filter { entry in
            guard entry.pillar == targetPillar else { return false }
            if selectedPortability != .all && entry.portability != selectedPortability.rawValue {
                return false
            }
            if !q.isEmpty {
                let blob = "\(entry.id) \(entry.title) \(entry.summary) \(entry.bottlenecks.joined(separator: " ")) \(entry.tags.joined(separator: " "))".lowercased()
                guard blob.contains(q) else { return false }
            }
            return true
        }
    }

    private var activeTriadEntry: TriadKBEntry? {
        if let id = selectedKBId, let found = filteredTriadEntries.first(where: { $0.id == id }) {
            return found
        }
        return filteredTriadEntries.first
    }

    var body: some View {
        VStack(spacing: 0) {
            topHubBar
            if selectedPillar != .evolution {
                Divider()
                filterBar
            }
            Divider()
            pillarContent
        }
        .onAppear {
            if store.breakthroughEntries.isEmpty {
                Task { await store.reloadBreakthroughs() }
            }
            if store.triadEntries.isEmpty {
                Task { await store.reloadTriadKB() }
            }
        }
        .onChange(of: selectedPillar) { _, newPillar in
            selectedKBId = nil
            if newPillar == .evolution {
                Task { await store.reloadTriadHistory() }
            } else {
                ensureActiveDetailLoaded()
            }
        }
        .onChange(of: selectedKBId) { _, _ in
            ensureActiveDetailLoaded()
        }
        .onChange(of: store.triadEntries) { _, _ in
            ensureActiveDetailLoaded()
        }
        .sheet(item: $feedbackRequest) { req in
            TriadKBFeedbackSheet(request: req) {
                detailCache.removeValue(forKey: req.entry.id)
                ensureActiveDetailLoaded()
            }
            .environmentObject(store)
        }
    }

    private var topHubBar: some View {
        HStack(spacing: 12) {
            Label(lang.tr("五元知识库 (5-Pillar KB)", "5-Pillar Knowledge Hub"), systemImage: "sparkles.rectangle.stack.fill")
                .font(.system(size: 13.5, weight: .bold))

            Picker("", selection: $selectedPillar) {
                ForEach(TriadKBPillar.allCases) { p in
                    Text(p.label(lang)).tag(p)
                }
            }
            .pickerStyle(.segmented)
            .frame(maxWidth: 520)

            Spacer()

            ProgressView().controlSize(.small)
                .opacity(store.isLoadingTriadKB ? 1 : 0)
                .accessibilityHidden(!store.isLoadingTriadKB)

            Button {
                Task {
                    await store.reloadTriadKB()
                    await store.reloadTriadHistory()
                    detailCache.removeAll()
                    ensureActiveDetailLoaded()
                }
            } label: {
                Label(lang.tr("刷新 KB", "Refresh KB"), systemImage: "arrow.clockwise")
                    .font(.system(size: 11.5))
            }
            .buttonStyle(.bordered)
            .controlSize(.small)

            Button {
                Task { await store.syncBreakthroughs() }
            } label: {
                Label(lang.tr("自动归档新突破", "Sync New"), systemImage: "arrow.triangle.2.circlepath")
                    .font(.system(size: 11.5))
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            .disabled(selectedPillar != .breakthrough)
            .opacity(selectedPillar == .breakthrough ? 1 : 0)
            .accessibilityHidden(selectedPillar != .breakthrough)

            if !store.breakthroughDir.isEmpty {
                Button {
                    NSWorkspace.shared.open(URL(fileURLWithPath: store.breakthroughDir))
                } label: {
                    Label(lang.tr("打开目录", "Open Folder"), systemImage: "folder")
                        .font(.system(size: 11.5))
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 9)
        .background(Color.primary.opacity(0.025))
    }

    private var filterBar: some View {
        HStack(spacing: 10) {
            TextField(
                lang.tr("按瓶颈 / 算子 / 关键词筛选…", "Filter by bottleneck / operator / keyword…"),
                text: $searchText
            )
            .textFieldStyle(.roundedBorder)
            .controlSize(.small)
            .frame(width: 220)

            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 5) {
                    ForEach(TriadPortabilityFilter.allCases) { pf in
                        let active = selectedPortability == pf
                        Button(pf.label(lang)) {
                            selectedPortability = pf
                        }
                        .buttonStyle(.bordered)
                        .controlSize(.mini)
                        .tint(active ? .accentColor : .secondary)
                    }
                }
            }

            if selectedPillar == .breakthrough {
                Divider().frame(height: 18)
                Stepper(
                    value: Binding(
                        get: { store.breakthroughThreshold },
                        set: { nv in Task { await store.updateScheduler(breakthroughThreshold: nv) } }
                    ),
                    in: 1.0...25.0,
                    step: 1.0
                ) {
                    Text(String(format: "Δ ≥ %.0f", store.breakthroughThreshold))
                        .font(.system(size: 11.5, weight: .bold, design: .monospaced))
                        .foregroundStyle(Color(red: 0.88, green: 0.46, blue: 0.08))
                }
                .controlSize(.small)

                Picker(lang.tr("类别", "Category"), selection: $selectedCategory) {
                    Text(lang.tr("全部类别", "All Categories")).tag("ALL")
                    ForEach(categories.filter { $0 != "ALL" }, id: \.self) { cat in
                        Text(lang.categoryName(cat)).tag(cat)
                    }
                }
                .pickerStyle(.menu)
                .controlSize(.small)
                .frame(width: 130)

                Picker("", selection: $sortByTime) {
                    Text(lang.tr("按突破分", "By Δ")).tag(false)
                    Text(lang.tr("按最新", "Newest")).tag(true)
                }
                .pickerStyle(.segmented)
                .controlSize(.small)
                .frame(width: 120)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 7)
        .background(Color.primary.opacity(0.015))
    }

    @ViewBuilder
    private var pillarContent: some View {
        if selectedPillar == .evolution {
            TriadEvolutionLogView()
        } else {
            HSplitView {
                Group {
                    if selectedPillar == .breakthrough {
                        BreakthroughListPane(
                            entries: filteredBreakthroughs,
                            triadById: triadById,
                            selectedId: $selectedBreakthroughId,
                            activeId: activeBreakthrough?.id
                        )
                    } else {
                        TriadKBListPane(
                            entries: filteredTriadEntries,
                            selectedEntryId: $selectedKBId,
                            emptyHint: lang.tr("当前筛选条件下无知识条目", "No KB entries match current filters")
                        )
                    }
                }
                .frame(minWidth: 320, idealWidth: 370, maxWidth: 440)

                Group {
                    if selectedPillar == .breakthrough {
                        breakthroughDetailPane
                    } else {
                        triadDetailPane
                    }
                }
                .frame(minWidth: 460, maxWidth: .infinity, maxHeight: .infinity)
            }
        }
    }

    @ViewBuilder
    private var breakthroughDetailPane: some View {
        if let entry = activeBreakthrough {
            BreakthroughDetailView(
                entry: entry,
                triadEntry: triadById[entry.id],
                onFeedback: { kb, verdict in
                    feedbackRequest = TriadKBFeedbackRequest(entry: kb, initialVerdict: verdict)
                }
            )
        } else {
            Text(lang.tr("选择左侧突破条目查看结构化经验", "Select a breakthrough entry on the left"))
                .font(.system(size: 12.5))
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    @ViewBuilder
    private var triadDetailPane: some View {
        if let entry = activeTriadEntry {
            let detailed = entry.pillar == "bugs" ? nil : detailCache[entry.id]
            TriadKBDetailView(
                entry: detailed ?? entry,
                loadedContent: detailed?.content,
                isLoadingContent: loadingDetailId == entry.id,
                onFeedback: { verdict in
                    feedbackRequest = TriadKBFeedbackRequest(entry: entry, initialVerdict: verdict)
                },
                onSelectRelatedSnippet: { sid in
                    selectedPillar = .snippets
                    selectedPortability = .all
                    searchText = ""
                    selectedKBId = sid
                }
            )
        } else {
            Text(lang.tr("选择左侧知识条目查看代码与实证", "Select an entry on the left to inspect code & evidence"))
                .font(.system(size: 12.5))
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    private func ensureActiveDetailLoaded() {
        guard selectedPillar == .snippets || selectedPillar == .hardware || selectedPillar == .community || selectedPillar == .bugs,
              let target = activeTriadEntry else { return }
        if detailCache[target.id]?.content != nil || loadingDetailId == target.id { return }
        let targetId = target.id
        loadingDetailId = targetId
        Task {
            if let fetched = await store.loadTriadEntryDetail(id: targetId) {
                detailCache[targetId] = fetched
            }
            if loadingDetailId == targetId {
                loadingDetailId = nil
            }
        }
    }
}
