# P21 门禁回放基准与 4.3 指标

分支：`impl/P21-gate-replay`。基于 4.2.1 的 `main`。无依赖。

## 为什么

4.3 的门禁包（P23、P24、P25、P29）都要回答同一个问题：**按上一场的真实回执逐发重放，新门禁会拦掉哪些发、放过哪些发。**

R5 的 13/231 是手算的。00-SUMMARY 用原始峰值复算得到 18/231。两个口径不同，也都不能重复运行。没有一把固定的尺，审查时就无法比较改前改后。

上一场的教训是「测量台是第一生产力」。这个包就是平台自己的测量台。

## 范围

1. **新模块** `cli/opsb_core/replay/gate_replay.py`，新命令 `opsb replay gate`。在 `cli/opsb_core/commands/replay_cmd.py` 注册，解析器在 `cli_app.py`。参数：

   | 参数 | 说明 |
   |---|---|
   | `--workspace <dir>` | 比赛目录，必填 |
   | `--board-mode best\|latest` | 没有规则快照时，全部题目按此榜型 |
   | `--gate NAME=VALUE` | 可重复，含义与 `opsb queue config --gate` 相同，只作用于本次回放 |
   | `--since` / `--until` | 时间窗 |
   | `--json` | 输出 JSON |

2. **只读。** 回放前把工作区的 `results/` 与清单复制到临时目录（`tempfile`），在副本上构造 `SubmissionGate`。门禁会写这些文件：`feedback-log.jsonl`、候选档案、`.case-map.json`、残差缓存等，都只能落在副本里。结束后删除副本。
   - 测试要断言原工作区所有文件的 mtime 与大小不变。
   - ops-bench 没有 `opsb.workspace.json`。用它比赛目录下题目仓库旁的 `problems.json` 在副本里生成最小清单，或者给 `--manifest` 参数。具体由你按 `scheduler/parhelion_paths.load_problem_manifest` 的规则决定。

3. **逐发重放：**
   - 回执取 `scheduler/receipts.canonical`，按 `submission_id` 去重，按 `submitted_at` 排序。
   - 对每一发，用 `SubmissionGate._evaluate_impl(title, sha, as_of=<submitted_at>, override_board_score=…, override_board_status=…)` 求判定。榜面取该时刻之前最后一条回执的在榜分：latest 是上一发的结果，best 是历史最高。
   - 所有记录都过一遍：被拦下的那一发在真实历史里仍然发生了。这样回放的是「同一段历史，每一发当时会怎么判」。
   - 报告注明：这是逐发判定，不是反事实模拟。被拦之后历史不会照原样发生。

4. **每场输出：**
   - **门禁**
     - 每阶段的拦截数，按阶段码分列；
     - `blocked_new_highs`：被拦下、且当时刷新了该题历史最高的发；
     - `blocked_restores`：被拦下、且是线上 Pass 过的 SHA 的发；
     - `frozen_shots`：判题侧失败之后，该题连续被 S1 拦下的发数；
     - `restore_approved / zero_events`：latest 下榜面归零之后，下一发为 restore 时的批准比例；
     - 每一发被拦时，门禁给出的下一步是否非空。
   - **冲刺结果**（只依赖回执）
     - 新高率；
     - 与峰值持平的比例；
     - 退步率；
     - 硬失败率（TLE/WA/RE）；
     - 分/判：总涨幅除以判题数；
     - 同源重投占比；
     - 归零后恢复的中位分钟数；
     - 收官差额：每题历史最高减去收盘在榜分的总和。
   - **可信度标注**：S1 的逃逸锁与 TLE、S3、榜型与 restore 只依赖回执，标为「可信」。S2 依赖本地证据，标为「近似」，并报告有可配对证据的发数占比。

5. **开关对比：**
   - 每场至少跑两组：当前默认（4.2.1）；4.2.0 行为（`board_mode_from_rules=false`、`tle_arms_escape_lock=true`、`circuit_blocks=true`、`noise_floor_from_judge_sigma=false`、`approve_low_gain_positive=false`）。
   - 4.2.0 组用于和 R5 的 13/231 对照。对不上的地方要解释。

## 复用

- `scheduler/gate.py` 的 `_evaluate_impl` 已支持 `as_of` 与榜面覆盖；`scheduler/estimator.py` 的 as_of 重放已用于残差。
- `cli/opsb_core/replay/cost.py` 的「被保留改进」口径，`cli/opsb_core/replay/sources.py` 的只读访问。
- P07 的做法：在比赛结果副本上只读核对（`docs/plan-4.1-4.2/reports/P07-queue-check.md`）。

## 测试

- 合成回执：三题、十余发，覆盖 latest 归零后 restore、TLE、WA、六发无增益。断言各计数。
- 只读断言：原工作区 mtime 与大小不变。
- `--gate` 只作用于本次回放，不写入 `config.json` 与桥接状态。

## 验收

- 在 `~/Competitions/ops-bench` 与 `/Users/winbeau/Competitions/mat-chem-sim-pred` 上各跑两组开关，结果写进报告，作为 4.3 的基线表（00-PLAN §2 的 G1–G5、G7）。
- 单场回放的耗时写进报告。目标是几分钟内跑完，后续每个门禁包都要跑。

## 真机

不需要。

## 报告（`docs/v4.3/reports/P21-gate-replay.md`）

- 按 [10-DSH.md](10-DSH.md) §1 的格式；
- 两场 × 两组开关的表；
- 与 R5 13/231 的对照与差异解释；
- 回放的已知偏差；
- 命令的完整用法。
