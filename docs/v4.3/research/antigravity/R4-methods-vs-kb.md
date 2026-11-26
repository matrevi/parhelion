# R4 经验文档与事实对照

> 只读侦察范围：`~/Competitions/ops-bench/{PERF-EXP.md,docs/*,skills/*}`、`results/`（回执/原始队列/校准器/本地 run）、`ict-cann-nov/<题>` 的 git 只读历史、P19 模板与 `AGENTS.md`。
> 本文只写这一个文件；未做任何 git 写操作，未联网，未写题目仓库。
> 证据标记：`[证据: 回执 <sid>]`、`[证据: sha <8位>]`、`[证据: <conv>#<step>]`、`[证据: 文件:行]`。**【证据】**=可复算物；**【推断】**=由证据推出、未直接测量。

## 0. 判定口径与三条方法论纠正

判定四级：**成立**（≥1 回执 + ≥1 可见 diff 或逐例数据）、**部分成立**（机制有证据但归因/数值/适用范围之一不成立）、**无证据**（只有文档自述）、**被推翻**（有反例数据）。"跨题"只在 ≥3 题有回执时才算；单题验证一律标"单题证据"。

本文用到的四个可复算数据源：

| 源 | 规模 | 用途 |
|---|---|---|
| `/tmp/p19/receipts.json` | 233 条已判回执（按 submission_id 去重） | 分数、时序、逐题阶梯 |
| `results/submit-queue-outcome.jsonl` | **270 条原始行**（未去重），含 `estimated_score` / `public_times_us` / `public_tbest_us` / `canonical_sha` | 预测偏差、公开例隐含分、重复发 |
| `results/scoring-rules.md` | 11-10 平台取证文档（[measured]/[metadata]/[inference] 三级标注） | 计分公式、tbest 更新律、权重 |
| `results/<题>/calibration.json` + `<sha>/validation.log` | 10 题校准器 + 266 份 `KERNEL_BINARY_STATS` | 预测能力、`.o/.text`/模板实例化 |

**方法论纠正一（下文反复用到）：核验"某机制是否在某个已交 SHA 里"，必须用「上一个已交 SHA → 本次已交 SHA 的树 diff」，不能用 tip commit message。** 提交树的 tip 只是最后一小步：`11bd996` 的 message 是"V4.1: fix AlignUp device call in RunGather"（一行修复），但 `03998e4→11bd996` 的树 diff 是 **494 插入 / 301 删除**；`7919dcb` 的 message 是"guard working directory in validation/run-npu.sh"（测试脚本），但 `899a2dc6→7919dcb` 的树 diff 是 **2120 插入 / 705 删除**。[证据: `git diff --stat` 于 `ict-cann-nov/PReluGradUpdate`、`ConcatD`]
【推断】历史上"某 SHA 做了 X"的叙述若只读 commit 标题，会把整棵树的功劳记到最后一个零散提交上。

**方法论纠正二：分数归因必须区分"该 SHA 的边际增量"与"该树的累计高度"。** 例：`0aa17ab` 被写进 4 条杀手锏，但它自身增量只有 **+0.72**（85.89→86.61），主力是 `28b9ce3`(+18.05)/`7d6aa46`(+15.26)。[证据: 回执 6b031672 / 6b030192 / 6b02e686]

**方法论纠正三：`latest` 计分下"历史最高分之和"与"榜面分"是两个口径，不能混用。** 按回执自算：各题 MAX 之和 = **752.33 裸分 / 1717.11 加权**（与 FACTS 的 752.3 一致），各题"最后一发"之和 = 742.14。[证据: `results/submit-queue-outcome.jsonl` 全量复算]

## 1. PERF-EXP 7 大杀手锏逐条核实

| # | 结论（原文压缩） | 出处 | 证据 | 判定 | 适用条件 | 备注 |
|---|---|---|---|---|---|---|
| 1 | 零 `TPipe`/`TBuf` 裸静态 UB Arena 消除框架开销、释放近全量 192KB | `PERF-EXP.md:37`、`docs/ascendc-optimization-patterns.md:7-32` | 回执 9773dff(73.65/73.40/67.91)、11bd996(78.01/76.10)、0aa17ab(86.61)；树 diff：`03998e4→11bd996` 719 行内核改动、`0aa17ab` 57+/49−，diff 内含 `static` 偏移构造 | **部分成立** | 中小 shape、schMode 数少、UB 吃紧 | 三题都有"零 TPipe"代码，但都是与分核/调度/编码方式同发的树级大改，**无法单独归因**；`0aa17ab` 边际仅 +0.72。单题证据×3，非受控对照 |
| 2 | 64B Direct-`__gm__` Tiling + `SetDataSize(actualBytes)` 消 184B–1704B H2D 与栈拷贝 | `PERF-EXP.md:38`、`patterns.md:36-57` | 回执 3267c09(65.15，note 明写 "64B direct-GM tiling (zero 184B GET_TILING_DATA_WITH_STRUCT copy)")；`4832524`/`03998e4` 同类；tip commit 与 note 同名 | **成立** | 热路径只需 5–8 个标量的中小 shape | "17 桶中位数 110.18→88.36us、冷启 −23.08us" 只见于文档，回执里没有逐例数字 → 数字层面按"单题证据+文档自述"记 |
| 3 | 极瘦 `.o` ELF + `noinline` 冷路径：破 32KB I-cache 冷启税、避 420s TLE | `PERF-EXP.md:39`、`patterns.md:61-73` | 回执 cec9fed：**同一 SHA 先 Pass 67.03（14:37 提交→14:44 完成，400s），7 分钟后重投 TLE 0 分**；b676b33(71.37)；`62a424f/f41d864/881eda0/f12039f` **无回执** | **部分成立** | 模板实例化爆炸 / `.text` 逼近 I-cache 与判题机时限时 | 避 TLE 成立（有回执）；**"瘦身→涨分"被反例削弱**：b676b33 比 9773dff 低 2.28 分，且其 note 自认 `[S2:NOISE_FLOOR] Expected gain +0.64`。且 400s→420s 说明 TLE 是**临界现象**不是确定性 |
| 4 | 半核 `1/20/40`（40 约数梯队）防端口争抢 + 规避 10 核 DVFS 尖峰 | `PERF-EXP.md:40`、`patterns.md:77-86` | 回执 31affc6(56.40→**65.72**)、aa74f56(64.31→71.00)、9c943d3(72.26→72.95)、0aa17ab(+0.72)；反例 734e097(59.74，−11.83)；73046fe 确为 10 核(+0.45 停滞) | **部分成立** | 64KB–2MB 的中小 payload；非对称核数一律避免 | **"半核比满核快"跨题不成立**：哑核 early-return 版 −11.83。原文"31affc6（56.40 → 78.18）"是**跨提交拼接**——78.18 属 8e7ba6b，31affc6 本身只到 65.72。三题增量 +0.69/+0.72/+0.39 均在 1σ=2.7 内 |
| 5 | 硬件特种指令取代 Gather/标量（`TransDataTo5HD`、`vconv`、Level-0 指令） | `PERF-EXP.md:41`、`patterns.md:90-102` | 回执 af0807c(27.13→43.66，commit 含 "bf16 transpose SIMD")、4832524(71.00→75.60，commit "zero-index TransDataTo5HD<u16> ZIP2"，note "32/32 beat torch, sum_med 113.28 vs 117.36us")；`ae27948` **无回执** | **成立** | 16-bit 转置/按列累加、窄化宽化替代索引 Gather | 两题都是回执 + 同义 diff + 逐桶数字。"48-Bank 6-Coloring 消 82% 冲突"只有文档自述 → 无证据 |
| 6 | PMU 驱动闭环：`PipeUtilization/Memory/UB/Conflict` 定界真瓶颈，拒绝凭直觉 | `PERF-EXP.md:42`、`patterns.md:106-111` | 管线真实：`validation.log` 内含 `vec_ratio/scalar_ratio/mte2_ratio/mte3_ratio`，`COLD_START_STATS` 出现在 **153** 份日志，`aiv_scalar_time` 出现在 16 份 | **部分成立** | 需要区分 MTE2/MTE3/VEC/Scalar 时 | 两个具名案例**数值不可复现**：PRelu 的 `scalar 7.39us(0.348) > mte2 7.21us(0.340)` 在留存日志里找不到该对值（日志中的 7.396 是 `mean_us`）；MML "4×UB 读写" 亦无对应记录 → 例证降为"无证据" |
| 7 | 官方样例仿真 + 线上残差闭环校准（`Simplex-MAP v2.0`、`EXPLORE`、温漂抵消），让本地估算与线上涨分同向 | `PERF-EXP.md:43`、`docs/local-bench-methodology.md:16-47` | 10 题 `calibration.json` 的 LOOCV：`dir_acc` **0.55–1.0（中位 0.721）**、`spearman` **−0.066–0.60（中位 0.284）**、`rmse_pts` **2.09–7.17（中位 4.95）**、`calibration_trust` 0.48–0.90 | **部分成立（被量化修正）** | 只能用于"方向"，不能用于"幅度"或"排序" | "同向"只在方向级成立（0.72 命中 > 0.5）；**RMSE≈4.95 ≈ 1.8σ，Spearman 0.284 表示排序几乎无效**；最差的两题（PadV3Grad −0.066、SSG +0.06）恰好是观测最多的两题（32/33 次）——加数据不涨精度，因为判题噪声是主项 |

## 2. 计分模型与调度法则核实

| 结论 | 出处 | 证据 | 判定 | 适用条件 | 备注 |
|---|---|---|---|---|---|
| 50 用例 = 5 公开 + 45 隐藏，等权、无 baseline、任一失败清零 | `PERF-EXP.md:13` | `scoring-rules.md:18,36,44`（[metadata] `use_baseline=false`、50 testcases、difficulty 合计 23→2300）；`public_times_us` 恒为 5 元 | **成立** | 本场（第 11 届华为 ICT 算子赛道） | 我按 5:45 反解 63 发，"隐藏隐含均值"全部落在 (0,1)，自洽 |
| 公开 5 例仅值 3–10 分，85–95% 分数在隐藏例 | `PERF-EXP.md:19` | `scoring-rules.md:223`（3.1–7.5 分/题，11-10 快照）；我用 63 发复算公开项 = pub/10 ∈ **5.4–8.9** | **成立** | 与本场同构（5+N 等权） | 唯一可当"公理"用的条目 |
| `pub_avg` vs `implied_hidden_avg` 倒挂 **17–23 分** | `PERF-EXP.md:19` | 63 发自算：`pub_implied − hidden_implied`（5:45 分解）**中位 +16.00，IQR 11.82–19.76，极值 2.29/24.02；19/63 落 17–23；41/63 < 17** | **部分成立** | 只在低 r 题（SSG 中位 20.19、PadV3Grad 19.53）成立；ConcatD 仅 7.95 | 数值口径正确但把**上四分位当成了普适区间**；原文举例"PadV3Grad 66% vs 39.8%"= 26.2 分，超出我样本全界（max 24.0）→ 该例不可复现 |
| tbest 非平稳：对手持续刷新，故不同时间窗绝对分不可加减，必须用同代快照 | `PERF-EXP.md:20` | `scoring-rules.md:24,163,177-179`：**公开例 tbest 24.55 小时 50 个 cell 一个都没动**；隐藏例漂移 **−0.01…−0.26 分/小时**；我另测 PadV3Grad 16 发 20 小时内 `public_tbest` **逐字节恒定**（sum=17.907） | **被推翻（作为主因）** | 只有隐藏例有慢漂，量级 0.24–6 分/天 | 主因不是 tbest 漂移而是**同源重投重计时**（`scoring-rules.md:24` 与 2.2/2.3 节）。因此"绝对分不可直接加减"应改为"可直接加减，误差 0.01–0.26/h，但单发不可信" |
| 平台固有抖动 **σ≈2.7 分**，据此设 `noise_floor` 防抖 | `PERF-EXP.md:21`、`docs/multi-agent-workflow.md:39` | 21 组同 SHA 重复（df=29）pooled within-SHA **sd = 3.04 分**；spread 中位 2.51、均值 3.62、max 15.96；门禁后期实测 `sigma=3.51/4.12` | **成立** | 单题单发；重尾 | 点估计 2.7 与我的 3.04 差 13%，同量级；但**重尾**：ec0867d 同 SHA 双发差 15.96、8e7ba6b 三发差 4.69。门禁后期已上调到 3.5–4.1，说明 2.7 偏低 |
| WSPT 优先级 = `difficulty × (Top1 − cur)` 决定算力倾斜，4 个 ×3 题单位收益最高 | `PERF-EXP.md:25` | 代码实现是 `difficulty * max(0, delta_exp)`（`cli/opsb_core/commands/queue_cmd.py:111`）；按 `difficulty × 剩余天花板` 排序，前 4 名恰为 4 个 ×3 题（PadV3Grad 201.9 / CTD 158.4 / ConcatD 150.6 / SSG 143.4，`scoring-rules.md:206-233` 复算）；这 4 题吃掉 **131/231 ≈ 57%** 回执 | **部分成立** | 赛程前半段 | 公式与文档不同（用预测增量而非 `Top1−cur`），但排序结论事后成立：11-10 判"边际递减、应停止投入"的三题（ConcatD/PRelu/SyncBN）最终涨幅 **+31.2/+22.6/+15.6**，排 10 题第 6/8/10 —— 是全部里最低的一档 |
| 12 小时 / 6 发无突破熔断（G3），触发后禁止微调、必须重写架构 | `PERF-EXP.md:27`、`multi-agent-workflow.md:40`、`skills/problem-develop/SKILL.md:17` | 配置存在 `circuit_breaker_limit: 6`（`cli/opsb_core/config.py:141`）；但按文档口径（12h 窗内连续 6 发无新峰）**至少 5 题应触发**：MML **12**、SSG **10**、LayerNormV3 **6**、PadV3Grad **6**、QUS **6**；观测到的 `CIRCUIT_*` 只有 UI 徽标字符串，42 条门禁日志全部 `CIRCUIT_CLOSED` | **被推翻（作为执行机制）** | —— | 全流程未见一次 `CIRCUIT_OPEN`；主控 11-22 自述"全题无 ESCAPE_LOCK，无 12h/6发 熔断"。[证据: 95eafe6a#17] 规则是文档+配置，不是被执行的约束 |
| Multi-Track 独立 `git worktree` 赛马，胜出者合入 `main` | `PERF-EXP.md:26`、`multi-agent-workflow.md:24-31` | 分支真实：`b-track1/b-track2`（CTC/ConcatD/CTD/MML）、`a-ml-pipe/a-s-elf`（PadV3Grad/SSG）、`tune-*`（ConcatD）；`main` 上大量 `[exp-b]`/`[main]` 前缀提交 | **部分成立** | 11-22 起 | **归并环节未落实**：10 题里 **2 题的最优 SHA 不在 main**（CTCLossV2 `dcc7754d`、CTD `235f754f`）；且 CTCLossV2 的 `main` = `a1df9d5` 正是那发 **Runtime Error（0 分）**。[证据: `git merge-base --is-ancestor` / `git rev-parse main`] |
| 10 题封盘 1637.34/2300、裸分 716.49、全国 #3/789 | `PERF-EXP.md:3,49` | 各题 MAX 之和 = **752.33 / 1717.11**（我的复算，与 FACTS 752.3 吻合）；`results/score-snapshot.json` 为 11-25 刷新的**陈旧榜面**（多题 `submission_time` 停在 11-10/11-18） | **无证据（口径混用）** | —— | 1637.34 是"榜面分（latest）"，不是"历史最优和"；#3/789 无留存榜单快照可核。10 题速查表是 **11-22 19:42 快照**，其中 3 行已被 11-23 回执超越（MML 61.93→**75.54**、PadV3Grad 51.85→**59.98**、SSG 71.57→**72.45**） |

### 2.1 十题战果复算（本文的审阅基准表）

单位：分。首发 = 该题第一条 Pass 回执；最终 = 该题历史最高 Pass；"突破/噪声级"按 `breakthroughs_raw.md` 的 87 条阶梯拆分。

| 题 | 权重 | 首发 | 最终 | 涨幅 | 回执数 | 阶梯条数 | 其中 <1σ(2.7) | 加权贡献 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| StridedSliceGrad | 3 | 47.48 | 72.45 | +24.97 | 43 | 13 | 8 | 217.35 |
| PadV3Grad | 3 | 27.12 | 59.98 | +32.86 | 33 | 11 | 6 | 179.94 |
| ConcatD | 3 | 52.36 | 77.96 | +25.60 | 30 | 10 | 7 | 233.88 |
| ConfusionTransposeD | 3 | 38.64 | 80.07 | +41.43 | 25 | 12 | 7 | 240.21 |
| LayerNormV3 | 2 | 26.81 | 73.65 | +46.84 | 18 | 8 | 3 | 147.30 |
| MultilabelMarginLoss | 2 | 46.09 | 75.54 | +29.45 | 25 | 5 | 0 | 151.08 |
| CTCLossV2 | 2 | 37.18 | 75.11 | +37.93 | 16 | 8 | 2 | 150.22 |
| QuantUpdateScatter | 2 | 47.15 | 86.61 | +39.46 | 17 | 8 | 6 | 173.22 |
| SyncBatchNormGatherStats | 2 | 57.45 | 72.95 | +15.50 | 10 | 4 | 2 | 145.90 |
| PReluGradUpdate | 1 | 56.56 | 78.01 | +21.45 | 14 | 8 | 2 | 78.01 |
| **合计** | **23** | —— | **752.33** | **+325.49** | **231** | **87** | **43** | **1717.11** |

> 回执列合计 231，与 `receipts.json` 的 233 相差 2 条：SSG 的 `e635a6c`/`121c9c4` 两条 Pass 缺 `submitted` 时间戳且 `score=None`，无时序无分数，未计入任何统计。

读法：**加权贡献与"投入回执数"不成正比**——PRelu（×1）用 14 发换来 78.01，SyncBN（×2）用 10 发只换来 72.95；而 SSG 用 43 发换来 3×72.45。若按"每发回执的加权分"看，PRelu 5.57/发最高，SSG 15.16/发（绝对值高因为基数高）。【推断】这解释了为什么 WSPT 排序在本场"看起来对"：×3 题即便每发效率一般，绝对加权收益仍最大。

**跨题 vs 单题汇总**（对本文所有"成立"条目）：真正 **≥3 题复用** 的只有三条——64B direct-GM tiling（3267c09 / 7919dcb / 03998e4）、约数核梯队的"禁用 10 核"部分（9c943d3 / 0aa17ab / 31affc6）、同源重投噪声（21 组、10 题全覆盖）。其余全部是 **单题证据**：零 TPipe（3 题但均与其它改动同发，无法隔离）、瘦 ELF（1 题有效）、`TransDataTo5HD`（2 题）、PMU 闭环（0 题可复现数值）。

## 3. 反例与证据错配（照做但没涨 / 涨分来自别处）

| # | 反例 | 事实 | 被削弱的是哪条结论 |
|---|---|---|---|
| R1 | PadV3Grad `be05c5b` | 本地 16 桶 1.4–2.2x，线上 **43.62 → 42.45**（回执 6b030be5，`estimated=47.84`）。且"172KB `.o`"其实是 **`.text`=172332B**，量在 `a72cbe9e9c` 的 run 里，不在 be05c5b 的 run 里（该 run 无 `KERNEL_BINARY_STATS`） | "本地大桶加速 ⇒ 线上涨"、"肥 `.o` 是主因"两条都只有间接证据 |
| R2 | StridedSliceGrad `734e097` | 照"半核"做哑核 `SetBlockDim(halfCores)`+early-return，线上 **59.74 vs 71.57，−11.83** | 杀手锏 4 的"半核更好"跨题推论 |
| R3 | LayerNormV3 `b676b33` | 剪 48 份死实例化、编译更快，分数 **71.37 < 9773dff 73.65** | 杀手锏 3 的"瘦 ELF 涨分" |
| R4 | LayerNormV3 `ec0867d` | **同一 SHA、同一 `queued_at`（12:17:03.673813）产生两条回执**：49.49 与 65.45；差值 15.96 被计入突破阶梯（`breakthroughs_raw.md:37`） | "突破阶梯=代码贡献"；同时是"σ≈2.7"的重尾反例 |
| R5 | ConcatD `e799eb6` | 同 SHA 23 分钟内 **−3.95 再 +1.67**（`scoring-rules.md:2.2`） | "改一个变量→看得见分差"在 <1σ 时不可判读 |
| R6 | ConfusionTransposeD `8e7ba6b` | 三发 78.18 / 74.39 / 79.08；而文档把"`.text` 减 60%"记在它头上——`.text` 瘦身实际发生在 `881eda0/f12039f`，而这两个提交**不是 8e7ba6b 的祖先**（分支不同源），且**无任何回执** | `templates/snippets/index.json` 里 `SNP-noinline-cold-pad` 的 evidence 条目（"79.08 cutting .text >60%"）是错配 |
| R7 | MultilabelMarginLoss `13c554d` | +10.55 被 `HW-16kb-aiv-l1i-hot-packing` 归因于 L1I 紧凑排布，但 tip commit 是 "inline vnchwconv suffix-sum + zero-WAR CompareScalar/Maxs + TBuffAddr MakeUb"，无 `.text` 相关改动；回执 note 只写 `.o=-1.1%` | "16KB L1I 铁律"是**可测量机制、弱分数证据**：SSG `a031e4e` 回执明确报 `_0+_1+_2=10.54KB L1I`，而它的增量只有 **+0.25** |
| R8 | PadV3Grad `37fd777` | 原文 "**+9.09** 借 EXPLORE 挽救"，但 +9.09 = 51.54 − **42.45（回退版）**；以回退前真基线 43.62 计应为 **+7.92** | 基线选择偏差：以回退版本为基线会系统性夸大下一发 |
| R9 | 突破阶梯整体 | 87 条中 **64 条（74%）Δ<5.0**，低于自家归档阈值（`skills/problem-develop/SKILL.md:18`）；**43 条（49%）Δ<1σ=2.7**，合计仅占总涨幅 10%（42.57/444.95）；而 Δ≥10 的 12 条占 **57%**（252.61/444.95） | 阶梯的"条数"半数是噪声，但"总幅度"稳健 → 只能引用聚合值，不能引用单条小增量 |

## 4. docs/ 五个专题文档核实

| 结论 | 出处 | 证据 | 判定 | 备注 |
|---|---|---|---|---|
| 优化前必须先把 workspace 从默认 16MB 降到 ≤1–4KB，否则每例固定罚 650–2050us | `patterns.md:3` | 无回执/无 diff 直接对应；但 `ACL 507035` 类的 guard 在模板 `SNP-direct-gm-tiling-64b.h` 里被写成强制警告 | **无证据** | 与"5 公开例仅 3–10 分"叠加后，650–2050us 的罚会让分数接近 0，机制上合理 → 仍是【推断】 |
| 判题机"编译+50 例"总时限约 7 分钟（420s） | `debugging-and-escape-postmortems.md:11` | cec9fed 两次回执：400s Pass（67.03）→ 420s TLE（0） | **成立** | 原文称"提交后直接 TLE"，隐去了同一 SHA 先通过的事实 |
| `LayerNormV3 cec9fed` 因 81 份实例化触发 TLE，剪到 33 份后 −23s | `postmortems.md:11-15`、`PERF-EXP.md:39` | cec9fed 回执（TLE）；b676b33 commit "fix cec9fed TLE (prune 48 dead mixed-dtype ... ~2x faster compile)"；但 LayerNormV3 的 **60 个 run 全部没有 `KERNEL_BINARY_STATS`** | **部分成立** | "81→33、−23s" 无法从留存证据复现（无实例化计数）；"~2x faster compile"是 commit 自述 |
| `PadV3Grad be05c5b` 172KB 肥 `.o` 导致 3-run `+1.8us` 冷启税，线上 43.62→42.45 | `postmortems.md:29-32` | 回执 6b030be5(42.45)；`.text`=172332B 在另一 SHA；`COLD_START_STATS` 存在于 153 份日志但无"+1.8us"的可追溯实例 | **部分成立** | 跌幅成立，归因链断两处（数值错位、冷启增量无来源） |
| `SyncBN V27 10 核` 冷启前两发 9us DVFS 双峰，V38 取消后 5.26→3.96us、72.26→72.95 | `postmortems.md:34-43` | 回执 73046fe(72.26) / 9c943d3(72.95，commit "optimal C<=512 1-active / C<=2048 2-active split ... -1 PipeBarrier") | **部分成立** | 回执差 +0.69 在 1σ 内；5.26→3.96us 无逐例数据留存 |
| `QuantUpdateScatter 6d2f1df` 是"线上事故"，死锁卡死 Die 0 八分钟 | `postmortems.md:45-49` | **无回执**（该 SHA 从未上判题）；主控原话：Track-B 的 `arena_cache.py replay` 跑完第 139 例后 AICore 死锁、阻塞后面 4 个算子。[证据: 95eafe6a#1474] | **被推翻（定性错误）** | 是**本地验证事故**不是线上事故；180s 看门狗与 `walk_model.py` 的防线是真实收益 |
| 多账号竞态二次重发已由 `eff6912`/`c07b126` 修复 | `postmortems.md:51-57` | 两个 SHA 无回执、无法定位 diff；但 `ec0867d` 在 **11-22** 仍出现同一 `queued_at` 双回执（49.49/65.45） | **被推翻（仍发生）** | 去重键必须含 `queued_at`；`receipts.canonical` 按 `submission_id` 去重**恰好保留了两条**，这在方法论上放大了噪声样本 |
| `-corr` 反相关桶（PadV3Grad 224×224 r∈[−0.21,−0.35]）、温漂 `+0.45~+1.65us`、δ=2.70 Huber | `local-bench-methodology.md:11,38` | 未找到 `bench_feedback.json`/`cases_perf.json` 任何实例（仓库无 `bench-specs/`）；9 题 `calibration.json` 有 `beta_tightness/transfer_gamma` 但无桶级 `r_k` | **无证据** | 校准器的 LOOCV 已在第 1 节给出：方向级可用、排序级无效 |
| 3-Phase 解耦 + `-j4`（`-j8` 会 192/192 全错）+ `180s` 看门狗 + 唯一判据 `EXTENDED_RC=0` | `multi-agent-workflow.md:13-18` | 主控原话记录 6d2f1df 死锁后加 `180s` 看门狗并立即疏通 4 个算子队列 [证据: 95eafe6a#1474]；`validation.log` 里精度判据字段存在 | **成立** | 这是全部文档中最"工程化"的一节，机制与排障口径都可复查 |

## 5. skills/ 核实（CATALOG 20 个，深读 6 个）

深读：`problem-develop`、`problem-submit`、`ascendc-tiling-design`、`ops-theoretical-performance-estimation`、`ascendc-perf-optimize`、`ops-profiling`。

| 结论 | 出处 | 证据 | 判定 | 备注 |
|---|---|---|---|---|
| 12h/6 发无进展就换实现 | `skills/problem-develop/SKILL.md:17` | 同第 2 节：应触发 5 题、实际 0 次；但"重写架构"行为确实发生（PadV3Grad 37fd777 H-First 重写、SSG f41d864 3KB 入口） | **部分成立** | 行为对、触发机制不存在。这是"执行了精神、没执行规则" |
| 突破归档阈值 Δ≥5.0 | `SKILL.md:18` | 87 条中 64 条 Δ<5（74%），43 条 Δ<2.7 | **被推翻（未执行）** | 归档闸门事实上失效；`BT-*` 目录里 38 个条目，与 87 条阶梯不成比例 |
| 提交从宽："精度过就提，不等'确定变快'" | `SKILL.md:20` | 与门禁 `G2 Noise Floor`（`multi-agent-workflow.md:39`，`Δ<1.9–2.7` 即 `HOLD_LOW_GAIN`）**直接冲突**；42 条门禁日志全部 `HOLD_LOW_GAIN` | **被推翻（规则冲突）** | 11-10 前后"从宽"为主（218 Pass / 270），11-22 后门禁收紧；两条规则同时存在会让 worker 无法判断 |
| 协议类改动必须数百次 bit-identical | `SKILL.md:19` | 回执 59f8065 note "sentinel handshake, 2860 runs bit-identical, 129 cases" | **成立** | 唯一有"重复次数"写进回执的条目 |
| Tiling 技能按算子类目路由，跨代际结论不可迁移 | `ascendc-tiling-design/SKILL.md:3-30` | 文档自述；未被本场任何回执检验 | **无证据** | 属上游通用知识，本场未产生该类证据 |
| 理论性能估计（Roofline/OpModel）给"延迟下界" | `ops-theoretical-performance-estimation/SKILL.md:20-27` | 本场无一处回执引用 Roofline 下界 | **无证据** | 本场实际替代品是 msprof 逐桶 + 与 torch 对拍 |

## 6. 模板现有字段 vs 成功那场实际承载的信息

### 6.1 模板字段清单

| Pillar | front-matter | index.json 附加 | 正文结构 |
|---|---|---|---|
| breakthrough | id, problem, difficulty, sha, baseline_sha, timestamp, score_before/after, delta_raw/weighted, status, category, tags, title, bottleneck, mechanisms[], precision_guard, transferable_rule, doc_path | —— （仅 schema.json，无 entries/） | 由 doc_path 指向外部档案 |
| hardware | id, pillar, portability, status, confidence, bottlenecks[], regimes[], dtypes[] | legacy_id, category, tags, prior_alpha/beta, wins/fails, **evidence[]**（workspace / problem / sha / delta_raw 或 delta_us / verdict / note / recorded_at）, related_snippets, doc_path | `## 1 铁律` / `## 2 布局` |
| snippets | 同上 | 同上 | 代码头 `// Why:` + 头文件 |
| community | 同上 | 同上 | `## 1 技术` / `## 2 SoC 验证` |

`category` 枚举只有 7 个：`simd-transpose / async-pipeline / separable-folding / deterministic-sync / vectorization / tiling-regime / dma-coalescing`。

### 6.2 模板缺的字段（与成功那场实际用到的信息逐项对照）

| # | 缺的字段 | 成功那场对应物 | 缺失后果 |
|---|---|---|---|
| 1 | **`mechanism_confirmed: yes/no/confounded`** | `postmortems.md` 的"现象/根因/修复"三段式；反例见 R6/R7 | 无法表达"这条卡片的机制其实没被证实"——现存 12 条 evidence 里至少 3 条属此类 |
| 2 | **`counterexample[]`（反例与失效域）** | `HW-dummy-halfcores`（−11.83）、`HW-10core`（−1.30us）、b676b33（瘦身掉分） | 只有"win"计数，没有"照做没涨"的正式位置 → 反例只能靠人记 |
| 3 | **`n_draws` / `same_sha_repeat` / `sigma_pts`** | 21 组同 SHA 重复、pooled σ=3.04、门禁 sigma 3.51/4.12 | 无法判断 evidence 里的 `delta_raw` 是否大于噪声；`confidence` 是贝叶斯先验（0.875 = prior 2.0 + 5 wins）而非测量 |
| 4 | **`epoch_sha` / `board_snapshot_at`** | `scoring-rules.md` 的三锚、`baseline_sha`（仅 breakthrough 有） | 跨代 delta 不可复核；本场 2 题最优 SHA 不在 main 这类事实无处记录 |
| 5 | **`trigger`（可测触发条件）** | "`.o`>64KB"、"`C<=2048`"、"payload 64KB–2MB"、"r_pub − r_hidden > 15" | 现只有 `regimes`（S_SMALL_CORE…）这种粗桶 + `bottlenecks` 标签，不足以决定"什么时候用" |
| 6 | **`cost` / `risk`（代价与风险）** | 64B tiling 的 ACL 507035 边界、哑核的 −11.83、transdata 的 bank 染色成本 | 模板只能写收益，不写"要付什么" |
| 7 | **`per_case_delta`（逐桶/逐例数字）** | "17 桶中位 110.18→88.36us"、"32/32 胜 torch"、"936.86→103.54us" | evidence 只有单点 `delta_raw`；本文档多处需要逐例数据才能判"成立" |
| 8 | **`repro`（复现命令/配置/环境）** | `opsb bench diagnose`、`opsb test`、msprof 口径、`-j4`、3-run | 违反 `AGENTS.md:7`"保存运行配置、环境、原始测量及来源" |
| 9 | **`postmortem{现象,根因,修复,永久防线}`** | `postmortems.md` 7 大事故的四段结构 | 模板里 anti-pattern 只有一个 `status` 值；事故的"防线"（看门狗、bit-identical、`-j4`）无处安放 |
| 10 | **`applicable_problems[]` + 复用结果** | 同一张"约数梯队"卡在 4 题被复用；"64B tiling"在 3 题被复用 | 无法统计"跨题复用过的卡"，也就无法给卡片升级置信度 |
| 11 | **`category` 缺 `scoring-law` / `measurement`** | 本场 87 条阶梯里，能改变决策的头部经验有一半是**计分/度量律**（5:45 分解、σ、重投噪声、latest 清零） | 这类知识无家可归 → 只能塞进 `community` 或丢失 |
| 12 | **`retracted_by`（作废指针）** | 10 题表 3 行被 11-23 超越；`1637.34` 与 `752.33` 两个口径并存 | 无法在不删历史的前提下标记"此数字已被替代" |

## 7. 缺的知识条目（12 张卡片草稿）

> 入选标准：有回执或可复算数据 + 未被任何模板承载 + 可跨题复用。凡"文档写了但无证据"一律放入 §8。

**C1 同源重投的分数噪声地板 ≈3 分，单发判读无效**
- 适用：所有题、所有"改一个变量→看分差"的决策
- 做法：任何 A/B 结论要求 |Δ| ≥ 3 分（≈1σ）；关键决策对同一 SHA 连提 2 发取均值；Δ<3 的"突破"只记为候选
- 证据：21 组同 SHA 重复 pooled sd=**3.04**（df=29）；spread 中位 2.51、max 15.96（ec0867d 49.49 vs 65.45，同 `queued_at`）；`scoring-rules.md:242` 独立给出"≳3 分才值得一发"
- 代价/风险：2 发/决策 → 额度消耗翻倍；本场 50 发/天/账号，10 题 × 每轮 2 发会挤压探索
- 置信度：0.9｜pillar：**community**（建议新增 `measurement`）

**C2 5:45 分解：公开例隐含分系统性高估官方分 8–20 分（逐题不同）**
- 适用：任何"公开例全绿/公开例加速"后的涨分判断
- 做法：用 `public_times_us/public_tbest_us` 算公开隐含分 `P`，再解隐藏隐含分 `H=(50·S−5·P)/45`；`P−H` 是该题的"公开例陷阱深度"
- 证据：63 发复算，`P−H` 中位 **+16.00**，IQR 11.82–19.76；逐题中位：SSG 20.19 / PadV3Grad 19.53 / MML 14.47 / CTC 14.24 / CTD 13.65 / SyncBN 11.58 / PRelu 8.56 / ConcatD 7.95
- 代价/风险：仅在本场 50 等权结构下有效；`P` 依赖平台返回 5 个公开时间
- 置信度：0.95｜pillar：**community**（`scoring-law`）

**C3 公开例 tbest 冻结，公开例不能当优化目标**
- 适用：赛程中段判断"该优化哪些桶"
- 做法：先测公开例 tbest 是否在 24h 内变化；不变则公开例只用于回归验证，涨分预算全部投隐藏型（大/中/非对齐/奇异 dtype）
- 证据：PadV3Grad 16 发 20 小时 `public_tbest` 逐字节相同（sum=17.907）；`scoring-rules.md:55,177`
- 代价/风险：容易把"公开例 5 个"当成全部回归面 → 需另建全谱用例
- 置信度：0.95｜pillar：**community**

**C4 同源重投不得计入突破阶梯（伪突破）**
- 适用：经验归档、战绩汇总、模型训练样本
- 做法：归档前校验 `(title, sha)` 是否已存在回执；同一 SHA 的第二次回执只能标 `restore`/`redraw`，delta 记 0
- 证据：`ec0867d` 同一 `queued_at` 两条回执 49.49→65.45，被记为 **+15.96** 突破（`breakthroughs_raw.md:37`）；ConcatD `474dc59b` 同为 "restore best validated SHA (76.10)" 得 77.96
- 代价/风险：需要保留原始 `queued_at`，不能只按 `submission_id` 去重
- 置信度：0.9｜pillar：**bugs**

**C5 基线必须是"回退前的真峰值"，用回退版当基线会夸大下一发**
- 适用：任何出现过回退/WA/TLE 归零后再涨分的题
- 做法：维护 `best_ever` 与 `board_current` 两条线；delta 一律对 `best_ever` 计算
- 证据：PadV3Grad `37fd777` 记 +9.09（对 42.45 回退版），对真基线 43.62 应为 **+7.92**；PERF-EXP 三处使用该算法
- 代价/风险：`best_ever` 本身可能是噪声高点（见 C1）
- 置信度：0.85｜pillar：**bugs**

**C6 机制归因必须用"相邻已交 SHA 的树 diff"，禁止用 tip commit message**
- 适用：审计任何"某 SHA 做了 X"的叙述
- 做法：`git diff <prev_submitted_sha> <sha> --stat`；`merge-base --is-ancestor` 确认机制提交是否在该树里
- 证据：`11bd996` 标题是一行 AlignUp 修复、树 diff 494 行；`7919dcb` 标题是测试脚本、树 diff 2120 行；`8e7ba6b` 不含 `881eda0`（不同源）却被记 `.text` 瘦身功劳
- 代价/风险：需要维护"已交 SHA 序列"，本场只能从回执重建
- 置信度：0.9｜pillar：**community**（审计方法）

**C7 校准模型的能力边界：方向可用、幅度不可用、排序不可用**
- 适用：任何用本地预测分数做提交/回滚决策的场合
- 做法：只把 `sign(Δ)` 喂给门禁；点值必须标注 ±RMSE；禁止用预测分差排序候选
- 证据：10 题 `calibration.json` LOOCV `dir_acc` 中位 **0.721**（range 0.55–1.0）、`spearman` 中位 **0.284**（含 −0.066）、`rmse_pts` 中位 **4.95**
- 代价/风险：方向准确率 0.72 在 2 分类上仍优于随机，不应整体弃用
- 置信度：0.9｜pillar：**community**（`measurement`）

**C8 `estimated_score` 相对官方分系统性偏高 1.3–1.6 分（中位），均值不可用**
- 适用：判读 `opsb` 返回的预测分
- 做法：用中位数偏差校正，不用均值；把清零样本单列
- 证据：63 对 `estimated_score`×`score`：全体中位 **−1.57**、均值 −5.18、区间 −76.36…+12.81；剔除 3 条清零（TLE/TLE/RE，est≈70→0）后中位 **−1.34**、均值 −1.83；36 对高估 / 19 对低估 / 8 对 ±1 内
- 代价/风险：中位偏差 1.34 < σ=3.04，**校正收益小于噪声**，实际意义是"别把预测分当证据"
- 置信度：0.85｜pillar：**community**

**C9 瘦 ELF 的收益上限是编译/TLE 余量，不是分数**
- 适用：模板实例化爆炸、`.text` 逼近 32KB I-cache、判题机 420s 临界
- 做法：瘦身作为**安全措施**归档；若要主张涨分，必须给出同代 `KERNEL_BINARY_STATS` + 逐桶数据
- 证据：`cec9fed` 400s Pass → 420s TLE（同 SHA）；`b676b33` 剪 48 份实例化后 **71.37 < 73.65**；SSG `a031e4e` 报 `_0+_1+_2=10.54KB L1I, tmpl=32` 而增量仅 **+0.25**
- 代价/风险：临界 TLE 可能被"剪枝后仍临界"掩盖
- 置信度：0.8｜pillar：**hardware**（补 `HW-16kb-aiv-l1i-hot-packing` 的反面）

**C10 判题 TLE 是临界现象：同一 SHA 会在 400s 通过、420s 超时**
- 适用：编译时长接近红线的任何提交
- 做法：一旦某 SHA 编译耗时进入 350s+ 区间，**不要重投同一 SHA 去"再试一次"**；先剪实例化再重投
- 证据：`cec9fed` 两条回执（67.03 Pass / 0 TLE），间隔 7 分钟；`b676b33` 才修复
- 代价/风险：`latest` 计分下这次重投直接把该题打到 0
- 置信度：0.9｜pillar：**bugs**

**C11 收官纪律：`latest` 计分下，最后一发是唯一有效发**
- 适用：赛程最后 12 小时
- 做法：①每题的 `main` 必须指向"最高分 ∩ 可编译 ∩ 本地全绿"；②收官前禁止在同一题试新结构；③失败发会立即把榜面清零，需立即补一发
- 证据：`scoring-rules.md:21,242-253`（latest、350 条 WA 挂 0 分）；CTCLossV2 `a1df9d5` 是 **RE**，而它正是该题 `main` 的 HEAD；10 题中 2 题最优 SHA 不在 `main`
- 代价/风险：为"补一发"消耗额度
- 置信度：0.95｜pillar：**community**（`scoring-law`）

**C12 "按当前 r 排序投入产出"这一排序可用，但"应停止投入"的判断需要重算**
- 适用：多题并行时的算力分配
- 做法：用 `difficulty × (100 − r_hidden)` 排序前 N 题；但"边际递减、停止投入"的结论要按最终涨幅复核
- 证据：11-10 的 `scoring-rules.md:206-233` 推荐低 r 三题（PadV3Grad/LayerNorm/CTC）最终涨幅 **+33.4/+46.5/+43.7**（10 题中第 5/1/2）；被判"递减、应停"的三题（ConcatD/PRelu/SyncBN）**+31.2/+22.6/+15.6**（第 6/8/10）
- 代价/风险：结论只在"tbest 冻结、自己还在改善"时成立
- 置信度：0.8（n=10，事后单样本）｜pillar：**community**

### 7.1 结构性结论：现有 5 pillar 装不下本场一半的有效经验

12 张卡里 **9 张**只能落 `community`，因为没有"计分/度量"类目。这不是归类洁癖问题：C1/C2/C3/C8/C11 全部是**决策律**，它们决定"这一发该不该提"，比任何 kernel 技巧都更直接影响分数（本场 87 条阶梯里 74% 是噪声级，而一条"别在噪声内提交"的律令就能省下大量额度）。建议 pillar 或 `category` 枚举至少增加 `scoring-law`、`measurement`。

## 8. 待验证（文档有说法，证据不足，**不得**写成卡片）

| 说法 | 出处 | 缺什么 |
|---|---|---|
| 48-Bank/16-BankGroup 6-Coloring 消 ~82% bankgroup 冲突 | `patterns.md:102`、`PERF-EXP.md:41` | 无冲突计数/`ResourceConflictRatio` 前后对比留存 |
| 温控漂移 `+0.45~+1.65us` 与"4 不变量"抵消 | `local-bench-methodology.md:11,32-34` | 无 `bench_feedback.json`/漂移日志实例 |
| PadV3Grad 4D `224×224` r=−0.21…−0.35 反相关；`2d_4x4_small` r=+0.79 | `local-bench-methodology.md:10`、`patterns.md:118` | 无桶级相关系数文件；`calibration.json` 只到题级 |
| "PMU 定位 PRelu `aiv_scalar_time 7.39us` > `mte2 7.21us`" | `patterns.md:109` | 留存日志无该对值；`aiv_scalar_time` 只在 16 份日志出现且不含 PRelu |
| "MML 每次 label 迭代 4×UB 读写 vs 3×计算" | `patterns.md:110` | 无 UB 流量计数 |
| `LayerNormV3 81→33` 实例化、编译 −23s | `PERF-EXP.md:39`、`postmortems.md:13` | LayerNormV3 全部 60 个 run 无 `KERNEL_BINARY_STATS` |
| "空核派发税 ~0.12us/block（1/32/48 blocks = 1.82/5.66/6.88us）" | `patterns.md:80` | 无该微基准的留存日志 |
| "线上 5 个公开用例全线卡在 5.3–5.6us（best 3.55）" | `postmortems.md:30` | be05c5b 的 outcome 行**没有** `public_times_us` 字段 |
| `urllib3` 超时误标"本地拦截"、`sync_error` 隔离 | `postmortems.md:51-57` | `eff6912`/`c07b126` 无回执、无法定位 diff（`sync_error` 状态本身在队列里确有 2 条） |
| 总榜 `#3/789`、`1637.34/2300` | `PERF-EXP.md:3` | 无留存榜单快照；留存 `score-snapshot.json` 为 11-25 陈旧刷新 |

## 9. 与 `AGENTS.md` 第 8 条逐步对照

第 8 条要求 worker 完成"可复查的迭代"：查规则与历史失败 → 选同代基线 → 从分桶证据提假设 → 改一个变量 → 同输入复测 → 交 SHA → 回执检验假设 → 调整下一轮。

| 步 | 要求 | 本场实际做法 | 判定 | Parhelion 现在缺什么 |
|---|---|---|---|---|
| 1 | 查规则与历史失败 | 有：`ESCAPE_LOCK` 把 dfa784f 的 WA 固化成 12/12 复现（`postmortems.md:25-27`）；`opsb breakthrough list` 在 `problem-develop:5` 被要求先查 | **做了** | 缺"历史失败 → 本题当前禁用动作"的结构化索引（现在是散文 + 一些 lock） |
| 2 | 选同代基线 | 部分：门禁有 `contemp`/`gamma` 折算（回执 note 里 `same-SHA ... contemp=[73.39/73.64]`），但 `baseline_sha` 只在 breakthrough schema 里 | **半形式** | 缺"本题当前基线 SHA + 代际快照时间"的一等字段（C5/C3）；`main` 与基线分叉无人校验（2 题最优不在 main） |
| 3 | 从分桶证据提假设 | 有：`Pairwise-v2 (N buckets [k/k spec] vs ...)` 出现在 40+ 条回执里，分桶数 13–48 | **做了** | 缺"假设文本 + 预测方向 + 预测幅度"的字段；现在的 note 只有桶统计，没有假设句 |
| 4 | 改一个变量 | **基本没做**：多数提交是"树级大改"（494–2120 行）+ 同时改分核/编码/流水 | **形式化** | 缺"单变量契约"：`parhelion_test` 应能强制声明"本轮唯一变量"，并在回执里校验多变量 |
| 5 | 同输入复测 | 部分：`opsb test` 固定用例与 3-Phase 口径（`multi-agent-workflow.md:13-16`），但**无重复抽样**；`scoring-rules.md:4.2` 自己提出"同一 SHA 5 分钟内连提两次"这个实验，**没做** | **半形式** | 缺 `n_draws` 与"同 SHA 重测"接口（C1）；缺"本地复测 ≥2 次取中位"的门禁 |
| 6 | 交 SHA | 做了且很规范：233 条回执、SHA→回执一一对应 | **做了** | —— |
| 7 | 用回执检验假设 | 弱：回执 note 里 95% 是 `Simplex-MAP/Pairwise-v2` 的**预测**统计（beta/gamma/trust/UCB），不是"检验"结果；真正检验只有事后人工复盘 | **形式化** | 缺"假设 vs 实际"配对表：`hypothesis_id` + `predicted_effect` + `observed_delta` + `verdict(pass/fail/inconclusive)`。`parhelion_submit_gate` 已有 `hypothesis_id/predicted_effect` 入参，但没有回填闭环 |
| 8 | 调整下一轮 | 部分：确有"重新探索/回退/换架构"（37fd777 重写、ba4a357 回退修复），但触发靠人判断；6 发熔断应触发 5 题、实际 0 次 | **形式化** | 缺"失败 → 强制换向"的自动闸门（C4/§2）；缺 `retry_conditions` 落库 |

**一句话**：第 8 条的 8 步里，**4 步真做了**（1、3、6，半步 2/5），**3 步是形式**（4、7、8）。缺的不是提示词里的措辞——`parhelion_submit_gate` 已经有 `hypothesis_id`/`predicted_effect` 字段——而是**回填**：提交后把 `observed_delta` 写回同一 `hypothesis_id`，并在下一轮把它作为 `retry_conditions` 的输入。没有回填，第 7、8 步永远只能是人工散文复盘。

**另外两处提示词级冲突需要裁决：**
1. `problem-develop:20`"提交从宽（精度过就提，不等确定变快）" vs 门禁 `G2`"Δ<1.9–2.7 即 HOLD"——同一条流水线上两条相反指令。
2. `problem-develop:18`"突破阈值默认 Δ≥5.0" vs 实际归档 64/87 条低于 5.0——归档闸门事实上没有执行，`BT-*` 目录（38 条）与阶梯（87 条）数量都对不上。

## 10. 交接要点

- **给 R1/R2（事实与时间线）**：本文所有分数均已对 `receipts.json` 复核；`PERF-EXP` 10 题表是 **11-22 19:42 快照**，有 3 行已被 11-23 回执超越（MML/PadV3Grad/SSG）。总量请用 **752.33 裸分 / 1717.11 加权**（历史最优和），不要用 1637.34（榜面分）。
- **给 R3（本地估计 vs 官方分）**：见 §附录 A，63 对已复算；结论是"中位偏高 1.3–1.6 分是系统性的（剔除清零样本后中位几乎不变），但均值 −5.18 完全被 3 条清零样本拖偏"。另可引用 §6.2 C7 的 10 题校准器 LOOCV。
- **给 R5（工具缺口/平台侧）**：最能立刻产生收益的三个工具缺口是 C1（同 SHA 重测 + `n_draws`）、C4（重投不得记突破）、C11（收官前 `main`=最优可编译 校验）。

## 11. 审阅清单（可直接勾选）

对着 `PERF-EXP.md` / `docs/` / `skills/` 逐项过，任一项打不出勾就说明该条不能当决策依据：

**总纲级**
- ☐ 这条结论有回执编号吗？（本文 87 条阶梯中 43 条拿不出 >1σ 的证据）
- ☐ delta 是对 `best_ever` 算的，不是对回退版算的？（R8）
- ☐ 引用的 SHA 是"机制所在树"还是"最后一小步"？（§0 方法论纠正一）
- ☐ 数值是 `.o` 还是 `.text`？是逐例中位还是单例？（R1）
- ☐ 结论写的是"该题成立"还是"跨题成立"？跨题有几题回执？（§2.1 汇总）

**提交决策级**
- ☐ 本次 Δ 是否 ≥3 分（1σ=3.04）？低于就只作候选。
- ☐ 是否已对同一 SHA 有第二次抽样？（`scoring-rules.md:4.2` 提出但没做）
- ☐ `latest` 计分下，这次失败会不会把榜面清零？回滚锚点在哪？
- ☐ 编译时长是否进入 350s+ 临界区？（C10：同 SHA 会 400s 过、420s 斩）

**归档级**
- ☐ `(title, sha)` 是否已有回执？重投不得记为突破（C4）。
- ☐ Δ≥5.0 吗？（`problem-develop:18` 的归档阈值，实际 74% 未达标）
- ☐ 反例写了吗？（哑核 −11.83、瘦身掉分、10 核尖峰）

**模板/平台级**
- ☐ 卡片里有 `n_draws` / `sigma_pts` / `counterexample` / `repro` 吗？（§6.2 缺字段 1/2/3/8）
- ☐ 卡片归到哪个 pillar？如果只能进 `community`，说明缺 `scoring-law` 类目（§7.1）。
- ☐ 假设有 `hypothesis_id` 并回填 `observed_delta` 吗？（§9 第 7 步）

## 附录 A：`estimated_score` vs 官方分复算（按主控要求补算，供 R3 引用）

数据：`results/submit-queue-outcome.jsonl` 原始 270 行（**未去重**），其中同时有 `estimated_score` 与 `score` 的 **63 对**；失败面为 Pass 218 / skipped 26 / rejected 9 / TLE 7 / WA 6 / RE 2 / sync_error 2。

| 口径 | n | 中位 | 均值 | 区间 |
|---|---:|---:|---:|---|
| 官方 − 估计（全体） | 63 | **−1.57** | −5.18 | −76.36 … +12.81 |
| 剔除 3 条清零（TLE 69.29 / TLE 76.36 / RE 70.64 → 官方 0） | 60 | **−1.34** | **−1.83** | −24.21 … +12.81 |
| 高估（<−1 分） | 36 | —— | —— | 剔除清零后仍 33 |
| 低估（>+1 分） | 19 | —— | —— | 剔除清零后仍 19 |
| 命中（±1 分内） | 8 | —— | —— | 剔除清零后仍 8 |

**结论（可直接引用）**：
1. **"预测偏高"是系统性的，不是被归零样本带偏的**——去掉 3 条清零样本后中位只从 −1.57 变到 −1.34，方向与量级都没变。
2. 被归零样本带偏的是**均值**：−5.18 → −1.83，三条样本贡献了约 3.35 分的均值位移。【推断】任何用 `mean(score−est)` 汇报的口径都应剔除失败发。
3. 但校正 1.3–1.6 分**没有实践意义**：它小于同源重投噪声 1σ=3.04（§2）。真正的结论是"预测分只能提供方向，不能提供幅度"，与 C7/C8 一致。
4. 与失败分类表对齐：270 行原始行里 Pass 218 / skipped 26 / rejected 9 / TLE 7 / WA 6 / RE 2 / sync_error 2；本文 §1、§3 引用的失败案例全部落在 TLE(2)/WA(3)/RE(2) 之中，`skipped` 与 `rejected` 不消耗判题额度（`scoring-rules.md` 与队列逻辑一致），因此计算偏差时只应取有 `score` 的 231 行。
