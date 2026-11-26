# P22 判题规则快照

分支：`impl/P22-judge-rules`。基于 4.2.1 的 `main`。无依赖。

## 为什么

- **上一场最高杠杆的 20 分钟，是一次规则取证**（R6 §6）：确认榜型 latest、50 例等权、tbest 只取 Pass、同源重投会重新计时。这一步当时靠 worker 用登录态抓判题接口完成，是一次越界。此后所有关于基线、噪声底线、公开与隐藏剪刀差的判断，都建立在这份文档上。
- **4.2.1 的门禁已经按榜型选口径，但当前工作区拿不到榜型。** 榜型来自赛题列表的抓取，而列表接口是否带这个字段还没有确认。
- **平台其实已经逐题读到了规则。** 对账回执时，`cli/opsb_core/services/sync_reconciler.py` 第 285 行左右读取 `/api/problems/{pid}`，`submit_runner.py` 的对账脚本第 1188 行左右也读一次。读到之后只在内存里用，没有落盘。
- **latest 赛制下，分数重建被整段关掉。** `judge_score_fetch.reconstruct_timing_score` 在 `ranking_submission_mode != "best"` 时返回 None。上一场有 7 条回执是 Pass 但没有分（6 条在 StridedSliceGrad），因此补不回来。而逐例计分公式与榜型无关。

## 范围

1. **规则快照**
   - 新模块 `cli/opsb_core/services/judge_rules.py`，负责读取、规范化与落盘 `results/<题>/.judge-rule.json`。
   - 字段只取规则里的公开元数据：

     | 字段 | 说明 |
     |---|---|
     | `score_mode`、`use_baseline`、`ranking_submission_mode` | 计分方式与榜型 |
     | `iterations`、`judge_revision`、`ranking_testcase_count` | 迭代次数、判题版本、计分用例数 |
     | 用例名单摘要 | 按 type 与可见性计数 |
     | 时限 | 规则里有时限字段才记录 |
     | `fetched_at` | 抓取时间 |

   - 不保存 cookie、用户 ID、账号或提交者信息。
   - 只在内容变化时写入。
2. **快照的来源**
   - `sync_reconciler` 与 `submit_runner` 已经读到规则，在那里顺手调用 `judge_rules.save`。保存失败不能影响对账。
   - 新命令 `opsb workspace rules [--refresh] [--json]`，挂在 `commands/admin_cmd.py` 的 workspace 子命令下。它用平台自己的登录态逐题读取规则，worker 不接触凭据。
   - `opsb workspace sync-problems` 结束后也调一次。
   - 缺登录态时只报告缺哪些题，不报错退出。
3. **榜型的首选来源**
   - `scheduler/estimator.py` 的 `_load_problems` 读榜型时，按「规则快照 → `problems-all.json` → 清单」的优先级取。
   - `scheduler/parhelion_paths.py` 的清单逻辑不变。
   - 这样 4.2.1 的 `_keeps_best` 不用再等重新抓赛题。
4. **latest 下的分数重建**
   - `reconstruct_timing_score` 去掉对榜型的要求，其余前提全部保留：`score_mode == 1`、`use_baseline is False`、判题版本一致、名单完整等。
   - `sync_reconciler._best_score_bound` 仍然只用于 best。用别的提交来约束本提交的分数，只在 best 赛制下成立。
5. **`parhelion_report(view=rules)`**
   - 在 `cli/opsb_core/mcp/report_views.py` 增加视图，列说明写进 `handlers_report.py` 的 HELP。worker 只能看本题。
   - 内容：
     - 快照字段；
     - 逐例计分公式：与 P01 回测报告一致，每例 100/(1+ln(t/tbest)/ln1.5) 后取均值；
     - tbest 的最近变化时间；
     - 本题判题噪声 σ̂：取 `scheduler/judge_noise.sigma_for`；
     - public/hidden 分解：用回执里已有的逐例结果算出本题最好一发与在榜那一发的公开项与隐藏项。先在已有回执上验证恒等式，算不出就省略，不猜。
   - 回复不超过 2KB。
   - `parhelion_status` 的 brief 里加一行榜型与 σ̂。注意 status 的 2KB 上限。

## 复用

- `scheduler/receipts.py`：`case_results`、`judge_score`；
- `scheduler/judge_cases.py`：roster、tbest；
- `scheduler/judge_noise.py`；
- `cli/opsb_core/services/contest_scrape_service.py`：4.2.1 已经在抓取时带上榜型。

## 开关

- 规则快照只是数据，不需要开关。
- latest 下的分数重建，加配置项 `judge.reconstruct_latest`，默认 true。

## 测试

- 规则规范化：剔除非白名单字段，且不含任何用户字段。
- 榜型的来源优先级。
- latest 下的重建：用夹具断言与判题分一致；`_best_score_bound` 在 latest 下仍返回 None。
- rules 视图的字节上限与角色限制。

## 验收

- **真实数据只读。** 在 `/Users/winbeau/Competitions/mat-chem-sim-pred` 的副本上（复制 `results/` 与清单到临时目录）跑 `opsb workspace rules --refresh --workspace <副本>`，报告各题快照摘要。这一步只读判题接口，不提交任何东西。不要写进真实工作区：真实工作区由主会话在发布时刷新。
- 在 ops-bench（latest 赛制）的回执上验证重建。找出既有逐例时间、又有同期榜单快照的回执，比较重建分与判题分（误差 ≤ 0.01）。7 条 Pass 无分的回执能补几条就补几条；补不了的写明缺什么数据。

## 真机

不需要。

## 报告（`docs/v4.3/reports/P22-judge-rules.md`）

- 快照字段表；
- 各题的榜型与来源；
- latest 重建的核对表；
- public/hidden 分解在已有回执上的验证；
- rules 视图与 status 的字节数。
