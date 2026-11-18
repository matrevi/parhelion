import SwiftUI

struct NpuServerQueueView: View {
    let npu: NpuTelemetry

    var body: some View {
        let activeCnt = npu.tasks.filter { $0.state == "ACTIVE" }.count
        let waitCnt = npu.tasks.count - activeCnt

        VStack(spacing: 0) {
            HStack {
                Label("NPU Server Task Queue", systemImage: "server.rack")
                    .font(.system(size: 12.5, weight: .bold))
                Spacer()
                SemanticPill(text: "\(activeCnt) Active · \(waitCnt) Waiting", compact: true)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 9)

            Divider()

            if npu.tasks.isEmpty {
                VStack(spacing: 8) {
                    Image(systemName: "checkmark.circle")
                        .font(.system(size: 26))
                        .foregroundStyle(.secondary)
                    Text(npu.online ? "NPU Server Idle" : "Waiting for SSH probe…")
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
                    Text(task.problem ?? "Queued Worker")
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
