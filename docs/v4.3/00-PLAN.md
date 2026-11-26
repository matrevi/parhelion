# Parhelion 4.3 计划：更便宜的试错、更快的回滚、更准的排序

日期 2026-11-26，基线是 4.2.1（`v4.2.1`，build 34）的 `main`。实施由 DSH 在 git 工作树里完成，主会话（Claude）负责审查、合入、维护 aoci 索引与发布。流程与 DSH 启动提示词见 [10-DSH.md](10-DSH.md)，续跑状态见 [RUNBOOK.md](RUNBOOK.md)。

依据是上一场 ops-bench 的归因调研 [research/antigravity/00-SUMMARY.md](research/antigravity/00-SUMMARY.md) 与 R1–R6。调研里的「4.3 现状」指当时的 `main`（4.2.0）。下面每条都已在 4.2.1 的 `main` 上重新核对过。

## 0. 一句话

上一场涨分，靠的是把每一发当成一次低价实验：
- 失败能一发回滚：16% 的提交是 restore；
- 判题噪声被接受：同源重判 σ 约 3 分；
- 硬件规律两小时内传遍全队。

4.2.1 已经拆掉了三处硬阻断：榜型、TLE 锁题、熔断。4.3 要补的是剩下三件事，**不加新的门**：
1. **试错更便宜**：给投机发正式的入口与预算，并带上「失败怎么撤回」；
2. **回滚更快**：restore 一键化，收官自动对账；
3. **排序更准**：估分只决定先发哪一发，判题规则由平台自己取证。

## 1. 调研结论在 4.2.1 之后的状态

| 调研条目 | 4.2.1 已做 | 4.3 还要做 | 包 |
|---|---|---|---|
| §4.1 榜型 latest | 门禁按题目的 `ranking_submission_mode` 选 best/latest 口径，开关 `board_mode_from_rules` | 榜型只能从赛题抓取拿到，当前工作区没有记录。另外，平台在对账回执时已经逐题读取 `/api/problems/{pid}`，这份规则却没有落盘（`sync_reconciler.py:285`）。latest 赛制下的分数重建在 `judge_score_fetch.reconstruct_timing_score` 里仍然早退，上一场 7 条「Pass 无分」因此补不回来 | P22 |
| §4.2 投机发与回滚 | 低增益但 P(gain>0)≥0.5 的候选按 EXPLORE 放行 | 没有 probe/restore 意图。worker 契约仍写着 "Never use --force, --now"。重投已在榜的 SHA 一律判 0 增益（`ALREADY_ACTIVE_ON_BOARD`），而 restore 只在榜面确认归零时才放行（`is_critical_restore`） | P23 |
| §4.3 熔断 | 熔断改为建议性：照常放行，优先级减半 | 「六发无增益」已经会给主控发 stagnation 事件（`agents/judge_feedback.py`），但还没有换席的执行器；连败也还没有按同一假设计数 | P26、P29 |
| §4.4 逃逸锁 | TLE 不再锁题；超时的 SHA 未线上 Pass 前不能原样重投 | WA/RE 复现变体在坏 SHA 上全过（未复现）时，锁永远停在 pending，没有「降级为提示」的出口（`wa_repro.status`） | P24 |
| §4.5 提交前机械体检 | — | S1 不读编译秒数、模板实例化数、内核体积与冷启动。这些数字在测试结果里已有一部分（`runtime/npu-test.py` 记录 `phase_a_cpu_sec`、`template_instantiations`） | P24 |
| §4.6 派活说明 | — | brief 契约已接近 1.5KB 上限。榜首、加权空间、最近几发的机制与结果、停手条件都没有给出 | P27 |
| §4.7 席位 | — | 只有 solo/dual/full 三种模式。席位按 `.worktrees/sprint-<axis>` 目录名识别，不能按题设模式，冲刺中途也不能调整。**同题两席共用一个远端检出目录**（`queue_cmd._remote_problem_dir` 是 `<root>/<Title>`），dual 模式已经存在这个风险 | P26 |
| §4.8 广播与证据等级 | — | `parhelion_squad` 只能定向发送，消息没有证据等级，也没有跨题横向扫描 | P27、P28 |
| §4.9 σ 与估分 | 噪声底线 = clamp(σ̂, 2.5, 5.0) | 估分器仍用常数 `SIGMA_HIDDEN_JITTER = 2.70`。跨代改动的预测高估约 3.7 倍。强制提交的回执还在进校准样本 | P23、P29 |
| §4.10 收官对账 | — | `restore_targets` 已经算出来了，但只用于显示；没有对账动作 | P25 |
| §4.11 槽位、公开/隐藏分解、过程知识 | — | 槽位利用率不可见；没有 public/hidden 分解；知识库没有计分律、度量律这类类目，也没有证据等级 | P22、P25、P28 |
| R6 §8 开局豁免、规则取证 | — | 开局豁免固定为 3 发，而上一场有 4 题前 3 发内没出分；规则取证要靠 worker 拿登录态去做 | P23、P22 |
| — 回放验收 | — | 门禁改动目前只能靠单测与逐发手算回放（R5 的 13/231 是手算的）。需要一个能重复运行的门禁回放基准，作为 4.3 所有门禁包的验收尺 | P21 |

核对后不做的条目见 §5。

## 2. 目标与验收指标

所有指标都用 P21 的同一套代码计算，跑在两场数据上：ops-bench（latest 赛制，233 条回执）和 2026-11-26 那场（mat-chem-sim-pred 工作区）。下一场冲刺用同一命令复测。

| # | 指标 | 4.2.0 基线 | 4.3 目标 | 怎么量 |
|---|---|---|---|---|
| G1 | 回放中被门禁拦下的「当时刷新历史最高」的发数 | 4（R5 手算：`0aa17ab` `ba4a357` `a031e4e` `1dc3411`） | 0 | `opsb replay gate` 的 blocked_new_highs |
| G2 | 回放总拦截率 | 13/231（5.6%，手算） | ≤ 3%，且每一发被拦都有可执行的下一步 | 同上，按阶段码分列 |
| G3 | 判题侧失败（TLE，以及未复现的 WA/RE）之后，该题被冻结的发数 | TLE 永久冻结（4.2.1 已修） | 冻结 ≤ 1 发：复现没过之前允许 restore 与改过的新 SHA | 回放的 frozen_shots |
| G4 | latest 赛制下，榜面归零后 restore 获批的比例 | 只有确认归零时才放行 | 100%（有线上 Pass 过的 SHA 时） | 回放的 restore_approved / zero_events |
| G5 | 收官差额：每题历史最高减去收盘在榜分，求和 | ops-bench 10.19 裸分（约 23.3 加权） | 下一场为 0，额度或时间不够时报告原因 | P25 的 close 视图；收盘后复算 |
| G6 | 排序质量：门禁优先级与实际增益的 Spearman，以及 top-1 命中率 | P29 在回放上先测出基线 | 置信区间下界高于基线才默认启用 | P29 报告 |
| G7 | 冲刺结果：新高率、硬失败率、分/判 | ops-bench：新高 35.6%，硬失败 6.4%，331.36 / 231 ≈ 1.43 分/判 | 下一场新高率 ≥ 35%，硬失败 ≤ 6%，编译规模导致的 TLE 为 0；分/判与题目难度相关，只记录不设线 | 回放 outcome 段 |
| G8 | 上下文成本不回退 | 工具表：主控 14601 / worker 12507 字节（上限 15000 / 13000） | 不超上限；启动包不超预算 | 现有字节测试；`opsb replay metrics` |

S2 依赖本地证据。旧数据的报告格式较早，S2 的回放只是近似。S1 的逃逸锁与 TLE、S3、榜型、restore 只依赖回执，回放是可信的。P21 的报告要分别标明。

## 3. 工作包

| 包 | 内容 | 解决 | 依赖 | 主要文件 | 规模 |
|---|---|---|---|---|---|
| [P21](P21-gate-replay.md) | 门禁回放基准与 4.3 指标：`opsb replay gate`，在比赛数据的副本上逐发重放门禁，输出拦截、冻结、restore 与冲刺结果 | G1–G5、G7 的尺子 | — | `cli/opsb_core/replay/gate_replay.py`（新）、`replay_cmd.py` | 中 |
| [P22](P22-judge-rules.md) | 判题规则快照：把逐题规则落盘，作为榜型的首选来源；latest 下的分数重建；`opsb workspace rules`；`parhelion_report(view=rules)`，含 public/hidden 分解 | §4.1 余项、R6 §8.3、§4.11 | — | `services/judge_rules.py`（新）、`sync_reconciler.py`、`judge_score_fetch.py`、`estimator._load_problems`、`report_views.py` | 中 |
| [P23](P23-submit-intents.md) | 提交意图：`intent=exploit\|probe\|restore`。probe 必须带 undo，并有每题预算；restore 按 σ̂ 定价；区分重投与恢复；开局豁免延长到首次出分；强制提交不进校准 | §4.2、§4.9 部分、R6 §8.1/§8.6 | P21 合入（验收） | `gate.py`、`estimator.py`、`handlers_bench.py`、`queue_service.py`、`brief.py` 一行 | 大 |
| [P24](P24-escape-health.md) | 逃逸锁出口与 S1 体检：未复现即降级为提示，并记作判题噪声；编译秒数、实例化数、体积增长与冷启动进入门禁回复；判题时限风险需要 probe 意图 | §4.4 余项、§4.5 | P21 合入（验收） | `wa_repro.py`、`gate.py` S1、`scheduler/health.py`（新） | 中 |
| [P25](P25-close-sweep.md) | 收官对账与槽位可见性：`parhelion_report(view=close)` 与 `opsb close-sweep`；收官窗口里 restore 排在前面；每槽利用率 | §4.10、§4.11 槽位 | P23 合入 | `scheduler/close_sweep.py`（新）、`gate.get_quota_status`、`report_views.py` | 中 |
| [P26](P26-sprint-seats.md) | 席位与冲刺 v2：按（题，分支）识别席位；新增 race/pipeline 模式与 recon/integrator/knowledge 角色；按题设模式；冲刺中调整与换席；同题远端检出隔离 | §4.7、§4.3 执行器、R6 §8.2/§8.4 | — | `agents/sprint.py`、`handlers_agents.py`、`manager.py`（薄）、`queue_cmd.py`、`mcp/tools.py` | 大 |
| [P27](P27-brief-broadcast.md) | 派活情报与全队广播：启动包加入榜首与加权空间、最近三发的机制与结果、停手条件、规则摘要；`parhelion_squad(to="all")`，消息必须标明证据等级；`parhelion_report(view=sweep)` 跨题横向扫描 | §4.6、§4.8 | P26 合入（brief 冲突）；P22 可选 | `startup_pack.py`、`state_card_ext.py`、`peer.py`、`report_views.py` | 中 |
| [P28](P28-kb-evidence.md) | 知识库 v2：每条证据带证据等级，补齐机制确认、反例、触发条件、代价、适用题与作废指针等字段；新增类目 `scoring-law`、`measurement`、`process`；晋升到硬件规律时必须有线上或配对证据；与设备无关的过程卡 | §4.11 过程知识、R4 §6–§7 | — | `knowledge/evolution.py`、`query.py`、`seeding.py`、`templates/**/index.json` | 中 |
| [P29](P29-ranking.md) | 估分排序：σ̂ 替换常数，同代重测与跨代改动分两档，跨代用饱和变换；在回放上验证排序质量，达标才默认启用 | §4.9 余项、G6 | P21、P23 合入 | `estimator.py`（薄）、`scheduler/ranking.py`（新） | 中 |

每个改变门禁或派发行为的包，都要给出一个 `GateConfig` 开关（经 `opsb queue config --gate` 设置）或配置项，能回到 4.2.1 的行为，与 4.2.1 的做法一致。

## 4. 依赖与合入顺序

- **先开**：P21、P22、P26。三者互不依赖，同时最多 3 个子代理。
- **P21 合入后**：P23、P24。两者都改 `gate.py`，但区域不同：P23 改 S2 与意图，P24 改 S1。谁先交付先合，另一个随后 `git rebase main`。
- **P23 合入后**：P25、P29。
- **P26 合入后**：P27。
- **P28** 在任一空档开工，随时可合。

冲突热点：
- `scheduler/gate.py`：P23、P24、P25、P29；
- `scheduler/estimator.py`：P22、P23、P29；
- `mcp/schemas.py` 与工具表字节测试：P22、P23、P25、P26、P27；
- `drivers/brief.py`：P23 改一行契约，P26 加角色 brief，P27 改启动包；
- `report_views.py`：P22、P25、P27；
- `agents/manager.py`：P26、P27。

工具表的余量只有主控约 400 字节、worker 约 490 字节。所以 4.3 **优先给现有工具加参数或视图，不加新工具**。各包在报告里写出改前改后的字节数。如果确实超限，由主会话在审查时决定是否调高上限，子代理不要自行修改上限。

## 5. 核对后不做的

| 条目 | 原因 |
|---|---|
| 把 `--force` 日常化（§5.1） | 上一场的净收益约为 0（+45.21 / −44.23），真正的价值在于能回滚。由 P23 的 probe 意图承担：带 undo、有预算 |
| 同源重投吃噪声（§5.2） | 同源重投的中位约 −1 分。P23 用 σ̂ 给它定价，不鼓励 |
| 冲刺中加账号槽（§4.11、R5 §6） | 加槽是用户的账号操作，平台不做。P25 只显示每槽利用率并提示 |
| worker 自写测量脚本代替平台套件（R5 §3） | 保留 AGENTS.md 第 5 条：套件由平台维护。worker 已能经平台接口增补用例与分桶；P26 的 harness/校准席位在 pipeline 模式里默认开，走的仍是这套接口 |
| 设备主机只读文件接口（R3 §5 #2） | 共享主机上开放任意只读路径，需要先定访问策略，留到 4.4 |
| S1 校验「树与 SHA 一致」（R5 §11） | 已核实不适用：设备主机按 SHA `checkout --detach --force` 并校验 HEAD（`runtime/npu-test.py:312-315`），证据绑定的就是该 SHA。R5 说的是上一场比赛目录里自带的测试脚本 |
| 默认一题 4 路（§5.7） | 证据只有主控自述，无法归因。P26 提供 race 模式，路数显式指定，默认仍是 solo |
| 赛后集中写知识库（§5.9） | 保留 4.2 的边打边写账本。P28 只补证据等级与过程类目 |
| 设备 profiler 的「硬件定律」直接广播（§5.8） | P27 的广播必须标证据等级，`local` 显示为「待验证」；P28 规定只有本地证据的条目不能晋升为硬件规律 |

## 6. 风险与未验证项

1. **旧数据的 S2 回放只是近似。** ops-bench 是 4.0 之前的报告格式，case-bench 配对证据很少。门禁包的 S2 结论以 2026-11-26 那场为主，ops-bench 主要验证 S1、S3、榜型与 restore。
2. **probe 预算可能多烧额度。** 默认值从保守出发（D2）。P21 回放要报告：放开 probe 后多批准了几发、预期增益是多少。
3. **race/pipeline 席位会加重编译 CPU 的争用。** R3 测得设备不是瓶颈，编译才是（中位 53 秒）。P26 要在真机上测同题两席并发，并确认远端检出已经隔离。
4. **判题规则接口的字段可能变化。** P22 按「字段缺失即省略」处理，门禁回落到 best 口径，与 4.2.1 相同。
5. **模型被内容安全过滤连续拒绝（R2 §2.2）** 属于驱动层故障，现有的驱动熔断会告警一次，不在 4.3 范围内。
6. **P29 可能达不到启用标准。** 达不到时开关默认关，只合入测量部分。

## 7. 需要你拍板的

| # | 决定 | 推荐 | 影响 |
|---|---|---|---|
| D1 | 开局豁免从「每题前 3 个判题回执」改为「直到该题首次有分，最多 8 发」 | 改。上一场有 4 题前 3 发内没出分，CTCLossV2 到第 40 小时才有分 | 改写 AGENTS.md 第 3 条；开关 `opening_until_first_score` |
| D2 | probe 意图：免噪声底线，每题每 12 小时最多 2 发；latest 赛制下必须有可回滚的线上 Pass SHA，否则按 exploit 评估 | 做。预算用完后按 exploit 评估，不拒绝 | worker 契约里的 "Never use --force" 保留，并加一句 probe 的用法 |
| D3 | 判题时限风险（编译秒数加上预估判题耗时，接近时限）时，exploit 暂缓，要求改代码或用 probe 发 | 做，开关可改为只提示 | 这是 4.3 唯一新增的暂缓理由，针对的是一发 TLE 约等于 2 发额度加 14 分钟 |
| D4 | stagnation（六发无增益）之后自动换席 | 默认关。平台准备好换席所需的失败清单与 brief，主控一次调用 `sprint_start(replace=…)` 完成 | AGENTS.md 第 6 条写的是主控收集失败总结后由新 worker 接手，4.3 只补执行器 |

D1 与 D4 涉及改写 AGENTS.md：第 3、6 条与末段的席位模式。主会话在发布 4.3.0 时按你的决定统一改写，改写不交给 DSH。

## 8. 发布 4.3.0（全部合入后，主会话）

1. 两条全量测试与 `swift build`（如有 Swift 改动）。
2. 重启守护进程，重装应用；确认 `opsb mcp serve` 的工具与视图都在，`opsb replay gate` 在两场数据上跑出 §2 的表。
3. 在当前工作区执行 `opsb workspace rules --refresh`（P22），确认榜型已记录。
4. 版本升到 4.3.0（`cli/opsb_core/__init__.py`，以及 `scripts/bundle_app.sh` 的两个版本字段），写 RELEASE-NOTES，打 tag `v4.3.0` 并推送。
5. 按 D1–D4 改写 AGENTS.md；aoci 收尾。
