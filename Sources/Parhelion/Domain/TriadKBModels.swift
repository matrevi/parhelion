import Foundation

enum TriadKBPillar: String, CaseIterable, Identifiable {
    case breakthrough
    case snippets
    case hardware
    case community
    case evolution

    var id: String { rawValue }

    func label(_ lang: AppLanguage) -> String {
        switch self {
        case .breakthrough: return lang.tr("突破档案", "Breakthroughs")
        case .snippets: return lang.tr("代码积木", "Snippets")
        case .hardware: return lang.tr("硬件规律", "Hardware")
        case .community: return lang.tr("社区方案", "Community")
        case .evolution: return lang.tr("进化日志", "Evolution Log")
        }
    }

    var symbol: String {
        switch self {
        case .breakthrough: return "flame.fill"
        case .snippets: return "chevron.left.forwardslash.chevron.right"
        case .hardware: return "cpu.fill"
        case .community: return "books.vertical.fill"
        case .evolution: return "clock.arrow.2.circlepath"
        }
    }
}

enum TriadPortabilityFilter: String, CaseIterable, Identifiable {
    case all
    case nativeAccel = "native-accel"
    case portable
    case remapToAccel = "remap-to-accel"
    case antiPattern = "anti-pattern"

    var id: String { rawValue }

    func label(_ lang: AppLanguage) -> String {
        switch self {
        case .all: return lang.tr("全部来源", "All")
        case .nativeAccel: return "native-accel"
        case .portable: return "portable"
        case .remapToAccel: return "remap-to-accel"
        case .antiPattern: return "anti-pattern"
        }
    }
}

struct TriadKBEvidence: Codable, Identifiable, Hashable {
    var id: String { "\(problem)-\(sha)-\(verdict)-\(recordedAt ?? "")" }
    let workspace: String?
    let problem: String
    let sha: String
    let deltaRaw: Double?
    let deltaUs: Double?
    let speedup: Double?
    let verdict: String
    let note: String?
    let recordedAt: String?

    enum CodingKeys: String, CodingKey {
        case workspace, problem, sha, speedup, verdict, note
        case deltaRaw = "delta_raw"
        case deltaUs = "delta_us"
        case recordedAt = "recorded_at"
    }
}

struct TriadKBEntry: Codable, Identifiable, Hashable {
    let id: String
    let legacyId: String?
    let pillar: String
    let title: String
    let summary: String
    let category: String
    let tags: [String]
    let bottlenecks: [String]
    let regimes: [String]
    let dtypes: [String]
    let portability: String
    let status: String
    let priorAlpha: Double?
    let priorBeta: Double?
    let confidence: Double
    let wins: Int
    let fails: Int
    let evidence: [TriadKBEvidence]
    let relatedSnippets: [String]
    let docPath: String
    let absDocPath: String?
    let content: String?

    // Optional breakthrough metadata
    let problem: String?
    let difficulty: Int?
    let sha: String?
    let baselineSha: String?
    let timestamp: String?
    let scoreBefore: Double?
    let scoreAfter: Double?
    let deltaRaw: Double?
    let deltaWeighted: Double?

    // Optional query scoring metadata
    let queryScore: Double?
    let overlapScore: Double?
    let matchedReasons: [String]?

    var shortSha: String? {
        guard let sha, !sha.isEmpty else { return nil }
        return String(sha.prefix(7))
    }

    var confidencePercent: Int {
        Int((min(1.0, max(0.0, confidence)) * 100.0).rounded())
    }

    var isAntiPattern: Bool {
        status == "anti-pattern" || portability == "anti-pattern"
    }

    enum CodingKeys: String, CodingKey {
        case id, pillar, title, summary, category, tags, bottlenecks, regimes, dtypes
        case portability, status, confidence, wins, fails, evidence, content
        case problem, difficulty, sha, timestamp
        case priorAlpha = "prior_alpha"
        case priorBeta = "prior_beta"
        case legacyId = "legacy_id"
        case relatedSnippets = "related_snippets"
        case docPath = "doc_path"
        case absDocPath = "abs_doc_path"
        case baselineSha = "baseline_sha"
        case scoreBefore = "score_before"
        case scoreAfter = "score_after"
        case deltaRaw = "delta_raw"
        case deltaWeighted = "delta_weighted"
        case queryScore = "query_score"
        case overlapScore = "overlap_score"
        case matchedReasons = "matched_reasons"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        legacyId = try c.decodeIfPresent(String.self, forKey: .legacyId)
        pillar = try c.decodeIfPresent(String.self, forKey: .pillar) ?? "community"
        title = try c.decodeIfPresent(String.self, forKey: .title) ?? id
        summary = try c.decodeIfPresent(String.self, forKey: .summary) ?? ""
        category = try c.decodeIfPresent(String.self, forKey: .category) ?? "general"
        tags = try c.decodeIfPresent([String].self, forKey: .tags) ?? []
        bottlenecks = try c.decodeIfPresent([String].self, forKey: .bottlenecks) ?? []
        regimes = try c.decodeIfPresent([String].self, forKey: .regimes) ?? []
        dtypes = try c.decodeIfPresent([String].self, forKey: .dtypes) ?? []
        portability = try c.decodeIfPresent(String.self, forKey: .portability) ?? "portable"
        status = try c.decodeIfPresent(String.self, forKey: .status) ?? "unverified"
        priorAlpha = try c.decodeIfPresent(Double.self, forKey: .priorAlpha)
        priorBeta = try c.decodeIfPresent(Double.self, forKey: .priorBeta)
        confidence = try c.decodeIfPresent(Double.self, forKey: .confidence) ?? 0.5
        wins = try c.decodeIfPresent(Int.self, forKey: .wins) ?? 0
        fails = try c.decodeIfPresent(Int.self, forKey: .fails) ?? 0
        evidence = try c.decodeIfPresent([TriadKBEvidence].self, forKey: .evidence) ?? []
        relatedSnippets = try c.decodeIfPresent([String].self, forKey: .relatedSnippets) ?? []
        docPath = try c.decodeIfPresent(String.self, forKey: .docPath) ?? ""
        absDocPath = try c.decodeIfPresent(String.self, forKey: .absDocPath)
        content = try c.decodeIfPresent(String.self, forKey: .content)
        problem = try c.decodeIfPresent(String.self, forKey: .problem)
        difficulty = try c.decodeIfPresent(Int.self, forKey: .difficulty)
        sha = try c.decodeIfPresent(String.self, forKey: .sha)
        baselineSha = try c.decodeIfPresent(String.self, forKey: .baselineSha)
        timestamp = try c.decodeIfPresent(String.self, forKey: .timestamp)
        scoreBefore = try c.decodeIfPresent(Double.self, forKey: .scoreBefore)
        scoreAfter = try c.decodeIfPresent(Double.self, forKey: .scoreAfter)
        deltaRaw = try c.decodeIfPresent(Double.self, forKey: .deltaRaw)
        deltaWeighted = try c.decodeIfPresent(Double.self, forKey: .deltaWeighted)
        queryScore = try c.decodeIfPresent(Double.self, forKey: .queryScore)
        overlapScore = try c.decodeIfPresent(Double.self, forKey: .overlapScore)
        matchedReasons = try c.decodeIfPresent([String].self, forKey: .matchedReasons)
    }
}

struct TriadKBListResponse: Codable {
    let ok: Bool
    let parhelionHome: String?
    let pillar: String?
    let count: Int
    let entries: [TriadKBEntry]

    enum CodingKeys: String, CodingKey {
        case ok, pillar, count, entries
        case parhelionHome = "parhelion_home"
    }
}

struct TriadKBShowResponse: Codable {
    let ok: Bool
    let entry: TriadKBEntry?
    let error: String?
}

struct TriadKBQueryResponse: Codable {
    let ok: Bool
    let parhelionHome: String?
    let totalScanned: Int?
    let pillarCounts: [String: Int]?
    let resultCount: Int?
    let antiPatternCount: Int?
    let results: [TriadKBEntry]?
    let entries: [TriadKBEntry]?
    let antiPatterns: [TriadKBEntry]?

    var resolvedEntries: [TriadKBEntry] {
        results ?? entries ?? []
    }

    enum CodingKeys: String, CodingKey {
        case ok, results, entries
        case parhelionHome = "parhelion_home"
        case totalScanned = "total_scanned"
        case pillarCounts = "pillar_counts"
        case resultCount = "result_count"
        case antiPatternCount = "anti_pattern_count"
        case antiPatterns = "anti_patterns"
    }
}

struct TriadKBFeedbackResponse: Codable {
    let ok: Bool
    let entry: TriadKBEntry?
    let action: String?
    let transition: String?
    let createdSnippetId: String?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case ok, entry, action, transition, error
        case createdSnippetId = "created_snippet_id"
    }
}

struct KBEvolutionEvent: Codable, Identifiable, Hashable {
    var id: String { eventId }
    let eventId: String
    let timestamp: String
    let action: String
    let transition: String?
    let pillar: String
    let entryId: String
    let reverted: Bool?
    let revertedEventId: String?
    let createdSnippetId: String?
    let evidence: TriadKBEvidence?

    enum CodingKeys: String, CodingKey {
        case timestamp, action, transition, pillar, reverted, evidence
        case eventId = "event_id"
        case entryId = "entry_id"
        case revertedEventId = "reverted_event_id"
        case createdSnippetId = "created_snippet_id"
    }
}

struct KBEvolutionHistoryResponse: Codable {
    let ok: Bool
    let logPath: String
    let totalEvents: Int
    let events: [KBEvolutionEvent]

    enum CodingKeys: String, CodingKey {
        case ok, events
        case logPath = "log_path"
        case totalEvents = "total_events"
    }
}

struct KBRollbackResponse: Codable {
    let ok: Bool
    let revertedEventId: String?
    let entryId: String?
    let pillar: String?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case ok, pillar, error
        case revertedEventId = "reverted_event_id"
        case entryId = "entry_id"
    }
}
