import Foundation

// MARK: - Bridge (`opsb bridge status --json`)

struct BridgeScope: Codable, Hashable {
    let paths: [String]
    let tags: [String]

    init(paths: [String] = [], tags: [String] = []) {
        self.paths = paths
        self.tags = tags
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        paths = (try? c.decodeIfPresent([String].self, forKey: .paths)).flatMap { $0 } ?? []
        tags = (try? c.decodeIfPresent([String].self, forKey: .tags)).flatMap { $0 } ?? []
    }
}

struct BridgeTransport: Codable, Hashable {
    let mode: String
    let exec: [String]
    let url: String

    init(mode: String = "file", exec: [String] = [], url: String = "") {
        self.mode = mode
        self.exec = exec
        self.url = url
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        mode = (try? c.decodeIfPresent(String.self, forKey: .mode)).flatMap { $0 } ?? "file"
        exec = (try? c.decodeIfPresent([String].self, forKey: .exec)).flatMap { $0 } ?? []
        url = (try? c.decodeIfPresent(String.self, forKey: .url)).flatMap { $0 } ?? ""
    }
}

struct BridgeCounts: Codable, Hashable {
    let pending: Int
    let pulled: Int
    let acked: Int
    let total: Int

    init(pending: Int = 0, pulled: Int = 0, acked: Int = 0, total: Int = 0) {
        self.pending = pending
        self.pulled = pulled
        self.acked = acked
        self.total = total
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        pending = (try? c.decodeIfPresent(Int.self, forKey: .pending)).flatMap { $0 } ?? 0
        pulled = (try? c.decodeIfPresent(Int.self, forKey: .pulled)).flatMap { $0 } ?? 0
        acked = (try? c.decodeIfPresent(Int.self, forKey: .acked)).flatMap { $0 } ?? 0
        total = (try? c.decodeIfPresent(Int.self, forKey: .total)).flatMap { $0 } ?? 0
    }
}

struct BridgeItem: Codable, Identifiable, Hashable {
    let entryId: String
    let entrySha: String
    let sha8: String
    let title: String
    let problem: String
    let deltaRaw: Double?
    let state: String
    let deliveredAt: String?
    let pulledAt: String?
    let publishedAt: String?
    let collectedAt: String?
    let inbox: String
    let inboxExists: Bool
    let outbox: String
    let outboxExists: Bool
    let outboxLines: Int
    let pushOk: Bool
    let lastError: String?
    let selfInitiated: Bool

    var id: String { "\(entryId)#\(sha8)" }
    var shortSha: String { sha8.isEmpty ? String(entrySha.prefix(8)) : sha8 }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        entryId = (try? c.decodeIfPresent(String.self, forKey: .entryId)).flatMap { $0 } ?? ""
        entrySha = (try? c.decodeIfPresent(String.self, forKey: .entrySha)).flatMap { $0 } ?? ""
        sha8 = (try? c.decodeIfPresent(String.self, forKey: .sha8)).flatMap { $0 } ?? ""
        title = (try? c.decodeIfPresent(String.self, forKey: .title)).flatMap { $0 } ?? ""
        problem = (try? c.decodeIfPresent(String.self, forKey: .problem)).flatMap { $0 } ?? ""
        deltaRaw = (try? c.decodeIfPresent(Double.self, forKey: .deltaRaw)).flatMap { $0 }
        state = (try? c.decodeIfPresent(String.self, forKey: .state)).flatMap { $0 } ?? "pending"
        deliveredAt = (try? c.decodeIfPresent(String.self, forKey: .deliveredAt)).flatMap { $0 }
        pulledAt = (try? c.decodeIfPresent(String.self, forKey: .pulledAt)).flatMap { $0 }
        publishedAt = (try? c.decodeIfPresent(String.self, forKey: .publishedAt)).flatMap { $0 }
        collectedAt = (try? c.decodeIfPresent(String.self, forKey: .collectedAt)).flatMap { $0 }
        inbox = (try? c.decodeIfPresent(String.self, forKey: .inbox)).flatMap { $0 } ?? ""
        inboxExists = (try? c.decodeIfPresent(Bool.self, forKey: .inboxExists)).flatMap { $0 } ?? false
        outbox = (try? c.decodeIfPresent(String.self, forKey: .outbox)).flatMap { $0 } ?? ""
        outboxExists = (try? c.decodeIfPresent(Bool.self, forKey: .outboxExists)).flatMap { $0 } ?? false
        outboxLines = (try? c.decodeIfPresent(Int.self, forKey: .outboxLines)).flatMap { $0 } ?? 0
        pushOk = (try? c.decodeIfPresent(Bool.self, forKey: .pushOk)).flatMap { $0 } ?? false
        lastError = (try? c.decodeIfPresent(String.self, forKey: .lastError)).flatMap { $0 }
        selfInitiated = (try? c.decodeIfPresent(Bool.self, forKey: .selfInitiated)).flatMap { $0 } ?? false
    }

    enum CodingKeys: String, CodingKey {
        case entryId = "entry_id"
        case entrySha = "entry_sha"
        case sha8, title, problem, state
        case deltaRaw = "delta_raw"
        case deliveredAt = "delivered_at"
        case pulledAt = "pulled_at"
        case publishedAt = "published_at"
        case collectedAt = "collected_at"
        case inbox
        case inboxExists = "inbox_exists"
        case outbox
        case outboxExists = "outbox_exists"
        case outboxLines = "outbox_lines"
        case pushOk = "push_ok"
        case lastError = "last_error"
        case selfInitiated = "self_initiated"
    }
}

struct BridgeAgent: Codable, Identifiable, Hashable {
    let agentId: String
    let display: String
    let kind: String
    let enabled: Bool
    let minDelta: Double
    let scope: BridgeScope
    let transport: BridgeTransport
    let counts: BridgeCounts
    let items: [BridgeItem]

    var id: String { agentId }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        agentId = (try? c.decodeIfPresent(String.self, forKey: .agentId)).flatMap { $0 } ?? ""
        display = (try? c.decodeIfPresent(String.self, forKey: .display)).flatMap { $0 } ?? agentId
        kind = (try? c.decodeIfPresent(String.self, forKey: .kind)).flatMap { $0 } ?? "cli"
        enabled = (try? c.decodeIfPresent(Bool.self, forKey: .enabled)).flatMap { $0 } ?? true
        minDelta = (try? c.decodeIfPresent(Double.self, forKey: .minDelta)).flatMap { $0 } ?? 0
        scope = (try? c.decodeIfPresent(BridgeScope.self, forKey: .scope)).flatMap { $0 } ?? BridgeScope()
        transport = (try? c.decodeIfPresent(BridgeTransport.self, forKey: .transport)).flatMap { $0 } ?? BridgeTransport()
        counts = (try? c.decodeIfPresent(BridgeCounts.self, forKey: .counts)).flatMap { $0 } ?? BridgeCounts()
        items = (try? c.decodeIfPresent([BridgeItem].self, forKey: .items)).flatMap { $0 } ?? []
    }

    enum CodingKeys: String, CodingKey {
        case agentId = "agent_id"
        case display, kind, enabled, scope, transport, counts, items
        case minDelta = "min_delta"
    }
}

struct BridgeTotals: Codable, Hashable {
    let pending: Int
    let pulled: Int
    let acked: Int
    let delivered: Int
    let outboxFiles: Int

    init(pending: Int = 0, pulled: Int = 0, acked: Int = 0, delivered: Int = 0, outboxFiles: Int = 0) {
        self.pending = pending
        self.pulled = pulled
        self.acked = acked
        self.delivered = delivered
        self.outboxFiles = outboxFiles
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        pending = (try? c.decodeIfPresent(Int.self, forKey: .pending)).flatMap { $0 } ?? 0
        pulled = (try? c.decodeIfPresent(Int.self, forKey: .pulled)).flatMap { $0 } ?? 0
        acked = (try? c.decodeIfPresent(Int.self, forKey: .acked)).flatMap { $0 } ?? 0
        delivered = (try? c.decodeIfPresent(Int.self, forKey: .delivered)).flatMap { $0 } ?? 0
        outboxFiles = (try? c.decodeIfPresent(Int.self, forKey: .outboxFiles)).flatMap { $0 } ?? 0
    }

    enum CodingKeys: String, CodingKey {
        case pending, pulled, acked, delivered
        case outboxFiles = "outbox_files"
    }
}

struct BridgeStatusResponse: Codable {
    let ok: Bool
    let bridgeDir: String
    let agentsPath: String
    let agents: [BridgeAgent]
    let totals: BridgeTotals
    let ledgerRecords: Int
    let summariesCount: Int
    let fetchedAt: String?

    init(ok: Bool = false, bridgeDir: String = "", agentsPath: String = "", agents: [BridgeAgent] = [],
         totals: BridgeTotals = BridgeTotals(), ledgerRecords: Int = 0, summariesCount: Int = 0, fetchedAt: String? = nil) {
        self.ok = ok
        self.bridgeDir = bridgeDir
        self.agentsPath = agentsPath
        self.agents = agents
        self.totals = totals
        self.ledgerRecords = ledgerRecords
        self.summariesCount = summariesCount
        self.fetchedAt = fetchedAt
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = (try? c.decodeIfPresent(Bool.self, forKey: .ok)).flatMap { $0 } ?? false
        bridgeDir = (try? c.decodeIfPresent(String.self, forKey: .bridgeDir)).flatMap { $0 } ?? ""
        agentsPath = (try? c.decodeIfPresent(String.self, forKey: .agentsPath)).flatMap { $0 } ?? ""
        agents = (try? c.decodeIfPresent([BridgeAgent].self, forKey: .agents)).flatMap { $0 } ?? []
        totals = (try? c.decodeIfPresent(BridgeTotals.self, forKey: .totals)).flatMap { $0 } ?? BridgeTotals()
        ledgerRecords = (try? c.decodeIfPresent(Int.self, forKey: .ledgerRecords)).flatMap { $0 } ?? 0
        summariesCount = (try? c.decodeIfPresent(Int.self, forKey: .summariesCount)).flatMap { $0 } ?? 0
        fetchedAt = (try? c.decodeIfPresent(String.self, forKey: .fetchedAt)).flatMap { $0 }
    }

    enum CodingKeys: String, CodingKey {
        case ok, agents, totals
        case bridgeDir = "bridge_dir"
        case agentsPath = "agents_path"
        case ledgerRecords = "ledger_records"
        case summariesCount = "summaries_count"
        case fetchedAt = "fetched_at"
    }
}

/// One collected agent receipt (`breakthrough/index.json#summaries[]`).
struct BreakthroughSummary: Codable, Identifiable, Hashable {
    let entryId: String
    let agentId: String
    let agentDisplay: String
    let problem: String
    let summary: String
    let lines: Int
    let summaryPath: String
    let collectedAt: String?

    var id: String { "\(entryId)#\(agentId)" }

    enum CodingKeys: String, CodingKey {
        case entryId = "entry_id"
        case agentId = "agent_id"
        case agentDisplay = "agent_display"
        case problem, summary, lines
        case summaryPath = "summary_path"
        case collectedAt = "collected_at"
    }
}

// MARK: - Auto backfill (`opsb bench backfill-status --json`)

struct BackfillBucketItem: Codable, Hashable, Identifiable {
    let label: String
    let problem: String
    let bucketId: String
    let stratum: String
    let detail: String

    var id: String { "\(problem)#\(bucketId)#\(label)" }

    init(label: String = "", problem: String = "", bucketId: String = "", stratum: String = "", detail: String = "") {
        self.label = label
        self.problem = problem
        self.bucketId = bucketId
        self.stratum = stratum
        self.detail = detail
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        label = (try? c.decodeIfPresent(String.self, forKey: .label)).flatMap { $0 } ?? ""
        problem = (try? c.decodeIfPresent(String.self, forKey: .problem)).flatMap { $0 } ?? ""
        bucketId = (try? c.decodeIfPresent(String.self, forKey: .bucketId)).flatMap { $0 } ?? ""
        stratum = (try? c.decodeIfPresent(String.self, forKey: .stratum)).flatMap { $0 } ?? ""
        detail = (try? c.decodeIfPresent(String.self, forKey: .detail)).flatMap { $0 } ?? ""
    }

    enum CodingKeys: String, CodingKey {
        case label, problem, stratum, detail
        case bucketId = "bucket_id"
    }
}

struct BackfillRunningState: Codable, Hashable {
    let active: Bool
    let problem: String
    let bucketId: String
    let startedAt: String
    let detail: String

    init(active: Bool = false, problem: String = "", bucketId: String = "", startedAt: String = "", detail: String = "") {
        self.active = active
        self.problem = problem
        self.bucketId = bucketId
        self.startedAt = startedAt
        self.detail = detail
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        active = (try? c.decodeIfPresent(Bool.self, forKey: .active)).flatMap { $0 } ?? false
        problem = (try? c.decodeIfPresent(String.self, forKey: .problem)).flatMap { $0 } ?? ""
        bucketId = (try? c.decodeIfPresent(String.self, forKey: .bucketId)).flatMap { $0 } ?? ""
        startedAt = (try? c.decodeIfPresent(String.self, forKey: .startedAt)).flatMap { $0 } ?? ""
        detail = (try? c.decodeIfPresent(String.self, forKey: .detail)).flatMap { $0 } ?? ""
    }

    enum CodingKeys: String, CodingKey {
        case active, problem, detail
        case bucketId = "bucket_id"
        case startedAt = "started_at"
    }
}

struct BackfillStatusReport: Codable, Hashable {
    let available: Bool
    let reason: String
    let source: String
    let unpaired: [BackfillBucketItem]
    let unpairedCount: Int
    let queued: [BackfillBucketItem]
    let queuedCount: Int
    let running: BackfillRunningState
    let lastRun: String?
    let etaSec: Double?
    let ledgerSha: String?
    let fetchedAt: String?

    init(available: Bool = false, reason: String = "", source: String = "", unpaired: [BackfillBucketItem] = [],
         unpairedCount: Int = 0, queued: [BackfillBucketItem] = [], queuedCount: Int = 0,
         running: BackfillRunningState = BackfillRunningState(), lastRun: String? = nil, etaSec: Double? = nil,
         ledgerSha: String? = nil, fetchedAt: String? = nil) {
        self.available = available
        self.reason = reason
        self.source = source
        self.unpaired = unpaired
        self.unpairedCount = unpairedCount
        self.queued = queued
        self.queuedCount = queuedCount
        self.running = running
        self.lastRun = lastRun
        self.etaSec = etaSec
        self.ledgerSha = ledgerSha
        self.fetchedAt = fetchedAt
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = (try? c.decodeIfPresent(Bool.self, forKey: .available)).flatMap { $0 } ?? false
        reason = (try? c.decodeIfPresent(String.self, forKey: .reason)).flatMap { $0 } ?? ""
        source = (try? c.decodeIfPresent(String.self, forKey: .source)).flatMap { $0 } ?? ""
        unpaired = (try? c.decodeIfPresent([BackfillBucketItem].self, forKey: .unpaired)).flatMap { $0 } ?? []
        unpairedCount = (try? c.decodeIfPresent(Int.self, forKey: .unpairedCount)).flatMap { $0 } ?? unpaired.count
        queued = (try? c.decodeIfPresent([BackfillBucketItem].self, forKey: .queued)).flatMap { $0 } ?? []
        queuedCount = (try? c.decodeIfPresent(Int.self, forKey: .queuedCount)).flatMap { $0 } ?? queued.count
        running = (try? c.decodeIfPresent(BackfillRunningState.self, forKey: .running)).flatMap { $0 } ?? BackfillRunningState()
        lastRun = (try? c.decodeIfPresent(String.self, forKey: .lastRun)).flatMap { $0 }
        etaSec = (try? c.decodeIfPresent(Double.self, forKey: .etaSec)).flatMap { $0 }
        ledgerSha = (try? c.decodeIfPresent(String.self, forKey: .ledgerSha)).flatMap { $0 }
        fetchedAt = (try? c.decodeIfPresent(String.self, forKey: .fetchedAt)).flatMap { $0 }
    }

    enum CodingKeys: String, CodingKey {
        case available, reason, source, unpaired, queued, running
        case unpairedCount = "unpaired_count"
        case queuedCount = "queued_count"
        case lastRun = "last_run"
        case etaSec = "eta_sec"
        case ledgerSha = "ledger_sha"
        case fetchedAt = "fetched_at"
    }

    static func unavailable(_ reason: String) -> BackfillStatusReport {
        BackfillStatusReport(available: false, reason: reason)
    }
}
