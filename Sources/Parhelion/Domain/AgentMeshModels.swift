import SwiftUI

struct AgentOrchestratorHot: Codable, Equatable {
    let driver: String
    let displayName: String
    let protocolName: String
    let guiAppPath: String
    let deepLink: String
    let sessionTitle: String
    let workspaceName: String
    let activeWorkers: Int
    let nativeSubagents: Int
    let squadCount: Int
    let status: String
    let cache: AgentCacheUsage?

    init(
        driver: String, displayName: String, protocolName: String, guiAppPath: String = "",
        deepLink: String = "", sessionTitle: String = "",
        workspaceName: String, activeWorkers: Int, nativeSubagents: Int, squadCount: Int = 0, status: String,
        cache: AgentCacheUsage? = nil
    ) {
        self.driver = driver
        self.displayName = displayName
        self.protocolName = protocolName
        self.guiAppPath = guiAppPath
        self.deepLink = deepLink
        self.sessionTitle = sessionTitle
        self.workspaceName = workspaceName
        self.activeWorkers = activeWorkers
        self.nativeSubagents = nativeSubagents
        self.squadCount = squadCount
        self.status = status
        self.cache = cache
    }

    enum CodingKeys: String, CodingKey {
        case driver, status
        case displayName = "display_name"
        case protocolName = "protocol"
        case guiAppPath = "gui_app_path"
        case deepLink = "deep_link"
        case sessionTitle = "session_title"
        case workspaceName = "workspace_name"
        case activeWorkers = "active_workers"
        case nativeSubagents = "native_subagents"
        case squadCount = "squad_count"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        driver = try c.decodeIfPresent(String.self, forKey: .driver) ?? "codex"
        displayName = try c.decodeIfPresent(String.self, forKey: .displayName) ?? driver.uppercased()
        protocolName = try c.decodeIfPresent(String.self, forKey: .protocolName) ?? ""
        guiAppPath = try c.decodeIfPresent(String.self, forKey: .guiAppPath) ?? ""
        deepLink = try c.decodeIfPresent(String.self, forKey: .deepLink) ?? ""
        sessionTitle = try c.decodeIfPresent(String.self, forKey: .sessionTitle) ?? ""
        workspaceName = try c.decodeIfPresent(String.self, forKey: .workspaceName) ?? ""
        activeWorkers = try c.decodeIfPresent(Int.self, forKey: .activeWorkers) ?? 0
        nativeSubagents = try c.decodeIfPresent(Int.self, forKey: .nativeSubagents) ?? 0
        squadCount = try c.decodeIfPresent(Int.self, forKey: .squadCount) ?? 0
        status = try c.decodeIfPresent(String.self, forKey: .status) ?? "standby"
        cache = try AgentCacheUsage.decodeIfPresent(from: decoder)
    }
}

/// Optional prompt-cache fields of a hot-mesh worker or orchestrator row (4.2 metering).
/// Absent until the daemon's background parser has read the agent's native session log.
struct AgentCacheUsage: Codable, Equatable, Hashable {
    let hitPct: Double
    let inputMissTokens: Int
    let cacheReadTokens: Int
    let lastMissReason: String

    enum CodingKeys: String, CodingKey {
        case hitPct = "cache_hit_pct"
        case inputMissTokens = "input_miss_tokens"
        case cacheReadTokens = "cache_read_tokens"
        case lastMissReason = "last_miss_reason"
    }

    init(hitPct: Double, inputMissTokens: Int, cacheReadTokens: Int, lastMissReason: String) {
        self.hitPct = hitPct
        self.inputMissTokens = inputMissTokens
        self.cacheReadTokens = cacheReadTokens
        self.lastMissReason = lastMissReason
    }

    /// Reads the flat optional fields from the enclosing row; nil when no hit rate is present.
    static func decodeIfPresent(from decoder: Decoder) throws -> AgentCacheUsage? {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let pct = try c.decodeIfPresent(Double.self, forKey: .hitPct) else { return nil }
        return AgentCacheUsage(
            hitPct: pct,
            inputMissTokens: try c.decodeIfPresent(Int.self, forKey: .inputMissTokens) ?? 0,
            cacheReadTokens: try c.decodeIfPresent(Int.self, forKey: .cacheReadTokens) ?? 0,
            lastMissReason: try c.decodeIfPresent(String.self, forKey: .lastMissReason) ?? ""
        )
    }

    var hitLabel: String { String(format: "%.0f%%", hitPct) }

    static func compactTokens(_ n: Int) -> String {
        if n >= 1_000_000 { return String(format: "%.1fM", Double(n) / 1_000_000) }
        if n >= 1_000 { return String(format: "%.0fK", Double(n) / 1_000) }
        return "\(n)"
    }
}

struct AgentReadyDriverHot: Codable, Identifiable, Hashable {
    var id: String { kind }
    let kind: String
    let displayName: String
    let protocolName: String
    let guiAppPath: String
    let installed: Bool
    let authOk: Bool
    let hasCli: Bool

    init(
        kind: String, displayName: String, protocolName: String, guiAppPath: String = "",
        installed: Bool, authOk: Bool, hasCli: Bool
    ) {
        self.kind = kind
        self.displayName = displayName
        self.protocolName = protocolName
        self.guiAppPath = guiAppPath
        self.installed = installed
        self.authOk = authOk
        self.hasCli = hasCli
    }

    enum CodingKeys: String, CodingKey {
        case kind, installed
        case displayName = "display_name"
        case protocolName = "protocol"
        case guiAppPath = "gui_app_path"
        case authOk = "auth_ok"
        case hasCli = "has_cli"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        kind = try c.decode(String.self, forKey: .kind)
        displayName = try c.decodeIfPresent(String.self, forKey: .displayName) ?? kind
        protocolName = try c.decodeIfPresent(String.self, forKey: .protocolName) ?? ""
        guiAppPath = try c.decodeIfPresent(String.self, forKey: .guiAppPath) ?? ""
        installed = try c.decodeIfPresent(Bool.self, forKey: .installed) ?? false
        authOk = try c.decodeIfPresent(Bool.self, forKey: .authOk) ?? false
        hasCli = try c.decodeIfPresent(Bool.self, forKey: .hasCli) ?? false
    }
}

struct AgentNativeSubagentHot: Codable, Identifiable, Hashable {
    let id: String
    let driver: String
    let roleLabel: String
    let status: String
    let problem: String
    let steps: Int
    let lastTool: String
    let title: String
    let deepLink: String

    enum CodingKeys: String, CodingKey {
        case id, driver, status, problem, steps, title
        case roleLabel = "role_label"
        case lastTool = "last_tool"
        case deepLink = "deep_link"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decodeIfPresent(String.self, forKey: .id) ?? UUID().uuidString
        driver = try c.decodeIfPresent(String.self, forKey: .driver) ?? ""
        roleLabel = try c.decodeIfPresent(String.self, forKey: .roleLabel) ?? "sub"
        status = try c.decodeIfPresent(String.self, forKey: .status) ?? "idle"
        problem = try c.decodeIfPresent(String.self, forKey: .problem) ?? ""
        steps = try c.decodeIfPresent(Int.self, forKey: .steps) ?? 0
        lastTool = try c.decodeIfPresent(String.self, forKey: .lastTool) ?? ""
        title = try c.decodeIfPresent(String.self, forKey: .title) ?? ""
        deepLink = try c.decodeIfPresent(String.self, forKey: .deepLink) ?? ""
    }
}

struct AgentSquadHot: Codable, Identifiable, Hashable {
    var id: String { problem }
    let problem: String
    let difficulty: Int
    let currentScore: Double
    let collabMode: String
    let escapeLocked: Bool
    let workerIds: [String]
    let activeCount: Int
    let stagnatedCount: Int

    enum CodingKeys: String, CodingKey {
        case problem, difficulty
        case currentScore = "current_score"
        case collabMode = "collab_mode"
        case escapeLocked = "escape_locked"
        case workerIds = "worker_ids"
        case activeCount = "active_count"
        case stagnatedCount = "stagnated_count"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        problem = try c.decode(String.self, forKey: .problem)
        difficulty = try c.decodeIfPresent(Int.self, forKey: .difficulty) ?? 2
        currentScore = try c.decodeIfPresent(Double.self, forKey: .currentScore) ?? 0
        collabMode = try c.decodeIfPresent(String.self, forKey: .collabMode) ?? "solo"
        escapeLocked = try c.decodeIfPresent(Bool.self, forKey: .escapeLocked) ?? false
        workerIds = try c.decodeIfPresent([String].self, forKey: .workerIds) ?? []
        activeCount = try c.decodeIfPresent(Int.self, forKey: .activeCount) ?? 0
        stagnatedCount = try c.decodeIfPresent(Int.self, forKey: .stagnatedCount) ?? 0
    }
}

struct AgentStrategicAlertHot: Codable, Identifiable, Hashable {
    let id: String
    let kind: String
    let problem: String
    let agentId: String
    let label: String

    enum CodingKeys: String, CodingKey {
        case id, kind, problem, label
        case agentId = "agent_id"
    }
}

struct AgentWorkerHot: Codable, Identifiable, Hashable {
    var id: String { agentId }
    let agentId: String
    let driver: String
    let guiAppPath: String
    let nativeSessionId: String
    let deepLink: String
    let resumeCommand: String
    let jumpMode: String
    let roleLabel: String
    let status: String
    let problem: String
    let collabMode: String
    let axis: String
    let worktree: String
    let worktreeMode: String
    let branch: String
    let turnCount: Int
    let noGainStreak: Int
    let unreadCount: Int
    let lastTool: String
    let lastSummary: String
    let contextPercent: Int?
    let lastCommitSha: String
    let error: String
    let elapsedSec: Int
    let lastMsgEpoch: Double
    let lastMsgDir: String
    let cache: AgentCacheUsage?

    var isLive: Bool { status == "working" || status == "spawning" || status == "waiting_input" }
    var isActive: Bool { status != "stopped" && status != "error" }
    var isStagnated: Bool { noGainStreak >= 6 && isActive }

    enum CodingKeys: String, CodingKey {
        case driver, status, problem, axis, worktree, branch, error
        case agentId = "agent_id"
        case guiAppPath = "gui_app_path"
        case nativeSessionId = "native_session_id"
        case deepLink = "deep_link"
        case resumeCommand = "resume_command"
        case jumpMode = "jump_mode"
        case roleLabel = "role_label"
        case collabMode = "collab_mode"
        case worktreeMode = "worktree_mode"
        case turnCount = "turn_count"
        case noGainStreak = "no_gain_streak"
        case unreadCount = "unread_count"
        case lastTool = "last_tool"
        case lastSummary = "last_summary"
        case contextPercent = "context_percent"
        case lastCommitSha = "last_commit_sha"
        case elapsedSec = "elapsed_sec"
        case lastMsgEpoch = "last_msg_epoch"
        case lastMsgDir = "last_msg_dir"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        agentId = try c.decode(String.self, forKey: .agentId)
        driver = try c.decodeIfPresent(String.self, forKey: .driver) ?? ""
        guiAppPath = try c.decodeIfPresent(String.self, forKey: .guiAppPath) ?? ""
        nativeSessionId = try c.decodeIfPresent(String.self, forKey: .nativeSessionId) ?? ""
        deepLink = try c.decodeIfPresent(String.self, forKey: .deepLink) ?? ""
        resumeCommand = try c.decodeIfPresent(String.self, forKey: .resumeCommand) ?? ""
        jumpMode = try c.decodeIfPresent(String.self, forKey: .jumpMode) ?? "deeplink"
        roleLabel = try c.decodeIfPresent(String.self, forKey: .roleLabel) ?? "worker"
        status = try c.decodeIfPresent(String.self, forKey: .status) ?? "idle"
        problem = try c.decodeIfPresent(String.self, forKey: .problem) ?? ""
        collabMode = try c.decodeIfPresent(String.self, forKey: .collabMode) ?? "solo"
        axis = try c.decodeIfPresent(String.self, forKey: .axis) ?? ""
        worktree = try c.decodeIfPresent(String.self, forKey: .worktree) ?? ""
        worktreeMode = try c.decodeIfPresent(String.self, forKey: .worktreeMode) ?? "isolated"
        branch = try c.decodeIfPresent(String.self, forKey: .branch) ?? ""
        turnCount = try c.decodeIfPresent(Int.self, forKey: .turnCount) ?? 0
        noGainStreak = try c.decodeIfPresent(Int.self, forKey: .noGainStreak) ?? 0
        unreadCount = try c.decodeIfPresent(Int.self, forKey: .unreadCount) ?? 0
        lastTool = try c.decodeIfPresent(String.self, forKey: .lastTool) ?? ""
        lastSummary = try c.decodeIfPresent(String.self, forKey: .lastSummary) ?? ""
        contextPercent = try c.decodeIfPresent(Int.self, forKey: .contextPercent)
        lastCommitSha = try c.decodeIfPresent(String.self, forKey: .lastCommitSha) ?? ""
        error = try c.decodeIfPresent(String.self, forKey: .error) ?? ""
        elapsedSec = try c.decodeIfPresent(Int.self, forKey: .elapsedSec) ?? 0
        lastMsgEpoch = try c.decodeIfPresent(Double.self, forKey: .lastMsgEpoch) ?? 0
        lastMsgDir = try c.decodeIfPresent(String.self, forKey: .lastMsgDir) ?? ""
        cache = try AgentCacheUsage.decodeIfPresent(from: decoder)
    }
}

struct AgentMeshHotState: Codable, Equatable {
    let orchestrator: AgentOrchestratorHot
    let readyDrivers: [AgentReadyDriverHot]
    let nativeSubagents: [AgentNativeSubagentHot]
    let squads: [AgentSquadHot]
    let strategicAlerts: [AgentStrategicAlertHot]
    let workers: [AgentWorkerHot]
    let pulses: [AgentPulseHot]
    let latestSeq: Int
    let ts: Double

    enum CodingKeys: String, CodingKey {
        case orchestrator, squads, workers, pulses, ts
        case readyDrivers = "ready_drivers"
        case nativeSubagents = "native_subagents"
        case strategicAlerts = "strategic_alerts"
        case latestSeq = "latest_seq"
    }

    init(
        orchestrator: AgentOrchestratorHot,
        readyDrivers: [AgentReadyDriverHot],
        nativeSubagents: [AgentNativeSubagentHot],
        squads: [AgentSquadHot],
        strategicAlerts: [AgentStrategicAlertHot],
        workers: [AgentWorkerHot],
        pulses: [AgentPulseHot],
        latestSeq: Int,
        ts: Double
    ) {
        self.orchestrator = orchestrator
        self.readyDrivers = readyDrivers
        self.nativeSubagents = nativeSubagents
        self.squads = squads
        self.strategicAlerts = strategicAlerts
        self.workers = workers
        self.pulses = pulses
        self.latestSeq = latestSeq
        self.ts = ts
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        orchestrator = try c.decode(AgentOrchestratorHot.self, forKey: .orchestrator)
        readyDrivers = try c.decodeIfPresent([AgentReadyDriverHot].self, forKey: .readyDrivers) ?? []
        nativeSubagents = try c.decodeIfPresent([AgentNativeSubagentHot].self, forKey: .nativeSubagents) ?? []
        squads = try c.decodeIfPresent([AgentSquadHot].self, forKey: .squads) ?? []
        strategicAlerts = try c.decodeIfPresent([AgentStrategicAlertHot].self, forKey: .strategicAlerts) ?? []
        workers = try c.decodeIfPresent([AgentWorkerHot].self, forKey: .workers) ?? []
        pulses = try c.decodeIfPresent([AgentPulseHot].self, forKey: .pulses) ?? []
        latestSeq = try c.decodeIfPresent(Int.self, forKey: .latestSeq) ?? 0
        ts = try c.decodeIfPresent(Double.self, forKey: .ts) ?? 0
    }
}
