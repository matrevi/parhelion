状态：已交付

# P23 提交意图：利用、试探与回滚

`intent=exploit|probe|restore` 进入门禁、队列行、回执行、账本与回放：probe 要有 undo、有每题预算、不设噪声底线；restore 按该 SHA 自己的 Pass 回执定价，榜面归零或上一发判题失败时无条件放行；开局豁免延长到首次出分（上限 8 发）；force/override 回执退出校准样本；S1/S2/S3 的每一种暂缓都带下一步。子计划 §1–§7 与新增 §6a 全部实现。

## 1. 分支与提交

分支 `impl/P23-submit-intents`，基于最新 `main`（`e681ed3`，含 P21、P22、P26 与 `f67f085` 回放入口修复）。

```text
$ git log --oneline main..HEAD
<deliver> Deliver P23: submit intents with an undo, a budget and a priced rollback
00acb57 Cover the opsb submit --intent path through the real CLI command
3ee51e4 Tell workers how to probe and how to roll back
853521b Replay the 4.3 intents to measure G4, probes and the opening window
06f2694 Carry the submit intent through the bridge, the CLI and the tool table
282b6fe Keep forced and overridden receipts out of the calibration samples
b11305f Add exploit, probe and restore submit intents to the gate
```

第一条是带着本报告的那个提交（提交号由提交动作本身决定，无法写进自己的内容里，所以留 `<deliver>`）；后六条的实现提交号准确。

| 提交 | 内容 | 文件 |
|---|---|---|
| `b11305f` | 意图判定、开关、开局窗口、§6a 下一步 | `scheduler/submit_intent.py`（新，336 行）、`scheduler/intent_pricing.py`（新，177 行）、`scheduler/gate.py`（+222）、`scheduler/tests/test_submit_intents.py`（新，571 行）、`scheduler/tests/test_probe_allowance.py` |
| `282b6fe` | force/override 回执退出校准样本 | `scheduler/estimator.py`、`scheduler/case_calibration.py` |
| `06f2694` | 桥、CLI、工具表、队列卡、账本 | `scheduler/cli_bridge.py`、`cli/opsb_core/cli_app.py`、`commands/queue_cmd.py`、`mcp/schemas.py`、`mcp/handlers_bench.py`、`services/gate_view.py`、`ipc_bridge.py`、`ledger/{auto,rows}.py`、`cli/tests/test_submit_intent_wiring.py`（新，120 行） |
| `853521b` | 回放意图块与 §6a 验收 | `replay/gate_replay.py`、`replay/gate_metrics.py`、`replay/intent_replay.py`（新，54 行）、`commands/replay_cmd.py`、`cli/tests/test_replay_gate.py` |
| `3ee51e4` | worker 契约与主控 brief | `cli/opsb_core/drivers/brief.py`、`cli/tests/test_dsh_and_subagent_launch.py` |
| `00acb57` | `opsb submit --intent` 端到端回归 | `cli/tests/test_submit_intent_wiring.py` |

两个新模块 336 与 177 行、新回放模块 54 行，均 ≤350。`gate.py` 只加接入点：一个开关块、一个 `with_next_step` 表、`probe_allowance` 的窗口规则、`_evaluate_impl` 里的意图分支；S1 的判定顺序与 `evaluate_safety` 的函数体没有重排（P24 的 S1 改动不受影响）。

## 2. 子计划逐项完成情况

### 2.1 意图参数 — 已完成

- `parhelion_submit_gate` 新增 `intent`（枚举 `exploit|probe|restore`，缺省 `exploit`），三处登记：`mcp/schemas.py`、`mcp/handlers_bench.py`、角色（该工具原本就对 orchestrator 与 worker 开放，未收紧）；参数名两侧逐字一致，参数无空 description。
- `opsb submit --intent`（`cli/opsb_core/cli_app.py` + `commands/queue_cmd.py`），并透传给桥的 `evaluate`。
- 意图写进队列行（`submit-queue.jsonl` 的 `intent`，仅在非 exploit 时写；请求与生效不一致时另写 `intent_requested`）与回执行（verdict 的 `intent`/`requested_intent`/`intent_code`/`intent_reason`/`undo`）。账本在生成的假设里记 `(<intent> intent)`；回放按回执行/队列行读回意图（`submit_intent.receipt_intent`，先按 `queued_at`、再按 SHA 回退）。
- 粘性：派发路径重新评估一个排队 SHA 时不带显式意图，`resolve_intent` 会认这条 pending 队列行的意图，所以 worker 与守护进程看到同一个判定。已判过的行不再粘（同一 SHA 之后可以自由选意图）。

### 2.2 probe 规则 — 已完成

- **S1 照常**：probe 不跳过 S1，逃逸锁与 TLE 原样重投照样拒绝（`test_probe_…` 与 `test_an_armed_escape_lock_still_stops_an_opening_probe` 覆盖）。
- **S2 不用噪声底线**：只问 P(gain>0) ≥ `probe_min_prob`（默认 0.3）；估计未知（`anchor_fallback` 或「no perf telemetry」）也放行，回复与阶段理由标「estimate unknown」。
- **undo**：best 榜天然成立（`kind=best_board_keeps_its_score`）；latest 下要求存在「线上 Pass 过、且分数不低于当前榜面」的 SHA（取前 3 个作为回滚目标），或榜面为 0/未出分/unknown（`kind=board_open`）。没有 undo 时按 exploit 评估，`intent_code=PROBE_NO_UNDO` 并说明。
- **预算**：`probe_window_hours`（默认 12）内「已判 + 在途」的 probe 合计 < `probe_per_window`（默认 2）。已判按回执的 submitted_at、在途按 pending 队列行的 queued_at；正在被判的这一发不消耗自己的预算。超出按 exploit 评估（`PROBE_BUDGET_SPENT`），不拒绝。
- **放行后**：`status_tag=PROBE`、S2 阶段 `PROBE_INTENT`，优先级 `权重 × E[max(0,gain)] × 0.5`；`ipc_bridge` 的队列排序把 PROBE 放在 READY 的 exploit 之后（CRITICAL_RESTORE 仍最前）；回复的 `next_step` 给出失败后的 restore 命令（`opsb submit <题> <回滚 SHA> --intent restore --note "undo a failed probe"`）。

### 2.3 restore 规则 — 已完成

- **前提**：SHA 必须在本题有线上 Pass 回执（score>0），否则按 exploit 评估（`RESTORE_UNPRICED`）。
- **定价**：该 SHA 历次 Pass 的平均分，每次按 `ScoreEstimator.discount_draw_to_contemporary` 折到当前（gamma 与 anchor 取 `ProblemLedger`，anchor 为该题在榜判分的时刻，无榜面时取 now）；σ 取 `scheduler.judge_noise.sigma_for`（`problem` 或 `contest`，自由度不足时取不到），取不到用常数 `DEFAULT_SIGMA = 2.70`（4.2.1 的 `SIGMA_HIDDEN_JITTER`）。回执的 `undo.pricing` 给出每笔折算明细。
- **latest**：增益 = 预期分 − 当前榜面。≥ 底线 `READY`；0<增益<底线且 P(gain>0)≥0.5 低优先级放行（`EXPLORE`）；否则暂缓。**榜面为 0/未出分/unknown，或上一发是 WA/RE/TLE/CE/ML/SE/Fail 时一律放行**，并把 `is_critical_restore` 置真（这是对 4.2.1「确认归零」的放宽，`status_tag=CRITICAL_RESTORE`，优先 1000+）。
- **best**：restore 不在榜的 SHA 暂缓并说明「the best board keeps its best score」（`RESTORE_NOT_ON_BEST_BOARD`）；SHA 就是榜上最好分时按 4.2.1 的 best 口径评估（`RESTORE_ON_BEST_BOARD`，回落 exploit）。
- **重投已在榜的 SHA**：exploit 下仍 `ALREADY_ACTIVE_ON_BOARD` 暂缓；只有 `intent=probe` 放行并计入 probe 预算，回复的 `undo` 给出该 SHA 的历次折算与 σ̂。

### 2.4 开局豁免 — 已完成

`opening_until_first_score`（默认开，D1）：该题还没有任何有分的 Pass 时窗口持续，最多 `opening_max_probes`（默认 8）个判题受理的 submission_id；关掉回到 4.2.1 的 3 发（`probe_submissions_per_problem`，置 0 仍可整体关闭）。`probe_allowance` 的返回新增 `until_first_score` 与 `first_score_seen`。口径与现行一致：只认判题受理的 `submission_id`（回执行 + `submission-receipt.json`）。

**附带收紧**：开局窗口只替换「证据完整性」（远端编译过即可），不再顺带绕过锁与身份类拒绝——逃逸锁、TLE 原样重投、未解析 SHA、git 树不符、result.json 损坏、判题工具链编译失败都在窗口内照样拒绝（`OPENING_HARD_CODES`）。`SHA_MISMATCH` 没有进这张表：4.2.1 允许 `result.json` 不带 sha 的记录走开局窗口，收紧会改动两处既有夹具的语义（见 §8）。

### 2.5 校准样本 — 已完成

- 估分残差（`estimator._receipt_residuals`，即 `.case-bench-residuals.json`）：`force` 或 override 的回执不再进入样本；每条样本新增 `intent` 标记，probe 照常进、标 `probe`。
- 逐例校准表（`scheduler/case_calibration.py`）：`refresh` 先剔除 forced 观测，再算配对记录；记录里新增 `intent` 字段。
- 识别方式：回执行自带的 `force`；reconciler 重建过、丢了标志的行，用产生它的队列行回退（先按 `queued_at` 精确匹配，再按 SHA 且要求该 SHA 的队列行全部 forced）。
- 真实数据规模：ops-bench 211 条有分 Pass（40 位 SHA）里 145 条带 force，mat-chem 50 条里 22 条——这一项把两场的校准样本分别减到 66 与 28 条。

### 2.6 契约与显示 — 已完成

- worker 契约（`compose_session_brief`，仍 1.5 KB）：新增一行 `Speculative shot: parhelion_submit_gate intent=probe; rollback: intent=restore.`，字节从「squad 段」挤出，末行的数据通道说明完整保留（见 §6.1）。
- 主控 brief（`_clean_sprint_brief`）的同一段：`Per problem, distinct judge submission IDs are opening probes until the first scored Pass, at most 8, even after an early AC`，并把 `From attempt four` 改成 `After that`，句末加 probe/restore 用法；`Never use --force, --now or a direct uploader` 原样保留。
- `gate_view`：`INTENT_BADGES = {PROBE_INTENT: PROBE, RESTORE_INTENT: RESTORE}`，`badge()` 在「已放行且非 CRITICAL_RESTORE」时给意图徽标；`next_step()` 先读 S1 的 `next_step`，没有则读 S2 的（probe 的回滚命令、restore 的定价说明）。
- Swift：未改，也不新增解码字段。`badge` 在 Swift 侧是 `String`，未知值走默认样式（`readableCandidateBadge` 落到默认标签，`gateSummaryColor` 默认绿色），`cli_bridge.py` 的字段集合未改。

### 2.6a 每一发被拦都给下一步 — 已完成（P21 审查后新增）

- `StageResult` 的补齐集中在 `gate.with_next_step`：`_evaluate_impl` 组装 `stages` 时对四个阶段各过一遍，**失败**且 `metrics` 里没有 `next_step` 的按阶段码查表补上，`EXIT_CODE_*` 用专门文案，查不到的表外用「read the stage reason and address it…」兜底；S1 原有的逃逸锁/TLE 上下文文案保持不动（被覆盖前先判空）。
- 覆盖的阶段码（每一条的文案在 `STAGE_NEXT_STEPS`）：
  - **S1**：`CORRUPT_RESULT_JSON`、`UNRESOLVED_SHA`、`SHA_MISMATCH`、`GIT_TREE_MISMATCH`、`MISSING_LOG`、`REFERENCE_SUITE_REQUIRED`、`FAST_LANE_ONLY`、`MISSING_EXTENDED_PASSED`、`NONZERO_MISMATCH`、`GATE_BAD_LANES`、`NON_DETERMINISTIC_GATE`、`STRESS_FAILURES`、`MISSING_CROSS_CORE_STRESS`、`JUDGE_COMPILE_FAILED`、`EXIT_CODE_*`；`ESCAPE_LOCK_ACTIVE`/`TLE_SHA_REPEAT` 沿用原上下文文案。
  - **S2**：`OPERATOR_SKIPPED`、`ALREADY_ACTIVE_ON_BOARD`、`BELOW_NOISE_FLOOR`、`BELOW_BEST_IN_RESTORE`、`NEGATIVE_EXPECTED_RESTORE`、`RESTORE_INTENT`、`PROBE_NO_UNDO`、`PROBE_BUDGET_SPENT`、`PROBE_NO_ODDS`。
  - **S3**：`CIRCUIT_OPEN_SWITCH_BASELINE`、`HALF_OPEN_UNPROVEN_SHA`。
  - **S4**（不拦批准，但卡片同样给下一步）：`DAILY_LIMIT_REACHED`。
- 文案按情形写：「低于底线」→「add a paired measurement that clears the floor, or send this SHA as intent=probe」；熔断建议 →「try an orthogonal mechanism direction (six non-improving shots in the window)」；已在榜 →「change it, or re-roll with intent=probe」；restore 暂缓 →「restore a proven SHA whose priced gain clears the floor, or wait for an open board」。
- 回放复测（§5.4）：ops-bench 4.2.1 组 `blocked_with_next_step` 从 P21 的 **6/50（12%）** 变成 **50/50（100%）**；4.2.0 组 132/132；mat-chem 66/66 与 69/69。

### 2.7 开关 — 已完成

`GateConfig` 新增并登记进 `GATE_SWITCHES`：`submit_intents`(bool)、`opening_until_first_score`(bool)、`opening_max_probes`(int)、`probe_per_window`(int)、`probe_window_hours`(float)、`probe_min_prob`(float)。`set_switch` 支持 int，并按 `GATE_SWITCH_BOUNDS` 分段收敛（窗口 0–168 h，上限 0–64 次；概率仍 0–1；非法整数/浮点报错而不是静默）。每个开关单独回 4.2.1：`submit_intents=false` 把所有意图按 exploit 判；`opening_until_first_score=false` 回到 3 发；`probe_per_window=0` 关掉 probe。测试逐个覆盖。

### 2.8 测试 — 已完成

新增 43 条（`scheduler/tests/test_submit_intents.py` 35、`cli/tests/test_submit_intent_wiring.py` 6、`cli/tests/test_replay_gate.py` +2），覆盖子计划「测试」节全部条目：三种意图在 best 与 latest 下的判定、probe 无 undo 回落 exploit、预算用尽/滚动/归零回落 exploit、restore 定价与折算（含每笔 drift）、重投在榜 SHA 的暂缓与 probe 放行、开局豁免持续到首次有分与上限、force 回执被排除出残差（probe 保留并打标）、六个开关逐个回退、§6a 全部阶段码与真实被拦判定、以及 `intent` 从 CLI/桥/MCP/schema 到队列行的端到端。

## 3. 判定表（意图 × 榜型 × 情形）

| 意图 | 榜型 | 情形 | 判定 | status_tag | S2 阶段码 |
|---|---|---|---|---|---|
| exploit | 任意 | 任意（缺省） | 完全走 4.2.1 的 S1→S2→S3→S4 | 4.2.1 原值 | 4.2.1 原值 |
| probe | best | 预算内，P(gain>0)≥`probe_min_prob` | 放行 | `PROBE` | `PROBE_INTENT` |
| probe | best | 预算内，估计未知 | 放行并标「estimate unknown」 | `PROBE` | `PROBE_INTENT` |
| probe | latest | 有 undo（线上 Pass 过且分数≥榜面）＋P≥min＋预算内 | 放行 | `PROBE` | `PROBE_INTENT` |
| probe | latest | 榜面 0/未出分/unknown（按 undo 存在处理） | 放行 | `PROBE` | `PROBE_INTENT` |
| probe | latest | 无 undo | 按 exploit 评估 | exploit 的标签 | `PROBE_NO_UNDO` |
| probe | 任意 | P(gain>0)<`probe_min_prob` | 按 exploit 评估 | exploit 的标签 | `PROBE_NO_ODDS` |
| probe | 任意 | 窗口内已判+在途 ≥ `probe_per_window` | 按 exploit 评估 | exploit 的标签 | `PROBE_BUDGET_SPENT` |
| probe | 任意 | 重投已在榜 SHA | exploit 下 `ALREADY_ACTIVE_ON_BOARD`；probe 放行并计预算 | `PROBE` | `PROBE_INTENT` |
| probe | 任意 | S1 不过（逃逸锁、TLE 原样重投、身份/编译） | 拒绝，probe 不绕过 S1 | `REJECTED_GATE` | S1 的 code |
| restore | latest | SHA 有线上 Pass，榜面 0/未出分 | 放行（`is_critical_restore` 放宽） | `CRITICAL_RESTORE` | `RESTORE_INTENT` |
| restore | latest | 上一发 WA/RE/TLE/CE/ML/SE/Fail | 放行 | `CRITICAL_RESTORE` | `RESTORE_INTENT` |
| restore | latest | 定价增益 ≥ 底线 | 放行 | `READY` | `RESTORE_INTENT` |
| restore | latest | 0<增益<底线 且 P(gain>0)≥0.5 | 低优先级放行 | `EXPLORE` | `RESTORE_INTENT` |
| restore | latest | 增益 ≤0 或 P<0.5 | 暂缓 | `HOLD_LOW_GAIN` | `RESTORE_INTENT` |
| restore | best | SHA 在榜（历史最高/在榜 SHA） | 按 4.2.1 best 口径评估（回落 exploit） | 4.2.1 原值 | `RESTORE_ON_BEST_BOARD` |
| restore | best | SHA 不在榜 | 暂缓：「best 榜面保留最好分」 | `HOLD_LOW_GAIN` | `RESTORE_NOT_ON_BEST_BOARD` |
| restore | 任意 | SHA 无线上 Pass 回执 | 按 exploit 评估 | exploit 的标签 | `RESTORE_UNPRICED` |
| 任意 | 任意 | `submit_intents=false` | 全部按 exploit | exploit 的标签 | `INTENTS_DISABLED` |
| probe / restore | 任意 | 落在开局窗口内 | 先走开局豁免（编译级证据），不消耗 probe 预算 | `PROBE` | `PROBE_ALLOWANCE` |
| 任意 | 任意 | `intent` 取值非法 | 调用错误（`ValueError`），不落队列 | — | — |

## 4. 测试

```text
$ ~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider
894 passed, 19 skipped, 2 subtests passed in 90.95s (0:01:30)

$ PYTHONPATH=cli:. ~/.parhelion/venv/bin/python -m pytest -q -p no:cacheprovider cli/tests runtime/tests
598 passed in 70.48s (0:01:10)
```

两条都跑到底，全绿。本分支 rebase 后的基线（`main`，含 P21/P22/P26 与回放入口修复）实测是 **851 passed / 19 skipped** 与 **590 passed**；本包新增 43 条（第一条命令里 43 条、第二条命令里 8 条），分别变成 894 与 598。`runtime/tests/test_device_broker.py` 的两条预算测试本轮全绿，没有触发偶发失败，不需要单独重跑。

Swift：没有 Swift 改动（`Sources/` 无 diff），**不需要 `swift build`**；未知 badge 的默认样式已按 §2.6 核对。

## 5. 真实数据验收（`opsb replay gate`）

命令（`main` 的 `f67f085` 之后可从正式入口跑，不再需要 `PYTHONPATH`）：

```bash
cli/opsb replay gate -w ~/Competitions/ops-bench --intents --json
cli/opsb replay gate -w ~/Competitions/mat-chem-sim-pred --intents --json
cli/opsb replay gate -w ~/Competitions/mat-chem-sim-pred --intents --board-mode latest --json
```

### 5.1 门禁与 §6a

| 指标 | ops-bench 4.2.1（latest） | ops-bench 4.2.0（best） | mat-chem 4.2.1（best） | mat-chem（latest） |
|---|---:|---:|---:|---:|
| 重放发数 | 233 | 233 | 78 | 78 |
| 被拦 | 50（21.5%） | 132（56.6%） | 66（84.6%） | 64（82.0%） |
| — S1 | 9 | 14 | 42 | 42 |
| — S2 | 41 | 105 | 24 | 22 |
| — S3 | 0 | 13 | 0 | 0 |
| **blocked_with_next_step** | **50 / 50 = 100%** | **132 / 132 = 100%** | **66 / 66 = 100%** | **64 / 64 = 100%** |
| blocked_new_highs | 18 | 39 | 35 | 35 |
| 收官差额 | 10.19 | 0.00 | 0.00 | 37.64 |

ops-bench 4.2.1 组的拦截数、分阶段码、`blocked_new_highs`（18）、`frozen`（1）、收官差额（10.19）与 P21 报告**逐项一致**——本包没有移动 4.2.1 的放行口径（校准样本的排除没有翻动任何一发；见 §8）。唯一变化是 `blocked_with_next_step`：6/50 → 50/50。

### 5.2 G4（榜面归零或判题失败后下一发为 restore 的批准比例）

| 场 | 榜型 | 归零/失败后的 restore 事件 | 获批 | 比例 |
|---|---|---:|---:|---:|
| ops-bench | latest | 9（每一发都同时是「榜面开」与「上一发失败」） | 9 | **100%** |
| mat-chem | latest（`--board-mode latest`） | 1 | 1 | **100%** |
| mat-chem | best（默认） | 0 | — | 不适用 |

best 榜型下 G4 没有事件：best 榜面不会归零，「上一发失败」也不会降低榜面，所以按子计划 §3「best 下 restore 不在榜的 SHA 无意义」，这类发不算 G4 的分母。目标 100% 在有事件的两种口径下都达到。

### 5.3 probe 的假想批准

口径：把 4.2.1 默认组里**被 S2 暂缓的每一发**当作 `intent=probe` 重判（按回放顺序模拟滚动预算，只有本回放会批准的发才消耗预算）。增益/损失用该发**真实回执**对当时榜面的变化（`board_after − board_score`）求和，与 00-SUMMARY 的 +45.21 / −44.23 同量纲。

| 场 / 榜型 | 重判发数 | 预算内批准 | 其中当时刷新最高 | 增益之和 | 损失之和 | 净 |
|---|---:|---:|---:|---:|---:|---:|
| ops-bench latest（对照 00-SUMMARY） | 41 | **23** | **10** | **+32.81** | **−30.31** | **+2.50** |
| mat-chem best | 24 | 20 | 9 | +19.75 | 0.00 | +19.75 |
| mat-chem latest | 22 | 18 | 9 | +21.71 | −47.86 | −26.15 |

- ops-bench：23 发里 10 发当时刷新最高，净 **+2.50**，与 26 发 force 的净 +0.98（+45.21/−44.23）同向但更小——probe 用「预算 + undo + 概率门槛」换掉了 force 的一部分收益，也换掉了 16 发无谓回落的自由度（10 发新高 vs force 的 10 发新高，但损失从 −44.23 收到 −30.31）。
- 典型获批发（ops-bench，括号内为对榜面变化）：`ec0867d` LayerNormV3 +15.96（同源重判，当时新高）、`7efadf8` +4.63、`f2cee3b` +3.79、`2a29531` +2.05、`b15e2dd` +1.69；损失一侧最大的是 `734e097` −11.83、`4e1fd47` −4.05、`544439e` −3.73、`252b13e` −3.03。
- mat-chem latest 的 −47.86 集中在少数几发：这批回执的榜面在窗口内连续下滑，probe 在仍有 undo 的时点放行，随后被更强的对手分数甩开。这是「latest + 衰减榜面」的结构性风险，写进 §9/§10。

### 5.4 开局豁免

| 场 | 各题首次出分在第几发 | 豁免长度（`min(首次出分, 8)`） | 与 4.2.1 的三发相比 |
|---|---|---|---|
| ops-bench（10 题） | 7 题在第 1 发，3 题在第 2 发 | 1 或 2 | 完全相同 |
| mat-chem（14 题） | 10 题在第 1 发，2 题在第 2 发，4 题在第 3 发 | 1、2 或 3 | 完全相同 |

两场里**没有一题**在前 3 发都没出分：mat-chem 的 `EdgeIndexCoalesce`、`FFT3DBatchComplex`、`NeighborListPbcFixedK`、`SgemmFp16Cube` 都是「两发失败 + 第 3 发 Pass」，第 3 发就出分，所以 D1 的延长在两场上没有改变任何一次判定（它只在「第 4 发仍未出分」时才与 4.2.1 不同，而这两场没有这种题）。这条结论与 R6 §8.1「4 题前 3 发内没出分」不一致，见 §8 NOTE 3。

### 5.5 只读核对与耗时

- 回放在 `tempfile.mkdtemp` 的副本上做，`PARHELION_HOME` 指向副本内，结束删除。回放前后对两场目录做逐文件 `(mtime_ns, size)` 比对：**ops-bench 14449 个文件 0 增 0 改**；mat-chem **0 增**，但 `results/scoring-evidence/team-ranking.json` 与 `results/FFT3DBatchComplex/.candidate-archive.json` 有变化——这两个文件由**常驻守护进程的榜单刷新/状态构装**写入（`ranking_service`、`cli_bridge._candidate_archive`），不是回放：同一机器上再跑一次 6 秒窗口的回放，前后 0 文件变化。两场都没有新增目录。
- 耗时：ops-bench `--intents` 69.3 s（233 发 × 2 组 + 意图重判）；mat-chem 8.7 s；mat-chem `--board-mode latest` 6.1 s。

## 6. 契约与字节

### 6.1 worker 契约与主控 brief

| 项 | 改前 | 改后 | 上限 | 末行数据通道说明 |
|---|---:|---:|---:|---|
| worker 契约（`compose_session_brief` 的预算部分） | 1331 B | **1389 B** | 1500 B | 保留（未截断） |
| 主控 brief（`_clean_sprint_brief`） | 2689 B | **2841 B** | 2950 B | 不适用 |

挤字节的地方：worker 契约新增一行 79 B 的 probe/restore 用法（`Speculative shot: parhelion_submit_gate intent=probe; rollback: intent=restore.`），从 `parhelion_squad` 段省出 22 B（"parhelion_squad recipient: teammate agent_id…" → "parhelion_squad: teammate agent_id…"；"Send claim (<=280 chars)…a body above 1200 chars…" → "Claim <=280 chars…a body over 1200 chars…"），加上行分隔符共 +80 B，squad 段省 22 B，净 +58 B（1331 → 1389）。主控 brief 的同一段：开局口径 +23 B（"the first three distinct judge submission IDs" → "distinct judge submission IDs … until the first scored Pass, at most 8"），`From attempt four` → `After that` −7 B，probe/restore 用法 +136 B，合计 +152 B（2689 → 2841）。

### 6.2 工具表

| 角色 | `main` | 本包 | 目标 | 上限 |
|---|---:|---:|---:|---:|
| 主控 | 14867 B | **14560 B** | ≤14578 | 15000（未改） |
| worker | 12522 B | **12372 B** | — | 13000（未改） |

主控净省 **307 B**（含 `intent` 参数的 +125 B），给 P28 留 440 B。`TOOL_TABLE_BUDGET` 未改；所有枚举值、`required`、`minimum/maximum` 与参数名逐字未动。逐条压缩：

| 工具 | ΔB | 改法 |
|---|---:|---|
| `parhelion_submit_gate` | **+125** | 新增 `intent` 枚举参数；描述加「intent=probe: budgeted, undoable; intent=restore: roll back.」 |
| `parhelion_agent_peek` | −108 | "without shelling out; use it instead of cat/tail/ls/git diff --stat on its worktree" → "instead of shelling out"；"one path **relative to…**; … optional lines range" → "path relative to…; … optional lines"；"log_tail (last lines, credentials redacted)" → "log_tail (credentials redacted)"；"Answers are capped at 8 KB." → "Answers <=8 KB." |
| `parhelion_report` | −98 | "instead of ad-hoc scripts over report.json or submit-queue-outcome.jsonl" → ", not report.json"；"help (what each column means)" → "help (column meanings)"；"environment facts…or from platform internals" → "env…or platform internals"。视图清单与数据通道规则完整保留 |
| `parhelion_wait` | −32 | "return only the changes since" → "returns what changed since"；"test runs" → "tests"；"Prefer this over looping" → "Prefer this to looping"；"omit timeout and the wait lengthens itself" → "omit timeout and it lengthens itself" |
| `parhelion_test` | −27 | "The level decides how much device time the run costs." → "Level sets the device time it costs."；level 说明 "all cases plus warm timing; final: candidate validation" → "all cases + warm timing; final: candidate run" |
| `parhelion_status` | −24 | "per-problem rows (brief)" → "per-problem rows"；"to wait for a change use" → "to wait use" |
| `parhelion_note` | −21 | claim "one assertive sentence, at most 240 characters" → "one assertive sentence, <=240 chars"；problem "a worker's problem is bound" → "workers are bound" |
| `parhelion_escape_reproducer` | −16 | "The reproducer variants for a WA/RE receipt and the parhelion_test calls that confirm and verify a repair." → "Reproducer variants of a WA/RE receipt and the parhelion_test calls that confirm a repair." |
| `parhelion_kb_query` | −15 | "brief (default) summaries, format=full entries" → "brief (default) or full entries" |
| `parhelion_elf_cold3_check` | −14 | "ELF .o/.text/template instantiation redlines" → "ELF .o/.text/template redlines" |
| `parhelion_agent_wait` | −14 | "arrive. Prefer parhelion_wait, which also covers…" → "arrive; parhelion_wait also covers…" |
| `parhelion_escape_status` | −13 | "the reviewed-trial binding and the next step; a worker sees its own problem only" → "reviewed-trial binding and next step; a worker sees its own problem" |
| `parhelion_escape_rebind` | −12 | "Bind the active escape lock's one reviewed trial … only while" → "Bind the escape lock's one reviewed trial … while" |
| `parhelion_agent_spawn` | −11 | "non-blocking autonomous external Harness worker" → "non-blocking external Harness worker" |
| `parhelion_harness_pool` | −9 | "Primary orchestrator, external Harnesses, …" → "Primary orchestrator, Harnesses, …" |
| `parhelion_agent_messages` | −8 | "(per worker or Problem Squad), worktree git diff --stat, and commit." → "(worker or Problem Squad), worktree git diff --stat, commit." |
| `parhelion_sprint_start` | −5 | "preview only checks readiness and spawns nothing" → "preview checks readiness and spawns nothing" |
| `parhelion_ledger` | −2 | "one case, or since an id" → "a case, or since an id" |
| `parhelion_agent_stop` | −2 | "(-pgid), optionally removing its worktree" → "(-pgid); optionally remove its worktree" |
| `parhelion_bench_diagnose` | −1 | "calibration trust & ESCAPE_LOCK state" → "calibration trust, ESCAPE_LOCK state" |
| **合计** | **主控 −307 / worker −150** | 两者都含 `intent` 的 +125 |

`intent` 参数没有单独写 description（会再花约 45 B，逼近 14578），语义放在工具描述里的一句；枚举值本身已区分三种意图。

## 7. 改变了的既有测试清单

| 测试 | 改动 | 为什么是 4.3 有意改变 |
|---|---|---|
| `scheduler/tests/test_probe_allowance.py::test_rejected_answer_still_gets_three_opening_probes` | 夹具改成 `GateConfig(opening_until_first_score=False)` | D1 把默认窗口从「3 发」改为「直到首次出分，最多 8 发」；这条测试钉的是 4.2.1 的三发口径，改成显式关掉开关后来验证，语义不变 |
| `…::test_fourth_submission_requires_full_validation_even_when_unsolved` | 同上 | 同上（第 4 发在默认配置下现在是窗口内） |
| `…::test_counts_distinct_judge_ids_including_repeated_sha` | 同上 | 断言 `remaining == 1` 依赖三发窗口 |
| `cli/tests/test_dsh_and_subagent_launch.py::test_new_contest_gui_stages_english_brief_before_templates_arrive` | 关键句从 `first three distinct judge submission IDs` / `From attempt four` 改为 `opening probes until the first scored Pass, at most 8` / `After that, apply the safety, gain and circuit gates`，并新增 `intent=probe`、`intent=restore` 两条 | 主控 brief 的同一段按 D1 与本包意图改写 |

除上表外没有改动任何既有测试；`scheduler/tests/test_gate_421.py`、`test_gate_confidence.py`、`cli/tests/test_gate_queue_p07.py`、`test_v303_guardrails.py` 等门禁/队列测试全部原样通过。

## 8. NOTE

1. **意图的粘性必须靠队列行，不能靠调用参数。** 守护进程派发一个排队 SHA 时会用 `evaluate` 重新过一次门禁，那时没有 `intent` 参数；如果门禁不认队列行，一发 probe 会在上传前被按 exploit 挡住而永远留在 pending。所以 `resolve_intent` 的顺序是「显式非 exploit > pending 队列行的意图 > exploit」。代价：worker 对一个已排队的 probe SHA 显式请求 `exploit` 时也会被当成 probe（从参数上无法与守护进程的重新评估区分），这是有意的取舍。
2. **开局窗口的锁收紧。** 4.2.1 的前 3 发会连逃逸锁一起绕过（`_compile_ok` 命中就直接放行）；D1 把窗口拉长到 8 发后再这么做等于给「问题级锁」开了 8 次例外。本包把锁与身份类拒绝排除在窗口之外（`OPENING_HARD_CODES`），`SHA_MISMATCH` 特意留在窗口内，因为 4.2.1 允许 `result.json` 不带 sha 的记录拿到开局豁免（两处既有夹具就是这样），收紧会凭字节改动改写门禁语义。
3. **D1 的延长在两场真实数据上没有被触发。** ops-bench 10 题全部在第 1–2 发出分，mat-chem 14 题最晚第 3 发出分；R6 §8.1 的「4 题前 3 发内没出分」在回执里复现不出来（那 4 题都是「两发失败 + 第 3 发 Pass」，第 3 发就出分）。因此本包能给的证据只是「延长没有害处」，不是「延长有用」。
4. **校准样本的排除没有翻动任何一发真实判定。** ops-bench 4.2.1 组的拦截数、分阶段码、新高拦截与收官差额与 P21 逐项一致，说明这 145 条被排除的 forced 回执在估分残差里没有改变任何一次放行/暂缓。但样本量本身降了约 2/3（211→66、50→28），`calibration_trust` 的 `n_eff` 项会下降，长期看会让 S2 更保守——这与「试错更便宜」相反，建议 P29 一并复测（见 §10）。
5. **restore 的定价在 latest 下经常「没有增益」。** 定价用 `ProblemLedger` 的 `gamma`（榜面/在榜判分）与 anchor 折算，榜面已经下滑时 `gamma<1`，历史 Pass 会折算到接近当前榜面，增益接近 0。这不是 bug：4.2.1 的衰减模型就是说「同一发在今天只值这么多」。probe 的 undo 判定用的是**原始** Pass 分数（不低于当前榜面），两者口径不同，是有意的（undo 是「能不能回到不比现在差」，定价是「现在值多少」）。
6. **best 榜型下 G4 的分母天然为 0。** best 榜面不归零，失败也不会压低榜面，因此「失败后 restore」在 best 下不属于 G4（本包按此实现）。mat-chem 的默认榜型是 best（无规则快照），所以那场的主口径下 G4 是「不适用」，`latest` 那一行是补充口径。
7. **`scheduler/submit_intent.py` 与 `scheduler/intent_pricing.py` 拆成两个模块**，是因为合成一个会到 484 行、超过 350 行上限；拆分按「决策（意图、读取、回落）」与「历史定价（预算、undo、restore 定价）」的边界，两者单向依赖（定价模块在被调用时才回头导入读取函数，避免循环导入）。
8. **`STAGE_NEXT_STEPS` 里 S1 的文案与 P24 有关。** 本包只在 `_evaluate_impl` 组装阶段时补 `next_step`，不改 `evaluate_safety` 的判定与文案；`ESCAPE_LOCK_ACTIVE`/`TLE_SHA_REPEAT` 自带的上下文文案优先。P24 若改写 S1 的 code 集合，只需往这张表里补条目。

## 9. 风险与后续

- **风险：probe 在 latest 的真实回放下净负。** mat-chem latest 组 18 发获批、增益 +21.71、损失 −47.86、净 −26.15。undo 规则只要求「有一个分数不低于当前榜面的 SHA 可回滚」，而 latest 的榜面会继续下滑，回滚也只能回到「当时的榜面」。下一场若主打 latest，建议先用本命令复测，再决定是否把 undo 收紧成「反事实定价不低于榜面 + 底线」。
- **风险：probe 预算按「回放顺序」模拟，不是当天真实时间轴上的并发。** 真实冲刺里两发 probe 可能同小时发出、在途相互不可见（本包的计数包含在途队列行，可回放里没有在途行），所以回放的 probe 批准数是**上界**。
- **风险：`is_critical_restore` 的放宽会让一张坏榜面上的回滚插到队首。** 榜面为 0/未出分或上一发失败时，restore 直接 `CRITICAL_RESTORE`（优先级 1000+），比任何 READY exploit 都先派发。这是 4.2.1 的既有语义，但触发面变宽了；如果下一场出现「频繁失败 → 频繁 restore 插队」，需要 P25 的收官对账一起看。
- **后续：**（a）P25 的 `get_quota_status` 里 `restore_targets` 可以改用本包的 `intent_pricing.rollback_targets`，让「可回滚目标」与 restore 判定同源；（b）P29 可用 `--intents` 的 probe 批准集做排序质量的对照组；（c）`prob_positive` 的下界（`probe_min_prob`）目前只看估计值，P29 的 σ̂ 上线后应复测这个默认 0.3。
- **未验证：**`probe_min_prob`、`probe_per_window`、`probe_window_hours` 三个默认值只在回放上试过，没有真机/真实冲刺验证（子计划「真机」节写明不需要）。

## 10. 待决

1. **probe 在 latest 下的净负要不要处理。** 回放显示 mat-chem latest 组净 −26.15、ops-bench 净 +2.50。可选：(a) 保持子计划原样（undo 只看原始分），把结论留给下一场复测；(b) 把 undo 收紧为「反事实定价 ≥ 榜面 + 底线」或「只允许 probe 一次」。我按子计划原样实现，没有自加条件。
2. **校准样本排除后的 `calibration_trust` 是否需要补偿。** 145/211 与 22/50 的样本被排除，`n_eff` 下降会让 S2 更保守。要不要把 forced 回执以「更低权重」而不是「完全排除」的方式留在残差里（R5 §7 只要求它不参与校准，可以再讨论口径），需要主会话定。
3. **D1 的 8 发上限在两场上都没有被触发。** 若下一场仍未出现「第 4 发还没出分」的题，可以考虑把 `opening_max_probes` 直接设成更小的值，或把「豁免长度」写进 RUNBOOK 的复测项。
4. **`intent` 是否要单独的 schema description。** 现在没有（省 45 B，主控表 14560 / 14578）。如果审查认为模型需要逐参数说明，需要再挤 45 B 或调上限——按 10-DSH §2 调上限由主会话决定。
