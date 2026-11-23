import Foundation

struct PipelineAccountSlot: Codable, Identifiable, Hashable {
    let id: String
    let label: String
    let loggedIn: Bool
    let enabled: Bool
    let cooldownRemainingSec: Int
    let cooldownEndsEpoch: Double
    let usedToday: Int?
    let dailyLimit: Int?
    let remainingToday: Int?
    let quotaExhausted: Bool?
    let phase: String
    let activeTitle: String?

    var isExhausted: Bool {
        if let quotaExhausted { return quotaExhausted }
        let used = usedToday ?? 0
        let lim = dailyLimit ?? 50
        return used >= lim
    }

    enum CodingKeys: String, CodingKey {
        case id, label, enabled, phase
        case loggedIn = "logged_in"
        case cooldownRemainingSec = "cooldown_remaining_sec"
        case cooldownEndsEpoch = "cooldown_ends_epoch"
        case usedToday = "used_today"
        case dailyLimit = "daily_limit"
        case remainingToday = "remaining_today"
        case quotaExhausted = "quota_exhausted"
        case activeTitle = "active_title"
    }
}

struct ActiveSubmissionItem: Codable, Identifiable, Hashable {
    let id: String
    let title: String
    let sha: String
    let shortSha: String
    let phase: String
    let accountId: String
    let accountLabel: String
    let remoteSubmissionId: String?
    let remoteJudgeId: Int?
    let remoteStatus: String?
    let startedEpoch: Double
    let elapsedSec: Int

    enum CodingKeys: String, CodingKey {
        case id, title, sha, phase
        case shortSha = "short_sha"
        case accountId = "account_id"
        case accountLabel = "account_label"
        case remoteSubmissionId = "remote_submission_id"
        case remoteJudgeId = "remote_judge_id"
        case remoteStatus = "remote_status"
        case startedEpoch = "started_epoch"
        case elapsedSec = "elapsed_sec"
    }
}

struct LivePipelineState: Codable {
    let isBusy: Bool
    let localBusy: Bool?
    let canSubmit: Bool?
    let phase: String
    let activeTitle: String?
    let activeSha: String?
    let activeShortSha: String?
    let remoteSubmissionId: String?
    let remoteJudgeId: Int?
    let remoteStatus: String?
    let judgeStartedEpoch: Double?
    let judgeElapsedSec: Int
    let cooldownEndsEpoch: Double
    let cooldownRemainingSec: Int
    let rateLimitGapSec: Int
    let serverClockOffsetSec: Double
    let accountSlots: [PipelineAccountSlot]?
    let activeSubmissions: [ActiveSubmissionItem]?
    let inflightKeys: [String]?

    /// True ONLY while a local worker is actively packaging, uploading, or waiting cooldown.
    /// Remote judging (`remote_queued`/`remote_compiling`/`remote_running`) does NOT block
    /// submitting the next problem once the 190s platform cooldown has elapsed.
    var isLocalBusy: Bool {
        if let localBusy { return localBusy }
        return phase == "waiting_cooldown" || phase == "packaging" || phase == "uploading"
    }

    enum CodingKeys: String, CodingKey {
        case phase
        case isBusy = "is_busy"
        case localBusy = "local_busy"
        case canSubmit = "can_submit"
        case activeTitle = "active_title"
        case activeSha = "active_sha"
        case activeShortSha = "active_short_sha"
        case remoteSubmissionId = "remote_submission_id"
        case remoteJudgeId = "remote_judge_id"
        case remoteStatus = "remote_status"
        case judgeStartedEpoch = "judge_started_epoch"
        case judgeElapsedSec = "judge_elapsed_sec"
        case cooldownEndsEpoch = "cooldown_ends_epoch"
        case cooldownRemainingSec = "cooldown_remaining_sec"
        case rateLimitGapSec = "rate_limit_gap_sec"
        case serverClockOffsetSec = "server_clock_offset_sec"
        case accountSlots = "account_slots"
        case activeSubmissions = "active_submissions"
        case inflightKeys = "inflight_keys"
    }
}

/// Compact `scheduler.backfill` passthrough (B0 auto-backfill lane, sourced from
/// `scheduler/backfill.py::BackfillScheduler.summary` via `python -m scheduler.backfill_status`).
struct BackfillPassthrough: Codable, Hashable {
    let lane: String
    let tier: Int
    let autoEnabled: Bool
    let unpairedTotal: Int
    let unpairedByOperator: [String: Int]
    let specPlaceholderTotal: Int
    let specPlaceholderByOperator: [String: Int]
    let running: Bool
    let runningTitle: String?
    let estSessionSec: Double?
    let ledgerSha: String?
    let queuedCount: Int?
    let etaSec: Double?
    let etaWallSec: Double?
    let cached: Bool?
    let lastRun: BackfillLastRun?

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        lane = (try? c.decodeIfPresent(String.self, forKey: .lane)).flatMap { $0 } ?? "B0"
        tier = (try? c.decodeIfPresent(Int.self, forKey: .tier)).flatMap { $0 } ?? 0
        autoEnabled = (try? c.decodeIfPresent(Bool.self, forKey: .autoEnabled)).flatMap { $0 } ?? false
        unpairedTotal = (try? c.decodeIfPresent(Int.self, forKey: .unpairedTotal)).flatMap { $0 } ?? 0
        unpairedByOperator = (try? c.decodeIfPresent([String: Int].self, forKey: .unpairedByOperator)).flatMap { $0 } ?? [:]
        specPlaceholderTotal = (try? c.decodeIfPresent(Int.self, forKey: .specPlaceholderTotal)).flatMap { $0 } ?? 0
        specPlaceholderByOperator = (try? c.decodeIfPresent([String: Int].self, forKey: .specPlaceholderByOperator)).flatMap { $0 } ?? [:]
        running = (try? c.decodeIfPresent(Bool.self, forKey: .running)).flatMap { $0 } ?? false
        runningTitle = (try? c.decodeIfPresent(String.self, forKey: .runningTitle)).flatMap { $0 }
        estSessionSec = (try? c.decodeIfPresent(Double.self, forKey: .estSessionSec)).flatMap { $0 }
        ledgerSha = (try? c.decodeIfPresent(String.self, forKey: .ledgerSha)).flatMap { $0 }
        queuedCount = (try? c.decodeIfPresent(Int.self, forKey: .queuedCount)).flatMap { $0 }
        etaSec = (try? c.decodeIfPresent(Double.self, forKey: .etaSec)).flatMap { $0 }
        etaWallSec = (try? c.decodeIfPresent(Double.self, forKey: .etaWallSec)).flatMap { $0 }
        cached = (try? c.decodeIfPresent(Bool.self, forKey: .cached)).flatMap { $0 }
        lastRun = (try? c.decodeIfPresent(BackfillLastRun.self, forKey: .lastRun)).flatMap { $0 }
    }

    enum CodingKeys: String, CodingKey {
        case lane, tier, running, cached
        case autoEnabled = "auto_enabled"
        case unpairedTotal = "unpaired_total"
        case unpairedByOperator = "unpaired_by_operator"
        case specPlaceholderTotal = "spec_placeholder_total"
        case specPlaceholderByOperator = "spec_placeholder_by_operator"
        case runningTitle = "running_title"
        case estSessionSec = "est_session_sec"
        case ledgerSha = "ledger_sha"
        case queuedCount = "queued_count"
        case etaSec = "eta_sec"
        case etaWallSec = "eta_wall_sec"
        case lastRun = "last_run"
    }
}

struct BackfillLastRun: Codable, Hashable {
    let title: String?
    let result: String?
    let finishedAt: String?
    let measuredCount: Int?

    enum CodingKeys: String, CodingKey {
        case title, result
        case finishedAt = "finished_at"
        case measuredCount = "measured_count"
    }
}

struct SchedulerState: Codable {
    let autoConsumerEnabled: Bool
    let rateLimitGapSec: Int
    let cooldownRemainingSec: Int
    let cooldownEndsEpoch: Double?
    let rateReady: Bool
    let lastSubmittedAt: String?
    let dailyQuotaLimit: Int
    let usedToday24h: Int
    let closingReserve: Int
    let availableNormalShots: Int
    let noiseFloorDelta: Double
    let breakthroughThreshold: Double?
    let breakthroughCount: Int?
    let circuitBreakerLimit: Int
    let queueLockHeld: Bool
    let actionableCount: Int
    let pipeline: LivePipelineState?
    /// B0 auto-backfill lane snapshot; `nil` when the probe was skipped/unavailable.
    let backfill: BackfillPassthrough?

    enum CodingKeys: String, CodingKey {
        case pipeline, backfill
        case autoConsumerEnabled = "auto_consumer_enabled"
        case rateLimitGapSec = "rate_limit_gap_sec"
        case cooldownRemainingSec = "cooldown_remaining_sec"
        case cooldownEndsEpoch = "cooldown_ends_epoch"
        case rateReady = "rate_ready"
        case lastSubmittedAt = "last_submitted_at"
        case dailyQuotaLimit = "daily_quota_limit"
        case usedToday24h = "used_today_24h"
        case closingReserve = "closing_reserve"
        case availableNormalShots = "available_normal_shots"
        case noiseFloorDelta = "noise_floor_delta"
        case breakthroughThreshold = "breakthrough_threshold"
        case breakthroughCount = "breakthrough_count"
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
    let ucbDelta: Double?
    let exploreEligible: Bool?
    let calibrationTrust: Double?

    enum CodingKeys: String, CodingKey {
        case safetyOk = "safety_ok"
        case noiseOk = "noise_ok"
        case circuitOk = "circuit_ok"
        case quotaOk = "quota_ok"
        case allPass = "all_pass"
        case deltaExpected = "delta_expected"
        case wsptScore = "wspt_score"
        case ucbDelta = "ucb_delta"
        case exploreEligible = "explore_eligible"
        case calibrationTrust = "calibration_trust"
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
    let breakthroughDelta: Double?
    let isBreakthrough: Bool?
    let experienceId: String?
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
        case breakthroughDelta = "breakthrough_delta"
        case isBreakthrough = "is_breakthrough"
        case experienceId = "experience_id"
        case wsptPriority = "wspt_priority"
        case canApprove = "can_approve"
        case canSkip = "can_skip"
        case canRestore = "can_restore"
    }
}

struct JumpSubmitResult: Sendable {
    let ok: Bool
    let message: String
    let isDuplicateOrMutex: Bool

    static func parse(from data: Data, problem: String, sha: String) -> JumpSubmitResult {
        guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return JumpSubmitResult(ok: false, message: "提交启动失败", isDuplicateOrMutex: false)
        }
        let ok = (obj["ok"] as? Bool) ?? false
        let err = (obj["error"] as? String) ?? "已启动提交"
        let dupFlags = [
            "duplicate_inflight",
            "duplicate_terminal",
            "duplicate_recent_outcome",
            "duplicate_sync_error",
            "operator_inflight",
            "operator_inflight_mutex",
        ]
        let isDup = dupFlags.contains { (obj[$0] as? Bool) == true }
        return JumpSubmitResult(
            ok: ok,
            message: ok ? "已提交 \(problem) (\(String(sha.prefix(7)))) 至发送通道" : err,
            isDuplicateOrMutex: isDup
        )
    }
}
