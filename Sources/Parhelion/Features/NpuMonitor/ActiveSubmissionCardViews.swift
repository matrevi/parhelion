import SwiftUI

/// Shared animated deck displaying live local packaging/uploading & remote judging cards.
struct ActiveSubmissionsDeckView: View {
    @EnvironmentObject var hotStore: HotTelemetryStore
    let fallbackPipeline: LivePipelineState?
    @ObservedObject var clock: WallClockTicker

    private var livePipeline: LivePipelineState? {
        hotStore.pipeline ?? fallbackPipeline
    }

    var body: some View {
        let pipe = livePipeline
        let activeIds = (pipe?.activeSubmissions ?? []).map(\.id)
        Group {
            if let pipe {
                if let actives = pipe.activeSubmissions, !actives.isEmpty {
                    ForEach(actives) { active in
                        ActiveSubmissionCardView(active: active, pipe: pipe, clock: clock)
                            .transition(.asymmetric(
                                insertion: .move(edge: .top).combined(with: .opacity),
                                removal: .opacity
                            ))
                    }
                } else if pipe.isBusy {
                    InflightJudgeCardView(pipe: pipe, clock: clock)
                        .transition(.opacity)
                }
            }
        }
        .animation(.snappy(duration: 0.28), value: activeIds)
        .animation(.snappy(duration: 0.28), value: pipe?.isBusy ?? false)
    }
}

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
    var ucbLabel: String? = nil
    var ucbHighlighted: Bool = false

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
                if let ucbLabel {
                    let accent: Color = ucbHighlighted ? .orange : .indigo
                    Text(ucbLabel)
                        .font(.system(size: 9.5, weight: .bold, design: .monospaced))
                        .foregroundStyle(accent)
                        .lineLimit(1)
                        .padding(.horizontal, 5)
                        .padding(.vertical, 1)
                        .background(Capsule().fill(accent.opacity(0.15)))
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

