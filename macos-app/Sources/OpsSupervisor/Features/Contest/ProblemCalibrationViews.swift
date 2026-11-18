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
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "waveform.badge.exclamationmark")
                .foregroundStyle(.orange)
                .padding(.top, 1)

            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 7) {
                    Text(lang.tr(
                        "发现 \(missing) 个新增性能桶缺少基线配对耗时",
                        "\(missing) perf bucket(s) missing baseline pairing"
                    ))
                    .font(.system(size: 11, weight: .medium))

                    if let bf = compact {
                        SemanticPill(text: "B0 · T\(bf.tier)", icon: "bolt.horizontal.fill", compact: true)
                        SemanticPill(
                            text: bf.autoEnabled
                                ? lang.tr("自动调度已开启", "AUTO ON")
                                : lang.tr("自动调度关闭", "AUTO OFF"),
                            compact: true
                        )
                        if bf.running {
                            ProgressView().controlSize(.small).scaleEffect(0.7)
                            SemanticPill(
                                text: lang.tr("补测中 \(bf.runningTitle ?? "—")", "RUNNING \(bf.runningTitle ?? "—")"),
                                compact: true
                            )
                        } else if (bf.queuedCount ?? 0) > 0 {
                            SemanticPill(text: lang.tr("已排队 \(bf.queuedCount ?? 0)", "QUEUED \(bf.queuedCount ?? 0)"), compact: true)
                        } else {
                            SemanticPill(text: lang.tr("调度器待触发", "SCHEDULER IDLE"), compact: true)
                        }
                    } else if let bf = backfill, bf.available {
                        if bf.running.active {
                            ProgressView().controlSize(.small).scaleEffect(0.7)
                            SemanticPill(text: lang.tr("自动补测中", "AUTO BACKFILLING"), compact: true)
                        } else {
                            SemanticPill(text: lang.tr("调度器待触发", "SCHEDULER IDLE"), compact: true)
                        }
                    } else if backfill != nil {
                        SemanticPill(text: lang.tr("自动调度未就绪", "AUTO SCHEDULER N/A"), compact: true)
                    } else {
                        ProgressView().controlSize(.small).scaleEffect(0.7)
                    }
                }

                detailLine(missing: missing)
            }

            Spacer(minLength: 6)

            Button {
                Task { await store.backfillProblem(problem.title) }
            } label: {
                if store.isBackfilling {
                    ProgressView().controlSize(.small).scaleEffect(0.7)
                } else {
                    Text(lang.tr("立即补测 (~20s)", "Backfill now (~20s)"))
                }
            }
            .buttonStyle(.borderedProminent)
            .tint(.orange)
            .controlSize(.mini)
            .disabled(store.isBackfilling)

            Button {
                Task {
                    await store.reloadBackfillStatus(force: true)
                    await store.refreshLocal()
                }
            } label: {
                Image(systemName: "arrow.clockwise")
                    .font(.system(size: 9.5, weight: .semibold))
            }
            .buttonStyle(.bordered)
            .controlSize(.mini)
            .help(lang.tr("刷新自动补测状态", "Refresh auto-backfill status"))
        }
        .padding(9)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.orange.opacity(0.10)))
    }

    @ViewBuilder
    private func detailLine(missing: Int) -> some View {
        if let bf = compact {
            HStack(spacing: 8) {
                Text(lang.tr(
                    "调度器未配对 \(bf.unpairedTotal)（本算子 \(bf.unpairedByOperator[problem.title] ?? missing)）",
                    "scheduler unpaired \(bf.unpairedTotal) (this operator \(bf.unpairedByOperator[problem.title] ?? missing))"
                ))
                .font(.system(size: 10, design: .monospaced))
                .foregroundStyle(.secondary)
                if let q = bf.queuedCount, q > 0 {
                    Text(lang.tr("排队 \(q) 个会话", "queued \(q) session(s)"))
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.secondary)
                }
                if let eta = bf.etaSec {
                    Text(lang.tr("预计 \(fmt(eta))", "ETA \(fmt(eta))"))
                        .font(.system(size: 10, weight: .semibold, design: .monospaced))
                        .foregroundStyle(.orange)
                }
                if let wall = bf.etaWallSec, let eta = bf.etaSec, wall > eta + 1 {
                    Text(lang.tr("含节流 \(fmt(wall))", "w/ throttle \(fmt(wall))"))
                        .font(.system(size: 9.5, design: .monospaced))
                        .foregroundStyle(.tertiary)
                }
                if let run = bf.lastRun, let title = run.title, !title.isEmpty {
                    Text(lang.tr("上次 \(title) \(run.result ?? "")",
                                 "last \(title) \(run.result ?? "")").trimmingCharacters(in: .whitespaces))
                        .font(.system(size: 9.5, design: .monospaced))
                        .foregroundStyle(.tertiary)
                }
                if bf.unpairedTotal == 0 && (bf.queuedCount ?? 0) == 0 {
                    Text(lang.tr("（调度器已无待补测，校准缓存可能滞后）",
                                 "(scheduler reports none left; calibration cache may lag)"))
                        .font(.system(size: 9.5))
                        .foregroundStyle(.tertiary)
                }
                Spacer()
            }
        } else if let bf = backfill, bf.available {
            HStack(spacing: 8) {
                Text(lang.tr("调度器未配对 \(bf.unpairedCount) · 排队 \(bf.queuedCount)",
                             "scheduler unpaired \(bf.unpairedCount) · queued \(bf.queuedCount)"))
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
                if let eta = bf.etaSec {
                    Text(lang.tr("预计 \(Int(eta))s", "ETA \(Int(eta))s"))
                        .font(.system(size: 10, weight: .semibold, design: .monospaced))
                        .foregroundStyle(.orange)
                }
                Spacer()
            }
        } else {
            HStack(spacing: 6) {
                Text(lang.tr(
                    "自动补测状态不可用，已降级为手动补测；不影响其它功能。",
                    "Auto-backfill status unavailable; manual override still works."
                ))
                .font(.system(size: 10))
                .foregroundStyle(.secondary)
                if let reason = backfill?.reason, !reason.isEmpty {
                    Text("[\(reason)]")
                        .font(.system(size: 9, design: .monospaced))
                        .foregroundStyle(.tertiary)
                }
                Spacer()
            }
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
                    HStack(spacing: 6) {
                        Text(lang.stratumLabel(item.stratum))
                            .font(.system(size: 11, weight: .semibold))
                        Text(drifted
                             ? String(format: "w %.0f%%→%.0f%%", item.priorW * 100, item.learnedW * 100)
                             : String(format: "ŵ %.1f%%", item.learnedW * 100))
                            .font(.system(size: 9.5, weight: .medium, design: .monospaced))
                            .foregroundStyle(drifted ? .orange : .secondary)
                        Spacer()
                        Text(String(format: "r̂ %.0f%%", item.efficiencyR * 100))
                            .font(.system(size: 10, design: .monospaced))
                            .foregroundStyle(.secondary)
                        Text(String(format: "%.1f / %.1f", item.currentPts, maxPts))
                            .font(.system(size: 10.5, weight: .semibold, design: .monospaced))
                        Text(String(format: "+%.1f", item.headroomPts))
                            .font(.system(size: 10.5, weight: .bold, design: .monospaced))
                            .foregroundStyle(item.headroomPts >= 12 ? Color(red: 0.88, green: 0.46, blue: 0.08) : Color(red: 0.10, green: 0.72, blue: 0.42))
                            .frame(width: 44, alignment: .trailing)
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
