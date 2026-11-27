import Foundation

/// Cold-path v3 actions on `ContestStore` (Triad KB, Harness Drivers, and on-demand ELF/Cold3/PMU).
/// Strictly separated from `HotTelemetryStore` and the 1s SSE loop.
extension ContestStore {
    var activeDriverDiagnostic: DriverDiagnosticModel? {
        driverDiagnostics.first(where: { $0.active || $0.kind == activeDriver })
    }

    var activeGuiSessionsCount: Int {
        driverSessions.filter { $0.status == "running" }.count
    }

    // MARK: - Triad Knowledge Base

    func reloadTriadKB(pillar: String = "all") async {
        isLoadingTriadKB = true
        defer { isLoadingTriadKB = false }
        do {
            let res = try await repository.fetchTriadKBList(pillar: pillar)
            triadEntries = res.entries
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func reloadTriadHistory(limit: Int = 50) async {
        do {
            let res = try await repository.fetchKBEvolutionHistory(limit: limit)
            triadEvolutionEvents = res.events
            triadEvolutionLogPath = res.logPath
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func loadTriadEntryDetail(id: String) async -> TriadKBEntry? {
        do {
            let res = try await repository.fetchTriadKBEntry(id: id)
            if res.ok { return res.entry }
            if let err = res.error { errorMessage = err }
        } catch {
            errorMessage = error.localizedDescription
        }
        return nil
    }

    func submitTriadFeedback(
        entryId: String,
        verdict: String,
        problem: String,
        sha: String,
        deltaRaw: Double?,
        deltaUs: Double?,
        speedup: Double?,
        note: String
    ) async -> Bool {
        do {
            let res = try await repository.recordTriadKBFeedback(
                entryId: entryId,
                verdict: verdict,
                problem: problem,
                sha: sha,
                deltaRaw: deltaRaw,
                deltaUs: deltaUs,
                speedup: speedup,
                note: note,
                workspace: selectedWorkspace
            )
            if res.ok {
                let trans = res.transition.flatMap { $0 == "unchanged" ? nil : " (\($0))" } ?? ""
                bannerMessage = tr(
                    "已记录 \(verdict.uppercased()) 反馈至 \(entryId)\(trans)",
                    "Recorded \(verdict.uppercased()) feedback for \(entryId)\(trans)"
                )
                await reloadTriadKB()
                await reloadTriadHistory()
                return true
            }
            errorMessage = res.error ?? tr("提交 KB 反馈失败", "Failed to record KB feedback")
        } catch {
            errorMessage = error.localizedDescription
        }
        return false
    }

    func rollbackTriadEvent(eventId: String) async {
        do {
            let res = try await repository.rollbackKBEvent(eventId: eventId)
            if res.ok {
                bannerMessage = tr(
                    "已回滚知识库变更 \(eventId)",
                    "Reverted KB evolution event \(eventId)"
                )
                await reloadTriadKB()
                await reloadTriadHistory()
            } else {
                errorMessage = res.error ?? tr("回滚失败", "Rollback failed")
            }
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    // MARK: - Unified Harness Drivers & Sessions

    func reloadDriverDoctor() async {
        isLoadingDrivers = true
        defer { isLoadingDrivers = false }
        if let res = try? await repository.fetchDriverDoctor() {
            activeDriver = res.activeDriver
            driverDiagnostics = res.diagnostics
        }
    }

    func setWorkerModel(_ model: String, driver: String) async {
        do {
            _ = try await repository.setWorkerModel(model, driver: driver)
            await reloadDriverDoctor()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    /// 4.4 P34: tick or untick one Harness for workers and scouts; the others keep their state.
    func setDriverAvailable(_ kind: String, _ on: Bool) async {
        var kinds = driverDiagnostics.filter { $0.available && $0.kind != kind }.map(\.kind)
        if on { kinds.append(kind) }
        do {
            _ = try await repository.setAvailableDrivers(kinds)
            await reloadDriverDoctor()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func selectHarnessDriver(_ kind: String) async {
        do {
            let res = try await repository.selectDriver(kind)
            if res.ok == false {
                errorMessage = res.error ?? tr("切换驱动失败", "Failed to switch driver")
                return
            }
            activeDriver = res.activeDriver ?? kind
            await reloadDriverDoctor()
            bannerMessage = tr("已切换默认 Harness 驱动为 \(activeDriver)", "Active Harness Driver set to \(activeDriver)")
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func syncActiveDriverWorkspace(driver: String? = nil) async {
        guard !selectedWorkspace.isEmpty else { return }
        isSyncingDriver = true
        defer { isSyncingDriver = false }
        do {
            let target = driver ?? activeDriver
            let res = try await repository.syncDriverWorkspace(workspace: selectedWorkspace, driver: target)
            if res.ok == false {
                errorMessage = res.error ?? tr("同步工作区失败", "Workspace sync failed")
                return
            }
            let projSuffix = res.projectId.map { !$0.isEmpty ? " · project=\(String($0.prefix(8)))" : "" } ?? ""
            bannerMessage = tr(
                "已同步 \(res.driver ?? target) 工作区 Skills 与 MCP 配置\(projSuffix)",
                "Synced \(res.driver ?? target) workspace skills & MCP config\(projSuffix)"
            )
            await reloadDriverDoctor()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func reloadDriverSessions(problem: String? = nil) async {
        guard !selectedWorkspace.isEmpty else { return }
        isLoadingSessions = true
        defer { isLoadingSessions = false }
        do {
            let res = try await repository.fetchDriverSessions(
                workspace: selectedWorkspace,
                problem: problem,
                includeGui: true
            )
            if res.ok == false, let err = res.error {
                errorMessage = err
                return
            }
            driverSessions = res.sessions
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func launchContestSubagentSession(driver: String? = nil, mode: String = "gui", preseed: Bool = false) async {
        await launchOperatorSession(problem: "ALL", driver: driver, mode: mode, preseed: preseed)
    }

    /// "New Contest Chat": the toolbar button and the ⌘⇧N menu item both run exactly this,
    /// the same as `opsb driver launch ALL --driver <active> --mode gui`.
    var canStartContestChat: Bool { !isLaunchingSession && !selectedWorkspace.isEmpty }

    func startContestChat() async {
        await launchContestSubagentSession(mode: "gui")
    }

    func launchOperatorSession(problem: String, driver: String? = nil, mode: String = "gui", preseed: Bool = false) async {
        guard !selectedWorkspace.isEmpty, !isLaunchingSession else { return }
        isLaunchingSession = true
        defer { isLaunchingSession = false }
        let chosen = driver ?? activeDriver
        let isAllOps = problem.uppercased() == "ALL"
        do {
            let res = try await repository.launchDriverSession(
                workspace: selectedWorkspace,
                problem: problem,
                driver: chosen,
                mode: mode,
                preseed: preseed
            )
            if res.ok {
                let drvName = res.session?.driver ?? chosen
                let wtName = res.worktree.map { URL(fileURLWithPath: $0).lastPathComponent } ?? problem
                let clipNote = (res.clipboardCopied == true)
                    ? tr("（Brief 已写入并复制到剪贴板）", " (brief saved & copied)")
                    : ""
                // DSH cannot take a folder or an initial prompt through `dsh://open`, so the
                // launcher seeds a real workspace + conversation inside DSH storage instead.
                let dshNote: String = {
                    guard res.dshWorkspace?.ok == true else { return "" }
                    let queued = res.dshSeed?.briefQueued == true
                        ? tr("，Brief 已预置为该对话的待发消息", ", brief pre-queued in that conversation")
                        : ""
                    let reload = res.dshWorkspace?.restarted == true
                        ? tr("；DSH 已重启以加载该工作区", "; DSH was restarted to load it")
                        : (res.dshWorkspace?.needsReload == true
                            ? tr("；按 ⌘R 刷新 DSH 即可看到", "; press ⌘R in DSH to view it")
                            : "")
                    return tr("（DSH 已登记工作区「\(wtName)」\(queued)\(reload)）",
                              " (DSH workspace \"\(wtName)\" ready\(queued)\(reload))")
                }()
                let notes = clipNote + dshNote
                bannerMessage = isAllOps
                    ? tr(
                        "已通过 \(drvName) 新开比赛主控对话 [\(wtName)]\(notes)",
                        "Opened a contest orchestrator chat with \(drvName) [\(wtName)]\(notes)"
                    )
                    : tr(
                        "已通过 \(drvName) 为 \(problem) 新开调优对话 [\(wtName)]\(notes)",
                        "Launched \(drvName) session for \(problem) [\(wtName)]\(notes)"
                    )
                await reloadDriverSessions()
            } else {
                errorMessage = res.error ?? tr("启动调优会话失败", "Failed to launch session")
            }
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    // MARK: - On-Demand Fast-Feedback (ELF / Cold-3 / PMU)

    func loadFastFeedback(for problem: String) async {
        guard !selectedWorkspace.isEmpty, !loadingFastFeedbackProblems.contains(problem) else { return }
        let ws = selectedWorkspace
        loadingFastFeedbackProblems.insert(problem)
        fastFeedbackErrorByProblem[problem] = nil
        defer { loadingFastFeedbackProblems.remove(problem) }

        do {
            let card = try await repository.fetchPmuTorchCard(workspace: ws, problem: problem, top: 8)
            // Drop results that land after a workspace switch (same title, different contest).
            guard ws == selectedWorkspace else { return }
            pmuCardsByProblem[problem] = card
            if let eg = card.elfGuard {
                elfByProblem[problem] = ElfGuardResult(title: problem, sha: card.sha, status: eg.status, metrics: eg.metrics, alerts: eg.alerts)
            }
            if let c3 = card.cold3 {
                cold3ByProblem[problem] = Cold3Result(title: problem, sha: card.sha, status: c3.status, metrics: c3.metrics, alerts: c3.alerts)
            }
            return
        } catch {
            guard ws == selectedWorkspace else { return }
            fastFeedbackErrorByProblem[problem] = error.localizedDescription
        }

        // Graceful fallback to individual ELF + Cold3 probes if PMU card fails
        async let elfReq = repository.fetchElfGuard(workspace: ws, problem: problem)
        async let cold3Req = repository.fetchCold3Probe(workspace: ws, problem: problem)
        let elf = try? await elfReq
        let cold3 = try? await cold3Req
        guard ws == selectedWorkspace else { return }
        if let elf { elfByProblem[problem] = elf }
        if let cold3 { cold3ByProblem[problem] = cold3 }
    }
}
