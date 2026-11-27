import SwiftUI

struct DriverSettingsTab: View {
    @EnvironmentObject var store: ContestStore
    @State private var modelEdits: [String: String] = [:]

    private var lang: AppLanguage { store.lang }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            headerCard
            ScrollView {
                LazyVStack(spacing: 8) {
                    ForEach(store.driverDiagnostics) { diag in
                        driverRowCard(diag)
                    }
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

                    DriverSuspensionBanner(driver: diag.kind)

                    Text(diag.detail)
                        .font(.system(size: 10.5))
                        .foregroundStyle(.secondary)
                        .lineLimit(2)

                    if let selected = diag.workerModel {
                        let model = Binding<String>(
                            get: { modelEdits[diag.kind] ?? selected },
                            set: { modelEdits[diag.kind] = $0 }
                        )
                        HStack {
                            Text(lang.tr("Worker 模型", "Worker model")).font(.caption)
                            TextField("Model", text: model).textFieldStyle(.roundedBorder)
                                .frame(maxWidth: 220)
                            Button {
                                Task { await store.setWorkerModel(model.wrappedValue, driver: diag.kind) }
                            } label: { Image(systemName: "checkmark") }
                            .help(lang.tr("保存模型", "Save model"))
                            .disabled(model.wrappedValue == selected || model.wrappedValue.isEmpty)
                        }
                    }

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
                    // 4.4 P34: only ticked Harnesses may run workers and doc scouts.
                    Toggle(lang.tr("可用", "Available"), isOn: Binding(
                        get: { diag.available },
                        set: { on in Task { await store.setDriverAvailable(diag.kind, on) } }
                    ))
                    .toggleStyle(.checkbox)
                    .controlSize(.small)
                    .help(lang.tr("勾选后主控才能用这个 Harness 开 worker 与文档侦察席",
                                  "Only ticked Harnesses may run workers and doc scouts"))
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
}
