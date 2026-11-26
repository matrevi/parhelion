# 多 NPU 界面与设备池：设计与改动清单（进行中）

用户要求（2026-11-26）：
- 在基本不改原布局的前提下展示多颗 NPU（现在 2 颗：`npu`、`npu2`，以后可能 3、4 乃至 N 颗）。
- 删掉「显存与内存」面板。
- 设置页「NPU SSH 连接」也要支持多台主机。

## 已有的基础（已提交）
- 路由：`cli/opsb_core/device_hosts.py`。`npu_ssh_hosts` 列出设备主机，每题按清单顺序取模固定到一台。`opsb test` 在 `queue_cmd.run_test` 里选主机。
- 估分器只在同一主机内配对比较（`estimator._case_bench_groups` 的键是 `<pairing_key>@<host>`）。
- 部署配置 `~/.parhelion/config.json` 已设 `npu_ssh_hosts: ["npu", "npu2"]`，主主机仍是 `npu_ssh_host: "npu"`。

## 设计

### 一、守护进程：每台主机一条遥测流
- `cli/opsb_core/daemon/server.py` 的 `OpsbDaemonRuntime.__init__` 现在只有一个 `self.stream_mgr = NpuStreamManager(host=self.ssh_host, ...)`（约第 182 行）。
- 新增 `self.extra_streams: dict[str, NpuStreamManager]`，按 `device_hosts.device_hosts(cfg)` 去掉主主机后建立。
- 在 `consume_queue` 的每个 tick 里与配置对账：启动新加的主机，停掉移除的主机。这样以后改设备池不用重启。
- `start` 与 `stop` 同步管理这些流（约第 561、591 行）。
- 热帧（`/v1/hot` 与 `/v1/stream/hot`，约第 363–425 行）在 `build_hot_frame` 之后追加 `frame["npu_hosts"]`：
  - 每个元素为 `{host, primary, stream_connected, probe_backend, dcmi_sample_ms, npu}`；
  - 主主机条目不带 `npu`（客户端用顶层的 `npu`），其余主机带 `mgr.snapshot(compact_history=not first, history_tail=5)`。
  - `/v1/health` 也列出主机。
- `NpuStreamManager`（`daemon/npu_stream.py`）本身按主机工作，不用改；它会向主机部署探针 `~/.opsb/npu_stream_probe.py`。
- **限制**：`daemon.server`、`daemon.npu_stream`、`daemon.state_cache` 都不在热重载白名单里（`daemon/hot_reload.py`）。新代码要重启一次守护进程才生效，而重启会停掉所有 worker。**重启前先问用户**。

### 二、命令行：设备池
- `admin_cmd.run_ssh` 与 `cli_app.py` 约第 427 行（`opsb ssh list|select|save`）。
- 新增 `opsb ssh pool [--add A] [--remove A] [--set a,b]`，写 `npu_ssh_hosts`，主主机始终在列表里。
- `ssh list` 的输出加上 `pool` 字段；`ssh_service.parse_ssh_hosts` 给每台主机加 `in_pool`。

### 三、App：数据层
- `Domain/NpuModels.swift`：
  - `HotStreamFrame` 加可选的 `npuHosts: [DeviceHostFrame]?`（键 `npu_hosts`，兼容旧守护进程）；
  - `NpuTaskItem` 加 `var host: String?`，由客户端填入，用于泳道徽标和 id 去重。
- `Features/Contest/HotTelemetryStore.swift`：
  - 加 `hosts: [String]`（主主机在前）、`hostNpu: [String: NpuTelemetry]`、`hostHistory: [String: [TelemetrySample]]`（每台各保留 120 个点）、`hostStream`、`selectedHost: String?`；
  - `applyHotFrame` 同时更新主主机与其余主机。

### 四、App：遥测面板 `Features/NpuMonitor/BtopTelemetryDeckView.swift`
- 保留面板 1（NPU 波形与 10 分钟占空）和面板 2（主机 CPU 与核心矩阵），只把数据源换成「选中的主机」。
- 删掉面板 3 `memoryHierarchyPanel`。
- 原位置（宽 286）换成「NPU 设备 · N」名册：每台一行，约 44 像素高，4 台以内不滚动，更多时用 ScrollView。每行：
  - 第一行：状态点、主机别名、温度、功耗、AIVec%；
  - 第二行：Die 锁（占用 · 题 · 时长 / 空闲）、等待 · 编译数、迷你占空条或迷你波形。
- 点击一行即选中，面板 1、2 和顶栏随之切换；选中行描边高亮。
- 顶栏显示选中主机的信息，再加一个汇总胶囊，如「2 设备 · 1 占用」。

### 五、App：三车道 `NpuServerQueueView.swift` 与 `NpuLaneModel.swift`
- 现在只吃 `liveNpu.tasks`（主主机）。改为合并所有主机的任务，每个任务带上 `host`。
- `NpuLaneModel` 里的 `pidOwner` 改用 `host#pid` 作键，避免不同主机的 pid 冲突。
- 卡片显示主机徽标（只有一台主机时不显示）。
- 「DIE 0 硬件独占执行区」的标题在多主机时改为通用的「NPU 硬件独占执行区」。

### 六、App：设置页 `Features/Settings/SSHSettingsTab.swift`
- 主机列表每行的「选用」改为两个控件：
  - 「设备池」开关（复选框），调用 `opsb ssh pool --add/--remove`；
  - 「设为主」，调用原来的 `ssh select`。
- 「启用中」徽标改为「主」，池内主机另显示「设备池」徽标。
- 右侧顶部显示「设备池：npu、npu2」。
- 相关代码：`store.selectSSHHost`、`store.repository.saveSSHHost`、`store.sshHosts`、`store.activeSSHHost`（ContestStore 与 repository 里）。

## 验收
- 定向测试：守护进程帧里有 `npu_hosts`；`ssh pool` 增删；`parse_ssh_hosts` 带 `in_pool`。
- `swift build` 与 `scripts/bundle_app.sh --install`；守护进程重启（先征得用户同意）后，界面上能看到两台设备，三车道能看到 `npu2` 的任务。
- 全量测试只在打版本号前跑一次。
