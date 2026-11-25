import Foundation

enum AppLanguage: String, CaseIterable, Identifiable, Codable {
    case zh = "zh"
    case en = "en"

    var id: String { rawValue }

    var displayName: String {
        switch self {
        case .zh: return "中文"
        case .en: return "English"
        }
    }

    var shortLabel: String {
        switch self {
        case .zh: return "中文"
        case .en: return "EN"
        }
    }

    func tr(_ zh: String, _ en: String) -> String {
        self == .zh ? zh : en
    }

    func npuStage(_ raw: String) -> String {
        guard self == .zh else {
            switch raw.uppercased() {
            case "BACKFILL": return "Bucket Backfill"
            case "BACKFILL-BUILD": return "Backfill Build"
            case "BACKFILL-WAIT": return "Backfill Wait"
            default: return raw
            }
        }
        switch raw.uppercased() {
        case "BACKFILL": return "样例桶补测"
        case "BACKFILL-BUILD": return "样例桶编译"
        case "BACKFILL-WAIT": return "样例桶候票"
        case "WAITING-DIE": return "排队等卡"
        case "CPU-BUILD": return "无锁编译"
        case "CASE-HOST": return "CPU 处理"
        case "PRECISION": return "精度验证"
        case "PROFILER": return "性能采集"
        case "BUILD": return "锁内编译"
        case "FAST-LANE": return "快速冒烟"
        case "STRESS": return "并发压测"
        default: return raw
        }
    }

    func npuDetail(_ raw: String) -> String {
        guard self == .zh else { return raw }
        if raw.hasPrefix("CMake parallel limit: ") {
            return raw.replacingOccurrences(of: "CMake parallel limit: ", with: "CMake 并发上限 ")
        }
        if raw.contains("Baseline perf-bucket backfill") {
            return "B0 自动基线配对 · 样例桶性能补测"
        }
        if raw.contains("Phase A baseline worktree build for perf-bucket backfill") {
            return "B0 样例桶基线无锁编译 (0s 占卡 · 独立 worktree)"
        }
        if raw.contains("Waiting idle Die 0 for perf-bucket backfill") {
            return "B0 样例桶低优候卡 (让行所有算子验证任务)"
        }
        if raw.contains("Phase A lock-free") {
            return "CPU 无锁并行编译"
        }
        if raw.contains("Priority boosted") {
            return "已由调度器置顶优先 (下一顺位获卡)"
        }
        if raw == "Waiting for exclusive NPU die lock" {
            return "正在等待独占 NPU Die 设备锁"
        }
        if raw.contains("Fast-lane queued") {
            return "快速通道 Q0 排队等卡中"
        }
        if raw == "Extended precision & boundary suite" {
            return "正在运行扩展精度与边界用例集"
        }
        if raw == "Hardware kernel profiling (profiler)" {
            return "硬件算子耗时采集 (profiler)"
        }
        if raw.hasPrefix("profiler (") {
            return raw.replacingOccurrences(of: "profiler (", with: "profiler 性能采集中 (")
        }
        if raw == "Compiling KernelC host/kernel package" {
            return "正在编译 KernelC 算子工程"
        }
        if raw.lowercased().contains("fast-lane") {
            return "快速冒烟与公开测例校验"
        }
        if raw == "Multi-run determinism & stress gate" {
            return "多轮确定性与压力门禁测试"
        }
        if raw == "Active NPU validation task" {
            return "正在执行 NPU 服务端验证任务"
        }
        return raw
    }

    func evalStatus(_ raw: String) -> String {
        guard self == .zh else { return raw }
        switch raw {
        case "Pass": return "通过"
        case "Wrong Answer": return "精度未过"
        case "Time Limit Exceeded": return "运行超时"
        case "Runtime Error": return "运行报错"
        case "Compile Error": return "编译失败"
        case "no-submission": return "未提交"
        case "rejected": return "本地拦截"
        case "sync_error": return "同步中断"
        case "skipped": return "已跳过"
        case "Running": return "评测中"
        case "Queued", "Pending": return "排队中"
        default: return raw
        }
    }

    func wsptTag(_ raw: String) -> String {
        let cleaned = raw.replacingOccurrences(of: "^", with: "").trimmingCharacters(in: .whitespaces)
        guard self == .zh else { return cleaned }
        if cleaned == "ESCAPE LOCK" { return "逃逸锁定" }
        if cleaned == "CRITICAL RESTORE" { return "清零待恢复" }
        if cleaned == "FIRST PASS" { return "待首通" }
        if cleaned.hasPrefix("RESTORE ") { return cleaned.replacingOccurrences(of: "RESTORE ", with: "可恢复 ") }
        if cleaned == "HIGH ROI" { return "高收益" }
        if cleaned == "HEADROOM" { return "提分空间" }
        if cleaned == "STEADY" { return "稳定在榜" }
        return cleaned
    }

    func stratumLabel(_ raw: String) -> String {
        switch raw {
        case "PUBLIC": return tr("公开锚点", "Public")
        case "S_SMALL_CORE": return tr("小尺寸核开销", "Small-Core")
        case "M_MID_UB": return tr("中尺寸 UB", "Mid-UB")
        case "L_HBM_TILED": return tr("大尺寸 HBM", "Large-HBM")
        case "X_EXTREME": return tr("异形极端", "Extreme")
        default: return raw
        }
    }

    func geomLabel(_ raw: String) -> String {
        if raw.hasPrefix("G0") { return tr("G0 退化空例", "G0 Degen") }
        if raw.hasPrefix("G1") { return tr("G1 奇素对齐", "G1 OddAlign") }
        if raw.hasPrefix("G2") { return tr("G2 分核切块", "G2 CoreTile") }
        if raw.hasPrefix("G3") { return tr("G3 高维形状", "G3 HighRank") }
        if raw.hasPrefix("G4") { return tr("G4 多类型积", "G4 Dtypes") }
        if raw.hasPrefix("G5") { return tr("G5 边角属性", "G5 EdgeAttr") }
        return raw
    }

    func distLabel(_ raw: String) -> String {
        if raw.hasPrefix("D0") { return tr("D0 标准", "D0 Std") }
        if raw.hasPrefix("D1") { return tr("D1 溢出", "D1 Ovf") }
        if raw.hasPrefix("D2") { return tr("D2 极小", "D2 Sub") }
        if raw.hasPrefix("D3") { return tr("D3 相消", "D3 Cancel") }
        if raw.hasPrefix("D4") { return tr("D4 舍入", "D4 ULP") }
        return raw
    }

    func categoryName(_ raw: String) -> String {
        guard self == .zh else { return raw }
        switch raw {
        case "simd-transpose": return "SIMD 转置向量化"
        case "async-pipeline": return "双缓冲异步流水"
        case "separable-folding": return "可分离维度折叠"
        case "deterministic-sync": return "跨核确定性同步"
        case "vectorization": return "向量化指令改造"
        case "tiling-regime": return "多模态分核切分"
        case "dma-coalescing": return "DMA 合并搬运"
        default: return raw
        }
    }
}
