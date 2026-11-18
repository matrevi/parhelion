import Foundation

struct NpuTelemetry: Codable {
    let online: Bool
    let sshHost: String
    let rttMs: Double?
    let probeLatencyMs: Double?
    let hostname: String?
    let arch: String?
    let sdkVersion: String?
    let smiVersion: String?
    let error: String?
    let primaryNpu: NpuDevice?
    let cpu: CpuMetrics?
    let memory: MemoryMetrics?
    let dieLocks: [DieLockInfo]
    let tasks: [NpuTaskItem]

    enum CodingKeys: String, CodingKey {
        case online, hostname, arch, error, cpu, memory, tasks
        case sshHost = "ssh_host"
        case rttMs = "rtt_ms"
        case probeLatencyMs = "probe_latency_ms"
        case sdkVersion = "sdk_version"
        case smiVersion = "smi_version"
        case primaryNpu = "primary_npu"
        case dieLocks = "die_locks"
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

    enum CodingKeys: String, CodingKey {
        case cores
        case load1m = "load_1m"
        case load5m = "load_5m"
        case load15m = "load_15m"
        case runningThreads = "running_threads"
        case totalThreads = "total_threads"
    }
}

struct MemoryMetrics: Codable {
    let totalMib: Int
    let usedMib: Int
    let availableMib: Int
    let totalGib: Double
    let usedGib: Double

    enum CodingKeys: String, CodingKey {
        case totalMib = "total_mib"
        case usedMib = "used_mib"
        case availableMib = "available_mib"
        case totalGib = "total_gib"
        case usedGib = "used_gib"
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
    var id: Int { pid }
    let pid: Int
    let die: Int?
    let state: String
    let stage: String
    let problem: String?
    let sha: String?
    let shortSha: String?
    let difficulty: Int?
    let wsptPriority: Double?
    let wsptTag: String?
    let elapsedSec: Int
    let detail: String
    let subprocesses: Int

    enum CodingKeys: String, CodingKey {
        case pid, die, state, stage, problem, sha, difficulty, detail, subprocesses
        case shortSha = "short_sha"
        case wsptPriority = "wspt_priority"
        case wsptTag = "wspt_tag"
        case elapsedSec = "elapsed_sec"
    }
}
