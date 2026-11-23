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
                bannerMessage = isAllOps
                    ? tr(
                        "已通过 \(drvName) 新开 10 题 Subagent 并行优化对话 [\(wtName)]\(clipNote)",
                        "Launched \(drvName) 10-op subagent orchestration session [\(wtName)]\(clipNote)"
                    )
                    : tr(
                        "已通过 \(drvName) 为 \(problem) 新开调优对话 [\(wtName)]\(clipNote)",
                        "Launched \(drvName) session for \(problem) [\(wtName)]\(clipNote)"
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
