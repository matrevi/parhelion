# P23 提交意图：利用、试探与回滚

分支：`impl/P23-submit-intents`。在 P21 合入后的 `main` 上开发，验收要用 `opsb replay gate`。

## 为什么

上一场的 270 条原始提交行里，171 条带 force。其中有 26 发是在门禁写着 `holding to …` 的情况下照发的：10 发创出新高，合计 +45.21；16 发回落，合计 −44.23。另有 43 发（16%）是 restore 或 rollback，大多当天就收回了回落（00-SUMMARY §4.2）。

结论是：**投机发有价值，前提是能撤回，并且有预算。** 反例是 11-23 那天，主控 13 次提交里有 11 次 force，当天是五天里产出最差的一天。

当前 `main` 的问题：
- **能用的只有 force。** 门禁里与之对应的只有 `force=True` 这个粗开关，worker 契约写着 "Never use --force, --now"。
- **restore 太窄。** 只有榜面确认归零（`is_critical_restore`）时才放行。latest 赛制下榜面只是变低时，restore 按普通候选评估。
- **重投与恢复不分。** 重投已在榜的 SHA 一律判 0 增益（`ALREADY_ACTIVE_ON_BOARD`），没有区分「再摇一次」和「恢复」。估分器的同源抖动是常数 `SIGMA_HIDDEN_JITTER = 2.70`。
- **开局豁免太短。** 固定为 3 发（`scheduler/probes.py` 的 `OPENING_PROBES`），而上一场 9 题中有 4 题前 3 发内没出分（R6 §8.1）。
- **强制提交的回执仍进校准样本**（R5 §7）。

## 范围

### 1. 意图参数

- `parhelion_submit_gate` 增加 `intent`：`exploit`（默认，现行为）、`probe`、`restore`。
- `opsb submit` 增加 `--intent`。
- 意图写进队列行与回执行，供校准、账本与回放读取。

### 2. probe 规则

- **S1** 照常，包括逃逸锁与 TLE 原样重投。
- **S2**
  - 不用噪声底线。
  - 有估计时，要求 P(gain>0) ≥ `probe_min_prob`（默认 0.3）。
  - 估计未知时也允许，并在回复里标明「未知」。
- **undo**
  - best 赛制下自然有 undo：榜面保留最好分。
  - latest 赛制下，本题必须有一个线上 Pass 过、且分数不低于当前榜面的 SHA 可以回滚，或者榜面为 0 / 未出分。
  - 没有 undo 时按 exploit 评估，回复说明原因。
- **预算**
  - 每题在 `probe_window_hours`（默认与熔断窗口相同，12 小时）内，已判与在途的 probe 合计少于 `probe_per_window`（默认 2）。
  - 超出预算时按 exploit 评估，不拒绝。
- **放行后**
  - status_tag 为 `PROBE`，阶段码 `PROBE_INTENT`。
  - 优先级为权重 × E[max(0,gain)] × 0.5，排在 READY 的 exploit 之后。
  - 回复里给出这一发失败后的 restore 命令。

### 3. restore 规则

- **前提**：SHA 必须在本题有线上 Pass 回执，否则按 exploit 评估并说明。
- **定价**
  - 预期分取这个 SHA 历次 Pass 的平均，每次按 `ScoreEstimator.discount_draw_to_contemporary` 折到当前。
  - σ 取 `scheduler/judge_noise.sigma_for`，取不到时用常数。
- **latest 赛制**
  - 增益 = 预期分 − 当前榜面。
  - 判定沿用 4.2.1 的 S2：≥ 底线为 READY；E>0 且 P≥0.5 时低优先级放行。
  - 榜面为 0，或上一发是 WA/RE/TLE，一律放行。这是对 `is_critical_restore` 的放宽：不再要求「确认归零」。
- **best 赛制**：restore 一个不在榜的 SHA 没有意义，暂缓并说明「best 榜面保留最好分」。
- **重投已在榜的 SHA**
  - 默认仍暂缓。
  - 只有 `intent=probe` 才放行，并计入 probe 预算。
  - 回复里给出这个 SHA 的历次判分与 σ̂，让 worker 看到重摇的代价。

### 4. 开局豁免

- 开关 `opening_until_first_score`（默认开，见 00-PLAN D1）：本题还没有任何有分的 Pass 时，开局豁免持续到首次有分，最多 `opening_max_probes`（默认 8）个判题回执。
- 关掉时回到现行的 3 发。
- 只认判题受理的 submission_id，与现行口径相同。

### 5. 校准样本

- force 或 override 发出的回执，不进估分残差（`estimator` 的 `.case-bench-residuals.json`），也不进逐例校准表（`scheduler/case_calibration.py`）。
- probe 回执照常进，只打标记。

### 6. 契约与显示

- **worker 契约**（`cli/opsb_core/drivers/brief.py`）
  - 保留 "never --force/--now"，加一句用法：投机发用 intent=probe，回滚用 intent=restore。
  - 契约的 1.5KB 上限不变。需要从别处挤出字节，不要截掉末行的数据通道说明。
  - 主控 brief 里的同一段同步修改。
- **显示**
  - `cli/opsb_core/services/gate_view.py` 为新阶段码给出 badge 与下一步。
  - Swift 侧确认未知 badge 有默认样式，不需要新增解码字段。

### 7. 开关

- 新增 `GateConfig` 字段，登记进 `GATE_SWITCHES`：

  | 字段 | 类型 | 说明 |
  |---|---|---|
  | `submit_intents` | bool | 关掉后所有意图按 exploit |
  | `opening_until_first_score` | bool | 开局豁免持续到首次出分 |
  | `opening_max_probes` | int | 开局豁免的上限 |
  | `probe_per_window` | int | 每窗口的 probe 预算 |
  | `probe_window_hours` | float | 预算窗口 |
  | `probe_min_prob` | float | probe 的最低 P(gain>0) |

- `set_switch` 目前只解析 bool 与 float，需要支持 int。

## 复用

- `gate.py`：`probe_allowance`、`_keeps_best`、`_noise_floor`、`get_quota_status` 里的 restore_targets，以及 `_prior_online_pass_shas`；
- `scheduler/candidate_archive.py`：top-3；
- `cli/opsb_core/ledger/auto.py`：`on_gate_link` 关联假设。

## 约束

- 额度不进入任何阈值或暂缓理由。probe 预算是试验预算，按每题的 probe 次数计，与账号额度无关。
- 不改自动提交开关。probe 与 restore 获批后，和 exploit 一样走现有派发，是否自动提交仍由管理员开关决定。
- 不提供绕过 S1 的路径。

## 测试

- 三种意图在 best 与 latest 下的判定，包括：
  - probe 无 undo 时回落为 exploit；
  - 预算用尽后回落为 exploit；
  - restore 的定价与折算；
  - 重投在榜 SHA 的暂缓与 probe 放行；
  - 开局豁免持续到首次有分，以及上限；
  - force 回执被排除出残差；
  - 开关逐个回退。
- 已有门禁测试全部保持通过。需要改的只能是 4.3 有意改变的行为，并在报告里逐条列出。

## 验收

用 P21 回放两场数据，报告以下几项：
- **G4**：榜面归零或判题失败之后，下一发为 restore 时的批准比例，目标 100%。
- **probe 的假想批准**：把 4.2.1 下被暂缓的每一发当作 `intent=probe` 重判，在预算内会批准几发。这些发里有几发当时刷新了最高，以及它们的实际增益与损失之和，对照 +45.21 / −44.23。
- **开局豁免**：ops-bench 各题的开局豁免长度，以及首次出分在第几发。

## 真机

不需要。

## 报告（`docs/v4.3/reports/P23-submit-intents.md`）

- 判定表：意图 × 榜型 × 情形；
- 回放结果；
- 契约改前改后的字节数；
- 工具表字节变化；
- 改变了的已有测试清单。
