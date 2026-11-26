# Parhelion 发布说明

> 设计：[00-OVERVIEW.md](00-OVERVIEW.md) · 详细设计：[10-DETAILED-INDEX.md](10-DETAILED-INDEX.md)

## 4.0.4 — 修复 NPU 遥测断流

4.0.1–4.0.3 之后 `opsb npu status` 正常，但 daemon 的常驻遥测流一直连不上，界面显示 NPU 离线（`"error": "SSH stream closed (rc=None)"`）。两个缺陷叠加：

- **远端探针版本标记过期。** daemon 用 `OPSB_STREAM_PROBE_V5` 标记判断远端副本是否可用，旧副本没有该标记，远端命令直接 `exit 42`。自动重部署分支因此不可达，因为它依赖退出码 42：读线程先看到 stdout EOF，主线程随即 `poll()` 拿到 `None`，42 被丢掉。现在改为先 `wait(timeout=2.0)` 收割真实退出码，超时才回退 `poll()`。
- **设备事实没有随命令下发。** 探针是独立副本，读不到本机 `~/.parhelion/config.json`，`driver_root`、`dcmi_lib`、`driver_libs` 全为空，`libdcmi` 预载失败后退化成 `npu-smi`，SoC 变成占位值。现在 `device_env.device_env_exports()` 把所有标量与结构化设备设置渲染成 `OPSB_DEVICE_<KEY>` 前缀，随 ssh 命令下发；字典与列表走 JSON，探针侧的独立解析器同步支持 JSON 覆盖。仓库仍然零厂商字面量，设备值只存在于 `config.json` 与运行时环境。

修复后 `probe_backend=dcmi`、`soc_name` 正确、`stream_connected=true`，采样约 40ms，热帧构建约 5ms。新增 `cli/tests/test_device_env_exports.py` 覆盖结构化设置的导出与探针侧解析。

## 4.0.3 — 修复清理误伤与索引校正

### 修复
- **厂商名清理的误伤。** 那次替换把 "cannot" 里的子串也换掉了，留下 28 处 `sdkot`，分布在 18 个文件的报错文本、注释、文档和测试里，现已还原。同一次清理还把 `fast_profiler.py` 里 profiler 的运行时追踪参数改成了 profiler 不认的名字，并把程序名写死为占位符。现在两者都和 `npu-test.py` 一样，从 `OPSB_DEVICE_PROFILER_BIN` 和 `OPSB_DEVICE_PROFILER_ACL_FLAG` 读取，也可以用新参数 `--profiler-bin` / `--acl-flag` 指定。
- **`opsb doctor clean --agents` 不再回收 `error` 状态 worker 的沙箱。** 这类席位还能通过显式 followup 续跑，Codex 续跑还要用沙箱里的 `codex_home`。现在它们默认只列出（显示为 `agent error (resumable)`），加 `--include-errored` 才会回收。
- **`AGENTS.md` 的冲刺启动段落与 4.0 默认值对齐。** 默认每题一个 solo 席位，`dual` / `full` 需要显式开启；没有校准席位时，官方回执投给提交该 SHA 的 worker。

### 仓库认知索引
- `services/test_runs.py` 是 `parhelion_test` / `parhelion_test_wait` 背后的登记表，但因为文件名以 `test_` 开头被归入了观察层。现在通过项目 scope 规则把它纳入索引，并补写了条目。
- 修正了 4 条描述与源码不符的条目：
  - `status_cmd`：`--compact` 只去掉缩进，2KB 上限在 MCP 那边；
  - `__init__`：`bundle_app.sh` 的版本号写死在脚本里，需要手工同步；
  - `bundle_app.sh`：默认只在仓库外构建，加 `--install` 才安装；
  - `fast_profiler`：目前是手动工具，平台代码不调用。
- 索引共 248 条；`check` 0 个问题，`guide` 为 aligned / complete。

## 4.0.2 — 重建仓库认知索引

4.0.0 主动删除了旧的 AOCI 索引以便重建。本次按 AOCI 的正式流程从零建立仓库级认知层：`aoci init` → `aoci scan`（294 个受管文件，其中 247 个需要条目）→ 分批创作 → 对齐。

- 索引共 **247 条**条目，覆盖 `Sources/`（Swift 前端）、`cli/opsb_core/`（CLI、daemon、MCP、驱动）、`scheduler/`、`runtime/`、`docs/`、`templates/`、`scripts/` 与根文件。
- 每条按紧凑标签 `A+B+C+E` 与 F/R/A/S 语义书写；S 只记不可推断且能防错的约束（如：inflight 文件是账号提交的唯一锁、`cleanup_stopped` 的判定谓词被 doctor 复用、stdout 只留给 JSON-RPC 协议流）。
- 测试文件（`cli/tests/**`，44 个）保持在观察层，不单独建条目，与既有 scope 策略一致。
- 终局验证：`aoci verify` 结构与治理均对齐；`aoci check` ok、0 findings；`aoci guide` 为 `stage=aligned, complete=true`。
- `aoci init` 会重写 `AGENTS.md` 的托管块并留下 `*.backup.*` 副本，后者已加入 `.gitignore`。

## 4.0.1 — 卫生修复

4.0 的清理只覆盖了"测试污染的工作区目录"和"远端死票"，两条平台自己写入的路径因此无界增长：每个 worker 的 `workspaces/<id>/agents/<agent_id>/`（各带一份 ~100MB 的 `codex_home`，实测 175 个共 1.0GB）与驱动会话流水（125MB，其中 90% 来自单日）。本次补齐回收，并修正一处权限漂移。

### `opsb doctor clean`

- `--agents`：回收已结束 agent 的沙箱。判定与 `AgentBusStore.cleanup_stopped` 完全一致（`stopped`/`error` 且 `pid`、`pgid` 均已清空），数据库里无记录的孤儿沙箱一并回收。登记工作区的**状态目录本身仍不移动**，只回收其 `agents/` 下已被证明结束的沙箱；数据库不可读时只列出、不移动。`--apply` 移入 `trash/<UTC>/agents/<workspace>/<agent_id>`。
- `--logs DAYS`（默认 14 天）与 `--logs-max-mb MB`（默认 100，`0` 关闭）：驱动会话流水的时间窗 + 总量封顶，按 mtime 从新到旧装满预算，装不下的更旧文件回收。这类流水属平台诊断 telemetry，与远端死票同类，`--apply` 直接删除；用户数据仍只移动。
- `--purge-trash`：唯一会删除 trash 的操作，需显式指定，用于真正释放前两步腾出的空间。
- 默认仍是只列出不动手；`--json` 在带对应开关时追加 `agents` / `logs` / `trash_purge`。

### 配置权限

- `ConfigRepository.save()` 在原子替换前 `chmod 0600`：迁移表承诺 `config.json` 为 0600，但运行期保存会掉回 umask。

### 实测回收

- `workspaces/` 1.0GB → 5.2MB，`logs/` 125MB → 100MB，`~/.parhelion` 约 1.2GB → 144MB。

## 4.0.0

### 测试平台
- 每题一个平台持有的版本化套件 `suite.json`；用例是最小单元，桶只是标签。
- 三个级别 `smoke` / `standard` / `final`。standard 运行可以作为提交网关的正式证据。
- 默认对全部官方例计时：丢弃 warmup 样本，报告热态最小值和中位数；套件可以用 `timing_scope=perf` 缩小计时范围。
- 每次运行生成不超过 2KB 的 digest，包括：
  - 逐例判定和失败摘要；
  - 计时结果，以及本次运行相对该 SHA 最好成绩的漂移和慢档标记；
  - 公开例效率，分为扣除计时开销后的值和原始值；
  - 相对最佳官方 SHA 的几何平均提速。
- 计时开销校准：用官方对同一 SHA 的逐例时间，标定本地事件计时的附加开销，并缓存到 `results/.timing-offset.json`。
- worker 不能再传 profile / experiment / timing / repeats / runner / bucket。

### 设备主机
- 常驻设备代理按 WSPT 顺序串行执行作业并独占设备锁；TLE 后释放锁，把剩余用例续排；代理启动时清理死票。代理不可用时回退到 3.6.2 的车道逻辑。
- 探针把代理识别为持锁者，探针版本标记升到 `V5`，设备主机会自动重新部署。

### 控制面与 MCP
- 新工具 `parhelion_test`、`parhelion_test_wait`、`parhelion_test_cancel`。`parhelion_test_plan` 保留为兼容别名。
- 测试运行登记表放在 `state/test_runs/`：相同请求会加入正在进行的运行，或复用 30 分钟内的结果；结束后作为 `test_result` 消息投递到请求者的收件箱。
- stdio MCP 服务改为多线程分发，回复可以乱序返回，长调用不再挡住其他请求。
- 输出上限：`status` 默认不超过 2KB；`kb_query` 默认返回 3 条、每条摘要不超过 600 字符；`bench_diagnose` 精简。传错 SHA 时，报错里列出最近 5 个提交。

### 编排
- 冲刺默认每题 1 个 worker（`mode=solo`），`dual` 和 `full` 需要显式开启。
- 供应商故障熔断：余额、配额、401/402/429 错误会暂停该驱动，只发一次告警，并停掉同驱动的 worker；管理员显式派发时自动解除暂停。
- 压缩阈值随模型窗口自适应，取窗口的 30%，限制在 12 万到 40 万 token（`agents.compact_tokens` 可以覆盖）；不再要求先有阶段结果。
- 不超过 2KB 的状态卡用于 brief 和压缩检查点；worker brief 不超过 1500 字节。
- squad 支持按 agent_id、角色名或 `all` 寻址；报错时附上队友列表。
- 不再唤醒 error / stopped / 已暂停驱动的 worker。
- 修复：`allow_claude_workers` 配置项从未被读取。

### 卫生
- 测试使用临时的 `PARHELION_HOME`，跑前跑后真实目录数不变（293 → 293）。
- 网关判定只在变化时写日志；`feedback-log.jsonl` 超过 8MB 自动轮转。
- 工作树路径的 `workspace_id` 归并到所属比赛工作区，不再为每个工作树新建一个数据目录。
- 新命令 `opsb doctor clean [--remote] [--apply] [--json]`，把测试残留移到 `~/.parhelion/trash/`，并清理设备主机上的死票。

## 迁移
1. 可选：`opsb bench suite migrate <Problem>`，把旧的 `case-profile.json` 显式转成 `suite.json`。不转也能运行，旧 profile 会在内存里自动转换。
2. `opsb doctor clean` 先看一遍列表，再用 `--apply` 清理。
3. 已安装的 3.x 应用在重新安装之前，仍然使用旧的运行时。新的设备代理会在第一次 4.0 测试时自动部署到设备主机。

## 端到端实测（2026-11-25，真机）

| 用例 | 结果 |
|---|---|
| E1 全量测试 | 378 通过 / 18 跳过；测试前后 workspaces 目录数不变；Swift 编译通过 |
| E3 smoke（RevINNorm best） | 端到端 22s，代理已启用，等待 0.001s，持锁 3.9s |
| E4 standard | 15/15 通过，端到端 31–35s，持锁 2.9–3.9s，formal 证据成立 |
| E5 复用 | 相同请求 0.4s 返回 `reused` |
| E6 三题并发 | 端到端 31/38/70s，排队 ≤3.3s，串行执行，零残留票据 |
| E7 MCP 全流程 | `parhelion_test` 0.02s 返回；运行期间 `status` 1.07s 返回（1.8KB）；wait 33s 得到 15/15；网关预览 0.3s |
| E8 清理 | 本地 290 个残留移入回收目录；远端死票 0（代理启动时已清理 23 个） |

## 已知限制
- **事件计时带有主机侧启动延迟。** 同一 SHA，小内核在不同运行之间、甚至同一次运行的不同用例之间，读数会在约 5µs 和约 25µs 之间跳档；10µs 以上的内核与官方相差约 2%。4.0 的应对办法：比较时使用该 SHA 的最好成绩，扣除标定出的开销（实测约 2.4µs），并标记慢档运行。真正的修复是 profiler 任务时长（见 4.1）。
- 编排流程只用假 adapter 测过，没有调用真实模型（DeepSeek 余额已耗尽）。ACP / Codex 的压缩路径也没有实机验证。
- 加入同一运行的第二个 worker，能从调用返回值里拿到结果，但收件箱只投递给最初提交的 worker。

## 4.1 路线
1. 把 profiler 任务时长接入 reference 引擎，替换事件计时（最高优先级）。
2. A/B 交替配对，以及多轮轮转计时（把各用例的样本分散到不同时刻）。
3. 跨题合并设备批次。
4. Swift UI 显示测试登记表、digest 和驱动暂停状态。
