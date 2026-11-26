import SwiftUI

struct SSHSettingsTab: View {
    @EnvironmentObject var store: ContestStore

    @UIState private var editAlias: String = ""
    @UIState private var editHost: String = ""
    @UIState private var editPort: String = "22003"
    @UIState private var editUser: String = "developer"
    @UIState private var editIdentity: String = ""
    @UIState private var writeSSHConfig: Bool = false
    @UIState private var testResult: SSHTestResult?
    @UIState private var isTesting: Bool = false
    @UIState private var isDeploying: Bool = false

    private var lang: AppLanguage { store.lang }

    var body: some View {
        HSplitView {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Text(lang.tr("主机列表 (~/.ssh/config)", "Hosts (~/.ssh/config)")).font(.system(size: 12, weight: .bold))
                    Spacer()
                    Button { Task { await store.reloadSSHHosts() } } label: {
                        Image(systemName: "arrow.clockwise").font(.system(size: 11))
                    }
                    .buttonStyle(.plain)
                }

                List(store.sshHosts) { host in
                    let isPrimary = host.alias == store.activeSSHHost
                    let inPool = isPrimary || store.sshPool.contains(host.alias)
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            HStack(spacing: 6) {
                                Text(host.alias).font(.system(size: 12.5, weight: .bold, design: .monospaced))
                                if isPrimary {
                                    SemanticPill(text: lang.tr("主", "MAIN"), compact: true)
                                }
                                if inPool && store.sshPool.count > 1 {
                                    SemanticPill(text: lang.tr("设备池", "POOL"), compact: true)
                                }
                            }
                            Text("\(host.user)@\(host.hostname):\(host.port)")
                                .font(.system(size: 10.5, design: .monospaced))
                                .foregroundStyle(.secondary)
                        }
                        Spacer()
                        Toggle(lang.tr("设备池", "Pool"), isOn: Binding(
                            get: { inPool },
                            set: { member in Task { await store.setDevicePool(host.alias, member: member) } }
                        ))
                        .toggleStyle(.checkbox)
                        .controlSize(.mini)
                        .font(.system(size: 10.5))
                        .disabled(isPrimary)
                        .help(isPrimary
                              ? lang.tr("主主机始终在设备池里；要移出它，先把别的主机设为主。",
                                        "The main host is always in the pool; make another host main first.")
                              : lang.tr("勾选后加入设备池：守护进程为它开遥测流，题目按清单顺序轮流固定到池内各台设备上测试。增减设备会重新分配题目，估分只在同一台设备内配对比较。",
                                        "Pooled hosts get a telemetry stream, and problems are pinned to pool devices in roster order. Changing the pool reassigns problems; estimates pair runs only within one device."))
                        Button(lang.tr("设为主", "Make Main")) {
                            populateForm(host)
                            Task { await store.selectSSHHost(host.alias) }
                        }
                        .controlSize(.mini)
                        .disabled(isPrimary)
                        .help(lang.tr("主主机是仪表盘默认显示的设备；没有设备池时所有测试都在它上面运行。更换主主机不会把题目换到别的设备。",
                                      "The main host is the one the dashboard shows first and, without a pool, runs every test. Changing it moves no problem to another device."))
                    }
                    .contentShape(Rectangle())
                    .onTapGesture { populateForm(host) }
                }
                .listStyle(.bordered(alternatesRowBackgrounds: true))
            }
            .frame(minWidth: 245, idealWidth: 275)
            .padding(.trailing, 8)

            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Text(lang.tr("NPU 主机连接参数", "NPU Host Details")).font(.system(size: 13, weight: .bold))
                    Spacer()
                    SemanticPill(text: "\(lang.tr("主", "Main")): \(store.activeSSHHost)", compact: true)
                    if store.sshPool.count > 1 {
                        SemanticPill(text: "\(lang.tr("设备池", "Pool")): \(store.sshPool.joined(separator: lang.tr("、", ", ")))", compact: true)
                    }
                }

                Grid(alignment: .leading, horizontalSpacing: 10, verticalSpacing: 8) {
                    GridRow {
                        Text(lang.tr("主机别名", "Alias")).font(.system(size: 11)).foregroundStyle(.secondary)
                        TextField("npu", text: $editAlias).textFieldStyle(.roundedBorder)
                    }
                    GridRow {
                        Text(lang.tr("地址 / IP", "HostName / IP")).font(.system(size: 11)).foregroundStyle(.secondary)
                        TextField("124.71.x.x", text: $editHost).textFieldStyle(.roundedBorder)
                    }
                    GridRow {
                        Text(lang.tr("端口与用户", "Port & User")).font(.system(size: 11)).foregroundStyle(.secondary)
                        HStack(spacing: 8) {
                            TextField("22", text: $editPort).textFieldStyle(.roundedBorder).frame(width: 80)
                            TextField("developer", text: $editUser).textFieldStyle(.roundedBorder)
                        }
                    }
                    GridRow {
                        Text(lang.tr("私钥路径", "IdentityFile")).font(.system(size: 11)).foregroundStyle(.secondary)
                        TextField("~/.ssh/id_ed25519", text: $editIdentity).textFieldStyle(.roundedBorder)
                    }
                }

                Toggle(lang.tr("同时写入受管 Host 块至 ~/.ssh/config", "Write managed Host block to ~/.ssh/config"), isOn: $writeSSHConfig)
                    .toggleStyle(.checkbox)
                    .font(.system(size: 11))

                HStack(spacing: 8) {
                    Button(lang.tr("保存并设为主", "Save as Main")) {
                        guard !editAlias.isEmpty, !editHost.isEmpty else { return }
                        Task {
                            do {
                                try await store.repository.saveSSHHost(
                                    alias: editAlias,
                                    hostname: editHost,
                                    port: Int(editPort) ?? 22,
                                    user: editUser,
                                    identityFile: editIdentity,
                                    writeConfig: writeSSHConfig,
                                    activate: true
                                )
                                await store.reloadSSHHosts()
                                await store.refreshNpu(silent: false)
                            } catch {
                                store.errorMessage = error.localizedDescription
                            }
                        }
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(editAlias.isEmpty || editHost.isEmpty)

                    Button {
                        let target = editAlias.isEmpty ? store.activeSSHHost : editAlias
                        isTesting = true
                        Task {
                            testResult = try? await store.repository.testSSH(target)
                            isTesting = false
                        }
                    } label: {
                        if isTesting { ProgressView().controlSize(.small) }
                        else { Label(lang.tr("测试连接", "Test Connection"), systemImage: "bolt.horizontal.fill") }
                    }
                    .buttonStyle(.bordered)

                    Button {
                        let target = editAlias.isEmpty ? store.activeSSHHost : editAlias
                        isDeploying = true
                        Task {
                            do {
                                try await store.repository.deployNpuProbe(target)
                                store.bannerMessage = lang.tr("已部署 ~/.opsb/npu_probe.py 至 \(target)", "Deployed ~/.opsb/npu_probe.py to \(target)")
                                await store.refreshNpu(silent: false)
                            } catch {
                                store.errorMessage = error.localizedDescription
                            }
                            isDeploying = false
                        }
                    } label: {
                        if isDeploying { ProgressView().controlSize(.small) }
                        else { Label(lang.tr("部署遥测探针", "Deploy Probe"), systemImage: "icloud.and.arrow.up") }
                    }
                    .buttonStyle(.bordered)
                }

                if let res = testResult {
                    HStack(spacing: 8) {
                        SemanticPill(text: res.ok ? "OK · \(Int(res.latencyMs))ms" : "FAILED", compact: true)
                        Text(res.info).font(.system(size: 11, design: .monospaced)).foregroundStyle(.secondary).lineLimit(2)
                    }
                    .padding(8)
                    .background(RoundedRectangle(cornerRadius: 7).fill(Color.primary.opacity(0.04)))
                }

                Spacer()
            }
            .padding(.leading, 10)
        }
        .onAppear {
            if editAlias.isEmpty, let cur = store.sshHosts.first(where: { $0.alias == store.activeSSHHost }) ?? store.sshHosts.first {
                populateForm(cur)
            }
        }
    }

    private func populateForm(_ host: SSHHostEntry) {
        editAlias = host.alias
        editHost = host.hostname
        editPort = "\(host.port)"
        editUser = host.user
        editIdentity = host.identityFile
    }
}
