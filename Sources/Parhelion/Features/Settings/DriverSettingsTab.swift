import SwiftUI

struct DriverSettingsTab: View {
    @EnvironmentObject var store: ContestStore
    @UIState private var isInstallingSkills: Bool = false

    private var lang: AppLanguage { store.lang }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            headerCard
            ScrollView {
                LazyVStack(spacing: 8) {
                    ForEach(store.driverDiagnostics) { diag in
                        driverRowCard(diag)
                    }
                    fallbackSkillsCard
                }
            }
        }
        .onAppear {
            if store.driverDiagnostics.isEmpty {
                Task { await store.reloadDriverDoctor() }
            }
        }
    }

    private var headerCard: some View {
        SurfaceCard(padding: 12) {
            HStack(spacing: 10) {
                Image(systemName: "terminal.fill")
                    .font(.system(size: 20))
                    .foregroundStyle(Color.accentColor)
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 6) {
                        Text(lang.tr("统一 Harness 驱动控制", "Unified Harness Driver"))
                            .font(.system(size: 13.5, weight: .bold))
                        SemanticPill(text: store.activeDriver.uppercased(), compact: true)
                    }
                    Text(lang.tr(
                        "选择默认调优对话 Harness（GUI Deep-Link / Headless CLI），并一键向当前工作区投影 Skills 与 MCP。",
                        "Select active Harness Driver (GUI Deep-Link / Headless CLI) and sync isolated workspace Skills & MCP."
                    ))
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                }
                Spacer()
                Button {
                    Task { await store.reloadDriverDoctor() }
                } label: {
                    if store.isLoadingDrivers {
                        ProgressView().controlSize(.small)
                    } else {
                        Label(lang.tr("刷新 Doctor", "Doctor"), systemImage: "stethoscope")
                            .font(.system(size: 11))
                    }
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .disabled(store.isLoadingDrivers)

                Button {
                    Task { await store.syncActiveDriverWorkspace() }
                } label: {
                    if store.isSyncingDriver {
                        ProgressView().controlSize(.small)
                    } else {
                        Label(lang.tr("同步工作区", "Sync Workspace"), systemImage: "arrow.triangle.2.circlepath")
                            .font(.system(size: 11, weight: .semibold))
                    }
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
                .disabled(store.isSyncingDriver || store.selectedWorkspace.isEmpty)
            }
        }
    }

    private func driverRowCard(_ diag: DriverDiagnosticModel) -> some View {
        let isActive = diag.active || diag.kind == store.activeDriver
        return SurfaceCard(highlighted: isActive, highlightColor: .accentColor, padding: 11) {
            HStack(alignment: .top, spacing: 10) {
                VStack(alignment: .leading, spacing: 5) {
                    HStack(spacing: 6) {
                        Text(diag.displayName)
                            .font(.system(size: 13, weight: .bold))
                        if isActive {
                            SemanticPill(text: lang.tr("当前默认", "ACTIVE"), compact: true)
                        }
                        SemanticPill(
                            text: diag.installed ? "OK" : lang.tr("未安装", "MISSING"),
                            compact: true
                        )
                        if diag.installed {
                            SemanticPill(
                                text: diag.authOk ? "AUTH" : "NOAUTH",
                                compact: true
                            )
                        }
                        if !diag.protocolName.isEmpty {
                            Text(diag.protocolName)
                                .font(.system(size: 10, weight: .medium, design: .monospaced))
                                .foregroundStyle(.secondary)
                                .padding(.horizontal, 6)
                                .padding(.vertical, 2)
                                .background(Capsule().fill(Color.primary.opacity(0.06)))
                        }
                    }

                    HStack(spacing: 8) {
                        if let guiLabel = diag.guiDisplayLabel {
                            let schemeText = diag.urlScheme.isEmpty ? "" : " · \(diag.urlScheme)://"
                            let verText = diag.guiVersion.isEmpty ? "" : " v\(diag.guiVersion)"
                            HStack(spacing: 4) {
                                Image(systemName: "macwindow")
                                    .font(.system(size: 9.5))
                                Text("\(guiLabel)\(verText)\(schemeText)")
                                    .font(.system(size: 10.5, weight: .semibold, design: .monospaced))
                            }
                            .foregroundStyle(.teal)
                            .padding(.horizontal, 6)
                            .padding(.vertical, 2)
                            .background(RoundedRectangle(cornerRadius: 5).fill(Color.teal.opacity(0.12)))
                        }

                        if !diag.version.isEmpty {
                            Text(diag.version)
                                .font(.system(size: 10.5, design: .monospaced))
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                        }
                    }

                    Text(diag.detail)
                        .font(.system(size: 10.5))
                        .foregroundStyle(.secondary)
                        .lineLimit(2)

                    if !diag.binaryPath.isEmpty {
                        Text(diag.binaryPath)
                            .font(.system(size: 9.5, design: .monospaced))
                            .foregroundStyle(.tertiary)
                            .lineLimit(1)
                            .truncationMode(.middle)
                    }
                }

                Spacer()

                VStack(alignment: .trailing, spacing: 6) {
                    if !isActive {
                        Button(lang.tr("设为默认", "Select")) {
                            Task { await store.selectHarnessDriver(diag.kind) }
                        }
                        .buttonStyle(.borderedProminent)
                        .controlSize(.small)
                    }
                    Button(lang.tr("同步 MCP", "Sync")) {
                        Task { await store.syncActiveDriverWorkspace(driver: diag.kind) }
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.mini)
                    .disabled(store.isSyncingDriver || store.selectedWorkspace.isEmpty)
                }
            }
        }
    }

    private var fallbackSkillsCard: some View {
        let dshOk = store.snapshot?.skills.targets["dsh"]?.allInstalled ?? false
        let codexOk = store.snapshot?.skills.targets["codex"]?.allInstalled ?? false
        return SurfaceCard(padding: 11) {
            HStack(spacing: 10) {
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 6) {
                        Text(lang.tr("兜底参考 Skill (`kernelc-deep-reference`)", "Fallback Skill (`kernelc-deep-reference`)"))
                            .font(.system(size: 12.5, weight: .bold))
                        SemanticPill(text: dshOk ? "dsh OK" : "dsh —", compact: true)
                        SemanticPill(text: codexOk ? "Codex OK" : "Codex —", compact: true)
                    }
                    Text(lang.tr(
                        "soc-a 硬件对齐与 PipeBarrier 手册；常规已由 `AGENTS.md` + MCP + 四元知识库覆盖，按需注册至 `~/.dsh/skills` 与 `~/.codex/skills`。",
                        "Optional soc-a reference manual; standard workflows use `AGENTS.md` + MCP + 4-Pillar KB."
                    ))
                    .font(.system(size: 10.5))
                    .foregroundStyle(.secondary)
                }
                Spacer()
                Button {
                    isInstallingSkills = true
                    Task {
                        do {
                            try await store.repository.installFallbackSkills()
                            store.bannerMessage = lang.tr("已注册兜底 Skill 至 dsh / Codex", "Registered fallback skill to dsh / Codex")
                            await store.refreshLocal()
                        } catch {
                            store.errorMessage = error.localizedDescription
                        }
                        isInstallingSkills = false
                    }
                } label: {
                    if isInstallingSkills {
                        ProgressView().controlSize(.small)
                    } else {
                        Label(lang.tr("注册 Skill", "Register"), systemImage: "square.and.arrow.down")
                            .font(.system(size: 11))
                    }
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .disabled(isInstallingSkills)
            }
        }
    }
}
