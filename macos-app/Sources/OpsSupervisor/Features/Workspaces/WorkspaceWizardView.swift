import AppKit
import SwiftUI

struct WorkspaceWizardView: View {
    @EnvironmentObject var store: ContestStore

    @UIState private var targetDir: String = "\(NSHomeDirectory())/Competitions/accel-next-contest"
    @UIState private var contestTitle: String = "KernelC Operator Optimization Track"
    @UIState private var contestId: String = ""
    @UIState private var gitOwner: String = "winbeau"
    @UIState private var repoPrefix: String = "accel-ops"
    @UIState private var npuHost: String = "npu"
    @UIState private var problemsSpec: String = "FlashAttentionScore:3, LayerNormGrad:2, MoeGatingTopK:3"
    @UIState private var activateOnCreate: Bool = false
    @UIState private var isCreating: Bool = false
    @UIState private var showPreview: Bool = false

    private var lang: AppLanguage { store.lang }

    var body: some View {
        HStack(alignment: .top, spacing: 16) {
            SurfaceCard(padding: 14) {
                VStack(alignment: .leading, spacing: 12) {
                    HStack {
                        Label(lang.tr("新建零噪声竞赛工作区", "New Clean Contest Workspace"), systemImage: "sparkles.rectangle.stack.fill")
                            .font(.system(size: 15, weight: .bold))
                        Spacer()
                        SemanticPill(text: "AGENTS.md-First", compact: true)
                    }

                    VStack(alignment: .leading, spacing: 8) {
                        labeledField(lang.tr("目标目录路径", "Target Directory")) {
                            HStack(spacing: 6) {
                                TextField("~/Competitions/...", text: $targetDir)
                                    .textFieldStyle(.roundedBorder)
                                    .font(.system(size: 12, design: .monospaced))
                                Button(lang.tr("浏览…", "Browse…"), action: pickFolder)
                                    .controlSize(.small)
                            }
                        }

                        HStack(spacing: 10) {
                            labeledField(lang.tr("赛事名称", "Contest Title")) {
                                TextField("Contest Title", text: $contestTitle).textFieldStyle(.roundedBorder)
                            }
                            labeledField(lang.tr("赛事 ID (选填)", "Contest ID (Optional)")) {
                                TextField("6a9e2d...", text: $contestId)
                                    .textFieldStyle(.roundedBorder)
                                    .font(.system(size: 12, design: .monospaced))
                            }
                        }

                        HStack(spacing: 10) {
                            labeledField(lang.tr("GitCode 账号", "GitCode Owner")) {
                                TextField("winbeau", text: $gitOwner)
                                    .textFieldStyle(.roundedBorder)
                                    .font(.system(size: 12, design: .monospaced))
                            }
                            labeledField(lang.tr("私有仓库前缀", "Private Repo Prefix")) {
                                TextField("accel-ops", text: $repoPrefix)
                                    .textFieldStyle(.roundedBorder)
                                    .font(.system(size: 12, design: .monospaced))
                            }
                            labeledField(lang.tr("NPU SSH 别名", "NPU SSH Alias")) {
                                TextField("npu", text: $npuHost)
                                    .textFieldStyle(.roundedBorder)
                                    .font(.system(size: 12, design: .monospaced))
                            }
                        }

                        labeledField(lang.tr("算子列表与难度等级 (名称:等级)", "Operators & Difficulty Tiers (Name:Tier)")) {
                            TextField("OpA:3, OpB:2", text: $problemsSpec)
                                .textFieldStyle(.roundedBorder)
                                .font(.system(size: 12, design: .monospaced))
                        }
                    }

                    HStack {
                        Toggle(lang.tr("创建后立即切换为当前工作区", "Switch active workspace after creation"), isOn: $activateOnCreate)
                            .toggleStyle(.checkbox)
                            .font(.system(size: 12))

                        Spacer()

                        Button(lang.tr("预览 AGENTS.md", "Preview AGENTS.md")) { showPreview = true }
                            .buttonStyle(.bordered)

                        Button {
                            isCreating = true
                            Task {
                                do {
                                    try await store.repository.scaffoldWorkspace(
                                        targetDir: targetDir,
                                        title: contestTitle,
                                        contestId: contestId,
                                        gitOwner: gitOwner,
                                        repoPrefix: repoPrefix,
                                        npuHost: npuHost,
                                        problemsSpec: problemsSpec,
                                        activate: activateOnCreate
                                    )
                                    store.bannerMessage = lang.tr("已在 \(targetDir) 初始化工作区", "Scaffolded clean workspace at \(targetDir)")
                                    await store.reloadWorkspaces()
                                    if activateOnCreate {
                                        await store.switchWorkspace(targetDir)
                                    }
                                } catch {
                                    store.errorMessage = error.localizedDescription
                                }
                                isCreating = false
                            }
                        } label: {
                            if isCreating {
                                ProgressView().controlSize(.small)
                            } else {
                                Label(lang.tr("一键初始化工作区", "Scaffold Workspace"), systemImage: "plus.rectangle.on.folder.fill")
                            }
                        }
                        .buttonStyle(.borderedProminent)
                        .disabled(targetDir.trimmingCharacters(in: .whitespaces).isEmpty || isCreating)
                    }
                }
            }
            .frame(maxWidth: .infinity)

            blueprintCard
                .frame(width: 335)
        }
        .sheet(isPresented: $showPreview) { previewSheet }
    }

    private var blueprintCard: some View {
        SurfaceCard(padding: 14) {
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Image(systemName: "square.split.bottomrightquarter").foregroundStyle(Color.accentColor)
                    Text(lang.tr("零噪声目录蓝图", "Zero-Noise Layout Blueprint")).font(.system(size: 13.5, weight: .bold))
                }
                VStack(alignment: .leading, spacing: 5) {
                    blueprintRow("opsb.workspace.json", lang.tr("工作区配置", "Manifest"), 0, "doc.badge.gearshape")
                    blueprintRow("AGENTS.md", lang.tr("opsb 协议", "CLI Protocol"), 0, "sparkles")
                    blueprintRow("breakthrough/", lang.tr("结构化经验库", "Playbook"), 0, "flame.fill")
                    blueprintRow("<Operator>/", lang.tr("纯算子目录", "Problem Scope"), 0, "folder.fill")
                    blueprintRow("PROBLEM.md", lang.tr("精度与规格", "Math & Tol"), 1, "function")
                    blueprintRow("op_host/ & op_kernel/", lang.tr("源码范围", "Editable C++"), 1, "cpu")
                    blueprintRow("tests/", lang.tr("扩展测例", "Extended Cases"), 1, "checkmark.shield")
                    blueprintRow("DESIGN.md", lang.tr("Tiling 设计", "Tiling & UB"), 1, "ruler")
                    blueprintRow("AGENTS.md", lang.tr("零 Skill 闭环", "Zero-Skill Loop"), 1, "terminal")
                }
                .padding(10)
                .background(RoundedRectangle(cornerRadius: 8).fill(Color.primary.opacity(0.03)))

                Divider()
                Text(lang.tr(
                    "SSH Die 锁 · profiler · 四阶门禁 · 190s 限流已全部收敛于全局 `opsb`。",
                    "SSH Die Lock · profiler · 4-Stage Gate · 190s Pacer encapsulated in global `opsb`."
                ))
                .font(.system(size: 10.5))
                .foregroundStyle(.secondary)
            }
        }
    }

    private func labeledField<C: View>(_ label: String, @ViewBuilder content: () -> C) -> some View {
        let built = content()
        return VStack(alignment: .leading, spacing: 4) {
            Text(label).font(.system(size: 11, weight: .medium)).foregroundStyle(.secondary)
            built
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func blueprintRow(_ name: String, _ tag: String, _ indent: Int, _ icon: String) -> some View {
        HStack(spacing: 6) {
            if indent > 0 {
                Text("└─").font(.system(size: 11, design: .monospaced)).foregroundStyle(.secondary)
            }
            Image(systemName: icon).font(.system(size: 10.5)).foregroundStyle(Color.accentColor).frame(width: 14)
            Text(name).font(.system(size: 11, weight: .medium, design: .monospaced))
            Spacer()
            Text(tag)
                .font(.system(size: 9.5, weight: .medium))
                .foregroundStyle(.secondary)
                .padding(.horizontal, 5)
                .padding(.vertical, 1.5)
                .background(Capsule().fill(Color.primary.opacity(0.06)))
        }
    }

    private func pickFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        if panel.runModal() == .OK, let url = panel.url {
            targetDir = url.appendingPathComponent("accel-contest").path
        }
    }

    private var previewSheet: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Label(lang.tr("自动生成的 AGENTS.md 协议预览", "Generated AGENTS.md Protocol"), systemImage: "doc.text.fill").font(.headline)
                Spacer()
                Button(lang.tr("关闭", "Close")) { showPreview = false }.keyboardShortcut(.defaultAction)
            }
            Divider()
            ScrollView {
                Text("""
                # <Operator> · Agent Guide (AGENTS.md-First)

                1. Edit ONLY `op_host/`, `op_kernel/`, `tests/`, `DESIGN.md`, `SCORE_HISTORY.md`.
                2. Review prior verified breakthroughs first:
                   $ opsb breakthrough list
                3. Commit & push to `\(gitOwner)/\(repoPrefix)-<slug>`, then validate on NPU (`\(npuHost)`):
                   $ opsb test <Operator> --fast
                   $ opsb test <Operator>
                4. Inspect 50-case ratios & enqueue into the 4-Stage Gate:
                   $ opsb history <Operator>
                   $ opsb submit <Operator> $(git rev-parse HEAD) --note "tile + dbuf speedup"
                """)
                .font(.system(size: 12, design: .monospaced))
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(10)
            }
            .background(RoundedRectangle(cornerRadius: 8).fill(Color.primary.opacity(0.04)))
        }
        .padding(16)
        .frame(width: 600, height: 360)
    }
}
