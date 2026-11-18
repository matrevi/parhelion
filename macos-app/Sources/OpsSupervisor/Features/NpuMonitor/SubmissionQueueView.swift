import SwiftUI

enum QueueViewTab: String, CaseIterable, Identifiable {
    case waiting = "待提交队列"
    case history = "历史评测记录"
    case held = "低收益暂缓"

    var id: String { rawValue }
}

struct SubmissionQueueView: View {
    @EnvironmentObject var store: ContestStore
    let items: [SubmissionQueueItem]

    @StateObject private var clock = WallClockTicker()
    @UIState private var selectedTab: QueueViewTab = .waiting

    private var pipeline: LivePipelineState? { store.snapshot?.scheduler.pipeline }
    private var isBusySubmitting: Bool { (pipeline?.isBusy ?? false) || store.isLaunchingSubmit }

    private var waitingItems: [SubmissionQueueItem] {
        items.filter { $0.kind != "completed" && !$0.badge.contains("HOLD") && !$0.badge.contains("CIRCUIT") }
    }

    private var heldItems: [SubmissionQueueItem] {
        items.filter { $0.kind != "completed" && ($0.badge.contains("HOLD") || $0.badge.contains("CIRCUIT")) }
    }

    private var completedItems: [SubmissionQueueItem] {
        items.filter { $0.kind == "completed" }
    }

    var body: some View {
        VStack(spacing: 0) {
            headerTabs
            Divider()

            ScrollView {
                LazyVStack(spacing: 9) {
                    // Always pin the active in-flight remote/local submission at #0 on the Waiting tab
                    if selectedTab == .waiting, let pipe = pipeline, pipe.isBusy {
                        inflightJudgeCard(pipe)
                    }

                    switch selectedTab {
                    case .waiting:
                        let combined = waitingItems.isEmpty ? heldItems : waitingItems
                        if combined.isEmpty && !(pipeline?.isBusy ?? false) {
                            emptyHint("当前没有待提交的候选版本")
                        } else {
                            ForEach(Array(combined.enumerated()), id: \.element.id) { idx, item in
                                candidateCard(item, rankIndex: idx + 1)
                            }
                        }
                    case .history:
                        if completedItems.isEmpty {
                            emptyHint("暂无历史评测记录")
                        } else {
                            ForEach(completedItems) { item in
                                completedCard(item)
                            }
                        }
                    case .held:
                        if heldItems.isEmpty {
                            emptyHint("没有被防抖门禁暂缓的候选版本")
                        } else {
                            ForEach(Array(heldItems.enumerated()), id: \.element.id) { idx, item in
                                candidateCard(item, rankIndex: idx + 1)
                            }
                        }
                    }
                }
                .padding(10)
            }
        }
    }

    private var headerTabs: some View {
        HStack {
            Label("提交与判题队列", systemImage: "paperplane.circle.fill")
                .font(.system(size: 13, weight: .bold))
            Spacer()
            Picker("", selection: $selectedTab) {
                Text("待提交 (\(waitingItems.count))").tag(QueueViewTab.waiting)
                Text("历史记录 (\(completedItems.count))").tag(QueueViewTab.history)
                Text("暂缓 (\(heldItems.count))").tag(QueueViewTab.held)
            }
            .pickerStyle(.segmented)
            .controlSize(.small)
            .frame(width: 295)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 9)
    }

    // MARK: - Top Pinned Live In-Flight Judge Card

    private func inflightJudgeCard(_ pipe: LivePipelineState) -> some View {
        let judgeEl = clock.elapsedSince(startedAt: pipe.judgeStartedEpoch)
        let cdRem = clock.cooldownRemaining(endsAt: pipe.cooldownEndsEpoch)

        return SurfaceCard(highlighted: true, highlightColor: .blue, padding: 11) {
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
                        Text("单号 #\(jid)")
                            .font(.system(size: 11, weight: .semibold, design: .monospaced))
                            .foregroundStyle(.blue)
                    }
                    Spacer()
                    SemanticPill(text: phaseLabel(pipe.phase, remoteStatus: pipe.remoteStatus))
                }

                HStack(spacing: 16) {
                    Label(elapsedLabel(phase: pipe.phase, sec: judgeEl), systemImage: "clock.fill")
                        .font(.system(size: 11.5, weight: .semibold, design: .monospaced))
                        .foregroundStyle(.blue)

                    Label(
                        cdRem > 0 ? "平台限流冷却剩余: \(cdRem)s" : "平台 190s 冷却已就绪",
                        systemImage: "timer"
                    )
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(cdRem > 0 ? .orange : .green)

                    Spacer()
                }
            }
        }
    }

    // MARK: - Waiting / Candidate Card (With One-Click Jump-Queue Button!)

    private func candidateCard(_ item: SubmissionQueueItem, rankIndex: Int) -> some View {
        let isCrit = item.badge == "CRITICAL RESTORE"
        let expDelta = (item.estimatedScore ?? item.currentScore) - item.currentScore
        let cdRem = clock.cooldownRemaining(endsAt: pipeline?.cooldownEndsEpoch ?? store.snapshot?.scheduler.cooldownEndsEpoch)

        return SurfaceCard(highlighted: rankIndex == 1 || isCrit, highlightColor: isCrit ? .red : .accentColor, padding: 11) {
            VStack(alignment: .leading, spacing: 8) {
                // Header Row
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
                    Spacer()
                    SemanticPill(text: readableCandidateBadge(item.badge))
                }

                // Causal 3-Step Score Flow: 提交前分数 -> 预计得分 -> 评测结果
                HStack(spacing: 8) {
                    causalScoreBox(title: "提交前分数", main: String(format: "%.2f", item.currentScore), delta: nil, mainColor: item.currentScore == 0 ? .red : .primary)
                    arrowSeparator
                    causalScoreBox(
                        title: "预计得分",
                        main: item.estimatedScore.map { String(format: "~%.2f", $0) } ?? "—",
                        delta: item.estimatedScore != nil ? expDelta : nil,
                        mainColor: .blue
                    )
                    arrowSeparator
                    causalScoreBox(title: "评测结果", main: "待提交", delta: nil, mainColor: .secondary)
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

                // Action Row: Primary Jump-Queue Submit + Secondary Skip
                HStack(spacing: 8) {
                    Text(gateSummaryText(item))
                        .font(.system(size: 10.5, weight: .medium))
                        .foregroundStyle(item.gates.safetyOk ? Color(red: 0.12, green: 0.70, blue: 0.42) : .orange)

                    Spacer()

                    if item.canSkip {
                        Button("忽略") {
                            Task { await store.skipCandidate(problem: item.title, sha: item.sha) }
                        }
                        .buttonStyle(.bordered)
                        .controlSize(.small)
                    }

                    Button {
                        Task { await store.jumpSubmitNow(problem: item.title, sha: item.sha, note: item.note) }
                    } label: {
                        if isBusySubmitting {
                            Label("上一发提交中，请等待", systemImage: "hourglass")
                                .font(.system(size: 11.5, weight: .semibold))
                        } else if rankIndex == 1 {
                            Label(cdRem > 0 ? "预约提交 (冷却 \(cdRem)s)" : "立即提交", systemImage: "paperplane.fill")
                                .font(.system(size: 11.5, weight: .semibold))
                        } else {
                            Label("插队提交", systemImage: "bolt.fill")
                                .font(.system(size: 11.5, weight: .semibold))
                        }
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(isCrit ? .red : .accentColor)
                    .controlSize(.small)
                    .disabled(isBusySubmitting)
                }
            }
        }
    }

    // MARK: - Completed Historical Card (提交前分数 -> 预计得分 -> 评测得分 (+Δ inline!))

    private func completedCard(_ item: SubmissionQueueItem) -> some View {
        let beforeScore = item.priorScore ?? 0.0
        let passed = item.rawStatus == "Pass"

        return SurfaceCard(padding: 11) {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 7) {
                    DifficultyBadge(difficulty: item.difficulty)
                    Text(item.title).font(.system(size: 13, weight: .bold))
                    Text(item.shortSha).font(.system(size: 11, design: .monospaced)).foregroundStyle(.secondary)
                    Text("· \(Formatters.shortTimestamp(item.finishedAt ?? item.queuedAt))")
                        .font(.system(size: 10.5, design: .monospaced))
                        .foregroundStyle(.secondary)
                    Spacer()
                    SemanticPill(text: readableHistoryBadge(item))
                }

                // Clear Causal Progression: 提交前分数 -> 预计得分 -> 评测得分 (+Δ inline!)
                HStack(spacing: 8) {
                    causalScoreBox(
                        title: "提交前分数",
                        main: String(format: "%.2f", beforeScore),
                        delta: nil,
                        mainColor: .secondary
                    )
                    arrowSeparator
                    causalScoreBox(
                        title: "预计得分",
                        main: item.estimatedScore.map { String(format: "~%.2f", $0) } ?? "—",
                        delta: nil,
                        mainColor: .blue
                    )
                    arrowSeparator
                    causalScoreBox(
                        title: "评测得分 (本次结果)",
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

                    if passed {
                        Button {
                            Task { await store.jumpSubmitNow(problem: item.title, sha: item.sha, note: "重新提交历史版本 \(item.shortSha)") }
                        } label: {
                            Text(isBusySubmitting ? "请等待…" : "重新提交此版")
                                .font(.system(size: 11))
                        }
                        .buttonStyle(.bordered)
                        .controlSize(.mini)
                        .disabled(isBusySubmitting)
                    }
                }
            }
        }
    }

    // MARK: - Helpers

    private var arrowSeparator: some View {
        Image(systemName: "arrow.right")
            .font(.system(size: 10, weight: .semibold))
            .foregroundStyle(.tertiary)
    }

    private func causalScoreBox(title: String, main: String, delta: Double?, mainColor: Color) -> some View {
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

    private func elapsedLabel(phase: String, sec: Int) -> String {
        switch phase {
        case "waiting_cooldown": return "本地排队等待: \(Formatters.duration(sec))"
        case "packaging", "uploading": return "本地处理耗时: \(Formatters.duration(sec))"
        case "fetching_score": return "总耗时: \(Formatters.duration(sec)) (同步榜单中)"
        default: return "远程判题已耗时: \(Formatters.duration(sec))"
        }
    }

    private func phaseLabel(_ phase: String, remoteStatus: String?) -> String {
        switch phase {
        case "waiting_cooldown": return "本地等待限流冷却"
        case "packaging": return "本地校验打包中"
        case "uploading": return "正在上传代码"
        case "remote_queued": return "远程排队中 (\(remoteStatus ?? "Queued"))"
        case "remote_compiling": return "远程编译算子中"
        case "remote_running": return "远程运行测例中 (\(remoteStatus ?? "Running"))"
        case "fetching_score": return "判题完成 · 同步分数中"
        default: return phase
        }
    }

    private func readableCandidateBadge(_ badge: String) -> String {
        let u = badge.uppercased()
        if u.contains("CRITICAL") { return "榜上0分 · 建议恢复" }
        if u.contains("HOLD") || u.contains("LOW") { return "提分较小 · 可插队强推" }
        if u.contains("CIRCUIT") { return "触发频控保护" }
        return "待提交 · READY"
    }

    private func readableHistoryBadge(_ item: SubmissionQueueItem) -> String {
        if item.rawStatus == "Pass" {
            return (item.deltaScore ?? 0) >= 0 ? "PASS ↑ 提分" : "PASS ↓ 波动"
        }
        if item.rawStatus == "skipped" { return "已忽略" }
        return "未通过 · \(readableRawStatus(item.rawStatus))"
    }

    private func readableRawStatus(_ raw: String) -> String {
        switch raw {
        case "Wrong Answer": return "精度未过 (WA)"
        case "Time Limit Exceeded": return "超时 (TLE)"
        case "Runtime Error": return "运行报错 (RE)"
        case "Compile Error": return "编译失败 (CE)"
        case "rejected": return "本地校验拦截"
        case "skipped": return "已跳过"
        default: return raw
        }
    }

    private func gateSummaryText(_ item: SubmissionQueueItem) -> String {
        if !item.gates.safetyOk { return "⚠ 缺少完整服务端精度测试记录" }
        if item.badge.contains("CRITICAL") { return "✓ 历史高分验证通过，可立即恢复" }
        if !item.gates.noiseOk { return "✓ 精度通过 · 预计涨幅低于自动阈值 (可手动插队)" }
        return "✓ 本地精度与防抖门禁全部通过"
    }

    private func cleanNote(_ raw: String) -> String {
        raw.components(separatedBy: " · [S2:").first ?? raw
    }

    private func emptyHint(_ text: String) -> some View {
        VStack(spacing: 8) {
            Image(systemName: "tray")
                .font(.system(size: 24))
                .foregroundStyle(.secondary)
            Text(text)
                .font(.system(size: 12.5))
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 40)
    }
}
