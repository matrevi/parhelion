import Foundation

struct AgentPulseHot: Codable, Identifiable, Hashable {
    var id: Int { seq }
    let seq: Int
    let agentId: String
    let problem: String
    let driver: String
    let direction: String
    let kind: String
    let priority: String
    let preview: String
    let epoch: Double

    enum CodingKeys: String, CodingKey {
        case seq, problem, driver, direction, kind, priority, preview, epoch
        case agentId = "agent_id"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        seq = try c.decode(Int.self, forKey: .seq)
        agentId = try c.decodeIfPresent(String.self, forKey: .agentId) ?? ""
        problem = try c.decodeIfPresent(String.self, forKey: .problem) ?? ""
        driver = try c.decodeIfPresent(String.self, forKey: .driver) ?? ""
        direction = try c.decodeIfPresent(String.self, forKey: .direction) ?? "outbound"
        kind = try c.decodeIfPresent(String.self, forKey: .kind) ?? "progress"
        priority = try c.decodeIfPresent(String.self, forKey: .priority) ?? "normal"
        preview = try c.decodeIfPresent(String.self, forKey: .preview) ?? ""
        epoch = try c.decodeIfPresent(Double.self, forKey: .epoch) ?? 0
    }
}

struct AgentBusMessage: Codable, Identifiable, Hashable {
    var id: Int { seq }
    let seq: Int
    let agentId: String
    let problem: String
    let direction: String
    let kind: String
    let priority: String
    let body: String
    let artifacts: [String]
    let commitSha: String
    let createdAt: String
    let epoch: Double
    /// 4.3 P27: the evidence class of a peer batch (local/paired/online_receipt); nil for other rows.
    let evidenceClass: String?

    enum CodingKeys: String, CodingKey {
        case seq, problem, direction, kind, priority, body, artifacts, epoch
        case agentId = "agent_id"
        case commitSha = "commit_sha"
        case createdAt = "created_at"
        case evidenceClass = "evidence_class"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        seq = try c.decode(Int.self, forKey: .seq)
        agentId = try c.decodeIfPresent(String.self, forKey: .agentId) ?? ""
        problem = try c.decodeIfPresent(String.self, forKey: .problem) ?? ""
        direction = try c.decodeIfPresent(String.self, forKey: .direction) ?? "system"
        kind = try c.decodeIfPresent(String.self, forKey: .kind) ?? "progress"
        priority = try c.decodeIfPresent(String.self, forKey: .priority) ?? "normal"
        body = try c.decodeIfPresent(String.self, forKey: .body) ?? ""
        artifacts = try c.decodeIfPresent([String].self, forKey: .artifacts) ?? []
        commitSha = try c.decodeIfPresent(String.self, forKey: .commitSha) ?? ""
        createdAt = try c.decodeIfPresent(String.self, forKey: .createdAt) ?? ""
        epoch = try c.decodeIfPresent(Double.self, forKey: .epoch) ?? 0
        let rawClass = try c.decodeIfPresent(String.self, forKey: .evidenceClass) ?? ""
        evidenceClass = rawClass.isEmpty ? nil : rawClass
    }

    /// 4.3 P27: a peer message that is not backed by an online judge receipt reads behind 待验证.
    var isUnverified: Bool { isPeer && evidenceClass != nil && evidenceClass != "online_receipt" }

    var isPeer: Bool { direction == "squad_note" || direction == "squad_to_worker" }
}

struct AgentMessagesResponse: Codable {
    let ok: Bool
    let problem: String?
    let diffStat: String?
    let latestSeq: Int
    let messages: [AgentBusMessage]
    /// The daemon restarted the bus sequence (full cleanup on an emptied bus), so the cursor
    /// that was sent belongs to the old history. Older daemons omit the field.
    let cursorReset: Bool?

    enum CodingKeys: String, CodingKey {
        case ok, problem, messages
        case diffStat = "diff_stat"
        case latestSeq = "latest_seq"
        case cursorReset = "cursor_reset"
    }

    /// True when `messages` start a new history for a reader that held `cursor`.
    func restartsHistory(after cursor: Int) -> Bool {
        guard cursor > 0 else { return false }
        // The daemon only returns seq > cursor unless it reset the cursor.
        return cursorReset ?? messages.contains { $0.seq <= cursor }
    }
}

struct AgentMeshResponse: Codable {
    let ok: Bool
    let agentMesh: AgentMeshHotState?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case ok, error
        case agentMesh = "agent_mesh"
    }
}

struct AgentActionResponse: Codable {
    let ok: Bool
    let error: String?
    let removed: Int?
}
