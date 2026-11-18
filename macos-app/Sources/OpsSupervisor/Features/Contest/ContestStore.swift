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
    @Published var pollIntervalSec: Int = 5
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
        } catch {
            errorMessage = error.localizedDescription
        }
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
        pollTimer = Timer.scheduledTimer(withTimeInterval: TimeInterval(pollIntervalSec), repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                await self.refreshLocal()
                await self.refreshNpu(silent: true)
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
