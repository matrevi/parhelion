import Foundation

// MARK: - Unified Harness Driver Models

struct DriverDoctorResponse: Codable {
    let activeDriver: String
    let diagnostics: [DriverDiagnosticModel]

    enum CodingKeys: String, CodingKey {
        case activeDriver = "active_driver"
        case diagnostics
    }
}

struct DriverDiagnosticModel: Codable, Identifiable, Hashable {
    var id: String { kind }
    let kind: String
    let displayName: String
    let installed: Bool
    let binaryPath: String
    let version: String
    let authOk: Bool
    let protocolName: String
    let detail: String
    let guiInstalled: Bool
    let guiAppPath: String
    let guiBundleId: String
    let guiVersion: String
    let urlScheme: String
    let active: Bool

    var guiAppName: String? {
        guard guiInstalled, !guiAppPath.isEmpty else { return nil }
        return URL(fileURLWithPath: guiAppPath).lastPathComponent
    }

    var guiDisplayLabel: String? {
        guard guiInstalled else { return nil }
        if kind == "codex" {
            return guiAppName.map { "\($0) (Codex GUI)" } ?? "Codex GUI"
        }
        return guiAppName ?? displayName
    }

    enum CodingKeys: String, CodingKey {
        case kind, installed, version, detail, active
        case displayName = "display_name"
        case binaryPath = "binary_path"
        case authOk = "auth_ok"
        case protocolName = "protocol"
        case guiInstalled = "gui_installed"
        case guiAppPath = "gui_app_path"
        case guiBundleId = "gui_bundle_id"
        case guiVersion = "gui_version"
        case urlScheme = "url_scheme"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        kind = try c.decode(String.self, forKey: .kind)
        displayName = try c.decodeIfPresent(String.self, forKey: .displayName) ?? kind
        installed = try c.decodeIfPresent(Bool.self, forKey: .installed) ?? false
        binaryPath = try c.decodeIfPresent(String.self, forKey: .binaryPath) ?? ""
        version = try c.decodeIfPresent(String.self, forKey: .version) ?? ""
        authOk = try c.decodeIfPresent(Bool.self, forKey: .authOk) ?? false
        protocolName = try c.decodeIfPresent(String.self, forKey: .protocolName) ?? ""
        detail = try c.decodeIfPresent(String.self, forKey: .detail) ?? ""
        guiInstalled = try c.decodeIfPresent(Bool.self, forKey: .guiInstalled) ?? false
        guiAppPath = try c.decodeIfPresent(String.self, forKey: .guiAppPath) ?? ""
        guiBundleId = try c.decodeIfPresent(String.self, forKey: .guiBundleId) ?? ""
        guiVersion = try c.decodeIfPresent(String.self, forKey: .guiVersion) ?? ""
        urlScheme = try c.decodeIfPresent(String.self, forKey: .urlScheme) ?? ""
        active = try c.decodeIfPresent(Bool.self, forKey: .active) ?? false
    }
}

struct DriverSelectResponse: Codable {
    let ok: Bool?
    let activeDriver: String?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case ok, error
        case activeDriver = "active_driver"
    }
}

struct DriverSyncResponse: Codable {
    let ok: Bool?
    let driver: String?
    let workspace: String?
    let skillsDir: String?
    let mcpConfigPath: String?
    let projectId: String?
    let createdNewProject: Bool?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case ok, driver, workspace, error
        case skillsDir = "skills_dir"
        case mcpConfigPath = "mcp_config_path"
        case projectId = "project_id"
        case createdNewProject = "created_new_project"
    }
}

struct DriverSessionModel: Codable, Identifiable, Hashable {
    var id: String { "\(driver)-\(sessionId)-\(nativeSessionId)" }
    let sessionId: String
    let driver: String
    let workspace: String
    let problem: String
    let worktree: String
    let nativeSessionId: String
    let status: String
    let pid: Int?
    let createdAt: String
    let updatedAt: String
    let briefPreview: String
    let launchMode: String
    let deepLink: String
    let projectId: String
    let briefPath: String
    let observedSteps: Int
    let tokensUsed: Int
    let killed: Bool?
    let presenceLockHeld: Bool?
    let agentName: String?
    let parentConversationId: String?
    let lastTool: String?
    let source: String?
    let guiLive: Bool?
    let model: String?
    let gitBranch: String?

    var displayTitle: String {
        if let agentName, !agentName.isEmpty { return agentName }
        if !briefPreview.isEmpty { return briefPreview }
        return problem.isEmpty ? sessionId : problem
    }

    var shortWorktree: String {
        guard !worktree.isEmpty else { return "—" }
        return URL(fileURLWithPath: worktree).lastPathComponent
    }

    var formattedTimestamp: String {
        let raw = updatedAt.isEmpty ? createdAt : updatedAt
        if raw.isEmpty || raw.hasPrefix("0001-") { return "—" }
        let iso = raw.replacingOccurrences(of: " ", with: "T")
        return Formatters.shortTimestamp(iso)
    }

    enum CodingKeys: String, CodingKey {
        case driver, workspace, problem, worktree, status, pid, killed, source, model
        case sessionId = "session_id"
        case nativeSessionId = "native_session_id"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case briefPreview = "brief_preview"
        case launchMode = "launch_mode"
        case deepLink = "deep_link"
        case projectId = "project_id"
        case briefPath = "brief_path"
        case observedSteps = "observed_steps"
        case tokensUsed = "tokens_used"
        case presenceLockHeld = "presence_lock_held"
        case agentName = "agent_name"
        case parentConversationId = "parent_conversation_id"
        case lastTool = "last_tool"
        case guiLive = "gui_live"
        case gitBranch = "git_branch"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        sessionId = try c.decodeIfPresent(String.self, forKey: .sessionId) ?? UUID().uuidString
        driver = try c.decodeIfPresent(String.self, forKey: .driver) ?? "unknown"
        workspace = try c.decodeIfPresent(String.self, forKey: .workspace) ?? ""
        problem = try c.decodeIfPresent(String.self, forKey: .problem) ?? ""
        worktree = try c.decodeIfPresent(String.self, forKey: .worktree) ?? ""
        nativeSessionId = try c.decodeIfPresent(String.self, forKey: .nativeSessionId) ?? ""
        status = try c.decodeIfPresent(String.self, forKey: .status) ?? "idle"
        pid = try c.decodeIfPresent(Int.self, forKey: .pid)
        createdAt = try c.decodeIfPresent(String.self, forKey: .createdAt) ?? ""
        updatedAt = try c.decodeIfPresent(String.self, forKey: .updatedAt) ?? ""
        briefPreview = try c.decodeIfPresent(String.self, forKey: .briefPreview) ?? ""
        launchMode = try c.decodeIfPresent(String.self, forKey: .launchMode) ?? "headless"
        deepLink = try c.decodeIfPresent(String.self, forKey: .deepLink) ?? ""
        projectId = try c.decodeIfPresent(String.self, forKey: .projectId) ?? ""
        briefPath = try c.decodeIfPresent(String.self, forKey: .briefPath) ?? ""
        observedSteps = try c.decodeIfPresent(Int.self, forKey: .observedSteps) ?? 0
        tokensUsed = try c.decodeIfPresent(Int.self, forKey: .tokensUsed) ?? 0
        killed = try c.decodeIfPresent(Bool.self, forKey: .killed)
        presenceLockHeld = try c.decodeIfPresent(Bool.self, forKey: .presenceLockHeld)
        agentName = try c.decodeIfPresent(String.self, forKey: .agentName)
        parentConversationId = try c.decodeIfPresent(String.self, forKey: .parentConversationId)
        lastTool = try c.decodeIfPresent(String.self, forKey: .lastTool)
        source = try c.decodeIfPresent(String.self, forKey: .source)
        guiLive = try c.decodeIfPresent(Bool.self, forKey: .guiLive)
        model = try c.decodeIfPresent(String.self, forKey: .model)
        gitBranch = try c.decodeIfPresent(String.self, forKey: .gitBranch)
    }
}

struct DriverSessionsResponse: Codable {
    let ok: Bool?
    let workspace: String?
    let includeGui: Bool?
    let sessions: [DriverSessionModel]
    let error: String?

    enum CodingKeys: String, CodingKey {
        case ok, workspace, sessions, error
        case includeGui = "include_gui"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = try c.decodeIfPresent(Bool.self, forKey: .ok)
        workspace = try c.decodeIfPresent(String.self, forKey: .workspace)
        includeGui = try c.decodeIfPresent(Bool.self, forKey: .includeGui)
        sessions = try c.decodeIfPresent([DriverSessionModel].self, forKey: .sessions) ?? []
        error = try c.decodeIfPresent(String.self, forKey: .error)
    }
}

/// DSH workspace registration result for a GUI launch (folder shown in DeepSeek Harness).
struct DshWorkspaceInfo: Codable {
    let ok: Bool?
    let workspaceId: String?
    let path: String?
    let created: Bool?
    let sessionLinked: Bool?
    let needsReload: Bool?
    let restarted: Bool?

    enum CodingKeys: String, CodingKey {
        case ok, path, created, restarted
        case workspaceId = "workspace_id"
        case sessionLinked = "session_linked"
        case needsReload = "needs_reload"
    }
}

/// DSH pre-seeded conversation result: the brief queued as the session's first user message.
struct DshSeedInfo: Codable {
    let ok: Bool?
    let sessionId: String?
    let briefQueued: Bool?
    let reason: String?

    enum CodingKeys: String, CodingKey {
        case ok, reason
        case sessionId = "session_id"
        case briefQueued = "brief_queued"
    }
}

struct DriverLaunchResponse: Codable {
    let ok: Bool
    let session: DriverSessionModel?
    let mode: String?
    let launchMode: String?
    let dryRun: Bool?
    let command: [String]?
    let mcpConfigPath: String?
    let worktree: String?
    let deepLink: String?
    let projectId: String?
    let briefPath: String?
    let clipboardCopied: Bool?
    let dshWorkspace: DshWorkspaceInfo?
    let dshSeed: DshSeedInfo?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case ok, session, mode, command, worktree, error
        case launchMode = "launch_mode"
        case dryRun = "dry_run"
        case mcpConfigPath = "mcp_config_path"
        case deepLink = "deep_link"
        case projectId = "project_id"
        case briefPath = "brief_path"
        case clipboardCopied = "clipboard_copied"
        case dshWorkspace = "dsh_workspace"
        case dshSeed = "dsh_seed"
    }
}
