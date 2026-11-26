# 4.2 剩余工作完成计划（DSH 执行，主会话审查合入）

日期 2026-11-26。基线是 4.1.0 发布后的 `main`。4.1.0 已发布：判题一致性、本地测量可信度、门禁与队列、4.2 的 A/B/C（计量、变化驱动的等待、实验账本），见 `docs/v4/RELEASE-NOTES.md`。

本文件交给 DSH 执行。DSH 在 git 工作树里开原生子代理实现各包；主会话（Claude）负责审查、合入 `main`、维护 aoci 索引并推送。上级文档：[00-MASTER.md](00-MASTER.md)（子计划索引与共同守则）、[RUNBOOK.md](RUNBOOK.md)（状态与流程）。

## 0. 范围

| 包 | 内容 | 子计划 | 依赖 | 状态 |
|---|---|---|---|---|
| P12 | 4.2 D：平台视图 `parhelion_report` | [P12](P12-report-views.md) + [聚类报告](reports/P12-script-clusters.md)（在 P12 分支上） | — | 第 1 步聚类已完成（`impl/P12-report-views` 2ef788a） |
| P13 | 4.2 E：统一输出上限 `bound_output` | [P13](P13-bound-output.md) | P12 合入 | 未开工 |
| P14 | 4.2 F：同伴消息协议 | [P14](P14-peer-protocol.md) | — | 未开工 |
| P08 | 4.2 G：worker 启动包；4.1 #15：DSH 插件按需挂载 | [P08](P08-startup-pack.md) | P13 合入（`agent_peek` 用 `bound_output`） | 未开工 |
| P15 | 4.2 K：worker 边界 | [P15](P15-worker-boundary.md) | P12 合入（`board`/`env` 视图） | 未开工 |
| P18 | 收尾小修（本文件 §3.6） | 本文件 | — | 未开工 |
| P16 | 4.2 H/I/J 的测量（本文件 §3.7），由主会话决定做不做 | 本文件 | P12、P13 合入 | 未开工 |

合入顺序按依赖，谁先交付且依赖已满足就先合：P12 先于 P13、P15；P13 先于 P08；P14、P18 随时可合。

## 1. 分工与流程

**DSH 总包**：DSH 主对话，在仓库根 `/Users/winbeau/Developer/personal/parhelion` 打开。它负责读本计划、建工作树、给原生子代理派活、汇总交付。它自己不改代码，也不在主检出目录切分支或提交。

**每包一个子代理、一个工作树、一个分支**。工作树放在仓库外，不污染主检出：

```bash
git -C /Users/winbeau/Developer/personal/parhelion worktree add /Users/winbeau/Developer/personal/parhelion-wt/P14 -b impl/P14-peer-protocol main
```

P12 的分支已经存在，基于旧 `main`。先挂出工作树，再在工作树里变基：

```bash
git -C /Users/winbeau/Developer/personal/parhelion worktree add /Users/winbeau/Developer/personal/parhelion-wt/P12 impl/P12-report-views
```

```bash
git -C /Users/winbeau/Developer/personal/parhelion-wt/P12 rebase main
```

各包分支名：P12 `impl/P12-report-views`，P13 `impl/P13-bound-output`，P14 `impl/P14-peer-protocol`，P08 `impl/P08-startup-pack`，P15 `impl/P15-worker-boundary`，P18 `impl/P18-cleanup`，P16 `impl/P16-measure`。

**子代理的边界**：
- 只在自己的工作树里改文件，只提交到自己的分支；
- 不推送，不合入 `main`，不改 `aoci*.txt` 与 `.aoci/`（可以用 `aoci_search` / `aoci_get_entries` 定位文件）。

**交付**，两件都要有，主会话的定时检查按这两点识别：
1. 报告 `docs/plan-4.1-4.2/reports/Pxx-<slug>.md`，首行写 `状态：已交付`，内容按 00-MASTER §2.4（分支与提交、逐项完成情况、两条全量测试末行、真机或真实数据验证、NOTE、风险与后续）；
2. 分支最后一个提交的标题以 `Deliver Pxx:` 开头。

**返工**：
- 主会话把审查意见写进 `main` 上的 `docs/plan-4.1-4.2/reviews/Pxx.md` 并推送；
- 子代理用 `git show main:docs/plan-4.1-4.2/reviews/Pxx.md` 读取，在原分支修正；
- 修正后最后一个提交的标题用 `Redeliver Pxx:`，报告追加「返工」一节。

**合入之后**：
- 主会话更新 RUNBOOK §2 与 00-MASTER §4；
- 依赖它的包从最新 `main` 新开分支；
- 已开工、又与新合入内容相关的分支，执行 `git rebase main`。

**并发**：同时最多 3 个子代理。建议的排法：
- 链 A：P12 → P13 → P16；
- 链 B：P14 → P08 → P15。P15 要等 P12 合入；P08 若 P13 还没合入，可以先做启动包与插件，`agent_peek` 最后接；
- P18 插在任一空档。

## 2. 共同守则（在 00-MASTER §2 之上）

00-MASTER §2.2 的约束全部照旧：
- 不改自动提交开关，不强制提交，不调用 `queue approve` / `force`，不向判题站提交任何东西；
- 额度不进入任何门禁阈值或暂缓理由；
- 不重启、不停止守护进程，不派发或停止 worker，不清理总线；
- 回放数据与真实比赛数据只读；
- 仓库里不出现真实设备值；
- 新 MCP 工具三处登记（schema、handler、角色）；
- `cli_bridge` 的 JSON 契约两端同步；
- 回执去重按 `submission_id`。

另外：

- **线上跑的是主检出的代码。** `~/.local/bin/opsb` 指向主检出的 `cli/opsb`，守护进程和 DSH 插件起的 `opsb mcp serve` 都用它。所以工作树里的改动不影响线上，验证也必须在工作树内完成：
  - 用 `PYTHONPATH=cli:. ~/.parhelion/venv/bin/python` 直接调 handler；
  - 或在工作树内运行 `cli/opsb mcp serve --workspace /Users/winbeau/Competitions/mat-chem-sim-pred`，做一次 stdio 最小调用。
  
  不要运行 `opsb daemon reload`，不要安装插件或应用。
- **P08 改 DSH 插件时不能写进 `~/.dsh`。** 写进去会立即作用于正在运行的 DSH 会话，包括总包自己。只用 node 伪造插件上下文来测。
- **隐私。** 会话日志（`~/.parhelion/logs/sessions/mesh-dee-*.jsonl`、`~/.dsh/sessions/**`）与总线数据库只做聚合统计，以只读方式打开。报告里不出现 cookie、账号名、用户 ID、token 的值。
- **模块与工具计数。** 新模块不超过 350 行；`manager.py`、`estimator.py`、`cli_bridge.py`、`handlers_bench.py` 这类大文件只加薄接入点。新增 MCP 工具会改变工具计数断言，在 `cli/tests/test_agent_mesh_http.py` 与 `cli/tests/test_mcp_server.py`，目前主控 29 个（含 P12 的 `parhelion_report`、P13 的 `parhelion_spill_read`、P08 的 `parhelion_agent_peek`）。
- **测试基线（main，P12–P15、P08、P18 合入后；新包从这里切）：**
  ```bash
  ~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider
  ```
  785 passed / 19 skipped。
  ```bash
  PYTHONPATH=cli:. ~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider cli/tests runtime/tests
  ```
  538 passed。改了 Swift 还要 `swift build`。

  `runtime/tests/test_device_broker.py` 的两条预算测试在机器高负载时偶发失败，单独重跑通过即可，报告里注明。
- **提交信息**用英文祈使句，正文写原因。末尾署名实际执行的模型，例如 `Generated-by: DSH <模型名>`；不要写 Claude 的署名行。

## 3. 各包计划

子计划写的是目标、证据、接口与验收。下面补充 4.1.0 之后的代码事实：哪些已经存在、应当复用，不要重写。子计划与源码冲突时以源码为准，并在报告里说明。

### 3.1 P12 `parhelion_report`（4.2 D）

先读子计划和 P12 分支上的聚类报告，其中 §3 分组、§5 新用途、§6 数据形状必读。新模块 `cli/opsb_core/mcp/handlers_report.py`。工具为 `parhelion_report(view, problem?, sha?, base?, limit?)`。

**各视图与复用来源：**

| 视图 | 内容 | 复用 |
|---|---|---|
| `per_case` | 固定列：用例、本方 µs、tbest µs、差距 %、噪声带、三态判定、判题映射标签、计时来源（event/profiler）、正确性（`passed`、`exit_code`、`mismatch_n`、`strict_fp32`）、WA 复现标记、最近运行 ID。≤4KB | 逐例加速比、差距与目标取 `services/state_card_ext.data()`（与门禁同源）；三态与噪声带取 `scheduler/verdicts`；映射取 `scheduler/case_map`；校准标签取 `scheduler/case_calibration.table`；复现状态取 `scheduler/wa_repro.status` |
| `diff` | 两个 SHA 的逐例比与区间，附 `git diff --stat` | 先读 `report.json` 已有的 `pairs` / `pair` / `pair_bootstrap` / `digest.paired`（P10），不要重算；没有配对时用 `scheduler/paired_timing` 与 `scheduler/timing.best_minima`；`git` 经 `ledger/gitref.git_out` |
| `history` | 判题历史、逐例比、回执状态 | `scheduler/receipts.canonical`，以及 `results/submit-queue-outcome.jsonl` |
| `queue` | 候选、暂缓、在途、已完成与门禁判定 | 与 `opsb queue list --json` 同源（`ipc_bridge.merge_bridge_enrichment` 的输出，含 `gate_view` 字段）；不返回 `account_id` |
| `board` | 榜单分、本方排名、逐例 tbest 与观测时间、frontier | 只读 `services/board_refresh_service` 与 `ranking_service` 已写入的缓存，测试用 mock 断言零网络请求 |
| `bottleneck` | R6：瓶颈类别、受限资源占峰值百分比、前 3 个指标、知识库候选修复族、原始剖析路径 | `fast_feedback/pmu_torch_card`、`knowledge/query` |
| `env` | 聚类 §5.1：平台版本、平台根、守护进程状态、工作区与结果目录、磁盘余量，≤1KB | 不含任何账号、会话或凭据字段。它也是 P15 的数据通道 |

**规则：**
- **形状。** `report.json` 有两种形态；`cases[].perf` 是 bool，不是对象；`report.json` 的路径不唯一，要按 glob 扫。`report.settings` 与 `report.environment` 里的设备字段不能带出，过滤按 `device_env` 的既有约定做。
- **角色。** worker 只能查自己的题，照 `mcp/tools.py` 的 `WORKER_SCOPED_PROBLEM_TOOLS` 与 `mcp/handlers_ledger.problem_of` 的做法。`board`、`env` worker 也能用；主控可以查全部。
- **工具描述。** 写明用它代替读 `report.json` / `outcome.jsonl` 的临时脚本，并写出各视图的列说明。这能吸收 `opsb --help` 的流量（聚类 §5.5）。

**验收**：子计划的单测，外加：
- 两种 `report.json` 形态的 fixture；
- 覆盖对照表：`opsb queue list --json` 与 `opsb history --json` 的每个字段在哪个视图里；
- 对真实比赛只读跑一遍全部视图，记录字节数。

回放口径按聚类报告修正为「A 组 441 次调用降 80%」，报告写明估算方法。

### 3.2 P13 `bound_output`（4.2 E）

`mcp/server.py` 的 `tools/call` 已经是唯一出口，并在那里调 `metrics/mcp_meter.record_call`。新模块 `cli/opsb_core/mcp/bound.py` 接在同一处，计量行加 `spilled` 与 `bound_bytes`。

- **落盘。** 路径 `<workspace state>/spill/<agent_id>/`，工作区状态目录用 `services/test_runs.registry_dir` 所用的同一个解析函数。保留 7 天、总量 200MB，并加入 `opsb doctor clean` 的列表（`commands/doctor_cmd.py`），默认只列不删。
- **分页。** 适用于 `parhelion_agent_messages`、`parhelion_kb_query`、`parhelion_ledger`，以及 `parhelion_report` 的 `history` 与 `queue` 视图。超限时先分页，再考虑落盘。
- **回读。** 新工具 `parhelion_spill_read`：worker 只能读自己的文件，主控可读全部；回读结果不再二次落盘。
- **两种格式。** `format=brief|full` 用于 `parhelion_status`、`parhelion_kb_query`、`parhelion_test_wait` 与测试结果消息。brief 的默认值就是现有上限（status 2KB；kb 3 条，每条 600 字符）。`parhelion_status` 的 `rev` / `if_rev` 语义不变。
- **失败。** 工具失败时保留结果尾部。
- **并入的跟进项（P12、P14 审查）：**
  - 把 `mcp/handlers_report.py` 拆成两个按常规格式书写的模块，不改行为（`reviews/P12.md` 第 1 项）；
  - `<workspace state>/peer/` 的附件、`quotes/`、`index.jsonl` 与 spill 一起进 `opsb doctor clean` 的列表，默认只列不删（`reviews/P14.md` 第 1、5 项）。

**验收**：按子计划，并用 `opsb replay metrics` 估算全部返回都经过 `bound_output` 时省下的字节。

### 3.3 P14 同伴消息协议（4.2 F）

现有入口：
- `agents/manager.py` 的 `squad` / `_squad_targets`，以及 `mcp/handlers_agents.tool_parhelion_squad`；
- `ledger/note.write(..., source="peer")` 已支持同伴来源；
- 回合边界有 `manager` 的 `note_boundary` 与 runner；唤醒走 `agents/event_hub` / `event_sources`。

**实现：**
- 信封、附件、去重与合并投递放进新模块 `agents/peer.py`，`manager.py` 只加接入点；附件落到 `<workspace state>/peer/<msg_id>.md`。
- 同一收件方的多条同伴消息在下一个回合边界合并成一条投递。管理员指令与判题反馈仍立即投递。合并后的投递只产生一个 hub 事件，`parhelion_wait` 的调用者不会被唤醒多次。
- `result` 消息遵守同样的 claim 与附件规则，全文落盘，同时写一条账本 `decision` 或 `insight`。

**验收**：按子计划。回放估算从 `agent_bus.db` 只读取数，打开方式照 `replay/sources.py` 的 `mode=ro`。

### 3.4 P08 worker 启动包与 DSH 插件（4.2 G；4.1 #15）

**启动包：**
- 接在 `drivers/brief.py` 的 `compose_session_brief` 后面；1.5KB 上限只约束契约部分。
- 账本段用 `ledger/digest.render`，逐例数据用 `services/state_card_ext`。
- 截断顺序 5、3、4，按 UTF-8 字节截断。
- 按 R11，仓库地图只有在回放证明有收益时才默认开启；否则只保留不可发现的约束（判题怪癖、禁用模式、平台陷阱）。

**顺带修正** `brief.py` 约第 188 行：它仍写着 “apply the normal safety, gain, circuit and quota gates”。改成与 4.1 门禁一致：额度不是门禁，S4 只决定派发时机。

**`workdir` 指引**：逐个驱动核实 bash 工具是否支持 `workdir`（DSH、Codex 等），报告列出结论。

**`parhelion_agent_peek`**：仅 orchestrator 可用，路径限定在该 worker 的工作树内；P13 合入后经 `bound_output` 输出。

**DSH 插件**（`drivers/dsh_parhelion_mcp.mjs`、`drivers/dsh_driver.py` 的 `ensure_parhelion_mcp_plugin`）：
- 懒启动：首次调用工具时才 spawn；DSH 不支持时，让 `opsb mcp serve` 空闲若干分钟后退出，由插件在下次请求时重启。
- 按 Agent 类型给角色：比赛根目录的主对话是 orchestrator；`.worktrees/*` 下的对话与原生子代理是 worker；识别不了时默认 worker。
- 插件块由起止标记界定、启动时整段改写，这个机制保持不变。
- **不要写进 `~/.dsh`**，只用 node 伪造上下文测试。

**验收**：
- 按子计划；
- 回放口径对 3 个真实 worker 生成启动包，记录字节数与估算 token；
- 目标「起步到第一次平台调用 ≤15K」「以 `cd` 开头的 shell 调用降 70%」要等下一场冲刺实测，本包只给估算。

### 3.5 P15 worker 边界（4.2 K）

**涉及的启动路径：**
- `agents/adapters.py` 中 `BaseWorkerAdapter` 及各子类的进程环境；
- `drivers/registry.py` 的分离式启动；
- `worker_access.py`；
- `drivers/codex_appserver.py` 的配置渲染；
- `drivers/dsh_driver.py`。

**检测范围**：按聚类报告 §5.1，13/13 个 worker 共有 91 次这类调用，形态包括：
- `import opsb_core`，或把平台根塞进 `sys.path`；
- 读 `state/test_runs/*.json`、`agent_bus.db`；
- 读 `~/.parhelion/cache/.cache_remote_judge_*.json`、`cj_accounts.json`、`~/.judge/`；
- 调 `_all_team_sessions`。

检测器必须覆盖这些形态，同时不误报工作树内的同名文件。

**告警：**
- 给主控和管理员各发一条总线 `platform` 消息，同一 worker 同一类只发一次；
- 写一条账本 `constraint`；
- 协作页标记该 worker：热 mesh 加字段，Swift 用 `decodeIfPresent` 解码并显示；
- 不自动停止 worker。

**数据通道**：worker 需要的榜单、tbest、回执与环境信息，全部经 `parhelion_report` 的 `board`、`history`、`per_case`、`env` 视图提供。brief 与工具描述里写明这一点。

**验收**：按子计划。回放扫描写入 `reports/P15-boundary-scan.md`，必须检出本场已知的 `_all_team_sessions` 事件；报告只写 worker、时间与类别，不写读到的内容。

### 3.6 P18 收尾小修

1. **可用额度显示。** `available_normal_shots` 不再扣除收尾预留，位置在 `cli/opsb_core/ipc_bridge.py` 约第 91 行、`cli/opsb_core/daemon/state_cache.py` 约第 829 行与第 949 行。收尾预留作为单独的显示字段保留。同步 `commands/status_cmd.py` 的文案；Swift 若有显示，一并更新。
2. **设备代理测试加固。** `runtime/tests/test_device_broker.py` 的 `test_job_budget_turns_remaining_cases_into_tle` 与 `test_case_budget_limits_each_execution_not_the_repeats` 不再依赖墙钟睡眠，改为注入时钟或事件驱动。验收：在机器高负载下（例如同时跑另一条全量测试）连跑 20 次不失败。
3. **（可选）用例缓存目录可配置。** P09 报告 §6 指出：设备主机是容器，重启会清空 `/tmp`。新增 `device.case_cache_dir` 与 `device.case_tmp_dir`，空值保持现状，经 `device_env` 下发。两个目录必须在同一文件系统才能硬链接，不同时给出明确提示或退回复制。仓库里不写真实路径。

### 3.7 P16 H/I/J 测量（只测量，不实现）

产出 `reports/P16-measure.md`，由主会话据此决定 H、I、J 做哪些，再补写子计划。等 P12、P13 合入后进行，这样工具表里已经包含 report 与 spill 工具。

- **H 观测遮蔽：**
  - 只读核实 DSH、ACP 与 Codex app-server 是否支持清理或替换旧工具结果，依据驱动现有代码与文档；
  - 用 `opsb replay metrics` 估算遮蔽 10 轮以前的可再生输出能省多少上下文。
- **I 预算与护栏：** 从回放统计每个 agent 的工具调用与模型调用分布、相同调用的连续风暴、空操作回显，判断护栏是否值得做。
- **J 渐进披露：**
  - 按角色序列化 `list_tool_schemas`，测工具表本身的字节与估算 token；
  - 统计 `kb_query` 返回里正文的占比。
- **P14 的 result claim：** P14 之后长 `result` 在总线上只留开头 280 字作 claim。用回放统计这 280 字里含结论（测得数字、SHA、判定词）的比例，与取最后一段对比，见 `reviews/P14.md` 第 4 项。

每项给出数字、建议做或不做、预计工作量。

**结论（P16 已合入）**：H、I 不做；J-1（`kb_query` 回复去掉与 `results` 重复的 `entries`）已由主会话在 main 上完成；J-2 进入 4.2.0，见 §3.8。

### 3.8 P20 工具表瘦身（4.2 J-2）

分支 `impl/P20-tool-table`，工作树 `/Users/winbeau/Developer/personal/parhelion-wt/P20`，从最新 `main` 切。报告 `reports/P20-tool-table.md`。

**为什么做**：P16 实测，工具表占 worker 首次提示的 86%（9,843 token 里的 8,432）；平台自己的那部分从冲刺时的 8,322 字节涨到现在 worker 角色 22,066 字节、主控 25,007 字节（`list_tool_schemas(role)` 紧凑 JSON），涨幅 165%，主要来自 4.2 自己新增的工具与描述。最大一项 `parhelion_report` 3,112 字节，其中描述 2,148 字符；`parhelion_note` 2,142 字节。工具表随每次模型调用发送，`bound_output` 管不到它。

**目标**：
- 主控 ≤ 15,000 字节、worker ≤ 13,000 字节（`json.dumps(list_tool_schemas(role), ensure_ascii=False, separators=(",", ":"))` 的 UTF-8 字节）；
- 加一条测试把这两个上限钉住，以后新增工具或加长描述会直接失败。

**做法**：
- 工具名、参数名、参数类型、角色与工具数（主控 29）一律不变，只改描述文字与 schema 里的说明字段。
- `parhelion_report` 的逐视图列说明移出 schema，改由工具自己回答：例如加 `view="help"`（或在各视图回复里带一段简短的 `columns_doc`），保证模型仍能从工具本身查到每一列的含义。schema 描述只留：视图名列表、一句「用它代替读 report.json 与临时脚本」，以及 P15 的数据通道规则的一句话版本。
- 其余描述按「一句话说做什么 + 必要的约束」改写：`parhelion_note` 的字段说明压缩；`parhelion_wait` 保留「优先它而不是循环 status」；`parhelion_test` 保留级别说明。
- 不做 P16 的 J-3（少用工具后置、工具搜索），留到 4.3。

**验收**：
- 两条全量测试全绿（基线见 §2；本包会改几处描述断言）；
- 报告给出每个角色改前改后的字节与估算 token（0.33 token/字节，与 P16 同口径），以及按 P16 §4.2 口径推算的 worker 首次提示变化；
- 在工作树内用 `cli/opsb mcp serve` 做一次 stdio 的 `tools/list` 与 `parhelion_report(view=help)`（或等价入口）调用，确认可用。

## 4. 主会话的审查与合入

**审查清单：**
- §2 的约束逐条核对；
- diff 只含本包文件；
- 新工具三处登记，角色正确；
- 没有设备值，也没有账号或凭据字段；
- 新模块不超过 350 行；
- 报告完整；
- 两条全量测试全绿。

**冲突热点**：`mcp/schemas.py`、`mcp/tools.py`、`agents/manager.py`、`mcp/handlers_bench.py`、工具计数断言。

**合入流程**：
1. `git merge --no-ff`；
2. 两条全量测试（需要时加 `swift build`）；
3. `aoci_maintain` 直至 aligned，索引单独提交；
4. 更新 RUNBOOK §2 与 00-MASTER §4；
5. `git push origin main`。

需要守护进程或应用生效的改动（P13 的 server、P14 的 manager、P15 的 adapters、P08 的 brief 与插件、P15 的 Swift），全部合入后统一重启一次守护进程、重装应用，作为 4.2.0 发布。

## 5. 4.2.0 发布（全部合入后，主会话）

1. 重启守护进程，重装应用，确认新工具在 `opsb mcp serve` 中可见。
2. 版本升到 4.2.0：`cli/opsb_core/__init__.py`，以及 `scripts/bundle_app.sh` 的 `CFBundleShortVersionString` 与 `CFBundleVersion`。在 `docs/v4/RELEASE-NOTES.md` 写发布说明，打 tag `v4.2.0` 并推送。
3. P16 决定要做的 H/I/J 另行计划。
4. 下一场同等规模的冲刺用 `docs/v4.2/00-PLAN.md` §3 的指标验收。

## 6. DSH 总包启动提示词

把下面这段贴进 DSH 主对话（工作目录为仓库根）：

```text
你是 Parhelion 4.2 剩余工作的总包。仓库：/Users/winbeau/Developer/personal/parhelion。
先完整读 docs/plan-4.1-4.2/20-REMAINING-4.2.md，再读 00-MASTER.md §2 和各包子计划。
按 §1 的流程，为每个包在 /Users/winbeau/Developer/personal/parhelion-wt/<包> 建工作树、开一个原生子代理；同时最多 3 个。
先开 P12（链 A）、P14（链 B）和 P18。P13、P15 等 P12 合入，P08 等 P13 合入，P16 等 P12 与 P13 合入。
子代理交付的标志：报告首行「状态：已交付」，最后一个提交的标题以「Deliver Pxx:」开头。交付后停下，等主会话审查合入。
你和子代理都不合入 main、不推送、不改 aoci 索引、不在主检出目录切分支或提交、不重启守护进程、不安装插件或应用。
合入进度看 docs/plan-4.1-4.2/RUNBOOK.md §2；返工意见在 main 的 docs/plan-4.1-4.2/reviews/Pxx.md（git show main:<路径> 读取）。
```
