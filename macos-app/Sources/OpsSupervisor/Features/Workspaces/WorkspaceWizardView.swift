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

    var body: some View {
        HStack(alignment: .top, spacing: 16) {
            SurfaceCard(padding: 14) {
                VStack(alignment: .leading, spacing: 12) {
                    HStack {
                        Label("New Clean Contest Workspace", systemImage: "sparkles.rectangle.stack.fill")
                            .font(.system(size: 15, weight: .bold))
                        Spacer()
                        SemanticPill(text: "AGENTS.md-First", compact: true)
                    }

                    VStack(alignment: .leading, spacing: 8) {
                        labeledField("Target Directory") {
                            HStack(spacing: 6) {
                                TextField("~/Competitions/...", text: $targetDir)
                                    .textFieldStyle(.roundedBorder)
                                    .font(.system(size: 12, design: .monospaced))
                                Button("Browse…", action: pickFolder)
                                    .controlSize(.small)
                            }
                        }

                        HStack(spacing: 10) {
                            labeledField("Contest Title") {
                                TextField("Contest Title", text: $contestTitle).textFieldStyle(.roundedBorder)
                            }
                            labeledField("Contest ID (Optional)") {
                                TextField("6a9e2d...", text: $contestId)
                                    .textFieldStyle(.roundedBorder)
                                    .font(.system(size: 12, design: .monospaced))
                            }
                        }

                        HStack(spacing: 10) {
                            labeledField("GitCode Owner") {
                                TextField("winbeau", text: $gitOwner)
                                    .textFieldStyle(.roundedBorder)
                                    .font(.system(size: 12, design: .monospaced))
                            }
                            labeledField("Private Repo Prefix") {
                                TextField("accel-ops", text: $repoPrefix)
                                    .textFieldStyle(.roundedBorder)
                                    .font(.system(size: 12, design: .monospaced))
                            }
                            labeledField("NPU SSH Alias") {
                                TextField("npu", text: $npuHost)
                                    .textFieldStyle(.roundedBorder)
                                    .font(.system(size: 12, design: .monospaced))
                            }
                        }

                        labeledField("Operators & Difficulty Weights (Name:Weight)") {
                            TextField("OpA:3, OpB:2", text: $problemsSpec)
                                .textFieldStyle(.roundedBorder)
                                .font(.system(size: 12, design: .monospaced))
                        }
                    }

                    HStack {
                        Toggle("Switch active workspace after creation", isOn: $activateOnCreate)
                            .toggleStyle(.checkbox)
                            .font(.system(size: 12))

                        Spacer()

                        Button("Preview AGENTS.md") { showPreview = true }
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
                                    store.bannerMessage = "Scaffolded clean workspace at \(targetDir)"
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
                                Label("Scaffold Workspace", systemImage: "plus.rectangle.on.folder.fill")
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
                    Text("Zero-Noise Layout Blueprint").font(.system(size: 13.5, weight: .bold))
                }
                VStack(alignment: .leading, spacing: 5) {
                    blueprintRow("opsb.workspace.json", "Manifest", 0, "doc.badge.gearshape")
                    blueprintRow("AGENTS.md", "opsb CLI Protocol", 0, "sparkles")
                    blueprintRow("<Operator>/", "Pure Problem Scope", 0, "folder.fill")
                    blueprintRow("PROBLEM.md", "Math & Tol", 1, "function")
                    blueprintRow("op_host/ & op_kernel/", "Editable C++", 1, "cpu")
                    blueprintRow("tests/", "Extended Cases", 1, "checkmark.shield")
                    blueprintRow("DESIGN.md", "Tiling & UB", 1, "ruler")
                    blueprintRow("SCORE_HISTORY.md", "Score Ledger", 1, "chart.xyaxis.line")
                    blueprintRow("AGENTS.md", "Zero-Skill Loop", 1, "terminal")
                }
                .padding(10)
                .background(RoundedRectangle(cornerRadius: 8).fill(Color.primary.opacity(0.03)))

                Divider()
                Text("SSH Die Lock · profiler · 4-Stage Gate · 190s Pacer encapsulated in global `opsb`.")
                    .font(.system(size: 10.5))
                    .foregroundStyle(.secondary)
            }
        }
    }

    private func labeledField<C: View>(_ label: String, @ViewBuilder content: () -> C) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label).font(.system(size: 11, weight: .medium)).foregroundStyle(.secondary)
            content()
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
                Label("Generated AGENTS.md Protocol", systemImage: "doc.text.fill").font(.headline)
                Spacer()
                Button("Close") { showPreview = false }.keyboardShortcut(.defaultAction)
            }
            Divider()
            ScrollView {
                Text("""
                # <Operator> · Agent Guide (AGENTS.md-First)

                1. Edit ONLY `op_host/`, `op_kernel/`, `tests/`, `DESIGN.md`, `SCORE_HISTORY.md`.
                2. Commit & push to `\(gitOwner)/\(repoPrefix)-<slug>`.
                3. Validate on NPU (`\(npuHost)`):
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
        .frame(width: 600, height: 340)
    }
}
