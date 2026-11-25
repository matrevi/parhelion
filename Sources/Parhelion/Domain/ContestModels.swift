import Foundation

struct SupervisorSnapshot: Codable {
    let sampledAt: String
    let contest: ContestSummary
    let scheduler: SchedulerState
    let problems: [ProblemItem]
    let submissionQueue: [SubmissionQueueItem]
    var npu: NpuTelemetry
    let auth: AuthStatus
    let skills: SkillsStatus
    let workspaces: [String]
    let language: String?

    enum CodingKeys: String, CodingKey {
        case sampledAt = "sampled_at"
        case contest, scheduler, problems
        case submissionQueue = "submission_queue"
        case npu, auth, skills, workspaces, language
    }

    func updatingNpu(_ newNpu: NpuTelemetry) -> SupervisorSnapshot {
        var copy = self
        copy.npu = newNpu
        return copy
    }
}

struct ContestSummary: Codable {
    let workspace: String
    let layout: String
    let contestId: String
    let title: String
    let teamName: String
    let gitOwner: String
    let repoPrefix: String
    let npuSshHost: String
    let contestUrl: String
    let gitcodeUrl: String
    let problemCount: Int
    let passingCount: Int
    let rawScore: Double
    let maxRawScore: Int
    let weightedScore: Double
    let maxWeightedScore: Int
    let closingPotentialWeighted: Double
    let modelMaePts: Double?
    /// Contest-wide team ranking from the public board (absent until first fetch).
    let teamRank: Int?
    let captainAccountId: String?
    let rankingState: String?
    let totalTeams: Int?
    let teamRankScore: Double?
    let teamRankFetchedAt: String?

    enum CodingKeys: String, CodingKey {
        case workspace, layout, title
        case contestId = "contest_id"
        case teamName = "team_name"
        case gitOwner = "git_owner"
        case repoPrefix = "repo_prefix"
        case npuSshHost = "npu_ssh_host"
        case contestUrl = "contest_url"
        case gitcodeUrl = "gitcode_url"
        case problemCount = "problem_count"
        case passingCount = "passing_count"
        case rawScore = "raw_score"
        case maxRawScore = "max_raw_score"
        case weightedScore = "weighted_score"
        case maxWeightedScore = "max_weighted_score"
        case closingPotentialWeighted = "closing_potential_weighted"
        case modelMaePts = "model_mae_pts"
        case teamRank = "team_rank"
        case captainAccountId = "captain_account_id"
        case rankingState = "ranking_state"
        case totalTeams = "total_teams"
        case teamRankScore = "team_rank_score"
        case teamRankFetchedAt = "team_rank_fetched_at"
    }
}

struct CaseRunSummary: Codable, Hashable {
    struct Bucket: Codable, Hashable {
        struct Member: Codable, Hashable, Identifiable {
            let id: String
            let name: String
            let status: String
            let us: Double?
        }
        let label: String
        let stratum: String?
        let members: [Member]
    }
    let buckets: [Bucket]?
    let formal: Bool?
    let experiment: String?
    let passed: Int?
    let selected: Int?
    let total: Int?
    let failures: [String]?
    let report: String?
}

struct ProblemItem: Codable, Identifiable, Hashable {
    var id: String { title }
    let title: String
    let slug: String
    let repoSlug: String
    let problemId: String
    let problemUrl: String?
    let numericId: Int?
    let difficulty: Int
    let sdkVersion: String
    let path: String
    let status: String
    let rank: Int?
    let totalTeams: Int?
    let currentScore: Double
    let latestStatus: String?
    let latestScore: Double?
    let latestSha: String?
    let latestAt: String?
    let boardListed: Bool?
    let boardScore: Double?
    func latestScoreText(_ lang: AppLanguage) -> String {
        if let latestScore { return String(format: "%.2f", latestScore) }
        return latestStatus == "Pass" ? lang.tr("待回分", "Pending") : "—"
    }
    func boardScoreText(_ lang: AppLanguage) -> String {
        if boardListed == false { return lang.tr("未上榜", "Unranked") }
        return boardScore.map { String(format: "%.2f", $0) } ?? "—"
    }
    let weightedScore: Double
    let bestScore: Double
    let rawBestScore: Double?
    let rawBestAt: String?
    let tbestDecayGamma: Double?
    let activeSha: String?
    let bestSha: String
    let leaderScore: Double
    let leaderNickname: String
    let publicMeanUs: Double?
    let tbestMeanUs: Double?
    let publicPts: Double
    let hiddenPts: Double
    let impliedHiddenRatio: Double
    let publicTimesUs: [Double]
    let publicTbestUs: [Double]
    let leaderTimesUs: [Double]
    let publicRatios: [Double]
    let wsptPriority: Double
    let wsptTag: String
    let circuitOpen: Bool
    let pressure12h: Int
    let headSha: String
    let headSubject: String
    var caseRun: CaseRunSummary? = nil
    var judgeCaseCount: Int? = nil
    let headAgeMin: Int?
    let calibration: ProblemCalibration?
    let testcases: [TestcaseItem]
    let history: [ProblemHistoryItem]

    enum CodingKeys: String, CodingKey {
        case title, slug, path, status, rank, difficulty, calibration, testcases, history
        case repoSlug = "repo_slug"
        case problemId = "problem_id"
        case problemUrl = "problem_url"
        case numericId = "numeric_id"
        case sdkVersion = "sdk_version"
        case totalTeams = "total_teams"
        case currentScore = "current_score"
        case latestStatus = "latest_status", latestScore = "latest_score", latestSha = "latest_sha"
        case latestAt = "latest_at"
        case boardListed = "board_listed", boardScore = "board_score"
        case weightedScore = "weighted_score"
        case bestScore = "best_score"
        case rawBestScore = "raw_best_score"
        case rawBestAt = "raw_best_at"
        case tbestDecayGamma = "tbest_decay_gamma"
        case activeSha = "active_sha"
        case bestSha = "best_sha"
        case leaderScore = "leader_score"
        case leaderNickname = "leader_nickname"
        case publicMeanUs = "public_mean_us"
        case tbestMeanUs = "tbest_mean_us"
        case publicPts = "public_pts"
        case hiddenPts = "hidden_pts"
        case impliedHiddenRatio = "implied_hidden_ratio"
        case publicTimesUs = "public_times_us"
        case publicTbestUs = "public_tbest_us"
        case leaderTimesUs = "leader_times_us"
        case publicRatios = "public_ratios"
        case wsptPriority = "wspt_priority"
        case wsptTag = "wspt_tag"
        case circuitOpen = "circuit_open"
        case pressure12h = "pressure_12h"
        case headSha = "head_sha"
        case headSubject = "head_subject"
        case caseRun = "case_run"
        case judgeCaseCount = "judge_case_count"
        case headAgeMin = "head_age_min"
    }
    // NOTE: Equatable/Hashable are synthesized over ALL fields on purpose. A hand-written `==`
    // that only compared title/currentScore/headSha let SwiftUI skip re-rendering views holding a
    // `ProblemItem` (inspector, banners) when status/rank/calibration/history changed, leaving
    // stale badges that contradicted the table.
}

struct TestcaseItem: Codable, Identifiable, Hashable {
    var id: Int { index }
    let index: Int
    let caseId: String
    let numericId: Int?
    let isHidden: Bool
    let tbestUs: Double?
    let oursUs: Double?
    let leaderUs: Double?
    let ratio: Double?
    let points: Double?

    enum CodingKeys: String, CodingKey {
        case index, ratio, points
        case caseId = "case_id"
        case numericId = "numeric_id"
        case isHidden = "is_hidden"
        case tbestUs = "tbest_us"
        case oursUs = "ours_us"
        case leaderUs = "leader_us"
    }
}

struct ProblemHistoryItem: Codable, Identifiable, Hashable {
    var id: String { "\(submissionId ?? "")-\(at)-\(sha)-\(status)" }
    let at: String
    let sha: String
    let status: String
    let score: Double?
    let delta: Double?
    let publicPts: Double?
    let hiddenPts: Double?
    let note: String
    let submissionId: String?
    let estimatedScore: Double?
    let residualGap: Double?

    enum CodingKeys: String, CodingKey {
        case at, sha, status, score, delta, note
        case publicPts = "public_pts"
        case hiddenPts = "hidden_pts"
        case submissionId = "submission_id"
        case estimatedScore = "estimated_score"
        case residualGap = "residual_gap"
    }
}
