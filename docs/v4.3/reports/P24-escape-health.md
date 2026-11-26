状态：已交付

# P24 逃逸锁出口与 S1 体检

分支 `impl/P24-escape-health`，基于 `main`（`e2ebd00`，含已合入的 P21、P22、P26）。子计划「范围」1–5 与「P21 审查后补充」全部实现。

一句话：判题判决不是源码的属性。本包给 WA/RE 逃逸锁加了「未复现即降级为提示」的出口，并让 S1 读编译秒数、模板实例化数、内核目标文件字节数与冷启动比值；除「判题时限风险」这一条（00-PLAN D3）外全部只提示、不暂缓。

## 1. 分支与提交

```text
$ git log --oneline main..HEAD
<deliver> Deliver P24: release an unreproduced escape lock and record the S1 health
bff0be2 Release an unreproduced escape lock and add the S1 mechanical health check
```

第一条就是带本报告的提交：提交号由提交动作本身决定，无法写进它自己的内容里，所以留 `<deliver>`；实现提交的哈希是准确的。基线 `main = e2ebd00`（P21 + P22 + P26）。没有推送、没有合入 `main`、没有 rebase（分支两次 `--ff-only` 跟到最新 `main`，中间无本地提交）。

改动文件（`git show --stat HEAD`）：

| 文件 | 行数 | 说明 |
|---|---:|---|
| `scheduler/health.py`（新） | 347 | 四个指标、来源合并、elf_guard 告警、增长百分比、判题时限风险公式 |
| `scheduler/wa_repro.py` | +95/−7 | `unreproduced` 状态、`escalation`、`next_step`、`lock_view` |
| `scheduler/gate.py` | +102/−35 | 6 个 `GateConfig` 开关、`_escape_lock_stage`、`_apply_health`、S1 两个接入点、EXPLORE/减半/惩罚 |
| `scheduler/judge_noise.py` | +59 | `verdict_flips` / `flip_count`（同源判决翻转） |
| `scheduler/case_bench.py` | +16 | `build_metrics` 事件入报告；正式 `result.json` 落盘体检字段 |
| `scheduler/tests/test_health.py`（新） | 326 | 17 条 |
| `scheduler/tests/test_wa_repro.py` | +127 | 7 条新增（共 20 条） |
| `runtime/npu_runtime/reference_cases.py` | +50/−1 | 远端只用标准库：编译秒数、模板实例化扫描、目标文件字节 |
| `cli/opsb_core/fast_feedback/elf_guard.py` | +8/−1 | `build_metrics=` 覆盖（跑测报告里的编译数字也能出告警） |
| `cli/opsb_core/services/gate_view.py` | +14/−3 | `health_of`、`card_fields.health`、`HOLD: JUDGE TIME`、`ESCAPE_SHA_REPEAT` |
| `cli/opsb_core/mcp/handlers_bench.py` | +1 | submit gate 回复加 `health`（无新参数） |
| `cli/opsb_core/services/judge_rules.py` | +41 | `time_limit_sec` / `iterations` 读取（P22 快照） |
| `cli/opsb_core/ledger/model.py` | +4/−3 | `measurement` 允许平台写 `judge_noise` 标记 |
| `cli/opsb_core/ledger/auto.py` | +66/−2 | `apply_judge_noise` / `on_judge_noise`，挂在 test settle 与 receipt 两处 |
| `cli/opsb_core/mcp/report_views.py` | +2/−1 | rules 视图 `flips` |
| `cli/opsb_core/mcp/handlers_report.py` | +2/−1 | rules 帮助文字 |
| `cli/tests/test_judge_noise_ledger.py`（新） | 178 | 6 条 |

新模块 `scheduler/health.py` 347 行（≤350）。`gate.py` 只保留薄接入：判定在 `wa_repro.escalation`、体检在 `health.py`；未改 `cli_bridge.py`（无字段变化，Swift 不用动）、未改 `estimator.py` / `manager.py` / `handlers_bench.py` 的字段语义。

## 2. 子计划逐项完成情况

| 范围 | 状态 | 落点 |
|---|---|---|
| 1. 未复现的出口 | 已完成 | `wa_repro.status` 的 `unreproduced`、`UNREPRO_RUNS=2`、会话复用条件、`next_step` |
| 2. 门禁的处理 | 已完成 | `gate._escape_lock_stage` + `wa_repro.escalation`：其他 SHA `ESCAPE_UNREPRODUCED` / EXPLORE / 减半 / 原因写坏 SHA 与回执；坏 SHA `ESCAPE_SHA_REPEAT`（TLE 规则）；开关 `escape_unreproduced_releases` |
| 3. 判题噪声事件 | 已完成 | `ledger.auto.apply_judge_noise`：未复现与同源翻转各一行 `measurement`，带 `judge_noise`，按 `submission_id` + 种类幂等；P22 的 rules 视图显示 `flips` |
| 4. S1 体检 | 已完成 | `health.check(workspace, title, sha, board_sha)`；四个指标；elf_guard 告警；与在榜 SHA 的增长百分比；case-bench 报告原本没记这些字段，已在远端脚本与 `result.json` 补记；数据缺失跳过、不暂缓 |
| 5. 体检进入门禁 | 已完成 | S1 `metrics["health"]` + `parhelion_submit_gate` 回复的 `health`；`health_penalty=0.8`；`JUDGE_TIME_RISK` 暂缓；`health_time_risk_holds` / `health_checks` 开关；阈值只用中性默认值 |
| P21 审查后补充·5 发新高 | 已完成 | §8.2 逐条 |
| P21 审查后补充·`REFERENCE_SUITE_REQUIRED` | 已完成 | §8.3 单列 27 发，不算本包效果 |

一处与子计划文字的差异：第 3 项说「如果 P22 已合入，rules 视图里显示本题的翻转次数」——P22 已合入，所以**接了 rules 视图**，没有走「跳过并注明」那条退路。

## 3. `unreproduced` 的判定口径

对一条 WA/RE 回执 `sid`（坏 SHA = `bad_sha`），`wa_repro.status(ws, title, sid, bad_sha)` 从磁盘上不可变的运行记录判定，顺序如下：

1. **`none`**：清单 `bench-specs/<题>/wa-repro.json` 里没有这条回执，或没有变体 → 什么都不猜。
2. **`reproduced`**（优先于未复现）：`results/<题>/<坏SHA>/bench-runs/*/report.json` 里存在一次运行，其 `cases` 中至少一个**该回执的变体**状态是 `WA`/`RE`（`as_of` 过滤照旧，回放用）。取最早一次这样的运行作 `reproduced_run`。
3. **`fixed`**：在 `reproduced` 前提下，候选 SHA 的正式运行**覆盖全部变体**、全部 `PASS`，且每个变体的 `key` 与坏 SHA 那次失败运行的 `key` 相同（同一输入）。
4. **`unreproduced`**：没有第 2 步的失败运行，且满足全部三条：
   - 运行是**正式运行**：`report["formal"] is True`；
   - 运行是**final 级**：`report["level"]` 为 `final`（变体只进 final 级；4.1 之前的老报告没有 `level` 字段，按 `None`/`""` 视作可接受，见 NOTE 3）；
   - 每次运行覆盖**全部**变体且全部 `PASS`（少一个变体、或多个变体行里有任何非 `PASS`，该次都不计）；
   - 这样的运行数 ≥ `unrepro_runs`（默认 2，`GateConfig.unrepro_runs`）；
   - **会话复用**：如果这些通过的运行里至少一条记录了 `settings.reuse_session`（能区分），则其中至少一条必须是 `reuse_session: false`（关闭会话复用/冷会话）；若运行格式不记录该字段，则不做此要求（无法区分就不假装区分）。
5. 次数不够或会话条件不满足时保持 `pending`，视图带诊断字段：`pass_runs`、`required_runs`、`session_reuse_recorded`、`session_reuse_off`，`next_step` 追加「N/2 次正式 final 运行全部通过」。

`next_step`（worker 面向，英文，与平台其它消息一致）：

```text
not reproduced in 2 formal final run(s) on <bad>: all 15 wa-repro variants passed each time;
treat the online verdict as judge-side noise — the escape lock is downgraded to a hint
```

即子计划要求的「N 次未复现，按判题侧噪声处理，锁降级为提示」。

`escalation` 把状态翻成 S1 的一个决定（`release=False` 即关掉开关，回到 4.2.1）：

| repro 状态 | 候选 | action | 阶段码 | S1 | status_tag | 优先级 |
|---|---|---|---|---|---|---|
| `fixed`（reference 分支） | 任意 | pass | `ESCAPE_REPRO_FIXED` | 过 | 照常 | 照常 |
| `unreproduced` | 其他 SHA | release | `ESCAPE_UNREPRODUCED` | 过 | `EXPLORE` | ×0.5 |
| `unreproduced` | 坏 SHA 本身 | repeat | `ESCAPE_SHA_REPEAT` | 拒 | `REJECTED_GATE` | — |
| 其它（含 `none`/`pending`/`reproduced`） | 任意 | hold | `ESCAPE_LOCK_ACTIVE` | 拒 | `REJECTED_GATE` | — |

`ESCAPE_UNREPRODUCED` 的 `reason` 写明坏 SHA、它的线上判决、回执 `submission_id` 与未复现次数（例：`ESCAPE LOCK released for fffffff: bbbbbbb failed online (Runtime Error, submission 6b07… ) but the failure did not reproduce in 2 formal final run(s)`）。`ESCAPE_SHA_REPEAT` 与 `TLE_SHA_REPEAT` 同规则：线上 Pass 过（`_prior_online_pass_shas`，带 `as_of`）才放行，否则只能改内核或恢复一个线上 Pass 过的 SHA。

## 4. 判题噪声账本行与 rules 翻转次数

- **同源判决翻转**：`judge_noise.verdict_flips(rows, title)` 按 `(题, sha12)` 分组、按提交时间排序，相邻两条已判回执的**判决类别**不同即一次翻转（类别：Pass/Accepted→pass；WA/RE/TLE/CE/MLE/SE/Fail 各自一类）。`Pass→WA→Pass` 记 2 次（与调研里那 1 例一致）。
- **账本行**：`ledger.auto.apply_judge_noise` 写平台账本（`worker=platform`、`source=platform`）：
  - `key="judge.noise.flip"`、`value=1`、`unit="count"`、`receipt_id=<新回执>`、`sha`、`judge_noise={"kind":"flip","from":…,"to":…,"previous_submission_id":…}`，`ekey="judge-noise:<sid>:flip"`；
  - `key="judge.noise.unreproduced"`、`value=<运行数>`、`unit="count"`、`receipt_id=<回执>`、`judge_noise={"kind":"unreproduced","runs":…,"variants":…,"session_reuse_off":…}`，`ekey="judge-noise:<sid>:unreproduced"`。
  - 幂等键 = `submission_id` + 种类：同一批回执重放、守护进程重启、投递回执重放都写不出第二行（测试逐字节比对账本文件）。
- **触发点**：`on_test_settled`（新的正式运行可能证明「未复现」）与 `on_receipt`（新判决可能翻转）。两处都包在 `guard` 里，账本故障不影响测试结果或回执。
- **rules 视图**：`parhelion_report(view="rules")` 的单题回复多一个 `flips`（本题同源判决类别变化次数），`view="help"` 的 rules 说明同步加了一句；全表视图不加列（P22 的列已固定，字节余量只有约 300 B）。

## 5. 体检字段在各报告格式里的来源

`health.check` 取**该 SHA 最近一次正式运行**，逐字段「先到先得」：

| 字段 | `bench-runs/<run>/report.json`（reference 路径） | `results/<题>/<sha>/result.json` | 日志 |
|---|---|---|---|
| 编译秒数 `compile_sec` | 新增 `build_metrics[].compile_sec`（候选臂 `candidate=true`） | `phase_a_cpu_sec` | `validation.log` 的 `BUILD_ONLY_PASSED (…s)` / `Phase-A CPU build failed in (…s)`（elf_guard 解析） |
| 模板实例化数 | `build_metrics[].template_instantiations`（`template_source="source_scan"`） | `template_instantiations` / `template_instantiation_count` | `KERNEL_BINARY_STATS`（`npu-test.py` 的旧路径） |
| 内核目标文件字节 | `build_metrics[].kernel_o_max_bytes`（另有 `kernel_o_count` / `kernel_o_sum_bytes`） | `kernel_o_max_bytes` / `kernel_o_sum_bytes` | `KERNEL_BINARY_STATS` |
| 冷启动比值 | 无（reference 路径不跑 cold3） | `cold3_stats`（`first_iter_us`、`warm_median_us`、`per_case`）或 `first_iter_us` + 中位/最小 | `COLD3_STATS` / `MS_PROF`（`cold3_probe` 解析；无时序时用 max/中位/最小算「synthetic order」） |
| 会话复用 | `settings.reuse_session` | — | — |
| elf_guard 告警 | 用上面的指标喂 `elf_guard`（`build_metrics=` 覆盖） | 同 | 同 |

**先核实的结论**：case-bench（参考模板路径）的报告**原本没有记录这些字段**——两场的 602 份 mat-chem `bench-runs/*/report.json` 里 `build_metrics` 为 0；case-bench 写的正式 `result.json` 只保留 `sha/title/exit_code/finished_at/verified/run_id/oracle/official/summary/built`。所以按子计划「没有记录就在测试时补记」补了两处，远端只用标准库（`re`/`pathlib`/`time`，见 `runtime/npu-test.py` 头部约定）：

1. `runtime/npu_runtime/reference_cases.py` 每次构建后发一个 `build_metrics` 事件：`compile_sec`（cmake 配置+build 的墙钟）、`template_instantiations`（`TILING_KEY_IS`/`REGISTER_TILING_KEY`/内核宏/显式实例化的源码扫描，与 `npu-test.py` 和 `elf_guard` 的口径一致）、`kernel_o_count/sum/max_bytes`（build 树里的 `.o`）；`candidate=true` 标出候选臂（A/B 配对运行时另一臂为 `false`）。
2. `scheduler/case_bench.py` 把它存进 `report["build_metrics"]`，并在写正式 `result.json` 时摊平为 `phase_a_cpu_sec` / `template_instantiations` / `kernel_o_count` / `kernel_o_sum_bytes` / `kernel_o_max_bytes`——与 `npu-test.py` 的字段同名，所以 `elf_guard`、`cold3_probe` 与其它老读者不需要改。

数据缺失时：`checked=false`、`skipped` 写明缺什么（`no formal run record for <sha>` / `no compile seconds recorded` / `judge publishes no time limit…`），**不暂缓**。实测覆盖：ops-bench 905 份 `result.json` 里 547 份有编译秒数、203 份有模板实例化（与 R3 的 203 一致）、264 份有目标文件字节、153 份有 `first_iter_us`；mat-chem 203 份里只有 31 份有编译秒数（case-bench 旧路径），下一场开始才会带全。

## 6. 判题时限风险公式

```text
compile_term   = compile_sec × judge_compile_factor            (GateConfig, 默认 1.0)
run_term       = predicted_run_sec × iterations                 (做不出预测时为 0)
predicted_run_sec = Σ 逐例预测时间 t_pred(us) / 1e6             (scheduler.receipt_predict.predict)
iterations     = P22 规则快照 results/<题>/.judge-rule.json 的 iterations
                 → 没有就 config.json 的 judge.iterations → 都没有取 1
limit_sec      = P22 规则快照的 time_limit（支持 *_ms 换算）
                 → 没有就 config.json 的 judge.time_limit_sec（默认空）
budget_sec     = health_time_margin × limit_sec                (默认 0.8)
risk           = compile_term + run_term ≥ budget_sec
```

- `predicted_run_sec` 用的是判题站真正计时的用例（`receipt_predict` 的 `cases[*].t_pred`），不是本地时间；无 case map / 无已判基线时返回 None，公式退化为「只用编译项」。
- 没有时限（快照不发布、`judge.time_limit_sec` 也没配）时**整条跳过**（`risk=None`），不拿猜的值当阈值。
- 触发时（exploit）：S1 换成 `JUDGE_TIME_RISK`（`passed=false`），`status_tag=REJECTED_GATE`，卡片 badge 为 `HOLD: JUDGE TIME`，`metrics.next_step` =「减少实例化或编译量；或请求主控决定」。本 base 没有 P23 的 `intent` 参数，所以按子计划只写「或请求主控决定」（P23 合入后应改成「或用 intent=probe 发」）。
- `health_time_risk_holds=false` 时同一条判断只进 S1 `reason` 与 `metrics.health.time_risk`，不拦。`health_checks=false` 时连体检都不做（等于 4.2.1 的 S1）。
- 阈值只写中性默认：`GROWTH_WARN_PCT=50.0`（增长百分比告警）、`HEALTH_TIME_MARGIN=0.8`、`health_penalty=0.8`；判题时限与 `judge_compile_factor` 这类部署值只从 `config.json` 的 `judge`/`device` 段读，仓库里没有真实设备值。

## 7. 测试

```text
$ ~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider
880 passed, 19 skipped, 2 subtests passed in 85.73s (0:01:25)

$ PYTHONPATH=cli:. ~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider cli/tests runtime/tests
595 passed in 67.75s (0:01:07)
```

本包新增 30 条测试（基线 `e2ebd00`：全套 869 → 899 收集，其中 880 passed / 19 skipped；`cli/tests+runtime/tests` 589 → 595）。`main` 之后又前进到 `e681ed3`（只多一条回放入口测试与 aoci 索引），本包按硬约束没有 rebase，故基线按 `e2ebd00` 计：

- `scheduler/tests/test_health.py`（新，17 条）：数据缺失跳过；正式运行报告优先、`result.json` 补缺；老格式顶层字段；非正式运行不读；cold3 冷启动比值与增长；规则快照的时限与迭代；`config.json` 回落、无时限跳过；`judge_compile_factor` 放大；无编译秒数跳过；`judge_rules` 的秒/毫秒/字典与 iterations；远端扫描器（模板数、目标文件字节、跳过 build 目录）；`case_bench.build_record`；体检进 S1 指标并 ×0.8；`JUDGE_TIME_RISK` 暂缓 + badge + next_step；开关关掉只提示；`ESCAPE_UNREPRODUCED` → EXPLORE + 半权；开关默认值与 `set_switch` 边界。
- `scheduler/tests/test_wa_repro.py`（+7 条）：一次通过不够（1/2）；部分覆盖不计、失败压过全过；只认正式 final 运行；会话复用记录时必须有一次关闭；`escalation` 四态；S1 实测（其他 SHA 放行 `ESCAPE_UNREPRODUCED`、坏 SHA 拒 `ESCAPE_SHA_REPEAT`、开关关掉都回 `ESCAPE_LOCK_ACTIVE`）。
- `cli/tests/test_judge_noise_ledger.py`（新，6 条）：翻转一行、重放逐字节不变；翻转回来再一行；未复现一行、`value=2`、重放不变；只有一次运行不写；rules 视图 `flips`；submit gate 回复带 `health`。

Swift：**未改 Swift**（`git diff main -- Sources Package.swift` 为空），不需要 `swift build`。

一条偶发：早先一次 `cli/tests runtime/tests` 里 `cli/tests/test_orchestration_v4.py::test_sprint_modes_open_one_two_or_three_seats_per_problem` 失败，报的是临时目录清理时的 `shutil` 错误（P26 新测试，高负载下的清理竞态）；单跑通过，重跑全套 595 全绿。与本包改动无关，记录在此。

## 8. 真实数据验证

`opsb replay gate` 本身就在自己的临时副本上重放；TLE 与逃逸锁的逐条核对另把两场复制到 `/tmp/p24-verify/` 再做。真实比赛目录、`~/.parhelion/workspaces`、会话日志、总线均未写入（本包所有写入都在 `/tmp/p24-verify/**` 与 `/tmp/p24h` 下；未访问判题接口）。

### 8.1 回放改前改后（P21 验收）

命令：

```bash
# 改后（本包默认）
opsb replay gate --workspace <副本> --groups default --json
# 改前（= 4.2.1：两个新开关都关）
opsb replay gate --workspace <副本> --groups default \
  --gate escape_unreproduced_releases=false --gate health_checks=false --json
```

| 指标 | ops-bench 改前 | ops-bench 改后 | mat-chem 改前 | mat-chem 改后 |
|---|---:|---:|---:|---:|
| 重放发数 | 233 | 233 | 78 | 78 |
| 被拦 | 50（21.5%） | **50** | 66（84.6%） | **66** |
| — S1 SAFETY | 9 | **9** | 42 | **42** |
| — S2 NOISE_FLOOR | 41 | **41** | 24 | **24** |
| 阶段码分布 | `BELOW_NOISE_FLOOR` 41、`ESCAPE_LOCK_ACTIVE` 6、`MISSING_CROSS_CORE_STRESS` 2、`MISSING_EXTENDED_PASSED` 1 | **同左** | `REFERENCE_SUITE_REQUIRED` 27、`BELOW_NOISE_FLOOR` 24、`EXIT_CODE_2` 9、`ESCAPE_LOCK_ACTIVE` 6 | **同左** |
| **frozen（G3，合计/最长）** | 1 / 1 | **1 / 1** | 24 / 3 | **24 / 3** |
| blocked_new_highs | 18 | **18** | 35 | **35** |
| blocked_restores | 10 | **10** | 8 | **8** |
| 被拦时下一步非空 | 6 / 50 | **6 / 50** | 6 / 66 | **6 / 66** |
| 新高率 / 硬失败率 | 35.6% / 6.4% | **同左** | 33.3% / 24.4% | **同左** |
| 收官差额 | 10.19 | **10.19** | 0.00 | **0.00** |
| 单场耗时（一组） | 57.4 s | 63.3 s | 6.6 s | 5.9 s |

**两场完全不变**，原因是真实数据里 `unreproduced` 一次都不成立（NOTE 1）。改前口径与 P21 报告的 4.2.1 基线逐项一致（50/9/41、frozen 1、新高 18、restore 10、10.19），说明本包没有误伤既有门禁行为。体检使回放多花约 3–6 s / 233 发（约 13–25 ms/发，随机器负载波动；早先单独比 `health on/off` 是 +3.1 s）；mat-chem 78 发在噪声内（5.9 vs 6.6 s）。

**为了看出口真的能用，在 mat-chem 的 `/tmp` 副本上补记了两条正式 final 运行**（坏 SHA `9ffca10…` 的 15 个变体全 PASS，04:22 复用会话、04:24 关闭会话复用，都在 04:26 那一发之前）：

| mat-chem 副本（合成两条 final 运行） | 补记前 | 补记后 |
|---|---:|---:|
| 被拦 | 66 | **65** |
| `ESCAPE_LOCK_ACTIVE` | 6 | **5** |
| frozen（G3） | 24 | **22** |
| `QuantScaleCalib` `5041c86`（其他 SHA） | S1 拒 `ESCAPE_LOCK_ACTIVE` | **放行**，`status_tag=EXPLORE`，next_step「not reproduced in 2 formal final run(s)…judge-side noise…downgraded to a hint」 |
| `9ffca10`（坏 SHA 自己的那次重投） | S1 拒 | 仍拒（那一发早于两条运行；补记后用同 SHA 重投会得到 `ESCAPE_SHA_REPEAT`，由单测钉住） |

frozen 24→22 的构成：04:20 那次 WA 之后的连冻从 2 变 0（04:26 的 `5041c86` 被放行、进入了 S2），04:26 那次 WA 之后的连冻仍是 1。

### 8.2 逃逸锁拦下的 5 发新高的去向（P21 审查后补充）

这 5 发都在 ops-bench 的 4.2.1 默认回放里被 S1 逃逸锁拦下。逐条（「锁由谁挂上」= 该题当时未解决的线上 WA/RE；「4.3 判定」= 本包默认行为）：

| 新高 | 题 | 分数 | 锁由谁挂上（回执） | repro 状态 | 4.3 判定 | 如果未复现成立（反事实） |
|---|---|---:|---|---|---|---|
| `af0807c` | LayerNormV3 | 43.66 | `07ae054` WA，sid `6af2bffe…` | none | **仍被拦** | S1 放行 → S2 `LOW_GAIN_POSITIVE` → approved EXPLORE，priority 1.86 |
| `de25877` | SyncBatchNormGatherStats | 68.03 | `080ee02` WA，sid `6b02de02…` | none | **仍被拦** | S1 放行 → S2 `ABOVE_NOISE_FLOOR` → approved EXPLORE，priority 5.75 |
| `ba4a357` | StridedSliceGrad | 71.57 | `dfa784f` WA，sid `6b03220c…` | none | **仍被拦** | S1 放行 → S2 `ABOVE_NOISE_FLOOR` → approved EXPLORE，priority 43.41（榜面 0，属恢复） |
| `dcc7754` | CTCLossV2 | 75.11 | `a1df9d5` **RE**，sid `6b041daa…` | none | **仍被拦** | S1 放行 → **S2 `BELOW_BEST_IN_RESTORE` → 仍被 S2 拦** |
| `0d6ea06` | ConcatD | 57.14 | `6933c00` WA，sid `6b02c73d…` | none | **仍被拦** | S1 放行 → S2 `EXPLORE_HIGH_UPSIDE` → approved EXPLORE，priority 5.89 |

**依据**：

- 5 条的锁分别来自另一发 SHA（不是新高自己），符合「对其他 SHA 放行」的场景；`dcc7754` 的锁确实来自一次 RE（`a1df9d5` 线上 `Runtime Error`），4.2.1 的 TLE 修复本来救不了它——本包的出口正好覆盖这一类。
- 但它们的 repro 状态全是 `none`：**ops-bench 的两个平台工作区里没有任何 `wa-repro.json`**（`~/.parhelion/workspaces/ops-bench-*`、`opsbench-*` 都没有），这些回执早于 4.1 的复现变体功能。没有变体就永远到不了 `unreproduced`，所以 4.3 的回放判定与 4.2.1 相同。
- 反事实列是同一台机器上把 `wa_repro.status` 换成「未复现」后逐发重算的；它说明：**出口放行只到 S1**，S2 仍按噪声底线/恢复规则独立判定。`dcc7754` 会继续被 S2 拦，这正是 P23/P29 的定价范围。

结论：**下一场**（回执在 ingest 时就规划变体、且能跑 final 级）这 5 发的「其他 SHA 类」会被 S1 放行，`dcc7754` 这类还要 S2 点头；历史 ops-bench 数据上无一可变，因为缺证据而不是因为出口没实现。

### 8.3 `REFERENCE_SUITE_REQUIRED` 单列

mat-chem 回放里有 **27 发** `SAFETY:REFERENCE_SUITE_REQUIRED`（改前改后相同，均为 27）。它们来自**证据格式漂移**：旧回执早于参考套件取证，回执里 `result.json` 的 `scheme` 不是 `reference`，而今天的仓库有 `reference.py`，于是 S1 要求它重跑。这不是当天的门禁判定，**不算本包的效果，也不计入拦截率对比**；本报告 §8.1 的 mat-chem 拦截数按 P21 的口径原样保留（含这 27 发），P21 报告 §6 偏差 1 已说明。

### 8.4 TLE 回执逐条核对

ops-bench 上 7 条 TLE 回执（`results/submit-queue-outcome.jsonl`），用本地 `results/<题>/<sha>/result.json` 跑 `health.check`（副本上）：

| # | 题 | sha7 | submission_id | 本地健康字段 | 体检指标 | elf_guard 告警 | D3 时限风险 |
|---|---|---|---|---|---:|---|---|
| 1 | MultilabelMarginLoss | `f77146d` | `6af26d95…` | 只有 `exit_code/sha/finished_at` | —（`checked=false`） | — | 未评估（无编译记录） |
| 2 | PReluGradUpdate | `0761133` | `6af29cde…` | 同上 | —（`checked=false`） | — | 未评估（无编译记录） |
| 3 | QuantUpdateScatter | `becddb8` | `6b02da42…` | `phase_a_cpu_sec=40.16` | compile 40.16 s | 无 | 未评估（无时限） |
| 4 | LayerNormV3 | `cec9fed` | `6b02ffd5…` | `phase_a_cpu_sec=228.79` | compile 228.79 s | **`COMPILE_TIME_TLE_RISK`（redline）** | 未评估（无时限） |
| 5 | ConfusionTransposeD | `b32a0a0` | `6b0342c6…` | `phase_a_cpu_sec=218.55` | compile 218.55 s，冷启动比 1.548 | **`COMPILE_TIME_TLE_RISK`（redline）** | 未评估（无时限） |
| 6 | MultilabelMarginLoss | `32e3c24` | `6b04137c…` | `phase_a_cpu_sec=18.45`、`template_instantiations=30`、`kernel_o_max_bytes=70472` | 同上 | **`ICACHE_TEXT_BLOAT`（.text 58 168 B ≥ 32 768 B，redline）** | 未评估（无时限） |
| 7 | ConcatD | `b4fc9d3` | `6b041443…` | `phase_a_cpu_sec=44.62`、`template_instantiations=3`、`kernel_o_max_bytes=81440`、`first_iter_us=3.58` | 同上，冷启动比 1.178 | **`ICACHE_TEXT_BLOAT`（.text 70 676 B，redline）** | 未评估（无时限） |

结论：

- **7 条里 5 条有体检记录**，2 条（第 1、2 条）本地只剩 `exit_code/sha/finished_at`，写「缺」。
- **4 条会被 elf_guard 的中性阈值标出来**（编译 redline 2 条、`.text` 超 I-Cache redline 2 条）；它们都是线上 TLE，方向一致。
- **D3 的时限风险 7 条全部「未评估」**：P22 规则快照两场都没发布 `time_limit`（P22 报告 §2.1 已注明），`config.json` 也没有 `judge.time_limit_sec`。这是子计划的回落链设计（默认空即跳过），不是漏判。若配置了时限，公式只用编译项（ops-bench 无 case map，`receipt_predict` 预测不出逐例时间），即 `required = compile_sec × 1.0`：第 4/5 条 228.79 s / 218.55 s，第 3/6/7 条 40.16 / 18.45 / 44.62 s，可与部署里配的时限直接比。
- 模板实例化：R3 的 203 次运行确认（905 份 `result.json` 里 203 份有该字段）；这 7 条 TLE 里只有第 6、7 条记录了它（30、3），其余写缺。

### 8.5 工具表字节

| 角色 | `main`（e2ebd00） | 本包 |
|---|---:|---:|
| 主控 | 14 867 | **14 867**（不变） |
| worker | 12 522 | **12 522**（不变） |

本包**没有新增工具参数**（体检查询走已有回复）：`mcp/schemas.py` 与 `mcp/tools.py` 均未改，`TOOL_TABLE_BUDGET` 未动，也没有压缩任何既有描述（没有可报告的字节变化）。主控 14 867 / 15 000 与 worker 12 522 / 13 000 都在上限内。

## 9. 真机结果

- 设备可达（`ssh -o BatchMode=yes` 一次通过，只测这一道题）。
- **没有写真实比赛目录**：把 mat-chem 的 `RevINNorm`（含 `.git`）复制到 `/tmp/p24-verify/devws/`，用临时 `PARHELION_HOME=/tmp/p24-verify/devhome` 跑：`opsb test RevINNorm --workspace /tmp/p24-verify/devws --level standard`，exit 0，15/15 PASS，`formal=true`，`level=standard`，run `8a0857c2…`。
- **新字段确实落盘**：
  - `bench-runs/<run>/report.json`：
    `"build_metrics": [{"tree": "repo", "candidate": true, "compile_sec": 15.307, "template_instantiations": 1, "template_source": "source_scan", "kernel_o_count": 1, "kernel_o_sum_bytes": 128928, "kernel_o_max_bytes": 128928}]`；
  - `results/RevINNorm/<sha>/result.json` 多出 `phase_a_cpu_sec: 15.307`、`template_instantiations: 1`、`kernel_o_count: 1`、`kernel_o_sum_bytes: 128928`、`kernel_o_max_bytes: 128928`（原有 `layout/scheme/verified` 不变）。
- 在该副本上跑 `health.check` / `evaluate_safety`：`checked=true`、来源 `bench-run`、指标如上；告警 `ELF_OBJECT_WARN`（128 928 B ≥ 98 304 B 中性 warning 线），状态 `warn`；时限风险因无时限跳过；S1 判定 `PASS`。
- 全程只测不提交：没有向判题站提交任何东西，没有派发或停止真实 worker，没有入队。

## 10. NOTE

1. **真实回放里 `unreproduced` 一次都不成立。** ops-bench 两场的平台工作区没有任何 `wa-repro.json`；mat-chem 只有 `QuantScaleCalib` 的 3 条 WA 回执有变体（各 15 个），但坏 SHA `9ffca10…` 的 4 条正式运行全是 `level=standard`（不含 final 级变体），平台在赛后没再跑 final。所以按判定口径它们都停在 `pending`，`frozen_shots` 与阶段码分布在两场都**不变**；出口的可执行性用 §8.1 的合成副本证明（真实回执 + 真实变体 + 两条补记的 final 运行）。
2. **「正式运行」= `report["formal"] is True`**，参考 P21 的回放与 `formal_evidence` 的用法；`level` 不写的老报告按 `None` 视作可接受（这些报告本来也不会含 4.1 的变体行，实际影响为 0）。
3. **会话复用的要求只在记录能区分时生效**：三条 final 运行都没有 `settings.reuse_session` 时，两次全过即判未复现。这是子计划「如果能区分」的字面实现。
4. **S1 逃逸锁的拒稿文案变了。** 老的非参考分支会提示 `verify-escape --bad-sha … --good-sha …`；现在统一走 `wa_repro.next_step`：有变体时是「awaiting reproduction / reproduced, awaiting a fix」，没有变体时是「reproduce the failure locally or request an exact-SHA reviewed trial」。ops-bench 回放的「被拦时下一步非空」仍是 6/50，没有回退。
5. **体检只在 `_evaluate_impl` 里接**，`evaluate_safety` 直接调用时 S1 指标里没有 `health`（`parhelion_submit_gate`/队列卡片都走 `_evaluate_impl`）。这是为了让体检需要「在榜 SHA」，而它来自 ledger。
6. **`elf_guard` 只加了一个入参**（`build_metrics=`），阈值与告警逻辑一行未改；所以体检的告警口径与 `opsb tools elf` 完全一致，不引入第二套阈值。
7. **`judge_rules` 的两个读取器是追加的**（`time_limit_sec`、`iterations`）：快照没有时限时返回 None；`judge_compile_factor` 的合法区间是 0.1–4.0（共享的 `set_switch` 会把「份额类」浮点夹到 0–1，这个开关单独处理）。
8. **优先级惩罚可叠加**：S1 未复现放行 ×0.5、熔断建议 ×0.5、体检告警 ×0.8；三者同时成立是 0.2×。这是保守选择（都只是排序，不拦），已在此说明。
9. **账本的 `judge_noise` 是平台专有字段**（worker 写不进），`measurement` 类型；eKey 是 `judge-noise:<sid>:<kind>`，所以同一回执的重放不会重复，且「翻转」与「未复现」两类互不覆盖。
10. **体检的时限风险对 probe 不生效**：开局 probe 走 `probe_allowance` 分支、根本不进 S1（与本包之前的 4.1 口径一致）；「exploit 暂缓」在本 base 等价于「所有经过 S1 的请求」。
11. **mat-chem 的 `result.json` 大多没有健康字段**（31/203 有编译秒数，因为 case-bench 的旧 `result.json` 只存摘要）。本包之后新跑的正式运行会带上；历史记录按「缺就跳过」处理，不补跑。
12. 回放与核对都在副本上做；本包在真实 `~/.parhelion/workspaces` 下没有留下任何新目录（所有写入都在 `/tmp/p24-verify/**`、`/tmp/p24h`）。`main` 上 12:24–12:30 出现的 `ws-*` / `opsbench-*` 目录不是本包造成的（本包开工晚于它们，且全程用临时 `PARHELION_HOME`）。

## 11. 风险与后续

- **出口只到 S1。** §8.2 的反事实显示 4 发会放行、`dcc7754` 仍被 S2 的 `BELOW_BEST_IN_RESTORE` 拦。要让「未复现」真正变成可发的一发，需要 P23/P29 的 S2 定价（按 σ̂ 而不是固定 2.5 分）。本包不做。
- **依赖已有变体。** 没有 `wa-repro` 清单的旧回执永远到不了 `unreproduced`（ops-bench 就是这一类）。可行的补救是给历史坏 SHA 手工补规划与 final 运行，但那是证据工程，不是门禁改动。
- **样本只有 2 次运行。** `unrepro_runs=2` 是子计划选的默认值；两次 final 运行仍可能是巧合（尤其每题变体多、失败依赖输入时）。会话复用条件把「只在 warm 会话里通过」这一种排除了一半。
- **D3 在时限未配置前是空转。** 需要有人把判题时限写进部署配置（`judge.time_limit_sec` 或等判题接口发布 `time_limit`）；在此之前 7 条历史 TLE 只能靠 elf_guard 的编译/I-Cache 中性阈值提示。
- **预测项依赖 case map。** 没有逐例映射的题（ops-bench 全库）D3 退化为编译项，会低估「编译不慢但判题跑得久」的风险；下一场有 case map 后才会用上 `receipt_predict`。
- **体检多花的时间**（回放里约 13–25 ms/发，随负载波动）在门禁常驻进程里可忽略；但 `receipt_predict` 的 Simplex-MAP 标定在赛程后段单发 1–2 s，只有配置了判题时限才会触发，若嫌慢可以给预测加缓存（本包未做）。
- **P23 合入后要改一句话**：`JUDGE_TIME_RISK` 的 next_step 目前是「或请求主控决定」，P23 有 `intent` 后应改成「或用 intent=probe 发」。
- **P23 与 `gate.py` 的冲突面**：本包只动 S1（`evaluate_safety` 的逃逸分支、`_apply_health`、优先级尾部），S2/S3 的判定逻辑一行未改；P23 若后合，冲突应集中在 `_evaluate_impl` 的 S2 段与优先级尾部（EXPLORE 分支）。

## 12. 待决

1. **要不要把判题时限写进部署配置。** D3 的判据已经实现且可开关，但两场都没有时限数据；需要把判题站的真实时限记进 `config.json` 的 `judge.time_limit_sec`（或在规则接口发布后由 P22 快照自动带上），否则这条 4.3 唯一的暂缓理由一直空转。
2. **`unrepro_runs` 是否要能通过 CLI 设置。** 现在是 `GateConfig` 字段（默认 2），但 `opsb queue config --gate` 的浮点开关会被夹到 0–1，整数开关没有通道，只能改桥接状态文件。要放开就得给 `set_switch` 加一种整数类型。
3. **rules 视图的翻转次数要不要进全表。** 目前只给单题 `flips`（P22 的全表列与 2 KB 预算只剩约 300 B）。
4. **`dcc7754` 这类「S1 放行、S2 仍拦」的发**，P23/P29 是否按 σ̂ 定价后放行，需要那两包的回放数字来定；本包只提供出口。
5. **合成验证用的两条 final 运行只存在于 `/tmp` 副本**，没有写回任何比赛目录。如果主会话希望在正式数据里也留一份「未复现成立」的证据，需要在下一场用平台命令（`opsb test --level final`）在坏 SHA 上重跑，本包不做。
6. **`main` 已前进到 `e681ed3`**（P21 回放入口修复：`cli/tests/test_replay_gate.py::test_real_entry_point_runs_without_pythonpath` 与 aoci 索引）。硬约束不允许 rebase，所以本包基线是 `e2ebd00`；两者在源码上只差这一条测试，与 P24 改动无交集，合入时按 10-DSH §1「谁后合入谁 rebase」处理即可。本报告的两次回放用本分支的 `cli/opsb replay gate`（带 `PYTHONPATH=cli:.`）跑，main 的回放入口修复只改启动方式，不影响回放判定。
