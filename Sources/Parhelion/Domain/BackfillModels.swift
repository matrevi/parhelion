import Foundation

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
