import SwiftUI

struct SpawnExternalWorkerSheet: View {
    let lang: AppLanguage
    let eligibleDrivers: [AgentReadyDriverHot]
    let problems: [String]
    let initialDriver: String
    let initialProblem: String
    let onDismiss: () -> Void
    let onSpawn: (String, String, String, String, String, String, String) -> Void

    @UIState private var driver: String = ""
    @UIState private var roleLabel: String = "impl"
    @UIState private var problem: String = ""
    @UIState private var collabMode: String = "orthogonal"
    @UIState private var axis: String = ""
    @UIState private var worktreeMode: String = "isolated"
    @UIState private var taskPrompt: String = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Label(lang.tr("派发自治外部 Worker", "Spawn Autonomous External Worker"), systemImage: "point.3.connected.trianglepath.dotted")
                    .font(.headline)
                Spacer()
                Button(lang.tr("取消", "Cancel"), action: onDismiss)
            }
            Divider()

            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Harness").font(.system(size: 11, weight: .semibold)).foregroundStyle(.secondary)
                    Picker("", selection: $driver) {
                        ForEach(eligibleDrivers) { d in Text(d.displayName).tag(d.kind) }
                    }
                    .labelsHidden()
                }
                VStack(alignment: .leading, spacing: 4) {
                    Text(lang.tr("协作哲学", "Philosophy")).font(.system(size: 11, weight: .semibold)).foregroundStyle(.secondary)
                    Picker("", selection: $collabMode) {
                        Text(lang.tr("正交探索", "Orthogonal")).tag("orthogonal")
                        Text(lang.tr("环节特化", "Pipeline")).tag("pipeline")
                        Text(lang.tr("单兵", "Solo")).tag("solo")
                    }
                    .pickerStyle(.segmented)
                    .frame(width: 210)
                }
            }

            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(lang.tr("目标算子 Squad", "Problem Squad")).font(.system(size: 11, weight: .semibold)).foregroundStyle(.secondary)
                    Picker("", selection: $problem) {
                        Text(lang.tr("工作区全局", "Workspace Root")).tag("")
                        ForEach(problems, id: \.self) { p in Text(p).tag(p) }
                    }
                    .labelsHidden()
                }
                VStack(alignment: .leading, spacing: 4) {
                    Text(lang.tr("角色 / 轴标签", "Role / Axis")).font(.system(size: 11, weight: .semibold)).foregroundStyle(.secondary)
                    HStack(spacing: 6) {
                        TextField("impl", text: $roleLabel).textFieldStyle(.roundedBorder).frame(width: 72)
                        TextField("L1I-pack / audit", text: $axis).textFieldStyle(.roundedBorder).frame(width: 120)
                    }
                }
            }

            HStack(spacing: 10) {
                Text("Worktree").font(.system(size: 11, weight: .semibold)).foregroundStyle(.secondary)
                Picker("", selection: $worktreeMode) {
                    Text("isolated").tag("isolated")
                    Text("readonly").tag("readonly")
                }
                .pickerStyle(.segmented)
                .frame(width: 160)
                Spacer()
            }

            VStack(alignment: .leading, spacing: 4) {
                Text(lang.tr("任务指令", "Task Prompt")).font(.system(size: 11, weight: .semibold)).foregroundStyle(.secondary)
                TextEditor(text: $taskPrompt)
                    .font(.system(size: 12))
                    .frame(height: 100)
                    .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(Color.primary.opacity(0.14)))
            }

            HStack {
                Spacer()
                Button(lang.tr("立即派发", "Launch Worker")) {
                    let chosen = driver.isEmpty ? (eligibleDrivers.first?.kind ?? "") : driver
                    onSpawn(chosen, roleLabel.isEmpty ? "worker" : roleLabel, problem, collabMode, axis, worktreeMode, taskPrompt)
                }
                .buttonStyle(.borderedProminent)
                .disabled(taskPrompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || eligibleDrivers.isEmpty)
            }
        }
        .padding(16)
        .frame(width: 500)
        .onAppear {
            driver = !initialDriver.isEmpty ? initialDriver : (eligibleDrivers.first?.kind ?? "")
            if !initialProblem.isEmpty { problem = initialProblem }
        }
    }
}
