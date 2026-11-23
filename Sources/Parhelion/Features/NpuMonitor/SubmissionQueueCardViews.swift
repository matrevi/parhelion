import SwiftUI

struct CandidateSubmissionCardView: View {
    @EnvironmentObject var store: ContestStore
    @EnvironmentObject var hotStore: HotTelemetryStore
    let item: SubmissionQueueItem
    let rankIndex: Int
    @ObservedObject var clock: WallClockTicker

    private var lang: AppLanguage { store.lang }
    private var pipeline: LivePipelineState? { hotStore.pipeline ?? store.snapshot?.scheduler.pipeline }
    private var isWaitingCooldown: Bool { pipeline?.phase == "waiting_cooldown" }
    private var isLocalUploading: Bool { ((pipeline?.isLocalBusy ?? false) && !isWaitingCooldown) || store.isLaunchingSubmit }

    var body: some View {
        let isCrit = item.badge == "CRITICAL RESTORE"
        let expDelta = (item.estimatedScore ?? item.currentScore) - item.currentScore
        let noiseFloor = store.snapshot?.scheduler.noiseFloorDelta ?? 1.9
        let exploreOn = (item.gates.exploreEligible ?? false) || item.badge.uppercased().contains("EXPLORE")
        let ucbLabel: String? = {
            guard let ucb = item.gates.ucbDelta, item.estimatedScore != nil else { return nil }
            guard (ucb - expDelta) >= 0.8, (exploreOn || ucb >= noiseFloor) else { return nil }
            return String(format: "%@ %+.1f", lang.tr("乐观", "UCB"), ucb)
        }()
        let cdRem = clock.cooldownRemaining(endsAt: pipeline?.cooldownEndsEpoch ?? hotStore.schedulerHot?.cooldownEndsEpoch ?? store.snapshot?.scheduler.cooldownEndsEpoch)

        SurfaceCard(highlighted: rankIndex == 1 || isCrit, highlightColor: isCrit ? .red : .accentColor, padding: 11) {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 7) {
                    Text("#\(rankIndex)")
                        .font(.system(size: 11, weight: .heavy, design: .monospaced))
                        .foregroundStyle(rankIndex == 1 ? .white : .secondary)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(Capsule().fill(rankIndex == 1 ? Color.accentColor : Color.primary.opacity(0.08)))

                    DifficultyBadge(difficulty: item.difficulty)
                    Text(item.title).font(.system(size: 13, weight: .bold))
                    Text(item.shortSha).font(.system(size: 11, design: .monospaced)).foregroundStyle(.secondary)
                    Text("· \(Formatters.shortTimestamp(item.queuedAt))")
                        .font(.system(size: 10.5, design: .monospaced))
                        .foregroundStyle(.secondary)
                    Spacer()
                    if let btd = item.breakthroughDelta, btd >= store.breakthroughThreshold {
                        SemanticPill(
                            text: String(format: "%@ %+.2f", lang.tr("预计突破", "BREAKTHROUGH"), btd),
                            icon: "flame.fill",
                            compact: true
                        )
                    }
                    SemanticPill(text: readableCandidateBadge(item))
                }

                HStack(spacing: 8) {
                    QueueScoreCell(
                        title: lang.tr("提交前分数", "Before"),
                        main: String(format: "%.2f", item.currentScore),
                        delta: nil,
                        mainColor: item.currentScore == 0 ? .red : .primary
                    )
                    QueueArrowSeparator()
                    QueueScoreCell(
                        title: lang.tr("预计得分", "Estimated"),
                        main: item.estimatedScore.map { String(format: "~%.2f", $0) } ?? "—",
                        delta: item.estimatedScore != nil ? expDelta : nil,
                        mainColor: .blue,
                        ucbLabel: ucbLabel,
                        ucbHighlighted: exploreOn
                    )
                    QueueArrowSeparator()
                    QueueScoreCell(
                        title: lang.tr("评测结果", "Judged"),
                        main: lang.tr("待提交", "Pending"),
                        delta: nil,
                        mainColor: .secondary
                    )
                }
                .padding(.vertical, 6)
                .padding(.horizontal, 10)
                .background(RoundedRectangle(cornerRadius: 7).fill(Color.primary.opacity(0.035)))

                if !item.note.isEmpty {
                    Text(cleanNote(item.note))
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }

                HStack(spacing: 8) {
                    Text(gateSummaryText(item))
                        .font(.system(size: 10.5, weight: .medium))
                        .foregroundStyle(gateSummaryColor(item))

                    Spacer()

                    if item.canSkip {
                        Button(lang.tr("忽略", "Skip")) {
                            Task { await store.skipCandidate(problem: item.title, sha: item.sha) }
                        }
                        .buttonStyle(.bordered)
                        .controlSize(.small)
                    }

                    Button {
                        Task { await store.jumpSubmitNow(problem: item.title, sha: item.sha, note: item.note) }
                    } label: {
                        if isLocalUploading {
                            Label(lang.tr("打包上传中，请稍候", "Uploading, please wait"), systemImage: "hourglass")
                                .font(.system(size: 11.5, weight: .semibold))
                        } else if isWaitingCooldown {
                            Label(
                                cdRem > 0
                                    ? lang.tr("替换预约 (\(cdRem)s)", "Replace (\(cdRem)s)")
                                    : lang.tr("替换并提交", "Replace & Submit"),
                                systemImage: "arrow.triangle.2.circlepath"
                            )
                            .font(.system(size: 11.5, weight: .semibold))
                        } else if rankIndex == 1 {
                            Label(
                                cdRem > 0
                                    ? lang.tr("预约提交 (冷却 \(cdRem)s)", "Schedule (\(cdRem)s)")
                                    : lang.tr("立即提交", "Submit Now"),
                                systemImage: "paperplane.fill"
                            )
                            .font(.system(size: 11.5, weight: .semibold))
                        } else {
                            Label(
                                cdRem > 0
                                    ? lang.tr("插队预约 (\(cdRem)s)", "Jump (\(cdRem)s)")
                                    : lang.tr("插队提交", "Jump Queue"),
                                systemImage: "bolt.fill"
                            )
                            .font(.system(size: 11.5, weight: .semibold))
                        }
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(isCrit ? .red : .accentColor)
                    .controlSize(.small)
                    .disabled(isLocalUploading || item.badge.uppercased().contains("ESCAPE") || !item.gates.safetyOk)
                }
            }
        }
    }

    private func readableCandidateBadge(_ item: SubmissionQueueItem) -> String {
        let u = item.badge.uppercased()
        if u.contains("ESCAPE") { return lang.tr("逃逸硬锁 · 禁止提交", "ESCAPE LOCK") }
        if u.contains("REJECTED") { return lang.tr("门禁拒绝 · REJECTED", "REJECTED") }
        if u.contains("CRITICAL") { return lang.tr("榜上0分 · 建议恢复", "CRITICAL RESTORE") }
        if u.contains("CIRCUIT") { return lang.tr("连6发无突破", "CIRCUIT OPEN") }
        if u.contains("HOLD") || u.contains("LOW") { return lang.tr("提分较小 · 可插队", "HOLD · LOW GAIN") }
        if u.contains("RATE") { return lang.tr("冷却等待 · RATE WAIT", "RATE WAIT") }
        if !item.gates.allPass { return lang.tr("门禁未通过 · HELD", "GATE HELD") }
        if u.contains("EXPLORE") { return lang.tr("探索通道 · EXPLORE", "EXPLORE") }
        return lang.tr("待提交 · READY", "READY")
    }

    private func gateSummaryColor(_ item: SubmissionQueueItem) -> Color {
        let u = item.badge.uppercased()
        if u.contains("ESCAPE") || u.contains("REJECTED") || !item.gates.safetyOk { return .red }
        if u.contains("CRITICAL") { return Color(red: 0.12, green: 0.70, blue: 0.42) }
        if !item.gates.allPass || u.contains("RATE") { return .orange }
        return Color(red: 0.12, green: 0.70, blue: 0.42)
    }

    private func gateSummaryText(_ item: SubmissionQueueItem) -> String {
        let u = item.badge.uppercased()
        if u.contains("ESCAPE") {
            return lang.tr(
                "🔒 逃逸硬锁激活：需先复现线上 WA/TLE 并通过 verify-escape 解锁",
                "🔒 ESCAPE LOCK: add reproducer & pass verify-escape before submitting"
            )
        }
        if !item.gates.safetyOk { return lang.tr("⚠ 缺少完整服务端精度测试记录", "⚠ Missing NPU extended validation") }
        if u.contains("CRITICAL") { return lang.tr("✓ 历史高分验证通过，可立即恢复", "✓ Validated best SHA ready to restore") }
        if u.contains("REJECTED") {
            return lang.tr("⛔ 门禁拒绝提交（收益/熔断/配额未通过）", "⛔ Rejected by submission gate")
        }
        if !item.gates.circuitOk {
            return lang.tr("⚠ 12h 连续未突破（熔断开启 · 请切换基线或插队覆盖）", "⚠ Circuit breaker open (switch baseline or override)")
        }
        if !item.gates.noiseOk {
            return lang.tr("✓ 精度通过 · 预计涨幅低于自动阈值 (可手动插队)", "✓ Precision OK · Below noise floor (Jump Queue allowed)")
        }
        if !item.gates.quotaOk {
            return lang.tr("⚠ 今日常规额度已用尽（保留收官恢复配额）", "⚠ Daily normal quota exhausted (reserve protected)")
        }
        if u.contains("RATE") {
            return lang.tr("⏳ 4项门禁通过 · 等待 190s 平台冷却就绪", "⏳ All 4 gates passed · Waiting 190s cooldown")
        }
        if !item.gates.allPass {
            return lang.tr("⚠ 未通过全部提交门禁", "⚠ Not all gate stages passed")
        }
        return lang.tr("✓ 本地精度与防抖门禁全部通过", "✓ All 4 gate stages passed")
    }
}

struct CompletedSubmissionCardView: View {
    @EnvironmentObject var store: ContestStore
    let item: SubmissionQueueItem
    let experience: BreakthroughEntry?
    let onInspectExperience: (BreakthroughEntry) -> Void

    private var lang: AppLanguage { store.lang }
    private var isLocalUploading: Bool { (store.snapshot?.scheduler.pipeline?.isLocalBusy ?? false) || store.isLaunchingSubmit }

    var body: some View {
        let beforeScore = item.priorScore ?? 0.0
        let passed = item.rawStatus == "Pass"

        SurfaceCard(padding: 11) {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 7) {
                    DifficultyBadge(difficulty: item.difficulty)
                    Text(item.title).font(.system(size: 13, weight: .bold))
                    Text(item.shortSha).font(.system(size: 11, design: .monospaced)).foregroundStyle(.secondary)
                    Text("· \(Formatters.shortTimestamp(item.finishedAt ?? item.queuedAt))")
                        .font(.system(size: 10.5, design: .monospaced))
                        .foregroundStyle(.secondary)
                    Spacer()
                    if let btd = item.breakthroughDelta, btd >= store.breakthroughThreshold {
                        SemanticPill(
                            text: String(format: "%@ %+.2f", lang.tr("突破", "BREAKTHROUGH"), btd),
                            icon: "flame.fill",
                            compact: true
                        )
                    }
                    SemanticPill(text: readableHistoryBadge())
                }

                HStack(spacing: 8) {
                    QueueScoreCell(
                        title: lang.tr("提交前分数", "Before"),
                        main: String(format: "%.2f", beforeScore),
                        delta: nil,
                        mainColor: .secondary
                    )
                    QueueArrowSeparator()
                    QueueScoreCell(
                        title: lang.tr("预计得分", "Estimated"),
                        main: item.estimatedScore.map { String(format: "~%.2f", $0) } ?? "—",
                        delta: nil,
                        mainColor: .blue
                    )
                    QueueArrowSeparator()
                    QueueScoreCell(
                        title: lang.tr("评测得分 (本次结果)", "Judged Score"),
                        main: item.actualScore.map { String(format: "%.2f", $0) } ?? readableRawStatus(item.rawStatus),
                        delta: item.deltaScore,
                        mainColor: passed ? .primary : .red
                    )
                }
                .padding(.vertical, 6)
                .padding(.horizontal, 10)
                .background(RoundedRectangle(cornerRadius: 7).fill(Color.primary.opacity(0.035)))

                HStack(spacing: 8) {
                    Text(cleanNote(item.note))
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)

                    Spacer()

                    if let exp = experience {
                        Button {
                            onInspectExperience(exp)
                        } label: {
                            Label(lang.tr("经验档案", "Playbook"), systemImage: "book.pages.fill")
                                .font(.system(size: 11))
                        }
                        .buttonStyle(.bordered)
                        .tint(.orange)
                        .controlSize(.mini)
                    }

                    if passed {
                        Button {
                            Task {
                                await store.jumpSubmitNow(
                                    problem: item.title,
                                    sha: item.sha,
                                    note: lang.tr("重新提交历史版本 \(item.shortSha)", "Resubmit historical \(item.shortSha)")
                                )
                            }
                        } label: {
                            Text(isLocalUploading
                                 ? lang.tr("上传中…", "Uploading…")
                                 : lang.tr("重新提交此版", "Resubmit"))
                                .font(.system(size: 11))
                        }
                        .buttonStyle(.bordered)
                        .controlSize(.mini)
                        .disabled(isLocalUploading)
                    }
                }
            }
        }
    }

    private func readableHistoryBadge() -> String {
        if item.rawStatus == "Pass" {
            return (item.deltaScore ?? 0) >= 0
                ? lang.tr("PASS ↑ 提分", "PASS ↑")
                : lang.tr("PASS ↓ 波动", "PASS ↓")
        }
        if item.rawStatus == "skipped" { return lang.tr("已忽略", "SKIPPED") }
        if item.rawStatus == "sync_error" { return lang.tr("已上传 · 同步中断", "SUBMITTED · SYNC ERR") }
        return "\(lang.tr("未通过", "FAILED")) · \(readableRawStatus(item.rawStatus))"
    }

    private func readableRawStatus(_ raw: String) -> String {
        switch raw {
        case "Wrong Answer": return lang.tr("精度未过 (WA)", "Wrong Answer")
        case "Time Limit Exceeded": return lang.tr("超时 (TLE)", "Time Limit")
        case "Runtime Error": return lang.tr("运行报错 (RE)", "Runtime Error")
        case "Compile Error": return lang.tr("编译失败 (CE)", "Compile Error")
        case "rejected": return lang.tr("本地拦截", "Rejected")
        case "sync_error": return lang.tr("同步中断", "Sync Error")
        case "skipped": return lang.tr("已跳过", "Skipped")
        default: return raw
        }
    }
}

private func cleanNote(_ raw: String) -> String {
    raw.components(separatedBy: " · [S2:").first ?? raw
}
