import Foundation

struct FastAlert: Codable, Identifiable, Hashable {
    var id: String { "\(code)-\(message)" }
    let code: String
    let severity: String
    let metric: String?
    let value: Double?
    let threshold: Double?
    let message: String
    let suggestedKbTags: [String]?

    enum CodingKeys: String, CodingKey {
        case code, severity, metric, value, threshold, message
        case suggestedKbTags = "suggested_kb_tags"
    }
}

struct ElfGuardMetrics: Codable, Hashable {
    let kernelOCount: Int?
    let kernelOMaxBytes: Int?
    let kernelOSumBytes: Int?
    let kernelOMaxTextBytes: Int?
    let phaseACpuSec: Double?
    let templateInstantiationCount: Int?
    let templateSource: String?
    let hasTelemetry: Bool

    enum CodingKeys: String, CodingKey {
        case kernelOCount = "kernel_o_count"
        case kernelOMaxBytes = "kernel_o_max_bytes"
        case kernelOSumBytes = "kernel_o_sum_bytes"
        case kernelOMaxTextBytes = "kernel_o_max_text_bytes"
        case phaseACpuSec = "phase_a_cpu_sec"
        case templateInstantiationCount = "template_instantiation_count"
        case templateSource = "template_source"
        case hasTelemetry = "has_telemetry"
    }
}

struct ElfGuardResult: Codable, Identifiable, Hashable {
    var id: String { title }
    let title: String
    let sha: String
    let status: String
    let metrics: ElfGuardMetrics
    let alerts: [FastAlert]
}

struct ElfBatchResponse: Codable {
    let workspace: String
    let count: Int
    let redlineCount: Int
    let warnCount: Int
    let problems: [ElfGuardResult]

    enum CodingKeys: String, CodingKey {
        case workspace, count, problems
        case redlineCount = "redline_count"
        case warnCount = "warn_count"
    }
}

struct Cold3SpikeCase: Codable, Identifiable, Hashable {
    var id: String { caseName }
    let caseName: String
    let run1Us: Double
    let run2Us: Double
    let run3Us: Double
    let ratio: Double
    let diffUs: Double

    enum CodingKeys: String, CodingKey {
        case ratio
        case caseName = "case"
        case run1Us = "run1_us"
        case run2Us = "run2_us"
        case run3Us = "run3_us"
        case diffUs = "diff_us"
    }
}

struct Cold3Metrics: Codable, Hashable {
    let firstIterUs: Double?
    let cold3RunMeanUs: Double?
    let warmMedianUs: Double?
    let coldTaxUs: Double?
    let coldSpreadMedianUs: Double?
    let bimodalSpikeDetected: Bool
    let explicitCold3Cases: Int
    let spikeCases: [Cold3SpikeCase]
    let hasTelemetry: Bool

    enum CodingKeys: String, CodingKey {
        case firstIterUs = "first_iter_us"
        case cold3RunMeanUs = "cold_3run_mean_us"
        case warmMedianUs = "warm_median_us"
        case coldTaxUs = "cold_tax_us"
        case coldSpreadMedianUs = "cold_spread_median_us"
        case bimodalSpikeDetected = "bimodal_spike_detected"
        case explicitCold3Cases = "explicit_cold3_cases"
        case spikeCases = "spike_cases"
        case hasTelemetry = "has_telemetry"
    }
}

struct Cold3Result: Codable, Identifiable, Hashable {
    var id: String { title }
    let title: String
    let sha: String
    let status: String
    let metrics: Cold3Metrics
    let alerts: [FastAlert]
}

struct PmuSummaryMetrics: Codable, Hashable {
    let aivVecRatio: Double?
    let aivMte2Ratio: Double?
    let aivMte3Ratio: Double?
    let aivScalarRatio: Double?
    let bankConflictRatio: Double?
    let aivUbBankConflict: Double?
    let samples: Int

    enum CodingKeys: String, CodingKey {
        case samples
        case aivVecRatio = "aiv_vec_ratio"
        case aivMte2Ratio = "aiv_mte2_ratio"
        case aivMte3Ratio = "aiv_mte3_ratio"
        case aivScalarRatio = "aiv_scalar_ratio"
        case bankConflictRatio = "bank_conflict_ratio"
        case aivUbBankConflict = "aiv_ub_bank_conflict"
    }
}

struct PmuBucketRow: Codable, Identifiable, Hashable {
    var id: String { bucket }
    let bucket: String
    let stratum: String
    let oursUs: Double
    let torchUs: Double?
    let ratioVsTorch: Double?
    let wHat: Double?
    let headroomScore: Double

    enum CodingKeys: String, CodingKey {
        case bucket, stratum
        case oursUs = "ours_us"
        case torchUs = "torch_us"
        case ratioVsTorch = "ratio_vs_torch"
        case wHat = "w_hat"
        case headroomScore = "headroom_score"
    }
}

struct PmuSubGuard<M: Codable & Hashable>: Codable, Hashable {
    let status: String
    let metrics: M
    let alerts: [FastAlert]
}

struct PmuTorchCard: Codable, Hashable {
    let title: String
    let sha: String
    let runtimeSoc: String?
    let dominantBottleneck: String
    let pmu: PmuSummaryMetrics
    let buckets: [PmuBucketRow]
    let topBuckets: [PmuBucketRow]
    let elfGuard: PmuSubGuard<ElfGuardMetrics>?
    let cold3: PmuSubGuard<Cold3Metrics>?
    let suggestedKbTags: [String]
    let markdownCard: String

    enum CodingKeys: String, CodingKey {
        case title, sha, pmu, buckets, cold3
        case runtimeSoc = "runtime_soc"
        case dominantBottleneck = "dominant_bottleneck"
        case topBuckets = "top_buckets"
        case elfGuard = "elf_guard"
        case suggestedKbTags = "suggested_kb_tags"
        case markdownCard = "markdown_card"
    }
}

struct FastFeedbackSummary: Hashable {
    let elf: ElfGuardResult
    let cold3: Cold3Result
    let pmu: PmuTorchCard?
}
