import SwiftUI

struct NpuServerQueueView: View {
    @EnvironmentObject var store: ContestStore
    @EnvironmentObject var hotStore: HotTelemetryStore
    @StateObject private var laneModel = NpuLaneModel()
    @Namespace private var laneNamespace
    let npu: NpuTelemetry

    private var lang: AppLanguage { store.lang }
    private var liveNpu: NpuTelemetry { hotStore.npu ?? npu }

    /// Lightweight Equatable signature so `laneModel` ingests on every meaningful hot NPU change.
    private var taskFingerprint: [String] {
        liveNpu.tasks.map {
            "\($0.runId ?? "")|\($0.pid)|\($0.state)|\($0.stage)|\($0.lane ?? "")|\($0.problem ?? "")|\($0.shortSha ?? "")|\($0.die ?? -1)|\($0.elapsedSec ?? -1)|\($0.boosted == true)|\($0.detail)|\($0.caseProgress?.hashValue ?? 0)"
        }
    }

    private var activeCards: [NpuLaneCard] {
        laneModel.hasIngested ? laneModel.cards : NpuLaneModel.preview(liveNpu.tasks)
    }

    private var dieExecCards: [NpuLaneCard] {
        activeCards.filter { $0.lane == .active }
    }

    private var cpuBuildCards: [NpuLaneCard] {
        activeCards.filter { $0.lane == .build }
    }

    private var waitingCards: [NpuLaneCard] {
        activeCards.filter { $0.lane == .waiting }
    }

    var body: some View {
        let liveDieCount = dieExecCards.filter { !$0.lingering }.count
        let liveBuildCount = cpuBuildCards.filter { !$0.lingering }.count
        let liveWaitCount = waitingCards.filter { !$0.lingering }.count

        VStack(spacing: 0) {
            HStack {
                Label(lang.tr("NPU 三车道验证队列", "NPU 3-Lane Pipeline Queue"), systemImage: "server.rack")
                    .font(.system(size: 12.5, weight: .bold))
                Spacer()
                SemanticPill(
                    text: lang.tr(
                        "\(liveDieCount) 占卡 · \(liveBuildCount) 编译 · \(liveWaitCount) 候票",
                        "\(liveDieCount) Die · \(liveBuildCount) Build · \(liveWaitCount) Wait"
                    ),
                    compact: true
                )
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 9)

            Divider()

            if !liveNpu.online && activeCards.isEmpty {
                emptyState(lang.tr("等待 SSH 探针连接…", "Waiting for SSH probe…"))
            } else {
                ScrollView {
                    // Use VStack (not LazyVStack) so matchedGeometryEffect tracks cards cleanly
                    // across all three lanes without destroying views or leaving slot gaps.
                    VStack(alignment: .leading, spacing: 10) {
                        laneSection(
                            title: lang.tr("① DIE 0 硬件独占执行区", "① DIE 0 EXECUTING"),
                            badge: "\(liveDieCount)",
                            tint: .blue,
                            items: dieExecCards,
                            emptyText: lang.tr("Die 0 空闲 · 可立即进入 Phase B", "Die 0 Free · Ready for Phase B")
                        )

                        laneSection(
                            title: lang.tr("② CPU 编译与校验（不占卡）", "② CPU BUILD & VALIDATION"),
                            badge: "\(liveBuildCount)",
                            tint: .teal,
                            items: cpuBuildCards,
                            emptyText: lang.tr("当前无后台编译任务", "No active Phase A builds")
                        )

                        laneSection(
                            title: lang.tr("③ WSPT 待锁票据队列", "③ WSPT DIE TICKET QUEUE"),
                            badge: "\(liveWaitCount)",
                            tint: .orange,
                            items: waitingCards,
                            emptyText: lang.tr("无排队等待任务", "Zero queue wait")
                        )
                    }
                    .padding(10)
                    .animation(.snappy(duration: 0.30, extraBounce: 0.0), value: laneModel.layoutSignature)
                }
            }
        }
        .onAppear {
            laneModel.ingest(liveNpu.tasks)
        }
        .onChange(of: taskFingerprint) { _, _ in
            laneModel.ingest(liveNpu.tasks)
        }
        .onChange(of: store.activeSSHHost) { _, _ in
            laneModel.reset()
        }
    }

    private func laneSection(title: String, badge: String, tint: Color, items: [NpuLaneCard], emptyText: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Circle().fill(tint).frame(width: 6, height: 6)
                Text(title)
                    .font(.system(size: 10.5, weight: .bold))
                    .foregroundStyle(.secondary)
                Spacer()
                Text(badge)
                    .font(.system(size: 10, weight: .bold, design: .monospaced))
                    .foregroundStyle(tint)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 1)
                    .background(Capsule().fill(tint.opacity(0.14)))
            }
            .padding(.horizontal, 2)

            if items.isEmpty {
                Text(emptyText)
                    .font(.system(size: 11))
                    .foregroundStyle(.tertiary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 7)
                    .background(RoundedRectangle(cornerRadius: 7).fill(Color.primary.opacity(0.025)))
                    .transition(.opacity)
            } else {
                ForEach(items) { card in
                    taskCard(card)
                        .opacity(card.lingering ? 0.68 : 1.0)
                        .matchedGeometryEffect(id: card.id, in: laneNamespace)
                        .transition(.opacity)
                }
            }
        }
    }

    private func taskCard(_ card: NpuLaneCard) -> some View {
        let task = card.task
        let active = card.lane == .active
        let isBuild = card.lane == .build
        let isBackfill = task.isBackfillTask
        let highlightCol: Color = isBackfill
            ? .orange
            : (active ? .accentColor : (isBuild ? .teal : .clear))

        return SurfaceCard(highlighted: active || isBuild || isBackfill, highlightColor: highlightCol, padding: 10) {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 7) {
                    SemanticPill(
                        text: lang.npuStage(task.stage),
                        icon: isBackfill
                            ? (active ? "waveform.path.ecg" : (isBuild ? "hammer.fill" : "clock.arrow.circlepath"))
                            : (active ? "play.fill" : (isBuild ? "hammer.fill" : "clock"))
                    )
                    if isBackfill {
                        Text(lang.tr("B0 样例桶", "B0 Bucket"))
                            .font(.system(size: 9.5, weight: .bold, design: .rounded))
                            .foregroundStyle(Color(red: 0.78, green: 0.42, blue: 0.04))
                            .padding(.horizontal, 6)
                            .padding(.vertical, 2)
                            .background(Capsule(style: .continuous).fill(Color.orange.opacity(0.16)))
                            .overlay(Capsule(style: .continuous).strokeBorder(Color.orange.opacity(0.35), lineWidth: 0.6))
                    }
                    Text(task.problem ?? (active ? lang.tr("设备锁持有者", "Device lock holder") : lang.tr("排队验证任务", "Queued Worker")))
                        .font(.system(size: 13, weight: .bold))
                        .lineLimit(1)
                    if let d = task.difficulty {
                        DifficultyBadge(difficulty: d)
                    }
                    Spacer()
                    if isBackfill {
                        Text(lang.tr("低优让行 · B0", "Low Prio · B0"))
                            .font(.system(size: 10, weight: .semibold, design: .monospaced))
                            .foregroundStyle(.orange)
                    } else if let w = task.wsptPriority, w > 0 {
                        Text(String(format: lang.tr("优先级 %.1f", "WSPT %.1f"), w))
                            .font(.system(size: 10, weight: .semibold, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }
                }

                HStack(spacing: 10) {
                    Label(task.shortSha ?? "—", systemImage: "number")
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.secondary)
                    if task.pid > 0 {
                        Text("PID \(task.pid)")
                            .font(.system(size: 10.5, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }
                    if task.elapsedSec != nil && task.caseProgress == nil {
                        Text("· \(Formatters.shortTimestamp(epoch: card.startedEpoch))")
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.tertiary)
                    }
                    if let die = task.die {
                        Text(lang.tr("\(die)号卡 (Die \(die))", "Die \(die)"))
                            .font(.system(size: 10.5, weight: .semibold, design: .monospaced))
                            .foregroundStyle(.green)
                    } else if let estWait = task.estimatedWaitSec, card.lane == .waiting {
                        Text(lang.tr("预计等 ~\(Formatters.duration(estWait))", "Est. ~\(Formatters.duration(estWait))"))
                            .font(.system(size: 10, weight: .semibold, design: .monospaced))
                            .foregroundStyle(.orange)
                    }
                    if let p = task.caseProgress, p.total > 0, p.index > 0, p.index <= p.total {
                        progressLabel(p)
                    }
                    Spacer(minLength: 0)
                    Text(task.elapsedSec.map { Formatters.duration($0) } ?? (active ? lang.tr("时长未知", "Duration unknown") : ""))
                        .font(.system(size: 11, weight: .semibold, design: .monospaced))
                        .foregroundStyle(active ? Color.accentColor : .secondary)
                }

                if task.caseProgress == nil || (!active && task.pid > 0 && !card.lingering) {
                  HStack(spacing: 6) {
                    if task.caseProgress == nil {
                        Text(lang.npuDetail(task.detail))
                        .font(.system(size: 10.5))
                        .foregroundStyle(task.boosted == true ? .orange : .secondary)
                        .lineLimit(1)
                    }

                    Spacer()

                    if card.lane == .waiting && task.boosted != true && task.pid > 0 && !card.lingering {
                        Button {
                            Task {
                                await store.boostNpuTicket(
                                    pid: task.pid,
                                    problem: task.problem ?? "Operator",
                                    sha: task.sha ?? ""
                                )
                            }
                        } label: {
                            Label(lang.tr("置顶优先", "Boost"), systemImage: "bolt.fill")
                                .font(.system(size: 10, weight: .semibold))
                        }
                        .buttonStyle(.bordered)
                        .tint(.orange)
                        .controlSize(.mini)
                    }

                    if (card.lane == .waiting || card.lane == .build) && task.pid > 0 && !card.lingering {
                        Button {
                            Task {
                                await store.cancelNpuTicket(pid: task.pid, problem: task.problem ?? "Task")
                            }
                        } label: {
                            Text(lang.tr("取消", "Cancel"))
                                .font(.system(size: 10))
                        }
                        .buttonStyle(.bordered)
                        .controlSize(.mini)
                    }
                  }
                }
            }
        }
    }

    private func progressLabel(_ p: NpuCaseProgress) -> some View {
        let stage = p.phase == "prepare" ? lang.tr("准备", "Prepare")
            : p.phase == "verify" ? lang.tr("校验", "Check") : lang.tr("用例", "Case")
        var fields = ["\(stage) \(p.index)/\(p.total)"]
        if let i = p.part, let n = p.parts, n > 1 { fields.append(lang.tr("段 \(i)/\(n)", "Part \(i)/\(n)")) }
        if let i = p.repetition, let n = p.repeats, n > 1 { fields.append(lang.tr("轮 \(i)/\(n)", "Run \(i)/\(n)")) }
        return Text(fields.joined(separator: " · "))
            .font(.system(size: 10.5, weight: .medium))
            .monospacedDigit()
            .foregroundStyle(.secondary)
            .lineLimit(1)
            .help(fields.joined(separator: " · "))
    }

    private func emptyState(_ text: String) -> some View {
        VStack(spacing: 8) {
            Image(systemName: "checkmark.circle")
                .font(.system(size: 26))
                .foregroundStyle(.secondary)
            Text(text)
                .font(.system(size: 12.5))
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}
