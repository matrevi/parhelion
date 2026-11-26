import Combine
import Foundation

/// Central @MainActor cold contest/leaderboard/queue ViewModel paired with `HotTelemetryStore` + native SSE.
/// Hot/cold sync lives in `ContestStore+Sync.swift`, mutations in `ContestStore+Actions.swift`.
@MainActor
final class ContestStore: ObservableObject {
    @Published var snapshot: SupervisorSnapshot?
    @Published var breakthroughEntries: [BreakthroughEntry] = []
    @Published var breakthroughDir: String = ""
    @Published var breakthroughThreshold: Double = 5.0

    // Automatic perf-bucket backfill (`opsb bench backfill-status`)
    @Published var backfillStatus: BackfillStatusReport?
    @Published var isBackfilling: Bool = false
    var lastBackfillFetch: Date = .distantPast

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
    var didSetLangLocally: Bool = false
    @Published var workspaces: [WorkspaceEntry] = []
    @Published var workspacesLoaded: Bool = false
    @Published var sshHosts: [SSHHostEntry] = []
    @Published var activeSSHHost: String = "npu"
    /// Device pool, primary included (`opsb ssh pool`).
    @Published var sshPool: [String] = []

    @Published var selectedWorkspace: String = ""
    @Published var selectedProblemTitle: String? = nil

    @Published var isRefreshingLocal: Bool = false
    @Published var isLaunchingSubmit: Bool = false
    @Published var pollIntervalSec: Double = 1.0
    @Published var bannerMessage: String?
    @Published var errorMessage: String?

    // Hot/cold reconciliation (see `ContestStore+Sync.swift`)
    /// `title|short_sha` keys that just left the hot inflight set but are not yet confirmed by a newer cold snapshot.
    @Published var settlingInflightKeys: Set<String> = []
    /// Optimistic "submit just requested" records bridging the click → hot-inflight gap.
    @Published var launchingSubmits: [String: LaunchingSubmission] = [:]
    /// Optimistic auto-consumer toggle; wins over hot/cold until the hot frame confirms it (or it expires).
    @Published var autoConsumerOverride: Bool?
    var autoConsumerOverrideUntil: Date = .distantPast
    var hotLiveInflightKeys: Set<String> = []
    var settlingSince: [String: Date] = [:]
    var lastActiveByKey: [String: ActiveSubmissionItem] = [:]
    var lastColdError: String?
    var breakthroughThresholdHoldUntil: Date = .distantPast

    let hotStore: HotTelemetryStore
    let repository = OpsRepository()
    let daemonClient = DaemonClient()

    var lastColdETag: String?
    var lastLoadedWorkspace: String?
    var streamTask: Task<Void, Never>?
    var fallbackTimer: Timer?
    /// Bumped on every stream restart / workspace / host switch; frames from older generations are dropped.
    var streamGeneration: Int = 0
    var isFetchingColdFromETag = false
    var coldRefreshPending = false
    var coldRefreshForcePending = false

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
        // Stop the old stream first so frames of the previous workspace can no longer land.
        stopTelemetryStream()
        selectedWorkspace = path
        lastColdETag = nil
        lastLoadedWorkspace = nil
        resetHotColdReconciliation()
        hotStore.resetPipelineState()
        selectedProblemTitle = nil
        pmuCardsByProblem = [:]
        elfByProblem = [:]
        cold3ByProblem = [:]
        fastFeedbackErrorByProblem = [:]
        driverSessions = []
        backfillStatus = nil
        try? await repository.selectWorkspace(path)
        await reloadWorkspaces()
        await daemonClient.invalidate(workspace: selectedWorkspace)
        await refreshAll()
        startTelemetryStream()
    }

    func reloadSSHHosts() async {
        if let res = try? await repository.listSSHHosts() {
            sshHosts = res.hosts
            activeSSHHost = res.activeHost
            sshPool = res.pool ?? [res.activeHost]
        }
    }

    /// The daemon reconciles its telemetry streams with the pool on its next tick (about a second).
    func setDevicePool(_ alias: String, member: Bool) async {
        do {
            try await repository.setDevicePool(alias: alias, member: member)
            await reloadSSHHosts()
            bannerMessage = member
                ? tr("\(alias) 已加入设备池：开始采集遥测，部分题目的测试将在它上面运行", "\(alias) joined the device pool")
                : tr("\(alias) 已移出设备池", "\(alias) left the device pool")
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func selectSSHHost(_ alias: String) async {
        stopTelemetryStream()
        activeSSHHost = alias
        hotStore.clearForHostSwitch()
        try? await repository.selectSSHHost(alias)
        await reloadSSHHosts()
        await daemonClient.configure(host: alias, interval: nil)
        startTelemetryStream()
    }

    func reloadBreakthroughs() async {
        let ws = selectedWorkspace
        if let cat = try? await repository.fetchBreakthroughCatalog(workspace: ws, minDelta: 0.5),
           ws == selectedWorkspace {
            breakthroughEntries = cat.entries
            breakthroughDir = cat.directory
        }
    }

    // MARK: - Auto backfill status

    func reloadBackfillStatusIfNeeded(_ snap: SupervisorSnapshot) async {
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
        let ws = selectedWorkspace
        let report = (try? await repository.fetchBackfillStatus(
            workspace: ws,
            problem: selectedProblemTitle
        )) ?? BackfillStatusReport.unavailable("decode_failed")
        guard ws == selectedWorkspace else { return }
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
}
