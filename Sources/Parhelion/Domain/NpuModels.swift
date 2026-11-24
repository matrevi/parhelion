import Foundation

struct TelemetrySample: Codable, Identifiable, Hashable {
    var id: Double { ts }
    let ts: Double
    let aicorePct: Double
    let aivectorPct: Double?
    let hbmBwPct: Double?
    let powerW: Double
    let tempC: Int
    let hbmPct: Double
    let cpuPct: Double
    let ramPct: Double

    enum CodingKeys: String, CodingKey {
        case ts
        case aicorePct = "aicore_pct"
        case aivectorPct = "aivector_pct"
        case hbmBwPct = "hbm_bw_pct"
        case powerW = "power_w"
        case tempC = "temp_c"
        case hbmPct = "hbm_pct"
        case cpuPct = "cpu_pct"
        case ramPct = "ram_pct"
    }
}

struct DieDuty10m: Codable, Hashable {
    let npuExecPct: Double
    let aclHostPct: Double
    let lockWastePct: Double
    let freePct: Double

    enum CodingKeys: String, CodingKey {
        case npuExecPct = "npu_exec_pct"
        case aclHostPct = "acl_host_pct"
        case lockWastePct = "lock_waste_pct"
        case freePct = "free_pct"
    }
}

struct ParallelCpuBuildItem: Codable, Identifiable, Hashable {
    var id: String { "\(pid)-\(title)-\(shortSha)" }
    let pid: Int
    let title: String
    let sha: String
    let shortSha: String
    let elapsedSec: Int

    enum CodingKeys: String, CodingKey {
        case pid, title, sha
        case shortSha = "short_sha"
        case elapsedSec = "elapsed_sec"
    }
}

struct NpuTelemetry: Codable {
    let online: Bool
    let sshHost: String
    let rttMs: Double?
    let probeLatencyMs: Double?
    let probeBackend: String?
    let sampleMs: Double?
    let hostname: String?
    let arch: String?
    let sdkVersion: String?
    let smiVersion: String?
    let error: String?
    let primaryNpu: NpuDevice?
    let cpu: CpuMetrics?
    let memory: MemoryMetrics?
    let history: [TelemetrySample]?
    let dieDuty10m: DieDuty10m?
    let slotsPerHr: Double?
    let avgDieHoldSec: Double?
    let parallelCpuBuilds: [ParallelCpuBuildItem]?
    let dieLocks: [DieLockInfo]
    let tasks: [NpuTaskItem]

    enum CodingKeys: String, CodingKey {
        case online, hostname, arch, error, cpu, memory, history, tasks
        case sshHost = "ssh_host"
        case rttMs = "rtt_ms"
        case probeLatencyMs = "probe_latency_ms"
        case probeBackend = "probe_backend"
        case sampleMs = "sample_ms"
        case sdkVersion = "sdk_version"
        case smiVersion = "smi_version"
        case primaryNpu = "primary_npu"
        case dieDuty10m = "die_duty_10m"
        case slotsPerHr = "slots_per_hr"
        case avgDieHoldSec = "avg_die_hold_sec"
        case parallelCpuBuilds = "parallel_cpu_builds"
        case dieLocks = "die_locks"
    }
}

struct SchedulerHotState: Codable, Equatable {
    let autoConsumerEnabled: Bool
    let rateLimitGapSec: Int
    let cooldownRemainingSec: Int
    let cooldownEndsEpoch: Double
    let rateReady: Bool
    let dailyQuotaLimit: Int
    let usedToday24h: Int
    let closingReserve: Int
    let availableNormalShots: Int
    let noiseFloorDelta: Double
    let breakthroughThreshold: Double
    let queueLockHeld: Bool

    enum CodingKeys: String, CodingKey {
        case autoConsumerEnabled = "auto_consumer_enabled"
        case rateLimitGapSec = "rate_limit_gap_sec"
        case cooldownRemainingSec = "cooldown_remaining_sec"
        case cooldownEndsEpoch = "cooldown_ends_epoch"
        case rateReady = "rate_ready"
        case dailyQuotaLimit = "daily_quota_limit"
        case usedToday24h = "used_today_24h"
        case closingReserve = "closing_reserve"
        case availableNormalShots = "available_normal_shots"
        case noiseFloorDelta = "noise_floor_delta"
        case breakthroughThreshold = "breakthrough_threshold"
        case queueLockHeld = "queue_lock_held"
    }
}

struct DaemonStats: Codable, Equatable {
    let streamConnected: Bool
    let probeBackend: String
    let dcmiSampleMs: Double
    let hotBuildMs: Double
    let coldRecomputeMs: Double
    let uptimeSec: Double

    enum CodingKeys: String, CodingKey {
        case streamConnected = "stream_connected"
        case probeBackend = "probe_backend"
        case dcmiSampleMs = "dcmi_sample_ms"
        case hotBuildMs = "hot_build_ms"
        case coldRecomputeMs = "cold_recompute_ms"
        case uptimeSec = "uptime_sec"
    }
}

struct HotStreamFrame: Codable {
    let ts: Double
    let workspace: String
    let coldEtag: String
    let npu: NpuTelemetry
    let pipeline: LivePipelineState
    let schedulerHot: SchedulerHotState
    let daemonStats: DaemonStats
    let agentMesh: AgentMeshHotState?

    enum CodingKeys: String, CodingKey {
        case ts, workspace, npu, pipeline
        case coldEtag = "cold_etag"
        case schedulerHot = "scheduler_hot"
        case daemonStats = "daemon_stats"
        case agentMesh = "agent_mesh"
    }
}

struct DaemonHealthResponse: Codable {
    let ok: Bool
    let pid: Int
    let port: Int
    let uptimeSec: Double
    let streamConnected: Bool
    let probeBackend: String
    let sshHost: String
    let workspace: String
    let coldEtag: String
    let dcmiSampleMs: Double
    let coldRecomputeMs: Double

    enum CodingKeys: String, CodingKey {
        case ok, pid, port, workspace
        case uptimeSec = "uptime_sec"
        case streamConnected = "stream_connected"
        case probeBackend = "probe_backend"
        case sshHost = "ssh_host"
        case coldEtag = "cold_etag"
        case dcmiSampleMs = "dcmi_sample_ms"
        case coldRecomputeMs = "cold_recompute_ms"
    }
}

struct NpuDevice: Codable {
    let npuId: Int
    let chipId: Int
    let socName: String
    let health: String
    let busId: String
    let powerW: Double?
    let tempC: Int?
    let aicorePct: Int
    let aivectorPct: Int?
    let hbmBwPct: Int?
    let ctrlcpuPct: Int?
    let hbmUsedMib: Int
    let hbmTotalMib: Int

    enum CodingKeys: String, CodingKey {
        case health
        case npuId = "npu_id"
        case chipId = "chip_id"
        case socName = "soc_name"
        case busId = "bus_id"
        case powerW = "power_w"
        case tempC = "temp_c"
        case aicorePct = "aicore_pct"
        case aivectorPct = "aivector_pct"
        case hbmBwPct = "hbm_bw_pct"
        case ctrlcpuPct = "ctrlcpu_pct"
        case hbmUsedMib = "hbm_used_mib"
        case hbmTotalMib = "hbm_total_mib"
    }
}

struct CpuMetrics: Codable {
    let cores: Int
    let load1m: Double
    let load5m: Double
    let load15m: Double
    let runningThreads: Int
    let totalThreads: Int
    let usagePct: Double?
    let perCorePct: [Double]?

    enum CodingKeys: String, CodingKey {
        case cores
        case load1m = "load_1m"
        case load5m = "load_5m"
        case load15m = "load_15m"
        case runningThreads = "running_threads"
        case totalThreads = "total_threads"
        case usagePct = "usage_pct"
        case perCorePct = "per_core_pct"
    }
}

struct MemoryMetrics: Codable {
    let totalMib: Int
    let usedMib: Int
    let availableMib: Int
    let totalGib: Double
    let usedGib: Double
    let cachedGib: Double?

    enum CodingKeys: String, CodingKey {
        case totalMib = "total_mib"
        case usedMib = "used_mib"
        case availableMib = "available_mib"
        case totalGib = "total_gib"
        case usedGib = "used_gib"
        case cachedGib = "cached_gib"
    }
}

struct DieLockInfo: Codable, Identifiable {
    var id: Int { die }
    let die: Int
    let lockPath: String
    let state: String
    let holderPid: Int?
    let problem: String?
    let sha: String?
    let shortSha: String?
    let stage: String?
    let elapsedSec: Int

    enum CodingKeys: String, CodingKey {
        case die, state, problem, sha, stage
        case lockPath = "lock_path"
        case holderPid = "holder_pid"
        case shortSha = "short_sha"
        case elapsedSec = "elapsed_sec"
    }
}

struct NpuTaskItem: Codable, Identifiable {
    var id: String { "\(pid)-\(state)-\(problem ?? "")" }
    let pid: Int
    let die: Int?
    let state: String
    let stage: String
    let lane: String?
    let isBackfill: Bool?
    let problem: String?
    let sha: String?
    let shortSha: String?
    let difficulty: Int?
    let wsptPriority: Double?
    let wsptTag: String?
    let elapsedSec: Int
    let estimatedWaitSec: Int?
    let boosted: Bool?
    let detail: String
    let subprocesses: Int

    var isBackfillTask: Bool {
        if isBackfill == true { return true }
        if lane?.uppercased() == "B0" { return true }
        return stage.uppercased().hasPrefix("BACKFILL")
    }

    enum CodingKeys: String, CodingKey {
        case pid, die, state, stage, lane, problem, sha, difficulty, boosted, detail, subprocesses
        case isBackfill = "is_backfill"
        case shortSha = "short_sha"
        case wsptPriority = "wspt_priority"
        case wsptTag = "wspt_tag"
        case elapsedSec = "elapsed_sec"
        case estimatedWaitSec = "estimated_wait_sec"
    }
}
