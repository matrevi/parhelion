import AppKit
import SwiftUI

struct BreakthroughLibraryView: View {
    @EnvironmentObject var store: ContestStore
    @UIState private var selectedEntryId: String? = nil
    @UIState private var selectedCategory: String = "ALL"

    private var lang: AppLanguage { store.lang }

    private var categories: [String] {
        let cats = Set(store.breakthroughEntries.map(\.category)).sorted()
        return ["ALL"] + cats
    }

    private var filteredEntries: [BreakthroughEntry] {
        store.breakthroughEntries.filter { entry in
            entry.deltaRaw >= store.breakthroughThreshold
                && (selectedCategory == "ALL" || entry.category == selectedCategory)
        }
    }

    private var activeEntry: BreakthroughEntry? {
        if let id = selectedEntryId, let found = filteredEntries.first(where: { $0.id == id }) {
            return found
        }
        return filteredEntries.first
    }

    var body: some View {
        VStack(spacing: 0) {
            topFilterBar
            Divider()
            HSplitView {
                entryListPane
                    .frame(minWidth: 320, idealWidth: 370, maxWidth: 440)
                detailPane
                    .frame(minWidth: 460, maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .onAppear {
            if store.breakthroughEntries.isEmpty {
                Task { await store.reloadBreakthroughs() }
            }
        }
    }

    private var topFilterBar: some View {
        HStack(spacing: 12) {
            Label(lang.tr("结构化突破经验库 (breakthrough/)", "Breakthrough Playbook (breakthrough/)"), systemImage: "sparkles.rectangle.stack.fill")
                .font(.system(size: 13.5, weight: .bold))

            Divider().frame(height: 18)

            HStack(spacing: 6) {
                Text(lang.tr("突破阈值:", "Threshold:"))
                    .font(.system(size: 11.5, weight: .medium))
                    .foregroundStyle(.secondary)
                Stepper(
                    value: Binding(
                        get: { store.breakthroughThreshold },
                        set: { nv in Task { await store.updateScheduler(breakthroughThreshold: nv) } }
                    ),
                    in: 1.0...25.0,
                    step: 1.0
                ) {
                    Text(String(format: "Δ ≥ %.1f pts", store.breakthroughThreshold))
                        .font(.system(size: 12, weight: .bold, design: .monospaced))
                        .foregroundStyle(Color(red: 0.88, green: 0.46, blue: 0.08))
                }
                .controlSize(.small)

                ForEach([3.0, 5.0, 10.0, 15.0], id: \.self) { preset in
                    Button(String(format: "+%.0f", preset)) {
                        Task { await store.updateScheduler(breakthroughThreshold: preset) }
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.mini)
                    .tint(abs(store.breakthroughThreshold - preset) < 0.1 ? .orange : .secondary)
                }
            }

            Picker(lang.tr("类别", "Category"), selection: $selectedCategory) {
                Text(lang.tr("全部类别", "All Categories")).tag("ALL")
                ForEach(categories.filter { $0 != "ALL" }, id: \.self) { cat in
                    Text(cat).tag(cat)
                }
            }
            .pickerStyle(.menu)
            .controlSize(.small)
            .frame(width: 155)

            Spacer()

            Button {
                Task { await store.syncBreakthroughs() }
            } label: {
                Label(lang.tr("自动归档新突破", "Sync New"), systemImage: "arrow.triangle.2.circlepath")
                    .font(.system(size: 11.5))
            }
            .buttonStyle(.bordered)
            .controlSize(.small)

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

    private var entryListPane: some View {
        ScrollView {
            LazyVStack(spacing: 8) {
                if filteredEntries.isEmpty {
                    VStack(spacing: 8) {
                        Image(systemName: "sparkles")
                            .font(.system(size: 24))
                            .foregroundStyle(.secondary)
                        Text(lang.tr(
                            String(format: "当前阈值 (Δ ≥ %.1f) 下无匹配突破档案", store.breakthroughThreshold),
                            String(format: "No breakthrough entries at Δ ≥ %.1f", store.breakthroughThreshold)
                        ))
                        .font(.system(size: 12))
                        .foregroundStyle(.secondary)
                        Text(lang.tr("可调低上方阈值 (如 +3) 查看中等幅度优化经验", "Lower the threshold above (e.g. +3) to inspect moderate gains"))
                            .font(.system(size: 11))
                            .foregroundStyle(.tertiary)
                    }
                    .padding(.vertical, 48)
                } else {
                    ForEach(filteredEntries) { entry in
                        let selected = activeEntry?.id == entry.id
                        Button {
                            selectedEntryId = entry.id
                        } label: {
                            entryRowCard(entry, selected: selected)
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
            .padding(10)
        }
    }

    private func entryRowCard(_ entry: BreakthroughEntry, selected: Bool) -> some View {
        SurfaceCard(highlighted: selected, highlightColor: Color(red: 0.88, green: 0.46, blue: 0.08), padding: 10) {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 6) {
                    DifficultyBadge(difficulty: entry.difficulty)
                    Text(entry.problem)
                        .font(.system(size: 12.5, weight: .bold))
                    Text(entry.shortSha)
                        .font(.system(size: 10.5, design: .monospaced))
                        .foregroundStyle(.secondary)
                    Spacer()
                    Text(String(format: "%+.2f", entry.deltaRaw))
                        .font(.system(size: 12, weight: .heavy, design: .monospaced))
                        .foregroundStyle(Color(red: 0.10, green: 0.72, blue: 0.42))
                }
                Text(entry.title)
                    .font(.system(size: 11.5, weight: .medium))
                    .lineLimit(2)
                    .multilineTextAlignment(.leading)
                HStack(spacing: 6) {
                    SemanticPill(text: entry.category, compact: true)
                    Text(lang.tr(
                        String(format: "%.2f → %.2f (加权 %+.1f)", entry.scoreBefore, entry.scoreAfter, entry.deltaWeighted),
                        String(format: "%.2f → %.2f (w %+.1f)", entry.scoreBefore, entry.scoreAfter, entry.deltaWeighted)
                    ))
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
                    Spacer()
                }
            }
        }
    }

    @ViewBuilder
    private var detailPane: some View {
        if let entry = activeEntry {
            BreakthroughDetailView(entry: entry)
        } else {
            VStack(spacing: 8) {
                Image(systemName: "book.closed")
                    .font(.system(size: 26))
                    .foregroundStyle(.secondary)
                Text(lang.tr("选择左侧突破条目查看结构化经验", "Select a breakthrough entry on the left"))
                    .font(.system(size: 12.5))
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }
}
