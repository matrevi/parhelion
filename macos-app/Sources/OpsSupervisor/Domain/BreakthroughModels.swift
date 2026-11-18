import Foundation

struct BreakthroughEntry: Codable, Identifiable, Hashable {
    let id: String
    let problem: String
    let difficulty: Int
    let sha: String
    let baselineSha: String?
    let timestamp: String
    let scoreBefore: Double
    let scoreAfter: Double
    let deltaRaw: Double
    let deltaWeighted: Double
    let status: String
    let category: String
    let tags: [String]
    let title: String
    let bottleneck: String
    let mechanisms: [String]
    let precisionGuard: String
    let transferableRule: String
    let docPath: String
    let absDocPath: String?
    let markdown: String?

    var shortSha: String { String(sha.prefix(7)) }

    enum CodingKeys: String, CodingKey {
        case id, problem, difficulty, sha, timestamp, status, category, tags, title, bottleneck, mechanisms, markdown
        case baselineSha = "baseline_sha"
        case scoreBefore = "score_before"
        case scoreAfter = "score_after"
        case deltaRaw = "delta_raw"
        case deltaWeighted = "delta_weighted"
        case precisionGuard = "precision_guard"
        case transferableRule = "transferable_rule"
        case docPath = "doc_path"
        case absDocPath = "abs_doc_path"
    }
}

struct BreakthroughCatalogResponse: Codable {
    let threshold: Double
    let directory: String
    let entries: [BreakthroughEntry]
    /// Agent receipts collected by `opsb bridge collect` (additive field on index.json).
    let summaries: [BreakthroughSummary]?
    let summaryCount: Int?
}
