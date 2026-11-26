# Parhelion 4.1 + 4.2 实施总计划

主会话负责本计划、审查与合并；每个子计划由一个 Opus 子代理在独立 git 工作树里实现（Sonnet 子代理在 P01 上两次被安全过滤中止，2026-11-26 改用 Opus），提交到自己的分支，主会话审查 diff 后合入 `main`，再跑全量测试、提交并用 `aoci_maintain` 维护索引。

需求来源：

- `docs/v4/RELEASE-NOTES.md`：「4.1 路线」（1–4）与「2026-11-26 冲刺暴露的问题 · 4.1 待办」（5–20，编号接续路线）。
- `docs/v4.2/00-PLAN.md`：工作包 A–K、§3 目标指标、§5 回放验收。
- 依据材料：`docs/v4/RUN-LOG-2026-11-26.md`、`docs/v4.2/research/token-economy.md`、`docs/v4.2/research/compaction-memory.md`（账本字段见其 §5.3）。
- 补充调研：`docs/v4.2/research/agent-optimization-practice.md`，内容是 Kaggle 实践、算子优化系统、记忆组织与社区 skill。其 R1–R12 已并入相关子计划，R1–R3 单列为 P17。

## 1. 子计划索引与落地顺序

落地顺序即合入 `main` 的顺序。允许提前开发（并行），但合入严格按此顺序；后合的分支由主会话合并时解决冲突。

| 序 | 子计划 | 覆盖条目 | 主要文件 | 依赖 |
|---|---|---|---|---|
| 1 | [P01 判题对齐与回执计分](P01-judge-alignment.md) | 4.1 #5 #7 #8 | `scheduler/estimator.py` `scheduler/case_bench.py` `cli/opsb_core/services/{judge_score_fetch,submit_runner,sync_reconciler}.py` 新模块 `scheduler/case_map.py` `scheduler/receipts.py` | — |
| 2 | [P02 WA 多 seed 复现](P02-wa-reproducers.md) | 4.1 #6 | `scheduler/case_feedback.py` `scheduler/suite.py` 新模块 `scheduler/wa_repro.py` | P01（`case_results`、用例映射） |
| 3 | [P03 测量：缓存命中、工具字节、回放基准](P03-metering-replay.md) | 4.2 A | 新包 `cli/opsb_core/metrics/` `cli/opsb_core/mcp/server.py` `cli/opsb_core/agents/manager.py`(hot mesh 字段) Swift `AgentMesh*` 新包 `cli/opsb_core/replay/` | — |
| 4 | [P04 变化驱动的等待](P04-change-driven-wait.md) | 4.2 B | 新模块 `cli/opsb_core/agents/event_hub.py` `mcp/{schemas,tools,handlers_agents,handlers_bench}.py` `daemon/agent_routes.py` `agents/manager.py`(wait_events) `services/test_runs.py` | — |
| 5 | [P05 账本内核、自动写入、交接校验、checkpoint 修正](P05-ledger-core.md) | 4.2 C #1 #2 #6 #8 | 新包 `cli/opsb_core/ledger/` `agents/{manager,adapters}.py` `services/test_runs.py` | P03（回放）、P04 |
| 6 | [P06 账本工具、代码拼装种子、压缩时机、状态卡逐例差距](P06-ledger-seed.md) | 4.2 C #3 #4 #5 #7；4.1 #13 #14 | `ledger/` `mcp/*` `services/state_card.py` `agents/manager.py` `replay/` | P05 |
| 7 | [P07 门禁与队列一致性、逃逸锁 MCP](P07-gate-queue.md) | 4.1 #9 #10 #11 #12 | `services/queue_service.py` `scheduler/cli_bridge.py` `scheduler/case_feedback.py` `mcp/*` Swift 队列卡 | P01、P02 |
| 8 | [P08 worker 启动包、DSH 插件按需挂载](P08-startup-pack.md) | 4.2 G；4.1 #15 | `drivers/brief.py` `drivers/dsh_parhelion_mcp.mjs` `drivers/dsh_driver.py` `mcp/*` | P06 |
| 9 | [P09 设备主机与守护进程](P09-host-daemon.md) | 4.1 #16 #17 #18 #19 #20 | `runtime/npu_runtime/case_batch.py` `commands/doctor_cmd.py` `agents/bus_store.py` `daemon/server.py` Swift 菜单 | — |
| 10 | [P10 profiler 任务时长与 A/B 交替配对](P10-profiler-ab.md) | 4.1 路线 #1 #2 | `runtime/npu_runtime/*` `runtime/npu-test.py` `scheduler/case_bench.py` | P01 |
| 11 | [P11 跨题设备批次与界面展示](P11-batch-ui.md) | 4.1 路线 #3 #4 | `runtime/npu_runtime/device_broker.py` `services/test_runs.py` Swift | P10 |
| 12 | [P12 平台视图 parhelion_report](P12-report-views.md) | 4.2 D | 新模块 `cli/opsb_core/mcp/handlers_report.py` `mcp/{schemas,tools}.py` | P01、P06 |
| 13 | [P13 统一输出上限 bound_output](P13-bound-output.md) | 4.2 E | `mcp/server.py` 新模块 `cli/opsb_core/mcp/bound.py` | P12 |
| 14 | [P14 同伴消息协议](P14-peer-protocol.md) | 4.2 F | `agents/manager.py`(squad) `mcp/handlers_agents.py` `ledger/` | P06 |
| 15 | [P15 worker 边界](P15-worker-boundary.md) | 4.2 K | `agents/adapters.py` `worker_access.py` `drivers/*` | P12（board 视图） |
| 15a | [P17 本地验证可信度、逐例校准表、噪声感知判定](P17-verify-calibrate.md) | 调研 R1 R2 R3 | `runtime/npu_runtime/*` `scheduler/{gate,case_bench,suite}.py` 新模块 | P01、P02、P07 |
| 16 | P16 H/I/J 决策 | 4.2 H I J | 由主会话根据 P03 的测量与回放结果决定，届时补写子计划；调研 R10（按代码相似度注入知识、按驱动 A/B）并入 J | P03–P15 |

### 1.1 执行分组

子计划按代码区域分成 4 组。每组由一个子代理按组内顺序依次完成，共享已经建立的代码上下文。

每完成一个子计划，子代理就交付报告并停下。主会话按 §3 合入后，再通知它从最新的 `main` 开新分支，继续做下一个子计划。合入仍按 §1 的顺序。

| 组 | 子计划（按序） | 区域 |
|---|---|---|
| 1 判题与门禁 | P01 → P02 → P07 → P17 | 回执、用例映射、WA 复现、门禁与队列、逃逸锁 MCP、验证加固与校准 |
| 2 上下文经济内核 | P03 → P04 → P05 → P06 | 计量与回放、等待、账本、压缩种子 |
| 3 设备与守护进程 | P09 → P10 → P11 | 用例缓存、总线序号、daemon 热加载、编译检查、profiler 计时、A/B 配对、跨题批次、界面 |
| 4 工具面与边界 | P12 → P13 → P14 → P08 → P15 | 平台视图、输出上限、同伴协议、启动包、worker 边界（P06 合入后启动） |

并发：同一时刻最多 3 个子代理。设备作业由设备代理按 WSPT 串行执行并独占设备锁，所以多组同时上真机不会互相干扰计时。

## 2. 子代理共同守则

每个子代理开工前完整读本文件与自己的子计划，并按需读子计划列出的资料。子计划写的是目标、证据、接口与验收；实现细节由你按现有代码风格决定，遇到子计划与源码事实冲突时以源码为准，并在报告里说明。

### 2.1 工作方式
- 只在分配给你的工作树里改文件。工具创建的工作树基于 `origin/main`，落后本地 `main`；开工第一步执行 `git checkout -b impl/Pxx-<slug> main`（分支名见子计划首行），并确认 `git log -1` 不早于本文件所在提交。只提交到该分支：不推送、不合入 `main`、不改主检出目录。
- 用 `aoci_search` / `aoci_get_entries`（若可用）定位文件；**不要**调用 `aoci_maintain`、`aoci_update_entry`，也不要改 `aoci*.txt`，索引由主会话维护。
- 新模块不超过 350 行（`docs/v3/REFACTOR_MODULAR_LAYOUT.md`）。已超长的文件（`estimator.py`、`cli_bridge.py`、`manager.py` 等）尽量只加薄接入点，新逻辑放新模块。
- 提交信息用英文祈使句，正文说明原因；末尾加 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`。可以分多次提交。

### 2.2 不可违反的约束
- 不改自动提交开关（`auto_consumer_enabled`），不强制提交，不调用 `queue approve` / `force`，不向判题站提交任何东西。额度不得进入任何门禁阈值或暂缓理由。
- 不重启、不停止 opsb 守护进程（PID 见 `~/.parhelion/daemon.json`），不派发或停止 worker，不清理总线。
- `~/.parhelion/logs/sessions/mesh-dee-*.jsonl`、`~/.dsh/sessions/**`、`/Users/winbeau/Competitions/mat-chem-sim-pred/**/.worktrees/**` 及其分支是 4.2 回放数据：只读，不移动、不删除、不压缩。
- 真实比赛数据（`/Users/winbeau/Competitions/mat-chem-sim-pred/results/**`、`~/.parhelion/**`）默认只读。子计划明确允许时才可经平台命令写入（例如在真机上跑 `opsb test` 会照常写 bench-runs）。分析产物放到工作树内的 `docs/plan-4.1-4.2/reports/` 或你的临时目录。
- 仓库里不得出现真实设备值（芯片型号、驱动路径、库名等）；设备值只存在于 `config.json` 的 `device` 段与运行时环境（见 `cli/opsb_core/device_env.py`）。
- MCP：stdout 只留给 JSON-RPC；新工具必须同时登记 schema（`mcp/schemas.py`）、handler（`mcp/tools.py`）与允许角色，参数名两侧逐字一致。worker 不能经 MCP 绕过门禁。
- `scheduler/cli_bridge.py` 的 JSON 是 CLI 与 GUI 的共享契约：改字段时 Swift 解码同步改，并保持宽容解码（`decodeIfPresent`）。
- 判题回执去重按 `submission_id`；重放同一批回执不得重复触发交接、扣额或账本写入。

### 2.3 测试与验收
- 测试一律使用临时 `PARHELION_HOME`（根 `conftest.py` 已处理），跑前跑后 `~/.parhelion/workspaces/` 目录数不变。不要写依赖真实比赛数据的单测；需要真实数据形状时，裁剪成最小 fixture 放进 `runtime/tests/fixtures/` 或 `cli/tests/fixtures/`，去掉账号名、cookie、用户 ID。
- 提交前必须全绿：
  ```bash
  ~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider
  ```
  ```bash
  PYTHONPATH=cli:. ~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider cli/tests runtime/tests
  ```
  基线（2026-11-26）：前者 424 passed / 18 skipped，后者 253 passed。改了 Swift 还要 `swift build` 通过。
- 子计划里标「真机验收」的步骤，在单测全绿后执行，并把命令、耗时与输出摘要写进报告。

### 2.4 交付报告（子代理最终回复）
1. 分支名与提交列表（`git log --oneline main..HEAD`）。
2. 每个条目的完成情况：已完成 / 部分完成（缺什么）/ 未做（为什么）。
3. 两条全量测试命令的末行输出；Swift 是否构建。
4. 真机或真实数据验证的结果（如有）。
5. NOTE：发现的非显然约束或与子计划不一致的源码事实（主会话会写进 aoci 条目）。
6. 已知风险与建议的后续工作。

## 3. 主会话合并流程（每个子计划）
1. 读子代理报告，`git diff main...impl/Pxx-*` 审查；不合格则续派（SendMessage）修正，不原样重发。
2. `git merge --no-ff impl/Pxx-*`，解决冲突。
3. 跑 §2.3 的两条全量测试（及 `swift build`），全绿后提交合并。
4. `aoci_maintain`，按返回的批次创作条目并 `aoci_update_entry`，直至 aligned；索引变更单独提交。
5. 在下表记录结果，然后 `git push origin main`，让本地 `main` 与 `origin/main` 保持一致（工具按 `origin/main` 切新工作树）。

## 4. 进度

| 子计划 | 分支 | 状态 | 合入提交 | 备注 |
|---|---|---|---|---|
| P01 | impl/P01-judge-alignment | 已合入 | a4a3eda | 留一回测 MAE 2.75→1.32；按位置配对的身份是对的，FFT 05 等是不同工作量（标 judge_mapped=false）；回填 19 行 |
| P02 | impl/P02-wa-reproducers | 已合入 | 见 git log | 真机复现 Quant a2717b5 的 WA（精确舍入平局变体 24/8320 错），榜上 SHA 全过；每次 WA 套件升版会使旧本地证据失配，修正并入 P07 |
| P03 | impl/P03-metering-replay | 已合入 | 见 git log | 缓存命中已达 99.3–99.6%，成本杠杆在上下文大小；回放基线 current 覆盖 0.642 [0.568,0.716]；主控轮询实测 136/h（计划写约 560/h，日志无法复原） |
| P04 | impl/P04-change-driven-wait | 进行中 | | |
| P05 | impl/P05-ledger-core | 待开始 | | |
| P06 | impl/P06-ledger-seed | 待开始 | | |
| P07 | impl/P07-gate-queue | 已完成，待按序合入；P07b 删除收尾预留进行中 | | |
| P08 | impl/P08-startup-pack | 待开始 | | |
| P09 | impl/P09-host-daemon | 已完成，待 P06 后按序合入 | | 设备主机 SSH 不可达，#16 远端检查与 #20 真机验收待补 |
| P10 | impl/P10-profiler-ab | 已完成，待按序合入（叠在 P09 上） | | profiler 计时 CV 61%→0.9%；Edge 基线复测待真机 |
| P11 | impl/P11-batch-ui | 待开始 | | |
| P12 | impl/P12-report-views | 待开始 | | |
| P13 | impl/P13-bound-output | 待开始 | | |
| P14 | impl/P14-peer-protocol | 待开始 | | |
| P15 | impl/P15-worker-boundary | 待开始 | | |
| P17 | impl/P17-v2 | 待重派 | | 首次在写 diff 规则筛查时被安全过滤中止；按用户决定去掉 diff 筛查后重试 |
| P16 | — | 待测量 | | |
