import Foundation

struct StratumCalibrationItem: Codable, Identifiable, Hashable {
    var id: String { stratum }
    let stratum: String
    let priorW: Double
    let learnedW: Double
    let efficiencyR: Double
    let currentPts: Double
    let headroomPts: Double

    enum CodingKeys: String, CodingKey {
        case stratum
        case priorW = "prior_w"
        case learnedW = "learned_w"
        case efficiencyR = "efficiency_r"
        case currentPts = "current_pts"
        case headroomPts = "headroom_pts"
    }
}

struct BenchCoverageGrid: Codable, Hashable {
    let rows: [String]
    let cols: [String]
    let cells: [[Int]]
}

struct ProblemCalibration: Codable, Hashable {
    let maePts: Double?
    let lastPredictedScore: Double?
    let lastResidualPts: Double?
    let escapeLock: Bool?
    let escapeSha: String?
    let unpairedBucketCount: Int?
    let strata: [StratumCalibrationItem]?
    let coverageGrid: BenchCoverageGrid?

    enum CodingKeys: String, CodingKey {
        case strata
        case maePts = "mae_pts"
        case lastPredictedScore = "last_predicted_score"
        case lastResidualPts = "last_residual_pts"
        case escapeLock = "escape_lock"
        case escapeSha = "escape_sha"
        case unpairedBucketCount = "unpaired_bucket_count"
        case coverageGrid = "coverage_grid"
    }
}
