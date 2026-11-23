import SwiftUI

struct NpuServerQueueView: View {
    @EnvironmentObject var store: ContestStore
    @EnvironmentObject var hotStore: HotTelemetryStore
    let npu: NpuTelemetry

    private var lang: AppLanguage { store.lang }
    private var liveNpu: NpuTelemetry { hotStore.npu ?? npu }

    private var dieExecTasks: [NpuTaskItem] {
        liveNpu.tasks.filter { $0.state == "ACTIVE" }
    }

    private var cpuBuildTasks: [NpuTaskItem] {
        liveNpu.tasks.filter { $0.state == "CPU_BUILD" }
    }

    private var waitingTickets: [NpuTaskItem] {
        liveNpu.tasks.filter { $0.state == "WAITING" }
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Label(lang.tr("NPU 三车道验证队列", "NPU 3-Lane Pipeline Queue"), systemImage: "server.rack")
                    .font(.system(size: 12.5, weight: .bold))
                Spacer()
                SemanticPill(
                    text: lang.tr(
                        "\(dieExecTasks.count) 占卡 · \(cpuBuildTasks.count) 编译 · \(waitingTickets.count) 候票",
                        "\(dieExecTasks.count) Die · \(cpuBuildTasks.count) Build · \(waitingTickets.count) Wait"
                    ),
                    compact: true
                )
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 9)

            Divider()

            if !liveNpu.online {
                emptyState(lang.tr("等待 SSH 探针连接…", "Waiting for SSH probe…"))
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 10) {
                        laneSection(
                            title: lang.tr("① DIE 0 硬件独占执行区", "① DIE 0 EXECUTING"),
                            badge: "\(dieExecTasks.count)",
                            tint: .blue,
                            items: dieExecTasks,
                            emptyText: lang.tr("Die 0 空闲 · 可立即进入 Phase B", "Die 0 Free · Ready for Phase B")
                        )

                        laneSection(
                            title: lang.tr("② 32核无锁并行编译区 (0s 占卡)", "② 32-CORE CPU BUILD (0s Lock)"),
                            badge: "\(cpuBuildTasks.count)",
                            tint: .teal,
                            items: cpuBuildTasks,
                            emptyText: lang.tr("当前无后台编译任务", "No active Phase A builds")
                        )

                        laneSection(
                            title: lang.tr("③ WSPT 待锁票据队列", "③ WSPT DIE TICKET QUEUE"),
                            badge: "\(waitingTickets.count)",
                            tint: .orange,
                            items: waitingTickets,
                            emptyText: lang.tr("无排队等待任务", "Zero queue wait")
                        )
                    }
                    .padding(10)
                }
            }
        }
    }

    private func laneSection(title: String, badge: String, tint: Color, items: [NpuTaskItem], emptyText: String) -> some View {
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
            } else {
                ForEach(items) { task in
                    taskCard(task)
                }
            }
        }
    }

    private func taskCard(_ task: NpuTaskItem) -> some View {
        let active = task.state == "ACTIVE"
        let isBuild = task.state == "CPU_BUILD"
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
                            .background(
                                Capsule(style: .continuous)
                                    .fill(Color.orange.opacity(0.16))
                            )
                            .overlay(
                                Capsule(style: .continuous)
                                    .strokeBorder(Color.orange.opacity(0.35), lineWidth: 0.6)
                            )
                    }
                    Text(task.problem ?? lang.tr("排队验证任务", "Queued Worker"))
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
                        Text(String("PID \(task.pid)"))
                            .font(.system(size: 10.5, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }
                    Text("· \(Formatters.shortTimestamp(epoch: Date().timeIntervalSince1970 - Double(max(0, task.elapsedSec))))")
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.tertiary)
                    if let die = task.die {
                        Text(lang.tr("\(die)号卡 (Die \(die))", "Die \(die)"))
                            .font(.system(size: 10.5, weight: .semibold, design: .monospaced))
                            .foregroundStyle(.green)
                    } else if let estWait = task.estimatedWaitSec, task.state == "WAITING" {
                        Text(lang.tr("预计等 ~\(Formatters.duration(estWait))", "Est. ~\(Formatters.duration(estWait))"))
                            .font(.system(size: 10, weight: .semibold, design: .monospaced))
                            .foregroundStyle(.orange)
                    }
                    Spacer()
                    Text(Formatters.duration(task.elapsedSec))
                        .font(.system(size: 11, weight: .semibold, design: .monospaced))
                        .foregroundStyle(active ? Color.accentColor : .secondary)
                }

                HStack(spacing: 6) {
                    Text(lang.npuDetail(task.detail))
                        .font(.system(size: 10.5))
                        .foregroundStyle(task.boosted == true ? .orange : .secondary)
                        .lineLimit(1)

                    Spacer()

                    if task.state == "WAITING" && task.boosted != true && task.pid > 0 {
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

                    if (task.state == "WAITING" || task.state == "CPU_BUILD") && task.pid > 0 {
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
