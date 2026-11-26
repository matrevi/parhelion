# P25 收官对账与槽位可见性

分支：`impl/P25-close-sweep`。在 P23 合入后的 `main` 上开发：restore 入队用 P23 的 `intent=restore`。

## 为什么

上一场收尾时，有 5 题的在榜分低于各自的历史最高，合计 10.19 裸分，约 23.3 加权分没收回：

| 题 | 历史最高 | 收盘在榜 |
|---|---|---|
| PadV3Grad | 59.98 | 57.72 |
| ConcatD | 77.96 | 75.40 |
| LayerNormV3 | 73.65 | 71.64 |
| PReluGradUpdate | 78.01 | 76.10 |
| MultilabelMarginLoss | 75.54 | 74.09 |

另外还有三处没对齐：
- **两题的历史最高 SHA 只在赛道分支上**：ConfusionTransposeD 的 `235f754`（80.07）只在 `b-track1`；CTCLossV2 的 `dcc7754`（75.11）只在 `b-track2`，而它 `main` 的 HEAD 是一发 RE。
- **`best/*` 标签过期**：还停在前一天的版本。
- **账号槽利用不均**：一个槽打满，另一个只用了 66%，没人发现（00-SUMMARY §4.10、§4.11）。

当前 `main` 的情况：
- `scheduler/gate.py` 的 `get_quota_status` 已经算出 `restore_targets` 与 `needed_closing_restores`，但只用于显示。
- 没有收盘对账的动作。
- 没有每槽利用率。

## 范围

1. **新模块 `scheduler/close_sweep.py`**
   - 逐题比较四样东西：
     - 历史最高 SHA 及其分数：取 `estimator.get_problem_ledger`；
     - 在榜 SHA 及其分数；
     - 题目仓库 `main` 的 HEAD；
     - 包含历史最高 SHA 的分支，以及 `best/*` 标签指向。
   - 输出三张清单：
     - **需要 restore**：latest 赛制下在榜分低于历史最高超过 σ̂，或榜面为 0 或失败。best 赛制下这张单为空。
     - **需要并入**：历史最高 SHA 不在 `main` 上。只列出分支，**不自动合并**任何分支。
     - **需要刷新 `best/*`**。
2. **入口**
   - `opsb close-sweep [--enqueue] [--json]`，经 `scheduler/cli_bridge.py` 的子命令实现。
   - `--enqueue` 把「需要 restore」的每一题，用 `intent=restore` 交给正常门禁入队。之后的派发仍由管理员的自动提交开关决定。不 force，不 approve。
   - `parhelion_report(view=close)`：主控可看全部题，worker 只能看本题。
3. **收官窗口**
   - 配置项 `closing.contest_end`：能从比赛元数据取到就自动填，取不到就留空。
   - 配置项 `closing.window_min`，默认 90 分钟。
   - 窗口内，restore 候选的优先级乘以 `closing.restore_boost`（默认 3）。**只改排序，不改任何门槛**：额度不进门禁。
   - 进入窗口时，给主控收件箱发一条战略消息，附上 close 视图的摘要。按窗口幂等，只发一次。
4. **账号槽利用率**
   - `QuotaStatus` 增加 `per_slot`：每槽当日已用、上限与利用率。
   - 状态卡与 `parhelion_status` 的 brief 在「任一槽利用率 < 60%、且当日已过半」时提示一行。只显示，不影响门禁与派发。
   - 先核实 `gate.py` 的 `_map_aid` 是不是把认不出的 `account_id` 记到了默认槽（R5 §6 的推断）。确有其事就修，并写测试。
   - 报告与显示里不出现账号名或账号 ID，只用槽序号。
5. **Swift**
   - `per_slot` 用 `decodeIfPresent` 解码；界面在调度条的配额提示里显示一行。
   - 如果改动超过 50 行，就只做 CLI/MCP，Swift 列为后续。

## 复用

- `get_quota_status`：restore_targets、`list_eligible_account_slots`；
- `scheduler/candidate_archive.py`；
- `cli/opsb_core/services/git_fast.py`：读 git 元数据不起子进程；分支包含关系可以用一次有界的 `git branch --contains`；
- P23 的 restore 定价。

## 测试

- 三张清单的判定：best 与 latest 两种赛制；历史最高在分支上；标签过期。
- `--enqueue` 只经门禁入队，且不改自动提交开关。
- 窗口内的排序加权与消息幂等。
- `per_slot` 的计算与提示。

## 验收

在 ops-bench 的副本上跑 `opsb close-sweep --json`，不带 `--enqueue`：
- 「需要 restore」应列出上面 5 题，差额合计 10.19；
- 「需要并入」应列出 ConfusionTransposeD 与 CTCLossV2 两题的分支。

题目仓库在 `~/Competitions/ops-bench/` 下的题目仓库目录里（每题一个 git 仓库），只读。如果实际结果与调研不符，逐题说明原因。调研用的是当时的分支快照，见 00-SUMMARY §6 第 4 条。

## 真机

不需要。

## 报告（`docs/v4.3/reports/P25-close-sweep.md`）

- 清单判定规则；
- ops-bench 核对表；
- 窗口与加权配置；
- `_map_aid` 的核实结论；
- Swift 是否改动。
