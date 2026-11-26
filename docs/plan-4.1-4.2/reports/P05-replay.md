# P05 回放：账本内核、自动写入、交接校验、checkpoint 修正（4.2 C #1 #2 #6 #8）

数据与口径同 P03 基线：2026-11-26 冲刺的 13 个 worker（`mesh-dee-*.jsonl`，19 个切点，148 个探针）。日志只读，只经代码打印汇总，本报告不含消息原文。

```bash
S=mesh-dee-07013f.jsonl,mesh-dee-0f97d5.jsonl,mesh-dee-17d6d3.jsonl,mesh-dee-2180de.jsonl,mesh-dee-345d84.jsonl,mesh-dee-43695a.jsonl,mesh-dee-754bf6.jsonl,mesh-dee-7be6aa.jsonl,mesh-dee-a07dea.jsonl,mesh-dee-abcefb.jsonl,mesh-dee-b0c078.jsonl,mesh-dee-ba3f0e.jsonl,mesh-dee-cf7eae.jsonl
opsb replay bench --sessions "$S" --schemes current,ledger_core,full_history --json      # 约 1.5 秒
```

## 1. 结论

- **只靠平台自己写的账本，覆盖率没有可检出的变化。** `ledger_core` 0.649（96/148）[0.574, 0.723]，`current` 0.642（95/148）[0.568, 0.716]。配对比较：`ledger_core` 多覆盖 3 个探针、少覆盖 2 个，差值 +0.007，按 worker 聚类的 bootstrap 95% CI 为 [−0.015, +0.032]，包含 0。
- **账本摘要有一点正贡献，尾部保留在这批日志上没有贡献。** 只把账本摘要接在日志里的原种子后面，是 0.655（97/148），比 `current` 多 2 个探针、没有少（差值 +0.014，CI [0.000, +0.036]）。只做尾部保留、不加摘要，是 0.635（94/148），多 1 个、少 2 个。两者合起来就是 `ledger_core` 的 +0.007。
- **提升的两个探针都是 `idea_repeat`（49/61，原来 48/61）。** 想法来自平台写的假设：门禁请求时的提交说明或 commit 标题成了假设的 claim，进了摘要。文件（31/70，0.443）完全没有变化：文件名不在任何平台事件里，要靠 P06 的 `parhelion_note` 与读文件记录才能进账本。`refuted_retry`（10/11）和 `retest`（6/6）原来就已覆盖。
- **这个回放测不到的收益。** 种子里写了 SHA 与测试结果，重复劳动照样发生（P03 的发现）。P05 让账本在测试和回执时自动成形、交接被校验，但不拦截重复；拦截要靠 P06 与之后的门禁。种子覆盖率本身也不代表行为变化，所以这一步的价值要靠上线后的实时指标（切点后重测次数、首个新实验的延迟）来验。
- **与 `current_plus_ledger` 相当。** 后者 0.655（97/148），比 `ledger_core` 多 1 个探针（`refuted_retry` 11/11 对 10/11；`idea_repeat` 同为 49/61，但覆盖的不是同一批探针），配对 +2 / −3，差值 −0.007，CI [−0.028, +0.016]。

## 2. 对照表

| 方案 | 覆盖率 | 95% CI | file_reread | idea_repeat | refuted_retry | retest | 种子长度中位 / 最大（字符） |
|---|---:|---|---:|---:|---:|---:|---|
| current | 0.642（95/148） | [0.568, 0.716] | 31/70 | 48/61 | 10/11 | 6/6 | 6,794 / 7,693 |
| **ledger_core** | **0.649（96/148）** | [0.574, 0.723] | 31/70 | 49/61 | 10/11 | 6/6 | 7,896 / 9,713 |
| 　摘要 + 原种子（消融） | 0.655（97/148） | [0.581, 0.723] | 31/70 | 50/61 | 10/11 | 6/6 | 7,310 |
| 　尾部保留 + 原叙述（消融） | 0.635（94/148） | [0.561, 0.710] | 31/70 | 47/61 | 10/11 | 6/6 | 6,972 |
| current_plus_ledger（P03） | 0.655（97/148） | [0.581, 0.723] | 31/70 | 49/61 | 11/11 | 6/6 | 8,460 |
| full_history | 1.000 | [1.000, 1.000] | 70/70 | 61/61 | 11/11 | 6/6 | 1,118,495 / 2,014,308 |

配对差（相对 `current`，探针级差值的均值；CI 为按 worker 聚类重采样 2000 次，固定种子 20261126）：

| 方案 | 多覆盖 / 少覆盖 | 均值差 | 95% CI |
|---|---|---:|---|
| ledger_core | +3 / −2 | +0.007 | [−0.015, +0.032] |
| 摘要 + 原种子 | +2 / −0 | +0.014 | [0.000, +0.036] |
| 尾部保留 + 原叙述 | +1 / −2 | −0.007 | [−0.021, 0.000] |
| current_plus_ledger | +2 / −0 | +0.014 | [0.000, +0.032] |

CI 来自 13 个 worker，其中多数没有变化，所以区间窄不代表有把握；结论按「没有可检出的差异」写。

## 3. `ledger_core` 怎么构成，回放能做到什么

种子 = 日志里的原交接叙述 + 账本摘要 + 状态卡与阶段结果尾部（预算 8000 字符）。账本由切点前的事件用**平台自己的写入代码**（`ledger.auto.apply_*`）在内存里重建，不写任何文件：

- `parhelion_test` / `parhelion_test_wait` 的已终态结果（带 `request_id`）→ 一条 measurement、一条 experiment；
- `parhelion_submit_gate` 且 `enqueue` 的调用 → 一条平台假设（claim 取提交说明，没有就取日志里的 commit 标题；预测取门禁的 μ 与 σ）；预览调用不写；
- 提示里的判题行 `<题> <sha7> · <状态> · score <分> · submission <id>` → 回执 measurement，以及对该 SHA 假设的判定（增益取与日志内更早的最高分之差，没有做同代折算）。

切点处账本的规模（19 个切点）：条目数中位 19、最多 76；open/queued 假设中位 1、最多 5；平台记下的失败（experiment 的 worse/WA/RE/TLE/compile_fail 与被证伪的假设）中位 4、最多 22；摘要中位 702 字符、最多 2,394。假设状态合计 open 16、queued 4、confirmed 1、refuted 1、inconclusive 1，即回执太少，大多数假设在切点前没有被判定。

回放的局限：

- 叙述沿用日志里的旧版（4.2 的结构化叙述与校验无法回放），所以这里测的是摘要与尾部保留，不是新交接 prompt。
- 账本里只有平台能写的事实。洞见、文件、模型的结论要等 P06 的 `parhelion_note`。
- 回放里没有状态卡的原始数据，卡取自日志里的原种子。
- 阶段结果取切点前最后一次 `parhelion_agent_report`（kind 为 result、breakthrough、stagnation、escape_zero），没有就取最后一条回复的末尾 3000 字符，与上线后的行为一致。`current` 用日志里实际发出的种子（阶段结果取开头约 2000 字符），所以那 2 个少覆盖的 `idea_repeat` 多半来自「开头」与「结尾」的取舍：长报告里想法若写在开头，尾部保留就会丢掉它。这只是按构造作的推断，没有逐个核对探针。

## 4. 判定常量（`ledger/rows.py`）

| 常量 | 值 | 用途 |
|---|---|---|
| `SPEEDUP_BETTER` / `SPEEDUP_WORSE` | 1.02 / 0.98 | 全部通过的运行，相对基线的几何平均加速比 ≥ 1.02 为 better，≤ 0.98 为 worse，之间为 neutral（同 P03 探针的 0.98） |
| `NOISE_MARGIN` | 0.10 | 计时器读数比该 SHA 自己最好的一次慢 1.5 倍以上（`timing.noisy`）、且加速比与 1 的距离不超过 0.10，记为 noise |
| 失败优先级 | WA > RE/ERROR > TLE | digest 的 `summary.counts` 里有非 PASS 时，取优先级最高的作 outcome；`failed` 的运行只有 `level=build` 或错误文本含 compile/build/nvcc 等才记 compile_fail，其余是平台故障，不记 |
| `CONFIRM_SIGMAS` | 1.0 | 回执判定：增益 ≥ μ − 1σ 为 confirmed（落在 μ±σ 内或更好）；增益 ≤ 0 或 WA/RE/TLE/CE 为 refuted；其余为 inconclusive |
| 增益 | `score × γ − 同代最高分` | 与 `judge_feedback` 的 breakthrough 判断同一算式（新函数 `epoch_gains`）。Pass 但分数未知时不判定，等分数到了再判；没有数值预测（门禁给不出 μ、σ）而增益为正，记 inconclusive |

## 5. 验证

- 新增 39 个测试（`test_ledger.py` 20、`test_ledger_handoff.py` 15、`test_ledger_replay.py` 4），全部用假 adapter 和临时目录：追加与 flock（8 线程 × 20 次加一个子进程 25 次，条目号无缺口无重复）、`supersedes` 折叠与旧 id 解析、各视图、模型不能改平台字段、测试结果与回执的幂等（重放同一批不新增一行）、假设三种判定及边界、`_checkpoint` 保留结尾、交接校验四步与回退、ACP 在旧会话里修复标题、Codex 前置摘要、校验器出错时仍保住压缩。
- `test_orchestration_v4.py` 里原有的 checkpoint 断言按新行为改了（预算 8000，状态卡在前）。
- 全量：根目录 `525 passed, 18 skipped`；`cli/tests runtime/tests` `320 passed`。
- 没有碰真实比赛目录、守护进程、worker 与判题站；回放只读日志，账本只在内存里重建。
