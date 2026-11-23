import AppKit
import SwiftUI

struct TriadKBDetailView: View {
    @EnvironmentObject var store: ContestStore
    let entry: TriadKBEntry
    let loadedContent: String?
    let isLoadingContent: Bool
    var onFeedback: (String) -> Void
    var onSelectRelatedSnippet: ((String) -> Void)? = nil

    @UIState private var copiedFeedback: Bool = false

    private var lang: AppLanguage { store.lang }
    private var isCodeSnippet: Bool {
        entry.pillar == "snippets" || entry.docPath.hasSuffix(".h") || entry.docPath.hasSuffix(".hpp")
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                headerBanner
                summaryCard
                codeOrDocSection
                metadataSection
                evidenceLedgerSection
            }
            .padding(16)
        }
    }

    private var headerBanner: some View {
        let accent: Color = entry.isAntiPattern
            ? Color(red: 0.84, green: 0.23, blue: 0.26)
            : (entry.pillar == "snippets" ? Color(red: 0.12, green: 0.68, blue: 0.62) : Color.accentColor)
        return SurfaceCard(highlighted: true, highlightColor: accent, padding: 14) {
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 8) {
                    Text(entry.id)
                        .font(.system(size: 11.5, weight: .bold, design: .monospaced))
                        .foregroundStyle(.secondary)
                    SemanticPill(text: entry.portability, compact: true)
                    SemanticPill(text: entry.status, compact: true)
                    SemanticPill(text: lang.categoryName(entry.category), compact: true)
                    Spacer()
                    TriadConfidenceBadge(entry: entry)
                }

                Text(entry.title)
                    .font(.system(size: 15, weight: .bold))
                    .fixedSize(horizontal: false, vertical: true)

                HStack(spacing: 8) {
                    Button {
                        onFeedback("win")
                    } label: {
                        Label(lang.tr("有效 (+Win)", "Win (+)"), systemImage: "hand.thumbsup.fill")
                            .font(.system(size: 11, weight: .semibold))
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(Color(red: 0.10, green: 0.72, blue: 0.43))
                    .controlSize(.small)

                    Button {
                        onFeedback("fail")
                    } label: {
                        Label(lang.tr("无效 (-Fail)", "Fail (-)"), systemImage: "hand.thumbsdown.fill")
                            .font(.system(size: 11, weight: .semibold))
                    }
                    .buttonStyle(.bordered)
                    .tint(.orange)
                    .controlSize(.small)

                    if !entry.isAntiPattern {
                        Button {
                            onFeedback("anti-pattern")
                        } label: {
                            Label(lang.tr("标为反模式", "Anti-Pattern"), systemImage: "exclamationmark.octagon")
                                .font(.system(size: 11, weight: .medium))
                        }
                        .buttonStyle(.bordered)
                        .tint(.red)
                        .controlSize(.small)
                    }

                    Spacer()

                    if let absPath = entry.absDocPath, !absPath.isEmpty {
                        Button {
                            NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: absPath)])
                        } label: {
                            Label(entry.docPath, systemImage: "doc.text.magnifyingglass")
                                .font(.system(size: 10.5, design: .monospaced))
                        }
                        .buttonStyle(.bordered)
                        .controlSize(.small)
                    }
                }
            }
        }
    }

    private var summaryCard: some View {
        SurfaceCard(padding: 12) {
            VStack(alignment: .leading, spacing: 6) {
                Text(lang.tr("核心机制与适用场景", "Summary & Applicability"))
                    .font(.system(size: 11.5, weight: .bold))
                    .foregroundStyle(.secondary)
                Text(entry.summary)
                    .font(.system(size: 12.5))
                    .fixedSize(horizontal: false, vertical: true)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var codeOrDocSection: some View {
        let content = loadedContent ?? entry.content ?? ""
        return SurfaceCard(padding: 12) {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 8) {
                    Label(
                        isCodeSnippet
                            ? lang.tr("KernelC C++ 代码积木 (\(entry.docPath))", "KernelC C++ Snippet (\(entry.docPath))")
                            : lang.tr("方案文档 (\(entry.docPath))", "Knowledge Document (\(entry.docPath))"),
                        systemImage: isCodeSnippet ? "curlybraces.square.fill" : "doc.plaintext.fill"
                    )
                    .font(.system(size: 12, weight: .bold))

                    Spacer()

                    if isLoadingContent {
                        ProgressView().controlSize(.small)
                    }

                    Button {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(content, forType: .string)
                        copiedFeedback = true
                        DispatchQueue.main.asyncAfter(deadline: .now() + 1.8) {
                            copiedFeedback = false
                        }
                    } label: {
                        Label(
                            copiedFeedback
                                ? lang.tr("已复制", "Copied ✓")
                                : (isCodeSnippet ? lang.tr("复制 C++ 代码", "Copy C++") : lang.tr("复制全文", "Copy")),
                            systemImage: copiedFeedback ? "checkmark" : "doc.on.doc"
                        )
                        .font(.system(size: 11, weight: .semibold))
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(copiedFeedback ? .green : .accentColor)
                    .controlSize(.small)
                    .disabled(content.isEmpty)
                }

                if content.isEmpty {
                    Text(isLoadingContent
                         ? lang.tr("正在加载文档内容…", "Loading document…")
                         : lang.tr("暂无文件内容", "No document body available"))
                        .font(.system(size: 11.5))
                        .foregroundStyle(.secondary)
                        .padding(.vertical, 12)
                } else {
                    ScrollView([.vertical, .horizontal]) {
                        Text(content)
                            .font(.system(size: 11.5, design: .monospaced))
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(10)
                    }
                    .frame(minHeight: 160, maxHeight: 340)
                    .background(
                        RoundedRectangle(cornerRadius: 8, style: .continuous)
                            .fill(Color.black.opacity(0.06))
                            .overlay(
                                RoundedRectangle(cornerRadius: 8, style: .continuous)
                                    .strokeBorder(Color.primary.opacity(0.08), lineWidth: 1)
                            )
                    )
                }
            }
        }
    }

    private var metadataSection: some View {
        SurfaceCard(padding: 12) {
            VStack(alignment: .leading, spacing: 8) {
                chipRow(title: lang.tr("瓶颈标签", "Bottlenecks"), items: entry.bottlenecks, tint: .orange)
                chipRow(title: lang.tr("适用分层", "Regimes"), items: entry.regimes, tint: .blue)
                chipRow(title: lang.tr("数据类型", "Dtypes"), items: entry.dtypes, tint: .teal)
                if !entry.relatedSnippets.isEmpty {
                    HStack(alignment: .center, spacing: 6) {
                        Text(lang.tr("关联积木", "Snippets"))
                            .font(.system(size: 10.5, weight: .semibold))
                            .foregroundStyle(.secondary)
                            .frame(width: 68, alignment: .leading)
                        ForEach(entry.relatedSnippets, id: \.self) { sid in
                            Button {
                                onSelectRelatedSnippet?(sid)
                            } label: {
                                Text(sid)
                                    .font(.system(size: 10.5, weight: .semibold, design: .monospaced))
                            }
                            .buttonStyle(.bordered)
                            .controlSize(.mini)
                        }
                        Spacer()
                    }
                }
            }
        }
    }

    private func chipRow(title: String, items: [String], tint: Color) -> some View {
        HStack(alignment: .top, spacing: 6) {
            Text(title)
                .font(.system(size: 10.5, weight: .semibold))
                .foregroundStyle(.secondary)
                .frame(width: 68, alignment: .leading)
            if items.isEmpty {
                Text("—").font(.system(size: 10.5)).foregroundStyle(.tertiary)
            } else {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 5) {
                        ForEach(items, id: \.self) { item in
                            Text(item)
                                .font(.system(size: 10, weight: .medium, design: .monospaced))
                                .foregroundStyle(tint)
                                .padding(.horizontal, 6)
                                .padding(.vertical, 2)
                                .background(Capsule().fill(tint.opacity(0.12)))
                        }
                    }
                }
            }
        }
    }

    private var evidenceLedgerSection: some View {
        SurfaceCard(padding: 12) {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Text(lang.tr("实证记录账本 (\(entry.evidence.count))", "EVIDENCE LEDGER (\(entry.evidence.count))"))
                        .font(.system(size: 10.5, weight: .bold))
                        .foregroundStyle(.secondary)
                    Spacer()
                    Text("W\(entry.wins) / F\(entry.fails)")
                        .font(.system(size: 10.5, weight: .bold, design: .monospaced))
                        .foregroundStyle(.secondary)
                }
                if entry.evidence.isEmpty {
                    Text(lang.tr("尚无实证记录", "No evidence recorded yet."))
                        .font(.system(size: 11.5))
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(entry.evidence) { ev in
                        HStack(alignment: .top, spacing: 8) {
                            SemanticPill(text: ev.verdict.uppercased(), compact: true)
                            Text(ev.problem)
                                .font(.system(size: 11.5, weight: .semibold))
                            Text(String(ev.sha.prefix(7)))
                                .font(.system(size: 10.5, design: .monospaced))
                                .foregroundStyle(.secondary)
                            if let dr = ev.deltaRaw {
                                Text(String(format: "%+.2f pts", dr))
                                    .font(.system(size: 10.5, weight: .bold, design: .monospaced))
                                    .foregroundStyle(dr >= 0 ? .green : .orange)
                            }
                            if let du = ev.deltaUs {
                                Text(String(format: "%+.2f µs", du))
                                    .font(.system(size: 10.5, weight: .semibold, design: .monospaced))
                                    .foregroundStyle(du <= 0 ? .green : .orange)
                            }
                            if let sp = ev.speedup {
                                Text(String(format: "%.2fx", sp))
                                    .font(.system(size: 10.5, weight: .semibold, design: .monospaced))
                                    .foregroundStyle(.teal)
                            }
                            Spacer()
                            if let ts = ev.recordedAt {
                                Text(Formatters.shortTimestamp(ts))
                                    .font(.system(size: 10, design: .monospaced))
                                    .foregroundStyle(.tertiary)
                            }
                        }
                        if let note = ev.note, !note.isEmpty {
                            Text(note)
                                .font(.system(size: 11))
                                .foregroundStyle(.secondary)
                                .padding(.leading, 4)
                        }
                        Divider()
                    }
                }
            }
        }
    }
}
