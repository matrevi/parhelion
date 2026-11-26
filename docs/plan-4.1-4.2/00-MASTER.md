# Parhelion 4.1 + 4.2 实施总计划

主会话负责本计划、审查与合并；每个子计划由一个子代理在独立 git 工作树里实现。4.1.0 发布后，4.2 剩余的包（P08、P12–P16、P18）改由 DSH 在工作树里开原生子代理实现，执行计划见 [20-REMAINING-4.2.md](20-REMAINING-4.2.md)；下文关于 Sonnet/Opus 的约定只适用于此前由主会话派出的子代理。模型优先用 Sonnet：Opus 额度消耗大，只在某个包的 Sonnet 代理被安全过滤中止后，才对该包改用 Opus。简报不写设备厂商相关字眼；会话日志只做聚合统计，不把原文读进上下文，提交到自己的分支，主会话审查 diff 后合入 `main`，再跑全量测试、提交并用 `aoci_maintain` 维护索引。

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
| 16 | P16 H/I/J 测量与决策 | 4.2 H I J | DSH 先按 20-REMAINING §3.7 出测量报告，主会话据此决定并补写子计划；调研 R10（按代码相似度注入知识、按驱动 A/B）并入 J | P12、P13 |
| 17 | P18 收尾小修 | 4.1 遗留 | 可用额度显示不扣收尾预留、设备代理测试加固、（可选）用例缓存目录可配置，见 20-REMAINING §3.6 | — |

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
- 提交信息用英文祈使句，正文说明原因；末尾加 `Co-Authored-By: Claude <所用模型> <noreply@anthropic.com>`，例如 `Claude Sonnet 5.5`。可以分多次提交。

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
  基线（main，P12–P15、P08、P18 合入后）：前者 785 passed / 19 skipped，后者 538 passed（4.1.0 时为 686/19 与 442）。改了 Swift 还要 `swift build` 通过。
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

无人值守续跑的当前状态与下一步，见 [RUNBOOK.md](RUNBOOK.md)。截至 P17 的各包已随 **4.1.0**（tag `v4.1.0`）发布。

| 子计划 | 分支 | 状态 | 合入提交 | 备注 |
|---|---|---|---|---|
| P01 | impl/P01-judge-alignment | 已合入 | a4a3eda | 留一回测 MAE 2.75→1.32；按位置配对的身份是对的，FFT 05 等是不同工作量（标 judge_mapped=false）；回填 19 行 |
| P02 | impl/P02-wa-reproducers | 已合入 | 见 git log | 真机复现 Quant a2717b5 的 WA（精确舍入平局变体 24/8320 错），榜上 SHA 全过；每次 WA 套件升版会使旧本地证据失配，修正并入 P07 |
| P03 | impl/P03-metering-replay | 已合入 | 见 git log | 缓存命中已达 99.3–99.6%，成本杠杆在上下文大小；回放基线 current 覆盖 0.642 [0.568,0.716]；主控轮询实测 136/h（计划写约 560/h，日志无法复原） |
| P04 | impl/P04-change-driven-wait | 已合入 | 见 git log | parhelion_wait 事件到返回 p50 262ms；主控投影 91–67 次/h（实测基线 136/h），单次返回从中位 6.7KB 降到约 200B；另修复两条日期相关测试 |
| P05 | impl/P05-ledger-core | 已合入 | 见 git log | 回放 ledger_core 0.649 对 current 0.642，配对差 +0.007 [−0.015,+0.032] 不显著；只有平台写入时账本内容太少 |
| P06 | impl/P06-ledger-seed | 已合入 | a72ac6d | DSH 会话写完；审查后修近似重复比较的 +/- 号缺陷；回放 ledger_full 与 current 打平，默认仍 ledger_core |
| P07 | impl/P07-gate-queue | 已合入 | 0a6aea0 | 队列灯读门禁判定；approve 输出如实；拒绝卡只显示判题消息；逃逸锁 MCP；前 3 名候选 |
| P08 | impl/P08-startup-pack | 已合入 | f4e0219 | 启动包 4.5–4.9KB（约 1.3K token），仓库地图按 R11 默认关；DSH 插件按 Agent 类型分角色，按需挂载经核实不可行；见 reviews/P08.md |
| P09 | impl/P09-host-daemon | 已合入 | 7c279f9 | 判题版本的工具链主机上没有，编译检查默认跳过 |
| P10 | impl/P10-profiler-ab | 已合入（随 P11） | bf8c08e | profiler 计时 CV 61%→0.9% |
| P11 | impl/P11-batch-ui | 已合入 | bf8c08e | 合并批次无可测节省；R9 早停仅单测；界面截图待 daemon 重启后补 |
| P12 | impl/P12-report-views | 已合入 | 901c3b6 | 七个视图；真实数据 per_case 2.4–2.5KB、env 421B；queue 视图同源的 bridge status 会写门禁日志；跟进见 reviews/P12.md |
| P13 | impl/P13-bound-output | 已合入 | 1f035b3 | 回放估算：平台 MCP 返回字节 −59%（7.8MB→3.2MB）；合入后修了翻页不终止与多确认一条，见 reviews/P13.md |
| P14 | impl/P14-peer-protocol | 已合入 | f92efeb | 回放：peer 字节 −69%、result −81%，219 条 peer 合并成 135 次投递；跟进见 reviews/P14.md |
| P15 | impl/P15-worker-boundary | 已合入 | a419f41 | 回放扫描 11/13 个 worker 共 130 次越界，检出已知 `_all_team_sessions` 事件；Codex 权限块未在真机验证 |
| P17 | impl/P17-v2-calibrate | 已合入 | 746e928 | 真机：已知通过 SHA 无误报；a2717b5 仍只在平局变体上失败 |
| P16 | impl/P16-measure | 可开工（P12、P13 已合入） | | 主会话据报告决定 H/I/J |
| P18 | impl/P18-cleanup | 已合入 | efe11d7 | 可用额度不再扣收尾预留（门禁不变）；两条预算测试改虚拟时钟，负载下 20/20；缓存目录可配置，未在真机验证 |
| P19 | impl/P19-antigravity-mining | 已合入 | dfb51a4 | 4.3 调研：上一场 Antigravity 记录的涨分归因与 Top 10 改进；文中「4.3 现状」指当前 main |
