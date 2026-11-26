import Foundation

/// One `parhelion_test` registry row from the daemon's hot frame (`test_runs`). Digests are not
/// in the hot frame; `TestRunDetailResponse` fetches one on demand. Every field is optional so
/// older daemons and partial rows decode.
struct TestRunEntry: Codable, Identifiable, Equatable {
    var id: String { requestId }
    let requestId: String
    let problem: String
    let sha7: String
    let level: String
    let status: String
    let runId: String?
    let exitCode: Int?
    let agentId: String?
    let compare: String?
    let casesDone: Int?
    let cases: Int?
    let phase: String?
    let waitSec: Double?
    let holdSec: Double?
    let e2eSec: Double?
    let createdAt: String?
    let hasDigest: Bool

    enum CodingKeys: String, CodingKey {
        case problem, sha7, level, status, compare, cases, phase
        case requestId = "request_id"
        case runId = "run_id"
        case exitCode = "exit_code"
        case agentId = "agent_id"
        case casesDone = "cases_done"
        case waitSec = "wait_sec"
        case holdSec = "hold_sec"
        case e2eSec = "e2e_sec"
        case createdAt = "created_at"
        case hasDigest = "has_digest"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        requestId = try c.decodeIfPresent(String.self, forKey: .requestId) ?? UUID().uuidString
        problem = try c.decodeIfPresent(String.self, forKey: .problem) ?? ""
        sha7 = try c.decodeIfPresent(String.self, forKey: .sha7) ?? ""
        level = try c.decodeIfPresent(String.self, forKey: .level) ?? ""
        status = try c.decodeIfPresent(String.self, forKey: .status) ?? ""
        runId = try c.decodeIfPresent(String.self, forKey: .runId)
        exitCode = try c.decodeIfPresent(Int.self, forKey: .exitCode)
        agentId = try c.decodeIfPresent(String.self, forKey: .agentId)
        compare = try c.decodeIfPresent(String.self, forKey: .compare)
        casesDone = try c.decodeIfPresent(Int.self, forKey: .casesDone)
        cases = try c.decodeIfPresent(Int.self, forKey: .cases)
        phase = try c.decodeIfPresent(String.self, forKey: .phase)
        waitSec = try c.decodeIfPresent(Double.self, forKey: .waitSec)
        holdSec = try c.decodeIfPresent(Double.self, forKey: .holdSec)
        e2eSec = try c.decodeIfPresent(Double.self, forKey: .e2eSec)
        createdAt = try c.decodeIfPresent(String.self, forKey: .createdAt)
        hasDigest = try c.decodeIfPresent(Bool.self, forKey: .hasDigest) ?? false
    }

    var isActive: Bool { status == "queued" || status == "running" }
}

/// The compact result digest of a finished run (at most 2KB, `case_bench.build_digest`).
struct TestRunDigest: Codable, Equatable {
    struct Summary: Codable, Equatable {
        let total: Int?
        let passed: Int?
    }
    struct Failure: Codable, Equatable {
        let id: String
        let status: String
        let detail: String?
    }
    struct CaseTiming: Codable, Equatable {
        let minUs: Double?
        let medianUs: Double?
        let coldUs: Double?
        enum CodingKeys: String, CodingKey {
            case minUs = "min_us"
            case medianUs = "median_us"
            case coldUs = "cold_us"
        }
    }
    struct Timing: Codable, Equatable {
        let eligible: Bool?
        let source: String?
        let drift: Double?
        let noisy: Bool?
        let cases: [String: CaseTiming]?
    }
    struct Baseline: Codable, Equatable {
        let sha: String?
        let geomeanSpeedup: Double?
        let cases: [String: Double]?
        enum CodingKeys: String, CodingKey {
            case sha, cases
            case geomeanSpeedup = "geomean_speedup"
        }
    }

    let level: String?
    let summary: Summary?
    let failures: [Failure]?
    let timing: Timing?
    let baseline: Baseline?
    let hint: String?
}

struct TestRunDetail: Codable, Equatable {
    let requestId: String?
    let status: String?
    let digest: TestRunDigest?
    let error: String?
    let reason: String?

    enum CodingKeys: String, CodingKey {
        case status, digest, error, reason
        case requestId = "request_id"
    }
}

struct TestRunDetailResponse: Codable {
    let ok: Bool
    let run: TestRunDetail?
}

/// A driver paused after a provider fault, with the workers it stopped (`agents/manager.py`).
struct DriverSuspension: Codable, Equatable, Identifiable, Hashable {
    var id: String { driver }
    let driver: String
    let since: String
    let reason: String
    let workers: [String]

    enum CodingKeys: String, CodingKey { case driver, since, reason, workers }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        driver = try c.decodeIfPresent(String.self, forKey: .driver) ?? ""
        since = try c.decodeIfPresent(String.self, forKey: .since) ?? ""
        reason = try c.decodeIfPresent(String.self, forKey: .reason) ?? ""
        workers = try c.decodeIfPresent([String].self, forKey: .workers) ?? []
    }
}
