import SwiftUI

struct FastFeedbackBodyView: View {
    @EnvironmentObject var store: ContestStore
    let problem: ProblemItem
    let card: PmuTorchCard?
    let elfFallback: ElfGuardResult?
    let cold3Fallback: Cold3Result?

    private var lang: AppLanguage { store.lang }

    private var elfStatus: String { card?.elfGuard?.status ?? elfFallback?.status ?? "ok" }
    private var elfMetrics: ElfGuardMetrics? { card?.elfGuard?.metrics ?? elfFallback?.metrics }
    private var cold3Status: String { card?.cold3?.status ?? cold3Fallback?.status ?? "ok" }
    private var cold3Metrics: Cold3Metrics? { card?.cold3?.metrics ?? cold3Fallback?.metrics }
    private var combinedAlerts: [FastAlert] {
        (card?.elfGuard?.alerts ?? elfFallback?.alerts ?? [])
            + (card?.cold3?.alerts ?? cold3Fallback?.alerts ?? [])
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            statusRow
            elfAndCold3Grid
            if let card {
                pmuPipeSection(card.pmu)
                if !card.topBuckets.isEmpty {
                    topBucketsSection(card.topBuckets)
                }
                if !card.suggestedKbTags.isEmpty {
                    suggestedTagsRow(card.suggestedKbTags)
                }
            }
            if !combinedAlerts.isEmpty {
                alertsSection(combinedAlerts)
            }
        }
    }

    private var statusRow: some View {
        HStack(spacing: 6) {
            if let bn = card?.dominantBottleneck {
                SemanticPill(text: bn, icon: "waveform.path.ecg", compact: true)
            }
            SemanticPill(text: "ELF: \(elfStatus.uppercased())", compact: true)
            SemanticPill(text: "COLD3: \(cold3Status.uppercased())", compact: true)
            if let soc = card?.runtimeSoc, !soc.isEmpty {
                Text(soc)
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
            Spacer()
            let sha = card?.sha ?? elfFallback?.sha ?? ""
            if !sha.isEmpty {
                Text(String(sha.prefix(7)))
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.tertiary)
            }
        }
    }

    private var elfAndCold3Grid: some View {
        HStack(spacing: 8) {
            if let em = elfMetrics {
                let txtKb = em.kernelOMaxTextBytes.map { String(format: "%.1f KB", Double($0) / 1024.0) } ?? "—"
                let objKb = em.kernelOMaxBytes.map { String(format: "%.1f KB", Double($0) / 1024.0) } ?? "—"
                let buildS = em.phaseACpuSec.map { String(format: "%.1fs", $0) } ?? "—"
                let tmplS = em.templateInstantiationCount.map { "\($0)" } ?? "—"
                diagnosticBox(
                    title: lang.tr("ELF & I-Cache 红线", "ELF & I-Cache"),
                    items: [
                        (".text max", txtKb),
                        (".o max", objKb),
                        ("Build CPU", buildS),
                        ("Templates", tmplS)
                    ]
                )
            }
            if let cm = cold3Metrics {
                let firstS = cm.firstIterUs.map { String(format: "%.2f µs", $0) } ?? "—"
                let mean3S = cm.cold3RunMeanUs.map { String(format: "%.2f µs", $0) } ?? "—"
                let warmS = cm.warmMedianUs.map { String(format: "%.2f µs", $0) } ?? "—"
                let taxS = cm.coldTaxUs.map { String(format: "%+.2f µs", $0) } ?? "—"
                diagnosticBox(
                    title: cm.bimodalSpikeDetected
                        ? lang.tr("Cold-3 (存在 DVFS 尖峰!)", "Cold-3 (DVFS Spike!)")
                        : lang.tr("Cold-3 冷启动探针", "Cold-3 Probe"),
                    items: [
                        ("1st Iter", firstS),
                        ("3-Run Mean", mean3S),
                        ("Warm Med", warmS),
                        ("Cold Tax", taxS)
                    ]
                )
            }
        }
    }

    private func diagnosticBox(title: String, items: [(String, String)]) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(title)
                .font(.system(size: 10, weight: .bold))
                .foregroundStyle(.secondary)
            LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], alignment: .leading, spacing: 4) {
                ForEach(items, id: \.0) { label, val in
                    VStack(alignment: .leading, spacing: 1) {
                        Text(label).font(.system(size: 9.5)).foregroundStyle(.tertiary)
                        Text(val).font(.system(size: 11, weight: .bold, design: .monospaced))
                    }
                }
            }
        }
        .padding(8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 7, style: .continuous).fill(Color.primary.opacity(0.035)))
    }

    private func pmuPipeSection(_ pmu: PmuSummaryMetrics) -> some View {
        let pipes: [(String, Double?)] = [
            ("VEC", pmu.aivVecRatio),
            ("MTE2", pmu.aivMte2Ratio),
            ("MTE3", pmu.aivMte3Ratio),
            ("SCALAR", pmu.aivScalarRatio),
            ("BANK", pmu.bankConflictRatio)
        ]
        return HStack(spacing: 8) {
            ForEach(pipes, id: \.0) { label, val in
                VStack(alignment: .leading, spacing: 2) {
                    Text(label)
                        .font(.system(size: 9, weight: .semibold, design: .monospaced))
                        .foregroundStyle(.secondary)
                    Text(val.map { String(format: "%.0f%%", $0 * 100) } ?? "—")
                        .font(.system(size: 11, weight: .bold, design: .monospaced))
                        .foregroundStyle((val ?? 0) >= 0.35 && label == "SCALAR" ? .orange : .primary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 6)
        .background(RoundedRectangle(cornerRadius: 7, style: .continuous).fill(Color.primary.opacity(0.03)))
    }

    private func topBucketsSection(_ rows: [PmuBucketRow]) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(lang.tr("高潜性能桶 (Ours vs Torch)", "TOP HEADROOM BUCKETS (OURS vs TORCH)"))
                .font(.system(size: 9.5, weight: .bold))
                .foregroundStyle(.secondary)
            ForEach(rows.prefix(5)) { row in
                HStack(spacing: 6) {
                    Text(row.bucket)
                        .font(.system(size: 10.5, weight: .semibold, design: .monospaced))
                        .lineLimit(1)
                    Spacer()
                    Text(String(format: "%.2fµs", row.oursUs))
                        .font(.system(size: 10, design: .monospaced))
                    Text(row.torchUs.map { String(format: "/ %.2fµs", $0) } ?? "")
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.secondary)
                    if let r = row.ratioVsTorch {
                        Text(String(format: "%.2fx", r))
                            .font(.system(size: 10, weight: .bold, design: .monospaced))
                            .foregroundStyle(r > 1.1 ? .orange : .green)
                            .frame(width: 44, alignment: .trailing)
                    }
                    Text(String(format: "H %.3f", row.headroomScore))
                        .font(.system(size: 9.5, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .frame(width: 48, alignment: .trailing)
                }
            }
        }
    }

    private func suggestedTagsRow(_ tags: [String]) -> some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 5) {
                Text("KB:")
                    .font(.system(size: 9.5, weight: .bold))
                    .foregroundStyle(.secondary)
                ForEach(tags.prefix(6), id: \.self) { tag in
                    Text("#\(tag)")
                        .font(.system(size: 9.5, weight: .medium, design: .monospaced))
                        .foregroundStyle(.teal)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(Capsule().fill(Color.teal.opacity(0.12)))
                }
            }
        }
    }

    private func alertsSection(_ alerts: [FastAlert]) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            ForEach(alerts.prefix(4)) { al in
                HStack(alignment: .top, spacing: 6) {
                    SemanticPill(text: al.severity.uppercased(), compact: true)
                    Text(al.message)
                        .font(.system(size: 10.5))
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }
}
