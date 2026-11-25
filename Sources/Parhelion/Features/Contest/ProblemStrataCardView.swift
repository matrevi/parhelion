import SwiftUI

struct StrataAndSlotsCardView: View {
    @EnvironmentObject var store: ContestStore
    let problem: ProblemItem
    @UIState private var mode: Int = 0
    @UIState private var expandedGroups: Set<Int> = []

    private var lang: AppLanguage { store.lang }
    private var strata: [StratumCalibrationItem] { problem.calibration?.strata ?? [] }

    var body: some View {
        SurfaceCard(padding: 11) {
            VStack(alignment: .leading, spacing: 9) {
                HStack {
                    Picker("", selection: $mode) {
                        Text(lang.tr("5层校准权重与挖潜", "Calibrated Strata (5)")).tag(0)
                        Text(lang.tr("测例槽位", "Case Slots")).tag(1)
                    }
                    .pickerStyle(.segmented)
                    .controlSize(.small)
                    .frame(width: 245)

                    Spacer()

                    if let mae = problem.calibration?.maePts {
                        Text(String(format: "MAE ±%.2f", mae))
                            .font(.system(size: 10, weight: .semibold, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }
                }

                if mode == 0 {
                    strataBarsView
                } else {
                    caseSlotsDrawerView
                }
            }
        }
        // Reset per-problem slot expansion so opening a drawer on Problem A never leaks into Problem B.
        .onChange(of: problem.title) { _, _ in
            expandedGroups.removeAll()
        }
    }

    private var strataBarsView: some View {
        VStack(spacing: 8) {
            ForEach(strata) { item in
                let maxPts = max(0.1, 100.0 * item.learnedW)
                let fillRatio = min(1.0, max(0.0, item.currentPts / maxPts))
                let drifted = abs(item.learnedW - item.priorW) >= 0.015

                VStack(alignment: .leading, spacing: 3.5) {
                    HStack(spacing: 8) {
                        Text(lang.stratumLabel(item.stratum))
                            .font(.system(size: 11.5, weight: .semibold))
                            .lineLimit(1)
                            .frame(width: 92, alignment: .leading)
                        Text(drifted
                             ? String(format: "w %.0f→%.0f%%", item.priorW * 100, item.learnedW * 100)
                             : String(format: "ŵ %.1f%%", item.learnedW * 100))
                            .font(.system(size: 9.5, weight: .medium, design: .monospaced))
                            .foregroundStyle(drifted ? .orange : .secondary)
                            .lineLimit(1)
                            .frame(width: 66, alignment: .leading)
                        Spacer(minLength: 4)
                        Text(String(format: "r̂ %.0f%%", item.efficiencyR * 100))
                            .font(.system(size: 10, design: .monospaced))
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .fixedSize()
                            .frame(width: 46, alignment: .trailing)
                        Text(String(format: "%.1f / %.1f", item.currentPts, maxPts))
                            .font(.system(size: 10.5, weight: .semibold, design: .monospaced))
                            .lineLimit(1)
                            .fixedSize()
                            .frame(width: 76, alignment: .trailing)
                        Text(String(format: "+%.1f", item.headroomPts))
                            .font(.system(size: 10.5, weight: .bold, design: .monospaced))
                            .foregroundStyle(item.headroomPts >= 12 ? Color(red: 0.88, green: 0.46, blue: 0.08) : Color(red: 0.10, green: 0.72, blue: 0.42))
                            .lineLimit(1)
                            .fixedSize()
                            .frame(width: 48, alignment: .trailing)
                    }

                    GeometryReader { geo in
                        ZStack(alignment: .leading) {
                            Capsule().fill(Color.primary.opacity(0.07))
                            Capsule()
                                .fill((fillRatio >= 0.65 ? Color.green : Color.accentColor).gradient)
                                .frame(width: max(4, geo.size.width * fillRatio))
                        }
                    }
                    .frame(height: 5.5)
                }
            }
        }
    }

    private var caseSlotsDrawerView: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack {
                Spacer()
                Button(expandedGroups.isEmpty ? lang.tr("全部展开", "Expand All") : lang.tr("全部收起", "Fold All")) {
                    expandedGroups = expandedGroups.isEmpty ? Set(0..<((problem.testcases.count + 9) / 10)) : []
                }
                .buttonStyle(.link)
                .font(.system(size: 10.5))
            }
            ForEach(0..<((problem.testcases.count + 9) / 10), id: \.self) { g in
                let s = g * 10
                let e = min(problem.testcases.count, s + 10)
                if s < e {
                    let slice = Array(problem.testcases[s..<e])
                    let pubCnt = slice.filter { !$0.isHidden }.count
                    DisclosureGroup(
                        isExpanded: Binding(
                            get: { expandedGroups.contains(g) },
                            set: { v in if v { expandedGroups.insert(g) } else { expandedGroups.remove(g) } }
                        )
                    ) {
                        VStack(spacing: 3) {
                            ForEach(slice) { tc in
                                HStack(spacing: 8) {
                                    Text(String(format: "#%02d", tc.index))
                                        .font(.system(size: 10.5, weight: .semibold, design: .monospaced))
                                        .frame(width: 28, alignment: .leading)
                                    SemanticPill(text: tc.isHidden ? lang.tr("隐藏", "Hidden") : lang.tr("公开", "Public"), compact: true)
                                    Spacer()
                                    Text(tc.tbestUs.map { String(format: "tbest %.2f µs", $0) }
                                         ?? String(format: "%@ · r̄_h %.2f", lang.tr("未公开", "Masked"), problem.impliedHiddenRatio))
                                        .font(.system(size: 10, design: .monospaced))
                                        .foregroundStyle(tc.tbestUs != nil ? .primary : .secondary)
                                    if let o = tc.oursUs {
                                        Text(String(format: "ours %.2f µs", o))
                                            .font(.system(size: 10.5, weight: .medium, design: .monospaced))
                                            .foregroundStyle(Color.accentColor)
                                            .frame(width: 82, alignment: .trailing)
                                    }
                                }
                                .padding(.vertical, 2)
                            }
                        }
                        .padding(.top, 4)
                    } label: {
                        HStack {
                            Text(lang.tr(String(format: "测例 #%02d – #%02d", s + 1, e), String(format: "Cases #%02d – #%02d", s + 1, e)))
                                .font(.system(size: 11, weight: .medium, design: .monospaced))
                            Spacer()
                            Text(pubCnt > 0
                                 ? lang.tr("\(pubCnt) 公开 · \(slice.count - pubCnt) 隐藏", "\(pubCnt) Public · \(slice.count - pubCnt) Hidden")
                                 : lang.tr("10 隐藏测例", "10 Hidden"))
                                .font(.system(size: 10))
                                .foregroundStyle(.secondary)
                        }
                    }
                    .padding(.horizontal, 8)
                    .padding(.vertical, 3.5)
                    .background(RoundedRectangle(cornerRadius: 6).fill(Color.primary.opacity(0.03)))
                }
            }
        }
    }
}
