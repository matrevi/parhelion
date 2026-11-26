状态：已交付

# P12 平台视图 `parhelion_report`（4.2 D）

## 1. 分支与提交

- 工作树 `/Users/winbeau/Developer/personal/parhelion-wt/P12`，分支 `impl/P12-report-views`，基线 `main` `152fd62`（4.1.0）。
- `git log --oneline main..HEAD`：

```
9d03630 Cluster the temp scripts and CLI bypasses for P12
Deliver P12: add the platform report views   (本报告随该提交进分支；哈希见 git log 末行)
```

标题以 `Deliver P12:` 开头。未合并 `main`、未 push。

## 2. 逐项完成情况

| 条目 | 状态 | 证据 / 说明 |
|---|---|---|
| 新模块 `cli/opsb_core/mcp/handlers_report.py` ≤350 行 | 已完成 | 350 行（`wc -l`）；七个视图全部在内，未改动既有大文件 |
| `per_case` 固定列 | 已完成 | 19 列：`case, ours_us, tbest_us, gap_pct, speedup, band_pct, verdict, noisy, judge, mapped, local, timer, passed, exit_code, mismatch_n, strict_fp32, cal, wa, run`；差距/目标/加速比取自 `state_card_ext.data()` 与同一 `speedups()`；三态与噪声带取自 `scheduler.verdicts.paired`；映射取自 `case_map.current`；`cal` 取自 `case_calibration.table`；`wa` 取自 `wa_repro.status` + manifest 的 `bases` |
| `diff` 两 SHA 逐例比与区间 | 已完成 | 先读 `report.json` 的 `cases[].pair` / `pair_bootstrap` / `digest.paired`（P10），不重算；无配对报告时用 `timing.best_minima` + `paired_timing.unpaired_interval`；`git diff --stat` 经 `ledger.gitref.git_out`；≤4KB |
| `history` 判题历史/逐例比/回执状态 | 已完成 | `scheduler.receipts.canonical` over `results/submit-queue-outcome.jsonl`；按 `submission_id` 去重；每条回执带 `sid/sha/status/score/beat_board/at/note/cases/mean_ratio`；`problems[]` 给每题的 judged/rows/best_judged/latest |
| `queue` 候选/暂缓/在途/已完成/门禁 | 已完成 | 与 `opsb queue list --json` 同源：`compute_workspace_snapshot` + `ipc_bridge.merge_bridge_enrichment`；`gates` 直接来自 `gate_view` 的灯泡；**不返回** `account_id`/`account_label`/`user_id`/cookie/token（递归键名过滤 + 测试断言） |
| `board` 榜单/排名/tbest/frontier | 已完成 | 只读 `board_refresh_service.current_board()` 与 `ranking_service.team_ranking(allow_fetch=False)`；单测把 `urllib.request.urlopen` 换成断言失败的桩，零网络；具名题给逐例 tbest + `observed_at`，主控全题只给每题摘要（预算内） |
| `bottleneck` R6 | 已完成 | `fast_feedback.build_pmu_torch_card` + `knowledge.query.query_kb(auto_seed=False)`；返回 `category/limited_resource_pct/top_metrics[3]/fixes/raw{report,log,run_dir}`；剥掉 `runtime_soc` |
| `env` | 已完成 | 平台版本/平台根/守护进程 pid 存活/工作区、results、state 目录/磁盘余量；实测 421 B ≤1KB；不含账号、会话、token、端口 |
| 形状：两种 `report.json` | 已完成 | fixture 同时含套件级（`level/timed/selection/weights/digest`）与遗留单次运行（只有 `sha/formal/cases`、raw `device_us`）两种；`cases[].perf` 一律按 bool 处理；`_docs()` 扫 `<sha>/bench-runs/*`、`bench-runs/*`、`bench-runs/*/*` 与 `<workspace state>/bench-runs/*` 并按真实 `sha` 过滤 |
| 设备字段过滤 | 已完成 | 视图不返回 `report.settings` / `report.environment` 原文；只取 `strict_fp32` 等标量；`device_env` 的键名仍走既有解析。单测断言 `soc-a/arch-v1/sdk-x/npu` 不出现在输出里 |
| 角色 | 已完成 | `WORKER_SCOPED_PROBLEM_TOOLS` 加入 `parhelion_report`；worker 的题取自 `handlers_ledger.problem_of`，与 escape 工具同样严格（无题/跨题一律 `PermissionError`）；`board`/`env` worker 可用但仍限本题；主控可查全部 |
| 三处登记 | 已完成 | `mcp/schemas.py`（含 `roles` 与逐视图列说明）、`mcp/tools.py`（`TOOL_HANDLERS` + `WORKER_SCOPED_PROBLEM_TOOLS` + `__all__`）；工具计数断言 26→27（`test_mcp_server.py` 两处、`test_agent_mesh_http.py` 一处） |
| 工具描述吸收 `--help` | 已完成 | schema `description` 写明「代替读 `report.json` / `outcome.jsonl` 的临时脚本」「代替 `opsb history`/`opsb --help` 找列」，并列出七个视图的列 |
| 单测 | 已完成 | 新增 `cli/tests/test_report_views.py` 12 条 + `cli/tests/fixtures/p12_report/`（形状裁剪自真实数据并去敏：假 SHA、假账号、无设备值；测试动态建 git 历史并把目录名改成真实提交） |
| 覆盖对照表 | 已完成 | 见 §5（`opsb queue list --json` 与 `opsb history --json` 逐字段） |
| 真实比赛只读验证 + 字节数 | 已完成（`queue` 有既有副作用，见 §4.3） | 六个视图对真实目录零写入；`queue` 因规定的同源命令会写平台日志，改在副本上跑并披露 |
| 回放口径 | 已完成 | 改为「A 组 441 次调用降 80%」，估算方法见 §6 |
| Swift | 未改动 | 本包不涉及 Swift；`swift build` 不需要 |

## 3. 测试

两条命令的末行：

```
698 passed, 19 skipped, 2 subtests passed in 75.95s
454 passed in 62.06s
```

第一条是根目录全量（4.1.0 基线 686 passed / 19 skipped，本包 +12 条）；第二条是 `PYTHONPATH=cli:.` 的 `cli/tests runtime/tests`（基线 442，本包 +12 条）。`runtime/tests/test_device_broker.py` 两条预算测试在本机这次没有失败，无需单独重跑。

## 4. 真实数据验证

数据：`/Users/winbeau/Competitions/mat-chem-sim-pred`（`results/` 115 MB，602 份 `report.json`，132 行回执）。为了不写真实 `~/.parhelion`，验证使用临时 `PARHELION_HOME`，并把只读输入（五个 KB 支柱目录、该工作区的 `bench-specs/`）**复制**进去；`ranking_service.refresh_async` 桩成 no-op、`OPSB_DISABLE_SYNC_RECONCILE=1`，避免后台线程/子进程。设备值用 `OPSB_DEVICE_ISOLATED=1` 隔离。

### 4.1 各视图字节数（真实数据）

| 视图 | 参数 | 输出字节 | 备注 |
|---|---|---:|---|
| `per_case` | `problem=EdgeIndexCoalesce` | **2515** | 15 例，≤4096 |
| `per_case` | `problem=QuantScaleCalib` | **2369** | 19 例；`wa_state=reproduced` |
| `diff` | `EdgeIndexCoalesce` 存配对报告（`sha=4f954c3`、`base=3f99cd8`） | **1514** | `source=paired-report`，`geomean=1.0183` |
| `diff` | `FFT3DBatchComplex` 分跑回退 | **1421** | `source=separate-runs`，15 例 `welch_log`，`git diff --stat` 2 行 |
| `history` | `problem=EdgeIndexCoalesce` | **3726** | 9 条回执 |
| `history` | 全题 | **1986** | 只给每题摘要 |
| `queue` | `EdgeIndexCoalesce`（副本） | **3662** | 11 项，`items` 4/11（预算截断，见 §8） |
| `queue` | 全题（副本） | **3363** | 88 项，`items` 3/88 |
| `board` | `EdgeIndexCoalesce` | **772** | 14 例 tbest + frontier |
| `board` | 全题 | **3082** | 14 题摘要 |
| `bottleneck` | `FFT3DBatchComplex` | **676** | 该题 PMU 计数不在日志里，`category=unknown`，`raw` 路径有值 |
| `bottleneck` | `EdgeIndexCoalesce` | **676** | 同上；无 `fast-feedback` 产物 |
| `bottleneck` | `QuantScaleCalib` | **668** | 同上 |
| `env` | 真实 home | **421** | daemon 在跑（pid 存活），无 token/端口 |

`per_case` 的硬预算 4KB 在 15 例与 19 例两个真实题上都满足；输入行数上限来自 `state_card_ext` 的 60 行，超过 4KB 时 `_trim` 从尾部删除并给 `truncated/shown`。

### 4.2 只读证明

先用 `touch /tmp/p12-marker2`，跑六个视图（`per_case`/`diff`×2/`history`×2/`board`×2/`bottleneck`×3），再查：

```
find /Users/winbeau/Competitions/mat-chem-sim-pred/results -newer /tmp/p12-marker2
# 空输出
```

即六个视图对真实比赛目录零写入。

### 4.3 `queue` 的既有副作用（必须披露）

`queue` 按子计划要求走 `opsb queue list --json` 同源路径，即 `scheduler.cli_bridge status`。该命令本身会：把 6 条 gate 记录（本次实测条数）追加到 `results/feedback-log.jsonl`、改写 `results/.feedback-signatures.json`、并刷新 `results/<title>/.candidate-archive.json`。第一遍把 `queue` 也跑在真实目录时，`find -newer` 检出正好这 5 个路径（3 个文件 + 2 个目录项）；这是被要求复用的源命令的既有行为，不是 `handlers_report.py` 直接写文件。为避免继续写真实目录，后续 `queue` 的字节数改在 `/tmp/p12-ws-copy`（`cp -a` 的完整副本）上取，副本内允许写。其余六个视图未触发任何写入。建议 P13 或 P18 给 bridge 加只读开关（见 §8）。

### 4.4 工作树内 stdio 最小调用

```
cli/opsb mcp serve --workspace /Users/winbeau/Competitions/mat-chem-sim-pred
```

`initialize` / `tools/list` / `tools/call parhelion_report(view=env)` / `tools/call parhelion_report(view=per_case, problem=EdgeIndexCoalesce)` 四条都返回：`tools/list` 27 个工具且含 `parhelion_report`；两次调用 `isError=false`，分别 405 B 与 2515 B（`env` 是临时 home，故 405 而非 421 B）。

## 5. 覆盖对照表

### 5.1 `opsb queue list --json`

顶层：`threshold` → `queue.threshold`；`count` → `queue.count`；`items[]` → `queue.items[]`；`active_submissions[]` → `queue.active_submissions`（计数）+ `items[kind=inflight]`（逐条）。

`items[]` 与 `gates`：

| CLI 字段 | 视图去处 |
|---|---|
| `id, kind, title, sha, submission_id, queued_at, finished_at, badge, raw_status, note, detail, next_step, current_score, estimated_score, actual_score, delta_score, breakthrough_delta, can_approve, can_skip, can_restore, archive_rank, experience_id` | 同名 `queue.items[]` 字段 |
| `short_sha` | `items[].sha`（本就 7 位） |
| `prior_score` | `items[].current_score`（卡生成时两者相同；已在描述里注明） |
| `phase` / `elapsed_sec` | `items[].phase` / `items[].elapsed_sec`（在途卡） |
| `gates.evaluated, gates.safety_ok, gates.noise_ok, gates.circuit_ok, gates.dispatch_ok, gates.all_pass, gates.delta_expected, gates.ucb_delta, gates.calibration_trust, gates.explore_eligible, gates.wspt_score` | `items[].gates.*` 同名 |
| `wspt_priority` | `items[].gates.wspt_score` |
| `is_breakthrough` | `items[].breakthrough_delta` 与 `queue.threshold` 比较 |
| `judge_message, local_error` | 折进 `items[].note` / `items[].detail`（`failure_text.card_text` 生成） |
| `score_source, score_upper_bound` | 不返回；估分质量走 `parhelion_status` |
| `difficulty` | 不返回（排序权重；`parhelion_status` 有） |
| `requested_submission, request_note, speedup, forced, agent_id` | 不返回（派发/候选内部字段；`agent_id` 是 worker 身份，不属于报表） |
| `baseline_pass_sha` | 不返回；用 `history` 的上一条回执 |
| `account_id, account_label` | **永不返回**（`FORBIDDEN` 过滤 + 单测断言） |
| `remote_submission_id` | 即 `items[].submission_id` |
| `remote_judge_id` | 不返回（与 `submission_id` 重复，判题侧 ID 不进报表） |

### 5.2 `opsb history --json`

| `history --json` 字段 | 视图去处 |
|---|---|
| `title` | 所有视图的 `problem` |
| `current_score, rank, total_teams, board_listed, board_score, board_submission_id, status` | `board.problems[]`（score/rank/total_teams/listed/board_submission_id） |
| `best_score, raw_best_score, raw_best_at, best_sha, active_sha` | `history.problems[].best_judged` 与 `history.receipts[]`（逐发）；`best_sha`/`active_sha` 不单列，用 receipts 的 `sha` |
| `latest_sha, latest_status, latest_score, latest_at, latest_score_source` | `history.problems[].latest` + 最后一条 `receipts[]` |
| `history[]` 的 `at, sha, status, score, submission_id, note` | `history.receipts[]` 的 `at/sha/status/score/sid/note` |
| `history[]` 的 `display_score` | `history.receipts[].score`（规范分）+ `beat_board` |
| `history[]` 的 `estimated_score, delta, public_pts, hidden_pts, score_source, score_upper_bound, timing_score` | 不返回（队列/估分，见 `queue` 与 `parhelion_status`） |
| `public_times_us, public_tbest_us, public_ratios, public_mean_us, tbest_mean_us, judge_case_count, public_pts, hidden_pts, implied_hidden_ratio` | `per_case`（`ours_us/tbest_us/gap_pct/speedup`）与 `board`（逐例 `tbest_us`）；`mean_us/ratios/pts/implied` 不单列 |
| `testcases[]` 的 `case_id, index, tbest_us, ours_us, ratio` | `per_case`（映射标签 + ours/tbest/gap）与 `board`（逐例 tbest） |
| `testcases[]` 的 `is_hidden, leader_us, points, numeric_id` | 不返回（隐藏标记/他队用时/判题点数是判题侧细节） |
| `head_sha, head_subject, head_age_min` | `per_case.sha/head`、`diff.sha`（subject/age 不返回） |
| `case_run.*` 的 `run_id, formal, passed, finished_at, experiment` | `per_case.run`（run_id 前 8 位）+ 正确性列 + `diff.source` |
| `case_run.report` | `bottleneck.raw.report` |
| `case_run.buckets, failures, selected, total` | 不返回（用 `parhelion_test_wait`/`bench diagnose`） |
| `calibration.*`（`mae_pts, loocv_*, bucket_correlations, buckets, strata, …`） | `parhelion_bench_diagnose`；`bottleneck.top_metrics` 复用桶相关度 |
| `calibration.escape_lock/escape_repro/escape_status/escape_sha/escape_full_sha` | `parhelion_escape_status` |
| `calibration.active_board_sha` | `per_case.base` / `diff.base` 的来源 |
| `circuit_open, pressure_12h, wspt_tag, wspt_priority, board_advice, critical_restore` | `queue`（badge/gates）或不返回（调度内部） |
| `leader_score, leader_nickname, leader_times_us` | 不返回（他队数据） |
| `problem_id, problem_url, slug, repo_slug, numeric_id, path, sdk_version` | 不返回（身份/URL/设备事实，P15 边界） |
| `tbest_decay_gamma, weighted_score` | 不返回（衰减模型/计分中间量；`parhelion_bench_diagnose`） |

## 6. 回放口径与估算

原始文档的「621 个脚本降 80%」不可达；按聚类报告修正为 **「A 组 441 次调用降 80%」**：可替代集合只取 A 组（441 次，占 672 次的 65.6%，成本 1,236,931 B），目标是这 441 次里至少 353 次不再出现（剩 ≤88 次视图调用）。B 组 91 次（直读平台内部，P15 的地盘）与 C 组 140 次（本地工作，无平台数据）不计入分母。

估算方法（可复现，来自聚类报告 §3–§4 的计数，不是实测）：

1. **单位**是 bash 工具调用（聚类口径），不是脚本个数；A 组的 441 次调用落在 243 个互不相同的 `(worker, 目标 SHA7)` 查询上，平均 1.81 次脚本/查询。
2. **一次视图调用替代一组脚本**：按 §4 的「查询数」列，一次 `per_case` 还吸收 `correctness`（38 个查询）与 `calibration`（17 个）以及部分 `run_index`（15 个）——它们本来就是同一张逐例表；`diff`/`board`/`queue`/`history` 各自吸收本簇。
3. **下界**：若每个查询都留一次视图调用，441 → 243，降 44.9%；**80% 目标**要求同一 SHA 的多轮迭代复用同一份输出（同一 SHA 只查一次 `per_case`/`diff`，`queue`/`history` 由 `parhelion_wait` 唤醒后按需刷新），即平均 2.76 个查询组共享一次调用，剩 ≤88 次。
4. **字节口径另算**：聚类报告对 A 组的字节节省估计是 447,788 B（1,236,931 B 的 36.2%）；本次实测视图输出（`per_case` 2515 B，其余 668–3726 B）与原脚本均值（1.1–4.5 KB）同量级，说明收益主要在调用次数与回合数，不在单次字节。两个口径不能混用。
5. 未计入分母但确实被吸收的流量：`opsb --help` 的 114 次（schema 描述已写清列），以及队列/历史脚本的重复输出（`items` 预算截断后仍比 CLI 均值小）。

## 7. NOTE（非显然约束与源码事实）

1. **`state_card_ext.data()["cases"][].speedup` 在真实数据上恒为 `None`**：`scheduler.estimator._case_bench_pair()` 的 `cases` 以**本地** case id（`01`…）为键，而 `state_card_ext.case_rows()` 用**判题** case id（`#01` 或 Mongo ObjectId）取值。本视图先经 `case_map.current().pairs` 把 judge→local 映射，再读同一个 `speedups()` 的 map，因此列有值且与门禁同源；`parhelion_status(detail=true)` 仍会显示空加速比，建议后续由 P06 负责人修一行（不在本包改动范围）。
2. **`report.json` 的遗留单次运行形状不写 `min_us`**：真实 602 份里 3711/11116 个 case 有 `min_us`，其余只有 raw `device_us`。`scheduler.timing.case_value` 不从 `device_us` 推最小值，所以 `diff` 的分跑回退会读不到这些报告。本模块在 `_docs()` 读出后给缺 `min_us` 的 case 补 `min(device_us)`（只改内存副本），`diff`/`per_case` 因此能读遗留形状；门禁自身仍看不到这类报告，建议在 scheduler 侧统一（P10/P17 的地盘）。
3. **两种形状的差别只在套件级字段**：`level/suite_version/weights/timed/selection/digest` 有或无；`cases[].perf` 在两种形状里都是 bool（本次抽样 11116/11116 个 case 均为 bool），不能当对象读。
4. **`report.json` 路径不唯一**：主形态 `results/<title>/<sha>/bench-runs/<run>/report.json`；`_docs()` 同时扫 `results/<title>/bench-runs/*`、`results/<title>/bench-runs/*/*` 与 `<PARHELION_HOME>/workspaces/<id>/bench-runs/*`，并按文档里的 `sha` 过滤（不看目录名），去重后取 `formal` 优先、`finished_at` 最新。
5. **`queue` 的同源命令有写副作用**（§4.3）：`scheduler.cli_bridge status` 会追加 `results/feedback-log.jsonl`、刷新 `.feedback-signatures.json` 与 `results/<title>/.candidate-archive.json`。这不是 `handlers_report.py` 的写入，但「只读跑全部视图」在 `queue` 上做不到；本包没有绕过它（子计划明确要求 `ipc_bridge.merge_bridge_enrichment` 的 `gate_view`）。
6. **worker 作用域比 ledger 更严**：`handlers_ledger.owned_ledger` 在 bus 上没有题时允许调用方显式传 `problem`；本工具按 `WORKER_SCOPED_PROBLEM_TOOLS` 的 escape 语义，**必须有 bus 题且与请求一致**，否则 `PermissionError`。`board`/`env` 也先过这一关（worker 必须已注册）。
7. **`board` 的 live 缓存只有带队长标记时才被 `current_board()` 采纳**：`ranking_service._target()` 在无 `ranking-captain.json` 时返回 3 元组，`board_refresh_service.board_scope()` 对 3 元组直接返回 `{}`，`current_board()` 于是忽略 `live-board-tbest.json`（退到 `score-snapshot.json`）。真实工作区有队长标记，所以正常；fixture 里放了假的 `ranking-captain.json` 来覆盖这条路径，并断言输出不含 `target/team_id/team_name/account_id/user_id`。
8. **`per_case` 的 `base` 可能是 head**：`state_card_ext.base_of()` 取「最后一次刷新榜单的 SHA」，若刚投的这发上了榜，base 就等于 head（`speedups()` 返回空、`diff` 报「需要两个不同 SHA」）。视图如实返回 `base` 并在 `diff` 里拒绝同 SHA；建议后续在 `diff` 里回退到再上一发榜上 SHA。
9. **`bottleneck` 的 PMU 可用性取决于 `validation.log` 是否带计数**：真实 602 份报告里，只有少数题有 `result.json`/`validation.log` 产物；三个抽样题都得到 `category=unknown`、`pmu_available=false`，但 `raw.report/log/run_dir` 仍给出原始路径。`runtime_soc` 被剥掉（设备值）。
10. **`opsb history --json` 的 `problem` 是位置参数**，不是 `--problem`；CLI 的 `--workspace` 也只在各子命令下。覆盖表按实际输出（`opsb history --json <title> --workspace <ws>`）取字段。
11. **`case_calibration.table(write=False)` 是全只读的**（`refresh` 在 `write=False` 时不追加记录），但会为缺失记录现算；`per_case` 每次调用因此多一次 `case_map`/校准计算（真实数据 <0.15 s）。
12. **`diff` 的并列区间来自 `paired_timing.interval`/`bootstrap` 的既有实现**：本视图只做三态归类（`lo>1` / `hi<1` / 其余），没有重算区间；分跑回退用 `Welch` 区间（`method=welch_log`），与配对报告的 `log_t` 在 `method` 列区分。

## 8. 已知风险与建议后续

1. **`queue`/`history` 的字节预算靠截断**：真实全题队列 88 项、`limit=20` 仍只显示 3 条，`truncated=true` 且 `shown` 给出条数。P13 的 `bound_output` 应把 `queue`/`history` 改成先分页再落盘，本包不越权实现。
2. **`queue` 的写副作用**（§4.3）：建议给 `scheduler.cli_bridge` 一个 `--no-journal`/只读模式，或让 `invoke_bridge` 在报表场景传只读标志；在加上之前，对真实目录跑 `queue` 仍会追加 gate 日志。
3. **`per_case` 的加速比依赖 worker 的 bus 记录**：`problem_of` 走 `agent_bus.db` 只读打开；bus 不可读时 worker 会被拒绝（安全侧），主控不受影响。
4. **遗留 `report.json` 的 `min_us` 归一化只在视图层**：门禁/估分仍读不到这类报告；建议在 `scheduler.timing.case_value` 里加同样的回退，并用回放验证不改变既有分数。
5. **`state_card_ext` 的 judge/local 键缺陷**（NOTE 1）：建议单开一个小修，让 `parhelion_status(detail=true)` 的加速比列也有值；本包已在自己的视图里绕过。
6. **`board` 的队长标记依赖**（NOTE 7）：无队长时 `board` 只能给 `score-snapshot` 回退；P15 给 worker 的 `board` 数据通道需要确保工作区有队长或改 `board_scope` 的行为。
7. **fixture 是人工裁剪的**：`cli/tests/fixtures/p12_report` 只保留形状，不含真实 SHA/账号/设备/主机值；测试动态建 git 历史，因此 fixture 里的 `sha-cand`/`sha-base` 目录名只能在测试内出现。新增字段时需同步 fixture（`hypothesis`：后续把 fixture 生成器放到 `cli/tests/` 里，避免 JSON 手工漂移）。
8. **真实数据验证的只读边界**：六个视图已证明零写入；`queue` 的副作用与 `~/.parhelion` 缓存写入（临时 home 之外未发生）都已在 §4 披露，未尝试回滚真实 `feedback-log.jsonl`（平台自己的 journal，daemon 平时也会写）。
