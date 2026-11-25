import SwiftUI

struct ProblemInspectorView: View {
    @EnvironmentObject var store: ContestStore
    let problem: ProblemItem
    let gitOwner: String

    private var lang: AppLanguage { store.lang }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                headerRow
                EscapeAndBackfillBannerView(problem: problem)

                if problem.bestScore - problem.currentScore >= 2.5
                    && !problem.bestSha.isEmpty
                    && problem.bestSha.prefix(7) != (problem.activeSha ?? "").prefix(7) {
                    restoreCallout
                }

                if let run = problem.caseRun {
                    referenceBench(run)
                } else {
                    accountingCard
                    StrataAndSlotsCardView(problem: problem)
                    publicRatioCard
                    BenchCoverageCardView(problem: problem)
                }
                FastFeedbackDiagnosticCardView(problem: problem)
                timelineCard
            }
            .padding(14)
        }
        .background(Color(nsColor: .windowBackgroundColor))
    }

    private var headerRow: some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 8) {
                    Text(problem.title).font(.system(size: 16, weight: .bold))
                    TierTagBadge(difficulty: problem.difficulty, text: lang.wsptTag(problem.wsptTag))
                }
                let rk = problem.rank.map { "#\($0)" } ?? "—"
                Text("\(gitOwner)/\(problem.repoSlug) · \(lang.tr("排名", "Rank")) \(rk) / \(problem.totalTeams ?? 0)")
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
            Spacer()
            HStack(spacing: 6) {
                Button {
                    Task { await store.launchOperatorSession(problem: problem.title, mode: "gui") }
                } label: {
                    if store.isLaunchingSession {
                        ProgressView().controlSize(.small)
                    } else {
                        Label(lang.tr("新开对话", "Launch"), systemImage: "plus.bubble.fill")
                            .font(.system(size: 11, weight: .semibold))
                    }
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
                .disabled(store.isLaunchingSession)
                .help(lang.tr(
                    "通过 \(store.activeDriver) 为 \(problem.title) 新开 GUI 调优对话",
                    "Launch \(store.activeDriver) GUI session for \(problem.title)"
                ))

                Button {
                    let fallbackBase = (store.snapshot?.contest.contestUrl ?? "https://judge.invalid").trimmingCharacters(in: CharacterSet(charactersIn: "/"))
                    let url = problem.problemUrl ?? "\(fallbackBase)/\(problem.slug)"
                    SystemNavigator.openURL(url)
                } label: { Image(systemName: "globe") }
                .help(lang.tr("在 Judge 打开赛题", "Open on Judge"))

                Button {
                    SystemNavigator.openURL("https://gitcode.com/\(gitOwner)/\(problem.repoSlug)")
                } label: { Image(systemName: "arrow.up.forward.square") }
                .help(lang.tr("打开 GitCode 仓库", "Open GitCode Repository"))
            }
        }
    }

    private func referenceBench(_ run: CaseRunSummary) -> some View {
        SurfaceCard(padding: 11) {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Text(lang.tr("本地逐例验证", "Local case results")).font(.headline)
                    Text(lang.tr("线上用例", "Judge cases") + " " + (problem.judgeCaseCount.map(String.init) ?? "—"))
                        .font(.caption)
                    Text(String(problem.headSha.prefix(7))).font(.caption.monospaced())
                    Spacer()
                    if let path = run.report {
                        Button {
                            NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
                        } label: { Image(systemName: "folder") }
                        .help(lang.tr("查看运行报告与日志", "Show report and logs"))
                    }
                }
                if let passed = run.passed, let selected = run.selected, let total = run.total {
                    HStack(spacing: 12) {
                        miniStat(run.formal == true ? lang.tr("正式验证", "Formal") : (run.experiment ?? lang.tr("实验", "Experiment")),
                                 "\(passed)/\(selected)", passed == selected ? .green : .orange)
                        miniStat(lang.tr("已选 / 全集", "Selected / total"), "\(selected)/\(total)", .secondary)
                    }
                    if let failures = run.failures, !failures.isEmpty {
                        Text(lang.tr("失败用例 ", "Failed cases ") + failures.joined(separator: ", "))
                            .font(.caption.monospaced()).textSelection(.enabled)
                    }
                } else {
                    Text(lang.tr("当前提交尚无逐例报告", "No case report for this commit")).foregroundStyle(.secondary)
                }
            }
        }
    }

    private var restoreCallout: some View {
        let sha7 = String(problem.bestSha.prefix(7))
        let gain = problem.bestScore - problem.currentScore
        return HStack(spacing: 10) {
            Image(systemName: "clock.arrow.circlepath").foregroundStyle(.orange)
            VStack(alignment: .leading, spacing: 1) {
                Text(lang.tr(
                    String(format: "同代最优版本: %.2f (%@)", problem.bestScore, sha7),
                    String(format: "Equiv Best Validated: %.2f (%@)", problem.bestScore, sha7)
                ))
                .font(.system(size: 11.5, weight: .semibold))
                Text(lang.tr(
                    String(format: "相比当前 %.2f 预计可恢复 +%.2f", problem.currentScore, gain),
                    String(format: "Recoverable +%.2f over current %.2f", gain, problem.currentScore)
                ))
                .font(.system(size: 10.5))
                .foregroundStyle(.secondary)
            }
            Spacer()
            Button(lang.tr("加入恢复队列", "Queue Restore")) {
                Task { await store.restoreBest(problem: problem.title) }
            }
            .buttonStyle(.borderedProminent)
            .tint(.orange)
            .controlSize(.small)
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.orange.opacity(0.10)))
    }

    private var accountingCard: some View {
        SurfaceCard(padding: 11) {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Text(lang.tr("分数构成拆解", "SCORE ACCOUNTING")).font(.system(size: 10, weight: .bold)).foregroundStyle(.secondary)
                    Spacer()
                    if problem.leaderScore > 0 {
                        Text(String(format: "#1 %@: %.2f", problem.leaderNickname, problem.leaderScore))
                            .font(.system(size: 10.5, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }
                }
                HStack(spacing: 12) {
                    miniStat(lang.tr("公开例", "Public") + " (\(problem.testcases.filter { !$0.isHidden }.count))", String(format: "%.2f", problem.publicPts), .blue)
                    miniStat(lang.tr("隐藏例", "Hidden") + " (\(problem.testcases.filter { $0.isHidden }.count))", String(format: "%.2f", problem.hiddenPts), .purple)
                    miniStat(lang.tr("隐藏均比 r̄_h", "Hidden r̄_h"), String(format: "%.1f%%", problem.impliedHiddenRatio * 100), .teal)
                    if let g = problem.tbestDecayGamma {
                        miniStat("tbest γ", String(format: "%.3f", g), g < 0.98 ? .orange : .secondary)
                    }
                }
            }
        }
    }

    private var publicRatioCard: some View {
        SurfaceCard(padding: 11) {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Text(lang.tr("公开测例耗时对标 (tbest / 本队)", "PUBLIC CASES · OURS vs TBEST"))
                        .font(.system(size: 10, weight: .bold)).foregroundStyle(.secondary)
                    Spacer()
                    Text("r = tbest / ours").font(.system(size: 10, design: .monospaced)).foregroundStyle(.secondary)
                }
                if problem.publicTbestUs.isEmpty {
                    Text(lang.tr("暂无公开测例耗时数据", "No public timings recorded."))
                        .font(.system(size: 11)).foregroundStyle(.secondary)
                } else {
                    ForEach(0..<problem.publicTbestUs.count, id: \.self) { i in
                        let tb = problem.publicTbestUs[i]
                        let ours = i < problem.publicTimesUs.count ? problem.publicTimesUs[i] : nil
                        let r = (ours != nil && ours! > 0) ? min(1.0, tb / ours!) : 0.0
                        ratioBar(index: i + 1, tbest: tb, ours: ours, ratio: r)
                    }
                }
            }
        }
    }

    private var timelineCard: some View {
        SurfaceCard(padding: 11) {
            VStack(alignment: .leading, spacing: 8) {
                Text(lang.tr("历史提交与预估残差 (\(problem.history.count))", "SUBMISSION TIMELINE & RESIDUALS (\(problem.history.count))"))
                    .font(.system(size: 10, weight: .bold))
                    .foregroundStyle(.secondary)
                ForEach(problem.history.prefix(15)) { h in
                    HStack(alignment: .top, spacing: 8) {
                        VStack(alignment: .leading, spacing: 2.5) {
                            HStack(spacing: 6) {
                                Text(String(h.sha.prefix(7))).font(.system(size: 11, weight: .semibold, design: .monospaced))
                                SemanticPill(text: lang.evalStatus(h.status), compact: true)
                                if let est = h.estimatedScore, let gap = h.residualGap {
                                    Text(String(format: "%@ %.1f · %@ %+.1f", lang.tr("预估", "est"), est, lang.tr("残差", "gap"), gap))
                                        .font(.system(size: 9.5, weight: .semibold, design: .monospaced))
                                        .foregroundStyle(abs(gap) <= 3.0 ? .teal : .orange)
                                        .padding(.horizontal, 5.5)
                                        .padding(.vertical, 1.5)
                                        .background(Capsule().fill((abs(gap) <= 3.0 ? Color.teal : Color.orange).opacity(0.13)))
                                }
                                Text(Formatters.shortTimestamp(h.at)).font(.system(size: 10, design: .monospaced)).foregroundStyle(.secondary)
                            }
                            if !h.note.isEmpty {
                                Text(h.note).font(.system(size: 10.5)).foregroundStyle(.secondary).lineLimit(2)
                            }
                        }
                        Spacer()
                        VStack(alignment: .trailing, spacing: 2) {
                            if let sc = h.score {
                                Text(String(format: "%.2f", sc)).font(.system(size: 12, weight: .bold, design: .monospaced))
                            }
                            if let d = h.delta {
                                Text(String(format: "%+.2f", d))
                                    .font(.system(size: 10, weight: .semibold, design: .monospaced))
                                    .foregroundStyle(d >= 0 ? .green : .orange)
                            }
                        }
                    }
                    .padding(.vertical, 3)
                    Divider()
                }
            }
        }
    }

    private func miniStat(_ label: String, _ val: String, _ color: Color) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label).font(.system(size: 10)).foregroundStyle(.secondary)
            Text(val).font(.system(size: 12.5, weight: .bold, design: .monospaced)).foregroundStyle(color)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func ratioBar(index: Int, tbest: Double, ours: Double?, ratio: Double) -> some View {
        VStack(spacing: 4) {
            HStack(spacing: 6) {
                Text(String(format: "#%02d", index)).font(.system(size: 11, weight: .semibold, design: .monospaced))
                Spacer()
                Text(String(format: "tbest %.2f µs", tbest)).font(.system(size: 10.5, design: .monospaced)).foregroundStyle(.secondary)
                Text("vs").font(.system(size: 10)).foregroundStyle(.secondary)
                Text(ours.map { String(format: "ours %.2f µs", $0) } ?? "ours —").font(.system(size: 10.5, weight: .semibold, design: .monospaced))
                Text(String(format: "%.0f%%", ratio * 100))
                    .font(.system(size: 10.5, weight: .bold, design: .monospaced))
                    .foregroundStyle(ratio >= 0.7 ? .green : .accentColor)
                    .frame(width: 38, alignment: .trailing)
            }
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.primary.opacity(0.07))
                    Capsule().fill((ratio >= 0.7 ? Color.green : Color.accentColor).gradient).frame(width: max(4, geo.size.width * ratio))
                }
            }
            .frame(height: 5.5)
        }
    }
}
