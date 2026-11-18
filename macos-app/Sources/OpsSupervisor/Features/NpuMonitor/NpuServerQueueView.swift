import SwiftUI

struct NpuServerQueueView: View {
    @EnvironmentObject var store: ContestStore
    let npu: NpuTelemetry

    private var lang: AppLanguage { store.lang }

    var body: some View {
        let activeCnt = npu.tasks.filter { $0.state == "ACTIVE" }.count
        let waitCnt = npu.tasks.count - activeCnt

        VStack(spacing: 0) {
            HStack {
                Label(lang.tr("NPU 服务端验证队列", "NPU Server Task Queue"), systemImage: "server.rack")
                    .font(.system(size: 12.5, weight: .bold))
                Spacer()
                SemanticPill(
                    text: lang.tr("\(activeCnt) 运行 · \(waitCnt) 等待", "\(activeCnt) Active · \(waitCnt) Waiting"),
                    compact: true
                )
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 9)

            Divider()

            if npu.tasks.isEmpty {
                VStack(spacing: 8) {
                    Image(systemName: "checkmark.circle")
                        .font(.system(size: 26))
                        .foregroundStyle(.secondary)
                    Text(npu.online
                         ? lang.tr("NPU 服务端空闲", "NPU Server Idle")
                         : lang.tr("等待 SSH 探针连接…", "Waiting for SSH probe…"))
                        .font(.system(size: 12.5))
                        .foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ScrollView {
                    LazyVStack(spacing: 7) {
                        ForEach(npu.tasks) { task in
                            taskCard(task)
                        }
                    }
                    .padding(10)
                }
            }
        }
    }

    private func taskCard(_ task: NpuTaskItem) -> some View {
        let active = task.state == "ACTIVE"
        return SurfaceCard(highlighted: active, highlightColor: .accentColor, padding: 10) {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 7) {
                    SemanticPill(text: task.stage, icon: active ? "play.fill" : "clock")
                    Text(task.problem ?? lang.tr("排队任务", "Queued Worker"))
                        .font(.system(size: 13, weight: .bold))
                        .lineLimit(1)
                    if let d = task.difficulty {
                        DifficultyBadge(difficulty: d)
                    }
                    Spacer()
                    if let w = task.wsptPriority, w > 0 {
                        Text(String(format: "WSPT %.1f", w))
                            .font(.system(size: 10, weight: .semibold, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }
                }

                HStack(spacing: 10) {
                    Label(task.shortSha ?? "—", systemImage: "number")
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.secondary)
                    Text("PID \(task.pid)")
                        .font(.system(size: 10.5, design: .monospaced))
                        .foregroundStyle(.secondary)
                    if let die = task.die {
                        Text("Die \(die)")
                            .font(.system(size: 10.5, weight: .semibold, design: .monospaced))
                            .foregroundStyle(.green)
                    }
                    Spacer()
                    Text(Formatters.duration(task.elapsedSec))
                        .font(.system(size: 11, weight: .semibold, design: .monospaced))
                        .foregroundStyle(active ? Color.accentColor : .secondary)
                }

                Text(task.detail)
                    .font(.system(size: 10.5))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        }
    }
}
