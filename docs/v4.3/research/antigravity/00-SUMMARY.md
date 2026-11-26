# P19 · Antigravity 涨分归因与 Parhelion 4.3 改进建议（总包）

> 状态：已交付。研究窗口 2026-11-10 ～ 2026-11-23，比赛 ops-bench（昇腾算子，10 题，单题 5 公开 + 45 隐藏）。
> 数据：233 条已判回执（按 `submission_id` 去重）、93 段 Antigravity 对话（3 主控 + 90 子代理）、10 个题目仓库 1,375 个提交。
> 本文件交叉核对 R1–R6，给出 4.3 候选改进；各子报告负责细节与证据，本文件只做合成与裁定。
> 口径：**裸分 = 各题官方分之和**；11-10 末 421.0 → 11-18 末 542.0 → 11-22 末 722.9 → 收尾 **752.33**（加权 1717.11）。任务书「331 分」= 752.33 − 420.97，R1 用 74 次上台阶精确闭合到 **331.36**。
> [证据: `/tmp/p19/receipts.json` 全量重算；`docs/v4.3/research/antigravity/R1-breakthroughs.md` §0.1]

---

## 0. 一句话

这场能涨，不是因为模型更会写 kernel，而是因为**每一发都被当成一次低价实验**：本地证据便宜（编译 53s、真 NPU 4.4s）、判题噪声被接受（σ≈2.3–3.0）、失败可以一发回滚（16% 的提交是 restore）、跨题的硬件规律在两小时内广播到全队。Parhelion 4.3 把这条链上的**三处**改成了闸门：提交前置门禁、连败熔断、逃逸锁；而这三处在赛场上按原样回放会拦掉 13–18/231 发（含 4 个当时新高、2 个最终最高分）、并且一次判题侧 TLE 就能永久冻结整题。**4.3 最该补的不是"更多门"，是"更便宜的试错 + 更快的回滚 + 更准的排序"。**

---

## 1. 口径与覆盖边界（先说清楚哪些结论不成立）

1. **11-10/11-11 那 41 发没有 Antigravity 记录**（最早的对话在 11-18T02:57Z），R1 只能把它们按「首发绝对分」处理。**这一段已由 R6 从 DSH 补上**：那两天跑在 `~/.dsh/sessions/--…-huawei-cann-ict--`（ops-bench 改名前的路径），482 个会话、31/31 个回执 SHA 全部命中。补上之后的结论见 §2 末与 R6；本文件的 331.36 分口径不变，仍指 11-10 之后的涨幅。
   [证据: `R1-breakthroughs.md` §0.1、附录]
2. **11-18 的 20 发只有一半落在记录窗口内**（`cacd27d9` 覆盖 02:57–08:45Z）；11-22 的 117 发与 11-23 的 55 发全程有记录。总体覆盖 184/233（79%）。
3. **判题不是源码属性**：整场有 **4 个 SHA 在源码一字未改**的情况下被判出两种判决类别（2 例 Pass→TLE、1 例 TLE→Pass、1 例 Pass→WA→Pass）；同 SHA 重判最大跨度 **15.96 分**（LayerNormV3 `ec0867d`：49.49 → 65.45）。
   [证据: `R5-open.md` §1、`R1-breakthroughs.md` §0.2、回执 `6b02dd43`/`6b02dec1`]
4. **同一二进制重判贡献 26.08 分（占 331.36 的 7.9%）**；扣除它之后，纯手法涨幅是 305.28。凡按「单发 +2 分」讲突破的结论都不可信。
   [证据: `R1-breakthroughs.md` §0.2/§2.3]
5. **`Pass` 但没拿到分数**：7 条回执 `status=Pass`、`score=None`（6 条在 StridedSliceGrad），根因是 `/ranking` 索引晚于 `get_submission`（赛后记为 `[V3-025]`）。这 6 发真实分数未知，R1 已从「同方向失败几次」的计数中剔除。
   [证据: `813867cb` 看板 board.jsonl #513/#533/#539；`R1-breakthroughs.md` §0.4]

---

## 2. 涨分主脉络（R1 的机制归因）

**11-22 单日 +180.9 分（占全部涨幅 54.6%），11-23 再 +29.5。** 但按「每发产出」排，最高的一天不是 11-22：

| 日期 | 判题 | 涨幅 | 分/判 |
|---|---:|---:|---:|
| 11-18 | 20 | +83.8 | **4.19** |
| 11-22 | 113 | +180.9 | 1.60 |
| 11-23 | 52 | +29.5 | 0.57 |
| （11-10/11-11 计入首发绝对分，与上列不可比） | | | |

11-18 的 4.19 分/判是 11-22 的 2.6 倍。那天主控干了什么：**亲手为每道题写测量台**——29 次改各题 `validation/`、11 次改 `tools/npu-test.py`，把「逐例时间 + 精度 + `.o` 体积 + 冷启」变成每发都能拿到的默认输出。R5 把它列为教训第 4 条：测量台是第一生产力，不是辅助设施。
[证据: `R5-open.md` 教训 4 与附录；`/tmp/p19/receipts.json` 按日聚合]

按源码树净差（`git diff <上一判题 SHA> <本次 SHA> --numstat`，只数 `op_kernel/`+`op_host/`）单标签归因：

| 类别 | 分值 | 占比 | 代表 SHA |
|---|---:|---:|---|
| A1 算法/数据流结构重写 | 111.18 | 33.6% | `28b9ce3` `0d5125c` `7d6aa46` `13c554d` `49f905f` |
| A2 零 TPipe / 裸 UB / schMode 专用化 | 85.53 | 25.8% | `3938863` `c8b5521` `8c8c164` `e21fdce` `9f92b0a` `306c457` |
| D 精度与数值/确定性路径 | 27.83 | 8.4% | `af0807c`(16.53) `de25877`(10.58) |
| B 核数与调度阶梯（含 DVFS） | 25.16 | 7.6% | `31affc6`(9.32) `aa74f56`(6.69) `d8eb6da`(6.29) |
| F 选择与发放（桶校准/回滚/重投） | 36.28 | 10.9%（上限，最可能 6%–9%） | `1baa854` `ba4a357` `2b9d49d` |
| G1 同二进制重判（噪声） | 26.08 | 7.9% | `ec0867d`(15.96) |
| C 搬运形状与 tiling | 7.37 | 2.2% | `4832524`(4.60) |
| E ELF/L1I/冷启动 | 7.30 | 2.2% | `398d587` `dcc7754` |
| G2 存疑 | 4.63 | 1.4% | `7efadf8` |

[证据: `R1-breakthroughs.md` §3.1–§3.3；闭合 A1+A2+B+C+D+E+F+G1+G2 = 331.36，误差 0]

三条最反直觉的读法：

- **教科书式的 `DataCopyPad`/tiling 只值 2.2%**，它几乎总是 A1/A2 的配套（`5a8d07f` 的 32KB 展开流水只换来 +0.03）。
- **两次最大跳变都发生在"先修对"之后**：SyncBN +10.58（bit-exact 12-bit split 除法 + `vrsqrt+2NR`）、LayerNorm +16.53（原生 fp16 + bf16 `TransDataTo5HD`）。快是正确性的副产品。
- **"零 TPipe + 按形状拆一堆极瘦 schMode"是 11-22/11-23 的通用胜负手**，首例 PadV3Grad `1dc3411`，随后 StridedSliceGrad → QuantUpdateScatter（+18.05）→ ConcatD（+7.14）→ LayerNormV3（+5.85）六题复用。这是本场最值得进知识库的一条。
  [证据: `R1-breakthroughs.md` §3.4 第 1 条；`95eafe6a` 看板 #21 把「64B 头 + `noinline` 冷路径 + 3KB Micro-Entry」列为全算子通用 3 招]

**还有一个不该忽略的事实：这条链路一次都没走 MCP。** 93 段对话共 53,095 行，`run_command` 10,768 次、`opsb test` 842 次、`opsb bench` 858 次，而 `parhelion_status`/`parhelion_test`/`parhelion_submit_gate` 作为 **tool_call 的调用次数是 0**（414 次出现在文本里，集中在"做 Parhelion 本身"的会话）。Parhelion v3 的 MCP 面当时在线，但刷分靠的是 CLI + ssh + 本地脚本。
[证据: `R3-loop-and-tools.md` §3.1；`813867cb#47` 确认 `opsb mcp serve` 在跑]
这不是说 MCP 方向错，而是说**入口的默认值决定行为**：4.3 若要 worker 走 MCP，就得让 CLI 与 MCP 输出同构（4.2 工作包 D 的 `parhelion_report` 正是这个方向），否则 worker 会继续挑更顺手的那个。

**那 421 分是怎么来的（R6，补于 DSH 会话）**：11-09 全天 **0 条回执**（一整天的本地开发），11-10 00:54–11:13 打完 **39 发**、九题合计 420.97（CTCLossV2 当日仍为 0，到 11-11 03:02 才有第一分 37.18）。当天的派活是 18 个 `Optimize <题>`（一题 1–4 路）+ 9 个"具体假设"worker（`Multilabel 16-bit accumulator`、`PadV3Grad 16bit write-range bug`…）+ 3 个 `+20` 目标 worker；本地测试密度 **5.8 次/发**，是后期的 2.9 倍；**26% 是同一 SHA 重投**，最早的 note 就写了 `hidden noise is +-4`。11-11 一整天不判题，20 个 `Translate` + 11 个调研/基建 worker 在建知识库。
[证据: `R6-dsh-1110.md` §2/§5/§7；482 个 DSH 会话，31/31 回执 SHA 命中]
这条补上了 §1 原先的空白，也改变了两个判断：**421 不是"第一天就会刷"，而是"第一天全在本地、第二天集中开闸"**；以及**最贵的一次越界（用登录态抓判题接口做规则考古，20 分钟）产出了后续全部判断所依赖的 `scoring-rules.md`**——4.3 应当把这件事做成平台能力，而不是靠 worker 拿凭据。

---

## 3. 六份报告的交叉核对

### 3.1 三个独立来源互相闭合（可信度最高）

| 事实 | R1 | R2 | R3 | R4 | R5 | 我的独立复核 |
|---|---|---|---|---|---|---|
| 421/542/723/752 锚点 | ✅ 331.36 | ✅ | ✅ | ✅ 1717.11 加权 | ✅ | ✅ 421.0/542.0/722.9/752.33 |
| 233 条去重回执、Pass 218/TLE 7/WA 6/RE 2 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 11-22 峰值 23 发/h、11-23 19 发/h | — | ✅ | ✅ | — | ✅ | ✅ 完全一致 |
| 多轨赛马真跑过 | ✅ 复用到 6 题 | ✅ 分支物证 | — | — | — | ✅ `exp-b` 8 仓 / `exp-carbon` 8 仓 / `b-track1,2` 5 仓 / `a-*` 2 仓 |
| 11-10 的 39 发可逐发归因 | ✅ 按首发处理 | — | — | — | — | ✅ R6 补：31/31 SHA 命中 DSH 会话，28 个会话承载 |
| 熔断在回执里留痕 5 次 | — | ✅ 5 条 | ✅ 5/5 | ❌ 判"从未触发" | ✅ | ✅ 我按 `[S3:CIRCUIT_BREAKER]` 检出正好 5 条 |

### 3.2 互补，不矛盾（同一条链的不同切面）

- **涨分归因 vs 提交决策**：R1 按源码树差分机制（A1/A2 占 59.4%）；我按回执 note 分「这一发是谁选的」——**74 次上台阶里有 39 次（164.94 分，占 49.8%）带着 `Simplex-MAP`/`Pairwise-v2` 的桶级判决**，11-22 当天更是 122.50/177.9（69%）。两者并不冲突：**校准层不是涨分的原因，是决定"发哪一发"的选择器**。R1 自己也是这么说的——note 记录的是"我怎么选中它"，不是"它改了什么"；`8e7ba6bc` 的 note 写 Simplex-MAP，树差却是 2,749 行真实代码。
  [证据: `R1-breakthroughs.md` §3.1；`/tmp/p19/receipts.json` note 分类]
- **估计器误差 vs 估计器用途**：R3 说 `est=`（同代重测）基本无偏、`d_hid`（跨代改动）高估 3.70×；R4 说 `estimated_score` 中位偏高 1.57 分。合起来是同一件事的两档：**同代重测可信、跨代外推不可信**。R4 还纠正了一个口径陷阱——剔除 3 条清零后中位只从 −1.57 变到 −1.34，被带偏的是均值不是中位。
  [证据: `R3-loop-and-tools.md` §1.5、`R4-methods-vs-kb.md` 附录 A]
- **熔断"该不该拦"**：R3 说被判为 OPEN 的 5 发 5/5 都是非改进（对）；R5 说按同一规则逐发回放会拦 13/231，其中 4 发当时刷新历史最高（也对）。差别在样本：R3 数的是"平台真的评估过并发车"的那 5 次，R5 是"整场每个时点都套用该规则"。**结论取后者**：规则本身在"成熟题命中率 < 1/6"时必然误杀。
  [证据: `R3-loop-and-tools.md` §1.4、`R5-open.md` §4]
- **tbest 漂移**：R4 推翻"绝对分不可加减"（公开例 tbest 20–24.55h 冻结、隐藏例仅 −0.01…−0.26 分/h），R1 观察到同 SHA 重投普遍更低。两者可共存：主项是同源重投的重计时抖动，tbest 漂移是次要项；R1 也把这条标为【推断】。
  [证据: `R4-methods-vs-kb.md` §2、`R1-breakthroughs.md` §0.2 尾注]

### 3.3 矛盾与裁定

| # | 争议 | 裁定 | 依据 |
|---|---|---|---|
| 1 | 11-22 的 117 发是 **3 个账号槽** 还是 **2 个槽** | **2 个槽。** 任务书给我的"28/39/50 三槽"是错的，我最初的复核也错在同一处；R2 对 | 按原始行分：28 条无 `account_id` 的行全部在 11-22T10:36–12:45Z 提交，而账号字段在 12:02–12:54 之间才开始出现。按**北京日**（UTC+8）分：无字段 28 + 槽 A 22 + 槽 B 33 = 83（11-22 北京日）、槽 A 44 + 槽 B 45 = 89（11-23 北京日）。把 28 条并入槽 A（22+28=50）正好打满 50 的天花板，与 `DAILY_HARD_LIMIT=50` 吻合；一个"用过 28 次后永不出现"的第三槽无法解释 11-23 还有 89 发。旁证：主控 13:48 报 `槽 used=38 + 槽 used=7 = 45`（`95eafe6a#1255`），13:44 的 `daily_quota_limit=50` 在 13:45 变成 `100`（`95eafe6a#1239/#1247`） |
| 2 | R4 判「12h/6 发熔断从未触发」 | **结论对，证据句错。** 熔断确实**一次也没拦住任何一发**；但它并非"从未触发"，5 条回执带 `[S3:CIRCUIT_BREAKER] … SWITCH BASELINE`，全部照常发出（2 条 Pass 无分、1 条 WA、2 条 Pass 低于峰值） | 我独立检出正好 5 条（`6b031766` `6b0316a6` `6b03220c` `6b04194a` `6b041e6a`）。R4 依据的 42 条 `results/feedback-log.jsonl` 时间戳全在 **2026-11-25**（赛后重建），不代表赛期判定；这一点 R5 也提醒过。R2 的"触发 5 次"与 R4 的"从未拦住"其实同时成立——**门有了，动作没有**，这恰是 §4.3 要修的地方 |
| 3 | R1 的 F 类（选择/回滚）只有 6%–9%，与"校准驱动 49.8%" | 两者都对，量的不是一回事 | F 类回答"这一发的分数主要由什么代码变化带来"；49.8% 回答"这一发是被什么证据选出来的"。**4.3 需要的是后者的能力**（排序），不是把分数记到校准头上 |
| 4 | `opus-*` 为何 15:15 全灭 | **模型被内容安全过滤器连杀 4 次**，不是预算也不是主控判断 | 8 条 SYSTEM_MESSAGE 逐条相同（`Output retries exhausted after 4 attempts`），`95eafe6a#1365–#1374`；我复核了 #1365 原文含 content safety 字样。我最初以为"12 步被停"，实际 12 是它们从未开始工作 |
| 5 | 11-23 是否"一个人都没换" | 是，`members.json` 21/21 `killed=false`，全程 `swarm{action:"kill"}` = 0 | `members.json`；`R2-orchestration.md` §2.4 |

---

## 4. 4.3 候选改进 Top 10

排序按「对每发产出（分/判）的影响 × 证据强度」。每条给出：成功那场怎么做（证据）→ 4.3 现状（文件/功能）→ 建议 → 预期作用 → 置信度 → 工作量。
「快」= 单位时间有效产出；「好」= 每发产出；「省」= 省额度/token/设备时间。

### 4.1 按榜型修正计量层与门禁的期望模型（latest 适配）

- **成功那场**：赛制是 `ranking_submission_mode = "latest"`，榜面显示最后一发、任一门不过整题归零。ConcatD 一次 WA 归零后，榜面 54.43 掉到 0，直到 61 分钟后才回到 57.14（两发代价）。收尾时 5 题的在榜分低于历史最高，合计 **10.19 裸分（约 23.3 加权分）没收回**（PadV3Grad 59.98→57.72、ConcatD 77.96→75.40、LayerNormV3 73.65→71.64、PReluGradUpdate 78.01→76.10、MultilabelMarginLoss 75.54→74.09）。
  [证据: `results/scoring-rules.md` §1；回执 `6b02c73d`；`/tmp/p19/receipts.json` 末发 vs 历史最高]
- **4.3 现状**：`scheduler/gate.py:91` 硬编码 `keep_best_board: bool = True`，据此按 `E[max(0, gain)]` 放行；本该纠正它的 `cli/opsb_core/services/sync_reconciler.py:219` 与 `judge_score_fetch.py:171` 都在 `ranking_submission_mode != "best"` 时整段早退。`gate.py` 的 `is_restore_regime` / `BELOW_BEST_IN_RESTORE` 也是 best 口径。
- **建议**：`keep_best_board` 由题目元数据派生；`latest` 下把 `E[max(0,gain)]` 换成 `E[gain]`，并在批准时把"失败则需一发 restore"计入成本（额度与时间）；补一条 `parhelion_status` 的榜型字段。
- **预期作用**：**好**（避免把 +1.09 的候选当 0 按住，也避免把期望为负的候选当免费期权放行）；**省**（少一次归零还账，等于每发省 0–1 发）。置信度：高（代码路径与回执均可复现）。工作量：小（2 处早退 + 1 个 EV 公式 + 1 个字段）。

### 4.2 投机发与回滚成为一等公民（`probe` / `exploit` 双意图 + `restore_best`）

- **成功那场**：270 条原始行里 **171 条 `force=True`**；**26 发在门禁明确写 `holding to …` 的情况下照发**，其中 **10 发创出新高（合计 +45.21）**、16 发回落（合计 −44.23）；**43 发是 restore/rollback**（占提交 16%），11-10 就有 `rollback: a458798 scored 49.94 < 52.36`。多数回落在当天被 restore 收回。
  [证据: `results/submit-queue-outcome.jsonl` 原始 `force` 字段；回执 `6b02f7af`(+8.39) / `6b031254`(+7.92) / `6b02f572`(+7.14)；`6af295e7`]
- **4.3 现状**：`cli/opsb_core/drivers/brief.py` 的冲刺提示词直接写「Never use `--force`, `--now` or a direct uploader」；`gate.py` 只有 `force=True` 这一个粗暴开关，`is_critical_restore` 只在榜面确认归零时才放行；没有"先探后回滚"的组合动作，也没有为回滚预留额度的执行器（`restore_targets` 只算不用）。
- **建议**：`parhelion_submit_gate(intent="probe"|"exploit")`——`probe` 免噪底、计入"实验预算"并要求预算内的预算-1；`exploit` 走现有门禁。新增 `parhelion_restore_best(problem, reserve_slots=1)`，在冲刺开始时预留 1 个槽位给收官回滚。
- **反面证据（必须一起抄）**：11-23 主控自己发了 13 次 `opsb submit`，其中 11 次带 `--force`、10 次带 `--now`，而那天是五天里产出最差的一天（**0.53 原始分/发、21.8% 刷新率**，对比 11-22 的 1.57）。所以"放开 force"必须配**硬预算**，否则就是拿额度换噪声。
  [证据: `R5-open.md` §7；`/tmp/p19/receipts.json` 按日聚合]
- **预期作用**：**好**（拿回 45 分那一半的期望，同时把 −44 那一半变成可回收）；**省**（避免"不敢发 → 停在局部最优"与"乱发 → 归零还账"两个极端）。置信度：高。工作量：中（门禁语义 + 队列 + 会话状态）。

### 4.3 断路器改判「方向」而不是「发数」

- **成功那场**：5 次 `[S3:CIRCUIT_BREAKER]` 触发后**没有任何一次换人**，只切锚点/查根因。按 `gate.py` 的规则逐发回放：R5 用衰减后的同期峰值算得 **13/231** 被拦；我用原始历史峰值独立重放得 **18/231**（口径更严）。两种口径找到的是**同一批 4 发新高**——`QuantUpdateScatter 0aa17ab`（86.61，该题全程最高，前 6 发全部低于 85.89 正好触发 OPEN）、`StridedSliceGrad ba4a357`（71.57，-peak 后连打 8 发才出分，×3 权重=11.82 加权分在榜上站了 16 小时）、`a031e4e`（72.45，全程最高）、`PadV3Grad 1dc3411`（42.77）。
  [证据: 回执 `6b031672`/`6b0328be`/`6b0432d4`/`6b030562`；`R5-open.md` §4]
- **4.3 现状**：`gate.py:95-96` `circuit_limit_shots=6` / `circuit_window_hours=12`，连败 ≥6 置 `OPEN` 且 `approved=False`；`AGENTS.md` 第 6 条另有一套"六发换人"，但**没有执行器**——`queue_service.py` 只把 `circuit_open` 变成 `circuit_ok=False`，全仓库没有任何 `spawn_agent` 挂在熔断上。
- **建议**：①`OPEN` 不封提交，只封**同一假设**——要求提交时 `note` 的机制关键词（ELF/核数/tiling/数值…）与最近 N 发不同，否则降为最低优先级；②把 `circuit_open` 接成事件：写进主控收件箱 + `parhelion_wait(until=["judge"])` 上抛，由 `parhelion_sprint_start(replace=[title])` 一键换席，并按 `submission_id` 集合去重防重放。
- **预期作用**：**好**（按回放，至少 2 个最终最好分不被自己拦掉）；**省**（不再浪费"连败"期的空转）。置信度：判据与数据为证据；"被拦后历史会变"使 13–18 都只是近似。工作量：小–中。

### 4.4 逃逸锁：补 TLE 路径 + 判题侧噪声判定

- **成功那场**：7 发 TLE 里有 5 发落在 11-22/23；**CTCLossV2 在 11-23 11:04 一次 RE 之后，12:30 用新 SHA `dcc7754` 拿到 75.11 的全程最高分**。整场有 4 个 SHA 在源码未改时被判出两种判决类别，`restore after a single WA on the same source (platform flake?)` 是当时的原话。
  [证据: 回执 `6b0431fa`；`6b02c73d` note；`R5-open.md` §1]
- **4.3 现状**：`scheduler/calibration.py` 把 WA/RE/TLE 一律置 `escape_lock=True`，`gate.py` 对该题任何新候选返回 `ESCAPE_LOCK_ACTIVE`；而 `scheduler/wa_repro.py:35-36` 只有 `FAILED = ("Wrong Answer", "Runtime Error")`，`LOCAL_FAILURES = ("WA", "RE")`，`apply()` 对 TLE 直接返回 `not a WA/RE receipt` —— **TLE 永远不会生成复现变体，也就永远解不了锁**（我已核到该行）。
- **建议**：①TLE 建与 WA/RE 并列的复现路径；②"同源曾线上 Pass 一次"作为第三种解锁证据；③同源重跑 N 次未复现即判为判题侧噪声并记录为 `judge_noise`，不进逃逸锁。
- **预期作用**：**好**（否则一次判题侧 TLE 就把该题冻结成"只能 restore"）；**省**（避免为解锁而重复烧发）。置信度：高（代码路径 + 回执双证）。工作量：小。

### 4.5 提交前机械体检接入 S1（冷启 / ELF / 核数）

- **成功那场**：7 大线上事故里有 3 条是同一类——LayerNormV3 `cec9fed` 81 份模板实例化撞 420s 编译 TLE（`b676b33` 剪到 33 份，−23s）；PadV3Grad `be05c5b` **172 KB `.o`** 在 32KB I-cache 的 3-run 冷启下白交 `+1.8us`，线上从 43.62 反跌 42.45；SyncBN `73046fe` 的 10 核 DVFS 前两发 `9us` 双峰被本地 warmup 完全掩盖。`.text` 从 80KB 砍到 63KB 这一发 +2.58 分。**但要说清楚：瘦身与涨分的因果只在单题成立，且有反例**——同一题把 81→33 份实例化（编译 −23s）的 `b676b33` 线上只有 **71.37**，比 `9773dff` 的 73.65 低 **2.28**，回执自认 `improved=4, regressed=7`。所以这一条的稳健内核是**防 TLE**，不是"瘦了就涨"。
  [证据: `docs/debugging-and-escape-postmortems.md` 事故 1/4/5；回执 `6b0431fa`；`95eafe6a` 看板 #19（全 10 题 `.o` 体积 vs 线上 pub#1..5 横向 CT 扫描）]
- **4.3 现状**：能力已经有——`cli/opsb_core/fast_feedback/cold3_probe.py`、`elf` 统计、`parhelion_elf_cold3_check`、`parhelion_bench_diagnose`；但 **`scheduler/` 与 `queue_cmd.py` 一次都没引用 `fast_feedback`**，S1 只看 `EXTENDED_PASSED` 与 `mismatched=0`。
- **建议**：S1 增一个机械前置：`.o`/`.text` 环比增长 >X%、cold3 `run1/run3 ≥ 1.45` 双峰、`block_dim ∈ {10, 半核 dummy}`、模板实例化数 > 阈值，任一命中则要求 `intent=probe` 或显式 acknowledgement，并把这些数字写进 `parhelion_submit_gate` 的返回。
- **预期作用**：**好**（拦掉"本地快 1.5×、线上倒跌 5.7"这一类，历史上占 3/7 事故）；**省**（省掉 TLE 的 2 发额度 + 14 分钟）。置信度：高。工作量：小–中（能力已存在，只缺接线）。

### 4.6 派活说明：把"本题情报 + 目标 + 契约"写进 brief

- **成功那场**：11-22 brief 中位 **1,160 字符**、11-23 **1,285 字符**，必含六项：独占目录/分支、测试与提交命令、目标分与 Top1 及加权空间、关键情报（病根 `.o` 172KB / 5 公例 5.3–5.6us）、汇报方式与收件人、熔断条件。11-23 还多两处升级——把只读研究单独设席（`-recon`/`-arch`，明确禁 `opsb test`），以及**指名下游收件人**。
  [证据: `813867cb` 看板 seq 2/6/90；`R2-orchestration.md` §1.2 字段表]
- **4.3 现状**：`cli/opsb_core/agents/sprint.py` 的 `SOLO_TASK` 是 **315 字符 / 45 词**的通稿（"Own this problem end to end…"）；`drivers/brief.py` 的 `MAX_BRIEF_BYTES = 1500`，模板里只有 `Board: score/status/rank`，**没有榜首、没有加权空间、没有本题病根、没有熔断与汇报契约**；`services/state_card.py`（2,000 字节）有 best/最近回执/公开例差距，但同样没有 leader 与桶级校准。
- **建议**：brief 模板加三行——`leader/headroom`（从 `load_slim_boards` 已有数据算）、`最近 3 发的机制关键词与结果`（从账本取）、`本轮假设 ID + 熔断条件`；把 `-recon` 类只读席位的 brief 单列（禁 `parhelion_test`，产出 blueprint）。
- **预期作用**：**好**（worker 第一轮就打对桶）；**省**（少一轮探索）。置信度：中–高（brief 质量与产出速度的相关性来自 R2 的三场对照，属观察非实验）。工作量：小。

### 4.7 席位：从「3 种方向」扩到「工序拆分 + 只读侦察 + 双轨赛马 + 按题覆盖」

- **成功那场**：11-23 的 `Group A · Split-by-Stage`（recon → s-elf/ml-pipe → int，**4 个只读/半只读工序**）× `Group B · Multi-Worktree Heterogeneous Racing`（t1/t2 双轨）＋ 每题一个 `-arch` 天马行空研究员，20 个席位零 kill。物证：`a-ml-pipe`/`a-s-elf` 在 2 题、`b-track1/2` 在 5 仓、`exp-b`/`exp-carbon` 各 8 仓。11-23 的 board 675 条里 599 条是 worker 互投，主控只发 76 条。
  [证据: `813867cb` 看板 seq 2–15/85–90；`git -C ict-cann-nov/<题> branch -a`；`R2-orchestration.md` §1.1/§3.3]
- **4.3 现状**：`MODES = {solo, dual(algorithm,dataflow), full(+calibration)}`；`sprint_seats()` 只认 `path.parent.name == ".worktrees"` 且目录名恰为 `sprint-<axis>` 的布局，**实战的 `ops-bench/.worktrees/<Title>-expB` 与 `<repo>/.worktrees/b-track1` 两种都识别不了**；`start_sprint` 对整个 `problems` 列表套同一个 mode，无法"3 题 dual + 2 题 pipeline"。
- **建议**：seat 按 `(problem, branch)` 认定；新增 `mode=pipeline`（`harness` 测量台作者 → `recon` → 按桶拆的实现席 → `integrator`，`recon` 强制只读）。第一道工序是 **harness 作者**而不是只读侦察：11-18 的 4.19 分/判来自主控亲手把测量台写进各题 `validation/`（29 次）+ `tools/npu-test.py`（11 次），这是全程最高的一天与 `mode=race`（两条同目标轨 + 显式 winner 选择）；允许 `problems=[{title, mode, seats}]` 覆盖；`parhelion_sprint_adjust` 支持冲刺中改席位（11-22 的"收缩并发"就是这种动作）。
- **预期作用**：**快**（并行度 ≈ 判题吞吐：10 worker→5 发/h、20 worker→19 发/h）；**好**（工序拆分让 recon 的 PMU 结论直接进实现席）。置信度：中（形态与吞吐相关是证据，"多出来的轨"具体产出哪一发无法严格归因）。工作量：中–大。

### 4.8 跨题扫描 + 全员广播 + 消息带证据等级

- **成功那场**：主控对全 10 题做了一次 `.o` 体积 × 线上 pub#1..5 的横向 CT 扫描，广播出"3 条价值 +150 加权分的铁证"（`95eafe6a` 看板 #19），随后 #21 又把 StridedSliceGrad 的零冷启瘦身列为"全队核弹级通用架构突破 · 立即借鉴"，直接带来 6 题复用。但同一机制也制造了两次事故：`SetBlockDim(halfCores)` 被广播为"硬件发现"，`734e097` 线上 **−11.83** 分；`PreLoad(N)` 被三题同时采纳，剂量反应把 PadV3Grad 从 54.86 打到 46.96，20 分钟内全队回滚。**board 上没有"这条是本地猜测还是线上回执"的字段**。
  `734e097` 这一发最刺眼：它的回执 note 写的是 `improved=23, regressed=0 · .o=-40.5% · All 4 stages passed · UCB=+7.50`——本地能给出的最强证据——线上却是 −11.83。**本地证据再漂亮也预测不了 3-run 冷启下的线上判决。**
  [证据: `813867cb` 看板 seq 117/120/200/201/255/268/316/368；回执 `6b03fddd`（734e097，59.74）与 `6b0328be`（ba4a357，71.57）]
- **4.3 现状**：`parhelion_pmu_card`/`bench_diagnose`/`elf_cold3_check` 都是**单题**；广播缺失（`parhelion_agent_send` 是 1:1、`parhelion_squad` 是定向）；`parhelion_agent_messages` 是主控视角回放，worker 之间没有共享看板；`kb_record` 有 `verdict/portability`，但消息通道没有证据等级。
- **建议**：①`parhelion_sweep(metric="elf|pmu|cold3", problems="all")` 出横向表；②`parhelion_broadcast(claim, evidence)` 一条到所有席位下一回合；③消息与 KB 强制 `evidence.class ∈ {local, paired, online_receipt}`，`local` 不允许直接转成 `hardware` 条目，协作页显示"待验证"。
- **预期作用**：**好**（把 +150 加权的那次扫描变成常规动作，同时挡住 −11.83 那类误广播）；**省**（一次扫描替代 10 题各自 rediscover）。置信度：高。工作量：中。

### 4.9 估分只做排序先验，不做闸门；σ 在线估计

- **成功那场**：10 题校准器自报 LOSO-CV `dir_acc` 中位 0.721、`Spearman` 0.284、`RMSE` 4.95；同 SHA 重投 pooled σ=3.04（df=29，极端 15.96），另有节点漂移 ±2.5%。而门禁用 `noise_floor_pts = 2.5`（≈1.07σ）当硬阈值，**比估计器自身的误差还小**——判不出 2.5 分与 0 分的区别。赛中 S2 把 9 发真提升按住（其中 3 发后来成了新高）。
  [证据: `results/<题>/calibration.json` 的 `loocv_dir_acc`/`loocv_mae_pts`（PadV3Grad 0.57/3.21、SSG 0.55/3.70、MML 0.69/4.48）；`R4-methods-vs-kb.md` §2；`R3-loop-and-tools.md` §1.4]
- **4.3 现状**：`scheduler/estimator.py` 有 `calibration_trust` 与 UCB，但 `gate.py` 的 `noise_floor_pts=2.5` 是常量，`SIGMA_HIDDEN_JITTER` 也是常量；`ALREADY_ACTIVE_ON_BOARD` 把同 SHA 重投的 `delta_expected` 定为 0。
- **建议**：噪底改为 `k·σ̂`（k=2，即约 4.6–6 分）且 σ̂ 按题在线回填；同代重测（`est`）与跨代改动（`d_hid`）分两档标定，跨代走饱和变换；`BELOW_NOISE_FLOOR` 只降优先级、不置 `approved=False`（与 4.3 条合并）。
- **预期作用**：**好**（少误杀）；**省**（不为 1σ 内的抖动烧发）。置信度：高（σ 与误差都可从数据复算）；"改完一定更好"是【推断】。工作量：中。
- **补充（我的独立测量）**：只看"每次都 Pass 且分数>0"的同 SHA 重判组（21 组、50 发），末次−首次的中位 **−1.08**、均值 −0.53、区间 **[−6.06, +15.96]**，13 降 / 8 升、9 组 |Δ|≥2。即**同源重投的期望约 −1 分，但尾部 ±6 到 +16**——它既不是免费期权，也不是稳定亏分，所以「靠重投刷分」和「一律禁止重投」都不对，只能用 σ̂ 定价。

### 4.10 收官对账自动化（restore 扫描 + 分支归并 + `best/*` 刷新）

- **成功那场**：收尾时 5 题在榜分低于历史最高（合计 10.19 裸分 / 23.3 加权分）；2 题的历史最高分 SHA **只存在于赛道分支**（ConfusionTransposeD `235f754` 80.07 只在 `b-track1`；CTCLossV2 `dcc7754` 75.11 只在 `b-track2`，而其 `main` HEAD 正是一发 RE=0 分）；`best/*` 标签停在 11-22 19:42 那一版（PadV3Grad 标 `51.54` 而实际 59.98、MML 标 `61.93` 而实际 75.54）。
  [证据: `git -C ict-cann-nov/<题> tag -l 'best/*'`、`branch -a --contains`；`/tmp/p19/receipts.json` 末发 vs 最高]
- **4.3 现状**：`gate.py` 的 `get_quota_status` 已经算出 `restore_targets` 与 `needed_closing_restores`，但**只用于展示**（`min_closing_reserve` 明确"never gates, holds or ranks"）；没有任何"收盘扫描"动作。
- **建议**：`parhelion_close_sweep()`：对每题比较 `historical_best_sha` vs `active_board_sha` vs `main` HEAD，输出三张清单（需 restore / 需并分支 / 需刷 `best/*`），并在额度的最后一个槽位上自动执行 restore。
- **预期作用**：**好**（直接拿回 10–23 分）；**省**（不用人肉对账）。置信度：高。工作量：小。

### 4.11 其余候选（未入 Top 10，但值得排期）

- **账号槽利用率可见 + 冲刺中可加槽**：11-22 两个槽的北京日用量是 83 / 89，其中一槽 22+28=50 打满、另一槽 33 只用了 66%；11-23 是 44/45。计量层已按 `list_eligible_account_slots` 求和，缺的是动作层入口（`parhelion_sprint_start` 没有加槽参数）与槽利用率告警（任一槽 <60% 提示并槽/加槽）。[证据: `95eafe6a#1239/#1247/#1255`；`R5-open.md` §6]
- **`public_term / hidden_term` 做成只读报告列**：单题 90 分在 45 个隐藏例上，而"这题剩余空间在可见例还是隐藏例"决定该改 kernel 还是改对齐。赛期只能靠读**全队账号会话凭据**去判题站拉这个分解（`95eafe6a` step 1140/1148，此处含凭据），4.3 侧没有等价入口。按 `results/scoring-rules.md` 的反解方法做成 `parhelion_report` 的一列即可。[证据: `R5-open.md` §8]
- **同类题传手法而不是传结论**：R1 的 10 条跨题复用链（零 TPipe、核数阶梯、L1I 瘦身、去 `GET_TILING_DATA_*`、桶校准选型、420s TLE 红线…）应当以"适用条件 + 首例 + 复用记录"入库；现在 `templates/` 的硬件/积木/社区三柱已有 15 条，但**过程类知识（怎么选、怎么回滚、怎么收官）一条都没有**。[证据: `templates/**/index.json`；`R4-methods-vs-kb.md` §3]
- **关闭"主控亲手造工具"的缺口**：10 个题的 `validation/` 全部由主控手写（29 次改动 + 11 次改 `npu-test.py`），说明"逐例证据生成"当时没有平台能力，只能由主控承担——这正是 4.2 工作包 D（`parhelion_report` 视图）要消灭的部分。[证据: `R5-open.md` §3]

---

## 5. 不建议照搬

1. **把 `--force` 日常化**。171/270 发带 `force=True`、26 发在门禁 HOLD 下强投；净收益只有 +1 分（+45.21 / −44.23），真正的价值在"能回滚"。要抄的是**4.2 的可回滚投机**，不是**无门禁**。
2. **同源重投吃噪声**。G1 的 26.08 分（7.9%）来自重判，极端一例同一次排队被判两次、差 15.96 分。这是判题侧方差，不是手法；`e799eb6` 的 note 自己写 `platform flake?`。
3. **靠人工加账号槽提吞吐**。117 发来自「12:54 上线第二槽」+「16:00Z 北京日重置」，且有一槽只用 66%。这是资源，不是方法；4.3 应做利用率和排队，而不是教人开小号。
4. **把 `opus-*` 全灭当"换人策略"**。8 个 worker 是 12 秒内被内容安全过滤器连杀的（`Output retries exhausted after 4 attempts`），与"换更强的模型"无关；同一故障在 11-23 又复现两次，直接导致收尾审计没做。
5. **主控人肉读全榜、手写 1,160–3,051 字符 brief**。这是一个人的判断力在 10 题 × 3 轨上的复制，短期不可复制；应转成平台生成的 brief（4.6 条），否则席位一多就塌。
6. **用户当调度器**。11-22 用户 96 次发话里一多半在指挥"开几路、换什么模型、收缩并发、关服务器"；11-23 用户发话只有 1/3，效率反而更高。无人值守场景不该依赖这条。
7. **一题 4 路 worker**。R5 把它列进 12 条，但证据只有主控自述（`813867cb` step 32/47），**无法归因到多出来的两路产出了哪一发**；在 4.3 里应当先做"按工序拆"（4.7），再谈"一题 4 路"。
8. **把本地 msprof 的"硬件定律"直接广播**。半天内两次（halfCores −11.83、PreLoad 剂量反应），两次都是 warmup 掩盖了 Die-0 冷启。要抄的是"广播"，不是"广播未经验证的结论"（4.8）。
9. **赛后集中写知识库**。11-22 22:44–22:52 一次性重写了 `PERF-EXP.md` 与 6 份 docs，11-23 03:44 又批量改写一次；这是因为当时没有平台账本。4.2 的账本方向正确，不要退回"赛后补写"。

---

## 6. 未验证与遗留

1. **11-10/11-11 的 41 发**没有 Antigravity 记录，但 R6 已从 DSH 会话补齐：11-09 全天 0 发（纯本地开发）、11-10 00:54–11:13 打完 39 发、11-11 只补 2 发。R6 的派活原文仍缺（根会话没留 subagent 参数），只能按 worker label 反推。
2. **`dcc7754`（CTCLossV2 75.11）会不会被 4.3 的逃逸锁拦下**是【推断】，取决于 11:04 那发 RE 能否被复现器复现。
3. **断路器回放 13（R5，衰减峰值）到 18（我用原始峰值重放）**：两种口径都只是近似——真实历史上被拦后后续发数不会照原样发生；被拦的 4 发新高在两种口径下一致。
4. **"多轨赛马保留哪条轨"用的是当前分支快照**，不是收盘时刻快照；ConfusionTransposeD 的 `main` HEAD 是收盘后的 `.gitignore` chore 提交，会削弱"未并入"结论的强度（R2 标注）。
5. **`d_hid` 3.70× 高估**里"估计器错"与"分数函数在高分区必然饱和"未分离（R3 标注）；因此 4.9 条只给相对结论。
6. **预测偏差的口径分歧**：R3 说 `est` 基本无偏、R4 说 `estimated_score` 系统性偏高 1.57。二者量的是两个字段/两档样本，本轮没有做统一重算。

---

*各子报告：R1 `R1-breakthroughs.md`（662 行）、R2 `R2-orchestration.md`（373 行）、R3 `R3-loop-and-tools.md`（448 行）、R4 `R4-methods-vs-kb.md`（325 行）、R5 `R5-open.md`（339 行）、R6 `R6-dsh-1110.md`（11-09～11-11 的 DSH 盘，补 421 分的来源）。*
