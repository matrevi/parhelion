import SwiftUI

struct ActiveSubmissionCardView: View {
    @EnvironmentObject var store: ContestStore
    let active: ActiveSubmissionItem
    let pipe: LivePipelineState
    @ObservedObject var clock: WallClockTicker

    private var lang: AppLanguage { store.lang }

    var body: some View {
        let judgeEl = clock.elapsedSince(startedAt: active.startedEpoch)
        let matchedSlot = pipe.accountSlots?.first(where: { $0.id == active.accountId })
        let slotCd = clock.cooldownRemaining(endsAt: matchedSlot?.cooldownEndsEpoch ?? pipe.cooldownEndsEpoch)
        let slotColor: Color = {
            if active.accountId == "primary" { return .blue }
            if active.accountId.hasSuffix("-2") { return .teal }
            if active.accountId.hasSuffix("-3") { return .purple }
            return .indigo
        }()

        SurfaceCard(highlighted: true, highlightColor: slotColor, padding: 11) {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 7) {
                    ProgressView().controlSize(.small)
                    Text(active.accountLabel)
                        .font(.system(size: 10, weight: .bold, design: .rounded))
                        .foregroundStyle(slotColor)
                        .padding(.horizontal, 6.5)
                        .padding(.vertical, 2)
                        .background(Capsule().fill(slotColor.opacity(0.14)))
                    Text(active.title)
                        .font(.system(size: 13.5, weight: .bold))
                    if !active.shortSha.isEmpty {
                        Text(active.shortSha)
                            .font(.system(size: 11, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }
                    if let jid = active.remoteJudgeId {
                        Text(lang.tr("单号 #\(jid)", "Judge #\(jid)"))
                            .font(.system(size: 11, weight: .semibold, design: .monospaced))
                            .foregroundStyle(.blue)
                    }
                    if active.startedEpoch > 0 {
                        Text("· \(Formatters.shortTimestamp(epoch: active.startedEpoch))")
                            .font(.system(size: 10.5, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    SemanticPill(text: phaseLabel(active.phase, remoteStatus: active.remoteStatus))
                }

                HStack(spacing: 16) {
                    Label(elapsedLabel(phase: active.phase, sec: judgeEl), systemImage: "clock.fill")
                        .font(.system(size: 11.5, weight: .semibold, design: .monospaced))
                        .foregroundStyle(slotColor)

                    Label(
                        slotCd > 0
                            ? lang.tr("\(active.accountLabel) 冷却剩余: \(slotCd)s", "\(active.accountLabel) cooldown: \(slotCd)s")
                            : lang.tr("\(active.accountLabel) 190s 冷却已就绪", "\(active.accountLabel) Ready"),
                        systemImage: "timer"
                    )
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(slotCd > 0 ? .orange : .green)

                    Spacer()
                }
            }
        }
    }

    private func elapsedLabel(phase: String, sec: Int) -> String {
        let dur = Formatters.duration(sec)
        switch phase {
        case "waiting_cooldown": return lang.tr("本地排队等待: \(dur)", "Waiting cooldown: \(dur)")
        case "packaging", "uploading": return lang.tr("本地处理耗时: \(dur)", "Local stage: \(dur)")
        case "fetching_score": return lang.tr("总耗时: \(dur) (同步榜单中)", "Elapsed: \(dur) (Syncing)")
        default: return lang.tr("远程判题已耗时: \(dur)", "Remote judging: \(dur)")
        }
    }

    private func phaseLabel(_ phase: String, remoteStatus: String?) -> String {
        switch phase {
        case "waiting_cooldown": return lang.tr("本地等待限流冷却", "Waiting Cooldown")
        case "packaging": return lang.tr("本地校验打包中", "Packaging")
        case "uploading": return lang.tr("正在上传代码", "Uploading")
        case "remote_queued": return lang.tr("远程排队中 (\(remoteStatus ?? "Queued"))", "Remote Queued")
        case "remote_compiling": return lang.tr("远程编译算子中", "Remote Compiling")
        case "remote_running": return lang.tr("远程运行测例中 (\(remoteStatus ?? "Running"))", "Remote Running")
        case "fetching_score": return lang.tr("判题完成 · 同步分数中", "Syncing Score")
        default: return phase
        }
    }
}

struct InflightJudgeCardView: View {
    @EnvironmentObject var store: ContestStore
    let pipe: LivePipelineState
    @ObservedObject var clock: WallClockTicker

    private var lang: AppLanguage { store.lang }

    var body: some View {
        let judgeEl = clock.elapsedSince(startedAt: pipe.judgeStartedEpoch)
        let cdRem = clock.cooldownRemaining(endsAt: pipe.cooldownEndsEpoch)

        SurfaceCard(highlighted: true, highlightColor: .blue, padding: 11) {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text(pipe.activeTitle ?? "Operator")
                        .font(.system(size: 13.5, weight: .bold))
                    if let sha = pipe.activeShortSha, !sha.isEmpty {
                        Text(sha)
                            .font(.system(size: 11, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }
                    if let jid = pipe.remoteJudgeId {
                        Text(lang.tr("单号 #\(jid)", "Judge #\(jid)"))
                            .font(.system(size: 11, weight: .semibold, design: .monospaced))
                            .foregroundStyle(.blue)
                    }
                    if let st = pipe.judgeStartedEpoch, st > 0 {
                        Text("· \(Formatters.shortTimestamp(epoch: st))")
                            .font(.system(size: 10.5, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    SemanticPill(text: phaseLabel(pipe.phase, remoteStatus: pipe.remoteStatus))
                }

                HStack(spacing: 16) {
                    Label(elapsedLabel(phase: pipe.phase, sec: judgeEl), systemImage: "clock.fill")
                        .font(.system(size: 11.5, weight: .semibold, design: .monospaced))
                        .foregroundStyle(.blue)

                    Label(
                        cdRem > 0
                            ? lang.tr("平台限流冷却剩余: \(cdRem)s", "Rate cooldown: \(cdRem)s")
                            : lang.tr("平台 190s 冷却已就绪", "190s Cooldown Ready"),
                        systemImage: "timer"
                    )
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(cdRem > 0 ? .orange : .green)

                    Spacer()
                }
            }
        }
    }

    private func elapsedLabel(phase: String, sec: Int) -> String {
        let dur = Formatters.duration(sec)
        switch phase {
        case "waiting_cooldown": return lang.tr("本地排队等待: \(dur)", "Waiting cooldown: \(dur)")
        case "packaging", "uploading": return lang.tr("本地处理耗时: \(dur)", "Local stage: \(dur)")
        case "fetching_score": return lang.tr("总耗时: \(dur) (同步榜单中)", "Elapsed: \(dur) (Syncing)")
        default: return lang.tr("远程判题已耗时: \(dur)", "Remote judging: \(dur)")
        }
    }

    private func phaseLabel(_ phase: String, remoteStatus: String?) -> String {
        switch phase {
        case "waiting_cooldown": return lang.tr("本地等待限流冷却", "Waiting Cooldown")
        case "packaging": return lang.tr("本地校验打包中", "Packaging")
        case "uploading": return lang.tr("正在上传代码", "Uploading")
        case "remote_queued": return lang.tr("远程排队中 (\(remoteStatus ?? "Queued"))", "Remote Queued")
        case "remote_compiling": return lang.tr("远程编译算子中", "Remote Compiling")
        case "remote_running": return lang.tr("远程运行测例中 (\(remoteStatus ?? "Running"))", "Remote Running")
        case "fetching_score": return lang.tr("判题完成 · 同步分数中", "Syncing Score")
        default: return phase
        }
    }
}

struct CandidateSubmissionCardView: View {
    @EnvironmentObject var store: ContestStore
    let item: SubmissionQueueItem
    let rankIndex: Int
    @ObservedObject var clock: WallClockTicker

    private var lang: AppLanguage { store.lang }
    private var pipeline: LivePipelineState? { store.snapshot?.scheduler.pipeline }
    private var isWaitingCooldown: Bool { pipeline?.phase == "waiting_cooldown" }
    private var isLocalUploading: Bool { ((pipeline?.isLocalBusy ?? false) && !isWaitingCooldown) || store.isLaunchingSubmit }

    var body: some View {
        let isCrit = item.badge == "CRITICAL RESTORE"
        let expDelta = (item.estimatedScore ?? item.currentScore) - item.currentScore
        let cdRem = clock.cooldownRemaining(endsAt: pipeline?.cooldownEndsEpoch ?? store.snapshot?.scheduler.cooldownEndsEpoch)

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
                    SemanticPill(text: readableCandidateBadge(item.badge))
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
                        mainColor: .blue
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
                        .foregroundStyle(item.gates.safetyOk ? Color(red: 0.12, green: 0.70, blue: 0.42) : .orange)

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
                    .disabled(isLocalUploading || item.badge.uppercased().contains("ESCAPE"))
                }
            }
        }
    }

    private func readableCandidateBadge(_ badge: String) -> String {
        let u = badge.uppercased()
        if u.contains("ESCAPE") { return lang.tr("逃逸硬锁 · 禁止提交", "ESCAPE LOCK") }
        if u.contains("CRITICAL") { return lang.tr("榜上0分 · 建议恢复", "CRITICAL RESTORE") }
        if u.contains("HOLD") || u.contains("LOW") { return lang.tr("提分较小 · 可插队", "HOLD · LOW GAIN") }
        if u.contains("CIRCUIT") { return lang.tr("触发频控保护", "CIRCUIT OPEN") }
        return lang.tr("待提交 · READY", "READY")
    }

    private func gateSummaryText(_ item: SubmissionQueueItem) -> String {
        if item.badge.uppercased().contains("ESCAPE") {
            return lang.tr(
                "🔒 逃逸硬锁激活：需先复现线上 WA/TLE 并通过 verify-escape 解锁",
                "🔒 ESCAPE LOCK: add reproducer & pass verify-escape before submitting"
            )
        }
        if !item.gates.safetyOk { return lang.tr("⚠ 缺少完整服务端精度测试记录", "⚠ Missing NPU extended validation") }
        if item.badge.contains("CRITICAL") { return lang.tr("✓ 历史高分验证通过，可立即恢复", "✓ Validated best SHA ready to restore") }
        if !item.gates.noiseOk { return lang.tr("✓ 精度通过 · 预计涨幅低于自动阈值 (可手动插队)", "✓ Precision OK · Below noise floor (Jump Queue allowed)") }
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

struct QueueArrowSeparator: View {
    var body: some View {
        Image(systemName: "arrow.right")
            .font(.system(size: 10, weight: .semibold))
            .foregroundStyle(.tertiary)
    }
}

struct QueueScoreCell: View {
    let title: String
    let main: String
    let delta: Double?
    let mainColor: Color

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title)
                .font(.system(size: 10))
                .foregroundStyle(.secondary)
            HStack(alignment: .firstTextBaseline, spacing: 5) {
                Text(main)
                    .font(.system(size: 13, weight: .bold, design: .monospaced))
                    .foregroundStyle(mainColor)
                if let d = delta {
                    Text(String(format: "(%+.2f)", d))
                        .font(.system(size: 11, weight: .bold, design: .monospaced))
                        .foregroundStyle(d >= 0 ? Color(red: 0.10, green: 0.72, blue: 0.42) : .orange)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private func cleanNote(_ raw: String) -> String {
    raw.components(separatedBy: " · [S2:").first ?? raw
}
