# Parhelion (`OpsSupervisor.app`) 零卡顿实时流架构升级计划（方案 1）

> 目标：彻底消除 1s 轮询卡顿与高 CPU 负载，将远端 NPU 采集从 **`1,129 ms` → `< 5 ms`**，本地状态刷新从 **`2,035 ms / 963 KB` → `< 5 ms`（热流 `~3 KB` + 冷数据 `304` 增量）**，实现原生 60fps 丝滑体验，同时 100% 复用现有 SSH 密钥、零暴露公网端口。

---

## 1. 现状实测瓶颈（Baseline Profile, 2026-11-22）

| 路径 | 当前耗时 | 单次载荷 | 根因 |
|---|---:|---:|---|
| `opsb status --json` | **`2,035.3 ms`** | **`962.6 KB`** | 每 1s 冷启 Python + 扫 `results/*/*/` + 10 仓库 `git` + `problems (444 KB)` + `submission_queue (199 KB)` 全量送主线程触发 SwiftUI 整树重绘 |
| `opsb npu --json` | **`1,128.6 ms`** | `19.4 KB` | 每 1s 冷启本地 Python + fork `ssh` + 冷启远端 Python + 远端 fork `npu-smi`（单次 `npu-smi` 实测 `349 ms`） |
| 远端可用硬件接口 | **`< 2 ms`** | — | 已验证远端存在 `/opt/driver/driver/lib64/driver/libdevmgr.so`，导出全套 `dcmi_init` / `dcmi_get_card_list` / `dcmi_get_device_utilization_rate` / `dcmi_get_device_hbm_info` / `dcmi_get_device_temperature` / `dcmi_get_device_power_info` C 接口 |

---

## 2. 目标架构（四层解耦）

```text
┌──────────────────────────────────────────────────────────┐
│ soc-a 服务器 (ssh npu)                              │
│  ~/.opsb/npu_stream_probe.py (单进程常驻，零 flock)        │
│   ├─ ctypes 直连 libdevmgr.so (<2ms/次，失败自动降级 npu-smi) │
│   ├─ 内存级 /proc/stat delta + /proc/meminfo (<0.5ms)    │
│   └─ 只读 die_session.json / tickets / cpu_builds        │
│        │  单条持久 SSH stdout NDJSON 行流 (~3KB/s)        │
└────────┼─────────────────────────────────────────────────┘
         ▼
┌──────────────────────────────────────────────────────────┐
│ Mac 常驻本地守护进程 (`opsb daemon`, 127.0.0.1 + Token)    │
│  ├─ NpuStreamManager : 维持单条持久 SSH 流 + 断线指数退避重连│
│  ├─ LocalStateWatcher: 按文件 mtime/size 指纹按需增量重算   │
│  ├─ GET /v1/stream/hot : SSE 推送热流 (NPU + Pipeline +   │
│  │                       cold_etag, ~3KB, <3ms)          │
│  ├─ GET /v1/snapshot   : 冷数据全量快照 + If-None-Match   │
│  │                       304 Not Modified (<1ms)         │
│  └─ POST /v1/actions/* : 暖进程快速执行队列/调度写操作     │
└────────┼─────────────────────────────────────────────────┘
         ▼
┌──────────────────────────────────────────────────────────┐
│ OpsSupervisor.app (SwiftUI 冷热双 Store + 后台线程解码)    │
│  ├─ DaemonClient.swift   : URLSession.bytes SSE + 自动拉起 │
│  ├─ HotTelemetryStore    : 仅 NPU/曲线/Die0/冷却胶囊订阅   │
│  └─ ContestStore (冷数据) : 仅当 cold_etag 变化才拉快照    │
└──────────────────────────────────────────────────────────┘
```

---

## 3. 分模块详细实施清单

### 模块 A：远端常驻 `libdevmgr.so` 流式采集器
- **文件**：`ops-supervisor/cli/opsb_core/probe/npu_stream_probe.py`（复用并保持与 `npu_probe.py` 100% 同构的 `NpuTelemetry` JSON 字段）
- **核心设计**：
  1. **只读绝对安全**：严禁对任何 `.lock` 文件调用 `flock`/`fcntl`（与现有 `npu_probe.py` 铁律一致）。
  2. **`libdevmgr.so` ctypes 直采**：
     - 启动时按顺序预加载 `/opt/driver/driver/lib64/common/libhal.so`（若存在）与 `/opt/driver/driver/lib64/driver/libdevmgr.so`，调用一次 `dcmi_init()` 与 `dcmi_get_card_list()` / `dcmi_get_device_id_in_card()` 缓存 `(card_id, device_id)`。
     - 每周期直接调用 `dcmi_get_device_utilization_rate`（AICore / HBM / Vector）、`dcmi_get_device_hbm_info`（或 `dcmi_get_device_memory_info_v3`）、`dcmi_get_device_temperature`、`dcmi_get_device_power_info`、`dcmi_get_device_chip_info`（芯片名仅启动查一次）。
     - **健壮降级**：任一 DCMI 字段读取异常或与 `npu-smi` 字段结构不匹配时，无缝降级到低频缓存的 `npu-smi`（每 3s 刷新一次，绝不卡死主循环），并在 JSON 中附带 `probe_backend: "dcmi" | "npu-smi"` 与 `sample_ms` 供验收！
  3. **零磁盘写 `/proc/stat`**：在进程内存直接保留上一帧 `prev_stat`，不再每秒写 `~/.opsb/cpu_stat_prev.json`。
  4. **进程/队列采集分级**：
     - 高频（每次 1s）：`die_session.json`、`/tmp/opsb-npu-tickets/*.json`、`~/.opsb/cpu_builds/*.json`、DCMI、`/proc/stat`、`/proc/meminfo`、`/proc/loadavg`；
     - 低频（每 2–3s 缓存）：`ps` 扫描与 `die_ledger.jsonl` 尾部解析（仅当 `die_ledger.jsonl` 的 `mtime/size` 变化时重新解析！）。
  5. **NDJSON 流模式**：支持 `--stream --interval 1.0`（也支持 `--once` 兼容单次调用），每帧输出单行 compact JSON 并 `sys.stdout.flush()`。
  6. **自动部署**：`deploy_npu_probe()` 同时同步部署 `npu_probe.py` 与 `npu_stream_probe.py` 到远端 `~/.opsb/`。

### 模块 B：Mac 侧常驻本地微服务端 (`opsb daemon`)
- **新增目录**：`ops-supervisor/cli/opsb_core/daemon/`
  - `server.py`：`ThreadingHTTPServer`（监听 `127.0.0.1` 随机可用端口或固定优先端口 `18910` 冲突自动顺延），生成随机 32-byte `X-Opsb-Token`，以 `0600` 权限写入 `~/.config/parhelion/daemon.json`（含 `pid`, `port`, `token`, `started_at`, `workspace`, `ssh_host`）。
  - `npu_stream.py`：`NpuStreamManager` 后台线程维护**单条持久 SSH 进程**（`ssh -o BatchMode=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 <host> python3 -u ~/.opsb/npu_stream_probe.py --stream --interval 1.0`），自动部署缺失脚本、逐行解析 NDJSON、合并节流后的 Mac 侧等待任务（`read_mac_npu_waiters`，每 2s 采一次）、支持运行时切换 `ssh_host` 与优雅 `SIGTERM` 清理子进程，断线后 `1s → 2s → 5s` 自动重连。
  - `state_cache.py`：`WorkspaceStateCache` 维护两级缓存：
    - **Cold Fingerprint (`cold_etag`)**：基于 `score-snapshot.json`, `submit-queue.jsonl`, `submit-queue-outcome.jsonl`, `live-board-tbest.json`, `team-ranking.json`, `calibration.json`, `config.json`, 各算子 `.git/HEAD` 与 ref 的 `(mtime_ns, size)` 哈希（实测计算 `< 0.5 ms`）。仅当指纹变化（或距上次刷新超过 `15s`）时在后台单线程重算 `SupervisorSnapshot` 并更新 `cold_etag`。
    - **Hot Frame (`< 3 ms`, `~3 KB`)**：组合内存最新 `npu` 帧 + 轻量化 `pipeline`（`compute_synchronized_pipeline` 复用内存 `outcome_rows` 缓存，仅 `inflight*.json` / `receipt` 变化时刷新）+ `cold_etag` + `daemon_stats`（`dcmi_sample_ms`, `cold_recompute_ms`, `stream_connected`）。
- **HTTP API 契约**（全部校验 `X-Opsb-Token`）：
  - `GET /v1/health` → `{ok, pid, uptime_sec, stream_connected, probe_backend, cold_etag}`
  - `GET /v1/hot?workspace=...&host=...` → 热帧 JSON（`npu` + `pipeline` + `cold_etag`）
  - `GET /v1/stream/hot?workspace=...&host=...&interval=1.0` → **SSE (`text/event-stream`)**，每秒推一条 `event: hot\ndata: {...}\n\n`
  - `GET /v1/snapshot?workspace=...` → 支持 `If-None-Match: <cold_etag>` 返回 **`304 Not Modified`**；变更时返回精简/完整 `SupervisorSnapshot` + `ETag` 头
  - `POST /v1/invalidate` → 强制标记冷缓存失效（写操作完成后调用）
- **CLI 命令接入**：在 `cli_app.py` 注册 `opsb daemon start [--background] | stop | status`。

### 模块 C：SwiftUI 冷热双 Store + 原生 SSE 客户端
- **新增 / 修改文件**（全部位于 `ops-supervisor/macos-app/Sources/OpsSupervisor/`）：
  1. `Core/DaemonClient.swift`（新增）：
     - 读取 `~/.config/parhelion/daemon.json` 并探活 `/v1/health`；未启动则后台拉起 `opsb daemon start --background`（务必 `standardInput = FileHandle.nullDevice` 防 GUI fd 0 异常）。
     - 使用 Swift 原生 `URLSession.shared.bytes(for:)` 消费 `GET /v1/stream/hot` SSE 流；**所有 `JSONDecoder` 解码在 `Task.detached(priority: .utility)` 后台线程完成**，解码成值类型后再轻量 hop 回 `MainActor` 更新状态。
     - 持有 `lastColdETag`，仅当 SSE 帧中 `cold_etag != lastColdETag` 时异步触发 `GET /v1/snapshot`（携带 `If-None-Match`）；若为 `304` 则零解码、零 SwiftUI 刷新！
     - 断线自动重连；若守护进程不可用，无缝回退旧 `ProcessClient` 保证 100% 可用性。
  2. `Features/Contest/HotTelemetryStore.swift`（新增，`@MainActor ObservableObject`）：
     - 独占高频变更状态：`@Published var npu: NpuTelemetry?`、`@Published var telemetryHistory: [TelemetrySample]`、`@Published var pipeline: LivePipelineState?`、`@Published var streamConnected: Bool`、`@Published var probeBackend: String`、`@Published var streamLatencyMs: Double`。
     - 仅高频视图（`NpuMonitorView`、`BtopTelemetryDeckView`、`NpuServerQueueView`、`SchedulerBarView`、`SubmissionQueueView` 顶部活跃卡片）注入并订阅 `HotTelemetryStore`，**彻底隔离 444 KB 榜单表格与 186 条历史队列免受 1s 刷新的无效重绘**！
  3. `Features/Contest/ContestStore.swift`（精简重构）：
     - 移除每秒背靠背 `Timer` 调用 `refreshLocal()` + `refreshNpu()`；改为由 `DaemonClient` SSE 事件驱动：热帧更新 `hotStore`，`cold_etag` 变化才拉 `snapshot` 更新 `ContestStore`！
     - `pollIntervalSec` 仍支持用户在 UI 切换 `0.5s / 1s / 2s / 5s`，切换时实时重连 SSE `interval=` 参数。
     - 保留所有原有功能（Auto-Submit 触发、50/50 满额红胶囊、队列操作、突破库、账号设置、单实例防多开）100% 行为一致。

---

## 4. 并发文件隔离约束（极其重要）

- **本任务独占修改范围**：
  - `ops-supervisor/cli/opsb_core/probe/`
  - `ops-supervisor/cli/opsb_core/daemon/`（新建）
  - `ops-supervisor/cli/opsb_core/services/npu_service.py`
  - `ops-supervisor/cli/opsb_core/cli_app.py`（仅新增 `daemon` 子命令）
  - `ops-supervisor/macos-app/`
  - `ops-supervisor/cli/tests/`（新增单元测试）
- **严禁触碰**：
  - `ops-supervisor/scheduler/*`（当前由 `Local Bench & Calibration Architect` subagent 独占开发中！）
  - `bench/*` 与 `.worktrees/*`（由算子优化 agents 独占！）

---

## 5. 验收与实测指标（Done Definition）

1. **远端 DCMI 实测**：在 `ssh npu` 上运行 `python3 ~/.opsb/npu_stream_probe.py --once` 及 `--stream` 连续 5 帧，确认 `probe_backend == "dcmi"`、单帧采集 `< 5 ms`、AICore/HBM/温度/功耗与 `npu-smi` 一致、零 `.lock` 触碰。
2. **Mac 常驻守护进程实测**：
   - `GET /v1/hot` 延迟 `< 10 ms`，载荷 `< 8 KB`；
   - `GET /v1/snapshot` 命中 `If-None-Match` 返回 `304` 耗时 `< 2 ms`；
   - `GET /v1/stream/hot` SSE 稳定推送 5+ 帧，期间远端无新建 `ssh` / `python3` / `npu-smi` 进程。
3. **App 编译与单实例上线**：
   - 新增 Python 单测全部通过；
   - `bash ops-supervisor/macos-app/scripts/bundle_app.sh` 零错误编出 Release `.app`；
   - 干净重启单实例 `OpsSupervisor.app`，验证界面流式连接正常、CPU 占用显著下降、50/50 红胶囊 / 同步中断 / 队列操作全部正常；
   - 仅 `git add` 本任务范围文件并在 `ops-supervisor` 提交，向 `@coordinator` 汇报前后实测对比数据。
