import AppKit
import SwiftUI

struct BreakthroughDetailView: View {
    @EnvironmentObject var store: ContestStore
    let entry: BreakthroughEntry

    private var lang: AppLanguage { store.lang }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                headerBanner
                ruleHighlightBox
                sectionCard(
                    title: lang.tr("1. 性能瓶颈诊断", "1. Bottleneck Diagnosis"),
                    icon: "magnifyingglass.circle.fill",
                    tint: .orange
                ) {
                    Text(entry.bottleneck)
                        .font(.system(size: 12.5))
                        .fixedSize(horizontal: false, vertical: true)
                }
                sectionCard(
                    title: lang.tr("2. 核心突破机制", "2. Core Breakthrough Mechanisms"),
                    icon: "cpu.fill",
                    tint: .blue
                ) {
                    VStack(alignment: .leading, spacing: 7) {
                        ForEach(Array(entry.mechanisms.enumerated()), id: \.offset) { idx, mech in
                            HStack(alignment: .top, spacing: 8) {
                                Text("\(idx + 1).")
                                    .font(.system(size: 11.5, weight: .bold, design: .monospaced))
                                    .foregroundStyle(.blue)
                                    .frame(width: 18, alignment: .trailing)
                                Text(mech)
                                    .font(.system(size: 12.5))
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                }
                sectionCard(
                    title: lang.tr("3. 精度与同步不变量", "3. Precision & Sync Invariants"),
                    icon: "checkmark.shield.fill",
                    tint: Color(red: 0.10, green: 0.72, blue: 0.42)
                ) {
                    Text(entry.precisionGuard)
                        .font(.system(size: 12.5))
                        .fixedSize(horizontal: false, vertical: true)
                }
                agentReceiptsSection
                tagsAndFileRow
            }
            .padding(16)
        }
    }

    private var headerBanner: some View {
        SurfaceCard(highlighted: true, highlightColor: Color(red: 0.88, green: 0.46, blue: 0.08), padding: 14) {
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 8) {
                    DifficultyBadge(difficulty: entry.difficulty)
                    Text(entry.problem)
                        .font(.system(size: 16, weight: .bold))
                    Text(entry.shortSha)
                        .font(.system(size: 12, design: .monospaced))
                        .foregroundStyle(.secondary)
                    if let base = entry.baselineSha, !base.isEmpty {
                        Text("vs \(String(base.prefix(7)))")
                            .font(.system(size: 11, design: .monospaced))
                            .foregroundStyle(.tertiary)
                    }
                    Spacer()
                    SemanticPill(text: lang.categoryName(entry.category), icon: "tag.fill", compact: true)
                    SemanticPill(text: String(format: "%@ %+.2f", lang.tr("突破", "BREAKTHROUGH"), entry.deltaRaw), icon: "flame.fill")
                }

                Text(entry.title)
                    .font(.system(size: 13.5, weight: .semibold))

                HStack(spacing: 14) {
                    metricChip(label: lang.tr("优化前得分", "Score Before"), value: String(format: "%.2f", entry.scoreBefore), color: .secondary)
                    Image(systemName: "arrow.right")
                        .font(.system(size: 11, weight: .bold))
                        .foregroundStyle(.tertiary)
                    metricChip(label: lang.tr("优化后得分", "Score After"), value: String(format: "%.2f", entry.scoreAfter), color: .primary)
                    Divider().frame(height: 24)
                    metricChip(label: lang.tr("单题净提分", "Raw Gain"), value: String(format: "%+.2f pts", entry.deltaRaw), color: Color(red: 0.10, green: 0.72, blue: 0.42))
                    metricChip(label: lang.tr("大赛加权贡献", "Weighted Gain"), value: String(format: "%+.2f pts", entry.deltaWeighted), color: Color(red: 0.88, green: 0.46, blue: 0.08))
                    Spacer()
                }
                .padding(.top, 2)
            }
        }
    }

    private var ruleHighlightBox: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "lightbulb.max.fill")
                .font(.system(size: 16))
                .foregroundStyle(Color(red: 0.88, green: 0.52, blue: 0.08))
                .padding(.top, 1)
            VStack(alignment: .leading, spacing: 4) {
                Text(lang.tr("可迁移 KernelC 算子范式", "Transferable KernelC Pattern"))
                    .font(.system(size: 11.5, weight: .bold))
                    .foregroundStyle(Color(red: 0.88, green: 0.46, blue: 0.08))
                Text(entry.transferableRule)
                    .font(.system(size: 12.5, weight: .medium))
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
        }
        .padding(12)
        .background(
            RoundedRectangle(cornerRadius: 9, style: .continuous)
                .fill(Color(red: 0.92, green: 0.56, blue: 0.10).opacity(0.11))
                .overlay(
                    RoundedRectangle(cornerRadius: 9, style: .continuous)
                        .strokeBorder(Color(red: 0.92, green: 0.56, blue: 0.10).opacity(0.32), lineWidth: 1)
                )
        )
    }

    private func sectionCard<Content: View>(title: String, icon: String, tint: Color, @ViewBuilder content: () -> Content) -> some View {
        let builtContent = content()
        return SurfaceCard(padding: 12) {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 6) {
                    Image(systemName: icon)
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(tint)
                    Text(title)
                        .font(.system(size: 12, weight: .bold))
                }
                builtContent
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    /// Agent summaries collected by `opsb bridge collect` for this entry.
    @ViewBuilder
    private var agentReceiptsSection: some View {
        let receipts = store.breakthroughSummaries.filter { $0.entryId == entry.id }
        if !receipts.isEmpty {
            sectionCard(
                title: lang.tr("4. Agent 回执 (\(receipts.count))", "4. Agent Receipts (\(receipts.count))"),
                icon: "checkmark.seal.fill",
                tint: Color(red: 0.12, green: 0.68, blue: 0.62)
            ) {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(receipts) { receipt in
                        VStack(alignment: .leading, spacing: 3) {
                            HStack(spacing: 6) {
                                SemanticPill(text: receipt.agentId, compact: true)
                                Text(receipt.agentDisplay)
                                    .font(.system(size: 10))
                                    .foregroundStyle(.secondary)
                                Spacer()
                                if let ts = receipt.collectedAt {
                                    Text(String(ts.prefix(19)).replacingOccurrences(of: "T", with: " "))
                                        .font(.system(size: 9.5, design: .monospaced))
                                        .foregroundStyle(.tertiary)
                                }
                            }
                            Text(receipt.summary)
                                .font(.system(size: 11.5))
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(8)
                        .background(RoundedRectangle(cornerRadius: 7).fill(Color.primary.opacity(0.03)))
                    }
                }
            }
        }
    }

    private var tagsAndFileRow: some View {
        HStack(spacing: 6) {
            ForEach(entry.tags, id: \.self) { tag in
                Text("#\(tag)")
                    .font(.system(size: 10.5, weight: .medium, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 7)
                    .padding(.vertical, 3)
                    .background(Capsule().fill(Color.primary.opacity(0.06)))
            }
            Spacer()
            if let absPath = entry.absDocPath {
                Button {
                    NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: absPath)])
                } label: {
                    Label(entry.docPath, systemImage: "doc.text.magnifyingglass")
                        .font(.system(size: 11, design: .monospaced))
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
            }
        }
    }

    private func metricChip(label: String, value: String, color: Color) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label).font(.system(size: 10)).foregroundStyle(.secondary)
            Text(value).font(.system(size: 13, weight: .bold, design: .monospaced)).foregroundStyle(color)
        }
    }
}
