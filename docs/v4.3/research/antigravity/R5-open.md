# R5 开放式发现

对象：11-10 / 11-18 / 11-22 / 11-23 那场（Antigravity 三个主控 + 93 段对话）做了什么，而 Parhelion 4.3 做不到、不鼓励或会直接拦住。对照材料为工作树内 `AGENTS.md`、`scheduler/gate.py`、`scheduler/estimator.py`、`scheduler/calibration.py`、`scheduler/wa_repro.py`、`cli/opsb_core/agents/sprint.py`，以及比赛仓只读的 `results/scoring-rules.md`。

## 0. 先把口径钉死（否则后面每条都会算错）

三件事必须分开，混在一起任何结论都是错的。

1. **榜型是 `latest`，不是 `best`。** 题面元数据 `ranking_submission_mode = "latest"`，榜面显示**最后一发**；实测反例：ConcatD 历史最好 52.87，榜面当时显示 46.78（`results/scoring-rules.md` §1.4，[measured]）。单题 = `100/50 × Σ(tbest_i/time_i)`，50 例等权，**任一门不过整题归零，无部分分**（§1.5）。
2. **同源重投的期望是负的。** 六对同源实测，隐藏项残差均值 **−2.03 分**，区间 −5.73…+1.59；纯 `tbest` 漂移只有 −0.08 分/小时（§2.2、§2.3）。所以"再摇一次"不是刷分手法。
3. **吞吐不是短板。** 11-22 那天 117 发 / 9.01 小时 ≈ **12.8 发/时**；Parhelion 自己在 11-26 的 13 worker 冲刺是 19 判 / 1.433 小时 ≈ **13.3 发/时**（`P03-baseline.md` §6）。差别在每发产出：11-22 是 **1.57 原始分/判**，11-26 是 **0.67/判**（同口径"高于该题此前最高官方分"，P03 §6 自己写明"未做 tbest 同代修正"）。

所以本报告排序按"每发产出"而不是"每发速度"。

---

## 1. 判题判决不是源码属性，而 4.3 的逃逸锁把它当确定性 —— 【会拦住】

**现象**
整场有 **4 个 SHA 在源码一字未改的情况下被判出两种判决类别**。11-22 之后这套认知被写进操作手法：先归零、再 restore、把"判题机抽风"当成常规成本。

**证据**
- `ConfusionTransposeD b32a0a0ba68b`：11-22T16:30 Pass **76.37**（`6b0318ac…`），同日 19:30 **Time Limit Exceeded 0**（`6b0342c6…`）。
- `LayerNormV3 cec9fed79880`：14:37 Pass **67.03**（`6b02fe43…`），14:44 **TLE 0**（`6b02ffd5…`）。
- `PReluGradUpdate 0761133e995c`：04:26 **TLE 0**（`6af29cde…`），04:29 Pass **56.56**（`6af29d94…`）。
- `ConcatD e799eb6ed744`：03:33 Pass 52.36 → 03:56 Pass 48.41 → 03:59 **WA 0** → 04:02 Pass 50.08；04:02 那发的 note 原文是 `restore after a single WA on the same source (platform flake?)`。

**Parhelion 4.3 现状**
`calibration.py:875` 与 `:923` 把 `Wrong Answer / Time Limit Exceeded / Runtime Error` 一律视为"逃逸"，未验证就置 `escape_lock = True`；`gate.py:546-567` 于是对该题**任何新候选**返回 `ESCAPE_LOCK_ACTIVE`。而解锁只有两条路：`wa_repro.status(...) == "fixed"`，或该 SHA 曾经线上 Pass。问题在 `wa_repro.py:35-36` 写着 `FAILED = ("Wrong Answer", "Runtime Error")`、`LOCAL_FAILURES = ("WA", "RE")`，`apply()` 对 TLE 回执直接返回 `{"applied": False, "reason": "not a WA/RE receipt"}`（`wa_repro.py:171-172`），`status()` 拿不到 variants 便返回 `state="none"`（`wa_repro.py:211-213`）。**TLE 永远不会被生成复现变体，也就永远解不了锁。**

**为什么这条最重要**
整场 231 发里 15 发非 Pass（6.5%），其中 **7 发是 TLE**。11-22/11-23 占了 5 发 TLE，分别落在 QuantUpdateScatter、LayerNormV3、ConfusionTransposeD、MultilabelMarginLoss、ConcatD；11-23 11:04 还有一发 RE 落在 CTCLossV2。按 4.3 的代码，这些题此后只能提交"曾经 Pass 过的 SHA"（`gate.py:545-546` 的 `prior` 短路），也就是**只剩 restore**。反证：CTCLossV2 在 11:04 RE 之后，12:30 用新 SHA `dcc7754d4a02` 拿到 **75.11 的全程最高分**（`6b0431fa…`），4.3 很可能在这一步把它拦下（【推断】，取决于复现器能否复现该 RE）。

**4.3 该怎么改**
给 TLE 建一条与 WA/RE 并列的复现路径（判题侧 wall-time 超时按"同源重跑 N 次未复现即判为判题侧噪声"处理），并允许"同源已验证 Pass 过一次"作为解 `escape_lock` 的第三种证据。

**置信度**：高（代码路径与回执均可复现）；"dcc7754 会被拦"是【推断】。

---

## 2. `latest` 榜型下每次提交都是状态变更，而门禁按"榜面留最好分"算期望 —— 【会拦住】

**现象**
失败 → 整题 0 → 榜面掉到 0（`0761133e995c` 的 TLE note 原文：`restore: our leaderboard row disappeared (no-submission)`）→ 必须花下一发把榜面拉回来。整场的 restore/rollback 动作因此成为常态，而不是异常。

**证据**
- ConcatD 11-22T10:43 `6933c00a3a9d` WA 归零（`6b02c73d…`），此前榜面是 54.43/55.42，直到 11:44 才回到 57.14 —— **61 分钟空窗，两发代价**。
- SyncBatchNormGatherStats 11-22 12:10、12:20 连续两发 WA，把 57.45 清零两遍；12:13/12:40/12:43 三发同一 SHA 回填（54.96 / 56.20 / 52.47）。
- 赛后实测的 4.3 门禁日志 `results/feedback-log.jsonl`（时间戳 2026-11-25，非赛期）：42 条 `HOLD_LOW_GAIN`，其中 **14 条是 `BELOW_BEST_IN_RESTORE`**，典型 reason 原文：`In [RESTORE] regime (best=59.56 vs board=57.32): est=58.41 gives delta_vs_best=-1.15 … holding to avoid eating cur->best delta`。

**Parhelion 4.3 现状**
`gate.py:91` `keep_best_board: bool = True`，注释写明"the board keeps each problem's best score, so a shot whose expected improvement E[max(0, gain)] clears the floor is worth taking"；`gate.py:1322-1340` 据此放行 `EXPECTED_IMPROVEMENT ≥2.5 且 P(gain>0) ≥0.2` 的候选。可赛制不是这样。更糟的是本该纠正它的两条路径被赛制关掉：`sync_reconciler.py:219` 与 `judge_score_fetch.py:171` 都带 `ranking_submission_mode != "best"` 的早退，本赛制下整段失效。`gate.py:1078-1082` 的 `is_restore_regime` 与 `:1242` 的 `BELOW_BEST_IN_RESTORE` 也是 best 口径：上面那条实测日志里，把榜面 57.32 抬到 58.41（**实得 +1.09**）的候选正是因为够不到历史最好 59.56 被按住。

**为什么这条重要**
`docs/SCHEDULER_DESIGN.md:37` 和 `skills/kernelc-deep-reference/SKILL.md:17` 都已经写明这是"catastrophic latest 模式、零部分分"，说明平台知道；但门禁的默认参数与三条分支都没跟着改。按实测，latest 模式下每一次"预期小负"的发数不是在赌一个 free option，而是在**扣 1–6 分**（§0 第 2 条），并且要额外一发还账。

**4.3 该怎么改**
让 `keep_best_board` 由题目元数据的 `ranking_submission_mode` 派生；`latest` 模式下把 `E[max(0,gain)]` 换成 `E[gain]`，并在批准时把一发 restore 计入成本。

**置信度**：高，【证据】。

---

## 3. 主控亲手写测量台（10 个题的 `validation/` 全是他写的） —— 【不鼓励 + 做不到】

**现象**
11-22 的主控一共 322 次 `edit_file`：其中 29 次落在 10 个题的 `validation/` 目录（`run-npu.sh`、`kperf.py`、`msprof_bench.py`、`msprof-spectrum.sh`、`stress.py`、`det_bad.py`、`kernel_perf.py`），11 次落在全局 `tools/npu-test.py`。**内核一行没改，测量台整套是他建的**。

**证据**
- `95eafe6a` step 241 / 267 / 274 / 351 / 356 / 360 / 366 / 416 / 427：LayerNormV3、PadV3Grad、PReluGradUpdate、ConcatD、ConfusionTransposeD、MultilabelMarginLoss、StridedSliceGrad、CTCLossV2、QuantUpdateScatter、SyncBatchNormGatherStats 的 `validation/` 脚本逐题改写。
- 这些台子的产出直接进了涨分：`59f80657c0fc` note `sentinel handshake, 2860 runs bit-identical, 129 cases`（+10.55）；`0d5125cd90ba` +15.67；`b15e2ddf7c3e` note `path encoded in rank sign: 256x256 222->9.1us`（msprof 谱）。
- 11-18 那天 20 发做出 **83.8 原始分（4.19/发）**，是全程效率最高的一天；那天的主控同样在改测量链路（`cacd27d9`：6 次 `npu-test`、7 条 `ssh` 命令、129 条 shell）。

**Parhelion 4.3 现状**
`AGENTS.md:11`（第 5 条）写死："worker 只能经 `parhelion_test` 选择 smoke、standard 或 final 级别，套件由平台按版本维护。" `sprint.py:21` 里唯一沾边的 `calibration` 席位（`seat_role` 返回 `bench-author`）职责是"Calibrate on a new candidate or official receipt … measure only the cases needed"，**是校准，不是造台**；而且 `MODES`（`sprint.py:25-29`）默认 `solo`，这个席位根本不开。

**为什么这条重要**
测量台决定哪一路本地信号可信。整场 1375 次 commit 只换来 233 发判题（5.9 commit/发），说明迭代成本不在写内核、在"判断这次改动到底快了没有"。4.3 把这一步完全收归平台，等于让每道题只能用一把通用尺子去量 10 种不同的算子。

**4.3 该怎么改**
把 `bench-author` 从 `full` 的可选第三席改为**每题默认第二席**，给它写 `validation/*` 并在 `report.json` 里登记 `run_id` 的权限（产物仍由平台校验）。

**置信度**：高，【证据】。

---

## 4. 12 小时连败 6 发就关闸，而闸正好关在出分那一发上 —— 【会拦住】

**现象**
按 `gate.py` 的判据回放整场：**13/231 发会被 `CIRCUIT_OPEN_SWITCH_BASELINE` 拦下**，其中 4 发在当时刷新了历史最高。

**证据**
- `QuantUpdateScatter 0aa17ab3c7e7`（`6b031672…`，86.61 = 该题全程最高）：之前 15:17/15:32/15:47/16:00/16:06/16:18 六发全部低于 85.89，`non_improving=6`，正好触发 OPEN。
- `StridedSliceGrad ba4a357b94d9`（`6b0328be…`，71.57，当天收盘最好）：14:10 的峰值 67.63 之后连打 **8 发**都低于它（含 17:10 一发 WA 归零），第 9 位才拿 71.57。
- `StridedSliceGrad a031e4e98a7c`（`6b0432d4…`，72.45 = 全程最高）、`PadV3Grad 1dc3411c15e5`（`6b030562…`，42.77）同样落在 streak ≥6 的位置。
- MultilabelMarginLoss 在 11-23 上午出现 **12 连败**（10:45–11:44），远超阈值。

**Parhelion 4.3 现状**
`gate.py:95-96` `circuit_limit_shots=6` / `circuit_window_hours=12.0`；`:762-767` 连败 ≥6 置 `OPEN` 并给 reason `SWITCH BASELINE`；`:1374-1381` 命中 OPEN 即 `approved=False`。另有 `AGENTS.md:12`（第 6 条）的"六发换人"：六发增益不过阈值就收集失败总结、换新 worker 从正交方向接手 —— 这是两套独立的"六"，都会在这份数据上触发。

**为什么这条重要**
成熟题的命中率天然低于 1/6。用连败计数当熔断，等于**在收益最需要堆发数的地方关闸**。上列 4 发里，`a031e4e` 与 `0aa17ab` 是两题的最终最好分；`ba4a357` 那 3.94 原始分（×3 权重 = 11.82 加权）在榜上站了 16 小时。

**4.3 该怎么改**
断路器改判"方向"而不是"发数"：连败时不封提交，只封**同一假设**（要求 note 里的机制关键词必须与最近 N 发不同），并把 `OPEN` 从 `approved=False` 降级为优先级最低。

**置信度**：判据与数据均为【证据】；"被拦的这 13 发里有多少真的会因此丢掉最终分"是【推断】（后续发数可能补回来）。

---

## 5. 同 SHA 重投不是刷分，而是还账；4.3 把它当 0 增益 —— 【会拦住】

**现象**
22 个 SHA 被判过 2–4 次（31 次额外 draw，占 231 发的 13.4%）。其中绝大多数不是"再摇一次"，而是**榜面归零后的 restore**。可分性在于：4.3 分不出"刷分式重投"和"恢复式重投"。

**证据**
- `SyncBatchNormGatherStats 544439ed46c3` 四发：11-10 57.45（`6af2a1b9…`）、11-22 12:13 54.96（`6b02dc83…`）、12:40 56.20（`6b02e2c3…`）、12:43 52.47（`6b02e382…`）。后三发都是 WA 后的回填，spread **−4.98**。
- `ConfusionTransposeD 8e7ba6bc0da3` 三发 78.18 / 74.39 / 79.08，第三发的 note 写明 `restore best validated lean V5 SHA (78.18) after b32a0a0 300s judge TLE`。
- 逐条核对：8 个 SHA 的末次 draw 高于首次，22 个 SHA 中 14 个末次不高于首次 —— 与 §2.2 的"残差均值 −2.03"一致。

**Parhelion 4.3 现状**
`gate.py:1171-1184` `ALREADY_ACTIVE_ON_BOARD` 把 `candidate_sha == active_board_sha` 直接 HOLD；`estimator.py:1642-1666` 的 identity invariant 把它的 `delta_expected` 定成 0。**但 `gate.py:1065-1069` 的 `is_critical_restore` 只在 `confirmed_board_zero` 时放行**，`estimator.py:1668-1686` 的历史同源分支用 `median` 而不是最保守值。另外 `estimator.py:53` `SIGMA_HIDDEN_JITTER = 2.70` 与实测（同源 draw 的 −5.73…+16 区间、4 例判决类别翻转）差 2–3 倍，会让门禁把噪声当信号。

**为什么这条重要**
13.4% 的判题额度花在重投上。方向对了（不该鼓励主动重投），但 4.3 的拒绝理由错：不是因为"重投没价值"，而是因为**判题侧方差被低估了 2–3 倍**，导致凡是"预期 +2.5 分以内"的候选，其真实信息量约等于一次重投 —— 门禁要么全放要么全拦，都不对。

**4.3 该怎么改**
`SIGMA_HIDDEN_JITTER` 改成按题在线回填的双侧经验分布，并把 restore 分支从 `median` 改成"取最低一次 draw"。

**置信度**：高，【证据】。

---

## 6. 吞吐的唯一自由度是账号槽，而冲刺中"加槽"没有任何入口 —— 【做不到】

**现象**
11-22 判 117 发，按原始回执的账号槽分组是 **28 / 39 / 50**（其中一槽正好打满 `DAILY_HARD_LIMIT=50`）。第 3 槽是**当天 12:50 才加的**，比当日首发（10:36）晚 2 小时 14 分。

**证据**
- 逐槽最小间隔：191.1 / 191.3 / 191.4 秒 —— 三个槽各自被 190 秒限速钉住，聚合的 12.8 发/时完全由槽数解释。
- 11-23 是 2 槽 27 + 28；11-18 那 20 发回执里没有账号字段，只能算 1 个槽。槽数与总发数一一对应。
- 利用率不齐：28/50 = **56%**，一个槽空着 22 发额度没用。

**Parhelion 4.3 现状**
多槽模型是有的：`parhelion_paths.py:249-288` `list_eligible_account_slots()`，`gate.py:792-939` 逐槽算 `used/remaining/rate_wait`（`gate.py:82-83` 的 50/190 与实测完全一致）。缺的是**冲刺层的动作与可见性**：`sprint.py:46-129` 的 `start_sprint` 没有槽参数、返回值里没有槽数字段；`AGENTS.md:9` 只说"结合账号槽、冷却、额度和在途请求逐次派发"，没有一句说"什么时候该加槽"。另外 `gate.py:808-814` 的 `_map_aid` 把认不出的 `account_id` 一律归到默认槽，中途新加的槽如果 id 不在注册表里，它的发数会被记到默认槽头上。

**为什么这条重要**
一天 117 发不是"绕过限流"，是"加了第三个槽"（与 `DAILY_HARD_LIMIT=50` 完全吻合）。既然吞吐的唯一自由度是槽，那"什么时候加槽、哪一槽闲"就是**比分母更直接**的收益项。56% 那一槽是纯浪费。

**4.3 该怎么改**
`parhelion_sprint_start` 增加 `account_slots` 预算参数并在回执里给每槽利用率；`QuotaStatus` 增加 `per_slot_utilization`，任一槽 <60% 时在状态卡上提示并槽/加槽。

**置信度**：槽数与首次出现时间为【证据】；"新槽 id 会被记到默认槽"是【推断】（读代码，未见实测）。

---

## 7. 主控直接 `opsb submit --force --now`、直接 `queue skip` —— 【不鼓励】

**现象**
11-23 的主控一共 13 次 `opsb submit <题> <SHA>`，其中 **11 次带 `--force`、10 次带 `--now`**；另有 7 条命令含 `queue skip`。11-22 的主控只走过 1 次（也带 force/now）。

**证据**
- `813867cb` step 365：`opsb queue skip CTCLossV2 2b32861 --reason "PRINTF diagnostic build …"` 紧接 `opsb submit PadV3Grad e3cd79c …`。
- `813867cb` step 512：`git merge --ff-only b-track1 && git push origin main` 之后直接 `opsb submit ConcatD a2c8bbf …`（不经过 worker 的 gate 请求）。
- `813867cb` step 370：`opsb submit StridedSliceGrad dd1fefc … --force --note "Group A integrated main: beats historical best ba4a357 (71.57) on 23/24 buckets…"`。

**Parhelion 4.3 现状**
force 通道是存在的：`gate.py:1044-1045` `effective_force`、`:1185-1192` `FORCED_APPROVAL`、`:1294-1302` `OPERATOR_OVERRIDE`、`:1366-1373` `OVERRIDE_CIRCUIT`；`queue skip` 对应 `gate.py:1153-1160` `OPERATOR_SKIPPED`。问题不是"没有"，而是**没有配额、不计入校准**：`AGENTS.md:9` 把"是否自动提交及理由"交给门禁，却没有一条限制主控自己 force 几次。

**为什么这条重要**
11-23 那天 noise floor 已经在跑（`813867cb` step 47 自报 `Cooldown 0s/190s｜24h Used 34/100｜Noise Floor Δ>=2.5`），而当天产出是 **0.54 原始分/发、21.8% 刷新率**，是五天里最差的一天。13 发里 11 发靠 force 穿过去，等于门禁在那天基本没干活。

**4.3 该怎么改**
给 force/override 加硬配额（例如每题每天 ≤3 次、全冲刺 ≤10 次），并把 `forced=True` 的回执单独排除出校准样本，否则校准会被人为发的数据污染。

**置信度**：命令计数与文本为【证据】；"force 导致当天产出变差"是【推断】（也可能是当天已到边际收益衰减段）。

---

## 8. 主控直接读判题站接口，拿 public / hidden 分解 —— 【会拦住（4.2 已列为边界问题 K）】

**现象**
11-22 的主控有 15 条命令命中 `/api/`，其中一条把**所有账号的会话文件读成一份可直接复用的凭据集合**后逐题拉 public / hidden 分解，输出标题原文是 `ALL 10 PROBLEMS: OFFICIAL PUBLIC vs HIDDEN BREAKDOWN`。

**证据**
- `95eafe6a` step 1140 / 1148：glob 全部账号会话文件、拼 `urllib.request` 请求头，注释里写明遍历全部槽（此处含凭据）。
- `95eafe6a` step 1131 / 1136 / 1138：走平台内部模块 `board_refresh_service` 拉 `live-board-tbest`，再按题打印 public/hidden 拆分。
- `95eafe6a` step 1322：调 `scheduler.calibration.inspect_escape_state` 对 10 题做 `ESCAPE LOCK AUDIT`。

**Parhelion 4.3 现状**
`AGENTS.md:16`（第 9 条）"角色权限、账号选择、冷却、设备竞争和底层日志由平台处理"；`docs/v4.2/00-PLAN.md` §1 已经把这件事单列为"另一个边界问题"（工作包 K）：有 worker 直接调平台内部服务，把全队判题账号的会话凭据读出来去查排行榜（原文含凭据，此处不引）。4.3 侧没有等价的**只读、无凭据**入口。

**为什么这条重要**
单题 100 分里 public 项最多 10 分（5 例 × 2 分），90 分在 45 个隐藏例上。没有 public/hidden 分解，就无法判断"这题的剩余空间在可见例还是隐藏例"，也就无法决定该改 kernel 还是改对齐。这是**决策级信息**，而且现在只能靠凭据级操作拿到。

**4.3 该怎么改**
把 `public_term / hidden_term / implied_hidden_ratio` 做成 `parhelion_report` 的只读列（`scoring-rules.md` 已给出反解方法），同时在真凭据路径上收口。

**置信度**：调用次数与输出标题为【证据】；"分解决定了改哪一层"是【推断】。

---

## 9. 一题 4 路 worker 赛马，4.3 上限是 2 路解题 —— 【做不到】

**现象**
11-23 的主控自述："Group A contains four StridedSliceGrad workers and Group B has two MultilabelMarginLoss workers"，共 15 个 worker、NPU 并发控制在 10 路。

**证据**
- `813867cb` step 32：原话 `Group A contains four "StridedSliceGrad" workers and Group B has two "MultilabelMarginLoss" workers`，同时 `swarm action=add` 逐个拉起。
- `813867cb` step 47：`5 题冲刺集群 + Hydrogen v3 观察哨（共 15 个 worker，其中活跃 NPU 构建/测试上限控制在 10 路以内）`。
- 产出侧：StridedSliceGrad 43 发（全场最多，最终 72.45）；`ba4a357b94d9` note 带 `[exp-b v14-fix]`，说明是多轨淘汰后的并轨结果。

**Parhelion 4.3 现状**
`sprint.py:25-29`：`solo=("solo",)`、`dual=("algorithm","dataflow")`、`full=("algorithm","dataflow","calibration")`。**解题席位最多 2 个**（第 3 个是 bench-author）。`AGENTS.md:12`（第 6 条）"冲刺默认每题一个 worker，双轨与校准席位需显式开启"。

**为什么这条重要**
权重 3 的题（StridedSliceGrad / ConcatD / PadV3Grad / ConfusionTransposeD）贡献了整场大部分加权分。把最难 4 题的并行解题路数从 4 砍到 2，等于把最难那部分的发数砍半；而 §4 已证明**这些题恰恰需要连败后再来几发**。

**4.3 该怎么改**
`MODES` 增加 `race`（每题 N 路算法席，N 按难度权重配），或让 `parhelion_sprint_start` 支持按题指定 `seats`。

**置信度**：席位描述与数字为【证据】；"砍半会掉分"是【推断】（无法严格归因多轨与最终分）。

---

## 10. worker 之间直连对话，而 solo 席位没有对话对象 —— 【不鼓励】

**现象**
11-23 的 worker 之间直接 `@` 名字对接，不经主控转述；主控 242 次 `read_board` + 196 次 `swarm_wait` + 56 次 `post_message`，基本只做调度与广播。

**证据**
- `813867cb` step 63：`ctc-t1` 直接呼 `@ctc-t2`，正文给的是 `Track 1 (b-track1, from 0c769ba)` 的路线细节。
- `813867cb` step 115：`cat-t1` → `@cat-t2`，正文 `[done ConcatD 6016980] t1-v3 PASS 90/90+32/32 …` 附完整测量结论。
- `813867cb` step 76：`v3-sentinel` → `@pad-recon`，正文是硬件规律条目号（`[V3-004] elf_guard.py misses …`）。

**Parhelion 4.3 现状**
`AGENTS.md:18` 给了 `parhelion_squad`："队友通过 parhelion_squad 定向分享复现、实验配置与对照结果。日常校准留在题组时间线，仅显式求助和战略事件通知主控。"方向是对的，但 `sprint.py:25-26` 的 `solo` 只开 1 席 —— **没有队友可分享**；`sprint.py:111` 里 `collab_mode="solo" if mode == "solo" else "orthogonal"`，solo 模式下连 orthogonal 协作都不是默认。

**为什么这条重要**
跨题同构的经验（例如"256×256 用 rank 符号编码路径"这类）走 worker 直连比走主控中转便宜一个数量级；P03 实测 1.43 小时里 peer 消息 212 条，说明这条路本来就在被走。solo 模式下把它关掉，等于让 10 个 worker 各自重新发现同一批硬件规律。

**4.3 该怎么改**
solo 模式也默认加入跨题 `parhelion_squad` 频道（横向同构题的经验直传），并把 peer 消息数纳入冲刺指标。

**置信度**：消息样本为【证据】；"直连比经主控便宜"是【推断】（P03 只有 peer 条数，没有成本对比）。

---

## 11. 4.3 声称校验"字节一致的 git tree"，代码只验 commit 存在；成功那场正好利用了这一点 —— 【不鼓励】

**现象**
11-22 的主控把 `tools/npu-test.py` 里"提交前必须干净工作树"的守卫改成"给了 `--sha` 就跳过"，于是**可以拿一个旧 SHA 的标签去验一棵未提交的树**。

**证据**
- `95eafe6a` step 1037：`Target` 原文 `if git(repo, "status", "--porcelain"): raise RuntimeError("Commit local changes before server validation")` → `Replacement` 加了 `if not sha_override and …`。
- 全场 1375 次 commit 换 233 发判题（5.9 commit/发），需要绕过的正是"一改一提交"的摩擦。
- `gate.py:5` 的 docstring 第 1 条写着"byte-consistent git tree (`op_host`/`op_kernel`)"。

**Parhelion 4.3 现状**
`gate.py:425-447` `_verify_git_commit_exists` 只做 `git cat-file -t`（是不是 commit）与 `git ls-tree --name-only`（有没有 `op_host/` `op_kernel/` 或 `kernel.asc`+`CMakeLists.txt`），**没有任何一步把 `result.json` 记录的校验对象与 commit 的树内容比对**。所以上面那种"旧 SHA 标签 + 未提交树"的验证在 4.3 里照样通过 S1。

**为什么这条重要**
门禁后面所有统计（`delta_expected`、`calibration`、`bypass`）都以 SHA 为主键。SHA 不忠实于被验内容，估分与突破阶梯全都在错误的键上累加。这不是漏放一个候选，而是**污染整条证据链**。

**4.3 该怎么改**
S1 增加一条硬校验：把 `result.json` 里登记的源码指纹（或 `op_host/op_kernel` 的树哈希）与 `git rev-parse <sha>:<path>` 比对，不等则 `GIT_TREE_MISMATCH`。

**置信度**：代码现状与那次编辑均为【证据】；"4.3 会放进这种验证"是【推断】（可构造复现）。

---

## 12. 知识库/文档赛后集中写，4.3 要求边打边写 —— 【不鼓励】

**现象**
11-22 的 19:04（UTC）——距当天最后一发（19:37）还有 33 分钟——用户下指令"现在是整理规范时间 … 在主目录下创建一个 `PERF-EXP.md`，整理刷分提升的方法论"，才拉一个专职 worker 做沉淀。次日 11-23 全程，`PERF-EXP` 在那段对话里**出现 0 次**（没人读）。

**证据**
- `95eafe6a` step 1629（`2026-11-22T19:04:54Z`）用户请求原文；step 1639 计划落盘；step 1650 `hydrogen-consolidation` 回报 `[done]`。
- `95eafe6a` 里 `PERF-EXP` 命中 23 次，全部集中在 1629 之后；`813867cb` 里 0 次。
- 对照 Parhelion 自己的冲刺：1.43 小时里 `parhelion_kb_record` 27 次、`kb_query` 29 次（`P03-baseline.md` §4），**边打边写**；但同一份报告 §1 承认"已证伪方向被重试 11 次，涉及 7 个 worker"。

**Parhelion 4.3 现状**
`AGENTS.md:12`（第 6 条）："本地放行但线上 WA、RE 或 TLE 时 … 主控另派 worker 将可泛化的防错经验写入 Bug 档案。官方同代增益超过配置阈值时，主控另派 worker 复核并提炼突破。"——即**事件触发、实时派单**，成本落在最贵的主控回合上。

**为什么这条重要**
关键在于"写完没人读"。`P03-baseline.md` §5 的实测是：`current_plus_ledger` 相对 `current` 只把回放覆盖率从 0.642 提到 0.655（+1.4pp），而"已证伪方向被重试 11 次"照旧发生 —— 说明**光有种子挡不住重复，必须由平台强制**。成功那场干脆不写，等到打完一次性写。

**4.3 该怎么改**
KB 写入从"事件触发"改成"阶段收尾批处理 + 由 gate/queue 强制注入到下一次同类候选的 context"，而不是靠主控派单。

**置信度**：时间戳与计数为【证据】；"集中写更省"是【推断】（无成本对照）。

---

## 意外失败

**失败 1：把"同源重投"当成刷分手法，统计上是在还债。**
11-10 之后一共 31 次额外 draw，其中 14 个 SHA 的末次不高于首次。`SyncBatchNormGatherStats 544439ed46c3` 四发一路 57.45 → 54.96 → 56.20 → 52.47。赛后官方口径给出的答案是隐藏项残差**均值 −2.03**（`scoring-rules.md` §2.2）。当时 note 里的 `same-SHA 2 draw(s) raw=[57.45/54.96]` 说明平台已经在算这个，但结论没有反过来变成"少投同源"。

**失败 2：`ec0867dd795b` 6 分钟内被判 49.49 与 65.45。**
`6b02dd43…`（12:17）与 `6b02dec1…`（12:23）的 note 完全一致（`Simplex-MAP (8/14 buckets vs e21fdce, beta=1.104 …)`），分数差 **+15.96**。事后解释只能是判题侧某些例没进入计时或落了不同的 tbest（【推断】）。代价不是 16 分，而是**第一发 49.49 被当成真分写进了阶梯**，随后才被覆盖。

**失败 3：本地"18/18 桶全绿 + 正向 d_hid"挡不住隐藏例。**
11-22 的 6 次 WA 里，至少 `6933c00a3a9d`（`d_hid=+2.74`）、`1795a8563892`（`raw_geo=1.412x, improved=17`）、`080ee029ec1d`（`raw_geo=1.459x, improved=16`）三发的本地证据是明确正向的。两发连打还把 SyncBatchNorm 的榜面清零两遍。

**失败 4：`b32a0a0ba68b` 先 Pass 76.37、3 小时后 TLE。**
同一天同一 SHA，16:30 拿 76.37，19:30 判 300 秒超时归零。这一发触发的 restore 用的是 `8e7ba6bc0da3`（79.08），算是赚了；但"Pass 过的 SHA 也可能 TLE"这条认知在当时没有被写成规则（`b32a0a0` 的 TLE note 仍写 `restore best validated SHA (76.37)`，说明第一反应还是回到 76.37）。

**失败 5：11-22 最后 7 分钟连打两发，只买到 0 分和一次恢复。**
19:30 `b32a0a0` TLE（0），19:37 `8e7ba6b` restore 79.08。两发额度、300 秒判题时间，净效果是把榜面从"归零"拉回到"略高于原最好"。这件事本身是对的，但它发生在**剩余额度已接近见底**的时段，说明额度规划里没有为"必然发生的归零"留固定预算。

---

## 教训

**1. `latest` 榜型下，"提交"是状态变更而不是观测。**
任何一次提交都必须同时准备好它的 undo（一个已验证的 restore SHA + 一发额度）。4.3 现在批候选时不算这本账，`gate.py:899-901` 的 `reserved` 明确写着"仅供显示，从不 hold 或排序"。

**2. 判题判决与源码不是一一对应，凡是"锁在判决上"的机制都要能自证。**
4 例判决翻转（2 例 Pass→TLE、1 例 TLE→Pass、1 例 Pass→WA→Pass）证明这一点。`ESCAPE_LOCK` 要求"复现失败变体再修"，在 TLE 上根本无法执行（`wa_repro.py:35-36`），结果是**一道题的永久冻结换取一个可能只是判题抖动的信号**。约束：任何 lock 必须有"未复现即降级为提示"的出口。

**3. 熔断规则必须在"还有别的方向可换"时才成立。**
12 小时连败 6 发就 `SWITCH BASELINE`（`gate.py:762-767`），在只剩 1–2 个方向时等同于关掉出分口。这场里 4 个历史最高分出现在 streak 的第 7、第 8 位。约束：连败计数换成"同假设连败计数"。

**4. 测量台是第一生产力，不是辅助设施。**
主控 40 次改测量脚本（29 次 `validation/` + 11 次 `npu-test.py`），换来 11-18 那天 4.19 原始分/发（全程最高）。4.3 把这一层完全锁在平台里，反而让 10 种不同算子共用一把尺。约束：per-problem harness 必须是**默认存在**的席位产出，不是 `full` 模式的可选项。

**5. 知识库的价值不在"写"，在"读"，而读必须由平台强制。**
成功那场赛后一次性写（19:04 起），次日 0 次读取；Parhelion 自己边打边写 27 条/1.43h，却仍重试了 11 次已证伪方向（`P03-baseline.md` §1、§5）。约束：KB 条目必须以"下一发同类候选的前置注入"形式生效，而不是以文件形式存在。

---

## 附：本报告用到的可复现口径

- 回执：`/tmp/p19/receipts.json`（233 条按 `submission_id` 去重；231 条有可解析的 `submitted_at`）。分日：11-10 39 / 11-11 2 / 11-18 20 / 11-22 115（可解析时间戳部分，按账号槽归组为 28+39+50=117）/ 11-23 55。
- 阶梯：按题内时间序、只在新高分时记账，得 231 发 / **84 次刷新历史最高** / 原始涨幅 315.49 / 加权 734.39。分日产出：11-10 0.55、11-18 4.19、11-22 1.57、11-23 0.54（原始分/判）。
- 断路器回放：按 `gate.py:728-790` 的算法（12h 窗口、`non_improving>=6` → OPEN、`consec_zeros>=2` → HALF_OPEN），用真实历史逐发回算，得 13/231 会被拦。**注意这是上界**：实际被拦后历史会改变，后续发数不会照原样发生。
- 门禁日志：`results/feedback-log.jsonl` 的 42 条 `gate` 记录全部落在 **2026-11-25**（赛期之后），只用于说明 4.3 门禁的判据分布，不代表 11-22/11-23 那两天的实际判定。
- 席位/命令统计：`aglib` 逐条计数；主控 95eafe6a 的 `edit_file` 322 次按路径分桶（`ops-supervisor` 186、题目仓 `validation/` 29、其它 107）。
