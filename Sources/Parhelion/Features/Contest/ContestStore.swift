import Combine
import Foundation

/// Central @MainActor cold contest/leaderboard/queue ViewModel paired with `HotTelemetryStore` + native SSE.
@MainActor
final class ContestStore: ObservableObject {
    @Published var snapshot: SupervisorSnapshot?
    @Published var breakthroughEntries: [BreakthroughEntry] = []
    @Published var breakthroughDir: String = ""
    @Published var breakthroughThreshold: Double = 5.0

    // Automatic perf-bucket backfill (`opsb bench backfill-status`)
    @Published var backfillStatus: BackfillStatusReport?
    @Published var isBackfilling: Bool = false
    private var lastBackfillFetch: Date = .distantPast

    // Parhelion v3 Cold-Path State (Triad KB, Harness Drivers, On-Demand Fast-Feedback)
    @Published var triadEntries: [TriadKBEntry] = []
    @Published var triadEvolutionEvents: [KBEvolutionEvent] = []
    @Published var triadEvolutionLogPath: String = ""
    @Published var isLoadingTriadKB: Bool = false
    @Published var activeDriver: String = "antigravity"
    @Published var driverDiagnostics: [DriverDiagnosticModel] = []
    @Published var driverSessions: [DriverSessionModel] = []
    @Published var isLoadingDrivers: Bool = false
    @Published var isSyncingDriver: Bool = false
    @Published var isLaunchingSession: Bool = false
    @Published var isLoadingSessions: Bool = false
    @Published var pmuCardsByProblem: [String: PmuTorchCard] = [:]
    @Published var elfByProblem: [String: ElfGuardResult] = [:]
    @Published var cold3ByProblem: [String: Cold3Result] = [:]
    @Published var loadingFastFeedbackProblems: Set<String> = []
    @Published var fastFeedbackErrorByProblem: [String: String] = [:]

    @Published var lang: AppLanguage = AppLanguage(rawValue: UserDefaults.standard.string(forKey: "parhelion.lang") ?? "zh") ?? .zh
    private var didSetLangLocally: Bool = false
    @Published var workspaces: [WorkspaceEntry] = []
    @Published var workspacesLoaded: Bool = false
    @Published var sshHosts: [SSHHostEntry] = []
    @Published var activeSSHHost: String = "npu"

    @Published var selectedWorkspace: String = ""
    @Published var selectedProblemTitle: String? = nil

    @Published var isRefreshingLocal: Bool = false
    @Published var isLaunchingSubmit: Bool = false
    @Published var pollIntervalSec: Double = 1.0
    @Published var bannerMessage: String?
    @Published var errorMessage: String?

    let hotStore: HotTelemetryStore
    let repository = OpsRepository()
    let daemonClient = DaemonClient()

    private var lastColdETag: String?
    private var lastLoadedWorkspace: String?
    private var streamTask: Task<Void, Never>?
    private var fallbackTimer: Timer?
    private var isFetchingColdFromETag = false

    init(hotStore: HotTelemetryStore? = nil) {
        self.hotStore = hotStore ?? HotTelemetryStore()
        Task { await bootstrap() }
    }

    deinit {
        streamTask?.cancel()
        fallbackTimer?.invalidate()
    }

    func bootstrap() async {
        await reloadWorkspaces()
        await reloadSSHHosts()
        guard !selectedWorkspace.isEmpty else {
            return
        }
        await refreshLocal(force: true)
        await reloadBreakthroughs()
        await reloadDriverDoctor()
        startTelemetryStream()
    }

    func refreshAll() async {
        await daemonClient.invalidate(workspace: selectedWorkspace)
        await refreshLocal(force: true)
        await reloadBreakthroughs()
        await refreshNpu(silent: false)
    }

    func refreshLocal(force: Bool = false) async {
        if isRefreshingLocal { return }
        isRefreshingLocal = true
        defer { isRefreshingLocal = false }

        let targetWs = selectedWorkspace
        let ifNoneMatch = (force || lastLoadedWorkspace != targetWs) ? nil : lastColdETag

        do {
            let result = try await daemonClient.fetchSnapshot(
                workspace: targetWs,
                host: activeSSHHost,
                ifNoneMatch: ifNoneMatch
            )
            switch result {
            case .notModified(let etag):
                if !etag.isEmpty { lastColdETag = etag }
                errorMessage = nil
            case .updated(let snap, let etag):
                lastLoadedWorkspace = targetWs
                if !etag.isEmpty { lastColdETag = etag }
                await applyColdSnapshot(snap)
            }
            return
        } catch {
            // Fall back transparently to ProcessClient CLI if daemon is unreachable
        }

        do {
            let localSnap = try await repository.fetchLocalSnapshot(workspace: targetWs)
            lastLoadedWorkspace = targetWs
            lastColdETag = nil
            await applyColdSnapshot(localSnap)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func applyColdSnapshot(_ localSnap: SupervisorSnapshot) async {
        snapshot = localSnap
        hotStore.applyColdSeed(from: localSnap)
        if let bt = localSnap.scheduler.breakthroughThreshold {
            breakthroughThreshold = bt
        }
        if !didSetLangLocally, let rawLang = localSnap.language, let parsed = AppLanguage(rawValue: rawLang) {
            lang = parsed
            UserDefaults.standard.set(parsed.rawValue, forKey: "parhelion.lang")
        }
        if selectedProblemTitle == nil || !localSnap.problems.contains(where: { $0.title == selectedProblemTitle }) {
            selectedProblemTitle = localSnap.problems.first?.title
        }
        errorMessage = nil
        await maybeAutoConsumeTopCandidate(localSnap)
        await reloadBackfillStatusIfNeeded(localSnap)
    }

    private func refreshColdSnapshotIfChanged(expectedEtag: String) async {
        guard !isFetchingColdFromETag, expectedEtag != lastColdETag else { return }
        isFetchingColdFromETag = true
        defer { isFetchingColdFromETag = false }
        await refreshLocal(force: false)
    }

    private func maybeAutoConsumeTopCandidate(
        _ snap: SupervisorSnapshot,
        livePipeline: LivePipelineState? = nil,
        liveCooldownRem: Int? = nil
    ) async {
        let pipe = livePipeline ?? hotStore.pipeline ?? snap.scheduler.pipeline
        let cdRem = liveCooldownRem ?? pipe?.cooldownRemainingSec ?? snap.scheduler.cooldownRemainingSec
        let autoEnabled = hotStore.schedulerHot?.autoConsumerEnabled ?? snap.scheduler.autoConsumerEnabled
        guard autoEnabled,
              (pipe?.canSubmit ?? true),
              !(pipe?.isLocalBusy ?? false),
              cdRem == 0,
              !isLaunchingSubmit else { return }

        // Never auto-consume `advisory` restore cards or `ESCAPE LOCK` items:
        // rollbacks are user-triggered (or closing-time), and escaped ops must pass verify-escape first.
        guard let top = snap.submissionQueue.first(where: { item in
            item.kind == "pending"
                && !item.badge.contains("ESCAPE")
                && !item.badge.contains("HOLD")
                && !item.badge.contains("CIRCUIT")
                && item.gates.allPass
        }) else { return }

        await jumpSubmitNow(problem: top.title, sha: top.sha, note: top.note)
    }

    func refreshNpu(silent: Bool = false) async {
        if hotStore.isRefreshingNpu { return }
        hotStore.isRefreshingNpu = true
        defer { hotStore.isRefreshingNpu = false }

        do {
            let frame = try await daemonClient.fetchHot(
                workspace: selectedWorkspace,
                host: activeSSHHost,
                interval: max(0.5, pollIntervalSec > 0 ? pollIntervalSec : 1.0)
            )
            await handleHotFrame(frame, decodeMs: 0)
            return
        } catch {
            // Fall back to ProcessClient if daemon unavailable
        }

        do {
            let npu = try await repository.fetchNpuTelemetry(workspace: selectedWorkspace, host: activeSSHHost)
            hotStore.applyFallbackNpu(npu)
        } catch {
            if !silent { errorMessage = error.localizedDescription }
        }
    }

    private func handleHotFrame(_ frame: HotStreamFrame, decodeMs: Double) async {
        hotStore.applyHotFrame(frame, decodeMs: decodeMs)
        if !frame.coldEtag.isEmpty && (frame.coldEtag != lastColdETag || lastLoadedWorkspace != selectedWorkspace) {
            await refreshColdSnapshotIfChanged(expectedEtag: frame.coldEtag)
        }
        if let snap = snapshot, frame.schedulerHot.autoConsumerEnabled {
            await maybeAutoConsumeTopCandidate(
                snap,
                livePipeline: frame.pipeline,
                liveCooldownRem: frame.schedulerHot.cooldownRemainingSec
            )
        }
    }

    func startTelemetryStream() {
        streamTask?.cancel()
        streamTask = nil
        stopFallbackPolling()

        let interval = pollIntervalSec
        guard interval > 0 else {
            hotStore.markStreamDisconnected()
            return
        }

        let ws = selectedWorkspace
        let host = activeSSHHost
        streamTask = Task { [weak self] in
            var consecutiveErrors = 0
            while !Task.isCancelled {
                guard let self else { break }
                do {
                    let stream = await self.daemonClient.openHotStream(
                        workspace: ws,
                        host: host,
                        interval: max(0.5, interval)
                    )
                    for try await (frame, decodeMs) in stream {
                        if Task.isCancelled { break }
                        consecutiveErrors = 0
                        self.stopFallbackPolling()
                        await self.handleHotFrame(frame, decodeMs: decodeMs)
                    }
                    if !Task.isCancelled {
                        self.hotStore.markStreamDisconnected()
                        try? await Task.sleep(nanoseconds: 1_000_000_000)
                    }
                } catch {
                    if Task.isCancelled { break }
                    consecutiveErrors += 1
                    self.hotStore.markStreamDisconnected()
                    if consecutiveErrors >= 2 {
                        self.hotStore.markDaemonOffline()
                        self.startFallbackPolling(interval: max(2.0, interval))
                    }
                    let delayNs: UInt64 = consecutiveErrors >= 2 ? 6_000_000_000 : 1_500_000_000
                    try? await Task.sleep(nanoseconds: delayNs)
                }
            }
        }
    }

    private func startFallbackPolling(interval: Double) {
        guard fallbackTimer == nil, interval > 0 else { return }
        fallbackTimer = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                async let localLoad: () = self.refreshLocal()
                async let npuLoad: () = self.refreshNpu(silent: true)
                _ = await (localLoad, npuLoad)
            }
        }
    }

    private func stopFallbackPolling() {
        fallbackTimer?.invalidate()
        fallbackTimer = nil
    }

    func reschedulePolling() {
        startTelemetryStream()
    }

    // MARK: - Workspaces & SSH

    func reloadWorkspaces() async {
        defer { workspacesLoaded = true }
        if let res = try? await repository.listWorkspaces() {
            workspaces = res.workspaces
            if let act = res.activeWorkspace, !act.isEmpty {
                selectedWorkspace = act
            } else if selectedWorkspace.isEmpty, let first = res.workspaces.first(where: { !$0.workspace.isEmpty }) {
                selectedWorkspace = first.workspace
            }
        }
    }

    func switchWorkspace(_ path: String) async {
        selectedWorkspace = path
        lastColdETag = nil
        lastLoadedWorkspace = nil
        pmuCardsByProblem = [:]
        elfByProblem = [:]
        cold3ByProblem = [:]
        fastFeedbackErrorByProblem = [:]
        driverSessions = []
        try? await repository.selectWorkspace(path)
        await reloadWorkspaces()
        await daemonClient.invalidate(workspace: path)
        await refreshAll()
        startTelemetryStream()
    }

    func reloadSSHHosts() async {
        if let res = try? await repository.listSSHHosts() {
            sshHosts = res.hosts
            activeSSHHost = res.activeHost
        }
    }

    func selectSSHHost(_ alias: String) async {
        activeSSHHost = alias
        hotStore.clearForHostSwitch()
        try? await repository.selectSSHHost(alias)
        await reloadSSHHosts()
        await daemonClient.configure(host: alias, interval: nil)
        startTelemetryStream()
    }

    func boostNpuTicket(pid: Int, problem: String, sha: String) async {
        do {
            try await repository.boostNpuTicket(host: activeSSHHost, pid: pid, problem: problem, sha: sha)
            bannerMessage = tr("已将 \(problem) 置顶为下一顺位获卡", "Boosted \(problem) to #1 NPU ticket")
            await refreshNpu(silent: true)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func cancelNpuTicket(pid: Int, problem: String) async {
        do {
            try await repository.cancelNpuTask(host: activeSSHHost, pid: pid)
            bannerMessage = tr("已取消远程任务 \(problem) (PID \(pid))", "Cancelled remote task \(problem) (PID \(pid))")
            await refreshNpu(silent: true)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    // MARK: - Queue & Gate Mutations

    func updateScheduler(
        autoConsumer: Bool? = nil,
        closingReserve: Int? = nil,
        noiseFloor: Double? = nil,
        breakthroughThreshold: Double? = nil
    ) async {
        if let bt = breakthroughThreshold {
            self.breakthroughThreshold = bt
        }
        do {
            try await repository.updateQueueConfig(
                autoConsumer: autoConsumer,
                closingReserve: closingReserve,
                noiseFloor: noiseFloor,
                breakthroughThreshold: breakthroughThreshold
            )
            await daemonClient.invalidate(workspace: selectedWorkspace)
            await refreshLocal(force: true)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func reloadBreakthroughs() async {
        if let cat = try? await repository.fetchBreakthroughCatalog(workspace: selectedWorkspace, minDelta: 0.5) {
            breakthroughEntries = cat.entries
            breakthroughDir = cat.directory
        }
    }

    // MARK: - Auto backfill status

    private func reloadBackfillStatusIfNeeded(_ snap: SupervisorSnapshot) async {
        guard snap.scheduler.backfill == nil else { return }
        let cal = (snap.problems.first { $0.title == selectedProblemTitle } ?? snap.problems.first)?.calibration
        let unpaired = cal?.unpairedBucketCount ?? 0
        let active = backfillStatus?.running.active ?? false
        guard unpaired > 0 || active || backfillStatus == nil else { return }
        guard Date().timeIntervalSince(lastBackfillFetch) >= 12.0 else { return }
        lastBackfillFetch = Date()
        await reloadBackfillStatus()
    }

    func reloadBackfillStatus(force: Bool = false) async {
        if force { lastBackfillFetch = .distantPast }
        let report = (try? await repository.fetchBackfillStatus(
            workspace: selectedWorkspace,
            problem: selectedProblemTitle
        )) ?? BackfillStatusReport.unavailable("decode_failed")
        backfillStatus = report
        lastBackfillFetch = Date()
    }

    func tr(_ zh: String, _ en: String) -> String {
        lang.tr(zh, en)
    }

    func setLanguage(_ newLang: AppLanguage) {
        didSetLangLocally = true
        lang = newLang
        UserDefaults.standard.set(newLang.rawValue, forKey: "parhelion.lang")
        Task {
            try? await repository.updateQueueConfig(language: newLang.rawValue)
            await daemonClient.invalidate(workspace: selectedWorkspace)
        }
    }

    func syncBreakthroughs() async {
        do {
            let (created, total) = try await repository.syncBreakthroughPlaybook(
                workspace: selectedWorkspace,
                minDelta: breakthroughThreshold
            )
            await reloadBreakthroughs()
            await daemonClient.invalidate(workspace: selectedWorkspace)
            await refreshLocal(force: true)
            if created > 0 {
                bannerMessage = tr("已同步 \(created) 条新突破至经验库 (共 \(total) 条)", "Synced \(created) new breakthrough(s) (\(total) total)")
            } else {
                bannerMessage = tr("经验库已是最新 (共 \(total) 条结构化突破)", "Playbook up to date (\(total) structured entries)")
            }
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func jumpSubmitNow(problem: String, sha: String, note: String) async {
        if isLaunchingSubmit { return }
        isLaunchingSubmit = true
        defer { isLaunchingSubmit = false }
        do {
            let (ok, msg) = try await repository.jumpSubmitNow(workspace: selectedWorkspace, problem: problem, sha: sha, note: note)
            if ok {
                bannerMessage = tr("已启动提交 \(problem) (\(String(sha.prefix(7))))", "Launched submit for \(problem) (\(String(sha.prefix(7))))")
            } else {
                errorMessage = msg
            }
            await daemonClient.invalidate(workspace: selectedWorkspace)
            await refreshLocal(force: true)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func approveCandidate(problem: String, sha: String, note: String) async {
        do {
            try await repository.approveSubmission(workspace: selectedWorkspace, problem: problem, sha: sha, note: note)
            bannerMessage = tr("已批准入队 \(problem) \(String(sha.prefix(7)))", "Approved & enqueued \(problem) \(String(sha.prefix(7)))")
            await daemonClient.invalidate(workspace: selectedWorkspace)
            await refreshLocal(force: true)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func skipCandidate(problem: String, sha: String) async {
        do {
            try await repository.skipSubmission(workspace: selectedWorkspace, problem: problem, sha: sha, reason: "held via Parhelion")
            bannerMessage = tr("已忽略 \(problem) \(String(sha.prefix(7)))", "Skipped \(problem) \(String(sha.prefix(7)))")
            await daemonClient.invalidate(workspace: selectedWorkspace)
            await refreshLocal(force: true)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func restoreBest(problem: String) async {
        do {
            try await repository.restoreBestSubmission(workspace: selectedWorkspace, problem: problem)
            bannerMessage = tr("已将 \(problem) 历史最优版本加入恢复队列", "Queued historical best SHA restore for \(problem)")
            await daemonClient.invalidate(workspace: selectedWorkspace)
            await refreshLocal(force: true)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func recalibrateProblem(_ problem: String) async {
        do {
            try await repository.calibrateProblemBench(workspace: selectedWorkspace, problem: problem)
            bannerMessage = tr("已完成 \(problem) 5层权重闭环校准", "Recalibrated 5-stratum weights for \(problem)")
            await daemonClient.invalidate(workspace: selectedWorkspace)
            await refreshLocal(force: true)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func backfillProblem(_ problem: String) async {
        if isBackfilling { return }
        isBackfilling = true
        bannerMessage = tr("正在补测 \(problem) 缺失性能桶…", "Backfilling missing perf buckets for \(problem)…")
        defer { isBackfilling = false }
        do {
            let res = try await repository.backfillProblemBench(workspace: selectedWorkspace, problem: problem)
            if res.measuredCount > 0 {
                bannerMessage = tr(
                    "已完成 \(problem) 性能桶补测（新增配对 \(res.measuredCount) 桶）",
                    "Backfilled \(res.measuredCount) bucket(s) for \(problem)"
                )
            } else if res.result == "die_busy" {
                bannerMessage = tr(
                    "\(problem) 补测因 Die 0 正忙被让行（die_busy），空卡后将自动补测",
                    "\(problem) backfill deferred (die_busy); B0 will retry when Die 0 is free"
                )
            } else if !res.dispatched {
                bannerMessage = tr(
                    "\(problem) 当前无可补测的实测性能桶（\(res.result)）",
                    "\(problem) has no measurable unpaired buckets (\(res.result))"
                )
            } else {
                bannerMessage = tr(
                    "\(problem) 补测结束：\(res.result)（新增 0 桶）",
                    "\(problem) backfill finished: \(res.result) (0 new buckets)"
                )
            }
            await daemonClient.invalidate(workspace: selectedWorkspace)
            await refreshLocal(force: true)
            await reloadBackfillStatus(force: true)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func drainQueueOnce() async {
        bannerMessage = "Running rate-limited queue consumer…"
        do {
            bannerMessage = try await repository.drainQueueOnce(workspace: selectedWorkspace)
            await daemonClient.invalidate(workspace: selectedWorkspace)
            await refreshLocal(force: true)
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}
