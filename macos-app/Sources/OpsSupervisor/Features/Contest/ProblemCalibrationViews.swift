import SwiftUI

struct EscapeAndBackfillBannerView: View {
    @EnvironmentObject var store: ContestStore
    let problem: ProblemItem

    private var lang: AppLanguage { store.lang }
    private var cal: ProblemCalibration? { problem.calibration }
    private var backfill: BackfillStatusReport? { store.backfillStatus }

    var body: some View {
        VStack(spacing: 8) {
            if cal?.escapeLock == true {
                HStack(spacing: 10) {
                    Image(systemName: "lock.trianglebadge.exclamationmark.fill")
                        .font(.system(size: 16))
                        .foregroundStyle(.red)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(lang.tr("逃逸硬锁已触发 (ESCAPE LOCK)", "ESCAPE LOCK ACTIVE"))
                            .font(.system(size: 11.5, weight: .bold))
                            .foregroundStyle(.red)
                        Text(lang.tr(
                            "上一发通过本地检查但线上 WA/TLE/RE，请向 cases_correctness.json 补充复现例并执行 verify-escape 解锁。",
                            "Online WA/TLE/RE escaped local checks. Add a D5 reproducer & run `opsb bench verify-escape`."
                        ))
                        .font(.system(size: 10.5))
                        .foregroundStyle(.secondary)
                    }
                    Spacer()
                }
                .padding(10)
                .background(RoundedRectangle(cornerRadius: 8).fill(Color.red.opacity(0.11)))
            }

            if let missing = cal?.unpairedBucketCount, missing > 0 {
                unpairedBanner(missing: missing)
            }
        }
    }

    /// Orange banner: only rendered while this operator actually has unpaired buckets.
    /// Primary source is the free `scheduler.backfill` passthrough that ships inside
    /// `opsb status --json`; the richer `opsb bench backfill-status` probe is only a fallback
    /// for older CLIs. Missing data degrades to an explicit muted row — never blank, never a crash.
    private func unpairedBanner(missing: Int) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(spacing: 8) {
                Image(systemName: "waveform.badge.exclamationmark")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(.orange)

                Text(lang.tr("\(missing) 个性能桶缺少基线配对", "\(missing) bucket(s) missing baseline"))
                    .font(.system(size: 12, weight: .semibold))
                    .lineLimit(1)
                    .layoutPriority(2)

                if let bf = compact, bf.autoEnabled {
                    Text(lang.tr("B0 自动", "B0 auto"))
                        .font(.system(size: 9.5, weight: .bold, design: .monospaced))
                        .foregroundStyle(.orange)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(Capsule().fill(Color.orange.opacity(0.16)))
                        .lineLimit(1)
                        .fixedSize()
                }
                if let bf = compact, bf.running {
                    ProgressView().controlSize(.small).scaleEffect(0.65)
                }

                Spacer(minLength: 8)

                Button {
                    Task { await store.backfillProblem(problem.title) }
                } label: {
                    if store.isBackfilling {
                        ProgressView().controlSize(.small).scaleEffect(0.7)
                    } else {
                        Text(lang.tr("立即补测 (~20s)", "Backfill (~20s)"))
                            .font(.system(size: 11, weight: .medium))
                            .lineLimit(1)
                    }
                }
                .buttonStyle(.borderedProminent)
                .tint(.orange)
                .controlSize(.small)
                .disabled(store.isBackfilling)

                Button {
                    Task {
                        await store.reloadBackfillStatus(force: true)
                        await store.refreshLocal()
                    }
                } label: {
                    Image(systemName: "arrow.clockwise")
                        .font(.system(size: 10, weight: .semibold))
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .help(lang.tr("刷新自动补测状态", "Refresh auto-backfill status"))
            }

            metricsLine(missing: missing)
                .lineLimit(1)
        }
        .padding(.horizontal, 11)
        .padding(.vertical, 9)
        .background(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(Color.orange.opacity(0.10))
                .overlay(
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .strokeBorder(Color.orange.opacity(0.22), lineWidth: 1)
                )
        )
    }

    /// One monospaced line, no pills, no character wrapping: segments are concatenated so
    /// the tail truncates cleanly on narrow windows instead of squeezing every token.
    @ViewBuilder
    private func metricsLine(missing: Int) -> some View {
        if let bf = compact {
            let mine = bf.unpairedByOperator[problem.title] ?? missing
            let eta = bf.etaSec.map(fmt)
            let wall = bf.etaWallSec.map(fmt)
            let lastRun = bf.lastRun?.title
            Text("")
                .font(.system(size: 10, design: .monospaced)) +
            Text(lang.tr("未配对 \(bf.unpairedTotal)", "unpaired \(bf.unpairedTotal)"))
                .foregroundColor(.secondary) +
            Text(lang.tr("（本算子 \(mine)）", " (this \(mine))"))
                .foregroundColor(.secondary) +
            Text(bf.queuedCount.map { lang.tr("  ·  排队 \($0) 会话", "  ·  queued \($0)") } ?? "")
                .foregroundColor(.secondary) +
            Text(eta.map { lang.tr("  ·  ETA \($0)", "  ·  ETA \($0)") } ?? "")
                .foregroundColor(.orange) +
            Text((wall != nil && eta != nil && wall != eta)
                    ? lang.tr("  ·  节流后 \(wall!)", "  ·  w/ throttle \(wall!)") : "")
                .foregroundColor(.orange.opacity(0.65)) +
            Text(lastRun.map { lang.tr("  ·  上次 \($0)", "  ·  last \($0)") } ?? "")
                .foregroundColor(.secondary.opacity(0.8)) +
            Text(bf.runningTitle.map { lang.tr("  ·  正在跑 \($0)", "  ·  running \($0)") } ?? "")
                .foregroundColor(.orange)
        } else if let bf = backfill, bf.available {
            Text(lang.tr("调度器未配对 \(bf.unpairedCount)  ·  排队 \(bf.queuedCount)",
                         "unpaired \(bf.unpairedCount)  ·  queued \(bf.queuedCount)"))
                .font(.system(size: 10, design: .monospaced))
                .foregroundStyle(.secondary)
                .lineLimit(1)
        } else {
            Text(lang.tr("自动补测状态不可用，可手动补测；不影响其它功能。",
                         "Auto-backfill status unavailable; manual override still works."))
                .font(.system(size: 10))
                .foregroundStyle(.secondary)
                .lineLimit(1)
        }
    }

    private var compact: BackfillPassthrough? { store.snapshot?.scheduler.backfill }

    private func fmt(_ seconds: Double) -> String {
        if seconds >= 90 {
            return String(format: "%.1f min", seconds / 60.0)
        }
        return String(format: "%.0f s", seconds)
    }
}

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
                        Text(lang.tr("50 测例槽位", "50 Slots")).tag(1)
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
                    slots50DrawerView
                }
            }
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

    private var slots50DrawerView: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack {
                Spacer()
                Button(expandedGroups.isEmpty ? lang.tr("全部展开", "Expand All") : lang.tr("全部收起", "Fold All")) {
                    expandedGroups = expandedGroups.isEmpty ? Set(0..<5) : []
                }
                .buttonStyle(.link)
                .font(.system(size: 10.5))
            }
            ForEach(0..<5, id: \.self) { g in
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

struct BenchCoverageCardView: View {
    @EnvironmentObject var store: ContestStore
    let problem: ProblemItem

    private var lang: AppLanguage { store.lang }

    var body: some View {
        if let grid = problem.calibration?.coverageGrid, !grid.rows.isEmpty {
            SurfaceCard(padding: 11) {
                VStack(alignment: .leading, spacing: 7) {
                    HStack {
                        Text(lang.tr("对抗精度集覆盖矩阵 (6×5 几何 × 分布)", "BENCH COVERAGE MATRIX (6×5 GEOM × DIST)"))
                            .font(.system(size: 10, weight: .bold))
                            .foregroundStyle(.secondary)
                        Spacer()
                        Button {
                            Task { await store.recalibrateProblem(problem.title) }
                        } label: {
                            Label(lang.tr("校准权重", "Calibrate"), systemImage: "slider.horizontal.2.gobackward")
                                .font(.system(size: 10))
                        }
                        .buttonStyle(.bordered)
                        .controlSize(.mini)
                    }

                    Grid(horizontalSpacing: 4, verticalSpacing: 4) {
                        GridRow {
                            Text("")
                                .frame(width: 78, alignment: .leading)
                            ForEach(grid.cols, id: \.self) { col in
                                Text(lang.distLabel(col))
                                    .font(.system(size: 9, weight: .semibold))
                                    .foregroundStyle(.secondary)
                                    .frame(maxWidth: .infinity)
                            }
                        }
                        ForEach(0..<grid.rows.count, id: \.self) { ri in
                            GridRow {
                                Text(lang.geomLabel(grid.rows[ri]))
                                    .font(.system(size: 9.5, weight: .medium, design: .monospaced))
                                    .foregroundStyle(.secondary)
                                    .frame(width: 78, alignment: .leading)
                                ForEach(0..<grid.cols.count, id: \.self) { ci in
                                    let val = (ri < grid.cells.count && ci < grid.cells[ri].count) ? grid.cells[ri][ci] : 0
                                    RoundedRectangle(cornerRadius: 3)
                                        .fill(cellColor(val))
                                        .frame(height: 13)
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    private func cellColor(_ state: Int) -> Color {
        switch state {
        case 2: return Color(red: 0.12, green: 0.75, blue: 0.46).opacity(0.82)
        case 1: return Color(red: 0.94, green: 0.62, blue: 0.12).opacity(0.68)
        default: return Color.primary.opacity(0.08)
        }
    }
}
