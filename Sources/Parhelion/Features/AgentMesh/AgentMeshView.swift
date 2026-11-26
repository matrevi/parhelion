import SwiftUI

struct AgentMeshView: View {
    @EnvironmentObject var store: ContestStore
    @EnvironmentObject var hotStore: HotTelemetryStore

    @UIState private var grouping: AgentMeshGrouping = .byProblem
    @UIState private var selection: AgentMeshSelection = .orchestrator
    @UIState private var messages: [AgentBusMessage] = []
    @UIState private var messageScope = ""
    @UIState private var diffStat: String = ""
    @UIState private var isSending: Bool = false
    @UIState private var showSpawnSheet: Bool = false
    @UIState private var preselectedDriver: String = ""
    @UIState private var preselectedProblem: String = ""

    private var lang: AppLanguage { store.lang }

    private var activeMesh: AgentMeshHotState {
        if let live = hotStore.agentMesh { return live }
        let ready = store.driverDiagnostics
            .filter { $0.kind != store.activeDriver }
            .map {
                AgentReadyDriverHot(
                    kind: $0.kind, displayName: $0.displayName, protocolName: $0.protocolName,
                    guiAppPath: $0.guiAppPath, installed: $0.installed, authOk: $0.authOk, hasCli: !$0.binaryPath.isEmpty
                )
            }
        return AgentMeshHotState(
            orchestrator: AgentOrchestratorHot(
                driver: store.activeDriver,
                displayName: store.activeDriverDiagnostic?.displayName ?? store.activeDriver.uppercased(),
                protocolName: store.activeDriverDiagnostic?.protocolName ?? "",
                guiAppPath: store.activeDriverDiagnostic?.guiAppPath ?? "",
                deepLink: fallbackDeepLink(for: store.activeDriver),
                sessionTitle: URL(fileURLWithPath: store.selectedWorkspace).lastPathComponent,
                workspaceName: URL(fileURLWithPath: store.selectedWorkspace).lastPathComponent,
                activeWorkers: 0,
                nativeSubagents: 0,
                squadCount: 0,
                status: "standby"
            ),
            readyDrivers: ready,
            nativeSubagents: [],
            squads: [],
            strategicAlerts: [],
            workers: [],
            pulses: [],
            latestSeq: 0,
            ts: Date().timeIntervalSince1970
        )
    }

    private var selectionKey: String {
        switch selection {
        case .orchestrator: return "ORCH"
        case .squad(let p): return "SQUAD::\(p)"
        case .worker(let w): return "WORKER::\(w)"
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            topControlBar
            DriverSuspensionBanner()
                .padding(.horizontal, 14)
                .padding(.vertical, (hotStore.agentMesh?.suspensions.isEmpty ?? true) ? 0 : 6)
            Divider()
            HStack(spacing: 0) {
                AgentMeshCanvasView(
                    mesh: activeMesh,
                    lang: lang,
                    grouping: grouping,
                    selection: selection,
                    onSelect: { selection = $0 },
                    onQuickSpawn: { drv, prob in
                        preselectedDriver = drv
                        preselectedProblem = prob
                        showSpawnSheet = true
                    },
                    onJumpOrchestrator: { jumpToOrchestrator() },
                    onJumpSubagent: { jumpToSubagent($0) },
                    onJumpWorker: { jumpToWorker($0) }
                )
                .frame(maxWidth: .infinity, maxHeight: .infinity)

                Divider()

                AgentMeshInspectorView(
                    mesh: activeMesh,
                    lang: lang,
                    selection: selection,
                    messages: messages,
                    diffStat: diffStat,
                    isSending: isSending,
                    onSendWorkerMessage: { wid, text, delivery in
                        Task { await send(agentId: wid, text: text, delivery: delivery) }
                    },
                    onSendAdminDirective: { body, prob, broadcast in
                        Task { await dispatchDirective(body: body, problem: prob, broadcast: broadcast) }
                    },
                    onStopWorker: { wid, cleanup in
                        Task { await stop(agentId: wid, cleanupWorktree: cleanup) }
                    },
                    onJumpOrchestrator: { jumpToOrchestrator() },
                    onJumpWorker: { jumpToWorker($0) },
                    onSelect: { selection = $0 }
                )
                .frame(width: 326)
            }
        }
        .task(id: "\(store.selectedWorkspace)::\(selectionKey)::\(activeMesh.latestSeq)") {
            await reloadMessages()
        }
        .onAppear {
            Task {
                if store.driverDiagnostics.isEmpty { await store.reloadDriverDoctor() }
                await refreshMeshOnce()
            }
        }
        .sheet(isPresented: $showSpawnSheet) {
            SpawnExternalWorkerSheet(
                lang: lang,
                eligibleDrivers: activeMesh.readyDrivers.filter { $0.installed && $0.hasCli },
                problems: store.snapshot?.problems.map(\.title) ?? [],
                initialDriver: preselectedDriver,
                initialProblem: preselectedProblem,
                onDismiss: { showSpawnSheet = false },
                onSpawn: { drv, role, prob, mode, axis, wtMode, task in
                    showSpawnSheet = false
                    Task {
                        await spawn(
                            driver: drv, role: role, problem: prob,
                            collabMode: mode, axis: axis, worktreeMode: wtMode, task: task
                        )
                    }
                }
            )
        }
    }

    private var topControlBar: some View {
        let orchStyle = AgentDriverStyle.forDriver(activeMesh.orchestrator.driver)
        return HStack(spacing: 10) {
            Menu {
                ForEach(store.driverDiagnostics) { diag in
                    Button {
                        Task {
                            await store.selectHarnessDriver(diag.kind)
                            await refreshMeshOnce()
                        }
                    } label: {
                        HStack {
                            Text("\(diag.displayName) (\(diag.kind))")
                            if diag.kind == activeMesh.orchestrator.driver { Image(systemName: "checkmark") }
                        }
                    }
                }
            } label: {
                HStack(spacing: 6) {
                    HarnessLogoView(driver: activeMesh.orchestrator.driver, appPath: activeMesh.orchestrator.guiAppPath, size: 17)
                    Text(activeMesh.orchestrator.displayName)
                        .font(.system(size: 11.5, weight: .bold))
                    Image(systemName: "chevron.down")
                        .font(.system(size: 8.5))
                        .foregroundStyle(.secondary)
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 4.5)
                .background(RoundedRectangle(cornerRadius: 7).fill(orchStyle.accent.opacity(0.11)))
            }
            .buttonStyle(.plain)

            HStack(spacing: 5) {
                Circle().fill(hotStore.daemonOnline ? Color.green : Color.orange).frame(width: 7, height: 7)
                Text(hotStore.daemonOnline ? "MCP BUS · LIVE" : "MCP BUS · OFFLINE")
                    .font(.system(size: 10, weight: .bold, design: .monospaced))
                    .foregroundStyle(.secondary)
            }

            Spacer()

            Picker("", selection: $grouping) {
                Text(lang.tr("按赛题", "By Problem")).tag(AgentMeshGrouping.byProblem)
                Text(lang.tr("按 Harness", "By Harness")).tag(AgentMeshGrouping.byHarness)
            }
            .pickerStyle(.segmented)
            .controlSize(.small)
            .frame(width: 172)

            if activeMesh.workers.contains(where: { !$0.isActive }) {
                Button {
                    Task { await cleanupStopped() }
                } label: {
                    Label(lang.tr("清理", "Prune"), systemImage: "trash")
                        .font(.system(size: 10.5))
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
            }

            Button {
                preselectedDriver = activeMesh.readyDrivers.first(where: { $0.installed && $0.hasCli })?.kind ?? ""
                preselectedProblem = ""
                showSpawnSheet = true
            } label: {
                Label(lang.tr("派发 Worker", "Spawn"), systemImage: "plus.circle.fill")
                    .font(.system(size: 11, weight: .semibold))
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.small)
            .disabled(activeMesh.readyDrivers.allSatisfy { !$0.installed || !$0.hasCli })
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
    }

    private func refreshMeshOnce() async {
        guard !store.selectedWorkspace.isEmpty else { return }
        if let m = try? await store.daemonClient.fetchAgentMesh(workspace: store.selectedWorkspace, host: store.activeSSHHost) {
            hotStore.agentMesh = m
        }
    }

    private func reloadMessages() async {
        let workspace = store.selectedWorkspace
        let scope = "\(workspace)::\(selectionKey)"
        if scope != messageScope {
            messageScope = scope
            messages = []
            diffStat = ""
        }
        guard !store.selectedWorkspace.isEmpty, hotStore.daemonOnline else { return }
        let agentId: String? = {
            if case .worker(let wid) = selection { return wid }
            return nil
        }()
        let problem: String? = {
            if case .squad(let p) = selection { return p }
            return nil
        }()
        while !Task.isCancelled {
            let cursor = messages.last?.seq ?? 0
            guard let resp = try? await store.daemonClient.fetchAgentMessages(
                workspace: workspace, host: store.activeSSHHost,
                agentId: agentId, problem: problem, sinceSeq: cursor, ack: false
            ), !Task.isCancelled, workspace == store.selectedWorkspace,
               scope == "\(workspace)::\(selectionKey)", messageScope == scope else { return }
            if resp.restartsHistory(after: cursor) {
                // A full cleanup emptied the bus and its sequence started again at 1.
                messages = resp.messages
            } else {
                let last = messages.last?.seq ?? 0
                messages.append(contentsOf: resp.messages.filter { $0.seq > last })
            }
            diffStat = resp.diffStat ?? ""
            if resp.messages.count < 80 || (messages.last?.seq ?? 0) >= resp.latestSeq { return }
        }
    }

    private func spawn(
        driver: String, role: String, problem: String,
        collabMode: String, axis: String, worktreeMode: String, task: String
    ) async {
        isSending = true
        defer { isSending = false }
        do {
            try await store.daemonClient.spawnExternalAgent(
                workspace: store.selectedWorkspace, host: store.activeSSHHost,
                driver: driver, roleLabel: role, problem: problem,
                collabMode: collabMode, axis: axis, task: task, worktreeMode: worktreeMode
            )
            await refreshMeshOnce()
            if let newest = hotStore.agentMesh?.workers.first { selection = .worker(newest.agentId) }
            await reloadMessages()
        } catch {
            store.errorMessage = error.localizedDescription
        }
    }

    private func send(agentId: String, text: String, delivery: String) async {
        isSending = true
        defer { isSending = false }
        do {
            try await store.daemonClient.sendAgentMessage(
                workspace: store.selectedWorkspace, host: store.activeSSHHost,
                agentId: agentId, message: text, delivery: delivery
            )
            await reloadMessages()
        } catch {
            store.errorMessage = error.localizedDescription
        }
    }

    private func dispatchDirective(body: String, problem: String, broadcast: Bool) async {
        isSending = true
        defer { isSending = false }
        do {
            try await store.daemonClient.sendAdminDirective(
                workspace: store.selectedWorkspace, host: store.activeSSHHost,
                body: body, problem: problem, broadcastWorkers: broadcast
            )
            await refreshMeshOnce()
            await reloadMessages()
        } catch {
            store.errorMessage = error.localizedDescription
        }
    }

    private func stop(agentId: String, cleanupWorktree: Bool) async {
        do {
            try await store.daemonClient.stopAgent(
                workspace: store.selectedWorkspace, host: store.activeSSHHost,
                agentId: agentId, cleanupWorktree: cleanupWorktree
            )
            await refreshMeshOnce()
            await reloadMessages()
        } catch {
            store.errorMessage = error.localizedDescription
        }
    }

    private func cleanupStopped() async {
        do {
            _ = try await store.daemonClient.cleanupStoppedAgents(workspace: store.selectedWorkspace, host: store.activeSSHHost)
            if case .worker(let wid) = selection,
               !(hotStore.agentMesh?.workers.contains(where: { $0.agentId == wid && $0.isActive }) ?? false) {
                selection = .orchestrator
            }
            await refreshMeshOnce()
            await reloadMessages()
        } catch {
            store.errorMessage = error.localizedDescription
        }
    }

    private func fallbackDeepLink(for driver: String) -> String {
        switch driver.lowercased() {
        case "antigravity": return "antigravity://open"
        case "codex": return "codex://open"
        case "deepseek": return "dsh://open"
        case "claude": return "claude://"
        default: return ""
        }
    }

    private func jumpToOrchestrator() {
        let orch = activeMesh.orchestrator
        let dl = orch.deepLink.isEmpty ? fallbackDeepLink(for: orch.driver) : orch.deepLink
        if let msg = SystemNavigator.jumpToHarnessContext(
            driver: orch.displayName,
            deepLink: dl,
            guiAppPath: orch.guiAppPath,
            clipboardHint: orch.sessionTitle.isEmpty ? orch.workspaceName : orch.sessionTitle,
            lang: lang
        ) {
            store.bannerMessage = msg
        }
    }

    private func jumpToSubagent(_ sub: AgentNativeSubagentHot) {
        let orch = activeMesh.orchestrator
        let dl = sub.deepLink.isEmpty ? (orch.deepLink.isEmpty ? fallbackDeepLink(for: sub.driver) : orch.deepLink) : sub.deepLink
        if let msg = SystemNavigator.jumpToHarnessContext(
            driver: sub.driver,
            deepLink: dl,
            guiAppPath: orch.guiAppPath,
            clipboardHint: sub.title.isEmpty ? sub.roleLabel : sub.title,
            lang: lang
        ) {
            store.bannerMessage = msg
        }
    }

    private func jumpToWorker(_ worker: AgentWorkerHot) {
        let dl = worker.deepLink.isEmpty ? fallbackDeepLink(for: worker.driver) : worker.deepLink
        let hint = !worker.nativeSessionId.isEmpty ? worker.nativeSessionId : "\(worker.problem) \(worker.roleLabel)"
        if let msg = SystemNavigator.jumpToHarnessContext(
            driver: AgentDriverStyle.forDriver(worker.driver).displayName,
            deepLink: dl,
            resumeCommand: worker.resumeCommand,
            jumpMode: worker.jumpMode,
            guiAppPath: worker.guiAppPath,
            clipboardHint: hint,
            lang: lang
        ) {
            store.bannerMessage = msg
        }
    }
}
