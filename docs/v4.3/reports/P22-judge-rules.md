状态：已交付

# P22 判题规则快照 — 交付报告

分支 `impl/P22-judge-rules`，基线 4.2.1 的 `main`，独立工作树（不在主检出目录里改）。
新模块 `cli/opsb_core/services/judge_rules.py` 282 行（上限 350）。

## 1. 分支与提交

```
$ git log --oneline main..HEAD
HEAD       Deliver P22: record the judge rule snapshot, replay latest scores and report the rules
236d0c0 Add the judge rule snapshot, latest-mode replay and the rules report view
```

`236d0c0`：规则快照模块、三处来源接入、`opsb workspace rules`、榜型优先级、latest 重建、rules 视图与 status 一行，含测试。
第二行是 HEAD 即本报告提交（`git log -1 --oneline` 即其哈希，报告内不自引哈希以免自指）。
中间没有其它提交（两个提交各自都是全绿状态）。

## 2. 子计划逐项完成情况

| 子计划条目 | 状态 | 位置 |
|---|---|---|
| 1. 规则快照（读取/规范化/落盘） | 已完成 | `services/judge_rules.py`（新） |
| 2. 快照来源（reconciler、submit 脚本、`rules` 命令、`sync-problems`） | 已完成 | `sync_reconciler.py`、`submit_runner.py`、`commands/admin_cmd.py`、`cli_app.py` |
| 3. 榜型首选来源（快照 → `problems-all.json` → 清单） | 已完成 | `scheduler/estimator.py::_load_problems` |
| 4. latest 下的分数重建 + `judge.reconstruct_latest` | 已完成 | `judge_score_fetch.py::reconstruct_timing_score` |
| 5. `parhelion_report(view=rules)` + status brief 一行 | 已完成 | `mcp/report_views.py`、`mcp/handlers_report.py`、`mcp/schemas.py`、`services/judge_rules.py::status_line` |
| 测试（子计划「测试」节 4 条） | 已完成，21 条新增 | `cli/tests/test_judge_rules.py`（新）、`cli/tests/test_sync_reconciler.py`（改 1 处断言） |

模块规模（`git diff --stat`）：`judge_rules.py` 新增 282 行；`report_views.py` +125（视图逻辑在这里，
允许厚）；`admin_cmd.py` +66；`handlers_bench.py` +6/−1（一个 import 加一处 `out["judge"] = ...` 接入，
σ̂ 与榜型的实现放在 `judge_rules.status_line`）；`estimator.py` +12/−1；`handlers_report.py` +20
（VIEWS/HELP/分发）；`judge_score_fetch.py` +23/−3；其余为小改。

### 2.1 规则快照字段表

落盘位置 `results/<题>/.judge-rule.json`，由 `judge_rules.normalize()` 按白名单生成：

| 字段 | 来源 | 取值 / 处理 |
|---|---|---|
| `schema_version` | 本模块 | 固定 1 |
| `problem_id` | 规则的 `_id` | 24 位公开对象 id；用于发现换题/改名（子计划字段表未列，见 NOTE 1） |
| `score_mode` | 同名 | 标量，实测 1 |
| `use_baseline` | 同名 | 布尔，实测 `false` |
| `ranking_submission_mode` | 同名 | 字符串，小写规范化（`Latest` → `latest`） |
| `iterations` | 同名 | 标量，实测 5 |
| `judge_revision` | 同名 | 标量，实测 0 |
| `ranking_testcase_count` | 同名 | 标量；两场实测为 `null` → 省略 |
| `testcases` | 同名 | `{total, by_type}`；`public`/`hidden` 仅当每条用例都声明可见性时追加 |
| `time_limit` | `time_limit`/`time_limit_ms`/`time_limit_sec`/`time_limit_s`/`judge_time_limit`/`limits` | `{field, value}`；两场都无此字段 → 省略（NOTE 2） |
| `fetched_at` | 抓取时间 | 内容变化时才更新，见 NOTE 3 |

规则里其余字段（`user_id`、`desc`、`code_template`、`tags`、`theory_*` 等）一律丢弃；测试断言快照里
不出现 `user_id`、题面、模板源码、cookie 字样。

### 2.2 快照来源

1. `sync_reconciler._reconcile_one_candidate`：读完 `/api/problems/{pid}` 后立刻 `judge_rules.save`，
   包在 `_save_rule()` 里吞掉所有异常（测试：把 `save` 换成抛 `OSError` 仍不影响对账）。
2. `submit_runner._wait_and_fetch_score` 生成的对账脚本：`reconstruct_timing_score` 之前插入
   `judge_rules.save(root, <题>, rule)`，同样就地 try/except。
3. 新命令 `opsb workspace rules [--refresh] [--json]`（挂在 `workspace` 子命令下）：
   - 无 `--refresh`：只读本地快照并列缺哪些题，不联网；
   - `--refresh`：用平台自己的登录态逐题读 `/api/problems/{pid}` 并落盘，**worker 不接触凭据**；
   - 缺登录态：列出缺哪些题、退出码 0；单题读失败只记 `{title, error}` 并继续。
4. `opsb workspace sync-problems` 结束后调一次 `judge_rules.refresh`，结果并入 `--json` 输出的
   `rules` 段与人类可读输出；失败只记一行，不影响同步本身。

### 2.3 各题榜型与来源

实测两场（只读，见 §4）。「改前」= 4.2.1 的 `_load_problems` 能拿到的来源。

| 赛题 | 题数 | 榜型（快照） | 清单 `problems.json` | `problems-all.json` | 改前实际取到 | 改后来源 |
|---|---|---|---|---|---|---|
| mat-chem（当前这场） | 14 | `best` ×14 | 无该字段 | **文件不存在** | 无 → 门禁按 best 缺省 | 规则快照 |
| ops-bench | 10 | `latest` ×10 | 无该字段 | `latest` ×10 | `problems-all.json` | 规则快照（同值） |

即：当前这场的榜型在 4.2.1 里根本没有记录，门禁是靠 `keep_best_board=True` 的缺省值恰好对；
ops-bench 本来能从 `problems-all.json` 读到，但那是赛后一次性抓取，不是对账时刷新。

### 2.4 latest 下的分数重建

`reconstruct_timing_score` 去掉「`ranking_submission_mode == "best"`」一条；其余前提全部保留
（`score_mode == 1`、`use_baseline is False`、`judge_revision` 一致、`valid`/`public_only`/
`judge_missing_cases`、名单完整、榜单行自检）。`judge.reconstruct_latest`（默认 true）为 false 时
`latest` 立即回到 4.2.1 的早退。`_best_score_bound` 未改，仍只在 `best` 下生效（测试钉住）。

核对表（真实数据，只读副本）：

| 数据 | 有逐例时间的回执 | 改写榜型为 latest，开关 on | 开关 off | best 榜型（回归） |
|---|---|---|---|---|
| mat-chem（best 赛制） | 22（21 Pass + 1 TLE） | **14/22 复现判题分，误差 0.0000** | **0/22**（= 4.2.1） | 14/22（与 4.3 相同） |
| ops-bench（latest 赛制） | **0/268**（全库无逐例数据） | 0 条可核对 | 0 | — |

- mat-chem 的 8 条未复现：7 条回执里的 tbest 是判题后再次读取的值（重算分比判题分低 0.05–3.25），
  1 条判题分为 0；自检按设计拒绝，不硬算。
- **ops-bench 的 7 条「Pass 无分」补了 0 条**。缺的数据：45 个隐藏用例的 `time` 与 `tbest`。
  该场回执（4.0 期格式）整库 0 条 `case_results`/`timing_score`/`tbest_by_case`；用仍可用的登录态
  只读拉 `/api/submissions/{sid}`，返回 50 行，其中 45 行 `testcase_status=Hidden` 且 `time=null`、
  无 `best_time`，只有 5 个公开例带时间，因此**任何账号都拼不出整题计分**。赛后也用不了「当前榜单」
  补算：tbest 已漂移，重建器按设计只接受与判题同期的榜单快照（`checked` 行自检）。
  → 这条改动的实际收益在下一场：判题后 72 小时对账窗口内，`latest` 赛制的「Pass 无分」会被补上。

### 2.5 `parhelion_report(view=rules)`

- 内容：`rule`（快照白名单字段）、`formula`（`100/(1+ln(t/tbest)/ln1.5)` 后取均值）、
  `tbest`（`cases`/`observed_at`/`changed_at`/`changed_cases`）、`noise`（`sigma_hat`/`df`/`scope`，
  取自 `scheduler.judge_noise.sigma_for`）、`split`（本题最好一发与在榜那一发的公开项/隐藏项，
  附 `verified{rows, public_cases, max_err}`）。
- 定位：给 `problem` 出单题；不给则出全表（`title/mode/cases/sigma_hat/fetched_at`）；worker 由
  `_caller` 绑到自己的题，指别人的题抛 `PermissionError`（测试覆盖）。
- 算不出就省略：`split` 只在某条回执同时满足「逐例均分复现判题分（误差 ≤ 0.01）」和「`public_times_us`
  恰好等于该回执前 N 个计时用例」时给出；否则整段省略（不猜）。
- 列说明进 `handlers_report.HELP["views"]["rules"]`（`view="help"` 可读）。

public/hidden 分解在已有回执上的验证（mat-chem 副本，14 题）：

| 项 | 数值 |
|---|---|
| 带逐例时间 + tbest 的回执 | 22 |
| 恒等式通过（逐例均分 = 判题分，误差 0.0000） | 14 |
| 位置校验通过（`public_times_us` = 前 5 个计时用例） | 14/14（这 14 条全部吻合） |
| 得到 `split` 的题目 | 8/14 |
| 其余 8 条回执 | 省略：7 条 tbest 在判题后漂移、1 条判题分为 0（这 8 条里另有 5 条 `public_times_us` 位置不吻合，本来也进不来） |
| 其余 6 题 | 视图省略 `split`（该题没有能通过校验的回执） |

示例（CGTensorProduct）：`best = board = 28.7`，公开项 `11.75`（5 例）+ 隐藏项 `16.95`（10 例）。
ops-bench 的回执没有逐例数据，`split` 全库省略。

字节数（改前 → 改后，上限 2048）：

| 对象 | 改前 | 改后 |
|---|---|---|
| `parhelion_report(view=rules)`，单题（mat-chem CGTensorProduct） | —（无此视图） | **693 B** |
| `parhelion_report(view=rules)`，全表（mat-chem 14 题） | — | **860 B** |
| `parhelion_status` brief，mat-chem 副本 14 题 | 1665 B | **1744 B**（+79） |
| `parhelion_status` brief，ops-bench 副本 10 题 | 1295 B | **1377 B**（+82） |

工具表（`tools/list` UTF-8 字节，上限 15000/13000，未改上限）：

| 角色 | 改前 | 改后 |
|---|---|---|
| 主控 | 14601 | **14616**（+15） |
| worker | 12507 | **12522**（+15） |

`view="help"` 回复：2452 → 3246 B（仍在 `output_cap_bytes` 5000 与测试的 4096 之内）。

### 2.6 测试

`cli/tests/test_judge_rules.py`（新增 21 条）：

- 规范化：白名单、`user_id`/题面/模板/cookie 不外泄、`null` 省略、可见性只在不全时省略、时限只在有字段时记；
- 落盘：内容不变不重写（`fetched_at` 保持首次值）、变化才写；
- 来源：缺登录态只报缺哪些题且不写盘；注入 fetch 逐题保存且幂等；单题失败继续；
- 榜型优先级：快照 > `problems-all.json` > 清单，且无清单时快照仍生效；
- latest 重建：夹具与判题分一致、开关关掉后 `latest` 为 None 而 `best` 仍复现、其余前提逐条仍拒绝；
  `_best_score_bound` 在 `latest` 下返回 None；
- rules 视图：字节上限、`split` 的验证与省略、全表形态、worker 角色限制、`status_line` 与 handler 接入。

`cli/tests/test_sync_reconciler.py` 的一条断言按子计划改写：原来把「`latest`/`None` → None」钉成
4.2.1 行为，现在钉成「默认复现、`judge.reconstruct_latest=false` 时回到 None、`best` 恒复现」。

## 3. 全量测试与 Swift

```
$ ~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider
822 passed, 19 skipped, 2 subtests passed in 81.84s
```

```
$ PYTHONPATH=cli:. ~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider cli/tests runtime/tests
561 passed in 63.12s
```

基线 801/19 与 540，本次各 +21（新增测试文件 21 条）。`runtime/tests/test_device_broker.py` 的
两条预算测试本轮未偶发失败，无需单跑。**未改 Swift**，因此没有 `swift build`。

## 4. 真实数据验证（只读）

数据只读：两场数据都先复制到临时目录再操作，真实工作区、`results/`、会话日志与总线均未写入、未移动。

1. **规则快照刷新**（`/tmp/.../matchem` 副本，`opsb workspace rules --refresh --workspace <副本>`）：
   14 题全部读到并落盘，第 2 次运行 0 题变化（幂等）。摘要：14 题 `best`、`score_mode=1`、
   `use_baseline=false`、`iterations=5`、`judge_revision=0`；用例数 15（12 题）/ 19（2 题：
   `SgemmFp16Cube`、`QuantScaleCalib`）；`ranking_testcase_count` 与 `time_limit` 均未发布。
   只读判题接口，未提交任何东西。
2. **ops-bench 副本**：10 题全部读到，`latest`、50 例，同样第 2 次 0 变化。
3. **榜型生效**：`ScoreEstimator` 在两份副本上分别读出 14×`best` / 10×`latest`；
   `gate._keeps_best` 读的是同一份 `problems_meta`，因此 4.2.1 的榜型口径不再依赖重新抓赛题。
4. **latest 重建**：见 §2.4 核对表（mat-chem 14/22 误差 0.0000；开关关掉 0/22；best 14/22）。
5. **σ̂**：ops-bench 副本的 contest pooled `σ̂ = 3.162`（df 26，与 4.2.1 噪声模块和 P19 报告的 3.04 同量级）；
   mat-chem 副本 `σ̂ = 0.506`（df 5，contest 口径）；单题（ConcatD）`σ̂ = 2.564`（df 5，problem 口径）。
6. **会话日志与总线**：本包没有读取；无聚合数字可报。

不需要真机，未跑真机步骤。

## 5. NOTE（源码事实与非显然约束）

1. **快照多了 `problem_id`**（子计划字段表未列）。理由：落盘目录只有题名，规则换题/改名时无法发现；
   `problem_id` 是公开对象 id（不是用户字段），测试也断言快照里没有任何用户字段。
2. **两场都没有时限字段**。规则里 `start_time`/`end_time` 是比赛窗口，`theory_quota_limit` 是理论题配额，
   都不是判题时限；`time_limit*` 之一出现才会写 `time_limit`。
3. **`fetched_at` 是「这份内容从何时起被记录」**，不是「最近一次读取」。因为要满足「只在内容变化时写入」，
   两者不能同时成立；要「最近读取」需要再加字段（见「待决」）。
4. **规则接口不发布用例可见性**：`testcases[*].testcase_id` 只有 `type`/`baseline`/`ID`，没有 `hidden`。
   所以快照的 `public`/`hidden` 计数在两场都不出现；只有每条都声明时才计（代码已支持，等接口补字段）。
5. **判题公式是 `100/(1+ln(t/tbest)/ln1.5)` 的均值**，不是 `results/scoring-rules.md` §1 写的
   `100·mean(tbest/time)`。mat-chem 14 条回执逐条复现判题分（误差 0.0000）证实了前者；视图的
   公开项/隐藏项也按对数式拆。
6. **`public_times_us` 是「前 5 个计时用例」，不等于公开用例集合**（`extract_public_times` 里硬编码
   `[:5]`）。视图因此把公开边界定义为「该回执 `public_times_us` 的长度 + 位置吻合」，不吻合就省略；
   mat-chem 里不吻合的 5 条都是无分/0 分行（它们的 `public_times_us` 来自榜单行而不是本次明细），
   有分的 14 条全部吻合。
7. **隐藏用例时间的可得性因场而异**：mat-chem 的提交明细给全部 15/19 例时间，ops-bench 只给 5 个公开例
   （45 行 `Hidden`）。这直接决定了「Pass 无分」能不能补。
8. **重建器只能在判题后短时间内用**：它的自检要求榜单行能被同一 tbest 复现；历史回执遇上今天的 tbest
   会算低（mat-chem 8 条即此类）。P22 放开的是同一窗口内的榜型限制，不是「事后重算」。
9. `opsb workspace rules` 不带 `--refresh` 不联网；带 `--refresh` 时用平台自己的登录态，
   命令从不打印或落盘凭据（测试断言会话内容不出现在快照里）。
10. `judge.reconstruct_latest` 是配置项而非 `GateConfig` 开关：它只影响「能不能补分」，不改门禁判定与派发，
    因此没有登记进 `GATE_SWITCHES`；关闭后 `latest` 完全回到 4.2.1。

## 6. 风险与后续

1. 快照存 `results/<题>/`，若赛后清理 `results/` 会一起丢；重建只需 `opsb workspace rules --refresh`（一题一次只读 GET）。
2. `split` 依赖「公开例 = `public_times_us` 前缀」这一平台惯例；判题站改公开例数量时 `split` 会自动省略，
   不会错报，但也不会提前告知。
3. 7 条历史「Pass 无分」仍然补不回来，需要平台在下一场判题后 72 小时窗口内醒来（默认开启即生效）。
4. `rules` 视图与 status 的字节数离上限还有余量（693/860 对 2048；1744/1377 对 2048），但 status 的
   余量只有约 300 B，后续包再往 brief 加行前要先量。

## 7. 待决

1. 是否把 `judge.reconstruct_latest` 做成 CLI 入口（如 `opsb queue config --judge reconstruct-latest=false`）。
   现状只能改 `~/.parhelion/config.json` 的 `judge` 段。
2. 快照是否需要「最近一次读取时间」字段。它与「只在内容变化时写入」冲突，需要先定语义。
3. `rules` 视图的 `split` 在缺 `public_times_us` 时是否回退到「前 5 个计时用例」。当前选择省略（更保守）。
4. `view="help"` 从 2452 B 涨到 3246 B（仍在 4096 与 5000 上限内）。若后续包继续加视图，可能需要把
   `help` 按视图分页。
