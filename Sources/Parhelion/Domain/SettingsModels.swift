import Foundation

struct AuthStatus: Codable {
    let judge: JudgeAuth
    let judgeAccounts: [JudgeAccountSlot]?
    let gitcode: GitCodeAuth

    enum CodingKeys: String, CodingKey {
        case judge, gitcode
        case judgeAccounts = "judge_accounts"
    }
}

struct JudgeAccountSlot: Codable, Identifiable, Hashable {
    let id: String
    let label: String
    let accountHint: String?
    let enabled: Bool
    let isPrimary: Bool
    let loggedIn: Bool
    let sessionFile: String
    let userId: String?
    let remainingHours: Double?

    enum CodingKeys: String, CodingKey {
        case id, label, enabled
        case accountHint = "account_hint"
        case isPrimary = "is_primary"
        case loggedIn = "logged_in"
        case sessionFile = "session_file"
        case userId = "user_id"
        case remainingHours = "remaining_hours"
    }
}

struct JudgeAuth: Codable {
    let loggedIn: Bool
    let sessionFile: String
    let baseUrl: String
    let userId: String?
    let loggedInAt: String?
    let expiresAt: String?
    let remainingHours: Double?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case error
        case loggedIn = "logged_in"
        case sessionFile = "session_file"
        case baseUrl = "base_url"
        case userId = "user_id"
        case loggedInAt = "logged_in_at"
        case expiresAt = "expires_at"
        case remainingHours = "remaining_hours"
    }
}

struct GitCodeAuth: Codable {
    let loggedIn: Bool
    let cliPath: String
    let username: String?
    let protocolName: String?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case username, error
        case loggedIn = "logged_in"
        case cliPath = "cli_path"
        case protocolName = "protocol"
    }
}

struct SkillsStatus: Codable {
    let mode: String
    let availableSkills: [String]
    let allowedTargets: [String]
    let targets: [String: SkillTargetState]

    enum CodingKeys: String, CodingKey {
        case mode, targets
        case availableSkills = "available_skills"
        case allowedTargets = "allowed_targets"
    }
}

struct SkillTargetState: Codable {
    let target: String
    let skillsDir: String
    let installedSkills: [String]
    let allInstalled: Bool

    enum CodingKeys: String, CodingKey {
        case target
        case skillsDir = "skills_dir"
        case installedSkills = "installed_skills"
        case allInstalled = "all_installed"
    }
}

struct SSHHostListResponse: Codable {
    let activeHost: String
    let hosts: [SSHHostEntry]
    /// The device pool (`npu_ssh_hosts`), primary included; absent from older CLIs.
    let pool: [String]?

    enum CodingKeys: String, CodingKey {
        case activeHost = "active_host"
        case hosts, pool
    }
}

struct SSHHostEntry: Codable, Identifiable, Hashable {
    var id: String { alias }
    var alias: String
    var hostname: String
    var port: Int
    var user: String
    var identityFile: String
    var source: String?
    var isActive: Bool?
    var inPool: Bool?

    enum CodingKeys: String, CodingKey {
        case alias, hostname, port, user, source
        case identityFile = "identity_file"
        case isActive = "is_active"
        case inPool = "in_pool"
    }
}

struct SSHTestResult: Codable {
    let ok: Bool
    let host: String
    let latencyMs: Double
    let info: String

    enum CodingKeys: String, CodingKey {
        case ok, host, info
        case latencyMs = "latency_ms"
    }
}

struct CollabInviteResponse: Codable {
    let ok: Bool
    let username: String
    let owner: String
    let permission: String
    let results: [CollabRepoResult]
}

struct CollabRepoResult: Codable, Identifiable {
    var id: String { repo }
    let problem: String
    let repo: String
    let status: String
    let detail: String
}

struct WorkspaceListResponse: Codable {
    let activeWorkspace: String?
    let workspaces: [WorkspaceEntry]

    enum CodingKeys: String, CodingKey {
        case activeWorkspace = "active_workspace"
        case workspaces
    }
}

struct WorkspaceEntry: Codable, Identifiable, Hashable {
    var id: String { workspace }
    let workspace: String
    let title: String
    let layout: String
    let active: Bool
}
