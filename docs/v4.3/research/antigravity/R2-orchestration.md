# R2 编排与信息流

> 数据窗口：11-18 `cacd27d9`（559 步 / 5.8h / 15 worker）、11-22～23 `95eafe6a`（2510 步 / 26.2h / 55 子代理）、11-23 `813867cb`（1717 步 / 5.25h / 20 worker），共 233 条去重判题回执（11-10 39、11-11 2、11-18 20、11-22 117、11-23 55）。board 时间戳为本地时间（UTC-5），transcript `created_at` 为 UTC，正文引用时统一换算。
>
> 数据来源限制：`cacd27d9` 没有 swarm 目录（无 board.jsonl/members.json），其派活原文只能从 `invoke_subagent` 参数恢复，worker 侧汇报只能从主控收到的 SYSTEM_MESSAGE 恢复。**因此 11-18 与后两场的对比在「派活」上可靠，在「worker 主动汇报的频次」上不可比。**

---

## 0. 一页速览

| 问题 | 结论 | 证据 |
|---|---|---|
| 11-22 那 8 个 `opus-*` 为什么在 15:15 全灭 | **模型不可用**：`claude-opus-5-5-high` 连续 4 次被内容安全过滤器拦下（"Output retries exhausted after 4 attempts"），8 个 worker 在 spawn 后 12 秒内全部 error。主控 2 分钟后按用户令改成 carbon。 | [证据: 95eafe6a#1365–#1374]、[证据: 95eafe6a#1378 用户输入]、[证据: 95eafe6a#1379] |
| `carbon-*` 16:57–17:55 陆续 killed 是换血还是收工 | **收工 + 收缩并发**，不是换血。17:04 用户令"收缩并发，把剩下 17% 额度全打在刀刃上"，主控杀 7 个；17:55 用户令"我关服务器了"，主控 `kill_all` + 杀最后一个。 | [证据: 95eafe6a#1558]、[证据: 95eafe6a#1560]、[证据: 95eafe6a#1620]、[证据: 95eafe6a#1622] |
| 11-23 为什么 20 个全存活 | 这一场**没有发生过任何 kill**：`members.json` 21 个成员 `killed=false`、`phase=RUNNING`，全 transcript 零 `swarm{action:"kill"}`。分工从"每题多轨"改成"每题多工序 + 多题并行"，协同靠 board 广播而非杀重开。 | [证据: 813867cb `members.json`]、[证据: 813867cb 工具计数 `swarm:kill=0`] |
| 「6 发无突破熔断」实际触发过几次 | **平台拦了 5 次，但一次都没有换人。** 5 次全部只写进 note 并"SWITCH BASELINE"（切锚点/全组查根因），没有任何新 worker 因此被派。 | [证据: 回执 `6b031766`、`6b0316a6`、`6b03220c`、`6b04194a`、`6b041e6a` 的 `[S3:CIRCUIT_BREAKER] n/6`] |
| 跨题复用手法 | 找到 3 条完整链路，其中**2 条是"发现→广播→跨题采纳→被线上证据推翻→全队回滚"**，同一失败模式（本地 msprof 掩盖 DVFS 冷启）在一天内犯了两次。 | 见 §3.1 |
| 多轨赛马是真跑过还是只写在文档里 | **真跑过**：`origin/exp-b` 在 7 个题目仓库留存、`origin/exp-carbon` 在 8 个，`b-track1`/`b-track2` 在 4 个、`a-s-elf`/`a-ml-pipe` 在 2 个；至少 7 发官方回执的 note 直接带 `[exp-b]`。但**收官只并了一半**：CTCLossV2 与 ConfusionTransposeD 的历史最高分发在赛道分支上，至今不在 `main`。 | 见 §3.3 |
| 11-22 单日 117 发是怎么来的 | **两个账号槽 + 北京时间跨日重置**（`bj_day_start` = 16:00Z），不是三个槽。11-22 槽 1 在 15:11 报 `50/50 exhausted`，16:11 变成 `4/50`；两个配额日各不超过 100。 | [证据: 95eafe6a#1342]、[证据: 95eafe6a#1484]、[证据: `cli/opsb_core/daemon/state_cache.py:505-521`] |

> **对交办方给的"3 个账号槽 28/39/50"的更正**：按 `submit-queue-outcome.jsonl` 原始行去重后确实是 28/39/50 三组，但 `<blank>` 那 28 组全部落在 `2026-11-22T10:36–12:54` 之间，而 `account_label` 字段的**第一条记录**出现在 `12:54:42`（[证据: `results/submit-queue-outcome.jsonl` 字段首次出现时间]）。即那 28 条不是第三个槽，而是**加字段之前的历史行**。旁证：13:48 的槽状态是 `槽1 used=38 + 槽2 used=7 = 45`，与当时去重累计 47 基本吻合，说明这 28 条已分摊在两个槽里（[证据: 95eafe6a#1255]）。**11-22 真正发生的是 12:54 前后新增第二个账号槽**（用户 12:31 才提出"我有双账号、三账号呢"，[证据: 95eafe6a#842]），**加中途重置**，两者叠加才有 117。

---

## 1. 怎么拆任务

### 1.1 三场对照

| | 11-18 `cacd27d9` | 11-22 `95eafe6a` | 11-23 `813867cb` |
|---|---|---|---|
| 同时活着的刷分 worker | 10（题数 × 1） | 10（Track A）→ +5+1+1（Track B）→ +8 `opus` → +8 `carbon` | 20 |
| 切割维度 | **按题** | **按题 × 轨道（main / exp-b / exp-carbon）** | **按题 × 工序** + **按题 × 双轨** |
| 每题的席位 | 1 | 最多 3（Track A 主仓 + `exp-b` + `exp-carbon`；只有部分题集齐） | PadV3Grad / StridedSliceGrad：5（recon→s-elf / ml-pipe→int）；ConcatD / CTCLossV2 / MultilabelMarginLoss：3（arch + t1 + t2） |
| 平台侧 worker | 2（调度器 + macOS app） | ～30（realtime-ui、consolidation、v3 四轨、5 轮 bugfix、审计） | 1（v3-sentinel，只读观察） |
| 派活载体 | `invoke_subagent` 直投，无 board | board `swarm add`（16 条 Track-C brief）+ `invoke_subagent`（Track B）+ 16 条平台 brief | board `swarm add`（20 条 brief，全部走 board） |
| 单条派活长度 | 1 357–1 562 字符 | 刷分 725–1 028；平台 1 291–9 096 | 1 100–3 051，中位 1 286 |
| 主控→worker 广播 | 0（全部 1:1 `send_message`，52 次） | 10 次 `post_message` | 56 次 `post_message`，**0 次 `send_message`** |

- 11-18 是"一题一个长驻工人"，用户原话就是「开 10 个 subagent 去做每一道题，目标全部 70 分」[证据: cacd27d9#0]。
- 11-22 的分轨是**用户下的指令**：「把所有没进前 50 的题目开两个 subagent…可以用 main + git worktree 控制版本，依旧沿用短期最近 6 发无突破即重头换方向」[证据: 95eafe6a#1028]。主控把"两个"扩成三批（Track B `exp-b` 13:02、Track C `exp-opus` 15:14、Track C `exp-carbon` 15:17）。
- 11-23 的五段工序是**用户在上一场末尾设计好的**：「单题精进-多 agent」+「每个关键环节都可以开一个 agent」+「B 组的意思是原来 worktree 那种形式」[证据: 95eafe6a#2210、#2214、#2216]，由下一场主控落成 `plan.json` 的 `t-a-pad` / `t-b-mml` 等 thread（[证据: 813867cb `plan.json`]）。

**判断**：三场里唯一由主控自己发明的切割方式是 `Group A · Split-by-Stage`（recon → s-elf / ml-pipe → int）。其余两个维度（按题、按 worktree 双轨）都是用户给的。

### 1.2 派活说明：必含字段与长度

最短的一条（1 100 字符，`@ssg-recon`）已经把契约写全：

> `Group A (Split-by-Stage) · StridedSliceGrad · Stage ① Recon & PMU vs torch.`
> `STRICT CONSTRAINTS: - READ-ONLY: do NOT edit any source files or commit/push…`
> [证据: 813867cb board seq 6]

最长的一条（3 051 字符，`@cat-arch`）把"天马行空"具体化为 4 个硬件假设 + 2 个交付物：

> `2. Inspect actual CANN 9.1.0 SDK headers on ssh npu (read-only!) … for exact L0 DMA/Vector instructions`
> `4. Think WILD & BOLD ("天马行空"): propose 3-5 radical, hardware-grounded ideas`
> [证据: 813867cb board seq 90]

三场的**必含字段**（按出现率统计）：

| 字段 | 11-18 | 11-22 | 11-23 |
|---|---|---|---|
| 独占目录/分支（防串改） | ✅ "Touch ONLY `ict-cann-nov/PadV3Grad`" | ✅ "独占目录 `.worktrees/PadV3Grad-opus`（分支 `exp-opus`）…严禁触碰" | ✅ 同（`.worktrees/a-s-elf`） |
| 测试与提交命令 | ✅ `tools/npu-test.py` + `tools/submit-queue.py enqueue` | ✅ `opsb test` → `opsb submit` | ✅ `opsb test <Title> --sha` + `parhelion` 工具名 |
| 目标分 | ✅ "Reach **70.0**" | ✅ "冲刺 `68~76+`（Top1=76.86）" | ✅ "headroom +85.29 weighted pts" |
| 关键情报（为什么现在打） | ⚠️ 只给"读 AGENTS.md/DESIGN.md" | ✅ 给出具体病根（`.o` 172KB、5 公开 5.3–5.6us） | ✅ 给出 strata 权重与 +corr 桶 |
| 汇报方式 | "Report back with your before/after msprof table" | "有重大突破后在板上向 @coordinator 汇报" | "Post a concise, data-backed Stage ① blueprint to @…"（指名收件人） |
| 熔断/换方向条件 | ❌ 无 | ✅ "连续 6 发无大突破即彻底换方向" | ✅ 以 round 号 + GREENLIGHT 代替 |
| 只读/写边界 | 只限目录 | 只限目录 | ✅ 显式 `READ-ONLY`（recon/arch 两个席位） |

[证据: cacd27d9#95 的 PadV3Grad 派活原文]、[证据: 95eafe6a#1355 的 8 条 `exp-opus` brief]、[证据: 813867cb board seq 2/6/90]

**11-23 的两处升级值得抄**：一是把"只读研究"单独设成一个席位（`-recon` / `-arch`），明确禁 `opsb test`，产出是 blueprint + 档案 md；二是**指名下游收件人**（recon 的交付必须投给 `-s-elf` / `-ml-pipe` / `-int`），工序之间靠 board 定向投递而不是主控转述。

### 1.3 追加指令：时机、长度、性质

- 11-22 board 共 55 条：32 条 brief + 23 条非 brief（**主控 10 条**广播 + 13 条 worker 的 `[done]` 汇报）。主控那 10 条长度 268–3 108 字符，**没有一条是"催进度"**，全部是"把某题刚拿到的线上事实转成别人的行动项"（seq 17–21、37、44、51 等）。
- 11-23 board 675 条里，coordinator 只发了 76 条（56 条广播 + 20 条 brief），其余 **599 条是 worker 互投**；广播长度中位 1 083 字符，≥2 000 字符的 10 条全部是"多题合并战报 + 分派指令"（[证据: 813867cb step 1439/947/898]）。
- **加压型指令极少，纠偏/改向型占多数**：17:04 那条"收缩并发"一条令终止 14 个 worker（7 个 carbon + 7 个题内/Track-B worker，只留 `carbon-padv3grad`）（[证据: 95eafe6a#1558、#1560]）；11-23 则是"ROUND-N GREENLIGHT"式推进，一条广播同时完成"结论 + 下一轮任务 + 约束"。
- 唯一反复出现的"加压"是**同题串行化**：`SAME-PROBLEM opsb test SERIALIZATION RULE`，因为 `opsb test` 的远端构建目录 `…/ict-cann-nov/<Title>/` 是共享的，两个同题 worker 同时跑会互相覆盖 `build/`（[证据: 813867cb board seq 32]）。这条规则在 11-22 不存在——11-22 的 worktree 隔离只隔离了源码，没隔离远端构建目录。

---

## 2. 怎么监控、怎么停、怎么换人

### 2.1 主控看什么信号

| 信号源 | 11-18 | 11-22 | 11-23 |
|---|---|---|---|
| worker 主动推 SYSTEM_MESSAGE | 主（靠 `[done]` 长报告） | 主（86 条 `send_message` 多为回信） | 辅（board 已承载） |
| 自己的 `run_command` 脚本查库 | 129 次 | **419 次**（写 Python 直接读 `submit-queue-outcome.jsonl`/`opsb status --json`） | 118 次 |
| `read_board` 拉增量 | 0 | 20 次 | **242 次** |
| `swarm_wait` 阻塞等变化 | 0 | 9 次 | **196 次** |

[证据: 三场 transcript 的工具调用计数]

11-22 的主控是一个"手写脚本的调度员"：419 次 `run_command` 里大量是 `python3 - <<PY` 直接读队列文件、算 WSPT、跑 `fetch_team_ranking`。11-23 变成"读板的调度员"：`swarm_wait` 196 次 + `read_board` 242 次，`run_command` 降到 118 次。

**判断**：`members.json` 的 `phase` / `desired_state` 三场都**没有**被主控当作决策依据——两场里主控从没读过 `members.json`，它的等价物是 `swarm{action:"status"}`（11-22 用了 8 次）和 `swarm_wait` 的返回值。真正的介入触发是**board 上的 worker 发言 + 用户输入**。

### 2.2 `opus-*` 8 个在 15:15 全灭：真因是模型不可用

时间线（UTC）：

| 时刻 | 事件 | 证据 |
|---|---|---|
| 15:13:04 | 用户："都开一个 opus xhigh 的 subagent 在努力努力" | 95eafe6a#1346 |
| 15:14:10 | 主控 `swarm add` ×8，`model=claude-opus-5-5-high`，worktree `.worktrees/<Title>-opus`，分支 `exp-opus` | 95eafe6a#1355 |
| 15:15:04–15:15:16 | 8 条系统错误，逐条一字不差：`Your previous response was blocked by content safety filters / Output retries exhausted after 4 attempts` | 95eafe6a#1365–#1374 |
| 15:17:02 | 用户："清理掉这 8 个 Track-C worker，换成 carbon 最高思考强度" | 95eafe6a#1378 |
| 15:17:02 | 主控 `swarm kill` ×8 | 95eafe6a#1379 |
| 15:17:38 | 主控 `swarm add` ×8，`gemini-4-carbon-high`，`.worktrees/<Title>-carbon` | 95eafe6a#1389 |

结论：**不是预算、不是主控判断、更不是"carbon 更强"**。8 个 worker 从未真正开始干活（`members.json` 里它们的 `last_read_seq` 全部停在 8，即 board 上 opus brief 的最后一条；carbon 的停在 22–25）。主控在 #1370 的 thinking 里已经猜到"content filtering"，但当时代码层的处理是**换模型重开**，而不是修 prompt 或修 provider 路由。

同一故障在 11-23 复现两次：`opus-v3-auditor`（[证据: 95eafe6a#2056]）和 `opus-frontend-audit`/`opus-backend-audit`（[证据: 95eafe6a#2495/#2496]），后者是用户 12:15 才要求的"派 2 个 opus agent 审计前端/后端"，**30 秒内全灭**；用户没有改口，而是在下一条消息里继续坚持"B（必须用 Opus）"，只是换成另开一个新文件夹再要提示词（[证据: 95eafe6a#2501]）。

**这是两天里最贵的一次编排损失**：11-22 那次烧掉 8 个 worktree 的创建/回收 + 一轮 brief，【推断】净损失 3–5 分钟；11-23 那次落在收尾审计窗口，直接导致审计没做成。

### 2.3 `carbon-*` 16:57–17:55 陆续 killed：收工，不是换血

- 17:04:33 用户："收缩并发，把剩下 17% 额度全打在刀刃上"（[证据: 95eafe6a#1558]）。主控同一秒杀 7 个 carbon，thinking 里写"the remaining carbon workers are terminated to reduce token consumption"（[证据: 95eafe6a#1560]），并重写 `plan.json` 只留 `carbon-padv3grad` + `hydrogen-realtime-ui`（[证据: 95eafe6a#1560 的 `write_file`]）。
- 17:55:46 用户："差不多收手吧，我关服务器了"（[证据: 95eafe6a#1620]）。主控 `kill_all` + 杀最后一个 carbon + 跑收官对账（[证据: 95eafe6a#1622]）。

**这次"留刀刃"的选择没有兑现**，而且**杀掉 worker 并不等于杀掉在途工作**：

- 被留下的 `carbon-padv3grad` 在 17:04–17:55 之间**没有产出任何新高**：它这一支最后的官方回执是 16:41 的 `97f0796`（48.66），还低于 16:05 的 `37fd777`（51.54）[证据: 回执 `6b031ac9`、`6b031254`]。
- 17:04 之后仍在落分的 3 发：SyncBN `9c943d3` 72.26 @17:07、ConcatD `474dc59` 76.10 @17:22、StridedSliceGrad `ba4a357` 71.57 @17:47。这三题的席位（`carbon-syncbn` / `carbon-concatd` / `carbon-stridedslice`，以及 `3d7f1d11` 等 7 个题内 worker）都在 17:04:33 被同时终止；**席位死了，在途判题照常出分**。【推断】这三发是终止前已入队的候选。
- 被砍掉的 7 个里，QuantUpdateScatter（86.61，最终全队最高）、SyncBN（72.95）、ConcatD（77.96）都是当日收盘历史最高分题，**它们在 17:04 之后就再没有推进**。

[证据: `results/submit-queue-outcome.jsonl` 的 `finished_at` 与 `sha`；`members.json` 中 carbon-* 的 `desired_state=KILLED`]

**对 4.3 的含义**：`parhelion_agent_stop` 之后判题回执仍会异步到达（AGENTS.md 第 3 条已写明"最终判题异步送回提交该 SHA 的 worker"），但**席位已死时回执没有收件人**。实战里主控只能在 17:56 的收官脚本里从 `outcome` 文件反查历史最高分（[证据: 95eafe6a#1622 的收官对账脚本，输出含"可收官恢复 <sha>"一栏]）。4.3 需要一个"席位已停 → 回执落到主控/park 池"的兜底。

### 2.4 11-23 为什么一个都没换

- `members.json`：21 个成员全部 `desired_state=RUNNING`、`phase=RUNNING`、`killed=false`，包括 `wait_since` 只到 599 的 `ssg-arch`。
- transcript 里 `swarm{action:"kill"}` 计数为 0，`manage_subagents` 计数为 0（[证据: 813867cb 工具计数]）。
- 原因是**分工形态变了**：11-22 的"多轨"是同一个目标的冗余副本（三条轨都追同一题的高分），必须靠"杀弱留强"收敛；11-23 的"多轨"是**正交分工**——Group A 的四道工序不重叠（recon 只读、s-elf 管 S 桶、ml-pipe 管 M/L 桶、int 管合入），Group B 的两条轨明确"100% ORTHOGONAL"（[证据: 813867cb board seq 608：`100% ORTHOGONAL TO @ctc-t2's SCH_MODE_3`]）。
- 因此"不换人"不是因为这一场更宽容，而是因为**失败被设计成继承而不是淘汰**：弱轨的产出不丢弃，而是被 `-int` 做 Pareto 并集（`ConcatD Round-4 Pareto Union`，[证据: 813867cb board seq 612]）。

**代价**：这一场 20 个 worker 各自持锁跑 `opsb test`，同题串行规则只能靠 board 喊话+自觉（`[testing <Title> <SHA>]` / `[done …]`），而且 `phase_a` 编译时间被并发拖长（`ConcatD phase_a=49.25s`、`PadV3Grad die=23.7–26.8s` vs 11-22 的 `die≈10.7–15.2s`）。**[推断]** 这是 11-23 每发回执的边际成本高于 11-22 的主因之一（见 §4.1、§4.3）。

### 2.5 与 PERF-EXP「6 发无突破熔断」对照

规则原文：

> `G3 Circuit Breaker`：**12 小时 / 6 发无突破自动熔断**——强迫停止微调，转向 PMU + 反演桶诊断做架构重写
> [证据: `docs/multi-agent-workflow.md:40`]、[证据: `PERF-EXP.md:27`]

平台实现：`circuit_breaker_limit: 6`（[证据: `cli/opsb_core/config.py:141`]）、`queue_service.py:105`、`contest_service.py:169`（"12h circuit-breaker state"）。

实际触发（全部 5 条都在回执 note 里留痕）：

| 时间 | 题 | SHA | note |
|---|---|---|---|
| 11-22 16:27 | StridedSliceGrad | `e635a6c` | `[S3:CIRCUIT_BREAKER] 6/6 consecutive non-improving shots in last 12h — SWITCH BASELINE` |
| 11-22 16:27 | StridedSliceGrad | `121c9c4` | 同上 |
| 11-22 17:19 | StridedSliceGrad | `dfa784f` | `9/6 …` |
| 11-23 10:47 | MultilabelMarginLoss | `ecf4af4` | `6/6 …`（该发 74.32，是新高） |
| 11-23 11:09 | MultilabelMarginLoss | `e9cdd3b` | `8/6 …` |

[证据: `results/submit-queue-outcome.jsonl` 中含 `CIRCUIT_BREAKER` 的 5 行]

**结论：规则写了，也确实触发了 5 次，但一次都没有换人。**

- 11-22 那三次触发**没有引起任何调度动作**：`e635a6c`(Pass)、`121c9c4`(Pass)、`dfa784f`(Wrong Answer) 都照常走完，负责 StridedSliceGrad 的 `3d7f1d11`（Optimizer）与 `a42d5e8e`（Track-B）一直活到 17:04 才被"额度收缩"杀掉，与熔断无关。
- 11-23 那次触发的真实反应是**全员查根因 + 切锚点**：主控广播"…(`CIRCUIT OPEN` anyway) until `@mml-arch` + `@mml-t1` + `@mml-t2` do an exact root-cause diff of `13c554d` (`75.54`) vs `8ebf08e`"（[证据: 813867cb board seq 416 / step 667]），并把 `pairwise_anchor` 的 anchor 改回历史峰值。`mml-t1/t2` 此后一直在同一批 worker 手里。
- 真正发生的 3 次换人，原因都**不是**熔断：
  1. 14:24:49 + 14:43:59 换掉 PRelu/QuantUpdateScatter/LayerNorm 的 Track-B → 动机是"**都开到 xhigh 思考强度**"和"**subagent pro 竟然是 H 模型，请你改为 inherit**"（[证据: 95eafe6a#1257、#1293、#1267、#1297]）；
  2. 15:17 opus → carbon：模型故障（§2.2）；
  3. 17:04 carbon 收缩：额度（§2.3）。

**这一点对 4.3 很关键**：平台把熔断实现成**一个门（gate）**，而文档想要的是**一个调度动作（换人）**。两者之间没有接线——`queue_service.py` 只把 `circuit_open` 变成 `circuit_ok=False` 让提交被拒/被标记，没有任何代码路径去 `spawn_agent`。AGENTS.md 第 6 条却明确写了"由新的 worker 从正交方向接手"（[证据: `AGENTS.md:12`]）。**规则存在，执行器不存在。**

---

## 3. 信息流

### 3.1 三条完整链路

#### 链路 A：`SetBlockDim(halfCores)` —— 跨题广播了，然后被线上证据推翻

| 环节 | 谁 | 载体 |
|---|---|---|
| 发现 | `@ssg-s-elf`（StridedSliceGrad Stage ②） | board seq 117：`SetBlockDim(halfCores)`（20）让 6 个 S 桶本地 2.0–2.96× |
| 广播 | 主控 | board seq 120 = 主控 step 146：`@all MASSIVE cross-op hardware discovery by @ssg-s-elf (#117)!` |
| 采纳 1 | `@cat-arch` | board seq 119（同一分钟，直接读 #117）→ 说这"directly solves tiny32 双峰" |
| 采纳 2 | `@pad-s-elf` | board seq 209/215：`b399ced` 以 "true `1c`" 名义落地 |
| 线上证伪 | `@ssg-s-elf` 自己 | board seq 200：`[CRITICAL ONLINE-JUDGE POST-MORTEM]` `734e097` = 59.74 vs `ba4a357` = 71.57（**-11.83**） |
| 回滚 | 主控 | board seq 201 `@all CRITICAL ONLINE-JUDGE REALITY CHECK`；seq 255 `ZERO dummy halfCores` |

[证据: 813867cb board seq 117/119/120/200/201/255]（`734e097` 在 `submit-queue-outcome.jsonl` 里是两行 `sync_error`、无 score，59.74 这个数字来自 `@ssg-s-elf` 与主控的对账发言本身，不是回执字段）

#### 链路 B：`AscendC::PreLoad(N)` —— 发现→跨题采纳→线上剂量反应→20 分钟全队回滚

| 环节 | 谁 | 载体 |
|---|---|---|
| 首次线索 | `@ssg-s-elf` | board seq 268（04:52）：`PreLoad(2048) caused a …` 的 A/B |
| 跨题转述 | `@pad-arch` | board seq 269（04:53）：`Quick heads-up from #268 (@ssg-s-elf A/B on f8b471c)` |
| 局部"定律" | `@ssg-s-elf` | board seq 316（05:08）：`VERIFIED SILICON HARDWARE DISCOVERY — PreLoad(N)`，给"1c 用 `ceil(entryBytes/2048)`、≥2c 零 PreLoad"的 SWARM RULE |
| 三题同时采纳 | `@cat-arch` seq 317、`@ctc-arch` seq 318、`@pad-s-elf` seq 321 | 各自 +0.3–0.7us 本地增益 |
| 线上证伪（剂量反应） | `@pad-recon` | board seq 365：5 发线上 PadV3Grad `avg_pub` 单调变差（4.880 → 5.754us），`score` 54.86 → 46.96 |
| 全队回滚 | 主控 | board seq 368 = 主控 step 548：`@all STRIP PreLoad(N) EVERYWHERE` |
| 执行（5 题 20 分钟内） | ssg-int seq 384、ctc-t1 seq 385、pad-int seq 388、mml-t2 seq 389、ctc-t2 seq 391、cat-t2 seq 398、mml-t1 seq 402 | 每条都写 `ZERO PreLoad` |

[证据: 813867cb board seq 268/269/316/317/318/321/365/368/384/385/388/389/391/398/402]

**这条链和链路 A 是同一个失败模式**：本地 `msprof` 有 warmup、掩盖了 Die-0 冷唤醒；两次都因此把"本地赢、线上输"当成硬件定律广播出去。主控自己在 seq 255 总结过："dummy `SetBlockDim(halfCores)`… only masks local idle-Die-0 cold-wake latency while strictly hurting online-judge latency"（[证据: 813867cb board seq 255]、[证据: seq 368 的同款措辞]）。**教训是新知识在拿到线上回执前必须降级为"假设"，而 board 没有这个字段。**

#### 链路 C：`64B` tiling / 去 `GET_TILING_DATA_WITH_STRUCT` 栈拷贝（11-18 → 11-22）

| 环节 | 谁 | 载体 |
|---|---|---|
| 发现（原题 ConcatD） | 主控汇总 ConcatD worker 的报告 | 11-18 step 181/189：`coreStart[40][2]` 把 `ConcatDTilingData` 从 `1064 B` 撑到 `1704 B`，每发都在传 |
| 沉淀 | 主控 + breakthrough 库 | 写进 `PERF-EXP.md` 杀手锏 #2，同一行同时记着两端：`StridedSliceGrad 3267c09` 与 `ConcatD 7919dcb（1704B → 64B）`（[证据: `PERF-EXP.md:38`]） |
| 移植（目标题 StridedSliceGrad） | `@…exp-b` Track-B 架构师（95eafe6a，worktree `StridedSliceGrad-expB`） | 提交 `3267c09`，note：`[exp-b v11] 64B direct-GM tiling (zero 184B GET_TILING_DATA_WITH_STRUCT copy) + deduplicated Load1DPadCold/Store1DPadCold` |
| 传播 | 主控 | board seq 21：`@all 全队核弹级通用架构突破 · 立即借鉴`，给出 3 招（64B 头 + `noinline` 冷路径 + 3KB Micro-Entry） |
| 再沉淀 | 主控自己的脚本/文档 | 11-23 step 2249：`Summary: Pack hot tiling scalars in the first 64B cacheline, trim H2D payload via SetDataSize(64)…` |

[证据: cacd27d9#181、#189]、[证据: 回执 `6b03156d85ad43bbe06eb64d`（`3267c09` Pass 65.15）]、[证据: 95eafe6a board seq 21]、[证据: `PERF-EXP.md:38`]

**这条链的价值在于它走了"完整一圈"**：题目内的发现 → 进共享经验库 → 另一题由不同 worker 移植并拿到官方回执 → 再回写经验库。它的传递介质不是 board，而是**共享文档 + worktree 里的既有代码**。

### 3.2 共享文档在哪里汇聚，谁写的，什么时候被引用

| 文件 | mtime | 作者 | 后续引用 |
|---|---|---|---|
| `skills/CATALOG.md` | 11-22 14:12 | `hydrogen-consolidation`（board seq 26 派活 14:06 → seq 27 交付 14:22） | 11-22 开局主控第一步就读它 [证据: 95eafe6a#1] |
| `docs/debugging-and-escape-postmortems.md` | 11-22 14:19 | 同上 | 被 `docs/README.md` 索引 |
| `docs/multi-agent-workflow.md`、`local-bench-methodology.md`、`ascendc-optimization-patterns.md`、`parhelion-architecture.md`、`README.md` | 11-22 22:44 | `hydrogen-consolidation` 起草，**主控在 11-23 03:44 用一段 Python 批量重写过**（[证据: 95eafe6a#1828 的 `targets = [AGENTS.md, PERF-EXP.md, …]`]） | 下一场 worker brief 里被点名读 |
| `PERF-EXP.md` | 11-22 22:52 | 同上 + 主控 11-23 03:51 补"实证战绩"段（[证据: 95eafe6a#1929 两条 `edit_file`]） | 11-23 的 `-arch` 席位被要求写 `~/.parhelion/logs/v4-arch-notes/<Title>.md`，格式对齐它 |
| `skills/*`（20 个技能） | 11-22 14:12 目录 | 10-11 月陆续积累 | 11-22 brief 里直接点名 `skills/ascendc-perf-optimize` |

补充证据：`ops-bench` 根目录**不是 git 仓库**（`git log` 返回 128），所以这些文档没有提交历史，只有 mtime；文档的"谁写的"只能从 board 的 `[done]` 报告反推（[证据: 95eafe6a board seq 27 的交付清单]）。

**没有 `scratch/` 目录**。三场的中间产物落在三个地方：`ops-bench/ops-supervisor/`（平台代码）、`~/.parhelion/logs/v4-arch-notes/<Title>.md`（11-23 的架构档案）、各题仓库的 `.worktrees/`（已被 `git worktree remove` 清掉，见 §3.3）。

### 3.3 Multi-Track 赛马：**真跑过**，但收官只并了一半

**跑过的物证**（工作树已被清理，但分支还在远端）：

| 布局 | 分支 | 存在于哪些仓库 | 时期 |
|---|---|---|---|
| `ops-bench/.worktrees/<Title>-expB` | `exp-b` (=`origin/exp-b`) | 7 题（ConcatD、ConfusionTransposeD、LayerNormV3、PReluGradUpdate、PadV3Grad、QuantUpdateScatter、StridedSliceGrad） | 11-22 13:02Z 起 |
| `ops-bench/.worktrees/<Title>-opus` / `-carbon` | `exp-opus` / `exp-carbon` | `exp-carbon` 在 8 题（`exp-opus` 未留下远端分支，与 §2.2 全灭一致） | 11-22 15:14Z / 15:17Z |
| `ict-cann-nov/<Title>/.worktrees/b-track1` / `b-track2` | `b-track1` / `b-track2` | CTCLossV2、ConcatD、ConfusionTransposeD、MultilabelMarginLoss | 11-23 02:38 本地 |
| `ict-cann-nov/<Title>/.worktrees/a-s-elf` / `a-ml-pipe` | `a-s-elf` / `a-ml-pipe` | PadV3Grad、StridedSliceGrad | 11-23 02:38 本地 |

[证据: `git -C ict-cann-nov/<Title> branch -a`]、[证据: 813867cb board seq 3/4/10–15 的 brief 里写明 `branch: a-s-elf` / `b-track1, seeded from main/best 9f92b0a`]

**真的出了分**：至少 7 发官方回执的 note 直接带 `[exp-b]` 标签，且都是当时的题内新高——

- `ConcatD 4832524` = **75.60**（11-22 15:15，`[exp-b] v8: zero-index TransDataTo5HD<u16> ZIP2…`）
- `PadV3Grad d13d44d` = **43.62**（11-22 15:16）、`be05c5b` 42.45、`97f0796` = **48.66**
- `StridedSliceGrad 8f498a9` = 61.71、`3267c09` = 65.15、`ba4a357` = **71.57**（11-22 17:47，当日收盘最高）

[证据: 回执 `6b03061e`（ConcatD 75.60）、`6b0306dd`（PadV3Grad 43.62）、`6b030be5`（be05c5b 42.45）、`6b031ac9`（97f0796 48.66）、`6b03040a`（8f498a9 61.71）、`6b03156d`（3267c09 65.15）、`6b0328be`（ba4a357 71.57）]

**最终保留了哪条轨**（按"历史最高分 SHA 在哪些分支上"判定）：

| 题 | 最终最高分 SHA | 所在分支 | 是否已并 main |
|---|---|---|---|
| PadV3Grad | `b6600e1` 59.98 | `a-s-elf` + `main` | ✅ |
| ConcatD | `474dc59` 77.96 | `b-track1` + `b-track2` + `main` | ✅ |
| MultilabelMarginLoss | `13c554d` 75.54 | 双轨 + `main` | ✅ |
| LayerNorm / QuantUpdateScatter / PReluGrad / SyncBN | `9773dff` / `0aa17ab` / `11bd996` / `9c943d3` | `main` | ✅ |
| StridedSliceGrad | `a031e4e` 72.45 | `main` | ✅ |
| **ConfusionTransposeD** | `235f754` **80.07** | **`b-track1` only** | ❌ |
| **CTCLossV2** | `dcc7754` **75.11** | **`b-track2` only** | ❌ |

[证据: `git -C <repo> branch -a --contains <sha>`；`git -C ConfusionTransposeD log --oneline main..b-track1` 有 2 个未并提交，`git -C CTCLossV2 log --oneline main..b-track2` 有 8 个未并提交]

也就是说：**多轨赛马真跑过，也确实产出了 2 个只存在于赛道分支的历史最高分（ConfusionTransposeD 80.07 在 `b-track1`、CTCLossV2 75.11 在 `b-track2`），至今没回 main**；PadV3Grad 的 59.98 虽出自 `a-s-elf`，但已经并进 main。CTCLossV2 的 `main` HEAD 是 `a1df9d5`（T2-V13，线上 Runtime Error，0 分），`ConfusionTransposeD` 的 `main` HEAD 是一个 `.gitignore` chore 提交。这两发都是 12:33/12:37 判出来的——**在用户 12:48「收个尾吧」之后**，所以是"赢了但没人收"。

**结论**：`docs/multi-agent-workflow.md` 四·2 写的"胜出分支合入 `main`"（[证据: `docs/multi-agent-workflow.md:49`]，另见 §二.1 的"一仓一主 Agent"在 :26 已被实战突破为"一 worktree 一 Agent"）**是人工执行的，而且漏了两题**。这不是文档写得不对，是**没有自动对账**：没有任何机制在收盘时比较"每题历史最高分 SHA"与"该题 `main` HEAD"。

---

## 4. 中后期效率

### 4.1 每小时判题数（`finished_at` 直方图，去重 233 条）

| 日期 | 总数 | 小时分布 | 峰值 |
|---|---:|---|---:|
| 11-10 | 39 | 00:2 01:2 02:3 03:5 04:8 05:3 06:2 07:4 08:5 09:2 10:1 11:2 | 8/h |
| 11-18 | 20 | 04:2 05:1 06:4 07:1 08:4 09:3 10:5 | 5/h |
| **11-22** | **117** | 10:2 11:10 12:21 13:14 14:23 15:13 16:21 17:9 19:4 | **23/h** |
| **11-23** | **55** | 08:2 09:6 10:19 11:16 12:12 | **19/h** |

[证据: `/tmp/p19/receipts.json` 的 `finished` 字段分组]

11-22 的 117 发有两个结构原因：账号槽从 1 个加到 2 个（12:54 后 outcome 行里才出现第二个槽的标签），以及**北京时间跨日**（16:00Z 配额清零，槽 1 在 15:11 `50/50 exhausted` → 16:11 `4/50`）。**两个配额日各不超过 100，没有任何绕过上限的操作。**

**对 4.3 的含义（复核交办方的推论）**：4.3 的额度计算**已经是按槽求和而非硬编码单槽**——`state_cache.py:836` 的 `team_daily_limit` 是 `sum(s["daily_limit"] for s in eligible_slots)`，`queue_service.py:190` 与 `ipc_bridge.py:86` 同样；`used_today` 的窗口按 `bj_day_start_ep`（北京时间零点 = 16:00Z）切（`state_cache.py:505-521`）。所以"中途加槽"在**计量层**不会卡住，卡住的是**动作层**：`parhelion_sprint_start` 只有 `enable_auto_submit` 一个与账号有关的开关（`sprint.py:46-48`），没有"新增/启用一个账号槽"的入口，加槽仍要回到 Parhelion GUI 的设置里手工导入。实战里正是这么做的：用户 12:31 才发现"我有双账号、三账号呢"并让主控加设置项（[证据: 95eafe6a#842、#1006]），13:48 槽状态才变成两个（[证据: 95eafe6a#1255]）。**建议**：加 `parhelion_sprint_adjust(accounts=[...])` 或至少在 `sprint_start` 的回执里把"本次冲刺可用槽数与各自 `remaining`"一并返回，否则 worker 与主控都不知道 117 这种吞吐还有多少余量。

### 4.2 主控介入频次

| | 11-18 | 11-22（刷分窗口 10:00–18:00Z） | 11-23 |
|---|---:|---:|---:|
| 主控工具调用 / 小时 | 42 | 122 | 116 |
| 用户输入 / 小时 | 2.8 | 6.4 | 3.6 |
| 每次用户输入覆盖的判题数 | 1.2 | 2.3 | 2.9 |
| `send_message`（1:1） | 52 | 86（多为回信） | **0** |
| `post_message`（广播） | 0 | 10 | 56 |
| 派活（`swarm add` + `invoke_subagent`） | 19 | 54（含 3 批 Track-C 重开） | 20 |
| 叫停/席位操作 | 22 次 `manage_subagents`（list/revive 混计） | 31 次 `swarm kill` + 4 次 `manage_subagents`（含 2 次 `kill_all`） | **0** |

[证据: 三场 transcript 工具计数 + `USER_INPUT` 时间戳]

**读法**：11-23 的主控工具调用密度和 11-22 相当（116 vs 122 /h），但**用户只在开局、3 个"进度如何"、2 个 v3 bug、1 个收尾共 19 次发话**，而 11-22 用户 96 次发话里有一多半是在指挥调度（开几路、换什么模型、收缩并发、关服务器）。**11-23 的效率提升主要来自"用户不再当调度器"**，不是来自主控更勤快。

### 4.3 高效率来自哪里

按贡献排序：

1. **并行度与席位形态**（最大）。11-22 峰值 23 发/h 出现在 14:00 那一小时，当时活着的是 10 个 Track-A 题目 worker + 6 个 Track-B 架构师（[证据: 95eafe6a#1042 派 5 个、#1101 补 LayerNormV3，对照 #1267 之前的 kill 记录]）；11-23 峰值 19 发/h 出现在 10:00，20 个 worker 全开。而 11-18 只有 10 个 worker、5 发/h——**并发数 ≈ 判题吞吐**。
2. **派活质量（第二）**：11-23 的 brief 把"headroom 来自哪个 strata"写进去了（`S_SMALL_CORE +18.68 pts / w=0.424`，[证据: 813867cb board seq 21]），worker 第一轮就打在正确桶上；11-18 的 brief 只有"Reach 70.0"。
3. **`opsb`/MCP 自动化（第三，但是门槛项）**：11-18 还要 worker 自己 `tools/npu-test.py` + `tools/submit-queue.py enqueue`；11-22 起统一成 `opsb test` → `opsb submit`，190s 冷却、账号选择、额度、上传全部由平台持有（用户明确要求过"agents 严禁自行启动后台脚本"，[证据: cacd27d9#8 读到的 `problem-submit` skill]）。11-23 又加了 `opsb bench elf|cold3|pmu` 和 `opsb kb query`，把原来靠脚本的诊断变成一条命令。
4. **反例（拖慢项）**：11-23 每发回执的"主控步数成本"反而最高（1717 步 / 55 发 ≈ 31 步/发，11-22 刷分窗口 ≈ 14 步/发）。原因是同题并发把远端构建目录打满（`phase_a=49.25s`、`die=23.7–26.8s`，见 §2.4），以及 20 个 worker 的 board 流量（675 条 vs 55 条）。

---

## 5. 与 Parhelion 4.3 逐条对比

### 5.1 席位模式

| 维度 | 成功那场怎么做（证据） | 4.3 现在怎么做 | 差距 | 4.3 建议 |
|---|---|---|---|---|
| solo | 11-18：10 题 × 1 worker，每题独占一个仓库 [证据: cacd27d9#95] | `MODES["solo"] = ("solo",)`，`SOLO_TASK` 就是"一题一人端到端"（`sprint.py:24-29`、`12-15`） | 无实质差距 | 保持 |
| dual | 11-22：`main` + `.worktrees/<Title>-expB`（`exp-b`）[证据: 95eafe6a#1042]；11-23：`b-track1`/`b-track2` [证据: 813867cb board seq 10–15] | `MODES["dual"] = ("algorithm","dataflow")`，每个 axis 一个 `.worktrees/sprint-<axis>`（`sprint.py:41`） | ① 名字对不上：实战是"两条都想赢的冗余轨"，4.3 是"算法/数据通路"两条**正交**轨；② 路径对不上：`sprint_seats()` 只认 `path.parent.name == ".worktrees"` 且目录名恰为 `sprint-<axis>`，实战用的是 `ops-bench/.worktrees/<Title>-expB` 和 `<repo>/.worktrees/b-track1`，**两种布局都识别不了**（`sprint.py:37-43`） | 让 seat 识别按 `(problem, branch)` 而不是按目录名；增加 `mode=race`（两条同目标轨 + 显式 winner 选择）与 `mode=orthogonal`（现有 dual）两个语义 |
| 按工序分工（Group A） | 11-23：recon(只读) → s-elf / ml-pipe（并行）→ int（合入），4 个席位各有独立 brief 和交付物 [证据: 813867cb board seq 2–5 / 6–9] | **完全没有**。`DIRECTIONS` 只有 solo/algorithm/dataflow/calibration 四种 | 这是三场里最有效的分工方式，4.3 表达不出来 | 加 `mode=pipeline`，席位 `recon / <按桶拆的 N 个实现席> / integrator`；`recon` 强制只读（禁 `parhelion_test`），`integrator` 独占 `main` |
| calibration（bench-author） | 实战里存在，但**是主控自己每次临时拉一个**：`ba0f12f5 Local Bench & Calibration Architect`（16:10）、`bench_evolution.py` v2.0（11:32 上线） | `MODES["full"]` 有 `calibration` 席位，`seat_role="bench-author"`（`sprint.py:33-34`） | 4.3 比实战更正式；但实战的校准是**一次性的架构任务**（写 `bench_evolution.py`），不是常驻席位 | `calibration` 席位应区分"常驻校准者"与"一次性工具作者"，后者不该按题开一个 |
| 席位数量与题数解耦 | 11-22 的 Track-B 只铺 7 题（Track-C 铺 8 题），MultilabelMarginLoss 与 PReluGradUpdate 全程单轨 [证据: 95eafe6a#1042/#1101/#1267 的 Track-B 派活名单，对照 board seq 1–8 的 8 个 `-opus` 题名] | `start_sprint(mode=...)` 对整个 `problems` 列表套同一个 mode；只有 `problems=[...]` 能做全选/全不选（`sprint.py:54-65`） | 无法"3 题 dual + 2 题 pipeline + 5 题 solo" | 允许 `assignments` 覆盖：`[{problem, mode}]`，默认取全局 mode |

### 5.2 六发无增益换人规则

| 维度 | 成功那场怎么做（证据） | 4.3 现在怎么做 | 差距 | 4.3 建议 |
|---|---|---|---|---|
| 触发判定 | 平台算了，写进 note：`[S3:CIRCUIT_BREAKER] 8/6 consecutive non-improving shots in last 12h` [证据: 回执 `6b041e6a`] | `circuit_breaker_limit: 6`（`config.py:141`）、`queue_service.py:105`；`contest_service.py:169` 出 "12h circuit-breaker state" | 判定逻辑已具备且口径一致 | 保持 |
| 触发后的动作 | **切基线 + 全组查根因**，从不换人（§2.5） | `queue_cmd.py:93/121/130`：`circuit_ok=False` 只让提交被拒/打 `CIRCUIT OPEN` 标；`gate_view.py:98` 只换徽章文字 | **AGENTS.md:12 说"由新的 worker 从正交方向接手"，代码里没有任何 `spawn_agent` 路径挂在熔断上** | 把 `circuit_open` 变成事件：`parhelion_wait(until=[judge])` 上抛 `circuit_open` 后，`parhelion_sprint_start(mode=..., replace=[title])` 应能一键换席；或由平台自动把 `circuit_open` 写进 orchestrator inbox 并要求显式决策 |
| 交接内容 | 交战经验：11-23 的做法是"给 `-arch` 派一个 clean-slate 重扫 + 把失败 diff 写进 `~/.parhelion/logs/v4-arch-notes/<Title>.md`" [证据: 813867cb board seq 595/596] | `docs/v4.2/00-PLAN.md:84-95` 的账本（`results/ledger/<problem>/<worker>.jsonl`）+ 压缩种子 | 4.2 的账本正好补上"换人时把失败方向带走"这一环 | 4.3 只需接线：熔断触发时把该题 ledger 的 `failures` 视图塞进新 worker 的启动包 |
| 重复触发 | 同一题 11-22 触发了 3 次（6/6、6/6、9/6），每次都没有新动作 | 无去重概念（因为无动作） | — | 若接线，必须按 AGENTS.md 要求"同一批回执重放不得重复触发交接"，用 `submission_id` 集合做幂等 |

### 5.3 主控与 worker 的分工

| 维度 | 成功那场怎么做（证据） | 4.3 现在怎么做 | 差距 | 4.3 建议 |
|---|---|---|---|---|
| 谁决定提交 | worker 自己：brief 明确"全绿后 `opsb submit <Title> <sha> --note …`" [证据: 95eafe6a#1355]；11-23 主控会 `submit-now` 插队，但也是执行 worker 的候选 [证据: 813867cb board seq 515] | AGENTS.md 第 1/3 条已这样规定：worker 经 MCP 请求，平台统一入口（`AGENTS.md:7`、`AGENTS.md:9`） | 一致 | 保持 |
| 谁决定方向 | **用户**决定席位形态与是否换模型；主控决定"哪一题这一轮打哪个桶"；worker 决定具体手法 [证据: 95eafe6a#1028/#1346/#1378/#1558] | AGENTS.md 第 6 条：`主控负责例外与方向`，日常验证 worker 直接入队（`AGENTS.md:12`） | 一致，但 4.3 缺"用户下席位形态指令"的入口——`sprint_start` 是唯一入口且只接受 `mode` | `sprint_start` 之外补 `parhelion_sprint_adjust(problems=[{title, mode, seats}])`，支持中途改席位（11-22 的"收缩并发"就是这种动作） |
| 谁做复盘 | 11-23：`-recon` 席位专职做只读复盘/PMU 审计，产出 blueprint 给下游 [证据: 813867cb board seq 17/21]；11-22：主控自己写脚本做横向 CT 扫描（`.o` 体积 vs 线上 5 公例）[证据: 95eafe6a board seq 19] | 平台提供 `parhelion_pmu_card` / `bench_diagnose` / `kb_query`，但没有"复盘者"角色 | 复盘这件事在 4.3 里是"主控顺手做"，在实战里已经是**一个席位** | 与 5.1 的 `recon` 席位同解 |
| 跨题传播 | 主控广播 + worker 互投（11-23：coordinator 只发 76 条，worker 互投 599 条） | AGENTS.md 第 7 条"协作页展示…有序历史"、`parhelion_squad`（定向分享）[证据: `AGENTS.md:14`、`AGENTS.md:18`] | 4.3 只有**定向**分享，没有**广播板**；而实战里 599/675 是定向互投、两条最有价值的跨题链路（§3.1 A/B）都是**广播** | 见 5.4 |

### 5.4 主控可见性：swarm board ≈ Parhelion 的什么，缺什么

| swarm board 的能力 | 4.3 的对应物 | 差距 |
|---|---|---|
| 全队共享的 append-only 发言流（`board.jsonl`，675 条带 `seq/agent/time`） | `parhelion_agent_messages(problem=..., since_seq=...)` 按会话聚合；`parhelion_harness_pool` 看席位 | 无"全队一条流"视图。11-23 的协作形态是**worker 之间直接互投 + 主控读同一条流**，4.3 的 `agent_messages` 是主控视角的回放，worker 之间只能靠 `parhelion_squad` 点对点 |
| 增量阻塞读（`read_board(since=seq)` + `swarm_wait`） | `parhelion_wait(since=rev, until=[inbox\|test\|agent\|judge])`、`parhelion_status(if_rev=)` | **基本对齐**，4.2 工作包 B 已设计（`docs/v4.2/00-PLAN.md:72-80`）。11-23 用了 196 次 `swarm_wait`，正是 4.2 想省掉的轮询 |
| 每条发言归属到具体 worker（`agent` 字段） | `parhelion_agent_messages(agent_id=...)` | 对齐 |
| 席位状态卡（`phase` / `desired_state` / `desired_model` / `last_read_seq` / `last_nudged_seq` / `killed`） | `parhelion_harness_pool`（eligible harness + squad 分组）；`parhelion_agent_*` 无 `phase/desired_model` | **缺**。实战里主控能在同一屏看到"谁卡在 `waiting`、谁的模型被换了"，4.3 要逐个 `agent_messages` 才能推 |
| 广播一条 `@all` 即到达所有 worker 的下一回合 | `parhelion_agent_send(delivery="queue"\|"interrupt")` 是**1:1**；`parhelion_squad` 是**定向** | **缺广播**。11-23 的 coordinator `@all` 广播共 **9 条**（board seq 120 = step 146、seq 368 = step 548 等），每一次都在 20 分钟内改变了 3–5 个题的执行方向 |
| 发言无证据等级 | `kb_record` 有 `verdict=win/fail/anti-pattern` + `portability`；`kb_query` 有 pillar | **board 没有等级，KB 有**。§3.1 两条链路之所以烧掉两轮，正是因为"本地 msprof 假设"和"线上回执事实"在 board 上是同一种文本 | 建议：`parhelion_squad`/广播消息强加 `evidence: {class: local|paired|online_receipt, ref}`，`class != online_receipt` 的消息在协作页显示为"待验证"，且不允许直接转成 KB `hardware` 条目 |

**一句话**：swarm board 相当于 4.3 的 `agent_messages` + `wait` + `harness_pool` + 缺掉的那块「全员广播 + 席位状态卡 + 证据等级」的组合。前三个 4.3 已有（部分在 4.2 计划里），后三个是真缺口。

---

## 6. 最不确定的两点

1. **11-22 的"第三个账号槽"是否真的从未存在**。我的判断依据是 `account_label` 字段的首现时间（12:54:42）和槽状态行的加总吻合（`38+7=45` vs 去重累计 47）。但这只能证明"那 28 条历史行已被分摊进两个槽"，**不能 100% 排除**用户在某段时间导入过第三个账号又没有产生带标签的行。要确证需要 `~/.parhelion/` 下的账号配置文件与 `judge_quota` 历史，属于我只读范围外（且含凭据）。**报告正文按"2 槽 + 中途重置"写，若交办方坚持 3 槽，请以账号配置为准。**

2. **"多轨赛马保留哪条轨"用的是当前分支快照，不是收盘时刻的快照**。我判 `ConfusionTransposeD 235f754` 与 `CTCLossV2 dcc7754` 未并 main，依据是此刻的 `git branch --contains` 与 `git log main..b-trackN`。但 11-26 之后是否有人手工并过、或这两个仓库后来是否被其他会话动过，我无法确认（`ops-bench` 根目录无 git，各题仓库的 reflog 我没查）。另有一条相关但未证实的观察：`ConfusionTransposeD` 的 `main` HEAD 是 `5ead806 chore: unify .gitignore`，说明收盘后有人动过 main——**这会改变"未并入"的结论强度**。
