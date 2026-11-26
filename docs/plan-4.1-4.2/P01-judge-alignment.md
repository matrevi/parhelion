# P01 判题对齐与回执计分（4.1 #5 #7 #8）

分支：`impl/P01-judge-alignment`。先做 #8，再 #5，最后 #7；三者共用一个「规范化回执」与一份「用例映射」。

## 证据（主会话已核实）

- 回执行在 `<contest>/results/submit-queue-outcome.jsonl`。刷新了最高分的行只有 `score` 与前 5 个公开例的 `public_times_us` / `public_tbest_us`；**未**刷新最高分的行 `score` 为 `None`，分数与全部 15 例时间在 `timing_score` 里：`{score, formula: "judge_log1p5_mean", case_count, reference_rows, tbest_by_case{testcase_id: us}, time_by_case{testcase_id: us}}`。108 行里只有 11 行带 `timing_score`。WA 行不记录哪个用例失败。
- 计分公式（`cli/opsb_core/services/judge_score_fetch.py:209-226` 的 `replay`）：每例 `100 / (1 + ln(t/tbest)/ln 1.5)`，`t ≥ tbest`，对全部用例取均值；tbest 为 None 的例记 0 分。
- `scheduler/case_bench.py:741-790` 的 `timing_offset` / `public_estimate` 用 `zip(sorted(official_ids), public_times_us)` 按位置把本地「official」用例与判题公开例配对。
- 按位置配对是错的。同 SHA 的本地正式运行最小值 ÷ 判题 `time_by_case`（按字典顺序）：
  - NeighborList d70506f：0.58–2.87；
  - EdgeIndexCoalesce 16f86a6：1.54–107；
  - CGTensorProduct 0a516d3：0.81–12.0。
  
  FFT 本地用例 05（8×256³，7.4s/次）对应位置的判题公开例只用 8.46ms、tbest 24µs，物理上不可能是同一形状。
- `scheduler/estimator.py:698` `_project_score_from_speedup` 用几何平均加速比做经验投影；NeighborList 两次高估 3.5 与 5.1 分。`_receipt_residuals`（:1245）按提交时刻重放并缓存在 `results/.case-bench-residuals.json`。
- 读取 `score` 的地方很多（门禁熔断、队列视图、标定、估分、状态卡、`agents/judge_feedback.py`、`services/sync_reconciler.py`、`knowledge/bugs.py`、`scheduler/case_feedback.py`、`services/contest_service.py`）。用 `grep -rn '"score"\|\.get("score")\|\["score"\]'` 全部排查。

## #8 回执分数字段归一

1. 新模块 `scheduler/receipts.py`（≤350 行）作为回执行的唯一读口：
   - `official_score(row)`：Pass 时取 `score`，否则取 `timing_score.score`；非 Pass 为 0。
   - `beat_board(row)`、`case_times(row)`、`case_tbest(row)`、`case_results(row)`。
   - `normalize(row)`：返回补齐的新行，旧字段不删。
2. 入库时写规范字段。`services/submit_runner.py` 的轮询（约 :1150-1225）和对账路径（`sync_reconciler.py`、`submit_reconcile.py`）对**每一条**回执（Pass/WA/RE/TLE）都写入：
   - `official_score`；
   - `case_results`：`[{testcase_id, status, time_us, tbest_us}]`，按判题返回顺序，来自 submission detail 的 `result` 行；
   - `time_by_case` / `tbest_by_case`：能得到时就写，不再只在 `timing_score` 里有。
   
   `score` 字段语义保持不变（兼容 GUI 与旧读者）。
3. 把上面列出的读者全部改走 `receipts.*`。熔断器只认 `status`，WA/RE 的 `official_score=0`。
4. 回填：`opsb bench receipts backfill [--apply]`，默认只列出。只使用磁盘上已有的数据，包括 outcome 行里的 `timing_score`、`results/<title>/<sha>/` 下的回执文件，以及 `results/scoring-evidence/` 的缓存。本包不发起任何网络请求；今后的新回执由常规轮询按第 2 条写全。
   - 回填结果追加为新行，并带 `source="receipt-backfill"`。
   - 去重仍按 `submission_id`；`_load_outcomes` 的择优规则要让规范行胜出。
   - 本地补不全的行如实列为缺失。
   - 单测全绿后，可以对真实比赛执行带 `--apply` 的回填。

## #5 本地用例与判题用例的对应关系

1. 查清两边的用例来源：
   - 本地 official 用例来自哪里：题目模板里的描述符，经 `runtime/npu_runtime/reference_cases.py` 与 `scheduler/suite.py`；
   - 判题用例的 roster 顺序、ID 与可见元数据：problem detail 的 `rule.testcases`、ranking 行。
   
   在报告里写明证据。
2. 查清本地 `device_us` 的计时范围：`runtime/npu_runtime/case_batch.py`、`device_broker.py`、`reference_cases.py`、`arena_*`。事件区间是否包含文件读写、H2D/D2H、多次 launch？与判题口径（逐例 `time`）对比。FFT 05 在本地是 7.4s，要给出数量级上的解释。
3. 新模块 `scheduler/case_map.py`：
   - 每题建立 `local_case_id ↔ judge_testcase_id` 映射，写到 `results/<title>/.case-map.json`，包含 `suite_version`、`judge_ids`（roster 顺序）、`pairs`、`method`、`confidence`、`evidence`（所用 submission_id 与 SHA）、`unmapped_local`、`unmapped_judge`。
   - `method` 优先用判题元数据（`roster`），其次用多 SHA 一致的指派（`assignment`：对 log 时间做带公共偏移的最小代价匹配，至少 2 个 SHA 一致才算 confident），都不行时为 `unmapped`。
   - 新回执到达时增量重算；结果只在新证据下变化。
4. 消费方改用映射：`timing_offset`、`public_estimate`、状态卡逐例对比，以及 #7。没有判题对应的本地用例（如确认 FFT 05 不在判题里）标 `judge_mapped=false`：
   - 不参与分数预测与计时偏移；
   - 正确性检查保留；
   - 是否从 standard 计时中移出以省设备时间，由你给出数据后提出建议，不要自行删用例。
5. CLI：`opsb bench case-map <Problem> [--json] [--rebuild]`，经 `scheduler/cli_bridge.py` 子命令实现。

## #7 用回执逐例时间直接校准预测

1. 在 `estimator` 旁新建 `scheduler/receipt_predict.py`。对候选 SHA c 与榜上 SHA b：
   - 判题逐例时间 `T_b[j]` 来自 b 最近的规范回执；
   - 本地配对比 `r[j] = local_min(b, i(j)) / local_min(c, i(j))`，同套件版本、正式、timing_eligible，扣除已标定的计时偏移后再求比；
   - 预测 `t̂_c[j] = T_b[j] / r[j]`，再按 #8 的公式、用当前 `tbest_by_case` 逐例计分取均值。`t̂ < tbest` 记 100 分，并标注「会刷新 tbest」。
   - 没有映射的判题例沿用 `T_b[j]`（视为无变化）；覆盖率进入置信度。
2. `ScoreEstimator.estimate` 在数据齐全时用这个预测（`estimate_method="receipt_per_case"`），否则回落到现有 `_case_bench_projection`。`_receipt_residuals` 对新方法同样按提交时刻重放，缓存键带上方法名，避免与旧缓存混用。门禁 S2 的「期望提升 E[max(0,增益)] ≥ 底线且 P(增益>0) ≥ 0.2」逻辑不变，额度不进阈值。
3. tbest 会漂移：取最近一次观测，记录观测时间。比较以同代 tbest 为基线（AGENTS.md §4）。

## 验收

- 单测：
  - `receipts.official_score` 覆盖四种回执；
  - 两份真实形状的回执 fixture（去敏）的规范化；
  - 用合成数据验证 `case_map` 的 roster / assignment / unmapped 三条路径；
  - `receipt_predict` 的公式与 `judge_score_fetch.replay` 对同一组时间结果一致；
  - 重放同一批回执不重复写行。
- 回测：用现有回执做留一回测，比较旧投影与新方法，给出 MAE、偏差和覆盖题数。写入 `docs/plan-4.1-4.2/reports/P01-backtest.md`，附各题的用例映射结论与 FFT 05 的解释。
- 真实数据：在真实比赛上执行 `case-map --rebuild` 与 `receipts backfill`（先列出，再 `--apply`），报告产出。
