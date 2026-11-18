import Foundation

struct SchedulerState: Codable {
    let autoConsumerEnabled: Bool
    let rateLimitGapSec: Int
    let cooldownRemainingSec: Int
    let rateReady: Bool
    let lastSubmittedAt: String?
    let dailyQuotaLimit: Int
    let usedToday24h: Int
    let closingReserve: Int
    let availableNormalShots: Int
    let noiseFloorDelta: Double
    let circuitBreakerLimit: Int
    let queueLockHeld: Bool
    let actionableCount: Int

    enum CodingKeys: String, CodingKey {
        case autoConsumerEnabled = "auto_consumer_enabled"
        case rateLimitGapSec = "rate_limit_gap_sec"
        case cooldownRemainingSec = "cooldown_remaining_sec"
        case rateReady = "rate_ready"
        case lastSubmittedAt = "last_submitted_at"
        case dailyQuotaLimit = "daily_quota_limit"
        case usedToday24h = "used_today_24h"
        case closingReserve = "closing_reserve"
        case availableNormalShots = "available_normal_shots"
        case noiseFloorDelta = "noise_floor_delta"
        case circuitBreakerLimit = "circuit_breaker_limit"
        case queueLockHeld = "queue_lock_held"
        case actionableCount = "actionable_count"
    }
}

struct GateBreakdown: Codable, Hashable {
    let safetyOk: Bool
    let noiseOk: Bool
    let circuitOk: Bool
    let quotaOk: Bool
    let allPass: Bool
    let deltaExpected: Double
    let wsptScore: Double

    enum CodingKeys: String, CodingKey {
        case safetyOk = "safety_ok"
        case noiseOk = "noise_ok"
        case circuitOk = "circuit_ok"
        case quotaOk = "quota_ok"
        case allPass = "all_pass"
        case deltaExpected = "delta_expected"
        case wsptScore = "wspt_score"
    }
}

struct SubmissionQueueItem: Codable, Identifiable, Hashable {
    let id: String
    let kind: String
    let title: String
    let difficulty: Int
    let sha: String
    let shortSha: String
    let note: String
    let detail: String?
    let submissionId: String?
    let queuedAt: String
    let finishedAt: String?
    let badge: String
    let rawStatus: String
    let currentScore: Double
    let priorScore: Double?
    let estimatedScore: Double?
    let actualScore: Double?
    let deltaScore: Double?
    let wsptPriority: Double
    let gates: GateBreakdown
    let canApprove: Bool
    let canSkip: Bool
    let canRestore: Bool

    enum CodingKeys: String, CodingKey {
        case id, kind, title, difficulty, sha, note, detail, badge, gates
        case shortSha = "short_sha"
        case submissionId = "submission_id"
        case queuedAt = "queued_at"
        case finishedAt = "finished_at"
        case rawStatus = "raw_status"
        case currentScore = "current_score"
        case priorScore = "prior_score"
        case estimatedScore = "estimated_score"
        case actualScore = "actual_score"
        case deltaScore = "delta_score"
        case wsptPriority = "wspt_priority"
        case canApprove = "can_approve"
        case canSkip = "can_skip"
        case canRestore = "can_restore"
    }
}
