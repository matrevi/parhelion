import Combine
import Foundation

/// Central @MainActor ViewModel coordinating decoupled fast-local (~60ms) and async-SSH NPU refreshes.
@MainActor
final class ContestStore: ObservableObject {
    @Published var snapshot: SupervisorSnapshot?
    @Published var telemetryHistory: [TelemetrySample] = []
    @Published var breakthroughEntries: [BreakthroughEntry] = []
    @Published var breakthroughDir: String = ""
    @Published var breakthroughThreshold: Double = 5.0

    // Automatic perf-bucket backfill (`opsb bench backfill-status`)
    @Published var backfillStatus: BackfillStatusReport?
    @Published var isBackfilling: Bool = false
    private var lastBackfillFetch: Date = .distantPast
    @Published var lang: AppLanguage = AppLanguage(rawValue: UserDefaults.standard.string(forKey: "parhelion.lang") ?? "zh") ?? .zh
    private var didSetLangLocally: Bool = false
    @Published var workspaces: [WorkspaceEntry] = []
    @Published var sshHosts: [SSHHostEntry] = []
    @Published var activeSSHHost: String = "npu"

    @Published var selectedWorkspace: String = "/Users/winbeau/Competitions/ops-bench"
    @Published var selectedProblemTitle: String? = "ConcatD"

    @Published var isRefreshingLocal: Bool = false
    @Published var isRefreshingNpu: Bool = false
    @Published var isLaunchingSubmit: Bool = false
    @Published var pollIntervalSec: Double = 1.0
    @Published var bannerMessage: String?
    @Published var errorMessage: String?

    let repository = OpsRepository()
    private var pollTimer: Timer?

    init() {
        reschedulePolling()
        Task { await bootstrap() }
    }

    func bootstrap() async {
        await refreshLocal()
        async let wsLoad: () = reloadWorkspaces()
        async let sshLoad: () = reloadSSHHosts()
        async let btLoad: () = reloadBreakthroughs()
        async let npuLoad: () = refreshNpu(silent: true)
        _ = await (wsLoad, sshLoad, btLoad, npuLoad)
    }

    func refreshAll() async {
        await refreshLocal()
        await reloadBreakthroughs()
        await refreshNpu(silent: false)
    }

    func refreshLocal() async {
        if isRefreshingLocal { return }
        isRefreshingLocal = true
        defer { isRefreshingLocal = false }
        do {
            let localSnap = try await repository.fetchLocalSnapshot(workspace: selectedWorkspace)
            if let existingNpu = snapshot?.npu, existingNpu.online {
                snapshot = localSnap.updatingNpu(existingNpu)
            } else {
                snapshot = localSnap
            }
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
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func maybeAutoConsumeTopCandidate(_ snap: SupervisorSnapshot) async {
        let cdRem = snap.scheduler.pipeline?.cooldownRemainingSec ?? snap.scheduler.cooldownRemainingSec
        guard snap.scheduler.autoConsumerEnabled,
              !(snap.scheduler.pipeline?.isLocalBusy ?? false),
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
        if isRefreshingNpu { return }
        isRefreshingNpu = true
        defer { isRefreshingNpu = false }
        do {
            let npu = try await repository.fetchNpuTelemetry(workspace: selectedWorkspace, host: activeSSHHost)
            if let cur = snapshot {
                snapshot = cur.updatingNpu(npu)
            }
            mergeTelemetryHistory(from: npu)
        } catch {
            if !silent { errorMessage = error.localizedDescription }
        }
    }

    private func mergeTelemetryHistory(from npu: NpuTelemetry) {
        var byTs: [Int: TelemetrySample] = [:]
        for pt in telemetryHistory {
            byTs[Int(pt.ts)] = pt
        }
        for pt in (npu.history ?? []) {
            byTs[Int(pt.ts)] = pt
        }
        let merged = byTs.values.sorted { $0.ts < $1.ts }
        telemetryHistory = Array(merged.suffix(120))
    }

    func reschedulePolling() {
        pollTimer?.invalidate()
        guard pollIntervalSec > 0 else { return }
        pollTimer = Timer.scheduledTimer(withTimeInterval: pollIntervalSec, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                async let localLoad: () = self.refreshLocal()
                async let npuLoad: () = self.refreshNpu(silent: true)
                _ = await (localLoad, npuLoad)
            }
        }
    }

    // MARK: - Workspaces & SSH

    func reloadWorkspaces() async {
        if let res = try? await repository.listWorkspaces() {
            workspaces = res.workspaces
            if let act = res.activeWorkspace, !act.isEmpty {
                selectedWorkspace = act
            }
        }
    }

    func switchWorkspace(_ path: String) async {
        selectedWorkspace = path
        try? await repository.selectWorkspace(path)
        await reloadWorkspaces()
        await refreshAll()
    }

    func reloadSSHHosts() async {
        if let res = try? await repository.listSSHHosts() {
            sshHosts = res.hosts
            activeSSHHost = res.activeHost
        }
    }

    func selectSSHHost(_ alias: String) async {
        activeSSHHost = alias
        try? await repository.selectSSHHost(alias)
        await reloadSSHHosts()
        await refreshNpu(silent: false)
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
            await refreshLocal()
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
        // The new CLI ships `scheduler.backfill` inside the poll payload: no extra probe needed.
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
        }
    }

    func syncBreakthroughs() async {
        do {
            let (created, total) = try await repository.syncBreakthroughPlaybook(
                workspace: selectedWorkspace,
                minDelta: breakthroughThreshold
            )
            await reloadBreakthroughs()
            await refreshLocal()
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
            await refreshLocal()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func approveCandidate(problem: String, sha: String, note: String) async {
        do {
            try await repository.approveSubmission(workspace: selectedWorkspace, problem: problem, sha: sha, note: note)
            bannerMessage = tr("已批准入队 \(problem) \(String(sha.prefix(7)))", "Approved & enqueued \(problem) \(String(sha.prefix(7)))")
            await refreshLocal()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func skipCandidate(problem: String, sha: String) async {
        do {
            try await repository.skipSubmission(workspace: selectedWorkspace, problem: problem, sha: sha, reason: "held via Parhelion")
            bannerMessage = tr("已忽略 \(problem) \(String(sha.prefix(7)))", "Skipped \(problem) \(String(sha.prefix(7)))")
            await refreshLocal()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func restoreBest(problem: String) async {
        do {
            try await repository.restoreBestSubmission(workspace: selectedWorkspace, problem: problem)
            bannerMessage = tr("已将 \(problem) 历史最优版本加入恢复队列", "Queued historical best SHA restore for \(problem)")
            await refreshLocal()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func recalibrateProblem(_ problem: String) async {
        do {
            try await repository.calibrateProblemBench(workspace: selectedWorkspace, problem: problem)
            bannerMessage = tr("已完成 \(problem) 5层权重闭环校准", "Recalibrated 5-stratum weights for \(problem)")
            await refreshLocal()
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
            await refreshLocal()
            await reloadBackfillStatus(force: true)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func drainQueueOnce() async {
        bannerMessage = "Running rate-limited queue consumer…"
        do {
            bannerMessage = try await repository.drainQueueOnce(workspace: selectedWorkspace)
            await refreshLocal()
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}
