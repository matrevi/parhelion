状态：已交付

# P29 估分排序校准：实测 σ、两档预测与排序质量指标

估分器的同源抖动从常数 2.70 改成按题实测的 `judge_noise.sigma_for`；预测按「同代重测 / 跨代改动」分两档，跨代档的增益先经判题公式的饱和变换、再按本题回执拟合的收缩系数缩小；新增 `scheduler/ranking_metrics.py` 在 P21 的回放上算 Spearman、top-1 命中率与增益误差（都带按题重采样的 bootstrap 区间）。**两场真实回放上，新排序的 Spearman 与 top-1 的置信区间下界都低于现方案**，因此 `ranking_v2` 默认关，只合入测量部分与 σ 替换（00-PLAN §6 第 6 条预期的结果）；σ 替换本身在两场上没有翻转任何一次放行/暂缓，只移动了 22 发（ops-bench）与 1 发（mat-chem）的优先级。

## 1. 分支与提交

分支 `impl/P29-ranking`。基线是 `main`（`0b777a7`，含 P21 回放框架、P22 判题规则、P23 意图与 restore 定价、P24 S1 体检与逃逸锁出口、P26 席位、P27 派活情报、P28 知识库证据等级）。开工时用的是主会话指定的集成基分支 `integ/base-P23`（`8c5a414` = `main` + `impl/P23-submit-intents`），交付前按主会话要求两次 rebase 到 `main`（`31cd89e` → `0b777a7`），下面所有数字都是 rebase 后在 `0b777a7` 上重跑的。

```text
$ git log --oneline main..HEAD
<deliver> Deliver P29: measured jitter, two prediction tiers and ranking quality metrics
e5f7fb6 Measure the judge jitter, the two prediction tiers and the ranking metrics
```

第一条是带着本报告的那个提交（提交号由提交动作本身决定，无法写进自己的内容里，所以留 `<deliver>`）。`e5f7fb6` 是全部代码与测试。

```text
$ git diff --name-only main...HEAD
cli/opsb_core/commands/replay_cmd.py
cli/opsb_core/replay/gate_replay.py
cli/tests/test_replay_gate.py
scheduler/calibration.py
scheduler/cli_bridge.py
scheduler/estimator.py
scheduler/gate.py
scheduler/pairwise_anchor.py
scheduler/ranking.py
scheduler/ranking_metrics.py
scheduler/tests/test_ranking.py
```

diff 只含本包文件。没有 `Sources/` 下的改动，也没有 `mcp/`、`docs/v4.3/` 里除本报告以外的文件（`RUNBOOK.md`、`00-PLAN.md`、`10-DSH.md`、各包子计划、`reviews/`、`AGENTS.md` 均未动）。

| 文件 | 行数 | 说明 |
|---|---:|---|
| `scheduler/ranking.py`（新） | 338 | σ 取值与回落、分档判定、饱和变换、收缩拟合、`adjust` 接入 |
| `scheduler/ranking_metrics.py`（新） | 247 | Spearman / top-1 / 增益误差与按题重采样 bootstrap |
| `scheduler/tests/test_ranking.py`（新） | 381 | 24 条测试（测试文件不受 350 行限制） |
| `scheduler/estimator.py` | +43 / −13 | 11 处常数换成实测 σ、`ranking` 字段、`adjust` 接入（**薄**） |
| `scheduler/gate.py` | +13 | 两个 `GateConfig` 开关 + 把它们传给 `estimate`（**薄**） |
| `scheduler/calibration.py` | +8 / −1 | `predict_candidate` 透传 `jitter_sigma`（**薄**） |
| `scheduler/pairwise_anchor.py` | +8 / −3 | `total_sigma` 用传入的 jitter，不再直接用常数 |
| `scheduler/cli_bridge.py` | +3 | 历史回算的 `estimate` 与门禁用同一组开关 |
| `cli/opsb_core/replay/gate_replay.py` | +20 / −2 | 两个开关组、逐发记录 `priority`/`predicted_*`/`tier`、`--ranking` 块 |
| `cli/opsb_core/commands/replay_cmd.py` | +32 / −2 | `--ranking` 参数与文本输出 |
| `cli/tests/test_replay_gate.py` | +25 | 1 条新测试 |

两个新模块分别 338 与 247 行，均在 350 行以内；`estimator.py` 只加接入点（没有重排 P23 的意图/restore 定价逻辑，也没有改既有预测路径的判定顺序）。

## 2. 子计划逐项完成情况

### 2.1 σ 用实测值 — 已完成

`scheduler/ranking.jitter_sigma(workspace, title)` 的三级回落就是子计划要的顺序：`judge_noise.sigma_for` 已经做到「按题取（`df ≥ MIN_DF=3`）→ 全场合并值 → `None`」，取不到时回落到 `SIGMA_FALLBACK = 2.70`（等于 `estimator.SIGMA_HIDDEN_JITTER`，测试钉住两者相等）。

接入点：`ScoreEstimator.estimate(..., jitter_from_noise=True)` 在函数开头取一次 σ，`estimate` 里 11 处用到 `SIGMA_HIDDEN_JITTER` 的 σ 计算全部改为该值（含 `active_board_identity`、`historical_sha_decay_adjusted`、`receipt_per_case`、`case_bench_paired`、`paired_*`、`note_heuristic`、`torch_ref_ratio`、`anchor_fallback`）。**Simplex 标定路径的 σ 也换掉了**：`predict_candidate` 把 `jitter_sigma` 透传给 `evaluate_pairwise_anchor`，后者的 `total_sigma` 与 `loocv_extra` 都用它，否则 ops-bench 上 177 发 `pairwise_simplex_v2` 的 σ 完全不受影响，σ 替换就只剩 1/4 的发数可见。

两场的实测值（`judge_noise`，读本场全量回执）：

| 场 | 全场合并 σ̂ | df | 拿到题级 σ 的题 | 用全场合并值的题 | 回落到常数 |
|---|---:|---:|---|---|---:|
| ops-bench | **3.162** | 26 | 4 题（0.22–6.09） | 6 题 | 0 |
| mat-chem | **0.506** | 5 | 0 题（题级 df 都不足） | 14 题 | 0 |

σ 的回放对比见 §6.4。

### 2.2 两档预测 — 已完成

分档与变换的定义见 §3。落地为 `scheduler/ranking.adjust()`，由 `estimate()` 在路径链之后调用一次。改动规模用 `git diff --numstat`（复用 `ledger/gitref.git_out`，它自带 5 秒超时）判定，任何失败按跨代处理。

两场上分档的实际分布（`ranking` 组，即 `ranking_v2=true`）：

| 场 | 同代重测 | 跨代改动 | 真正做了变换的发数 | 收缩系数中位 | 拟合来源 |
|---|---:|---:|---:|---:|---|
| ops-bench | 16 | 217 | **61** | 0.503 | 题级 47 / 全场 12 / 先验 2 |
| mat-chem | 63 | 15 | **1** | 0.287 | 全场 1 |

「真正做了变换」= 跨代 **且** 这道题的基线 SHA 有逐例时间可读（mat-chem 的 15 发跨代里，14 发是各题的第一发或没有基线，前面没有任何逐例证据）。这条限制是 §8 的「数据不足」之一。

### 2.3 排序质量指标 — 已完成

`scheduler/ranking_metrics.py` 算三项，`opsb replay gate --ranking` 输出，口径见 §4。逐发 Spearman、逐时刻 Spearman、top-1 命中率、增益误差（中位/均值/分档）以及三项的按题 bootstrap 区间都有。子计划允许的「一个时刻只有一个候选时改用逐发 Spearman」也实现了：`spearman_predicted` 是「预测增益 vs 实际增益」的逐发 Spearman，`spearman` 是「门禁优先级 vs 实际增益」的逐发 Spearman，后者在两场都可用（n=177 / n=12）。

另外补了一个**反事实集**（`counterfactual`：不按批准与否过滤，把每一发已判回执都当候选），因为 mat-chem 的 66 发拦截里 42 发是证据格式漂移，批准集只剩 12 发。验收仍按批准集判定（子计划口径），反事实集只作对照，见 §8。

### 2.4 启用标准 — 已完成（结论：不达标）

判据与 P06 相同：新排序的 Spearman 或 top-1 命中率，其置信区间下界高于现方案的点估计，且同一条指标在两场都成立。两场都不成立（§6.5），因此 `ranking_v2` 默认关，只合入测量与 σ 替换。

### 2.5 接入 — 已完成

`estimator.py` 的 diff 是 +43 / −13 行，其中 11 行是常数替换、11 行是接入点、其余是注释与字段；新逻辑全部在 `scheduler/ranking.py`。没有新增任何**暂缓理由**，额度也没有进入任何阈值（本包只动 `estimated_score` → `delta_expected` → `priority_score` 这条链，门禁的四个阶段判定条件一行未改）。

## 3. 分档与变换的定义

### 3.1 分档

`ranking.classify(repo, sha, baseline)`：

| 情形 | 判定 | 理由 |
|---|---|---|
| `sha == baseline` | 同代重测 | `same SHA` |
| diff 为空（内容相同的两个提交） | 同代重测 | `identical trees` |
| diff 只动了非内核源码（文档、脚本、测试、配置） | 同代重测 | `N file(s) changed, none of them kernel source` |
| diff 动了内核源码 | 跨代改动 | `N kernel source file(s) changed` |
| 没有基线 SHA | 跨代改动 | `no baseline to diff against` |
| 仓库缺失/提交缺失/超时/取不到 numstat | 跨代改动 | `diff unavailable (...)` |

「内核源码」= 仓库里 `op_host/` 或 `op_kernel/` 下、后缀属于 `.h/.hpp/.c/.cc/.cpp/.cxx` 的文件——与 `estimator._code_fingerprint` 用的是同一个集合，避免出现两套「什么算内核代码」的定义。**失败一律按跨代处理**：量不到改动不等于没有改动。

基线取该发预测实际依据的 SHA（`base_sha`，回落 `ledger.active_board_sha`/`historical_best_sha`），与预测路径同源。

### 3.2 饱和变换（判题公式）

`ranking.saturate(doc, speedup, anchor)`：

1. `doc` 是这道题某个已判回执的**真实逐例时间**（优先 `public_times_us`/`public_tbest_us`，其次 `time_by_case`/`tbest_by_case`，最后 `case_results`）；基线 SHA 有就用基线的，没有就借同题最新的一份分布形状。
2. **把分布的水平对齐到锚点分数**：`level_shift(doc, anchor)` 二分求一个对数时间平移 `d`，使 `mean_j 100/(1 + ln(t_j/tbest_j·e^d)/ln 1.5) = anchor`。
3. **每个用例自己的时间除以预测加速比**：`predicted = mean_j 100/(1 + ln(t_j/tbest_j·e^{d−ln k})/ln 1.5)`，直接调用 `receipts.case_points`（就是判题脚本用的那条公式），不是把本地比值线性乘到分数上。
4. 增益 = `predicted − anchor`。

边界与单调性（测试钉住）：`k=1` 精确回到锚点；`k` 增大时 `predicted` 单调不减；`predicted ≤ 100`；分布已经全部在 tbest 上时任何加速比都还是 100；`k` 极小时分数跌到下限、但不会为负。

**一处与子计划的偏差（有意）**：饱和读数先被夹到「模型自己那条预测的增益」之内（同号、绝对值不变大），再乘收缩系数。原因有两个：公开例只有 5 个，离散度比 50 例名册窄得多，未经夹取的判题读数在两场上 **有 59/61 次没有被夹取收窄**（§6.2，即模型读数才是更保守的那个），直接用会凭空放大预测；而这一档变换的职责是**去掉高估**，不是制造更大的增益。所以跨代档的实际形式是「模型读数 → 判题公式上限 → 收缩系数」三层，**只能变小**。报告 §6.2 把两个中间量都记下来了，`estimate.ranking` 里也留着 `saturated_gain`/`capped_gain`/`model_is_smaller`，便于复核。

### 3.3 收缩系数

`ranking.shrinkage(workspace, title, as_of)`：

- **拟合样本**：同一题相邻两发已判 `Pass` 回执（都要有逐例时间、分数 > 0）。前一发的时间按它自己的分数对水平、除以这两发之间的几何加速比、用判题公式重打分 → 饱和预测；两发的官方分差 → 实际增益。这就是「判题公式说这一发该涨 X，判题实际给了 Y」。失败发（WA/RE/TLE）不进入拟合：硬失败是 S1 的预测问题，不是排序误差。
- **斜率**：过原点的最小二乘 `Σ p·a / Σ p²`，`p` 是饱和预测增益、`a` 是实际增益；再加一个 **岭项 `SLOPE_RIDGE = 1.0`**（点的平方）——否则「两发时间几乎没变、预测≈0」的一对会把斜率拉到 −52（实测过一次），把整题的预测清零。斜率截到 `[0, 1]`：负斜率等于宣称反向排序，本变换不认。
- **层级回落**：题级要 **≥ `MIN_PAIRS = 2` 对**才说话，否则用全场池化斜率，再否则用先验。
- **先验**：`SHRINK_PRIOR = 1/3.7 ≈ 0.2703`（R3 §1.5 的跨代高估倍数），按 `SHRINK_PRIOR_WEIGHT = 3` 个伪样本与实测斜率混合。
- **只用 `as_of` 之前**：`fit_pairs` 按回执时间戳过滤（不是按文件位置），`as_of` 之后的一律读不到。测试用一条构造的「未来回执」验证：加进去以后 `fit_pairs`/`shrinkage`/池化结果逐字段不变，而 `as_of=None` 时那条会被读到（说明过滤真的生效、不是碰巧）。

### 3.4 最终形式

```text
g_model = estimated_score − anchor              # 模型自己的跨代增益
g_sat   = saturate(该题逐例分布, speedup, anchor) − anchor
g_cap   = min(g_sat, g_model)   （两者同号；负增益时取 max）
g_out   = shrink · g_cap
estimated_score = anchor + g_out                # 只影响 delta_expected 与它喂出的 priority
```

`rank_jitter`（σ）完全不进入这条链，它与 `ranking_v2` 是两个独立开关。

## 4. 指标口径

### 4.1 候选集

- **主口径 = 门禁批准的发**（`approved=true`）：被拦下的发当天不会被派发，不是候选。
- **反事实集 = 全部已判发**：只作对照（mat-chem 必要，见 §8）。
- 两者都要求该发 `score` 不为 `None`（`Pass` 但排名行没给分的 6 发无法算增益，计入 `unpriced`）。
- **实际增益 = 该发判分 − 它前面的榜面**（回放记录的 `score − board_score`），与估分器的 `delta_expected` 同量纲；硬失败按判 0 算，即「把榜面输掉」。`ex_hard` 一栏把硬失败剔除后重算，供核对「指标是不是在被 S1 的活牵着走」。

### 4.2 三项

| 指标 | 定义 | 一个时刻只有一个候选时 |
|---|---|---|
| Spearman（逐发，`spearman`） | 全部候选的「门禁优先级 vs 实际增益」的秩相关 | 仍然可用（子计划允许的替代口径） |
| Spearman（逐时刻，`spearman_moment`） | 按 30 分钟墙钟桶分组（跨题，因为队列是跨题排序的），每桶 ≥2 个候选才算一个时刻，按桶内对数加权平均 | 桶内只有 1 个 → 不计入 |
| Spearman（预测，`spearman_predicted`） | 「预测增益 vs 实际增益」的逐发秩相关，检验预测本身 | 仍然可用 |
| top-1 命中率（`top1`） | 逐时刻：优先级最高的一发，其实际增益是否等于桶内最高（并列最高算命中） | 桶内只有 1 个 → 不计入 |
| 增益误差（`error`） | 预测 − 实际：中位、均值，以及按预测增益分档 `<=0 / 0-2 / 2-5 / 5-10 / >10` 的 n 与均值 | 不适用 |

### 4.3 bootstrap

`cluster_ci`：**重采样单位是题（`title`）**，不是发——同一题的各发共享 tbst 与历史，不独立。1000 轮，`BOOTSTRAP_SEED = 20261126`，百分位区间 95%。逐时刻指标的重采样仍然抽题，每轮把抽到的题的发拼起来重新分桶。

`unstable` 标记：如果点估计落在自己的区间外（聚类 bootstrap 在样本极少时不收敛），该 cell 会带 `unstable: true`，文本输出也会标 `unstable`。mat-chem 的反事实 top-1 就是这种情况（0.125 对 [0.154, 0.667]），见 §8。

### 4.4 优先级口径

用的是**门禁当时的真实 `priority_score`**（`replay` 记录里的 `verdict.priority_score`），不是任何复算公式——因此自动带上 P23 的 probe/restore 定价、P24 的 `health_penalty`（×0.8）与 EXPLORE 半权、熔断建议半权（三者可叠乘）。这也是本报告的优先级数字**不能**与 P21 报告的旧优先级直接对比的原因。

## 5. 测试

```text
$ ~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider
987 passed, 19 skipped, 2 subtests passed in 98.39s (0:01:38)

$ PYTHONPATH=cli:. ~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider cli/tests runtime/tests
642 passed in 68.94s (0:01:08)
```

两条都跑到底，全绿。主会话在 `main`（`0b777a7`）上实测的基线是 **962 passed / 19 skipped** 与 **641 passed**，本包新增 **25 条**：`scheduler/tests/test_ranking.py` 24 条（只进第一条命令）+ `cli/tests/test_replay_gate.py` 1 条（两条命令都进），962+25=987、641+1=642，与实测一致。`runtime/tests/test_device_broker.py` 的两条预算测试本轮全绿，没有触发偶发失败，不需要单独重跑。

**Swift：没有 Swift 改动**（`git diff --name-only main...HEAD -- Sources/` 为空），不需要 `swift build`。`cli_bridge.py` 的字段集合没有改：`ScoreEstimate` 新增的 `ranking` 是**附加**字段，桥接层的卡片 JSON 是显式挑字段构造的，所以 Swift 解码不受影响（`GateVerdict.to_dict()` 的 `estimate` 里会多一个 `ranking` 键，属于附加）。

新增测试覆盖子计划「测试」节的全部条目：

| 条目 | 测试 |
|---|---|
| σ 的取值与回落 | `JitterTest`（题级 → 全场 → 常数三级；`SIGMA_FALLBACK == SIGMA_HIDDEN_JITTER`；两个开关下估计器的 σ 比值等于 σ̂/2.70） |
| 分档判定 | `TierTest` 6 条（同 SHA、内核改动、非内核改动、空 diff、内核源码判定、5 种失败情形） |
| 饱和变换的单调性与边界 | `SaturationTest` 4 条（`k=1` 回锚点、9 个加速比单调、0≤值≤100、tbest 上的分布无余量、零锚点拒绝变换、水平对齐精度 1e-4） |
| 拟合只用 as_of 之前 | `ShrinkTest::test_nothing_after_as_of_is_read`（构造未来回执，加进去前后逐字段相同；`as_of=None` 时能被读到） |
| 指标计算 | `MetricsTest` 5 条（秩与并列、候选过滤、三个指标的已知值、单候选时刻不计入、`compare`/`verdict` 判据） |
| 拟合稳健性 | `ShrinkTest` 另 3 条（岭项、`MIN_PAIRS`、三层回落、只减不增） |

**真实入口**（仓库外、去掉 `PYTHONPATH`，`cwd=/tmp`）：

```text
$ cd /tmp && env -u PYTHONPATH ~/.parhelion/venv/bin/python <worktree>/cli/opsb replay gate \
    -w ~/Competitions/ops-bench --groups default,ranking --ranking
… 每组的 ranking 块正常打印 …  total 71.3s
exit=0
```

`--ranking` 是本包唯一新增的命令行参数（不是 MCP 工具参数），从正式入口跑通、退出码 0。MCP 工具表**未改**（本包没动 `mcp/`）：主控 **14951 / 15000**、worker **12323 / 13000**，与 `main` 完全相同（主控只剩 49 B，本包没有占用）。

## 6. 两场回放：现方案 vs 新方案（含 σ 组）

命令（在工作树内；`--groups` 三组共享同一份副本、同一份回执历史）：

```bash
cli/opsb replay gate -w ~/Competitions/ops-bench            --groups default,sigma,ranking --ranking --detail --json
cli/opsb replay gate -w ~/Competitions/mat-chem-sim-pred    --groups default,sigma,ranking --ranking --detail --json
```

三组的含义：`default` = 4.2.1 + 实测 σ（**现方案 / 本包默认**）；`sigma` = 同一门禁但 σ 回落到常数 2.70（用来量 σ 替换）；`ranking` = 实测 σ + `ranking_v2`（**新方案**）。榜型按规则快照：ops-bench 全部 latest、mat-chem 全部 best；两场都是先复制到临时目录再跑，原目录只读。

### 6.1 门禁不变性核对

先确认本包没有误伤既有行为：`default` 组与 `main` 上 P21/P23/P24 报告的数字逐项一致（ops-bench 233 发、拦 50、新高 18、restore 9/9、收官差额 10.19、points 742.14；mat-chem 78 发、拦 66、新高 35、收官差额 0.00、points 374.94）。`ranking` 组在两场都**没有改变任何一次放行/暂缓**（ops-bench 50→50，mat-chem 66→66），只改了 47 发的优先级（其中 11 发从 `READY` 降为 `EXPLORE`，仍放行）。

### 6.2 ops-bench（latest，233 发 / 10 题）

| 指标 | 现方案 `default`（实测 σ，v2 关） | 新方案 `ranking`（实测 σ，v2 开） | σ 组 `sigma`（常数 2.70） |
|---|---|---|---|
| 可排发数 | 177（拦 50、无分 6、硬失败 10） | 177 | 177 |
| **Spearman**（优先级 vs 实际增益，逐发） | **+0.481 [0.365, 0.587]** | **+0.504 [0.384, 0.619]** | +0.483 [0.370, 0.586] |
| Spearman（逐时刻，32 个时刻） | +0.365 [0.121, 0.529] | +0.376 [0.129, 0.510] | +0.376 [0.150, 0.529] |
| Spearman（预测 vs 实际，逐发替代口径） | +0.502 [0.401, 0.600] | +0.517 [0.407, 0.624] | +0.502 [0.401, 0.600] |
| **top-1 命中率** | **+0.688 [0.581, 0.825]**（22/32） | **+0.625 [0.567, 0.793]**（20/32） | +0.688 [0.581, 0.825]（22/32） |
| 增益误差（中位 / 均值） | 2.70 / 6.57 | **2.14 / 5.79** | 2.70 / 6.57 |
| — `<=0` 档 | −4.12（n=2） | −4.12（n=2） | −4.12（n=2） |
| — `0-2` 档 | +6.25（n=47） | +6.87（n=56） | +6.25（n=47） |
| — `2-5` 档 | +3.90（n=40） | **+1.57（n=50）** | +3.90（n=40） |
| — `5-10` 档 | +8.61（n=48） | +8.53（n=33） | +8.61（n=48） |
| — `>10` 档 | +7.73（n=40） | +8.04（n=36） | +7.73（n=40） |
| 剔除硬失败后 Spearman | +0.518 | +0.542 | +0.520 |
| 剔除硬失败后 top-1 | +0.677 | +0.613 | +0.677 |
| 反事实集 Spearman（226 发） | +0.458 [0.376, 0.523] | +0.476 [0.401, 0.547] | +0.458 [0.378, 0.523] |
| 反事实集 top-1（42 个时刻） | +0.714 [0.611, 0.826] | +0.667 [0.600, 0.800] | +0.714 [0.611, 0.826] |
| 反事实集增益误差（中位 / 均值） | 2.06 / 6.00 | 1.65 / 5.40 | 2.06 / 6.00 |

`ranking` 组的分档与变换统计：同代重测 16 / 跨代改动 217，**61 发真正做了变换**（都有基线 SHA 的逐例时间，`doc_source=baseline`），其中 **59 发的模型读数比判题读数更保守**（即夹取生效、实际用的是模型读数 × 收缩），收缩系数中位 **0.503**，预测增益中位 3.72 → 收缩后 1.78。

读法：**收缩确实把系统性高估压下去了**（均值 6.57 → 5.79、中位 2.70 → 2.14，`2-5` 档从 +3.90 压到 +1.57），逐发 Spearman 也略升（+0.481 → +0.504），但 **top-1 命中率下降**（0.688 → 0.625），且两条指标的区间下界都低于现方案的点估计 —— 判定见 §6.5。

### 6.3 mat-chem（best，78 发 / 14 题）

| 指标 | 现方案 `default` | 新方案 `ranking` | σ 组 `sigma` |
|---|---|---|---|
| 可排发数 | **12**（拦 66、无分 0、硬失败 2、7 题） | 12 | 12 |
| Spearman（逐发） | +0.475 [−0.105, 0.810] | +0.475 [−0.105, 0.810] | +0.482 [−0.082, 0.842] |
| Spearman（逐时刻，3 个时刻） | −0.357 [−1.000, 0.200] | −0.357 [−1.000, 0.200] | −0.143 [−1.000, 1.000] |
| Spearman（预测 vs 实际） | +0.441 [−0.208, 0.882] | +0.441 [−0.208, 0.882] | +0.441 [−0.208, 0.882] |
| top-1 命中率 | +0.333 [0.000, 1.000]（1/3） | +0.333 [0.000, 1.000]（1/3） | +0.333 [0.333, 1.000]（1/3） |
| 增益误差（中位 / 均值） | 4.88 / 8.95 | 4.88 / 8.95 | 4.88 / 8.95 |
| 反事实集 Spearman（77 发） | +0.397 [0.254, 0.549] | +0.397 [0.254, 0.549] | +0.398 [0.254, 0.549] |
| 反事实集 top-1（16 个时刻） | +0.125 [0.154, 0.667] `unstable` | 同左 | 同左 |
| 反事实集增益误差（中位 / 均值） | 3.33 / 9.51 | 3.33 / 9.51 | 3.33 / 9.51 |

`ranking` 组：同代重测 63 / 跨代改动 15，但只有 **1 发**真正做了变换（收缩 0.287，来自全场池化），所以新方案在这一场与现方案**逐项相同**。这一场的数字不能当作「新方案无害」的证据，只能当作「这一场量不出差别」，见 §8。

### 6.4 σ 替换本身的回放对比

| 项 | ops-bench | mat-chem |
|---|---|---|
| `default` 组 σ 中位 / 范围 | 3.77 / 1.98–9.37 | 4.03 / 0.72–5.03 |
| `sigma` 组 σ 中位 / 范围 | 3.67 / 2.70–9.23 | 4.83 / 2.86–5.68 |
| 改变了 σ 的发数 | 231 / 233 | 全部（题级 σ̂ 都取不到，回落到全场 0.506 与常数 2.70 的混合） |
| 被移动的优先级 | **22** | **1** |
| 被判定的发（放行/暂缓） | **0** | **0**（只有 1 发从 `EXPLORE` 升为 `READY`，仍放行） |
| 拦截数 | 50 → 50 | 66 → 66 |
| 三项指标 | 见 §6.2（Spearman +0.483 对 +0.481，其余一致） | 见 §6.3（Spearman +0.482 对 +0.475） |

结论：**σ 替换在两场上都没有翻转任何一次放行/暂缓**。ops-bench 的实测 σ̂（3.16）与常数 2.70 只差 0.46，落在同一个「噪声底线 → σ 只是把 `prob_positive` 与 UCB 微微推动」的量级；mat-chem 的 σ̂ 只有 0.51，本应让门禁更敢排，但那一场的拦截几乎全是 S1 证据格式（与 σ 无关），所以也只动了 1 发的优先级标签。这是一条**中性**结果：替换没有害处，也没有在历史上显出收益。

### 6.5 启用判定

判据（子计划 §4 / 00-PLAN G6）：新方案在**两场**回放上，Spearman 或 top-1 命中率之一，其 **95% 区间下界高于现方案的点估计**。

| 指标 | ops-bench：现方案点估计 | 新方案 CI 下界 | 单场判定 |
|---|---:|---:|---|
| Spearman | +0.481 | **+0.384** | 不达标（低 0.097） |
| Spearman（逐时刻） | +0.365 | +0.129 | 不达标 |
| top-1 | +0.688 | **+0.567** | 不达标（低 0.121） |
| 反事实 Spearman | +0.458 | +0.401 | 不达标 |
| 反事实 top-1 | +0.714 | +0.600 | 不达标 |

| 指标 | mat-chem：现方案点估计 | 新方案 CI 下界 | 单场判定 |
|---|---:|---:|---|
| Spearman | +0.475 | **−0.105** | 不达标 |
| top-1 | +0.333 | **+0.000** | 不达标 |
| 反事实 Spearman | +0.397 | +0.254 | 不达标 |

**没有任何一条指标在两场同时把下界抬过现方案**，因此 `ranking_v2` 默认关。注意这是「证据不足 + 方向不一致」：ops-bench 上逐发 Spearman 略升、top-1 略降，两场的区间宽度都在 ±0.1 量级，即使真有 +0.02 的改善，这套数据也测不出来（见 §8）。

## 7. 开关默认值与理由

| 开关 | 默认 | 理由 | 回到旧行为 |
|---|---|---|---|
| `jitter_sigma_from_judge_noise` | **true** | 子计划 §1 要求把常数换成实测值；两场回放没有翻转任何一次判定（§6.4），风险已量化为 0，且它与 S2 噪声底线用的是同一份测量（口径统一）。 | `false` → 恢复 4.2.1 的常数 2.70 |
| `ranking_v2` | **false** | 达不到子计划 §4 的启用标准（§6.5）：两场、三条指标、五种口径，没有一个下界过线。按 00-PLAN §6 第 6 条，「达不到时开关默认关，只合入测量部分」。 | 本来就是关；打开只影响跨代档的预测增益与它喂出的优先级 |
| 测量部分（`opsb replay gate --ranking`） | 参数，默认不加 | 不加参数时不产生任何 `ranking` 块，回放成本与 P23 时相同（+0） | 不传 `--ranking` 即可 |

两个开关都登记在 `GATE_SWITCHES`，可用 `opsb queue config --gate name=value` 单独设置；`set_switch` 的布尔解析与既有开关一致。回放里两个组 `sigma` / `ranking` 就是这两种翻转，随时可以复现上表。

## 8. 数据不足之处

1. **跨代档真正被变换的发数太少。** ops-bench 217 发跨代里只有 61 发（28%）有基线 SHA 的逐例时间；mat-chem 15 发跨代里只有 1 发。原因是 `public_times_us`/`public_tbest_us` 只在 ops-bench 最后两天（11-22 之后）与 mat-chem 11-24 之后的回执里才有（ops-bench 71/233、mat-chem 96/138 行）。早期回执没有逐例时间，饱和变换就无从算起——按设计这时**不做变换**（保留模型读数），所以「新方案」在赛程前段等于「现方案」。
2. **公开例只有 5 个，离散度比 50 例名册窄。** 这直接导致未经夹取的判题读数几乎总是没有被夹取收窄（59/61，模型读数才是更保守的那个），所以 §3.2 的夹取不是可选项。要真正用判题公式做饱和，需要**全名册**逐例时间（mat-chem 的 `case_results` 就是这种格式，30 行），建议后续包把回执取证补齐。
3. **mat-chem 的候选集只剩 12 发。** 66 发拦截里 42 发是 S1 `REFERENCE_SUITE_REQUIRED`（证据格式漂移：旧回执早于参考套件取证），不是当天的门禁判定（P21 §6 偏差 1、P24 NOTE 5）。这一场的批准集指标 n=12、时刻只有 3 个，区间宽度覆盖整个取值域，**不能用于任何结论**；反事实集（77 发 / 16 个时刻）更可用，但它的 top-1 点估计落在自己区间外（0.125 对 [0.154, 0.667]，已标 `unstable`），说明 14 题 × 16 时刻也不够聚类 bootstrap 收敛。
4. **σ 用的是全场口径，不是时点口径。** `judge_noise` 读整份回执（与 S2 噪声底线同源，P21 的偏差 3 已记），回放里它看到了 `as_of` 之后的重复判题。σ 是噪声属性而非分数，方向上的偏差有限，但这是已知口径，没在包内改（改它属于门禁改动）。
5. **收缩拟合的样本在赛程前段只有 0–2 对。** 上表里 61 发变换中有 12 发落在全场池化、2 发落在先验，题级拟合的 47 发里也有不少只有 2–3 对。`MIN_PAIRS=2` + 岭项 + 3 个伪样本的先验把噪声压住了，但**前几发基本由先验 0.27 决定**，不是这场的实测。
6. **两场的区间宽度都在 ±0.1 量级。** ops-bench 177 发 / 32 个时刻、10 题，按题重采样的 Spearman 区间仍有 ±0.11；要判 +0.02 级的改善，这个样本量不够。这是 00-PLAN §6 第 6 条「P29 可能达不到启用标准」的具体形态。
7. **增益误差本身很大，且不是这一档能修的。** 现方案的均值误差 +6.57（ops-bench）、+8.95（mat-chem），意味着估分器系统性高估 6–9 分；收缩把它压到 +5.79，仍然是同量级。误差主要来自 `anchor_fallback`/`paired_*` 这些没有逐例证据的路径，不是排序档位的事。

## 9. NOTE

1. **`estimate()` 的 σ 替换是「一处取值、11 处替换」**，没有重排 P23 的意图与 restore 定价路径（`gate.py` 的 S2 判定顺序、`submit_intent.plan` 的调用位置一行未动），P23 的 43 条测试与 P24 的 30 条测试在 rebase 后全绿。
2. **Simplex 标定的 σ 也必须换，否则 σ 替换只覆盖 1/4 的发数。** ops-bench 177/233 发走 `pairwise_simplex_v2`，它的 σ 在 `pairwise_anchor.total_sigma` 里由 `SIGMA_HIDDEN_JITTER` 合成。本包给 `predict_candidate` / `evaluate_pairwise_anchor` 各加一个 `jitter_sigma` 参数（默认仍是常数，其他调用方行为不变），由估计器把实测值传进去。`calibration.py` 与 `pairwise_anchor.py` 因此不在「只加薄接入点」的五个文件里，但改动只有参数透传与一个项替换。
3. **两个档位的定义与 `_code_fingerprint` 的内核源码集合一致**（`op_host/` + `op_kernel/` + `.h/.hpp/.c/.cc/.cpp/.cxx`），避免仓库里出现两套「什么算内核代码」。
4. **饱和变换必须夹在模型读数之内**（§3.2）。这是与子计划字面措辞的**有意偏差**，理由是 5 例公开子集的离散度问题，实测 59/61 次夹取生效。`estimate.ranking` 里保留了 `saturated_gain`、`capped_gain`、`model_is_smaller` 三个中间量，复核时不需要重跑。
5. **岭项是被实测逼出来的。** 第一版没有 `SLOPE_RIDGE`，MultilabelMarginLoss 的一个「两发时间几乎没变」的对给出斜率 −52.5，把该题的跨代预测整段清零（`shrink=0`）。加 1.0 点² 的岭项后同一处斜率回到 −0.02 量级。测试 `test_a_pair_that_predicted_nothing_cannot_set_the_slope` 钉住这条。
6. **「一对不算拟合」**（`MIN_PAIRS = 2`）与 `judge_noise` 的 `MIN_DF` 是同一种纪律：题级证据不足时向上一层借，而不是让一发噪声决定一个题。
7. **收缩系数按 `as_of` 逐发拟合，所以同一题在不同时刻的系数不同**（ops-bench 上 ConcatD 在赛程前段是 1.0、后段 0.27–1.0；PadV3Grad 从 0.28 涨到 0.48）。这是有意的：赛程越往后，这道题的回执越多，先验的权重越小。
8. **`priority` 口径含 P24 的 `health_penalty` 与 EXPLORE 半权**（P24 NOTE 8 说明三者可叠乘到 0.2×），所以本报告的 Spearman 是「对真实门禁排序的秩相关」，不是对某个公式的复算。`ranking_v2` 在 ops-bench 上把 11 发从 `READY` 降为 `EXPLORE`（收缩后增益跌破底线但 P(gain>0) 仍 ≥0.5），放行状态不变。
9. **工具表字节**：主控 14951 / 15000、worker 12323 / 13000，与 `main` 完全相同（本包没动 `mcp/`）。主控只剩 49 B —— 本包没有新增任何工具或参数，`--ranking` 属于 CLI 参数，不占工具表。
10. **`ScoreEstimate.ranking` 是附加字段**（默认空 dict），`cli_bridge.py` 的卡片 JSON 是显式挑键构造的，因此 Swift 契约不变；`GateVerdict.to_dict()` 的 `estimate` 里多一个键，属于附加不破坏。
11. **`unstable` 是本包自己加的诚实标记**：区间不包住点估计时标出来，而不是让它看起来像个精确界。目前只有 mat-chem 反事实 top-1 一处。
12. **回放的只读性照旧**：两场都在 `tempfile.mkdtemp` 的副本上跑，`PARHELION_HOME` 指向副本内，结束删除；本包没有写 `~/.parhelion/workspaces`、没有写比赛目录、没有写总线与会话日志（只出聚合数字）。

## 10. 风险与后续

- **风险：σ̂ 小的场会让门禁更敢排。** mat-chem 的 σ̂ = 0.506（常数是 2.70），本包把它接进 `prob_positive`、`ucb_delta`、`expected_improvement`，等于把该场的探索倾向整体调高。这一场只动了 1 发的标签（那一场的拦截主要来自 S1 证据格式），但**下一场如果是 best 榜且证据齐全，σ 替换的推力会比这场大**，建议下一次冲刺用 `--groups default,sigma` 复测一次再定。
- **风险：跨代档的「模型读数」依赖 `effective_speedup`**，而该值在不同预测路径上量纲不完全一致（`paired_*` 是配对几何比、`anchor_fallback` 是 1.0、`pairwise_simplex_v2` 是 `est/anchor`）。夹取保证不会放大，但「模型读数」本身的可靠性就是估分器的可靠性。
- **后续（1）：把 `--ranking` 变成每个门禁包的例行输出。** 它是唯一能量化「排序有没有变准」的尺子；本包的 `default` 组数字可直接作为下一场的基线（G6）。
- **后续（2）：全名册逐例取证。** 只要回执里带全名册 `time_by_case`/`tbest_by_case`（mat-chem 已有 30 行这种格式），饱和变换的夹取就不必那么保守，跨代档才可能真正胜过线性预测。
- **后续（3）：`judge_noise` 的 as_of 化。** 现在 σ 与 S2 噪声底线都用全场口径；若要让它进入时点严格的回放，需要给 `judge_noise.estimate` 加 `as_of`（属于门禁改动，本包没做）。
- **后续（4）：`judge_noise` 的题级 df。** mat-chem 14 题全部落到全场合并值，说明那一场的重复判题太少；若下一场仍如此，题级 σ 就永远是名义上的。

## 11. 待决

1. **`ranking_v2` 的去留。** 本包按判据默认关并保留了完整实现（约 90 行纯逻辑 + 两个开关组）。可选：(a) 保留到下一场复测（推荐，成本只是一个关闭的开关与两个回放组）；(b) 若下一场复测仍不达标，直接从 `main` 删除该档，只留测量与 σ。这需要主会话决定。
2. **mat-chem 的 42 发证据格式漂移要不要在排序指标里剔除。** 若剔除，该场批准集仍是 12 发（这些发本来就散在各处），但反事实集会变成 35 发；P21 §11 第 3 条提过同一件事。本包没有剔除，按 P21/P24 的口径原样保留。
3. **验收口径用批准集还是全体已判集。** 本报告主口径按子计划的「待发的候选」= 批准集，同时给了反事实集。如果主会话认为排序质量应该对「全部发」评价（更接近「估分器排序准不准」而不是「门禁排序准不准」），下一场的对照表应改用反事实集 —— 本包两种都算，切换成本为零。
4. **σ 替换要不要也进 `bench_evolution` / `calibration` 的 Huber 阈值。** 这两个模块仍直接用常数 `SIGMA_HIDDEN_JITTER` 作为鲁棒损失的尺度（不是 σ 输出）。本包没动：它们是拟合的稳健性参数，不是「同一发重判的抖动」。是否需要一起换，取决于主会话对「同源抖动」范围的定义。
